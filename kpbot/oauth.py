"""OAuth2 для Gmail (XOAUTH2) — правильный путь, когда «пароля приложения» нет.

Схема: OAuth-клиент типа «Desktop app» в Google Cloud → локальный redirect на
http://127.0.0.1:<port>/ → обмен кода на токены → refresh-токен в файл → IMAP
аутентифицируется через SASL XOAUTH2. Пароль пользователя нигде не хранится.

Не требует верификации Google, пока приложение в статусе «Testing» и в список
тестовых пользователей добавлен твой же аккаунт.
"""

from __future__ import annotations

import json
import hmac
import logging
import secrets
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

log = logging.getLogger("kpbot.oauth")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
# Полный доступ к почте нужен, чтобы читать и отправлять письма через IMAP/SMTP.
SCOPE = "https://mail.google.com/"


class OAuthError(RuntimeError):
    """Ошибка авторизации с понятным текстом."""


@dataclass(slots=True)
class Token:
    refresh_token: str = ""
    access_token: str = ""
    expires_at: float = 0.0
    email: str = ""

    @property
    def expired(self) -> bool:
        return not self.access_token or time.time() > self.expires_at - 60


class TokenStore:
    """Простое хранилище токена. Файл лежит вне git (`data/` в .gitignore)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Token:
        if not self.path.exists():
            return Token()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return Token()
        return Token(**{k: payload.get(k, "") for k in ("refresh_token", "access_token", "expires_at", "email")})

    def save(self, token: Token) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(token), ensure_ascii=False, indent=2), encoding="utf-8")
        try:  # на Windows ACL ограничить сложно, но попытка дешёвая
            self.path.chmod(0o600)
        except OSError:
            pass


def build_auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    if not client_id:
        raise OAuthError("не задан KPBOT_OAUTH_CLIENT_ID")
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    return f"{AUTH_URL}?{query}"


TOKEN_ERRORS = (
    ("invalid_client", "Client ID и Client secret не совпадают (или клиент удалён) — проверьте .env"),
    ("invalid_grant", "код авторизации просрочен или уже использован — повторите `oauth-login`"),
    ("redirect_uri_mismatch", "redirect_uri не принят: клиент должен быть типа Desktop app"),
    ("unauthorized_client", "клиенту запрещён этот тип входа: пересоздайте его как Desktop app"),
    ("access_denied", "доступ не выдан: на экране согласия нажали «Отмена»"),
)


def _post_token(url: str, data: dict, timeout: float = 20.0) -> dict:
    try:
        response = httpx.post(url, data=data, timeout=timeout)
    except httpx.HTTPError as exc:
        raise OAuthError(f"нет связи с сервером авторизации: {exc}") from exc
    if response.status_code != 200:
        body = response.text[:400]
        for marker, hint in TOKEN_ERRORS:
            if marker in body:
                raise OAuthError(f"{hint} (Google: {marker})")
        raise OAuthError(f"сервер авторизации вернул {response.status_code}: {body}")
    return response.json()


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> Token:
    payload = _post_token(
        TOKEN_URL,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
    )
    return Token(
        refresh_token=payload.get("refresh_token", ""),
        access_token=payload.get("access_token", ""),
        expires_at=time.time() + float(payload.get("expires_in", 0)),
    )


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> Token:
    if not refresh_token:
        raise OAuthError("нет refresh-токена: выполните `python -m kpbot.cli oauth-login`")
    payload = _post_token(
        TOKEN_URL,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )
    return Token(
        refresh_token=refresh_token,
        access_token=payload.get("access_token", ""),
        expires_at=time.time() + float(payload.get("expires_in", 0)),
    )


def ensure_access_token(
    store: TokenStore, client_id: str, client_secret: str
) -> str:
    """Возвращает живой access-токен, обновляя его при необходимости."""
    token = store.load()
    if not token.expired:
        return token.access_token
    if not token.refresh_token:
        raise OAuthError("нет сохранённого токена: выполните `python -m kpbot.cli oauth-login`")
    fresh = refresh_access_token(client_id, client_secret, token.refresh_token)
    fresh.email = token.email
    store.save(fresh)
    return fresh.access_token


class _CodeReceiver(BaseHTTPRequestHandler):
    server_version = "kpbot-oauth/1.0"

    def do_GET(self) -> None:  # noqa: N802 - имя метода задаёт stdlib
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        code = (params.get("code") or [""])[0]
        error = (params.get("error") or [""])[0]
        state = (params.get("state") or [""])[0]
        expected = getattr(self.server, "expected_state", "")
        # state привязывает ответ к нашему запросу: без проверки в 5-минутное
        # окно можно подсунуть чужой authorization code.
        if expected and not hmac.compare_digest(state, expected):
            self.server.error = "state_mismatch"
            code = ""
        # favicon и прочие запросы без параметров не должны затирать уже полученный код
        if code:
            self.server.code = code  # type: ignore[attr-defined]
        if error:
            self.server.error = error  # type: ignore[attr-defined]
        body = (
            "<html><meta charset='utf-8'><body style='font-family:sans-serif'>"
            "<h2>Готово</h2><p>Можно закрыть вкладку и вернуться в консоль.</p>"
            "</body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # тишина в консоли
        return


def run_local_flow(
    client_id: str,
    client_secret: str,
    store: TokenStore,
    port: int = 8765,
    *,
    open_browser: bool = True,
) -> Token:
    """Открывает браузер, ловит код на локальном порту и сохраняет refresh-токен."""
    redirect_uri = f"http://127.0.0.1:{port}/"
    state = secrets.token_urlsafe(16)
    url = build_auth_url(client_id, redirect_uri, state)

    try:
        server = HTTPServer(("127.0.0.1", port), _CodeReceiver)
    except OSError as exc:
        raise OAuthError(
            f"порт {port} занят ({exc}). Закройте лишнее приложение или укажите другой KPBOT_OAUTH_REDIRECT_PORT"
        ) from exc

    server.code = ""  # type: ignore[attr-defined]
    server.error = ""  # type: ignore[attr-defined]
    server.expected_state = state  # type: ignore[attr-defined]
    server.timeout = 5

    print("Откройте ссылку и разрешите доступ к почте:", flush=True)
    print(url, flush=True)
    if open_browser:
        webbrowser.open(url)

    deadline = time.time() + 300
    while time.time() < deadline:
        server.handle_request()
        code = getattr(server, "code", "")
        error = getattr(server, "error", "")
        if error:
            raise OAuthError(f"доступ не выдан: {error}")
        if code:
            break
    else:
        raise OAuthError("не дождались подтверждения (5 минут)")

    token = exchange_code(client_id, client_secret, code, redirect_uri)
    if not token.refresh_token:
        raise OAuthError(
            "Google не вернул refresh-токен. Удалите доступ приложения в аккаунте и повторите вход"
        )
    store.save(token)
    return token


def auth_string(user: str, access_token: str) -> bytes:
    """SASL-строка для IMAP AUTHENTICATE XOAUTH2."""
    return f"user={user}\x01auth=Bearer {access_token}\x01\x01".encode()