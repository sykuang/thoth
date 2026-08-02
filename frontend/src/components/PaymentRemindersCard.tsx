/**
 * PaymentRemindersCard — Dashboard 繳費提醒 section (Phase L10).
 *
 * 規格 (cards/auto-debit/reminders backend):
 *   - reason='no_account'   未設定自動扣繳 + due 0~3 天內 → 黃色警示
 *   - reason='insufficient' 已設定扣繳戶但餘額不足 → 紅色警示 + shortfall
 *   - 空 list 直接 hide (不佔位)
 *   - 點 row 跳轉到帳戶 tab (sync / 設定可在那裡操作)
 *
 * Sort 已由 backend 處理 (days_until_due asc, bill_due_amount desc).
 *
 * UI 鐵令 (使用者 2026-06-17): 卡片兩行, 禁 metadata/subtotals/gradient, white+4px brand strip.
 * → 紅色 (insufficient) / 黃色 (no_account) brand strip 取代 brand-500.
 */
import { useRouter } from 'expo-router';
import { Pressable, Text, View } from 'react-native';

import { BANK_LABELS, type PaymentReminder, type SupportedBank } from '@/types/api';

type Props = {
  reminders: PaymentReminder[];
};

function fmtTWD(n: number): string {
  return `NT$${Math.round(n).toLocaleString('en-US')}`;
}

function daysLabel(days: number): string {
  if (days === 0) return '今天到期';
  if (days === 1) return '明天到期';
  return `${days} 天後到期`;
}

export function PaymentRemindersCard({ reminders }: Props) {
  const router = useRouter();

  if (!reminders || reminders.length === 0) return null;

  return (
    <View className="mb-4" testID="payment-reminders-card">
      <Text className="text-ink-900 dark:text-ink-50 text-h3 mb-2">⚠️ 繳費提醒</Text>
      {reminders.map((r) => {
        const isInsufficient = r.reason === 'insufficient';
        const stripColor = isInsufficient ? 'bg-red-500' : 'bg-amber-500';
        const bankLabel = BANK_LABELS[r.card_bank as SupportedBank] ?? r.card_bank;
        const cardLabel = r.card_name ? `${bankLabel}・${r.card_name}` : bankLabel;

        return (
          <Pressable
            key={`${r.card_bank}-${r.card_no}-${r.payment_due_date}-${r.bill_due_amount}`}
            onPress={() => router.push('/(tabs)/cards')}
            className="bg-white dark:bg-ink-900 rounded-2xl shadow-card mb-2 flex-row overflow-hidden active:opacity-80"
            testID={`payment-reminder-${r.card_bank}-${r.card_no}`}
          >
            <View className={`w-1 ${stripColor}`} />
            <View className="flex-1 px-4 py-3">
              {/* Row 1: 卡名 + 到期天數 badge */}
              <View className="flex-row items-center justify-between">
                <Text
                  className="text-ink-900 dark:text-ink-50 text-body font-semibold flex-1"
                  numberOfLines={1}
                >
                  {cardLabel}
                </Text>
                <Text
                  className={`text-small font-semibold ml-2 ${
                    isInsufficient
                      ? 'text-red-600 dark:text-red-400'
                      : 'text-amber-700 dark:text-amber-500'
                  }`}
                >
                  {daysLabel(r.days_until_due)}
                </Text>
              </View>
              {/* Row 2: reason + 金額 */}
              <View className="flex-row items-center justify-between mt-1">
                <Text className="text-ink-600 dark:text-ink-400 text-small flex-1">
                  {isInsufficient
                    ? `扣繳戶餘額不足${
                        r.shortfall != null ? `（差 ${fmtTWD(r.shortfall)}）` : ''
                      }`
                    : '尚未設定自動扣繳帳號'}
                </Text>
                <Text className="text-ink-900 dark:text-ink-50 text-small font-semibold ml-2">
                  {fmtTWD(r.bill_due_amount)}
                </Text>
              </View>
            </View>
          </Pressable>
        );
      })}
    </View>
  );
}
