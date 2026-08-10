/**
 * Phase 1 backend response shapes.
 *
 * Sources of truth:
 *   - backend/server/routers/auth.py        (register/login)
 *   - backend/server/routers/credentials.py (list/put/delete)
 *   - backend/server/routers/sync.py        (trigger/list/get)
 *   - backend/server/app.py                 (/auth/me, /healthz)
 */

export type RegisterResponse = {
  token: string;
  /** L9: access_token (= token, alias) */
  access_token?: string;
  /** L9: long-lived refresh token (絕對不要寫進 log!) */
  refresh_token?: string;
  /** L9: access token TTL 秒數 */
  expires_in?: number;
  user_id: number;
  email: string;
};

export type LoginResponse = {
  access_token: string;
  token_type: string;
  /** L9: long-lived refresh token (絕對不要寫進 log!) */
  refresh_token?: string;
  /** L9: access token TTL 秒數 */
  expires_in?: number;
};

export type RefreshResponse = {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
};

export type Me = {
  id: number;
  email: string;
  created_at: string;
};

export type BankCredentialMeta = {
  bank: string;
  has_creds: boolean;
  fields_set: string[];
};

// L5-1: 一個 user 在同一銀行可以有多個 account (主帳 / 老婆 / 公司)
export type BankAccount = {
  id: number;
  bank: SupportedBank;
  label: string;
  created_at: string;
  updated_at: string;
  has_creds: boolean;
  fields_set: string[];
};

// L13 (2026-06-23 使用者指示): 自動同步 — 每個 user 一個 daily 時間,
// fire 時 fan-out 該 user 全部 has_creds account.
// 取代 L12 per-account 設計 (使用者「我要使用者設定一個時間給所有帳號」).
export type SyncPreference = {
  user_id: number;
  hour: number;       // 0-23
  minute: number;     // 0-59
  tz: string;         // 'Asia/Taipei'
  enabled: boolean;
  last_run_at: string | null;
  created_at: string;
  updated_at: string;
};

export type SyncJob = {
  id: number;
  user_id: number;
  bank: string;
  account_id: number | null;   // L5-1: 舊 job 為 null
  status: 'queued' | 'running' | 'done' | 'failed';
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  error_msg: string | null;
  result_summary: string | null;
};

export type TriggerSyncResponse = {
  job_id: number;
  bank: string;
  status: string;
  account_id?: number;   // L5-1 新路徑會帶
  label?: string;
};

// ---------------------------------------------------------------------------
// Phase 5 — /transactions API (跨銀行聚合)
// ---------------------------------------------------------------------------

export type TransactionKind = 'twd' | 'billed' | 'pending';

/**
 * Phase 10 (2026-07-29) — 分類拆帳的單一子項。
 *
 * `amount` 一律正數 (絕對值), 方向沿用母筆 cashflow_direction —
 * 一筆交易不可能同時有收入與支出子項, 那是兩筆不同交易而非拆帳。
 * `auto_excluded` 讓使用者對「這一份」單獨決定要不要納入收支統計
 * (e.g. 一筆 1200 的家庭採買, 其中 400 是幫同事代墊 → 該份不算自己的支出)。
 */
export type TransactionSplit = {
  /** 該份金額, 正整數. 所有子項相加必須等於母筆 |cashflow_amount|. */
  amount: number;
  category: string | null;
  subcategory?: string | null;
  /** 該份備註, 最長 200 字 (e.g.「同事代墊」). */
  note?: string | null;
  /** true = 這一份不納入收支統計 (仍在列表可見, 反灰). */
  auto_excluded?: boolean;
};

