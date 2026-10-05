const assert = require('node:assert/strict');
const { afterEach, beforeEach, test } = require('node:test');
const { setImmediate } = require('node:timers/promises');

// Stub native/persistence boundaries only; exercise the real API, auth store,
// refresh policy and replica owner epochs. No network or saved credentials.
function stubModule(id, exports) {
  const filename = require.resolve(id);
  require.cache[filename] = { id: filename, filename, loaded: true, exports };
}
stubModule('react-native', { Platform: { OS: 'web' } });
stubModule('expo-secure-store', {});
stubModule('expo-router', { router: { replace: () => assert.fail('unexpected navigation') } });
let hasSavedCredentials = async () => false;
let loadSavedCredentials = async () => assert.fail('unexpected credential access');
stubModule('./credentials', {
  hasCredentials: () => hasSavedCredentials(),
  loadCredentials: () => loadSavedCredentials(),
});
const { api, ApiError } = require('./api');
const { useAuthStore } = require('../stores/auth');
const { activateReplicaOwner, makeReplicaOwnerKey, getReplicaOwnerEpoch, assertReplicaOwnerEpoch } =
  require('./replica');

const originalFetch = globalThis.fetch;
const calls = [];
let transport;
const json = (value, status = 200) => new Response(JSON.stringify(value), { status });
function deferred() {
  let resolve;
  const promise = new Promise((r) => { resolve = r; });
  return { promise, resolve };
}
const flush = () => setImmediate();
let testId = 0;
beforeEach(() => {
  calls.length = 0;
  hasSavedCredentials = async () => false;
  loadSavedCredentials = async () => assert.fail('unexpected credential access');
  useAuthStore.setState({
    serverUrl: 'https://warmup.invalid/api/', email: `test-${++testId}@example.invalid`,
    token: 'test-access', refreshToken: 'test-refresh', apiKey: 'test-key', hydrated: true,
  });
  const state = useAuthStore.getState();
  activateReplicaOwner(makeReplicaOwnerKey(state.serverUrl, state.email));
  transport = async () => assert.fail('unexpected fetch');
  globalThis.fetch = (async (url, init = {}) => {
    const call = { url: String(url), init };
    calls.push(call);
    return transport(call);
  });
});
afterEach(() => { globalThis.fetch = originalFetch; });

for (const status of [301, 302, 307, 401, 403, 404, 429, 500]) {
  test(`health HTTP ${status} fails closed without auth recovery`, async () => {
    transport = async () => json({ status: 'ok' }, status);
    await assert.rejects(api('/sync/all', { method: 'POST' }), (e) => e instanceof ApiError && e.status === status);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].init.method, 'GET');
    assert.equal(useAuthStore.getState().token, 'test-access');
  });
}
for (const body of ['not json', 'null', '{}', '{"status":"OK"}', '{"status":true}', '[{"status":"ok"}]']) {
  test(`malformed health ${body} fails closed`, async () => {
    transport = async () => new Response(body);
    await assert.rejects(api('/sync/all', { method: 'POST' }));
    assert.equal(calls.length, 1);
    assert.equal(calls[0].init.method, 'GET');
  });
}

test('transient transport and 502/503/504 retry only health GETs', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'] });
  const replies = [new TypeError('Network request failed'), 502, 503, 504, 200];
  transport = async ({ url }) => {
    if (!url.endsWith('/healthz')) return json({ queued: 1 }, 202);
    const reply = replies.shift();
    if (reply instanceof Error) throw reply;
    return json({ status: 'ok' }, reply);
  };
  let failure;
  const result = api('/sync/all', { method: 'POST' }).catch((e) => { failure = e; });
  for (let attempt = 1; attempt <= 4; attempt += 1) {
    await flush();
    assert.equal(failure, undefined);
    assert.equal(calls.length, attempt);
    assert.ok(calls.every((c) => c.init.method === 'GET'));
    t.mock.timers.tick(2000);
  }
  assert.deepEqual(await result, { queued: 1 });
  assert.deepEqual(calls.map((c) => c.init.method), ['GET', 'GET', 'GET', 'GET', 'GET', 'POST']);
});

