import type { CategoryRule } from '@/types/api';
import { filterCategoryRules } from './categoryRules';

function deepEqual(actual: unknown, expected: unknown, message: string): void {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`${message}\nexpected: ${JSON.stringify(expected)}\nactual: ${JSON.stringify(actual)}`);
  }
}

function rule(
  id: number,
  name: string,
  pattern: string,
  category: string,
  subcategory: string | null = null,
): CategoryRule {
  return {
    id,
    name,
    pattern,
    category,
    subcategory,
    priority: 100,
    enabled: 1,
    auto_excluded: 0,
    created_at: '2026-08-22T00:00:00Z',
    updated_at: '2026-08-22T00:00:00Z',
  };
}

const rules = [
  rule(1, '薪資', 'SALARY|Payroll|台積', '薪資'),
  rule(2, '貸款利息支出', '放款利息|循環息', '金融', '利息'),
  rule(3, '早餐店', '早餐|蛋餅', '飲食', '早餐'),
  rule(4, '通勤', '捷運', '交通', '月票'),
];

deepEqual(
  filterCategoryRules(rules, 'payroll').map((item) => item.id),
  [1],
  '搜尋不分英文大小寫，且會比對 Regex pattern',
);

deepEqual(
  filterCategoryRules(rules, ' 金融 ').map((item) => item.id),
  [2],
  '搜尋會忽略前後空白並比對主分類',
);

deepEqual(
  filterCategoryRules(rules, '月票').map((item) => item.id),
  [4],
  '搜尋會比對只出現在子分類的文字',
);

deepEqual(
  filterCategoryRules(rules, ''),
  rules,
  '空白搜尋保留所有規則及原始排序',
);

deepEqual(
  filterCategoryRules(rules, '找不到'),
  [],
  '沒有符合條件時回傳空清單',
);

console.log('category rule search tests passed');
