"""sweep_stale_running() — 殭屍 job 自動清理 regression.

2026-06-22 (使用者回報「同步完成通知收到但前端還顯示同步中」):
container restart / OOM crash 期間正在跑的 job 沒人走到 mark_done/mark_failed,
sync_jobs.status='running' 永遠卡死 → frontend `hasRunningJob` 永遠 true.
解法: GET /sync/jobs 路徑開頭一次性 sweep, 閾值 15 分鐘.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.server import sync_jobs_repo
from backend.server.db import get_conn


@pytest.fixture
def server_db(tmp_path, monkeypatch):
    """Fresh server DB for each test."""
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    # 強制 db_backend = sqlite (test default)
    monkeypatch.delenv("DB_BACKEND", raising=False)
    # reload db module 才能 pick 新 env (tests already pattern, 參 conftest)
    import importlib

    from backend.server import db as db_mod
    importlib.reload(db_mod)
    # reload sync_jobs_repo 也要 re-import get_conn
    importlib.reload(sync_jobs_repo)
    yield


def _insert_job(user_id: int, bank: str, status: str,
                started_at: str | None, finished_at: str | None = None) -> int:
    """Helper: 直接 insert job row, 不走 enqueue."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO sync_jobs (user_id, bank, account_id, status, "
            "created_at, started_at, finished_at, error_msg, "
            "result_summary, batch_id) "
            "VALUES (?, ?, NULL, ?, ?, ?, ?, NULL, NULL, NULL)",
            (user_id, bank, status, "2026-06-22T00:00:00+00:00",
             started_at, finished_at),
        )
        return cur.lastrowid


def _utc_iso(seconds_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def test_sweep_clears_running_job_older_than_threshold(server_db):
    """started_at > 15 min ago + status='running' → sweep 改 failed."""
    job_id = _insert_job(
        user_id=1, bank="cathay", status="running",
        started_at=_utc_iso(16 * 60),  # 16 分鐘前
    )
    swept = sync_jobs_repo.sweep_stale_running()
    assert swept >= 1
    with get_conn() as conn:
        row = conn.execute(
            "SELECT status, error_msg FROM sync_jobs WHERE id=?", (job_id,)
        ).fetchone()
    assert row[0] == "failed"
    assert row[1] is not None and "stale_sweep" in row[1]


def test_sweep_keeps_running_job_within_threshold(server_db):
    """started_at < 15 min ago → 仍是 running, sweep 不動."""
    job_id = _insert_job(
        user_id=1, bank="cathay", status="running",
        started_at=_utc_iso(5 * 60),  # 5 分鐘前 — 正常 sync 中
    )
    sync_jobs_repo.sweep_stale_running()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM sync_jobs WHERE id=?", (job_id,)
        ).fetchone()
    assert row[0] == "running", "5 分鐘前的 running 不該被誤殺"


def test_sweep_ignores_already_done_or_failed(server_db):
    """status != 'running' → sweep 不動 (idempotent)."""
    done_id = _insert_job(
        user_id=1, bank="hsbc", status="done",
        started_at=_utc_iso(20 * 60), finished_at=_utc_iso(15 * 60),
    )
    failed_id = _insert_job(
        user_id=1, bank="ctbc", status="failed",
        started_at=_utc_iso(30 * 60), finished_at=_utc_iso(25 * 60),
    )
    sync_jobs_repo.sweep_stale_running()
    with get_conn() as conn:
        row1 = conn.execute("SELECT status FROM sync_jobs WHERE id=?", (done_id,)).fetchone()
        row2 = conn.execute("SELECT status FROM sync_jobs WHERE id=?", (failed_id,)).fetchone()
    assert row1[0] == "done"
    assert row2[0] == "failed"


def test_sweep_ignores_queued_with_null_started_at(server_db):
    """status='running' 但 started_at IS NULL → 防呆 skip (理論上不該發生)."""
    job_id = _insert_job(
        user_id=1, bank="ubot", status="running",
        started_at=None,  # 異常 case
    )
    sync_jobs_repo.sweep_stale_running()
    with get_conn() as conn:
        row = conn.execute("SELECT status FROM sync_jobs WHERE id=?", (job_id,)).fetchone()
    assert row[0] == "running", "started_at NULL 不可被 sweep (數據異常另案處理)"


def test_list_recent_for_user_triggers_sweep(server_db):
    """使用者原 bug: 前端輪詢 /sync/jobs 該自動 sweep, user 看不到殭屍 job."""
    stale_id = _insert_job(
        user_id=1, bank="cathay", status="running",
        started_at=_utc_iso(16 * 60),
    )
    fresh_id = _insert_job(
        user_id=1, bank="ubot", status="running",
        started_at=_utc_iso(3 * 60),
    )
    jobs = sync_jobs_repo.list_recent_for_user(user_id=1, limit=50)
    # frontend 看到的 status 應該是 sweep 之後
    stale_job = next(j for j in jobs if j["id"] == stale_id)
    fresh_job = next(j for j in jobs if j["id"] == fresh_id)
    assert stale_job["status"] == "failed", "殭屍 job 沒被 sweep → frontend 還會卡同步中"
    assert fresh_job["status"] == "running", "新鮮 running 不該被誤殺"


def test_sweep_isolates_users_in_list_but_acts_globally(server_db):
    """List 是 per-user, 但 sweep 是 global (任何 user 撈 jobs 都該觸發全局 cleanup)."""
    user_a_stale = _insert_job(
        user_id=1, bank="cathay", status="running",
        started_at=_utc_iso(20 * 60),
    )
    user_b_stale = _insert_job(
        user_id=2, bank="hsbc", status="running",
        started_at=_utc_iso(20 * 60),
    )
    # user 1 撈 jobs → 應該連 user 2 的殭屍也清乾淨
    sync_jobs_repo.list_recent_for_user(user_id=1, limit=50)
    with get_conn() as conn:
        row_a = conn.execute("SELECT status FROM sync_jobs WHERE id=?", (user_a_stale,)).fetchone()
        row_b = conn.execute("SELECT status FROM sync_jobs WHERE id=?", (user_b_stale,)).fetchone()
    assert row_a[0] == "failed"
    assert row_b[0] == "failed", "global sweep 該清掉所有 user 的殭屍 job"
