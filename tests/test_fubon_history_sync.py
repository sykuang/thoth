from __future__ import annotations

import ast
from copy import deepcopy
from datetime import date
from html import escape
import inspect
import textwrap
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from backend.banks.fubon import (
    FubonCrawler,
    _fubon_history_windows,
    _validated_fubon_twd_options,
)
from backend.core.base import BankCollectResult, ResponseCollector, _OriginGuardProxy
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
            "frameExact": True,
            "fieldsExact": True,
            "requestCaptured": True,
            "responseMatched": True,
            "controlValuesMatched": True,
            "viewStateFingerprintMatched": True,
            "formBound": True,
        },
        "snapshot": {
            "evidenceFresh": True,
            "documentFresh": True,
            "documentReady": True,
            "busy": False,
            "failed": False,
            "selectedValue": "012-000-90000000267053-X-TW",
            "selectedIdentity": ACCOUNT,
            "hasGrid": not empty,
            "gridCandidateCount": 0 if empty else 1,
            "hiddenGridCount": 0,
            "hiddenGridDataRowCount": 0,
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
            _result("2025-08-30", "2026-02-27", "2025-09-02"),
            _result("2026-02-28", "2026-08-30", "2026-08-29"),
        ],
        "deposit_page_text": (
            f"{ACCOUNT}\n\t活儲存款\t測試分行\t臺幣\t84\t84\t快速功能"
        ),
        "card_bill_facts_ok": False,
    }


@pytest.fixture(scope="module")
def fubon_dom_snapshot():
    """Execute the collector's actual inline JS; no bank navigation or network."""
    from patchright.sync_api import sync_playwright
    from backend.banks.fubon import bounded_evaluate

    tree = ast.parse(textwrap.dedent(inspect.getsource(FubonCrawler._collect_twd_window)))
    sources = [
        node.value.args[1].value for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and len(node.value.args) > 1
        and isinstance(node.value.args[1], ast.Constant)
        and isinstance(node.value.args[1].value, str)
        and any(isinstance(target, ast.Name) and target.id == "snapshot" for target in node.targets)
    ]
    assert len(sources) == 1
    with sync_playwright() as patchright:
        if not Path(patchright.chromium.executable_path).exists():
            pytest.skip("Patchright browser binary is not installed")
        browser = patchright.chromium.launch(headless=True)
        try:
            page = browser.new_page(service_workers="block")
            page.route("**/*", lambda route: route.abort())

            def snapshot(*, description="利息", memo="", layout=False, mutate=None):
                result = _result("2026-02-28", "2026-08-30", "2026-08-29")
                row = result["snapshot"]["gridRows"][0]
                row[2], row[6] = description, memo
                headers = ("帳務日期", "交易時間", "摘要", "支出金額", "存入金額", "即時餘額", "附註")
                page.set_content(
                    '<style>td,th {min-width:20px;padding:2px}</style><form id="form1">'
                    f'<select id="form1:comboAccount"><option value="{result["account_value"]}">'
                    f'{ACCOUNT}</option></select><section><table id="transactions"><thead><tr>'
                    + "".join(f"<th>{header}</th>" for header in headers)
                    + '</tr></thead><tbody><tr id="transaction">'
                    + "".join(f"<td>{escape(cell)}</td>" for cell in row)
                    + '</tr></tbody></table><div id="total">共 1 筆</div></section></form>'
                )
                if layout:
                    page.evaluate("""() => {
                        const section=document.querySelector('section'), outer=document.createElement('table');
                        section.before(outer); outer.insertRow().insertCell().append(section);
                    }""")
                if mutate:
                    page.evaluate(mutate)
                result["snapshot"] = bounded_evaluate(page, sources[0])
                # about:blank tests DOM only; transport/URL are separate unit fixtures.
                assert result["snapshot"].pop("href") == "about:blank"
                return result

            yield snapshot
        finally:
            browser.close()


