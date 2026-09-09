const assert = require('node:assert/strict');
const { afterEach, beforeEach, test } = require('node:test');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { QueryClient, QueryClientProvider, useQuery, onlineManager } = require('@tanstack/react-query');

// Same module-boundary stubs as api.test.cjs. Render the actual screen, rows and
// filters with real React/Query state; only native UI and unrelated sheets stop here.
function stubModule(id, exports) {
  const filename = require.resolve(id);
  require.cache[filename] = { id: filename, filename, loaded: true, exports };
}
const native = ({ children, testID, visible }) => visible === false ? null
  : React.createElement('div', { 'data-testid': testID }, children);
stubModule('react-native', {
  ...Object.fromEntries(['View', 'Text', 'Pressable', 'ScrollView', 'Modal', 'TextInput', 'RefreshControl'].map((name) => [name, native])),
  ActivityIndicator: () => React.createElement('i', { 'data-testid': 'spinner' }),
  Platform: { OS: 'web' },
});
let params;
stubModule('expo-router', { useLocalSearchParams: () => params });
stubModule('../hooks/useBreakpoint', { useBreakpoint: () => ({ isMd: true }) });
let preferences;
let hasServerData;
stubModule('../hooks/usePreferences', { usePreferences: () => ({ data: preferences, hasServerData }) });
for (const [file, name] of [
  ['BulkEditSheet', 'BulkEditSheet'], ['BankBadge', 'BankBadge'],
  ['transactions/MonthCarousel', 'MonthCarousel'], ['transactions/TxnDetailModal', 'TxnDetailModal'],
]) stubModule(`../components/${file}`, { [name]: () => null });
const ownerKey = 'screen-test-owner';
const ownerEpoch = 7;
const datasetKey = ['frontend-dataset', 'replica', ownerKey, ownerEpoch];
const brokerageKey = ['snaptrade', 'portfolio', ownerKey, ownerEpoch];
const accountsKey = ['accounts', ownerKey, ownerEpoch];
let brokerageOptions;
stubModule('@tanstack/react-query', {
  ...require('@tanstack/react-query'),
  useQuery: (options) => {
    // SSR skips the effect that publishes updated options to the query cache.
    if (options.queryKey[0] === 'snaptrade') brokerageOptions = options;
    return useQuery(options);
  },
});
const pending = () => new Promise(() => {});
let ownerCalls;
const ownerApi = async (path) => { ownerCalls.push(path); return path === '/accounts' ? [] : emptyPortfolio; };
stubModule('../lib/api', { api: () => assert.fail('unscoped API request'), formatApiError: (error) => error.message });
stubModule('../hooks/useFrontendDatasetCache', {
  useFrontendDatasetCache: () => ({
    ...useQuery({ queryKey: datasetKey, queryFn: pending }),
    ownerKey, ownerEpoch, ownerApi, refreshSnapshot: pending, isRefreshingChanges: false,
  }),
});
const TransactionsScreen = require('../app/(tabs)/transactions').default;
const { currentPeriodKey } = require('./period');
const transaction = {
  id: 1, bank: 'cathay', kind: 'twd', date: `${currentPeriodKey('month')}-10`,
  description: 'cached-lunch', category: '飲食', subcategory: '午餐', amount: -100,
  currency: 'TWD', account_no: 'test-account', account_or_card: 'test-account',
  cashflow_direction: 'expense', cashflow_amount: 100, excluded: false, auto_excluded: false,
};
const emptyPortfolio = { accounts: [], activities: [], positions: [], balances: [] };
let client;
beforeEach(() => {
  params = {};
  preferences = { fx_display_mode: 'original', card_date_basis: 'consume', show_snaptrade_transactions: true };
  hasServerData = true;
  ownerCalls = [];
  client = new QueryClient({ defaultOptions: { queries: { retry: false, retryOnMount: false, staleTime: Infinity, gcTime: Infinity } } });
  client.setQueryData(datasetKey, { transactions: [transaction], preferences });
  client.setQueryData(accountsKey, [{ bank: 'cathay', has_creds: true }]);
});
afterEach(() => client.clear());

