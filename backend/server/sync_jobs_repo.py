"""sync_jobs (server DB) — queue/poll bank sync runs.

Columns: id, user_id, bank, account_id, status, created_at, started_at,
         finished_at, error_msg, result_summary, batch_id.

2026-06-23: 加 `batch_id` — `/sync/all` 跟 scheduler fan-out 排出的 job 共用同個
batch_id, 用來聚合「同步全部完成」一則 summary push (取代每家銀行各推 sync_done).
單支 /sync/{bank} / /sync/account/{id} 路徑 batch_id = NULL → 走 legacy 單則 push.

2026-06-22: 加 `sweep_stale_running()` 自動 sweep 殭屍 running job. Container restart /
OOM crash 期間正在跑的 job 因為沒人走到 mark_done/mark_failed, sync_jobs.status='running'
永遠卡死 → frontend `hasRunningJob` 永遠 true → 顯示「同步中」即使通知早就收到.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from backend.server.db import get_conn, now_iso

_COLS = (
    "id, user_id, bank, account_id, status, created_at, started_at, "
    "finished_at, error_msg, result_summary, batch_id, history_mode"
)

# 任何 sync 跑超過這時間都算 stale (sync 最慢 10 分鐘左右 — cathay/ctbc anti-bot 完整流程).
# 15 分鐘安全 margin, 不誤判正常 sync.
_STALE_THRESHOLD_SECONDS = 15 * 60


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row[0],
        "user_id": row[1],
        "bank": row[2],
        "account_id": row[3],
        "status": row[4],
        "created_at": row[5],
        "started_at": row[6],
        "finished_at": row[7],
        "error_msg": row[8],
        "result_summary": row[9],
        "batch_id": row[10],
        "history_mode": row[11],
    }


def queue(
    *,
    user_id: int,
    bank: str,
    account_id: int | None = None,
    batch_id: int | None = None,
    history_mode: str = "incremental",
) -> int:
    """INSERT queued row, return new id.

    `batch_id` 為非 None 時, 收尾邏輯會走 batch summary 路徑
    (sync_runner._maybe_send_batch_summary), skip 個別 sync_done push.
    """
    if history_mode not in {"full", "incremental"}:
        raise ValueError(f"invalid history_mode: {history_mode!r}")
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO sync_jobs
                (user_id, bank, account_id, status, created_at, batch_id, history_mode)
            VALUES (?, ?, ?, 'queued', ?, ?, ?) RETURNING id
            """,
            (user_id, bank, account_id, now_iso(), batch_id, history_mode),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("INSERT sync_jobs 後 RETURNING 為 None")
    return int(row[0])


def get(job_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {_COLS} FROM sync_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_recent_for_user(user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    # 2026-06-22: sweep stale running jobs 才回 jobs list, 避免殭屍 job 卡 UI.
    # 不傳 user_id — sweep 是 global cleanup (殭屍 job 任何 user 撈到 jobs 都該清乾淨).
    sweep_stale_running()
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {_COLS} FROM sync_jobs WHERE user_id=? "
            "ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def has_completed_for_account(account_id: int) -> bool:
    """A failed/aborted first sync does not turn later retries into incremental syncs."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM sync_jobs "
            "WHERE account_id=? AND status='done' LIMIT 1",
            (account_id,),
        ).fetchone()
    return row is not None


def sweep_stale_running() -> int:
    """把 started_at 超過 _STALE_THRESHOLD_SECONDS 的 running job mark 成 failed.

    2026-06-22: container restart / OOM / thread crash 期間正在跑的 job 沒人走到
    mark_done/mark_failed, status='running' 永遠卡死, frontend `hasRunningJob`
    永遠 true → 顯示「同步中」即使通知早就收到.

    解法: GET /sync/jobs 路徑開頭一次性 sweep, 沒有 cron 也能自動清.
    閾值 15 分鐘 (sync 最慢 ~10 分鐘 cathay/ctbc anti-bot 完整流程, margin 足).
    error_msg 寫死 'stale_sweep: ...' 識別性高,
    debug 時 grep 一翻兩瞪眼.

    回傳 swept count (test 用).
    """
    threshold = (
        datetime.now(timezone.utc) - timedelta(seconds=_STALE_THRESHOLD_SECONDS)
    ).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE sync_jobs SET status='failed', finished_at=?, "
            "error_msg=? WHERE status='running' AND started_at IS NOT NULL "
            "AND started_at < ?",
            (now_iso(), f"stale_sweep: job stuck >{_STALE_THRESHOLD_SECONDS}s "
                        "without mark_done/mark_failed (container restart / crash?)",
             threshold),
        )
        return cur.rowcount or 0


def list_by_batch(batch_id: int) -> list[dict[str, Any]]:
    """撈某 batch 全部 job (給 sync_batches_repo 收尾算 done/failed/txn 用)."""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {_COLS} FROM sync_jobs WHERE batch_id=? ORDER BY id",
            (batch_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def mark_running(job_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE sync_jobs SET status='running', started_at=? WHERE id=?",
            (now_iso(), job_id),
        )


def mark_done(job_id: int, result_summary: str | None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE sync_jobs SET status='done', "
            "finished_at=?, result_summary=? WHERE id=?",
            (now_iso(), result_summary, job_id),
        )


def mark_failed(job_id: int, error_msg: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE sync_jobs SET status='failed', "
            "finished_at=?, error_msg=? WHERE id=?",
            (now_iso(), error_msg, job_id),
        )
