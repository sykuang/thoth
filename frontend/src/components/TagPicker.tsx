/**
 * TagPicker — 跨平台 hashtag 選擇器 (Web + iOS + Android + Tauri).
 *
 * Phase 9.1 (2026-06-17): 取代「detail modal 內 inline TextInput 加 tag」這個爛 UX:
 *   - 使用者每次重打 → 同義字打不齊 (「日本旅遊」vs「日本旅行」變兩個 tag)
 *   - 看不到自己過去用過哪些 tag
 *   - input 在 detail modal 底部, 鍵盤一跳就被蓋
 *
 * 設計:
 *   - iOS 用 presentationStyle="pageSheet" (Mail compose / Notes 風),
 *     ScrollView 加 automaticallyAdjustKeyboardInsets, input 在頂端
 *   - Web / macOS 用浮動 modal (鍵盤不存在不撞)
 *   - Body 抽 const 變數兩 branch 共用
 *   - input 頂端 + 已選 chip + 推薦 list (count desc / recent 切換) + 「新增」action
 *   - 上限 20 個, 接近上限給提示
 *
 * Affordance:
 *   - 已選 chip: 點 ✕ 移除
 *   - 推薦 list: 點 row toggle 選 / 取消 (左側 ✓)
 *   - input 沒命中已存在 tag: 顯示「+ 新增『XXX』」
 *
 * @example
 *   <TagPicker
 *     visible={tagPickerVisible}
 *     value={editTags}
 *     onChange={setEditTags}
 *     onClose={() => setTagPickerVisible(false)}
 *   />
 */
import { useMemo, useState } from 'react';
import {
  ActivityIndicator,
  Modal,
  Platform,
  Pressable,
  ScrollView,
  Text,
  TextInput,
  View,
} from 'react-native';
import { useQuery } from '@tanstack/react-query';

import { api, type ApiError } from '@/lib/api';

export type PopularTag = {
  name: string;
  count: number;
  last_used: string | null;
};

type Props = {
  visible: boolean;
  value: string[];
  onChange: (next: string[]) => void;
  onClose: () => void;
};

const MAX_TAGS = 20;
const TAG_MAX_LEN = 50;

