"""Цены в прайсе уже с НДС: НДС выделяется, а не начисляется второй раз."""

import support  # общий стенд

import os
import unittest
from decimal import Decimal

os.environ["KPBOT_API_KEY"] = "test-key"
os.environ["KPBOT_UI_PASSWORD"] = "test-pass"
os.environ["KPBOT_SESSION_SECRET"] = "test-secret"

from kpbot.config import Settings  # noqa: E402
from kpbot.domain import PriceItem  # noqa: E402
from kpbot.pipeline import Pipeline  # noqa: E402
from kpbot.store import Store  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
