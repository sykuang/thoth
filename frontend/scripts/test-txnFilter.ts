// Phase 9 C-2 (2026-06-19): standalone smoke test for txnFilter.ts pure functions.
// frontend 沒裝 jest, 但這幾個 pure function 邏輯關鍵, 寫個 minimal node script
// 走過所有 edge case (對齊 backend transactions.py:579-766). 跑法:
//   cd ~/src/thoth/frontend && npx tsx scripts/test-txnFilter.ts
//   (no tsx? 直接 npx ts-node 也可, 或臨時轉 .js)
import {
  applyTxnFilters,
  aggregateByCategory,
  aggregateBySubcategory,
  computePeriodStats,
} from '../src/lib/txnFilter';
import type { Transaction } from '../src/types/api';

// Minimal Transaction factory
function t(overrides: Partial<Transaction>): Transaction {
  return {
    id: 1,
    bank: 'ctbc',
    kind: 'billed',
    date: '2026-06-15',
    description: 'test',
    amount: -100,
    currency: 'TWD',
    category: null,
    subcategory: null,
    txn_type: null,
    flow_type: null,
    is_subscription: false,
    income_category: null,
    excluded: false,
    auto_excluded: false,
    ...overrides,
  } as Transaction;
}

let pass = 0;
let fail = 0;
function check(name: string, cond: boolean, detail?: string) {
  if (cond) {
    pass++;
    console.log(`  ✓ ${name}`);
  } else {
    fail++;
    console.error(`  ✗ ${name}${detail ? ` — ${detail}` : ''}`);
  }
}

// ============================================================
// applyTxnFilters
// ============================================================
console.log('\n== applyTxnFilters ==');
{
  const items = [
    t({ id: 1, category: '飲食', subcategory: '早餐', amount: -50, description: '早安美芝城' }),
    t({ id: 2, category: '飲食', subcategory: '晚餐', amount: -300, description: '鼎泰豐' }),
    t({ id: 3, category: '交通', subcategory: '計程車', amount: -250, description: 'Uber' }),
    t({ id: 4, category: null, amount: -88, description: '中華電信' }),
    t({ id: 5, category: '飲食', amount: -120, description: 'starbucks咖啡', subcategory: null }),
    t({ id: 6, category: '飲食', amount: 200, description: '退款 鼎泰豐' }),  // income (refund)
  ];

  // 空 filter = 全留
  check('empty filter → all 6',
    applyTxnFilters(items, { category: '', subcategory: '', direction: 'all', search: '' }).length === 6);

  // category 飲食 → 4 筆
  check('category=飲食 → 4',
    applyTxnFilters(items, { category: '飲食', subcategory: '', direction: 'all', search: '' }).length === 4);

  // category=__null__ (未分類)
  check('category=__null__ → 1 (only item 4)',
    applyTxnFilters(items, { category: '__null__', subcategory: '', direction: 'all', search: '' }).length === 1);

  // subcategory 早餐 (只在飲食底下)
  check('category=飲食 + subcategory=早餐 → 1',
    applyTxnFilters(items, { category: '飲食', subcategory: '早餐', direction: 'all', search: '' }).length === 1);

  // direction=income (amt > 0)
  check('direction=income → 1 (item 6 refund)',
    applyTxnFilters(items, { category: '', subcategory: '', direction: 'income', search: '' }).length === 1);

  // direction=expense (amt < 0)
  check('direction=expense → 5',
    applyTxnFilters(items, { category: '', subcategory: '', direction: 'expense', search: '' }).length === 5);

  // search case-insensitive
  check('search="STARBUCKS" → 1',
    applyTxnFilters(items, { category: '', subcategory: '', direction: 'all', search: 'STARBUCKS' }).length === 1);

  // search 對齊 backend `q` filter (description substring)
  check('search="鼎泰豐" → 2',
    applyTxnFilters(items, { category: '', subcategory: '', direction: 'all', search: '鼎泰豐' }).length === 2);

  // 複合 filter
  check('category=飲食 + direction=expense + search="泰" → 1',
    applyTxnFilters(items, { category: '飲食', subcategory: '', direction: 'expense', search: '泰' }).length === 1);
}

// ============================================================
// aggregateByCategory
// ============================================================
console.log('\n== aggregateByCategory ==');
{
  const items = [
    t({ id: 1, category: '飲食' }),
    t({ id: 2, category: '飲食' }),
    t({ id: 3, category: '交通' }),
    t({ id: 4, category: null }),         // → '__null__'
    t({ id: 5, category: '飲食', excluded: true }),       // skip
    t({ id: 6, category: '購物', auto_excluded: true }),  // skip
  ];
  const r = aggregateByCategory(items);
  check('飲食=2', r['飲食'] === 2, `got ${r['飲食']}`);
  check('交通=1', r['交通'] === 1, `got ${r['交通']}`);
  check('__null__=1', r['__null__'] === 1, `got ${r['__null__']}`);
  check('購物 not present (auto_excluded)', !('購物' in r));
}

// ============================================================
// aggregateBySubcategory
// ============================================================
console.log('\n== aggregateBySubcategory ==');
{
  const items = [
    t({ id: 1, category: '飲食', subcategory: '早餐' }),
    t({ id: 2, category: '飲食', subcategory: '早餐' }),
    t({ id: 3, category: '飲食', subcategory: '晚餐' }),
    t({ id: 4, category: '飲食', subcategory: null }),         // sub=NULL skip
    t({ id: 5, category: '交通', subcategory: '計程車' }),     // 跨主類 skip
    t({ id: 6, category: '飲食', subcategory: '早餐', excluded: true }),  // skip
  ];
  // currentCategory='飲食' → 限縮
  const r1 = aggregateBySubcategory(items, '飲食');
  check('飲食.早餐=2', r1['早餐'] === 2, `got ${r1['早餐']}`);
  check('飲食.晚餐=1', r1['晚餐'] === 1);
  check('飲食.計程車 not present (跨主類)', !('計程車' in r1));

  // currentCategory='' → 不顯示子類 chip
  const r2 = aggregateBySubcategory(items, '');
  check('category="" → {} (no chip)', Object.keys(r2).length === 0);

  // currentCategory='__null__' → 同理
  const r3 = aggregateBySubcategory(items, '__null__');
  check('category=__null__ → {} (no chip)', Object.keys(r3).length === 0);
}

// ============================================================
// computePeriodStats
// ============================================================
console.log('\n== computePeriodStats ==');
{
  const items = [
    t({ id: 1, amount: -300 }),    // expense
    t({ id: 2, amount: -200 }),    // expense
    t({ id: 3, amount: 50000 }),   // income (salary)
    t({ id: 4, amount: -100, excluded: true }),       // skip
    t({ id: 5, amount: -100, auto_excluded: true }),  // skip (還款)
    t({ id: 6, amount: 0 }),       // 0 → count++ 但不進 income/expense
  ];
  const r = computePeriodStats(items);
  check('income=50000', r.income === 50000, `got ${r.income}`);
  check('expense=500', r.expense === 500, `got ${r.expense}`);
  check('net=49500', r.net === 49500, `got ${r.net}`);
  check('count=4 (skip 2 excluded/auto_excluded)', r.count === 4, `got ${r.count}`);
}

// ============================================================
// Summary
// ============================================================
console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
