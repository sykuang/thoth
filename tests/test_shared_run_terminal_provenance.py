"""Offline native shared-run producers, joined through the real queued executor."""
from types import SimpleNamespace
from importlib import import_module
from pathlib import Path

import pytest

from backend.core import base
from backend.core.error_codes import exception_error_id, with_error_code
from backend.server import sync_runner as runner
from tests.test_esun_sync_diagnostic_propagation import _sink


REPEATED_MESSAGE = "[ESUN-07-930-001] 瀏覽器回呼：瀏覽器回呼重複執行 (RuntimeError)"


def _drive(monkeypatch, *, login=True, repeat=False, fault=None, reentrant=None,
           detach_fault=False, bank="esun", run=None, module=base, exception=None,
           direct=False, crawler_class=None, trace=None, native_collector=False, native_publication=False,
           native_dispatch=False, escaped=None, unwind_exception=None):
    """Fake browser boundary only; run, dispatch and executor remain production."""
    trace = [] if trace is None else trace
    errors = []
    callback = None
    page = SimpleNamespace(url="https://synthetic.invalid/", frames=[])
    class Crawler(module.BankCrawler):
        def login(self, raw):
            raise AssertionError("shared login owns this boundary")

        def collect(self, raw, collector):
            raise AssertionError("fake collection must be installed")

    crawler = object.__new__(crawler_class or Crawler)
    crawler.name = bank
    crawler.session_dir = Path("/synthetic-unused-session")
    crawler.transaction_cursors = {}
    failure = exception if exception is not None else ValueError("injected private native fault")

    def native(name):
        trace.append(name)
        if reentrant == name:
            callback(page)
        if fault == name or (detach_fault and name == "detach"):
            errors.append(failure)
            raise failure

    collector_base = (import_module("backend.banks.esun_spa.capture").SpaCollector
                      if native_publication else module.ResponseCollector)

    class Collector(collector_base):
        def attach(self, raw):
            native("attach")
            if native_collector:
                super().attach(raw)

        def detach(self, raw):
            native("detach")
            if native_collector:
                super().detach(raw)

    page.on = lambda event, handler: trace.append("on:" + event)

    def remove_listener(event, handler):
        trace.append("remove:" + event)
        if native_collector:
            errors.append(failure)
            raise failure

    page.remove_listener = remove_listener

    class Result(module.BankCollectResult):
        def to_dict(self):
            native("to_dict")
            return super().to_dict()

    def make(raw):
        native("make")
        collector = Collector()
        if native_publication:
            collector.publication_checks.append(lambda: native(
                "publication" if trace.count("publication") == 0 else "late_publication"))
        return collector

    def shared_login(raw):
        native("login")
        return login

    def collect(raw, collector):
        native("collect")
        return Result(card_bill_facts_ok=False)

    def publication(collector):
        if native_publication:
            import_module("backend.banks.esun").EsunCrawler._validate_collect_publication(crawler, collector)
        else:
            native("publication" if trace.count("publication") == 0 else "late_publication")

    def fetch(url, *, page_action, **kwargs):
        nonlocal callback
        callback = page_action
        if native_dispatch:
            native("browser_start")
        assert page_action(page) is page
        if repeat:
            assert page_action(page) is page
        if native_dispatch:
            native("browser_unwind")
            if unwind_exception is not None:
                errors.append(unwind_exception)
                raise unwind_exception

    monkeypatch.setattr(crawler, "_enforce_session_freshness", lambda: None)
    monkeypatch.setattr(crawler, "_build_fetch_kwargs", lambda: {"__cleanups__": [lambda: native("cleanup")]})
    monkeypatch.setattr(crawler, "_make_collector", make)
    monkeypatch.setattr(crawler, "attach_shared_dialog_handler", lambda raw: native("dialog"))
    monkeypatch.setattr(crawler, "_shared_login", shared_login)
    monkeypatch.setattr(crawler, "_ensure_collect_origin", lambda raw: native("origin"))
    monkeypatch.setattr(crawler, "_credential_origin_allowed", lambda raw: True)
    monkeypatch.setattr(crawler, "collect", collect)
    monkeypatch.setattr(crawler, "_validate_collect_publication", publication)
    monkeypatch.setattr(crawler, "logout", lambda raw: native("logout"))
    if native_dispatch:
        monkeypatch.setattr(module.StealthyFetcher, "fetch", fetch)
        if type(bank) is str and bank in {"fubon", "rakuten"} and crawler_class is not None:
            # Session-based adapters keep their real dispatch; fake only the SDK.
            for method in ("set_default_navigation_timeout", "set_default_timeout",
                           "set_extra_http_headers", "goto", "wait_for_load_state",
                           "wait_for_timeout", "close"):
                setattr(page, method, lambda *args, **kwargs: None)

            class Session:
                context = SimpleNamespace(new_page=lambda: page)

                def __init__(self, **kwargs):
                    pass

                def __enter__(self):
                    native("browser_start")
                    return self

                def __exit__(self, *args):
                    native("browser_unwind")
                    return False

            monkeypatch.setattr(import_module(crawler_class.__module__), "StealthySession", Session)
    else:
        monkeypatch.setattr(crawler, "_execute_browser_flow", fetch)
    returned = []

    def actual_run(**kwargs):
        try:
            result = (run or module.BankCrawler.run)(crawler, **kwargs)
        except BaseException as error:
            if escaped is not None:
                escaped.append(error)
            raise
        returned.append(result)
        return result

    monkeypatch.setattr(crawler, "run", actual_run)
    if direct:
        result = actual_run(login_url=page.url, headless=True)
        return crawler, result, trace, errors, []
    failures, store = _sink(monkeypatch, crawler)
    if type(bank) is str and bank in base._ERROR_BANKS:
        monkeypatch.setattr(runner, "get_job", lambda job_id: {
            "user_id": 1, "bank": bank, "account_id": None, "batch_id": None, "history_mode": "full",
        })
    assert runner._exec_sync(31) is True
    store.close.assert_called_once()
    if native_dispatch and not returned:
        import_module("backend.core.persist").persist_collected.assert_not_called()
        store.stats.assert_not_called()
    return crawler, returned[0] if returned else None, trace, errors, failures


