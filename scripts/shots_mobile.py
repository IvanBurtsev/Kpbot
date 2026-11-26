"""Мобильные скриншоты kpbot (390×844) для проверки вёрстки.

Логин — через httpx и cookie; Playwright использует установленный Edge.
Запуск: KPBOT_UI_PW=... python scripts/shots_mobile.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
from playwright.sync_api import sync_playwright

BASE = os.environ.get("KPBOT_SHOT_URL", "https://debtcrm.duckdns.org:8443")
OUT = Path(__file__).resolve().parent.parent / "presentation" / "shots" / "mobile"
VIEWPORT = {"width": 390, "height": 844}


def session_cookie(password: str) -> str:
    client = httpx.Client(verify=False, timeout=30)
    response = client.post(
        f"{BASE}/ui/login",
        data={"email": "admin@local", "password": password},
        headers={"Origin": BASE},
        follow_redirects=False,
    )
    if response.status_code != 303:
        raise SystemExit(f"вход не удался: {response.status_code}")
    return response.cookies["kpbot_session"]


def main() -> int:
    password = os.environ.get("KPBOT_UI_PW", "")
    if not password:
        print("нет KPBOT_UI_PW")
        return 2
    OUT.mkdir(parents=True, exist_ok=True)
    token = session_cookie(password)
    host = urlparse(BASE).hostname or "debtcrm.duckdns.org"

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=2,
            is_mobile=True,
            has_touch=True,
            ignore_https_errors=True,
            locale="ru-RU",
        )
        context.add_cookies(
            [
                {
                    "name": "kpbot_session",
                    "value": token,
                    "domain": host,
                    "path": "/",
                    "secure": BASE.startswith("https"),
                    "httpOnly": True,
                }
            ]
        )
        page = context.new_page()
        page.set_default_timeout(30000)

        def shot(name: str, full: bool = True) -> None:
            path = OUT / f"{name}.png"
            page.screenshot(path=str(path), full_page=full)
            print(f"  {name}: {path.stat().st_size // 1024} КБ")

        page.goto(f"{BASE}/ui", wait_until="networkidle")
        shot("01-inbox")

        link = page.locator('a[href^="/ui/rfq/"]').first
        if link.count():
            rfq_id = link.get_attribute("href").rstrip("/").split("/")[-1]
            page.goto(f"{BASE}/ui/rfq/{rfq_id}", wait_until="networkidle")
            shot("02-rfq-card")

        for name, path in (
            ("03-ops", "/ui/ops"),
            ("04-admin", "/ui/admin"),
            ("05-setup", "/ui/setup"),
            ("06-prices", "/ui/prices"),
        ):
            page.goto(f"{BASE}{path}", wait_until="networkidle")
            shot(name)

        context.close()
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
