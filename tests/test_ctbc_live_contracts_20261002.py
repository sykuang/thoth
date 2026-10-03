from datetime import date

from backend.banks.ctbc import _validated_ctbc_detail


def test_native_form_datetime_is_canonicalized():
    # Live 2026-10-02: native qu002 form returns "YYYY-MM-DD HH:MM:SS.ffff".
    row = _validated_ctbc_detail(
        {"actDtTm": "2026-09-03 10:11:12.1234", "trnDtRaw": "20260903", "dbAmt": 0, "crAmt": 100},
        start=date(2026, 9, 1), end=date(2026, 9, 30),
    )
    assert row["actDtTm"] == "2026-09-03-10.11.12.1234"


def test_native_form_datetime_outside_window_rejected():
    import pytest
    with pytest.raises(ValueError):
        _validated_ctbc_detail(
            {"actDtTm": "2026-10-03 10:11:12.1234", "dbAmt": 1, "crAmt": 0},
            start=date(2026, 9, 1), end=date(2026, 9, 30),
        )
