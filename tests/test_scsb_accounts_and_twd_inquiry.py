"""SCSB 帳戶分類與台幣明細導航回歸測試。"""
from __future__ import annotations

from backend.banks.scsb import ScsbCrawler
from backend.core.persist import persist_scsb
from backend.core.store import BankStore


def test_scsb_extract_accounts_does_not_steal_loan_header_for_first_deposit():
    """總覽卡片上方有「我的貸款總餘額」，不能讓第一個活儲帳戶吃到貸款 header。"""
    text = """
我的帳戶總額
我的存款總額
NT$73,549
我的貸款總餘額
NT$20,589,800
我的帳戶摘要
所有帳戶查詢
看總覽
活儲存款
中壢分行
90000000167058
NT$73,500
交易明細
轉帳
轉定存
活儲存款
世貿分行
90000000207039
NT$0
交易明細
轉帳
轉定存
活期存款
中壢分行
90000000237023
USD1.55
交易明細
賣外幣
轉定存
貸款
西湖分行
90000000247044 到期日 140/09/24
NT$20,589,800
明細
基本資料
償還本金
"""
    accounts = ScsbCrawler._extract_accounts(text)

    by_acct = {a["account_no"]: a for a in accounts}
    assert by_acct["90000000167058"]["type_header"] == "活儲存款"
    assert by_acct["90000000207039"]["type_header"] == "活儲存款"
    assert by_acct["90000000237023"]["type_header"] == "活期存款"
    assert by_acct["90000000247044"]["type_header"] == "貸款"


def test_persist_scsb_first_deposit_remains_asset_not_loan(tmp_path, monkeypatch):
    """2620...8541 必須入成 deposit，否則 portfolio 會把活儲當負債。"""
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("scsb")
    try:
        persist_scsb(
            {
                "accounts": [
                    {"account_no": "90000000167058", "currency": "TWD", "balance": "73500", "type_header": "活儲存款"},
                    {"account_no": "90000000247044", "currency": "TWD", "balance": "20589800", "type_header": "貸款"},
                ],
            },
            store,
        )
        rows = store.conn.execute(
            "SELECT account_no, type, product_type, raw_balance FROM accounts ORDER BY account_no",
        ).fetchall()
    finally:
        store.close()

    got = {r["account_no"]: dict(r) for r in rows}
    assert got["90000000167058"]["type"] == "活儲存款"
    assert got["90000000167058"]["product_type"] == "deposit"
    assert got["90000000167058"]["raw_balance"] == 73500
    assert got["90000000247044"]["product_type"] == "loan"
    assert got["90000000247044"]["raw_balance"] == -20589800


def test_scsb_twd_inquiry_accepts_chinese_menu_labels():
    """SCSB 目前是中文選單；導航關鍵字不能只找 TWD Deposit 英文。"""
    nav = ScsbCrawler._twd_inquiry_nav_script()
    assert "臺幣存匯" in nav
    assert "台幣存匯" in nav
    assert "TWD Deposit" in nav  # fallback only
    assert "交易明細" in nav
