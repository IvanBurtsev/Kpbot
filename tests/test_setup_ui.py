"""Визард подключения: шаги, состояние и доступ."""

import support  # общий стенд: изолированные данные, сеть запрещена

import os
import unittest
from pathlib import Path

_TMP = support.subdir("setup_ui")
os.environ.setdefault("KPBOT_API_KEY", "test-key")
os.environ.setdefault("KPBOT_UI_PASSWORD", "test-pass")
os.environ.setdefault("KPBOT_SESSION_SECRET", "test-secret")

from fastapi.testclient import TestClient  # noqa: E402

from kpbot.web import app, settings, store  # noqa: E402


def csrf(client: TestClient) -> str:
    page = client.get("/ui/setup")
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


class SetupWizardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/ui/login", data={"password": settings.ui_password})
        # Прайс общий на весь тестовый прогон: шаг визарда заменяет его целиком,
        # поэтому после проверки возвращаем как было. Иначе следующие тесты
        # получают чужой прайс из двух позиций и падают «на пустом месте».
        self.saved_prices = store.price_items()
        with store._connect() as conn, conn:
            conn.execute("DELETE FROM app_settings")

    def tearDown(self) -> None:
        store.replace_price_items(self.saved_prices)

    def test_page_opens_and_shows_three_steps(self) -> None:
        page = self.client.get("/ui/setup")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Шаг 1", page.text)
        self.assertIn("Шаг 2", page.text)
        self.assertIn("Шаг 3", page.text)

    def test_company_step_marks_progress(self) -> None:
        token = csrf(self.client)
        response = self.client.post(
            "/ui/setup/company",
            data={"csrf_token": token, "name": "ООО «Пилот»", "inn": "7701234567"},
        )
        self.assertEqual(response.status_code, 303)
        page = self.client.get("/ui/setup")
        self.assertIn("Пилот", page.text)
        self.assertIn("готово", page.text)

    def test_price_step_counts_positions(self) -> None:
        token = csrf(self.client)
        payload = "Код;Наименование;Единица;Цена\nA-1;Позиция;шт;10\nA-2;Вторая;шт;20\n"
        response = self.client.post(
            "/ui/setup/price",
            data={"csrf_token": token},
            files={"file": ("price.csv", payload.encode("utf-8"), "text/csv")},
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(store.price_count(), 2)

    def test_bad_file_is_rejected_with_message(self) -> None:
        token = csrf(self.client)
        response = self.client.post(
            "/ui/setup/price",
            data={"csrf_token": token},
            files={"file": ("price.pdf", b"%PDF", "application/pdf")},
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("flash=", response.headers["location"])

    def test_mail_check_reports_result(self) -> None:
        token = csrf(self.client)
        response = self.client.post("/ui/setup/mail", data={"csrf_token": token})
        self.assertEqual(response.status_code, 303)
        self.assertIn("flash=", response.headers["location"])

    def test_manager_cannot_open_wizard(self) -> None:
        try:
            store.create_user(email="mgr-setup@test.ru", password="managerpass", role="manager")
        except Exception:  # noqa: BLE001
            pass
        manager = TestClient(app, follow_redirects=False)
        manager.post("/ui/login", data={"email": "mgr-setup@test.ru", "password": "managerpass"})
        self.assertEqual(manager.get("/ui/setup").status_code, 403)


if __name__ == "__main__":
    unittest.main()
