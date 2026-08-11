import {
  invalidateAccountQueries,
  invalidateSyncNotificationQueries,
} from './syncRefresh';

function equal(actual: unknown, expected: unknown): void {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

const expected = [
  ['sync', 'jobs'],
  ['transactions'],
  ['frontend-dataset'],
  ['portfolio'],
  ['cards'],
  ['accounts'],
  ['auto-debit', 'reminders'],
];

function collect(run: (client: { invalidateQueries: (filters: { queryKey: readonly unknown[] }) => void }) => void) {
  const keys: (readonly unknown[])[] = [];
  run({ invalidateQueries: ({ queryKey }) => { keys.push(queryKey); } });
  return keys;
}

equal(collect((client) => invalidateAccountQueries(client)), expected);
equal(collect((client) => invalidateSyncNotificationQueries(client, { kind: 'sync_done' })), expected);
equal(collect((client) => invalidateSyncNotificationQueries(client, { kind: 'sync_all_failed' })), expected);
equal(collect((client) => invalidateSyncNotificationQueries(client, { kind: 'payment_reminder' })), []);
equal(collect((client) => invalidateSyncNotificationQueries(client, {})), []);

console.log('sync refresh query invalidation tests passed');
