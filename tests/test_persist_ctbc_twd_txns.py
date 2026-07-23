"""驗證 persist_ctbc 寫 twd_transactions 的 mechanism (2026-06-20 補上 known TODO).

CTBC collect() 新增的 twd_history 結構（從 probe 反推）:
  [{account_no, months: {m0: [detail,...], m1:[...], ...}, errors: {...}}]

每筆 detail (CTBC raw 欄位):
  actDtTm        '2026-06-02-14.53.14.296159'   → datetime '2026-06-02 14:53:14'
  trnDtRaw       '20260602'                     → account_date '2026-06-02'
  memo1          '跨行轉'        → desc 前段
  memo2          '永豐銀'        → desc 後段
  dbAmt          0 / 15943      → expend (int)
  crAmt          15943 / 0      → income (int)
  balanceAmt     '15,950'       → balance (拔 comma → int)
  bankId         '' / '004'     → counterparty_bank
  trfAcct        '001680180**80607' → counterparty_acct
  memoCode       'ZD' / 'GK'    → memo

case 1: 基本 parse + upsert
case 2: 跨月 overlap dedup (m0 和 m1 都帶相同 row → store dedup_key 去重)
case 3: 空 detail / 缺帳號 / 缺 actDtTm gracefully handle
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.persist import persist_ctbc
from backend.core.persist.ctbc import (
    _ctbc_yyyymmdd_to_iso,
    _normalize_ctbc_datetime,
    _parse_ctbc_twd_history,
)
from backend.core.store import BankStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    s = BankStore("ctbc_twd_test")
    yield s
    s.close()


# ----------------------- helpers (unit-level) -----------------------

def test_normalize_ctbc_datetime_normal():
    assert _normalize_ctbc_datetime("2026-06-02-14.53.14.296159") == "2026-06-02 14:53:14"


def test_normalize_ctbc_datetime_missing_micro():
    """有些行可能沒 microseconds, 仍要能 parse 'HH.MM.SS'."""
    assert _normalize_ctbc_datetime("2026-06-02-14.53.14") == "2026-06-02 14:53:14"


@pytest.mark.parametrize("bad", [
    "",
    None,
    "not-a-datetime",
    "2026-06-02",  # 缺時間
    "2026-06-02-14",  # 時間段只 1 個
    "2026-06-02-14.53",  # 時間段只 2 個
    "26-06-02-14.53.14",  # 年份 2 碼
])
def test_normalize_ctbc_datetime_bad_input_returns_none(bad):
    assert _normalize_ctbc_datetime(bad) is None


def test_ctbc_yyyymmdd_to_iso_normal():
    assert _ctbc_yyyymmdd_to_iso("20260602") == "2026-06-02"


@pytest.mark.parametrize("bad", ["", None, "2026-06-02", "20260", "SyntheticTestPassword01!"])
def test_ctbc_yyyymmdd_to_iso_bad_input_returns_none(bad):
    assert _ctbc_yyyymmdd_to_iso(bad) is None


# ----------------------- _parse_ctbc_twd_history -----------------------

def test_parse_basic_one_month_two_rows():
    history = [{
        "account_no": "0000900000317011",
        "months": {
            "m0": [
                {
                    # 跨行轉入 15943
                    "actDtTm": "2026-06-02-14.53.14.296159",
                    "trnDtRaw": "20260602",
                    "memo1": "跨行轉", "memo2": "永豐銀",
                    "dbAmt": 0, "crAmt": 15943, "balanceAmt": "15,950",
                    "bankId": "807", "trfAcct": "001680180**80607",
                    "memoCode": "ZD",
                },
                {
                    # 中信卡扣繳 15943
                    "actDtTm": "2026-06-08-00.45.27.256896",
                    "trnDtRaw": "20260608",
                    "memo1": "中信卡", "memo2": "",
                    "dbAmt": 15943, "crAmt": 0, "balanceAmt": "7",
                    "bankId": "", "trfAcct": "",
                    "memoCode": "GK",
                },
            ],
        },
        "errors": {},
    }]
    rows = _parse_ctbc_twd_history(history)
    assert len(rows) == 2

    r0 = rows[0]
    assert r0["account_no"] == "0000900000317011"
    assert r0["datetime"] == "2026-06-02 14:53:14"
    assert r0["account_date"] == "2026-06-02"
    assert r0["desc"] == "跨行轉 永豐銀"
    assert r0["expend"] == 0
    assert r0["income"] == 15943
    assert r0["balance"] == 15950  # 拔 comma
    assert r0["counterparty_bank"] == "807"
    assert r0["counterparty_acct"] == "001680180**80607"
    assert r0["memo"] == "ZD"

    r1 = rows[1]
    assert r1["datetime"] == "2026-06-08 00:45:27"
    assert r1["desc"] == "中信卡"  # memo2 空, 只剩 memo1
    assert r1["expend"] == 15943
    assert r1["income"] == 0
    assert r1["counterparty_bank"] is None  # 空字串 → None
    assert r1["counterparty_acct"] is None


def test_parse_empty_history_returns_empty():
    assert _parse_ctbc_twd_history([]) == []
    assert _parse_ctbc_twd_history(None) == []  # type: ignore[arg-type]


def test_parse_missing_account_skipped():
    history = [{
        "account_no": None,
        "months": {"m0": [{"actDtTm": "2026-06-02-14.53.14"}]},
    }]
    assert _parse_ctbc_twd_history(history) == []


def test_parse_missing_actDtTm_yields_none_datetime():
    """Persist 層假設 collector 已濾過缺 actDtTm 的 row (見 banks/ctbc.py
    `_collect_twd_deposit_history`). 但若某天 collector 邏輯被改壞繞過,
    persist 不 raise — datetime 留 None, PG NOT NULL 會炸出來讓 caller 警覺.

    這個 test 鎖死 persist 層「raw 假設完整」的 contract: 不在這層 sweep
    structural anomaly, 只保證 happy-path 該過. Collector 層的 skip 邏輯
    test 在 banks/test_ctbc_collector_validate.py.
    """
    history = [{
        "account_no": "X",
        "months": {"m0": [
            {"trnDtRaw": "20260602", "memo1": "weird", "dbAmt": 1, "balanceAmt": "0"},
        ]},
    }]
    rows = _parse_ctbc_twd_history(history)
    assert len(rows) == 1
    assert rows[0]["datetime"] is None
    assert rows[0]["account_date"] == "2026-06-02"
    assert rows[0]["desc"] == "weird"


def test_parse_cross_month_collects_all_months():
    """m0/m1/m2 都有 rows, parse 後保留全部.

    （store.upsert_twd_txns 用 dedup_key 去重月窗 overlap，這層只負責拍平。）
    """
    history = [{
        "account_no": "A",
        "months": {
            "m0": [{"actDtTm": "2026-06-01-10.00.00", "trnDtRaw": "20260601",
                    "dbAmt": 1, "crAmt": 0, "balanceAmt": "100", "memo1": "june"}],
            "m1": [{"actDtTm": "2026-05-15-10.00.00", "trnDtRaw": "20260515",
                    "dbAmt": 2, "crAmt": 0, "balanceAmt": "101", "memo1": "may"}],
            "m2": [{"actDtTm": "2026-04-15-10.00.00", "trnDtRaw": "20260415",
                    "dbAmt": 3, "crAmt": 0, "balanceAmt": "103", "memo1": "april"}],
        },
    }]
    rows = _parse_ctbc_twd_history(history)
    assert len(rows) == 3
    assert {r["desc"] for r in rows} == {"june", "may", "april"}


# ----------------------- persist_ctbc integration -----------------------

def test_persist_writes_twd_transactions(store: BankStore):
    """End-to-end: collect data 進 persist_ctbc → twd_transactions table 有 row."""
    data = {
        "summary": {},
        "twd_deposit": {
            "demDepBalSummaryResponse": {
                "infoList": [
                    {"accountId": "0000900000317011", "balance": "27",
                     "accountNickName": "活儲"},
                ],
            },
        },
        "twd_history": [{
            "account_no": "0000900000317011",
            "months": {
                "m0": [
                    {"actDtTm": "2026-06-02-14.53.14.296159", "trnDtRaw": "20260602",
                     "memo1": "跨行轉", "memo2": "永豐銀",
                     "dbAmt": 0, "crAmt": 15943, "balanceAmt": "15,950",
                     "bankId": "807", "trfAcct": "001680180**80607", "memoCode": "ZD"},
                    {"actDtTm": "2026-06-08-00.45.27.256896", "trnDtRaw": "20260608",
                     "memo1": "中信卡", "memo2": "",
                     "dbAmt": 15943, "crAmt": 0, "balanceAmt": "7",
                     "bankId": "", "trfAcct": "", "memoCode": "GK"},
                ],
            },
            "errors": {},
        }],
        "card_api_dump": {},
    }
    delta = persist_ctbc(data, store)
    assert delta["twd_txn_new"] == 2

    cur = store.conn.execute(
        "SELECT account_no, txn_datetime, description, expend, income, balance "
        "FROM twd_transactions ORDER BY txn_datetime"
    )
    rows = cur.fetchall()
    assert len(rows) == 2
    # 第 1 筆 跨行轉入
    assert rows[0][0] == "0000900000317011"
    assert rows[0][1] == "2026-06-02 14:53:14"
    assert rows[0][2] == "跨行轉 永豐銀"
    assert rows[0][3] == 0
    assert rows[0][4] == 15943
    assert rows[0][5] == 15950
    # 第 2 筆 中信卡扣繳
    assert rows[1][3] == 15943
    assert rows[1][4] == 0


def test_persist_dedup_idempotent_on_second_run(store: BankStore):
    """Re-run 同 data → dedup_key 命中 → 0 new rows."""
    data = {
        "twd_history": [{
            "account_no": "A",
            "months": {"m0": [
                {"actDtTm": "2026-06-01-10.00.00", "trnDtRaw": "20260601",
                 "memo1": "x", "dbAmt": 1, "crAmt": 0, "balanceAmt": "100"},
            ]},
        }],
    }
    d1 = persist_ctbc(data, store)
    assert d1["twd_txn_new"] == 1
    d2 = persist_ctbc(data, store)
    assert d2["twd_txn_new"] == 0


def test_persist_no_twd_history_returns_zero(store: BankStore):
    """data 沒 twd_history → twd_txn_new=0, 不 raise."""
    delta = persist_ctbc({"twd_history": []}, store)
    assert delta["twd_txn_new"] == 0