test('Expo native FetchError retries health without resending a mutation', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'] });
  const path = require('node:path');
  const { FetchError } = require(path.join(path.dirname(require.resolve('expo/package.json')), 'src/winter/fetch/FetchErrors.ts'));
  transport = async ({ url }) => {
    if (calls.length === 1) throw new FetchError('The network connection was lost.');
    return url.endsWith('/healthz') ? json({ status: 'ok' }) : json({ queued: 1 }, 202);
  };
  let failure;
  const result = api('/sync/all', { method: 'POST' }).catch((e) => { failure = e; });
  await flush();
  assert.equal(failure, undefined);
  t.mock.timers.tick(2000);
  assert.deepEqual(await result, { queued: 1 });
  assert.deepEqual(calls.map((c) => c.init.method), ['GET', 'GET', 'POST']);
});

test('transient health body transport failure retries GET, not POST', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'] });
  transport = async ({ url }) => {
    if (!url.endsWith('/healthz')) return json({ queued: 1 }, 202);
    const response = json({ status: 'ok' });
    if (calls.length === 1) response.json = async () => { throw new TypeError('terminated'); };
    return response;
  };
  let failure;
  const result = api('/sync/all', { method: 'POST' }).catch((e) => { failure = e; });
  await flush();
  assert.equal(failure, undefined);
  assert.equal(calls.length, 1);
  t.mock.timers.tick(2000);
  assert.deepEqual(await result, { queued: 1 });
  assert.deepEqual(calls.map((c) => c.init.method), ['GET', 'GET', 'POST']);
});

for (const phase of ['headers', 'body', 'backoff']) {
  for (const stop of ['abort', 'deadline']) {
    test(`${stop} during health ${phase} prevents POST, even if transport ignores abort`, async (t) => {
      t.mock.timers.enable({ apis: ['setTimeout', 'Date'] });
      const pending = deferred();
      transport = async () => {
        if (phase === 'headers') return pending.promise;
        if (phase === 'backoff') return json({}, 503);
        const response = json({ status: 'ok' });
        response.json = () => pending.promise;
        return response;
      };
      const ctrl = new AbortController();
      let failure;
      const result = api('/sync/all', { method: 'POST', signal: ctrl.signal }).catch((e) => { failure = e; });
      await flush();
      assert.equal(failure, undefined);
      if (stop === 'abort') ctrl.abort();
      else t.mock.timers.tick(120_000);
      await flush();
      assert.ok(failure, `${stop} must settle the warmup without waiting for transport`);
      assert.equal(calls.length, 1);
      assert.equal(calls[0].init.signal.aborted, true);
      if (stop === 'abort') assert.equal(failure.name, 'AbortError');
      else assert.match(failure.message, /120000|120 秒/);
      pending.resolve(phase === 'body' ? { status: 'ok' } : json({ status: 'ok' }));
      await result;
      t.mock.timers.tick(120_000);
      await flush();
      assert.equal(calls.length, 1, 'no delayed retries or POST after cancellation');
    });
  }
}

test('already aborted sync does not read its body or send health/POST', async () => {
  const ctrl = new AbortController();
  ctrl.abort();
  let bodyReads = 0;
  await assert.rejects(api('/sync/all', {
    method: 'POST', signal: ctrl.signal,
    get body() { bodyReads += 1; return {}; },
  }));
  assert.equal(calls.length, 0);
  assert.equal(bodyReads, 0);
});

function ownerInit() {
  const state = useAuthStore.getState();
  const ownerKey = makeReplicaOwnerKey(state.serverUrl, state.email);
  const ownerEpoch = getReplicaOwnerEpoch(ownerKey);
  return {
    method: 'POST',
    authRetryKey: `${ownerKey}:${ownerEpoch}`,
    authRetryGuard: () => assertReplicaOwnerEpoch(ownerKey, ownerEpoch),
  };
}

