"""Explicit row booking evidence promotes snapshots, never history coverage."""
from datetime import date

import pytest

from backend.core.store import BankStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("DB_BACKEND", raising=False)
    value = BankStore("booking-test", user_id=1, source_account_id=101)
    yield value
    value.close()


ROW = {"card_no": "****1234", "date": "2026-07-01", "post_date": "2026-07-03",
       "desc": "SHOP", "amount": 100, "currency": "TWD", "booking_status": "booked"}


def rows(store, table):
    return [dict(row) for row in store.conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()]


def test_explicit_booked_snapshot_is_durable_without_claiming_history(store):
    store.upsert_card_billed([{**ROW, "date": "2026-06-01", "post_date": "2026-06-03"}])
    store.conn.commit()
    assert store.refresh_card_pending("unbilled", [ROW], fetch_ok=True) == 0
    actual = rows(store, "card_billed_txns")
    assert len(actual) == 2
    assert actual[1]["post_date"] == "2026-07-03"
    assert actual[1]["bill_date"] is None
    assert store.latest_card_transaction_dates() == {"****1234": date(2026, 6, 3)}
    store.refresh_card_pending("unbilled", [], fetch_ok=True)
    assert len(rows(store, "card_billed_txns")) == 2


@pytest.mark.parametrize("post_date", ["", "   ", "00000000", "0000-00-00", "2026-02-30",
                                      "2026-07-03garbage", "2026-07-03T99:99:99", 20260703,
                                      ["2026-07-03"], {"date": "2026-07-03"}])
def test_malformed_booked_post_date_fails_closed(store, post_date):
    assert store.refresh_card_pending("unbilled", [{**ROW, "post_date": post_date}], fetch_ok=True) == 1
    assert rows(store, "card_billed_txns") == []


@pytest.mark.parametrize("status", [None, "unknown", "unbooked", "BOOKED", True])
def test_date_alone_does_not_promote_unknown_or_unbooked(store, status):
    assert store.refresh_card_pending("unbilled", [{**ROW, "booking_status": status}], fetch_ok=True) == 1
    assert rows(store, "card_billed_txns") == []


@pytest.mark.parametrize("fetch_ok", [False, None])
def test_failed_and_legacy_fetch_never_promote(store, fetch_ok):
    store.refresh_card_pending("unbilled", [{**ROW, "booking_status": None}])
    assert store.refresh_card_pending("unbilled", [ROW], fetch_ok=fetch_ok) == 1
    assert rows(store, "card_billed_txns") == []


def test_explicit_booking_without_date_is_not_downgraded(store):
    assert store.refresh_card_pending("unbilled", [{**ROW, "post_date": None}], fetch_ok=True) == 0
    assert rows(store, "card_billed_txns")[0]["post_date"] is None
    assert store.latest_card_transaction_dates() == {}


OVERLAY_FIELDS = ("category", "subcategory", "description_overwrite", "tags_overwrite",
                  "auto_excluded", "splits_overwrite")
OVERLAY = ("旅遊", "訂閱", "my note", '["trip"]', 1,
           '[{"amount":40,"category":"餐飲"},{"amount":60,"category":"購物"}]')


def edit_pending(store, scope="unbilled", overlay=OVERLAY):
    store.conn.execute(
        "UPDATE card_pending_txns SET " + ",".join(f"{field}=?" for field in OVERLAY_FIELDS) + " WHERE scope=?",
        (*overlay, scope),
    )
    store.conn.commit()


def signature(row):
    return tuple(row[field] for field in OVERLAY_FIELDS)


@pytest.mark.parametrize("reverse", [False, True])
def test_batch_renamed_weak_source_preserves_unique_legacy_exact_edits(store, reverse):
    import json
    from backend.core.card_bills import CardBillWriteBarrier

    store.bank = "ctbc"
    old = {**ROW, "desc": "OLD", "post_date": None, "txn_type": "spending",
           "native_identity": None}
    store.refresh_card_pending("unbilled", [old], fetch_ok=None)
    edit_pending(store)
    before = signature(rows(store, "card_pending_txns")[0])
    assert before == OVERLAY
    shared = {**ROW, "txn_type": "spending", "bill_date": "2026-07-18"}
    weak = {**shared, "desc": "NEW", "native_identity": None}
    strong = {**shared, "desc": "OLD", "native_identity": json.dumps(
        ["ctbc", 1, 101, "1234_0", "a"], separators=(",", ":"))}
    for _ in range(2):
        incoming = [strong, weak] if reverse else [weak, strong]
        CardBillWriteBarrier(store).persist_card_batch(
            incoming, [], scope="unbilled", fetch_ok=True, rules=[])
        actual = rows(store, "card_billed_txns")
        assert len(actual) == 2
        matched = next(row for row in actual if row["description"] == "OLD")
        unmatched = next(row for row in actual if row["description"] == "NEW")
        assert signature(matched) == before
        assert signature(unmatched) == (None, None, None, None, 0, None)
        assert rows(store, "card_pending_txns") == []


@pytest.mark.parametrize("source_native", ["absent", None, "actual"])
@pytest.mark.parametrize("route", ["batch", "direct"])
def test_statement_source_missing_native_never_borrows_history(store, source_native, route):
    import json

    store.bank = "ctbc"
    native = json.dumps(["ctbc", 1, 101, "1234_0", "a"], separators=(",", ":"))
    historical = {**ROW, "desc": "NEW", "txn_type": "spending",
                  "bill_date": "2026-07-18", "native_identity": native}
    historical.pop("booking_status")
    store.upsert_card_billed([historical])
    store.conn.commit()
    original = rows(store, "card_billed_txns")[0]
    assert signature(original) == (None, None, None, None, 0, None)
    old = {**historical, "desc": "OLD", "post_date": None}
    store.refresh_card_pending("unbilled", [old], fetch_ok=None)
    edit_pending(store)
    assert signature(rows(store, "card_pending_txns")[0]) == OVERLAY
    source = {**historical}
    if source_native == "absent":
        source.pop("native_identity")
    elif source_native is None:
        source["native_identity"] = None
    for _ in range(2):
        if route == "batch":
            from backend.core.card_bills import CardBillWriteBarrier

            CardBillWriteBarrier(store).persist_card_batch([source], [], scope="unbilled", fetch_ok=True)
        else:
            store.upsert_card_billed([source])
            store.refresh_card_pending("unbilled", [], fetch_ok=True)
        actual = rows(store, "card_billed_txns")
        assert len(actual) == 1
        assert {key: actual[0][key] for key in original if key not in OVERLAY_FIELDS
                and key not in ("flow_type", "income_category", "is_subscription")} == {
                    key: original[key] for key in original if key not in OVERLAY_FIELDS
                    and key not in ("flow_type", "income_category", "is_subscription")}
        assert signature(actual[0]) == (OVERLAY if source_native == "actual"
                                       else (None, None, None, None, 0, None))
        assert rows(store, "card_pending_txns") == []


