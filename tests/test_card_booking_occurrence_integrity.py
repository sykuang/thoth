"""Synthetic end-to-end occurrence contracts through the actual CTBC barrier."""
import copy
import json
from collections import Counter

import pytest

from backend.core.store import BankStore
from tests.test_card_booking_core_regressions import (
    LEDGERS, pending, persist, rows, statement, store as _store_fixture,
)

store = _store_fixture


FIELDS = ("category", "subcategory", "description_overwrite", "tags_overwrite",
          "auto_excluded", "splits_overwrite")
OVERLAY = ("購物", "訂閱", "KEEP", '["synthetic"]', 1,
           '[{"amount":40,"category":"餐飲","note":"one","auto_excluded":false},'
           '{"amount":60,"category":"交通","note":"two","auto_excluded":true}]')


def annotate(store, table="card_pending_txns"):
    store.conn.execute(f"UPDATE {table} SET " + ",".join(f"{field}=?" for field in FIELDS), OVERLAY)
    store.commit()


def seed_ledgers(store):
    store._new_billed_ids = [91]
    store._current_billed_ids = [92]
    store._adopted_pending_raw_keys = {("seed",): {"current": Counter({("raw",): 2})}}
    store._unsafe_pending_overlay_identities = {("seed-unsafe",)}
    return {name: copy.deepcopy(getattr(store, name)) for name in LEDGERS}


def assert_durable(store, billed, pending_rows):
    observer = BankStore("ctbc", user_id=1, source_account_id=101)
    try:
        assert rows(observer) == billed
        assert rows(observer, "card_pending_txns") == pending_rows
    finally:
        observer.close()


@pytest.mark.parametrize("old_ref,new_ref", [(None, "synthetic-ref"), ("synthetic-ref", None), (None, None)])
@pytest.mark.parametrize("fx", [False, True])
def test_wrapper_unknown_missing_reference_rename_fails_closed(store, old_ref, new_ref, fx):
    original = pending(acwRefNbr=old_ref, **({"origCurDesc": "USD", "origCurAmt": 3} if fx else {}))
    persist(store, pending=[original])
    annotate(store)
    before = rows(store, "card_pending_txns")
    ledgers = seed_ledgers(store)
    if fx and old_ref is None and new_ref is None:
        persist(store, pending=[{**original, "description": "RENAMED"}])
        assert tuple(rows(store, "card_pending_txns")[0][field] for field in FIELDS) == OVERLAY
        return  # Existing exact original-FX legacy fallback is admissible.
    with pytest.raises(ValueError, match="ambiguous"):
        persist(store, pending=[{**original, "acwRefNbr": new_ref, "description": "RENAMED"}])
    assert rows(store, "card_pending_txns") == before
    assert rows(store) == []
    assert {name: getattr(store, name) for name in LEDGERS} == ledgers
    assert_durable(store, [], before)
    # A repaired exact snapshot can be retried without leaking a failed mapping.
    persist(store, pending=[original])
    assert tuple(rows(store, "card_pending_txns")[0][field] for field in FIELDS) == OVERLAY


@pytest.mark.parametrize("change", [
    {"acwRefNbr": "different-known-ref"}, {"origCurDesc": "EUR", "origCurAmt": 3},
    {"purchaseDt": "20260911"}, {"cardNoSuffixFour": "5678_0"},
    {"purchaseAmt": -100, "txCode": "41"},
])
def test_wrapper_unrelated_purchase_is_not_a_legacy_ambiguity(store, change):
    persist(store, pending=[pending(acwRefNbr="synthetic-ref" if "acwRefNbr" in change else None)])
    annotate(store)
    persist(store, pending=[pending(description="NEW PURCHASE", **change)])
    actual = rows(store, "card_pending_txns")
    assert len(actual) == 1
    assert actual[0]["description_overwrite"] is None