export type Transaction = {
  /**
   * SQLite rowid in the bank's DB. Required for detail / PATCH endpoints.
   *
   * Phase 10 (2026-07-29): 分類拆帳的子項 id 是 `"{母id}#{序號}"` 字串,
   * 不可送去 detail / PATCH endpoint — 要編輯請用 `split_of` 拿母筆 id。
   * 用 `isSplitChild(t)` 判斷。
   */
  id: number | string;
  bank: string;
  kind: TransactionKind;
  date: string | null;
  datetime: string | null;
  description: string | null;
  /** Phase 8.2 — 使用者覆寫的說明 (null = 沒覆寫, 顯示原 description). */
  description_overwrite?: string | null;
  /** Raw bank/card statement perspective amount (kept for audit/backward compatibility). */
  amount: number;
  /** Normalized user cash-flow direction for filters/stats/UI. */
  cashflow_direction?: 'income' | 'expense' | 'neutral';
  /** Signed user-perspective cash-flow amount: income positive, expense negative, neutral 0. */
  cashflow_amount?: number;
  /** Absolute amount the UI should display with sign decided by cashflow_direction. */
  display_amount?: number;
  currency: string;
  category: string | null;
  /** Exact TWD account number for internal filtering; UI should keep using masked account_or_card. */
  account_no?: string | null;
  /** Exact card number / bank-provided masked card number for internal filtering. */
  card_no?: string | null;
  /** 帳號 / 卡號 末四 (前面用 * mask). */
  account_or_card: string | null;
  /** 台幣交易專屬 — 當下餘額 */
  balance?: number | null;
  /**
   * Phase 8.4 (2026-06-15): 對方帳號名稱 (twd_transactions 才有).
   * 永豐 DataText8 / 玉山等 — desc 是「交易類別」, counterparty 是真對方。
   * Backend 統一 join 進 `display_description` — frontend 直接拿那欄顯示。
   */
  counterparty_acct?: string | null;
  /** 對方銀行代號 (目前永豐沒給). */
  counterparty_bank?: string | null;
  /** 完整摘要 (含對方資訊 + 摘要代碼). 給 detail modal 全文顯示用. */
  memo?: string | null;
  /**
   * Phase 8.4 (2026-06-15): backend transform 統一 join 後的顯示字串.
   * = description (raw) + counterparty_acct 主 token (不同則 join '·').
   * Raw `description` 永遠不動 (audit/categorizer 用), UI 拿這欄。
   */
  display_description?: string | null;
  /**
   * Phase 6 (excluded) — 該帳戶被使用者標「不納入淨資產統計」.
   * 只有 twd_transactions 該帳戶能 true (信用卡走 card_no 永遠 false).
   * UI 應反灰顯示這筆 txn.
   */
  excluded?: boolean;
  /**
   * Phase 8.3 (2026-06-15) — rule auto_excluded 命中 → stats 自動 skip 收支桶.
   * 跟 excluded 等效 (UI 反灰 + 統計不算), 但 row 仍出現在 list.
   * 解決「信用卡還款/轉帳/退款/回饋」by-definition 不算收支但仍要看得到。
   */
  auto_excluded?: boolean;
  /**
   * Phase 9 (2026-06-16) — user 自定義標籤 (hashtag).
   * Backend 存在 tags_overwrite TEXT 欄 (JSON array), 透傳成 `string[]`.
   * 空陣列 = 無標籤 (NULL 與 [] 等價). 跨銀行 / 跨主類 mark.
   */
  tags?: string[];
  /**
   * Phase 10 (2026-07-29) — 分類拆帳子項 (母筆才有, [] = 未拆帳).
   * Backend 存在 splits_overwrite TEXT 欄 (JSON array), raw amount/category 不動。
   * 子項和必須等於母筆 |cashflow_amount| — PATCH 時 backend 驗, 不符回 400。
   * 注意: 列表 API 回的是「展開後的子項」, 母筆不會出現; 這欄只在
   * detail endpoint (GET /transactions/{bank}/{kind}/{id}) 拿母筆時看得到。
   */
  splits?: TransactionSplit[];
  /** 拆帳子項 — 母筆的 rowid (只有子項有此欄). */
  split_of?: number | null;
  /** 拆帳子項 — 第幾份 (0-based). */
  split_index?: number | null;
  /** 拆帳子項 — 該份備註 (e.g.「同事代墊」). */
  split_note?: string | null;
  /** 信用卡交易日期認列 (消費日 / 入帳日). */
  consume_date?: string | null;
  /** 信用卡 billed/pending — 入帳日 */
  post_date?: string | null;
  /** 信用卡 billed / pending — 外幣消費原幣 */
  consume_currency?: string | null;
  consume_amount?: number | null;
  /**
   * Phase 6 — 真實匯率 (來自銀行帳單或 CTBC 估算), 禁推算。
   * null 代表純台幣交易或 HSBC pending (未出帳).
   */
  fx_rate?: number | null;
  /**
   * Phase 6 — 匯率來源 label, 給 UI 標註可信度。
   *   'bank_billed'            → 帳單已結算的真實匯率 (含跨刷費)
   *   'bank_pending_estimate'  → CTBC pending 估算 (出帳前可能變)
   *   null                     → 無匯率資訊 (純台幣 / HSBC pending 等出帳)
   */
  fx_rate_source?: 'bank_billed' | 'bank_pending_estimate' | null;
  /** 信用卡 pending — scope */
  scope?: string | null;
  /**
   * Phase 6 (B-full) — 交易類型分類:
   *   spending     一般消費 (進 expense)
   *   cashback     刷卡金 / 現金回饋 (進 income, 即使 amount < 0)
   *   refund       商家退款 (進 income)
   *   payment      還款 / 自動扣繳 (不進 income 也不進 expense, 是 transfer)
   *   fee          國外交易手續費 / 利息 / 違約金 (進 expense)
   *   annual_fee   年費 (進 expense)
   *   fee_waiver   年費/手續費/利息減免 (進 income, 銀行退還已收費用) — 2026-07-04 新增
   *   installment  分期付款 (進 expense)
   *   unknown      無法歸類 (照符號)
   *   null         twd_transactions (台幣存款不需要分類, 已有 expend/income 分欄)
   */
  txn_type?:
    | 'spending'
    | 'cashback'
    | 'refund'
    | 'payment'
    | 'fee'
    | 'annual_fee'
    | 'fee_waiver'
    | 'installment'
    | 'unknown'
    | null;
  /**
   * Phase 6 (category taxonomy 2026-06-15) — 收支統計閘門。
   *   expense     一般支出 → 進「本月支出」KPI
   *   income      一般收入 → 進「本月收入」KPI
   *   transfer    轉帳/還款 → 都不進 (金錢只是換位置)
   *   investment  投資/贖回 → 都不進 (走 portfolio 真實估值)
   * 跟 txn_type 正交 (txn_type 是卡費行為類型, flow_type 是統計分桶).
   * 詳見 wiki [[personal-finance-transaction-category-taxonomy]]
   */
  flow_type?: 'expense' | 'income' | 'transfer' | 'investment';
  /**
   * Phase 6 (category taxonomy) — 訂閱 flag.
   * Netflix / Spotify / iCloud / ChatGPT / GitHub Copilot 等月扣 SaaS.
   * 跨多個 category, 不是單獨主類, 是橫向 flag.
   */
  is_subscription?: boolean;
  /**
   * Phase 6 (category taxonomy) — 用戶自訂子分類.
   * 主類 13/5 鎖死 (category 欄), 子類由用戶填 (e.g. "飲食/早餐").
   */
  subcategory?: string | null;
  /**
   * Phase 6 (category taxonomy) — migration audit trail.
   * 保留 migration 前的舊類名 (e.g. "轉帳" / "手續費" / "餐飲" / null),
   * 隨時可對照 mapping 是否正確 / rollback.
   */
  legacy_category?: string | null;
  /**
   * Phase 7 (Income 5 類 2026-06-15) — 收入分類.
   * 只在 flow_type='income' 才有意義, 其他 row 永遠 null.
   * 5 enum: salary / bonus / interest_dividend / investment_gain / other
   *   - 主動收入: salary (薪資/薪轉/Payroll) + bonus (獎金/三節/年終)
   *   - 被動收入: interest_dividend (利息+股息+債息) + investment_gain (證券處分/房租/資本利得)
   *   - other: 退稅/紅包/中發票/雜項
   * null = 未分類 (UI 顯示「未分類」+ tap-to-edit 改 5 enum 之一)
   * 信用卡 income row (refund/cashback) 永遠 null — by-design 不算 FIRE 意義收入.
   * 詳見 wiki [[income-classifier-and-fire-passive-income-spec]]
   */
  income_category?: 'salary' | 'bonus' | 'interest_dividend' | 'investment_gain' | 'other' | null;
  /** raw row dict, 給 detail view */
  raw?: Record<string, unknown>;
};

