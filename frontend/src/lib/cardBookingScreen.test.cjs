/* global __dirname */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

const filename = path.resolve(__dirname, '../app/(tabs)/cards/[bank]/[card_no].tsx');
const compiled = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS },
}).outputText;
const jsx = require('react/jsx-runtime').jsx;

function render(card) {
  const module = { exports: {} };
  const mocks = {
    'react/jsx-runtime': require('react/jsx-runtime'),
    'expo-router': { useLocalSearchParams: () => ({ bank: 'hsbc', card_no: '****1234' }) },
    '@tanstack/react-query': { useQuery: () => ({ data: card }) },
    'react-native': { Text: ({children}) => jsx('span', {children}), View: ({children}) => jsx('div', {children}), ActivityIndicator: () => null },
    '@/components/KeyboardAwareScrollView': { KeyboardAwareScrollView: ({children}) => jsx('div', {children}) },
    '@/components/BankBadge': { BankBadge: () => null },
    '@/lib/api': { api() {}, formatApiError: String },
    '@/lib/banks': { bankMeta: () => ({ short: 'Synthetic bank' }) },
    '@/lib/currency': { formatCurrency: String, formatSignedCurrency: String },
    '@/lib/mask': { maskCardNo: String },
  };
  vm.runInNewContext(compiled, {
    module, exports: module.exports,
    require(name) { assert.ok(name in mocks, `unexpected dependency ${name}`); return mocks[name]; },
  }, { filename });
  return require('react-dom/server').renderToStaticMarkup(
    require('react').createElement(module.exports.default),
  ).replace(/<[^>]*>/g, ' ');
}

const txn = (description, date = '2026-10-23') => ({ date, post_date: date, amount: 100, description });
const base = {
  bank: 'hsbc', card_no: '****1234', name: 'Synthetic card', payments: [],
  bill_due_amount: 100, bill_status: 'due', billed_txns: [txn('closed-statement')],
  pending_txns: [txn('still-unknown')],
};

test('card page renders booked purchases beyond the closed statement and undated booked rows', () => {
  const output = render({ ...base, posted_txns: [txn('closed-statement'), txn('post-close-booked'), txn('undated-booked', null)] });
  for (const value of ['已入帳明細', '未入帳／待確認', 'post-close-booked', 'undated-booked', 'still-unknown']) {
    assert.ok(output.includes(value), `missing ${value}`);
  }
  assert.ok(!output.includes('本期已出帳明細'));
});

test('old server data remains renderable without inventing newer booked history', () => {
  const output = render(base);
  assert.ok(output.includes('closed-statement'));
  assert.ok(!output.includes('post-close-booked'));
});

test('unknown bill amount is unknown, not a paid zero; genuine zero remains zero', () => {
  const unknown = render({ ...base, bill_due_amount: null, bill_status: 'unknown', unbilled_amount: null });
  assert.match(unknown, /本期應繳\s+—\s+狀態未知/);
  assert.ok(!/本期應繳\s+0/.test(unknown));
  const zero = render({ ...base, bill_due_amount: 0, bill_status: 'no_payment_required' });
  assert.match(zero, /本期應繳\s+0\s+無需繳款/);
});

test('actual React card page renders actual HTTP null as unknown, preserving booked collection',
  {skip: !process.env.CARD_BOOKING_HTTP_FIXTURE}, () => {
    const snapshots = JSON.parse(fs.readFileSync(process.env.CARD_BOOKING_HTTP_FIXTURE, 'utf8'));
    assert.equal(snapshots.length, 4);
    for (const {card, zeroCard} of snapshots) {
      const output = render(card);
      assert.match(output, /本期應繳\s+—\s+狀態未知/);
      assert.ok(output.includes('membership probe 0'));
      assert.equal(zeroCard.bill_due_amount, 0);
      assert.match(render(zeroCard), /本期應繳\s+0\s+✓ 已繳/);
      const legacy = {...card};
      delete legacy.posted_txns;
      assert.ok(render(legacy).includes('已入帳明細'));
    }
  });
