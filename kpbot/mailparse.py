"""Разбор письма (.eml) в текст. Никакого HTML-рендера и никаких переходов по ссылкам."""

from __future__ import annotations

import email
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage, Message
from email.utils import formatdate, make_msgid, parseaddr

MAX_TEXT_CHARS = 200_000
_HTML_TAG = re.compile(r"<[^>]{1,200}>")
_WHITESPACE = re.compile(r"[ \t\u00a0]+")


@dataclass(slots=True)
class ParsedEmail:
    sender: str
    subject: str
    message_id: str
    received_at: str
    text: str


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

    text = _extract_text(msg)
    if not text:
        raise ValueError("в письме нет текстового содержимого")

    return ParsedEmail(
        sender=sender[:320],
        subject=subject[:500],
        message_id=message_id[:500],
        received_at=received_at,
        text=text[:MAX_TEXT_CHARS],
    )


def _extract_text(msg: Message) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_filename():  # вложения пока не разбираем
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
        text = "\n".join(plain_parts)
    else:
        text = _HTML_TAG.sub("\n", "\n".join(html_parts))

    text = _WHITESPACE.sub(" ", text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


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