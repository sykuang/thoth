"""Real synthetic SQLite operation errors must survive adapter cleanup errors."""
from copy import deepcopy
from datetime import date
from importlib import import_module
import sqlite3

import pytest

from backend.core import persist, store as store_module
from backend.core.error_codes import exception_error_id
from backend.core.store import BankStore
from tests.taishin_fixtures import with_taishin_history


LEDGERS = ("_new_billed_ids", "_current_billed_ids", "_adopted_pending_raw_keys",
           "_unsafe_pending_overlay_identities")


def _billed_payload(bank, monkeypatch):
    module = import_module("backend.core.persist." + bank)
    if bank == "taishin":
        from datetime import datetime, timedelta, timezone

        class ReceiptDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 9, 3, 12, tzinfo=tz)

        monkeypatch.setattr(module, "datetime", ReceiptDatetime)
        monkeypatch.setattr(module, "ZoneInfo", lambda name: timezone(timedelta(hours=8)))
        data = with_taishin_history({"credit_card_parsed": {
            "cards": [{"number": "****7030", "name": "Synthetic card"}],
            "billing_period": {"statement_date": "2026/05/17"},
            "billed_txns": [{"txn_date": "2026/05/04", "post_date": "2026/05/04",
                             "card_no_suffix": "7030", "desc": "synthetic purchase",
                             "currency": "新臺幣", "amount": 100}],
        }}, as_of=date(2026, 9, 3))
    elif bank == "hsbc":
        from tests.test_persist_hsbc_native_bill_fields import _hsbc_card_payload
        data = _hsbc_card_payload()
        detail = next(iter(data["card_detail"].values()))
        detail["posted"] = [{"description": "synthetic purchase", "transactionDate": "2026-07-01T00:00",
                             "postedDate": "2026-07-03T00:00", "ntdAmount": "100 TWD",
                             "isPositive": True, "isForeign": False}]
        detail["posted_receipt"].update(status="complete", rows=1)
        data["history_coverage"]["domains"][0]["windows"][0].update(status="complete", rows=1)
    else:
        data = import_module("tests.test_" + bank + "_history_sync")._persist_payload()
        monkeypatch.setattr(module, "_today", lambda: date.fromisoformat(data["history_coverage"]["as_of"]))

    if bank == "sinopac":
        data.update({
            "all_cards": {"Result": {"Items": [{
                "CardNo": "9000000000347030", "Name": "Synthetic card", "ExpDate": "0829",
            }]}},
            "card_statements": [{
                "billing_cycle_date": "2026/05/17",
                "records": [{"trans_date": "2026/05/04", "post_date": "2026/05/04",
                             "card_last4": "7030", "description": "synthetic purchase", "amount": "100"}],
            }],
        })

    if bank == "ubot":
        data["card_billed"] = [{"CardHeader": {"stmtDate": "20260517"}, "CardList": [{
            "cardNo": "****7030", "effectDate": "20260504", "postDate": "20260504",
            "txDesc": "synthetic purchase", "txAmt": "100", "Currency": "TWD", "oriAmt": "100",
        }]}]
    # Rakuten's validated account-only persister has no card insert/refresh path.
    return data


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("bank", ["rakuten", "sinopac", "taishin", "ubot", "hsbc"])
@pytest.mark.parametrize("route", ["direct", "direct_commit_false", "collected"])
@pytest.mark.parametrize("rollback_fault", [None, "before", "after"])
def test_public_persister_preserves_native_insert_error(bank, route, rollback_fault, nested, tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    monkeypatch.setattr(store_module.bank_pg, "enabled", lambda: False)
    module = import_module("backend.core.persist." + bank)
    data = _billed_payload(bank, monkeypatch)

    class FaultConnection(sqlite3.Connection):
        operation_error = None
        rollback_calls = 0
        commit_calls = 0
        billed_at_failure = None
        ledgers_at_failure = None

        def execute(self, sql, parameters=()):
            if "INSERT INTO sync_log" in sql:
                self.billed_at_failure = super().execute("SELECT COUNT(*) FROM card_billed_txns").fetchone()[0]
                self.ledgers_at_failure = {name: deepcopy(getattr(store, name)) for name in LEDGERS}
            try:
                return super().execute(sql, parameters)
            except sqlite3.IntegrityError as error:
                self.operation_error = error
                raise

        def rollback(self):
            self.rollback_calls += 1
            if rollback_fault == "before":
                raise sqlite3.OperationalError("synthetic rollback failure")
            super().rollback()
            if rollback_fault == "after":
                raise sqlite3.OperationalError("synthetic rollback failure")

        def commit(self):
            self.commit_calls += 1
            super().commit()

    store = BankStore(bank, user_id=1, source_account_id=7)
    store.conn.close()
    store.conn = sqlite3.connect(store.db_path, factory=FaultConnection)
    store.conn.row_factory = sqlite3.Row
    store.conn.execute(
        "CREATE TEMP TRIGGER fail_native_log BEFORE INSERT ON sync_log "
        "BEGIN SELECT RAISE(ABORT, 'synthetic native INSERT failure'); END"
    )
    sqlite3.Connection.commit(store.conn)
    if nested:
        store.begin_transaction()
        store.conn.execute("SAVEPOINT synthetic_outer")
        store.put_daily_metric("synthetic_outer", {"keep": "until owner cleanup"}, "2026-09-03", commit=False)
    names = LEDGERS
    before = {name: deepcopy(getattr(store, name)) for name in names}
    assert before == dict(zip(names, ([], [], {}, set())))
    try:
        with pytest.raises(Exception) as caught:
            if route == "collected":
                persist.persist_collected(bank, data, store)
            else:
                getattr(module, "persist_" + bank)(data, store, commit=route != "direct_commit_false")

        assert caught.value is store.conn.operation_error
        assert type(caught.value) is sqlite3.IntegrityError
        assert str(caught.value) == "synthetic native INSERT failure"
        assert exception_error_id(caught.value, bank=bank) == "SYS-08-168-001"
        assert store.conn.commit_calls == 0
        # Rakuten intentionally owns rollback even with commit=False; the other
        # adapters leave it to the atomic dispatcher. Do not normalize that policy.
        rollbacks = (2 if bank == "rakuten" else 1) if route == "collected" else (
            1 if bank == "rakuten" or route == "direct" else 0)
        assert store.conn.rollback_calls == rollbacks
        assert store.conn.billed_at_failure == (0 if bank == "rakuten" else 1)
        assert store.conn.ledgers_at_failure is not None
        if bank != "rakuten":
            assert store.conn.ledgers_at_failure["_new_billed_ids"] == [1]
            assert store.conn.ledgers_at_failure["_current_billed_ids"] == [1]
        if rollbacks == 0 or rollback_fault == "before":
            # No physical cleanup occurred: RAM restoration cannot establish SQL rollback.
            assert store.conn.in_transaction
            assert store.conn.execute("SELECT COUNT(*) FROM card_billed_txns").fetchone()[0] == (0 if bank == "rakuten" else 1)
        else:
            assert not store.conn.in_transaction
            assert all(count == 0 for count in store.stats().values())
        expected = before if rollbacks else store.conn.ledgers_at_failure
        assert {name: getattr(store, name) for name in names} == expected
        if rollbacks == 0 or rollback_fault == "before":
            sqlite3.Connection.rollback(store.conn)
        assert not store.conn.in_transaction
        assert all(count == 0 for count in store.stats().values())
        assert store.conn.execute("SELECT COUNT(*) FROM history_transaction_cursors").fetchone()[0] == 0
        observer = sqlite3.connect(store.db_path)
        try:
            for table in ("accounts", "cards", "twd_transactions", "card_billed_txns",
                          "card_pending_txns", "daily_metrics", "sync_log", "history_transaction_cursors"):
                assert observer.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        finally:
            observer.close()
    finally:
        store.close()


@pytest.mark.parametrize("bank", ["rakuten", "sinopac", "taishin", "ubot", "hsbc"])
@pytest.mark.parametrize("route", ["direct", "direct_commit_false", "collected"])
def test_public_persister_success_keeps_new_ledgers(bank, route, tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(store_module.bank_pg, "enabled", lambda: False)
    data = _billed_payload(bank, monkeypatch)
    store = BankStore(bank, user_id=1, source_account_id=7)
    try:
        if route == "collected":
            persist.persist_collected(bank, data, store)
        else:
            getattr(persist, "persist_" + bank)(data, store, commit=route != "direct_commit_false")
        count = 0 if bank == "rakuten" else 1
        assert store.conn.execute("SELECT COUNT(*) FROM card_billed_txns").fetchone()[0] == count
        assert store._new_billed_ids == ([] if bank == "rakuten" else [1])
        assert store._current_billed_ids == ([] if bank == "rakuten" else [1])
        observer = sqlite3.connect(store.db_path)
        try:
            assert observer.execute("SELECT COUNT(*) FROM card_billed_txns").fetchone()[0] == (
                0 if route == "direct_commit_false" else count)
            assert observer.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0] == (
                0 if route == "direct_commit_false" else 1)
        finally:
            observer.close()
        assert store.conn.in_transaction == (route == "direct_commit_false")
    finally:
        store.close()


@pytest.mark.parametrize("bank", ["sinopac", "taishin", "ubot", "hsbc"])
@pytest.mark.parametrize("route", ["direct", "collected"])
def test_owned_failure_restores_nonempty_preimage(bank, route, tmp_path, monkeypatch):
    from collections import Counter

    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(store_module.bank_pg, "enabled", lambda: False)
    data = _billed_payload(bank, monkeypatch)
    store = BankStore(bank, user_id=1, source_account_id=7)
    try:
        getattr(persist, "persist_" + bank)(data, store)
        store._adopted_pending_raw_keys = {("synthetic unrelated receipt",): {"raw": Counter({"original": 2})}}
        store._unsafe_pending_overlay_identities = {("synthetic unrelated overlay",)}
        before = {name: deepcopy(getattr(store, name)) for name in LEDGERS}
        assert before["_new_billed_ids"] == before["_current_billed_ids"] == [1]
        # A new native occurrence must be inserted after the existing durable row.
        if bank == "sinopac":
            data["card_statements"][0]["records"][0]["description"] = "second synthetic purchase"
        elif bank == "ubot":
            data["card_billed"][0]["CardList"][0]["txDesc"] = "second synthetic purchase"
        elif bank == "taishin":
            data["credit_card_parsed"]["billed_txns"][0]["desc"] = "second synthetic purchase"
        else:
            next(iter(data["card_detail"].values()))["posted"][0]["description"] = "second synthetic purchase"
        store.conn.execute("CREATE TEMP TRIGGER fail_second BEFORE INSERT ON sync_log "
                           "BEGIN SELECT RAISE(ABORT, 'synthetic second failure'); END")
        store.conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="synthetic second failure"):
            if route == "collected":
                persist.persist_collected(bank, data, store)
            else:
                getattr(persist, "persist_" + bank)(data, store)
        assert not store.conn.in_transaction
        assert store.conn.execute("SELECT COUNT(*) FROM card_billed_txns").fetchone()[0] == 1
        assert {name: getattr(store, name) for name in LEDGERS} == before
        assert before["_adopted_pending_raw_keys"] == {("synthetic unrelated receipt",): {"raw": Counter({"original": 2})}}
    finally:
        store.close()


