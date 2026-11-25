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
import time
import socket
import ssl
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


@dataclass(slots=True)
class MaxConfig:
    """MAX (мессенджер): API на platform-api2.max.ru, токен в заголовке Authorization."""

    token: str
    user_id: str = ""
    chat_id: str = ""
    api_base: str = "https://platform-api2.max.ru"
    timeout: float = 15.0

    @property
    def addressed(self) -> bool:
        return bool(self.user_id or self.chat_id)


_CERTS_DIR = Path(__file__).resolve().parent / "certs"
_MAX_SSL_CONTEXT: "ssl.SSLContext | None" = None


def max_ssl_context() -> "ssl.SSLContext":
    """Контекст с доверием к сертификату Минцифры: TLS проверяется, а не отключается.

    MAX использует российский УЦ, которого нет в стандартном наборе Python.
    Корневой и промежуточный сертификаты лежат в kpbot/certs.
    """
    global _MAX_SSL_CONTEXT
    if _MAX_SSL_CONTEXT is not None:
        return _MAX_SSL_CONTEXT

    context = ssl.create_default_context()
    loaded = 0
    for cert in sorted(_CERTS_DIR.glob("*.pem")):
        try:
            context.load_verify_locations(cafile=str(cert))
            loaded += 1
        except ssl.SSLError:
            log.warning("не удалось загрузить сертификат %s", cert.name)
    if not loaded:
        log.warning("сертификаты Минцифры не найдены в %s — запросы к MAX могут не пройти", _CERTS_DIR)
    _MAX_SSL_CONTEXT = context
    return context


def send_max(config: MaxConfig, text: str) -> None:
    if not config.token:
        raise NotifyError("не задан токен бота MAX")
    if not config.addressed:
        raise NotifyError("не задан user_id или chat_id для MAX")

    params = {"chat_id": config.chat_id} if config.chat_id else {"user_id": config.user_id}
    url = f"{config.api_base.rstrip('/')}/messages"
    payload = {"text": text[:4000], "disable_link_preview": True}

    try:
        response = httpx.post(
            url,
            params=params,
            headers={"Authorization": config.token, "Content-Type": "application/json"},
            json=payload,
            timeout=config.timeout,
            verify=max_ssl_context(),
        )
    except httpx.HTTPError as exc:
        raise NotifyError(f"сеть: {redact(str(exc))[:200]}") from exc

    if response.status_code != 200:
        raise NotifyError(f"MAX ответил {response.status_code}: {redact(response.text)[:200]}")
    body = response.json() if response.content else {}
    if isinstance(body, dict) and body.get("success") is False:
        raise NotifyError(f"MAX вернул ошибку: {body.get('code', '')} {str(body.get('message', ''))[:120]}")


def max_bot_info(config: MaxConfig) -> dict:
    url = f"{config.api_base.rstrip('/')}/me"
    try:
        response = httpx.get(
            url,
            headers={"Authorization": config.token},
            timeout=config.timeout,
            verify=max_ssl_context(),
        )
    except httpx.HTTPError as exc:
        raise NotifyError(f"сеть: {redact(str(exc))[:200]}") from exc
    if response.status_code != 200:
        raise NotifyError(f"MAX ответил {response.status_code}: {redact(response.text)[:200]}")
    return response.json()


def load_max_user_ids(config: MaxConfig) -> list[tuple[str, str]]:
    """Кто писал боту: из long polling /updates. Нужно при первом подключении.

    /updates без параметра `timeout` держит соединение до события, поэтому для
    разведки просим минимальное ожидание и считаем таймаут «сообщений нет».
    """
    url = f"{config.api_base.rstrip('/')}/updates"
    try:
        response = httpx.get(
            url,
            headers={"Authorization": config.token},
            params={"timeout": 1, "limit": 100},
            timeout=config.timeout,
            verify=max_ssl_context(),
        )
    except httpx.ReadTimeout:
        return []
    except httpx.HTTPError as exc:
        raise NotifyError(f"сеть: {redact(str(exc))[:200]}") from exc
    if response.status_code != 200:
        raise NotifyError(f"MAX ответил {response.status_code}: {redact(response.text)[:200]}")

    found: dict[str, str] = {}
    for update in response.json().get("updates", []):
        message = update.get("message") or {}
        sender = message.get("sender") or {}
        user_id = sender.get("user_id")
        if user_id and not sender.get("is_bot"):
            found[str(user_id)] = sender.get("name") or sender.get("first_name") or ""
    return sorted(found.items())


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


