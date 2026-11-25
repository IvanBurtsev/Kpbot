"""Дымовые проверки страниц: всё открывается, ничего не сыплется.

Ловят то, что раньше видели только глазами: 500 в шаблоне, незакрытые
переменные Jinja, пропавшую статику, доступ без авторизации.
"""

import os
import unittest
from pathlib import Path

_TMP = Path(__file__).resolve().parent.parent / ".tmp" / "pages"
_TMP.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("KPBOT_DATA_DIR", str(_TMP))
os.environ.setdefault("KPBOT_API_KEY", "test-key")
os.environ.setdefault("KPBOT_UI_PASSWORD", "test-pass")
os.environ.setdefault("KPBOT_SESSION_SECRET", "test-secret")
os.environ.setdefault("KPBOT_PDF_ENABLED", "false")
os.environ.setdefault("KPBOT_ENV", "dev")

from fastapi.testclient import TestClient  # noqa: E402

from kpbot.web import app, pipeline, settings, store  # noqa: E402

# страницы, которые обязан видеть администратор
ADMIN_PAGES = [
    "/ui",
    "/ui/prices",
    "/ui/setup",
    "/ui/settings",
    "/ui/stats",
    "/ui/admin",
    "/ui/admin?tab=users",
    "/ui/admin?tab=learned",
    "/ui/admin?tab=events",
    "/ui/admin?tab=company",
    "/ui/admin/users",
    "/ui/admin/events",
    "/ui/admin/learned",
]


class PageSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.store = store
        cls.pipeline = pipeline
        cls.rfq_id = pipeline.process_text(
            "уголок 50х50 — 10 шт", sender="smoke@example.ru", subject="Дымовой тест"
        ).rfq_id

    def setUp(self) -> None:
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/ui/login", data={"password": settings.ui_password})

    def test_admin_pages_render(self) -> None:
        for path in ADMIN_PAGES:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, f"{path} → {response.status_code}")

    def test_pages_have_no_unrendered_template(self) -> None:
        for path in ADMIN_PAGES:
            body = self.client.get(path).text
            self.assertNotIn("{{", body, f"незакрытая переменная на {path}")
            self.assertNotIn("{%", body, f"незакрытый тег на {path}")
            self.assertNotIn("Traceback", body, f"трейсбек на {path}")

    def test_rfq_page_and_artifacts(self) -> None:
        page = self.client.get(f"/ui/rfq/{self.rfq_id}")
        self.assertEqual(page.status_code, 200)
        for marker in ("Письмо клиента", "Что понял сервис", "Расчёт", "Отправить клиенту"):
            self.assertIn(marker, page.text, marker)

        for suffix in ("kp.html", "kp.docx", "export.xlsx"):
            response = self.client.get(f"/ui/rfq/{self.rfq_id}/{suffix}")
            self.assertEqual(response.status_code, 200, suffix)
            self.assertGreater(len(response.content), 500, suffix)

        # PDF выключен в тестах — должен честно отвечать 404, а не падать
        self.assertEqual(self.client.get(f"/ui/rfq/{self.rfq_id}/kp.pdf").status_code, 404)

    def test_static_and_health(self) -> None:
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        css = self.client.get("/static/app.css")
        self.assertEqual(css.status_code, 200)
        self.assertIn("--accent", css.text)
        self.assertIn("@media", css.text)

    def test_auth_is_enforced(self) -> None:
        anonymous = TestClient(app, follow_redirects=False)
        for path in ("/ui", "/ui/prices", "/ui/setup", "/ui/admin", f"/ui/rfq/{self.rfq_id}"):
            response = anonymous.get(path)
            self.assertIn(response.status_code, (303, 401, 403), path)

    def test_manager_sees_only_allowed_pages(self) -> None:
        try:
            store.create_user(email="smoke-manager@test.ru", password="managerpass", role="manager")
        except Exception:  # noqa: BLE001 - мог остаться с прошлого прогона
            pass
        manager = TestClient(app, follow_redirects=False)
        manager.post("/ui/login", data={"email": "smoke-manager@test.ru", "password": "managerpass"})
        self.assertEqual(manager.get("/ui").status_code, 200)
        self.assertEqual(manager.get("/ui/prices").status_code, 200)
        for path in ("/ui/admin", "/ui/setup", "/ui/settings"):
            self.assertEqual(manager.get(path).status_code, 403, path)

    def test_unknown_paths_are_404(self) -> None:
        for path in ("/ui/nope", "/ui/rfq/" + "f" * 32, "/nope"):
            self.assertEqual(self.client.get(path).status_code, 404, path)


if __name__ == "__main__":
    unittest.main()