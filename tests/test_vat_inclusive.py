"""Цены в прайсе уже с НДС: НДС выделяется, а не начисляется второй раз."""

import support  # общий стенд

import os
import unittest
from decimal import Decimal

os.environ["KPBOT_API_KEY"] = "test-key"
os.environ["KPBOT_UI_PASSWORD"] = "test-pass"
os.environ["KPBOT_SESSION_SECRET"] = "test-secret"

from kpbot.config import Settings  # noqa: E402
from kpbot.domain import LineStatus, PriceItem, RfqLine, price_quote  # noqa: E402
from kpbot.mailparse import ParsedEmail  # noqa: E402
from kpbot.pipeline import Pipeline, build_reply_text  # noqa: E402
from kpbot.store import Store  # noqa: E402


def _quote(delivery: str, *, inclusive: bool, price: str = "122.00"):
    line = RfqLine(
        idx=1, raw_text="уголок", qty=Decimal("10"), unit="шт",
        matched_sku="KR-001", matched_name="Уголок", match_score=1.0,
        status=LineStatus.OK,
    )
    item = PriceItem("KR-001", "Уголок", "шт", Decimal(price))
    return price_quote(
        [line], {"KR-001": item}, vat_rate=Decimal("0.22"),
        delivery=Decimal(delivery), prices_include_vat=inclusive,
    )


def _store(name: str) -> Store:
    return Store(support.ROOT / f"{name}-{os.getpid()}.sqlite3")


class VatInclusiveTest(unittest.TestCase):
    def test_gross_price_does_not_add_vat_again(self) -> None:
        store = _store("vat-gross")
        store.replace_price_items(
            [PriceItem(sku="KR-001", name="Уголок 50х50", unit="шт", price=Decimal("122.00"))]
        )
        pipeline = Pipeline(store, Settings(prices_include_vat=True, vat_rate=Decimal("0.22")))
        result = pipeline.process_text(
            "уголок 50х50 — 10 шт", sender="a@b.ru", subject="Заявка", message_id="vat-gross-1"
        )
        quote = store.get_quote(result.rfq_id)
        self.assertEqual(quote["subtotal"], "1000.00")
        self.assertEqual(quote["vat"], "220.00")
        self.assertEqual(quote["total"], "1220.00")

    def test_net_price_still_adds_vat(self) -> None:
        store = _store("vat-net")
        store.replace_price_items(
            [PriceItem(sku="KR-001", name="Уголок 50х50", unit="шт", price=Decimal("100.00"))]
        )
        pipeline = Pipeline(store, Settings(prices_include_vat=False, vat_rate=Decimal("0.22")))
        result = pipeline.process_text(
            "уголок 50х50 — 10 шт", sender="a@b.ru", subject="Заявка", message_id="vat-net-1"
        )
        quote = store.get_quote(result.rfq_id)
        self.assertEqual(quote["subtotal"], "1000.00")
        self.assertEqual(quote["vat"], "220.00")
        self.assertEqual(quote["total"], "1220.00")

    def test_zero_rate_with_gross_prices(self) -> None:
        store = _store("vat-zero")
        store.replace_price_items(
            [PriceItem(sku="KR-001", name="Уголок 50х50", unit="шт", price=Decimal("100.00"))]
        )
        pipeline = Pipeline(store, Settings(prices_include_vat=True, vat_rate=Decimal("0")))
        result = pipeline.process_text(
            "уголок 50х50 — 3 шт", sender="a@b.ru", subject="Заявка", message_id="vat-zero-1"
        )
        quote = store.get_quote(result.rfq_id)
        self.assertEqual(quote["subtotal"], "300.00")
        self.assertEqual(quote["vat"], "0.00")
        self.assertEqual(quote["total"], "300.00")

    def test_inclusive_delivery_reply_adds_up(self) -> None:
        """1000 + 242 + 122 = 1364, а «Итого» 1342: в письме доставка была с НДС.

        В режиме «цены с НДС» доставка тоже уже с НДС, поэтому её нельзя
        показывать рядом с «Сумма без НДС» и «НДС» — иначе клиент складывает
        три числа и получает на 22 ₽ больше итога.
        """
        quote = _quote("122.00", inclusive=True)
        text = build_reply_text(
            ParsedEmail(sender="a@b.ru", subject="Заявка", message_id="x", received_at="", text=""),
            quote,
            "x" * 32,
        )
        self.assertIn("Сумма с НДС по товарам: 1 220,00 ₽", text)
        self.assertIn("В том числе НДС: 242,00 ₽", text)
        self.assertIn("Доставка с НДС: 122,00 ₽", text)
        self.assertIn("Итого: 1 342,00 ₽", text)
        self.assertNotIn("Сумма без НДС", text)

    def test_net_delivery_reply_adds_up(self) -> None:
        quote = _quote("100.00", inclusive=False, price="100.00")
        text = build_reply_text(
            ParsedEmail(sender="a@b.ru", subject="Заявка", message_id="x", received_at="", text=""),
            quote,
            "x" * 32,
        )
        self.assertIn("Сумма без НДС: 1 000,00 ₽", text)
        self.assertIn("НДС: 242,00 ₽", text)
        self.assertIn("Доставка без НДС: 100,00 ₽", text)
        self.assertIn("Итого: 1 342,00 ₽", text)


if __name__ == "__main__":
    unittest.main()
