"""驗證 persist_scb 把 per-card 信用卡 4 欄 (limit/used/stmt/due) 寫進 cards 表。

SCB 特性: 兩張卡共用 credit_limit (shared limit 模型)。
驗 6 個案例:
  1. card_text 全有 → 寫入完整 4 欄
  2. card_text 信用額度缺 → limit=None, used=None (不能算)
  3. card_text 可用額度缺 → limit 有, used=None
  4. card_text 只有 1 張卡的 due → 另一張 due=None
  5. _scb_due_to_stmt helper: due 推算 stmt
  6. card_text 完全沒給 → all None 但 cards 仍 upsert (來自 API)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.persist import persist_scb
from backend.core.persist.scb import _scb_due_to_stmt
from backend.core.store import BankStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch):
    from backend.core import store as store_mod
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path, raising=True)
    s = BankStore("scb_per_card_test")
    yield s
    s.close()


def _make_scb_data(card_text: str = "", cards: list[dict] | None = None) -> dict:
    """製造一份最小 SCB collected.json 結構。"""
    return {
        "card_text": card_text,
        "api_responses": {
            "crditAcctList": [{
                "url": "https://.../crditAcctList",
                "resp": {
                    "body": {
                        "sharedCard": {
                            "creditLimitAmt": "encrypted_hex",
                            "availiableLimitAmt": "encrypted_hex",
                            "sharedCards": cards or [],
                        }
                    }
                }
            }]
        }
    }


def test_scb_per_card_full_payload(store):
    """case 1: card_text 全有 → 寫入完整 4 欄, used = limit - available。"""
    # 用 fake/dummy 資料 (鐵律: 不用使用者真資料進 test)
    card_text = """共用額度綜覽

信用額度

TWD 100,000.00

可用額度

TWD 80,000.00

預借現金額度

TWD 10,000.00

可用預借現金餘額

TWD 10,000.00
 帳單查詢

測試聯名卡 主卡

9049-XXXX-XXXX-7050

最近一期繳款截止日

2026/07/15

自動扣款帳號

99999

測試回饋卡 主卡

9066-XXXX-XXXX-7041

最近一期繳款截止日

2026/08/10

自動扣款帳號

88888
"""
    cards = [
        {"cardNoForDisplay": "9049-XXXX-XXXX-7050", "cardTypeName": "測試聯名卡",
         "primarycard": True, "open": True},
        {"cardNoForDisplay": "9066-XXXX-XXXX-7041", "cardTypeName": "測試回饋卡",
         "primarycard": True, "open": True},
    ]
    data = _make_scb_data(card_text=card_text, cards=cards)
    delta = persist_scb(data, store)
    assert delta["cards_n"] == 2

    rows = store.conn.execute("""
        SELECT card_no, name, credit_limit, used_credit, statement_close_date, payment_due_date, active
        FROM cards ORDER BY card_no
    """).fetchall()
    assert len(rows) == 2
    # ****7041 (回饋卡)
    assert rows[0]["card_no"] == "****7041"
    assert rows[0]["credit_limit"] == 100000.0
    assert rows[0]["used_credit"] == 20000.0  # 100000 - 80000
    assert rows[0]["payment_due_date"] == "2026-08-10"
    assert rows[0]["statement_close_date"] == "2026-07-16"  # due - 25 天
    assert rows[0]["active"] == 1
    # ****7050 (聯名卡)
    assert rows[1]["card_no"] == "****7050"
    assert rows[1]["credit_limit"] == 100000.0
    assert rows[1]["used_credit"] == 20000.0
    assert rows[1]["payment_due_date"] == "2026-07-15"


def test_scb_per_card_no_limit_in_text(store):
    """case 2: card_text 沒「信用額度」→ limit=None, used=None。"""
    card_text = """測試卡 主卡
9049-XXXX-XXXX-7050
最近一期繳款截止日
2026/07/15
"""
    cards = [{"cardNoForDisplay": "9049-XXXX-XXXX-7050", "cardTypeName": "測試卡",
              "primarycard": True, "open": True}]
    data = _make_scb_data(card_text=card_text, cards=cards)
    persist_scb(data, store)
    row = store.conn.execute("SELECT credit_limit, used_credit, payment_due_date FROM cards").fetchone()
    assert row["credit_limit"] is None
    assert row["used_credit"] is None
    assert row["payment_due_date"] == "2026-07-15"


def test_scb_per_card_no_available_in_text(store):
    """case 3: 只有 limit 沒 available → limit 有, used=None。"""
    card_text = """信用額度

TWD 50,000.00

測試卡 主卡

9049-XXXX-XXXX-7050

最近一期繳款截止日

2026/07/15
"""
    cards = [{"cardNoForDisplay": "9049-XXXX-XXXX-7050", "cardTypeName": "測試卡",
              "primarycard": True, "open": True}]
    data = _make_scb_data(card_text=card_text, cards=cards)
    persist_scb(data, store)
    row = store.conn.execute("SELECT credit_limit, used_credit FROM cards").fetchone()
    assert row["credit_limit"] == 50000.0
    assert row["used_credit"] is None


def test_scb_per_card_partial_due(store):
    """case 4: 只有 1 張卡的 due, 另一張 due=None。"""
    card_text = """信用額度

TWD 100,000.00

可用額度

TWD 100,000.00

測試卡A 主卡

9048-XXXX-XXXX-7026

最近一期繳款截止日

2026/07/15

測試卡B 主卡

9050-XXXX-XXXX-7042

(沒給 due_date)
"""
    cards = [
        {"cardNoForDisplay": "9048-XXXX-XXXX-7026", "cardTypeName": "測試卡A",
         "primarycard": True, "open": True},
        {"cardNoForDisplay": "9050-XXXX-XXXX-7042", "cardTypeName": "測試卡B",
         "primarycard": True, "open": True},
    ]
    data = _make_scb_data(card_text=card_text, cards=cards)
    persist_scb(data, store)
    rows = {r["card_no"]: r for r in store.conn.execute(
        "SELECT card_no, payment_due_date FROM cards").fetchall()}
    assert rows["****7026"]["payment_due_date"] == "2026-07-15"
    assert rows["****7042"]["payment_due_date"] is None


def test_scb_due_to_stmt_helper():
    """case 5: _scb_due_to_stmt → due - 25 天。"""
    assert _scb_due_to_stmt("2026-07-26") == "2026-07-01"
    assert _scb_due_to_stmt("2026-12-26") == "2026-12-01"
    assert _scb_due_to_stmt("2025-09-26") == "2025-09-01"
    assert _scb_due_to_stmt(None) is None
    assert _scb_due_to_stmt("") is None
    assert _scb_due_to_stmt("bad-date") is None


def test_scb_per_card_no_card_text(store):
    """case 6: card_text 完全沒給 → cards 仍 upsert 來自 API, 但 4 欄 None。"""
    cards = [{"cardNoForDisplay": "9049-XXXX-XXXX-7050", "cardTypeName": "測試卡",
              "primarycard": True, "open": False}]  # open=False → active=0
    data = _make_scb_data(card_text="", cards=cards)
    persist_scb(data, store)
    row = store.conn.execute(
        "SELECT card_no, credit_limit, used_credit, payment_due_date, statement_close_date, active FROM cards"
    ).fetchone()
    assert row["card_no"] == "****7050"
    assert row["credit_limit"] is None
    assert row["used_credit"] is None
    assert row["payment_due_date"] is None
    assert row["statement_close_date"] is None
    assert row["active"] == 0  # open=False
