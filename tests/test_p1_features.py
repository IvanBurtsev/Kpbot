"""Функции, которые были заявлены, но работали хуже: правки, журнал, очередь."""

import support  # общий стенд

import os
import time
import unittest
from decimal import Decimal
from unittest.mock import patch

os.environ["KPBOT_API_KEY"] = "test-key"
os.environ["KPBOT_UI_PASSWORD"] = "test-pass"
os.environ["KPBOT_SESSION_SECRET"] = "test-secret"

from kpbot.config import settings  # noqa: E402
from kpbot.domain import LineStatus, RfqLine, ValidationError  # noqa: E402
from kpbot.pipeline import Pipeline  # noqa: E402
from kpbot.store import Store  # noqa: E402
from kpbot.users import User  # noqa: E402
from kpbot.web import actor_of  # noqa: E402


def _lines(store: Store, rfq_id: str) -> list[RfqLine]:
    return [
        RfqLine(
            idx=row["idx"],
            raw_text=row["raw_text"],
            qty=Decimal(row["qty"]) if row["qty"] else None,
            unit=row["unit"],
            matched_sku=row["matched_sku"],
            matched_name=row["matched_name"],
            status=LineStatus(row["status"]),
        )
        for row in store.get_rfq(rfq_id)["lines"]
    ]


class EditSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(support.ROOT / f"edit-{id(self)}.sqlite3")
        self.pipeline = Pipeline(self.store, settings)
        self.rfq_id = self.pipeline.process_text(
            "уголок 50х50 — 10 шт", sender="a@b.ru", subject="Заявка", message_id=f"edit-{id(self)}"
        ).rfq_id

    def test_duplicate_sku_is_rejected(self) -> None:
        lines = _lines(self.store, self.rfq_id)
        duplicate = RfqLine(
            idx=2,
            raw_text="уголок 50х50",
            qty=Decimal("5"),
            unit="шт",
            matched_sku=lines[0].matched_sku,
            matched_name=lines[0].matched_name,
            status=LineStatus.OK,
        )
        total_before = self.store.get_quote(self.rfq_id)["total"]
        with self.assertRaises(ValidationError):
            self.pipeline.apply_edits(self.rfq_id, lines + [duplicate], actor="test")
        self.assertEqual(len(self.store.get_rfq(self.rfq_id)["lines"]), 1)
        self.assertEqual(self.store.get_quote(self.rfq_id)["total"], total_before)

    def test_failed_edit_keeps_database_unchanged(self) -> None:
        lines = _lines(self.store, self.rfq_id)
        lines[0].qty = Decimal("2000000")  # больше max_qty
        total_before = self.store.get_quote(self.rfq_id)["total"]
        with self.assertRaises(ValidationError):
            self.pipeline.apply_edits(self.rfq_id, lines, actor="test")
        stored = _lines(self.store, self.rfq_id)
        self.assertEqual(str(stored[0].qty), "10.000")
        self.assertEqual(self.store.get_quote(self.rfq_id)["total"], total_before)


class InboxFiltersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(support.ROOT / f"inbox-{id(self)}.sqlite3")
        self.pipeline = Pipeline(self.store, settings)
        for index, subject in enumerate(("Заявка на уголок", "Заявка на кабель", "Отказ по заявке")):
            self.pipeline.process_text(
                "уголок 50х50 — 10 шт",
                sender=f"client{index}@example.ru",
                subject=subject,
                message_id=f"inbox-{id(self)}-{index}",
            )

    def test_query_filters_by_subject(self) -> None:
        rows = self.store.list_rfqs(50, query="кабель")
        self.assertEqual(len(rows), 1)
        self.assertIn("кабель", rows[0]["subject"])

    def test_query_filters_by_sender(self) -> None:
        rows = self.store.list_rfqs(50, query="client1@")
        self.assertEqual(len(rows), 1)

    def test_offset_paginates(self) -> None:
        first = self.store.list_rfqs(2, offset=0)
        second = self.store.list_rfqs(2, offset=2)
        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first[0]["id"], second[0]["id"])

    def test_issues_filter(self) -> None:
        self.assertEqual(self.store.list_rfqs(50, status="issues"), [])


class ActorTest(unittest.TestCase):
    def test_user_is_recorded_instead_of_ip(self) -> None:
        user = User(id="u-1", email="director@example.ru", role="admin")
        with patch("kpbot.web.current_user", return_value=user):
            self.assertEqual(actor_of(object()), "user:director@example.ru")

    def test_fallback_keeps_ip(self) -> None:
        with patch("kpbot.web.current_user", return_value=None):
            self.assertTrue(actor_of(object()).startswith("ui:"))


class SessionRevocationTest(unittest.TestCase):
    def test_password_change_invalidates_old_cookie(self) -> None:
        import uuid

        from fastapi.testclient import TestClient

        from kpbot.web import app
        from kpbot.web import store as web_store

        email = f"revoke-{uuid.uuid4().hex[:8]}@test.ru"
        user = web_store.create_user(email=email, password="old-password-1", role="manager")

        client = TestClient(app, follow_redirects=False)
        login = client.post("/ui/login", data={"email": email, "password": "old-password-1"})
        self.assertEqual(login.status_code, 303)
        self.assertEqual(client.get("/ui").status_code, 200)

        # В токене время с точностью до секунды, поэтому ждём смену секунды:
        # иначе «вход и смена пароля в одну секунду» — законный допуск.
        time.sleep(1.1)

        # админ меняет пароль — старые cookie этого человека должны умереть
        web_store.set_user_password(user.id, "new-password-2")
        self.assertEqual(client.get("/ui").status_code, 303)

        fresh = TestClient(app, follow_redirects=False)
        again = fresh.post("/ui/login", data={"email": email, "password": "new-password-2"})
        self.assertEqual(again.status_code, 303)


if __name__ == "__main__":
    unittest.main()
