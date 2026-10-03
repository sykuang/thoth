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
    TWD_HISTORY_URL,
    _fubon_form_action_matches,
    _fubon_history_windows,
    _fubon_preset_diagnostic_guard,
    _fubon_settled_control_guard,
    _validated_fubon_twd_options,
)
from backend.core.base import (
    BankCollectResult,
    ResponseCollector,
    _collect_failure_diagnostics,
)
from backend.core.persist import persist_collected
from backend.core.store import BankStore


ACCOUNT = "90000000267053"


def test_fubon_submit_binds_to_single_server_observed_action_preset_pair() -> None:
    source = inspect.getsource(FubonCrawler._collect_twd_window)
    assert "new FormData(form).getAll('checkedConvenientPeriod')" not in source
    assert "_fubon_settled_control_guard(settled, option, query_control_pairs)" in source
    assert "_control_action" not in source
    assert "submit_requests, preset_shapes" in source
    assert "radio_submitted = bounded_evaluate" in source
    assert "submits[0].click();" in source
    assert 'frame.click("#form1\\\\:doValidateAndSubmit"' not in source
    assert "all_responses_bound = all(" in source
    assert "data-hermes-stale-evidence" not in source
    assert "resultDigest" in source
    assert "selectedPreset" not in source
    assert "form?.contains(table)" in source
    assert "periods:periods.map" not in source
    assert "periods:digestPeriods.map" not in source
    assert "periodRanges" not in source
    assert "displayedStart" not in source
    assert "_periodGuard" not in source
    assert '"start": window["start"]' in source


def test_fubon_settled_control_diagnostics_are_closed() -> None:
    valid = {
        "value": "native", "text": f"account {ACCOUNT}", "detail": True,
        "fast": True, "preset": True,
        "formBound": True, "viewState": "state", "formAction": TWD_HISTORY_URL,
    }
    option = {"value": "native", "identity": ACCOUNT}
    observed = {("query-action", "server-value")}
    assert _fubon_settled_control_guard(valid, option, observed) is None
    assert _fubon_settled_control_guard(None, option, observed) == "fubon-twd-history-controls-settled-shape"
    assert _fubon_settled_control_guard({**valid, "value": "other"}, option, observed) == "fubon-twd-history-controls-account"
    assert _fubon_settled_control_guard({**valid, "detail": False}, option, observed) == "fubon-twd-history-controls-detail"
    assert _fubon_settled_control_guard({**valid, "viewState": ""}, option, observed) == "fubon-twd-history-controls-viewstate"



def test_fubon_settled_control_rejects_ambiguous_observed_pair() -> None:
    settled = {
        "value": "native", "text": f"account {ACCOUNT}", "detail": True,
        "fast": True, "preset": True,
        "formBound": True, "viewState": "state", "formAction": TWD_HISTORY_URL,
    }
    option = {"value": "native", "identity": ACCOUNT}

    assert _fubon_settled_control_guard(
        settled,
        option,
        {("query-action", "control-preset"), ("other-action", "other-preset")},
    ) == "fubon-twd-history-controls-preset"


