"""НДС: с 01.01.2026 законная ставка — 22%, 20% и 18% в документы не попадают.

Проверяем не только настройку клиента (её уже проверяет test_tenant_calc), но и
два пути, где 20% могло просочиться мимо неё:

* строка прайса со «Ставкой НДС» 20% — из старой выгрузки или старой базы;
* прямой вызов расчёта с 20% — например, из нового кода или скрипта.
"""

import support  # общий стенд: изолированные данные, сеть запрещена

import os
import unittest
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


if __name__ == "__main__":
    unittest.main()
