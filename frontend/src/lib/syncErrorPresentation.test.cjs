const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const ts = require('typescript');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');

const filename = path.resolve(__dirname, '../app/(tabs)/cards/index.tsx');
const compiled = ts.transpileModule(fs.readFileSync(filename, 'utf8') + '\nexport { BankGroupCard, PendingBankAccountRow };', {
  compilerOptions: {jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS},
}).outputText;

function render(job, pending = false) {
  const texts = [];
  const box = ({children}) => React.createElement('div', null, children);
  const empty = () => null;
  const mocks = {
    'react': React,
    'react/jsx-runtime': require('react/jsx-runtime'),
    'react-native': {Text: (props) => {texts.push(props); return React.createElement('span', null, props.children);},
      View: box, Pressable: box, Modal: empty, Platform: {OS: 'web'}, ActivityIndicator: empty},
    'expo-router': {useRouter: () => ({})},
    '@tanstack/react-query': {},
    'lucide-react-native': {Cloud: empty, MoreHorizontal: empty},
    '@/components/BankBadge': {BankBadge: empty},
    '@/components/AutoDebitSettingModal': {AutoDebitSettingModal: empty},
    '@/components/SnapTradeSections': {},
    '@/hooks/useBreakpoint': {},
    '@/hooks/useFrontendDatasetCache': {},
    '@/lib/accountTabCache': {},
    '@/lib/api': {}, '@/lib/currency': {}, '@/lib/mask': {}, '@/lib/replica': {},
    '@/lib/datetime': {formatRelativeTime: () => '剛剛'},
    '@/types/api': {BANK_LABELS: {esun: '玉山銀行'}},
  };
  const module = {exports: {}};
  vm.runInNewContext(compiled, {module, exports: module.exports,
    require(name) {assert.ok(name in mocks, `unexpected dependency ${name}`); return mocks[name];}}, {filename});
  const Component = pending ? module.exports.PendingBankAccountRow : module.exports.BankGroupCard;
  const props = pending ? {account: {id: 1, has_creds: true}, isLast: true, lastJob: job} : {
    group: {bank: 'esun', accounts: [], cards: [], pendingBankAccounts: []}, lastJob: job,
  };
  const html = renderToStaticMarkup(React.createElement(Component, props));
  return {html, texts};
}

const message = '[ESUN-00-001-1234567] 載入銀行模組：銀行模組無法載入';
for (const pending of [false, true]) {
  test(`full failure copy is selectable and not line clipped (pending=${pending})`, () => {
    const {html, texts} = render({status: 'failed', error_msg: message, finished_at: '2026-01-01'}, pending);
    assert.ok(html.includes(message));
    const diagnostic = texts.find(p => JSON.stringify(p.children).includes(message));
    assert.ok(diagnostic);
    assert.equal(diagnostic.numberOfLines, undefined);
    assert.equal(diagnostic.selectable, true);
  });
  test(`failed legacy row renders without inventing an ID (pending=${pending})`, () => {
    const {html} = render({status: 'failed', error_msg: null, finished_at: null}, pending);
    assert.ok(html.includes('上次同步失敗'));
    assert.ok(!html.includes('[ESUN-'));
  });
}

test('real API decoder and React retain backend REST/WS failure copy', async () => {
  assert.ok(process.env.SYNC_ERROR_HTTP_FIXTURE, 'backend-produced checkpoint contract is required');
  const fixtures = JSON.parse(fs.readFileSync(process.env.SYNC_ERROR_HTTP_FIXTURE, 'utf8'));
  const expected = ['ESUN-03-900-001', 'ESUN-07-960-001', 'ESUN-01-910-001',
    'ESUN-08-900-001', 'ESUN-08-800-002', 'UBOT-03-065-001', 'UBOT-03-070-001'];
  assert.equal(fixtures.length, expected.length);
  for (const code of expected) {
    assert.equal(fixtures.filter(row => row.prefix.startsWith(`[${code}]`)).length, 1);
  }
  function stub(id, exports) {
    const filename = require.resolve(id);
    require.cache[filename] = {id: filename, filename, loaded: true, exports};
  }
  stub('react-native', {Platform: {OS: 'web'}});
  stub('expo-secure-store', {});
  stub('expo-router', {router: {replace: () => assert.fail('unexpected auth navigation')}});
  stub('./credentials', {hasCredentials: async () => false, loadCredentials: () => assert.fail('no credentials')});
  const {api} = require('./api');
  const {useAuthStore} = require('../stores/auth');
  const {activateReplicaOwner, makeReplicaOwnerKey} = require('./replica');
  useAuthStore.setState({serverUrl: 'https://offline.invalid', email: 'synthetic@palace.example', token: 'synthetic-token', refreshToken: null, apiKey: '', hydrated: true});
  activateReplicaOwner(makeReplicaOwnerKey('https://offline.invalid', 'synthetic@palace.example'));
  const originalFetch = globalThis.fetch;
  try {
    for (const fixture of fixtures) {
      globalThis.fetch = async (url) => new Response(JSON.stringify(String(url).endsWith('/sync/jobs') ? fixture.list : fixture.detail));
      const detail = await api(`/sync/jobs/${fixture.detail.id}`);
      const list = await api('/sync/jobs');
      assert.equal(detail.error_msg, fixture.ws.error_msg);
      assert.equal(detail.error_msg, list.find(j => j.id === detail.id).error_msg);
      for (const pending of [false, true]) {
        assert.ok(render(detail, pending).html.includes(fixture.prefix));
      }
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
});
