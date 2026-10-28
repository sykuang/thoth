"""Offline parity with immutable original sources; never construct a real crawler."""
import ast
from collections import Counter
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from tests.diagnostic_originals import original_source
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@lru_cache(None)
def original_namespace(bank):
    module = import_module(f"backend.banks.{bank}")
    source = original_source(f"backend/banks/{bank}.py")
    # Execute only original definitions in an isolated namespace: no main, imports,
    # credentials, debug-directory creation, browser or persistence invocation.
    tree = ast.parse(source)
    tree.body = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))]
    namespace = vars(module).copy()
    exec(compile(tree, f"original-{bank}.py", "exec"), namespace)
    return namespace


class Page:
    def __init__(self, bank, failure=None):
        self.bank, self.failure = bank, failure
        self.events, self.counts = [], Counter()
        self.error = RuntimeError("PRIVATE synthetic native/logger failure")
        self.location = "https://synthetic.invalid/overview"
        self.card = 0
        self.mouse = SimpleNamespace(click=self.click)

    def action(self, key, *args):
        self.counts[key] += 1
        self.events.append((key, args))
        if self.failure == (key, self.counts[key]):
            raise self.error

    @property
    def url(self):
        self.action("url")
        return self.location

    def title(self):
        self.action("title")
        return "retained title"

    def wait_for_timeout(self, ms):
        self.action("wait", ms)

    def screenshot(self, **kwargs):
        self.action("screenshot", kwargs)

    def goto(self, url, **kwargs):
        self.action("goto", url, kwargs)
        self.location = "https://synthetic.invalid/overview"

    def click(self, x, y):
        self.action("mouse", x, y)
        if self.bank == "scb":
            self.card = 1 if x < 100 else 2
            self.location = f"https://synthetic.invalid/expense/{self.card}"

    def evaluate(self, script, arg=None):
        if self.bank == "dbs":
            return self.dbs_evaluate(script, arg)
        return self.scb_evaluate(script, arg)

    def dbs_evaluate(self, script, arg):
        if "15000)" in script:
            key, result = "home", "retained home"
        elif "t.length > 30" in script:
            key, result = "nav", [{"text": "retained nav"}]
        elif "({acctName, acctTail})" in script:
            key, result = "drilldown", {"clicked": True, "candidate_count": 1}
        elif "40000)" in script:
            key, result = "detail", "retained account detail"
        elif ".slice(0, 120)" in script:
            key, result = "controls", []
        elif "(wanted)" in script:
            key, result = "month", {"clicked": True}
        elif "t === '其他月份'" in script:
            key, result = "other", {"clicked": True}
        elif "new Set(" in script:
            key, result = "other-controls", []
        elif "({year, monthLabel})" in script:
            key, result = "target", {"found": True, "x": 1, "y": 2}
        elif "x.text === '繳卡費'" in script:
            key, result = "fee-click", {"clicked": True}
        elif "20000)" in script:
            key, result = "fee-text", "retained fee detail"
        else:
            raise AssertionError(f"unplanned DBS script: {script}")
        self.action(key, script, arg)
        return result

    def scb_evaluate(self, script, arg):
        buttons = [{"x": x, "y": 10, "w": 10, "h": 10} for x in (10, 200)]
        if "15000)" in script:
            key = "home" if self.counts["overview"] == 0 else "card-text"
            result = "retained " + key
        elif "25000)" in script:
            key, result = "card-detail", f"retained card {self.card} detail"
        elif "20000)" in script:
            key, result = "bill-text", "retained bill detail"
        elif "t !== '信用卡綜覽'" in script:
            key, result = "overview", {"ok": True}
        elif "t !== '消費明細'" in script:
            key, result = "buttons", buttons
        elif "t !== '查詢'" in script:
            key, result = "query", None
        elif "t !== '帳單查詢'" in script:
            key, result = "bill-click", {"ok": True}
        elif "trim() === '帳戶綜覽'" in script:
            key, result = "deposit-click", True
        elif script == "() => document.body.innerText":
            key = "deposit-text"
            result = "結構型商品查詢\n活期存款 123●●●456\n存款總金額 TWD 1,234"
        else:
            raise AssertionError(f"unplanned SCB script: {script}")
        self.action(key, script, arg)
        return result


