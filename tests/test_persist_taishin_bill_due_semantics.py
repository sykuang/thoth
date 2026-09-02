"""Regression tests for persist_taishin bill_due semantics.

2026-07-03 (0.3.63): 台新 1409 卡 auto-debit case
  summary.bill_amount = 80, summary.paid = 80, summary.remaining = 0
舊 persist 錯把 bill_amount 寫進 cards.bill_due_amount，
frontend bill_status 誤判「未繳」。修正後 bill_due_amount 只吃 remaining。

也順便驗 payment 事實 (last_payment_amount/date) 仍走真實 billed_txns 扣繳
row，避免 summary.paid 誤植 (那是本期已繳「金額」但沒日期，日期只能來自 row)。
"""
from __future__ import annotations



from backend.banks.taishin import _taishin_card_bill_fact
from backend.core.persist.taishin import persist_taishin
from backend.core.store import BankStore
from tests.taishin_fixtures import with_taishin_history


def _build_data(*, summary: dict, billed_txns: list[dict] | None = None) -> dict:
    return {
        "api_responses": {},
        "credit_card_parsed": {
            "fetch_ok": True,
            "cards": [
                {
                    "number": "****7018",
                    "name": "Richart卡",
                    "currency": "TWD",
                }
            ],
            "billing_period": {
                "statement_date": "2026/6/14",
                "pay_due_date": "2026/06/29",
                "bill_amount": summary.get("bill_amount"),
                "min_pay": 80,
            },
            "summary": summary,
            "billed_txns": billed_txns or [],
            "pending_txns": [],
            "top_summary": {},
        },
    }


