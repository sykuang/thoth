/**
 * API client wrapper — fetch + JWT injection + 401 auto-logout.
 *
 * Server URL precedence:
 *   1. zustand store serverUrl (user-set, persisted to SecureStore/localStorage)
 *   2. process.env.EXPO_PUBLIC_API_URL (build-time, web-only fallback)
 *   3. 'http://localhost:8000' (final fallback for web dev)
 *
 * Note: BASE_URL is now a getter, not a constant — it re-reads from the store
 * each call. This lets users change server URL at runtime without rebuild.
 * Throws ApiError on non-2xx so TanStack Query can render error states.
 */
import { router } from 'expo-router';

import { SessionPromiseGate, storedCredentialsMatchSession } from '@/lib/authRetryPolicy';
import { loadCredentials, hasCredentials } from '@/lib/credentials';
import { queryClient } from '@/lib/queryClient';
import { useAuthStore } from '@/stores/auth';
import type { LoginResponse } from '@/types/api';

/** Azure scale-to-zero cold starts can exceed 50 seconds. */
const DEFAULT_TIMEOUT_MS = 90_000;

/**
 * Resolve current server URL from zustand store (live read each call).
 * Returns '' if neither store nor env is set — caller should treat as error.
 */
export function getBaseUrl(): string {
  const fromStore = useAuthStore.getState().serverUrl;
  if (fromStore) return fromStore.replace(/\/$/, ''); // strip trailing slash
  const configured = process.env.EXPO_PUBLIC_API_URL;
  if (configured === '__THOTH_SAME_ORIGIN__' && typeof globalThis.location?.origin === 'string') {
    return `${globalThis.location.origin}/api`;
  }
  return configured ?? 'http://localhost:8000';
}

/** @deprecated Use getBaseUrl() instead — this is kept for backward compat only. */
export const BASE_URL: string = getBaseUrl();

export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, body: unknown) {
    super(`API ${status}: ${typeof body === 'string' ? body : JSON.stringify(body)}`);
    this.status = status;
    this.body = body;
    this.name = 'ApiError';
  }
}

// ============================================================
// formatApiError — 把 FastAPI / 任何 thrown error 正規化成繁中人話
// ============================================================
//
// FastAPI 的錯誤 body 有三種 shape:
//   1. {detail: "帳號或密碼錯誤"}                ← HTTPException(status, "msg")
//   2. {detail: [{loc, msg, type, ctx}, ...]}    ← Pydantic 422 validation
//   3. {detail: {arbitrary: "object"}}           ← 自訂格式
//
// 之前 String(detail) 對 shape 2 會吐 "[object Object]"。
// 這 helper 把三種一律轉成可顯示的字串, 並把常見 Pydantic 錯誤碼翻成中文。

const PYDANTIC_TYPE_LABELS: Record<string, string> = {
  string_too_short: '字數太少',
  string_too_long: '字數太多',
  value_error: '格式不正確',
  missing: '必填欄位',
  string_type: '必須是文字',
  int_parsing: '必須是整數',
  email: 'email 格式錯誤',
};

const PYDANTIC_LOC_LABELS: Record<string, string> = {
  email: 'Email',
  password: '密碼',
  body: '',
  bank: '銀行',
  label: '帳號名稱',
};

function translateFieldName(loc: unknown): string {
  if (!Array.isArray(loc)) return '';
  const parts = loc
    .filter((p) => p !== 'body')
    .map((p) => PYDANTIC_LOC_LABELS[String(p)] ?? String(p));
  return parts.join('.');
}

function translatePydanticError(item: {
  type?: string;
  msg?: string;
  loc?: unknown;
  ctx?: Record<string, unknown>;
}): string {
  const field = translateFieldName(item.loc);
  const t = item.type;

  // 常見 case 中文化
  if (t === 'string_too_short' && item.ctx?.min_length) {
    return `${field}至少要 ${item.ctx.min_length} 個字`;
  }
  if (t === 'string_too_long' && item.ctx?.max_length) {
    return `${field}最多 ${item.ctx.max_length} 個字`;
  }
  if (t === 'missing') {
    return `${field}必填`;
  }
  if (t === 'value_error' && field.toLowerCase().includes('email')) {
    return 'Email 格式不正確';
  }

  // Fallback: 拿 msg + 欄位名
  const label = t ? PYDANTIC_TYPE_LABELS[t] : null;
  if (label) return field ? `${field}: ${label}` : label;
  return field ? `${field}: ${item.msg ?? '格式錯誤'}` : (item.msg ?? '格式錯誤');
}

