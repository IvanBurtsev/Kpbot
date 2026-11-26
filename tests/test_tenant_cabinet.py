"""Мультитенантность кабинета: вход по ссылке поставщика и своя база."""

import support  # общий стенд: изолированные данные, сеть запрещена

import unittest
import uuid

from fastapi.testclient import TestClient

from kpbot.cabinet_store import CabinetStore
from kpbot.store import Store
from kpbot.tenant_ctx import CLIENT_TENANT_COOKIE
from kpbot.web import app, registry, settings

TMP = support.subdir("tenant-cabinet")


class TenantCabinetTest(unittest.TestCase):
    def setUp(self) -> None:
        suffix = uuid.uuid4().hex[:8]
        self.path = TMP / f"tenant-{suffix}.sqlite3"
        self.tenant = registry.create(f"ООО Поставщик-{suffix}", self.path)
        self.company = CabinetStore(self.path).create_company(f"ООО Покупатель-{suffix}", inn="7700000000")
        self.password = "password1"
        self.user = CabinetStore(self.path).create_user(
            self.company["id"],
            email=f"client-{suffix}@test.ru",
            password=self.password,
            role="client_admin",
        )
        self.client = TestClient(app, follow_redirects=False)

    def test_login_by_supplier_link_uses_tenant_db(self) -> None:
        page = self.client.get(f"/cabinet/login?t={self.tenant['id']}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(self.tenant["name"], page.text)

        response = self.client.post(
            "/cabinet/login",
            data={
                "email": self.user["email"],
                "password": self.password,
                "tenant": self.tenant["id"],
            },
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn(CLIENT_TENANT_COOKIE, response.cookies)
        self.assertEqual(response.cookies[CLIENT_TENANT_COOKIE].count("."), 2)

        dashboard = self.client.get("/cabinet")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(self.company["name"], dashboard.text)

        # В основной базе такого покупателя нет — вход без кода не должен сработать.
        fresh = TestClient(app, follow_redirects=False)
        denied = fresh.post(
            "/cabinet/login",
            data={"email": self.user["email"], "password": self.password},
        )
        self.assertEqual(denied.status_code, 401)

    def test_unknown_tenant_code_is_404(self) -> None:
        page = self.client.get("/cabinet/login?t=zzzzzzzz")
        self.assertEqual(page.status_code, 404)
        self.assertIn("не найден", page.text)

    def test_logout_clears_client_tenant_cookie(self) -> None:
        self.client.post(
            "/cabinet/login",
            data={"email": self.user["email"], "password": self.password, "tenant": self.tenant["id"]},
        )
        self.assertIn(CLIENT_TENANT_COOKIE, self.client.cookies)
        response = self.client.post("/cabinet/logout")
        self.assertEqual(response.status_code, 303)
        self.assertNotIn(CLIENT_TENANT_COOKIE, self.client.cookies)

    def test_api_uses_tenant_header(self) -> None:
        Store(self.path).replace_price_items([])
        headers = {"X-API-Key": settings.api_key, "X-Tenant": self.tenant["id"]}
        response = self.client.get("/cabinet/api/1c/prices", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])
        self.assertEqual(
            self.client.get("/cabinet/api/1c/prices", headers={"X-API-Key": settings.api_key, "X-Tenant": "nope"}).status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
