from __future__ import annotations

import ast
from calendar import monthrange
from copy import deepcopy
from datetime import date, timedelta
import inspect
import json
from pathlib import Path
import sqlite3
import textwrap
from types import SimpleNamespace

import pytest

from backend.banks.sinopac import SinopacCrawler, _sinopac_hidden_changed_reason
from backend.core import bank_pg
from backend.core.base import ApiHit, BankCollectResult, ResponseCollector, validate_history_coverage
from backend.core.persist import sinopac as sinopac_persist_module
from backend.core.persist import persist_collected
from backend.core.persist.sinopac import persist_sinopac
from backend.core.store import BankStore
from backend.server.sync_jobs_repo import _is_full_history_attestation


ACCOUNT = "01234567890123"
LABEL = "測試帳戶 01234567890123"


def _sinopac_dom_scripts() -> tuple[str, str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(SinopacCrawler._collect_transactions)))
    dom_probe = next(
        ast.literal_eval(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "dom_probe" for target in node.targets)
    )
    final_expression = next(
        node.args[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "evaluate"
        and node.args
        and "async () =>" in ast.unparse(node.args[0])
    )
    finalizer = eval(
        compile(ast.Expression(final_expression), inspect.getfile(SinopacCrawler), "eval"),
        {"dom_probe": dom_probe},
    )
    return dom_probe, finalizer


def _sinopac_row_html(*, row_id: str = "", hidden: bool = False) -> str:
    attributes = f' id="{row_id}"' if row_id else ""
    if hidden:
        attributes += ' style="display:none"'
    return (
        f"<tr{attributes}><td>x0</td><td>2026/08/01</td><td>摘要</td>"
        "<td>5</td><td></td><td>備註</td><td>x6</td><td>末欄</td></tr>"
    )


def _sinopac_expected_row() -> list[str]:
    return ["unused", "2026/08/01", "摘要", "-5", "備註", "", "", "末欄", "", "", ""]


def test_sinopac_hidden_prior_rows_require_exact_multiset() -> None:
    from patchright.sync_api import sync_playwright

    dom_probe, _finalizer = _sinopac_dom_scripts()
    with sync_playwright() as patchright:
        if not Path(patchright.chromium.executable_path).exists():
            pytest.skip("Patchright browser binary is not installed")
        browser = patchright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(
                '<table id="ListingTable"><tbody><tr><td>查無資料</td></tr>'
                + _sinopac_row_html(hidden=True)
                + _sinopac_row_html(hidden=True)
                + "</tbody></table>"
            )
            page.evaluate(
                """(prior) => {
                  const empty=document.querySelector('#ListingTable tbody tr td');
                  window.__hermesSinopacExpectedRows=[];
                  window.__hermesSinopacPriorRows=prior;
                  window.__hermesSinopacState={mutations:1,freshEmptyNodes:new WeakSet([empty]),staleHiddenRows:new WeakMap()};
                }""",
                [_sinopac_expected_row()],
            )

            result = page.evaluate(dom_probe)

            assert result["hiddenChangedRows"] == 2
            assert result["hiddenChangedPriorRows"] == 1
            assert result["bound"] is False
        finally:
            browser.close()


def test_sinopac_final_probe_rechecks_computed_visibility_after_cssom_macrotask() -> None:
    from patchright.sync_api import sync_playwright

    dom_probe, finalizer = _sinopac_dom_scripts()
    with sync_playwright() as patchright:
        if not Path(patchright.chromium.executable_path).exists():
            pytest.skip("Patchright browser binary is not installed")
        browser = patchright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(
                "<style></style><table id='ListingTable'><tbody>"
                + _sinopac_row_html(row_id="result-row")
                + "</tbody></table>"
            )
            page.evaluate(
                """(expected) => {
                  window.__hermesSinopacExpectedRows=expected;
                  window.__hermesSinopacPriorRows=[];
                  window.__hermesSinopacState={mutations:1,freshEmptyNodes:new WeakSet(),staleHiddenRows:new WeakMap()};
                  window.__hermesSinopacObserver=new MutationObserver(records => window.__hermesSinopacState.mutations+=records.length);
                  window.__hermesSinopacObserver.observe(document.body,{subtree:true,childList:true,attributes:true,characterData:true});
                  window.__hermesSinopacObserverTimer=setTimeout(()=>{},35000);
                }""",
                [_sinopac_expected_row()],
            )
            before = page.evaluate(dom_probe)
            page.evaluate(
                """() => { const nativeSetTimeout=window.setTimeout.bind(window); let armed=true;
                  window.setTimeout=(callback,delay,...args)=>{
                    if(armed&&delay===0){armed=false;
                      document.styleSheets[0].insertRule('#result-row{visibility:hidden}');}
                    return nativeSetTimeout(callback,delay,...args);
                  };
                }"""
            )

            after = page.evaluate(finalizer)

            assert before["visibleRows"] == 1
            assert after != before
            assert after["visibleRows"] == 0
            assert page.evaluate("() => '__hermesSinopacObserverTimer' in window") is False
        finally:
            browser.close()


def test_sinopac_final_probe_does_not_execute_overridden_geometry_getters() -> None:
    from patchright.sync_api import sync_playwright

    dom_probe, finalizer = _sinopac_dom_scripts()
    with sync_playwright() as patchright:
        if not Path(patchright.chromium.executable_path).exists():
            pytest.skip("Patchright browser binary is not installed")
        browser = patchright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(
                "<style></style><table id='ListingTable'><tbody>"
                + _sinopac_row_html(row_id="result-row")
                + "</tbody></table>"
            )
            page.evaluate(
                """(expected) => {
                  window.__hermesSinopacExpectedRows=expected;
                  window.__hermesSinopacPriorRows=[];
                  window.__hermesSinopacState={mutations:1,freshEmptyNodes:new WeakSet(),staleHiddenRows:new WeakMap()};
                  window.__hermesSinopacObserver=new MutationObserver(records => window.__hermesSinopacState.mutations+=records.length);
                  window.__hermesSinopacObserver.observe(document.body,{subtree:true,childList:true,attributes:true,characterData:true});
                  window.__hermesSinopacObserverTimer=setTimeout(()=>{},35000);
                }""",
                [_sinopac_expected_row()],
            )
            before = page.evaluate(dom_probe)
            page.evaluate(
                """() => { const row=document.querySelector('#result-row'); window.__geometryReads=0;
                  window.__geometryArmed=false; window.__geometryQueued=false;
                  Object.defineProperty(row,'offsetWidth',{configurable:true,get(){
                    window.__geometryReads+=1;
                    if(window.__geometryArmed&&!window.__geometryQueued){window.__geometryQueued=true;
                      queueMicrotask(()=>document.styleSheets[0].insertRule('#result-row{visibility:hidden}'));}
                    return 100;
                  }});
                  const nativeSetTimeout=window.setTimeout.bind(window);
                  window.setTimeout=(callback,delay,...args)=>{
                    if(delay===0)window.__geometryArmed=true;
                    return nativeSetTimeout(callback,delay,...args);
                  };
                }"""
            )

            after = page.evaluate(finalizer)

            assert after == before
            assert page.evaluate(
                "() => getComputedStyle(document.querySelector('#result-row')).visibility"
            ) == "visible"
            assert page.evaluate("() => window.__geometryReads") == 0
        finally:
            browser.close()


def test_sinopac_final_probe_supports_non_html_elements_and_cleans_up() -> None:
    from patchright.sync_api import sync_playwright

    _dom_probe, finalizer = _sinopac_dom_scripts()
    with sync_playwright() as patchright:
        if not Path(patchright.chromium.executable_path).exists():
            pytest.skip("Patchright browser binary is not installed")
        browser = patchright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(
                "<svg></svg><table id='ListingTable'><tbody>"
                + _sinopac_row_html(row_id="result-row")
                + "</tbody></table>"
            )
            page.evaluate(
                """(expected) => {
                  window.__hermesSinopacExpectedRows=expected;
                  window.__hermesSinopacPriorRows=[];
                  window.__hermesSinopacState={mutations:1,freshEmptyNodes:new WeakSet(),staleHiddenRows:new WeakMap()};
                  window.__hermesSinopacObserver=new MutationObserver(()=>{});
                  window.__hermesSinopacObserver.observe(document.body,{subtree:true,childList:true,attributes:true,characterData:true});
                  window.__hermesSinopacObserverTimer=setTimeout(()=>{},35000);
                }""",
                [_sinopac_expected_row()],
            )

            result = page.evaluate(finalizer)

            assert result["visibleRows"] == 1
            assert page.evaluate("() => '__hermesSinopacObserverTimer' in window") is False
        finally:
            browser.close()


def test_sinopac_final_probe_cleans_up_when_sampling_throws() -> None:
    from patchright.sync_api import Error, sync_playwright

    _dom_probe, finalizer = _sinopac_dom_scripts()
    with sync_playwright() as patchright:
        if not Path(patchright.chromium.executable_path).exists():
            pytest.skip("Patchright browser binary is not installed")
        browser = patchright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content("<table id='ListingTable'><tbody></tbody></table>")
            page.evaluate(
                """() => {
                  window.__hermesSinopacExpectedRows=[]; window.__hermesSinopacPriorRows=[];
                  window.__hermesSinopacState={mutations:1,freshEmptyNodes:new WeakSet(),staleHiddenRows:new WeakMap()};
                  window.__hermesSinopacObserver=new MutationObserver(()=>{});
                  window.__hermesSinopacObserver.observe(document.body,{subtree:true,childList:true,attributes:true,characterData:true});
                  window.__hermesSinopacObserverTimer=setTimeout(()=>{},35000);
                  Element.prototype.getClientRects=()=>{throw new Error('synthetic');};
                }"""
            )

            with pytest.raises(Error):
                page.evaluate(finalizer)

            assert page.evaluate(
                "() => ['__hermesSinopacObserverTimer','__hermesSinopacObserver','__hermesSinopacState',"
                "'__hermesSinopacExpectedRows','__hermesSinopacPriorRows'].some(key => key in window)"
            ) is False
        finally:
            browser.close()


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ({"hiddenChangedRows": 1, "hiddenChangedFullTransactionRows": 1}, "empty-hidden-full-transaction"),
        ({"hiddenChangedRows": 1, "hiddenChangedDateRows": 1}, "empty-hidden-date-cell"),
        ({"hiddenChangedRows": 1, "hiddenChangedNumericRows": 1}, "empty-hidden-numeric-cell"),
        ({"hiddenChangedRows": 1, "hiddenChangedControlRows": 1}, "empty-hidden-control"),
        ({"hiddenChangedRows": 1, "hiddenChangedHeaderRows": 1}, "empty-hidden-header"),
        ({"hiddenChangedRows": 1, "hiddenChangedHeaderRows": 0}, "empty-hidden-template-text"),
    ],
)
def test_sinopac_hidden_changed_reason_is_closed(state, reason) -> None:
    assert _sinopac_hidden_changed_reason(state) == reason


