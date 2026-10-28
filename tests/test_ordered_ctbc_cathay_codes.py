"""Offline leaf diagnostics: synthetic inputs, no credentials or browser network."""
import ast
import inspect
from copy import deepcopy
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

import backend.banks.cathay as cathay_module
import backend.banks.ctbc as ctbc_module
from backend.banks.cathay import CathayCrawler, CathayLoginError
from backend.banks.ctbc import CtbcCrawler, CtbcLoginError, _CTBC_EBMW_PATH
from backend.core.base import ApiHit, ResponseCollector
from backend.core.error_codes import exception_error_id


@pytest.mark.parametrize("module", [ctbc_module, cathay_module])
def test_all_explicit_terminal_guard_sites_are_tagged(module):
    tree = ast.parse(inspect.getsource(module))
    unmapped = [node.lineno for node in ast.walk(tree)
                if isinstance(node, ast.Raise) and node.exc is not None
                and not (isinstance(node.exc, ast.Call) and isinstance(node.exc.func, ast.Name)
                         and node.exc.func.id == "with_error_code")]
    assert unmapped == []


@pytest.mark.parametrize("module", [ctbc_module, cathay_module])
def test_independent_rejection_or_predicates_are_split(module):
    tree = ast.parse(inspect.getsource(module))
    unsplit = [node.lineno for node in ast.walk(tree)
               if isinstance(node, ast.If) and len(node.body) == 1
               and isinstance(node.body[0], ast.Raise)
               and isinstance(node.test, ast.BoolOp) and isinstance(node.test.op, ast.Or)]
    assert unsplit == []


class InjectedFailure(RuntimeError):
    pass


@pytest.mark.parametrize("bank,method,swallowed", [
    ("ctbc", "prepare_login_page", False), ("cathay", "prepare_login_page", False),
    ("ctbc", "_logged_in", True), ("cathay", "_logged_in", True),
    ("ctbc", "_goto_credit_card", True), ("ctbc", "_goto_twd_deposit", True),
    ("ctbc", "logout", False), ("cathay", "_collect_card_details", True),
])
def test_actual_native_actions_keep_exception_and_best_effort_policy(bank, method, swallowed):
    crawler_cls = CtbcCrawler if bank == "ctbc" else CathayCrawler
    crawler = object.__new__(crawler_cls)
    failure = InjectedFailure("synthetic native failure")
    page = SimpleNamespace(url=("https://www.ctbcbank.com/twrbc/twrbc-home/qu000/010" if bank == "ctbc"
                                else "https://www.cathaybk.com.tw/OnlineBanking/Home/Asset"))
    def fail(*args, **kwargs):
        raise failure
    page.wait_for_timeout = page.evaluate = page.locator = page.goto = fail
    if swallowed:
        result = getattr(crawler, method)(page)
        if method == "_logged_in":
            assert result is False
    else:
        with pytest.raises(InjectedFailure) as caught:
            getattr(crawler, method)(page)
        assert caught.value is failure
    assert exception_error_id(failure, bank=bank) is not None



class LoginPage:
    def __init__(self, fail_at):
        self.fail_at = fail_at
        self.events = []
        self.error = InjectedFailure("synthetic native failure")
        self.keyboard = self
        self.value = ""

    def __getattr__(self, operation):
        def call(*args, **kwargs):
            self.events.append(operation)
            if len(self.events) == self.fail_at:
                raise self.error
            if operation == "locator":
                return self
            if operation == "count":
                return 1
            if operation == "nth":
                return self
            if operation in ("is_visible", "is_enabled"):
                return True
            if operation == "type":
                self.value = args[0]
            if operation == "input_value":
                return self.value
        return call


@pytest.mark.parametrize("crawler_cls,error_cls,bank", [
    (CtbcCrawler, CtbcLoginError, "ctbc"),
    (CathayCrawler, CathayLoginError, "cathay"),
])
def test_credential_native_failures_have_distinct_field_and_operation_codes(crawler_cls, error_cls, bank):
    crawler = object.__new__(crawler_cls)
    crawler.creds = SimpleNamespace(national_id="synthetic", user_code="synthetic", password="synthetic",
                                   cust_id="synthetic", user_id="synthetic")
    ids = []
    # Wait, then 11 native operations per field; stop before submitting anything.
    for fail_at in range(1, 35):
        page = LoginPage(fail_at)
        with pytest.raises(error_cls) as caught:
            crawler.submit_credentials_once(page)
        assert len(page.events) == fail_at
        assert page.error is caught.value.__context__
        code = exception_error_id(caught.value, bank=bank)
        assert code is not None, (bank, fail_at, page.events)
        ids.append(code)
    assert len(set(ids)) == len(ids), ids


@pytest.mark.parametrize("method,arg", [
    ("_norm_card_txn", {"consumeCurrency": 1}),
    ("_parse_consume", {"consume": [{"tradeData": [None]}]}),
    ("_parse_bill_detail", {"twdBillDetailInfo": [None]}),
    ("_normalize_balance_row", {"snapshotDate": object()}),
])
def test_actual_parser_helper_failures_are_tagged(method, arg):
    if method == "_normalize_balance_row":
        class BadText:
            def __str__(self):
                raise InjectedFailure("synthetic conversion failure")
        arg = {"snapshotDate": BadText()}
    crawler = object.__new__(CathayCrawler)
    with pytest.raises((AttributeError, TypeError, InjectedFailure)) as caught:
        getattr(crawler, method)(arg)
    assert exception_error_id(caught.value, bank="cathay") is not None


def test_feature_navigation_actions_are_source_distinct(monkeypatch):
    seen = []
    for key, path in cathay_module.FEATURE_PAGES.items():
        monkeypatch.setattr(cathay_module, "FEATURE_PAGES", {key: path})
        crawler = object.__new__(CathayCrawler)
        navigation_error = InjectedFailure("synthetic navigation failure")
        wait_error = InjectedFailure("synthetic wait failure")
        def goto(*args, **kwargs):
            raise navigation_error
        def wait(*args, **kwargs):
            raise wait_error
        with pytest.raises(InjectedFailure) as caught:
            crawler.collect(SimpleNamespace(goto=goto, wait_for_timeout=wait), ResponseCollector())
        assert caught.value is wait_error
        seen.extend([exception_error_id(navigation_error, bank="cathay"),
                     exception_error_id(wait_error, bank="cathay")])
    assert all(seen)
    assert len(set(seen)) == len(seen)


def test_card_navigation_actions_distinguish_each_detail_kind():
    errors = []
    def goto(*args, **kwargs):
        failure = InjectedFailure("synthetic card navigation failure")
        errors.append(failure)
        raise failure
    object.__new__(CathayCrawler)._collect_card_details(SimpleNamespace(goto=goto))
    codes = [exception_error_id(error, bank="cathay") for error in errors]
    assert len(codes) == 3
    assert len(set(codes)) == 3


class NativeForm:
    """One synthetic native query; no actual browser or bank requests."""
    url = "https://www.ctbcbank.com/twrbc/twrbc-deposit/qu002/010"
    def __init__(self, mutate=lambda hit: None, fail_at=None):
        self.collector = ResponseCollector()
        self.main_frame = object()
        self.keyboard = self
        self.active = None
        self.events = []
        self.fail_at = fail_at
        self.failure = InjectedFailure("synthetic browser failure")
        self.mutate = mutate
        self.values = {}

    def event(self, operation):
        self.events.append(operation)
        if len(self.events) == self.fail_at:
            raise self.failure

    def locator(self, selector):
        self.event("locator:" + selector)
        name = ("start" if selector.startswith("input") and "startDt" in selector else
                "end" if selector.startswith("input") else "panel")
        return self.Node(self, name)

    def wait_for_timeout(self, ms):
        self.event("wait")

    def press(self, key):
        self.event("press")

    def type(self, value, **kwargs):
        self.event("type")
        self.values[self.active] = value

    class Node:
        def __init__(self, page, name):
            self.page, self.name = page, name
        def count(self):
            self.page.event("count:" + self.name)
            return 1
        def nth(self, index):
            self.page.event("nth:" + self.name)
            return self
        def is_visible(self):
            self.page.event("visible:" + self.name)
            return True
        def is_enabled(self):
            self.page.event("enabled:" + self.name)
            return True
        def locator(self, selector):
            self.page.event("locator:action")
            return type(self)(self.page, "action")
        def evaluate(self, script):
            self.page.event("evaluate:action")
            return False
        def inner_text(self):
            self.page.event("text:action")
            return "搜尋"
        def input_value(self):
            self.page.event("value:" + self.name)
            return self.page.values[self.name]
        def click(self, **kwargs):
            self.page.event("click:" + self.name)
            self.page.active = self.name
            if self.name == "action":
                url = f"https://www.ctbcbank.com{_CTBC_EBMW_PATH}"
                hit = ApiHit(url=url, raw_url=url + "?IIhfvu=synthetic", method="POST", status=200,
                             content_type="application/json", main_frame_request=True,
                             request_frame=self.page.main_frame, request_frame_url=self.page.url,
                             request_sequence=1,
                             req_body={"resource": "/twrbc-deposit/qu002/011", "rqData": {
                                 "accountId": "synthetic", "startDate": "20260801",
                                 "endDate": "20260831", "type": "custom"}},
                             resp_json={"code": "0000", "rsData": {
                                 "dataTime": "synthetic", "detailList": [], "dtSort": "", "nextKey": ""}})
                self.page.mutate(hit)
                self.page.collector.hits.append(hit)


