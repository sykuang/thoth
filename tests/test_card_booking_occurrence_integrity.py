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


def test_occurrence_source_snapshot_restores_actual_barrier_owner_without_shadow_state(store):
    from backend.core.card_bills import CardBillWriteBarrier
    from backend.core.persist._common import _snapshot_occurrence_ledgers, _restore_occurrence_ledgers

    store.upsert_card_billed([{"card_no": "****1234", "date": "2026-07-01", "desc": "SOURCE",
                              "amount": 100, "currency": "TWD", "post_date": "2026-07-03"}])
    barrier = CardBillWriteBarrier(store)
    before = _snapshot_occurrence_ledgers(barrier)
    assert before["_billed_source_rows"]
    store._billed_source_rows.clear()
    store._current_billed_ids.clear()
    _restore_occurrence_ledgers(barrier, before)
    assert _snapshot_occurrence_ledgers(store) == before
    assert vars(barrier) == {"_store": store}


@pytest.mark.parametrize("entry", ["batch", "promotion", "pending"])
@pytest.mark.parametrize("commit", [False, True])
def test_authentic_source_ledger_rolls_back_at_each_owned_late_insert_failure(store, entry, commit):
    from backend.core.card_bills import CardBillWriteBarrier
    from backend.core.error_codes import exception_error_id
    from backend.core.persist._common import _snapshot_occurrence_ledgers

    a = {"card_no": "****1234", "date": "2026-07-01", "desc": "A", "amount": 100,
         "currency": "TWD", "post_date": None, "txn_type": "spending",
         "native_identity": json.dumps(["ctbc", 1, 101, "1234_0", "a"])}
    store.refresh_card_pending("unbilled", [a], fetch_ok=None)
    store.refresh_card_pending("current", [{**a, "desc": "OTHER"}], fetch_ok=None)
    annotate(store)
    original = rows(store, "card_pending_txns")
    assert [tuple(row[f] for f in FIELDS) for row in original] == [OVERLAY] * 2
    source = {**a, "post_date": "2026-07-03", "bill_date": "2026-07-18"}
    incoming = {**a, "date": "2026-07-02", "desc": "INCOMING", "native_identity": None,
                "booking_status": None}
    barrier = CardBillWriteBarrier(store)
    if entry == "pending":
        barrier.upsert_card_billed([source])
        if commit:
            store.commit()
    if not commit:
        store.conn.execute("UPDATE card_pending_txns SET refreshed_at='caller-prefix' WHERE scope='current'")
    before_sql = (rows(store), rows(store, "card_pending_txns"))
    before = _snapshot_occurrence_ledgers(store)
    assert "_billed_source_rows" in before
    observed = []
    store.conn.execute("CREATE TEMP TRIGGER fail_actual_source_insert BEFORE INSERT ON card_pending_txns "
                       "BEGIN SELECT RAISE(ABORT, 'actual source late insert'); END")

    def observe(sql):
        if "INSERT INTO card_pending_txns" in sql:
            observed.append((_snapshot_occurrence_ledgers(store),
                             rows(store, "card_pending_txns")))

    store.conn.set_trace_callback(observe)
    with pytest.raises(Exception, match="actual source late insert") as failure:
        if entry == "batch":
            barrier.persist_card_batch([source], [incoming], scope="unbilled", fetch_ok=True, commit=commit)
        else:
            barrier.refresh_card_pending("unbilled", [{**source, "booking_status": "booked"}, incoming]
                                         if entry == "promotion" else [incoming], fetch_ok=True, commit=commit)
    store.conn.set_trace_callback(None)
    assert exception_error_id(failure.value) == "SYS-08-155-025"
    assert observed
    partial, pending_at_insert = observed[0]
    assert partial["_billed_source_rows"]
    assert any(marker[0] == "booked" for marker in partial["_unsafe_pending_overlay_identities"])
    assert not any(row["scope"] == "unbilled" for row in pending_at_insert)
    assert _snapshot_occurrence_ledgers(store) == before
    # Caller-owned prefix remains provisional; Store-owned failure rolls it back.
    assert (rows(store), rows(store, "card_pending_txns")) == (before_sql if not commit else
        (before_sql[0] if entry == "pending" else [], original))
    store.conn.execute("DROP TRIGGER fail_actual_source_insert")
    if not commit:
        assert store.conn.in_transaction
        store.rollback()


