"""驗證玉山來源缺少入帳日時，canonical store 保留 NULL（顯示誠實）。

2026-06-20 修：玉山「信用卡消費明細查詢」列表頁的物理限制 —
列表頁只給 6 欄 (消費日期/商店/消費幣別+金額/繳款幣別+金額/卡號/狀態)，
**沒有獨立的「請款日/入帳日」欄位**。

`post_date` 缺失必須一路保留 NULL；不得在 persist 或 shared store 偽造成消費日。

未來想抓真實入帳日需另抓「月結帳單」(信用卡帳單 > 明細) 那邊的 posting date。
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


def test_esun_persist_passes_missing_post_date_as_null(store, monkeypatch):
    """來源沒入帳日的 row 要明確傳 NULL，不可複製消費日。"""
    captured_rows: list[list[dict]] = []

    real_upsert = store.upsert_card_billed

    def spy(txns, rules=None):
        captured_rows.append([dict(t) for t in txns])
        return real_upsert(txns, rules=rules)

    monkeypatch.setattr(store, "upsert_card_billed", spy)

    data = {
        "accounts": [],
        "card_transactions_ok": True,
        "card_transactions": [
            {
                "consume_date": "2026/06/08",
                "merchant": "中油",
                "consume_currency": "TWD",
                "consume_amount": 1727.0,
                "billed_currency": "TWD",
                "billed_amount": 1727.0,
                "card_no": "9064-XXXX-XXXX-7032",
                "card_last4": "7032",
                "status": "已入帳",
            },
        ],
    }
    persist_esun(data, store, rules=[])

    assert len(captured_rows) == 1
    rows = captured_rows[0]
    assert len(rows) == 1
    row = rows[0]
    # persist 明確傳 post_date=None；shared store 也保留 NULL
    assert row["post_date"] is None
    # consume_date (key 為 "date") 應正確帶入
    assert row["date"] == "2026-06-08"


def test_esun_store_preserves_missing_post_date(store):
    """來源沒提供入帳日，DB 必須保留 NULL，不得複製消費日。"""
    data = {
        "accounts": [],
        "card_transactions_ok": True,
        "card_transactions": [
            {
                "consume_date": "2026/06/08",
                "merchant": "中油",
                "consume_currency": "TWD",
                "consume_amount": 1727.0,
                "billed_currency": "TWD",
                "billed_amount": 1727.0,
                "card_no": "9064-XXXX-XXXX-7032",
                "card_last4": "7032",
                "status": "已入帳",
            },
        ],
    }
    persist_esun(data, store, rules=[])

    rows = store.conn.execute(
        "SELECT consume_date, post_date FROM card_billed_txns"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["consume_date"] == "2026-06-08"
    assert rows[0]["post_date"] is None


def test_esun_pending_preserves_missing_post_date(store):
    """未入帳來源也沒有入帳日，canonical row 必須保留 NULL。"""
    data = {
        "accounts": [],
        "card_transactions_ok": True,
        "card_transactions": [
            {
                "consume_date": "2026/06/15",
                "merchant": "連加可不可",
                "consume_currency": "TWD",
                "consume_amount": 40.0,
                "billed_currency": "TWD",
                "billed_amount": 40.0,
                "card_no": "9064-XXXX-XXXX-7032",
                "card_last4": "7032",
                "status": "未入帳",
            },
        ],
    }
    persist_esun(data, store, rules=[])

    # pending row 寫進 card_pending_txns
    billed = store.conn.execute("SELECT COUNT(*) FROM card_billed_txns").fetchone()[0]
    pending_row = store.conn.execute(
        "SELECT post_date FROM card_pending_txns"
    ).fetchone()
    assert billed == 0
    assert pending_row is not None
    assert pending_row["post_date"] is None
