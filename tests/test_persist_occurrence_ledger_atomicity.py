"""Real synthetic SQLite late-phase failures must restore SQL and RAM together."""
from collections import Counter
from copy import deepcopy
from hashlib import sha256
import sqlite3

import pytest

from backend.core import persist, store as store_module
from backend.core.card_bills import publish_card_bill_facts
from backend.banks.hsbc import _hsbc_card_bill_facts
from backend.core.store import BankStore
from tests.test_persist_hsbc_native_bill_fields import _hsbc_card_payload


@pytest.mark.parametrize("route,stage", [("dispatcher", stage) for stage in
                          ("metric", "facts", "cursor", "commit")]
                         + [("direct", "metric"), ("direct", "commit")])
def test_late_dispatcher_failure_restores_occurrence_ledgers(route, stage, tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(store_module.bank_pg, "enabled", lambda: False)
    store = BankStore("hsbc", user_id=1, source_account_id=7)
    data = _hsbc_card_payload()
    detail = next(iter(data["card_detail"].values()))
    detail["posted"] = [{"description": "synthetic purchase", "transactionDate": "2026-07-01T00:00",
                         "postedDate": "2026-07-03T00:00", "ntdAmount": "100 TWD",
                         "isPositive": True, "isForeign": False}]
    detail["posted_receipt"].update(status="complete", rows=1)
    data["history_coverage"]["domains"][0]["windows"][0].update(status="complete", rows=1)
    publish_card_bill_facts(data, _hsbc_card_bill_facts(data))
    if stage == "commit":
        store.conn.execute("PRAGMA foreign_keys=ON")
        store.conn.execute("CREATE TABLE fault_parent (id INTEGER PRIMARY KEY)")
        store.conn.execute("CREATE TABLE fault_child (parent_id INTEGER REFERENCES fault_parent(id) "
                           "DEFERRABLE INITIALLY DEFERRED)")
        store.conn.execute("CREATE TEMP TRIGGER defer_commit_failure AFTER INSERT ON sync_log "
                           "BEGIN INSERT INTO fault_child VALUES(1); END")
    else:
        table, operation = {"metric": ("daily_metrics", "INSERT"), "facts": ("cards", "UPDATE"),
                            "cursor": ("history_transaction_cursors", "INSERT")}[stage]
        store.conn.execute(f"CREATE TEMP TRIGGER fail_late BEFORE {operation} ON {table} "
                           "BEGIN SELECT RAISE(ABORT, 'synthetic late failure'); END")
    store.conn.commit()
    names = ("_new_billed_ids", "_current_billed_ids", "_adopted_pending_raw_keys",
             "_unsafe_pending_overlay_identities")
    original = {name: deepcopy(getattr(store, name)) for name in names}
    try:
        with pytest.raises(sqlite3.IntegrityError):
            if route == "direct":
                persist.persist_hsbc(data, store)
            else:
                persist.persist_collected("hsbc", data, store)
        assert all(n == 0 for n in store.stats().values())
        assert store.latest_card_transaction_dates() == {}
        assert {name: getattr(store, name) for name in names} == original
    finally:
        store.close()


@pytest.mark.parametrize("with_dict", [False, True])
def test_snapshot_does_not_probe_unowned_compatibility_attributes(with_dict):
    from backend.core.card_bills import CardBillWriteBarrier
    from backend.core.persist._common import _snapshot_occurrence_ledgers, _restore_occurrence_ledgers

    class NoRAM:
        __slots__ = ("__dict__",) if with_dict else ()

        def __getattr__(self, name):
            raise AssertionError(f"unexpected compatibility lookup: {name}")

        @property
        def _billed_source_rows(self):
            raise AssertionError("readable property is not owned RAM")

    store = NoRAM()
    barrier = CardBillWriteBarrier(store)
    for owner in (object(), store, barrier):
        before = _snapshot_occurrence_ledgers(owner)
        assert before == {}
        _restore_occurrence_ledgers(owner, before)
    if with_dict:
        assert vars(store) == {}
    assert vars(barrier) == {"_store": store}


SCSB_LEDGERS = ("_new_billed_ids", "_current_billed_ids", "_adopted_pending_raw_keys",
                "_unsafe_pending_overlay_identities")


def _scsb_ram(store):
    return {name: deepcopy(getattr(store, name)) for name in SCSB_LEDGERS}


def _scsb_sql(conn):
    return tuple(conn.iterdump())


def _scsb_preimage(tmp_path, monkeypatch, nonempty):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(store_module.bank_pg, "enabled", lambda: False)
    store = BankStore("scsb", user_id=1, source_account_id=7)
    row = {"card_no": "****1234", "scope": "unbilled", "date": "2026-07-01",
           "desc": "synthetic purchase", "amount": 100, "currency": "TWD"}
    # Legacy seeding is deliberately not a credible refresh: the public producer
    # must be the first operation to observe the conflicting six-field overlays.
    store.refresh_card_pending("unbilled", [row, row], fetch_ok=None)
    ids = [record[0] for record in store.conn.execute("SELECT id FROM card_pending_txns ORDER BY id")]
    fields = ("category", "subcategory", "description_overwrite", "tags_overwrite",
              "auto_excluded", "splits_overwrite")
    overlays = (("food", "one", "original one", '["one"]', 0, '[{"amount":100}]'),
                ("travel", "two", "original two", '["two"]', 1, '[{"amount":40},{"amount":60}]'))
    for pending_id, overlay in zip(ids, overlays, strict=True):
        store.conn.execute("UPDATE card_pending_txns SET " + ",".join(f"{field}=?" for field in fields)
                           + " WHERE id=?", (*overlay, pending_id))
    if nonempty:
        store.upsert_card_billed([{**row, "date": "2026-06-01", "post_date": "2026-06-03"}])
        store._adopted_pending_raw_keys = {("unrelated receipt",): {"raw": Counter({("original",): 2})}}
        store._unsafe_pending_overlay_identities = {("unrelated overlay",)}
    store.conn.commit()
    store.conn.execute("BEGIN")
    store.put_daily_metric("synthetic_caller", {"keep": True}, "2026-07-01", commit=False)
    data = {"card_inquiry": {"leaves": {"unbilled": {"nav": {"ok": True}, "rows": [row]}}}}
    marker = ("exact", row["card_no"], row["date"], row["amount"], row["desc"])
    return store, data, marker


@pytest.mark.parametrize("route", ["direct", "collected"])
@pytest.mark.parametrize("nonempty", [False, True])
def test_scsb_valid_leaf_outer_release_restores_public_preimage(route, nonempty, tmp_path, monkeypatch):
    from backend.core.error_codes import exception_error_id

    store, data, marker = _scsb_preimage(tmp_path, monkeypatch, nonempty)
    before_ram, before_sql = _scsb_ram(store), _scsb_sql(store.conn)
    assert marker not in before_ram["_unsafe_pending_overlay_identities"]
    assert all(bool(value) == nonempty for value in before_ram.values())
    real = store.conn
    assert isinstance(real, sqlite3.Connection)
    denied, errors, operations = [], [], []

    def authorizer(action, operation, name, *_):
        if action == sqlite3.SQLITE_SAVEPOINT and name == "scsb_persist":
            operations.append(operation)
            if operation == "RELEASE" and not denied:
                denied.append(operation)
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    class ObserveConnection:
        def __getattr__(self, name):
            return getattr(real, name)

        def execute(self, sql, *args):
            if sql == "RELEASE SAVEPOINT scsb_persist" and not denied:
                assert marker in store._unsafe_pending_overlay_identities
                assert _scsb_ram(store) != before_ram
                assert _scsb_sql(real) != before_sql
                assert real.execute("SELECT COUNT(*) FROM card_pending_txns").fetchall()[0][0] == 1
                assert real.execute("SELECT COUNT(*) FROM sync_log").fetchall()[0][0] == 1
            try:
                return real.execute(sql, *args)
            except sqlite3.DatabaseError as error:
                errors.append(error)
                raise

    real.set_authorizer(authorizer)
    monkeypatch.setattr(store, "conn", ObserveConnection())
    try:
        with pytest.raises(sqlite3.DatabaseError, match="authorized") as caught:
            if route == "direct":
                persist.persist_scsb(data, store)
            else:
                persist.persist_collected("scsb", data, store)
        assert caught.value is errors[0]
        assert exception_error_id(caught.value) == "SYS-08-146-003"
        assert operations == ["BEGIN", "RELEASE", "ROLLBACK", "RELEASE"]
        assert real.in_transaction  # The caller owns the surviving SQL preimage.
        assert _scsb_sql(real) == before_sql
        print("SCSB SQL restored", route, nonempty, sha256(repr(before_sql).encode()).hexdigest(),
              "RAM preimage", sha256(repr(before_ram).encode()).hexdigest())
        assert _scsb_ram(store) == before_ram
    finally:
        real.set_authorizer(None)
        real.rollback()
        store.close()


@pytest.mark.parametrize("route", ["direct", "collected"])
@pytest.mark.parametrize("nonempty", [False, True])
def test_scsb_public_success_keeps_real_refresh_mutation(route, nonempty, tmp_path, monkeypatch):
    store, data, marker = _scsb_preimage(tmp_path, monkeypatch, nonempty)
    before = _scsb_ram(store)
    assert isinstance(store.conn, sqlite3.Connection)
    assert store.db_path is not None
    try:
        delta = (persist.persist_scsb(data, store) if route == "direct"
                 else persist.persist_collected("scsb", data, store))
        assert delta["card_unbilled"] == 1
        assert marker in store._unsafe_pending_overlay_identities
        assert all(getattr(store, name) == before[name] for name in SCSB_LEDGERS[:-1])
        assert not store.conn.in_transaction
        observer = sqlite3.connect(store.db_path)
        try:
            assert observer.execute("SELECT COUNT(*) FROM card_pending_txns").fetchone()[0] == 1
            assert observer.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0] == 1
        finally:
            observer.close()
    finally:
        store.close()


