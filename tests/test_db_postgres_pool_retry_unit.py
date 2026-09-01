"""Unit tests for PostgreSQL pool/retry glue without requiring a real PG server."""
from __future__ import annotations

import importlib
import os

import pytest


@pytest.fixture(autouse=True)
def _restore_sqlite_db_module_after_test(monkeypatch):
    yield
    # This file intentionally reloads backend.server.db in postgres mode and
    # monkeypatches pool internals. Restore the process-global module to sqlite
    # so route tests collected later in the same pytest process do not inherit
    # DB_BACKEND=postgres / DummyConnectionPool state.
    os.environ["DB_BACKEND"] = "sqlite"
    os.environ.pop("DATABASE_URL", None)
    from backend.server import db
    importlib.reload(db)
    from backend.server import users
    importlib.reload(users)
    from backend.server import creds_store
    importlib.reload(creds_store)


def _reload_pg_db(monkeypatch):
    monkeypatch.setenv("DB_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@example.invalid:5432/db")
    monkeypatch.setenv("PG_CONNECT_ATTEMPTS", "3")
    monkeypatch.setenv("PG_CONNECT_BASE_DELAY", "0")
    from backend.server import db
    return importlib.reload(db)


def test_pg_connection_retry_retries_operational_error(monkeypatch):
    db = _reload_pg_db(monkeypatch)

    class DummyOperationalError(Exception):
        pass

    class DummyPsycopg:
        OperationalError = DummyOperationalError

    attempts = {"n": 0}

    class DummyPool:
        def connection(self, timeout=None):
            class CM:
                def __enter__(self_inner):
                    attempts["n"] += 1
                    if attempts["n"] < 3:
                        raise DummyOperationalError("temporary dns failure")
                    return "raw-conn"

                def __exit__(self_inner, exc_type, exc, tb):
                    return False

            return CM()

    monkeypatch.setattr(db, "psycopg", DummyPsycopg)
    monkeypatch.setattr(db, "_get_pg_pool", lambda: DummyPool())
    monkeypatch.setattr(db.time, "sleep", lambda _seconds: None)

    with db._pg_connection_with_retry() as raw:
        assert raw == "raw-conn"
    assert attempts["n"] == 3


def test_pg_pool_created_lazy_with_expected_bounds(monkeypatch):
    db = _reload_pg_db(monkeypatch)
    db._pg_pool = None

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

    monkeypatch.setattr(db, "ConnectionPool", DummyConnectionPool)
    pool = db._get_pg_pool()
    assert pool is db._pg_pool
    assert pool.opened is True
    assert pool.open_wait is False
    assert calls[0][0].startswith("postgresql://user:")
    assert calls[0][0].endswith("@example.invalid:5432/db")
    assert calls[0][1]["open"] is False
    assert calls[0][1]["min_size"] == db._PG_POOL_MIN_SIZE
    assert calls[0][1]["max_size"] == db._PG_POOL_MAX_SIZE
    assert calls[0][1]["check"] is DummyConnectionPool.check_connection


def test_pg_schema_ensure_takes_cross_process_advisory_lock(monkeypatch):
    db = _reload_pg_db(monkeypatch)
    events: list[str] = []

    class Connection:
        def execute(self, sql, params=()):
            events.append(sql)

    monkeypatch.setattr(db, "_ensure_schema", lambda _conn: events.append("schema"))

    db._ensure_schema_serialized(Connection())

    assert events == [
        "SELECT pg_advisory_xact_lock(hashtext('thoth-schema'))",
        "schema",
    ]
