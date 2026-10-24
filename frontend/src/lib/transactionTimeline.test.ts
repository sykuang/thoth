import type { BankTransaction, BrokerageAccount, BrokerageActivity, Transaction } from '@/types/api';
import { isTransactionInPeriod, isTransactionVisibleForBasis, mergeTransactionTimeline, transactionDateForBasis } from './transactionTimeline';
import { projectReplicaDataset, type ReplicaEnvelope } from './replica';
import { computeLocalDashboardStats } from './localStats';

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
} as BankTransaction;
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
for (const kind of ['billed', 'pending'] as const) {
  for (const post_date of [null, undefined, '', '   ']) {
    const unposted = { ...emptyPostCard, kind, post_date };
    if (transactionDateForBasis(unposted, 'post') !== '') {
      throw new Error('post basis must not recognize a card without a real post date');
    }
    if (transactionDateForBasis(unposted, 'consume') !== '2026-07-31') {
      throw new Error('consume basis must retain unposted cards');
    }
    const undatedBank = { ...bankRows[0], date: null };
    const postedPending = { ...crossMonthCard, kind: 'pending' as const, id: 10 };
    const timeline = mergeTransactionTimeline([unposted, undatedBank, postedPending], brokerageRows, accounts, 'post');
    if (timeline.length !== 4 || !timeline.some(row => row.source === 'bank' && row.transaction === postedPending)
      || timeline.some(row => row.source === 'bank' && row.transaction === unposted)) {
      throw new Error('post timeline must hide only unposted cards, retaining posted pending, undated TWD and brokerage');
    }
  }
}

// Replica date is a display fallback in older envelopes, not consumption evidence.
// Exercise the actual projection and dashboard aggregation, including split children.
for (const kind of ['billed', 'pending'] as const) {
  for (const consume_date of [null, undefined, '', '   ']) {
    for (const splits of [[], [
      { amount: 150, category: '飲食', subcategory: null },
      { amount: 250, category: '交通', subcategory: null },
    ]]) {
      const envelope = {
        partitions: { 'bank:cathay': { transactions: [{
          id: 101, bank: 'cathay', kind, date: '2026-06-24', consume_date,
          post_date: '2026-06-24', amount: -400, currency: 'TWD',
          cashflow_direction: 'expense', cashflow_amount: 400, flow_type: 'expense', splits,
        }] } }, generations: {}, syncedAt: '2026-06-24',
      } as unknown as ReplicaEnvelope;
      const rows = projectReplicaDataset(JSON.parse(JSON.stringify(envelope))).transactions;
      if (rows.length !== (splits.length || 1)) throw new Error('split projection lost rows');
      for (const row of rows) {
        if (row.consume_date != null && row.consume_date.trim() !== '') throw new Error('projection fabricated consume date');
        if (transactionDateForBasis(row, 'consume') !== '') {
          throw new Error('post-only replica row must not acquire a consumption date from display fallback');
        }
        if (isTransactionVisibleForBasis(row, 'consume')) throw new Error('post-only card visible in consume mode');
        for (const bounds of [[], ['2026-06-01'], [undefined, '2026-06-30'], ['2026-06-01', '2026-06-30']] as const) {
          if (isTransactionInPeriod(row, 'consume', ...bounds) || !isTransactionInPeriod(row, 'post', ...bounds)) {
            throw new Error('post-only split/date basis parity failed for one-sided or unbounded period');
          }
        }
      }
      const consume = computeLocalDashboardStats(rows, 'consume');
      if (JSON.stringify(consume) !== JSON.stringify(computeLocalDashboardStats([], 'consume'))) {
        throw new Error('consume stats must exclude post-only cards before all aggregation');
      }
      if (mergeTransactionTimeline(rows, [], [], 'consume').length) throw new Error('consume timeline contains post-only cards');
      const post = computeLocalDashboardStats(rows, 'post');
      if (post.total !== rows.length || post.total_expense !== 400 || post.amount_by_month['2026-06']?.expense !== 400) {
        throw new Error('post-only card must retain its actual posting month and entire split amount');
      }
      if (mergeTransactionTimeline(rows, [], [], 'post').length !== rows.length) throw new Error('post timeline lost rows');
    }
  }
}

