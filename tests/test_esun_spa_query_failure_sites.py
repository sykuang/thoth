"""Synthetic native query failures through run → dispatch → stored job.

Only loopback browser traffic and an isolated SQLite job table; no bank DB/creds.
"""

import sqlite3
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

import pytest
from patchright.sync_api import JSHandle, Locator, TimeoutError

from backend.banks.esun_spa import query
from backend.server import sync_jobs_repo, sync_runner as runner
from tests.test_esun_spa_product import PATHS, browser as browser, product as product
from tests.test_esun_spa_completion import inventory_product as inventory_product
from tests.test_esun_sync_diagnostic_propagation import HostileValue, _sink


@pytest.fixture(autouse=True)
def synthetic_credentials_only(monkeypatch):
    monkeypatch.setattr("backend.core.creds._ENV_LOADED", True)


def form_diagnostic(result, error):
    """Keep exact site assertions independent of shared runtime metadata additions."""
    diagnostic = dict(result["collect_diagnostics"])
    suffix = ""
    for key, allowed in (
        ("stage", {"collect", "collect_transactions", "collect_navigation", "collect_validation"}),
        ("underlying_exception", {"Exception", "TimeoutError", "ValueError", "RuntimeError"}),
    ):
        if key in diagnostic:
            value = diagnostic.pop(key)
            assert type(value) is str and value in allowed
            suffix += f": {key}={value}"
    assert error == result["error"]
    assert error.endswith(suffix)
    return diagnostic, error.removesuffix(suffix)


@pytest.fixture
def stored_job(product, monkeypatch, tmp_path):
    crawler, _, origin, *_ = product
    real_get, real_claim, real_failed = runner.get_job, sync_jobs_repo.claim_queued, sync_jobs_repo.mark_failed
    _, store = _sink(monkeypatch, crawler)
    store.latest_twd_transaction_dates = lambda: {"0000000000001": date(2026, 9, 20)}
    monkeypatch.setattr(runner, "get_job", real_get)
    monkeypatch.setattr(sync_jobs_repo, "claim_queued", real_claim)
    monkeypatch.setattr(sync_jobs_repo, "mark_failed", real_failed)
    monkeypatch.setattr(runner, "_load_crawler", lambda _: (SimpleNamespace(BASE=origin + "/synthetic"), lambda: crawler))
    database = tmp_path / "synthetic-jobs.sqlite"

    @contextmanager
    def connection():
        conn = sqlite3.connect(database)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # The real job repository owns queue/claim/failure/readback; financial store is poison.
    with connection() as conn:
        conn.execute("""CREATE TABLE sync_jobs (
            id INTEGER PRIMARY KEY, user_id INTEGER, bank TEXT, account_id INTEGER,
            status TEXT, created_at TEXT, started_at TEXT, finished_at TEXT,
            error_msg TEXT, result_summary TEXT, batch_id INTEGER, history_mode TEXT)""")
    monkeypatch.setattr(sync_jobs_repo, "get_conn", connection)
    returned, escaped = [], []
    run, collect = crawler.run, crawler.collect

    def observe_run(**kwargs):
        result = run(**kwargs)
        returned.append(result)
        return result

    def observe_collect(*args):
        try:
            return collect(*args)
        except BaseException as error:
            escaped.append((error, error.__cause__, error.__context__))
            raise

    monkeypatch.setattr(crawler, "run", observe_run)
    monkeypatch.setattr(crawler, "collect", observe_collect)

    def execute(history_mode="incremental"):
        job_id = sync_jobs_repo.queue(user_id=1, bank="esun", history_mode=history_mode)
        assert runner._exec_sync(job_id) is True
        job = sync_jobs_repo.get(job_id)
        assert job is not None
        assert job["status"] == "failed" and job["result_summary"] is None
        store.close.assert_called_once()
        return returned[0], job["error_msg"], escaped

    return execute


