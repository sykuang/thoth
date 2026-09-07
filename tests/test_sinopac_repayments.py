"""Offline native browser tests; every URL is fulfilled locally, never sent to a bank."""
from copy import deepcopy
import html
import json
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from backend.banks.sinopac import LOAN_DETAIL_URL, SinopacCrawler
from backend.core.base import ResponseCollector
from backend.core.persist.sinopac import _persist_sinopac
from backend.core.store import BankStore
from tests.test_sinopac_loan import FIXTURE, _repayment

ORIGIN = "https://mma.sinopac.com"
OVERVIEW = ORIGIN + "/mma/bank/easy_index_loan/mma_loandetail.aspx"
ENDPOINT = ORIGIN + "/ws/bank/loan/ws_loandetail.ashx"
ACCOUNT = FIXTURE["account_response"][0]["SubInfo"][0]
RECORD = FIXTURE["info_response"][0]["SubInfo"][0]
HEADERS = ["繳款日", "應繳日", "攤還本金", "繳息金額", "本金餘額", "違約金金額", "繳款金額", "交易狀態"]


@pytest.fixture
def native_page():
    from patchright.sync_api import sync_playwright

    state = {"requests": [], "response": {"HeadInfo": [dict.fromkeys(
        ("HeadText", "HeadAlign", "DataAlign", "MainShow", "DetailShow", "FieldKey", "OrderIndex", "FieldWidth"), ""
    ) for _ in range(3)], "SubInfo": _repayment()["records"], "Header": "SUCCESS", "Message": ""}}

    def inputs(fields):
        return "".join(f'<input type="hidden" id="{key}" name="{key}" value="{html.escape(value, quote=True)}">'
                       for key, value in fields.items())

    def route(request_route):
        request = request_route.request
        state["requests"].append((request.method, request.url))
        if request.url == OVERVIEW:
            args = ["detail", RECORD["Sub1_Sub2"], RECORD["Currency"], RECORD["LoanAmt"],
                    RECORD["LoanBalance"], RECORD["LoanKind"], RECORD["PayName"]]
            handler = "forQuery(" + ",".join("'" + arg + "'" for arg in args) + ");"
            markup = f'''<form id="tr_mma" method="post">{inputs({
                "AcctValue": ACCOUNT["AcctValue"], "AcctValueFormat": ACCOUNT["AcctValueFormat"],
                **dict.fromkeys(("LNMAINACNO", "LNALTNO", "CURRENCY", "LoanAmt", "LoanBalance", "Sub1_Sub2", "LoanKind", "PayName"), "")})}
                <table id="tbDetails"><tbody><tr><th>貸款明細</th></tr><tr><th>查詢</th></tr><tr><td>
                <a href="javascript:;" onclick="{html.escape(handler.replace("'detail'", "'paydetail'"), quote=True)}">其他明細</a>
                <a href="javascript:;" onclick="{html.escape(handler, quote=True)}">繳款明細</a></td></tr></tbody></table></form>
                <script>function forQuery(type, sub, cur, amount, balance, kind, pay) {{
                    if(type !== 'detail') throw Error('payment path must never be selected');
                    const values = {{LNMAINACNO:document.querySelector('#AcctValue').value, LNALTNO:sub,
                        CURRENCY:cur, LoanAmt:amount, LoanBalance:balance, Sub1_Sub2:sub, LoanKind:kind, PayName:pay}};
                    for(const [key,value] of Object.entries(values)) document.getElementById(key).value=value;
                    const form=document.getElementById('tr_mma'); form.action={json.dumps(LOAN_DETAIL_URL)}; form.submit();
                }}</script>'''
            request_route.fulfill(status=200, content_type="text/html; charset=utf-8", body=markup)
        elif request.url == LOAN_DETAIL_URL and request.method == "POST":
            fields = {k: v[0] for k, v in parse_qs(request.post_data, keep_blank_values=True).items()}
            fields["TextType"] = ""
            fields.update(state.get("history_fields", {}))
            markup = f'''<form id="tr_mma">{inputs(fields)}
                <input id="StartDate" name="StartDate" value="20260801">
                <input id="EndDate" name="EndDate" value="20260907">
                <a id="btnQuery" href="javascript:;" onclick="doQuery()">查詢</a></form>
                <table id="tbDetails"><thead><tr>{''.join('<th>'+x+'</th>' for x in HEADERS)}</tr><tr><th colspan="8"></th></tr></thead>
                <tbody id="tbodyDetails"></tbody><tbody id="tbodyNoDetail" style="display:none"><tr><td>查無資料</td></tr></tbody></table>
                <script>function doQuery() {{
                    const request=new XMLHttpRequest(); request.open('POST',{json.dumps(ENDPOINT)},false);
                    request.setRequestHeader('Content-Type','application/x-www-form-urlencoded');
                    request.send(new URLSearchParams(new FormData(document.getElementById('tr_mma'))).toString());
                    const data=JSON.parse(request.responseText)[0];
                    document.getElementById('tbodyDetails').innerHTML=data.SubInfo.map(row=>'<tr>'+[2,1,11,4,5,6,3,10].map(i=>'<td><span>'+row['DataValue'+i]+'</span></td>').join('')+'</tr>').join('');
                    {state.get('query_after', '')}
                }}</script>{state.get('history_extra', '')}'''
            request_route.fulfill(status=200, content_type="text/html; charset=utf-8", body=markup)
        elif request.url == ENDPOINT:
            state["query_form"] = parse_qs(request.post_data, keep_blank_values=True)
            request_route.fulfill(status=200, content_type="application/json", body=json.dumps([state["response"]]))
        else:
            request_route.abort()

    with sync_playwright() as playwright:
        assert Path(playwright.chromium.executable_path).exists(), "Offline browser binary required"
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(service_workers="block")
            context.route("**/*", route)
            page = context.new_page()
            collector = ResponseCollector("sinopac.com")
            collector.attach(page)
            page.goto(OVERVIEW)
            yield page, collector, state
        finally:
            browser.close()


