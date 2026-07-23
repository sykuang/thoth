/**
 * 設定 → 自動分類規則。
 *
 * Backend:
 *   GET    /rules
 *   POST   /rules
 *   PUT    /rules/{rule_id}
 *   DELETE /rules/{rule_id}
 *   POST   /rules/preview
 *   POST   /rules/recategorize
 *   GET    /rules/categories?include_all=true
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Stack } from 'expo-router';
import { useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  Modal,
  Platform,
  Pressable,
  ScrollView,
  Switch,
  Text,
  TextInput,
  View,
} from 'react-native';
import { KeyboardAwareScrollView } from '@/components/KeyboardAwareScrollView';

import { api, ApiError, formatApiError } from '@/lib/api';
import { sortCategoryKeys } from '@/lib/category-color';
import type { CategoryRule, RecategorizeResult } from '@/types/api';

type FormState = {
  name: string;
  pattern: string;
  category: string;
  /** Phase 8.1 (2026-06-15): 子分類, 留空 = 整主類 match. */
  subcategory: string;
  priority: string;
  /** Phase 8.3 (2026-06-15): 命中此 rule 的 txn 在 stats 自動 skip 收支桶 */
  auto_excluded: boolean;
};

const EMPTY_FORM: FormState = {
  name: '',
  pattern: '',
  category: '',
  subcategory: '',  // Phase 8.1: 留空 = 無子分類
  priority: '',  // Phase 8 (2026-06-15 使用者指示): 留空 → submit 時 default 100
  auto_excluded: false,  // Phase 8.3
};

