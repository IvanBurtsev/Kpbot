"""Регрессии по итогам разбора: запятая перед количеством, HTML-таблицы, срок КП, схема БД."""

import support  # общий стенд: изолированные данные, сеть запрещена

import shutil
import unittest

from kpbot.extract import RuleExtractor
from kpbot.mailparse import parse_eml
from kpbot.pipeline import build_reply_text_from_store
from kpbot.pricelist import default_price_items
from kpbot.store import Store

TMP = support.subdir("regressions")


def _extract(text: str):
    return RuleExtractor().extract(text)


def _html_email(html: str) -> bytes:
    return (
        "From: Ivanov <ivanov@example.ru>\r\n"
        "Subject: Zayavka\r\n"
        "Message-ID: <html-table@example.ru>\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        "Content-Transfer-Encoding: 8bit\r\n\r\n" + html
    ).encode("utf-8")


class CommaBeforeQuantityTest(unittest.TestCase):
    """«уголок 50х50, 200 шт» — одна позиция: запятая отделяет количество, а не позицию."""

    def test_comma_before_quantity_is_one_line(self) -> None:
        lines = _extract("уголок 50х50, 200 шт")
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].raw_text, "уголок 50х50")
        self.assertEqual(str(lines[0].qty), "200.000")
        self.assertEqual(lines[0].unit, "шт")

    def test_comma_before_dimension_quantity(self) -> None:
        lines = _extract("Профиль 20х40, 3 м")
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].raw_text, "Профиль 20х40")
        self.assertEqual(str(lines[0].qty), "3.000")
        self.assertEqual(lines[0].unit, "м")

    def test_real_list_of_two_items_still_splits(self) -> None:
        lines = _extract("уголок 50х50, 200 шт, саморез 4.2х75, 5 уп")
        self.assertEqual(
            [(line.raw_text, str(line.qty), line.unit) for line in lines],
            [("уголок 50х50", "200.000", "шт"), ("саморез 4.2х75", "5.000", "уп")],
        )

    def test_quantity_first_list_still_splits(self) -> None:
        lines = _extract("200 шт уголок 50х50, 300 шт труба 20")
        self.assertEqual([str(line.qty) for line in lines], ["200.000", "300.000"])


class HtmlTableTest(unittest.TestCase):
    def test_table_row_becomes_one_line(self) -> None:
        parsed = parse_eml(
            _html_email(
                "<html><body><p>Просим посчитать:</p><table>"
                "<tr><td>Наименование</td><td>Кол-во</td><td>Ед.</td></tr>"
                "<tr><td>Уголок 50х50</td><td>200</td><td>шт</td></tr>"
                "<tr><td>Саморез 4.2х75</td><td>5</td><td>уп</td></tr>"
                "</table></body></html>"
            )
        )
        lines = _extract(parsed.text)
        self.assertEqual(
            [(line.raw_text, str(line.qty), line.unit) for line in lines],
            [("Уголок 50х50", "200.000", "шт"), ("Саморез 4.2х75", "5.000", "уп")],
        )

    def test_plain_text_email_is_untouched(self) -> None:
        raw = (
            "From: a@b.ru\r\nSubject: Zayavka\r\nMessage-ID: <plain@example.ru>\r\n"
            "MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            "уголок 50х50 — 10 шт"
        ).encode("utf-8")
        parsed = parse_eml(raw)
        lines = _extract(parsed.text)
        self.assertEqual(len(lines), 1)


class QuoteValidityTest(unittest.TestCase):
    def _text(self, days: int) -> str:
        quote = {"lines": [1], "unmatched": 0, "subtotal": "100", "vat": "20", "total": "120"}
        return build_reply_text_from_store({}, quote, "rfq", None, valid_days=days)

    def test_configured_days_are_used(self) -> None:
        self.assertIn("1 календарный день", self._text(1))
        self.assertIn("2 календарных дня", self._text(2))
        self.assertIn("14 календарных дней", self._text(14))


class SchemaReinitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.data_dir = TMP / "db"
        shutil.rmtree(self.data_dir, ignore_errors=True)
        self.data_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def test_schema_is_recreated_after_file_removal(self) -> None:
        db_path = self.data_dir / "kpbot.sqlite3"
        store = Store(db_path)
        store.replace_price_items(default_price_items())
        self.assertEqual(store.price_count(), 55)

        db_path.unlink()
        fresh = Store(db_path)
        self.assertEqual(fresh.price_count(), 0)
        # и таблицы снова на месте
        self.assertEqual(fresh.stats()["rfqs"], 0)


if __name__ == "__main__":
    unittest.main()
