// Input comes from tests/test_loan_timeline_contract.py's real writer/API/replica flow.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { projectReplicaDataset } = require('../src/lib/replica.ts');
const { computeLocalDashboardStats } = require('../src/lib/localStats.ts');
const { computePeriodStats } = require('../src/lib/txnFilter.ts');
const { renderAmount } = require('../src/lib/currency.ts');
const source = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const envelope = {ownerKey:'synthetic-contract', ownerId:1, schemaVersion:2,
  generations:{'bank:sinopac':1}, syncedAt:'2026-06-02', partitions:{'bank:sinopac':source.facts}};
const projected = projectReplicaDataset(JSON.parse(JSON.stringify(envelope)));
const rows = projected.transactions.filter(t => t.kind === 'loan_repayment');
assert.equal(rows.length, source.items.length);
const fields = ['id','kind','component','bank','source_account_id','account_no','currency','date','datetime',
  'amount','display_amount','display_sign','cashflow_direction','cashflow_amount','flow_type',
  'category','subcategory','read_only','reconciliation_status','excluded','auto_excluded'];
for (const row of rows) {
  const api = source.items.find(t => t.id === row.id);
  assert.ok(api);
  for (const field of fields) assert.deepEqual(row[field], api[field], `${row.component}.${field}`);
  assert.deepEqual(row.loan_repayment, api.loan_repayment);
  assert.equal(renderAmount(row).direction, row.component === 'principal' ? 'zero' : 'expense');
  assert.ok(renderAmount(row).primary.startsWith(row.component === 'principal' ? '+' : '-'));
}
const stats = computeLocalDashboardStats(rows, 'consume');
for (const key of ['total_income','total_expense','total_net']) assert.equal(String(stats[key]), source.stats[key], key);
for (const month of Object.keys(source.stats.amount_by_month)) {
  assert.equal(stats.amount_by_month[month].count, source.stats.amount_by_month[month].count);
  for (const key of ['income','expense','net']) assert.equal(String(stats.amount_by_month[month][key]), source.stats.amount_by_month[month][key]);
}
for (const key of Object.keys(source.stats.amount_by_category)) assert.equal(String(stats.amount_by_category[key]), source.stats.amount_by_category[key]);
assert.equal(String(computePeriodStats(rows).expense), source.stats.total_expense);
console.log('BACKEND_WRITER_API_REPLICA_FRONTEND_CONTRACT_PASS');
