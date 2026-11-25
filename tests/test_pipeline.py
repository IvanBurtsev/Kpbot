import support  # общий стенд: изолированные данные, сеть запрещена

import shutil
import unittest
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

import httpx

from kpbot.config import BASE_DIR, Settings
from kpbot.mailparse import parse_eml
from kpbot.pipeline import NotAnRfq, Pipeline
from kpbot.store import Store

TMP_ROOT = support.ROOT


def make_data_dir(name: str) -> Path:
    TMP_ROOT.mkdir(exist_ok=True)
    path = TMP_ROOT / name
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)
    return path


def build_eml(body: str, *, subject: str = "Заявка", message_id: str = "<m1@test.local>") -> bytes:
    msg = EmailMessage()
    msg["From"] = "client@example.com"
    msg["To"] = "sales@example.com"
    msg["Subject"] = subject
    msg["Date"] = "Sat, 22 Nov 2026 10:00:00 +0300"
    msg["Message-ID"] = message_id
    msg.set_content(body, charset="utf-8")
    return msg.as_bytes()


class PipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.data_dir = make_data_dir("pipeline")
        self.settings = Settings(
            data_dir=self.data_dir,
            api_key="test-key",
            ui_password="test-pass",
            session_secret="test-secret",
            pdf_enabled=False,
        )
        self.store = Store(self.settings.db_path)
        self.pipeline = Pipeline(self.store, self.settings)

    def tearDown(self) -> None:
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def test_end_to_end_creates_quote_and_documents(self) -> None:
        result = self.pipeline.process_eml_bytes(build_eml("уголок 50х50 — 10 шт"), actor="test")

        self.assertEqual(result.status, "draft_ready")
        self.assertEqual(result.lines, 1)
        self.assertEqual(result.matched, 1)
        # 34.50 * 10 = 345.00 + НДС 20% = 414.00
        self.assertEqual(Decimal(result.total), Decimal("414.00"))

        self.assertTrue(Path(result.html_path).exists())
        self.assertTrue(Path(result.docx_path).exists())

        quote = self.store.get_quote(result.rfq_id)
        self.assertIsNotNone(quote)
        self.assertEqual(quote["total"], "414.00")
        self.assertEqual(len(quote["lines"]), 1)

    def test_unknown_position_goes_to_human_review(self) -> None:
        result = self.pipeline.process_eml_bytes(build_eml("подрозетник 68 мм — 20 шт"), actor="test")
        self.assertEqual(result.status, "human_review")
        self.assertEqual(result.matched, 0)
        self.assertEqual(Decimal(result.total), Decimal("0.00"))
        self.assertTrue(result.excluded)

    def test_duplicate_email_is_idempotent(self) -> None:
        raw = build_eml("уголок 50х50 — 10 шт")
        first = self.pipeline.process_eml_bytes(raw, actor="test")
        second = self.pipeline.process_eml_bytes(raw, actor="test")

        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(first.rfq_id, second.rfq_id)
        self.assertEqual(self.store.stats()["rfqs"], 1)

    def test_approve_writes_outbox_and_marks_sent(self) -> None:
        result = self.pipeline.process_eml_bytes(build_eml("уголок 50х50 — 10 шт"), actor="test")
        approved = self.pipeline.approve(result.rfq_id, actor="test", comment="ок")

        self.assertEqual(approved.status, "sent")
        outbox = self.settings.out_dir / "sent" / f"{result.rfq_id}.eml"
        self.assertTrue(outbox.exists())
        self.assertIn("414,00", parse_eml(outbox.read_bytes()).text)

        rfq = self.store.get_rfq(result.rfq_id)
        self.assertEqual(rfq["status"], "sent")
        actions = [row["action"] for row in self.store.decisions(result.rfq_id)]
        self.assertIn("approved", actions)
        self.assertIn("sent", actions)

    def test_reject_marks_status(self) -> None:
        result = self.pipeline.process_eml_bytes(build_eml("уголок 50х50 — 10 шт"), actor="test")
        self.pipeline.reject(result.rfq_id, actor="test", reason="не наш профиль")
        self.assertEqual(self.store.get_rfq(result.rfq_id)["status"], "rejected")

    def test_garbage_email_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            self.pipeline.process_eml_bytes(b"", actor="test")

    def test_newsletter_is_not_an_rfq_and_leaves_no_trace(self) -> None:
        newsletter = build_eml(
            "Новые проекты на бирже. Смотрите подборку за неделю и не забудьте обновить профиль.",
            subject="Дайджест недели",
        )
        with self.assertRaises(NotAnRfq):
            self.pipeline.process_eml_bytes(newsletter, actor="test")
        self.assertEqual(self.store.stats()["rfqs"], 0)

    def test_request_with_single_position_is_accepted(self) -> None:
        result = self.pipeline.process_eml_bytes(build_eml("кабель 3х2.5 — 20 м"), actor="test")
        self.assertEqual(result.lines, 1)
        self.assertEqual(self.store.stats()["rfqs"], 1)

    def test_html_is_escaped(self) -> None:
        result = self.pipeline.process_eml_bytes(
            build_eml("уголок 50х50 — 10 шт <script>alert(1)</script>"), actor="test"
        )
        html = Path(result.html_path).read_text(encoding="utf-8")
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_notification_failure_does_not_break_ingest(self) -> None:
        settings = Settings(
            data_dir=self.data_dir,
            api_key="test-key",
            ui_password="test-pass",
            session_secret="test-secret",
            pdf_enabled=False,
            telegram_enabled=True,
            telegram_bot_token="1234567890:AAmockedtokenvalue1234567890abcdefg",
            telegram_chat_id="42",
        )
        pipeline = Pipeline(self.store, settings)
        with patch("kpbot.notify.httpx.post", side_effect=httpx.ConnectError("нет сети")):
            result = pipeline.process_eml_bytes(build_eml("уголок 50х50 — 10 шт"), actor="test")
        self.assertEqual(result.status, "draft_ready")
        actions = [row["action"] for row in self.store.decisions(result.rfq_id)]
        self.assertIn("notify_failed", actions)

    def test_notification_sent_once_per_rfq(self) -> None:
        settings = Settings(
            data_dir=self.data_dir,
            api_key="test-key",
            ui_password="test-pass",
            session_secret="test-secret",
            pdf_enabled=False,
            telegram_enabled=True,
            telegram_bot_token="1234567890:AAmockedtokenvalue1234567890abcdefg",
            telegram_chat_id="42",
        )
        pipeline = Pipeline(self.store, settings)
        ok = httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", "https://api.telegram.org/"))
        with patch("kpbot.notify.httpx.post", return_value=ok) as mocked:
            first = pipeline.process_eml_bytes(build_eml("уголок 50х50 — 10 шт"), actor="test")
            pipeline.process_eml_bytes(build_eml("уголок 50х50 — 10 шт"), actor="test")
        self.assertEqual(mocked.call_count, 1)
        actions = [row["action"] for row in self.store.decisions(first.rfq_id)]
        self.assertIn("notified", actions)


if __name__ == "__main__":
    unittest.main()
