"""Offline synthetic regressions through the production bill-write barrier."""
import copy
from datetime import date

import pytest

from backend.core.persist import persist_collected
from backend.core.store import BankStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    value = BankStore("ctbc", user_id=1, source_account_id=101)
    yield value
    value.close()


def payload(pending=(), billed=()):
    return {"card_api_dump": {
        "/twrbc-card/qu006/011": {"allItems": list(pending)},
        "/twrbc-card/qu002/010": {"billData": {"TWD": {"2026/09": {"bills": list(billed)}}}},
    }}


def pending(**changes):
    return {"purchaseDt": "20260910", "postingDt": "00000000", "cardNoSuffixFour": "1234_0",
            "description": "SHOP CITY", "purchaseAmt": 100, "origCurDesc": "TWD",
            "origCurAmt": 100, "txCode": "40", "acwRefNbr": "synthetic-ref", **changes}


def statement(**changes):
    return {"purchaseDt": "091026", "postingDt": "091226", "cardNo": "1234_0",
            "merchantChiName": "SHOP", "ntAmt": 100, "occCurCode": "TWD",
            "foreignAmt": "100", "txCode": "40", "acwRefNbr": "synthetic-ref", **changes}


def persist(store, **kwargs):
    return persist_collected("ctbc", payload(**kwargs), store)


def rows(store, table="card_billed_txns"):
    return [dict(row) for row in store.conn.execute(f"SELECT * FROM {table} ORDER BY id")]


def edit(store, splits=None):
    store.conn.execute("UPDATE card_pending_txns SET description_overwrite='KEEP', splits_overwrite=?", (splits,))
    store.commit()


@pytest.mark.parametrize("mode", ["promotion", "reestimate", "statement"])
def test_wrapper_native_fx_settlement_preserves_edit_and_rescales_splits(store, mode):
    original = pending(origCurDesc="USD", origCurAmt=3)
    persist(store, pending=[original])
    edit(store, '[{"amount":40},{"amount":60}]')
    if mode == "statement":
        persist(store, billed=[statement(ntAmt=150, occCurCode="USD", foreignAmt="3")])
    else:
        persist(store, pending=[{**original, "purchaseAmt": 150, "description": "SETTLED",
                                 "postingDt": "20260912" if mode == "promotion" else "00000000"}])
    actual = rows(store, "card_pending_txns" if mode == "reestimate" else "card_billed_txns")
    assert len(actual) == 1
    assert actual[0]["amount"] == 150
    assert actual[0]["description_overwrite"] == "KEEP"
    assert actual[0]["splits_overwrite"] == '[{"amount":60},{"amount":90}]'
    expected_cursor = {"****1234": date(2026, 9, 12)} if mode == "statement" else {}
    assert store.latest_card_transaction_dates() == expected_cursor


@pytest.mark.parametrize("count", [1, 2])
def test_native_null_postdate_enriches_same_occurrences_without_duplicate(store, count):
    native = '["ctbc",1,101,"1234_0","synthetic-ref"]'
    txn = {"card_no": "****1234", "date": "2026-09-10", "amount": 100, "desc": "SHOP",
           "currency": "TWD", "txn_type": "spending", "booking_status": "booked",
           "post_date": None, "native_identity": native}
    store.refresh_card_pending("unbilled", [txn] * count, fetch_ok=True)
    original_ids = [row["id"] for row in rows(store)]
    if count == 1:
        store.conn.execute("UPDATE card_billed_txns SET description_overwrite='KEEP'")
        store.commit()
    dated = {**txn, "post_date": "2026-09-12", "bill_date": "2026-09-18"}
    assert store.upsert_card_billed([dated] * count) == 0
    actual = rows(store)
    assert [row["id"] for row in actual] == original_ids
    assert [row["post_date"] for row in actual] == ["2026-09-12"] * count
    if count == 1:
        assert actual[0]["description_overwrite"] == "KEEP"
    assert store.upsert_card_billed([dated] * count) == 0
    assert store.upsert_card_billed([{**dated, "post_date": "2026-10-12"}]) == 1
    assert len(rows(store)) == count + 1


@pytest.mark.parametrize("target", ["billed", "pending"])
def test_missing_reference_cannot_reuse_conflicting_original_currency(store, target):
    original = pending(origCurDesc="USD", origCurAmt=3)
    if target == "billed":
        persist(store, billed=[statement(merchantChiName="SHOP CITY", occCurCode="USD", foreignAmt="3")])
        persist(store, billed=[statement(acwRefNbr=None, merchantChiName="SHOP CITY", occCurCode="EUR", foreignAmt="3")])
        assert [row["consume_currency"] for row in rows(store)] == ["USD", "EUR"]
    else:
        persist(store, pending=[original])
        edit(store)
        persist(store, pending=[{**original, "acwRefNbr": None, "origCurDesc": "EUR"}])
        actual = rows(store, "card_pending_txns")
        assert actual[0]["consume_currency"] == "EUR"
        assert actual[0]["description_overwrite"] is None


