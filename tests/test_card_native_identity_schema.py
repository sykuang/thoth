"""Additive native identity migration contracts; PostgreSQL tests are offline SQL contracts."""
import pytest

from backend.core import bank_pg
from backend.core.store import BankStore


def test_sqlite_legacy_card_identity_migration_is_nullable_and_preserves_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("native_schema", user_id=1)
    store.upsert_card_billed([{"card_no": "1234", "date": "2026-07-01", "amount": 500, "desc": "SHOP"}])
    store.conn.execute("UPDATE card_billed_txns SET description_overwrite='keep note'")
    for table in ("card_billed_txns", "card_pending_txns"):
        store.conn.execute(f"ALTER TABLE {table} DROP COLUMN native_identity")
    store.conn.commit()
    store.close()
    reopened = BankStore("native_schema", user_id=1)
    try:
        for table in ("card_billed_txns", "card_pending_txns"):
            column = next(row for row in reopened.conn.execute(f"PRAGMA table_info({table})")
                          if row["name"] == "native_identity")
            assert column["type"] == "TEXT"
            assert column["notnull"] == 0
            assert column["dflt_value"] is None
        row = reopened.conn.execute("SELECT amount, description_overwrite, native_identity FROM card_billed_txns").fetchone()
        assert tuple(row) == (500, "keep note", None)
        assert reopened.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        reopened.close()


def test_postgres_native_identity_addition_retries_failed_ddl():
    class Cursor:
        def __init__(self, rows=()):
            self.rows = list(rows)

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.rows[0] if self.rows else None

    class Conn:
        def __init__(self):
            self.calls = []
            self.fail = True
            self.rollbacks = 0

        def execute(self, sql, params=()):
            self.calls.append(sql)
            if "FROM pg_namespace WHERE nspname" in sql:
                return Cursor([(71,)])
            if "information_schema.tables" in sql:
                return Cursor([("card_billed_txns",), ("card_pending_txns",)])
            if "ADD COLUMN IF NOT EXISTS native_identity" in sql and self.fail:
                self.fail = False
                raise RuntimeError("synthetic native DDL failure")
            return Cursor()

        def commit(self):
            pass

        def rollback(self):
            self.rollbacks += 1

    conn = Conn()
    schema = "bank_native_identity_synthetic"
    with pytest.raises(RuntimeError, match="synthetic native DDL failure"):
        bank_pg._ensure_phase_c_user_id_pg(conn, schema)
    assert conn.rollbacks == 1
    assert schema not in bank_pg._PHASE_C_PG_MIGRATED
    bank_pg._ensure_phase_c_user_id_pg(conn, schema)
    for table in ("card_billed_txns", "card_pending_txns"):
        assert f'ALTER TABLE "{schema}"."{table}" ADD COLUMN IF NOT EXISTS native_identity TEXT' in conn.calls
    assert schema not in bank_pg._PHASE_C_PG_MIGRATED  # Partial schema stays retryable.


def test_bank_pg_rows_support_native_occurrence_guards(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("native_rows", user_id=1)
    try:
        # Exercise real SQL with PostgreSQL-compatible mapping/iteration semantics.
        store.conn.row_factory = lambda cursor, values: bank_pg.Row(
            dict(zip([column[0] for column in cursor.description], values, strict=True)),
            [column[0] for column in cursor.description],
        )
        row = {"card_no": "1234", "date": "2026-07-01", "post_date": "2026-07-03",
               "amount": 500, "desc": "SHOP CITY", "native_identity": "synthetic-native",
               "booking_status": "booked", "txn_type": "spending", "currency": "TWD"}
        assert store.refresh_card_pending("unbilled", [row], fetch_ok=True) == 0
        assert store.upsert_card_billed([{**row, "bill_date": "2026-07-18", "desc": "SHOP"}]) == 0
        actual = store.conn.execute("SELECT amount, bill_date FROM card_billed_txns").fetchall()
        assert [tuple(item) for item in actual] == [(500, "2026-07-18")]
    finally:
        store.close()
