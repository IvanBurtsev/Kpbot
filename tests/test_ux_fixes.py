"""Правки по отзывам: понятные статусы, подсказки, список без почты, 1С из UI."""

import support  # общий стенд: изолированные данные, сеть запрещена

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from kpbot.security import SESSION_COOKIE, csrf_for
from kpbot.store import Store
from kpbot.web import app, settings, store


class UxFixesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/ui/login", data={"password": settings.ui_password})
        session = self.client.cookies.get(SESSION_COOKIE)
        self.csrf = csrf_for(settings.session_secret, session)

    def _rfq(self, sender: str, text: str = "уголок 50х50 — 10 шт") -> str:
        response = self.client.post(
            "/api/v1/rfqs",
            json={"sender": sender, "subject": "Заявка", "text": text},
            headers={"X-API-Key": settings.api_key},
        )
        self.assertIn(response.status_code, (200, 201))
        return response.json()["rfq_id"]

    def test_inbox_shows_company_not_email(self) -> None:
        self._rfq("ИП Сергеев <sergeev.demo@example.ru>")
        page = self.client.get("/ui")
        self.assertEqual(page.status_code, 200)
        self.assertIn("ИП Сергеев", page.text)
        self.assertNotIn("sergeev.demo@example.ru", page.text)
        # в карточке почта остаётся
        rfq_id = store.list_rfqs(1)[0]["id"]
        card = self.client.get(f"/ui/rfq/{rfq_id}")
        self.assertIn("sergeev.demo@example.ru", card.text)

    def test_attention_column_has_explanation(self) -> None:
        self._rfq("ООО Тест <a@b.ru>", "1. монтаж кабель-канала — 100 м")
        page = self.client.get("/ui")
        self.assertIn("похоже, но не точно", page.text)  # title у числа
        self.assertIn("Проверить", page.text)

    def test_statuses_speak_about_quote(self) -> None:
        self._rfq("ООО Статус <s@b.ru>")
        page = self.client.get("/ui")
        self.assertIn("КП готово", page.text)
        card = self.client.get(f"/ui/rfq/{store.list_rfqs(1)[0]['id']}")
        self.assertIn("Путь заявки", card.text)
        self.assertIn("КП отправлено", card.text)

    def test_dropzone_and_script_are_served(self) -> None:
        page = self.client.get("/ui/prices")
        self.assertIn("data-dropzone", page.text)
        script = self.client.get("/static/app.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("dropzone", script.text)


class OneCFromUiTest(unittest.TestCase):
    class FakeClient:
        def prices(self):
            return [{"sku": "KR-001", "name": "Уголок", "unit": "шт", "price": "99"}]

        def stock_auto(self):
            return [("KR-001", "5", "шт", "Основной склад")]

    def setUp(self) -> None:
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/ui/login", data={"password": settings.ui_password})
        session = self.client.cookies.get(SESSION_COOKIE)
        self.csrf = csrf_for(settings.session_secret, session)

    def test_prices_from_1c_updates_price_list(self) -> None:
        old_url, settings.one_c_base_url = settings.one_c_base_url, "https://1c.example"
        with patch("kpbot.web.client_from_settings", return_value=OneCFromUiTest.FakeClient()):
            response = self.client.post("/ui/prices/from-1c", data={"csrf_token": self.csrf})
        settings.one_c_base_url = old_url
        self.assertEqual(response.status_code, 303)
        self.assertIn("1%D0%A1", response.headers["location"])  # «1С» в flash
        prices = {item.sku: item.price for item in store.price_items()}
        self.assertEqual(str(prices["KR-001"]), "99")

    def test_stock_from_1c_updates_stock(self) -> None:
        old_url, settings.one_c_base_url = settings.one_c_base_url, "https://1c.example"
        with patch("kpbot.web.client_from_settings", return_value=OneCFromUiTest.FakeClient()):
            response = self.client.post("/ui/prices/stock-1c", data={"csrf_token": self.csrf})
        settings.one_c_base_url = old_url
        self.assertEqual(response.status_code, 303)
        self.assertEqual(store.stock_count(), 1)
        self.assertEqual(store.stock_map()["KR-001"]["qty"], "5")


class SoftDeleteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(support.ROOT / "soft-delete.sqlite3")
        import uuid
        self.email = f"manager-{uuid.uuid4().hex[:8]}@test.ru"
        self.user = self.store.create_user(email=self.email, password="password1")

    def test_blocked_user_cannot_login_and_disappears(self) -> None:
        self.store.delete_user(self.user.id)
        self.assertIsNone(self.store.authenticate(self.email, "password1"))
        self.assertIsNone(self.store.user_by_id(self.user.id))
        self.assertNotIn(self.email, [user.email for user in self.store.users()])

    def test_blocked_admin_is_not_counted(self) -> None:
        admin = self.store.create_user(email="admin2@test.ru", password="password1", role="admin")
        before = self.store.admin_count()
        self.store.delete_user(admin.id)
        self.assertEqual(self.store.admin_count(), before - 1)


if __name__ == "__main__":
    unittest.main()
