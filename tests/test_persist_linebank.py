"""Test persist_linebank with fixture payload (structured DB insertion correctness).

persist_linebank unit test — 用 fixture payload 驗結構化入庫正確。

不需要碰真銀行；payload 直接 hand-craft 自實測拿到的真實 endpoint 結構
(`payables` / `transactions` / `informations`)。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.persist import persist_linebank
from backend.core.store import BankStore


# ──────────────────────────────────────────────────────────────────────────────
# Fixture: 仿造實測 2026-06-13 linebank live login 拿到的 endpoint payload
# ──────────────────────────────────────────────────────────────────────────────

FIXTURE_DATA = {
    "_all_endpoints": ["informations", "login", "payables", "transactions"],
    "final_url": "https://accessibility.linebank.com.tw/transaction",
    "api_responses": {
        "payables": [{
            "url": "https://accessibility.linebank.com.tw/v1/account/common/payables",
            "method": "GET",
            "status": 200,
            "resp": {
                "code": "200",
                "message": "success",
                "content": {
                    "custUnitTxfrRmngLmtAmt": 50000,
                    "custDylyTxfrRmngLmtAmt": 100000,
                    "custMnlyTxfrRmngLmtAmt": 200000,
                    "dpstAcctListCnt": 1,
                    "dpstAcctList": [{
                        "arrId": "9000000117025CS0142011088",
                        "acctNbr": "900000077063",
                        "acctNick": "主帳戶",
                        "acctBal": 2806,
                        "wdrwAvblAmt": 2806,
                        "cardNbr": "900054******7048",
                        "cardStsCd": "00",
                        "pdNm": "主帳戶",
                    }],
                },
            },
        }],
        "transactions": [{
            "url": "https://accessibility.linebank.com.tw/v1/account/history/transactions",
            "method": "POST",
            "status": 200,
            "resp": {
                "code": "200",
                "message": "success",
                "content": {
                    "arrId": "9000000117025CS0142011088",
                    "acctNbr": "900000077063",
                    "acctNick": "主帳戶",
                    "acctBal": 2806,
                    "totTxCnt": 2,
                    "txCnt": 2,
                    "txLst": [
                        {
                            "txSeqNbr": 1, "txDt": "20260528", "txTm": "070641",
                            "dpstWdrwDsCd": "2",  # 出帳
                            "bizTxFuncTpNm": "貸款還款",
                            "txAmt": 53176, "afTxBal": 2806,
                            "txRmkCont": "分期信貸", "txMemoVal": None,
                        },
                        {
                            "txSeqNbr": 1, "txDt": "20260524", "txTm": "063907",
                            "dpstWdrwDsCd": "1",  # 入帳
                            "bizTxFuncTpNm": "轉帳",
                            "txAmt": 53176, "afTxBal": 55982,
                            "txRmkCont": "永豐商業銀行 ***********80607", "txMemoVal": None,
                        },
                    ],
                },
            },
        }],
        "informations": [{
            "url": "https://accessibility.linebank.com.tw/v1/customer/profile/informations",
            "method": "GET",
            "status": 200,
            "resp": {
                "code": "200",
                "message": "success",
                "content": {
                    "custNm": "測劭昀",
                    "nick": "TestUser",
                    # 故意塞敏感欄位驗證 persist 不會誤存
                    "natlId": "B123456789",
                    "brthDt": "19900101",
                    "mbleTelNbr": "0900000000",
                    "emalAddr": "leak@example.com",
                    "analIncmAmt": 3000000,
                },
            },
        }],
    },
}


@pytest.fixture
def store(tmp_path: Path, monkeypatch):
    """獨立 BankStore for test, sqlite 寫在 tmp_path。"""
    # BankStore 用 DATA_ROOT 拼路徑，patch 它
    from backend.core import store as store_mod
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path, raising=True)
    s = BankStore("linebank_test")
    yield s
    s.close()


def test_persist_linebank_writes_account(store):
    delta = persist_linebank(FIXTURE_DATA, store)

    assert delta["bank"] == "linebank"
    assert delta["scope"] == "structured"
    assert delta["balance_days"] == 1

    # accounts 表：只有主存款帳戶（LINE Bank raw 無 loan endpoint，不再合成假
    # linebank_loan_inferred row — 即使 fixture 內 txn 寫「分期信貸」也只當交易
    # 紀錄不推斷帳戶，2026-06-15 拔除）
    rows = store.conn.execute(
        "SELECT account_no, nickname, currency, type, product_type FROM accounts "
        "ORDER BY account_no"
    ).fetchall()
    assert len(rows) == 1

    # 主存款帳戶
    main = rows[0]
    assert tuple(main) == ("900000077063", "主帳戶", "TWD", "主帳戶", "deposit")

    # 假信貸 row 不該存在
    assert not [r for r in rows if r["account_no"] == "linebank_loan_inferred"]


def test_persist_linebank_writes_transactions(store):
    delta = persist_linebank(FIXTURE_DATA, store)

    assert delta["twd_txn_new"] == 2

    rows = store.conn.execute(
        "SELECT account_no, txn_datetime, account_date, description, expend, income, balance "
        "FROM twd_transactions ORDER BY txn_datetime"
    ).fetchall()
    assert len(rows) == 2

    # 第 1 筆: 5/24 入帳 (永豐轉入)
    r1 = rows[0]
    assert r1[0] == "900000077063"
    assert r1[1] == "2026-05-24T06:39:07"
    assert r1[2] == "2026-05-24"
    assert "轉帳" in r1[3] and "永豐" in r1[3]
    assert r1[4] is None         # expend
    assert r1[5] == 53176        # income
    assert r1[6] == 55982        # balance after

    # 第 2 筆: 5/28 出帳 (貸款還款)
    r2 = rows[1]
    assert r2[1] == "2026-05-28T07:06:41"
    assert r2[3] == "貸款還款: 分期信貸"
    assert r2[4] == 53176        # expend
    assert r2[5] is None         # income
    assert r2[6] == 2806


def test_persist_linebank_balance_history(store):
    persist_linebank(FIXTURE_DATA, store)

    rows = store.conn.execute(
        "SELECT twd_balance, fx_balance FROM balance_history"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 2806
    assert rows[0][1] is None


def test_persist_linebank_skips_sensitive_profile(store):
    """守鐵律: informations 只能存 nick + custNm, 絕不存身分證/電話/收入。"""
    persist_linebank(FIXTURE_DATA, store)

    # daily_metrics 應該有 linebank_profile, 但內容不含敏感欄位
    rows = store.conn.execute(
        "SELECT payload_json FROM daily_metrics WHERE category='linebank_profile'"
    ).fetchall()
    assert len(rows) == 1
    payload = rows[0][0]
    # 顯示用欄位該有
    assert "TestUser" in payload
    assert "測劭昀" in payload
    # 敏感欄位絕對不能出現
    assert "B123456789" not in payload, "身分證不該寫入 DB"
    assert "19900101" not in payload, "生日不該寫入 DB"
    assert "0900000000" not in payload, "手機不該寫入 DB"
    assert "leak@example.com" not in payload, "email 不該寫入 DB"
    assert "3000000" not in payload, "年收入不該寫入 DB"


def test_persist_linebank_empty_data(store):
    """空 data: 不該炸, 全 delta 為 0。"""
    delta = persist_linebank({}, store)
    assert delta["bank"] == "linebank"
    assert delta["twd_txn_new"] == 0
    assert delta.get("balance_days", 0) == 0


def test_persist_linebank_dedup_on_rerun(store):
    """跑兩次同樣 data: 第二次 twd_txn_new=0 (dedup 生效)。"""
    delta1 = persist_linebank(FIXTURE_DATA, store)
    assert delta1["twd_txn_new"] == 2

    delta2 = persist_linebank(FIXTURE_DATA, store)
    assert delta2["twd_txn_new"] == 0  # 全 dedup 命中

    # accounts 表仍只有 1 筆 (僅主帳戶；linebank_loan_inferred 2026-06-15 拔除,
    # 詳見 test_persist_linebank_writes_account)
    n = store.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    assert n == 1
