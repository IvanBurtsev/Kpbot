"""Отправка КП: сборка письма и реальная отправка на локальный SMTP-приёмник."""

import email
import shutil
import socketserver
import threading
import unittest
from decimal import Decimal
from email import policy
from pathlib import Path

from kpbot.config import BASE_DIR, Settings
from kpbot.mailer import (
    Attachment,
    MailError,
    Mailer,
    build_kp_message,
    load_attachments,
    reply_subject,
)

TMP = BASE_DIR / ".tmp" / "mailer"


class MessageTest(unittest.TestCase):
    def test_message_has_attachments_and_headers(self) -> None:
        message = build_kp_message(
            sender="client@example.ru",
            from_addr="sales@firma.ru",
            subject="Re: Заявка",
            body="Добрый день, КП во вложении",
            attachments=[Attachment("kp.pdf", b"%PDF-1.7 test", "application", "pdf")],
            reply_to="sales@firma.ru",
        )
        self.assertEqual(message["To"], "client@example.ru")
        self.assertEqual(message["Subject"], "Re: Заявка")
        self.assertEqual(message["Reply-To"], "sales@firma.ru")

        parsed = email.message_from_bytes(message.as_bytes(), policy=policy.default)
        attachments = [part.get_filename() for part in parsed.iter_attachments()]
        self.assertEqual(attachments, ["kp.pdf"])

    def test_reply_subject_is_not_doubled(self) -> None:
        self.assertEqual(reply_subject("Re: Заявка"), "Re: Заявка")
        self.assertEqual(reply_subject("Заявка"), "Re: Заявка")

    def test_disabled_smtp_raises_clear_error(self) -> None:
        settings = Settings(data_dir=TMP, smtp_enabled=False)
        with self.assertRaises(MailError) as ctx:
            Mailer(settings).send(build_kp_message(
                sender="a@b.ru", from_addr="c@d.ru", subject="s", body="b", attachments=[]
            ), to="a@b.ru")
        self.assertIn("выключена", str(ctx.exception))

    def test_attachments_come_from_disk(self) -> None:
        shutil.rmtree(TMP, ignore_errors=True)
        TMP.mkdir(parents=True)
        rfq_id = "a" * 32
        (TMP / f"{rfq_id}.pdf").write_bytes(b"%PDF-1.7" + b"x" * 2000)
        (TMP / f"{rfq_id}.docx").write_bytes(b"PK" + b"x" * 2000)
        attachments = load_attachments(TMP, rfq_id, short=rfq_id[:8])
        self.assertEqual([item.filename for item in attachments], [f"kp-aaaaaaaa.pdf", f"kp-aaaaaaaa.docx"])


class _Sink(socketserver.StreamRequestHandler):
    """Минимальный SMTP-сервер: принимает письмо и складывает его в self.server.messages."""

    def handle(self) -> None:
        self.wfile.write(b"220 sink ready\r\n")
        data_mode = False
        while True:
            line = self.rfile.readline()
            if not line:
                return
            if data_mode:
                if line.strip() == b".":
                    data_mode = False
                    self.wfile.write(b"250 queued\r\n")
                    continue
                self.server.messages.append(line)  # type: ignore[attr-defined]
                continue
            command = line.strip().upper()
            if command.startswith(b"EHLO") or command.startswith(b"HELO"):
                self.wfile.write(b"250-sink\r\n250 AUTH PLAIN LOGIN\r\n")
            elif command.startswith(b"AUTH"):
                self.wfile.write(b"235 ok\r\n")
            elif command.startswith(b"MAIL FROM") or command.startswith(b"RCPT TO"):
                self.wfile.write(b"250 ok\r\n")
            elif command.startswith(b"DATA"):
                self.wfile.write(b"354 go ahead\r\n")
                data_mode = True
            elif command.startswith(b"QUIT"):
                self.wfile.write(b"221 bye\r\n")
                return
            else:
                self.wfile.write(b"250 ok\r\n")


class SendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = socketserver.TCPServer(("127.0.0.1", 0), _Sink)
        self.server.messages = []  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def test_message_really_goes_over_smtp(self) -> None:
        settings = Settings(
            data_dir=TMP,
            smtp_enabled=True,
            smtp_host="127.0.0.1",
            smtp_port=self.port,
            smtp_user="",
            smtp_from="sales@firma.ru",
            smtp_starttls=False,
        )
        message = build_kp_message(
            sender="client@example.ru",
            from_addr="sales@firma.ru",
            subject="Re: Заявка",
            body="КП во вложении",
            attachments=[Attachment("kp.pdf", b"%PDF-1.7", "application", "pdf")],
        )
        Mailer(settings).send(message, to="client@example.ru")
        received = b"".join(self.server.messages).decode("utf-8", "replace")  # type: ignore[attr-defined]
        self.assertIn("client@example.ru", received)
        self.assertIn("kp.pdf", received)


if __name__ == "__main__":
    unittest.main()