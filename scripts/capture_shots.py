"""Снимает настоящие скриншоты kpbot для презентации — запускать на своей машине.

В песочнице агента браузер не запускается (запрещены именованные каналы),
поэтому этот шаг выполняется один раз локально:

    pip install playwright
    playwright install chromium      # или используйте уже установленный Edge
    python scripts/capture_shots.py --url http://127.0.0.1:8000

Скриншоты кладутся в data/presentation/shots, после чего
`python scripts/build_deck.py` соберёт колоду уже с настоящими экранами.

Пароль берётся из .env (KPBOT_UI_PASSWORD) или из --password.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "data" / "presentation" / "shots"
ENV = ROOT / ".env"


def env_password() -> str:
    if not ENV.exists():
        return ""
    match = re.search(r"^KPBOT_UI_PASSWORD=(.+)$", ENV.read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1).strip() if match else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Скриншоты интерфейса kpbot")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--password", default="")
    parser.add_argument("--channel", default="msedge", help="msedge, chrome или пусто для Chromium")
    parser.add_argument("--headless", action="store_true", default=True)
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("нужен Playwright: pip install playwright && playwright install chromium")
        return 2

    password = args.password or env_password()
    if not password:
        print("не задан пароль: --password или KPBOT_UI_PASSWORD в .env")
        return 2

    SHOTS.mkdir(parents=True, exist_ok=True)
    base = args.url.rstrip("/")

    def shoot(page, name: str, full: bool = False) -> None:
        page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=full)
        print("снято:", name)

    with sync_playwright() as playwright:
        launch = {"headless": args.headless}
        if args.channel:
            launch["channel"] = args.channel
        browser = playwright.chromium.launch(**launch)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        page = context.new_page()

        page.goto(f"{base}/ui/login", wait_until="networkidle")
        shoot(page, "01-login")
        page.fill('input[name="password"]', password)
        page.click('button[type="submit"]')
        page.wait_for_url("**/ui", timeout=20000)
        page.wait_for_load_state("networkidle")
        shoot(page, "02-inbox", full=True)

        href = page.locator('a[href^="/ui/rfq/"]').first.get_attribute("href")
        page.goto(f"{base}{href}", wait_until="networkidle")
        shoot(page, "03-rfq", full=True)

        for path, name in (
            ("/ui/prices", "04-prices"),
            ("/ui/stats", "05-stats"),
            ("/ui/admin", "06-admin"),
            ("/ui/setup", "07-setup"),
            ("/api/docs", "08-api"),
        ):
            page.goto(f"{base}{path}", wait_until="networkidle")
            shoot(page, name, full=name in {"06-admin", "07-setup"})

        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(f"{base}/ui", wait_until="networkidle")
        shoot(page, "09-mobile", full=True)
        browser.close()

    print("готово:", SHOTS)
    print("теперь: python scripts/build_deck.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
