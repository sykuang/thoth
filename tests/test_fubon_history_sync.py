from __future__ import annotations

import ast
from copy import deepcopy
from datetime import date
import inspect
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from backend.banks.fubon import (
    FubonCrawler,
    _fubon_history_windows,
    _validated_fubon_twd_options,
)
from backend.core.base import BankCollectResult, ResponseCollector
from backend.core.persist import persist_collected
from backend.core.store import BankStore


ACCOUNT = "90000000267053"


def _coverage(*, status="complete"):
    return {
        "version": 1,
        "mode": "full",
        "domains": [{
            "domain": "twd_transactions",
            "expected": [{"identity": ACCOUNT, "start": "2025-08-30", "end": "2026-08-30"}],
            "windows": [
                {"identity": ACCOUNT, "start": "2025-08-30", "end": "2026-03-02", "status": status, "pages": 1},
                {"identity": ACCOUNT, "start": "2026-03-03", "end": "2026-08-30", "status": status, "pages": 1},
            ],
        }],
    }


def _result(start, end, txn_date, *, empty=False):
    rows = [] if empty else [[txn_date.replace("-", "/"), f"{txn_date.replace('-', '/')} 12:00:00", "利息", "", "5.00", "84.00", ""]]
    return {
        "account_no": ACCOUNT,
        "account_value": "012-000-90000000267053-X-TW",
        "preset": "rdoDay180_365" if start == "2025-08-30" else "rdoDay180",
        "start": start,
        "end": end,
        "status": "explicit_empty" if empty else "complete",
        "url": "https://ebank.taipeifubon.com.tw/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces",
        "transport": {
            "status": 200,
            "contentType": "text/plain",
            "responseCount": 1,
            "frameBound": True,
            "presetBound": True,
            "fieldsBound": True,
            "viewStateBound": True,
            "actionBound": True,
            "formBound": True,
        },
        "snapshot": {
            "evidenceFresh": True,
            "busy": False,
            "failed": False,
            "selectedValue": "012-000-90000000267053-X-TW",
            "selectedIdentity": ACCOUNT,
            "selectedPreset": "rdoDay180_365" if start == "2025-08-30" else "rdoDay180",
            "windowBound": True,
            "displayedStart": start,
            "displayedEnd": end,
            "hasGrid": not empty,
            "gridCandidateCount": 0 if empty else 1,
            "hiddenGridCount": 0,
            "pagerNodeCount": 0,
            "structuralErrorCount": 0,
            "resultContainerBound": True,
            "gridRows": rows,
            "gridRowCount": len(rows),
            "rawDataRowCount": len(rows),
            "malformedRowCount": 0,
            "hiddenRowCount": 0,
            "hiddenCellCount": 0,
            "totalCount": len(rows),
            "nativeTotalFound": not empty,
            "nativeTotalMarkerCount": 0 if empty else 1,
            "gridText": "" if empty else "\n".join("\t".join(r) for r in rows),
            "emptyMarker": "查無相關資料" if empty else None,
            "pager": {"present": False, "actionableNext": 0},
        },
    }


def _payload():
    return {
        "history_coverage": _coverage(),
        "accounts": [{"account_no": ACCOUNT, "currency": "TWD", "type": "deposit", "name": "台幣存款"}],
        "deposit_txn_results": [
            _result("2025-08-30", "2026-03-02", "2025-09-02"),
            _result("2026-03-03", "2026-08-30", "2026-08-29"),
        ],
        "deposit_page_text": (
            f"{ACCOUNT}\n\t活儲存款\t測試分行\t臺幣\t84\t84\t快速功能"
        ),
        "card_bill_facts_ok": False,
    }


def test_fubon_opts_into_twd_history_only():
    assert FubonCrawler.HISTORY_COVERAGE_REQUIRED is True
    assert frozenset({"twd_transactions"}) == FubonCrawler.HISTORY_COVERAGE_DOMAINS


