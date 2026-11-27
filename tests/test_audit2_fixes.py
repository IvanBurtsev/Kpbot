"""Регрессии повторного аудита: P0-путь PDF, импорт, обучение, 1С, кабинет.

Каждый тест — про конкретный дефект, найденный при разборе кода, а не про
«вообще работает»: если кто-то вернёт прежнее поведение, тест упадёт с
понятным сообщением.
"""

import support  # общий стенд: изолированные данные, сеть запрещена

import argparse
import shutil
import unittest
import uuid
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from kpbot import cli as cli_module
from kpbot.cabinet_store import CabinetStore
from kpbot.config import Settings
from kpbot.domain import LineStatus, RfqLine, ValidationError
from kpbot.logs import redact, register_settings_secrets
from kpbot.one_c import OneCClient, parse_balance
from kpbot.pipeline import Pipeline
from kpbot.pricelist import load_price_items, load_stock_items
from kpbot.security import make_user_session
from kpbot.store import Store

TMP = support.subdir("audit2")


def make_settings(name: str, **overrides) -> Settings:
    data_dir = TMP / name
    shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(parents=True)
    base = dict(
        data_dir=data_dir,
        api_key="test-key",
        ui_password="test-pass",
        session_secret="test-secret",
        pdf_enabled=False,
        llm_enabled=False,
    )
    base.update(overrides)
    return Settings(**base)


class PdfEnabledTest(unittest.TestCase):
    """P0: при KPBOT_PDF_ENABLED=true приём письма падал с AttributeError."""

    def setUp(self) -> None:
        import kpbot.pipeline as pipeline_module

        pipeline_module._pdf_works = None
        pipeline_module._pdf_failed_at = 0.0
        self.settings = make_settings("pdf-enabled", pdf_enabled=True)
        self.store = Store(self.settings.db_path)
        self.pipeline = Pipeline(self.store, self.settings)

    def tearDown(self) -> None:
        shutil.rmtree(self.settings.data_dir, ignore_errors=True)

    def test_ingest_with_pdf_enabled_builds_documents(self) -> None:
        with patch("kpbot.pipeline.html_to_pdf", return_value=(False, "нет браузера в тесте")):
            result = self.pipeline.process_text(
                "уголок 50х50 — 2 шт", sender="a@b.ru", subject="Заявка"
            )
        self.assertEqual(result.status, "draft_ready")
        self.assertTrue(Path(result.html_path).exists(), "HTML КП не создан")
        self.assertTrue(Path(result.docx_path).exists(), "DOCX КП не создан")
        # Ошибка PDF больше не роняет пайплайн и не теряет заявку.
        self.assertIn("нет браузера", result.pdf_error)


class PriceImportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = TMP / f"price-{uuid.uuid4().hex[:8]}"
        self.dir.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name: str, text: str, encoding: str = "utf-8-sig") -> Path:
        path = self.dir / name
        path.write_text(text, encoding=encoding)
        return path

    def test_nbsp_thousands_are_not_lost(self) -> None:
        path = self.write(
            "nbsp.csv",
            "SKU;Наименование;Ед.;Цена\n"
            "A-1;Уголок 50х50;шт;1\u00a0234,50\n"
            "A-2;Труба 20;м;96,00\n",
        )
        items = {item.sku: item for item in load_price_items(path)}
        self.assertIn("A-1", items, "строка с неразрывным пробелом выпала из прайса")
        self.assertEqual(items["A-1"].price, Decimal("1234.50"))

    def test_duplicate_sku_does_not_break_import(self) -> None:
        path = self.write(
            "dup.csv",
            "SKU;Наименование;Ед.;Цена\n"
            "A-1;Уголок 50х50;шт;34,50\n"
            "A-1;Уголок 50х50 новый;шт;35,00\n",
        )
        items = load_price_items(path)
        self.assertEqual(len(items), 1, "дубль артикула должен схлопнуться, а не уронить загрузку")
        self.assertEqual(items[0].price, Decimal("35.00"))

    def test_cp1251_file_is_read(self) -> None:
        path = self.dir / "cp.csv"
        path.write_bytes(
            "SKU;Наименование;Ед.;Цена\nA-1;Уголок 50х50;шт;34,50\n".encode("cp1251")
        )
        self.assertEqual(load_price_items(path)[0].name, "Уголок 50х50")

    def test_vat_in_words_is_detected(self) -> None:
        for index, header in enumerate(("Цена с учётом НДС", "Цена, в т.ч. НДС")):
            path = self.write(
                f"vat-{index}.csv",
                f"Код;Наименование;Ед.;{header}\nA-1;Уголок;шт;1220\n",
            )
            with self.assertRaises(ValidationError, msg=f"не распознана колонка «{header}»"):
                load_price_items(path)

    def test_headerless_file_keeps_first_row(self) -> None:
        path = self.write("nohead.csv", "KR-001;Уголок 50х50;шт;34,50\nKR-002;Труба 20;м;96\n")
        items = load_price_items(path)
        self.assertEqual(len(items), 2, "первая строка файла без шапки потерялась")

    def test_unrecognized_header_is_refused(self) -> None:
        path = self.write("badhead.csv", "Наименование;Артикул;Ед.;Прайс\nУголок;KR-001;шт;34,50\n")
        with self.assertRaises(ValidationError):
            load_price_items(path)

    def test_stock_rows_are_summed_across_warehouses(self) -> None:
        path = self.write(
            "stock.csv",
            "Артикул;Наименование;Количество;Склад\n"
            "KR-001;Уголок;10;Основной\n"
            "KR-001;Уголок;5;Резервный\n"
            "KR-002;Труба;3;Основной\n",
        )
        rows = {row[0]: row for row in load_stock_items(path)}
        self.assertEqual(Decimal(rows["KR-001"][1]), Decimal("15"), "остатки складов не сложились")
        self.assertEqual(len(rows), 2)


class OneCClientTest(unittest.TestCase):
    def client(self, handler) -> OneCClient:
        http = httpx.Client(transport=httpx.MockTransport(handler), timeout=5)
        return OneCClient("https://1c.example/base", "user", "pass", http=http)

    def test_multi_warehouse_balance_is_summed(self) -> None:
        payload = {
            "value": [
                {"Склад": "Основной", "Номенклатура": "Уголок", "Артикул": "KR-001",
                 "КоличествоОстаток": 10},
                {"Склад": "Резервный", "Номенклатура": "Уголок", "Артикул": "KR-001",
                 "КоличествоОстаток": 5},
            ]
        }
        client = self.client(lambda request: httpx.Response(200, json=payload))
        rows = client.stock()
        self.assertEqual(len(rows), 1, "остаток одного артикула на двух складах дал две строки")
        self.assertEqual(Decimal(rows[0][1]), Decimal("15"))

    def test_key_field_is_not_used_as_presentation(self) -> None:
        payload = {
            "value": [
                {
                    "Номенклатура_Key": "22222222-2222-2222-2222-222222222222",
                    "Номенклатура": "Уголок монтажный 50х50",
                    "КоличествоОстаток": 7,
                }
            ]
        }
        rows = parse_balance(payload)
        self.assertEqual(rows[0][0], "Уголок монтажный 50х50")

    def test_next_link_pages_are_read(self) -> None:
        pages = {
            1: {
                "value": [{"Номенклатура": "Уголок", "Артикул": "KR-001", "КоличествоОстаток": 1}],
                "odata.nextLink": "https://1c.example/base/odata/standard.odata/page2",
            },
            2: {
                "value": [{"Номенклатура": "Труба", "Артикул": "SN-001", "КоличествоОстаток": 2}]
            },
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=pages[2 if "page2" in str(request.url) else 1])

        rows = self.client(handler).stock()
        self.assertEqual({row[0] for row in rows}, {"KR-001", "SN-001"})


class LearningShiftTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = make_settings("learning")
        self.store = Store(self.settings.db_path)
        self.pipeline = Pipeline(self.store, self.settings)
        self.pipeline.ensure_price_list()

    def tearDown(self) -> None:
        shutil.rmtree(self.settings.data_dir, ignore_errors=True)

    def _confirm_once(self, phrase: str) -> None:
        """Одна заявка с похожей строкой, которую человек подтвердил."""
        ambiguous = RfqLine(
            idx=1, raw_text=phrase, qty=Decimal("1"), unit="шт",
            matched_sku="KR-001", matched_name="Уголок", match_score=0.6,
            status=LineStatus.AMBIGUOUS,
        )
        rfq_id = uuid.uuid4().hex
        self.store.save_rfq(
            rfq_id=rfq_id, message_id=f"c-{rfq_id}", sender="a@b.ru", subject="Заявка",
            received_at="", status="human_review", raw_text=phrase, lines=[ambiguous],
        )
        confirmed = [
            RfqLine(
                idx=1, raw_text=phrase, qty=Decimal("1"), unit="шт",
                matched_sku="KR-001", matched_name="Уголок", match_score=1.0,
                status=LineStatus.OK,
            )
        ]
        self.pipeline.apply_edits(rfq_id, confirmed, actor="unittest")

    def test_two_confirmations_make_pair_learned(self) -> None:
        phrase = "швеллер гнутый 100х50"
        key = self.pipeline.learn_key(phrase)
        self._confirm_once(phrase)
        self.assertIsNone(self.store.learned_sku(key), "одного подтверждения мало")
        self._confirm_once(phrase)
        self.assertEqual(
            self.store.learned_sku(key), "KR-001",
            "два подтверждения похожей строки не включили выученное соответствие",
        )

    def test_deleting_middle_line_does_not_teach_wrong_pairs(self) -> None:
        lines = [
            RfqLine(idx=1, raw_text="уголок 50х50", qty=Decimal("10"), unit="шт",
                    matched_sku="KR-001", matched_name="Уголок", status=LineStatus.OK),
            RfqLine(idx=2, raw_text="муфта ППР 25", qty=Decimal("5"), unit="шт",
                    matched_sku="SN-005", matched_name="Муфта", status=LineStatus.OK),
            RfqLine(idx=3, raw_text="труба ППР 25", qty=Decimal("3"), unit="м",
                    matched_sku="SN-001", matched_name="Труба", status=LineStatus.OK),
        ]
        rfq_id = uuid.uuid4().hex
        self.store.save_rfq(
            rfq_id=rfq_id, message_id=f"m-{rfq_id}", sender="a@b.ru", subject="Заявка",
            received_at="", status="draft_ready", raw_text="x", lines=lines,
        )
        # человек удалил первую строку: номера оставшихся сдвинулись
        self.pipeline.apply_edits(rfq_id, lines[1:], actor="unittest")
        pairs = {row["key"]: row for row in self.store.learned_pairs()}
        # Ни одна оставшаяся строка не менялась — учить нечему. Старая сверка «по
        # idx» записывала «муфта → SN-005» и «труба → SN-001» как новые
        # подтверждения, а потом ещё и промахи по ним же: удаление одной строки
        # портило две чужие пары.
        self.assertEqual(pairs, {}, f"удаление строки создало ложное обучение: {pairs}")


class _FakeMailbox:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.seen: list[str] = []

    def check(self) -> dict:
        return {"folder": "INBOX", "total": 1, "unseen": 1}

    def fetch_unseen(self, limit: int = 10):
        return [("1", self.raw)]

    def fetch_recent(self, *args, **kwargs):
        return []

    def mark_seen(self, uids) -> None:
        self.seen.extend(uids)


