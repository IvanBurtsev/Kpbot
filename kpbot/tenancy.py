"""Реестр клиентов (тенантов): кто есть и в какой базе живёт.

Подход — отдельная база на клиента (database-per-tenant). Так проще и безопаснее,
чем tenant_id в каждой таблице: данные физически разделены, ошибка в фильтре
не покажет чужие заявки.

Реестр — отдельный файл `data/tenants.sqlite3`, в нём список клиентов и их баз.
Пользователи остаются в базе своего клиента: вход ищет их по очереди.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

REGISTRY_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS tenants(
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    db_path    TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);

-- Свой ключ API у каждого клиента: утечка одного ключа открывает одну базу,
-- а не все. Хранится только хеш — сам ключ показывается один раз при выпуске.
CREATE TABLE IF NOT EXISTS tenant_keys(
    tenant_id  TEXT PRIMARY KEY,
    key_hash   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


_REGISTRIES: dict[tuple[str, str], "TenantRegistry"] = {}


def get_registry(settings) -> "TenantRegistry":
    """Один реестр на процесс: админка и кабинет должны видеть одни и те же базы."""
    key = (str(settings.tenants_db_path), str(settings.db_path))
    registry = _REGISTRIES.get(key)
    if registry is None:
        registry = TenantRegistry(settings.tenants_db_path, settings.db_path)
        _REGISTRIES[key] = registry
    return registry


class TenantRegistry:
    """Список клиентов и пути их баз."""

    def __init__(self, path: Path, default_db: Path, default_name: str = "Основной") -> None:
        self.path = path
        self.default_db = default_db
        self.default_name = default_name

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def init(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.executescript(REGISTRY_SCHEMA)

    def ensure_default(self) -> str:
        """Первый запуск: существующая база становится клиентом «Основной»."""
        self.init()
        with closing(self._connect()) as conn, conn:
            # основная база — отдельный клиент: ищем именно его, а не «любого первого»
            row = conn.execute(
                "SELECT id FROM tenants WHERE id = 'default' OR db_path = ? LIMIT 1",
                (str(self.default_db),),
            ).fetchone()
            if row is not None:
                return row["id"]
            tenant_id = "default"
            conn.execute(
                "INSERT INTO tenants(id, name, db_path, status, created_at) VALUES (?, ?, ?, 'active', ?)",
                (tenant_id, self.default_name, str(self.default_db), _utcnow()),
            )
        return tenant_id

    def list(self) -> list[dict]:
        """Битый или чужой файл реестра не должен ронять интерфейс."""
        try:
            self.init()
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    "SELECT id, name, db_path, status, created_at FROM tenants ORDER BY created_at"
                ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error:
            return []

    def get(self, tenant_id: str) -> dict | None:
        if not tenant_id:
            return None
        try:
            self.init()
        except sqlite3.Error:
            return None
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT id, name, db_path, status, created_at FROM tenants WHERE id = ?", (tenant_id,)
            ).fetchone()
        return dict(row) if row else None

    def create(self, name: str, db_path: Path, tenant_id: str | None = None) -> dict:
        self.init()
        tenant_id = tenant_id or uuid.uuid4().hex[:12]
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO tenants(id, name, db_path, status, created_at) VALUES (?, ?, ?, 'active', ?)",
                (tenant_id, name.strip() or tenant_id, str(db_path), _utcnow()),
            )
        return {"id": tenant_id, "name": name.strip() or tenant_id, "db_path": str(db_path), "status": "active"}

    def set_status(self, tenant_id: str, status: str) -> None:
        if status not in ("active", "paused"):
            raise ValueError("статус бывает active или paused")
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE tenants SET status = ? WHERE id = ?", (status, tenant_id))

    def rename(self, tenant_id: str, name: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE tenants SET name = ? WHERE id = ?", (name.strip(), tenant_id))

    def find_by_login(self, email: str) -> list[dict]:
        """Клиенты, в базе которых есть такой логин: вход без угадывания клиента."""
        email = (email or "").strip().lower()
        if not email:
            return []
        found: list[dict] = []
        for tenant in self.list():
            db_path = Path(tenant["db_path"])
            if not db_path.exists():
                continue
            try:
                with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
                    row = conn.execute(
                        "SELECT 1 FROM users WHERE lower(email) = ? LIMIT 1", (email,)
                    ).fetchone()
            except sqlite3.Error:
                continue
            if row:
                found.append(tenant)
        return found

    # ---------- ключи API на клиента ----------

    def issue_key(self, tenant_id: str) -> str:
        """Выпускает новый ключ клиента и возвращает его один раз (в базе — хеш)."""
        key = "kpb_" + secrets.token_urlsafe(24)
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO tenant_keys(tenant_id, key_hash, created_at) VALUES (?, ?, ?)"
                " ON CONFLICT(tenant_id) DO UPDATE SET key_hash = excluded.key_hash,"
                " created_at = excluded.created_at",
                (tenant_id, digest, _utcnow()),
            )
        return key

    def verify_key(self, tenant_id: str, key: str) -> bool:
        """Подходит ли ключ этому клиенту. Пустой/чужой ключ — False."""
        if not key:
            return False
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT key_hash FROM tenant_keys WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()
        if row is None:
            return False
        return hmac.compare_digest(hashlib.sha256(key.encode("utf-8")).hexdigest(), row["key_hash"])

    def has_key(self, tenant_id: str) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM tenant_keys WHERE tenant_id = ? LIMIT 1", (tenant_id,)
            ).fetchone()
        return row is not None