def native_query(page):
    return CtbcCrawler._fetch_native_history_window(
        page, page.collector, "synthetic", date(2026, 8, 1), date(2026, 8, 31))


def test_native_form_all_executed_operations_preserve_original_exception():
    good = NativeForm()
    assert native_query(good) == []
    seen = []
    for index in range(1, len(good.events) + 1):
        page = NativeForm(fail_at=index)
        with pytest.raises(InjectedFailure) as caught:
            native_query(page)
        assert caught.value is page.failure
        assert page.events == good.events[:index]
        assert exception_error_id(caught.value, bank="ctbc") is not None
        seen.append(exception_error_id(caught.value, bank="ctbc"))
    assert len(set(seen)) == len(seen)


def test_native_form_raw_url_and_envelope_rejections_are_distinct():
    mutations = [
        lambda hit: setattr(hit, "raw_url", hit.raw_url.replace("https:", "http:")),
        lambda hit: setattr(hit, "raw_url", hit.raw_url + "&extra=synthetic"),
        lambda hit: setattr(hit, "raw_url", hit.raw_url + "#synthetic"),
        lambda hit: setattr(hit, "raw_url", hit.url),
        lambda hit: hit.req_body.update(extra="synthetic"),
        lambda hit: hit.req_body.update(rqData=[]),
    ]
    seen = []
    for mutate in mutations:
        with pytest.raises(RuntimeError, match="ctbc-twd-history-fetch") as caught:
            native_query(NativeForm(mutate=mutate))
        seen.append(exception_error_id(caught.value, bank="ctbc"))
    assert all(seen)
    assert len(set(seen)) == len(seen)


def test_skipped_history_rows_keep_distinct_closed_leaf_ids(monkeypatch):
    from tests.test_ctbc_history_sync import _collector, _FetchPage
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    crawler = object.__new__(CtbcCrawler)
    crawler.transaction_cursors = {}
    collector = _collector(["synthetic"])
    deposit, identities = crawler._validated_twd_inventory(collector)
    seen = []
    for row in [None, {}, {"actDtTm": "invalid"},
                {"actDtTm": "2026-03-99-00.00.00"},
                {"actDtTm": "2026-03-01-00.00.00", "dbAmt": True}]:
        monkeypatch.setattr(crawler, "_fetch_qu002_011", lambda *args: [row])
        with pytest.raises(RuntimeError, match="ctbc-twd-history-row") as caught:
            crawler._collect_twd_deposit_history(_FetchPage(), collector, deposit,
                                                expected_identities=identities, as_of=date(2026, 8, 30))
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        assert caught.value.__suppress_context__ is False
        seen.append(exception_error_id(caught.value, bank="ctbc"))
    assert all(seen)
    assert len(set(seen)) == len(seen)


@pytest.mark.parametrize("bank,target", [("ctbc", "make_card_bill_fact"),
                                        ("ctbc", "publish_card_bill_facts"),
                                        ("ctbc", "BankCollectResult"),
                                        ("cathay", "BankCollectResult")])
def test_actual_publication_boundaries_keep_original_failure(monkeypatch, bank, target):
    module = ctbc_module if bank == "ctbc" else cathay_module
    crawler = object.__new__(CtbcCrawler if bank == "ctbc" else CathayCrawler)
    failure = InjectedFailure("synthetic publication failure")
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(module, target, fail)
    history = {"accounts": [], "coverage": {}}
    page = SimpleNamespace(wait_for_timeout=lambda ms: None, url="https://synthetic.invalid/",
                           locator=lambda selector: SimpleNamespace(first=SimpleNamespace(count=lambda: 0)))
    if bank == "ctbc":
        monkeypatch.setattr(crawler, "_latest_rsdata", lambda *args: {})
        monkeypatch.setattr(crawler, "_goto_twd_deposit", lambda *args: None)
        monkeypatch.setattr(crawler, "_validated_twd_inventory", lambda *args, **kwargs: ({}, set()))
        monkeypatch.setattr(crawler, "_collect_twd_deposit_history", lambda *args, **kwargs: history)
        monkeypatch.setattr(crawler, "_goto_credit_card", lambda *args, **kwargs: {"clicked": None})
    else:
        monkeypatch.setattr(module, "FEATURE_PAGES", {"twd_txn": "/synthetic"})
        page.goto = lambda *args, **kwargs: None
        monkeypatch.setattr(crawler, "_collect_twd_history", lambda *args, **kwargs: history)
        monkeypatch.setattr(crawler, "_twd_account_inventory", lambda *args, **kwargs: set())
        monkeypatch.setattr(crawler, "_collect_card_details", lambda *args: None)
        monkeypatch.setattr(crawler, "_parse", lambda *args: {})
    with pytest.raises(InjectedFailure) as caught:
        crawler.collect(page, ResponseCollector())
    assert caught.value is failure
    assert exception_error_id(caught.value, bank=bank) is not None


@pytest.mark.parametrize("module", [ctbc_module, cathay_module])
def test_native_calls_have_operation_level_tags_and_registered_literals(module):
    from backend.core.error_codes import code_details
    native = {"locator", "count", "nth", "get_attribute", "is_visible", "is_enabled", "inner_text",
              "click", "wait_for_timeout", "wait_for_selector", "press", "type", "input_value",
              "evaluate", "goto", "hover"}
    tree = ast.parse(inspect.getsource(module))
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith(module.__name__.rsplit(".", 1)[-1].upper() + "-"):
            assert code_details(node.value, bank=module.__name__.rsplit(".", 1)[-1]) is not None, node.value
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in native):
            continue
        ancestors = []
        current = node
        while current in parents:
            current = parents[current]
            ancestors.append(current)
        assert any((isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name)
                    and parent.func.id == "_native_step") or
                   (isinstance(parent, ast.With) and any(isinstance(item.context_expr, ast.Call)
                    and isinstance(item.context_expr.func, ast.Name)
                    and item.context_expr.func.id == "error_step" for item in parent.items))
                   for parent in ancestors), (module.__name__, node.lineno)


def test_inventory_leaf_reasons():
    valid: dict[str, Any] = dict(url=f"https://www.ctbcbank.com{_CTBC_EBMW_PATH}", method="POST", status=200,
                 req_body={"resource": "/twrbc-deposit/qu001/010"},
                 resp_json={"code": "0000", "rsData": {"twdAcctSummaryResponse": {
                     "demDepBalSummaryResponse": {"infoList": [{"accountId": "synthetic"}]}}}})
    observed = []
    cases = [None, {**valid, "method": "GET"}, {**valid, "status": True},
             {**valid, "status": 503}, {**valid, "url": "https://synthetic.invalid/"},
             {**valid, "resp_json": []}, {**valid, "resp_json": {"code": "bad"}},
             {**valid, "resp_json": {"code": "0000"}},
             {**valid, "resp_json": {"code": "0000", "rsData": {}}}]
    for rows in ({}, [None], [{"accountId": None}], [{"accountId": ""}],
                 [{"accountId": " synthetic"}], [{"accountId": "synthetic"}] * 2):
        value = deepcopy(valid)
        value["resp_json"]["rsData"]["twdAcctSummaryResponse"]["demDepBalSummaryResponse"]["infoList"] = rows
        cases.append(value)
    for value in cases:
        collector = ResponseCollector()
        if value is not None:
            collector.hits.append(ApiHit(**deepcopy(value)))
        with pytest.raises(RuntimeError, match="ctbc-twd-history-inventory") as caught:
            CtbcCrawler._validated_twd_inventory(collector)
        observed.append(exception_error_id(caught.value, bank="ctbc"))
    assert all(observed), observed
    assert len(set(observed)) == len(cases)


