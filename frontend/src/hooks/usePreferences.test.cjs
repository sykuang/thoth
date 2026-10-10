const assert = require('node:assert/strict');
const { test } = require('node:test');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { QueryClient, QueryClientProvider } = require('@tanstack/react-query');
function stubModule(id, exports) { const f = require.resolve(id); require.cache[f] = { id: f, filename: f, loaded: true, exports }; }
stubModule('../hooks/useOwnerBoundApi', { useOwnerBoundApi: () => ({ ownerKey: 'o', ownerEpoch: 1 }) });
stubModule('react-native', {});
stubModule('../lib/replica', { assertReplicaOwnerEpoch(){}, getReplicaOwnerEpoch: () => 1, makeReplicaOwnerKey: () => 'o', ReplicaSyncCancelledError: class {}, updateReplicaPreferences(){} });
stubModule('../lib/replicaStore', { replicaStore: {} });
stubModule('../stores/auth', { useAuthStore: { getState: () => ({}) } });
stubModule('../lib/api', { api: () => new Promise(() => {}) });
const { usePreferences } = require('./usePreferences');

test('settings falls back to replica prefs, not DEFAULT, when GET has no data', () => {
  const client = new QueryClient();
  client.setQueryData(['frontend-dataset', 'replica', 'o', 1], { preferences: { card_date_basis: 'post' } });
  let seen;
  const Probe = () => { seen = usePreferences().data.card_date_basis; return null; };
  renderToStaticMarkup(React.createElement(QueryClientProvider, { client }, React.createElement(Probe)));
  assert.equal(seen, 'post');
});
