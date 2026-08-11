type NotificationData = Record<string, unknown>;
type QueryKey = readonly unknown[];
type QueryInvalidator = {
  invalidateQueries: (filters: { queryKey: QueryKey }) => unknown;
};

const SYNC_KINDS = new Set([
  'sync_done',
  'sync_failed',
  'sync_all_done',
  'sync_all_failed',
]);

const ACCOUNT_REFRESH_QUERY_KEYS: QueryKey[] = [
  ['sync', 'jobs'],
  ['transactions'],
  ['frontend-dataset'],
  ['portfolio'],
  ['cards'],
  ['accounts'],
  ['auto-debit', 'reminders'],
];

export function invalidateAccountQueries(queryClient: QueryInvalidator): void {
  for (const queryKey of ACCOUNT_REFRESH_QUERY_KEYS) {
    void queryClient.invalidateQueries({ queryKey });
  }
}

export function invalidateSyncNotificationQueries(
  queryClient: QueryInvalidator,
  data: NotificationData,
): void {
  if (typeof data.kind === 'string' && SYNC_KINDS.has(data.kind)) {
    invalidateAccountQueries(queryClient);
  }
}
