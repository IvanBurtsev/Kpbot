"""Клиент к 1С: разбор ответов OData и поведение при ошибках.

Сеть не трогаем: подменяем транспорт httpx на заранее заготовленные ответы 1С.
Форматы ответов взяты из реального OData 1С (Balance регистра накопления,
$metadata, создание документа).
"""

import support  # общий стенд: изолированные данные, сеть запрещена

import json
import unittest
from decimal import Decimal

import httpx

from kpbot.one_c import OneCClient, OneCError, parse_balance

METADATA = '<?xml version="1.0"?><edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx"><edmx:DataServices/></edmx:Edmx>'

BALANCE = {
    "odata.metadata": "http://1c/odata/$metadata#AccumulationRegister_ТоварыНаСкладах",
    "value": [
        {
            "RecordType": "Balance",
            "LineNumber": 1,
            "Склад": "Основной склад",
            "Склад_Key": "11111111-1111-1111-1111-111111111111",
            "Номенклатура": "Уголок монтажный 50х50 мм стальной",
            "Номенклатура_Key": "22222222-2222-2222-2222-222222222222",
            "Артикул": "KR-001",
            "КоличествоОстаток": 120,
        },
        {
            "RecordType": "Balance",
            "LineNumber": 2,
            "Склад": "Основной склад",
            "Номенклатура": "Кабель ВВГнг-LS 3х2.5",
            "Артикул": "EL-001",
            "КоличествоОстаток": "40.5",
        },
    ],
}


def client_with(handler) -> OneCClient:
    """Клиент с подменённым транспортом: реальный код, поддельная сеть."""
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, timeout=5)
    return OneCClient("https://1c.example/base", "user", "pass", http=http)


class BalanceParsingTest(unittest.TestCase):
    def test_fields_found_by_meaning(self) -> None:
        rows = parse_balance(BALANCE)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "KR-001")
        self.assertEqual(Decimal(rows[0][1]), Decimal("120"))
        self.assertEqual(rows[1][3], "Основной склад")

    def test_without_sku_uses_name(self) -> None:
        payload = {
            "value": [
                {"Номенклатура": "Цемент М500", "КоличествоОстаток": 15},
            ]
        }
        rows = parse_balance(payload)
        self.assertEqual(rows[0][0], "Цемент М500")

    def test_rows_without_quantity_are_skipped(self) -> None:
        payload = {"value": [{"Номенклатура": "Уголок"}, {"Номенклатура": "Кабель", "КоличествоОстаток": 3}]}
        rows = parse_balance(payload)
        self.assertEqual(len(rows), 1)

    def test_empty_payload(self) -> None:
        self.assertEqual(parse_balance({}), [])
        self.assertEqual(parse_balance({"value": []}), [])


class ClientBehaviourTest(unittest.TestCase):
    def test_check_reads_metadata(self) -> None:
        client = client_with(lambda request: httpx.Response(200, text=METADATA))
        self.assertIn("доступ есть", client.check())


    def test_stock_returns_rows(self) -> None:
        client = client_with(lambda request: httpx.Response(200, json=BALANCE))
        rows = client.stock()
        self.assertEqual(len(rows), 2)


    def test_wrong_password_is_explained(self) -> None:
        client = client_with(lambda request: httpx.Response(401, text=""))
        with self.assertRaises(OneCError) as ctx:
                client.check()
        self.assertIn("логин или пароль", str(ctx.exception))


    def test_html_instead_of_json_is_explained(self) -> None:
        client = client_with(lambda request: httpx.Response(200, text="<html>логин</html>"))
        with self.assertRaises(OneCError) as ctx:
                client.stock()
        self.assertIn("не JSON", str(ctx.exception))


    def test_empty_balance_is_explained(self) -> None:
        client = client_with(lambda request: httpx.Response(200, json={"value": []}))
        with self.assertRaises(OneCError) as ctx:
                client.stock()
        self.assertIn("нет остатков", str(ctx.exception))



NOMENCLATURE = {
    "value": [
        {"Ref_Key": "n-1", "Артикул": "KR-001", "Наименование": "Уголок монтажный 50х50 мм",
         "ЕдиницаИзмерения": "шт"},
        {"Ref_Key": "n-2", "Артикул": "EL-001", "Наименование": "Кабель ВВГнг-LS 3х2.5",
         "ЕдиницаИзмерения": "м"},
    ]
}
WAREHOUSES = {"value": [{"Ref_Key": "w-1", "Description": "Основной склад"}]}
INCOMING = {
    "value": [
        {
            "Склад_Key": "w-1",
            "Товары": [
                {"Номенклатура_Key": "n-1", "Количество": 100},
                {"Номенклатура_Key": "n-2", "Количество": "50"},
            ],
        }
    ]
}
OUTGOING = {
    "value": [
        {
            "Склад_Key": "w-1",
            "Товары": [{"Номенклатура_Key": "n-1", "Количество": 30}],
        }
    ]
}