def run_collect(monkeypatch, bank, failure=None, *, original=False, logger_fails=False,
                old_logger_fails=False, old_logger_failure=None):
    module = import_module(f"backend.banks.{bank}")
    namespace = original_namespace(bank) if original else vars(module)
    page = Page(bank, failure)
    logs, diagnostics = [], []

    def log(message):
        if message.startswith(f"[{bank.upper()}-"):
            diagnostics.append(message)
            if logger_fails:
                raise page.error  # Same RuntimeError instance as the native failure.
        else:
            logs.append(message)
            if old_logger_fails or (old_logger_failure and old_logger_failure in message):
                raise page.error

    with monkeypatch.context() as patch:
        patch.setitem(namespace, "_debug_dir", lambda: Path("/synthetic-unused"))
        patch.setitem(namespace, "_log", log)
        cls = namespace[{"dbs": "DbsCrawler", "scb": "ScbCrawler"}[bank]]
        crawler = object.__new__(cls)
        hit = SimpleNamespace(endpoint="synthetic", resp_json={"retained": True},
                              url="https://synthetic.invalid/api", method="GET", status=200,
                              req_body=None)
        def latest(endpoint):
            page.action("latest", endpoint)
            return None
        collector = SimpleNamespace(hits=[hit], latest=latest)
        try:
            result = crawler.collect(page, collector).to_dict()
        except Exception as error:
            return page, None, error, diagnostics, logs
    return page, result, None, diagnostics, logs


DBS_CASES = [
    (("title", 1), "DBS-04-001-001"),
    (("home", 1), "DBS-04-002-001"),
    (("nav", 1), "DBS-04-003-001"),
    (("month", 1), "DBS-05-005-001"),
    (("other", 1), "DBS-05-014-001"),
    (("target", 1), "DBS-05-009-001"),
    (("drilldown", 1), "DBS-05-001-001"),
    (("fee-click", 1), "DBS-06-005-001"),
]
SCB_CASES = [
    (("title", 1), "SCB-04-005-001"),
    (("home", 1), "SCB-06-002-001"),
    (("card-text", 1), "SCB-06-005-001"),
    (("card-detail", 1), "SCB-06-012-001"),
    (("mouse", 1), "SCB-06-007-001"),
    (("overview", 3), "SCB-06-016-001"),
    (("bill-text", 1), "SCB-06-020-001"),
]


@pytest.mark.parametrize("bank", ["dbs", "scb"])
def test_success_matches_original(monkeypatch, bank):
    before = run_collect(monkeypatch, bank, original=True)
    after = run_collect(monkeypatch, bank, logger_fails=True)
    assert before[2] is after[2] is None
    assert before[1] == after[1]
    assert before[0].events == after[0].events
    assert after[1]["api_responses"]["synthetic"][0]["resp"] == {"retained": True}
    assert not after[3]


@pytest.mark.parametrize("bank,failure,code", [
    *(('dbs', failure, code) for failure, code in DBS_CASES),
    *(('scb', failure, code) for failure, code in SCB_CASES),
])
def test_optional_native_failure_keeps_original_fallback_when_diagnostic_throws(
        monkeypatch, bank, failure, code):
    before = run_collect(monkeypatch, bank, failure, original=True, logger_fails=True)
    after = run_collect(monkeypatch, bank, failure, logger_fails=True)
    assert before[2] is None
    assert after[2] is None, f"diagnostic escaped original fallback: {after[2]!r}"
    assert before[1] == after[1]
    assert before[0].events == after[0].events  # Every native call, argument, count and order.
    assert after[0].counts[failure[0]] >= failure[1]
    assert len(after[3]) == 1
    assert after[3][0].startswith(f"[{code}] ")
    assert any("\u4e00" <= char <= "\u9fff" for char in after[3][0])
    assert "PRIVATE" not in after[3][0]
    if bank == "dbs":
        if failure[0] == "fee-click":
            assert after[1]["dbs_card_fee_error"] == "probe_failed"
        else:
            assert after[1]["dbs_card_fee_page_text"] == "retained fee detail"
        if failure[0] in {"month", "other", "target"}:
            assert after[1]["twd_account_detail_text"] == "retained account detail"
            assert "twd_account_drilldown_error" not in after[1]
        if failure[0] == "month":
            assert after[1]["twd_txn_month_clicks"][0]["error"] == "probe_failed"
            assert after[1]["twd_txn_month_clicks"][1]["clicked"] is True
        if failure[0] == "target":
            assert after[1]["twd_txn_other_month_clicks"][1]["clicked"] is True
    else:
        assert after[1]["cards_detail"][-1]["text"] == "retained card 2 detail"
        assert after[1]["accounts"][0]["balance"] == "1234"
        if failure[0] == "card-detail":
            assert after[1]["cards_detail"][0]["text"] == ""


