"""FX rates router (Phase 6 — 給 frontend 顯示外幣帳戶 TWD 估值).

提供:
  GET /fx/rates → {
    "as_of": "2026-06-14T12:34:56+00:00",
    "source": "bank_of_taiwan" | "open_er_api",
    "base": "TWD",
    "rates": {"USD": 31.62, "JPY": 0.19945, "CNY": 4.6825, ...}
  }

  - rates[X] = 1 X 等於多少 TWD (給 frontend 算「外幣 → TWD 估值」用)
  - 來源失敗 → 503; 但 fx_service 內部會 fallback 到 open.er-api, 真正回 503 機會很低

設計鐵則:
  - 跟其他 portfolio endpoint 一致 require auth (Depends(current_user))
  - rates 抓不到 → HTTP 503 (服務不可用), frontend 容忍
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from backend.server.deps import current_user
from backend.server.fx_service import get_rates

router = APIRouter(prefix="/fx", tags=["fx"])


@router.get("/rates")
def fx_rates(user: dict = Depends(current_user)) -> dict[str, Any]:
    """回目前快取的匯率 dict, base=TWD.

    rates[X] = 1 X = N TWD (e.g. "JPY": 0.2 表示 1 JPY = 0.2 TWD).

    來源優先序: 台銀 CSV → open.er-api fallback.
    若兩源都掛 (且無舊 cache) → 503.
    """
    bundle = get_rates()
    if bundle is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "目前無法取得外匯匯率 (台銀 & open.er-api 都失敗)",
        )
    return {
        "as_of": bundle.get("as_of"),
        "source": bundle.get("source"),
        "base": "TWD",
        "rates": bundle.get("rates", {}),
    }
