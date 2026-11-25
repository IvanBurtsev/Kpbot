"""Запрет реальных сетевых вызовов в тестовом режиме (KPBOT_OFFLINE=1).

Зачем: тесты не должны ходить боевыми ключами в Telegram, LLM, почту или 1С.
Раньше запуск `unittest` отправлял настоящие сообщения и тратил токены —
этот модуль закрывает такую возможность на уровне транспорта.

Как это устроено:
* подменяются обработчики httpx, точки подключения smtplib и imaplib;
* локальные адреса (127.0.0.1, localhost) разрешены — тесты поднимают
  собственный SMTP-приёмник и не должны из-за этого падать;
* тесты, которым нужен «успешный» ответ, передают `httpx.MockTransport`:
  он не использует `HTTPTransport`, поэтому под запрет не попадает.

Запрет включается только при `KPBOT_OFFLINE=1`, в обычном запуске это no-op.
"""

from __future__ import annotations

import imaplib
import ipaddress
import smtplib

import httpx

_LOCAL_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1", "testserver", "test"})


def is_local(host: str | None) -> bool:
    """Локальный ли адрес: петля или имя тестового стенда."""
    host = (host or "").strip().strip("[]").lower()
    if host in _LOCAL_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# Оригиналы сохраняем до подмены: локальные вызовы должны работать как раньше.
_http_handle = httpx.HTTPTransport.handle_request
_http_handle_async = httpx.AsyncHTTPTransport.handle_async_request
_smtp_connect = smtplib.SMTP.connect
_imap_open = imaplib.IMAP4.open

_installed = False


def _guard_http(self, request: httpx.Request) -> httpx.Response:
    if not is_local(request.url.host):
        raise httpx.ConnectError(
            f"сеть запрещена в тестах (KPBOT_OFFLINE=1): {request.method} {request.url.host}",
            request=request,
        )
    return _http_handle(self, request)


async def _guard_http_async(self, request: httpx.Request) -> httpx.Response:
    if not is_local(request.url.host):
        raise httpx.ConnectError(
            f"сеть запрещена в тестах (KPBOT_OFFLINE=1): {request.method} {request.url.host}",
            request=request,
        )
    return await _http_handle_async(self, request)


def _guard_smtp(self, host: str = "localhost", port: int = 0, source_address=None):
    if not is_local(host):
        raise ConnectionRefusedError(f"SMTP запрещён в тестах (KPBOT_OFFLINE=1): {host}")
    return _smtp_connect(self, host, port, source_address)


def _guard_imap(self, host: str = "", port: int = 143):
    if not is_local(host):
        raise ConnectionRefusedError(f"IMAP запрещён в тестах (KPBOT_OFFLINE=1): {host}")
    return _imap_open(self, host, port)


def install_offline_guard() -> None:
    """Включает запрет. Повторный вызов безопасен."""
    global _installed
    if _installed:
        return
    httpx.HTTPTransport.handle_request = _guard_http
    httpx.AsyncHTTPTransport.handle_async_request = _guard_http_async
    smtplib.SMTP.connect = _guard_smtp
    imaplib.IMAP4.open = _guard_imap
    _installed = True
