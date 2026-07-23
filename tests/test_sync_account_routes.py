"""Phase L5-1 — /sync/account/{account_id} end-to-end test.

Phase L5-1 — /sync/account/{account_id} 端到端測試。

Mock 掉 _dispatch_crawler_and_persist 避免真去登銀行;
驗證新路徑會設 BANK_CRAWLER_ACCOUNT_ID env, 而非舊 BANK_CRAWLER_USER_ID 唯一。
"""
from __future__ import annotations

import os
import time


def _register(client, email: str = "syncacct@palace.example"):
    r = client.post("/auth/register", json={"email": email, "password": "secret-pw"})
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


def test_post_sync_account_returns_job_id_with_account_metadata(client, monkeypatch):
    import backend.server.sync_runner as sr
    monkeypatch.setattr(sr, "_dispatch_crawler_and_persist",
                        lambda bank, user_id, headless: {"delta": {}, "stats": {}})
    token = _register(client)

    # 建一個 account
    r = client.post("/accounts", json={"bank": "sinopac", "label": "主帳"}, headers=_auth(token))
    aid = r.json()["id"]

    # 跑 sync
    r = client.post(f"/sync/account/{aid}", json={}, headers=_auth(token))
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["account_id"] == aid
    assert body["bank"] == "sinopac"
    assert body["label"] == "主帳"

    # 等 job 跑完, 確認 account_id 有記到 sync_jobs
    job = _wait_job_done(client, body["job_id"], token)
    assert job["account_id"] == aid
    assert job["status"] == "done"


def test_post_sync_account_not_owned_404(client, monkeypatch):
    import backend.server.sync_runner as sr
    monkeypatch.setattr(sr, "_dispatch_crawler_and_persist",
                        lambda bank, user_id, headless: {"delta": {}, "stats": {}})
    t1 = _register(client, "u1-acct@palace.example")
    t2 = _register(client, "u2-acct@palace.example")

    r = client.post("/accounts", json={"bank": "sinopac", "label": "x"}, headers=_auth(t1))
    aid = r.json()["id"]

    r = client.post(f"/sync/account/{aid}", json={}, headers=_auth(t2))
    assert r.status_code == 404


def test_post_sync_account_unknown_id_404(client):
    token = _register(client)
    r = client.post("/sync/account/99999", json={}, headers=_auth(token))
    assert r.status_code == 404


def test_sync_account_sets_account_id_env_in_dispatch(client, monkeypatch):
    """確認 daemon thread 內 dispatch 時 BANK_CRAWLER_ACCOUNT_ID 有被設。"""
    seen_env: dict[str, str | None] = {}

    def _fake_dispatch(bank: str, user_id: int, headless: bool):
        seen_env["account_id"] = os.environ.get("BANK_CRAWLER_ACCOUNT_ID")
        seen_env["user_id"] = os.environ.get("BANK_CRAWLER_USER_ID")
        return {"delta": {}, "stats": {}}

    import backend.server.sync_runner as sr
    monkeypatch.setattr(sr, "_dispatch_crawler_and_persist", _fake_dispatch)
    token = _register(client)

    r = client.post("/accounts", json={"bank": "ctbc", "label": "x"}, headers=_auth(token))
    aid = r.json()["id"]
    r = client.post(f"/sync/account/{aid}", json={}, headers=_auth(token))
    job_id = r.json()["job_id"]
    _wait_job_done(client, job_id, token)

    assert seen_env["account_id"] == str(aid)
    assert seen_env["user_id"] is not None  # 兩個都該被設


def test_legacy_sync_bank_route_does_not_set_account_id_env(client, monkeypatch):
    """老路徑 POST /sync/{bank} 不該設 ACCOUNT_ID env。"""
    seen_env: dict[str, str | None] = {}

    def _fake_dispatch(bank: str, user_id: int, headless: bool):
        seen_env["account_id"] = os.environ.get("BANK_CRAWLER_ACCOUNT_ID")
        seen_env["user_id"] = os.environ.get("BANK_CRAWLER_USER_ID")
        return {"delta": {}, "stats": {}}

    import backend.server.sync_runner as sr
    monkeypatch.setattr(sr, "_dispatch_crawler_and_persist", _fake_dispatch)
    token = _register(client)

    r = client.post("/sync/ctbc", json={}, headers=_auth(token))
    job_id = r.json()["job_id"]
    _wait_job_done(client, job_id, token)

    assert seen_env["account_id"] is None  # 老路徑不該設
    assert seen_env["user_id"] is not None
