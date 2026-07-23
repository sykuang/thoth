/** Settings → 分類與標籤。 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Stack } from 'expo-router';
import { useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  Platform,
  Pressable,
  Text,
  TextInput,
  View,
} from 'react-native';

import { KeyboardAwareScrollView } from '@/components/KeyboardAwareScrollView';
import { api, ApiError, formatApiError } from '@/lib/api';
import { sortCategoryKeys } from '@/lib/category-color';
import type { CategoryRule } from '@/types/api';

type CategoryMutationResult = {
  category: string;
  renamed_to: string | null;
  rules_updated: number;
  transactions_updated: number;
};

type SubcategoryMutationResult = CategoryMutationResult & { subcategory: string };
type HashtagMutationResult = {
  hashtag: string;
  renamed_to: string | null;
  transactions_updated: number;
};
type EditingLabel = {
  kind: 'category' | 'subcategory' | 'hashtag';
  name: string;
  category?: string;
};
type PopularTag = { name: string; count: number; last_used: string | null };

export default function LabelsScreen() {
  const qc = useQueryClient();
  const [status, setStatus] = useState<{ kind: 'ok' | 'err'; msg: string } | null>(null);
  const [labelTab, setLabelTab] = useState<'categories' | 'hashtags'>('categories');
  const [expandedCategory, setExpandedCategory] = useState<string | null>(null);
  const [editingLabel, setEditingLabel] = useState<EditingLabel | null>(null);
  const [labelDraft, setLabelDraft] = useState('');
  const rulesQ = useQuery<CategoryRule[]>({
    queryKey: ['rules'],
    queryFn: () => api<CategoryRule[]>('/rules'),
  });
  const categoriesQ = useQuery<{ categories: string[] }>({
    queryKey: ['rules', 'categories', 'manage'],
    queryFn: () => api<{ categories: string[] }>('/rules/categories?include_all=true'),
  });
  const hashtagsQ = useQuery<{ tags: PopularTag[] }>({
    queryKey: ['transactions', 'tags', 'popular'],
    queryFn: () => api<{ tags: PopularTag[] }>('/transactions/tags/popular'),
  });

  // Management includes rule-backed and transaction-only labels so every
  // persisted category remains reachable after its last rule is deleted.
  const usedCategories = sortCategoryKeys(categoriesQ.data?.categories ?? []);
  function invalidateCategoryData() {
    qc.invalidateQueries({ queryKey: ['rules'] });
    qc.invalidateQueries({ queryKey: ['transactions'] });
    qc.invalidateQueries({ queryKey: ['frontend-dataset'] });
    qc.invalidateQueries({ queryKey: ['portfolio', 'summary'] });
  }

  function handleLabelMutationError(e: ApiError) {
    invalidateCategoryData();
    setStatus({ kind: 'err', msg: formatApiError(e) });
  }

  function beginEditingLabel(label: EditingLabel) {
    setEditingLabel(label);
    setLabelDraft(label.name);
  }

  function finishEditingLabel() {
    setEditingLabel(null);
    setLabelDraft('');
  }

  const renameCategoryMut = useMutation<
    CategoryMutationResult,
    ApiError,
    { oldName: string; newName: string }
  >({
    mutationFn: ({ oldName, newName }) =>
      api<CategoryMutationResult>('/rules/categories', {
        method: 'PUT',
        body: { old_name: oldName, name: newName },
      }),
    onSuccess: (data) => {
      invalidateCategoryData();
      finishEditingLabel();
      setExpandedCategory(data.renamed_to);
      setStatus({
        kind: 'ok',
        msg: `「${data.category}」已改名為「${data.renamed_to}」；${data.transactions_updated} 筆交易已更新`,
      });
    },
    onError: handleLabelMutationError,
  });

  const deleteCategoryMut = useMutation<CategoryMutationResult, ApiError, string>({
    mutationFn: (category) =>
      api<CategoryMutationResult>('/rules/categories', {
        method: 'DELETE',
        body: { name: category },
      }),
    onSuccess: (data) => {
      invalidateCategoryData();
      setExpandedCategory(null);
      setStatus({
        kind: 'ok',
        msg: `「${data.category}」已刪除；${data.rules_updated} 條規則已移除，${data.transactions_updated} 筆交易改為未分類`,
      });
    },
    onError: handleLabelMutationError,
  });

  const renameSubcategoryMut = useMutation<
    SubcategoryMutationResult,
    ApiError,
    { category: string; oldName: string; newName: string }
  >({
    mutationFn: ({ category, oldName, newName }) =>
      api<SubcategoryMutationResult>('/rules/subcategories', {
        method: 'PUT',
        body: { category, old_name: oldName, name: newName },
      }),
    onSuccess: (data) => {
      invalidateCategoryData();
      finishEditingLabel();
      setStatus({
        kind: 'ok',
        msg: `「${data.subcategory}」已改名為「${data.renamed_to}」；${data.transactions_updated} 筆交易已更新`,
      });
    },
    onError: handleLabelMutationError,
  });

  const deleteSubcategoryMut = useMutation<
    SubcategoryMutationResult,
    ApiError,
    { category: string; name: string }
  >({
    mutationFn: ({ category, name }) =>
      api<SubcategoryMutationResult>('/rules/subcategories', {
        method: 'DELETE',
        body: { category, name },
      }),
    onSuccess: (data) => {
      invalidateCategoryData();
      setStatus({
        kind: 'ok',
        msg: `子分類「${data.subcategory}」已刪除；${data.transactions_updated} 筆交易已清除子分類`,
      });
    },
    onError: handleLabelMutationError,
  });

  const renameHashtagMut = useMutation<
    HashtagMutationResult,
    ApiError,
    { oldName: string; newName: string }
  >({
    mutationFn: ({ oldName, newName }) =>
      api<HashtagMutationResult>('/transactions/tags', {
        method: 'PUT',
        body: { old_name: oldName, name: newName },
      }),
    onSuccess: (data) => {
      invalidateCategoryData();
      finishEditingLabel();
      setStatus({
        kind: 'ok',
        msg: `#${data.hashtag} 已改名為 #${data.renamed_to}；${data.transactions_updated} 筆交易已更新`,
      });
    },
    onError: handleLabelMutationError,
  });

  const deleteHashtagMut = useMutation<HashtagMutationResult, ApiError, string>({
    mutationFn: (name) =>
      api<HashtagMutationResult>('/transactions/tags', {
        method: 'DELETE',
        body: { name },
      }),
    onSuccess: (data) => {
      invalidateCategoryData();
      setStatus({
        kind: 'ok',
        msg: `#${data.hashtag} 已刪除；${data.transactions_updated} 筆交易已更新`,
      });
    },
    onError: handleLabelMutationError,
  });
  function confirmDeleteCategory(category: string) {
    const msg = `刪除「${category}」會移除使用它的分類規則，並把既有交易改成未分類。此動作無法復原。`;
    if (Platform.OS === 'web') {
      if (window.confirm(msg)) deleteCategoryMut.mutate(category);
    } else {
      Alert.alert('刪除分類標籤', msg, [
        { text: '取消', style: 'cancel' },
        {
          text: '刪除',
          style: 'destructive',
          onPress: () => deleteCategoryMut.mutate(category),
        },
      ]);
    }
  }

  function confirmDeleteSubcategory(category: string, name: string) {
    const msg = `刪除子分類「${name}」會清除規則與交易上的子分類，但保留主分類「${category}」。`;
    if (Platform.OS === 'web') {
      if (window.confirm(msg)) deleteSubcategoryMut.mutate({ category, name });
    } else {
      Alert.alert('刪除子分類', msg, [
        { text: '取消', style: 'cancel' },
        { text: '刪除', style: 'destructive', onPress: () => deleteSubcategoryMut.mutate({ category, name }) },
      ]);
    }
  }

  function confirmDeleteHashtag(name: string) {
    const msg = `刪除 #${name} 會從所有交易移除此 Hashtag。`;
    if (Platform.OS === 'web') {
      if (window.confirm(msg)) deleteHashtagMut.mutate(name);
    } else {
      Alert.alert('刪除 Hashtag', msg, [
        { text: '取消', style: 'cancel' },
        { text: '刪除', style: 'destructive', onPress: () => deleteHashtagMut.mutate(name) },
      ]);
    }
  }

  return (
    <KeyboardAwareScrollView className="flex-1 bg-ink-50 dark:bg-ink-950">
      <Stack.Screen options={{ title: '分類與標籤' }} />
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

        <View
          className="bg-white dark:bg-ink-900 rounded-2xl shadow-card mb-4 overflow-hidden"
          testID="category-labels-card"
        >
          <View className="p-5 pb-3">
            <Text className="text-ink-900 dark:text-ink-50 text-h2">標籤管理</Text>
            <Text className="text-ink-500 dark:text-ink-400 text-small mt-1">
              集中整理主分類、子分類與 Hashtags。
            </Text>
          </View>
          <View className="flex-row p-1 mx-5 mb-3 rounded-xl bg-ink-100 dark:bg-ink-800">
            <Pressable
              testID="label-tab-categories"
              accessibilityRole="tab"
              accessibilityState={{ selected: labelTab === 'categories' }}
              className={`flex-1 py-2 rounded-lg items-center ${
                labelTab === 'categories' ? 'bg-white dark:bg-ink-700 shadow-card' : ''
              }`}
              onPress={() => setLabelTab('categories')}
            >
              <Text className={`text-small font-semibold ${
                labelTab === 'categories'
                  ? 'text-ink-900 dark:text-ink-50'
                  : 'text-ink-500 dark:text-ink-400'
              }`}>
                分類
              </Text>
            </Pressable>
            <Pressable
              testID="label-tab-hashtags"
              accessibilityRole="tab"
              accessibilityState={{ selected: labelTab === 'hashtags' }}
              className={`flex-1 py-2 rounded-lg items-center ${
                labelTab === 'hashtags' ? 'bg-white dark:bg-ink-700 shadow-card' : ''
              }`}
              onPress={() => setLabelTab('hashtags')}
            >
              <Text className={`text-small font-semibold ${
                labelTab === 'hashtags'
                  ? 'text-ink-900 dark:text-ink-50'
                  : 'text-ink-500 dark:text-ink-400'
              }`}>
                Hashtags
              </Text>
            </Pressable>
          </View>

          {labelTab === 'categories' ? (
            categoriesQ.isLoading ? (
              <ActivityIndicator size="small" className="mb-5" />
            ) : usedCategories.length === 0 ? (
              <Text className="text-ink-400 text-small text-center px-5 pb-5">尚無分類</Text>
            ) : (
              <View className="border-t border-ink-100 dark:border-ink-800">
                {usedCategories.map((category) => {
                  const expanded = expandedCategory === category;
                  const editing = editingLabel?.kind === 'category' && editingLabel.name === category;
                  return (
                    <View key={category} className="border-b border-ink-100 dark:border-ink-800">
                      <View className="flex-row items-center px-5 py-3 gap-2">
                        <Pressable
                          className="flex-1 flex-row items-center gap-2"
                          onPress={() => setExpandedCategory(expanded ? null : category)}
                          accessibilityLabel={`${expanded ? '收合' : '展開'} ${category} 子分類`}
                        >
                          <Text className="text-ink-400 w-4">{expanded ? '⌄' : '›'}</Text>
                          <View className="flex-1">
                            <Text className="text-ink-900 dark:text-ink-50 text-body font-medium">
                              {category}
                            </Text>
                            <Text className="text-ink-500 dark:text-ink-400 text-micro mt-0.5">
                              {(rulesQ.data ?? []).filter((rule) => rule.category === category).length} 條規則
                            </Text>
                          </View>
                        </Pressable>
                        <LabelActions
                          editLabel={`編輯分類 ${category}`}
                          onEdit={() => beginEditingLabel({ kind: 'category', name: category })}
                          onDelete={() => confirmDeleteCategory(category)}
                          disabled={deleteCategoryMut.isPending}
                        />
                      </View>
                      {editing ? (
                        <InlineLabelEditor
                          value={labelDraft}
                          onChange={setLabelDraft}
                          onCancel={finishEditingLabel}
                          onSave={() => renameCategoryMut.mutate({
                            oldName: category,
                            newName: labelDraft.trim(),
                          })}
                          saving={renameCategoryMut.isPending}
                          unchanged={labelDraft.trim() === category}
                          accessibilityLabel={`重新命名 ${category}`}
                        />
                      ) : null}
                      {expanded ? (
                        <ManagedSubcategories
                          category={category}
                          editingLabel={editingLabel}
                          labelDraft={labelDraft}
                          onDraftChange={setLabelDraft}
                          onEdit={beginEditingLabel}
                          onCancelEdit={finishEditingLabel}
                          onRename={(oldName, newName) => renameSubcategoryMut.mutate({
                            category,
                            oldName,
                            newName,
                          })}
                          onDelete={(name) => confirmDeleteSubcategory(category, name)}
                          saving={renameSubcategoryMut.isPending}
                          deleting={deleteSubcategoryMut.isPending}
                        />
                      ) : null}
                    </View>
                  );
                })}
              </View>
            )
          ) : hashtagsQ.isLoading ? (
            <ActivityIndicator size="small" className="mb-5" />
          ) : (hashtagsQ.data?.tags.length ?? 0) === 0 ? (
            <Text className="text-ink-400 text-small text-center px-5 pb-5">尚無 Hashtags</Text>
          ) : (
            <View className="border-t border-ink-100 dark:border-ink-800">
              {hashtagsQ.data?.tags.map((tag) => {
                const editing = editingLabel?.kind === 'hashtag' && editingLabel.name === tag.name;
                return (
                  <View key={tag.name} className="border-b border-ink-100 dark:border-ink-800">
                    <View className="flex-row items-center px-5 py-3 gap-2">
                      <View className="flex-1">
                        <Text className="text-brand-600 dark:text-brand-400 text-body font-medium">
                          #{tag.name}
                        </Text>
                        <Text className="text-ink-500 dark:text-ink-400 text-micro mt-0.5">
                          {tag.count} 筆交易
                        </Text>
                      </View>
                      <LabelActions
                        editLabel={`編輯 Hashtag ${tag.name}`}
                        onEdit={() => beginEditingLabel({ kind: 'hashtag', name: tag.name })}
                        onDelete={() => confirmDeleteHashtag(tag.name)}
                        disabled={deleteHashtagMut.isPending}
                      />
                    </View>
                    {editing ? (
                      <InlineLabelEditor
                        value={labelDraft}
                        onChange={setLabelDraft}
                        onCancel={finishEditingLabel}
                        onSave={() => renameHashtagMut.mutate({
                          oldName: tag.name,
                          newName: labelDraft.trim(),
                        })}
                        saving={renameHashtagMut.isPending}
                        unchanged={labelDraft.trim() === tag.name}
                        accessibilityLabel={`編輯 Hashtag ${tag.name}`}
                        maxLength={50}
                      />
                    ) : null}
                  </View>
                );
              })}
            </View>
          )}
        </View>
      </View>
    </KeyboardAwareScrollView>
  );
}

function LabelActions({
  editLabel,
  onEdit,
  onDelete,
  disabled,
}: {
  editLabel: string;
  onEdit: () => void;
  onDelete: () => void;
  disabled: boolean;
}) {
  return (
    <View className="flex-row gap-1">
      <Pressable
        className="px-2.5 py-2 rounded-lg active:bg-ink-100 dark:active:bg-ink-800"
        accessibilityLabel={editLabel}
        onPress={onEdit}
      >
        <Text className="text-brand-600 dark:text-brand-400 text-small font-medium">改名</Text>
      </Pressable>
      <Pressable
        className={`px-2.5 py-2 rounded-lg active:bg-red-50 dark:active:bg-red-950 ${
          disabled ? 'opacity-50' : ''
        }`}
        accessibilityLabel={editLabel.replace('編輯', '刪除')}
        onPress={onDelete}
        disabled={disabled}
      >
        <Text className="text-red-600 dark:text-red-400 text-small font-medium">刪除</Text>
      </Pressable>
    </View>
  );
}

function InlineLabelEditor({
  value,
  onChange,
  onCancel,
  onSave,
  saving,
  unchanged,
  accessibilityLabel,
  maxLength = 80,
}: {
  value: string;
  onChange: (value: string) => void;
  onCancel: () => void;
  onSave: () => void;
  saving: boolean;
  unchanged: boolean;
  accessibilityLabel: string;
  maxLength?: number;
}) {
  const disabled = saving || !value.trim() || unchanged;
  return (
    <View className="px-5 pb-3 gap-2">
      <TextInput
        className="border border-brand-300 dark:border-brand-700 rounded-xl px-3 py-2.5 text-body bg-white dark:bg-ink-800 text-ink-900 dark:text-ink-50"
        value={value}
        onChangeText={onChange}
        autoFocus
        maxLength={maxLength}
        selectTextOnFocus
        accessibilityLabel={accessibilityLabel}
      />
      <View className="flex-row justify-end gap-2">
        <Pressable className="px-3 py-2 rounded-lg bg-ink-100 dark:bg-ink-800" onPress={onCancel}>
          <Text className="text-ink-700 dark:text-ink-200 text-small">取消</Text>
        </Pressable>
        <Pressable
          className={`px-3 py-2 rounded-lg bg-brand-600 ${disabled ? 'opacity-50' : ''}`}
          onPress={onSave}
          disabled={disabled}
        >
          <Text className="text-white text-small font-semibold">{saving ? '儲存中…' : '儲存'}</Text>
        </Pressable>
      </View>
    </View>
  );
}

function ManagedSubcategories({
  category,
  editingLabel,
  labelDraft,
  onDraftChange,
  onEdit,
  onCancelEdit,
  onRename,
  onDelete,
  saving,
  deleting,
}: {
  category: string;
  editingLabel: EditingLabel | null;
  labelDraft: string;
  onDraftChange: (value: string) => void;
  onEdit: (label: EditingLabel) => void;
  onCancelEdit: () => void;
  onRename: (oldName: string, newName: string) => void;
  onDelete: (name: string) => void;
  saving: boolean;
  deleting: boolean;
}) {
  const subQ = useQuery<{ subcategories: string[] }>({
    queryKey: ['rules', 'subcategories', category, 'manage'],
    queryFn: () => api<{ subcategories: string[] }>(
      '/rules/subcategories?category=' + encodeURIComponent(category) + '&include_all=true',
    ),
  });

  if (subQ.isLoading) {
    return <ActivityIndicator size="small" className="py-3 bg-ink-50 dark:bg-ink-950" />;
  }
  const subcategories = subQ.data?.subcategories ?? [];
  return (
    <View className="bg-ink-50 dark:bg-ink-950 border-t border-ink-100 dark:border-ink-800">
      <Text className="px-11 pt-3 pb-1 text-ink-400 dark:text-ink-500 text-micro font-semibold">
        子分類
      </Text>
      {subcategories.length === 0 ? (
        <Text className="px-11 pb-3 text-ink-400 dark:text-ink-500 text-small">尚無子分類</Text>
      ) : subcategories.map((subcategory) => {
        const editing =
          editingLabel?.kind === 'subcategory' &&
          editingLabel.category === category &&
          editingLabel.name === subcategory;
        return (
          <View key={subcategory} className="border-t border-ink-100 dark:border-ink-800">
            <View className="flex-row items-center pl-11 pr-5 py-2 gap-2">
              <Text className="text-ink-400">└</Text>
              <Text className="flex-1 text-ink-700 dark:text-ink-200 text-small">{subcategory}</Text>
              <LabelActions
                editLabel={`編輯子分類 ${subcategory}`}
                onEdit={() => onEdit({ kind: 'subcategory', category, name: subcategory })}
                onDelete={() => onDelete(subcategory)}
                disabled={deleting}
              />
            </View>
            {editing ? (
              <InlineLabelEditor
                value={labelDraft}
                onChange={onDraftChange}
                onCancel={onCancelEdit}
                onSave={() => onRename(subcategory, labelDraft.trim())}
                saving={saving}
                unchanged={labelDraft.trim() === subcategory}
                accessibilityLabel={`編輯子分類 ${subcategory}`}
              />
            ) : null}
          </View>
        );
      })}
    </View>
  );
}
