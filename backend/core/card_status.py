"""Canonical credit-card status values shared by collectors and readers."""
from __future__ import annotations

from backend.core.error_codes import with_error_code
from enum import StrEnum
from typing import Any


class CathayBillStatus(StrEnum):
    PAID = "paid"
    UNPAID = "unpaid"


def cathay_bill_status(value: Any, *, strict: bool = False) -> CathayBillStatus | None:
    """Normalize Cathay's observed status spellings; reject unknown values."""
    token = str(value or "").strip().casefold()
    if token in {"paid", "payed"}:
        return CathayBillStatus.PAID
    # Cathay's bill overview renders「我要繳費」for the live Payment status.
    if token in {"unpaid", "payment"}:
        return CathayBillStatus.UNPAID
    if strict:
        raise with_error_code(ValueError(f"unsupported Cathay bill status: {value!r}"), 'CATHAY-08-200-001')
    return None
