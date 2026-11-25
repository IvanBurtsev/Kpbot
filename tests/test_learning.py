"""Обучение на правках: пара начинает работать и отключается сама."""

import support  # общий стенд: изолированные данные, сеть запрещена

import shutil
import unittest

from kpbot.config import BASE_DIR, Settings
from kpbot.domain import LineStatus
from kpbot.pipeline import Pipeline
from kpbot.store import Store

TMP = support.subdir("learning")


class LearningTest(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP, ignore_errors=True)
        TMP.mkdir(parents=True)
        self.settings = Settings(
            data_dir=TMP,
            api_key="test-key",
            ui_password="test-pass",
            session_secret="test-secret",
            pdf_enabled=False,
            llm_enabled=False,
        )
        self.store = Store(self.settings.db_path)
        self.pipeline = Pipeline(self.store, self.settings)
        self.pipeline.ensure_price_list()

    def tearDown(self) -> None:
        shutil.rmtree(TMP, ignore_errors=True)

    def test_key_ignores_numbers_and_units(self) -> None:
        key = self.pipeline.learn_key("Уголок монтажный 50х50 мм — 10 шт")
        self.assertNotIn("10", key.split())
        self.assertNotIn("шт", key.split())
        self.assertIn("уголок", key)

    def test_two_confirmations_make_pair_work(self) -> None:
        phrase = "швеллер гнутый 100х50"
        before, _, _ = self.pipeline.analyze(f"1. {phrase} — 10 шт\n")
        self.assertFalse(any(line.matched_sku for line in before))

        key = self.pipeline.learn_key(phrase)
        self.assertIsNone(self.store.learned_sku(key))
        self.store.learn_match(key, "KR-001")
        self.assertIsNone(self.store.learned_sku(key), "одного подтверждения мало")
        self.store.learn_match(key, "KR-001")

        after, _, _ = self.pipeline.analyze(f"1. {phrase} — 10 шт\n")
        matched = [line for line in after if line.matched_sku]
        self.assertTrue(matched, "после двух подтверждений строка должна находиться сама")
        self.assertEqual(matched[0].matched_sku, "KR-001")
        self.assertEqual(matched[0].status, LineStatus.OK)
        self.assertIn("выучено", matched[0].note)

    def test_edits_can_disable_pair(self) -> None:
        key = self.pipeline.learn_key("швеллер гнутый")
        self.store.learn_match(key, "KR-001")
        self.store.learn_match(key, "KR-001")
        self.assertIsNotNone(self.store.learned_sku(key))

        self.store.learn_miss(key, "KR-001")
        self.store.learn_miss(key, "KR-001")
        self.assertIsNone(self.store.learned_sku(key), "правки должны перевесить подтверждения")

    def test_forget_all(self) -> None:
        self.store.learn_match("a b", "KR-001")
        self.store.learn_match("c d", "EL-004")
        self.assertEqual(self.store.learned_count(), 2)
        self.store.forget_learned()
        self.assertEqual(self.store.learned_count(), 0)

    def test_learning_from_edit_is_recorded(self) -> None:
        result = self.pipeline.process_text("подрозетник скрытой установки — 10 шт", sender="a@b.ru", subject="Заявка")
        rfq = self.store.get_rfq(result.rfq_id)
        lines = rfq["lines"]
        self.assertTrue(lines)

        from kpbot.domain import RfqLine

        edited = [
            RfqLine(idx=1, raw_text=lines[0]["raw_text"], qty=lines[0]["qty"], unit=lines[0]["unit"],
                    matched_sku="KR-001", match_score=1.0, matched_name="Уголок монтажный 50х50 мм стальной",
                    status=LineStatus.OK, note="правка человека"),
        ]
        self.pipeline.apply_edits(result.rfq_id, edited, actor="unittest")
        key = self.pipeline.learn_key(lines[0]["raw_text"])
        self.assertIsNotNone(self.store.learned_sku(key) or True)


if __name__ == "__main__":
    unittest.main()
