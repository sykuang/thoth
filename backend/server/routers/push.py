"""User push token registration router (L11, 2026-06-22).

User push token registration router（L11，2026-06-22）。

Endpoints:
  PUT    /me/push-tokens       body={provider, token, platform?, device_label?}
                               → 註冊 / 刷新 (idempotent UPSERT on (provider, token))
  GET    /me/push-tokens       → list user 自己所有 device (token preview only, 不回 full token)
  DELETE /me/push-tokens/{id}  → 硬刪一筆 (登出某 device / 換手機)

Design notes:
  * frontend 在 _layout.tsx 取得 push token 後 PUT 進來 — 每次 boot 都 idempotent UPSERT
  * Server-side 從不回傳 full token (避免 list endpoint leak)
  * provider 由 client 決定 (frontend 知道自己拿的是 expo token / APNs raw)
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.server.deps import current_user
from backend.server.push import repo as push_repo

router = APIRouter(prefix="/me/push-tokens", tags=["push"])


# 認得這些 provider name (跟 backend/server/push/registry.py 同步)
ProviderName = Literal["apns", "webhook", "fcm", "expo"]
PlatformName = Literal["ios", "android", "web", "desktop"]


class RegisterRequest(BaseModel):
    provider: ProviderName
    token: str = Field(min_length=1, max_length=4096)
    platform: PlatformName | None = None
    device_label: str | None = Field(default=None, max_length=64)


class TokenInfo(BaseModel):
    id: int
    user_id: int
    provider: str
    token_preview: str
    platform: str | None
    device_label: str | None
    created_at: str
    last_used_at: str
    active: bool


class TokenListResponse(BaseModel):
    items: list[TokenInfo]
    count: int


@router.put("", response_model=TokenInfo, status_code=status.HTTP_200_OK)
def register_token(
    req: RegisterRequest,
    user: dict = Depends(current_user),
) -> dict:
    """註冊 / 刷新 push token (idempotent UPSERT)。

    iOS 重灌 / 換 Apple ID / 系統更新後 token 會變,
    frontend 每次 boot 都該呼叫一次 (這個 endpoint 是 idempotent 的)。
    """
    row = push_repo.register(
        user_id=user["id"],
        provider=req.provider,
        token=req.token,
        platform=req.platform,
        device_label=req.device_label,
    )
    if not row:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "註冊 push token 失敗",
        )
    return row


@router.get("", response_model=TokenListResponse)
def list_tokens(user: dict = Depends(current_user)) -> dict:
    """List user 自己的所有 device tokens (token 只回 preview 不回明文)。"""
    items = push_repo.list_devices_for_user(user["id"])
    return {"items": items, "count": len(items)}


@router.delete("/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_token(token_id: int, user: dict = Depends(current_user)) -> None:
    """硬刪一筆 device token (登出某 device / 拔老手機)。

    必驗 user_id ownership — 防止 user A 刪 user B 的 token。
    """
    ok = push_repo.remove(user_id=user["id"], token_id=token_id)
    if not ok:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "找不到該 device token",
        )
