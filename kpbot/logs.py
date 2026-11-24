"""Логи без утечек: ключи не должны попадать в файлы и journalctl."""

from __future__ import annotations

import logging
import re

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"GOCSPX-[A-Za-z0-9_\-]{4,}"),
    re.compile(r"(?i)(password|token|secret|api[_-]?key)\s*[=:]\s*[^\s,;]+"),
)


def redact(text: str) -> str:
    value = str(text)
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