@pytest.mark.parametrize("attack", [
    "wrong_account_before", "wrong_path_before", "wrong_subaccount", "wrong_currency",
    "wrong_mainaccount", "wrong_formatted", "unexpected_field", "duplicate_field",
    "bad_header", "bad_message", "bad_metadata", "pager", "modal", "changed_headers",
    "hidden_rows", "empty_visible", "changed_dates", "dialog",
])
def test_native_repayment_rejects_unattested_results(native_page, attack):
    page, collector, state = native_page
    crawler = SinopacCrawler.__new__(SinopacCrawler)
    fields = {"wrong_subaccount": ("LNALTNO", "99-0002"), "wrong_currency": ("CURRENCY", "USD"),
              "wrong_mainaccount": ("LNMAINACNO", "999999999999"), "wrong_formatted": ("AcctValueFormat", "999-999-999999"),
              "unexpected_field": ("Unknown", "value")}
    if attack in fields:
        key, value = fields[attack]
        state["history_fields"] = {key: value}
    elif attack == "wrong_account_before":
        page.locator("#AcctValue").evaluate("el => el.value='999999999999'")
    elif attack == "wrong_path_before":
        page.evaluate("history.replaceState(null,'','/unexpected')")
    elif attack == "duplicate_field":
        state["history_extra"] = '<input name="LNALTNO" value="99-0001" form="tr_mma">'
    elif attack == "bad_header":
        state["response"]["Header"] = "FAIL"
    elif attack == "bad_message":
        state["response"]["Message"] = "系統錯誤"
    elif attack == "bad_metadata":
        state["response"]["HeadInfo"] = []
    elif attack == "dialog":
        crawler._shared_dialog_blocked = True
    else:
        state["query_after"] = {
            "pager": "document.body.insertAdjacentHTML('beforeend','<button class=\"pager\">下一頁</button>');",
            "modal": "document.body.insertAdjacentHTML('beforeend','<div role=\"dialog\">未完成</div>');",
            "changed_headers": "document.querySelector('#tbDetails th').textContent='其他日期';",
            "hidden_rows": "document.querySelector('#tbodyDetails tr').style.display='none';",
            "empty_visible": "document.querySelector('#tbodyNoDetail').style.display='';",
            "changed_dates": "document.querySelector('#StartDate').value='20260701';",
        }[attack]
    with pytest.raises(RuntimeError, match="^sinopac-loan-repayments$"):
        crawler._collect_loan_repayments(page, collector, ACCOUNT, RECORD)


def test_native_default_repayment_click_to_durable_fact(native_page, tmp_path, monkeypatch):
    from backend.core import bank_pg, store as store_mod
    page, collector, state = native_page
    crawler = SinopacCrawler.__new__(SinopacCrawler)
    repayment = crawler._collect_loan_repayments(page, collector, ACCOUNT, RECORD)
    assert repayment == _repayment()
    assert state["query_form"]["LNMAINACNO"] == [ACCOUNT["AcctValue"]]
    assert state["query_form"]["LNALTNO"] == [RECORD["Sub1_Sub2"]]
    assert [request for request in state["requests"] if request[1] == ENDPOINT] == [("POST", ENDPOINT)]
    monkeypatch.setattr(bank_pg, "DB_BACKEND", "sqlite")
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path)
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    try:
        delta = _persist_sinopac({"loan": {"fetch_ok": True, "details": [{
            "account": ACCOUNT["AcctValue"], "records": [deepcopy(RECORD)], "repayments": [repayment],
        }]}}, store)
        assert delta["loan_repayments"] == 1
        assert store.conn.execute("SELECT principal FROM loan_repayments").fetchone()[0] == "10000"
    finally:
        store.close()
