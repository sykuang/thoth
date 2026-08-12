/**
 * 信用卡帳單頁面 (Phase 9.4, 2026-06-16).
 *
 * 路徑: /(tabs)/cards/[bank]/[card_no]
 * 來源: GET /cards/{bank}/{card_no}
 *
 * 結構 (上到下):
 *   1. Header: 卡名 + 末四碼 + brand badge
 *   2. 帳單摘要區
 *      - 本期應繳    NT$ XXX  (大字)
 *      - 截止日      YYYY-MM-DD (色: 過期未繳=紅 / 近=橘 / 一般=灰 / 已繳=綠)
 *      - 狀態 badge
 *      - 注意：帳戶層 summary 欄位不顯示在卡片帳戶 header 下
 *   3. 繳款紀錄 section (最近 12 筆)
 *   4. 本期已出帳明細 section
 *   5. 未出帳明細 section
 *
 * 設計取捨 (使用者 2026-06-16 spec):
 *   - 純展示, 不做「使用者新增繳款」按鈕 (銀行已自動 import via card_billed_txns)
 *   - last_payment 來源 = 真實 payment rows + cards native latest payment 去重合併
 *   - bill_status='paid' 不顯示逾期；有帳單結帳日時，繳款必須不早於該日
 */
import { useLocalSearchParams } from 'expo-router';
import { useQuery } from '@tanstack/react-query';
import { ActivityIndicator, Text, View } from 'react-native';

import { KeyboardAwareScrollView } from '@/components/KeyboardAwareScrollView';
import { BankBadge } from '@/components/BankBadge';
import { api, formatApiError } from '@/lib/api';
import { bankMeta } from '@/lib/banks';
import { formatCurrency, formatSignedCurrency } from '@/lib/currency';
import { maskCardNo } from '@/lib/mask';
import type { CardDetail } from '@/types/api';

export default function CardDetailPage() {
  const { bank, card_no } = useLocalSearchParams<{ bank: string; card_no: string }>();
  const detailQ = useQuery({
    queryKey: ['cards', bank, card_no],
    queryFn: () => api<CardDetail>(`/cards/${bank}/${card_no}`),
    enabled: !!bank && !!card_no,
  });

  if (detailQ.isLoading) {
    return (
      <View className="flex-1 items-center justify-center bg-ink-50 dark:bg-ink-950">
        <ActivityIndicator />
      </View>
    );
  }
  if (detailQ.isError || !detailQ.data) {
    return (
      <View className="flex-1 items-center justify-center px-6 bg-ink-50 dark:bg-ink-950">
        <Text className="text-red-600 dark:text-red-400 text-body text-center">
          {formatApiError(detailQ.error) || '載入失敗'}
        </Text>
      </View>
    );
  }

  const card = detailQ.data;
  const displayName = (card.nickname_overwrite || card.name || '(未命名)').trim();
  const billDue = card.bill_due_amount ?? 0;
  const status = card.bill_status ?? 'unknown';

  return (
    <KeyboardAwareScrollView
      className="flex-1 bg-ink-50 dark:bg-ink-950"
      contentContainerStyle={{ paddingBottom: 32 }}
    >
      {/* Header */}
      <View className="bg-white dark:bg-ink-900 px-4 py-5 flex-row items-center gap-3 border-b border-ink-100 dark:border-ink-800">
        <BankBadge bank={card.bank} size="md" />
        <View className="flex-1 min-w-0">
          <Text className="text-h2 text-ink-900 dark:text-ink-50" numberOfLines={1}>
            {displayName}
          </Text>
          <Text className="text-ink-500 dark:text-ink-400 text-small font-mono" numberOfLines={1}>
            {maskCardNo(card.card_no)} · {bankMeta(card.bank).short}
          </Text>
        </View>
      </View>

      {/* 帳單摘要 */}
      <View className="bg-white dark:bg-ink-900 mt-3 px-4 py-5">
        <Text className="text-ink-500 dark:text-ink-400 text-small mb-1">
          本期應繳
        </Text>
        <Text
          className={`text-display font-mono font-semibold ${
            billDue > 0
              ? status === 'overdue'
                ? 'text-red-600 dark:text-red-400'
                : 'text-ink-900 dark:text-ink-50'
              : 'text-emerald-600 dark:text-emerald-400'
          }`}
        >
          {formatCurrency(billDue, 'TWD')}
        </Text>
        <View className="flex-row items-center gap-2 mt-2 flex-wrap">
          <StatusBadge status={status} />
          {card.payment_due_date && (
            <Text className="text-ink-500 dark:text-ink-400 text-small">
              截止 {card.payment_due_date.slice(0, 10)}
            </Text>
          )}
        </View>
      </View>

      {/* 繳款紀錄 */}
      <Section
        title="繳款紀錄"
        subtitle={card.payments.length > 0 ? `最近 ${card.payments.length} 筆` : undefined}
        empty={card.payments.length === 0 ? '沒有繳款紀錄' : undefined}
      >
        {card.payments.map((p, i) => (
          <TxnRow
            key={`pay-${i}`}
            date={p.date}
            description={p.description}
            amount={p.amount}
            isLast={i === card.payments.length - 1}
            positive
          />
        ))}
      </Section>

      {/* 本期已出帳明細 */}
      <Section
        title="本期已出帳明細"
        subtitle={card.billed_txns.length > 0 ? `${card.billed_txns.length} 筆` : undefined}
        empty={card.billed_txns.length === 0 ? '本期沒有明細' : undefined}
      >
        {card.billed_txns.map((t, i) => (
          <TxnRow
            key={`b-${i}`}
            date={t.date?.slice(0, 10) ?? ''}
            description={t.description}
            amount={t.amount}
            isLast={i === card.billed_txns.length - 1}
            meta={categoryMeta(t.category, t.subcategory)}
          />
        ))}
      </Section>

      {/* 未出帳消費 */}
      <Section
        title="未出帳消費"
        subtitle={card.pending_txns.length > 0 ? `${card.pending_txns.length} 筆` : undefined}
        empty={card.pending_txns.length === 0 ? '沒有未出帳消費' : undefined}
      >
        {card.pending_txns.map((t, i) => (
          <TxnRow
            key={`p-${i}`}
            date={t.date?.slice(0, 10) ?? ''}
            description={t.description}
            amount={t.amount}
            isLast={i === card.pending_txns.length - 1}
            meta={categoryMeta(t.category, t.subcategory)}
          />
        ))}
      </Section>
    </KeyboardAwareScrollView>
  );
}