@pytest.mark.parametrize("amount", [100, 150])
@pytest.mark.parametrize("rename", [False, True])
def test_wrapper_legacy_fx_disappearance_preserves_sql_and_all_ledgers(store, amount, rename):
    persist(store, pending=[pending(acwRefNbr=None, origCurDesc="USD", origCurAmt=3)])
    annotate(store)
    before = rows(store, "card_pending_txns")
    ledgers = seed_ledgers(store)
    if not rename and amount == 100:
        persist(store, billed=[statement(ntAmt=amount, occCurCode="USD", foreignAmt="3",
                                        merchantChiName="SHOP CITY")])
        assert tuple(rows(store)[0][field] for field in FIELDS) == OVERLAY
        return  # Exact raw evidence plus non-conflicting money remains admissible.
    with pytest.raises(ValueError, match="ambiguous"):
        persist(store, billed=[statement(ntAmt=amount, occCurCode="USD", foreignAmt="3",
                                        merchantChiName="SHOP" if rename else "SHOP CITY")])
    assert rows(store) == []
    assert rows(store, "card_pending_txns") == before
    assert {name: getattr(store, name) for name in LEDGERS} == ledgers
    assert_durable(store, [], before)


@pytest.mark.parametrize("snapshot_date", ["20260912", "00000000"])
def test_wrapper_already_booked_fx_settles_in_place_and_old_snapshot_cannot_regress(store, snapshot_date):
    original = pending(postingDt="20260912", origCurDesc="USD", origCurAmt=3)
    persist(store, pending=[original])
    assert store.latest_card_transaction_dates() == {}
    annotate(store, "card_billed_txns")
    before = rows(store)[0]
    persist(store, billed=[statement(ntAmt=150, occCurCode="USD", foreignAmt="3")])
    actual = rows(store)
    assert len(actual) == 1
    settled = actual[0]
    assert settled["id"] == before["id"]
    assert settled["amount"] == 150
    assert settled["post_date"] == "2026-09-12"
    assert settled["bill_date"] == "2026-09-01"
    assert tuple(settled[field] for field in FIELDS[:-1]) == OVERLAY[:-1]
    splits = json.loads(OVERLAY[-1])
    splits[0]["amount"], splits[1]["amount"] = 60, 90
    assert json.loads(settled["splits_overwrite"]) == splits
    persist(store, billed=[statement(ntAmt=150, occCurCode="USD", foreignAmt="3")])
    assert rows(store) == actual
    ledgers = seed_ledgers(store)
    try:
        persist(store, pending=[{**original, "postingDt": snapshot_date}])
    except ValueError:
        assert {name: getattr(store, name) for name in LEDGERS} == ledgers
    assert rows(store) == actual
    assert rows(store, "card_pending_txns") == []
    assert_durable(store, actual, [])


@pytest.mark.parametrize("count", [1, 2])
def test_wrapper_fx_settlement_ambiguity_or_invalid_splits_rolls_back_and_retries(store, count):
    original = pending(postingDt="20260912", origCurDesc="USD", origCurAmt=3)
    persist(store, pending=[original] * count)
    annotate(store, "card_billed_txns")
    if count == 1:
        store.conn.execute("UPDATE card_billed_txns SET splits_overwrite='bad'")
        store.commit()
    before = rows(store)
    ledgers = seed_ledgers(store)
    with pytest.raises(ValueError, match="ambiguous|split"):
        persist(store, billed=[statement(ntAmt=150, occCurCode="USD", foreignAmt="3")] * count)
    assert rows(store) == before
    assert {name: getattr(store, name) for name in LEDGERS} == ledgers
    assert_durable(store, before, [])
    if count == 1:
        annotate(store, "card_billed_txns")
        persist(store, billed=[statement(ntAmt=150, occCurCode="USD", foreignAmt="3")])
        assert len(rows(store)) == 1 and rows(store)[0]["amount"] == 150


