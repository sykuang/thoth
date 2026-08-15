/**
 * Client-side transaction filter + stats utilities (Phase 9 C-2, 2026-06-19).
 *
 * 背景 (使用者問: 「為什麼前端針對分類做搜尋 要重打ＡＰＩ呢」):
 *   Phase 8.2 A (2026-06-14) 設計把 chip 切 filter 拆成 2 個 API call
 *   (txnsQ + statsQ), category/subcategory/direction/q 任一動就重打 2 query.
 *   個人 finance app 一個月 50-200 筆 txn, payload < 50KB, 完全可以 client hold.
 *
 * 此檔抽出純函式:
 *   - 1 個 useQuery 撈當前 period+bank+kind 的 raw items (limit=5000)
 *   - filter (category/subcategory/direction/search) 全 client useMemo
 *   - chip count (by_category/by_subcategory) 也 client useMemo
 *
 * 鐵則 (跟 backend transactions.py:579-766 對齊):
 *   - chip count 不算 auto_excluded / excluded row (避免「還款 9 筆」chip 出現)
 *   - by_subcategory 限縮到當前主類 (避免子類 chip 跨主類混雜)
 *   - monthStats 算 income/expense 時 skip excluded + auto_excluded
 *   - search 是 description case-insensitive substring (對齊 backend `q` filter)
 *
 * 不抽出來的 (server-only):
 *   - period/bank/kind 仍 server filter (since/until SQL 強項, 跨銀行 OR, kind 不同 table)
 *   - dashboard.tsx 用的 amount_by_month / subscription / passive_income / flow_type
 *     enum 邏輯 (太複雜, 且 dashboard 無 filter 重打成本=0, 維持 server stats)
 */
import type { Transaction } from '@/types/api';

// ============================================================
// Filter — 套 category/subcategory/direction/search
// ============================================================

export type TxnFilters = {
  category: string;                    // '' = 全部主類
  subcategory: string;                 // '' = 該主類全部
  direction: 'all' | 'income' | 'expense';
  search: string;                      // case-insensitive substring 對 description
};

export type TxnCashflowDirection = 'income' | 'expense' | 'zero';

/** Keep provider-level card rows visible without attributing them to one card. */
export function matchesCardDrilldown(t: Transaction, cardNo: string): boolean {
  return t.card_no === cardNo
    || ((t.kind === 'billed' || t.kind === 'pending') && !t.card_no);
}

/**
 * Transaction cash-flow direction used consistently by list filters, cards,
 * and category aggregates. This intentionally mirrors currency.ts/renderAmount:
 * refund/cashback are positive cash-flow even when bank raw amount is negative;
 * payment is neutral transfer; all other rows use amount sign.
 */
export function txnCashflowDirection(t: Transaction): TxnCashflowDirection {
  if (t.cashflow_direction === 'income' || t.cashflow_direction === 'expense') {
    return t.cashflow_direction;
  }
  if (t.cashflow_direction === 'neutral') return 'zero';
  if (t.txn_type === 'cashback' || t.txn_type === 'refund' || t.txn_type === 'fee_waiver') return 'income';
  if (t.txn_type === 'payment') return 'zero';
  const amt = t.amount ?? 0;
  if (amt > 0) return 'income';
  if (amt < 0) return 'expense';
  return 'zero';
}

/** Signed amount from the user's cash-flow perspective. */
export function txnCashflowAmount(t: Transaction): number {
  if (typeof t.cashflow_amount === 'number') {
    const dir = txnCashflowDirection(t);
    if (dir === 'income') return Math.abs(t.cashflow_amount);
    if (dir === 'expense') return -Math.abs(t.cashflow_amount);
    return 0;
  }
  const amt = t.amount ?? 0;
  const dir = txnCashflowDirection(t);
  if (dir === 'income') return Math.abs(amt);
  if (dir === 'expense') return -Math.abs(amt);
  return 0;
}

/**
 * 套 client-side filter 到 raw items.
 *
 * NULL category 用 sentinel '__null__' (跟 backend by_category chip 對齊).
 * 點「未分類」chip → category='__null__' → 這裡比對 t.category=null/undefined/''.
 */
export function applyTxnFilters(items: Transaction[], f: TxnFilters): Transaction[] {
  const searchLower = f.search.trim().toLowerCase();
  return items.filter((t) => {
    // 主類: '__null__' sentinel 代表未分類
    if (f.category) {
      if (f.category === '__null__') {
        if (t.category) return false;  // 有分類的不要
      } else if (t.category !== f.category) {
        return false;
      }
    }
    // 子類
    if (f.subcategory && t.subcategory !== f.subcategory) return false;
    // direction: amount=0 一律不算 (既不收入也不支出)
    if (f.direction !== 'all' && txnCashflowDirection(t) !== f.direction) return false;
    // search: description + tag 名稱 case-insensitive substring (2026-06-22 使用者指示).
    // 為什麼一起 match: 使用者把「日本旅遊」打成 tag 後, 在收支表搜「日本」應該也要找到,
    // 不需要記得「這筆是 tag 還是 desc」. tag list 通常 0-5 個, 全掃成本 = 0.
    // 對齊 backend `q` filter 行為: server 端只 match desc, 此處是 client 補強 (期間內 row
    // 已全載到 rawItems, 多 match tag 不會增加 API call).
    if (searchLower) {
      const desc = (t.description ?? '').toLowerCase();
      const hitDesc = desc.includes(searchLower);
      const hitTag = (t.tags ?? []).some((tag) =>
        tag.toLowerCase().includes(searchLower),
      );
      if (!hitDesc && !hitTag) return false;
    }
    return true;
  });
}

