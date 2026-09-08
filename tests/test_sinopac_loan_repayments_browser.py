"""Synthetic offline native UI + CDP tests: all requests intercepted, no bank I/O."""
from copy import deepcopy
from datetime import date
import html
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from backend.banks.sinopac import LOAN_DETAIL_URL, LOAN_REPAYMENT_PATH, SinopacCrawler
from backend.core.base import ResponseCollector, _OriginGuardProxy

ORIGIN = "https://mma.sinopac.com"
OVERVIEW = ORIGIN + "/mma/bank/easy_index_loan/mma_loandetail.aspx"
ENDPOINT = ORIGIN + LOAN_REPAYMENT_PATH
CONTRACT = json.loads((Path(__file__).parent / "fixtures/sinopac_loan_api_contract.json").read_text())
ACCOUNT = CONTRACT["account_response"][0]["SubInfo"][0]
RECORD = CONTRACT["info_response"][0]["SubInfo"][0]
HEADERS = ["繳款日", "應繳日", "攤還本金", "繳息金額", "本金餘額", "違約金金額", "繳款金額", "交易狀態"]


def inputs(fields):
    return "".join(f'<input type="hidden" id="{k}" name="{k}" value="{html.escape(v, quote=True)}">' for k, v in fields.items())


@pytest.fixture
def native(monkeypatch):
    from patchright.sync_api import CDPSession, Locator, sync_playwright

    commands = []
    original_send = CDPSession.send
    def send(session, method, params=None):
        commands.append(method)
        assert method != "Network.loadNetworkResource", "Response replay forbidden"
        if method == "Network.getResponseBody" and state.get("body_failure"):
            raise RuntimeError("Synthetic missing CDP body")
        return original_send(session, method, params)
    monkeypatch.setattr(CDPSession, "send", send)
    original_click = Locator.click
    def click(locator, *args, **kwargs):
        kind = "query" if locator.get_attribute("id") == "btnQuery" else "detail"
        result = original_click(locator, *args, **kwargs)
        if state.get("click_failure") == kind:
            raise RuntimeError("Synthetic post-click context destruction")
        return result
    monkeypatch.setattr(Locator, "click", click)

    monkeypatch.setattr("backend.banks.sinopac._taipei_today", lambda: date(2026, 9, 7))
    monkeypatch.setattr("backend.core.persist.sinopac._today", lambda: date(2026, 9, 7))
    values = ["2026/08/06", "2026/08/06", "10,300.00", "300.00", "890,000.00", "0.00", "", "", "", "", "10,000.00"]
    row = dict(zip((f"DataValue{i}" for i in range(1, 12)), values, strict=True))
    state = {"commands": commands, "requests": [], "accounts": [deepcopy(ACCOUNT)], "records": [deepcopy(RECORD)],
             "selected": deepcopy(ACCOUNT), "text_type": "", "raw_responses": [],
             "response": {"HeadInfo": [dict.fromkeys(("HeadText", "HeadAlign", "DataAlign", "MainShow", "DetailShow", "FieldKey", "OrderIndex", "FieldWidth"), "") for _ in range(3)],
                          "SubInfo": [row, {**row, "DataValue1": "2026/09/06", "DataValue2": "2026/09/06"}], "Header": "SUCCESS", "Message": ""}}

    def links():
        result = []
        for record in state["records"]:
            args = ["detail", *(record[k] for k in ("Sub1_Sub2", "Currency", "LoanAmt", "LoanBalance", "LoanKind", "PayName"))]
            handler = "forQuery(" + ",".join("'" + x + "'" for x in args) + ");"
            result.append('<tr><td><a href="javascript:;" onclick="' + html.escape(handler.replace("'detail'", "'paydetail'"), quote=True) + '">繳款明細</a></td></tr>')
            result.append('<tr><td><a href="javascript:;" onclick="' + html.escape(handler, quote=True) + '">繳款明細</a></td></tr>')
        return "".join(result)

    def route(r):
        q = r.request
        path = urlparse(q.url).path
        fields = parse_qs(q.post_data or "", keep_blank_values=True)
        state["requests"].append((q.method, q.url, fields))
        if q.url == LOAN_DETAIL_URL and q.method == "GET":
            # Route fulfillment cannot intercept Chromium's internal HTTP redirect fetch.
            r.fulfill(content_type="text/html", body=f'<script>location.replace({json.dumps(OVERVIEW)})</script>')
        elif q.url == OVERVIEW:
            selected = state["selected"]
            markup = f'''<form id="tr_mma" method="post">{inputs({"AcctValue": selected["AcctValue"], "AcctValueFormat": selected["AcctValueFormat"], **dict.fromkeys(("LNMAINACNO", "LNALTNO", "CURRENCY", "LoanAmt", "LoanBalance", "Sub1_Sub2", "LoanKind", "PayName"), "")})}
            <button type="button" id="btnQuery" onclick="queryInfo()">查詢</button>
            <table id="tbDetails"><tbody>{links()}</tbody></table></form>
            <script>
            function send(url, form) {{const x=new XMLHttpRequest();x.open('POST',url,false);x.setRequestHeader('Content-Type','application/x-www-form-urlencoded');x.send(form);return x;}}
            send('/ws/bank/loan/ws_loanaccount.ashx','');
            function queryInfo() {{send('/ws/bank/loan/ws_loaninfo.ashx',new URLSearchParams(new FormData(document.querySelector('#tr_mma'))).toString());}}
            function forQuery(type,sub,cur,amount,balance,kind,pay) {{
              if(type!=='detail') throw Error('wrong link');
              const fields={{LNMAINACNO:document.querySelector('#AcctValue').value,LNALTNO:sub,CURRENCY:cur,LoanAmt:amount,LoanBalance:balance,Sub1_Sub2:sub,LoanKind:kind,PayName:pay}};
              for(const [key,value] of Object.entries(fields)) document.getElementById(key).value=value;
              const f=document.getElementById('tr_mma');f.action={json.dumps(LOAN_DETAIL_URL)};f.submit();
            }}</script>'''
            r.fulfill(status=200, content_type="text/html; charset=utf-8", body=markup)
        elif path.endswith("ws_loanaccount.ashx"):
            r.fulfill(json=[{"SubInfo": state["accounts"], "Header": "", "Message": ""}])
        elif path.endswith("ws_loaninfo.ashx"):
            state["selected"] = {k: fields[k][0] for k in ("AcctValue", "AcctValueFormat")}
            r.fulfill(json=[{"SubInfo": state["records"], "Header": "", "Message": ""}])
        elif q.url == LOAN_DETAIL_URL and q.method == "POST":
            form = {k: v[0] for k, v in fields.items()}
            form["TextType"] = state["text_type"]
            form.update(state.get("bad_fields", {}))
            markup = f'''<form id="tr_mma">{inputs(form)}
            <input id="StartDate" name="StartDate" value="20260801"><input id="EndDate" name="EndDate" value="20260907">
            <a href="javascript:;" id="btnQuery" onclick="doQuery()">查詢</a></form>
            <table id="tbDetails"><thead><tr>{''.join('<th>'+x+'</th>' for x in HEADERS)}</tr></thead>
            <tbody id="tbodyDetails"></tbody><tbody id="tbodyNoDetail" style="display:none"><tr><td>查無資料</td></tr></tbody></table>
            <script>function doQuery() {{
              const x=new XMLHttpRequest();x.open('POST',{json.dumps(ENDPOINT + '?1788750000000')},false);
              x.setRequestHeader('Content-Type','application/x-www-form-urlencoded');
              const form=new URLSearchParams(new FormData(document.getElementById('tr_mma')));
              {state.get('before_send', '')}
              x.send(form.toString());
              const data=JSON.parse(x.responseText)[0];
              document.getElementById('tbodyDetails').innerHTML=data.SubInfo.map(row=>'<tr>'+[2,1,11,4,5,6,3,10].map(i=>'<td>'+row['DataValue'+i]+'</td>').join('')+'</tr>').join('');
              {state.get('after_query', '')}
            }}</script>{state.get('extra', '')}'''
            r.fulfill(status=200, content_type="text/html; charset=utf-8", body=markup)
        elif path == LOAN_REPAYMENT_PATH:
            r.fulfill(status=state.get("status", 200), content_type=state.get("content_type", "application/json"), body=json.dumps([state["response"]]))
        else:
            r.abort()
            raise AssertionError("Unexpected offline request: " + q.url)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--host-resolver-rules=MAP * ~NOTFOUND", "--disable-background-networking"])
        try:
            context = browser.new_context(service_workers="block")
            context.route("**/*", route)
            raw = context.new_page()
            original_wait = raw.wait_for_timeout
            # Shorten only legacy inventory sleeps; still pump actual browser events.
            monkeypatch.setattr(raw, "wait_for_timeout", lambda ms: original_wait(min(ms, 40)))
            raw.on("response", lambda response: state["raw_responses"].append(response) if urlparse(response.url).path == LOAN_REPAYMENT_PATH else None)
            raw.goto(OVERVIEW)
            def guard():
                assert raw.url.startswith(ORIGIN + "/")
            page = _OriginGuardProxy(raw, guard)
            collector = ResponseCollector("sinopac.com")
            collector.attach(page)
            crawler = SinopacCrawler.__new__(SinopacCrawler)
            yield crawler, page, collector, state
            collector.detach(page)
        finally:
            browser.close()


