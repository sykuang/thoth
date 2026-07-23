/**
 * Transaction row — renders one row of the transactions list (mobile + desktop).
 *
 * Extracted from app/(tabs)/transactions.tsx (W Phase 17, 2026-06-17).
 * Memoized with custom areEqual: only re-renders when row shape changes,
 * not when parent callbacks change (callbacks always use latest closure).
 */
import React from 'react';
import { Pressable, Text, View } from 'react-native';

import { BankBadge } from '@/components/BankBadge';
import { renderAmount } from '@/lib/currency';
import { maskCardNo } from '@/lib/mask';
import { getDisplayDescription, parseDateForMobileLayout } from '@/lib/txnDisplay';
import {
  type CardDateBasis,
  type SupportedBank,
  type Transaction,
  BANK_LABELS,
} from '@/types/api';

export type TxnRowProps = {
  t: Transaction;
  wide: boolean;
  fxMode: import('@/types/api').FxDisplayMode;
  /** 信用卡交易日期認列 — 'consume' (預設) / 'post' (入帳日). */
  cardDateBasis?: CardDateBasis;
  onPress?: () => void;
  onLongPress?: () => void;
  selected?: boolean;
  selectionMode?: boolean;
};

export const TxnRow = React.memo(
  function TxnRowImpl({
    t,
    wide,
    fxMode,
    cardDateBasis = 'consume',
    onPress,
    onLongPress,
    selected = false,
    selectionMode = false,
  }: TxnRowProps) {
    const render = renderAmount(t, fxMode);
    // Backend already returns t.date according to cardDateBasis; keep this fallback
    // for older API payloads that may not yet have migrated.
    const isCardRow = t.kind === 'billed' || t.kind === 'pending';
    const displayDate =
      isCardRow && cardDateBasis === 'post' && t.post_date
        ? t.post_date
        : t.date;
    // Phase 6 (excluded): 該帳戶被標「不納入淨資產統計」→ 整列反灰 + 金額劃線
    // Phase 9.3 補 (2026-06-18): 該筆 auto_excluded (rule 自動排 / 使用者手動勾「忽略此筆」)
    // 也要反灰 — backend stats 用 (excluded OR auto_excluded) 兩個一起 skip,
    // UI 不能只反映其中一個讓「不納入統計」狀態看不出來.
    const isExcluded = t.excluded === true || t.auto_excluded === true;
    const amountColor = isExcluded
      ? 'text-ink-400 dark:text-ink-500 line-through'
      : render.direction === 'income'
        ? 'text-accent-600 dark:text-accent-500'
        : render.direction === 'expense'
          ? 'text-red-600 dark:text-red-400'
          : 'text-ink-500 dark:text-ink-400';

    if (!wide) {
      // === UI 鐵令 (使用者 2026-06-17) ===
      // 兩行極簡 (左日期+strip 不算 row):
      //   行 1: 分類 · 描述(粗) | 金額(粗大字)
      //   行 2: bank badge + tags | 金額副字 (FX)
      // 廢除: 分類獨佔一行、描述加 "- 帳號末四碼" 後綴
      // 左 4px brand strip (income=綠 / expense=紅 / 其他=ink), strip 取代日期左側 padding
      // 日期堆疊保留 (左, 視覺錨點), 內容雙行緊湊
      const dateParts = parseDateForMobileLayout(displayDate);
      const stripColor = isExcluded
        ? 'bg-ink-300 dark:bg-ink-700'
        : render.direction === 'income'
          ? 'bg-accent-500'
          : render.direction === 'expense'
            ? 'bg-red-500'
            : 'bg-ink-400';
      const [shown, edited] = getDisplayDescription(t);
      return (
        <Pressable
          onPress={onPress}
          onLongPress={onLongPress}
          delayLongPress={400}
          className={`flex-row items-stretch border-b border-ink-100 dark:border-ink-800 active:bg-ink-50 dark:active:bg-ink-800 ${
            isExcluded ? 'opacity-50' : ''
          } ${selected ? 'bg-brand-50 dark:bg-brand-950' : ''}`}
        >
          {/* UI 鐵令: 4px brand strip (dynamic by direction) */}
          <View className={`w-1 ${stripColor}`} />

          {/* Phase 9.2: selection mode 左側 checkbox */}
          {selectionMode && (
            <View className="w-7 ml-2 items-center justify-center">
              <View
                className={`w-5 h-5 rounded-md border-2 items-center justify-center ${
                  selected
                    ? 'bg-brand-600 border-brand-600'
                    : 'bg-white dark:bg-ink-800 border-ink-300 dark:border-ink-600'
                }`}
              >
                {selected && (
                  <Text className="text-white text-micro font-bold">✓</Text>
                )}
              </View>
            </View>
          )}

          {/* 左: 日期堆疊 (固定寬度) */}
          <View className="w-10 ml-3 mr-2 items-start justify-center py-3">
            <Text className="text-ink-400 dark:text-ink-500 text-micro">
              {dateParts.month}
            </Text>
            <Text className="text-ink-700 dark:text-ink-300 text-large font-semibold leading-tight">
              {dateParts.day}
            </Text>
          </View>

          {/* 中 + 右: 兩行 flex-1 */}
          <View className="flex-1 mr-3 py-3">
            {/* 行 1: 分類 · 描述 (左) | 金額 (右) */}
            <View className="flex-row items-baseline justify-between">
              <View className="flex-1 mr-2">
                <Text className="text-ink-900 dark:text-ink-50 text-small" numberOfLines={1}>
                  {t.category ? (
                    <Text className="text-brand-600 dark:text-brand-400 font-semibold">
                      {t.category}
                      <Text className="text-ink-400 dark:text-ink-500"> · </Text>
                    </Text>
                  ) : null}
                  {edited ? '✏️ ' : ''}{shown}
                </Text>
              </View>
              <Text className={`text-base font-bold font-mono ${amountColor}`} numberOfLines={1}>
                {render.primary}
              </Text>
            </View>
            {/* 行 2: bank badge + tags (左) | FX 副字 (右) */}
            <View className="flex-row items-center justify-between mt-1">
              <View className="flex-row items-center gap-2 flex-wrap flex-1 mr-2">
                <BankBadge bank={t.bank as SupportedBank} size="xs" rectangular />
                {(t.tags ?? []).slice(0, 3).map((tag) => (
                  <Text
                    key={tag}
                    className="text-brand-600 dark:text-brand-400 text-micro font-semibold"
                  >
                    #{tag}
                  </Text>
                ))}
                {(t.tags ?? []).length > 3 && (
                  <Text className="text-ink-500 dark:text-ink-400 text-micro">
                    +{(t.tags ?? []).length - 3}
                  </Text>
                )}
              </View>
              {render.sub && (
                <Text className="text-ink-500 dark:text-ink-400 text-micro font-mono" numberOfLines={1}>
                  {render.sub}
                </Text>
              )}
            </View>
          </View>
        </Pressable>
      );
    }

    // 桌機表格 row
    // Phase 7.5 (2026-06-15 使用者指示): 移除「類型」欄整欄 (台幣/未出帳 badge)。
    // 2026-07-08 再確認：回饋/退款/還款/手續費/年費/分期這類 txn_type badge 也不顯示；
    // 使用者已可用 category/subcategory 表達，txn_type 只留給 stats/display direction。
    return (
      <Pressable
        onPress={onPress}
        onLongPress={onLongPress}
        delayLongPress={400}
        className={`flex-row border-b border-ink-100 dark:border-ink-800 active:bg-ink-50 dark:active:bg-ink-800 ${
          isExcluded ? 'opacity-50' : ''
        } ${selected ? 'bg-brand-50 dark:bg-brand-950' : ''}`}
      >
        {selectionMode && (
          <View className="w-10 px-3 py-2 justify-center">
            <View
              className={`w-5 h-5 rounded-md border-2 items-center justify-center ${
                selected
                  ? 'bg-brand-600 border-brand-600'
                  : 'bg-white dark:bg-ink-800 border-ink-300 dark:border-ink-600'
              }`}
            >
              {selected && (
                <Text className="text-white text-micro font-bold">✓</Text>
              )}
            </View>
          </View>
        )}
        <Text className="w-28 px-3 py-2 text-small text-ink-700 dark:text-ink-300">
          {displayDate ?? '—'}
        </Text>
        <Text className="w-20 px-3 py-2 text-small text-ink-700 dark:text-ink-300">
          {BANK_LABELS[t.bank as SupportedBank] ?? t.bank}
        </Text>
        <Text className="w-32 px-3 py-2 text-small text-ink-700 dark:text-ink-300 font-mono">
          {maskCardNo(t.account_or_card)}
        </Text>
        <View className="flex-1 px-3 py-2">
          <Text className="text-small text-ink-700 dark:text-ink-300" numberOfLines={2}>
            {/* Phase 8.2: overwrite 優先, 有覆寫加 ✏️ */}
            {(() => {
              const [shown, edited] = getDisplayDescription(t);
              return edited ? `✏️ ${shown}` : shown;
            })()}
          </Text>
          {/* Phase 9 (2026-06-16): tags inline 顯示給 user 自我參考 */}
          {(t.tags ?? []).length > 0 && (
            <View className="flex-row items-center gap-1.5 flex-wrap mt-0.5">
              {(t.tags ?? []).map((tag) => (
                <Text
                  key={tag}
                  className="text-brand-600 dark:text-brand-400 text-micro font-semibold"
                >
                  #{tag}
                </Text>
              ))}
            </View>
          )}
        </View>
        <View className="w-20 px-3 py-2 items-start">
          {t.category ? (
            <Text className="text-brand-600 dark:text-brand-400 text-micro font-semibold">
              {t.category}
            </Text>
          ) : null}
        </View>
        <View className="w-32 px-3 py-2 items-end">
          <Text className={`text-small text-right font-mono font-semibold ${amountColor}`}>
            {render.primary}
          </Text>
          {render.sub && (
            <Text className="text-ink-500 dark:text-ink-400 text-micro font-mono text-right">
              {render.sub}
            </Text>
          )}
        </View>
      </Pressable>
    );
  },
  // areEqual: 只在 row-shape 變化時 re-render
  (prev, next) =>
    prev.t === next.t &&
    prev.wide === next.wide &&
    prev.fxMode === next.fxMode &&
    prev.cardDateBasis === next.cardDateBasis &&
    prev.selected === next.selected &&
    prev.selectionMode === next.selectionMode,
);
TxnRow.displayName = 'TxnRow';
