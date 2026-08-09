export const ROUTE_PARENTS = {
  '+not-found': '/',
  '(tabs)/cards/add': '/(tabs)/cards',
  '(tabs)/cards/new': '/(tabs)/cards/add',
  '(tabs)/cards/credentials/[bank]': '/(tabs)/cards',
  '(tabs)/cards/[bank]/[card_no]': '/(tabs)/cards',
  '(tabs)/cards/brokerage/[account_id]': '/(tabs)/cards',
  '(tabs)/cards/manual/[account_id]': '/(tabs)/cards',
  '(tabs)/cards/manual/transaction': '/(tabs)/cards/manual/[account_id]',
  '(tabs)/settings/categories': '/(tabs)/settings',
  '(tabs)/settings/labels': '/(tabs)/settings',
  '(tabs)/settings/auto-sync': '/(tabs)/settings',
} as const;

export function manualAccountParent(accountId: string) {
  return accountId === 'new'
    ? ROUTE_PARENTS['(tabs)/cards/new']
    : ROUTE_PARENTS['(tabs)/cards/manual/[account_id]'];
}

export function manualTransactionParent(accountId: string) {
  return accountId
    ? {
        pathname: ROUTE_PARENTS['(tabs)/cards/manual/transaction'],
        params: { account_id: accountId },
      } as const
    : ROUTE_PARENTS['(tabs)/cards/manual/[account_id]'];
}

export function manualTransactionReturnParent(
  requestedAccountId: string,
  resolvedAccountId: string | undefined,
  transactionRequested: boolean,
  transactionFound: boolean,
  routeIsValidated: boolean,
) {
  if (!routeIsValidated) return manualTransactionParent(requestedAccountId);
  return manualTransactionParent(
    resolvedAccountId && (!transactionRequested || transactionFound)
      ? resolvedAccountId
      : '',
  );
}
