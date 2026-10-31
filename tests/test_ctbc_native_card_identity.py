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


def unbilled(ref: str | None = "synthetic-a", **changes):
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


@pytest.mark.parametrize("with_billed", [False, True])
def test_complete_changed_native_reference_replaces_old_pending_edits(store, with_billed):
    persist_ctbc(payload(pending=[unbilled(postingDt="00000000")]), store)
    store.conn.execute("UPDATE card_pending_txns SET description_overwrite='old note', "
                       "tags_overwrite='[\"old\"]', auto_excluded=1, "
                       "splits_overwrite='[{\"amount\":200},{\"amount\":300}]'")
    store.conn.commit()
    incoming = unbilled(ref="synthetic-c", postingDt="00000000")
    persist_ctbc(payload(pending=[incoming], billed=[statement(ref="synthetic-b")]
                         if with_billed else []), store)
    pending = [dict(r) for r in store.conn.execute("SELECT * FROM card_pending_txns").fetchall()]
    assert len(pending) == 1
    assert (pending[0]["description_overwrite"], pending[0]["tags_overwrite"],
            pending[0]["auto_excluded"], pending[0]["splits_overwrite"]) == (None, None, 0, None)
    ids = [r["id"] for r in billed_rows(store)]
    assert len(ids) == int(with_billed)
    persist_ctbc(payload(pending=[incoming], billed=[statement(ref="synthetic-b")]
                         if with_billed else []), store)
    assert [r["id"] for r in billed_rows(store)] == ids
    assert store.conn.execute("SELECT COUNT(*) FROM card_pending_txns").fetchone()[0] == 1


@pytest.mark.parametrize("with_billed", [False, True])
def test_changed_native_reference_discards_only_unsafe_pending_edit(store, with_billed):
    safe = unbilled(ref="synthetic-safe", description="SAFE SHOP", postingDt="00000000")
    unsafe = unbilled(postingDt="00000000")
    persist_ctbc(payload(pending=[unsafe, safe]), store)
    fields = ("category", "subcategory", "description_overwrite", "tags_overwrite",
              "auto_excluded", "splits_overwrite")
    overlay = ("旅遊", "訂閱", "safe note", '["safe"]', 1,
               '[{"amount":200,"category":"餐飲"},{"amount":300,"category":"購物"}]')
    store.conn.execute("UPDATE card_pending_txns SET " + ",".join(f"{field}=?" for field in fields), overlay)
    store.conn.commit()
    incoming = [unbilled(ref="synthetic-c", postingDt="00000000"), safe]
    billed = [statement(ref="synthetic-b")] if with_billed else []
    ids = None
    for _ in range(2):
        persist_ctbc(payload(pending=incoming, billed=billed), store)
        actual = [dict(r) for r in store.conn.execute("SELECT * FROM card_pending_txns").fetchall()]
        assert len(actual) == 2
        kept = next(r for r in actual if r["description"] == "SAFE SHOP")
        dropped = next(r for r in actual if r["description"] != "SAFE SHOP")
        assert tuple(kept[field] for field in fields) == overlay
        assert tuple(dropped[field] for field in fields) == (None, None, None, None, 0, None)
        assert [(r["amount"], r["currency"], r["consume_amount"], r["post_date"]) for r in actual] == [
            (500, "TWD", None, None), (500, "TWD", None, None)]
        current_ids = [r["id"] for r in billed_rows(store)]
        assert len(current_ids) == int(with_billed)
        if ids is not None:
            assert current_ids == ids
        ids = current_ids


@pytest.mark.parametrize("cleanup", ["rollback-before", "rollback-after", "release-after"])
def test_refresh_cleanup_failure_preserves_original_sql_error(store, monkeypatch, cleanup):
    from copy import deepcopy
    import sqlite3

    persist_ctbc(payload(pending=[unbilled(postingDt="00000000")]), store)
    real = store.conn
    before = [dict(r) for r in real.execute("SELECT * FROM card_pending_txns").fetchall()]
    names = ("_new_billed_ids", "_current_billed_ids", "_adopted_pending_raw_keys",
             "_unsafe_pending_overlay_identities")
    ledgers = {name: deepcopy(getattr(store, name)) for name in names}
    real.execute("CREATE TRIGGER fail_cleanup_probe BEFORE INSERT ON card_pending_txns "
                 "BEGIN SELECT RAISE(ABORT, 'synthetic insert failure'); END")
    real.commit()
    seen = []

    class Connection:
        def __getattr__(self, name):
            return getattr(real, name)

        def execute(self, sql, *args):
            target = ("RELEASE SAVEPOINT pending_overlay_attempt" if cleanup == "release-after"
                      else "ROLLBACK TO SAVEPOINT pending_overlay_attempt")
            if sql == target and cleanup == "rollback-before":
                raise OSError("synthetic cleanup failure")
            try:
                result = real.execute(sql, *args)
            except sqlite3.IntegrityError as error:
                seen.append(error)
                raise
            if sql == target:
                raise OSError("synthetic cleanup failure")
            return result

    monkeypatch.setattr(store, "conn", Connection())
    try:
        with pytest.raises(sqlite3.IntegrityError) as caught:
            store.refresh_card_pending("unbilled", [{"card_no": before[0]["card_no"],
                "date": "2026-09-10", "amount": 500, "desc": "NEW SHOP",
                "currency": "TWD", "txn_type": "spending", "booking_status": "unknown"}], fetch_ok=True)
        assert caught.value is seen[0]
        assert {name: getattr(store, name) for name in names} == ledgers
        if cleanup != "rollback-before":
            assert [dict(r) for r in real.execute("SELECT * FROM card_pending_txns").fetchall()] == before
    finally:
        real.rollback()