@pytest.fixture(autouse=True)
def _freeze_persistence_today(monkeypatch):
    monkeypatch.setattr(sinopac_persist_module, "_today", lambda: date(2026, 8, 31))


def _inventory_hit() -> ApiHit:
    return ApiHit(
        url="https://mma.sinopac.com/ws/bank/transdetail/ws_debitacct.ashx",
        raw_url="https://mma.sinopac.com/ws/bank/transdetail/ws_debitacct.ashx?1788171564478",
        method="POST",
        status=200,
        content_type="application/json; charset=utf-8",
        body_size=300,
        request_sequence=1,
        main_frame_request=True,
        resp_json=[{
            "Header": "SUCCESS",
            "Message": "",
            "SubInfo": [{
                "DataText": LABEL,
                "DataValue": ACCOUNT,
                "DisplayText": "TWD",
            }],
        }],
    )


def _row(*, when: str = "2026/08/31<br />12:34") -> dict:
    return {
        "DataText1": when,
        "DataText2": "2026/08/31",
        "DataText3": "利息存入",
        "DataText4": "+30",
        "DataText5": "1,030",
        "DataText6": "",
        "DataText7": "測試票號",
        "DataText8": "測試備註",
        "DataText9": "測試用途",
        "DataText10": "",
        "DataText11": "",
    }


def _history_hit(
    *, start: str = "20260801", end: str = "20260831", rows=None,
    message: str | None = None,
) -> ApiHit:
    rows = [_row()] if rows is None else rows
    if message is None:
        message = "" if rows else "查無資料"
    body = {
        "BeginDate": "20250901",
        "DefBeginDate": "20260701",
        "DefEndDate": "20260731",
        "EndDate": "20260831",
        "HeadInfo": [{
            "FieldKey": f"DataText{i}",
            "HeadText": f"欄位{i}",
            "FieldWidth": "10",
            "HeadAlign": "L",
            "DataAlign": "L",
            "OrderIndex": str(i),
            "MainShow": "Y",
            "DetailShow": "Y",
        } for i in range(1, 10)],
        "Header": "SUCCESS",
        "MaxMonth": "3",
        "Message": message,
        "RecordCount": "0" if rows else None,
        "SubInfo": rows,
        "isOBU": "Y",
    }
    return ApiHit(
        url="https://mma.sinopac.com/ws/bank/transdetail/ws_transdetailMerge.ashx",
        raw_url="https://mma.sinopac.com/ws/bank/transdetail/ws_transdetailMerge.ashx?1788171564478",
        method="POST",
        status=200,
        req_body=(
            f"Acct={LABEL}&AcctName=&AcctValue={ACCOUNT}&BusinessDate=20260831&"
            f"Curr=TWD&CurrName=&EndDate={end}&QueryType=3&StartDate={start}&TextType="
        ),
        content_type="application/json; charset=utf-8",
        body_size=2_000,
        request_sequence=2,
        main_frame_request=True,
        resp_json=[body],
    )


def _coverage(*, mode: str = "full", start: str = "2025-09-01", end: str = "2026-08-31") -> dict:
    cursor = date.fromisoformat(start)
    finish = date.fromisoformat(end)
    windows = []
    while cursor <= finish:
        window_end = min(
            finish, date(cursor.year, cursor.month, monthrange(cursor.year, cursor.month)[1]),
        )
        windows.append({
            "identity": ACCOUNT,
            "start": cursor.isoformat(),
            "end": window_end.isoformat(),
            "status": "complete" if window_end == finish else "explicit_empty",
            "pages": 1,
        })
        cursor = window_end + timedelta(days=1)
    return {
        "mode": mode,
        "as_of": end,
        "domains": [{
            "domain": "account_transactions",
            "expected": [{"identity": ACCOUNT, "start": start, "end": end}],
            "windows": windows,
        }],
    }


def _persist_payload(*, mode: str = "full") -> dict:
    start = "2025-09-01" if mode == "full" else "2026-08-24"
    coverage = _coverage(mode=mode, start=start)
    history_results = []
    for window in coverage["domains"][0]["windows"]:
        final = window["end"] == "2026-08-31"
        rows = [_row()] if final else []
        history_results.append({
            "account": ACCOUNT,
            "account_name": LABEL,
            "currency": "TWD",
            "records": rows,
            "receipt": {**window, "rows": len(rows)},
        })
    return BankCollectResult(
        bank_balance=[{
            "Header": "SUCCESS",
            "Message": "",
            "SubInfo": [{
                "AcctValue": ACCOUNT,
                "Curr": "TWD",
                "AvailBalance": "1030",
                "AcctText": "測試帳戶",
            }],
        }],
        account_transactions=history_results,
        debit_accounts=[{"label": LABEL, "identity": ACCOUNT, "currency": "TWD"}],
        history_coverage=coverage,
        card_bill_facts_ok=False,
    ).to_dict()


def test_sinopac_opts_into_all_currency_account_history() -> None:
    assert frozenset({"account_transactions"}) == SinopacCrawler.HISTORY_COVERAGE_DOMAINS
    assert SinopacCrawler.HISTORY_COVERAGE_REQUIRED is True


def test_sinopac_full_windows_cover_latest_year_by_calendar_month() -> None:
    end = date(2026, 8, 31)
    start = SinopacCrawler._history_floor(end)
    windows = SinopacCrawler._history_windows(start, end)

    assert start == date(2025, 9, 1)
    assert windows[0] == (date(2025, 9, 1), date(2025, 9, 30))
    assert windows[-1] == (date(2026, 8, 1), end)
    assert len(windows) == 12
    assert all(a_start.month == a_end.month for a_start, a_end in windows)
    assert all(
        right_start == left_end + timedelta(days=1)
        for (_, left_end), (right_start, _) in zip(windows, windows[1:])
    )


def test_sinopac_incremental_range_uses_identity_cursor_and_rejects_future() -> None:
    crawler = object.__new__(SinopacCrawler)
    crawler.transaction_cursors = {"account_transactions": {ACCOUNT: date(2026, 8, 20)}}
    assert crawler._history_range(ACCOUNT, end=date(2026, 8, 31), mode="incremental") == (
        date(2026, 8, 13), date(2026, 8, 31),
    )
    crawler.transaction_cursors = {"account_transactions": {ACCOUNT: date(2026, 9, 1)}}
    for mode in ("full", "incremental"):
        with pytest.raises(RuntimeError, match="sinopac-twd-history-cursor"):
            crawler._history_range(ACCOUNT, end=date(2026, 8, 31), mode=mode)


@pytest.mark.parametrize(
    ("mutation", "guard"),
    [
        ("method", "sinopac-twd-history-inventory-envelope"),
        ("body", "sinopac-twd-history-inventory-envelope"),
        ("host", "sinopac-twd-history-inventory-envelope"),
        ("path", "sinopac-twd-history-inventory-envelope"),
        ("status", "sinopac-twd-history-inventory-envelope"),
        ("mime", "sinopac-twd-history-inventory-envelope"),
        ("redirect", "sinopac-twd-history-inventory-envelope"),
        ("duplicate", "sinopac-twd-history-inventory-identity"),
        ("currency", "sinopac-twd-history-inventory-identity"),
        ("identity", "sinopac-twd-history-inventory-identity"),
        ("sequence", "sinopac-twd-history-inventory-cardinality"),
        ("frame", "sinopac-twd-history-inventory-envelope"),
        ("row", "sinopac-twd-history-inventory-row"),
    ],
)
def test_sinopac_inventory_is_exact_authoritative_set(mutation, guard) -> None:
    assert guard in SinopacCrawler.SAFE_COLLECT_GUARDS
    hit = _inventory_hit()
    if mutation == "method":
        hit.method = "GET"
    elif mutation == "body":
        hit.req_body = f"AcctValue={ACCOUNT}"
    elif mutation == "host":
        hit.url = "https://mma.sinopac.com.evil.example/ws/bank/transdetail/ws_debitacct.ashx"
    elif mutation == "path":
        hit.url = "https://mma.sinopac.com/evil/ws_debitacct.ashx"
    elif mutation == "status":
        hit.status = 500
    elif mutation == "mime":
        hit.content_type = "text/html"
    elif mutation == "redirect":
        hit.redirected = True
    elif mutation == "duplicate":
        hit.resp_json[0]["SubInfo"].append(deepcopy(hit.resp_json[0]["SubInfo"][0]))
    elif mutation == "currency":
        hit.resp_json[0]["SubInfo"][0]["DisplayText"] = "usd"
    elif mutation == "identity":
        hit.resp_json[0]["SubInfo"][0]["DataValue"] = "1234"
    elif mutation == "sequence":
        hit.request_sequence = 0
    elif mutation == "row":
        hit.resp_json[0]["SubInfo"][0] = {}
    else:
        hit.main_frame_request = False
    collector = ResponseCollector("sinopac.com")
    collector.hits = [hit]

    with pytest.raises(RuntimeError, match=f"^{guard}$"):
        SinopacCrawler._twd_inventory(collector)


def test_sinopac_account_controls_keep_all_native_currency_accounts() -> None:
    inventory = [
        {"label": "USD account", "identity": ACCOUNT, "currency": "USD"},
        {"label": LABEL, "identity": ACCOUNT, "currency": "TWD"},
    ]
    handlers = [
        f"setDebitAccount('USD account', '{ACCOUNT}', 'USD')",
        f"setDebitAccount('{LABEL}', '{ACCOUNT}', 'TWD')",
    ]

    assert SinopacCrawler._validated_twd_handlers(handlers, inventory) == [
        (0, ("USD account", ACCOUNT, "USD")),
        (1, (LABEL, ACCOUNT, "TWD")),
    ]


def test_sinopac_inventory_keeps_all_native_currency_accounts() -> None:
    hit = _inventory_hit()
    hit.resp_json[0]["SubInfo"].insert(0, {
        "DataText": "USD account",
        "DataValue": ACCOUNT,
        "DisplayText": "USD",
    })
    collector = ResponseCollector("sinopac.com")
    collector.hits = [hit]

    assert SinopacCrawler._twd_inventory(collector) == [
        {"label": "USD account", "identity": ACCOUNT, "currency": "USD"},
        {"label": LABEL, "identity": ACCOUNT, "currency": "TWD"},
    ]


def test_sinopac_inventory_accepts_native_rolling_month_form() -> None:
    hit = _inventory_hit()
    hit.req_body = (
        "Acct=&AcctValue=&CurrName=&QueryType=&AcctName=&Curr=&TextType=&"
        "BusinessDate=20260831&StartDate=20260731&EndDate=20260831"
    )
    collector = ResponseCollector("sinopac.com")
    collector.hits = [hit]

    assert SinopacCrawler._twd_inventory(collector) == [{
        "label": LABEL, "identity": ACCOUNT, "currency": "TWD",
    }]