@pytest.mark.parametrize("failure,code", [
    (("deposit-click", 1), "SCB-04-001-001"),
    (("wait", 1), "SCB-04-002-001"),
    (("deposit-text", 1), "SCB-04-003-001"),
])
def test_deposit_helper_retains_empty_fallback(monkeypatch, failure, code):
    module = import_module("backend.banks.scb")
    pages = []
    diagnostics = []
    for original in (True, False):
        namespace = original_namespace("scb") if original else vars(module)
        page = Page("scb", failure)
        pages.append(page)
        def log(message):
            diagnostics.append(message)
            raise page.error
        with monkeypatch.context() as patch:
            patch.setitem(namespace, "_log", log)
            assert namespace["ScbCrawler"]._deposit_accounts(page) == []
    assert pages[0].events == pages[1].events
    assert len(diagnostics) == 1
    assert diagnostics[0].startswith(f"[{code}] ")
    assert "PRIVATE" not in diagnostics[0]


@pytest.mark.parametrize("bank", ["dbs", "scb"])
def test_original_logging_failures_are_not_newly_suppressed(monkeypatch, bank):
    before = run_collect(monkeypatch, bank, original=True, old_logger_fails=True)
    after = run_collect(monkeypatch, bank, old_logger_fails=True)
    assert before[2] is before[0].error
    assert after[2] is after[0].error
    assert before[0].events == after[0].events


def test_deposit_failure_still_reaches_original_mandatory_terminal(monkeypatch):
    before = run_collect(monkeypatch, "scb", ("deposit-click", 1), original=True, logger_fails=True)
    after = run_collect(monkeypatch, "scb", ("deposit-click", 1), logger_fails=True)
    assert type(before[2]) is type(after[2]) is RuntimeError
    assert str(before[2]) == str(after[2]) == "scb-accounts-empty"
    assert before[0].events == after[0].events


@pytest.mark.parametrize("bank,failure,message", [
    ("dbs", ("drilldown", 1), "drilldown probe failed; details withheld"),
    ("dbs", ("fee-click", 1), "probe failed; details withheld"),
    ("scb", ("mouse", 1), "例外:"),
])
def test_old_handler_logs_still_throw(monkeypatch, bank, failure, message):
    before = run_collect(monkeypatch, bank, failure, original=True,
                         old_logger_failure=message)
    after = run_collect(monkeypatch, bank, failure, logger_fails=True,
                        old_logger_failure=message)
    assert before[2] is before[0].error
    assert after[2] is after[0].error
    assert before[0].events == after[0].events
    assert before[4] == after[4]


@pytest.mark.parametrize("bank,failure,code", [
    *(('dbs', failure, code) for failure, code in DBS_CASES),
    *(('scb', failure, code) for failure, code in SCB_CASES),
])
def test_working_diagnostic_retains_native_leaf(monkeypatch, bank, failure, code):
    before = run_collect(monkeypatch, bank, failure, original=True)
    after = run_collect(monkeypatch, bank, failure)
    assert before[2] is after[2] is None
    assert before[1] == after[1]
    assert before[0].events == after[0].events
    assert before[4] == after[4]
    assert len(after[3]) == 1
    assert after[3][0].startswith(f"[{code}] ")
    assert "PRIVATE" not in after[3][0]
