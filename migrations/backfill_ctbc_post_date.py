"""Backfill: 清掉 CTBC `post_date == consume_date` 的舊 BUG row.

背景
====
0.3.0 以前 backend/core/persist/ctbc.py line 255 寫死:
    "post_date": consume_date,  # CTBC 無單獨入帳日 (← 錯)
事實上 raw API 有 `postingDt` 欄位 (MMDDYY 格式), 跟 `purchaseDt` (消費日) 並存,
通常相差 1-3 天.

0.3.1 fix 後新 sync 進來的 row `post_date = postingDt` (真實入帳日 != consume_date).
但 `card_billed_txns` dedup_key 涵蓋 post_date → ON CONFLICT(user_id, dedup_key)
DO NOTHING 不會覆蓋舊 row, 所以 UI 上會看到「同一筆消費出現兩列」:
  - 舊 row: post_date = consume_date (BUG 寫入的假值)
  - 新 row: post_date = postingDt (真實入帳日)

策略
====
對 (user_id, card_no, consume_date, amount, description) 全等的 group, 若同時存在
「post_date == consume_date」 + 「post_date != consume_date」 兩筆,
刪掉「post_date == consume_date」(BUG row), 保留「post_date != consume_date」(真值).

保護:
  - 單筆 post=consume 沒對應 (esun/ubot 等沒 postingDt 銀行) → 不動
  - HSBC 分期付款 (同消費多期 post_date 各不同, 都 != consume_date) → 不動
  - 不同 user 各自獨立處理 (PARTITION BY user_id)

用法
====
mac local (SQLite):
    DB_BACKEND=sqlite uv run python -m migrations.backfill_ctbc_post_date            # dry-run
    DB_BACKEND=sqlite uv run python -m migrations.backfill_ctbc_post_date --execute  # 真刪

prod pod (PG, via `az containerapp exec`):
    az containerapp exec -n thoth-backend-vnet -g thoth-rg \\
        --command "python -m migrations.backfill_ctbc_post_date"            # dry-run
    az containerapp exec -n thoth-backend-vnet -g thoth-rg \\
        --command "python -m migrations.backfill_ctbc_post_date --execute"  # 真刪

退場
====
所有 CTBC user 都 backfill 完一輪後, 此 script 應該被刪除 (一次性 migration).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 加 path 讓 standalone 跑得起來
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---------------- SQLite path ----------------

def backfill_for_sqlite(conn, *, dry_run: bool = False) -> dict:
    """對 SQLite connection 跑 backfill.

    Returns: {'deleted': int, 'pairs': int, 'users_affected': set[int]}
    """
    # 找 BUG row id: 同 group 內存在 post_date != consume_date 的對應 row
    # 用 EXISTS 子查詢 (SQLite + PG 通用)
    candidates_sql = """
        SELECT bug.id, bug.user_id
        FROM card_billed_txns AS bug
        WHERE bug.post_date IS NOT NULL
          AND bug.consume_date IS NOT NULL
          AND bug.post_date = bug.consume_date
          AND bug.card_no IS NOT NULL
          AND bug.amount IS NOT NULL
          AND bug.description IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM card_billed_txns AS real
              WHERE real.user_id = bug.user_id
                AND real.card_no = bug.card_no
                AND real.consume_date = bug.consume_date
                AND real.amount = bug.amount
                AND real.description = bug.description
                AND real.post_date IS NOT NULL
                AND real.post_date != real.consume_date
          )
    """
    rows = list(conn.execute(candidates_sql))
    deleted_count = len(rows)
    users = {r[1] for r in rows}

    # pairs = 受影響的 (user, card, date, amount, desc) 不同 group 數
    pairs_sql = """
        SELECT COUNT(DISTINCT user_id || '|' || card_no || '|' || consume_date
                              || '|' || amount || '|' || description)
        FROM card_billed_txns AS bug
        WHERE bug.post_date = bug.consume_date
          AND bug.card_no IS NOT NULL
          AND bug.amount IS NOT NULL
          AND bug.description IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM card_billed_txns AS real
              WHERE real.user_id = bug.user_id
                AND real.card_no = bug.card_no
                AND real.consume_date = bug.consume_date
                AND real.amount = bug.amount
                AND real.description = bug.description
                AND real.post_date != real.consume_date
          )
    """
    pairs_count = conn.execute(pairs_sql).fetchone()[0]

    if not dry_run and deleted_count > 0:
        bug_ids = [r[0] for r in rows]
        # 分批 DELETE (避免 SQLite 999 parameter limit)
        BATCH = 500
        for i in range(0, len(bug_ids), BATCH):
            chunk = bug_ids[i:i + BATCH]
            placeholders = ",".join("?" * len(chunk))
            conn.execute(
                f"DELETE FROM card_billed_txns WHERE id IN ({placeholders})",
                chunk,
            )
        conn.commit()

    return {
        "deleted": deleted_count,
        "pairs": pairs_count,
        "users_affected": users,
    }


# ---------------- PG path ----------------

def backfill_for_postgres(conn, *, dry_run: bool = False) -> dict:
    """對 psycopg connection 跑 backfill.

    psycopg 用 %s placeholder, 跟 SQLite ? 不同. SQL 結構幾乎一樣.
    Returns: {'deleted': int, 'pairs': int, 'users_affected': set[int]}
    """
    candidates_sql = """
        SELECT bug.id, bug.user_id
        FROM card_billed_txns AS bug
        WHERE bug.post_date IS NOT NULL
          AND bug.consume_date IS NOT NULL
          AND bug.post_date = bug.consume_date
          AND bug.card_no IS NOT NULL
          AND bug.amount IS NOT NULL
          AND bug.description IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM card_billed_txns AS real
              WHERE real.user_id = bug.user_id
                AND real.card_no = bug.card_no
                AND real.consume_date = bug.consume_date
                AND real.amount = bug.amount
                AND real.description = bug.description
                AND real.post_date IS NOT NULL
                AND real.post_date != real.consume_date
          )
    """
    with conn.cursor() as cur:
        cur.execute(candidates_sql)
        rows = cur.fetchall()
    deleted_count = len(rows)
    users = {r[1] for r in rows}

    # PG 用 || 字串接是 ANSI, COUNT(DISTINCT ...) PG/SQLite 都行
    pairs_sql = """
        SELECT COUNT(DISTINCT user_id::text || '|' || card_no || '|' || consume_date
                              || '|' || amount::text || '|' || description)
        FROM card_billed_txns AS bug
        WHERE bug.post_date = bug.consume_date
          AND bug.card_no IS NOT NULL
          AND bug.amount IS NOT NULL
          AND bug.description IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM card_billed_txns AS real
              WHERE real.user_id = bug.user_id
                AND real.card_no = bug.card_no
                AND real.consume_date = bug.consume_date
                AND real.amount = bug.amount
                AND real.description = bug.description
                AND real.post_date != real.consume_date
          )
    """
    with conn.cursor() as cur:
        cur.execute(pairs_sql)
        pairs_count = cur.fetchone()[0]

    if not dry_run and deleted_count > 0:
        bug_ids = [r[0] for r in rows]
        # PG 用 ANY(%s) 不必分批
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM card_billed_txns WHERE id = ANY(%s)",
                (bug_ids,),
            )
        conn.commit()

    return {
        "deleted": deleted_count,
        "pairs": pairs_count,
        "users_affected": users,
    }


# ---------------- entry point ----------------

def main() -> int:
    ap = argparse.ArgumentParser(
        prog="backfill_ctbc_post_date",
        description="清掉 CTBC post_date == consume_date 的舊 BUG row",
    )
    ap.add_argument(
        "--execute",
        action="store_true",
        help="預設 dry-run 只報數量; --execute 才真刪",
    )
    args = ap.parse_args()

    dry_run = not args.execute
    backend = os.environ.get("DB_BACKEND", "sqlite").lower()

    if backend == "postgres":
        # Prod PG path — 每銀行一個 schema (bank_ctbc / bank_cathay / ...),
        # 遍歷所有 bank schema 各跑一次 backfill. 實際 BUG 只發生在 bank_ctbc,
        # 其他 schema 應該 deleted=0.
        from backend.core.bank_pg import _dsn, schema_name
        import psycopg

        BANKS = {"cathay", "ubot", "hsbc", "ctbc", "sinopac", "scsb",
                 "esun", "taishin", "fubon", "dbs", "scb", "linebank"}

        dsn = _dsn()
        total = {"deleted": 0, "pairs": 0, "users_affected": set()}
        with psycopg.connect(dsn) as conn:
            for bank in sorted(BANKS):
                schema = schema_name(bank)
                # 先檢查 schema 是否存在 + 表是否存在
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema = %s AND table_name = 'card_billed_txns'",
                        (schema,),
                    )
                    if not cur.fetchone():
                        print(f"  [skip] {bank}: no card_billed_txns in schema {schema}",
                              file=sys.stderr)
                        continue
                    # 切 search_path 到該 schema
                    cur.execute(f'SET search_path TO "{schema}", public')
                r = backfill_for_postgres(conn, dry_run=dry_run)
                if r["deleted"] > 0 or r["pairs"] > 0:
                    print(f"  [{bank}] deleted={r['deleted']} pairs={r['pairs']} "
                          f"users={sorted(r['users_affected'])}")
                total["deleted"] += r["deleted"]
                total["pairs"] += r["pairs"]
                total["users_affected"] |= r["users_affected"]
        result = total
    else:
        # SQLite path — 遍歷所有 bank DB 用 BankStore 開連線
        # 實際 BUG 只發生在 ctbc.sqlite, 但保險起見全掃.
        from backend.core.store import BankStore

        BANKS = {"cathay", "ubot", "hsbc", "ctbc", "sinopac", "scsb",
                 "esun", "taishin", "fubon", "dbs", "scb", "linebank"}

        total = {"deleted": 0, "pairs": 0, "users_affected": set()}
        for bank in BANKS:
            try:
                store = BankStore(bank, user_id=1)  # user_id 不影響 backfill (掃全 user)
            except Exception as e:
                print(f"  [skip] {bank}: {e}", file=sys.stderr)
                continue
            try:
                r = backfill_for_sqlite(store.conn, dry_run=dry_run)
            finally:
                store.close()
            if r["deleted"] > 0 or r["pairs"] > 0:
                print(f"  [{bank}] deleted={r['deleted']} pairs={r['pairs']} "
                      f"users={sorted(r['users_affected'])}")
            total["deleted"] += r["deleted"]
            total["pairs"] += r["pairs"]
            total["users_affected"] |= r["users_affected"]
        result = total

    mode = "DRY-RUN (no changes)" if dry_run else "EXECUTED"
    print(f"\n=== Backfill result ({mode}) ===")
    print(f"  BUG rows {'would be ' if dry_run else ''}deleted: {result['deleted']}")
    print(f"  Affected (user, card, date, amount, desc) groups: {result['pairs']}")
    print(f"  Users affected: {sorted(result['users_affected'])}")
    if dry_run and result["deleted"] > 0:
        print("\n  Re-run with --execute to actually delete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
