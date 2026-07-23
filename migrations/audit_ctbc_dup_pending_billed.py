"""Read-only audit: 找使用者指認的「暫無資訊 [SGD 100.2]」+「中華航空 / china air」兩筆 CTBC
重複案例的 raw row，列出兩表所有欄位看 dedup 該用什麼 key。

Usage:
    DB_BACKEND=postgres DATABASE_URL=$(az ...) uv run python -m migrations.audit_ctbc_dup_pending_billed

NEVER deletes anything. Pure read.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    from backend.core.bank_pg import _dsn, schema_name
    import psycopg

    dsn = _dsn()
    schema = schema_name("ctbc")
    print(f"=== audit ctbc schema: {schema} ===\n")

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(f'SET search_path TO "{schema}", public')

        # --- 1. 撈所有 pending row (估 user_id=6 不超過 50 筆) ---
        print("=" * 70)
        print("STEP 1: card_pending_txns columns + ALL rows for user_id=6")
        print("=" * 70)
        cur.execute("""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = 'card_pending_txns'
                ORDER BY ordinal_position
            """, (schema,))
        cols = cur.fetchall()
        print(f"pending columns ({len(cols)}):")
        for c in cols:
            print(f"  - {c[0]:25s} ({c[1]})")

        cur.execute("""
                SELECT * FROM card_pending_txns
                WHERE user_id = 6
                ORDER BY consume_date DESC, id DESC
            """)
        colnames = [d[0] for d in cur.description]
        pending_rows = cur.fetchall()
        print(f"\npending rows (user_id=6): {len(pending_rows)}")
        for i, r in enumerate(pending_rows):
            d = dict(zip(colnames, r))
            desc_short = (d.get('description') or '')[:35]
            print(f"  [{i:2d}] id={d['id']:4d} card={d.get('card_no','?'):>10} "
                  f"date={d.get('consume_date','?')} amt={d.get('amount','?')!s:>8} "
                  f"cur={d.get('consume_currency') or '-':>5} "
                  f"camt={d.get('consume_amount') or '-':>8} "
                  f"desc='{desc_short}'")

        # --- 2. 找關鍵字 row in pending ---
        print("\n" + "=" * 70)
        print("STEP 2: pending rows matching 'SGD' / 'china' / 'air' / '中華' / '暫無'")
        print("=" * 70)
        cur.execute("""
                SELECT * FROM card_pending_txns
                WHERE user_id = 6
                  AND (description ILIKE %s OR description ILIKE %s
                       OR description ILIKE %s OR description ILIKE %s
                       OR description ILIKE %s)
                ORDER BY consume_date DESC
            """, ('%SGD%', '%china%', '%air%', '%中華%', '%暫無%'))
        hits = cur.fetchall()
        for r in hits:
            d = dict(zip(colnames, r))
            print(f"\n--- pending row id={d['id']} ---")
            for k, v in d.items():
                if v not in (None, ''):
                    print(f"  {k:25s} = {v!r}")

        # --- 3. billed schema + 關鍵字 row in billed ---
        print("\n" + "=" * 70)
        print("STEP 3: card_billed_txns columns")
        print("=" * 70)
        cur.execute("""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = 'card_billed_txns'
                ORDER BY ordinal_position
            """, (schema,))
        for c in cur.fetchall():
            print(f"  - {c[0]:25s} ({c[1]})")

        print("\n" + "=" * 70)
        print("STEP 4: billed rows matching 'SGD' / 'china' / 'air' / '中華' / '暫無'")
        print("=" * 70)
        cur.execute("""
                SELECT * FROM card_billed_txns
                WHERE user_id = 6
                  AND (description ILIKE %s OR description ILIKE %s
                       OR description ILIKE %s OR description ILIKE %s
                       OR description ILIKE %s
                       OR consume_currency = 'SGD')
                ORDER BY consume_date DESC
            """, ('%SGD%', '%china%', '%air%', '%中華%', '%暫無%'))
        colnames_b = [d[0] for d in cur.description]
        hits_b = cur.fetchall()
        for r in hits_b:
            d = dict(zip(colnames_b, r))
            print(f"\n--- billed row id={d['id']} ---")
            for k, v in d.items():
                if v not in (None, ''):
                    print(f"  {k:25s} = {v!r}")

        # --- 5. JOIN pending + billed by (card_no, consume_date, amount) 找對應 ---
        print("\n" + "=" * 70)
        print("STEP 5: pending+billed JOIN by (card_no, consume_date, amount)")
        print("       — same key but possibly different description")
        print("=" * 70)
        cur.execute("""
                SELECT p.id AS pid, p.description AS p_desc, p.amount AS p_amt,
                       p.consume_currency AS p_cur, p.consume_amount AS p_camt,
                       b.id AS bid, b.description AS b_desc, b.amount AS b_amt,
                       b.consume_currency AS b_cur, b.consume_amount AS b_camt,
                       p.card_no, p.consume_date
                FROM card_pending_txns p
                JOIN card_billed_txns b
                  ON p.user_id = b.user_id
                 AND p.card_no = b.card_no
                 AND p.consume_date = b.consume_date
                 AND p.amount = b.amount
                WHERE p.user_id = 6
                ORDER BY p.consume_date DESC
            """)
        joins = cur.fetchall()
        print(f"\nfound {len(joins)} (pending, billed) PAIRS by (card_no, date, amount):")
        for j in joins:
            (pid, pd, pa, pc, pca,
             bid, bd, ba, bc, bca, card, date) = j
            print(f"\n  card={card} date={date} amt={pa}")
            print(f"    pending  id={pid}: desc='{pd[:40] if pd else None}' "
                  f"cur={pc} camt={pca}")
            print(f"    billed   id={bid}: desc='{bd[:40] if bd else None}' "
                  f"cur={bc} camt={bca}")
            if pd == bd:
                print("    → desc MATCH ✓ (should have been pruned!)")
            else:
                print("    → desc MISMATCH ✗ (the smoking gun for our bug)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