@pytest.mark.parametrize("donor_desc,donor_native,expected", [
    ("OLD", None, False), ("NEW", None, True), ("OLD", "actual", True),
])
def test_promotion_actual_raw_does_not_borrow_historical_description(
        store, donor_desc, donor_native, expected):
    import json

    store.bank = "ctbc"
    native = json.dumps(["ctbc", 1, 101, "1234_0", "a"], separators=(",", ":"))
    historical = {**ROW, "desc": "OLD", "txn_type": "spending", "native_identity": native}
    store.upsert_card_billed([historical])
    store.conn.commit()
    original = rows(store, "card_billed_txns")[0]
    assert signature(original) == (None, None, None, None, 0, None)
    old = {**historical, "desc": donor_desc, "post_date": None,
           "native_identity": native if donor_native else None, "booking_status": None}
    store.refresh_card_pending("unbilled", [old], fetch_ok=None)
    edit_pending(store)
    assert signature(rows(store, "card_pending_txns")[0]) == OVERLAY
    source = {**historical, "desc": "NEW"}
    for _ in range(2):
        assert store.refresh_card_pending("unbilled", [source], fetch_ok=True, rules=[]) == 0
        actual = rows(store, "card_billed_txns")
        assert len(actual) == 1
        assert (actual[0]["id"], actual[0]["description"], actual[0]["native_identity"],
                actual[0]["amount"], actual[0]["post_date"], actual[0]["consume_date"]) == (
                    original["id"], "OLD", native, 100, "2026-07-03", "2026-07-01")
        assert signature(actual[0]) == (OVERLAY if expected else (None, None, None, None, 0, None))
        assert rows(store, "card_pending_txns") == []


@pytest.mark.parametrize("route", ["batch", "direct", "promotion"])
def test_canonical_blank_card_backfill_is_not_actual_source_ownership(store, route):
    historical = {**ROW, "post_date": None}
    store.upsert_card_billed([historical])
    store.conn.commit()
    original = rows(store, "card_billed_txns")[0]
    store.refresh_card_pending("unbilled", [historical], fetch_ok=None)
    edit_pending(store)
    assert signature(rows(store, "card_pending_txns")[0]) == OVERLAY
    source = {**ROW, "card_no": None}
    if route == "promotion":
        store.refresh_card_pending("unbilled", [source], fetch_ok=True)
    elif route == "batch":
        store.persist_card_batch([source], [], scope="unbilled", fetch_ok=True)
    else:
        store.upsert_card_billed([source])
        store.refresh_card_pending("unbilled", [], fetch_ok=True)
    actual = rows(store, "card_billed_txns")
    assert len(actual) == 1
    assert (actual[0]["id"], actual[0]["card_no"], actual[0]["post_date"]) == (
        original["id"], "****1234", "2026-07-03")
    assert signature(actual[0]) == (None, None, None, None, 0, None)
    assert rows(store, "card_pending_txns") == []


@pytest.mark.parametrize("reverse", [False, True])
def test_skipped_blank_card_source_does_not_misalign_promotion_target(store, reverse):
    import json

    ambiguous = {**ROW, "desc": "AMBIGUOUS", "post_date": None}
    store.upsert_card_billed([ambiguous, {**ambiguous, "card_no": "****9999"}])
    store.conn.commit()
    original = rows(store, "card_billed_txns")
    native = json.dumps(["ctbc", 1, 101, "5678_0", "a"], separators=(",", ":"))
    source = {**ROW, "date": "2026-08-01", "post_date": "2026-08-03",
              "card_no": "****5678", "desc": "NEW", "txn_type": "spending", "native_identity": native}
    store.refresh_card_pending("unbilled", [{**source, "desc": "OLD", "post_date": None}], fetch_ok=None)
    edit_pending(store)
    assert signature(rows(store, "card_pending_txns")[0]) == OVERLAY
    skipped = {**ambiguous, "card_no": None, "post_date": "2026-07-03"}
    incoming = [source, skipped] if reverse else [skipped, source]
    assert store.refresh_card_pending("unbilled", incoming, fetch_ok=True) == 0
    actual = rows(store, "card_billed_txns")
    assert actual[:2] == original
    assert len(actual) == 3 and signature(actual[2]) == OVERLAY
    assert (actual[2]["description"], actual[2]["card_no"], actual[2]["native_identity"]) == (
        "NEW", "****5678", native)
    assert rows(store, "card_pending_txns") == []


@pytest.mark.parametrize("reverse", [False, True])
def test_actual_strong_statement_is_bound_to_own_id_not_historical_native_guess(store, reverse):
    import json

    native = json.dumps(["ctbc", 1, 101, "1234_0", "a"], separators=(",", ":"))
    historical = {**ROW, "desc": "NEW", "txn_type": "spending", "native_identity": native,
                  "bill_date": "2026-07-18"}
    historical.pop("booking_status")
    # Native enrichment legally leaves the original legacy dedup key unchanged.
    store.upsert_card_billed([{**historical, "native_identity": None}])
    store.upsert_card_billed([historical])
    store.conn.commit()
    original = rows(store, "card_billed_txns")[0]
    store.refresh_card_pending("unbilled", [{**historical, "desc": "OLD", "post_date": None}], fetch_ok=None)
    edit_pending(store)
    assert signature(rows(store, "card_pending_txns")[0]) == OVERLAY
    historical = {**historical, "bill_date": "2026-08-18"}
    weak = {**historical, "native_identity": None}
    incoming = [historical, weak] if reverse else [weak, historical]
    store.persist_card_batch(incoming, [], scope="unbilled", fetch_ok=True)
    actual = rows(store, "card_billed_txns")
    assert len(actual) == 2
    bound = next(row for row in actual if store._billed_source_rows[row["id"]][0]["native_identity"] == native)
    other = next(row for row in actual if row["id"] != bound["id"])
    assert signature(bound) == OVERLAY
    assert signature(other) == (None, None, None, None, 0, None)
    assert (actual[0]["id"], actual[0]["native_identity"], actual[0]["description"]) == (
        original["id"], native, "NEW")
    assert rows(store, "card_pending_txns") == []


