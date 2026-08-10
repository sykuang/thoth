import { useQuery } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { queryClient } from '@/lib/queryClient';
import {
  assertReplicaOwnerEpoch,
  guardReplicaOwnerRequest,
  patchReplicaAccountTabCache,
  projectReplicaDataset,
  REPLICA_SCHEMA_VERSION,
  ReplicaSyncCancelledError,
  syncReplica,
  updateReplicaAccountTabCache,
  updateReplicaDashboardCache,
  waitForReplicaOwner,
  type ReplicaAccountTabCache,
  type ReplicaDashboardCache,
  type ReplicaResponse,
  type ReplicaTransactionDataset,
} from '@/lib/replica';
import { useOwnerBoundApi } from '@/hooks/useOwnerBoundApi';
import { replicaStore } from '@/lib/replicaStore';


const hydratedOwners = new Set<string>();

export function useFrontendDatasetCache() {
  const { ownerKey, ownerEpoch, request } = useOwnerBoundApi();
  const ownerSessionKey = `${ownerKey}#${ownerEpoch}`;
  const queryKey = useMemo(
    () => ['frontend-dataset', 'replica', ownerKey, ownerEpoch] as const,
    [ownerEpoch, ownerKey],
  );
  const [isSyncing, setIsSyncing] = useState(false);
  const synchronizedOwnerRef = useRef<string | null>(null);
  const syncCountRef = useRef(0);

  const requestReplica = useCallback(async (
    path: '/replica/bootstrap' | '/replica/pull',
    body?: { schema_version: number; generations: Record<string, number> },
  ): Promise<ReplicaResponse> => request<ReplicaResponse>(path, {
    method: path === '/replica/pull' ? 'POST' : 'GET',
    body,
  }), [request]);

  const refreshReplica = useCallback(async (): Promise<ReplicaTransactionDataset> => {
    if (!ownerKey) throw new Error('Replica owner is unavailable');
    syncCountRef.current += 1;
    setIsSyncing(true);
    try {
      const envelope = await syncReplica(replicaStore, ownerKey, requestReplica);
      const dataset = projectReplicaDataset(envelope);
      console.info(
        `[replica-v1] synced owner_id=${envelope.ownerId} partitions=${Object.keys(envelope.partitions).length} transactions=${dataset.transactions.length}`,
      );
      queryClient.setQueryData(queryKey, dataset);
      synchronizedOwnerRef.current = ownerSessionKey;
      return dataset;
    } finally {
      syncCountRef.current -= 1;
      if (syncCountRef.current === 0) setIsSyncing(false);
    }
  }, [ownerKey, ownerSessionKey, queryKey, requestReplica]);

  const persistDashboardCache = useCallback(async (
    dashboardCache: ReplicaDashboardCache,
    expectedEpoch: number,
  ): Promise<void> => {
    if (!ownerKey) return;
    await updateReplicaDashboardCache(
      replicaStore,
      ownerKey,
      expectedEpoch,
      dashboardCache,
    );
    assertReplicaOwnerEpoch(ownerKey, expectedEpoch);
    queryClient.setQueryData<ReplicaTransactionDataset>(queryKey, (current) => (
      current ? { ...current, dashboardCache } : current
    ));
  }, [ownerKey, queryKey]);

  const persistAccountTabCache = useCallback(async (
    accountTabCache: ReplicaAccountTabCache,
    expectedEpoch: number,
  ): Promise<void> => {
    if (!ownerKey) return;
    await updateReplicaAccountTabCache(
      replicaStore,
      ownerKey,
      expectedEpoch,
      accountTabCache,
    );
    assertReplicaOwnerEpoch(ownerKey, expectedEpoch);
    queryClient.setQueryData<ReplicaTransactionDataset>(queryKey, (current) => (
      current ? { ...current, accountTabCache } : current
    ));
  }, [ownerKey, queryKey]);

  const persistAccountTabCacheUpdate = useCallback(async (
    updater: (cache: ReplicaAccountTabCache) => ReplicaAccountTabCache,
    expectedEpoch: number,
  ): Promise<void> => {
    if (!ownerKey) return;
    await patchReplicaAccountTabCache(replicaStore, ownerKey, expectedEpoch, updater);
    assertReplicaOwnerEpoch(ownerKey, expectedEpoch);
    queryClient.setQueryData<ReplicaTransactionDataset>(queryKey, (current) => (
      current?.accountTabCache
        ? { ...current, accountTabCache: updater(current.accountTabCache) }
        : current
    ));
  }, [ownerKey, queryKey]);

  const replicaQ = useQuery<ReplicaTransactionDataset>({
    queryKey,
    enabled: Boolean(ownerKey),
    queryFn: async () => {
      await waitForReplicaOwner(ownerKey, ownerEpoch);
      const firstHydration = !hydratedOwners.has(ownerSessionKey);
      hydratedOwners.add(ownerSessionKey);
      if (firstHydration) {
        const persisted = await guardReplicaOwnerRequest(
          ownerKey,
          ownerEpoch,
          () => replicaStore.load(ownerKey),
        );
        if (persisted?.schemaVersion === REPLICA_SCHEMA_VERSION) {
          const dataset = projectReplicaDataset(persisted);
          console.info(
            `[replica-v1] hydrated owner_id=${persisted.ownerId} transactions=${dataset.transactions.length}`,
          );
          return dataset;
        }
      }
      try {
        return await refreshReplica();
      } catch (syncError) {
        if (syncError instanceof ReplicaSyncCancelledError) throw syncError;
        const persisted = await guardReplicaOwnerRequest(
          ownerKey,
          ownerEpoch,
          () => replicaStore.load(ownerKey),
        );
        if (persisted?.schemaVersion === REPLICA_SCHEMA_VERSION) {
          return projectReplicaDataset(persisted);
        }
        throw syncError;
      }
    },
    staleTime: Infinity,
    gcTime: 30 * 60 * 1000,
  });

  useEffect(() => {
    if (!ownerKey || !replicaQ.data || synchronizedOwnerRef.current === ownerSessionKey) return;
    void refreshReplica().catch(() => {
      // Offline/read failure keeps the last owner-scoped local replica visible.
    });
  }, [ownerKey, ownerSessionKey, replicaQ.data, refreshReplica]);

  return {
    ...replicaQ,
    ownerKey,
    ownerEpoch,
    ownerApi: request,
    isRefetching: replicaQ.isRefetching || isSyncing,
    refreshSnapshot: refreshReplica,
    refreshChanges: refreshReplica,
    isRefreshingChanges: isSyncing,
    persistDashboardCache,
    persistAccountTabCache,
    persistAccountTabCacheUpdate,
  };
}