def test_natural_repeat_after_false_login_reaches_actual_executor(monkeypatch):
    crawler, result, trace, errors, failures = _drive(monkeypatch, login=False, repeat=True)
    assert failures == [(31, REPEATED_MESSAGE)]
    assert result == {"error": "browser_callback_repeated", "failure_diagnostics": {
        "exception": "RuntimeError", "error_id": "ESUN-07-930-001",
    }}
    assert trace == ["make", "attach", "dialog", "login", "detach", "cleanup"]
    assert errors == []


def test_injected_to_dict_failure_has_native_producer_at_executor(monkeypatch):
    failure = ValueError("injected private native fault")
    _, result, trace, errors, failures = _drive(monkeypatch, fault="to_dict", exception=failure)
    expected = "[ESUN-07-910-001] 結果序列化：結果轉換失敗 | collect_failed: ValueError: code=collect_contract"
    assert failures == [(31, expected)]
    assert result == {"error": expected, "collect_diagnostics": {
        "exception": "ValueError", "code": "collect_contract", "error_id": "ESUN-07-910-001",
    }}
    assert errors == [failure]
    assert trace == ["make", "attach", "dialog", "login", "origin", "collect", "origin", "to_dict", "logout", "detach", "cleanup"]


@pytest.mark.parametrize("boundary", ["publication", "late_publication"])
def test_injected_publication_failure_has_native_producer_at_executor(monkeypatch, boundary):
    _, result, trace, errors, failures = _drive(monkeypatch, fault=boundary)
    expected = "[ESUN-07-920-001] 結果發佈：發佈證據檢查失敗 | collect_failed: ValueError: code=collect_contract"
    assert failures == [(31, expected)]
    assert result == {"error": expected, "collect_diagnostics": {
        "exception": "ValueError", "code": "collect_contract", "error_id": "ESUN-07-920-001",
    }}
    expected_trace = ["make", "attach", "dialog", "login", "origin", "collect", "origin", "to_dict", "publication", "logout"]
    if boundary == "late_publication":
        expected_trace.append("late_publication")
    assert trace == [*expected_trace, "detach", "cleanup"]
    assert len(errors) == 1


