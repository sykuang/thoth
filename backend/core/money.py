from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any
import re

from backend.core.error_codes import error_step, with_error_code

MAX_SAFE_INTEGER = Decimal("9007199254740991")


def native_money(
    value: Any,
    currency: Any,
    *,
    optional: bool = False,
    absolute: bool = False,
    preserve_decimal: bool = False,
    _codes: tuple[str, ...] | None = None,
) -> int | float | Decimal | None:
    """Validate money; private codes retain the caller's field at first rejection."""
    codes = _codes if _codes is not None else (
        'SYS-08-001-001', 'SYS-08-001-002', 'SYS-08-001-003',
        'SYS-08-001-004', 'SYS-08-001-005', 'SYS-08-001-006',
        'SYS-08-001-007', 'SYS-08-001-008', 'SYS-08-001-009',
    )
    with error_step(_codes[9] if _codes is not None and len(_codes) > 9 else None):
        if value is None or (optional and str(value).strip() in {"", "-"}):
            if optional:
                return None
            raise with_error_code(ValueError("invalid native monetary value"), codes[0])
        if isinstance(value, bool):
            raise with_error_code(ValueError("invalid native monetary value"), codes[1])
        canonical = str(currency or "").strip().upper()
        if re.fullmatch(r"[A-Z]{3}", canonical) is None:
            raise with_error_code(ValueError("invalid native monetary value"), codes[2])
        try:
            amount = Decimal(str(value).replace(",", "").replace(" ", "").strip())
        except (InvalidOperation, ValueError):
            raise with_error_code(ValueError("invalid native monetary value"), codes[3]) from None
        if not amount.is_finite():
            raise with_error_code(ValueError("invalid native monetary value"), codes[4])
        if abs(amount) > MAX_SAFE_INTEGER:
            raise with_error_code(ValueError("invalid native monetary value"), codes[5])
        if canonical == "TWD":
            if amount != amount.to_integral_value():
                raise with_error_code(ValueError("invalid native monetary value"), codes[6])
            result: int | float | Decimal = int(amount)
        else:
            if amount != amount.quantize(Decimal("0.000001")):
                raise with_error_code(ValueError("invalid native monetary value"), codes[7])
            if amount == amount.to_integral_value():
                result = int(amount)
            elif preserve_decimal:
                result = amount
            else:
                result = float(amount)
                if Decimal(str(result)) != amount:
                    raise with_error_code(ValueError("invalid native monetary value"), codes[8])
        return abs(result) if absolute else result
