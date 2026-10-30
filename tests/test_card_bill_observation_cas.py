"""Pre-collect canonical snapshots: synthetic stores only, no bank/network I/O."""
from __future__ import annotations

import pytest

from backend.core import card_bills, store as store_mod
from backend.core.persist import persist_collected
from backend.core.store import BankStore


FIELDS = (
    "bill_due_amount", "statement_close_date", "payment_due_date",
    "last_payment_amount", "last_payment_date", "bill_facts_revision",
)
SAVED = {
    "number": "****7001", "name": "Synthetic",
    "bill_due_amount": 1000, "statement_close_date": "2026-10-01",
    "payment_due_date": "2026-10-20",
    "last_payment_amount": 200, "last_payment_date": "2026-10-05",
}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path)
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(store_mod.bank_pg, "enabled", lambda: False)
    instance = BankStore("dbs", user_id=1)
    instance.upsert_cards([SAVED])
    yield instance
    instance.close()


def capture(store):
    capture_fn = getattr(card_bills, "capture_card_bill_snapshot", None)
    assert callable(capture_fn), "pre-collect snapshot API is required"
    return capture_fn(store)


def canonical(store, number="****7001"):
    row = store.conn.execute(
        f"SELECT {', '.join(FIELDS)} FROM cards WHERE user_id = ? AND card_no = ?",
        (store.user_id, number),
    ).fetchone()
    return tuple(row)


def collected(remaining=0, **overrides):
    fact = {
        "scope": "bank", "status": "unpaid" if remaining else "no_payment_required",
        "remaining_due": remaining, "payment_due_date": "2026-10-20",
        **overrides,
    }
    return {
        "api_responses": {"liabilities": [{"resp": {"creditCard": {
            "cards": [{"cardNumber": "************7001", "isDisplayImg": True}],
            "paymentDetails": {"amount": 1000, "dueDate": "2026-10-20"},
        }}}]},
        "card_bill_facts_ok": True, "card_bill_facts": [fact],
    }


@pytest.mark.parametrize("remaining", [0, 300, 1500])
def test_actual_dbs_persist_refreshes_remaining_preserving_dated_payment(store, remaining):
    snapshot = capture(store)
    before = canonical(store)
    delta = persist_collected("dbs", collected(remaining), store, bill_snapshot=snapshot)
    after = canonical(store)
    assert delta["card_bill_facts_applied"] == 1
    assert after[:5] == (remaining, *before[1:5])
    assert after[5] and after[5] != before[5]


def test_upsert_versions_canonical_writes_but_not_metadata(store):
    before = canonical(store)
    assert before[5] is not None
    store.upsert_cards([{"number": SAVED["number"], "name": "metadata", "used_credit": 9}])
    assert canonical(store) == before
    # Even writing identical canonical values invalidates an in-flight observation.
    store.upsert_cards([SAVED])
    assert canonical(store)[:5] == before[:5]
    assert canonical(store)[5] != before[5]


def test_auto_debit_versions_canonical_payment_writer(store):
    before = canonical(store)
    assert store.record_auto_debit_payment("2026-10-06", 400) == 1
    assert canonical(store)[5] != before[5]
    revision = canonical(store)[5]
    assert store.record_auto_debit_payment("2026-10-04", 100) == 0
    assert canonical(store)[5] == revision


@pytest.mark.parametrize("writer", ["upsert", "facts", "auto_debit"])
def test_aba_paid_then_reversed_cannot_reuse_snapshot(store, writer):
    snapshot = capture(store)
    before = canonical(store)
    if writer == "auto_debit":
        store.record_auto_debit_payment("2026-10-06", 1000)
    elif writer == "facts":
        store.update_card_bill_facts([{
            "number": SAVED["number"], "bill_due_amount": 0,
            "payment_due_date": SAVED["payment_due_date"],
            "last_payment_amount": 1000, "last_payment_date": "2026-10-06",
        }])
    else:
        store.upsert_cards([{**SAVED, "bill_due_amount": 0}])
    store.upsert_cards([SAVED])
    assert canonical(store)[:5] == before[:5]
    assert canonical(store)[5] != before[5]
    assert persist_collected("dbs", collected(), store, bill_snapshot=snapshot)[
        "card_bill_facts_applied"
    ] == 0
    assert canonical(store)[:5] == before[:5]


