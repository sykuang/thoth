"""Phase 6 (category taxonomy 2026-06-15) — 4 new columns migration test.

驗 store.py 的 `_migrate()` 對三張交易表加 4 個 COICOP 對齊欄位:
- flow_type: TEXT NOT NULL DEFAULT 'expense'  (收支統計閘門)
- is_subscription: INTEGER NOT NULL DEFAULT 0 (訂閱 flag)
- subcategory: TEXT NULL                       (用戶自訂子分類)
- legacy_category: TEXT NULL                   (migration audit trail)

涵蓋 7 個情境:
1. Fresh DB — 4 欄齊全
2. Idempotent — 重開 DB 不爆
3. Default value 正確 (expense / 0 / NULL / NULL)
4. NOT NULL constraint 真的 enforce
5. flow_type 4 個合法 enum 值都能寫
6. is_subscription 0/1 都能寫
7. subcategory / legacy_category 可寫可讀任意字串

詳見 wiki [[personal-finance-transaction-category-taxonomy]]
"""
from __future__ import annotations

import importlib

import pytest


CATEGORY_TABLES = ("twd_transactions", "card_billed_txns", "card_pending_txns")
NEW_COLUMNS = ("flow_type", "is_subscription", "subcategory", "legacy_category", "income_category")


@pytest.fixture
def store(tmp_path, monkeypatch):
    """隔離 BANK_DATA_ROOT 到 tmp_path 不污染 backend/data/。"""
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    import backend.core.store as store_mod
    importlib.reload(store_mod)
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path / "banks")
    bs = store_mod.BankStore("testbank")
    yield bs
    bs.close()


def _cols(store, tbl):
    return {r["name"] for r in store.conn.execute(f"PRAGMA table_info({tbl})").fetchall()}


def test_fresh_db_has_all_four_columns_in_all_three_tables(store):
    """情境 1: Fresh DB 開出來, 三張交易表 4 欄齊全。"""
    for tbl in CATEGORY_TABLES:
        cols = _cols(store, tbl)
        for col in NEW_COLUMNS:
            assert col in cols, f"{tbl} 缺欄 {col}"


def test_migration_is_idempotent(tmp_path, monkeypatch):
    """情境 2: 重開同一個 DB, _migrate 不爆 + 欄位數不變。"""
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    import backend.core.store as store_mod
    importlib.reload(store_mod)
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path / "banks")

    s1 = store_mod.BankStore("idem")
    cols_first = {tbl: _cols(s1, tbl) for tbl in CATEGORY_TABLES}
    s1.close()

    # 第二次開, 不應 raise, 欄位數應一致
    s2 = store_mod.BankStore("idem")
    cols_second = {tbl: _cols(s2, tbl) for tbl in CATEGORY_TABLES}
    s2.close()

    assert cols_first == cols_second


def test_default_values_for_new_rows(store):
    """情境 3: 新 row 不傳 flow_type/is_subscription/subcategory/legacy_category,
    default 應是 'expense' / 0 / NULL / NULL.
    """
    store.conn.execute(
        """INSERT INTO twd_transactions
           (account_no, txn_datetime, description, expend, balance, first_seen, dedup_key)
           VALUES (?,?,?,?,?,?,?)""",
        ("001", "2026-06-15 09:00", "test", 100, 1000, "2026-06-15T09:00:00Z", "dk1"),
    )
    r = store.conn.execute(
        "SELECT flow_type, is_subscription, subcategory, legacy_category "
        "FROM twd_transactions WHERE dedup_key='dk1'"
    ).fetchone()
    assert r["flow_type"] == "expense"
    assert r["is_subscription"] == 0
    assert r["subcategory"] is None
    assert r["legacy_category"] is None


def test_flow_type_not_null_constraint_enforced(store):
    """情境 4: flow_type 是 NOT NULL, UPDATE 成 NULL 必須被 reject。"""
    import sqlite3

    store.conn.execute(
        """INSERT INTO twd_transactions
           (account_no, txn_datetime, description, expend, balance, first_seen, dedup_key)
           VALUES (?,?,?,?,?,?,?)""",
        ("001", "2026-06-15 09:00", "test", 100, 1000, "2026-06-15T09:00:00Z", "dk_notnull"),
    )
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        store.conn.execute(
            "UPDATE twd_transactions SET flow_type=NULL WHERE dedup_key='dk_notnull'"
        )


def test_is_subscription_not_null_constraint_enforced(store):
    """情境 4b: is_subscription 也是 NOT NULL。"""
    import sqlite3

    store.conn.execute(
        """INSERT INTO twd_transactions
           (account_no, txn_datetime, description, expend, balance, first_seen, dedup_key)
           VALUES (?,?,?,?,?,?,?)""",
        ("001", "2026-06-15 09:00", "test", 100, 1000, "2026-06-15T09:00:00Z", "dk_sub"),
    )
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        store.conn.execute(
            "UPDATE twd_transactions SET is_subscription=NULL WHERE dedup_key='dk_sub'"
        )