@pytest.mark.parametrize("change", [
    {"postingDt": "101226"}, {"cardNo": "5678_0"}, {"acwRefNbr": "different-ref"},
    {"ntAmt": -150, "txCode": "41"}, {"foreignAmt": "4"}, {"occCurCode": "EUR"},
])
def test_wrapper_fx_billed_unrelated_occurrence_cannot_steal_edits(store, change):
    persist(store, pending=[pending(postingDt="20260912", origCurDesc="USD", origCurAmt=3)])
    annotate(store, "card_billed_txns")
    before = rows(store)
    try:
        persist(store, billed=[statement(**{"ntAmt": 150, "occCurCode": "USD", "foreignAmt": "3", **change})])
    except ValueError:
        assert rows(store) == before
    else:
        assert len(rows(store)) == 2
        assert rows(store)[0] == before[0]
        assert rows(store)[1]["description_overwrite"] is None


@pytest.mark.parametrize("old_ref,new_ref", [(None, "synthetic-ref"), ("synthetic-ref", None), (None, None)])
def test_wrapper_legacy_already_booked_fx_rename_cannot_erase_or_duplicate_donor(store, old_ref, new_ref):
    persist(store, pending=[pending(acwRefNbr=old_ref, postingDt="20260912", origCurDesc="USD", origCurAmt=3)])
    annotate(store, "card_billed_txns")
    before = rows(store)
    ledgers = seed_ledgers(store)
    with pytest.raises(ValueError, match="ambiguous"):
        persist(store, billed=[statement(acwRefNbr=new_ref, ntAmt=150, occCurCode="USD", foreignAmt="3")])
    assert rows(store) == before
    assert {name: getattr(store, name) for name in LEDGERS} == ledgers
    assert_durable(store, before, [])


@pytest.mark.parametrize("original_amount", [0, None])
def test_zero_or_absent_original_fx_is_not_settlement_proof(store, original_amount):
    original = pending(postingDt="20260912", origCurDesc="USD", origCurAmt=original_amount)
    if original_amount == 0:
        # Tightened native admission rejects zero original FX before the seed exists.
        ledgers = seed_ledgers(store)
        changes = store.conn.total_changes
        with pytest.raises(ValueError, match="incomplete native card occurrence"):
            persist(store, pending=[original])
        assert store.conn.total_changes == changes
        assert rows(store) == rows(store, "card_pending_txns") == []
        assert {name: getattr(store, name) for name in LEDGERS} == ledgers
        assert_durable(store, [], [])
        return
    persist(store, pending=[original])
    annotate(store, "card_billed_txns")
    before = rows(store)
    try:
        persist(store, billed=[statement(ntAmt=150, occCurCode="USD",
                                        foreignAmt=str(original_amount) if original_amount is not None else None)])
    except ValueError:
        assert rows(store) == before
    else:
        assert len(rows(store)) == 2
        assert rows(store)[0] == before[0]


@pytest.mark.parametrize("mode", ["unknown", "promotion", "statement"])
def test_legacy_fx_original_money_never_authorizes_reversal_overlay_transfer(store, mode):
    persist(store, pending=[pending(acwRefNbr=None, origCurDesc="USD", origCurAmt=3)])
    annotate(store)
    before = rows(store, "card_pending_txns")
    try:
        if mode == "statement":
            persist(store, billed=[statement(acwRefNbr=None, ntAmt=-150, txCode="41",
                                            occCurCode="USD", foreignAmt="3")])
        else:
            persist(store, pending=[pending(acwRefNbr=None, purchaseAmt=-150, txCode="41",
                                           description="REVERSAL", origCurDesc="USD", origCurAmt=3,
                                           postingDt="20260912" if mode == "promotion" else "00000000")])
    except ValueError:
        assert rows(store) == []
        assert rows(store, "card_pending_txns") == before
    else:
        actual = rows(store, "card_pending_txns" if mode == "unknown" else "card_billed_txns")
        assert len(actual) == 1
        assert actual[0]["description_overwrite"] is None


