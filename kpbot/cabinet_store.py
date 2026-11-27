"""Данные личного кабинета клиента (B2B-портал покупателя).

Живёт в базе поставщика (tenant): у каждой установки/клиента поставщика своя
база, поэтому данные покупателей физически разделены. Внутри базы доступ
всегда ограничен company_id из сессии.

Покупатель (компания) — тот, кто закупается у поставщика. У компании несколько
пользователей: client_admin (финансы) и client_manager (каталог и заказы).
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .users import check_role, hash_password, verify_password

CLIENT_ROLES = ("client_admin", "client_manager")
ORDER_STATUSES = ("pending", "processing", "shipped", "cancelled")
ORDER_STATUS_LABELS = {
    "pending": "Ожидает",
    "processing": "В обработке",
    "shipped": "Отгружен",
    "cancelled": "Отменён",
}

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS cabinet_companies(
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    inn               TEXT NOT NULL DEFAULT '',
    kpp               TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'active',
    balance           TEXT NOT NULL DEFAULT '0',
    credit_limit      TEXT NOT NULL DEFAULT '0',
    debt              TEXT NOT NULL DEFAULT '0',
    finance_updated_at TEXT,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cabinet_users(
    id            TEXT PRIMARY KEY,
    company_id    TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL DEFAULT '',
    role          TEXT NOT NULL DEFAULT 'client_manager',
    salt          TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    deleted_at    TEXT,
    password_changed_at TEXT,
    FOREIGN KEY(company_id) REFERENCES cabinet_companies(id)
);

CREATE TABLE IF NOT EXISTS cabinet_prices(
    company_id TEXT NOT NULL,
    sku        TEXT NOT NULL,
    price      TEXT NOT NULL,
    PRIMARY KEY(company_id, sku)
);

CREATE TABLE IF NOT EXISTS cabinet_packs(
    sku      TEXT PRIMARY KEY,
    pack_qty INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS cabinet_order_events(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   TEXT NOT NULL,
    company_id TEXT NOT NULL,
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_order_events_company ON cabinet_order_events(company_id, id);

CREATE TABLE IF NOT EXISTS cabinet_cart(
    user_id    TEXT NOT NULL,
    sku        TEXT NOT NULL,
    qty        INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(user_id, sku)
);

CREATE TABLE IF NOT EXISTS cabinet_orders(
    id         TEXT PRIMARY KEY,
    company_id TEXT NOT NULL,
    created_by TEXT NOT NULL,
    number     TEXT NOT NULL UNIQUE,
    status     TEXT NOT NULL DEFAULT 'pending',
    comment    TEXT NOT NULL DEFAULT '',
    total      TEXT NOT NULL DEFAULT '0',
    rfq_id     TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(company_id) REFERENCES cabinet_companies(id)
);

CREATE TABLE IF NOT EXISTS cabinet_order_lines(
    order_id TEXT NOT NULL,
    sku      TEXT NOT NULL,
    name     TEXT NOT NULL,
    qty      INTEGER NOT NULL,
    price    TEXT NOT NULL,
    amount   TEXT NOT NULL,
    FOREIGN KEY(order_id) REFERENCES cabinet_orders(id)
);

CREATE INDEX IF NOT EXISTS cabinet_orders_company ON cabinet_orders(company_id, created_at);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _number(value: object, default: str = "0") -> str:
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except (InvalidOperation, ValueError, TypeError):
        return default


class CabinetStore:
    """SQLite-хранилище ЛК: компании, пользователи, корзина, заказы."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def init_schema(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.executescript(SCHEMA)
            # базы, созданные до появления поля, получают его мягкой миграцией
            try:
                conn.execute("ALTER TABLE cabinet_users ADD COLUMN password_changed_at TEXT")
            except sqlite3.OperationalError:
                pass

    # ---------- компании ----------

    def create_company(self, name: str, *, inn: str = "", kpp: str = "", status: str = "active") -> dict:
        self.init_schema()
        company_id = uuid.uuid4().hex
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO cabinet_companies(id, name, inn, kpp, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (company_id, name.strip() or "Клиент", inn.strip(), kpp.strip(), status, _utcnow()),
            )
        return self.company(company_id) or {}

    def company(self, company_id: str) -> dict | None:
        self.init_schema()
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM cabinet_companies WHERE id = ?", (company_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_companies(self) -> list[dict]:
        self.init_schema()
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM cabinet_companies ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def update_finance(
        self,
        company_id: str,
        *,
        balance: object | None = None,
        credit_limit: object | None = None,
        debt: object | None = None,
        status: str | None = None,
    ) -> None:
        values: dict[str, object] = {}
        if balance is not None:
            values["balance"] = _number(balance)
        if credit_limit is not None:
            values["credit_limit"] = _number(credit_limit)
        if debt is not None:
            values["debt"] = _number(debt)
        if status in ("active", "blocked"):
            values["status"] = status
        if not values:
            return
        values["finance_updated_at"] = _utcnow()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                f"UPDATE cabinet_companies SET {assignments} WHERE id = ?",
                (*values.values(), company_id),
            )

    def set_company_status(self, company_id: str, status: str) -> None:
        if status not in ("active", "blocked"):
            raise ValueError("статус компании: active или blocked")
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE cabinet_companies SET status = ?, finance_updated_at = ? WHERE id = ?",
                (status, _utcnow(), company_id),
            )

    # ---------- пользователи ЛК ----------

    def create_user(
        self,
        company_id: str,
        *,
        email: str,
        password: str,
        role: str = "client_manager",
        name: str = "",
    ) -> dict:
        self.init_schema()
        email = (email or "").strip().lower()
        if "@" not in email:
            raise ValueError("нужен корректный e-mail")
        if len(password or "") < 8:
            raise ValueError("пароль короче 8 символов")
        if role not in CLIENT_ROLES:
            raise ValueError("роль: client_admin или client_manager")
        salt, digest = hash_password(password)
        user_id = uuid.uuid4().hex
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO cabinet_users(id, company_id, email, name, role, salt,"
                " password_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, company_id, email, name.strip(), role, salt, digest, _utcnow()),
            )
        return self.user_by_id(user_id) or {}

    def authenticate(self, email: str, password: str) -> dict | None:
        self.init_schema()
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM cabinet_users WHERE email = ? AND deleted_at IS NULL",
                ((email or "").strip().lower(),),
            ).fetchone()
        if row is None or not verify_password(password, row["salt"], row["password_hash"]):
            return None
        user = dict(row)
        user.pop("salt", None)
        user.pop("password_hash", None)
        return user

    def set_password(self, user_id: str, password: str) -> None:
        """Меняет пароль и обрывает ранее выданные cookie (password_changed_at)."""
        if len(password or "") < 8:
            raise ValueError("пароль короче 8 символов")
        salt, digest = hash_password(password)
        self.init_schema()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE cabinet_users SET salt = ?, password_hash = ?, password_changed_at = ?"
                " WHERE id = ?",
                (salt, digest, _utcnow(), user_id),
            )

    def user_by_id(self, user_id: str) -> dict | None:
        self.init_schema()
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM cabinet_users WHERE id = ? AND deleted_at IS NULL", (user_id,)
            ).fetchone()
        if row is None:
            return None
        user = dict(row)
        user.pop("salt", None)
        user.pop("password_hash", None)
        return user

    def list_users(self, company_id: str) -> list[dict]:
        self.init_schema()
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, email, name, role, created_at FROM cabinet_users"
                " WHERE company_id = ? AND deleted_at IS NULL ORDER BY role, email",
                (company_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_user(self, user_id: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE cabinet_users SET deleted_at = ? WHERE id = ?", (_utcnow(), user_id))

    # ---------- персональные цены и кратность ----------

    def set_price(self, company_id: str, sku: str, price: object) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO cabinet_prices(company_id, sku, price) VALUES (?, ?, ?)"
                " ON CONFLICT(company_id, sku) DO UPDATE SET price = excluded.price",
                (company_id, sku, _number(price)),
            )

    def price_map(self, company_id: str) -> dict[str, str]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT sku, price FROM cabinet_prices WHERE company_id = ?", (company_id,)
            ).fetchall()
        return {row["sku"]: row["price"] for row in rows}

    def set_pack(self, sku: str, qty: int) -> None:
        qty = max(1, int(qty))
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO cabinet_packs(sku, pack_qty) VALUES (?, ?)"
                " ON CONFLICT(sku) DO UPDATE SET pack_qty = excluded.pack_qty",
                (sku, qty),
            )

    def pack_map(self) -> dict[str, int]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT sku, pack_qty FROM cabinet_packs").fetchall()
        return {row["sku"]: int(row["pack_qty"]) for row in rows}

    # ---------- корзина ----------

    def cart_items(self, user_id: str) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT sku, qty FROM cabinet_cart WHERE user_id = ? ORDER BY created_at", (user_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def cart_qty(self, user_id: str, sku: str) -> int:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT qty FROM cabinet_cart WHERE user_id = ? AND sku = ?", (user_id, sku)
            ).fetchone()
        return int(row["qty"]) if row else 0

    def set_cart_qty(self, user_id: str, sku: str, qty: int) -> None:
        qty = int(qty)
        with closing(self._connect()) as conn, conn:
            if qty <= 0:
                conn.execute(
                    "DELETE FROM cabinet_cart WHERE user_id = ? AND sku = ?", (user_id, sku)
                )
            else:
                conn.execute(
                    "INSERT INTO cabinet_cart(user_id, sku, qty, created_at) VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(user_id, sku) DO UPDATE SET qty = excluded.qty",
                    (user_id, sku, qty, _utcnow()),
                )

    def clear_cart(self, user_id: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM cabinet_cart WHERE user_id = ?", (user_id,))

    # ---------- заказы ----------

    def next_number(self) -> str:
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM cabinet_orders WHERE number LIKE ?", (f"ЛК-{day}-%",)
            ).fetchone()
        return f"ЛК-{day}-{int(row['c']) + 1:03d}"

    def create_order(
        self,
        *,
        company_id: str,
        created_by: str,
        number: str,
        comment: str,
        total: str,
        lines: list[dict],
        rfq_id: str = "",
    ) -> dict:
        order_id = uuid.uuid4().hex
        now = _utcnow()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO cabinet_orders(id, company_id, created_by, number, status, comment,"
                " total, rfq_id, created_at, updated_at) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
                (order_id, company_id, created_by, number, comment, total, rfq_id, now, now),
            )
            conn.executemany(
                "INSERT INTO cabinet_order_lines(order_id, sku, name, qty, price, amount)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (order_id, line["sku"], line["name"], line["qty"], line["price"], line["amount"])
                    for line in lines
                ],
            )
        return self.order(order_id) or {}

    def order(self, order_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM cabinet_orders WHERE id = ?", (order_id,)
            ).fetchone()
            if row is None:
                return None
            lines = conn.execute(
                "SELECT sku, name, qty, price, amount FROM cabinet_order_lines"
                " WHERE order_id = ? ORDER BY rowid",
                (order_id,),
            ).fetchall()
        order = dict(row)
        order["lines"] = [dict(line) for line in lines]
        order["status_label"] = ORDER_STATUS_LABELS.get(order["status"], order["status"])
        return order

    def list_orders(self, company_id: str, limit: int = 100) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT o.*, (SELECT COUNT(*) FROM cabinet_order_lines l WHERE l.order_id = o.id)"
                " AS line_count FROM cabinet_orders o WHERE o.company_id = ?"
                " ORDER BY o.created_at DESC LIMIT ?",
                (company_id, max(1, min(int(limit), 500))),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["status_label"] = ORDER_STATUS_LABELS.get(item["status"], item["status"])
            result.append(item)
        return result

    def all_orders(self, limit: int = 200) -> list[dict]:
        """Заказы всех компаний — для админки поставщика."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT o.*, c.name AS company_name,"
                " (SELECT COUNT(*) FROM cabinet_order_lines l WHERE l.order_id = o.id) AS line_count"
                " FROM cabinet_orders o JOIN cabinet_companies c ON c.id = o.company_id"
                " ORDER BY o.created_at DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["status_label"] = ORDER_STATUS_LABELS.get(item["status"], item["status"])
            result.append(item)
        return result

    def update_status(self, order_id: str, status: str) -> None:
        if status not in ORDER_STATUSES:
            raise ValueError("неизвестный статус заказа")
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE cabinet_orders SET status = ?, updated_at = ? WHERE id = ?",
                (status, _utcnow(), order_id),
            )

    def log_order_event(self, order_id: str, company_id: str, status: str) -> None:
        """Событие для клиента: «заказ такой-то стал отгружен»."""
        self.init_schema()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO cabinet_order_events(order_id, company_id, status, created_at)"
                " VALUES (?, ?, ?, ?)",
                (order_id, company_id, status, _utcnow()),
            )

    def recent_order_events(self, company_id: str, limit: int = 10) -> list[dict]:
        self.init_schema()
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT e.*, o.number FROM cabinet_order_events e"
                " JOIN cabinet_orders o ON o.id = e.order_id"
                " WHERE e.company_id = ? ORDER BY e.id DESC LIMIT ?",
                (company_id, max(1, min(int(limit), 50))),
            ).fetchall()
        result: list[dict] = []
        for row in rows:
            item = dict(row)
            item["status_label"] = ORDER_STATUS_LABELS.get(item["status"], item["status"])
            result.append(item)
        return result
