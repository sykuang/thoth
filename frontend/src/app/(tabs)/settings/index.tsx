/**
 * 設定首頁 (Phase 8.2 — 重新定位為 menu hub).
 *
 * Phase 8 之前: 這頁是 bank cred 編輯 (4 + tab 內最厚).
 * Phase 8.2 (2026-06-15 使用者指示 IA 重整): bank cred 編輯搬到帳戶 tab,
 * 這頁改成設定首頁, 列出可用的設定 sub-screen + 外幣顯示偏好 toggle.
 *
 * 目前有:
 *   - 分類規則 (categories)
 *   - 外幣顯示偏好 (auto / always_twd / always_original)
 *
 * Phase 8.2 follow-up (2026-06-15 使用者「這個不需要了」): 移除指向帳戶 tab
 * 的「銀行帳號」link — 使用者本來就會去帳戶 tab, 從設定再繞一圈是多餘的。
 *
 * 未來新設定 (主題 / 語系 / 備份匯出) 都加在這頁.
 */
import { Link, type Href } from 'expo-router';
import { Bell, ChevronRight, Clock3, Tags, Workflow, type LucideIcon } from 'lucide-react-native';
import { useEffect, useState, type ReactNode } from 'react';
import { Alert, AppState, Linking, Platform, Pressable, Switch, Text, View } from 'react-native';
import { KeyboardAwareScrollView } from '@/components/KeyboardAwareScrollView';
import { SnapTradeConnectionSettings } from '@/components/SnapTradeSections';

import { usePreferences } from '@/hooks/usePreferences';
import { biometricAvailable } from '@/lib/biometric';
import { clearCredentials, hasCredentials } from '@/lib/credentials';
import {
  getPushPermissionStatus,
  getPushProvider,
  registerForPushNotifications,
  requestPushNotifications,
} from '@/lib/push';
import { useAuthStore } from '@/stores/auth';
import { FX_DISPLAY_MODES, CARD_DATE_BASIS_MODES, type CardDateBasis } from '@/types/api';

export default function SettingsHomeScreen() {
  const pushProvider = getPushProvider();
  const showDevicePush = Platform.OS !== 'web' && pushProvider !== 'none' && pushProvider !== 'webhook';

  return (
    <KeyboardAwareScrollView className="flex-1 bg-ink-50 dark:bg-ink-950">
      <View className="px-6 py-6 max-w-[800px] w-full mx-auto">
        <Text className="text-ink-900 dark:text-ink-50 text-h1 mb-2">設定</Text>
        <Text className="text-ink-500 dark:text-ink-400 text-small mb-6">
          管理資料顯示、分類自動化與安全性
        </Text>

        <SettingsGroup title="資料與顯示">
          <FxDisplayToggle />
          <View className="h-px bg-ink-100 dark:bg-ink-800 my-3" />
          <CardDateBasisToggle />
        </SettingsGroup>

        <SettingsGroup title="分類與自動化" testID="settings-classification-group">
          <SettingsLinkRow
            href="/(tabs)/settings/labels"
            icon={Tags}
            title="分類與標籤"
            description="管理主分類、子分類與自訂標籤"
            testID="settings-labels-link"
          />
          <SettingsLinkRow
            href="/(tabs)/settings/categories"
            icon={Workflow}
            title="自動分類規則"
            description="依交易內容自動套用分類與排除條件"
            testID="settings-categories-link"
          />
          <SettingsLinkRow
            href="/(tabs)/settings/auto-sync"
            icon={Clock3}
            title="自動同步"
            description="每天自動同步全部已綁定帳號"
            testID="settings-auto-sync-link"
            last
          />
        </SettingsGroup>

        <SettingsGroup title="券商連結">
          <SnapTradeConnectionSettings />
        </SettingsGroup>

        {showDevicePush && (
          <SettingsGroup title="通知">
            <PushNotificationSetting />
          </SettingsGroup>
        )}

        {/* 安全性 — iOS native 才顯示 (web 沒有 Face ID) */}
        {Platform.OS !== 'web' && (
          <SettingsGroup title="安全性">
            <BiometricToggle />
            <View className="h-px bg-ink-100 dark:bg-ink-800 my-3" />
            <RememberCredentialsToggle />
          </SettingsGroup>
        )}
      </View>
    </KeyboardAwareScrollView>
  );
}

