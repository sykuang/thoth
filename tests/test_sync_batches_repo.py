"""2026-06-23 (Plan A) — sync_batches_repo unit tests.

驗:
  * create() 寫進 schema, 拿得到 batch_id
  * create() 拒不合法 kind / 負 total_jobs
  * claim_for_notification 兩階段 atomic:
      - 還有 in-flight job → None
      - 全 done/failed → 拿到 row, 後續呼叫 None (已被搶過)
  * Race: 兩 thread 同時 claim, 只 1 個拿到 row
"""
from __future__ import annotations

import threading

import pytest

from backend.server import sync_batches_repo, sync_jobs_repo


def _create_user_and_account(client):
    """Quick helper — register a user, return user_id from /auth/register response."""
    r = client.post("/auth/register",
                    json={"email": "batch@palace.example", "password": "secret-pw"})
    assert r.status_code == 201, r.text
    body = r.json()
    return body["user_id"]


def test_create_returns_batch_id(client):
    user_id = _create_user_and_account(client)
    bid = sync_batches_repo.create(
        user_id=user_id, total_jobs=3, kind=sync_batches_repo.KIND_MANUAL_ALL,
    )
    assert isinstance(bid, int)
    assert bid > 0

    row = sync_batches_repo.get(bid)
    assert row is not None
    assert row["user_id"] == user_id
    assert row["total_jobs"] == 3
    assert row["kind"] == "manual_all"
    assert row["created_at"]  # not empty
    assert row["finished_at"] is None
    assert row["notified_at"] is None


def test_create_rejects_invalid_kind(client):
    user_id = _create_user_and_account(client)
    with pytest.raises(ValueError, match="kind="):
        sync_batches_repo.create(user_id=user_id, total_jobs=1, kind="bogus")


def test_create_rejects_negative_total(client):
    user_id = _create_user_and_account(client)
    with pytest.raises(ValueError, match="total_jobs"):
        sync_batches_repo.create(
            user_id=user_id, total_jobs=-1,
            kind=sync_batches_repo.KIND_MANUAL_ALL,
        )


def test_claim_returns_none_while_jobs_in_flight(client):
    user_id = _create_user_and_account(client)
    bid = sync_batches_repo.create(
        user_id=user_id, total_jobs=2, kind=sync_batches_repo.KIND_MANUAL_ALL,
    )
    # 2 個 queued job
    j1 = sync_jobs_repo.queue(user_id=user_id, bank="cathay", batch_id=bid)
    j2 = sync_jobs_repo.queue(user_id=user_id, bank="ubot", batch_id=bid)

    # 還沒收 — 應 None
    assert sync_batches_repo.claim_for_notification(bid) is None

    # 收 1 個, 還有 1 個 in-flight — 仍 None
    sync_jobs_repo.mark_done(j1, '{"txn_count": 5}')
    assert sync_batches_repo.claim_for_notification(bid) is None

    # 收完最後 1 個 — 拿得到 row
    sync_jobs_repo.mark_done(j2, '{"txn_count": 3}')
    claimed = sync_batches_repo.claim_for_notification(bid)
    assert claimed is not None
    assert claimed["id"] == bid
    assert claimed["notified_at"]
    assert claimed["finished_at"]

    # 二次呼叫 — None (notified_at 已不為 NULL)
    again = sync_batches_repo.claim_for_notification(bid)
    assert again is None


def test_claim_treats_running_as_in_flight(client):
    user_id = _create_user_and_account(client)
    bid = sync_batches_repo.create(
        user_id=user_id, total_jobs=1, kind=sync_batches_repo.KIND_MANUAL_ALL,
    )
    j = sync_jobs_repo.queue(user_id=user_id, bank="cathay", batch_id=bid)
    sync_jobs_repo.mark_running(j)
    # running 仍 in-flight
    assert sync_batches_repo.claim_for_notification(bid) is None
    sync_jobs_repo.mark_failed(j, "boom")
    assert sync_batches_repo.claim_for_notification(bid) is not None


def test_claim_race_only_one_winner(client):
    """並發 claim 只有 1 個 thread 拿到 row (atomic CAS via UPDATE ... RETURNING)."""
    user_id = _create_user_and_account(client)
    bid = sync_batches_repo.create(
        user_id=user_id, total_jobs=1, kind=sync_batches_repo.KIND_MANUAL_ALL,
    )
    # 1 個 job, 全收完, 開放 claim
    j = sync_jobs_repo.queue(user_id=user_id, bank="cathay", batch_id=bid)
    sync_jobs_repo.mark_done(j, "{}")

    winners: list[dict] = []
    losers: list[None] = []
    barrier = threading.Barrier(8)

    def _attempt():
        barrier.wait()  # 同時開跑
        result = sync_batches_repo.claim_for_notification(bid)
        if result is not None:
            winners.append(result)
        else:
            losers.append(None)

    threads = [threading.Thread(target=_attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1, f"expected 1 winner, got {len(winners)}: {winners}"
    assert len(losers) == 7
