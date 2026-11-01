"""Finite offline optional-emission parity against frozen 16f2 definitions.

No crawler constructors, persistence, browser, credentials loading or real I/O.
Run explicitly with -p tests.parent_offline_gate (portable scoped policy).
"""
import ast
import __future__
from asyncio import CancelledError
from functools import lru_cache
from importlib import import_module
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from tests.test_optional_diagnostic_logging import Page

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "16f2d4b130f44973d346b420f3d486ac839f71ac"


@pytest.fixture(autouse=True)
def require_offline_gate(request):
    from tests import parent_offline_gate
    assert request.config.pluginmanager.getplugin("tests.parent_offline_gate") is parent_offline_gate, "hard offline gate required"
    assert parent_offline_gate.active, "hard offline gate must be active"


@lru_cache(None)
def original_namespace(bank):
    module = import_module(f"backend.banks.{bank}")
    source = subprocess.check_output(
        ["git", "show", f"{BASELINE}:backend/banks/{bank}.py"], cwd=ROOT, text=True
    )
    tree = ast.parse(source)
    tree.body = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))]
    namespace = vars(module).copy()
    exec(compile(tree, f"original-{bank}.py", "exec",
                 flags=__future__.annotations.compiler_flag), namespace)
    return namespace


class LinePage(Page):
    def evaluate(self, script, arg=None):
        if "15000)" in script:
            key, result = "home", "retained home"
        elif "20000)" in script:
            key, result = "transaction", "retained transaction"
        elif "25000)" in script:
            key, result = "account-text", "retained account"
        elif "t.length > 30" in script:
            key, result = "nav", [{"text": "retained nav"}]
        elif "return {opts}" in script:
            key, result = "options", {"opts": [
                {"value": "synthetic-one", "text": "first"},
                {"value": "synthetic-two", "text": "second"},
            ]}
        elif "(value)" in script:
            key, result = "select", True
        elif "t === '查詢'" in script:
            key, result = "query", True
        else:
            raise AssertionError(f"unplanned LINE script: {script}")
        self.action(key, script, arg)
        return result


def run_line(monkeypatch, failure=None, *, original=False, mode="working", old_log=None):
    module = import_module("backend.banks.linebank")
    ns = original_namespace("linebank") if original else vars(module)
    page = LinePage("linebank", failure)
    logs, diagnostics = [], []
    logger_error = BrokenPipeError("synthetic broken pipe")

    def log(message):
        if message.startswith("[LINEBANK-"):
            diagnostics.append(message)
            if mode == "logger":
                raise logger_error
            if mode == "same":
                raise page.error
        else:
            logs.append(message)
            if old_log and old_log in message:
                raise logger_error

    def broken_format(*args, **kwargs):
        raise logger_error

    with monkeypatch.context() as patch:
        patch.setitem(ns, "_debug_dir", lambda: Path("/synthetic-unused"))
        patch.setitem(ns, "_log", log)
        if mode == "format":
            patch.setitem(ns, "format_error_id", broken_format)
        crawler = object.__new__(ns["LinebankCrawler"])
        try:
            result = crawler.collect(page, SimpleNamespace(hits=[])).to_dict()
            error = None
        except Exception as caught:
            result, error = None, caught
    return SimpleNamespace(result=result, error=error, page=page, logs=logs,
                           diagnostics=diagnostics, logger_error=logger_error)


def assert_parity(before, after):
    assert type(before.error) is type(after.error), repr(after.error)
    assert str(before.error) == str(after.error)
    assert before.result == after.result
    assert before.page.events == after.page.events
    assert before.page.counts == after.page.counts
    assert before.logs == after.logs


LINE_CASES = [
    (("title", 1), "LINEBANK-04-002-001", None),
    (("home", 1), "LINEBANK-04-003-001", None),
    (("nav", 1), "LINEBANK-04-004-001", None),
    (("goto", 1), "LINEBANK-04-005-001", "goto /transaction 失敗"),
    (("transaction", 1), "LINEBANK-04-006-001", None),
    (("options", 1), "LINEBANK-05-002-001", "讀 select 失敗"),
    (("select", 1), "LINEBANK-05-003-001", "查詢帳戶#1 失敗"),
]


