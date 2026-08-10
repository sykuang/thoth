"""E.SUN deposit transaction detail persistence.

玉山 FAO01002「存款交易明細查詢」有兩種重要 raw shape：
1. 查得到資料時，result grid/table 會出現在 snapshot.gridText / tables 裡。
2. 使用者目前臺幣帳戶可能查得到表單與查詢時間，但期間內沒有任何 row；這時不能
   製造假交易，delta 要誠實 twd_txn_new=0。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.persist.esun import _parse_esun_twd_txn_results, persist_esun
from backend.core.store import BankStore


@pytest.fixture
def store_esun_twd(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("esun_twd_test")
    try:
        yield store
    finally:
        store.close()


def test_parse_esun_twd_snapshot_table_rows() -> None:
    """FAO01002 result table rows should become twd_transactions payload rows."""
    rows = _parse_esun_twd_txn_results([
        {
            "account_no": "0900000087022",
            "selected_text": "0900000087022 臺幣綜存",
            "snapshot": {
                "gridText": (
                    "交易日 帳務日 摘要 支出 存入 餘額 備註\n"
                    "2026/06/29 2026/06/30 跨行轉帳 80  4 台新卡費\n"
                    "2026/06/01 2026/06/01 利息  2 84 活存利息\n"
                ),
            },
        }
    ])

    assert rows == [
        {
            "account_no": "0900000087022",
            "datetime": "2026-06-29",
            "account_date": "2026-06-30",
            "desc": "跨行轉帳",
            "expend": 80.0,
            "income": None,
            "balance": 4.0,
            "counterparty_bank": None,
            "counterparty_acct": None,
            "memo": "台新卡費",
        },
        {
            "account_no": "0900000087022",
            "datetime": "2026-06-01",
            "account_date": "2026-06-01",
            "desc": "利息",
            "expend": None,
            "income": 2.0,
            "balance": 84.0,
            "counterparty_bank": None,
            "counterparty_acct": None,
            "memo": "活存利息",
        },
    ]


def test_parse_esun_twd_user_copied_multiline_rows() -> None:
    """Parser must handle the real FAO01002 copied-result shape 使用者 pasted."""
    raw = """
查詢時間：2026/07/01 00:46:28
交易日期
時間    摘要    提    存    帳戶餘額    存摺備註
對方銀行代碼/帳號    轉帳留言
*2026/06/21
00:43:43    利息        3    4    T900***001
2026/03/30
06:12:14    玉山卡款扣繳    65,714        1    測＊試
*2026/03/28
03:38:04    ＡＴＭ跨行轉        65,714    65,715    永豐銀行
807/0090000000197014
2026/03/06
06:09:03    玉山卡款扣繳    12,792        1    測＊試
2026/03/03
12:39:55    ＡＴＭ跨行轉        12,792    12,793    永豐銀行
807/0090000000197014
"""

    rows = _parse_esun_twd_txn_results([
        {"account_no": "0900000087022", "text": raw, "snapshot": {"gridText": raw}}
    ])

    assert len(rows) == 5
    assert rows[0] == {
        "account_no": "0900000087022",
        "datetime": "2026-06-21 00:43:43",
        "account_date": "2026-06-21",
        "desc": "利息",
        "expend": None,
        "income": 3.0,
        "balance": 4.0,
        "counterparty_bank": None,
        "counterparty_acct": None,
        "memo": "T900***001",
    }
    assert rows[1]["desc"] == "玉山卡款扣繳"
    assert rows[1]["expend"] == 65714.0
    assert rows[1]["income"] is None
    assert rows[1]["balance"] == 1.0
    assert rows[2]["desc"] == "ＡＴＭ跨行轉"
    assert rows[2]["expend"] is None
    assert rows[2]["income"] == 65714.0
    assert rows[2]["balance"] == 65715.0
    assert rows[2]["counterparty_bank"] == "永豐銀行"
    assert rows[2]["counterparty_acct"] == "807/0090000000197014"


def test_persist_esun_writes_twd_transaction_detail_rows(store_esun_twd) -> None:
    """persist_esun must upsert parsed FAO01002 TWD transactions."""
    data = {
        "accounts": [{"account_no": "0900000087022", "category": "臺幣綜存", "currency": "TWD", "balance": 4}],
        "twd_txn_results": [
            {
                "account_no": "0900000087022",
                "selected_text": "0900000087022 臺幣綜存",
                "snapshot": {
                    "gridText": "交易日 帳務日 摘要 支出 存入 餘額 備註\n2026/06/29 2026/06/30 跨行轉帳 80  4 台新卡費\n",
                },
            }
        ],
    }

    delta = persist_esun(data, store_esun_twd)

    assert delta["twd_txn_new"] == 1
    rows = [tuple(row) for row in store_esun_twd.conn.execute(
        "SELECT account_no, txn_datetime, account_date, description, raw_description, "
        "expend, income, balance, memo "
        "FROM twd_transactions"
    )]
    assert rows == [(
        "0900000087022", "2026-06-29", "2026-06-30",
        "跨行轉帳 - 台新卡費", "跨行轉帳", 80.0, None, 4.0, "台新卡費",
    )]


def test_persist_esun_empty_twd_query_does_not_create_rows(store_esun_twd) -> None:
    """Form/result shell without grid rows should stay empty, not hallucinate txns."""
    data = {
        "accounts": [{"account_no": "0900000087022", "category": "臺幣綜存", "currency": "TWD", "balance": 4}],
        "twd_txn_results": [
            {
                "account_no": "0900000087022",
                "selected_text": "0900000087022 臺幣綜存",
                "clicked_period": {"via": "custom-one-year", "start": "2025/07/02", "end": "2026/07/01"},
                "text": "存款交易明細查詢\n2025/07/022026/07/01TWD\n查詢時間：2026/07/01 00:39:53\n提醒您：本查詢保留近一年的交易明細。",
                "snapshot": {"hasGrid": False, "gridText": "", "qryResult": [{"visible": False, "text": "查詢時間：2026/07/01 00:39:53"}]},
            }
        ],
    }

    delta = persist_esun(data, store_esun_twd)

    assert delta["twd_txn_new"] == 0
    count = store_esun_twd.conn.execute("SELECT COUNT(*) FROM twd_transactions").fetchone()[0]
    assert count == 0
