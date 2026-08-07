/**
 * (tabs) layout — gated by auth. If no token, redirect to /login.
 *
 * L5-RWD: header/tab colors 用 brand-* token, dark mode 由 useColorScheme 自動切。
 */
import { Redirect, Tabs } from 'expo-router';
import { LayoutDashboard, ReceiptText, Settings, WalletCards } from 'lucide-react-native';
import { useColorScheme } from 'nativewind';

import { useAuthStore } from '@/stores/auth';

export default function TabsLayout() {
  const token = useAuthStore((s) => s.token);
  const { colorScheme } = useColorScheme();
  const isDark = colorScheme === 'dark';

  if (!token) {
    return <Redirect href="/login" />;
  }
  return (
    <Tabs
      screenOptions={{
        headerStyle: { backgroundColor: isDark ? '#0f172a' : '#7e22ce' }, // ink-900 / brand-700
        headerTintColor: '#fff',
        headerTitleStyle: { fontWeight: '600' },
        tabBarActiveTintColor: isDark ? '#c084fc' : '#7e22ce', // brand-400 / brand-700
        tabBarInactiveTintColor: isDark ? '#64748b' : '#94a3b8',
        tabBarStyle: {
          backgroundColor: isDark ? '#0f172a' : '#ffffff',
          borderTopColor: isDark ? '#1e293b' : '#e2e8f0',
        },
        tabBarIconStyle: { marginBottom: 2 },
        sceneStyle: { backgroundColor: isDark ? '#020617' : '#f8fafc' },
      }}
    >
      <Tabs.Screen
        name="dashboard"
        options={{
          title: '儀表板',
          tabBarIcon: ({ color, size, focused }) => (
            <LayoutDashboard size={size} color={color} strokeWidth={focused ? 2.8 : 2.2} />
          ),
        }}
      />
      <Tabs.Screen
        name="transactions"
        options={{
          title: '交易',
          tabBarIcon: ({ color, size, focused }) => (
            <ReceiptText size={size} color={color} strokeWidth={focused ? 2.8 : 2.2} />
          ),
        }}
      />
      <Tabs.Screen
        name="cards"
        options={{
          title: '帳戶',
          tabBarIcon: ({ color, size, focused }) => (
            <WalletCards size={size} color={color} strokeWidth={focused ? 2.8 : 2.2} />
          ),
        }}
      />
      <Tabs.Screen
        name="investments"
        options={{
          href: null,
        }}
      />
      <Tabs.Screen
        name="settings"
        options={{
          title: '設定',
          tabBarIcon: ({ color, size, focused }) => (
            <Settings size={size} color={color} strokeWidth={focused ? 2.8 : 2.2} />
          ),
        }}
      />
    </Tabs>
  );
}