@pytest.mark.parametrize("missing", ["absent", None])
def test_source_evidence_uses_writer_aliases_and_normalized_defaults_not_target(store, missing):
    historical = {**ROW, "consume_currency": "USD", "consume_amount": 3}
    store.upsert_card_billed([historical])
    store.conn.commit()
    original = rows(store, "card_billed_txns")[0]
    source = {**ROW, "currency": " twd ", "amount": "100", "consume_currency": None,
              "description": "SHADOW", "consume_date": "1999-01-01", "native_identity": None}
    if missing is None:
        source["consume_amount"] = None
    store.upsert_card_billed([source])
    bound_id = store._current_billed_ids[-1]
    assert store._billed_source_rows[bound_id] == [{
        "id": bound_id, "card_no": "****1234", "amount": 100, "currency": "TWD",
        "consume_currency": None, "consume_amount": None, "native_identity": None,
        "txn_type": None, "consume_date": "2026-07-01", "description": "SHOP",
        "post_date": "2026-07-03"}]
    assert source["amount"] == "100" and source["currency"] == " twd "
    assert rows(store, "card_billed_txns")[0] == original


@pytest.mark.parametrize("donor_native", [None, "actual"])
@pytest.mark.parametrize("reverse", [False, True])
def test_same_canonical_id_retains_all_actual_source_occurrences(store, donor_native, reverse):
    import json

    native = json.dumps(["ctbc", 1, 101, "1234_0", "a"], separators=(",", ":"))
    strong = {**ROW, "txn_type": "spending", "native_identity": native, "bill_date": "2026-07-18"}
    strong.pop("booking_status")
    store.upsert_card_billed([{**strong, "native_identity": None}])
    store.upsert_card_billed([strong])
    store.conn.commit()
    original = rows(store, "card_billed_txns")[0]
    donor = {**strong, "post_date": None, "native_identity": native if donor_native else None}
    store.refresh_card_pending("unbilled", [donor], fetch_ok=None)
    edit_pending(store)
    assert signature(rows(store, "card_pending_txns")[0]) == OVERLAY
    weak = {**strong, "native_identity": None}
    store.persist_card_batch([weak, strong] if reverse else [strong, weak], [],
                             scope="unbilled", fetch_ok=True)
    actual = rows(store, "card_billed_txns")
    assert len(actual) == (2 if reverse else 1) and actual[0]["id"] == original["id"]
    assert [signature(row) for row in actual].count(OVERLAY) == int(bool(donor_native))
    assert all(signature(row) in (OVERLAY, (None, None, None, None, 0, None)) for row in actual)
    assert rows(store, "card_pending_txns") == []


@pytest.mark.parametrize("target", ["new", "old", "undated"])
@pytest.mark.parametrize("card_no", ["****1234", "", None])
def test_all_overlays_follow_explicit_occurrence_even_to_old_or_upgraded_target(store, target, card_no):
    txn = {**ROW, "card_no": card_no}
    original_id = None
    if target != "new":
        store.upsert_card_billed([{**txn, "post_date": None if target == "undated" else txn["post_date"]}])
        store.conn.commit()
        original_id = rows(store, "card_billed_txns")[0]["id"]
    store._new_billed_ids.clear()
    store._current_billed_ids.clear()
    store.refresh_card_pending("unbilled", [txn], fetch_ok=None)
    edit_pending(store)
    assert store.refresh_card_pending("unbilled", [txn], fetch_ok=True) == 0
    billed = rows(store, "card_billed_txns")
    assert len(billed) == 1
    assert signature(billed[0]) == OVERLAY
    assert billed[0]["post_date"] == txn["post_date"]
    if original_id is not None:
        assert billed[0]["id"] == original_id
    assert store.refresh_card_pending("unbilled", [txn], fetch_ok=True) == 0
    assert len(rows(store, "card_billed_txns")) == 1


@pytest.mark.parametrize("changed", [{"consume_currency": "EUR", "consume_amount": 4},
                                      {"consume_amount": 4}])
def test_conflicting_old_original_currency_does_not_transfer_edits_or_block_new_source(store, changed):
    txn = {**ROW, "consume_currency": "USD", "consume_amount": 3}
    store.refresh_card_pending("unbilled", [txn])
    edit_pending(store)
    assert store.refresh_card_pending("unbilled", [{**txn, **changed}], fetch_ok=True) == 0
    assert rows(store, "card_pending_txns") == []
    billed = rows(store, "card_billed_txns")
    assert len(billed) == 1 and signature(billed[0]) == (None, None, None, None, 0, None)
    assert billed[0]["consume_currency"] == changed.get("consume_currency", "USD")
    assert billed[0]["consume_amount"] == changed["consume_amount"]


def test_existing_target_cannot_hide_explicit_original_currency_conflict(store):
    txn = {**ROW, "consume_currency": "USD", "consume_amount": 3}
    store.upsert_card_billed([txn])
    store.conn.commit()
    store.refresh_card_pending("unbilled", [txn])
    edit_pending(store)
    before = (rows(store, "card_pending_txns"), rows(store, "card_billed_txns"))
    with pytest.raises(ValueError, match="conflict"):
        store.refresh_card_pending("unbilled", [{**txn, "consume_currency": "EUR"}], fetch_ok=True)
    assert (rows(store, "card_pending_txns"), rows(store, "card_billed_txns")) == before


@pytest.mark.parametrize("disappeared", [False, True])
def test_legacy_exact_adoption_vetoes_conflicting_original_currency(store, disappeared):
    txn = {**ROW, "consume_currency": "USD", "consume_amount": 3}
    txn.pop("booking_status")
    store.refresh_card_pending("unbilled", [txn])
    edit_pending(store)
    store.upsert_card_billed([{**txn, "consume_currency": "EUR", "consume_amount": 4}])
    store.refresh_card_pending("unbilled", [] if disappeared else [txn], fetch_ok=True)
    assert not any(signature(rows(store, "card_billed_txns")[0]))
    if not disappeared:
        assert signature(rows(store, "card_pending_txns")[0]) == OVERLAY