@pytest.mark.parametrize("change", [{"post_date": "2026-09-13"}, {"txn_type": "refund"}])
def test_barrier_legacy_exact_metadata_cannot_cross_known_occurrence_conflict(store, change):
    from backend.core.card_bills import CardBillWriteBarrier

    original = {"card_no": "****1234", "date": "2026-09-10", "post_date": "2026-09-12",
                "amount": 100, "desc": "SHOP", "currency": "TWD", "txn_type": "spending",
                "native_identity": None, "booking_status": None}
    barrier = CardBillWriteBarrier(store)
    barrier.persist_card_batch([], [original], scope="unbilled", fetch_ok=True)
    annotate(store)
    before = rows(store, "card_pending_txns")
    try:
        barrier.persist_card_batch([], [{**original, **change}], scope="unbilled", fetch_ok=True)
    except ValueError:
        assert rows(store, "card_pending_txns") == before
    else:
        assert rows(store, "card_pending_txns")[0]["description_overwrite"] is None


def test_barrier_already_booked_fx_commit_false_close_reopen_and_retry(store):
    from backend.core.card_bills import CardBillWriteBarrier

    persist(store, pending=[pending(postingDt="20260912", origCurDesc="USD", origCurAmt=3)])
    annotate(store, "card_billed_txns")
    before = rows(store)
    txn = {"card_no": "****1234", "date": "2026-09-10", "post_date": "2026-09-12",
           "bill_date": "2026-09-01", "amount": 150, "desc": "SHOP", "currency": "TWD",
           "consume_currency": "USD", "consume_amount": 3, "txn_type": "spending",
           "native_identity": before[0]["native_identity"]}
    assert CardBillWriteBarrier(store).persist_card_batch([txn], [], scope="unbilled", fetch_ok=True,
                                                        commit=False) == (0, 0)
    assert rows(store)[0]["amount"] == 150
    assert_durable(store, before, [])
    store.close()
    reopened = BankStore("ctbc", user_id=1, source_account_id=101)
    try:
        assert rows(reopened) == before
        persist(reopened, billed=[statement(ntAmt=150, occCurCode="USD", foreignAmt="3")])
        actual = rows(reopened)
        assert len(actual) == 1 and actual[0]["id"] == before[0]["id"]
        assert actual[0]["amount"] == 150 and actual[0]["description_overwrite"] == "KEEP"
        assert_durable(reopened, actual, [])
    finally:
        reopened.close()


def test_wrapper_conflicting_native_exact_occurrence_shrink_fails_closed(store):
    persist(store, pending=[pending()] * 2)
    annotate(store)
    store.conn.execute("UPDATE card_pending_txns SET description_overwrite='OTHER' WHERE id=(SELECT MAX(id) FROM card_pending_txns)")
    store.commit()
    before = rows(store, "card_pending_txns")
    ledgers = seed_ledgers(store)
    with pytest.raises(ValueError, match="ambiguous"):
        persist(store, pending=[pending()])
    assert rows(store, "card_pending_txns") == before
    assert {name: getattr(store, name) for name in LEDGERS} == ledgers
    assert_durable(store, [], before)


@pytest.mark.parametrize("mode", ["unknown", "promotion", "statement"])
def test_wrapper_distinct_known_native_purchase_does_not_block_new_purchase(store, mode):
    persist(store, pending=[pending()])
    annotate(store)
    if mode == "statement":
        persist(store, billed=[statement(acwRefNbr="new-known-ref")])
    else:
        persist(store, pending=[pending(acwRefNbr="new-known-ref", description="NEW PURCHASE",
                                       postingDt="20260912" if mode == "promotion" else "00000000")])
    actual = rows(store, "card_pending_txns" if mode == "unknown" else "card_billed_txns")
    assert len(actual) == 1
    assert actual[0]["description_overwrite"] is None


