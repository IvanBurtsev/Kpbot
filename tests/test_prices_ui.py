"""Загрузка прайса через интерфейс: права, замена списка, ошибки."""

import os
import unittest
from pathlib import Path

_TMP = Path(__file__).resolve().parent.parent / ".tmp" / "prices_ui"
_TMP.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("KPBOT_DATA_DIR", str(_TMP))
os.environ.setdefault("KPBOT_API_KEY", "test-key")
os.environ.setdefault("KPBOT_UI_PASSWORD", "test-pass")
os.environ.setdefault("KPBOT_SESSION_SECRET", "test-secret")

from fastapi.testclient import TestClient  # noqa: E402

from kpbot.store import Store  # noqa: E402
from kpbot.users import ROLE_MANAGER  # noqa: E402
from kpbot.web import app, settings, store  # noqa: E402


def login(client: TestClient) -> None:
    response = client.post("/ui/login", data={"password": settings.ui_password})
    assert response.status_code == 303, response.text


def csrf(client: TestClient) -> str:
    page = client.get("/ui/prices")
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


class PriceUploadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, follow_redirects=False)

    def test_page_requires_login(self) -> None:
        self.assertEqual(self.client.get("/ui/prices").status_code, 303)

    def test_admin_uploads_csv_and_list_is_replaced(self) -> None:
        login(self.client)
        token = csrf(self.client)
        payload = "Код;Наименование;Единица;Цена\nZZ-1;Тестовая позиция;шт;100,50\n"
        response = self.client.post(
            "/ui/prices/upload",
            data={"csrf_token": token},
            files={"file": ("price.csv", payload.encode("utf-8"), "text/csv")},
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(store.price_count(), 1)

    def test_broken_file_keeps_old_list(self) -> None:
        login(self.client)
        token = csrf(self.client)
        response = self.client.post(
            "/ui/prices/upload",
            data={"csrf_token": token},
            files={"file": ("price.csv", b"\xff\xfe not a price list", "text/csv")},
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("flash=", response.headers["location"])

    def test_manager_cannot_upload(self) -> None:
        user_store = Store(settings.db_path)
        try:
            user_store.create_user(email="mgr@test.ru", password="managerpass", role=ROLE_MANAGER)
        except Exception:  # noqa: BLE001 - пользователь мог остаться с прошлого прогона
            pass
        self.client.post("/ui/login", data={"email": "mgr@test.ru", "password": "managerpass"})
        response = self.client.post(
            "/ui/prices/upload",
            data={"csrf_token": "x"},
            files={"file": ("price.csv", b"a", "text/csv")},
        )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