@pytest.mark.parametrize("route", ["direct", "collected"])
@pytest.mark.parametrize("fail", [False, True])
def test_scsb_public_no_ledger_proxy_is_unchanged(route, fail, tmp_path, monkeypatch):
    real, data, _ = _scsb_preimage(tmp_path, monkeypatch, False)
    assert isinstance(real.conn, sqlite3.Connection)
    before_sql = _scsb_sql(real.conn)

    class NoLedgerStore:
        def __getattr__(self, name):
            if name in SCSB_LEDGERS:
                raise AttributeError(name)
            return getattr(real, name)

    fake = NoLedgerStore()
    if fail:
        real.conn.execute("CREATE TEMP TRIGGER fail_scsb_fake BEFORE INSERT ON sync_log "
                          "BEGIN SELECT RAISE(ABORT, 'synthetic no-ledger failure'); END")
    try:
        if fail:
            with pytest.raises(sqlite3.IntegrityError, match="synthetic no-ledger failure"):
                if route == "direct":
                    getattr(persist, "persist_scsb")(data, fake)
                else:
                    persist.persist_collected("scsb", data, fake)
            assert _scsb_sql(real.conn) == before_sql
            assert real.conn.in_transaction
        else:
            if route == "direct":
                getattr(persist, "persist_scsb")(data, fake)
            else:
                persist.persist_collected("scsb", data, fake)
            assert not real.conn.in_transaction
        assert vars(fake) == {}
        assert all(not hasattr(fake, name) for name in SCSB_LEDGERS)
    finally:
        real.conn.rollback()
        real.close()