def test_fubon_twd_history_runs_before_optional_credit_card_flow(monkeypatch):
    source = inspect.getsource(FubonCrawler.collect)
    assert source.index("_collect_attested_twd_history") < source.index("if not candidates")
    assert 'out["error"] = "no_credit_card_items"' not in source
    result = BankCollectResult(accounts=_payload()["accounts"])
    assert result.to_dict()["accounts"][0]["account_no"] == ACCOUNT

    class EmptyCardFrame:
        url = "https://ebank.taipeifubon.com.tw/B2C/cgequ/cgequ001/CGEQU001_Home.faces"
        name = "txnFrame"

        def locator(self, _selector):
            return self

        def evaluate(self, _expression, _arg=None, **_kwargs):
            return []

    frame = EmptyCardFrame()
    page = SimpleNamespace(
        url=frame.url,
        frames=[frame],
        goto=lambda *_args, **_kwargs: None,
        wait_for_timeout=lambda *_args: None,
    )
    crawler = object.__new__(FubonCrawler)
    history = _payload()
    monkeypatch.setattr(FubonCrawler, "_collect_attested_twd_history", lambda self, page: {
        key: history[key] for key in ("accounts", "deposit_txn_results", "history_coverage")
    })
    collected = crawler.collect(page, ResponseCollector())
    assert collected.history_coverage == history["history_coverage"]
    assert collected.accounts == history["accounts"]


def test_fubon_full_history_uses_two_native_six_month_windows(monkeypatch):
    crawler = object.__new__(FubonCrawler)
    crawler.transaction_cursors = {"twd_transactions": {ACCOUNT: date(2026, 8, 20)}}
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    assert crawler._history_windows(ACCOUNT, date(2026, 8, 30)) == [
        {"preset": "rdoDay180_365", "start": "2025-08-30", "end": "2026-03-02"},
        {"preset": "rdoDay180", "start": "2026-03-03", "end": "2026-08-30"},
    ]


def test_fubon_incremental_uses_live_verified_native_window(monkeypatch):
    crawler = object.__new__(FubonCrawler)
    crawler.transaction_cursors = {"twd_transactions": {ACCOUNT: date(2026, 8, 20)}}
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "incremental")
    assert crawler._history_windows(ACCOUNT, date(2026, 8, 30)) == [
        {"preset": "rdoDay180", "start": "2026-03-03", "end": "2026-08-30"},
    ]


def test_fubon_embedded_javascript_compiles():
    tree = ast.parse(Path(inspect.getfile(FubonCrawler)).read_text())
    sources = [
        call.args[1].value
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "bounded_evaluate"
        and len(call.args) > 1
        and isinstance(call.args[1], ast.Constant)
        and isinstance(call.args[1].value, str)
    ]
    assert len(sources) >= 4
    subprocess.run(
        ["node", "-e", "for(const s of JSON.parse(process.argv[1]))new Function('return ('+s+');')", json.dumps(sources)],
        check=True,
    )


def test_fubon_defaults_to_full_even_when_cursor_exists(monkeypatch):
    crawler = object.__new__(FubonCrawler)
    crawler.transaction_cursors = {"twd_transactions": {ACCOUNT: date(2026, 8, 20)}}
    monkeypatch.delenv("BANK_CRAWLER_HISTORY_MODE", raising=False)
    assert crawler._history_windows(ACCOUNT, date(2026, 8, 30)) == _fubon_history_windows(date(2026, 8, 30))


def test_fubon_missing_cursor_falls_back_to_native_full(monkeypatch):
    crawler = object.__new__(FubonCrawler)
    crawler.transaction_cursors = {"twd_transactions": {}}
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "incremental")
    assert crawler._history_windows(ACCOUNT, date(2026, 8, 30)) == _fubon_history_windows(date(2026, 8, 30))


def test_fubon_inventory_requires_exact_unique_twd_options():
    options = [
        {"index": 0, "value": "none", "text": "==請選擇=="},
        {"index": 1, "value": "012-000-90000000267053-X-TW", "text": f"{ACCOUNT} (測試分行)"},
    ]
    assert _validated_fubon_twd_options(options) == [
        {"index": 1, "value": "012-000-90000000267053-X-TW", "text": f"{ACCOUNT} (測試分行)", "identity": ACCOUNT},
    ]
    for bad in (
        [*options, deepcopy(options[1])],
        [options[0], {**options[1], "index": 2, "value": "arbitrary-TW"}],
        [options[0], {**options[1], "index": 2}],
        [
            options[0],
            {"index": 1, "value": "012-000-99123456789012-X-TW", "text": "1234567890 (測試分行)"},
        ],
        [options[0]],
        [*options, {"index": 2, "value": "012-0000000000000002-US", "text": "90000000267054 (測試分行)"}],
        [*options, {"index": 2, "value": "loading", "text": "資料載入中"}],
    ):
        with pytest.raises(ValueError, match="inventory"):
            _validated_fubon_twd_options(bad)