def _open_store(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    return BankStore("taishin", user_id=1)


def test_taishin_collector_fact_uses_remaining_and_real_period_keys():
    parsed = _build_data(summary={"bill_amount": 80, "paid": 80, "remaining": 0})[
        "credit_card_parsed"
    ]

    assert _taishin_card_bill_fact(parsed) == {
        "scope": "bank",
        "status": "no_payment_required",
        "remaining_due": 0.0,
        "statement_close_date": "2026-06-14",
        "payment_due_date": "2026-06-29",
    }


def test_taishin_failed_parse_cannot_clear_saved_due():
    parsed = _build_data(summary={"remaining": 0})["credit_card_parsed"]
    parsed["fetch_ok"] = False

    assert _taishin_card_bill_fact(parsed) is None


def test_taishin_collector_carries_real_payment_pair():
    parsed = _build_data(
        summary={"remaining": 0},
        billed_txns=[{
            "amount": -80, "post_date": "2026/06/20", "desc": "自動扣繳",
        }],
    )["credit_card_parsed"]

    fact = _taishin_card_bill_fact(parsed)

    assert fact is not None
    assert fact.get("last_payment_amount") == 80.0
    assert fact.get("last_payment_date") == "2026-06-20"


def _query_card(store: BankStore) -> dict:
    row = store.conn.execute(
        "SELECT bill_due_amount, last_payment_amount, last_payment_date, "
        "payment_due_date FROM cards WHERE card_no = ?",
        ("****7018",),
    ).fetchone()
    assert row is not None, "cards row missing"
    return {
        "bill_due_amount": row[0],
        "last_payment_amount": row[1],
        "last_payment_date": row[2],
        "payment_due_date": row[3],
    }


def test_taishin_bill_due_uses_summary_remaining_not_bill_amount(tmp_path, monkeypatch):
    """本期已繳完 (remaining=0)：bill_due_amount 必須寫 0，不是 80。"""
    store = _open_store(tmp_path, monkeypatch)
    try:
        data = _build_data(summary={
            "prev_balance": 0.0,
            "new_charges": 80.0,
            "bill_amount": 80.0,
            "min_pay": 80.0,
            "paid": 80.0,
            "remaining": 0.0,
        })
        persist_taishin(with_taishin_history(data), store)
        card = _query_card(store)
        assert card["bill_due_amount"] == 0
        assert card["payment_due_date"] == "2026-06-29"
    finally:
        store.close()


def test_taishin_bill_due_reflects_partial_payment(tmp_path, monkeypatch):
    """部分繳款 (bill=80, paid=20, remaining=60)：bill_due_amount = 60。"""
    store = _open_store(tmp_path, monkeypatch)
    try:
        data = _build_data(summary={
            "prev_balance": 0.0,
            "new_charges": 80.0,
            "bill_amount": 80.0,
            "min_pay": 20.0,
            "paid": 20.0,
            "remaining": 60.0,
        })
        persist_taishin(with_taishin_history(data), store)
        card = _query_card(store)
        assert card["bill_due_amount"] == 60
    finally:
        store.close()


def test_taishin_bill_due_falls_back_to_bill_minus_paid_when_remaining_missing(tmp_path, monkeypatch):
    """Raw 沒吐 remaining 時，用 bill_amount - paid 計算。"""
    store = _open_store(tmp_path, monkeypatch)
    try:
        data = _build_data(summary={
            "bill_amount": 100.0,
            "paid": 30.0,
            # 沒有 remaining
        })
        persist_taishin(with_taishin_history(data), store)
        card = _query_card(store)
        assert card["bill_due_amount"] == 70
    finally:
        store.close()


def test_taishin_last_payment_only_from_real_billed_payment_row(tmp_path, monkeypatch):
    """last_payment_amount/date 只能從真實 billed_txns 扣繳 row 抓，
    不能誤把 summary.paid 當 last_payment_amount (那是本期已繳「金額」，
    沒有日期，且已被 remaining 涵蓋)。"""
    store = _open_store(tmp_path, monkeypatch)
    try:
        data = _build_data(
            summary={
                "bill_amount": 80.0,
                "paid": 80.0,
                "remaining": 0.0,
            },
            billed_txns=[
                {
                    "txn_date": "2026/06/29",
                    "post_date": "2026/06/29",
                    "desc": "台新銀行帳戶自動轉帳扣繳台新信用卡款",
                    "amount": -80.0,
                    "currency": "新臺幣",
                    "card_no_suffix": "7018",
                },
                {
                    "txn_date": "2026/05/21",
                    "post_date": "2026/05/22",
                    "desc": "臺北大眾捷運股份有限公司",
                    "amount": 40.0,
                    "currency": "新臺幣",
                    "card_no_suffix": "7018",
                },
            ],
        )
        persist_taishin(with_taishin_history(data), store)
        card = _query_card(store)
        assert card["last_payment_amount"] == 80
        assert card["last_payment_date"] == "2026-06-29"
        assert card["bill_due_amount"] == 0
    finally:
        store.close()


def test_taishin_no_real_payment_row_keeps_last_payment_null(tmp_path, monkeypatch):
    """沒有真實扣繳 row 就不寫 last_payment_*（保「不假造」鐵則）。"""
    store = _open_store(tmp_path, monkeypatch)
    try:
        data = _build_data(summary={
            "bill_amount": 80.0,
            "paid": 80.0,
            "remaining": 0.0,
        })
        persist_taishin(with_taishin_history(data), store)
        card = _query_card(store)
        assert card["last_payment_amount"] is None
        assert card["last_payment_date"] is None
        assert card["bill_due_amount"] == 0
    finally:
        store.close()


def test_taishin_billed_missing_post_date_stays_null(tmp_path, monkeypatch):
    store = _open_store(tmp_path, monkeypatch)
    try:
        data = _build_data(
            summary={"remaining": 0},
            billed_txns=[{
                "txn_date": "2026/06/29",
                "post_date": None,
                "desc": "測試消費",
                "amount": 80.0,
                "currency": "新臺幣",
                "card_no_suffix": "7018",
            }],
        )
        persist_taishin(with_taishin_history(data), store)
        row = store.conn.execute(
            "SELECT consume_date, post_date FROM card_billed_txns"
        ).fetchone()
        assert row is not None
        assert tuple(row) == ("2026-06-29", None)
    finally:
        store.close()