def test_shrinking_raw_group_keeps_unique_native_full_overlay(store):
    from backend.core.persist.ctbc import _ctbc_native_identity

    old = unbilled(postingDt="00000000")
    other = unbilled(ref="synthetic-b", postingDt="00000000")
    persist_ctbc(payload(pending=[old, other]), store)
    identity = _ctbc_native_identity(old, "1234_0", store)
    store.conn.execute("UPDATE card_pending_txns SET category='旅遊', subcategory='交通', "
                       "description_overwrite='keep note', tags_overwrite='[\"keep\"]', "
                       "auto_excluded=1, splits_overwrite='[{\"amount\":200},{\"amount\":300}]' "
                       "WHERE native_identity=?", (identity,))
    store.conn.execute("UPDATE card_pending_txns SET description_overwrite='discard note' "
                       "WHERE native_identity<>?", (identity,))
    store.conn.commit()
    columns = ("category", "subcategory", "description_overwrite", "tags_overwrite",
               "auto_excluded", "splits_overwrite")
    original = dict(store.conn.execute("SELECT * FROM card_pending_txns WHERE native_identity=?",
                                      (identity,)).fetchone())
    for _ in range(2):
        persist_ctbc(payload(pending=[old]), store)
        actual = [dict(r) for r in store.conn.execute("SELECT * FROM card_pending_txns").fetchall()]
        assert len(actual) == 1
        assert actual[0]["native_identity"] == identity
        assert tuple(actual[0][c] for c in columns) == tuple(original[c] for c in columns)


@pytest.mark.parametrize("reverse", [False, True])
def test_unique_native_match_survives_new_reference_with_same_raw_key(store, reverse):
    from backend.core.persist.ctbc import _ctbc_native_identity

    old = unbilled(postingDt="00000000")
    new = unbilled(ref="synthetic-c", postingDt="00000000")
    persist_ctbc(payload(pending=[old]), store)
    store.conn.execute("UPDATE card_pending_txns SET description_overwrite='keep matched note'")
    store.conn.commit()
    incoming = [new, old] if reverse else [old, new]
    for _ in range(2):
        persist_ctbc(payload(pending=incoming), store)
        actual = [dict(r) for r in store.conn.execute("SELECT * FROM card_pending_txns").fetchall()]
        assert len(actual) == 2
        kept = next(r for r in actual if r["native_identity"] == _ctbc_native_identity(old, "1234_0", store))
        assert kept["description_overwrite"] == "keep matched note"
        assert next(r for r in actual if r["id"] != kept["id"])["description_overwrite"] is None


@pytest.mark.parametrize("unsafe_case", ["occurrence", "multiple", "missing-reference", "adoption-multiple", "adoption-split", "adoption-conflict"])
def test_native_overlay_refusal_keeps_independent_safe_pending_metadata(store, unsafe_case):
    from backend.core.persist.ctbc import _ctbc_native_identity

    safe = unbilled(ref="synthetic-safe", description="SAFE SHOP", postingDt="00000000")
    unsafe = unbilled(postingDt="00000000")
    persist_ctbc(payload(pending=[unsafe, safe]), store)
    store.conn.execute("UPDATE card_pending_txns SET description_overwrite='keep note'")
    if unsafe_case == "adoption-split":
        store.conn.execute("UPDATE card_pending_txns SET splits_overwrite='bad json' WHERE description != 'SAFE SHOP'")
    store.conn.commit()
    unsafe_new = unbilled(postingDt="00000000")
    if unsafe_case == "occurrence":
        unsafe_new["purchaseAmt"] = 600
    if unsafe_case == "missing-reference":
        unsafe_new.update(acwRefNbr=None, description="RENAMED")
    incoming = [unsafe_new] * (2 if unsafe_case == "multiple" else 1) + [safe]
    billed = []
    if unsafe_case.startswith("adoption-"):
        incoming = [safe]
        billed = [statement()] * (2 if unsafe_case == "adoption-multiple" else 1)
        if unsafe_case == "adoption-conflict":
            persist_ctbc(payload(pending=[unsafe, safe], billed=billed), store)
            store.conn.execute("UPDATE card_billed_txns SET description_overwrite='existing note'")
            store.conn.commit()
    for _ in range(2):
        persist_ctbc(payload(pending=incoming, billed=billed), store)
        actual = [dict(r) for r in store.conn.execute("SELECT * FROM card_pending_txns").fetchall()]
        kept = next(r for r in actual if r["description"] == "SAFE SHOP")
        assert kept["description_overwrite"] == "keep note"
        assert kept["native_identity"] == _ctbc_native_identity(safe, "1234_0", store)
        assert all(r["description_overwrite"] is None for r in actual if r["description"] != "SAFE SHOP")
        assert len(actual) == len(incoming)
        assert len(billed_rows(store)) == len(billed)


@pytest.mark.parametrize("failure", ["insert", "commit", "commit-booked", "commit-cleanup"])
def test_fallback_cross_scope_mutation_keeps_pristine_rollback_snapshot(store, failure, monkeypatch):
    from copy import deepcopy
    import sqlite3
    from backend.core.persist.ctbc import _ctbc_native_identity

    persist_ctbc(payload(pending=[unbilled(postingDt="00000000")]), store)
    store.conn.execute("UPDATE card_pending_txns SET description_overwrite='old note'")
    columns = [r["name"] for r in store.conn.execute("PRAGMA table_info(card_pending_txns)").fetchall()
               if r["name"] != "id"]
    expressions = ["?" if name in ("scope", "description_overwrite") else f'"{name}"' for name in columns]
    for scope, note in (("current", "other edit"), ("realtime", "different edit")):
        values = [scope if name == "scope" else note for name in columns
                  if name in ("scope", "description_overwrite")]
        store.conn.execute(f"INSERT INTO card_pending_txns ({','.join(columns)}) "
                           f"SELECT {','.join(expressions)} FROM card_pending_txns "
                           "WHERE user_id=? AND scope='unbilled'", (*values, store.user_id))
    store.conn.commit()
    before_rows = [dict(r) for r in store.conn.execute("SELECT * FROM card_pending_txns ORDER BY id").fetchall()]
    old = before_rows[0]
    incoming = {"card_no": old["card_no"], "date": old["consume_date"], "desc": old["description"],
                "amount": old["amount"], "currency": old["currency"],
                "consume_currency": old["consume_currency"], "consume_amount": old["consume_amount"],
                "txn_type": old["txn_type"], "booking_status": "unknown", "post_date": None,
                "native_identity": _ctbc_native_identity(unbilled(ref="synthetic-c"), old["card_no"], store)}
    names = ("_new_billed_ids", "_current_billed_ids", "_adopted_pending_raw_keys", "_unsafe_pending_overlay_identities")
    before = {name: deepcopy(getattr(store, name)) for name in names}
    reader = None
    seen = []
    if failure == "insert":
        store.conn.execute("CREATE TRIGGER reject_replacement BEFORE INSERT ON card_pending_txns "
                           "BEGIN SELECT RAISE(ABORT, 'synthetic replacement failure'); END")
        store.conn.commit()
        error_type = sqlite3.IntegrityError
    else:
        assert store.conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
        store.conn.execute("PRAGMA busy_timeout=0")
        path = store.conn.execute("PRAGMA database_list").fetchone()[2]
        reader = sqlite3.connect(path, timeout=0)
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM card_pending_txns").fetchall()
        original = store.commit
        def commit():
            try:
                original()
            except sqlite3.OperationalError as error:
                seen.append(error)
                raise
        monkeypatch.setattr(store, "commit", commit)
        if failure == "commit-cleanup":
            original_rollback = store.rollback
            def rollback():
                original_rollback()
                raise OSError("synthetic rollback observer failure")
            monkeypatch.setattr(store, "rollback", rollback)
        error_type = sqlite3.OperationalError
    try:
        with pytest.raises(error_type) as caught:
            txns = [incoming]
            if failure == "commit-booked":
                txns.append({**incoming, "desc": "OTHER SHOP", "booking_status": "booked",
                             "post_date": "2026-09-12", "native_identity": _ctbc_native_identity(
                                 unbilled(ref="synthetic-b"), old["card_no"], store)})
            store.refresh_card_pending("unbilled", txns, fetch_ok=True)
        if failure in ("commit", "commit-cleanup"):
            assert caught.value is seen[0]
        assert [dict(r) for r in store.conn.execute("SELECT * FROM card_pending_txns ORDER BY id").fetchall()] == before_rows
        assert billed_rows(store) == []
        assert {name: getattr(store, name) for name in names} == before
    finally:
        if reader is not None:
            reader.close()