/**
 * 把 thrown error (ApiError / Error / unknown) 變成可直接顯示的繁中字串。
 *
 * 用法:
 *   try { await api(...) }
 *   catch (e) { setError(formatApiError(e)) }
 */
export function formatApiError(err: unknown): string {
  if (err instanceof ApiError) {
    const body = err.body;
    // Shape 1: {detail: "..."}
    if (body && typeof body === 'object' && 'detail' in body) {
      const detail = (body as { detail: unknown }).detail;
      if (typeof detail === 'string') return detail;
      // Shape 2: {detail: [{loc, msg, ...}]} — Pydantic 422
      if (Array.isArray(detail)) {
        const msgs = detail
          .map((d) =>
            d && typeof d === 'object'
              ? translatePydanticError(d as Parameters<typeof translatePydanticError>[0])
              : String(d),
          )
          .filter((s) => s);
        if (msgs.length) return msgs.join('；');
      }
      // Shape 3: 其他 object — JSON dump
      return JSON.stringify(detail);
    }
    // No detail field → 用原始 status code message
    return err.message;
  }
  if (err instanceof Error) {
    if (err.message.includes('fetch') || err.message.includes('Network')) {
      return `連線失敗: ${err.message}。請確認伺服器網址是否正確。`;
    }
    return err.message;
  }
  return '發生未知錯誤';
}

export type ApiInit = Omit<RequestInit, 'body'> & {
  body?: unknown;
  skipAuth?: boolean;
  /** Fail on 401 without refreshing/retrying. Owner-bound callers should use authRetryGuard. */
  skipAuthRetry?: boolean;
  /** Revalidate an owner/session boundary before token rotation and retry. */
  authRetryGuard?: () => void;
  /** Legacy caller label; recovery is always keyed by the auth store session. */
  authRetryKey?: string;
  /** If true, returns void on 204 instead of attempting JSON parse. */
  raw?: boolean;
  /** Request timeout in ms (default 90000). Set to 0 to disable. */
  timeoutMs?: number;
  /** Internal: prevent infinite recursion when a 401 retry itself 401s. */
  _retriedAfterRefresh?: boolean;
  /** Internal (L13): after a biometric silent re-login. Same as above — used to
   *  detect when even Face ID re-login produced a token that 401s, meaning the
   *  stored credentials are no longer valid (password changed, account deleted). */
  _retriedAfterBiometric?: boolean;
  /** Internal: reuse a sibling's newer credentials at most once. */
  _retriedWithLatestToken?: boolean;
};

// ============================================================
// L9 (2026-06-21) — refresh token queue
// ============================================================
//
// 401 來時所有併發 request 必須共用同一個 refresh promise，否則 N 個 request
// 同時 401 會 N 次打 /auth/refresh，但只有第一個能拿到新 token（rotation
// chain 設計：舊 refresh 用一次就 revoke），其餘全部被當 reuse 攻擊 → revoke
// 整個 family → user 全部 device 強制重登。經典 race。
//
// 解法：以 owner/session epoch 為 key 的 promise gate。同 session 的 401 共用
// 一次 rotation；切帳號後另開新 flight，避免加入舊 owner 的失敗結果。
//
// Refresh 失敗（401 / 410 / network error）→ 一律當 session 已死：
//   1. clear queryClient cache (cross-user 防漏)
//   2. logout() 清掉 access + refresh token
//   3. router.replace('/login')

const refreshGate = new SessionPromiseGate<string>();

function currentAuthSessionKey(): string {
  const { serverUrl, email, apiKey, sessionEpoch } = useAuthStore.getState();
  return JSON.stringify([serverUrl, email, apiKey, sessionEpoch]);
}

