"""Offline E.SUN collect failure → sync-job error, with no financial writes."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.banks.esun import BASE, EsunCrawler
from backend.core import base, persist, store
from backend.server import card_events, rules_repo, sync_jobs_repo, sync_runner as runner


class HostileValue:
    def __str__(self):
        raise AssertionError("must not stringify diagnostic value")

    def __eq__(self, other):
        raise AssertionError("must not compare diagnostic value")

    def __bool__(self):
        raise AssertionError("must not truth-test diagnostic value")


def _sink(monkeypatch, crawler):
    failures = []
    fake_store = SimpleNamespace(
        latest_twd_transaction_dates=lambda: {},
        latest_card_transaction_dates=lambda: {},
        close=Mock(),
        stats=Mock(side_effect=AssertionError("no stats after failure")),
    )
    monkeypatch.setattr(store, "BankStore", lambda *args, **kw: fake_store)
    monkeypatch.setattr(rules_repo, "list_rules", lambda **kw: [])
    monkeypatch.setattr(runner, "_load_crawler", lambda bank: (SimpleNamespace(BASE=BASE), lambda: crawler))
    monkeypatch.setattr(runner, "get_job", lambda job_id: {
        "user_id": 1, "bank": "esun", "account_id": None, "batch_id": None,
        "history_mode": "full",
    })
    monkeypatch.setattr(sync_jobs_repo, "claim_queued", lambda job_id: True)
    monkeypatch.setattr(sync_jobs_repo, "mark_failed", lambda job_id, error: failures.append((job_id, error)))
    monkeypatch.setattr(sync_jobs_repo, "mark_done", Mock(side_effect=AssertionError("not done")))
    monkeypatch.setattr(card_events, "snapshot_cards", lambda **kw: [])
    monkeypatch.setattr(runner, "_send_sync_notification", lambda **kw: None)
    monkeypatch.setattr(base, "validate_history_coverage", Mock(side_effect=AssertionError("no coverage after failure")))
    monkeypatch.setattr(persist, "persist_collected", Mock(side_effect=AssertionError("no financial persist")))
    return failures, fake_store


@pytest.mark.parametrize("repeat", [False, True])
def test_real_esun_collect_diagnostic_survives_to_stored_job(monkeypatch, tmp_path, capsys, repeat):
    crawler = object.__new__(EsunCrawler)
    crawler.name = "esun"
    crawler.transaction_cursors = {}
    page = SimpleNamespace(
        url=BASE, frames=[], evaluate=Mock(return_value=True),
        wait_for_timeout=Mock(), on=Mock(), remove_listener=Mock(),
    )
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(crawler, "_make_collector", lambda _: base.ResponseCollector())
    monkeypatch.setattr(crawler, "_enforce_session_freshness", lambda: None)
    monkeypatch.setattr(crawler, "_build_fetch_kwargs", lambda: {})
    monkeypatch.setattr(crawler, "_shared_login", lambda _: True)
    monkeypatch.setattr(crawler, "logout", Mock(return_value=True))
    monkeypatch.setattr(crawler, "_navigate_menu", Mock(side_effect=AssertionError("no legacy navigation")))
    def execute(_url, **kw):
        kw["page_action"](page)
        if repeat:
            kw["page_action"](page)

    monkeypatch.setattr(crawler, "_execute_browser_flow", execute)
    returned = []
    actual_run = crawler.run

    def run(**kwargs):
        result = actual_run(**kwargs)
        returned.append(result)
        return result

    monkeypatch.setattr(crawler, "run", run)
    failures, fake_store = _sink(monkeypatch, crawler)

    assert runner._exec_sync(17) is True
    expected = ("sync_failed:RuntimeError" if repeat else
                "collect_failed: ValueError: code=collect_adapter: phase=spa_entry")
    assert failures == [(17, expected)]
    if repeat:
        assert returned == [{"error": "browser_callback_repeated"}]
    crawler._navigate_menu.assert_not_called()
    fake_store.close.assert_called_once()
    assert "collect_failed:" in capsys.readouterr().err


@pytest.mark.parametrize("diagnostic, expected", [
    ({"exception": "ValueError", "code": "private_account_1234", "phase": "spa_entry"},
     "sync_failed:RuntimeError"),
    ({"exception": "ValueError", "code": "collect_adapter", "phase": "private_account_1234"},
     "collect_failed: ValueError: code=collect_adapter"),
    ({"exception": "ValueError", "code": "collect_adapter", "phase": ["spa_entry"]},
     "collect_failed: ValueError: code=collect_adapter"),
    ({"exception": "ValueError", "code": "collect_adapter", "phase": "spa_entry", "raw": "private_account_1234"},
     "sync_failed:RuntimeError"),
    ({"exception": "ValueError", "code": "collect_adapter", "guard": "private_account_1234"},
     "collect_failed: ValueError: code=collect_adapter"),
    ({"exception": "ValueError", "code": "collect_adapter", "phase": HostileValue()},
     "collect_failed: ValueError: code=collect_adapter"),
    ({"exception": HostileValue(), "code": "collect_adapter"},
     "sync_failed:RuntimeError"),
    ("private_account_1234", "sync_failed:RuntimeError"),
], ids=["unknown-code", "unknown-phase", "wrong-type", "extra-field", "unknown-guard", "hostile-phase", "hostile-exception", "not-dict"])
def test_unknown_result_metadata_never_exposes_private_values(monkeypatch, diagnostic, expected):
    crawler = SimpleNamespace(
        configure_transaction_cursor=lambda *args: None,
        run=lambda **kw: {"error": "collect_failed: private_account_1234", "collect_diagnostics": diagnostic},
    )
    failures, fake_store = _sink(monkeypatch, crawler)

    assert runner._exec_sync(18) is True
    assert failures == [(18, expected)]
    assert "private_account_1234" not in failures[0][1]
    fake_store.close.assert_called_once()


def test_store_close_failure_does_not_mask_collect_diagnostic(monkeypatch):
    crawler = object.__new__(EsunCrawler)
    monkeypatch.setattr(crawler, "configure_transaction_cursor", lambda *args: None)
    monkeypatch.setattr(crawler, "run", lambda **kw: {
        "error": "collect_failed: private_account_1234",
        "collect_diagnostics": {
            "exception": "ValueError", "code": "collect_adapter", "phase": "spa_entry",
        },
    })
    failures, fake_store = _sink(monkeypatch, crawler)
    fake_store.close.side_effect = RuntimeError("private_account_1234")

    assert runner._exec_sync(20) is True
    assert failures == [(20, "collect_failed: ValueError: code=collect_adapter: phase=spa_entry")]
    fake_store.close.assert_called_once()


@pytest.mark.parametrize("failure, family", [
    (ValueError("synthetic private persistence payload"), "ValueError"),
    (type("synthetic_private_exception_name", (Exception,), {})("private text"), "Exception"),
])
@pytest.mark.parametrize("close_fails", [False, True])
def test_persistence_failure_has_fresh_stage_at_job_sink(monkeypatch, capsys, failure, family, close_fails):
    crawler = object.__new__(EsunCrawler)
    crawler._esun_spa_phase = "transaction_render"
    crawler._esun_spa_gate = "blocker_allowed"
    monkeypatch.setattr(crawler, "configure_transaction_cursor", lambda *args: None)
    monkeypatch.setattr(crawler, "run", lambda **kw: {"data": {}})
    failures, fake_store = _sink(monkeypatch, crawler)
    monkeypatch.setattr(base, "validate_history_coverage", Mock(return_value={}))
    failed_persist = Mock(side_effect=failure)
    monkeypatch.setattr(persist, "persist_collected", failed_persist)
    if close_fails:
        fake_store.close.side_effect = RuntimeError("synthetic private close payload")

    assert runner._exec_sync(21) is True
    expected = f"sync_failed:{family}: phase=persistence"
    assert failures == [(21, expected)]
    failed_persist.assert_called_once()
    fake_store.stats.assert_not_called()
    fake_store.close.assert_called_once()
    stderr = capsys.readouterr().err
    assert expected in stderr
    assert "private" not in stderr
    assert "gate=" not in stderr and "guard=" not in stderr
    assert crawler._esun_spa_phase == "transaction_render"


@pytest.mark.parametrize("boundary", ["crawler", "coverage", "stats", "close"])
def test_sibling_failure_never_inherits_persistence_stage(monkeypatch, capsys, boundary):
    failure = type("synthetic_private_exception_name", (Exception,), {})("private text")
    crawler = object.__new__(EsunCrawler)
    monkeypatch.setattr(crawler, "configure_transaction_cursor", lambda *args: None)
    monkeypatch.setattr(crawler, "run", Mock(return_value={"data": {}}))
    failures, fake_store = _sink(monkeypatch, crawler)
    coverage = Mock(return_value={})
    monkeypatch.setattr(base, "validate_history_coverage", coverage)
    successful_persist = Mock(return_value={})
    monkeypatch.setattr(persist, "persist_collected", successful_persist)
    fake_store.stats.side_effect = None
    fake_store.stats.return_value = {}
    {"crawler": crawler.run, "coverage": coverage,
     "stats": fake_store.stats, "close": fake_store.close}[boundary].side_effect = failure

    assert runner._exec_sync(22) is True
    assert failures == [(22, "sync_failed:Exception")]
    fake_store.close.assert_called_once()
    if boundary in {"crawler", "coverage"}:
        successful_persist.assert_not_called()
    else:
        successful_persist.assert_called_once()
    assert "phase=persistence" not in capsys.readouterr().err


def test_real_persist_rejection_retains_cause(monkeypatch):
    crawler = object.__new__(EsunCrawler)
    monkeypatch.setattr(crawler, "configure_transaction_cursor", lambda *args: None)
    monkeypatch.setattr(crawler, "run", lambda **kw: {"data": {}})
    real_persist = persist.persist_collected
    _, fake_store = _sink(monkeypatch, crawler)
    monkeypatch.setattr(base, "validate_history_coverage", Mock(return_value={}))
    monkeypatch.setattr(persist, "persist_collected", real_persist)
    with pytest.raises(runner._PersistenceError) as raised:
        runner._dispatch_crawler_and_persist("esun", 1)
    assert type(raised.value.__cause__) is ValueError
    assert str(raised.value) == "persistence_failed"
    fake_store.close.assert_called_once()


def test_job_sink_still_records_failure_when_classifier_import_is_unavailable(monkeypatch):
    import builtins

    failures, _ = _sink(monkeypatch, object.__new__(EsunCrawler))
    monkeypatch.setattr(runner, "_dispatch_crawler_and_persist", Mock(side_effect=RuntimeError("private")))
    original_import = builtins.__import__

    def unavailable(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "backend.core.base" and "_safe_exception_type" in fromlist:
            raise ImportError("synthetic private import detail")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", unavailable)
    assert runner._exec_sync(23) is True
    assert failures == [(23, "sync_failed:Exception")]


def test_persistence_carrier_without_cause_has_safe_fallback(monkeypatch, capsys):
    failures, _ = _sink(monkeypatch, object.__new__(EsunCrawler))
    monkeypatch.setattr(runner, "_dispatch_crawler_and_persist",
                        Mock(side_effect=runner._PersistenceError("synthetic private text")))
    assert runner._exec_sync(24) is True
    assert failures == [(24, "sync_failed:Exception: phase=persistence")]
    assert "private" not in capsys.readouterr().err


@pytest.mark.parametrize("boundary", ["persistence", "generic", "collect"])
def test_exception_mro_descriptor_is_never_executed(monkeypatch, capsys, boundary):
    import sys

    accesses = []

    class HostileMeta(type):
        @property
        def __mro__(cls):
            accesses.append(True)
            print("synthetic private stdout")
            print("synthetic private stderr", file=sys.stderr)
            return type.__dict__["__mro__"].__get__(cls, type(cls))

    class Failure(ValueError, metaclass=HostileMeta):
        pass

    failure = Failure("synthetic private message")
    crawler = object.__new__(EsunCrawler)
    failures, _ = _sink(monkeypatch, crawler)
    if boundary == "persistence":
        monkeypatch.setattr(crawler, "configure_transaction_cursor", lambda *args: None)
        monkeypatch.setattr(crawler, "run", lambda **kw: {"data": {}})
        monkeypatch.setattr(base, "validate_history_coverage", Mock(return_value={}))
        monkeypatch.setattr(persist, "persist_collected", Mock(side_effect=failure))
        assert runner._exec_sync(25) is True
        assert failures == [(25, "sync_failed:ValueError: phase=persistence")]
    elif boundary == "generic":
        monkeypatch.setattr(runner, "_dispatch_crawler_and_persist", Mock(side_effect=failure))
        assert runner._exec_sync(25) is True
        assert failures == [(25, "sync_failed:ValueError")]
    else:
        diagnostic = base._collect_failure_diagnostics(crawler, failure)
        assert diagnostic["exception"] == "ValueError"
        assert base._exception_inherits(failure, ValueError)
    output = capsys.readouterr()
    assert accesses == []
    assert "private" not in output.out + output.err


def test_forged_carrier_is_revalidated_at_sink(monkeypatch):
    crawler = object.__new__(EsunCrawler)
    failures, _ = _sink(monkeypatch, crawler)
    forged = runner._CollectDiagnosticError(crawler, {
        "exception": "ValueError", "code": "private_account_1234",
        "phase": "spa_entry",
    })
    monkeypatch.setattr(runner, "_dispatch_crawler_and_persist", Mock(side_effect=forged))

    assert runner._exec_sync(19) is True
    assert failures == [(19, "sync_failed:RuntimeError")]