function nodes(tree) {
  return React.Children.toArray(tree).flatMap((node) => React.isValidElement(node)
    ? [node, ...nodes(node.props.children)] : []);
}
function render(actions = []) {
  function Driver() {
    const step = React.useRef(0);
    const [, rerender] = React.useState(0);
    // Invoke during this render so React applies screen callbacks to its real
    // useState hooks, without a DOM dependency or hook-index overrides.
    const tree = TransactionsScreen();
    if (step.current < actions.length) {
      actions[step.current++](nodes(tree));
      rerender(step.current);
    }
    return tree;
  }
  return renderToStaticMarkup(React.createElement(QueryClientProvider, { client }, React.createElement(Driver)));
}
const press = (id) => (tree) => {
  const node = tree.find((node) => node.props.testID === id);
  assert.ok(node, `missing control ${id}`);
  node.props.onPress();
};
const has = (html, id) => html.includes(`data-testid="${id}"`);

const cachedPortfolio = () => ({
  ...emptyPortfolio,
  accounts: [{ id: 'broker', institution_name: 'Test broker' }],
  activities: [{ id: 'trade', account_id: 'broker', type: 'BUY', symbol: 'CACHED', trade_date: transaction.date, amount: '-100', currency: 'USD' }],
});
const refreshControl = (tree) => tree.find((node) => node.props.refreshControl).props.refreshControl.props;

for (const value of [undefined, false, 'true', 1]) {
  test(`only boolean true opts in: ${String(value)} hides cached rows and counts`, () => {
    if (value === undefined) delete preferences.show_snaptrade_transactions;
    else preferences.show_snaptrade_transactions = value;
    client.setQueryData(brokerageKey, cachedPortfolio());
    const html = render();
    assert.equal(brokerageOptions.enabled, false);
    assert.ok(html.includes(transaction.description));
    assert.ok(!html.includes('CACHED'));
    assert.match(html, /共 <div[^>]*>1<\/div> 筆/);
    assert.ok(!html.includes('該期間 2 筆'));
    assert.equal(client.getQueryData(brokerageKey).activities.length, 1, 'shared cache must remain intact');
  });
}

for (const state of ['pending', 'error', 'refetching']) {
  test(`off suppresses brokerage ${state} and explicit refresh`, async () => {
    preferences.show_snaptrade_transactions = false;
    if (state === 'error') await failQuery(brokerageKey);
    if (state === 'refetching') {
      client.setQueryData(brokerageKey, cachedPortfolio());
      void client.fetchQuery({ queryKey: brokerageKey, staleTime: 0, queryFn: pending }).catch(() => {});
    }
    let refresh;
    const html = render([(tree) => { refresh = refreshControl(tree); }]);
    assert.ok(!has(html, 'txn-brokerage-loading'));
    assert.ok(!has(html, 'txn-brokerage-error'));
    assert.ok(!has(html, 'spinner'));
    assert.equal(refresh.refreshing, false);
    refresh.onRefresh();
    assert.ok(!ownerCalls.includes('/snaptrade/portfolio'));
    assert.equal(brokerageOptions.enabled, false);
  });
}

for (const view of ['list', 'category']) {
  test(`off ${view} empty scope does not call intentionally disabled brokerage unknown`, () => {
    preferences.show_snaptrade_transactions = false;
    client.setQueryData(datasetKey, { transactions: [], preferences });
    client.setQueryData(accountsKey, []);
    const html = render(view === 'category' ? [press('txn-view-category')] : []);
    assert.ok(has(html, 'txn-empty'));
    assert.ok(!has(html, 'txn-sources-unknown'));
    assert.ok(!has(html, 'spinner'));
    assert.ok(html.includes('目前顯示範圍沒有交易來源'));
    assert.ok(!html.includes('還沒有任何交易來源'));
    assert.ok(html.includes('設定'));
  });

  test(`off ${view} unknown inventory pull-to-refresh never fetches brokerage`, async () => {
    preferences.show_snaptrade_transactions = false;
    client.setQueryData(datasetKey, { transactions: [], preferences });
    client.removeQueries({ queryKey: accountsKey });
    await failQuery(accountsKey);
    let refresh;
    const actions = view === 'category' ? [press('txn-view-category')] : [];
    const html = render([...actions, (tree) => { refresh = refreshControl(tree); }]);
    assert.ok(has(html, 'txn-sources-unknown'), 'bank inventory genuinely remains unknown');
    assert.equal(brokerageOptions.enabled, false);
    refresh.onRefresh();
    assert.ok(ownerCalls.includes('/accounts'));
    assert.ok(!ownerCalls.includes('/snaptrade/portfolio'), 'explicit inventory fallback must respect off');
  });
}

const text = (tree) => React.Children.toArray(tree).map((node) => React.isValidElement(node)
  ? text(node.props.children) : String(node)).join('');