def test_fx_explicit_promotion_rescales_splits_using_original_currency_identity(store):
    txn = {**ROW, "consume_currency": "USD", "consume_amount": 3}
    store.refresh_card_pending("unbilled", [txn], fetch_ok=None)
    edit_pending(store)
    settled = {**txn, "amount": 150, "desc": "SETTLED SHOP"}
    assert store.refresh_card_pending("unbilled", [settled], fetch_ok=True) == 0
    billed = rows(store, "card_billed_txns")[0]
    assert signature(billed)[:-1] == OVERLAY[:-1]
    assert billed["splits_overwrite"] == '[{"amount":60,"category":"餐飲"},{"amount":90,"category":"購物"}]'


def test_promotion_failure_rolls_back_database_and_all_occurrence_ledgers(store, monkeypatch):
    import copy
    from collections import Counter

    store.refresh_card_pending("unbilled", [ROW], fetch_ok=None)
    edit_pending(store)
    store._adopted_pending_raw_keys = {("seed",): {"current": Counter({("raw",): 1})}}
    store._unsafe_pending_overlay_identities = {("unsafe",)}
    names = ("_new_billed_ids", "_current_billed_ids", "_adopted_pending_raw_keys",
             "_unsafe_pending_overlay_identities")
    before = {name: copy.deepcopy(getattr(store, name)) for name in names}
    pending_before = rows(store, "card_pending_txns")
    original = store._refresh_card_pending

    def fail_after_refresh(*args, **kwargs):
        original(*args, **kwargs)
        store._unsafe_pending_overlay_identities.add(("partial",))
        store._adopted_pending_raw_keys.clear()
        raise RuntimeError("injected after membership write")

    monkeypatch.setattr(store, "_refresh_card_pending", fail_after_refresh)
    with pytest.raises(RuntimeError, match="injected"):
        store.refresh_card_pending("unbilled", [ROW], fetch_ok=True)
    assert rows(store, "card_billed_txns") == []
    assert rows(store, "card_pending_txns") == pending_before
    assert {name: getattr(store, name) for name in names} == before
    assert store.latest_card_transaction_dates() == {}
    monkeypatch.setattr(store, "_refresh_card_pending", original)
    assert store.refresh_card_pending("unbilled", [ROW], fetch_ok=True) == 0
    assert signature(rows(store, "card_billed_txns")[0]) == OVERLAY


@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("fx", [False, True])
@pytest.mark.parametrize("attested", [False, True])
def test_fetched_unknown_survives_history_then_disappearance_adopts_overlay(store, mixed, fx, attested):
    txn = {**ROW, "booking_status": "unknown"}
    if not attested:
        txn.pop("booking_status")
    if fx:
        txn.update(consume_currency="USD", consume_amount=3)
    store.refresh_card_pending("unbilled", [txn])
    edit_pending(store)
    store.upsert_card_billed([txn])
    incoming = [txn, {**ROW, "desc": "OTHER", "amount": 200}] if mixed else [txn]
    assert store.refresh_card_pending("unbilled", incoming, fetch_ok=True) == 1
    assert signature(rows(store, "card_pending_txns")[0]) == OVERLAY
    assert not any(signature(rows(store, "card_billed_txns")[0]))
    assert store.refresh_card_pending("unbilled", [], fetch_ok=True) == 0
    assert signature(rows(store, "card_billed_txns")[0]) == OVERLAY


def test_unbooked_occurrence_is_not_pruned_by_sibling_promotion(store):
    unknown = {**ROW, "booking_status": "unknown"}
    assert store.refresh_card_pending("unbilled", [ROW, unknown], fetch_ok=True) == 1
    assert len(rows(store, "card_billed_txns")) == 1
    assert len(rows(store, "card_pending_txns")) == 1
    assert store.refresh_card_pending("unbilled", [unknown], fetch_ok=True) == 1


@pytest.mark.parametrize("overlay", [OVERLAY, ("rule category", None, None, None, 0, None)])
def test_unmatched_vanished_legacy_pending_is_replaced_without_transferring_edits(store, overlay):
    old = {**ROW, "desc": "OLD SHOP", "booking_status": "unknown", "post_date": None}
    store.refresh_card_pending("unbilled", [old], fetch_ok=True)
    edit_pending(store, overlay=overlay)
    rules = [{"pattern": "NEW SHOP", "category": "餐飲", "subcategory": "其他"}]
    incoming = {**ROW, "desc": "NEW SHOP"}
    assert store.refresh_card_pending("unbilled", [incoming], rules=rules, fetch_ok=True) == 0
    billed = rows(store, "card_billed_txns")
    assert rows(store, "card_pending_txns") == []
    assert len(billed) == 1
    assert billed[0]["description"] == "NEW SHOP"
    assert signature(billed[0]) == ("餐飲", "其他", None, None, 0, None)
    target_id = billed[0]["id"]
    assert store.refresh_card_pending("unbilled", [incoming], rules=rules, fetch_ok=True) == 0
    assert [r["id"] for r in rows(store, "card_billed_txns")] == [target_id]
    assert store.latest_card_transaction_dates() == {}


def test_attested_missing_reference_replaces_unpairable_legacy_pending(store):
    old = {**ROW, "desc": "OLD SHOP", "booking_status": "unknown"}
    store.refresh_card_pending("unbilled", [old], fetch_ok=True)
    edit_pending(store)
    incoming = {**ROW, "desc": "NEW SHOP", "native_identity": None}
    assert store.persist_card_batch(billed=[incoming], pending=[], scope="unbilled", fetch_ok=True) == (1, 0)
    assert rows(store, "card_pending_txns") == []
    billed = rows(store, "card_billed_txns")
    assert len(billed) == 1 and billed[0]["description"] == "NEW SHOP"
    assert signature(billed[0]) == (None, None, None, None, 0, None)