@pytest.mark.parametrize("header_tag", ["th", "td"])
def test_fubon_dom_layout_table_is_not_a_duplicate_grid(fubon_dom_snapshot, header_tag):
    baseline = fubon_dom_snapshot(description="failed retry", memo="loading")
    result = fubon_dom_snapshot(
        description="failed retry", memo="loading", layout=True,
        mutate="() => {document.querySelectorAll('th').forEach(cell=>{"
        + f"const replacement=document.createElement('{header_tag}');"
        + "replacement.textContent=[...cell.textContent].join(' \\u3000\\n');cell.replaceWith(replacement);});}",
    )
    assert result["snapshot"]["gridCandidateCount"] == 1
    assert result["snapshot"] == baseline["snapshot"]
    assert FubonCrawler._validated_twd_history_result(result)["status"] == "complete"


@pytest.mark.parametrize('hidden_duplicate', [False, True])
def test_fubon_header_sort_scripts_are_not_labels(fubon_dom_snapshot, hidden_duplicate):
    result = fubon_dom_snapshot(mutate="""() => {
        document.querySelectorAll('th').forEach(cell => {
            const label=document.createElement('a'); label.textContent=cell.textContent;
            const script=document.createElement('script'); script.textContent='window.syntheticSortInit = true;';
            const input=document.createElement('input');input.type='hidden';input.value='fixture-sort';
            cell.replaceChildren(script,label,input);
        });
        """ + ("const clone=document.querySelector('#transactions').cloneNode(true);clone.hidden=true;document.querySelector('section').append(clone);" if hidden_duplicate else "") + "}")
    assert result['snapshot']['gridCandidateCount'] == 1
    assert result['snapshot']['hiddenGridDataRowCount'] == int(hidden_duplicate)
    if hidden_duplicate:
        with pytest.raises(RuntimeError, match='fubon-twd-history-result'):
            FubonCrawler._validated_twd_history_result(result)
    else:
        assert FubonCrawler._validated_twd_history_result(result)['status'] == 'complete'


def test_fubon_script_only_headers_do_not_supply_visible_labels(fubon_dom_snapshot):
    result = fubon_dom_snapshot(mutate="""() => {
        document.querySelectorAll('th').forEach(cell=>{
            const script=document.createElement('script');script.type='text/plain';script.textContent=cell.textContent;
            cell.replaceChildren(script);
        });
    }""")
    assert result['snapshot']['gridCandidateCount'] == 0
    with pytest.raises(RuntimeError, match='fubon-twd-history-result'):
        FubonCrawler._validated_twd_history_result(result)


@pytest.mark.parametrize("mutate", [
    "document.querySelector('thead tr').append(document.querySelector('th'))",
    "document.querySelector('th').prepend('其他')",
    "() => {const row=document.querySelector('thead tr');row.innerHTML='<td>'+row.textContent+'</td>';}",
    "document.querySelectorAll('th').forEach(cell=>cell.innerHTML='<table><tr><td>'+cell.textContent+'</td></tr></table>')",
])
def test_fubon_dom_requires_exact_table_local_headers(fubon_dom_snapshot, mutate):
    result = fubon_dom_snapshot(mutate=mutate)
    assert result["snapshot"]["gridCandidateCount"] == 0
    with pytest.raises(RuntimeError, match="fubon-twd-history-result"):
        FubonCrawler._validated_twd_history_result(result)


@pytest.mark.parametrize("hidden, visible_count, hidden_rows", [(False, 2, 0), (True, 1, 1)])
def test_fubon_dom_layout_keeps_actual_duplicate_grids(fubon_dom_snapshot, hidden, visible_count, hidden_rows):
    result = fubon_dom_snapshot(
        layout=True,
        mutate="() => {const grid=document.querySelector('#transactions'), clone=grid.cloneNode(true);"
        + f"clone.hidden={json.dumps(hidden)};grid.after(clone);}}",
    )
    assert result["snapshot"]["gridCandidateCount"] == visible_count
    assert result["snapshot"]["hiddenGridDataRowCount"] == hidden_rows
    with pytest.raises(RuntimeError, match="fubon-twd-history-result"):
        FubonCrawler._validated_twd_history_result(result)


