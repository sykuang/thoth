import type { Transaction } from '@/types/api';
import { computeLocalPortfolio } from './localPortfolio';
import type { ReplicaEnvelope } from './replica';

function equal(actual: unknown, expected: unknown): void {
  if (!Object.is(actual, expected)) throw new Error(`expected ${String(expected)}, got ${String(actual)}`);
}

function deepEqual(actual: unknown, expected: unknown): void {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

const envelope: ReplicaEnvelope = {
  ownerKey: 'owner',
  ownerId: 1,
  schemaVersion: 2,
  generations: {
    'bank:cathay': 1,
    manual: 1,
    brokerage: 1,
    market: 1,
  },
  syncedAt: '2026-08-11T00:00:00Z',
  partitions: {
    'bank:cathay': {
      accounts: [
        {
          account_no: 'cash-in', currency: 'TWD', product_type: 'deposit',
          raw_balance: 20_000, raw_balance_date: '2026-08-10', excluded: false,
        },
        {
          account_no: 'cash-out', currency: 'TWD', product_type: 'deposit',
          raw_balance: 5_000, raw_balance_date: '2026-08-10', excluded: true,
        },
        {
          account_no: 'jpy', currency: 'JPY', product_type: 'fx_deposit',
          raw_balance: 1_000, raw_balance_date: '2026-08-10', excluded: false,
        },
        {
          account_no: 'loan-out', currency: 'TWD', product_type: ' LOAN ',
          raw_balance: -1_000, raw_balance_date: '2026-08-09', excluded: true,
        },
      ],
      transactions: [
        {
          id: 1, bank: 'cathay', kind: 'pending', consume_date: null,
          amount: -100, cashflow_direction: 'expense', cashflow_amount: 100,
          currency: 'TWD', excluded: false, auto_excluded: false, splits: [],
        },
        {
          id: 2, bank: 'cathay', kind: 'billed', consume_date: '2026-08-05',
          amount: -200, cashflow_direction: 'expense', cashflow_amount: 200,
          currency: 'TWD', excluded: false, auto_excluded: false, splits: [],
        },
        {
          id: 3, bank: 'cathay', kind: 'billed', consume_date: '2026-07-31',
          amount: -300, cashflow_direction: 'expense', cashflow_amount: 300,
          currency: 'TWD', excluded: false, auto_excluded: false, splits: [],
        },
        {
          id: 4, bank: 'cathay', kind: 'billed', consume_date: '2026-08-06',
          amount: 50, cashflow_direction: 'income', cashflow_amount: 50,
          currency: 'TWD', excluded: false, auto_excluded: false, splits: [],
        },
        {
          id: 5, bank: 'cathay', kind: 'billed', consume_date: '2026-08-07',
          amount: -400, cashflow_direction: 'expense', cashflow_amount: 400,
          currency: 'TWD', excluded: false, auto_excluded: false, splits: [],
          txn_type: 'spending', flow_type: 'transfer',
        },
      ],
      portfolio_facts: {
        latest_twd_balance: { snapshot_date: '2026-08-10', twd_balance: 25_000 },
        latest_account_transaction_balances: [],
        loan_balance: { snapshot_date: '2026-08-09', amount_twd: 5_000, source: 'accounts' },
        card_unpaid: { snapshot_date: '2026-08-08', amount_twd: 300 },
      },
    },
    'bank:ubot': {
      accounts: [],
      transactions: [],
      portfolio_facts: {
        latest_twd_balance: null,
        latest_account_transaction_balances: [],
        loan_balance: null,
        card_unpaid: null,
      },
    },
    'bank:unknown': {
      accounts: [],
      transactions: [],
      portfolio_facts: {
        latest_twd_balance: { snapshot_date: '2026-08-11', twd_balance: 999_999_999 },
        latest_account_transaction_balances: [],
        loan_balance: null,
        card_unpaid: null,
      },
    },
    manual: {
      accounts: [
        {
          id: 'manual:1', product_type: 'deposit', currency: 'TWD', balance: '1000',
          included_in_net_worth: true,
        },
        {
          id: 'manual:2', product_type: 'loan', currency: 'USD', balance: '-200',
          included_in_net_worth: true,
        },
        {
          id: 'manual:3', product_type: 'investment', currency: 'TWD', balance: '50',
          included_in_net_worth: true,
        },
      ],
      transactions: [
        {
          id: 1, account_id: 'manual:3', kind: 'sell', occurred_on: '2026-08-02',
          symbol: 'AAA', quantity: '1', amount: '10', currency: 'USD',
        },
        {
          id: 2, account_id: 'manual:3', kind: 'buy', occurred_on: '2026-08-01',
          symbol: 'AAA', quantity: '2', amount: '20', currency: 'USD',
        },
      ],
    },
    brokerage: {
      accounts: [
        {
          id: 'broker-1', balance_total: '100', balance_currency: 'USD',
          synced_at: '2026-08-11T01:00:00Z',
        },
      ],
      balances: [], positions: [], activities: [], last_synced_at: '2026-08-11T01:00:00Z',
    },
    market: {
      fx: { source: 'test', as_of: '2026-08-11', rates: { TWD: 1, USD: 30, JPY: 0.2 } },
      quotes: [
        {
          symbol: 'AAA', currency: 'USD', regular_market_price: '10',
          regular_market_time: 1_786_406_400,
        },
      ],
      unavailable_symbols: [],
    },
  },
};

const transactions = ((envelope.partitions['bank:cathay'] as {
  transactions: Transaction[];
}).transactions);
const summary = computeLocalPortfolio(
  envelope,
  transactions,
  new Date('2026-08-11T12:00:00Z'),
);
equal(summary.total_assets, 20_000);
equal(summary.fx_assets_twd, 200);
equal(summary.brokerage_assets_twd, 3_000);
equal(summary.manual_assets_twd, 1_300);
equal(summary.total_assets_with_fx, 24_500);
equal(summary.total_card_unpaid, 300);
equal(summary.total_loan, 10_000);
equal(summary.manual_liabilities_twd, 6_000);
equal(summary.total_liabilities, 10_300);
equal(summary.current_month_spending, 300);
equal(summary.net_worth, 9_700);
equal(summary.net_worth_with_fx, 14_200);
equal(summary.as_of, '2026-08-11');
deepEqual(summary.skipped, ['ubot']);
deepEqual(summary.by_bank, [{
  bank: 'cathay',
  assets: 25_000,
  fx_assets_twd: 200,
  liabilities: 4_300,
  card_unpaid: 300,
  loan_balance: 5_000,
  current_month_spending: 300,
  stale: false,
  as_of: '2026-08-10',
}]);

const roundingSummary = computeLocalPortfolio({
  ...envelope,
  partitions: {
    'bank:cathay': {
      accounts: [{
        account_no: 'half-even', currency: 'TWD', product_type: 'deposit',
        raw_balance: 2.5, excluded: true,
      }],
      transactions: [],
      portfolio_facts: {
        latest_twd_balance: { snapshot_date: '2026-08-10', twd_balance: 10 },
        latest_account_transaction_balances: [],
        loan_balance: null,
        card_unpaid: null,
      },
    },
  },
}, [], new Date('2026-08-11T12:00:00Z'));
equal(roundingSummary.total_assets, 8);

const currencyScopedAccountEnvelope: ReplicaEnvelope = {
  ...envelope,
  partitions: {
    'bank:cathay': {
      accounts: [
        { account_no: 'shared', currency: 'TWD', product_type: 'deposit', excluded: false },
        { account_no: 'shared', currency: 'JPY', product_type: 'fx_deposit', excluded: false },
        { account_no: 'loan-included', currency: 'USD', product_type: 'loan', excluded: false },
        { account_no: 'loan-excluded', currency: 'USD', product_type: 'loan', excluded: true },
      ],
      transactions: [],
      portfolio_facts: {
        latest_twd_balance: { snapshot_date: '2026-08-10', twd_balance: 1000 },
        latest_account_transaction_balances: [
          { account_no: 'shared', currency: 'TWD', balance: 1000 },
          { account_no: 'shared', currency: 'JPY', balance: 10 },
          { account_no: 'loan-included', currency: 'USD', balance: -10 },
          { account_no: 'loan-excluded', currency: 'USD', balance: -5 },
        ],
        loan_balance: { snapshot_date: '2026-08-10', amount_twd: 1000, source: 'balance_history' },
        card_unpaid: null,
      },
    },
    market: { fx: { rates: { TWD: 1, USD: 30, JPY: 0.2 } }, quotes: [] },
  },
};
const currencyScopedAccountSummary = computeLocalPortfolio(
  currencyScopedAccountEnvelope,
  [],
  new Date('2026-08-11T12:00:00Z'),
);
equal(currencyScopedAccountSummary.fx_assets_twd, 2);
equal(currencyScopedAccountSummary.total_loan, 1300);

const accountSourcedLoanEnvelope = structuredClone(currencyScopedAccountEnvelope);
const accountSourcedFacts = (accountSourcedLoanEnvelope.partitions['bank:cathay'] as Record<string, unknown>)
  .portfolio_facts as Record<string, unknown>;
accountSourcedFacts.loan_balance = {
  snapshot_date: '2026-08-10', amount_twd: 1450, source: 'accounts',
};
const accountSourcedLoanSummary = computeLocalPortfolio(
  accountSourcedLoanEnvelope,
  [],
  new Date('2026-08-10T12:00:00Z'),
);
equal(accountSourcedLoanSummary.total_loan, 1300);

const missingLoanFxEnvelope = structuredClone(currencyScopedAccountEnvelope);
((missingLoanFxEnvelope.partitions.market as Record<string, unknown>).fx as Record<string, unknown>).rates = {};
const missingLoanFxSummary = computeLocalPortfolio(
  missingLoanFxEnvelope,
  [],
  new Date('2026-08-10T12:00:00Z'),
);
equal(missingLoanFxSummary.total_loan, 0);
equal(missingLoanFxSummary.skipped.includes('cathay'), true);

const zeroLoanSummary = computeLocalPortfolio({
  ...envelope,
  partitions: {
    'bank:cathay': {
      accounts: [{
        account_no: 'paid-loan', currency: 'TWD', product_type: 'loan',
        raw_balance: null, excluded: false,
      }],
      transactions: [],
      portfolio_facts: {
        latest_twd_balance: { snapshot_date: '2026-08-10', twd_balance: 1000 },
        latest_account_transaction_balances: [],
        loan_balance: {
          snapshot_date: '2026-08-10', amount_twd: 0, source: 'normalized_balance_metric',
        },
        card_unpaid: null,
      },
    },
  },
}, [], new Date('2026-08-10T12:00:00Z'));
equal(zeroLoanSummary.total_assets, 1000);
equal(zeroLoanSummary.total_loan, 0);
equal(zeroLoanSummary.skipped.includes('cathay'), false);

const negativeBrokerageSummary = computeLocalPortfolio({
  ...envelope,
  partitions: {
    brokerage: {
      accounts: [{ balance_total: '-100', balance_currency: 'USD' }],
      balances: [], positions: [], activities: [], last_synced_at: null,
    },
    market: { fx: { rates: { TWD: 1, USD: 30 } }, quotes: [] },
  },
}, [], new Date('2026-08-11T12:00:00Z'));
equal(negativeBrokerageSummary.brokerage_assets_twd, -3_000);

const exactManualInvestmentSummary = computeLocalPortfolio({
  ...envelope,
  partitions: {
    manual: {
      accounts: [{
        id: 'manual:exact', product_type: 'investment', currency: 'TWD', balance: null,
        included_in_net_worth: true,
      }],
      transactions: [{
        id: 1, account_id: 'manual:exact', kind: 'buy', occurred_on: '2026-08-01',
        symbol: 'EXACT', quantity: '0.0001', currency: 'TWD',
      }],
    },
    brokerage: { accounts: [], balances: [], positions: [], activities: [] },
    market: {
      fx: { rates: { TWD: 1 } },
      quotes: [{
        symbol: 'EXACT', currency: 'TWD', regular_market_price: '25000.000000004',
        regular_market_time: null,
      }],
    },
  },
}, [], new Date('2026-08-11T12:00:00Z'));
equal(exactManualInvestmentSummary.manual_assets_twd, 3);

const exactCancellationSummary = computeLocalPortfolio({
  ...envelope,
  partitions: {
    'bank:cathay': {
      accounts: [
        Number.MAX_SAFE_INTEGER,
        Number.MAX_SAFE_INTEGER,
        -(Number.MAX_SAFE_INTEGER - 2),
        -Number.MAX_SAFE_INTEGER,
      ].map((balance, index) => ({
        account_no: `fx-cancellation-${index}`,
        product_type: 'fx_deposit',
        currency: 'USD',
        raw_balance: balance,
        raw_balance_date: '2026-08-11',
        excluded: false,
      })),
      transactions: [],
      portfolio_facts: {
        latest_twd_balance: { snapshot_date: '2026-08-11', twd_balance: 0 },
        latest_account_transaction_balances: [],
        loan_balance: null,
        card_unpaid: null,
      },
    },
    manual: { accounts: [], transactions: [] },
    brokerage: { accounts: [], balances: [], positions: [], activities: [] },
    market: { fx: { rates: { TWD: 1, USD: 1 } }, quotes: [] },
  },
}, [], new Date('2026-08-11T12:00:00Z'));
equal(exactCancellationSummary.fx_assets_twd, 2);

console.log('local portfolio parity tests passed');
