import ts from 'typescript';

import { formatTransactionMemo } from './txnDisplay';

function equal(actual: unknown, expected: unknown, message: string): void {
  if (actual !== expected) {
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

const modalPath = 'src/components/transactions/TxnDetailModal.tsx';
const modalSource = ts.sys.readFile(modalPath);
if (!modalSource) throw new Error(`cannot read ${modalPath}`);
if (!modalSource.includes('<DetailRow label="備註" value={transactionMemo} />')) {
  throw new Error('transaction detail modal does not wire the formatted bank memo to the memo row');
}
if (!modalSource.includes('flex-1 ml-4 text-right')) {
  throw new Error('transaction detail values cannot wrap within the modal width');
}

console.log('transaction memo display checks passed');
