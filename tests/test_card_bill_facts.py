from __future__ import annotations

import threading

from backend.core import persist as persist_mod
from backend.core.card_bills import (
    apply_card_bill_facts,
    make_card_bill_fact,
    publish_card_bill_facts,
)
from backend.core.store import BankStore


def _store(tmp_path, monkeypatch) -> BankStore:
    from backend.core import store as store_mod

    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path)
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    return BankStore("demo", user_id=1)


def test_make_card_bill_fact_fails_closed_and_keeps_payment_pair_atomic():
    assert make_card_bill_fact(remaining_due=True) is None
    assert make_card_bill_fact(remaining_due="NaN") is None
    assert make_card_bill_fact(remaining_due="Infinity") is None
    assert make_card_bill_fact(remaining_due=100_000_001) is None
    assert make_card_bill_fact(remaining_due=10, payment_due_date="not-a-date") is None

    assert make_card_bill_fact(
        remaining_due=0,
        last_payment_amount=123,
        last_payment_date="malformed",
    ) is None
    assert make_card_bill_fact(
        remaining_due=0, payment_due_date="2026-08-20junk",
    ) is None


def test_publish_card_bill_facts_rejects_partial_per_card_set():
    valid = make_card_bill_fact(
        scope="card", card_no="****7001", remaining_due=0,
    )
    out = {}

    publish_card_bill_facts(out, [valid, None])

    assert out == {"card_bill_facts_ok": False, "card_bill_facts": []}