@pytest.mark.parametrize("mode", ["working", "logger", "format", "same"])
@pytest.mark.parametrize("failure,leaf,old_log", LINE_CASES)
def test_line_natural_handlers(monkeypatch, failure, leaf, old_log, mode):
    before = run_line(monkeypatch, failure, original=True)
    after = run_line(monkeypatch, failure, mode=mode)
    assert before.error is None
    assert_parity(before, after)
    if mode != "format":
        assert len(after.diagnostics) == 1
        assert after.diagnostics[0].startswith(f"[{leaf}]")
        assert "PRIVATE" not in after.diagnostics[0]
    if failure[0] == "select":
        assert after.result["accounts_queried"][-1]["option"]["text"] == "second"
    if old_log:
        before = run_line(monkeypatch, failure, original=True, old_log=old_log)
        after = run_line(monkeypatch, failure, mode=mode, old_log=old_log)
        assert before.error is before.logger_error
        assert after.error is after.logger_error
        assert_parity(before, after)


def test_first_native_title_failure_and_broken_pipe_keep_original_default(monkeypatch):
    failure = ("title", 1)
    before = run_line(monkeypatch, failure, original=True)
    after = run_line(monkeypatch, failure, mode="logger")
    assert before.error is None
    assert_parity(before, after)
    assert after.result["title"] == ""
    assert after.page.counts["title"] == 1
    assert after.diagnostics[0].startswith("[LINEBANK-04-002-001]")


def run_submit(monkeypatch, bank, *, original=False, mode="working", fail=True):
    fixture_module = import_module("tests.test_rakuten_crawler" if bank == "rakuten"
                                 else f"tests.test_{bank}_login_checkpoints")
    fixture = (fixture_module._submit_fixture(monkeypatch) if bank == "rakuten"
               else fixture_module._submit_fixture())
    template, page = fixture[:2]
    module = import_module(f"backend.banks.{bank}")
    ns = original_namespace(bank) if original else vars(module)
    cls = ns[{"linebank": "LinebankCrawler", "taishin": "TaishinCrawler",
              "rakuten": "RakutenCrawler"}[bank]]
    crawler = object.__new__(cls)
    crawler.__dict__.update(template.__dict__)
    native = RuntimeError("PRIVATE synthetic native/logger failure")
    logger_error = BrokenPipeError("synthetic broken pipe")
    diagnostics, events = [], []
    fields = fixture[2] if bank != "taishin" else fixture[3]
    if bank == "rakuten":
        def count():
            events.append("otp-count")
            if fail:
                raise native
            return 0
        fixture[4]["input[name='otpCode']"].count.side_effect = count
    else:
        def wait(ms):
            events.append(("wait", ms))
            if fail and ms == 10000:
                raise native
        page.wait_for_timeout.side_effect = wait

    def emit(message):
        diagnostics.append(message)
        if mode == "logger":
            raise logger_error
        if mode == "same":
            raise native

    def formatter(*args, **kwargs):
        raise logger_error

    with monkeypatch.context() as patch:
        patch.setitem(ns, "print" if bank == "rakuten" else "_log", emit)
        if mode == "format":
            patch.setitem(ns, "format_error_id", formatter)
        try:
            result = crawler.submit_credentials_once(page)
            error = None
        except Exception as caught:
            result, error = None, caught
    trace = [repr(page.mock_calls), *[repr(f.mock_calls) for f in fields.values()],
             repr([call.args[0] is page for call in crawler._logged_in.call_args_list])]
    if bank == "taishin":
        trace += [repr(fixture[2].mock_calls), repr(fixture[4].mock_calls),
                  repr(fixture[5].mock_calls),
                  repr([(call.args[0] is fixture[2], call.kwargs)
                        for call in crawler._ocr_captcha.call_args_list])]
    elif bank == "linebank":
        trace += [repr(fixture[3].mock_calls), repr(fixture[4].mock_calls),
                  repr(fixture[5].mock_calls), repr(fixture[5].nth.return_value.mock_calls)]
    else:
        trace += [repr(fixture[3].mock_calls),
                  *[repr(locator.mock_calls) for locator in fixture[4].values()]]
    return SimpleNamespace(result=result, error=error, diagnostics=diagnostics,
                           trace=trace, events=events)


