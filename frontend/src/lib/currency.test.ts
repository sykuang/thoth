import {
  formatCurrency,
  formatDecimalCurrency,
  formatSignedCurrency,
  renderAmount,
} from './currency';
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
assertEqual(billed.primary, '-EUR 18.60');
assertEqual(billed.sub, 'NT$ 687');

const pendingEstimate = renderAmount(
  foreignTxn({ kind: 'pending', fx_rate_source: 'bank_pending_estimate' }),
  'auto',
);
assertEqual(pendingEstimate.sub, '≈ NT$ 687');

assertEqual(formatCurrency(1234, 'TWD'), 'NT$ 1,234');
assertEqual(formatSignedCurrency(-1234, 'TWD'), '-NT$ 1,234');
assertEqual(formatSignedCurrency(1234, 'TWD', true), '+NT$ 1,234');
assertEqual(formatDecimalCurrency('42350.55', 'USD'), 'USD 42,350.55');
assertEqual(formatDecimalCurrency('-1234', 'TWD'), '-NT$ 1,234');
assertEqual(formatDecimalCurrency('42.5', ''), '42.5');
assertEqual(formatDecimalCurrency('0.12345678', 'BTC'), 'BTC 0.12345678');
assertEqual(formatDecimalCurrency('1000', 'JPY'), 'JPY 1,000');
assertEqual(formatDecimalCurrency('1000', 'CNY'), 'CNY 1,000.00');
for (const currency of ['HKD', 'AUD', 'CAD', 'SGD', 'CHF']) {
  assertEqual(formatDecimalCurrency('1000', currency), `${currency} 1,000.00`);
}
assertEqual(formatDecimalCurrency('not-a-number', 'TWD'), null);

console.log('currency display tests passed');
