"""Regression test for migrations/backfill_ctbc_post_date.py.

背景:
    CTBC persist 之前 BUG 把 post_date = consume_date (line 255 寫死),
    舊 row post_date == consume_date.
    0.3.1 fix 後 sync 進來新 row post_date = postingDt (真實入帳日, !=consume_date).
    dedup_key 涵蓋 post_date → 新舊兩筆會共存, 在 TxnDetail 看到「相同消費」兩列.

Script 策略:
    對 (user_id, card_no, consume_date, amount, description) 全等的 group,
    若同時存在 「post_date == consume_date」 + 「post_date != consume_date」 兩筆,
    刪掉「post_date == consume_date」(BUG row), 保留「post_date != consume_date」(真值).

    其他 group (只有一筆, 或都是 post==consume 沒新值)  → 不動.
    保護: HSBC 分期、esun 等銀行根本 post=consume 是真實值, 沒有「對應的 post!=consume」
    就不會被誤刪.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core import store as store_mod
from backend.core.store import BankStore


@pytest.fixture
def tmp_store(tmp_path: Path, monkeypatch) -> BankStore:
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path)
    return BankStore("testbank")


def _insert_raw_billed(conn, **kw) -> int:
    cur = conn.execute(
        """INSERT INTO card_billed_txns
           (card_no, bill_date, currency, consume_date, post_date, description,
            amount, consume_country, consume_currency, consume_amount, first_seen,
            dedup_key, category)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (kw["card_no"], kw.get("bill_date"), kw.get("currency", "TWD"),
         kw["consume_date"], kw["post_date"],
         kw["description"], kw["amount"], None,
         kw.get("consume_currency"), kw.get("consume_amount"),
         kw["first_seen"], kw["dedup_key"], None),
    )
    return cur.lastrowid


def _run_backfill(store: BankStore, *, dry_run: bool = False):
    """Invoke the backfill function — script 直接 import 出來測.

    Returns: dict {'deleted': int, 'kept': int, 'pairs': int}
    """
    from migrations.backfill_ctbc_post_date import backfill_for_sqlite
    return backfill_for_sqlite(store.conn, dry_run=dry_run)


# ---------------- HAPPY PATH ----------------

def test_backfill_deletes_bug_row_keeps_real_row(tmp_store: BankStore) -> None:
    """經典 case: 同消費的舊 BUG row (post=consume) + 新真值 row (post!=consume) 兩筆共存."""
    conn = tmp_store.conn
    bug_id = _insert_raw_billed(
        conn,
        card_no="1234", consume_date="2026-06-05", post_date="2026-06-05",
        description="SUKIYA", amount=350, bill_date="2026-06-30",
        first_seen="2026-06-10T00:00:00", dedup_key="bug1",
    )
    real_id = _insert_raw_billed(
        conn,
        card_no="1234", consume_date="2026-06-05", post_date="2026-06-08",
        description="SUKIYA", amount=350, bill_date="2026-06-30",
        first_seen="2026-06-19T00:00:00", dedup_key="real1",
    )
    conn.commit()

    result = _run_backfill(tmp_store)

    assert result["deleted"] == 1
    assert result["pairs"] == 1
    remaining = [r[0] for r in conn.execute("SELECT id FROM card_billed_txns")]
    assert bug_id not in remaining
    assert real_id in remaining


def test_backfill_dry_run_no_delete(tmp_store: BankStore) -> None:
    """--dry-run 模式只報告不真刪."""
    conn = tmp_store.conn
    _insert_raw_billed(
        conn,
        card_no="1234", consume_date="2026-06-05", post_date="2026-06-05",
        description="SUKIYA", amount=350, bill_date="2026-06-30",
        first_seen="2026-06-10T00:00:00", dedup_key="bug1",
    )
    _insert_raw_billed(
        conn,
        card_no="1234", consume_date="2026-06-05", post_date="2026-06-08",
        description="SUKIYA", amount=350, bill_date="2026-06-30",
        first_seen="2026-06-19T00:00:00", dedup_key="real1",
    )
    conn.commit()

    result = _run_backfill(tmp_store, dry_run=True)

    # dry-run 回報「會刪 1 筆」但實際 row 數還是 2
    assert result["deleted"] == 1
    count = conn.execute("SELECT COUNT(*) FROM card_billed_txns").fetchone()[0]
    assert count == 2


