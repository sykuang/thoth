"""Offline real delegated validators -> persister -> typed persistence terminal."""
from datetime import date
from importlib import import_module
from types import SimpleNamespace

import pytest

from backend.core.error_codes import exception_error_id, with_error_code
from backend.core.sync_errors import safe_failure_message
from backend.server.sync_runner import _PersistenceError


CASES = [
    ("fubon", "FUBON-07-115-001", "查詢開始日期解析：日期不存在或無法解析", "invalid Fubon TWD history result"),
    ("rakuten", "RAKUTEN-05-053-030", "臺幣歷史表格驗證：「busy」：「dom」檢查未通過", "invalid Rakuten history coverage"),
    ("taishin", "TAISHIN-05-057-001", "臺幣歷史傳輸驗證：原有例外處理路徑失敗", "invalid Taishin history"),
    ("ubot", "UBOT-07-402-001", "資料解析與發佈：原有拒絕分支：RuntimeError(error)", "invalid UBOT history coverage"),
]


def payload(bank, monkeypatch):
    fixtures = import_module(f"tests.test_{bank}_history_sync")
    module = import_module(f"backend.core.persist.{bank}")
    if bank == "fubon":
        data = fixtures._payload()
    elif bank == "rakuten":
        monkeypatch.setattr(module, "_today", lambda: date(2026, 9, 3))
        data = fixtures._persist_payload()
    elif bank == "ubot":
        monkeypatch.setattr(module, "_today", lambda: date(2026, 9, 1))
        data = fixtures._persist_payload()
    else:
        data = fixtures._attested_payload()
    return data


def corrupt(bank, data):
    if bank == "fubon":
        data["deposit_txn_results"][0]["start"] = "2026-02-30"
        data["history_coverage"]["domains"][0]["windows"][0]["start"] = "2026-02-30"
    elif bank == "rakuten":
        data["twd_txn_results"][0]["dom"]["busy"] = 1
    elif bank == "taishin":
        data["twd_txn_results"][0]["transport"]["url"] = "https://[invalid/"
    else:
        data["twd_txns"][0]["NTDetailList"][0]["TraDate"] = "2026/02/30"


def store_boundary():
    events = []
    store = SimpleNamespace(
        latest_twd_transaction_dates=lambda: {},
        rollback=lambda: events.append("rollback"),
        conn=SimpleNamespace(rollback=lambda: events.append("rollback")),
    )
    return store, events


def terminal(bank, data, store):
    try:
        getattr(import_module(f"backend.core.persist.{bank}"), f"persist_{bank}")(data, store)
    except ValueError as source:
        raise _PersistenceError("persistence_failed") from source


@pytest.mark.parametrize("bank,expected,chinese,text", CASES)
def test_actual_validator_rewrap_reaches_typed_parent(bank, expected, chinese, text, monkeypatch):
    data = payload(bank, monkeypatch)
    corrupt(bank, data)
    store, events = store_boundary()
    with pytest.raises(_PersistenceError) as caught:
        terminal(bank, data, store)
    replacement = caught.value.__cause__
    assert type(replacement) is ValueError
    assert str(replacement) == text
    assert replacement.__cause__ is None
    assert replacement.__suppress_context__ is True
    assert type(replacement.__context__) is RuntimeError
    assert exception_error_id(replacement.__context__, bank=bank) == expected
    assert exception_error_id(caught.value, bank=bank) == expected
    assert safe_failure_message(caught.value, bank, persistence=True) == (
        f"[{expected}] {chinese} | sync_failed:ValueError: phase=persistence"
    )
    assert events == (["rollback"] if bank in {"rakuten", "taishin"} else [])


@pytest.mark.parametrize("bank", [case[0] for case in CASES])
def test_valid_synthetic_coverage_is_still_accepted(bank, monkeypatch):
    from copy import deepcopy
    data = payload(bank, monkeypatch)
    before = deepcopy(data)
    module = import_module(f"backend.core.persist.{bank}")
    store, events = store_boundary()
    if bank == "fubon":
        rows = module._validated_fubon_twd_results_for_coverage(data)
        assert len(rows) == 2 and rows[0]["income"] == 5
    elif bank == "taishin":
        rows = module._validated_attested_twd_rows(data, store)
        assert rows == data["twd_txn_results"][0]["rows"]
    else:
        result = getattr(module, f"_validate_{bank}_history")(data, store)
        assert result.isoformat() == data["history_coverage"]["as_of"]
    assert data == before
    assert events == []


@pytest.mark.parametrize("bank,cls,helper,fallback", [
    ("fubon", "FubonCrawler", "_validated_twd_history_result", "FUBON-08-005-027"),
    ("rakuten", "RakutenCrawler", "_validated_history_result", "RAKUTEN-08-007-020"),
    ("taishin", "TaishinCrawler", "_validate_history_transport", "TAISHIN-08-001-041"),
    ("ubot", "UbotCrawler", "_strict_slash_date", "UBOT-08-004-052"),
])
def test_foreign_bank_source_retains_old_replacement_fallback(bank, cls, helper, fallback, monkeypatch):
    data = payload(bank, monkeypatch)
    source = with_error_code(RuntimeError("PRIVATE source"), "ESUN-07-421-001")
    calls = []
    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise source
    monkeypatch.setattr(getattr(import_module(f"backend.banks.{bank}"), cls), helper, staticmethod(fail))
    store, events = store_boundary()
    with pytest.raises(_PersistenceError) as caught:
        terminal(bank, data, store)
    assert len(calls) == 1
    replacement = caught.value.__cause__
    assert type(replacement) is ValueError
    assert replacement.__context__ is source
    assert replacement.__cause__ is None and replacement.__suppress_context__ is True
    assert exception_error_id(caught.value, bank=bank) == fallback
    assert "PRIVATE" not in safe_failure_message(caught.value, bank, persistence=True)
    assert events == (["rollback"] if bank in {"rakuten", "taishin"} else [])


@pytest.mark.parametrize("bank,helper,expected", [
    ("rakuten", "_iso_date", "RAKUTEN-08-005-003"),
    ("ubot", "_iso_date", "UBOT-08-003-003"),
])
def test_local_calendar_conversion_keeps_its_existing_terminal(bank, helper, expected):
    module = import_module(f"backend.core.persist.{bank}")
    with pytest.raises(ValueError, match="^fixture$") as caught:
        getattr(module, helper)("2026-02-30", "fixture")
    assert exception_error_id(caught.value, bank=bank) == expected


def test_untagged_ubot_delegated_conversion_keeps_old_fallback(monkeypatch):
    from backend.banks.ubot import UbotCrawler
    data = payload("ubot", monkeypatch)
    source = ValueError("PRIVATE untagged conversion")
    def fail(*args):
        raise source
    monkeypatch.setattr(UbotCrawler, "_strict_slash_date", staticmethod(fail))
    store, events = store_boundary()
    with pytest.raises(_PersistenceError) as caught:
        terminal("ubot", data, store)
    assert caught.value.__cause__.__context__ is source
    assert exception_error_id(caught.value, bank="ubot") == "UBOT-08-004-052"
    assert events == []
