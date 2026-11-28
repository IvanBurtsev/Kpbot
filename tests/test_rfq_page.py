"""Страница заявки: цифры, адрес получателя, состояния и защита от мусора.

Регрессии аудита 28.11.2026 — каждая проверка соответствует дефекту карточки:

* итоги в режиме «цены с НДС» склеивались в строку и брали режим из `.env`,
  а не из снимка расчёта клиента;
* получатель с названием компании («ООО «X» <mail@…>») попадал в поле
  `type="email"`, и браузер не давал отправить КП;
* количество «Infinity»/«NaN»/«1e999» роняло запрос в 500;
* одобрение отклонённой заявки писало в журнал «approved» и создавало
  черновик письма, хотя статус не менялся;
* подтверждение строки без количества делало её «в прайсе», но в расчёт
  она не попадала;
* отправленная заявка показывала формы правки и кнопку одобрения, которые
  заведомо отказывали.
"""

import support  # общий стенд: изолированные данные, сеть запрещена

import os
import re
import unittest
from decimal import Decimal

os.environ.setdefault("KPBOT_API_KEY", "test-key")
os.environ.setdefault("KPBOT_UI_PASSWORD", "test-pass")
os.environ.setdefault("KPBOT_SESSION_SECRET", "test-secret")
os.environ.setdefault("KPBOT_PDF_ENABLED", "false")

from fastapi.testclient import TestClient  # noqa: E402

from kpbot.domain import LineStatus, RfqLine  # noqa: E402
from kpbot.web import app, pipeline, settings, store  # noqa: E402


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return match.group(1) if match else ""


def _flash(response) -> str:
    from urllib.parse import unquote

    match = re.search(r"flash=([^&]+)", response.headers.get("location", ""))
    return unquote(match.group(1)) if match else ""


class RfqPageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/ui/login", data={"password": "test-pass"})
        pipeline.ensure_price_list()
        self._saved_settings = store.get_settings()

    def tearDown(self) -> None:
        # Настройки общие для прогона: чужие тесты не должны зависеть от наших.
        store.set_settings(
            {key: self._saved_settings.get(key, "") for key in ("prices_include_vat", "delivery_price")}
        )

    def _create(self, text: str = "уголок 50х50 — 10 шт", sender: str = "client@example.ru") -> str:
        return pipeline.process_text(text, sender=sender, subject="Заявка").rfq_id

    def _lines(self, rfq_id: str) -> list[RfqLine]:
        rfq = store.get_rfq(rfq_id) or {}
        return [
            RfqLine(
                idx=int(row["idx"]),
                raw_text=row["raw_text"],
                qty=Decimal(row["qty"]) if row["qty"] else None,
                unit=row["unit"],
                matched_sku=row["matched_sku"],
                matched_name=row["matched_name"],
                match_score=float(row["score"] or 0),
                status=LineStatus(row["status"]),
                note=row["note"],
            )
            for row in rfq.get("lines", [])
        ]

    # ---------- деньги ----------

    def test_totals_follow_quote_mode_and_are_numbers(self) -> None:
        """Итоги карточки — как в письме: режим из снимка КП, суммы не склеены."""
        store.set_settings({"prices_include_vat": "1"})
        rfq_id = self._create()
        quote = store.get_quote(rfq_id)
        page = self.client.get(f"/ui/rfq/{rfq_id}").text

        self.assertIn("Сумма с НДС по товарам", page)
        gross = Decimal(quote["subtotal"]) + Decimal(quote["vat"])
        self.assertIn(f"{gross:.2f}".replace(".", ","), page)
        # Склейка двух строк давала «282.7962.21» в одном числе.
        self.assertNotIn(quote["subtotal"] + quote["vat"], page)

    def test_totals_without_vat_mode_are_unchanged(self) -> None:
        store.set_settings({"prices_include_vat": ""})
        rfq_id = self._create()
        page = self.client.get(f"/ui/rfq/{rfq_id}").text
        self.assertIn("Сумма без НДС", page)
        self.assertNotIn("Сумма с НДС по товарам", page)

    # ---------- адрес получателя ----------

    def test_send_form_uses_bare_address(self) -> None:
        """Заказ из кабинета приходит как «ООО «X» <mail@…>» — в поле только адрес."""
        from kpbot.web import mailer

        enabled, host = mailer.enabled, mailer.host
        mailer.enabled, mailer.host = True, "smtp.example.ru"
        try:
            rfq_id = self._create(sender="ООО «Гарант-Строй» <snab@example.ru>")
            html = self.client.get(f"/ui/rfq/{rfq_id}").text
            self.client.post(f"/ui/rfq/{rfq_id}/approve", data={"csrf_token": _csrf(html)})
            html = self.client.get(f"/ui/rfq/{rfq_id}").text
            match = re.search(r'<input type="email" name="to" value="([^"]*)"', html)
            self.assertIsNotNone(match)
            self.assertEqual(match.group(1), "snab@example.ru")
        finally:
            mailer.enabled, mailer.host = enabled, host

    def test_send_form_is_hidden_until_approved(self) -> None:
        from kpbot.web import mailer

        enabled, host = mailer.enabled, mailer.host
        mailer.enabled, mailer.host = True, "smtp.example.ru"
        try:
            rfq_id = self._create()
            html = self.client.get(f"/ui/rfq/{rfq_id}").text
            self.assertNotIn(f'action="/ui/rfq/{rfq_id}/send"', html)
            self.assertIn("Сначала одобрите КП", html)
        finally:
            mailer.enabled, mailer.host = enabled, host

    def test_header_injection_in_recipient_is_rejected(self) -> None:
        rfq_id = self._create()
        html = self.client.get(f"/ui/rfq/{rfq_id}").text
        self.client.post(f"/ui/rfq/{rfq_id}/approve", data={"csrf_token": _csrf(html)})
        html = self.client.get(f"/ui/rfq/{rfq_id}").text
        response = self.client.post(
            f"/ui/rfq/{rfq_id}/send",
            data={
                "csrf_token": _csrf(html),
                "to": "victim@example.ru\r\nBcc: evil@example.com",
                "body": "текст",
            },
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("адрес", _flash(response))

    # ---------- количество ----------

    def test_absurd_quantity_is_rejected_with_message(self) -> None:
        rfq_id = self._create()
        before = Decimal(store.get_quote(rfq_id)["total"])
        for bad in ("Infinity", "NaN", "1e999", "abc", "0", "-3"):
            html = self.client.get(f"/ui/rfq/{rfq_id}").text
            response = self.client.post(
                f"/ui/rfq/{rfq_id}/lines/1/update",
                data={"csrf_token": _csrf(html), "sku": "KR-001", "qty": bad},
            )
            self.assertEqual(response.status_code, 303, bad)
            self.assertTrue(_flash(response), bad)
        self.assertEqual(Decimal(store.get_quote(rfq_id)["total"]), before)

    # ---------- статусы ----------

    def test_approve_of_rejected_has_no_side_effects(self) -> None:
        rfq_id = self._create()
        html = self.client.get(f"/ui/rfq/{rfq_id}").text
        self.client.post(f"/ui/rfq/{rfq_id}/reject", data={"csrf_token": _csrf(html), "reason": "нет"})

        actions_before = {row["action"] for row in store.decisions(rfq_id)}
        arts_before = {row["kind"] for row in store.artifacts(rfq_id)}
        html = self.client.get(f"/ui/rfq/{rfq_id}").text
        response = self.client.post(f"/ui/rfq/{rfq_id}/approve", data={"csrf_token": _csrf(html)})

        self.assertEqual(response.status_code, 303)
        self.assertIn("нельзя", _flash(response))
        self.assertNotIn("approved", {row["action"] for row in store.decisions(rfq_id)} - actions_before)
        self.assertFalse({row["kind"] for row in store.artifacts(rfq_id)} - arts_before)
        self.assertEqual(store.get_rfq(rfq_id)["status"], "rejected")

    def test_sent_request_is_read_only(self) -> None:
        rfq_id = self._create()
        store.set_status(rfq_id, "sent", force=True)
        page = self.client.get(f"/ui/rfq/{rfq_id}").text
        self.assertNotIn(f"/ui/rfq/{rfq_id}/lines/1/update", page)
        self.assertNotIn("Одобрить и подготовить ответ", page)
        self.assertIn("правка закрыта", page)
        self.assertIn("КП отправлено", page)
        self.assertNotIn("передано в отправку", page)

    def test_approved_request_hides_approve_button(self) -> None:
        rfq_id = self._create()
        html = self.client.get(f"/ui/rfq/{rfq_id}").text
        self.client.post(f"/ui/rfq/{rfq_id}/approve", data={"csrf_token": _csrf(html)})
        page = self.client.get(f"/ui/rfq/{rfq_id}").text
        self.assertNotIn("Одобрить и подготовить ответ", page)
        self.assertIn("Отклонить", page)

    # ---------- строки ----------

    def test_line_can_be_replaced_by_name(self) -> None:
        rfq_id = self._create("монтаж кабель-канала — 100 м")
        item = store.price_items()[0]
        html = self.client.get(f"/ui/rfq/{rfq_id}").text
        response = self.client.post(
            f"/ui/rfq/{rfq_id}/lines/1/update",
            data={"csrf_token": _csrf(html), "sku": item.name, "qty": "2"},
        )
        self.assertEqual(response.status_code, 303)
        line = store.get_rfq(rfq_id)["lines"][0]
        self.assertEqual(line["matched_sku"], item.sku)
        self.assertEqual(line["status"], "ok")
        self.assertEqual(Decimal(store.get_rfq(rfq_id)["lines"][0]["qty"]), Decimal("2"))

    def test_confirm_requires_quantity(self) -> None:
        rfq_id = self._create()
        # Строка с подобранной позицией, но без количества — так выглядит
        # письмо «подрозетник без числа».
        line = self._lines(rfq_id)[0]
        line.qty = None
        line.status = LineStatus.AMBIGUOUS
        pipeline.apply_edits(rfq_id, [line], actor="test", note="без количества")

        html = self.client.get(f"/ui/rfq/{rfq_id}").text
        response = self.client.post(
            f"/ui/rfq/{rfq_id}/lines/1/confirm", data={"csrf_token": _csrf(html)}
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("количество", _flash(response))
        self.assertNotEqual(store.get_rfq(rfq_id)["lines"][0]["status"], "ok")

    def test_confirm_applies_edited_quantity(self) -> None:
        """Правка количества и «Подтвердить» в одной форме: теряться не должна."""
        rfq_id = self._create()
        line = self._lines(rfq_id)[0]
        line.qty = Decimal("5")
        line.status = LineStatus.AMBIGUOUS
        pipeline.apply_edits(rfq_id, [line], actor="test", note="похожая строка")

        html = self.client.get(f"/ui/rfq/{rfq_id}").text
        response = self.client.post(
            f"/ui/rfq/{rfq_id}/lines/1/confirm",
            data={"csrf_token": _csrf(html), "qty": "7"},
        )
        self.assertEqual(response.status_code, 303)
        after = store.get_rfq(rfq_id)["lines"][0]
        self.assertEqual(after["status"], "ok")
        self.assertEqual(Decimal(after["qty"]), Decimal("7"))

    def test_empty_quote_is_explained(self) -> None:
        rfq_id = self._create()
        html = self.client.get(f"/ui/rfq/{rfq_id}").text
        self.client.post(f"/ui/rfq/{rfq_id}/lines/1/delete", data={"csrf_token": _csrf(html)})
        page = self.client.get(f"/ui/rfq/{rfq_id}").text
        self.assertIn("нет ни одной позиции", page)
        self.assertNotIn("0 требуют уточнения", page)

    # ---------- исполнитель ----------

    def test_manager_can_take_free_request(self) -> None:
        """Менеджер берёт свободную заявку себе — кнопка и сервер совпадают."""
        manager = store.create_user(
            email="take-card@local", password="take-pass-1", role="manager", name="Менеджер"
        )
        client = TestClient(app, follow_redirects=False)
        login = client.post(
            "/ui/login", data={"email": "take-card@local", "password": "take-pass-1"}
        )
        self.assertEqual(login.status_code, 303)

        rfq_id = self._create()
        page = client.get(f"/ui/rfq/{rfq_id}").text
        self.assertIn("Взять себе", page)
        response = client.post(
            f"/ui/rfq/{rfq_id}/assign", data={"csrf_token": _csrf(page)}
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(store.get_rfq(rfq_id)["assignee"], manager.id)

    def test_shared_login_cannot_take_request(self) -> None:
        """Общий пароль: личности нет — кнопки нет и сервер объясняет почему."""
        rfq_id = self._create()
        page = self.client.get(f"/ui/rfq/{rfq_id}").text
        self.assertNotIn("Взять себе", page)
        response = self.client.post(
            f"/ui/rfq/{rfq_id}/assign", data={"csrf_token": _csrf(page)}
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("учётной записью", _flash(response))
        self.assertIn(store.get_rfq(rfq_id)["assignee"], (None, ""))

    # ---------- дата письма ----------

    def test_letter_date_is_human_readable(self) -> None:
        raw = (
            b"From: Ivan <ivan@example.ru>\r\nSubject: test\r\n"
            b"Date: Fri, 20 Nov 2026 10:00:00 +0300\r\n"
            b"Message-ID: <date-test@example.ru>\r\nMIME-Version: 1.0\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"\xd1\x83\xd0\xb3\xd0\xbe\xd0\xbb\xd0\xbe\xd0\xba 50\xd1\x8550 - 3 \xd1\x88\xd1\x82\r\n"
        )
        rfq_id = pipeline.process_eml_bytes(raw, actor="test").rfq_id
        page = self.client.get(f"/ui/rfq/{rfq_id}").text
        self.assertIn("20.11.2026 10:00", page)
        self.assertNotIn("Fri, 20 Nov", page)


if __name__ == "__main__":
    unittest.main()
