"""Почта клиентов: свой ящик на каждого, poll --all-tenants обходит всех."""

import support  # общий стенд: изолированные данные, сеть запрещена

import argparse
import unittest
from pathlib import Path
from unittest.mock import patch

from kpbot import cli
from kpbot.config import settings
from kpbot.store import Store


class FakeMailbox:
    """Заглушка ящика: фиксирует, с какими настройками её создали."""

    created: list[dict] = []

    def __init__(self, config) -> None:
        if isinstance(config, dict):  # cli передаёт настройки клиента словарём
            self.host = config.get("mail_imap_host", "")
            self.user = config.get("mail_imap_user", "")
            self.folder = config.get("mail_imap_folder", "INBOX")
        else:
            self.host = config.host
            self.user = config.user
            self.folder = config.folder
        FakeMailbox.created.append({"host": self.host, "user": self.user, "folder": self.folder})

    def check(self) -> dict:
        return {"folder": self.folder, "total": 0, "unseen": 0, "auth": "LOGIN"}

    def fetch_unseen(self, limit: int = 10):  # pragma: no cover - в тесте не доходим
        return []


class TenantMailboxTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeMailbox.created = []

    def test_tenant_settings_win_over_env(self) -> None:
        mailbox = cli._mailbox(
            {
                "mail_imap_host": "imap.yandex.ru",
                "mail_imap_port": "993",
                "mail_imap_user": "client@firma.ru",
                "mail_imap_password": "app-password",
                "mail_imap_folder": "Заявки",
            }
        )
        self.assertEqual(mailbox.config.host, "imap.yandex.ru")
        self.assertEqual(mailbox.config.user, "client@firma.ru")
        self.assertEqual(mailbox.config.folder, "Заявки")
        self.assertEqual(mailbox.auth_mode, "LOGIN")

    def test_no_mail_settings_is_clear_error(self) -> None:
        from kpbot.mailbox import MailError

        with self.assertRaises(MailError):
            cli._mailbox({})  # в стенде IMAP-пользователь пуст

    def test_poll_all_tenants_uses_each_mailbox(self) -> None:
        first = Store(support.ROOT / "tenant-a.sqlite3")
        first.set_settings(
            {
                "mail_imap_host": "imap.a.ru",
                "mail_imap_user": "a@a.ru",
                "mail_imap_password": "p",
                "mail_imap_folder": "INBOX",
            }
        )
        second = Store(support.ROOT / "tenant-b.sqlite3")
        second.set_settings({"company_name": "Без почты"})
        second.replace_price_items([])

        with patch.object(cli, "tenant_stores", lambda: [
            ("default", "Основной", first),
            ("b", "Клиент Б", second),
        ]), patch.object(cli, "_mailbox", FakeMailbox):
            code = cli._poll_all_tenants(argparse.Namespace(limit=5, dry_run=True))

        self.assertEqual(code, 0)
        self.assertEqual(len(FakeMailbox.created), 1)
        self.assertEqual(FakeMailbox.created[0]["user"], "a@a.ru")
        # клиенту без прайса демо-прайс не подсовываем
        self.assertEqual(second.price_count(), 0)

    def test_poll_all_tenants_without_mail_does_nothing(self) -> None:
        store = Store(support.ROOT / "tenant-empty.sqlite3")
        with patch.object(cli, "tenant_stores", lambda: [("x", "Пусто", store)]), \
                patch.object(cli, "_mailbox", FakeMailbox), \
                patch.object(settings, "imap_user", ""):
            code = cli._poll_all_tenants(argparse.Namespace(limit=5, dry_run=True))
        self.assertEqual(code, 0)
        self.assertEqual(FakeMailbox.created, [])


if __name__ == "__main__":
    unittest.main()