@pytest.mark.parametrize("mode", ["working", "logger", "format", "same"])
@pytest.mark.parametrize("bank,leaf", [
    ("linebank", "LINEBANK-03-001-001"),
    ("taishin", "TAISHIN-03-001-001"),
    ("rakuten", "RAKUTEN-03-702-002"),
])
def test_submit_optional_observation_returns_without_resubmit(monkeypatch, bank, leaf, mode):
    before = run_submit(monkeypatch, bank, original=True)
    after = run_submit(monkeypatch, bank, mode=mode)
    assert before.error is None
    assert after.error is None, repr(after.error)
    assert before.result is after.result is None
    assert before.trace == after.trace
    assert before.events == after.events
    if mode != "format":
        assert len(after.diagnostics) == 1
        assert after.diagnostics[0].startswith(f"[{leaf}]")
        assert "PRIVATE" not in after.diagnostics[0]


@pytest.mark.parametrize("bank", ["linebank", "taishin", "rakuten"])
def test_submit_healthy_observation_trace_control(monkeypatch, bank):
    before = run_submit(monkeypatch, bank, original=True, fail=False)
    after = run_submit(monkeypatch, bank, fail=False, mode="logger")
    assert before.error is after.error is None
    assert before.trace == after.trace
    assert before.events == after.events
    assert not after.diagnostics


class TaishinFrame:
    def __init__(self, page, name):
        self.page, self.name = page, name
        self.url = "https://my.taishinbank.com.tw/TIBNetBank/svc/rwd/index.html"

    def evaluate(self, script, arg=None):
        return self.page.evaluate_in(self.name, script, arg)

    def locator(self, selector):
        self.page.action("locator", self.name, selector)
        return self

    def nth(self, index):
        self.page.action("nth", self.name, index)
        return self

    def select_option(self, **kwargs):
        self.page.action("month-select", self.name, kwargs)
        self.page.month = kwargs["index"]

    def get_attribute(self, name):
        self.page.action("iframe-attribute", name)
        return "svc/rwd/index.html"

    def bounding_box(self):
        self.page.action("iframe-box")
        return {"x": 30, "y": 40}


class TaishinPage(Page):
    def __init__(self, failure=None):
        super().__init__("taishin", failure)
        self.main_frame = TaishinFrame(self, "main")
        self.children = [TaishinFrame(self, "first"), TaishinFrame(self, "second")]
        self.mouse = SimpleNamespace(move=self.move, click=self.click)
        self.month = 0

    @property
    def frames(self):
        self.action("frames")
        return [self.main_frame, *self.children]

    def move(self, *args, **kwargs):
        self.action("move", args, kwargs)

    def query_selector_all(self, selector):
        self.action("iframes", selector)
        return self.children[:1]

    def evaluate(self, script, arg=None):
        return self.evaluate_in("page", script, arg)

    def evaluate_in(self, name, script, arg):
        if "querySelectorAll('tr')" in script:
            key = "overview"
            result = [["未設定", "1234-56-1234567-8", "0", "0", "105"]]
        elif "click_visible:" in script:
            key = "card-nav"
            result = [{"y": 100, "click_visible": True}] if name != "page" else []
        elif "span._nav_menu__text" in script:
            key, result = "li-box", {"x": 10, "y": 20, "w": 40, "h": 20}
        elif "(li_y)" in script:
            key, result = "mega-links", [
                {"text": "查詢信用卡明細", "x": 50, "y": 100, "w": 40, "h": 20}]
        elif "12000)" in script:
            key, result = "card-text", self.card_text()
        elif "monthSel" in script:
            key, result = "months", [{"index": 1, "text": "2026/06"},
                                       {"index": 2, "text": "2026/05"}]
        elif "location.hash" in script:
            key, result = "month-route", None
        elif "50000)" in script:
            key, result = "month-text", self.card_text()
        else:
            raise AssertionError(f"unplanned Taishin script: {script}")
        self.action(key, name, script, arg)
        return result

    def card_text(self):
        page = self

        class Text(str):
            def lower(self):
                page.action("parse")
                return super().lower()

        return Text("信用卡 即時消費紀錄 消費日期 消費時間 消費明細 授權結果\n"
                    "本期剩餘應繳款項\n0\n帳單結帳日\n2026/06/14\n"
                    "繳款截止日\n2026/06/29\n上期實繳金額明細\n"
                    f"2026/06/{20 + self.month}\n2026/06/{20 + self.month}\n自動扣繳\n新臺幣\n-80\n"
                    "當期消費明細\n")


