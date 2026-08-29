"""驗證 billed transition 在可信 pending refresh 後清對應 stale row。

銀行 billed 出帳後，pending 通常仍短暫出現在未出帳清單；只有本輪安全且唯一的
exact transition 可在 `fetch_ok=True` refresh 時合併。抓取失敗時 membership 不動，
多 occurrence 不猜。底層 exact helper 仍維持四欄全等與缺欄 fail-closed contract。
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
    txn = {"card_no": "****7032", "date": "2026-06-08", "desc": "中油",
           "amount": 1727, "currency": "TWD"}
    _seed_pending(tmp_store, "unbilled", [txn])
    assert tmp_store.conn.execute("SELECT COUNT(*) FROM card_pending_txns").fetchone()[0] == 1

    tmp_store.upsert_card_billed([{**txn, "bill_date": "2026-06-29"}])
    tmp_store.refresh_card_pending("unbilled", [txn], fetch_ok=True)

    assert tmp_store.conn.execute("SELECT COUNT(*) FROM card_billed_txns").fetchone()[0] == 1
    assert tmp_store.conn.execute("SELECT COUNT(*) FROM card_pending_txns").fetchone()[0] == 0, \
        "可信 pending refresh 後 exact transition 應只留 billed"


# === 2. 兩筆 pending 但只一筆出帳 → 只清出帳那筆 ===
def test_billed_purges_only_matching_pending(tmp_store: BankStore) -> None:
    pending = [
        {"card_no": "****7032", "date": "2026-06-08", "desc": "中油",
         "amount": 1727, "currency": "TWD"},
        {"card_no": "****7032", "date": "2026-06-08", "desc": "優步",
         "amount": 358, "currency": "TWD"},
    ]
    _seed_pending(tmp_store, "unbilled", pending)

    tmp_store.upsert_card_billed([
        {**pending[0], "bill_date": "2026-06-29"},
    ])
    tmp_store.refresh_card_pending("unbilled", pending, fetch_ok=True)

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
    tmp_store.refresh_card_pending("unbilled", [{
        "card_no": "****7015", "date": "2026-06-08", "desc": "中油",
        "amount": 1727, "currency": "TWD",
    }], fetch_ok=True)

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
    tmp_store.refresh_card_pending("unbilled", [{
        "card_no": "****7032", "date": "2026-06-08", "desc": "中油",
        "amount": 1700, "currency": "TWD",
    }], fetch_ok=True)

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
    tmp_store.refresh_card_pending("unbilled", [{
        "card_no": "****7032", "date": "2026-06-08", "desc": "7-11 內湖店",
        "amount": 100, "currency": "TWD",
    }], fetch_ok=True)

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
    tmp_store.refresh_card_pending("unbilled", [{
        "card_no": "****7032", "date": "2026-06-08", "desc": "中油",
        "amount": 1727, "currency": "TWD",
    }], fetch_ok=True)

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

    txn = {"card_no": "****7032", "date": "2026-06-08", "desc": "中油",
           "amount": 1727, "currency": "TWD"}
    tmp_store.upsert_card_billed([{**txn, "bill_date": "2026-06-29"}])
    tmp_store.refresh_card_pending("unbilled", [txn], fetch_ok=True)
    tmp_store.refresh_card_pending("current", [txn], fetch_ok=True)

    assert tmp_store.conn.execute("SELECT COUNT(*) FROM card_pending_txns").fetchone()[0] == 0, \
        "各 scope 經可信 refresh 後都只留 billed"


# === 8. 新 billed transition 只在可信 refresh 中做 exact merge ===


def test_refresh_pending_dedups_against_new_billed_transition(tmp_store: BankStore) -> None:
    """同一 sync 新 billed 與可信 pending 1:1 exact 時只留 billed。"""
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
    ], fetch_ok=True)

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
    txn = {"card_no": "****7036", "date": "2026-05-20", "desc": "ＳＵＫＩＹＡ",
           "amount": -268, "currency": "TWD"}
    _seed_pending(tmp_store, "current", [txn])
    assert tmp_store.conn.execute(
        "SELECT COUNT(*) FROM card_pending_txns WHERE scope='current'"
    ).fetchone()[0] == 1

    # 跑另一個 scope 的可信 refresh — 不該動 current 那筆。
    tmp_store.refresh_card_pending("unbilled", [], fetch_ok=True)

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
    tmp_store.refresh_card_pending("unbilled", [txn], fetch_ok=True)

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