_EXPECTED_FOLLOWUP_LABELS = {'CTBC-05-400-001': ('送出已驗證月份請求並讀取回應', '瀏覽器原生操作失敗'), 'CATHAY-05-500-001': ('開啟台幣交易查詢', '瀏覽器原生操作失敗'), 'CATHAY-03-011-001': ('檢查登入正向訊號', '瀏覽器原生操作失敗'),
 'CATHAY-03-988-001': ('登入送出後規則比對', '送出後沒有規則或成功狀態可確認'),
 'CATHAY-03-988-002': ('登入送出後規則比對', '收斂階段沒有規則或成功狀態可確認'),
 'CATHAY-04-020-001': ('驗證台幣帳戶識別值', '帳戶識別值不是字串'),
 'CATHAY-04-500-001': ('開啟資產總覽', '瀏覽器原生操作失敗'),
 'CATHAY-05-010-005': ('建立原生帳戶查詢模板', '帳號群組缺失'),
 'CATHAY-05-010-006': ('建立原生帳戶查詢模板', '帳號輸入欄位缺失'),
 'CATHAY-05-010-007': ('建立原生帳戶查詢模板', '期間群組缺失'),
 'CATHAY-05-010-008': ('建立原生帳戶查詢模板', '期間輸入欄位缺失'),
 'CATHAY-05-022-001': ('驗證歷史模板帳戶識別值', '帳戶識別值不是字串'),
 'CATHAY-05-030-012': ('驗證帳戶歷史查詢回應', '回應起日不符'),
 'CATHAY-05-030-013': ('驗證帳戶歷史查詢回應', '回應迄日不符'),
 'CATHAY-05-040-008': ('收集完整台幣歷史', '回應不是物件'),
 'CATHAY-05-040-009': ('解碼台幣歷史查詢回應', 'JSON解碼失敗'),
 'CATHAY-05-800-001': ('驗證台幣交易支出金額', '金額不可為布林值'),
 'CATHAY-05-801-001': ('驗證台幣交易收入金額', '金額不可為布林值'),
 'CATHAY-05-802-001': ('驗證台幣交易餘額', '金額不可為布林值'),
 'CATHAY-05-810-001': ('驗證台幣交易交易摘要', '文字欄位型別不符'),
 'CATHAY-05-811-001': ('驗證台幣交易對方銀行', '文字欄位型別不符'),
 'CATHAY-05-812-001': ('驗證台幣交易對方帳號', '文字欄位型別不符'),
 'CATHAY-05-813-001': ('驗證台幣交易備註', '文字欄位型別不符'),
 'CATHAY-05-830-001': ('驗證歷史回應起日', '起日不是字串'),
 'CATHAY-05-830-002': ('驗證歷史回應起日', '起日格式不符'),
 'CATHAY-05-830-003': ('驗證歷史回應起日', '起日日曆日期無效'),
 'CATHAY-05-831-001': ('驗證歷史回應迄日', '迄日不是字串'),
 'CATHAY-05-831-002': ('驗證歷史回應迄日', '迄日格式不符'),
 'CATHAY-05-831-003': ('驗證歷史回應迄日', '迄日日曆日期無效'),
 'CATHAY-05-832-001': ('驗證歷史回應帳務日期', '帳務日期不是字串'),
 'CATHAY-05-832-002': ('驗證歷史回應帳務日期', '帳務日期格式不符'),
 'CATHAY-05-832-003': ('驗證歷史回應帳務日期', '帳務日期日曆日期無效'),
 'CATHAY-06-340-001': ('正規化已出帳信用卡交易', '資料讀取或轉換失敗'),
 'CATHAY-06-350-001': ('解析未出帳信用卡明細', '資料讀取或轉換失敗'),
 'CATHAY-06-350-003': ('正規化未出帳信用卡交易', '資料讀取或轉換失敗'),
 'CATHAY-06-360-001': ('解析即時消費信用卡明細', '資料讀取或轉換失敗'),
 'CATHAY-06-360-003': ('正規化即時消費信用卡交易', '資料讀取或轉換失敗'),
 'CATHAY-07-401-001': ('讀取帳戶清單', '資料讀取或轉換失敗'),
 'CATHAY-07-413-001': ('讀取信用卡額度', '資料讀取或轉換失敗'),
 'CATHAY-07-450-001': ('讀取外幣現值', '資料讀取或轉換失敗'),
 'CATHAY-07-451-001': ('讀取投資現值', '資料讀取或轉換失敗'),
 'CATHAY-07-452-001': ('讀取保險現值', '資料讀取或轉換失敗'),
 'CATHAY-07-453-001': ('讀取貸款現值', '資料讀取或轉換失敗'),
 'CTBC-03-011-001': ('檢查登入正向訊號', '瀏覽器原生操作失敗'),
 'CTBC-03-988-001': ('登入送出後規則比對', '送出後沒有規則或成功狀態可確認'),
 'CTBC-03-988-002': ('登入送出後規則比對', '收斂階段沒有規則或成功狀態可確認'),
 'CTBC-04-040-001': ('讀取首頁彙總成功資料', '資料讀取或轉換失敗'),
 'CTBC-04-100-001': ('開啟台幣存款總覽', '瀏覽器原生操作失敗'),
 'CTBC-05-020-020': ('收集完整台幣歷史', '查詢請求模板缺失'),
 'CTBC-05-040-020': ('重送已驗證月份查詢', '銀行回應不是物件'),
 'CTBC-05-040-034': ('解碼台幣歷史查詢回應', 'JSON解碼失敗'),
 'CTBC-05-071-001': ('選取歷史查詢模板', '原生模板狀態未成功'),
 'CTBC-05-071-002': ('選取歷史查詢模板', '原生模板資料不合法'),
 'CTBC-05-800-001': ('驗證台幣交易支出金額', '金額不可為布林值'),
 'CTBC-05-801-001': ('驗證台幣交易收入金額', '金額不可為布林值'),
 'CTBC-05-802-001': ('驗證台幣交易餘額', '金額不可為布林值'),
 'CTBC-05-810-001': ('驗證台幣交易摘要一', '文字欄位型別不符'),
 'CTBC-05-811-001': ('驗證台幣交易摘要二', '文字欄位型別不符'),
 'CTBC-05-812-001': ('驗證台幣交易對方銀行', '文字欄位型別不符'),
 'CTBC-05-813-001': ('驗證台幣交易對方帳號', '文字欄位型別不符'),
 'CTBC-05-814-001': ('驗證台幣交易備註代碼', '文字欄位型別不符'),
 'CTBC-05-820-001': ('驗證歷史回應總頁數證明', '總頁數不是整數'),
 'CTBC-05-821-001': ('驗證歷史回應頁數證明', '頁數不是整數'),
 'CTBC-05-822-001': ('驗證歷史回應筆數證明', '筆數不是整數'),
 'CTBC-05-823-001': ('驗證歷史回應總筆數證明', '總筆數不是整數'),
 'CTBC-05-824-001': ('驗證歷史回應下一頁', '回應帶有後續標記'),
 'CTBC-05-825-001': ('驗證歷史回應後續資料', '回應帶有後續標記'),
 'CTBC-05-826-001': ('驗證歷史回應更多資料', '回應帶有後續標記'),
 'CTBC-05-827-001': ('驗證歷史回應後續頁碼', '回應帶有後續標記'),
 'CTBC-05-828-001': ('驗證歷史回應後續游標', '回應帶有後續標記'),
 'CTBC-05-829-001': ('驗證歷史回應下一頁', '無後續頁證明不符'),
 'CTBC-05-833-001': ('驗證歷史回應後續資料', '無後續頁證明不符'),
 'CTBC-05-834-001': ('驗證歷史回應更多資料', '無後續頁證明不符'),
 'CATHAY-07-020-001': ('驗證台幣交易時間', '交易時間不是字串'),
 'CTBC-06-040-001': ('讀取信用卡功能成功資料', '資料讀取或轉換失敗')}

def sink(bank, error, code, chinese):
    crawler = object.__new__(CtbcCrawler if bank == 'ctbc' else CathayCrawler)
    crawler.name = bank
    from backend.core.base import _collect_failure_diagnostics, _format_collect_diagnostics
    result = _format_collect_diagnostics(_collect_failure_diagnostics(crawler, error))
    assert exception_error_id(error, bank=bank) == code
    assert chinese in str(result)
    action, reason = _EXPECTED_FOLLOWUP_LABELS[code]
    assert f"[{code}] {action}：{reason}" in result
    assert 'PRIVATE_SENTINEL' not in str(result)


@pytest.mark.parametrize('endpoint,content,code,label', [
    ('C_BILL_Q_RecentBillDetail', {'twdBillDetailInfo': [{'tradeData': [{'consumeCurrency': 1}]}]}, 'CATHAY-06-340-001', '已出帳'),
    ('C_BILL_Q_CardUnbilledConsume', {'consume': [{'tradeData': [{'amount': 1, 'consumeCurrency': 1}]}]}, 'CATHAY-06-350-003', '未出帳'),
    ('C_BILL_Q_CardCurrentConsume', {'consume': [{'tradeData': [{'amount': 1, 'consumeCurrency': 1}]}]}, 'CATHAY-06-360-003', '即時消費'),
])
def test_role_leaf_reaches_actual_sink(endpoint, content, code, label):
    col = ResponseCollector()
    col.hits.append(ApiHit(url='https://synthetic.invalid/' + endpoint, method='POST', status=200,
                           resp_json={'success': True, 'content': content}))
    with pytest.raises(AttributeError, match="'int' object has no attribute 'strip'") as caught:
        object.__new__(CathayCrawler)._parse(col)
    sink('cathay', caught.value, code, label)

