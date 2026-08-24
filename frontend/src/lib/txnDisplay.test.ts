import ts from 'typescript';

import { formatTransactionSource, getDisplayDescription } from './txnDisplay';

function equal(actual: unknown, expected: unknown, message: string): void {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`${message}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}


equal(
  getDisplayDescription({ description: '轉帳 - 0050FUND 基金配息', memo: '0050FUND 基金配息' }),
  ['轉帳 - 0050FUND 基金配息', false],
  'frontend should use the canonical description already persisted by the database',
);
equal(
  getDisplayDescription({ description: '轉帳', display_description: '轉帳 - 0050FUND 基金配息', memo: '基金配息' }),
  ['轉帳 - 0050FUND 基金配息', false],
  'database display description should be authoritative without another UI join',
);
equal(
  getDisplayDescription({ description: null, display_description: '0050FUND', memo: '0050FUND 基金配息' }),
  ['0050FUND', false],
  'frontend must not expand a database fallback token from memo',
);
equal(
  getDisplayDescription({
    description: null,
    display_description: 'ABCDEFGHIJKLMNOPQRSTUVWXYZ1234',
    memo: 'ABCDEFGHIJKLMNOPQRSTUVWXYZ123456789 基金配息',
  }),
  ['ABCDEFGHIJKLMNOPQRSTUVWXYZ1234', false],
  'frontend must not reconstruct a truncated database token',
);
equal(
  getDisplayDescription({ description: '轉帳', description_overwrite: '皇上自訂', memo: '基金配息' }),
  ['皇上自訂', true],
  'user overwrite should replace the combined bank description',
);

equal(
  formatTransactionSource('富邦銀行', {
    kind: 'billed',
    accountNo: null,
    accountOrCard: '900051******7021',
  }),
  '富邦銀行 - 7021',
  'credit-card source should combine the bank and bare card last four digits',
);
equal(
  formatTransactionSource('中國信託', {
    kind: 'twd',
    accountNo: ' 1234567890123456 ',
    accountOrCard: '****3456',
  }),
  '中國信託 - 1234567890123456',
  'deposit-account source should display the complete canonical account number',
);
equal(
  formatTransactionSource('富邦銀行', {
    kind: 'pending',
    accountNo: null,
    accountOrCard: null,
  }),
  '富邦銀行',
  'transaction source should keep the bank-only fallback when no account or card is available',
);

const modalPath = 'src/components/transactions/TxnDetailModal.tsx';
const modalSource = ts.sys.readFile(modalPath);
if (!modalSource) throw new Error(`cannot read ${modalPath}`);
const rawDescriptionBinding = `const [rawDisplayDescription] = getDisplayDescription({
    ...txn,
    description_overwrite: null,
  });`;
function assertModalDescriptionContract(source: string): void {
  if (!source.includes('formatTransactionSource(\n                    BANK_LABELS[txn.bank as SupportedBank] ?? txn.bank,\n                    {\n                      kind: txn.kind,\n                      accountNo: txn.account_no,\n                      accountOrCard: txn.account_or_card,\n                    },\n                  )')) {
    throw new Error('transaction detail subtitle does not combine bank with account/card source');
  }
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