def test_injected_terminal_detach_has_native_producer_at_executor(monkeypatch):
    failure = ValueError("injected private native fault")
    _, result, trace, errors, failures = _drive(monkeypatch, login=False, detach_fault=True, exception=failure)
    # False login is primary: cleanup must not replace its first error.
    assert failures == [(31, "[ESUN-03-910-001] 登入結果：登入方法未回報成功 (RuntimeError)")]
    assert result == {"error": "[ESUN-03-910-001] 登入結果：登入方法未回報成功 (RuntimeError)",
                      "failure_diagnostics": {"exception": "RuntimeError", "error_id": "ESUN-03-910-001"}}
    assert trace == ["make", "attach", "dialog", "login", "detach", "cleanup"]
    assert errors == [failure]
    _, result, trace, errors, failures = _drive(monkeypatch, detach_fault=True)
    assert failures == [(31, "[ESUN-07-940-001] 瀏覽器收尾：回應觀察器解除失敗 (ValueError)")]
    assert result == {"error": "ValueError: browser cleanup failed", "failure_diagnostics": {
        "exception": "ValueError", "error_id": "ESUN-07-940-001",
    }}
    assert trace == ["make", "attach", "dialog", "login", "origin", "collect", "origin", "to_dict", "publication", "logout", "late_publication", "detach", "cleanup"]
    assert len(errors) == 1


@pytest.mark.parametrize("fault", [None, "collect", "to_dict", "publication", "late_publication", "detach"])
def test_repeat_replaces_prior_collect_success_and_cleanup_carriers(monkeypatch, fault):
    _, result, trace, errors, failures = _drive(monkeypatch, fault=fault, repeat=True)
    assert result == {"error": "browser_callback_repeated", "failure_diagnostics": {
        "exception": "RuntimeError", "error_id": "ESUN-07-930-001",
    }}
    assert failures == [(31, REPEATED_MESSAGE)]
    assert trace.count("login") == trace.count("collect") == trace.count("detach") == 1
    assert trace[-1] == "cleanup"
    assert len(errors) == int(fault is not None)


@pytest.mark.parametrize("timing", ["collect", "to_dict", "publication", "late_publication", "logout", "detach"])
@pytest.mark.parametrize("reject", [False, True])
def test_reentrant_repeat_stays_primary_even_if_native_method_then_raises(monkeypatch, timing, reject):
    _, result, trace, errors, failures = _drive(monkeypatch, reentrant=timing, fault=timing if reject else None)
    assert result == {"error": "browser_callback_repeated", "failure_diagnostics": {
        "exception": "RuntimeError", "error_id": "ESUN-07-930-001",
    }}
    assert failures == [(31, REPEATED_MESSAGE)]
    assert trace.count("login") == trace.count("collect") == trace.count("detach") == 1
    assert trace.count(timing) == 1
    assert len(errors) == int(reject)


@pytest.mark.parametrize("primary", ["collect", "to_dict", "publication", "late_publication"])
def test_detach_never_replaces_primary_collect_diagnostics(monkeypatch, primary):
    _, first, first_trace, _, first_failures = _drive(monkeypatch, fault=primary)
    _, second, second_trace, errors, second_failures = _drive(monkeypatch, fault=primary, detach_fault=True)
    assert first == second
    assert first_failures == second_failures
    assert first_trace == second_trace
    assert len(errors) == 2


