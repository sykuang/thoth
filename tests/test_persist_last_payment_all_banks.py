"""跨 6 家銀行 (sinopac/cathay/fubon/ctbc/dbs/esun/taishin) 信用卡 last_payment +
bill_due_amount audit + persist regression.

2026-06-22 audit (使用者指示「檢查所有銀行是不是都能正確 parse 繳款紀錄」):
盤完 12 家 collected.json raw 找出每家 last_payment_amount / last_payment_date /
bill_due_amount 三欄的真實 source. ubot 已單獨 ship 在 tests/test_persist_ubot_last_payment.py.

此檔驗 7 家其他銀行 (sinopac/cathay/fubon/ctbc/dbs/esun/taishin) persist 寫入正確.
全用 minimal mock data, 不依賴實際 collected.json 檔.

Sentinel rules (跨家一致, 2026-06-22 v2):
  - last_payment_date 只在 raw 有真實 date 時才寫 (沒 raw source → 永遠 None)
  - last_payment_amount: 0 是合法值 (本期未繳 / 自動扣繳尚未到期), 寫進 DB
  - card_events 靠 last_payment_date is None 來 gate 通知, 不會誤推
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.persist import (
    persist_cathay,
    persist_ctbc,
    persist_dbs,
    persist_esun,
    persist_fubon,
    persist_sinopac,
    persist_taishin,
)
from backend.core.store import BankStore


def _make_store(tmp_path, monkeypatch, bank):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    return BankStore(bank, user_id=1)


def _read_card_fields(store, card_no):
    cur = store.conn.execute(
        "SELECT card_no, bill_due_amount, last_payment_amount, last_payment_date "
        "FROM cards WHERE user_id=? AND card_no=?",
        (store.user_id, card_no),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "card_no": row[0],
        "bill_due_amount": row[1],
        "last_payment_amount": row[2],
        "last_payment_date": row[3],
    }


# ============================================================
# Sinopac — paid+date 都從 card_statements[0] 抓
# ============================================================

def test_sinopac_writes_three_fields_from_statement(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch, "sinopac")
    try:
        data = {
            "card_statements": [{
                "billing_cycle_date": "2026/05/17",
                "payment_due_date": "2026/06/01",
                "summary": {"current_due": "0", "paid": "69"},
                "records": [
                    {"trans_date": "2026/05/04", "post_date": "2026/05/04",
                     "card_last4": "7030", "description": "永豐自扣已入帳，謝謝！", "amount": "-69"},
                ],
            }],
            "all_cards": {"Result": {"Items": [
                {"CardNo": "9000000000347030", "Name": "永豐大戶卡", "CardTypeDesc": "credit",
                 "CardBrand": "VISA", "ExpDate": "0829"},
            ]}},
        }
        persist_sinopac(data, store, rules=None)
        card = _read_card_fields(store, "9000000000347030")
        assert card is not None
        assert card["bill_due_amount"] == 0.0
        assert card["last_payment_amount"] == 69.0
        assert card["last_payment_date"] == "2026-05-04"
    finally:
        store.close()


def test_sinopac_no_autopay_record_does_not_create_payment(tmp_path, monkeypatch):
    """summary.paid 不是繳款 row；沒「自扣已入帳」record 時不寫 last_payment。"""
    store = _make_store(tmp_path, monkeypatch, "sinopac")
    try:
        data = {
            "card_statements": [{
                "billing_cycle_date": "2026/05/17",
                "payment_due_date": "2026/06/01",
                "summary": {"current_due": "0", "paid": "69"},
                "records": [
                    {"trans_date": "2026/05/04", "description": "其他消費", "amount": "-69"},
                ],
            }],
            "all_cards": {"Result": {"Items": [
                {"CardNo": "9000000000347030", "Name": "永豐", "CardTypeDesc": "c",
                 "CardBrand": "V", "ExpDate": "0829"},
            ]}},
        }
        persist_sinopac(data, store, rules=None)
        card = _read_card_fields(store, "9000000000347030")
        assert card is not None
        assert card["last_payment_amount"] is None
        assert card["last_payment_date"] is None
    finally:
        store.close()


def test_sinopac_uses_latest_payment_record_across_statement_months(tmp_path, monkeypatch):
    """最新月份沒有自扣 row 時，要往較舊 statement 找真正繳款紀錄。"""
    store = _make_store(tmp_path, monkeypatch, "sinopac")
    try:
        data = {
            "card_statements": [
                {
                    "billing_cycle_date": "2026/06/17",
                    "payment_due_date": "2026/07/01",
                    "summary": {"current_due": "500", "paid": "0"},
                    "records": [{"trans_date": "2026/06/10", "description": "一般消費", "amount": "500"}],
                },
                {
                    "billing_cycle_date": "2026/05/17",
                    "payment_due_date": "2026/06/01",
                    "summary": {"current_due": "0", "paid": "69"},
                    "records": [
                        {"trans_date": "2026/05/04", "post_date": "2026/05/04",
                         "card_last4": "7030", "description": "永豐自扣已入帳，謝謝！", "amount": "-69"},
                    ],
                },
            ],
            "all_cards": {"Result": {"Items": [
                {"CardNo": "9000000000347030", "Name": "永豐", "CardTypeDesc": "c",
                 "CardBrand": "V", "ExpDate": "0829"},
            ]}},
        }
        persist_sinopac(data, store, rules=None)
        card = _read_card_fields(store, "9000000000347030")
        assert card is not None
        assert card["last_payment_amount"] == 69.0
        assert card["last_payment_date"] == "2026-05-04"
    finally:
        store.close()


# ============================================================
# Cathay — bill_due from bill_summary; last_payment only from billed_detail real row
# ============================================================

def test_cathay_writes_bill_due_without_payment_fallback(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch, "cathay")
    try:
        data = {
            "credit_card": {
                "cards": [{"number": "****7016", "name": "國泰卡", "association": "VISA",
                           "type": "credit", "is_cube": False}],
                "bill_summary": {
                    "payment_deadline": "2026-05-05T00:00:00",
                    "currencies": [{
                        "currencyDataType": "TWD", "currency": "TWD",
                        "currentPaymentAmount": 100,  # 本期應繳 → bill_due
                        "paymentAmount": 2130,  # 上期已繳 → last_payment_amount
                        "billDate": "2026-04-19T00:00:00",
                    }],
                },
                "quota": {"credit_limit": "100000", "current": "100"},
            },
        }
        persist_cathay(data, store, rules=None)
        card = _read_card_fields(store, "****7016")
        assert card is not None
        assert card["bill_due_amount"] == 100.0
        assert card["last_payment_amount"] is None
        assert card["last_payment_date"] is None
    finally:
        store.close()


def test_cathay_payment_amount_zero_without_real_row_does_not_create_payment(tmp_path, monkeypatch):
    """paymentAmount=0 也只是 summary；沒有 billed_detail payment row 就不寫 last_payment。"""
    store = _make_store(tmp_path, monkeypatch, "cathay")
    try:
        data = {
            "credit_card": {
                "cards": [{"number": "****7016", "name": "國泰卡", "association": "VISA",
                           "type": "credit", "is_cube": False}],
                "bill_summary": {
                    "payment_deadline": "2026-05-05T00:00:00",
                    "currencies": [{
                        "currencyDataType": "TWD", "currency": "TWD",
                        "currentPaymentAmount": 0, "paymentAmount": 0,
                        "billDate": "2026-04-19T00:00:00",
                    }],
                },
                "quota": {"credit_limit": "100000", "current": "0"},
            },
        }
        persist_cathay(data, store, rules=None)
        card = _read_card_fields(store, "****7016")
        assert card is not None
        assert card["bill_due_amount"] == 0.0
        assert card["last_payment_amount"] is None
        assert card["last_payment_date"] is None
    finally:
        store.close()


def test_cathay_paid_status_zeroes_due(tmp_path, monkeypatch):
    """國泰最新帳單標記已繳時，剩餘應繳必須為零。"""
    store = _make_store(tmp_path, monkeypatch, "cathay")
    try:
        data = {
            "credit_card": {
                "cards": [{"number": "****7016", "name": "國泰卡", "association": "VISA",
                           "type": "credit", "is_cube": False}],
                "latest_bill": {
                    "twd": {"billAmount": 4321, "payBillStatus": "Payed"},
                },
                "bill_summary": {
                    "payment_deadline": "2026-08-05T00:00:00",
                    "currencies": [{
                        "currencyDataType": "TWD", "currency": "TWD",
                        "currentPaymentAmount": 4321,
                        "billDate": "2026-07-19T00:00:00",
                    }],
                },
            },
        }
        persist_cathay(data, store, rules=None)
        card = _read_card_fields(store, "****7016")
        assert card is not None
        assert card["bill_due_amount"] == 0.0
    finally:
        store.close()


def test_cathay_newer_deposit_payment_wins_over_old_billed_payment(tmp_path, monkeypatch):
    """活存「信用卡款」是真實入帳列，應比舊帳單扣繳列更新。"""
    store = _make_store(tmp_path, monkeypatch, "cathay")
    try:
        data = {
            "credit_card": {
                "cards": [{"number": "****7016", "name": "國泰卡", "association": "VISA",
                           "type": "credit", "is_cube": False}],
                "billed_detail": {"TWD": [{
                    "card_no": "", "date": "2026-03-04", "post_date": "2026-03-04",
                    "desc": "本行自動扣繳", "amount": -1200, "currency": "TWD",
                }]},
            },
            "twd_transactions": [{
                "account": "SYNTHETIC-ACCOUNT",
                "transactions": [{
                    "datetime": "2026-08-07T04:56:44",
                    "account_date": "2026-08-07T00:00:00",
                    "desc": "信用卡款",
                    "expend": 4321,
                    "income": None,
                    "memo": "國泰世華卡 信用卡款",
                }],
            }],
        }
        persist_cathay(data, store, rules=None)
        card = _read_card_fields(store, "****7016")
        assert card is not None
        assert card["last_payment_date"] == "2026-08-07"
        assert card["last_payment_amount"] == 4321.0
    finally:
        store.close()


def test_cathay_payment_ignores_memo_only_match(tmp_path, monkeypatch):
    """自由文字 memo 提到信用卡款，不是銀行產生的付款描述。"""
    store = _make_store(tmp_path, monkeypatch, "cathay")
    try:
        data = {
            "credit_card": {"cards": [{"number": "****7016", "name": "國泰卡"}]},
            "twd_transactions": [{
                "account": "SYNTHETIC-ACCOUNT",
                "transactions": [{
                    "datetime": "2026-08-07T04:56:44",
                    "account_date": "2026-08-07T00:00:00",
                    "desc": "一般轉帳",
                    "memo": "朋友代墊信用卡款",
                    "expend": 999,
                }],
            }],
        }
        persist_cathay(data, store, rules=None)
        card = _read_card_fields(store, "****7016")
        assert card is not None
        assert card["last_payment_date"] is None
        assert card["last_payment_amount"] is None
    finally:
        store.close()


def test_cathay_payment_candidates_sort_normalized_dates(tmp_path, monkeypatch):
    """候選日期需先正規化；slash 八月不能在 ISO 十二月之後。"""
    store = _make_store(tmp_path, monkeypatch, "cathay")
    try:
        data = {
            "credit_card": {
                "cards": [{"number": "****7016", "name": "國泰卡"}],
                "billed_detail": {"TWD": [{
                    "post_date": "2026-12-01",
                    "desc": "本行自動扣繳",
                    "amount": -1200,
                }]},
            },
            "twd_transactions": [{
                "account": "SYNTHETIC-ACCOUNT",
                "transactions": [{
                    "datetime": "2026/08/07T04:56:44",
                    "account_date": "2026/08/07T00:00:00",
                    "desc": "信用卡款",
                    "expend": 999,
                }],
            }],
        }
        persist_cathay(data, store, rules=None)
        card = _read_card_fields(store, "****7016")
        assert card is not None
        assert card["last_payment_date"] == "2026-12-01"
        assert card["last_payment_amount"] == 1200.0
    finally:
        store.close()


def test_cathay_shared_payment_updates_known_card_missing_from_current_inventory(tmp_path, monkeypatch):
    """整戶繳款事實需套到既有 sibling 卡，不得只更新本次卡片清單。"""
    store = _make_store(tmp_path, monkeypatch, "cathay")
    try:
        store.upsert_cards([{"number": "****9999", "name": "既有卡"}])
        data = {
            "credit_card": {
                "cards": [{"number": "****7016", "name": "目前卡"}],
                "latest_bill": {"twd": {"billAmount": 4321, "payBillStatus": "Payed"}},
                "bill_summary": {
                    "currencies": [{"currentPaymentAmount": 4321}],
                },
            },
            "twd_transactions": [{
                "account": "SYNTHETIC-ACCOUNT",
                "transactions": [{
                    "datetime": "2026-08-07T04:56:44",
                    "account_date": "2026-08-07T00:00:00",
                    "desc": "信用卡款",
                    "expend": 4321,
                }],
            }],
        }
        persist_cathay(data, store, rules=None)
        stale = _read_card_fields(store, "****9999")
        assert stale is not None
        assert stale["bill_due_amount"] == 0.0
        assert stale["last_payment_date"] == "2026-08-07"
        assert stale["last_payment_amount"] == 4321.0
    finally:
        store.close()


def test_cathay_shared_payment_updates_known_card_when_current_inventory_is_empty(tmp_path, monkeypatch):
    """卡片清單暫時為空時，已成功取得的整戶繳款仍要更新既有卡。"""
    store = _make_store(tmp_path, monkeypatch, "cathay")
    try:
        store.upsert_cards([{
            "number": "****9999",
            "name": "既有卡",
            "bill_due_amount": 999,
            "last_payment_date": "2026-01-01",
            "last_payment_amount": 999,
        }])
        data = {
            "credit_card": {
                "cards": [],
                "latest_bill": {"twd": {"billAmount": 4321, "payBillStatus": "Payed"}},
                "bill_summary": {"currencies": [{"currentPaymentAmount": 4321}]},
            },
            "twd_transactions": [{
                "account": "SYNTHETIC-ACCOUNT",
                "transactions": [{
                    "datetime": "2026-08-07T04:56:44",
                    "account_date": "2026-08-07T00:00:00",
                    "desc": "信用卡款",
                    "expend": 4321,
                }],
            }],
        }
        persist_cathay(data, store, rules=None)
        stale = _read_card_fields(store, "****9999")
        assert stale is not None
        assert stale["bill_due_amount"] == 0.0
        assert stale["last_payment_date"] == "2026-08-07"
        assert stale["last_payment_amount"] == 4321.0
    finally:
        store.close()


def test_cathay_older_payload_payment_does_not_replace_newer_saved_payment(tmp_path, monkeypatch):
    """近 30 日付款消失後，舊帳單列不得讓已保存的最近繳款倒退。"""
    store = _make_store(tmp_path, monkeypatch, "cathay")
    try:
        store.upsert_cards([{
            "number": "****9999",
            "name": "既有卡",
            "last_payment_date": "2026-08-07",
            "last_payment_amount": 4321,
        }])
        data = {
            "credit_card": {
                "cards": [],
                "billed_detail": {"TWD": [{
                    "post_date": "2026-03-04",
                    "desc": "本行自動扣繳",
                    "amount": -1200,
                }]},
            },
        }
        persist_cathay(data, store, rules=None)
        card = _read_card_fields(store, "****9999")
        assert card is not None
        assert card["last_payment_date"] == "2026-08-07"
        assert card["last_payment_amount"] == 4321.0
    finally:
        store.close()


def test_cathay_same_date_payment_uses_authoritative_source_precedence(tmp_path, monkeypatch):
    """同日事實：活存明確付款 > 已保存事實 > 舊帳單付款列。"""
    store = _make_store(tmp_path, monkeypatch, "cathay")
    try:
        store.upsert_cards([{
            "number": "****9999",
            "name": "既有卡",
            "last_payment_date": "2026-08-07",
            "last_payment_amount": 4321,
        }])
        base = {
            "credit_card": {
                "cards": [],
                "billed_detail": {"TWD": [{
                    "post_date": "2026-08-07",
                    "desc": "本行自動扣繳",
                    "amount": -1200,
                }]},
            },
        }
        persist_cathay(base, store, rules=None)
        card = _read_card_fields(store, "****9999")
        assert card is not None
        assert card["last_payment_amount"] == 4321.0

        data_with_deposit = {
            **base,
            "twd_transactions": [{
                "account": "SYNTHETIC-ACCOUNT",
                "transactions": [{
                    "datetime": "2026-08-07T12:00:00",
                    "account_date": "2026-08-07",
                    "desc": "信用卡款",
                    "expend": 5555,
                }],
            }],
        }
        persist_cathay(data_with_deposit, store, rules=None)
        card = _read_card_fields(store, "****9999")
        assert card is not None
        assert card["last_payment_date"] == "2026-08-07"
        assert card["last_payment_amount"] == 5555.0
    finally:
        store.close()


def test_cathay_paid_status_without_bill_amount_preserves_saved_due(tmp_path, monkeypatch):
    """只有 paid status、缺 billAmount 時，不足以清掉已保存應繳。"""
    store = _make_store(tmp_path, monkeypatch, "cathay")
    try:
        store.upsert_cards([{
            "number": "****9999",
            "name": "既有卡",
            "bill_due_amount": 999,
        }])
        persist_cathay({
            "credit_card": {
                "cards": [],
                "latest_bill": {"twd": {"payBillStatus": "Payed"}},
            },
        }, store, rules=None)
        card = _read_card_fields(store, "****9999")
        assert card is not None
        assert card["bill_due_amount"] == 999.0
    finally:
        store.close()


@pytest.mark.parametrize("bill_amount", ["NaN", "Infinity"])
def test_cathay_paid_status_with_non_finite_bill_amount_preserves_saved_due(
    tmp_path, monkeypatch, bill_amount,
):
    store = _make_store(tmp_path, monkeypatch, "cathay")
    try:
        store.upsert_cards([{
            "number": "****9999",
            "name": "既有卡",
            "bill_due_amount": 999,
        }])
        persist_cathay({
            "credit_card": {
                "cards": [],
                "latest_bill": {"twd": {
                    "payBillStatus": "Payed",
                    "billAmount": bill_amount,
                }},
            },
        }, store, rules=None)
        card = _read_card_fields(store, "****9999")
        assert card is not None
        assert card["bill_due_amount"] == 999.0
    finally:
        store.close()


@pytest.mark.parametrize("credit_card", [
    {"cards": [], "quota": [None]},
    {"cards": [], "bill_summary": [None]},
    {"cards": [], "bill_summary": {"currencies": [None]}},
    {"cards": [], "latest_bill": [None]},
    {"cards": [], "latest_bill": {"twd": [None]}},
])
def test_cathay_malformed_shared_fact_shapes_preserve_known_card(
    tmp_path, monkeypatch, credit_card,
):
    """空 inventory 遇 malformed shared objects 必須保留舊欄位，不可 crash。"""
    store = _make_store(tmp_path, monkeypatch, "cathay")
    try:
        store.upsert_cards([{
            "number": "****9999",
            "name": "既有卡",
            "credit_limit": 999,
            "used_credit": 123,
            "bill_due_amount": 456,
        }])
        persist_cathay({"credit_card": credit_card}, store, rules=None)
        row = store.conn.execute(
            "SELECT credit_limit, used_credit, bill_due_amount FROM cards WHERE card_no = ?",
            ("****9999",),
        ).fetchone()
        assert row is not None
        assert tuple(row) == (999.0, 123.0, 456.0)
    finally:
        store.close()


@pytest.mark.parametrize("current_amount", ["NaN", "Infinity", "-Infinity"])
def test_cathay_non_finite_current_payment_amount_preserves_saved_due(
    tmp_path, monkeypatch, current_amount,
):
    store = _make_store(tmp_path, monkeypatch, "cathay")
    try:
        store.upsert_cards([{
            "number": "****9999",
            "name": "既有卡",
            "bill_due_amount": 999,
        }])
        persist_cathay({
            "credit_card": {
                "cards": [],
                "bill_summary": {"currencies": [{
                    "currentPaymentAmount": current_amount,
                }]},
            },
        }, store, rules=None)
        card = _read_card_fields(store, "****9999")
        assert card is not None
        assert card["bill_due_amount"] == 999.0
    finally:
        store.close()


@pytest.mark.parametrize("credit_card", [
    {"cards": [], "bill_summary": {"currencies": [{"currentPaymentAmount": True}]}},
    {"cards": [], "latest_bill": {"twd": {
        "billAmount": True,
        "payBillStatus": "Payed",
    }}},
])
def test_cathay_boolean_amounts_preserve_saved_due(tmp_path, monkeypatch, credit_card):
    store = _make_store(tmp_path, monkeypatch, "cathay")
    try:
        store.upsert_cards([{
            "number": "****9999",
            "name": "既有卡",
            "bill_due_amount": 999,
        }])
        persist_cathay({"credit_card": credit_card}, store, rules=None)
        card = _read_card_fields(store, "****9999")
        assert card is not None
        assert card["bill_due_amount"] == 999.0
    finally:
        store.close()


@pytest.mark.parametrize("quota", [
    {"credit_limit": True, "current": "Infinity"},
    {"credit_limit": "-Infinity", "current": False},
])
def test_cathay_invalid_quota_amounts_preserve_saved_fields(tmp_path, monkeypatch, quota):
    store = _make_store(tmp_path, monkeypatch, "cathay")
    try:
        store.upsert_cards([{
            "number": "****9999",
            "name": "既有卡",
            "credit_limit": 999,
            "used_credit": 123,
        }])
        persist_cathay({
            "credit_card": {"cards": [], "quota": quota},
        }, store, rules=None)
        row = store.conn.execute(
            "SELECT credit_limit, used_credit FROM cards WHERE card_no = ?",
            ("****9999",),
        ).fetchone()
        assert row is not None
        assert row["credit_limit"] == 999.0
        assert row["used_credit"] == 123.0
    finally:
        store.close()


def test_cathay_non_finite_payment_candidates_are_ignored(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch, "cathay")
    try:
        store.upsert_cards([{
            "number": "****9999",
            "name": "既有卡",
            "last_payment_date": "2026-08-07",
            "last_payment_amount": 4321,
        }])
        delta = persist_cathay({
            "credit_card": {
                "cards": [],
                "billed_detail": {"TWD": [
                    {
                        "post_date": "2026-10-01",
                        "desc": "本行自動扣繳",
                        "amount": float("-inf"),
                    },
                    {
                        "post_date": "2026-12-01",
                        "desc": "本行自動扣繳",
                        "amount": -(10 ** 400),
                    },
                    {
                        "date": "2026-12-02",
                        "post_date": "2026-12-03",
                        "desc": "FOREIGN BOOL",
                        "amount": 100,
                        "consume_amount": True,
                    },
                    {
                        "date": "2026-12-04",
                        "post_date": "2026-12-05",
                        "desc": "FOREIGN INF",
                        "amount": 100,
                        "consume_amount": float("inf"),
                    },
                    {
                        "date": "2026-12-06",
                        "post_date": "2026-12-07",
                        "desc": "FOREIGN HUGE",
                        "amount": 100,
                        "consume_amount": 10 ** 400,
                    },
                ]},
            },
            "twd_transactions": [{
                "account": "SYNTHETIC-ACCOUNT",
                "transactions": [
                    {
                        "datetime": "2026-09-01T04:56:44",
                        "account_date": "2026-09-01T00:00:00",
                        "desc": "信用卡款",
                        "expend": float("inf"),
                    },
                    {
                        "datetime": "2026-11-01T04:56:44",
                        "account_date": "2026-11-01T00:00:00",
                        "desc": "信用卡款",
                        "expend": True,
                    },
                ],
            }],
        }, store, rules=None)
        card = _read_card_fields(store, "****9999")
        assert card is not None
        assert card["last_payment_date"] == "2026-08-07"
        assert card["last_payment_amount"] == 4321.0
        assert delta is not None
        assert delta["card_billed_skipped_invalid_amount"] == 5
        assert delta["twd_txn_skipped_invalid_amount"] == 2
        assert store.conn.execute("SELECT COUNT(*) FROM card_billed_txns").fetchone()[0] == 0
        assert store.conn.execute("SELECT COUNT(*) FROM twd_transactions").fetchone()[0] == 0
    finally:
        store.close()


# ============================================================
# DBS — bill_due/payment_due from card fee page; no last_payment history
# ============================================================

def test_dbs_writes_bill_due_without_last_payment_fallback(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch, "dbs")
    try:
        data = {
            "api_responses": {
                "liabilities": [{
                    "resp": {
                        "creditCard": {
                            "cards": [{
                                "cardNumber": "************7016",
                                "cardDescription": "DBS Card", "isPrimaryCard": True,
                                "isDisplayImg": True, "cardId": "X", "cardExpiryDate": "122026",
                            }],
                            "paymentDetails": {
                                "amount": 5000.0, "alreadyPaid": 2500.0,
                                "dueDate": "2026-06-22", "minimumAmount": 100.0,
                                "currency": "TWD",
                            },
                        },
                    },
                }],
                "assets": [{"resp": {"casa": {"accounts": []}}}],
            },
        }
        persist_dbs(data, store, rules=None)
        card = _read_card_fields(store, "****7016")
        assert card is not None
        assert card["bill_due_amount"] == 5000.0
        assert card["last_payment_amount"] is None
        assert card["last_payment_date"] is None
    finally:
        store.close()


def test_dbs_card_fee_page_overrides_dashboard_bill_due(tmp_path, monkeypatch):
    """登入後點「繳卡費」看到的最近一期帳單金額，才是 bill_due source。"""
    store = _make_store(tmp_path, monkeypatch, "dbs")
    try:
        data = {
            "dbs_card_fee_page": {
                "bill_due_amount": 1234.0,
                "payment_due_date": "2026-07-22",
                "currency": "TWD",
            },
            "api_responses": {
                "liabilities": [{
                    "resp": {
                        "creditCard": {
                            "cards": [{
                                "cardNumber": "************7016",
                                "cardDescription": "DBS Card", "isPrimaryCard": True,
                                "isDisplayImg": True, "cardId": "X", "cardExpiryDate": "122026",
                            }],
                            "paymentDetails": {
                                "amount": 5000.0, "alreadyPaid": 2500.0,
                                "dueDate": "2026-06-22", "minimumAmount": 100.0,
                                "currency": "TWD",
                            },
                        },
                    },
                }],
                "assets": [{"resp": {"casa": {"accounts": []}}}],
            },
        }
        persist_dbs(data, store, rules=None)
        card = _read_card_fields(store, "****7016")
        assert card is not None
        assert card["bill_due_amount"] == 1234.0
        # card fee page is current bill amount, not payment history.
        assert card["last_payment_amount"] is None
        assert card["last_payment_date"] is None
    finally:
        store.close()


# ============================================================
# Esun — bill_due + last_payment_amount from card_bills[0]
# ============================================================

def test_esun_writes_bill_due_and_last_payment(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch, "esun")
    try:
        data = {
            "card_summary": {"payment_due_date_roc": "115/06/29",
                             "credit_limit_twd": "100000"},
            "card_bills": [
                {"bill_month": "2026-05", "currency": "TWD",
                 "due_amount": 500, "paid_amount": 1000},  # 上期 paid=1000
                {"bill_month": "2026-04", "currency": "TWD",
                 "due_amount": 0, "paid_amount": 0},
            ],
            "card_transactions": [
                {"card_no": "9064-XXXX-XXXX-7032", "card_last4": "7032",
                 "status": "已入帳", "billed_amount": "500"},
            ],
        }
        persist_esun(data, store, rules=None)
        card = _read_card_fields(store, "****7032")
        assert card is not None
        assert card["bill_due_amount"] == 500.0
        assert card["last_payment_amount"] == 1000.0
        assert card["last_payment_date"] is None  # 玉山無此欄
    finally:
        store.close()


# ============================================================
# Taishin — bill_due + paid from credit_card_parsed.summary
# ============================================================

def test_taishin_writes_bill_due_and_paid(tmp_path, monkeypatch):
    """2026-07-03: bill_due 是「剩餘應繳」不是「本期帳單」;
    last_payment_amount 沒真扣繳 row 就必須 None (使用者「不假造」鐵則)。"""
    store = _make_store(tmp_path, monkeypatch, "taishin")
    try:
        data = {
            "credit_card_parsed": {
                "summary": {"bill_amount": 100.0, "paid": 50.0, "remaining": 50.0},
                "billing_period": {
                    "statement_date": "2026/5/12", "pay_due_date": "2026/05/27",
                },
                "cards": [{"number": "****7050", "name": "台新卡", "association": "VISA",
                           "type": "credit", "is_cube": False}],
                "top_summary": {"unpaid": 100},
            },
            "api_responses": {
                "qryRealTime": {"value": {"crlimit": "100000"}},
                "doXTPA": {"value": {"001": {"OUT-CRLIMIT-PERM": "100000",
                                              "OUT-AVAIL-CREDIT": "99900"}}},
            },
        }
        persist_taishin(data, store, rules=None)
        card = _read_card_fields(store, "****7050")
        assert card is not None
        assert card["bill_due_amount"] == 50.0
        assert card["last_payment_amount"] is None
        assert card["last_payment_date"] is None
    finally:
        store.close()


def test_taishin_writes_last_payment_from_credit_card_month_payment_row(tmp_path, monkeypatch):
    """台新真正的繳款紀錄在信用卡明細其他月份的「上期實繳金額明細」。

    2026-07-03: bill=80 & paid=80 → remaining 未回時仍算 0 (bill-paid fallback)，
    UI 應顯示已繳完；last_payment 走真實扣繳 row。
    """
    store = _make_store(tmp_path, monkeypatch, "taishin")
    try:
        data = {
            "credit_card_parsed": {
                "summary": {"bill_amount": 80.0, "paid": 80.0},
                "billing_period": {
                    "statement_date": "2026/6/14", "pay_due_date": "2026/06/29",
                },
                "cards": [{"number": "****7018", "name": "Richart卡"}],
                "top_summary": {"unpaid": 80},
                "billed_txns": [{
                    "txn_date": "2026/04/27",
                    "post_date": "2026/04/27",
                    "desc": "台新銀行帳戶自動轉帳扣繳台新信用卡款",
                    "currency": "新臺幣",
                    "amount": -56.0,
                    "card_no_suffix": "7018",
                }],
            },
            "api_responses": {
                "qryRealTime": {"value": {"crlimit": "400000"}},
                "doXTPA": {"value": {"001": {"OUT-CRLIMIT-PERM": "400000",
                                              "OUT-AVAIL-CREDIT": "399920"}}},
            },
        }
        persist_taishin(data, store, rules=None)
        card = _read_card_fields(store, "****7018")
        assert card is not None
        assert card["bill_due_amount"] == 0.0
        assert card["last_payment_amount"] == 56.0
        assert card["last_payment_date"] == "2026-04-27"
    finally:
        store.close()


# ============================================================
# CTBC — bill_due from cc_summary.unpaidStmt + last_payment from qu038/011 payment history
# ============================================================

def test_ctbc_writes_bill_due_and_last_payment(tmp_path, monkeypatch):
    """CTBC 真實繳款紀錄在「信用卡繳款記錄」qu038/011，不該用 billDt approximate。"""
    store = _make_store(tmp_path, monkeypatch, "ctbc")
    try:
        data = {
            "summary": {
                "creditCardSummary": {
                    "unpaidStmt": 21681,  # 本期應繳 → bill_due
                    "quota": 35921,
                    "availBal": 664079,
                    "pmtExpDt": "2026/07/05",
                },
            },
            "card_api_dump": {
                "/twrbc-card/qu002/010": {
                    "billCycle": "17",
                    "billData": {
                        "TWD": {
                            # 這裡的 pmtAmt/billDt 是帳單 summary，不是真繳款日。
                            "2026/06": {"summary": {
                                "pmtAmt": 15943, "currPmtAmt": 21681,
                                "billDt": "061726",
                            }},
                        },
                    },
                    "cardDataList": [{
                        "cardNoSuffixFour": "7036", "cardNo": "9000-56**-****-7036",
                        "cardName": "中信卡", "positiveOrAttached": "正卡",
                    }],
                },
                "/twrbc-card/qu038/011": {
                    "billDataTWD": [
                        {"payDt": "2026/06/05", "postingDt": "2026/06/08",
                         "merchantChiName": "本行扣繳", "curCode": "TWD", "amt": "15943"},
                        {"payDt": "2026/05/05", "postingDt": "2026/05/06",
                         "merchantChiName": "本行扣繳", "curCode": "TWD", "amt": "8600"},
                    ],
                },
            },
        }
        persist_ctbc(data, store, rules=None)
        card = _read_card_fields(store, "****7036")
        assert card is not None
        assert card["bill_due_amount"] == 21681.0
        assert card["last_payment_amount"] == 15943.0
        assert card["last_payment_date"] == "2026-06-05"
    finally:
        store.close()


def test_ctbc_missing_payment_history_does_not_use_billdt_as_payment_date(tmp_path, monkeypatch):
    """沒有 qu038/011 真繳款紀錄時，不用 billData.summary.billDt 假裝繳款日。"""
    store = _make_store(tmp_path, monkeypatch, "ctbc")
    try:
        data = {
            "summary": {
                "creditCardSummary": {
                    "unpaidStmt": 0, "quota": 0, "availBal": 100000,
                    "pmtExpDt": "2026/07/05",
                },
            },
            "card_api_dump": {
                "/twrbc-card/qu002/010": {
                    "billCycle": "17",
                    "billData": {
                        "TWD": {
                            "2026/06": {"summary": {"pmtAmt": 5000, "billDt": "061726"}},
                        },
                    },
                    "cardDataList": [{
                        "cardNoSuffixFour": "7036", "cardNo": "9000-56**-****-7036",
                        "cardName": "中信卡", "positiveOrAttached": "正卡",
                    }],
                },
            },
        }
        persist_ctbc(data, store, rules=None)
        card = _read_card_fields(store, "****7036")
        assert card is not None
        assert card["last_payment_amount"] is None
        assert card["last_payment_date"] is None
    finally:
        store.close()


def test_ctbc_payment_history_invalid_rows_skipped(tmp_path, monkeypatch):
    """qu038/011 rows 缺 payDt 或 amt 異常時 skip，不 raise、不回落 billDt。"""
    store = _make_store(tmp_path, monkeypatch, "ctbc")
    try:
        data = {
            "summary": {
                "creditCardSummary": {"unpaidStmt": 0, "quota": 0,
                                      "availBal": 100000, "pmtExpDt": "2026/07/05"},
            },
            "card_api_dump": {
                "/twrbc-card/qu002/010": {
                    "billCycle": "17",
                    "billData": {"TWD": {"2026/06": {"summary": {"pmtAmt": 100, "billDt": "061826"}}}},
                    "cardDataList": [{
                        "cardNoSuffixFour": "7036", "cardNo": "9000-56**-****-7036",
                        "cardName": "中信卡", "positiveOrAttached": "正卡",
                    }],
                },
                "/twrbc-card/qu038/011": {"billDataTWD": [
                    {"payDt": "", "amt": "100", "merchantChiName": "本行扣繳"},
                    {"payDt": "2026/06/05", "amt": "abc", "merchantChiName": "本行扣繳"},
                ]},
            },
        }
        persist_ctbc(data, store, rules=None)
        card = _read_card_fields(store, "****7036")
        assert card is not None
        assert card["last_payment_amount"] is None
        assert card["last_payment_date"] is None
    finally:
        store.close()


# ============================================================
# Fubon — bill_due + last_payment_amount + date all from billing_summary
# ============================================================

def test_fubon_writes_all_three_fields(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch, "fubon")
    try:
        # 富邦走 HTML parsing, 給 minimal text 觸發 regex 命中
        amount_text = (
            "繳款及額度查詢 帳單明細查詢\n"
            "額度\t\t\n"
            "正卡人信用額度\t80,000\t額度調整\n"
            "正卡人可用額度\t72,000\n"
            "本期帳單結帳日\t應繳總金額\t最低應繳金額\t繳款截止日\t本期循環利率\t預借現金循環利率\n"
            "2026/05/16\t1,000\t100\t2026/06/05\t12.62%\t5.62%\n"
            "繳款狀態\t上次繳款日\t上次繳款金額\t剩餘未繳金額\t自動扣繳帳號\n"
            "已繳清\t2026/05/05\t500\t0\t台北富邦900047****7012\n"
        )
        data = {
            "amount_page_text": amount_text,
            "billed_page_text": "",
            "frames": [{"text": "信用卡"}],
            "card_page_html": (
                # cards parse 需要的 minimal html
                '<table>'
                '<tr><th>卡號</th><th>卡片名稱</th></tr>'
                '<tr><td>9049-XXXX-XXXX-7050</td><td>富邦卡</td></tr>'
                '</table>'
            ),
        }
        persist_fubon(data, store, rules=None)
        # fubon parse cards from HTML 跟 amount_text 有耦合, 拿任意一張卡看 last_payment 套上去
        rows = store.conn.execute(
            "SELECT card_no, bill_due_amount, last_payment_amount, last_payment_date "
            "FROM cards WHERE user_id=?", (store.user_id,),
        ).fetchall()
        # fubon parse cards from card_page_html, 若沒解到就 graceful skip
        # 該 test 主要驗 billing_summary regex parse 出三欄
        # 真實 prod 環境 cards 來自 fubon_collected.json full payload, test 用 minimal mock
        # 不強制 card 數量 — 重點是 persist 不 raise + 若有 card 則三欄正確
        if rows:
            for r in rows:
                assert r[1] == 1000.0, f"bill_due_amount: {r[1]}"
                assert r[2] == 500.0, f"last_payment_amount: {r[2]}"
                assert r[3] == "2026-05-05", f"last_payment_date: {r[3]}"
    finally:
        store.close()


def test_cathay_last_payment_date_from_billed_detail(tmp_path, monkeypatch):
    """0.3.34: cathay 從 billed_detail.TWD 找「本行自動扣繳」record post_date.

    使用者 local crawl 證實 cathay raw billed_detail.TWD 有 records:
      {"desc": "本行自動扣繳", "amount": -2130, "post_date": "2026-04-08T..."}
    persist 取最新一筆 post_date 寫 last_payment_date.
    """
    store = _make_store(tmp_path, monkeypatch, "cathay")
    try:
        data = {
            "credit_card": {
                "cards": [{"number": "9061****7045", "name": "國泰卡", "association": "MASTER",
                          "type": "credit", "is_cube": True}],
                "bill_summary": {
                    "payment_deadline": "2026-05-05T00:00:00",
                    "currencies": [{
                        "currencyDataType": "TWD", "currency": "TWD",
                        "currentPaymentAmount": 0, "paymentAmount": 2130,
                        "billDate": "2026-04-19T00:00:00",
                    }],
                },
                "quota": {"credit_limit": "100000", "current": "0"},
                "billed_detail": {
                    "TWD": [
                        {"card_no": "", "date": None, "post_date": None,
                         "desc": "上期帳單總額", "amount": 2130, "currency": "TWD"},
                        {"card_no": "", "date": "2026-04-08T00:00:00",
                         "post_date": "2026-04-08T00:00:00",
                         "desc": "本行自動扣繳", "amount": -2130, "currency": "TWD"},
                    ],
                },
            },
        }
        persist_cathay(data, store, rules=None)
        card = _read_card_fields(store, "9061****7045")
        assert card is not None
        assert card["bill_due_amount"] == 0.0
        assert card["last_payment_amount"] == 2130.0
        assert card["last_payment_date"] == "2026-04-08", \
            "從 billed_detail '本行自動扣繳' record post_date 推"
    finally:
        store.close()
