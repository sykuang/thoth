import { renderAmount } from './currency';
import type { Transaction } from '@/types/api';

function assertEqual(actual: string | null, expected: string | null): void {
  if (actual !== expected) throw new Error(`expected ${expected}, got ${actual}`);
}

function foreignTxn(overrides: Partial<Transaction>): Transaction {
  return {
    amount: -687,
    currency: 'TWD',
    consume_currency: 'EUR',
    consume_amount: 18.6,
    cashflow_direction: 'expense',
    ...overrides,
  } as Transaction;
}

const billed = renderAmount(
  foreignTxn({ kind: 'billed', fx_rate_source: 'bank_billed' }),
  'auto',
);
assertEqual(billed.primary, '-€ 18.60');
assertEqual(billed.sub, 'NT$ 687');

const pendingEstimate = renderAmount(
  foreignTxn({ kind: 'pending', fx_rate_source: 'bank_pending_estimate' }),
  'auto',
);
assertEqual(pendingEstimate.sub, '≈ NT$ 687');

console.log('currency display tests passed');
