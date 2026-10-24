"""Synthetic HTTP privacy contract for all raw transaction producers."""
import json
import sqlite3
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from backend.core.store import BankStore
from backend.server import db
from backend.server.deps import current_user
from backend.server.routers import cache, replica, transactions
from tests.test_card_booking_consumers import card_client as _card_client_fixture

card_client = _card_client_fixture


@pytest.mark.parametrize("kind", ["billed", "pending", "twd"])
def test_http_raw_projection_keeps_private_identity_server_side(card_client, monkeypatch, kind):
    monkeypatch.setenv("SERVER_FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(transactions, "AccountsRepo", lambda: SimpleNamespace(
        list_for_user=lambda uid: [SimpleNamespace(bank="esun")]))
    card_client.app.include_router(replica.router)
    card_client.app.include_router(cache.router)
    table = {"billed": "card_billed_txns", "pending": "card_pending_txns", "twd": "twd_transactions"}[kind]
    store = BankStore("esun", user_id=1)
    try:
        for sibling in ("card_billed_txns", "card_pending_txns", "twd_transactions"):
            store.conn.execute(f"DELETE FROM {sibling}")
        # Future/internal credential columns must not silently become public raw
        # fields either. These values are deliberately synthetic, never bank data.
        cols = {r["name"] for r in store.conn.execute(f"PRAGMA table_info({table})")}
        for col in ("native_identity", "source_account_id"):
            if col not in cols:
                store.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
        if kind == "twd":
            store.conn.execute(f"INSERT INTO {table} (user_id, account_no, txn_datetime, description, expend, dedup_key, first_seen) VALUES (1, 'synthetic', '2026-06-24', 'privacy probe', 400, 'privacy', '2026-06-24')")
        else:
            metadata = "dedup_key, first_seen" if kind == "billed" else "scope, refreshed_at"
            store.conn.execute(f"INSERT INTO {table} (user_id, card_no, consume_date, post_date, description, amount, {metadata}) VALUES (1, '1234', '2026-06-24', '2026-06-24', 'privacy probe', 400, 'privacy', '2026-06-24')")
        store.conn.execute(f"UPDATE {table} SET native_identity = 'SYNTHETIC-PRIVATE-REFERENCE', source_account_id = 'SYNTHETIC-CREDENTIAL-REFERENCE'")
        txn_id = store.conn.execute(f"SELECT id FROM {table}").fetchone()["id"]
        store.conn.commit()
    finally:
        store.close()

    uri = f"/transactions/esun/{kind}/{txn_id}"
    listed = card_client.get("/transactions", params={"bank": "esun", "kind": kind})
    assert listed.status_code == 200
    detail = card_client.get(uri)
    patched = card_client.patch(uri, json={"description_overwrite": "synthetic edit"})
    assert detail.status_code == patched.status_code == 200
    assert patched.json()["id"] == txn_id
    assert card_client.get(uri).json()["description_overwrite"] == "synthetic edit"
    bootstrap = card_client.get("/replica/bootstrap")
    assert bootstrap.status_code == 200, bootstrap.text
    card = card_client.get("/cards/esun/1234")
    assert card.status_code == 200
    snapshot = card_client.get("/cache/snapshot")
    assert snapshot.status_code == 200
    for response in (listed.json(), detail.json(), patched.json(), bootstrap.json(), card.json(), snapshot.json()):
        def check(value):
            if isinstance(value, dict):
                assert "native_identity" not in value
                # A loan's explicit public source identity is a separate contract;
                # this fixture has no loans and must expose no raw credential ref.
                assert "source_account_id" not in value
                for child in value.values():
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)
            elif isinstance(value, str):
                assert "SYNTHETIC-PRIVATE-REFERENCE" not in value
                assert "SYNTHETIC-CREDENTIAL-REFERENCE" not in value
        check(response)
    store = BankStore("esun", user_id=1)
    try:
        row = store.conn.execute(f"SELECT native_identity, source_account_id FROM {table} WHERE id = ?", (txn_id,)).fetchone()
        assert row["native_identity"] == "SYNTHETIC-PRIVATE-REFERENCE"
        assert row["source_account_id"] == "SYNTHETIC-CREDENTIAL-REFERENCE"
    finally:
        store.close()
    wrong_kind = "pending" if kind != "pending" else "billed"
    wrong_uri = f"/transactions/esun/{wrong_kind}/{txn_id}"
    assert card_client.get(wrong_uri).status_code == 404
    assert card_client.patch(wrong_uri, json={"category": "forbidden"}).status_code == 404
    card_client.app.dependency_overrides[current_user] = lambda: {"id": 2}
    assert card_client.get(uri).status_code == 404
    assert card_client.patch(uri, json={"category": "forbidden"}).status_code == 404
    assert card_client.get("/transactions", params={"bank": "esun"}).json()["total"] == 0


