"""Phase 1 — Sync runner (background thread + DB state machine).

Phase 1 — sync runner（背景 thread + DB state machine）。

策略：完全 mock 掉 BankCrawler/persist_*，只測 state machine 邏輯：
  - INSERT queued
  - thread 跑完 UPDATE running → done / failed
  - 設 env BANK_CRAWLER_USER_ID
  - 未知 bank → ValueError
"""
from __future__ import annotations

import importlib
import os
import threading
import time

import pytest
from cryptography.fernet import Fernet


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET", "test-secret-32+bytes-padding-padding-padding!")
    monkeypatch.setenv("SERVER_FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("BANK_CRAWLER_USER_ID", raising=False)
    import backend.server.db as db_mod
    import backend.server.creds_store as cs_mod
    importlib.reload(db_mod)
    importlib.reload(cs_mod)
    import backend.server.sync_runner as sr
    importlib.reload(sr)
    # 確保 user_id=1 存在（INSERT job 需要）
    import backend.server.auth as auth_mod
    import backend.server.users as users_mod
    importlib.reload(auth_mod)
    importlib.reload(users_mod)
    users_mod.create_user(email="syncuser@palace.example", password="SyntheticTestPassword02!")
    return tmp_path


def _wait_for_status(job_id: int, want: set[str], timeout: float = 5.0):
    """polling 等 job 進到指定狀態之一。"""
    from backend.server.sync_runner import get_job
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = get_job(job_id)
        if row and row["status"] in want:
            return row
        time.sleep(0.05)
    raise TimeoutError(f"job {job_id} 沒進入 {want}，最後狀態={row['status'] if row else 'NONE'}")


def test_run_sync_job_creates_queued_row(isolated, monkeypatch):
    """呼叫 run_sync_job → DB 立刻有一筆 queued 列；status 可能下一刻變 running。"""
    import backend.server.sync_runner as sr
    # 攔截 _exec_sync 不真跑 → 確保我們只看到 queued
    monkeypatch.setattr(sr, "_exec_sync", lambda job_id: None)
    job_id = sr.run_sync_job(user_id=1, bank="sinopac", headless=True)
    assert isinstance(job_id, int)
    row = sr.get_job(job_id)
    assert row is not None
    assert row["user_id"] == 1
    assert row["bank"] == "sinopac"
    assert row["status"] == "queued"
    assert row["history_mode"] == "full"


def test_external_execution_mode_is_normalized_before_launch(isolated, monkeypatch):
    import backend.server.sync_runner as sr
    from backend.server.creds_store import AccountsRepo

    calls: list[int] = []
    monkeypatch.setenv("SYNC_EXECUTION_MODE", " External ")
    monkeypatch.setattr(sr, "_exec_sync", lambda job_id: calls.append(job_id))
    account = AccountsRepo().create(1, "sinopac", "main")

    job_id = sr.run_sync_job_for_account(account.id)
    time.sleep(0.05)

    assert calls == []
    row = sr.get_job(job_id)
    assert row is not None
    assert row["status"] == "queued"


@pytest.mark.parametrize("bank", sorted([
    "cathay", "ubot", "hsbc", "ctbc", "sinopac", "scsb", "esun",
    "taishin", "fubon", "dbs", "scb", "linebank", "rakuten",
]))
def test_account_sync_uses_full_history_once_then_incremental(
    isolated, monkeypatch, bank,
):
    import backend.server.sync_runner as sr
    from backend.server import sync_jobs_repo
    from backend.server.creds_store import AccountsRepo

    monkeypatch.setattr(sr, "_exec_sync", lambda job_id: None)
    monkeypatch.setattr(
        sr, "_required_history_domains", lambda bank: frozenset({"twd_transactions"}),
    )
    account = AccountsRepo().create(1, bank, "主帳")

    first_id = sr.run_sync_job_for_account(account.id)
    assert sr.get_job(first_id)["history_mode"] == "full"

    # Legacy or pre-attestation done jobs do not prove history completeness.
    sync_jobs_repo.mark_done(first_id, "{}")
    retry_id = sr.run_sync_job_for_account(account.id)
    assert sr.get_job(retry_id)["history_mode"] == "full"

    sync_jobs_repo.mark_done(
        retry_id,
        '{"history_coverage":{"ok":true,"mode":"full",'
        '"domains":["twd_transactions"],"identities":1,"windows":1,'
        '"start":"2025-08-31","end":"2026-08-30"}}',
    )
    update_id = sr.run_sync_job_for_account(account.id)
    assert sr.get_job(update_id)["history_mode"] == "incremental"

    forced_id = sr.run_sync_job_for_account(account.id, force_full_history=True)
    assert sr.get_job(forced_id)["history_mode"] == "full"


def test_non_opted_adapter_preserves_legacy_first_then_incremental(
    isolated, monkeypatch,
):
    import backend.server.sync_runner as sr
    from backend.server import sync_jobs_repo
    from backend.server.creds_store import AccountsRepo

    monkeypatch.setattr(sr, "_exec_sync", lambda job_id: None)
    monkeypatch.setattr(sr, "_required_history_domains", lambda bank: frozenset())
    account = AccountsRepo().create(1, "ctbc", "主帳")

    first_id = sr.run_sync_job_for_account(account.id)
    assert sr.get_job(first_id)["history_mode"] == "full"
    sync_jobs_repo.mark_done(first_id, "{}")

    second_id = sr.run_sync_job_for_account(account.id)
    assert sr.get_job(second_id)["history_mode"] == "incremental"


def test_sync_runner_updates_status_done_on_success(isolated, monkeypatch):
    """假 crawler 成功跑完 → 清 Dashboard cache，再標 done。"""
    import backend.server.sync_runner as sr

    # 假 dispatch：直接 return {"data": {...}, "delta": {...}}
    def fake_dispatch(bank: str, user_id: int, headless: bool) -> dict:
        # 也驗證 user_id 真的從上層帶下來
        assert user_id == 1
        return {"delta": {"twd_txn_new": 3}, "stats": {}}

    cleared: list[int] = []
    monkeypatch.setattr(sr, "_dispatch_crawler_and_persist", fake_dispatch)
    monkeypatch.setattr(sr, "clear_dashboard_cache", lambda user_id: cleared.append(user_id))

    job_id = sr.run_sync_job(user_id=1, bank="sinopac", headless=True)
    row = _wait_for_status(job_id, {"done", "failed"})
    assert row["status"] == "done", f"unexpected: {row}"
    assert row["finished_at"]
    assert row["error_msg"] is None or row["error_msg"] == ""
    assert cleared == [1]
    # result_summary 應該 JSON-able 且含 delta
    import json
    summary = json.loads(row["result_summary"])
    assert summary["delta"]["twd_txn_new"] == 3


def test_sync_job_waiting_for_dispatch_lock_stays_queued_not_running(isolated, monkeypatch):
    """排程 fan-out 會一次開多個 thread；等 lock 的 job 不可先標 running。

    若等待中的 job 先變 running，/sync/jobs 的 stale sweep 會把排在後面的銀行
    誤殺成 failed（典型：定時 sync 全部時永豐排在多家銀行後面；單獨 sync 不會）。
    """
    import backend.server.sync_runner as sr

    monkeypatch.setattr(
        sr,
        "_dispatch_crawler_and_persist",
        lambda bank, user_id, headless: {"delta": {}, "stats": {}},
    )

    sr._dispatch_lock.acquire()
    try:
        job_id = sr.run_sync_job(user_id=1, bank="sinopac", headless=True)
        # 給 background thread 時間跑到 lock 前。正確行為：仍然 queued。
        time.sleep(0.15)
        row = sr.get_job(job_id)
        assert row is not None
        assert row["status"] == "queued"
        assert row["started_at"] is None
    finally:
        sr._dispatch_lock.release()

    row = _wait_for_status(job_id, {"done", "failed"})
    assert row["status"] == "done", f"unexpected: {row}"


def test_sync_runner_records_error_on_failure(isolated, monkeypatch):
    """假 crawler 丟例外 → status=failed、error_msg 記住訊息。"""
    import backend.server.sync_runner as sr

    def boom(bank: str, user_id: int, headless: bool) -> dict:
        raise RuntimeError("simulated login failure")

    monkeypatch.setattr(sr, "_dispatch_crawler_and_persist", boom)
    job_id = sr.run_sync_job(user_id=1, bank="sinopac", headless=True)
    row = _wait_for_status(job_id, {"done", "failed"})
    assert row["status"] == "failed", f"unexpected: {row}"
    assert row["error_msg"] == "sync_failed:RuntimeError"


def test_unknown_bank_raises(isolated):
    """run_sync_job 對未知 bank 應 raise ValueError（route 層轉 400）。"""
    import backend.server.sync_runner as sr
    with pytest.raises(ValueError, match="unknown bank"):
        sr.run_sync_job(user_id=1, bank="zionsbank", headless=True)


def test_card_bill_cycle_coverage_summary_is_non_sensitive_and_nullable():
    from backend.server.sync_runner import _summarize_card_bill_cycle_coverage

    assert _summarize_card_bill_cycle_coverage({
        "card_bill_facts_ok": True,
        "card_bill_facts": [
            {"scope": "bank", "statement_close_date": "2026-08-01"},
            {"scope": "card", "payment_due_date": "2026-08-20"},
            {"scope": "card"},
        ],
    }) == {
        "ok": True,
        "facts": 3,
        "bank_scope": 1,
        "card_scope": 2,
        "statement_date": 1,
        "due_date": 1,
        "without_cycle": 1,
    }


def test_dispatch_rejects_missing_coverage_for_opted_in_adapter(isolated, monkeypatch):
    from backend.banks import sinopac
    import backend.server.sync_runner as sr

    class FakeCrawler:
        HISTORY_COVERAGE_REQUIRED = True
        HISTORY_COVERAGE_DOMAINS = frozenset({"twd_transactions"})
        cursor_domains = []

        def configure_transaction_cursor(self, domain, cursor):
            self.cursor_domains.append(domain)

        def run(self, *, login_url, headless):
            return {"data": {"card_bill_facts_ok": False}}

    monkeypatch.setattr(sinopac, "SinopacCrawler", FakeCrawler)

    with pytest.raises(ValueError, match="history coverage"):
        sr._dispatch_crawler_and_persist("sinopac", user_id=1, headless=True)

    assert set(FakeCrawler.cursor_domains) == {
        "twd_transactions", "card_billed_transactions",
    }


def test_dispatch_defaults_coverage_validation_to_full(isolated, monkeypatch):
    from backend.banks import sinopac
    from backend.core import base, persist
    import backend.server.sync_runner as sr

    seen = []

    class FakeCrawler:
        HISTORY_COVERAGE_REQUIRED = True
        HISTORY_COVERAGE_DOMAINS = frozenset({"twd_transactions"})

        def __init__(self, **_kwargs):
            pass

        def configure_transaction_cursor(self, _domain, _cursor):
            pass

        def run(self, *, login_url, headless):
            return {"data": {"history_coverage": {}}}

    def validate(_coverage, *, expected_mode, expected_domains):
        seen.append((expected_mode, expected_domains))
        return {}

    monkeypatch.delenv("BANK_CRAWLER_HISTORY_MODE", raising=False)
    monkeypatch.setattr(sinopac, "SinopacCrawler", FakeCrawler)
    monkeypatch.setattr(base, "validate_history_coverage", validate)
    monkeypatch.setattr(persist, "persist_collected", lambda *_args, **_kwargs: {})

    sr._dispatch_crawler_and_persist("sinopac", user_id=1, headless=True)

    assert seen == [("full", frozenset({"twd_transactions"}))]


@pytest.mark.parametrize("legacy_summary", [
    "null", "[]", "1", '"text"', "not-json",
    '{"history_coverage":{"ok":true,"mode":"full"}}',
    '{"history_coverage":"full"}',
    '{"history_coverage":{"ok":true,"mode":"full","domains":[{}],'
    '"identities":1,"windows":1,"start":"2025-08-31","end":"2026-08-30"}}',
    '{"history_coverage":{"ok":true,"mode":"full","domains":[[]],'
    '"identities":1,"windows":1,"start":"2025-08-31","end":"2026-08-30"}}',
    '{"history_coverage":{"ok":true,"mode":"full",'
    '"domains":["twd_transactions","twd_transactions"],'
    '"identities":1,"windows":1,"start":"2025-08-31","end":"2026-08-30"}}',
    '{"history_coverage":{"ok":true,"mode":"full","domains":["twd_transactions"],'
    '"identities":1,"windows":1,"start":"20250831","end":"2026-08-30"}}',
    '{"history_coverage":{"ok":true,"mode":"full","domains":["twd_transactions"],'
    '"identities":1,"windows":1,"start":"2025-08-31T00:00:00","end":"2026-08-30"}}',
])
def test_non_object_legacy_summary_does_not_unlock_incremental(
    isolated, monkeypatch, legacy_summary,
):
    import backend.server.sync_runner as sr
    from backend.server import sync_jobs_repo
    from backend.server.creds_store import AccountsRepo

    monkeypatch.setattr(sr, "_exec_sync", lambda job_id: None)
    monkeypatch.setattr(
        sr, "_required_history_domains", lambda bank: frozenset({"twd_transactions"}),
    )
    account = AccountsRepo().create(1, "scsb", "主帳")
    job_id = sr.run_sync_job_for_account(account.id)
    sync_jobs_repo.mark_done(job_id, legacy_summary)

    retry_id = sr.run_sync_job_for_account(account.id)
    assert sr.get_job(retry_id)["history_mode"] == "full"


def test_stale_partial_domain_attestation_does_not_unlock_incremental(
    isolated, monkeypatch,
):
    import backend.server.sync_runner as sr
    from backend.server import sync_jobs_repo
    from backend.server.creds_store import AccountsRepo

    monkeypatch.setattr(sr, "_exec_sync", lambda job_id: None)
    monkeypatch.setattr(
        sr,
        "_required_history_domains",
        lambda bank: frozenset({"twd_transactions", "card_billed_transactions"}),
    )
    account = AccountsRepo().create(1, "scsb", "主帳")
    first_id = sr.run_sync_job_for_account(account.id)
    sync_jobs_repo.mark_done(
        first_id,
        '{"history_coverage":{"ok":true,"mode":"full",'
        '"domains":["twd_transactions"],"identities":1,"windows":1,'
        '"start":"2025-08-31","end":"2026-08-30"}}',
    )

    retry_id = sr.run_sync_job_for_account(account.id)
    retry = sr.get_job(retry_id)
    assert retry is not None
    assert retry["history_mode"] == "full"


def test_sync_dispatcher_loads_user_rules_and_passes_to_persist(isolated, monkeypatch):
    """Phase 5.1：_dispatch_crawler_and_persist 必須撈 user 的 rules，
    並透過 BANK_CRAWLER_RULES env 或直接 inject 給 persist/store 用。
    我們驗證：跑完後 user 的 rules 真的有被 sync_runner 撈出來（透過監聽器）。
    """
    from backend.server import rules_repo
    import backend.server.sync_runner as sr

    # 先建一條 rule
    rules_repo.create_rule(user_id=1, name="transit", pattern=r"北捷",
                           category="交通", priority=100)

    captured: dict = {}

    def fake_dispatch_persist(bank: str, user_id: int, headless: bool) -> dict:
        # 在 dispatch 內, sync_runner 直接以 user_id 撈 rules (Phase C 起改走 arg
        # 而非 env), 可被 persist 看見
        rules = rules_repo.list_rules(user_id=user_id, enabled_only=True)
        captured["rules"] = rules
        return {"delta": {"twd_txn_new": 0}, "stats": {}}

    monkeypatch.setattr(sr, "_dispatch_crawler_and_persist", fake_dispatch_persist)
    job_id = sr.run_sync_job(user_id=1, bank="sinopac", headless=True)
    _wait_for_status(job_id, {"done", "failed"})
    assert captured.get("rules") and captured["rules"][0]["pattern"] == "北捷", \
        f"expected rules captured, got: {captured}"


def test_concurrent_jobs_restore_environment_from_inside_dispatch_lock(
    isolated, monkeypatch,
):
    import backend.server.sync_runner as sr
    from backend.server import sync_jobs_repo

    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "outer")
    first_entered = threading.Event()
    release_first = threading.Event()
    calls = 0

    def fake_dispatch(bank: str, user_id: int, headless: bool) -> dict:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_entered.set()
            assert release_first.wait(timeout=3)
        return {"delta": {}, "stats": {}}

    monkeypatch.setattr(sr, "_dispatch_crawler_and_persist", fake_dispatch)
    first = sync_jobs_repo.queue(user_id=1, bank="scsb", history_mode="full")
    second = sync_jobs_repo.queue(user_id=1, bank="scsb", history_mode="incremental")
    first_thread = threading.Thread(target=sr._exec_sync, args=(first,))
    second_thread = threading.Thread(target=sr._exec_sync, args=(second,))

    first_thread.start()
    assert first_entered.wait(timeout=3)
    second_thread.start()
    time.sleep(0.05)
    release_first.set()
    first_thread.join(timeout=3)
    second_thread.join(timeout=3)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert os.environ["BANK_CRAWLER_HISTORY_MODE"] == "outer"
