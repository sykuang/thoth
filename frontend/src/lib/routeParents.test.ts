import {
  manualAccountParent,
  manualTransactionParent,
  manualTransactionReturnParent,
} from './routeParents';
import ts from 'typescript';

function assertJson(actual: unknown, expected: unknown): void {
  const actualJson = JSON.stringify(actual);
  const expectedJson = JSON.stringify(expected);
  if (actualJson !== expectedJson) {
    throw new Error(`expected ${expectedJson}, got ${actualJson}`);
  }
}

assertJson(manualAccountParent('new'), '/(tabs)/cards/add');
assertJson(manualAccountParent('manual-7'), '/(tabs)/cards');
assertJson(manualTransactionParent(''), '/(tabs)/cards');
assertJson(manualTransactionParent('manual-7'), {
  pathname: '/(tabs)/cards/manual/[account_id]',
  params: { account_id: 'manual-7' },
});
assertJson(
  manualTransactionReturnParent('manual-7', undefined, true, false, false),
  { pathname: '/(tabs)/cards/manual/[account_id]', params: { account_id: 'manual-7' } },
);
assertJson(
  manualTransactionReturnParent('manual-7', 'manual-7', false, false, true),
  { pathname: '/(tabs)/cards/manual/[account_id]', params: { account_id: 'manual-7' } },
);
assertJson(
  manualTransactionReturnParent('manual-7', 'manual-7', true, true, true),
  { pathname: '/(tabs)/cards/manual/[account_id]', params: { account_id: 'manual-7' } },
);
assertJson(manualTransactionReturnParent('manual-7', 'manual-7', true, false, true), '/(tabs)/cards');
assertJson(manualTransactionReturnParent('manual-7', undefined, false, false, true), '/(tabs)/cards');

const pagePath = 'src/app/(tabs)/cards/manual/transaction.tsx';
const pageSource = ts.sys.readFile(pagePath);
if (!pageSource) throw new Error(`cannot read ${pagePath}`);
const page = ts.createSourceFile(pagePath, pageSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
let liveCall: ts.CallExpression | undefined;
function visit(node: ts.Node): void {
  if (
    ts.isCallExpression(node)
    && ts.isIdentifier(node.expression)
    && node.expression.text === 'manualTransactionReturnParent'
  ) {
    liveCall = node;
  }
  ts.forEachChild(node, visit);
}
visit(page);
assertJson(liveCall?.arguments.map((arg) => arg.getText(page)), [
  'accountId',
  'account?.id',
  'transactionId != null',
  'transactionIdIsValid && initialTransaction != null',
  'routeIsValidated',
]);

console.log('route parent contract tests passed');
