"""Шифрование секретов, которые лежат в базе клиента.

Зачем: пароли почтовых ящиков клиентов хранятся в их SQLite-базах. Копия базы
(бэкап, украденный диск, случайно отправленный архив) не должна раскрывать
пароли. Ключ живёт вне базы — в `.env` (`KPBOT_SECRET_KEY`) или, если он не
задан, в секрете сессии.

Формат: `enc:v1:<token>` (Fernet: AES-128-CBC + HMAC-SHA256). Значения без
префикса читаются как есть — старые установки продолжают работать, а при
следующем `mail-setup` пароль шифруется.

Честная граница: это защита от утечки базы без `.env`, а не от того, кто
читает оба файла. Так и написано в docs/ARCHITECTURE.md.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "enc:v1:"


class SecretError(RuntimeError):
    """Секрет не расшифровывается: сменился ключ или данные повреждены."""


def _fernet(secret: str) -> Fernet:
    if not secret:
        raise SecretError("не задан ключ шифрования: KPBOT_SECRET_KEY или KPBOT_SESSION_SECRET")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def is_encrypted(value: str | None) -> bool:
    return bool(value) and str(value).startswith(PREFIX)


def encrypt(value: str, secret: str) -> str:
    """Шифрует значение. Пустое и уже зашифрованное возвращает как есть."""
    if not value or is_encrypted(value):
        return value
    token = _fernet(secret).encrypt(value.encode("utf-8")).decode("ascii")
    return f"{PREFIX}{token}"


def decrypt(value: str, secret: str) -> str:
    """Расшифровывает. Старое значение без префикса возвращает без изменений."""
    if not value or not is_encrypted(value):
        return value
    try:
        return _fernet(secret).decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretError(
            "секрет не расшифровывается: изменился KPBOT_SECRET_KEY "
            "(верните прежний ключ или задайте пароль заново через mail-setup)"
        ) from exc
