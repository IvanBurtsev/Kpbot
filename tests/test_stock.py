"""Остатки из 1С: загрузка файла и показ нехватки в заявке."""

import support  # общий стенд: изолированные данные, сеть запрещена

import os
import unittest
from pathlib import Path

_TMP = support.subdir("stock")
os.environ.setdefault("KPBOT_API_KEY", "test-key")
os.environ.setdefault("KPBOT_UI_PASSWORD", "test-pass")
os.environ.setdefault("KPBOT_SESSION_SECRET", "test-secret")
os.environ.setdefault("KPBOT_PDF_ENABLED", "false")

from fastapi.testclient import TestClient  # noqa: E402

from kpbot.pricelist import load_stock_items  # noqa: E402
from kpbot.web import app, pipeline, settings, store  # noqa: E402


def csrf(client: TestClient, url: str = "/ui/prices") -> str:
    page = client.get(url)
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


class StockFileTest(unittest.TestCase):
    def test_1c_export_is_parsed(self) -> None:
        path = _TMP / "stock.csv"
        path.write_text(
            "Артикул;Номенклатура;Склад;Остаток\n"
            "KR-001;Уголок 50х50;Основной;120\n"
            "EL-004;Провод ПуГВ 1х4;Основной;40,5\n",
            encoding="utf-8-sig",
        )
        items = load_stock_items(path)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0][0], "KR-001")
        self.assertEqual(items[0][1], "120")
        self.assertEqual(items[1][1], "40.5")

    def test_report_without_sku_column_uses_name(self) -> None:
        path = _TMP / "stock-nosku.csv"
        path.write_text("Номенклатура;Количество\nУголок 50х50;15\n", encoding="utf-8-sig")
        items = load_stock_items(path)
        self.assertEqual(items[0][0], "Уголок 50х50")

    def test_file_without_quantity_is_rejected(self) -> None:
        from kpbot.domain import ValidationError

        path = _TMP / "bad.csv"
        path.write_text("Артикул;Цена\nA;10\n", encoding="utf-8-sig")
        with self.assertRaises(ValidationError):
            load_stock_items(path)


class StockUiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/ui/login", data={"password": settings.ui_password})
        with store._connect() as conn, conn:
            conn.execute("DELETE FROM stock")

    def test_upload_updates_stock(self) -> None:
        token = csrf(self.client)
        payload = "Артикул;Остаток\nKR-001;120\n"
        response = self.client.post(
            "/ui/prices/stock",
            data={"csrf_token": token},
            files={"file": ("stock.csv", payload.encode("utf-8"), "text/csv")},
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(store.stock_count(), 1)
        self.assertEqual(store.stock_map()["KR-001"]["qty"], "120")

    def test_rfq_shows_stock_and_warns_when_short(self) -> None:
        store.replace_stock([("KR-001", "5", "шт", "Основной")])
        rfq = pipeline.process_text("уголок 50х50 — 10 шт", sender="a@b.ru", subject="Заявка")
        page = self.client.get(f"/ui/rfq/{rfq.rfq_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Остаток", page.text)
        # просят 10, на складе 5 — должно быть предупреждение
        self.assertIn("warn", page.text)


if __name__ == "__main__":
    unittest.main()
