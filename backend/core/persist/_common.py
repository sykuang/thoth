"""Shared persist helpers (跨銀行共用 helper)。

從 persist.py 拆分而來 (2026-06-17, W persist.py split refactor)。
這層只放真正跨 2+ 家 bank 共用的純 helper, 單家用 helper 跟著該 bank 走。
"""
from __future__ import annotations

from backend.core.error_codes import error_step, with_error_code
from datetime import datetime

from decimal import Decimal, InvalidOperation

from backend.core.money import MAX_SAFE_INTEGER, native_money


def _num(s, *, _codes: tuple[str, ...] | None = None) -> int | None:
    """台幣金額字串（'40,956' / '-15' / ''）→ int；空字串/None → None。
    台幣入帳金額無小數，用整數。"""
    with error_step('SYS-08-012-001'):
        value = native_money(s, "TWD", optional=True, _codes=_codes)
    return value if isinstance(value, int) else None

def _num_real(s, *, _codes: tuple[str, ...] | None = None) -> float | int | None:
    """原幣金額，最多六位小數；空字串/None → None。"""
    with error_step('SYS-08-013-001'):
        return native_money(s, "USD", optional=True, _codes=_codes)

def _num_to_float(s, *, _codes: tuple[str, ...] | None = None) -> float | None:
    """'300,000' / 300000 / '344,282' / None → float | None.

    Unparseable, non-finite, or out-of-range bank fields are unavailable (None)
    so callers keep saved values; a representable amount that would lose
    precision still raises.
    """
    with error_step(_codes[9] if _codes is not None and len(_codes) > 9 else None):
        if isinstance(s, bool):
            return None
        if s is not None:
            text = str(s).replace(",", "").replace(" ", "").strip()
            if text in {"", "-"}:
                return None
            try:
                with error_step('SYS-08-014-004'):
                    parsed = Decimal(text)
            except InvalidOperation:
                return None
            if not parsed.is_finite() or abs(parsed) > MAX_SAFE_INTEGER:
                return None
        with error_step('SYS-08-014-002'):
            value = native_money(s, "USD", optional=True, _codes=_codes)
        if value is None:
            return None
        with error_step('SYS-08-014-003'):
            result = float(value)
        if native_money(result, "USD", _codes=_codes) != value:
            raise with_error_code(ValueError("invalid native monetary value"), _codes[8] if _codes is not None else 'SYS-08-014-001')
        return result

def _slash_date_to_iso(s: str | None) -> str | None:
    """'2026/06/05' / '2026/6/5' → '2026-06-05'. CTBC 用."""
    if not s:
        return None
    try:
        parts = str(s).strip().replace("-", "/").split("/")
        if len(parts) != 3:
            return None
        with error_step('SYS-08-015-001'):
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
        with error_step('SYS-08-016-001'):
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
        with error_step('SYS-08-017-001'):
            roc_y = int(parts[0])
        with error_step('SYS-08-017-002'):
            mo = int(parts[1])
        with error_step('SYS-08-017-003'):
            d = int(parts[2])
        west_y = roc_y + 1911
        return f"{west_y:04d}-{mo:02d}-{d:02d}"
    except Exception:
        return s
