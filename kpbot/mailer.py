"""Отправка готового КП клиенту по почте.

Письмо собирается из того, что уже лежит в базе: текст ответа, PDF и DOCX.
Отправка выключена, пока не заданы SMTP-параметры — так сервис не может
случайно написать клиенту от чужого имени.
"""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

from .config import Settings


class MailError(RuntimeError):
    """Почта не настроена или сервер отказал."""


@dataclass(slots=True)
class Attachment:
    filename: str
    content: bytes
    maintype: str
    subtype: str


def build_kp_message(
    *,
    sender: str,
    from_addr: str,
    subject: str,
    body: str,
    attachments: list[Attachment],
    reply_to: str = "",
) -> EmailMessage:
    """Собирает письмо с вложениями. Отдельно от отправки — чтобы можно было проверять."""
    message = EmailMessage()
    message["From"] = formataddr(("Коммерческое предложение", from_addr))
    message["To"] = sender
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=from_addr.split("@")[-1] or "localhost")
    if reply_to:
        message["Reply-To"] = reply_to
    message.set_content(body)

    for item in attachments:
        message.add_attachment(
            item.content,
            maintype=item.maintype,
            subtype=item.subtype,
            filename=item.filename,
        )
    return message


def load_attachments(out_dir: Path, rfq_id: str, *, short: str) -> list[Attachment]:
    """PDF и DOCX, если они уже собраны. HTML не прикладываем — почтовики его режут."""
    result: list[Attachment] = []
    pdf = out_dir / f"{rfq_id}.pdf"
    if pdf.exists() and pdf.stat().st_size > 1024:
        result.append(Attachment(f"kp-{short}.pdf", pdf.read_bytes(), "application", "pdf"))
    docx = out_dir / f"{rfq_id}.docx"
    if docx.exists() and docx.stat().st_size > 1024:
        result.append(
            Attachment(
                f"kp-{short}.docx",
                docx.read_bytes(),
                "application",
                "vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        )
    return result


class Mailer:
    """Тонкая обёртка над smtplib: старт TLS, вход, отправка, понятные ошибки."""

    def __init__(self, cfg: Settings) -> None:
        self.host = cfg.smtp_host
        self.port = cfg.smtp_port
        self.user = cfg.smtp_user
        self.password = cfg.smtp_password
        self.from_addr = cfg.smtp_from or cfg.smtp_user
        self.security = (cfg.smtp_security or "starttls").lower()
        self.timeout = cfg.smtp_timeout_seconds
        self.enabled = cfg.smtp_enabled
        self.auth = (getattr(cfg, "smtp_auth", "auto") or "auto").lower()
        self.oauth_client_id = cfg.oauth_client_id
        self.oauth_client_secret = cfg.oauth_client_secret
        self.oauth_token_path = cfg.oauth_token_path

    def auth_mode(self) -> str:
        """«password» или «xoauth2».

        Gmail без пароля приложения не принимает обычный пароль, а с OAuth-токеном
        (тем же, что и для IMAP) отправка работает: XOAUTH2 поддерживает и SMTP.
        """
        if self.auth in ("xoauth2", "oauth"):
            return "xoauth2"
        if self.auth in ("password", "login"):
            return "password"
        return "password" if self.password else "xoauth2"

    def _login(self, client) -> None:
        if not self.user:
            return
        if self.auth_mode() == "password":
            client.login(self.user, self.password)
            return
        from .oauth import TokenStore, auth_string, ensure_access_token

        access = ensure_access_token(
            TokenStore(self.oauth_token_path), self.oauth_client_id, self.oauth_client_secret
        )
        client.auth(
            "XOAUTH2",
            lambda _challenge=None: auth_string(self.user, access).decode(),
            initial_response_ok=True,
        )

    def check(self) -> None:
        if not self.enabled:
            raise MailError("отправка выключена: KPBOT_SMTP_ENABLED=false")
        if not self.host:
            raise MailError("не задан KPBOT_SMTP_HOST")
        if not self.from_addr:
            raise MailError("не задан адрес отправителя KPBOT_SMTP_FROM")
        if self.user and self.auth_mode() == "password" and not self.password:
            raise MailError("нет пароля приложения: задайте KPBOT_SMTP_PASSWORD или KPBOT_SMTP_AUTH=xoauth2")

    def _connect(self, context) -> smtplib.SMTP:
        """465 — шифрование с первого байта (SSL), 587 — STARTTLS, none — только тесты."""
        if self.security == "ssl":
            return smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=context)
        return smtplib.SMTP(self.host, self.port, timeout=self.timeout)

    def send(self, message: EmailMessage, *, to: str) -> None:
        self.check()
        context = ssl.create_default_context()
        try:
            client = self._connect(context)
            with client:
                client.ehlo()
                if self.security == "starttls":
                    client.starttls(context=context)
                    client.ehlo()
                self._login(client)
                client.send_message(message, to_addrs=[to])
        except smtplib.SMTPAuthenticationError as exc:
            raise MailError(f"сервер не принял логин: {exc.smtp_code}") from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise MailError(f"адрес отклонён: {to}") from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise MailError(f"не удалось отправить: {exc}") from exc


def reply_subject(subject: str) -> str:
    base = (subject or "заявка").strip()
    if base.lower().startswith("re:"):
        return base
    return f"Re: {base}"


def check_connection(cfg: Settings) -> str:
    """Проверяет связь и логин, ничего не отправляя. Возвращает текст для человека."""
    mailer = Mailer(cfg)
    mailer.check()
    context = ssl.create_default_context()
    try:
        with mailer._connect(context) as client:
            client.ehlo()
            if mailer.security == "starttls":
                client.starttls(context=context)
                client.ehlo()
            mailer._login(client)
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError(f"логин отклонён: {exc.smtp_code}") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"связи нет: {exc}") from exc
    mode = "OAuth (XOAUTH2)" if mailer.auth_mode() == "xoauth2" else "пароль приложения"
    return (
        f"связь есть, вход выполнен ({mode}): {mailer.host}:{mailer.port} · "
        f"отправитель {mailer.from_addr}"
    )
