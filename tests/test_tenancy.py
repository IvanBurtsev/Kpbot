"""Мультитенантность: у каждого клиента своя база, данные не пересекаются."""

import support  # общий стенд: изолированные данные, сеть запрещена

import os
import shutil
import unittest
from pathlib import Path

_TMP = support.subdir("tenancy")
os.environ.setdefault("KPBOT_API_KEY", "test-key")
os.environ.setdefault("KPBOT_UI_PASSWORD", "test-pass")
os.environ.setdefault("KPBOT_SESSION_SECRET", "test-secret")
os.environ.setdefault("KPBOT_PDF_ENABLED", "false")

from fastapi.testclient import TestClient  # noqa: E402

from kpbot.store import Store  # noqa: E402
from kpbot.tenancy import TenantRegistry  # noqa: E402
from kpbot.web import app, settings  # noqa: E402


def csrf(client: TestClient, url: str) -> str:
    page = client.get(url)
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


class TenantRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        # каждый тест — чистый реестр и своя база клиента
        import uuid

        run = uuid.uuid4().hex[:8]
        self.main_db = _TMP / f"main-{run}.sqlite3"
        self.registry = TenantRegistry(_TMP / f"registry-{run}.sqlite3", self.main_db)

    def test_default_tenant_is_created_once(self) -> None:
        first = self.registry.ensure_default()
        second = self.registry.ensure_default()
        self.assertEqual(first, second)
        main = [t for t in self.registry.list() if t["db_path"] == str(self.main_db)]
        self.assertEqual(len(main), 1, "основная база должна быть ровно одним клиентом")
        self.assertEqual(main[0]["id"], "default")

    def test_create_and_find_by_login(self) -> None:
        path = _TMP / f"tenant-a-{self.registry.path.stem}.sqlite3"
        tenant = self.registry.create("Клиент А", path)
        store = Store(path)
        store.init_schema()
        store.create_user(email="boss@a.ru", password="strongpass1", role="admin")

        found = self.registry.find_by_login("BOSS@A.RU")
        self.assertEqual([t["id"] for t in found], [tenant["id"]])
        self.assertEqual(self.registry.find_by_login("никого@нет.ру"), [])

    def test_status_toggle(self) -> None:
        tenant = self.registry.create("Клиент Б", _TMP / f"tenant-b-{self.registry.path.stem}.sqlite3")
        self.registry.set_status(tenant["id"], "paused")
        self.assertEqual(self.registry.get(tenant["id"])["status"], "paused")
        with self.assertRaises(ValueError):
            self.registry.set_status(tenant["id"], "неверный")


class IsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(_TMP / "tenants", ignore_errors=True)
        shutil.rmtree(_TMP / "data", ignore_errors=True)
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/ui/login", data={"password": settings.ui_password})

    def test_created_tenant_has_own_base_and_user(self) -> None:
        token = csrf(self.client, "/ui/ops")
        response = self.client.post(
            "/ui/ops/create",
            data={"csrf_token": token, "name": "ООО «Второй»", "email": "boss@second.ru"},
        )
        # пароль отдаётся страницей, а не редиректом с паролем в адресе
        self.assertEqual(response.status_code, 200)

        from kpbot.web import registry

        tenants = [t for t in registry.list() if t["name"] == "ООО «Второй»"]
        self.assertEqual(len(tenants), 1)
        store = Store(Path(tenants[0]["db_path"]))
        self.assertIsNotNone(store.authenticate("boss@second.ru", self._password_from_flash(response)))

    def _password_from_flash(self, response) -> str:
        import re as _re

        found = _re.search(r"пароль\s+(\S+)", response.text)
        self.assertIsNotNone(found, "пароль не показан в ответе")
        return found.group(1)

    def test_switch_changes_visible_data(self) -> None:
        from kpbot.web import pipeline, registry

        # в основной базе создаём заявку
        default_rfq = pipeline.process_text("уголок 50х50 — 10 шт", sender="a@b.ru", subject="Основная база")
        self.assertTrue(default_rfq.rfq_id)

        token = csrf(self.client, "/ui/ops")
        self.client.post(
            "/ui/ops/create",
            data={"csrf_token": token, "name": "ООО «Изолированный»", "email": "boss@iso.ru"},
        )
        tenant = [t for t in registry.list() if t["name"] == "ООО «Изолированный»"][0]

        token = csrf(self.client, "/ui/ops")
        switched = self.client.post(
            "/ui/ops/switch", data={"csrf_token": token, "tenant_id": tenant["id"]}
        )
        self.assertEqual(switched.status_code, 303)

        # в базе клиента заявок нет — чужие данные не видны
        page = self.client.get("/ui")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("Основная база", page.text)

    def test_manager_cannot_open_ops(self) -> None:
        from kpbot.web import store

        try:
            store.create_user(email="mgr-ops@test.ru", password="managerpass", role="manager")
        except Exception:  # noqa: BLE001
            pass
        manager = TestClient(app, follow_redirects=False)
        manager.post("/ui/login", data={"email": "mgr-ops@test.ru", "password": "managerpass"})
        self.assertEqual(manager.get("/ui/ops").status_code, 403)


if __name__ == "__main__":
    unittest.main()