@pytest.mark.parametrize("phase", ["pending", "booked", "statement"])
def test_real_ctbc_writer_cannot_leak_native_reference_via_derived_keys(
    card_client, monkeypatch, phase,
):
    from backend.core.persist import persist_collected
    from tests.test_ctbc_native_card_identity import payload, statement, unbilled

    monkeypatch.setenv("SERVER_FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(transactions, "AccountsRepo", lambda: SimpleNamespace(
        list_for_user=lambda uid: [SimpleNamespace(bank="ctbc")]))
    card_client.app.include_router(replica.router)
    card_client.app.include_router(cache.router)
    reference = "synthetic-private-ctbc-reference"
    store = BankStore("ctbc", user_id=1, source_account_id=101)
    try:
        store.upsert_cards([{"number": "****1234", "name": "Synthetic native card"}])
        row = unbilled(ref=reference)
        if phase == "pending":
            row["postingDt"] = "00000000"
        persist_collected("ctbc", payload(pending=[row]), store)
    finally:
        store.close()
    store = BankStore("ctbc", user_id=1, source_account_id=101)
    try:
        if phase == "statement":
            persist_collected("ctbc", payload(billed=[statement(ref=reference)]), store)
        kind = "pending" if phase == "pending" else "billed"
        table = "card_pending_txns" if kind == "pending" else "card_billed_txns"
        rows = store.conn.execute(f"SELECT * FROM {table}").fetchall()
        assert len(rows) == 1
        txn_id = rows[0]["id"]
        database_path = store.db_path
        assert database_path is not None
        assert reference in rows[0]["native_identity"]
        if kind == "billed":
            assert reference in rows[0]["dedup_key"]
    finally:
        store.close()

    uri = f"/transactions/ctbc/{kind}/{txn_id}"
    def open_conn(bank):
        if bank != "ctbc":
            return None
        connection = sqlite3.connect(database_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(db, "open_bank_conn", open_conn)
    listed = card_client.get("/transactions", params={"bank": "ctbc", "kind": kind})
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["id"] == txn_id
    responses = [
        listed,
        card_client.get(uri),
        card_client.patch(uri, json={"description_overwrite": "synthetic edit"}),
        card_client.get("/cache/snapshot"),
        card_client.get("/replica/bootstrap"),
        card_client.get("/cards/ctbc/****1234"),
    ]
    for response in responses:
        assert response.status_code == 200
        public = json.dumps(response.json())
        assert reference not in public
        for key in ("native_identity", "dedup_key", "source_account_id"):
            assert f'"{key}"' not in public
    store = BankStore("ctbc", user_id=1, source_account_id=101)
    try:
        durable = store.conn.execute(f"SELECT * FROM {table} WHERE id = ?", (txn_id,)).fetchone()
        assert durable is not None
        assert reference in durable["native_identity"]
        assert durable["description_overwrite"] == "synthetic edit"
        if kind == "billed":
            assert reference in durable["dedup_key"]
    finally:
        store.close()
