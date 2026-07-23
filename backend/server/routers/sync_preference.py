"""Auto-sync preference router (L13, 2026-06-23 使用者指示).

從 L12 per-account 改成 per-user 單一時間:
  使用者「我不是要每個銀行都有各自的時間 我要使用者設定一個時間給所有帳號」

Endpoints (per-user, ownership via current_user):
  GET    /me/sync-preference           → 取 current user 的 preference (或 null)
  PUT    /me/sync-preference           → upsert (hour/minute/tz/enabled)
  DELETE /me/sync-preference           → hard delete (回 null state)
  GET    /me/sync-preference/_debug    → APScheduler in-memory jobs (debug)

Design:
  * 1 user = 1 schedule (user_id PK).
  * Daily HH:MM only; 沒 cron expression.
  * 寫 DB + 同步呼 scheduler.add_or_replace_for_user / scheduler.remove_for_user
    讓改動立刻生效 (不等下次 backend restart).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.server import scheduler, user_sync_pref_repo
from backend.server.deps import current_user

router = APIRouter(prefix="/me/sync-preference", tags=["sync-preference"])


# ============================================================
# Pydantic models
# ============================================================

class SyncPreferenceUpsertRequest(BaseModel):
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)
    tz: str = Field(default="Asia/Taipei", max_length=64)
    enabled: bool = True


class SyncPreferenceInfo(BaseModel):
    user_id: int
    hour: int
    minute: int
    tz: str
    enabled: bool
    last_run_at: str | None
    created_at: str
    updated_at: str


class SchedulerJobDebugInfo(BaseModel):
    id: str
    name: str
    next_run_time: str | None
    trigger: str


class SchedulerDebugResponse(BaseModel):
    items: list[SchedulerJobDebugInfo]
    count: int


# ============================================================
# Routes
# ============================================================

@router.get("", response_model=SyncPreferenceInfo | None)
def get_preference(user: dict = Depends(current_user)) -> dict | None:
    """Get the current user's auto-sync preference (or null if never set)."""
    return user_sync_pref_repo.get(user["id"])


@router.get("/_debug", response_model=SchedulerDebugResponse)
def debug_scheduler_jobs(user: dict = Depends(current_user)) -> dict:
    """[Debug] Inspect APScheduler in-memory jobs (admin/debug)."""
    jobs = scheduler.list_jobs()
    return {"items": jobs, "count": len(jobs)}


@router.put(
    "",
    response_model=SyncPreferenceInfo,
    status_code=status.HTTP_200_OK,
)
def upsert_preference(
    req: SyncPreferenceUpsertRequest,
    user: dict = Depends(current_user),
) -> dict:
    """Create or update the auto-sync preference for the current user.

    Validates the tz string BEFORE writing DB (so an invalid tz never sticks
    around and breaks scheduler reload on next boot). Then writes the DB row
    + immediately reflects in the in-process APScheduler.
    """
    # Validate timezone before DB write — invalid tz must NOT persist
    # (else next boot's reload_all_jobs() crashes on CronTrigger init).
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(req.tz)
    except Exception as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"無效時區: {req.tz}",
        ) from e

    row = user_sync_pref_repo.upsert(
        user_id=user["id"],
        hour=req.hour,
        minute=req.minute,
        tz=req.tz,
        enabled=req.enabled,
    )
    try:
        scheduler.add_or_replace_for_user(row)
    except Exception as e:
        # 已 validate tz 了, 這層 raise 應該很少見 — 保險還是擋
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"排程設定無效: {e}",
        ) from e
    return row


@router.delete(
    "",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_preference(
    user: dict = Depends(current_user),
) -> None:
    """Hard delete the schedule for current user.

    Caller can PUT with enabled=False to soft-disable (keep the time values
    for future re-enable). DELETE wipes the row entirely.
    """
    user_sync_pref_repo.delete(user["id"])
    scheduler.remove_for_user(user["id"])
