import ts from 'typescript';

import { formatTransactionMemo, getDisplayDescription } from './txnDisplay';

function equal(actual: unknown, expected: unknown, message: string): void {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`${message}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

equal(
  formatTransactionMemo('  0050FUND        基金配息                    '),
  '0050FUND 基金配息',
  'bank memo whitespace should be normalized for display',
);
equal(formatTransactionMemo('　0050FUND　基金配息　'), '0050FUND 基金配息', 'full-width spaces');
equal(formatTransactionMemo('   '), null, 'blank memo should stay hidden');
equal(formatTransactionMemo(null), null, 'missing memo should stay hidden');
equal(formatTransactionMemo(undefined), null, 'undefined memo should stay hidden');

equal(
  getDisplayDescription({ description: '轉帳', memo: '  0050FUND        基金配息  ' }),
  ['轉帳 - 0050FUND 基金配息', false],
  'raw description and bank memo should share one description',
);
equal(
  getDisplayDescription({ description: '轉帳', display_description: '轉帳 · 王小明', memo: '基金配息' }),
  ['轉帳 · 王小明 - 基金配息', false],
  'joined counterparty description should retain memo evidence',
);
equal(
  getDisplayDescription({ description: null, display_description: '0050FUND', memo: '0050FUND 基金配息' }),
  ['0050FUND 基金配息', false],
  'memo fallback should not duplicate its leading token',
);
equal(
  getDisplayDescription({
    description: null,
    display_description: 'ABCDEFGHIJKLMNOPQRSTUVWXYZ1234',
    memo: 'ABCDEFGHIJKLMNOPQRSTUVWXYZ123456789 基金配息',
  }),
  ['ABCDEFGHIJKLMNOPQRSTUVWXYZ123456789 基金配息', false],
  'truncated memo fallback token should not be duplicated',
);
equal(
  getDisplayDescription({ description: '轉帳', description_overwrite: '皇上自訂', memo: '基金配息' }),
  ['皇上自訂', true],
  'user overwrite should replace the combined bank description',
);

const modalPath = 'src/components/transactions/TxnDetailModal.tsx';
const modalSource = ts.sys.readFile(modalPath);
if (!modalSource) throw new Error(`cannot read ${modalPath}`);
const rawDescriptionBinding = `const [rawDisplayDescription] = getDisplayDescription({
    ...txn,
    description_overwrite: null,
  });`;
function assertModalDescriptionContract(source: string): void {
  if (source.includes('<DetailRow label="備註"')) {
    throw new Error('bank memo must not render as a separate detail row');
  }
  if (!source.includes(rawDescriptionBinding)) {
    throw new Error('combined raw description is not derived from the shared display helper');
  }
  if (!source.includes('原文: {rawDisplayDescription}')) {
    throw new Error('overwritten description does not disclose the combined bank original');
  }
  if (!source.includes('placeholder={rawDisplayDescription}')) {
    throw new Error('description editor does not expose the combined raw description and memo');
  }
}
assertModalDescriptionContract(modalSource);

const brokenModalSource = modalSource.replace(
  rawDescriptionBinding,
  'const rawDisplayDescription = txn.description;',
);
if (brokenModalSource === modalSource) throw new Error('modal negative-control mutation did not apply');
let rejectedBrokenModal = false;
try {
  assertModalDescriptionContract(brokenModalSource);
} catch {
  rejectedBrokenModal = true;
}
if (!rejectedBrokenModal) throw new Error('modal contract accepted a raw-description-only mutation');

console.log('transaction description display checks passed');