def inject_fault(product, monkeypatch, fault):
    crawler, page, *_ = product
    original, native_click = query.query_twd, Locator.click
    observed = {"actions": 0, "submits": 0, "failures": []}

    def click(target, *args, **kwargs):
        in_query = crawler._esun_spa_phase == "transaction_form"
        if in_query:
            observed["actions"] += 1
        is_submit = in_query and target.evaluate("n => n.matches('button[type=submit]')")
        result = native_click(target, *args, **kwargs)
        if is_submit:
            observed["submits"] += 1
            if fault == "post_submit":
                page.locator('input[name=accountList]').evaluate("n => n.value='SYNTHETIC-DRIFT'")
        if in_query and fault == "readiness" and target.evaluate("n => n.matches('input[value=customized]')"):
            page.locator('.calendar-period-container').evaluate("n => n.remove()")
        if in_query and fault in ("calendar_state", "calendar_parse") and target.evaluate("n => n.matches('.calendar-base .combo-input-wrapper')"):
            page.locator('dialog[open] .header-label').evaluate("(n, parse) => {n.setAttribute('aria-label','SYNTHETIC-MISMATCH'); if(parse)n.textContent='SYNTHETIC-MISMATCH'}", fault == "calendar_parse")
        return result

    def run(*args, **kwargs):
        if fault == "cardinality":
            page.locator('form.search-helper-container').evaluate("n => n.after(n.cloneNode(true))")
        elif fault == "budget":
            kwargs["action_budget"][0] = 32
        elif fault == "owner":
            page.locator('form.search-helper-container').evaluate("n => n.insertAdjacentHTML('beforeend','<span class=selected-tag>SYNTHETIC</span>')")
        elif fault == "form_visibility":
            page.locator('form.search-helper-container').evaluate("n => n.style.visibility='hidden'")
        elif fault == "issuance":
            page.evaluate('''path => fetch(path, {method:'POST',
                headers:{'content-type':'application/json'},
                body:JSON.stringify({requestBody:{startIndex:101,count:100}})
            }).then(r=>r.json())''', PATHS[3])
        try:
            return original(*args, **kwargs)
        except BaseException as error:
            observed["failures"].append((error, error.__cause__, error.__context__))
            raise

    monkeypatch.setattr(query, "query_twd", run)
    monkeypatch.setattr(Locator, "click", click)
    return observed


@pytest.mark.parametrize("fault,gate,submits", [
    ("cardinality", "form_cardinality", 0),
    ("readiness", "calendar_readiness", 0),
    ("budget", "native_action_budget", 0),
    ("post_submit", "post_submit_validation", 1),
    ("owner", "form_owner", 0),
    ("calendar_state", "calendar_state", 0),
    ("calendar_parse", "calendar_state", 0),
    ("form_visibility", "form_owner", 0),
    ("issuance", "query_issuance_admission", 0),
])
def test_value_errors_are_distinct_at_stored_job(product, stored_job, monkeypatch, capsys, fault, gate, submits):
    crawler, _, _, hits, external, captured, _, logout, login_submit = product
    observed = inject_fault(product, monkeypatch, fault)
    result, error, escaped = stored_job()
    assert not external and not captured and "data" not in result
    assert [p for p, _ in hits] == list(PATHS[:2 + submits]) + ([PATHS[3]] if fault == "issuance" else [])
    assert observed["submits"] == submits
    assert len(observed["failures"]) == len(escaped) == 1
    original, cause, context = observed["failures"][0]
    assert escaped[0][0] is original
    assert original.__cause__ is cause and original.__context__ is context
    login_submit.assert_called_once()
    logout.assert_called_once()
    assert not crawler.collector.observers
    diagnostic, core_error = form_diagnostic(result, error)
    assert core_error == f"collect_failed: ValueError: code=collect_adapter: phase=transaction_form: gate={gate}"
    assert diagnostic == {
        "exception": "ValueError", "code": "collect_adapter", "phase": "transaction_form", "gate": gate,
    }
    assert error == result["error"]
    assert not any(text in repr(result) + error + capsys.readouterr().err for text in (
        "PRIVATE-", "SYNTHETIC-", "TEST-", "0000000000001", "Locator.click",
    ))


