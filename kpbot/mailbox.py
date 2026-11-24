"""IMAP-адаптер: забирает новые письма и не помечает их прочитанными до обработки.

Поддерживает две схемы входа:
  * XOAUTH2 (Gmail без «пароля приложения») — предпочтительно;
  * LOGIN с паролем приложения (Yandex, Mail.ru, корпоративный IMAP).

Используется UID-доступ: порядковые номера съезжают при удалении писем, UID — нет.
Тело читается через BODY.PEEK[], поэтому просмотр не делает письмо прочитанным.
"""

from __future__ import annotations

import imaplib
import logging
import ssl
from dataclasses import dataclass

from .oauth import auth_string

log = logging.getLogger("kpbot.mailbox")


def _decode_header(raw: bytes) -> str:
    """Декодирует MIME-заголовок: RFC 2047 (=?utf-8?b?...?=) и обычный utf-8."""
    if not raw:
        return ""
    try:
        from email.header import decode_header, make_header

        return str(make_header(decode_header(raw.decode("utf-8", "replace"))))
    except Exception:  # noqa: BLE001 - битый заголовок не должен ломать поиск
        return raw.decode("utf-8", "replace")


class MailError(RuntimeError):
    """Ошибка подключения/авторизации с человеческим текстом."""


@dataclass(slots=True)
class ImapConfig:
    host: str
    user: str
    password: str = ""
    access_token: str = ""
    port: int = 993
    folder: str = "INBOX"


