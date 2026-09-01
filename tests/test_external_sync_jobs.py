from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


def test_worker_enforces_explicit_user_ids() -> None:
    env = os.environ.copy()
    env.pop("THOTH_REQUIRE_EXPLICIT_USER_ID", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os; import backend.server.sync_job_worker; "
                "assert os.environ['THOTH_REQUIRE_EXPLICIT_USER_ID'] == '1'"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_backend_externalizes_azure_without_breaking_standalone() -> None:
    app_source = Path("backend/server/app.py").read_text()
    assert Path("backend/server/scheduler.py").exists()
    assert "apscheduler" in Path("pyproject.toml").read_text().lower()
    assert "scheduler_module.in_process_enabled()" in app_source


def test_sync_job_claim_is_atomic(client) -> None:
    from backend.server import sync_jobs_repo

    registered = client.post(
        "/auth/register",
        json={"email": "claim@palace.example", "password": "SyntheticTestPassword02!"},
    ).json()
    job_id = sync_jobs_repo.queue(user_id=registered["user_id"], bank="sinopac")

    assert sync_jobs_repo.claim_queued(job_id) is True
    assert sync_jobs_repo.claim_queued(job_id) is False
    row = sync_jobs_repo.get(job_id)
    assert row is not None
    assert row["status"] == "running"
    assert row["started_at"] is not None


def test_sync_runner_does_not_execute_already_claimed_job(client, monkeypatch) -> None:
    from backend.server import sync_jobs_repo
    from backend.server import sync_runner

    registered = client.post(
        "/auth/register",
        json={"email": "claimed-run@palace.example", "password": "SyntheticTestPassword02!"},
    ).json()
    job_id = sync_jobs_repo.queue(user_id=registered["user_id"], bank="sinopac")
    assert sync_jobs_repo.claim_queued(job_id) is True
    monkeypatch.setattr(
        sync_runner,
        "_dispatch_crawler_and_persist",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("duplicate dispatch")),
    )

    sync_runner._exec_sync(job_id)

    row = sync_jobs_repo.get(job_id)
    assert row is not None
    assert row["status"] == "running"


def test_queued_job_ids_exclude_claimed_rows(client) -> None:
    from backend.server import sync_jobs_repo

    registered = client.post(
        "/auth/register",
        json={"email": "queue-list@palace.example", "password": "SyntheticTestPassword02!"},
    ).json()
    first = sync_jobs_repo.queue(user_id=registered["user_id"], bank="sinopac")
    second = sync_jobs_repo.queue(user_id=registered["user_id"], bank="cathay")
    assert sync_jobs_repo.claim_queued(first) is True

    assert sync_jobs_repo.list_queued_ids() == [second]


def test_worker_drains_manual_and_scheduled_jobs(client, monkeypatch) -> None:
    from backend.server import sync_batches_repo, sync_job_worker, sync_jobs_repo, sync_runner

    registered = client.post(
        "/auth/register",
        json={"email": "manual-queue@palace.example", "password": "SyntheticTestPassword02!"},
    ).json()
    user_id = registered["user_id"]
    batch_id = sync_batches_repo.create(
        user_id=user_id,
        total_jobs=1,
        kind=sync_batches_repo.KIND_SCHEDULED_ALL,
    )
    scheduled_job = sync_jobs_repo.queue(user_id=user_id, bank="sinopac", batch_id=batch_id)
    manual_job = sync_jobs_repo.queue(user_id=user_id, bank="cathay")
    monkeypatch.setenv("SYNC_EXECUTION_MODE", "external")
    monkeypatch.setattr(sync_runner, "_required_history_domains", lambda _bank: frozenset())
    monkeypatch.setattr(
        sync_runner,
        "_dispatch_crawler_and_persist",
        lambda *_args, **_kwargs: {"delta": {}, "stats": {}},
    )

    result = sync_job_worker.run_queued_jobs()

    assert result["processed"] == 2
    scheduled_row = sync_jobs_repo.get(scheduled_job)
    manual_row = sync_jobs_repo.get(manual_job)
    assert scheduled_row is not None
    assert manual_row is not None
    assert scheduled_row["status"] == "done"
    assert manual_row["status"] == "done"


