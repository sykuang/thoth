"""CTW01002 row normalization; posting date remains explicitly unknown.

Caller must establish selected-account/inventory and fresh request/response binding.
Query windows are context, not constraints on bank-reported transaction dates.
Source: 9413 share timestamp; 7187 Chinese DB/CR help; schema_summary profiles.
Only observed nonnegative integer money is accepted, bounded by current PG model.
"""

from __future__ import annotations
from backend.core.error_codes import with_error_code
from datetime import date, datetime
import re
from typing import TypedDict


class TwdCandidate(TypedDict):
    account_no: str
    datetime: str
    account_date: None
    desc: str
    expend: int | None
    income: int | None
    balance: int


def normalize_row(row: dict, *, account_no: str, currency: str, start: date, end: date) -> TwdCandidate:
    if type(row) is not dict:
        raise with_error_code(ValueError('invalid CTW01002 context'), 'ESUN-07-416-001')
    if type(account_no) is not str:
        raise with_error_code(ValueError('invalid CTW01002 context'), 'ESUN-07-416-002')
    if re.fullmatch("[0-9]{13}", account_no) is None:
        raise with_error_code(ValueError('invalid CTW01002 context'), 'ESUN-07-416-003')
    if currency != "TWD":
        raise with_error_code(ValueError('invalid CTW01002 context'), 'ESUN-07-416-004')
    if type(start) is not date:
        raise with_error_code(ValueError('invalid CTW01002 context'), 'ESUN-07-416-005')
    if type(end) is not date:
        raise with_error_code(ValueError('invalid CTW01002 context'), 'ESUN-07-416-006')
    if start > end:
        raise with_error_code(ValueError('invalid CTW01002 context'), 'ESUN-07-416-007')
    if "demandDeptAcc" in row and row["demandDeptAcc"] != account_no:
        raise with_error_code(ValueError('CTW01002 row account mismatch'), 'ESUN-07-417-001')
    day, time = (row.get("txDate"), row.get("txTime"))
    if type(day) is not str:
        raise with_error_code(ValueError('invalid CTW01002 transaction timestamp'), 'ESUN-07-418-001')
    if re.fullmatch("[0-9]{4}/[0-9]{2}/[0-9]{2}", day) is None:
        raise with_error_code(ValueError('invalid CTW01002 transaction timestamp'), 'ESUN-07-418-002')
    if type(time) is not str:
        raise with_error_code(ValueError('invalid CTW01002 transaction timestamp'), 'ESUN-07-418-003')
    if re.fullmatch("[0-9]{2}:[0-9]{2}:[0-9]{2}", time) is None:
        raise with_error_code(ValueError('invalid CTW01002 transaction timestamp'), 'ESUN-07-418-004')
    try:
        stamp = datetime.strptime(day + " " + time, "%Y/%m/%d %H:%M:%S")
    except ValueError:
        raise with_error_code(ValueError("invalid CTW01002 transaction timestamp"), 'ESUN-07-421-001') from None
    title, direction = (row.get("detailTitle"), row.get("debitCredit"))
    if type(title) is not str:
        raise with_error_code(ValueError('invalid CTW01002 description or direction'), 'ESUN-07-419-001')
    if not title.strip():
        raise with_error_code(ValueError('invalid CTW01002 description or direction'), 'ESUN-07-419-002')
    if direction not in ("DB", "CR"):
        raise with_error_code(ValueError('invalid CTW01002 description or direction'), 'ESUN-07-419-003')
    amount, balance = (row.get("amount"), row.get("balance"))
    rejection = None

    def invalid_money(x, codes):
        nonlocal rejection
        if type(x) is not int:
            rejection = codes[0]
        elif not 0 <= x <= 2147483647:
            rejection = codes[1]
        else:
            return False
        return True

    if any(invalid_money(x, codes) for x, codes in zip((amount, balance), (
        ('ESUN-07-450-001', 'ESUN-07-450-002'),
        ('ESUN-07-451-001', 'ESUN-07-451-002'),
    ))):
        raise with_error_code(ValueError('unsupported CTW01002 money representation'), rejection)
    return {
        "account_no": account_no,
        "datetime": stamp.strftime("%Y-%m-%d %H:%M:%S"),
        "account_date": None,
        "desc": title,
        "expend": amount if direction == "DB" else None,
        "income": amount if direction == "CR" else None,
        "balance": balance,
    }
