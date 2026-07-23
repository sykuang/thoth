"""Tests for user_sync_pref_repo + sync_preference router + scheduler (L13).

Replaces L12 per-account test_sync_schedules.py — design changed to
per-user single time (使用者「我要使用者設定一個時間給所有帳號」).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.server import scheduler, user_sync_pref_repo


# ============================================================
# Helpers
# ============================================================

@pytest.fixture
def user_token(client: TestClient) -> tuple[str, int]:
    """Register a fresh user, return (bearer_token, user_id).

    Uses conftest's isolated `client` fixture (per-test tmp_path sqlite +
    fresh JWT secret + fresh Fernet key) — DO NOT shadow that fixture by
    constructing TestClient(app) ourselves, it shares the process-level DB
    and leaks rows between tests (e.g. bad-tz row poisons reload tests).
    """
    import uuid
    email = f"l13-{uuid.uuid4().hex[:8]}@palace.example"
    pw = "TestPass123!"
    r = client.post("/auth/register", json={"email": email, "password": pw})
    assert r.status_code in (200, 201)
    body = r.json()
    token = body["token"]
    uid = body["user_id"]
    return token, uid


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ============================================================
# Repo unit tests
# ============================================================

class TestRepo:
    def test_upsert_creates_new(self, user_token: tuple[str, int]) -> None:
        _, uid = user_token
        row = user_sync_pref_repo.upsert(
            user_id=uid, hour=9, minute=30, tz="Asia/Taipei", enabled=True,
        )
        assert row["user_id"] == uid
        assert row["hour"] == 9
        assert row["minute"] == 30
        assert row["enabled"] is True
        assert row["last_run_at"] is None

    def test_upsert_updates_existing_preserves_last_run(
        self, user_token: tuple[str, int],
    ) -> None:
        _, uid = user_token
        user_sync_pref_repo.upsert(user_id=uid, hour=9, minute=0)
        user_sync_pref_repo.mark_last_run(user_id=uid)
        before = user_sync_pref_repo.get(uid)
        assert before is not None and before["last_run_at"] is not None
        # Update time
        row = user_sync_pref_repo.upsert(user_id=uid, hour=22, minute=15)
        assert row["hour"] == 22
        assert row["minute"] == 15
        # last_run_at preserved
        assert row["last_run_at"] == before["last_run_at"]

    def test_upsert_invalid_hour(self, user_token: tuple[str, int]) -> None:
        _, uid = user_token
        with pytest.raises(ValueError, match="hour"):
            user_sync_pref_repo.upsert(user_id=uid, hour=25, minute=0)

    def test_upsert_invalid_minute(self, user_token: tuple[str, int]) -> None:
        _, uid = user_token
        with pytest.raises(ValueError, match="minute"):
            user_sync_pref_repo.upsert(user_id=uid, hour=9, minute=60)

    def test_get_none_when_never_set(self, user_token: tuple[str, int]) -> None:
        _, uid = user_token
        assert user_sync_pref_repo.get(uid) is None

    def test_delete(self, user_token: tuple[str, int]) -> None:
        _, uid = user_token
        user_sync_pref_repo.upsert(user_id=uid, hour=9, minute=0)
        assert user_sync_pref_repo.delete(uid) is True
        assert user_sync_pref_repo.get(uid) is None
        assert user_sync_pref_repo.delete(uid) is False  # idempotent

    def test_list_all_enabled_only(self) -> None:
        # Two new users for isolation
        import uuid
        from backend.server.db import get_conn, now_iso
        with get_conn() as conn:
            now = now_iso()
            cur1 = conn.execute(
                "INSERT INTO users (email, password_hash, created_at) "
                "VALUES (?, ?, ?)",
                (f"r1-{uuid.uuid4().hex[:6]}@palace.example", "h", now),
            )
            cur2 = conn.execute(
                "INSERT INTO users (email, password_hash, created_at) "
                "VALUES (?, ?, ?)",
                (f"r2-{uuid.uuid4().hex[:6]}@palace.example", "h", now),
            )
            u1 = cur1.lastrowid
            u2 = cur2.lastrowid
        assert u1 and u2
        user_sync_pref_repo.upsert(user_id=u1, hour=9, minute=0, enabled=True)
        user_sync_pref_repo.upsert(user_id=u2, hour=20, minute=0, enabled=False)
        enabled = user_sync_pref_repo.list_all_enabled()
        ids = {p["user_id"] for p in enabled}
        assert u1 in ids
        assert u2 not in ids


# ============================================================
# Router endpoint tests
# ============================================================

class TestRouter:
    def test_get_returns_null_when_never_set(
        self, client: TestClient, user_token: tuple[str, int],
    ) -> None:
        token, _ = user_token
        r = client.get("/me/sync-preference", headers=_auth(token))
        assert r.status_code == 200
        assert r.json() is None

    def test_put_creates_and_get_returns_it(
        self, client: TestClient, user_token: tuple[str, int],
    ) -> None:
        token, uid = user_token
        r = client.put(
            "/me/sync-preference",
            json={"hour": 23, "minute": 45, "tz": "Asia/Taipei", "enabled": True},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["user_id"] == uid
        assert body["hour"] == 23
        assert body["minute"] == 45
        # GET round-trip
        r2 = client.get("/me/sync-preference", headers=_auth(token))
        assert r2.status_code == 200
        assert r2.json()["hour"] == 23

    def test_put_validates_hour_range(
        self, client: TestClient, user_token: tuple[str, int],
    ) -> None:
        token, _ = user_token
        r = client.put(
            "/me/sync-preference",
            json={"hour": 99, "minute": 0},
            headers=_auth(token),
        )
        assert r.status_code == 422

    def test_put_invalid_tz_returns_400(
        self, client: TestClient, user_token: tuple[str, int],
    ) -> None:
        token, _ = user_token
        r = client.put(
            "/me/sync-preference",
            json={"hour": 9, "minute": 0, "tz": "Bogus/Foo"},
            headers=_auth(token),
        )
        assert r.status_code == 400

    def test_delete_removes(
        self, client: TestClient, user_token: tuple[str, int],
    ) -> None:
        token, uid = user_token
        client.put(
            "/me/sync-preference",
            json={"hour": 9, "minute": 0},
            headers=_auth(token),
        )
        r = client.delete("/me/sync-preference", headers=_auth(token))
        assert r.status_code == 204
        assert user_sync_pref_repo.get(uid) is None

    def test_requires_auth(self, client: TestClient) -> None:
        r = client.get("/me/sync-preference")
        assert r.status_code == 401

    def test_debug_endpoint_lists_jobs(
        self, client: TestClient, user_token: tuple[str, int],
    ) -> None:
        token, _ = user_token
        r = client.get("/me/sync-preference/_debug", headers=_auth(token))
        assert r.status_code == 200
        body = r.json()
        assert "items" in body
        assert "count" in body


# ============================================================
# Scheduler integration tests (DB writes + APScheduler in-memory)
# ============================================================

class TestSchedulerWiring:
    def test_add_or_replace_creates_job(self, user_token: tuple[str, int]) -> None:
        _, uid = user_token
        s = scheduler.get_scheduler()
        if not s.running:
            s.start()
        try:
            scheduler.add_or_replace_for_user({
                "user_id": uid,
                "hour": 9,
                "minute": 30,
                "tz": "Asia/Taipei",
                "enabled": True,
            })
            jobs = scheduler.list_jobs()
            assert any(j["id"] == f"user-{uid}" for j in jobs)
        finally:
            scheduler.remove_for_user(uid)

    def test_add_or_replace_disabled_removes_job(
        self, user_token: tuple[str, int],
    ) -> None:
        _, uid = user_token
        s = scheduler.get_scheduler()
        if not s.running:
            s.start()
        try:
            # First add enabled
            scheduler.add_or_replace_for_user({
                "user_id": uid,
                "hour": 9,
                "minute": 30,
                "tz": "Asia/Taipei",
                "enabled": True,
            })
            assert any(j["id"] == f"user-{uid}" for j in scheduler.list_jobs())
            # Then disable
            scheduler.add_or_replace_for_user({
                "user_id": uid,
                "hour": 9,
                "minute": 30,
                "tz": "Asia/Taipei",
                "enabled": False,
            })
            assert not any(j["id"] == f"user-{uid}" for j in scheduler.list_jobs())
        finally:
            scheduler.remove_for_user(uid)

    def test_fire_fans_out_to_all_has_creds_accounts(
        self, user_token: tuple[str, int],
    ) -> None:
        """Fire 觸發時應呼 run_sync_job_for_account 對每個 ready account 一次."""
        _, uid = user_token
        # Mock AccountsRepo.list_for_user + LocalFernetBackend.list_fields_acct
        from dataclasses import dataclass

        @dataclass
        class _Stub:
            id: int
            bank: str = "fubon"
            label: str = "main"

        fake_accts = [_Stub(id=101), _Stub(id=102), _Stub(id=103)]
        # 第 1 + 3 ready, 第 2 沒 creds
        fake_fields = {101: ["password"], 102: [], 103: ["password"]}

        with patch(
            "backend.server.creds_store.AccountsRepo"
        ) as mock_repo_cls, patch(
            "backend.server.creds_store.LocalFernetBackend"
        ) as mock_store_cls, patch(
            "backend.server.sync_runner.run_sync_job_for_account"
        ) as mock_run:
            mock_repo_cls.return_value.list_for_user.return_value = fake_accts
            mock_store_cls.return_value.list_fields_acct.side_effect = (
                lambda aid, expected_owner_user_id=None: fake_fields.get(aid, [])
            )
            mock_run.side_effect = [201, 202]  # only 2 ready -> 2 calls

            scheduler._run_sync_for_user(uid)

            # Only ready ones get queued
            assert mock_run.call_count == 2
            queued_ids = [c.kwargs["account_id"] for c in mock_run.call_args_list]
            assert sorted(queued_ids) == [101, 103]

    def test_reload_all_jobs_from_db(self, user_token: tuple[str, int]) -> None:
        _, uid = user_token
        user_sync_pref_repo.upsert(
            user_id=uid, hour=8, minute=0, enabled=True,
        )
        s = scheduler.get_scheduler()
        if not s.running:
            s.start()
        try:
            count = scheduler.reload_all_jobs()
            assert count >= 1
            assert any(j["id"] == f"user-{uid}" for j in scheduler.list_jobs())
        finally:
            scheduler.remove_for_user(uid)
            user_sync_pref_repo.delete(uid)