/**
 * Category mode ignores detail-only category/search filters, but the visible
 * income/expense cards still define the scope shared by both view modes.
 * Neutral transfers remain visible only when no direction is selected.
 */
export function filterCategoryViewItems(
  items: Transaction[],
  direction: TxnFilters['direction'],
): Transaction[] {
  if (direction === 'all') return items;
  return items.filter((item) => txnCashflowDirection(item) === direction);
}

export function transactionSectionTitle(direction: TxnFilters['direction']): string {
  if (direction === 'income') return '收入明細';
  if (direction === 'expense') return '支出明細';
  return '收支明細';
}

// ============================================================
// Chip count aggregator — by_category / by_subcategory
// ============================================================

/**
 * 算當前 raw items 的 by_category (chip 來源 + count).
 *
 * 鐵則 (對齊 backend transactions.py:656-661):
 *   - skip excluded + auto_excluded row
 *   - NULL category 用 '__null__' sentinel (UI 顯示「未分類」)
 *
 * 回傳 dict (跟 backend by_category response shape 一致), 按 count desc sort 由 caller 用 Object.entries 排序.
 */
export function aggregateByCategory(items: Transaction[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const t of items) {
    if (t.excluded === true || t.auto_excluded === true) continue;
    const key = t.category || '__null__';
    out[key] = (out[key] ?? 0) + 1;
  }
  return out;
}

/**
 * 算當前 raw items 的 by_subcategory, 可選限縮到指定主類.
 *
 * 鐵則 (對齊 backend transactions.py:662-666):
 *   - skip excluded + auto_excluded
 *   - 只 aggregate 屬於 currentCategory 的 row (避免子類 chip 跨主類混雜)
 *   - subcategory NULL/空字串 不計 (跟 backend `sub and (...)` 判斷對齊)
 *
 * 如果 currentCategory='' (未選主類), 子類 chip 不該出現, 故回 {}.
 * 如果 currentCategory='__null__' (選了未分類), 同理子類 chip 不該出現, 也回 {}.
 */
export function aggregateBySubcategory(
  items: Transaction[],
  currentCategory: string,
): Record<string, number> {
  if (!currentCategory || currentCategory === '__null__') return {};
  const out: Record<string, number> = {};
  for (const t of items) {
    if (t.excluded === true || t.auto_excluded === true) continue;
    if (t.category !== currentCategory) continue;
    const sub = t.subcategory;
    if (!sub) continue;
    out[sub] = (out[sub] ?? 0) + 1;
  }
  return out;
}

// ============================================================
// Period stats (收入/支出兩張卡的金額)
// ============================================================

export type PeriodStats = {
  income: number;   // 正數
  expense: number;  // 正數 (絕對值)
  net: number;      // income - expense
  count: number;    // 入算的筆數
};

/**
 * 算當前 period 的 income/expense/net (收支表上方兩張卡).
 *
 * 鐵則 (對齊 backend transactions.py:678-720 amount_by_month 邏輯 + frontend
 * lib/currency.ts:80-98 renderAmount 邏輯):
 *   - skip excluded + auto_excluded (不算金額也不算 count)
 *   - cashback / refund / fee_waiver → 一律算 income (取 abs, raw amount 可能是負 — 信用卡退款
 *     / 年費減免從帳單視角是負, 對使用者是正向現金流)
 *   - payment → 既不算 income 也不算 expense (還錢本身是 transfer, 不是收支)
 *   - spending / fee / annual_fee / installment / unknown / null → 純看 amount 符號
 *
 * Bug 修正 (2026-06-22 使用者指示):
 *   前版「簡化版純看符號」的假設 (cashback amount 已正、payment 已 auto_excluded)
 *   對 HSBC 退稅 (TaxRefund / Globalblue) row 不成立 — 那些 row amount<0 +
 *   txn_type=refund + auto_excluded=0, 使用者要算進 income 卻被簡化版算進 expense.
 *   單行 renderAmount 顯示綠色「+NT$ 622」但下方統計卡片算成 expense -622, 割裂.
 *   修法 = 完全對齊 renderAmount 的 txn_type-aware 邏輯, 行裡顯示什麼方向、統計卡
 *   就算什麼方向.
 */
export function computePeriodStats(items: Transaction[]): PeriodStats {
  let income = 0;
  let expense = 0;
  let count = 0;
  for (const t of items) {
    if (t.excluded === true || t.auto_excluded === true) continue;
    const signed = txnCashflowAmount(t);
    if (signed !== 0) count += 1;
    if (signed > 0) {
      income += signed;
    } else if (signed < 0) {
      expense += -signed;
    }
  }
  return { income, expense, net: income - expense, count };
}
