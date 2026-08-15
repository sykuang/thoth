import type { BrokerageAccount, BrokerageActivity, Transaction } from '@/types/api';
import { mergeTransactionTimeline, transactionDateForBasis } from './transactionTimeline';

const bankRows = [
  { id: 1, bank: 'ctbc', kind: 'twd', date: '2026-08-07', datetime: null },
  { id: 2, bank: 'ctbc', kind: 'twd', date: '2026-08-05', datetime: null },
] as Transaction[];
const brokerageRows = [
  { id: 'a', account_id: 'acct', trade_date: '2026-08-08', settlement_date: null, amount: '9007199254740993.01' },
  { id: 'b', account_id: 'acct', trade_date: '2026-08-06', settlement_date: null, amount: '12.34' },
] as BrokerageActivity[];
const accounts = [
  { id: 'acct', institution_name: 'Interactive Brokers', name: 'Individual', number: '8091' },
] as BrokerageAccount[];

const merged = mergeTransactionTimeline(bankRows, brokerageRows, accounts);
const keys = merged.map((row) => row.key).join(',');
if (keys !== 'brokerage:acct:a,bank:ctbc:twd:1,brokerage:acct:b,bank:ctbc:twd:2') {
  throw new Error(`unexpected timeline order: ${keys}`);
}
const first = merged[0];
if (first.source !== 'brokerage' || first.activity.amount !== '9007199254740993.01') {
  throw new Error('brokerage decimal string was not preserved');
}

const timezoneMerged = mergeTransactionTimeline(
  [
    { id: 3, bank: 'ctbc', kind: 'twd', date: '2026-08-08', datetime: '2026-08-08T23:30:00+0800' },
    { id: 4, bank: 'ctbc', kind: 'twd', date: '2026-08-08', datetime: '2026-08-08 23:45:00' },
    { id: 5, bank: 'ctbc', kind: 'twd', date: '2026-08-09', datetime: '2026-08-08T16:30:00Z' },
  ] as Transaction[],
  [
    { id: 'c', account_id: 'acct', trade_date: '2026-08-08T16:00:00Z', settlement_date: null },
    { id: 'd', account_id: 'acct', trade_date: '2026-08-08T23:00:00Z', settlement_date: null },
  ] as BrokerageActivity[],
  accounts,
);
const timezoneKeys = timezoneMerged.map((row) => row.key).join(',');
if (timezoneKeys !== 'bank:ctbc:twd:5,brokerage:acct:d,brokerage:acct:c,bank:ctbc:twd:4,bank:ctbc:twd:3') {
  throw new Error(`unexpected timezone-aware order: ${timezoneKeys}`);
}

const stableFallback = mergeTransactionTimeline(
  [{ id: 6, bank: 'ctbc', kind: 'twd', date: '2026-08-10', datetime: null }] as Transaction[],
  [{ id: 'e', account_id: 'acct', trade_date: '2026-08-10T20:00:00Z', settlement_date: null }] as BrokerageActivity[],
  accounts,
);
if (stableFallback.map((row) => row.key).join(',') !== 'bank:ctbc:twd:6,brokerage:acct:e') {
  throw new Error('date-only rows did not preserve stable input order');
}

const crossMonthCard = {
  id: 9,
  bank: 'cathay',
  kind: 'billed',
  date: '2026-07-31',
  consume_date: '2026-07-31',
  post_date: '2026-08-02',
  amount: -100,
  currency: 'TWD',
} as Transaction;
if (transactionDateForBasis(crossMonthCard, 'consume') !== '2026-07-31') {
  throw new Error('consume-date basis regressed');
}
if (transactionDateForBasis(crossMonthCard, 'post') !== '2026-08-02') {
  throw new Error('post-date basis regressed');
}
const postBasisTimeline = mergeTransactionTimeline(
  [
    { id: 8, bank: 'cathay', kind: 'twd', date: '2026-08-01', amount: -1, currency: 'TWD' },
    crossMonthCard,
  ] as Transaction[],
  [],
  [],
  'post',
);
if (postBasisTimeline[0].source !== 'bank' || postBasisTimeline[0].transaction.id !== 9) {
  throw new Error('post-date timeline ordering regressed');
}
const emptyPostCard = { ...crossMonthCard, post_date: '   ' };
if (transactionDateForBasis(emptyPostCard, 'post') !== '2026-07-31') {
  throw new Error('empty post-date must fall back to consumption date');
}

console.log('transaction timeline checks passed');