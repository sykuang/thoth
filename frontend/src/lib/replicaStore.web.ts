import type { ReplicaEnvelope, ReplicaStore } from './replica';

const DATABASE_NAME = 'thoth-replica';
const STORE_NAME = 'replicas';

try {
  if (typeof globalThis.localStorage?.removeItem === 'function') {
    localStorage.removeItem('thoth.frontendDataset.v1');
  }
} catch {
  // Storage can be disabled by browser privacy policy; IndexedDB may still work.
}

function requestResult<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error('IndexedDB request failed'));
  });
}

function transactionDone(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error ?? new Error('IndexedDB transaction failed'));
    transaction.onabort = () => reject(transaction.error ?? new Error('IndexedDB transaction aborted'));
  });
}

let databasePromise: Promise<IDBDatabase> | undefined;
function database(): Promise<IDBDatabase> {
  if (!databasePromise) {
    databasePromise = new Promise((resolve, reject) => {
      const request = indexedDB.open(DATABASE_NAME, 1);
      request.onupgradeneeded = () => {
        if (!request.result.objectStoreNames.contains(STORE_NAME)) {
          request.result.createObjectStore(STORE_NAME, { keyPath: 'ownerKey' });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error ?? new Error('IndexedDB open failed'));
    });
  }
  return databasePromise;
}

export const replicaStore: ReplicaStore = {
  async load(ownerKey) {
    if (typeof indexedDB === 'undefined') return undefined;
    const db = await database();
    const transaction = db.transaction(STORE_NAME, 'readonly');
    const value = await requestResult(
      transaction.objectStore(STORE_NAME).get(ownerKey) as IDBRequest<ReplicaEnvelope | undefined>,
    );
    await transactionDone(transaction);
    return value?.ownerKey === ownerKey ? value : undefined;
  },

  async save(envelope) {
    if (typeof indexedDB === 'undefined') return;
    const db = await database();
    const transaction = db.transaction(STORE_NAME, 'readwrite');
    transaction.objectStore(STORE_NAME).put(envelope);
    await transactionDone(transaction);
  },

  async clear(ownerKey) {
    if (typeof indexedDB === 'undefined') return;
    const db = await database();
    const transaction = db.transaction(STORE_NAME, 'readwrite');
    transaction.objectStore(STORE_NAME).delete(ownerKey);
    await transactionDone(transaction);
  },
};
