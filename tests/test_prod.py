"""Прод-конфигурация и служебные эндпоинты."""

import os
import unittest
from pathlib import Path

_TMP = Path(__file__).resolve().parent.parent / ".tmp" / "prod"
_TMP.mkdir(parents=True, exist_ok=True)
os.environ["KPBOT_DATA_DIR"] = str(_TMP)
os.environ["KPBOT_PDF_ENABLED"] = "false"

from kpbot.config import Settings  # noqa: E402


class ProductionGuardTest(unittest.TestCase):
    def test_defaults_are_rejected_in_production(self) -> None:
        settings = Settings(
            env="prod",
            api_key="dev-api-key-change-me",
            ui_password="dev-password",
            session_secret="dev-session-secret-change-me",
            data_dir=_TMP,
        )
        problems = settings.production_problems()
        self.assertGreaterEqual(len(problems), 3)
        self.assertTrue(any("KPBOT_UI_PASSWORD" in text for text in problems))

    def test_strong_config_passes(self) -> None:
        settings = Settings(
            env="prod",
            api_key="a" * 32,
            ui_password="StrongPass!2026",
            session_secret="s" * 48,
            oauth_client_id="x.apps.googleusercontent.com",
            oauth_client_secret="GOCSPX-secret",
            imap_user="a@b.ru",
            public_base_url="https://kp.example.ru:8443",
            data_dir=_TMP,
        )
        self.assertEqual(settings.production_problems(), [])

    def test_short_secrets_are_rejected(self) -> None:
        settings = Settings(
            env="prod",
            api_key="short",
            ui_password="StrongPass!2026",
            session_secret="short",
            oauth_client_id="x",
            oauth_client_secret="y",
            data_dir=_TMP,
        )
        problems = settings.production_problems()
        self.assertTrue(any("24" in text for text in problems))
        self.assertTrue(any("32" in text for text in problems))

    def test_dev_env_does_not_require_anything(self) -> None:
        settings = Settings(env="dev", data_dir=_TMP)
        self.assertFalse(settings.is_production)
        # в dev проверка не запускается вообще
        self.assertGreater(len(settings.production_problems()), 0)


class HealthAndRequestIdTest(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from kpbot.web import app

        self.client = TestClient(app)

    def test_healthz_is_open(self) -> None:
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_request_id_header_present(self) -> None:
        response = self.client.get("/healthz")
        self.assertTrue(response.headers.get("X-Request-ID"))

    def test_request_id_is_reused_when_provided(self) -> None:
        response = self.client.get("/healthz", headers={"X-Request-ID": "abc123"})
        self.assertEqual(response.headers["X-Request-ID"], "abc123")


if __name__ == "__main__":
    unittest.main()