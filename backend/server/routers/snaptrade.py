"""Authenticated SnapTrade connection and read-only portfolio routes."""
from __future__ import annotations

from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.server.deps import current_user
from backend.server.dashboard_cache import clear_dashboard_cache
from backend.server.snaptrade import (
    SnapTradeBusy,
    SnapTradeInvalidCallback,
    SnapTradeNotConfigured,
    SnapTradeNotRegistered,
    SnapTradeService,
)

router = APIRouter(prefix="/snaptrade", tags=["snaptrade"])

_SYNC_ERROR_MARKERS = (
    ("connection 已停用", "connection_disabled"),
    ("帳戶回應為空", "accounts_empty"),
    ("缺少 transactions sync status", "transactions_status_missing"),
    ("holdings 尚未完成初次同步", "holdings_initial_sync_pending"),
    ("transactions 尚未完成初次同步", "transactions_initial_sync_pending"),
    ("transactions freshness 格式錯誤", "transactions_freshness_invalid"),
    ("transactions freshness 已過期", "transactions_freshness_stale"),
    ("回傳部分資料", "partial_snapshot"),
    ("response rows 格式錯誤", "response_rows_invalid"),
    ("response envelope 格式錯誤", "response_envelope_invalid"),
    ("pagination", "activities_pagination_invalid"),
    ("option position 缺少有效 multiplier", "option_multiplier_invalid"),
    ("position 缺少 symbol", "position_symbol_missing"),
    ("position 缺少 units", "position_units_missing"),
)


class ConnectRequest(BaseModel):
    redirect_uri: str = Field(min_length=8, max_length=2048)


def get_service() -> SnapTradeService:
    return SnapTradeService()


def _sync_error_code(error: Exception) -> str:
    message = str(error)
    for marker, code in _SYNC_ERROR_MARKERS:
        if marker in message:
            return code
    if message.startswith("SnapTrade HTTP "):
        return "upstream_http"
    return "unexpected_upstream"


def _raise_http(error: Exception, *, sync_error_code: str | None = None) -> NoReturn:
    if isinstance(error, SnapTradeNotConfigured):
        raise HTTPException(status_code=503, detail=str(error)) from error
    if isinstance(error, (SnapTradeBusy, SnapTradeNotRegistered)):
        raise HTTPException(status_code=409, detail=str(error)) from error
    if isinstance(error, SnapTradeInvalidCallback):
        raise HTTPException(status_code=400, detail=str(error)) from error
    headers = (
        {"X-SnapTrade-Error-Code": sync_error_code}
        if sync_error_code is not None
        else None
    )
    raise HTTPException(
        status_code=502,
        detail="SnapTrade 上游暫時無法使用",
        headers=headers,
    ) from error


@router.get("/status")
def status(user: dict = Depends(current_user)) -> dict[str, Any]:
    try:
        return get_service().status(user["id"])
    except Exception as error:
        _raise_http(error)


@router.post("/connect")
def connect(
    body: ConnectRequest,
    user: dict = Depends(current_user),
) -> dict[str, str]:
    try:
        return {
            "redirect_uri": get_service().connection_url(user["id"], body.redirect_uri),
        }
    except Exception as error:
        _raise_http(error)


@router.post("/sync")
def sync(user: dict = Depends(current_user)) -> dict[str, Any]:
    try:
        result = get_service().sync(user["id"])
        clear_dashboard_cache(user_id=user["id"], namespace="portfolio.summary")
        return result
    except Exception as error:
        _raise_http(error, sync_error_code=_sync_error_code(error))


@router.get("/portfolio")
def portfolio(user: dict = Depends(current_user)) -> dict[str, Any]:
    return get_service().snapshot(user["id"])
