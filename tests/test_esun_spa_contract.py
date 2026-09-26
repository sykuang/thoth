"""Synthetic request/row contracts promoted with the native runtime."""

from datetime import date

import pytest

from backend.banks.esun_spa.collection import bind_continuation
from backend.banks.esun_spa.rows import normalize_row


ROW = dict(debitCredit="CR", detailTitle="SYNTHETIC", txDate="2026/09/20", txTime="12:00:00", amount=1, balance=1)


def test_response_cursor_and_duplicate_merge():
    group = dict(year="2026", month="9", detailInfo=[ROW])
    previous = dict(startIndex=7, count=3, detailListData=[group])
    request = dict(
        account="0000000000001", startDate="START", endDate="END", startIndex=1, count=100, customerInputHashtag=[]
    )
    issued = dict(request, startIndex=10, count=3)
    body = dict(startIndex=10, count=2, detailListData=[group])
    assert bind_continuation(previous, request, body, issued)[0]["detailInfo"] == [ROW, ROW]
    assert previous["detailListData"][0]["detailInfo"] == [ROW]
    for key, value in [
        ("account", "OTHER"),
        ("startDate", "OTHER"),
        ("endDate", "OTHER"),
        ("customerInputHashtag", ["OTHER"]),
        ("startIndex", 101),
        ("count", 100),
    ]:
        with pytest.raises(ValueError):
            bind_continuation(previous, request, body, dict(issued, **{key: value}))
    for bad in [
        dict(body, startIndex=7),
        dict(body, startIndex=True),
        dict(body, count=0),
        dict(queryDeptTxDtlResult=body),
        dict(body, displayErrorCode="PRIVATE"),
    ]:
        with pytest.raises(ValueError):
            bind_continuation(previous, request, bad, issued)
    assert bind_continuation(previous, request, dict(body, detailListData=[]), issued) == [group]


def normalize(row):
    return normalize_row(row, account_no="0000000000001", currency="TWD", start=date(2026, 9, 1), end=date(2026, 9, 30))


def test_row_direction_and_unknown_posting_date():
    result = normalize(ROW)
    assert result["income"] == 1 and result["expend"] is None
    assert result["datetime"] == "2026-09-20 12:00:00" and result["account_date"] is None
    assert normalize(dict(ROW, debitCredit="DB"))["expend"] == 1
    assert normalize(dict(ROW, amount=0))["income"] == 0


@pytest.mark.parametrize(
    "change",
    [
        {"debitCredit": "debit"},
        {"debitCredit": "credit"},
        {"debitCredit": None},
        {"amount": True},
        {"amount": "7"},
        {"amount": 7.0},
        {"amount": -1},
        {"amount": 2147483648},
        {"balance": None},
        {"balance": -1},
        {"txDate": "2026/02/30"},
        {"txDate": "2026/08/31"},
        {"txDate": "2026/9/01"},
        {"txTime": "24:00:00"},
        {"detailTitle": ""},
        {"demandDeptAcc": "0000000000002"},
    ],
)
def test_unsupported_rows_are_rejected(change):
    with pytest.raises(ValueError):
        normalize(dict(ROW, **change))
