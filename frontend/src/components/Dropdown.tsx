/**
 * Dropdown — 跨平台單選 picker (Web + iOS + Android + Tauri).
 *
 * 用 RN Modal 自製 bottom-sheet style picker, 避免 @react-native-picker/picker
 * 的 native dependency. iOS 上看起來像 ActionSheet, web/desktop 上像 dialog.
 *
 * 為什麼自製不用 native picker:
 *   - native 在 Tauri desktop / web 行為不一致 (有時是 select dropdown, 有時 sheet)
 *   - 自製 Modal 確保 web/iOS/desktop 視覺與互動完全一樣
 *
 * Affordance 設計（解掉「chip group 看起來是複選」問題）:
 *   - Trigger: 純 row 顯示「label: <當前值 ▼>」, 像 iOS Settings cell
 *   - Sheet: 開的時候彈 modal, 每個選項一行
 *   - 選中: 右側打勾 ✓ + 主色 text
 *   - 自然單選 mental model (radio group + iOS list pattern)
 *
 * @example
 *   <Dropdown
 *     label="主分類"
 *     value={editCat}
 *     onChange={setEditCat}
 *     options={categories}
 *     placeholder="請選擇主分類"
 *   />
 */
import { useState } from 'react';
import {
  Modal,
  Platform,
  Pressable,
  ScrollView,
  Text,
  useWindowDimensions,
  View,
} from 'react-native';

export type DropdownOption = {
  /** 顯示文字. 為了讓 caller 對 enum + label 場景一致 (e.g. '__null__' → '未分類') */
  label: string;
  /** State 存的值. 跟 value 比對. */
  value: string;
  /** 可選: 副字 (顯示在 label 右側灰字, 例如「12 筆」) */
  hint?: string;
};

type DropdownProps = {
  /** 上方 label, 例如「主分類」「子分類」. */
  label?: string;
  /** 目前選中值. 空字串 = 未選. */
  value: string;
  /** 改變時 callback. */
  onChange: (next: string) => void;
  /** 選項清單. */
  options: DropdownOption[];
  /** 沒選中時 trigger 文字. */
  placeholder?: string;
  /** 為 true → trigger 顯示 disabled, 不能 tap. */
  disabled?: boolean;
  /** 「(無)」chip 文字. 給可清空的 picker 用. omit 則無清空選項. */
  clearLabel?: string;
  /** testID prefix, 給 e2e. */
  testID?: string;
  /** Modal 標題, 預設用 label. */
  modalTitle?: string;
  /**
   * Sheet 最大高度占 viewport 的比例。預設 0.52，避免長清單一路貼到畫面頂端
   * 讓 iPhone 下方選項難點。單一 caller 可傳 0.6/0.7 調高。
   */
  maxHeightRatio?: number;
};