@pytest.mark.parametrize("first_scope", ["unbilled", "current"])
@pytest.mark.parametrize("first_entry", ["statement", "promotion"])
def test_missing_source_proof_does_not_poison_later_unique_other_scope_source(store, first_scope, first_entry):
    from backend.core.card_bills import CardBillWriteBarrier

    a = {"card_no": "****1234", "date": "2026-07-01", "desc": "NEW", "amount": 100,
         "currency": "TWD", "post_date": "2026-07-03", "bill_date": "2026-07-18",
         "txn_type": "spending", "native_identity": json.dumps(["ctbc", 1, 101, "1234_0", "a"])}
    store.upsert_card_billed([a])
    store.commit()
    original = rows(store)[0]
    second_scope = "current" if first_scope == "unbilled" else "unbilled"
    for scope in (first_scope, second_scope):
        store.refresh_card_pending(scope, [{**a, "desc": "OLD", "post_date": None}], fetch_ok=None)
    annotate(store)
    original_pending = rows(store, "card_pending_txns")
    assert [tuple(row[f] for f in FIELDS) for row in original_pending] == [OVERLAY] * 2
    remaining = next(row for row in original_pending if row["scope"] == second_scope)
    barrier = CardBillWriteBarrier(store)
    weak = {**a, "native_identity": None}
    if first_entry == "statement":
        barrier.persist_card_batch([weak], [], scope=first_scope, fetch_ok=True, commit=False)
    else:
        barrier.refresh_card_pending(first_scope, [{**weak, "booking_status": "booked"}],
                                     fetch_ok=True, commit=False)
    assert rows(store) == [original]
    assert rows(store, "card_pending_txns") == [remaining]
    assert ("booked", original["id"]) not in store._unsafe_pending_overlay_identities
    assert barrier.refresh_card_pending(second_scope, [{**a, "booking_status": "booked"}], fetch_ok=True) == 0
    actual = rows(store)
    assert len(actual) == 1 and actual[0]["id"] == original["id"]
    assert tuple(actual[0][f] for f in FIELDS) == OVERLAY
    assert rows(store, "card_pending_txns") == []


def test_sequential_promotion_remembers_two_actual_booked_sources(store):
    from backend.core.card_bills import CardBillWriteBarrier

    a = {"card_no": "****1234", "date": "2026-07-01", "desc": "A", "amount": 100,
         "currency": "TWD", "post_date": None, "txn_type": "spending",
         "native_identity": json.dumps(["ctbc", 1, 101, "1234_0", "a"])}
    for scope in ("unbilled", "current"):
        store.refresh_card_pending(scope, [a], fetch_ok=None, rules=[])
    annotate(store)
    original = rows(store, "card_pending_txns")
    original_current = next(row for row in original if row["scope"] == "current")
    assert [tuple(row[field] for field in FIELDS) for row in original] == [OVERLAY] * 2
    b = {**a, "booking_status": "booked", "post_date": "2026-07-03"}
    barrier = CardBillWriteBarrier(store)
    assert barrier.refresh_card_pending("unbilled", [b, b], fetch_ok=True, commit=False, rules=[]) == 0
    first = rows(store)
    assert len(first) == 2
    assert [tuple(row[field] for field in FIELDS) for row in first] == [(None, None, None, None, 0, None)] * 2
    assert rows(store, "card_pending_txns") == [original_current]
    assert barrier.refresh_card_pending("current", [b], fetch_ok=True, rules=[]) == 0
    assert rows(store) == first
    assert rows(store, "card_pending_txns") == []
    assert store.latest_card_transaction_dates() == {}
    assert_durable(store, first, [])