def documents_handler(request: httpx.Request) -> httpx.Response:
    """Поддельная 1С: справочники и документы, регистр остатков не опубликован."""
    path = request.url.path
    if "AccumulationRegister" in path:
        return httpx.Response(404, text="not found")
    if "Catalog_КлассификаторЕдиницИзмерения" in path:
        return httpx.Response(200, json=UNITS)
    if "Catalog_Номенклатура" in path:
        return httpx.Response(200, json=NOMENCLATURE)
    if "Catalog_Склады" in path:
        return httpx.Response(200, json=WAREHOUSES)
    if "Document_ПоступлениеТоваровУслуг" in path:
        return httpx.Response(200, json=INCOMING)
    if "Document_РеализацияТоваровУслуг" in path:
        return httpx.Response(200, json=OUTGOING)
    return httpx.Response(404, text="not found")


UNITS = {"value": [{"Ref_Key": "u-1", "Description": "шт"}, {"Ref_Key": "u-2", "Description": "м"}]}


def base_handler(request: httpx.Request) -> httpx.Response:
    """Справочники, которые нужны почти любому сценарию."""
    path = request.url.path
    if "Catalog_КлассификаторЕдиницИзмерения" in path:
        return httpx.Response(200, json=UNITS)
    if "Catalog_Номенклатура" in path:
        return httpx.Response(200, json=NOMENCLATURE)
    if "Catalog_Склады" in path:
        return httpx.Response(200, json=WAREHOUSES)
    if "Catalog_Организации" in path:
        return httpx.Response(200, json={"value": [{"Ref_Key": "org-1", "Description": "ООО Тест"}]})
    if "Catalog_Валюты" in path:
        return httpx.Response(200, json={"value": [{"Ref_Key": "cur-1", "Description": "руб."}]})
    return httpx.Response(404)


def order_handler(request: httpx.Request) -> httpx.Response:
    """Заказ клиента существует; POST перехватываем и запоминаем тело."""
    if "Document_ЗаказКлиента" in request.url.path:
        if request.method == "POST":
            order_handler.captured["payload"] = json.loads(request.content)
            return httpx.Response(201, json={"Ref_Key": "order-1"})
        return httpx.Response(200, json={"value": []})
    return base_handler(request)


order_handler.captured = {}