def test_direct_refresh_insert_failure_restores_adoption_ledger(store):
    from copy import deepcopy
    import sqlite3
    from backend.core.persist.ctbc import _ctbc_native_identity

    old = unbilled(postingDt="00000000")
    persist_ctbc(payload(pending=[old]), store)
    store.conn.execute("UPDATE card_pending_txns SET description_overwrite='old note'")
    store.conn.commit()
    persist_ctbc(payload(pending=[old], billed=[statement()]), store)
    before_pending = [dict(r) for r in store.conn.execute("SELECT * FROM card_pending_txns").fetchall()]
    before_billed = billed_rows(store)
    names = ("_new_billed_ids", "_current_billed_ids", "_adopted_pending_raw_keys", "_unsafe_pending_overlay_identities")
    before = {name: deepcopy(getattr(store, name)) for name in names}
    incoming = {"card_no": "****1234", "date": "2026-09-10", "desc": "NEW SHOP", "amount": 500,
                "currency": "TWD", "consume_currency": "TWD", "consume_amount": 500,
                "booking_status": "unknown", "post_date": None, "txn_type": "expense",
                "native_identity": _ctbc_native_identity(unbilled(ref="synthetic-c"), "****1234", store)}
    store.conn.execute("CREATE TRIGGER reject_replacement BEFORE INSERT ON card_pending_txns "
                       "BEGIN SELECT RAISE(ABORT, 'synthetic replacement failure'); END")
    store.conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="synthetic replacement failure"):
        store.refresh_card_pending("unbilled", [incoming], fetch_ok=True)
    assert [dict(r) for r in store.conn.execute("SELECT * FROM card_pending_txns").fetchall()] == before_pending
    assert billed_rows(store) == before_billed
    assert {name: getattr(store, name) for name in names} == before


@pytest.mark.parametrize("operation", ["DELETE", "INSERT"])
def test_pending_replacement_failure_rolls_back_sql_and_ledgers(store, operation):
    from copy import deepcopy
    import sqlite3

    persist_ctbc(payload(pending=[unbilled(postingDt="00000000")]), store)
    store.conn.execute("UPDATE card_pending_txns SET description_overwrite='old note'")
    store.conn.commit()
    before_pending = [dict(r) for r in store.conn.execute("SELECT * FROM card_pending_txns").fetchall()]
    names = ("_new_billed_ids", "_current_billed_ids", "_adopted_pending_raw_keys", "_unsafe_pending_overlay_identities")
    before = {name: deepcopy(getattr(store, name)) for name in names}
    store.conn.execute(f"CREATE TRIGGER reject_replacement BEFORE {operation} ON card_pending_txns "
                       "BEGIN SELECT RAISE(ABORT, 'synthetic replacement failure'); END")
    store.conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="synthetic replacement failure"):
        persist_ctbc(payload(pending=[unbilled(ref="synthetic-c", postingDt="00000000")],
                             billed=[statement(ref="synthetic-b")]), store)
    assert [dict(r) for r in store.conn.execute("SELECT * FROM card_pending_txns").fetchall()] == before_pending
    assert billed_rows(store) == []
    assert {name: getattr(store, name) for name in names} == before


def test_pending_native_rename_keeps_overlay_before_booking(store):
    persist_ctbc(payload(pending=[unbilled(postingDt="00000000")]), store)
    store.conn.execute("UPDATE card_pending_txns SET description_overwrite='keep note'")
    store.conn.commit()
    persist_ctbc(payload(pending=[unbilled(description="RENAMED", postingDt="00000000")]), store)
    assert store.conn.execute("SELECT description_overwrite FROM card_pending_txns").fetchone()[0] == "keep note"


