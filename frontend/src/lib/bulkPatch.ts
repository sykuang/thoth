import type { Transaction } from '@/types/api';

/** Mirror the single-row PATCH semantics: '' clears, tags_mode add unions. */
export function applyBulkPatch(t: Transaction, patch: Record<string, unknown>): Transaction {
  const next = { ...t };
  if ('category' in patch) next.category = (patch.category as string) || null;
  if ('subcategory' in patch) next.subcategory = (patch.subcategory as string) || null;
  if ('tags' in patch) {
    const tags = patch.tags as string[];
    next.tags = patch.tags_mode === 'add' ? [...new Set([...(t.tags ?? []), ...tags])] : tags;
  }
  return next;
}