class ImapMailbox:
    def __init__(self, config: ImapConfig, timeout: int = 20) -> None:
        if not config.user:
            raise MailError("не задан KPBOT_IMAP_USER — почта не подключена")
        if not config.password and not config.access_token:
            raise MailError(
                "нет способа войти в почту: нужен либо пароль приложения "
                "(KPBOT_IMAP_PASSWORD), либо OAuth-токен (`python -m kpbot.cli oauth-login`)"
            )
        self.config = config
        self.timeout = timeout

    @property
    def auth_mode(self) -> str:
        return "XOAUTH2" if self.config.access_token else "LOGIN"

    # ---------- служебное ----------

    def _connect(self) -> imaplib.IMAP4_SSL:
        try:
            client = imaplib.IMAP4_SSL(
                self.config.host,
                self.config.port,
                ssl_context=ssl.create_default_context(),
                timeout=self.timeout,
            )
        except OSError as exc:
            raise MailError(f"нет соединения с {self.config.host}:{self.config.port}: {exc}") from exc

        try:
            if self.config.access_token:
                client.authenticate(
                    "XOAUTH2",
                    lambda _challenge: auth_string(self.config.user, self.config.access_token),
                )
            else:
                client.login(self.config.user, self.config.password)
        except imaplib.IMAP4.error as exc:
            raise MailError(self._explain(str(exc))) from exc

        return client

    @staticmethod
    def _explain(message: str) -> str:
        if "Application-specific password required" in message:
            return (
                "Gmail требует пароль приложения, но в аккаунте он недоступен "
                "(включена защита аккаунта, политика Workspace или вход без пароля). "
                "Используйте OAuth: `python -m kpbot.cli oauth-login`"
            )
        if "AUTHENTICATIONFAILED" in message or "Invalid credentials" in message:
            return "неверный логин или пароль приложения"
        if "Invalid SASL" in message or "OAUTH" in message.upper():
            return "OAuth-токен отклонён — получите новый: `python -m kpbot.cli oauth-login`"
        return f"ошибка входа в почту: {message}"

    # ---------- операции ----------

    def check(self) -> dict:
        """Проверка доступа: сколько писем в ящике. Только чтение."""
        client = self._connect()
        try:
            status, data = client.select(self.config.folder, readonly=True)
            if status != "OK":
                raise MailError(f"не удалось открыть папку {self.config.folder}: {data}")
            total = int(data[0])
            status, unseen = client.uid("SEARCH", None, "UNSEEN")
            unread = len(unseen[0].split()) if status == "OK" and unseen and unseen[0] else 0
            return {
                "folder": self.config.folder,
                "total": total,
                "unseen": unread,
                "auth": self.auth_mode,
            }
        finally:
            self._safe_logout(client)

    def fetch_unseen(self, limit: int = 10) -> list[tuple[str, bytes]]:
        """Непрочитанные письма, старые первыми. Ящик открыт на запись (позже ставим \\Seen)."""
        return self.fetch_recent(limit, unseen_only=True, readonly=False)

    def fetch_recent(
        self,
        limit: int = 10,
        *,
        unseen_only: bool = False,
        readonly: bool = True,
        subject_contains: str = "",
    ) -> list[tuple[str, bytes]]:
        """Письма последние `limit` штук. readonly=True — ящик вообще не меняется.

        Фильтр по теме делается локально: серверный SEARCH с кириллицей требует
        CHARSET UTF-8, который поддерживают не все IMAP-серверы, а заголовки
        последних писем дешевле прочитать и сравнить самим.
        """
        limit = max(1, min(int(limit), 50))
        client = self._connect()
        try:
            status, data = client.select(self.config.folder, readonly=readonly)
            if status != "OK":
                raise MailError(f"не удалось открыть папку {self.config.folder}: {data}")

            criteria = "UNSEEN" if unseen_only else "ALL"
            status, found = client.uid("SEARCH", None, criteria)
            if status != "OK":
                raise MailError("поиск писем не удался")
            uids = found[0].split() if found and found[0] else []

            if subject_contains:
                uids = self._filter_by_subject(client, uids, subject_contains)

            uids = uids[-limit:] if len(uids) > limit else uids

            messages: list[tuple[str, bytes]] = []
            for uid in uids:
                status, payload = client.uid("FETCH", uid, "(BODY.PEEK[])")
                if status != "OK" or not payload:
                    continue
                raw = b"".join(
                    part[1] for part in payload if isinstance(part, tuple) and len(part) == 2
                )
                if raw:
                    messages.append((uid.decode(), raw))
            return messages
        finally:
            self._safe_logout(client)

    def _filter_by_subject(
        self, client: imaplib.IMAP4_SSL, uids: list[bytes], needle: str, scan: int = 120
    ) -> list[bytes]:
        needle_low = needle.casefold()
        matched: list[bytes] = []
        for uid in uids[-scan:]:
            status, payload = client.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
            if status != "OK" or not payload:
                continue
            raw = b"".join(
                part[1] for part in payload if isinstance(part, tuple) and len(part) == 2
            )
            if needle_low in _decode_header(raw).casefold():
                matched.append(uid)
        return matched

    def mark_seen(self, uids: list[str]) -> None:
        if not uids:
            return
        client = self._connect()
        try:
            client.select(self.config.folder, readonly=False)
            for uid in uids:
                client.uid("STORE", uid, "+FLAGS", "(\\Seen)")
        finally:
            self._safe_logout(client)

    def ensure_folder(self, name: str) -> None:
        """Создаёт папку, если её нет. Существующая папка — не ошибка."""
        client = self._connect()
        try:
            client.create(name)
        except imaplib.IMAP4.error:
            log.debug("папка %s уже существует", name, exc_info=True)
        finally:
            self._safe_logout(client)

    def append(self, folder: str, messages: list[bytes]) -> int:
        """Кладёт письма в папку как непрочитанные. Возвращает количество."""
        if not messages:
            return 0
        client = self._connect()
        try:
            added = 0
            for raw in messages:
                status, _ = client.append(folder, None, None, raw)
                if status == "OK":
                    added += 1
            return added
        finally:
            self._safe_logout(client)

    @staticmethod
    def _safe_logout(client: imaplib.IMAP4_SSL) -> None:
        try:
            client.logout()
        except Exception:  # noqa: BLE001 - разрыв соединения при выходе не критичен
            log.debug("не удалось корректно закрыть IMAP-сессию", exc_info=True)