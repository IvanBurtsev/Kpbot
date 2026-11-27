"""Ключи API на клиента: один ключ открывает одну базу, а не все."""

import support  # общий стенд

import os
import unittest
import uuid
from pathlib import Path

os.environ["KPBOT_API_KEY"] = "test-key"
os.environ["KPBOT_UI_PASSWORD"] = "test-pass"
os.environ["KPBOT_SESSION_SECRET"] = "test-secret"

from fastapi.testclient import TestClient  # noqa: E402

from kpbot.store import Store  # noqa: E402
from kpbot.web import app, registry, settings  # noqa: E402

PAYLOAD = {
    "sender": "client@example.ru",
    "subject": "Заявка по ключу",
    "text": "уголок 50х50 — 3 шт",
}


class TenantKeyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.code = "t" + uuid.uuid4().hex[:10]
        self.tenant = registry.create(
            "Ключ-тест", settings.tenants_dir / f"{self.code}.sqlite3", tenant_id=self.code
        )
        Store(Path(self.tenant["db_path"])).init_schema()
        self.client = TestClient(app, follow_redirects=False)

    def test_key_opens_only_own_tenant(self) -> None:
        key = registry.issue_key(self.code)
        created = self.client.post(
            "/api/v1/rfqs",
            json=PAYLOAD,
            headers={"X-API-Key": key, "X-Tenant": self.code},
        )
        self.assertEqual(created.status_code, 201, created.text)
        tenant_store = Store(Path(self.tenant["db_path"]))
        self.assertEqual(tenant_store.stats()["rfqs"], 1)

    def test_key_without_tenant_header_is_rejected(self) -> None:
        key = registry.issue_key(self.code)
        response = self.client.get("/api/v1/stats", headers={"X-API-Key": key})
        self.assertEqual(response.status_code, 401)

    def test_key_of_other_tenant_is_rejected(self) -> None:
        key = registry.issue_key(self.code)
        response = self.client.get(
            "/api/v1/stats", headers={"X-API-Key": key, "X-Tenant": "default"}
        )
        self.assertEqual(response.status_code, 401)

    def test_owner_key_still_works(self) -> None:
        response = self.client.get("/api/v1/stats", headers={"X-API-Key": settings.api_key})
        self.assertEqual(response.status_code, 200)

    def test_owner_key_does_not_open_tenant_cabinet_api(self) -> None:
        """Ключ владельца не должен читать кабинет чужого клиента через X-Tenant."""
        response = self.client.get(
            "/cabinet/api/1c/stock",
            headers={"X-API-Key": settings.api_key, "X-Tenant": self.code},
        )
        self.assertEqual(response.status_code, 401, response.text)
        own_key = registry.issue_key(self.code)
        allowed = self.client.get(
            "/cabinet/api/1c/stock",
            headers={"X-API-Key": own_key, "X-Tenant": self.code},
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)

    def test_reissued_key_replaces_old(self) -> None:
        old = registry.issue_key(self.code)
        registry.issue_key(self.code)
        self.assertFalse(registry.verify_key(self.code, old))
        self.assertTrue(registry.has_key(self.code))


if __name__ == "__main__":
    unittest.main()
