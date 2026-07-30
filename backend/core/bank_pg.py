"""PostgreSQL compatibility layer for per-bank BankStore data.

This module lets the existing SQLite-oriented BankStore and routers operate
against PostgreSQL when DB_BACKEND=postgres.

Design:
- one PostgreSQL schema per bank: bank_hsbc, bank_ctbc, ...
- same table names inside each schema: accounts, cards, twd_transactions, ...
- callsites keep SQLite-style `?` placeholders and sqlite3.Row-like access
- a tiny adapter handles PRAGMA/sqlite_master compatibility used by existing code
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from typing import Any
from collections.abc import Iterator

DB_BACKEND = os.environ.get("DB_BACKEND", "sqlite").lower()
if DB_BACKEND == "postgres":
    from psycopg_pool import ConnectionPool

_pg_pool_lock = threading.Lock()
_pg_pool: Any | None = None
_PG_POOL_MIN_SIZE = int(os.environ.get("PG_POOL_MIN_SIZE", "1"))
_PG_POOL_MAX_SIZE = int(os.environ.get("PG_POOL_MAX_SIZE", "4"))
_PG_POOL_TIMEOUT = float(os.environ.get("PG_POOL_TIMEOUT", "10"))
_PG_POOL_MAX_LIFETIME = float(os.environ.get("PG_POOL_MAX_LIFETIME", "1800"))
_PG_POOL_MAX_IDLE = float(os.environ.get("PG_POOL_MAX_IDLE", "300"))
_PG_POOL_RECONNECT_TIMEOUT = float(os.environ.get("PG_POOL_RECONNECT_TIMEOUT", "60"))


# Phase C (2026-06-18): per-process cache of PG schemas already migrated for
# user_id columns. SQLite side has _PHASE_C_MIGRATED in db.py; this is the PG
# mirror. Keyed by schema name (one entry per bank per process).
_PHASE_C_PG_MIGRATED: set[str] = set()

# Tables that need user_id column + composite UNIQUE INDEX (mirrors
# db.py:_PHASE_C_PK_INDEXES exactly so SQLite and PG converge on the same shape).
_PHASE_C_PG_TABLES = (
    "twd_transactions",
    "card_billed_txns",
    "card_pending_txns",
    "balance_history",
    "accounts",
    "cards",
    "daily_metrics",
)

# (table, index_name, column_list) — composite UNIQUE INDEX to align with
# router-side INSERT ... ON CONFLICT (user_id, ...).
_PHASE_C_PG_INDEXES = (
    ("balance_history", "ux_balance_history_user_snap", "(user_id, snapshot_date)"),
    ("accounts", "ux_accounts_user_no", "(user_id, account_no)"),
    ("cards", "ux_cards_user_no", "(user_id, card_no)"),
    ("daily_metrics", "ux_daily_metrics_user_snap_cat", "(user_id, snapshot_date, category)"),
    ("twd_transactions", "ux_twd_dedup", "(user_id, dedup_key)"),
    ("card_billed_txns", "ux_card_billed_dedup", "(user_id, dedup_key)"),
)

# Phase C-pk (2026-06-18): tables whose legacy PRIMARY KEY was single-column
# (account_no / card_no / snapshot_date / etc.) — need to be swapped to composite
# (user_id, ...) so multi-tenant INSERT doesn't violate the old PK.
#
# (table, old_pk_column, new_composite_pk_columns)
_PHASE_C_PG_PK_SWAPS = (
    ("accounts", "account_no", "(user_id, account_no)"),
    ("cards", "card_no", "(user_id, card_no)"),
    ("balance_history", "snapshot_date", "(user_id, snapshot_date)"),
    ("daily_metrics", None, "(user_id, snapshot_date, category)"),  # may be composite already
)


def _ensure_phase_c_user_id_pg(conn: Any, schema: str) -> None:
    """Idempotent: 每張 bank 表如缺 user_id column 就補 + backfill = 1.

    PG mirror of db.py:_ensure_phase_c_user_id. Runs once per process per
    schema. Failures are swallowed so a missing/broken schema doesn't block
    Connection creation — actual row-level access will surface the real error.
    """
    if schema in _PHASE_C_PG_MIGRATED:
        return
    try:
        # 1) Existing tables in this schema
        cur = conn.execute(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema = %s AND table_type = 'BASE TABLE'""",
            (schema,),
        )
        existing = {r[0] for r in cur.fetchall()}

        # 2) ADD COLUMN user_id (idempotent via IF NOT EXISTS, PG 9.6+)
        for tbl in _PHASE_C_PG_TABLES:
            if tbl not in existing:
                continue
            try:
                conn.execute(
                    f'ALTER TABLE "{schema}"."{tbl}" '
                    f'ADD COLUMN IF NOT EXISTS user_id INTEGER NOT NULL DEFAULT 1'
                )
                # Defensive backfill: rows might have been inserted with NULL/0
                # before ADD COLUMN landed (unlikely with NOT NULL DEFAULT 1
                # but harmless).
                conn.execute(
                    f'UPDATE "{schema}"."{tbl}" SET user_id = 1 '
                    f'WHERE user_id IS NULL OR user_id = 0'
                )
            except Exception:
                # If a single table fails (e.g. permissions, conflicting
                # constraint), don't block the others. Real query-time errors
                # will surface in the router.
                conn.rollback()
                continue

        # 3) Composite UNIQUE INDEX (idempotent CREATE INDEX IF NOT EXISTS)
        for tbl, idx, cols in _PHASE_C_PG_INDEXES:
            if tbl not in existing:
                continue
            try:
                conn.execute(
                    f'CREATE UNIQUE INDEX IF NOT EXISTS "{idx}" '
                    f'ON "{schema}"."{tbl}" {cols}'
                )
            except Exception:
                # Old data may have duplicates that violate the new uniqueness;
                # skip rather than block the read path.
                conn.rollback()
                continue

        # 4) Phase C-pk (2026-06-18): swap legacy single-column PRIMARY KEY
        # to composite (user_id, ...). Without this, multi-tenant INSERT of
        # the same account_no/card_no for two different users hits
        # UniqueViolation on the legacy single-column PK.
        # Idempotent: skip if current PK already has user_id in it.
        for tbl, old_pk_col, new_pk_cols in _PHASE_C_PG_PK_SWAPS:
            if tbl not in existing:
                continue
            try:
                # Query current PK columns
                cur = conn.execute(
                    """SELECT a.attname
                       FROM pg_index i
                       JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                       JOIN pg_class c ON c.oid = i.indrelid
                       JOIN pg_namespace n ON n.oid = c.relnamespace
                       WHERE i.indisprimary
                         AND n.nspname = %s AND c.relname = %s
                       ORDER BY a.attnum""",
                    (schema, tbl),
                )
                current_pk_cols = [r[0] for r in cur.fetchall()]
                if "user_id" in current_pk_cols:
                    continue  # Already composite — skip

                # Find PK constraint name (Postgres autogenerates as <table>_pkey)
                cur = conn.execute(
                    """SELECT con.conname FROM pg_constraint con
                       JOIN pg_class c ON c.oid = con.conrelid
                       JOIN pg_namespace n ON n.oid = c.relnamespace
                       WHERE con.contype = 'p'
                         AND n.nspname = %s AND c.relname = %s""",
                    (schema, tbl),
                )
                pk_name_row = cur.fetchone()
                if pk_name_row is None:
                    # No PK at all — just add the composite one
                    conn.execute(
                        f'ALTER TABLE "{schema}"."{tbl}" '
                        f'ADD PRIMARY KEY {new_pk_cols}'
                    )
                    conn.commit()
                    continue

                pk_name = pk_name_row[0]
                # Drop legacy PK + add composite PK in one transaction
                conn.execute(
                    f'ALTER TABLE "{schema}"."{tbl}" '
                    f'DROP CONSTRAINT "{pk_name}"'
                )
                conn.execute(
                    f'ALTER TABLE "{schema}"."{tbl}" '
                    f'ADD PRIMARY KEY {new_pk_cols}'
                )
                conn.commit()
            except Exception:
                # If swap fails (e.g. duplicate rows blocking the new PK),
                # skip — read path still works via the UNIQUE INDEX, write
                # path will surface the real error to the user.
                try:
                    conn.rollback()
                except Exception:
                    pass
                continue

        conn.commit()
    except Exception:
        # Schema audit itself failed (rare — connection-level issue). Mark as
        # attempted and move on so we don't keep hammering on every request.
        try:
            conn.rollback()
        except Exception:
            pass
    _PHASE_C_PG_MIGRATED.add(schema)