@pytest.mark.parametrize("unsafe_match", ["zero", "ambiguous", "invalid-split", "existing-edit", "scope-conflict"])
@pytest.mark.parametrize("safe_status", ["booked", "unknown"])
def test_unsafe_promotion_group_does_not_discard_unrelated_safe_edit(store, unsafe_match, safe_status):
    unsafe = {**ROW, "booking_status": "unknown"}
    safe = {**ROW, "desc": "SAFE SHOP", "date": "2026-07-02", "booking_status": "unknown"}
    store.refresh_card_pending("unbilled", [unsafe, safe], fetch_ok=True)
    edit_pending(store)
    if unsafe_match == "invalid-split":
        store.conn.execute("UPDATE card_pending_txns SET splits_overwrite='bad json' WHERE description='SHOP'")
    if unsafe_match == "scope-conflict":
        store.refresh_card_pending("current", [unsafe], fetch_ok=True)
        edit_pending(store, "current", ("other", *OVERLAY[1:]))
    unsafe_incoming = {**ROW, "desc": "RENAMED"} if unsafe_match == "zero" else dict(ROW)
    if unsafe_match == "existing-edit":
        store.upsert_card_billed([unsafe_incoming])
        store.conn.execute("UPDATE card_billed_txns SET category='existing edit'")
        store._new_billed_ids.clear()
        store._current_billed_ids.clear()
    store.conn.commit()
    incoming = [unsafe_incoming] * (2 if unsafe_match == "ambiguous" else 1)
    incoming.append({**safe, "booking_status": safe_status})
    ids = None
    for _ in range(2):
        store.refresh_card_pending("unbilled", incoming, fetch_ok=True)
        billed = rows(store, "card_billed_txns")
        pending = rows(store, "card_pending_txns")
        kept = next(r for r in (*billed, *pending) if r["description"] == "SAFE SHOP")
        assert signature(kept) == OVERLAY
        assert (kept["amount"], kept["currency"], kept["consume_date"], kept["post_date"]) == (
            100, "TWD", "2026-07-02", "2026-07-03")
        unsafe_rows = [r for r in billed if r["description"] != "SAFE SHOP"]
        assert len(unsafe_rows) == (2 if unsafe_match == "ambiguous" else 1)
        assert all(signature(r) == (("existing edit", None, None, None, 0, None)
                                   if unsafe_match == "existing-edit" else (None, None, None, None, 0, None))
                   for r in unsafe_rows)
        assert len([r for r in pending if r["scope"] == "unbilled"]) == int(safe_status == "unknown")
        if unsafe_match == "scope-conflict":
            assert signature(next(r for r in pending if r["scope"] == "current")) == ("other", *OVERLAY[1:])
        current_ids = [r["id"] for r in billed]
        if ids is not None:
            assert current_ids == ids
        ids = current_ids


def test_growing_legacy_pending_occurrences_do_not_guess_one_old_edit(store):
    unsafe = {**ROW, "booking_status": "unknown", "post_date": None}
    safe = {**unsafe, "desc": "SAFE SHOP", "date": "2026-07-02"}
    store.refresh_card_pending("unbilled", [unsafe, safe], fetch_ok=True)
    edit_pending(store)
    for _ in range(2):
        assert store.refresh_card_pending("unbilled", [unsafe, unsafe, safe], fetch_ok=True) == 3
        pending = rows(store, "card_pending_txns")
        assert signature(next(r for r in pending if r["description"] == "SAFE SHOP")) == OVERLAY
        ambiguous = [r for r in pending if r["description"] == "SHOP"]
        assert len(ambiguous) == 2
        assert all(signature(r) == (None, None, None, None, 0, None) for r in ambiguous)


@pytest.mark.parametrize("case", ["repeated", "mixed", "invalid-split", "existing-edit", "scope-conflict"])
def test_unpairable_pending_edits_do_not_reject_authoritative_replacement(store, case):
    txn = dict(ROW)
    if case == "existing-edit":
        store.upsert_card_billed([txn])
        store.conn.execute("UPDATE card_billed_txns SET category='existing edit'")
        store.conn.commit()
        store._new_billed_ids.clear()
        store._current_billed_ids.clear()
    store.refresh_card_pending("unbilled", [txn])
    edit_pending(store, overlay=(*OVERLAY[:-1], "bad json") if case == "invalid-split" else OVERLAY)
    if case == "scope-conflict":
        store.refresh_card_pending("current", [txn])
        edit_pending(store, "current", ("other", *OVERLAY[1:]))
    incoming = [txn, txn] if case == "repeated" else [txn]
    if case == "mixed":
        incoming.append({**txn, "booking_status": "unknown"})
    assert store.refresh_card_pending("unbilled", incoming, fetch_ok=True) == (1 if case == "mixed" else 0)
    billed = rows(store, "card_billed_txns")
    assert len(billed) == (2 if case == "repeated" else 1)
    for row in billed:
        assert signature(row) == (("existing edit", None, None, None, 0, None)
                                  if case == "existing-edit" else (None, None, None, None, 0, None))
    pending = rows(store, "card_pending_txns")
    if case == "scope-conflict":
        assert len(pending) == 1 and pending[0]["scope"] == "current"
        assert signature(pending[0]) == ("other", *OVERLAY[1:])
    elif case == "mixed":
        assert len(pending) == 1 and signature(pending[0]) == (None, None, None, None, 0, None)
    else:
        assert pending == []


def test_ambiguous_blank_card_source_still_rolls_back(store):
    txn = dict(ROW)
    store.upsert_card_billed([{**txn, "card_no": card} for card in ("****1111", "****2222")])
    store.conn.commit()
    txn["card_no"] = ""
    store.refresh_card_pending("unbilled", [txn])
    edit_pending(store)
    before = (rows(store, "card_pending_txns"), rows(store, "card_billed_txns"))
    with pytest.raises(ValueError, match="ambiguous"):
        store.refresh_card_pending("unbilled", [txn], fetch_ok=True)
    assert (rows(store, "card_pending_txns"), rows(store, "card_billed_txns")) == before


@pytest.mark.parametrize("post_date", ["2026-07-03", None])
def test_repeated_unedited_purchases_survive_promotion_and_replay(store, post_date):
    txn = {**ROW, "post_date": post_date}
    assert store.refresh_card_pending("unbilled", [txn, txn], fetch_ok=True) == 0
    ids = [row["id"] for row in rows(store, "card_billed_txns")]
    assert len(ids) == 2
    assert store.refresh_card_pending("unbilled", [txn, txn], fetch_ok=True) == 0
    assert [row["id"] for row in rows(store, "card_billed_txns")] == ids
    if post_date:
        assert store.upsert_card_billed([{**txn, "bill_date": "2026-08-01"}] * 2) == 0
        assert store.upsert_card_billed([{**txn, "bill_date": "2026-09-01"}] * 2) == 0
        assert [row["id"] for row in rows(store, "card_billed_txns")] == ids


def test_cross_scope_equal_overlays_are_preserved_without_deleting_sibling_scope(store):
    for scope in ("unbilled", "current"):
        store.refresh_card_pending(scope, [ROW])
        edit_pending(store, scope)
    assert store.refresh_card_pending("unbilled", [ROW], fetch_ok=True) == 0
    assert len(rows(store, "card_pending_txns")) == 1
    assert rows(store, "card_pending_txns")[0]["scope"] == "current"
    assert store.refresh_card_pending("current", [ROW], fetch_ok=True) == 0
    assert signature(rows(store, "card_billed_txns")[0]) == OVERLAY


