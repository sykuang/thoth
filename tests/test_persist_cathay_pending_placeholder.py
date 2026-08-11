"""驗證 persist_cathay + collector _parse_consume filter NULL placeholder row。

2026-06-22 Bug 5: Cathay 即時消費 (current) / 未出帳 (unbilled) API 會在 list
開頭塞一個空殼 row（amount=None + desc=None + card_no='9062****7033'），
collector `_parse_consume` 看到第一個 dict 含 `amount` key 就全收 →
寫進 card_pending_txns → 前端 amount or 0 顯示 0 元假交易。

雙重 guard:
1. collector `_parse_consume._is_placeholder_consume_row` 物理 invariant filter (治本)
2. persist `persist_cathay` 對稱 filter + telemetry counter (防禦 + 觀察)

詳見 wiki [[card-billed-pending-cross-table-consistency-lesson]] Bug 5。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.banks.cathay import CathayCrawler
from backend.core.persist import persist_cathay
from backend.core.store import BankStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch):
    from backend.core import store as store_mod
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path, raising=True)
    s = BankStore("cathay_pending_placeholder_test")
    yield s
    s.close()


# ===== collector layer (raw 守門員) =====


def test_collector_filters_placeholder_in_inline_txn_list():
    """`_parse_consume` 處理「元素本身就是交易」分支：amount + desc 都空的不收。"""
    crawler = CathayCrawler.__new__(CathayCrawler)  # 不跑 __init__ (沒 creds)
    content = {
        "twd": [
            # placeholder — 該 filter
            {"amount": None, "desc": None, "card_no": "9062****7033"},
            # real txn
            {"amount": -100, "desc": "7-11", "card_no": "9062****7033",
             "date": "2026-06-22"},
        ],
    }
    result = crawler._parse_consume(content)
    assert "twd" in result
    assert len(result["twd"]) == 1, f"expected 1 real txn, got {result['twd']}"
    # _norm_card_txn 會 rename desc 為其他欄位，這裡只驗 amount 對得到 real
    assert result["twd"][0].get("amount") == -100


def test_collector_filters_placeholder_in_tradedata_branch():
    """`_parse_consume` 處理 `tradeData[]` 巢狀分支：同樣 filter。"""
    crawler = CathayCrawler.__new__(CathayCrawler)
    content = {
        "twd": [
            {"tradeData": [
                # placeholder
                {"amount": None, "transDesc": "  ", "card_no": ""},
                # real
                {"amount": -250, "transDesc": "全聯", "card_no": "9062****7033",
                 "consumeDate": "2026-06-22"},
            ]},
        ],
    }
    result = crawler._parse_consume(content)
    assert "twd" in result
    assert len(result["twd"]) == 1


def test_collector_keeps_zero_amount_real_txn():
    """amount=0 是合法 (refund / 紅利折抵)，不該被當 placeholder filter。"""
    crawler = CathayCrawler.__new__(CathayCrawler)
    content = {
        "twd": [
            # 0 元但有 desc → real (例如 refund 沖銷)
            {"amount": 0, "desc": "紅利折抵", "card_no": "9062****7033"},
        ],
    }
    result = crawler._parse_consume(content)
    assert len(result["twd"]) == 1


def test_collector_keeps_no_amount_with_desc():
    """amount 缺但有 desc → 不是 placeholder (保守 keep)。"""
    crawler = CathayCrawler.__new__(CathayCrawler)
    content = {
        "twd": [
            {"amount": None, "desc": "某筆消費", "card_no": "9062****7033"},
        ],
    }
    result = crawler._parse_consume(content)
    assert len(result["twd"]) == 1


# ===== persist layer (對稱補 telemetry) =====


def _persist(store, **cc_extra) -> dict:
    """run persist_cathay with credit_card given, return delta."""
    data = {
        "accounts": [],
        "balance_history": [],
        "credit_card": {
            "cards": [],
            **cc_extra,
        },
    }
    return persist_cathay(data, store, rules=[])


def test_persist_filters_pending_current_placeholder(store):
    """current_detail 含 placeholder row → persist 跳過 + 記 telemetry。"""
    delta = _persist(store, current_detail={"TWD": [
        # placeholder (collector 若漏網的補殺)
        {"amount": None, "desc": None, "card_no": "9062****7033"},
        # real
        {"amount": -100, "desc": "7-11", "card_no": "9062****7033",
         "date": "2026-06-22"},
    ]})
    assert delta["card_current"] == 1
    assert delta.get("card_current_skipped_placeholder") == 1

    rows = store.conn.execute(
        "SELECT description, amount FROM card_pending_txns WHERE scope='current'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["description"] == "7-11"


def test_persist_filters_pending_unbilled_placeholder(store):
    """unbilled_detail 同樣 filter。"""
    delta = _persist(store, unbilled_detail={"TWD": [
        {"amount": None, "desc": "   ", "card_no": ""},  # whitespace 也算空
        {"amount": -50, "desc": "全家", "card_no": "9062****7033",
         "date": "2026-06-22"},
    ]})
    assert delta["card_unbilled"] == 1
    assert delta.get("card_unbilled_skipped_placeholder") == 1


def test_persist_no_skipped_counter_when_all_real(store):
    """全是真實交易時不該寫 skipped_placeholder counter (避免 telemetry 噪音)。"""
    delta = _persist(store, current_detail={"TWD": [
        {"amount": -100, "desc": "7-11", "card_no": "9062****7033",
         "date": "2026-06-22"},
    ]})
    assert delta["card_current"] == 1
    assert "card_current_skipped_placeholder" not in delta


def test_persist_zero_amount_with_desc_is_real(store):
    """amount=0 + 有 desc → 入庫 (不當 placeholder)。"""
    delta = _persist(store, current_detail={"TWD": [
        {"amount": 0, "desc": "紅利折抵", "card_no": "9062****7033",
         "date": "2026-06-22"},
    ]})
    assert delta["card_current"] == 1
    assert "card_current_skipped_placeholder" not in delta


@pytest.mark.parametrize(("detail_key", "scope"), [
    ("unbilled_detail", "unbilled"),
    ("current_detail", "current"),
])
@pytest.mark.parametrize(("field", "value"), [
    ("amount", True),
    ("amount", "Infinity"),
    ("amount", 10 ** 400),
    ("consume_amount", True),
    ("consume_amount", float("nan")),
    ("consume_amount", 10 ** 400),
])
def test_persist_invalid_pending_money_preserves_saved_scope(
    store, detail_key, scope, field, value,
):
    store.refresh_card_pending(scope, [{
        "card_no": "9062****7033",
        "date": "2026-06-20",
        "desc": "OLD SAVED ROW",
        "amount": -50,
    }], fetch_ok=True)
    malformed = {
        "card_no": "9062****7033",
        "date": "2026-06-22",
        "desc": "MALFORMED ROW",
        "amount": -100,
    }
    malformed[field] = value

    delta = _persist(store, **{detail_key: {"TWD": [malformed]}})

    rows = store.conn.execute(
        "SELECT description, amount FROM card_pending_txns WHERE scope = ?",
        (scope,),
    ).fetchall()
    assert [(row["description"], row["amount"]) for row in rows] == [
        ("OLD SAVED ROW", -50),
    ]
    assert delta[f"card_{scope}_skipped_invalid_amount"] == 1
