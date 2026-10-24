"""Public card consumers retain posted purchases without inventing statement facts."""
from __future__ import annotations

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.core import bank_pg
from backend.core.store import BankStore
from backend.server import db
from backend.server.db_facade import db_api
from backend.server.deps import current_user
from backend.server.dashboard_cache import clear_dashboard_cache
from backend.server.routers import cards, transactions


@pytest.fixture
def card_client(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(bank_pg, "enabled", lambda: False)
    store = BankStore("esun", user_id=1)
    store.upsert_cards([{
        "number": "1234", "name": "Synthetic card", "statement_close_date": "2026-06-18",
        "bill_due_amount": 700, "last_payment_amount": 50, "last_payment_date": "2026-06-19",
    }])
    store.upsert_card_billed([
        {"card_no": "1234", "date": "2026-05-20", "post_date": "2026-05-21",
         "bill_date": "2026-06-18", "desc": "closed purchase", "amount": 700},
        {"card_no": "1234", "date": "2026-06-19", "post_date": "2026-06-20",
         "bill_date": None, "desc": "new posted purchase", "amount": 200},
        {"card_no": "1234", "date": None, "post_date": None,
         "bill_date": None, "desc": "booked without date", "amount": 30},
        {"card_no": "OTHER", "date": "2026-06-20", "post_date": "2026-06-21",
         "desc": "other card", "amount": 900},
    ], rules=[])
    store.refresh_card_pending("current", [
        {"card_no": "1234", "date": "2026-06-22", "post_date": None,
         "desc": "unknown pending", "amount": 100},
    ], rules=[], fetch_ok=True)
    store.conn.execute(
        "INSERT INTO card_billed_txns (user_id, card_no, description, amount, dedup_key, first_seen) "
        "VALUES (2, '1234', 'other user', 999, 'other-user', '2026-06-20')"
    )
    store.conn.commit()
    store.close()

    def open_conn(bank):
        con = sqlite3.connect(tmp_path / "esun.sqlite", check_same_thread=False)
        con.row_factory = sqlite3.Row
        return con

    monkeypatch.setattr(db, "open_bank_conn", open_conn)
    app = FastAPI()
    app.include_router(cards.router)
    app.include_router(transactions.router)
    app.dependency_overrides[current_user] = lambda: {"id": 1}
    clear_dashboard_cache()
    with TestClient(app) as client:
        yield client
    clear_dashboard_cache()


def test_card_route_keeps_closed_cycle_and_exposes_all_posted_rows(card_client):
    response = card_client.get("/cards/esun/1234")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [t["description"] for t in body["billed_txns"]] == ["closed purchase"]
    assert [t["description"] for t in body["posted_txns"]] == [
        "new posted purchase", "closed purchase", "booked without date",
    ]
    undated = body["posted_txns"][-1]
    assert undated["date"] is None
    assert undated["post_date"] is None
    assert [t["description"] for t in body["pending_txns"]] == ["unknown pending"]
    assert body["bill_due_amount"] == 700
    assert body["last_payment_amount"] == 50
    assert body["payments"] == [{"date": "2026-06-19", "amount": 50, "description": "最近繳款"}]


def test_public_facade_preserves_legacy_cycle_scope_with_posted_collection(card_client):
    detail = db_api.get_card_detail(
        bank="esun", user_id=1, card_no="1234", cycle_start="2026-06-19",
    )
    assert detail is not None
    assert [t.description for t in detail.billed_txns] == ["new posted purchase"]
    assert {t.description for t in detail.posted_txns} == {
        "new posted purchase", "closed purchase", "booked without date",
    }


def test_unassigned_booked_rows_do_not_turn_statement_total_into_pending_balance(card_client):
    detail = card_client.get("/cards/esun/1234").json()
    summary = card_client.get("/cards?bank=esun").json()[0]
    # 100 is only the pending subtotal, not a statement-scope unbilled total.
    assert detail["unbilled_amount"] is None
    assert summary["unbilled_amount"] is None
    assert detail["bill_due_amount"] == summary["bill_due_amount"] == 700
    assert detail["last_payment_amount"] == summary["last_payment_amount"] == 50


def test_replica_public_collector_keeps_real_booking_identity_and_missing_dates(card_client):
    from backend.server.replica_facts import collect_bank_replica_facts

    facts = collect_bank_replica_facts("esun", 1)
    txns = {row["description"]: row for row in facts["transactions"]}
    assert set(txns) == {
        "closed purchase", "new posted purchase", "booked without date", "other card", "unknown pending",
    }
    assert txns["new posted purchase"]["kind"] == "billed"
    assert txns["new posted purchase"]["post_date"] == "2026-06-20"
    assert txns["booked without date"]["kind"] == "billed"
    assert txns["booked without date"]["post_date"] is None
    assert txns["unknown pending"]["kind"] == "pending"
    assert txns["unknown pending"]["consume_date"] == "2026-06-22"
    assert txns["unknown pending"]["post_date"] is None


def test_authoritative_snapshot_promotion_remains_visible_in_card_route(card_client):
    store = BankStore("esun", user_id=1)
    try:
        assert store.refresh_card_pending("promotion", [{
            "card_no": "1234", "date": "2026-06-23", "post_date": "2026-06-24",
            "booking_status": "booked", "desc": "promoted snapshot", "amount": 450,
        }], rules=[], fetch_ok=True) == 0
    finally:
        store.close()

    body = card_client.get("/cards/esun/1234").json()
    assert "promoted snapshot" in {row["description"] for row in body["posted_txns"]}
    assert "promoted snapshot" not in {row["description"] for row in body["billed_txns"]}
    assert "promoted snapshot" not in {row["description"] for row in body["pending_txns"]}
    assert body["bill_due_amount"] == 700
    assert body["unbilled_amount"] is None


def test_unassigned_bank_level_booked_purchase_makes_per_card_totals_unknown(card_client):
    store = BankStore("esun", user_id=1)
    try:
        store.upsert_cards([{"number": "5678", "name": "Second synthetic card"}])
        store.upsert_card_billed([{
            "card_no": "", "date": "2026-06-23", "post_date": "2026-06-24",
            "bill_date": None, "desc": "bank-level booked purchase", "amount": 450,
        }], rules=[])
        store.conn.commit()
    finally:
        store.close()

    summaries = card_client.get("/cards?bank=esun").json()
    assert {row["card_no"]: row["unbilled_amount"] for row in summaries} == {
        "1234": None, "5678": None,
    }


@pytest.mark.parametrize("kind", ["pending", "billed"])
@pytest.mark.parametrize("missing_post_date", [None, "", "  "])
@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("full_stats", [False, True])
def test_http_card_post_basis_requires_real_post_date(
    card_client, kind, missing_post_date, split, full_stats,
):
    # Insert canonical rows directly: this consumer contract must not depend on
    # provider promotion deciding which physical table receives the row.
    store = BankStore("esun", user_id=1)
    try:
        store.conn.execute("DELETE FROM card_billed_txns")
        store.conn.execute("DELETE FROM card_pending_txns")
        table = f"card_{kind}_txns"
        for description, consume_date, post_date, amount in [
            ("probe missing post", "2026-06-22", missing_post_date, 100),
            ("probe real post", "2026-06-22", "2026-06-24", 200),
            ("probe no dates", None, None, 300),
            ("probe post date only", None, "2026-06-24", 400),
        ]:
            metadata_cols = "dedup_key, first_seen" if kind == "billed" else "scope, refreshed_at"
            store.conn.execute(
                f"INSERT INTO {table} "
                f"(user_id, card_no, consume_date, post_date, description, amount, {metadata_cols}) "
                "VALUES (1, '1234', ?, ?, ?, ?, ?, '2026-06-24')",
                (consume_date, post_date, description, amount, description),
            )
        if split:
            store.conn.execute(
                f"UPDATE {table} SET splits_overwrite = ? WHERE amount = 100",
                ('[{"amount": 40, "category": "food"}, {"amount": 60, "category": "other"}]',),
            )
        store.conn.commit()
    finally:
        store.close()

    params = {"bank": "esun", "kind": kind}
    if full_stats:
        params["q"] = "probe"  # Public search forces full-transform stats.
    for basis, bounds, count, expense in [
        ("post", {"since": "2026-06-22", "until": "2026-06-22"}, 0, 0),
        ("post", {"since": "2026-06-24", "until": "2026-06-24"}, 2, 600),
        ("post", {}, 2, 600),
        ("post", {"until": "2026-06-24"}, 2, 600),
        ("post", {"since": "2026-06-22"}, 2, 600),

        ("consume", {"since": "2026-06-22", "until": "2026-06-22"}, 3 if split else 2, 300),
        ("consume", {"since": "2026-06-24", "until": "2026-06-24"}, 0, 0),
        ("consume", {}, 3 if split else 2, 300),
        ("consume", {"until": "2026-06-30"}, 3 if split else 2, 300),
        ("consume", {"since": "2026-06-01"}, 3 if split else 2, 300),
        ("consume", {"since": "2026-06-01", "until": "2026-06-30"}, 3 if split else 2, 300),
    ]:
        query = {**params, "card_date_basis": basis, **bounds}
        response = card_client.get("/transactions", params=query)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == count, query
        if basis == "post" and count:
            assert body["items"][0]["date"] == "2026-06-24"
            assert {row["consume_date"] for row in body["items"]} == {"2026-06-22", None}
        response = card_client.get("/transactions/stats", params=query)
        assert response.status_code == 200, response.text
        stats = response.json()
        assert stats["total"] == count, query
        assert stats["total_expense"] == expense, query


@pytest.mark.parametrize("kind", ["pending", "billed"])
@pytest.mark.parametrize("split", [False, True])
def test_http_consume_membership_precedes_every_aggregate(card_client, monkeypatch, kind, split):
    import json
    import os
    from pathlib import Path
    from cryptography.fernet import Fernet
    from backend.server.routers import replica

    monkeypatch.setenv("SERVER_FERNET_KEY", Fernet.generate_key().decode())
    original_open = db.open_bank_conn
    monkeypatch.setattr(db, "open_bank_conn", lambda bank: original_open(bank) if bank == "esun" else None)
    card_client.app.include_router(replica.router)
    store = BankStore("esun", user_id=1)
    try:
        store.conn.execute("DELETE FROM card_billed_txns")
        store.conn.execute("DELETE FROM card_pending_txns")
        store.conn.execute("UPDATE cards SET bill_due_amount = NULL")
        table = f"card_{kind}_txns"
        metadata = "dedup_key, first_seen" if kind == "billed" else "scope, refreshed_at"
        for index, consume_date in enumerate((None, "", "   ")):
            store.conn.execute(
                f"INSERT INTO {table} (user_id, card_no, consume_date, post_date, description, amount, category, flow_type, is_subscription, {metadata}) "
                "VALUES (1, '1234', ?, '2026-06-24', ?, 400, 'food', 'expense', 1, ?, '2026-06-24')",
                (consume_date, f"membership probe {index}", f"membership-{index}"),
            )
        if split:
            store.conn.execute(f"UPDATE {table} SET splits_overwrite = ? WHERE description = 'membership probe 0'",
                               ('[{"amount":150,"category":"food"},{"amount":250,"category":"transport"}]',))
        store.conn.commit()
    finally:
        store.close()
    scenarios = []
    for basis in ("consume", "post"):
        for bounds in ({}, {"until": "2026-06-30"}, {"since": "2026-06-01"},
                       {"since": "2026-06-01", "until": "2026-06-30"}):
            query = {"bank": "esun", "kind": kind, "card_date_basis": basis, **bounds}
            listed = card_client.get("/transactions", params=query)
            assert listed.status_code == 200
            count = (4 if split else 3) if basis == "post" else 0
            assert listed.json()["total"] == count
            for full in (False, True):
                response = card_client.get("/transactions/stats", params={**query, **({"q": "probe"} if full else {})})
                assert response.status_code == 200
                stats = response.json()
                assert stats["total"] == count
                assert stats["total_expense"] == (1200 if count else 0)
                assert stats["amount_by_category"] == (({"food":950, "transport":250} if split else {"food":1200}) if count else {})
                assert stats["by_category"] == (({"food":3, "transport":1} if split else {"food":3}) if count else {})
                assert stats["amount_by_flow_type"] == {"expense":1200 if count else 0, "income":0, "transfer":0, "investment":0}
                assert stats["subscription_total"] == (1200 if count else 0)
                assert stats["subscription_by_month"] == ({"2026-06":1200} if count else {})
                assert stats["by_kind"] == ({kind:count} if count else {})
                assert stats["amount_by_month"] == ({"2026-06":{"income":0,"expense":1200,"net":-1200,"count":count}} if count else {})
                scenarios.append({"basis":basis, "bounds":bounds, "full":full, "list":listed.json(), "stats":stats})
    response = card_client.get("/replica/bootstrap")
    assert response.status_code == 200, response.text
    detail = card_client.get("/cards/esun/1234")
    assert detail.status_code == 200
    assert detail.json()["bill_due_amount"] is None
    store = BankStore("esun", user_id=1)
    try:
        store.conn.execute("UPDATE cards SET bill_due_amount = 0 WHERE card_no = '1234'")
        store.conn.commit()
    finally:
        store.close()
    zero_card = card_client.get("/cards/esun/1234")
    assert zero_card.status_code == 200
    assert zero_card.json()["bill_due_amount"] == 0
    assert zero_card.json()["bill_status"] == "paid"  # Native payment facts remain intact.
    # Optional cross-runtime fixture: only actual HTTP responses, never fabricated
    # frontend equivalents. Consumer suites explicitly report a skip if not set.
    output = os.environ.get("CARD_BOOKING_HTTP_FIXTURE")
    if output:
        path = Path(output)
        collected = json.loads(path.read_text()) if path.exists() else []
        collected.append({"kind":kind, "split":split, "replica":response.json(), "card":detail.json(), "zeroCard":zero_card.json(), "scenarios":scenarios})
        path.write_text(json.dumps(collected))


@pytest.mark.parametrize("basis", ["consume", "post"])
def test_shared_card_membership_gate_keeps_non_card_siblings_and_dated_payment(basis):
    rows = [{"kind":kind, "date":None} for kind in ("twd", "manual", "loan_repayment", "payment")]
    rows.append({"kind":"billed", "txn_type":"payment", "consume_date":"2026-06-23", "post_date":"2026-06-24"})
    assert transactions._apply_stat_filters(rows, since=None, until=None, q=None, card_date_basis=basis) == rows


@pytest.mark.parametrize("second_card", [None, "active", "inactive"])
@pytest.mark.parametrize("bank_card_no", [None, ""])
def test_http_pending_totals_respect_bank_inventory_not_requested_card_subset(
    card_client, second_card, bank_card_no,
):
    store = BankStore("esun", user_id=1)
    try:
        # Keep real statement/payment facts; remove only the unassigned booked
        # purchases that would independently make these totals unknown.
        store.conn.execute("DELETE FROM card_billed_txns WHERE bill_date IS NULL")
        store.conn.execute("DELETE FROM card_pending_txns")
        if second_card:
            store.upsert_cards([{"number": "5678", "name": "Second synthetic card"}])
            if second_card == "inactive":
                store.conn.execute("UPDATE cards SET active = 0 WHERE card_no = '5678'")
        for card_no, amount in [(bank_card_no, 100), ("1234", 20), ("5678", 30)]:
            if card_no == "5678" and not second_card:
                continue
            store.conn.execute(
                "INSERT INTO card_pending_txns "
                "(user_id, scope, card_no, consume_date, description, amount, refreshed_at) "
                "VALUES (1, 'current', ?, '2026-06-22', 'pending', ?, '2026-06-22')",
                (card_no, amount),
            )
        # Other users must not make single-card attribution ambiguous.
        store.conn.execute("INSERT INTO cards (user_id, card_no, name, updated_at) "
                           "VALUES (2, '9999', 'other user', '2026-06-22')")
        store.conn.commit()
    finally:
        store.close()

    for include_inactive in [False, True]:
        response = card_client.get("/cards", params={
            "bank": "esun", "include_inactive": include_inactive,
        })
        assert response.status_code == 200, response.text
        for summary in response.json():
            card_no = summary["card_no"]
            expected = None if second_card else 120
            assert summary["unbilled_amount"] == expected
            response = card_client.get(f"/cards/esun/{card_no}")
            assert response.status_code == 200, response.text
            detail = response.json()
            assert detail["unbilled_amount"] == expected
            facade_summary = db_api.get_card(bank="esun", user_id=1, card_no=card_no)
            assert facade_summary is not None
            assert facade_summary.unbilled_amount == expected
            if card_no == "1234":
                assert detail["bill_due_amount"] == summary["bill_due_amount"] == 700
                assert detail["last_payment_amount"] == summary["last_payment_amount"] == 50
                assert detail["payments"] == [
                    {"date": "2026-06-19", "amount": 50, "description": "最近繳款"},
                ]


def test_http_attributed_pending_totals_remain_per_card(card_client):
    store = BankStore("esun", user_id=1)
    try:
        store.conn.execute("DELETE FROM card_billed_txns WHERE bill_date IS NULL")
        store.upsert_cards([{"number": "5678", "name": "Second synthetic card"}])
        store.refresh_card_pending("second", [{
            "card_no": "5678", "date": "2026-06-22", "desc": "second pending", "amount": 30,
        }], rules=[], fetch_ok=True)
        store.conn.commit()
    finally:
        store.close()
    response = card_client.get("/cards?bank=esun")
    assert response.status_code == 200, response.text
    assert {row["card_no"]: row["unbilled_amount"] for row in response.json()} == {
        "1234": 100, "5678": 30,
    }
    for card_no, expected in [("1234", 100), ("5678", 30)]:
        response = card_client.get(f"/cards/esun/{card_no}")
        assert response.status_code == 200, response.text
        assert response.json()["unbilled_amount"] == expected