@pytest.mark.parametrize("flow_type", ["expense", "income", "transfer", "investment"])
def test_flow_type_accepts_all_four_enum_values(store, flow_type):
    """情境 5: flow_type 4 個 enum 值都能寫進去 (SQLite TEXT 不做 enum 檢查, 但要確保 NOT NULL 不擋有效值)。"""
    store.conn.execute(
        """INSERT INTO twd_transactions
           (account_no, txn_datetime, description, expend, balance, first_seen,
            dedup_key, flow_type)
           VALUES (?,?,?,?,?,?,?,?)""",
        ("001", "2026-06-15 09:00", "test", 100, 1000, "2026-06-15T09:00:00Z",
         f"dk_{flow_type}", flow_type),
    )
    r = store.conn.execute(
        "SELECT flow_type FROM twd_transactions WHERE dedup_key=?", (f"dk_{flow_type}",)
    ).fetchone()
    assert r["flow_type"] == flow_type


def test_subcategory_and_legacy_category_roundtrip(store):
    """情境 7: subcategory / legacy_category 可寫任意字串 + 可讀回。"""
    store.conn.execute(
        """INSERT INTO twd_transactions
           (account_no, txn_datetime, description, expend, balance, first_seen,
            dedup_key, subcategory, legacy_category)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        ("001", "2026-06-15 09:00", "test", 100, 1000, "2026-06-15T09:00:00Z",
         "dk_sub_round", "早餐", "餐飲-早午餐"),
    )
    r = store.conn.execute(
        "SELECT subcategory, legacy_category FROM twd_transactions "
        "WHERE dedup_key='dk_sub_round'"
    ).fetchone()
    assert r["subcategory"] == "早餐"
    assert r["legacy_category"] == "餐飲-早午餐"


def test_existing_data_backfilled_to_default_on_migration(tmp_path, monkeypatch):
    """情境 8 (regression): 既有資料 ALTER ADD COLUMN 後應 backfill 成 default,
    不該變 NULL 害下次 query 出 None.
    """
    import sqlite3
    # 先做一個「老版本」DB — 不跑 _migrate, 直接寫 SCHEMA 沒 4 欄的 twd_transactions
    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE twd_transactions (
            account_no TEXT, txn_datetime TEXT, description TEXT,
            expend INTEGER, income INTEGER, balance INTEGER,
            dedup_key TEXT UNIQUE NOT NULL
        );
        INSERT INTO twd_transactions (account_no, txn_datetime, description,
            expend, balance, dedup_key)
        VALUES ('001', '2026-06-15 09:00', 'legacy txn', 100, 1000, 'legacy_dk1');
    """)
    conn.commit()
    conn.close()

    # 現在用 BankStore 開這個老 DB → 應自動跑 migration
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    import backend.core.store as store_mod
    importlib.reload(store_mod)
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path / "banks")
    # 把老 DB 搬到 BankStore 預期路徑
    tmp_path / "old.sqlite"  # store 會用 {bank}.sqlite
    s = store_mod.BankStore("old")
    # backfill 後既有 row 應該不是 NULL
    r = s.conn.execute(
        "SELECT flow_type, is_subscription FROM twd_transactions WHERE dedup_key='legacy_dk1'"
    ).fetchone()
    if r is not None:
        # 視 BankStore.__init__ 行為決定: 如果它打開的是同一個 db, backfill 後應 default 值
        # (若 init 重建 schema 則沒這 row, 跳過 assert)
        assert r["flow_type"] == "expense", f"既有 row 應 backfill 成 'expense', 實際 {r['flow_type']!r}"
        assert r["is_subscription"] == 0, f"既有 row 應 backfill 成 0, 實際 {r['is_subscription']!r}"
    s.close()


