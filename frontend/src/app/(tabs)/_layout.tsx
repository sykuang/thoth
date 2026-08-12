/**
 * (tabs) layout — gated by auth. If no token, redirect to /login.
 *
 * L5-RWD: header/tab colors 用 brand-* token, dark mode 由 useColorScheme 自動切。
 */
import { Redirect, Tabs } from 'expo-router';
import { LayoutDashboard, ReceiptText, Settings, WalletCards } from 'lucide-react-native';
import { useColorScheme } from 'nativewind';
import { Platform } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { useBreakpoint } from '@/hooks/useBreakpoint';
import { useAuthStore } from '@/stores/auth';

export default function TabsLayout() {
  const token = useAuthStore((s) => s.token);
  const { colorScheme } = useColorScheme();
  const bp = useBreakpoint();
  const insets = useSafeAreaInsets();
  const isDark = colorScheme === 'dark';
  const isDesktop = Platform.OS === 'web' && bp.isLg;

  if (!token) {
    return <Redirect href="/login" />;
  }
  return (
    <Tabs
      screenOptions={{
        headerShown: false,
        tabBarActiveTintColor: isDark ? '#c084fc' : '#7e22ce', // brand-400 / brand-700
        tabBarInactiveTintColor: isDark ? '#64748b' : '#94a3b8',
        tabBarPosition: isDesktop ? 'left' : 'bottom',
        tabBarVariant: isDesktop ? 'material' : 'uikit',
        tabBarLabelPosition: isDesktop ? 'below-icon' : undefined,
        tabBarStyle: {
          backgroundColor: isDark ? '#0f172a' : '#ffffff',
          ...(isDesktop
            ? {
                width: 92,
                borderRightColor: isDark ? '#1e293b' : '#e2e8f0',
              }
            : {
                borderTopColor: isDark ? '#1e293b' : '#e2e8f0',
              }),
        },
        tabBarIconStyle: { marginBottom: 2 },
        sceneStyle: {
          backgroundColor: isDark ? '#020617' : '#f8fafc',
          paddingTop: isDesktop ? 0 : insets.top,
        },
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