@pytest.mark.parametrize("kind", ["native", "fx", "local"])
@pytest.mark.parametrize("first_scope", ["unbilled", "current"])
@pytest.mark.parametrize("reverse_seed", [False, True])
@pytest.mark.parametrize("sibling_edits", ["blank", "same"])
@pytest.mark.parametrize("source_count", [1, 2])
@pytest.mark.parametrize("second_entry", ["promotion", "vanished"])
def test_sequential_source_preimage_rejection_keeps_independent_edits(
        store, kind, first_scope, reverse_seed, sibling_edits, source_count, second_entry):
    from datetime import date
    from backend.core.card_bills import CardBillWriteBarrier

    a = {"card_no": "****1234", "date": "2026-07-01", "desc": "A", "amount": 100,
         "currency": "TWD", "post_date": None, "txn_type": "spending",
         "native_identity": json.dumps(["ctbc", 1, 101, "1234_0", "a"])}
    if kind != "native":
        a["native_identity"] = None
    if kind == "fx":
        a.update(consume_currency="USD", consume_amount=3)
    safe = {**a, "date": "2026-07-02", "desc": "SAFE",
            "native_identity": json.dumps(["ctbc", 1, 101, "1234_0", "safe"])}
    second_scope = "current" if first_scope == "unbilled" else "unbilled"
    seeds = [(first_scope, [a]), (second_scope, [a, safe])]
    for scope, txns in seeds[::-1] if reverse_seed else seeds:
        store.refresh_card_pending(scope, txns, fetch_ok=None, rules=[])
    annotate(store)
    if sibling_edits == "blank":
        store.conn.execute("UPDATE card_pending_txns SET " + ",".join(f"{f}=?" for f in FIELDS)
                           + " WHERE scope=? AND description='A'",
                           (None, None, None, None, 0, None, second_scope))
        store.commit()
    original_other = rows(store, "card_pending_txns")
    original_other = [row for row in original_other if row["scope"] == second_scope]
    original_safe = next(row for row in original_other if row["description"] == "SAFE")
    assert tuple(original_safe[f] for f in FIELDS) == OVERLAY
    b = {**a, "booking_status": "booked", "post_date": "2026-07-03"}
    barrier = CardBillWriteBarrier(store)
    assert barrier.refresh_card_pending(first_scope, [b] * source_count,
                                        fetch_ok=True, commit=False, rules=[]) == 0
    first = rows(store)
    expected = OVERLAY if source_count == 1 and sibling_edits == "same" else (None, None, None, None, 0, None)
    assert len(first) == source_count
    assert [tuple(row[f] for f in FIELDS) for row in first] == [expected] * source_count
    assert rows(store, "card_pending_txns") == original_other
    if second_entry == "vanished":
        barrier.upsert_card_billed([b], rules=[])
    safe_booked = {**safe, "booking_status": "booked", "post_date": "2026-07-03"}
    incoming = [safe_booked] if second_entry == "vanished" else [b, safe_booked]
    assert barrier.refresh_card_pending(second_scope, incoming, fetch_ok=True, rules=[]) == 0
    billed = rows(store)
    assert [row for row in billed if row["description"] == "A"] == first
    assert rows(store, "card_pending_txns") == []
    kept_safe = next(row for row in billed if row["description"] == "SAFE")
    assert tuple(kept_safe[f] for f in FIELDS) == OVERLAY
    assert len(billed) == source_count + 1
    assert [(row["amount"], row["currency"], row["native_identity"], row["txn_type"])
            for row in first] == [(100, "TWD", a["native_identity"], "spending")] * source_count
    assert store.latest_card_transaction_dates() == (
        {"****1234": date(2026, 7, 3)} if second_entry == "vanished" else {})
    assert_durable(store, billed, [])