@pytest.mark.parametrize('bank,field,code,chinese', [
    ('ctbc', 'dbAmt', 'CTBC-05-800-001', '支出金額'),
    ('ctbc', 'crAmt', 'CTBC-05-801-001', '收入金額'),
    ('ctbc', 'balanceAmt', 'CTBC-05-802-001', '餘額'),
    ('ctbc', 'memo1', 'CTBC-05-810-001', '摘要一'),
    ('ctbc', 'memo2', 'CTBC-05-811-001', '摘要二'),
    ('ctbc', 'bankId', 'CTBC-05-812-001', '對方銀行'),
    ('ctbc', 'trfAcct', 'CTBC-05-813-001', '對方帳號'),
    ('ctbc', 'memoCode', 'CTBC-05-814-001', '備註代碼'),
    ('cathay', 'txnDateTime', 'CATHAY-07-020-001', '交易時間'),
    ('cathay', 'expendAmt', 'CATHAY-05-800-001', '支出金額'),
    ('cathay', 'incomeAmt', 'CATHAY-05-801-001', '收入金額'),
    ('cathay', 'balance', 'CATHAY-05-802-001', '餘額'),
    ('cathay', 'description', 'CATHAY-05-810-001', '交易摘要'),
    ('cathay', 'expendBankId', 'CATHAY-05-811-001', '對方銀行'),
    ('cathay', 'expendAcctNo', 'CATHAY-05-812-001', '對方帳號'),
    ('cathay', 'memo', 'CATHAY-05-813-001', '備註'),
])
def test_history_field_failure_actual_sink(monkeypatch, bank, field, code, chinese):
    monkeypatch.setenv('BANK_CRAWLER_HISTORY_MODE', 'full')
    if bank == 'ctbc':
        from tests.test_ctbc_history_sync import _FetchPage, _collector
        crawler = object.__new__(CtbcCrawler)
        crawler.transaction_cursors = {}
        col = _collector(['synthetic'])
        deposit, identities = crawler._validated_twd_inventory(col)
        page = _FetchPage()
        original = page.evaluate
        def evaluate(script, payload):
            value = original(script, payload)
            value['json']['rsData']['detailList'][0][field] = True
            return value
        page.evaluate = evaluate
        call = lambda: crawler._collect_twd_deposit_history(page, col, deposit, expected_identities=identities, as_of=date(2026, 8, 30))
        message = 'ctbc-twd-history-row'
    else:
        from tests.test_cathay_history_sync import _FetchPage, _template
        crawler = object.__new__(CathayCrawler)
        crawler.transaction_cursors = {}
        col = ResponseCollector()
        monkeypatch.setattr(crawler, '_seed_twd_query_templates', lambda *args: [_template('synthetic')])
        page = _FetchPage()
        original = page.evaluate
        def evaluate(script, payload):
            value = original(script, payload)
            value['json']['content']['datas'][0]['details'][0][field] = True
            return value
        page.evaluate = evaluate
        call = lambda: crawler._collect_twd_history(page, col, as_of=date(2026, 8, 30))
        suffix = 'date' if field == 'txnDateTime' else 'amount' if field in ('expendAmt', 'incomeAmt', 'balance') else 'text'
        message = 'cathay-twd-history-transaction-' + suffix
    with pytest.raises(RuntimeError) as caught:
        call()
    assert str(caught.value) == message
    assert len(page.payloads) == 1
    sink(bank, caught.value, code, chinese)

class OfflineScriptPage:
    """Execute the exact evaluate script in Node; every browser primitive is local."""
    def __init__(self, fail_json):
        self.fail_json = fail_json
        self.events = []
        self.result = None

    def evaluate(self, script, payload):
        import json
        import subprocess
        harness = r"""
const fs = require('fs');
const {script, payload, fail} = JSON.parse(fs.readFileSync(0, 'utf8'));
const events = [];
global.location = {protocol: 'https:', hostname: 'www.ctbcbank.com', port: ''};
global.AbortController = class { constructor(){ events.push('controller'); this.signal = 'signal'; } abort(){events.push('abort');} };
global.setTimeout = (fn, ms) => {events.push(['timeout', ms]); return 7;};
global.clearTimeout = id => events.push(['clear', id]);
global.fetch = async (url, options) => {
  events.push(['fetch', url, options]);
  return {status:200, url, redirected:false, headers:{get:()=> 'application/json'},
    json:async()=>{events.push('json'); if(fail) throw new SyntaxError('PRIVATE_SENTINEL'); return null;}};
};
(async()=>{ const result = await eval('(' + script + ')')(payload);
process.stdout.write(JSON.stringify({result, events})); })().catch(e=>{console.error(e);process.exit(1);});
"""
        run = subprocess.run(['node', '-e', harness], input=json.dumps({'script':script, 'payload':payload, 'fail':self.fail_json}), text=True, capture_output=True, check=True)
        output = json.loads(run.stdout)
        self.events.append(output['events'])
        self.result = output['result']
        return self.result


@pytest.mark.parametrize('bank,fail,code', [
    ('ctbc', True, 'CTBC-05-040-034'), ('ctbc', False, 'CTBC-05-040-020'),
    ('cathay', True, 'CATHAY-05-040-009'), ('cathay', False, 'CATHAY-05-040-008'),
])
def test_actual_json_catch_producer_and_terminal(monkeypatch, bank, fail, code):
    page = OfflineScriptPage(fail)
    if bank == 'ctbc':
        from tests.test_ctbc_history_sync import _template
        hit = _template('synthetic')
        call = lambda: CtbcCrawler._fetch_qu002_011(page, hit.url, hit.req_body, 'synthetic', 'm0', 'Bearer synthetic-token')
        message = 'invalid-response'
    else:
        from tests.test_cathay_history_sync import _template
        crawler = object.__new__(CathayCrawler)
        crawler.transaction_cursors = {}
        monkeypatch.setattr(crawler, '_seed_twd_query_templates', lambda *args: [_template('synthetic')])
        call = lambda: crawler._collect_twd_history(page, ResponseCollector(), as_of=date(2026, 8, 30))
        message = 'cathay-twd-history-response'
    with pytest.raises(RuntimeError) as caught:
        call()
    assert str(caught.value) == message
    assert page.result['json'] is None
    assert len(page.events) == 1
    events = page.events[0]
    assert [e if isinstance(e, str) else e[0] for e in events] == ['controller', 'timeout', 'fetch', 'json', 'clear']
    assert events[1] == ['timeout', 30000]
    assert events[-1] == ['clear', 7]
    assert events[2][2]['redirect'] == 'error'
    assert events[2][2]['credentials'] == 'include'
    sink(bank, caught.value, code, 'JSON解碼失敗' if fail else '不是物件')

@pytest.mark.parametrize('endpoint,code,label', [
    ('G_CUST_Q_TransAccountList', 'CATHAY-07-401-001', '帳戶清單'),
    ('C_CardInfo_Q_CardQuota', 'CATHAY-07-413-001', '信用卡額度'),
])
def test_content_access_role_original_native_identity(endpoint, code, label):
    error = InjectedFailure('PRIVATE_SENTINEL')
    events = []
    class Hit:
        @property
        def resp_json(self):
            events.append('response')
            raise error
    col = SimpleNamespace(latest=lambda ep: (events.append(ep), Hit() if ep == endpoint else None)[1])
    with pytest.raises(InjectedFailure) as caught:
        object.__new__(CathayCrawler)._parse(col)
    assert caught.value is error
    assert events[-2:] == [endpoint, 'response']
    assert events.count('response') == 1
    sink('cathay', caught.value, code, label)


@pytest.mark.parametrize('where,code', [('inventory', 'CATHAY-04-020-001'), ('template', 'CATHAY-05-022-001')])
def test_identity_stage_actual_sink(where, code):
    if where == 'inventory':
        from backend.banks.cathay import _ACCOUNT_INVENTORY_PATH
        col = ResponseCollector()
        col.hits.append(ApiHit(url='https://www.cathaybk.com.tw'+_ACCOUNT_INVENTORY_PATH, method='POST', status=200,
            req_body={'content':{'queryType':'TWD'}}, resp_json={'success':True,'returnCode':'0000','content':{'datas':[{'currency':'TWD','accountNo':None}]}}))
        call = lambda: CathayCrawler._twd_account_inventory(col)
    else:
        from tests.test_cathay_history_sync import _template
        hit = _template('synthetic')
        hit.req_body['content']['queryFilters'][0]['accountNumber'] = None
        call = lambda: CathayCrawler._twd_template_account(hit)
    with pytest.raises(RuntimeError, match='cathay-twd-history-account-inventory') as caught:
        call()
    sink('cathay', caught.value, code, '帳戶識別值不是字串')


