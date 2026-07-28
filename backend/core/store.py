#!/usr/bin/env python3
"""Bank data SQLite persistence - incremental UPSERT.

銀行資料 SQLite 持久化 — 增量 UPSERT。

增量核心三類處理（不能一視同仁）：
  1. 已過帳事件（台幣已入帳交易、信用卡已出帳明細）：凍結不變
     → UNIQUE 自然鍵 + ON CONFLICT DO NOTHING（append-only，重跑跳過）
  2. 未出帳 / 即時消費（會變：待沖正、入帳中）：非 append-only
     → 每次該卡先 DELETE 該 scope 再 INSERT（refresh-by-scope）
  3. 每日快照（餘額走勢、額度、紅利、各類現值）：同期覆蓋最新
     → PK = 期間 + ON CONFLICT DO UPDATE

去重鍵設計原則：銀行 API 不給穩定 transaction ID（sequenceNumber 是本次查詢列號，
不穩），所以用「交易內容的自然不變欄位」當鍵。台幣交易用 balance 當 tie-breaker
（同秒多筆時餘額不同），幾乎不碰撞。已知限制：信用卡同日同商家同額且銀行未給其他
區分欄位時，會被視為一筆（銀行對帳本身也分不出，合理）。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3  # only allowed here + bank_pg.py + server/db.py (the 3 db layer files)
from datetime import datetime, UTC
from pathlib import Path

from backend.core import bank_pg

DATA_ROOT = Path(__file__).resolve().parents[1] / "data"

# Phase C-Suggestion (2026-06-17): per-process migration cache.
# _migrate 跑 30+ PRAGMA + ALTER 累積 ~50ms 每次, server-mode 每 sync request
# 開一個 BankStore 就跑一次, 重請求成本可觀。同 process 同 db_path 已 migrate 過
# 必不再重跑 (SQLite ALTER 都 idempotent 不會破壞, 但白白燒 CPU + 可能噴 ERROR log)。
# 用 abspath 當 key 防 cwd 切換 / 不同 user 的 BANK_DATA_ROOT 各自獨立。
# pg-mode 用 special key "pg:<bank>" 因 connection 不對 file path。
_MIGRATED_DBS: set[str] = set()


def _migration_cache_key(db_path: Path | None, bank: str) -> str:
    if db_path is None:
        # pg-mode: shared schema 已 migrate 一次後 process 內不再重跑
        return f"pg:{bank}"
    return str(db_path.resolve())


def _reset_migration_cache() -> None:
    """Test-only: clear migration cache so reload + fresh tmp_path 永遠 re-migrate.

    Production code 不該呼叫。conftest autouse fixture 用此防 process state leak。
    """
    _MIGRATED_DBS.clear()

def _data_root() -> Path:
    """讀環境變數 BANK_DATA_ROOT (測試用 tmp_path), 否則用 module default.

    2026-06-14: 修 test fixture leak — 多個 test 用 monkeypatch.setenv 但
    BankStore 之前讀的是 module-level DATA_ROOT, 沒看 env, 結果 leak 寫入
    真實 backend/data/*.sqlite 留垃圾資料.
    """
    import os
    env = os.environ.get("BANK_DATA_ROOT")
    return Path(env) if env else DATA_ROOT


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_date_text(s) -> str | None:
    """Normalize bank-native date text for normalized DB columns.

    Accepts YYYY-M-D / YYYY/M/D / ISO datetime and returns YYYY-MM-DD.
    Keeps non-date strings as-is so legacy MM-DD or unsupported bank-specific
    sentinels do not get silently destroyed.
    """
    if s is None or s == "":
        return None
    t = str(s).strip()
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:T.*)?$", t)
    if not m:
        return t
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def _dedup_key(*parts) -> str:
    """把去重欄位正規化成穩定字串鍵：NULL/None → ''，數字保留，用 \\x1f 分隔。
    避開 SQLite UNIQUE 把 NULL 視為互不相等的陷阱。
    """
    return "\x1f".join("" if p is None else str(p) for p in parts)


def _categorizer_text(t: dict) -> str:
    """Phase 8.4 (2026-06-15): 拼 categorizer 看的文字 — desc + counterparty + memo.

    各銀行 raw description 常是「交易類別名」(永豐「台幣匯款」/玉山「跨行匯入」),
    真正交易對象/識別在 counterparty_acct / memo 欄。Categorizer 改吃 join 後文字,
    薪資 rule 才能命中 MICROSOFT 等 counterparty 字串。
    Raw description 不動 — 保「修正≠刪除」鐵則。
    """
    parts = [
        t.get("desc") or "",
        t.get("counterparty_acct") or "",
        t.get("memo") or "",
    ]
    # 去重 + 空字串過濾 — 有些銀行 desc 跟 counterparty 完全一樣不必重複
    seen = set()
    out = []
    for p in parts:
        s = p.strip() if p else ""
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return " | ".join(out)


# seed_rules 的 category → (flow_type, income_category)。
# 未列出的 category (飲食/交通/購物/…) 一律 expense + income_category=None。
# income_category 只在 flow_type='income' 有意義 (schema 註記), 其餘一律 None。
_FLOW_BY_CATEGORY: dict[str, tuple[str, str | None]] = {
    "薪資": ("income", "salary"),
    "獎金": ("income", "bonus"),
    "利息股息": ("income", "interest_dividend"),
    "投資收益": ("income", "investment_gain"),
    "轉帳": ("transfer", None),
    "還款": ("transfer", None),
    "投資": ("investment", None),
}


def _flow_fields(
    category: str | None,
    subcategory: str | None,
    amount: int | float | None,
    txn_type: str | None = None,
) -> tuple[str, str | None]:
    """category / txn_type (+金額方向) → (flow_type, income_category)。

    2026-07-28: 三個 upsert_* 的 INSERT 從來沒寫過這兩欄, 全靠 schema
    `DEFAULT 'expense'` — 所以連「存款利息 +$4」都被記成支出,
    passive_income / amount_by_flow_type 永遠是 0。root cause 在此統一補。

    規則 (依序):
      1. 信用卡 txn_type 優先 — 跟 routers/transactions._transaction_cashflow 同一組:
         cashback/refund/fee_waiver → income (但 income_category=None, 不算 FIRE),
         payment (卡費還款) → transfer。
      2. category 命中 _FLOW_BY_CATEGORY → 用該映射。
      3. 「其他/退稅」是政府退款, category 太泛不能整類當收入, 只認這個 subcategory。
      4. 都沒命中但金額為正 (台幣存款 income 欄) → income/other。
         台幣交易的 amount 由 caller 傳 income-expend, 方向可信。
         信用卡 caller 傳 None → 不走這條, 維持 expense。
    """
    if txn_type in ("cashback", "refund", "fee_waiver"):
        return ("income", None)
    if txn_type == "payment":
        return ("transfer", None)
    if txn_type in ("spending", "fee", "annual_fee", "installment"):
        # 2026-07-28: txn_type 必須壓過 category。真實案例 HSBC
        # 「減少消費款利息 -241 txn_type=fee」被 category『利息股息』誤判成收入,
        # 但那是利息「支出」。銀行給的 txn_type 比 keyword rule 權威。
        return ("expense", None)
    if category in _FLOW_BY_CATEGORY:
        return _FLOW_BY_CATEGORY[category]
    if category == "其他" and subcategory == "退稅":
        return ("income", "other")
    if amount is not None and amount > 0:
        return ("income", "other")
    return ("expense", None)


def _is_subscription(subcategory: str | None) -> bool:
    """subcategory == '訂閱' → is_subscription=1。

    2026-07-28: 跟 flow_type 同一批死欄位 — 三個 INSERT 從沒寫過 is_subscription,
    dashboard 訂閱卡永遠 0。`backend/subscriptions.yml` 有 110+ 關鍵字但**零 Python
    引用** (Phase 6 的 loader 在 5da42db 被當死碼刪掉)。

    這裡不重新引入 YAML loader — seed_rules 已有 priority 110 的「訂閱服務」rule
    (Netflix|Spotify|iCloud|ChatGPT|Adobe|Notion|...) 寫 subcategory='訂閱',
    直接讀它即可。單一 source of truth, 不維護兩份關鍵字表。
    # ponytail: 綁 seed rule 的 subcategory; 若日後需要「同金額月扣 ≥3 期」
    # auto-detect (spec § 5.4 提過), 再引入 subscriptions.yml loader。
    """
    return subcategory == "訂閱"


def _with_occurrence(content_keys: list[str]) -> list[str]:
    """對一批 content key 附加「同鍵出現序號」。

    解決「真實重複交易」誤殺問題：使用者同一天同店刷兩筆一樣的咖啡，
    content key 相同，但它們在同一次抓取回應裡會一起出現 →
    標成 key#0 / key#1 兩個不同 dedup_key，兩筆都留。
    重抓同一批時，又是 key#0 / key#1，對到同 dedup_key → DO NOTHING 去重。

    前提：銀行回應對同一查詢區間的交易**順序穩定**（實測台灣網銀皆如此，
    按 sequenceNumber / 時間排序固定）。
    """
    seen: dict[str, int] = {}
    out = []
    for k in content_keys:
        n = seen.get(k, 0)
        out.append(f"{k}\x1e{n}")  # \x1e = occurrence 分隔
        seen[k] = n + 1
    return out


SCHEMA = """
-- 1. 已過帳台幣交易（append-only）
-- 注意：SQLite UNIQUE 把 NULL 視為互不相等，故去重鍵不能直接含可空欄位。
-- 改用 dedup_key 正規化欄位（NULL→''/0）並對它建 UNIQUE INDEX。
-- 2026-06-17 C: user_id 多租戶隔離 — 所有 row 必屬於某 user；
-- dedup unique 改成 (user_id, dedup_key) 避免不同 user 同 dedup 撞。
CREATE TABLE IF NOT EXISTS twd_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL DEFAULT 1,
    account_no        TEXT NOT NULL,
    txn_datetime      TEXT NOT NULL,
    account_date      TEXT,
    description       TEXT,
    expend            INTEGER,
    income            INTEGER,
    balance           INTEGER,
    counterparty_bank TEXT,
    counterparty_acct TEXT,
    memo              TEXT,
    first_seen        TEXT NOT NULL,
    dedup_key         TEXT NOT NULL
);
-- 2026-06-17 C: 真正的 (user_id, dedup_key) 複合 unique 由 _migrate 升級,
-- SCHEMA 這裡只用單欄 dedup_key 避開「舊 DB ALTER ADD user_id 前 CREATE INDEX 撞欄」競態。
-- 新 DB 第一次跑會用單欄 unique → migration 立刻 DROP+CREATE 成複合 unique。
CREATE UNIQUE INDEX IF NOT EXISTS ux_twd_dedup ON twd_transactions(dedup_key);

-- 2. 信用卡已出帳逐筆明細（append-only）
-- 消費日(consume_date) 與 入帳日(post_date) 分開存；爬不到入帳日才由 persist 層
-- fallback 成與消費日相同（設計規範：兩個日期都要確實爬出）。
-- 外幣消費：consume_currency 存每筆原始幣別（JPY/USD/EUR…），consume_amount 用 REAL
-- 保留外幣小數（如 USD 123.45 的分），不可截斷；amount 是台幣入帳金額(整數)。
CREATE TABLE IF NOT EXISTS card_billed_txns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL DEFAULT 1,
    card_no          TEXT,
    bill_date        TEXT,
    currency         TEXT,
    consume_date     TEXT,
    post_date        TEXT,
    description      TEXT,
    amount           INTEGER,
    consume_country  TEXT,
    consume_currency TEXT,
    consume_amount   REAL,
    first_seen       TEXT NOT NULL,
    dedup_key        TEXT NOT NULL
);
-- 2026-06-17 C: 同上, 複合 unique 由 _migrate 升級
CREATE UNIQUE INDEX IF NOT EXISTS ux_card_billed_dedup ON card_billed_txns(dedup_key);

-- 3. 信用卡未出帳 / 即時消費（refresh-by-scope，每次 replace）
CREATE TABLE IF NOT EXISTS card_pending_txns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL DEFAULT 1,
    scope            TEXT NOT NULL,   -- 'unbilled' | 'current'
    card_no          TEXT,
    consume_date     TEXT,
    post_date        TEXT,
    description      TEXT,
    amount           INTEGER,         -- 入帳幣別金額（多半 TWD；外幣交易這裡放台幣折算）
    currency         TEXT,            -- 入帳幣別（多半 TWD，外幣交易也是 TWD 因為 amount 是台幣折算）
    consume_country  TEXT,            -- 2026-06-14: 消費國家
    consume_currency TEXT,            -- 2026-06-14: 原始消費幣別（如 EUR）
    consume_amount   REAL,            -- 2026-06-14: 原始外幣金額（保留小數）
    refreshed_at     TEXT NOT NULL
);

-- 4. 餘額走勢（每日快照，同日覆蓋）
-- 2026-06-17 C: PK 改 (user_id, snapshot_date)
CREATE TABLE IF NOT EXISTS balance_history (
    user_id       INTEGER NOT NULL DEFAULT 1,
    snapshot_date TEXT NOT NULL,
    twd_balance   INTEGER,
    fx_balance    INTEGER,
    loan_balance  INTEGER,           -- 2026-06-14 新增: 貸款餘額（負債類，獨立記錄）
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (user_id, snapshot_date)
);

-- 5. 帳戶當前狀態（UPSERT by account_no）
-- 2026-06-17 C: PK 改 (user_id, account_no)；account_no 在不同 user 可重複
--    raw_balance / raw_balance_date 是爬蟲層直接抓到的帳號級餘額快照
--    （SCSB overview「NT$13,065」、Cathay 主帳戶餘額 …），讓 portfolio 層
--    不必再從 twd_transactions 推算最新餘額（避免同日多筆 txn datetime 相同時
--    MAX() 隨機挑、貸款帳戶/真實 0 餘額/外幣帳戶沒入 twd_txn 表時餘額消失）。
--    raw_balance 用 REAL 因為外幣可能 USD1.55 帶小數。
CREATE TABLE IF NOT EXISTS accounts (
    user_id          INTEGER NOT NULL DEFAULT 1,
    account_no       TEXT NOT NULL,
    currency         TEXT,
    branch           TEXT,
    nickname         TEXT,
    type             TEXT,
    product_type     TEXT,
    raw_balance      REAL,
    raw_balance_date TEXT,
    excluded         INTEGER NOT NULL DEFAULT 0,
    -- Phase 8.2 C (2026-06-14): user 在 thoth UI 自取的帳戶暱稱
    -- 鐵則: nickname 是銀行 API 原文 (e.g. 「主存錢筒」, sync 蓋),
    -- nickname_overwrite 是 user 覆寫 (sync 不動).
    -- UI fallback: nickname_overwrite || nickname || account_no
    nickname_overwrite TEXT,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (user_id, account_no)
);

-- 6. 信用卡當前狀態（UPSERT by card_no）
-- 2026-06-17 C: PK 改 (user_id, card_no)
CREATE TABLE IF NOT EXISTS cards (
    user_id              INTEGER NOT NULL DEFAULT 1,
    card_no              TEXT NOT NULL,
    name                 TEXT,
    association          TEXT,
    type                 TEXT,
    is_cube              INTEGER,
    -- Step 2 (2026-06-14): per-card 信用額度 + 帳單日（共通 schema）
    -- 各家 collector 後續分階段補抓
    credit_limit         REAL,    -- 該卡核給的信用額度
    used_credit          REAL,    -- 該卡目前已動用金額
    statement_close_date TEXT,    -- 帳單結帳日 (YYYY-MM-DD or MM-DD)
    payment_due_date     TEXT,    -- 帳單繳費截止日 (YYYY-MM-DD or MM-DD)
    -- 2026-06-20 (HSBC bill_due 1.3M bug 修): 銀行原生「本期應繳」/ 最近繳款金額/日期.
    -- HSBC card_detail.details[] 已直給, 跳過 db_facade SQL derive (card_billed_txns
    -- bill_date NULL 時會把 12 個月歷史消費 SUM 成本期). 其他銀行可選擇性 backfill.
    -- 三欄都 NULL → db_facade 走原 derive fallback (對 cathay/ubot/etc 等 bill_date 正常的銀行).
    bill_due_amount      REAL,    -- 本期帳單應繳金額 (HSBC: Last Statement Amount)
    last_payment_amount  REAL,    -- 最近一次繳款金額 (HSBC: Last Payment Amount)
    last_payment_date    TEXT,    -- 最近一次繳款日 (HSBC: Last Payment Date, YYYY-MM-DD)
    -- 2026-06-14 使用者指示: 過期卡 UI 不顯示但 txn 紀錄保留
    -- active=1 顯示, active=0 隱藏 (DBS isDisplayImg=False / 其他行各自判斷)
    -- transactions / stats / current_month_spending 一律不過濾, 保留歷史計算
    active               INTEGER NOT NULL DEFAULT 1,
    excluded             INTEGER NOT NULL DEFAULT 0,
    -- Phase 8.2 C (2026-06-14): user 在 thoth UI 自取的卡片暱稱
    -- 鐵則: cards.name 是銀行 API 原文 (重 sync 蓋), nickname_overwrite
    -- 是 user 覆寫 (重 sync 不動). UI fallback nickname_overwrite || name.
    -- 與 transactions.description_overwrite / accounts.nickname_overwrite 同 pattern.
    nickname_overwrite   TEXT,
    updated_at           TEXT NOT NULL,
    PRIMARY KEY (user_id, card_no)
);

-- 7. 每日數值快照（額度/紅利/現值/投資/保險/貸款；保留時序）
--    同一天同 category 覆蓋（DO UPDATE），跨天保留歷史
-- 2026-06-17 C: PK 改 (user_id, snapshot_date, category)
CREATE TABLE IF NOT EXISTS daily_metrics (
    user_id       INTEGER NOT NULL DEFAULT 1,
    snapshot_date TEXT NOT NULL,
    category      TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (user_id, snapshot_date, category)
);

-- 同步紀錄
CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL DEFAULT 1,
    synced_at  TEXT NOT NULL,
    summary    TEXT
);
"""


class BankStore:
    def __init__(self, bank: str, user_id: int | None = None):
        """Open per-bank store for one user.

        Phase C (2026-06-17): user_id required for multi-tenant row isolation.
        Phase C-Suggestion (2026-06-17): default=None + 環境變數嚴格模式 —
        production (`THOTH_REQUIRE_EXPLICIT_USER_ID=1`) 沒傳 user_id 直接 raise，
        防止未來 multi-user 部署忘記顯式傳 user_id 而把所有資料 silent 寫進 user_id=1
        造成 cross-tenant data leak。Server bootstrap (server/app.py) 自動設此 env；
        CLI / tests / tools 不設 env → fallback user_id=1 保歷史單 user 語意。
        Production sync_runner._dispatch_crawler_and_persist 已永遠顯式傳 user_id，
        嚴格模式不會誤殺 production code。
        """
        if user_id is None:
            if os.environ.get("THOTH_REQUIRE_EXPLICIT_USER_ID", "").strip().lower() in ("1", "true", "yes"):
                raise ValueError(
                    f"BankStore({bank!r}) 沒帶 user_id，但 THOTH_REQUIRE_EXPLICIT_USER_ID 啟用。"
                    " Production 必須顯式傳 user_id 防 multi-tenant data leak。"
                    " 若是 CLI / test / script，請顯式傳 user_id=1 或 unset env。",
                )
            user_id = 1
        self.bank = bank
        self.user_id = user_id
        if bank_pg.enabled():
            self.db_path = None
            self.conn = bank_pg.connect(bank)
        else:
            data_root = _data_root()
            self.db_path = data_root / f"{bank}.sqlite"
            data_root.mkdir(parents=True, exist_ok=True)
            # C-5 (2026-06-17): check_same_thread=False — FastAPI routers 在 threadpool
            # 可能跨 thread 取 BankStore；不加會撞 SQLite ProgrammingError。
            # 應用層用 _dispatch_lock / 模組級鎖序列化 write，並無真實 concurrent issue。
            self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # Phase C-Suggestion (2026-06-17): per-process migration cache.
        # 同 process 同 db_path 已 migrate 過就 skip 30+ PRAGMA + ALTER。
        # 仍會跑 executescript(SCHEMA) — CREATE TABLE IF NOT EXISTS 不會傷既有表,
        # 但補 _migrate 才有 ALTER ADD COLUMN / CREATE UNIQUE INDEX (新 schema 差異)。
        cache_key = _migration_cache_key(self.db_path, bank)
        if cache_key not in _MIGRATED_DBS:
            self._migrate()
            _MIGRATED_DBS.add(cache_key)
        self.conn.commit()

    def _migrate(self):
        """對既有 DB 補新增欄位（CREATE TABLE IF NOT EXISTS 不會改既有表）。"""
        cols = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(card_billed_txns)").fetchall()}
        if "post_date" not in cols:
            # 入帳日欄位；既有列先以消費日回填（爬不到入帳日時兩者相同的既定語意）
            self.conn.execute("ALTER TABLE card_billed_txns ADD COLUMN post_date TEXT")
            self.conn.execute(
                "UPDATE card_billed_txns SET post_date = consume_date WHERE post_date IS NULL")
        # Phase 5.1：分類欄位（三張交易表都加）
        for tbl in ("twd_transactions", "card_billed_txns", "card_pending_txns"):
            tbl_cols = {r["name"] for r in self.conn.execute(
                f"PRAGMA table_info({tbl})").fetchall()}
            if "category" not in tbl_cols:
                self.conn.execute(f"ALTER TABLE {tbl} ADD COLUMN category TEXT")
        # Phase 6 (B-full): txn_type 欄位 — 區分 spending/cashback/refund/payment/fee/...
        # 統計層用這欄判斷「正向現金流 (cashback/refund) 不歸 expense」。
        # 兩張信用卡表都要；twd_transactions 暫不加（台幣存款 expend/income 已分開）。
        for tbl in ("card_billed_txns", "card_pending_txns"):
            tbl_cols = {r["name"] for r in self.conn.execute(
                f"PRAGMA table_info({tbl})").fetchall()}
            if "txn_type" not in tbl_cols:
                self.conn.execute(f"ALTER TABLE {tbl} ADD COLUMN txn_type TEXT")
        # Phase 6 (loan): balance_history 加 loan_balance 欄
        # 把信貸/房貸跟存款分開記錄，避免貸款餘額被當資產灌進 twd_balance。
        bh_cols = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(balance_history)").fetchall()}
        if "loan_balance" not in bh_cols:
            self.conn.execute("ALTER TABLE balance_history ADD COLUMN loan_balance INTEGER")
        # Phase 6 (FX pending): card_pending_txns 加外幣三欄
        # 過去外幣交易把 foreignAmount/currency 直接塞 amount/currency，
        # 害 detail UI 以為「沒台幣金額」(currency=EUR)。
        # 改用跟 card_billed_txns 對齊的 schema：
        #   amount/currency = 入帳金額 + TWD
        #   consume_amount/consume_currency/consume_country = 原始消費幣別與金額
        pending_cols = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(card_pending_txns)").fetchall()}
        for col, ddl in [
            ("post_date", "ALTER TABLE card_pending_txns ADD COLUMN post_date TEXT"),
            ("consume_country", "ALTER TABLE card_pending_txns ADD COLUMN consume_country TEXT"),
            ("consume_currency", "ALTER TABLE card_pending_txns ADD COLUMN consume_currency TEXT"),
            ("consume_amount", "ALTER TABLE card_pending_txns ADD COLUMN consume_amount REAL"),
        ]:
            if col not in pending_cols:
                self.conn.execute(ddl)
        # Phase 6 (raw balance): accounts 加 raw_balance + raw_balance_date 兩欄
        # 過去 portfolio 從 twd_transactions 最新一筆 balance 推算帳號餘額，
        # 但同日多筆 txn datetime 完全相同時 MAX() 不分先後（SCSB 11101 案）、
        # 貸款帳戶根本沒入 twd_txn 表 → 餘額消失、外幣帳戶（SCSB 26108 USD1.55）
        # 沒入 twd_txn 也消失。改用爬蟲直接抓的帳號級餘額為主。
        # Phase 6 (excluded): 加 excluded 旗標讓使用者能標「不納入淨資產統計」
        # → portfolio summary 跳過、txn list 反灰、stats 不算
        acc_cols = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(accounts)").fetchall()}
        for col, ddl in [
            ("raw_balance", "ALTER TABLE accounts ADD COLUMN raw_balance REAL"),
            ("raw_balance_date", "ALTER TABLE accounts ADD COLUMN raw_balance_date TEXT"),
            ("excluded", "ALTER TABLE accounts ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0"),
            # Phase 8.2 C (2026-06-14): user 覆寫帳戶暱稱
            ("nickname_overwrite", "ALTER TABLE accounts ADD COLUMN nickname_overwrite TEXT"),
        ]:
            if col not in acc_cols:
                self.conn.execute(ddl)
        # Phase 6 (excluded) + Step 2 (per-card 信用額度/帳單日) +
        # 2026-06-14 (active): 過期卡 UI hide. 5 + 1 共 6 個 nullable migration.
        card_cols = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(cards)").fetchall()}
        for col, ddl in [
            ("excluded", "ALTER TABLE cards ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0"),
            ("credit_limit", "ALTER TABLE cards ADD COLUMN credit_limit REAL"),
            ("used_credit", "ALTER TABLE cards ADD COLUMN used_credit REAL"),
            ("statement_close_date", "ALTER TABLE cards ADD COLUMN statement_close_date TEXT"),
            ("payment_due_date", "ALTER TABLE cards ADD COLUMN payment_due_date TEXT"),
            ("active", "ALTER TABLE cards ADD COLUMN active INTEGER NOT NULL DEFAULT 1"),
            # 2026-06-20 (HSBC bill_due 1.3M bug 修): 銀行原生欄, NULL 走 derive fallback
            ("bill_due_amount", "ALTER TABLE cards ADD COLUMN bill_due_amount REAL"),
            ("last_payment_amount", "ALTER TABLE cards ADD COLUMN last_payment_amount REAL"),
            ("last_payment_date", "ALTER TABLE cards ADD COLUMN last_payment_date TEXT"),
            # Phase 8.2 C (2026-06-14): user 覆寫卡片暱稱
            ("nickname_overwrite", "ALTER TABLE cards ADD COLUMN nickname_overwrite TEXT"),
        ]:
            if col not in card_cols:
                self.conn.execute(ddl)
        # Phase 6 (category taxonomy 2026-06-15): 三張交易表加 4 個 COICOP 對齊欄位。
        # 詳見 wiki [[personal-finance-transaction-category-taxonomy]]
        #   flow_type        — 收支統計閘門 (expense/income/transfer/investment)
        #                      注意跟既有 txn_type (spending/cashback/refund/payment/...) 正交
        #                      txn_type 是「卡費行為類型」, flow_type 是「收支統計分桶」
        #   is_subscription  — 訂閱 flag (Netflix/Spotify/iCloud/...), 跨多個 category
        #   subcategory      — 用戶自訂子分類 (UI 下拉 + 新增)
        #   legacy_category  — migration audit trail, 保留舊類名不刪 (回滾路徑)
        #
        # Phase 7 (2026-06-15) — Income 5 類:
        #   income_category  — 收入 5 主類 (salary/bonus/interest_dividend/investment_gain/other)
        #                      只在 flow_type='income' 才有意義, 其他 row 永遠 NULL
        #                      詳見 wiki [[income-classifier-and-fire-passive-income-spec]]
        # Phase 8.2 (2026-06-14) — User overwrite 欄:
        #   description_overwrite — 使用者覆寫的說明文字, raw description 永遠不動
        #                           (使用者鐵則「修正≠刪除」). frontend 顯示時 fallback
        #                           description_overwrite || description.
        # Phase 8.3 (2026-06-15) — auto_excluded:
        #   auto_excluded         — categorizer 命中某 rule (auto_excluded=1) 後寫進 txn,
        #                           stats aggregate 時自動 skip income/expense 桶。
        #                           解決「信用卡還款/轉帳/退款/回饋等 by-definition 不算
        #                           收支」row 永遠不必使用者手動勾的痛點。
        # Phase 9 (2026-06-16) — tags_overwrite:
        #   tags_overwrite        — 使用者自訂標籤 JSON array (e.g. ["週末","出差"]).
        #                           跟 description_overwrite 同 overlay pattern,
        #                           raw row 沒對應 tags 欄, 完全 user 自加. NULL = 無標籤.
        #                           empty `[]` = 顯式清空. 跨銀行 filter / 跨主類 mark.
        for tbl in ("twd_transactions", "card_billed_txns", "card_pending_txns"):
            tbl_cols = {r["name"] for r in self.conn.execute(
                f"PRAGMA table_info({tbl})").fetchall()}
            for col, ddl in [
                ("flow_type", f"ALTER TABLE {tbl} ADD COLUMN flow_type TEXT NOT NULL DEFAULT 'expense'"),
                ("is_subscription", f"ALTER TABLE {tbl} ADD COLUMN is_subscription INTEGER NOT NULL DEFAULT 0"),
                ("subcategory", f"ALTER TABLE {tbl} ADD COLUMN subcategory TEXT"),
                ("legacy_category", f"ALTER TABLE {tbl} ADD COLUMN legacy_category TEXT"),
                ("income_category", f"ALTER TABLE {tbl} ADD COLUMN income_category TEXT"),
                ("description_overwrite", f"ALTER TABLE {tbl} ADD COLUMN description_overwrite TEXT"),
                ("auto_excluded", f"ALTER TABLE {tbl} ADD COLUMN auto_excluded INTEGER NOT NULL DEFAULT 0"),
                ("tags_overwrite", f"ALTER TABLE {tbl} ADD COLUMN tags_overwrite TEXT"),
            ]:
                if col not in tbl_cols:
                    self.conn.execute(ddl)
        # Phase C (2026-06-17): multi-user 隔離 — 所有 bank-level 表加 user_id NOT NULL DEFAULT 1
        # 既有 row backfill 成 user_id=1（使用者單人模式起家）; 新 row 由 BankStore(user_id=...) 顯式帶入。
        # 8 張表：twd_transactions / card_billed_txns / card_pending_txns /
        #          balance_history / accounts / cards / daily_metrics / sync_log
        # 注意 UNIQUE INDEX / PRIMARY KEY 在新 DB 已含 user_id；舊 DB 此處只補 column。
        # ref wiki [[thoth-multi-user-row-isolation-refactor-2026-06-17]]
        for tbl in (
            "twd_transactions", "card_billed_txns", "card_pending_txns",
            "balance_history", "accounts", "cards", "daily_metrics", "sync_log",
        ):
            tbl_cols = {r["name"] for r in self.conn.execute(
                f"PRAGMA table_info({tbl})").fetchall()}
            if "user_id" not in tbl_cols:
                self.conn.execute(
                    f"ALTER TABLE {tbl} ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1",
                )
        # 舊 DB 在 ALTER 前可能已有「不含 user_id」的 UNIQUE index/legacy UNIQUE column,
        # 升 (user_id, dedup_key) 為複合 unique key。
        # DROP IF EXISTS 是 idempotent (新 DB SCHEMA 用同名 index 但已含 user_id, 也安全)。
        # 兩張表都重建。
        for tbl, idx in (
            ("twd_transactions", "ux_twd_dedup"),
            ("card_billed_txns", "ux_card_billed_dedup"),
        ):
            try:
                self.conn.execute(f"DROP INDEX IF EXISTS {idx}")
                self.conn.execute(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {idx} ON {tbl}(user_id, dedup_key)",
                )
            except sqlite3.OperationalError as e:
                # Composite unique 是 ON CONFLICT(user_id, dedup_key) 必要條件,
                # 失敗就讓它炸 — 比 silent 後寫入時才爆好 debug。
                import logging
                logging.error(
                    "Phase C: composite unique upgrade failed for %s/%s: %s — "
                    "INSERT...ON CONFLICT(user_id, dedup_key) will fail on next sync",
                    tbl, idx, e,
                )
                raise
        # 2026-06-17 C-pk: 4 張 PK 表 (balance_history/accounts/cards/daily_metrics) 舊 DB
        # 的 PK 是「不含 user_id」單欄, SCHEMA 新版升為複合 PK 但 SQLite 不支援 ALTER PK,
        # 改用 CREATE UNIQUE INDEX 兜底 — INSERT...ON CONFLICT(user_id, ...) 可走 unique
        # index 而不必走 PK, INSERT 路徑寫死 ON CONFLICT(user_id, account_no) 等就 work。
        # 新 DB (Phase C 後建) PK 已含 user_id, CREATE INDEX 同名也 idempotent 安全。
        for tbl, idx, cols in (
            ("balance_history", "ux_balance_history_user_snap", "(user_id, snapshot_date)"),
            ("accounts", "ux_accounts_user_no", "(user_id, account_no)"),
            ("cards", "ux_cards_user_no", "(user_id, card_no)"),
            ("daily_metrics", "ux_daily_metrics_user_snap_cat", "(user_id, snapshot_date, category)"),
        ):
            try:
                self.conn.execute(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {idx} ON {tbl}{cols}",
                )
            except sqlite3.OperationalError as e:
                import logging
                logging.error(
                    "Phase C-pk: composite unique upgrade failed for %s/%s: %s — "
                    "INSERT...ON CONFLICT(user_id, ...) will fail on next sync",
                    tbl, idx, e,
                )
                raise
        self.conn.commit()

    def close(self):
        self.conn.close()

    def _pending_user_metadata(self, scope: str | None = None) -> dict[tuple, list[tuple]]:
        """Snapshot user-edited fields before pending rows are replaced or promoted."""
        sql = (
            "SELECT card_no, consume_date, amount, description, category, subcategory, "
            "description_overwrite, tags_overwrite, auto_excluded "
            "FROM card_pending_txns WHERE user_id = ?"
        )
        args: tuple = (self.user_id,)
        if scope is not None:
            sql += " AND scope = ?"
            args = (self.user_id, scope)
        sql += " ORDER BY id"
        snapshots: dict[tuple, list[tuple]] = {}
        for row in self.conn.execute(sql, args).fetchall():
            key = (
                row["card_no"], row["consume_date"], row["amount"], row["description"],
            )
            snapshots.setdefault(key, []).append((
                row["category"], row["subcategory"], row["description_overwrite"],
                row["tags_overwrite"], row["auto_excluded"],
            ))
        return snapshots

    # ---- 1. 台幣已過帳交易：append-only ----
    def upsert_twd_txns(self, txns: list[dict], rules: list[dict] | None = None) -> int:
        """寫入台幣交易。若 `rules` 提供，每筆 desc 跑 categorize → 寫 category + subcategory + auto_excluded 欄。"""
        from backend.server.categorizer import categorize_with_excluded  # 延遲 import 避免 cli 依賴
        before = self.conn.total_changes
        now = _now()
        # 先算每筆的 content key（含 balance 當 running-balance tie-breaker），
        # 再附加同鍵出現序號 → 真實重複交易也能各自留存、重抓又能去重
        content_keys = [
            _dedup_key(t.get("account_no"), t.get("datetime"), t.get("expend"),
                       t.get("income"), t.get("balance"), t.get("desc"))
            for t in txns
        ]
        dedup_keys = _with_occurrence(content_keys)
        for t, key in zip(txns, dedup_keys, strict=True):
            cat, sub, auto_ex = (categorize_with_excluded(_categorizer_text(t), rules)
                                  if rules else (None, None, False))
            # 台幣: amount 方向可信 (income - expend), 給 _flow_fields 當 fallback
            net = (t.get("income") or 0) - (t.get("expend") or 0)
            flow, income_cat = _flow_fields(cat, sub, net)
            self.conn.execute(
                """INSERT INTO twd_transactions
                   (user_id, account_no, txn_datetime, account_date, description, expend, income,
                    balance, counterparty_bank, counterparty_acct, memo, first_seen, dedup_key,
                    category, subcategory, auto_excluded, flow_type, income_category,
                    is_subscription)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(user_id, dedup_key) DO NOTHING""",
                (self.user_id, t.get("account_no"), t.get("datetime"), t.get("account_date"),
                 t.get("desc"), t.get("expend"), t.get("income"), t.get("balance"),
                 t.get("counterparty_bank"), t.get("counterparty_acct"), t.get("memo"),
                 now, key, cat, sub, 1 if auto_ex else 0, flow, income_cat,
                 1 if _is_subscription(sub) else 0),
            )
        self.conn.commit()
        return self.conn.total_changes - before

    # ---- 2. 信用卡已出帳明細：append-only ----
    def upsert_card_billed(self, txns: list[dict], rules: list[dict] | None = None) -> int:
        from backend.server.categorizer import categorize_with_excluded
        before = self.conn.total_changes
        now = _now()
        pending_metadata = self._pending_user_metadata()
        # 信用卡無 running balance，重複刷卡更需 occurrence index 防誤殺。
        # dedup_key 同時納入 consume_date(消費日) 與 post_date(入帳日)：
        # 同消費日但不同入帳日（如分期、跨月折算）算不同筆，不可去重誤殺。
        content_keys = [
            _dedup_key(t.get("card_no"), t.get("bill_date"), t.get("date"),
                       t.get("post_date"), t.get("desc"), t.get("amount"),
                       t.get("consume_amount"))
            for t in txns
        ]
        dedup_keys = _with_occurrence(content_keys)
        for t, key in zip(txns, dedup_keys, strict=True):
            # 入帳日爬不到 → fallback 成與消費日相同（設計規範）
            post_date = t.get("post_date") or t.get("date")
            cat, sub, auto_ex = (categorize_with_excluded(_categorizer_text(t), rules)
                                  if rules else (None, None, False))
            metadata_rows = None
            if (
                t.get("card_no") and t.get("date")
                and t.get("amount") is not None and t.get("desc")
            ):
                metadata_rows = pending_metadata.get((
                    t.get("card_no"), t.get("date"), t.get("amount"), t.get("desc"),
                ))
            description_overwrite = tags_overwrite = None
            if metadata_rows:
                cat, sub, description_overwrite, tags_overwrite, auto_ex = metadata_rows.pop(0)
            # 信用卡: amount=None 讓 _flow_fields 不走「正值即收入」fallback
            # (帳單視角的正負跟 user cashflow 方向不一致, 只信 txn_type/category)
            flow, income_cat = _flow_fields(cat, sub, None, t.get("txn_type"))
            self.conn.execute(
                """INSERT INTO card_billed_txns
                   (user_id, card_no, bill_date, currency, consume_date, post_date, description,
                    amount, consume_country, consume_currency, consume_amount, first_seen,
                    dedup_key, category, subcategory, txn_type, auto_excluded,
                    description_overwrite, tags_overwrite, flow_type, income_category,
                    is_subscription)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(user_id, dedup_key) DO NOTHING""",
                (self.user_id, t.get("card_no"), t.get("bill_date"), t.get("currency"), t.get("date"),
                 post_date, t.get("desc"), t.get("amount"), t.get("consume_country"),
                 t.get("consume_currency"), t.get("consume_amount"), now, key, cat, sub,
                 t.get("txn_type"), 1 if auto_ex else 0,
                 description_overwrite, tags_overwrite, flow, income_cat,
                 1 if _is_subscription(sub) else 0),
            )
            # 2026-06-13: 對齊「顯示誠實」鐵律 — 寫 billed 同時把 pending 對應筆清掉。
            # 銀行 billed 出帳後，pending 通常 1-3 天才會從未出帳清單移除；過渡期 UI
            # 會雙重計算同一筆消費 (見 esun ****7032 過渡期 4 筆 stale row 案例)。
            # 比對條件：card_no + consume_date + amount + desc (四欄全等 ↔ 同一筆)。
            # 為何不只比 card_no+date+amount？同日同卡同金額不同商家有可能 (e.g. 7-11 跨店買 100)。
            self._purge_overlapping_pending(
                card_no=t.get("card_no"),
                consume_date=t.get("date"),
                amount=t.get("amount"),
                desc=t.get("desc"),
            )
        self.conn.commit()
        return self.conn.total_changes - before

    def _purge_overlapping_pending(
        self,
        card_no: str | None,
        consume_date: str | None,
        amount: int | float | None,
        desc: str | None,
    ) -> int:
        """寫 billed 時連動清掉 pending 中同一筆消費（過渡期 stale row）。

        比對 4 欄全等才清：card_no, consume_date, amount, description。
        4 欄任一為 None 視為「資料不齊不敢清」直接 return 0（保守策略）。

        Phase C-review (2026-06-17): 顯式列出已知 scope (unbilled/current),
        避免未來新增 scope (e.g. 'instalment' 分期未來幾期) 被誤殺。
        新增 scope 時須評估是否該被 billed 出帳事件 purge。

        Returns: 清掉的 pending row 數。
        """
        if not card_no or not consume_date or amount is None or not desc:
            return 0
        cur = self.conn.execute(
            """DELETE FROM card_pending_txns
               WHERE user_id = ?
                 AND card_no = ?
                 AND consume_date = ?
                 AND amount = ?
                 AND description = ?
                 AND scope IN ('unbilled', 'current')""",
            (self.user_id, card_no, consume_date, amount, desc),
        )
        return cur.rowcount or 0

    def dedup_billed_stale_rows(self) -> int:
        """清理 card_billed_txns 中因 norm 規則演進造成的「同消費多 row」歷史包袱。

        背景：dedup_key 包含 consume_amount 欄位，當 norm 規則改變
        （例如 esun 純台幣從寫 358.0 改成寫 None；ctbc 外幣從寫 None 改成寫 196.2），
        同一筆消費再 reprocess 會產生不同 dedup_key → INSERT 不會被 ON CONFLICT
        攔下 → 同消費雙列。

        策略：對 (card_no, consume_date, post_date, amount, description) 五欄全等的
        群組，保留「資訊量最高」那筆 — 即 consume_currency/consume_amount 至少有一個
        NOT NULL 那筆；若資訊量相同則保留 first_seen 最舊的。
        DELETE 其他。

        為何 5 欄全等才合併（含 post_date）？
          - HSBC 分期付款案例：同筆「剩餘本金 0 元」每期會打印一次，
            consume_date 都同 2026-01-27 但 post_date 是 2026-02-02 / 03-02 / 04-02 / 05-02
            → 是真的 6 期分期，不可合併
          - dedup_key 本身就包含 post_date，這裡 dedup 也要對齊
          - 同 _purge_overlapping_pending 邏輯：同日同卡同金額不同 desc → 視為不同筆

        為何不單純留最舊？
          - ctbc EUR 外幣 case: 舊 row consume_amount=None / 新 row consume_amount=196.2
          - 留最舊會丟失外幣金額資訊（資料變不完整）
          - 「留最完整」確保不論 norm 演進方向都不丟資料

        任一關鍵欄 (card_no/consume_date/amount/description) 為 NULL 不參與合併
        （保守策略避免 wildcard 誤殺）。post_date 為 NULL 視為 NULL（COALESCE 對齊）。

        Returns: 清掉的 row 數。
        """
        # 資訊量打分：consume_currency NOT NULL 加 1，consume_amount NOT NULL 加 1
        # 對 5 欄群組找最高分（分數同則最舊 first_seen）→ 留下，DELETE 其他
        # 用 ROW_NUMBER 視窗函數 (SQLite 3.25+, macOS 自帶 3.43+)
        # Phase C (2026-06-17): 只 dedup 本 user 的 row, partition 多加 user_id 也安全。
        cur = self.conn.execute(
            """DELETE FROM card_billed_txns
               WHERE id IN (
                   SELECT id FROM (
                       SELECT id,
                              ROW_NUMBER() OVER (
                                  PARTITION BY user_id, card_no, consume_date, post_date, amount, description
                                  ORDER BY
                                    (CASE WHEN consume_currency IS NOT NULL THEN 1 ELSE 0 END
                                     + CASE WHEN consume_amount IS NOT NULL THEN 1 ELSE 0 END) DESC,
                                    first_seen ASC,
                                    id ASC
                              ) AS rn
                       FROM card_billed_txns
                       WHERE user_id = ?
                         AND card_no IS NOT NULL
                         AND consume_date IS NOT NULL
                         AND amount IS NOT NULL
                         AND description IS NOT NULL
                   ) ranked
                   WHERE rn > 1
               )""",
            (self.user_id,),
        )
        deleted = cur.rowcount or 0
        self.conn.commit()
        return deleted

    def purge_legacy_masked_card_no_rows(self) -> tuple[int, int]:
        """一次性 cleanup：砍 card_no 仍為「raw masked full」(例 9064-XXXX-XXXX-7032) 的 row。

        Background: esun persist 在 2026-06-13 ~ 2026-06-20 期間 bug，把 raw masked
        full card_no 直接寫進 card_billed_txns / card_pending_txns，但 cards 表用
        `****{last4}` 格式 → bill_summary join 失敗 → 帳戶 tab 顯示帳單 0。
        Fix 後（bcfbf6f）需 idempotent cleanup 把舊格式 row 一次性砍掉。

        Returns: (billed_deleted, pending_deleted)
        """
        cur_b = self.conn.execute(
            "DELETE FROM card_billed_txns WHERE user_id = ? AND card_no LIKE '%-XXXX-XXXX-%'",
            (self.user_id,),
        )
        cur_p = self.conn.execute(
            "DELETE FROM card_pending_txns WHERE user_id = ? AND card_no LIKE '%-XXXX-XXXX-%'",
            (self.user_id,),
        )
        self.conn.commit()
        return (cur_b.rowcount or 0, cur_p.rowcount or 0)

    # ---- 3. 未出帳/即時：refresh-by-scope ----
    def refresh_card_pending(self, scope: str, txns: list[dict],
                             rules: list[dict] | None = None) -> int:
        from backend.server.categorizer import categorize_with_excluded
        now = _now()
        pending_metadata = self._pending_user_metadata(scope)
        # Phase C (2026-06-17): refresh 只清本 user 的, 不影響別 user。
        self.conn.execute(
            "DELETE FROM card_pending_txns WHERE user_id = ? AND scope = ?",
            (self.user_id, scope),
        )
        for t in txns:
            cat, sub, auto_ex = (categorize_with_excluded(_categorizer_text(t), rules)
                                  if rules else (None, None, False))
            metadata_rows = pending_metadata.get((
                t.get("card_no"), t.get("date"), t.get("amount"), t.get("desc"),
            ))
            description_overwrite = tags_overwrite = None
            if metadata_rows:
                cat, sub, description_overwrite, tags_overwrite, auto_ex = metadata_rows.pop(0)
            flow, income_cat = _flow_fields(cat, sub, None, t.get("txn_type"))
            self.conn.execute(
                """INSERT INTO card_pending_txns
                   (user_id, scope, card_no, consume_date, post_date, description, amount, currency,
                    consume_country, consume_currency, consume_amount,
                    refreshed_at, category, subcategory, txn_type, auto_excluded,
                    description_overwrite, tags_overwrite, flow_type, income_category,
                    is_subscription)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.user_id, scope, t.get("card_no"), t.get("date"),
                 t.get("post_date") or t.get("date"), t.get("desc"),
                 t.get("amount"), t.get("currency"),
                 t.get("consume_country"), t.get("consume_currency"),
                 t.get("consume_amount"),
                 now, cat, sub, t.get("txn_type"), 1 if auto_ex else 0,
                 description_overwrite, tags_overwrite, flow, income_cat,
                 1 if _is_subscription(sub) else 0),
            )

        # Phase 8.5 (2026-06-18) — 結帳跨表去重:
        # 銀行結帳後同筆 txn 同時出現在 pending (未結款 API 還回) +
        # billed (帳單已生成). 若不去重, /transactions UNION ALL 會雙顯, UI 重複.
        # 使用者 CTBC 4 筆重複案例 (SUKIYA/健身工廠/中華航空/中華電信) 觸發.
        #
        # 策略: pending 是「即時」性, billed 是「正式」, billed 為準 — pending
        # 同 (card_no, consume_date, description, amount) 命中 billed 即 prune.
        # 必須在 caller 端 (persist/*.py) 先 upsert_card_billed 再 refresh_card_pending,
        # 才能保證 billed 已就位. 目前 ctbc/hsbc/ubot/cathay/esun 都是這順序.
        pruned = self.conn.execute(
            """DELETE FROM card_pending_txns
               WHERE user_id = ? AND scope = ?
                 AND EXISTS (
                   SELECT 1 FROM card_billed_txns b
                   WHERE b.user_id = card_pending_txns.user_id
                     AND b.card_no = card_pending_txns.card_no
                     AND b.consume_date = card_pending_txns.consume_date
                     AND b.description = card_pending_txns.description
                     AND b.amount = card_pending_txns.amount
                 )""",
            (self.user_id, scope),
        ).rowcount

        self.conn.commit()
        # 回傳實際留下的 row 數 (txns 輸入數 - pruned 被去重數)
        return len(txns) - max(pruned, 0)

    # ---- 4. 餘額走勢：同日 UPSERT ----
    def upsert_balance_history(self, rows: list[dict]) -> int:
        before = self.conn.total_changes
        now = _now()
        for r in rows:
            self.conn.execute(
                """INSERT INTO balance_history
                       (user_id, snapshot_date, twd_balance, fx_balance, loan_balance, updated_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(user_id, snapshot_date)
                   DO UPDATE SET twd_balance=excluded.twd_balance,
                                 fx_balance=excluded.fx_balance,
                                 loan_balance=excluded.loan_balance,
                                 updated_at=excluded.updated_at""",
                (self.user_id, r.get("snapshotDate"), r.get("twdBalance"), r.get("fxBalance"),
                 r.get("loanBalance"), now),
            )
        self.conn.commit()
        return self.conn.total_changes - before

    # ---- 5. 帳戶狀態：UPSERT ----
    def upsert_accounts(self, accts: list[dict]) -> int:
        """Upsert accounts。

        新增欄位（使用者鐵律：所有爬蟲都該抓帳號級餘額）：
          - raw_balance (REAL): 帳號餘額（爬蟲層直接抓的，非 twd_txn 推算）
              0 跟 None 有意義區別：0=真實 0 餘額（顯示 $0）、None=爬不到（顯示 —）
          - raw_balance_date (TEXT): 該餘額的 snapshot 日期，ISO YYYY-MM-DD
        既有 caller 不帶這兩欄就傳 None（不覆蓋之前抓到的）— UPSERT 用 COALESCE
        保護舊值，避免某次爬蟲忘了帶 raw_balance 就把歷史餘額沖掉。
        """
        now = _now()
        for a in accts:
            if not a.get("account_no"):
                continue
            self.conn.execute(
                """INSERT INTO accounts
                       (user_id, account_no, currency, branch, nickname, type, product_type,
                        raw_balance, raw_balance_date, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(user_id, account_no) DO UPDATE SET
                     currency=excluded.currency, branch=excluded.branch, nickname=excluded.nickname,
                     type=excluded.type, product_type=excluded.product_type,
                     raw_balance=COALESCE(excluded.raw_balance, accounts.raw_balance),
                     raw_balance_date=COALESCE(excluded.raw_balance_date, accounts.raw_balance_date),
                     updated_at=excluded.updated_at""",
                (self.user_id, a.get("account_no"), a.get("currency"), a.get("branch"), a.get("nickname"),
                 a.get("type"), a.get("product_type"),
                 a.get("raw_balance"), a.get("raw_balance_date"),
                 now),
            )
        self.conn.commit()
        return len(accts)

    # ---- 6. 卡片狀態：UPSERT ----
    def upsert_cards(self, cards: list[dict]) -> int:
        """Step 2 (2026-06-14): 接 credit_limit / used_credit /
        statement_close_date / payment_due_date 四欄 (各家 collector 分階段補).
        2026-06-14 (active): 接 active 欄 (DBS isDisplayImg=False → 0).

        COALESCE 防呼: 沒帶就保留舊值, 不會被 NULL 沖掉. 同 raw_balance 模式.
        active 因 schema NOT NULL DEFAULT 1, 必須在 Python 端判斷:
          - 沒帶 active → INSERT 時用 1 (新卡預設顯示), UPDATE 時保留舊值
          - 帶 True/False → INSERT 用 1/0, UPDATE 也覆寫成 1/0
        為了 UPDATE 保留邏輯, 我們在 Python 判斷 + ON CONFLICT 用 CASE.
        """
        now = _now()
        for c in cards:
            if not c.get("number"):
                continue
            # active 兩種型態: 沒帶 / True / False
            #   沒帶 (None) → INSERT 用 schema default (1), UPDATE 保留原值
            #   True/False → INSERT/UPDATE 都用 1/0
            active_provided = "active" in c
            active_insert = 1 if (not active_provided or c.get("active")) else 0
            active_update_marker = active_insert if active_provided else None
            # ON CONFLICT 時, 若 active_update_marker=None 走 cards.active 保留;
            # 否則覆寫. 用 CASE WHEN 而不是 COALESCE 因為 0 也是合法值不能被 COALESCE 跳過.
            statement_close_date = _normalize_date_text(c.get("statement_close_date"))
            payment_due_date = _normalize_date_text(c.get("payment_due_date"))
            last_payment_date = _normalize_date_text(c.get("last_payment_date"))
            self.conn.execute(
                """INSERT INTO cards (user_id, card_no, name, association, type, is_cube,
                                      credit_limit, used_credit,
                                      statement_close_date, payment_due_date,
                                      bill_due_amount, last_payment_amount, last_payment_date,
                                      active, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(user_id, card_no) DO UPDATE SET
                     name=excluded.name, association=excluded.association, type=excluded.type,
                     is_cube=excluded.is_cube,
                     credit_limit=COALESCE(excluded.credit_limit, cards.credit_limit),
                     used_credit=COALESCE(excluded.used_credit, cards.used_credit),
                     statement_close_date=COALESCE(excluded.statement_close_date, cards.statement_close_date),
                     payment_due_date=COALESCE(excluded.payment_due_date, cards.payment_due_date),
                     bill_due_amount=COALESCE(excluded.bill_due_amount, cards.bill_due_amount),
                     last_payment_amount=COALESCE(excluded.last_payment_amount, cards.last_payment_amount),
                     last_payment_date=COALESCE(excluded.last_payment_date, cards.last_payment_date),
                     active=CASE WHEN ? IS NULL THEN cards.active ELSE ? END,
                     updated_at=excluded.updated_at""",
                (self.user_id, c.get("number"), c.get("name"), c.get("association"), c.get("type"),
                 1 if c.get("is_cube") else 0,
                 c.get("credit_limit"), c.get("used_credit"),
                 statement_close_date, payment_due_date,
                 c.get("bill_due_amount"), c.get("last_payment_amount"), last_payment_date,
                 active_insert,
                 now,
                 active_update_marker, active_update_marker),
            )
        self.conn.commit()
        return len(cards)

    # ---- 7. 每日數值快照：同日同 category 覆蓋 ----
    def put_daily_metric(self, category: str, payload, snapshot_date: str | None = None) -> None:
        if payload is None:
            return
        now = _now()
        day = snapshot_date or datetime.now().strftime("%Y-%m-%d")
        self.conn.execute(
            """INSERT INTO daily_metrics (user_id, snapshot_date, category, payload_json, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(user_id, snapshot_date, category)
               DO UPDATE SET payload_json=excluded.payload_json, updated_at=excluded.updated_at""",
            (self.user_id, day, category, json.dumps(payload, ensure_ascii=False), now),
        )
        self.conn.commit()

    def log_sync(self, summary: dict) -> None:
        self.conn.execute(
            "INSERT INTO sync_log (user_id, synced_at, summary) VALUES (?,?,?)",
            (self.user_id, _now(), json.dumps(summary, ensure_ascii=False)),
        )
        self.conn.commit()

    # ---- 查詢 ----
    def stats(self) -> dict:
        """每張表的 row count, 只算本 user 的 (Phase C 2026-06-17)."""
        out = {}
        for tbl in ["twd_transactions", "card_billed_txns", "card_pending_txns",
                    "balance_history", "accounts", "cards", "daily_metrics", "sync_log"]:
            out[tbl] = self.conn.execute(
                f"SELECT COUNT(*) FROM {tbl} WHERE user_id = ?",
                (self.user_id,),
            ).fetchone()[0]
        return out
