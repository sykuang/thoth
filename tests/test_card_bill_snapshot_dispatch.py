"""Pre-login freshness capture at both real persistence entry points (no bank calls)."""
from types import SimpleNamespace

import pytest

from backend.core import card_bills, persist, store as store_module
from backend.server import auto_debit_payments, rules_repo, sync_runner
from cli import cli


def _entry(monkeypatch, entry, bank, events, snapshot_error=None):
    marker = {}

    class Store:
        db_path = "synthetic-only"

        def __init__(self, *_args, **_kwargs):
            pass

        def latest_twd_transaction_dates(self):
            return {}

        def latest_account_transaction_dates(self):
            return {}

        def latest_card_transaction_dates(self):
            return {}

        def stats(self):
            return {}

        def close(self):
            events.append("close")

    class Crawler:
        HISTORY_COVERAGE_REQUIRED = False

        def configure_transaction_cursor(self, *_args):
            pass

        def run(self, **_kwargs):
            events.append("run")
            return {"data": {"card_bill_facts_ok": False}}

    def capture(_store):
        events.append("snapshot")
        if snapshot_error is not None:
            raise snapshot_error
        return marker

    def save(_bank, data, _store, **kwargs):
        events.append("persist")
        if bank == "dbs":
            assert kwargs["bill_snapshot"] is marker
        else:
            assert "bill_snapshot" not in kwargs
        assert "bill_snapshot" not in data
        return {}

    monkeypatch.delenv("BANK_CRAWLER_ACCOUNT_ID", raising=False)
    monkeypatch.setattr(card_bills, "capture_card_bill_snapshot", capture, raising=False)
    monkeypatch.setattr(rules_repo, "list_rules", lambda **_kwargs: [])
    monkeypatch.setattr(persist, "persist_collected", save)
    monkeypatch.setattr(auto_debit_payments, "apply_auto_debit_payments", lambda *_args: 0)
    if entry == "server":
        monkeypatch.setattr(store_module, "BankStore", Store)
        monkeypatch.setattr(sync_runner, "_load_crawler", lambda _bank: (SimpleNamespace(BASE="https://synthetic.invalid"), Crawler))
        return lambda: sync_runner._dispatch_crawler_and_persist(bank, user_id=1)
    monkeypatch.setattr(cli, "BankStore", Store)
    monkeypatch.setattr(cli, "_get_crawler", lambda _bank: (Crawler(), "https://synthetic.invalid"))
    monkeypatch.setattr(cli, "_write_private_json", lambda *_args: None)
    return lambda: cli._cmd_sync(SimpleNamespace(bank=bank, headless=True))


@pytest.mark.parametrize("entry", ["server", "cli"])
@pytest.mark.parametrize("bank", ["dbs", "cathay"])
def test_snapshot_is_captured_before_run_and_only_passed_for_dbs(monkeypatch, entry, bank):
    events = []
    run = _entry(monkeypatch, entry, bank, events)
    run()
    expected = ["snapshot", "run", "persist", "close"] if bank == "dbs" else ["run", "persist", "close"]
    assert events == expected


@pytest.mark.parametrize("entry", ["server", "cli"])
def test_snapshot_failure_aborts_before_bank_run_and_closes_store(monkeypatch, entry):
    events = []
    original = OSError("synthetic snapshot failure")
    run = _entry(monkeypatch, entry, "dbs", events, snapshot_error=original)
    with pytest.raises(OSError) as exc:
        run()
    assert exc.value is original
    assert events == ["snapshot", "close"]


def test_without_snapshot_keeps_legacy_store_call_signature(monkeypatch):
    calls = []
    store = object.__new__(store_module.BankStore)
    card = {"card_no": "****7001", "name": "synthetic", "association": None,
            "type": "credit", "is_cube": 0, "active": 1}

    def legacy_update(rows, *, commit=True):
        calls.append((rows, commit))
        return 1

    monkeypatch.setattr(store, "list_cards", lambda: [card])
    monkeypatch.setattr(store, "update_card_bill_facts", legacy_update)
    assert card_bills.apply_card_bill_facts(
        store, facts_ok=True,
        facts=[{"scope": "bank", "status": "no_payment_required", "remaining_due": 0}],
    ) == 1
    assert len(calls) == 1
    assert calls[0][0][0]["bill_due_amount"] == 0
    assert calls[0][1] is True