@pytest.mark.parametrize('field,code,label', [
    ('totalPages','CTBC-05-820-001','總頁數'), ('pageCount','CTBC-05-821-001','頁數'),
    ('count','CTBC-05-822-001','筆數'), ('totalCount','CTBC-05-823-001','總筆數'),
])
def test_replay_evidence_field_actual_sink(field, code, label):
    from tests.test_ctbc_history_sync import _template
    hit = _template('synthetic')
    data = hit.resp_json['rsData']
    if field == 'pageCount': del data['totalPages']
    if field == 'totalCount': del data['count']
    data[field] = True
    events = []
    def evaluate(script, payload):
        events.append('evaluate')
        return {'status':200,'url':hit.url,'redirected':False,'contentType':'application/json','json':hit.resp_json}
    with pytest.raises(RuntimeError) as caught:
        CtbcCrawler._fetch_qu002_011(SimpleNamespace(evaluate=evaluate),hit.url,hit.req_body,'synthetic','m0','Bearer synthetic-token')
    assert str(caught.value) == ('pagination-detected' if field in ('totalPages','pageCount') else 'count-mismatch')
    assert events == ['evaluate']
    sink('ctbc',caught.value,code,label)

@pytest.mark.parametrize('inventory,recover', [(False, False), (True, False), (False, True)])
def test_real_collect_swallowed_navigation_only_corresponding_terminal(monkeypatch, inventory, recover):
    from tests.test_ctbc_history_sync import _inventory_hit
    crawler = object.__new__(CtbcCrawler)
    crawler.name = 'ctbc'
    col = ResponseCollector()
    if inventory: col.hits.append(_inventory_hit([]))
    error = InjectedFailure('PRIVATE_SENTINEL')
    events = []
    def evaluate(script, *args):
        events.append('deposit-evaluate')
        raise error
    def wait(ms):
        events.append(('wait', ms))
        if recover and ms == 3000: col.hits.append(_inventory_hit([]))
    page = SimpleNamespace(evaluate=evaluate, wait_for_timeout=wait)
    if inventory or recover:
        unrelated = InjectedFailure('unrelated')
        def history(*args, **kwargs):
            events.append('history')
            raise unrelated
        monkeypatch.setattr(crawler, '_collect_twd_deposit_history', history)
        with pytest.raises(InjectedFailure) as caught: crawler.collect(page, col)
        assert caught.value is unrelated
        assert caught.value.__cause__ is None
        assert exception_error_id(caught.value, bank='ctbc') != 'CTBC-04-100-001'
        assert events == [('wait', 5000), 'deposit-evaluate', ('wait', 3000), 'history']
    else:
        with pytest.raises(RuntimeError, match='ctbc-twd-history-inventory') as caught: crawler.collect(page, col)
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        assert caught.value.__suppress_context__ is False
        sink('ctbc', caught.value, 'CTBC-04-100-001', '開啟台幣存款總覽')
        assert events == [('wait', 5000), 'deposit-evaluate', ('wait', 3000)]

@pytest.mark.parametrize('field,value,code,label', [
    ('startDate', None, 'CATHAY-05-830-001', '起日不是字串'),
    ('startDate', 'bad', 'CATHAY-05-830-002', '起日格式不符'),
    ('startDate', '2026-02-30', 'CATHAY-05-830-003', '起日日曆日期無效'),
    ('startDate', '2026-08-02', 'CATHAY-05-030-012', '起日不符'),
    ('endDate', None, 'CATHAY-05-831-001', '迄日不是字串'),
    ('endDate', 'bad', 'CATHAY-05-831-002', '迄日格式不符'),
    ('endDate', '2026-02-30', 'CATHAY-05-831-003', '迄日日曆日期無效'),
    ('endDate', '2026-08-02', 'CATHAY-05-030-013', '迄日不符'),
    ('accountDate', 1, 'CATHAY-05-832-001', '帳務日期不是字串'),
    ('accountDate', 'bad', 'CATHAY-05-832-002', '帳務日期格式不符'),
    ('accountDate', '2026-02-30', 'CATHAY-05-832-003', '帳務日期日曆日期無效'),
])
def test_returned_date_rejection_only_mandatory_caller(monkeypatch, field, value, code, label):
    response = {'success': True, 'returnCode': '0000', 'content': {'datas': [{
        'queryStatus': 'SUCCESS', 'accountNumber': 'synthetic', 'count': 1,
        'startDate': '2026-08-01', 'endDate': '2026-08-30',
        'details': [{'txnDateTime':'2026-08-20T10:00:00', 'expendAmt':'1'}],
    }]}}
    acct = response['content']['datas'][0]
    (acct['details'][0] if field == 'accountDate' else acct)[field] = value
    original = CathayCrawler._twd_response_date
    reads = []
    def observe(value, **kwargs):
        reads.append(value)
        return original(value, **kwargs)
    monkeypatch.setattr(CathayCrawler, '_twd_response_date', staticmethod(observe))
    with pytest.raises(RuntimeError) as caught:
        CathayCrawler._validated_twd_account(response, 'synthetic', start=date(2026,8,1), end=date(2026,8,30))
    assert reads == ([value] if field == 'startDate' else ['2026-08-01', value] if field == 'endDate' else ['2026-08-01','2026-08-30',value])
    assert str(caught.value) == ('cathay-twd-history-transaction-date' if field == 'accountDate' else 'cathay-twd-history-account-mismatch')
    sink('cathay', caught.value, code, label)
    assert original('bad') is None  # optional lookup still nonfatal


@pytest.mark.parametrize('label,reason,code', [
    ('帳號','no-group','CATHAY-05-010-005'), ('帳號','no-input','CATHAY-05-010-006'),
    ('查詢期間','no-group','CATHAY-05-010-007'), ('查詢期間','no-input','CATHAY-05-010-008'),
])
def test_seed_returned_reason_exact_native_order(label, reason, code):
    events=[]
    def evaluate(script, arg=None):
        events.append(('evaluate', arg))
        if arg is None: return 1
        return reason if arg == label else 'focused'
    page=SimpleNamespace(evaluate=evaluate, wait_for_timeout=lambda n: events.append(('wait', n)), keyboard=SimpleNamespace(press=lambda k: events.append(('key', k))))
    with pytest.raises(RuntimeError) as caught:
        object.__new__(CathayCrawler)._seed_twd_query_templates(page, ResponseCollector())
    expected=[('wait',3000),('evaluate',None),('key','Escape'),('wait',300),('evaluate','帳號')]
    if label == '查詢期間': expected += [('wait',400),('key','ArrowDown'),('wait',250),('key','Enter'),('wait',800),('evaluate','查詢期間')]
    assert events == expected  # no query click and no extra reads
    assert str(caught.value) == ('cathay-twd-history-account-control' if label == '帳號' else 'cathay-twd-history-period-control')
    sink('cathay',caught.value,code, ('帳號' if label == '帳號' else '期間') + ('群組缺失' if reason == 'no-group' else '輸入欄位缺失'))

@pytest.mark.parametrize('kind,code', [('unrelated','CTBC-05-020-020'),('status','CTBC-05-071-001'),('malformed','CTBC-05-071-002'),('recover-new',None),('recover-old',None)])
def test_owned_seed_rejection_and_recovery_actual_terminal(monkeypatch, kind, code):
    from tests.test_ctbc_history_sync import _collector, _template, _FetchPage
    crawler=object.__new__(CtbcCrawler)
    crawler.transaction_cursors={}
    col=_collector(['synthetic'])
    good=_template('synthetic')
    bad=_template('synthetic')
    if kind == 'status': bad.status=503
    else: bad.req_body['rqData']['accountId']=None
    unrelated=_template('synthetic'); unrelated.req_body['resource']='/other'
    col.hits=col.hits[:1]+({'unrelated':[unrelated], 'status':[bad], 'malformed':[bad], 'recover-new':[bad,good], 'recover-old':[good,bad]}[kind])
    deposit, identities=crawler._validated_twd_inventory(col)
    page=_FetchPage(empty=True)
    if code:
        with pytest.raises(RuntimeError, match='ctbc-twd-history-template') as caught:
            crawler._collect_twd_deposit_history(page,col,deposit,expected_identities=identities,as_of=date(2026,8,30))
        sink('ctbc',caught.value,code, '模板缺失' if kind == 'unrelated' else '狀態未成功' if kind == 'status' else '模板資料不合法')
        assert page.payloads == []
    else:
        result=crawler._collect_twd_deposit_history(page,col,deposit,expected_identities=identities,as_of=date(2026,8,30))
        assert len(page.payloads)==6
        assert result['accounts'][0]['months']['m0']==[]

