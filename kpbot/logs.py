"""Логи без утечек: ключи не должны попадать в файлы и journalctl."""

from __future__ import annotations

import logging
import re
import threading

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"GOCSPX-[A-Za-z0-9_\-]{4,}"),
    # токен Telegram-бота: 10 цифр, двоеточие, 35 символов (может стоять вплотную к /bot)
    re.compile(r"\d{8,12}:[A-Za-z0-9_\-]{30,}"),
    re.compile(r"(?i)(password|token|secret|api[_-]?key)\s*[=:]\s*[^\s,;]+"),
)


_REGISTERED: set[str] = set()
_LOCK = threading.Lock()


def register_secret(value: str) -> None:
    """Запоминает конкретное секретное значение (токен без явного префикса).

    Шаблоны ловят `sk-…`, `GOCSPX-…`, токены Telegram, но у токенов MAX
    нет сигнатуры — поэтому вырезаем их по точному значению.
    """
    if value and len(value) >= 8:
        with _LOCK:
            _REGISTERED.add(value)


def register_settings_secrets(settings) -> None:
    for name in (
        "llm_api_key",
        "telegram_bot_token",
        "max_bot_token",
        "oauth_client_secret",
        "imap_password",
        "api_key",
        "ui_password",
        "session_secret",
        # не только «секрет приложения», но и пароли интеграций: они тоже
        # попадают в тексты исключений (SMTP, OData, ключ HTTP-сервиса 1С)
        "smtp_password",
        "one_c_password",
        "one_c_service_token",
        "secret_key",
    ):
        register_secret(str(getattr(settings, name, "") or ""))


def redact(text: str) -> str:
    value = str(text)
    with _LOCK:
        registered = sorted(_REGISTERED, key=len, reverse=True)
    for secret in registered:
        value = value.replace(secret, "***")
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("***", value)
    return value


class RedactingFormatter(logging.Formatter):
    """Форматтер, который вычищает секреты из готовой строки лога."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def mask(value: str) -> str:
    """Для диагностики: показать, что ключ есть, но не показать ключ."""
    if not value:
        return "не задан"
    tail = value[-4:] if len(value) >= 8 else ""
    return f"задан (***{tail})"