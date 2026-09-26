"""Offline review regressions for E.SUN collector selection, privacy, and JSF parity."""
import ast
from copy import deepcopy
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.banks import esun
from backend.banks.esun import EsunCrawler
from backend.banks.esun_spa.capture import SpaCollector
from backend.core.base import ResponseCollector


class StartupPage:
    def __init__(self, mount_after=None, *, legacy=False):
        self.url = "https://ebank.esunbank.com.tw/index.jsp"
        self.waited = 0
        self.mount_after = mount_after
        self.frames = []
        self.main_frame = object()
        self.legacy = legacy
        self.listeners = {}
        if legacy:
            frame = SimpleNamespace(
                url="https://ebank.esunbank.com.tw/fco/fco08001/FCO08001_Home.faces",
                name="iframe1", parent_frame=self.main_frame,
            )
            frame.locator = lambda selector: SimpleNamespace(count=lambda: 1)
            self.frames.append(frame)

    def evaluate(self, script):
        return self.mount_after is not None and self.waited >= self.mount_after

    def wait_for_timeout(self, milliseconds):
        self.waited += milliseconds

    def on(self, name, fn):
        self.listeners.setdefault(name, []).append(fn)

    def remove_listener(self, name, fn):
        self.listeners[name].remove(fn)


def _crawler():
    crawler = object.__new__(EsunCrawler)
    crawler.name = "esun"
    return crawler


def test_delayed_spa_mount_selects_capture_before_any_login_or_repeat(monkeypatch):
    page = StartupPage(mount_after=500)
    crawler = _crawler()
    submit = Mock()
    crawler.submit_credentials_once = submit
    collector = crawler._make_collector(page)
    assert isinstance(collector, SpaCollector)
    assert page.waited >= 500
    assert page.waited <= 10000
    assert crawler._spa_login_baseline is None
    submit.assert_not_called()
    # The core attaches this collector before the first credential submission.
    monkeypatch.setattr("backend.banks.esun_spa.capture._HistoryBodyObserver",
                        lambda *args: SimpleNamespace(disconnect=lambda: None, close=lambda: None))
    collector.attach(page)
    try:
        assert collector.snapshot()["collector"] is collector
        submit.assert_not_called()
    finally:
        collector.detach(page)


def test_legacy_login_form_is_positive_evidence_not_spa_timeout():
    page = StartupPage(legacy=True)
    collector = _crawler()._make_collector(page)
    assert type(collector) is ResponseCollector
    assert page.waited == 0


def test_delayed_mount_run_attaches_before_login_and_repeated_callback_never_submits_again(
    monkeypatch, tmp_path,
):
    page = StartupPage(mount_after=500)
    crawler = _crawler()
    crawler.session_dir = tmp_path / "session"
    crawler.session_dir.mkdir()
    submit = Mock()
    crawler.submit_credentials_once = submit
    monkeypatch.setattr(crawler, "_enforce_session_freshness", lambda: None)
    monkeypatch.setattr(crawler, "_build_fetch_kwargs", lambda: {"__cleanups__": []})
    monkeypatch.setattr(crawler, "attach_shared_dialog_handler", lambda page: None)
    monkeypatch.setattr("backend.banks.esun_spa.capture._HistoryBodyObserver",
                        lambda *args: SimpleNamespace(disconnect=lambda: None, close=lambda: None))

    def login(raw_page):
        assert isinstance(crawler.collector, SpaCollector)
        assert crawler.collector.page is raw_page
        crawler._spa_login_baseline = crawler.collector.snapshot()
        submit(raw_page)
        return False

    monkeypatch.setattr(crawler, "_shared_login", login)

    def execute(url, *, headless, page_action, fetch_kwargs):
        page_action(page)
        page_action(page)

    monkeypatch.setattr(crawler, "_execute_browser_flow", execute)
    result = crawler.run(page.url, headless=True)
    assert result == {"error": "browser_callback_repeated"}
    assert isinstance(crawler._spa_login_baseline, dict)
    assert crawler._spa_login_baseline["collector"] is crawler.collector
    submit.assert_called_once_with(page)


def test_unknown_startup_and_origin_drift_fail_closed_without_submission():
    crawler = _crawler()
    crawler.submit_credentials_once = Mock()
    unknown = StartupPage()
    with pytest.raises(Exception, match="collector|登入|頁面"):
        crawler._make_collector(unknown)
    assert unknown.waited <= 10000
    drift = StartupPage(mount_after=500)
    def wait_and_drift(milliseconds):
        drift.waited += milliseconds
        drift.url = "https://other.invalid/"
    drift.wait_for_timeout = wait_and_drift
    with pytest.raises(Exception, match="collector|登入|頁面"):
        crawler._make_collector(drift)
    assert drift.waited < 10000
    crawler.submit_credentials_once.assert_not_called()