export function TagPicker({ visible, value, onChange, onClose }: Props) {
  const [query, setQuery] = useState('');
  const [sort, setSort] = useState<'count' | 'recent'>('count');

  const popularQ = useQuery<{ tags: PopularTag[] }, ApiError>({
    queryKey: ['transactions', 'tags', 'popular', sort],
    queryFn: () =>
      api<{ tags: PopularTag[] }>(`/transactions/tags/popular?sort=${sort}`),
    enabled: visible,
    staleTime: 30_000,
  });

  const trimmedQuery = query.trim();
  // useMemo 包 allPopular 避免 ?? [] 每 render 產生新 reference 害下游 hook deps 抖
  const allPopular = useMemo(() => popularQ.data?.tags ?? [], [popularQ.data?.tags]);

  // 過濾: 模糊 substring match (case-insensitive 對 ASCII; CJK 直接 includes)
  // 已選的 tag 也要列出 (帶 ✓), 讓 user 能直接取消
  const filtered = useMemo(() => {
    if (!trimmedQuery) return allPopular;
    const needle = trimmedQuery.toLowerCase();
    return allPopular.filter((t) =>
      t.name.toLowerCase().includes(needle),
    );
  }, [allPopular, trimmedQuery]);

  // input 是否「沒命中既有 tag」→ 顯示「新增 XXX」action
  const showAddAction = useMemo(() => {
    if (!trimmedQuery) return false;
    if (trimmedQuery.length > TAG_MAX_LEN) return false;
    // 已存在於推薦 (大小寫不敏感) or 已選 → 不顯示新增
    const lower = trimmedQuery.toLowerCase();
    const inPopular = allPopular.some((t) => t.name.toLowerCase() === lower);
    const inValue = value.some((v) => v.toLowerCase() === lower);
    return !inPopular && !inValue;
  }, [trimmedQuery, allPopular, value]);

  function toggleTag(name: string) {
    const lower = name.toLowerCase();
    const idx = value.findIndex((v) => v.toLowerCase() === lower);
    if (idx >= 0) {
      // 已選 → 取消
      onChange(value.filter((_, i) => i !== idx));
    } else {
      if (value.length >= MAX_TAGS) return;
      onChange([...value, name]);
    }
  }

  function addNew() {
    if (!showAddAction) return;
    if (value.length >= MAX_TAGS) return;
    onChange([...value, trimmedQuery]);
    setQuery('');
  }

  function removeTag(name: string) {
    onChange(value.filter((v) => v !== name));
  }

  const remainingSlots = MAX_TAGS - value.length;
  const atLimit = remainingSlots <= 0;
  const nearLimit = remainingSlots <= 3 && remainingSlots > 0;

  const body = (
    <>
      {/* Header: 完成 button + 標題 */}
      <View className="flex-row items-center justify-between mb-4">
        <Pressable onPress={onClose} className="py-2 -ml-2 px-2">
          <Text className="text-brand-600 dark:text-brand-400 text-body">取消</Text>
        </Pressable>
        <Text className="text-ink-900 dark:text-ink-50 text-h3 font-semibold">
          選擇標籤
        </Text>
        <Pressable onPress={onClose} className="py-2 -mr-2 px-2">
          <Text className="text-brand-600 dark:text-brand-400 text-body font-semibold">
            完成
          </Text>
        </Pressable>
      </View>

      {/* Search input */}
      <View className="mb-3">
        <TextInput
          value={query}
          onChangeText={setQuery}
          placeholder="🔍 搜尋或新增標籤…"
          placeholderTextColor="#94a3b8"
          maxLength={TAG_MAX_LEN}
          autoCorrect={false}
          autoCapitalize="none"
          returnKeyType="done"
          onSubmitEditing={addNew}
          className="border border-ink-200 dark:border-ink-700 rounded-xl px-3 py-2.5 text-body bg-white dark:bg-ink-800 text-ink-900 dark:text-ink-50"
          testID="tag-picker-search"
        />
      </View>

      {/* 新增 action — input 沒命中既有 tag 才出現 */}
      {showAddAction && (
        <Pressable
          onPress={addNew}
          disabled={atLimit}
          className={`flex-row items-center px-3 py-2.5 rounded-xl mb-3 border ${
            atLimit
              ? 'border-ink-200 dark:border-ink-700 bg-ink-50 dark:bg-ink-800'
              : 'border-brand-500/50 bg-brand-50 dark:bg-brand-950 dark:border-brand-700'
          }`}
          testID="tag-picker-add-new"
        >
          <Text className={`text-body ${atLimit ? 'text-ink-400' : 'text-brand-700 dark:text-brand-300'}`}>
            ＋ 新增「{trimmedQuery}」
          </Text>
        </Pressable>
      )}

      {/* 已選 chip 區 (只在有選時顯示) */}
      {value.length > 0 && (
        <View className="mb-3">
          <View className="flex-row items-center justify-between mb-2">
            <Text className="text-ink-500 dark:text-ink-400 text-micro font-semibold tracking-wider uppercase">
              已選 {value.length}{nearLimit ? ` / ${MAX_TAGS}` : ''}
            </Text>
            {atLimit && (
              <Text className="text-amber-600 dark:text-amber-400 text-micro">
                已達上限
              </Text>
            )}
          </View>
          <View className="flex-row flex-wrap gap-2">
            {value.map((tag) => (
              <Pressable
                key={tag}
                onPress={() => removeTag(tag)}
                className="flex-row items-center px-2.5 py-1 rounded-full bg-brand-100 dark:bg-brand-900 active:opacity-60"
                testID={`tag-picker-selected-${tag}`}
              >
                <Text className="text-brand-700 dark:text-brand-300 text-small">
                  #{tag}
                </Text>
                <Text className="text-brand-500 dark:text-brand-400 text-small ml-1.5">
                  ✕
                </Text>
              </Pressable>
            ))}
          </View>
        </View>
      )}

      {/* Sort segmented control + 列表 標題 */}
      <View className="flex-row items-center justify-between mb-2">
        <Text className="text-ink-500 dark:text-ink-400 text-micro font-semibold tracking-wider uppercase">
          {sort === 'count' ? '常用' : '最近用過'}
        </Text>
        <View className="flex-row border border-ink-200 dark:border-ink-700 rounded-lg overflow-hidden">
          <Pressable
            onPress={() => setSort('count')}
            className={`px-3 py-1 ${sort === 'count' ? 'bg-brand-600' : 'bg-white dark:bg-ink-800'}`}
            testID="tag-picker-sort-count"
          >
            <Text className={`text-micro ${sort === 'count' ? 'text-white font-semibold' : 'text-ink-600 dark:text-ink-300'}`}>
              常用
            </Text>
          </Pressable>
          <Pressable
            onPress={() => setSort('recent')}
            className={`px-3 py-1 ${sort === 'recent' ? 'bg-brand-600' : 'bg-white dark:bg-ink-800'}`}
            testID="tag-picker-sort-recent"
          >
            <Text className={`text-micro ${sort === 'recent' ? 'text-white font-semibold' : 'text-ink-600 dark:text-ink-300'}`}>
              最近
            </Text>
          </Pressable>
        </View>
      </View>

      {/* 推薦 list */}
      {popularQ.isLoading ? (
        <View className="py-8 items-center">
          <ActivityIndicator color="#0d9488" />
        </View>
      ) : filtered.length === 0 ? (
        <Text className="text-ink-400 dark:text-ink-500 text-small py-4 text-center">
          {trimmedQuery
            ? '沒有相符的標籤, 試試上方「+ 新增」'
            : '還沒有任何標籤, 上方 input 直接新增'}
        </Text>
      ) : (
        <View>
          {filtered.map((tag) => {
            const isSelected = value.some((v) => v.toLowerCase() === tag.name.toLowerCase());
            const disabled = !isSelected && atLimit;
            return (
              <Pressable
                key={tag.name}
                onPress={() => toggleTag(tag.name)}
                disabled={disabled}
                className={`flex-row items-center px-3 py-3 border-b border-ink-100 dark:border-ink-800 active:bg-ink-50 dark:active:bg-ink-800 ${
                  disabled ? 'opacity-30' : ''
                }`}
                testID={`tag-picker-popular-${tag.name}`}
              >
                <View className="w-6">
                  {isSelected && (
                    <Text className="text-brand-600 dark:text-brand-400 text-body font-bold">
                      ✓
                    </Text>
                  )}
                </View>
                <Text className={`text-body flex-1 ${isSelected ? 'text-brand-700 dark:text-brand-300 font-semibold' : 'text-ink-800 dark:text-ink-200'}`}>
                  #{tag.name}
                </Text>
              </Pressable>
            );
          })}
        </View>
      )}
    </>
  );

  // iOS native pageSheet — 系統處理 modal layout
  // ScrollView automaticallyAdjustKeyboardInsets — 鍵盤跳出自動 inset
  if (Platform.OS === 'ios') {
    return (
      <Modal
        visible={visible}
        animationType="slide"
        presentationStyle="pageSheet"
        onRequestClose={onClose}
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

  // web / macOS — 浮動 modal
  return (
    <Modal visible={visible} transparent animationType="fade" onRequestClose={onClose}>
      <Pressable
        className="flex-1 items-center justify-center bg-black/50 px-4"
        onPress={onClose}
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
