/**
 * CategoryPicker — MoneyBook-style category selector.
 *
 * Unlike the generic Dropdown list, top-level categories are visual choices:
 * a bottom/floating sheet with Cancel / title / Category Management and a
 * 4-column icon grid. This avoids a long vertical dropdown that can extend
 * below the phone viewport.
 */
import { useMemo, useState } from 'react';
import {
  BadgeDollarSign,
  Beer,
  BookOpen,
  Briefcase,
  Car,
  Coins,
  Cross,
  Gamepad2,
  Gift,
  House,
  Landmark,
  Package as PackageIcon,
  Plane,
  ShoppingBag,
  Smartphone,
  TrendingUp,
  Utensils,
  type LucideIcon,
} from 'lucide-react-native';
import {
  Modal,
  Platform,
  Pressable,
  ScrollView,
  Text,
  useWindowDimensions,
  View,
} from 'react-native';

import { EXPENSE_CATEGORIES, INCOME_CATEGORIES } from '@/lib/category-color';

export type CategoryPickerOption = {
  label: string;
  value: string;
  hint?: string;
};

type CategoryPickerProps = {
  label?: string;
  value: string;
  onChange: (next: string) => void;
  options: CategoryPickerOption[];
  placeholder?: string;
  disabled?: boolean;
  testID?: string;
  modalTitle?: string;
  /** 顯示右上「分類管理」。caller 可先關掉外層 modal 再導頁。 */
  onManage?: () => void;
};

const SPECIAL_PREFIX = '__';
const EXPENSE_ONLY_SET = new Set<string>(EXPENSE_CATEGORIES.filter((c) => c !== '其他'));
const INCOME_SET = new Set<string>(INCOME_CATEGORIES);
const INCOME_ONLY_SET = new Set<string>(INCOME_CATEGORIES.filter((c) => c !== '其他'));
const NEUTRAL_SET = new Set<string>(['其他', '轉帳', '還款']);

const CATEGORY_ACCENTS: Record<string, string> = {
  飲食: '#fb923c',
  購物: '#22d3ee',
  交通: '#60a5fa',
  居住: '#a3e635',
  通訊: '#06b6d4',
  娛樂: '#a855f7',
  醫療: '#ef4444',
  教育: '#4f46e5',
  旅遊: '#84cc16',
  金融: '#ec4899',
  投資: '#22c55e',
  酒菸: '#f97316',
  其他: '#0f172a',
  薪資: '#22c55e',
  獎金: '#84cc16',
  利息股息: '#10b981',
  投資收益: '#6366f1',
};

const CATEGORY_ICONS: Record<string, LucideIcon> = {
  飲食: Utensils,
  購物: ShoppingBag,
  交通: Car,
  居住: House,
  通訊: Smartphone,
  娛樂: Gamepad2,
  醫療: Cross,
  教育: BookOpen,
  旅遊: Plane,
  金融: Landmark,
  投資: TrendingUp,
  酒菸: Beer,
  其他: PackageIcon,
  薪資: Briefcase,
  獎金: Gift,
  利息股息: Coins,
  投資收益: BadgeDollarSign,
};

function categoryIconFor(category: string): LucideIcon {
  return CATEGORY_ICONS[category] ?? PackageIcon;
}

function accentFor(category: string): string {
  return CATEGORY_ACCENTS[category] ?? '#64748b';
}

function isSpecialOption(opt: CategoryPickerOption): boolean {
  return opt.value === '' || opt.value.startsWith(SPECIAL_PREFIX);
}

function testIdSuffix(value: string): string {
  return value === '' ? 'clear' : value;
}