@pytest.mark.parametrize("repair", ["stale_rows", "one_off_plan"])
def test_legacy_repair_boundaries_do_not_collapse_native_occurrences(store, repair):
    persist(store, billed=[statement(acwRefNbr="native-a"), statement(acwRefNbr="native-b")])
    annotate(store, "card_billed_txns")
    store.conn.execute("UPDATE card_billed_txns SET first_seen='2026-09-20' WHERE id=(SELECT MAX(id) FROM card_billed_txns)")
    store.commit()
    before = rows(store)
    if repair == "stale_rows":
        assert store.dedup_billed_stale_rows() == 0
    else:
        from backend.core.twd_dedup_repair import USER_COLUMNS, plan_cards
        assert plan_cards(store.card_dedup_rows(USER_COLUMNS)) == []
    assert rows(store) == before


def test_wrapper_billed_native_source_owner_is_not_reassigned(store):
    persist(store, pending=[pending(postingDt="20260912", origCurDesc="USD", origCurAmt=3)])
    annotate(store, "card_billed_txns")
    before = rows(store)
    other = BankStore("ctbc", user_id=1, source_account_id=102)
    try:
        persist(other, billed=[statement(ntAmt=150, occCurCode="USD", foreignAmt="3")])
        actual = rows(other)
        assert len(actual) == 2
        assert actual[0] == before[0]
        assert actual[1]["native_identity"] != before[0]["native_identity"]
        assert actual[1]["description_overwrite"] is None
    finally:
        other.close()


def native_row(**changes):
    return {"card_no": "****1234", "date": "2026-09-10", "post_date": None,
            "amount": 100, "desc": "SHOP CITY", "currency": "TWD", "txn_type": "spending",
            "consume_currency": "USD", "consume_amount": 3, "booking_status": "booked",
            "native_identity": '["ctbc",1,101,"1234_0","synthetic-ref"]', **changes}


def write_native(store, entry, txns):
    from backend.core.card_bills import CardBillWriteBarrier

    barrier = CardBillWriteBarrier(store)
    if entry == "upsert":
        return barrier.upsert_card_billed(txns)
    if entry in ("refresh", "refresh_unknown", "legacy_refresh"):
        return barrier.refresh_card_pending("unbilled", txns,
                                            fetch_ok=None if entry == "legacy_refresh" else True)
    return barrier.persist_card_batch(txns if entry == "batch_billed" else [],
                                      txns if entry == "batch_pending" else [],
                                      scope="unbilled", fetch_ok=True)


@pytest.mark.parametrize("entry", ["upsert", "refresh", "legacy_refresh", "batch_billed", "batch_pending"])
@pytest.mark.parametrize("booking_status", [None, "booked"])
@pytest.mark.parametrize("change", [
    {"card_no": None}, {"card_no": ""}, {"card_no": " \t"},
    {"card_no": None, "consume_currency": "TWD", "consume_amount": 100},
    {"card_no": "", "consume_currency": "TWD", "consume_amount": 100},
    {"card_no": " \t", "consume_currency": "TWD", "consume_amount": 100},
    {"date": None}, {"date": ""}, {"date": " \t"},
    {"consume_amount": 0}, {"currency": "USD", "consume_currency": None, "consume_amount": None},
    {"consume_currency": "TWD", "consume_amount": None, "amount": None},
    {"amount": None},
    {"date": None, "consume_date": "2026-09-10"},
    {"date": "", "consume_date": "2026-09-10"},
    {"date": " \t", "consume_date": "2026-09-10"},
    {"consume_date": "2026-09-11"}, {"consume_date": None},
    {"date": "2026-02-30"}, {"date": "2026/09/10"}, {"date": 42},
    {"post_date": "2026-02-30"}, {"post_date": ""},
    {"card_no": 1234}, {"card_no": [1234]}, {"txn_type": None}, {"txn_type": ""},
])
def test_native_incomplete_first_publication_is_rejected_before_any_sql(store, entry, booking_status, change):
    # A valid leading row must not be inserted before the bad trailing row is checked.
    txns = [native_row(native_identity="other-valid-ref", booking_status=booking_status),
            native_row(booking_status=booking_status, **change)] * 2
    original = copy.deepcopy(txns)
    ledgers = seed_ledgers(store)
    changes = store.conn.total_changes
    with pytest.raises(ValueError, match="incomplete native card occurrence"):
        write_native(store, entry, txns)
    assert store.conn.total_changes == changes
    assert txns == original
    assert rows(store) == rows(store, "card_pending_txns") == []
    assert {name: getattr(store, name) for name in LEDGERS} == ledgers
    assert_durable(store, [], [])
    store.close()
    reopened = BankStore("ctbc", user_id=1, source_account_id=101)
    try:
        assert rows(reopened) == rows(reopened, "card_pending_txns") == []
    finally:
        reopened.close()


