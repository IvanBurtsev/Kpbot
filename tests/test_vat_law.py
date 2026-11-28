"""НДС: с 01.01.2026 законная ставка — 22%, 20% и 18% в документы не попадают.

Проверяем не только настройку клиента (её уже проверяет test_tenant_calc), но и
два пути, где 20% могло просочиться мимо неё:

* строка прайса со «Ставкой НДС» 20% — из старой выгрузки или старой базы;
* прямой вызов расчёта с 20% — например, из нового кода или скрипта.
"""

import support  # общий стенд: изолированные данные, сеть запрещена

import os
import unittest
import uuid
from urllib.parse import unquote
from decimal import Decimal

os.environ.setdefault("KPBOT_PDF_ENABLED", "false")

from kpbot.domain import (  # noqa: E402
    LEGAL_VAT_RATES,
    LineStatus,
    PriceItem,
    RfqLine,
    ValidationError,
    check_vat_rate,
    price_quote,
)
from kpbot.pipeline import Pipeline  # noqa: E402
from kpbot.store import Store  # noqa: E402
from kpbot.config import Settings  # noqa: E402


def _line() -> RfqLine:
    return RfqLine(
        idx=1, raw_text="уголок", qty=Decimal("1"), unit="шт",
        matched_sku="A", match_score=1.0, matched_name="Уголок",
        status=LineStatus.OK,
    )