def run_taishin(monkeypatch, failure=None, *, original=False, mode="working", old_log=None):
    module = import_module("backend.banks.taishin")
    ns = original_namespace("taishin") if original else vars(module)
    page = TaishinPage(failure)
    logs, diagnostics = [], []
    logger_error = BrokenPipeError("synthetic broken pipe")

    def log(message):
        if message.startswith("[TAISHIN-"):
            diagnostics.append(message)
            if mode == "logger":
                raise logger_error
            if mode == "same":
                raise page.error
            if isinstance(mode, BaseException):
                raise mode
        else:
            logs.append(message)
            if old_log and old_log in message:
                raise logger_error

    def broken_format(*args, **kwargs):
        raise logger_error

    with monkeypatch.context() as patch:
        patch.setitem(ns, "_log", log)
        if mode == "format":
            patch.setitem(ns, "format_error_id", broken_format)
        crawler = object.__new__(ns["TaishinCrawler"])
        # Unrelated mandatory history is supplied equally to both frozen classes.
        def history(*args):
            page.action("required-history")
            return {"twd_txn_results": []}
        crawler._collect_attested_twd_history = history
        try:
            result = crawler.collect(page, SimpleNamespace(hits=[])).to_dict()
            error = None
        except Exception as caught:
            result, error = None, caught
    return SimpleNamespace(result=result, error=error, page=page, logs=logs,
                           diagnostics=diagnostics, logger_error=logger_error)


TAISHIN_COLLECT_CASES = [
    (("overview", 1), "TAISHIN-04-001-001", None),
    (("li-box", 1), "TAISHIN-06-001-001", "取 LI bbox 例外"),
    (("iframe-box", 1), "TAISHIN-06-004-001", "取 iframe offset 失敗"),
    (("move", 1), "TAISHIN-06-851-001", "mouse.move 例外"),
    (("mega-links", 1), "TAISHIN-06-009-001", "dump mega links 失敗"),
    (("mouse", 1), "TAISHIN-06-012-001", "click 子項例外"),
    (("card-nav", 1), "TAISHIN-06-016-001", None),
    (("frames", 2), "TAISHIN-07-009-001", "找信用卡 nav 例外"),
    (("card-text", 1), "TAISHIN-06-018-001", None),
    (("months", 1), "TAISHIN-06-019-001", "月份下拉 dump 失敗"),
    (("month-select", 1), "TAISHIN-06-715-001", "月份 2026/06 查詢失敗"),
    (("parse", 1), "TAISHIN-07-013-001", "parser 例外"),
]


@pytest.mark.parametrize("mode", ["working", "logger", "format", "same"])
@pytest.mark.parametrize("failure,leaf,old_log", TAISHIN_COLLECT_CASES)
def test_taishin_natural_handlers_and_nested_recovery(monkeypatch, failure, leaf, old_log, mode):
    before = run_taishin(monkeypatch, failure, original=True)
    after = run_taishin(monkeypatch, failure, mode=mode)
    assert before.error is None, repr(before.error)
    assert_parity(before, after)
    if mode != "format":
        assert len(after.diagnostics) == 1, after.diagnostics
        assert after.diagnostics[0].startswith(f"[{leaf}]")
        assert "PRIVATE" not in after.diagnostics[0]
    assert after.page.counts["required-history"] == 1
    if failure[0] in {"card-nav", "card-text"}:
        assert after.result["credit_card_parsed"]["fetch_ok"] is True
    if failure[0] == "month-select":
        assert after.page.counts["month-select"] == 2
        assert after.result["credit_card_parsed"]["billed_txns"][-1]["post_date"] == "2026/06/22"
    if failure[0] == "parse":
        assert after.result["credit_card_parsed"] == {"error": "parse_failed"}
    if old_log:
        before = run_taishin(monkeypatch, failure, original=True, old_log=old_log)
        after = run_taishin(monkeypatch, failure, mode=mode, old_log=old_log)
        assert_parity(before, after)
        # Some old logs are caught by an existing outer recovery, never newly suppressed.
        assert old_log in "\n".join(after.logs)


