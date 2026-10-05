const assert = require('node:assert/strict');
const { test, afterEach } = require('node:test');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const query = require('@tanstack/react-query');
function stub(id, exports) {
  const filename = require.resolve(id);
  require.cache[filename] = { id: filename, filename, loaded: true, exports };
}
const native = ({ children, testID, visible }) => visible === false ? null
  : React.createElement('div', { 'data-testid': testID }, children);
stub('react-native', { ...Object.fromEntries(['View', 'Text', 'Pressable', 'ScrollView', 'Modal', 'TextInput'].map(n => [n, native])),
  ActivityIndicator: native, Platform: { OS: 'web' }, useWindowDimensions: () => ({ width: 1200, height: 800 }) });
stub('expo-secure-store', {});
stub('expo-router', { useRouter: () => ({ push() {}, replace() {} }) });
let remote = 'pending';
const calls = [];
stub('./api', { api: async (path) => {
  calls.push(path);
  if (remote === 'reject') throw Error('offline');
  if (remote === 'success') return {};
  return new Promise(() => {});
}, formatApiError: e => e.message });
const options = new Map();
const mutations = [];
stub('@tanstack/react-query', { ...query, useQuery: opts => {
  options.set(opts.queryKey[0], opts);
  return query.useQuery(opts);
}, useMutation: opts => {
  const mutation = query.useMutation(opts);
  mutations.push(mutation);
  return mutation;
} });
const { useAuthStore } = require('../stores/auth');
const { queryClient: client } = require('./queryClient');
client.setDefaultOptions({ queries: { retry: false, retryOnMount: false, staleTime: Infinity, gcTime: Infinity } });
const replica = require('./replica');
const { replicaStore } = require('./replicaStore');
const { SUPPORTED_BANKS } = require('../types/api');
const Dashboard = require('../app/(tabs)/dashboard').default;
const { computeLocalPortfolio } = require('./localPortfolio');
stub('expo-linking', {});
stub('expo-web-browser', {});
const { SnapTradeAccountsSection, SnapTradeHoldingsSection, SnapTradeConnectionSettings } = require('../components/SnapTradeSections');
let serial = 0;
function owner() {
  const auth = { token: 'fixture', email: `fixture-${++serial}@example.test`, serverUrl: 'https://example.test' };
  // Zustand SSR reads the initial snapshot. Keep that snapshot and live state aligned.
  Object.assign(useAuthStore.getInitialState(), auth);
  useAuthStore.setState(auth);
  const key = replica.makeReplicaOwnerKey(auth.serverUrl, auth.email);
  replica.activateReplicaOwner(key);
  return key;
}
function envelope(ownerKey, due = '2026-09-18') {
  const partitions = { user: { bank_accounts: [{ id: 1, bank: 'ubot', label: 'fixture',
    created_at: '2026-09-15', updated_at: '2026-09-15', has_creds: true, fields_set: [] }],
    preferences: {}, auto_debit_settings: [] }, manual: { accounts: [], transactions: [] },
    brokerage: { accounts: [], balances: [], positions: [], activities: [], last_synced_at: null },
    market: { fx: { source: null, as_of: null, rates: { TWD: 1 } }, quotes: [], unavailable_symbols: [] } };
  for (const bank of SUPPORTED_BANKS) partitions[`bank:${bank}`] = { accounts: [], cards: [], transactions: [],
    portfolio_facts: { latest_twd_balance: null, latest_account_transaction_balances: [], loan_balance: null, card_unpaid: null } };
  partitions['bank:ubot'].cards = [{ bank: 'ubot', card_no: '1234', name: 'fixture', active: true,
    excluded: false, bill_due_amount: 1234, payment_due_date: due }];
  return { ownerKey, ownerId: 1, schemaVersion: 2, generations: {}, partitions, syncedAt: '2026-09-15T00:00:00Z' };
}
function render(inspect) {
  function Driver() {
    const tree = Dashboard();
    inspect?.(tree);
    return tree;
  }
  return renderToStaticMarkup(React.createElement(query.QueryClientProvider, { client }, React.createElement(Driver)));
}
async function hydrate(e) {
  await replicaStore.save(e);
  render(); // Capture the real hook's query options, not a substitute query function.
  await client.fetchQuery(options.get('frontend-dataset'));
}
afterEach(() => { client.clear(); options.clear(); calls.length = 0; mutations.length = 0; remote = 'pending'; });
for (const Component of [SnapTradeAccountsSection, SnapTradeConnectionSettings]) {
  test(`${Component.name} successful sync refreshes mounted Dashboard read models`, async () => {
    const ownerKey = owner();
    const epoch = replica.getReplicaOwnerEpoch(ownerKey);
    const keys = [['frontend-dataset', 'replica', ownerKey, epoch],
      ['portfolio', 'summary', ownerKey, epoch], ['snaptrade', 'portfolio', ownerKey, epoch]];
    client.setQueryData(keys[0], replica.projectReplicaDataset(envelope(ownerKey)));
    client.setQueryData(keys[1], { brokerage_assets_twd: 123 });
    client.setQueryData(keys[2], { accounts: [], balances: [], positions: [], activities: [] });
    renderToStaticMarkup(React.createElement(query.QueryClientProvider, { client }, React.createElement(Component)));
    remote = 'success';
    await mutations[0].mutateAsync();
    for (const key of keys) assert.equal(client.getQueryState(key).isInvalidated, true, key[0]);
    assert.deepEqual(calls, ['/snaptrade/sync']);
  });
}

