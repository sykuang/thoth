import type { ReplicaEnvelope, ReplicaStore } from './replica';

const values = new Map<string, ReplicaEnvelope>();

export const replicaStore: ReplicaStore = {
  async load(ownerKey) {
    return values.get(ownerKey);
  },
  async save(envelope) {
    values.set(envelope.ownerKey, envelope);
  },
  async clear(ownerKey) {
    values.delete(ownerKey);
  },
};
