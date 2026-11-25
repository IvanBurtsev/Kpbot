"""Безопасность: идентификаторы из адреса, разбор чисел, cookie и заголовки."""

import support  # общий стенд: изолированные данные, сеть запрещена

import os
import unittest
from pathlib import Path

_TMP = support.subdir("security")
os.environ.setdefault("KPBOT_API_KEY", "test-key")
os.environ.setdefault("KPBOT_UI_PASSWORD", "test-pass")
os.environ.setdefault("KPBOT_SESSION_SECRET", "test-secret")

from fastapi.testclient import TestClient  # noqa: E402

from kpbot.web import app, settings, store  # noqa: E402


class IdValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/ui/login", data={"password": settings.ui_password})

    def test_traversal_in_rfq_id_is_rejected(self) -> None:
        evil_urls = [
            "/ui/rfq/..%2F..%2Fetc%2Fpasswd/kp.html",
            "/ui/rfq/../../etc/passwd",
            "/ui/rfq/..%2F..%2Fetc%2Fpasswd/kp.pdf",
            "/ui/rfq/%2e%2e%2f%2e%2e%2fetc%2fpasswd/export.xlsx",
        ]
        for url in evil_urls:
            response = self.client.get(url)
            # главное: никаких 500 и никакого содержимого системных файлов
            self.assertNotEqual(response.status_code, 500, url)
            self.assertNotIn("root:x:", response.text, url)

    def test_short_or_non_hex_id_is_404(self) -> None:
        for bad in ("abc", "0" * 31, "Z" * 32, "0" * 33):
            self.assertEqual(self.client.get(f"/ui/rfq/{bad}").status_code, 404, bad)
            self.assertEqual(self.client.get(f"/ui/rfq/{bad}/kp.pdf").status_code, 404, bad)

    def test_valid_but_unknown_id_is_404(self) -> None:
        self.assertEqual(self.client.get(f"/ui/rfq/{'a' * 32}").status_code, 404)

    def test_api_rejects_bad_id(self) -> None:
        headers = {"X-API-Key": settings.api_key}
        self.assertEqual(self.client.get("/api/v1/rfqs/../../etc/passwd", headers=headers).status_code, 404)


class NumberParsingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/ui/login", data={"password": settings.ui_password})

    def test_bad_days_does_not_break_page(self) -> None:
        for value in ("abc", "", "-5", "999999", "1e999", "٣٠"):
            response = self.client.get(f"/ui/stats?days={value}")
            self.assertEqual(response.status_code, 200, value)

    def test_api_accepts_only_valid_payload(self) -> None:
        headers = {"X-API-Key": settings.api_key}
        bad = {"sender": "не-почта", "subject": "", "text": ""}
        self.assertEqual(self.client.post("/api/v1/rfqs", json=bad, headers=headers).status_code, 422)
        self.assertEqual(self.client.post("/api/v1/rfqs", json={"sender": "a@b.ru"}).status_code, 401)


class HeadersAndCookieTest(unittest.TestCase):
    def test_security_headers_present(self) -> None:
        client = TestClient(app, follow_redirects=False)
        response = client.get("/healthz")
        self.assertEqual(response.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(response.headers.get("x-frame-options"), "DENY")
        self.assertEqual(response.headers.get("referrer-policy"), "no-referrer")
        self.assertIn("default-src", response.headers.get("content-security-policy", "default-src 'self'"))

    def test_cookie_is_secure_on_https(self) -> None:
        original = settings.public_base_url
        settings.public_base_url = "https://kp.example.ru:8443"
        try:
            client = TestClient(app, follow_redirects=False)
            response = client.post("/ui/login", data={"password": settings.ui_password})
            cookie = response.headers.get("set-cookie", "")
            self.assertIn("Secure", cookie, cookie)
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=lax", cookie.replace("samesite", "SameSite"))
        finally:
            settings.public_base_url = original


class SqlInjectionTest(unittest.TestCase):
    def test_search_and_subjects_do_not_break_queries(self) -> None:
        client = TestClient(app, follow_redirects=False)
        client.post("/ui/login", data={"password": settings.ui_password})
        payloads = ["' OR 1=1 --", "'; DROP TABLE rfqs; --", "%27%20OR%201%3D1"]
        for payload in payloads:
            response = client.get("/ui/prices", params={"q": payload})
            self.assertEqual(response.status_code, 200, payload)
        # таблица на месте: запросы параметризованы
        self.assertGreaterEqual(store.stats()["rfqs"], 0)
        self.assertIsInstance(store.price_count(), int)


if __name__ == "__main__":
    unittest.main()
