import {
  consumeTerminalSyncJobIds,
  deriveAccountTabLoadStatus,
  fetchCompleteAccountTabCache,
  hasNewerAccountTabRevision,
  updateCachedBankBalance,
  updateCachedCard,
  updateCachedManualAccount,
} from './accountTabCache';

function equal(actual: unknown, expected: unknown): void {
  if (!Object.is(actual, expected)) throw new Error(`expected ${String(expected)}, got ${String(actual)}`);
}

async function main() {
  let releaseManual!: (value: { id: string }[]) => void;
  const manualGate = new Promise<{ id: string }[]>((resolve) => { releaseManual = resolve; });
  let settled = false;
  const complete = fetchCompleteAccountTabCache({
    balances: async () => [{ bank: 'cathay', account_no: '1234' }] as never,
    accounts: async () => [{ id: 1, has_creds: true }] as never,
    cards: async () => [{ bank: 'cathay', card_no: '9999' }] as never,
    manualAccounts: async () => manualGate as never,
    now: () => '2026-08-10T03:00:00Z',
  });
  void complete.then(() => { settled = true; });
  await Promise.resolve();
  equal(settled, false);
  releaseManual([{ id: 'manual-1' }]);
  const cache = await complete;
  equal(cache.cachedAt, '2026-08-10T03:00:00Z');
  equal(cache.balances[0]?.account_no, '1234');
  equal(cache.accounts[0]?.id, 1);
  equal(cache.cards[0]?.card_no, '9999');
  equal(cache.manualAccounts[0]?.id, 'manual-1');
  const updatedBalance = updateCachedBankBalance(cache, 'cathay', '1234', { excluded: true });
  equal(updatedBalance.balances[0]?.excluded, true);
  equal(cache.balances[0]?.excluded, undefined);
  const updatedCard = updateCachedCard(cache, 'cathay', '9999', { excluded: true });
  equal(updatedCard.cards[0]?.excluded, true);
  const updatedManual = updateCachedManualAccount(cache, 'manual-1', {
    included_in_net_worth: false,
  });
  equal(updatedManual.manualAccounts[0]?.included_in_net_worth, false);

  let rejected = false;
  await fetchCompleteAccountTabCache({
    balances: async () => [] as never,
    accounts: async () => [] as never,
    cards: async () => { throw new Error('cards unavailable'); },
    manualAccounts: async () => [] as never,
    now: () => 'must-not-be-used',
  }).catch(() => { rejected = true; });
  equal(rejected, true);
  equal(hasNewerAccountTabRevision([2, 1, 1, 1], [1, 1, 1, 1]), true);
  equal(hasNewerAccountTabRevision([1, 1, 1, 1], [1, 1, 1, 1]), false);

  const emptyCache = {
    ...cache,
    balances: [],
    accounts: [],
    cards: [],
    manualAccounts: [],
  };
  equal(deriveAccountTabLoadStatus(
    emptyCache,
    { isFetched: false, isError: false },
    [{ isPending: true, isError: false }],
  ), 'ready');
  equal(deriveAccountTabLoadStatus(
    undefined,
    { isFetched: false, isError: false },
    [{ isPending: false, isError: false }],
  ), 'loading');
  equal(deriveAccountTabLoadStatus(
    undefined,
    { isFetched: true, isError: false },
    [{ isPending: true, isError: false }],
  ), 'loading');
  equal(deriveAccountTabLoadStatus(
    undefined,
    { isFetched: true, isError: false },
    [{ isPending: false, isError: true }],
  ), 'error');

  const queued = consumeTerminalSyncJobIds(
    new Set([7]),
    [{ id: 7, status: 'queued' }],
  );
  equal(queued.reachedTerminalState, false);
  equal(queued.remaining.has(7), true);
  const completedWithoutObservedRunning = consumeTerminalSyncJobIds(
    queued.remaining,
    [{ id: 7, status: 'done' }],
  );
  equal(completedWithoutObservedRunning.reachedTerminalState, true);
  equal(completedWithoutObservedRunning.remaining.size, 0);

  console.log('account tab cache coordination tests passed');
}

void main();
