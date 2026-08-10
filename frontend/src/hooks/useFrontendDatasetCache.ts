import { useQuery } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { api } from '@/lib/api';
import { queryClient } from '@/lib/queryClient';
import {
  getReplicaOwnerEpoch,
  makeReplicaOwnerKey,
  projectReplicaDataset,
  REPLICA_SCHEMA_VERSION,
  ReplicaSyncCancelledError,
  syncReplica,
  updateReplicaDashboardCache,
  type ReplicaDashboardCache,
  type ReplicaResponse,
  type ReplicaTransactionDataset,
} from '@/lib/replica';
import { replicaStore } from '@/lib/replicaStore';
import { useAuthStore } from '@/stores/auth';


const hydratedOwners = new Set<string>();

export function useFrontendDatasetCache() {
  const token = useAuthStore((state) => state.token);
  const email = useAuthStore((state) => state.email) ?? '';
  const serverUrl = useAuthStore((state) => state.serverUrl);
  const ownerKey = useMemo(
    () => token && email && serverUrl ? makeReplicaOwnerKey(serverUrl, email) : '',
    [token, email, serverUrl],
  );
  const queryKey = useMemo(
    () => ['frontend-dataset', 'replica', ownerKey] as const,
    [ownerKey],
  );
  const [isSyncing, setIsSyncing] = useState(false);
  const synchronizedOwnerRef = useRef<string | null>(null);
  const syncCountRef = useRef(0);

  const requestReplica = useCallback(async (
    path: '/replica/bootstrap' | '/replica/pull',
    body?: { schema_version: number; generations: Record<string, number> },
  ): Promise<ReplicaResponse> => api<ReplicaResponse>(path, {
    method: path === '/replica/pull' ? 'POST' : 'GET',
    body,
  }), []);

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
      synchronizedOwnerRef.current = ownerKey;
      return dataset;
    } finally {
      syncCountRef.current -= 1;
      if (syncCountRef.current === 0) setIsSyncing(false);
    }
  }, [ownerKey, queryKey, requestReplica]);

  const persistDashboardCache = useCallback(async (
    dashboardCache: ReplicaDashboardCache,
  ): Promise<void> => {
    if (!ownerKey) return;
    await updateReplicaDashboardCache(
      replicaStore,
      ownerKey,
      getReplicaOwnerEpoch(ownerKey),
      dashboardCache,
    );
    queryClient.setQueryData<ReplicaTransactionDataset>(queryKey, (current) => (
      current ? { ...current, dashboardCache } : current
    ));
  }, [ownerKey, queryKey]);

  const replicaQ = useQuery<ReplicaTransactionDataset>({
    queryKey,
    enabled: Boolean(ownerKey),
    queryFn: async () => {
      const firstHydration = !hydratedOwners.has(ownerKey);
      hydratedOwners.add(ownerKey);
      if (firstHydration) {
        const persisted = await replicaStore.load(ownerKey);
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
        const persisted = await replicaStore.load(ownerKey);
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
    if (!ownerKey || !replicaQ.data || synchronizedOwnerRef.current === ownerKey) return;
    void refreshReplica().catch(() => {
      // Offline/read failure keeps the last owner-scoped local replica visible.
    });
  }, [ownerKey, replicaQ.data, refreshReplica]);

  return {
    ...replicaQ,
    ownerKey,
    isRefetching: replicaQ.isRefetching || isSyncing,
    refreshSnapshot: refreshReplica,
    refreshChanges: refreshReplica,
    isRefreshingChanges: isSyncing,
    persistDashboardCache,
  };
}
