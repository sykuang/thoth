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
def test_conflicting_original_currency_cannot_take_exact_local_overlay(store, changed):
    txn = {**ROW, "consume_currency": "USD", "consume_amount": 3}
    store.refresh_card_pending("unbilled", [txn])
    edit_pending(store)
    before = rows(store, "card_pending_txns")
    with pytest.raises(ValueError, match="ambiguous"):
        store.refresh_card_pending("unbilled", [{**txn, **changed}], fetch_ok=True)
    assert rows(store, "card_pending_txns") == before
    assert rows(store, "card_billed_txns") == []


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


@pytest.mark.parametrize("case", ["existing-edit", "scope-conflict", "repeated-edit", "invalid-split",
                                  "renamed-twd", "mixed-booking-occurrences", "blank-card-ambiguity"])
def test_ambiguous_overlay_transitions_raise_without_discarding_edits(store, case):
    txn = dict(ROW)
    if case == "existing-edit":
        store.upsert_card_billed([txn])
        store.conn.execute("UPDATE card_billed_txns SET category='existing edit'")
        store.conn.commit()
        store._new_billed_ids.clear()
        store._current_billed_ids.clear()
    if case == "blank-card-ambiguity":
        store.upsert_card_billed([{**txn, "card_no": card} for card in ("****1111", "****2222")])
        store.conn.commit()
        txn["card_no"] = ""
    store.refresh_card_pending("unbilled", [txn, txn] if case == "repeated-edit" else [txn])
    edit_pending(store, overlay=(*OVERLAY[:-1], "bad json") if case == "invalid-split" else OVERLAY)
    if case == "scope-conflict":
        store.refresh_card_pending("current", [txn])
        edit_pending(store, "current", ("other", *OVERLAY[1:]))
    incoming = [txn]
    if case == "renamed-twd":
        incoming = [{**txn, "desc": "renamed without stable id"}]
    elif case == "repeated-edit":
        incoming = [txn, txn]
    elif case == "mixed-booking-occurrences":
        incoming = [txn, {**txn, "booking_status": "unknown"}]
    before = (rows(store, "card_pending_txns"), rows(store, "card_billed_txns"))
    with pytest.raises(ValueError, match="ambiguous|conflict|preserve"):
        store.refresh_card_pending("unbilled", incoming, fetch_ok=True)
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


def test_cross_scope_edit_cannot_choose_between_booked_and_unknown_occurrences(store):
    store.refresh_card_pending("current", [ROW])
    edit_pending(store, "current")
    unknown = {**ROW, "booking_status": "unknown"}
    before = rows(store, "card_pending_txns")
    with pytest.raises(ValueError, match="ambiguous"):
        store.refresh_card_pending("unbilled", [ROW, unknown], fetch_ok=True)
    assert rows(store, "card_pending_txns") == before
    assert rows(store, "card_billed_txns") == []


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
def test_sparse_historical_target_cannot_wash_out_incoming_fx_evidence(store, historical, donor, incoming):
    import copy

    store.upsert_card_billed([{**ROW, **historical}])
    store.conn.execute("UPDATE card_billed_txns SET consume_currency=NULL")
    store.conn.commit()
    store.refresh_card_pending("unbilled", [{**ROW, **donor}])
    edit_pending(store)
    names = ("_new_billed_ids", "_current_billed_ids", "_adopted_pending_raw_keys",
             "_unsafe_pending_overlay_identities")
    before = (rows(store, "card_billed_txns"), rows(store, "card_pending_txns"),
              {name: copy.deepcopy(getattr(store, name)) for name in names})
    with pytest.raises(ValueError, match="ambiguous|conflict"):
        store.refresh_card_pending("unbilled", [{**ROW, **incoming}], fetch_ok=True)
    assert (rows(store, "card_billed_txns"), rows(store, "card_pending_txns"),
            {name: getattr(store, name) for name in names}) == before


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