def _reset_phase_c_pg_cache() -> None:
    """Test hook: clear per-process PG schema migration cache.

    Symmetric to db.py:_reset_migration_cache for BankStore. Called from
    conftest.py autouse fixture so each test sees a clean migration state.
    """
    _PHASE_C_PG_MIGRATED.clear()


def enabled() -> bool:
    return DB_BACKEND == "postgres"


class Row(dict):
    """sqlite3.Row-ish mapping supporting both row["col"] and row[0].

    Critical contract (matches stdlib sqlite3.Row):
      - row[int]  → positional value (column 0, 1, …)
      - row[str]  → value by column name
      - iter(row) → VALUES in column order (NOT keys — that's the default dict
                    behavior). Tuple unpacking `a, b = row` depends on this.
      - row.keys() → list of column names (inherited from dict; works since
                     dict preserves insertion order in 3.7+ and we pass the
                     values dict in column order).

    Bug history (2026-06-18, v0.2.8): without __iter__ override, dict.__iter__
    yielded KEYS, so `for raw_tags, raw_date in cur.fetchall()` in
    list_popular_tags unpacked the column-name strings, fed them to
    _parse_tags_overwrite (which silently returned [] on non-JSON input),
    and the endpoint always returned `{"tags": []}` in PG mode even though
    rows existed. SQLite mode worked because sqlite3.Row iterates values.
    """

    def __init__(self, values: dict[str, Any], order: list[str]):
        super().__init__(values)
        self._order = order

    def __getitem__(self, key: str | int) -> Any:  # type: ignore[override]
        if isinstance(key, int):
            return super().__getitem__(self._order[key])
        return super().__getitem__(key)

    def __iter__(self) -> Iterator[Any]:  # type: ignore[override]
        # sqlite3.Row contract: iterate VALUES in column order (not keys).
        # Without this override, dict.__iter__ yields keys and tuple unpacking
        # silently returns column names instead of data.
        return (dict.__getitem__(self, k) for k in self._order)