function currentAuthCredentialsKey(): string {
  const { token, refreshToken } = useAuthStore.getState();
  return JSON.stringify([token, refreshToken]);
}

/** A recovery may rotate credentials, but never replace a newer login or token pair. */
function captureAuthRecovery(authRetryGuard?: () => void): () => string | null {
  const sessionKey = currentAuthSessionKey();
  const credentialsKey = currentAuthCredentialsKey();
  return () => {
    if (currentAuthSessionKey() !== sessionKey) {
      throw new ApiError(409, { detail: 'auth session changed during recovery' });
    }
    authRetryGuard?.();
    return currentAuthCredentialsKey() !== credentialsKey ? useAuthStore.getState().token : null;
  };
}

/** 並發 401 的單例 refresh：只讓同一 owner/session epoch 共用 promise。 */
async function getOrStartRefresh(
  authRetryGuard?: () => void,
): Promise<string> {
  authRetryGuard?.();
  const gateKey = currentAuthSessionKey();
  return refreshGate.getOrStart(gateKey, async () => {
    authRetryGuard?.();
    const newerToken = captureAuthRecovery(authRetryGuard);
    const store = useAuthStore.getState();
    const refresh = store.refreshToken;
    if (!refresh) {
      throw new ApiError(401, { detail: 'no refresh token' });
    }
    const url = `${getBaseUrl()}/auth/refresh`;
    const headers: Record<string, string> = {
      Accept: 'application/json',
      'Content-Type': 'application/json',
    };
    if (store.apiKey) headers['X-API-Key'] = store.apiKey;

    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), DEFAULT_TIMEOUT_MS);
    let resp: Response;
    try {
      resp = await fetch(url, {
        method: 'POST',
        headers,
        body: JSON.stringify({ refresh_token: refresh }),
        signal: ctrl.signal,
      });
    } finally {
      clearTimeout(timer);
    }

    const recoveredBeforeBody = newerToken();
    if (recoveredBeforeBody) return recoveredBeforeBody;
    if (!resp.ok) {
      throw new ApiError(resp.status, await safeReadJson(resp));
    }
    const data = (await resp.json()) as {
      access_token: string;
      refresh_token: string;
    };
    if (!data.access_token || !data.refresh_token) {
      throw new ApiError(500, { detail: 'malformed refresh response' });
    }
    const recovered = newerToken();
    if (recovered) return recovered;
    authRetryGuard?.();
    useAuthStore.getState().setTokens(data.access_token, data.refresh_token);
    return data.access_token;
  });
}

async function safeReadJson(resp: Response): Promise<unknown> {
  try {
    return await resp.json();
  } catch {
    return null;
  }
}

function hardLogout(): void {
  // Phase C-fe (2026-06-17): cross-user cache leak fix.
  queryClient.clear();
  useAuthStore.getState().logout();
  try {
    router.replace('/login');
  } catch {
    if (typeof window !== 'undefined') {
      window.location.href = '/login';
    }
  }
}

// ============================================================
// L13 (2026-06-22) — biometric silent re-login (C 方案)
// ============================================================
//
// Refresh token chain 一旦死透 (30+ 天沒開 / server revoke / rotation race),
// 預設行為是 hardLogout() 把使用者踢回 /login。對 daily user 來說很痛。
//
// 解法：在死透 → hardLogout 之間，多一道保險:
//   1. 看 keychain 有沒有存過 email + password (hasCredentials)
//   2. 有的話 prompt Face ID 拿出來 (loadCredentials — OS-level 生物辨識 gate)
//   3. 用拿到的 password silent re-login → setTokens → 拿到全新 access+refresh
//   4. 整段成功 = 使用者完全無感 (只看到 Face ID 一閃)
//   5. 任何一步失敗 = hardLogout fallback (跟以前一樣)
//
// Face ID 也走同一套 session-keyed gate，多個 401 不會重複 prompt，
// 不同 owner 則絕不共用舊 session 的登入結果。

const biometricReLoginGate = new SessionPromiseGate<string>();