function SettingsGroup({
  title,
  testID,
  children,
}: {
  title: string;
  testID?: string;
  children: ReactNode;
}) {
  return (
    <View className="mb-5" testID={testID}>
      <Text className="text-ink-500 dark:text-ink-400 text-micro font-semibold tracking-wider mb-2 px-1">
        {title}
      </Text>
      <View className="bg-white dark:bg-ink-900 rounded-2xl px-5 shadow-card overflow-hidden">
        {children}
      </View>
    </View>
  );
}

function SettingsLinkRow({
  href,
  icon: Icon,
  title,
  description,
  testID,
  last = false,
}: {
  href: Href;
  icon: LucideIcon;
  title: string;
  description: string;
  testID: string;
  last?: boolean;
}) {
  return (
    <Link href={href} asChild>
      <Pressable
        className={`flex-row items-center py-4 active:opacity-60 ${
          last ? '' : 'border-b border-ink-100 dark:border-ink-800'
        }`}
        testID={testID}
      >
        <View className="w-9 h-9 rounded-xl bg-brand-100 dark:bg-brand-900 items-center justify-center mr-3">
          <Icon size={18} color="#9333ea" strokeWidth={2.3} />
        </View>
        <View className="flex-1 pr-3">
          <Text className="text-ink-900 dark:text-ink-50 text-body font-semibold">{title}</Text>
          <Text className="text-ink-500 dark:text-ink-400 text-small mt-0.5">{description}</Text>
        </View>
        <ChevronRight size={18} color="#94a3b8" />
      </Pressable>
    </Link>
  );
}

function SettingsDisclosure({
  title,
  summary,
  testID,
  children,
}: {
  title: string;
  summary: string;
  testID: string;
  children: ReactNode;
}) {
  const [expanded, setExpanded] = useState(false);

  return (
    <View>
      <Pressable
        onPress={() => setExpanded((value) => !value)}
        className="flex-row items-center py-4 active:opacity-60"
        accessibilityRole="button"
        accessibilityState={{ expanded }}
        testID={testID}
      >
        <View className="flex-1 pr-3">
          <Text className="text-ink-900 dark:text-ink-50 text-body font-semibold">{title}</Text>
          <Text className="text-ink-500 dark:text-ink-400 text-small mt-0.5">{summary}</Text>
        </View>
        <ChevronRight
          size={18}
          color="#94a3b8"
          style={{ transform: [{ rotate: expanded ? '90deg' : '0deg' }] }}
        />
      </Pressable>
      {expanded && <View className="pb-4">{children}</View>}
    </View>
  );
}

