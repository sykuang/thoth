"""Synthetic CTBC references: no live keys, sessions, or financial data."""
import pytest

from backend.core.persist.ctbc import persist_ctbc
from backend.core.store import BankStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("DB_BACKEND", raising=False)
    value = BankStore("ctbc_native_test", user_id=1, source_account_id=101)
    yield value
    value.close()


def payload(*, pending=(), billed=()):
    return {"card_api_dump": {
        "/twrbc-card/qu006/011": {"allItems": list(pending)},
        "/twrbc-card/qu002/010": {"billData": {"TWD": {"2026/09": {"bills": list(billed)}}}},
    }}


def unbilled(ref="synthetic-a", **changes):
    return {"purchaseDt": "20260910", "postingDt": "20260912", "cardNoSuffixFour": "1234_0",
            "description": "SHOP TAIPEI CITY TW", "purchaseAmt": 500,
            "origCurDesc": "TWD", "origCurAmt": 500, "txCode": "40",
            "acwRefNbr": ref, "pkIdx": "synthetic-unbilled-index", "authCode": "111111", **changes}


def statement(ref="synthetic-a", **changes):
    return {"purchaseDt": "091026", "postingDt": "091226", "cardNo": "1234_0",
            "merchantChiName": "SHOP", "ntAmt": 500, "txCode": "40",
            "acwRefNbr": ref, "pkIdx": "synthetic-statement-index", "authCode": "111111", **changes}


def billed_rows(store):
    return [dict(row) for row in store.conn.execute("SELECT * FROM card_billed_txns ORDER BY id").fetchall()]


def test_native_reference_preserves_renamed_pending_edits_and_splits(store):
    persist_ctbc(payload(pending=[unbilled(postingDt="00000000")]), store)
    store.conn.execute("UPDATE card_pending_txns SET description_overwrite='note', "
                       "splits_overwrite='[{\"amount\":200},{\"amount\":300}]'")
    store.conn.commit()
    persist_ctbc(payload(pending=[unbilled(description="RENAMED")]), store)
    actual = billed_rows(store)
    assert len(actual) == 1
    assert actual[0]["description_overwrite"] == "note"
    assert actual[0]["splits_overwrite"] == '[{"amount":200},{"amount":300}]'


@pytest.mark.parametrize("count", [1, 2])
def test_native_repeated_occurrences_replay_and_statement_keep_count(store, count):
    persist_ctbc(payload(pending=[unbilled()] * count), store)
    ids = [r["id"] for r in billed_rows(store)]
    persist_ctbc(payload(billed=[statement()] * count), store)
    assert [r["id"] for r in billed_rows(store)] == ids
    assert len(ids) == count


@pytest.mark.parametrize("changes", [
    {"acwRefNbr": "synthetic-b"}, {"cardNoSuffixFour": "1234_1"},
    {"postingDt": "20260913"}, {"purchaseAmt": -500},
])
def test_native_distinct_purchases_installments_reversals_do_not_merge(store, changes):
    persist_ctbc(payload(pending=[unbilled(), unbilled(**changes)]), store)
    assert len(billed_rows(store)) == 2
    persist_ctbc(payload(pending=[unbilled(**changes), unbilled()]), store)
    assert len(billed_rows(store)) == 2


@pytest.mark.parametrize("native", [False, True])
def test_legacy_renamed_promoted_row_fails_closed_without_pairing_evidence(store, native):
    persist_ctbc(payload(pending=[unbilled(ref=None)]), store)
    before = billed_rows(store)
    with pytest.raises(ValueError, match="native.*identity|ambiguous"):
        persist_ctbc(payload(billed=[statement(ref="synthetic-a" if native else None)]), store)
    store.rollback()
    assert billed_rows(store) == before


def test_statement_disappearance_transfers_native_pending_overlay(store):
    persist_ctbc(payload(pending=[unbilled(postingDt="00000000")]), store)
    store.conn.execute("UPDATE card_pending_txns SET description_overwrite='keep note'")
    store.conn.commit()
    persist_ctbc(payload(billed=[statement()]), store)
    assert billed_rows(store)[0]["description_overwrite"] == "keep note"


