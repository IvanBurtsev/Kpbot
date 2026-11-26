"""Разбор письма (.eml) в текст. Никакого HTML-рендера и никаких переходов по ссылкам."""

from __future__ import annotations

import email
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage, Message
from email.utils import formatdate, make_msgid, parseaddr
from html.parser import HTMLParser
from pathlib import Path

MAX_TEXT_CHARS = 200_000
# Вложение-заявка (Excel/Word/PDF): не больше 5 МБ на файл и 20 МБ распакованных
# данных. Второе — защита от zip-бомбы: xlsx/docx внутри zip, и «файл на 20 КБ»
# может развернуться в гигабайты.
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_ATTACHMENT_UNCOMPRESSED = 20 * 1024 * 1024
MAX_ATTACHMENT_TEXT = 60_000
MAX_ATTACHMENTS = 10
_WHITESPACE = re.compile(r"[ \t\u00a0]+")


class _HtmlText(HTMLParser):
    """HTML → текст с сохранением строк таблиц.

    Раньше теги просто заменялись на перевод строки, и табличная заявка
    превращалась в столбик «Уголок 50х50 / 200 / шт», где количество уже
    не связано с названием. Теперь ячейки строки склеиваются пробелом,
    а строки таблицы разделяются переводом строки.
    """

    _BLOCK = {
        "p", "div", "br", "li", "ul", "ol", "tr", "table", "h1", "h2", "h3",
        "h4", "h5", "h6", "blockquote", "pre", "section", "article", "header",
        "footer",
    }
    _CELL = {"td", "th"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")
        elif tag in self._CELL:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK:
            self.parts.append("\n")
        elif tag in self._CELL:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


@dataclass(slots=True)
class ParsedEmail:
    sender: str
    subject: str
    message_id: str
    received_at: str
    text: str
    attachments: list[str] = field(default_factory=list)


def sender_display_name(value: str) -> str:
    """«ИП Сергеев <mail@example.ru>» → «ИП Сергеев»; без имени остаётся адрес."""
    text = (value or "").strip()
    if not text:
        return ""
    match = re.match(r'^\s*"?([^"<]*?)"?\s*<[^>]+>\s*$', text)
    name = (match.group(1).strip() if match else text).strip().strip('"')
    return name or text


def parse_eml(raw: bytes) -> ParsedEmail:
    if not raw:
        raise ValueError("пустое письмо")
    if len(raw) > 20 * 1024 * 1024:
        raise ValueError("письмо больше 20 МБ")

    msg: Message = email.message_from_bytes(raw, policy=policy.default)

    sender = parseaddr(str(msg.get("From", "")))[1] or str(msg.get("From", "")).strip()
    subject = str(msg.get("Subject", "")).strip()
    message_id = str(msg.get("Message-ID", "")).strip() or f"local-{abs(hash(raw)):x}"
    received_at = str(msg.get("Date", "")).strip() or datetime.now(timezone.utc).isoformat(timespec="seconds")

    text, attachments = _extract_text(msg)
    if not text and not attachments:
        raise ValueError("в письме нет ни текста, ни разбираемых вложений")

    return ParsedEmail(
        sender=sender[:320],
        subject=subject[:500],
        message_id=message_id[:500],
        received_at=received_at,
        text=text[:MAX_TEXT_CHARS],
        attachments=attachments,
    )


def _zip_uncompressed_size(data: bytes) -> int:
    """Сколько распакуется: защита от zip-бомбы в xlsx/docx."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return sum(item.file_size for item in archive.infolist())
    except (zipfile.BadZipFile, OSError):
        return 0


def _xlsx_text(data: bytes) -> str:
    from openpyxl import load_workbook

    book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    lines: list[str] = []
    try:
        for sheet in book.worksheets:
            for row in sheet.iter_rows(values_only=True):
                cells = [str(cell).strip() for cell in row if cell not in (None, "")]
                if cells:
                    lines.append(" ".join(cells))
    finally:
        book.close()
    return "\n".join(lines)


def _docx_text(data: bytes) -> str:
    from docx import Document

    document = Document(io.BytesIO(data))
    lines = [paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                lines.append(" ".join(cells))
    return "\n".join(lines)


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:30])
    except Exception:  # noqa: BLE001 - битый PDF не должен ронять разбор письма
        return ""


def _attachment_text(filename: str, data: bytes) -> str:
    """Текст вложения-заявки: xlsx, docx, pdf, csv/txt."""
    suffix = Path(filename).suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        if _zip_uncompressed_size(data) > MAX_ATTACHMENT_UNCOMPRESSED:
            return ""
        return _xlsx_text(data)
    if suffix == ".docx":
        if _zip_uncompressed_size(data) > MAX_ATTACHMENT_UNCOMPRESSED:
            return ""
        return _docx_text(data)
    if suffix == ".pdf":
        return _pdf_text(data)
    if suffix in {".csv", ".txt"}:
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return data.decode("cp1251", "replace")
    return ""


def _extract_text(msg: Message) -> tuple[str, list[str]]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachment_parts: list[str] = []
    attachment_names: list[str] = []

    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = part.get_filename()
        if filename:
            # Заявка часто лежит в Excel/Word/PDF, а в теле — «список во вложении».
            if len(attachment_names) >= MAX_ATTACHMENTS:
                continue
            try:
                payload = part.get_payload(decode=True)
            except Exception:  # noqa: BLE001 - битое вложение не должно ронять письмо
                payload = None
            if not payload or len(payload) > MAX_ATTACHMENT_BYTES:
                continue
            try:
                extracted = _attachment_text(filename, payload)
            except Exception:  # noqa: BLE001 - битый файл пропускаем
                extracted = ""
            if extracted.strip():
                attachment_parts.append(extracted[:MAX_ATTACHMENT_TEXT])
                attachment_names.append(filename[:120])
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            payload = part.get_content()
        except Exception:
            continue
        if not isinstance(payload, str):
            continue
        if content_type == "text/plain":
            plain_parts.append(payload)
        else:
            html_parts.append(payload)

    if plain_parts:
        body = "\n".join(plain_parts)
    else:
        parser = _HtmlText()
        parser.feed("\n".join(html_parts))
        parser.close()
        body = parser.text()

    chunks = ([body] if body.strip() else []) + attachment_parts
    text = _WHITESPACE.sub(" ", "\n".join(chunks))
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line), attachment_names


def build_reply_eml(
    *,
    sender_to: str,
    sender_from: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
) -> bytes:
    """Текст ответа клиенту. Отправку выполняет отдельный шаг."""
    msg = EmailMessage()
    msg["From"] = sender_from
    msg["To"] = sender_to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain="kpbot.local")
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    return msg.as_bytes()