def test_sinopac_inventory_accepts_native_initial_form() -> None:
    hit = _inventory_hit()
    hit.req_body = (
        "Acct=&AcctValue=&CurrName=&QueryType=&AcctName=&Curr=&TextType=&"
        "BusinessDate=20260831&StartDate=20260801&EndDate=20260831"
    )
    collector = ResponseCollector("sinopac.com")
    collector.hits = [hit]

    assert SinopacCrawler._twd_inventory(collector) == [{
        "label": LABEL, "identity": ACCOUNT, "currency": "TWD",
    }]


@pytest.mark.parametrize("change", [
    lambda body: body + "&Acct=",
    lambda body: body + "&extra=",
    lambda body: body.replace("Acct=&", "Acct=selected&"),
    lambda body: body.replace("QueryType=&", "QueryType=3&"),
    lambda body: body.replace("StartDate=20260801", "StartDate=20260832"),
    lambda body: body.replace("StartDate=20260801", "StartDate=20260901"),
    lambda body: body.replace("BusinessDate=20260831", "BusinessDate=20260830"),
    lambda body: body.replace("StartDate=20260801", "StartDate=20260701"),
    lambda body: body.replace("BusinessDate=20260831", "BusinessDate="),
    lambda body: body.replace("EndDate=20260831", "EndDate=2026-08-31"),
    lambda body: body.replace("CurrName=&", ""),
    lambda _body: {},
])
def test_sinopac_inventory_rejects_unbound_initial_form(change) -> None:
    hit = _inventory_hit()
    hit.req_body = change(
        "Acct=&AcctValue=&CurrName=&QueryType=&AcctName=&Curr=&TextType=&"
        "BusinessDate=20260831&StartDate=20260801&EndDate=20260831"
    )
    collector = ResponseCollector("sinopac.com")
    collector.hits = [hit]

    with pytest.raises(
        RuntimeError, match="^sinopac-twd-history-inventory-envelope$"
    ):
        SinopacCrawler._twd_inventory(collector)


def test_sinopac_inventory_returns_exact_live_contract() -> None:
    collector = ResponseCollector("sinopac.com")
    collector.hits = [_inventory_hit()]
    assert SinopacCrawler._twd_inventory(collector) == [{
        "label": LABEL, "identity": ACCOUNT, "currency": "TWD",
    }]


def test_sinopac_inventory_accepts_authoritative_empty_set() -> None:
    hit = _inventory_hit()
    hit.resp_json[0]["SubInfo"] = []
    collector = ResponseCollector("sinopac.com")
    collector.hits = [hit]
    assert SinopacCrawler._twd_inventory(collector) == []


def test_sinopac_inventory_rejects_multiple_authoritative_responses() -> None:
    first = _inventory_hit()
    second = _inventory_hit()
    second.request_sequence = 2
    collector = ResponseCollector("sinopac.com")
    collector.hits = [first, second]

    with pytest.raises(
        RuntimeError, match="^sinopac-twd-history-inventory-cardinality$"
    ):
        SinopacCrawler._twd_inventory(collector)


def test_response_collector_records_non_bearer_request_issuance_sequence() -> None:
    collector = ResponseCollector("sinopac.com")
    page = SimpleNamespace()
    frame = SimpleNamespace(page=page)
    page.main_frame = frame
    request = SimpleNamespace(
        url="https://mma.sinopac.com/ws/bank/transdetail/ws_debitacct.ashx?1788171564478",
        headers={}, method="POST", post_data="{}", redirected_from=None, frame=frame,
    )
    collector._on_request(request)
    collector._on_response(SimpleNamespace(
        url=request.url,
        request=request,
        headers={
            "content-type": "application/json",
            "content-length": "2",
            "content-encoding": "identity",
        },
        status=200,
        body=lambda: b"{}",
        json=lambda: pytest.fail("bounded response must use raw body"),
    ))

    assert collector.request_sequence == 1
    assert collector.issued_count("ws_debitacct.ashx") == 1
    assert collector.hits[0].request_sequence == 1
    assert collector.hits[0].main_frame_request is True
    assert collector.hits[0].body_size == 2
    assert collector.hits[0].resp_json == {}


def test_response_collector_clears_failed_request_state() -> None:
    collector = ResponseCollector("mma.sinopac.com")
    request = SimpleNamespace(
        url="https://mma.sinopac.com/ws/bank/transdetail/ws_debitacct.ashx",
        headers={"authorization": "Bearer opaque"},
        redirected_from=None,
    )
    collector._on_request(request)
    assert len(collector._requests) == 1
    assert len(collector._request_main_frame) == 1
    assert len(collector._auth_requests) == 1

    collector._on_request_failed(request)

    assert collector._requests == {}
    assert collector._request_main_frame == {}
    assert collector._auth_requests == {}


def test_response_collector_frame_error_leaves_no_partial_state() -> None:
    collector = ResponseCollector("mma.sinopac.com")

    class Request:
        url = "https://mma.sinopac.com/ws/bank/transdetail/ws_debitacct.ashx"

        @property
        def frame(self):
            raise RuntimeError("unavailable")

    collector._on_request(Request())

    assert collector.request_sequence == 0
    assert collector._requests == {}
    assert collector._request_main_frame == {}
    assert collector.issued_count("ws_debitacct.ashx") == 0


def test_response_collector_does_not_decode_compressed_sinopac_history() -> None:
    collector = ResponseCollector("sinopac.com")
    request = SimpleNamespace(
        url="https://mma.sinopac.com/ws/bank/transdetail/ws_transdetailMerge.ashx?1788171564478",
        headers={}, method="POST", post_data="{}", redirected_from=None,
    )
    decoded = False

    def decode():
        nonlocal decoded
        decoded = True
        return {}

    collector._on_request(request)
    collector._on_response(SimpleNamespace(
        url=request.url,
        request=request,
        headers={
            "content-type": "application/json",
            "content-length": "100",
            "content-encoding": "gzip",
        },
        status=200,
        json=decode,
    ))

    assert decoded is False
    assert collector.hits[0].resp_json is None


def test_response_collector_uses_actual_bounded_body_size() -> None:
    collector = ResponseCollector("sinopac.com")
    page = SimpleNamespace()
    frame = SimpleNamespace(page=page)
    page.main_frame = frame
    request = SimpleNamespace(
        url="https://mma.sinopac.com/ws/bank/transdetail/ws_transdetailMerge.ashx?1",
        headers={}, method="POST", post_data="", redirected_from=None, frame=frame,
    )
    collector._on_request(request)
    collector._on_response(SimpleNamespace(
        url=request.url,
        request=request,
        status=200,
        headers={
            "content-type": "application/json",
            "content-length": "1",
            "content-encoding": "identity",
        },
        body=lambda: b" " * 5_000_001,
        json=lambda: pytest.fail("bounded response must not call resp.json"),
    ))

    assert collector.hits[0].body_size == 5_000_001
    assert collector.hits[0].resp_json is None


def test_response_collector_preserves_bounded_form_body_for_exact_validation() -> None:
    collector = ResponseCollector("sinopac.com")
    page = SimpleNamespace()
    frame = SimpleNamespace(page=page)
    page.main_frame = frame
    body = "Acct=x&" + "A" * 600
    request = SimpleNamespace(
        url="https://mma.sinopac.com/ws/bank/transdetail/ws_transdetailMerge.ashx?1",
        headers={}, method="POST", post_data=body, redirected_from=None, frame=frame,
    )
    collector._on_request(request)
    collector._on_response(SimpleNamespace(
        url=request.url, request=request, status=200,
        headers={
            "content-type": "application/json",
            "content-length": "2",
            "content-encoding": "identity",
        },
        body=lambda: b"{}",
    ))

    assert collector.hits[0].req_body == body


def test_response_collector_marks_oversized_bounded_request_body() -> None:
    collector = ResponseCollector("sinopac.com")
    page = SimpleNamespace()
    frame = SimpleNamespace(page=page)
    page.main_frame = frame
    request = SimpleNamespace(
        url="https://mma.sinopac.com/ws/bank/transdetail/ws_debitacct.ashx?1",
        headers={}, method="POST", post_data="A" * 16_385,
        redirected_from=None, frame=frame,
    )
    collector._on_request(request)
    collector._on_response(SimpleNamespace(
        url=request.url, request=request, status=200,
        headers={
            "content-type": "application/json",
            "content-length": "2",
            "content-encoding": "identity",
        },
        body=lambda: b"{}",
    ))

    assert collector.hits[0].req_body == {"__oversize__": True}


def test_response_collector_marks_explicit_json_null_request_body() -> None:
    collector = ResponseCollector("sinopac.com")
    page = SimpleNamespace()
    frame = SimpleNamespace(page=page)
    page.main_frame = frame
    request = SimpleNamespace(
        url="https://mma.sinopac.com/ws/bank/transdetail/ws_debitacct.ashx?1",
        headers={}, method="POST", post_data="null",
        redirected_from=None, frame=frame,
    )
    collector._on_request(request)
    collector._on_response(SimpleNamespace(
        url=request.url, request=request, status=200,
        headers={
            "content-type": "application/json",
            "content-length": "2",
            "content-encoding": "identity",
        },
        body=lambda: b"{}",
    ))

    assert collector.hits[0].req_body == {"__json_null__": True}


def test_sinopac_history_rejects_twd_decimal_amounts() -> None:
    row = _row()
    row["DataText4"] = "-12.34"
    hit = _history_hit(rows=[row])

    with pytest.raises(RuntimeError, match="sinopac-twd-history-row"):
        SinopacCrawler._validate_history_hit(
            hit,
            label=LABEL,
            identity=ACCOUNT,
            currency="TWD",
            start=date(2026, 8, 1),
            end=date(2026, 8, 31),
            business_date="20260831",
            as_of=date(2026, 8, 31),
        )


def test_sinopac_history_accepts_foreign_decimal_amounts() -> None:
    row = _row()
    row["DataText4"] = "-12.34"
    row["DataText5"] = "1,234.56"
    hit = _history_hit(rows=[row])
    hit.req_body = hit.req_body.replace("Curr=TWD", "Curr=USD")

    result = SinopacCrawler._validate_history_hit(
        hit, label=LABEL, identity=ACCOUNT, currency="USD",
        start=date(2026, 8, 1), end=date(2026, 8, 31),
        business_date="20260831", as_of=date(2026, 8, 31),
    )

    assert result["rows"] == 1


def test_sinopac_history_identity_scopes_foreign_currency_cursor() -> None:
    assert SinopacCrawler._history_identity(ACCOUNT, "TWD") == ACCOUNT
    assert SinopacCrawler._history_identity(ACCOUNT, "USD") == f"{ACCOUNT}:USD"
    source = inspect.getsource(SinopacCrawler._collect_transactions)
    assert "history_identity = self._history_identity(item[\"identity\"], item[\"currency\"])" in source
    assert "self._history_range(history_identity" in source


