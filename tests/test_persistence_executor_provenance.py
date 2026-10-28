"""Finite offline executor provenance: synthetic ingress, original rejection path.

No collector/live-bank certificate or database durability claim. Unlike the older
terminal helper, these checks execute the real dispatcher and _exec_sync.
"""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta
from importlib import import_module
import json
import sys
from zoneinfo import ZoneInfo

import pytest

from backend.core import base, persist, store
from backend.core.error_codes import exception_error_id
from backend.server import card_events, rules_repo, sync_jobs_repo, sync_runner as runner
from tests.test_persistence_replacement_provenance import CASES, corrupt


# Independent literal oracle; do not derive expected messages from the renderer.
NATURAL = [
    ("fubon", "FUBON-07-288-001", "驗證交易結果：處理欄位或原生操作失敗", "invalid Fubon TWD history result"),
    *CASES[1:],
]
VALIDATORS = {
    "fubon": ("FubonCrawler", "_validated_twd_history_result", "_validated_fubon_twd_results_for_coverage"),
    "rakuten": ("RakutenCrawler", "_validated_history_result", "_validate_rakuten_history"),
    "taishin": ("TaishinCrawler", "_validate_history_transport", "_validated_attested_twd_rows"),
    "ubot": ("UbotCrawler", "_strict_slash_date", "_validate_ubot_history"),
}


def _payload(bank):
    """Reuse synthetic fixtures, refresh time-sensitive ingress, never the clock."""
    fixtures = import_module(f"tests.test_{bank}_history_sync")
    if bank == "fubon":
        return fixtures._payload()
    if bank == "taishin":
        return fixtures._attested_payload()
    data = fixtures._persist_payload()
    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    domain = data["history_coverage"]["domains"][0]
    identity = domain["expected"][0]["identity"]
    if bank == "rakuten":
        month = today.replace(day=1)
        months = []
        for _ in range(6):
            months.append(month)
            month = (month - timedelta(days=1)).replace(day=1)
        results = []
        for start in reversed(months):
            result = fixtures._history_result(empty=start != months[0])
            next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
            result["selected_month"] = f"{start:%Y/%m} 活存明細"
            result["receipt"].update(start=start.isoformat(), end=min(today, next_month - timedelta(days=1)).isoformat())
            results.append(result)
        data["twd_txn_results"] = results
        windows = [{key: result["receipt"][key] for key in ("identity", "start", "end", "status", "pages")} for result in results]
    else:
        cls = fixtures.UbotCrawler
        results = []
        for start, end in cls._history_windows(cls._history_floor(today), today):
            rows = [fixtures._row()] if not results else []
            receipt = dict(identity=identity, start=start.isoformat(), end=end.isoformat(), status="complete" if rows else "explicit_empty", pages=1)
            results.append(dict(Account=identity, NTDetailList=rows, NTTotal={}, receipt={**receipt, "rows": len(rows)}))
        data["twd_txns"] = results
        windows = [{key: result["receipt"][key] for key in ("identity", "start", "end", "status", "pages")} for result in results]
    domain["windows"] = windows
    domain["expected"] = [{"identity": identity, "start": windows[0]["start"], "end": today.isoformat()}]
    data["history_coverage"]["as_of"] = today.isoformat()
    return data


class MemoryStore:
    """Fail on every financial mutation; transaction/cleanup counters only."""
    def __init__(self):
        self.calls = Counter()
        self.conn = self

    def latest_twd_transaction_dates(self):
        return {}

    latest_account_transaction_dates = latest_twd_transaction_dates
    latest_card_transaction_dates = latest_twd_transaction_dates

    def begin_transaction(self):
        self.calls["begin"] += 1

    def rollback(self):
        self.calls["rollback"] += 1

    def close(self):
        self.calls["close"] += 1

    def __getattr__(self, name):
        if name.startswith(("upsert_", "refresh_", "put_", "record_")) or name in {"commit", "log_sync", "stats"}:
            def forbidden(*args, **kwargs):
                self.calls["financial_write" if name != "stats" else "stats"] += 1
                raise AssertionError(f"unexpected store operation: {name}")
            return forbidden
        raise AssertionError(f"unexpected store attribute: {name}")


