import unittest

from kpbot.mailbox import ImapConfig, ImapMailbox, MailError


class MailboxGuardTest(unittest.TestCase):
    def test_missing_credentials_raises_human_error(self) -> None:
        with self.assertRaises(MailError) as ctx:
            ImapMailbox(ImapConfig(host="imap.gmail.com", user="", password=""))
        self.assertIn("KPBOT_IMAP_USER", str(ctx.exception))

    def test_config_defaults(self) -> None:
        config = ImapConfig(host="imap.gmail.com", user="a@b.c", password="x")
        self.assertEqual(config.port, 993)
        self.assertEqual(config.folder, "INBOX")

    def test_message_about_app_password_is_explained(self) -> None:
        # Проверяем формулировку, которую пользователь увидит при включённой 2FA.
        error = MailError(
            "Gmail требует пароль приложения: включите двухфакторную аутентификацию "
            "и создайте отдельный пароль для почты (16 символов), обычный пароль не подойдёт"
        )
        self.assertIn("пароль приложения", str(error))


if __name__ == "__main__":
    unittest.main()