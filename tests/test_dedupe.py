"""Обрубки от модели: строка без совпадения, повторяющая уже найденную позицию."""

import support  # общий стенд: изолированные данные, сеть запрещена

import shutil
import unittest

from kpbot.config import BASE_DIR, Settings
from kpbot.pipeline import Pipeline
from kpbot.store import Store

TMP = support.subdir("dedupe")


class DedupeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        shutil.rmtree(TMP, ignore_errors=True)
        TMP.mkdir(parents=True)
        cls.settings = Settings(
            data_dir=TMP,
            api_key="test-key",
            ui_password="test-pass",
            session_secret="test-secret",
            pdf_enabled=False,
            llm_enabled=False,
        )
        cls.store = Store(cls.settings.db_path)
        cls.pipeline = Pipeline(cls.store, cls.settings)
        cls.pipeline.ensure_price_list()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(TMP, ignore_errors=True)

    def test_fragment_of_matched_line_is_dropped(self) -> None:
        lines, _, _ = self.pipeline.analyze(
            "Прошу рассчитать:\n"
            "1. Шпаклевка финишная 25 кг — 10 меш\n"
            "2. Шпаклевка финишная — 10 меш\n"
            "3. Уголок монтажный 50х50 мм стальной — 100 шт\n"
        )
        texts = [line.raw_text for line in lines]
        self.assertEqual(len(lines), 2, texts)
        self.assertTrue(all(line.matched_sku for line in lines))

    def test_two_different_items_are_kept(self) -> None:
        lines, _, _ = self.pipeline.analyze(
            "1. Уголок монтажный 50х50 мм стальной — 10 шт\n"
            "2. Цемент М500 50 кг — 5 меш\n"
        )
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line.matched_sku for line in lines))

    def test_unknown_item_is_kept_for_human(self) -> None:
        lines, _, _ = self.pipeline.analyze("1. подрозетник скрытой установки — 10 шт\n")
        self.assertEqual(len(lines), 1)
        self.assertIsNone(lines[0].matched_sku)


if __name__ == "__main__":
    unittest.main()
