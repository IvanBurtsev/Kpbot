"""Конечный автомат заявки: запрещённые переходы не проходят."""

import support  # общий стенд: изолированные данные, сеть запрещена

import shutil
import unittest

from fastapi.testclient import TestClient

from kpbot.domain import RfqStatus, ValidationError, check_transition
from kpbot.store import Store
from kpbot.web import app, pipeline, settings

TMP = support.subdir("status-machine")


class TransitionRulesTest(unittest.TestCase):
    def test_allowed(self) -> None:
        check_transition(RfqStatus.DRAFT_READY, RfqStatus.APPROVED)
        check_transition(RfqStatus.APPROVED, RfqStatus.SENT)
        check_transition(RfqStatus.REJECTED, RfqStatus.HUMAN_REVIEW)
        check_transition(RfqStatus.SENT, RfqStatus.SENT)  # повтор — не ошибка

    def test_forbidden(self) -> None:
        for current, new in (
            (RfqStatus.SENT, RfqStatus.APPROVED),
            (RfqStatus.SENT, RfqStatus.REJECTED),
            (RfqStatus.DRAFT_READY, RfqStatus.SENT),
            (RfqStatus.RECEIVED, RfqStatus.APPROVED),
        ):
            with self.assertRaises(ValidationError, msg=f"{current} -> {new}"):
                check_transition(current, new)

    def test_unknown_status(self) -> None:
        with self.assertRaises(ValidationError):
            check_transition("draft_ready", "телепорт")


class StoreTransitionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.data_dir = TMP / "store"
        shutil.rmtree(self.data_dir, ignore_errors=True)
        self.data_dir.mkdir(parents=True)
        self.store = Store(self.data_dir / "kpbot.sqlite3")
        self.rfq_id = "a" * 32
        self.store.save_rfq(
            rfq_id=self.rfq_id,
            message_id="<status-test@example.ru>",
            sender="a@b.ru",
            subject="Заявка",
            received_at="2026-01-01T00:00:00+00:00",
            status="draft_ready",
            raw_text="уголок 50х50 — 1 шт",
            lines=[],
        )

    def test_store_enforces_transitions(self) -> None:
        self.store.set_status(self.rfq_id, RfqStatus.APPROVED.value)
        with self.assertRaises(ValidationError):
            self.store.set_status(self.rfq_id, RfqStatus.RECEIVED.value)

        self.store.set_status(self.rfq_id, RfqStatus.SENT.value)
        with self.assertRaises(ValidationError):
            self.store.set_status(self.rfq_id, RfqStatus.APPROVED.value)

        # Служебный путь может исправить разошедшийся статус.
        self.store.set_status(self.rfq_id, RfqStatus.APPROVED.value, force=True)
        self.assertEqual(self.store.get_rfq(self.rfq_id)["status"], "approved")

    def test_unknown_status_value_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.set_status(self.rfq_id, "почти_отправлено")


class ApiConflictTest(unittest.TestCase):
    def test_second_approve_is_409(self) -> None:
        result = pipeline.process_text("уголок 50х50 — 2 шт", sender="a@b.ru", subject="Заявка")
        headers = {"X-API-Key": settings.api_key}
        client = TestClient(app, follow_redirects=False)
        first = client.post(f"/api/v1/rfqs/{result.rfq_id}/approve", headers=headers)
        self.assertEqual(first.status_code, 200)
        second = client.post(f"/api/v1/rfqs/{result.rfq_id}/approve", headers=headers)
        self.assertEqual(second.status_code, 409)
        self.assertIn("нельзя перевести", second.json()["detail"])


if __name__ == "__main__":
    unittest.main()