class Cursor:
    def __init__(self, cur: Any):
        self._cur = cur
        self.rowcount = cur.rowcount
        self._rows: list[Row] | None = None

    def _convert(self) -> list[Row]:
        if self._rows is not None:
            return self._rows
        if not self._cur.description:
            self._rows = []
            return self._rows
        cols = [d.name for d in self._cur.description]
        raw = self._cur.fetchall()
        out: list[Row] = []
        for tup in raw:
            out.append(Row(dict(zip(cols, tup, strict=True)), cols))
        self._rows = out
        return out

    def fetchone(self) -> Row | None:
        rows = self._convert()
        return rows[0] if rows else None

    def fetchall(self) -> list[Row]:
        return self._convert()

    def __iter__(self) -> Iterator[Row]:
        return iter(self._convert())


_PRAGMA_RE = re.compile(r"^\s*PRAGMA\s+table_info\(([^)]+)\)\s*$", re.I)
_SQLITE_MASTER_ALL_RE = re.compile(
    r"^\s*SELECT\s+name\s+FROM\s+sqlite_master\s+WHERE\s+type\s*=\s*'table'\s*$",
    re.I,
)
_SQLITE_MASTER_ONE_RE = re.compile(
    r"^\s*SELECT\s+1\s+FROM\s+sqlite_master\s+WHERE\s+type\s*=\s*'table'\s+AND\s+name\s*=\s*\?\s+LIMIT\s+1\s*$",
    re.I,
)
_SQLITE_MASTER_ONE_LITERAL_RE = re.compile(
    r"^\s*SELECT\s+1\s+FROM\s+sqlite_master\s+WHERE\s+type\s*=\s*'table'\s+AND\s+name\s*=\s*'([^']+)'\s+LIMIT\s+1\s*$",
    re.I,
)


