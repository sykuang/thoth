"""驗證 upsert_card_billed 連動清 card_pending_txns 中對應 stale row。

背景：銀行 billed 出帳後，pending 通常 1-3 天才會從未出帳清單移除；
過渡期 UI 會雙重計算同一筆消費（見 esun ****7032 case）。

設計：寫 billed 時呼叫 _purge_overlapping_pending() 比對 4 欄全等才清：
  card_no + consume_date + amount + description

任一欄為 None 視為「資料不齊不敢清」直接 skip（保守策略）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core import store as store_mod
from backend.core.store import BankStore


@pytest.fixture
def tmp_store(tmp_path: Path, monkeypatch) -> BankStore:
    """獨立 tmp DB 的 BankStore，避免污染 backend/data/*.sqlite。"""
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path)
    return BankStore("testbank")


def _seed_pending(store: BankStore, scope: str, txns: list[dict]) -> None:
    """寫一批 pending row 進去（refresh_card_pending 會先 DELETE WHERE scope=?）。"""
    store.refresh_card_pending(scope, txns, rules=None)


# === 1. 正常 case：4 欄全等 → 清掉 pending ===
def test_billed_purges_matching_pending(tmp_store: BankStore) -> None:
    _seed_pending(tmp_store, "unbilled", [
        {"card_no": "****7032", "date": "2026-06-08", "desc": "中油",
         "amount": 1727, "currency": "TWD"},
    ])
    assert tmp_store.conn.execute("SELECT COUNT(*) FROM card_pending_txns").fetchone()[0] == 1

    tmp_store.upsert_card_billed([
        {"card_no": "****7032", "date": "2026-06-08", "desc": "中油",
         "amount": 1727, "currency": "TWD", "bill_date": "2026-06-29"},
    ])

    assert tmp_store.conn.execute("SELECT COUNT(*) FROM card_billed_txns").fetchone()[0] == 1
    assert tmp_store.conn.execute("SELECT COUNT(*) FROM card_pending_txns").fetchone()[0] == 0, \
        "billed 寫入後 pending 對應筆應清掉"


# === 2. 兩筆 pending 但只一筆出帳 → 只清出帳那筆 ===
def test_billed_purges_only_matching_pending(tmp_store: BankStore) -> None:
    _seed_pending(tmp_store, "unbilled", [
        {"card_no": "****7032", "date": "2026-06-08", "desc": "中油",
         "amount": 1727, "currency": "TWD"},
        {"card_no": "****7032", "date": "2026-06-08", "desc": "優步",
         "amount": 358, "currency": "TWD"},
    ])

    tmp_store.upsert_card_billed([
        {"card_no": "****7032", "date": "2026-06-08", "desc": "中油",
         "amount": 1727, "currency": "TWD", "bill_date": "2026-06-29"},
    ])

    rows = tmp_store.conn.execute(
        "SELECT description FROM card_pending_txns").fetchall()
    descs = sorted(r["description"] for r in rows)
    assert descs == ["優步"], f"應只剩優步未出帳，實際 {descs}"


# === 3. card_no 不同 → 不清 ===
def test_billed_does_not_purge_different_card(tmp_store: BankStore) -> None:
    _seed_pending(tmp_store, "unbilled", [
        {"card_no": "****7015", "date": "2026-06-08", "desc": "中油",
         "amount": 1727, "currency": "TWD"},
    ])

    tmp_store.upsert_card_billed([
        {"card_no": "****7032", "date": "2026-06-08", "desc": "中油",
         "amount": 1727, "currency": "TWD", "bill_date": "2026-06-29"},
    ])

    assert tmp_store.conn.execute("SELECT COUNT(*) FROM card_pending_txns").fetchone()[0] == 1, \
        "不同卡號的 pending 不該被清"


# === 4. amount 不同 → 不清 ===
def test_billed_does_not_purge_different_amount(tmp_store: BankStore) -> None:
    _seed_pending(tmp_store, "unbilled", [
        {"card_no": "****7032", "date": "2026-06-08", "desc": "中油",
         "amount": 1700, "currency": "TWD"},
    ])

    tmp_store.upsert_card_billed([
        {"card_no": "****7032", "date": "2026-06-08", "desc": "中油",
         "amount": 1727, "currency": "TWD", "bill_date": "2026-06-29"},
    ])

    assert tmp_store.conn.execute("SELECT COUNT(*) FROM card_pending_txns").fetchone()[0] == 1, \
        "不同金額的 pending 不該被清（可能是不同筆消費）"


# === 5. desc 不同 → 不清（同日同卡同金額不同商家是合理 case） ===
def test_billed_does_not_purge_different_desc(tmp_store: BankStore) -> None:
    _seed_pending(tmp_store, "unbilled", [
        {"card_no": "****7032", "date": "2026-06-08", "desc": "7-11 內湖店",
         "amount": 100, "currency": "TWD"},
    ])

    tmp_store.upsert_card_billed([
        {"card_no": "****7032", "date": "2026-06-08", "desc": "7-11 信義店",
         "amount": 100, "currency": "TWD", "bill_date": "2026-06-29"},
    ])

    assert tmp_store.conn.execute("SELECT COUNT(*) FROM card_pending_txns").fetchone()[0] == 1, \
        "不同描述視為不同筆消費，不該清（同日跨店 100 元案例）"


# === 6. card_no=None → 不敢清（保守策略，避免 wildcard 誤殺） ===
def test_billed_with_none_card_does_not_purge(tmp_store: BankStore) -> None:
    _seed_pending(tmp_store, "unbilled", [
        {"card_no": "****7032", "date": "2026-06-08", "desc": "中油",
         "amount": 1727, "currency": "TWD"},
    ])

    tmp_store.upsert_card_billed([
        {"card_no": None, "date": "2026-06-08", "desc": "中油",
         "amount": 1727, "currency": "TWD", "bill_date": "2026-06-29"},
    ])

    assert tmp_store.conn.execute("SELECT COUNT(*) FROM card_pending_txns").fetchone()[0] == 1, \
        "card_no=None 視為資料不齊不敢清（保守策略）"


# === 7. 跨 scope (unbilled + current) 都會被清 ===
def test_billed_purges_across_scopes(tmp_store: BankStore) -> None:
    _seed_pending(tmp_store, "unbilled", [
        {"card_no": "****7032", "date": "2026-06-08", "desc": "中油",
         "amount": 1727, "currency": "TWD"},
    ])
    _seed_pending(tmp_store, "current", [
        {"card_no": "****7032", "date": "2026-06-08", "desc": "中油",
         "amount": 1727, "currency": "TWD"},
    ])
    assert tmp_store.conn.execute("SELECT COUNT(*) FROM card_pending_txns").fetchone()[0] == 2

    tmp_store.upsert_card_billed([
        {"card_no": "****7032", "date": "2026-06-08", "desc": "中油",
         "amount": 1727, "currency": "TWD", "bill_date": "2026-06-29"},
    ])

    assert tmp_store.conn.execute("SELECT COUNT(*) FROM card_pending_txns").fetchone()[0] == 0, \
        "不論 scope='unbilled' 或 'current'，對應同筆 pending 都該清"


# === 8. _purge_overlapping_pending 直接呼叫 (sweep 歷史 stale row 用) ===
def test_purge_overlapping_pending_direct_call(tmp_store: BankStore) -> None:
    _seed_pending(tmp_store, "unbilled", [
        {"card_no": "****7032", "date": "2026-06-08", "desc": "中油",
         "amount": 1727, "currency": "TWD"},
    ])

    n = tmp_store._purge_overlapping_pending(
        card_no="****7032", consume_date="2026-06-08",
        amount=1727, desc="中油",
    )
    assert n == 1
    assert tmp_store.conn.execute("SELECT COUNT(*) FROM card_pending_txns").fetchone()[0] == 0


# === Phase 8.5 (2026-06-18) ===
# refresh_card_pending 自己 INSERT 完也要去重 ↔ billed 已就位的 row。
# 真因: CTBC sync 流程是 upsert_card_billed 先, refresh_card_pending 後;
#       refresh 內部 DELETE 全 scope + INSERT 全新 list, 把 billed 已存在
#       的同筆又灌回 pending 表 → /transactions UNION ALL 雙顯。
# 修法: refresh INSERT 完跑 prune SQL 把存在於 billed 的 row 砍掉。
# 使用者 prod CTBC 4 筆重複觸發 (SUKIYA/健身工廠/中華航空/中華電信).


def test_refresh_pending_dedups_against_existing_billed(tmp_store: BankStore) -> None:
    """refresh_card_pending 寫完後, 應自動去掉 billed 已存在的 row。

    真實 CTBC scenario:
      1. upsert_card_billed 寫入 SUKIYA 已結帳
      2. refresh_card_pending(unbilled, [SUKIYA + 健身房]) 一次寫兩筆
      3. INSERT 後立刻 prune: SUKIYA 被砍 (已在 billed), 健身房保留 (未在 billed)
    """
    # 1. billed 先存在
    tmp_store.upsert_card_billed([
        {"card_no": "****7036", "date": "2026-05-20", "desc": "ＳＵＫＩＹＡ　台北市政",
         "amount": -268, "currency": "TWD", "bill_date": "2026-06-05"},
    ])
    assert tmp_store.conn.execute("SELECT COUNT(*) FROM card_billed_txns").fetchone()[0] == 1

    # 2. refresh pending 一次寫兩筆: 一筆同 key billed, 一筆全新
    n = tmp_store.refresh_card_pending("unbilled", [
        {"card_no": "****7036", "date": "2026-05-20", "desc": "ＳＵＫＩＹＡ　台北市政",
         "amount": -268, "currency": "TWD"},   # ← 跟 billed 同 key, 應被 prune
        {"card_no": "****7036", "date": "2026-06-10", "desc": "健身房",
         "amount": -1288, "currency": "TWD"},  # ← 未在 billed, 保留
    ])

    # 回傳數應為 1 (傳 2 筆, prune 1, 留 1)
    assert n == 1, f"refresh 應回傳實際保留數 1, 實際 {n}"

    # DB 內只剩 1 筆 pending
    rows = tmp_store.conn.execute(
        "SELECT description FROM card_pending_txns ORDER BY description"
    ).fetchall()
    descs = [r["description"] for r in rows]
    assert descs == ["健身房"], f"應只剩健身房, 實際 {descs}"


def test_refresh_pending_does_not_prune_different_card(tmp_store: BankStore) -> None:
    """不同卡號的 billed 不該誤殺 pending。"""
    tmp_store.upsert_card_billed([
        {"card_no": "****7015", "date": "2026-05-20", "desc": "ＳＵＫＩＹＡ",
         "amount": -268, "currency": "TWD", "bill_date": "2026-06-05"},
    ])

    n = tmp_store.refresh_card_pending("unbilled", [
        {"card_no": "****7026", "date": "2026-05-20", "desc": "ＳＵＫＩＹＡ",
         "amount": -268, "currency": "TWD"},
    ])
    assert n == 1
    assert tmp_store.conn.execute("SELECT COUNT(*) FROM card_pending_txns").fetchone()[0] == 1


def test_refresh_pending_prune_respects_scope_isolation(tmp_store: BankStore) -> None:
    """refresh scope='unbilled' 不應去刪 scope='current' 的 row, 即使 billed match。

    Why: refresh API 是 per-scope, 一次 refresh 只負責本 scope, 其他 scope 不該動。
    Prune SQL 也應限 user_id + scope.
    """
    # 先在 current scope 塞一筆 + billed 同 key 也有
    tmp_store.upsert_card_billed([
        {"card_no": "****7036", "date": "2026-05-20", "desc": "ＳＵＫＩＹＡ",
         "amount": -268, "currency": "TWD", "bill_date": "2026-06-05"},
    ])
    _seed_pending(tmp_store, "current", [
        {"card_no": "****7036", "date": "2026-05-20", "desc": "ＳＵＫＩＹＡ",
         "amount": -268, "currency": "TWD"},
    ])
    # _seed_pending 本身會走 refresh → 已 prune. 確認 current scope 那筆被砍了 (對, 本 scope refresh 也砍)
    # 真正 race condition test 是: 第二次 refresh unbilled scope 不該誤砍 current 還在的 row
    # 但因 _seed_pending 已用 refresh 跑過, current scope 那筆已被 prune.
    # 改寫策略: 直接 INSERT 繞過 refresh, 模擬 stale current row
    tmp_store.conn.execute(
        """INSERT INTO card_pending_txns
           (user_id, scope, card_no, consume_date, description, amount, currency,
            consume_country, consume_currency, consume_amount, refreshed_at,
            category, subcategory, txn_type, auto_excluded)
           VALUES (?,?,?,?,?,?,?,NULL,NULL,NULL,'2026-06-18',NULL,NULL,NULL,0)""",
        (tmp_store.user_id, "current", "****7036", "2026-05-20",
         "ＳＵＫＩＹＡ", -268, "TWD"),
    )
    tmp_store.conn.commit()
    assert tmp_store.conn.execute(
        "SELECT COUNT(*) FROM card_pending_txns WHERE scope='current'"
    ).fetchone()[0] == 1

    # 跑 refresh unbilled scope — 不該砍 current 那筆
    tmp_store.refresh_card_pending("unbilled", [])

    cur_left = tmp_store.conn.execute(
        "SELECT COUNT(*) FROM card_pending_txns WHERE scope='current'"
    ).fetchone()[0]
    assert cur_left == 1, "refresh unbilled 不該動 current scope 的 row"


def test_refresh_pending_preserves_user_metadata(tmp_store: BankStore) -> None:
    txn = {"card_no": "****7032", "date": "2026-07-20", "desc": "晚餐",
           "amount": 680.0, "currency": "TWD"}
    _seed_pending(tmp_store, "unbilled", [txn])
    tmp_store.conn.execute(
        """UPDATE card_pending_txns
           SET category=?, subcategory=?, description_overwrite=?,
               tags_overwrite=?, auto_excluded=1""",
        ("飲食", "聚餐", "慶生晚餐", json.dumps(["家人", "生日"], ensure_ascii=False)),
    )
    tmp_store.conn.commit()

    tmp_store.refresh_card_pending(
        "unbilled", [txn],
        rules=[{"pattern": "晚餐", "category": "其他", "subcategory": "待確認"}],
    )

    row = tmp_store.conn.execute(
        """SELECT category, subcategory, description_overwrite,
                  tags_overwrite, auto_excluded
           FROM card_pending_txns""",
    ).fetchone()
    assert row is not None
    assert dict(row) == {
        "category": "飲食",
        "subcategory": "聚餐",
        "description_overwrite": "慶生晚餐",
        "tags_overwrite": '["家人", "生日"]',
        "auto_excluded": 1,
    }


def test_billed_inherits_matching_pending_user_metadata(tmp_store: BankStore) -> None:
    txn = {"card_no": "****7032", "date": "2026-07-20", "desc": "晚餐",
           "amount": 680, "currency": "TWD"}
    _seed_pending(tmp_store, "unbilled", [txn])
    tmp_store.conn.execute(
        """UPDATE card_pending_txns
           SET category=?, subcategory=?, description_overwrite=?,
               tags_overwrite=?, auto_excluded=1""",
        ("飲食", "聚餐", "慶生晚餐", json.dumps(["家人", "生日"], ensure_ascii=False)),
    )
    tmp_store.conn.commit()

    tmp_store.upsert_card_billed(
        [{**txn, "bill_date": "2026-07-21"}],
        rules=[{"pattern": "晚餐", "category": "其他", "subcategory": "待確認"}],
    )

    row = tmp_store.conn.execute(
        """SELECT category, subcategory, description_overwrite,
                  tags_overwrite, auto_excluded
           FROM card_billed_txns""",
    ).fetchone()
    assert row is not None
    assert dict(row) == {
        "category": "飲食",
        "subcategory": "聚餐",
        "description_overwrite": "慶生晚餐",
        "tags_overwrite": '["家人", "生日"]',
        "auto_excluded": 1,
    }
    pending_count = tmp_store.conn.execute(
        "SELECT COUNT(*) FROM card_pending_txns",
    ).fetchone()
    assert pending_count is not None
    assert pending_count[0] == 0


def test_billed_does_not_inherit_metadata_without_complete_identity(
    tmp_store: BankStore,
) -> None:
    txn = {"card_no": None, "date": "2026-07-20", "desc": "晚餐",
           "amount": 680, "currency": "TWD"}
    _seed_pending(tmp_store, "unbilled", [txn])
    tmp_store.conn.execute(
        "UPDATE card_pending_txns SET category='飲食', tags_overwrite='[\"家人\"]'",
    )
    tmp_store.conn.commit()

    tmp_store.upsert_card_billed([{**txn, "bill_date": "2026-07-21"}])

    billed = tmp_store.conn.execute(
        "SELECT category, tags_overwrite FROM card_billed_txns",
    ).fetchone()
    assert billed is not None
    assert dict(billed) == {"category": None, "tags_overwrite": None}