def test_fubon_dom_transaction_text_is_not_operation_status(fubon_dom_snapshot):
    baseline = fubon_dom_snapshot()
    assert FubonCrawler._validated_twd_history_result(baseline)["status"] == "complete"
    result = fubon_dom_snapshot(description="failed transfer retry", memo="loading 請稍候 系統錯誤")
    assert result["snapshot"]["failed"] is False
    assert result["snapshot"]["busy"] is False
    assert result["snapshot"]["gridRows"][0][2:] == [
        "failed transfer retry", "", "5.00", "84.00", "loading 請稍候 系統錯誤",
    ]
    assert FubonCrawler._validated_twd_history_result(result)["status"] == "complete"


@pytest.mark.parametrize('marker, flag', [
    ('class="error"', 'failed'), ('role="alert"', 'failed'),
    ('role="dialog"', 'failed'), ('aria-busy="true"', 'busy'),
    ('class="spinner"', 'busy'),
])
@pytest.mark.parametrize('hidden', [False, True])
def test_fubon_boxless_structural_blocker_without_error_keywords(fubon_dom_snapshot, marker, flag, hidden):
    html = '<div style="display:contents" ' + marker + (' hidden' if hidden else '') + '><span>請確認</span></div>'
    result = fubon_dom_snapshot(mutate='() => document.body.insertAdjacentHTML("beforeend", ' + json.dumps(html) + ')')
    assert result['snapshot'][flag] is not hidden
    if hidden:
        assert FubonCrawler._validated_twd_history_result(result)['status'] == 'complete'
    else:
        with pytest.raises(RuntimeError, match='fubon-twd-history-result'):
            FubonCrawler._validated_twd_history_result(result)


@pytest.mark.parametrize("style", ["display:contents", "width:0;height:0;overflow:visible"])
@pytest.mark.parametrize("marker, flag", [("failed retry", "failed"), ("loading 請稍候", "busy")])
def test_fubon_dom_boxless_ancestors_keep_operational_text(fubon_dom_snapshot, style, marker, flag):
    result = fubon_dom_snapshot(
        description="failed retry", memo="loading",
        mutate="() => {const section=document.querySelector('section');"
        + f"section.style.cssText={json.dumps(style)};"
        + f"section.insertAdjacentHTML('beforeend', {json.dumps('<p>' + marker + '</p>')});}}",
    )
    assert result["snapshot"]["gridCandidateCount"] == 1
    assert result["snapshot"][flag] is True
    with pytest.raises(RuntimeError, match="fubon-twd-history-result"):
        FubonCrawler._validated_twd_history_result(result)


@pytest.mark.parametrize("hidden", [
    'style="display:none"', 'style="visibility:hidden"', 'style="visibility:collapse"',
    'style="opacity:0"', 'hidden', 'aria-hidden="true"',
])
def test_fubon_dom_boxless_ancestors_ignore_hidden_status(fubon_dom_snapshot, hidden):
    result = fubon_dom_snapshot(
        description="failed retry", memo="loading",
        mutate="() => {const section=document.querySelector('section');section.style.display='contents';"
        + f"section.insertAdjacentHTML('beforeend', {json.dumps('<div ' + hidden + '><p>failed retry loading 請稍候</p></div>')});}}",
    )
    assert result["snapshot"]["failed"] is False
    assert result["snapshot"]["busy"] is False
    assert FubonCrawler._validated_twd_history_result(result)["status"] == "complete"


@pytest.mark.parametrize("amounts", [
    ["-", "+5.00", "84.00"],
    ["5", "-", "-84.00"],
    ["", "-0.00", "+1,084.00"],
])
def test_fubon_dom_status_exclusion_accepts_persistable_money(fubon_dom_snapshot, amounts):
    result = fubon_dom_snapshot(
        description="failed retry", memo="loading",
        mutate="() => { const cells=document.querySelector('#transaction').cells;"
        + f"{json.dumps(amounts)}.forEach((value,index)=>cells[index+3].textContent=value); }}",
    )
    assert result["snapshot"]["failed"] is False
    assert result["snapshot"]["busy"] is False
    assert FubonCrawler._validated_twd_history_result(result)["status"] == "complete"