@pytest.mark.parametrize("bad_overlay", ["conflict", "invalid-splits", "ambiguous-repeats"])
def test_native_statement_uses_new_rows_when_pending_edits_cannot_be_adopted(store, bad_overlay):
    pending = unbilled(postingDt="00000000")
    persist_ctbc(payload(pending=[pending] * (2 if bad_overlay == "ambiguous-repeats" else 1)), store)
    store.conn.execute("UPDATE card_pending_txns SET description_overwrite='keep note'")
    if bad_overlay == "invalid-splits":
        store.conn.execute("UPDATE card_pending_txns SET splits_overwrite='invalid'")
    if bad_overlay == "conflict":
        persist_ctbc(payload(pending=[pending], billed=[statement()]), store)
        store.conn.execute("UPDATE card_billed_txns SET description_overwrite='existing edit'")
    store.conn.commit()
    persist_ctbc(payload(billed=[statement()]), store)
    billed = billed_rows(store)
    assert len(billed) == 1
    assert billed[0]["description_overwrite"] == ("existing edit" if bad_overlay == "conflict" else None)
    assert billed[0]["splits_overwrite"] is None
    assert store.conn.execute("SELECT COUNT(*) FROM card_pending_txns").fetchone()[0] == 0
    persist_ctbc(payload(billed=[statement()]), store)
    assert [r["id"] for r in billed_rows(store)] == [billed[0]["id"]]


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


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_owned_ctbc_batch_busy_commit_restores_sql_and_all_four_ledgers(store, monkeypatch, cleanup_fails):
    from collections import Counter
    from copy import deepcopy
    import sqlite3

    fields = ("category", "subcategory", "description_overwrite", "tags_overwrite",
              "auto_excluded", "splits_overwrite")
    overlay = ("旅遊", "交通", "owned note", '["owned"]', 1,
               '[{"amount":200},{"amount":300}]')
    persist_ctbc(payload(pending=[unbilled(postingDt="00000000")]), store)
    store.conn.execute("UPDATE card_pending_txns SET " + ",".join(f"{f}=?" for f in fields), overlay)
    store.conn.commit()
    store._new_billed_ids = [71]
    store._current_billed_ids = [72]
    store._adopted_pending_raw_keys = {("seed",): {"current": Counter({("raw",): 1})}}
    store._unsafe_pending_overlay_identities = {("pristine",)}
    names = ("_new_billed_ids", "_current_billed_ids", "_adopted_pending_raw_keys",
             "_unsafe_pending_overlay_identities")
    pristine = {name: deepcopy(getattr(store, name)) for name in names}
    assert store.conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    store.conn.execute("PRAGMA busy_timeout=0")
    reader = sqlite3.connect(store.db_path, timeout=0)
    before_sql = []
    seen = []
    batch = store.persist_card_batch
    commit = store.commit
    rollback = store.rollback

    def observe_batch(*args, **kwargs):
        before_sql.extend(store.conn.iterdump())
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM card_pending_txns").fetchall()
        return batch(*args, **kwargs)

    def observe_commit():
        actual = billed_rows(store)
        assert len(actual) == 1 and tuple(actual[0][f] for f in fields) == overlay
        assert store.conn.execute("SELECT COUNT(*) FROM card_pending_txns").fetchone()[0] == 0
        assert store._new_billed_ids and store._current_billed_ids
        assert store._adopted_pending_raw_keys != pristine["_adopted_pending_raw_keys"]
        try:
            commit()
        except sqlite3.OperationalError as error:
            seen.append(error)
            raise

    def observe_rollback():
        rollback()
        if cleanup_fails:
            raise OSError("synthetic post-rollback cleanup failure")

    monkeypatch.setattr(store, "persist_card_batch", observe_batch)
    monkeypatch.setattr(store, "commit", observe_commit)
    monkeypatch.setattr(store, "rollback", observe_rollback)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked") as caught:
            persist_ctbc(payload(billed=[statement()]), store)
        assert len(seen) == 1 and caught.value is seen[0]
        assert list(store.conn.iterdump()) == before_sql
        assert {name: getattr(store, name) for name in names} == pristine
    finally:
        reader.close()
        store.conn.rollback()


