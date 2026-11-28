"""Хранилище на SQLite. Все запросы — параметризованные (?), без склейки строк."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from .domain import PriceItem, Quote, QuoteLine, RfqLine, RfqStatus, check_transition
from .users import ROLE_ADMIN, User, check_role, hash_password, normalize_email, verify_password

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS price_items(
    sku         TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    unit        TEXT NOT NULL,
    price       TEXT NOT NULL,
    currency    TEXT NOT NULL DEFAULT 'RUB',
    vat_rate    TEXT,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS price_meta(
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rfqs(
    id          TEXT PRIMARY KEY,
    message_id  TEXT NOT NULL,
    sender      TEXT NOT NULL,
    subject     TEXT NOT NULL,
    received_at TEXT NOT NULL,
    status      TEXT NOT NULL,
    raw_text    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_rfqs_message ON rfqs(message_id);

CREATE TABLE IF NOT EXISTS rfq_lines(
    rfq_id       TEXT NOT NULL REFERENCES rfqs(id) ON DELETE CASCADE,
    idx          INTEGER NOT NULL,
    raw_text     TEXT NOT NULL,
    qty          TEXT,
    unit         TEXT,
    matched_sku  TEXT,
    matched_name TEXT NOT NULL DEFAULT '',
    score        REAL NOT NULL DEFAULT 0,
    status       TEXT NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (rfq_id, idx)
);

CREATE TABLE IF NOT EXISTS quotes(
    id              TEXT PRIMARY KEY,
    rfq_id          TEXT NOT NULL REFERENCES rfqs(id) ON DELETE CASCADE,
    number          INTEGER,
    subtotal        TEXT NOT NULL,
    vat             TEXT NOT NULL,
    delivery        TEXT NOT NULL,
    total           TEXT NOT NULL,
    vat_rate        TEXT NOT NULL,
    -- Снимок должен помнить режим НДС и НДС с доставки: иначе письмо и
    -- документ, пересобранные из базы, считают итог иначе, чем при создании.
    prices_include_vat INTEGER NOT NULL DEFAULT 0,
    delivery_vat    TEXT NOT NULL DEFAULT '0.00',
    requires_review INTEGER NOT NULL,
    unmatched       INTEGER NOT NULL,
    duration_ms     INTEGER,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_quotes_rfq ON quotes(rfq_id);

CREATE TABLE IF NOT EXISTS quote_lines(
    quote_id TEXT NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
    idx      INTEGER NOT NULL,
    sku      TEXT NOT NULL,
    name     TEXT NOT NULL,
    unit     TEXT NOT NULL,
    qty      TEXT NOT NULL,
    price    TEXT NOT NULL,
    amount   TEXT NOT NULL,
    net      TEXT NOT NULL DEFAULT '0.00',
    vat      TEXT NOT NULL DEFAULT '0.00',
    vat_rate TEXT,
    PRIMARY KEY (quote_id, idx)
);

CREATE TABLE IF NOT EXISTS artifacts(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    rfq_id     TEXT NOT NULL REFERENCES rfqs(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    path       TEXT NOT NULL,
    sha256     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    rfq_id     TEXT NOT NULL REFERENCES rfqs(id) ON DELETE CASCADE,
    action     TEXT NOT NULL,
    actor      TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users(
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL DEFAULT '',
    role          TEXT NOT NULL DEFAULT 'manager',
    salt          TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock(
    sku        TEXT PRIMARY KEY,
    qty        TEXT NOT NULL,
    unit       TEXT NOT NULL DEFAULT '',
    warehouse  TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    rfq_id     TEXT NOT NULL,
    to_addr    TEXT NOT NULL,
    subject    TEXT NOT NULL,
    body       TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    sent_at    TEXT
);

CREATE TABLE IF NOT EXISTS learned(
    key        TEXT PRIMARY KEY,
    sku        TEXT NOT NULL,
    hits       INTEGER NOT NULL DEFAULT 0,
    misses     INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings(
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    action     TEXT NOT NULL,
    actor      TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_usage(
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    rfq_id            TEXT NOT NULL REFERENCES rfqs(id) ON DELETE CASCADE,
    model             TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_rub          TEXT NOT NULL DEFAULT '0',
    balance_rub       TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_llm_usage_rfq ON llm_usage(rfq_id);
"""


def utcnow() -> str:
    """Время с микросекундами: секундной точности не хватает для метрики «письмо → КП»."""
    return datetime.now(timezone.utc).isoformat()