@pytest.mark.parametrize('resource,code,label', [
    ('/twrbc-home/qu000/010','CTBC-04-040-001','首頁彙總'),
    ('/twrbc-card/qu006/011','CTBC-06-040-001','信用卡功能'),
])
def test_latest_rsdata_role_through_real_collect(monkeypatch, resource, code, label):
    error=InjectedFailure('PRIVATE_SENTINEL'); events=[]
    class Response(dict):
        def get(self, key, *args):
            events.append(key)
            if key == 'rsData': raise error
            return super().get(key,*args)
    col=ResponseCollector()
    col.hits.append(ApiHit(url='https://www.ctbcbank.com'+_CTBC_EBMW_PATH,method='POST',status=200,
                           req_body={'resource':resource},resp_json=Response(code='0000')))
    crawler=object.__new__(CtbcCrawler)
    monkeypatch.setattr(crawler,'_goto_twd_deposit',lambda *args: None)
    monkeypatch.setattr(crawler,'_validated_twd_inventory',lambda *args,**kwargs: ({},set()))
    monkeypatch.setattr(crawler,'_collect_twd_deposit_history',lambda *args,**kwargs: {'accounts':[],'coverage':{}})
    monkeypatch.setattr(crawler,'_goto_credit_card',lambda *args,**kwargs: {'clicked':None})
    page=SimpleNamespace(wait_for_timeout=lambda ms: None, locator=lambda s: SimpleNamespace(first=SimpleNamespace(count=lambda:0)))
    with pytest.raises(InjectedFailure) as caught: crawler.collect(page,col)
    assert caught.value is error
    assert events == ['code','rsData']
    sink('ctbc',caught.value,code,label)


@pytest.mark.parametrize('endpoint,code,label', [
    ('C_BILL_Q_CardUnbilledConsume','CATHAY-06-350-001','未出帳'),
    ('C_BILL_Q_CardCurrentConsume','CATHAY-06-360-001','即時消費'),
])
def test_malformed_consume_block_role_at_actual_sink(endpoint,code,label):
    col=ResponseCollector()
    col.hits.append(ApiHit(url='https://synthetic.invalid/'+endpoint,method='POST',status=200,
                           resp_json={'success':True,'content':{'consume':[{'tradeData':[]},None]}}))
    with pytest.raises(AttributeError,match="'NoneType' object has no attribute 'get'") as caught:
        object.__new__(CathayCrawler)._parse(col)
    sink('cathay',caught.value,code,label)

@pytest.mark.parametrize('bank', ['ctbc','cathay'])
@pytest.mark.parametrize('signals', [('error','error'), ('negative','negative'), ('error','negative'), ('error','positive','negative')])
def test_shared_login_actual_terminal_inspection_cause(monkeypatch, bank, signals):
    from backend.core import base
    from backend.core.error_codes import format_error_id
    from backend.core.login_checkpoints import LoginCheckpointBlocked
    from tests.test_auth_probe_diagnostic_bridge import run_offline
    cls=CtbcCrawler if bank=='ctbc' else CathayCrawler
    crawler=object.__new__(cls); crawler.name=bank
    monkeypatch.setattr(crawler,'prepare_login_page',lambda page: None)
    monkeypatch.setattr(crawler,'login_checkpoint_rules',lambda: ())
    monkeypatch.setattr(crawler,'_submit_with_captcha_reloads',lambda *args: events.append('submit'))
    events=[]; errors=[]; sequence=iter(signals)
    def evaluate(script):
        event=next(sequence)
        events.append(event)
        if event=='error':
            error=InjectedFailure('PRIVATE_SENTINEL');errors.append(error);raise error
        return {'ok':event=='positive'}
    page=SimpleNamespace(url=('https://www.ctbcbank.com/twrbc/twrbc-home/qu000/010' if bank=='ctbc' else 'https://www.cathaybk.com.tw/OnlineBanking/Home/Asset'), evaluate=evaluate,
                         frames=[],main_frame=object(),set_default_timeout=lambda n: events.append(n))
    original_outcomes = []
    validate = base.validate_login_checkpoint_outcome
    def observe(outcome, rules):
        original_outcomes.append(outcome)
        return validate(outcome, rules)
    monkeypatch.setattr(base, 'validate_login_checkpoint_outcome', observe)
    result, terminals = run_offline(monkeypatch, crawler, page)
    terminal, = terminals
    assert type(terminal) is LoginCheckpointBlocked
    assert terminal.outcome is original_outcomes[-1]
    terminal_code = bank.upper() + ('-03-988-002' if len(signals) == 3 else '-03-988-001')
    expected_code = bank.upper() + '-03-011-001' if signals[-1] == 'error' else terminal_code
    assert original_outcomes[-1].error_id == expected_code
    assert terminal.outcome.error_id == expected_code
    assert exception_error_id(terminal, bank=bank) == expected_code
    assert terminal.__cause__ is None and terminal.__context__ is None
    if signals[-1]=='error':
        assert exception_error_id(errors[-1], bank=bank) == expected_code
        assert errors[-1].__cause__ is None and errors[-1].__context__ is None
        sink(bank,terminal,expected_code,'檢查登入正向訊號')
    else:
        sink(bank,terminal,terminal_code,'登入')
    assert result['failure_diagnostics']['error_id'] == expected_code
    assert format_error_id(expected_code, bank=bank) in result['error']
    action, reason = _EXPECTED_FOLLOWUP_LABELS[expected_code]
    assert f'[{expected_code}] {action}：{reason}' in result['error']
    assert 'LoginCheckpointBlocked' in result['error']
    assert 'PRIVATE_SENTINEL' not in repr(result)
    assert [e for e in events if isinstance(e,str) and e!='submit']==list(signals)
    assert events.count('submit') == 1
    assert events.count(5000)==len(signals)
    assert events.count(180000)==len(signals)
    assert '_login_inspection_failures' not in crawler.__dict__

@pytest.mark.parametrize('sdk', ['patchright','playwright'])
@pytest.mark.parametrize('origin,expected_reads', [('protocol',['protocol']),('hostname',['protocol','hostname']),('port',['protocol','hostname','port']),('fetch',['protocol','hostname','port'])])
def test_installed_sdk_historical_thrown_transport_offline(original_banks, sdk, origin, expected_reads):
    """Historical thrown-channel control; current rejection uses a returned marker."""
    import importlib
    import json
    import subprocess
    from pathlib import Path
    from tests.test_ctbc_history_sync import _template
    scripts=[]
    hit=_template('synthetic')
    def capture(script,payload):
        scripts.append((script,payload))
        return {'status':200,'url':hit.url,'redirected':False,'contentType':'application/json','json':None}
    with pytest.raises(RuntimeError):
        original_banks['ctbc']['CtbcCrawler']._fetch_qu002_011(SimpleNamespace(evaluate=capture),hit.url,hit.req_body,'synthetic','m0','Bearer synthetic-token')
    bundle=Path(importlib.import_module(sdk).__file__).parent/'driver/package/lib/coreBundle.js'
    harness=r"""
const fs=require('fs');const input=JSON.parse(fs.readFileSync(0,'utf8'));
const source=fs.readFileSync(input.bundle,'utf8');
function extract(start,end) {const i=source.indexOf(start); if(i<0) throw Error(start);return source.slice(i,source.indexOf(end,i)+end.length);}
// Execute the installed implementation, not a reimplementation of its serializer.
eval(extract('function getExceptionMessage(exceptionDetails) {','\n}'));
eval(extract('function serializeError(e) {','\n}'));
eval(extract('function isError(','\n}'));
const JavaScriptErrorInEvaluate=eval('('+extract('JavaScriptErrorInEvaluate = class extends Error {','\n    };').replace(/^JavaScriptErrorInEvaluate = /,'').replace(/;$/,'')+')');
eval(extract('function rewriteError(error) {','\n}'));
const CRExecutionContext=eval('('+extract('CRExecutionContext = class {','\n    };').replace(/^CRExecutionContext = /,'').replace(/;$/,'')+')');
let reads=[],fetches=0;
global.location={};
for(const key of ['protocol','hostname','port']) Object.defineProperty(location,key,{get(){
 reads.push(key);if(reads.length>input.expected.length)throw Error('short-circuit violated');
 return key===input.origin ? ({protocol:'http:',hostname:'foreign.invalid',port:'444'}[key]) : ({protocol:'https:',hostname:'www.ctbcbank.com',port:''}[key]);}});
global.fetch=()=>{fetches++;throw Error(input.origin==='fetch'?'PRIVATE_SENTINEL':'fetch must not run');};
const client={send:async()=>{
 try {await eval('('+input.script+')')(input.payload);throw Error('expected rejection');}
 catch(e){
  if(e.message!==(input.origin==='fetch'?'PRIVATE_SENTINEL':'ctbc-origin-mismatch'))throw e;
  // Even explicitly supplied remote metadata is discarded by the real adapter.
  e.name='ClosedOriginProtocol';e.closedReason='protocol';
  return {exceptionDetails:{text:'Uncaught',exception:{description:e.stack,closedReason:e.closedReason,name:e.name}},result:{}};
 }
}};
(async()=>{try{await new CRExecutionContext(client,{id:1}).evaluateWithArguments('unused',true,{_objectId:'fake'},[],[]);throw Error('expected SDK error');}
catch(e){if(!(e instanceof JavaScriptErrorInEvaluate))throw e;process.stdout.write(JSON.stringify({serialized:serializeError(e),reads,fetches}));}})().catch(e=>{console.error(e);process.exit(1);});
"""
    out=subprocess.run(['node','-e',harness],input=json.dumps({'bundle':str(bundle),'script':scripts[0][0],'payload':scripts[0][1],'origin':origin,'expected':expected_reads}),text=True,capture_output=True,check=True)
    observed=json.loads(out.stdout)
    assert observed['reads']==expected_reads
    assert observed['fetches']==(1 if origin=='fetch' else 0)
    payload=observed['serialized']['error']
    assert set(payload)=={'message','name','stack'}
    assert payload['name']=='Error'  # custom name did not cross the Chromium adapter
    error=importlib.import_module(sdk+'._impl._helper').parse_error(payload)
    assert type(error) is importlib.import_module(sdk+'._impl._errors').Error
    assert error.name=='Error'
    assert ('PRIVATE_SENTINEL' if origin=='fetch' else 'ctbc-origin-mismatch') in error.message
    assert not hasattr(error,'closedReason')
    def fail(script,payload): raise error
    with pytest.raises(type(error)) as caught:
        CtbcCrawler._fetch_qu002_011(SimpleNamespace(evaluate=fail),hit.url,hit.req_body,'synthetic','m0','Bearer synthetic-token')
    assert caught.value is error
    # Actual native evaluation errors remain generic; do not classify their message.
    sink('ctbc',caught.value,'CTBC-05-400-001','送出已驗證月份請求並讀取回應')