for (const [name, balance, currency, reason, expected, incomplete] of [
  ['Yahoo valued total once', '1234', 'USD', null, 39488, false],
  ['fallback', '20', 'TWD', 'quote_unavailable', 20, true],
  ['missing FX', '20', 'EUR', null, 0, true],
  ['missing total', null, 'TWD', 'holdings_unavailable', 0, true],
  ['valid zero', '0', 'TWD', null, 0, false],
  ['negative unchanged', '-20', 'TWD', null, -20, false],
]) test(`brokerage Dashboard/local projection: ${name}`, async () => {
  const e = envelope(owner());
  e.partitions['bank:ubot'].cards = [];
  e.partitions.market.fx.rates.USD = 32;
  e.partitions.brokerage.accounts = [{ balance_total: balance, balance_currency: currency,
    valuation_source: reason ? 'broker_snapshot' : 'yahoo', valuation_reason: reason }];
  e.partitions.brokerage.balances = [{ cash: '999999', currency: 'USD' }];
  const summary = computeLocalPortfolio(e, []);
  assert.equal(summary.brokerage_assets_twd, expected);
  assert.equal(summary.net_worth_with_fx, expected);
  assert.equal(summary.brokerage_valuation_incomplete, incomplete);
  await hydrate(e);
  const html = render();
  assert.equal(html.includes('部分券商估值資料不足，請至帳戶查看'), incomplete);
  if (expected) assert.match(html, new RegExp(Math.abs(expected).toLocaleString('en-US')));
});
test('actual SnapTrade sections show server valuation, separate quote time, legacy/fallback and owner isolation', () => {
  owner();
  const renderSection = (Component, props) => renderToStaticMarkup(React.createElement(query.QueryClientProvider,
    { client }, React.createElement(Component, props)));
  renderSection(SnapTradeAccountsSection);
  const key = options.get('snaptrade').queryKey;
  const account = { id: 'a', name: 'A', institution_name: 'Broker', balance_total: '1234.56',
    balance_currency: 'USD', synced_at: '2026-09-10T01:02:00Z', valuation_source: 'mixed',
    valuation_as_of: '2026-09-11T03:04:00Z', valuation_reason: null };
  client.setQueryData(key, { accounts: [account, { ...account, id: 'b', valuation_source: undefined,
    valuation_as_of: null, balance_total: null, valuation_reason: 'quote_unavailable' }], balances: [],
    positions: [{ account_id: 'a', provider_symbol_id: 'stock', symbol: 'STOCK', asset_type: 'CS', quantity: '2',
      price: '500', market_value: '1000', average_cost: '123.45', currency: 'USD', valuation_source: 'yahoo', valuation_as_of: account.valuation_as_of },
    { account_id: 'a', provider_symbol_id: 'option', symbol: 'OPTION', asset_type: 'OPTION', average_cost: '3', quantity: '1', market_value: '234.56', currency: 'USD', valuation_source: 'broker_snapshot' }], activities: [] });
  const { formatLocalDateTime } = require('./datetime');
  const cards = renderSection(SnapTradeAccountsSection);
  assert.match(cards, /Yahoo／券商/);
  assert.match(cards, /1,234.56/);
  assert.match(cards, /資料不足 · 券商快照/);
  assert.ok(cards.includes(formatLocalDateTime(account.valuation_as_of)));
  const holdings = renderSection(SnapTradeHoldingsSection, { accountId: 'a' });
  assert.match(holdings, /帳戶總值/);
  assert.match(holdings, /券商快照/);
  assert.ok(holdings.includes(`報價 ${formatLocalDateTime(account.valuation_as_of)}`));
  assert.ok(holdings.includes(`持倉更新 ${formatLocalDateTime(account.synced_at)}`));
  assert.equal((holdings.match(/Yahoo/g) || []).length, 1);
  assert.equal((holdings.match(/報價/g) || []).length, 1);
  assert.equal((holdings.match(/>成本</g) || []).length, 2);
  assert.equal((holdings.match(/>目前市值</g) || []).length, 2);
  assert.match(holdings, /成本<\/div><div>[^<]*246\.90/);
  assert.match(holdings, /目前市值<\/div><div>[^<]*1,000/);
  assert.match(holdings, /成本<\/div><div>—/);
  assert.match(holdings, /目前市值<\/div><div>[^<]*234\.56/);
  for (const removed of ['單價', '帳戶總值包含現金', '非買入成本', account.synced_at, account.valuation_as_of]) {
    assert.ok(!holdings.includes(removed), removed);
  }
  const other = renderSection(SnapTradeHoldingsSection, { accountId: 'b' });
  assert.ok(!other.includes(formatLocalDateTime(account.valuation_as_of)));
  assert.match(other, /資料不足 · 券商快照/);
  assert.match(other, /—/);
  const snapshot = client.getQueryData(key);
  for (const [source, reason, expected] of [
    ['yahoo', null, 'Yahoo'], ['broker_snapshot', null, '券商快照'],
    [undefined, null, '券商快照'], ['yahoo', 'quote_unavailable', '資料不足 · 券商快照'],
  ]) {
    client.setQueryData(key, { ...snapshot, accounts: [{ ...account, valuation_source: source, valuation_reason: reason }],
      positions: [{ ...snapshot.positions[0], valuation_source: source, valuation_reason: reason,
        average_cost: null, market_value: null }] });
    const html = renderSection(SnapTradeHoldingsSection, { accountId: 'a' });
    assert.ok(html.includes(expected));
    assert.match(html, /成本<\/div><div>—/);
    assert.match(html, /目前市值<\/div><div>—/);
    assert.equal((html.match(/券商快照/g) || []).length, source === 'yahoo' && !reason ? 0 : 2);
    assert.equal((html.match(/報價/g) || []).length, 1);
  }
  owner();
  assert.ok(!renderSection(SnapTradeHoldingsSection, { accountId: 'a' }).includes(formatLocalDateTime(account.valuation_as_of)));
});
test('real Dashboard and dataset hook hydrate persisted ubot facts with remote pending/rejected; no reminder GET', async t => {
  t.mock.timers.enable({ apis: ['Date'], now: new Date('2026-09-15T00:00:00Z') });
  const e = envelope(owner());
  await hydrate(e);
  assert.match(render(), /payment-reminder-ubot-/);
  assert.match(render(), /3 天後到期/);
  assert.ok(!options.has('auto-debit'), 'remote reminder query must not exist');
  const opts = options.get('frontend-dataset');
  void client.fetchQuery({ ...opts, staleTime: 0 });
  await new Promise(resolve => setTimeout(resolve, 5));
  assert.match(render(), /payment-reminder-ubot-/);
  assert.ok(calls.includes('/replica/pull'), 'normal background refresh still uses replica');
  await client.cancelQueries();
  // Rejection on a separate owner avoids intentionally unresolved owner queue work.
  remote = 'reject';
  await hydrate(envelope(owner()));
  await client.fetchQuery({ ...options.get('frontend-dataset'), staleTime: 0 });
  await assert.rejects(client.fetchQuery(options.get('sync')), /offline/);
  assert.match(render(), /payment-reminder-ubot-/);
  assert.ok(!calls.includes('/cards/auto-debit/reminders'));
});
test('unknown shows explicit status, malformed reminders leave the valid summary visible, retry uses dataset', async t => {
  t.mock.timers.enable({ apis: ['Date'], now: new Date('2026-09-15T00:00:00Z') });
  owner();
  assert.match(render(), /載入繳費提醒/);
  const e = envelope(owner()); delete e.partitions.user.auto_debit_settings;
  await hydrate(e);
  const html = render();
  assert.match(html, /暫時無法載入繳費提醒/);
  assert.match(html, /payment-reminders-retry/);
  assert.ok(!html.includes('暫時無法載入財務摘要'));
  assert.ok(!html.includes('尚未設定自動扣繳帳號'));
  let retry;
  const walk = tree => React.Children.toArray(tree).forEach(n => {
    if (!React.isValidElement(n)) return;
    if (n.props.testID === 'payment-reminders-retry') retry = n.props.onPress;
    walk(n.props.children);
  });
  render(walk);
  remote = 'reject';
  retry();
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.ok(calls.includes('/replica/pull'), 'retry must use the existing dataset path');
});
test('Taipei midnight recomputes yesterday empty and old days without a fetch; changed facts and owners remove reminders', async t => {
  t.mock.timers.enable({ apis: ['Date'], now: new Date('2026-09-14T15:59:59Z') });
  const e = envelope(owner()); await hydrate(e);
  assert.ok(!render().includes('payment-reminder-ubot-'));
  t.mock.timers.setTime(new Date('2026-09-14T16:00:00Z').getTime());
  assert.match(render(), /3 天後到期/);
  t.mock.timers.setTime(new Date('2026-09-17T16:00:00Z').getTime());
  assert.match(render(), /今天到期/);
  e.partitions['bank:ubot'].cards[0].excluded = true;
  client.setQueryData(options.get('frontend-dataset').queryKey, replica.projectReplicaDataset(e));
  assert.ok(!render().includes('payment-reminder-ubot-'));
  owner();
  const html = render();
  assert.ok(!html.includes('payment-reminder-ubot-'));
  assert.match(html, /載入繳費提醒/);
});