// A single predicate also guards upper-only/unbounded ranges, where '' can
// otherwise pass a lexical comparison despite the chosen card date being absent.
const siblingRows = projectReplicaDataset({
  partitions: { 'bank:cathay': {
    transactions: [
      {id: 201, bank:'manual', kind:'twd', date:'2026-06-24', consume_date:null, post_date:null,
        amount:-25, currency:'TWD', cashflow_direction:'expense', cashflow_amount:25, flow_type:'expense'},
      {id:202, bank:'cathay', kind:'billed', txn_type:'payment', date:'2026-06-24',
        consume_date:'2026-06-24', post_date:'2026-06-24', amount:500, currency:'TWD',
        cashflow_direction:'neutral', cashflow_amount:500, flow_type:'transfer'},
    ],
    loan_repayments: [{id:'loan:v1:date-basis', bank:'cathay', source_account_id:1,
      account_no:'synthetic', sub_account:'', currency:'TWD', due_date:'2026-06-24',
      paid_on:'2026-06-24', status:'paid', query_start:null, query_end:null,
      principal:'100', interest:'10', penalty:'2', paid_total:'112', principal_balance:'900'}],
  } }, generations:{}, syncedAt:'2026-06-24',
} as unknown as ReplicaEnvelope).transactions;
for (const basis of ['consume', 'post'] as const) {
  const stats = computeLocalDashboardStats(siblingRows, basis);
  if (stats.total !== siblingRows.length || String(stats.total_expense) !== '37'
    || String(stats.total_income) !== '0' || !stats.loan_by_currency) throw new Error('manual/payment/loan stats changed with card basis');
  if (mergeTransactionTimeline(siblingRows, [], [], basis).length !== siblingRows.length) throw new Error('sibling timeline lost rows');
  for (const bounds of [[], ['2026-06-01'], [undefined, '2026-06-30'], ['2026-06-01', '2026-06-30']] as const) {
    const noDate = { ...crossMonthCard, date: '2026-06-24', consume_date: null, post_date: null };
    if (isTransactionInPeriod(noDate, basis, ...bounds)) throw new Error('missing chosen card date leaked into a period');
    for (const txn_type of ['payment', 'spending'] as const) {
      const card = { ...noDate, txn_type, consume_date: '2026-06-23', post_date: '2026-06-24' };
      if (!isTransactionInPeriod(card, basis, ...bounds)) throw new Error('dated payment or purchase lost');
    }
    if (!siblingRows.every(row => isTransactionInPeriod(row, basis, ...bounds))) throw new Error('projected manual/payment/loan sibling lost to date bounds');
  }
  const before = { ...crossMonthCard, consume_date: '2026-05-31', post_date: '2026-05-31' };
  const after = { ...before, consume_date: '2026-07-01', post_date: '2026-07-01' };
  if (isTransactionInPeriod(before, basis, '2026-06-01')
    || isTransactionInPeriod(after, basis, undefined, '2026-06-30')) throw new Error('one-sided bounds ignored');
  const undated = { ...bankRows[0], date: null };
  if (!isTransactionInPeriod(undated, basis)) throw new Error('unbounded non-card history lost');
  if (isTransactionInPeriod(undated, basis, undefined, '2026-06-30')) throw new Error('undated row assigned to period');
}

// Direct pre-replica server inputs may omit the fact field entirely. Only that
// legacy shape can use date; an explicitly present unknown fact is authoritative.
for (const kind of ['billed', 'pending'] as const) {
  const legacy = { ...crossMonthCard, kind, date: '2026-06-24', post_date: '2026-07-02' };
  delete legacy.consume_date;
  if (transactionDateForBasis(legacy, 'consume') !== '2026-06-24'
    || !isTransactionInPeriod(legacy, 'consume', '2026-06-01', '2026-06-30')) {
    throw new Error('legacy omitted consume_date must retain date-only compatibility');
  }
  for (const consume_date of [null, undefined, '', '  ']) {
    const authoritative = { ...legacy, consume_date };
    if (transactionDateForBasis(authoritative, 'consume') !== ''
      || isTransactionVisibleForBasis(authoritative, 'consume')) {
      throw new Error('explicit unknown consume_date must not fall back to date/post_date');
    }
  }
}

console.log('transaction timeline checks passed');