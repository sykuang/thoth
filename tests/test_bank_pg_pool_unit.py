"""Unit tests for per-bank PostgreSQL connection pooling without a real PG server."""
from __future__ import annotations

import importlib
import os

import pytest


@pytest.fixture(autouse=True)
def _restore_sqlite_bank_pg_after_test(monkeypatch):
    yield
    os.environ["DB_BACKEND"] = "sqlite"
    os.environ.pop("DATABASE_URL", None)
    from backend.core import bank_pg
    importlib.reload(bank_pg)


def _reload_pg_bank_pg(monkeypatch):
    monkeypatch.setenv("DB_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:***@example.invalid:5432/db")
    from backend.core import bank_pg
    return importlib.reload(bank_pg)


class DummyRawConn:
    def __init__(self):
        self.sql: list[tuple[str, tuple]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def execute(self, sql: str, params: tuple = ()):  # mimics psycopg cursor return
        self.sql.append((sql, params))

        class Cur:
            rowcount = 0
            description = None

            def fetchall(self):
                return []

            def fetchone(self):
                return None

        return Cur()

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class DummyCheckout:
    def __init__(self, raw: DummyRawConn, exits: list[tuple | None]):
        self.raw = raw
        self.exits = exits
        self.entered = 0

    def __enter__(self) -> DummyRawConn:
        self.entered += 1
        return self.raw

    def __exit__(self, exc_type, exc, tb):  # context manager protocol
        self.exits.append((exc_type, exc, tb))
        return False


class DummyPool:
    def __init__(self, raw: DummyRawConn, exits: list[tuple | None]):
        self.raw = raw
        self.exits = exits
        self.connection_calls = 0

    def connection(self, timeout=None):  # mimics psycopg_pool API
        self.connection_calls += 1
        return DummyCheckout(self.raw, self.exits)


def test_bank_pg_connection_checks_out_from_pool_and_returns_on_close(monkeypatch):
    bank_pg = _reload_pg_bank_pg(monkeypatch)
    raw = DummyRawConn()
    exits: list[tuple | None] = []
    pool = DummyPool(raw, exits)

    monkeypatch.setattr(bank_pg, "_get_pg_pool", lambda: pool)
    monkeypatch.setattr(bank_pg, "_ensure_phase_c_user_id_pg", lambda _conn, _schema: None)

    conn = bank_pg.Connection("pytest")
    assert pool.connection_calls == 1
    assert raw.closed is False
    assert raw.sql[0][0] == 'CREATE SCHEMA IF NOT EXISTS "bank_pytest"'
    assert raw.sql[1][0] == 'SET search_path TO "bank_pytest", public'

    conn.close()

    assert exits == [(None, None, None)]
    assert raw.rollbacks == 1, "close must rollback uncommitted transition before pool return"
    assert raw.closed is False, "pooled checkout must be returned to pool, not physically closed"


def test_bank_pg_connection_returns_checkout_when_init_fails(monkeypatch):
    bank_pg = _reload_pg_bank_pg(monkeypatch)
    raw = DummyRawConn()
    exits: list[tuple | None] = []
    pool = DummyPool(raw, exits)

    def fail_migration(_conn, _schema):
        raise RuntimeError("migration failed")

    monkeypatch.setattr(bank_pg, "_get_pg_pool", lambda: pool)
    monkeypatch.setattr(bank_pg, "_ensure_phase_c_user_id_pg", fail_migration)

    with pytest.raises(RuntimeError, match="migration failed"):
        bank_pg.Connection("pytest")

    assert len(exits) == 1
    assert exits[0][0] is RuntimeError
    assert raw.closed is False


def test_bank_pg_pool_created_lazy_with_expected_bounds(monkeypatch):
    bank_pg = _reload_pg_bank_pg(monkeypatch)
    bank_pg._pg_pool = None
    calls = []

    class DummyConnectionPool:
        @staticmethod
        def check_connection(_conn):
            return None

        def __init__(self, conninfo, **kwargs):
            calls.append((conninfo, kwargs))
            self.opened = False

        def open(self, wait=False):
            self.opened = True
            self.open_wait = wait

    monkeypatch.setattr(bank_pg, "ConnectionPool", DummyConnectionPool)

    pool = bank_pg._get_pg_pool()

    assert pool is bank_pg._pg_pool
    assert pool.opened is True
    assert pool.open_wait is False
    assert calls[0][0].startswith("postgresql://user:***@example.invalid:5432/db")
    assert calls[0][1]["open"] is False
    assert calls[0][1]["kwargs"] == {"prepare_threshold": None}
    assert calls[0][1]["min_size"] == bank_pg._PG_POOL_MIN_SIZE
    assert calls[0][1]["max_size"] == bank_pg._PG_POOL_MAX_SIZE
    assert calls[0][1]["check"] is DummyConnectionPool.check_connection