@pytest.mark.parametrize("boundary", ["to_dict", "publication", "late_publication", "detach"])
@pytest.mark.parametrize("tag", ["direct", "cause", "context"])
def test_native_specific_leaf_and_original_exception_links_survive(monkeypatch, boundary, tag):
    leaf = with_error_code(TypeError("injected private leaf"), "ESUN-05-867-001")
    failure = leaf if tag == "direct" else ValueError("injected private outer")
    if tag == "cause":
        failure.__cause__ = leaf
    elif tag == "context":
        failure.__context__ = leaf
    links = (failure.__cause__, failure.__context__, failure.__suppress_context__)
    _, result, _, errors, failures = _drive(monkeypatch, fault=boundary, exception=failure)
    carrier = result.get("collect_diagnostics", result.get("failure_diagnostics"))
    # error_step's existing first-code rule keeps a directly tagged native
    # exception, and explicit causes outrank the shared wrapper. A shared direct
    # ID intentionally outranks an implicit handled context.
    expected_id = ("ESUN-07-940-001" if boundary == "detach" else
                   "ESUN-07-910-001" if boundary == "to_dict" else "ESUN-07-920-001") if tag == "context" else "ESUN-05-867-001"
    assert carrier["error_id"] == expected_id
    assert exception_error_id(failure, bank="esun") == expected_id
    assert errors == [failure]
    assert (failure.__cause__, failure.__context__, failure.__suppress_context__) == links
    assert failures[0][1] == result["error"] if boundary != "detach" else failures[0][1] == (
        "[ESUN-07-940-001] 瀏覽器收尾：回應觀察器解除失敗 (ValueError)" if tag == "context" else
        "[ESUN-05-867-001] 傳統臺幣查詢：帳戶選單不存在 (TypeError)" if tag == "direct" else
        "[ESUN-05-867-001] 傳統臺幣查詢：帳戶選單不存在 (ValueError)"
    )


BANK_CLASSES = {
    "cathay": "CathayCrawler", "ctbc": "CtbcCrawler", "dbs": "DbsCrawler", "esun": "EsunCrawler",
    "fubon": "FubonCrawler", "hsbc": "HsbcCrawler", "linebank": "LinebankCrawler", "rakuten": "RakutenCrawler",
    "scb": "ScbCrawler", "scsb": "ScsbCrawler", "sinopac": "SinopacCrawler", "taishin": "TaishinCrawler", "ubot": "UbotCrawler",
}


@pytest.mark.parametrize("bank", sorted(BANK_CLASSES))
@pytest.mark.parametrize("boundary, suffix", [("to_dict", "07-910-001"), ("publication", "07-920-001"), ("late_publication", "07-920-001"), ("detach", "07-940-001"), ("repeat", "07-930-001")])
def test_all_bank_classes_bind_shared_run_producers(monkeypatch, bank, boundary, suffix):
    cls = getattr(import_module("backend.banks." + bank), BANK_CLASSES[bank])
    assert cls.run is base.BankCrawler.run
    _, result, trace, _, failures = _drive(monkeypatch, bank=bank, crawler_class=cls,
        repeat=boundary == "repeat", fault=None if boundary == "repeat" else boundary)
    carrier = result.get("collect_diagnostics", result.get("failure_diagnostics"))
    assert carrier["error_id"] == bank.upper() + "-" + suffix
    copy = {"to_dict": "結果序列化：結果轉換失敗", "publication": "結果發佈：發佈證據檢查失敗",
            "late_publication": "結果發佈：發佈證據檢查失敗", "detach": "瀏覽器收尾：回應觀察器解除失敗",
            "repeat": "瀏覽器回呼：瀏覽器回呼重複執行"}[boundary]
    expected = f"[{bank.upper()}-{suffix}] {copy}"
    expected += " (RuntimeError)" if boundary == "repeat" else " (ValueError)" if boundary == "detach" else " | collect_failed: ValueError: code=collect_contract"
    assert failures == [(31, expected)]
    assert trace.count("login") == trace.count("collect") == trace.count("detach") == 1


class HostileName:
    def __str__(self):
        raise AssertionError("private bank name must not be formatted")

    def __eq__(self, other):
        raise AssertionError("private bank name must not be compared")

    def __hash__(self):
        raise AssertionError("private bank name must not be hashed")


