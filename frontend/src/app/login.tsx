/**
 * Login / register 頁 (L8.5: 加入 server URL + X-API-Key 進階設定).
 *
 * 視覺:
 *   - 桌機 (md+): split-screen 左 brand 漸層, 右 form card
 *   - 手機 (xs): 單欄置中, brand 縮成 logo header
 *   - 紫色 brand 主色 + dark mode 自動跟隨
 *
 * 進階設定區 (collapsible):
 *   - 伺服器網址 (web + iOS 都顯示, 之前只 iOS, (2026-06-13) 改為全平台顯示)
 *   - X-API-Key (對應 backend SERVER_API_KEY env, 沒設 backend env 時可留空)
 *   - 第一次或 serverUrl/apiKey 都空 → 預設展開; 兩個都有值 → 預設摺疊
 *
 * Backend contract (不變):
 *   POST /auth/register  (JSON {email, password})         → 201 {token, ...}
 *   POST /auth/login     (form: username, password)        → 200 {access_token, ...}
 */
import { useRouter } from 'expo-router';
import { ChevronDown, ChevronUp, Eye, EyeOff, Lock, RefreshCw, ScanFace, Smartphone, Users } from 'lucide-react-native';
import { useEffect, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  Platform,
  Pressable,
  Switch,
  Text,
  TextInput,
  View,
} from 'react-native';

import { KeyboardAwareScrollView } from '@/components/KeyboardAwareScrollView';
import { useBreakpoint } from '@/hooks/useBreakpoint';
import { api, ApiError, formatApiError, getBaseUrl } from '@/lib/api';
import { biometricAvailable } from '@/lib/biometric';
import { hasCredentials, saveCredentials } from '@/lib/credentials';
import { queryClient } from '@/lib/queryClient';
import { useAuthStore } from '@/stores/auth';
import type { LoginResponse, RegisterResponse } from '@/types/api';

type Mode = 'login' | 'register';

/** Allow http only for loopback/private LAN (RFC 1918); require https otherwise. */
function validateServerUrl(raw: string): { ok: true; normalized: string } | { ok: false; error: string } {
  const trimmed = raw.trim().replace(/\/$/, '');
  if (!trimmed) return { ok: false, error: '請輸入伺服器網址' };
  let u: URL;
  try {
    u = new URL(trimmed);
  } catch {
    return { ok: false, error: '網址格式錯誤 (需以 http:// 或 https:// 開頭)' };
  }
  if (!['http:', 'https:'].includes(u.protocol)) {
    return { ok: false, error: '網址必須使用 http:// 或 https://' };
  }
  if (u.protocol === 'http:') {
    const h = u.hostname;
    const isLoopback = h === 'localhost' || h === '127.0.0.1' || h === '::1';
    const isPrivate =
      /^10\./.test(h) ||
      /^192\.168\./.test(h) ||
      /^172\.(1[6-9]|2[0-9]|3[01])\./.test(h) ||
      /\.local$/.test(h);
    if (!isLoopback && !isPrivate) {
      return {
        ok: false,
        error: 'http:// 只允許 localhost / 10.x / 192.168.x / 172.16-31.x / *.local',
      };
    }
  }
  return { ok: true, normalized: trimmed };
}

