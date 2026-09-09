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
const native = ({ children, testID, visible, className }) => visible === false ? null
  : React.createElement('div', { 'data-testid': testID, className }, children);
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
test('loan facts render neutral positive principal and a loan expense summary without reconciliation warnings, never selectable', () => {
  const {projectReplicaDataset} = require('./replica');
  const facts = {id:'loan:v1:test', bank:'cathay', source_account_id:1, account_no:'123456789', sub_account:'', currency:'TWD', due_date:transaction.date, paid_on:transaction.date, status:'paid', query_start:null, query_end:null, principal:'100.001', interest:'0.1', penalty:'0.2', paid_total:'100.301', principal_balance:'900'};
  const dataset = projectReplicaDataset({partitions:{'bank:cathay':{transactions:[],loan_repayments:[facts]}},generations:{},syncedAt:transaction.date});
  client.setQueryData(datasetKey,{...dataset,preferences});
  const html = render();
  assert.ok(has(html, 'loan-expense-summary'));
  assert.ok(!html.includes('未核對'));
  assert.ok(!has(html, 'loan-reconciliation-warning'));
  assert.ok(dataset.transactions.every(t => t.reconciliation_status === 'unverified'));
  assert.match(html, /data-testid="expense-card-toggle"[^]*?NT\$ 0\.3/);
  assert.ok(!html.includes('未併入上方統計'));
  assert.ok(html.includes('+NT$ 100.001'));
  assert.ok(html.includes('-NT$ 0.1'));
  assert.ok(html.includes('123456789'));
  const {TxnRow} = require('../components/transactions/TxnRow');
  for (const wide of [false, true]) {
    const row = TxnRow.type({t:dataset.transactions[0], wide, fxMode:'original',
      selectionMode:true, onLongPress:()=>assert.fail('loan must remain read-only')});
    assert.equal(row.props.onLongPress, undefined);
    const markup = renderToStaticMarkup(row);
    assert.ok(!markup.includes('唯讀'));
    if (!wide) assert.ok(markup.includes(' · 貸款'));
  }
  const categoryHtml = render([press('txn-view-category')]);
  assert.ok(categoryHtml.includes('-NT$ 0.3'));
  assert.ok(!categoryHtml.includes('+NT$ 100.001'));
  const selected = render([press('txn-selection-enter'), tree => {
    const row = tree.find(node => node.props.t?.kind === 'loan_repayment');
    assert.ok(row); row.props.onLongPress(); row.props.onPress();
  }]);
  assert.ok(selected.includes('已選 0 筆'));
});