for (const change of ['server', 'account', 'logout-login']) {
  test(`${change} during health prevents POST and body capture`, async () => {
    const health = deferred();
    transport = async ({ url }) => url.endsWith('/healthz') ? health.promise : json({ queued: 1 }, 202);
    const init = ownerInit();
    let bodyReads = 0;
    const result = api('/sync/account/17', {
      ...init, get body() { bodyReads += 1; return {}; },
    });
    const rejected = assert.rejects(result);
    await flush();
    const state = useAuthStore.getState();
    if (change === 'server') state.setServerUrl('https://other.invalid/api');
    if (change === 'account') state.setAuth('other-access', 'other@example.invalid', 'other-refresh');
    if (change === 'logout-login') {
      state.logout();
      state.setAuth(state.token, state.email, state.refreshToken);
    }
    await flush();
    health.resolve(json({ status: 'ok' }));
    await rejected;
    assert.equal(bodyReads, 0);
    assert.deepEqual(calls.map((c) => c.init.method), ['GET']);
    assert.equal(calls[0].url, 'https://warmup.invalid/api/healthz');
  });
}

test('stale owner is rejected before any health/POST', async () => {
  const init = ownerInit();
  useAuthStore.getState().logout();
  await assert.rejects(api('/sync/all', init));
  assert.equal(calls.length, 0);
});

test('owner change in backoff prevents even another health GET', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'] });
  transport = async () => json({}, 503);
  let failure;
  const result = api('/sync/all', ownerInit()).catch((e) => { failure = e; });
  await flush();
  useAuthStore.getState().setAuth('other-access', 'other@example.invalid');
  t.mock.timers.tick(2000);
  await flush();
  assert.ok(failure);
  await result;
  assert.equal(calls.length, 1);
});

for (const change of ['owner', 'abort']) {
  test(`${change} during body serialization still prevents POST`, async () => {
    transport = async () => json({ status: 'ok' });
    const ctrl = new AbortController();
    const init = ownerInit();
    const body = { toJSON() {
      if (change === 'owner') useAuthStore.getState().logout();
      else ctrl.abort();
      return { headless: true };
    } };
    await assert.rejects(api('/sync/all', { ...init, signal: ctrl.signal, body }));
    assert.deepEqual(calls.map((c) => c.init.method), ['GET']);
  });
}

test('repeated failures exhaust one 120s budget without POST', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'] });
  transport = async () => json({}, 503);
  let failure;
  const result = api('/sync/all', { method: 'POST' }).catch((e) => { failure = e; });
  for (let attempt = 1; attempt <= 60; attempt += 1) {
    await flush();
    assert.equal(failure, undefined);
    assert.equal(calls.length, attempt);
    t.mock.timers.tick(2000);
  }
  await result;
  assert.match(failure.message, /120 秒/);
  assert.equal(calls.length, 60);
  assert.ok(calls.every((c) => c.init.method === 'GET'));
});

test('non-transport health exceptions fail closed without retries', async () => {
  const error = new Error('unexpected transport bug');
  transport = async () => { throw error; };
  await assert.rejects(api('/sync/all', { method: 'POST' }), (e) => e === error);
  assert.deepEqual(calls.map((c) => c.init.method), ['GET']);
});

for (const failure of ['transport', 502, 503, 504]) {
  test(`sync mutation ${failure} failure is never retried`, async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout', 'Date'] });
    const error = new TypeError('Network request failed');
    transport = async ({ url }) => {
      if (url.endsWith('/healthz')) return json({ status: 'ok' });
      if (failure === 'transport') throw error;
      return json({ detail: 'failed' }, failure);
    };
    await assert.rejects(api('/sync/account/17', ownerInit()), (e) =>
      failure === 'transport' ? e === error : e instanceof ApiError && e.status === failure);
    t.mock.timers.tick(120_000);
    await flush();
    assert.deepEqual(calls.map((c) => c.init.method), ['GET', 'POST']);
  });
}

