"""Скриншоты работающего kpbot для презентации.

Логин делается через httpx (форма + Origin), полученная cookie передаётся
Playwright — так скриншоты не зависят от поведения формы в браузере.
Запуск: KPBOT_UI_PW=... python scripts/shots_app.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
from playwright.sync_api import sync_playwright

BASE = os.environ.get("KPBOT_SHOT_URL", "https://debtcrm.duckdns.org:8443")
OUT = Path(__file__).resolve().parent.parent / "presentation" / "shots" / "app"
WIDTH, HEIGHT = 1600, 1000


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

        # Логин снимаем в отдельной анонимной сессии: с cookie страница входа
        # сразу редиректит на «Заявки».
        anonymous = browser.new_context(
            viewport={"width": WIDTH, "height": HEIGHT},
            ignore_https_errors=True,
            locale="ru-RU",
        )
        anon_page = anonymous.new_page()
        anon_page.set_default_timeout(30000)
        anon_page.goto(f"{BASE}/ui/login", wait_until="networkidle")
        anon_page.screenshot(path=str(OUT / "01-login.png"))
        print(f"  01-login: {(OUT / '01-login.png').stat().st_size // 1024} КБ")
        anonymous.close()

        context = browser.new_context(
            viewport={"width": WIDTH, "height": HEIGHT},
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

        def shot(name: str, full: bool = False) -> None:
            path = OUT / f"{name}.png"
            page.screenshot(path=str(path), full_page=full)
            print(f"  {name}: {path.stat().st_size // 1024} КБ")

        page.goto(f"{BASE}/ui", wait_until="networkidle")
        shot("02-inbox")

        link = page.locator('a[href^="/ui/rfq/"]').first
        if link.count():
            href = link.get_attribute("href")
            page.goto(f"{BASE}{href}", wait_until="networkidle")
            shot("03-rfq-card", full=True)
            rfq_id = href.rstrip("/").split("/")[-1]
            # КП отдаётся как файл (Content-Disposition: attachment), поэтому
            # забираем HTML по HTTP и открываем локально — так видно сам документ.
            kp = httpx.Client(verify=False, timeout=30, cookies={"kpbot_session": token}).get(
                f"{BASE}/ui/rfq/{rfq_id}/kp.html"
            )
            if kp.status_code == 200:
                local = Path(__file__).resolve().parent.parent / ".tmp" / "kp-preview.html"
                local.write_text(kp.text, encoding="utf-8")
                page.goto(local.as_uri(), wait_until="networkidle")
                shot("04-kp-html", full=True)

        for name, path in (
            ("05-prices", "/ui/prices"),
            ("06-setup", "/ui/setup"),
            ("07-stats", "/ui/stats"),
            ("08-report", "/ui/report"),
            ("09-admin", "/ui/admin"),
        ):
            page.goto(f"{BASE}{path}", wait_until="networkidle")
            shot(name, full=True)

        page.goto(f"{BASE}/cabinet/login", wait_until="networkidle")
        shot("10-cabinet-login")

        context.close()
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
