"""User preferences router (Phase 6).

User preferences router（Phase 6）—— per-user 顯示偏好。

設計策略:
  一張 user_preferences 表, payload 用 JSON, 避免每加一個 user setting
  就 ALTER TABLE。當前 schema:
    {
      "fx_display_mode": "auto" | "always_twd" | "always_original"
    }

  - auto (default, MoneyBook 風): 原幣 ≠ TWD 時 UI 顯示原幣, 否則顯示 TWD
  - always_twd: 全部換算為 TWD 顯示 (外幣顯示銀行端 TWD 結算金額)
  - always_original: 外幣顯示原幣 (台幣帳戶仍 TWD)

Endpoints:
  GET  /users/me/preferences   → { fx_display_mode: ... }
  PUT  /users/me/preferences   body={ fx_display_mode: ... } → 200 same shape

Plan B (2026-06-19): SQL 抽到 PreferencesRepo (server DB Repo pattern,
跟 creds_store 平行). Router 只負責 default merge + validate + HTTP.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel

from backend.server.deps import current_user
from backend.server import preferences_repo

router = APIRouter(prefix="/users/me/preferences", tags=["preferences"])


# Default payload — 任何欄位若 user 沒設, 都用這份 default 補
DEFAULT_PREFERENCES: dict[str, Any] = {
    "fx_display_mode": "auto",
    # 信用卡明細/統計使用哪個日期 — 'consume' (消費日, 預設) / 'post' (入帳日).
    # 會影響 /transactions 的日期篩選、排序、月份歸屬與 list row 顯示。
    "card_date_basis": "consume",
}

VALID_FX_MODES = ("auto", "always_twd", "always_original")
VALID_CARD_DATE_BASIS = ("consume", "post")


class PreferencesPayload(BaseModel):
    """PUT body schema. 所有欄位 optional, 缺欄就保留 DB 既有值 (partial update)."""

    fx_display_mode: Literal["auto", "always_twd", "always_original"] | None = None
    card_date_basis: Literal["consume", "post"] | None = None


def _load_with_defaults(user_id: int) -> dict[str, Any]:
    """從 DB 撈該 user 的偏好, 與 DEFAULT 合併後回傳完整 dict。"""
    stored = preferences_repo.get_payload(user_id)
    return {**DEFAULT_PREFERENCES, **stored}


@router.get("")
def get_preferences(user: dict = Depends(current_user)) -> dict[str, Any]:
    """回傳 user 的完整偏好 (合併 default)。第一次呼叫不會建 row, 直接回 default。"""
    return _load_with_defaults(user["id"])


@router.put("")
def update_preferences(
    body: PreferencesPayload = Body(...),
    user: dict = Depends(current_user),
) -> dict[str, Any]:
    """Partial update: 只有 body 裡 non-None 的欄位會覆寫, 其餘保留 DB 舊值。

    回傳合併後的完整 dict (與 GET 同 shape)。
    """
    # 從 DB 拿現有 (含 default merge)
    current = _load_with_defaults(user["id"])

    # 套上 body 裡 non-None 的欄位
    incoming = body.model_dump(exclude_none=True)

    # 額外 enum validate (Pydantic Literal 已擋, 但 defense-in-depth)
    if "fx_display_mode" in incoming and incoming["fx_display_mode"] not in VALID_FX_MODES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"fx_display_mode 必須是 {VALID_FX_MODES} 之一",
        )
    if "card_date_basis" in incoming and incoming["card_date_basis"] not in VALID_CARD_DATE_BASIS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"card_date_basis 必須是 {VALID_CARD_DATE_BASIS} 之一",
        )

    merged = {**current, **incoming}
    preferences_repo.upsert_payload(user["id"], merged)
    return merged