for (const [path, method] of [
  ['/accounts', 'GET'], ['/sync/jobs', 'GET'], ['/sync/all', 'GET'],
  ['/sync/account/17', 'GET'], ['/accounts', 'POST'], ['/auth/login', 'POST'],
  ['/snaptrade/sync', 'POST'], ['/replica/pull', 'POST'], ['/sync/all/extra', 'POST'],
]) {
  test(`${method} ${path} bypasses warmup`, async () => {
    transport = async () => json({ result: 'ok' });
    assert.deepEqual(await api(path, { method }), { result: 'ok' });
    assert.deepEqual(calls.map((c) => [c.url, c.init.method]), [[`https://warmup.invalid/api${path}`, method]]);
  });
}

test('sync 401 retains one auth refresh and authenticated retry', async () => {
  let posts = 0;
  transport = async ({ url }) => {
    if (url.endsWith('/healthz')) return json({ status: 'ok' });
    if (url.endsWith('/auth/refresh')) return json({ access_token: 'refreshed-access', refresh_token: 'refreshed-refresh' });
    return ++posts === 1 ? json({}, 401) : json({ queued: 1 }, 202);
  };
  assert.deepEqual(await api('/sync/all', ownerInit()), { queued: 1 });
  assert.deepEqual(calls.map((c) => [new URL(c.url).pathname, c.init.method]), [
    ['/api/healthz', 'GET'], ['/api/sync/all', 'POST'], ['/api/auth/refresh', 'POST'],
    ['/api/healthz', 'GET'], ['/api/sync/all', 'POST'],
  ]);
  assert.equal(new Headers(calls[4].init.headers).get('Authorization'), 'Bearer refreshed-access');
});

test('skipAuthRetry keeps sync 401 terminal', async () => {
  transport = async ({ url }) => url.endsWith('/healthz') ? json({ status: 'ok' }) : json({}, 401);
  await assert.rejects(api('/sync/all', { ...ownerInit(), skipAuthRetry: true }), (e) => e.status === 401);
  assert.deepEqual(calls.map((c) => c.init.method), ['GET', 'POST']);
});

for (const path of ['/sync/all', '/sync/account/17']) {
  test(`${path}: anonymous health completes before POST/body/timer capture`, async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout', 'Date'] });
    const health = deferred();
    let bodyReads = 0;
    transport = async ({ url }) => url.endsWith('/healthz') ? health.promise : json({ queued: 1 }, 202);
    const result = api(path, {
      method: 'POST', timeoutMs: 10,
      get body() { bodyReads += 1; return { headless: true }; },
      headers: { 'X-Request-ID': 'test-request' },
    });
    await flush();
    assert.deepEqual(calls.map((c) => [c.url, c.init.method]), [
      ['https://warmup.invalid/api/healthz', 'GET'],
    ]);
    assert.equal(bodyReads, 0);
    const headers = new Headers(calls[0].init.headers);
    assert.equal(headers.has('Authorization'), false);
    assert.equal(headers.has('X-API-Key'), false);
    assert.equal(headers.has('X-Request-ID'), false);
    assert.equal(calls[0].init.credentials, 'omit');
    assert.equal(calls[0].init.cache, 'no-store');
    assert.equal(calls[0].init.redirect, 'manual');
    assert.equal(calls[0].init.body, undefined);
    t.mock.timers.tick(100);
    health.resolve(json({ status: 'ok' }));
    assert.deepEqual(await result, { queued: 1 });
    assert.equal(calls.length, 2);
    const post = calls[1];
    assert.equal(post.url, `https://warmup.invalid/api${path}`);
    assert.equal(post.init.method, 'POST');
    assert.equal(post.init.body, '{"headless":true}');
    assert.equal(post.init.signal?.aborted, false);
    assert.equal(new Headers(post.init.headers).get('Authorization'), 'Bearer test-access');
    assert.equal(new Headers(post.init.headers).get('X-API-Key'), 'test-key');
    assert.equal(new Headers(post.init.headers).get('X-Request-ID'), 'test-request');
  });
}