# ---------------- 保護條件 ----------------

def test_backfill_skips_when_no_real_row(tmp_store: BankStore) -> None:
    """單筆 post=consume row (沒有對應 post!=consume) → 不動.

    保護 esun/ubot 等銀行真實沒 postingDt 的 row.
    """
    conn = tmp_store.conn
    _insert_raw_billed(
        conn,
        card_no="9999", consume_date="2026-05-01", post_date="2026-05-01",
        description="ESUN_TXN", amount=100, bill_date="2026-05-31",
        first_seen="2026-05-10T00:00:00", dedup_key="esun1",
    )
    conn.commit()

    result = _run_backfill(tmp_store)

    assert result["deleted"] == 0
    assert result["pairs"] == 0
    count = conn.execute("SELECT COUNT(*) FROM card_billed_txns").fetchone()[0]
    assert count == 1


def test_backfill_skips_hsbc_installments(tmp_store: BankStore) -> None:
    """HSBC 分期付款: 同 consume_date 同 amount 同 desc, post_date 不同 (期數).

    每期都 post != consume, 沒 BUG row 對應, 完全不該被誤刪.
    """
    conn = tmp_store.conn
    for i, post in enumerate(["2026-02-02", "2026-03-02", "2026-04-02", "2026-05-02"]):
        _insert_raw_billed(
            conn,
            card_no="HSBC_CARD", consume_date="2026-01-27",
            post_date=post,
            description="分期付款 剩餘 0",
            amount=1000, bill_date=post,
            first_seen="2026-02-10T00:00:00",
            dedup_key=f"hsbc_inst_{i}",
        )
    conn.commit()

    result = _run_backfill(tmp_store)

    assert result["deleted"] == 0
    count = conn.execute("SELECT COUNT(*) FROM card_billed_txns").fetchone()[0]
    assert count == 4  # 4 期全留


def test_backfill_multiple_user_isolation(tmp_store: BankStore) -> None:
    """user_id=1 / user_id=2 各自有 BUG+real pair, script 應只動本 user 不誤刪別 user."""
    conn = tmp_store.conn
    # user_id=1 (default) — 經典 pair
    bug_u1 = _insert_raw_billed(
        conn,
        card_no="1234", consume_date="2026-06-05", post_date="2026-06-05",
        description="SUKIYA", amount=350, bill_date="2026-06-30",
        first_seen="2026-06-10T00:00:00", dedup_key="bug_u1",
    )
    real_u1 = _insert_raw_billed(
        conn,
        card_no="1234", consume_date="2026-06-05", post_date="2026-06-08",
        description="SUKIYA", amount=350, bill_date="2026-06-30",
        first_seen="2026-06-19T00:00:00", dedup_key="real_u1",
    )
    # user_id=2 — 另一個 user 的 pair (UPDATE row 強塞 user_id=2)
    bug_u2 = _insert_raw_billed(
        conn,
        card_no="1234", consume_date="2026-06-05", post_date="2026-06-05",
        description="SUKIYA", amount=350, bill_date="2026-06-30",
        first_seen="2026-06-10T00:00:00", dedup_key="bug_u2",
    )
    real_u2 = _insert_raw_billed(
        conn,
        card_no="1234", consume_date="2026-06-05", post_date="2026-06-08",
        description="SUKIYA", amount=350, bill_date="2026-06-30",
        first_seen="2026-06-19T00:00:00", dedup_key="real_u2",
    )
    conn.execute("UPDATE card_billed_txns SET user_id = 2 WHERE id IN (?, ?)",
                 (bug_u2, real_u2))
    conn.commit()

    result = _run_backfill(tmp_store)

    # 兩個 user 各刪 1 筆 BUG row
    assert result["deleted"] == 2
    remaining = sorted(
        r[0] for r in conn.execute("SELECT id FROM card_billed_txns ORDER BY id")
    )
    # 留 real_u1 + real_u2 (兩筆), 刪 bug_u1 + bug_u2
    assert bug_u1 not in remaining
    assert bug_u2 not in remaining
    assert real_u1 in remaining
    assert real_u2 in remaining