def seed_historical_incomplete_native(store, table, change):
    from backend.core.store import _dedup_key

    old = native_row(**change)
    data = {"user_id": 1, "card_no": old["card_no"], "consume_date": old["date"],
            "post_date": old["post_date"], "amount": old["amount"], "description": old["desc"],
            "currency": old["currency"], "consume_currency": old["consume_currency"],
            "consume_amount": old["consume_amount"], "native_identity": old["native_identity"],
            "txn_type": old["txn_type"], **dict(zip(FIELDS, OVERLAY, strict=True))}
    if table == "card_billed_txns":
        data.update(first_seen="2026-09-11", dedup_key=_dedup_key(
            "native", old["native_identity"], old["card_no"], old["date"], old["post_date"], old["amount"],
        ))
    else:
        data.update(scope="unbilled", refreshed_at="2026-09-11")
    store.conn.execute(f"INSERT INTO {table} ({','.join(data)}) VALUES ({','.join('?' for _ in data)})",
                       tuple(data.values()))
    store.commit()


@pytest.mark.parametrize("table", ["card_billed_txns", "card_pending_txns"])
@pytest.mark.parametrize("entry", ["upsert", "refresh", "refresh_unknown", "legacy_refresh", "batch_billed", "batch_pending"])
@pytest.mark.parametrize("change", [
    {"card_no": None}, {"card_no": ""}, {"card_no": " \t"},
    {"date": None}, {"date": ""}, {"date": " \t"}, {"consume_amount": 0},
    {"currency": "USD", "consume_currency": None, "consume_amount": None},
    {"consume_currency": "TWD", "consume_amount": None, "amount": None}, {"amount": None},
    {"date": "2026-02-30"}, {"post_date": "2026-02-30"}, {"txn_type": None},
])
def test_historical_incomplete_native_cannot_authorize_second_publication(store, table, entry, change):
    # SQL models previously accepted native rows, including old amount-sensitive keys.
    # A now-complete incoming card/date/money must not bypass an incomplete donor.
    seed_historical_incomplete_native(store, table, change)
    before = rows(store), rows(store, "card_pending_txns")
    ledgers = seed_ledgers(store)
    changes = store.conn.total_changes
    incoming = native_row(amount=150, desc="SHOP", post_date="2026-09-12", bill_date="2026-09-01",
                          booking_status=None if entry == "refresh_unknown" else "booked")
    with pytest.raises(ValueError, match="incomplete native card occurrence"):
        write_native(store, entry, [incoming])
    assert store.conn.total_changes == changes
    assert (rows(store), rows(store, "card_pending_txns")) == before
    assert {name: getattr(store, name) for name in LEDGERS} == ledgers
    assert_durable(store, *before)
    store.close()
    reopened = BankStore("ctbc", user_id=1, source_account_id=101)
    try:
        assert (rows(reopened), rows(reopened, "card_pending_txns")) == before
    finally:
        reopened.close()