function Section({
  title,
  subtitle,
  empty,
  children,
}: {
  title: string;
  subtitle?: string;
  empty?: string;
  children?: React.ReactNode;
}) {
  return (
    <View className="bg-white dark:bg-ink-900 mt-3">
      <View className="px-4 pt-4 pb-2 flex-row justify-between items-baseline">
        <Text className="text-ink-900 dark:text-ink-50 text-h3 font-semibold">
          {title}
        </Text>
        {subtitle && (
          <Text className="text-ink-400 dark:text-ink-500 text-micro">{subtitle}</Text>
        )}
      </View>
      {empty ? (
        <Text className="px-4 py-6 text-ink-400 dark:text-ink-500 text-small text-center">
          {empty}
        </Text>
      ) : (
        children
      )}
    </View>
  );
}

function TxnRow({
  date,
  description,
  amount,
  isLast,
  positive = false,
  meta,
}: {
  date: string;
  description: string;
  amount: number;
  isLast: boolean;
  positive?: boolean;
  meta?: string | null;
}) {
  const border = isLast ? '' : 'border-b border-ink-100 dark:border-ink-800';
  // 銀行 raw amount: 消費正數 / 繳款負數. payments section 已 abs() positive=true 強制顯示綠.
  // billed/pending 直接 follow raw sign.
  const displayedNegative = !positive && amount < 0;
  const displayAmt = Math.abs(amount);
  return (
    <View className={`px-4 py-3 flex-row items-center gap-3 ${border}`}>
      <View className="w-12">
        <Text className="text-ink-500 dark:text-ink-400 text-micro font-mono">
          {date.length >= 10 ? `${date.slice(5, 7)}/${date.slice(8, 10)}` : date}
        </Text>
      </View>
      <View className="flex-1 min-w-0">
        <Text className="text-ink-900 dark:text-ink-50 text-body" numberOfLines={1}>
          {description || '(無說明)'}
        </Text>
        {meta && (
          <Text className="text-ink-400 dark:text-ink-500 text-micro mt-0.5">
            {meta}
          </Text>
        )}
      </View>
      <Text
        className={`text-body font-mono font-semibold ${
          positive
            ? 'text-emerald-600 dark:text-emerald-400'
            : displayedNegative
              ? 'text-emerald-600 dark:text-emerald-400'
              : 'text-ink-900 dark:text-ink-50'
        }`}
      >
        {formatSignedCurrency(displayAmt, 'TWD', positive || displayedNegative)}
      </Text>
    </View>
  );
}

function StatusBadge({ status }: { status: string }) {
  const styles: Record<string, { bg: string; text: string; label: string }> = {
    paid: {
      bg: 'bg-emerald-100 dark:bg-emerald-900/40',
      text: 'text-emerald-700 dark:text-emerald-300',
      label: '✓ 已繳',
    },
    overdue: {
      bg: 'bg-red-100 dark:bg-red-900/40',
      text: 'text-red-700 dark:text-red-300',
      label: '⚠ 逾期',
    },
    due: {
      bg: 'bg-amber-100 dark:bg-amber-900/40',
      text: 'text-amber-700 dark:text-amber-300',
      label: '待繳',
    },
    no_payment_required: {
      bg: 'bg-ink-100 dark:bg-ink-800',
      text: 'text-ink-600 dark:text-ink-300',
      label: '無需繳款',
    },
    unknown: {
      bg: 'bg-ink-100 dark:bg-ink-800',
      text: 'text-ink-500 dark:text-ink-400',
      label: '狀態未知',
    },
  };
  const s = styles[status] || styles.unknown;
  return (
    <View className={`px-2 py-1 rounded-md ${s.bg}`}>
      <Text className={`text-micro font-semibold ${s.text}`}>{s.label}</Text>
    </View>
  );
}

function categoryMeta(
  category: string | null | undefined,
  subcategory: string | null | undefined,
): string | null {
  if (category && subcategory) return `${category} / ${subcategory}`;
  return category || subcategory || null;
}
