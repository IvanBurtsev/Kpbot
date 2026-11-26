"""Заявка во вложении: xlsx, docx, pdf, csv.

Раньше вложения молча игнорировались, письмо «список во вложении» считалось
не-заявкой и помечалось прочитанным — клиент оставался без ответа.
"""

import support  # общий стенд

import io
import os
import unittest
from email.message import EmailMessage

os.environ["KPBOT_API_KEY"] = "test-key"
os.environ["KPBOT_UI_PASSWORD"] = "test-pass"
os.environ["KPBOT_SESSION_SECRET"] = "test-secret"

from kpbot.config import settings  # noqa: E402
from kpbot.mailparse import parse_eml  # noqa: E402
from kpbot.pipeline import Pipeline  # noqa: E402
from kpbot.store import Store  # noqa: E402


def _eml(filename: str, payload: bytes, maintype: str, subtype: str, body: str = "Список во вложении") -> bytes:
    message = EmailMessage()
    message["From"] = "client@example.ru"
    message["To"] = "sales@example.ru"
    message["Subject"] = "Заявка во вложении"
    message["Message-ID"] = f"<{filename}@example.ru>"
    message.set_content(body)
    message.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)
    return message.as_bytes()


def _xlsx() -> bytes:
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.append(("Наименование", "Кол-во", "Ед."))
    sheet.append(("Уголок 50х50", 200, "шт"))
    sheet.append(("Кабель ВВГ 3х2.5", 150, "м"))
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _docx() -> bytes:
    from docx import Document

    document = Document()
    document.add_paragraph("Цемент М500 — 40 мешков")
    table = document.add_table(rows=2, cols=3)
    table.rows[0].cells[0].text = "Наименование"
    table.rows[1].cells[0].text = "Саморез 4.2х75"
    table.rows[1].cells[1].text = "10"
    table.rows[1].cells[2].text = "уп"
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


class AttachmentParseTest(unittest.TestCase):
    def test_xlsx_attachment_is_read(self) -> None:
        parsed = parse_eml(_eml("zayavka.xlsx", _xlsx(), "application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
        self.assertEqual(parsed.attachments, ["zayavka.xlsx"])
        self.assertIn("Уголок 50х50 200 шт", parsed.text)
        self.assertIn("Кабель ВВГ 3х2.5 150 м", parsed.text)

    def test_docx_attachment_is_read(self) -> None:
        parsed = parse_eml(_eml("zayavka.docx", _docx(), "application", "vnd.openxmlformats-officedocument.wordprocessingml.document"))
        self.assertIn("Цемент М500 — 40 мешков", parsed.text)
        self.assertIn("Саморез 4.2х75 10 уп", parsed.text)

    def test_csv_attachment_is_read(self) -> None:
        parsed = parse_eml(_eml("zayavka.csv", "Уголок 50х50;200;шт\n".encode("utf-8"), "text", "csv"))
        self.assertIn("Уголок 50х50;200;шт", parsed.text)

    def test_empty_body_with_attachment_is_not_an_error(self) -> None:
        parsed = parse_eml(_eml("zayavka.xlsx", _xlsx(), "application", "octet-stream", body=""))
        self.assertIn("Уголок 50х50", parsed.text)

    def test_broken_attachment_does_not_break_letter(self) -> None:
        parsed = parse_eml(_eml("broken.xlsx", b"not a zip", "application", "octet-stream", body="Уголок 50х50 — 5 шт"))
        self.assertIn("Уголок 50х50", parsed.text)
        self.assertEqual(parsed.attachments, [])


class AttachmentPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(support.ROOT / f"attach-{id(self)}.sqlite3")
        self.pipeline = Pipeline(self.store, settings)

    def test_rfq_from_xlsx_attachment(self) -> None:
        result = self.pipeline.process_eml_bytes(
            _eml("zayavka.xlsx", _xlsx(), "application", "octet-stream"), actor="test"
        )
        self.assertFalse(result.duplicate)
        self.assertGreaterEqual(result.lines, 2)
        self.assertGreaterEqual(result.matched, 2)
        self.assertNotEqual(result.total, "0.00")


if __name__ == "__main__":
    unittest.main()
