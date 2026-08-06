/**
 * Root layout.
 *
 * Wires:
 *   - Global Tailwind CSS (NativeWind)
 *   - Theme: useThemeStore (system/light/dark) → setColorScheme NativeWind
 *   - TanStack Query provider (shared cache across pages)
 *   - Stack navigator (login + (tabs) group)
 *   - Auth hydration gate (avoid flash of /login while persist rehydrates)
 *   - iOS biometric unlock (Touch/Face ID) when app boots with stored token
 *     web 上 authenticate() 直接回 true，不影響 web flow
 */
import '../../global.css';

import { focusManager, QueryClientProvider } from '@tanstack/react-query';
import { router, Stack } from 'expo-router';
import { useColorScheme } from 'nativewind';
import { useEffect } from 'react';
import { ActivityIndicator, AppState, Platform, View } from 'react-native';

import { ErrorBoundary } from '@/components/ErrorBoundary';
import { authenticate } from '@/lib/biometric';
import {
  attachNotificationListeners,
  registerForPushNotifications,
} from '@/lib/push';
import { queryClient } from '@/lib/queryClient';
import { routeFromNotificationData } from '@/lib/pushRoutes';
import { useAuthStore } from '@/stores/auth';
import { useThemeStore } from '@/stores/theme';

export default function RootLayout() {
  const hydrated = useAuthStore((s) => s.hydrated);
  const token = useAuthStore((s) => s.token);
  const biometricEnabled = useAuthStore((s) => s.biometricEnabled);
  const logout = useAuthStore((s) => s.logout);

  // dark mode: 把 store 的 mode 套到 NativeWind
  const themeMode = useThemeStore((s) => s.mode);
  const { setColorScheme } = useColorScheme();
  useEffect(() => {
    setColorScheme(themeMode); // 'system' | 'light' | 'dark' 三種值都吃
  }, [themeMode, setColorScheme]);

  // React Query 在 React Native 不會自行收到 browser window focus 事件。
  // 把 AppState 接到 focusManager，讓日期衍生 query（例如繳費提醒）能在
  // app 跨日後回到前景時重新取得今天的狀態；web 仍沿用 browser adapter。
  useEffect(() => {
    if (Platform.OS === 'web') return;
    focusManager.setFocused(AppState.currentState === 'active');
    const subscription = AppState.addEventListener('change', (status) => {
      focusManager.setFocused(status === 'active');
    });
    return () => subscription.remove();
  }, []);

  // Biometric unlock — opt-in only. User toggles in Settings → 安全性 → 生物識別.
  // 預設 false → fresh install 不會跳 Face ID 權限 / 解鎖框。
  useEffect(() => {
    if (!hydrated || !token || !biometricEnabled) return;
    let cancelled = false;
    (async () => {
      const ok = await authenticate('解鎖 Thoth');
      if (!cancelled && !ok) {
        // Phase C-fe (2026-06-17): 生物識別 reject = 強制登出, clear cache
        // 避免 user A 的資料留在 cache 等下一個 user 看到
        queryClient.clear();
        logout();
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [hydrated, token, biometricEnabled, logout]);

  // L11 (2026-06-22): Push notification registration + tap handler.
  // 只有 hydrated + 登入後才註冊。PUSH_PROVIDER=none 時 silent skip。
  useEffect(() => {
    if (!hydrated || !token) return;
    registerForPushNotifications().catch((e) => {
      console.warn('[push] register failed', e);
    });
  }, [hydrated, token]);

  // L11: 收到通知後 tap → deep link 跳對應頁。
  // 必須跟 push register 拆開 useEffect — 不該因為 token 變就重 attach listener。
  useEffect(() => {
    const detach = attachNotificationListeners({
      onNotificationTap: (data) => {
        const route = routeFromNotificationData(data);
        if (!route) return;
        // 例: payment_reminder → /(tabs)/cards。不要直接 push thoth:/// 這種 OS launch URL。
        try {
          router.push(route as never);
        } catch (e) {
          console.warn('[push] deep link push failed', e);
        }
      },
    });
    return detach;
  }, []);

  if (!hydrated) {
    return (
      <View className="flex-1 items-center justify-center bg-white dark:bg-ink-950">
        <ActivityIndicator size="large" />
      </View>
    );
  }

  return (
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <Stack screenOptions={{ headerShown: false }}>
          <Stack.Screen name="index" />
          <Stack.Screen name="login" />
          <Stack.Screen name="(tabs)" />
        </Stack>
      </QueryClientProvider>
    </ErrorBoundary>
  );
}