@pytest.mark.parametrize("bank", ["rakuten", "sinopac", "taishin", "ubot", "hsbc"])
@pytest.mark.parametrize("route", ["direct", "collected"])
def test_owned_failure_does_not_add_absent_fake_store_ledgers(bank, route, tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(store_module.bank_pg, "enabled", lambda: False)
    data = _billed_payload(bank, monkeypatch)
    real = BankStore(bank, user_id=1, source_account_id=7)

    class NoLedgerStore:
        # Compatibility double only; all SQL still executes on synthetic SQLite.
        def __getattr__(self, name):
            if name in LEDGERS:
                raise AttributeError(name)
            return getattr(real, name)

    fake = NoLedgerStore()
    try:
        real.conn.execute("CREATE TEMP TRIGGER fail_fake BEFORE INSERT ON sync_log "
                          "BEGIN SELECT RAISE(ABORT, 'synthetic fake-store failure'); END")
        real.conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="synthetic fake-store failure"):
            if route == "collected":
                persist.persist_collected(bank, data, fake)
            else:
                getattr(persist, "persist_" + bank)(data, fake)
        assert vars(fake) == {}
        assert all(not hasattr(fake, name) for name in LEDGERS)
        assert not real.conn.in_transaction
        assert all(count == 0 for count in real.stats().values())
    finally:
        real.close()


