"""Доступ: API-ключ, подписанная сессия, CSRF, простой rate limit.

Прототип намеренно не тащит JWT и OAuth: сессия — HMAC-подписанная метка
времени в HttpOnly-cookie, CSRF — отдельный HMAC от сессии.
В проде: argon2id для паролей, серверные сессии в Redis, rate limit в Redis.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from collections import deque

SESSION_COOKIE = "kpbot_session"
CSRF_FIELD = "csrf_token"


def _hmac(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def make_session(secret: str, ttl: int) -> str:
    issued = int(time.time())
    payload = str(issued)
    return f"{payload}.{_hmac(secret, payload)}"


def verify_session(secret: str, token: str | None, ttl: int) -> bool:
    if not token or "." not in token:
        return False
    payload, signature = token.rsplit(".", 1)
    if not payload.isdigit():
        return False
    issued = int(payload)
    if issued > time.time() + 60:  # часы в будущем — отвергаем
        return False
    if time.time() - issued > ttl:
        return False
    return constant_time_equal(_hmac(secret, payload), signature)


def csrf_for(secret: str, session: str | None) -> str:
    return _hmac(secret, f"csrf:{session or 'anonymous'}")


def verify_csrf(secret: str, session: str | None, token: str | None) -> bool:
    if not token:
        return False
    return constant_time_equal(csrf_for(secret, session), token)


class RateLimiter:
    """Скользящее окно в памяти. Для одного процесса этого достаточно."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = max(1, per_minute)
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        window = 60.0
        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            while bucket and now - bucket[0] > window:
                bucket.popleft()
            if len(bucket) >= self.per_minute:
                return False
            bucket.append(now)
            if len(self._hits) > 10_000:  # защита от роста памяти
                self._hits.clear()
            return True