def query_requests(state):
    return [r for r in state["requests"] if urlparse(r[1]).path == LOAN_REPAYMENT_PATH]


@pytest.mark.parametrize("message", ["", "synthetic nonempty notice", "x" * 2000],
                         ids=["empty", "nonempty", "maximum-length"])
def test_guarded_native_response_identity_and_default_query(native, message):
    crawler, page, collector, state = native
    # Live evidence: nonempty Message plus two matching API/DOM rows. Text is synthetic.
    state["response"]["Message"] = message
    repayment = crawler._collect_loan_repayments(page, collector, ACCOUNT, RECORD)
    assert repayment["records"] == state["response"]["SubInfo"]
    assert repayment["receipt"] == {"account": ACCOUNT["AcctValue"], "sub_account": RECORD["Sub1_Sub2"], "currency": "TWD", "start": "2026-08-01", "end": "2026-09-07", "status": "complete", "period": "native_default", "pages": 1, "rows": 2}
    response, = state["raw_responses"]
    assert response.request.frame is not page.main_frame
    assert page._wrap(response).request.frame is page.main_frame
    assert len(query_requests(state)) == 1
    assert collector.by_endpoint("ws_loandetail.ashx") == []


def test_preserves_nonempty_native_text_type(native):
    crawler, page, collector, state = native
    state["text_type"] = "native-default"
    crawler._collect_loan_repayments(page, collector, ACCOUNT, RECORD)
    assert query_requests(state)[0][2]["TextType"] == ["native-default"]