def _history_result(*, empty=False):
    account = "9999999999999"
    start, end = date(2025, 8, 31), date(2026, 8, 30)
    result = {
        "account_no": account, "selected_identity": account,
        "clicked_period": {"checked": True, "start": "2025/08/31", "end": "2026/08/30"},
        "submit": {"clicked": "visible-query"},
        "url": "https://ebank.esunbank.com.tw/fco/fao01002/FAO01002.faces",
        "text": f"帳號 {account} 查詢期間 2025/08/31 至 2026/08/30\n" + (
            "查無符合資料！" if empty else "交易明細"),
        "snapshot": {
            "busy": False, "pager": {"present": False, "actionableNext": 0},
            "hasGrid": not empty, "gridCandidateCount": 0 if empty else 1,
            "gridText": "" if empty else "2026/08/20\n12:00:00 利息 2 84 活存利息\n",
            "gridRowCount": 0 if empty else 1,
            "gridRows": [] if empty else [["2026/08/20", "12:00:00", "利息", "", "2", "84", "活存利息"]],
            "totalCount": 0 if empty else None,
            "emptyMarker": "查無符合資料！" if empty else None,
        },
    }
    return result, account, start, end


@pytest.mark.parametrize("empty", [False, True])
def test_jsf_release_parity_only_for_unreported_count_or_known_empty(empty):
    result, account, start, end = _history_result(empty=empty)
    receipt = EsunCrawler._validated_twd_history_result(
        result, identity=account, start=start, end=end)
    assert receipt["status"] == ("explicit_empty" if empty else "complete")


@pytest.mark.parametrize("mutation", ["ambiguous_empty", "wrong_count", "unknown_marker", "cross_origin"])
def test_jsf_still_rejects_unproven_history(mutation):
    result, account, start, end = _history_result(empty=mutation != "wrong_count")
    result = deepcopy(result)
    if mutation == "ambiguous_empty":
        result["snapshot"]["emptyMarker"] = None
    elif mutation == "wrong_count":
        result["snapshot"]["totalCount"] = 2
    elif mutation == "unknown_marker":
        result["snapshot"]["emptyMarker"] = "未知資料狀態"
    else:
        result["url"] = "https://foreign.invalid/fco/fao01002/FAO01002.faces"
    with pytest.raises(RuntimeError, match="esun-twd-history-result"):
        EsunCrawler._validated_twd_history_result(result, identity=account, start=start, end=end)


def test_quota_parser_does_not_return_raw_canary():
    canary = "PRIVATE-CANARY-ID-987654321"
    parsed = EsunCrawler._parse_card_quota(f"歸戶\n20\n80\n{canary}")
    assert parsed == {"used_credit_twd": 20, "available_credit_twd": 80, "credit_limit_twd": 100}
    assert canary not in str(parsed)


def test_standalone_result_is_not_dumped_to_disk(monkeypatch, capsys):
    canary = "PRIVATE-CANARY-ID-987654321"
    source = Path(esun.__file__).read_text()
    main = next(node for node in ast.parse(source).body if isinstance(node, ast.If)
                and isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "__name__")
    writes = []
    monkeypatch.setattr(Path, "write_text", lambda self, *args, **kwargs: writes.append((self, args)))
    scope = {"__name__": "__main__", "EsunCrawler": lambda: SimpleNamespace(
        run=lambda **kwargs: {"error": "synthetic", "data": {"private": canary}}),
        "EsunLoginError": esun.EsunLoginError, "Path": Path, "__file__": esun.__file__,
        "BASE": esun.BASE, "_log": lambda message: None}
    exec(compile(ast.Module(body=[main], type_ignores=[]), esun.__file__, "exec"), scope)
    assert writes == []
    assert canary not in capsys.readouterr().err


def test_quota_stderr_logging_never_interpolates_raw_quota_dict():
    collect = next(node for node in ast.parse(Path(esun.__file__).read_text()).body
                   if isinstance(node, ast.ClassDef) and node.name == "EsunCrawler")
    source = ast.unparse(next(node for node in collect.body if isinstance(node, ast.FunctionDef)
                              and node.name == "collect"))
    assert 'card_quota={out[' not in source
