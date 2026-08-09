"""Versioned frontend replica partition reconciliation."""
from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from backend.server import db

_sqlite_reconcile_thread_lock = threading.Lock()


@dataclass(frozen=True)
class ReplicaPartition:
    name: str
    generation: int
    data: dict[str, Any]


def _persist_partitions(
    user_id: int,
    payloads: dict[str, dict[str, Any]],
) -> list[ReplicaPartition]:
    out: list[ReplicaPartition] = []
    with db.get_conn() as conn:
        for name in sorted(payloads):
            data = payloads[name]
            canonical_json = json.dumps(
                data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            content_hash = hashlib.sha256(canonical_json.encode()).hexdigest()
            generation = db.upsert_replica_partition(
                conn,
                user_id=user_id,
                partition_key=name,
                content_hash=content_hash,
            )
            out.append(ReplicaPartition(
                name=name,
                generation=generation,
                data=data,
            ))
    return out


def reconcile_partitions(
    user_id: int,
    payload_builder: Callable[[], dict[str, dict[str, Any]]],
) -> list[ReplicaPartition]:
    """Build/reconcile one user's partitions under a cross-process lock."""
    if db.DB_BACKEND == "sqlite":
        import fcntl

        lock_path = db.server_db_path().with_suffix(".replica.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # ponytail: one in-process SQLite lock; split per user only if replica
        # contention ever matters. flock adds cross-process serialization.
        with (
            _sqlite_reconcile_thread_lock,
            lock_path.open("a", encoding="utf-8") as lock_file,
        ):
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                return _persist_partitions(user_id, payload_builder())
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    # Dedicated advisory-lock connection lives outside the bounded app pool;
    # payload reads and metadata writes can therefore always check out a slot.
    with db.replica_reconcile_lock(user_id):
        return _persist_partitions(user_id, payload_builder())
