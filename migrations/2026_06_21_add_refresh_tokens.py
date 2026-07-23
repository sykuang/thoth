"""One-shot prod PG migration: 加 refresh_tokens 表 (L9 token rotation).

跑法：在可連 thoth VNet 的執行環境注入 `DATABASE_URL` 後執行：
  uv run python migrations/2026_06_21_add_refresh_tokens.py

設計（詳 backend/server/db.py 與 backend/server/refresh_tokens.py）:
  * Refresh token rotation chain + reuse detection
  * token_hash = sha256(raw_token)；DB 永遠不存明文
  * family_id (uuid) — 同 chain 共用，reuse 偵測時整批 revoke
  * replaced_by — chain pointer，audit 用

Idempotent: 表/index 已存在就 skip (IF NOT EXISTS).
"""
import os
import psycopg

database_url = os.environ["DATABASE_URL"]
con = psycopg.connect(database_url)
con.autocommit = True

print("=== migration: add refresh_tokens table (L9) ===\n")

cur = con.cursor()

# Server-level table 放在 public schema (跟 users / bank_accounts 同個 schema)
cur.execute("""
    CREATE TABLE IF NOT EXISTS public.refresh_tokens (
        id          BIGSERIAL PRIMARY KEY,
        user_id     INTEGER NOT NULL,
        token_hash  TEXT NOT NULL UNIQUE,
        family_id   TEXT NOT NULL,
        issued_at   TEXT NOT NULL,
        expires_at  TEXT NOT NULL,
        revoked_at  TEXT,
        replaced_by TEXT,
        user_agent  TEXT,
        ip_address  TEXT
    )
""")
print("[OK] CREATE TABLE refresh_tokens")

for idx_sql in [
    "CREATE INDEX IF NOT EXISTS ix_refresh_user ON public.refresh_tokens(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_refresh_family ON public.refresh_tokens(family_id)",
    "CREATE INDEX IF NOT EXISTS ix_refresh_expires ON public.refresh_tokens(expires_at)",
]:
    cur.execute(idx_sql)
    print(f"[OK] {idx_sql}")

# 驗證
cur.execute("""
    SELECT column_name, data_type FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'refresh_tokens'
    ORDER BY ordinal_position
""")
print("\nrefresh_tokens columns:")
for r in cur.fetchall():
    print(f"  {r[0]:<14} {r[1]}")

cur.execute("""
    SELECT indexname FROM pg_indexes
    WHERE schemaname = 'public' AND tablename = 'refresh_tokens'
    ORDER BY indexname
""")
print("\nrefresh_tokens indexes:")
for r in cur.fetchall():
    print(f"  {r[0]}")

print("\n=== done ===")
con.close()
