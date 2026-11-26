"""Контекст текущего клиента (тенанта) на время HTTP-запроса.

Две независимые оси:
  * supplier_db — база поставщика, которую смотрит админка `/ui` (выбирается
    владельцем в `/ui/ops` и живёт в подписанной куке `kpbot_tenant`);
  * client_db — база поставщика, в которую пришёл покупатель в `/cabinet`
    (своя кука `kpbot_client_tenant`, чтобы вход в кабинет и вход в админку
    не перетирали друг друга на одной машине).

Без этих контекстов личный кабинет всегда работал бы с основной базой — то есть
мультитенантность существовала бы только в админке.
"""

from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path

CLIENT_TENANT_COOKIE = "kpbot_client_tenant"

supplier_db: ContextVar[Path | None] = ContextVar("supplier_db", default=None)
client_db: ContextVar[Path | None] = ContextVar("client_db", default=None)


def current_supplier_db(default: Path) -> Path:
    return supplier_db.get() or default


def current_client_db(default: Path) -> Path:
    return client_db.get() or default