test('sibling refresh preserves an accepted sync response without replay', async () => {
  const pending = deferred();
  transport = async ({ url, init }) => {
    if (url.endsWith('/healthz')) return json({ status: 'ok' });
    if (url.endsWith('/sync/all')) return pending.promise;
    if (url.endsWith('/auth/refresh')) return json({ access_token: 'rotated-access', refresh_token: 'rotated-refresh' });
    return new Headers(init.headers).get('Authorization') === 'Bearer test-access'
      ? json({}, 401) : json({ ok: true });
  };
  const result = api('/sync/all', ownerInit());
  await flush();
  await api('/accounts');
  pending.resolve(json({ queued: 13 }, 202));
  assert.deepEqual(await result, { queued: 13 });
  assert.equal(calls.filter((c) => c.url.endsWith('/sync/all')).length, 1);
  assert.equal(calls.filter((c) => c.url.endsWith('/auth/refresh')).length, 1);
});

for (const phase of ['headers', 'body']) {
  test(`same-session rotation during ${phase} preserves successful response`, async () => {
    const pending = deferred();
    transport = async () => {
      if (phase === 'headers') return pending.promise;
      const response = json({ ok: true });
      response.text = () => pending.promise;
      return response;
    };
    const result = api('/accounts');
    await flush();
    useAuthStore.getState().setTokens('rotated-access', 'rotated-refresh');
    pending.resolve(phase === 'headers' ? json({ ok: true }) : '{"ok":true}');
    assert.deepEqual(await result, { ok: true });
    assert.equal(calls.length, 1);
  });
}

test('late 401 reuses sibling credentials without rotating twice', async () => {
  const pending = deferred();
  transport = async ({ url, init }) => {
    const old = new Headers(init.headers).get('Authorization') === 'Bearer test-access';
    if (url.endsWith('/delayed') && old) return pending.promise;
    if (url.endsWith('/auth/refresh')) return json({ access_token: 'rotated-access', refresh_token: 'rotated-refresh' });
    return old ? json({}, 401) : json({ ok: true });
  };
  const result = api('/delayed');
  await flush();
  await api('/accounts');
  pending.resolve(json({}, 401));
  assert.deepEqual(await result, { ok: true });
  assert.equal(calls.filter((c) => c.url.endsWith('/auth/refresh')).length, 1);
});

test('owner-bound and generic requests share the same refresh flight', async () => {
  const pending = deferred();
  transport = async ({ url, init }) => {
    if (url.endsWith('/auth/refresh')) return pending.promise;
    return new Headers(init.headers).get('Authorization') === 'Bearer test-access'
      ? json({}, 401) : json({ ok: true });
  };
  const requests = [api('/accounts', ownerInit()), api('/cards')];
  const result = Promise.allSettled(requests);
  await flush();
  const refreshes = calls.filter((c) => c.url.endsWith('/auth/refresh')).length;
  pending.resolve(json({ access_token: 'rotated-access', refresh_token: 'rotated-refresh' }));
  const settled = await result;
  assert.equal(refreshes, 1);
  assert.ok(settled.every((r) => r.status === 'fulfilled'));
});

test('rotation while health waits uses new credentials for the first POST', async () => {
  const health = deferred();
  transport = async ({ url }) => url.endsWith('/healthz') ? health.promise : json({ queued: 1 }, 202);
  const result = api('/sync/all', ownerInit());
  await flush();
  useAuthStore.getState().setTokens('rotated-access', 'rotated-refresh');
  health.resolve(json({ status: 'ok' }));
  assert.deepEqual(await result, { queued: 1 });
  assert.equal(calls.length, 2);
  assert.equal(new Headers(calls[1].init.headers).get('Authorization'), 'Bearer rotated-access');
});