@pytest.mark.parametrize("field,value", [
    ("bill_due_amount", 777),
    ("statement_close_date", "2026-10-02"),
    ("payment_due_date", "2026-10-21"),
    ("last_payment_amount", 201),
    ("last_payment_date", "2026-10-06"),
    ("bill_facts_revision", "different-revision"),
])
@pytest.mark.parametrize("incoming_payment", [False, True])
def test_each_snapshot_component_blocks_even_otherwise_permitted_route(
    store, field, value, incoming_payment,
):
    snapshot = capture(store)
    # Leave revision unchanged to prove every individual value is compared.
    store.conn.execute(
        f"UPDATE cards SET {field} = ? WHERE user_id = ? AND card_no = ?",
        (value, store.user_id, SAVED["number"]),
    )
    store.conn.commit()
    before = canonical(store)
    kwargs = {"last_payment_amount": 800, "last_payment_date": "2026-10-10"} if incoming_payment else {}
    assert persist_collected("dbs", collected(**kwargs), store, bill_snapshot=snapshot)[
        "card_bill_facts_applied"
    ] == 0
    assert canonical(store) == before


def test_second_connection_changes_only_debt_immediately_before_atomic_update(store):
    snapshot = capture(store)
    other = BankStore("dbs", user_id=1)
    raw = store.conn
    raced = []

    class RaceConnection:
        def __getattr__(self, name):
            return getattr(raw, name)

        def execute(self, sql, params=()):
            if sql.startswith("UPDATE cards\n"):
                other.conn.execute(
                    "UPDATE cards SET bill_due_amount = 777 WHERE user_id = 1 AND card_no = ?",
                    (SAVED["number"],),
                )
                other.conn.commit()
                raced.append(True)
            return raw.execute(sql, params)

    store.conn = RaceConnection()
    try:
        delta = persist_collected("dbs", collected(), store, bill_snapshot=snapshot)
        assert raced == [True]
        assert delta["card_bill_facts_applied"] == 0
        assert canonical(store)[0] == 777
        assert canonical(store)[1:] == snapshot[("dbs", 1, SAVED["number"])][1:]
    finally:
        store.conn = raw
        other.close()


@pytest.mark.parametrize("context", [
    None, {}, {("dbs", 2, "****7001"): ()},
    {("other", 1, "****7001"): ()}, {("dbs", 1, "****7002"): ()},
])
def test_missing_or_foreign_context_cannot_bypass_saved_payment(store, context):
    before = canonical(store)
    delta = persist_collected("dbs", collected(), store, bill_snapshot=context)
    assert delta["card_bill_facts_applied"] == 0
    assert canonical(store) == before


def test_real_cross_owner_snapshot_does_not_apply_to_other_owner(store):
    snapshot = capture(store)
    other = BankStore("dbs", user_id=2)
    try:
        other.upsert_cards([SAVED])
        before = canonical(other)
        assert persist_collected("dbs", collected(), other, bill_snapshot=snapshot)[
            "card_bill_facts_applied"
        ] == 0
        assert canonical(other) == before
    finally:
        other.close()


def test_bank_snapshot_includes_inactive_and_payload_omitted_cards(store):
    store.upsert_cards([
        {**SAVED, "number": "****7002", "active": False},
        {**SAVED, "number": "****7003"},
    ])
    snapshot = capture(store)
    assert len(snapshot) == 3
    assert persist_collected("dbs", collected(), store, bill_snapshot=snapshot)[
        "card_bill_facts_applied"
    ] == 3
    rows = {row["card_no"]: row for row in store.list_cards()}
    assert [row["bill_due_amount"] for row in rows.values()] == [0, 0, 0]
    assert rows["****7002"]["active"] == 0


