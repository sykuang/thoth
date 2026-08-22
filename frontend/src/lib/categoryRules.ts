import type { CategoryRule } from '@/types/api';

/**
 * Client-side rule search for the settings list.
 *
 * Rules are few enough to keep locally, so filtering should be immediate and
 * must cover every field a user is likely to remember when locating a rule.
 */
export function filterCategoryRules(
  rules: readonly CategoryRule[],
  query: string,
): CategoryRule[] {
  const normalizedQuery = query.trim().toLocaleLowerCase();
  if (!normalizedQuery) return [...rules];

  return rules.filter((rule) =>
    [rule.name, rule.pattern, rule.category, rule.subcategory ?? ''].some((value) =>
      value.toLocaleLowerCase().includes(normalizedQuery),
    ),
  );
}
