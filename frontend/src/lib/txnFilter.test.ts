import type { Transaction } from '@/types/api';
import { filterCategoryViewItems, transactionSectionTitle } from './txnFilter';

function deepEqual(actual: unknown, expected: unknown, message: string): void {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`${message}\nexpected: ${JSON.stringify(expected)}\nactual: ${JSON.stringify(actual)}`);
  }
}

function txn(
  id: number,
  category: string,
  cashflowDirection: 'income' | 'expense' | 'neutral',
): Transaction {
  return {
    id,
    bank: 'cathay',
    kind: 'twd',
    date: '2026-08-10',
    datetime: null,
    description: category,
    amount: cashflowDirection === 'income' ? 100 : cashflowDirection === 'expense' ? -100 : 0,
    cashflow_direction: cashflowDirection,
    cashflow_amount: 100,
    currency: 'TWD',
    category,
    excluded: false,
    auto_excluded: false,
  } as Transaction;
}

const items = [
  txn(1, '薪資', 'income'),
  txn(2, '飲食', 'expense'),
  txn(3, '轉帳', 'neutral'),
];

deepEqual(
  filterCategoryViewItems(items, 'income').map((item) => item.category),
  ['薪資'],
  '收入分類檢視只能聚合收入交易',
);
deepEqual(
  filterCategoryViewItems(items, 'expense').map((item) => item.category),
  ['飲食'],
  '支出分類檢視只能聚合支出交易',
);
deepEqual(
  filterCategoryViewItems(items, 'all').map((item) => item.category),
  ['薪資', '飲食', '轉帳'],
  '未選方向時保留整個期間的分類分佈',
);
deepEqual(
  [transactionSectionTitle('income'), transactionSectionTitle('expense'), transactionSectionTitle('all')],
  ['收入明細', '支出明細', '收支明細'],
  '區段標題必須反映收入/支出 scope',
);

console.log('transaction category view filter tests passed');