@pytest.mark.parametrize("fx", [False, True])
@pytest.mark.parametrize("incoming_weak", [False, True])
@pytest.mark.parametrize("old_reverse", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("other_raw", [False, True])
@pytest.mark.parametrize("booked", [(False, False), (True, True), (True, False), (False, True)])
def test_unsafe_legacy_sibling_cannot_taint_or_steal_unique_native_overlay(
        store, fx, incoming_weak, old_reverse, reverse, other_raw, booked):
    from backend.core.persist.ctbc import _ctbc_native_identity

    fields = ("category", "subcategory", "description_overwrite", "tags_overwrite",
              "auto_excluded", "splits_overwrite")
    overlay = ("旅遊", "交通", "native note", '["native"]', 1,
               '[{"amount":40,"category":"餐飲"},{"amount":60,"category":"購物"}]')
    changes = {"purchaseAmt": 100, "postingDt": "00000000"}
    if fx:
        changes.update(origCurDesc="USD", origCurAmt=3)
    strong = unbilled(**changes)
    weak = unbilled(ref=None, **changes)
    persist_ctbc(payload(pending=[strong, weak] if old_reverse else [weak, strong]), store)
    identity = _ctbc_native_identity(strong, "1234_0", store)
    store.conn.execute("UPDATE card_pending_txns SET " + ",".join(f"{f}=?" for f in fields)
                       + " WHERE native_identity=?", (*overlay, identity))
    store.conn.execute("UPDATE card_pending_txns SET description_overwrite='unsafe legacy note' "
                       "WHERE native_identity IS NULL")
    store.conn.commit()
    incoming = [unbilled(ref=ref, **{**changes, "postingDt": "20260912" if status else "00000000"})
                for ref, status in zip(("synthetic-a", None if incoming_weak else "synthetic-b"), booked, strict=True)]
    if other_raw:
        incoming[1]["description"] = "OTHER SHOP"
    if reverse:
        incoming.reverse()
    ids = None
    for _ in range(2):
        persist_ctbc(payload(pending=incoming), store)
        pending_rows = [dict(r) for r in store.conn.execute("SELECT * FROM card_pending_txns").fetchall()]
        actual = [*billed_rows(store), *pending_rows]
        assert len(actual) == 2
        kept = next(r for r in actual if r["native_identity"] == identity)
        other = next(r for r in actual if r["native_identity"] != identity)
        assert tuple(kept[f] for f in fields) == overlay
        assert tuple(other[f] for f in fields) == (None, None, None, None, 0, None)
        assert other["native_identity"] == _ctbc_native_identity(
            unbilled(ref=None if incoming_weak else "synthetic-b"), "1234_0", store)
        for row, status in ((kept, booked[0]), (other, booked[1])):
            assert (row["card_no"], row["consume_date"], row["description"], row["amount"],
                    row["currency"], row["consume_currency"], row["consume_amount"], row["post_date"]) == (
                        "****1234", "2026-09-10", "OTHER SHOP" if row is other and other_raw else "SHOP TAIPEI CITY TW", 100, "TWD",
                        "USD" if fx else None, 3 if fx else None, "2026-09-12" if status else None)
        assert len(pending_rows) == booked.count(False)
        current_ids = [r["id"] for r in billed_rows(store)]
        if ids is not None:
            assert current_ids == ids
        ids = current_ids


def test_nullable_reference_fx_statement_preserves_unique_six_field_overlay(store):
    fields = ("category", "subcategory", "description_overwrite", "tags_overwrite",
              "auto_excluded", "splits_overwrite")
    overlay = ("旅遊", "交通", "FX note", '["fx"]', 1,
               '[{"amount":40,"category":"餐飲"},{"amount":60,"category":"購物"}]')
    old = unbilled(ref=None, postingDt="00000000", purchaseAmt=100,
                   origCurDesc="USD", origCurAmt=3, description="SHOP CITY")
    persist_ctbc(payload(pending=[old]), store)
    store.conn.execute("UPDATE card_pending_txns SET " + ",".join(f"{f}=?" for f in fields), overlay)
    store.conn.commit()
    incoming = statement(ref=None, ntAmt=150, occCurCode="USD", foreignAmt="3")
    ids = None
    for _ in range(2):
        persist_ctbc(payload(billed=[incoming]), store)
        actual = billed_rows(store)
        assert len(actual) == 1
        assert tuple(actual[0][f] for f in fields[:-1]) == overlay[:-1]
        assert actual[0]["splits_overwrite"] == '[{"amount":60,"category":"餐飲"},{"amount":90,"category":"購物"}]'
        assert (actual[0]["native_identity"], actual[0]["description"], actual[0]["amount"],
                actual[0]["currency"], actual[0]["consume_currency"], actual[0]["consume_amount"],
                actual[0]["consume_date"], actual[0]["post_date"], actual[0]["bill_date"]) == (
                    None, "SHOP", 150, "TWD", "USD", 3, "2026-09-10", "2026-09-12", "2026-09-01")
        assert store.conn.execute("SELECT COUNT(*) FROM card_pending_txns").fetchone()[0] == 0
        current_ids = [r["id"] for r in actual]
        if ids is not None:
            assert current_ids == ids
        ids = current_ids


def test_known_reference_conflict_never_reuses_exact_pending_overlay(store):
    persist_ctbc(payload(pending=[unbilled(postingDt="00000000")]), store)
    store.conn.execute("UPDATE card_pending_txns SET description_overwrite='private edit'")
    store.conn.commit()
    persist_ctbc(payload(pending=[unbilled(ref="synthetic-b", postingDt="00000000")]), store)
    assert store.conn.execute("SELECT description_overwrite FROM card_pending_txns").fetchone()[0] is None


@pytest.mark.parametrize("evidence", ["native", "fx"])
@pytest.mark.parametrize("weak_edited", [False, True])
@pytest.mark.parametrize("old_reverse", [False, True])
@pytest.mark.parametrize("mode", ["pending", "mixed", "promotion"])
def test_renamed_strong_donor_wins_over_old_weak_exact_key(
        store, evidence, weak_edited, old_reverse, mode):
    from backend.core.persist.ctbc import _ctbc_native_identity

    fields = ("category", "subcategory", "description_overwrite", "tags_overwrite",
              "auto_excluded", "splits_overwrite")
    overlay = ("旅遊", "交通", "strong note", '["strong"]', 1,
               '[{"amount":40,"category":"餐飲"},{"amount":60,"category":"購物"}]')
    strong = unbilled(ref="synthetic-a" if evidence == "native" else None,
                      postingDt="00000000", purchaseAmt=100, description="OLD SHOP")
    if evidence == "fx":
        strong.update(origCurDesc="USD", origCurAmt=3)
    weak = unbilled(ref=None, postingDt="00000000", purchaseAmt=100, description="NEW SHOP")
    persist_ctbc(payload(pending=[strong, weak] if old_reverse else [weak, strong]), store)
    strong_id = store.conn.execute("SELECT id FROM card_pending_txns WHERE description='OLD SHOP'").fetchone()[0]
    store.conn.execute("UPDATE card_pending_txns SET " + ",".join(f"{f}=?" for f in fields)
                       + " WHERE id=?", (*overlay, strong_id))
    if weak_edited:
        store.conn.execute("UPDATE card_pending_txns SET description_overwrite='weak note' WHERE id<>?", (strong_id,))
    store.conn.commit()
    incoming = {**strong, "description": "NEW SHOP",
                "postingDt": "20260912" if mode == "promotion" else "00000000"}
    unrelated = unbilled(ref="synthetic-unrelated", purchaseAmt=200, description="UNRELATED SHOP")
    billed_ids = None
    for _ in range(2):
        persist_ctbc(payload(pending=[incoming, unrelated] if mode == "mixed" else [incoming]), store)
        pending = [dict(r) for r in store.conn.execute("SELECT * FROM card_pending_txns").fetchall()]
        billed = billed_rows(store)
        actual = [*pending, *billed]
        assert len(actual) == (2 if mode == "mixed" else 1)
        kept = next(r for r in actual if r["description"] == "NEW SHOP")
        assert tuple(kept[f] for f in fields) == overlay
        assert (kept["native_identity"], kept["card_no"], kept["consume_date"], kept["amount"],
                kept["currency"], kept["consume_currency"], kept["consume_amount"], kept["post_date"]) == (
                    _ctbc_native_identity(strong, "1234_0", store), "****1234", "2026-09-10", 100,
                    "TWD", "USD" if evidence == "fx" else None, 3 if evidence == "fx" else None,
                    "2026-09-12" if mode == "promotion" else None)
        if mode == "mixed":
            assert tuple(next(r for r in actual if r is not kept)[f] for f in fields) == (None, None, None, None, 0, None)
        assert len(pending) == (0 if mode == "promotion" else 1)
        ids = [r["id"] for r in billed]
        if billed_ids is not None:
            assert ids == billed_ids
        billed_ids = ids


@pytest.mark.parametrize("old_reverse", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("weak_edited", [False, True])
def test_vanished_weak_fx_donor_sees_native_statement_before_assignment(
        store, old_reverse, reverse, weak_edited):
    from backend.core.persist.ctbc import _ctbc_native_identity

    fields = ("category", "subcategory", "description_overwrite", "tags_overwrite",
              "auto_excluded", "splits_overwrite")
    overlay = ("旅遊", "交通", "native note", '["native"]', 1,
               '[{"amount":40,"category":"餐飲"},{"amount":60,"category":"購物"}]')
    strong = unbilled(postingDt="00000000", purchaseAmt=100, origCurDesc="USD", origCurAmt=3)
    weak = {**strong, "acwRefNbr": None}
    persist_ctbc(payload(pending=[strong, weak] if old_reverse else [weak, strong]), store)
    identity = _ctbc_native_identity(strong, "1234_0", store)
    store.conn.execute("UPDATE card_pending_txns SET " + ",".join(f"{f}=?" for f in fields)
                       + " WHERE native_identity=?", (*overlay, identity))
    if weak_edited:
        store.conn.execute("UPDATE card_pending_txns SET description_overwrite='unsafe weak FX note' WHERE native_identity IS NULL")
    store.conn.commit()
    statements = [statement(ntAmt=100, occCurCode="USD", foreignAmt="3"),
                  statement(ref=None, ntAmt=100, occCurCode="USD", foreignAmt="3")]
    if reverse:
        statements.reverse()
    ids = None
    for _ in range(2):
        persist_ctbc(payload(billed=statements), store)
        actual = billed_rows(store)
        assert len(actual) == 2
        kept = next(r for r in actual if r["native_identity"] == identity)
        other = next(r for r in actual if r["native_identity"] is None)
        assert tuple(kept[f] for f in fields) == overlay
        assert tuple(other[f] for f in fields) == (None, None, None, None, 0, None)
        assert [(r["amount"], r["currency"], r["consume_currency"], r["consume_amount"],
                 r["consume_date"], r["post_date"], r["bill_date"]) for r in actual] == [
                     (100, "TWD", "USD", 3, "2026-09-10", "2026-09-12", "2026-09-01")] * 2
        assert store.conn.execute("SELECT COUNT(*) FROM card_pending_txns").fetchone()[0] == 0
        current_ids = [r["id"] for r in actual]
        if ids is not None:
            assert current_ids == ids
        ids = current_ids
@pytest.mark.parametrize("reverse", [False, True])
def test_historical_native_target_cannot_fabricate_missing_source_reference(store, reverse):
    fields = ("category", "subcategory", "description_overwrite", "tags_overwrite",
              "auto_excluded", "splits_overwrite")
    overlay = ("旅遊", "交通", "native note", '["native"]', 1,
               '[{"amount":40},{"amount":60}]')
    historical = unbilled(purchaseAmt=100, description="NEW SHOP")
    persist_ctbc(payload(pending=[historical]), store)
    original = billed_rows(store)[0]
    persist_ctbc(payload(pending=[unbilled(purchaseAmt=100, description="OLD SHOP",
                                         postingDt="00000000")]), store)
    store.conn.execute("UPDATE card_pending_txns SET " + ",".join(f"{f}=?" for f in fields), overlay)
    store.conn.commit()
    incoming = [unbilled(ref=None, purchaseAmt=100, description="NEW SHOP"),
                unbilled(purchaseAmt=100, description="RENAMED SHOP", postingDt="00000000")]
    if reverse:
        incoming.reverse()
    for _ in range(2):
        persist_ctbc(payload(pending=incoming), store)
        pending = [dict(r) for r in store.conn.execute("SELECT * FROM card_pending_txns").fetchall()]
        assert len(pending) == 1
        assert tuple(pending[0][f] for f in fields) == overlay
        assert pending[0]["native_identity"] == original["native_identity"]
        assert pending[0]["description"] == "RENAMED SHOP" and pending[0]["post_date"] is None
        assert billed_rows(store) == [original]
@pytest.mark.parametrize("old_ref,new_ref", [(None, None), (None, "synthetic-a"),
    ("synthetic-a", None), ("synthetic-a", "synthetic-a"), ("synthetic-a", "synthetic-b")])
@pytest.mark.parametrize("amount", [100, 150])
def test_nullable_fx_reference_transition_preserves_pending_overlay(store, old_ref, new_ref, amount):
    fields = ("category", "subcategory", "description_overwrite", "tags_overwrite",
              "auto_excluded", "splits_overwrite")
    overlay = ("旅遊", "交通", "FX note", '["fx"]', 1, '[{"amount":40},{"amount":60}]')
    persist_ctbc(payload(pending=[unbilled(ref=old_ref, postingDt="00000000", purchaseAmt=100,
        origCurDesc="USD", origCurAmt=3, description="OLD SHOP")]), store)
    store.conn.execute("UPDATE card_pending_txns SET " + ",".join(f"{f}=?" for f in fields), overlay)
    store.conn.commit()
    incoming = unbilled(ref=new_ref, postingDt="00000000", purchaseAmt=amount,
                        origCurDesc="USD", origCurAmt=3, description="NEW SHOP")
    for _ in range(2):
        persist_ctbc(payload(pending=[incoming]), store)
        actual = dict(store.conn.execute("SELECT * FROM card_pending_txns").fetchone())
        expected = (None, None, None, None, 0, None) if old_ref and new_ref and old_ref != new_ref else (
            *overlay[:-1], overlay[-1] if amount == 100 else '[{"amount":60},{"amount":90}]')
        assert tuple(actual[f] for f in fields) == expected
        assert (actual["description"], actual["amount"], actual["consume_currency"], actual["consume_amount"],
                actual["post_date"]) == ("NEW SHOP", amount, "USD", 3, None)
        assert billed_rows(store) == []
@pytest.mark.parametrize("old_ref,new_ref", [(None, None), (None, "synthetic-a"),
    ("synthetic-a", None), ("synthetic-a", "synthetic-a"), ("synthetic-a", "synthetic-b")])
@pytest.mark.parametrize("amount", [100, 150])
@pytest.mark.parametrize("mode", ["statement", "promotion"])
def test_nullable_fx_reference_transition_preserves_billed_overlay(store, old_ref, new_ref, amount, mode):
    fields = ("category", "subcategory", "description_overwrite", "tags_overwrite",
              "auto_excluded", "splits_overwrite")
    overlay = ("旅遊", "交通", "FX note", '["fx"]', 1, '[{"amount":40},{"amount":60}]')
    persist_ctbc(payload(pending=[unbilled(ref=old_ref, postingDt="00000000", purchaseAmt=100,
        origCurDesc="USD", origCurAmt=3, description="OLD SHOP")]), store)
    store.conn.execute("UPDATE card_pending_txns SET " + ",".join(f"{f}=?" for f in fields), overlay)
    store.conn.commit()
    incoming = (payload(billed=[statement(ref=new_ref, ntAmt=amount, occCurCode="USD", foreignAmt="3",
                                         merchantChiName="NEW SHOP")]) if mode == "statement" else
                payload(pending=[unbilled(ref=new_ref, purchaseAmt=amount, origCurDesc="USD", origCurAmt=3,
                                         description="NEW SHOP")]))
    ids = None
    for _ in range(2):
        persist_ctbc(incoming, store)
        actual = billed_rows(store)
        assert len(actual) == 1
        expected = (None, None, None, None, 0, None) if old_ref and new_ref and old_ref != new_ref else (
            *overlay[:-1], overlay[-1] if amount == 100 else '[{"amount":60},{"amount":90}]')
        assert tuple(actual[0][f] for f in fields) == expected
        assert (actual[0]["description"], actual[0]["amount"], actual[0]["consume_currency"],
                actual[0]["consume_amount"], actual[0]["post_date"]) == ("NEW SHOP", amount, "USD", 3, "2026-09-12")
        assert store.conn.execute("SELECT COUNT(*) FROM card_pending_txns").fetchone()[0] == 0
        if ids is not None:
            assert [r["id"] for r in actual] == ids
        ids = [r["id"] for r in actual]
@pytest.mark.parametrize("mode", ["pending", "promotion", "statement"])
@pytest.mark.parametrize("old_ref,new_ref", [(None, "synthetic-a"), ("synthetic-a", None)])
@pytest.mark.parametrize("case", ["duplicate", "zero-original", "missing-original", "currency-veto"])
def test_nullable_fx_transition_never_guesses_unpairable_overlay(store, mode, old_ref, new_ref, case):
    fields = ("category", "subcategory", "description_overwrite", "tags_overwrite",
              "auto_excluded", "splits_overwrite")
    overlay = ("旅遊", "交通", "FX note", '["fx"]', 1, '[{"amount":40},{"amount":60}]')
    old = unbilled(ref=old_ref, postingDt="00000000", purchaseAmt=100,
                   origCurDesc="USD", origCurAmt=3, description="OLD SHOP")
    persist_ctbc(payload(pending=[old]), store)
    store.conn.execute("UPDATE card_pending_txns SET " + ",".join(f"{f}=?" for f in fields), overlay)
    store.conn.commit()
    currency = "EUR" if case == "currency-veto" else "USD"
    original = 0 if case == "zero-original" else None if case == "missing-original" else 3
    count = 2 if case == "duplicate" else 1
    incoming = (payload(billed=[statement(ref=new_ref, ntAmt=100, occCurCode=currency, foreignAmt=original,
                                         merchantChiName="NEW SHOP")] * count) if mode == "statement" else
                payload(pending=[unbilled(ref=new_ref, purchaseAmt=100, origCurDesc=currency, origCurAmt=original,
                    description="NEW SHOP", postingDt="00000000" if mode == "pending" else "20260912")] * count))
    before = list(store.conn.iterdump())
    if case == "zero-original" and new_ref and mode != "statement":
        with pytest.raises(ValueError, match="incomplete native card occurrence"):
            persist_ctbc(incoming, store)
        assert list(store.conn.iterdump()) == before
        return  # The native producer preserves zero; validation refuses incomplete evidence.
    for _ in range(2):
        persist_ctbc(incoming, store)
        pending = [dict(r) for r in store.conn.execute("SELECT * FROM card_pending_txns").fetchall()]
        actual = [*pending, *billed_rows(store)]
        assert len(actual) == count
        assert all(tuple(r[f] for f in fields) == (None, None, None, None, 0, None) for r in actual)
        assert all(r["description"] == "NEW SHOP" for r in actual)


@pytest.mark.parametrize("old_reverse", [False, True])
@pytest.mark.parametrize("weak_edited", [False, True])
@pytest.mark.parametrize("independent", [False, True])
def test_one_null_statement_cannot_choose_native_or_fx_donor(store, old_reverse, weak_edited, independent):
    fields = ("category", "subcategory", "description_overwrite", "tags_overwrite",
              "auto_excluded", "splits_overwrite")
    overlay = ("旅遊", "交通", "original note", '["original"]', 1,
               '[{"amount":40},{"amount":60}]')
    strong = unbilled(postingDt="00000000", purchaseAmt=100, origCurDesc="USD",
                      origCurAmt=3, description="OLD A")
    weak = {**strong, "acwRefNbr": None, "description": "OLD W"}
    safe = unbilled(ref="independent", postingDt="00000000", purchaseAmt=100,
                    purchaseDt="20260911", description="SAFE")
    persist_ctbc(payload(pending=([weak, strong] if old_reverse else [strong, weak])
                         + ([safe] if independent else [])), store)
    store.conn.execute("UPDATE card_pending_txns SET " + ",".join(f"{f}=?" for f in fields)
                       + " WHERE native_identity IS NOT NULL", overlay)
    if weak_edited:
        store.conn.execute("UPDATE card_pending_txns SET " + ",".join(f"{f}=?" for f in fields)
                           + " WHERE native_identity IS NULL",
                           ("其他", "電商", "conflicting note", '["weak"]', 1,
                            '[{"amount":30},{"amount":70}]'))
    store.conn.commit()
    original = {r["description"]: tuple(r[f] for f in fields) for r in
                store.conn.execute("SELECT * FROM card_pending_txns")}
    assert original["OLD A"] == overlay
    if independent:
        assert original["SAFE"] == overlay
    incoming = payload(billed=[statement(ref=None, ntAmt=100, occCurCode="USD",
                                         foreignAmt="3", merchantChiName="NEW"),
                              *([statement(ref="independent", ntAmt=100, purchaseDt="091126",
                                           merchantChiName="SAFE")] if independent else [])])
    ids = None
    for _ in range(2):
        persist_ctbc(incoming, store)
        actual = billed_rows(store)
        assert len(actual) == (2 if independent else 1)
        if independent:
            assert tuple(next(r for r in actual if r["description"] == "SAFE")[f] for f in fields) == original["SAFE"]
        target = next(r for r in actual if r["description"] == "NEW")
        assert tuple(target[f] for f in fields) == (None, None, None, None, 0, None)
        assert (target["native_identity"], target["amount"], target["currency"],
                target["consume_currency"], target["consume_amount"], target["consume_date"],
                target["post_date"], target["bill_date"]) == (
                    None, 100, "TWD", "USD", 3, "2026-09-10", "2026-09-12", "2026-09-01")
        assert store.conn.execute("SELECT COUNT(*) FROM card_pending_txns").fetchone()[0] == 0
        if ids is not None:
            assert [r["id"] for r in actual] == ids
        ids = [r["id"] for r in actual]


@pytest.mark.parametrize("old_reverse", [False, True])
@pytest.mark.parametrize("other_edited", [False, True])
@pytest.mark.parametrize("mode", ["pending", "promotion", "statement"])
def test_different_raw_same_native_contraction_never_uses_exact_cache(
        store, old_reverse, other_edited, mode):
    fields = ("category", "subcategory", "description_overwrite", "tags_overwrite",
              "auto_excluded", "splits_overwrite")
    overlay = ("旅遊", "交通", "original note", '["original"]', 1,
               '[{"amount":40},{"amount":60}]')
    old = unbilled(postingDt="00000000", purchaseAmt=100, description="A")
    other = {**old, "description": "B"}
    safe = unbilled(ref="independent", postingDt="00000000", purchaseAmt=100,
                    purchaseDt="20260911", description="SAFE")
    persist_ctbc(payload(pending=([other, old] if old_reverse else [old, other]) + [safe]), store)
    store.conn.execute("UPDATE card_pending_txns SET " + ",".join(f"{f}=?" for f in fields)
                       + " WHERE description<>'B'", overlay)
    if other_edited:
        store.conn.execute("UPDATE card_pending_txns SET " + ",".join(f"{f}=?" for f in fields)
                           + " WHERE description='B'",
                           ("其他", "電商", "conflicting note", '["other"]', 1,
                            '[{"amount":30},{"amount":70}]'))
    store.conn.commit()
    original = {r["description"]: tuple(r[f] for f in fields) for r in
                store.conn.execute("SELECT * FROM card_pending_txns")}
    assert original["SAFE"] == original["A"] == overlay
    incoming = (payload(billed=[statement(ntAmt=100, merchantChiName="A"),
                               statement(ref="independent", ntAmt=100, purchaseDt="091126",
                                         merchantChiName="SAFE")]) if mode == "statement" else
                payload(pending=[{**old, "postingDt": "20260912" if mode == "promotion" else "00000000"},
                                 {**safe, "postingDt": "20260912" if mode == "promotion" else "00000000"}]))
    ids = None
    for _ in range(2):
        persist_ctbc(incoming, store)
        pending = [dict(r) for r in store.conn.execute("SELECT * FROM card_pending_txns")]
        actual = [*pending, *billed_rows(store)]
        assert len(actual) == 2
        assert tuple(next(r for r in actual if r["description"] == "SAFE")[f] for f in fields) == original["SAFE"]
        target = next(r for r in actual if r["description"] == "A")
        assert tuple(target[f] for f in fields) == (None, None, None, None, 0, None)
        assert (target["amount"], target["currency"], target["consume_date"], target["post_date"]) == (
            100, "TWD", "2026-09-10", None if mode == "pending" else "2026-09-12")
        assert len(pending) == (2 if mode == "pending" else 0)
        if ids is not None:
            assert [r["id"] for r in billed_rows(store)] == ids
        ids = [r["id"] for r in billed_rows(store)]


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("mode", ["promotion", "statement"])
def test_equivalent_cross_scope_donor_copies_still_preserve_six_fields(store, native, mode):
    fields = ("category", "subcategory", "description_overwrite", "tags_overwrite",
              "auto_excluded", "splits_overwrite")
    overlay = ("旅遊", "交通", "original note", '["original"]', 1,
               '[{"amount":40},{"amount":60}]')
    old = unbilled(ref="synthetic-a" if native else None, postingDt="00000000",
                   purchaseAmt=100, origCurDesc="USD", origCurAmt=3, description="OLD")
    persist_ctbc(payload(pending=[old]), store)
    store.conn.execute("UPDATE card_pending_txns SET " + ",".join(f"{f}=?" for f in fields), overlay)
    columns = [r["name"] for r in store.conn.execute("PRAGMA table_info(card_pending_txns)")
               if r["name"] != "id"]
    expressions = ["'current'" if name == "scope" else name for name in columns]
    store.conn.execute(f"INSERT INTO card_pending_txns ({','.join(columns)}) "
                       f"SELECT {','.join(expressions)} FROM card_pending_txns WHERE scope='unbilled'")
    store.conn.commit()
    original = [dict(r) for r in store.conn.execute("SELECT * FROM card_pending_txns ORDER BY id")]
    assert [tuple(r[f] for f in fields) for r in original] == [overlay, overlay]
    incoming = (payload(billed=[statement(ref="synthetic-a" if native else None, ntAmt=100,
                                         occCurCode="USD", foreignAmt="3", merchantChiName="NEW")])
                if mode == "statement" else payload(pending=[{**old, "description": "NEW", "postingDt": "20260912"}]))
    ids = None
    for _ in range(2):
        persist_ctbc(incoming, store)
        actual = billed_rows(store)
        assert len(actual) == 1 and tuple(actual[0][f] for f in fields) == overlay
        assert [dict(r) for r in store.conn.execute("SELECT * FROM card_pending_txns")] == [original[1]]
        if ids is not None:
            assert [r["id"] for r in actual] == ids
        ids = [r["id"] for r in actual]

