/**
 * Settings stack layout (Phase 8.2 — bank cred 編輯已搬到帳戶 tab).
 *
 * - index = 設定首頁 menu hub (headerShown=false, 用 tab header)
 * - categories = 分類規則 (push 進來時帶 navigation header + back 按鈕)
 */
import { Stack } from 'expo-router';
import { useColorScheme } from 'nativewind';

export default function SettingsLayout() {
  const { colorScheme } = useColorScheme();
  const isDark = colorScheme === 'dark';
  return (
    <Stack>
      <Stack.Screen name="index" options={{ headerShown: false }} />
      <Stack.Screen
        name="categories"
        options={{
          title: '自動分類規則',
          headerBackTitle: '設定',
          headerStyle: { backgroundColor: isDark ? '#0f172a' : '#7e22ce' },
          headerTintColor: '#fff',
          headerTitleStyle: { fontWeight: '600' },
        }}
      />
      <Stack.Screen
        name="labels"
        options={{
          title: '分類與標籤',
          headerBackTitle: '設定',
          headerStyle: { backgroundColor: isDark ? '#0f172a' : '#7e22ce' },
          headerTintColor: '#fff',
          headerTitleStyle: { fontWeight: '600' },
        }}
      />
      <Stack.Screen
        name="auto-sync"
        options={{
          title: '自動同步',
          headerBackTitle: '設定',
          headerStyle: { backgroundColor: isDark ? '#0f172a' : '#7e22ce' },
          headerTintColor: '#fff',
          headerTitleStyle: { fontWeight: '600' },
        }}
      />
    </Stack>
  );
}