def test_shared_ledger_snapshot_detaches_nested_state():
    from collections import Counter
    from types import SimpleNamespace
    from backend.core.persist._common import _snapshot_occurrence_ledgers, _restore_occurrence_ledgers

    store = SimpleNamespace(_new_billed_ids=[1], _current_billed_ids=[1],
                            _adopted_pending_raw_keys={("receipt",): {"raw": Counter({"original": 2})}},
                            _unsafe_pending_overlay_identities={("overlay",)})
    before = _snapshot_occurrence_ledgers(store)
    store._new_billed_ids.append(2)
    store._current_billed_ids.append(2)
    store._adopted_pending_raw_keys[("receipt",)]["raw"]["original"] = 0
    store._unsafe_pending_overlay_identities.clear()
    _restore_occurrence_ledgers(store, before)
    assert store._new_billed_ids == store._current_billed_ids == [1]
    assert store._adopted_pending_raw_keys == {("receipt",): {"raw": Counter({"original": 2})}}
    assert store._unsafe_pending_overlay_identities == {("overlay",)}


@pytest.mark.parametrize("route", ["direct", "collected"])
@pytest.mark.parametrize("nonempty", [False, True])
@pytest.mark.parametrize("stage,cleanup", [(stage, cleanup) for stage in ("body", "release")
                         for cleanup in (None, "rollback_denied", "rollback_after", "release_denied")]
                         + [("commit", None)])
