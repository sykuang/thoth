"""Code-owned loan diagnostics; synthetic pages only, no browser or bank I/O."""
from copy import deepcopy
from datetime import date
import json
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import Mock

import pytest

from backend.banks.sinopac import SinopacCrawler
from backend.core.base import ResponseCollector, _safe_collect_guard
from tests.test_bank_login_lifecycle import _run
from tests.test_sinopac_loan import FIXTURE, _LoanPage, _repayment


# One label per existing rejection predicate (compound predicates stay intact).
LOAN_GUARDS = (
    "sinopac-loan-inventory-envelope",
    "sinopac-loan-inventory-row",
    "sinopac-loan-inventory-identity",
    "sinopac-loan-query-control",
    "sinopac-loan-response-missing",
    "sinopac-loan-response-http",
    "sinopac-loan-response-envelope",
    "sinopac-loan-response-records",
    "sinopac-loan-restore-page",
    "sinopac-loan-restore-account",
)


@pytest.mark.parametrize("guard", LOAN_GUARDS)
def test_loan_rejections_reach_safe_sink(monkeypatch, tmp_path, capsys, guard):
    crawler = SinopacCrawler.__new__(SinopacCrawler)
    crawler.name = "sinopac"
    collector = ResponseCollector("sinopac.com")
    page = _LoanPage(collector)
    goto, evaluate, go_back = page.goto, page.evaluate, page.go_back
    repayment = Mock(return_value=_repayment())
    monkeypatch.setattr(crawler, "_collect_loan_repayments", repayment)

    def inventory(url, **kwargs):
        goto(url, **kwargs)
        if guard == LOAN_GUARDS[0]:
            collector.hits[-1].resp_json = None
        elif guard == LOAN_GUARDS[1]:
            collector.hits[-1].resp_json[0]["SubInfo"] = [None]
        elif guard == LOAN_GUARDS[2]:
            collector.hits[-1].resp_json[0]["SubInfo"][0]["AcctValue"] = ""

    def query(script, args):
        result = evaluate(script, args)
        hit = collector.hits[-1]
        if guard == LOAN_GUARDS[3]:
            return False
        if guard == LOAN_GUARDS[4]:
            hit.req_body = "AcctValue=PRIVATE-CUSTOMER"
        elif guard == LOAN_GUARDS[5]:
            hit.status = 500
        elif guard == LOAN_GUARDS[6]:
            hit.resp_json = [{}]
        elif guard == LOAN_GUARDS[7]:
            hit.resp_json = [{"SubInfo": [], "Message": "PRIVATE-CUSTOMER"}]
        return result

    def restore(**kwargs):
        go_back(**kwargs)
        if guard == LOAN_GUARDS[8]:
            page.url = "https://example.invalid/PRIVATE-CUSTOMER"
        elif guard == LOAN_GUARDS[9]:
            page.query_args[-1]["account"] = "PRIVATE-CUSTOMER"

    page.goto = inventory
    monkeypatch.setattr(page, "evaluate", query)
    page.go_back = Mock(side_effect=restore)
    page.wait_for_timeout = Mock()
    monkeypatch.setattr(crawler, "collect", lambda *_: crawler._collect_loans(page, collector))
    monkeypatch.setattr(crawler, "_shared_login", lambda _: True)
    monkeypatch.setattr(crawler, "_credential_origin_allowed", lambda _: True)
    monkeypatch.setattr(crawler, "_build_fetch_kwargs", lambda: {})
    logout = Mock(return_value=True)
    monkeypatch.setattr(crawler, "logout", logout)

    result, _ = _run(monkeypatch, tmp_path, crawler, None)
    assert result["error"] == f"collect_failed: RuntimeError: code=collect_history: guard={guard}"
    stderr = capsys.readouterr().err
    assert f"guard={guard}" in stderr
    assert "PRIVATE-CUSTOMER" not in repr(result) + stderr
    assert "Traceback" not in stderr
    assert page.urls == ["https://mma.sinopac.com/mma/bank/easy_index_loan/mma_detail.aspx"]
    expected_queries = int(guard not in LOAN_GUARDS[:3])
    assert len(page.query_args) == expected_queries
    assert repayment.call_count == page.go_back.call_count == int(guard in LOAN_GUARDS[8:])
    assert page.wait_for_timeout.call_args_list[0].args == (8000,)
    logout.assert_called_once()


# Includes the nested ensure_page callback and both date-validator call sites.
REPAYMENT_GUARDS = (
    "page-state", "account-control", "link-cardinality", "form-control",
    "start-date", "end-date", "text-type", "form-binding", "query-control",
    "query-label", "response-cardinality", "request-binding", "response-type",
    "response-body", "response-envelope", "response-metadata", "response-records",
    "result-state", "result-rows",
)