@pytest.mark.parametrize("runner", [run_line, run_taishin])
def test_healthy_collect_preserves_original_finance_and_native_order(monkeypatch, runner):
    before = runner(monkeypatch, original=True)
    after = runner(monkeypatch, mode="logger")
    assert before.error is None, repr(before.error)
    assert_parity(before, after)
    assert not after.diagnostics


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit, CancelledError])
def test_taishin_cancel_is_not_optional_logging_failure(monkeypatch, error_type):
    error = error_type("synthetic cancellation")
    with pytest.raises(error_type) as caught:
        run_taishin(monkeypatch, ("li-box", 1), mode=error)
    assert caught.value is error


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit, CancelledError])
def test_original_native_cancellation_escapes_unchanged(monkeypatch, error_type):
    error = error_type("synthetic native cancellation")
    action = TaishinPage.action
    def cancel(page, key, *args):
        action(page, key, *args)
        if key == "li-box":
            raise error
    monkeypatch.setattr(TaishinPage, "action", cancel)
    for original in (True, False):
        with pytest.raises(error_type) as caught:
            run_taishin(monkeypatch, original=original, mode="logger")
        assert caught.value is error


def run_value_helper(monkeypatch, role, *, original=False, mode="working", fail=True):
    module = import_module("backend.banks.taishin")
    ns = original_namespace("taishin") if original else vars(module)
    page = Page("taishin")
    native = (ValueError if role == "payment" else RuntimeError)("PRIVATE original failure")
    diagnostics = []

    class Amount:
        def __init__(self, value):
            self.value = value

        def __float__(self):
            page.action("float", self.value)
            if fail and self.value == 0:
                raise native
            return float(self.value)

    class Response(dict):
        def get(self, key, *default):
            page.action("projection-get", key)
            if fail and key == "value":
                raise native
            return super().get(key, *default)

    def emit(message):
        diagnostics.append(message)
        if mode == "logger":
            raise BrokenPipeError("synthetic broken pipe")
        if mode == "same":
            raise native

    def broken_format(*args, **kwargs):
        raise ValueError("synthetic formatter failure")

    with monkeypatch.context() as patch:
        patch.setitem(ns, "_log", emit)
        if mode == "format":
            patch.setitem(ns, "format_error_id", broken_format)
        try:
            if role == "payment":
                parsed = {"fetch_ok": True, "summary": {"remaining": 0},
                          "billing_period": {"statement_date": "2026/06/14",
                                             "pay_due_date": "2026/06/29"},
                          "billed_txns": [
                              {"amount": Amount(0), "post_date": "2026/06/19"},
                              {"amount": Amount(-80), "post_date": "2026/06/20"},
                              {"amount": Amount(-90), "post_date": "2026/06/21"}]}
                result = ns["_taishin_card_bill_fact"](parsed)
            else:
                url = "https://my.taishinbank.com.tw/TIBNetBank/svc/web/common/qryTaishinPoint"
                def hit(response):
                    return SimpleNamespace(raw_url=url, url=url, content_type="application/json",
                                           method="POST", status=200, redirected=False,
                                           body_size=100, resp_json=response)
                result = ns["TaishinCrawler"]._non_sensitive_api_responses([
                    hit(Response(value={"balance": 10})), hit({"value": {"balance": 20}})])
            error = None
        except Exception as caught:
            result, error = None, caught
    return SimpleNamespace(result=result, error=error, page=page, logs=[],
                           diagnostics=diagnostics)


@pytest.mark.parametrize("mode", ["working", "logger", "format", "same"])
@pytest.mark.parametrize("role,leaf", [("payment", "TAISHIN-06-015-001"),
                                       ("projection", "TAISHIN-07-007-001")])
def test_invalid_payment_and_api_projection_keep_later_candidates(monkeypatch, role, leaf, mode):
    before = run_value_helper(monkeypatch, role, original=True)
    after = run_value_helper(monkeypatch, role, mode=mode)
    assert before.error is None
    assert_parity(before, after)
    if role == "payment":
        assert after.page.counts["float"] == 3
        assert after.result["last_payment_date"] == "2026-06-21"
        assert after.result["last_payment_amount"] == 90.0
    else:
        assert after.result == {"qryTaishinPoint": {"value": {"balance": 20}}}
    if mode != "format":
        assert len(after.diagnostics) == 1
        assert after.diagnostics[0].startswith(f"[{leaf}]")
        assert "PRIVATE" not in after.diagnostics[0]