def _dsn() -> str:
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        raise RuntimeError("DB_BACKEND=postgres requires DATABASE_URL")
    return dsn


def _get_pg_pool() -> Any:
    """Return process-global psycopg pool for per-bank schemas.

    `open=False` keeps module import from resolving DNS; `open(wait=False)`
    starts the pool lazily and actual checkout happens in Connection.__init__.

    The per-bank adapter runs unqualified SQL through a pooled connection with
    different ``search_path`` values (one schema per bank). psycopg3 starts
    server-side prepared statements after a few identical SQL executions by
    default. With search_path switching, ``SELECT * FROM card_pending_txns`` can
    be prepared against one bank schema and later reused against another schema
    whose table has a different row type after ALTER/sync, causing PostgreSQL to
    raise ``cached plan must not change result type``. Disable auto-prepare for
    this compatibility layer so every checkout plans against the current schema.
    """
    global _pg_pool
    if _pg_pool is not None:
        return _pg_pool
    with _pg_pool_lock:
        if _pg_pool is None:
            pool = ConnectionPool(
                _dsn(),
                kwargs={"prepare_threshold": None},
                min_size=_PG_POOL_MIN_SIZE,
                max_size=_PG_POOL_MAX_SIZE,
                open=False,
                timeout=_PG_POOL_TIMEOUT,
                max_lifetime=_PG_POOL_MAX_LIFETIME,
                max_idle=_PG_POOL_MAX_IDLE,
                reconnect_timeout=_PG_POOL_RECONNECT_TIMEOUT,
                check=ConnectionPool.check_connection,
            )
            pool.open(wait=False)
            _pg_pool = pool
    return _pg_pool


def schema_name(bank: str) -> str:
    safe = re.sub(r"[^a-z0-9_]", "_", bank.lower())
    return f"bank_{safe}"


def q(sql: str) -> str:
    """Convert SQLite-style SQL to PostgreSQL SQL for our small subset."""
    out = sql
    out = out.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    out = out.replace("INTEGER PRIMARY KEY", "BIGSERIAL PRIMARY KEY")
    out = out.replace("BLOB", "BYTEA")
    out = re.sub(
        r"CASE WHEN \? IS NULL THEN ([A-Za-z0-9_]+\.[A-Za-z0-9_]+) ELSE \? END",
        r"CASE WHEN ?::integer IS NULL THEN \1 ELSE ?::integer END",
        out,
    )
    # SQLite pattern `col IS ?` is used for NULL-safe equality. PostgreSQL
    # parameter placeholders cannot appear after bare `IS`, so translate before
    # placeholder conversion.
    out = out.replace(" IS ?", " IS NOT DISTINCT FROM ?")
    # psycopg pyformat treats bare % as placeholders; escape first, then ? -> %s.
    out = out.replace("?", "__THOTH_PARAM__")
    out = out.replace("%", "%%")
    return out.replace("__THOTH_PARAM__", "%s")


def split_statements(script: str) -> list[str]:
    out: list[str] = []
    for stmt in script.split(";"):
        s = stmt.strip()
        if not s:
            continue
        lines = [ln for ln in s.splitlines() if ln.strip() and not ln.strip().startswith("--")]
        if lines:
            out.append("\n".join(lines))
    return out


