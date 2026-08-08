"""FX rate service (Phase 6 — 外幣帳戶 TWD 估值).

設計策略:
  - **主來源**: 台灣銀行官方匯率 CSV
      URL: https://rate.bot.com.tw/xrt/flcsv/0/day
      每列: <CCY>,本行買入,現金買入,即期買入,...,本行賣出,現金賣出,即期賣出,...
            col[0]=currency, col[3]=即期買入, col[13]=即期賣出
  - **取「即期買入 / 即期賣出」中間價** (col[3], col[13])
      作為外幣資產的中性 TWD 估值，不偏向任一交易方向。
      JPY/SEK/THB/ZAR/IDR 等小面額幣別 BoT 用「100 單位」報價 (rate.bot 把 100 JPY 一起報)
      他們 raw CSV 仍是 per-1-unit (JPY 0.20 對 TWD), 不需另外處理。
  - **Fallback**: open.er-api.com/v6/latest/TWD
      回 rates[X] = "1 TWD = N X", 反推 1/rate 得「1 X = ? TWD」
      免費 + 無 token + 24h 更新一次, 適合擋網路盲區。
  - **Cache**: in-memory dict + TTL 6 小時
      避免每次 frontend refresh 都打台銀 (會被封 IP)。
      module-level cache 在 server process lifetime 共用。

提供:
  - get_rate(currency: str) -> float | None        — 1 單位該幣 = 多少 TWD
  - convert_to_twd(amount, currency) -> int | None — 原幣 → TWD (int 四捨五入)

任何來源失敗就回 None, 不 raise — caller (router) 容忍 None 顯示 "—".
"""
from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import io
import math
import threading
import time
from datetime import datetime, UTC
from typing import Any

import httpx2 as httpx

# ============================================================
# Constants
# ============================================================

BOT_CSV_URL = "https://rate.bot.com.tw/xrt/flcsv/0/day"
ER_API_URL = "https://open.er-api.com/v6/latest/TWD"
CACHE_TTL_SECONDS = 6 * 60 * 60  # 6 小時
HTTP_TIMEOUT = 5.0  # 秒 — 失敗快, 不拖慢 endpoint

# BoT CSV 欄位 index (0-based, 確認自 https://rate.bot.com.tw/xrt/flcsv/0/day)
#   col[0]  = 幣別 (USD/JPY/CNY/...)
#   col[1]  = '本行買入' literal
#   col[2]  = 現金買入
#   col[3]  = 即期買入  ← spot buying ★
#   col[4..10] = 遠期 buying (10/30/60/90/120/150/180 天)
#   col[11] = '本行賣出' literal
#   col[12] = 現金賣出
#   col[13] = 即期賣出  ← spot selling ★
_BOT_SPOT_BUY_COL = 3
_BOT_SPOT_SELL_COL = 13


# ============================================================
# Cache
# ============================================================
#
# Module-level cache. 結構:
#   _cache = {
#     "fetched_at": <epoch_seconds>,
#     "source": "bank_of_taiwan" | "open_er_api",
#     "as_of": "<ISO datetime>",
#     "rates": {"USD": 31.62, "JPY": 0.19945, ...},
#   }
# 用 RLock 保護 race (FastAPI thread pool 可能多 thread 同時打)

_cache: dict[str, Any] | None = None
_cache_lock = threading.RLock()


def _is_cache_fresh() -> bool:
    if _cache is None:
        return False
    return (time.time() - _cache["fetched_at"]) < CACHE_TTL_SECONDS


def _clear_cache() -> None:
    """測試用：強制清掉 cache, 下次重打 source."""
    global _cache
    with _cache_lock:
        _cache = None


# ============================================================
# Source 1: 台灣銀行 CSV
# ============================================================

