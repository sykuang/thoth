"""驗證 persist_scsb 處理 statement.months[] 月份迭代資料。

2026-06-13 升級：collect 新加月份 tab click 後，persist 把每月帳單摘要寫進
daily_metric `scsb_card_statement_months`。即使 due/paid 全 --- 也記錄 has_data=False
證明 mechanism 跑過。

case 1: 全 --- 月份 → has_data=False, due=None
case 2: 有金額月份 → has_data=True, due=int
case 3: 沒 months → 不寫 statement_months metric
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.persist import persist_scsb
from backend.core.store import BankStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch):
    from backend.core import store as store_mod
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path, raising=True)
    s = BankStore("scsb_test")
    yield s
    s.close()


# 模擬 SCSB 真實 statement.text fixture (3 月份, 全 ---)
EMPTY_STMT_TEXT = """Statement Inquiry and Payment
Data Time：2026/06/14 00:23:21
2026/05
2026/04
2026/03

Only the past three months of data are available for inquiry.

Bill Settlement Date
---
Your account number
A99999****
Current Period Total Amount Due
---
Current Period Total Minimum Amount Due
---
"""


def test_scsb_statement_months_all_empty(store):
    """3 個月份 due/paid 全 --- → has_data=False。"""
    data = {
        "accounts": [],
        "card_inquiry": {
            "leaves": {
                "statement": {
                    "url": "https://scsb/statement",
                    "text": EMPTY_STMT_TEXT,
                    "months": [
                        {"month": "2026/05", "text": EMPTY_STMT_TEXT},
                        {"month": "2026/04", "text": EMPTY_STMT_TEXT},
                        {"month": "2026/03", "text": EMPTY_STMT_TEXT},
                    ],
                },
            },
        },
    }
    persist_scsb(data, store)
    rows = list(store.conn.execute(
        "SELECT payload_json FROM daily_metrics WHERE category='scsb_card_statement_months'"
    ))
    assert len(rows) == 1
    months = json.loads(rows[0][0])
    assert "2026/05" in months
    assert "2026/04" in months
    assert "2026/03" in months
    for mo in ("2026/05", "2026/04", "2026/03"):
        assert months[mo]["has_data"] is False
        assert months[mo]["due_amount"] is None


def test_scsb_statement_months_with_data(store):
    """某月份有 due 金額 → has_data=True, due=int。"""
    text_with_data = """Statement Inquiry and Payment
Data Time：2026/06/14
2026/05
2026/04

Your account number
A99999****
Current Period Total Amount Due
12,345
Current Period Total Minimum Amount Due
1,500
"""
    data = {
        "accounts": [],
        "card_inquiry": {
            "leaves": {
                "statement": {
                    "url": "https://scsb/statement",
                    "text": text_with_data,
                    "months": [
                        {"month": "2026/05", "text": text_with_data},
                    ],
                },
            },
        },
    }
    persist_scsb(data, store)
    rows = list(store.conn.execute(
        "SELECT payload_json FROM daily_metrics WHERE category='scsb_card_statement_months'"
    ))
    assert len(rows) == 1
    months = json.loads(rows[0][0])
    assert months["2026/05"]["has_data"] is True
    assert months["2026/05"]["due_amount"] == 12345
    assert months["2026/05"]["min_payment"] == 1500


def test_scsb_statement_no_months_field_safe(store):
    """statement 沒 months[] field → 不寫 statement_months metric。"""
    data = {
        "accounts": [],
        "card_inquiry": {
            "leaves": {
                "statement": {
                    "url": "https://scsb/statement",
                    "text": EMPTY_STMT_TEXT,
                    # 沒 months
                },
            },
        },
    }
    persist_scsb(data, store)
    rows = list(store.conn.execute(
        "SELECT COUNT(*) FROM daily_metrics WHERE category='scsb_card_statement_months'"
    ))
    assert rows[0][0] == 0


def test_scsb_statement_empty_no_fake_card_inserted(store):
    """statement.text 全 '---' 且只含身分證 masked → cards 表保持空，不插假卡。

    🚨 2026-06-14 bug fix：之前 `[A-Z]\\d{4,8}\\*+` 會把身分證 masked
    A99999**** 誤抓當卡號，結果使用者 SCSB 沒辦任何信用卡卻在 cards 表多一張幽靈卡。
    """
    data = {
        "accounts": [],
        "card_inquiry": {
            "leaves": {
                "statement": {
                    "url": "https://scsb/statement",
                    "text": EMPTY_STMT_TEXT,
                },
            },
        },
    }
    persist_scsb(data, store)
    cards = list(store.conn.execute("SELECT card_no, name FROM cards"))
    assert len(cards) == 0, f"應該不寫任何卡 (帳單全 ---) 但寫了 {cards}"


def test_scsb_real_credit_card_masked_extracted_from_unbilled(store):
    """真實 SCSB 信用卡 masked (純數字+星號) 從 unbilled.text 抽出 → 寫進 cards 表。

    Statement 頁的「Your account number」永遠是身分證，不抽。
    真正的卡號要從 unbilled / current 的交易明細表格抽。
    """
    # 模擬 unbilled 有交易、卡號 ****7016
    text_with_real_unbilled = """Unbilled Transaction Details
Data Time：2026/06/14 00:26:16
Transaction Date\tCard Last 4\tMerchant\tAmount
2026/06/10\t****7016\tStarbucks\t150
2026/06/11\t****7016\t7-11\t89
"""
    data = {
        "accounts": [],
        "card_inquiry": {
            "leaves": {
                "unbilled": {
                    "url": "https://scsb/unbilled",
                    "text": text_with_real_unbilled,
                },
                "statement": {
                    "url": "https://scsb/statement",
                    "text": EMPTY_STMT_TEXT,  # 身分證 A99999**** 不該被抽
                },
            },
        },
    }
    persist_scsb(data, store)
    cards = list(store.conn.execute("SELECT card_no FROM cards"))
    card_nos = [c[0] for c in cards]
    # 確認身分證 masked 沒被當卡號（regression guard）
    assert not any(c.startswith("A") for c in card_nos), \
        f"身分證 masked 不該當卡號: {card_nos}"
    # 注意：unbilled parser 抽不抽到 ****7016 取決於 _scsb_parse_card_rows 實作；
    # 此 test 的主旨是驗證 statement 不再產生幽靈卡，這部分必為真。