class Connection:
    def __init__(self, bank: str):
        self.bank = bank
        self.schema = schema_name(bank)
        self._checkout_cm = _get_pg_pool().connection(timeout=_PG_POOL_TIMEOUT)
        self._conn = self._checkout_cm.__enter__()
        self._closed = False
        try:
            self.total_changes = 0
            self.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
            # Search path applies to all unqualified table names below.
            self.execute(f'SET search_path TO "{self.schema}", public')
            self.commit()
            # Phase C (2026-06-18): backfill user_id columns + composite UNIQUE
            # INDEX for legacy PG schemas created before Path A multi-user. SQLite
            # side has db.py:_ensure_phase_c_user_id; this is the PG mirror.
            # Per-process per-schema cache means at most one audit per schema.
            _ensure_phase_c_user_id_pg(self._conn, self.schema)
        except BaseException as e:
            self._checkout_cm.__exit__(type(e), e, e.__traceback__)
            self._closed = True
            raise

    def execute(self, sql: str, params: tuple | list = ()) -> Cursor:
        sql_stripped = sql.strip()
        m = _PRAGMA_RE.match(sql_stripped)
        if m:
            table = m.group(1).strip().strip('"')
            cur = self._conn.execute(
                """SELECT column_name AS name
                   FROM information_schema.columns
                   WHERE table_schema = %s AND table_name = %s
                   ORDER BY ordinal_position""",
                (self.schema, table),
            )
            return Cursor(cur)

        if _SQLITE_MASTER_ALL_RE.match(sql_stripped):
            cur = self._conn.execute(
                """SELECT table_name AS name
                   FROM information_schema.tables
                   WHERE table_schema = %s AND table_type = 'BASE TABLE'""",
                (self.schema,),
            )
            return Cursor(cur)

        if _SQLITE_MASTER_ONE_RE.match(sql_stripped):
            table = params[0] if params else ""
            cur = self._conn.execute(
                """SELECT 1 AS exists
                   FROM information_schema.tables
                   WHERE table_schema = %s AND table_name = %s
                   LIMIT 1""",
                (self.schema, table),
            )
            return Cursor(cur)

        m = _SQLITE_MASTER_ONE_LITERAL_RE.match(sql_stripped)
        if m:
            table = m.group(1)
            cur = self._conn.execute(
                """SELECT 1 AS exists
                   FROM information_schema.tables
                   WHERE table_schema = %s AND table_name = %s
                   LIMIT 1""",
                (self.schema, table),
            )
            return Cursor(cur)

        try:
            cur = self._conn.execute(q(sql), tuple(params))
        except Exception as e:
            # Match SQLite router behavior: missing per-bank tables/columns are
            # treated as "bank has no data yet" and swallowed by existing
            # sqlite3.OperationalError handlers. PostgreSQL marks the current
            # transaction failed after an error, so rollback immediately before
            # surfacing the compatibility exception.
            if e.__class__.__name__ in {"UndefinedTable", "UndefinedColumn"}:
                self._conn.rollback()
                raise sqlite3.OperationalError(str(e)) from e
            raise
        if cur.rowcount and cur.rowcount > 0 and not cur.description:
            self.total_changes += cur.rowcount
        return Cursor(cur)

    def executescript(self, script: str) -> None:
        for stmt in split_statements(script):
            self.execute(stmt)

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        if self._closed:
            return
        # Pool connection context 在 __exit__(None, …) 會 commit。BankStore.close() 常在
        # sync_runner finally 執行，若 persist 中途拋錯，不能把未完成的 billed→pending
        # transition 當正常離場提交；已 commit 的正常路徑 rollback 是 no-op。
        try:
            self._conn.rollback()
        finally:
            self._checkout_cm.__exit__(None, None, None)
            self._closed = True


def connect(bank: str) -> Connection:
    return Connection(bank)