async function getOrStartBiometricReLogin(
  authRetryGuard?: () => void,
): Promise<string> {
  authRetryGuard?.();
  const gateKey = currentAuthSessionKey();
  return biometricReLoginGate.getOrStart(gateKey, async () => {
    authRetryGuard?.();
    const newerToken = captureAuthRecovery(authRetryGuard);
    const active = useAuthStore.getState();
    const available = await hasCredentials();
    const recoveredBeforePrompt = newerToken();
    if (recoveredBeforePrompt) return recoveredBeforePrompt;
    if (!available) {
      throw new ApiError(401, { detail: 'no saved credentials' });
    }
    const creds = await loadCredentials('使用 Face ID 重新登入 Thoth');
    const recoveredAfterPrompt = newerToken();
    if (recoveredAfterPrompt) return recoveredAfterPrompt;
    if (!creds) {
      throw new ApiError(401, { detail: 'biometric cancelled' });
    }
    if (!storedCredentialsMatchSession(creds, {
      serverUrl: active.serverUrl,
      email: active.email ?? '',
    })) {
      throw new ApiError(409, { detail: 'saved credentials do not match active session' });
    }
    const recoveredBeforeLogin = newerToken();
    if (recoveredBeforeLogin) return recoveredBeforeLogin;
    authRetryGuard?.();
    const form = new URLSearchParams();
    form.append('username', creds.email);
    form.append('password', creds.password);
    const url = `${active.serverUrl.replace(/\/+$/, '')}/auth/login`;
    const headers: Record<string, string> = {
      Accept: 'application/json',
      'Content-Type': 'application/x-www-form-urlencoded',
    };
    if (active.apiKey) headers['X-API-Key'] = active.apiKey;

    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), DEFAULT_TIMEOUT_MS);
    let resp: Response;
    try {
      resp = await fetch(url, {
        method: 'POST',
        headers,
        body: form,
        signal: ctrl.signal,
      });
    } finally {
      clearTimeout(timer);
    }
    const recoveredBeforeBody = newerToken();
    if (recoveredBeforeBody) return recoveredBeforeBody;
    if (!resp.ok) {
      throw new ApiError(resp.status, await safeReadJson(resp));
    }
    const data = (await resp.json()) as LoginResponse;
    if (!data.access_token) {
      throw new ApiError(500, { detail: 'malformed re-login response' });
    }
    const recovered = newerToken();
    if (recovered) return recovered;
    authRetryGuard?.();
    useAuthStore
      .getState()
      .setTokens(data.access_token, data.refresh_token ?? null);
    return data.access_token;
  });
}

/** Only the anonymous probe is retryable; never wrap the sync mutation in a retry. */
async function warmUpSync(baseUrl: string, signal: AbortSignal | null | undefined, assertActive: () => void): Promise<void> {
  const aborted = () => signal?.reason ?? new DOMException('Aborted', 'AbortError');
  if (signal?.aborted) throw aborted();
  const controller = new AbortController();
  const deadline = Date.now() + 120_000;
  const timeoutError = new ApiError(0, { detail: '伺服器暖機超過 120 秒，未送出同步' });
  let interrupt!: (reason: unknown) => void;
  const interrupted = new Promise<never>((_, reject) => { interrupt = reject; });
  const onAbort = () => {
    controller.abort();
    interrupt(signal?.aborted ? aborted() : timeoutError);
  };
  signal?.addEventListener('abort', onAbort, { once: true });
  const timer = setTimeout(onAbort, 120_000);
  let retryTimer: ReturnType<typeof setTimeout> | undefined;
  try {
    for (;;) {
      assertActive();
      if (signal?.aborted) throw aborted();
      if (controller.signal.aborted || Date.now() >= deadline) throw timeoutError;
      try {
        const health = await Promise.race([fetch(`${baseUrl}/healthz`, {
          method: 'GET', credentials: 'omit', cache: 'no-store', redirect: 'manual',
          headers: { Accept: 'application/json' }, signal: controller.signal,
        }), interrupted]);
        if (![502, 503, 504].includes(health.status)) {
          if (!health.ok) throw new ApiError(health.status, { detail: '伺服器尚未就緒，未送出同步' });
          const data: unknown = await Promise.race([health.json(), interrupted]);
          if (signal?.aborted) throw aborted();
          if (Date.now() >= deadline) throw timeoutError;
          if (!data || typeof data !== 'object' || !('status' in data) || data.status !== 'ok') {
            throw new ApiError(502, { detail: '伺服器健康檢查格式錯誤，未送出同步' });
          }
          return;
        }
      } catch (error) {
        // Expo native FetchError extends Error, unlike browser fetch's TypeError.
        const transportError = error instanceof TypeError ||
          (error instanceof Error && error.message.startsWith('fetch failed: '));
        if (controller.signal.aborted || !transportError) throw error;
      }
      await Promise.race([
        new Promise((resolve) => { retryTimer = setTimeout(resolve, 2000); }),
        interrupted,
      ]);
    }
  } finally {
    clearTimeout(timer);
    clearTimeout(retryTimer);
    signal?.removeEventListener('abort', onAbort);
    controller.abort();
  }
}

