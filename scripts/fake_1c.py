#!/usr/bin/env python3
"""Тестовый стенд 1С: отвечает по OData так же, как живая база.

Нужен, чтобы проверить весь путь интеграции без лицензионной базы:
адрес → авторизация → справочники → цены → остатки → запись документа.
Данные специально повторяют демо-прайс kpbot (KR-001, EL-001, MT-002).

Это стенд для проверки, а не замена 1С.
Запуск:
    python scripts/fake_1c.py --port 8200 --user kpbot --password test
"""

from __future__ import annotations

import argparse
import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

UNITS = [
    {"Ref_Key": "u1", "Description": "шт"},
    {"Ref_Key": "u2", "Description": "м"},
    {"Ref_Key": "u3", "Description": "меш"},
]

WAREHOUSES = [
    {"Ref_Key": "w1", "Description": "Основной склад"},
]

ORGANIZATIONS = [
    {"Ref_Key": "o1", "Description": "ООО «Ромашка»", "ИНН": "7700000000"},
]

CURRENCIES = [
    {"Ref_Key": "c1", "Description": "руб.", "Код": "643"},
]

COUNTERPARTIES = [
    {"Ref_Key": "cp1", "Description": "ООО «Клиент»", "ИНН": "7701234567"},
]

NOMENCLATURE = [
    {
        "Ref_Key": "n1",
        "Артикул": "KR-001",
        "Наименование": "Уголок монтажный 50х50 мм стальной",
        "ЕдиницаИзмерения_Key": "u1",
        "IsFolder": False,
        "DeletionMark": False,
    },
    {
        "Ref_Key": "n2",
        "Артикул": "EL-001",
        "Наименование": "Кабель ВВГнг-LS 3х2.5",
        "ЕдиницаИзмерения_Key": "u2",
        "IsFolder": False,
        "DeletionMark": False,
    },
    {
        "Ref_Key": "n3",
        "Артикул": "MT-002",
        "Наименование": "Цемент М500 50 кг",
        "ЕдиницаИзмерения_Key": "u3",
        "IsFolder": False,
        "DeletionMark": False,
    },
]

PRICES = [
    {"Номенклатура_Key": "n1", "Цена": "34.50", "Period": "2026-11-01T00:00:00"},
    {"Номенклатура_Key": "n2", "Цена": "128.00", "Period": "2026-11-01T00:00:00"},
    {"Номенклатура_Key": "n3", "Цена": "520.00", "Period": "2026-11-01T00:00:00"},
]

# правдоподобный ответ регистра «Товары на складах» (как отдаёт 1С)
BALANCE = {
    "odata.metadata": "http://fake-1c/odata/standard.odata/$metadata#AccumulationRegister_ТоварыНаСкладах",
    "value": [
        {
            "RecordType": "Balance",
            "LineNumber": 1,
            "Склад": "Основной склад",
            "Склад_Key": "w1",
            "Номенклатура": "Уголок монтажный 50х50 мм стальной",
            "Артикул": "KR-001",
            "КоличествоОстаток": 120,
        },
        {
            "RecordType": "Balance",
            "LineNumber": 2,
            "Склад": "Основной склад",
            "Склад_Key": "w1",
            "Номенклатура": "Кабель ВВГнг-LS 3х2.5",
            "Артикул": "EL-001",
            "КоличествоОстаток": "40.5",
        },
        {
            "RecordType": "Balance",
            "LineNumber": 3,
            "Склад": "Основной склад",
            "Склад_Key": "w1",
            "Номенклатура": "Цемент М500 50 кг",
            "Артикул": "MT-002",
            "КоличествоОстаток": 7,
        },
        {
            "RecordType": "Balance",
            "LineNumber": 4,
            "Склад": "Основной склад",
            "Склад_Key": "w1",
            "Номенклатура": "Шпаклевка финишная 25 кг",
            "Артикул": "MT-007",
            "КоличествоОстаток": 0,
        },
    ],
}

