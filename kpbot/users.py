"""Пользователи и роли.

Пароли хранятся как PBKDF2-HMAC-SHA256 с солью (stdlib, без внешних зависимостей).
Роли простые: admin видит и правит всё, manager — только заявки на себе и свободные.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

ITERATIONS = 200_000
ROLE_ADMIN = "admin"
ROLE_MANAGER = "manager"
ROLES = (ROLE_ADMIN, ROLE_MANAGER)


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """Возвращает (соль, хеш). Пароль в открытом виде нигде не сохраняется."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), ITERATIONS)
    return salt, digest.hex()


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    _, actual = hash_password(password, salt)
    return hmac.compare_digest(actual, expected_hash)


@dataclass(frozen=True, slots=True)
class User:
    id: str
    email: str
    role: str
    name: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def title(self) -> str:
        return self.name or self.email


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def check_role(role: str) -> str:
    role = (role or ROLE_MANAGER).strip().lower()
    if role not in ROLES:
        raise ValueError(f"неизвестная роль: {role}")
    return role