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


class ConnectRequest(BaseModel):
    redirect_uri: str = Field(min_length=8, max_length=2048)


def get_service() -> SnapTradeService:
    return SnapTradeService()


def _raise_http(error: Exception) -> NoReturn:
    if isinstance(error, SnapTradeNotConfigured):
        raise HTTPException(status_code=503, detail=str(error)) from error
    if isinstance(error, (SnapTradeBusy, SnapTradeNotRegistered)):
        raise HTTPException(status_code=409, detail=str(error)) from error
    if isinstance(error, SnapTradeInvalidCallback):
        raise HTTPException(status_code=400, detail=str(error)) from error
    raise HTTPException(status_code=502, detail="SnapTrade 上游暫時無法使用") from error


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
        _raise_http(error)


@router.get("/portfolio")
def portfolio(user: dict = Depends(current_user)) -> dict[str, Any]:
    return get_service().snapshot(user["id"])
