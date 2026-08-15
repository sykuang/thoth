"""驗證 persist_cathay filter「上期帳單總額」這類 summary header row。

2026-06-20 修：Cathay 帳單 API 的 tradeData[] 包含帳單頂端的「上期」摘要列
（date=None, post_date=None, card_no='', desc='上期帳單總額', amount=2130）。
這列**不是真實交易**，是月結頁面的開頭小計，必須在 persist 層 filter 掉。

判定條件：consume_date AND post_date 同時 NULL → 一定不是交易（真實刷卡至少有消費日）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.persist import persist_cathay
from backend.core.store import BankStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch):
    from backend.core import store as store_mod
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path, raising=True)
    s = BankStore("cathay_test")
    yield s
    s.close()


def _persist(store, billed_detail: dict, **extra) -> dict:
    """run persist_cathay with given billed_detail, return delta."""
    data = {
        "accounts": [],
        "balance_history": [],
        "credit_card": {
            "cards": [],
            "billed_detail": billed_detail,
            **extra,
        },
    }
    return persist_cathay(data, store, rules=[])


def test_cathay_filters_previous_bill_summary_row(store):
    """「上期帳單總額」(date=None, post_date=None) 不該入庫。"""
    delta = _persist(store, {
        "TWD": [
            # summary row — 該被 filter
            {
                "card_no": "",
                "date": None,
                "post_date": None,
                "desc": "上期帳單總額",
                "amount": 2130,
                "currency": "TWD",
                "consume_country": "",
                "consume_currency": "",
                "consume_amount": 0,
            },
            # real txn — 該入庫
            {
                "card_no": "****7016",
                "date": "2026-04-08T00:00:00",
                "post_date": "2026-04-08T00:00:00",
                "desc": "本行自動扣繳",
                "amount": -2130,
                "currency": "TWD",
                "consume_country": "",
                "consume_currency": "",
                "consume_amount": 0,
            },
        ],
    })
    assert delta["card_billed_new"] == 1
    assert delta.get("card_billed_skipped_summary") == 1

    # 驗 DB 只剩 real txn
    rows = store.conn.execute(
        "SELECT description, consume_date, post_date FROM card_billed_txns"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["description"] == "本行自動扣繳"
    assert rows[0]["consume_date"] == "2026-04-08T00:00:00"


def test_cathay_filters_only_when_both_dates_null(store):
    """只有 consume_date AND post_date 同 NULL 才 filter；單側 NULL 不該 filter（保守）。"""
    delta = _persist(store, {
        "TWD": [
            # 只 consume_date NULL — 不 filter (可能是入帳日揭露 + 補登)
            {
                "card_no": "****7016",
                "date": None,
                "post_date": "2026-04-08T00:00:00",
                "desc": "邊緣 case 入帳日有消費日缺",
                "amount": 100,
                "currency": "TWD",
                "consume_country": "",
                "consume_currency": "",
                "consume_amount": 0,
            },
            # 只 post_date NULL — 不 filter；store 必須原樣保留 NULL
            {
                "card_no": "****7016",
                "date": "2026-05-01T00:00:00",
                "post_date": None,
                "desc": "邊緣 case 消費日有入帳日缺",
                "amount": 200,
                "currency": "TWD",
                "consume_country": "",
                "consume_currency": "",
                "consume_amount": 0,
            },
        ],
    })
    assert delta["card_billed_new"] == 2
    assert "card_billed_skipped_summary" not in delta


def test_cathay_filters_skipped_count_tracked_in_delta(store):
    """delta["card_billed_skipped_summary"] 統計 skip 次數 (>= 1 才出現)。"""
    delta = _persist(store, {
        "TWD": [
            {"card_no": "", "date": None, "post_date": None, "desc": "上期帳單總額", "amount": 100, "currency": "TWD", "consume_country": "", "consume_currency": "", "consume_amount": 0},
            {"card_no": "", "date": None, "post_date": None, "desc": "上期帳單總額", "amount": 200, "currency": "TWD", "consume_country": "", "consume_currency": "", "consume_amount": 0},
            {"card_no": "", "date": None, "post_date": None, "desc": "本期消費總額", "amount": 300, "currency": "TWD", "consume_country": "", "consume_currency": "", "consume_amount": 0},
        ],
    })
    assert delta["card_billed_new"] == 0
    assert delta["card_billed_skipped_summary"] == 3


def test_cathay_empty_billed_detail_no_skip_tracking(store):
    """空 billed_detail → 沒 skip 也沒 new；skipped_summary 不該出現。"""
    delta = _persist(store, {})
    assert delta["card_billed_new"] == 0
    assert "card_billed_skipped_summary" not in delta


def test_cathay_real_txn_preserved_when_summary_in_same_batch(store):
    """summary + real 混雜時，real 全部正確入庫。"""
    delta = _persist(store, {
        "TWD": [
            {"card_no": "", "date": None, "post_date": None, "desc": "上期帳單總額", "amount": 1000, "currency": "TWD", "consume_country": "", "consume_currency": "", "consume_amount": 0},
            {"card_no": "****7050", "date": "2026-06-01T00:00:00", "post_date": "2026-06-03T00:00:00", "desc": "7-11", "amount": 50, "currency": "TWD", "consume_country": "", "consume_currency": "", "consume_amount": 0},
            {"card_no": "****7050", "date": "2026-06-02T00:00:00", "post_date": "2026-06-04T00:00:00", "desc": "全家", "amount": 75, "currency": "TWD", "consume_country": "", "consume_currency": "", "consume_amount": 0},
        ],
        "USD": [
            {"card_no": "****7050", "date": "2026-05-15T00:00:00", "post_date": "2026-05-18T00:00:00", "desc": "Amazon", "amount": 1500, "currency": "TWD", "consume_country": "US", "consume_currency": "USD", "consume_amount": 50},
        ],
    })
    assert delta["card_billed_new"] == 3
    assert delta["card_billed_skipped_summary"] == 1
    rows = store.conn.execute(
        "SELECT description, currency, consume_currency FROM card_billed_txns ORDER BY consume_date"
    ).fetchall()
    assert [r["description"] for r in rows] == ["Amazon", "7-11", "全家"]
