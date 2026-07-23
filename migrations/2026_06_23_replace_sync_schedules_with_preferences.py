"""One-shot prod PG migration: drop user_sync_schedules + create user_sync_preferences (L13).

L12 (2026-06-22) per-account 設計被使用者「我要使用者設定一個時間給所有帳號」
直接改成 per-user 單一時間 (L13, 2026-06-23). prod 沒人設過 (reloaded 0
schedules from DB), 直接 DROP 安全.

跑法：在可連 thoth VNet 的執行環境注入 `DATABASE_URL` 後執行：
  uv run python migrations/2026_06_23_replace_sync_schedules_with_preferences.py

設計（詳 backend/server/db.py 與 backend/server/user_sync_pref_repo.py）:
  * user_id PK = 1 user 1 schedule (改自 L12 account_id PK)
  * hour 0-23, minute 0-59 — 純 daily
  * tz 預設 Asia/Taipei
  * Fire 時 scheduler.py fan-out 該 user 全部 has_creds account

Idempotent: DROP IF EXISTS + CREATE IF NOT EXISTS 重複跑安全.
"""
import os
import psycopg

database_url = os.environ["DATABASE_URL"]
con = psycopg.connect(database_url)
con.autocommit = True

print("=== migration: drop user_sync_schedules + add user_sync_preferences (L13) ===\n")

cur = con.cursor()

# Step 1: drop L12 表 (prod 從沒人設過 row, 安全)
cur.execute("SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='user_sync_schedules'")
exists_old = cur.fetchone()[0] > 0
if exists_old:
    cur.execute("SELECT COUNT(*) FROM public.user_sync_schedules")
    n = cur.fetchone()[0]
    print(f"  drop user_sync_schedules (current rows: {n})")
    cur.execute("DROP TABLE public.user_sync_schedules")
    print("✓ dropped user_sync_schedules")
else:
    print("  (user_sync_schedules not present — skip drop)")

# Step 2: create L13 表
cur.execute("""
    CREATE TABLE IF NOT EXISTS public.user_sync_preferences (
        user_id     INTEGER PRIMARY KEY,
        hour        INTEGER NOT NULL,
        minute      INTEGER NOT NULL,
        tz          TEXT NOT NULL DEFAULT 'Asia/Taipei',
        enabled     INTEGER NOT NULL DEFAULT 1,
        last_run_at TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        CHECK (hour BETWEEN 0 AND 23),
        CHECK (minute BETWEEN 0 AND 59),
        CHECK (enabled IN (0, 1))
    )
""")
print("✓ table user_sync_preferences ready")

cur.execute("""
    CREATE INDEX IF NOT EXISTS ix_sync_pref_enabled
        ON public.user_sync_preferences(enabled)
""")
print("✓ index ix_sync_pref_enabled ready")

# Verify
cur.execute("""
    SELECT column_name, data_type FROM information_schema.columns
    WHERE table_schema='public' AND table_name='user_sync_preferences'
    ORDER BY ordinal_position
""")
cols = cur.fetchall()
print(f"\nuser_sync_preferences columns ({len(cols)}):")
for col in cols:
    print(f"  {col[0]:25s} {col[1]}")

cur.execute("SELECT COUNT(*) FROM public.user_sync_preferences")
print(f"\nrow count: {cur.fetchone()[0]}")

con.close()
print("\n=== L13 migration done ===")
