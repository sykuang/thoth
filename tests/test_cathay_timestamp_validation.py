"""Cathay raw timestamps must be valid before datetime can normalize offsets."""
from datetime import date

import pytest

from backend.banks.cathay import CathayCrawler


@pytest.mark.parametrize("offset", ["+08:60", "+0860", "-08:60", "-0860", "+00:99", "-0099"])
def test_response_date_rejects_overflowing_offset_minutes(offset):
    assert CathayCrawler._twd_response_date(f"2026-06-23T00:00:00{offset}") is None


@pytest.mark.parametrize("offset", ["+08:60", "+0860", "-08:60", "-0860", "+00:99", "-0099"])
def test_transaction_datetime_rejects_overflowing_offset_minutes(offset):
    with pytest.raises(ValueError, match="invalid datetime"):
        CathayCrawler._normalize_twd_datetime(f"2026-06-23T00:00:00{offset}")


@pytest.mark.parametrize("separator", ["T", " "])
@pytest.mark.parametrize("clock", ["00:30", "00:30:45", "00:30:45.1234567"])
@pytest.mark.parametrize("offset", ["", "Z", "+08:00", "+0800", "-08:00", "-0800", "+23:59", "-2359"])
def test_timestamp_parsers_preserve_existing_valid_encodings(separator, clock, offset):
    raw = f"2026-06-23{separator}{clock}{offset}"
    assert CathayCrawler._twd_response_date(raw) == date(2026, 6, 23)
    assert CathayCrawler._normalize_twd_datetime(raw) == raw


@pytest.mark.parametrize("raw", [
    "2026-02-30T00:00:00", "0000-00-00T00:00:00", "2026-06-23T24:00:00",
    "2026-06-23T00:60:00", "2026-06-23T00:00:60", "2026-06-23T00:00:00+24:00",
    "2026-06-23T00:00:00-2400", "2026-06-23T00:00:00+08:00:30",
    "2026-06-23T00:00.5", "2026-06-23T00:00:00junk", True, 20260623, None,
])
def test_timestamp_parsers_keep_existing_invalid_input_guards(raw):
    assert CathayCrawler._twd_response_date(raw) is None
    with pytest.raises(ValueError, match="invalid datetime"):
        CathayCrawler._normalize_twd_datetime(raw)


def test_date_only_and_whitespace_contracts_are_unchanged():
    assert CathayCrawler._twd_response_date("2024-02-29") == date(2024, 2, 29)
    assert CathayCrawler._twd_response_date("2026-02-29") is None
    assert CathayCrawler._twd_response_date(" 2026-06-23T00:00:00Z ") is None
    assert CathayCrawler._normalize_twd_datetime(" 2026-06-23T00:00:00Z ") == "2026-06-23T00:00:00Z"
    with pytest.raises(ValueError, match="invalid datetime"):
        CathayCrawler._normalize_twd_datetime("2026-06-23")


@pytest.mark.parametrize("offset", ["+08:60", "+0860", "-08:60", "-0860"])
@pytest.mark.parametrize("field,guard", [
    ("startDate", "account-mismatch"), ("endDate", "account-mismatch"),
    ("accountDate", "transaction-date"), ("txnDateTime", "transaction-date"),
])
def test_history_attestation_rejects_invalid_offsets_at_each_date_boundary(field, guard, offset):
    account = {
        "queryStatus": "SUCCESS", "accountNumber": "00001234", "count": 1,
        "startDate": "2026-06-23", "endDate": "2026-06-23",
        "details": [{"txnDateTime": "2026-06-23T00:00:00", "accountDate": "2026-06-23",
                     "expendAmt": "1"}],
    }
    target = account if field in ("startDate", "endDate") else account["details"][0]
    target[field] = f"2026-06-23T00:00:00{offset}"
    response = {"success": True, "returnCode": "0000", "content": {"datas": [account]}}
    with pytest.raises(RuntimeError, match=f"cathay-twd-history-{guard}"):
        CathayCrawler._validated_twd_account(
            response, "00001234", start=date(2026, 6, 23), end=date(2026, 6, 23),
        )
