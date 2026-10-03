from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any
import re

MAX_SAFE_INTEGER = Decimal("9007199254740991")


def native_money(
    value: Any,
    currency: Any,
    *,
    optional: bool = False,
    absolute: bool = False,
    preserve_decimal: bool = False,
) -> int | float | Decimal | None:
    """Validate and normalize one native-currency monetary value."""
    if value is None or (optional and str(value).strip() in {"", "-"}):
        if optional:
            return None
        raise ValueError("invalid native monetary value")
    if isinstance(value, bool):
        raise ValueError("invalid native monetary value")
    canonical = str(currency or "").strip().upper()
    if re.fullmatch(r"[A-Z]{3}", canonical) is None:
        raise ValueError("invalid native monetary value")
    try:
        amount = Decimal(str(value).replace(",", "").replace(" ", "").strip())
    except (InvalidOperation, ValueError):
        raise ValueError("invalid native monetary value") from None
    if not amount.is_finite() or abs(amount) > MAX_SAFE_INTEGER:
        raise ValueError("invalid native monetary value")
    if canonical == "TWD":
        if amount != amount.to_integral_value():
            raise ValueError("invalid native monetary value")
        result: int | float | Decimal = int(amount)
    else:
        if amount != amount.quantize(Decimal("0.000001")):
            raise ValueError("invalid native monetary value")
        if amount == amount.to_integral_value():
            result = int(amount)
        elif preserve_decimal:
            result = amount
        else:
            result = float(amount)
            if Decimal(str(result)) != amount:
                raise ValueError("invalid native monetary value")
    return abs(result) if absolute else result
