"""Доступ: API-ключ, подписанная сессия, CSRF, простой rate limit.

Сервис намеренно не тащит JWT и OAuth: сессия — HMAC-подписанная метка
времени в HttpOnly-cookie, CSRF — отдельный HMAC от сессии.
В проде: argon2id для паролей, серверные сессии в Redis, rate limit в Redis.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from collections import deque
from typing import Any

SESSION_COOKIE = "kpbot_session"
CSRF_FIELD = "csrf_token"


def client_ip(request: Any, trusted_proxies: set[str]) -> str:
    """Адрес клиента с учётом X-Forwarded-For только от доверенного прокси.

    Без списка доверенных адресов заголовок игнорируется: иначе любой клиент
    подставит чужой IP и обойдёт лимит попыток входа.
    """
    peer = request.client.host if getattr(request, "client", None) else "unknown"
    if peer in trusted_proxies:
        # Заголовок дописывается слева направо: правый адрес поставил ближайший
        # доверенный прокси, левые может подставить кто угодно.
        forwarded = [
            part.strip()
            for part in (request.headers.get("x-forwarded-for") or "").split(",")
            if part.strip()
        ]
        for address in reversed(forwarded):
            if address not in trusted_proxies:
                return address[:64]
    return peer


def same_origin(request: Any, public_base_url: str) -> bool:
    """Запрос пришёл со страницы того же сайта.

    Браузер присылает Origin на POST. Если он есть и не совпадает с публичным
    адресом — это login CSRF (чужой сайт авторизует жертву в своём аккаунте).
    Отсутствие заголовка не блокируем: старые клиенты и curl его не шлют.
    """
    origin = (request.headers.get("origin") or "").strip().rstrip("/")
    if not origin:
        return True
    return origin == (public_base_url or "").strip().rstrip("/")


def _hmac(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def make_session(secret: str, ttl: int) -> str:
    issued = int(time.time())
    payload = str(issued)
    return f"{payload}.{_hmac(secret, payload)}"


def make_user_session(secret: str, user_id: str, ttl: int) -> str:
    """Сессия конкретного пользователя: uid.время.подпись."""
    payload = f"{user_id}.{int(time.time())}"
    return f"{payload}.{_hmac(secret, payload)}"


def _session_payload(secret: str, token: str | None, ttl: int) -> tuple[str, int] | None:
    """Проверяет подпись и срок; возвращает (id пользователя, время выдачи)."""
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    user_id, issued, signature = parts
    if not issued.isdigit():
        return None
    if not constant_time_equal(_hmac(secret, f"{user_id}.{issued}"), signature):
        return None
    issued_at = int(issued)
    if issued_at > time.time() + 60 or time.time() - issued_at > ttl:
        return None
    return user_id, issued_at


def user_from_session(secret: str, token: str | None, ttl: int) -> str | None:
    """Возвращает id пользователя, если подпись и срок в порядке."""
    payload = _session_payload(secret, token, ttl)
    return payload[0] if payload else None


def session_issued_at(secret: str, token: str | None, ttl: int) -> int | None:
    """Когда выдан токен: нужно, чтобы отозвать сессии после смены пароля."""
    payload = _session_payload(secret, token, ttl)
    return payload[1] if payload else None


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
            if len(self._hits) > 10_000:
                # Не сбрасываем счётчики всем (это способ обойти лимит), а выкидываем
                # только записи, которые уже не влияют на окно, и самые старые.
                stale = [k for k, hits in self._hits.items() if not hits or now - hits[-1] > window]
                for stale_key in stale:
                    self._hits.pop(stale_key, None)
                if len(self._hits) > 10_000:
                    oldest = sorted(self._hits.items(), key=lambda pair: pair[1][-1])
                    for stale_key, _ in oldest[: len(oldest) // 4]:
                        self._hits.pop(stale_key, None)
            return True