@pytest.mark.parametrize("role", ["payment", "projection"])
def test_value_helper_success_control(monkeypatch, role):
    before = run_value_helper(monkeypatch, role, original=True, fail=False)
    after = run_value_helper(monkeypatch, role, mode="logger", fail=False)
    assert before.error is None
    assert_parity(before, after)
    assert not after.diagnostics


class HistoryPage(Page):
    def __init__(self, stage=None, persistent=False):
        from copy import deepcopy
        from tests.test_taishin_history_sync import _form_snapshot
        super().__init__("taishin")
        self.stage, self.persistent = stage, persistent
        self.route, self.queried = 0, False
        self.form = _form_snapshot()
        self.form["selects"][1]["options"].append(
            {"index": 2, "text": "012-34-567890-1-24 Richart", "value": "01234567890124"})
        for control in self.form["selects"]:
            control["selected_index"] = 0
        self.location = "https://my.taishinbank.com.tw/TIBNetBank/svc/rwd/index.html#/RB0102/0100"
        self.mouse = SimpleNamespace(move=lambda *args: self.action("move", *args))
        self.collector = SimpleNamespace(hits=[], request_sequence=0,
                                         issued_count=self.issued_count)
        self.copy = deepcopy

    @property
    def frames(self):
        self.action("frames")
        return [self]

    def evaluate(self, script, arg=None):
        from tests.test_taishin_history_sync import _result_snapshot
        if script.startswith("location.hash"):
            self.action("route", script, arg)
            self.route += 1
            self.queried = False
            return None
        if "const form =" in script:
            self.action("result", script, arg)
            state = _result_snapshot()
            if not self.queried:
                state.update(table_count=0, no_more_count=0, rows=[], headers=[], total_count=0)
            state["form"] = self.copy(self.form)
            return state
        if "query_buttons:" in script:
            key = "initial-inventory" if self.route == 1 else "next-inventory"
            self.action(key, script, arg)
            if self.stage == key and (self.persistent or self.counts[key] == 1):
                raise self.error
            return self.copy(self.form)
        if "MutationObserver" in script or "disconnect()" in script:
            self.action("observer", script, arg)
            return None
        raise AssertionError(f"unplanned history script: {script}")

    def locator(self, selector):
        self.action("locator", selector)
        if selector == "select":
            return SimpleNamespace(nth=self.select_nth)
        assert selector == "input[value='查詢']"
        return SimpleNamespace(count=self.query_count, nth=self.query_nth)

    def issued_count(self, endpoint):
        self.action("issued-count", endpoint)
        return self.counts["query-click"]

    def query_count(self):
        self.action("query-count")
        return 1

    def query_nth(self, index):
        self.action("query-nth", index)
        return SimpleNamespace(click=self.query_click)

    def select_nth(self, index):
        self.action("nth", index)
        def select_option(**kwargs):
            self.action("select-option", index, kwargs)
            self.form["selects"][index]["selected_index"] = kwargs["index"]
        return SimpleNamespace(select_option=select_option)

    def query_click(self, **kwargs):
        from tests.test_taishin_history_sync import _history_hit
        self.action("query-click", kwargs)
        self.queried = True
        collector = self.collector
        collector.request_sequence += 1
        hit = _history_hit()
        hit.request_sequence = collector.request_sequence
        control = self.form["selects"][1]
        identity = control["options"][control["selected_index"]]["value"]
        hit.req_body["account"] = identity
        hit.request_frame = self
        collector.hits.append(hit)


def run_history(monkeypatch, stage=None, *, original=False, mode="working", persistent=False):
    from datetime import date
    module = import_module("backend.banks.taishin")
    ns = original_namespace("taishin") if original else vars(module)
    page = HistoryPage(stage, persistent)
    diagnostics = []

    def emit(message):
        diagnostics.append(message)
        if mode == "logger":
            raise BrokenPipeError("synthetic broken pipe")
        if mode == "same":
            raise page.error

    def broken_format(*args, **kwargs):
        raise ValueError("synthetic formatter failure")

    with monkeypatch.context() as patch:
        patch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
        patch.setitem(ns, "_log", emit)
        if mode == "format":
            patch.setitem(ns, "format_error_id", broken_format)
        crawler = object.__new__(ns["TaishinCrawler"])
        crawler.transaction_cursors = {"twd_transactions": {}}
        crawler._shared_dialog_blocked = False
        try:
            result = crawler._collect_attested_twd_history(
                page, page.collector, as_of=date(2026, 9, 2))
            error = None
        except Exception as caught:
            result, error = None, caught
    return SimpleNamespace(result=result, error=error, page=page, logs=[],
                           diagnostics=diagnostics)


