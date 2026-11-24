"""Метрика времени, импорт прайса из 1С и лист для 1С в экспорте."""

import shutil
import unittest
from decimal import Decimal

from kpbot.config import BASE_DIR, Settings
from kpbot.pipeline import Pipeline
from kpbot.pricelist import load_price_items
from kpbot.store import Store

TMP = BASE_DIR / ".tmp" / "metrics"


class PriceListHeadersTest(unittest.TestCase):
    def setUp(self) -> None:
        TMP.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TMP, ignore_errors=True)

    def test_1c_headers_are_recognised(self) -> None:
        path = TMP / "1c.csv"
        path.write_text(
            "Код;Номенклатура;Единица измерения;Цена\n"
            "KR-001;Уголок 50х50;шт;34,50\n"
            "EL-001;Кабель 3х2.5;м;128\n",
            encoding="utf-8-sig",
        )
        items = load_price_items(path)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].sku, "KR-001")
        self.assertEqual(items[1].price, Decimal("128"))

    def test_our_template_still_works(self) -> None:
        path = TMP / "own.csv"
        path.write_text(
            "SKU;Наименование;Ед.;Цена;Валюта\nMT-002;Цемент М500;меш;520.00;RUB\n",
            encoding="utf-8-sig",
        )
        items = load_price_items(path)
        self.assertEqual(items[0].unit, "меш")

    def test_row_without_code_gets_generated_sku(self) -> None:
        path = TMP / "nocode.csv"
        path.write_text("Наименование;Цена\nГрунтовка;580\n", encoding="utf-8-sig")
        items = load_price_items(path)
        self.assertTrue(items[0].sku.startswith("AUTO-"))


class TimingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.data_dir = TMP / "db"
        shutil.rmtree(self.data_dir, ignore_errors=True)
        self.data_dir.mkdir(parents=True)
        self.settings = Settings(
            data_dir=self.data_dir,
            api_key="test-key",
            ui_password="test-pass",
            session_secret="test-secret",
            pdf_enabled=False,
            llm_enabled=False,
        )
        self.store = Store(self.settings.db_path)
        self.pipeline = Pipeline(self.store, self.settings)

    def tearDown(self) -> None:
        shutil.rmtree(TMP, ignore_errors=True)

    def test_time_to_quote_is_measured(self) -> None:
        result = self.pipeline.process_text("уголок 50х50 — 10 шт", sender="a@b.ru", subject="Заявка")
        timing = self.store.rfq_timing(result.rfq_id)
        self.assertIsNotNone(timing["seconds"])
        self.assertGreaterEqual(timing["seconds"], 0)

        stats = self.store.timing_stats()
        self.assertEqual(stats["count"], 1)
        self.assertIsNotNone(stats["median_seconds"])


if __name__ == "__main__":
    unittest.main()