ENTITIES = [
    "Catalog_Номенклатура",
    "Catalog_КлассификаторЕдиницИзмерения",
    "Catalog_Склады",
    "Catalog_Организации",
    "Catalog_Валюты",
    "Catalog_Контрагенты",
    "InformationRegister_ЦеныНоменклатуры",
    "AccumulationRegister_ТоварыНаСкладах",
    "Document_ЗаказКлиента",
    "Document_СчетНаОплатуПокупателю",
]

_SCHEMA = "".join(
    f'<EntitySet Name="{name}" EntityType="StandardODATA.{name}"/>' for name in ENTITIES
)
METADATA = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx" '
    'xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata" Version="1.0">'
    "<edmx:DataServices>"
    '<Schema xmlns="http://schemas.microsoft.com/ado/2009/11/edm" Namespace="StandardODATA">'
    '<EntityContainer Name="EnterpriseV8" m:IsDefaultEntityContainer="true">'
    f"{_SCHEMA}"
    "</EntityContainer></Schema></edmx:DataServices></edmx:Edmx>"
)


class Handler(BaseHTTPRequestHandler):
    user = "kpbot"
    password = "test"

    def log_message(self, fmt: str, *args) -> None:  # тише в консоли
        return

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
        except Exception:
            return False
        return decoded == f"{self.user}:{self.password}"

    def _send(self, code: int, body: str, content_type: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, code: int, value: object) -> None:
        self._send(code, json.dumps({"value": value}, ensure_ascii=False), "application/json")

    def do_GET(self) -> None:
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="1C"')
            self.end_headers()
            return

        path = unquote(self.path.split("?")[0]).rstrip("/")
        if path.endswith("/$metadata"):
            self._send(200, METADATA, "application/xml")
            return
        if path.endswith("/Catalog_Номенклатура"):
            self._json(200, NOMENCLATURE)
            return
        if path.endswith("/Catalog_КлассификаторЕдиницИзмерения"):
            self._json(200, UNITS)
            return
        if path.endswith("/Catalog_Склады"):
            self._json(200, WAREHOUSES)
            return
        if path.endswith("/Catalog_Организации"):
            self._json(200, ORGANIZATIONS)
            return
        if path.endswith("/Catalog_Валюты"):
            self._json(200, CURRENCIES)
            return
        if path.endswith("/Catalog_Контрагенты"):
            self._json(200, COUNTERPARTIES)
            return
        if path.endswith("/InformationRegister_ЦеныНоменклатуры"):
            self._json(200, PRICES)
            return
        if "AccumulationRegister_ТоварыНаСкладах" in path and path.endswith("/Balance"):
            self._send(200, json.dumps(BALANCE, ensure_ascii=False), "application/json")
            return
        if path.endswith("/Document_ЗаказКлиента") or path.endswith(
            "/Document_СчетНаОплатуПокупателю"
        ):
            self._json(200, [])
            return
        self._send(404, json.dumps({"error": "not found", "path": path}, ensure_ascii=False),
                   "application/json")

    def do_POST(self) -> None:
        if not self._authorized():
            self.send_response(401)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            payload = {}
        created = {
            "Ref_Key": "00000000-0000-0000-0000-000000000001",
            "Number": "0000-000001",
            "Строк": len(payload.get("Товары") or []),
        }
        self._send(201, json.dumps(created, ensure_ascii=False), "application/json")


def main() -> None:
    parser = argparse.ArgumentParser(description="Тестовый стенд 1С (OData)")
    parser.add_argument("--port", type=int, default=8200)
    parser.add_argument("--user", default="kpbot")
    parser.add_argument("--password", default="test")
    args = parser.parse_args()

    Handler.user = args.user
    Handler.password = args.password
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"тестовый стенд 1С: http://127.0.0.1:{args.port} · логин {args.user}")
    server.serve_forever()


if __name__ == "__main__":
    main()
