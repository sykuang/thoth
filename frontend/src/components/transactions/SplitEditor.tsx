/**
 * SplitEditor — 分類拆帳編輯器 (Phase 10, 2026-07-29).
 *
 * 把一筆交易拆成多個分類, 每份可獨立決定是否納入收支統計。
 *
 * 設計鐵則:
 *   - **總額不變**: 子項和必須等於母筆金額。這是「分類拆帳」的定義 —— 只改
 *     分類歸屬, 不改總支出。和對不上時 disable 儲存並顯示差額, 不讓使用者
 *     送出後才吃 backend 400。
 *   - **raw 不動**: 拆帳寫進 splits_overwrite overlay 欄, 銀行原始 amount /
 *     category 永遠保留 (使用者鐵則「修正≠刪除」)。
 *   - **每份可獨立忽略**: 例如一筆 1200 的採買, 其中 400 是幫同事代墊 →
 *     該份勾「不計入」, 統計只算 800。
 *
 * 為什麼不用 react-hook-form / formik: 這裡只有一個 array of 4 fields,
 * 且驗證邏輯 (總和) 是跨欄位的單一條件, 自己 useState 比裝 form library 短。
 */
import { useMemo } from 'react';
import { Pressable, Text, TextInput, View } from 'react-native';

import { CategoryPicker } from '@/components/CategoryPicker';
import { formatCurrency } from '@/lib/currency';
import type { DropdownOption } from '@/components/Dropdown';
import type { TransactionSplit } from '@/types/api';

/** 編輯中的子項 — amount 用 string 讓輸入框可以是空的 / 打到一半。 */
export type DraftSplit = {
  amount: string;
  category: string;
  note: string;
  auto_excluded: boolean;
};

export function emptyDraftSplit(): DraftSplit {
  return { amount: '', category: '', note: '', auto_excluded: false };
}

/** 已存在的 splits → 編輯草稿。無拆帳時回 []。 */
export function toDraftSplits(splits: TransactionSplit[] | undefined): DraftSplit[] {
  if (!splits || splits.length === 0) return [];
  return splits.map((s) => ({
    amount: String(s.amount),
    category: s.category ?? '',
    note: s.note ?? '',
    auto_excluded: s.auto_excluded === true,
  }));
}

/** 草稿 → API payload。空 amount 當 0 (由 sum 檢查擋下)。 */
export function toApiSplits(drafts: DraftSplit[]): TransactionSplit[] {
  return drafts.map((d) => ({
    amount: Number.parseInt(d.amount, 10) || 0,
    category: d.category || null,
    note: d.note.trim() || null,
    auto_excluded: d.auto_excluded,
  }));
}

/**
 * 驗證草稿, 回錯誤訊息或 null。
 * 跟 backend `_normalize_splits_input` 同一組規則 —— 這裡擋是為了即時回饋,
 * backend 那層才是真正的 trust boundary (不可只靠 client 驗)。
 */
export function validateDrafts(
  drafts: DraftSplit[],
  parentAmount: number,
): string | null {
  if (drafts.length === 0) return null; // 未拆帳
  if (drafts.length < 2) return '拆帳至少要兩份';
  for (const d of drafts) {
    const n = Number.parseInt(d.amount, 10);
    if (!Number.isFinite(n) || n <= 0) return '每份金額必須大於 0';
  }
  const total = drafts.reduce((s, d) => s + (Number.parseInt(d.amount, 10) || 0), 0);
  if (total !== parentAmount) {
    const diff = total - parentAmount;
    return diff > 0
      ? `超出 ${formatCurrency(diff, 'TWD')} — 需與原金額相同`
      : `還差 ${formatCurrency(-diff, 'TWD')} — 需與原金額相同`;
  }
  return null;
}

export type SplitEditorProps = {
  drafts: DraftSplit[];
  onChange: (next: DraftSplit[]) => void;
  /** 母筆金額 (絕對值), 子項總和必須等於它. */
  parentAmount: number;
  categoryOptions: DropdownOption[];
  categoriesLoading?: boolean;
};

