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
    """Заказ клиента существует; запоминаем шапку и табличную часть отдельно."""
    if "Document_ЗаказКлиента" in request.url.path:
        if request.method == "POST":
            order_handler.captured["header"] = json.loads(request.content)
            return httpx.Response(201, json={"Ref_Key": "order-1"})
        if request.method == "PATCH":
            order_handler.captured["patch"] = json.loads(request.content)
            return httpx.Response(200, json={})
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
        header = order_handler.captured["header"]
        patch = order_handler.captured["patch"]
        self.assertIn("Date", header)
        self.assertNotIn("Товары", header)
        self.assertEqual(patch["Товары"][0]["Номенклатура_Key"], "n-1")
        self.assertEqual(patch["Товары"][0]["Количество"], 10)
        self.assertEqual(patch["Товары"][1]["Цена"], 128)
        self.assertEqual(patch["Товары"][0]["LineNumber"], "1")
        self.assertEqual(patch["Товары"][1]["LineNumber"], "2")

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
                    capture["header"] = json.loads(request.content)
                    if status >= 400:
                        return httpx.Response(status, text="внутренняя ошибка OData")
                    return httpx.Response(201, json={"Ref_Key": "inv-1"})
                if request.method == "PATCH":
                    capture["patch"] = json.loads(request.content)
                    return httpx.Response(200, json={})
                return httpx.Response(200, json={"value": []})
            return base_handler(request)

        return handler

    def test_header_and_lines_follow_real_schema(self) -> None:
        capture: dict = {}
        result = client_with(self._handler(capture)).create_invoice(
            counterparty="cp-1", lines=[("KR-001", "2", "100")]
        )
        self.assertEqual(result, "inv-1")
        header = capture["header"]
        self.assertEqual(header["Организация_Key"], "org-1")
        self.assertEqual(header["ВалютаДокумента_Key"], "cur-1")
        self.assertEqual(header["Склад_Key"], "w-1")
        self.assertIs(header["СуммаВключаетНДС"], False)
        self.assertIs(header["ДокументБезНДС"], False)
        self.assertNotIn("Товары", header)

        patch = capture["patch"]
        self.assertEqual(patch["СуммаДокумента"], 240.0)
        line = patch["Товары"][0]
        self.assertEqual(line["Номенклатура_Key"], "n-1")
        self.assertEqual(line["Количество"], 2)
        self.assertEqual(line["Сумма"], 200.0)
        self.assertEqual(line["СуммаНДС"], 40.0)
        self.assertEqual(line["СтавкаНДС"], "НДС20")
        self.assertEqual(line["LineNumber"], "1")

    def test_without_vat_marks_document(self) -> None:
        capture: dict = {}
        client_with(self._handler(capture)).create_invoice(
            counterparty="cp-1",
            lines=[("KR-001", "2", "100")],
            vat_rate=Decimal("0"),
        )
        self.assertIs(capture["header"]["ДокументБезНДС"], True)
        line = capture["patch"]["Товары"][0]
        self.assertEqual(line["СтавкаНДС"], "БезНДС")
        self.assertEqual(line["СуммаНДС"], 0.0)
        self.assertEqual(capture["patch"]["СуммаДокумента"], 200.0)

    def test_500_is_shown_with_text(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "Document_СчетНаОплатуПокупателю" in request.url.path:
                return httpx.Response(
                    500, json={"odata.error": {"message": {"value": "ПередЗаписью: склад"}}}
                )
            return base_handler(request)

        with self.assertRaises(OneCError) as ctx:
            client_with(handler).create_invoice(
                counterparty="cp-1", lines=[("KR-001", "1", "100")]
            )
        self.assertIn("ПередЗаписью", str(ctx.exception))
        self.assertIn("500", str(ctx.exception))

    def test_counterparty_is_required(self) -> None:
        with self.assertRaises(OneCError) as ctx:
            client_with(base_handler).create_invoice(counterparty="", lines=[("KR-001", "1", "1")])
        self.assertIn("контрагент", str(ctx.exception))


class InvoiceLifecycleTest(unittest.TestCase):
    def test_create_patch_post_unpost_delete(self) -> None:
        calls: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            calls.append((request.method, path))
            if "Document_СчетНаОплатуПокупателю" in path:
                if request.method == "POST" and path.endswith("/Post()"):
                    return httpx.Response(200, text="")
                if request.method == "POST" and path.endswith("/Unpost()"):
                    return httpx.Response(200, text="")
                if request.method == "POST":
                    return httpx.Response(201, json={"Ref_Key": "inv-9"})
                if request.method == "PATCH":
                    return httpx.Response(200, json={})
                if request.method == "DELETE":
                    return httpx.Response(204, text="")
                return httpx.Response(200, json={"Posted": True})
            return base_handler(request)

        client = client_with(handler)
        ref = client.create_invoice(
            counterparty="cp-1", lines=[("KR-001", "2", "100")], post=True
        )
        self.assertEqual(ref, "inv-9")
        client.unpost_document("Document_СчетНаОплатуПокупателю", ref)
        client.delete_entity("Document_СчетНаОплатуПокупателю", ref)

        methods = [method for method, _ in calls if "Document_Счет" in _]
        self.assertEqual(
            methods, ["POST", "PATCH", "POST", "POST", "DELETE"]
        )

    def test_odata_error_text_is_extracted(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={"odata.error": {"message": {"value": "Не заполнен контрагент"}}},
            )

        with self.assertRaises(OneCError) as ctx:
            client_with(handler).delete_entity("Catalog_Номенклатура", "x")
        self.assertIn("Не заполнен контрагент", str(ctx.exception))


class InvoiceServiceTest(unittest.TestCase):
    def _client(self, handler, service_url="https://1c.example/hs/kpbot/invoice"):
        http = httpx.Client(transport=httpx.MockTransport(handler), timeout=5)
        return OneCClient("https://1c.example/base", "user", "pass", http=http,
                          service_url=service_url)

    def test_success_uses_service_and_returns_number(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["payload"] = json.loads(request.content)
            return httpx.Response(200, json={"ok": True, "number": "0000-000777"})

        number = self._client(handler).create_invoice_via_service(
            counterparty="cp-1", lines=[("EL-001", "2", "100")]
        )
        self.assertEqual(number, "0000-000777")
        self.assertIn("/hs/kpbot/invoice", captured["url"])
        self.assertEqual(captured["payload"]["counterparty"], "cp-1")
        self.assertEqual(captured["payload"]["lines"][0]["sku"], "EL-001")
        self.assertEqual(captured["payload"]["lines"][0]["qty"], "2")

    def test_service_error_is_shown(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": False, "error": "не заполнен склад"})

        with self.assertRaises(OneCError) as ctx:
            self._client(handler).create_invoice_via_service(
                counterparty="cp-1", lines=[("EL-001", "1", "1")]
            )
        self.assertIn("не заполнен склад", str(ctx.exception))

    def test_without_service_url_is_explained(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("сеть не должна вызываться")

        with self.assertRaises(OneCError) as ctx:
            self._client(handler, service_url="").create_invoice_via_service(
                counterparty="cp-1", lines=[("EL-001", "1", "1")]
            )
        self.assertIn("KPBOT_ONE_C_SERVICE_URL", str(ctx.exception))

    def test_http_error_is_shown(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(502, text="плохой шлюз")

        with self.assertRaises(OneCError) as ctx:
            self._client(handler).create_invoice_via_service(
                counterparty="cp-1", lines=[("EL-001", "1", "1")]
            )
        self.assertIn("502", str(ctx.exception))


class PricesFromOneCTest(unittest.TestCase):
    def test_latest_period_wins_and_units_resolved(self) -> None:
        fixture = {
            "value": [
                {"Номенклатура_Key": "n-1", "Цена": 34.5, "Period": "2026-01-01T00:00:00"},
                {"Номенклатура_Key": "n-1", "Цена": 36.0, "Period": "2026-06-01T00:00:00"},
                {"Номенклатура_Key": "n-2", "Цена": "128"},
            ]
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if "InformationRegister_ЦеныНоменклатуры" in request.url.path:
                return httpx.Response(200, json=fixture)
            return base_handler(request)

        rows = client_with(handler).prices()
        by_sku = {row["sku"]: row for row in rows}
        self.assertEqual(by_sku["KR-001"]["price"], "36")
        self.assertEqual(by_sku["KR-001"]["unit"], "шт")
        self.assertEqual(by_sku["EL-001"]["price"], "128")

    def test_empty_register_is_explained(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "InformationRegister_ЦеныНоменклатуры" in request.url.path:
                return httpx.Response(200, json={"value": []})
            return base_handler(request)

        with self.assertRaises(OneCError) as ctx:
            client_with(handler).prices()
        self.assertIn("нет цен", str(ctx.exception))


class DecisionMarkerTest(unittest.TestCase):
    def test_last_decision_returns_latest(self) -> None:
        from kpbot.store import Store

        store = Store(support.ROOT / "marker.sqlite3")
        store.save_rfq(
            rfq_id="rfq-1", message_id="m1", sender="a@b.ru", subject="Заявка",
            received_at="2026-11-26T00:00:00+00:00", status="draft_ready",
            raw_text="уголок 1 шт", lines=[],
        )
        store.log("rfq-1", "invoice_1c", "cli", "0000-000001")
        store.log("rfq-1", "invoice_1c", "cli", "0000-000002")
        store.log("rfq-1", "approved", "ui", "")
        marker = store.last_decision("rfq-1", "invoice_1c")
        self.assertIsNotNone(marker)
        self.assertIn("0000-000002", marker["detail"])
        self.assertIsNone(store.last_decision("rfq-1", "invoice_odata"))


if __name__ == "__main__":
    unittest.main()