@pytest.mark.parametrize('endpoint,code,label', [
    ('G_CUST_Q_ForeignNetPresentInfo','CATHAY-07-450-001','外幣現值'),
    ('G_CUST_Q_InvestNetPresentInfo','CATHAY-07-451-001','投資現值'),
    ('G_CUST_Q_InsNetPresentInfo','CATHAY-07-452-001','保險現值'),
    ('G_CUST_Q_LoanNetPresentInfo','CATHAY-07-453-001','貸款現值'),
])
def test_net_present_content_leaf_roles(endpoint,code,label):
    error=InjectedFailure('PRIVATE_SENTINEL')
    class Hit:
        @property
        def resp_json(self): raise error
    col=SimpleNamespace(latest=lambda ep: Hit() if ep==endpoint else None)
    with pytest.raises(InjectedFailure) as caught: object.__new__(CathayCrawler)._parse(col)
    assert caught.value is error
    sink('cathay',caught.value,code,label)


@pytest.mark.parametrize('field,code', [('expendAmt','CATHAY-05-800-001'),('incomeAmt','CATHAY-05-801-001'),('balance','CATHAY-05-802-001')])
def test_standalone_normalize_field_valueerror_contract(field,code):
    row={'txnDateTime':'2026-08-20T10:00:00','expendAmt':'1','incomeAmt':'0','balance':'99'}
    row[field]=True
    with pytest.raises(ValueError) as caught: object.__new__(CathayCrawler)._normalize_twd_transactions([row])
    assert str(caught.value)=='invalid amount'
    sink('cathay',caught.value,code,'金額' if field!='balance' else '餘額')


@pytest.mark.parametrize('field,value,code,label', [
    ('hasNext','next','CTBC-05-824-001','下一頁'), ('hasMore','next','CTBC-05-825-001','後續資料'),
    ('moreData','next','CTBC-05-826-001','更多資料'), ('nextPage','next','CTBC-05-827-001','後續頁碼'),
    ('nextToken','next','CTBC-05-828-001','後續游標'),
    ('hasNext',None,'CTBC-05-829-001','下一頁'), ('hasMore',None,'CTBC-05-833-001','後續資料'),
    ('moreData',None,'CTBC-05-834-001','更多資料'),
])
def test_other_evidence_field_roles(field,value,code,label):
    from tests.test_ctbc_history_sync import _template
    hit=_template('synthetic');hit.resp_json['rsData'][field]=value
    def evaluate(script,payload):return {'status':200,'url':hit.url,'redirected':False,'contentType':'application/json','json':hit.resp_json}
    with pytest.raises(RuntimeError,match='pagination-detected') as caught:
        CtbcCrawler._fetch_qu002_011(SimpleNamespace(evaluate=evaluate),hit.url,hit.req_body,'synthetic','m0','Bearer synthetic-token')
    sink('ctbc',caught.value,code,label)

@pytest.mark.parametrize('recover,independent', [(False,False),(True,False),(True,True)])
def test_cathay_collect_inventory_navigation_cause_is_stage_bounded(monkeypatch, recover, independent):
    from backend.banks.cathay import _ACCOUNT_INVENTORY_PATH
    crawler=object.__new__(CathayCrawler);crawler.name='cathay';crawler.transaction_cursors={}
    col=ResponseCollector(); events=[]
    failure=InjectedFailure('PRIVATE_SENTINEL'); later=InjectedFailure('unrelated')
    monkeypatch.setattr(cathay_module,'FEATURE_PAGES',{'asset':'/asset','twd_txn':'/twd'})
    def goto(url,**kwargs):
        events.append(('goto',url.rsplit('/',1)[-1]))
        if url.endswith('/asset'):raise failure
    def wait(ms):
        events.append(('wait',ms))
        if recover and not col.hits:
            col.hits.append(ApiHit(url='https://www.cathaybk.com.tw'+_ACCOUNT_INVENTORY_PATH,method='POST',status=200,
                req_body={'content':{'queryType':'TWD'}},resp_json={'success':True,'returnCode':'0000','content':{'datas':[]}}))
    def cards(page):
        if independent:raise later
    monkeypatch.setattr(crawler,'_collect_card_details',cards)
    monkeypatch.setattr(crawler,'_parse',lambda col: {})
    page=SimpleNamespace(goto=goto,wait_for_timeout=wait)
    if not recover:
        with pytest.raises(RuntimeError,match='cathay-twd-history-account-inventory') as caught:crawler.collect(page,col)
        assert (caught.value.__cause__, caught.value.__context__, caught.value.__suppress_context__) == (None, None, False)
        sink('cathay',caught.value,'CATHAY-04-500-001','開啟資產總覽')
    elif independent:
        with pytest.raises(InjectedFailure) as caught:crawler.collect(page,col)
        assert caught.value is later
        assert caught.value.__cause__ is None
    else:
        assert crawler.collect(page,col).twd_transactions==[]
    assert events==[('goto','asset'),('wait',6500),('goto','twd'),('wait',6500)]

@pytest.fixture(scope='module')
def original_banks():
    import subprocess
    from pathlib import Path
    originals={}
    for bank in ('ctbc','cathay'):
        path='backend/banks/'+bank+'.py'
        source=subprocess.run(['git','show','16f2d4b130f44973d346b420f3d486ac839f71ac:'+path],text=True,capture_output=True,check=True).stdout
        namespace={'__file__':str(Path(__file__).resolve().parents[1]/path),'__name__':'offline_original_'+bank}
        exec(compile(source,path,'exec'),namespace)
        originals[bank]=namespace
    return originals


def outcome(operation):
    try: return ('ok',operation())
    except Exception as error: return (type(error).__name__,str(error))


def test_original_current_native_form_trace_and_outcome(original_banks):
    results=[]
    for cls in (original_banks['ctbc']['CtbcCrawler'],CtbcCrawler):
        page=NativeForm()
        result=cls._fetch_native_history_window(page,page.collector,'synthetic',date(2026,8,1),date(2026,8,31))
        results.append((result,page.events))
    assert results[0]==results[1]
    assert results[0][0]==[]
    assert results[0][1].count('click:action')==1


@pytest.mark.parametrize('bank', ['ctbc','cathay'])
def test_original_current_field_date_contract_matrix(original_banks,bank):
    if bank=='ctbc':
        original=original_banks['ctbc']['_validated_ctbc_detail'];current=ctbc_module._validated_ctbc_detail
        base={'actDtTm':'2026-08-20-10.00.00','dbAmt':'1','crAmt':'0','balanceAmt':'99'}
        mutations=[(f,True) for f in ('dbAmt','crAmt','balanceAmt','memo1','memo2','bankId','trfAcct','memoCode')]+[('actDtTm','bad')]
        run=lambda function,row:function(row,start=date(2026,8,1),end=date(2026,8,30))
    else:
        original=original_banks['cathay']['CathayCrawler']._validated_twd_account;current=CathayCrawler._validated_twd_account
        base={'queryStatus':'SUCCESS','accountNumber':'synthetic','count':1,'startDate':'2026-08-01','endDate':'2026-08-30',
              'details':[{'txnDateTime':'2026-08-20T10:00:00','expendAmt':'1','incomeAmt':'0','balance':'99'}]}
        mutations=[(f,True) for f in ('expendAmt','incomeAmt','balance','description','expendBankId','expendAcctNo','memo')]
        mutations += [(f,v) for f in ('startDate','endDate','accountDate') for v in (None,1,'bad','2026-02-30','2026-08-02')]
        run=lambda function,row:function({'success':True,'returnCode':'0000','content':{'datas':[row]}},'synthetic',start=date(2026,8,1),end=date(2026,8,30))
    for field,value in [*mutations, (None, None)]:
        row=deepcopy(base)
        if field:
            (row['details'][0] if bank=='cathay' and field not in ('startDate','endDate') else row)[field]=value
        assert outcome(lambda:run(original,deepcopy(row)))==outcome(lambda:run(current,deepcopy(row)))


