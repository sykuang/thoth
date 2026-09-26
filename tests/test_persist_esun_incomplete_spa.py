"""Incomplete modern E.SUN data must not reach adapter writes."""
import pytest

from backend.core.persist import persist_collected
from backend.core.persist.esun import persist_esun
from backend.core.store import BankStore


class RecordingStore:
    begin_transaction = BankStore.begin_transaction

    def __init__(self):
        self.calls = []
        self.conn = self

    def execute(self, sql, *args, **kwargs):
        # Transaction control is allowed; no query or data write may reach SQL.
        assert sql == "SAVEPOINT bank_store_transaction"
        assert args == () and kwargs == {}
        self.calls.append("savepoint")

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append(name)
            return 0
        return record


class Hostile(str):
    def __bool__(self):
        raise AssertionError("synthetic bool must not run")

    def __eq__(self, other):
        raise AssertionError("synthetic eq must not run")

    def __str__(self):
        raise AssertionError("synthetic str must not run")


class FalseyText(str):
    def __bool__(self):
        return False


@pytest.mark.parametrize("error", [
    "synthetic error", False, 0, [], {}, Hostile(""), Hostile("synthetic"),
    FalseyText("synthetic"),
], ids=["text", "false", "zero", "list", "dict", "hostile-empty", "hostile-text", "falsey-text"])
@pytest.mark.parametrize("entry", ["direct", "direct-no-commit", "dispatch"])
def test_error_only_rejected_before_store_access(error, entry):
    store = RecordingStore()
    data = {"error": error}
    with pytest.raises(ValueError) as caught:
        if entry == "dispatch":
            data.update({
                "history_coverage": {"mode": "full", "domains": [{
                    "domain": "twd_transactions", "expected": [], "windows": [],
                    "empty_window": {"start": "2026-01-01", "end": "2026-01-01",
                                     "status": "explicit_empty", "pages": 1},
                }]},
                "twd_txn_results": [], "card_bill_facts_ok": False,
            })
            persist_collected("esun", data, store)
        else:
            persist_esun(data, store, commit=entry == "direct")  # type: ignore[arg-type]
    assert str(caught.value) == "E.SUN collection result contains an error"
    assert store.calls == (["savepoint", "rollback"] if entry == "dispatch" else [])


@pytest.mark.parametrize("carrier", [None, [], [{"desc": "synthetic"}]])
@pytest.mark.parametrize("commit", [False, True])
def test_unsupported_carrier_rejected_independently(carrier, commit):
    store = RecordingStore()
    with pytest.raises(ValueError) as caught:
        persist_esun({"twd_txns": carrier}, store, commit=commit)  # type: ignore[arg-type]
    assert str(caught.value) == "unsupported E.SUN normalized TWD payload"
    assert store.calls == []


@pytest.mark.parametrize("data", [{}, {"error": None}, {"error": ""}])
@pytest.mark.parametrize("commit", [False, True])
def test_legacy_empty_error_keeps_existing_behavior(data, commit):
    store = RecordingStore()
    persist_esun(data, store, commit=commit)  # type: ignore[arg-type]
    assert store.calls == ["refresh_card_pending", "log_sync"]
