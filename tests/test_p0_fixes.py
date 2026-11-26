"""Регрессия на P0-дефекты аудита 27.11.2026.

Каждый тест — про конкретную поломку, найденную на реальных данных:
размер перед количеством («3х2.5 150 м» → 5150), тысячи через точку,
обрезка пула матчинга, удвоение в отчёте, плавающий номер КП, формула из
письма в Excel, одобрение вместо отправки.
"""

import support  # общий стенд: изолированные данные, сеть запрещена

import io
import os
import unittest
from decimal import Decimal

os.environ["KPBOT_API_KEY"] = "test-key"
os.environ["KPBOT_UI_PASSWORD"] = "test-pass"
os.environ["KPBOT_SESSION_SECRET"] = "test-secret"

from kpbot.config import settings  # noqa: E402
from kpbot.domain import PriceItem, RfqLine, LineStatus  # noqa: E402
from kpbot.export import build_rfq_xlsx  # noqa: E402
from kpbot.extract import RuleExtractor  # noqa: E402
from kpbot.matching import PriceIndex  # noqa: E402
from kpbot.pipeline import Pipeline  # noqa: E402
from kpbot.store import Store  # noqa: E402


class ExtractorQuantityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.extractor = RuleExtractor()

    def _one(self, text: str):
        lines = self.extractor.extract(text)
        self.assertEqual(len(lines), 1, f"ожидалась одна строка, получено {lines}")
        return lines[0]

    def test_decimal_dimension_does_not_eat_quantity(self) -> None:
        # «кабель 3х2.5 150 м» раньше склеивался в «3х2.5150» → 5150 м
        line = self._one("кабель 3х2.5 150 м")
        self.assertEqual(str(line.qty), "150.000")
        self.assertEqual(line.unit, "м")
        self.assertIn("3х2.5", line.raw_text)

    def test_second_decimal_dimension(self) -> None:
        line = self._one("труба 25.4 100 м")
        self.assertEqual(str(line.qty), "100.000")
        self.assertIn("25.4", line.raw_text)

    def test_dot_thousands(self) -> None:
        # «10.000 шт» — это десять тысяч, а не десять
        line = self._one("уголок 50х50 — 10.000 шт")
        self.assertEqual(str(line.qty), "10000.000")

    def test_dot_thousands_before_unit(self) -> None:
        line = self._one("кабель 3х2.5 — 1.500 м")
        self.assertEqual(str(line.qty), "1500.000")

    def test_space_thousands_multiple_groups(self) -> None:
        line = self._one("труба — 1 500 000 м")
        self.assertEqual(str(line.qty), "1500000.000")


class MatchingExactTest(unittest.TestCase):
    def test_exact_name_wins_on_large_catalog(self) -> None:
        # 300 однотипных позиций: точное совпадение не должно проигрывать
        # «первой попавшейся» из-за обрезки пула кандидатов
        items = [
            PriceItem(
                sku=f"Z-{index:04d}",
                name=f"Кабель ВВГнг-LS 3х2.5 вариант {index}",
                unit="м",
                price=Decimal("100"),
            )
            for index in range(300)
        ]
        items.append(PriceItem(sku="RIGHT", name="Кабель ВВГнг-LS 3х2.5", unit="м", price=Decimal("1")))
        index = PriceIndex(items)
        item, score = index.best("кабель ввгнг ls 3х2.5")
        self.assertIsNotNone(item)
        self.assertEqual(item.sku, "RIGHT")
        self.assertGreaterEqual(score, 0.99)


class PriceSearchTest(unittest.TestCase):
    def test_top_returns_few_candidates_not_whole_catalog(self) -> None:
        items = [
            PriceItem(
                sku=f"S-{index:04d}",
                name=f"Уголок стальной {index % 7 + 40}х{index % 5 + 40} мм",
                unit="шт",
                price=Decimal("100"),
            )
            for index in range(3000)
        ]
        items.append(PriceItem(sku="EXACT", name="Уголок 50х50", unit="шт", price=Decimal("1")))
        index = PriceIndex(items)
        top = index.top("уголок 50х50", 5)
        self.assertLessEqual(len(top), 5)
        self.assertEqual(top[0][0].sku, "EXACT")

    def test_top_ignores_meaningless_query(self) -> None:
        index = PriceIndex([PriceItem(sku="A", name="Уголок 50х50", unit="шт", price=Decimal("1"))])
        self.assertEqual(index.top("12", 5), [])


