#!/usr/bin/env python3
"""Тестовый стенд 1С: отвечает по OData так же, как живая база.

Нужен, чтобы проверить весь путь интеграции без лицензионной базы:
адрес → авторизация → разбор остатков → запись в kpbot.

Это стенд для проверки, а не замена 1С: данные берутся из файла рядом.
Запуск:
    python scripts/fake_1c.py --port 8200 --user Пользователь --password Пароль
"""

from __future__ import annotations

import argparse
import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

METADATA = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx" Version="1.0">'
    "<edmx:DataServices/>"
    "</edmx:Edmx>"
)

# правдоподобный ответ регистра «Товары на складах» (как отдаёт 1С)
BALANCE = {
    "odata.metadata": "http://fake-1c/odata/standard.odata/$metadata#AccumulationRegister_ТоварыНаСкладах",
    "value": [
        {
            "RecordType": "Balance",
            "LineNumber": 1,
            "Склад": "Основной склад",
            "Склад_Key": "11111111-1111-1111-1111-111111111111",
            "Номенклатура": "Уголок монтажный 50х50 мм стальной",
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
        {
            "RecordType": "Balance",
            "LineNumber": 3,
            "Склад": "Основной склад",
            "Номенклатура": "Цемент М500 50 кг",
            "Артикул": "MT-002",
            "КоличествоОстаток": 7,
        },
        {
            "RecordType": "Balance",
            "LineNumber": 4,
            "Склад": "Основной склад",
            "Номенклатура": "Шпаклевка финишная 25 кг",
            "Артикул": "MT-007",
            "КоличествоОстаток": 0,
        },
    ],
}


class Handler(BaseHTTPRequestHandler):
    user = "Пользователь"
    password = "Пароль"

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

    def do_GET(self) -> None:
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="1C"')
            self.end_headers()
            return
        path = self.path.split("?")[0]
        if path.endswith("/$metadata"):
            self._send(200, METADATA, "application/xml")
            return
        if "AccumulationRegister" in path and path.endswith("/Balance"):
            self._send(200, json.dumps(BALANCE, ensure_ascii=False), "application/json")
            return
        self._send(404, json.dumps({"error": "not found"}, ensure_ascii=False), "application/json")

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
    parser.add_argument("--user", default="Пользователь")
    parser.add_argument("--password", default="Пароль")
    args = parser.parse_args()

    Handler.user = args.user
    Handler.password = args.password
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"тестовый стенд 1С: http://127.0.0.1:{args.port} · логин {args.user}")
    server.serve_forever()


if __name__ == "__main__":
    main()