test('loan detail edits category and inclusion through real React hooks and Query mutation, invalidates canonical replica on success and error', async () => {
  const filename = require.resolve('../components/transactions/TxnDetailModal');
  delete require.cache[filename];
  for (const name of ['CategoryPicker', 'Dropdown']) stubModule(`../components/${name}`, {[name]: native});
  stubModule('../components/TagPicker', {TagPicker: () => assert.fail('loan tags unsupported')});
  stubModule('../components/transactions/SplitEditor', {SplitEditor: () => assert.fail('loan splits unsupported')});
  let fail = false;
  const calls = [];
  const replica = require('./replica');
  let auth = {token:'test-token',email:'loan@example.test',serverUrl:'https://example.test'};
  stubModule('../stores/auth', {useAuthStore: selector => selector(auth)});
  const editOwner = replica.makeReplicaOwnerKey(auth.serverUrl, auth.email);
  replica.activateReplicaOwner(editOwner);
  const editEpoch = replica.getReplicaOwnerEpoch(editOwner);
  let complete;
  let delayed = false;
  stubModule('../lib/api', {api: async (path, options) => {
    assert.equal(options?.authRetryKey, `${editOwner}:${editEpoch}`, 'unscoped API request');
    assert.equal(typeof options.authRetryGuard, 'function');
    calls.push({path,method:options.method,body:options.body});
    if(fail) throw Error('save denied');
    if(delayed) await new Promise(resolve => {complete = resolve;});
    return txn;
  }, formatApiError:e=>e.message});
  delete require.cache[require.resolve('../hooks/useOwnerBoundApi')];
  const {LoanTxnDetail, TxnDetailModal} = require('../components/transactions/TxnDetailModal');
  const {projectLoanRepayment} = require('./loanRepayments');
  const txn = projectLoanRepayment({id:'loan:v1:detail',bank:'cathay',source_account_id:1,account_no:'123456789',sub_account:'A',currency:'USD',paid_on:transaction.date,due_date:transaction.date,status:'paid',query_start:null,query_end:null,principal:'1.001',interest:'0.1',penalty:'0',paid_total:'1.101',principal_balance:'99.999'})[0];
  assert.equal(TxnDetailModal({txn}).type, LoanTxnDetail);
  let close = 0;
  let save;
  let dismiss;
  let editingTxn = txn;
  function Driver() {
    const step = React.useRef(0);
    const [,rerender] = React.useState(0);
    const tree = LoanTxnDetail({txn:editingTxn,fxMode:'auto',onClose:()=>close++});
    const all = nodes(tree);
    dismiss = all.find(n => n.props.accessibilityLabel === '關閉貸款明細').props.onPress;
    const control = id => all.find(n=>n.props.testID===id);
    if(step.current===0) {assert.equal(control('txn-detail-save').props.disabled,true); control('txn-detail-category-dropdown').props.onChange('住房');}
    if(step.current===1) {assert.equal(control('txn-detail-subcategory-dropdown').props.value,'');control('txn-detail-subcategory-dropdown').props.onChange('房貸');control('txn-detail-ignore-toggle').props.onPress();}
    if(step.current===2) {assert.equal(control('txn-detail-subcategory-dropdown').props.value,'房貸');assert.ok(control('txn-detail-category-dropdown').props.options.some(o => o.value === '住房'), 'custom category remains visible');assert.ok(control('txn-detail-subcategory-dropdown').props.options.some(o => o.value === '房貸'), 'custom subcategory remains visible');assert.equal(control('txn-detail-ignore-toggle').props.accessibilityState.checked,true);save=control('txn-detail-save').props.onPress;}
    if(step.current++<2) rerender(step.current);
    return tree;
  }
  const renderEditor = () => renderToStaticMarkup(React.createElement(QueryClientProvider,{client},React.createElement(Driver)));
  for (const error of [false,true]) {
    fail=error;
    client.setQueryData(datasetKey,{transactions:[txn]});
    client.setQueryData(['portfolio','summary'],{});
    const html=renderEditor();
    assert.ok(html.includes('儲存') && html.includes('99.999'));
    assert.ok(!/唯讀|未核對|拆帳|標籤/.test(html));
    save();
    await new Promise(resolve=>setTimeout(resolve,20));
    assert.deepEqual(calls.at(-1),{path:'/transactions/cathay/loan_repayment/loan%3Av1%3Adetail%3Aprincipal',method:'PATCH',body:{category:'住房',subcategory:'房貸',auto_excluded:true}});
    assert.equal(client.getQueryState(datasetKey).isInvalidated,true);
    assert.equal(client.getQueryState(['portfolio','summary']).isInvalidated,true);
    assert.equal(close,1,'failed save must not close');
    const mutation=client.getMutationCache().getAll().at(-1);
    assert.equal(mutation.state.status,error?'error':'success');
    if(error) {
      assert.equal(mutation.state.error.message,'save denied');
      assert.deepEqual(mutation.state.variables,{category:'住房',subcategory:'房貸',auto_excluded:true},'failed edit remains available without reset');
    }
    assert.deepEqual(client.getQueryData(datasetKey).transactions[0],txn,'no optimistic loan arithmetic');
  }
  fail = false;
  delayed = true;
  renderEditor();
  save();
  await new Promise(resolve => setTimeout(resolve, 20));
  dismiss();
  const afterDismiss = close;
  editingTxn = {...txn,id:'loan:v1:other:principal'};
  renderEditor(); // Another real React edit instance with unsaved edits.
  const newerSave = save;
  complete();
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(close, afterDismiss, 'dismissed A success must not close B');
  assert.equal(client.getQueryState(datasetKey).isInvalidated, true);
  // B's retained production save callback still carries its edits.
  delayed = false;
  fail = true;
  newerSave();
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.deepEqual(calls.at(-1).body, {category:'住房',subcategory:'房貸',auto_excluded:true});
  assert.equal(calls.at(-1).path, '/transactions/cathay/loan_repayment/loan%3Av1%3Aother%3Aprincipal');
  assert.equal(close, afterDismiss, 'failed B save must not close');
  editingTxn = txn;
  close = 1;
  // SSR rerenders retain the actual hook state, but do not run mount effects.
  const originalAuth = auth;
  function SwitchDriver() {
    const [switched, setSwitched] = React.useState(false);
    const tree = LoanTxnDetail({txn,fxMode:'auto',onClose:()=>close++});
    if (!switched) {
      assert.ok(nodes(tree).some(n => n.props.testID === 'loan-detail'));
      auth = {...auth,email:'direct-switch@example.test'};
      setSwitched(true);
    } else {
      assert.equal(tree, null, 'retained old owner financial modal must disappear immediately');
    }
    return tree;
  }
  renderToStaticMarkup(React.createElement(QueryClientProvider,{client},React.createElement(SwitchDriver)));
  auth = originalAuth;
  fail = false;
  for (const suffix of [['categories'],['subcategories','住房']]) {
    const query = client.getQueryCache().find({queryKey:['rules',...suffix,editOwner,editEpoch],exact:true});
    assert.ok(query, 'category queries must be scoped to the edit owner');
    await query.options.queryFn();
  }
  delayed = true;
  renderEditor();
  save();
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(typeof complete, 'function');
  auth = {...auth,token:null,email:null};
  await replica.clearReplicaOwner({clear:async()=>{}}, 'https://example.test', 'loan@example.test');
  client.setQueryData(datasetKey,{transactions:[txn]});
  client.setQueryData(['portfolio','summary'],{});
  complete();
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(close,1,'late completion must not close');
  assert.equal(client.getQueryState(datasetKey).isInvalidated,false,'late completion must not invalidate');
  const count = calls.length;
  save();
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(calls.length,count,'stale save must never reach API');
  assert.equal(close,1);
  assert.equal(client.getQueryState(['portfolio','summary']).isInvalidated,false);
  // Switch to another active account, then replay the actual old callbacks.
  auth = {token:'other-token',email:'other@example.test',serverUrl:'https://example.test'};
  replica.activateReplicaOwner(replica.makeReplicaOwnerKey(auth.serverUrl,auth.email));
  save();
  const oldMutation = client.getMutationCache().getAll().at(-1);
  await assert.rejects(oldMutation.options.mutationFn(oldMutation.state.variables), /owner transition/);
  oldMutation.options.onSuccess(txn);
  oldMutation.options.onSettled();
  const categoryQuery = client.getQueryCache().find({queryKey:['rules','categories',editOwner,editEpoch],exact:true});
  await assert.rejects(categoryQuery.options.queryFn(), /owner transition/);
  assert.equal(calls.length,count);
  assert.equal(close,1,'stale production success callback cannot close another account');
  assert.equal(client.getQueryState(datasetKey).isInvalidated,false);
});

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
