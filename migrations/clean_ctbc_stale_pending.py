"""One-off: clean stale CTBC pending rows (scope='unbilled') from prod PG.

Background:
  2026-06-19 confirmed CTBC qu041/010 (即時消費 API) returns data that never aligns
  with billed (qu002) — different dates, different countries, different desc format.
  Decision: ctbc persist_ctbc no longer writes pending rows. New ship (0.3.2)
  always calls refresh_card_pending("unbilled", [], ...) which DELETE+INSERT empty
  → naturally sweeps stale rows on next sync.

  But使用者 doesn't want to wait for next sync — clean prod now.

Strategy:
  Only DELETE WHERE bank='ctbc' AND scope='unbilled'. Never touch other banks.
  Never touch card_billed_txns.

Usage:
  DB_BACKEND=postgres DATABASE_URL=$(az ...) uv run python -m migrations.clean_ctbc_stale_pending          # dry-run
  DB_BACKEND=postgres DATABASE_URL=$(az ...) uv run python -m migrations.clean_ctbc_stale_pending --execute # real delete
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    ap = argparse.ArgumentParser(prog="clean_ctbc_stale_pending")
    ap.add_argument("--execute", action="store_true",
                    help="預設 dry-run; --execute 才真 DELETE")
    args = ap.parse_args()
    dry_run = not args.execute

    backend = os.environ.get("DB_BACKEND", "sqlite").lower()
    if backend != "postgres":
        print("ERROR: this script is prod-PG-only. Set DB_BACKEND=postgres.", file=sys.stderr)
        return 1

    from backend.core.bank_pg import _dsn, schema_name
    import psycopg

    schema = schema_name("ctbc")
    print(f"=== clean_ctbc_stale_pending (mode={'DRY-RUN' if dry_run else 'EXECUTE'}) ===")
    print(f"schema: {schema}\n")

    with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
        cur.execute(f'SET search_path TO "{schema}", public')

        # Step 1: confirm pending row count per user
        print("STEP 1: pending row count per user (scope='unbilled')")
        cur.execute("""
                SELECT user_id, COUNT(*)
                FROM card_pending_txns
                WHERE scope = 'unbilled'
                GROUP BY user_id
                ORDER BY user_id
            """)
        users = cur.fetchall()
        total = sum(c for _, c in users)
        for uid, cnt in users:
            print(f"  user_id={uid:3d}: {cnt} pending rows")
        print(f"  TOTAL: {total} rows to delete")

        # Step 2: sample preview of what will be deleted
        print("\nSTEP 2: sample of rows that will be deleted (first 10)")
        cur.execute("""
                SELECT user_id, card_no, consume_date, amount, description
                FROM card_pending_txns
                WHERE scope = 'unbilled'
                ORDER BY user_id, consume_date DESC
                LIMIT 10
            """)
        for uid, card, date, amt, desc in cur.fetchall():
            desc_short = (desc or '')[:35]
            print(f"  user={uid} card={card} date={date} amt={amt!s:>8} desc='{desc_short}'")

        # Step 3: billed unchanged check
        print("\nSTEP 3: billed row count (should be UNCHANGED, just a sanity baseline)")
        cur.execute("SELECT COUNT(*) FROM card_billed_txns")
        billed_count = cur.fetchone()[0]
        print(f"  card_billed_txns total: {billed_count}")

        # Step 4: execute (or skip in dry-run)
        if total == 0:
            print("\n→ Nothing to delete.")
        elif dry_run:
            print(f"\n=== DRY-RUN: {total} rows would be deleted (no changes made) ===")
            print("→ Re-run with --execute to actually DELETE.")
        else:
            print("\nSTEP 4: DELETE FROM card_pending_txns WHERE scope='unbilled'")
            cur.execute("DELETE FROM card_pending_txns WHERE scope = 'unbilled'")
            deleted = cur.rowcount
            conn.commit()
            print(f"  deleted: {deleted} rows")

            # verify
            cur.execute("SELECT COUNT(*) FROM card_pending_txns WHERE scope = 'unbilled'")
            left = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM card_billed_txns")
            billed_after = cur.fetchone()[0]
            print("\nverify after:")
            print(f"  pending(unbilled) remaining: {left} (expected 0)")
            print(f"  billed unchanged: {billed_after} (was {billed_count})")
            if left == 0 and billed_after == billed_count:
                print("\n✅ all good")
            else:
                print("\n❌ verify failed")
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