@pytest.mark.parametrize("name", [None, "ESUN", "foreign", HostileName()], ids=["none", "uppercase", "foreign", "hostile"])
def test_repeat_clears_old_carrier_even_if_bank_binding_becomes_invalid(monkeypatch, name):
    # The ordinary false-login helper produces a real carrier, then the test
    # invalidates its bank binding before the natural second browser callback.
    original = base._failure_diagnostics

    def mutate_after_false_login(crawler, error, fallback):
        diagnostic = original(crawler, error, fallback)
        if fallback == "03-910-001":
            crawler.name = name
        return diagnostic

    monkeypatch.setattr(base, "_failure_diagnostics", mutate_after_false_login)
    _, result, trace, _, failures = _drive(monkeypatch, login=False, repeat=True)
    assert result == {"error": "browser_callback_repeated"}
    assert failures == [(31, "[ESUN-09-900-001] 同步工作：尚未分類的工作失敗 | sync_failed:RuntimeError")]
    assert trace == ["make", "attach", "dialog", "login", "detach", "cleanup"]


def test_actual_serializer_operand_failure_is_not_only_a_mocked_to_dict(monkeypatch):
    failure = OSError("injected serializer operand")
    original = base.BankCollectResult.to_dict
    calls = []

    class Operand:
        def __eq__(self, value):
            calls.append(value)
            raise failure

    def to_dict(result):
        result.final_url = Operand()
        return original(result)

    monkeypatch.setattr(base.BankCollectResult, "to_dict", to_dict)
    _, result, trace, _, failures = _drive(monkeypatch)
    expected = "[ESUN-07-910-001] 結果序列化：結果轉換失敗 | collect_failed: OSError: code=collect_contract"
    assert result == {"error": expected, "collect_diagnostics": {
        "exception": "OSError", "code": "collect_contract", "error_id": "ESUN-07-910-001",
    }}
    assert failures == [(31, expected)]
    assert calls == [[]]
    assert trace == ["make", "attach", "dialog", "login", "origin", "collect", "origin", "to_dict", "logout", "detach", "cleanup"]
    assert failure.args == ("injected serializer operand",)
    assert failure.__cause__ is failure.__context__ is None


def test_actual_response_collector_detach_native_failure_reaches_executor(monkeypatch):
    failure = OSError("injected native remove_listener")
    _, result, trace, errors, failures = _drive(monkeypatch, native_collector=True, exception=failure)
    assert result == {"error": "RuntimeError: browser cleanup failed", "failure_diagnostics": {
        "exception": "RuntimeError", "error_id": "ESUN-07-940-001",
    }}
    assert failures == [(31, "[ESUN-07-940-001] 瀏覽器收尾：回應觀察器解除失敗 (RuntimeError)")]
    assert trace == ["make", "attach", "on:request", "on:requestfailed", "on:response", "dialog", "login", "origin", "collect", "origin", "to_dict", "publication", "logout", "late_publication", "detach", "remove:request", "remove:requestfailed", "remove:response", "cleanup"]
    assert errors == [failure, failure, failure]
    assert failure.args == ("injected native remove_listener",)
    assert failure.__cause__ is failure.__context__ is None


@pytest.mark.parametrize("boundary", ["publication", "late_publication"])
def test_actual_esun_publication_proof_failure_reaches_shared_producer(monkeypatch, boundary):
    _, result, trace, errors, failures = _drive(monkeypatch, native_publication=True, fault=boundary)
    expected = "[ESUN-07-920-001] 結果發佈：發佈證據檢查失敗 | collect_failed: ValueError: code=collect_validation"
    assert result == {"error": expected, "collect_diagnostics": {
        "exception": "ValueError", "code": "collect_validation", "error_id": "ESUN-07-920-001",
    }}
    assert failures == [(31, expected)]
    assert trace.count("publication") == trace.count("logout") == trace.count("detach") == 1
    assert trace.count("late_publication") == int(boundary == "late_publication")
    assert len(errors) == 1


