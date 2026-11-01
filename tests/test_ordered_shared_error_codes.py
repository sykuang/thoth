"""Real shared crawler boundaries retain registered action IDs without bank I/O."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.banks.ctbc import BASE, CtbcCrawler
from backend.core import base
from backend.core.error_codes import with_error_code


def test_recovered_native_query_readiness_does_not_mask_terminal_wait(monkeypatch):
    from datetime import date
    from backend.banks.esun_spa import query

    trace = []
    values = iter(([2026, 1, 1], 1200))
    original = TimeoutError("synthetic private wait")

    class Locator:
        def __init__(self, selector):
            self.selector = selector

        def locator(self, selector):
            trace.append(("nested", selector))
            return Locator(selector)

        def count(self):
            trace.append(("count", self.selector))
            return 0 if self.selector.startswith("dialog:") else 1

        def is_visible(self):
            trace.append(("visible", self.selector))
            return False

    class Page:
        def evaluate(self, script):
            trace.append(("evaluate",))
            return next(values)

        def locator(self, selector):
            trace.append(("locator", selector))
            return Locator(selector)

        def wait_for_timeout(self, milliseconds):
            trace.append(("wait", milliseconds))
            raise original

    monkeypatch.setattr(query, "time", SimpleNamespace(monotonic=lambda: 0))
    with pytest.raises(TimeoutError) as caught:
        query.query_twd(
            Page(), date(2026, 1, 1), date(2026, 1, 1), "zh-TW", "synthetic account",
            lambda: True, lambda: "synthetic account",
        )
    assert caught.value is original
    assert base.exception_error_id(original, bank="esun") == "ESUN-05-728-001"
    context = BaseException.__dict__["__context__"].__get__(original, BaseException)
    assert isinstance(context, ValueError)
    assert base.exception_error_id(context, bank="esun") == "ESUN-05-784-004"
    assert trace == [
        ("evaluate",), ("evaluate",), ("locator", query.ROOT), ("nested", query.FORM),
        ("locator", 'dialog:visible,[role="dialog"]:visible'),
        ("count", 'dialog:visible,[role="dialog"]:visible'),
        ("count", 'dialog:visible,[role="dialog"]:visible'),
        ("locator", "#layout-content"), ("count", "#layout-content"),
        ("locator", ".ctw01002"), ("count", ".ctw01002"),
        ("count", query.ROOT), ("visible", query.ROOT), ("wait", 50),
    ]


@pytest.mark.parametrize("kind", ["failure", "collect"])
@pytest.mark.parametrize("error_id, expected", [
    ("SYS-08-001-002", "[SYS-08-001-002] 原生金額精度驗證：金額不可使用布林值"),
    ("SYS-09-001-001", "[SYS-09-001-001] 同步排隊：等待工作程序執行逾時"),
])
def test_shared_namespace_collision_never_changes_action_reason(kind, error_id, expected):
    from backend.core import sync_errors

    crawler = object.__new__(CtbcCrawler)
    crawler.name = "ctbc"
    payload = {"exception": "ValueError", "error_id": error_id}
    if kind == "collect":
        payload["code"] = "collect_contract"
        validate = base._validated_collect_diagnostics
        carrier = sync_errors.CollectDiagnosticError
    else:
        validate = base._validated_failure_diagnostics
        carrier = sync_errors.FailureDiagnosticError
    diagnostics = validate(crawler, payload)
    assert diagnostics["error_id"] == error_id
    rendered = sync_errors.safe_failure_message(carrier(crawler, diagnostics), "ctbc")
    assert rendered.startswith(expected)


def test_collect_sink_binds_shared_validation_code_to_its_bank():
    crawler = object.__new__(CtbcCrawler)
    crawler.name = "ctbc"
    error = with_error_code(ValueError("synthetic private date"), "SYS-07-980-002")
    diagnostic = base._collect_failure_diagnostics(crawler, error)
    assert diagnostic["error_id"] == "CTBC-07-980-002"
    assert base._format_collect_diagnostics(diagnostic).startswith("[CTBC-07-980-002]")


def test_generic_failure_validator_binds_registered_shared_template():
    crawler = object.__new__(CtbcCrawler)
    crawler.name = "ctbc"
    diagnostic = base._validated_failure_diagnostics(crawler, {
        "exception": "RuntimeError", "error_id": "SYS-03-972-001",
    })
    assert diagnostic == {"exception": "RuntimeError", "error_id": "CTBC-03-972-001"}
    assert base._validated_failure_diagnostics(crawler, {
        "exception": "RuntimeError", "error_id": "HSBC-03-972-001",
    }) == {}


def test_collect_sink_retains_registered_valueerror_reason():
    crawler = object.__new__(CtbcCrawler)
    crawler.name = "ctbc"
    error = with_error_code(ValueError("synthetic private payload"), "CTBC-04-010-001")
    diagnostic = base._collect_failure_diagnostics(crawler, error)
    assert diagnostic["error_id"] == "CTBC-04-010-001"
    rendered = base._format_collect_diagnostics(diagnostic)
    assert rendered.startswith("[CTBC-04-010-001]")
    assert "未取得原生回應" in rendered
    assert "synthetic private" not in rendered


def test_real_run_preserves_login_action_code(monkeypatch, capsys):
    crawler = object.__new__(CtbcCrawler)
    crawler.name = "ctbc"
    page = SimpleNamespace(url=BASE, frames=[], on=Mock(), remove_listener=Mock())
    monkeypatch.setattr(crawler, "_enforce_session_freshness", lambda: None)
    monkeypatch.setattr(crawler, "_make_collector", lambda _: base.ResponseCollector())
    monkeypatch.setattr(crawler, "_build_fetch_kwargs", lambda: {})
    monkeypatch.setattr(crawler, "_execute_browser_flow", lambda _url, **kw: kw["page_action"](page))
    original = with_error_code(ValueError("synthetic private field"), "CTBC-02-900-001")

    def fail(_page):
        raise original

    monkeypatch.setattr(crawler, "_shared_login", fail)
    unused_collect = Mock(side_effect=AssertionError("no collect after login failure"))
    monkeypatch.setattr(crawler, "collect", unused_collect)
    result = crawler.run(login_url=BASE, headless=True)
    assert result["failure_diagnostics"]["error_id"] == "CTBC-02-900-001"
    assert result["error"].startswith("[CTBC-02-900-001]")
    assert "synthetic private" not in repr(result)
    assert "synthetic private" not in capsys.readouterr().err
    unused_collect.assert_not_called()


@pytest.mark.parametrize("boundary, expected", [
    ("make", "CTBC-01-920-001"),
    ("attach", "CTBC-01-930-001"),
    ("dialog", "CTBC-01-940-001"),
])
def test_setup_boundaries_have_distinct_codes(monkeypatch, boundary, expected):
    crawler = object.__new__(CtbcCrawler)
    crawler.name = "ctbc"
    page = SimpleNamespace(url=BASE, frames=[], on=Mock(), remove_listener=Mock())
    collector = SimpleNamespace(attach=Mock(), detach=Mock())
    original = ValueError("synthetic private setup")
    make = Mock(return_value=collector)
    dialog = Mock()
    {"make": make, "attach": collector.attach, "dialog": dialog}[boundary].side_effect = original
    monkeypatch.setattr(crawler, "_enforce_session_freshness", lambda: None)
    monkeypatch.setattr(crawler, "_make_collector", make)
    monkeypatch.setattr(crawler, "attach_shared_dialog_handler", dialog)
    monkeypatch.setattr(crawler, "_build_fetch_kwargs", lambda: {})
    monkeypatch.setattr(crawler, "_execute_browser_flow", lambda _url, **kw: kw["page_action"](page))
    result = crawler.run(login_url=BASE, headless=True)
    assert result["failure_diagnostics"]["error_id"] == expected
    assert result["error"].startswith(f"[{expected}]")
    assert "synthetic private" not in repr(result)


@pytest.mark.parametrize("origins, rules, expected", [
    ([False], (), "CTBC-01-960-001"),
    ([True, False], (), "CTBC-01-960-002"),
    ([True, True], (SimpleNamespace(name="same", bank="ctbc"), SimpleNamespace(name="same", bank="ctbc")), "CTBC-03-950-001"),
    ([True, True], (SimpleNamespace(name="synthetic", bank="hsbc"),), "CTBC-03-950-002"),
])
def test_shared_login_distinguishes_origin_and_rule_rejections(monkeypatch, origins, rules, expected):
    crawler = object.__new__(CtbcCrawler)
    crawler.name = "ctbc"
    decisions = iter(origins)
    monkeypatch.setattr(crawler, "_credential_origin_allowed", lambda _: next(decisions))
    monkeypatch.setattr(crawler, "prepare_login_page", Mock())
    monkeypatch.setattr(crawler, "login_checkpoint_rules", lambda: rules)
    with pytest.raises(base.LoginCheckpointBlocked) as caught:
        crawler._shared_login(SimpleNamespace())
    assert base.exception_error_id(caught.value, bank="ctbc") == expected


@pytest.mark.parametrize("mode, origins, expected", [
    ("dialog_before", [True, True], "CTBC-03-940-001"),
    ("origin_before", [True, True, False], "CTBC-03-940-002"),
    ("origin_after", [True, True, True, False], "CTBC-03-940-003"),
    ("dialog_after", [True, True, True, True], "CTBC-03-940-004"),
    ("prepare_error", [True], "CTBC-01-950-001"),
    ("inspect_error", [True, True, True], "CTBC-03-920-001"),
])
def test_shared_login_iteration_boundaries_preserve_action(monkeypatch, mode, origins, expected):
    crawler = object.__new__(CtbcCrawler)
    crawler.name = "ctbc"
    crawler._shared_dialog_blocked = mode == "dialog_before"
    decisions = iter(origins)
    monkeypatch.setattr(crawler, "_credential_origin_allowed", lambda _: next(decisions))
    prepare = Mock(side_effect=ValueError("synthetic private prepare") if mode == "prepare_error" else None)
    monkeypatch.setattr(crawler, "prepare_login_page", prepare)
    monkeypatch.setattr(crawler, "login_checkpoint_rules", lambda: ())

    def inspect(*args, **kwargs):
        if mode == "inspect_error":
            raise ValueError("synthetic private inspect")
        if mode == "dialog_after":
            crawler._shared_dialog_blocked = True
        return base.CheckpointOutcome(base.CheckpointKind.READY_FOR_CREDENTIALS)

    monkeypatch.setattr(base, "evaluate_login_checkpoint", inspect)
    with pytest.raises(Exception) as caught:
        crawler._shared_login(SimpleNamespace())
    assert base.exception_error_id(caught.value, bank="ctbc") == expected


def _offline_run_crawler(monkeypatch, *, login=True, payload=None):
    crawler = object.__new__(CtbcCrawler)
    crawler.name = "ctbc"
    page = SimpleNamespace(url=BASE, frames=[], on=Mock(), remove_listener=Mock())
    monkeypatch.setattr(crawler, "_enforce_session_freshness", lambda: None)
    monkeypatch.setattr(crawler, "_make_collector", lambda _: base.ResponseCollector())
    monkeypatch.setattr(crawler, "_build_fetch_kwargs", lambda: {})
    monkeypatch.setattr(crawler, "_execute_browser_flow", lambda _url, **kw: kw["page_action"](page))
    monkeypatch.setattr(crawler, "_shared_login", lambda _: login)
    monkeypatch.setattr(crawler, "_ensure_collect_origin", lambda _: None)
    monkeypatch.setattr(crawler, "_validate_collect_publication", lambda _: None)
    monkeypatch.setattr(crawler, "logout", lambda _: None)
    monkeypatch.setattr(crawler, "collect", lambda *_: payload)
    return crawler


def test_false_login_has_explicit_action_code(monkeypatch):
    crawler = _offline_run_crawler(monkeypatch, login=False)
    result = crawler.run(login_url=BASE)
    assert result["failure_diagnostics"]["error_id"] == "CTBC-03-910-001"


@pytest.mark.parametrize("payload, expected", [
    (object(), "CTBC-07-900-001"),
    (base.BankCollectResult(bank="ctbc", error="synthetic private native message"), "CTBC-07-900-002"),
    (base.BankCollectResult(bank="ctbc"), "CTBC-07-900-003"),
])
def test_shared_result_rejections_have_distinct_codes(monkeypatch, payload, expected):
    crawler = _offline_run_crawler(monkeypatch, payload=payload)
    result = crawler.run(login_url=BASE)
    assert result["collect_diagnostics"]["error_id"] == expected
    assert result["error"].startswith(f"[{expected}]")
    assert "synthetic private" not in repr(result)