export type TransactionsListResponse = {
  total: number;
  items: Transaction[];
  offset: number;
  limit: number;
  stats: {
    by_bank: Record<string, number>;
    by_kind: Record<string, number>;
    banks_queried: string[];
  };
};

export type TransactionsStatsResponse = {
  total: number;
  by_bank: Record<string, number>;
  by_kind: Record<string, number>;
  by_month: Record<string, number>;
  by_category: Record<string, number>;
  /** Phase 8.2 — 按 subcategory 累計筆數 (給子類 chip 來源用, 跟隨 filter). */
  by_subcategory?: Record<string, number>;
  banks_queried: string[];
  /** Phase 6 — 完整月份金流 (income/expense/net/count), 給月份 carousel 用. */
  amount_by_month?: Record<string, { income: number; expense: number; net: number; count: number }>;
  /** Phase 6 — 按 category 累計金額 (絕對值). */
  amount_by_category?: Record<string, number>;
  total_income?: number;
  total_expense?: number;
  total_net?: number;
};

export type Card = {
  card_no: string;
  bank: string;
  name: string | null;
  /**
   * Phase 8.2 C — user 在 thoth UI 自取的卡片暱稱.
   * 鐵則: name 是銀行 API 原文 (重 sync 蓋), nickname_overwrite 是 user 覆寫 (sync 不動).
   * UI 顯示 fallback: nickname_overwrite || name.
   */
  nickname_overwrite?: string | null;
  type: string | null;
  /** 卡組織 (VISA/Master/JCB/UnionPay), 可能 NULL — Phase 6 補. */
  association?: string | null;
  /** 中信專用旗標 — Phase 6 補. */
  is_cube?: boolean | null;
  updated_at?: string | null;
  /**
   * Phase 6 (excluded) — 使用者手動標「不納入淨資產統計」.
   * - 該卡 billed/pending txn 反灰 + stats 跳過
   * - portfolio.current_month_spending 跳過該卡
   */
  excluded?: boolean;
  /** Step 2 — 信用額度 / 已用額度 / 帳單日 / 繳款截止日. */
  credit_limit?: number | null;
  used_credit?: number | null;
  available_credit?: number | null;
  statement_close_date?: string | null;
  payment_due_date?: string | null;
  /** MoneyBook-style normalized bill summary from /cards. */
  bill_due_amount?: number | null;
  unbilled_amount?: number | null;
  bill_status?: 'due' | 'paid' | 'no_payment_required' | 'overdue' | 'unknown';
  /**
   * Phase 9.4 (2026-06-16) — 帳單頁面用. 最後一次「真實繳款」.
   * 從 card_billed_txns where txn_type=payment AND flow_type=transfer 推算.
   * categorizer 已標 refund/cashback 不算 payment, 所以這欄純粹是繳款.
   */
  last_payment_date?: string | null;
  last_payment_amount?: number | null;
};

