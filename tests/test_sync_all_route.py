"""L7 — POST /sync/all end-to-end test.

L7 — POST /sync/all 端到端測試。

確認:
- 401 未登入
- 沒任何 account → 200, queued=0, skipped=0
- 有 account 但沒 cred → 跳過, skipped=N
- 有 cred 的 account → queued, jobs[] 有 job_id
- 混合: 部分有 cred / 部分沒 → queued + skipped 分開計
"""
from __future__ import annotations

import time


def _register(client, email: str = "syncall@palace.example"):
    r = client.post("/auth/register", json={"email": email, "password": "secret-pw"})
    assert r.status_code == 201, r.text
    return r.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _wait_for_jobs_done(job_ids: list[int], *, max_wait: float = 3.0) -> None:
    """Wait for daemon sync threads before the fixture tears down test DB state.

    /sync/all returns immediately after spawning daemon threads. If a route test
    ends before those threads finish, they can continue running after conftest has
    reloaded modules and swapped BANK_DATA_ROOT for the next test. That leaks
    sync_failed push calls into unrelated tests and makes full-suite CI order-dependent.
    """
    from backend.server import sync_jobs_repo

    deadline = time.time() + max_wait
    remaining = set(job_ids)
    while remaining and time.time() < deadline:
        done = {
            job_id for job_id in remaining
            if (row := sync_jobs_repo.get(job_id))
            and row["status"] in {"done", "failed"}
        }
        remaining -= done
        if remaining:
            time.sleep(0.02)
    assert not remaining, f"sync jobs did not finish before fixture teardown: {sorted(remaining)}"


def test_sync_all_requires_auth(client):
    r = client.post("/sync/all")
    assert r.status_code == 401


def test_sync_all_no_accounts_returns_empty(client, monkeypatch):
    import backend.server.sync_runner as sr
    monkeypatch.setattr(sr, "_dispatch_crawler_and_persist",
                        lambda bank, user_id, headless: {"delta": {}, "stats": {}})
    token = _register(client)
    r = client.post("/sync/all", json={}, headers=_auth(token))
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["queued"] == 0
    assert body["skipped"] == 0
    assert body["jobs"] == []
    assert body["skipped_accounts"] == []
    # 沒 ready account → 不建 batch (避免 total_jobs=0 空 batch claim 立刻搶贏)
    assert body["batch_id"] is None


def test_sync_all_skips_accounts_without_creds(client, monkeypatch):
    import backend.server.sync_runner as sr
    monkeypatch.setattr(sr, "_dispatch_crawler_and_persist",
                        lambda bank, user_id, headless: {"delta": {}, "stats": {}})
    token = _register(client)
    client.post("/accounts", json={"bank": "sinopac", "label": "主帳"}, headers=_auth(token))
    client.post("/accounts", json={"bank": "cathay", "label": "主帳"}, headers=_auth(token))

    r = client.post("/sync/all", json={}, headers=_auth(token))
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["queued"] == 0
    assert body["skipped"] == 2
    assert len(body["skipped_accounts"]) == 2
    assert all(s["reason"] == "尚未設定登入欄位" for s in body["skipped_accounts"])
    # 全 skip 也不該建 batch
    assert body["batch_id"] is None


def test_sync_all_queues_jobs_for_accounts_with_creds(client, monkeypatch):
    import backend.server.sync_runner as sr
    from backend.server import sync_batches_repo, sync_jobs_repo
    monkeypatch.setattr(sr, "_dispatch_crawler_and_persist",
                        lambda bank, user_id, headless: {"delta": {}, "stats": {}})
    token = _register(client)
    # 建兩個 account 並各填 cred
    r1 = client.post("/accounts", json={"bank": "sinopac", "label": "主帳"}, headers=_auth(token))
    aid1 = r1.json()["id"]
    client.put(f"/accounts/{aid1}/fields",
               json={"national_id": "B123456789", "user_code": "u1", "password": "secret-pw"}, headers=_auth(token))
    r2 = client.post("/accounts", json={"bank": "cathay", "label": "主帳"}, headers=_auth(token))
    aid2 = r2.json()["id"]
    client.put(f"/accounts/{aid2}/fields",
               json={"cust_id": "A987654321", "user_id": "u2", "password": "secret-pw"}, headers=_auth(token))

    r = client.post("/sync/all", json={}, headers=_auth(token))
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["queued"] == 2
    assert body["skipped"] == 0
    assert len(body["jobs"]) == 2
    job_account_ids = {j["account_id"] for j in body["jobs"]}
    assert job_account_ids == {aid1, aid2}
    # 每個 job 都有 job_id (背景 thread 排好)
    assert all("job_id" in j for j in body["jobs"])
    # 2026-06-23 (Plan A): 該建 batch, total_jobs == queued
    assert isinstance(body["batch_id"], int)
    batch = sync_batches_repo.get(body["batch_id"])
    assert batch is not None
    assert batch["total_jobs"] == 2
    assert batch["kind"] == "manual_all"
    # 兩 job 都該 stamp 同 batch_id
    for j in body["jobs"]:
        job_row = sync_jobs_repo.get(j["job_id"])
        assert job_row is not None
        assert job_row["batch_id"] == body["batch_id"]
    _wait_for_jobs_done([j["job_id"] for j in body["jobs"]])


def test_sync_all_mixed_creds_and_no_creds(client, monkeypatch):
    import backend.server.sync_runner as sr
    from backend.server import sync_batches_repo
    monkeypatch.setattr(sr, "_dispatch_crawler_and_persist",
                        lambda bank, user_id, headless: {"delta": {}, "stats": {}})
    token = _register(client)
    # account 1: 有 cred
    r1 = client.post("/accounts", json={"bank": "sinopac", "label": "主帳"}, headers=_auth(token))
    aid1 = r1.json()["id"]
    client.put(f"/accounts/{aid1}/fields",
               json={"national_id": "B123456789", "user_code": "u1", "password": "secret-pw"}, headers=_auth(token))
    # account 2: 沒 cred (empty)
    r2 = client.post("/accounts", json={"bank": "cathay", "label": "empty"}, headers=_auth(token))
    aid2 = r2.json()["id"]

    r = client.post("/sync/all", json={}, headers=_auth(token))
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["queued"] == 1
    assert body["skipped"] == 1
    assert body["jobs"][0]["account_id"] == aid1
    assert body["skipped_accounts"][0]["account_id"] == aid2
    # 有 1 個 ready account → 該建 batch, total_jobs = 1 (skipped 不算)
    assert isinstance(body["batch_id"], int)
    batch = sync_batches_repo.get(body["batch_id"])
    assert batch is not None
    assert batch["total_jobs"] == 1
    _wait_for_jobs_done([j["job_id"] for j in body["jobs"]])