def test_pending_native_rename_keeps_overlay_before_booking(store):
    persist_ctbc(payload(pending=[unbilled(postingDt="00000000")]), store)
    store.conn.execute("UPDATE card_pending_txns SET description_overwrite='keep note'")
    store.conn.commit()
    persist_ctbc(payload(pending=[unbilled(description="RENAMED", postingDt="00000000")]), store)
    assert store.conn.execute("SELECT description_overwrite FROM card_pending_txns").fetchone()[0] == "keep note"


@pytest.mark.parametrize("bad_overlay", ["conflict", "invalid-splits", "ambiguous-repeats"])
def test_native_statement_overlay_errors_roll_back_whole_card_batch(store, bad_overlay):
    import copy

    pending = unbilled(postingDt="00000000")
    persist_ctbc(payload(pending=[pending] * (2 if bad_overlay == "ambiguous-repeats" else 1)), store)
    store.conn.execute("UPDATE card_pending_txns SET description_overwrite='keep note'")
    if bad_overlay == "invalid-splits":
        store.conn.execute("UPDATE card_pending_txns SET splits_overwrite='invalid'")
    if bad_overlay == "conflict":
        persist_ctbc(payload(pending=[pending], billed=[statement()]), store)
        store.conn.execute("UPDATE card_billed_txns SET description_overwrite='existing edit'")
    store.conn.commit()
    before_billed = billed_rows(store)
    before_pending = [dict(row) for row in store.conn.execute("SELECT * FROM card_pending_txns").fetchall()]
    names = ("_new_billed_ids", "_current_billed_ids", "_adopted_pending_raw_keys", "_unsafe_pending_overlay_identities")
    before_ledgers = {name: copy.deepcopy(getattr(store, name)) for name in names}
    with pytest.raises(ValueError, match="native|ambiguous|conflict|split"):
        persist_ctbc(payload(billed=[statement()]), store)
    assert billed_rows(store) == before_billed
    assert [dict(row) for row in store.conn.execute("SELECT * FROM card_pending_txns").fetchall()] == before_pending
    assert {name: getattr(store, name) for name in names} == before_ledgers


def test_native_unknown_overlap_preserves_pending_then_adopts_after_reopen(store):
    pending = unbilled(postingDt="00000000")
    persist_ctbc(payload(pending=[pending]), store)
    store.conn.execute("UPDATE card_pending_txns SET description_overwrite='keep note'")
    store.conn.commit()
    persist_ctbc(payload(pending=[pending], billed=[statement()]), store)
    assert billed_rows(store)[0]["description_overwrite"] is None
    other = BankStore(store.bank, user_id=1, source_account_id=101)
    try:
        persist_ctbc(payload(billed=[statement()]), other)
        assert billed_rows(other)[0]["description_overwrite"] == "keep note"
    finally:
        other.close()


def test_native_fx_currency_conflict_vetoes_same_reference(store):
    persist_ctbc(payload(pending=[unbilled(origCurDesc="USD", origCurAmt=3)]), store)
    before = billed_rows(store)
    with pytest.raises(ValueError, match="conflict"):
        persist_ctbc(payload(billed=[statement(occCurCode="EUR", foreignAmt="3")]), store)
    assert billed_rows(store) == before


def test_native_bank_user_credential_namespace_does_not_merge(store):
    persist_ctbc(payload(pending=[unbilled()]), store)
    for user_id, source in [(2, 101), (1, 102)]:
        other = BankStore(store.bank, user_id=user_id, source_account_id=source)
        try:
            persist_ctbc(payload(pending=[unbilled()]), other)
        finally:
            other.close()
    assert len(billed_rows(store)) == 3