@pytest.mark.parametrize("first_scope", ["unbilled", "current"])
@pytest.mark.parametrize("source_count", [1, 2])
def test_statement_source_preimage_rejection_survives_later_explicit_promotion(
        store, first_scope, source_count):
    from backend.core.card_bills import CardBillWriteBarrier

    a = {"card_no": "****1234", "date": "2026-07-01", "desc": "A", "amount": 100,
         "currency": "TWD", "post_date": None, "txn_type": "spending",
         "native_identity": json.dumps(["ctbc", 1, 101, "1234_0", "a"])}
    second_scope = "current" if first_scope == "unbilled" else "unbilled"
    for scope in (first_scope, second_scope):
        store.refresh_card_pending(scope, [a], fetch_ok=None, rules=[])
    annotate(store)
    original_other = next(row for row in rows(store, "card_pending_txns") if row["scope"] == second_scope)
    b = {**a, "post_date": "2026-07-03", "booking_status": "booked"}
    barrier = CardBillWriteBarrier(store)
    barrier.upsert_card_billed([b] * source_count, rules=[])
    assert barrier.refresh_card_pending(first_scope, [], fetch_ok=True, commit=False, rules=[]) == 0
    first = rows(store)
    expected = OVERLAY if source_count == 1 else (None, None, None, None, 0, None)
    assert [tuple(row[f] for f in FIELDS) for row in first] == [expected] * source_count
    assert rows(store, "card_pending_txns") == [original_other]
    assert barrier.refresh_card_pending(second_scope, [b], fetch_ok=True, rules=[]) == 0
    assert rows(store) == first and rows(store, "card_pending_txns") == []
    assert_durable(store, first, [])


@pytest.mark.parametrize("kind", ["native", "fx", "local"])
@pytest.mark.parametrize("first_scope", ["unbilled", "current"])
@pytest.mark.parametrize("reverse_sources", [False, True])
def test_booked_and_pending_source_preimage_remains_ambiguous_after_scope_consumption(
        store, kind, first_scope, reverse_sources):
    from backend.core.card_bills import CardBillWriteBarrier

    a = {"card_no": "****1234", "date": "2026-07-01", "desc": "A", "amount": 100,
         "currency": "TWD", "post_date": None, "txn_type": "spending",
         "native_identity": json.dumps(["ctbc", 1, 101, "1234_0", "a"]) if kind == "native" else None}
    if kind == "fx":
        a.update(consume_currency="USD", consume_amount=3)
    second_scope = "current" if first_scope == "unbilled" else "unbilled"
    for scope in (first_scope, second_scope):
        store.refresh_card_pending(scope, [a], fetch_ok=None, rules=[])
    annotate(store)
    original_other = next(row for row in rows(store, "card_pending_txns") if row["scope"] == second_scope)
    assert tuple(original_other[f] for f in FIELDS) == OVERLAY
    b = {**a, "booking_status": "booked", "post_date": "2026-07-03"}
    unknown = {**a, "booking_status": None}
    sources = [b, unknown]
    barrier = CardBillWriteBarrier(store)
    assert barrier.refresh_card_pending(first_scope, sources[::-1] if reverse_sources else sources,
                                        fetch_ok=True, rules=[], commit=False) == 1
    first = rows(store)
    assert len(first) == 1 and not any(first[0][f] for f in FIELDS)
    pending_rows = rows(store, "card_pending_txns")
    assert next(row for row in pending_rows if row["scope"] == second_scope) == original_other
    assert not any(next(row for row in pending_rows if row["scope"] == first_scope)[f] for f in FIELDS)
    assert barrier.refresh_card_pending(second_scope, [b], fetch_ok=True, rules=[]) == 0
    assert rows(store) == first
    assert barrier.refresh_card_pending(first_scope, [], fetch_ok=True, rules=[]) == 0
    assert rows(store) == first and rows(store, "card_pending_txns") == []
    assert_durable(store, first, [])


def test_source_rejection_ledger_resets_only_at_new_card_batch(store):
    from backend.core.card_bills import CardBillWriteBarrier

    a = {"card_no": "****1234", "date": "2026-07-01", "desc": "A", "amount": 100,
         "currency": "TWD", "post_date": None, "txn_type": "spending",
         "native_identity": json.dumps(["ctbc", 1, 101, "1234_0", "a"])}
    for scope in ("unbilled", "current"):
        store.refresh_card_pending(scope, [a], fetch_ok=None, rules=[])
    annotate(store)
    b = {**a, "booking_status": "booked", "post_date": "2026-07-03"}
    barrier = CardBillWriteBarrier(store)
    barrier.refresh_card_pending("unbilled", [b, b], fetch_ok=True, rules=[], commit=False)
    target_ids = [row["id"] for row in rows(store)]
    assert len(target_ids) == 2
    rejected = {("booked", target_id) for target_id in target_ids}
    assert rejected <= store._unsafe_pending_overlay_identities
    barrier.refresh_card_pending("realtime", [], fetch_ok=False, rules=[], commit=False)
    assert rejected <= store._unsafe_pending_overlay_identities
    assert barrier.persist_card_batch([], [], scope="realtime", fetch_ok=True, rules=[]) == (0, 0)
    assert store._unsafe_pending_overlay_identities == set()
    assert barrier.refresh_card_pending("current", [b], fetch_ok=True, rules=[]) == 0
    assert [row["id"] for row in rows(store)] == target_ids
    assert [tuple(row[f] for f in FIELDS) for row in rows(store)] == [OVERLAY, (None, None, None, None, 0, None)]
    assert rows(store, "card_pending_txns") == []
    assert_durable(store, rows(store), [])


