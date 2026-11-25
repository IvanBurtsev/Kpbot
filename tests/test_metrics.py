"""Метрика времени, импорт прайса из 1С и лист для 1С в экспорте."""

import support  # общий стенд: изолированные данные, сеть запрещена

import shutil
import unittest
from decimal import Decimal

from kpbot.config import BASE_DIR, Settings
from kpbot.pipeline import Pipeline
from kpbot.pricelist import load_price_items
from kpbot.store import Store

TMP = support.subdir("metrics")


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

    def test_report_counts_rfq_and_auto_share(self) -> None:
        result = self.pipeline.process_text("200 шт уголок 50х50", sender="r@test.ru", subject="Заявка")
        self.assertTrue(result.rfq_id)
        report = self.store.report(30)
        self.assertEqual(report["rfqs"], 1)
        self.assertEqual(report["quotes"], 1)
        self.assertGreater(report["total_sum"], 0)
        self.assertGreater(report["auto_share"], 0)
        self.assertIsNotNone(report["median_seconds"])


class Export1cTest(unittest.TestCase):
    def test_exported_sheet_is_valid_for_1c(self) -> None:
        from kpbot.export import build_rfq_xlsx, check_1c_sheet

        rfq = {
            "id": "a" * 32,
            "sender": "a@b.ru",
            "subject": "Заявка",
            "created_at": "2026-11-25T00:00:00+00:00",
            "status": "draft_ready",
            "lines": [{"idx": 1, "raw_text": "уголок", "qty": "10", "unit": "шт",
                       "status": "ok", "score": 0.9, "matched_name": "Уголок 50х50"}],
        }
        quote = {
            "lines": [{"sku": "KR-001", "name": "Уголок 50х50", "unit": "шт",
                       "qty": "10", "price": "34.50", "amount": "345.00"}],
            "subtotal": "345.00", "vat": "69.00", "delivery": "0", "total": "414.00",
        }
        content = build_rfq_xlsx(rfq, quote)
        self.assertEqual(check_1c_sheet(content), [])

    def test_stock_and_vat_are_in_1c_sheet(self) -> None:
        from io import BytesIO

        from openpyxl import load_workbook

        from kpbot.export import build_rfq_xlsx

        rfq = {
            "id": "b" * 32,
            "sender": "a@b.ru",
            "subject": "Заявка",
            "created_at": "2026-11-25T00:00:00+00:00",
            "status": "draft_ready",
            "lines": [],
        }
        quote = {
            "lines": [{"sku": "KR-001", "name": "Уголок", "unit": "шт",
                       "qty": "10", "price": "34.50", "amount": "345.00"}],
            "subtotal": "345.00", "vat": "69.00", "delivery": "0", "total": "414.00",
            "vat_rate": "0.20",
        }
        stock = {"KR-001": {"warehouse": "Основной склад", "qty": "120"}}
        content = build_rfq_xlsx(rfq, quote, stock=stock)
        sheet = load_workbook(filename=BytesIO(content))["Для 1С"]
        header = [cell.value for cell in sheet[1]]
        row = [cell.value for cell in sheet[2]]
        self.assertEqual(header[-2:], ["Склад", "Ставка НДС"])
        self.assertEqual(row[-2:], ["Основной склад", "20%"])

    def test_unknown_vat_rate_is_blank(self) -> None:
        from io import BytesIO

        from openpyxl import load_workbook

        from kpbot.export import build_rfq_xlsx

        rfq = {"id": "c" * 32, "sender": "a@b.ru", "subject": "Заявка", "lines": []}
        quote = {
            "lines": [{"sku": "KR-001", "name": "Уголок", "unit": "шт",
                       "qty": "1", "price": "10", "amount": "10"}],
            "subtotal": "10", "vat": "0", "delivery": "0", "total": "10",
        }
        sheet = load_workbook(filename=BytesIO(build_rfq_xlsx(rfq, quote)))["Для 1С"]
        self.assertIn(sheet.cell(row=2, column=7).value, (None, ""))

    def test_duplicate_code_is_reported(self) -> None:
        from io import BytesIO

        from openpyxl import Workbook

        from kpbot.export import check_1c_sheet

        wb = Workbook()
        sheet = wb.active
        sheet.title = "Для 1С"
        sheet.append(("Код", "Наименование", "Количество", "Цена", "Сумма", "Склад", "Ставка НДС"))
        sheet.append(("KR-001", "Уголок", 1, 34.5, 34.5, "", "20%"))
        sheet.append(("KR-001", "Уголок ещё раз", 2, 34.5, 69.0, "", "20%"))
        wb.create_sheet("Номенклатура").append(("Наименование", "Артикул", "Единица измерения"))
        buffer = BytesIO()
        wb.save(buffer)
        problems = check_1c_sheet(buffer.getvalue())
        self.assertTrue(any("повтор кода" in problem for problem in problems), problems)

    def test_csv_has_stock_and_vat(self) -> None:
        from kpbot.export import build_rfq_csv

        rfq = {"id": "d" * 32, "lines": []}
        quote = {
            "lines": [{"sku": "KR-001", "name": "Уголок", "unit": "шт",
                       "qty": "2", "price": "34.50", "amount": "69.00"}],
            "subtotal": "69.00", "vat": "13.80", "delivery": "0", "total": "82.80",
            "vat_rate": "0.20",
        }
        stock = {"KR-001": {"warehouse": "Склад №2"}}
        TMP.mkdir(parents=True, exist_ok=True)
        files = build_rfq_csv(rfq, quote, TMP, stock=stock)
        order = next(path for path in files if "order" in path.name)
        text = order.read_text(encoding="utf-8-sig")
        self.assertIn("Склад;Ставка НДС", text)
        self.assertIn("Склад №2", text)
        self.assertIn("20%", text)

    def test_broken_file_is_reported(self) -> None:
        from io import BytesIO

        from openpyxl import Workbook

        from kpbot.export import check_1c_sheet

        wb = Workbook()
        sheet = wb.active
        sheet.title = "Для 1С"
        sheet.append(("Код", "Наименование", "Количество", "Цена", "Сумма"))
        sheet.append(("KR-001", "Уголок", "много", 34.5, 345.0))
        buffer = BytesIO()
        wb.save(buffer)
        problems = check_1c_sheet(buffer.getvalue())
        self.assertTrue(any("количество не число" in p for p in problems))



if __name__ == "__main__":
    unittest.main()
