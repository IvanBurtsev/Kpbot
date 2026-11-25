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
        self.assertFalse(result["telegram"])

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


class MaxChannelTest(unittest.TestCase):
    def setUp(self) -> None:
        from kpbot.notify import MaxConfig

        self.config = MaxConfig(token="max-token", user_id="777")

    def test_send_uses_header_and_user_id(self) -> None:
        from kpbot.notify import send_max

        with patch("kpbot.notify.httpx.post", return_value=response(200, {"message": {}})) as mocked:
            send_max(self.config, "привет")
        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs["params"], {"user_id": "777"})
        self.assertEqual(kwargs["headers"]["Authorization"], "max-token")
        self.assertTrue(kwargs["verify"])  # TLS проверяется, а не отключается

    def test_http_error_is_reported(self) -> None:
        from kpbot.notify import NotifyError, send_max

        with patch("kpbot.notify.httpx.post", return_value=httpx.Response(
            401, text='{"code":"verify.token"}', request=httpx.Request("POST", "https://platform-api2.max.ru/")
        )):
            with self.assertRaises(NotifyError) as ctx:
                send_max(self.config, "текст")
        self.assertIn("401", str(ctx.exception))
        self.assertNotIn("max-token", str(ctx.exception))

    def test_business_error_in_body_is_reported(self) -> None:
        from kpbot.notify import NotifyError, send_max

        payload = {"success": False, "code": "chat.denied", "message": "bot is not a member"}
        with patch("kpbot.notify.httpx.post", return_value=response(200, payload)):
            with self.assertRaises(NotifyError) as ctx:
                send_max(self.config, "текст")
        self.assertIn("chat.denied", str(ctx.exception))

    def test_missing_recipient(self) -> None:
        from kpbot.notify import MaxConfig, NotifyError, send_max

        with self.assertRaises(NotifyError):
            send_max(MaxConfig(token="max-token"), "текст")

    def test_channels_are_independent(self) -> None:
        from kpbot.notify import MaxConfig

        notifier = Notifier(
            TelegramConfig(token=TOKEN, chat_id="1"),
            MaxConfig(token="max-token", user_id="777"),
        )
        def fake_post(url, *args, **kwargs):
            # телеграм падает всегда (в том числе на повторе), MAX отвечает
            if "telegram" in str(url):
                raise httpx.ConnectError("telegram недоступен")
            return response(200, {"message": {}})

        with patch("kpbot.notify.httpx.post", side_effect=fake_post):
            result = notifier.notify_new_rfq(
                rfq_id="1", subject="s", sender="a", total="100", lines=1, matched=1
            )
        self.assertEqual(result, {"telegram": False, "max": True})

    def test_ssl_context_loads_mintsifry_ca(self) -> None:
        from kpbot.notify import max_ssl_context

        stats = max_ssl_context().cert_store_stats()
        self.assertGreater(stats["x509"], 10)


class MaxPollingTest(unittest.TestCase):
    def test_long_poll_timeout_means_no_messages(self) -> None:
        from kpbot.notify import MaxConfig, load_max_user_ids

        with patch("kpbot.notify.httpx.get", side_effect=httpx.ReadTimeout("нет событий")):
            users = load_max_user_ids(MaxConfig(token="max-token"))
        self.assertEqual(users, [])

    def test_updates_are_parsed_and_bots_ignored(self) -> None:
        from kpbot.notify import MaxConfig, load_max_user_ids

        payload = {
            "updates": [
                {"message": {"sender": {"user_id": 501, "name": "Иван", "is_bot": False}}},
                {"message": {"sender": {"user_id": 900, "name": "Бот", "is_bot": True}}},
            ]
        }
        with patch("kpbot.notify.httpx.get", return_value=response(200, payload)):
            users = load_max_user_ids(MaxConfig(token="max-token"))
        self.assertEqual(users, [("501", "Иван")])


class MaxTokenRedactionTest(unittest.TestCase):
    def test_token_without_prefix_is_hidden(self) -> None:
        from kpbot.logs import register_secret

        max_token = "f9LHodD0cOLvtNgNb8l7jFDH0yym2QYSZEwFC7OYLHPA3MLb5Onrz"
        register_secret(max_token)
        cleaned = redact(f"Authorization: {max_token}, ответ 401")
        self.assertNotIn(max_token, cleaned)
        self.assertIn("***", cleaned)

    def test_link_is_public_and_not_local(self) -> None:
        from kpbot.notify import Notifier

        notifier = Notifier(telegram=None, max_channel=None, public_base_url="https://kp.example.ru:8443")
        self.assertEqual(
            notifier.link_for("abc123"), "https://kp.example.ru:8443/ui/rfq/abc123"
        )

    def test_local_address_produces_no_link(self) -> None:
        from kpbot.notify import Notifier

        for base in ("http://127.0.0.1:8000", "http://localhost:8000", ""):
            notifier = Notifier(telegram=None, max_channel=None, public_base_url=base)
            self.assertEqual(notifier.link_for("abc123"), "", f"адрес {base!r} не должен попадать в уведомление")

    def test_message_without_link_has_no_trailing_blank(self) -> None:
        from kpbot.notify import format_new_rfq

        text = format_new_rfq(subject="Заявка", sender="a@b.ru", total="100", lines=2, matched=2, link="")
        self.assertFalse(text.endswith("\n"))


    def test_retry_wraps_transient_failure(self) -> None:
        from kpbot.notify import NotifyError, _with_retry

        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 2:
                raise NotifyError("сеть моргнула")
            return "ok"

        self.assertEqual(_with_retry(flaky, attempts=2, delay=0), "ok")
        self.assertEqual(calls["n"], 2)

    def test_retry_gives_up_and_raises(self) -> None:
        from kpbot.notify import NotifyError, _with_retry

        def always_fails():
            raise NotifyError("канал недоступен")

        with self.assertRaises(NotifyError):
            _with_retry(always_fails, attempts=2, delay=0)



if __name__ == "__main__":
    unittest.main()