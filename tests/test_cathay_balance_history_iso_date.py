"""Regression tests for cathay collector: normalize snapshotDate to IsoDate.

2026-07-03 (0.3.62): cathay balance_history 直接放 raw `dateBalanceList`，
snapshotDate 是 '2025-07-01T00:00:00' ISO datetime。0.3.61 typed 化後
`BankCollectResult.balance_history[i].snapshotDate` 只吃 YYYY-MM-DD，
會 raise ValueError → collect_failed，整包 cathay sync 掛掉。
"""
from __future__ import annotations

import pytest

from backend.banks.cathay import CathayCrawler
from backend.core.base import BankCollectResult


def test_cathay_normalize_iso_date_strips_time_component():
    assert CathayCrawler._normalize_iso_date("2025-07-01T00:00:00") == "2025-07-01"
    assert CathayCrawler._normalize_iso_date("2025/7/1") == "2025-07-01"
    assert CathayCrawler._normalize_iso_date("2025-07-01") == "2025-07-01"


def test_cathay_normalize_iso_date_handles_none_and_empty():
    assert CathayCrawler._normalize_iso_date(None) is None
    assert CathayCrawler._normalize_iso_date("") is None
    assert CathayCrawler._normalize_iso_date("   ") is None


def test_cathay_normalize_iso_date_keeps_unknown_text_for_visibility():
    assert CathayCrawler._normalize_iso_date("民國114/07/03") == "民國114/07/03"


def test_cathay_normalize_balance_row_covers_snapshot_date_aliases():
    # 用 __new__ 繞開 CathayCrawler.__init__ 的 CathayCreds.load() —— CI 環境沒
    # bank creds env, 直接 constructor 會 raise CredError. _normalize_balance_row
    # 是 pure method, 不需要 creds.
    crawler = CathayCrawler.__new__(CathayCrawler)
    row = {
        "snapshotDate": "2025-07-01T00:00:00",
        "twdBalance": 100,
    }
    normalized = crawler._normalize_balance_row(row)
    assert normalized["snapshotDate"] == "2025-07-01"
    assert normalized["twdBalance"] == 100

    row2 = {"date": "2025/6/30T00:00:00", "twdBalance": 50}
    normalized2 = crawler._normalize_balance_row(row2)
    assert normalized2["date"] == "2025-06-30"


def test_bank_collect_result_accepts_normalized_cathay_balance_history():
    crawler = CathayCrawler.__new__(CathayCrawler)  # 同上, 繞 creds
    raw_rows = [
        {"snapshotDate": "2025-06-30T00:00:00", "twdBalance": 50},
        {"snapshotDate": "2025-07-01T00:00:00", "twdBalance": 100},
    ]
    normalized = [crawler._normalize_balance_row(r) for r in raw_rows]

    # 若 snapshotDate 沒正規化，這裡就會 raise ValueError（regression 抓不到）。
    result = BankCollectResult(balance_history=normalized)

    assert result.balance_history[0]["snapshotDate"] == "2025-06-30"
    assert result.balance_history[1]["snapshotDate"] == "2025-07-01"


def test_bank_collect_result_rejects_raw_cathay_datetime_without_normalization():
    with pytest.raises(ValueError, match="snapshotDate"):
        BankCollectResult(balance_history=[{"snapshotDate": "2025-07-01T00:00:00"}])
