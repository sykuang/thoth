/**
 * 帳戶 Stack layout (Phase 8.2).
 *
 * 結構：
 *   index = 帳戶列表 (每銀行 group 顯示存款+信用卡 + 「⚙️ 管理登入」按鈕)
 *   new = 新增銀行帳號 (選銀行 + label)
 *   credentials/[bank] = 該銀行底下所有 BankAccount 的 cred 管理
 *
 * 設計取捨 (使用者 2026-06-15 指示):
 *   原本 settings/index 的 bank cred 管理搬到這層。Tab header 統一由 (tabs)
 *   layout 提供 (headerShown=false), 子 route push 進來時自帶 navigation header.
 */
import { Stack } from 'expo-router';
import { useColorScheme } from 'nativewind';

import { DeterministicBackButton } from '@/components/DeterministicBackButton';
import { ROUTE_PARENTS } from '@/lib/routeParents';

export default function CardsLayout() {
  const { colorScheme } = useColorScheme();
  const isDark = colorScheme === 'dark';
  const screen = (route: keyof typeof ROUTE_PARENTS, title: string, label: string) => ({
    title,
    headerBackVisible: false,
    headerLeft: () => <DeterministicBackButton target={ROUTE_PARENTS[route]} label={label} />,
  });
  return (
    <Stack
      screenOptions={{
        headerStyle: { backgroundColor: isDark ? '#0f172a' : '#7e22ce' },
        headerTintColor: '#fff',
        headerTitleStyle: { fontWeight: '600' },
        headerBackTitle: '帳戶',
      }}
    >
      <Stack.Screen name="index" options={{ headerShown: false }} />
      <Stack.Screen name="add" options={screen('(tabs)/cards/add', '新增帳戶', '帳戶')} />
      <Stack.Screen name="new" options={screen('(tabs)/cards/new', '連結銀行帳號', '新增帳戶')} />
      <Stack.Screen
        name="credentials/[bank]"
        options={screen('(tabs)/cards/credentials/[bank]', '管理登入', '帳戶')}
      />
      <Stack.Screen name="[bank]/[card_no]" options={screen('(tabs)/cards/[bank]/[card_no]', '帳單明細', '帳戶')} />
      <Stack.Screen name="brokerage/[account_id]" options={screen('(tabs)/cards/brokerage/[account_id]', '持股明細', '帳戶')} />
      <Stack.Screen name="manual/[account_id]" options={screen('(tabs)/cards/manual/[account_id]', '手動帳戶', '帳戶')} />
      <Stack.Screen name="manual/transaction" options={{ title: '新增／編輯交易', headerBackVisible: false }} />
    </Stack>
  );
}
