"""Клиент к 1С: разбор ответов OData и поведение при ошибках.

Сеть не трогаем: подменяем транспорт httpx на заранее заготовленные ответы 1С.
Форматы ответов взяты из реального OData 1С (Balance регистра накопления,
$metadata, создание документа).
"""

import support  # общий стенд: изолированные данные, сеть запрещена

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



if __name__ == "__main__":
    unittest.main()