/** Card detail page (GET /cards/{bank}/{card_no}) — Phase 9.4. */
export type CardDetail = Card & {
  billed_txns: {
    date: string;
    post_date: string | null;
    amount: number;
    description: string;
    currency: string | null;
    category?: string | null;
    subcategory?: string | null;
    txn_type: string | null;
    flow_type: string | null;
  }[];
  pending_txns: {
    date: string;
    amount: number;
    description: string;
    currency: string | null;
    category?: string | null;
    subcategory?: string | null;
  }[];
  payments: {
    date: string;
    amount: number;
    description: string;
  }[];
};

export type FrontendDatasetCache = {
  cursor: string;
  accounts: BankAccountBalance[];
  cards: Card[];
  transactions: Transaction[];
};

/** All supported banks (mirrors backend/server/sync_runner.py SUPPORTED_BANKS). */
export const SUPPORTED_BANKS = [
  'cathay',
  'ctbc',
  'dbs',
  'esun',
  'fubon',
  'hsbc',
  'linebank',
  'rakuten',
  'scb',
  'scsb',
  'sinopac',
  'taishin',
  'ubot',
] as const;

export type SupportedBank = (typeof SUPPORTED_BANKS)[number];

/** Bank → required credential fields (mirrors backend/core/creds.py _attrs()). */
export const BANK_FIELDS: Record<SupportedBank, readonly string[]> = {
  cathay: ['cust_id', 'user_id', 'password'],
  ctbc: ['national_id', 'user_code', 'password'],
  dbs: ['username', 'password'],
  esun: ['national_id', 'user_code', 'password'],
  fubon: ['national_id', 'user_code', 'password'],
  hsbc: ['user_id', 'password'],
  linebank: ['national_id', 'user_code', 'password'],
  rakuten: ['national_id', 'user_code', 'password'],
  scb: ['national_id', 'username', 'password'],
  scsb: ['national_id', 'user_code', 'password'],
  sinopac: ['national_id', 'user_code', 'password'],
  taishin: ['national_id', 'user_code', 'password'],
  ubot: ['national_id', 'user_code', 'password'],
};

