import { Text, View } from 'react-native';

import { BankBadge } from '@/components/BankBadge';
import { formatDecimal, formatDecimalFixed } from '@/lib/decimal';
import { parseDateForMobileLayout } from '@/lib/txnDisplay';
import type { BrokerageAccount, BrokerageActivity } from '@/types/api';

function money(value: string | null, currency: string | null): string {
  if (value == null) return '—';
  const formatted = currency === 'USD' ? formatDecimalFixed(value, 2) : formatDecimal(value);
  return formatted == null ? '—' : `${currency ?? ''} ${formatted}`.trim();
}

export function BrokerageTxnRow({
  activity,
  account,
  wide,
}: {
  activity: BrokerageActivity;
  account: BrokerageAccount | undefined;
  wide: boolean;
}) {
  const date = activity.trade_date ?? activity.settlement_date;
  const displayDate = date?.slice(0, 10) ?? '—';
  const dateParts = parseDateForMobileLayout(date);
  const source = account?.institution_name ?? '券商';
  const accountName = account?.number ?? account?.name ?? '—';
  const description = `${activity.type} · ${activity.symbol ?? activity.description ?? '—'}`;
  const amount = money(activity.amount, activity.currency);

  if (!wide) {
    return (
      <View className="flex-row items-stretch border-b border-ink-100 dark:border-ink-800">
        <View className="w-1 bg-brand-500" />
        <View className="w-10 ml-3 mr-2 items-start justify-center py-3">
          <Text className="text-ink-400 dark:text-ink-500 text-micro">{dateParts.month}</Text>
          <Text className="text-ink-700 dark:text-ink-300 text-large font-semibold leading-tight">
            {dateParts.day}
          </Text>
        </View>
        <View className="flex-1 mr-3 py-3">
          <View className="flex-row items-baseline justify-between">
            <Text className="text-ink-900 dark:text-ink-50 text-small flex-1 mr-2" numberOfLines={1}>
              {description}
            </Text>
            <Text className="text-base font-bold font-mono text-ink-700 dark:text-ink-300" numberOfLines={1}>
              {amount}
            </Text>
          </View>
          <View className="flex-row items-center gap-2 mt-1">
            <BankBadge bank={source} size="xs" rectangular />
            <Text className="text-ink-500 dark:text-ink-400 text-micro" numberOfLines={1}>
              {source} · {accountName}
            </Text>
          </View>
        </View>
      </View>
    );
  }

  return (
    <View className="flex-row border-b border-ink-100 dark:border-ink-800">
      <Text className="w-28 px-3 py-2 text-small text-ink-700 dark:text-ink-300">{displayDate}</Text>
      <Text className="w-20 px-3 py-2 text-small text-ink-700 dark:text-ink-300" numberOfLines={1}>
        {source}
      </Text>
      <Text className="w-32 px-3 py-2 text-small text-ink-700 dark:text-ink-300 font-mono" numberOfLines={1}>
        {accountName}
      </Text>
      <Text className="flex-1 px-3 py-2 text-small text-ink-700 dark:text-ink-300" numberOfLines={2}>
        {description}
      </Text>
      <Text className="w-20 px-3 py-2 text-micro font-semibold text-brand-600 dark:text-brand-400">投資</Text>
      <Text className="w-32 px-3 py-2 text-small text-right font-mono font-semibold text-ink-700 dark:text-ink-300">
        {amount}
      </Text>
    </View>
  );
}