for (const transition of ['relogin', 'logout-login', 'server-roundtrip', 'key-roundtrip']) {
  for (const phase of ['headers', 'body']) {
    test(`${transition} invalidates an old ${phase} continuation even with identical credentials`, async () => {
      const pending = deferred();
      transport = async () => {
        if (phase === 'headers') return pending.promise;
        const response = json({ ok: true }); response.text = () => pending.promise; return response;
      };
      const result = assert.rejects(api('/accounts'), (e) => e.status === 409);
      await flush();
      const s = useAuthStore.getState();
      if (transition === 'logout-login') s.logout();
      if (transition === 'relogin' || transition === 'logout-login') s.setAuth(s.token, s.email, s.refreshToken);
      if (transition === 'server-roundtrip') { s.setServerUrl('https://other.invalid'); s.setServerUrl(s.serverUrl); }
      if (transition === 'key-roundtrip') { s.setApiKey('other-test-key'); s.setApiKey(s.apiKey); }
      pending.resolve(phase === 'headers' ? json({ ok: true }) : '{"ok":true}');
      await result;
      assert.equal(calls.length, 1);
      assert.equal(useAuthStore.getState().token, s.token);
    });
  }
}

for (const status of [200, 401]) {
  test(`late refresh HTTP ${status} cannot overwrite or log out newer credentials`, async () => {
    const refresh = deferred();
    transport = async ({ url, init }) => url.endsWith('/auth/refresh') ? refresh.promise
      : new Headers(init.headers).get('Authorization') === 'Bearer test-access' ? json({}, 401) : json({ ok: true });
    const result = api('/accounts');
    await flush();
    useAuthStore.getState().setTokens('newer-access', 'newer-refresh');
    refresh.resolve(json({ access_token: 'stale-access', refresh_token: 'stale-refresh' }, status));
    assert.deepEqual(await result, { ok: true });
    assert.equal(useAuthStore.getState().token, 'newer-access');
  });
}

test('session replacement while checking saved credentials never opens Face ID', async () => {
  const available = deferred();
  hasSavedCredentials = () => available.promise;
  transport = async () => json({}, 401);
  useAuthStore.getState().setTokens('test-access', null);
  const result = assert.rejects(api('/accounts'), (e) => e.status === 409);
  await flush();
  useAuthStore.getState().setAuth('other-access', 'other@example.invalid', 'other-refresh');
  available.resolve(true);
  await result;
  assert.equal(calls.length, 1);
  assert.equal(useAuthStore.getState().token, 'other-access');
});

for (const branch of ['no-refresh-token', 'after-refresh', 'refresh-failed']) {
  test(`${branch}: sibling rotation in credential-check microtask skips Face ID`, async () => {
    const available = deferred();
    let checks = 0, prompts = 0;
    hasSavedCredentials = () => ++checks === 1 ? available.promise : Promise.resolve(true);
    const state = useAuthStore.getState();
    loadSavedCredentials = async () => {
      prompts++;
      return { serverUrl: state.serverUrl, email: state.email, password: 'synthetic-password' };
    };
    if (branch === 'no-refresh-token') state.setTokens('test-access', null);
    transport = async ({ url, init }) => {
      if (url.endsWith('/auth/refresh')) return json({}, 401);
      if (url.endsWith('/auth/login')) return json({ access_token: 'unnecessary-access', refresh_token: 'unnecessary-refresh' });
      return new Headers(init.headers).get('Authorization') === 'Bearer test-access'
        ? json({}, 401) : json({ ok: true });
    };
    const result = api('/accounts', { _retriedAfterRefresh: branch === 'after-refresh' });
    await flush();
    assert.equal(checks, 1);
    // Queue rotation after the helper's await handler, but before its caller resumes.
    available.promise.then(() => state.setTokens('valid-sibling-access', 'valid-sibling-refresh'));
    available.resolve(true);
    assert.deepEqual(await result, { ok: true });
    assert.equal(prompts, 0);
    assert.equal(useAuthStore.getState().token, 'valid-sibling-access');
    assert.equal(useAuthStore.getState().refreshToken, 'valid-sibling-refresh');
    assert.equal(calls.filter((c) => c.url.endsWith('/auth/login')).length, 0);
    assert.equal(calls.filter((c) => c.url.endsWith('/auth/refresh')).length, branch === 'refresh-failed' ? 1 : 0);
    assert.deepEqual(calls.filter((c) => c.url.endsWith('/accounts'))
      .map((c) => new Headers(c.init.headers).get('Authorization')),
    ['Bearer test-access', 'Bearer valid-sibling-access']);
  });
}