def _fetch_bot_csv() -> dict[str, Any] | None:
    """打台銀 CSV 解出 {currency: float} dict. 失敗回 None.

    返回:
        {"source": "bank_of_taiwan",
         "as_of": "<ISO datetime>",
         "rates": {"USD": 31.62, "JPY": 0.19945, ...}}
    """
    try:
        r = httpx.get(BOT_CSV_URL, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return None
        # BoT 回 UTF-8 with BOM, content 是 bytes
        text = r.content.decode("utf-8-sig", errors="replace")
    except (httpx.HTTPError, OSError):
        return None

    rates: dict[str, float] = {}
    reader = csv.reader(io.StringIO(text))
    next(reader, None)  # skip header row (幣別,匯率,...)
    for row in reader:
        if len(row) <= _BOT_SPOT_SELL_COL:
            continue
        ccy = (row[0] or "").strip().upper()
        if not ccy or len(ccy) != 3:
            continue
        try:
            buy = float(row[_BOT_SPOT_BUY_COL])
            sell = float(row[_BOT_SPOT_SELL_COL])
        except (ValueError, TypeError):
            continue
        if not math.isfinite(buy) or not math.isfinite(sell) or buy <= 0 or sell <= 0:
            continue
        rates[ccy] = round((buy + sell) / 2, 6)

    if not rates:
        return None

    return {
        "source": "bank_of_taiwan",
        "as_of": datetime.now(UTC).isoformat(),
        "rates": rates,
    }


# ============================================================
# Source 2: open.er-api.com (fallback)
# ============================================================

def _fetch_er_api() -> dict[str, Any] | None:
    """打 open.er-api.com fallback. 回相同 shape; 失敗回 None.

    open.er-api 回 base=TWD 的 rates[X] = "1 TWD = N X"
    要反推: 1 X = 1/N TWD → frontend 期待的 "1 單位該幣值多少 TWD"
    """
    try:
        r = httpx.get(ER_API_URL, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return None
        data = r.json()
    except (httpx.HTTPError, OSError, ValueError):
        return None

    if data.get("result") != "success":
        return None
    raw_rates = data.get("rates", {})
    if not isinstance(raw_rates, dict) or not raw_rates:
        return None

    rates: dict[str, float] = {}
    for ccy, val in raw_rates.items():
        try:
            v = float(val)
        except (ValueError, TypeError):
            continue
        if v <= 0:
            continue
        # 反推: 1 ccy = 1/v TWD
        rates[ccy.upper()] = round(1.0 / v, 6)

    if not rates:
        return None

    as_of = data.get("time_last_update_utc") or datetime.now(UTC).isoformat()
    return {
        "source": "open_er_api",
        "as_of": as_of,
        "rates": rates,
    }


# ============================================================
# Public API
# ============================================================

def get_rates() -> dict[str, Any] | None:
    """取目前 cached rates dict (主來源優先, fallback 次之).

    回 None 表示主+備兩來源都失敗 (網路掛 + 沒舊 cache).
    """
    global _cache

    with _cache_lock:
        if _is_cache_fresh():
            return _cache

        # cache miss / expired → 嘗試 refresh
        fresh = _fetch_bot_csv()
        if fresh is None:
            fresh = _fetch_er_api()

        if fresh is None:
            # 兩 source 都失敗：若有舊 cache, 退而求其次仍給舊資料 (穩定 > 新鮮)
            return _cache  # 可能是 None

        _cache = {
            "fetched_at": time.time(),
            **fresh,
        }
        return _cache


def get_rate(currency: str) -> float | None:
    """回 1 單位該外幣 = 多少 TWD. 找不到 / 拉不到回 None.

    TWD → 1.0 (恆等, 避免 caller 寫 if).
    """
    if not currency:
        return None
    ccy = currency.strip().upper()
    if ccy == "TWD":
        return 1.0
    bundle = get_rates()
    if bundle is None:
        return None
    rate = bundle["rates"].get(ccy)
    if rate is None:
        return None
    try:
        return float(rate)
    except (ValueError, TypeError):
        return None


def convert_to_twd(amount: float | int | str | None, currency: str | None) -> int | None:
    """原幣金額 → TWD 估值 (int 四捨五入).

    None 條件:
      - amount 是 None
      - currency 拉不到匯率 (源失敗 / 該幣別不存在)

    TWD 自身: 直接回 int(amount) (不過 caller 通常會自己 short-circuit).
    """
    if amount is None:
        return None
    if currency is None:
        return None
    rate = get_rate(currency)
    if rate is None:
        return None
    try:
        value = Decimal(str(amount)) * Decimal(str(rate))
        if not value.is_finite():
            return None
        return int(value.to_integral_value(rounding=ROUND_HALF_EVEN))
    except (InvalidOperation, ValueError, TypeError):
        return None