const choose = (label) => (tree) => {
  const node = tree.find((node) => node.props.accessibilityRole === 'button'
    && text(node) === label);
  assert.ok(node, `missing filter ${label}`);
  node.props.onPress();
};
const searchFor = (value) => (tree) => tree.find((node) => node.props.onChangeText)?.props.onChangeText(value);

for (const [name, actions, marker] of [
  ['category view', [press('txn-view-category')], 'txn-cat-row-飲食'],
  ['expense direction', [press('expense-card-toggle')], transaction.description],
  ['selection mode', [press('txn-selection-enter')], transaction.description],
  ['category filter', [press('txn-filter-open'), choose('飲食'), press('txn-filter-done')], transaction.description],
  ['subcategory filter', [press('txn-filter-open'), choose('飲食'), choose('午餐'), press('txn-filter-done')], transaction.description],
]) test(`${name} renders local results without irrelevant brokerage loading status`, () => {
  const html = render(actions);
  assert.ok(html.includes(marker));
  assert.ok(!has(html, 'txn-brokerage-loading'), 'brokerage cannot contribute to this view');
  assert.ok(!has(html, 'spinner'));
  assert.equal(brokerageOptions.enabled, false);
});

test('search remains brokerage-relevant until its matching rows are known', () => {
  const html = render([press('txn-filter-open'), searchFor('not-local'), press('txn-filter-done')]);
  assert.ok(has(html, 'txn-brokerage-loading'));
  assert.ok(!has(html, 'txn-empty'), 'pending search source is not a confirmed no-match');
  assert.ok(has(html, 'spinner'));
  assert.equal(brokerageOptions.enabled, true);
});

test('on → off → on recomputes the mounted screen without clearing shared cache', () => {
  client.setQueryData(brokerageKey, cachedPortfolio());
  const brokerageRows = (tree) => tree.filter((node) => node.props.activity);
  const html = render([
    (tree) => {
      assert.equal(brokerageRows(tree).length, 1);
      preferences = { ...preferences, show_snaptrade_transactions: false };
    },
    (tree) => {
      assert.equal(brokerageRows(tree).length, 0);
      assert.equal(brokerageOptions.enabled, false);
      refreshControl(tree).onRefresh();
      assert.ok(!ownerCalls.includes('/snaptrade/portfolio'));
      preferences = { ...preferences, show_snaptrade_transactions: true };
    },
    (tree) => {
      assert.equal(brokerageRows(tree).length, 1);
      assert.equal(brokerageOptions.enabled, true);
    },
  ]);
  assert.ok(html.includes('CACHED'));
  assert.equal(client.getQueryData(brokerageKey).activities.length, 1);
});

test('a brokerage response completing after opt-out stays cached but cannot reveal rows', async () => {
  let complete;
  const request = client.fetchQuery({ queryKey: brokerageKey, queryFn: () => new Promise((resolve) => { complete = resolve; }) });
  assert.ok(has(render(), 'txn-brokerage-loading'));
  preferences = { ...preferences, show_snaptrade_transactions: false };
  assert.ok(!has(render(), 'txn-brokerage-loading'));
  complete(cachedPortfolio());
  await request;
  const html = render();
  assert.ok(!html.includes('CACHED'));
  assert.equal(brokerageOptions.enabled, false);
  assert.equal(client.getQueryData(brokerageKey).activities.length, 1);
});

for (const saved of [undefined, false, true]) {
  test(`replica fallback preserves opt-in ${String(saved)} until authoritative preferences arrive`, () => {
    hasServerData = false;
    preferences = { fx_display_mode: 'auto' };
    client.setQueryData(datasetKey, { transactions: [transaction], preferences: { ...preferences, show_snaptrade_transactions: saved } });
    client.setQueryData(brokerageKey, cachedPortfolio());
    assert.equal(render().includes('CACHED'), saved === true);
    hasServerData = true;
    preferences = { ...preferences, show_snaptrade_transactions: false };
    assert.ok(!render().includes('CACHED'), 'explicit server false must win over replica true');
  });
}

test('opt-in retains search, period filtering, count and refresh behavior', () => {
  const portfolio = cachedPortfolio();
  portfolio.activities.push({ ...portfolio.activities[0], id: 'older', symbol: 'OLD', trade_date: '2000-01-01' });
  client.setQueryData(brokerageKey, portfolio);
  let html = render();
  assert.ok(html.includes('CACHED'));
  assert.ok(!html.includes('OLD'));
  assert.match(html, /共 <div[^>]*>2<\/div> 筆/);
  html = render([press('txn-filter-open'), searchFor('BUY'), press('txn-filter-done')]);
  assert.ok(html.includes('CACHED'));
  assert.ok(!html.includes(transaction.description));
  assert.ok(has(render([press('txn-filter-open'), searchFor('absent'), press('txn-filter-done')]), 'txn-empty'));
  let refresh;
  render([(tree) => { refresh = refreshControl(tree); }]);
  refresh.onRefresh();
  assert.ok(ownerCalls.includes('/snaptrade/portfolio'));
});

