const assert = {
  equal(actual:unknown, expected:unknown, message=''):void { if (actual !== expected) throw new Error(`${message}: ${actual} !== ${expected}`); },
  deepEqual(actual:unknown, expected:unknown):void { this.equal(JSON.stringify(actual), JSON.stringify(expected)); },
  throws(fn:()=>unknown):void { let threw=false; try {fn();} catch {threw=true;} this.equal(threw,true); },
};
import { projectReplicaDataset, type ReplicaEnvelope } from './replica';
import { renderAmount } from './currency';
import { computePeriodStats } from './txnFilter';
const fact = { id: 'loan:v1:opaque', bank: 'cathay', source_account_id: 1, account_no: '123456789', sub_account: '', currency: 'USD', due_date: '2026-09-01', paid_on: '2026-09-02', status: 'paid', query_start: '2026-09-01', query_end: '2026-09-30', principal: '100.001', interest: '0.1', penalty: '0.2', paid_total: '100.301', principal_balance: '999.999', excluded: false };
const envelope = (facts?: unknown): ReplicaEnvelope => ({ ownerKey:'test', ownerId:1, schemaVersion:2, generations:{'bank:cathay':1}, syncedAt:'2026-09-02', partitions:{'bank:cathay':{transactions:[], ...(facts === undefined ? {} : {loan_repayments:facts})}} });
const rows = projectReplicaDataset(envelope([fact])).transactions;
assert.equal(rows.length, 3, 'raw loan facts must reach the real projection');
assert.deepEqual(rows.map(t => t.id), ['loan:v1:opaque:principal','loan:v1:opaque:interest','loan:v1:opaque:penalty']);
assert.deepEqual(renderAmount(rows[0]), {primary:'+USD 100.001', sub:null, direction:'zero'});
assert.deepEqual(renderAmount(rows[1]), {primary:'-USD 0.1', sub:null, direction:'expense'});
assert.equal(rows[0].amount, '100.001');
assert.equal(rows[0].cashflow_amount, '0');
assert.equal(rows[1].amount, '-0.1');
assert.equal(rows[0].read_only, false);
assert.equal(rows[0].reconciliation_status, 'unverified');
assert.equal(computePeriodStats(rows).income, 0);
assert.equal(computePeriodStats(rows).expense, 0, 'USD is never added to legacy TWD aggregates');
assert.deepEqual(computePeriodStats(rows).loan_by_currency, { USD: {income:'0', expense:'0.3', net:'-0.3', count:2} });
assert.equal(projectReplicaDataset(envelope()).loanRepaymentsAvailable, false);
assert.equal(projectReplicaDataset(envelope([])).loanRepaymentsAvailable, true);
assert.throws(() => projectReplicaDataset(envelope([{...fact, interest:0.1}])));
assert.equal(projectReplicaDataset(envelope([{...fact, interest:'0', penalty:'0'}])).transactions.length, 1);
for (const key of ['principal', 'interest', 'penalty', 'paid_total', 'principal_balance']) {
  assert.throws(() => projectReplicaDataset(envelope([{...fact, [key]: '-0.1'}])));
}
const hydrated = projectReplicaDataset(JSON.parse(JSON.stringify(envelope([fact]))));
assert.deepEqual(hydrated.transactions, rows);
const excluded = envelope([fact]);
(excluded.partitions['bank:cathay'] as Record<string,unknown>).accounts = [{account_no:fact.account_no,currency:fact.currency,excluded:true}];
assert.equal(projectReplicaDataset(excluded).transactions.every(t=>t.excluded), true);
assert.deepEqual(computePeriodStats(projectReplicaDataset(excluded).transactions).loan_by_currency, {});
const twdRows = projectReplicaDataset(envelope([{...fact, currency:'TWD', interest:'9007199254740993.1'}])).transactions;
const period = computePeriodStats(twdRows);
assert.equal(period.income, 0);
assert.equal(period.expense, '9007199254740993.3');
assert.equal(period.net, '-9007199254740993.3');
assert.equal(period.count, 2);
import { aggregateByCategory } from './txnFilter';
import { computeLocalDashboardStats } from './localStats';
assert.deepEqual(aggregateByCategory(twdRows), {'還款':1,'金融':2});
const local = computeLocalDashboardStats(twdRows, 'consume');
assert.equal(local.total_income, 0);
assert.equal(local.total_expense, '9007199254740993.3');
assert.equal(local.amount_by_month['2026-09'].expense, '9007199254740993.3');
assert.equal(local.amount_by_category['金融'], '9007199254740993.3');
assert.equal(local.amount_by_flow_type?.expense, '9007199254740993.3');
// Hydrated old-server facts remain available without invented loans; malformed metadata fails closed.
assert.equal(projectReplicaDataset(JSON.parse(JSON.stringify(envelope()))).loanRepaymentsAvailable, false);
for (const bad of [{...fact, currency:'__proto__'}, {...fact, source_account_id:null}, {...fact, source_account_id:0}, {...fact, source_account_id:-1}, {...fact, source_account_id:'1'}, {...fact, bank:'wrong'}, {...fact, raw_json:'private'}, {...fact, paid_on:7}, {...fact, interest:'1e3'}]) assert.throws(() => projectReplicaDataset(envelope([bad])));
const legacy = {id:1,bank:'cathay',kind:'twd' as const,date:'2026-09-02',datetime:null,description:'deposit debit',amount:-10,currency:'TWD',category:'金融',cashflow_direction:'expense' as const,cashflow_amount:10,flow_type:'expense' as const,account_or_card:null};
for (const mixed of [[legacy,...twdRows,...rows], [...rows,...twdRows,legacy]]) {
  assert.equal(computePeriodStats(mixed).expense, '9007199254741003.3');
  const stats = computeLocalDashboardStats(mixed, 'consume');
  assert.equal(stats.total_expense, '9007199254741003.3');
  assert.equal(stats.total_net, '-9007199254741003.3');
  assert.equal(stats.amount_by_category['金融'], '9007199254741003.3');
  assert.equal(stats.amount_by_month['2026-09'].net, '-9007199254741003.3');
}
assert.equal(computePeriodStats(twdRows.map(t => ({...t, excluded:true}))).expense, 0);
import { addDecimal } from './decimal';
const tinyLoanRows = projectReplicaDataset(envelope([{...fact, currency:'TWD', principal:'0', interest:'0.1', penalty:'0', paid_total:'0.1'}])).transactions;
const large = {...legacy, id:101, amount:-Number.MAX_SAFE_INTEGER, cashflow_amount:Number.MAX_SAFE_INTEGER};
const two = {...legacy, id:102, amount:-2, cashflow_amount:2};
const exactLegacy = addDecimal(String(Number.MAX_SAFE_INTEGER), '2')!;
const exactExpense = addDecimal(exactLegacy, '0.1')!;
for (const list of [[large, two, ...tinyLoanRows], [...tinyLoanRows, two, large]]) {
  assert.equal(String(computePeriodStats(list).expense), exactExpense);
  const stats = computeLocalDashboardStats(list, 'consume');
  assert.equal(String(stats.total_expense), exactExpense);
  assert.equal(String(stats.amount_by_month['2026-09'].expense), exactExpense);
  assert.equal(String(stats.amount_by_category['金融']), exactExpense);
}
for (const list of [[large, two, ...tinyLoanRows], [...tinyLoanRows, two, large]]) {
  const incomes = list.map(t => t.kind === 'loan_repayment' ? t : ({...t, amount:t.cashflow_amount!, cashflow_direction:'income' as const, flow_type:'income' as const, income_category:'salary' as const}));
  assert.equal(String(computePeriodStats(incomes).income), exactLegacy);
  const stats = computeLocalDashboardStats(incomes, 'consume');
  assert.equal(String(stats.total_income), exactLegacy);
  assert.equal(String(stats.amount_by_month['2026-09'].income), exactLegacy);
  assert.equal(String(stats.amount_by_income_category?.salary), exactLegacy);
}
const currencyChanged = envelope([{...fact, currency:'TWD'}]);
(currencyChanged.partitions['bank:cathay'] as Record<string,unknown>).accounts = [{account_no:fact.account_no,currency:'USD',excluded:true}];
assert.equal(projectReplicaDataset(currencyChanged).transactions.every(t=>t.excluded), true);
assert.equal(computeLocalDashboardStats(twdRows, 'consume').amount_by_month['2026-09'].count, twdRows.length);
const edited = (ignored: boolean) => projectReplicaDataset(envelope([{...fact, currency:'TWD', component_overrides:{principal:{category:'收入', subcategory:null},interest:{category:'住房',subcategory:'房貸',auto_excluded:ignored}}}])).transactions;
assert.equal(edited(true)[0].category, '收入');
assert.equal(edited(true)[0].subcategory, null);
assert.equal(edited(true)[0].cashflow_direction, 'neutral');
assert.equal(edited(true)[1].auto_excluded, true);
for (const ignored of [true,false]) {
  const stats = computeLocalDashboardStats(edited(ignored), 'consume');
  assert.equal(stats.total_income, 0);
  assert.equal(stats.total_expense, ignored ? '0.2' : '0.3');
  assert.equal(stats.amount_by_month['2026-09'].expense, ignored ? '0.2' : '0.3');
  assert.equal(stats.amount_by_category['住房'] ?? 0, ignored ? 0 : '0.1');
}
for (const component_overrides of [null, [], {other:{}}, {interest:null}, {interest:[]}, {interest:{auto_excluded:1}}, {interest:{category:7}}, {interest:{category:'x'.repeat(101)}}, {interest:{tags:[]}}, JSON.parse('{"__proto__":{}}'), {interest:JSON.parse('{"__proto__":{}}')}]) assert.throws(() => projectReplicaDataset(envelope([{...fact,component_overrides}])));
assert.equal(projectReplicaDataset(envelope([{...fact,excluded:true,component_overrides:{interest:{auto_excluded:false}}}])).transactions[1].excluded,true);
for (const key of ['category', 'subcategory']) {
  const category = '😀'.repeat(100);
  const projected = projectReplicaDataset(envelope([{...fact, component_overrides:{interest:{[key]:category}}}])).transactions;
  assert.equal(projected[1][key as 'category' | 'subcategory'], category);
  assert.throws(() => projectReplicaDataset(envelope([{...fact,component_overrides:{interest:{[key]:'😀'.repeat(101)}}}])));
}
console.log('loanRepayments: projection, overrides, decimal, hydration and exclusion checks passed');
