"""Админка: доступ только у администратора, защита последнего админа."""

import support  # общий стенд: изолированные данные, сеть запрещена

import os
import unittest
import uuid
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote

_TMP = support.subdir("admin_ui")
os.environ.setdefault("KPBOT_API_KEY", "test-key")
os.environ.setdefault("KPBOT_UI_PASSWORD", "test-pass")
os.environ.setdefault("KPBOT_SESSION_SECRET", "test-secret")

from fastapi.testclient import TestClient  # noqa: E402

from kpbot.users import ROLE_ADMIN, ROLE_MANAGER  # noqa: E402
from kpbot.web import app, pipeline, settings, store  # noqa: E402


def csrf_from(client: TestClient, url: str = "/ui/admin/users") -> str:
    page = client.get(url)
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


class AdminPanelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/ui/login", data={"password": settings.ui_password})
        store.ensure_default_admin(settings.ui_password)

    def test_dashboard_opens_for_admin(self) -> None:
        response = self.client.get("/ui/admin")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Админка", response.text)

    def test_manager_cannot_open_admin(self) -> None:
        try:
            store.create_user(email="mgr-admin-test@test.ru", password="managerpass", role=ROLE_MANAGER)
        except Exception:  # noqa: BLE001 - остался с прошлого прогона
            pass
        manager = TestClient(app, follow_redirects=False)
        manager.post("/ui/login", data={"email": "mgr-admin-test@test.ru", "password": "managerpass"})
        self.assertEqual(manager.get("/ui/admin").status_code, 403)
        self.assertEqual(manager.get("/ui/admin/users").status_code, 403)
        self.assertEqual(manager.get("/ui/admin/events").status_code, 403)

    def test_create_user_and_see_it_in_list(self) -> None:
        token = csrf_from(self.client)
        response = self.client.post(
            "/ui/admin/users",
            data={"email": "new-manager@test.ru", "name": "Новый", "password": "strongpass1", "role": "manager",
                  "csrf_token": token},
        )
        self.assertEqual(response.status_code, 303)
        page = self.client.get("/ui/admin/users")
        self.assertIn("new-manager@test.ru", page.text)

    def test_cannot_delete_last_admin(self) -> None:
        admins = [u for u in store.users() if u.role == ROLE_ADMIN]
        target = admins[0]
        token = csrf_from(self.client)
        response = self.client.post(
            f"/ui/admin/users/{target.id}/delete", data={"csrf_token": token}
        )
        self.assertEqual(response.status_code, 303)
        self.assertIsNotNone(store.user_by_id(target.id))

    def test_password_reset_changes_login(self) -> None:
        try:
            user = store.create_user(email="reset-me@test.ru", password="oldpassword", role=ROLE_MANAGER)
        except Exception:  # noqa: BLE001
            user = next(u for u in store.users() if u.email == "reset-me@test.ru")
        token = csrf_from(self.client)
        self.client.post(
            f"/ui/admin/users/{user.id}/password",
            data={"password": "newpassword1", "csrf_token": token},
        )
        fresh = TestClient(app, follow_redirects=False)
        response = fresh.post("/ui/login", data={"email": "reset-me@test.ru", "password": "newpassword1"})
        self.assertEqual(response.status_code, 303)

    def test_manager_cannot_approve_above_limit(self) -> None:
        email = f"limit-{uuid.uuid4().hex[:6]}@test.ru"
        store.create_user(email=email, password="managerpass", role=ROLE_MANAGER)
        manager = TestClient(app, follow_redirects=False)
        self.assertEqual(
            manager.post("/ui/login", data={"email": email, "password": "managerpass"}).status_code,
            303,
        )
        rfq = pipeline.process_text(
            "уголок 50х50 — 10 шт", sender="limit@test.ru", subject="Лимит"
        )
        token = csrf_from(manager, f"/ui/rfq/{rfq.rfq_id}")
        old_limit = settings.approval_limit
        settings.approval_limit = Decimal("100.00")
        try:
            response = manager.post(
                f"/ui/rfq/{rfq.rfq_id}/approve", data={"csrf_token": token}
            )
        finally:
            settings.approval_limit = old_limit
        self.assertEqual(response.status_code, 303)
        self.assertIn("лимит", unquote(response.headers["location"]))
        self.assertNotEqual(store.get_rfq(rfq.rfq_id)["status"], "approved")

    def test_events_page_shows_actions(self) -> None:
        store.log_event("test_event", "unittest", "проверка журнала")
        page = self.client.get("/ui/admin/events")
        self.assertIn("test_event", page.text)

    def test_destructive_forms_confirm_without_inline_script(self) -> None:
        # CSP запрещает inline-обработчики: onsubmit="confirm(...)" не выполнялся,
        # и «Заблокировать» срабатывало сразу по клику.
        page = self.client.get("/ui/admin/users")
        self.assertIn('data-confirm="', page.text)
        self.assertNotIn("onsubmit=", page.text)
        self.assertNotIn("onclick=", page.text)
        csp = page.headers.get("content-security-policy", "")
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-inline", csp.split("script-src")[-1].split(";")[0])

    def test_confirm_handler_is_shipped_in_app_js(self) -> None:
        script = self.client.get("/static/app.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("data-confirm", script.text)


if __name__ == "__main__":
    unittest.main()
