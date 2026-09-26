"""CTW01002 row normalization; posting date remains explicitly unknown.

Caller must establish selected-account/inventory and fresh request/response binding.
Window checks here concern transaction date only, not server window semantics.
Source: 9413 share timestamp; 7187 Chinese DB/CR help; schema_summary profiles.
Only observed nonnegative integer money is accepted, bounded by current PG model.
"""

from __future__ import annotations
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
    if (
        type(row) is not dict
        or type(account_no) is not str
        or re.fullmatch("[0-9]{13}", account_no) is None
        or (currency != "TWD")
        or (type(start) is not date)
        or (type(end) is not date)
        or (start > end)
    ):
        raise ValueError("invalid CTW01002 context")
    if "demandDeptAcc" in row and row["demandDeptAcc"] != account_no:
        raise ValueError("CTW01002 row account mismatch")
    day, time = (row.get("txDate"), row.get("txTime"))
    if (
        type(day) is not str
        or re.fullmatch("[0-9]{4}/[0-9]{2}/[0-9]{2}", day) is None
        or type(time) is not str
        or (re.fullmatch("[0-9]{2}:[0-9]{2}:[0-9]{2}", time) is None)
    ):
        raise ValueError("invalid CTW01002 transaction timestamp")
    try:
        stamp = datetime.strptime(day + " " + time, "%Y/%m/%d %H:%M:%S")
    except ValueError:
        raise ValueError("invalid CTW01002 transaction timestamp") from None
    if not start <= stamp.date() <= end:
        raise ValueError("CTW01002 transaction outside candidate window")
    title, direction = (row.get("detailTitle"), row.get("debitCredit"))
    if type(title) is not str or not title.strip() or direction not in ("DB", "CR"):
        raise ValueError("invalid CTW01002 description or direction")
    amount, balance = (row.get("amount"), row.get("balance"))
    if any((type(x) is not int or not 0 <= x <= 2147483647 for x in (amount, balance))):
        raise ValueError("unsupported CTW01002 money representation")
    return {
        "account_no": account_no,
        "datetime": stamp.strftime("%Y-%m-%d %H:%M:%S"),
        "account_date": None,
        "desc": title,
        "expend": amount if direction == "DB" else None,
        "income": amount if direction == "CR" else None,
        "balance": balance,
    }