class DocumentStockTest(unittest.TestCase):
    def test_stock_is_incoming_minus_outgoing(self) -> None:
        rows = client_with(documents_handler).stock_from_documents()
        by_sku = {sku: (Decimal(qty), unit, warehouse) for sku, qty, unit, warehouse in rows}
        self.assertEqual(by_sku["KR-001"], (Decimal("70"), "шт", "Основной склад"))
        self.assertEqual(by_sku["EL-001"], (Decimal("50"), "м", "Основной склад"))

    def test_zero_balance_is_kept(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "Catalog_Номенклатура" in request.url.path:
                return httpx.Response(200, json=NOMENCLATURE)
            if "Catalog_Склады" in request.url.path:
                return httpx.Response(200, json=WAREHOUSES)
            if "Document_ПоступлениеТоваровУслуг" in request.url.path:
                return httpx.Response(200, json=INCOMING)
            if "Document_РеализацияТоваровУслуг" in request.url.path:
                return httpx.Response(200, json={"value": [{"Товары": [
                    {"Номенклатура_Key": "n-1", "Количество": 100},
                    {"Номенклатура_Key": "n-2", "Количество": 50},
                ]}]})
            return httpx.Response(404)

        rows = client_with(handler).stock_from_documents()
        by_sku = {sku: Decimal(qty) for sku, qty, _, _ in rows}
        self.assertEqual(by_sku["KR-001"], Decimal("0"))

    def test_auto_falls_back_to_documents(self) -> None:
        rows = client_with(documents_handler).stock_auto()
        self.assertEqual({sku for sku, _, _, _ in rows}, {"KR-001", "EL-001"})

    def test_no_documents_is_explained(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "Catalog_Номенклатура" in request.url.path:
                return httpx.Response(200, json=NOMENCLATURE)
            return httpx.Response(404)

        with self.assertRaises(OneCError) as ctx:
            client_with(handler).stock_from_documents()
        self.assertIn("нет документов", str(ctx.exception))

    def test_auto_reports_both_sources(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text="not found")

        with self.assertRaises(OneCError) as ctx:
            client_with(handler).stock_auto()
        self.assertIn("регистр", str(ctx.exception))
        self.assertIn("документы", str(ctx.exception))


class NomenclatureUnitsTest(unittest.TestCase):
    def test_unit_name_is_resolved_from_classifier(self) -> None:
        rows = client_with(base_handler).nomenclature()
        self.assertEqual(rows["n-1"][0], "KR-001")
        self.assertEqual(rows["n-1"][2], "шт")
        self.assertEqual(rows["n-2"][2], "м")


class StockSourceTest(unittest.TestCase):
    def test_missing_registers_are_listed(self) -> None:
        client = client_with(lambda request: httpx.Response(404, text="not found"))
        with self.assertRaises(OneCError) as ctx:
            client.stock()
        message = str(ctx.exception)
        self.assertIn("пробовали", message)
        self.assertIn("ТоварыНаСкладах", message)


class OrderCreationTest(unittest.TestCase):
    def test_order_payload_uses_refs_and_string_numbers(self) -> None:
        order_handler.captured.clear()
        result = client_with(order_handler).create_order(
            [("KR-001", "10", "34.50"), ("EL-001", "2", "128")]
        )
        self.assertEqual(result, "order-1")
        payload = order_handler.captured["payload"]
        self.assertIn("Date", payload)
        self.assertEqual(payload["Товары"][0]["Номенклатура_Key"], "n-1")
        self.assertEqual(payload["Товары"][0]["Количество"], "10")
        self.assertEqual(payload["Товары"][1]["Цена"], "128")

    def test_unknown_sku_is_explained(self) -> None:
        with self.assertRaises(OneCError) as ctx:
            client_with(order_handler).create_order([("NO-SUCH", "1", "10")])
        self.assertIn("NO-SUCH", str(ctx.exception))
        self.assertIn("нет артикулов", str(ctx.exception))

    def test_1c_rejection_text_is_kept(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "Document_ЗаказКлиента" in request.url.path:
                if request.method == "POST":
                    return httpx.Response(400, text="ПередЗаписью: не заполнен склад")
                return httpx.Response(200, json={"value": []})
            return base_handler(request)

        with self.assertRaises(OneCError) as ctx:
            client_with(handler).create_order([("KR-001", "1", "10")])
        self.assertIn("ПередЗаписью", str(ctx.exception))

    def test_empty_order_is_rejected_before_network(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("сеть не должна вызываться")

        with self.assertRaises(OneCError):
            client_with(handler).create_order([])

    def test_bukhbase_without_orders_is_explained(self) -> None:
        # в Бухгалтерии нет «Заказа клиента» — 404 на сущность
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text="not found")

        with self.assertRaises(OneCError) as ctx:
            client_with(handler).create_order([("KR-001", "1", "10")])
        self.assertIn("Бухгалтерию", str(ctx.exception))


class InvoiceCreationTest(unittest.TestCase):
    def _handler(self, capture: dict, status: int = 201):
        def handler(request: httpx.Request) -> httpx.Response:
            if "Document_СчетНаОплатуПокупателю" in request.url.path:
                if request.method == "POST":
                    capture["payload"] = json.loads(request.content)
                    if status >= 400:
                        return httpx.Response(status, text="внутренняя ошибка OData")
                    return httpx.Response(201, json={"Ref_Key": "inv-1"})
                return httpx.Response(200, json={"value": []})
            return base_handler(request)

        return handler

    def test_header_fields_follow_real_schema(self) -> None:
        capture: dict = {}
        result = client_with(self._handler(capture)).create_invoice(
            counterparty="cp-1", lines=[("KR-001", "2", "100")]
        )
        self.assertEqual(result, "inv-1")
        payload = capture["payload"]
        self.assertEqual(payload["Организация_Key"], "org-1")
        self.assertEqual(payload["ВалютаДокумента_Key"], "cur-1")
        self.assertEqual(payload["Склад_Key"], "w-1")
        self.assertEqual(payload["СуммаДокумента"], "200")
        self.assertIs(payload["СуммаВключаетНДС"], True)
        self.assertEqual(payload["Товары"][0]["Номенклатура_Key"], "n-1")
        self.assertEqual(payload["Товары"][0]["Количество"], "2")

    def test_500_hints_at_file_export(self) -> None:
        with self.assertRaises(OneCError) as ctx:
            client_with(self._handler({}, status=500)).create_invoice(
                counterparty="cp-1", lines=[("KR-001", "1", "100")]
            )
        self.assertIn("табличную часть", str(ctx.exception).lower())

    def test_counterparty_is_required(self) -> None:
        with self.assertRaises(OneCError) as ctx:
            client_with(base_handler).create_invoice(counterparty="", lines=[("KR-001", "1", "1")])
        self.assertIn("контрагент", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