def _ingress(monkeypatch, bank, data):
    bank_mod = import_module(f"backend.banks.{bank}")
    cls = getattr(bank_mod, VALIDATORS[bank][0])
    assert cls.HISTORY_COVERAGE_REQUIRED is True
    assert frozenset({"twd_transactions"}) == cls.HISTORY_COVERAGE_DOMAINS
    crawler = object.__new__(cls)  # No constructor / BankCreds access.
    crawler.transaction_cursors = {}
    ingress = Counter()
    memory = MemoryStore()
    jobs = {71: dict(user_id=1, bank=bank, account_id=None, batch_id=None, history_mode="full", status="queued", error_msg=None)}

    def run(**kwargs):
        assert kwargs == {"login_url": bank_mod.BASE, "headless": True}
        ingress["run"] += 1
        return {"data": data}

    def factory():
        ingress["factory"] += 1
        return crawler

    def bank_store(*args, **kwargs):
        assert args == (bank,)
        assert kwargs == {"user_id": 1, "source_account_id": None}
        ingress["store"] += 1
        return memory

    def claim(job_id):
        assert jobs[job_id]["status"] == "queued"
        jobs[job_id]["status"] = "running"
        ingress["claim"] += 1
        return True

    def failed(job_id, error_msg):
        assert jobs[job_id]["status"] == "running"
        jobs[job_id].update(status="failed", error_msg=error_msg)
        ingress["mark_failed"] += 1

    def done(*args):
        pytest.fail("rejection must not mark done")

    notifications = []
    monkeypatch.setattr(crawler, "run", run)
    monkeypatch.setattr(runner, "_load_crawler", lambda key: (bank_mod, factory) if key == bank else pytest.fail("foreign bank"))
    monkeypatch.setattr(store, "BankStore", bank_store)
    monkeypatch.setattr(rules_repo, "list_rules", lambda **kwargs: [])
    monkeypatch.setattr(sync_jobs_repo, "get", lambda job_id: dict(jobs[job_id]))
    monkeypatch.setattr(sync_jobs_repo, "claim_queued", claim)
    monkeypatch.setattr(sync_jobs_repo, "mark_failed", failed)
    monkeypatch.setattr(sync_jobs_repo, "mark_done", done)
    monkeypatch.setattr(card_events, "snapshot_cards", lambda **kwargs: [])
    monkeypatch.setattr(runner, "_send_sync_notification", lambda **kwargs: notifications.append(kwargs))
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    monkeypatch.delenv("BANK_CRAWLER_ACCOUNT_ID", raising=False)
    return crawler, memory, ingress, notifications


def _execute(bank):
    """Observe originals without wrapping, replacing or injecting exceptions."""
    bank_mod = import_module(f"backend.banks.{bank}")
    adapter = import_module(f"backend.core.persist.{bank}")
    cls_name, leaf, validation = VALIDATORS[bank]
    targets = {
        "exec": runner._exec_sync,
        "safe_failure_message": runner.safe_failure_message,
        "dispatch": runner._dispatch_crawler_and_persist,
        "generic_coverage": base.validate_history_coverage,
        "persist_collected": persist.persist_collected,
        "public_persister": getattr(adapter, f"persist_{bank}"),
        "native_validation": getattr(adapter, validation),
        "delegated_validator": getattr(getattr(bank_mod, cls_name), leaf),
        "configure_cursor": base.BankCrawler.configure_transaction_cursor,
    }
    code_names = {fn.__code__: name for name, fn in targets.items()}
    calls = Counter()
    order = []
    caught = []

    def trace(frame, event, arg):
        if frame.f_code in code_names:
            if event == "call":
                name = code_names[frame.f_code]
                calls[name] += 1
                order.append(name)
            elif event == "exception" and frame.f_code is runner._exec_sync.__code__:
                caught.append(arg[1])
            return trace
        return None

    previous = sys.gettrace()
    try:
        sys.settrace(trace)
        returned = runner._exec_sync(71)
    finally:
        sys.settrace(previous)
    assert returned is True  # Executor returns a handled flag, not an error string.
    assert len(caught) == 1
    return caught[0], calls, order


