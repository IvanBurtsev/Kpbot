"""Очередь отправки: письмо не теряется при сбое почты."""

import os
import unittest
from pathlib import Path
from urllib.parse import unquote

_TMP = Path(__file__).resolve().parent.parent / ".tmp" / "outbox"
_TMP.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("KPBOT_DATA_DIR", str(_TMP))
os.environ.setdefault("KPBOT_API_KEY", "test-key")
os.environ.setdefault("KPBOT_UI_PASSWORD", "test-pass")
os.environ.setdefault("KPBOT_SESSION_SECRET", "test-secret")

from fastapi.testclient import TestClient  # noqa: E402

from kpbot.web import app, pipeline, settings, store  # noqa: E402


def csrf(client: TestClient, url: str) -> str:
    page = client.get(url)
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


class OutboxTest(unittest.TestCase):
    def test_enqueue_and_mark_sent(self) -> None:
        before = store.outbox_stats()["pending"]
        message_id = store.enqueue_message(
            rfq_id="a" * 32, to_addr="client@example.ru", subject="Re: Заявка", body="текст", error="нет связи"
        )
        self.assertGreater(message_id, 0)
        self.assertEqual(store.outbox_stats()["pending"], before + 1)

        pending = [item for item in store.pending_messages(100) if item["id"] == message_id]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["to_addr"], "client@example.ru")
        self.assertEqual(pending[0]["attempts"], 1)

        store.mark_sent(message_id)
        self.assertFalse([item for item in store.pending_messages(100) if item["id"] == message_id])
        self.assertEqual(store.outbox_stats()["pending"], before)

    def test_failed_attempt_increases_counter(self) -> None:
        message_id = store.enqueue_message(
            rfq_id="b" * 32, to_addr="x@y.ru", subject="s", body="b", error="таймаут"
        )
        store.mark_attempt_failed(message_id, "снова таймаут")
        item = [i for i in store.pending_messages(100) if i["id"] == message_id][0]
        self.assertEqual(item["attempts"], 2)
        self.assertIn("таймаут", item["last_error"])
        store.mark_sent(message_id)

    def test_after_five_attempts_message_leaves_queue(self) -> None:
        message_id = store.enqueue_message(rfq_id="c" * 32, to_addr="z@z.ru", subject="s", body="b")
        for _ in range(4):
            store.mark_attempt_failed(message_id, "ошибка")
        self.assertFalse([i for i in store.pending_messages(100) if i["id"] == message_id])
        store.mark_sent(message_id)


class SendFailureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/ui/login", data={"password": settings.ui_password})
        self.rfq = pipeline.process_text("уголок 50х50 — 10 шт", sender="client@example.ru", subject="Заявка")

    def test_failed_send_lands_in_outbox(self) -> None:
        original_enabled = settings.smtp_enabled
        settings.smtp_enabled = False
        pending_before = store.outbox_stats()["pending"]
        try:
            token = csrf(self.client, f"/ui/rfq/{self.rfq.rfq_id}")
            response = self.client.post(
                f"/ui/rfq/{self.rfq.rfq_id}/send",
                data={"csrf_token": token, "to": "client@example.ru", "body": "КП во вложении"},
            )
        finally:
            settings.smtp_enabled = original_enabled
        self.assertEqual(response.status_code, 303)
        self.assertIn("очеред", unquote(response.headers["location"]).lower())
        self.assertGreater(store.outbox_stats()["pending"], pending_before)


if __name__ == "__main__":
    unittest.main()