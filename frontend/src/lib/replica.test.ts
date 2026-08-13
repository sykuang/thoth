import { SUPPORTED_BANKS } from '@/types/api';

import {
  applyReplicaResponse,
  makeReplicaOwnerKey,
  projectReplicaDataset,
  type ReplicaAccountTabCache,
  type ReplicaEnvelope,
  type ReplicaResponse,
} from './replica';

function equal(actual: unknown, expected: unknown): void {
  if (!Object.is(actual, expected)) throw new Error(`expected ${String(expected)}, got ${String(actual)}`);
}

function notEqual(actual: unknown, expected: unknown): void {
  if (Object.is(actual, expected)) throw new Error(`did not expect ${String(expected)}`);
}

function deepEqual(actual: unknown, expected: unknown): void {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

function ok<T>(value: T): asserts value is NonNullable<T> {
  if (value === null || value === undefined || value === false) throw new Error('expected value');
}

const ownerKey = makeReplicaOwnerKey('https://money.example/', 'A@Example.COM');
equal(ownerKey, JSON.stringify(['https://money.example', 'A@Example.COM']));
notEqual(ownerKey, makeReplicaOwnerKey('https://money.example', 'b@example.com'));
notEqual(ownerKey, makeReplicaOwnerKey('https://other.example', 'a@example.com'));
notEqual(ownerKey, makeReplicaOwnerKey('https://money.example', 'a@example.com'));
notEqual(
  makeReplicaOwnerKey('https://money.example/API', 'A@Example.COM'),
  makeReplicaOwnerKey('https://money.example/api', 'A@Example.COM'),
);
equal(makeReplicaOwnerKey('https://guest@money.example/', 'A@Example.COM').includes('guest'), false);

const bootstrap: ReplicaResponse = {
  schema_version: 2,
  owner_id: 7,
  reset_required: false,
  generations: { user: 1, 'bank:cathay': 1 },
  partitions: [
    { name: 'user', generation: 1, data: {
      preferences: { fx_display_mode: 'always_original', card_date_basis: 'post' },
    } },
    {
      name: 'bank:cathay',
      generation: 1,
      data: {
        accounts: [{ bank: 'cathay', account_no: '1234', currency: 'TWD', excluded: false }],
        cards: [{ bank: 'cathay', card_no: '9999', name: 'Card' }],
        transactions: [
          {
            id: 1,
            bank: 'cathay',
            kind: 'billed',
            date: '2026-08-09',
            datetime: null,
            description: '午餐',
            amount: -100,
            cashflow_direction: 'expense',
            cashflow_amount: 100,
            currency: 'TWD',
            category: '飲食',
            card_no: '123456789999',
            excluded: false,
            auto_excluded: false,
            tags: ['午餐'],
            splits: [
              { amount: 40, category: '飲食', subcategory: '自付', auto_excluded: false },
              { amount: 60, category: '其他', subcategory: null, auto_excluded: true },
            ],
          },
          {
            id: 2,
            bank: 'cathay',
            kind: 'twd',
            date: '2026-08-08',
            datetime: '2026-08-08T12:00:00',
            description: '轉帳',
            counterparty_acct: '御膳房 001',
            display_description: '轉帳 · 御膳房',
            amount: -80,
            cashflow_direction: 'expense',
            cashflow_amount: 80,
            currency: 'TWD',
            category: '轉帳',
            account_no: '12345',
            excluded: false,
            auto_excluded: false,
            tags: [' 御膳 ', '', '御膳', '月結'],
            splits: [{ amount: 80, category: '轉帳', auto_excluded: false }],
          },
          {
            id: 3,
            bank: 'cathay',
            kind: 'pending',
            date: '2026-08-07',
            datetime: null,
            description: 'SHOP [USD 10]',
            amount: -310,
            cashflow_direction: 'expense',
            cashflow_amount: 310,
            currency: 'TWD',
            category: null,
            card_no: '9999',
            excluded: false,
            auto_excluded: false,
            tags: [],
            splits: [],
          },
          {
            id: 4,
            bank: 'cathay',
            kind: 'pending',
            date: '2026-08-06',
            datetime: null,
            description: '匯豐銀行自動扣款',
            amount: -12729,
            cashflow_direction: 'neutral',
            cashflow_amount: 0,
            currency: 'TWD',
            category: '還款',
            card_no: '3254',
            excluded: false,
            auto_excluded: true,
            tags: [],
            splits: [],
          },
        ],
        portfolio_facts: {},
      },
    },
  ],
};

const envelope = applyReplicaResponse(undefined, bootstrap, ownerKey);
ok(envelope);
equal(envelope.ownerId, 7);
deepEqual(envelope.generations, { user: 1, 'bank:cathay': 1 });

const pull: ReplicaResponse = {
  schema_version: 2,
  owner_id: 7,
  reset_required: false,
  generations: { 'bank:cathay': 2 },
  partitions: [{
    ...bootstrap.partitions[1],
    generation: 2,
    data: {
      ...(bootstrap.partitions[1].data as Record<string, unknown>),
      marker: 'changed',
    },
  }],
};
const merged = applyReplicaResponse(envelope, pull, ownerKey);
ok(merged);
equal(merged.generations.user, 1);
equal(merged.generations['bank:cathay'], 2);
deepEqual(merged.partitions.user, {
  preferences: { fx_display_mode: 'always_original', card_date_basis: 'post' },
});
equal((merged.partitions['bank:cathay'] as { marker: string }).marker, 'changed');

const wrongOwner = applyReplicaResponse(envelope, { ...pull, owner_id: 8 }, ownerKey);
equal(wrongOwner, undefined);
const reset = applyReplicaResponse(envelope, { ...pull, reset_required: true }, ownerKey);
equal(reset, undefined);

const dataset = projectReplicaDataset(envelope as ReplicaEnvelope);
deepEqual(dataset.preferences, {
  fx_display_mode: 'always_original',
  card_date_basis: 'post',
});
equal(dataset.transactions.length, 5);
const children = dataset.transactions.filter((row) => row.split_of === 1);
deepEqual(children.map((row) => row.id), ['1#0', '1#1']);
deepEqual(children.map((row) => row.amount), [-40, -60]);
deepEqual(children.map((row) => row.auto_excluded), [false, true]);
const malformedParent = dataset.transactions.find((row) => row.id === 2);
ok(malformedParent);
equal(malformedParent.amount, -80);
equal(malformedParent.display_description, '轉帳 · 御膳房');
equal(malformedParent.account_or_card, '*2345');
deepEqual(malformedParent.tags, ['御膳', '月結']);
const pending = dataset.transactions.find((row) => row.id === 3);
ok(pending);
equal(pending.consume_currency, 'USD');
equal(pending.consume_amount, 10);
equal(pending.fx_rate, 31);
equal(pending.fx_rate_source, 'bank_pending_estimate');
equal('raw' in pending, false);
const payment = dataset.transactions.find((row) => row.id === 4);
ok(payment);
equal(payment.cashflow_direction, 'neutral');
equal(payment.cashflow_amount, 0);
equal(payment.display_amount, 12729);

const parityEnvelope = applyReplicaResponse(undefined, {
  schema_version: 2,
  owner_id: 7,
  reset_required: false,
  generations: { 'bank:cathay': 1 },
  partitions: [{
    name: 'bank:cathay',
    generation: 1,
    data: {
      accounts: [],
      cards: [],
      transactions: [
        {
          id: 2, bank: 'cathay', kind: 'twd', date: '2026-08-09', datetime: null,
          amount: -2, currency: 'TWD', account_no: '123-456-789', splits: [],
        },
        {
          id: 10, bank: 'cathay', kind: 'twd', date: '2026-08-09', datetime: null,
          amount: -10, currency: 'TWD', splits: [],
        },
        {
          id: 4, bank: 'cathay', kind: 'billed', date: '2026-08-09', datetime: null,
          amount: 0, currency: 'TWD', consume_currency: 'USD', consume_amount: 10,
          counterparty_acct: 'ABCDEFGHIJKLMNOPQRSTUVWXYZ123456789',
          display_description: 'SERVER CANONICAL DISPLAY', splits: [],
        },
      ],
    },
  }],
}, ownerKey);
ok(parityEnvelope);
const parityTransactions = projectReplicaDataset(parityEnvelope).transactions;
deepEqual(parityTransactions.map((row) => row.id), [10, 2, 4]);
equal(parityTransactions[1].account_or_card, '*******-789');
equal(parityTransactions[2].fx_rate, null);
equal(parityTransactions[2].fx_rate_source, null);
equal(parityTransactions[2].display_description, 'SERVER CANONICAL DISPLAY');

const completePartitions: Record<string, unknown> = {
  ...envelope.partitions,
  user: {
    ...(envelope.partitions.user as Record<string, unknown>),
    bank_accounts: [{
      id: 1, bank: 'cathay', label: '主帳', created_at: envelope.syncedAt,
      updated_at: envelope.syncedAt, has_creds: true, fields_set: [],
    }],
  },
  manual: { accounts: [], transactions: [] },
  brokerage: { accounts: [], balances: [], positions: [], activities: [], last_synced_at: null },
  market: { fx: { rates: { TWD: 1 } }, quotes: [] },
};
for (const bank of SUPPORTED_BANKS) {
  completePartitions[`bank:${bank}`] ??= {
    accounts: [],
    cards: [],
    transactions: [],
    portfolio_facts: {
      latest_twd_balance: null,
      latest_account_transaction_balances: [],
      loan_balance: null,
      card_unpaid: null,
    },
  };
}
completePartitions['bank:cathay'] = {
  accounts: [{
    account_no: '1234', currency: 'TWD', product_type: 'deposit', raw_balance: 1234,
    raw_balance_date: '2026-08-10', excluded: false,
  }],
  cards: [],
  transactions: [],
  portfolio_facts: {
    latest_twd_balance: { snapshot_date: '2026-08-10', twd_balance: 1234 },
    latest_account_transaction_balances: [],
    loan_balance: null,
    card_unpaid: null,
  },
};
const staleDashboardEnvelope = {
  ...envelope,
  partitions: completePartitions,
  dashboardCache: {
    cachedAt: '2026-08-10T00:00:00Z',
    accounts: [{ has_creds: true }],
    portfolio: { current_month_spending: 9479 },
    stats: { total: 1 },
  },
} as unknown as ReplicaEnvelope;
const locallyProjectedDashboard = projectReplicaDataset(staleDashboardEnvelope).dashboardCache;
ok(locallyProjectedDashboard);
equal(locallyProjectedDashboard.portfolio.total_assets, 1234);
equal(locallyProjectedDashboard.accounts[0].bank, 'cathay');

const validAccountTabCache: ReplicaAccountTabCache = {
  cachedAt: '2026-08-10T03:00:00Z',
  balances: [{
    bank: 'cathay',
    account_no: '1234',
    currency: 'TWD',
    nickname: '主帳戶',
    product_type: 'deposit',
    type: '活期存款',
    balance: 1000,
    snapshot_date: '2026-08-10',
    is_stale: false,
    twd_estimate: 1000,
    fx_rate_used: 1,
    excluded: false,
  }],
  accounts: [{
    id: 1,
    bank: 'cathay',
    label: '主帳',
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-10T00:00:00Z',
    has_creds: true,
    fields_set: ['username'],
  }],
  cards: [{ card_no: '9999', bank: 'cathay', name: 'Card', type: 'credit' }],
  manualAccounts: [{
    id: 'manual-1',
    source: 'manual',
    source_ref: 'manual-1',
    institution_name: null,
    name: '現金',
    account_ref: null,
    product_type: 'deposit',
    currency: 'TWD',
    balance: '500',
    as_of: '2026-08-10',
    valuation_source: null,
    included_in_net_worth: true,
    editable: true,
    deletable: true,
  }],
};
const validAccountTabEnvelope = { ...envelope, accountTabCache: validAccountTabCache };
equal(projectReplicaDataset(validAccountTabEnvelope).accountTabCache, validAccountTabCache);
const preservedAccountTab = applyReplicaResponse(validAccountTabEnvelope, pull, ownerKey);
ok(preservedAccountTab);
equal(preservedAccountTab.accountTabCache, validAccountTabCache);

for (const accountTabCache of [
  {
    ...validAccountTabCache,
    balances: [{ ...validAccountTabCache.balances[0], is_stale: 'false' }],
  },
  {
    ...validAccountTabCache,
    accounts: [{ ...validAccountTabCache.accounts[0], fields_set: [7] }],
  },
  {
    ...validAccountTabCache,
    cards: [{ ...validAccountTabCache.cards[0], bill_due_amount: Number.NaN }],
  },
  {
    ...validAccountTabCache,
    manualAccounts: [{ ...validAccountTabCache.manualAccounts[0], balance: 500 }],
  },
]) {
  const corrupt = { ...envelope, accountTabCache } as unknown as ReplicaEnvelope;
  equal(projectReplicaDataset(corrupt).accountTabCache, undefined);
}

console.log('replica contract tests passed');
