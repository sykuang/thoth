"""Revision DDL belongs to the existing schema-OID migration authority."""
from __future__ import annotations

import pytest

from backend.core import bank_pg


class MigrationConnection:
    def __init__(self):
        self.oid = 101
        self.tables = set(bank_pg._PHASE_C_PG_TABLES)
        self.calls = []
        self.rows = []
        self.fail = False
        self.fail_commit = False
        self.rollbacks = 0

    def execute(self, sql, params=()):
        self.calls.append(sql)
        if "bill_facts_revision" in sql and self.fail:
            raise RuntimeError("synthetic revision DDL failure")
        self.rows = []
        if "SELECT oid FROM pg_namespace" in sql:
            self.rows = [(self.oid,)]
        elif "SELECT table_name FROM information_schema.tables" in sql:
            self.rows = [(name,) for name in self.tables]
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def commit(self):
        if self.fail_commit:
            raise RuntimeError("synthetic commit failure")

    def rollback(self):
        self.rollbacks += 1


def test_revision_migration_once_per_schema_oid():
    conn = MigrationConnection()
    schema = "bank_revision_synthetic"
    bank_pg._ensure_phase_c_user_id_pg(conn, schema)
    revision = [sql for sql in conn.calls if "bill_facts_revision" in sql]
    assert revision == [
        f'ALTER TABLE "{schema}"."cards" ADD COLUMN IF NOT EXISTS bill_facts_revision TEXT'
    ]
    conn.calls.clear()
    bank_pg._ensure_phase_c_user_id_pg(conn, schema)
    assert conn.calls == ["SELECT oid FROM pg_namespace WHERE nspname = %s"]
    conn.oid += 1
    bank_pg._ensure_phase_c_user_id_pg(conn, schema)
    assert [sql for sql in conn.calls if "bill_facts_revision" in sql] == revision
    assert bank_pg._PHASE_C_PG_MIGRATED[schema] == conn.oid


def test_revision_ddl_failure_has_its_own_action_code():
    from backend.core.error_codes import exception_error_id

    conn = MigrationConnection()
    conn.fail = True
    with pytest.raises(RuntimeError) as exc:
        bank_pg._ensure_phase_c_user_id_pg(conn, "bank_revision_action_synthetic")
    assert exception_error_id(exc.value) == "SYS-08-173-041"
    assert conn.rollbacks == 1


@pytest.mark.parametrize("failure", ["revision", "commit", "partial", "empty"])
def test_revision_migration_does_not_cache_incomplete_authority(failure):
    conn = MigrationConnection()
    schema = "bank_revision_synthetic"
    if failure in {"revision", "commit"}:
        conn.fail = failure == "revision"
        conn.fail_commit = failure == "commit"
        with pytest.raises(RuntimeError, match="synthetic"):
            bank_pg._ensure_phase_c_user_id_pg(conn, schema)
        assert conn.rollbacks == 1
    else:
        conn.tables = {"cards"} if failure == "partial" else set()
        bank_pg._ensure_phase_c_user_id_pg(conn, schema)
    assert schema not in bank_pg._PHASE_C_PG_MIGRATED
    conn.fail = conn.fail_commit = False
    conn.tables = set(bank_pg._PHASE_C_PG_TABLES)
    bank_pg._ensure_phase_c_user_id_pg(conn, schema)
    assert any("bill_facts_revision" in sql for sql in conn.calls)
    assert bank_pg._PHASE_C_PG_MIGRATED[schema] == conn.oid
