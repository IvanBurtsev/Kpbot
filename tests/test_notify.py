"""Уведомления в Telegram: формат, ошибки, отсутствие утечки токена."""

import unittest
from unittest.mock import patch

import httpx

from kpbot.logs import redact
from kpbot.notify import (
    Notifier,
    NotifyError,
    TelegramConfig,
    format_new_rfq,
    load_chat_id,
    send_telegram,
)

TOKEN = "8935828001:AAHmockedtokenvalue1234567890abcdefg"


def response(status: int, payload: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status, json=payload or {}, request=httpx.Request("POST", "https://api.telegram.org/")
    )


class FormatTest(unittest.TestCase):
    def test_message_contains_key_facts(self) -> None:
        text = format_new_rfq(
            subject="Заявка на материалы",
            sender="zakaz@example.ru",
            total="16524.00",
            lines=4,
            matched=4,
            link="https://kp.example/ui/rfq/abc",
        )
        self.assertIn("Заявка на материалы", text)
        self.assertIn("16524.00", text)
        self.assertIn("4 из 4", text)
        self.assertIn("https://kp.example/ui/rfq/abc", text)

    def test_long_subject_is_cut(self) -> None:
        text = format_new_rfq(subject="я" * 500, sender="a@b.ru", total="0", lines=0, matched=0, link="x")
        self.assertLess(len(text), 4200)


class SendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = TelegramConfig(token=TOKEN, chat_id="123456")

    def test_success(self) -> None:
        with patch("kpbot.notify.httpx.post", return_value=response(200, {"ok": True})) as mocked:
            send_telegram(self.config, "привет")
        payload = mocked.call_args.kwargs["json"]
        self.assertEqual(payload["chat_id"], "123456")
        self.assertTrue(payload["disable_web_page_preview"])

    def test_error_does_not_leak_token(self) -> None:
        body = '{"ok":false,"description":"Unauthorized"}'
        with patch("kpbot.notify.httpx.post", return_value=httpx.Response(
            401, text=body, request=httpx.Request("POST", "https://api.telegram.org/")
        )):
            with self.assertRaises(NotifyError) as ctx:
                send_telegram(self.config, "текст")
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertIn("401", str(ctx.exception))

    def test_network_error_is_wrapped(self) -> None:
        with patch("kpbot.notify.httpx.post", side_effect=httpx.ConnectError("нет сети")):
            with self.assertRaises(NotifyError):
                send_telegram(self.config, "текст")

    def test_missing_chat_id(self) -> None:
        with self.assertRaises(NotifyError):
            send_telegram(TelegramConfig(token=TOKEN, chat_id=""), "текст")


class NotifierTest(unittest.TestCase):
    def test_disabled_when_no_chat_id(self) -> None:
        notifier = Notifier(TelegramConfig(token=TOKEN, chat_id=""))
        self.assertFalse(notifier.enabled)
        self.assertFalse(
            notifier.notify_new_rfq(rfq_id="1", subject="s", sender="a", total="0", lines=0, matched=0)
        )

    def test_failure_does_not_raise(self) -> None:
        notifier = Notifier(TelegramConfig(token=TOKEN, chat_id="1"))
        with patch("kpbot.notify.httpx.post", side_effect=httpx.ConnectError("нет сети")):
            result = notifier.notify_new_rfq(
                rfq_id="1", subject="s", sender="a", total="0", lines=1, matched=1
            )
        self.assertFalse(result)

    def test_link_uses_public_base(self) -> None:
        notifier = Notifier(TelegramConfig(token=TOKEN, chat_id="1"), public_base_url="https://kp.example/")
        self.assertEqual(notifier.link_for("abc"), "https://kp.example/ui/rfq/abc")


class ChatDiscoveryTest(unittest.TestCase):
    def test_reads_chats_from_updates(self) -> None:
        updates = {
            "ok": True,
            "result": [
                {"message": {"chat": {"id": 42, "username": "ivan"}}},
                {"message": {"chat": {"id": 42, "username": "ivan"}}},
                {"message": {"chat": {"id": 77, "first_name": "Отдел"}}},
            ],
        }
        with patch("kpbot.notify.httpx.get", return_value=response(200, updates)):
            chats = load_chat_id(TelegramConfig(token=TOKEN, chat_id=""))
        self.assertEqual(chats, [("42", "ivan"), ("77", "Отдел")])


class RedactionTest(unittest.TestCase):
    def test_bot_token_is_removed(self) -> None:
        cleaned = redact(f"url https://api.telegram.org/bot{TOKEN}/sendMessage failed")
        self.assertNotIn(TOKEN, cleaned)
        self.assertIn("***", cleaned)


if __name__ == "__main__":
    unittest.main()