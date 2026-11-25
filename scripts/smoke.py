"""Проверка живого сервиса после деплоя: без браузера и без записи данных.

Запуск на сервере или с машины, которая его видит:

    python scripts/smoke.py --url http://127.0.0.1:8100
    python scripts/smoke.py --url https://kp.example.ru --api-key ... --password ...

Проверяет: healthz, API, вход, закрытость интерфейса, статику и авторизацию API.
Ничего не создаёт: POST без ключа должен вернуть 401 и не дойти до базы.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import httpx

if hasattr(sys.stdout, "reconfigure"):  # консоль Windows не знает часть символов
    sys.stdout.reconfigure(errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка живого kpbot")
    parser.add_argument("--url", default="http://127.0.0.1:8100")
    parser.add_argument("--api-key", default=os.environ.get("KPBOT_API_KEY", ""))
    parser.add_argument("--password", default=os.environ.get("KPBOT_UI_PASSWORD", ""))
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    base = args.url.rstrip("/")
    results: list[tuple[str, str, str]] = []

    def check(name: str, func) -> None:
        started = time.perf_counter()
        try:
            detail = func()
        except Exception as exc:  # noqa: BLE001 - отчёт важнее типа исключения
            results.append(("FAIL", name, f"{type(exc).__name__}: {str(exc)[:120]}"))
            return
        results.append(("OK", name, f"{detail} · {(time.perf_counter() - started) * 1000:.0f} мс"))

    with httpx.Client(timeout=args.timeout, follow_redirects=False) as client:
        def healthz() -> str:
            response = client.get(f"{base}/healthz")
            assert response.status_code == 200, f"HTTP {response.status_code}"
            payload = response.json()
            assert payload.get("status") == "ok", payload
            return "status=ok"

        def api_health() -> str:
            response = client.get(f"{base}/api/v1/health")
            assert response.status_code == 200, f"HTTP {response.status_code}"
            return "200"

        def login_page() -> str:
            response = client.get(f"{base}/ui/login")
            assert response.status_code == 200, f"HTTP {response.status_code}"
            assert "Вход" in response.text, "нет формы входа"
            return "форма входа на месте"

        def ui_is_closed() -> str:
            response = client.get(f"{base}/ui")
            assert response.status_code == 303, f"HTTP {response.status_code}"
            assert "/ui/login" in response.headers.get("location", ""), response.headers.get("location")
            return "редирект на вход"

        def api_requires_key() -> str:
            response = client.post(
                f"{base}/api/v1/rfqs",
                json={"sender": "smoke@example.ru", "subject": "smoke", "text": "уголок 1 шт"},
            )
            assert response.status_code == 401, f"HTTP {response.status_code}"
            return "401 без ключа"

        def static_css() -> str:
            response = client.get(f"{base}/static/app.css")
            assert response.status_code == 200, f"HTTP {response.status_code}"
            assert len(response.content) > 1000, "пустой css"
            return f"{len(response.content)} байт"

        def api_with_key() -> str:
            if not args.api_key:
                raise RuntimeError("ключ не передан: пропущено")
            response = client.get(f"{base}/api/v1/stats", headers={"X-API-Key": args.api_key})
            assert response.status_code == 200, f"HTTP {response.status_code}"
            return "статистика доступна"

        def login_works() -> str:
            if not args.password:
                raise RuntimeError("пароль не передан: пропущено")
            response = client.post(f"{base}/ui/login", data={"password": args.password})
            assert response.status_code == 303, f"HTTP {response.status_code}"
            assert "kpbot_session" in response.cookies, "нет cookie сессии"
            return "вход выполнен"

        check("healthz", healthz)
        check("API health", api_health)
        check("статик", static_css)
        check("интерфейс закрыт", ui_is_closed)
        check("API без ключа", api_requires_key)
        check("API с ключом", api_with_key)
        check("вход в интерфейс", login_works)
        check("страница входа", login_page)

    width = max(len(name) for _, name, _ in results)
    for status, name, detail in results:
        mark = "[ ok ]" if status == "OK" else "[fail]"
        print(f"{mark} {name.ljust(width)}  {detail}")

    failed = sum(1 for status, _, _ in results if status == "FAIL")
    print(f"\nсервис: {base} · проверок {len(results)}, провалено {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