@pytest.mark.parametrize("entry", ["refresh", "legacy_refresh", "batch_pending"])
def test_historical_incomplete_native_disappearance_does_not_erase_edits(store, entry):
    seed_historical_incomplete_native(store, "card_pending_txns", {"card_no": None})
    before = rows(store, "card_pending_txns")
    ledgers = seed_ledgers(store)
    changes = store.conn.total_changes
    with pytest.raises(ValueError, match="incomplete native card occurrence"):
        write_native(store, entry, [])
    assert store.conn.total_changes == changes
    assert rows(store) == [] and rows(store, "card_pending_txns") == before
    assert {name: getattr(store, name) for name in LEDGERS} == ledgers
    assert_durable(store, [], before)


@pytest.mark.parametrize("entry", ["upsert", "refresh", "legacy_refresh", "batch_billed", "batch_pending"])
@pytest.mark.parametrize("card,date", [(None, None), ("", ""), (" \t", " \t"), (None, "2026-09-10")])
def test_legacy_nonnative_incomplete_rows_remain_admissible(store, entry, card, date):
    txn = native_row(native_identity=None, card_no=card, date=date, booking_status=None)
    write_native(store, entry, [txn])
    table = "card_billed_txns" if entry in ("upsert", "batch_billed") else "card_pending_txns"
    actual = rows(store, table)
    assert len(actual) == 1
    assert actual[0]["card_no"] == card and actual[0]["consume_date"] == date
    assert actual[0]["native_identity"] is None
    store.commit()  # Direct upsert deliberately leaves publication to its caller.
    assert_durable(store, rows(store), rows(store, "card_pending_txns"))


@pytest.mark.parametrize("entry", ["upsert", "refresh", "legacy_refresh", "batch_billed", "batch_pending"])
@pytest.mark.parametrize("change", [
    {}, {"consume_currency": "TWD", "consume_amount": 100}, {"consume_currency": "EUR"},
    {"date": "2026-09-11"}, {"amount": -100, "txn_type": "refund"},
])
def test_complete_native_repeated_occurrences_replay_without_lost_edits(store, entry, change):
    txns = [native_row(**change)] * 2
    write_native(store, entry, txns)
    table = "card_pending_txns" if entry == "legacy_refresh" else "card_billed_txns"
    assert len(rows(store, table)) == 2
    annotate(store, table)
    before = rows(store, table)
    write_native(store, entry, txns)
    actual = rows(store, table)
    assert len(actual) == 2
    assert [tuple(row[field] for field in FIELDS) for row in actual] == [OVERLAY] * 2
    if table == "card_billed_txns":
        assert actual == before
    assert_durable(store, rows(store), rows(store, "card_pending_txns"))


def test_batch_rejects_incomplete_pending_before_publishing_valid_billed(store):
    from backend.core.card_bills import CardBillWriteBarrier

    ledgers = seed_ledgers(store)
    changes = store.conn.total_changes
    with pytest.raises(ValueError, match="incomplete native card occurrence"):
        CardBillWriteBarrier(store).persist_card_batch(
            [native_row()], [native_row(card_no=None, booking_status=None)],
            scope="unbilled", fetch_ok=True,
        )
    assert store.conn.total_changes == changes
    assert rows(store) == rows(store, "card_pending_txns") == []
    assert {name: getattr(store, name) for name in LEDGERS} == ledgers
    assert_durable(store, [], [])


@pytest.mark.parametrize("table", ["card_billed_txns", "card_pending_txns"])
@pytest.mark.parametrize("fx", [False, True])
@pytest.mark.parametrize("old_card,new_card", [
    (None, ""), ("", None), (None, " \t"), (" \t", None),
    (None, "****1234"), ("****1234", None), ("", "****1234"), ("****1234", ""),
    (" \t", "****1234"), ("****1234", " \t"),
])
def test_native_incomplete_card_representation_changes_never_publish_twice(store, table, fx, old_card, new_card):
    money = {} if fx else {"consume_currency": "TWD", "consume_amount": 100}
    seed_historical_incomplete_native(store, table, {"card_no": old_card, **money})
    before = rows(store), rows(store, "card_pending_txns")
    ledgers = seed_ledgers(store)
    changes = store.conn.total_changes
    with pytest.raises(ValueError, match="incomplete native card occurrence"):
        write_native(store, "batch_billed", [native_row(
            card_no=new_card, amount=150, post_date="2026-09-12", bill_date="2026-09-01", **money,
        )])
    assert store.conn.total_changes == changes
    assert (rows(store), rows(store, "card_pending_txns")) == before
    assert {name: getattr(store, name) for name in LEDGERS} == ledgers
    assert_durable(store, *before)