// ============================================================
// FxDisplayToggle (Phase 6 → 保留, Phase 8.2 從原 cred 編輯頁搬過來) —
// 外幣顯示偏好 (auto / always_twd / always_original).
// segmented control pattern; 寫進 backend user_preferences.
// ============================================================
function FxDisplayToggle() {
  const { data: prefs, mutate, isMutating } = usePreferences();
  const mode = prefs.fx_display_mode;
  const activeOpt = FX_DISPLAY_MODES.find((o) => o.value === mode);

  return (
    <SettingsDisclosure
      title="外幣交易顯示"
      summary={activeOpt?.label ?? '自動'}
      testID="settings-fx-disclosure"
    >
      <Text className="text-ink-500 dark:text-ink-400 text-small mb-3">
        選擇外幣交易在交易表與明細中的主要顯示幣別。
      </Text>
      <View
        className={`self-start flex-row bg-ink-100 dark:bg-ink-800 rounded-lg p-1 ${
          isMutating ? 'opacity-50' : ''
        }`}
      >
        {FX_DISPLAY_MODES.map((opt) => {
          const isSel = mode === opt.value;
          return (
            <Pressable
              key={opt.value}
              onPress={() => {
                if (!isMutating && !isSel) mutate({ fx_display_mode: opt.value });
              }}
              disabled={isMutating}
              testID={`fx-mode-${opt.value}`}
              className={`px-3 py-1.5 rounded-md ${isSel ? 'bg-white dark:bg-ink-700' : ''}`}
            >
              <Text
                className={`text-small ${
                  isSel
                    ? 'text-ink-900 dark:text-ink-50 font-semibold'
                    : 'text-ink-500 dark:text-ink-400'
                }`}
              >
                {opt.label}
              </Text>
            </Pressable>
          );
        })}
      </View>
      {activeOpt && (
        <Text className="text-ink-500 dark:text-ink-400 text-micro mt-2">
          {activeOpt.hint}
        </Text>
      )}
    </SettingsDisclosure>
  );
}

// ============================================================
// CardDateBasisToggle — 信用卡交易日期認列 (消費日 / 入帳日).
// 影響 /transactions 的日期篩選、排序、月份歸屬、統計與明細列顯示。
// ============================================================
function CardDateBasisToggle() {
  const { data: prefs, mutate, isMutating } = usePreferences();
  const mode: CardDateBasis = prefs.card_date_basis ?? 'consume';
  const activeLabel = CARD_DATE_BASIS_MODES.find((option) => option.value === mode)?.label;

  return (
    <SettingsDisclosure
      title="信用卡交易日期認列"
      summary={activeLabel ?? '消費日'}
      testID="settings-card-date-disclosure"
    >
      <Text className="text-ink-500 dark:text-ink-400 text-small mb-3">
        此設定會影響明細篩選、月份歸屬與統計。
      </Text>
      <View
        className={`self-start flex-row bg-ink-100 dark:bg-ink-800 rounded-lg p-1 ${
          isMutating ? 'opacity-50' : ''
        }`}
      >
        {CARD_DATE_BASIS_MODES.map((opt) => {
          const isSel = mode === opt.value;
          return (
            <Pressable
              key={opt.value}
              onPress={() => {
                if (!isMutating && !isSel) mutate({ card_date_basis: opt.value });
              }}
              disabled={isMutating}
              testID={`card-date-basis-${opt.value}`}
              className={`px-3 py-1.5 rounded-md ${isSel ? 'bg-white dark:bg-ink-700' : ''}`}
            >
              <Text
                className={`text-small ${
                  isSel
                    ? 'text-ink-900 dark:text-ink-50 font-semibold'
                    : 'text-ink-500 dark:text-ink-400'
                }`}
              >
                {opt.label}
              </Text>
            </Pressable>
          );
        })}
      </View>
    </SettingsDisclosure>
  );
}