@pytest.mark.parametrize("mutate, flag", [
    ("document.body.insertAdjacentHTML('beforeend', '<p>failed retry</p>')", "failed"),
    ("document.body.insertAdjacentHTML('afterbegin', '<p>loading 請稍候</p>')", "busy"),
    ("document.body.insertAdjacentHTML('beforeend', '<p>fail<span>ed</span></p>')", "failed"),
    ("document.body.insertAdjacentHTML('beforeend', 'load<span>ing</span>')", "busy"),
    ("document.querySelector('section').insertAdjacentHTML('beforeend', '<p>系統錯誤</p>')", "failed"),
    ("document.querySelector('tbody').insertAdjacentHTML('beforeend', '<tr><td colspan=7>failed</td></tr>')", "failed"),
    ("document.querySelector('tbody').insertAdjacentHTML('afterbegin', '<tr><td colspan=7>loading</td></tr>')", "busy"),
    ("document.querySelector('tbody').insertAdjacentHTML('beforeend', '<tr><td>2026/08/29</td><td></td><td>failed</td><td></td><td></td><td></td><td></td></tr>')", "failed"),
    ("document.querySelector('#transaction td:nth-child(7)').innerHTML = '<span role=alert>問題</span>'", "failed"),
    ("document.querySelector('#transaction td:nth-child(7)').innerHTML = '<span class=errorMessage>問題</span>'", "failed"),
    ("document.querySelector('#transaction td:nth-child(7)').innerHTML = '<span aria-invalid=true>問題</span>'", "failed"),
    ("document.querySelector('#transaction td:nth-child(7)').innerHTML = '<dialog open>問題</dialog>'", "failed"),
    ("document.querySelector('#transaction').setAttribute('aria-busy', 'true')", "busy"),
    ("document.querySelector('#transaction td:nth-child(7)').innerHTML = '<span role=progressbar>進行</span>'", "busy"),
    ("document.querySelector('#transaction td:nth-child(7)').innerHTML = '<span class=spinner>進行</span>'", "busy"),
])
def test_fubon_dom_keeps_operational_markers_inside_and_outside_grid(fubon_dom_snapshot, mutate, flag):
    result = fubon_dom_snapshot(description="failed transfer retry", memo="loading", mutate=mutate)
    assert result["snapshot"][flag] is True
    with pytest.raises(RuntimeError, match="fubon-twd-history-result"):
        FubonCrawler._validated_twd_history_result(result)


@pytest.mark.parametrize("layout", [False, True])
@pytest.mark.parametrize("mutate", [
    "document.querySelector('th').textContent='其他欄名'",
    "document.querySelector('#total').textContent='共 2 筆'",
    "document.querySelector('#total').insertAdjacentHTML('afterend', '<div>共 1 筆</div>')",
    "document.querySelector('#total').insertAdjacentHTML('beforebegin', '<div>其他內容</div>')",
    "document.querySelector('#transaction td').textContent='2025/01/01'",
    "document.querySelector('option').textContent='90000000267054'",
    "document.querySelector('option').value='012-000-90000000267054-X-TW'",
    "document.querySelector('form').setAttribute('data-hermes-pre-submit-form','1')",
    "document.querySelector('#transactions').setAttribute('data-hermes-stale-evidence','1')",
    "document.querySelector('#transaction').hidden=true",
    "document.querySelector('#transaction td:nth-child(7)').hidden=true",
    "document.querySelector('#transaction td:nth-child(7)').remove()",
    "document.querySelector('#transactions').insertAdjacentHTML('afterend', '<a rel=next>下一頁</a>')",
    "document.querySelector('#transactions').insertAdjacentHTML('afterend', '<div>查無相關資料</div>')",
    "document.querySelector('#transactions').insertAdjacentHTML('afterend', document.querySelector('#transactions').outerHTML)",
    "() => {const clone=document.querySelector('#transactions').cloneNode(true);clone.hidden=true;document.querySelector('section').append(clone);}",
])
def test_fubon_dom_data_text_exclusion_preserves_attestation_guards(fubon_dom_snapshot, mutate, layout):
    result = fubon_dom_snapshot(description="failed retry", memo="loading", layout=layout, mutate=mutate)
    with pytest.raises(RuntimeError, match="fubon-twd-history-result"):
        FubonCrawler._validated_twd_history_result(result)