class _FailingPipeline:
    def __init__(self, out_dir: Path) -> None:
        self._out = out_dir

    def process_eml_bytes(self, raw: bytes, actor: str = ""):
        raise RuntimeError("внезапный сбой разбора")

    def out_dir(self) -> Path:
        return self._out


class PollFailureTest(unittest.TestCase):
    """README обещает: при сбое письмо остаётся непрочитанным."""

    def setUp(self) -> None:
        self.dir = TMP / f"poll-{uuid.uuid4().hex[:8]}"
        self.dir.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_failed_letter_is_not_marked_seen(self) -> None:
        mailbox = _FakeMailbox(b"From: a@b.ru\nSubject: test\n\nbody")
        args = argparse.Namespace(dry_run=False, limit=10)
        status = cli_module._poll_mailbox(_FailingPipeline(self.dir), mailbox, args)
        self.assertEqual(status, 1, "сбой обработки должен давать ненулевой код")
        self.assertEqual(mailbox.seen, [], "письмо со сбоем помечено прочитанным")
        self.assertTrue((self.dir / "failed.jsonl").exists(), "сбой не записан в failed.jsonl")


class CabinetRegressionTest(unittest.TestCase):
    """Кабинет: сброс пароля в тенанте, блокировка компании, корзина."""

    def setUp(self) -> None:
        from kpbot.web import app, registry, settings

        self.app, self.registry, self.settings = app, registry, settings
        suffix = uuid.uuid4().hex[:8]
        self.suffix = suffix
        self.path = TMP / f"tenant-{suffix}.sqlite3"
        self.tenant = registry.create(f"Поставщик-{suffix}", self.path)
        store = CabinetStore(self.path)
        self.company = store.create_company(f"Покупатель-{suffix}", inn="7700000000")
        self.password = "password1"
        self.user = store.create_user(
            self.company["id"], email=f"client-{suffix}@test.ru",
            password=self.password, role="client_admin",
        )
        self.client = TestClient(app, follow_redirects=False)

    def test_reset_link_works_without_tenant_cookie(self) -> None:
        token = CabinetStore(self.path).create_password_reset(self.user["id"])
        fresh = TestClient(self.app, follow_redirects=False)
        page = fresh.get(f"/cabinet/reset?t={token}&c={self.tenant['id']}")
        self.assertEqual(page.status_code, 200, "ссылка сброса не открылась без куки")
        response = fresh.post(
            "/cabinet/reset",
            data={"token": token, "tenant": self.tenant["id"],
                  "new": "newpassword9", "repeat": "newpassword9"},
        )
        self.assertEqual(response.status_code, 303)
        self.assertIsNotNone(
            CabinetStore(self.path).authenticate(self.user["email"], "newpassword9"),
            "пароль не сменился через ссылку без куки",
        )

    def test_blocked_company_loses_session_immediately(self) -> None:
        login = self.client.post(
            "/cabinet/login",
            data={"email": self.user["email"], "password": self.password,
                  "tenant": self.tenant["id"]},
        )
        self.assertEqual(login.status_code, 303)
        self.assertEqual(self.client.get("/cabinet/catalog").status_code, 200)
        CabinetStore(self.path).update_finance(self.company["id"], status="blocked")
        blocked = self.client.get("/cabinet/catalog")
        self.assertEqual(blocked.status_code, 303, "заблокированная компания осталась в кабинете")

    def test_huge_cart_quantity_does_not_break_cart(self) -> None:
        from kpbot.cabinet import CLIENT_COOKIE
        from kpbot.security import csrf_for

        login = self.client.post(
            "/cabinet/login",
            data={"email": self.user["email"], "password": self.password,
                  "tenant": self.tenant["id"]},
        )
        self.assertEqual(login.status_code, 303)
        token = csrf_for(self.settings.session_secret, self.client.cookies.get(CLIENT_COOKIE))
        self.client.post(
            "/cabinet/cart/add",
            data={"csrf_token": token, "sku": "KR-001", "qty": "2000000"},
        )
        page = self.client.get("/cabinet/cart")
        self.assertEqual(page.status_code, 200, "корзина падает на большом количестве")


