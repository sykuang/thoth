"""Phase 10 (2026-07-29) — 分類拆帳 (splits_overwrite) 端到端測試。

一筆交易拆成多個分類, 每份可獨立設定是否納入收支統計。

涵蓋:
- PATCH 拆帳 → 列表展開成 N 筆子項 (母筆不再出現)
- 子項總和 != 母筆金額 → 400 (分類拆帳的定義就是不改總額)
- 子項各自的 auto_excluded → stats 只算未排除的那幾份
- category filter 看得到子項分類 (展開必須發生在 filter 之前)
- 送 [] 取消拆帳 → 回歸母筆統計
- raw row 的 amount / category 永不變動 (「修正≠刪除」鐵律)
- 拆帳後重跑 pending refresh 不會被洗掉 (overlay 保存)
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.test_transactions_routes import _auth, _register, _seed_bank_db


@pytest.fixture
def data_root(tmp_path):
    import os
    return Path(os.environ["BANK_DATA_ROOT"])


def _seed_one_billed(client, data_root, token, amount: int = 1200):
    """建一筆信用卡已出帳交易, 回 (raw_id,).

    用 billed 而非 pending — billed 是 append-only, 比較貼近真實拆帳情境
    (未出帳的金額還會變, 使用者通常等出帳才拆)。
    """
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))
    _seed_bank_db(
        data_root, "hsbc",
        billed=[{
            "card_no": "1234", "date": "2026-07-10", "desc": "全聯福利中心",
            "amount": amount, "category": "日用品",
        }],
    )
    r = client.get("/transactions?bank=hsbc&kind=billed", headers=_auth(token))
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    # 信用卡消費 amount 正值 → transform 反號成支出
    assert items[0]["cashflow_direction"] == "expense"
    assert items[0]["cashflow_amount"] == amount
    assert items[0]["splits"] == []
    return items[0]["raw"]["id"]


# ============================================================
# 基本拆帳 + 列表展開
# ============================================================

def test_split_expands_into_children_and_hides_parent(client, data_root):
    token = _register(client, email="split-basic@p.com")
    raw_id = _seed_one_billed(client, data_root, token, amount=1200)

    r = client.patch(
        f"/transactions/hsbc/billed/{raw_id}",
        json={"splits": [
            {"amount": 800, "category": "餐飲"},
            {"amount": 400, "category": "日用品"},
        ]},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text

    r = client.get("/transactions?bank=hsbc&kind=billed", headers=_auth(token))
    items = r.json()["items"]
    # 母筆被兩個子項取代
    assert len(items) == 2
    assert {i["category"] for i in items} == {"餐飲", "日用品"}
    assert sorted(i["cashflow_amount"] for i in items) == [400, 800]
    # 子項都指回母筆, 且 id 帶序號後綴
    assert all(i["split_of"] == raw_id for i in items)
    assert {i["id"] for i in items} == {f"{raw_id}#0", f"{raw_id}#1"}
    # 方向沿用母筆 (都是支出)
    assert all(i["cashflow_direction"] == "expense" for i in items)
    # 子項不再帶 splits, 避免遞迴
    assert all(i["splits"] == [] for i in items)


def test_split_leaves_raw_row_untouched(client, data_root):
    """「修正≠刪除」: raw amount / category 永不變動。"""
    token = _register(client, email="split-raw@p.com")
    raw_id = _seed_one_billed(client, data_root, token, amount=1200)

    client.patch(
        f"/transactions/hsbc/billed/{raw_id}",
        json={"splits": [
            {"amount": 800, "category": "餐飲"},
            {"amount": 400, "category": "日用品"},
        ]},
        headers=_auth(token),
    )

    con = sqlite3.connect(str(data_root / "hsbc.sqlite"))
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT amount, category, splits_overwrite FROM card_billed_txns WHERE id = ?",
        (raw_id,),
    ).fetchone()
    con.close()
    assert row["amount"] == 1200          # raw 金額不動
    assert row["category"] == "日用品"     # raw 分類不動
    assert row["splits_overwrite"] is not None


# ============================================================
# 金額總和驗證
# ============================================================

def test_split_sum_mismatch_rejected(client, data_root):
    token = _register(client, email="split-sum@p.com")
    raw_id = _seed_one_billed(client, data_root, token, amount=1200)

    r = client.patch(
        f"/transactions/hsbc/billed/{raw_id}",
        json={"splits": [
            {"amount": 800, "category": "餐飲"},
            {"amount": 300, "category": "日用品"},  # 合計 1100 != 1200
        ]},
        headers=_auth(token),
    )
    assert r.status_code == 400
    assert "1100" in r.text and "1200" in r.text


def test_split_requires_at_least_two_parts(client, data_root):
    token = _register(client, email="split-one@p.com")
    raw_id = _seed_one_billed(client, data_root, token, amount=1200)
    r = client.patch(
        f"/transactions/hsbc/billed/{raw_id}",
        json={"splits": [{"amount": 1200, "category": "餐飲"}]},
        headers=_auth(token),
    )
    assert r.status_code == 400


def test_split_rejects_non_positive_amount(client, data_root):
    token = _register(client, email="split-neg@p.com")
    raw_id = _seed_one_billed(client, data_root, token, amount=1200)
    r = client.patch(
        f"/transactions/hsbc/billed/{raw_id}",
        json={"splits": [
            {"amount": 1400, "category": "餐飲"},
            {"amount": -200, "category": "日用品"},
        ]},
        headers=_auth(token),
    )
    assert r.status_code == 400


def test_split_rejects_unknown_subfield(client, data_root):
    token = _register(client, email="split-badfield@p.com")
    raw_id = _seed_one_billed(client, data_root, token, amount=1200)
    r = client.patch(
        f"/transactions/hsbc/billed/{raw_id}",
        json={"splits": [
            {"amount": 800, "category": "餐飲", "evil": 1},
            {"amount": 400, "category": "日用品"},
        ]},
        headers=_auth(token),
    )
    assert r.status_code == 400


# ============================================================
# 每份獨立納入/排除統計 — 皇上明確要求的核心行為
# ============================================================

def test_split_per_part_auto_excluded_affects_stats(client, data_root):
    token = _register(client, email="split-stats@p.com")
    raw_id = _seed_one_billed(client, data_root, token, amount=1200)

    # 拆成 800 餐飲(算) + 400 代墊(不算)
    r = client.patch(
        f"/transactions/hsbc/billed/{raw_id}",
        json={"splits": [
            {"amount": 800, "category": "餐飲"},
            {"amount": 400, "category": "日用品",
             "auto_excluded": True, "note": "同事代墊"},
        ]},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text

    r = client.get("/transactions/stats?bank=hsbc&kind=billed", headers=_auth(token))
    assert r.status_code == 200, r.text
    stats = r.json()
    # 只有未排除的 800 進支出桶
    assert stats["total_expense"] == 800
    assert stats["amount_by_category"].get("餐飲") == 800
    assert "日用品" not in stats["amount_by_category"]

    # 列表兩筆都看得到, 但排除那筆有旗標 (反灰顯示用)
    r = client.get("/transactions?bank=hsbc&kind=billed", headers=_auth(token))
    items = {i["category"]: i for i in r.json()["items"]}
    assert items["餐飲"]["auto_excluded"] is False
    assert items["日用品"]["auto_excluded"] is True
    assert items["日用品"]["split_note"] == "同事代墊"


def test_split_parent_auto_excluded_wins_over_children(client, data_root):
    """母筆整筆已排除 → 所有子項一律排除 (OR 邏輯, 母筆優先)。"""
    token = _register(client, email="split-parentex@p.com")
    raw_id = _seed_one_billed(client, data_root, token, amount=1200)

    client.patch(
        f"/transactions/hsbc/billed/{raw_id}",
        json={"splits": [
            {"amount": 800, "category": "餐飲"},
            {"amount": 400, "category": "日用品"},
        ]},
        headers=_auth(token),
    )
    client.patch(
        f"/transactions/hsbc/billed/{raw_id}",
        json={"auto_excluded": True},
        headers=_auth(token),
    )

    r = client.get("/transactions/stats?bank=hsbc&kind=billed", headers=_auth(token))
    assert r.json()["total_expense"] == 0


# ============================================================
# filter 互動 — 展開必須發生在 filter 之前
# ============================================================

def test_split_children_visible_to_category_filter(client, data_root):
    """母筆分類是「日用品」, 但有「餐飲」子項 — 篩餐飲必須找得到。"""
    token = _register(client, email="split-filter@p.com")
    raw_id = _seed_one_billed(client, data_root, token, amount=1200)

    client.patch(
        f"/transactions/hsbc/billed/{raw_id}",
        json={"splits": [
            {"amount": 800, "category": "餐飲"},
            {"amount": 400, "category": "日用品"},
        ]},
        headers=_auth(token),
    )

    r = client.get(
        "/transactions?bank=hsbc&kind=billed&category=餐飲", headers=_auth(token),
    )
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["cashflow_amount"] == 800


# ============================================================
# 取消拆帳
# ============================================================

def test_split_cleared_by_empty_list(client, data_root):
    token = _register(client, email="split-clear@p.com")
    raw_id = _seed_one_billed(client, data_root, token, amount=1200)

    client.patch(
        f"/transactions/hsbc/billed/{raw_id}",
        json={"splits": [
            {"amount": 800, "category": "餐飲"},
            {"amount": 400, "category": "日用品"},
        ]},
        headers=_auth(token),
    )
    assert len(client.get(
        "/transactions?bank=hsbc&kind=billed", headers=_auth(token),
    ).json()["items"]) == 2

    r = client.patch(
        f"/transactions/hsbc/billed/{raw_id}",
        json={"splits": []}, headers=_auth(token),
    )
    assert r.status_code == 200, r.text

    r = client.get("/transactions?bank=hsbc&kind=billed", headers=_auth(token))
    items = r.json()["items"]
    assert len(items) == 1                       # 回歸母筆
    assert items[0]["id"] == raw_id
    assert items[0]["cashflow_amount"] == 1200
    assert items[0]["category"] == "日用品"

    r = client.get("/transactions/stats?bank=hsbc&kind=billed", headers=_auth(token))
    assert r.json()["total_expense"] == 1200


# ============================================================
# sync 保存 (overlay pattern 的核心價值)
# ============================================================

def test_split_survives_pending_refresh(client, data_root):
    """重跑 pending refresh 時, 使用者的拆帳不可被洗掉。"""
    from backend.core.store import BankStore

    token = _register(client, email="split-sync@p.com")
    client.post("/accounts", json={"bank": "hsbc", "label": "t"}, headers=_auth(token))

    pending = [{
        "card_no": "1234", "date": "2026-07-10", "desc": "全聯福利中心",
        "amount": 1200, "currency": "TWD", "scope": "unbilled",
    }]
    store = BankStore("hsbc", user_id=1)
    store.refresh_card_pending("unbilled", pending)
    store.close()

    r = client.get("/transactions?bank=hsbc&kind=pending", headers=_auth(token))
    raw_id = r.json()["items"][0]["raw"]["id"]

    client.patch(
        f"/transactions/hsbc/pending/{raw_id}",
        json={"splits": [
            {"amount": 800, "category": "餐飲"},
            {"amount": 400, "category": "日用品", "auto_excluded": True},
        ]},
        headers=_auth(token),
    )

    # 同一批資料再 refresh 一次 (模擬下次 sync)
    store = BankStore("hsbc", user_id=1)
    store.refresh_card_pending("unbilled", pending)
    store.close()

    r = client.get("/transactions?bank=hsbc&kind=pending", headers=_auth(token))
    items = r.json()["items"]
    assert len(items) == 2, "拆帳在 refresh 後被洗掉了"
    by_cat = {i["category"]: i for i in items}
    assert by_cat["餐飲"]["cashflow_amount"] == 800
    assert by_cat["日用品"]["auto_excluded"] is True