export function SplitEditor({
  drafts,
  onChange,
  parentAmount,
  categoryOptions,
  categoriesLoading = false,
}: SplitEditorProps) {
  const total = useMemo(
    () => drafts.reduce((s, d) => s + (Number.parseInt(d.amount, 10) || 0), 0),
    [drafts],
  );
  const remaining = parentAmount - total;
  const error = validateDrafts(drafts, parentAmount);

  const patchAt = (i: number, patch: Partial<DraftSplit>) => {
    onChange(drafts.map((d, idx) => (idx === i ? { ...d, ...patch } : d)));
  };

  // 起手式: 兩份, 第一份帶全額讓使用者只需改一個數字 (第二份自動是剩餘)
  const startSplitting = () => {
    onChange([
      { ...emptyDraftSplit(), amount: String(parentAmount) },
      emptyDraftSplit(),
    ]);
  };

  if (drafts.length === 0) {
    return (
      <View className="mb-4">
        <Text className="text-ink-700 dark:text-ink-300 text-small font-semibold mb-2">
          拆帳
        </Text>
        <Pressable
          onPress={startSplitting}
          className="border border-dashed border-ink-300 dark:border-ink-600 rounded-xl px-3 py-3 bg-white dark:bg-ink-800 active:bg-ink-50 dark:active:bg-ink-700"
          testID="txn-detail-split-start"
        >
          <Text className="text-ink-500 dark:text-ink-400 text-body text-center">
            ✂️ 拆成多個分類
          </Text>
        </Pressable>
        <Text className="text-ink-400 dark:text-ink-500 text-micro mt-1">
          一筆交易含多種消費時使用, 每份可分別設定是否納入統計
        </Text>
      </View>
    );
  }

  return (
    <View className="mb-4">
      <View className="flex-row items-center justify-between mb-2">
        <Text className="text-ink-700 dark:text-ink-300 text-small font-semibold">
          拆帳 ({drafts.length} 份)
        </Text>
        <Pressable
          onPress={() => onChange([])}
          className="px-2 py-1 rounded-lg bg-ink-100 dark:bg-ink-800 border border-ink-200 dark:border-ink-700"
          testID="txn-detail-split-clear"
        >
          <Text className="text-ink-600 dark:text-ink-400 text-micro">↺ 取消拆帳</Text>
        </Pressable>
      </View>

      {drafts.map((d, i) => (
        <View
          key={i}
          className="border border-ink-200 dark:border-ink-700 rounded-xl p-3 mb-2 bg-white dark:bg-ink-800"
        >
          <View className="flex-row items-center justify-between mb-2">
            <Text className="text-ink-500 dark:text-ink-400 text-micro font-semibold">
              第 {i + 1} 份
            </Text>
            {drafts.length > 2 ? (
              <Pressable
                onPress={() => onChange(drafts.filter((_, idx) => idx !== i))}
                className="px-2 py-0.5"
                testID={`txn-detail-split-remove-${i}`}
              >
                <Text className="text-red-500 dark:text-red-400 text-micro">移除</Text>
              </Pressable>
            ) : null}
          </View>

          <View className="flex-row gap-2 mb-2">
            <View className="flex-1">
              <TextInput
                value={d.amount}
                onChangeText={(v) => patchAt(i, { amount: v.replace(/[^0-9]/g, '') })}
                keyboardType="number-pad"
                placeholder="金額"
                placeholderTextColor="#94a3b8"
                testID={`txn-detail-split-amount-${i}`}
                className="border border-ink-200 dark:border-ink-700 rounded-lg px-3 py-2 text-body font-mono bg-white dark:bg-ink-900 text-ink-900 dark:text-ink-50"
              />
            </View>
            {/* 補足剩餘 — 拆兩份時最常見的動作, 省去心算 */}
            {remaining !== 0 ? (
              <Pressable
                onPress={() =>
                  patchAt(i, {
                    amount: String((Number.parseInt(d.amount, 10) || 0) + remaining),
                  })
                }
                className="px-3 justify-center rounded-lg bg-ink-100 dark:bg-ink-700 active:bg-ink-200"
                testID={`txn-detail-split-fill-${i}`}
              >
                <Text className="text-ink-600 dark:text-ink-300 text-micro">
                  補足剩餘
                </Text>
              </Pressable>
            ) : null}
          </View>

          <CategoryPicker
            label=""
            value={d.category}
            onChange={(next) => patchAt(i, { category: next })}
            options={categoryOptions}
            placeholder={categoriesLoading ? '載入中…' : '請選擇分類'}
            disabled={categoriesLoading}
            testID={`txn-detail-split-category-${i}`}
            modalTitle="選擇分類"
          />

          <TextInput
            value={d.note}
            onChangeText={(v) => patchAt(i, { note: v })}
            placeholder="備註 (選填, 例如「同事代墊」)"
            placeholderTextColor="#94a3b8"
            maxLength={200}
            testID={`txn-detail-split-note-${i}`}
            className="mt-2 border border-ink-200 dark:border-ink-700 rounded-lg px-3 py-2 text-small bg-white dark:bg-ink-900 text-ink-900 dark:text-ink-50"
          />

          <Pressable
            onPress={() => patchAt(i, { auto_excluded: !d.auto_excluded })}
            className="flex-row items-center justify-between mt-2 py-1"
            testID={`txn-detail-split-ignore-${i}`}
          >
            <Text className="text-ink-600 dark:text-ink-400 text-micro flex-1 mr-2">
              這份不納入收支統計
            </Text>
            <View
              className={`w-10 h-6 rounded-full justify-center px-0.5 ${
                d.auto_excluded ? 'bg-brand-600' : 'bg-ink-300 dark:bg-ink-600'
              }`}
            >
              <View
                className={`w-5 h-5 rounded-full bg-white shadow-sm ${
                  d.auto_excluded ? 'self-end' : 'self-start'
                }`}
              />
            </View>
          </Pressable>
        </View>
      ))}

      <Pressable
        onPress={() => onChange([...drafts, emptyDraftSplit()])}
        className="border border-dashed border-ink-300 dark:border-ink-600 rounded-xl py-2.5 mb-2 active:bg-ink-50 dark:active:bg-ink-800"
        testID="txn-detail-split-add"
      >
        <Text className="text-ink-500 dark:text-ink-400 text-small text-center">
          ＋ 再加一份
        </Text>
      </Pressable>

      {/* 總和對帳 — 一眼看出還差多少, 不必等 backend 400 */}
      <View
        className={`rounded-xl px-3 py-2 ${
          error
            ? 'bg-amber-100 dark:bg-amber-950'
            : 'bg-accent-100 dark:bg-accent-950'
        }`}
      >
        <View className="flex-row items-center justify-between">
          <Text className="text-ink-600 dark:text-ink-400 text-micro">
            合計 / 原金額
          </Text>
          <Text className="text-ink-900 dark:text-ink-50 text-small font-mono font-semibold">
            {formatCurrency(total, 'TWD')} / {formatCurrency(parentAmount, 'TWD')}
          </Text>
        </View>
        {error ? (
          <Text
            className="text-amber-700 dark:text-amber-300 text-micro mt-1"
            testID="txn-detail-split-error"
          >
            {error}
          </Text>
        ) : null}
      </View>
    </View>
  );
}
