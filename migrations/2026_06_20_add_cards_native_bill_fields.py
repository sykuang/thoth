"""One-shot prod PG migration: 加 cards 表 native 欄到 10 家 bank schemas.

跑法：在可連 thoth VNet 的執行環境注入 `DATABASE_URL` 後執行：
  uv run python migrations/2026_06_20_add_cards_native_bill_fields.py

新欄 (3):
  bill_due_amount      REAL  -- HSBC: Last Statement Amount (本期應繳)
  last_payment_amount  REAL  -- HSBC: Last Payment Amount
  last_payment_date    TEXT  -- HSBC: Last Payment Date (YYYY-MM-DD)

NULL → db_facade _bill_summary_for_cards 走 derive fallback (其他銀行不變).
HSBC 下次 sync 起 persist 會寫進這些欄.

Idempotent: 已存在欄就 skip (IF NOT EXISTS).
"""
import os
import psycopg

BANKS = [
    "bank_cathay",
    "bank_ctbc",
    "bank_dbs",
    "bank_esun",
    "bank_fubon",
    "bank_hsbc",
    "bank_scsb",
    "bank_sinopac",
    "bank_taishin",
    "bank_ubot",
]

NEW_COLUMNS = [
    ("bill_due_amount", "REAL"),
    ("last_payment_amount", "REAL"),
    ("last_payment_date", "TEXT"),
]

database_url = os.environ["DATABASE_URL"]
con = psycopg.connect(database_url)
con.autocommit = True  # DDL 不走 tx (PG 對 ALTER TABLE 也 OK)

print("=== migration: 加 cards 表 native bill fields ===\n")

for bank in BANKS:
    # 先確認 schema 存在
    cur = con.cursor()
    cur.execute(
        """SELECT schema_name FROM information_schema.schemata
           WHERE schema_name = %s""",
        (bank,),
    )
    if not cur.fetchone():
        print(f"[SKIP] {bank}: schema 不存在")
        continue

    # 確認 cards 表存在
    cur.execute(
        """SELECT table_name FROM information_schema.tables
           WHERE table_schema = %s AND table_name = 'cards'""",
        (bank,),
    )
    if not cur.fetchone():
        print(f"[SKIP] {bank}: cards 表不存在")
        continue

    # 抓現有欄
    cur.execute(
        """SELECT column_name FROM information_schema.columns
           WHERE table_schema = %s AND table_name = 'cards'""",
        (bank,),
    )
    existing = {r[0] for r in cur.fetchall()}

    print(f"[{bank}] cards 表現有 {len(existing)} 欄")
    for col, ty in NEW_COLUMNS:
        if col in existing:
            print(f"  ✓ {col} 已存在, skip")
            continue
        sql = f'ALTER TABLE {bank}.cards ADD COLUMN {col} {ty}'
        print(f"  + {sql}")
        cur.execute(sql)
        print("    ✅ added")

print("\n=== done ===")

# 驗證 bank_hsbc 三欄都在
cur = con.cursor()
cur.execute(
    """SELECT column_name, data_type FROM information_schema.columns
       WHERE table_schema = 'bank_hsbc' AND table_name = 'cards'
         AND column_name IN ('bill_due_amount', 'last_payment_amount', 'last_payment_date')
       ORDER BY column_name"""
)
print("\nbank_hsbc.cards new cols:")
for r in cur.fetchall():
    print(f"  {r[0]:<22} {r[1]}")

con.close()