test('skipAuthRetry also rejects an old owner after delayed error-body parsing', async () => {
  const body = deferred();
  transport = async () => { const response = json({}, 401); response.json = () => body.promise; return response; };
  const result = assert.rejects(api('/accounts', { skipAuthRetry: true }), (e) => e.status === 409);
  await flush();
  useAuthStore.getState().setAuth('other-access', 'other@example.invalid', 'other-refresh');
  body.resolve({ detail: 'old-owner-detail' });
  await result;
  assert.equal(calls.length, 1);
});

test('biometric recovery is single-flight and preserves the active lifecycle', async () => {
  const prompt = deferred(), response = deferred();
  let prompts = 0;
  hasSavedCredentials = async () => true;
  loadSavedCredentials = () => { prompts++; return prompt.promise; };
  useAuthStore.getState().setTokens('test-access', null);
  const state = useAuthStore.getState();
  transport = async ({ url, init }) => {
    if (url.endsWith('/auth/login')) return response.promise;
    return new Headers(init.headers).get('Authorization') === 'Bearer test-access'
      ? json({}, 401) : json({ ok: true });
  };
  const result = Promise.all([api('/accounts', ownerInit()), api('/cards')]);
  await flush();
  assert.equal(prompts, 1);
  prompt.resolve({ serverUrl: state.serverUrl, email: state.email, password: 'synthetic-password' });
  await flush();
  assert.equal(calls.filter((c) => c.url.endsWith('/auth/login')).length, 1);
  response.resolve(json({ access_token: 'biometric-access', refresh_token: 'biometric-refresh' }));
  assert.deepEqual(await result, [{ ok: true }, { ok: true }]);
  assert.equal(useAuthStore.getState().sessionEpoch, state.sessionEpoch);
});

for (const phase of ['refresh', 'prompt', 'login-response']) {
  test(`same-owner replacement during ${phase} cannot publish or replay old recovery`, async () => {
    const pending = deferred();
    hasSavedCredentials = async () => true;
    const state = useAuthStore.getState();
    loadSavedCredentials = async () => phase === 'prompt' ? pending.promise
      : { serverUrl: state.serverUrl, email: state.email, password: 'synthetic-password' };
    if (phase !== 'refresh') state.setTokens(state.token, null);
    transport = async ({ url }) => url.endsWith('/auth/refresh') || url.endsWith('/auth/login')
      ? pending.promise : json({}, 401);
    const result = assert.rejects(api('/accounts'), (e) => e.status === 409);
    await flush();
    state.setAuth(state.token, state.email, state.refreshToken);
    pending.resolve(phase === 'prompt'
      ? { serverUrl: state.serverUrl, email: state.email, password: 'synthetic-password' }
      : json({ access_token: 'stale-access', refresh_token: 'stale-refresh' }));
    await result;
    assert.equal(useAuthStore.getState().token, state.token);
    assert.equal(calls.length, phase === 'prompt' ? 1 : 2);
  });
}

test('successive credential rotations cannot cause unbounded retries', async () => {
  let attempts = 0;
  transport = async () => {
    attempts++;
    useAuthStore.getState().setTokens(`rotated-${attempts}`, `refresh-${attempts}`);
    return json({}, 401);
  };
  await assert.rejects(api('/accounts'), (e) => e.status === 401);
  assert.equal(calls.length, 2);
  assert.equal(useAuthStore.getState().token, 'rotated-2');
});