def test_stale_queued_jobs_are_failed_without_execution(client) -> None:
    from backend.server import sync_batches_repo, sync_jobs_repo
    from backend.server.db import get_conn

    registered = client.post(
        "/auth/register",
        json={"email": "stale-queued@palace.example", "password": "SyntheticTestPassword02!"},
    ).json()
    user_id = registered["user_id"]
    job_id = sync_jobs_repo.queue(user_id=user_id, bank="sinopac")
    scheduled_batch = sync_batches_repo.create(
        user_id=user_id,
        total_jobs=1,
        kind=sync_batches_repo.KIND_SCHEDULED_ALL,
    )
    scheduled_job = sync_jobs_repo.queue(
        user_id=user_id,
        bank="cathay",
        batch_id=scheduled_batch,
    )
    with get_conn() as conn:
        conn.execute(
            "UPDATE sync_jobs SET created_at='2026-01-01T00:00:00+00:00' "
            "WHERE id IN (?, ?)",
            (job_id, scheduled_job),
        )

    assert sync_jobs_repo.sweep_stale_queued().swept_count == 1
    row = sync_jobs_repo.get(job_id)
    assert row is not None
    assert row["status"] == "failed"
    assert row["error_msg"] == "stale_queue: worker did not claim within 1 hour"
    scheduled_row = sync_jobs_repo.get(scheduled_job)
    assert scheduled_row is not None
    assert scheduled_row["status"] == "queued"


def test_external_queue_keeps_jobs_within_seven_hour_worker_window(client, monkeypatch) -> None:
    from backend.server import sync_jobs_repo
    from backend.server.db import get_conn

    registered = client.post(
        "/auth/register",
        json={"email": "fresh-external-queue@palace.example", "password": "SyntheticTestPassword02!"},
    ).json()
    job_id = sync_jobs_repo.queue(user_id=registered["user_id"], bank="sinopac")
    with get_conn() as conn:
        conn.execute(
            "UPDATE sync_jobs SET created_at=? WHERE id=?",
            ((datetime.now(ZoneInfo("UTC")) - timedelta(hours=2)).isoformat(), job_id),
        )
    monkeypatch.setenv("SYNC_EXECUTION_MODE", "external")

    swept = sync_jobs_repo.sweep_stale_queued()

    assert swept.swept_count == 0
    row = sync_jobs_repo.get(job_id)
    assert row is not None and row["status"] == "queued"


def test_stale_scheduled_jobs_are_failed_only_after_worker_timeout(client) -> None:
    from backend.server import sync_batches_repo, sync_jobs_repo
    from backend.server.db import get_conn

    registered = client.post(
        "/auth/register",
        json={"email": "stale-scheduled@palace.example", "password": "SyntheticTestPassword02!"},
    ).json()
    user_id = registered["user_id"]
    batch_id = sync_batches_repo.create(
        user_id=user_id,
        total_jobs=1,
        kind=sync_batches_repo.KIND_SCHEDULED_ALL,
    )
    scheduled_job = sync_jobs_repo.queue(
        user_id=user_id,
        bank="sinopac",
        batch_id=batch_id,
    )
    manual_job = sync_jobs_repo.queue(user_id=user_id, bank="cathay")
    with get_conn() as conn:
        conn.execute(
            "UPDATE sync_jobs SET created_at='2026-01-01T00:00:00+00:00' "
            "WHERE id IN (?, ?)",
            (scheduled_job, manual_job),
        )

    assert sync_jobs_repo.sweep_stale_scheduled_queued().swept_count == 1
    scheduled_row = sync_jobs_repo.get(scheduled_job)
    manual_row = sync_jobs_repo.get(manual_job)
    assert scheduled_row is not None and scheduled_row["status"] == "failed"
    assert manual_row is not None and manual_row["status"] == "queued"


def test_stale_manual_batch_still_finalizes_summary(client, monkeypatch) -> None:
    from backend.server import sync_batches_repo, sync_job_worker, sync_jobs_repo, sync_runner
    from backend.server.db import get_conn

    registered = client.post(
        "/auth/register",
        json={"email": "stale-summary@palace.example", "password": "SyntheticTestPassword02!"},
    ).json()
    user_id = registered["user_id"]
    batch_id = sync_batches_repo.create(
        user_id=user_id,
        total_jobs=1,
        kind=sync_batches_repo.KIND_MANUAL_ALL,
    )
    job_id = sync_jobs_repo.queue(
        user_id=user_id,
        bank="sinopac",
        batch_id=batch_id,
    )
    with get_conn() as conn:
        conn.execute(
            "UPDATE sync_jobs SET created_at='2026-01-01T00:00:00+00:00' WHERE id=?",
            (job_id,),
        )
    finalized: list[int] = []
    original_summary = sync_runner._maybe_send_batch_summary

    def _record_summary(*, batch_id: int, user_id: int) -> None:
        finalized.append(batch_id)
        original_summary(batch_id=batch_id, user_id=user_id)

    monkeypatch.setattr(sync_runner, "_maybe_send_batch_summary", _record_summary)

    sync_job_worker.run_queued_jobs()

    assert finalized == [batch_id]


