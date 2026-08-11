import type { Transaction } from '@/types/api';

import { computeLocalDashboardStats } from './localStats';

function deepEqual(actual: unknown, expected: unknown): void {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

const transactions: Transaction[] = [
  {
    id: 1, bank: 'cathay', kind: 'twd', date: '2026-08-01', datetime: null,
    description: '配息', amount: 100, cashflow_direction: 'income', cashflow_amount: 100,
    currency: 'TWD', category: '收入', account_or_card: null, excluded: false,
    auto_excluded: false, flow_type: 'income', income_category: 'interest_dividend',
  },
  {
    id: 2, bank: 'cathay', kind: 'billed', date: '2026-08-02', datetime: null,
    consume_date: '2026-08-02', post_date: '2026-09-01', description: '訂閱', amount: -20,
    cashflow_direction: 'expense', cashflow_amount: 20, currency: 'TWD', category: '訂閱',
    account_or_card: null, excluded: false, auto_excluded: false, flow_type: 'expense',
    is_subscription: true,
  },
  {
    id: 3, bank: 'ubot', kind: 'twd', date: '2026-08-03', datetime: null,
    description: '中性', amount: 0, cashflow_direction: 'neutral', cashflow_amount: 0,
    currency: 'TWD', category: null, account_or_card: null, excluded: false,
    auto_excluded: false, flow_type: 'transfer',
  },
  {
    id: 4, bank: 'ubot', kind: 'twd', date: '2026-08-04', datetime: null,
    description: '排除', amount: -50, cashflow_direction: 'expense', cashflow_amount: 50,
    currency: 'TWD', category: '其他', account_or_card: null, excluded: true,
    auto_excluded: false, flow_type: 'expense',
  },
];

const stats = computeLocalDashboardStats(transactions, 'consume');
deepEqual(stats, {
  total: 4,
  total_income: 100,
  total_expense: 20,
  total_net: 80,
  amount_by_month: {
    '2026-08': { income: 100, expense: 20, net: 80, count: 3 },
  },
  amount_by_category: { 訂閱: 20 },
  by_kind: { twd: 3, billed: 1 },
  amount_by_flow_type: { expense: 20, income: 100, transfer: 0, investment: 0 },
  subscription_total: 20,
  subscription_by_month: { '2026-08': 20 },
  amount_by_income_category: {
    salary: 0, bonus: 0, interest_dividend: 100, investment_gain: 0, other: 0,
  },
  passive_income_total: 100,
  passive_income_by_month: { '2026-08': 100 },
  passive_income_pct: 100,
  income_unclassified_count: 0,
});

const postStats = computeLocalDashboardStats(transactions, 'post');
deepEqual(postStats.amount_by_month, {
  '2026-09': { income: 0, expense: 20, net: -20, count: 1 },
  '2026-08': { income: 100, expense: 0, net: 100, count: 2 },
});

const tiePercent = computeLocalDashboardStats([
  {
    ...transactions[0], id: 10, amount: 1, cashflow_amount: 1,
  },
  {
    ...transactions[0], id: 11, amount: 15, cashflow_amount: 15,
    income_category: 'salary',
  },
], 'consume');
deepEqual(tiePercent.passive_income_pct, 6.2);

console.log('local dashboard stats parity tests passed');
