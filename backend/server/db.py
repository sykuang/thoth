"""Server-mode DB connection + schema (Phase 0 → Phase 9 portable backend).

Server-mode DB 連線 + schema。

Phase 9 (2026-06-15): 加 `DB_BACKEND` env switch — `sqlite`（預設）或 `postgres`。
SQL 全用 portable subset:
  - placeholder `?` 透過 q() 轉成 `%s` 給 psycopg
  - timestamp 用 Python `now_iso()` 不用 SQL `strftime(...)`
  - `INSERT OR IGNORE` 改 `INSERT ... ON CONFLICT DO NOTHING`（兩邊都吃）
  - `INSERT ... RETURNING id` 取代 `cur.lastrowid`（兩邊都吃）
  - column-exists check 透過 `_columns()` helper（SQLite PRAGMA / PG information_schema）

當前 schema：
  - users              — email + bcrypt password_hash
  - bank_accounts      — (user_id, bank, label) 唯一鍵，一個 user 同銀行可多帳號 [L5-1]
  - bank_credentials   — 舊 schema (user_id, bank, field_name)；保留做 migration 安全網
  - bank_credentials_v2 — 新 schema (account_id, field_name) 唯一鍵，存 Fernet 密文 [L5-1]
  - sync_jobs          — Phase 1 sync_runner；L5-1 加 account_id
  - category_rules     — Phase 5.1（Phase 8.1 加 subcategory，8.3 加 auto_excluded）
  - user_preferences   — Phase 6（JSON payload）

Env:
  - DB_BACKEND         — `sqlite`（預設）或 `postgres`
  - DATABASE_URL       — PG only: `postgresql://user:pass@host:5432/dbname`
  - BANK_DATA_ROOT     — SQLite only: server.sqlite 根目錄（test 用 tmp_path）
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, UTC
from pathlib import Path
from typing import Any
from collections.abc import Iterator

# ---------------------------------------------------------------------------
# Backend switch
# ---------------------------------------------------------------------------

DB_BACKEND = os.environ.get("DB_BACKEND", "sqlite").lower()
if DB_BACKEND not in ("sqlite", "postgres"):
    raise RuntimeError(
        f"DB_BACKEND={DB_BACKEND!r} 未支援；可選 'sqlite' (預設) 或 'postgres'",
    )

# Schema-ensure run-once latch — 解 PG concurrent DDL deadlock
# 見 get_conn() docstring
import threading as _threading
_schema_lock = _threading.Lock()
_schema_ensured = False

# PostgreSQL connection pool — lazy-created so module import doesn't DNS-resolve.
# Azure PG / ACA 會偶發 DNS / transient connectivity error；pool + connect retry
# 把短暫抖動吸收在 server side，不直接變成 user-facing 500。
_pg_pool_lock = _threading.Lock()
_pg_pool: Any | None = None
_PG_CONNECT_ATTEMPTS = int(os.environ.get("PG_CONNECT_ATTEMPTS", "4"))
_PG_CONNECT_BASE_DELAY = float(os.environ.get("PG_CONNECT_BASE_DELAY", "0.25"))
_PG_POOL_MIN_SIZE = int(os.environ.get("PG_POOL_MIN_SIZE", "1"))
_PG_POOL_MAX_SIZE = int(os.environ.get("PG_POOL_MAX_SIZE", "4"))
_PG_POOL_TIMEOUT = float(os.environ.get("PG_POOL_TIMEOUT", "10"))
_PG_POOL_MAX_LIFETIME = float(os.environ.get("PG_POOL_MAX_LIFETIME", "1800"))
_PG_POOL_MAX_IDLE = float(os.environ.get("PG_POOL_MAX_IDLE", "300"))
_PG_POOL_RECONNECT_TIMEOUT = float(os.environ.get("PG_POOL_RECONNECT_TIMEOUT", "60"))

if DB_BACKEND == "postgres":
    try:
        import psycopg
        from psycopg.errors import UniqueViolation as _IntegrityError
        from psycopg_pool import ConnectionPool
    except ImportError as e:
        raise RuntimeError(
            "DB_BACKEND=postgres 需要安裝 psycopg[binary,pool]: "
            "uv add 'psycopg[binary,pool]>=3.2'",
        ) from e

    IntegrityError = _IntegrityError
    _PARAM = "%s"
    _BLOB_TYPE = "BYTEA"
    _PK_TYPE = "BIGSERIAL PRIMARY KEY"  # PG 自動遞增
else:
    IntegrityError = sqlite3.IntegrityError
    _PARAM = "?"
    _BLOB_TYPE = "BLOB"
    _PK_TYPE = "INTEGER PRIMARY KEY AUTOINCREMENT"


def q(sql: str) -> str:
    """轉 `?` placeholder 到目前 backend style（PG `%s` / SQLite `?`）。

    rules: callsite 永遠寫 `?` placeholder，q() 在執行前轉成 backend native。
    safe because: 本專案 SQL 沒有 hard-coded `?` 在 string literal 內。

    PG only: 也要 escape SQL string literal 內的 `%` 字元為 `%%`（psycopg pyformat
    paramstyle 對裸 `%` 字元敏感，會把 `LIKE '%T%'` 當作未知 format directive 而炸）。
    """
    if _PARAM == "?":
        return sql
    # PG: 先 escape `%` → `%%`（避免 psycopg 把 SQL literal 內的 `%T%` 當 placeholder），
    # 再把 `?` 換成 `%s`
    return sql.replace("%", "%%").replace("?", "%s")


def now_iso() -> str:
    """ISO 8601 UTC 字串 with millisecond precision，兩邊 backend 都能存。

    Format: `YYYY-MM-DDTHH:MM:SS.fffZ`（跟舊 SQLite strftime('%Y-%m-%dT%H:%M:%fZ', 'now') 對齊）
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{datetime.now(UTC).microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# Schema (portable DDL — SQLite + PostgreSQL 都吃)
# ---------------------------------------------------------------------------

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS users (
    id {_PK_TYPE},
    email          TEXT NOT NULL UNIQUE,
    password_hash  TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

-- Phase L5-1: 一個 user 同銀行可以有多組帳密 (主帳 / 老婆 / 公司 ...)
CREATE TABLE IF NOT EXISTS bank_accounts (
    id {_PK_TYPE},
    user_id        INTEGER NOT NULL,
    bank           TEXT NOT NULL,
    label          TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE(user_id, bank, label)
);
CREATE INDEX IF NOT EXISTS ix_accounts_user_bank
    ON bank_accounts(user_id, bank);

-- 舊 schema (Phase 0~L4)。L5-1 起停用, 留作 migration 安全網
CREATE TABLE IF NOT EXISTS bank_credentials (
    id {_PK_TYPE},
    user_id        INTEGER NOT NULL,
    bank           TEXT NOT NULL,
    field_name     TEXT NOT NULL,
    encrypted_val  {_BLOB_TYPE} NOT NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE(user_id, bank, field_name)
);

-- Phase L5-1: cred 改掛 account_id, 一個 account 一份欄位
CREATE TABLE IF NOT EXISTS bank_credentials_v2 (
    id {_PK_TYPE},
    account_id     INTEGER NOT NULL REFERENCES bank_accounts(id) ON DELETE CASCADE,
    field_name     TEXT NOT NULL,
    encrypted_val  {_BLOB_TYPE} NOT NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE(account_id, field_name)
);
CREATE INDEX IF NOT EXISTS ix_creds2_account
    ON bank_credentials_v2(account_id);

CREATE TABLE IF NOT EXISTS sync_jobs (
    id {_PK_TYPE},
    user_id        INTEGER NOT NULL,
    bank           TEXT NOT NULL,
    account_id     INTEGER,
    status         TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    started_at     TEXT,
    finished_at    TEXT,
    error_msg      TEXT,
    result_summary TEXT,
    batch_id       INTEGER
);
-- ix_sync_jobs_batch: 移到 _ensure_schema ALTER 補欄位之後建,
-- 避免老 sqlite (sync_jobs 已存在但沒 batch_id 欄) 跑 CREATE INDEX 炸 "no such column"

-- 2026-06-23 (sync-all batch summary): 聚合多 job 推「同步全部完成」一則,
-- 取代每家銀行各推一則 sync_done (12 家 = 12 則噪音).
--   * user_id: 哪個 user 觸發
--   * total_jobs: batch 內排了幾個 job (= 該 user has_creds 的 account 數)
--   * kind: 'manual_all' (UI POST /sync/all) | 'scheduled_all' (APScheduler 自動)
--   * notified_at: atomic CAS sentinel — 最後一個 job 收尾時 UPDATE ... WHERE
--     notified_at IS NULL RETURNING ..., race 輸的拿不到 row 就 skip 推播.
--     SQLite ≥3.35 + PG 都吃 RETURNING.
--   * finished_at: 全 job done|failed 後寫一次 (跟 notified_at 同 transaction).
CREATE TABLE IF NOT EXISTS sync_batches (
    id {_PK_TYPE},
    user_id     INTEGER NOT NULL,
    total_jobs  INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    finished_at TEXT,
    notified_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_sync_batches_user
    ON sync_batches(user_id);

-- Phase 5.1: per-user 分類規則
CREATE TABLE IF NOT EXISTS category_rules (
    id {_PK_TYPE},
    user_id        INTEGER NOT NULL,
    name           TEXT NOT NULL,
    pattern        TEXT NOT NULL,
    category       TEXT NOT NULL,
    subcategory    TEXT,
    priority       INTEGER NOT NULL DEFAULT 100,
    enabled        INTEGER NOT NULL DEFAULT 1,
    auto_excluded  INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rules_user_priority
    ON category_rules(user_id, priority DESC, enabled);

-- Phase 6: per-user display preferences (JSON 1-row-per-user)
CREATE TABLE IF NOT EXISTS user_preferences (
    user_id      INTEGER PRIMARY KEY,
    payload_json TEXT NOT NULL DEFAULT '{{}}',
    updated_at   TEXT NOT NULL
);

-- Phase L10 (2026-06-20): 信用卡自動扣繳帳號設定 (per-user, per-card-bank).
-- 設計：
--   * card_bank: 信用卡所屬銀行 ('cathay', 'ctbc', 'sinopac', ...)
--   * account_bank + account_no: 扣繳戶 (跨銀行允許，例 CTBC 卡可用永豐戶)
--   * UI 鐵則 (G4): account_no 必須是該 bank.sqlite accounts 表中 currency='TWD' 的活儲戶
--   * 一個 user 一個 bank 一筆設定 (A2 per-bank)，銀行底下所有卡共用同一扣繳戶
--   * UI picker 過濾掉外幣戶 / excluded 戶 / 貸款型 (product_type IN loan/mortgage/credit_line)
--   * Dashboard 用：no setting + due_date ≤ 3 天 → 'no_account' 提醒；
--     有 setting + balance < bill_due_amount → 'insufficient' 提醒
CREATE TABLE IF NOT EXISTS card_auto_debit_settings (
    user_id      INTEGER NOT NULL,
    card_bank    TEXT NOT NULL,
    account_bank TEXT NOT NULL,
    account_no   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (user_id, card_bank)
);
CREATE INDEX IF NOT EXISTS ix_auto_debit_user
    ON card_auto_debit_settings(user_id);

-- Phase L9 (2026-06-21): refresh tokens (rotation + reuse detection).
-- 設計：
--   * token_hash: sha256(raw_token) — DB 永遠不存明文，洩漏 dump 也無法登入
--   * family_id: 一條 rotation chain 共用一個 family；偵測到 revoked token 再被用
--                直接 revoke 整個 family（OAuth 2.0 token-reuse detection）
--   * replaced_by: 指向 rotation 後的新 token_hash，建 chain
--   * user_agent / ip_address: audit 用，方便日後做 device list
CREATE TABLE IF NOT EXISTS refresh_tokens (
    id          {_PK_TYPE},
    user_id     INTEGER NOT NULL,
    token_hash  TEXT NOT NULL UNIQUE,
    family_id   TEXT NOT NULL,
    issued_at   TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    revoked_at  TEXT,
    replaced_by TEXT,
    user_agent  TEXT,
    ip_address  TEXT
);
CREATE INDEX IF NOT EXISTS ix_refresh_user
    ON refresh_tokens(user_id);
CREATE INDEX IF NOT EXISTS ix_refresh_family
    ON refresh_tokens(family_id);
CREATE INDEX IF NOT EXISTS ix_refresh_expires
    ON refresh_tokens(expires_at);

-- Phase L11 (2026-06-22): pluggable push notification tokens (multi-device, multi-provider).
-- 設計:
--   * 一個 user 多 device (Kphone / iPad / 老婆手機 ...), 每 device 一筆 row
--   * provider 欄區分 — 同 user 可同時有 apns token + webhook URL
--   * UNIQUE(provider, token) — 同 token 在不同 user 之間 conflict, 走 UPSERT 改 user_id
--     (場景: 手機轉手 / 換帳號)
--   * active=0 但不刪 row — 保留 audit (debug 「為什麼 user 收不到」)
--   * platform: 'ios' | 'android' | 'web' | 'desktop' (給未來 FCM / web-push 用)
CREATE TABLE IF NOT EXISTS user_push_tokens (
    id           {_PK_TYPE},
    user_id      INTEGER NOT NULL,
    provider     TEXT NOT NULL,
    token        TEXT NOT NULL,
    platform     TEXT,
    device_label TEXT,
    created_at   TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE(provider, token)
);
CREATE INDEX IF NOT EXISTS ix_push_tokens_user
    ON user_push_tokens(user_id, active);
CREATE INDEX IF NOT EXISTS ix_push_tokens_last_used
    ON user_push_tokens(last_used_at);

-- SnapTrade：server-side app credentials + per-user encrypted SnapTrade identity。
-- 所有 portfolio rows 明確帶 user_id；provider secrets 永不下放 frontend。
CREATE TABLE IF NOT EXISTS snaptrade_users (
    user_id               INTEGER PRIMARY KEY,
    snaptrade_user_id     TEXT NOT NULL UNIQUE,
    encrypted_user_secret {_BLOB_TYPE} NOT NULL,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snaptrade_locks (
    user_id      INTEGER NOT NULL,
    operation    TEXT NOT NULL,
    owner_token  TEXT NOT NULL,
    acquired_at  TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    PRIMARY KEY (user_id, operation)
);

CREATE TABLE IF NOT EXISTS brokerage_accounts (
    user_id               INTEGER NOT NULL,
    provider              TEXT NOT NULL,
    provider_account_id   TEXT NOT NULL,
    name                   TEXT NOT NULL,
    number                 TEXT,
    institution_name       TEXT NOT NULL,
    brokerage_slug         TEXT,
    balance_total          TEXT,
    balance_currency       TEXT,
    activities_supported INTEGER NOT NULL DEFAULT 0,
    holdings_unavailable INTEGER NOT NULL DEFAULT 0,
    transactions_last_successful_sync TEXT,
    transactions_first_transaction_date TEXT,
    synced_at              TEXT NOT NULL,
    PRIMARY KEY (user_id, provider, provider_account_id)
);
CREATE INDEX IF NOT EXISTS ix_brokerage_accounts_user
    ON brokerage_accounts(user_id, provider);

CREATE TABLE IF NOT EXISTS brokerage_balances (
    user_id              INTEGER NOT NULL,
    provider             TEXT NOT NULL,
    provider_account_id  TEXT NOT NULL,
    currency             TEXT NOT NULL,
    cash                 TEXT,
    buying_power         TEXT,
    synced_at            TEXT NOT NULL,
    PRIMARY KEY (user_id, provider, provider_account_id, currency)
);

CREATE TABLE IF NOT EXISTS brokerage_positions (
    user_id              INTEGER NOT NULL,
    provider             TEXT NOT NULL,
    provider_account_id  TEXT NOT NULL,
    provider_symbol_id   TEXT NOT NULL,
    symbol               TEXT NOT NULL,
    description          TEXT,
    asset_type           TEXT,
    quantity             TEXT NOT NULL,
    price                TEXT,
    market_value         TEXT,
    average_cost         TEXT,
    currency             TEXT,
    synced_at            TEXT NOT NULL,
    PRIMARY KEY (user_id, provider, provider_account_id, provider_symbol_id)
);

CREATE TABLE IF NOT EXISTS brokerage_activities (
    user_id              INTEGER NOT NULL,
    provider             TEXT NOT NULL,
    provider_activity_id TEXT NOT NULL,
    provider_account_id  TEXT NOT NULL,
    activity_type        TEXT NOT NULL,
    trade_date           TEXT,
    settlement_date      TEXT,
    symbol               TEXT,
    description          TEXT,
    units                TEXT,
    price                TEXT,
    amount               TEXT,
    fee                  TEXT,
    currency             TEXT,
    synced_at            TEXT NOT NULL,
    PRIMARY KEY (user_id, provider, provider_account_id, provider_activity_id)
);
CREATE INDEX IF NOT EXISTS ix_brokerage_activities_user_date
    ON brokerage_activities(user_id, provider, trade_date);

-- User-maintained financial accounts. Provider snapshots stay in their own
-- authoritative stores; the canonical read model adapts both sources.
CREATE TABLE IF NOT EXISTS manual_financial_accounts (
    id                    {_PK_TYPE},
    user_id               INTEGER NOT NULL,
    product_type          TEXT NOT NULL,
    name                  TEXT NOT NULL,
    currency              TEXT NOT NULL,
    balance               TEXT NOT NULL,
    included_in_net_worth INTEGER NOT NULL DEFAULT 1,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_manual_financial_accounts_user
    ON manual_financial_accounts(user_id, updated_at DESC);

-- Manual investment journal only. Current account valuation remains the
-- authoritative net-worth input; historical trade cost is not market value.
CREATE TABLE IF NOT EXISTS manual_investment_transactions (
    id           {_PK_TYPE},
    user_id      INTEGER NOT NULL,
    account_id   INTEGER NOT NULL REFERENCES manual_financial_accounts(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    occurred_on  TEXT NOT NULL,
    symbol       TEXT,
    quantity     TEXT,
    unit_price   TEXT,
    amount       TEXT NOT NULL,
    currency     TEXT NOT NULL,
    note         TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_manual_investment_txns_account_date
    ON manual_investment_transactions(user_id, account_id, occurred_on DESC, id DESC);

-- 2026-06-23 (L13 使用者指示): 自動同步排程 — 每個 user 一個 daily schedule.
--   * user_id PK = 1 user 1 schedule (1:N to bank_accounts at fire-time)
--   * Fire 時 fan-out 該 user 全部 has_creds=true 的 account
--   * hour 0-23, minute 0-59 — daily only
--   * tz default Asia/Taipei
--   * enabled=0 vs 沒 row 兩種「停掉」, 前者保留時間值方便重啟
--   * last_run_at 給 UI 顯示「上次自動同步」, 細節點開看 sync_jobs
--   * 取代 L12 per-account 設計 (使用者「我要使用者設定一個時間給所有帳號」)
CREATE TABLE IF NOT EXISTS user_sync_preferences (
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
);
CREATE INDEX IF NOT EXISTS ix_sync_pref_enabled
    ON user_sync_preferences(enabled);

-- Phase L15 (2026-06-26): 每日信用卡繳費提醒 push dedupe.
--   * reminder_date: 以使用者時區算出的 local date (YYYY-MM-DD)，一天最多推一次同一筆
--   * key 含 payment_due_date + reason，避免同一卡隔月帳單 / 狀態改變被舊 row 擋住
--   * notified_at: 實際 claim/attempt 時間；推送本身失敗也不重複轟炸同一天
CREATE TABLE IF NOT EXISTS payment_reminder_notifications (
    id               {_PK_TYPE},
    user_id          INTEGER NOT NULL,
    card_bank        TEXT NOT NULL,
    card_no          TEXT NOT NULL,
    payment_due_date TEXT NOT NULL,
    reason           TEXT NOT NULL,
    reminder_date    TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    notified_at      TEXT NOT NULL,
    UNIQUE(user_id, card_bank, card_no, payment_due_date, reason, reminder_date)
);
CREATE INDEX IF NOT EXISTS ix_payment_reminder_notifications_user_date
    ON payment_reminder_notifications(user_id, reminder_date);
"""

# Phase L5-1 migration: 用 `ON CONFLICT DO NOTHING` 兩邊都吃
# 為每個 (user_id, bank) 建一個 label='預設' 的 account, 然後把該組 cred 全部移過去。
_MIGRATE_V1_TO_V2 = """
INSERT INTO bank_accounts (user_id, bank, label, created_at, updated_at)
SELECT DISTINCT user_id, bank, '預設',
       COALESCE(MIN(created_at), '1970-01-01T00:00:00.000Z'),
       COALESCE(MAX(updated_at), '1970-01-01T00:00:00.000Z')
FROM bank_credentials
GROUP BY user_id, bank
ON CONFLICT (user_id, bank, label) DO NOTHING;

INSERT INTO bank_credentials_v2 (account_id, field_name, encrypted_val, created_at, updated_at)
SELECT a.id, c.field_name, c.encrypted_val, c.created_at, c.updated_at
FROM bank_credentials c
JOIN bank_accounts a
  ON a.user_id = c.user_id AND a.bank = c.bank AND a.label = '預設'
ON CONFLICT (account_id, field_name) DO NOTHING;
"""

# Phase 6 timestamp migration（兩邊都吃 REPLACE / || string concat）
_MIGRATE_LEGACY_TIMESTAMPS = """
UPDATE users SET created_at = REPLACE(created_at, ' ', 'T') || '.000Z'
WHERE created_at NOT LIKE '%T%' AND created_at NOT LIKE '%Z';

UPDATE bank_accounts SET created_at = REPLACE(created_at, ' ', 'T') || '.000Z'
WHERE created_at NOT LIKE '%T%' AND created_at NOT LIKE '%Z';
UPDATE bank_accounts SET updated_at = REPLACE(updated_at, ' ', 'T') || '.000Z'
WHERE updated_at NOT LIKE '%T%' AND updated_at NOT LIKE '%Z';

UPDATE bank_credentials SET created_at = REPLACE(created_at, ' ', 'T') || '.000Z'
WHERE created_at NOT LIKE '%T%' AND created_at NOT LIKE '%Z';
UPDATE bank_credentials SET updated_at = REPLACE(updated_at, ' ', 'T') || '.000Z'
WHERE updated_at NOT LIKE '%T%' AND updated_at NOT LIKE '%Z';

UPDATE bank_credentials_v2 SET created_at = REPLACE(created_at, ' ', 'T') || '.000Z'
WHERE created_at NOT LIKE '%T%' AND created_at NOT LIKE '%Z';
UPDATE bank_credentials_v2 SET updated_at = REPLACE(updated_at, ' ', 'T') || '.000Z'
WHERE updated_at NOT LIKE '%T%' AND updated_at NOT LIKE '%Z';

UPDATE sync_jobs SET created_at = REPLACE(created_at, ' ', 'T') || '.000Z'
WHERE created_at NOT LIKE '%T%' AND created_at NOT LIKE '%Z';
UPDATE sync_jobs SET started_at = REPLACE(started_at, ' ', 'T') || '.000Z'
WHERE started_at IS NOT NULL AND started_at NOT LIKE '%T%' AND started_at NOT LIKE '%Z';
UPDATE sync_jobs SET finished_at = REPLACE(finished_at, ' ', 'T') || '.000Z'
WHERE finished_at IS NOT NULL AND finished_at NOT LIKE '%T%' AND finished_at NOT LIKE '%Z';

UPDATE category_rules SET created_at = REPLACE(created_at, ' ', 'T') || '.000Z'
WHERE created_at NOT LIKE '%T%' AND created_at NOT LIKE '%Z';
UPDATE category_rules SET updated_at = REPLACE(updated_at, ' ', 'T') || '.000Z'
WHERE updated_at NOT LIKE '%T%' AND updated_at NOT LIKE '%Z';
"""


def server_db_path() -> Path:
    """`server.sqlite` 的路徑（SQLite backend only）。

    PG backend 無此概念—DATABASE_URL 直接指定 host:port/dbname。
    """
    root = Path(os.environ.get("BANK_DATA_ROOT", "backend/data"))
    return root / "server.sqlite"


# ---------------------------------------------------------------------------
# Column-exists helper（替代 PRAGMA table_info）
# ---------------------------------------------------------------------------

def _columns(conn: Any, table: str) -> set[str]:
    """回傳 table 現有的欄位名稱 set；兩邊 backend 都可呼叫。

    Note: SQL 寫 `?` placeholder，_ConnAdapter 會自動 q() 轉成 PG `%s`；
    這裡不該手動呼叫 q()，否則會 double-encode（`?` → `%s` → `%%s` 永遠 0 placeholder）。
    """
    if DB_BACKEND == "postgres":
        cur = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ?",
            (table,),
        )
        return {r[0] for r in cur.fetchall()}
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {r[1] for r in cur.fetchall()}


def _ensure_schema(conn: Any) -> None:
    """執行 schema DDL (IF NOT EXISTS, 重複安全) + L5-1 migration。

    Note: SQLite 用 `executescript()` 一次跑多 statement；PG psycopg 用
    `execute()` 直接吃 multi-statement string（autocommit/transaction 通用）。
    """
    if DB_BACKEND == "postgres":
        # psycopg 一次 execute 多 statement 需要拆，或用 execute() 但 PG 接受 ;
        for stmt in _split_statements(_SCHEMA):
            conn.execute(stmt)
    else:
        conn.executescript(_SCHEMA)

    brokerage_cols = _columns(conn, "brokerage_accounts")
    if "holdings_unavailable" not in brokerage_cols:
        conn.execute(
            "ALTER TABLE brokerage_accounts "
            "ADD COLUMN holdings_unavailable INTEGER NOT NULL DEFAULT 0",
        )
    if "transactions_last_successful_sync" not in brokerage_cols:
        conn.execute(
            "ALTER TABLE brokerage_accounts "
            "ADD COLUMN transactions_last_successful_sync TEXT",
        )
    if "transactions_first_transaction_date" not in brokerage_cols:
        conn.execute(
            "ALTER TABLE brokerage_accounts "
            "ADD COLUMN transactions_first_transaction_date TEXT",
        )

    manual_account_cols = _columns(conn, "manual_financial_accounts")
    for obsolete_column in ("institution_name", "account_ref", "as_of"):
        if obsolete_column in manual_account_cols:
            conn.execute(
                f"ALTER TABLE manual_financial_accounts DROP COLUMN {obsolete_column}",
            )

    # 老 sync_jobs 表缺 account_id 欄位 → 補上
    cols = _columns(conn, "sync_jobs")
    if "account_id" not in cols:
        conn.execute("ALTER TABLE sync_jobs ADD COLUMN account_id INTEGER")
    # 2026-06-23: 老 sync_jobs 表缺 batch_id 欄位 → 補上 (老 row 為 NULL, 走 legacy 單則 push)
    if "batch_id" not in cols:
        conn.execute("ALTER TABLE sync_jobs ADD COLUMN batch_id INTEGER")
    # batch_id 欄位確定存在後才能建 index (老 sqlite 沒 batch_id 直接 CREATE INDEX 會炸)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_sync_jobs_batch ON sync_jobs(batch_id)",
    )

    # Phase 8.1: subcategory 欄
    rules_cols = _columns(conn, "category_rules")
    if "subcategory" not in rules_cols:
        conn.execute("ALTER TABLE category_rules ADD COLUMN subcategory TEXT")

    # Phase 8.3: auto_excluded 欄
    if "auto_excluded" not in rules_cols:
        conn.execute(
            "ALTER TABLE category_rules ADD COLUMN auto_excluded INTEGER NOT NULL DEFAULT 0",
        )

    # L5-1 migration: v1 cred → v2
    if DB_BACKEND == "postgres":
        for stmt in _split_statements(_MIGRATE_V1_TO_V2):
            conn.execute(stmt)
        for stmt in _split_statements(_MIGRATE_LEGACY_TIMESTAMPS):
            conn.execute(stmt)
    else:
        conn.executescript(_MIGRATE_V1_TO_V2)
        conn.executescript(_MIGRATE_LEGACY_TIMESTAMPS)
    conn.commit()


def _split_statements(script: str) -> list[str]:
    """切多 statement string 成 list（給 psycopg 用，跳過空行與註解）。

    Note (2026-06-16): 不能直接 split(";")，因為 SQL 註解 (`-- ...`) 內可能含
    `;` 字元（例如「重複灌; (b) 手動加的不會撞 ...」），會被誤切成第二段而把
    `(b) ...` 當 SQL 送進 PG，炸 SyntaxError。
    修法：先把每行的 `-- comment` 部分砍掉（保留行內 SQL），再 split(";")。
    限制：純行內 `'literal ; in string'` 仍不保護（本專案 SQL 沒這 pattern, 真有
    時要升級成 proper SQL parser）。
    """
    # 1) 先逐行剝掉 -- 之後的註解 (避免註解 ; 被當 statement 邊界)
    sanitized_lines: list[str] = []
    for raw_line in script.split("\n"):
        # 註解符號前段才是 SQL；註解 (`-- xxx`) 部分整段砍
        idx = raw_line.find("--")
        sql_part = raw_line if idx < 0 else raw_line[:idx]
        if sql_part.strip():
            sanitized_lines.append(sql_part)
    sanitized = "\n".join(sanitized_lines)

    # 2) split(";")
    out: list[str] = []
    for stmt in sanitized.split(";"):
        s = stmt.strip()
        if not s:
            continue
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# Connection wrapper
# ---------------------------------------------------------------------------

def _database_url() -> str:
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        raise RuntimeError(
            "DB_BACKEND=postgres 需要設 DATABASE_URL env "
            "(format: postgresql://user:***@host:5432/dbname)",
        )
    return dsn


def _get_pg_pool() -> Any:
    """Return process-global psycopg pool, creating it lazily.

    `open=False` avoids DNS lookup at import time. The first `connection()` call
    opens connections and is wrapped by `_pg_connection_with_retry()`.
    """
    global _pg_pool
    if _pg_pool is not None:
        return _pg_pool
    with _pg_pool_lock:
        if _pg_pool is None:
            pool = ConnectionPool(
                _database_url(),
                min_size=_PG_POOL_MIN_SIZE,
                max_size=_PG_POOL_MAX_SIZE,
                open=False,
                timeout=_PG_POOL_TIMEOUT,
                max_lifetime=_PG_POOL_MAX_LIFETIME,
                max_idle=_PG_POOL_MAX_IDLE,
                reconnect_timeout=_PG_POOL_RECONNECT_TIMEOUT,
                check=ConnectionPool.check_connection,
            )
            # psycopg_pool `open=False` avoids import-time DNS lookup, but the
            # pool must still be explicitly opened before `connection()`.
            # wait=False keeps open non-blocking; connection checkout below is
            # where transient DNS/connect errors get retried.
            pool.open(wait=False)
            _pg_pool = pool
    return _pg_pool


@contextmanager
def _pg_connection_with_retry() -> Iterator[Any]:
    """Yield a raw psycopg connection from pool with small transient retry.

    Azure PostgreSQL Flexible Server / ACA can occasionally fail DNS resolution
    or connection establishment for a few seconds. Retry only the pool checkout /
    connection acquisition. SQL execution errors still surface normally.
    """
    attempts = max(1, _PG_CONNECT_ATTEMPTS)
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        cm = _get_pg_pool().connection(timeout=_PG_POOL_TIMEOUT)
        try:
            raw = cm.__enter__()
        except psycopg.OperationalError as e:
            last_exc = e
            if attempt >= attempts:
                break
            delay = _PG_CONNECT_BASE_DELAY * (2 ** (attempt - 1))
            print(
                f"[db][postgres] transient connect failure "
                f"attempt {attempt}/{attempts}: {e}; retry in {delay:.2f}s",
                flush=True,
            )
            time.sleep(delay)
            continue

        try:
            try:
                yield raw
            except BaseException as e:
                cm.__exit__(type(e), e, e.__traceback__)
                raise
            else:
                cm.__exit__(None, None, None)
            return
        finally:
            # __exit__ is called in the inner try/except/else above so it receives
            # the correct exception info for commit/rollback. This outer finally is
            # intentionally empty; it documents that raw must always be returned via cm.
            pass

    assert last_exc is not None
    raise last_exc


class _ConnAdapter:
    """psycopg 連線的薄 wrapper，讓 callsite 寫 `conn.execute(?)` 跟 SQLite 一致。

    主要負責：
    1. `execute(sql, params)` 自動 q() 轉 placeholder
    2. `executescript(script)` 切 statements 後逐一 execute
    3. transparent close/commit
    4. 暴露 `lastrowid` for legacy callsite（PG 走 RETURNING 即可，不該用 lastrowid，
       但保留 attribute 為 None 不爆）
    """

    def __init__(self, raw_conn: Any, *, close_on_close: bool = True) -> None:
        self._conn = raw_conn
        self._close_on_close = close_on_close

    def execute(self, sql: str, params: tuple = ()) -> Any:
        # psycopg cursor 需透過 .cursor() 取，但 psycopg3 Connection 直接 .execute() 也行
        return self._conn.execute(q(sql), params)

    def executescript(self, script: str) -> None:
        for stmt in _split_statements(script):
            self._conn.execute(stmt)

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        if self._close_on_close:
            self._conn.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


@contextmanager
def get_conn() -> Iterator[Any]:
    """yield 一個已建表 + foreign_keys ON 的連線；with-block 結束自動 commit/close。

    Schema DDL（CREATE TABLE IF NOT EXISTS + ALTER + seed INSERT）只在「process
    內第一次呼叫」跑一次，之後 skip。理由：
    - SQLite 單 file lock，重跑無痛
    - PG row-level lock，多 worker concurrent 跑相同 DDL + INSERT 會 deadlock
      （實測 [[azure-container-apps-pg-flexible-thoth-deploy]] Phase 8 — frontend
      多 query 並發 → 5 個 worker 同時跑 _ensure_schema → INSERT INTO bank_accounts
      互卡 → 隨機 500 + frontend 顯示 Load failed）
    - DDL 是 idempotent 但「跑」本身在 PG 會拿 lock；只跑一次最安全

    SQLite: yield `sqlite3.Connection`（原生介面）
    PostgreSQL: yield `_ConnAdapter`（psycopg connection wrap，介面相容）
    """
    global _schema_ensured
    raw_cm: Any | None = None
    if DB_BACKEND == "postgres":
        raw_cm = _pg_connection_with_retry()
        raw = raw_cm.__enter__()
        conn: Any = _ConnAdapter(raw, close_on_close=False)
    else:
        db_path = server_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute("PRAGMA foreign_keys=ON")

    # Schema ensure 策略：
    # - SQLite: 每次跑（為了 test 能 trigger v1→v2 migration）。SQLite 單 file
    #   lock，重跑無痛
    # - PG:    只跑一次（latch）。PG 多 worker concurrent DDL + seed INSERT 會
    #   row-lock deadlock（[[azure-container-apps-pg-flexible-thoth-deploy]] Phase 8
    #   發現：frontend 多 query 並發 → workers concurrent _ensure_schema → INSERT
    #   bank_accounts 互卡 → 隨機 500 + frontend 顯示 Load failed）
    global _schema_ensured
    if DB_BACKEND == "sqlite":
        _ensure_schema(conn)
    elif not _schema_ensured:
        with _schema_lock:
            if not _schema_ensured:
                _ensure_schema(conn)
                _schema_ensured = True
    try:
        yield conn
        conn.commit()
    except BaseException as e:
        if DB_BACKEND == "postgres":
            assert raw_cm is not None
            raw_cm.__exit__(type(e), e, e.__traceback__)
        else:
            conn.close()
        raise
    else:
        if DB_BACKEND == "postgres":
            assert raw_cm is not None
            raw_cm.__exit__(None, None, None)
        else:
            conn.close()


# ===========================================================================
# Public facade — application code (routers, store, etc.) SHOULD import
# everything DB-related from here, not directly from `sqlite3` / `psycopg`.
#
# This is the single seam between "application" and "storage backend".
# A linter rule (tools/check_db_imports.py) enforces that only db.py and
# bank_pg.py touch sqlite3 / psycopg directly.
#
# Why: 2026-06-17 three production bugs all came from routers reaching
# into sqlite3 directly:
#   1. row[1] positional access — works on SQLite tuples, IndexError on PG
#   2. raw sqlite3.connect() — bypassed PG dispatcher entirely
#   3. except sqlite3.IntegrityError — does not match psycopg.UniqueViolation
# See ~/wiki/concepts/thoth-dual-backend-audit-2026-06-17.md
# ===========================================================================

# --- Connection / cursor types (for type hints) ---
# Use these instead of `sqlite3.Connection` / `sqlite3.Row` in router signatures.
# They are intentionally `Any` because the actual class differs per backend
# (`sqlite3.Connection` vs `_ConnAdapter`/`bank_pg.Connection`).
# Type hint purpose only — runtime polymorphism via duck typing.
Connection = Any
Cursor = Any
Row = Any  # row supports both row["col"] and (for SQLite tuples) row[0]


# --- Exception classes (portable across SQLite + PG) ---
# IntegrityError already declared above (line ~78).
# OperationalError is the "table/column missing / bank has no data yet" error.
# bank_pg.py re-raises psycopg.UndefinedTable/UndefinedColumn as
# sqlite3.OperationalError, so the SQLite class catches both backends.
OperationalError = sqlite3.OperationalError


# Phase C (2026-06-17) — multi-user row-level isolation safety net.
# 在 fixture/raw DB 不走 BankStore._migrate 的場景 (tests/手動 import legacy db),
# open_bank_conn 第一次拿到 connection 時自動補 user_id column + 預設 user_id=1
# (legacy single-user backfill, 對齊 BankStore._migrate)。
# Per-process cache by absolute path: 避免每次 query 都 PRAGMA round-trip。
_PHASE_C_MIGRATED: set[str] = set()
_PHASE_C_TABLES = (
    "twd_transactions",
    "card_billed_txns",
    "card_pending_txns",
    "balance_history",
    "accounts",
    "cards",
    "daily_metrics",
    "sync_log",
)
# C-pk: 4 張 PK 表升級用 composite UNIQUE INDEX (SQLite 不支援 ALTER PK)。
# 對齊 BankStore._migrate 末段的 unique index 升級邏輯,
# 讓 INSERT...ON CONFLICT(user_id, ...) 在 legacy DB 也 work。
_PHASE_C_PK_INDEXES = (
    ("balance_history", "ux_balance_history_user_snap", "(user_id, snapshot_date)"),
    ("accounts", "ux_accounts_user_no", "(user_id, account_no)"),
    ("cards", "ux_cards_user_no", "(user_id, card_no)"),
    ("daily_metrics", "ux_daily_metrics_user_snap_cat", "(user_id, snapshot_date, category)"),
    ("twd_transactions", "ux_twd_dedup", "(user_id, dedup_key)"),
    ("card_billed_txns", "ux_card_billed_dedup", "(user_id, dedup_key)"),
)


def _ensure_phase_c_user_id(con: sqlite3.Connection, cache_key: str) -> None:
    """Idempotent: 每張 bank 表如缺 user_id column 就補 + backfill = 1.

    純 SQLite (BankStore 已正式做; 這是 raw fixture 兜底 net)。
    PG backend 由 bank_pg 處理，這條 path 只跑在 SQLite。
    cache_key 用 db_path string，per-process set 去重。

    C-pk (2026-06-17): 同時補 composite UNIQUE INDEX, 對齊 BankStore._migrate
    末段邏輯, 讓 router-side endpoints 對 legacy DB 跑 INSERT 不爆。
    """
    if cache_key in _PHASE_C_MIGRATED:
        return
    try:
        existing = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.OperationalError:
        _PHASE_C_MIGRATED.add(cache_key)
        return
    for tbl in _PHASE_C_TABLES:
        if tbl not in existing:
            continue
        try:
            cols = {r[1] for r in con.execute(f"PRAGMA table_info({tbl})")}
            if "user_id" not in cols:
                con.execute(f"ALTER TABLE {tbl} ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
                con.execute(f"UPDATE {tbl} SET user_id = 1 WHERE user_id IS NULL OR user_id = 0")
        except sqlite3.OperationalError:
            continue
    # C-pk: 補 composite UNIQUE INDEX (idempotent CREATE IF NOT EXISTS)
    for tbl, idx, cols in _PHASE_C_PK_INDEXES:
        if tbl not in existing:
            continue
        try:
            con.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {idx} ON {tbl}{cols}")
        except sqlite3.OperationalError:
            # 老 DB 可能有 duplicate row 跑不過 — 不擋 read path
            continue
    try:
        con.commit()
    except sqlite3.OperationalError:
        pass
    _PHASE_C_MIGRATED.add(cache_key)
# --- Bank-side connection ---
def open_bank_conn(bank: str) -> Connection | None:
    """Open a connection to one bank's per-bank data store.

    SQLite mode: opens BANK_DATA_ROOT/<bank>.sqlite (returns None if file
    does not exist, signalling "bank has no data yet").

    PostgreSQL mode: opens a psycopg connection scoped to schema
    bank_<bank> via the bank_pg adapter.

    Always go through this function — never call `sqlite3.connect()` directly.

    Phase C (2026-06-17): SQLite path 自動 ensure user_id columns (raw fixture
    兜底, BankStore._migrate 已正式做)。
    """
    from backend.core import bank_data  # local import to avoid cycle
    con = bank_data.open_bank_db(bank)
    if con is None:
        return None
    # 只對 raw sqlite3.Connection 做 Phase C 兜底; PG adapter 跳過
    if isinstance(con, sqlite3.Connection):
        # cache_key 用 absolute resolved path, 避免 BANK_DATA_ROOT 空字串或
        # 不同 root 但同 bank 名互相 cache hit (Phase C-review Warning #2)。
        # fallback 用 connection 物件 id (process 內唯一) — 至少不會跨 conn collision。
        import os
        root_env = os.environ.get("BANK_DATA_ROOT", "")
        if root_env:
            from pathlib import Path
            cache_key = str((Path(root_env) / f"{bank}.sqlite").resolve())
        else:
            # BANK_DATA_ROOT 未設 (production fallback path), 用 connection id
            cache_key = f"conn:{id(con)}:{bank}"
        _ensure_phase_c_user_id(con, cache_key)
    return con


_SNAPTRADE_PROVIDER = "snaptrade"


def snaptrade_acquire_lock(
    user_id: int,
    operation: str,
    owner_token: str,
    acquired_at: str,
    expires_at: str,
) -> bool:
    with get_conn() as conn:
        result = conn.execute(
            "INSERT INTO snaptrade_locks "
            "(user_id, operation, owner_token, acquired_at, expires_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (user_id, operation) DO UPDATE SET "
            "owner_token=excluded.owner_token, acquired_at=excluded.acquired_at, "
            "expires_at=excluded.expires_at WHERE snaptrade_locks.expires_at <= excluded.acquired_at",
            (user_id, operation, owner_token, acquired_at, expires_at),
        )
        return result.rowcount == 1


def snaptrade_release_lock(user_id: int, operation: str, owner_token: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM snaptrade_locks "
            "WHERE user_id = ? AND operation = ? AND owner_token = ?",
            (user_id, operation, owner_token),
        )


def snaptrade_get_credentials(user_id: int) -> tuple[str, bytes] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT snaptrade_user_id, encrypted_user_secret "
            "FROM snaptrade_users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    encrypted = row[1].tobytes() if isinstance(row[1], memoryview) else bytes(row[1])
    return str(row[0]), encrypted


def _snaptrade_lock_is_current(
    conn: Connection,
    user_id: int,
    operation: str,
    owner_token: str,
    now: str,
) -> bool:
    if DB_BACKEND == "postgres":
        sql = (
            "SELECT owner_token, expires_at FROM snaptrade_locks "
            "WHERE user_id = ? AND operation = ? FOR UPDATE"
        )
    else:
        conn.execute("BEGIN IMMEDIATE")
        sql = (
            "SELECT owner_token, expires_at FROM snaptrade_locks "
            "WHERE user_id = ? AND operation = ?"
        )
    row = conn.execute(sql, (user_id, operation)).fetchone()
    return row is not None and row[0] == owner_token and row[1] > now


def snaptrade_insert_credentials(
    user_id: int,
    snaptrade_user_id: str,
    encrypted_user_secret: bytes,
    now: str,
    *,
    lock_owner: str,
) -> bool:
    with get_conn() as conn:
        if not _snaptrade_lock_is_current(
            conn, user_id, "registration", lock_owner, now,
        ):
            return False
        conn.execute(
            "INSERT INTO snaptrade_users "
            "(user_id, snaptrade_user_id, encrypted_user_secret, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, snaptrade_user_id, encrypted_user_secret, now, now),
        )
    return True


def snaptrade_replace_snapshot(
    user_id: int,
    accounts: list[dict[str, Any]],
    balances: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    activities: list[dict[str, Any]],
    *,
    lock_owner: str,
    lock_now: str,
) -> bool:
    with get_conn() as conn:
        if not _snaptrade_lock_is_current(
            conn, user_id, "sync", lock_owner, lock_now,
        ):
            return False
        params = (user_id, _SNAPTRADE_PROVIDER)
        for table in (
            "brokerage_activities",
            "brokerage_balances",
            "brokerage_positions",
            "brokerage_accounts",
        ):
            conn.execute(
                f"DELETE FROM {table} WHERE user_id = ? AND provider = ?",
                params,
            )
        for row in accounts:
            conn.execute(
                "INSERT INTO brokerage_accounts "
                "(user_id, provider, provider_account_id, name, number, institution_name, "
                "brokerage_slug, balance_total, balance_currency, activities_supported, "
                "holdings_unavailable, transactions_last_successful_sync, "
                "transactions_first_transaction_date, synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id, _SNAPTRADE_PROVIDER, row["id"], row["name"], row["number"],
                    row["institution_name"], row["brokerage_slug"], row["balance_total"],
                    row["balance_currency"], int(row["activities_supported"]),
                    int(row["holdings_unavailable"]),
                    row["transactions_last_successful_sync"],
                    row["transactions_first_transaction_date"], row["synced_at"],
                ),
            )
        for row in balances:
            conn.execute(
                "INSERT INTO brokerage_balances VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id, _SNAPTRADE_PROVIDER, row["account_id"], row["currency"], row["cash"],
                    row["buying_power"], row["synced_at"],
                ),
            )
        for row in positions:
            conn.execute(
                "INSERT INTO brokerage_positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id, _SNAPTRADE_PROVIDER, row["account_id"], row["provider_symbol_id"],
                    row["symbol"], row["description"], row["asset_type"], row["quantity"],
                    row["price"], row["market_value"], row["average_cost"], row["currency"],
                    row["synced_at"],
                ),
            )
        for row in activities:
            conn.execute(
                "INSERT INTO brokerage_activities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (user_id, provider, provider_account_id, provider_activity_id) DO UPDATE SET "
                "provider_account_id=excluded.provider_account_id, activity_type=excluded.activity_type, "
                "trade_date=excluded.trade_date, settlement_date=excluded.settlement_date, "
                "symbol=excluded.symbol, description=excluded.description, units=excluded.units, "
                "price=excluded.price, amount=excluded.amount, fee=excluded.fee, "
                "currency=excluded.currency, synced_at=excluded.synced_at",
                (
                    user_id, _SNAPTRADE_PROVIDER, row["id"], row["account_id"], row["type"],
                    row["trade_date"], row["settlement_date"], row["symbol"], row["description"],
                    row["units"], row["price"], row["amount"], row["fee"], row["currency"],
                    row["synced_at"],
                ),
            )
    return True


def snaptrade_snapshot(user_id: int) -> dict[str, Any]:
    with get_conn() as conn:
        conn.execute(
            "BEGIN ISOLATION LEVEL REPEATABLE READ"
            if DB_BACKEND == "postgres" else "BEGIN",
        )
        account_rows = conn.execute(
            "SELECT provider_account_id, name, number, institution_name, brokerage_slug, "
            "balance_total, balance_currency, activities_supported, holdings_unavailable, "
            "transactions_last_successful_sync, transactions_first_transaction_date, synced_at "
            "FROM brokerage_accounts WHERE user_id = ? AND provider = ? ORDER BY institution_name, name",
            (user_id, _SNAPTRADE_PROVIDER),
        ).fetchall()
        balance_rows = conn.execute(
            "SELECT provider_account_id, currency, cash, buying_power, synced_at "
            "FROM brokerage_balances WHERE user_id = ? AND provider = ? "
            "ORDER BY provider_account_id, currency",
            (user_id, _SNAPTRADE_PROVIDER),
        ).fetchall()
        position_rows = conn.execute(
            "SELECT provider_account_id, provider_symbol_id, symbol, description, asset_type, "
            "quantity, price, market_value, average_cost, currency, synced_at "
            "FROM brokerage_positions WHERE user_id = ? AND provider = ? "
            "ORDER BY provider_account_id, symbol",
            (user_id, _SNAPTRADE_PROVIDER),
        ).fetchall()
        activity_rows = conn.execute(
            "SELECT provider_activity_id, provider_account_id, activity_type, trade_date, "
            "settlement_date, symbol, description, units, price, amount, fee, currency, synced_at "
            "FROM brokerage_activities WHERE user_id = ? AND provider = ? "
            "ORDER BY trade_date DESC, provider_activity_id DESC",
            (user_id, _SNAPTRADE_PROVIDER),
        ).fetchall()
    accounts = [{
        "id": r[0], "name": r[1], "number": r[2], "institution_name": r[3],
        "brokerage_slug": r[4], "balance_total": r[5], "balance_currency": r[6],
        "activities_supported": bool(r[7]), "holdings_unavailable": bool(r[8]),
        "transactions_last_successful_sync": r[9],
        "transactions_first_transaction_date": r[10], "synced_at": r[11],
    } for r in account_rows]
    balances = [{
        "account_id": r[0], "currency": r[1], "cash": r[2],
        "buying_power": r[3], "synced_at": r[4],
    } for r in balance_rows]
    positions = [{
        "account_id": r[0], "provider_symbol_id": r[1], "symbol": r[2],
        "description": r[3], "asset_type": r[4], "quantity": r[5], "price": r[6],
        "market_value": r[7], "average_cost": r[8], "currency": r[9], "synced_at": r[10],
    } for r in position_rows]
    activities = [{
        "id": r[0], "account_id": r[1], "type": r[2], "trade_date": r[3],
        "settlement_date": r[4], "symbol": r[5], "description": r[6], "units": r[7],
        "price": r[8], "amount": r[9], "fee": r[10], "currency": r[11],
        "synced_at": r[12],
    } for r in activity_rows]
    return {
        "accounts": accounts,
        "balances": balances,
        "positions": positions,
        "activities": activities,
        "last_synced_at": max((row["synced_at"] for row in accounts), default=None),
    }


__all__ = [
    "DB_BACKEND",
    "Connection",
    "Cursor",
    "IntegrityError",
    "OperationalError",
    "Row",
    "get_conn",
    "now_iso",
    "open_bank_conn",
    "q",
    "server_db_path",
    "snaptrade_acquire_lock",
    "snaptrade_get_credentials",
    "snaptrade_insert_credentials",
    "snaptrade_release_lock",
    "snaptrade_replace_snapshot",
    "snaptrade_snapshot",
]
