// MonthCarousel — 從 transactions.tsx 拆出的 period 切換 UI。
// 為什麼拆: 96 行獨立元件、無 transactions internal state 耦合，純 prop-driven，
// 移出後 transactions.tsx 變短 ~96 行，且未來 reports/dashboard 可重用。

import { View, Text, Pressable } from 'react-native';
import {
  Granularity,
  currentPeriodKey,
  shiftPeriod,
  periodDisplayLabel,
} from '../../lib/period';

export interface MonthCarouselProps {
  granularity: Granularity;
  selectedPeriod: string;
  onGranularityChange: (g: Granularity) => void;
  onPeriodChange: (p: string) => void;
  /** stats for current period — 含 count 顯示在副字; 上游負責跨 granularity 計算 */
  monthStat: { income: number; expense: number; net: number; count: number } | null;
}

export function MonthCarousel({
  granularity,
  selectedPeriod,
  onGranularityChange,
  onPeriodChange,
  monthStat,
}: MonthCarouselProps) {
  // 「下一期」是否超過當下 (不能跳到未來)
  const currentKey = currentPeriodKey(granularity);
  const isAtCurrent = selectedPeriod >= currentKey;

  // granularity label 給按鈕用
  const granularityOptions: { value: Granularity; label: string }[] = [
    { value: 'day', label: '日' },
    { value: 'month', label: '月' },
    { value: 'year', label: '年' },
  ];

  return (
    <View className="bg-white dark:bg-ink-900 rounded-2xl shadow-card mb-4 overflow-hidden" testID="period-carousel">
      {/* 日 / 月 / 年 segmented control */}
      <View className="flex-row p-1.5 gap-1 border-b border-ink-100 dark:border-ink-800 bg-ink-50 dark:bg-ink-950">
        {granularityOptions.map((opt) => {
          const sel = granularity === opt.value;
          return (
            <Pressable
              key={opt.value}
              onPress={() => onGranularityChange(opt.value)}
              className={`flex-1 py-1.5 rounded-lg items-center ${
                sel ? 'bg-white dark:bg-ink-700 shadow-card' : ''
              }`}
              testID={`granularity-${opt.value}`}
            >
              <Text
                className={`text-small font-semibold ${
                  sel ? 'text-ink-900 dark:text-ink-50' : 'text-ink-500 dark:text-ink-400'
                }`}
              >
                {opt.label}
              </Text>
            </Pressable>
          );
        })}
      </View>

      {/* Period carousel: 左箭頭 + 中央 label + 右箭頭 */}
      <View className="flex-row items-stretch">
        <Pressable
          onPress={() => onPeriodChange(shiftPeriod(granularity, selectedPeriod, -1))}
          className="px-4 items-center justify-center active:bg-ink-50 dark:active:bg-ink-800"
          testID="period-prev"
          accessibilityLabel="上一期"
        >
          <Text className="text-ink-500 dark:text-ink-400 text-h2">‹</Text>
        </Pressable>

        <View className="flex-1 py-3 px-2 items-center">
          <Text className="text-ink-900 dark:text-ink-50 text-h3 font-semibold mb-1">
            {periodDisplayLabel(granularity, selectedPeriod)}
          </Text>
          {monthStat ? (
            <Text className="text-ink-400 dark:text-ink-500 text-micro">
              {monthStat.count} 筆交易
            </Text>
          ) : (
            <Text className="text-ink-400 dark:text-ink-500 text-micro">
              此期間無交易
            </Text>
          )}
        </View>

        <Pressable
          onPress={() => {
            const next = shiftPeriod(granularity, selectedPeriod, 1);
            if (next > currentKey) return;     // 不超過當下
            onPeriodChange(next);
          }}
          disabled={isAtCurrent}
          className={`px-4 items-center justify-center active:bg-ink-50 dark:active:bg-ink-800 ${
            isAtCurrent ? 'opacity-30' : ''
          }`}
          testID="period-next"
          accessibilityLabel="下一期"
        >
          <Text className="text-ink-500 dark:text-ink-400 text-h2">›</Text>
        </Pressable>
      </View>
    </View>
  );
}
