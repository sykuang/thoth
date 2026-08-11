import { SUPPORTED_BANKS, type Transaction } from '@/types/api';

import { projectReplicaDashboard } from './dashboardCache';
import type { ReplicaEnvelope } from './replica';

function equal(actual: unknown, expected: unknown): void {
  if (!Object.is(actual, expected)) throw new Error(`expected ${String(expected)}, got ${String(actual)}`);
}

const partitions: Record<string, unknown> = {
  user: {
    bank_accounts: [{
      id: 1,
      bank: 'cathay',
      label: '主帳',
      created_at: '2026-08-10T00:00:00Z',
      updated_at: '2026-08-10T00:00:00Z',
      has_creds: true,
      fields_set: [],
    }],
    preferences: { card_date_basis: 'consume' },
  },
  manual: { accounts: [], transactions: [] },
  brokerage: { accounts: [], balances: [], positions: [], activities: [], last_synced_at: null },
  market: { fx: { source: null, as_of: null, rates: { TWD: 1 } }, quotes: [], unavailable_symbols: [] },
};
for (const bank of SUPPORTED_BANKS) {
  partitions[`bank:${bank}`] = {
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
(partitions['bank:cathay'] as Record<string, unknown>).portfolio_facts = {
  latest_twd_balance: { snapshot_date: '2026-08-10', twd_balance: 1234 },
  latest_account_transaction_balances: [],
  loan_balance: null,
  card_unpaid: null,
};

const envelope: ReplicaEnvelope = {
  ownerKey: 'owner',
  ownerId: 1,
  schemaVersion: 2,
  generations: {},
  partitions,
  syncedAt: '2026-08-11T00:00:00Z',
};
const cache = projectReplicaDashboard(envelope, [], 'consume', new Date('2026-08-11T12:00:00Z'));
equal(cache?.cachedAt, envelope.syncedAt);
equal(cache?.accounts.length, 1);
equal(cache?.portfolio.total_assets, 1234);
equal(cache?.stats.total, 0);

const incomplete: ReplicaEnvelope = {
  ...envelope,
  partitions: { ...partitions },
};
delete incomplete.partitions.market;
equal(projectReplicaDashboard(incomplete, [], 'consume'), undefined);

const malformed: ReplicaEnvelope = {
  ...envelope,
  partitions: {
    ...partitions,
    'bank:cathay': {
      ...(partitions['bank:cathay'] as Record<string, unknown>),
      portfolio_facts: {
        latest_twd_balance: { snapshot_date: '2026-08-10', twd_balance: 'not-money' },
        latest_account_transaction_balances: [],
        loan_balance: null,
        card_unpaid: null,
      },
    },
  },
};
equal(projectReplicaDashboard(malformed, [], 'consume'), undefined);

const malformedFlow: ReplicaEnvelope = {
  ...envelope,
  partitions: {
    ...partitions,
    'bank:cathay': {
      ...(partitions['bank:cathay'] as Record<string, unknown>),
      transactions: [{
        id: 1,
        bank: 'cathay',
        kind: 'billed',
        date: '2026-08-10',
        consume_date: '2026-08-10',
        post_date: null,
        amount: -100,
        cashflow_direction: 'expense',
        cashflow_amount: 100,
        currency: 'TWD',
        consume_currency: null,
        consume_amount: null,
        category: null,
        subcategory: null,
        txn_type: 'spending',
        flow_type: 7,
        is_subscription: false,
        income_category: null,
        excluded: false,
        auto_excluded: false,
        splits: [],
      }],
    },
  },
};
equal(projectReplicaDashboard(malformedFlow, [], 'consume'), undefined);

const invalidQuoteTime: ReplicaEnvelope = {
  ...envelope,
  partitions: {
    ...partitions,
    market: {
      fx: { rates: { TWD: 1 } },
      quotes: [{
        symbol: 'AAA', currency: 'TWD', regular_market_price: '1',
        regular_market_time: 1e308,
      }],
    },
  },
};
equal(projectReplicaDashboard(invalidQuoteTime, [], 'consume'), undefined);

const negativeManualAsset: ReplicaEnvelope = {
  ...envelope,
  partitions: {
    ...partitions,
    manual: {
      accounts: [{
        id: 'manual:1', product_type: 'deposit', currency: 'TWD',
        balance: '-100', included_in_net_worth: true,
      }],
      transactions: [],
    },
  },
};
equal(projectReplicaDashboard(negativeManualAsset, [], 'consume'), undefined);

const hugeIncome = [1, 2].map((id) => ({
  id,
  bank: 'cathay',
  kind: 'billed' as const,
  date: '2026-08-10',
  consume_date: '2026-08-10',
  post_date: null,
  amount: Number.MAX_VALUE,
  cashflow_direction: 'income' as const,
  cashflow_amount: Number.MAX_VALUE,
  currency: 'TWD',
  consume_currency: null,
  consume_amount: null,
  category: null,
  subcategory: null,
  txn_type: 'cashback' as const,
  flow_type: 'income' as const,
  is_subscription: false,
  income_category: 'interest_dividend' as const,
  excluded: false,
  auto_excluded: false,
  splits: [],
}));
const unsafeTransactions: ReplicaEnvelope = {
  ...envelope,
  partitions: {
    ...partitions,
    'bank:cathay': {
      ...(partitions['bank:cathay'] as Record<string, unknown>),
      transactions: hugeIncome,
    },
  },
};
equal(projectReplicaDashboard(unsafeTransactions, hugeIncome as unknown as Transaction[], 'consume'), undefined);

const impossibleDate: ReplicaEnvelope = {
  ...envelope,
  partitions: {
    ...partitions,
    'bank:cathay': {
      ...(partitions['bank:cathay'] as Record<string, unknown>),
      portfolio_facts: {
        latest_twd_balance: { snapshot_date: '2026-02-31', twd_balance: 100 },
        latest_account_transaction_balances: [],
        loan_balance: null,
        card_unpaid: null,
      },
    },
  },
};
equal(projectReplicaDashboard(impossibleDate, [], 'consume'), undefined);

const negativeQuote: ReplicaEnvelope = {
  ...envelope,
  partitions: {
    ...partitions,
    market: {
      fx: { rates: { TWD: 1 } },
      quotes: [{
        symbol: 'AAA', currency: 'TWD', regular_market_price: '-1',
        regular_market_time: null,
      }],
    },
  },
};
equal(projectReplicaDashboard(negativeQuote, [], 'consume'), undefined);

const oversizedManualBalance: ReplicaEnvelope = {
  ...envelope,
  partitions: {
    ...partitions,
    manual: {
      accounts: [{
        id: 'manual:large', product_type: 'deposit', currency: 'TWD',
        balance: '1111111111111111', included_in_net_worth: true,
      }],
      transactions: [],
    },
  },
};
equal(projectReplicaDashboard(oversizedManualBalance, [], 'consume'), undefined);

function manualLedgerEnvelope(transactions: Record<string, unknown>[]): ReplicaEnvelope {
  return {
    ...envelope,
    partitions: {
      ...partitions,
      manual: {
        accounts: [{
          id: 'manual:holding', product_type: 'investment', currency: 'TWD',
          balance: '999', included_in_net_worth: true,
        }],
        transactions,
      },
    },
  };
}

const zeroBuy = manualLedgerEnvelope([{
  id: 1, account_id: 'manual:holding', kind: 'buy', occurred_on: '2026-08-01',
  symbol: 'AAA', quantity: '0', amount: '1', currency: 'TWD',
}]);
equal(projectReplicaDashboard(zeroBuy, [], 'consume'), undefined);

const oversell = manualLedgerEnvelope([
  {
    id: 1, account_id: 'manual:holding', kind: 'buy', occurred_on: '2026-08-01',
    symbol: 'AAA', quantity: '1', amount: '1', currency: 'TWD',
  },
  {
    id: 2, account_id: 'manual:holding', kind: 'sell', occurred_on: '2026-08-02',
    symbol: 'AAA', quantity: '2', amount: '1', currency: 'TWD',
  },
]);
equal(projectReplicaDashboard(oversell, [], 'consume'), undefined);

const noncanonicalSymbol = manualLedgerEnvelope([{
  id: 1, account_id: 'manual:holding', kind: 'buy', occurred_on: '2026-08-01',
  symbol: ' AAA ', quantity: '1', amount: '1', currency: 'TWD',
}]);
equal(projectReplicaDashboard(noncanonicalSymbol, [], 'consume'), undefined);

const duplicateManualAccounts: ReplicaEnvelope = {
  ...envelope,
  partitions: {
    ...partitions,
    manual: {
      accounts: [
        {
          id: 'manual:duplicate', product_type: 'investment', currency: 'TWD',
          balance: '10', included_in_net_worth: true,
        },
        {
          id: 'manual:duplicate', product_type: 'investment', currency: 'TWD',
          balance: '10', included_in_net_worth: true,
        },
      ],
      transactions: [],
    },
  },
};
equal(projectReplicaDashboard(duplicateManualAccounts, [], 'consume'), undefined);

console.log('replica dashboard projection tests passed');
