"""Брендинг КП: логотип и цвет компании доходят до документа."""

import base64
import os
import unittest
from pathlib import Path

_TMP = Path(__file__).resolve().parent.parent / ".tmp" / "branding"
_TMP.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("KPBOT_DATA_DIR", str(_TMP))
os.environ.setdefault("KPBOT_API_KEY", "test-key")
os.environ.setdefault("KPBOT_UI_PASSWORD", "test-pass")
os.environ.setdefault("KPBOT_SESSION_SECRET", "test-secret")

from fastapi.testclient import TestClient  # noqa: E402

from kpbot.web import app, pipeline, settings, store  # noqa: E402

# минимальный PNG 1×1
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)


def csrf(client: TestClient, url: str = "/ui/settings") -> str:
    page = client.get(url)
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


class BrandingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/ui/login", data={"password": settings.ui_password})
        with store._connect() as conn, conn:
            conn.execute("DELETE FROM app_settings")

    def test_color_and_logo_reach_quote(self) -> None:
        token = csrf(self.client)
        response = self.client.post(
            "/ui/settings",
            data={"csrf_token": token, "name": "ООО «Бренд»", "color": "#c81e3a"},
            files={"logo": ("logo.png", PNG, "image/png")},
        )
        self.assertEqual(response.status_code, 303)

        company = pipeline.company()
        self.assertEqual(company["color"], "#c81e3a")
        self.assertTrue(company["logo"].startswith("data:image/png;base64,"))

        rfq = pipeline.process_text("уголок 50х50 — 10 шт", sender="a@b.ru", subject="Заявка")
        html = (settings.out_dir / f"{rfq.rfq_id}.html").read_text(encoding="utf-8")
        self.assertIn("#c81e3a", html)
        self.assertIn("data:image/png;base64,", html)
        self.assertIn("ООО «Бренд»", html)

    def test_bad_color_is_ignored(self) -> None:
        token = csrf(self.client)
        self.client.post(
            "/ui/settings",
            data={"csrf_token": token, "name": "ООО «Тест»", "color": "javascript:alert(1)"},
        )
        self.assertEqual(pipeline.company()["color"], "")

    def test_oversized_logo_is_rejected(self) -> None:
        token = csrf(self.client)
        big = b"x" * (settings.max_logo_bytes + 100)
        self.client.post(
            "/ui/settings",
            data={"csrf_token": token, "name": "ООО «Тест»"},
            files={"logo": ("big.png", big, "image/png")},
        )
        self.assertEqual(pipeline.company()["logo"], "")

    def test_wizard_saves_brand_too(self) -> None:
        token = csrf(self.client, "/ui/setup")
        response = self.client.post(
            "/ui/setup/company",
            data={"csrf_token": token, "name": "ООО «Визард»", "color": "#0a7d4f"},
            files={"logo": ("logo.png", PNG, "image/png")},
        )
        self.assertEqual(response.status_code, 303)
        company = pipeline.company()
        self.assertEqual(company["color"], "#0a7d4f")
        self.assertTrue(company["logo"])


if __name__ == "__main__":
    unittest.main()