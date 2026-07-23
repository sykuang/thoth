"""驗證 persist_esun 處理 card_transactions 雙幣值規範化邏輯。

2026-06-13 修正：純台幣 transactions (consume_currency='TWD' 同 billed_currency)
不應寫進 consume_currency / consume_amount（對齊 cathay norm 規則，避免 DB 髒資料）。

case 1: 純台幣 → consume_currency=None, consume_amount=None
case 2: 外幣 USD → consume_currency='USD', consume_amount 保留原值
case 3: 'TWD' 字串但 billed 也是 'TWD' → 視為純台幣
case 4: status='未入帳' 走 pending, '已入帳' 走 billed
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.persist import persist_esun
from backend.core.store import BankStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch):
    from backend.core import store as store_mod
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path, raising=True)
    s = BankStore("esun_test")
    yield s
    s.close()


def test_esun_pure_twd_no_consume_currency(store):
    """case 1+3: 純台幣 transactions → consume_currency=None。"""
    data = {
        "accounts": [],
        "card_transactions": [
            {
                "consume_date": "2026/06/08",
                "merchant": "優步－大埔鐵板燒",
                "consume_currency": "TWD",   # 純台幣
                "consume_amount": 358.0,
                "billed_currency": "TWD",
                "billed_amount": 358.0,
                "card_no": "9064-XXXX-XXXX-7032",
                "card_last4": "7032",
                "status": "已入帳",
            },
        ],
    }
    delta = persist_esun(data, store)
    assert delta["card_billed_new"] == 1
    rows = list(store.conn.execute(
        "SELECT description, amount, currency, consume_currency, consume_amount FROM card_billed_txns"
    ))
    assert len(rows) == 1
    desc, amt, cur, cc, ca = rows[0]
    assert desc == "優步－大埔鐵板燒"
    assert amt == 358
    assert cur == "TWD"
    assert cc is None  # 純台幣不寫 consume_currency
    assert ca is None


def test_esun_foreign_currency_preserved(store):
    """case 2: 外幣 USD → consume_currency + consume_amount 保留原值。"""
    data = {
        "accounts": [],
        "card_transactions": [
            {
                "consume_date": "2026/05/15",
                "merchant": "AMAZON.COM",
                "consume_currency": "USD",
                "consume_amount": 59.99,
                "billed_currency": "TWD",
                "billed_amount": 1850,
                "card_no": "9064-XXXX-XXXX-7032",
                "card_last4": "7032",
                "status": "已入帳",
            },
        ],
    }
    delta = persist_esun(data, store)
    assert delta["card_billed_new"] == 1
    rows = list(store.conn.execute(
        "SELECT description, amount, currency, consume_currency, consume_amount FROM card_billed_txns"
    ))
    desc, amt, cur, cc, ca = rows[0]
    assert desc == "AMAZON.COM"
    assert amt == 1850
    assert cur == "TWD"
    assert cc == "USD"
    assert abs(ca - 59.99) < 0.01


def test_esun_unbilled_goes_to_pending_not_billed(store):
    """case 4: status='未入帳' → 走 pending 不寫 billed。"""
    data = {
        "accounts": [],
        "card_transactions": [
            {
                "consume_date": "2026/06/10",
                "merchant": "全聯福利中心",
                "consume_currency": "TWD",
                "consume_amount": 500,
                "billed_currency": "TWD",
                "billed_amount": 500,
                "card_no": "9064-XXXX-XXXX-7032",
                "card_last4": "7032",
                "status": "未入帳",  # ← 走 pending
            },
        ],
    }
    delta = persist_esun(data, store)
    assert delta.get("card_billed_new", 0) == 0
    assert delta["card_unbilled"] == 1
    billed = list(store.conn.execute("SELECT COUNT(*) FROM card_billed_txns"))
    pending = list(store.conn.execute("SELECT COUNT(*) FROM card_pending_txns"))
    assert billed[0][0] == 0
    assert pending[0][0] == 1


def test_esun_billed_status_goes_to_billed(store):
    """status='已入帳' → 走 billed 不寫 pending。"""
    data = {
        "accounts": [],
        "card_transactions": [
            {
                "consume_date": "2026/06/08",
                "merchant": "中油加油站",
                "consume_currency": "TWD",
                "consume_amount": 1727,
                "billed_currency": "TWD",
                "billed_amount": 1727,
                "card_no": "9064-XXXX-XXXX-7032",
                "card_last4": "7032",
                "status": "已入帳",
            },
        ],
    }
    delta = persist_esun(data, store)
    assert delta["card_billed_new"] == 1
    assert delta.get("card_unbilled", 0) == 0


def test_esun_card_summary_and_bills_in_daily_metric(store):
    """card_summary + card_bills 應寫 daily_metric (即使 due=0 也要保留證據)。"""
    data = {
        "accounts": [],
        "card_summary": {
            "credit_limit_twd": 400000,
            "epoint": 1183,
            "payment_due_date_roc": "115/06/29",
        },
        "card_bills": [
            {"bill_month": "2026-05", "currency": "TWD", "due_amount": 0, "paid_amount": 0},
            {"bill_month": "2026-04", "currency": "TWD", "due_amount": 0, "paid_amount": 0},
        ],
    }
    delta = persist_esun(data, store)
    assert delta["card_bills"] == 2
    import json
    summary_row = list(store.conn.execute(
        "SELECT payload_json FROM daily_metrics WHERE category='esun_card_summary'"
    ))
    assert len(summary_row) == 1
    s = json.loads(summary_row[0][0])
    assert s["credit_limit_twd"] == 400000

    bills_row = list(store.conn.execute(
        "SELECT payload_json FROM daily_metrics WHERE category='esun_card_bills'"
    ))
    assert len(bills_row) == 1
    b = json.loads(bills_row[0][0])
    assert b["count"] == 2


def test_esun_card_billed_card_no_normalized_to_last4_format(store):
    """2026-06-20 regression: 帳戶 tab 玉山卡顯示「使用額度 0」root cause.

    cards.card_no='****7032' (esun_seen_cards line 140) 跟
    card_billed_txns.card_no='9064-XXXX-XXXX-7032' (舊 raw 寫入) 不對齊,
    `_bill_summary_for_cards` SQL `WHERE card_no = ?` join 失敗 → bill_due_amount=0.
    修法: persist_esun 寫 card_billed_txns 時把 card_no 統一 normalize 成
    f'****{card_last4}', 跟 cards 同格式 (跟 HSBC/CTBC/Taishin baseline 一致).
    """
    data = {
        "accounts": [],
        "card_transactions": [
            {
                "consume_date": "2026/06/08",
                "merchant": "優步－大埔鐵板燒",
                "billed_currency": "TWD",
                "billed_amount": 358.0,
                "card_no": "9064-XXXX-XXXX-7032",  # raw masked full
                "card_last4": "7032",
                "status": "已入帳",
            },
            {
                "consume_date": "2026/06/09",
                "merchant": "另一筆消費",
                "billed_currency": "TWD",
                "billed_amount": 1000.0,
                "card_no": "9063-XXXX-XXXX-7016",
                "card_last4": "7016",
                "status": "未入帳",
            },
        ],
    }
    persist_esun(data, store)

    # 已入帳 row → card_billed_txns.card_no='****7032'
    billed_rows = list(store.conn.execute(
        "SELECT card_no FROM card_billed_txns"
    ))
    assert len(billed_rows) == 1
    assert billed_rows[0][0] == "****7032"  # 不是 '9064-XXXX-XXXX-7032'

    # 未入帳 row → card_pending_txns.card_no='****7016'
    pending_rows = list(store.conn.execute(
        "SELECT card_no FROM card_pending_txns"
    ))
    assert len(pending_rows) == 1
    assert pending_rows[0][0] == "****7016"

    # cards table 同樣 normalize (確認新舊兩路徑一致)
    cards_rows = list(store.conn.execute("SELECT card_no FROM cards"))
    card_nos = {r[0] for r in cards_rows}
    assert "****7032" in card_nos
    assert "****7016" in card_nos


def test_esun_card_billed_fallback_to_raw_when_last4_missing(store):
    """sanity: 萬一 raw data 沒 card_last4 (極端 edge case),
    persist 應退而求其次用 raw card_no, 不能 KeyError 或寫 '****'.
    """
    data = {
        "accounts": [],
        "card_transactions": [
            {
                "consume_date": "2026/06/08",
                "merchant": "no last4 row",
                "billed_currency": "TWD",
                "billed_amount": 100.0,
                "card_no": "RAW-CARD-NO",
                # 無 card_last4
                "status": "已入帳",
            },
        ],
    }
    persist_esun(data, store)
    billed_rows = list(store.conn.execute(
        "SELECT card_no FROM card_billed_txns"
    ))
    # 無 last4 → fallback 寫 raw, 至少不會炸 / 不會寫 '****'
    assert len(billed_rows) == 1
    assert billed_rows[0][0] == "RAW-CARD-NO"
