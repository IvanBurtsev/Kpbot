"""Хранилище на SQLite. Все запросы — параметризованные (?), без склейки строк."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .domain import PriceItem, Quote, QuoteLine, RfqLine

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS price_items(
    sku         TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    unit        TEXT NOT NULL,
    price       TEXT NOT NULL,
    currency    TEXT NOT NULL DEFAULT 'RUB',
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
    subtotal        TEXT NOT NULL,
    vat             TEXT NOT NULL,
    delivery        TEXT NOT NULL,
    total           TEXT NOT NULL,
    vat_rate        TEXT NOT NULL,
    requires_review INTEGER NOT NULL,
    unmatched       INTEGER NOT NULL,
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
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init_schema(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.executescript(SCHEMA)
            # мягкая миграция для баз, созданных до появления поля
            try:
                conn.execute("ALTER TABLE llm_usage ADD COLUMN balance_rub TEXT")
            except sqlite3.OperationalError:
                pass

    # ---------- прайс ----------

    def replace_price_items(self, items: list[PriceItem]) -> int:
        now = utcnow()
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM price_items")
            conn.executemany(
                "INSERT INTO price_items(sku, name, unit, price, currency, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [(i.sku, i.name, i.unit, str(i.price), i.currency, now) for i in items],
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
                "SELECT sku, name, unit, price, currency FROM price_items ORDER BY sku"
            ).fetchall()
        return [
            PriceItem(
                sku=row["sku"],
                name=row["name"],
                unit=row["unit"],
                price=Decimal(row["price"]),
                currency=row["currency"],
            )
            for row in rows
        ]

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

    def set_status(self, rfq_id: str, status: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE rfqs SET status = ? WHERE id = ?", (status, rfq_id))

    def list_rfqs(self, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT r.id, r.sender, r.subject, r.received_at, r.status, r.created_at,
                       (SELECT COUNT(*) FROM rfq_lines l WHERE l.rfq_id = r.id) AS line_count,
                       (SELECT COUNT(*) FROM rfq_lines l WHERE l.rfq_id = r.id AND l.status <> 'ok') AS line_issues,
                       (SELECT q.total FROM quotes q WHERE q.rfq_id = r.id ORDER BY q.rowid DESC LIMIT 1) AS total
                FROM rfqs r
                ORDER BY r.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------- КП ----------

    def save_quote(self, quote_id: str, rfq_id: str, quote: Quote) -> str:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO quotes(id, rfq_id, subtotal, vat, delivery, total, vat_rate,"
                " requires_review, unmatched, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    quote_id,
                    rfq_id,
                    str(quote.subtotal),
                    str(quote.vat),
                    str(quote.delivery),
                    str(quote.total),
                    str(quote.vat_rate),
                    int(quote.requires_review),
                    int(quote.unmatched),
                    utcnow(),
                ),
            )
            conn.executemany(
                "INSERT INTO quote_lines(quote_id, idx, sku, name, unit, qty, price, amount)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
                    )
                    for idx, line in enumerate(quote.lines, start=1)
                ],
            )
        return quote_id

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


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()