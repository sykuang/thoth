"""Canonical credit-card status values shared by collectors and readers."""
from __future__ import annotations

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
    if token == "unpaid":
        return CathayBillStatus.UNPAID
    if strict:
        raise ValueError(f"unsupported Cathay bill status: {value!r}")
    return None
