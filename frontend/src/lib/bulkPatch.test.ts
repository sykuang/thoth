import { applyBulkPatch } from './bulkPatch';
import type { Transaction } from '@/types/api';

const eq = (actual: unknown, expected: unknown) => {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) throw new Error(`${JSON.stringify(actual)} !== ${JSON.stringify(expected)}`);
};

const t = { bank: 'esun', kind: 'twd', id: 1, category: '飲食', subcategory: '早餐', tags: ['a'] } as unknown as Transaction;
eq(applyBulkPatch(t, { category: '交通' }), { ...t, category: '交通' });
eq(applyBulkPatch(t, { subcategory: '' }).subcategory, null);
eq(applyBulkPatch(t, { tags: ['a', 'b'], tags_mode: 'add' }).tags, ['a', 'b']);
eq(applyBulkPatch(t, { tags: ['c'], tags_mode: 'replace' }).tags, ['c']);
eq(t.category, '飲食');
console.log('bulkPatch ok');