def test_fubon_dom_memos_survive_persistence(fubon_dom_snapshot, tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    data = _payload()
    data["deposit_txn_results"][1] = fubon_dom_snapshot(
        description="failed transfer retry", memo="loading 請稍候 系統錯誤",
    )
    store = BankStore("fubon", user_id=7, source_account_id=97)
    try:
        assert persist_collected("fubon", data, store)["twd_txn_new"] == 2
        row = store.conn.execute(
            "SELECT raw_description, memo FROM twd_transactions WHERE account_date='2026-08-29'"
        ).fetchone()
        assert row is not None
        assert tuple(row) == ("failed transfer retry", "loading 請稍候 系統錯誤")
        assert store.latest_twd_transaction_dates() == {ACCOUNT: date(2026, 8, 30)}
    finally:
        store.close()


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
    opaque = [options[0], {**options[1], "value": f"012-A{ACCOUNT}-TWD"}]
    assert _validated_fubon_twd_options(opaque)[0]["identity"] == ACCOUNT
    digit_adjacent = [options[0], {**options[1], "value": f"012-99{ACCOUNT}88-X"}]
    assert _validated_fubon_twd_options(digit_adjacent)[0]["identity"] == ACCOUNT
    for bad in (
        [*options, deepcopy(options[1])],
        [options[0], {**options[1], "index": 2, "value": "arbitrary-TW"}],
        [options[0], {**options[1], "index": 2}],
        [
            options[0],
            {"index": 1, "value": "012-000-99876543210987-X-TW", "text": "1234567890 (測試分行)"},
        ],
        [options[0]],
        [*options, {"index": 2, "value": "012-0000000000000002-US", "text": "90000000267054 (測試分行)"}],
        [*options, {"index": 2, "value": "loading", "text": "資料載入中"}],
        [options[0], {**options[1], "value": f"012-{ACCOUNT}<script>"}],
    ):
        with pytest.raises(ValueError, match="inventory"):
            _validated_fubon_twd_options(bad)


def test_fubon_result_requires_transport_account_range_and_complete_dom():
    valid = _result("2025-08-30", "2026-02-27", "2025-09-02")
    without_total_marker = deepcopy(valid)
    without_total_marker["snapshot"]["nativeTotalFound"] = False
    without_total_marker["snapshot"]["nativeTotalMarkerCount"] = 0
    assert FubonCrawler._validated_twd_history_result(without_total_marker)["status"] == "complete"
    unbound_total_marker = deepcopy(without_total_marker)
    unbound_total_marker["snapshot"]["nativeTotalMarkerCount"] = 1
    with pytest.raises(RuntimeError, match="fubon-twd-history-result"):
        FubonCrawler._validated_twd_history_result(unbound_total_marker)
    hidden_template = deepcopy(valid)
    hidden_template["snapshot"]["hiddenGridCount"] = 1
    assert FubonCrawler._validated_twd_history_result(hidden_template)["status"] == "complete"
    opaque = deepcopy(valid)
    opaque["account_value"] = f"012-A{ACCOUNT}-PRIVATE_OPAQUE_TOKEN"
    opaque["snapshot"]["selectedValue"] = opaque["account_value"]
    assert FubonCrawler._validated_twd_history_result(opaque)["identity"] == ACCOUNT
    sanitized = deepcopy(opaque)
    sanitized.pop("account_value")
    sanitized["snapshot"].pop("selectedValue")
    sanitized["snapshot"]["selectedValueBound"] = True
    assert FubonCrawler._validated_twd_history_result(sanitized)["identity"] == ACCOUNT
    assert "PRIVATE_OPAQUE_TOKEN" not in json.dumps(sanitized)
    mutations = (
        lambda item: item.update(url="https://ebank.taipeifubon.com.tw/B2C/wrong.faces"),
        lambda item: item.update(url="https://ebank.taipeifubon.com.tw/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces;attacker"),
        lambda item: item["transport"].update(status=204),
        lambda item: item["transport"].update(responseCount=2),
        lambda item: item["transport"].update(frameExact=False),
        lambda item: item["transport"].update(fieldsExact=False),
        lambda item: item["transport"].update(requestCaptured=False),
        lambda item: item["transport"].update(responseMatched=False),
        lambda item: item["transport"].update(controlValuesMatched=False),
        lambda item: item["transport"].update(viewStateFingerprintMatched=False),
        lambda item: item["transport"].update(formBound=False),
        lambda item: item.update(
            url="https://ebank.taipeifubon.com.tw/b2c/cdsqu/cdsqu001/cdsqu001_home.faces",
        ),
        lambda item: item["snapshot"].update(selectedIdentity="90000000267054"),
        lambda item: item["snapshot"].update(selectedValue="012-000-90000000267054-X-TW"),
        lambda item: item["snapshot"].update(documentFresh=False),
        lambda item: item["snapshot"].update(documentReady=False),
        lambda item: item["snapshot"].update(failed=True),
        lambda item: item["snapshot"].update(nativeTotalMarkerCount=2),
        lambda item: item["snapshot"].update(rawDataRowCount=2),
        lambda item: item["snapshot"].update(hiddenGridDataRowCount=1),
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


def test_fubon_navigation_waits_for_delayed_history_anchor(monkeypatch):
    crawler = object.__new__(FubonCrawler)
    frame = SimpleNamespace(locator=lambda _selector: evaluator)
    evaluator = SimpleNamespace(
        evaluate=Mock(side_effect=[
            {"ok": False, "count": 0},
            {"ok": True},
        ])
    )
    page = Mock()
    monkeypatch.setattr(
        crawler,
        "_fubon_content_frame",
        Mock(return_value=frame),
    )

    assert crawler._open_twd_query(page) is frame
    assert evaluator.evaluate.call_count == 2
    assert page.wait_for_timeout.call_args_list == [call(5000), call(500), call(8000)]


def test_fubon_window_rereads_native_controls_after_ajax_settle():
    source = inspect.getsource(FubonCrawler._collect_twd_window)
    assert '("requestfinished", control_finished)' in source
    assert '("requestfailed", control_failed_request)' in source
    assert "remove_control_listeners()" in source
    assert source.count("add_control_listeners()") >= 2
    assert 'click_control("#form1\\\\:rdoTxDetail")' in source
    assert 'click_control("#form1\\\\:rdoFast")' in source
    assert "click_control(f\"#form1\\\\:{window['preset']}\")" in source
    assert "control_pending or control_failed or stable_ticks < 10" in source
    assert 'settled["presetValue"]' in source
    assert 'settled["viewState"]' in source
    assert "dates.length===0&&isHeaderRow(row)" in source
    assert "rawDataRowCount++;" in source
    assert source.index("dates.length===0&&isHeaderRow(row)") < source.index("rawDataRowCount++;")
    assert source.index("stable_ticks < 10") < source.index('settled["presetValue"]')


def test_fubon_result_accepts_only_live_queryless_post_submit_url():
    valid = _result("2025-08-30", "2026-02-27", "2025-09-02")
    assert FubonCrawler._validated_twd_history_result(valid)["status"] == "complete"
    for query in (
        "menuId=CDS0401",
        "account=PRIVATE",
        "menuId=private",
        "menuId=CDS0401&&",
        "menuId=CDS0401&menuId=CDS0401",
        "menuId=",
        "menuId=CDS0401&extra=1",
    ):
        invalid = deepcopy(valid)
        invalid["url"] += "?" + query
        with pytest.raises(RuntimeError, match="fubon-twd-history-result"):
            FubonCrawler._validated_twd_history_result(invalid)


def test_fubon_frame_and_post_binding_are_exact(monkeypatch):
    crawler = object.__new__(FubonCrawler)
    crawler._is_owned_frame = lambda page, frame: page is not None and frame is not None
    correct = SimpleNamespace(
        url=(
            "https://ebank.taipeifubon.com.tw/B2C/cdsqu/cdsqu001/"
            "CDSQU001_Home.faces?menuId=CDS0401"
        ),
        name="",
    )
    misleading = SimpleNamespace(
        url="https://ebank.taipeifubon.com.tw/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces;attacker",
        name="txnFrame",
    )
    wrong_query = SimpleNamespace(
        url=(
            "https://ebank.taipeifubon.com.tw/B2C/cdsqu/cdsqu001/"
            "CDSQU001_Home.faces?account=PRIVATE"
        ),
        name="txnFrame",
    )
    duplicate_query = SimpleNamespace(
        url=(
            "https://ebank.taipeifubon.com.tw/B2C/cdsqu/cdsqu001/"
            "CDSQU001_Home.faces?menuId=CDS0401&menuId=CDS0401"
        ),
        name="txnFrame",
    )
    lowercase = SimpleNamespace(
        url="https://ebank.taipeifubon.com.tw/b2c/cdsqu/cdsqu001/cdsqu001_home.faces",
        name="",
    )
    page = SimpleNamespace(
        frames=[misleading, wrong_query, duplicate_query, lowercase, correct]
    )
    assert crawler._fubon_content_frame(page, "/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces") is correct
    monkeypatch.setattr(
        crawler, "_is_owned_frame", lambda page, frame: frame is correct,
    )
    correct.url = correct.url.split("?", 1)[0]
    assert crawler._bound_twd_result_frame(page, correct) is correct

    owned_page = object()
    raw_frame = SimpleNamespace(page=owned_page)
    guarded_frame = _OriginGuardProxy(raw_frame, lambda: None)
    settled_state = "ABCDEFGH" + "A" * 121
    request_state = "ABCDEFGH" + "B" * 121
    request = SimpleNamespace(
        url=correct.url.split("?", 1)[0],
        method="POST",
        frame=raw_frame,
        post_data=f"ajaxAction=query-action&checkedConvenientPeriod=native-180&javax.faces.ViewState={request_state}",
    )
    response = SimpleNamespace(
        url=correct.url.split("?", 1)[0],
        request=request,
        status=200,
        headers={"content-type": "text/plain; charset=UTF-8"},
    )
    requests = []
    request.post_data = "ajaxAction=combo-change"
    FubonCrawler._capture_twd_request(
        request, requests, guarded_frame, True, {("query-action", "native-180")}, settled_state,
    )
    assert requests == []
    request.post_data = f"ajaxAction=query-action&checkedConvenientPeriod=native-180&javax.faces.ViewState={request_state}"
    FubonCrawler._capture_twd_request(
        request, requests, guarded_frame, True, {("query-action", "native-180")}, settled_state,
    )
    assert requests == [request]
    for post_data in (
        f"ajaxAction=wrong&checkedConvenientPeriod=native-180&javax.faces.ViewState={request_state}",
        f"ajaxAction=query-action&checkedConvenientPeriod=wrong&javax.faces.ViewState={request_state}",
        f"ajaxAction=query-action&checkedConvenientPeriod=native-180&javax.faces.ViewState={'ZZZZZZZZ' + 'B' * 121}",
    ):
        request.post_data = post_data
        rejected = []
        FubonCrawler._capture_twd_request(
            request, rejected, guarded_frame, True, {("query-action", "native-180")}, settled_state,
        )
        assert rejected == []
    request.post_data = f"ajaxAction=query-action&checkedConvenientPeriod=other-preset&javax.faces.ViewState={request_state}"
    cross_product = []
    FubonCrawler._capture_twd_request(
        request, cross_product, guarded_frame, True,
        {("query-action", "native-180"), ("other-action", "other-preset")}, settled_state,
    )
    assert cross_product == []
    request.post_data = f"ajaxAction=query-action&checkedConvenientPeriod=native-180&javax.faces.ViewState={request_state}"
    hits = []
    FubonCrawler._capture_twd_response(response, hits, requests)
    assert hits == [{
        "status": 200,
        "contentType": "text/plain",
        "frameExact": True,
        "fieldsExact": True,
        "requestCaptured": True,
        "responseMatched": True,
        "controlValuesMatched": True,
        "viewStateFingerprintMatched": True,
        "formBound": True,
    }]
    response.request = SimpleNamespace()
    mismatched_request_hits = []
    FubonCrawler._capture_twd_response(response, mismatched_request_hits, requests)
    assert mismatched_request_hits == []
    response.request = request
    response.url = correct.url + "?menuId=CDS0401"
    query_bearing_hits = []
    FubonCrawler._capture_twd_response(response, query_bearing_hits, requests)
    assert query_bearing_hits == []
    response.url = correct.url.split("?", 1)[0]
    request.frame = SimpleNamespace(page=owned_page)
    sibling_frame_requests = []
    FubonCrawler._capture_twd_request(
        request, sibling_frame_requests, guarded_frame, True,
        {("query-action", "native-180")}, settled_state,
    )
    assert sibling_frame_requests == []


def test_fubon_result_rejects_pager_busy_and_ambiguous_empty():
    valid = _result("2026-02-28", "2026-08-30", "2026-08-29")
    assert FubonCrawler._validated_twd_history_result(valid)["status"] == "complete"
    with pytest.raises(RuntimeError, match="fubon-twd-history-result"):
        FubonCrawler._validated_twd_history_result(
            _result("2026-02-28", "2026-08-30", "2026-08-29", empty=True),
        )
    for mutation in ("pager", "busy", "empty", "count", "stale"):
        bad = deepcopy(valid)
        if mutation == "pager": bad["snapshot"]["pager"] = {"present": True, "actionableNext": 1}
        elif mutation == "busy": bad["snapshot"]["busy"] = True
        elif mutation == "empty": bad["snapshot"]["emptyMarker"] = "查無相關資料"
        elif mutation == "count": bad["snapshot"]["totalCount"] = 2
        else: bad["snapshot"]["evidenceFresh"] = False
        with pytest.raises(RuntimeError, match="fubon-twd-history-result"):
            FubonCrawler._validated_twd_history_result(bad)


def test_fubon_cli_keeps_dom_history_in_canonical_db_only(monkeypatch):
    from cli import cli
    from backend.core import persist as persist_module
    from backend.server import rules_repo

    class Crawler:
        HISTORY_COVERAGE_REQUIRED = True
        HISTORY_COVERAGE_DOMAINS = frozenset({"twd_transactions"})

        @staticmethod
        def configure_transaction_cursor(_domain, _cursor):
            pass

        @staticmethod
        def run(*, login_url, headless):
            return {"data": _payload()}

    class Store:
        db_path = "private"

        @staticmethod
        def latest_twd_transaction_dates():
            return {}

        @staticmethod
        def latest_card_transaction_dates():
            return {}

        @staticmethod
        def stats():
            return {}

        @staticmethod
        def close():
            pass

    monkeypatch.setattr(cli, "_get_crawler", lambda _bank: (Crawler(), "https://example.com"))
    monkeypatch.setattr(cli, "BankStore", lambda _bank: Store())
    monkeypatch.setattr(rules_repo, "list_rules", lambda **_kwargs: [{}])
    monkeypatch.setattr(persist_module, "persist_collected", lambda *_args, **_kwargs: {})
    removed = []
    monkeypatch.setattr(
        cli,
        "_write_private_json",
        lambda *_args, **_kwargs: pytest.fail("Fubon collected backup must stay disabled"),
    )
    monkeypatch.setattr(cli, "_remove_private_json", lambda path: removed.append(path.name))
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")

    assert cli.cmd_sync(SimpleNamespace(bank="fubon", headless=True)) == 0
    assert removed == ["fubon_collected.json"]


def test_fubon_valid_attested_payload_persists_and_advances_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("fubon", user_id=7, source_account_id=97)
    try:
        data = _payload()
        for result in data["deposit_txn_results"]:
            result.pop("account_value")
            result["snapshot"].pop("selectedValue")
            result["snapshot"]["selectedValueBound"] = True
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
