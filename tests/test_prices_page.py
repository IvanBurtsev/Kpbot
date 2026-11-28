"""Страница «Прайс»: загрузка, валюта, ставки, поиск, остатки, права.

Регрессии аудита 28.11.2026:

* загрузка прайса с колонкой «Цена с НДС» смотрела на `.env`, а не на настройку
  клиента: файл отклонялся советом править конфиг сервера;
* дата обновления и цены показывались как `2026-11-28T19:04:32` и `34.50 ₽`;
* ставка НДС позиции не была видна — при смешанных 22/10 это скрывало причину
  разной суммы;
* при поиске печаталось «показаны первые 1 из 55» — это не усечение, а результат;
* слишком большой файл отдавал английское «payload too large» без подсказки;
* лимит в тексте был жёстко «2 МБ», хотя он настраивается;
* замена всего прайса не спрашивала подтверждения.
"""

import support  # общий стенд: изолированные данные, сеть запрещена

import os
import re
import unittest

os.environ.setdefault("KPBOT_API_KEY", "test-key")
os.environ.setdefault("KPBOT_UI_PASSWORD", "test-pass")
os.environ.setdefault("KPBOT_SESSION_SECRET", "test-secret")
os.environ.setdefault("KPBOT_PDF_ENABLED", "false")

from fastapi.testclient import TestClient  # noqa: E402

from kpbot.web import app, settings, store  # noqa: E402


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return match.group(1) if match else ""


def _flash(response) -> str:
    from urllib.parse import unquote

    match = re.search(r"flash=([^&]+)", response.headers.get("location", ""))
    return unquote(match.group(1)) if match else ""


class PricesPageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/ui/login", data={"password": "test-pass"})
        self.saved_items = store.price_items()
        self.saved_settings = store.get_settings()
        self.token = _csrf(self.client.get("/ui/prices").text)

    def tearDown(self) -> None:
        store.replace_price_items(self.saved_items)
        store.set_settings(
            {key: self.saved_settings.get(key, "") for key in ("prices_include_vat", "vat_rate")}
        )

    def _upload(self, name: str, content: bytes, url: str = "/ui/prices/upload"):
        return self.client.post(
            url,
            data={"csrf_token": self.token},
            files={"file": (name, content, "application/octet-stream")},
            follow_redirects=False,
        )

    # ---------- деньги и ставки ----------

    def test_vat_inclusive_upload_follows_tenant_setting(self) -> None:
        store.set_settings({"prices_include_vat": "1"})
        response = self._upload(
            "vat.csv", "SKU;Наименование;Ед.;Цена с НДС\nD1;Розетка;шт;315\n".encode()
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual([item.sku for item in store.price_items()], ["D1"])
        self.assertNotIn("KPBOT_PRICES_INCLUDE_VAT", _flash(response))

    def test_vat_inclusive_upload_refused_without_setting(self) -> None:
        store.set_settings({"prices_include_vat": ""})
        response = self._upload(
            "vat.csv", "SKU;Наименование;Ед.;Цена с НДС\nD1;Розетка;шт;315\n".encode()
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("НДС", _flash(response))

    def test_illegal_vat_rate_in_price_is_rejected(self) -> None:
        response = self._upload(
            "vat20.csv", "SKU;Наименование;Ед.;Цена;Ставка НДС\nV1;Товар;шт;100;20\n".encode()
        )
        self.assertIn("не применяется", _flash(response))

    def test_legal_vat_rate_is_kept(self) -> None:
        self._upload(
            "vat10.csv", "SKU;Наименование;Ед.;Цена;Ставка НДС\nV2;Товар;шт;100;10\n".encode()
        )
        item = store.price_items()[0]
        self.assertEqual(item.vat_rate, __import__("decimal").Decimal("0.1"))

    def test_foreign_currency_is_rejected(self) -> None:
        response = self._upload(
            "usd.csv", "SKU;Наименование;Ед.;Цена;Валюта\nX1;Товар;шт;10;USD\n".encode()
        )
        self.assertIn("валюте", _flash(response))

    def test_duplicate_sku_keeps_last_row(self) -> None:
        self._upload(
            "dup.csv", "SKU;Наименование;Ед.;Цена\nD;Первый;шт;10\nD;Второй;шт;20\n".encode()
        )
        self.assertEqual([item.name for item in store.price_items()], ["Второй"])

    # ---------- разбор файла ----------

    def test_header_without_price_is_explained(self) -> None:
        response = self._upload(
            "noprice.csv", "Код;Наименование;Количество\nA;Товар;5\n".encode()
        )
        self.assertIn("шапка", _flash(response))

    def test_empty_price_is_refused(self) -> None:
        response = self._upload("empty.csv", "SKU;Наименование;Ед.;Цена\n".encode())
        self.assertIn("пуст", _flash(response))

    def test_wrong_extension_is_refused(self) -> None:
        response = self._upload("price.txt", b"x")
        self.assertIn("xlsx", _flash(response))

    def test_too_big_file_gets_russian_message(self) -> None:
        big = "SKU;Наименование;Ед.;Цена\n".encode() + b"Z;T;u;1\n" * 400_000
        response = self._upload("big.csv", big)
        self.assertEqual(response.status_code, 413)
        self.assertIn("больш", response.text.lower())

    def test_csv_without_header_is_read_positionally(self) -> None:
        self._upload("nohead.csv", "N1;Молоток;шт;500\nN2;Гвоздь;кг;250\n".encode())
        self.assertEqual(store.price_count(), 2)

    # ---------- страница ----------

    def test_page_shows_human_date_money_and_vat(self) -> None:
        self._upload(
            "one.csv", "SKU;Наименование;Ед.;Цена;Ставка НДС\nA1;Уголок;шт;34,50;22\n".encode()
        )
        page = self.client.get("/ui/prices").text
        self.assertIn("34,50 ₽", page)
        self.assertIn(">НДС</th>", page.replace("\n", ""))
        self.assertIn("22%", page)
        self.assertNotRegex(page, r"обновлён \d{4}-\d\d-\d\dT")

    def test_search_shows_found_count_not_false_truncation(self) -> None:
        self._upload(
            "two.csv", "SKU;Наименование;Ед.;Цена\nA1;Уголок;шт;1\nB1;Кабель;м;2\n".encode()
        )
        page = self.client.get("/ui/prices?q=уголок").text
        self.assertIn("найдено", page)
        self.assertNotIn("показаны первые", page)

    def test_pagination_splits_long_price_list(self) -> None:
        rows = "".join(f"K{i:04d};Товар {i};шт;10\n" for i in range(305))
        self._upload("many.csv", f"SKU;Наименование;Ед.;Цена\n{rows}".encode())
        first = self.client.get("/ui/prices").text
        self.assertIn("страница 1 из 4", first)
        self.assertIn("K0000", first)
        self.assertNotIn("K0150", first)
        second = self.client.get("/ui/prices?page=2").text
        self.assertIn("страница 2 из 4", second)
        self.assertIn("K0100", second)
        self.assertIn("page=3", second)

    def test_export_keeps_vat_rates(self) -> None:
        self._upload(
            "rates.csv",
            "SKU;Наименование;Ед.;Цена;Ставка НДС\nA1;Уголок;шт;34,50;22\nB1;Кабель;м;128;10\n".encode(),
        )
        response = self.client.get("/ui/prices/export.xlsx")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"PK"))
        path = support.subdir("prices_export") / "export.xlsx"
        path.write_bytes(response.content)
        from kpbot.pricelist import load_price_items

        items = {item.sku: item for item in load_price_items(path)}
        self.assertEqual(items["A1"].vat_rate, __import__("decimal").Decimal("0.22"))
        self.assertEqual(items["B1"].vat_rate, __import__("decimal").Decimal("0.1"))

    def test_export_requires_login(self) -> None:
        fresh = TestClient(app, follow_redirects=False)
        self.assertEqual(fresh.get("/ui/prices/export.xlsx").status_code, 401)

    def test_stock_table_is_shown(self) -> None:
        self._upload(
            "stock2.csv", "Артикул;Остаток;Единица;Склад\nA1;10;шт;Основной\n".encode(),
            url="/ui/prices/stock",
        )
        page = self.client.get("/ui/prices").text
        self.assertIn("A1", page)
        self.assertIn("Основной", page)
        self.assertIn(">Остаток</th>", page.replace("\n", ""))

    def test_search_ignores_yo_and_case(self) -> None:
        self._upload(
            "yo.csv", "SKU;Наименование;Ед.;Цена\nA1;Ёлка новогодняя;шт;100\n".encode()
        )
        self.assertIn("Ёлка", self.client.get("/ui/prices?q=елка").text)
        self.assertIn("Ёлка", self.client.get("/ui/prices?q=ЁЛКА").text)

    def test_upload_form_asks_confirmation(self) -> None:
        page = self.client.get("/ui/prices").text
        self.assertIn("data-confirm", page)
        self.assertIn("полностью заменён", page)

    def test_csrf_is_required(self) -> None:
        response = self.client.post(
            "/ui/prices/upload", data={}, files={"file": ("a.csv", b"x", "text/csv")}
        )
        self.assertEqual(response.status_code, 403)

    # ---------- остатки ----------

    def test_stock_upload_and_bad_stock_file(self) -> None:
        good = self._upload(
            "stock.csv", "Артикул;Остаток;Единица\nA1;10;шт\n".encode(), url="/ui/prices/stock"
        )
        self.assertEqual(good.status_code, 303)
        self.assertGreaterEqual(store.stock_count(), 1)
        bad = self._upload("bad.csv", "Артикул;Единица\nA1;шт\n".encode(), url="/ui/prices/stock")
        self.assertIn("количеств", _flash(bad).lower())


if __name__ == "__main__":
    unittest.main()