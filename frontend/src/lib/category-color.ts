/**
 * Category badge color — fixed mapping per 13+5 known categories, djb2 fallback.
 *
 * Phase 11 (W 2026-06-17 review): 原本只用 djb2 % 8 → 13 主類撞 8 色必有
 * 至少 5 對撞色，UI 上「飲食」「交通」可能同橙色看不出差別。改成：
 *   - 18 個已知 category (13 expense + 5 income) → 明確固定色
 *   - 未知 category (使用者自訂 / migration 殘留) 才走 djb2 hash fallback
 * 這樣 known taxonomy 的色完全 deterministic 而且絕無撞色，新 category
 * 只要加進 CATEGORY_COLORS 就有專屬色。
 */

/** 13 個 dist 色（避開 brand purple + income green + expense red）+ neutral. */
const PALETTE: { bg: string; text: string }[] = [
  { bg: 'bg-orange-100 dark:bg-orange-950/40', text: 'text-orange-700 dark:text-orange-300' },     // 0
  { bg: 'bg-sky-100 dark:bg-sky-950/40', text: 'text-sky-700 dark:text-sky-300' },                  // 1
  { bg: 'bg-emerald-100 dark:bg-emerald-950/40', text: 'text-emerald-700 dark:text-emerald-300' },  // 2
  { bg: 'bg-pink-100 dark:bg-pink-950/40', text: 'text-pink-700 dark:text-pink-300' },              // 3
  { bg: 'bg-violet-100 dark:bg-violet-950/40', text: 'text-violet-700 dark:text-violet-300' },      // 4
  { bg: 'bg-cyan-100 dark:bg-cyan-950/40', text: 'text-cyan-700 dark:text-cyan-300' },              // 5
  { bg: 'bg-yellow-100 dark:bg-yellow-950/40', text: 'text-yellow-700 dark:text-yellow-300' },     // 6
  { bg: 'bg-teal-100 dark:bg-teal-950/40', text: 'text-teal-700 dark:text-teal-300' },              // 7
  { bg: 'bg-amber-100 dark:bg-amber-950/40', text: 'text-amber-700 dark:text-amber-300' },          // 8
  { bg: 'bg-rose-100 dark:bg-rose-950/40', text: 'text-rose-700 dark:text-rose-300' },              // 9
  { bg: 'bg-indigo-100 dark:bg-indigo-950/40', text: 'text-indigo-700 dark:text-indigo-300' },     // 10
  { bg: 'bg-lime-100 dark:bg-lime-950/40', text: 'text-lime-700 dark:text-lime-300' },              // 11
  { bg: 'bg-fuchsia-100 dark:bg-fuchsia-950/40', text: 'text-fuchsia-700 dark:text-fuchsia-300' }, // 12
];

/**
 * 13 主支出類 + 5 收入類的固定色 slot。
 * 對齊 wiki [[personal-finance-transaction-category-taxonomy]] § 5.1。
 * 注意：'其他' 在支出跟收入都會出現，皆 fallback 到 PALETTE[7] (teal 中性);
 * 不另設 ink 灰，因為 null category 已用 ink 灰，要區隔 explicit '其他' vs 沒分類.
 */
const CATEGORY_COLORS: Record<string, number> = {
  // === 13 支出主類 (COICOP 2018) ===
  飲食: 0,   // 橙
  酒菸: 8,   // 琥珀
  購物: 6,   // 黃
  居住: 4,   // 紫
  交通: 1,   // 天藍
  通訊: 5,   // 青
  娛樂: 3,   // 粉
  醫療: 9,   // 玫瑰
  教育: 10,  // 靛
  旅遊: 11,  // 萊姆
  金融: 12,  // 桃
  投資: 4,   // 紫 (同居住，但金額分區明顯不會混淆)
  其他: 7,   // 蒂芙尼
  // === 5 收入類 ===
  薪資: 2,         // 翠綠 (income 主色)
  獎金: 11,        // 萊姆 (income 第二色)
  利息股息: 2,     // 翠綠
  投資收益: 10,    // 靛 (區隔 income/投資成長)
};