class QuoteNumberTest(unittest.TestCase):
    def setUp(self) -> None:
        # своя база на тест: соседние тесты в модуле не должны влиять на цифры
        self.store = Store(support.ROOT / f"number-{id(self)}.sqlite3")
        self.pipeline = Pipeline(self.store, settings)

    def test_number_is_stable_after_edit(self) -> None:
        result = self.pipeline.process_text(
            "уголок 50х50 — 10 шт", sender="a@b.ru", subject="Заявка", message_id="p0-number"
        )
        first = self.store.get_quote(result.rfq_id)["number"]
        lines = [
            RfqLine(
                idx=row["idx"],
                raw_text=row["raw_text"],
                qty=Decimal(row["qty"]) if row["qty"] else None,
                unit=row["unit"],
                matched_sku=row["matched_sku"],
                matched_name=row["matched_name"],
                status=LineStatus(row["status"]),
            )
            for row in self.store.get_rfq(result.rfq_id)["lines"]
        ]
        lines[0].qty = Decimal("20")
        self.pipeline.apply_edits(result.rfq_id, lines, actor="test")
        self.assertEqual(self.store.get_quote(result.rfq_id)["number"], first)


class ReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(support.ROOT / f"report-{id(self)}.sqlite3")
        self.pipeline = Pipeline(self.store, settings)

    def test_report_counts_latest_quote_only(self) -> None:
        result = self.pipeline.process_text(
            "уголок 50х50 — 10 шт", sender="a@b.ru", subject="Заявка", message_id="p0-report"
        )
        before = self.store.report(30)
        lines = [
            RfqLine(
                idx=row["idx"],
                raw_text=row["raw_text"],
                qty=Decimal(row["qty"]) if row["qty"] else None,
                unit=row["unit"],
                matched_sku=row["matched_sku"],
                matched_name=row["matched_name"],
                status=LineStatus(row["status"]),
            )
            for row in self.store.get_rfq(result.rfq_id)["lines"]
        ]
        lines[0].qty = Decimal("20")
        self.pipeline.apply_edits(result.rfq_id, lines, actor="test")
        after = self.store.report(30)
        latest = self.store.get_quote(result.rfq_id)
        self.assertEqual(after["rfqs"], 1)
        self.assertEqual(after["quotes"], 1, "правка не должна считаться вторым КП")
        self.assertAlmostEqual(after["total_sum"], float(latest["total"]), places=2)
        self.assertGreater(after["total_sum"], before["total_sum"])


class ExportInjectionTest(unittest.TestCase):
    def test_formula_from_letter_is_text(self) -> None:
        rfq = {
            "id": "a" * 32,
            "sender": "=cmd|' /C calc'!A0",
            "subject": '=HYPERLINK("http://evil.example","нажми")',
            "created_at": "2026-11-27T00:00:00",
            "status": "draft_ready",
            "lines": [
                {
                    "idx": 1,
                    "raw_text": "=1+1 — 10 шт",
                    "qty": "10",
                    "unit": "шт",
                    "status": "ok",
                    "score": 1.0,
                    "matched_name": "=SUM(A1:A2)",
                }
            ],
        }
        quote = {
            "lines": [
                {
                    "sku": "=A1",
                    "name": "=SUM(1,2)",
                    "unit": "шт",
                    "qty": "10",
                    "price": "1",
                    "amount": "10",
                }
            ],
            "subtotal": "10",
            "vat": "2.2",
            "delivery": "0",
            "total": "12.2",
            "vat_rate": "0.22",
        }
        content = build_rfq_xlsx(rfq, quote)
        from openpyxl import load_workbook

        book = load_workbook(io.BytesIO(content))
        calc = book["Расчёт"]
        self.assertNotEqual(calc["B3"].data_type, "f", "номер заявки не должен быть формулой")
        self.assertNotEqual(calc["B4"].data_type, "f", "отправитель не должен быть формулой")
        self.assertNotEqual(calc["B5"].data_type, "f", "тема не должна быть формулой")
        self.assertNotEqual(calc["B10"].data_type, "f", "артикул не должен быть формулой")
        self.assertNotEqual(calc["C10"].data_type, "f", "наименование не должно быть формулой")
        letter = book["Строки из письма"]
        self.assertNotEqual(letter["B2"].data_type, "f", "строка письма не должна быть формулой")
        self.assertNotEqual(letter["G2"].data_type, "f")
        one_c = book["Для 1С"]
        self.assertNotEqual(one_c["A2"].data_type, "f")
        self.assertNotEqual(one_c["B2"].data_type, "f")


if __name__ == "__main__":
    unittest.main()
