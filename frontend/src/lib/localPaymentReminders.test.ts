import { projectReplicaDataset, type ReplicaEnvelope } from './replica';
import * as reminders from './localPaymentReminders';

const assert = {
  equal(actual: unknown, expected: unknown, message = '') {
    if (!Object.is(actual, expected)) throw new Error(`${message}: expected ${String(expected)}, got ${String(actual)}`);
  },
  deepEqual(actual: unknown, expected: unknown, message = '') {
    this.equal(JSON.stringify(actual), JSON.stringify(expected), message);
  },
};


const card = { bank: 'ubot', card_no: '1234', name: 'test card', active: true, excluded: false,
  bill_due_amount: 1000, payment_due_date: '2026-09-18' };
const envelope: ReplicaEnvelope = { ownerKey: 'test', ownerId: 1, schemaVersion: 2,
  generations: {}, syncedAt: '2026-09-15T00:00:00Z', partitions: {
    user: { bank_accounts: [{ bank: 'ubot' }], auto_debit_settings: [] },
    'bank:ubot': { cards: [card], accounts: [], transactions: [] },
  } };
assert.deepEqual(projectReplicaDataset(envelope).paymentReminderInputs, {
  cards: [card], settings: [], accounts: [],
}, 'existing replica must expose validated, ephemeral reminder inputs');

assert.equal(typeof reminders.computeLocalPaymentReminders, 'function', 'local projection is required');
const compute = (e = envelope, day = '2026-09-15') =>
  reminders.computeLocalPaymentReminders(projectReplicaDataset(e).paymentReminderInputs, day);
assert.deepEqual(compute(), [{ reason: 'no_account', card_bank: 'ubot', card_no: '', card_name: null,
  bill_due_amount: 1000, payment_due_date: '2026-09-18', days_until_due: 3,
  account_bank: null, account_no: null, account_balance: null, shortfall: null }]);
assert.deepEqual(compute(envelope, '2026-09-14'), []);
assert.equal(compute(envelope, '2026-09-18')?.[0].days_until_due, 0);
assert.deepEqual(compute(envelope, '2026-09-19'), []);
assert.equal(reminders.taipeiPaymentDay(new Date('2026-09-14T15:59:59Z')), '2026-09-14');
assert.equal(reminders.taipeiPaymentDay(new Date('2026-09-14T16:00:00Z')), '2026-09-15');
for (const active of [false, true]) {
  const e = structuredClone(envelope);
  (e.partitions['bank:ubot'] as any).cards = [{ ...card, active, excluded: active }];
  assert.deepEqual(compute(e), []);
}
const funded = structuredClone(envelope);
(funded.partitions.user as any).auto_debit_settings = [{ card_bank: 'ubot', account_bank: 'cathay', account_no: 'acct' }];
funded.partitions['bank:cathay'] = { cards: [{ ...card, bank: 'cathay' }], accounts: [
  { bank: 'cathay', account_no: 'acct', raw_balance: 300 },
], transactions: [] };
assert.equal(compute(funded)?.length, 1, 'metadata excludes other card banks, not cross-bank debit accounts');
assert.deepEqual(compute(funded)?.[0], { ...compute()![0], reason: 'insufficient',
  account_bank: 'cathay', account_no: 'acct', account_balance: 300, shortfall: 700 });
for (const balance of [1000, 1001]) {
  (funded.partitions['bank:cathay'] as any).accounts[0].raw_balance = balance;
  assert.deepEqual(compute(funded), []);
}
(funded.partitions['bank:cathay'] as any).accounts[0].raw_balance = null;
assert.equal(compute(funded)?.[0].shortfall, 1000);
(funded.partitions['bank:cathay'] as any).accounts = [];
assert.equal(compute(funded)?.[0].shortfall, 1000, 'known missing account retains backend zero-balance rule');
(funded.partitions['bank:ubot'] as any).cards[0].bill_due_amount = 2.675;
assert.equal(compute(funded)?.[0].shortfall, 2.67, 'Python binary float round(..., 2) parity');
for (const bank of ['ubot', 'hsbc']) {
  const e = structuredClone(envelope);
  (e.partitions.user as any).bank_accounts = [{ bank }];
  e.partitions[`bank:${bank}`] = { cards: [ { ...card, bank }, { ...card, bank, card_no: '5678' } ], transactions: [] };
  assert.equal(compute(e)?.length, bank === 'hsbc' ? 2 : 1);
  assert.equal(compute(e)?.[0].card_no, bank === 'hsbc' ? '1234' : '');
}
for (const mutate of [
  (e: any) => { delete e.partitions.user.auto_debit_settings; },
  (e: any) => { e.partitions.user.auto_debit_settings = [null]; },
  (e: any) => { delete e.partitions.user.bank_accounts; },
  (e: any) => { delete e.partitions['bank:ubot'].cards; },
  (e: any) => { delete e.partitions['bank:ubot'].cards[0].active; },
  (e: any) => { e.partitions['bank:ubot'].cards[0].bill_due_amount = '1000'; },
  (e: any) => { e.partitions['bank:ubot'].cards[0].payment_due_date = '2026-02-30'; },
  (e: any) => { e.partitions['bank:ubot'].cards[0].bank = 'hsbc'; },
]) {
  const e = structuredClone(envelope); mutate(e);
  assert.equal(compute(e), undefined, 'malformed required facts are unknown, not known none');
}
delete (funded.partitions['bank:cathay'] as any).accounts;
assert.equal(compute(funded), undefined, 'missing account partition is not zero balance');
assert.equal(reminders.computeLocalPaymentReminders(undefined, '2026-09-15'), undefined);
console.log('local payment reminder tests passed');
