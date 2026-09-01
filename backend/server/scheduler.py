"""APScheduler integration — daily auto-sync per user (L13).

設計 (L13, 2026-06-23 使用者指示):
  * In-process BackgroundScheduler (uvicorn worker 同 process)
  * MemoryJobStore — preference 在 user_sync_preferences table, scheduler 只是
    runtime view, restart 一律從 DB reload (詳 reload_all_jobs)
  * Job id 命名: f"user-{user_id}" (一個 CronTrigger 可含 0-3 個 hour)
  * Fire 時 fan-out: 該 user 全部 has_creds=true 的 account 依序排同步
    (sync_runner.run_sync_job_for_account 自己開 daemon thread, 立刻回 job_id)
  * 失敗處理: APScheduler 跑 job 時自身 exception 不 retry; 每個 account 失敗
    走 sync_runner._send_sync_notification → push notification
  * Azure 設 SYNC_EXECUTION_MODE=external 時不啟動本 scheduler，由 Container Apps
    Jobs 接手；standalone/local 預設仍使用本 scheduler。

⚠️ APScheduler thread vs uvicorn lifecycle:
  - BackgroundScheduler.start() 開 daemon thread, uvicorn 接 SIGTERM 時必 shutdown
  - 多 replica 場景 (現在 minReplicas=1 不會撞): 每 replica 自己跑 scheduler →
    同 user 同時跑 N 份 fan-out. 將來擴 replica 必加 DB lock. Phase 1 不處理.

⚠️ Tests: 跑 unit test 時 BackgroundScheduler 不該真啟動 — get_scheduler() 純粹
工廠, 啟動由 app.py lifespan 控制. test 直接 mock / 用 dry-run helper.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from backend.server import user_sync_pref_repo

logger = logging.getLogger("backend.scheduler")

# Singleton instance — 由 app.py lifespan 啟動 / 停止
_scheduler: BackgroundScheduler | None = None
_lock = threading.Lock()


def in_process_enabled() -> bool:
    disabled = os.environ.get("THOTH_DISABLE_SCHEDULER", "").strip().lower()
    mode = os.environ.get("SYNC_EXECUTION_MODE", "inprocess").strip().lower()
    return disabled not in {"1", "true"} and mode != "external"


def get_scheduler() -> BackgroundScheduler:
    """Lazy singleton. 啟動由 caller (app.py lifespan) 決定."""
    global _scheduler
    if _scheduler is None:
        with _lock:
            if _scheduler is None:
                _scheduler = BackgroundScheduler(
                    job_defaults={
                        "coalesce": True,  # 錯過 N 次 fire 合併成 1
                        "max_instances": 1,  # 同 job 同時只跑 1 份
                        "misfire_grace_time": 3600,  # 1 小時內錯過仍跑
                    },
                )
    return _scheduler


def _job_id(user_id: int) -> str:
    return f"user-{user_id}"


def _payment_reminder_job_id() -> str:
    return "payment-reminders-daily"


def _run_sync_for_user(user_id: int) -> None:
    """APScheduler 排程 fire 時呼此 fn — fan-out 該 user 全部 has_creds account.

    流程:
      1. 撈該 user 全部 bank_accounts.has_creds=true (用 AccountsRepo)
      2. 對每個 account 呼 sync_runner.run_sync_job_for_account 排同步
         (sync_runner 內部開 daemon thread, 立刻回 job_id)
      3. 標 last_run_at (純 timestamp, 不分 ok/fail)

    任何 exception 都吞 — APScheduler thread 不該炸到 scheduler.
    """
    try:
        # 延後 import 避免循環依賴 (sync_runner 依賴 scheduler 反過來不行)
        from backend.server import sync_batches_repo
        from backend.server.creds_store import AccountsRepo, LocalFernetBackend
        from backend.server.sync_runner import reconcile_batch_fanout, run_sync_job_for_account

        logger.info("[scheduler] fire user_id=%s", user_id)
        accounts = AccountsRepo().list_for_user(user_id)
        store = LocalFernetBackend()
        # has_creds = 該 account 在加密 vault 裡有任何欄位
        # (跟 GET /accounts router 算法一致)
        ready = []
        for a in accounts:
            try:
                fields = store.list_fields_acct(
                    a.id, expected_owner_user_id=user_id,
                )
                if fields:
                    ready.append(a)
            except Exception as exc:
                logger.warning(
                    "[scheduler] list_fields_acct failed user_id=%s account_id=%s error_type=%s",
                    user_id, a.id, type(exc).__name__,
                )
        logger.info(
            "[scheduler] user_id=%s fan-out: %d total, %d ready",
            user_id, len(accounts), len(ready),
        )

        # 2026-06-23 (Plan A): ready > 0 才建 batch, 共用 batch_id 給 summary push
        batch_id: int | None = None
        if ready:
            try:
                batch_id = sync_batches_repo.create(
                    user_id=user_id,
                    total_jobs=len(ready),
                    kind=sync_batches_repo.KIND_SCHEDULED_ALL,
                )
            except Exception as exc:
                logger.warning(
                    "[scheduler] sync_batches.create failed user_id=%s error_type=%s",
                    user_id, type(exc).__name__,
                )
                batch_id = None

        job_ids: list[int] = []
        try:
            for acct in ready:
                try:
                    job_id = run_sync_job_for_account(
                        account_id=acct.id, headless=True, batch_id=batch_id,
                    )
                    logger.info(
                        "[scheduler] queued user_id=%s account_id=%s job_id=%s batch_id=%s",
                        user_id, acct.id, job_id, batch_id,
                    )
                    job_ids.append(job_id)
                except Exception as exc:
                    logger.warning(
                        "[scheduler] queue failed user_id=%s account_id=%s error_type=%s",
                        user_id, acct.id, type(exc).__name__,
                    )
        finally:
            reconcile_batch_fanout(
                batch_id=batch_id,
                user_id=user_id,
                job_ids=job_ids,
            )

        user_sync_pref_repo.mark_last_run(user_id=user_id)
        logger.info(
            "[scheduler] fire complete user_id=%s queued=%d/%d batch_id=%s",
            user_id, len(job_ids), len(ready), batch_id,
        )
    except Exception as exc:
        logger.warning(
            "[scheduler] fire failed user_id=%s error_type=%s",
            user_id, type(exc).__name__,
        )


def _run_payment_reminders_for_all_users(tz: str = "Asia/Taipei") -> None:
    """Daily payment reminder push sweep. Never affects auto-sync success/failure."""
    try:
        from backend.server import payment_reminder_notifications as prn

        result = prn.dispatch_daily_payment_reminders_for_all_users(tz=tz)
        logger.info("[scheduler] payment reminders sweep result=%s", result)
    except Exception as exc:
        logger.warning(
            "[scheduler] payment reminders sweep failed error_type=%s",
            type(exc).__name__,
        )


def add_or_replace_for_user(pref: dict[str, Any]) -> None:
    """新增或取代一個 user 的 schedule. 對應 user 改 time / 開關 enabled.

    若 enabled=False → 改成 remove (不留 disabled job 浪費 scheduler).
    若 schedule 已存在 → APScheduler 同 id 直接覆寫 (replace_existing=True).
    """
    s = get_scheduler()
    user_id = pref["user_id"]
    jid = _job_id(user_id)

    slots = pref.get("slots")
    if slots is None:
        slots = (
            [f"{pref['hour']:02d}:{pref['minute']:02d}"]
            if pref.get("enabled", True)
            else []
        )
    if not slots:
        try:
            s.remove_job(jid)
            logger.info("[scheduler] removed (disabled) user_id=%s", user_id)
        except Exception:
            pass
        return

    hours = [int(slot.split(":", 1)[0]) for slot in slots]
    trigger = CronTrigger(
        hour=",".join(str(hour) for hour in hours),
        minute=0,
        timezone=pref.get("tz", "Asia/Taipei"),
    )
    s.add_job(
        _run_sync_for_user,
        trigger=trigger,
        id=jid,
        args=[user_id],
        replace_existing=True,
        name=f"daily-sync-user-{user_id}",
    )

    logger.info(
        "[scheduler] add/replace user_id=%s slots=%s %s",
        user_id, ",".join(slots),
        pref.get("tz", "Asia/Taipei"),
    )


def remove_for_user(user_id: int) -> None:
    """User 主動刪 schedule. 不抛 — 如果 scheduler 沒這 job 也 OK."""
    s = get_scheduler()
    try:
        s.remove_job(_job_id(user_id))
        logger.info("[scheduler] removed user_id=%s", user_id)
    except Exception:
        pass


def add_or_replace_payment_reminder_sweep() -> None:
    """Global daily sweep for payment reminder push (default 09:00 Asia/Taipei)."""
    s = get_scheduler()
    hour = int(os.environ.get("PAYMENT_REMINDER_HOUR", "9"))
    minute = int(os.environ.get("PAYMENT_REMINDER_MINUTE", "0"))
    tz = os.environ.get("PAYMENT_REMINDER_TZ", "Asia/Taipei")
    trigger = CronTrigger(hour=hour, minute=minute, timezone=tz)
    s.add_job(
        _run_payment_reminders_for_all_users,
        trigger=trigger,
        id=_payment_reminder_job_id(),
        args=[tz],
        replace_existing=True,
        name="daily-payment-reminders",
    )
    logger.info("[scheduler] add/replace daily payment reminders %02d:%02d %s", hour, minute, tz)


def reload_all_jobs() -> int:
    """Boot 時呼一次 — 從 DB 撈所有 enabled preference 進 scheduler.

    回傳載入幾筆.

    流程:
      1. clear 現有 jobs (避免 double-load)
      2. list_all_enabled → for each add_or_replace_for_user
    """
    s = get_scheduler()
    s.remove_all_jobs()
    prefs = user_sync_pref_repo.list_all_enabled()
    for p in prefs:
        add_or_replace_for_user(p)
    add_or_replace_payment_reminder_sweep()
    logger.info("[scheduler] reloaded %d schedules from DB", len(prefs))
    return len(prefs)


def start() -> None:
    """啟動 scheduler thread + load 全部 enabled preference.

    Idempotent — 重複呼 OK (內部 _scheduler.start() 重呼 raise
    SchedulerAlreadyRunningError, 我們吞掉).
    """
    s = get_scheduler()
    if s.running:
        logger.info("[scheduler] already running — skip start")
        return
    try:
        s.start()
        logger.info("[scheduler] started")
    except Exception as exc:
        logger.warning("[scheduler] start failed error_type=%s", type(exc).__name__)
        return
    reload_all_jobs()


def shutdown(wait: bool = False) -> None:
    """Uvicorn SIGTERM 時呼. wait=False 不等 in-flight job."""
    global _scheduler
    if _scheduler is None or not _scheduler.running:
        return
    try:
        _scheduler.shutdown(wait=wait)
        logger.info("[scheduler] shutdown (wait=%s)", wait)
    except Exception as exc:
        logger.warning("[scheduler] shutdown failed error_type=%s", type(exc).__name__)
    _scheduler = None


def list_jobs() -> list[dict[str, Any]]:
    """Debug / admin endpoint 用. 列當前 scheduler 內 jobs + next_run_time."""
    s = get_scheduler()
    if not s.running:
        return []
    return [
        {
            "id": j.id,
            "name": j.name,
            "next_run_time": j.next_run_time.isoformat() if j.next_run_time else None,
            "trigger": str(j.trigger),
        }
        for j in s.get_jobs()
    ]