def test_fubon_result_requires_transport_account_range_and_complete_dom():
    valid = _result("2025-08-30", "2026-03-02", "2025-09-02")
    mutations = (
        lambda item: item.update(url="https://ebank.taipeifubon.com.tw/B2C/wrong.faces"),
        lambda item: item.update(url="https://ebank.taipeifubon.com.tw/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces;attacker"),
        lambda item: item["transport"].update(status=204),
        lambda item: item["transport"].update(responseCount=2),
        lambda item: item["transport"].update(frameBound=False),
        lambda item: item["transport"].update(presetBound=False),
        lambda item: item["transport"].update(fieldsBound=False),
        lambda item: item["transport"].update(viewStateBound=False),
        lambda item: item["transport"].update(actionBound=False),
        lambda item: item["transport"].update(formBound=False),
        lambda item: item.update(
            url="https://ebank.taipeifubon.com.tw/b2c/cdsqu/cdsqu001/cdsqu001_home.faces",
        ),
        lambda item: item["snapshot"].update(selectedIdentity="90000000267054"),
        lambda item: item["snapshot"].update(selectedValue="012-000-90000000267054-X-TW"),
        lambda item: item["snapshot"].update(selectedPreset="rdoDay30"),
        lambda item: item["snapshot"].update(windowBound=False),
        lambda item: item["snapshot"].update(displayedStart="2025-08-31"),
        lambda item: item["snapshot"].update(displayedEnd="2026-03-01"),
        lambda item: item["snapshot"].update(
            displayedStart="2026-03-02", displayedEnd="2025-08-30", windowBound=True,
        ),
        lambda item: item["snapshot"].update(failed=True),
        lambda item: item["snapshot"].update(nativeTotalFound=False),
        lambda item: item["snapshot"].update(nativeTotalMarkerCount=2),
        lambda item: item["snapshot"].update(rawDataRowCount=2),
        lambda item: item["snapshot"].update(hiddenGridCount=1),
        lambda item: item["snapshot"].update(pagerNodeCount=1),
        lambda item: item["snapshot"].update(structuralErrorCount=1),
        lambda item: item["snapshot"].update(resultContainerBound=False),
        lambda item: item["snapshot"].update(malformedRowCount=1),
        lambda item: item["snapshot"].update(hiddenRowCount=1),
        lambda item: item["snapshot"].update(hiddenCellCount=1),
    )
    for mutate in mutations:
        item = deepcopy(valid)
        mutate(item)
        with pytest.raises(RuntimeError, match="fubon-twd-history-result"):
            FubonCrawler._validated_twd_history_result(item)


def test_fubon_frame_and_post_binding_are_exact(monkeypatch):
    crawler = object.__new__(FubonCrawler)
    crawler._is_owned_frame = lambda page, frame: page is not None and frame is not None
    correct = SimpleNamespace(
        url="https://ebank.taipeifubon.com.tw/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces",
        name="",
    )
    misleading = SimpleNamespace(
        url="https://ebank.taipeifubon.com.tw/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces;attacker",
        name="txnFrame",
    )
    lowercase = SimpleNamespace(
        url="https://ebank.taipeifubon.com.tw/b2c/cdsqu/cdsqu001/cdsqu001_home.faces",
        name="",
    )
    page = SimpleNamespace(frames=[misleading, lowercase, correct])
    assert crawler._fubon_content_frame(page, "/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces") is correct
    monkeypatch.setattr(
        FubonCrawler, "_fubon_content_frame", lambda self, page, *routes: lowercase,
    )
    with pytest.raises(RuntimeError):
        crawler._bound_twd_result_frame(page, correct)

    request = SimpleNamespace(
        method="POST",
        frame=correct,
        post_data="ajaxAction=query-action&checkedConvenientPeriod=rdoDay180&javax.faces.ViewState=state-1",
    )
    response = SimpleNamespace(
        url=correct.url,
        request=request,
        status=200,
        headers={"content-type": "text/plain; charset=UTF-8"},
    )
    hits = []
    FubonCrawler._capture_twd_response(
        response, hits, correct, "rdoDay180", "state-1", "query-action", True,
    )
    assert hits == [{
        "status": 200,
        "contentType": "text/plain",
        "frameBound": True,
        "presetBound": True,
        "fieldsBound": True,
        "viewStateBound": True,
        "actionBound": True,
        "formBound": True,
    }]