def test_foreign_native_pending_refresh_fails_closed_after_reopen(store):
    persist(store, pending=[pending()])
    edit(store)
    before = rows(store, "card_pending_txns")
    other = BankStore("ctbc", user_id=1, source_account_id=102)
    try:
        with pytest.raises(ValueError, match="source|credential"):
            persist(other)
        assert rows(other, "card_pending_txns") == before
        assert rows(other) == []
    finally:
        other.close()


@pytest.mark.parametrize("case", ["blank-card", "blank-card-legacy", "infer-card", "legacy-key"])
def test_all_weak_billed_fallbacks_veto_known_currency_conflicts(store, case):
    txn = {"card_no": None if case.startswith("blank-card") else "****1234", "date": "2026-09-10",
           "post_date": None if case == "infer-card" else "2026-09-12", "desc": "SHOP",
           "amount": 100, "currency": "TWD", "consume_currency": "USD", "consume_amount": 3,
           "native_identity": None if case in ("legacy-key", "blank-card-legacy") else "synthetic-native"}
    if txn["native_identity"]:
        txn["txn_type"] = "spending"
    if case == "blank-card":
        # Model a previously admitted native row; new incomplete native input must
        # never regain admission merely to seed this historical fallback regression.
        store.conn.execute(
            "INSERT INTO card_billed_txns (user_id, card_no, consume_date, post_date, "
            "description, amount, currency, consume_currency, consume_amount, "
            "native_identity, txn_type, dedup_key, first_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (store.user_id, txn["card_no"], txn["date"], txn["post_date"], txn["desc"],
             txn["amount"], txn["currency"], txn["consume_currency"], txn["consume_amount"],
             txn["native_identity"], txn["txn_type"], "historical-blank-native", "2026-09-10"),
        )
    else:
        store.upsert_card_billed([txn])
    store.commit()
    other = {**txn, "consume_currency": "EUR", "native_identity": None,
             "post_date": "2026-09-12", "card_no": None if case == "infer-card" else "****1234"}
    before = rows(store)
    if case == "blank-card":
        changes = store.conn.total_changes
        with pytest.raises(ValueError, match="incomplete native card occurrence"):
            store.upsert_card_billed([other])
        assert store.conn.total_changes == changes
        assert rows(store) == before
        observer = BankStore("ctbc", user_id=1, source_account_id=101)
        try:
            assert rows(observer) == before
        finally:
            observer.close()
    elif case == "legacy-key":
        with pytest.raises(ValueError, match="conflict"):
            store.upsert_card_billed([other])
        assert rows(store) == before
    else:
        assert store.upsert_card_billed([other]) == 1
        actual = rows(store)
        assert actual[0] == before[0]
        assert actual[1]["consume_currency"] == "EUR"
        assert actual[1]["card_no"] == other["card_no"]


@pytest.mark.parametrize("change", [{"txn_type": "payment"}, {"amount": -150},
                                    {"post_date": "2026-09-13"}, {"consume_amount": 4}])
def test_native_unknown_reestimate_conflicts_fail_closed_without_losing_edit(store, change):
    txn = {"card_no": "****1234", "date": "2026-09-10", "post_date": "2026-09-12",
           "amount": 100, "desc": "SHOP", "currency": "TWD", "consume_currency": "USD",
           "consume_amount": 3, "native_identity": "synthetic-native", "txn_type": "spending"}
    store.refresh_card_pending("unbilled", [txn], fetch_ok=True)
    edit(store)
    before = rows(store, "card_pending_txns")
    with pytest.raises(ValueError, match="conflict|ambiguous"):
        store.refresh_card_pending("unbilled", [{**txn, **change}], fetch_ok=True)
    assert rows(store, "card_pending_txns") == before
    assert rows(store) == []


def test_edited_native_null_postdate_cannot_guess_between_distinct_installments(store):
    txn = {"card_no": "****1234", "date": "2026-09-10", "post_date": None,
           "amount": 100, "desc": "SHOP", "currency": "TWD", "txn_type": "spending",
           "native_identity": "synthetic-native", "booking_status": "booked"}
    store.refresh_card_pending("unbilled", [txn], fetch_ok=True)
    store.conn.execute("UPDATE card_billed_txns SET description_overwrite='KEEP'")
    store.commit()
    before = rows(store)
    with pytest.raises(ValueError, match="ambiguous"):
        store.refresh_card_pending("unbilled", [{**txn, "post_date": day} for day in
                                              ("2026-09-12", "2026-10-12")], fetch_ok=True)
    assert rows(store) == before


@pytest.mark.parametrize("settlement", [{"occCurCode": "EUR", "foreignAmt": "3"},
                                        {"occCurCode": "USD", "foreignAmt": "4"},
                                        {"occCurCode": "TWD", "foreignAmt": "100", "ntAmt": 150}])