def _coverage(*, status="complete"):
    return {
        "version": 1,
        "mode": "full",
        "domains": [{
            "domain": "twd_transactions",
            "expected": [{"identity": ACCOUNT, "start": "2025-08-30", "end": "2026-08-30"}],
            "windows": [
                {"identity": ACCOUNT, "start": "2025-08-30", "end": "2026-02-27", "status": status, "pages": 1},
                {"identity": ACCOUNT, "start": "2026-02-28", "end": "2026-08-30", "status": status, "pages": 1},
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
            "matchingResponseCount": 1,
            "frameBound": True,
            "requestBound": True,
            "presetBound": True,
            "fieldsBound": True,
            "viewStateBound": True,
            "actionBound": True,
            "formBound": True,
            "allResponsesBound": True,
        },
        "snapshot": {
            "resultFresh": True,
            "busy": False,
            "failed": False,
            "selectedValue": "012-000-90000000267053-X-TW",
            "selectedIdentity": ACCOUNT,

            "hasGrid": not empty,
            "gridCandidateCount": 0 if empty else 1,
            "hiddenGridCount": 0,
            "pagerNodeCount": 0,
            "structuralErrorCount": 0,
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
            _result("2025-08-30", "2026-02-27", "2025-09-02"),
            _result("2026-02-28", "2026-08-30", "2026-08-29"),
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
        {"preset": "rdoDay180_365", "start": "2025-08-30", "end": "2026-02-27"},
        {"preset": "rdoDay180", "start": "2026-02-28", "end": "2026-08-30"},
    ]


def test_fubon_incremental_uses_live_verified_native_window(monkeypatch):
    crawler = object.__new__(FubonCrawler)
    crawler.transaction_cursors = {"twd_transactions": {ACCOUNT: date(2026, 8, 20)}}
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "incremental")
    assert crawler._history_windows(ACCOUNT, date(2026, 8, 30)) == [
        {"preset": "rdoDay180", "start": "2026-02-28", "end": "2026-08-30"},
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


def test_fubon_inventory_accepts_native_account_value() -> None:
    value = f"012-00{ACCOUNT}-TWD-00"
    options = [
        {"index": 0, "value": "none", "text": "==請選擇=="},
        {"index": 1, "value": value, "text": f"{ACCOUNT} (測試分行)"},
    ]

    assert _validated_fubon_twd_options(options) == [
        {"index": 1, "value": value, "text": f"{ACCOUNT} (測試分行)", "identity": ACCOUNT},
    ]


def test_fubon_result_requires_transport_account_range_and_complete_dom():
    valid = _result("2025-08-30", "2026-02-27", "2025-09-02")
    mutations = (
        lambda item: item.update(url="https://ebank.taipeifubon.com.tw/B2C/wrong.faces"),
        lambda item: item.update(url="https://ebank.taipeifubon.com.tw/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces;attacker"),
        lambda item: item["transport"].update(status=204),
        lambda item: item["transport"].update(responseCount=0),
        lambda item: item["transport"].update(allResponsesBound=False),
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

        lambda item: item["snapshot"].update(failed=True),
        lambda item: item["snapshot"].update(nativeTotalFound=False),
        lambda item: item["snapshot"].update(nativeTotalMarkerCount=2),
        lambda item: item["snapshot"].update(rawDataRowCount=2),
        lambda item: item["snapshot"].update(hiddenGridCount=1),
        lambda item: item["snapshot"].update(pagerNodeCount=1),
        lambda item: item["snapshot"].update(structuralErrorCount=1),
        lambda item: item["snapshot"].update(malformedRowCount=1),
        lambda item: item["snapshot"].update(hiddenRowCount=1),
        lambda item: item["snapshot"].update(hiddenCellCount=1),
    )
    for mutate in mutations:
        item = deepcopy(valid)
        mutate(item)
        with pytest.raises(RuntimeError, match="fubon-twd-history-result"):
            FubonCrawler._validated_twd_history_result(item)


@pytest.mark.parametrize(
    ("key", "value", "guard"),
    [
        ("resultFresh", False, "freshness"),
        ("busy", True, "busy"),
        ("failed", True, "failed"),
        ("selectedValue", "wrong", "account-value"),
        ("selectedIdentity", "90000000267054", "account-identity"),

    ],
)
def test_fubon_result_snapshot_diagnostics_are_closed(
    key: str, value, guard: str,
) -> None:
    item = _result("2026-02-28", "2026-08-30", "2026-08-29")
    item["snapshot"][key] = value

    with pytest.raises(
        RuntimeError,
        match=rf"^fubon-twd-history-result-{guard}$",
    ) as raised:
        FubonCrawler._validated_twd_history_result(item)
    diagnostics = _collect_failure_diagnostics(
        object.__new__(FubonCrawler), raised.value,
    )
    assert diagnostics["guard"] == f"fubon-twd-history-result-{guard}"



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
        response, hits, correct, True, [request],
    )
    assert hits == [{
        "status": 200,
        "contentType": "text/plain",
        "frameBound": True,
        "requestBound": True,
        "_navigation": False,
        "_presetValue": "rdoDay180",
        "presetBound": True,
        "fieldsBound": True,
        "viewStateBound": True,
        "actionBound": True,
        "formBound": True,
    }]
    rejected_request = SimpleNamespace(
        method="POST",
        frame=correct,
        post_data="ajaxAction=&checkedConvenientPeriod=rdoDay180&javax.faces.ViewState=",
    )
    rejected_response = SimpleNamespace(
        url=correct.url,
        request=rejected_request,
        status=200,
        headers={"content-type": "text/plain; charset=UTF-8"},
    )
    rejected = []
    FubonCrawler._capture_twd_response(
        rejected_response, rejected, correct, True, [rejected_request],
    )
    assert len(rejected) == 1
    assert rejected[0]["presetBound"] is True
    assert rejected[0]["actionBound"] is False

    oversized_request = SimpleNamespace(
        method="POST",
        frame=correct,
        post_data="x" * 32_769,
    )
    oversized_response = SimpleNamespace(
        url=correct.url,
        request=oversized_request,
        status=200,
        headers={"content-type": "text/plain; charset=UTF-8"},
    )
    FubonCrawler._capture_twd_response(
        oversized_response, hits, correct,
        True, [oversized_request],
    )
    assert len(hits) == 2
    assert hits[-1]["fieldsBound"] is False


@pytest.mark.parametrize(
    ("mode", "expected_error"),
    [
        ("pre-cleanup", "fubon-twd-history-transport-no-bound-response"),
        ("during-cleanup", "fubon-twd-history-result-mutated"),
        ("teardown-pending", "fubon-twd-history-transport-pending"),
        ("teardown-failed", "fubon-twd-history-transport-failed"),
        ("teardown-response", "fubon-twd-history-transport-no-bound-response"),
        ("radio-wrong", "fubon-twd-history-controls-preset"),
        ("guard-false", "fubon-twd-history-result-mutated"),
        ("guard-error", "synthetic guard setup"),
        ("listener-error", "fubon-twd-history-listener-cleanup"),
        ("transport-reject", "fubon-twd-history-transport"),
        ("transport-cleanup-error", "fubon-twd-history-transport"),
        ("no-update", "fubon-twd-history-result-freshness"),
        ("companion", None),
        ("document-replaced", "fubon-twd-history-result-mutated"),
        ("normal", None),
    ],
)
def test_fubon_settles_all_same_frame_responses_before_dom_publication(
    mode: str,
    expected_error: str | None,
) -> None:
    listeners: dict[str, list] = {}
    snapshot = deepcopy(_result("2026-02-28", "2026-08-30", "2026-08-29")["snapshot"])

    class Request:
        method = "POST"

        def __init__(self, frame, post_data):
            self.frame = frame
            self.post_data = post_data
            self.url = TWD_HISTORY_URL

    class Response:
        def __init__(self, request, content_type="text/plain; charset=UTF-8"):
            self.request = request
            self.url = request.url
            self.headers = {"content-type": content_type}
            self.status = (
                201 if mode in {"transport-reject", "transport-cleanup-error"} else 200
            )

    class EvalLocator:
        def __init__(self, frame):
            self.frame = frame

        def evaluate(self, expression, arg=None, **_kwargs):
            if "account.value=args.value" in expression:
                return {
                    "ok": True,
                    "value": self.frame.option["value"],
                    "text": f"account {ACCOUNT}",
                    "viewState": "state-1",
                    "formBound": True,
                    "formAction": TWD_HISTORY_URL,
                }
            if "nodes[0].checked===true : null" in expression:
                return False
            if "nodes[0].click()" in expression:
                self.frame.page.emit_control_request(self.frame)
                return True
            if "preset:preset.checked" in expression:
                return {
                    "value": self.frame.option["value"],
                    "text": f"account {ACCOUNT}",
                    "detail": True,
                    "fast": True,
                    "preset": True,
                    "viewState": "state-1",
                    "formBound": True,
                    "formAction": TWD_HISTORY_URL,
                }
            if "submits[0].click()" in expression:
                if mode == "radio-wrong":
                    return False
                self.frame.page.emit_submit_requests(self.frame)
                return True
            if "gridRows:projected" in expression:
                digest = "00000000" if mode == "no-update" else "11111111"
                return {"href": TWD_HISTORY_URL, **snapshot, "resultDigest": digest}
            if "present,digest:hash(signature)" in expression:
                return {"present": True, "digest": "00000000"}
            if "__hermesFubonDomGuard=new MutationObserver" in expression:
                self.frame.guard_active = True
                if mode == "guard-error":
                    raise RuntimeError("synthetic guard setup")
                return mode != "guard-false"
            if "observer?.takeRecords" in expression:
                self.frame.guard_active = False
                return {
                    "sameDocument": True,
                    "submitDocument": mode != "document-replaced",
                    "mutations": int(mode == "during-cleanup"),
                }
            if "delete window.__hermesFubonDomGuard" in expression:
                if mode == "transport-cleanup-error":
                    raise RuntimeError("synthetic dom cleanup")
                self.frame.guard_active = False
                return None
            raise AssertionError("unexpected Fubon evaluate")

    class Frame:
        url = TWD_HISTORY_URL
        name = "txnFrame"

        def __init__(self, option):
            self.option = option
            self.page = None
            self.guard_active = False

        def locator(self, selector):
            assert selector == "html"
            return EvalLocator(self)

        def click(self, _selector, **_kwargs):
            raise AssertionError("submit must be atomic with the final radio check")

    class Page:
        def __init__(self, frame):
            self.frames = [frame]
            frame.page = self

        def on(self, event, callback):
            listeners.setdefault(event, []).append(callback)

        def remove_listener(self, event, callback):
            if mode == "teardown-response" and event == "response":
                request = Request(
                    self.frames[0],
                    "ajaxAction=late&checkedConvenientPeriod=late&javax.faces.ViewState=late",
                )
                self._emit("response", Response(request))
            listeners[event].remove(callback)
            if event == "response" and mode in {"teardown-pending", "teardown-failed"}:
                request = Request(
                    self.frames[0],
                    "ajaxAction=late&checkedConvenientPeriod=late&javax.faces.ViewState=late",
                )
                self._emit("request", request)
                if mode == "teardown-failed":
                    self._emit("requestfailed", request)
            if mode == "during-cleanup" and event == "response":
                snapshot["gridRows"][0][2] = "LATE-UNBOUND"
            if mode == "listener-error" and event == "response":
                raise RuntimeError("synthetic listener cleanup failure")

        def wait_for_timeout(self, _milliseconds):
            return None

        def close(self):
            self.frames[0].guard_active = False

        @staticmethod
        def _emit(event, value):
            for callback in tuple(listeners.get(event, ())):
                callback(value)

        def emit_control_request(self, frame):
            request = Request(
                frame,
                "ajaxAction=query-action&checkedConvenientPeriod=server-value&javax.faces.ViewState=state-1",
            )
            self._emit("request", request)
            self._emit("requestfinished", request)

        def emit_submit_requests(self, frame):
            bound = Request(
                frame,
                "ajaxAction=query-action&checkedConvenientPeriod=server-value&javax.faces.ViewState=state-1",
            )
            if mode == "companion":
                requests = (
                    Request(frame, "nativeSubmit=1"),
                    bound,
                )
            elif mode != "pre-cleanup":
                requests = (bound,)
            else:
                snapshot["gridRows"][0][2] = "UNBOUND-NOISE"
                requests = (
                    bound,
                    Request(
                        frame,
                        "ajaxAction=query-action&checkedConvenientPeriod=server-value&javax.faces.ViewState=state-1&extra=noise",
                    ),
                )
            for request in requests:
                self._emit("request", request)
                content_type = "text/html; charset=UTF-8" if mode == "companion" and request is requests[0] else "text/plain; charset=UTF-8"
                self._emit("response", Response(request, content_type))
                self._emit("requestfinished", request)

    option = {
        "value": f"012-000-{ACCOUNT}-X-TW",
        "identity": ACCOUNT,
    }
    frame = Frame(option)
    page = Page(frame)
    crawler = object.__new__(FubonCrawler)
    crawler._is_owned_frame = lambda _page, candidate: candidate is frame

    if expected_error is None:
        result, receipt = crawler._collect_twd_window(
            page,
            frame,
            option,
            {"preset": "rdoDay180", "start": "2026-02-28", "end": "2026-08-30"},
        )
        assert result["snapshot"]["gridRows"][0][2] != "LATE-UNBOUND"
        if mode == "pre-cleanup":
            assert result["snapshot"]["gridRows"][0][2] == "UNBOUND-NOISE"
        assert receipt["status"] == "complete"
        if mode == "companion":
            assert result["transport"]["responseCount"] == 2
            assert result["transport"]["matchingResponseCount"] == 1
            assert result["transport"]["allResponsesBound"] is True
    else:
        with pytest.raises(RuntimeError, match=expected_error):
            crawler._collect_twd_window(
                page,
                frame,
                option,
                {"preset": "rdoDay180", "start": "2026-02-28", "end": "2026-08-30"},
            )
    assert frame.guard_active is False


@pytest.mark.parametrize(
    ("preset_fields", "expected_shape", "expected_guard"),
    [
        ("", "missing", "fubon-twd-history-transport-preset-missing"),
        ("&checkedConvenientPeriod=a&checkedConvenientPeriod=b", "duplicate", "fubon-twd-history-transport-preset-duplicate"),
        ("&checkedConvenientPeriod=", "mismatch", "fubon-twd-history-transport-preset-mismatch"),
        ("&checkedConvenientPeriod=rdoDay180", "present", None),
        ("&checkedConvenientPeriod=server-native-value", "present", None),
    ],
)
def test_fubon_preset_shape_diagnostics_are_closed(
    preset_fields: str,
    expected_shape: str,
    expected_guard: str | None,
) -> None:
    frame = SimpleNamespace()
    request = SimpleNamespace(
        method="POST",
        frame=frame,
        post_data=(
            "ajaxAction=query-action&javax.faces.ViewState=state-1" + preset_fields
        ),
    )
    response = SimpleNamespace(
        url="https://ebank.taipeifubon.com.tw/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces",
        request=request,
        status=200,
        headers={"content-type": "text/plain"},
    )
    shapes: list[str] = []
    FubonCrawler._capture_twd_response(
        response, hits := [], frame, True, [request], shapes,
    )
    assert shapes == [expected_shape]
    assert _fubon_preset_diagnostic_guard(hits, shapes) == expected_guard


def test_fubon_preset_diagnostic_guard_does_not_override_bound_response() -> None:
    assert _fubon_preset_diagnostic_guard(
        [{"presetBound": True}], ["mismatch"],
    ) is None
    assert _fubon_preset_diagnostic_guard(
        [{"presetBound": False}], [],
    ) is None


def test_fubon_window_stages_account_dependent_controls_after_ajax_settle():
    source = inspect.getsource(FubonCrawler._collect_twd_window)
    assert '("requestfinished", control_finished)' in source
    assert '("requestfailed", control_failed_request)' in source
    assert "remove_control_listeners()" in source
    assert source.count("add_control_listeners()") >= 2
    assert 'click_control("#form1\\\\:rdoTxDetail")' in source
    assert 'click_control("#form1\\\\:rdoFast")' in source
    assert "f\"#form1\\\\:{window['preset']}\"" in source
    assert "control_pending or control_failed or stable_ticks < 10" in source
    assert 'settled["viewState"]' in inspect.getsource(_fubon_settled_control_guard)
    assert source.index("stable_ticks < 10") < source.index("settled_guard =")
    assert "expected_preset: str | None = None" in source
    assert "before_checked is True and expected_preset is None" in source
    assert "if not preset or len(preset) > 128" in source
    assert 'expected_preset=window["preset"]' in source
    assert "query_control_pairs = click_control" in source
    assert "_control_action" not in source
    assert "query_control_pairs.add" not in source
    assert '("request", submit_request)' in source
    assert '("requestfinished", submit_finished)' in source
    assert '("requestfailed", submit_failed)' in source
    assert "if submit_pending:" in source
    assert "if submit_failed_flag:" in source
    assert "if stable_ticks < 5:" in source
    assert "if submit_request_count != len(hits):" in source
    assert "contextlib.suppress(Exception)" not in source


def test_fubon_transport_diagnostic_guards_are_transport_safe() -> None:
    reasons = {
        "failed", "no-bound-response",
        "no-action-bound-response", "no-fields-bound-response", "no-form-bound-response",
        "no-frame-bound-response", "no-preset-bound-response", "no-viewstate-bound-response",
        "preset-duplicate", "preset-mismatch", "preset-missing",
        "no-response", "pending", "quiescence", "request-count",
        "too-many-responses",
    }
    assert {
        f"fubon-twd-history-transport-{reason}" for reason in reasons
    } <= FubonCrawler.SAFE_COLLECT_GUARDS


def test_fubon_result_accepts_native_account_value_identity_group() -> None:
    valid = _result("2026-02-28", "2026-08-30", "2026-08-29")
    valid["account_value"] = f"012-00{ACCOUNT}-TWD-00"
    valid["snapshot"]["selectedValue"] = valid["account_value"]

    assert FubonCrawler._validated_twd_history_result(valid)["identity"] == ACCOUNT


def test_fubon_result_rejects_pager_busy_and_ambiguous_empty():
    valid = _result("2026-02-28", "2026-08-30", "2026-08-29")
    assert FubonCrawler._validated_twd_history_result(valid)["status"] == "complete"
    for mutation in ("pager", "busy", "empty", "count", "stale"):
        bad = deepcopy(valid)
        if mutation == "pager": bad["snapshot"]["pager"] = {"present": True, "actionableNext": 1}
        elif mutation == "busy": bad["snapshot"]["busy"] = True
        elif mutation == "empty": bad["snapshot"]["emptyMarker"] = "查無相關資料"
        elif mutation == "count": bad["snapshot"]["totalCount"] = 2
        else: bad["snapshot"]["resultFresh"] = False
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
            "expected": [{"identity": ACCOUNT, "start": "2026-02-28", "end": "2026-08-30"}],
            "windows": [{
                "identity": ACCOUNT, "start": "2026-02-28", "end": "2026-08-30",
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


def test_fubon_history_menu_route_requires_exact_owned_query():
    crawler = object.__new__(FubonCrawler)
    crawler._is_owned_frame = lambda page, frame: True
    base = "https://ebank.taipeifubon.com.tw/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces"
    for suffix in ("?menuId=CDS0401", "?menuId=CDS0401&"):
        menu = SimpleNamespace(url=base + suffix)
        page = SimpleNamespace(frames=[menu])
        assert crawler._fubon_content_frame(
            page, "/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces"
        ) is menu

    for suffix in (
        "?account=PRIVATE", "?menuId=private", "?menuId=CDS0401&&",
        "?menuId=CDS0401&menuId=CDS0401", "?menuId=", "?menuId=CDS0401&extra=1",
        ";attacker",
    ):
        page.frames = [SimpleNamespace(url=base + suffix)]
        with pytest.raises(RuntimeError, match="fubon-twd-history-frame"):
            crawler._fubon_content_frame(
                page, "/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces"
            )


def test_fubon_history_form_action_accepts_only_native_menu_query_variants():
    base = "https://ebank.taipeifubon.com.tw/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces"
    assert _fubon_form_action_matches(base)
    assert _fubon_form_action_matches(base + "?menuId=CDS0401")
    assert _fubon_form_action_matches(base + "?menuId=CDS0401&")
    for rejected in (
        base + "?menuId=CDS0401&extra=1",
        "http://ebank.taipeifubon.com.tw" + base.split(".com.tw", 1)[1],
        "https://evil.example" + base.split(".com.tw", 1)[1],
        base + "#fragment",
        "https://ebank.taipeifubon.com.tw:bad/",
    ):
        assert not _fubon_form_action_matches(rejected)


def test_fubon_native_window_boundaries_match_live_semantics():
    # Live 2026-10-02: rdoDay180 starts six calendar months back; rdoDay180_365 also returns that first day.
    from backend.banks.fubon import _fubon_history_windows
    assert _fubon_history_windows(date(2026, 10, 2)) == [
        {"preset": "rdoDay180_365", "start": "2025-10-02", "end": "2026-04-01"},
        {"preset": "rdoDay180", "start": "2026-04-02", "end": "2026-10-02"},
    ]
    assert _fubon_history_windows(date(2026, 8, 31))[1]["start"] == "2026-02-28"