def test_fubon_result_rejects_pager_busy_and_ambiguous_empty():
    valid = _result("2026-03-03", "2026-08-30", "2026-08-29")
    assert FubonCrawler._validated_twd_history_result(valid)["status"] == "complete"
    for mutation in ("pager", "busy", "empty", "count", "stale"):
        bad = deepcopy(valid)
        if mutation == "pager": bad["snapshot"]["pager"] = {"present": True, "actionableNext": 1}
        elif mutation == "busy": bad["snapshot"]["busy"] = True
        elif mutation == "empty": bad["snapshot"]["emptyMarker"] = "查無相關資料"
        elif mutation == "count": bad["snapshot"]["totalCount"] = 2
        else: bad["snapshot"]["evidenceFresh"] = False
        with pytest.raises(RuntimeError, match="fubon-twd-history-result"):
            FubonCrawler._validated_twd_history_result(bad)


def test_fubon_valid_attested_payload_persists_and_advances_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("fubon", user_id=7, source_account_id=97)
    try:
        data = _payload()
        data["deposit_page_text"] = ""
        delta = persist_collected("fubon", data, store)
        assert delta["accounts_new"] == 1
        assert delta["twd_txn_new"] == 2
        assert store.latest_twd_transaction_dates() == {ACCOUNT: date(2026, 8, 30)}
        assert tuple(store.conn.execute(
            "SELECT DISTINCT typeof(income), typeof(balance) FROM twd_transactions"
        ).fetchall()[0]) == ("integer", "integer")
    finally:
        store.close()


def test_fubon_attested_persistence_ignores_unattested_twd_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    data = _payload()
    data["deposit_page_text"] = (
        f"{ACCOUNT}\n\t外匯活存\t測試分行\t美元\t999\t999\t快速功能"
        "\n90000000267054\n\t活儲存款\t其他分行\t臺幣\t10\t10\t快速功能"
    )
    store = BankStore("fubon", user_id=7, source_account_id=97)
    try:
        assert persist_collected("fubon", data, store)["accounts_new"] == 1
        rows = store.conn.execute("SELECT account_no, currency, raw_balance FROM accounts").fetchall()
        assert [(row["account_no"], row["currency"], row["raw_balance"]) for row in rows] == [
            (ACCOUNT, "TWD", None),
        ]
    finally:
        store.close()