function PushNotificationSetting() {
  const [permission, setPermission] = useState<'granted' | 'denied' | 'undetermined' | 'unavailable' | null>(null);
  const [registrationFailed, setRegistrationFailed] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let mounted = true;
    const refresh = async () => {
      const status = await getPushPermissionStatus();
      const failed = status === 'granted' && (await registerForPushNotifications()) == null;
      if (!mounted) return;
      setPermission(status);
      setRegistrationFailed(failed);
    };
    void refresh();
    const subscription = AppState.addEventListener('change', (state) => {
      if (state === 'active') void refresh();
    });
    return () => {
      mounted = false;
      subscription.remove();
    };
  }, []);

  const handlePress = async () => {
    setBusy(true);
    try {
      if (permission === 'denied') {
        await Linking.openSettings();
        return;
      }
      const result = await requestPushNotifications();
      setPermission(
        result === 'registered' || result === 'registration_failed'
          ? 'granted'
          : result === 'permission_denied'
            ? 'denied'
            : 'unavailable',
      );
      setRegistrationFailed(result === 'registration_failed');
    } catch {
      Alert.alert('無法啟用通知', '請稍後再試，或到系統設定檢查 Thoth 的通知權限。');
    } finally {
      setBusy(false);
    }
  };

  const disabled =
    busy ||
    permission === null ||
    permission === 'unavailable' ||
    (permission === 'granted' && !registrationFailed);

  return (
    <View className="flex-row items-center gap-3 py-4">
      <View className="w-9 h-9 rounded-xl bg-brand-100 dark:bg-brand-900 items-center justify-center">
        <Bell size={18} color="#9333ea" strokeWidth={2.3} />
      </View>
      <View className="flex-1">
        <Text className="text-ink-900 dark:text-ink-50 text-body font-semibold">推播通知</Text>
        <Text className="text-ink-500 dark:text-ink-400 text-small mt-0.5">
          {permission === 'granted' && registrationFailed
            ? '權限已開啟，但裝置註冊失敗，請重試'
            : permission === 'granted'
              ? '已啟用帳單與同步狀態通知'
              : permission === 'unavailable'
                ? '此裝置不支援推播通知'
                : permission === 'denied'
                  ? '系統權限已關閉，請到設定重新開啟'
                  : '只在你按下啟用後才會要求系統權限'}
        </Text>
      </View>
      <Pressable
        onPress={handlePress}
        disabled={disabled}
        accessibilityRole="button"
        accessibilityState={{ disabled }}
        className={`rounded-lg px-3 py-2 ${
          (permission === 'granted' && !registrationFailed) || permission === 'unavailable'
            ? 'bg-ink-100 dark:bg-ink-800'
            : 'bg-brand-600 active:bg-brand-700'
        } ${busy || permission === null ? 'opacity-50' : ''}`}
        testID="settings-push-enable"
      >
        <Text className={disabled ? 'text-ink-500 text-small' : 'text-white text-small font-semibold'}>
          {busy
            ? '處理中…'
            : registrationFailed
              ? '重試'
              : permission === 'granted'
                ? '已啟用'
                : permission === 'unavailable'
                  ? '不可用'
                  : permission === 'denied'
                    ? '系統設定'
                    : '啟用'}
        </Text>
      </Pressable>
    </View>
  );
}

