const assert = require('node:assert/strict');
const { test } = require('node:test');
const { SUPPORTED_BANKS } = require('../types/api.ts');
const { projectLoanRepayment } = require('./loanRepayments.ts');
const { computeLocalDashboardStats } = require('./localStats.ts');
const { aggregateByCategory, aggregateBySubcategory, computePeriodStats } = require('./txnFilter.ts');
const { projectReplicaDataset } = require('./replica.ts');

// Actual synthetic writer + authenticated PATCH outputs from the independent review.
// Subcategory variants below deliberately extend those returned facts locally.
const samples = [
  {
    "category": "__proto__",
    "fact": {
      "id": "loan:v1:89345f66ebaa81b56b5f4355dd94a9e3e69e95e44d4d2ce6c8c2e1af54eb5801",
      "bank": "sinopac",
      "source_account_id": 1,
      "account_no": "LOAN-1",
      "sub_account": "01",
      "currency": "TWD",
      "due_date": "2026-06-01",
      "paid_on": "2026-06-02",
      "status": "paid",
      "query_start": "2026-06-01",
      "query_end": "2026-06-30",
      "principal": "9007199254740993.25",
      "interest": "0.125",
      "penalty": "0.025",
      "paid_total": "9007199254740993.4",
      "principal_balance": "100.1",
      "component_overrides": {
        "interest": {
          "category": "__proto__",
          "subcategory": null,
          "auto_excluded": false
        }
      }
    },
    "backend_expense": "0.15"
  },
  {
    "category": "constructor",
    "fact": {
      "id": "loan:v1:89345f66ebaa81b56b5f4355dd94a9e3e69e95e44d4d2ce6c8c2e1af54eb5801",
      "bank": "sinopac",
      "source_account_id": 1,
      "account_no": "LOAN-1",
      "sub_account": "01",
      "currency": "TWD",
      "due_date": "2026-06-01",
      "paid_on": "2026-06-02",
      "status": "paid",
      "query_start": "2026-06-01",
      "query_end": "2026-06-30",
      "principal": "9007199254740993.25",
      "interest": "0.125",
      "penalty": "0.025",
      "paid_total": "9007199254740993.4",
      "principal_balance": "100.1",
      "component_overrides": {
        "interest": {
          "category": "constructor",
          "subcategory": null,
          "auto_excluded": false
        }
      }
    },
    "backend_expense": "0.15"
  },
  {
    "category": "toString",
    "fact": {
      "id": "loan:v1:89345f66ebaa81b56b5f4355dd94a9e3e69e95e44d4d2ce6c8c2e1af54eb5801",
      "bank": "sinopac",
      "source_account_id": 1,
      "account_no": "LOAN-1",
      "sub_account": "01",
      "currency": "TWD",
      "due_date": "2026-06-01",
      "paid_on": "2026-06-02",
      "status": "paid",
      "query_start": "2026-06-01",
      "query_end": "2026-06-30",
      "principal": "9007199254740993.25",
      "interest": "0.125",
      "penalty": "0.025",
      "paid_total": "9007199254740993.4",
      "principal_balance": "100.1",
      "component_overrides": {
        "interest": {
          "category": "toString",
          "subcategory": null,
          "auto_excluded": false
        }
      }
    },
    "backend_expense": "0.15"
  }
];
const envelope = {
  ownerKey: 'synthetic', ownerId: 1, schemaVersion: 2, generations: {},
  syncedAt: '2026-06-03T00:00:00Z',
  partitions: {
    user: { bank_accounts: [] }, manual: { accounts: [], transactions: [] },
    brokerage: { accounts: [], balances: [], positions: [], activities: [] },
    market: { fx: { rates: { TWD: 1 } }, quotes: [] },
    ...Object.fromEntries(SUPPORTED_BANKS.map(bank => [`bank:${bank}`, {
      accounts: [], cards: [], transactions: [], portfolio_facts: {
        latest_twd_balance: null, latest_account_transaction_balances: [],
        loan_balance: null, card_unpaid: null,
      },
    }])),
  },
};
for (const { category, fact: raw, backend_expense } of samples) {
  const fact = raw;
  test(`PATCH category ${category}: exact local and replica dashboard`, () => {
    const rows = projectLoanRepayment(fact);
    const stats = computeLocalDashboardStats(rows, 'consume');
    assert.equal(stats.total_expense, backend_expense);
    assert.equal(stats.amount_by_category[category], '0.125');
    assert.equal(Object.hasOwn(stats.amount_by_category, category), true);
    assert.deepEqual(stats.amount_by_month['2026-06'], { income: 0, expense: '0.15', net: '-0.15', count: 3 });
    assert.equal(rows[0].amount, '9007199254740993.25');
    const period = computePeriodStats(rows);
    assert.deepEqual(period, { income: 0, expense: '0.15', net: '-0.15', count: 2,
      loan_by_currency: { TWD: { income: '0', expense: '0.15', net: '-0.15', count: 2 } } });
    const replica = structuredClone(envelope);
    replica.partitions['bank:sinopac'].loan_repayments = [fact];
    assert.deepEqual(projectReplicaDataset(replica).dashboardCache?.stats, stats);
  });
  test(`category and subcategory counts ${category}: plain own-key output`, () => {
    for (const subcategory of ['__proto__', 'constructor', 'toString']) {
      const rows = projectLoanRepayment({ ...fact, component_overrides: {
        interest: { category, subcategory, auto_excluded: false },
      } });
      const interest = rows[1];
      const selected = [interest, interest, { ...interest, excluded: true }, { ...interest, auto_excluded: true },
        { ...interest, category: 'other' }];
      assert.deepEqual(aggregateByCategory(selected), { [category]: 2, other: 1 });
      assert.deepEqual(aggregateBySubcategory(selected, category), { [subcategory]: 2 });
      assert.deepEqual(aggregateBySubcategory(selected, ''), {});
      assert.deepEqual(aggregateBySubcategory(selected, '__null__'), {});
      const excluded = rows.map(row => ({ ...row, excluded: true }));
      assert.deepEqual(computeLocalDashboardStats(excluded, 'consume').amount_by_category, {});
      assert.equal(computePeriodStats(excluded).expense, 0);
    }
  });
  test(`legacy category ${category} and enum own-key membership`, () => {
    const row = { id: 1, bank: 'sinopac', kind: 'twd', date: '2026-06-02', datetime: null,
      description: 'synthetic', amount: -10, currency: 'TWD', category, subcategory: category,
      excluded: false, auto_excluded: false, flow_type: category };
    const stats = computeLocalDashboardStats([row], 'consume');
    assert.deepEqual(stats.amount_by_category, { [category]: 10 });
    assert.deepEqual(stats.amount_by_flow_type, { expense: 0, income: 0, transfer: 0, investment: 0 });
    const income = computeLocalDashboardStats([{ ...row, amount: 10, flow_type: 'income', income_category: category }], 'consume');
    assert.equal(income.income_unclassified_count, 1);
    assert.equal(income.passive_income_pct, 0);
  });
}