@pytest.mark.parametrize("commit", [False, True])
def test_source_multiplicity_marker_precedes_delete_and_late_sql_failure_restores_preimage(
        store, monkeypatch, commit):
    import sqlite3
    from backend.core.card_bills import CardBillWriteBarrier
    from backend.core.error_codes import exception_error_id

    a = {"card_no": "****1234", "date": "2026-07-01", "desc": "A", "amount": 100,
         "currency": "TWD", "post_date": None, "txn_type": "spending",
         "native_identity": json.dumps(["ctbc", 1, 101, "1234_0", "a"])}
    for scope in ("unbilled", "current"):
        store.refresh_card_pending(scope, [a], fetch_ok=None, rules=[])
    annotate(store)
    before_ram = seed_ledgers(store)
    real = store.conn
    real.execute("CREATE TRIGGER reject_source_insert BEFORE INSERT ON card_pending_txns "
                 "BEGIN SELECT RAISE(ABORT, 'synthetic source multiplicity late failure'); END")
    real.commit()
    before_sql = list(real.iterdump())
    real.execute("BEGIN")
    seen = []
    deletes = []

    class ObserveConnection:
        def __getattr__(self, name):
            return getattr(real, name)

        def execute(self, sql, *args):
            if sql.lstrip().startswith("DELETE FROM card_pending_txns"):
                target_ids = [row[0] for row in real.execute("SELECT id FROM card_billed_txns")]
                assert len(target_ids) == 2
                assert all(("booked", target_id) in store._unsafe_pending_overlay_identities
                           for target_id in target_ids)
                deletes.append(sql)
            if sql.lstrip().startswith("INSERT INTO card_pending_txns"):
                assert deletes
                assert real.execute("SELECT COUNT(*) FROM card_pending_txns WHERE scope='unbilled'").fetchone()[0] == 0
            try:
                return real.execute(sql, *args)
            except sqlite3.IntegrityError as error:
                seen.append(error)
                raise

    monkeypatch.setattr(store, "conn", ObserveConnection())
    b = {**a, "booking_status": "booked", "post_date": "2026-07-03"}
    safe = {**a, "date": "2026-07-02", "desc": "SAFE", "booking_status": None,
            "native_identity": json.dumps(["ctbc", 1, 101, "1234_0", "safe"])}
    with pytest.raises(sqlite3.IntegrityError, match="synthetic source multiplicity late failure") as caught:
        CardBillWriteBarrier(store).refresh_card_pending("unbilled", [b, b, safe], fetch_ok=True,
                                                        rules=[], commit=commit)
    assert caught.value is seen[0]
    assert exception_error_id(caught.value) == "SYS-08-155-025"
    assert list(real.iterdump()) == before_sql
    assert {name: getattr(store, name) for name in LEDGERS} == before_ram
    real.rollback()
    real.execute("DROP TRIGGER reject_source_insert")
    real.commit()


FIELDS = ("category", "subcategory", "description_overwrite", "tags_overwrite",
          "auto_excluded", "splits_overwrite")
OVERLAY = ("購物", "訂閱", "KEEP", '["synthetic"]', 1,
           '[{"amount":40,"category":"餐飲","note":"one","auto_excluded":false},'
           '{"amount":60,"category":"交通","note":"two","auto_excluded":true}]')