def test_wrapper_statement_disappearance_cannot_erase_conflicting_native_donor(store, settlement):
    persist(store, pending=[pending(origCurDesc="USD", origCurAmt=3)])
    edit(store)
    before = rows(store, "card_pending_txns")
    ledgers = {name: copy.deepcopy(getattr(store, name)) for name in LEDGERS}
    with pytest.raises(ValueError, match="conflict|ambiguous"):
        persist(store, billed=[statement(**settlement)])
    assert rows(store, "card_pending_txns") == before
    assert rows(store) == []
    assert {name: getattr(store, name) for name in LEDGERS} == ledgers


def test_native_undated_replay_after_enrichment_does_not_duplicate_or_erase_postdate(store):
    txn = {"card_no": "****1234", "date": "2026-09-10", "post_date": None,
           "amount": 100, "desc": "SHOP", "currency": "TWD", "txn_type": "spending",
           "native_identity": "synthetic-native", "booking_status": "booked"}
    store.refresh_card_pending("unbilled", [txn], fetch_ok=True)
    store.upsert_card_billed([{**txn, "post_date": "2026-09-12", "bill_date": "2026-09-18"}])
    store.commit()
    before = rows(store)
    assert store.refresh_card_pending("unbilled", [txn], fetch_ok=True) == 0
    assert rows(store) == before


@pytest.mark.parametrize("old_count,new_count", [(1, 2), (2, 1), (2, 2)])
def test_wrapper_native_fx_reestimate_cannot_guess_edited_repeated_occurrences(store, old_count, new_count):
    txn = pending(origCurDesc="USD", origCurAmt=3)
    persist(store, pending=[txn] * old_count)
    edit(store)
    before = rows(store, "card_pending_txns")
    with pytest.raises(ValueError, match="ambiguous"):
        persist(store, pending=[{**txn, "purchaseAmt": 150, "description": "RENAMED"}] * new_count)
    assert rows(store, "card_pending_txns") == before
    assert rows(store) == []


def test_card_batch_commit_false_is_rollbackable_and_retryable_through_barrier(store):
    from backend.core.card_bills import CardBillWriteBarrier

    txn = {"card_no": "****1234", "date": "2026-09-10", "post_date": "2026-09-12",
           "amount": 100, "desc": "SHOP", "currency": "TWD", "txn_type": "spending",
           "native_identity": "synthetic-native"}
    barrier = CardBillWriteBarrier(store)
    assert barrier.persist_card_batch([txn], [], scope="unbilled", fetch_ok=True, commit=False) == (1, 0)
    observer = BankStore("ctbc", user_id=1, source_account_id=101)
    try:
        assert rows(observer) == []
        store.rollback()
        assert rows(store) == []
        assert barrier.persist_card_batch([txn], [], scope="unbilled", fetch_ok=True) == (1, 0)
        assert len(rows(observer)) == 1
    finally:
        observer.close()


def test_real_wrapper_card_batch_does_not_bypass_bill_semantic_barrier(store):
    store.upsert_cards([{"number": "****1234", "bill_due_amount": 17,
                         "statement_close_date": "2026-09-18", "payment_due_date": "2026-10-05"}])
    data = payload(pending=[pending()])
    data["summary"] = {"creditCardSummary": {"quota": 200, "availBal": 800,
                                           "unpaidStmt": 999, "pmtExpDt": "2026/10/10"}}
    data["card_api_dump"]["/twrbc-card/qu002/010"]["cardDataList"] = [
        {"cardNoSuffixFour": "1234_0", "cardName": "SYNTHETIC CARD"}]
    persist_collected("ctbc", data, store)
    card = dict(store.conn.execute("SELECT * FROM cards WHERE card_no='****1234'").fetchone())
    assert card["name"] == "SYNTHETIC CARD"
    assert card["used_credit"] == 200
    assert card["bill_due_amount"] == 17
    assert card["statement_close_date"] == "2026-09-18"
    assert card["payment_due_date"] == "2026-10-05"


LEDGERS = ("_new_billed_ids", "_current_billed_ids", "_adopted_pending_raw_keys",
           "_unsafe_pending_overlay_identities")


def test_wrapper_legacy_rename_cannot_discard_edit(store):
    persist(store, pending=[pending(acwRefNbr=None)])
    edit(store)
    before = rows(store, "card_pending_txns")
    with pytest.raises(ValueError, match="ambiguous"):
        persist(store, billed=[statement()])
    assert rows(store) == []
    assert rows(store, "card_pending_txns") == before


def test_wrapper_card_batch_restores_all_ledgers_and_sql_on_failure(store):
    persist(store, pending=[pending()])
    edit(store, "bad")
    before = {name: copy.deepcopy(getattr(store, name)) for name in LEDGERS}
    pending_before = rows(store, "card_pending_txns")
    with pytest.raises(ValueError, match="split"):
        persist(store, billed=[statement()])
    assert {name: getattr(store, name) for name in LEDGERS} == before
    assert rows(store) == []
    assert rows(store, "card_pending_txns") == pending_before
    edit(store, '[{"amount":40},{"amount":60}]')
    persist(store, billed=[statement()])
    assert rows(store)[0]["description_overwrite"] == "KEEP"
    assert rows(store, "card_pending_txns") == []