def test_bank_attested_future_date_is_preserved_without_claiming_history(store):
    from datetime import timedelta

    future = (date.today() + timedelta(days=1)).isoformat()
    assert store.refresh_card_pending("unbilled", [{**ROW, "post_date": future}], fetch_ok=True) == 0
    assert rows(store, "card_billed_txns")[0]["post_date"] == future
    assert store.latest_card_transaction_dates() == {}


def test_commit_false_leaves_promotions_rollbackable(store):
    assert store.refresh_card_pending("unbilled", [ROW], fetch_ok=True, commit=False) == 0
    assert len(rows(store, "card_billed_txns")) == 1
    store.rollback()
    assert rows(store, "card_billed_txns") == []


def test_distinct_real_posting_dates_are_not_merged(store):
    txns = [ROW, {**ROW, "post_date": "2026-08-03"}]
    assert store.refresh_card_pending("unbilled", txns, fetch_ok=True) == 0
    assert [row["post_date"] for row in rows(store, "card_billed_txns")] == ["2026-07-03", "2026-08-03"]


def test_new_rules_do_not_overwrite_pending_manual_overlays(store):
    store.refresh_card_pending("unbilled", [ROW])
    edit_pending(store)
    rules = [{"pattern": "SHOP", "category": "餐飲", "subcategory": "早餐"}]
    assert store.refresh_card_pending("unbilled", [ROW], rules=rules, fetch_ok=True) == 0
    assert signature(rows(store, "card_billed_txns")[0]) == OVERLAY


def test_cross_scope_edit_is_not_guessed_for_booked_and_unknown_occurrences(store):
    store.refresh_card_pending("current", [ROW])
    edit_pending(store, "current")
    unknown = {**ROW, "booking_status": "unknown"}
    before = rows(store, "card_pending_txns")
    assert store.refresh_card_pending("unbilled", [ROW, unknown], fetch_ok=True) == 1
    pending = rows(store, "card_pending_txns")
    assert [r for r in pending if r["scope"] == "current"] == before
    assert signature(next(r for r in pending if r["scope"] == "unbilled")) == (None, None, None, None, 0, None)
    billed = rows(store, "card_billed_txns")
    assert len(billed) == 1 and signature(billed[0]) == (None, None, None, None, 0, None)


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("post_date", [None, "2026-07-03"])
def test_undated_occurrences_enrich_without_copying_or_collapsing(store, count, post_date):
    txn = {**ROW, "post_date": None}
    assert store.refresh_card_pending("unbilled", [txn] * count, fetch_ok=True) == 0
    ids = [r["id"] for r in rows(store, "card_billed_txns")]
    enriched = {**txn, "post_date": post_date, "bill_date": "2026-07-01"}
    assert store.refresh_card_pending("unbilled", [enriched] * count, fetch_ok=True) == 0
    actual = rows(store, "card_billed_txns")
    assert [r["id"] for r in actual] == ids
    assert [r["post_date"] for r in actual] == [post_date] * count
    assert [r["bill_date"] for r in actual] == ["2026-07-01"] * count
    assert store.upsert_card_billed([enriched] * count) == 0
    assert [r["id"] for r in rows(store, "card_billed_txns")] == ids
    if post_date is None:
        assert store.upsert_card_billed([{**enriched, "bill_date": "2026-08-01"}] * count) == count
        assert len(rows(store, "card_billed_txns")) == count * 2


@pytest.mark.parametrize("historical,donor,incoming", [
    ({}, {"consume_currency": "EUR", "consume_amount": 4}, {"consume_currency": "USD"}),
    ({"consume_amount": 3}, {"consume_currency": "EUR", "consume_amount": 3},
     {"consume_currency": "USD", "consume_amount": 3}),
])
def test_sparse_historical_target_is_not_given_conflicting_pending_edits(store, historical, donor, incoming):
    store.upsert_card_billed([{**ROW, **historical}])
    store.conn.execute("UPDATE card_billed_txns SET consume_currency=NULL")
    store.conn.commit()
    store.refresh_card_pending("unbilled", [{**ROW, **donor}])
    edit_pending(store)
    before = rows(store, "card_billed_txns")
    assert store.refresh_card_pending("unbilled", [{**ROW, **incoming}], fetch_ok=True) == 0
    assert rows(store, "card_pending_txns") == []
    assert rows(store, "card_billed_txns") == before


@pytest.mark.parametrize("count", [1, 2])
def test_dated_promoted_occurrence_enriches_statement_date_without_duplication(store, count):
    store.refresh_card_pending("unbilled", [ROW] * count, fetch_ok=True)
    ids = [row["id"] for row in rows(store, "card_billed_txns")]
    assert store.latest_card_transaction_dates() == {}
    statement = {**ROW, "bill_date": "2026-07-18"}
    assert store.upsert_card_billed([statement] * count) == 0
    actual = rows(store, "card_billed_txns")
    assert [row["id"] for row in actual] == ids
    assert [row["bill_date"] for row in actual] == ["2026-07-18"] * count
    assert store.upsert_card_billed([statement] * count) == 0