// Tracer bullet: the existing global gate renders only a spinner here.
test('cached bank row renders while independent brokerage query is cold pending', () => {
  const html = render();
  assert.ok(html.includes(transaction.description), 'ready local row must not wait for brokerage');
  assert.ok(has(html, 'txn-brokerage-loading'), 'partial brokerage status must remain visible');
  assert.ok(!has(html, 'spinner'), 'ready rows must not be replaced by a blocking spinner');
});

test('offline paused brokerage preserves cached rows and does not report unknown rows as empty', () => {
  onlineManager.setOnline(false);
  try {
    const html = render();
    assert.ok(html.includes(transaction.description));
    assert.ok(has(html, 'txn-brokerage-loading'));
    assert.ok(!has(html, 'spinner'));
    client.setQueryData(datasetKey, { transactions: [], preferences });
    const empty = render();
    assert.ok(has(empty, 'spinner'));
    assert.ok(!has(empty, 'txn-empty'));
  } finally {
    onlineManager.setOnline(true);
  }
});

async function failQuery(queryKey) {
  await assert.rejects(client.fetchQuery({ queryKey, staleTime: 0, queryFn: async () => { throw new Error('offline-test'); } }));
}

for (const actions of [[], [press('txn-view-category')]]) {
  test(`retained local cache survives background errors in ${actions.length ? 'category' : 'list'} view`, async () => {
    await failQuery(datasetKey);
    await failQuery(brokerageKey);
    const html = render(actions);
    assert.ok(html.includes(actions.length ? 'txn-cat-row-飲食' : transaction.description), 'retained data must render');
    assert.ok(has(html, 'txn-dataset-error'), 'refresh failure must not be silently hidden');
    assert.equal(has(html, 'txn-brokerage-error'), actions.length === 0);
  });
}

test('cold sources show loading, not a false empty state', () => {
  client.removeQueries({ queryKey: datasetKey });
  const html = render();
  assert.ok(has(html, 'spinner'), 'cold source loading must be visible');
  assert.ok(!has(html, 'txn-empty'), 'unknown rows are not empty rows');
});

for (const scope of [{ bank: 'cathay' }, { bank: 'cathay', account_no: 'test-account' }, { bank: 'cathay', card_no: 'test-card' }]) {
  test(`bank/account/card scope ignores pending or failed brokerage: ${JSON.stringify(scope)}`, async () => {
    params = scope;
    for (const failed of [false, true]) {
      if (failed) await failQuery(brokerageKey);
      const html = render();
      assert.ok(!has(html, 'txn-brokerage-loading'), 'out-of-scope pending must not leak');
      assert.ok(!has(html, 'txn-brokerage-error'), 'out-of-scope error must not leak');
      assert.ok(!has(html, 'spinner'), 'out-of-scope query must not block');
      assert.equal(client.getQueryCache().find({ queryKey: brokerageKey, exact: true }).options.enabled, false);
    }
  });
}

test('empty category view ignores an unavailable brokerage source', async () => {
  client.setQueryData(datasetKey, { transactions: [], preferences });
  await failQuery(brokerageKey);
  const html = render([press('txn-view-category')]);
  assert.ok(has(html, 'txn-empty'), 'local empty result must render');
  assert.ok(!html.includes('券商交易目前無法載入'), 'irrelevant brokerage failure must not replace empty category state');
});

test('pending account inventory is unknown, not proof there are no sources', () => {
  client.setQueryData(datasetKey, { transactions: [], preferences });
  client.setQueryData(brokerageKey, emptyPortfolio);
  client.removeQueries({ queryKey: accountsKey });
  const html = render();
  assert.ok(!has(html, 'spinner'), 'metadata cannot add rows and must not block');
  assert.ok(has(html, 'txn-empty'), 'ready empty row sources can show an empty filter');
  assert.ok(has(html, 'txn-sources-unknown'), 'pending metadata must not claim no sources');
});

test('failed account inventory is unknown, not proof there are no sources', async () => {
  client.setQueryData(datasetKey, { transactions: [], preferences });
  client.setQueryData(brokerageKey, emptyPortfolio);
  client.removeQueries({ queryKey: accountsKey });
  await failQuery(accountsKey);
  const html = render();
  assert.ok(has(html, 'txn-sources-unknown'), 'inventory failure must be distinguished from no accounts');
  assert.ok(!has(html, 'txn-no-sources'), 'unknown source inventory is not empty');
});