@pytest.mark.parametrize("kind", ["native", "mixed-fx"])
@pytest.mark.parametrize("commit", [False, True])
def test_cross_scope_rejected_target_late_insert_failure_restores_sql_and_four_ledgers(
        store, monkeypatch, kind, commit):
    import sqlite3
    from backend.core.card_bills import CardBillWriteBarrier
    from backend.core.error_codes import exception_error_id

    a = {"card_no": "****1234", "date": "2026-07-01", "desc": "A", "amount": 100,
         "currency": "TWD", "post_date": None, "txn_type": "spending",
         "native_identity": json.dumps(["ctbc", 1, 101, "1234_0", "a"])}
    w = {**a, "desc": "W"}
    if kind == "mixed-fx":
        a.update(consume_currency="USD", consume_amount=3)
        w = {**a, "desc": "W", "native_identity": None}
    safe = {**a, "date": "2026-07-02", "desc": "SAFE",
            "native_identity": json.dumps(["ctbc", 1, 101, "1234_0", "safe"])}
    store.refresh_card_pending("unbilled", [a, safe], rules=[])
    store.refresh_card_pending("current", [w], rules=[])
    annotate(store)
    before_ram = seed_ledgers(store)
    real = store.conn
    real.execute("CREATE TRIGGER reject_cross_scope_insert BEFORE INSERT ON card_pending_txns "
                 "BEGIN SELECT RAISE(ABORT, 'synthetic cross-scope late failure'); END")
    real.commit()
    before_sql = list(real.iterdump())
    statement_row = {**a, "post_date": "2026-07-03", "bill_date": "2026-07-18",
                     "native_identity": None if kind == "mixed-fx" else a["native_identity"]}
    seen = []

    class ObserveConnection:
        def __getattr__(self, name):
            return getattr(real, name)

        def execute(self, sql, *args):
            if sql.lstrip().startswith("INSERT INTO card_pending_txns"):
                target_id = real.execute("SELECT id FROM card_billed_txns").fetchone()[0]
                assert ("booked", target_id) in store._unsafe_pending_overlay_identities
                assert real.execute("SELECT COUNT(*) FROM card_pending_txns WHERE scope='unbilled'").fetchone()[0] == 0
            try:
                return real.execute(sql, *args)
            except sqlite3.IntegrityError as error:
                seen.append(error)
                raise

    monkeypatch.setattr(store, "conn", ObserveConnection())
    with pytest.raises(sqlite3.IntegrityError, match="synthetic cross-scope late failure") as caught:
        CardBillWriteBarrier(store).persist_card_batch([statement_row], [safe], scope="unbilled",
                                                      fetch_ok=True, rules=[], commit=commit)
    assert caught.value is seen[0]
    assert exception_error_id(caught.value) == "SYS-08-155-025"
    assert list(real.iterdump()) == before_sql
    assert {name: getattr(store, name) for name in LEDGERS} == before_ram
    real.rollback()
    real.execute("DROP TRIGGER reject_cross_scope_insert")
    real.commit()


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
def test_wrapper_unknown_missing_reference_rename_rebuilds_without_guessed_edits(store, old_ref, new_ref, fx):
    original = pending(acwRefNbr=old_ref, **({"origCurDesc": "USD", "origCurAmt": 3} if fx else {}))
    persist(store, pending=[original])
    annotate(store)
    seed_ledgers(store)
    incoming = {**original, "acwRefNbr": new_ref, "description": "RENAMED"}
    expected_overlay = OVERLAY if fx else (None, None, None, None, 0, None)
    persist(store, pending=[incoming])
    actual = rows(store, "card_pending_txns")
    assert rows(store) == [] and len(actual) == 1
    row = actual[0]
    assert (row["card_no"], row["consume_date"], row["post_date"], row["description"],
            row["amount"], row["currency"], row["consume_currency"], row["consume_amount"],
            row["txn_type"]) == ("****1234", "2026-09-10", None, "RENAMED", 100, "TWD",
                                 "USD" if fx else None, 3 if fx else None, "spending")
    if new_ref is None:
        assert row["native_identity"] is None
    else:
        assert json.loads(row["native_identity"]) == ["ctbc", 1, 101, "1234_0", new_ref]
    # Unique exact original-FX legacy evidence remains an admissible matched control.
    matched = fx
    assert tuple(row[field] for field in FIELDS) == expected_overlay
    assert row["flow_type"] == "expense" and row["income_category"] is None
    assert row["is_subscription"] == int(matched)
    assert_durable(store, [], actual)
    # Replaying the new bank occurrence must not resurrect an unmatchable old edit.
    persist(store, pending=[incoming])
    replay = rows(store, "card_pending_txns")
    assert len(replay) == 1 and rows(store) == []
    assert tuple(replay[0][field] for field in FIELDS) == tuple(row[field] for field in FIELDS)
    assert replay[0]["native_identity"] == row["native_identity"]
    assert_durable(store, [], replay)


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
def test_wrapper_legacy_fx_disappearance_publishes_bank_rows_without_guessed_edits(store, amount, rename):
    persist(store, pending=[pending(acwRefNbr=None, origCurDesc="USD", origCurAmt=3)])
    annotate(store)
    seed_ledgers(store)
    description = "SHOP" if rename else "SHOP CITY"
    incoming = statement(ntAmt=amount, occCurCode="USD", foreignAmt="3", merchantChiName=description)
    expected_splits = json.loads(OVERLAY[-1])
    if amount == 150:
        expected_splits[0]["amount"], expected_splits[1]["amount"] = 60, 90
    expected_overlay = OVERLAY[:-1]
    persist(store, billed=[incoming])
    actual = rows(store)
    assert len(actual) == 1 and rows(store, "card_pending_txns") == []
    row = actual[0]
    assert (row["card_no"], row["consume_date"], row["post_date"], row["bill_date"],
            row["description"], row["amount"], row["currency"], row["consume_currency"],
            row["consume_amount"], row["txn_type"]) == (
                "****1234", "2026-09-10", "2026-09-12", "2026-09-01", description,
                amount, "TWD", "USD", 3, "spending")
    assert json.loads(row["native_identity"]) == ["ctbc", 1, 101, "1234_0", "synthetic-ref"]
    # Unique original FX proves this occurrence despite rename/reference enrichment.
    assert tuple(row[field] for field in FIELDS[:-1]) == expected_overlay
    assert json.loads(row["splits_overwrite"]) == expected_splits
    assert row["flow_type"] == "expense" and row["income_category"] is None
    assert row["is_subscription"] == 1
    assert_durable(store, actual, [])
    persist(store, billed=[incoming])
    assert rows(store) == actual and rows(store, "card_pending_txns") == []
    assert_durable(store, actual, [])


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