@pytest.mark.parametrize("canonical_seed", [{}, {"bill_due_amount": 9}, {"payment_due_date": "2026-09-20"}])
def test_card_missing_from_snapshot_can_only_initialize_empty_unversioned_tuple(store, canonical_seed):
    snapshot = capture(store)
    store.upsert_cards([{"number": "****7002", **canonical_seed}])
    data = collected()
    data["card_bill_facts"][0].update(scope="card", card_no="****7002")
    before = canonical(store, "****7002")
    expected = 0 if canonical_seed else 1
    assert persist_collected("dbs", data, store, bill_snapshot=snapshot)["card_bill_facts_applied"] == expected
    after = canonical(store, "****7002")
    if expected:
        assert after[:5] == (0, None, "2026-10-20", None, None)
        assert after[5]
    else:
        assert after == before


def test_empty_but_versioned_card_missing_from_snapshot_is_not_new(store):
    snapshot = capture(store)
    store.upsert_cards([{"number": "****7002"}])
    store.conn.execute("UPDATE cards SET bill_facts_revision = 'prior-write' WHERE card_no = '****7002'")
    store.conn.commit()
    data = collected()
    data["card_bill_facts"][0].update(scope="card", card_no="****7002")
    assert persist_collected("dbs", data, store, bill_snapshot=snapshot)["card_bill_facts_applied"] == 0
    assert canonical(store, "****7002") == (None, None, None, None, None, "prior-write")


def test_no_snapshot_keeps_original_newer_payment_route(store):
    assert persist_collected("dbs", collected(
        last_payment_amount=800, last_payment_date="2026-10-10",
    ), store)["card_bill_facts_applied"] == 1
    assert canonical(store)[3:5] == (800, "2026-10-10")


def test_actual_dbs_adapter_can_initialize_new_card_discovered_after_snapshot(store):
    snapshot = capture(store)
    data = collected()
    data["api_responses"]["liabilities"][0]["resp"]["creditCard"]["cards"].append({
        "cardNumber": "************7002", "isDisplayImg": True,
    })
    assert persist_collected("dbs", data, store, bill_snapshot=snapshot)["card_bill_facts_applied"] == 2
    assert canonical(store, "****7002")[:5] == (0, None, "2026-10-20", None, None)


def test_snapshot_does_not_enable_dbs_payment_exception_for_other_bank(store):
    # Same synthetic connection, but bind both the store and baseline to another bank.
    store.bank = "ctbc"
    snapshot = capture(store)
    before = canonical(store)
    assert card_bills.apply_card_bill_facts(
        store, facts_ok=True, facts=collected()["card_bill_facts"], bill_snapshot=snapshot,
    ) == 0
    assert canonical(store) == before


@pytest.mark.parametrize("overrides", [
    {"payment_due_date": "2026-09-20"},
    {"payment_due_date": None},
    {"last_payment_amount": 100, "last_payment_date": "2026-10-04"},
])
def test_snapshot_does_not_override_old_cycle_missing_due_or_older_dated_payment(store, overrides):
    snapshot = capture(store)
    before = canonical(store)
    persist_collected("dbs", collected(**overrides), store, bill_snapshot=snapshot)
    assert canonical(store)[:5] == before[:5]


@pytest.mark.parametrize("overrides", [
    {"remaining_due": -1}, {"remaining_due": 1.5},
    {"last_payment_amount": 10}, {"payment_due_date": "invalid"},
])
def test_invalid_facts_fail_before_adapter_writes(store, overrides):
    snapshot = capture(store)
    before = canonical(store)
    with pytest.raises(ValueError):
        persist_collected("dbs", collected(**overrides), store, bill_snapshot=snapshot)
    assert canonical(store) == before
    assert store.list_cards()[0]["name"] == SAVED["name"]


