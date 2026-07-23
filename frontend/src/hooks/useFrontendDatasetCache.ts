import { useQuery } from '@tanstack/react-query';

import { api } from '@/lib/api';
import type { FrontendDatasetCache } from '@/types/api';

const STORAGE_KEY = 'thoth.frontendDataset.v1';

function storageAvailable(): boolean {
  return typeof globalThis.localStorage?.getItem === 'function'
    && typeof globalThis.localStorage?.setItem === 'function';
}

function loadPersisted(): FrontendDatasetCache | undefined {
  if (!storageAvailable()) return undefined;
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return undefined;
    const parsed = JSON.parse(raw) as FrontendDatasetCache;
    if (!parsed?.cursor || !Array.isArray(parsed.transactions)) return undefined;
    return parsed;
  } catch {
    return undefined;
  }
}

function persist(data: FrontendDatasetCache): void {
  if (!storageAvailable()) return;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
  } catch {
    // best-effort cache persistence; React Query memory cache remains source.
  }
}

export function useFrontendDatasetCache() {
  const snapshotQ = useQuery<FrontendDatasetCache>({
    queryKey: ['frontend-dataset'],
    queryFn: async () => {
      const data = await api<FrontendDatasetCache>('/cache/snapshot');
      persist(data);
      return data;
    },
    initialData: loadPersisted,
    staleTime: 5 * 60 * 1000,
    gcTime: 30 * 60 * 1000,
  });

  return {
    ...snapshotQ,
    refreshSnapshot: snapshotQ.refetch,
    // Frontend filtering contract: fetch the whole DB snapshot, then all screen
    // filters are local state only.  Keep these aliases so older screens/tests do
    // not accidentally reintroduce scoped backend fetches.
    refreshChanges: snapshotQ.refetch,
    isRefreshingChanges: false,
  };
}
