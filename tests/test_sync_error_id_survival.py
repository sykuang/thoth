"""Strictly offline, existing error_msg carrier: producer through every sink."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.core import base, error_codes, persist, store
from backend.server import card_events, rules_repo, sync_jobs_repo, sync_runner as runner

REAL_PERSIST = persist.persist_collected


class SyntheticCrawler(base.BankCrawler):
    HISTORY_COVERAGE_REQUIRED = False

    def login(self, page):
        return True

    def collect(self, page, collector):
        return {}


@pytest.fixture
def offline(monkeypatch, tmp_path):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    monkeypatch.setenv("SYNC_EXECUTION_MODE", "external")
    monkeypatch.setattr(card_events, "snapshot_cards", lambda **kw: [])
    monkeypatch.setattr(rules_repo, "list_rules", lambda **kw: [])
    crawler = object.__new__(SyntheticCrawler)
    crawler.name = "esun"
    crawler.configure_transaction_cursor = Mock()
    crawler.run = Mock(return_value={"data": {}})
    db = SimpleNamespace(latest_twd_transaction_dates=Mock(return_value={}),
        latest_account_transaction_dates=Mock(return_value={}),
        latest_card_transaction_dates=Mock(return_value={}), stats=Mock(return_value={}), close=Mock())
    monkeypatch.setattr(store, "BankStore", lambda *a, **kw: db)
    monkeypatch.setattr(runner, "_load_crawler", lambda bank: (SimpleNamespace(BASE="offline"), lambda: crawler))
    monkeypatch.setattr(persist, "persist_collected", Mock(return_value={}))
    import backend.server.auto_debit_payments as debit
    monkeypatch.setattr(debit, "apply_auto_debit_payments", lambda *a: 0)
    return crawler, db


def test_catalog_has_distinct_cursor_actions():
    ids = [f"ESUN-00-{action:03d}-001" for action in range(1, 7)]
    assert all(error_codes.code_details(value, bank="esun") for value in ids)
    assert len({error_codes.format_error_id(value) for value in ids}) == len(ids)


@pytest.mark.parametrize("method, error_id", [
    ("latest_twd_transaction_dates", "ESUN-00-004-001"),
    ("latest_account_transaction_dates", "ESUN-00-005-001"),
    ("latest_card_transaction_dates", "ESUN-00-006-001"),
    ("stats", "ESUN-08-910-001"), ("close", "ESUN-08-920-001"),
])
def test_action_exception_keeps_type_and_id(offline, method, error_id):
    _, db = offline
    failure = ValueError("PRIVATE_SENTINEL")
    getattr(db, method).side_effect = failure
    with pytest.raises(ValueError) as caught:
        runner._dispatch_crawler_and_persist("esun", 1)
    assert caught.value is failure
    assert error_codes.exception_error_id(caught.value, bank="esun") == error_id


def test_original_persistence_id_survives_wrapper_and_close(offline, monkeypatch):
    _, db = offline
    failure = error_codes.with_error_code(ValueError("PRIVATE_SENTINEL"), "SYS-08-001-007")
    monkeypatch.setattr(persist, "persist_collected", Mock(side_effect=failure))
    db.close.side_effect = RuntimeError("SECONDARY_PRIVATE")
    with pytest.raises(runner._PersistenceError) as caught:
        runner._dispatch_crawler_and_persist("esun", 1)
    assert caught.value.__cause__ is failure
    assert error_codes.exception_error_id(caught.value, bank="esun") == "SYS-08-001-007"


def test_cli_safe_failure_and_primary_cleanup(offline, monkeypatch, capsys):
    from cli import cli
    crawler, db = offline
    crawler.run.return_value = {"error": "PRIVATE_SENTINEL", "failure_diagnostics": {
        "exception": "RuntimeError", "error_id": "ESUN-03-910-001"}}
    db.close.side_effect = ValueError("SECONDARY_PRIVATE")
    monkeypatch.setattr(cli, "_get_crawler", lambda bank: (crawler, "offline"))
    monkeypatch.setattr(cli, "BankStore", lambda bank: db)
    monkeypatch.setattr(cli, "_remove_private_json", lambda path: None)
    assert cli.cmd_sync(SimpleNamespace(bank="esun", headless=True)) == 1
    output = capsys.readouterr()
    assert error_codes.format_error_id("ESUN-03-910-001") in output.out
    assert "PRIVATE" not in output.out + output.err
    persist.persist_collected.assert_not_called()


def test_push_never_slices_registered_long_id(monkeypatch):
    from backend.core.error_catalog import system
    error_id = "ESUN-00-001-" + "1" * 100
    monkeypatch.setitem(system.CODES, error_id, ("讀取游標", "讀取失敗"))
    prefix = error_codes.format_error_id(error_id)
    assert runner._brief_error(prefix + " | optional=" + "x" * 200 + "\nignored") == prefix


@pytest.mark.parametrize("sweep, error_id", [
    ("sweep_stale_queued", "SYS-09-001-001"),
    ("sweep_stale_scheduled_queued", "SYS-09-002-001"),
    ("sweep_stale_running", "SYS-09-003-001"),
])
def test_stale_writers_use_registered_infrastructure_copy(client, monkeypatch, sweep, error_id):
    from backend.server import db, sync_batches_repo
    token = client.post("/auth/register", json={"email": "stale@palace.example", "password": "SyntheticPassword03!"}).json()["token"]
    user_id = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).json()["id"]
    kind = "scheduled_all" if "scheduled" in sweep else "manual_all"
    batch = sync_batches_repo.create(user_id=user_id, total_jobs=1, kind=kind)
    job = sync_jobs_repo.queue(user_id=user_id, bank="esun", batch_id=batch)
    with db.get_conn() as conn:
        conn.execute("UPDATE sync_jobs SET created_at='2000-01-01', started_at='2000-01-01', status=? WHERE id=?", ("running" if sweep.endswith("running") else "queued", job))
    assert getattr(sync_jobs_repo, sweep)().swept_count == 1
    assert sync_jobs_repo.get(job)["error_msg"].startswith(error_codes.format_error_id(error_id))


@pytest.mark.parametrize("stage, error_id", [
    ("login", "ESUN-03-900-001"),
    ("collect", "ESUN-07-960-001"),
    ("browser", "ESUN-01-910-001"),
    ("persistence", "ESUN-08-900-001"),
    ("persist_contract", "ESUN-08-800-002"),
])
def test_producer_to_worker_db_rest_ws_push_and_cli(client, offline, monkeypatch, capsys, stage, error_id):
    import json
    import os
    from pathlib import Path
    from cli import cli
    from backend.server import push, sync_job_worker
    crawler, db = offline
    token = client.post("/auth/register", json={"email": "survival@palace.example", "password": "SyntheticPassword03!"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    user_id = client.get("/auth/me", headers=headers).json()["id"]
    payloads = []
    monkeypatch.setattr(push, "get_notifier", lambda: SimpleNamespace(send_to_user=lambda **kw: payloads.append(kw["payload"])))
    failure = error_codes.with_error_code(ValueError("PRIVATE_SENTINEL"), error_id)
    if stage == "persistence":
        monkeypatch.setattr(persist, "persist_collected", Mock(side_effect=failure))
    elif stage == "persist_contract":
        monkeypatch.setattr(persist, "persist_collected", REAL_PERSIST)
    else:
        monkeypatch.setattr(crawler, "run", base.BankCrawler.run.__get__(crawler))
        monkeypatch.setattr(crawler, "_enforce_session_freshness", lambda: None)
        monkeypatch.setattr(crawler, "_build_fetch_kwargs", lambda: {})
        monkeypatch.setattr(crawler, "_make_collector", lambda page: base.ResponseCollector())
        monkeypatch.setattr(crawler, "_ensure_collect_origin", lambda page: None)
        monkeypatch.setattr(crawler, "_credential_origin_allowed", lambda page: True)
        monkeypatch.setattr(crawler, "logout", Mock(side_effect=RuntimeError("SECONDARY_PRIVATE")))
        monkeypatch.setattr(crawler, "_shared_login", Mock(return_value=True, side_effect=failure if stage == "login" else None))
        monkeypatch.setattr(crawler, "collect", Mock(side_effect=failure))
        page = SimpleNamespace(on=Mock(), remove_listener=Mock())
        if stage == "browser":
            monkeypatch.setattr(crawler, "_execute_browser_flow", Mock(side_effect=failure))
        else:
            monkeypatch.setattr(crawler, "_execute_browser_flow", lambda url, **kw: kw["page_action"](page))
    db.close.side_effect = RuntimeError("SECONDARY_PRIVATE")
    job = sync_jobs_repo.queue(user_id=user_id, bank="esun", history_mode="full")
    assert sync_job_worker.run_queued_jobs() == {"processed": 1, "done": 0, "failed": 1}
    stored = sync_jobs_repo.get(job)
    prefix = error_codes.format_error_id(error_id)
    assert stored["error_msg"].startswith(prefix)
    detail = client.get(f"/sync/jobs/{job}", headers=headers).json()
    listed = client.get("/sync/jobs", headers=headers).json()
    assert detail["error_msg"] == stored["error_msg"]
    assert next(j for j in listed if j["id"] == job)["error_msg"] == stored["error_msg"]
    with client.websocket_connect(f"/ws/sync/{job}", subprotocols=["bearer", token]) as ws:
        message = ws.receive_json()
    assert message["error_msg"] == stored["error_msg"]
    assert len(payloads) == 1 and payloads[0].body.startswith(prefix)
    assert len(payloads[0].body) <= 100
    # Same actual producer, validator and formatter, without entering credential loading.
    monkeypatch.setattr(cli, "_get_crawler", lambda bank: (crawler, "offline"))
    monkeypatch.setattr(cli, "BankStore", lambda bank: db)
    monkeypatch.setattr(cli, "_remove_private_json", lambda path: None)
    assert cli.cmd_sync(SimpleNamespace(bank="esun", headless=True)) == 1
    output = capsys.readouterr()
    assert prefix in output.out
    assert "PRIVATE" not in output.out + output.err + stored["error_msg"] + payloads[0].body
    if stage not in {"persistence", "persist_contract"}:
        persist.persist_collected.assert_not_called()
    fixture_path = os.environ.get("SYNC_ERROR_HTTP_FIXTURE")
    if fixture_path:
        path = Path(fixture_path)
        fixtures = json.loads(path.read_text()) if path.exists() else []
        fixtures = [item for item in fixtures if item.get("prefix") != prefix]
        fixtures.append({"prefix": prefix, "detail": detail, "list": listed, "ws": message})
        path.write_text(json.dumps(fixtures, ensure_ascii=False))


@pytest.mark.parametrize("boundary, error_id", [
    ("module", "ESUN-00-001-001"), ("credentials", "ESUN-00-002-001"),
    ("store", "ESUN-00-003-001"), ("coverage", "ESUN-07-950-001"),
])
def test_preparation_boundaries_tag_original_exception(offline, monkeypatch, boundary, error_id):
    crawler, _ = offline
    failure = OSError("PRIVATE_SENTINEL")
    if boundary == "module":
        monkeypatch.setattr(runner, "_load_crawler", Mock(side_effect=failure))
    elif boundary == "credentials":
        monkeypatch.setattr(runner, "_load_crawler", lambda bank: (SimpleNamespace(BASE="offline"), Mock(side_effect=failure)))
    elif boundary == "store":
        monkeypatch.setattr(store, "BankStore", Mock(side_effect=failure))
    else:
        crawler.HISTORY_COVERAGE_REQUIRED = True
        crawler.HISTORY_COVERAGE_DOMAINS = frozenset({"twd_transactions"})
        monkeypatch.setattr(base, "validate_history_coverage", Mock(side_effect=failure))
    with pytest.raises(OSError) as caught:
        runner._dispatch_crawler_and_persist("esun", 1)
    assert caught.value is failure
    assert error_codes.exception_error_id(failure, bank="esun") == error_id


@pytest.mark.parametrize("boundary, error_id", [
    ("cache", "ESUN-09-001-001"), ("json", "ESUN-09-002-001"),
    ("mark_done", "ESUN-09-003-001"),
])
def test_finalization_failure_stores_its_own_id(client, offline, monkeypatch, boundary, error_id):
    response = client.post("/auth/register", json={"email": "finalize@palace.example", "password": "SyntheticPassword03!"}).json()
    job = sync_jobs_repo.queue(user_id=response["user_id"], bank="esun")
    failure = ValueError("PRIVATE_SENTINEL")
    monkeypatch.setattr(runner, "_send_sync_notification", lambda **kw: None)
    if boundary == "cache":
        monkeypatch.setattr(runner, "clear_dashboard_cache", Mock(side_effect=failure))
    elif boundary == "json":
        monkeypatch.setattr(runner, "_dispatch_crawler_and_persist", lambda *a, **kw: {"unsupported": object()})
    else:
        monkeypatch.setattr(sync_jobs_repo, "mark_done", Mock(side_effect=failure))
    assert runner._exec_sync(job)
    row = sync_jobs_repo.get(job)
    assert row["status"] == "failed"
    assert row["error_msg"].startswith(error_codes.format_error_id(error_id))
    assert "PRIVATE" not in row["error_msg"]


@pytest.mark.parametrize("carrier", [
    {"exception": "RuntimeError", "error_id": "CTBC-03-910-001"},
    {"exception": "RuntimeError", "error_id": "ESUN-03-910-999999"},
    {"exception": "RuntimeError", "error_id": "ESUN-03-910-001", "raw": "PRIVATE"},
])
def test_forged_failure_carrier_is_revalidated(offline, carrier):
    from backend.core.sync_errors import FailureDiagnosticError, safe_failure_message
    crawler, _ = offline
    message = safe_failure_message(FailureDiagnosticError(crawler, carrier), "esun")
    assert message.startswith(error_codes.format_error_id("ESUN-09-900-001"))
    assert "PRIVATE" not in message


def test_exception_metaclass_hooks_never_run_at_shared_sink():
    from backend.core.sync_errors import safe_failure_message
    class HostileMeta(type):
        def __eq__(self, other):
            raise AssertionError("must not invoke exception metaclass equality")
        __hash__ = type.__hash__
    class Failure(ValueError, metaclass=HostileMeta):
        pass
    message = safe_failure_message(Failure("PRIVATE_SENTINEL"), "esun")
    assert message.startswith(error_codes.format_error_id("ESUN-09-900-001"))
    assert "PRIVATE" not in message


@pytest.mark.parametrize("mode", ["inprocess", "external"])
def test_actual_terminal_storage_failure_is_safe_at_both_entrypoints(
    client, offline, monkeypatch, capsys, mode,
):
    from backend.server import sync_job_worker
    response = client.post("/auth/register", json={
        "email": "terminal-boundary@palace.example", "password": "SyntheticPassword03!",
    }).json()
    job = sync_jobs_repo.queue(user_id=response["user_id"], bank="esun")
    dispatch = Mock(side_effect=RuntimeError("PRIMARY_PRIVATE_SENTINEL"))
    monkeypatch.setattr(runner, "_dispatch_crawler_and_persist", dispatch)
    write_failure = Mock(side_effect=OSError("TERMINAL_PRIVATE_SENTINEL"))
    monkeypatch.setattr(sync_jobs_repo, "mark_failed", write_failure)
    notify = Mock()
    monkeypatch.setattr(runner, "_send_sync_notification", notify)
    if mode == "inprocess":
        captured = {}
        thread = Mock()

        def make_thread(**kwargs):
            captured.update(kwargs)
            return thread

        monkeypatch.setenv("SYNC_EXECUTION_MODE", "inprocess")
        monkeypatch.setattr(runner.threading, "Thread", make_thread)
        runner._launch_job(job)
        thread.start.assert_called_once_with()
        assert captured["daemon"] is True
        assert captured["args"] == (job,)
        captured["target"](*captured["args"])
    else:
        assert sync_job_worker.main(["queued"]) == 1
    dispatch.assert_called_once()
    write_failure.assert_called_once()
    notify.assert_not_called()
    row = sync_jobs_repo.get(job)
    assert row is not None
    assert row["status"] == "running"
    assert row["finished_at"] is None and row["error_msg"] is None
    output = capsys.readouterr()
    assert "[ESUN-09-004-001] 同步結果收尾：失敗狀態寫入失敗；無法確認儲存結果" in output.err
    assert output.out == ""
    assert "PRIVATE" not in output.err and "Traceback" not in output.err


def test_worker_terminal_storage_failure_is_safe_not_fake_success(monkeypatch, capsys):
    from backend.server import sync_job_worker
    failure = error_codes.with_error_code(OSError("PRIVATE_SENTINEL"), "ESUN-09-004-001")
    monkeypatch.setattr(sync_job_worker, "run_queued_jobs", Mock(side_effect=failure))
    assert sync_job_worker.main(["queued"]) == 1
    output = capsys.readouterr()
    assert error_codes.format_error_id("ESUN-09-004-001") in output.err
    assert output.out == ""
    assert "PRIVATE" not in output.err