@pytest.fixture
def repayment_case(monkeypatch):
    from backend.banks.sinopac import LOAN_REPAYMENT_PATH

    monkeypatch.setattr("backend.banks.sinopac._taipei_today", lambda: date(2026, 9, 7))
    monkeypatch.setattr("backend.core.persist.sinopac._today", lambda: date(2026, 9, 7))
    crawler = SinopacCrawler.__new__(SinopacCrawler)
    crawler.name = "sinopac"
    account = deepcopy(FIXTURE["account_response"][0]["SubInfo"][0])
    record = deepcopy(FIXTURE["info_response"][0]["SubInfo"][0])
    fields = {key: account[key] for key in ("AcctValue", "AcctValueFormat")}
    fields.update(LNMAINACNO=account["AcctValue"], LNALTNO=record["Sub1_Sub2"],
                  CURRENCY=record["Currency"], TextType="", StartDate="20260801", EndDate="20260907")
    fields.update({key: record[key] for key in ("Sub1_Sub2", "LoanAmt", "LoanBalance", "LoanKind", "PayName")})
    body = {"Header": "SUCCESS", "Message": "", "SubInfo": _repayment()["records"],
            "HeadInfo": [dict.fromkeys(("HeadText", "HeadAlign", "DataAlign", "MainShow", "DetailShow",
                                       "FieldKey", "OrderIndex", "FieldWidth"), "") for _ in range(3)]}
    form = list(map(list, fields.items()))
    dom = {"unique": True, "visible": True, "empty": False, "pager": False,
           "headers": ["繳款日", "應繳日", "攤還本金", "繳息金額", "本金餘額", "違約金金額", "繳款金額", "交易狀態"],
           "form": deepcopy(form),
           "rows": [[row[f"DataValue{i}"] for i in (2, 1, 11, 4, 5, 6, 3, 10)] for row in body["SubInfo"]]}
    state = {"form": form, "payload": [body], "dom": dom}
    page = Mock(url="https://mma.sinopac.com/mma/bank/easy_index_loan/mma_loandetail.aspx", frames=[])
    controls = {selector: Mock() for selector in ("#AcctValue", "#AcctValueFormat", "#tr_mma", "#StartDate", "#EndDate", "#btnQuery", "#tbDetails a[onclick]")}
    for control in controls.values():
        control.count.return_value = 1
        control.is_visible.return_value = control.is_enabled.return_value = True
    for key in ("AcctValue", "AcctValueFormat"):
        controls["#" + key].input_value.return_value = account[key]
    link = controls["#tbDetails a[onclick]"].nth.return_value
    args = ["detail", *(record[key] for key in ("Sub1_Sub2", "Currency", "LoanAmt", "LoanBalance", "LoanKind", "PayName"))]
    link.get_attribute.return_value = "forQuery(" + ",".join("'" + arg + "'" for arg in args) + ");"
    link.inner_text.return_value = "繳款明細"
    link.is_visible.return_value = True
    missing = Mock()
    missing.count.return_value = 0
    page.locator.side_effect = lambda selector: controls.get(selector, missing)
    page.wait_for_url.side_effect = lambda url, **_: setattr(page, "url", url)
    page.evaluate.side_effect = lambda script: state["dom"] if "const rows" in script else state["form"]
    response = SimpleNamespace(url="https://mma.sinopac.com" + LOAN_REPAYMENT_PATH,
                               request=SimpleNamespace(post_data=urlencode(fields)),
                               headers={"content-type": "application/json"})
    callbacks = {}
    page.on.side_effect = lambda event, handler: callbacks.update({event: handler})
    button = controls["#btnQuery"]
    button.inner_text.return_value = "查詢"
    button.click.side_effect = lambda **_: callbacks["response"](response)
    observer = Mock(bad=False)
    observer.read.side_effect = lambda *_: json.dumps(state["payload"]).encode()
    monkeypatch.setattr("backend.banks.sinopac._LoanRepaymentBodyObserver", Mock(return_value=observer))
    collector = Mock()
    return SimpleNamespace(crawler=crawler, account=account, record=record, page=page,
                           controls=controls, link=link, button=button, state=state,
                           response=response, observer=observer, collector=collector)