def test_bank_scoped_card_bill_fact_updates_all_known_cards(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    try:
        store.upsert_cards([
            {"number": "****7001", "name": "A", "bill_due_amount": 1000},
            {"number": "****7002", "name": "B", "bill_due_amount": 2000},
        ])

        updated = apply_card_bill_facts(
            store,
            facts_ok=True,
            facts=[{
                "scope": "bank",
                "status": "paid",
                "remaining_due": 0,
                "payment_due_date": "2026-08-20",
                "last_payment_amount": 3000,
                "last_payment_date": "2026-08-10",
            }],
        )

        rows = store.conn.execute(
            "SELECT card_no, name, bill_due_amount, last_payment_amount, last_payment_date "
            "FROM cards ORDER BY card_no"
        ).fetchall()
        assert updated == 2
        assert [tuple(row) for row in rows] == [
            ("****7001", "A", 0.0, 3000.0, "2026-08-10"),
            ("****7002", "B", 0.0, 3000.0, "2026-08-10"),
        ]
    finally:
        store.close()


def test_card_scoped_fact_updates_only_matching_card(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    try:
        store.upsert_cards([
            {"number": "****7001", "name": "A", "bill_due_amount": 1000},
            {"number": "****7002", "name": "B", "bill_due_amount": 2000},
        ])

        updated = apply_card_bill_facts(
            store,
            facts_ok=True,
            facts=[{
                "scope": "card", "status": "paid",
                "card_no": "****7002", "remaining_due": 0,
            }],
        )

        rows = store.conn.execute(
            "SELECT card_no, bill_due_amount FROM cards ORDER BY card_no"
        ).fetchall()
        assert updated == 1
        assert [tuple(row) for row in rows] == [
            ("****7001", 1000.0),
            ("****7002", 0.0),
        ]
    finally:
        store.close()


def test_failed_card_bill_fetch_preserves_saved_values(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    try:
        store.upsert_cards([{"number": "****7001", "name": "A", "bill_due_amount": 1000}])

        updated = apply_card_bill_facts(store, facts_ok=False, facts=[])

        due = store.conn.execute(
            "SELECT bill_due_amount FROM cards WHERE card_no='****7001'"
        ).fetchone()[0]
        assert updated == 0
        assert due == 1000.0
    finally:
        store.close()


def test_older_incoming_payment_does_not_regress_saved_pair(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    try:
        store.upsert_cards([{
            "number": "****7001", "name": "A", "bill_due_amount": 1000,
            "last_payment_amount": 500, "last_payment_date": "2026-08-10",
        }])

        apply_card_bill_facts(store, facts_ok=True, facts=[{
            "scope": "bank", "status": "unpaid", "remaining_due": 900,
            "last_payment_amount": 300, "last_payment_date": "2026-08-01",
        }])

        row = store.conn.execute(
            "SELECT bill_due_amount, last_payment_amount, last_payment_date "
            "FROM cards WHERE card_no='****7001'"
        ).fetchone()
        assert row is not None
        assert tuple(row) == (900.0, 500.0, "2026-08-10")
    finally:
        store.close()


def test_same_due_cycle_can_add_statement_date_and_refresh_amount(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    try:
        store.upsert_cards([{
            "number": "****7001", "name": "A", "bill_due_amount": 5000,
            "payment_due_date": "2026-09-20",
        }])

        apply_card_bill_facts(store, facts=[{
            "scope": "bank", "status": "unpaid", "remaining_due": 1000,
            "statement_close_date": "2026-09-01", "payment_due_date": "2026-09-20",
        }], facts_ok=True)

        row = store.conn.execute(
            "SELECT bill_due_amount, statement_close_date, payment_due_date "
            "FROM cards WHERE card_no='****7001'"
        ).fetchone()
        assert row is not None
        assert tuple(row) == (1000.0, "2026-09-01", "2026-09-20")
    finally:
        store.close()


def test_concurrent_fact_writers_cannot_regress_newer_payment(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.upsert_cards([{"number": "****7001", "name": "A"}])
    store.close()
    barrier = threading.Barrier(2)
    errors = []

    def write(fact):
        worker = BankStore("demo", user_id=1)
        try:
            barrier.wait()
            apply_card_bill_facts(worker, facts=[fact], facts_ok=True)
        except Exception as exc:  # pragma: no cover - assertion below reports it
            errors.append(exc)
        finally:
            worker.close()

    older = {
        "scope": "bank", "status": "unpaid", "remaining_due": 5000,
        "statement_close_date": "2026-08-01", "payment_due_date": "2026-08-20",
        "last_payment_amount": 500, "last_payment_date": "2026-08-05",
    }
    newer = {
        "scope": "bank", "status": "unpaid", "remaining_due": 1000,
        "statement_close_date": "2026-09-01", "payment_due_date": "2026-09-20",
        "last_payment_amount": 1000, "last_payment_date": "2026-09-10",
    }
    threads = [
        threading.Thread(target=write, args=(older,)),
        threading.Thread(target=write, args=(newer,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    verify = BankStore("demo", user_id=1)
    try:
        row = verify.conn.execute(
            "SELECT bill_due_amount, statement_close_date, payment_due_date, "
            "last_payment_amount, last_payment_date FROM cards WHERE card_no='****7001'"
        ).fetchone()
        assert row is not None
        assert tuple(row) == (
            1000.0, "2026-09-01", "2026-09-20", 1000.0, "2026-09-10",
        )
    finally:
        verify.close()


def test_persist_collected_applies_canonical_facts_after_bank_adapter(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)

    def fake_persist(data, target_store, rules=None):
        target_store.upsert_cards([
            {"number": "****7001", "name": "A", "bill_due_amount": 1000},
            {"number": "****7002", "name": "B", "bill_due_amount": 2000},
        ])
        return {"bank": "demo"}

    monkeypatch.setitem(persist_mod.PERSISTERS, "demo", fake_persist)
    try:
        delta = persist_mod.persist_collected(
            "demo",
            {
                "card_bill_facts_ok": True,
                "card_bill_facts": [{
                    "scope": "bank", "status": "paid", "remaining_due": 0,
                }],
            },
            store,
        )
        dues = store.conn.execute(
            "SELECT bill_due_amount FROM cards ORDER BY card_no"
        ).fetchall()
        assert delta["card_bill_facts_applied"] == 2
        assert [row[0] for row in dues] == [0.0, 0.0]
    finally:
        store.close()


def test_persist_collected_blocks_bank_specific_bill_writes_on_failed_fetch(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.upsert_cards([{
        "number": "****7001", "name": "Old", "bill_due_amount": 777,
        "payment_due_date": "2026-08-20",
    }])

    def fake_persist(data, target_store, rules=None):
        target_store.upsert_cards([{
            "number": "****7001", "name": "Fresh metadata",
            "bill_due_amount": 9999, "payment_due_date": "2026-09-20",
        }])
        return {}

    monkeypatch.setitem(persist_mod.PERSISTERS, "demo", fake_persist)
    try:
        persist_mod.persist_collected(
            "demo", {"card_bill_facts_ok": False}, store,
        )
        row = store.conn.execute(
            "SELECT name, bill_due_amount, payment_due_date "
            "FROM cards WHERE card_no='****7001'"
        ).fetchone()
        assert row is not None
        assert tuple(row) == ("Fresh metadata", 777.0, "2026-08-20")
    finally:
        store.close()