@pytest.mark.parametrize("field", FIELDS[:5])
def test_each_upsert_canonical_column_rotates_revision(store, field):
    before = canonical(store)
    store.upsert_cards([{"number": SAVED["number"], field: SAVED[field]}])
    assert canonical(store)[:5] == before[:5]
    assert canonical(store)[5] != before[5]


def test_legacy_sqlite_migration_is_nullable_idempotent_and_preserves_facts(store):
    before = canonical(store)[:5]
    store.conn.execute("ALTER TABLE cards DROP COLUMN bill_facts_revision")
    store.conn.commit()
    store.close()
    for _ in range(2):
        reopened = BankStore("dbs", user_id=1)
        try:
            columns = {row["name"]: row for row in reopened.conn.execute("PRAGMA table_info(cards)")}
            assert columns["bill_facts_revision"]["type"] == "TEXT"
            assert columns["bill_facts_revision"]["notnull"] == 0
            assert canonical(reopened) == (*before, None)
            assert persist_collected("dbs", collected(), reopened, bill_snapshot=capture(reopened))[
                "card_bill_facts_applied"
            ] == 1
            # Keep the second reopen's migration baseline identical, without a fresh UUID.
            reopened.conn.execute(
                "UPDATE cards SET bill_due_amount = 1000, bill_facts_revision = NULL WHERE card_no = ?",
                (SAVED["number"],),
            )
            reopened.conn.commit()
        finally:
            reopened.close()


def test_postgres_constructor_delegates_revision_migration_to_authority_cache(monkeypatch):
    from backend.core.bank_pg import q

    statements = []

    class FakePgConnection:
        def executescript(self, script):
            assert "bill_facts_revision  TEXT" in q(script)

        def ensure_schema_migrations(self):
            statements.append("existing-migrations")

        def execute(self, sql, params=()):
            statements.append(q(sql))

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(store_mod.bank_pg, "enabled", lambda: True)
    monkeypatch.setattr(store_mod.bank_pg, "connect", lambda bank: FakePgConnection())
    for _ in range(2):
        BankStore("dbs", user_id=1).close()
    assert statements == ["existing-migrations"] * 2


@pytest.mark.parametrize("empty_baseline", [False, True])
def test_postgres_cas_sql_uses_column_typed_nullable_comparisons(store, empty_baseline):
    from backend.core.bank_pg import q

    snapshot = {} if empty_baseline else capture(store)
    expected_baseline = (None,) * 6 if empty_baseline else canonical(store)
    raw = store.conn
    observed = []

    class SqlCheckingConnection:
        def __getattr__(self, name):
            return getattr(raw, name)

        def execute(self, sql, params=()):
            translated = q(sql)
            assert "%s IS NULL" not in translated
            assert "%s IS NOT NULL" not in translated
            assert translated.count("%s") == len(params)
            where = translated.split("WHERE", 1)[1]
            for field in FIELDS:
                assert f"{field} IS NOT DISTINCT FROM %s" in where
            assert tuple(params[-6:]) == expected_baseline
            observed.append(translated)
            # Execute the original SQL on real SQLite; this is not a real PG test.
            return raw.execute(sql, params)

    store.conn = SqlCheckingConnection()
    try:
        assert store.update_card_bill_facts([{
            "number": SAVED["number"], "bill_due_amount": 0,
            "payment_due_date": SAVED["payment_due_date"],
        }], bill_snapshot=snapshot) == (0 if empty_baseline else 1)
        assert len(observed) == 1
    finally:
        store.conn = raw


def test_snapshot_write_respects_commit_false_and_rollback(store):
    snapshot = capture(store)
    before = canonical(store)
    assert card_bills.apply_card_bill_facts(
        store, facts_ok=True, facts=collected()["card_bill_facts"],
        bill_snapshot=snapshot, commit=False,
    ) == 1
    assert canonical(store)[0] == 0
    store.conn.rollback()
    assert canonical(store) == before