def test_native_browser_startup_reaches_actual_executor_without_callback(monkeypatch):
    failure = ValueError("injected private native startup")
    escaped = []
    _, result, trace, errors, failures = _drive(
        monkeypatch, native_dispatch=True, fault="browser_start", exception=failure, escaped=escaped)
    assert failures == [(31, "[ESUN-01-910-001] 登入頁面：瀏覽器啟動或導頁失敗 | sync_failed:ValueError")]
    assert result is None
    assert errors == escaped == [failure]
    assert trace == ["browser_start", "cleanup"]
    assert failure.args == ("injected private native startup",)
    assert failure.__cause__ is failure.__context__ is None


@pytest.mark.parametrize("bank", sorted(BANK_CLASSES))
@pytest.mark.parametrize("boundary", ["browser_start", "browser_unwind"])
def test_native_browser_dispatch_before_and_after_callback_all_banks(monkeypatch, bank, boundary):
    cls = getattr(import_module("backend.banks." + bank), BANK_CLASSES[bank])
    assert cls.run is base.BankCrawler.run
    escaped = []
    failure = OSError("private native dispatch failure")
    _, result, trace, errors, failures = _drive(
        monkeypatch, bank=bank, crawler_class=cls, native_dispatch=True,
        fault=boundary, exception=failure, escaped=escaped)
    prefix = (f"[{bank.upper()}-01-910-001] 登入頁面：瀏覽器啟動或導頁失敗" if boundary == "browser_start" else
              f"[{bank.upper()}-09-900-001] 同步工作：尚未分類的工作失敗")
    if bank == "fubon":
        prefix = "[FUBON-01-234-001] 登入頁準備：處理欄位或原生操作（extra_headers、load）失敗"
    assert failures == [(31, prefix + " | sync_failed:OSError")]
    assert result is None
    assert errors == escaped == [failure]
    assert trace.count("browser_start") == trace.count("cleanup") == 1
    assert trace.count("login") == trace.count("collect") == trace.count("detach") == int(boundary == "browser_unwind")
    assert failure.args == ("private native dispatch failure",)
    assert failure.__cause__ is failure.__context__ is None


@pytest.mark.parametrize("boundary", ["browser_start", "browser_unwind"])
@pytest.mark.parametrize("tag", ["direct", "cause", "context"])
@pytest.mark.parametrize("code", ["ESUN-05-867-001", "CTBC-01-920-001", "SYS-08-001-002"])
def test_browser_dispatch_keeps_original_specific_classifier_priority(monkeypatch, boundary, tag, code):
    from backend.core.sync_errors import safe_failure_message

    leaf = with_error_code(TypeError("private source leaf"), code)
    failure = leaf if tag == "direct" else ValueError("private outer dispatch failure")
    if tag == "cause":
        failure.__cause__ = leaf
    elif tag == "context":
        failure.__context__ = leaf
    links = (failure.__cause__, failure.__context__, failure.__suppress_context__)
    # The original dispatch wrapper tags the escaping exception with 09-900;
    # explicit causes beat it, while handled implicit contexts do not.
    oracle = leaf if tag == "direct" else ValueError(*failure.args)
    if tag == "cause":
        oracle.__cause__ = leaf
    elif tag == "context":
        oracle.__context__ = leaf
    with_error_code(oracle, "ESUN-09-900-001")
    expected = safe_failure_message(oracle, "esun")
    escaped = []
    _, result, _, errors, failures = _drive(monkeypatch, native_dispatch=True,
        fault=boundary, exception=failure, escaped=escaped)
    assert failures == [(31, expected)]
    assert result is None
    assert errors == escaped == [failure]
    assert (failure.__cause__, failure.__context__, failure.__suppress_context__) == links
    assert exception_error_id(failure) != "ESUN-01-910-001"


