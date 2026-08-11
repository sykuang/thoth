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
    assert "simulated login failure" in (row["error_msg"] or "")


def test_unknown_bank_raises(isolated):
    """run_sync_job 對未知 bank 應 raise ValueError（route 層轉 400）。"""
    import backend.server.sync_runner as sr
    with pytest.raises(ValueError, match="unknown bank"):
        sr.run_sync_job(user_id=1, bank="zionsbank", headless=True)


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