def test_scsb_public_native_error_survives_scope_cleanup(stage, cleanup, route, nonempty, tmp_path, monkeypatch):
    from tests.test_persist_occurrence_ledger_atomicity import (
        _scsb_preimage, _scsb_ram, _scsb_sql,
    )

    store, data, marker = _scsb_preimage(tmp_path, monkeypatch, nonempty)
    real = store.conn
    assert isinstance(real, sqlite3.Connection)
    if stage == "body":
        real.execute("CREATE TEMP TRIGGER fail_scsb_log BEFORE INSERT ON sync_log "
                     "BEGIN SELECT RAISE(ABORT, 'synthetic SCSB body failure'); END")

    before_ram, before_sql = _scsb_ram(store), _scsb_sql(real)
    errors, operations = [], []
    denied_release = False
    whole_rollbacks = []

    def authorizer(action, operation, name, *_):
        nonlocal denied_release
        if stage == "commit" and action == sqlite3.SQLITE_TRANSACTION and operation == "COMMIT":
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_SAVEPOINT and name == "scsb_persist":
            operations.append(operation)
            if operation == "RELEASE" and stage == "release" and not denied_release:
                denied_release = True
                return sqlite3.SQLITE_DENY
            if operation == "ROLLBACK" and cleanup == "rollback_denied":
                return sqlite3.SQLITE_DENY
            if operation == "RELEASE" and cleanup == "release_denied" and "ROLLBACK" in operations:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    class ObserveConnection:
        def __getattr__(self, name):
            return getattr(real, name)

        def execute(self, sql, *args):
            if ("INSERT INTO sync_log" in sql or sql == "RELEASE SAVEPOINT scsb_persist") and not errors:
                assert marker in store._unsafe_pending_overlay_identities
                assert _scsb_sql(real) != before_sql
            try:
                result = real.execute(sql, *args)
                if sql == "ROLLBACK TO SAVEPOINT scsb_persist" and cleanup == "rollback_after":
                    raise sqlite3.OperationalError("synthetic after-rollback cleanup failure")
                return result
            except sqlite3.DatabaseError as error:
                errors.append(error)
                raise

        def commit(self):
            assert marker in store._unsafe_pending_overlay_identities
            try:
                real.commit()
            except sqlite3.DatabaseError as error:
                errors.append(error)
                raise

        def rollback(self):
            whole_rollbacks.append(True)
            real.rollback()

    real.set_authorizer(authorizer)
    monkeypatch.setattr(store, "conn", ObserveConnection())
    try:
        with pytest.raises(sqlite3.DatabaseError) as caught:
            if route == "direct":
                persist.persist_scsb(data, store)
            else:
                persist.persist_collected("scsb", data, store)
        assert caught.value is errors[0]
        assert exception_error_id(caught.value) == {
            "body": "SYS-08-168-001", "release": "SYS-08-146-003", "commit": "SYS-08-148-001",
        }[stage]
        assert whole_rollbacks == []
        assert real.in_transaction
        assert _scsb_ram(store) == before_ram
        if stage == "commit" or cleanup == "rollback_denied":
            assert _scsb_sql(real) != before_sql  # No physical SQL rollback: do not claim atomic SQL.
        else:
            assert _scsb_sql(real) == before_sql
        assert ("ROLLBACK" in operations) == (stage != "commit")
        assert len(errors) == (2 if cleanup is not None else 1)
    finally:
        real.set_authorizer(None)
        real.rollback()
        store.close()