class VatLawTest(unittest.TestCase):
    def test_old_rates_are_rejected_by_calculation(self) -> None:
        for rate in ("0.20", "0.18"):
            with self.subTest(rate=rate):
                with self.assertRaises(ValidationError):
                    price_quote([_line()], {"A": PriceItem("A", "Уголок", "шт", Decimal("100"))},
                                vat_rate=Decimal(rate))

    def test_legal_rates_are_accepted(self) -> None:
        for rate in ("0", "0.05", "0.07", "0.10", "0.22"):
            with self.subTest(rate=rate):
                quote = price_quote(
                    [_line()],
                    {"A": PriceItem("A", "Уголок", "шт", Decimal("100"))},
                    vat_rate=Decimal(rate),
                )
                self.assertEqual(len(quote.lines), 1)
                self.assertEqual(quote.lines[0].vat_rate, Decimal(rate))

    def test_line_rate_from_old_price_is_rejected(self) -> None:
        item = PriceItem("A", "Уголок", "шт", Decimal("100"), vat_rate=Decimal("0.20"))
        with self.assertRaises(ValidationError):
            price_quote([_line()], {"A": item}, vat_rate=Decimal("0.22"))

    def test_check_vat_rate_message_names_the_law(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            check_vat_rate(Decimal("0.20"))
        self.assertIn("01.01.2026", str(ctx.exception))
        self.assertIn("22%", str(ctx.exception))
        self.assertEqual(
            LEGAL_VAT_RATES, {Decimal("0"), Decimal("0.05"), Decimal("0.07"),
                              Decimal("0.10"), Decimal("0.22")}
        )


class LegacyPriceRowTest(unittest.TestCase):
    """Старая строка прайса с 20% не должна выпускать документ с 20%."""

    def setUp(self) -> None:
        self.store = Store(support.subdir("vat-law") / "kpbot.sqlite3")
        self.store.replace_price_items(
            [
                PriceItem("A", "Уголок монтажный", "шт", Decimal("100"), vat_rate=Decimal("0.20")),
                PriceItem("B", "Кабель", "м", Decimal("50"), vat_rate=Decimal("0.10")),
            ]
        )
        self.pipeline = Pipeline(self.store, Settings(data_dir=support.ROOT, pdf_enabled=False))

    def test_20_percent_in_base_is_neutralized(self) -> None:
        items = {item.sku: item for item in self.store.price_items()}
        self.assertIsNone(items["A"].vat_rate, "20% не должно пережить чтение прайса")
        self.assertEqual(items["B"].vat_rate, Decimal("0.10"), "10% — законная, остаётся")

    def test_quote_uses_client_rate_for_legacy_line(self) -> None:
        lines, quote, _is_request = self.pipeline.analyze("уголок монтажный — 2 шт")
        self.assertEqual(len(quote.lines), 1)
        self.assertEqual(quote.lines[0].vat_rate, Decimal("0.22"))
        self.assertEqual(quote.vat, Decimal("44.00"))
        self.assertEqual(quote.total, Decimal("244.00"))


class LegacyQuoteCardTest(unittest.TestCase):
    """Снимок с 20%: карточка предупреждает, одобрение блокируется, пересчёт чинит."""

    def setUp(self) -> None:
        from fastapi.testclient import TestClient
        from kpbot.web import app, pipeline, store

        self.store = store
        self.pipeline = pipeline
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/ui/login", data={"password": "test-pass"})
        self.rfq_id = pipeline.process_text(
            "уголок 50х50 — 2 шт", sender="legacy@example.ru", subject="Ставка 20%"
        ).rfq_id
        self._save_legacy_quote()

    def _save_legacy_quote(self) -> None:
        from kpbot.domain import Quote, QuoteLine

        quote = Quote(
            lines=[
                QuoteLine(
                    sku="KR-001", name="Уголок монтажный 50х50", unit="шт",
                    qty=Decimal("2"), price=Decimal("34.50"), amount=Decimal("69.00"),
                    net=Decimal("69.00"), vat=Decimal("13.80"), vat_rate=Decimal("0.20"),
                )
            ],
            subtotal=Decimal("69.00"), vat=Decimal("13.80"), delivery=Decimal("0.00"),
            total=Decimal("82.80"), vat_rate=Decimal("0.20"), requires_review=False,
            unmatched=0, prices_include_vat=False,
        )
        self.store.save_quote(uuid.uuid4().hex, self.rfq_id, quote)

    def _csrf(self) -> str:
        import re

        html = self.client.get(f"/ui/rfq/{self.rfq_id}").text
        match = re.search(r'name="csrf_token" value="([^"]+)"', html)
        return match.group(1) if match else ""

    def test_card_warns_and_hides_approve(self) -> None:
        page = self.client.get(f"/ui/rfq/{self.rfq_id}").text
        self.assertIn("не применяется", page)
        self.assertIn("Пересчитать по действующей ставке", page)
        self.assertNotIn("Одобрить и подготовить ответ", page)

    def test_approve_is_blocked(self) -> None:
        response = self.client.post(
            f"/ui/rfq/{self.rfq_id}/approve", data={"csrf_token": self._csrf()}
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("Пересчитайте", unquote(response.headers.get("location", "")))
        self.assertNotEqual(self.store.get_rfq(self.rfq_id)["status"], "approved")

    def test_reprice_moves_to_legal_rate(self) -> None:
        response = self.client.post(
            f"/ui/rfq/{self.rfq_id}/reprice", data={"csrf_token": self._csrf()}
        )
        self.assertEqual(response.status_code, 303)
        quote = self.store.get_quote(self.rfq_id)
        self.assertEqual(Decimal(quote["vat_rate"]), Decimal("0.22"))
        self.assertEqual(Decimal(quote["lines"][0]["vat_rate"]), Decimal("0.22"))
        page = self.client.get(f"/ui/rfq/{self.rfq_id}").text
        self.assertNotIn("не применяется", page)
        self.assertIn("Одобрить и подготовить ответ", page)

    def test_card_shows_kp_number_and_vat_column(self) -> None:
        page = self.client.get(f"/ui/rfq/{self.rfq_id}").text
        self.assertIn("КП №", page)
        self.assertIn(">НДС</th>", page.replace("\n", ""))
        self.assertIn(">20%</td>", page.replace("\n", ""))

    def test_sent_request_can_be_reopened_with_force(self) -> None:
        self.store.set_status(self.rfq_id, "sent", force=True)
        response = self.client.post(
            f"/ui/rfq/{self.rfq_id}/reprice",
            data={"csrf_token": self._csrf(), "force": "1"},
        )
        self.assertEqual(response.status_code, 303)
        quote = self.store.get_quote(self.rfq_id)
        self.assertEqual(Decimal(quote["vat_rate"]), Decimal("0.22"))
        self.assertIn(
            self.store.get_rfq(self.rfq_id)["status"], {"human_review", "draft_ready"},
            "исправленная заявка должна вернуться в работу",
        )

    def test_sent_request_is_not_repriced(self) -> None:
        self.store.set_status(self.rfq_id, "sent", force=True)
        response = self.client.post(
            f"/ui/rfq/{self.rfq_id}/reprice", data={"csrf_token": self._csrf()}
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn(
            "уже отправлена", unquote(response.headers.get("location", "")),
            "ожидали отказ «уже отправлена»",
        )
        self.assertEqual(Decimal(self.store.get_quote(self.rfq_id)["vat_rate"]), Decimal("0.20"))


if __name__ == "__main__":
    unittest.main()
