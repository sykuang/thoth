/**
 * Credentials store — opt-in 密碼記憶 (C 方案 / L13).
 *
 * 設計：把 email + password 加密存進 iOS Keychain (expo-secure-store)，
 *      access policy 設成 `requireAuthentication: true` —— **OS-level**：
 *      Keychain 在 read 時會自動跳 Face ID / Touch ID prompt，
 *      我們不用手動呼 LocalAuthentication.authenticateAsync。
 *
 * Web 永遠回 false / 不存取（瀏覽器沒對等保護面）。
 *
 * 觸發路徑：
 *   1. 使用者登入時勾「記住密碼」→ saveCredentials()
 *   2. Settings → 生物辨識頁手動開關
 *   3. /auth/refresh 失敗時 → loadCredentials() 自動 silent re-login
 *
 * 安全模型 (參考 1Password autofill / Apple keychain best practices):
 *   - WHEN_UNLOCKED_THIS_DEVICE_ONLY：裝置解鎖後才能讀，且不會 iCloud 同步出去
 *   - requireAuthentication: true：每次 read 必過生物辨識，passcode fallback
 *   - 使用者 logout() 主動清，並提供 settings 一鍵清除
 *
 * 不存：refresh token / access token（那些走另一個 store + rotation chain，
 *      過期才會用到這裡）。這裡只負責「session 真死了」的最後一張保命符。
 */
import * as SecureStore from 'expo-secure-store';
import { Platform } from 'react-native';

const KEY = 'bank-crawlers-credentials';

export type StoredCredentials = {
  email: string;
  password: string;
};

/** 是否有儲存的帳密。不會 prompt 生物辨識，只看 keychain item 存不存在。
 *  注意：iOS 上有 keychain item != 一定能讀到，requireAuthentication 設下去後
 *  read 才會觸發 Face ID。這裡只是輕量探測，給 UI 顯示「✅ 已啟用」用。 */
export async function hasCredentials(): Promise<boolean> {
  if (Platform.OS === 'web') return false;
  try {
    // iOS 上 isAvailableAsync 不等於 hasItem；用 getItemAsync skipAuth 模式不存在，
    // 改 strategy：呼 setItemAsync 前 / clear 後我們會自己標一個 flag 在 AsyncStorage？
    // 為了避免引入第二個 store，這裡用 SecureStore 額外存一個無 auth 的 marker key。
    const marker = await SecureStore.getItemAsync(`${KEY}-marker`);
    return marker === '1';
  } catch {
    return false;
  }
}

/** 把 email + password 寫進 keychain (覆寫舊值)。需要使用者已經透過 Face ID
 *  授權 — 我們把 requireAuthentication 設 true，OS 在 set 時就會 prompt。
 *
 *  Throws if user cancels biometric prompt or keychain write fails. */
export async function saveCredentials(email: string, password: string): Promise<void> {
  if (Platform.OS === 'web') return; // no-op
  const value = JSON.stringify({ email, password } satisfies StoredCredentials);
  await SecureStore.setItemAsync(KEY, value, {
    keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY,
    requireAuthentication: true,
    authenticationPrompt: '啟用 Face ID 快速登入',
  });
  // 寫一個 unauthenticated marker 給 hasCredentials() 探測用
  await SecureStore.setItemAsync(`${KEY}-marker`, '1', {
    keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY,
  });
}

/** 拿出存好的帳密。**會** prompt Face ID / Touch ID。
 *  Returns null if no creds saved, user cancelled, or keychain read failed. */
export async function loadCredentials(
  reason = '使用 Face ID 重新登入',
): Promise<StoredCredentials | null> {
  if (Platform.OS === 'web') return null;
  try {
    const raw = await SecureStore.getItemAsync(KEY, {
      requireAuthentication: true,
      authenticationPrompt: reason,
    });
    if (!raw) return null;
    const parsed = JSON.parse(raw) as StoredCredentials;
    if (!parsed.email || !parsed.password) return null;
    return parsed;
  } catch (err) {
    // 使用者取消 Face ID / 三次失敗 / keychain corrupted — 都當沒有
    if (typeof console !== 'undefined') {
      console.warn('[credentials] loadCredentials failed:', err);
    }
    return null;
  }
}

/** 清除存好的帳密 (logout / settings 關閉時呼)。不會 prompt。 */
export async function clearCredentials(): Promise<void> {
  if (Platform.OS === 'web') return;
  try {
    await SecureStore.deleteItemAsync(KEY);
  } catch {
    // ignore — 沒存過就刪不到
  }
  try {
    await SecureStore.deleteItemAsync(`${KEY}-marker`);
  } catch {
    // ignore
  }
}
