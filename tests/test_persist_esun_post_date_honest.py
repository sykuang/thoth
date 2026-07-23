"""驗證 persist_esun 不再寫 post_date = consume_date fallback（顯示誠實）。

2026-06-20 修：玉山「信用卡消費明細查詢」列表頁的物理限制 —
列表頁只給 6 欄 (消費日期/商店/消費幣別+金額/繳款幣別+金額/卡號/狀態)，
**沒有獨立的「請款日/入帳日」欄位**。

原本 persist line 208 寫 `row["post_date"] = row["date"]` 等於假裝爬到了入帳日 —
不誠實。改成不寫 post_date 留 NULL，讓 store.upsert_card_billed 的內建
fallback (store.py:571 `post_date = t.get("post_date") or t.get("date")`)
處理 DB 寫入 — 行為相同但 persist 層不再假裝。

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


def test_esun_persist_does_not_set_post_date_in_row(store, monkeypatch):
    """capture row passed to store.upsert_card_billed — 不該含 post_date."""
    captured_rows: list[list[dict]] = []

    real_upsert = store.upsert_card_billed

    def spy(txns, rules=None):
        captured_rows.append([dict(t) for t in txns])
        return real_upsert(txns, rules=rules)

    monkeypatch.setattr(store, "upsert_card_billed", spy)

    data = {
        "accounts": [],
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
    # persist 層不再寫 post_date — 留給 store fallback
    assert "post_date" not in row
    # consume_date (key 為 "date") 應正確帶入
    assert row["date"] == "2026-06-08"


def test_esun_store_fallback_writes_post_date_equal_to_consume(store):
    """store 層 fallback：persist 沒給 post_date 時，DB 寫入 post_date = consume_date.

    這個行為保持向後相容（顯示層仍能對齊「入帳日 = 消費日」這條規範，
    只是改由 store 層而非 persist 層執行）。
    """
    data = {
        "accounts": [],
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
    # store fallback kicks in
    assert rows[0]["post_date"] == "2026-06-08"


def test_esun_pending_row_unaffected_by_post_date_change(store):
    """未入帳交易不該有 post_date 影響（pending 本來就無 post_date 欄位）。"""
    data = {
        "accounts": [],
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
    pending = store.conn.execute("SELECT COUNT(*) FROM card_pending_txns").fetchone()[0]
    assert billed == 0
    assert pending == 1