@pytest.mark.parametrize("mode", ["working", "logger", "format", "same"])
@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.parametrize("stage,leaf,terminal", [
    ("initial-inventory", "TAISHIN-05-041-001", "taishin-twd-history-form"),
    ("next-inventory", "TAISHIN-05-041-001", "taishin-twd-history-inventory"),
])
def test_history_inventory_retry_terminal_and_recovery_parity(
        monkeypatch, stage, leaf, terminal, persistent, mode):
    before = run_history(monkeypatch, stage, original=True, persistent=persistent)
    after = run_history(monkeypatch, stage, mode=mode, persistent=persistent)
    if persistent:
        assert type(before.error) is RuntimeError
        assert str(before.error) == terminal
        assert before.page.counts[stage] == 60
    else:
        assert before.error is None, repr(before.error)
        assert len(before.result["twd_txn_results"]) == 2
        assert before.page.counts["query-click"] == 2
    assert_parity(before, after)
    if mode != "format":
        assert len(after.diagnostics) == (60 if persistent else 1)
        assert all(message.startswith(f"[{leaf}]") for message in after.diagnostics)
        assert all("PRIVATE" not in message for message in after.diagnostics)


def test_history_healthy_native_trace_control(monkeypatch):
    before = run_history(monkeypatch, original=True)
    after = run_history(monkeypatch, mode="logger")
    assert before.error is None, repr(before.error)
    assert_parity(before, after)
    assert before.page.counts["query-click"] == 2
    assert not after.diagnostics


@pytest.mark.parametrize("bank,count", [("linebank", 8), ("taishin", 17), ("rakuten", 1)])
def test_exact_single_emission_guards_not_handler_or_collector_guards(bank, count):
    tree = ast.parse((ROOT / f"backend/banks/{bank}.py").read_text())
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    emissions = [node for node in ast.walk(tree) if isinstance(node, ast.Expr)
                 and isinstance(node.value, ast.Call)
                 and isinstance(node.value.func, ast.Name)
                 and node.value.func.id in {"_log", "print"}
                 and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                         and c.func.id == "format_error_id" for c in ast.walk(node))]
    assert len(emissions) == count
    for emission in emissions:
        guard = parents[emission]
        assert isinstance(guard, ast.With)
        assert len(guard.items) == 1
        assert ast.unparse(guard.items[0].context_expr) == "contextlib.suppress(Exception)"
        assert guard.body == [emission]
        assert isinstance(parents[guard], ast.ExceptHandler)


@pytest.mark.parametrize("failure,old_log,primary_leaf", [
    (("li-box", 1), "取 LI bbox 例外", "TAISHIN-06-001-001"),
    (("months", 1), "月份下拉 dump 失敗", "TAISHIN-06-019-001"),
    (("month-select", 1), "月份 2026/06 查詢失敗", "TAISHIN-06-715-001"),
])
def test_old_nested_handler_logger_failure_still_reaches_original_outer_catch(
        monkeypatch, failure, old_log, primary_leaf):
    before = run_taishin(monkeypatch, failure, original=True, old_log=old_log)
    after = run_taishin(monkeypatch, failure, mode="logger", old_log=old_log)
    assert_parity(before, after)
    assert len(after.diagnostics) == 2
    assert all(message.startswith(f"[{primary_leaf}]") for message in after.diagnostics)
    outer_log = "找信用卡 nav 例外" if failure[0] == "li-box" else "parser 例外"
    assert outer_log in after.logs[-2]


@pytest.mark.parametrize("failure,old_log", [
    (("frames", 2), "找信用卡 nav 例外"),
    (("parse", 1), "parser 例外"),
])
def test_old_outer_handler_logs_remain_fallible(monkeypatch, failure, old_log):
    before = run_taishin(monkeypatch, failure, original=True, old_log=old_log)
    after = run_taishin(monkeypatch, failure, mode="logger", old_log=old_log)
    assert before.error is before.logger_error
    assert after.error is after.logger_error
    assert_parity(before, after)
