"""Phase 1 — /sync routes (end-to-end).

Phase 1 — /sync routes（端到端）。

mock 掉 _dispatch_crawler_and_persist 避免真去登銀行。
"""
from __future__ import annotations

import time


def _register(client, email: str = "syncroute@palace.example"):
    r = client.post(
        "/auth/register",
        json={"email": email, "password": "SyntheticTestPassword02!"},
    )
    assert r.status_code == 201, r.text
    return r.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _wait_job_done(client, job_id: int, token: str, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/sync/jobs/{job_id}", headers=_auth(token))
        if r.status_code == 200 and r.json()["status"] in {"done", "failed"}:
            return r.json()
        time.sleep(0.05)
    raise TimeoutError(f"job {job_id} 沒結束")


def test_post_sync_returns_job_id_and_unknown_bank_400(client, monkeypatch):
    import backend.server.sync_runner as sr
    # 假 dispatch 避免真去爬
    monkeypatch.setattr(sr, "_dispatch_crawler_and_persist",
                        lambda bank, user_id, headless: {"delta": {}, "stats": {}})
    token = _register(client)

    # 正路
    r = client.post("/sync/sinopac", json={}, headers=_auth(token))
    assert r.status_code == 202, r.text
    body = r.json()
    assert "job_id" in body
    assert body["bank"] == "sinopac"

    # 未知 bank → 400
    r = client.post("/sync/zionsbank", json={}, headers=_auth(token))
    assert r.status_code == 400, r.text


def test_get_jobs_lists_only_own_jobs(client, monkeypatch):
    import backend.server.sync_runner as sr
    monkeypatch.setattr(sr, "_dispatch_crawler_and_persist",
                        lambda bank, user_id, headless: {"delta": {}, "stats": {}})
    t1 = _register(client, email="u1-sync@palace.example")
    t2 = _register(client, email="u2-sync@palace.example")

    j1 = client.post("/sync/sinopac", json={}, headers=_auth(t1)).json()["job_id"]
    _wait_job_done(client, j1, t1)

    # user2 列表不該看見 user1 的 job
    r = client.get("/sync/jobs", headers=_auth(t2))
    assert r.status_code == 200
    assert all(j["id"] != j1 for j in r.json())


def test_get_job_other_user_404(client, monkeypatch):
    import backend.server.sync_runner as sr
    monkeypatch.setattr(sr, "_dispatch_crawler_and_persist",
                        lambda bank, user_id, headless: {"delta": {}, "stats": {}})
    t1 = _register(client, email="owner-sync@palace.example")
    t2 = _register(client, email="other-sync@palace.example")

    j1 = client.post("/sync/sinopac", json={}, headers=_auth(t1)).json()["job_id"]
    # user2 直接抓 user1 的 job → 404（不洩漏存在性）
    r = client.get(f"/sync/jobs/{j1}", headers=_auth(t2))
    assert r.status_code == 404, r.text


def test_get_job_status_after_run(client, monkeypatch):
    import backend.server.sync_runner as sr
    monkeypatch.setattr(sr, "_dispatch_crawler_and_persist",
                        lambda bank, user_id, headless: {"delta": {"twd_txn_new": 5}, "stats": {}})
    token = _register(client)
    job_id = client.post("/sync/sinopac", json={"headless": True},
                         headers=_auth(token)).json()["job_id"]
    done = _wait_job_done(client, job_id, token)
    assert done["status"] == "done"
    assert done["finished_at"]
    assert "twd_txn_new" in done["result_summary"]