class TenantHeaderPrecedenceTest(unittest.TestCase):
    """Ключ клиента B + X-Tenant B не должен писать в базу A по куке."""

    def test_api_header_wins_over_cookie(self) -> None:
        from kpbot.web import TENANT_COOKIE, app, registry, settings

        suffix = uuid.uuid4().hex[:8]
        path_a = TMP / f"tenant-a-{suffix}.sqlite3"
        path_b = TMP / f"tenant-b-{suffix}.sqlite3"
        tenant_a = registry.create(f"A-{suffix}", path_a)
        tenant_b = registry.create(f"B-{suffix}", path_b)
        # Базы должны существовать: без файла клиент не обслуживается.
        Store(path_a)
        Store(path_b)
        key_b = registry.issue_key(tenant_b["id"])

        client = TestClient(app, follow_redirects=False)
        client.cookies.set(
            TENANT_COOKIE,
            make_user_session(settings.session_secret, tenant_a["id"], 86400),
        )
        message_id = f"<precedence-{suffix}@test>"
        response = client.post(
            "/api/v1/rfqs",
            headers={"X-API-Key": key_b, "X-Tenant": tenant_b["id"]},
            json={"sender": "a@b.ru", "subject": "Заявка", "text": "уголок 50х50 — 2 шт",
                  "message_id": message_id},
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertIsNotNone(
            Store(path_b).find_rfq_by_message(message_id),
            "заявка не попала в базу клиента из X-Tenant",
        )
        self.assertIsNone(
            Store(path_a).find_rfq_by_message(message_id),
            "заявка записана в базу клиента из куки при чужом X-Tenant",
        )


class SendKpStatusTest(unittest.TestCase):
    """CLI send-kp: после фактической отправки заявка должна стать «отправлена»."""

    def test_cli_send_marks_rfq_sent(self) -> None:
        import argparse
        from unittest.mock import patch

        from kpbot import cli

        cfg = make_settings("send-kp", smtp_enabled=True, smtp_host="smtp.example",
                            smtp_user="robot@example.ru", smtp_password="x")
        store = Store(cfg.db_path)
        pipeline = Pipeline(store, cfg)
        result = pipeline.process_text("уголок 50х50 — 2 шт", sender="client@example.ru",
                                       subject="Заявка")
        pipeline.approve(result.rfq_id, actor="unittest")
        # Вложение нужно, иначе отправка законно откажется.
        pdf = cfg.out_dir / f"{result.rfq_id}.pdf"
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(b"%PDF-1.4\n" + b"0" * 2000)
        with patch("kpbot.cli.settings", cfg), \
                patch("kpbot.mailer.Mailer.send", return_value=None):
            code = cli.cmd_send_kp(
                argparse.Namespace(rfq_id=result.rfq_id, to="client@example.ru", body="")
            )
        self.assertEqual(code, 0)
        self.assertEqual(
            store.get_rfq(result.rfq_id)["status"], "sent",
            "после отправки статус остался «одобрено» — карточка врёт",
        )
        shutil.rmtree(cfg.data_dir, ignore_errors=True)


class LogsRedactionTest(unittest.TestCase):
    def test_integration_passwords_are_masked(self) -> None:
        settings = make_settings(
            "logs", smtp_password="smtp-secret-123", one_c_password="onec-secret-456",
            one_c_service_token="service-token-789", ui_password="ui-pass-1",
        )
        register_settings_secrets(settings)
        text = "ошибка: smtp-secret-123 / onec-secret-456 / service-token-789"
        masked = redact(text)
        for secret in ("smtp-secret-123", "onec-secret-456", "service-token-789"):
            self.assertNotIn(secret, masked)
        shutil.rmtree(settings.data_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
