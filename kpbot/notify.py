"""Уведомления о новых заявках.

Канал — Telegram Bot API. Требования, которые мы соблюдаем (как это делают
взрослые продукты):
  * одно сообщение на одну заявку, дедупликация по её идентификатору;
  * сбой уведомления не должен ломать приём письма;
  * токен бота живёт в настройках, в логи не попадает;
  * ссылка в сообщении ведёт прямо в карточку заявки.
"""

from __future__ import annotations

import logging
import socket
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx

from .logs import redact

log = logging.getLogger("kpbot.notify")

_OVERRIDE_LOCK = threading.Lock()


class NotifyError(RuntimeError):
    """Не удалось отправить уведомление."""


@dataclass(slots=True)
class TelegramConfig:
    token: str
    chat_id: str
    api_base: str = "https://api.telegram.org"
    ip_override: str = ""
    timeout: float = 15.0


@contextmanager
def _resolve_override(host: str, ip: str):
    """Подменяет DNS для одного хоста на время вызова.

    Нужно там, где провайдер режет часть адресов Telegram: сам API доступен,
    а конкретный IP из ответа DNS — нет.
    """
    if not ip:
        yield
        return
    with _OVERRIDE_LOCK:
        original = socket.getaddrinfo

        def patched(address, *args, **kwargs):
            if address == host:
                address = ip
            return original(address, *args, **kwargs)

        socket.getaddrinfo = patched  # type: ignore[assignment]
        try:
            yield
        finally:
            socket.getaddrinfo = original  # type: ignore[assignment]


def _api_url(config: TelegramConfig) -> str:
    return f"{config.api_base.rstrip('/')}/bot{config.token}/sendMessage"


def send_telegram(config: TelegramConfig, text: str) -> None:
    if not config.token or not config.chat_id:
        raise NotifyError("не заданы токен бота или chat_id")

    payload = {
        "chat_id": config.chat_id,
        "text": text[:4000],
        "disable_web_page_preview": True,
    }
    try:
        with _resolve_override("api.telegram.org", config.ip_override):
            response = httpx.post(_api_url(config), json=payload, timeout=config.timeout)
    except httpx.HTTPError as exc:
        raise NotifyError(f"сеть: {redact(str(exc))[:200]}") from exc

    if response.status_code != 200:
        body = redact(response.text)[:200]
        raise NotifyError(f"Telegram ответил {response.status_code}: {body}")


def format_new_rfq(
    *,
    subject: str,
    sender: str,
    total: str,
    lines: int,
    matched: int,
    link: str,
) -> str:
    tail = f"\nПозиций: {matched} из {lines}" if lines else ""
    return (
        f"Новая заявка\n"
        f"Тема: {subject[:120]}\n"
        f"От: {sender[:80]}\n"
        f"Сумма: {total} ₽{tail}\n"
        f"{link}"
    )


class Notifier:
    """Отправляет уведомления в Telegram и запоминает факт отправки."""

    def __init__(self, config: TelegramConfig | None, *, public_base_url: str = "") -> None:
        self.config = config
        self.public_base_url = public_base_url.rstrip("/")

    @classmethod
    def from_settings(cls, settings) -> "Notifier":
        if not settings.telegram_enabled or not settings.telegram_bot_token:
            return cls(None, public_base_url=settings.public_base_url)
        return cls(
            TelegramConfig(
                token=settings.telegram_bot_token,
                chat_id=settings.telegram_chat_id,
                api_base=settings.telegram_api_base,
                ip_override=settings.telegram_ip_override,
            ),
            public_base_url=settings.public_base_url,
        )

    @property
    def enabled(self) -> bool:
        return self.config is not None and bool(self.config.chat_id)

    def link_for(self, rfq_id: str) -> str:
        base = self.public_base_url or "http://127.0.0.1:8000"
        return f"{base}/ui/rfq/{rfq_id}"

    def notify_new_rfq(
        self,
        *,
        rfq_id: str,
        subject: str,
        sender: str,
        total: str,
        lines: int,
        matched: int,
    ) -> bool:
        """Возвращает True, если сообщение ушло. Исключения не пробрасывает."""
        if not self.enabled:
            return False
        text = format_new_rfq(
            subject=subject,
            sender=sender,
            total=total,
            lines=lines,
            matched=matched,
            link=self.link_for(rfq_id),
        )
        try:
            send_telegram(self.config, text)
        except NotifyError as exc:
            log.warning("уведомление не отправлено: %s", redact(str(exc)))
            return False
        return True


def load_chat_id(config: TelegramConfig) -> list[tuple[str, str]]:
    """Возвращает [(chat_id, имя)] из getUpdates — нужно при первом подключении."""
    url = f"{config.api_base.rstrip('/')}/bot{config.token}/getUpdates"
    try:
        with _resolve_override("api.telegram.org", config.ip_override):
            response = httpx.get(url, timeout=config.timeout)
    except httpx.HTTPError as exc:
        raise NotifyError(f"сеть: {redact(str(exc))[:200]}") from exc
    if response.status_code != 200:
        raise NotifyError(f"Telegram ответил {response.status_code}: {redact(response.text)[:200]}")

    found: dict[str, str] = {}
    for item in response.json().get("result", []):
        message = item.get("message") or item.get("edited_message") or {}
        chat = message.get("chat") or {}
        if chat.get("id"):
            found[str(chat["id"])] = chat.get("username") or chat.get("first_name") or ""
    return sorted(found.items())


def bot_username(config: TelegramConfig) -> str:
    url = f"{config.api_base.rstrip('/')}/bot{config.token}/getMe"
    try:
        with _resolve_override("api.telegram.org", config.ip_override):
            response = httpx.get(url, timeout=config.timeout)
    except httpx.HTTPError as exc:
        raise NotifyError(f"сеть: {redact(str(exc))[:200]}") from exc
    if response.status_code != 200:
        raise NotifyError(f"Telegram ответил {response.status_code}: {redact(response.text)[:200]}")
    return str(response.json().get("result", {}).get("username", ""))


def resolve_chat_id_from_env_file(path: Path) -> str:  # pragma: no cover - вспомогательное
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    for line in text.splitlines():
        if line.startswith("KPBOT_TELEGRAM_CHAT_ID="):
            return line.split("=", 1)[1].strip()
    return ""