def test_fubon_structured_rows_fail_closed_before_write(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    for index, value in (
        (2, ""), (3, "1,2,3"), (3, "-1"), (4, "NaN"), (4, "5.5"),
        (4, "2,147,483,648"), (5, "Infinity"),
    ):
        store = BankStore("fubon", user_id=7, source_account_id=97)
        data = _payload()
        data["deposit_txn_results"][0]["snapshot"]["gridRows"][0][index] = value
        try:
            with pytest.raises(ValueError):
                persist_collected("fubon", data, store)
            assert all(count == 0 for count in store.stats().values())
            assert store.latest_twd_transaction_dates() == {}
        finally:
            store.close()
    for cells in (
        ["2025/09/02", "2025/09/02 10:00:00", "薪資", "", "5.00", "10.00"],
        ["2025/09/02", "2025/09/02 10:00:00", "薪資", "", "5.00", "10.00", "", "unexpected"],
    ):
        store = BankStore("fubon", user_id=7, source_account_id=97)
        data = _payload()
        data["deposit_txn_results"][0]["snapshot"]["gridRows"] = [cells]
        try:
            with pytest.raises(ValueError):
                persist_collected("fubon", data, store)
            assert all(count == 0 for count in store.stats().values())
        finally:
            store.close()


def test_fubon_full_coverage_requires_native_presets_before_write(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("fubon", user_id=7, source_account_id=97)
    data = _payload()
    data["deposit_txn_results"][0]["preset"] = "rdoCustom"
    data["deposit_txn_results"][0]["snapshot"]["selectedPreset"] = "rdoCustom"
    try:
        with pytest.raises(ValueError):
            persist_collected("fubon", data, store)
        assert all(value == 0 for value in store.stats().values())
        assert store.latest_twd_transaction_dates() == {}
    finally:
        store.close()


def test_fubon_incremental_rejects_unverified_custom_preset(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    data = _payload()
    result = deepcopy(data["deposit_txn_results"][1])
    result["preset"] = "rdoCustom"
    result["start"] = "2026-08-13"
    result["snapshot"]["selectedPreset"] = "rdoCustom"
    result["snapshot"]["displayedStart"] = "2026-08-13"
    data["deposit_txn_results"] = [result]
    data["history_coverage"] = {
        "version": 1,
        "mode": "incremental",
        "domains": [{
            "domain": "twd_transactions",
            "expected": [{"identity": ACCOUNT, "start": "2026-08-13", "end": "2026-08-30"}],
            "windows": [{
                "identity": ACCOUNT, "start": "2026-08-13", "end": "2026-08-30",
                "status": "complete", "pages": 1,
            }],
        }],
    }
    store = BankStore("fubon", user_id=7, source_account_id=97)
    try:
        with pytest.raises(ValueError):
            persist_collected("fubon", data, store)
        assert all(value == 0 for value in store.stats().values())
    finally:
        store.close()


def test_fubon_incremental_native_window_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    data = _payload()
    result = data["deposit_txn_results"][1]
    data["deposit_txn_results"] = [result]
    data["history_coverage"] = {
        "version": 1,
        "mode": "incremental",
        "domains": [{
            "domain": "twd_transactions",
            "expected": [{"identity": ACCOUNT, "start": "2026-03-03", "end": "2026-08-30"}],
            "windows": [{
                "identity": ACCOUNT, "start": "2026-03-03", "end": "2026-08-30",
                "status": "complete", "pages": 1,
            }],
        }],
    }
    store = BankStore("fubon", user_id=7, source_account_id=97)
    try:
        assert persist_collected("fubon", data, store)["twd_txn_new"] == 1
    finally:
        store.close()


def test_fubon_rejects_generic_empty_inventory_attestation(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    data = _payload()
    data["accounts"] = []
    data["deposit_txn_results"] = []
    data["history_coverage"]["domains"][0] = {
        "domain": "twd_transactions",
        "expected": [],
        "windows": [],
        "empty_window": {
            "start": "2025-08-30", "end": "2026-08-30",
            "status": "explicit_empty", "pages": 1,
        },
    }
    store = BankStore("fubon", user_id=7, source_account_id=97)
    try:
        with pytest.raises(ValueError):
            persist_collected("fubon", data, store)
        assert all(value == 0 for value in store.stats().values())
    finally:
        store.close()


def test_fubon_card_fact_validation_precedes_every_write(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("fubon", user_id=7, source_account_id=97)
    data = _payload()
    data["card_bill_facts_ok"] = True
    data["card_bill_facts"] = [{"scope": "bank", "status": "unpaid", "remaining_due": -1}]
    try:
        with pytest.raises(ValueError):
            persist_collected("fubon", data, store)
        assert all(value == 0 for value in store.stats().values())
        assert store.latest_twd_transaction_dates() == {}
    finally:
        store.close()


def test_fubon_missing_or_mismatched_coverage_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    for mutation in ("missing", "pager", "identity"):
        store = BankStore("fubon", user_id=7, source_account_id=97)
        data = _payload()
        if mutation == "missing": data.pop("history_coverage")
        elif mutation == "pager": data["deposit_txn_results"][0]["snapshot"]["pager"] = {"present": True, "actionableNext": 1}
        else: data["deposit_txn_results"][0]["account_no"] = "90000000267054"
        try:
            with pytest.raises(ValueError):
                persist_collected("fubon", data, store)
            assert all(value == 0 for value in store.stats().values())
            assert store.latest_twd_transaction_dates() == {}
        finally:
            store.close()