@pytest.mark.parametrize("fault,phase,gate,family", [
    ("transition", "transaction_form", "calendar_transition", "ValueError"),
    ("timeout", "transaction_form", "native_action", "TimeoutError"),
    ("menu_plan", "menu_plan", None, "ValueError"),
    ("guard", "transaction_form", "blocker_allowed", "ValueError"),
    ("cleanup", "transaction_form", "calendar_cleanup", "TimeoutError"),
])
def test_first_failure_survives_transition_cleanup(product, stored_job, monkeypatch, capsys, fault, phase, gate, family):
    from backend.banks.esun_spa import collection

    crawler, page, _, hits, external, captured, _, logout, login_submit = product
    click, evaluate, dispose = Locator.click, JSHandle.evaluate, JSHandle.dispose
    original_error, cleanup_calls = [], []
    cleanup_error = TimeoutError("SYNTHETIC-PRIVATE-CLEANUP")
    cleanup_error.__cause__ = LookupError("SYNTHETIC-PRIVATE-CAUSE")
    armed = False

    def month_click(target, *args, **kwargs):
        nonlocal armed
        is_month = crawler._esun_spa_phase == "transaction_form" and target.evaluate("n => n.matches('.month-item')")
        assert not cleanup_calls, "no native action after transition failure/cleanup"
        if is_month and fault == "timeout":
            target.evaluate("""n => {const cover=document.createElement('div');
                cover.style='position:fixed;inset:0;z-index:2147483647';
                n.closest('dialog').append(cover)}""")
            try:
                return click(target, *args, **dict(kwargs, timeout=100))
            except TimeoutError as error:
                original_error.append((error, error.__cause__, error.__context__))
                raise
        result = click(target, *args, **kwargs)
        if is_month:
            armed = True
            if fault == "menu_plan":
                owner = crawler.collector._latest_spa[PATHS[0]][1]
                crawler.collector._on_request_failed(owner._native_request)
            elif fault == "guard":
                page.evaluate("document.body.insertAdjacentHTML('beforeend','<div id=synthetic-blocker role=dialog>SYNTHETIC</div>')")
        return result

    def handle_evaluate(handle, expression, *args, **kwargs):
        if expression == "s => !!s.first && !s.first.isConnected" and fault == "transition":
            return False  # A transition that never retires its old grid.
        if expression == "s => s.observer.disconnect()":
            cleanup_calls.append("disconnect")
            evaluate(handle, expression, *args, **kwargs)
            if fault == "guard":
                page.locator('#synthetic-blocker').evaluate("n=>n.remove()")
            # A successful nested guard clears the live marker; the saved primary must survive.
            collection.navigation_guard(crawler, page, lambda: True)()
            if fault == "cleanup":
                original_error.append((cleanup_error, cleanup_error.__cause__, cleanup_error.__context__))
            raise cleanup_error
        return evaluate(handle, expression, *args, **kwargs)

    def handle_dispose(handle):
        if cleanup_calls == ["disconnect"]:
            cleanup_calls.append("dispose")
        return dispose(handle)

    monkeypatch.setattr(Locator, "click", month_click)
    monkeypatch.setattr(JSHandle, "evaluate", handle_evaluate)
    monkeypatch.setattr(JSHandle, "dispose", handle_dispose)
    result, error, escaped = stored_job()
    assert armed or fault == "timeout"
    assert cleanup_calls == ["disconnect", "dispose"]
    assert [p for p, _ in hits] == list(PATHS[:2])
    assert not captured and not external and "data" not in result
    assert len(escaped) == 1
    if original_error:
        original, cause, context = original_error[0]
        assert escaped[0][0] is original
        assert original.__cause__ is cause and original.__context__ is context
    else:
        assert escaped[0][0] is not cleanup_error
    code = "collect_navigation" if fault == "timeout" else "collect_contract" if fault == "cleanup" else "collect_adapter"
    expected = f"collect_failed: {family}: code={code}: phase={phase}" + (f": gate={gate}" if gate else "")
    assert form_diagnostic(result, error)[1] == expected
    login_submit.assert_called_once()
    logout.assert_called_once()
    assert not crawler.collector.observers
    assert "SYNTHETIC" not in error + capsys.readouterr().err


