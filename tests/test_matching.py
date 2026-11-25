import support  # общий стенд: изолированные данные, сеть запрещена

import unittest
from decimal import Decimal

from kpbot.domain import LineStatus, PriceItem, RfqLine
from kpbot.extract import RuleExtractor, canonical_unit
from kpbot.matching import match_lines, normalize, similarity


def price_items() -> list[PriceItem]:
    return [
        PriceItem("KR-001", "Уголок монтажный 50х50 мм стальной", "шт", Decimal("34.50")),
        PriceItem("EL-001", "Кабель ВВГнг-LS 3х2.5", "м", Decimal("128.00")),
        PriceItem("MT-006", "Штукатурка гипсовая 30 кг", "меш", Decimal("465.00")),
        PriceItem("IN-004", "Саморез по металлу 3.5х25 мм", "уп", Decimal("185.00")),
    ]


class NormalizeTest(unittest.TestCase):
    def test_dimensions_unified(self) -> None:
        self.assertEqual(normalize("Уголок 50Х50"), normalize("уголок 50x50"))

    def test_yo_and_punctuation(self) -> None:
        self.assertEqual(normalize("Ёмкость, 10 л."), "емкость 10 л")


class SimilarityTest(unittest.TestCase):
    def test_exact_is_one(self) -> None:
        self.assertEqual(similarity("Кабель ВВГнг-LS 3х2.5", "кабель ввгнг ls 3x2.5"), 1.0)

    def test_close_match_is_high(self) -> None:
        score = similarity("уголок 50х50", "Уголок монтажный 50х50 мм стальной")
        self.assertGreater(score, 0.8)

    def test_word_forms_match(self) -> None:
        score = similarity("штукатурки гипсовой", "Штукатурка гипсовая 30 кг")
        self.assertGreater(score, 0.7)

    def test_unrelated_is_low(self) -> None:
        self.assertLess(similarity("цемент м500", "Унитаз-компакт с бачком"), 0.35)

    def test_different_size_is_penalised(self) -> None:
        right = similarity("саморез 4.2х75", "Саморез по дереву 4.2х75 мм")
        wrong = similarity("саморез 4.2х75", "Саморез по металлу 3.5х25 мм")
        self.assertGreater(right, wrong)


class MatchLinesTest(unittest.TestCase):
    def test_ok_when_confident(self) -> None:
        lines = [RfqLine(idx=1, raw_text="уголок 50х50", qty=Decimal("10"), unit="шт")]
        result = match_lines(lines, price_items())
        self.assertIs(result[0].status, LineStatus.OK)
        self.assertEqual(result[0].matched_sku, "KR-001")

    def test_unit_mismatch_goes_to_human(self) -> None:
        lines = [RfqLine(idx=1, raw_text="саморез по металлу 3.5х25", qty=Decimal("100"), unit="шт")]
        result = match_lines(lines, price_items())
        self.assertIs(result[0].status, LineStatus.AMBIGUOUS)
        self.assertIn("единица", result[0].note)

    def test_unknown_position_is_not_guessed(self) -> None:
        lines = [RfqLine(idx=1, raw_text="подрозетник 68 мм", qty=Decimal("20"), unit="шт")]
        result = match_lines(lines, price_items())
        self.assertIs(result[0].status, LineStatus.NO_MATCH)
        self.assertIsNone(result[0].matched_sku)

    def test_missing_qty_is_invalid(self) -> None:
        lines = [RfqLine(idx=1, raw_text="кабель ввг", qty=None, unit=None)]
        result = match_lines(lines, price_items())
        self.assertIs(result[0].status, LineStatus.INVALID)


class ExtractTest(unittest.TestCase):
    def test_numbered_list(self) -> None:
        text = "1. Уголок 50х50 — 200 шт\n2. Кабель ВВГнг 3х2.5 — 150 м"
        lines = RuleExtractor().extract(text)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0].qty, Decimal("200.000"))
        self.assertEqual(lines[0].unit, "шт")
        self.assertIn("Уголок", lines[0].raw_text)
        self.assertEqual(lines[1].unit, "м")

    def test_bullets_and_semicolons(self) -> None:
        text = "- уголок 50х50 – 20 шт;\n- кабель 3х2.5 – 5 м"
        self.assertEqual(len(RuleExtractor().extract(text)), 2)

    def test_single_line_comma_separated(self) -> None:
        text = "Нужно 5 листов гипсокартона 12.5, 10 уп саморез 3.5х25"
        lines = RuleExtractor().extract(text)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0].raw_text, "гипсокартона 12.5")

    def test_qty_after_name(self) -> None:
        lines = RuleExtractor().extract("Труба ППР 25 — 60 м")
        self.assertEqual(lines[0].qty, Decimal("60.000"))
        self.assertEqual(lines[0].raw_text, "Труба ППР 25")

    def test_line_without_qty_is_skipped(self) -> None:
        self.assertEqual(RuleExtractor().extract("Добрый день, посмотрите пожалуйста"), [])

    def test_duplicates_are_merged(self) -> None:
        text = "уголок 50х50 — 10 шт\nуголок 50х50 — 10 шт"
        self.assertEqual(len(RuleExtractor().extract(text)), 1)

    def test_canonical_units(self) -> None:
        self.assertEqual(canonical_unit("мешков"), "меш")
        self.assertEqual(canonical_unit("упаковок"), "уп")
        self.assertEqual(canonical_unit("листов"), "лист")


if __name__ == "__main__":
    unittest.main()
