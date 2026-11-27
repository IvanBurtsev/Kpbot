"""Настройки компании: реквизиты в КП и подпись в письме."""

import support  # общий стенд: изолированные данные, сеть запрещена

import os
import unittest
from pathlib import Path

_TMP = support.subdir("settings_ui")
os.environ.setdefault("KPBOT_API_KEY", "test-key")
os.environ.setdefault("KPBOT_UI_PASSWORD", "test-pass")
os.environ.setdefault("KPBOT_SESSION_SECRET", "test-secret")

from fastapi.testclient import TestClient  # noqa: E402

from kpbot.web import app, pipeline, settings, store  # noqa: E402


def csrf(client: TestClient) -> str:
    page = client.get("/ui/settings")
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


class CompanySettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/ui/login", data={"password": settings.ui_password})

    def tearDown(self) -> None:
        with store._connect() as conn, conn:
            conn.execute("DELETE FROM app_settings")

    def test_saved_settings_reach_quote_and_signature(self) -> None:
        token = csrf(self.client)
        response = self.client.post(
            "/ui/settings",
            data={
                "csrf_token": token,
                "name": "ООО «Тест-Снаб»",
                "inn": "7701234567",
                "kpp": "770101001",
                "address": "Москва, ул. Тестовая, 1",
                "phone": "+7 495 111-22-33",
                "email": "sales@test-snab.ru",
                "signer": "Петров П. П., директор",
                "signature": "С уважением,\n{{подписант}}\n{{компания}}",
                "kp_intro": "Цены действительны 7 дней",
            },
        )
        self.assertEqual(response.status_code, 303)

        company = pipeline.company()
        self.assertEqual(company["name"], "ООО «Тест-Снаб»")
        self.assertEqual(company["inn"], "7701234567")

        rfq = pipeline.process_text("уголок 50х50 — 10 шт", sender="a@b.ru", subject="Заявка")
        quote = store.get_quote(rfq.rfq_id)
        from kpbot.pipeline import build_reply_text_from_store

        text = build_reply_text_from_store(store.get_rfq(rfq.rfq_id), quote, rfq.rfq_id, company)
        self.assertIn("Петров П. П.", text)
        self.assertIn("ООО «Тест-Снаб»", text)

        html = (settings.out_dir / f"{rfq.rfq_id}.html").read_text(encoding="utf-8")
        self.assertIn("Тест-Снаб", html)
        self.assertIn("7701234567", html)
        self.assertIn("Цены действительны 7 дней", html)

    def test_vat_20_is_not_saved(self) -> None:
        """С 2026 года 20% незаконна: настройка не сохраняется, человек видит причину."""
        token = csrf(self.client)
        response = self.client.post(
            "/ui/settings",
            data={"csrf_token": token, "name": "ООО «Ставка»", "vat_rate": "20"},
        )
        self.assertEqual(response.status_code, 303)
        self.assertNotEqual(store.get_settings().get("vat_rate"), "0.2000")
        self.assertIn("flash=", response.headers["location"])
        # ставка не записалась — расчёт возьмёт законные 22% из конфига
        self.assertEqual(pipeline.calc().vat_rate, settings.vat_rate)

    def test_vat_22_is_saved(self) -> None:
        token = csrf(self.client)
        response = self.client.post(
            "/ui/settings",
            data={"csrf_token": token, "name": "ООО «Ставка»", "vat_rate": "22"},
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(store.get_settings().get("vat_rate"), "0.2200")

    def test_manager_cannot_open_settings(self) -> None:
        try:
            store.create_user(email="mgr-settings@test.ru", password="managerpass", role="manager")
        except Exception:  # noqa: BLE001
            pass
        manager = TestClient(app, follow_redirects=False)
        manager.post("/ui/login", data={"email": "mgr-settings@test.ru", "password": "managerpass"})
        self.assertEqual(manager.get("/ui/settings").status_code, 403)


if __name__ == "__main__":
    unittest.main()