export default function LoginScreen() {
  const router = useRouter();
  const bp = useBreakpoint();
  const setAuth = useAuthStore((s) => s.setAuth);
  const storedServerUrl = useAuthStore((s) => s.serverUrl);
  const setServerUrl = useAuthStore((s) => s.setServerUrl);
  const storedApiKey = useAuthStore((s) => s.apiKey);
  const setApiKey = useAuthStore((s) => s.setApiKey);
  const [mode, setMode] = useState<Mode>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [serverInput, setServerInput] = useState(storedServerUrl);
  const [apiKeyInput, setApiKeyInput] = useState(storedApiKey);
  const [showApiKey, setShowApiKey] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [pingStatus, setPingStatus] = useState<{ ok: boolean; text: string } | null>(null);
  const [pinging, setPinging] = useState(false);
  // 進階設定預設展開的判斷:
  //   - serverUrl 空 → 一定展開 (第一次使用必填)
  //   - serverUrl 有值但 apiKey 空 → 摺疊 (常見情境: backend 沒設 SERVER_API_KEY)
  //   - 兩者都有 → 摺疊
  const [advancedOpen, setAdvancedOpen] = useState(!storedServerUrl);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // L13 (2026-06-22) — Face ID 快速登入記住帳密
  // 預設「已勾」：如果裝置有生物辨識硬體且使用者還沒存過帳密。
  // 已存過帳密的 user 預設不勾,免得登入流程一直跳第二次 Face ID 蓋掉本來那筆。
  const [rememberCreds, setRememberCreds] = useState(false);
  const [bioReady, setBioReady] = useState(false);
  useEffect(() => {
    if (Platform.OS === 'web') return;
    void (async () => {
      const ok = await biometricAvailable();
      const had = await hasCredentials();
      setBioReady(ok);
      setRememberCreds(ok && !had);
    })();
  }, []);

  async function testConnection() {
    setPingStatus(null);
    const v = validateServerUrl(serverInput);
    if (!v.ok) {
      setPingStatus({ ok: false, text: v.error });
      return;
    }
    setPinging(true);
    try {
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort(), 5000);
      // healthz 是 GET, 不需 auth, 沒帳號也能打
      const resp = await fetch(`${v.normalized}/healthz`, {
        method: 'GET',
        signal: ctrl.signal,
      });
      clearTimeout(t);
      if (resp.ok) {
        setPingStatus({ ok: true, text: `✓ 連線成功 (HTTP ${resp.status})` });
      } else {
        setPingStatus({ ok: false, text: `✗ HTTP ${resp.status} — server 有回應但不是 200` });
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setPingStatus({
        ok: false,
        text: `✗ 連不到 server: ${msg}\n→ 確認手機跟電腦同 WiFi、URL 正確、後端有跑`,
      });
    } finally {
      setPinging(false);
    }
  }

  async function submit() {
    setError(null);
    if (!email && !password) {
      setError('請輸入 email 與密碼');
      return;
    }
    const v = validateServerUrl(serverInput);
    if (!v.ok) {
      setError(v.error);
      setAdvancedOpen(true);
      return;
    }
    if (v.normalized !== storedServerUrl) {
      setServerUrl(v.normalized);
    }
    // X-API-Key 只 trim 不 validate (backend 沒設 env 就空字串通行)
    const trimmedKey = apiKeyInput.trim();
    if (trimmedKey !== storedApiKey) {
      setApiKey(trimmedKey);
    }
    if (!email || !password) {
      setError('請輸入 email 與密碼');
      return;
    }
    if (password.length < 6) {
      setError('密碼至少需要 6 個字元');
      return;
    }
    setLoading(true);
    try {
      let token: string;
      let refreshToken: string | null = null;
      if (mode === 'register') {
        const r = await api<RegisterResponse>('/auth/register', {
          method: 'POST',
          body: { email, password },
          skipAuth: true,
        });
        token = r.token;
        // L9: backend 0.3.10+ register 也回 refresh_token；舊 backend 沒給就 null
        refreshToken = r.refresh_token ?? null;
      } else {
        const form = new URLSearchParams();
        form.append('username', email);
        form.append('password', password);
        const r = await api<LoginResponse>('/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: form,
          skipAuth: true,
        });
        token = r.access_token;
        // L9: backend 0.3.10+ login 回 refresh_token；舊 backend 沒給就 null
        refreshToken = r.refresh_token ?? null;
      }
      // Phase C-fe (2026-06-17): cross-user cache leak fix.
      // 新 user 登入前清掉前一個 user 的 cache (transactions/cards/portfolio/me/...),
      // 不然新 user 第一次進 dashboard 在 staleTime 30s 內會看到舊 user 資料。
      queryClient.clear();
      setAuth(token, email, refreshToken);

      // L13: 使用者勾「Face ID 快速登入」→ 把帳密寫進 Keychain (會 prompt Face ID 一次)。
      //      失敗 (使用者取消 / 沒生物辨識) 不擋 login 流程,只是不存。
      if (rememberCreds && Platform.OS !== 'web') {
        try {
          await saveCredentials(v.normalized, email, password);
        } catch (saveErr) {
          // 不擋登入,只通知使用者沒存成功
          Alert.alert(
            'Face ID 設定失敗',
            '已成功登入,但無法儲存帳密 (你可能取消了 Face ID 提示)。\n之後可在「設定 → 生物辨識」重試。',
          );
          if (typeof console !== 'undefined') {
            console.warn('[login] saveCredentials failed:', saveErr);
          }
        }
      }
      router.replace('/(tabs)/dashboard');
    } catch (e) {
      setError(formatApiError(e));
      // X-API-Key 錯 → 自動展開讓用戶調整
      if (e instanceof ApiError && e.status === 401) {
        const detail = typeof e.body === 'object' && e.body && 'detail' in e.body
          ? String((e.body as { detail: unknown }).detail)
          : '';
        if (detail.includes('X-API-Key')) {
          setAdvancedOpen(true);
        }
      }
    } finally {
      setLoading(false);
    }
  }

  const isWide = bp.isMd;

  return (
    <View className="flex-1 bg-ink-50 dark:bg-ink-950">
      <KeyboardAwareScrollView
        className="flex-1"
        contentContainerStyle={{ flexGrow: 1 }}
      >
        <View className="flex-1 flex-row">
      {/* Brand 側欄: 桌機 split-left, 手機隱藏 */}
      {isWide && (
        <View className="flex-1 bg-brand-700 dark:bg-brand-900 p-12 justify-center">
          <View className="max-w-md">
            <Text className="text-white text-display mb-3">Thoth</Text>
            <Text className="text-brand-100 text-h2 mb-8 font-normal">
              自架的台灣銀行與資產管理工具
            </Text>
            <View className="gap-4">
              <BrandFeature
                icon={<Users size={20} color="#fff" strokeWidth={2.4} />}
                label="多帳號管理"
                desc="同一家銀行可管理多組登入資料（個人／家庭／公司）"
              />
              <BrandFeature
                icon={<Lock size={20} color="#fff" strokeWidth={2.4} />}
                label="加密儲存"
                desc="敏感資料加密保存，不在畫面顯示完整內容"
              />
              <BrandFeature
                icon={<RefreshCw size={20} color="#fff" strokeWidth={2.4} />}
                label="一鍵同步"
                desc="自動更新銀行資料，隨時查看最新帳務"
              />
              <BrandFeature
                icon={<Smartphone size={20} color="#fff" strokeWidth={2.4} />}
                label="跨平台"
                desc="桌機與行動裝置共用同一套資料"
              />
            </View>
          </View>
        </View>
      )}

      {/* Form 區: 手機全寬 / 桌機右半邊 */}
      <View className={`flex-1 ${isWide ? '' : 'items-center justify-center'} px-6`}>
        <View className={isWide ? 'flex-1 justify-center max-w-md w-full mx-auto py-12' : 'w-full max-w-md'}>
          {/* 手機: brand header */}
          {!isWide && (
            <View className="items-center mb-8 mt-12">
              <View className="bg-brand-600 dark:bg-brand-700 rounded-2xl px-5 py-4 mb-3 shadow-brand">
                <Text className="text-white text-display">Thoth</Text>
              </View>
              <Text className="text-ink-500 dark:text-ink-400 text-body text-center">
                自架的台灣銀行與資產管理工具
              </Text>
            </View>
          )}

          <View className="bg-white dark:bg-ink-900 rounded-2xl p-7 shadow-pop">
            {/* 標題進到卡片內, 跟 form 對齊 */}
            <Text className="text-ink-900 dark:text-ink-50 text-h1 mb-1">
              {mode === 'login' ? '歡迎回來' : '建立新帳號'}
            </Text>
            <Text className="text-ink-500 dark:text-ink-400 text-small mb-5">
              {mode === 'login' ? '請登入帳號' : '建立新帳號'}
            </Text>

            {/* L8.5: 進階設定 (collapsible) — 伺服器網址 + X-API-Key */}
            <View className="mb-4 border border-ink-200 dark:border-ink-700 rounded-xl overflow-hidden">
              <Pressable
                onPress={() => setAdvancedOpen((v) => !v)}
                className="flex-row items-center justify-between px-3.5 py-2.5 bg-ink-50 dark:bg-ink-800"
                testID="advanced-toggle"
              >
                <View className="flex-row items-center gap-2">
                  <Text className="text-ink-700 dark:text-ink-200 text-small font-semibold">
                    進階設定
                  </Text>
                  {!advancedOpen && (
                    <Text className="text-ink-400 dark:text-ink-500 text-micro" numberOfLines={1}>
                      {storedServerUrl
                        ? `${storedServerUrl}${storedApiKey ? ' · 🔑' : ''}`
                        : '(未設定)'}
                    </Text>
                  )}
                </View>
                {advancedOpen ? (
                  <ChevronUp size={18} color="#64748b" strokeWidth={2.2} />
                ) : (
                  <ChevronDown size={18} color="#64748b" strokeWidth={2.2} />
                )}
              </Pressable>

              {advancedOpen && (
                <View className="px-3.5 pt-3 pb-1 bg-white dark:bg-ink-900">
                  <Field label="伺服器網址" hint="(改了會自動加密存進本機)">
                    <TextInput
                      className="border border-ink-200 dark:border-ink-700 rounded-xl px-3.5 py-2.5 text-body bg-white dark:bg-ink-800 text-ink-900 dark:text-ink-50"
                      value={serverInput}
                      onChangeText={setServerInput}
                      placeholder="http://192.168.1.50:8000"
                      placeholderTextColor="#94a3b8"
                      autoCapitalize="none"
                      autoCorrect={false}
                      keyboardType="url"
                      testID="server-url-input"
                      editable={!loading}
                    />
                  </Field>

                  <Field
                    label="X-API-Key"
                    hint="(對應後端 SERVER_API_KEY env;後端未設定時可留空)"
                  >
                    <View className="flex-row items-center gap-2">
                      <TextInput
                        className="flex-1 border border-ink-200 dark:border-ink-700 rounded-xl px-3.5 py-2.5 text-body bg-white dark:bg-ink-800 text-ink-900 dark:text-ink-50"
                        value={apiKeyInput}
                        onChangeText={setApiKeyInput}
                        placeholder="(選填 — backend 沒設就空白)"
                        placeholderTextColor="#94a3b8"
                        autoCapitalize="none"
                        autoCorrect={false}
                        secureTextEntry={!showApiKey}
                        testID="api-key-input"
                        editable={!loading}
                      />
                      <Pressable
                        onPress={() => setShowApiKey((v) => !v)}
                        className="p-2.5 rounded-xl border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-800"
                        testID="api-key-toggle-visibility"
                      >
                        {showApiKey ? (
                          <EyeOff size={18} color="#64748b" strokeWidth={2.2} />
                        ) : (
                          <Eye size={18} color="#64748b" strokeWidth={2.2} />
                        )}
                      </Pressable>
                    </View>
                  </Field>

                  {Platform.OS !== 'web' && (
                    <Text className="text-ink-400 dark:text-ink-500 text-micro mb-3 -mt-1">
                      🔒 兩者都會加密存進 iOS Keychain (AFTER_FIRST_UNLOCK)
                    </Text>
                  )}

                  {/* 測試連線按鈕 + 結果 */}
                  <Pressable
                    onPress={testConnection}
                    disabled={pinging}
                    className={`bg-ink-100 dark:bg-ink-800 active:bg-ink-200 dark:active:bg-ink-700 rounded-xl py-2.5 items-center mb-3 border border-ink-200 dark:border-ink-700 ${pinging ? 'opacity-50' : ''}`}
                    testID="test-connection-btn"
                  >
                    {pinging ? (
                      <ActivityIndicator color="#64748b" />
                    ) : (
                      <Text className="text-ink-700 dark:text-ink-200 text-small font-semibold">
                        🔌 測試連線
                      </Text>
                    )}
                  </Pressable>
                  {pingStatus && (
                    <View
                      className={`rounded-lg px-3 py-2 mb-3 border ${pingStatus.ok ? 'bg-emerald-50 dark:bg-emerald-950 border-emerald-200 dark:border-emerald-900' : 'bg-red-50 dark:bg-red-950 border-red-200 dark:border-red-900'}`}
                    >
                      <Text
                        className={`text-micro ${pingStatus.ok ? 'text-emerald-700 dark:text-emerald-300' : 'text-red-700 dark:text-red-300'}`}
                      >
                        {pingStatus.text}
                      </Text>
                    </View>
                  )}
                </View>
              )}
            </View>

            <Field label="EMAIL">
              <TextInput
                className="border border-ink-200 dark:border-ink-700 rounded-xl px-3.5 py-2.5 text-body bg-white dark:bg-ink-800 text-ink-900 dark:text-ink-50"
                value={email}
                onChangeText={setEmail}
                placeholder="you@example.com"
                placeholderTextColor="#94a3b8"
                autoCapitalize="none"
                autoComplete="email"
                keyboardType="email-address"
                testID="email-input"
                editable={!loading}
              />
            </Field>

            <Field label="密碼">
              <View className="flex-row items-center gap-2">
                <TextInput
                  className="flex-1 border border-ink-200 dark:border-ink-700 rounded-xl px-3.5 py-2.5 text-body bg-white dark:bg-ink-800 text-ink-900 dark:text-ink-50"
                  value={password}
                  onChangeText={setPassword}
                  placeholder="(至少 6 個字元)"
                  placeholderTextColor="#94a3b8"
                  secureTextEntry={!showPassword}
                  autoComplete="current-password"
                  testID="password-input"
                  editable={!loading}
                />
                <Pressable
                  onPress={() => setShowPassword((v) => !v)}
                  className="p-2.5 rounded-xl border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-800"
                  testID="password-toggle-visibility"
                >
                  {showPassword ? (
                    <EyeOff size={18} color="#64748b" strokeWidth={2.2} />
                  ) : (
                    <Eye size={18} color="#64748b" strokeWidth={2.2} />
                  )}
                </Pressable>
              </View>
            </Field>

            {error && (
              <View className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg px-3 py-2 mb-3">
                <Text className="text-red-700 dark:text-red-300 text-small">{error}</Text>
              </View>
            )}

            {bioReady && Platform.OS !== 'web' && (
              <Pressable
                onPress={() => setRememberCreds((v) => !v)}
                className="flex-row items-center mb-3 py-2"
              >
                <Switch
                  value={rememberCreds}
                  onValueChange={setRememberCreds}
                />
                <View className="ml-3 flex-1">
                  <View className="flex-row items-center gap-1.5">
                    <ScanFace size={16} color="#7c3aed" strokeWidth={2.2} />
                    <Text className="text-ink-900 dark:text-ink-100 text-body-sm font-semibold">
                      Face ID 快速登入
                    </Text>
                  </View>
                  <Text className="text-ink-600 dark:text-ink-400 text-caption mt-0.5">
                    記住帳密。登入過期時用 Face ID 自動重登,不再被踢出 app。
                  </Text>
                </View>
              </Pressable>
            )}

            <Pressable
              className={`bg-brand-600 dark:bg-brand-500 active:bg-brand-700 dark:active:bg-brand-600 rounded-xl py-3.5 items-center shadow-brand ${loading ? 'opacity-50' : ''}`}
              onPress={submit}
              disabled={loading}
            >
              {loading ? (
                <ActivityIndicator color="#fff" />
              ) : (
                <Text className="text-white text-h3">
                  {mode === 'login' ? '登入' : '註冊'}
                </Text>
              )}
            </Pressable>

            <Pressable
              className="mt-4"
              onPress={() => {
                setMode(mode === 'login' ? 'register' : 'login');
                setError(null);
              }}
              disabled={loading}
            >
              <Text className="text-brand-600 dark:text-brand-400 text-small text-center">
                {mode === 'login'
                  ? '還沒有帳號? 立即註冊'
                  : '已經有帳號? 回去登入'}
              </Text>
            </Pressable>
          </View>

          {__DEV__ && (
            <Text className="text-ink-400 dark:text-ink-600 text-micro text-center mt-6">
              API: {getBaseUrl() || '(未設定)'}
            </Text>
          )}
        </View>
      </View>
        </View>
      </KeyboardAwareScrollView>
    </View>
  );
}

// ============================================================
// Small subcomponents
// ============================================================

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <View className="mb-4">
      <Text className="text-ink-600 dark:text-ink-300 text-micro mb-1.5 tracking-wider font-semibold">
        {label.toUpperCase()}
      </Text>
      {children}
      {hint && (
        <Text className="text-ink-400 dark:text-ink-500 text-micro mt-1">{hint}</Text>
      )}
    </View>
  );
}

function BrandFeature({
  icon,
  label,
  desc,
}: {
  icon: React.ReactNode;
  label: string;
  desc: string;
}) {
  return (
    <View className="flex-row items-start gap-3">
      <View className="bg-brand-500/30 rounded-xl w-10 h-10 items-center justify-center">
        {icon}
      </View>
      <View className="flex-1 pt-1">
        <Text className="text-white text-h3 mb-0.5">{label}</Text>
        <Text className="text-brand-100/80 text-small">{desc}</Text>
      </View>
    </View>
  );
}
