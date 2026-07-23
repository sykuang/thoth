"""Bank data storage follows DB_BACKEND for the whole data layer."""
from __future__ import annotations

import importlib
import os

import pytest


DATABASE_URL = os.environ.get("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set")


@pytest.fixture(autouse=True)
def _pg_bank_backend(monkeypatch):
    monkeypatch.setenv("DB_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    from backend.core import bank_pg
    from backend.core import bank_data
    from backend.core import store
    importlib.reload(bank_pg)
    importlib.reload(bank_data)
    importlib.reload(store)
    # cleanup the test schema before and after; using a fixed bank isolates scope.
    import psycopg
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute('DROP SCHEMA IF EXISTS bank_pytest CASCADE')
        conn.commit()
    yield
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute('DROP SCHEMA IF EXISTS bank_pytest CASCADE')
        conn.commit()


def test_bankstore_uses_postgres_when_db_backend_postgres():
    from backend.core.store import BankStore
    from backend.core import bank_data

    store = BankStore("pytest")
    try:
        assert store.db_path is None
        assert bank_data.has_table(store.conn, "accounts")
        store.upsert_accounts([
            {
                "account_no": "00112233",
                "currency": "TWD",
                "nickname": "測試帳戶",
                "type": "活存",
                "product_type": "deposit",
                "raw_balance": 12345,
                "raw_balance_date": "2026-06-16",
            }
        ])
        row = store.conn.execute("SELECT account_no, raw_balance FROM accounts").fetchone()
        assert row["account_no"] == "00112233"
        assert int(row["raw_balance"]) == 12345
    finally:
        store.close()


def test_router_helpers_read_postgres_bank_schema():
    from backend.core.store import BankStore
    from backend.server.routers.portfolio import _bank_accounts

    store = BankStore("pytest")
    try:
        store.upsert_accounts([
            {
                "account_no": "00112233",
                "currency": "TWD",
                "nickname": "測試帳戶",
                "type": "活存",
                "product_type": "deposit",
                "raw_balance": 12345,
                "raw_balance_date": "2026-06-16",
            }
        ])
        store.upsert_cards([
            {
                "number": "****7016",
                "name": "測試卡",
                "association": "VISA",
                "type": "信用卡",
                "credit_limit": 100000,
                "used_credit": 12000,
                "payment_due_date": "2026-06-30",
            }
        ])
        accounts = _bank_accounts(store.conn, "pytest")
        assert len(accounts) == 1
        assert accounts[0].balance == 12345
        assert accounts[0].nickname == "測試帳戶"

        # Directly verify card query primitives through the same connection;
        # list_cards auth is covered elsewhere.
        row = store.conn.execute("SELECT card_no, name, credit_limit, used_credit FROM cards").fetchone()
        assert row["card_no"] == "****7016"
        assert int(row["credit_limit"]) == 100000
    finally:
        store.close()
