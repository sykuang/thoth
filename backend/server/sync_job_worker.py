"""Finite Container Apps Job entrypoint for bank sync and payment reminders."""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

os.environ.setdefault("THOTH_REQUIRE_EXPLICIT_USER_ID", "1")

from backend.server import (
    payment_reminder_notifications,
    sync_batches_repo,
    sync_jobs_repo,
    sync_runner,
    user_sync_pref_repo,
)
from backend.server.creds_store import AccountsRepo, LocalFernetBackend

dispatch_daily_payment_reminders_for_all_users = (
    payment_reminder_notifications.dispatch_daily_payment_reminders_for_all_users
)


def run_queued_jobs() -> dict[str, int]:
    for swept in (
        sync_jobs_repo.sweep_stale_queued(),
        sync_jobs_repo.sweep_stale_scheduled_queued(),
        sync_jobs_repo.sweep_stale_running(),
    ):
        for batch_id, user_id in swept.batches:
            sync_runner._maybe_send_batch_summary(batch_id=batch_id, user_id=user_id)
    counts = {"processed": 0, "done": 0, "failed": 0}
    while queued_ids := sync_jobs_repo.list_queued_ids():
        claimed = False
        for job_id in queued_ids:
            if not sync_runner._exec_sync(job_id):
                continue
            claimed = True
            counts["processed"] += 1
            row = sync_jobs_repo.get(job_id)
            if row and row["status"] in {"done", "failed"}:
                counts[row["status"]] += 1
        if not claimed:
            break
    return counts


def run_scheduled_slot(now: datetime | None = None) -> dict[str, int]:
    swept = sync_jobs_repo.sweep_stale_scheduled_queued()
    for batch_id, user_id in swept.batches:
        sync_runner._maybe_send_batch_summary(batch_id=batch_id, user_id=user_id)
    local_now = now or datetime.now(ZoneInfo("Asia/Taipei"))
    slot = f"{local_now.hour:02d}:00"
    counts = {"users": 0, "jobs": 0, "done": 0, "failed": 0}
    repo = AccountsRepo()
    store = LocalFernetBackend()

    for pref in user_sync_pref_repo.list_all_enabled():
        if slot not in pref["slots"]:
            continue
        counts["users"] += 1
        user_id = pref["user_id"]
        ready = [
            account
            for account in repo.list_for_user(user_id)
            if store.list_fields_acct(account.id, expected_owner_user_id=user_id)
        ]
        batch_id = None
        if ready:
            batch_id = sync_batches_repo.create(
                user_id=user_id,
                total_jobs=len(ready),
                kind=sync_batches_repo.KIND_SCHEDULED_ALL,
            )
        job_ids: list[int] = []
        try:
            for account in ready:
                job_ids.append(
                    sync_runner.run_sync_job_for_account(
                        account_id=account.id,
                        headless=True,
                        batch_id=batch_id,
                    ),
                )
        finally:
            sync_runner.reconcile_batch_fanout(
                batch_id=batch_id,
                user_id=user_id,
                job_ids=job_ids,
            )
        counts["jobs"] += len(job_ids)
        user_sync_pref_repo.mark_last_run(user_id=user_id)
    return counts


def run_payment_reminders() -> dict[str, int]:
    return dispatch_daily_payment_reminders_for_all_users(tz="Asia/Taipei")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("queued", "scheduled", "reminders"))
    args = parser.parse_args(argv)
    if args.mode == "queued":
        result = run_queued_jobs()
    elif args.mode == "scheduled":
        result = run_scheduled_slot()
    else:
        result = run_payment_reminders()
    print(json.dumps(result, sort_keys=True))
    return 1 if result.get("failed", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