export default function CategoriesScreen() {
  const qc = useQueryClient();
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [previewTexts, setPreviewTexts] = useState('');
  const [previewResult, setPreviewResult] = useState<number[] | null>(null);
  const [status, setStatus] = useState<{ kind: 'ok' | 'err'; msg: string } | null>(null);
  // Phase 8 (2026-06-15 使用者指示): inline edit modal
  const [editingRule, setEditingRule] = useState<CategoryRule | null>(null);
  const [editForm, setEditForm] = useState<FormState>(EMPTY_FORM);
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [showPreview, setShowPreview] = useState(false);

  const rulesQ = useQuery<CategoryRule[]>({
    queryKey: ['rules'],
    queryFn: () => api<CategoryRule[]>('/rules'),
  });
  const categoriesQ = useQuery<{ categories: string[] }>({
    queryKey: ['rules', 'categories', 'manage'],
    queryFn: () => api<{ categories: string[] }>('/rules/categories?include_all=true'),
  });

  const allCategoryOptions = sortCategoryKeys(categoriesQ.data?.categories ?? []);

  const createMut = useMutation<CategoryRule, ApiError, FormState>({
    mutationFn: (body) =>
      api<CategoryRule>('/rules', {
        method: 'POST',
        body: {
          name: body.name,
          pattern: body.pattern,
          category: body.category,
          subcategory: body.subcategory.trim() || null,
          priority: parseInt(body.priority, 10) || 100,
          auto_excluded: body.auto_excluded,  // Phase 8.3
        },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['rules'] });
      setForm(EMPTY_FORM);
      setStatus({ kind: 'ok', msg: '規則已新增' });
    },
    onError: (e) => setStatus({ kind: 'err', msg: formatApiError(e) }),
  });

  const deleteMut = useMutation<void, ApiError, number>({
    mutationFn: (rule_id) => api<void>(`/rules/${rule_id}`, { method: 'DELETE' }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['rules'] });
      setStatus({ kind: 'ok', msg: '已刪除' });
    },
    onError: (e) => setStatus({ kind: 'err', msg: formatApiError(e) }),
  });

  const toggleMut = useMutation<CategoryRule, ApiError, { id: number; enabled: boolean }>({
    mutationFn: ({ id, enabled }) =>
      api<CategoryRule>(`/rules/${id}`, { method: 'PUT', body: { enabled } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['rules'] }),
    onError: (e) => setStatus({ kind: 'err', msg: formatApiError(e) }),
  });

  // Phase 8 (2026-06-15): inline 完整 PUT (改 name/pattern/category/subcategory/priority)
  const updateMut = useMutation<CategoryRule, ApiError, { id: number; body: FormState }>({
    mutationFn: ({ id, body }) =>
      api<CategoryRule>(`/rules/${id}`, {
        method: 'PUT',
        body: {
          name: body.name,
          pattern: body.pattern,
          category: body.category,
          subcategory: body.subcategory.trim(),  // '' → backend 視為「清掉」
          priority: parseInt(body.priority, 10) || 100,
          auto_excluded: body.auto_excluded,  // Phase 8.3
        },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['rules'] });
      setEditingRule(null);
      setStatus({ kind: 'ok', msg: '規則已更新' });
    },
    onError: (e) => setStatus({ kind: 'err', msg: formatApiError(e) }),
  });

  // Phase 8 (2026-06-15): 一鍵恢復 — 砍掉重塞 DEFAULT_RULES
  const resetMut = useMutation<{ deleted: number; added: number }, ApiError, void>({
    mutationFn: () => api<{ deleted: number; added: number }>('/rules/reset', { method: 'POST' }),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ['rules'] });
      setStatus({
        kind: 'ok',
        msg: `已恢復預設：刪 ${data.deleted} 條 / 加 ${data.added} 條`,
      });
    },
    onError: (e) => setStatus({ kind: 'err', msg: formatApiError(e) }),
  });

  // 二次確認包 (web 用 confirm, native 用 Alert) — 防使用者手滑
  function confirmReset() {
    const msg = '此動作會砍掉所有自訂規則重塞預設, 無法復原, 確定要繼續？';
    if (Platform.OS === 'web') {
       
      if (window.confirm(msg)) resetMut.mutate();
    } else {
      Alert.alert('恢復預設規則', msg, [
        { text: '取消', style: 'cancel' },
        { text: '確定', style: 'destructive', onPress: () => resetMut.mutate() },
      ]);
    }
  }

  // Phase 8: 開編輯 modal — 把 rule 內容填進 editForm
  function openEdit(r: CategoryRule) {
    setEditForm({
      name: r.name,
      pattern: r.pattern,
      category: r.category,
      subcategory: r.subcategory ?? '',
      priority: String(r.priority),
      auto_excluded: r.auto_excluded === 1,  // Phase 8.3
    });
    setEditingRule(r);
  }

  const previewMut = useMutation<
    { matched_indices: number[] },
    ApiError,
    { pattern: string; sample_texts: string[] }
  >({
    mutationFn: (body) => api('/rules/preview', { method: 'POST', body }),
    onSuccess: (data) => {
      setPreviewResult(data.matched_indices);
      setStatus({ kind: 'ok', msg: `預覽：${data.matched_indices.length} 筆 match` });
    },
    onError: (e) => setStatus({ kind: 'err', msg: formatApiError(e) }),
  });

  const recategorizeMut = useMutation<RecategorizeResult, ApiError, void>({
    mutationFn: () => api<RecategorizeResult>('/rules/recategorize', { method: 'POST' }),
    onSuccess: (data) => {
      // Phase C-fe Warning #1 (2026-06-17): backend rewrite 所有 txn category,
      // 必須 invalidate transactions + portfolio summary (current_month_spending
      // 按 category 算), 不然 user 切回 transactions tab 在 staleTime 30s 內看到舊 category。
      qc.invalidateQueries({ queryKey: ['transactions'] });
      qc.invalidateQueries({ queryKey: ['frontend-dataset'] });
      qc.invalidateQueries({ queryKey: ['portfolio', 'summary'] });
      setStatus({
        kind: 'ok',
        msg: `重新分類完成：${data.updated}/${data.total_rows} 筆更新`,
      });
    },
    onError: (e) => setStatus({ kind: 'err', msg: formatApiError(e) }),
  });

  function runPreview() {
    const lines = previewTexts
      .split('\n')
      .map((l) => l.trim())
      .filter((l) => l.length > 0);
    if (!form.pattern || lines.length === 0) {
      setStatus({ kind: 'err', msg: '請填 pattern 並輸入至少一行範例文字' });
      return;
    }
    previewMut.mutate({ pattern: form.pattern, sample_texts: lines });
  }

  const inputBase =
    'border border-ink-200 dark:border-ink-700 rounded-xl px-3 py-2.5 text-body bg-white dark:bg-ink-800 text-ink-900 dark:text-ink-50';

  // editRule modal body — iOS pageSheet / web 浮動 兩 branch 共用
  const editRuleBody = (
    <View className="gap-3">
      <TextInput
        className={inputBase}
        placeholder="名稱"
        placeholderTextColor="#94a3b8"
        value={editForm.name}
        onChangeText={(t) => setEditForm({ ...editForm, name: t })}
      />
      <TextInput
        className={inputBase}
        placeholder="Regex pattern"
        placeholderTextColor="#94a3b8"
        value={editForm.pattern}
        onChangeText={(t) => setEditForm({ ...editForm, pattern: t })}
        autoCapitalize="none"
        autoCorrect={false}
      />
      <TextInput
        className={inputBase}
        placeholder="分類 (13 主類 / 5 收入類 / 轉帳 / 還款)"
        placeholderTextColor="#94a3b8"
        value={editForm.category}
        onChangeText={(t) => setEditForm({ ...editForm, category: t })}
      />
      <View className="flex-row flex-wrap gap-1">
        {allCategoryOptions.map((cat) => (
          <Pressable
            key={cat}
            onPress={() => setEditForm({ ...editForm, category: cat })}
            className={`px-2 py-1 rounded ${
              editForm.category === cat
                ? 'bg-brand-600'
                : 'bg-ink-100 dark:bg-ink-800'
            }`}
          >
            <Text
              className={`text-micro ${
                editForm.category === cat
                  ? 'text-white'
                  : 'text-ink-700 dark:text-ink-300'
              }`}
            >
              {cat}
            </Text>
          </Pressable>
        ))}
      </View>
      <View>
        <Text className="text-ink-700 dark:text-ink-300 text-small font-semibold mb-2">
          子分類 (選填，留空 = 整主類 match)
        </Text>
        {editForm.category ? (
          <CategorySubChips
            category={editForm.category}
            selected={editForm.subcategory}
            onSelect={(sub) => setEditForm({ ...editForm, subcategory: sub })}
          />
        ) : null}
        <TextInput
          className={inputBase}
          placeholder="自訂子分類（或留空清掉）"
          placeholderTextColor="#94a3b8"
          value={editForm.subcategory}
          onChangeText={(t) => setEditForm({ ...editForm, subcategory: t })}
        />
      </View>
      <TextInput
        className={inputBase}
        placeholder="優先級 (預設 100, 數字越大越優先)"
        placeholderTextColor="#94a3b8"
        value={editForm.priority}
        onChangeText={(t) => setEditForm({ ...editForm, priority: t })}
        keyboardType="number-pad"
      />
      <Pressable
        className="flex-row items-center justify-between py-2 px-3 rounded-xl border border-ink-200 dark:border-ink-700 bg-ink-50 dark:bg-ink-800"
        onPress={() =>
          setEditForm({ ...editForm, auto_excluded: !editForm.auto_excluded })
        }
      >
        <View className="flex-1">
          <Text className="text-ink-900 dark:text-ink-50 text-small font-medium">
            🚫 不算收支
          </Text>
          <Text className="text-ink-500 dark:text-ink-400 text-micro mt-0.5">
            命中此 rule 的交易自動排除 (還款/轉帳/退款/回饋等)
          </Text>
        </View>
        <Switch
          value={editForm.auto_excluded}
          onValueChange={(v) => setEditForm({ ...editForm, auto_excluded: v })}
        />
      </Pressable>
      <View className="flex-row gap-2 mt-2">
        <Pressable
          className="flex-1 bg-ink-200 dark:bg-ink-700 rounded-xl py-3 items-center"
          onPress={() => setEditingRule(null)}
        >
          <Text className="text-ink-800 dark:text-ink-100 text-h3">取消</Text>
        </Pressable>
        <Pressable
          className={`flex-1 bg-brand-600 active:bg-brand-700 rounded-xl py-3 items-center ${
            updateMut.isPending || !editForm.name || !editForm.pattern || !editForm.category
              ? 'opacity-50'
              : ''
          }`}
          onPress={() => {
            if (!editingRule) return;
            updateMut.mutate({ id: editingRule.id, body: editForm });
          }}
          disabled={
            updateMut.isPending ||
            !editForm.name ||
            !editForm.pattern ||
            !editForm.category
          }
        >
          <Text className="text-white text-h3">
            {updateMut.isPending ? '儲存中…' : '儲存'}
          </Text>
        </Pressable>
      </View>
    </View>
  );


  return (
    <KeyboardAwareScrollView className="flex-1 bg-ink-50 dark:bg-ink-950">
      <Stack.Screen options={{ title: '自動分類規則' }} />
      <View className="px-6 py-6 max-w-[1024px] w-full mx-auto">
        {/* Status bar */}
        {status && (
          <View
            className={`mb-4 rounded-xl px-3 py-2.5 border ${
              status.kind === 'ok'
                ? 'bg-accent-500/10 dark:bg-accent-500/20 border-accent-500/30'
                : 'bg-red-50 dark:bg-red-950 border-red-200 dark:border-red-900'
            }`}
          >
            <Text
              className={`text-small ${
                status.kind === 'ok'
                  ? 'text-accent-600 dark:text-accent-500'
                  : 'text-red-700 dark:text-red-300'
              }`}
            >
              {status.msg}
            </Text>
          </View>
        )}

        <Pressable
          testID="rules-create-toggle"
          accessibilityRole="button"
          accessibilityLabel="新增自動分類規則"
          accessibilityState={{ expanded: showCreateForm }}
          className="bg-brand-600 active:bg-brand-700 rounded-xl py-3.5 px-4 mb-4 flex-row items-center justify-between"
          onPress={() => setShowCreateForm((visible) => !visible)}
        >
          <Text className="text-white text-h3">＋ 新增規則</Text>
          <Text className="text-white text-h3">{showCreateForm ? '⌃' : '⌄'}</Text>
        </Pressable>
        {showCreateForm ? (
        <View className="bg-white dark:bg-ink-900 rounded-2xl p-5 shadow-card mb-4">
          <Text className="text-ink-900 dark:text-ink-50 text-h2 mb-3">新增規則</Text>
          <View className="gap-3">
            <TextInput
              className={inputBase}
              placeholder="名稱（如 transit）"
              placeholderTextColor="#94a3b8"
              value={form.name}
              onChangeText={(t) => setForm({ ...form, name: t })}
            />
            <TextInput
              className={inputBase}
              placeholder="Regex pattern（如 北捷|台鐵|高鐵）"
              placeholderTextColor="#94a3b8"
              value={form.pattern}
              onChangeText={(t) => setForm({ ...form, pattern: t })}
              autoCapitalize="none"
              autoCorrect={false}
            />
            {/* Phase 8 (2026-06-15 使用者指示): 分類改 dynamic chip pick + 自訂 input */}
            <View>
              <Text className="text-ink-700 dark:text-ink-300 text-small font-semibold mb-2">
                分類 (從現有分類選, 或輸入自訂)
              </Text>
              <View className="flex-row flex-wrap gap-1 mb-2">
                {allCategoryOptions.map((cat) => (
                  <Pressable
                    key={cat}
                    onPress={() => setForm({ ...form, category: cat })}
                    className={`px-2 py-1 rounded ${
                      form.category === cat
                        ? 'bg-brand-600'
                        : 'bg-ink-100 dark:bg-ink-800'
                    }`}
                  >
                    <Text
                      className={`text-micro ${
                        form.category === cat
                          ? 'text-white'
                          : 'text-ink-700 dark:text-ink-300'
                      }`}
                    >
                      {cat}
                    </Text>
                  </Pressable>
                ))}
              </View>
              <TextInput
                className={inputBase}
                placeholder="或直接輸入自訂分類名 (例如：寵物、孝親)"
                placeholderTextColor="#94a3b8"
                value={form.category}
                onChangeText={(t) => setForm({ ...form, category: t })}
              />
            </View>
            {/* Phase 8.1 (2026-06-15): 子分類 — 可選, 留空 = 整主類 match */}
            <View>
              <Text className="text-ink-700 dark:text-ink-300 text-small font-semibold mb-2">
                子分類 (選填，例如「飲食」下分「早餐/午餐/咖啡」)
              </Text>
              {form.category ? (
                <CategorySubChips
                  category={form.category}
                  selected={form.subcategory}
                  onSelect={(sub) => setForm({ ...form, subcategory: sub })}
                />
              ) : (
                <Text className="text-ink-500 text-micro mb-2">
                  先選主分類, 再看可用子分類
                </Text>
              )}
              <TextInput
                className={inputBase}
                placeholder="或輸入自訂子分類 (留空 = 整主類 match)"
                placeholderTextColor="#94a3b8"
                value={form.subcategory}
                onChangeText={(t) => setForm({ ...form, subcategory: t })}
              />
            </View>
            <TextInput
              className={inputBase}
              placeholder="優先級 (留空預設 100，數字越大越優先)"
              placeholderTextColor="#94a3b8"
              value={form.priority}
              onChangeText={(t) => setForm({ ...form, priority: t })}
              keyboardType="number-pad"
            />
            {/* Phase 8.3: auto_excluded toggle */}
            <Pressable
              className="flex-row items-center justify-between py-2 px-3 rounded-xl border border-ink-200 dark:border-ink-700 bg-ink-50 dark:bg-ink-800"
              onPress={() => setForm({ ...form, auto_excluded: !form.auto_excluded })}
            >
              <View className="flex-1">
                <Text className="text-ink-900 dark:text-ink-50 text-small font-medium">
                  🚫 不算收支
                </Text>
                <Text className="text-ink-500 dark:text-ink-400 text-micro mt-0.5">
                  命中此 rule 的交易自動排除 (還款/轉帳/退款/回饋等)
                </Text>
              </View>
              <Switch
                value={form.auto_excluded}
                onValueChange={(v) => setForm({ ...form, auto_excluded: v })}
              />
            </Pressable>
            <Pressable
              className={`bg-brand-600 active:bg-brand-700 rounded-xl py-3 items-center shadow-brand ${
                createMut.isPending || !form.name || !form.pattern || !form.category ? 'opacity-50' : ''
              }`}
              onPress={() => createMut.mutate(form)}
              disabled={createMut.isPending || !form.name || !form.pattern || !form.category}
            >
              <Text className="text-white text-h3">新增</Text>
            </Pressable>
          </View>
        </View>
        ) : null}

        {/* Rules list */}
        <View className="bg-white dark:bg-ink-900 rounded-2xl p-5 shadow-card mb-4">
          <Text className="text-ink-900 dark:text-ink-50 text-h2 mb-3">規則清單</Text>
          {rulesQ.isLoading && <ActivityIndicator />}
          {rulesQ.error && (
            <Text className="text-red-600 dark:text-red-400 text-small">
              {formatApiError(rulesQ.error)}
            </Text>
          )}
          {rulesQ.data?.length === 0 && (
            <Text className="text-ink-400 dark:text-ink-500 text-small text-center py-4">尚無規則</Text>
          )}
          {rulesQ.data?.map((r) => (
            <Pressable
              key={r.id}
              onPress={() => openEdit(r)}
              className="flex-row items-center justify-between py-3 border-b border-ink-100 dark:border-ink-800 last:border-b-0 active:bg-ink-50 dark:active:bg-ink-800"
            >
              <View className="flex-1">
                <Text className="text-ink-900 dark:text-ink-50 text-h3 mb-1">
                  {r.name} → {r.category}{' '}
                  <Text className="text-ink-400 dark:text-ink-500 text-small font-normal">
                    (優先序 {r.priority})
                  </Text>
                  {/* Phase 8.3: auto_excluded 徽章 */}
                  {r.auto_excluded === 1 && (
                    <Text className="text-amber-700 dark:text-amber-400 text-micro font-semibold">
                      {' '}🚫不算收支
                    </Text>
                  )}
                </Text>
                <Text className="text-ink-500 dark:text-ink-400 text-micro font-mono">{r.pattern}</Text>
              </View>
              <View className="flex-row items-center gap-2">
                {/* Phase 8: 編輯按鈕 (除了整 row click 也可這裡) */}
                <View className="px-3 py-1.5 rounded-lg bg-brand-50 dark:bg-brand-950 border border-brand-200 dark:border-brand-900">
                  <Text className="text-brand-600 dark:text-brand-400 text-micro font-semibold">
                    ✎ 編輯
                  </Text>
                </View>
                <Switch
                  value={r.enabled === 1 || (r.enabled as unknown as boolean) === true}
                  onValueChange={(v) => toggleMut.mutate({ id: r.id, enabled: v })}
                />
                <Pressable
                  onPress={(e) => {
                    e.stopPropagation();  // 避免 row click 開 modal
                    deleteMut.mutate(r.id);
                  }}
                  className="px-3 py-1.5 rounded-lg bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900"
                  disabled={deleteMut.isPending}
                >
                  <Text className="text-red-600 dark:text-red-400 text-micro font-semibold">刪</Text>
                </Pressable>
              </View>
            </Pressable>
          ))}
        </View>

        <Pressable
          testID="rules-preview-toggle"
          accessibilityRole="button"
          accessibilityLabel="測試 Regex 規則"
          accessibilityState={{ expanded: showPreview }}
          className="bg-white dark:bg-ink-900 rounded-xl py-3.5 px-4 mb-4 flex-row items-center justify-between border border-ink-200 dark:border-ink-700"
          onPress={() => setShowPreview((visible) => !visible)}
        >
          <Text className="text-ink-900 dark:text-ink-50 text-h3">測試 Regex 規則</Text>
          <Text className="text-ink-400 text-h3">{showPreview ? '⌃' : '⌄'}</Text>
        </Pressable>
        {showPreview ? (
        <View className="bg-white dark:bg-ink-900 rounded-2xl p-5 shadow-card mb-4">
          <Text className="text-ink-900 dark:text-ink-50 text-h2 mb-3">
            預覽 match（一行一筆範例文字）
          </Text>
          <TextInput
            className={inputBase}
            placeholder="Regex pattern"
            placeholderTextColor="#94a3b8"
            value={form.pattern}
            onChangeText={(pattern) => setForm({ ...form, pattern })}
            autoCapitalize="none"
            autoCorrect={false}
          />
          <TextInput
            className={`${inputBase} h-24 mt-3`}
            placeholder={'北捷儲值\n早餐店\n蝦皮購物'}
            placeholderTextColor="#94a3b8"
            multiline
            numberOfLines={4}
            value={previewTexts}
            onChangeText={setPreviewTexts}
            autoCapitalize="none"
            autoCorrect={false}
            style={{ textAlignVertical: 'top' }}
          />
          <Pressable
            className={`mt-3 bg-brand-600 active:bg-brand-700 rounded-xl py-3 items-center shadow-brand ${
              previewMut.isPending ? 'opacity-50' : ''
            }`}
            onPress={runPreview}
            disabled={previewMut.isPending}
          >
            <Text className="text-white text-h3">預覽</Text>
          </Pressable>
          {previewResult && (
            <View className="mt-3 bg-ink-100 dark:bg-ink-800 rounded-lg p-3">
              <Text className="text-ink-700 dark:text-ink-300 text-small">
                Match 到的列: {previewResult.length === 0 ? '(無)' : previewResult.join(', ')}
              </Text>
            </View>
          )}
        </View>
        ) : null}

        <View
          className="bg-white dark:bg-ink-900 rounded-2xl p-5 shadow-card"
          testID="rules-advanced-actions"
        >
          <Text className="text-ink-900 dark:text-ink-50 text-h2">進階操作</Text>
          <Text className="text-ink-500 dark:text-ink-400 text-small mt-1">
            這些操作會大量改動既有規則或交易，執行前請先確認。
          </Text>
        {/* Recat all + reset: 手機與桌機都用直列，避免危險操作搶寬。 */}
        <View className="gap-2 mt-4">
          <Pressable
            className={`bg-ink-100 dark:bg-ink-800 active:bg-ink-200 dark:active:bg-ink-700 rounded-xl py-3 items-center ${
              recategorizeMut.isPending ? 'opacity-50' : ''
            }`}
            onPress={() => recategorizeMut.mutate()}
            disabled={recategorizeMut.isPending}
          >
            <Text className="text-ink-900 dark:text-ink-50 text-h3">
              {recategorizeMut.isPending ? '處理中…' : '重新分類所有交易'}
            </Text>
          </Pressable>
          {/* Phase 8 (2026-06-15 使用者指示): 一鍵恢復預設 */}
          <Pressable
            className={`border border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-950 active:bg-red-100 dark:active:bg-red-900 rounded-xl py-3 px-4 items-center justify-center ${
              resetMut.isPending ? 'opacity-50' : ''
            }`}
            onPress={confirmReset}
            disabled={resetMut.isPending}
          >
            <Text className="text-red-700 dark:text-red-300 text-h3">
              {resetMut.isPending ? '⏳' : '↺ 恢復預設'}
            </Text>
          </Pressable>
        </View>
        </View>

      </View>

      {/* Phase 8 (2026-06-15): 編輯規則 Modal — iOS pageSheet / web 浮動 */}
      <Modal
        visible={editingRule !== null}
        transparent={Platform.OS !== 'ios'}
        animationType={Platform.OS === 'ios' ? 'slide' : 'fade'}
        presentationStyle={Platform.OS === 'ios' ? 'pageSheet' : undefined}
        onRequestClose={() => setEditingRule(null)}
      >
        {Platform.OS === 'ios' ? (
          <View className="flex-1 bg-white dark:bg-ink-900">
            <ScrollView
              className="flex-1 px-6 pt-4"
              keyboardShouldPersistTaps="handled"
              showsVerticalScrollIndicator={false}
              contentContainerStyle={{ paddingBottom: 32 }}
              automaticallyAdjustKeyboardInsets
            >
              <Text className="text-ink-900 dark:text-ink-50 text-h2 mb-4">
                編輯規則 #{editingRule?.id}
              </Text>
              {editRuleBody}
            </ScrollView>
          </View>
        ) : (
          <View className="flex-1 bg-black/50 items-center justify-center p-4">
            <View className="bg-white dark:bg-ink-900 rounded-2xl w-full max-w-[480px] shadow-card max-h-[90%]">
              <ScrollView
                className="p-6"
                keyboardShouldPersistTaps="handled"
                showsVerticalScrollIndicator={false}
              >
                <Text className="text-ink-900 dark:text-ink-50 text-h2 mb-4">
                  編輯規則 #{editingRule?.id}
                </Text>
                {editRuleBody}
              </ScrollView>
            </View>
          </View>
        )}
      </Modal>
    </KeyboardAwareScrollView>
  );
}