def test_pending_native_reference_cannot_hide_fx_conflict(store):
    persist_ctbc(payload(pending=[unbilled(postingDt="00000000", origCurDesc="USD", origCurAmt=3)]), store)
    store.conn.execute("UPDATE card_pending_txns SET description_overwrite='keep note'")
    store.conn.commit()
    before = [dict(row) for row in store.conn.execute("SELECT * FROM card_pending_txns").fetchall()]
    with pytest.raises(ValueError, match="conflict"):
        persist_ctbc(payload(pending=[unbilled(postingDt="00000000", description="RENAMED",
                                             origCurDesc="EUR", origCurAmt=3)]), store)
    assert [dict(row) for row in store.conn.execute("SELECT * FROM card_pending_txns").fetchall()] == before


def test_distinct_native_pending_purchases_keep_their_own_edits(store):
    pending = [unbilled(ref=ref, postingDt="00000000") for ref in ("synthetic-a", "synthetic-b")]
    persist_ctbc(payload(pending=pending), store)
    store.conn.execute("UPDATE card_pending_txns SET description_overwrite='first' WHERE id=(SELECT MIN(id) FROM card_pending_txns)")
    store.conn.execute("UPDATE card_pending_txns SET description_overwrite='second' WHERE description_overwrite IS NULL")
    store.conn.commit()
    persist_ctbc(payload(pending=list(reversed(pending))), store)
    actual = store.conn.execute("SELECT description_overwrite FROM card_pending_txns ORDER BY id").fetchall()
    assert [row[0] for row in actual] == ["second", "first"]


def test_native_statement_counts_once_in_backend_stats_and_replica(store, monkeypatch):
    import sqlite3
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.server import db
    from backend.server.deps import current_user
    from backend.server.replica_facts import collect_bank_replica_facts
    from backend.server.routers import transactions

    persist_ctbc(payload(pending=[unbilled()]), store)
    persist_ctbc(payload(billed=[statement()]), store)

    def open_conn(_bank):
        conn = sqlite3.connect(store.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(db, "open_bank_conn", open_conn)
    facts = collect_bank_replica_facts("ctbc", 1)
    assert len(facts["transactions"]) == 1
    assert facts["transactions"][0]["cashflow_amount"] == 500
    assert facts["transactions"][0]["cashflow_direction"] == "expense"
    assert "native_identity" not in facts["transactions"][0]
    app = FastAPI()
    app.include_router(transactions.router)
    app.dependency_overrides[current_user] = lambda: {"id": 1}
    with TestClient(app) as client:
        for query in ({}, {"q": "SHOP"}):
            result = client.get("/transactions/stats", params={"bank": "ctbc", **query})
            assert result.status_code == 200
            assert result.json()["total_expense"] == 500
            assert result.json()["total"] == 1


def test_new_references_cannot_guess_between_edited_legacy_occurrences(store):
    persist_ctbc(payload(pending=[unbilled(ref=None)] * 2), store)
    store.conn.execute("UPDATE card_billed_txns SET description_overwrite='legacy edit'")
    store.conn.commit()
    before = billed_rows(store)
    with pytest.raises(ValueError, match="ambiguous"):
        persist_ctbc(payload(billed=[statement(ref=ref, merchantChiName="SHOP TAIPEI CITY TW")
                                     for ref in ("synthetic-a", "synthetic-b")]), store)
    assert billed_rows(store) == before


@pytest.mark.parametrize("ref", [None, "", "   "])
def test_blank_native_reference_is_missing_not_a_shared_purchase_id(store, ref):
    persist_ctbc(payload(billed=[statement(ref=ref, merchantChiName="本行扣繳", ntAmt=-500)]), store)
    assert len(billed_rows(store)) == 1
    assert billed_rows(store)[0]["native_identity"] is None


def test_known_reference_conflict_never_reuses_exact_pending_overlay(store):
    persist_ctbc(payload(pending=[unbilled(postingDt="00000000")]), store)
    store.conn.execute("UPDATE card_pending_txns SET description_overwrite='private edit'")
    store.conn.commit()
    with pytest.raises(ValueError, match="ambiguous|conflict"):
        persist_ctbc(payload(pending=[unbilled(ref="synthetic-b", postingDt="00000000")]), store)
    assert store.conn.execute("SELECT description_overwrite FROM card_pending_txns").fetchone()[0] == "private edit"
