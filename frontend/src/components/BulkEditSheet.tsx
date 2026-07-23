/**
 * BulkEditSheet — 批量編輯 N 筆交易 (category / subcategory / tags).
 *
 * Phase 9.2 (2026-06-17): 跨平台 Modal:
 *   - iOS: presentationStyle="pageSheet" (從底滑入)
 *   - Web/macOS: 浮動 modal + backdrop
 *   - ScrollView 加 automaticallyAdjustKeyboardInsets (學 TagPicker)
 *
 * Three optional 編輯區:
 *   1. 主分類 Dropdown (預設「不修改」)
 *   2. 子分類 Dropdown (主分類選了才出現有效選項)
 *   3. 標籤: tags_mode (不修改 / 加入 / 覆寫) + TagPicker 入口
 *
 * 儲存 button enabled 條件: 至少改了一項 (category / subcategory / tags)
 *
 * **實作說明（TODO: 改成真 bulk endpoint）**：
 *   目前對 N 筆 targets 用 Promise.allSettled 連發 single PATCH /transactions/{bank}/{kind}/{id}。
 *   100 筆 = 100 個 HTTP request。失敗清單回傳 onSuccess 讓 caller 顯示細節。
 *   待 backend 加 POST /transactions/bulk 後一次寫入。
 */
import { useMemo, useState } from 'react';
import {
  ActivityIndicator,
  Modal,
  Platform,
  Pressable,
  ScrollView,
  Text,
  View,
} from 'react-native';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { CategoryPicker } from '@/components/CategoryPicker';
import { api, type ApiError, formatApiError } from '@/lib/api';
import { sortCategoryKeys } from '@/lib/category-color';
import { Dropdown } from './Dropdown';
import { TagPicker } from './TagPicker';

const NO_CHANGE_VALUE = '__no_change__';
const CLEAR_VALUE = '__clear__';

type TagsMode = 'no_change' | 'replace' | 'add';

export type BulkTarget = {
  bank: string;
  kind: 'twd' | 'billed' | 'pending';
  id: number;
};

type Props = {
  visible: boolean;
  targets: BulkTarget[];
  onClose: () => void;
  /**
   * 完工 callback。
   * @param updated 成功筆數
   * @param failed  失敗筆數
   * @param failedTargets 失敗的 BulkTarget 與錯誤訊息（讓 caller 可保留 selection 或顯示細節）
   */
  onSuccess: (
    updated: number,
    failed: number,
    failedTargets?: { target: BulkTarget; error: string }[],
  ) => void;
};