export async function api<T = unknown>(path: string, init: ApiInit = {}): Promise<T> {
  const requestAuthSessionKey = init.skipAuth ? null : currentAuthSessionKey();
  const assertRequestAuthSession = () => {
    if (requestAuthSessionKey !== null && currentAuthSessionKey() !== requestAuthSessionKey) {
      throw new ApiError(409, { detail: 'auth session changed during request' });
    }
    init.authRetryGuard?.();
  };
  const baseUrl = getBaseUrl();
  const isSync = init.method?.toUpperCase() === 'POST' && /^\/sync\/(all|account\/[^/?#]+)$/.test(path);
  const assertSyncReady = () => {
    if (init.signal?.aborted) throw init.signal.reason ?? new DOMException('Aborted', 'AbortError');
    if (getBaseUrl() !== baseUrl) throw new ApiError(409, { detail: 'server changed during sync warmup' });
    assertRequestAuthSession();
  };
  if (isSync) {
    assertSyncReady();
    await warmUpSync(baseUrl, init.signal, assertSyncReady);
    assertSyncReady();
  }
  const requestCredentialsKey = currentAuthCredentialsKey();
  const headers: Record<string, string> = {
    Accept: 'application/json',
    ...(init.headers as Record<string, string> | undefined),
  };
  const body = init.body;
  // For form-encoded callers (login OAuth2), respect their content-type.
  if (body !== undefined && !(typeof body === 'string' && headers['Content-Type'])) {
    headers['Content-Type'] = headers['Content-Type'] ?? 'application/json';
  }
  if (!init.skipAuth) {
    const token = useAuthStore.getState().token;
    if (token) headers['Authorization'] = `Bearer ${token}`;
  }
  // L8.5 — server-level X-API-Key (always sent if user has set one;
  // backend ignores when SERVER_API_KEY env is not set, so harmless).
  const apiKey = useAuthStore.getState().apiKey;
  if (apiKey) headers['X-API-Key'] = apiKey;

  const reqBody: BodyInit | undefined =
    body === undefined
      ? undefined
      : typeof body === 'string' || body instanceof URLSearchParams
        ? (body as BodyInit)
        : JSON.stringify(body);

  // Default timeout covers Azure scale-to-zero cold starts; caller signal still wins.
  const timeoutMs = init.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const controller = timeoutMs > 0 ? new AbortController() : null;
  const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
  // 如 caller 傳 signal，跨接到臣妾 controller 上 (雙重中斷守護)
  if (controller && init.signal) {
    if (init.signal.aborted) controller.abort();
    else init.signal.addEventListener('abort', () => controller.abort(), { once: true });
  }
  const signalToUse = controller ? controller.signal : init.signal;

  let res: Response;
  try {
    if (isSync) assertSyncReady();
    res = await fetch(`${baseUrl}${path}`, {
      ...init,
      headers,
      body: reqBody,
      signal: signalToUse,
    });
  } catch (e) {
    if (timer) clearTimeout(timer);
    if (e instanceof DOMException && e.name === 'AbortError') {
      throw new ApiError(0, { detail: `請求超過 ${timeoutMs}ms 未回應` });
    }
    throw e;
  }
  if (timer) clearTimeout(timer);
  if (!init.skipAuth) assertRequestAuthSession();

  // L9: 401 → 嘗試 refresh 一次 → retry 原 request；refresh 失敗才 hard logout。
  // L13: refresh 失敗 → 試 biometric silent re-login (有存 creds 的話) → retry。
  // 例外：(a) skipAuth=true 的 request (login/register/refresh 自己) 不 retry。
  //       (b) 已經 retry 過一次的 request 直接 logout 防無限迴圈。
  if (res.status === 401 && init.skipAuthRetry) {
    const body = await safeReadJson(res);
    if (!init.skipAuth) assertRequestAuthSession();
    throw new ApiError(401, body);
  }
  if (res.status === 401 && !init.skipAuth) {
    const reuseNewerCredentials = (): Promise<T> | null => {
      assertRequestAuthSession();
      if (currentAuthCredentialsKey() === requestCredentialsKey || !useAuthStore.getState().token) return null;
      if (init._retriedWithLatestToken) {
        throw new ApiError(401, { detail: 'credentials changed during retry' });
      }
      return api<T>(path, { ...init, _retriedWithLatestToken: true });
    };
    const failUnauthorized = (detail: string): Promise<T> => {
      const retry = reuseNewerCredentials();
      if (retry) return retry;
      hardLogout();
      throw new ApiError(401, { detail });
    };
    const retry = reuseNewerCredentials();
    if (retry) return retry;
    const hasCredentialsForActiveSession = async () => {
      const available = await hasCredentials();
      assertRequestAuthSession();
      return available && currentAuthCredentialsKey() === requestCredentialsKey;
    };
    if (init._retriedAfterBiometric) {
      return failUnauthorized('unauthorized after biometric re-login');
    }
    if (init._retriedAfterRefresh) {
      if (await hasCredentialsForActiveSession()) {
        const retry = reuseNewerCredentials();
        if (retry) return retry;
        try {
          await getOrStartBiometricReLogin(assertRequestAuthSession);
          assertRequestAuthSession();
          return api<T>(path, { ...init, _retriedAfterBiometric: true });
        } catch {
          assertRequestAuthSession();
        }
      }
      return failUnauthorized('unauthorized after refresh retry');
    }
    if (!useAuthStore.getState().refreshToken) {
      if (await hasCredentialsForActiveSession()) {
        const retry = reuseNewerCredentials();
        if (retry) return retry;
        try {
          await getOrStartBiometricReLogin(assertRequestAuthSession);
          assertRequestAuthSession();
          return api<T>(path, { ...init, _retriedAfterBiometric: true });
        } catch {
          assertRequestAuthSession();
        }
      }
      return failUnauthorized('unauthorized');
    }
    try {
      await getOrStartRefresh(assertRequestAuthSession);
      assertRequestAuthSession();
    } catch {
      // A failed old refresh must not prompt or log out newer credentials.
      const retry = reuseNewerCredentials();
      if (retry) return retry;
      if (await hasCredentialsForActiveSession()) {
        const retry = reuseNewerCredentials();
        if (retry) return retry;
        try {
          await getOrStartBiometricReLogin(assertRequestAuthSession);
          assertRequestAuthSession();
          return api<T>(path, { ...init, _retriedAfterBiometric: true });
        } catch {
          assertRequestAuthSession();
        }
      }
      return failUnauthorized('refresh failed');
    }
    assertRequestAuthSession();
    return api<T>(path, { ...init, _retriedAfterRefresh: true });
  }

  // 204 No Content (PUT/DELETE credentials)
  if (res.status === 204) {
    return undefined as T;
  }

  const text = await res.text();
  if (!init.skipAuth) assertRequestAuthSession();
  let parsed: unknown = undefined;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = text;
    }
  }

  if (!res.ok) {
    throw new ApiError(res.status, parsed);
  }
  return parsed as T;
}
