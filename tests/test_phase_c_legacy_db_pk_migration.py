"""Phase C-pk (2026-06-17) — legacy DB 升級到 composite UNIQUE INDEX 的 regression test.

Reproduce production blocker — Path A 升 PK 為 (user_id, X) 後, 任何「Phase C 之前
的舊 sqlite」(沒 user_id 欄、PK 是單欄 X) 下次 sync 就會在 ON CONFLICT(user_id, X)
撞 "no PRIMARY KEY or UNIQUE constraint matches" 全爆。

修法 (BankStore._migrate + db.open_bank_conn lazy migration):
  4 張 PK 表 (balance_history/accounts/cards/daily_metrics) 升 composite UNIQUE INDEX,
  讓 INSERT...ON CONFLICT(user_id, ...) 走 unique index 而不必走 PK,
  避免 SQLite 不支援 ALTER PK 的限制。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path



def _create_legacy_cathay_sqlite(data_root: Path) -> Path:
    """Reproduce Phase C 之前的 production cathay.sqlite schema:
    - 沒 user_id 欄
    - PK 是單欄 (account_no / card_no / snapshot_date / (snapshot_date, category))
    """
    path = data_root / "cathay.sqlite"
    con = sqlite3.connect(str(path))
    # 1. accounts — 單欄 PK account_no
    con.execute("""CREATE TABLE accounts (
        account_no TEXT PRIMARY KEY, currency TEXT, branch TEXT,
        nickname TEXT, type TEXT, product_type TEXT,
        raw_balance REAL, raw_balance_date TEXT,
        updated_at TEXT NOT NULL
    )""")
    # 2. cards — 單欄 PK card_no
    con.execute("""CREATE TABLE cards (
        card_no TEXT PRIMARY KEY, name TEXT, association TEXT, type TEXT,
        updated_at TEXT NOT NULL
    )""")
    # 3. balance_history — 單欄 PK snapshot_date (production schema 有 updated_at)
    con.execute("""CREATE TABLE balance_history (
        snapshot_date TEXT PRIMARY KEY,
        twd_balance INTEGER, fx_balance REAL, fx_currency TEXT,
        updated_at TEXT NOT NULL
    )""")
    # 4. daily_metrics — 複合 PK (snapshot_date, category) 不含 user_id
    con.execute("""CREATE TABLE daily_metrics (
        snapshot_date TEXT NOT NULL, category TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (snapshot_date, category)
    )""")
    # 5. 兩張交易表 — UNIQUE INDEX 也是單欄
    con.execute("""CREATE TABLE twd_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_no TEXT NOT NULL, txn_datetime TEXT NOT NULL,
        description TEXT, expend INTEGER, income INTEGER,
        first_seen TEXT NOT NULL, dedup_key TEXT NOT NULL,
        flow_type TEXT NOT NULL DEFAULT 'expense'
    )""")
    con.execute("CREATE UNIQUE INDEX ux_twd_dedup ON twd_transactions(dedup_key)")
    con.execute("""CREATE TABLE card_billed_txns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        card_no TEXT, bill_date TEXT, consume_date TEXT,
        description TEXT, amount INTEGER,
        first_seen TEXT NOT NULL, dedup_key TEXT NOT NULL,
        flow_type TEXT NOT NULL DEFAULT 'expense'
    )""")
    con.execute("CREATE UNIQUE INDEX ux_card_billed_dedup ON card_billed_txns(dedup_key)")
    # Seed 一筆舊資料 (有 dedup_key 但沒 user_id) — migration 應 backfill user_id=1
    con.execute(
        "INSERT INTO accounts (account_no, currency, updated_at) VALUES (?, ?, ?)",
        ("LEGACY-001", "TWD", "2026-06-15"),
    )
    con.commit()
    con.close()
    return path


def test_bankstore_migrate_upgrades_legacy_pk_to_composite_unique_index(tmp_path, monkeypatch):
    """BankStore._migrate 必須對 4 張 PK 表加 composite UNIQUE INDEX,
    讓 INSERT...ON CONFLICT(user_id, ...) work 在升級後的 legacy DB。
    """
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("SERVER_FERNET_KEY", "test-fernet-key-padding-padding-padding00=")
    _create_legacy_cathay_sqlite(tmp_path)

    # 模擬 production 下次 sync — BankStore() 跑 _migrate
    import importlib

    import backend.core.store as store_mod
    importlib.reload(store_mod)
    BankStore = store_mod.BankStore

    s = BankStore("cathay", user_id=1)
    try:
        # 這四個 upsert 在修法前必爆 OperationalError
        n_acct = s.upsert_accounts([
            {"account_no": "LEGACY-001", "currency": "TWD", "nickname": "renamed"},
            {"account_no": "NEW-002", "currency": "USD"},
        ])
        n_card = s.upsert_cards([{"card_no": "9000-0000-0036-7037", "name": "test"}])
        n_bh = s.upsert_balance_history([{
            "snapshot_date": "2026-06-17", "twd_balance": 12345,
            "fx_balance": None, "fx_currency": None,
        }])
        s.put_daily_metric("balance_latest", {"twd": 12345})

        # 驗證 ON CONFLICT 真的 work — 重複 insert 不該炸
        s.upsert_accounts([{"account_no": "LEGACY-001", "currency": "TWD"}])  # update path
        s.put_daily_metric("balance_latest", {"twd": 99999})  # update path

        # 驗證 user_id 被 backfill = 1
        cur = s.conn.execute("SELECT user_id, account_no FROM accounts ORDER BY account_no")
        rows = cur.fetchall()
        assert all(r[0] == 1 for r in rows), f"user_id 未 backfill: {[(r[0], r[1]) for r in rows]}"
        assert len(rows) == 2, f"預期 2 row, 拿到 {len(rows)}"

        assert n_acct > 0
        assert n_card > 0
        assert n_bh > 0
    finally:
        s.close()


def test_lazy_migration_open_bank_conn_upgrades_legacy_pk(tmp_path, monkeypatch):
    """db.open_bank_conn 對 raw legacy DB 第一次連線也要補 composite UNIQUE INDEX,
    確保 router-side endpoint (不一定走 BankStore) 對 legacy DB 也 work。
    """
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("SERVER_FERNET_KEY", "test-fernet-key-padding-padding-padding00=")
    _create_legacy_cathay_sqlite(tmp_path)

    import importlib

    import backend.server.db as db_mod
    importlib.reload(db_mod)

    # 第一次 open_bank_conn — 該 trigger lazy migration
    con = db_mod.open_bank_conn("cathay")
    assert con is not None

    # 驗證 user_id column 被加進去
    cols = {r["name"] for r in con.execute("PRAGMA table_info(accounts)")}
    assert "user_id" in cols, "lazy migration 沒加 user_id 欄"

    # 驗證 composite UNIQUE INDEX 存在
    indexes = {r[1] for r in con.execute("PRAGMA index_list(accounts)")}
    assert "ux_accounts_user_no" in indexes, f"composite unique index 沒建: {indexes}"

    # 驗證 INSERT...ON CONFLICT(user_id, account_no) 真的 work
    con.execute("""
        INSERT INTO accounts (user_id, account_no, currency, updated_at)
        VALUES (1, 'NEW-LAZY', 'TWD', '2026-06-17')
        ON CONFLICT(user_id, account_no) DO UPDATE SET currency = excluded.currency
    """)
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    assert n == 2, f"預期 2 row, 拿到 {n}"
    con.close()


def test_phase_c_pk_indexes_idempotent_on_fresh_db(tmp_path, monkeypatch):
    """新 DB (Phase C 之後建) 已含複合 PK, CREATE UNIQUE INDEX IF NOT EXISTS 必須是
    idempotent no-op, 不能撞已存在的 PK 或 index。
    """
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("SERVER_FERNET_KEY", "test-fernet-key-padding-padding-padding00=")

    import importlib

    import backend.core.store as store_mod
    importlib.reload(store_mod)
    BankStore = store_mod.BankStore

    # 第一次 — 全新 DB, SCHEMA 已含複合 PK
    s1 = BankStore("ubot", user_id=1)
    s1.upsert_accounts([{"account_no": "FRESH-001", "currency": "TWD"}])
    s1.close()

    # 第二次 — 重開, 應該 idempotent 不爆
    s2 = BankStore("ubot", user_id=1)
    s2.upsert_accounts([{"account_no": "FRESH-002", "currency": "USD"}])

    # 驗證兩筆都在
    n = s2.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    assert n == 2
    s2.close()
