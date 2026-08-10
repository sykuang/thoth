import type {
  BankAccount,
  BankAccountBalance,
  Card,
  FinancialAccount,
} from '@/types/api';

import type { ReplicaAccountTabCache } from './replica';

type AccountTabCacheFetchers = {
  balances: () => Promise<BankAccountBalance[]>;
  accounts: () => Promise<BankAccount[]>;
  cards: () => Promise<Card[]>;
  manualAccounts: () => Promise<FinancialAccount[]>;
  now?: () => string;
};

export type AccountTabRevisionTuple = [number, number, number, number];
export type AccountTabLoadStatus = 'ready' | 'loading' | 'error';

export function deriveAccountTabLoadStatus(
  cache: ReplicaAccountTabCache | undefined,
  dataset: { isFetched: boolean; isError: boolean },
  queries: { isPending: boolean; isError: boolean }[],
): AccountTabLoadStatus {
  if (cache) return 'ready';
  if ((!dataset.isFetched && !dataset.isError) || queries.some((query) => query.isPending)) {
    return 'loading';
  }
  return dataset.isError || queries.some((query) => query.isError) ? 'error' : 'loading';
}

export function consumeTerminalSyncJobIds(
  tracked: ReadonlySet<number>,
  jobs: { id: number; status: string }[],
): { remaining: Set<number>; reachedTerminalState: boolean } {
  const remaining = new Set(tracked);
  let reachedTerminalState = false;
  for (const job of jobs) {
    if (!remaining.has(job.id) || (job.status !== 'done' && job.status !== 'failed')) continue;
    remaining.delete(job.id);
    reachedTerminalState = true;
  }
  return { remaining, reachedTerminalState };
}

export function updateCachedBankBalance(
  cache: ReplicaAccountTabCache,
  bank: string,
  accountNo: string,
  changes: Partial<BankAccountBalance>,
): ReplicaAccountTabCache {
  return {
    ...cache,
    balances: cache.balances.map((row) => row.bank === bank && row.account_no === accountNo
      ? { ...row, ...changes }
      : row),
  };
}

export function updateCachedCard(
  cache: ReplicaAccountTabCache,
  bank: string,
  cardNo: string,
  changes: Partial<Card>,
): ReplicaAccountTabCache {
  return {
    ...cache,
    cards: cache.cards.map((row) => row.bank === bank && row.card_no === cardNo
      ? { ...row, ...changes }
      : row),
  };
}

export function updateCachedManualAccount(
  cache: ReplicaAccountTabCache,
  accountId: string,
  changes: Partial<FinancialAccount>,
): ReplicaAccountTabCache {
  return {
    ...cache,
    manualAccounts: cache.manualAccounts.map((row) => row.id === accountId
      ? { ...row, ...changes }
      : row),
  };
}

export function hasNewerAccountTabRevision(
  current: AccountTabRevisionTuple,
  complete: AccountTabRevisionTuple,
): boolean {
  return current.some((revision, index) => revision > complete[index]);
}

export async function fetchCompleteAccountTabCache({
  balances,
  accounts,
  cards,
  manualAccounts,
  now = () => new Date().toISOString(),
}: AccountTabCacheFetchers): Promise<ReplicaAccountTabCache> {
  const [balanceRows, accountRows, cardRows, manualRows] = await Promise.all([
    balances(),
    accounts(),
    cards(),
    manualAccounts(),
  ]);
  return {
    cachedAt: now(),
    balances: balanceRows,
    accounts: accountRows,
    cards: cardRows,
    manualAccounts: manualRows,
  };
}
