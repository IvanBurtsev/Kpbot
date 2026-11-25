#!/usr/bin/env python3
"""Проверка, доступна ли 1С снаружи: OData, HTTP-сервис, веб-клиент.

Запуск:
    python scripts/check_1c.py https://edu.1cfresh.com/a/XXXX --user ЛОГИН --password ПАРОЛЬ

Ничего не меняет: только GET-запросы и чтение метаданных.
"""

from __future__ import annotations

import argparse
import base64
import sys

import httpx

ODATA_PATH = "/odata/standard.odata/"


def auth_header(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def probe(client: httpx.Client, url: str, title: str) -> dict:
    try:
        response = client.get(url)
    except httpx.HTTPError as exc:
        return {"title": title, "url": url, "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    body = response.text[:200].replace("\n", " ")
    return {"title": title, "url": url, "status": response.status_code, "body": body}


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка доступности 1С снаружи")
    parser.add_argument("base", help="адрес базы, например https://edu.1cfresh.com/a/123456")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--timeout", type=float, default=25.0)
    args = parser.parse_args()

    base = args.base.rstrip("/")
    headers = auth_header(args.user, args.password)
    client = httpx.Client(headers=headers, follow_redirects=True, timeout=args.timeout)

    checks = [
        (f"{base}{ODATA_PATH}", "OData: корень"),
        (f"{base}{ODATA_PATH}$metadata", "OData: метаданные"),
        (f"{base}{ODATA_PATH}Catalog_Номенклатура?$top=3&$format=json", "OData: номенклатура"),
        (f"{base}/hs/", "HTTP-сервисы"),
        (base, "Веб-клиент"),
    ]

    print(f"База: {base}\nПользователь: {args.user}\n")
    results = [probe(client, url, title) for url, title in checks]

    for item in results:
        if "error" in item:
            print(f"{item['title']:28} ошибка: {item['error']}")
            continue
        print(f"{item['title']:28} HTTP {item['status']}  {item['body'][:120]}")

    odata_ok = any(
        item.get("status") == 200 and "OData" in item["title"] for item in results
    )
    served = any(
        item.get("status") in (401, 403) and "OData" in item["title"] for item in results
    )
    print()
    if odata_ok:
        print("ВЫВОД: OData доступен — можно читать остатки и цены, создавать документы.")
        return 0
    if served:
        print("ВЫВОД: OData отвечает, но не пускает — проверьте логин и права пользователя.")
        return 1
    print("ВЫВОД: OData снаружи недоступен. Остаётся файловый обмен или другой способ.")
    print("        Для Fresh это ожидаемо: облачные базы обычно не публикуют OData.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
