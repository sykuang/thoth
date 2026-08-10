import { useCallback, useMemo } from 'react';

import { api, type ApiInit } from '@/lib/api';
import {
  assertReplicaOwnerEpoch,
  getReplicaOwnerEpoch,
  guardReplicaOwnerRequest,
  makeReplicaOwnerKey,
  waitForReplicaOwner,
} from '@/lib/replica';
import { useAuthStore } from '@/stores/auth';

export function useOwnerBoundApi() {
  const token = useAuthStore((state) => state.token);
  const email = useAuthStore((state) => state.email) ?? '';
  const serverUrl = useAuthStore((state) => state.serverUrl);
  const ownerKey = useMemo(
    () => token && email && serverUrl ? makeReplicaOwnerKey(serverUrl, email) : '',
    [token, email, serverUrl],
  );
  const ownerEpoch = ownerKey ? getReplicaOwnerEpoch(ownerKey) : 0;

  const request = useCallback(async <T,>(path: string, init: ApiInit = {}): Promise<T> => {
    if (!ownerKey) throw new Error('Replica owner is unavailable');
    await waitForReplicaOwner(ownerKey, ownerEpoch);
    return guardReplicaOwnerRequest(
      ownerKey,
      ownerEpoch,
      () => api<T>(path, {
        ...init,
        authRetryKey: `${ownerKey}:${ownerEpoch}`,
        authRetryGuard: () => assertReplicaOwnerEpoch(ownerKey, ownerEpoch),
      }),
    );
  }, [ownerEpoch, ownerKey]);

  return { ownerKey, ownerEpoch, request };
}
