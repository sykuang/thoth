import {
  activateReplicaOwner,
  assertReplicaOwnerEpoch,
  clearReplicaOwner,
  getReplicaOwnerEpoch,
  makeReplicaOwnerKey,
  ReplicaSyncCancelledError,
  syncReplica,
  updateReplicaPreferences,
  type ReplicaEnvelope,
  type ReplicaRequest,
  type ReplicaResponse,
  type ReplicaStore,
} from './replica';

function equal(actual: unknown, expected: unknown): void {
  if (!Object.is(actual, expected)) throw new Error(`expected ${String(expected)}, got ${String(actual)}`);
}

function deepEqual(actual: unknown, expected: unknown): void {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

class MemoryStore implements ReplicaStore {
  value: ReplicaEnvelope | undefined;
  clears = 0;

  async load(ownerKey: string) {
    return this.value?.ownerKey === ownerKey ? this.value : undefined;
  }

  async save(envelope: ReplicaEnvelope) {
    this.value = envelope;
  }

  async clear(ownerKey: string) {
    if (this.value?.ownerKey === ownerKey) this.value = undefined;
    this.clears += 1;
  }
}

async function main() {
  const ownerKey = makeReplicaOwnerKey('https://money.example/', 'A@Example.COM');
  const full: ReplicaResponse = {
    schema_version: 1,
    owner_id: 7,
    reset_required: false,
    generations: { user: 1 },
    partitions: [{ name: 'user', generation: 1, data: { marker: 'full' } }],
  };

  const emptyStore = new MemoryStore();
  const bootstrapCalls: string[] = [];
  const bootstrapped = await syncReplica(emptyStore, ownerKey, async (path) => {
    bootstrapCalls.push(path);
    return full;
  });
  deepEqual(bootstrapCalls, ['/replica/bootstrap']);
  equal((bootstrapped.partitions.user as { marker: string }).marker, 'full');
  equal(emptyStore.value?.ownerId, 7);

  const pullCalls: { path: string; body?: unknown }[] = [];
  const pulled = await syncReplica(emptyStore, ownerKey, async (path, body) => {
    pullCalls.push({ path, body });
    return {
      schema_version: 1,
      owner_id: 7,
      reset_required: false,
      generations: { user: 2 },
      partitions: [{ name: 'user', generation: 2, data: { marker: 'changed' } }],
    };
  });
  deepEqual(pullCalls, [{
    path: '/replica/pull',
    body: { schema_version: 1, generations: { user: 1 } },
  }]);
  equal((pulled.partitions.user as { marker: string }).marker, 'changed');
  equal(pulled.generations.user, 2);

  const resetCalls: string[] = [];
  const request: ReplicaRequest = async (path) => {
    resetCalls.push(path);
    if (path === '/replica/pull') {
      return {
        schema_version: 1,
        owner_id: 7,
        reset_required: true,
        generations: {} as Record<string, number>,
        partitions: [],
      };
    }
    return { ...full, generations: { user: 3 }, partitions: [
      { name: 'user', generation: 3, data: { marker: 'rebuilt' } },
    ] };
  };
  const resetResult = await syncReplica(emptyStore, ownerKey, request);
  deepEqual(resetCalls, ['/replica/pull', '/replica/bootstrap']);
  equal((resetResult.partitions.user as { marker: string }).marker, 'rebuilt');
  equal(emptyStore.clears, 1);

  const serializedStore = new MemoryStore();
  serializedStore.value = resetResult;
  let releaseFirst!: () => void;
  const firstGate = new Promise<void>((resolve) => { releaseFirst = resolve; });
  const pullBodies: unknown[] = [];
  let pullNumber = 0;
  const serializedRequest: ReplicaRequest = async (_path, body) => {
    pullNumber += 1;
    pullBodies.push(body);
    if (pullNumber === 1) await firstGate;
    const generation = pullNumber + 3;
    return {
      schema_version: 1,
      owner_id: 7,
      reset_required: false,
      generations: { user: generation },
      partitions: [{ name: 'user', generation, data: { marker: `g${generation}` } }],
    };
  };
  const firstSync = syncReplica(serializedStore, ownerKey, serializedRequest);
  const secondSync = syncReplica(serializedStore, ownerKey, serializedRequest);
  for (let i = 0; i < 20 && pullNumber === 0; i += 1) await Promise.resolve();
  equal(pullNumber, 1);
  releaseFirst();
  await Promise.all([firstSync, secondSync]);
  deepEqual(pullBodies, [
    { schema_version: 1, generations: { user: 3 } },
    { schema_version: 1, generations: { user: 4 } },
  ]);
  equal(serializedStore.value?.generations.user, 5);

  let releaseStale!: () => void;
  const staleGate = new Promise<void>((resolve) => { releaseStale = resolve; });
  let staleStarted = false;
  const staleSync = syncReplica(serializedStore, ownerKey, async () => {
    staleStarted = true;
    await staleGate;
    return {
      ...full,
      generations: { user: 6 },
      partitions: [{ name: 'user', generation: 6, data: { marker: 'stale' } }],
    };
  });
  while (!staleStarted) await Promise.resolve();
  const cleared = clearReplicaOwner(serializedStore, 'https://money.example/', 'A@Example.COM');
  releaseStale();
  let cancelled = false;
  await staleSync.catch((error: unknown) => {
    cancelled = error instanceof ReplicaSyncCancelledError;
  });
  await cleared;
  equal(cancelled, true);
  equal(serializedStore.value, undefined);

  let blockedRequestCalled = false;
  await syncReplica(serializedStore, ownerKey, async () => {
    blockedRequestCalled = true;
    return full;
  }).catch(() => undefined);
  equal(blockedRequestCalled, false);
  activateReplicaOwner(ownerKey);
  await syncReplica(serializedStore, ownerKey, async () => full);
  equal(serializedStore.value?.generations.user, 1);
  await updateReplicaPreferences(serializedStore, ownerKey, getReplicaOwnerEpoch(ownerKey), {
    fx_display_mode: 'always_twd',
    card_date_basis: 'post',
  });
  equal(
    ((serializedStore.value?.partitions.user as { preferences: { card_date_basis: string } })
      .preferences.card_date_basis),
    'post',
  );
  const stalePreferenceEpoch = getReplicaOwnerEpoch(ownerKey);
  await clearReplicaOwner(serializedStore, 'https://money.example/', 'A@Example.COM');
  activateReplicaOwner(ownerKey);
  await syncReplica(serializedStore, ownerKey, async () => full);
  let staleRequestCalls = 0;
  try {
    assertReplicaOwnerEpoch(ownerKey, stalePreferenceEpoch);
    staleRequestCalls += 1;
  } catch (error) {
    equal(error instanceof ReplicaSyncCancelledError, true);
  }
  equal(staleRequestCalls, 0);
  let stalePreferenceCancelled = false;
  await updateReplicaPreferences(serializedStore, ownerKey, stalePreferenceEpoch, {
    fx_display_mode: 'always_original',
    card_date_basis: 'post',
  }).catch((error: unknown) => {
    stalePreferenceCancelled = error instanceof ReplicaSyncCancelledError;
  });
  equal(stalePreferenceCancelled, true);
  equal(
    (serializedStore.value?.partitions.user as { preferences?: unknown }).preferences,
    undefined,
  );

  let rejectRequest!: (error: Error) => void;
  let rejectingRequestStarted = false;
  const rejectingSync = syncReplica(serializedStore, ownerKey, async () => {
    rejectingRequestStarted = true;
    return new Promise<ReplicaResponse>((_resolve, reject) => { rejectRequest = reject; });
  });
  while (!rejectingRequestStarted) await Promise.resolve();
  const clearRejectingOwner = clearReplicaOwner(
    serializedStore,
    'https://money.example/',
    'A@Example.COM',
  );
  rejectRequest(new Error('network failed after logout'));
  let rejectedAsCancellation = false;
  await rejectingSync.catch((error: unknown) => {
    rejectedAsCancellation = error instanceof ReplicaSyncCancelledError;
  });
  await clearRejectingOwner;
  equal(rejectedAsCancellation, true);
  equal(serializedStore.value, undefined);

  console.log('replica sync tests passed');
}

void main();