export function BulkEditSheet({ visible, targets, onClose, onSuccess }: Props) {
  const qc = useQueryClient();

  // 編輯 state — 預設都不修改
  const [editCat, setEditCat] = useState<string>(NO_CHANGE_VALUE);
  const [editSub, setEditSub] = useState<string>(NO_CHANGE_VALUE);
  const [tagsMode, setTagsMode] = useState<TagsMode>('no_change');
  const [editTags, setEditTags] = useState<string[]>([]);
  const [tagPickerVisible, setTagPickerVisible] = useState(false);
  const [errMsg, setErrMsg] = useState<string | null>(null);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);

  // 主分類列表。
  // /rules/categories 回 SQL 字典序；UI dropdown 要跟 filter chips / summary view 一樣用 life-first order。
  const categoriesQ = useQuery<{ categories: string[] }, ApiError>({
    queryKey: ['rules', 'categories'],
    queryFn: () => api<{ categories: string[] }>('/rules/categories'),
    enabled: visible,
    staleTime: 60_000,
  });

  // 子分類列表 (主類選了才撈)
  const subQ = useQuery<{ subcategories: string[] }, ApiError>({
    queryKey: ['rules', 'subcategories', editCat],
    queryFn: () =>
      api<{ subcategories: string[] }>(
        `/rules/subcategories?category=${encodeURIComponent(editCat)}`,
      ),
    enabled: visible && editCat !== NO_CHANGE_VALUE && editCat !== CLEAR_VALUE && !!editCat,
    staleTime: 60_000,
  });

  const catOptions = useMemo(() => {
    const cats = sortCategoryKeys(categoriesQ.data?.categories ?? []);
    return [
      { value: NO_CHANGE_VALUE, label: '— 不修改 —' },
      { value: CLEAR_VALUE, label: '— 清空 —' },
      ...cats.map((c) => ({ value: c, label: c })),
    ];
  }, [categoriesQ.data]);

  const subOptions = useMemo(() => {
    const subs = subQ.data?.subcategories ?? [];
    return [
      { value: NO_CHANGE_VALUE, label: '— 不修改 —' },
      { value: CLEAR_VALUE, label: '— 清空 —' },
      ...subs.map((s) => ({ value: s, label: s })),
    ];
  }, [subQ.data]);

  const tagsModeOptions = [
    { value: 'no_change' as const, label: '不修改' },
    { value: 'add' as const, label: '加入 (append + dedup)' },
    { value: 'replace' as const, label: '覆寫 (整個換掉)' },
  ];

  const hasCatChange = editCat !== NO_CHANGE_VALUE;
  const hasSubChange = editSub !== NO_CHANGE_VALUE;
  const hasTagChange = tagsMode !== 'no_change';
  const hasAnyChange = hasCatChange || hasSubChange || hasTagChange;

  const bulkMut = useMutation<
    { updated: number; failed: number; failedTargets: { target: BulkTarget; error: string }[] },
    ApiError,
    void
  >({
    mutationFn: async () => {
      const patch: Record<string, unknown> = {};
      if (hasCatChange) {
        patch.category = editCat === CLEAR_VALUE ? '' : editCat;
      }
      if (hasSubChange) {
        patch.subcategory = editSub === CLEAR_VALUE ? '' : editSub;
      }
      if (hasTagChange) {
        patch.tags = editTags;
        patch.tags_mode = tagsMode;
      }
      // 對 N 筆連發 single PATCH (server 已支援 tags_mode=replace|add).
      // Promise.allSettled 收集每筆結果, 不會因為某筆 fail 就停。
      setProgress({ done: 0, total: targets.length });
      let done = 0;
      const results = await Promise.allSettled(
        targets.map(async (t) => {
          const r = await api(
            `/transactions/${t.bank}/${t.kind}/${t.id}`,
            { method: 'PATCH', body: patch },
          );
          done += 1;
          setProgress({ done, total: targets.length });
          return r;
        }),
      );
      const updated = results.filter((r) => r.status === 'fulfilled').length;
      const failed = results.length - updated;
      // 收集失敗 target 與訊息，回給 caller 顯示細節
      const failedTargets: { target: BulkTarget; error: string }[] = [];
      results.forEach((r, idx) => {
        if (r.status === 'rejected') {
          const reason = r.reason;
          const msg =
            reason instanceof Error
              ? reason.message
              : typeof reason === 'string'
                ? reason
                : '未知錯誤';
          failedTargets.push({ target: targets[idx], error: msg });
        }
      });
      return { updated, failed, failedTargets };
    },
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ['transactions'] });
      qc.invalidateQueries({ queryKey: ['frontend-dataset'] });
      qc.invalidateQueries({ queryKey: ['transactions', 'tags', 'popular'] });
      setProgress(null);
      onSuccess(res.updated, res.failed, res.failedTargets);
      setEditCat(NO_CHANGE_VALUE);
      setEditSub(NO_CHANGE_VALUE);
      setTagsMode('no_change');
      setEditTags([]);
      setErrMsg(null);
    },
    onError: (e) => {
      setProgress(null);
      setErrMsg(formatApiError(e));
    },
  });

  function handleClose() {
    // 沒在 submit 時才允許關閉
    if (bulkMut.isPending) return;
    setEditCat(NO_CHANGE_VALUE);
    setEditSub(NO_CHANGE_VALUE);
    setTagsMode('no_change');
    setEditTags([]);
    setErrMsg(null);
    onClose();
  }

  const body = (
    <>
      {/* Header */}
      <View className="flex-row items-center justify-between mb-4">
        <Pressable onPress={handleClose} disabled={bulkMut.isPending} className="py-2 -ml-2 px-2">
          <Text className={`text-body ${bulkMut.isPending ? 'text-ink-400' : 'text-brand-600 dark:text-brand-400'}`}>
            取消
          </Text>
        </Pressable>
        <Text className="text-ink-900 dark:text-ink-50 text-h3 font-semibold">
          批量編輯 {targets.length} 筆
        </Text>
        <Pressable
          onPress={() => bulkMut.mutate()}
          disabled={!hasAnyChange || bulkMut.isPending}
          className="py-2 -mr-2 px-2"
        >
          {bulkMut.isPending ? (
            <ActivityIndicator size="small" color="#0d9488" />
          ) : (
            <Text className={`text-body font-semibold ${
              !hasAnyChange ? 'text-ink-300 dark:text-ink-600' : 'text-brand-600 dark:text-brand-400'
            }`}>
              儲存
            </Text>
          )}
        </Pressable>
      </View>

      {progress && (
        <View className="bg-brand-50 dark:bg-brand-950 rounded-xl p-3 mb-3">
          <Text className="text-brand-700 dark:text-brand-300 text-small font-semibold mb-1.5">
            處理中… {progress.done} / {progress.total}
          </Text>
          <View className="h-1.5 bg-brand-100 dark:bg-brand-900 rounded-full overflow-hidden">
            <View
              className="h-full bg-brand-500"
              style={{ width: `${(progress.done / progress.total) * 100}%` }}
            />
          </View>
        </View>
      )}

      {errMsg && (
        <View className="bg-red-100 dark:bg-red-950 rounded-xl p-3 mb-3">
          <Text className="text-red-700 dark:text-red-300 text-small">{errMsg}</Text>
        </View>
      )}

      {/* 主分類 */}
      <View className="mb-3">
        <CategoryPicker
          label="主分類"
          value={editCat}
          onChange={(v) => {
            setEditCat(v);
            // 切主類時 reset 子分類
            setEditSub(NO_CHANGE_VALUE);
          }}
          options={catOptions}
          placeholder="— 不修改 —"
          modalTitle="選擇分類"
        />
      </View>

      {/* 子分類 — 主類選了「真分類」才出現有效選項 */}
      {hasCatChange && editCat !== CLEAR_VALUE && (
        <View className="mb-3">
          <Dropdown
            label="子分類"
            value={editSub}
            onChange={setEditSub}
            options={subOptions}
            placeholder="— 不修改 —"
          />
        </View>
      )}

      {/* 標籤 mode */}
      <View className="mb-3">
        <Dropdown
          label="標籤"
          value={tagsMode}
          onChange={(v) => setTagsMode(v as TagsMode)}
          options={tagsModeOptions}
          placeholder="不修改"
        />
      </View>

      {/* 標籤 picker 入口 (mode 不是 no_change 才顯示) */}
      {hasTagChange && (
        <View className="mb-3">
          <Pressable
            onPress={() => setTagPickerVisible(true)}
            className="border border-ink-200 dark:border-ink-700 rounded-xl px-3 py-2.5 bg-white dark:bg-ink-800 active:bg-ink-50 dark:active:bg-ink-700"
          >
            {editTags.length === 0 ? (
              <Text className="text-ink-400 dark:text-ink-500 text-body">
                ＋ {tagsMode === 'replace' ? '選擇覆寫的標籤 (空 = 清空)' : '選擇要加入的標籤'}
              </Text>
            ) : (
              <View className="flex-row items-center justify-between">
                <View className="flex-row flex-wrap gap-1.5 flex-1 mr-2">
                  {editTags.map((tag) => (
                    <View
                      key={tag}
                      className="bg-brand-100 dark:bg-brand-950 rounded-full px-2.5 py-0.5"
                    >
                      <Text className="text-brand-700 dark:text-brand-300 text-micro font-semibold">
                        #{tag}
                      </Text>
                    </View>
                  ))}
                </View>
                <Text className="text-brand-600 dark:text-brand-400 text-small">
                  ✏️
                </Text>
              </View>
            )}
          </Pressable>
          <Text className="text-ink-400 dark:text-ink-500 text-micro mt-1">
            {tagsMode === 'replace'
              ? '所有選中交易的標籤都會被換成這組 (空 = 清空)'
              : '把這些標籤加到所有選中交易 (已存在的會 skip)'}
          </Text>
        </View>
      )}

      {/* 提示 */}
      <Text className="text-ink-400 dark:text-ink-500 text-micro mt-2">
        儲存後會更新 {targets.length} 筆交易; 沒勾的欄位完全不動 (raw 永遠不改)。
      </Text>

      {/* nested TagPicker — visible 控制 */}
      <TagPicker
        visible={tagPickerVisible}
        value={editTags}
        onChange={setEditTags}
        onClose={() => setTagPickerVisible(false)}
      />
    </>
  );

  if (Platform.OS === 'ios') {
    return (
      <Modal
        visible={visible}
        animationType="slide"
        presentationStyle="pageSheet"
        onRequestClose={handleClose}
      >
        <View className="flex-1 bg-white dark:bg-ink-900">
          <ScrollView
            className="flex-1 px-5 pt-4"
            keyboardShouldPersistTaps="handled"
            showsVerticalScrollIndicator={false}
            contentContainerStyle={{ paddingBottom: 32 }}
            automaticallyAdjustKeyboardInsets
          >
            {body}
          </ScrollView>
        </View>
      </Modal>
    );
  }

  return (
    <Modal visible={visible} transparent animationType="fade" onRequestClose={handleClose}>
      <Pressable
        className="flex-1 items-center justify-center bg-black/50 px-4"
        onPress={handleClose}
      >
        <Pressable
          className="bg-white dark:bg-ink-900 rounded-2xl w-full max-w-[480px] shadow-card max-h-[90%]"
          onPress={(e) => e.stopPropagation()}
        >
          <ScrollView
            className="p-5"
            keyboardShouldPersistTaps="handled"
            showsVerticalScrollIndicator={false}
          >
            {body}
          </ScrollView>
        </Pressable>
      </Pressable>
    </Modal>
  );
}