@pytest.mark.parametrize("name", [None, "ESUN", "foreign", HostileName()], ids=["none", "uppercase", "foreign", "hostile"])
def test_browser_startup_invalid_bank_never_manufactures_carrier(monkeypatch, name):
    escaped = []
    _, result, trace, errors, failures = _drive(monkeypatch, bank=name,
        native_dispatch=True, fault="browser_start", escaped=escaped)
    assert result is None
    assert trace == ["browser_start", "cleanup"]
    assert errors == escaped
    assert failures == [(31, "[ESUN-09-900-001] 同步工作：尚未分類的工作失敗 | sync_failed:ValueError")]
    assert exception_error_id(escaped[0]) == "ESUN-09-900-001"


@pytest.mark.parametrize("boundary", ["browser_start", "browser_unwind"])
@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("family", [KeyboardInterrupt, SystemExit, BaseException])
def test_fatal_browser_dispatch_keeps_identity_and_cleanup(monkeypatch, boundary, direct, family):
    failure = family("private fatal dispatch")
    escaped, trace = [], []
    with pytest.raises(family) as caught:
        _drive(monkeypatch, native_dispatch=True, fault=boundary, direct=direct,
               exception=failure, escaped=escaped, trace=trace)
    assert caught.value is failure
    assert escaped == [failure]
    assert trace.count("browser_start") == trace.count("cleanup") == 1
    assert trace.count("login") == trace.count("collect") == int(boundary == "browser_unwind")
    assert exception_error_id(failure) is None
    assert failure.__cause__ is failure.__context__ is None


@pytest.mark.parametrize("boundary", ["browser_start", "browser_unwind"])
@pytest.mark.parametrize("direct", [False, True])
def test_cancelled_browser_dispatch_is_not_converted_to_failure(monkeypatch, boundary, direct):
    from asyncio import CancelledError

    test_fatal_browser_dispatch_keeps_identity_and_cleanup(monkeypatch, boundary, direct, CancelledError)


@pytest.mark.parametrize("boundary", ["browser_start", "browser_unwind"])
def test_direct_browser_dispatch_bare_reraises_original_exception(monkeypatch, boundary):
    failure = ValueError("private direct startup")
    leaf = TypeError("untagged explicit cause")
    failure.__cause__ = leaf
    links = (failure.args, failure.__cause__, failure.__context__, failure.__suppress_context__)
    escaped, trace = [], []
    with pytest.raises(ValueError) as caught:
        _drive(monkeypatch, native_dispatch=True, fault=boundary, direct=True,
               exception=failure, escaped=escaped, trace=trace)
    assert caught.value is failure
    assert escaped == [failure]
    assert (failure.args, failure.__cause__, failure.__context__, failure.__suppress_context__) == links
    assert exception_error_id(failure) == ("ESUN-01-910-001" if boundary == "browser_start" else None)
    assert trace.count("cleanup") == 1


@pytest.mark.parametrize("primary", ["publication", "late_publication", "detach"])
def test_native_unwind_still_escapes_instead_of_republishing_callback_outcome(monkeypatch, primary):
    # Callback may already have rejected publication/cleanup. Native unwind is
    # still escaping, not transformed into a successful returned result.
    failure = OSError("private SDK teardown")
    escaped = []
    _, result, trace, _, failures = _drive(monkeypatch, native_dispatch=True,
        fault=primary, unwind_exception=failure, escaped=escaped)
    assert result is None
    assert escaped == [failure]
    assert failures == [(31, "[ESUN-09-900-001] 同步工作：尚未分類的工作失敗 | sync_failed:OSError")]
    assert trace.count(primary) == trace.count("cleanup") == 1


@pytest.mark.parametrize("bank", sorted(BANK_CLASSES))
def test_native_browser_success_keeps_single_callback_budget(monkeypatch, bank):
    cls = getattr(import_module("backend.banks." + bank), BANK_CLASSES[bank])
    _, result, trace, errors, failures = _drive(monkeypatch, bank=bank,
        crawler_class=cls, native_dispatch=True, direct=True)
    assert result == {"data": {"card_bill_facts_ok": False}}
    assert trace == ["browser_start", "make", "attach", "dialog", "login", "origin",
                     "collect", "origin", "to_dict", "publication", "logout",
                     "late_publication", "detach", "browser_unwind", "cleanup"]
    assert errors == failures == []