@pytest.mark.parametrize('bank', ['ctbc','cathay'])
@pytest.mark.parametrize('fail', [True,False])
def test_original_current_actual_request_json_trace(original_banks,bank,fail):
    results=[]
    for cls in (original_banks[bank]['CtbcCrawler' if bank=='ctbc' else 'CathayCrawler'], CtbcCrawler if bank=='ctbc' else CathayCrawler):
        page=OfflineScriptPage(fail)
        if bank=='ctbc':
            from tests.test_ctbc_history_sync import _template
            hit=_template('synthetic')
            result=outcome(lambda:cls._fetch_qu002_011(page,hit.url,hit.req_body,'synthetic','m0','Bearer synthetic-token'))
        else:
            from tests.test_cathay_history_sync import _template
            crawler=object.__new__(cls);crawler.transaction_cursors={}
            crawler._seed_twd_query_templates=lambda *args: [_template('synthetic')]
            result=outcome(lambda:crawler._collect_twd_history(page,ResponseCollector(),as_of=date(2026,8,30)))
        results.append((result,page.events,page.result['json']))
    assert results[0]==results[1]


@pytest.mark.parametrize('bank', ['ctbc','cathay'])
@pytest.mark.parametrize('signals',[('error','error'),('negative','negative'),('error','positive','negative'),('error','positive','positive')])
def test_original_current_shared_login_native_trace(original_banks,bank,signals):
    results=[]
    for cls in (original_banks[bank]['CtbcCrawler' if bank=='ctbc' else 'CathayCrawler'],CtbcCrawler if bank=='ctbc' else CathayCrawler):
        crawler=object.__new__(cls);crawler.name=bank;events=[];sequence=iter(signals)
        crawler.prepare_login_page=lambda page: events.append('prepare')
        crawler.login_checkpoint_rules=lambda: ()
        crawler._submit_with_captcha_reloads=lambda *args:events.append('submit')
        class Page:
            frames=[];main_frame=object()
            @property
            def url(self):
                events.append('url')
                return 'https://www.ctbcbank.com/twrbc/home' if bank=='ctbc' else 'https://www.cathaybk.com.tw/OnlineBanking/Home/Asset'
            def set_default_timeout(self,n):events.append(('timeout',n))
            def evaluate(self,script):
                signal=next(sequence);events.append(signal)
                if signal=='error':raise InjectedFailure('PRIVATE_SENTINEL')
                return {'ok':signal=='positive'}
        result=outcome(lambda:crawler._shared_login(Page()))
        assert "_login_inspection_failures" not in crawler.__dict__
        results.append((result,events))
    assert results[0]==results[1]


def test_ctbc_real_collect_valid_empty_inventory_recovers_navigation(monkeypatch):
    from tests.test_ctbc_history_sync import _inventory_hit
    crawler=object.__new__(CtbcCrawler);crawler.name='ctbc';crawler.transaction_cursors={}
    col=ResponseCollector();col.hits.append(_inventory_hit([]));events=[]
    def evaluate(script):
        events.append('deposit')
        raise InjectedFailure('PRIVATE_SENTINEL')
    monkeypatch.setattr(crawler,'_goto_credit_card',lambda *args,**kwargs: {'clicked':None})
    page=SimpleNamespace(url='https://www.ctbcbank.com/twrbc/home',evaluate=evaluate,
        wait_for_timeout=lambda ms:events.append(ms),locator=lambda selector:SimpleNamespace(first=SimpleNamespace(count=lambda:0)))
    result=crawler.collect(page,col)
    assert result.twd_history==[]
    assert events==[5000,'deposit',3000,6000,6000,6000,6000]


def test_cathay_real_collect_missing_query_control_retains_stage_navigation(monkeypatch):
    from backend.banks.cathay import _ACCOUNT_INVENTORY_PATH
    crawler=object.__new__(CathayCrawler);crawler.name='cathay';crawler.transaction_cursors={}
    col=ResponseCollector()
    col.hits.append(ApiHit(url='https://www.cathaybk.com.tw'+_ACCOUNT_INVENTORY_PATH,method='POST',status=200,
        req_body={'content':{'queryType':'TWD'}},resp_json={'success':True,'returnCode':'0000','content':{'datas':[{'currency':'TWD','accountNo':'synthetic'}]}}))
    monkeypatch.setattr(cathay_module,'FEATURE_PAGES',{'twd_txn':'/twd'})
    error=InjectedFailure('PRIVATE_SENTINEL');events=[]
    def goto(*args,**kwargs):events.append('goto');raise error
    def evaluate(script,arg=None):events.append(('evaluate',arg));return 1 if arg is None else 'no-group'
    page=SimpleNamespace(goto=goto,evaluate=evaluate,wait_for_timeout=lambda ms:events.append(ms),keyboard=SimpleNamespace(press=lambda key:events.append(key)))
    with pytest.raises(RuntimeError,match='cathay-twd-history-account-control') as caught:crawler.collect(page,col)
    assert (caught.value.__cause__, caught.value.__context__, caught.value.__suppress_context__) == (None, None, False)
    sink('cathay',caught.value,'CATHAY-05-500-001','開啟台幣交易查詢')
    assert events==['goto',6500,3000,('evaluate',None),'Escape',300,('evaluate','帳號')]


def test_original_catalog_entries_byte_exact_and_no_id_recycling():
    import hashlib
    from pathlib import Path
    expected={'ctbc':'b84b1dc446ea7aff7229ec1928c9aa88bc509bcec6dd87d4bb2b6b09966a9850',
              'cathay':'b9d282888a31c6d04312ae47cf007300a8f81b3e79c32ff75b5441dee246d798'}
    for bank,digest in expected.items():
        source=(Path(__file__).resolve().parents[1]/'backend/core/error_catalog'/f'{bank}.py').read_text()
        original=source.split('\n\nCODES.update(',1)[0]+'\n'
        assert hashlib.sha256(original.encode()).hexdigest()==digest
        seen=set()
        for statement in ast.parse(source).body:
            if isinstance(statement,ast.Assign):entries=statement.value
            elif isinstance(statement,ast.Expr) and isinstance(statement.value,ast.Call):entries=statement.value.args[0]
            else:continue
            values=ast.literal_eval(entries)
            assert not seen.intersection(values)
            seen.update(values)


@pytest.mark.parametrize('field,value', [(f,v) for f in ('totalPages','pageCount','count','totalCount') for v in (True,2)] + [(f,v) for f in ('hasNext','hasMore','moreData','nextPage','nextToken') for v in ('next',None)])
def test_original_current_replay_evidence_order_contract(original_banks,field,value):
    from tests.test_ctbc_history_sync import _template
    results=[]
    for cls in (original_banks['ctbc']['CtbcCrawler'],CtbcCrawler):
        hit=_template('synthetic');data=hit.resp_json['rsData'];events=[]
        if field=='pageCount':del data['totalPages']
        if field=='totalCount':del data['count']
        data[field]=value
        def evaluate(script,payload):
            events.append(payload)
            return {'status':200,'url':hit.url,'redirected':False,'contentType':'application/json','json':hit.resp_json}
        result=outcome(lambda:cls._fetch_qu002_011(SimpleNamespace(evaluate=evaluate),hit.url,hit.req_body,'synthetic','m0','Bearer synthetic-token'))
        results.append((result,events))
        assert len(events)==1
    assert results[0]==results[1]

@pytest.mark.parametrize('unexpected',[[],{}])
def test_seed_unknown_return_preserves_original_terminal_contract(unexpected):
    page=SimpleNamespace(evaluate=lambda script,arg=None: 1 if arg is None else unexpected,
        wait_for_timeout=lambda ms:None,keyboard=SimpleNamespace(press=lambda key:None))
    with pytest.raises(RuntimeError) as caught:object.__new__(CathayCrawler)._seed_twd_query_templates(page,ResponseCollector())
    assert str(caught.value)=='cathay-twd-history-account-control'
    assert exception_error_id(caught.value,bank='cathay')=='CATHAY-05-010-001'