def test_sinopac_history_accepts_native_current_defaults_for_historical_window() -> None:
    hit = _history_hit(start="20250101", end="20250131", rows=[])
    hit.resp_json[0]["BeginDate"] = "20250101"
    hit.resp_json[0]["EndDate"] = "20250131"

    result = SinopacCrawler._validate_history_hit(
        hit,
        label=LABEL,
        identity=ACCOUNT,
        currency="TWD",
        start=date(2025, 1, 1),
        end=date(2025, 1, 31),
        business_date="20260831",
        as_of=date(2026, 8, 31),
    )

    assert result["status"] == "explicit_empty"


def test_sinopac_history_accepts_native_fail_no_data_envelope() -> None:
    hit = _history_hit(rows=[])
    hit.resp_json[0]["Header"] = "FAIL"
    hit.resp_json[0]["HeadInfo"] = None

    result = SinopacCrawler._validate_history_hit(
        hit,
        label=LABEL,
        identity=ACCOUNT,
        currency="TWD",
        start=date(2026, 8, 1),
        end=date(2026, 8, 31),
        business_date="20260831",
        as_of=date(2026, 8, 31),
    )

    assert result == {
        "records": [],
        "status": "explicit_empty",
        "rows": 0,
        "display_fields": [],
    }


@pytest.mark.parametrize("mutation", ["message", "record-count", "rows", "head"])
def test_sinopac_history_rejects_nonempty_fail_envelope(mutation: str) -> None:
    hit = _history_hit(rows=[])
    body = hit.resp_json[0]
    body["Header"] = "FAIL"
    body["HeadInfo"] = None
    if mutation == "message":
        body["Message"] = ""
    elif mutation == "record-count":
        body["RecordCount"] = "0"
    elif mutation == "rows":
        body["SubInfo"] = [_row()]
    else:
        body["HeadInfo"] = []

    with pytest.raises(RuntimeError, match="sinopac-twd-history-response"):
        SinopacCrawler._validate_history_hit(
            hit,
            label=LABEL,
            identity=ACCOUNT,
            currency="TWD",
            start=date(2026, 8, 1),
            end=date(2026, 8, 31),
            business_date="20260831",
            as_of=date(2026, 8, 31),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "method", "host", "path", "params", "fragment", "query", "status", "mime", "redirect", "keys",
        "form_keys", "account", "range", "query_type", "header", "max_month",
        "coverage", "head_info", "head_order", "row_keys", "row_date", "row_money", "row_overflow",
        "row_description_html", "row_noncanonical_datetime", "row_noncanonical_date",
        "empty_marker", "record_count", "body_size", "business_date", "is_obu",
        "head_keys", "sequence", "duplicate_row", "frame",
    ],
)
def test_sinopac_history_response_fails_closed(mutation) -> None:
    hit = _history_hit()
    if mutation == "method":
        hit.method = "GET"
    elif mutation == "host":
        hit.url = "https://evil.example/ws/bank/transdetail/ws_transdetailMerge.ashx"
    elif mutation == "path":
        hit.url = "https://mma.sinopac.com/evil/ws_transdetailMerge.ashx"
    elif mutation == "params":
        hit.raw_url = hit.raw_url.replace(".ashx?", ".ashx;jsessionid=opaque?")
    elif mutation == "fragment":
        hit.raw_url += "#opaque"
    elif mutation == "query":
        hit.raw_url += "&evil=1"
    elif mutation == "status":
        hit.status = 500
    elif mutation == "mime":
        hit.content_type = "text/html"
    elif mutation == "redirect":
        hit.redirected = True
    elif mutation == "body_size":
        hit.body_size = 5_000_001
    elif mutation == "keys":
        hit.resp_json[0]["extra"] = 1
    elif mutation == "form_keys":
        hit.req_body += "&extra=1"
    elif mutation == "account":
        hit.req_body = hit.req_body.replace(ACCOUNT, "99999999999999")
    elif mutation == "range":
        hit.req_body = hit.req_body.replace("StartDate=20260801", "StartDate=20260701")
    elif mutation == "query_type":
        hit.req_body = hit.req_body.replace("QueryType=3", "QueryType=2")
    elif mutation == "business_date":
        hit.req_body = hit.req_body.replace("BusinessDate=20260831", "BusinessDate=29990101")
    elif mutation == "header":
        hit.resp_json[0]["Header"] = "FAILED"
    elif mutation == "max_month":
        hit.resp_json[0]["MaxMonth"] = "12"
    elif mutation == "coverage":
        hit.resp_json[0]["BeginDate"] = "20260802"
    elif mutation == "head_info":
        hit.resp_json[0]["HeadInfo"].pop()
    elif mutation == "head_order":
        hit.resp_json[0]["HeadInfo"].reverse()
    elif mutation == "head_keys":
        hit.resp_json[0]["HeadInfo"][0]["extra"] = "bad"
    elif mutation == "row_keys":
        hit.resp_json[0]["SubInfo"][0].pop("DataText11")
    elif mutation == "row_date":
        hit.resp_json[0]["SubInfo"][0]["DataText1"] = "2026/09/01<br />12:34"
    elif mutation == "row_money":
        hit.resp_json[0]["SubInfo"][0]["DataText4"] = "NaN"
    elif mutation == "row_overflow":
        hit.resp_json[0]["SubInfo"][0]["DataText4"] = "+2147483648"
    elif mutation == "row_description_html":
        hit.resp_json[0]["SubInfo"][0]["DataText3"] = "<b></b>"
    elif mutation == "row_noncanonical_datetime":
        hit.resp_json[0]["SubInfo"][0]["DataText1"] = "2026/8/1<br />1:02"
    elif mutation == "row_noncanonical_date":
        hit.resp_json[0]["SubInfo"][0]["DataText2"] = "2026/8/1"
    elif mutation == "empty_marker":
        hit = _history_hit(rows=[], message="系統忙碌")
    elif mutation == "is_obu":
        hit.resp_json[0]["isOBU"] = "maybe"
    elif mutation == "sequence":
        hit.request_sequence = 0
    elif mutation == "duplicate_row":
        hit.resp_json[0]["SubInfo"].append(deepcopy(hit.resp_json[0]["SubInfo"][0]))
    elif mutation == "frame":
        hit.main_frame_request = False
    else:
        hit.resp_json[0]["RecordCount"] = "2"

    with pytest.raises(RuntimeError, match="sinopac-twd-history"):
        SinopacCrawler._validate_history_hit(
            hit,
            label=LABEL,
            identity=ACCOUNT,
            currency="TWD",
            start=date(2026, 8, 1),
            end=date(2026, 8, 31),
            business_date="20260831",
            as_of=date(2026, 8, 31),
        )


def test_sinopac_history_accepts_native_head_ordering() -> None:
    hit = _history_hit()
    head_info = hit.resp_json[0]["HeadInfo"]
    head_info[:] = [head_info[-1], *head_info[:-1]]
    for index, (item, order) in enumerate(zip(
        head_info, ("01", "01", "02", "03", "04", "05", "06", "08", "08"),
    )):
        item["OrderIndex"] = order
        item["DataAlign"] = "2" if index < 4 else "3"
        item["HeadAlign"] = "2" if index < 4 else "3"
        item["MainShow"] = "Y" if index in {1, 2} else "N"
        item["DetailShow"] = "N" if index < 3 else "Y"
    head_info[0]["HeadText"] = "欄" * 67
    second_row = deepcopy(hit.resp_json[0]["SubInfo"][0])
    second_row["DataText3"] = "第二筆"
    hit.resp_json[0]["SubInfo"].append(second_row)
    hit.resp_json[0]["RecordCount"] = "1"

    result = SinopacCrawler._validate_history_hit(
        hit, label=LABEL, identity=ACCOUNT, currency="TWD",
        start=date(2026, 8, 1), end=date(2026, 8, 31),
        business_date="20260831", as_of=date(2026, 8, 31),
    )

    assert result["rows"] == 2
    assert result["display_fields"] == [f"DataText{i}" for i in range(1, 9)]


def test_sinopac_history_accepts_nonempty_and_exact_empty() -> None:
    complete = SinopacCrawler._validate_history_hit(
        _history_hit(), label=LABEL, identity=ACCOUNT, currency="TWD",
        start=date(2026, 8, 1), end=date(2026, 8, 31),
        business_date="20260831", as_of=date(2026, 8, 31),
    )
    empty = SinopacCrawler._validate_history_hit(
        _history_hit(rows=[]), label=LABEL, identity=ACCOUNT, currency="TWD",
        start=date(2026, 8, 1), end=date(2026, 8, 31),
        business_date="20260831", as_of=date(2026, 8, 31),
    )
    assert complete["status"] == "complete"
    assert complete["rows"] == 1
    assert empty == {
        "records": [], "status": "explicit_empty", "rows": 0,
        "display_fields": [f"DataText{i}" for i in range(1, 10)],
    }
    null_head = _history_hit(rows=[])
    null_head.resp_json[0]["HeadInfo"] = None
    assert SinopacCrawler._validate_history_hit(
        null_head, label=LABEL, identity=ACCOUNT, currency="TWD",
        start=date(2026, 8, 1), end=date(2026, 8, 31),
        business_date="20260831", as_of=date(2026, 8, 31),
    ) == {
        "records": [], "status": "explicit_empty", "rows": 0,
        "display_fields": [],
    }


def test_sinopac_pager_detection_accepts_only_semantic_visible_controls() -> None:
    source = inspect.getsource(SinopacCrawler._collect_transactions)

    assert "e.closest('.pagination,.pager,[class*=pagination i],[class*=pager i]')" in source
    assert "e.matches('input[type=button],input[type=submit],select,option') && /^\\d{1,3}$/.test" not in source
    assert "button[onclick],a[onclick]" in source
    assert "(?:go|set|change|select)?page\\s*\\(" in source
    assert "[onclick],[name*=page i]" not in source
    assert ".filter(visible).filter(e =>" in source


def test_sinopac_dom_binding_uses_native_stable_columns() -> None:
    source = inspect.getsource(SinopacCrawler._collect_transactions)

    assert "const signedAmount=normalize(expectedRow[3])" in source
    assert "const amountCell=signedAmount.startsWith('-')?3:4" in source
    assert "const otherAmountCell=amountCell===3?4:3" in source
    assert "responseCells[otherAmountCell]=''" in source
    assert 'row[f"DataText{i}"] for i in range(1, 12)' in source


def test_sinopac_dom_diagnostic_guards_are_transport_safe() -> None:
    reasons = {
        "shape", "pager", "error", "binding", "mutation-shape", "mutation-range",
        "table-count", "table-visibility", "nonempty-attestation", "empty-row-count",
        "empty-row-visibility", "empty-marker", "empty-binding", "empty-signature",
        "empty-no-row", "empty-extra-hidden-rows", "empty-hidden-data",
        "empty-hidden-header", "empty-hidden-template-text", "empty-hidden-transaction",
        "empty-hidden-full-transaction", "empty-hidden-date-cell",
        "empty-hidden-numeric-cell", "empty-hidden-control",
        "empty-multiple-visible-rows",
        "nonempty-binding", "unknown",
    }
    assert {
        f"sinopac-twd-history-result-table-{reason}" for reason in reasons
    } <= SinopacCrawler.SAFE_COLLECT_GUARDS


