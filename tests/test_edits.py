"""Правка строк, экспорт в Excel и бэкап — глазами HTTP."""

import os
import shutil
import unittest
from decimal import Decimal
from pathlib import Path

from kpbot.config import BASE_DIR

_TMP = Path(__file__).resolve().parent.parent / ".tmp" / "edits"
_TMP.mkdir(parents=True, exist_ok=True)
os.environ["KPBOT_DATA_DIR"] = str(_TMP)
os.environ["KPBOT_API_KEY"] = "test-key"
os.environ["KPBOT_UI_PASSWORD"] = "test-pass"
os.environ["KPBOT_SESSION_SECRET"] = "test-secret"
os.environ["KPBOT_PDF_ENABLED"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from kpbot.domain import LineStatus  # noqa: E402
from kpbot.pipeline import Pipeline  # noqa: E402
from kpbot.security import SESSION_COOKIE, csrf_for  # noqa: E402
from kpbot.store import Store  # noqa: E402
from kpbot.web import app, settings, store  # noqa: E402


class EditFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/ui/login", data={"password": "test-pass"})
        self.session = self.client.cookies.get(SESSION_COOKIE)
        self.csrf = csrf_for(settings.session_secret, self.session)

    def _create_rfq(self, text: str = "подрозетник 68 мм — 20 шт") -> str:
        response = self.client.post(
            "/api/v1/rfqs",
            json={"sender": "a@b.ru", "subject": "Заявка", "text": text},
            headers={"X-API-Key": "test-key"},
        )
        self.assertIn(response.status_code, (200, 201))
        return response.json()["rfq_id"]

    def test_unknown_line_can_be_replaced_with_price_item(self) -> None:
        rfq_id = self._create_rfq()
        self.assertIn(
            store.get_rfq(rfq_id)["status"], {"human_review"}
        )

        response = self.client.post(
            f"/ui/rfq/{rfq_id}/lines/1/update",
            data={"csrf_token": self.csrf, "sku": "EL-009", "qty": "3"},
        )
        self.assertEqual(response.status_code, 303)

        rfq = store.get_rfq(rfq_id)
        self.assertEqual(rfq["status"], "draft_ready")
        quote = store.get_quote(rfq_id)
        # EL-009 = 315.00 × 3 = 945.00 + НДС 20%
        self.assertEqual(Decimal(quote["total"]), Decimal("1134.00"))

    def test_ambiguous_line_can_be_confirmed(self) -> None:
        # «подрозетник» — не розетка, но похоже: система должна спросить человека
        rfq_id = self._create_rfq("подрозетник 68 мм — 20 шт")
        lines = store.get_rfq(rfq_id)["lines"]
        self.assertEqual(lines[0]["status"], "ambiguous")
        self.assertTrue(lines[0]["matched_sku"])

        response = self.client.post(
            f"/ui/rfq/{rfq_id}/lines/1/confirm", data={"csrf_token": self.csrf}
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(store.get_rfq(rfq_id)["lines"][0]["status"], "ok")

    def test_quantity_can_be_changed(self) -> None:
        rfq_id = self._create_rfq("уголок 50х50 — 10 шт")
        self.client.post(
            f"/ui/rfq/{rfq_id}/lines/1/update",
            data={"csrf_token": self.csrf, "sku": "KR-001", "qty": "100"},
        )
        quote = store.get_quote(rfq_id)
        # 34.50 × 100 = 3450 + 20%
        self.assertEqual(Decimal(quote["total"]), Decimal("4140.00"))

    def test_invalid_quantity_is_rejected(self) -> None:
        rfq_id = self._create_rfq("уголок 50х50 — 10 шт")
        response = self.client.post(
            f"/ui/rfq/{rfq_id}/lines/1/update",
            data={"csrf_token": self.csrf, "sku": "KR-001", "qty": "-5"},
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("flash=", response.headers["location"])
        quote = store.get_quote(rfq_id)
        self.assertEqual(Decimal(quote["total"]), Decimal("414.00"))

    def test_line_can_be_deleted_and_added(self) -> None:
        rfq_id = self._create_rfq("уголок 50х50 — 10 шт\nкабель 3х2.5 — 5 м")
        self.assertEqual(len(store.get_rfq(rfq_id)["lines"]), 2)

        self.client.post(f"/ui/rfq/{rfq_id}/lines/1/delete", data={"csrf_token": self.csrf})
        self.assertEqual(len(store.get_rfq(rfq_id)["lines"]), 1)

        self.client.post(
            f"/ui/rfq/{rfq_id}/lines/add",
            data={"csrf_token": self.csrf, "sku": "MT-002", "qty": "2"},
        )
        rfq = store.get_rfq(rfq_id)
        self.assertEqual(len(rfq["lines"]), 2)
        self.assertIn("MT-002", [line["matched_sku"] for line in rfq["lines"]])

    def test_csrf_is_required_for_edits(self) -> None:
        rfq_id = self._create_rfq()
        response = self.client.post(
            f"/ui/rfq/{rfq_id}/lines/1/update",
            data={"csrf_token": "bad", "sku": "EL-009", "qty": "1"},
        )
        self.assertEqual(response.status_code, 403)

    def test_export_xlsx_returns_workbook(self) -> None:
        rfq_id = self._create_rfq("уголок 50х50 — 10 шт")
        response = self.client.get(f"/ui/rfq/{rfq_id}/export.xlsx")
        self.assertEqual(response.status_code, 200)
        self.assertIn("spreadsheetml", response.headers["content-type"])
        self.assertTrue(response.content.startswith(b"PK"))

    def test_sent_rfq_cannot_be_edited(self) -> None:
        rfq_id = self._create_rfq("уголок 50х50 — 10 шт")
        self.client.post(f"/ui/rfq/{rfq_id}/approve", data={"csrf_token": self.csrf})
        self.assertEqual(store.get_rfq(rfq_id)["status"], "sent")

        response = self.client.post(
            f"/ui/rfq/{rfq_id}/lines/1/update",
            data={"csrf_token": self.csrf, "sku": "KR-001", "qty": "1"},
        )
        self.assertEqual(response.status_code, 303)
        from urllib.parse import unquote

        self.assertIn("нельзя", unquote(response.headers["location"]))


class BackupTest(unittest.TestCase):
    def test_backup_creates_copy_and_rotates(self) -> None:
        from kpbot.cli import main

        backups = settings.data_dir / "backups"
        shutil.rmtree(backups, ignore_errors=True)

        for _ in range(3):
            self.assertEqual(main(["backup", "--keep", "2"]), 0)

        files = sorted(backups.glob("kpbot-*.sqlite3"))
        self.assertEqual(len(files), 2)
        self.assertTrue(all(path.stat().st_size > 0 for path in files))


if __name__ == "__main__":
    unittest.main()