def test_wrapper_conflicting_native_exact_occurrence_shrink_keeps_bank_count_without_guessed_edits(store):
    persist(store, pending=[pending()] * 2)
    annotate(store)
    store.conn.execute("UPDATE card_pending_txns SET description_overwrite='OTHER' WHERE id=(SELECT MAX(id) FROM card_pending_txns)")
    store.commit()
    seed_ledgers(store)
    persist(store, pending=[pending()])
    actual = rows(store, "card_pending_txns")
    assert len(actual) == 1 and rows(store) == []
    row = actual[0]
    assert (row["card_no"], row["consume_date"], row["post_date"], row["description"],
            row["amount"], row["currency"], row["consume_currency"], row["consume_amount"],
            row["txn_type"]) == ("****1234", "2026-09-10", None, "SHOP CITY", 100, "TWD", None, None, "spending")
    assert json.loads(row["native_identity"]) == ["ctbc", 1, 101, "1234_0", "synthetic-ref"]
    assert tuple(row[field] for field in FIELDS) == (None, None, None, None, 0, None)
    assert row["flow_type"] == "expense" and row["income_category"] is None
    assert row["is_subscription"] == 0
    assert_durable(store, [], actual)
    persist(store, pending=[pending()])
    replay = rows(store, "card_pending_txns")
    assert len(replay) == 1 and rows(store) == []
    assert tuple(replay[0][field] for field in FIELDS) == (None, None, None, None, 0, None)
    assert replay[0]["native_identity"] == row["native_identity"]
    assert_durable(store, [], replay)


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