@pytest.mark.parametrize("empty", [False, True])
def test_second_window_uses_fresh_failure_site(inventory_product, stored_job, monkeypatch, empty):
    product, requests, inventory = inventory_product
    inventory[:] = inventory[:1]
    crawler, page, _, _, external, captured, state, _, login_submit = product
    if empty:
        state["page_mode"] = "empty_initial"
    original = query.query_twd
    invocations = []

    def second_failure(*args, **kwargs):
        invocations.append(kwargs["on_failure"])
        assert crawler._esun_spa_gate is None
        if len(invocations) == 2:
            page.locator('form.search-helper-container').evaluate("n => n.after(n.cloneNode(true))")
        result = original(*args, **kwargs)
        assert crawler._esun_spa_gate is None  # Successful query leaves no label.
        crawler._esun_spa_gate = "calendar_cleanup"  # Stale label from a prior operation.
        return result

    monkeypatch.setattr(query, "query_twd", second_failure)
    result, error, _ = stored_job(history_mode="full")
    assert len(invocations) == 2 and invocations[0] is not invocations[1]
    assert [p for p, _ in requests] == list(PATHS[1:3])
    assert result["collect_diagnostics"]["gate"] == "form_cardinality"
    assert form_diagnostic(result, error)[1].endswith("phase=transaction_form: gate=form_cardinality")
    assert not external and not captured and "data" not in result
    login_submit.assert_called_once()


def test_second_calendar_keeps_budget_site_after_successful_guards(product, stored_job, monkeypatch):
    crawler, page, _, hits, external, _, _, _, login_submit = product
    original, click = query.query_twd, Locator.click
    budget, days = [], []

    def retain_budget(*args, **kwargs):
        budget.append(kwargs["action_budget"])
        return original(*args, **kwargs)

    def day_click(target, *args, **kwargs):
        is_day = crawler._esun_spa_phase == "transaction_form" and target.evaluate("n => n.matches('.day-cell')")
        result = click(target, *args, **kwargs)
        if is_day:
            days.append(True)
            budget[0][0] = 32
        return result

    monkeypatch.setattr(query, "query_twd", retain_budget)
    monkeypatch.setattr(Locator, "click", day_click)
    result, error, _ = stored_job()
    assert days == [True] and budget == [[32]]
    assert [p for p, _ in hits] == list(PATHS[:2])
    assert form_diagnostic(result, error)[1].endswith("phase=transaction_form: gate=native_action_budget")
    assert not external and "data" not in result
    login_submit.assert_called_once()


def test_successful_query_clears_stale_marker(product, monkeypatch):
    crawler, _, origin, _, external, _, _, _, _ = product
    original = query.query_twd
    completed = []
    crawler._esun_spa_phase, crawler._esun_spa_gate = "transaction_form", "calendar_cleanup"

    def success(*args, **kwargs):
        assert crawler._esun_spa_gate is None
        result = original(*args, **kwargs)
        assert crawler._esun_spa_gate is None
        completed.append(True)
        return result

    monkeypatch.setattr(query, "query_twd", success)
    result = crawler.run(origin + "/synthetic", headless=True)
    assert completed == [True] and not external
    assert result["collect_diagnostics"]["phase"] == "incomplete_result"
    assert "gate" not in result["collect_diagnostics"]


class HostileString(str):
    def __eq__(self, other):
        raise AssertionError("must not compare subclass metadata")
    def __hash__(self):
        raise AssertionError("must not hash subclass metadata")
    def __str__(self):
        raise AssertionError("must not stringify subclass metadata")


@pytest.mark.parametrize("marker", [
    "calendar_readiness", "calendar_readiness\nSYNTHETIC-DOM", ["calendar_readiness"],
    {"gate": "calendar_readiness"}, HostileValue(), HostileString("calendar_readiness"),
], ids=["closed", "unknown", "list", "dict", "hostile-object", "hostile-string"])
def test_gate_metadata_is_allowlisted_at_producer_and_stored_sink(product, stored_job, monkeypatch, capsys, marker):
    crawler, *_ = product

    def fail(*_args):
        crawler._esun_spa_phase, crawler._esun_spa_gate = "transaction_form", marker
        raise ValueError("SYNTHETIC-PRIVATE-ERROR")

    monkeypatch.setattr(crawler, "collect", fail)
    result, error, _ = stored_job()
    expected = "collect_failed: ValueError: code=collect_contract: phase=transaction_form"
    if type(marker) is str and marker == "calendar_readiness":
        expected += ": gate=calendar_readiness"
    else:
        assert "gate" not in result["collect_diagnostics"]
    assert form_diagnostic(result, error)[1] == expected
    assert "SYNTHETIC" not in error + capsys.readouterr().err
