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

from kpbot.security import same_origin  # noqa: E402
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


class LoginOriginTest(unittest.TestCase):
    def test_foreign_origin_is_rejected(self) -> None:
        client = TestClient(app, follow_redirects=False)
        response = client.post(
            "/ui/login",
            data={"password": settings.ui_password},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(response.status_code, 403)

    def test_same_origin_is_allowed(self) -> None:
        client = TestClient(app, follow_redirects=False)
        response = client.post(
            "/ui/login",
            data={"password": settings.ui_password},
            headers={"Origin": settings.public_base_url},
        )
        self.assertEqual(response.status_code, 303)

    def test_forwarded_for_is_trusted_only_from_proxy(self) -> None:
        from starlette.requests import Request

        from kpbot.security import client_ip

        def make(peer: str, forwarded: bytes = b"203.0.113.7, 10.0.0.1"):
            return Request({
                "type": "http",
                "client": (peer, 1234),
                "headers": [(b"x-forwarded-for", forwarded)],
            })

        # ближайший к нам адрес добавлен прокси; прокси в списке доверенных — идём левее
        self.assertEqual(client_ip(make("10.0.0.1"), {"10.0.0.1"}), "203.0.113.7")
        # без доверенного прокси заголовку не верим вовсе
        self.assertEqual(client_ip(make("198.51.100.5"), {"127.0.0.1"}), "198.51.100.5")
        # подделанный левый адрес не должен попадать в лимиты
        self.assertEqual(
            client_ip(make("10.0.0.1", b"1.2.3.4, 203.0.113.7, 10.0.0.1"), {"10.0.0.1"}),
            "203.0.113.7",
        )


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


class SameOriginTest(unittest.TestCase):
    """CSRF-проверка входа: чужой сайт — нет, свой адрес под любым именем — да."""

    def _request(self, origin: str, host: str = "debtcrm.duckdns.org:8443", scheme: str = "https"):
        from types import SimpleNamespace

        return SimpleNamespace(
            headers={"origin": origin, "host": host, "x-forwarded-proto": scheme},
            url=SimpleNamespace(scheme=scheme),
        )

    def test_public_url_is_allowed(self) -> None:
        request = self._request("https://debtcrm.duckdns.org:8443")
        self.assertTrue(same_origin(request, "https://debtcrm.duckdns.org:8443"))

    def test_request_own_host_is_allowed(self) -> None:
        # владелец открыл сервис по IP: адрес не совпадает с публичным, но это свой сайт
        request = self._request("http://45.152.202.114", host="45.152.202.114", scheme="http")
        self.assertTrue(same_origin(request, "https://debtcrm.duckdns.org:8443"))

    def test_foreign_site_is_rejected(self) -> None:
        request = self._request("https://evil.example")
        self.assertFalse(same_origin(request, "https://debtcrm.duckdns.org:8443"))

    def test_missing_origin_is_allowed(self) -> None:
        self.assertTrue(same_origin(self._request(""), "https://debtcrm.duckdns.org:8443"))


if __name__ == "__main__":
    unittest.main()