/**
 * Phase 8.1 (2026-06-15): 子分類 chip — 抓 GET /rules/subcategories?category=X
 * 回傳該主類下「已用過」的子分類, 點 chip 直接填進 form。
 */
function CategorySubChips({
  category,
  selected,
  onSelect,
}: {
  category: string;
  selected: string;
  onSelect: (sub: string) => void;
}) {
  const subQ = useQuery<{ subcategories: string[] }>({
    // Phase C-fe Warning #2 (2026-06-17): queryKey 統一用 ['rules','subcategories',cat]
    // (對齊 BulkEditSheet/TxnDetailModal), 不然 rule mutation invalidate ['rules'] 時
    // 這頁子分類 chips 不會更新, user 要重整頁面才看到新 sub。
    queryKey: ['rules', 'subcategories', category],
    queryFn: () =>
      api<{ subcategories: string[] }>(
        `/rules/subcategories?category=${encodeURIComponent(category)}`,
      ),
    enabled: !!category,
    staleTime: 30_000,
  });

  if (subQ.isPending) {
    return (
      <View className="mb-2">
        <ActivityIndicator size="small" />
      </View>
    );
  }
  const subs = subQ.data?.subcategories ?? [];
  if (subs.length === 0) {
    return (
      <Text className="text-ink-500 text-micro mb-2">
        「{category}」目前還沒有子分類, 下方輸入框可自訂
      </Text>
    );
  }
  return (
    <View className="flex-row flex-wrap gap-1 mb-2">
      {subs.map((sub) => (
        <Pressable
          key={sub}
          onPress={() => onSelect(selected === sub ? '' : sub)}
          className={`px-2 py-1 rounded ${
            selected === sub ? 'bg-brand-600' : 'bg-ink-100 dark:bg-ink-800'
          }`}
        >
          <Text
            className={`text-micro ${
              selected === sub ? 'text-white' : 'text-ink-700 dark:text-ink-300'
            }`}
          >
            {sub}
          </Text>
        </Pressable>
      ))}
    </View>
  );
}
