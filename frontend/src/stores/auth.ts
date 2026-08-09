/**
 * Auth state (zustand) — platform-aware persistence.
 *
 * Web   → localStorage (sync)
 * Native (iOS) → expo-secure-store (async, keychain-backed)
 *
 * zustand v5 createJSONStorage 同時支援 sync 與 async storage interface，
 * 所以同一份 store code 兩端共用，沒有額外 wrapper。
 */
import { Platform } from 'react-native';
import * as SecureStore from 'expo-secure-store';
import { create } from 'zustand';
import { createJSONStorage, persist } from 'zustand/middleware';

import { activateReplicaOwner, clearReplicaOwner, makeReplicaOwnerKey } from '@/lib/replica';
import { replicaStore } from '@/lib/replicaStore';

type AuthState = {
  token: string | null;
  /** L9 (2026-06-21): long-lived refresh token (DB-backed, rotation chain).
   * Used by lib/api.ts on 401 to silently refresh access token without user re-login.
   * Stored in Keychain (iOS) / localStorage (web) alongside access token. */
  refreshToken: string | null;
  email: string | null;
  /** Backend server URL, e.g. "http://192.168.1.50:8000". Empty on iOS first launch
   * forces the user to set it on the login screen. Web auto-fills from build-time env. */
  serverUrl: string;
  /** Optional server-level X-API-Key (set if backend has SERVER_API_KEY env).
   * Sent on EVERY request as `X-API-Key` header. Empty string means none. */
  apiKey: string;
  /** When true, on every app boot (with a stored token) prompt Face ID / Touch ID
   * before showing the dashboard. Default false — user opt-in via Settings. */
  biometricEnabled: boolean;
  hydrated: boolean;
  /** L9: setAuth now takes refreshToken too. Old 2-arg signature kept for callers
   * that haven't been updated yet (backward compat — refreshToken stays null). */
  setAuth: (token: string, email: string, refreshToken?: string | null) => void;
  /** L9: lib/api.ts uses this when refresh endpoint returns a new pair. */
  setTokens: (accessToken: string, refreshToken: string | null) => void;
  setServerUrl: (url: string) => void;
  setApiKey: (key: string) => void;
  setBiometricEnabled: (v: boolean) => void;
  logout: () => void;
  _setHydrated: (v: boolean) => void;
};

// Web: localStorage 直接接（同步 API）
function webStorageAvailable(): boolean {
  return typeof globalThis.localStorage?.getItem === 'function'
    && typeof globalThis.localStorage?.setItem === 'function'
    && typeof globalThis.localStorage?.removeItem === 'function';
}

const webStorage = {
  getItem: (name: string): string | null => {
    if (!webStorageAvailable()) return null;
    return localStorage.getItem(name);
  },
  setItem: (name: string, value: string): void => {
    if (!webStorageAvailable()) return;
    localStorage.setItem(name, value);
  },
  removeItem: (name: string): void => {
    if (!webStorageAvailable()) return;
    localStorage.removeItem(name);
  },
};

// Native (iOS): SecureStore（async）；keychain item 設成
// AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY → 裝置 reboot 後第一次解鎖才能存取，
// 且 keychain item 不會隨 iCloud backup 漂出去。
// SecureStore size limit 2KB on iOS，token + email + serverUrl 綽綽有餘。
const nativeStorage = {
  getItem: async (name: string): Promise<string | null> => SecureStore.getItemAsync(name),
  setItem: async (name: string, value: string): Promise<void> => {
    await SecureStore.setItemAsync(name, value, {
      keychainAccessible: SecureStore.AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY,
    });
  },
  removeItem: async (name: string): Promise<void> => SecureStore.deleteItemAsync(name),
};

const safeStorage = Platform.OS === 'web' ? webStorage : nativeStorage;

// Web fallback: 用 build-time env，因為瀏覽器在跟 server 同台/同網段時
// 不需要使用者手填。Native (iOS) fallback 是空字串，逼使用者在 login
// 頁填一次（之後 SecureStore 保存）。
const configuredWebServerUrl = process.env.EXPO_PUBLIC_API_URL;
const DEFAULT_SERVER_URL = Platform.OS === 'web'
  ? configuredWebServerUrl === '__THOTH_SAME_ORIGIN__' && typeof globalThis.location?.origin === 'string'
    ? `${globalThis.location.origin}/api`
    : (configuredWebServerUrl ?? 'http://localhost:8000')
  : '';

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      token: null,
      refreshToken: null,
      email: null,
      serverUrl: DEFAULT_SERVER_URL,
      apiKey: '',
      biometricEnabled: false,
      hydrated: false,
      setAuth: (token, email, refreshToken = null) => set((state) => {
        if (state.email && state.email !== email) {
          void clearReplicaOwner(replicaStore, state.serverUrl, state.email);
        }
        activateReplicaOwner(makeReplicaOwnerKey(state.serverUrl, email));
        return { token, email, refreshToken };
      }),
      setTokens: (accessToken, refreshToken) =>
        set({ token: accessToken, refreshToken }),
      setServerUrl: (serverUrl) => set((state) => {
        if (state.email && state.serverUrl !== serverUrl) {
          void clearReplicaOwner(replicaStore, state.serverUrl, state.email);
        }
        if (state.email) activateReplicaOwner(makeReplicaOwnerKey(serverUrl, state.email));
        return { serverUrl };
      }),
      setApiKey: (apiKey) => set({ apiKey }),
      setBiometricEnabled: (biometricEnabled) => set({ biometricEnabled }),
      logout: () => set((state) => {
        void clearReplicaOwner(replicaStore, state.serverUrl, state.email);
        return { token: null, refreshToken: null, email: null };
      }),
      _setHydrated: (v) => set({ hydrated: v }),
    }),
    {
      name: 'bank-crawlers-auth',
      storage: createJSONStorage(() => safeStorage),
      partialize: (state) => ({
        token: state.token,
        refreshToken: state.refreshToken,
        email: state.email,
        serverUrl: state.serverUrl,
        apiKey: state.apiKey,
        biometricEnabled: state.biometricEnabled,
      }),
      onRehydrateStorage: () => (state, error) => {
        // 即使 SecureStore 讀失敗也要把 hydrated 設 true，否則整個 UI
        // 永遠卡在 <ActivityIndicator />（fresh install on iOS、keychain
        // 不可用、simulator 等情境都會中招）。
        state?._setHydrated(true);
        if (error) {
           
          console.warn('[auth] rehydrate failed:', error);
        }
      },
    }
  )
);

// Safety net: 某些 RN/iOS 情境下 zustand persist 的 onRehydrateStorage
// callback 不會 fire（例：SecureStore 第一次讀 keychain silent hang、
// Hermes JSI bridge race condition、async storage promise 卡 micro-task
// queue 等）。3 秒後不管結果如何強制 hydrated=true 保 UI 一定能 render。
//
// 業界 zustand v5 + RN AsyncStorage / SecureStore 都會踩這坑。production
// 上沒這個 net，fresh install 的使用者第一次開 app 會永遠卡白屏。
if (typeof setTimeout !== 'undefined') {
  setTimeout(() => {
    const s = useAuthStore.getState();
    if (!s.hydrated) {
       
      console.warn('[auth] rehydrate timeout, forcing hydrated=true');
      s._setHydrated(true);
    }
  }, 3000);
}