/** Credential field 中文 label (maintainer 指引: 跟銀行表單實際欄位名一致) */
export const BANK_FIELD_LABELS: Record<string, string> = {
  national_id: '身分證字號',
  cust_id: '身分證字號',       // 國泰用 cust_id 但實際就是身分證
  user_id: '使用者名稱',
  user_code: '使用者名稱',
  username: '使用者名稱',
  password: '密碼',
};

/** Bank 中文名 (用於 UI 顯示, 大寫 toggle 仍可從 SUPPORTED_BANKS 取) */
export const BANK_LABELS: Record<SupportedBank, string> = {
  cathay: '國泰世華',
  ctbc: '中國信託',
  dbs: '星展銀行',
  esun: '玉山銀行',
  fubon: '富邦銀行',
  hsbc: '匯豐銀行',
  linebank: 'LINE Bank',
  rakuten: '樂天國際銀行',
  scb: '渣打銀行',
  scsb: '上海商銀',
  sinopac: '永豐銀行',
  taishin: '台新銀行',
  ubot: '聯邦銀行',
};

// =====================================================================
// Phase 6 Plan A — Portfolio Summary (MoneyBook 風 dashboard)
// =====================================================================

export type DashboardStats = {
  total: number;
  total_income: number;
  total_expense: number;
  total_net: number;
  amount_by_month: Record<string, { income: number; expense: number; net: number; count: number }>;
  amount_by_category: Record<string, number>;
  by_kind: Record<string, number>;
  amount_by_flow_type?: Record<string, number>;
  subscription_total?: number;
  subscription_by_month?: Record<string, number>;
  amount_by_income_category?: Record<string, number>;
  passive_income_total?: number;
  passive_income_by_month?: Record<string, number>;
  passive_income_pct?: number;
  income_unclassified_count?: number;
};