@pytest.mark.parametrize("bank,expected,chinese,text", NATURAL)
def test_native_persistence_leaf_reaches_real_executor_sink(bank, expected, chinese, text, monkeypatch, capsys, record_property):
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    data = _payload(bank)
    crawler, memory, ingress, notifications = _ingress(monkeypatch, bank, data)
    # Genuine preflight: original metadata and native validation pass before the
    # one malformed ingress field. No generic guard/domain flags are bypassed.
    assert base.validate_history_coverage(data["history_coverage"], expected_mode="full", expected_domains=crawler.HISTORY_COVERAGE_DOMAINS)["ok"] is True
    adapter = import_module(f"backend.core.persist.{bank}")
    validate = getattr(adapter, VALIDATORS[bank][2])
    before = deepcopy(data)
    assert validate(data) if bank == "fubon" else validate(data, memory)
    assert data == before and not memory.calls
    if bank == "fubon":
        snapshot = data["deposit_txn_results"][0]["snapshot"]
        snapshot["gridRows"][0][0] = "2026/02/30"
        snapshot["gridText"] = "\n".join("\t".join(row) for row in snapshot["gridRows"])
    else:
        corrupt(bank, data)

    error, calls, order = _execute(bank)
    assert type(error) is runner._PersistenceError
    assert str(error) == "persistence_failed"
    replacement = error.__cause__
    assert type(replacement) is ValueError and str(replacement) == text
    assert replacement.__cause__ is None and replacement.__suppress_context__ is True
    assert type(replacement.__context__) is RuntimeError
    assert replacement.__context__.__cause__ is None
    assert replacement.__context__.__suppress_context__ is True
    assert exception_error_id(replacement.__context__, bank=bank) == expected
    assert exception_error_id(error, bank=bank) == expected
    message = f"[{expected}] {chinese} | sync_failed:ValueError: phase=persistence"
    stored = runner.get_job(71)
    assert stored is not None
    assert stored["status"] == "failed" and stored["error_msg"] == message
    assert notifications == [dict(user_id=1, bank=bank, ok=False, error_brief=runner._brief_error(message))]
    output = capsys.readouterr()
    assert output.err == f"[sync] {message}; details withheld\n" and output.out == ""
    assert text not in stored["error_msg"]
    assert calls == dict(exec=1, safe_failure_message=1, dispatch=1, generic_coverage=2 if bank == "fubon" else 3, persist_collected=1, public_persister=1, native_validation=1, delegated_validator=1, configure_cursor=3)
    assert order.index("generic_coverage") < order.index("persist_collected") < order.index("public_persister") < order.index("delegated_validator")
    assert crawler.transaction_cursors == {name: {} for name in ("twd_transactions", "account_transactions", "card_billed_transactions")}
    assert ingress == dict(factory=1, run=1, store=1, claim=1, mark_failed=1)
    # Wrapper owns atomic rollback too: Rakuten adapter + wrapper = two;
    # Taishin/UBOT wrapper only = one; Fubon has no atomic boundary here.
    assert memory.calls == ({"close": 1} if bank == "fubon" else {"begin": 1, "rollback": 2 if bank == "rakuten" else 1, "close": 1})
    assert memory.calls["financial_write"] == memory.calls["stats"] == 0
    record_property("executor_evidence", json.dumps(dict(bank=bank, source_id=expected, error_msg=message, calls=calls, order=order, store=dict(memory.calls), ingress=dict(ingress), replacement_type="ValueError", native_context_type="RuntimeError", from_none=True), ensure_ascii=False))


def test_fubon_invalid_start_is_correct_earlier_generic_coverage_control(monkeypatch, capsys, record_property):
    data = _payload("fubon")
    corrupt("fubon", data)  # Preserve the older invalid-result + invalid-metadata fixture.
    _, memory, ingress, _ = _ingress(monkeypatch, "fubon", data)
    error, calls, order = _execute("fubon")
    assert type(error) is ValueError
    # The source is shared SYS; only the safe job renderer bank-qualifies it.
    assert exception_error_id(error, bank="fubon") == "SYS-07-988-103"
    assert calls == dict(exec=1, safe_failure_message=1, dispatch=1, configure_cursor=3, generic_coverage=1)
    message = "[FUBON-07-988-103] 查詢視窗起始日期驗證：日期不符合日曆 | sync_failed:ValueError"
    stored = runner.get_job(71)
    assert stored is not None
    assert stored["error_msg"] == message
    assert stored["status"] == "failed"
    assert "FUBON-07-115-001" not in message and "phase=persistence" not in message
    output = capsys.readouterr()
    assert output.err == f"[sync] {message}; details withheld\n" and not output.out
    assert memory.calls == {"close": 1}
    assert memory.calls["financial_write"] == 0
    assert ingress == dict(factory=1, run=1, store=1, claim=1, mark_failed=1)
    record_property("executor_evidence", json.dumps(dict(bank="fubon", control=True, source_id="SYS-07-988-103", rendered_id="FUBON-07-988-103", error_msg=message, calls=calls, order=order, store=dict(memory.calls)), ensure_ascii=False))