test('real auto-debit save and clear mutations invalidate replica plus settings', async () => {
  const { AutoDebitSettingModal } = require('../components/AutoDebitSettingModal');
  const key = owner();
  const epoch = replica.getReplicaOwnerEpoch(key);
  const datasetKey = ['frontend-dataset', 'replica', key, epoch];
  const settingsKey = ['auto-debit', 'settings', key, epoch];
  remote = 'success';
  client.setQueryData(['auto-debit', 'eligible-accounts', key, epoch], [
    { bank: 'ubot', account_no: 'acct', raw_balance: 1000 },
  ]);
  const nodes = tree => React.Children.toArray(tree).flatMap(n => React.isValidElement(n)
    ? [n, ...nodes(n.props.children)] : []);
  for (const action of ['save', 'clear']) {
    client.setQueryData(datasetKey, replica.projectReplicaDataset(envelope(key)));
    client.setQueryData(settingsKey, [{ card_bank: 'ubot', account_bank: 'ubot', account_no: 'acct' }]);
    let press;
    function Driver() {
      const tree = AutoDebitSettingModal({ visible: true, cardBank: 'ubot', bankLabel: '聯邦', onClose() {} });
      press = nodes(tree).find(n => n.props.testID === `auto-debit-${action}-button`).props.onPress;
      return tree;
    }
    renderToStaticMarkup(React.createElement(query.QueryClientProvider, { client }, React.createElement(Driver)));
    press();
    await new Promise(resolve => setTimeout(resolve, 10));
    assert.equal(client.getMutationCache().getAll().at(-1).state.status, 'success');
    assert.equal(client.getQueryState(settingsKey).isInvalidated, true);
    assert.equal(client.getQueryState(datasetKey).isInvalidated, true, `${action} must refresh reminder facts`);
  }
});