/** djb2 hash — string → uint32 (deterministic fallback for unknown categories). */
function djb2(s: string): number {
  let h = 5381;
  for (let i = 0; i < s.length; i++) {
    h = ((h << 5) + h) ^ s.charCodeAt(i); // h * 33 ^ ch
  }
  return h >>> 0; // → unsigned
}

/** category → tailwind bg/text class pair. category=null → 中性灰. */
export function categoryColor(category: string | null | undefined): { bg: string; text: string } {
  if (!category) {
    return {
      bg: 'bg-ink-100 dark:bg-ink-800',
      text: 'text-ink-600 dark:text-ink-300',
    };
  }
  // 已知 taxonomy → fixed slot (絕無撞色)
  const fixed = CATEGORY_COLORS[category];
  if (fixed !== undefined) return PALETTE[fixed];
  // 未知 category → djb2 hash fallback (使用者自訂類別有 stable 色)
  const slot = djb2(category) % PALETTE.length;
  return PALETTE[slot];
}

/**
 * Phase 6 (category taxonomy 2026-06-15) — 13 主類 emoji 對映。
 *
 * 對齊 COICOP 2018 + 主計總處 2024 13 大類 (合併「酒菸獨立、飲食併餐飲」使用者拍板版).
 * 詳見 wiki [[personal-finance-transaction-category-taxonomy]] § 5.1.
 *
 * 注意:
 *   - 主類鎖死 13 個 + 5 個收入類; subcategory 由用戶自由填.
 *   - 未列入清單的 category (如 migration 前的舊類, 用戶自訂) fallback 到 '📦'.
 *   - 'subscription' 不是 category, 是 is_subscription flag, 對應的 emoji 在 UI 自己加.
 */
const CATEGORY_EMOJI: Record<string, string> = {
  // === 13 支出主類 (COICOP 2018) ===
  飲食: '🍱',
  酒菸: '🍺',
  購物: '🛍️',
  居住: '🏠',
  交通: '🚗',
  通訊: '📱',
  娛樂: '🎮',
  醫療: '🏥',
  教育: '📚',
  旅遊: '✈️',
  金融: '💰',
  投資: '💼',
  其他: '📦',
  // === 5 收入類 ===
  薪資: '💼',
  獎金: '🎁',
  利息股息: '💵',
  投資收益: '📈',
};

/** category → emoji. 未知 / null → 📦 (其他). */
export function categoryEmoji(category: string | null | undefined): string {
  if (!category) return '📦';
  return CATEGORY_EMOJI[category] ?? '📦';
}

/**
 * 13 主類清單 (給 UI dropdown / filter 用)。
 *
 * 2026-07-05 A 方案：不新增「日用」主類，但 UI 排序改成生活記帳常用順序，
 * 避免 byCategory insertion order 讓「飲食」跑到底。這不是 COICOP 原始順序；
 * COICOP 只作底層語意參考，UI 以高頻操作優先。
 */
export const EXPENSE_CATEGORIES = [
  '飲食', '購物', '交通', '居住', '通訊',
  '娛樂', '醫療', '教育', '旅遊', '金融',
  '投資', '酒菸', '其他',
] as const;

/** 5 收入類. */
export const INCOME_CATEGORIES = [
  '薪資', '獎金', '利息股息', '投資收益', '其他',
] as const;

const CATEGORY_DISPLAY_ORDER = [
  ...EXPENSE_CATEGORIES,
  ...INCOME_CATEGORIES.filter((c) => c !== '其他'),
] as const;

/** category → deterministic UI sort rank. Unknown user categories go after known taxonomy; 未分類 last. */
export function categorySortRank(category: string): number {
  if (category === '__null__') return 1_000_000;
  const idx = CATEGORY_DISPLAY_ORDER.indexOf(category as (typeof CATEGORY_DISPLAY_ORDER)[number]);
  return idx >= 0 ? idx : 10_000;
}

/** Sort category keys for chips/dropdowns with stable life-first order. */
export function sortCategoryKeys<T extends string>(keys: readonly T[]): T[] {
  return [...keys].sort((a, b) => {
    const rankDiff = categorySortRank(a) - categorySortRank(b);
    if (rankDiff !== 0) return rankDiff;
    return a.localeCompare(b, 'zh-Hant');
  });
}