export type PortfolioBankSummary = {
  bank: string;
  assets: number | null;
  /** 該銀行外幣帳戶 TWD 估值 sum (null = 無外幣帳戶或解不出). */
  fx_assets_twd: number | null;
  /** = card_unpaid + loan_balance. null 表示完全沒負債資料. */
  liabilities: number | null;
  /** 信用卡未繳 (上期帳單 unpaid). null = 該銀行無信用卡或解不出. */
  card_unpaid: number | null;
  /** 貸款餘額 (信貸+房貸). null = 該銀行無貸款資料. */
  loan_balance: number | null;
  current_month_spending: number;
  stale: boolean;
  as_of: string | null;
};

export type PortfolioSummary = {
  total_assets: number;
  /** 外幣帳戶 TWD 估值 sum (台銀即期買賣中間價, 6h cache). */
  fx_assets_twd: number;
  /** SnapTrade 券商帳戶總值的 TWD 估值；每帳戶只採 balance_total 一次. */
  brokerage_assets_twd: number;
  /** 手動存款與投資的獨立 TWD asset bucket；只在 total_assets_with_fx 加一次. */
  manual_assets_twd: number;
  /** 手動貸款的 TWD breakdown；已包含在 total_liabilities，不可重加. */
  manual_liabilities_twd: number;
  /** = total_assets + fx_assets_twd + brokerage_assets_twd + manual_assets_twd. */
  total_assets_with_fx: number;
  /** = total_card_unpaid + total_loan. */
  total_liabilities: number;
  /** 信用卡未繳合計 (上期帳單未繳, 不含本月已刷). */
  total_card_unpaid: number;
  /** 貸款餘額合計 (信貸+房貸+credit line). */
  total_loan: number;
  /** 本月消費 (資訊性, 不進淨資產). pending 全部 + billed 本月 consume_date sum. */
  current_month_spending: number;
  /** = total_assets - total_liabilities (TWD only, 保守). */
  net_worth: number;
  /** = total_assets_with_fx - total_liabilities (含外幣與券商估值). */
  net_worth_with_fx: number;
  as_of: string | null;
  by_bank: PortfolioBankSummary[];
  skipped: string[];
};

export type SnapTradeStatus = {
  configured: boolean;
  registered: boolean;
  connection_count: number | null;
  last_synced_at: string | null;
};

export type BrokerageAccount = {
  id: string;
  name: string;
  number: string | null;
  institution_name: string;
  brokerage_slug: string | null;
  balance_total: string | null;
  balance_currency: string | null;
  activities_supported: boolean;
  holdings_unavailable: boolean;
  transactions_last_successful_sync: string | null;
  transactions_first_transaction_date: string | null;
  synced_at: string;
};

export type BrokerageBalance = {
  account_id: string;
  currency: string;
  cash: string | null;
  buying_power: string | null;
  synced_at: string;
};

export type BrokeragePosition = {
  account_id: string;
  provider_symbol_id: string;
  symbol: string;
  description: string | null;
  asset_type: string | null;
  quantity: string;
  price: string | null;
  market_value: string | null;
  average_cost: string | null;
  currency: string | null;
  synced_at: string;
};

export type BrokerageActivity = {
  id: string;
  account_id: string;
  type: string;
  trade_date: string | null;
  settlement_date: string | null;
  symbol: string | null;
  description: string | null;
  units: string | null;
  price: string | null;
  amount: string | null;
  fee: string | null;
  currency: string | null;
  synced_at: string;
};

export type SnapTradePortfolio = {
  accounts: BrokerageAccount[];
  balances: BrokerageBalance[];
  positions: BrokeragePosition[];
  activities: BrokerageActivity[];
  last_synced_at: string | null;
};

export type FinancialAccountSource = 'manual' | 'bank_sync' | 'brokerage_sync';
export type FinancialAccountProductType =
  | 'deposit'
  | 'time_deposit'
  | 'fx_deposit'
  | 'checking'
  | 'loan'
  | 'mortgage'
  | 'credit_line'
  | 'investment'
  | 'unknown';

