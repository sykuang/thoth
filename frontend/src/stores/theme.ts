/**
 * Theme store —— 'system' | 'light' | 'dark'
 *
 * 預設 'system' 跟系統; 使用者手動切換後 persist。
 * 套到 NativeWind: app root 用 useColorScheme + setColorScheme 同步。
 */
import { Platform } from 'react-native';
import * as SecureStore from 'expo-secure-store';
import { create } from 'zustand';
import { createJSONStorage, persist } from 'zustand/middleware';

export type ThemeMode = 'system' | 'light' | 'dark';

type ThemeState = {
  mode: ThemeMode;
  setMode: (m: ThemeMode) => void;
};

// 跟 auth.ts 同一套 storage adapter (web localStorage / native SecureStore)
const webStorage = {
  getItem: (name: string): string | null => {
    if (typeof localStorage === 'undefined') return null;
    return localStorage.getItem(name);
  },
  setItem: (name: string, value: string): void => {
    if (typeof localStorage === 'undefined') return;
    localStorage.setItem(name, value);
  },
  removeItem: (name: string): void => {
    if (typeof localStorage === 'undefined') return;
    localStorage.removeItem(name);
  },
};

const nativeStorage = {
  getItem: async (name: string) => (await SecureStore.getItemAsync(name)) ?? null,
  setItem: async (name: string, value: string) => SecureStore.setItemAsync(name, value),
  removeItem: async (name: string) => SecureStore.deleteItemAsync(name),
};

export const useThemeStore = create<ThemeState>()(
  persist(
    (set) => ({
      mode: 'system',
      setMode: (mode) => set({ mode }),
    }),
    {
      name: 'theme',
      storage: createJSONStorage(() =>
        Platform.OS === 'web' ? webStorage : nativeStorage
      ),
    }
  )
);
