"""Regression: PG bank schemas auto-migrate user_id column on first connect.

Prod 500 bug (2026-06-18, deploy 0.1.25):
    GET /transactions/list → sqlite3.OperationalError:
        column "user_id" does not exist
        LINE 1: SELECT * FROM twd_transactions WHERE user_id = $1 ...

Root cause: Phase C (Path A multi-user) added `WHERE user_id = ?` to all
router queries + wrote SQLite migration `_ensure_phase_c_user_id` to backfill
the column on legacy DBs, but **never wrote the PG mirror**. Prod PG schemas
created before Phase C had no user_id column → instant 500 on any read.

Fix: bank_pg.Connection.__init__ calls _ensure_phase_c_user_id_pg, which
mirrors the SQLite migration and revalidates every opened schema authority.

These tests verify:
1. Legacy schema (no user_id columns) gets backfilled on first connect
2. Repeated connects are idempotent and replacement-sensitive
3. Composite UNIQUE INDEX supports INSERT ... ON CONFLICT (user_id, ...)
4. Partial schemas remain retryable
"""
from __future__ import annotations

import importlib
import os

import pytest

DATABASE_URL = os.environ.get("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set")


@pytest.fixture(autouse=True)
def _pg_backend(monkeypatch):
    monkeypatch.setenv("DB_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    from backend.core import bank_pg
    importlib.reload(bank_pg)
    bank_pg._reset_phase_c_pg_cache()
    import psycopg  # type: ignore[import-not-found]
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute('DROP SCHEMA IF EXISTS bank_phaseclegacy CASCADE')
        conn.commit()
    yield
    bank_pg._reset_phase_c_pg_cache()
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute('DROP SCHEMA IF EXISTS bank_phaseclegacy CASCADE')
        conn.commit()


def _create_legacy_pg_schema():
    """Simulate a pre-Phase-C PG schema: tables exist without user_id column."""
    import psycopg  # type: ignore[import-not-found]
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute('CREATE SCHEMA bank_phaseclegacy')
        # Mimic a row of the bank tables that existed before Phase C
        # (no user_id, no composite UNIQUE INDEX).
        conn.execute("""
            CREATE TABLE bank_phaseclegacy.twd_transactions (
                id BIGSERIAL PRIMARY KEY,
                dedup_key TEXT NOT NULL UNIQUE,
                account_no TEXT,
                amount REAL,
                txn_datetime TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE bank_phaseclegacy.card_billed_txns (
                id BIGSERIAL PRIMARY KEY,
                dedup_key TEXT NOT NULL UNIQUE,
                card_no TEXT,
                amount REAL,
                consume_date TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE bank_phaseclegacy.accounts (
                account_no TEXT PRIMARY KEY,
                raw_balance REAL,
                currency TEXT
            )
        """)
        conn.execute("""
            INSERT INTO bank_phaseclegacy.twd_transactions (dedup_key, account_no, amount, txn_datetime)
            VALUES ('legacy-row-1', '00112233', 100.0, '2026-06-01T10:00:00')
        """)
        conn.commit()


def test_legacy_pg_schema_gets_user_id_column_on_first_connect():
    """Connection.__init__ runs Phase C migration on legacy schemas."""
    _create_legacy_pg_schema()
    from backend.core import bank_pg

    # First connect triggers migration
    conn = bank_pg.Connection("phaseclegacy")
    try:
        # Verify user_id column exists on all Phase C tables
        import psycopg  # type: ignore[import-not-found]
        with psycopg.connect(DATABASE_URL) as audit:
            for tbl in ("twd_transactions", "card_billed_txns", "accounts"):
                cur = audit.execute(
                    """SELECT column_name FROM information_schema.columns
                       WHERE table_schema = 'bank_phaseclegacy'
                         AND table_name = %s
                         AND column_name = 'user_id'""",
                    (tbl,),
                )
                row = cur.fetchone()
                assert row is not None, f"user_id column missing on {tbl} after Connection.__init__"
    finally:
        conn.close()


def test_legacy_pg_rows_backfilled_to_user_id_1():
    """Pre-existing rows get user_id=1 default (single-user legacy semantic)."""
    _create_legacy_pg_schema()
    from backend.core import bank_pg

    conn = bank_pg.Connection("phaseclegacy")
    try:
        import psycopg  # type: ignore[import-not-found]
        with psycopg.connect(DATABASE_URL) as audit:
            cur = audit.execute(
                "SELECT user_id FROM bank_phaseclegacy.twd_transactions WHERE dedup_key = 'legacy-row-1'"
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == 1, f"legacy row not backfilled to user_id=1, got {row[0]}"
    finally:
        conn.close()


def test_legacy_pg_dedup_indexes_become_user_scoped():
    _create_legacy_pg_schema()
    from backend.core import bank_pg
    import psycopg  # type: ignore[import-not-found]

    conn = bank_pg.Connection("phaseclegacy")
    conn.close()
    with psycopg.connect(DATABASE_URL) as audit:
        audit.execute(
            "INSERT INTO bank_phaseclegacy.twd_transactions "
            "(user_id, dedup_key, account_no, amount, txn_datetime) "
            "VALUES (2, 'legacy-row-1', 'OTHER', 1, '2026-06-02')"
        )
        columns = audit.execute(
            """SELECT a.attname
               FROM pg_index i
               CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS key(attnum, ord)
               JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=key.attnum
               JOIN pg_class c ON c.oid=i.indexrelid
               JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname='bank_phaseclegacy' AND c.relname='ux_twd_dedup'
               ORDER BY key.ord"""
        ).fetchall()
        assert columns == [("user_id",), ("dedup_key",)]


def test_legacy_pg_account_currency_is_canonicalized_before_identity_migration():
    _create_legacy_pg_schema()
    import psycopg  # type: ignore[import-not-found]
    with psycopg.connect(DATABASE_URL) as seed:
        seed.execute(
            "ALTER TABLE bank_phaseclegacy.twd_transactions ADD COLUMN user_id INTEGER"
        )
        seed.execute(
            "INSERT INTO bank_phaseclegacy.accounts (account_no, currency) VALUES (%s, %s)",
            ("FOREIGN", " usd "),
        )
        seed.commit()

    from backend.core import bank_pg
    conn = bank_pg.Connection("phaseclegacy")
    conn.close()

    with psycopg.connect(DATABASE_URL) as audit:
        row = audit.execute(
            "SELECT currency FROM bank_phaseclegacy.accounts WHERE account_no = 'FOREIGN'"
        ).fetchone()
        assert row == ("USD",)
        nullable = audit.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema='bank_phaseclegacy' "
            "AND table_name='twd_transactions' AND column_name='user_id'"
        ).fetchone()
        assert nullable == ("NO",)


def test_legacy_pg_invalid_account_currency_fails_closed():
    _create_legacy_pg_schema()
    import psycopg  # type: ignore[import-not-found]
    with psycopg.connect(DATABASE_URL) as seed:
        seed.execute(
            "INSERT INTO bank_phaseclegacy.accounts (account_no, currency) VALUES (%s, %s)",
            ("INVALID", "US$"),
        )
        seed.commit()

    from backend.core import bank_pg
    with pytest.raises(RuntimeError, match="invalid legacy account currency"):
        bank_pg.Connection("phaseclegacy")
    assert "bank_phaseclegacy" not in bank_pg._PHASE_C_PG_MIGRATED


def test_partial_phase_c_schema_remains_retryable():
    """A legacy subset is migrated but never cached as a complete schema."""
    _create_legacy_pg_schema()
    from backend.core import bank_pg

    conn1 = bank_pg.Connection("phaseclegacy")
    conn1.close()
    assert "bank_phaseclegacy" not in bank_pg._PHASE_C_PG_MIGRATED

    sentinel_called = []
    original = bank_pg._ensure_phase_c_user_id_pg

    def _spy(conn, schema):
        sentinel_called.append(schema)
        original(conn, schema)

    bank_pg._ensure_phase_c_user_id_pg = _spy
    try:
        conn2 = bank_pg.Connection("phaseclegacy")
        conn2.close()
        assert sentinel_called == ["bank_phaseclegacy"]
        assert "bank_phaseclegacy" not in bank_pg._PHASE_C_PG_MIGRATED
    finally:
        bank_pg._ensure_phase_c_user_id_pg = original


def test_router_select_with_user_id_works_after_migration():
    """Real prod regression: SELECT ... WHERE user_id = $1 must not raise."""
    _create_legacy_pg_schema()
    from backend.core import bank_pg
    import sqlite3 as _sqlite3

    conn = bank_pg.Connection("phaseclegacy")
    try:
        # This is the exact pattern that crashed in prod (transactions.py:498)
        cur = conn.execute(
            "SELECT * FROM twd_transactions WHERE user_id = ? ORDER BY txn_datetime DESC",
            (1,),
        )
        rows = cur.fetchall()
        # Legacy row was backfilled to user_id=1 so it should come back
        assert len(rows) == 1
        assert rows[0]["dedup_key"] == "legacy-row-1"
    except _sqlite3.OperationalError as e:
        pytest.fail(f"Phase C user_id query crashed (regression): {e}")
    finally:
        conn.close()


def test_composite_unique_index_created_for_insert_on_conflict():
    """INSERT ... ON CONFLICT (user_id, dedup_key) needs composite UNIQUE INDEX."""
    _create_legacy_pg_schema()
    from backend.core import bank_pg

    conn = bank_pg.Connection("phaseclegacy")
    try:
        import psycopg  # type: ignore[import-not-found]
        with psycopg.connect(DATABASE_URL) as audit:
            # Composite UNIQUE INDEX must exist for ON CONFLICT to work
            cur = audit.execute(
                """SELECT indexname FROM pg_indexes
                   WHERE schemaname = 'bank_phaseclegacy'
                     AND indexname = 'ux_twd_dedup'"""
            )
            row = cur.fetchone()
            assert row is not None, "composite UNIQUE INDEX ux_twd_dedup missing"
    finally:
        conn.close()


def test_fresh_pg_schema_waits_for_tables_before_caching_migration():
    """An empty schema remains retryable until BankStore creates its tables."""
    from backend.core import bank_pg

    conn = bank_pg.Connection("phaseclegacy")
    try:
        assert "bank_phaseclegacy" not in bank_pg._PHASE_C_PG_MIGRATED
    finally:
        conn.close()


def test_legacy_single_column_pk_swapped_to_composite():
    """Phase C-pk (2026-06-18): prod regression — Cathay UniqueViolation.

    Multi-tenant and multi-currency INSERTs require the composite
    (user_id, account_no, currency) identity.
    """
    _create_legacy_pg_schema()
    from backend.core import bank_pg

    conn = bank_pg.Connection("phaseclegacy")
    try:
        import psycopg  # type: ignore[import-not-found]
        with psycopg.connect(DATABASE_URL) as audit:
            # The migration and this audit both preserve index key ordinality.
            cur = audit.execute(
                """SELECT a.attname
                   FROM pg_index i
                   JOIN pg_class c ON c.oid = i.indrelid
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   CROSS JOIN LATERAL unnest(i.indkey::smallint[])
                       WITH ORDINALITY AS key(attnum, ordinality)
                   JOIN pg_attribute a
                     ON a.attrelid = i.indrelid AND a.attnum = key.attnum
                   WHERE i.indisprimary
                     AND n.nspname = 'bank_phaseclegacy'
                     AND c.relname = 'accounts'
                   ORDER BY key.ordinality"""
            )
            pk_cols = [r[0] for r in cur.fetchall()]
            assert pk_cols == ["user_id", "account_no", "currency"], (
                f"unexpected composite PK: {pk_cols}"
            )
    finally:
        conn.close()


def test_multi_user_same_account_no_no_pk_violation():
    """Prod regression: user 1 + user 6 both have account_no=900000057055
    after a shared bank (cathay) is connected. Without composite PK swap,
    second INSERT raises UniqueViolation: accounts_pkey.
    """
    _create_legacy_pg_schema()
    from backend.core import bank_pg

    conn = bank_pg.Connection("phaseclegacy")
    try:
        # Insert account_no=999 for user_id=1 then user_id=6
        # The legacy row was backfilled to user_id=1 (account_no='00112233' from fixture)
        # but the schema only had twd_transactions there; accounts is separate.
        # First add an accounts row for user_id=1
        conn.execute(
            "INSERT INTO accounts (user_id, account_no, raw_balance, currency) "
            "VALUES (?, ?, ?, ?)",
            (1, "900000057055", 12345.0, "TWD"),
        )
        conn.commit()
        # Now insert SAME account_no for user_id=6 — must succeed
        conn.execute(
            "INSERT INTO accounts (user_id, account_no, raw_balance, currency) "
            "VALUES (?, ?, ?, ?)",
            (6, "900000057055", 67890.0, "TWD"),
        )
        conn.commit()

        # Verify both rows exist
        cur = conn.execute(
            "SELECT user_id, raw_balance FROM accounts WHERE account_no = ? ORDER BY user_id",
            ("900000057055",),
        )
        rows = cur.fetchall()
        assert len(rows) == 2
        assert rows[0]["user_id"] == 1
        assert rows[1]["user_id"] == 6
    finally:
        conn.close()


def test_pg_currency_canonicalization_merges_collisions_and_privacy_fields():
    from backend.core.store import BankStore
    import psycopg  # type: ignore[import-not-found]

    first = BankStore("phaseclegacy", user_id=1)
    first.close()
    with psycopg.connect(DATABASE_URL) as seed:
        seed.execute("""
            INSERT INTO bank_phaseclegacy.accounts
                (user_id, account_no, currency, raw_balance, excluded,
                 nickname_overwrite, updated_at)
            VALUES
                (1, 'SHARED', 'USD', 1, 1, 'Private', '2026-09-29'),
                (1, 'SHARED', ' usd ', NULL, 0, NULL, '2026-09-30')
        """)
        seed.commit()

    second = BankStore("phaseclegacy", user_id=1)
    second.close()
    with psycopg.connect(DATABASE_URL) as audit:
        rows = audit.execute("""
            SELECT currency, raw_balance, excluded, nickname_overwrite
            FROM bank_phaseclegacy.accounts
            WHERE user_id = 1 AND account_no = 'SHARED'
        """).fetchall()
    assert rows == [("USD", 1.0, 1, "Private")]


def test_pg_migration_revalidates_replaced_table_in_cached_schema():
    from backend.core import bank_pg
    from backend.core.store import BankStore
    import psycopg  # type: ignore[import-not-found]

    first = BankStore("phaseclegacy", user_id=1)
    first.close()
    assert "bank_phaseclegacy" in bank_pg._PHASE_C_PG_MIGRATED

    with psycopg.connect(DATABASE_URL) as seed:
        seed.execute("DROP TABLE bank_phaseclegacy.accounts")
        seed.execute("""
            CREATE TABLE bank_phaseclegacy.accounts (
                user_id INTEGER NOT NULL DEFAULT 1,
                account_no TEXT NOT NULL,
                currency TEXT,
                PRIMARY KEY (user_id, account_no)
            )
        """)
        seed.commit()

    second = BankStore("phaseclegacy", user_id=1)
    second.close()
    with psycopg.connect(DATABASE_URL) as audit:
        columns = audit.execute("""
            SELECT a.attname
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            CROSS JOIN LATERAL unnest(i.indkey::smallint[])
                WITH ORDINALITY AS key(attnum, ordinality)
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = key.attnum
            WHERE i.indisprimary AND n.nspname = 'bank_phaseclegacy'
              AND c.relname = 'accounts'
            ORDER BY key.ordinality
        """).fetchall()
    assert [row[0] for row in columns] == ["user_id", "account_no", "currency"]