# Схема на файл применяется один раз за жизнь процесса: иначе каждый Store()
# (а их на страницу десятки) гоняет весь DDL и четыре ALTER.
_INIT_LOCK = threading.Lock()
_INITIALIZED: set[str] = set()


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def init_schema(self) -> None:
        key = str(self.db_path.resolve())
        with _INIT_LOCK:
            # Файл могли удалить и создать заново (тесты, восстановление из бэкапа):
            # тогда схему нужно применить снова.
            if key in _INITIALIZED and self.db_path.exists():
                return
            _INITIALIZED.discard(key)
            with closing(self._connect()) as conn, conn:
                conn.executescript(SCHEMA)
                # мягкая миграция для баз, созданных до появления поля
                try:
                    conn.execute("ALTER TABLE llm_usage ADD COLUMN balance_rub TEXT")
                except sqlite3.OperationalError:
                    pass
                try:
                    conn.execute("ALTER TABLE quotes ADD COLUMN duration_ms INTEGER")
                except sqlite3.OperationalError:
                    pass
                try:
                    conn.execute("ALTER TABLE quotes ADD COLUMN number INTEGER")
                except sqlite3.OperationalError:
                    pass
                try:
                    conn.execute(
                        "ALTER TABLE quotes ADD COLUMN prices_include_vat INTEGER NOT NULL DEFAULT 0"
                    )
                except sqlite3.OperationalError:
                    pass
                try:
                    conn.execute(
                        "ALTER TABLE quotes ADD COLUMN delivery_vat TEXT NOT NULL DEFAULT '0.00'"
                    )
                except sqlite3.OperationalError:
                    pass
                try:
                    conn.execute("ALTER TABLE quote_lines ADD COLUMN net TEXT NOT NULL DEFAULT '0.00'")
                except sqlite3.OperationalError:
                    pass
                try:
                    conn.execute("ALTER TABLE quote_lines ADD COLUMN vat TEXT NOT NULL DEFAULT '0.00'")
                except sqlite3.OperationalError:
                    pass
                try:
                    conn.execute("ALTER TABLE quote_lines ADD COLUMN vat_rate TEXT")
                except sqlite3.OperationalError:
                    pass
                try:
                    conn.execute("ALTER TABLE price_items ADD COLUMN vat_rate TEXT")
                except sqlite3.OperationalError:
                    pass
                try:
                    conn.execute("ALTER TABLE rfqs ADD COLUMN assignee TEXT")
                except sqlite3.OperationalError:
                    pass
                # мягкое удаление пользователей: история заявок и журнал остаются
                try:
                    conn.execute("ALTER TABLE users ADD COLUMN deleted_at TEXT")
                except sqlite3.OperationalError:
                    pass
                # версия сессии: после смены пароля старые cookie должны умереть
                try:
                    conn.execute("ALTER TABLE users ADD COLUMN password_changed_at TEXT")
                except sqlite3.OperationalError:
                    pass
            _INITIALIZED.add(key)

    # ---------- прайс ----------

    def replace_price_items(self, items: list[PriceItem]) -> int:
        now = utcnow()
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM price_items")
            conn.executemany(
                "INSERT INTO price_items(sku, name, unit, price, currency, vat_rate, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        i.sku, i.name, i.unit, str(i.price), i.currency,
                        str(i.vat_rate) if i.vat_rate is not None else None,
                        now,
                    )
                    for i in items
                ],
            )
            conn.execute(
                "INSERT INTO price_meta(k, v) VALUES ('updated_at', ?)"
                " ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (now,),
            )
        return len(items)

    def price_items(self) -> list[PriceItem]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT sku, name, unit, price, currency, vat_rate FROM price_items ORDER BY sku"
            ).fetchall()
        result: list[PriceItem] = []
        for row in rows:
            raw_rate = row["vat_rate"]
            result.append(
                PriceItem(
                    sku=row["sku"],
                    name=row["name"],
                    unit=row["unit"],
                    price=Decimal(row["price"]),
                    currency=row["currency"],
                    vat_rate=Decimal(str(raw_rate)) if raw_rate not in (None, "") else None,
                )
            )
        return result

    def price_count(self) -> int:
        with closing(self._connect()) as conn:
            return int(conn.execute("SELECT COUNT(*) AS c FROM price_items").fetchone()["c"])

    def price_updated_at(self) -> str:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT v FROM price_meta WHERE k = 'updated_at'").fetchone()
        return row["v"] if row else ""

    # ---------- заявки ----------

    def find_rfq_by_message(self, message_id: str) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT id FROM rfqs WHERE message_id = ?", (message_id,)).fetchone()
        return row["id"] if row else None

    def save_rfq(
        self,
        *,
        rfq_id: str,
        message_id: str,
        sender: str,
        subject: str,
        received_at: str,
        status: str,
        raw_text: str,
        lines: list[RfqLine],
    ) -> str:
        created = utcnow()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO rfqs(id, message_id, sender, subject, received_at, status, raw_text, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (rfq_id, message_id, sender, subject, received_at, status, raw_text, created),
            )
            self._insert_lines(conn, rfq_id, lines)
        return rfq_id

    @staticmethod
    def _insert_lines(conn: sqlite3.Connection, rfq_id: str, lines: list[RfqLine]) -> None:
        conn.executemany(
            "INSERT INTO rfq_lines(rfq_id, idx, raw_text, qty, unit, matched_sku, matched_name, score, status, note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    rfq_id,
                    line.idx,
                    line.raw_text,
                    str(line.qty) if line.qty is not None else None,
                    line.unit,
                    line.matched_sku,
                    line.matched_name,
                    float(line.match_score),
                    line.status.value,
                    line.note,
                )
                for line in lines
            ],
        )

    def replace_lines(self, rfq_id: str, lines: list[RfqLine]) -> None:
        """Полная замена строк заявки — после правок человека."""
        with closing(self._connect()) as conn, conn:
            exists = conn.execute("SELECT 1 FROM rfqs WHERE id = ?", (rfq_id,)).fetchone()
            if not exists:
                raise KeyError(rfq_id)
            conn.execute("DELETE FROM rfq_lines WHERE rfq_id = ?", (rfq_id,))
            self._insert_lines(conn, rfq_id, lines)

    def get_rfq(self, rfq_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            head = conn.execute("SELECT * FROM rfqs WHERE id = ?", (rfq_id,)).fetchone()
            if head is None:
                return None
            lines = conn.execute(
                "SELECT * FROM rfq_lines WHERE rfq_id = ? ORDER BY idx", (rfq_id,)
            ).fetchall()
        return {**dict(head), "lines": [dict(row) for row in lines]}

    def set_status(self, rfq_id: str, status: str, *, force: bool = False) -> None:
        """Меняет статус, проверяя допустимость перехода.

        force=True — только для служебных путей (повтор уже отправленного письма),
        которые обязаны довести заявку до sent, даже если статус разошёлся с данными.
        """
        new_status = RfqStatus(status)
        with closing(self._connect()) as conn, conn:
            row = conn.execute("SELECT status FROM rfqs WHERE id = ?", (rfq_id,)).fetchone()
            if row is None:
                raise KeyError(rfq_id)
            if not force:
                check_transition(row["status"], new_status)
            conn.execute("UPDATE rfqs SET status = ? WHERE id = ?", (new_status.value, rfq_id))

    def list_rfqs(
        self,
        limit: int = 100,
        *,
        assignee: str | None = None,
        include_unassigned: bool = True,
        query: str | None = None,
        status: str | None = None,
        offset: int = 0,
    ) -> list[dict]:
        """Очередь заявок: фильтры, поиск и пагинация.

        assignee=None — без фильтра (админ). Иначе только свои и свободные.
        status="issues" — только заявки, где есть строки для человека.
        """
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        clauses: list[str] = []
        params: list[object] = []
        if assignee is not None:
            if include_unassigned:
                clauses.append("(r.assignee = ? OR r.assignee IS NULL)")
            else:
                clauses.append("r.assignee = ?")
            params.append(assignee)
        if status == "issues":
            clauses.append(
                "EXISTS (SELECT 1 FROM rfq_lines l WHERE l.rfq_id = r.id AND l.status <> 'ok')"
            )
        elif status:
            clauses.append("r.status = ?")
            params.append(status)
        if query:
            like = f"%{query.strip()[:100]}%"
            clauses.append("(r.subject LIKE ? OR r.sender LIKE ?)")
            params.extend([like, like])
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT r.id, r.sender, r.subject, r.received_at, r.status, r.created_at,
                       r.assignee,
                       (SELECT COUNT(*) FROM rfq_lines l WHERE l.rfq_id = r.id) AS line_count,
                       (SELECT COUNT(*) FROM rfq_lines l WHERE l.rfq_id = r.id AND l.status <> 'ok') AS line_issues,
                       (SELECT COUNT(*) FROM rfq_lines l WHERE l.rfq_id = r.id AND l.status = 'ambiguous') AS line_ambiguous,
                       (SELECT COUNT(*) FROM rfq_lines l WHERE l.rfq_id = r.id AND l.status = 'no_match') AS line_no_match,
                       (SELECT COUNT(*) FROM rfq_lines l WHERE l.rfq_id = r.id AND l.status = 'invalid') AS line_invalid,
                       (SELECT q.total FROM quotes q WHERE q.rfq_id = r.id ORDER BY q.rowid DESC LIMIT 1) AS total
                FROM rfqs r
                {where}
                ORDER BY r.created_at DESC
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------- КП ----------

    def save_quote(self, quote_id: str, rfq_id: str, quote: Quote, *, duration_ms: int | None = None) -> int:
        """Сохраняет снимок КП. Номер закрепляется за заявкой и не меняется.

        Номер — это то, на что клиент ссылается в переписке («КП № 12»).
        Правка строк создаёт новую версию расчёта, но номер у неё тот же:
        иначе клиент и менеджер видят разные номера одного документа.
        """
        with closing(self._connect()) as conn, conn:
            existing = conn.execute(
                "SELECT number FROM quotes WHERE rfq_id = ? ORDER BY rowid LIMIT 1",
                (rfq_id,),
            ).fetchone()
            if existing is not None and existing["number"] is not None:
                number = int(existing["number"])
            else:
                row = conn.execute("SELECT COALESCE(MAX(number), 0) + 1 AS n FROM quotes").fetchone()
                number = int(row["n"])
            quote.number = number
            conn.execute(
                "INSERT INTO quotes(id, rfq_id, number, subtotal, vat, delivery, total, vat_rate,"
                " prices_include_vat, delivery_vat, requires_review, unmatched, duration_ms, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    quote_id,
                    rfq_id,
                    number,
                    str(quote.subtotal),
                    str(quote.vat),
                    str(quote.delivery),
                    str(quote.total),
                    str(quote.vat_rate),
                    int(quote.prices_include_vat),
                    str(quote.delivery_vat),
                    int(quote.requires_review),
                    int(quote.unmatched),
                    duration_ms,
                    utcnow(),
                ),
            )
            conn.executemany(
                "INSERT INTO quote_lines(quote_id, idx, sku, name, unit, qty, price, amount, net, vat, vat_rate)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        quote_id,
                        idx,
                        line.sku,
                        line.name,
                        line.unit,
                        str(line.qty),
                        str(line.price),
                        str(line.amount),
                        str(line.net),
                        str(line.vat),
                        str(line.vat_rate) if line.vat_rate is not None else None,
                    )
                    for idx, line in enumerate(quote.lines, start=1)
                ],
            )
        return number

    def get_quote(self, rfq_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            head = conn.execute(
                "SELECT * FROM quotes WHERE rfq_id = ? ORDER BY rowid DESC LIMIT 1", (rfq_id,)
            ).fetchone()
            if head is None:
                return None
            lines = conn.execute(
                "SELECT * FROM quote_lines WHERE quote_id = ? ORDER BY idx", (head["id"],)
            ).fetchall()
        return {**dict(head), "lines": [dict(row) for row in lines]}

    def get_quote_by_id(self, quote_id: str) -> dict | None:
        """Конкретный снимок КП.

        Нужен кабинету: заказ помнит свой расчёт, и документы клиента (счёт,
        УПД) не должны пересчитываться по последней правке заявки поставщиком —
        иначе строки в документе относятся к одной сумме, а «Итого» к другой.
        """
        if not quote_id:
            return None
        with closing(self._connect()) as conn:
            head = conn.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
            if head is None:
                return None
            lines = conn.execute(
                "SELECT * FROM quote_lines WHERE quote_id = ? ORDER BY idx", (quote_id,)
            ).fetchall()
        return {**dict(head), "lines": [dict(row) for row in lines]}

    # ---------- артефакты и журнал ----------

    def add_artifact(self, rfq_id: str, kind: str, path: Path, sha256: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO artifacts(rfq_id, kind, path, sha256, created_at) VALUES (?, ?, ?, ?, ?)",
                (rfq_id, kind, str(path), sha256, utcnow()),
            )

    def artifacts(self, rfq_id: str) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE rfq_id = ? ORDER BY id", (rfq_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def log_event(self, action: str, actor: str = "", detail: str = "") -> None:
        """События вне заявок: загрузка прайса, вход, действия администратора."""
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO events(action, actor, detail, created_at) VALUES (?, ?, ?, ?)",
                (action, actor, detail, utcnow()),
            )

    def log(self, rfq_id: str, action: str, actor: str, detail: str = "") -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO decisions(rfq_id, action, actor, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                (rfq_id, action, actor, detail[:2000], utcnow()),
            )

    def decisions(self, rfq_id: str) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM decisions WHERE rfq_id = ? ORDER BY id", (rfq_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def last_decision(self, rfq_id: str, action: str) -> dict | None:
        """Последнее событие заданного типа: нужно, чтобы не выгружать счёт дважды."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM decisions WHERE rfq_id = ? AND action = ? ORDER BY id DESC LIMIT 1",
                (rfq_id, action),
            ).fetchone()
        return dict(row) if row else None

    # ---------- расход на LLM ----------

    def add_llm_usage(
        self,
        rfq_id: str,
        *,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_rub: str = "0",
        balance_rub: str | None = None,
    ) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO llm_usage(rfq_id, model, prompt_tokens, completion_tokens,"
                " cost_rub, balance_rub, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rfq_id, model, int(prompt_tokens), int(completion_tokens), str(cost_rub),
                 balance_rub, utcnow()),
            )

    def llm_usage_for(self, rfq_id: str) -> dict:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS calls, COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
                " COALESCE(SUM(completion_tokens), 0) AS completion_tokens"
                " FROM llm_usage WHERE rfq_id = ?",
                (rfq_id,),
            ).fetchone()
            cost_row = conn.execute(
                "SELECT COALESCE(SUM(CAST(cost_rub AS REAL)), 0) AS cost FROM llm_usage WHERE rfq_id = ?",
                (rfq_id,),
            ).fetchone()
        return {
            "calls": int(row["calls"] or 0),
            "prompt_tokens": int(row["prompt_tokens"] or 0),
            "completion_tokens": int(row["completion_tokens"] or 0),
            "cost_rub": round(float(cost_row["cost"] or 0), 4),
        }

    def llm_totals(self) -> dict:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS calls, COUNT(DISTINCT rfq_id) AS rfqs,"
                " COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
                " COALESCE(SUM(completion_tokens), 0) AS completion_tokens,"
                " COALESCE(SUM(CAST(cost_rub AS REAL)), 0) AS cost"
                " FROM llm_usage"
            ).fetchone()
            today = conn.execute(
                "SELECT COUNT(*) AS calls, COALESCE(SUM(CAST(cost_rub AS REAL)), 0) AS cost"
                " FROM llm_usage WHERE substr(created_at, 1, 10) = substr(?, 1, 10)",
                (utcnow(),),
            ).fetchone()
            balance = conn.execute(
                "SELECT balance_rub FROM llm_usage WHERE balance_rub IS NOT NULL"
                " ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return {
            "calls": int(row["calls"] or 0),
            "rfqs": int(row["rfqs"] or 0),
            "prompt_tokens": int(row["prompt_tokens"] or 0),
            "completion_tokens": int(row["completion_tokens"] or 0),
            "cost_rub": round(float(row["cost"] or 0), 4),
            "today_calls": int(today["calls"] or 0),
            "today_cost_rub": round(float(today["cost"] or 0), 4),
            "balance_rub": (round(float(balance["balance_rub"]), 2) if balance else None),
        }

    def report(self, days: int = 30) -> dict:
        """Сводка для разговора с клиентом: сколько, на какую сумму, за какое время."""
        since = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).isoformat()
        with closing(self._connect()) as conn:
            head = conn.execute(
                """
                SELECT COUNT(DISTINCT r.id) AS rfqs,
                       COUNT(q.id) AS quotes,
                       COALESCE(SUM(CAST(q.total AS REAL)), 0) AS total_sum
                FROM rfqs r
                LEFT JOIN quotes q ON q.id = (
                    SELECT id FROM quotes WHERE rfq_id = r.id ORDER BY rowid DESC LIMIT 1
                )
                WHERE r.created_at >= ?
                """,
                (since,),
            ).fetchone()
            lines = conn.execute(
                """
                SELECT COUNT(*) AS lines,
                       SUM(CASE WHEN l.status = 'ok' THEN 1 ELSE 0 END) AS auto_lines
                FROM rfq_lines l JOIN rfqs r ON r.id = l.rfq_id
                WHERE r.created_at >= ?
                """,
                (since,),
            ).fetchone()
            cost = conn.execute(
                "SELECT COALESCE(SUM(CAST(cost_rub AS REAL)), 0) AS cost FROM llm_usage WHERE created_at >= ?",
                (since,),
            ).fetchone()
            statuses = conn.execute(
                "SELECT status, COUNT(*) AS n FROM rfqs WHERE created_at >= ? GROUP BY status", (since,)
            ).fetchall()
        timing = self.timing_stats()
        total_lines = int(lines["lines"] or 0)
        auto_lines = int(lines["auto_lines"] or 0)
        return {
            "days": int(days),
            "rfqs": int(head["rfqs"] or 0),
            "quotes": int(head["quotes"] or 0),
            "total_sum": round(float(head["total_sum"] or 0), 2),
            "lines": total_lines,
            "auto_lines": auto_lines,
            "auto_share": round(auto_lines / total_lines * 100, 1) if total_lines else 0.0,
            "llm_cost": round(float(cost["cost"] or 0), 4),
            "statuses": {row["status"]: int(row["n"]) for row in statuses},
            "median_seconds": timing.get("median_seconds"),
            "median_wait_seconds": timing.get("median_wait_seconds"),
        }

    def unmatched_report(
        self, days: int = 30, limit: int = 10, *, assignee: str | None = None
    ) -> list[dict]:
        """Что чаще всего не сопоставилось: подсказка клиенту, чего нет в прайсе.

        assignee ограничивает отчёт заявками одного менеджера: текст строк — это
        содержимое писем, и менеджер не должен видеть переписку коллег.
        """
        since = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).isoformat()
        scope = "AND r.assignee = ?" if assignee else ""
        params: list[object] = [since]
        if assignee:
            params.append(assignee)
        params.append(max(1, min(int(limit), 50)))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT l.raw_text AS text, COUNT(*) AS n
                FROM rfq_lines l JOIN rfqs r ON r.id = l.rfq_id
                WHERE r.created_at >= ? AND l.status IN ('no_match', 'ambiguous', 'invalid')
                {scope}
                GROUP BY lower(l.raw_text)
                ORDER BY n DESC, l.raw_text
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [{"text": row["text"], "count": int(row["n"])} for row in rows]

    def purge_older_than(self, days: int, *, dry_run: bool = True) -> dict:
        """Удаляет заявки старше срока хранения вместе со строками и журналом.

        Возвращает список файлов артефактов: их удаляет вызывающий, чтобы Store
        не занимался файловой системой. dry_run ничего не меняет.
        """
        since = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).isoformat()
        with closing(self._connect()) as conn, conn:
            ids = [row["id"] for row in conn.execute(
                "SELECT id FROM rfqs WHERE created_at < ?", (since,)
            ).fetchall()]
            if not ids:
                return {"rfqs": 0, "artifacts": [], "dry_run": dry_run}
            placeholders = ",".join("?" for _ in ids)
            paths = [row["path"] for row in conn.execute(
                f"SELECT path FROM artifacts WHERE rfq_id IN ({placeholders})", ids
            ).fetchall()]
            if dry_run:
                return {"rfqs": len(ids), "artifacts": paths, "dry_run": True}
            conn.execute(f"DELETE FROM outbox WHERE rfq_id IN ({placeholders})", ids)
            conn.execute(f"DELETE FROM rfqs WHERE id IN ({placeholders})", ids)
        return {"rfqs": len(ids), "artifacts": paths, "dry_run": False}

    def purge_demo_rfqs(self) -> int:
        """Удаляет заявки демо-стенда (адреса @demo.local), не трогая настоящие."""
        with closing(self._connect()) as conn, conn:
            ids = [row["id"] for row in conn.execute(
                "SELECT id FROM rfqs WHERE sender LIKE '%@demo.local%' OR sender LIKE '%demo.local>%'"
            ).fetchall()]
            for rfq_id in ids:
                conn.execute("DELETE FROM artifacts WHERE rfq_id = ?", (rfq_id,))
                conn.execute("DELETE FROM rfqs WHERE id = ?", (rfq_id,))
        return len(ids)

    def get_settings(self) -> dict[str, str]:
        """Настройки компании, которые правятся из интерфейса (перекрывают .env)."""
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT k, v FROM app_settings").fetchall()
        return {row["k"]: row["v"] for row in rows}

    def set_settings(self, values: dict[str, str]) -> None:
        with closing(self._connect()) as conn, conn:
            for key, value in values.items():
                conn.execute(
                    "INSERT INTO app_settings(k, v) VALUES (?, ?)"
                    " ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                    (key, str(value)),
                )

    def stats(self) -> dict:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT (SELECT COUNT(*) FROM rfqs) AS rfqs,
                       (SELECT COUNT(*) FROM quotes) AS quotes,
                       (SELECT COUNT(*) FROM price_items) AS price_items,
                       (SELECT COUNT(*) FROM decisions) AS decisions
                """
            ).fetchone()
        return dict(row)


    def set_quote_duration(self, quote_id: str, duration_ms: int) -> None:
        """Сколько занял разбор вместе с подготовкой документов."""
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE quotes SET duration_ms = ? WHERE id = ?", (int(duration_ms), quote_id)
            )

    def sent_history(self, rfq_id: str, limit: int = 20) -> list[dict]:
        """История отправок КП: что ушло, кому и когда."""
        limit = max(1, min(int(limit), 100))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT action, actor, detail, created_at FROM decisions"
                " WHERE rfq_id = ? AND action IN ('sent', 'send_failed')"
                " ORDER BY id DESC LIMIT ?",
                (rfq_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------- остатки ----------

    def replace_stock(self, items: list[tuple[str, str, str, str]]) -> int:
        """Заменяет остатки: (артикул, количество, единица, склад)."""
        now = utcnow()
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM stock")
            conn.executemany(
                "INSERT INTO stock(sku, qty, unit, warehouse, updated_at) VALUES (?, ?, ?, ?, ?)",
                [(sku, qty, unit, warehouse, now) for sku, qty, unit, warehouse in items],
            )
        return len(items)

    def stock_map(self) -> dict[str, dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT sku, qty, unit, warehouse, updated_at FROM stock").fetchall()
        return {row["sku"]: dict(row) for row in rows}

    def stock_count(self) -> int:
        with closing(self._connect()) as conn:
            return int(conn.execute("SELECT COUNT(*) AS c FROM stock").fetchone()["c"])

    def stock_updated_at(self) -> str:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT MAX(updated_at) AS t FROM stock").fetchone()
        return row["t"] or ""

    # ---------- очередь отправки ----------

    def enqueue_message(self, *, rfq_id: str, to_addr: str, subject: str, body: str, error: str = "") -> int:
        """Письмо, которое не ушло: сохраняем, чтобы не потерять, и пробуем позже."""
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "INSERT INTO outbox(rfq_id, to_addr, subject, body, attempts, last_error, created_at)"
                " VALUES (?, ?, ?, ?, 1, ?, ?)",
                (rfq_id, to_addr, subject[:300], body, error[:300], utcnow()),
            )
            return int(cursor.lastrowid)

    def pending_messages(self, limit: int = 20) -> list[dict]:
        limit = max(1, min(int(limit), 100))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM outbox WHERE sent_at IS NULL AND attempts < 5"
                " ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_sent(self, message_id: int) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE outbox SET sent_at = ? WHERE id = ?", (utcnow(), message_id))

    def mark_attempt_failed(self, message_id: int, error: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE outbox SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                (error[:300], message_id),
            )

    def outbox_stats(self) -> dict:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total, SUM(CASE WHEN sent_at IS NULL THEN 1 ELSE 0 END) AS pending"
                " FROM outbox"
            ).fetchone()
        return {"total": int(row["total"] or 0), "pending": int(row["pending"] or 0)}

    # ---------- обучение на правках ----------

    def learn_match(self, key: str, sku: str) -> None:
        """Человек подтвердил соответствие: «эта формулировка = этот артикул»."""
        if not key or not sku:
            return
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO learned(key, sku, hits, misses, updated_at) VALUES (?, ?, 1, 0, ?)"
                " ON CONFLICT(key) DO UPDATE SET hits = hits + 1, sku = excluded.sku,"
                " updated_at = excluded.updated_at",
                (key, sku, utcnow()),
            )

    def learn_miss(self, key: str, sku: str | None = None) -> None:
        """Человек убрал или заменил совпадение: доверие к паре падает."""
        if not key:
            return
        with closing(self._connect()) as conn, conn:
            if sku:
                conn.execute(
                    "UPDATE learned SET misses = misses + 1, updated_at = ? WHERE key = ? AND sku = ?",
                    (utcnow(), key, sku),
                )
            else:
                conn.execute(
                    "UPDATE learned SET misses = misses + 1, updated_at = ? WHERE key = ?",
                    (utcnow(), key),
                )

    def learned_sku(self, key: str) -> str | None:
        """Пара считается выученной, если подтверждений минимум два и они перевешивают правки."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT sku FROM learned WHERE key = ? AND hits >= 2 AND hits >= misses + 2",
                (key,),
            ).fetchone()
        return row["sku"] if row else None

    def learned_pairs(self, limit: int = 200) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT key, sku, hits, misses, updated_at FROM learned"
                " ORDER BY hits - misses DESC, updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def learned_count(self) -> int:
        with closing(self._connect()) as conn:
            return int(conn.execute("SELECT COUNT(*) AS c FROM learned").fetchone()["c"])

    def forget_learned(self, key: str | None = None) -> int:
        with closing(self._connect()) as conn, conn:
            if key:
                cur = conn.execute("DELETE FROM learned WHERE key = ?", (key,))
            else:
                cur = conn.execute("DELETE FROM learned")
            return cur.rowcount

    # ---------- пользователи ----------

    def create_user(self, *, email: str, password: str, role: str = "manager", name: str = "") -> User:
        email = normalize_email(email)
        if not email or "@" not in email:
            raise ValueError("нужен корректный e-mail")
        if len(password or "") < 8:
            raise ValueError("пароль короче 8 символов")
        salt, digest = hash_password(password)
        user = User(id=uuid.uuid4().hex, email=email, role=check_role(role), name=name.strip())
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO users(id, email, name, role, salt, password_hash, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user.id, user.email, user.name, user.role, salt, digest, utcnow()),
            )
        return user

    def set_user_role(self, user_id: str, role: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE users SET role = ? WHERE id = ?", (check_role(role), user_id))

    def set_user_password(self, user_id: str, password: str) -> None:
        if len(password or "") < 8:
            raise ValueError("пароль короче 8 символов")
        salt, digest = hash_password(password)
        with closing(self._connect()) as conn, conn:
            # Время смены пароля обрывает все выданные ранее cookie этого человека.
            conn.execute(
                "UPDATE users SET salt = ?, password_hash = ?, password_changed_at = ? WHERE id = ?",
                (salt, digest, utcnow(), user_id),
            )

    def delete_user(self, user_id: str) -> None:
        """Мягкое удаление: пользователь блокируется, история и журнал остаются."""
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE rfqs SET assignee = NULL WHERE assignee = ?", (user_id,))
            conn.execute("UPDATE users SET deleted_at = ? WHERE id = ?", (utcnow(), user_id))

    def admin_count(self) -> int:
        with closing(self._connect()) as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM users WHERE role = 'admin' AND deleted_at IS NULL"
                ).fetchone()["c"]
            )

    def list_events(self, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT action, actor, detail, created_at FROM events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def users(self) -> list[User]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, email, role, name FROM users WHERE deleted_at IS NULL ORDER BY role, email"
            ).fetchall()
        return [User(id=r["id"], email=r["email"], role=r["role"], name=r["name"]) for r in rows]

    def user_count(self) -> int:
        with closing(self._connect()) as conn:
            return int(
                conn.execute("SELECT COUNT(*) AS c FROM users WHERE deleted_at IS NULL").fetchone()["c"]
            )

    def authenticate(self, email: str, password: str) -> User | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT id, email, role, name, salt, password_hash FROM users"
                " WHERE email = ? AND deleted_at IS NULL",
                (normalize_email(email),),
            ).fetchone()
        if row is None:
            return None
        if not verify_password(password, row["salt"], row["password_hash"]):
            return None
        return User(id=row["id"], email=row["email"], role=row["role"], name=row["name"])

    def user_by_id(self, user_id: str) -> User | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT id, email, role, name, password_changed_at FROM users"
                " WHERE id = ? AND deleted_at IS NULL",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return User(
            id=row["id"],
            email=row["email"],
            role=row["role"],
            name=row["name"],
            password_changed_at=row["password_changed_at"] or "",
        )

    def set_assignee(self, rfq_id: str, user_id: str | None) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE rfqs SET assignee = ? WHERE id = ?", (user_id, rfq_id))

    def ensure_default_admin(self, password: str, email: str = "admin@local") -> User | None:
        """Первый запуск: создаём администратора из пароля в конфигурации."""
        if self.user_count() > 0:
            return None
        return self.create_user(email=email, password=password, role=ROLE_ADMIN, name="Администратор")

    # ---------- метрики времени ----------

    def rfq_timing(self, rfq_id: str) -> dict:
        """Два разных числа: сколько шёл разбор и сколько письмо ждало в очереди."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT r.created_at AS received, r.received_at AS mail_date,"
                " q.created_at AS quoted, q.duration_ms AS duration_ms"
                " FROM rfqs r LEFT JOIN quotes q ON q.rfq_id = r.id"
                " WHERE r.id = ? ORDER BY q.rowid LIMIT 1",
                (rfq_id,),
            ).fetchone()
        if row is None:
            return {}
        start = row["mail_date"] if _parse_dt(row["mail_date"] or "") else row["received"]
        total = _seconds_between(start, row["quoted"]) if row["quoted"] else None
        duration = (row["duration_ms"] / 1000) if row["duration_ms"] is not None else None
        wait = round(max(total - duration, 0.0), 1) if (total is not None and duration is not None) else None
        return {
            "start": start,
            "received_at": row["received"],
            "quoted_at": row["quoted"],
            "seconds": total,
            "duration_seconds": duration,
            "wait_seconds": wait,
        }

    def timing_stats(self) -> dict:
        """Метрика «письмо → первый расчёт», по одной строке на заявку.

        Раньше JOIN со всеми версиями КП считал каждую правку отдельной заявкой:
        у заявки с тремя пересчётами count был 3, медиана и максимум — по
        последней версии, а не по первой. Теперь берётся первый снимок КП —
        именно он отвечает на вопрос «за сколько сервис подготовил КП».
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT r.created_at AS received, r.received_at AS mail_date,"
                " q.created_at AS quoted, q.duration_ms AS duration_ms"
                " FROM rfqs r JOIN quotes q ON q.id = ("
                "   SELECT id FROM quotes WHERE rfq_id = r.id ORDER BY rowid LIMIT 1"
                " )"
            ).fetchall()
        durations: list[float] = []
        waits: list[float] = []
        for row in rows:
            start = row["mail_date"] if _parse_dt(row["mail_date"] or "") else row["received"]
            total = _seconds_between(start, row["quoted"])
            duration = (row["duration_ms"] / 1000) if row["duration_ms"] is not None else None
            if duration is not None:
                durations.append(duration)
            if total is not None and duration is not None:
                waits.append(max(total - duration, 0.0))

        def median(values: list[float]) -> float | None:
            if not values:
                return None
            ordered = sorted(values)
            middle = len(ordered) // 2
            result = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
            return round(result, 1)

        return {
            "count": len(durations),
            "median_seconds": median(durations),
            "max_seconds": round(max(durations), 1) if durations else None,
            "median_wait_seconds": median(waits),
        }


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_dt(value: str):
    """Понимает и ISO из базы, и заголовок Date из письма (RFC 2822)."""
    from datetime import datetime
    from email.utils import parsedate_to_datetime

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        from datetime import timezone

        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _seconds_between(start: str, end: str) -> float | None:
    """Разница между двумя метками времени. None, если что-то не разобралось."""
    first = _parse_dt(start)
    second = _parse_dt(end)
    if first is None or second is None:
        return None
    return max((second - first).total_seconds(), 0.0)