export function CategoryPicker({
  label,
  value,
  onChange,
  options,
  placeholder = '請選擇',
  disabled,
  testID,
  modalTitle = '選擇分類',
  onManage,
}: CategoryPickerProps) {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<'expense' | 'income' | 'neutral'>(
    INCOME_ONLY_SET.has(value) ? 'income' : NEUTRAL_SET.has(value) ? 'neutral' : 'expense',
  );
  const { height } = useWindowDimensions();
  const viewportSafeHeight = Math.max(360, height - 24);
  const sheetMaxHeight = Math.min(
    viewportSafeHeight,
    Math.max(420, Math.round(height * 0.88)),
  );
  const gridMaxHeight = Math.max(220, sheetMaxHeight - 174);

  const selected = options.find((o) => o.value === value);
  const triggerText = selected?.label ?? placeholder;
  const isPlaceholder = !selected;

  const { specialOptions, expenseOptions, incomeOptions, neutralOptions } = useMemo(() => {
    const special = options.filter(isSpecialOption);
    const regular = options.filter((o) => !isSpecialOption(o));
    const known = (value: string) =>
      EXPENSE_ONLY_SET.has(value) || INCOME_ONLY_SET.has(value) || NEUTRAL_SET.has(value);
    return {
      specialOptions: special,
      expenseOptions: regular.filter((o) => EXPENSE_ONLY_SET.has(o.value)),
      incomeOptions: regular.filter((o) => INCOME_SET.has(o.value) && o.value !== '其他'),
      neutralOptions: regular.filter((o) => NEUTRAL_SET.has(o.value) || !known(o.value)),
    };
  }, [options]);

  const hasIncome = incomeOptions.length > 0;
  const hasNeutral = neutralOptions.length > 0;
  const gridOptions =
    hasIncome && tab === 'income'
      ? incomeOptions
      : hasNeutral && tab === 'neutral'
        ? neutralOptions
        : expenseOptions;
  const tabs = [
    { id: 'expense' as const, label: '支出', options: expenseOptions },
    { id: 'income' as const, label: '收入', options: incomeOptions },
    { id: 'neutral' as const, label: '其他', options: neutralOptions },
  ].filter((item) => item.options.length > 0);

  const handlePick = (next: string) => {
    onChange(next);
    setOpen(false);
  };

  const handleManage = () => {
    setOpen(false);
    onManage?.();
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
            className="bg-white dark:bg-ink-900 rounded-t-2xl web:rounded-2xl shadow-card web:max-w-[520px] web:w-full overflow-hidden"
          >
            <View className="px-4 py-3 border-b border-ink-100 dark:border-ink-800 flex-row items-center justify-between">
              <Pressable
                onPress={() => setOpen(false)}
                className="py-1 pr-2"
                testID={testID ? `${testID}-cancel` : undefined}
              >
                <Text className="text-brand-600 dark:text-brand-400 text-body font-semibold">
                  取消
                </Text>
              </Pressable>
              <Text className="text-ink-900 dark:text-ink-50 text-h3 font-bold">
                {modalTitle}
              </Text>
              {onManage ? (
                <Pressable
                  onPress={handleManage}
                  className="py-1 pl-2"
                  testID={testID ? `${testID}-manage` : undefined}
                >
                  <Text className="text-brand-600 dark:text-brand-400 text-body font-semibold">
                    分類管理
                  </Text>
                </Pressable>
              ) : (
                <View className="w-16" />
              )}
            </View>

            {specialOptions.map((opt) => {
              const active = opt.value === value;
              return (
                <Pressable
                  key={opt.value}
                  onPress={() => handlePick(opt.value)}
                  className="flex-row items-center justify-between px-5 py-4 border-b border-ink-100 dark:border-ink-800"
                  testID={testID ? `${testID}-option-${testIdSuffix(opt.value)}` : undefined}
                >
                  <Text className="text-ink-900 dark:text-ink-50 text-body font-semibold">
                    {opt.label}
                  </Text>
                  {active && (
                    <Text className="text-brand-600 dark:text-brand-400 text-h2">✓</Text>
                  )}
                </Pressable>
              );
            })}

            {tabs.length > 1 && (
              <View className="mx-4 mt-4 mb-2 p-1 rounded-xl bg-ink-100 dark:bg-ink-800 flex-row">
                {tabs.map((item) => (
                  <Pressable
                    key={item.id}
                    onPress={() => setTab(item.id)}
                    className={`flex-1 py-2 rounded-lg items-center ${
                      tab === item.id ? 'bg-white dark:bg-ink-700 shadow-sm' : ''
                    }`}
                    testID={testID ? `${testID}-tab-${item.id}` : undefined}
                  >
                    <Text className="text-ink-900 dark:text-ink-50 text-body font-semibold">
                      {item.label}
                    </Text>
                  </Pressable>
                ))}
              </View>
            )}

            <ScrollView
              style={{ maxHeight: gridMaxHeight }}
              contentContainerStyle={{ paddingHorizontal: 18, paddingTop: 12, paddingBottom: 24 }}
            >
              <View className="flex-row flex-wrap">
                {gridOptions.map((opt) => {
                  const active = opt.value === value;
                  const CategoryIcon = categoryIconFor(opt.value);
                  return (
                    <Pressable
                      key={opt.value}
                      onPress={() => handlePick(opt.value)}
                      className="items-center mb-6 px-1"
                      style={{ width: '25%' }}
                      testID={testID ? `${testID}-option-${testIdSuffix(opt.value)}` : undefined}
                    >
                      <View
                        className={`w-16 h-16 rounded-full items-center justify-center mb-2 ${
                          active ? 'border-4 border-brand-500' : ''
                        }`}
                        style={{ backgroundColor: accentFor(opt.value) }}
                      >
                        <CategoryIcon
                          size={30}
                          color="#fff"
                          strokeWidth={2.4}
                          absoluteStrokeWidth
                        />
                        {active && (
                          <View className="absolute -right-1 -top-1 bg-brand-600 rounded-full w-6 h-6 items-center justify-center border-2 border-white dark:border-ink-900">
                            <Text className="text-white text-micro font-bold">✓</Text>
                          </View>
                        )}
                      </View>
                      <Text
                        className="text-ink-900 dark:text-ink-50 text-body font-semibold text-center"
                        numberOfLines={2}
                      >
                        {opt.label}
                      </Text>
                      {opt.hint && (
                        <Text className="text-ink-400 dark:text-ink-500 text-micro mt-0.5 text-center">
                          {opt.hint}
                        </Text>
                      )}
                    </Pressable>
                  );
                })}
              </View>
            </ScrollView>
          </Pressable>
        </Pressable>
      </Modal>
    </View>
  );
}
