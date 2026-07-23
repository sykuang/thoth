"""One-shot prod PG migration: 加 user_push_tokens 表 (L11 pluggable push).

跑法：在可連 thoth VNet 的執行環境注入 `DATABASE_URL` 後執行：
  uv run python migrations/2026_06_22_add_user_push_tokens.py

設計（詳 backend/server/db.py 與 backend/server/push/repo.py）:
  * Multi-device-per-user, multi-provider tokens
  * UNIQUE(provider, token) — UPSERT 容忍同 token 換手機 / 換帳號
  * active=0 不刪 row — 保留 audit

Idempotent: 表/index 已存在就 skip (IF NOT EXISTS).
"""
import os
import psycopg

database_url = os.environ["DATABASE_URL"]
con = psycopg.connect(database_url)
con.autocommit = True

print("=== migration: add user_push_tokens table (L11) ===\n")

cur = con.cursor()

# Server-level table 放在 public schema
cur.execute("""
    CREATE TABLE IF NOT EXISTS public.user_push_tokens (
        id           BIGSERIAL PRIMARY KEY,
        user_id      INTEGER NOT NULL,
        provider     TEXT NOT NULL,
        token        TEXT NOT NULL,
        platform     TEXT,
        device_label TEXT,
        created_at   TEXT NOT NULL,
        last_used_at TEXT NOT NULL,
        active       INTEGER NOT NULL DEFAULT 1,
        UNIQUE(provider, token)
    )
""")
print("[OK] CREATE TABLE user_push_tokens")

for idx_sql in [
    "CREATE INDEX IF NOT EXISTS ix_push_tokens_user ON public.user_push_tokens(user_id, active)",
    "CREATE INDEX IF NOT EXISTS ix_push_tokens_last_used ON public.user_push_tokens(last_used_at)",
]:
    cur.execute(idx_sql)
    print(f"[OK] {idx_sql}")

# 驗證
cur.execute("""
    SELECT column_name, data_type FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'user_push_tokens'
    ORDER BY ordinal_position
""")
print("\nuser_push_tokens columns:")
for r in cur.fetchall():
    print(f"  {r[0]:<14} {r[1]}")

cur.execute("""
    SELECT indexname FROM pg_indexes
    WHERE schemaname = 'public' AND tablename = 'user_push_tokens'
    ORDER BY indexname
""")
print("\nuser_push_tokens indexes:")
for r in cur.fetchall():
    print(f"  {r[0]}")

print("\n=== done ===")
con.close()