export function Dropdown({
  label,
  value,
  onChange,
  options,
  placeholder = '請選擇',
  disabled,
  clearLabel,
  testID,
  modalTitle,
  maxHeightRatio = 0.52,
}: DropdownProps) {
  const [open, setOpen] = useState(false);
  const { height } = useWindowDimensions();
  const viewportSafeHeight = Math.max(240, height - 48);
  const sheetMaxHeight = Math.min(
    viewportSafeHeight,
    Math.max(260, Math.round(height * maxHeightRatio)),
  );
  const listMaxHeight = Math.max(180, sheetMaxHeight - 64);

  const selected = options.find((o) => o.value === value);
  const triggerText = selected?.label ?? placeholder;
  const isPlaceholder = !selected;

  const handlePick = (next: string) => {
    onChange(next);
    setOpen(false);
  };

  return (
    <View>
      {label && (
        <Text className="text-ink-700 dark:text-ink-300 text-small font-semibold mb-2">
          {label}
        </Text>
      )}
      <Pressable
        onPress={() => !disabled && setOpen(true)}
        disabled={disabled}
        testID={testID}
        className={`flex-row items-center justify-between px-3 py-3 rounded-xl border ${
          disabled
            ? 'bg-ink-100 dark:bg-ink-900 border-ink-200 dark:border-ink-800 opacity-60'
            : 'bg-white dark:bg-ink-800 border-ink-300 dark:border-ink-600'
        }`}
      >
        <Text
          className={`flex-1 mr-2 text-body ${
            isPlaceholder
              ? 'text-ink-400 dark:text-ink-500'
              : 'text-ink-900 dark:text-ink-50 font-semibold'
          }`}
          numberOfLines={1}
        >
          {triggerText}
        </Text>
        <Text className="text-ink-500 dark:text-ink-400 text-small">
          {disabled ? '' : '▼'}
        </Text>
      </Pressable>

      <Modal
        visible={open}
        transparent
        animationType={Platform.OS === 'ios' ? 'slide' : 'fade'}
        onRequestClose={() => setOpen(false)}
      >
        <Pressable
          className="flex-1 bg-black/50 justify-end web:items-center web:justify-center web:p-4"
          onPress={() => setOpen(false)}
        >
          <Pressable
            onPress={(e) => e.stopPropagation()}
            style={{ maxHeight: sheetMaxHeight }}
            className="bg-white dark:bg-ink-900 rounded-t-2xl web:rounded-2xl shadow-card web:max-w-[420px] web:w-full"
          >
            <View className="px-4 py-3 border-b border-ink-100 dark:border-ink-800 flex-row items-center justify-between">
              <Text className="text-ink-900 dark:text-ink-50 text-h3 font-bold">
                {modalTitle ?? label ?? '請選擇'}
              </Text>
              <Pressable
                onPress={() => setOpen(false)}
                className="px-2 py-1 -mr-2"
                testID={testID ? `${testID}-close` : undefined}
              >
                <Text className="text-ink-500 dark:text-ink-400 text-h3">✕</Text>
              </Pressable>
            </View>

            <ScrollView style={{ maxHeight: listMaxHeight }}>
              {clearLabel !== undefined && (
                <Pressable
                  onPress={() => handlePick('')}
                  className={`flex-row items-center justify-between px-4 py-3 border-b border-ink-100 dark:border-ink-800 ${
                    value === '' ? 'bg-brand-50 dark:bg-brand-950' : ''
                  }`}
                  testID={testID ? `${testID}-option-clear` : undefined}
                >
                  <Text
                    className={`text-body ${
                      value === ''
                        ? 'text-brand-700 dark:text-brand-300 font-semibold'
                        : 'text-ink-700 dark:text-ink-300'
                    }`}
                  >
                    {clearLabel}
                  </Text>
                  {value === '' && (
                    <Text className="text-brand-600 dark:text-brand-400 text-h3">
                      ✓
                    </Text>
                  )}
                </Pressable>
              )}
              {options.map((opt) => {
                const active = opt.value === value;
                return (
                  <Pressable
                    key={opt.value}
                    onPress={() => handlePick(opt.value)}
                    className={`flex-row items-center justify-between px-4 py-3 border-b border-ink-100 dark:border-ink-800 ${
                      active ? 'bg-brand-50 dark:bg-brand-950' : ''
                    }`}
                    testID={testID ? `${testID}-option-${opt.value}` : undefined}
                  >
                    <View className="flex-1 mr-2">
                      <Text
                        className={`text-body ${
                          active
                            ? 'text-brand-700 dark:text-brand-300 font-semibold'
                            : 'text-ink-900 dark:text-ink-50'
                        }`}
                        numberOfLines={1}
                      >
                        {opt.label}
                      </Text>
                      {opt.hint && (
                        <Text className="text-ink-400 dark:text-ink-500 text-micro mt-0.5">
                          {opt.hint}
                        </Text>
                      )}
                    </View>
                    {active && (
                      <Text className="text-brand-600 dark:text-brand-400 text-h3">
                        ✓
                      </Text>
                    )}
                  </Pressable>
                );
              })}
            </ScrollView>
          </Pressable>
        </Pressable>
      </Modal>
    </View>
  );
}
