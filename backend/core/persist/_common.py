"""Shared persist helpers (跨銀行共用 helper)。

從 persist.py 拆分而來 (2026-06-17, W persist.py split refactor)。
這層只放真正跨 2+ 家 bank 共用的純 helper, 單家用 helper 跟著該 bank 走。
"""
from __future__ import annotations

from datetime import datetime

from backend.core.money import native_money


def _num(s) -> int | None:
    """台幣金額字串（'40,956' / '-15' / ''）→ int；空字串/None → None。
    台幣入帳金額無小數，用整數。"""
    value = native_money(s, "TWD", optional=True)
    return value if isinstance(value, int) else None

def _num_real(s) -> float | int | None:
    """原幣金額，最多六位小數；空字串/None → None。"""
    return native_money(s, "USD", optional=True)

def _num_to_float(s) -> float | None:
    """'300,000' / 300000 / '344,282' / None → float | None."""
    value = native_money(s, "USD", optional=True)
    if value is None:
        return None
    result = float(value)
    if native_money(result, "USD") != value:
        raise ValueError("invalid native monetary value")
    return result

def _slash_date_to_iso(s: str | None) -> str | None:
    """'2026/06/05' / '2026/6/5' → '2026-06-05'. CTBC 用."""
    if not s:
        return None
    try:
        parts = str(s).strip().replace("-", "/").split("/")
        if len(parts) != 3:
            return None
        y, mo, d = int(parts[0]), int(parts[1]), int(parts[2])
        return f"{y:04d}-{mo:02d}-{d:02d}"
    except Exception:
        return None

def _ubot_date(s: str | None) -> str | None:
    """聯邦日期：'2026/05/16' 或 '20260515' → 'YYYY-MM-DD'。"""
    if not s:
        return None
    text = str(s).strip().replace("/", "").replace("-", "")
    if len(text) != 8 or not text.isdigit() or text == "00000000":
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _roc_to_west(s: str | None) -> str | None:
    """民國年轉西元：'115/05/16' → '2026-05-16'；'115/5/16' 也支援。

    esun / fubon 共用 (fubon 透過 _parse_fubon_credit_card 內呼叫)。
    """
    if not s:
        return None
    try:
        parts = s.replace("-", "/").strip().split("/")
        if len(parts) != 3:
            return s
        roc_y = int(parts[0])
        mo = int(parts[1])
        d = int(parts[2])
        west_y = roc_y + 1911
        return f"{west_y:04d}-{mo:02d}-{d:02d}"
    except Exception:
        return s