def test_backfill_multiple_bug_rows_same_group_all_deleted(tmp_store: BankStore) -> None:
    """同 group 內有多筆 BUG row (再 sync 多次未升級) + 1 筆 real row → 全部 BUG 刪光留 real."""
    conn = tmp_store.conn
    bug_ids = []
    for i in range(3):
        bug_ids.append(_insert_raw_billed(
            conn,
            card_no="1234", consume_date="2026-06-05", post_date="2026-06-05",
            description="SUKIYA", amount=350, bill_date="2026-06-30",
            first_seen=f"2026-06-1{i}T00:00:00", dedup_key=f"bug_{i}",
        ))
    real_id = _insert_raw_billed(
        conn,
        card_no="1234", consume_date="2026-06-05", post_date="2026-06-08",
        description="SUKIYA", amount=350, bill_date="2026-06-30",
        first_seen="2026-06-19T00:00:00", dedup_key="real",
    )
    conn.commit()

    result = _run_backfill(tmp_store)

    assert result["deleted"] == 3
    remaining = [r[0] for r in conn.execute("SELECT id FROM card_billed_txns")]
    for bid in bug_ids:
        assert bid not in remaining
    assert real_id in remaining


def test_backfill_empty_table(tmp_store: BankStore) -> None:
    """空表跑不該炸."""
    result = _run_backfill(tmp_store)
    assert result["deleted"] == 0
    assert result["pairs"] == 0


def test_backfill_different_amounts_not_paired(tmp_store: BankStore) -> None:
    """同 card+date+desc 但 amount 不同 → 不算同 group, 都留."""
    conn = tmp_store.conn
    _insert_raw_billed(
        conn,
        card_no="1234", consume_date="2026-06-05", post_date="2026-06-05",
        description="SUKIYA", amount=350, bill_date="2026-06-30",
        first_seen="2026-06-10T00:00:00", dedup_key="a",
    )
    _insert_raw_billed(
        conn,
        card_no="1234", consume_date="2026-06-05", post_date="2026-06-08",
        description="SUKIYA", amount=500,  # 不同 amount
        bill_date="2026-06-30",
        first_seen="2026-06-19T00:00:00", dedup_key="b",
    )
    conn.commit()

    result = _run_backfill(tmp_store)

    assert result["deleted"] == 0
    count = conn.execute("SELECT COUNT(*) FROM card_billed_txns").fetchone()[0]
    assert count == 2


def test_backfill_returns_pairs_count(tmp_store: BankStore) -> None:
    """3 個獨立 pair (3 BUG + 3 real) → deleted=3, pairs=3."""
    conn = tmp_store.conn
    for i in range(3):
        _insert_raw_billed(
            conn,
            card_no=f"card_{i}", consume_date="2026-06-05",
            post_date="2026-06-05",
            description=f"desc_{i}", amount=100 + i,
            bill_date="2026-06-30",
            first_seen="2026-06-10T00:00:00", dedup_key=f"bug_{i}",
        )
        _insert_raw_billed(
            conn,
            card_no=f"card_{i}", consume_date="2026-06-05",
            post_date="2026-06-08",
            description=f"desc_{i}", amount=100 + i,
            bill_date="2026-06-30",
            first_seen="2026-06-19T00:00:00", dedup_key=f"real_{i}",
        )
    conn.commit()

    result = _run_backfill(tmp_store)

    assert result["deleted"] == 3
    assert result["pairs"] == 3