@pytest.mark.parametrize("raw_original", [None, "", "not-money", [], {}])
@pytest.mark.parametrize("old_currency", ["USD", ""])
@pytest.mark.parametrize("amount", [100, 150])
def test_native_fx_evidence_enrichment_never_publishes_second_billed_purchase(
    store, raw_original, old_currency, amount,
):
    persist(store, billed=[statement(ntAmt=100, occCurCode=old_currency, foreignAmt=raw_original)])
    annotate(store, "card_billed_txns")
    before = rows(store)
    ledgers = seed_ledgers(store)
    try:
        persist(store, billed=[statement(ntAmt=amount, occCurCode="USD", foreignAmt="3")])
    except ValueError:
        assert rows(store) == before
        assert {name: getattr(store, name) for name in LEDGERS} == ledgers
    else:
        actual = rows(store)
        assert len(actual) == 1 and actual[0]["id"] == before[0]["id"]
        assert tuple(actual[0][field] for field in FIELDS) == OVERLAY
    assert_durable(store, rows(store), [])


@pytest.mark.parametrize("mixed_sides", [False, True])
@pytest.mark.parametrize("change", [
    {"amount": float("nan")}, {"amount": float("inf")},
    {"amount": "9007199254740992"}, {"consume_amount": "3.1234567"},
    {"currency": "INVALID"}, {"consume_currency": "US"},
])
def test_native_invalid_money_preflight_cannot_mutate_leading_booked_row(store, mixed_sides, change):
    from backend.core.card_bills import CardBillWriteBarrier

    leading = native_row(native_identity='["ctbc",1,101,"1234_0","leading-valid"]')
    trailing = native_row(booking_status=None, **change)
    ledgers = seed_ledgers(store)
    changes = store.conn.total_changes
    with pytest.raises(ValueError):
        CardBillWriteBarrier(store).persist_card_batch(
            [leading] if mixed_sides else [], [trailing] if mixed_sides else [leading, trailing],
            scope="unbilled", fetch_ok=True,
        )
    assert store.conn.total_changes == changes
    assert rows(store) == rows(store, "card_pending_txns") == []
    assert {name: getattr(store, name) for name in LEDGERS} == ledgers
    assert_durable(store, [], [])


@pytest.mark.parametrize("raw_amount", [None, "", "not-money", [], {}])
def test_real_ctbc_missing_settlement_money_is_rejected_before_publication(store, raw_amount):
    ledgers = seed_ledgers(store)
    changes = store.conn.total_changes
    with pytest.raises(ValueError, match="incomplete native card occurrence"):
        persist(store, billed=[statement(ntAmt=raw_amount, occCurCode="USD", foreignAmt="3")])
    assert store.conn.total_changes == changes
    assert rows(store) == rows(store, "card_pending_txns") == []
    assert {name: getattr(store, name) for name in LEDGERS} == ledgers
    assert_durable(store, [], [])
    store.close()
    reopened = BankStore("ctbc", user_id=1, source_account_id=101)
    try:
        assert rows(reopened) == rows(reopened, "card_pending_txns") == []
        persist(reopened, billed=[statement(ntAmt=150, occCurCode="USD", foreignAmt="3")])
        assert len(rows(reopened)) == 1
        assert rows(reopened)[0]["amount"] == 150
        assert_durable(reopened, rows(reopened), [])
    finally:
        reopened.close()