def test_polling_finalizes_batch_after_external_running_timeout(client, monkeypatch) -> None:
    from backend.server import sync_batches_repo, sync_jobs_repo, sync_runner
    from backend.server.db import get_conn

    registered = client.post(
        "/auth/register",
        json={"email": "running-summary@palace.example", "password": "SyntheticTestPassword02!"},
    ).json()
    user_id = registered["user_id"]
    batch_id = sync_batches_repo.create(
        user_id=user_id,
        total_jobs=1,
        kind=sync_batches_repo.KIND_MANUAL_ALL,
    )
    job_id = sync_jobs_repo.queue(
        user_id=user_id,
        bank="sinopac",
        batch_id=batch_id,
    )
    sync_jobs_repo.mark_running(job_id)
    with get_conn() as conn:
        conn.execute(
            "UPDATE sync_jobs SET started_at='2026-01-01T00:00:00+00:00' WHERE id=?",
            (job_id,),
        )
    finalized: list[int] = []
    monkeypatch.setenv("SYNC_EXECUTION_MODE", "external")
    monkeypatch.setattr(
        sync_runner,
        "_maybe_send_batch_summary",
        lambda *, batch_id, user_id: finalized.append(batch_id),
    )

    sync_runner.list_recent_jobs(user_id)

    assert finalized == [batch_id]


def test_worker_sweeps_stale_running_jobs_without_queued_rows(client, monkeypatch) -> None:
    from backend.server import sync_batches_repo, sync_job_worker, sync_jobs_repo, sync_runner
    from backend.server.db import get_conn

    registered = client.post(
        "/auth/register",
        json={"email": "worker-stale-running@palace.example", "password": "SyntheticTestPassword02!"},
    ).json()
    user_id = registered["user_id"]
    batch_id = sync_batches_repo.create(
        user_id=user_id,
        total_jobs=1,
        kind=sync_batches_repo.KIND_MANUAL_ALL,
    )
    job_id = sync_jobs_repo.queue(user_id=user_id, bank="sinopac", batch_id=batch_id)
    sync_jobs_repo.mark_running(job_id)
    with get_conn() as conn:
        conn.execute(
            "UPDATE sync_jobs SET started_at='2026-01-01T00:00:00+00:00' WHERE id=?",
            (job_id,),
        )
    finalized: list[int] = []
    monkeypatch.setenv("SYNC_EXECUTION_MODE", "external")
    monkeypatch.setattr(
        sync_runner,
        "_maybe_send_batch_summary",
        lambda *, batch_id, user_id: finalized.append(batch_id),
    )

    sync_job_worker.run_queued_jobs()

    row = sync_jobs_repo.get(job_id)
    assert row is not None and row["status"] == "failed"
    assert finalized == [batch_id]


def test_external_worker_drains_queued_jobs(client, monkeypatch) -> None:
    from backend.server import sync_job_worker, sync_jobs_repo, sync_runner

    registered = client.post(
        "/auth/register",
        json={"email": "worker@palace.example", "password": "SyntheticTestPassword02!"},
    ).json()
    monkeypatch.setenv("SYNC_EXECUTION_MODE", "external")
    monkeypatch.setattr(
        sync_runner,
        "_dispatch_crawler_and_persist",
        lambda *_args, **_kwargs: {"delta": {}, "stats": {}},
    )
    job_ids = [
        sync_runner.run_sync_job(user_id=registered["user_id"], bank=bank)
        for bank in ("sinopac", "cathay")
    ]

    result = sync_job_worker.run_queued_jobs()

    assert result == {"processed": 2, "done": 2, "failed": 0}
    rows = [sync_jobs_repo.get(job_id) for job_id in job_ids]
    assert all(row is not None for row in rows)
    assert [row["status"] for row in rows if row is not None] == ["done", "done"]


