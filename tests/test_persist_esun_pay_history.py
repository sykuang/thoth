"""Esun _parse_card_pay_history + persist 整合 test (2026-06-23 v3).

使用者 local crawl 證實「信用卡繳款明細查詢」(FCM01005 widget) 真實 raw shape:
  繳款日期\\n繳款方式\\n繳款行庫\\n幣別\\n應繳款金額\\n繳款金額\\n
  2026/03/30\\n玉山自動扣繳　\\n玉山自動轉帳\\n臺幣 TWD\\n65,714\\n65,714
  2026/03/06\\n玉山自動扣繳　\\n玉山自動轉帳\\n臺幣 TWD\\n12,792\\n12,792
records 排序新→舊, [0] 是最新.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.banks.esun import EsunCrawler
from backend.core.persist import persist_esun
from backend.core.store import BankStore


SAMPLE_TEXT = (
    "繳款日期\n繳款方式\n繳款行庫\n幣別\n應繳款金額\n繳款金額\n"
    "\n"
    "2026/03/30\n玉山自動扣繳　\n玉山自動轉帳\n臺幣 TWD\n65,714\n65,714\n"
    "\n"
    "2026/03/06\n玉山自動扣繳　\n玉山自動轉帳\n臺幣 TWD\n12,792\n12,792\n"
)


def test_parse_card_pay_history_real_shape():
    """真實玉山 FCM01005 表格 → 2 筆 records."""
    result = EsunCrawler._parse_card_pay_history(SAMPLE_TEXT)
    records = result["records"]
    assert len(records) == 2
    # records[0] = 最新
    assert records[0]["post_date"] == "2026-03-30"
    assert records[0]["method"] == "玉山自動扣繳"
    assert records[0]["bank"] == "玉山自動轉帳"
    assert records[0]["currency"] == "TWD"
    assert records[0]["due_amount"] == 65714
    assert records[0]["paid_amount"] == 65714
    # records[1]
    assert records[1]["post_date"] == "2026-03-06"
    assert records[1]["paid_amount"] == 12792


def test_parse_card_pay_history_empty_text():
    """空 text → records=[],不 raise."""
    assert EsunCrawler._parse_card_pay_history("") == {"records": []}


def test_parse_card_pay_history_no_records_in_text():
    """有 header 但沒 records (玉山「您沒有任何繳款紀錄喔」) → records=[]."""
    text = "繳款日期\n繳款方式\n繳款行庫\n幣別\n應繳款金額\n繳款金額\n您沒有任何繳款紀錄喔！"
    result = EsunCrawler._parse_card_pay_history(text)
    assert result == {"records": []}


def test_persist_esun_writes_last_payment_from_pay_history(tmp_path, monkeypatch):
    """persist_esun 從 card_pay_history.records[0] 寫 last_payment_*."""
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("esun", user_id=1)
    try:
        data = {
            "card_summary": {"payment_due_date_roc": "115/06/29",
                             "credit_limit_twd": "100000"},
            "card_bills": [],  # 沒帳單 fallback
            "card_transactions": [
                {"card_no": "9064-XXXX-XXXX-7032", "card_last4": "7032",
                 "status": "已入帳", "billed_amount": "500"},
            ],
            "card_pay_history": {
                "records": [
                    # records[0] = 最新, 該被寫進 cards
                    {"post_date": "2026-03-30", "method": "玉山自動扣繳",
                     "bank": "玉山自動轉帳", "currency": "TWD",
                     "due_amount": 65714, "paid_amount": 65714},
                    {"post_date": "2026-03-06", "method": "玉山自動扣繳",
                     "bank": "玉山自動轉帳", "currency": "TWD",
                     "due_amount": 12792, "paid_amount": 12792},
                ],
            },
        }
        persist_esun(data, store, rules=None)
        row = store.conn.execute(
            "SELECT card_no, bill_due_amount, last_payment_amount, last_payment_date "
            "FROM cards WHERE user_id=1 AND card_no='****7032'",
        ).fetchone()
        assert row is not None
        assert row[2] == pytest.approx(65714.0)
        assert row[3] == "2026-03-30"
    finally:
        store.close()


def test_persist_esun_falls_back_to_card_bills_when_no_pay_history(tmp_path, monkeypatch):
    """card_pay_history 缺 → fallback 用 card_bills[0] (既有 0.3.26 行為不變)."""
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("esun", user_id=1)
    try:
        data = {
            "card_summary": {"payment_due_date_roc": "115/06/29",
                             "credit_limit_twd": "100000"},
            "card_bills": [{"bill_month": "2026-05", "currency": "TWD",
                           "due_amount": 500, "paid_amount": 1000}],
            "card_transactions": [
                {"card_no": "9064-XXXX-XXXX-7032", "card_last4": "7032",
                 "status": "已入帳", "billed_amount": "500"},
            ],
            # 無 card_pay_history
        }
        persist_esun(data, store, rules=None)
        row = store.conn.execute(
            "SELECT card_no, bill_due_amount, last_payment_amount, last_payment_date "
            "FROM cards WHERE user_id=1 AND card_no='****7032'",
        ).fetchone()
        assert row is not None
        assert row[1] == pytest.approx(500.0), "bill_due 從 card_bills[0]"
        assert row[2] == pytest.approx(1000.0), "last_pay_amt 從 card_bills[0].paid_amount"
        assert row[3] is None, "無 pay_history → date None"
    finally:
        store.close()
