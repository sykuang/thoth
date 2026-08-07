"""Phase 9 (2026-06-15): DB backend portability smoke test.

驗證 db.py 在 DB_BACKEND=postgres 時能：
1. _ensure_schema 正常跑（含 ALTER TABLE column-exists 邏輯）
2. users.create_user + get_user_by_email INSERT/SELECT 正常
3. RETURNING id 在 PG 正常 fetch
4. ON CONFLICT DO UPDATE 正常 upsert
5. IntegrityError unified import 正常 catch

Skipped if no `DATABASE_URL` env set or psycopg not installed。
本地測試方法：
  docker run -d --name thoth-pg-test -p 5433:5432 \\
    -e POSTGRES_PASSWORD=test -e POSTGRES_DB=thoth_test postgres:16-alpine
  DATABASE_URL=postgresql://postgres:test@localhost:5433/thoth_test \\
    DB_BACKEND=postgres .venv/bin/pytest tests/test_db_backend_portability.py -v
"""
from __future__ import annotations

import importlib
import os

import pytest

# 條件 skip：沒設 DATABASE_URL 跳整檔
DATABASE_URL = os.environ.get("DATABASE_URL", "")
SKIP_REASON = (
    "DATABASE_URL not set — set to "
    "'postgresql://postgres:test@localhost:5433/thoth_test' to run"
)
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason=SKIP_REASON)


@pytest.fixture(autouse=True)
def _force_pg_backend(monkeypatch):
    """強制 DB_BACKEND=postgres + 重 reload db module 重新讀 env。"""
    monkeypatch.setenv("DB_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    # SERVER_FERNET_KEY for creds_store
    monkeypatch.setenv(
        "SERVER_FERNET_KEY",
        "bJY-CYFV7z6hwng8R9jRghCl6fINjm8N_0cjcRY48IE=",
    )
    # 強制 reload db module — env 在 module level 讀，必須 reimport
    import backend.server.db
    importlib.reload(backend.server.db)
    import backend.server.users
    importlib.reload(backend.server.users)
    import backend.server.creds_store
    importlib.reload(backend.server.creds_store)
    yield
    # 清空測試資料
    from backend.server.db import get_conn
    with get_conn() as conn:
        for table in [
            "snaptrade_locks",
            "brokerage_activities",
            "brokerage_positions",
            "brokerage_balances",
            "brokerage_accounts",
            "snaptrade_users",
            "user_preferences",
            "category_rules",
            "sync_jobs",
            "bank_credentials_v2",
            "bank_credentials",
            "bank_accounts",
            "users",
        ]:
            conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    import backend.server.db
    backend.server.db._schema_ensured = False
    backend.server.db._pg_pool = None


def test_schema_creates_all_tables_on_postgres():
    """_ensure_schema 跑完所有 CREATE TABLE 後，information_schema 應有全部表。"""
    from backend.server.db import get_conn

    with get_conn() as conn:
        cur = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' ORDER BY table_name"
        )
        tables = {r[0] for r in cur.fetchall()}

    expected = {
        "bank_accounts",
        "bank_credentials",
        "bank_credentials_v2",
        "brokerage_accounts",
        "brokerage_balances",
        "brokerage_positions",
        "brokerage_activities",
        "category_rules",
        "snaptrade_locks",
        "snaptrade_users",
        "sync_jobs",
        "user_preferences",
        "users",
    }
    assert expected.issubset(tables), f"missing tables: {expected - tables}"


def test_create_user_returning_id_works_on_postgres():
    """users.create_user 使用 RETURNING id 應正常拿到 user_id。"""
    from backend.server.users import create_user, get_user_by_email

    uid = create_user(email="pg-smoke@example.com", password="SyntheticTestPassword01!")
    assert uid > 0

    user = get_user_by_email("pg-smoke@example.com")
    assert user is not None
    assert user["id"] == uid
    assert user["email"] == "pg-smoke@example.com"
    # created_at 該是 ISO 8601 UTC w/ ms precision
    assert user["created_at"].endswith("Z")
    assert "T" in user["created_at"]


def test_duplicate_email_raises_user_exists_error_on_postgres():
    """SQLite IntegrityError vs PG UniqueViolation 都應被 db.IntegrityError 統一捕捉。"""
    from backend.server.users import UserExistsError, create_user

    create_user(email="dup@example.com", password="SyntheticTestPassword01!")
    with pytest.raises(UserExistsError):
        create_user(email="dup@example.com", password="SyntheticTestPassword01!")


def test_on_conflict_do_update_upsert_works_on_postgres():
    """creds_store put() 用 ON CONFLICT DO UPDATE upsert 應在 PG 正常運作。"""
    from backend.server.creds_store import LocalFernetBackend
    from backend.server.users import create_user

    uid = create_user(email="upsert@example.com", password="SyntheticTestPassword01!")
    store = LocalFernetBackend()

    # 第一次 put — INSERT
    store.put(user_id=uid, bank="dbs", field="username", plain="alice")
    assert store.get(user_id=uid, bank="dbs", field="username") == "alice"

    # 第二次 put 同 (user, bank, field) — ON CONFLICT DO UPDATE
    store.put(user_id=uid, bank="dbs", field="username", plain="bob")
    assert store.get(user_id=uid, bank="dbs", field="username") == "bob"


def test_column_exists_helper_uses_information_schema_on_postgres():
    """_columns() 在 PG 應走 information_schema 而非 PRAGMA。"""
    from backend.server.db import _columns, get_conn

    with get_conn() as conn:
        cols = _columns(conn, "users")
    assert {"id", "email", "password_hash", "created_at"}.issubset(cols)