def _with_retry(action, *args, attempts: int = 2, delay: float = 0.7, **kwargs):
    """Повторяет сетевой вызов: короткий сбой провайдера не должен съедать уведомление."""
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return action(*args, **kwargs)
        except NotifyError as exc:
            last = exc
            if attempt < attempts:
                time.sleep(delay * attempt)
    raise last if last is not None else NotifyError("неизвестная ошибка")


def _is_local_url(url: str) -> bool:
    lowered = url.lower()
    return any(host in lowered for host in ("127.0.0.1", "localhost", "0.0.0.0", "::1"))


def send_telegram(config: TelegramConfig, text: str, button_url: str = "") -> None:
    if not config.token or not config.chat_id:
        raise NotifyError("не заданы токен бота или chat_id")

    payload: dict[str, object] = {
        "chat_id": config.chat_id,
        "text": text[:4000],
        "disable_web_page_preview": True,
    }
    if button_url:
        # кнопка открывает веб-версию заявки в браузере, а не локальный адрес
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": "Открыть заявку", "url": button_url}]]
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
    body = (
        f"Новая заявка\n"
        f"Тема: {subject[:120]}\n"
        f"От: {sender[:80]}\n"
        f"Сумма: {total} ₽{tail}"
    )
    return f"{body}\n{link}" if link else body


class Notifier:
    """Отправляет уведомления в подключённые каналы: Telegram и MAX."""

    def __init__(
        self,
        telegram: TelegramConfig | None,
        max_channel: MaxConfig | None = None,
        *,
        public_base_url: str = "",
    ) -> None:
        self.telegram = telegram
        self.max = max_channel
        self.public_base_url = public_base_url.rstrip("/")

    @classmethod
    def from_settings(cls, settings) -> "Notifier":
        telegram = None
        if settings.telegram_enabled and settings.telegram_bot_token:
            telegram = TelegramConfig(
                token=settings.telegram_bot_token,
                chat_id=settings.telegram_chat_id,
                api_base=settings.telegram_api_base,
                ip_override=settings.telegram_ip_override,
            )
        max_channel = None
        if settings.max_enabled and settings.max_bot_token:
            max_channel = MaxConfig(
                token=settings.max_bot_token,
                user_id=settings.max_user_id,
                chat_id=settings.max_chat_id,
                api_base=settings.max_api_base,
            )
        return cls(telegram, max_channel, public_base_url=settings.public_base_url)

    @property
    def enabled(self) -> bool:
        telegram_ready = self.telegram is not None and bool(self.telegram.chat_id)
        max_ready = self.max is not None and self.max.addressed
        return telegram_ready or max_ready

    def link_for(self, rfq_id: str) -> str:
        """Только публичный адрес: локальный в мессенджере бесполезен."""
        base = (self.public_base_url or "").rstrip("/")
        if not base or _is_local_url(base):
            log.error(
                "не задан публичный адрес (KPBOT_PUBLIC_BASE_URL=%r): "
                "ссылки в уведомлениях будут нерабочими",
                base,
            )
            return ""
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
    ) -> dict[str, bool]:
        """Отправляет во все подключённые каналы. Каналы не влияют друг на друга."""
        results: dict[str, bool] = {}
        if not self.enabled:
            return results

        link = self.link_for(rfq_id)
        text = format_new_rfq(
            subject=subject,
            sender=sender,
            total=total,
            lines=lines,
            matched=matched,
            link=link,
        )

        if self.telegram is not None and self.telegram.chat_id:
            try:
                _with_retry(send_telegram, self.telegram, text, button_url=link)
                results["telegram"] = True
            except NotifyError as exc:
                log.warning("telegram: уведомление не отправлено: %s", redact(str(exc)))
                results["telegram"] = False

        if self.max is not None and self.max.addressed:
            try:
                _with_retry(send_max, self.max, text)
                results["max"] = True
            except NotifyError as exc:
                log.warning("max: уведомление не отправлено: %s", redact(str(exc)))
                results["max"] = False

        return results


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