@pytest.mark.parametrize(
    (
        "empty", "dom_bound", "mutations", "fresh_empty", "hidden_rows",
        "hidden_nonempty_rows", "hidden_header_rows", "hidden_transaction_rows",
        "hidden_changed_rows", "hidden_changed_header_rows", "hidden_changed_transaction_rows",
        "hidden_changed_prior_rows",
        "expected_guard",
    ),
    [
        (False, True, 1, False, 0, 0, 0, 0, 0, 0, 0, 0, None),
        (True, True, 0, False, 0, 0, 0, 0, 0, 0, 0, 0, None),
        (True, True, 3, False, 1, 0, 0, 0, 0, 0, 0, 0, None),
        # Unchanged pre-query hidden rows are stale scaffolding, not response evidence.
        (True, True, 3, False, 1, 1, 0, 1, 0, 0, 0, 0, None),
        # Re-rendered rows are accepted only when bound to an already accepted prior window.
        (True, True, 3, False, 1, 1, 0, 1, 1, 0, 1, 1, None),
        (False, False, 1, False, 0, 0, 0, 0, 0, 0, 0, 0, "nonempty-binding"),
        (True, False, 3, False, 2, 1, 0, 0, 1, 0, 0, 0, "empty-hidden-template-text"),
        (True, False, 3, False, 1, 1, 1, 0, 1, 1, 0, 0, "empty-hidden-header"),
        (True, False, 3, False, 1, 1, 0, 1, 1, 0, 1, 0, "empty-hidden-numeric-cell"),
    ],
)
def test_sinopac_collect_uses_native_account_date_and_query_controls(
    monkeypatch, empty, dom_bound, mutations, fresh_empty, hidden_rows,
    hidden_nonempty_rows, hidden_header_rows, hidden_transaction_rows,
    hidden_changed_rows, hidden_changed_header_rows, hidden_changed_transaction_rows,
    hidden_changed_prior_rows,
    expected_guard,
) -> None:
    collect_source = inspect.getsource(SinopacCrawler._collect_transactions)
    assert "freshEmptyNodes:new WeakSet" in collect_source
    assert "freshEmptyNodes.has(emptyCell)" in collect_source
    assert "staleHiddenRows=new WeakMap" in collect_source
    assert "const visible=e => !!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);" in collect_source
    assert "hiddenChangedRows" in collect_source
    assert "normalize(row.innerHTML)!==''&&row.querySelectorAll('th').length>0" in collect_source
    assert "freshEmpty:true" not in collect_source
    assert "beforeMutations=window.__hermesSinopacState?.mutations" in collect_source
    assert "await new Promise(resolve=>setTimeout(resolve,0))" in collect_source
    assert "afterMutations===beforeMutations" in collect_source
    assert "__hermesSinopacObserver?.takeRecords()" in collect_source
    collector = ResponseCollector("sinopac.com")
    crawler = object.__new__(SinopacCrawler)
    crawler.transaction_cursors = {}
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    monkeypatch.setattr(crawler, "_twd_inventory", lambda _collector, **_kwargs: [{
        "label": LABEL, "identity": ACCOUNT, "currency": "TWD",
    }])
    monkeypatch.setattr(
        crawler, "_history_range",
        lambda _identity, *, end, mode: (date(2026, 8, 1), date(2026, 8, 31)),
    )

    values = {"start": "", "end": ""}
    clicks = []

    class Locator:
        def __init__(self, kind):
            self.kind = kind

        def count(self):
            return 1

        def nth(self, _index):
            return self

        def is_visible(self):
            return True

        def is_enabled(self):
            return True

        def click(self, **_kwargs):
            clicks.append(self.kind)
            if self.kind == "query":
                collector._issued_endpoint_counts["ws_transdetailMerge.ashx"] = 1
                collector.hits.append(_history_hit(
                    start=values["start"], end=values["end"],
                    rows=[] if empty else None,
                ))

        def fill(self, value):
            values[self.kind] = value

        def input_value(self):
            return values[self.kind]

    class Page:
        def goto(self, *_args, **_kwargs):
            collector._issued_endpoint_counts["ws_debitacct.ashx"] = 1
            collector.hits.append(_inventory_hit())
            return None

        def wait_for_timeout(self, _milliseconds):
            return None

        def locator(self, selector):
            return Locator({
                "#spanDebitAccount": "toggle",
                "#divDebitAccount [onclick]": "option",
                "#StartDate": "start",
                "#EndDate": "end",
                "#btnQuery": "query",
            }[selector])

        def evaluate(self, script, _arg=None):
            if "__hermesSinopacExpectedRows = rows" in script:
                return None
            if "map(e => e.getAttribute('onclick')" in script:
                return [f"setDebitAccount('{LABEL}', '{ACCOUNT}', 'TWD')"]
            if "Object.fromEntries" in script:
                return {
                    "Acct": LABEL,
                    "AcctValue": ACCOUNT,
                    "Curr": "TWD",
                    "BusinessDate": "20260831",
                }
            if "if(!table)return null" in script:
                return [1, 1]
            if "ListingTable" in script:
                if empty:
                    return {
                        "tables": 1,
                        "rows": 1 + hidden_rows,
                        "visibleRows": 1,
                        "hiddenRows": hidden_rows,
                        "hiddenNonemptyRows": hidden_nonempty_rows,
                        "hiddenHeaderRows": hidden_header_rows,
                        "hiddenTransactionRows": hidden_transaction_rows,
                        "hiddenChangedRows": hidden_changed_rows,
                        "hiddenChangedPriorRows": hidden_changed_prior_rows,
                        "hiddenChangedHeaderRows": hidden_changed_header_rows,
                        "hiddenChangedTransactionRows": hidden_changed_transaction_rows,
                        "hiddenChangedFullTransactionRows": 0,
                        "hiddenChangedDateRows": 0,
                        "hiddenChangedNumericRows": hidden_changed_transaction_rows,
                        "hiddenChangedControlRows": 0,
                        "pagers": 0,
                        "errors": 0,
                        "visible": True,
                        "emptyMarker": True,
                        "freshEmpty": fresh_empty,
                        "bound": dom_bound,
                        "signature": [2, 2] if mutations == 3 else [1, 1],
                        "mutations": mutations,
                    }
                return {
                    "tables": 1,
                    "rows": 1,
                    "visibleRows": 1,
                    "hiddenRows": 0,
                    "hiddenNonemptyRows": 0,
                    "hiddenHeaderRows": 0,
                    "hiddenTransactionRows": 0,
                    "hiddenChangedRows": 0,
                    "hiddenChangedPriorRows": 0,
                    "hiddenChangedHeaderRows": 0,
                    "hiddenChangedTransactionRows": 0,
                    "hiddenChangedFullTransactionRows": 0,
                    "hiddenChangedDateRows": 0,
                    "hiddenChangedNumericRows": 0,
                    "hiddenChangedControlRows": 0,
                    "pagers": 0,
                    "errors": 0,
                    "visible": True,
                    "emptyMarker": False,
                    "freshEmpty": False,
                    "bound": dom_bound,
                    "signature": [2, 2],
                    "mutations": 1,
                }
            raise AssertionError("unexpected evaluate")

    if expected_guard:
        with pytest.raises(
            RuntimeError,
            match=f"sinopac-twd-history-result-table-{expected_guard}",
        ):
            crawler._collect_transactions(Page(), collector)
        return

    result = crawler._collect_transactions(Page(), collector)

    assert values == {"start": "20260801", "end": "20260831"}
    assert clicks == ["toggle", "option", "query"]
    assert result["results"][0]["receipt"]["status"] == (
        "explicit_empty" if empty else "complete"
    )
    assert result["coverage"]["domains"][0]["expected"][0]["identity"] == ACCOUNT


def test_sinopac_collect_publishes_explicit_empty_inventory_coverage(monkeypatch) -> None:
    collector = ResponseCollector("sinopac.com")
    hit = _inventory_hit()
    hit.resp_json[0]["SubInfo"] = []
    crawler = object.__new__(SinopacCrawler)
    crawler.transaction_cursors = {}
    monkeypatch.delenv("BANK_CRAWLER_HISTORY_MODE", raising=False)

    class Page:
        def goto(self, *_args, **_kwargs):
            collector._issued_endpoint_counts["ws_debitacct.ashx"] = 1
            collector.hits.append(hit)
            return None

        def wait_for_timeout(self, _milliseconds):
            return None

        def evaluate(self, script):
            return 0 if "const tables=document.querySelectorAll('table')" in script else []

    result = crawler._collect_transactions(Page(), collector)
    domain = result["coverage"]["domains"][0]

    assert result["results"] == []
    assert result["inventory"] == []
    assert result["coverage"]["mode"] == "full"
    assert domain["expected"] == []
    assert domain["empty_window"]["status"] == "explicit_empty"


@pytest.mark.parametrize(
    "blocker_token",
    ["document.querySelectorAll('table')", ".modal.in", "[aria-modal=true]", "[class*=loading-overlay]"],
)
def test_sinopac_empty_inventory_rejects_result_or_overlay_blocker(
    monkeypatch, blocker_token,
) -> None:
    collector = ResponseCollector("sinopac.com")
    hit = _inventory_hit()
    hit.resp_json[0]["SubInfo"] = []
    crawler = object.__new__(SinopacCrawler)
    crawler.transaction_cursors = {}

    class Page:
        def goto(self, *_args, **_kwargs):
            collector._issued_endpoint_counts["ws_debitacct.ashx"] = 1
            collector.hits.append(hit)

        def wait_for_timeout(self, _milliseconds):
            return None

        def evaluate(self, script):
            if "const tables=document.querySelectorAll('table')" in script:
                return 1 if blocker_token in script else 0
            return []

    with pytest.raises(RuntimeError, match="sinopac-twd-history-empty-inventory-blocked"):
        crawler._collect_transactions(Page(), collector)


def test_sinopac_empty_inventory_rechecks_dialog_after_dom_probe(monkeypatch) -> None:
    collector = ResponseCollector("sinopac.com")
    hit = _inventory_hit()
    hit.resp_json[0]["SubInfo"] = []
    crawler = object.__new__(SinopacCrawler)
    crawler.transaction_cursors = {}
    crawler._shared_dialog_blocked = False

    class Page:
        def goto(self, *_args, **_kwargs):
            collector._issued_endpoint_counts["ws_debitacct.ashx"] = 1
            collector.hits.append(hit)

        def wait_for_timeout(self, _milliseconds):
            return None

        def evaluate(self, script):
            if "const tables=document.querySelectorAll('table')" in script:
                crawler._shared_dialog_blocked = True
                return 0
            return []

    with pytest.raises(RuntimeError, match="sinopac-twd-history-dialog"):
        crawler._collect_transactions(Page(), collector)