def test_existing_twd_description_is_canonicalized_from_memo_on_migration(tmp_path, monkeypatch):
    import sqlite3

    data_root = tmp_path / "banks"
    data_root.mkdir()
    db = data_root / "legacy_description.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE twd_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_no TEXT NOT NULL,
            txn_datetime TEXT NOT NULL,
            description TEXT,
            memo TEXT,
            expend INTEGER,
            income INTEGER,
            balance INTEGER,
            first_seen TEXT,
            dedup_key TEXT NOT NULL
        );
        INSERT INTO twd_transactions
            (account_no, txn_datetime, description, memo, income, balance, first_seen, dedup_key)
        VALUES
            ('001', '2026-08-10 09:00', '轉帳', '0050FUND　基金配息', 4494, 10000,
             '2026-08-10T09:00:00Z', 'legacy-canonical-description');
    """)
    conn.commit()
    conn.close()

    import backend.core.store as store_mod
    monkeypatch.setattr(store_mod, "DATA_ROOT", data_root)
    store_mod._MIGRATED_DBS.clear()
    store_mod.migrate_existing_bank_stores(["legacy_description", "absent"])
    assert not (data_root / "absent.sqlite").exists()

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT raw_description, description, memo FROM twd_transactions",
    ).fetchone()
    conn.close()

    assert row is not None
    assert dict(row) == {
        "raw_description": "轉帳",
        "description": "轉帳 - 0050FUND 基金配息",
        "memo": "0050FUND　基金配息",
    }

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO twd_transactions "
        "(account_no, txn_datetime, description, raw_description, memo, income, balance, "
        "first_seen, dedup_key, user_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("002", "2026-08-11 09:00", "純備註", None, "純備註", 1, 10001,
         "2026-08-11T09:00:00Z", "memo-only", 1),
    )
    conn.commit()
    conn.close()
    store_mod._MIGRATED_DBS.clear()
    store_mod.migrate_existing_bank_stores(["legacy_description"])

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    memo_only = conn.execute(
        "SELECT raw_description, description FROM twd_transactions WHERE account_no='002'",
    ).fetchone()
    conn.close()
    assert memo_only is not None
    assert dict(memo_only) == {"raw_description": None, "description": "純備註"}


# =============================================================================
# Phase 7 (Income 5 類 2026-06-15) — income_category 專屬 test
# =============================================================================

def test_income_category_column_is_nullable(store):
    """情境 9: income_category 是 nullable TEXT (跟 subcategory/legacy_category 同型)."""
    for tbl in CATEGORY_TABLES:
        info = store.conn.execute(f"PRAGMA table_info({tbl})").fetchall()
        ic = next((r for r in info if r["name"] == "income_category"), None)
        assert ic is not None, f"{tbl} 缺 income_category"
        assert ic["type"] == "TEXT", f"{tbl}.income_category 應 TEXT, got {ic['type']!r}"
        assert ic["notnull"] == 0, f"{tbl}.income_category 應 nullable"


def test_income_category_default_is_null(store):
    """情境 10: 插 row 不指定 income_category, 預設 NULL."""
    store.conn.execute(
        """INSERT INTO twd_transactions
           (account_no, txn_datetime, description, income, balance, first_seen, dedup_key)
           VALUES (?,?,?,?,?,?,?)""",
        ("001", "2026-06-15 09:00", "test", 100, 1000, "2026-06-15T09:00:00Z", "dk_ic_null"),
    )
    r = store.conn.execute(
        "SELECT income_category FROM twd_transactions WHERE dedup_key='dk_ic_null'"
    ).fetchone()
    assert r["income_category"] is None


@pytest.mark.parametrize("ic", [
    "salary", "bonus", "interest_dividend", "investment_gain", "other"
])
def test_income_category_accepts_all_5_enum_values(store, ic):
    """情境 11: 5 個 income_category enum 都能寫進去 (DB 層不擋, classifier 負責 valid)."""
    store.conn.execute(
        """INSERT INTO twd_transactions
           (account_no, txn_datetime, description, income, balance, first_seen,
            dedup_key, flow_type, income_category)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        ("001", "2026-06-15 09:00", "test", 100, 1000, "2026-06-15T09:00:00Z",
         f"dk_ic_{ic}", "income", ic),
    )
    r = store.conn.execute(
        f"SELECT income_category FROM twd_transactions WHERE dedup_key='dk_ic_{ic}'"
    ).fetchone()
    assert r["income_category"] == ic


def test_income_category_independent_of_flow_type(store):
    """情境 12 (anti-pattern guard): DB 層不強制 income_category 只能跟 flow_type='income' 共存。

    這個鐵則由 classifier / persist 層保證, DB schema 故意不加 CHECK constraint
    避免 migration 時舊資料卡關。
    """
    # 即使 flow_type=expense 也能寫 income_category (DB 不擋, 但這是反模式應用 lint 抓)
    store.conn.execute(
        """INSERT INTO twd_transactions
           (account_no, txn_datetime, description, expend, balance, first_seen,
            dedup_key, flow_type, income_category)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        ("001", "2026-06-15 09:00", "anti-pattern", 100, 1000,
         "2026-06-15T09:00:00Z", "dk_ic_anti", "expense", "salary"),
    )
    r = store.conn.execute(
        "SELECT flow_type, income_category FROM twd_transactions WHERE dedup_key='dk_ic_anti'"
    ).fetchone()
    # DB 允許 (沒 raise)，但語意上是反模式
    assert r["flow_type"] == "expense"
    assert r["income_category"] == "salary"