export type FinancialAccount = {
  id: string;
  source: FinancialAccountSource;
  source_ref: string;
  institution_name: string | null;
  name: string;
  account_ref: string | null;
  product_type: FinancialAccountProductType;
  currency: string;
  balance: string | null;
  manual_balance?: string | null;
  as_of: string | null;
  valuation_source: 'manual' | 'yahoo_finance' | 'manual_fallback' | null;
  included_in_net_worth: boolean;
  editable: boolean;
  deletable: boolean;
};

export type YahooSymbolMatch = {
  symbol: string;
  name: string;
  exchange: string | null;
  exchange_name: string | null;
  quote_type: string;
};

export type YahooQuote = {
  symbol: string;
  name: string;
  currency: string;
  exchange_name: string | null;
  quote_type: string | null;
  regular_market_price: string;
  regular_market_time: number | null;
};

export type ManualInvestmentTransaction = {
  id: number;
  account_id: string;
  kind: 'opening' | 'buy' | 'sell' | 'fee';
  occurred_on: string;
  symbol: string | null;
  quantity: string | null;
  amount: string;
  currency: string;
  note: string | null;
  created_at: string;
  updated_at: string;
};

export type ManualInvestmentHolding = {
  symbol: string;
  quantity: string;
  currency: string;
};

// =====================================================================
// Phase 5.1 — Categorization Rules
// =====================================================================

// =====================================================================
// Phase 6 — User Preferences (per-user display settings)
// =====================================================================

/**
 * 外幣顯示模式 (對應 backend preferences_router.VALID_FX_MODES)。
 *
 *   - 'auto'             外幣交易顯示原幣 + 灰 TWD 副字 (MoneyBook 風, default)
 *   - 'always_twd'       全部換算為 TWD 顯示 (適合「我只在乎台幣花多少」用戶)
 *   - 'always_original'  外幣交易只顯示原幣 (適合「我在追蹤外幣支出」用戶)
 */
export type FxDisplayMode = 'auto' | 'always_twd' | 'always_original';

export const FX_DISPLAY_MODES: { value: FxDisplayMode; label: string; hint: string }[] = [
  { value: 'auto', label: '自動', hint: '外幣顯示原幣 + 台幣副字' },
  { value: 'always_twd', label: '一律台幣', hint: '全部換算成台幣顯示' },
  { value: 'always_original', label: '一律原幣', hint: '外幣只顯示原幣不換算' },
];

/**
 * 信用卡交易明細頁 row 顯示哪一個日期 (2026-06-20 使用者指示).
 *
 *   - 'consume': 消費日 (預設, MoneyBook 風 — 看見實際刷卡那天)
 *   - 'post':    入帳日 (對帳族 — 對銀行 statement 比較順, 未爬到入帳日 fallback 消費日)
 *
 * 會一起控制明細日期、排序、月份歸屬與統計，確保選擇 post 時跨月交易歸入帳月。
 */
export type CardDateBasis = 'consume' | 'post';

export const CARD_DATE_BASIS_MODES: { value: CardDateBasis; label: string; hint: string }[] = [
  { value: 'consume', label: '消費日', hint: '用實際刷卡日認列 (MoneyBook 風)' },
  { value: 'post', label: '入帳日', hint: '用銀行入帳日認列, 跨月消費會歸入帳月' },
];

export type UserPreferences = {
  fx_display_mode: FxDisplayMode;
  /** 信用卡交易日期認列 (消費日 / 入帳日). */
  card_date_basis?: CardDateBasis;
  /** Future-proof: backend payload_json 可加任何欄位, frontend 容忍。 */
  [key: string]: unknown;
};

export type CategoryRule = {
  id: number;
  name: string;
  pattern: string;
  category: string;
  /** Phase 8.1 (2026-06-15): 子分類, null/'' 表「無子分類」(整主類 match). */
  subcategory: string | null;
  priority: number;
  enabled: number;          // SQLite 0/1
  /**
   * Phase 8.3 (2026-06-15): 命中此 rule 的 txn 在 stats 自動 skip 收支桶.
   * SQLite 0/1. 預設 0; DEFAULT_RULES 的「轉帳匯款/信用卡還款/刷卡回饋/退款退貨」
   * 4 條預設 1 (by definition 不算收支).
   */
  auto_excluded: number;
  created_at: string;
  updated_at: string;
};

