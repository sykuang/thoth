import * as SQLite from 'expo-sqlite';

import type { ReplicaEnvelope, ReplicaStore } from './replica';

const DB_NAME = 'thoth-replica.db';
let databasePromise: ReturnType<typeof SQLite.openDatabaseAsync> | undefined;

async function database() {
  if (!databasePromise) {
    databasePromise = SQLite.openDatabaseAsync(DB_NAME).then(async (db) => {
      await db.execAsync(`
        PRAGMA journal_mode = WAL;
        CREATE TABLE IF NOT EXISTS replica_envelopes (
          owner_key TEXT PRIMARY KEY NOT NULL,
          payload_json TEXT NOT NULL
        );
      `);
      console.info('[replica-v1] native sqlite ready');
      return db;
    });
  }
  return databasePromise;
}

export const replicaStore: ReplicaStore = {
  async load(ownerKey) {
    const db = await database();
    const row = await db.getFirstAsync<{ payload_json: string }>(
      'SELECT payload_json FROM replica_envelopes WHERE owner_key = ?',
      ownerKey,
    );
    if (!row) return undefined;
    try {
      const envelope = JSON.parse(row.payload_json) as ReplicaEnvelope;
      return envelope.ownerKey === ownerKey ? envelope : undefined;
    } catch {
      await db.runAsync('DELETE FROM replica_envelopes WHERE owner_key = ?', ownerKey);
      return undefined;
    }
  },

  async save(envelope) {
    const db = await database();
    await db.withExclusiveTransactionAsync(async (transaction) => {
      await transaction.runAsync(
        `INSERT INTO replica_envelopes (owner_key, payload_json)
         VALUES (?, ?)
         ON CONFLICT(owner_key) DO UPDATE SET payload_json = excluded.payload_json`,
        envelope.ownerKey,
        JSON.stringify(envelope),
      );
    });
  },

  async clear(ownerKey) {
    const db = await database();
    await db.runAsync('DELETE FROM replica_envelopes WHERE owner_key = ?', ownerKey);
  },
};
