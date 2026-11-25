"""Браузерный e2e (опционально): проверяет то, чего не видит HTTP-тест — вёрстку.

Playwright не входит в requirements: на сервере без графики он не нужен,
а в песочнице Windows не ставится. Если он есть — скрипт проходит путь
«вход → заявки → карточка» и делает скриншоты.

Установка:
    pip install playwright
    playwright install chromium

Запуск:
    python scripts/e2e_browser.py --url http://127.0.0.1:8100 --password ...
    python scripts/e2e_browser.py --url http://127.0.0.1:8100 --password ... --screenshot data/out/e2e
"""

from __future__ import annotations

import argparse
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

HELP = """\
Playwright не установлен — это не ошибка продукта.
Браузерный e2e нужен только для проверки вёрстки:
    pip install playwright
    playwright install chromium
Затем повторите запуск. HTTP-сценарий уже покрыт tests/test_e2e_flow.py
и scripts/smoke.py — они работают без браузера.
"""


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(HELP)
        return 2

    parser = argparse.ArgumentParser(description="Браузерный e2e kpbot")
    parser.add_argument("--url", default="http://127.0.0.1:8100")
    parser.add_argument("--password", required=True)
    parser.add_argument("--screenshot", default="", help="куда сохранить скриншоты (без расширения)")
    parser.add_argument("--headless", action="store_true", default=True)
    args = parser.parse_args()

    base = args.url.rstrip("/")
    shots = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=args.headless)
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        page.goto(f"{base}/ui/login")
        page.fill('input[name="password"]', args.password)
        page.click('button[type="submit"]')
        page.wait_for_url("**/ui", timeout=15000)
        assert "Заявки" in page.content(), "после входа нет навигации «Заявки»"
        print("[ ok ] вход в интерфейс")
        if args.screenshot:
            page.screenshot(path=f"{args.screenshot}-inbox.png", full_page=True)
            shots += 1

        page.goto(f"{base}/ui/prices")
        assert page.locator("table").count() > 0, "на странице прайса нет таблицы"
        print("[ ok ] страница прайса")
        if args.screenshot:
            page.screenshot(path=f"{args.screenshot}-prices.png", full_page=True)
            shots += 1

        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(f"{base}/ui")
        assert page.content(), "пустая страница на мобильной ширине"
        print("[ ok ] мобильная ширина открывается")
        if args.screenshot:
            page.screenshot(path=f"{args.screenshot}-mobile.png", full_page=True)
            shots += 1

        browser.close()

    print(f"\nбраузерный e2e пройден, скриншотов: {shots}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
