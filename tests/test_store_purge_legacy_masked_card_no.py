"""驗證 BankStore.purge_legacy_masked_card_no_rows() 一次性 cleanup helper。

Context (2026-06-20): esun persist 在 2026-06-13 ~ 2026-06-20 期間 bug，把 raw
masked full card_no (例 '9064-XXXX-XXXX-7032') 直接寫進 card_billed_txns/
card_pending_txns，但 cards 表用 `****{last4}` 格式 → bill_summary join 失敗
→ 帳戶 tab 顯示帳單 0。Fix 後 (bcfbf6f) 需 idempotent cleanup helper 把舊格式
row 一次性砍掉。

此 helper 原本 inline SQL 寫在 persist/esun.py:168-174 違反 Plan B SQL audit
（all SQL 必須在 infrastructure layer），這個 test 也確保 helper 在 store.py
而不是 persist。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.store import BankStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch):
    from backend.core import store as store_mod
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path, raising=True)
    s = BankStore("legacy_purge_test")
    yield s
    s.close()


def _insert_raw_billed(store, **kwargs):
    """直接 INSERT 繞過 upsert，模擬舊格式 row（dedup_key 隨便給但 unique）。"""
    import time
    now = "2026-06-19T00:00:00+00:00"
    kw = {
        "user_id": 1,
        "card_no": "",
        "bill_date": None,
        "currency": "TWD",
        "consume_date": "2026-06-15",
        "description": "test",
        "amount": 100,
        "consume_country": None,
        "consume_currency": None,
        "consume_amount": None,
        "first_seen": now,
        "dedup_key": f"test_{time.time_ns()}_{kwargs.get('card_no', '')}_{kwargs.get('description', '')}",
        "txn_type": "expense",
    }
    kw.update(kwargs)
    store.conn.execute(
        """INSERT INTO card_billed_txns
           (user_id, card_no, bill_date, currency, consume_date, description,
            amount, consume_country, consume_currency, consume_amount,
            first_seen, dedup_key, txn_type)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (kw["user_id"], kw["card_no"], kw["bill_date"], kw["currency"],
         kw["consume_date"], kw["description"], kw["amount"],
         kw["consume_country"], kw["consume_currency"], kw["consume_amount"],
         kw["first_seen"], kw["dedup_key"], kw["txn_type"]),
    )
    store.conn.commit()


def _insert_raw_pending(store, **kwargs):
    """直接 INSERT pending row。"""
    kw = {
        "user_id": 1,
        "scope": "unbilled",
        "card_no": "",
        "consume_date": "2026-06-15",
        "description": "test",
        "amount": 100,
        "currency": "TWD",
        "consume_country": None,
        "consume_currency": None,
        "consume_amount": None,
        "refreshed_at": "2026-06-19T00:00:00+00:00",
        "txn_type": "expense",
    }
    kw.update(kwargs)
    store.conn.execute(
        """INSERT INTO card_pending_txns
           (user_id, scope, card_no, consume_date, description, amount, currency,
            consume_country, consume_currency, consume_amount, refreshed_at, txn_type)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (kw["user_id"], kw["scope"], kw["card_no"], kw["consume_date"],
         kw["description"], kw["amount"], kw["currency"],
         kw["consume_country"], kw["consume_currency"], kw["consume_amount"],
         kw["refreshed_at"], kw["txn_type"]),
    )
    store.conn.commit()


def test_purge_deletes_legacy_billed_rows(store):
    """billed rows with raw masked full card_no → 全砍。"""
    _insert_raw_billed(store, card_no="9064-XXXX-XXXX-7032", description="old1")
    _insert_raw_billed(store, card_no="9049-XXXX-XXXX-7050", description="old2")
    _insert_raw_billed(store, card_no="****7032", description="new1")  # 新格式不該砍

    b, p = store.purge_legacy_masked_card_no_rows()
    assert b == 2
    assert p == 0

    rows = store.conn.execute("SELECT card_no FROM card_billed_txns ORDER BY description").fetchall()
    assert [r["card_no"] for r in rows] == ["****7032"]


def test_purge_deletes_legacy_pending_rows(store):
    """pending rows with raw masked full card_no → 全砍。"""
    _insert_raw_pending(store, card_no="9064-XXXX-XXXX-7032", description="oldP1")
    _insert_raw_pending(store, card_no="****7032", description="newP1")

    b, p = store.purge_legacy_masked_card_no_rows()
    assert b == 0
    assert p == 1

    rows = store.conn.execute("SELECT card_no FROM card_pending_txns").fetchall()
    assert len(rows) == 1
    assert rows[0]["card_no"] == "****7032"


def test_purge_idempotent_empty_db(store):
    """空 DB → 0+0，不爆炸。"""
    b, p = store.purge_legacy_masked_card_no_rows()
    assert (b, p) == (0, 0)


def test_purge_idempotent_no_legacy_rows(store):
    """全部新格式 row → 0+0，不誤殺。"""
    _insert_raw_billed(store, card_no="****7032", description="new1")
    _insert_raw_pending(store, card_no="****7032", description="newP1")

    b, p = store.purge_legacy_masked_card_no_rows()
    assert (b, p) == (0, 0)

    assert store.conn.execute("SELECT COUNT(*) FROM card_billed_txns").fetchone()[0] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM card_pending_txns").fetchone()[0] == 1


def test_purge_does_not_affect_other_users_rows(store):
    """user_id=2 的 legacy row → 不該被 user_id=1 的 BankStore 砍。"""
    _insert_raw_billed(store, user_id=2, card_no="9064-XXXX-XXXX-7032", description="otheruser")
    _insert_raw_billed(store, user_id=1, card_no="9064-XXXX-XXXX-7032", description="myuser")

    b, p = store.purge_legacy_masked_card_no_rows()
    assert b == 1  # 只砍 user_id=1

    rows = store.conn.execute(
        "SELECT user_id, description FROM card_billed_txns ORDER BY user_id"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["user_id"] == 2
    assert rows[0]["description"] == "otheruser"


def test_purge_matches_all_raw_masked_patterns(store):
    """任何 LIKE '%-XXXX-XXXX-%' pattern 都該砍（不限定 5242 prefix）。"""
    _insert_raw_billed(store, card_no="9064-XXXX-XXXX-7032", description="visa")
    _insert_raw_billed(store, card_no="9060-XXXX-XXXX-7016", description="mastercard")
    _insert_raw_billed(store, card_no="3792-XXXXXX-XXXXX", description="amex_no_match")  # 不符 pattern

    b, _ = store.purge_legacy_masked_card_no_rows()
    assert b == 2

    rows = store.conn.execute("SELECT description FROM card_billed_txns").fetchall()
    assert [r["description"] for r in rows] == ["amex_no_match"]