@pytest.mark.parametrize("evidence", ["native", "fx"])
@pytest.mark.parametrize("weak_scope", ["unbilled", "current"])
@pytest.mark.parametrize("booked", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("other_raw", [False, True])
def test_global_weak_sibling_cannot_invalidate_unique_strong_donor(
        store, evidence, weak_scope, booked, reverse, other_raw):
    import json

    old = {**ROW, "booking_status": "unknown", "post_date": None, "txn_type": "spending"}
    if evidence == "native":
        strong = {**old, "native_identity": json.dumps([store.bank, 1, 101, "1234", "strong-a"])}
        other = {**old, "native_identity": json.dumps([store.bank, 1, 101, "1234", "strong-b"])}
    else:
        strong = {**old, "consume_currency": "USD", "consume_amount": 3}
        other = {**old, "consume_currency": "EUR", "consume_amount": 4}
    if other_raw:
        other["desc"] = "OTHER SHOP"
    if weak_scope == "unbilled":
        store.refresh_card_pending("unbilled", [old, strong], fetch_ok=True)
    else:
        store.refresh_card_pending("current", [old], fetch_ok=True)
        store.refresh_card_pending("unbilled", [strong], fetch_ok=True)
    donor_rows = rows(store, "card_pending_txns")
    strong_id = next(r["id"] for r in donor_rows if r["native_identity"] or r["consume_amount"] == 3)
    store.conn.execute("UPDATE card_pending_txns SET " + ",".join(f"{f}=?" for f in OVERLAY_FIELDS)
                       + " WHERE id=?", (*OVERLAY, strong_id))
    store.conn.execute("UPDATE card_pending_txns SET description_overwrite='weak note' WHERE id<>?", (strong_id,))
    store.conn.commit()
    current_before = [r for r in rows(store, "card_pending_txns") if r["scope"] == "current"]
    incoming = [{**txn, "booking_status": "booked" if booked else "unknown",
                 "post_date": ROW["post_date"] if booked else None} for txn in (strong, other)]
    if reverse:
        incoming.reverse()
    ids = None
    for _ in range(2):
        assert store.refresh_card_pending("unbilled", incoming, fetch_ok=True) == (0 if booked else 2)
        pending = rows(store, "card_pending_txns")
        assert [r for r in pending if r["scope"] == "current"] == current_before
        actual = rows(store, "card_billed_txns") if booked else [r for r in pending if r["scope"] == "unbilled"]
        assert len(actual) == 2
        kept = next(r for r in actual if r["native_identity"] == strong.get("native_identity")
                    and r["consume_amount"] == strong.get("consume_amount"))
        assert signature(kept) == OVERLAY
        assert signature(next(r for r in actual if r["id"] != kept["id"])) == (None, None, None, None, 0, None)
        for r in actual:
            source = strong if r is kept else other
            assert (r["card_no"], r["consume_date"], r["amount"], r["currency"], r["description"], r["post_date"]) == (
                source["card_no"], source["date"], source["amount"], source["currency"], source["desc"],
                ROW["post_date"] if booked else None)
        current_ids = [r["id"] for r in rows(store, "card_billed_txns")]
        if ids is not None:
            assert current_ids == ids
        ids = current_ids


@pytest.mark.parametrize("evidence", ["native", "fx"])
@pytest.mark.parametrize("seed_weak", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("statuses", [(False, False), (True, False), (False, True), (True, True)])
def test_reference_less_incoming_sibling_cannot_take_unique_strong_overlay(
        store, evidence, seed_weak, reverse, statuses):
    import json

    weak = {**ROW, "booking_status": "unknown", "post_date": None, "txn_type": "spending"}
    strong = {**weak}
    if evidence == "native":
        strong["native_identity"] = json.dumps([store.bank, 1, 101, "1234", "strong-a"])
    else:
        strong.update(consume_currency="USD", consume_amount=3)
    store.refresh_card_pending("unbilled", [weak, strong] if seed_weak else [strong], fetch_ok=True)
    donor = next(r for r in rows(store, "card_pending_txns")
                 if r["native_identity"] or r["consume_amount"] == 3)
    store.conn.execute("UPDATE card_pending_txns SET " + ",".join(f"{f}=?" for f in OVERLAY_FIELDS)
                       + " WHERE id=?", (*OVERLAY, donor["id"]))
    store.conn.execute("UPDATE card_pending_txns SET description_overwrite='ambiguous weak edit' WHERE id<>?",
                       (donor["id"],))
    store.conn.commit()
    incoming = [{**txn, "booking_status": "booked" if booked else "unknown",
                 "post_date": ROW["post_date"] if booked else None}
                for txn, booked in zip((strong, weak), statuses, strict=True)]
    if reverse:
        incoming.reverse()
    billed_ids = None
    for _ in range(2):
        store.refresh_card_pending("unbilled", incoming, fetch_ok=True)
        pending = rows(store, "card_pending_txns")
        billed = rows(store, "card_billed_txns")
        actual = [*pending, *billed]
        assert len(actual) == 2
        kept = next(r for r in actual if r["native_identity"] or r["consume_amount"] == 3)
        other = next(r for r in actual if r["id"] != kept["id"] or r is not kept)
        assert signature(kept) == OVERLAY
        assert signature(other) == (None, None, None, None, 0, None)
        assert len(pending) == statuses.count(False)
        for row, booked in ((kept, statuses[0]), (other, statuses[1])):
            assert (row["card_no"], row["consume_date"], row["description"], row["amount"],
                    row["currency"], row["post_date"]) == (
                        ROW["card_no"], ROW["date"], ROW["desc"], 100, "TWD",
                        ROW["post_date"] if booked else None)
        if billed_ids is not None:
            assert [r["id"] for r in billed] == billed_ids
        billed_ids = [r["id"] for r in billed]


@pytest.mark.parametrize("nested", [False, True])
def test_batch_commit_false_keeps_caller_owned_transaction_rollbackable(store, nested):
    old = {**ROW, "booking_status": "unknown", "post_date": None}
    store.refresh_card_pending("unbilled", [old], fetch_ok=True)
    edit_pending(store)
    before = list(store.conn.iterdump())
    if nested:
        store.begin_transaction()
    assert store.persist_card_batch([], [ROW], scope="unbilled", fetch_ok=True, commit=False) == (0, 0)
    assert signature(rows(store, "card_billed_txns")[0]) == OVERLAY
    assert store.conn.in_transaction
    store.rollback()
    assert list(store.conn.iterdump()) == before


def test_undated_booked_row_later_statement_enrichment_preserves_id_and_edits(store):
    txn = {**ROW, "post_date": None}
    store.refresh_card_pending("unbilled", [txn])
    edit_pending(store)
    assert store.refresh_card_pending("unbilled", [txn], fetch_ok=True) == 0
    original = rows(store, "card_billed_txns")[0]
    assert store.upsert_card_billed([{**ROW, "bill_date": "2026-08-01"}]) == 0
    assert store.upsert_card_billed([{**ROW, "bill_date": "2026-09-01"}]) == 0
    actual = rows(store, "card_billed_txns")
    assert len(actual) == 1
    assert actual[0]["id"] == original["id"]
    assert signature(actual[0]) == OVERLAY


@pytest.mark.parametrize("mode", ["pending", "promotion", "statement"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("old_reverse", [False, True])
def test_weak_fx_exact_target_cannot_hide_renamed_native_source(store, mode, reverse, old_reverse):
    import json

    weak = {**ROW, "desc": "WEAK SHOP", "post_date": None, "booking_status": None,
            "txn_type": "spending", "consume_currency": "USD", "consume_amount": 3,
            "native_identity": None}
    strong = {**weak, "desc": "OLD STRONG SHOP",
              "native_identity": json.dumps([store.bank, 1, 101, "1234", "strong-a"])}
    store.refresh_card_pending("unbilled", [strong, weak] if old_reverse else [weak, strong], fetch_ok=True)
    donor = next(r for r in rows(store, "card_pending_txns") if r["native_identity"])
    store.conn.execute("UPDATE card_pending_txns SET " + ",".join(f"{f}=?" for f in OVERLAY_FIELDS)
                       + " WHERE id=?", (*OVERLAY, donor["id"]))
    store.conn.execute("UPDATE card_pending_txns SET description_overwrite='unsafe weak note' WHERE native_identity IS NULL")
    store.conn.commit()
    incoming = [{**strong, "desc": "RENAMED STRONG SHOP"}, weak]
    if mode != "pending":
        incoming = [{**txn, "post_date": ROW["post_date"], "booking_status": "booked"} for txn in incoming]
    if reverse:
        incoming.reverse()
    ids = None
    for _ in range(2):
        if mode == "statement":
            store.persist_card_batch([{**txn, "bill_date": "2026-07-18"} for txn in incoming], [],
                                     scope="unbilled", fetch_ok=True)
        else:
            store.refresh_card_pending("unbilled", incoming, fetch_ok=True)
        pending = rows(store, "card_pending_txns")
        billed = rows(store, "card_billed_txns")
        actual = [*pending, *billed]
        assert len(actual) == 2
        kept = next(r for r in actual if r["native_identity"])
        other = next(r for r in actual if r["native_identity"] is None)
        assert signature(kept) == OVERLAY
        assert signature(other) == (None, None, None, None, 0, None)
        assert len(pending) == (2 if mode == "pending" else 0)
        assert [(r["amount"], r["currency"], r["consume_currency"], r["consume_amount"])
                for r in actual] == [(100, "TWD", "USD", 3)] * 2
        current_ids = [r["id"] for r in billed]
        if ids is not None:
            assert current_ids == ids
        ids = current_ids
@pytest.mark.parametrize("entry,commit,deny_rollback", [
    (entry, commit, deny) for entry in ("pending", "promotion", "batch")
    for commit in (False, True) for deny in (False, True)
] + [("scsb", True, False)])
def test_real_sqlite_release_failure_restores_owned_scope(store, monkeypatch, entry, commit, deny_rollback):
    from copy import deepcopy
    import sqlite3
    from backend.core.error_codes import exception_error_id
    from backend.core.persist.scsb import persist_scsb

    old = {**ROW, "post_date": None, "booking_status": None}
    store.refresh_card_pending("unbilled", [old], fetch_ok=True)
    edit_pending(store)
    names = ("_new_billed_ids", "_current_billed_ids", "_adopted_pending_raw_keys",
             "_unsafe_pending_overlay_identities")
    before_ram = {name: deepcopy(getattr(store, name)) for name in names}
    real = store.conn
    before_sql = list(real.iterdump())
    real.execute("BEGIN")
    real.execute("INSERT INTO sync_log(user_id,synced_at,summary) VALUES(1,'synthetic','caller-owned')")
    caller_sql = list(real.iterdump())
    target = {"pending": "pending_overlay_attempt", "promotion": "card_booking_promotion",
              "batch": "card_batch", "scsb": "scsb_persist"}[entry]
    seen = []
    denied = []

    def authorizer(action, operation, name, *_):
        if action == sqlite3.SQLITE_SAVEPOINT and name == target:
            if operation == "RELEASE" and not denied:
                denied.append(operation)
                return sqlite3.SQLITE_DENY
            if operation == "ROLLBACK" and deny_rollback:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    class ObserveConnection:
        def __getattr__(self, name):
            return getattr(real, name)

        def execute(self, sql, *args):
            if sql == f"RELEASE SAVEPOINT {target}" and not denied:
                assert list(real.iterdump()) != caller_sql  # Successful body had real SQL effects.
            try:
                return real.execute(sql, *args)
            except sqlite3.DatabaseError as error:
                seen.append(error)
                raise

    real.set_authorizer(authorizer)
    monkeypatch.setattr(store, "conn", ObserveConnection())
    try:
        with pytest.raises(sqlite3.DatabaseError, match="authorized") as caught:
            if entry == "batch":
                store.persist_card_batch([], [ROW], scope="unbilled", fetch_ok=True, commit=commit)
            elif entry == "scsb":
                persist_scsb({}, store)
            else:
                store.refresh_card_pending("unbilled", [ROW if entry == "promotion" else
                    {**old, "desc": "NEW SHOP"}], fetch_ok=True, commit=commit)
        assert denied == ["RELEASE"] and caught.value is seen[0]
        assert exception_error_id(caught.value) == "SYS-08-146-003"
        assert {name: getattr(store, name) for name in names} == before_ram
        if entry == "scsb":
            assert list(real.iterdump()) == caller_sql
        elif commit:
            assert list(real.iterdump()) == before_sql
            assert not real.in_transaction
        elif not deny_rollback:
            assert list(real.iterdump()) == caller_sql
            assert real.in_transaction
        else:
            assert list(real.iterdump()) != caller_sql  # Failed physical cleanup is not SQL restoration.
    finally:
        real.set_authorizer(None)
        real.rollback()
@pytest.mark.parametrize("missing", [{}, {"native_identity": None, "consume_currency": None, "consume_amount": None}])
def test_absent_source_evidence_is_not_inherited_from_historical_target(store, missing):
    import json

    native = json.dumps([store.bank, 1, 101, "1234", "synthetic-a"])
    historical = {**ROW, "desc": "NEW SHOP", "txn_type": "spending", "native_identity": native}
    store.upsert_card_billed([historical])
    store.conn.commit()
    original = rows(store, "card_billed_txns")[0]
    old = {**historical, "desc": "OLD SHOP", "post_date": None, "booking_status": None}
    store.refresh_card_pending("unbilled", [old], fetch_ok=True)
    edit_pending(store)
    weak = {**ROW, "desc": "NEW SHOP", "txn_type": "spending", **missing}
    unknown = {**old, "desc": "RENAMED SHOP"}
    for _ in range(2):
        assert store.refresh_card_pending("unbilled", [weak, unknown], fetch_ok=True) == 1
        pending = rows(store, "card_pending_txns")
        assert signature(pending[0]) == OVERLAY
        assert pending[0]["native_identity"] == native and pending[0]["description"] == "RENAMED SHOP"
        assert rows(store, "card_billed_txns") == [original]