export type RecategorizeResult = {
  total_rows: number;
  updated: number;
  by_bank: Record<string, number>;
};

// /portfolio/accounts — per-bank-per-account latest balance
export type BankAccountBalance = {
  bank: string;                  // 'sinopac', 'cathay', ...
  account_no: string;            // raw account number
  currency: string;              // 'TWD' / 'JPY' / 'USD' / ...
  nickname: string | null;       // accounts.nickname
  /**
   * Phase 8.2 C — user 在 thoth UI 自取的帳戶暱稱.
   * 鐵則: nickname 是銀行 API 原文 (重 sync 蓋), nickname_overwrite 是 user 覆寫 (sync 不動).
   * UI 顯示 fallback: nickname_overwrite || nickname || type || account_no.
   */
  nickname_overwrite?: string | null;
  product_type: string | null;   // 'deposit', 'loan', 'credit_line'
  type: string | null;           // 中文 nickname 補充 (e.g. '營業部DAWHO活期儲蓄存款')
  balance: number | null;        // 最新餘額 (原幣)
  snapshot_date: string | null;  // ISO date YYYY-MM-DD
  is_stale: boolean;             // > 7 天沒更新
  // Phase 6 — 外幣帳戶 → TWD 估值 (給「JPY 1,201,387 ≈ NT$ 240,277」灰副字用)
  // 規則:
  //   - currency='TWD' → twd_estimate=balance, fx_rate_used=1.0
  //   - currency!='TWD' → backend 打 fx_service.get_rate(currency)
  //   - balance=null 或 fx_service 抓不到該幣別 → 兩個都 null
  twd_estimate: number | null;   // TWD 估值 (rounded int)
  fx_rate_used: number | null;   // 用了哪個匯率 (debug + 透明度)
  /**
   * Phase 6 (excluded) — 使用者手動標「不納入淨資產統計」.
   * - true 時 portfolio summary 跳過該帳戶 (TWD 從 total_assets 扣, 外幣不算進 fx_assets_twd)
   * - 該帳戶 twd_transactions 也會被標 excluded → 收支表反灰 + stats 不算
   */
  excluded: boolean;
};

// ============================================================
// Phase L10 (2026-06-20): Credit-card auto-debit account settings
// 設計：A2 per-card-bank (一銀行底下所有卡共用一扣繳戶), B2 允許跨銀行 account,
// G4 picker filter TWD-only, H2 dashboard reminders 位於 KPI 後 / Subscription 前.
// ============================================================

export type AutoDebitSetting = {
  card_bank: string;     // 信用卡所屬銀行 ('cathay', 'ctbc', ...)
  account_bank: string;  // 扣繳戶所在銀行 (可跨)
  account_no: string;    // 扣繳戶帳號 (G4: TWD 活儲)
  updated_at: string;
};

export type EligibleAccount = {
  bank: string;
  account_no: string;
  nickname: string | null;             // API 原文 (e.g. '主存錢筒')
  nickname_overwrite: string | null;   // user 覆寫
  type: string | null;                 // '活儲', '數位存款帳戶１—１類', etc.
  raw_balance: number | null;
};

export type PaymentReminderReason = 'no_account' | 'insufficient';

export type PaymentReminder = {
  reason: PaymentReminderReason;
  card_bank: string;
  card_no: string;
  card_name: string | null;
  bill_due_amount: number;
  payment_due_date: string;     // ISO YYYY-MM-DD
  days_until_due: number;       // 0 ~ 3 (D3)
  // 只有 reason='insufficient' 才填
  account_bank: string | null;
  account_no: string | null;
  account_balance: number | null;
  shortfall: number | null;
};
