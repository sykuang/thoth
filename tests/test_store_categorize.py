"""Phase 5.1 — store.upsert_* must write category column when rules present.

Phase 5.1 — store.upsert_* 接 rules 時應寫 category 欄。

直接打 BankStore，不經過 sync_runner / persist。
"""
from __future__ import annotations

import pytest


@pytest.fixture
def store(tmp_path, monkeypatch):
    # 將 DATA_ROOT 指到 tmp_path/data 隔離（不污染 backend/data/）
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    import backend.core.store as store_mod
    import importlib
    importlib.reload(store_mod)
    # store 模組用 DATA_ROOT 常數（hard-coded），這個 fixture 改 module-level
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path / "banks")
    bs = store_mod.BankStore("testbank")
    yield bs
    bs.close()


def test_upsert_twd_with_rules_writes_category(store):
    rules = [
        {"pattern": r"北捷|台鐵", "category": "交通"},
        {"pattern": r"星巴克", "category": "餐飲"},
    ]
    txns = [
        {"account_no": "001", "datetime": "2026-06-12 09:00", "desc": "北捷加值",
         "expend": 500, "income": None, "balance": 10000},
        {"account_no": "001", "datetime": "2026-06-12 10:00", "desc": "星巴克拿鐵",
         "expend": 120, "income": None, "balance": 9880},
        {"account_no": "001", "datetime": "2026-06-12 11:00", "desc": "未知商店",
         "expend": 50, "income": None, "balance": 9830},
    ]
    n = store.upsert_twd_txns(txns, rules=rules)
    assert n == 3
    rows = store.conn.execute(
        "SELECT description, category FROM twd_transactions ORDER BY id"
    ).fetchall()
    cats = {r["description"]: r["category"] for r in rows}
    assert cats["北捷加值"] == "交通"
    assert cats["星巴克拿鐵"] == "餐飲"
    assert cats["未知商店"] is None


def test_upsert_twd_without_rules_leaves_category_null(store):
    """rules=None / [] 時不該動 category 欄（保持 NULL）。"""
    txns = [
        {"account_no": "001", "datetime": "2026-06-12 09:00", "desc": "any",
         "expend": 1, "income": None, "balance": 999},
    ]
    store.upsert_twd_txns(txns)
    row = store.conn.execute("SELECT category FROM twd_transactions").fetchone()
    assert row["category"] is None


def test_upsert_card_billed_with_rules_writes_category(store):
    rules = [{"pattern": r"Netflix", "category": "訂閱"}]
    txns = [{
        "card_no": "1234", "bill_date": "2026-06-01", "currency": "TWD",
        "date": "2026-05-15", "post_date": "2026-05-16", "desc": "Netflix 月費",
        "amount": 390, "consume_currency": "TWD", "consume_amount": None,
    }]
    store.upsert_card_billed(txns, rules=rules)
    row = store.conn.execute("SELECT category FROM card_billed_txns").fetchone()
    assert row["category"] == "訂閱"


def test_refresh_card_pending_with_rules_writes_category(store):
    rules = [{"pattern": r"麥當勞", "category": "餐飲"}]
    txns = [
        {"card_no": "1234", "date": "2026-06-12", "desc": "麥當勞早餐", "amount": 89,
         "currency": "TWD"},
        {"card_no": "1234", "date": "2026-06-12", "desc": "停車費", "amount": 30,
         "currency": "TWD"},
    ]
    store.refresh_card_pending("unbilled", txns, rules=rules)
    rows = store.conn.execute(
        "SELECT description, category FROM card_pending_txns ORDER BY id"
    ).fetchall()
    cats = {r["description"]: r["category"] for r in rows}
    assert cats["麥當勞早餐"] == "餐飲"
    assert cats["停車費"] is None


# ===========================================================================
# Phase 8.1 (2026-06-15): rules 帶 subcategory → store 同時寫 subcategory 欄
# ===========================================================================

def test_upsert_twd_with_subcategory_writes_both(store):
    rules = [
        {"pattern": r"麥當勞|肯德基", "category": "飲食", "subcategory": "速食"},
        {"pattern": r"星巴克", "category": "飲食"},  # 無 sub
    ]
    txns = [
        {"account_no": "001", "datetime": "2026-06-12 09:00", "desc": "麥當勞早安",
         "expend": 100, "income": None, "balance": 1000},
        {"account_no": "001", "datetime": "2026-06-12 10:00", "desc": "星巴克拿鐵",
         "expend": 120, "income": None, "balance": 880},
    ]
    store.upsert_twd_txns(txns, rules=rules)
    rows = store.conn.execute(
        "SELECT description, category, subcategory FROM twd_transactions ORDER BY id"
    ).fetchall()
    d = {r["description"]: (r["category"], r["subcategory"]) for r in rows}
    assert d["麥當勞早安"] == ("飲食", "速食")
    assert d["星巴克拿鐵"] == ("飲食", None)


def test_upsert_card_billed_with_subcategory(store):
    rules = [{"pattern": r"Netflix", "category": "娛樂", "subcategory": "訂閱"}]
    txns = [{
        "card_no": "1234", "bill_date": "2026-06-01", "currency": "TWD",
        "date": "2026-05-15", "post_date": "2026-05-16", "desc": "Netflix 月費",
        "amount": 390, "consume_currency": "TWD", "consume_amount": None,
    }]
    store.upsert_card_billed(txns, rules=rules)
    row = store.conn.execute(
        "SELECT category, subcategory FROM card_billed_txns"
    ).fetchone()
    assert row["category"] == "娛樂"
    assert row["subcategory"] == "訂閱"


def test_refresh_card_pending_with_subcategory(store):
    rules = [{"pattern": r"麥當勞", "category": "飲食", "subcategory": "速食"}]
    txns = [{"card_no": "1234", "date": "2026-06-12", "desc": "麥當勞早餐",
             "amount": 89, "currency": "TWD"}]
    store.refresh_card_pending("unbilled", txns, rules=rules)
    row = store.conn.execute(
        "SELECT category, subcategory FROM card_pending_txns"
    ).fetchone()
    assert row["category"] == "飲食"
    assert row["subcategory"] == "速食"