def test_sinopac_coverage_fixture_is_valid() -> None:
    assert validate_history_coverage(
        _coverage(), expected_mode="full", expected_domains=frozenset({"account_transactions"}),
    )["identities"] == 1


def test_sinopac_persistence_accepts_authoritative_empty_inventory(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    payload = BankCollectResult(
        debit_accounts=[],
        account_transactions=[],
        history_coverage={
            "mode": "full",
            "as_of": "2026-08-31",
            "domains": [{
                "domain": "account_transactions",
                "expected": [],
                "windows": [],
                "empty_window": {
                    "start": "2025-09-01",
                    "end": "2026-08-31",
                    "status": "explicit_empty",
                    "pages": 1,
                },
            }],
        },
        card_bill_facts_ok=False,
    ).to_dict()
    assert "debit_accounts" not in payload
    assert "account_transactions" not in payload
    try:
        delta = persist_collected("sinopac", payload, store)
        assert delta["twd_txn_new"] == 0
    finally:
        store.close()


def test_sinopac_persistence_preserves_foreign_currency_and_decimals(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    payload = _persist_payload()
    history_identity = f"{ACCOUNT}:USD"
    payload["debit_accounts"][0]["currency"] = "USD"
    payload["bank_balance"][0]["SubInfo"][0]["Curr"] = "USD"
    domain = payload["history_coverage"]["domains"][0]
    domain["expected"][0]["identity"] = history_identity
    for window in domain["windows"]:
        window["identity"] = history_identity
    for result in payload["account_transactions"]:
        result["currency"] = "USD"
        result["receipt"]["identity"] = history_identity
    row = payload["account_transactions"][-1]["records"][0]
    row["DataText4"] = "-12.34"
    row["DataText5"] = "1,234.56"

    try:
        delta = persist_sinopac(payload, store)
        saved = store.conn.execute(
            "SELECT currency, expend, income, balance FROM twd_transactions"
        ).fetchone()
        cursor = store.conn.execute(
            "SELECT identity FROM history_transaction_cursors"
        ).fetchone()
        assert delta["twd_txn_new"] == 1
        assert saved is not None and tuple(saved) == ("USD", 12.34, None, 1234.56)
        assert cursor is not None and cursor["identity"] == history_identity
    finally:
        store.close()


def test_sinopac_persistence_keeps_same_account_identity_in_each_currency(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    payload = _persist_payload()
    foreign = deepcopy(payload["account_transactions"])
    history_identity = f"{ACCOUNT}:USD"
    for item in foreign:
        item["currency"] = "USD"
        item["account_name"] = "USD account"
        item["receipt"]["identity"] = history_identity
    foreign[-1]["records"][0]["DataText4"] = "-12.34"
    foreign[-1]["records"][0]["DataText5"] = "1,234.56"
    payload["account_transactions"].extend(foreign)
    payload["debit_accounts"].append({
        "label": "USD account", "identity": ACCOUNT, "currency": "USD",
    })
    payload["bank_balance"][0]["SubInfo"].append({
        "AcctValue": ACCOUNT,
        "Curr": "USD",
        "AvailBalance": "12.34",
        "AcctText": "USD account",
    })
    domain = payload["history_coverage"]["domains"][0]
    domain["expected"].append({
        "identity": history_identity,
        "start": domain["expected"][0]["start"],
        "end": domain["expected"][0]["end"],
    })
    domain["windows"].extend(
        {key: item["receipt"][key] for key in ("identity", "start", "end", "status", "pages")}
        for item in foreign
    )

    try:
        assert persist_collected("sinopac", payload, store)["twd_txn_new"] == 2
        saved = store.conn.execute(
            "SELECT currency, expend FROM twd_transactions ORDER BY currency"
        ).fetchall()
        assert [tuple(row) for row in saved] == [("TWD", None), ("USD", 12.34)]
        accounts = store.conn.execute(
            "SELECT account_no, currency, nickname FROM accounts ORDER BY currency"
        ).fetchall()
        assert [tuple(row) for row in accounts] == [
            (ACCOUNT, "TWD", "測試帳戶"),
            (ACCOUNT, "USD", "USD account"),
        ]
    finally:
        store.close()


def test_sinopac_balances_do_not_aggregate_unrelated_foreign_currencies(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    payload = _persist_payload()
    payload["bank_balance"][0]["SubInfo"] = [
        {"AcctValue": ACCOUNT, "Curr": "USD", "AvailBalance": "10.99", "AcctText": "USD"},
        {"AcctValue": "11234567890123", "Curr": "JPY", "AvailBalance": "1000.000001", "AcctText": "JPY"},
    ]
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    try:
        persist_collected("sinopac", payload, store)
        balance = store.conn.execute(
            "SELECT twd_balance, fx_balance FROM balance_history"
        ).fetchone()
        assert balance is not None
        assert tuple(balance) == (None, None)
        metric = store.conn.execute(
            "SELECT payload_json FROM daily_metrics WHERE category='balance_latest'"
        ).fetchone()
        assert metric is not None
        value = json.loads(metric[0])
        assert value["fx_raw"] is None
        assert value["fx_by_currency"] == {"JPY": 1000.000001, "USD": 10.99}
    finally:
        store.close()


def test_sinopac_single_foreign_balance_preserves_decimal_snapshot_schema(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    payload = _persist_payload()
    payload["bank_balance"][0]["SubInfo"] = [{
        "AcctValue": ACCOUNT, "Curr": "USD", "AvailBalance": "10.99", "AcctText": "USD",
    }]
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    try:
        persist_collected("sinopac", payload, store)
        row = store.conn.execute("SELECT fx_balance FROM balance_history").fetchone()
        column = next(
            item for item in store.conn.execute("PRAGMA table_info(balance_history)").fetchall()
            if item["name"] == "fx_balance"
        )
        assert row is not None
        assert row["fx_balance"] == 10.99
        assert column["type"] == "REAL"
    finally:
        store.close()


def test_sinopac_foreign_balance_sum_is_rounded_to_native_precision(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    payload = _persist_payload()
    payload["bank_balance"][0]["SubInfo"] = [
        {"AcctValue": "USD-A", "Curr": "USD", "AvailBalance": "0.1", "AcctText": "USD"},
        {"AcctValue": "USD-B", "Curr": "USD", "AvailBalance": "0.2", "AcctText": "USD"},
    ]
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    try:
        persist_collected("sinopac", payload, store)
        balance = store.conn.execute("SELECT fx_balance FROM balance_history").fetchone()
        metric = store.conn.execute(
            "SELECT payload_json FROM daily_metrics WHERE category='balance_latest'"
        ).fetchone()
        assert balance is not None and balance["fx_balance"] == 0.3
        assert metric is not None
        assert json.loads(metric["payload_json"])["fx_by_currency"] == {"USD": 0.3}
    finally:
        store.close()


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_sinopac_nonfinite_foreign_balance_never_reaches_metrics(
    tmp_path, monkeypatch, value,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    payload = _persist_payload()
    payload["bank_balance"][0]["SubInfo"] = [{
        "AcctValue": ACCOUNT, "Curr": "USD", "AvailBalance": value, "AcctText": "USD",
    }]
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    try:
        with pytest.raises(ValueError, match="invalid (?:SinoPac )?native amount|invalid native monetary value"):
            persist_collected("sinopac", payload, store)
        for table in ("accounts", "balance_history", "daily_metrics"):
            count = store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            assert count is not None and count[0] == 0
    finally:
        store.close()


def test_store_money_barriers_reject_fractional_twd_before_sql(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1)
    try:
        with pytest.raises(ValueError, match="invalid native monetary value"):
            store.upsert_twd_txns([{
                "account_no": "A", "currency": "TWD", "datetime": "2026-10-01",
                "income": 1.9, "expend": None, "balance": 1.9,
            }])
        with pytest.raises(ValueError, match="invalid native monetary value"):
            store.upsert_balance_history([{
                "snapshotDate": "2026-10-01", "twdBalance": 1.9,
                "loanBalance": None,
            }])
        with pytest.raises(ValueError, match="invalid native monetary value"):
            store.upsert_accounts([{
                "account_no": "A", "currency": "TWD", "raw_balance": 1.9,
            }])
        invalid_card_txn = {
            "card_no": "C", "currency": "TWD", "date": "2026-10-01",
            "amount": 1.9, "desc": "bad",
        }
        with pytest.raises(ValueError, match="invalid native monetary value"):
            store.upsert_card_billed([invalid_card_txn])
        with pytest.raises(ValueError, match="invalid native monetary value"):
            store.refresh_card_pending("C", [invalid_card_txn], fetch_ok=True)
        with pytest.raises(ValueError, match="invalid native monetary value"):
            store.upsert_cards([{"number": "C", "used_credit": 1.9}])
        with pytest.raises(ValueError, match="invalid native monetary value"):
            store.update_card_bill_facts([{"number": "C", "bill_due_amount": 1.9}])
        for table in (
            "twd_transactions", "balance_history", "accounts",
            "card_billed_txns", "card_pending_txns", "cards",
        ):
            count = store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            assert count is not None and count[0] == 0
    finally:
        store.close()


@pytest.mark.parametrize(
    ("currency", "value"),
    [("TWD", "1000.5"), ("USD", "0.1234567")],
)
def test_sinopac_rejects_invalid_current_balance_precision_atomically(
    tmp_path, monkeypatch, currency, value,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    payload = _persist_payload()
    payload["bank_balance"][0]["SubInfo"] = [{
        "AcctValue": "BAL-BAD",
        "Curr": currency,
        "AvailBalance": value,
        "AcctText": "活存",
    }]
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    try:
        with pytest.raises(ValueError, match="invalid (?:SinoPac )?native amount|invalid native monetary value"):
            persist_collected("sinopac", payload, store)
        for table in ("accounts", "balance_history", "daily_metrics"):
            count = store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            assert count is not None and count[0] == 0
    finally:
        store.close()


@pytest.mark.parametrize(
    ("currency", "value"),
    [("TWD", "1000.5"), ("USD", "0.1234567")],
)
def test_sinopac_rejects_invalid_native_loan_precision_atomically(
    tmp_path, monkeypatch, currency, value,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    payload = _persist_payload()
    payload["loan"] = {
        "fetch_ok": True,
        "details": [{
            "account": "LOAN-BAD",
            "records": [{
                "Currency": currency,
                "LoanBalance": value,
                "LoanKind": "信貸",
            }],
        }],
    }
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    try:
        with pytest.raises(ValueError, match="invalid (?:SinoPac )?native amount|invalid native monetary value"):
            persist_collected("sinopac", payload, store)
        for table in ("accounts", "balance_history", "daily_metrics"):
            count = store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            assert count is not None and count[0] == 0
    finally:
        store.close()


def test_sinopac_loan_totals_are_currency_scoped(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    payload = _persist_payload()
    payload["loan"] = {
        "fetch_ok": True,
        "details": [{
            "account": "LOAN-SAME",
            "records": [
                {"Currency": "TWD", "LoanBalance": "1000", "LoanKind": "信貸"},
                {"Currency": "USD", "LoanBalance": "12.34", "LoanKind": "信貸"},
            ],
        }],
    }
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    try:
        persist_collected("sinopac", payload, store)
        accounts = store.conn.execute(
            "SELECT currency, raw_balance FROM accounts WHERE account_no='LOAN-SAME' "
            "ORDER BY currency"
        ).fetchall()
        balance = store.conn.execute("SELECT loan_balance FROM balance_history").fetchone()
        metric = store.conn.execute(
            "SELECT payload_json FROM daily_metrics WHERE category='balance_latest'"
        ).fetchone()
        assert balance is not None
        assert metric is not None
        assert [tuple(row) for row in accounts] == [("TWD", -1000.0), ("USD", -12.34)]
        assert balance["loan_balance"] == 1000
        assert json.loads(metric["payload_json"])["loan_by_currency"] == {
            "TWD": 1000.0, "USD": 12.34,
        }
    finally:
        store.close()


def test_sinopac_store_migrates_account_identity_to_include_currency(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    db_path = tmp_path / "sinopac.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE accounts (
            user_id INTEGER NOT NULL DEFAULT 1,
            account_no TEXT NOT NULL,
            currency TEXT NOT NULL DEFAULT 'TWD',
            branch TEXT, nickname TEXT, type TEXT, product_type TEXT,
            raw_balance REAL, raw_balance_date TEXT,
            excluded INTEGER NOT NULL DEFAULT 0,
            nickname_overwrite TEXT, updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, account_no)
        )
    """)
    conn.execute(
        "INSERT INTO accounts (account_no, currency, nickname, updated_at) "
        "VALUES (?, 'TWD', 'legacy', '2026-09-30T00:00:00')",
        (ACCOUNT,),
    )
    conn.commit()
    conn.close()

    store = BankStore("sinopac", user_id=1)
    try:
        primary_key = [
            row["name"]
            for row in store.conn.execute("PRAGMA table_info(accounts)").fetchall()
            if row["pk"]
        ]
        assert primary_key == ["user_id", "account_no", "currency"]
        store.upsert_accounts([{
            "account_no": ACCOUNT,
            "currency": "USD",
            "nickname": "USD account",
        }])
        rows = store.conn.execute(
            "SELECT currency, nickname FROM accounts ORDER BY currency"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("TWD", "legacy"), ("USD", "USD account"),
        ]
    finally:
        store.close()


def test_store_normalizes_missing_and_legacy_null_account_currency(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    db_path = tmp_path / "sinopac.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE accounts (
            user_id INTEGER NOT NULL DEFAULT 1,
            account_no TEXT NOT NULL,
            currency TEXT,
            branch TEXT, nickname TEXT, type TEXT, product_type TEXT,
            raw_balance REAL, raw_balance_date TEXT,
            excluded INTEGER NOT NULL DEFAULT 0,
            nickname_overwrite TEXT, updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, account_no, currency)
        )
    """)
    conn.execute(
        "INSERT INTO accounts (account_no, currency, raw_balance, excluded, "
        "nickname_overwrite, updated_at) "
        "VALUES ('A', NULL, 1, 1, 'Private', '2026-09-29T00:00:00')"
    )
    conn.execute(
        "INSERT INTO accounts (account_no, currency, raw_balance, excluded, "
        "nickname_overwrite, updated_at) "
        "VALUES ('A', 'TWD', 2, 0, NULL, '2026-09-30T00:00:00')"
    )
    conn.commit()
    conn.close()

    store = BankStore("sinopac", user_id=1)
    try:
        store.upsert_accounts([{"account_no": "B"}])
        store.upsert_accounts([{"account_no": "B"}])
        rows = store.conn.execute(
            "SELECT account_no, currency, raw_balance, excluded, nickname_overwrite "
            "FROM accounts ORDER BY account_no"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("A", "TWD", 2.0, 1, "Private"),
            ("B", "TWD", None, 0, None),
        ]
        currency_info = next(
            row for row in store.conn.execute("PRAGMA table_info(accounts)").fetchall()
            if row["name"] == "currency"
        )
        assert currency_info["notnull"] == 1
        assert currency_info["dflt_value"] == "'TWD'"
    finally:
        store.close()


def test_store_rebuilds_current_pk_when_currency_values_are_not_canonical(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    db_path = tmp_path / "sinopac.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE accounts (
            user_id INTEGER NOT NULL DEFAULT 1,
            account_no TEXT NOT NULL,
            currency TEXT NOT NULL DEFAULT 'TWD',
            branch TEXT, nickname TEXT, type TEXT, product_type TEXT,
            raw_balance REAL, raw_balance_date TEXT,
            excluded INTEGER NOT NULL DEFAULT 0,
            nickname_overwrite TEXT, updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, account_no, currency)
        )
    """)
    conn.execute(
        "INSERT INTO accounts (account_no, currency, raw_balance, excluded, "
        "nickname_overwrite, updated_at) VALUES ('A', ' usd ', 1, 1, 'Private', '2026-09-29')"
    )
    conn.execute(
        "INSERT INTO accounts (account_no, currency, raw_balance, updated_at) "
        "VALUES ('A', 'USD', 2, '2026-09-30')"
    )
    conn.execute(
        "INSERT INTO accounts (account_no, currency, product_type, raw_balance, "
        "raw_balance_date, updated_at) VALUES "
        "('B', 'USD', 'deposit', 10, '2026-09-29', '2026-09-29')"
    )
    conn.execute(
        "INSERT INTO accounts (account_no, currency, nickname, raw_balance, updated_at) "
        "VALUES ('B', ' usd ', 'Newest', NULL, '2026-09-30')"
    )
    conn.commit()
    conn.close()

    store = BankStore("sinopac", user_id=1)
    try:
        rows = store.conn.execute(
            "SELECT account_no, currency, nickname, product_type, raw_balance, "
            "raw_balance_date, excluded, nickname_overwrite FROM accounts ORDER BY account_no"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("A", "USD", "", None, 2.0, None, 1, "Private"),
            ("B", "USD", "Newest", "deposit", 10.0, "2026-09-29", 0, None),
        ]
    finally:
        store.close()


def test_store_rejects_malformed_currency_even_with_current_pk(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    db_path = tmp_path / "sinopac.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE accounts (
            user_id INTEGER NOT NULL DEFAULT 1,
            account_no TEXT NOT NULL,
            currency TEXT NOT NULL DEFAULT 'TWD',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, account_no, currency)
        )
    """)
    conn.execute("INSERT INTO accounts VALUES (1, 'A', 'US$', '2026-09-30')")
    conn.commit()
    conn.close()

    with pytest.raises(sqlite3.OperationalError, match="invalid account currency"):
        BankStore("sinopac", user_id=1)


def test_store_repairs_legacy_accounts_without_currency(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    db_path = tmp_path / "sinopac.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE accounts (
            account_no TEXT PRIMARY KEY,
            raw_balance REAL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("INSERT INTO accounts VALUES ('A', 1, '2026-09-30')")
    conn.commit()
    conn.close()

    store = BankStore("sinopac", user_id=1)
    try:
        row = store.conn.execute(
            "SELECT user_id, account_no, currency, raw_balance FROM accounts"
        ).fetchone()
        pk = [
            column["name"] for column in sorted(
                store.conn.execute("PRAGMA table_info(accounts)"),
                key=lambda column: column["pk"],
            ) if column["pk"]
        ]
        assert row is not None
        assert tuple(row) == (1, "A", "TWD", 1.0)
        assert pk == ["user_id", "account_no", "currency"]
    finally:
        store.close()


def test_store_repairs_current_identity_default_and_stale_index(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    db_path = tmp_path / "sinopac.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE accounts (
            user_id INTEGER NOT NULL DEFAULT 1,
            account_no TEXT NOT NULL,
            currency TEXT NOT NULL,
            branch TEXT, nickname TEXT, type TEXT, product_type TEXT,
            raw_balance REAL, raw_balance_date TEXT,
            excluded INTEGER NOT NULL DEFAULT 0,
            nickname_overwrite TEXT, updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, account_no, currency)
        )
    """)
    conn.execute("CREATE UNIQUE INDEX ux_accounts_user_no ON accounts(user_id, account_no)")
    conn.execute(
        "INSERT INTO accounts (account_no, currency, updated_at) VALUES ('A', 'TWD', '2026-09-30')"
    )
    conn.commit()
    conn.close()

    store = BankStore("sinopac", user_id=1)
    try:
        currency = next(
            row for row in store.conn.execute("PRAGMA table_info(accounts)")
            if row["name"] == "currency"
        )
        index_columns = [
            row["name"] for row in store.conn.execute("PRAGMA index_info(ux_accounts_user_no)")
        ]
        store.upsert_accounts([{
            "account_no": "A", "currency": "USD", "raw_balance": 1,
            "raw_balance_date": "2026-09-30",
        }])
        assert str(currency["dflt_value"]).strip("'\"") == "TWD"
        assert index_columns == ["user_id", "account_no", "currency"]
    finally:
        store.close()


def test_store_revalidates_replaced_database_at_cached_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    first = BankStore("sinopac", user_id=1)
    db_path = first.db_path
    first.close()
    assert db_path is not None
    db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE accounts (
            user_id INTEGER NOT NULL DEFAULT 1,
            account_no TEXT NOT NULL,
            currency TEXT,
            updated_at TEXT,
            PRIMARY KEY (user_id, account_no)
        )
    """)
    conn.commit()
    conn.close()

    second = BankStore("sinopac", user_id=1)
    try:
        pk = [
            row["name"] for row in sorted(
                second.conn.execute("PRAGMA table_info(accounts)"),
                key=lambda row: row["pk"],
            ) if row["pk"]
        ]
        assert pk == ["user_id", "account_no", "currency"]
    finally:
        second.close()


def test_sinopac_store_migrates_legacy_fx_balance_to_real(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    db_path = tmp_path / "sinopac.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE balance_history (
            user_id INTEGER NOT NULL DEFAULT 1,
            snapshot_date TEXT NOT NULL,
            twd_balance INTEGER,
            fx_balance INTEGER,
            loan_balance INTEGER,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, snapshot_date)
        )
    """)
    conn.execute(
        "INSERT INTO balance_history VALUES (1, '2026-09-30', NULL, 10.99, NULL, 'now')"
    )
    conn.commit()
    conn.close()

    store = BankStore("sinopac", user_id=1, source_account_id=7)
    try:
        column = next(
            row for row in store.conn.execute("PRAGMA table_info(balance_history)").fetchall()
            if row["name"] == "fx_balance"
        )
        row = store.conn.execute("SELECT fx_balance FROM balance_history").fetchone()
        assert column["type"] == "REAL"
        assert row is not None
        assert row["fx_balance"] == 10.99
    finally:
        store.close()


def test_postgres_native_currency_migration_waits_for_tables_and_retries_failures() -> None:
    class Cursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return self._rows[0] if self._rows else None

    class Conn:
        def __init__(self):
            self.tables = set()
            self.fail_cast = False
            self.fail_user_id = False
            self.calls = []
            self.rollbacks = 0

        def execute(self, sql, params=()):
            self.calls.append((sql, params))
            if "information_schema.tables" in sql:
                return Cursor([(table,) for table in sorted(self.tables)])
            if "information_schema.columns" in sql:
                return Cursor([("expend",), ("income",), ("balance",)])
            if "ALTER COLUMN expend TYPE DOUBLE PRECISION" in sql and self.fail_cast:
                self.fail_cast = False
                raise RuntimeError("synthetic cast failure")
            if (
                "ALTER TABLE" in sql
                and '"accounts"' in sql
                and "ADD COLUMN IF NOT EXISTS user_id" in sql
                and self.fail_user_id
            ):
                self.fail_user_id = False
                raise RuntimeError("synthetic user_id failure")
            if "pg_index" in sql or "pg_constraint" in sql:
                return Cursor([])
            return Cursor([])

        def commit(self):
            pass

        def rollback(self):
            self.rollbacks += 1

    schema = "bank_freshcurrency"
    conn = Conn()
    bank_pg._reset_phase_c_pg_cache()

    bank_pg._ensure_phase_c_user_id_pg(conn, schema)
    assert schema not in bank_pg._PHASE_C_PG_MIGRATED

    conn.tables.add("twd_transactions")
    conn.fail_cast = True
    with pytest.raises(RuntimeError, match="synthetic cast failure"):
        bank_pg._ensure_phase_c_user_id_pg(conn, schema)
    assert schema not in bank_pg._PHASE_C_PG_MIGRATED

    conn.tables.update(bank_pg._PHASE_C_PG_TABLES)
    bank_pg._ensure_phase_c_user_id_pg(conn, schema)
    assert schema in bank_pg._PHASE_C_PG_MIGRATED
    assert sum("ALTER COLUMN expend TYPE DOUBLE PRECISION" in sql for sql, _ in conn.calls) == 2

    retry_schema = "bank_retryuserid"
    retry_conn = Conn()
    retry_conn.tables.add("accounts")
    retry_conn.fail_user_id = True
    with pytest.raises(RuntimeError, match="synthetic user_id failure"):
        bank_pg._ensure_phase_c_user_id_pg(retry_conn, retry_schema)
    assert retry_schema not in bank_pg._PHASE_C_PG_MIGRATED
    bank_pg._ensure_phase_c_user_id_pg(retry_conn, retry_schema)
    assert retry_schema not in bank_pg._PHASE_C_PG_MIGRATED

    class OrderedPkConn(Conn):
        def execute(self, sql, params=()):
            if "i.indisunique" in sql:
                self.calls.append((sql, params))
                return Cursor([("user_id", True), ("account_no", True), ("currency", True)])
            if "FROM pg_index" in sql:
                self.calls.append((sql, params))
                return Cursor([("user_id",), ("account_no",), ("currency",)])
            return super().execute(sql, params)

    ordered_schema = "bank_orderedpk"
    ordered_conn = OrderedPkConn()
    ordered_conn.tables.add("accounts")
    bank_pg._ensure_phase_c_user_id_pg(ordered_conn, ordered_schema)
    assert any("WITH ORDINALITY" in sql for sql, _ in ordered_conn.calls)
    assert not any("DROP CONSTRAINT accounts_pkey" in sql for sql, _ in ordered_conn.calls)


def test_bank_store_runs_post_schema_postgres_migrations() -> None:
    source = inspect.getsource(BankStore.__init__)
    assert "self.conn.ensure_schema_migrations()" in source


def test_postgres_migration_adds_native_currency_and_decimal_columns() -> None:
    source = inspect.getsource(bank_pg._ensure_phase_c_user_id_pg)
    assert "ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'TWD'" in source
    assert '("expend", "income", "balance")' in source
    assert '"fx_balance" in columns' in source
    assert "SELECT column_name FROM information_schema.columns" in source
    assert "TYPE DOUBLE PRECISION" in source


def test_account_transactions_full_coverage_unlocks_incremental_sync() -> None:
    summary = validate_history_coverage(
        _persist_payload()["history_coverage"],
        expected_mode="full",
        expected_domains=frozenset({"account_transactions"}),
    )
    assert _is_full_history_attestation(
        summary,
        expected_domains=frozenset({"account_transactions"}),
    )


def test_sinopac_persistence_requires_attested_coverage_before_write(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    payload = _persist_payload()
    payload.pop("history_coverage")
    try:
        with pytest.raises(ValueError, match="sinopac persistence requires history coverage"):
            persist_collected("sinopac", payload, store)
        assert store.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    finally:
        store.close()


def test_sinopac_persistence_rejects_operation_over_5mb(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    payload = _persist_payload()
    target = next(item for item in payload["account_transactions"] if item["records"])
    base = target["records"][0]
    target["records"] = [
        {**base, "DataText6": f"{index:04d}" + "x" * 1900}
        for index in range(3000)
    ]
    target["receipt"]["rows"] = len(target["records"])
    try:
        with pytest.raises(ValueError, match="invalid SinoPac history coverage"):
            persist_sinopac(payload, store)
        row = store.conn.execute("SELECT COUNT(*) FROM twd_transactions").fetchone()
        assert row is not None and row[0] == 0
    finally:
        store.close()


def test_sinopac_persistence_rejects_boolean_page_and_row_counts(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    payload = _persist_payload()
    target = payload["account_transactions"][-1]
    target["receipt"]["pages"] = True
    target["receipt"]["rows"] = True
    payload["history_coverage"]["domains"][0]["windows"][-1]["pages"] = True
    try:
        with pytest.raises(ValueError, match="history coverage"):
            persist_sinopac(payload, store)
    finally:
        store.close()


def test_sinopac_direct_persister_requires_history_coverage(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    try:
        with pytest.raises(ValueError, match="invalid SinoPac history coverage"):
            persist_sinopac({"bank_balance": []}, store)
        row = store.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()
        assert row is not None and row[0] == 0
    finally:
        store.close()


def test_sinopac_persistence_uses_coverage_as_of_for_card_expiry(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(sinopac_persist_module, "_today", lambda: date(2026, 9, 1))
    expiry_months = []
    original_expired = sinopac_persist_module._mmyy_expired

    def expired(value, today_yyyy_mm=None):
        expiry_months.append(today_yyyy_mm)
        return original_expired(value, today_yyyy_mm)

    monkeypatch.setattr(sinopac_persist_module, "_mmyy_expired", expired)
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    payload = _persist_payload()
    payload["all_cards"] = {
        "Result": {"Items": [{"CardNo": "1234", "ExpDate": "0826"}]},
    }
    try:
        persist_sinopac(payload, store)
        row = store.conn.execute(
            "SELECT active FROM cards WHERE card_no = ?", ("1234",),
        ).fetchone()
        assert row is not None and row[0] == 1
        assert expiry_months == ["2026-08"]
    finally:
        store.close()


def test_sinopac_card_expiry_uses_canonical_year_month() -> None:
    assert sinopac_persist_module._mmyy_expired("0926", "2026-10") is True


def test_sinopac_dom_binding_is_exact_mapped_multiset_not_substring_only() -> None:
    source = inspect.getsource(SinopacCrawler._collect_transactions)
    assert "const cellIndexes=[1,2,3,4,5,7]" in source
    assert "const domTuples=visibleRows.map" in source
    assert "const responseTuples=expected.map" in source
    assert "domTuples.every((value,index)=>value===responseTuples[index])" in source
    assert ".includes(" not in source
    assert "連線中斷|disconnected|retry" in source
    assert "!e.closest('#ListingTable')" not in source


def test_sinopac_persistence_rejects_html_only_description(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    payload = _persist_payload()
    payload["account_transactions"][-1]["records"][0]["DataText3"] = "<b>&nbsp;</b>"
    try:
        with pytest.raises(ValueError, match="history coverage"):
            persist_sinopac(payload, store)
    finally:
        store.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [("DataText1", "2026/8/1<br />1:02"), ("DataText2", "2026/8/1")],
)
def test_sinopac_persistence_rejects_noncanonical_dates(
    tmp_path, monkeypatch, field, value,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    payload = _persist_payload()
    payload["account_transactions"][-1]["records"][0][field] = value
    try:
        with pytest.raises(ValueError, match="history coverage"):
            persist_sinopac(payload, store)
    finally:
        store.close()


def test_sinopac_direct_persister_remains_durable(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    persist_sinopac(_persist_payload(), store)
    store.close()

    reopened = BankStore("sinopac", user_id=1, source_account_id=7)
    try:
        row = reopened.conn.execute(
            "SELECT txn_datetime FROM twd_transactions",
        ).fetchone()
        assert row["txn_datetime"] == "2026-08-31T12:34:00"
    finally:
        reopened.close()


def test_sinopac_direct_persister_rolls_back_on_late_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    monkeypatch.setattr(
        store, "refresh_card_pending",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("pending failed")),
    )
    try:
        with pytest.raises(RuntimeError, match="pending failed"):
            persist_sinopac(_persist_payload(), store)
        store.commit()
        assert store.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
        assert store.conn.execute("SELECT COUNT(*) FROM twd_transactions").fetchone()[0] == 0
    finally:
        store.close()


def test_sinopac_persistence_rolls_back_all_writes_on_cursor_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    monkeypatch.setattr(
        store, "record_history_coverage_cursors",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cursor write failed")),
    )
    try:
        with pytest.raises(RuntimeError, match="cursor write failed"):
            persist_collected("sinopac", _persist_payload(), store)
        assert store.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
        assert store.conn.execute("SELECT COUNT(*) FROM twd_transactions").fetchone()[0] == 0
        assert store.conn.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0] == 0
    finally:
        store.close()


def test_sinopac_incremental_persistence_binds_existing_cursor(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    monkeypatch.setattr(
        store, "latest_twd_transaction_dates", lambda: {ACCOUNT: date(2026, 8, 1)},
    )
    try:
        with pytest.raises(ValueError, match="SinoPac history"):
            persist_collected("sinopac", _persist_payload(mode="incremental"), store)
        assert store.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    finally:
        store.close()


@pytest.mark.parametrize("mode", ("full", "incremental"))
def test_sinopac_persistence_rejects_future_cursor_in_all_modes(tmp_path, monkeypatch, mode) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    monkeypatch.setattr(
        store, "latest_account_transaction_dates", lambda: {ACCOUNT: date(2026, 9, 1)},
    )
    try:
        with pytest.raises(ValueError, match="SinoPac history"):
            persist_collected("sinopac", _persist_payload(mode=mode), store)
        assert store.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    finally:
        store.close()


def test_sinopac_persistence_rejects_identity_inventory_mismatch(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    payload = _persist_payload()
    payload["debit_accounts"][0]["identity"] = "99999999999999"
    try:
        with pytest.raises(ValueError, match="SinoPac history"):
            persist_collected("sinopac", payload, store)
        assert store.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    finally:
        store.close()
