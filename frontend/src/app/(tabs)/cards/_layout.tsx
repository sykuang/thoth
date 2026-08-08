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

export default function CardsLayout() {
  const { colorScheme } = useColorScheme();
  const isDark = colorScheme === 'dark';
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
      <Stack.Screen name="new" options={{ title: '新增銀行帳號' }} />
      <Stack.Screen
        name="credentials/[bank]"
        options={{ title: '管理登入' }}
      />
      <Stack.Screen name="[bank]/[card_no]" options={{ title: '帳單明細' }} />
      <Stack.Screen name="brokerage/[account_id]" options={{ title: '持股明細' }} />
      <Stack.Screen name="manual/[account_id]" options={{ title: '手動帳戶' }} />
    </Stack>
  );
}
