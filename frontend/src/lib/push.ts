/**
 * Push notification token registration + handlers.
 *
 * 設計鐵令（使用者 turn 3「實作的時候應該 expo 是可選模式」+ turn 4「只要做 B」）:
 *  - 走 B 路徑 — Expo 套件,但直接拿 raw APNs token,**不**經 Expo Push Service relay
 *  - 開源 user 不設 PUSH_PROVIDER 時 backend 預設 noop,frontend 仍註冊 token (idempotent UPSERT),
 *    未來 user 啟用 PUSH_PROVIDER=apns 累積的 token 立刻 active
 *  - Provider 由 frontend 編譯期決定 — EXPO_PUBLIC_PUSH_PROVIDER env (build time, NOT runtime)
 *    default = 'apns' (因為臣妾這次只實作 B); 開源 fork 想要 webhook 自己 fork 改
 *
 * 用法 (RootLayout):
 *   useEffect(() => {
 *     if (token) registerForPushNotifications();
 *   }, [token]);
 */
import Constants from 'expo-constants';
import * as Device from 'expo-device';
import * as Notifications from 'expo-notifications';
import { Platform } from 'react-native';

import { api } from './api';

/** EXPO_PUBLIC_PUSH_PROVIDER (build time) — 預設 'expo' (B1 路徑,使用者選擇)。 */
const PUSH_PROVIDER = (process.env.EXPO_PUBLIC_PUSH_PROVIDER ?? 'expo').toLowerCase() as
  | 'apns'
  | 'webhook'
  | 'fcm'
  | 'expo'
  | 'none';

/** 預設 notification handler — alert + sound + badge 都顯示 (foreground 時)。 */
Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldPlaySound: true,
    shouldSetBadge: true,
    shouldShowBanner: true,
    shouldShowList: true,
  }),
});

/**
 * 取得 push token + 註冊到 backend。Idempotent — 每次 boot 都該呼叫一次。
 *
 * 對下列情況一律 silent return null (不報錯,不阻擋 app boot):
 *   - PUSH_PROVIDER=none
 *   - Web / Tauri 桌面 (不支援 native push)
 *   - iOS Simulator (`Device.isDevice=false` — APNs 不發 token 給模擬器)
 *   - User 拒絕通知權限
 *   - Expo native module 不在 (fresh prebuild 還沒做 / Expo Go)
 *
 * 失敗永遠 console.warn,絕不 throw。Push 不該擋住 app 啟動。
 */
export async function registerForPushNotifications(): Promise<string | null> {
  if (PUSH_PROVIDER === 'none') {
    console.log('[push] PUSH_PROVIDER=none, skipping');
    return null;
  }
  if (Platform.OS === 'web') {
    console.log('[push] web platform, native push not supported');
    return null;
  }
  if (!Device.isDevice) {
    console.log('[push] not a physical device (simulator?), skipping');
    return null;
  }

  try {
    // 1. 權限 — 沒給就跳請求 (iOS 第一次 boot 會跳系統 dialog)
    const { status: existingStatus } = await Notifications.getPermissionsAsync();
    let finalStatus = existingStatus;
    if (existingStatus !== 'granted') {
      const { status } = await Notifications.requestPermissionsAsync();
      finalStatus = status;
    }
    if (finalStatus !== 'granted') {
      console.warn('[push] notification permission not granted:', finalStatus);
      return null;
    }

    // 2. 拿 token — 依 PUSH_PROVIDER 決定要 raw APNs / FCM token vs Expo Push Service token
    let token: string;
    if (PUSH_PROVIDER === 'apns' || PUSH_PROVIDER === 'fcm') {
      // B 路徑 — 直接拿 device token (Apple/Google raw),backend 直連 APNs/FCM
      const result = await Notifications.getDevicePushTokenAsync();
      token = result.data;
    } else if (PUSH_PROVIDER === 'expo') {
      // 走 Expo Push Service relay (需要 expoConfig.extra.eas.projectId,使用者沒選這條)
      const projectId =
        Constants.expoConfig?.extra?.eas?.projectId ??
        Constants.easConfig?.projectId;
      if (!projectId) {
        console.warn('[push] PUSH_PROVIDER=expo but no projectId, skipping');
        return null;
      }
      const result = await Notifications.getExpoPushTokenAsync({ projectId });
      token = result.data;
    } else {
      console.warn('[push] unsupported PUSH_PROVIDER:', PUSH_PROVIDER);
      return null;
    }

    if (!token) {
      console.warn('[push] empty token returned');
      return null;
    }

    // 3. UPSERT 到 backend
    await api('/me/push-tokens', {
      method: 'PUT',
      body: {
        provider: PUSH_PROVIDER,
        token,
        platform: Platform.OS === 'ios' ? 'ios' : 'android',
        device_label: Device.modelName ?? 'Unknown device',
      },
    });
    console.log('[push] registered token', token.slice(0, 12) + '…');
    return token;
  } catch (e) {
    console.warn('[push] registration failed:', e);
    return null;
  }
}

/**
 * 收通知 (foreground 顯示 + tap navigation).
 * 回傳 cleanup function — caller useEffect return 它。
 */
export function attachNotificationListeners(opts: {
  onNotificationTap?: (data: Record<string, unknown>) => void;
}): () => void {
  const subscriptions: Notifications.EventSubscription[] = [];
  let cancelled = false;

  const handleResponse = (response: Notifications.NotificationResponse) => {
    const data = response.notification.request.content.data ?? {};
    try {
      opts.onNotificationTap?.(data as Record<string, unknown>);
    } catch (e) {
      console.warn('[push] tap handler error:', e);
    }
  };

  if (opts.onNotificationTap) {
    // Cold-start path: user taps notification while app is killed. Expo Router may first
    // receive only `thoth:///`; recover the real payload from expo-notifications.
    Notifications.getLastNotificationResponseAsync()
      .then((response) => {
        if (!cancelled && response) {
          handleResponse(response);
          Notifications.clearLastNotificationResponseAsync?.().catch(() => {});
        }
      })
      .catch((e) => {
        console.warn('[push] get last notification response failed:', e);
      });

    const sub = Notifications.addNotificationResponseReceivedListener(
      (response) => {
        handleResponse(response);
      },
    );
    subscriptions.push(sub);
  }

  return () => {
    cancelled = true;
    subscriptions.forEach((s) => s.remove());
  };
}

export function getPushProvider(): typeof PUSH_PROVIDER {
  return PUSH_PROVIDER;
}