// ============================================================
// RememberCredentialsToggle (L13 — 2026-06-22 使用者指示) —
// Face ID 快速登入 = 把帳密存進 Keychain (requireAuthentication=true),
// refresh token chain 死透時自動 Face ID → silent re-login,
// 使用者不會被踢回 /login 重新打字。
//
// 跟上面 BiometricToggle 不一樣:
//   - BiometricToggle = 每次開 app 都過 Face ID (額外保護層,session 還活的時候用)
//   - 這個 = session 死透時用 Face ID 救一次 (replace login screen),
//     兩者完全獨立可同時開、可分開開。
// ============================================================
function RememberCredentialsToggle() {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [hardwareReady, setHardwareReady] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void (async () => {
      setHardwareReady(await biometricAvailable());
      setEnabled(await hasCredentials());
    })();
  }, []);

  const handleToggle = async (next: boolean) => {
    if (!next) {
      // 關閉 = 清掉 Keychain item
      Alert.alert(
        '關閉 Face ID 快速登入?',
        '會清除裝置上儲存的帳密。下次 session 過期時你需要手動重新登入。',
        [
          { text: '取消', style: 'cancel' },
          {
            text: '關閉',
            style: 'destructive',
            onPress: async () => {
              setBusy(true);
              try {
                await clearCredentials();
                setEnabled(false);
              } finally {
                setBusy(false);
              }
            },
          },
        ],
      );
      return;
    }
    // 開啟 = 引導使用者登出再重登 (沒辦法在這頁問密碼,密碼明文 prop drilling 風險高)
    Alert.alert(
      '啟用 Face ID 快速登入',
      '需要登出後重新登入,在登入頁勾「Face ID 快速登入」即可記住帳密。',
      [
        { text: '取消', style: 'cancel' },
        {
          text: '登出',
          onPress: () => {
            useAuthStore.getState().logout();
          },
        },
      ],
    );
  };

  const disabled = hardwareReady === false || enabled === null || busy;

  return (
    <View>
      <View className="flex-row items-center gap-3 py-2 px-1">
        <View className="flex-1">
          <Text className="text-ink-900 dark:text-ink-50 text-h3 mb-1">
            記住帳密 (Face ID 快速登入)
          </Text>
          <Text className="text-ink-500 dark:text-ink-400 text-small">
            把帳密加密存進 Keychain。登入過期時自動用 Face ID 解鎖重登,
            不再被踢出 app 重新打字。
          </Text>
        </View>
        <Switch
          value={enabled === true}
          onValueChange={handleToggle}
          disabled={disabled}
          testID="settings-remember-creds-toggle"
        />
      </View>
      {hardwareReady === false && (
        <Text className="text-ink-400 dark:text-ink-500 text-micro px-1 mt-1">
          此裝置未設定 Face ID / Touch ID,請先到 iOS 設定中啟用後再回來開啟。
        </Text>
      )}
      {enabled === true && hardwareReady === true && (
        <Text className="text-accent-600 dark:text-accent-400 text-micro px-1 mt-1">
          ✓ 已啟用 — session 過期時會自動 Face ID 重登
        </Text>
      )}
    </View>
  );
}
// ============================================================
// BiometricToggle (Phase 8.5 — 使用者 2026-06-16 指示) —
// Face ID / Touch ID 解鎖開關. 預設 off → fresh install 不會跳權限框.
// hardwareReady=false 時 (沒設 Face ID / 模擬器 / 舊機型) Switch 變灰且註腳提示.
// 開啟瞬間立即 trigger 一次 Face ID 驗證, 確認 user 真的能解才寫進 store.
// ============================================================
function BiometricToggle() {
  const biometricEnabled = useAuthStore((s) => s.biometricEnabled);
  const setBiometricEnabled = useAuthStore((s) => s.setBiometricEnabled);
  const [hardwareReady, setHardwareReady] = useState<boolean | null>(null);

  useEffect(() => {
    biometricAvailable().then(setHardwareReady);
  }, []);

  const handleToggle = async (next: boolean) => {
    if (!next) {
      setBiometricEnabled(false);
      return;
    }
    // 開啟前先試一次 — 若 user 取消 / 失敗就不啟用,避免 store 寫了 true
    // 但下次開 app 又卡解鎖框出不去 (deadlock).
    const { authenticate } = await import('@/lib/biometric');
    const ok = await authenticate('啟用生物識別解鎖');
    if (ok) setBiometricEnabled(true);
  };

  const disabled = hardwareReady === false || hardwareReady === null;

  return (
    <View>
      <View className="flex-row items-center gap-3 py-2 px-1">
        <View className="flex-1">
          <Text className="text-ink-900 dark:text-ink-50 text-h3 mb-1">
            Face ID / Touch ID 解鎖
          </Text>
          <Text className="text-ink-500 dark:text-ink-400 text-small">
            每次打開 Thoth 都需通過生物識別驗證才能看到資料。
          </Text>
        </View>
        <Switch
          value={biometricEnabled}
          onValueChange={handleToggle}
          disabled={disabled}
          testID="settings-biometric-toggle"
        />
      </View>
      {hardwareReady === false && (
        <Text className="text-ink-400 dark:text-ink-500 text-micro px-1 mt-1">
          此裝置未設定 Face ID / Touch ID,請先到 iOS 設定中啟用後再回來開啟。
        </Text>
      )}
    </View>
  );
}