test('confirmed empty sources differ from a known source with no matching transactions', () => {
  client.setQueryData(datasetKey, { transactions: [], preferences });
  client.setQueryData(brokerageKey, emptyPortfolio);
  client.setQueryData(accountsKey, []);
  assert.ok(has(render(), 'txn-no-sources'), 'all successful empty sources may show no sources');
  client.setQueryData(accountsKey, [{ bank: 'cathay', has_creds: true }]);
  const html = render();
  assert.ok(has(html, 'txn-empty'), 'known source with no rows is an empty filter');
  assert.ok(!has(html, 'txn-no-sources'), 'known source must not be described as absent');
});

test('known brokerage accounts are not described as absent in category view', () => {
  client.setQueryData(datasetKey, { transactions: [], preferences });
  client.setQueryData(accountsKey, []);
  client.setQueryData(brokerageKey, { ...emptyPortfolio, accounts: [{ id: 'broker', institution_name: 'Test broker' }] });
  const html = render([press('txn-view-category')]);
  assert.ok(has(html, 'txn-empty'), 'category result is empty');
  assert.ok(!has(html, 'txn-no-sources'), 'a source excluded from the view still exists');
});

for (const state of ['pending', 'error']) test(`cached brokerage rows render while bank rows are ${state}`, async () => {
  client.removeQueries({ queryKey: datasetKey });
  if (state === 'error') await failQuery(datasetKey);
  client.setQueryData(brokerageKey, {
    ...emptyPortfolio,
    accounts: [{ id: 'broker', institution_name: 'Test broker' }],
    activities: [{ id: 'trade', account_id: 'broker', type: 'BUY', symbol: 'CACHED', trade_date: transaction.date, amount: '-100', currency: 'USD' }],
  });
  const html = render();
  assert.ok(html.includes('CACHED'), 'the ready source must render independently');
  assert.ok(has(html, `txn-dataset-${state === 'pending' ? 'loading' : 'error'}`), 'partial bank status must be visible');
  assert.ok(!has(html, 'spinner'), 'ready brokerage rows must not be blocked by bank rows');
});

test('pull-to-refresh retries unknown account inventory without making it a row gate', async () => {
  client.setQueryData(datasetKey, { transactions: [], preferences });
  client.setQueryData(brokerageKey, emptyPortfolio);
  client.removeQueries({ queryKey: accountsKey });
  await failQuery(accountsKey);
  let refresh;
  render([(tree) => { refresh = tree.find((node) => node.props.refreshControl).props.refreshControl.props.onRefresh; }]);
  refresh();
  assert.ok(ownerCalls.includes('/accounts'), 'the unknown-source retry must actually retry inventory');
});

test('category pull-to-refresh retries missing brokerage inventory through the guarded API', async () => {
  client.setQueryData(datasetKey, { transactions: [], preferences });
  client.setQueryData(accountsKey, []);
  await failQuery(brokerageKey);
  let refresh;
  const html = render([press('txn-view-category'), (tree) => {
    refresh = tree.find((node) => node.props.refreshControl).props.refreshControl.props.onRefresh;
  }]);
  assert.ok(has(html, 'txn-sources-unknown'));
  assert.ok(html.includes('請下拉重新整理'));
  assert.ok(!has(html, 'spinner'), 'unknown metadata must not block ready rows');
  assert.equal(brokerageOptions.enabled, false);
  assert.deepEqual(ownerCalls, [], 'no passive inventory request');
  refresh();
  assert.ok(ownerCalls.includes('/snaptrade/portfolio'), 'explicit refresh must retry missing brokerage inventory');
});

test('account and brokerage queries share the dataset owner session and guarded API', async () => {
  render();
  for (const [queryKey, path] of [[accountsKey, '/accounts'], [brokerageKey, '/snaptrade/portfolio']]) {
    const query = client.getQueryCache().find({ queryKey, exact: true });
    assert.equal(typeof query?.options.queryFn, 'function', 'screen must use the shared owner-scoped query');
    await query.options.queryFn();
    assert.equal(ownerCalls.at(-1), path);
  }
  assert.equal(client.getQueryCache().find({ queryKey: ['snaptrade', 'portfolio'], exact: true }), undefined);
  assert.equal(client.getQueryCache().find({ queryKey: ['accounts'], exact: true }), undefined);
});