@pytest.mark.parametrize("suffix", (*REPAYMENT_GUARDS, "unexpected"))
def test_repayment_rejections_retain_deep_guard_through_safe_sink(
    repayment_case, monkeypatch, tmp_path, capsys, suffix,
):
    c = repayment_case
    if suffix == "page-state":
        c.page.url = "https://example.invalid/PRIVATE-CUSTOMER"
    elif suffix == "account-control":
        c.controls["#AcctValue"].input_value.return_value = "PRIVATE-CUSTOMER"
    elif suffix == "link-cardinality":
        c.controls["#tbDetails a[onclick]"].count.return_value = 0
    elif suffix == "form-control":
        c.controls["#tr_mma"].count.return_value = 0
    elif suffix in {"start-date", "end-date", "text-type", "form-binding"}:
        field = {"start-date": "StartDate", "end-date": "EndDate", "text-type": "TextType", "form-binding": "LNALTNO"}[suffix]
        for item in c.state["form"]:
            if item[0] == field:
                item[1] = None if suffix == "text-type" else "PRIVATE-CUSTOMER"
    elif suffix == "query-control":
        c.controls["#StartDate"].is_enabled.return_value = False
    elif suffix == "query-label":
        c.button.inner_text.return_value = "PRIVATE-CUSTOMER"
    elif suffix == "response-cardinality":
        c.button.click.side_effect = None
    elif suffix == "request-binding":
        c.response.request.post_data = "PRIVATE-CUSTOMER"
    elif suffix == "response-type":
        c.response.headers["content-type"] = "text/html"
    elif suffix == "response-body":
        c.observer.read.side_effect = None
        c.observer.read.return_value = None
    elif suffix == "response-envelope":
        c.state["payload"] = {}
    elif suffix == "response-metadata":
        c.state["payload"][0]["Message"] = "PRIVATE-CUSTOMER"
    elif suffix == "response-records":
        c.state["payload"][0]["SubInfo"] = []
    elif suffix == "result-state":
        c.state["dom"]["pager"] = True
    elif suffix == "result-rows":
        c.state["dom"]["rows"] = []
    else:
        c.link.click.side_effect = RuntimeError("PRIVATE-CUSTOMER")
    guard = "sinopac-loan-repayments" + ("-" + suffix if suffix != "unexpected" else "")
    caught = []

    def collect(*_):
        try:
            c.crawler._collect_loan_repayments(c.page, c.collector, c.account, c.record)
        except RuntimeError as exc:
            caught.append(exc)
            raise RuntimeError("PRIVATE-WRAPPER\nFORGED_LOG") from None

    monkeypatch.setattr(c.crawler, "collect", collect)
    monkeypatch.setattr(c.crawler, "_shared_login", lambda _: True)
    monkeypatch.setattr(c.crawler, "_credential_origin_allowed", lambda _: True)
    monkeypatch.setattr(c.crawler, "_build_fetch_kwargs", lambda: {})
    monkeypatch.setattr(c.crawler, "logout", Mock(return_value=True))
    result, _ = _run(monkeypatch, tmp_path, c.crawler, None)
    stderr = capsys.readouterr().err
    assert result["error"].endswith(f": guard={guard}")
    assert f"guard={guard}" in stderr
    assert caught[0].__suppress_context__ is True
    assert _safe_collect_guard(caught[0], SinopacCrawler.SAFE_COLLECT_GUARDS) == guard
    assert all(marker not in repr(result) + stderr for marker in ("PRIVATE", "FORGED_LOG", "Traceback"))
    assert c.link.click.call_count == int(suffix not in REPAYMENT_GUARDS[:3])
    queried = suffix in REPAYMENT_GUARDS[10:]
    assert c.button.click.call_count == int(queried)
    assert c.collector.detach.call_count == c.observer.close.call_count == c.collector.attach.call_count == int(queried)
    assert c.page.remove_listener.call_count == int(queried)
    assert c.page.go_back.call_count == 0


def test_repayment_success_keeps_native_result_and_single_query(repayment_case):
    c = repayment_case
    assert c.crawler._collect_loan_repayments(c.page, c.collector, c.account, c.record) == _repayment()
    c.link.click.assert_called_once_with(timeout=8_000)
    c.button.click.assert_called_once_with(timeout=8_000)
    c.observer.read.assert_called_once()
    c.observer.close.assert_called_once()
    c.collector.detach.assert_called_once_with(c.page)
    c.collector.attach.assert_called_once_with(c.page)


def test_loan_guard_allowlist_is_exact_bounded_and_rejects_customer_text():
    from backend.core.base import _class_collect_guard_allowlist

    crawler = SinopacCrawler.__new__(SinopacCrawler)
    allowlist = _class_collect_guard_allowlist(crawler)
    expected = set(LOAN_GUARDS) | {"sinopac-loan-repayments"} | {
        "sinopac-loan-repayments-" + suffix for suffix in REPAYMENT_GUARDS
    }
    assert {guard for guard in allowlist if guard.startswith("sinopac-loan-")} == expected
    assert type(allowlist) is frozenset and len(allowlist) <= 128
    assert all(type(guard) is str and len(guard) <= 128 for guard in allowlist)
    for message in ("PRIVATE-CUSTOMER", "sinopac-loan-repayments-PRIVATE", "sinopac-loan-response-http: PRIVATE"):
        assert _safe_collect_guard(RuntimeError(message), allowlist) is None
