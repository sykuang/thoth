from datetime import date

import pytest

from backend.banks.ctbc import _filter_valid_ctbc_details, _validated_ctbc_detail


def test_native_form_datetime_is_canonicalized():
    # Live 2026-10-02: native qu002 form returns "YYYY-MM-DD HH:MM:SS.ffff".
    row = _validated_ctbc_detail(
        {"actDtTm": "2026-09-03 10:11:12.1234", "trnDtRaw": "20260903", "dbAmt": 0, "crAmt": 100},
        start=date(2026, 9, 1), end=date(2026, 9, 30),
    )
    assert row["actDtTm"] == "2026-09-03-10.11.12.1234"


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("field", ["actDtTm", "trnDtRaw"])
@pytest.mark.parametrize("bank_date", ["1999-01-01", "2099-01-01"])
def test_bank_dates_outside_window_are_preserved(native, field, bank_date):
    raw = {"actDtTm": "2026-09-03-10.11.12.1234", "trnDtRaw": "20260903", "dbAmt": 1, "crAmt": 0}
    raw[field] = bank_date + "-10.11.12.1234" if field == "actDtTm" else bank_date.replace("-", "")
    expected_datetime = raw["actDtTm"]
    if native:
        raw["actDtTm"] = raw["actDtTm"][:10] + " " + raw["actDtTm"][11:19].replace(".", ":") + raw["actDtTm"][19:]

    rows, skipped = _filter_valid_ctbc_details(
        [raw], start=date(2026, 9, 1), end=date(2026, 9, 30),
    )

    assert skipped == 0
    assert len(rows) == 1
    assert rows[0]["actDtTm"] == expected_datetime
    assert rows[0]["trnDtRaw"] == raw["trnDtRaw"]


@pytest.mark.parametrize("field,value", [
    ("actDtTm", "2026-02-30-10.11.12"),
    ("actDtTm", "2026-09-03-25.11.12"),
    ("trnDtRaw", "20260230"),
    ("trnDtRaw", "not-a-date"),
])
def test_malformed_bank_dates_still_rejected(field, value):
    raw = {"actDtTm": "2026-09-03-10.11.12", "trnDtRaw": "20260903", "dbAmt": 1}
    raw[field] = value
    with pytest.raises(ValueError):
        _validated_ctbc_detail(raw, start=date(2026, 9, 1), end=date(2026, 9, 30))
