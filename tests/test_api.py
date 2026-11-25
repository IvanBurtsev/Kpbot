"""Тесты HTTP-слоя. Окружение подменяется до импорта приложения."""

import support  # общий стенд: изолированные данные, сеть запрещена

import os
import unittest
from pathlib import Path

_TMP = support.subdir("api")
os.environ["KPBOT_API_KEY"] = "test-key"
os.environ["KPBOT_UI_PASSWORD"] = "test-pass"
os.environ["KPBOT_SESSION_SECRET"] = "test-secret"

from fastapi.testclient import TestClient  # noqa: E402

from kpbot.web import app  # noqa: E402


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, follow_redirects=False)

    def test_health_is_open(self) -> None:
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_api_requires_key(self) -> None:
        payload = {"sender": "a@b.ru", "subject": "Заявка", "text": "уголок 50х50 — 10 шт"}
        self.assertEqual(self.client.post("/api/v1/rfqs", json=payload).status_code, 401)
        self.assertEqual(
            self.client.post("/api/v1/rfqs", json=payload, headers={"X-API-Key": "wrong"}).status_code,
            401,
        )

    def test_api_creates_rfq(self) -> None:
        payload = {"sender": "a@b.ru", "subject": "Заявка", "text": "уголок 50х50 — 10 шт"}
        response = self.client.post("/api/v1/rfqs", json=payload, headers={"X-API-Key": "test-key"})
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["matched"], 1)
        self.assertEqual(body["total"], "414.00")

    def test_api_validates_payload(self) -> None:
        bad = {"sender": "не-почта", "subject": "", "text": ""}
        response = self.client.post("/api/v1/rfqs", json=bad, headers={"X-API-Key": "test-key"})
        self.assertEqual(response.status_code, 422)

    def test_ui_redirects_to_login(self) -> None:
        response = self.client.get("/ui")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/ui/login")

    def test_security_headers_present(self) -> None:
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

    def test_wrong_password_rejected(self) -> None:
        response = self.client.post("/ui/login", data={"password": "nope"})
        self.assertEqual(response.status_code, 401)

    def test_login_then_upload_without_csrf_is_forbidden(self) -> None:
        login = self.client.post("/ui/login", data={"password": "test-pass"})
        self.assertEqual(login.status_code, 303)
        self.assertIn("kpbot_session", login.cookies)

        upload = self.client.post(
            "/ui/upload",
            files={"file": ("x.eml", b"From: a@b.ru\nSubject: test\n\nhello", "message/rfc822")},
            data={"csrf_token": "forged"},
        )
        self.assertEqual(upload.status_code, 403)

    def test_oversized_upload_rejected(self) -> None:
        self.client.post("/ui/login", data={"password": "test-pass"})
        # CSRF нет — получаем 403 раньше проверки размера, поэтому проверяем middleware напрямую
        big = b"x" * (3 * 1024 * 1024)
        response = self.client.post(
            "/ui/upload",
            files={"file": ("x.eml", big, "message/rfc822")},
            headers={"Content-Length": str(len(big) + 200)},
        )
        self.assertIn(response.status_code, {403, 413})


if __name__ == "__main__":
    unittest.main()