def test_scheduled_slot_queues_entire_batch_for_worker(client, monkeypatch) -> None:
    from backend.server import (
        sync_job_worker,
        sync_jobs_repo,
        sync_runner,
        user_sync_pref_repo,
    )
    from backend.server.creds_store import AccountsRepo, LocalFernetBackend

    first = client.post(
        "/auth/register",
        json={"email": "slot-10@palace.example", "password": "SyntheticTestPassword02!"},
    ).json()["user_id"]
    second = client.post(
        "/auth/register",
        json={"email": "slot-12@palace.example", "password": "SyntheticTestPassword02!"},
    ).json()["user_id"]
    user_sync_pref_repo.upsert_slots(user_id=first, slots=["10:00", "12:00"])
    user_sync_pref_repo.upsert(user_id=second, hour=12, minute=0)
    account = AccountsRepo().create(first, "taishin", "main")
    second_account = AccountsRepo().create(first, "taishin", "secondary")
    LocalFernetBackend().put_acct(
        account.id, "password", "synthetic", expected_owner_user_id=first,
    )
    LocalFernetBackend().put_acct(
        second_account.id, "password", "synthetic", expected_owner_user_id=first,
    )
    monkeypatch.setenv("SYNC_EXECUTION_MODE", "external")
    monkeypatch.setattr(sync_runner, "_required_history_domains", lambda _bank: frozenset())
    monkeypatch.setattr(
        sync_runner,
        "_dispatch_crawler_and_persist",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("crawler ran in scheduler")),
    )
    reminders: list[str] = []
    monkeypatch.setattr(
        sync_job_worker,
        "dispatch_daily_payment_reminders_for_all_users",
        lambda **_kwargs: reminders.append("sent") or {"users": 2, "sent": 0, "skipped": 0},
    )

    result = sync_job_worker.run_scheduled_slot(
        datetime(2026, 9, 1, 10, 5, tzinfo=ZoneInfo("Asia/Taipei")),
    )

    assert result["users"] == 1
    assert result["jobs"] == 2
    assert result["done"] == 0
    queued_rows = [sync_jobs_repo.get(job_id) for job_id in sync_jobs_repo.list_queued_ids()]
    assert [row["status"] for row in queued_rows if row is not None] == ["queued", "queued"]
    assert reminders == []
    first_pref = user_sync_pref_repo.get(first)
    second_pref = user_sync_pref_repo.get(second)
    assert first_pref is not None
    assert second_pref is not None
    assert first_pref["slots"] == ["10:00", "12:00"]
    assert first_pref["last_run_at"] is not None
    assert second_pref["last_run_at"] is None


def test_scheduled_slot_reconciles_partial_queue_failure(client, monkeypatch) -> None:
    from backend.server import sync_batches_repo, sync_job_worker, sync_jobs_repo, sync_runner
    from backend.server.creds_store import AccountsRepo, LocalFernetBackend

    registered = client.post(
        "/auth/register",
        json={"email": "partial-slot@palace.example", "password": "SyntheticTestPassword02!"},
    ).json()
    user_id = registered["user_id"]
    sync_job_worker.user_sync_pref_repo.upsert(user_id=user_id, hour=10, minute=0)
    accounts = [
        AccountsRepo().create(user_id, "taishin", "main"),
        AccountsRepo().create(user_id, "taishin", "secondary"),
    ]
    for account in accounts:
        LocalFernetBackend().put_acct(
            account.id,
            "password",
            "synthetic",
            expected_owner_user_id=user_id,
        )
    monkeypatch.setenv("SYNC_EXECUTION_MODE", "external")
    original_queue = sync_runner.run_sync_job_for_account
    attempts = 0

    def _fail_second_queue(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise RuntimeError("queue failed")
        return original_queue(**kwargs)

    monkeypatch.setattr(sync_runner, "run_sync_job_for_account", _fail_second_queue)

    with pytest.raises(RuntimeError, match="queue failed"):
        sync_job_worker.run_scheduled_slot(
            datetime(2026, 9, 1, 10, 5, tzinfo=ZoneInfo("Asia/Taipei")),
        )

    queued = sync_jobs_repo.list_queued_ids()
    assert len(queued) == 1
    row = sync_jobs_repo.get(queued[0])
    assert row is not None and row["batch_id"] is not None
    batch = sync_batches_repo.get(row["batch_id"])
    assert batch is not None
    assert batch["total_jobs"] == 1


def test_payment_reminders_run_in_an_independent_worker(client, monkeypatch) -> None:
    from backend.server import sync_job_worker

    reminders: list[str] = []
    monkeypatch.setattr(
        sync_job_worker,
        "dispatch_daily_payment_reminders_for_all_users",
        lambda **_kwargs: reminders.append("sent") or {"users": 2, "sent": 1, "skipped": 1},
    )

    result = sync_job_worker.run_payment_reminders()

    assert reminders == ["sent"]
    assert result == {"users": 2, "sent": 1, "skipped": 1}
