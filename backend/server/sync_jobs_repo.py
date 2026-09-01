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

import json
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, NamedTuple

from backend.server.db import get_conn, now_iso

_HISTORY_DOMAINS = frozenset({"twd_transactions", "card_billed_transactions"})


class StaleSweep(NamedTuple):
    swept_count: int
    batches: tuple[tuple[int, int], ...]


def _canonical_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _is_full_history_attestation(
    value: Any,
    *,
    expected_domains: frozenset[str],
) -> bool:
    if (
        not isinstance(value, dict)
        or not isinstance(expected_domains, frozenset)
        or not expected_domains
        or not expected_domains <= _HISTORY_DOMAINS
    ):
        return False
    domains = value.get("domains")
    start = _canonical_date(value.get("start"))
    end = _canonical_date(value.get("end"))
    return (
        value.get("ok") is True
        and value.get("mode") == "full"
        and isinstance(domains, list)
        and bool(domains)
        and all(
            isinstance(domain, str) and domain in _HISTORY_DOMAINS
            for domain in domains
        )
        and len(domains) == len(set(domains))
        and set(domains) == expected_domains
        and type(value.get("identities")) is int
        and value["identities"] >= 0
        and type(value.get("windows")) is int
        and value["windows"] >= max(value["identities"], 1)
        and start is not None
        and end is not None
        and start <= end
    )

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


def list_recent_for_user(
    user_id: int,
    limit: int = 50,
    *,
    sweep: bool = True,
) -> list[dict[str, Any]]:
    # 2026-06-22: sweep stale running jobs 才回 jobs list, 避免殭屍 job 卡 UI.
    # 不傳 user_id — sweep 是 global cleanup (殭屍 job 任何 user 撈到 jobs 都該清乾淨).
    if sweep:
        sweep_stale_running()
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {_COLS} FROM sync_jobs WHERE user_id=? "
            "ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_queued_ids(limit: int = 1000) -> list[int]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM sync_jobs WHERE status='queued' ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
    return [int(row[0]) for row in rows]


def has_completed_for_account(account_id: int) -> bool:
    """A failed/aborted first sync does not turn later retries into incremental syncs."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM sync_jobs "
            "WHERE account_id=? AND status='done' LIMIT 1",
            (account_id,),
        ).fetchone()
    return row is not None


def has_completed_full_history_for_account(
    account_id: int,
    *,
    expected_domains: frozenset[str],
) -> bool:
    """Only an attested full-history job unlocks later incremental syncs."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT result_summary FROM sync_jobs "
            "WHERE account_id=? AND status='done' AND history_mode='full'",
            (account_id,),
        ).fetchall()
    for row in rows:
        try:
            summary = json.loads(row[0] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(summary, dict):
            continue
        if _is_full_history_attestation(
            summary.get("history_coverage"), expected_domains=expected_domains,
        ):
            return True
    return False


def sweep_stale_queued() -> StaleSweep:
    mode = os.environ.get("SYNC_EXECUTION_MODE", "inprocess").strip().lower()
    stale_hours = 7 if mode == "external" else 1
    threshold = (datetime.now(timezone.utc) - timedelta(hours=stale_hours)).isoformat()
    error = f"stale_queue: worker did not claim within {stale_hours} hour{'s' if stale_hours != 1 else ''}"
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE sync_jobs SET status='failed', finished_at=?, error_msg=? "
            "WHERE status='queued' AND created_at < ? "
            "AND (batch_id IS NULL OR batch_id IN "
            "(SELECT id FROM sync_batches WHERE kind='manual_all')) "
            "RETURNING batch_id, user_id",
            (now_iso(), error, threshold),
        )
        rows = cur.fetchall()
    batches = tuple(sorted({(int(row[0]), int(row[1])) for row in rows if row[0] is not None}))
    return StaleSweep(len(rows), batches)


def sweep_stale_scheduled_queued() -> StaleSweep:
    threshold = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    error = "stale_queue: scheduled worker did not claim within 7 hours"
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE sync_jobs SET status='failed', finished_at=?, error_msg=? "
            "WHERE status='queued' AND created_at < ? "
            "AND batch_id IN "
            "(SELECT id FROM sync_batches WHERE kind='scheduled_all') "
            "RETURNING batch_id, user_id",
            (now_iso(), error, threshold),
        )
        rows = cur.fetchall()
    batches = tuple(sorted({(int(row[0]), int(row[1])) for row in rows if row[0] is not None}))
    return StaleSweep(len(rows), batches)


def sweep_stale_running() -> StaleSweep:
    """Mark running jobs older than the active execution mode allows as failed.

    2026-06-22: container restart / OOM / thread crash 期間正在跑的 job 沒人走到
    mark_done/mark_failed, status='running' 永遠卡死, frontend `hasRunningJob`
    永遠 true → 顯示「同步中」即使通知早就收到.

    解法: GET /sync/jobs 路徑開頭一次性 sweep, 沒有 cron 也能自動清.
    Standalone uses 15 minutes; external Container Apps Jobs use 7 hours,
    one hour beyond their six-hour replica timeout.
    error_msg 寫死 'stale_sweep: ...' 識別性高,
    debug 時 grep 一翻兩瞪眼.

    回傳 swept rows and affected batches so callers can finalize summaries.
    """
    mode = os.environ.get("SYNC_EXECUTION_MODE", "inprocess").strip().lower()
    stale_seconds = 7 * 60 * 60 if mode == "external" else _STALE_THRESHOLD_SECONDS
    threshold = (
        datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)
    ).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE sync_jobs SET status='failed', finished_at=?, "
            "error_msg=? WHERE status='running' AND started_at IS NOT NULL "
            "AND started_at < ? RETURNING batch_id, user_id",
            (now_iso(), f"stale_sweep: job stuck >{stale_seconds}s "
                        "without mark_done/mark_failed (container restart / crash?)",
             threshold),
        )
        rows = cur.fetchall()
    batches = tuple(sorted({(int(row[0]), int(row[1])) for row in rows if row[0] is not None}))
    return StaleSweep(len(rows), batches)


def list_by_batch(batch_id: int) -> list[dict[str, Any]]:
    """撈某 batch 全部 job (給 sync_batches_repo 收尾算 done/failed/txn 用)."""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {_COLS} FROM sync_jobs WHERE batch_id=? ORDER BY id",
            (batch_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def claim_queued(job_id: int) -> bool:
    """Atomically claim one queued job across API and Container Apps Job processes."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE sync_jobs SET status='running', started_at=? "
            "WHERE id=? AND status='queued'",
            (now_iso(), job_id),
        )
        return (cur.rowcount or 0) == 1


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