def test_outer_collects_each_account_and_subaccount(native):
    crawler, page, collector, state = native
    state["accounts"].append({**ACCOUNT, "AcctValue": "999999999999", "AcctValueFormat": "999-999-999999"})
    state["records"].append({**RECORD, "Sub1_Sub2": "99-0002"})
    result = crawler._collect_loans(page, collector)
    expected = [(a["AcctValue"], r["Sub1_Sub2"]) for a in state["accounts"] for r in state["records"]]
    actual = [(d["account"], r["sub_account"]) for d in result["details"] for r in d.get("repayments", [])]
    assert actual == expected
    assert [(r[2]["AcctValue"][0], r[2]["Sub1_Sub2"][0]) for r in query_requests(state)] == expected
    assert all(r[2]["StartDate"] == ["20260801"] and r[2]["EndDate"] == ["20260907"] for r in query_requests(state))
    assert result["fetch_ok"] is True
    assert page.url == OVERVIEW
    assert state["commands"].count("Network.getResponseBody") == 4


@pytest.mark.parametrize("attack,queries", [
    ("account", 0), ("subaccount", 0), ("currency", 0), ("extra_field", 0),
    ("text_type_bound", 1), ("oversized_text_type", 0), ("http", 1), ("envelope", 1),
    ("empty", 1), ("pager", 1), ("dom", 1), ("hidden", 1), ("modal", 1),
    ("body_failure", 1), ("query_click_failure", 1), ("detail_click_failure", 0),
    ("message_null", 1), ("message_bool", 1), ("message_number", 1),
    ("message_object", 1), ("message_array", 1), ("message_oversized", 1),
])
def test_outer_failure_never_retries_or_advances(native, attack, queries):
    crawler, page, collector, state = native
    state["records"].append({**RECORD, "Sub1_Sub2": "99-0002"})
    state["response"]["Message"] = "synthetic nonempty notice"
    if attack in {"account", "subaccount", "currency", "extra_field"}:
        key, value = {"account": ("LNMAINACNO", "999999999999"), "subaccount": ("LNALTNO", "99-9999"), "currency": ("CURRENCY", "USD"), "extra_field": ("Unknown", "value")}[attack]
        state["bad_fields"] = {key: value}
    elif attack == "text_type_bound":
        state["before_send"] = "form.set('TextType','changed');"
    elif attack == "oversized_text_type":
        state["text_type"] = "x" * 2001
    elif attack == "http":
        state["status"] = 500
    elif attack == "envelope":
        state["response"]["Header"] = "FAIL"
    elif attack == "empty":
        state["response"]["SubInfo"] = []
    elif attack in {"pager", "dom", "hidden", "modal"}:
        state["after_query"] = {
            "pager": "document.body.insertAdjacentHTML('beforeend','<button class=pager>下一頁</button>');",
            "dom": "document.querySelector('#tbodyDetails td').textContent='2026/08/07';",
            "hidden": "document.querySelector('#tbodyDetails').style.display='none';",
            "modal": "document.body.insertAdjacentHTML('beforeend','<div role=dialog>停止</div>');",
        }[attack]
    elif attack == "body_failure":
        state["body_failure"] = True
    elif attack.startswith("message_"):
        state["response"]["Message"] = {
            "message_null": None, "message_bool": False, "message_number": 0,
            "message_object": {}, "message_array": [], "message_oversized": "x" * 2001,
        }[attack]
    else:
        state["click_failure"] = attack.split("_")[0]
    with pytest.raises(RuntimeError, match="^sinopac-loan-repayments$"):
        crawler._collect_loans(page, collector)
    assert len(query_requests(state)) == queries
    assert len([r for r in state["requests"] if r[0] == "POST" and r[1] == LOAN_DETAIL_URL]) == 1
    assert page.url == LOAN_DETAIL_URL
    assert "Network.loadNetworkResource" not in state["commands"]


def test_identity_negative_control_reproduces_raw_callback_defect(native, monkeypatch):
    from backend.banks.sinopac import _LoanRepaymentBodyObserver
    from backend.core.base import _HistoryBodyObserver
    crawler, page, collector, state = native
    monkeypatch.setattr(_LoanRepaymentBodyObserver, "read", _HistoryBodyObserver.read)
    with pytest.raises(RuntimeError, match="^sinopac-loan-repayments$"):
        crawler._collect_loan_repayments(page, collector, ACCOUNT, RECORD)
    assert len(query_requests(state)) == 1
    assert "Network.getResponseBody" not in state["commands"]


def test_empty_loan_inventory_still_succeeds(native):
    crawler, page, collector, state = native
    state["accounts"] = []
    assert crawler._collect_loans(page, collector) == {"details": [], "fetch_ok": True}
    assert query_requests(state) == []
