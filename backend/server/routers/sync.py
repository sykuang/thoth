"""Phase 1 - /sync routes (trigger + polling for sync status).

Phase 1 — /sync routes（觸發 + polling 查狀態）。

Endpoints:
  POST /sync/admin/account/{account_id}/full-history  → 202 {job_id} (ADMIN_API_KEY)
  POST /sync/{bank}                    body={?headless: true}   → 202 {job_id} (legacy)
  POST /sync/account/{account_id}      body={?headless: true}   → 202 {job_id} [L5-1 新]
  GET  /sync/jobs                      → [recent 50 jobs (per user)]
  GET  /sync/jobs/{job_id}             → {status, started_at, ...} or 404
"""
from __future__ import annotations

import os
import secrets

from fastapi import APIRouter, Body, Depends, Header, HTTPException, status

from backend.server.deps import current_user
from backend.server.creds_store import AccountsRepo
from backend.server.sync_runner import (
    SUPPORTED_BANKS,
    get_job,
    list_recent_jobs,
    reconcile_batch_fanout,
    run_sync_job,
    run_sync_job_for_account,
    supports_attested_history,
)

router = APIRouter(prefix="/sync", tags=["sync"])


def _require_admin_api_key(
    x_admin_key: str = Header(default="", alias="X-Admin-Key"),
) -> None:
    expected = os.environ.get("ADMIN_API_KEY", "").strip()
    client_key = os.environ.get("SERVER_API_KEY", "").strip()
    if not expected or (client_key and secrets.compare_digest(expected, client_key)):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "admin sync is disabled")
    if not secrets.compare_digest(x_admin_key.strip(), expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid admin API key")


@router.post(
    "/admin/account/{account_id}/full-history",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_require_admin_api_key)],
)
def force_full_history_sync(account_id: int) -> dict:
    """Admin-only recovery path; normal account syncs stay incremental after first success."""
    acct = AccountsRepo().get(account_id)
    if acct is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "找不到此帳號")
    if not supports_attested_history(acct.bank):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "此銀行尚未支援可驗證的完整歷史同步",
        )
    job_id = run_sync_job_for_account(
        account_id=account_id,
        headless=True,
        force_full_history=True,
    )
    return {
        "job_id": job_id,
        "account_id": account_id,
        "bank": acct.bank,
        "history_mode": "full",
        "status": "queued",
    }


@router.post("/all", status_code=status.HTTP_202_ACCEPTED)
def trigger_sync_all(
    body: dict = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict:
    """[L7-2] 觸發本 user 所有「已設定憑證」帳號的 sync job。

    對每個有任何加密欄位的 account 各 schedule 一個背景 job。
    回傳 job_ids list, 前端可 poll /sync/jobs 看進度。

    跳過: 尚未填欄位 的 account。

    2026-06-23 (Plan A): ready accounts > 0 時建一筆 sync_batches row, 所有 job
    共用同 batch_id, 收尾走 _maybe_send_batch_summary 推「同步全部完成」一則
    取代每家銀行各推 sync_done (12 家原本 = 12 則噪音). 失敗的 job 仍個別推
    sync_failed (失敗不能漏, 使用者同意).
    """
    from backend.server import sync_batches_repo
    from backend.server.creds_store import LocalFernetBackend
    repo = AccountsRepo()
    store = LocalFernetBackend()
    accts = repo.list_for_user(user["id"])
    headless = bool(body.get("headless", True))

    # 第一遍: 分 ready / skipped, 算 ready 數才能定 batch.total_jobs
    ready_accts = []
    skipped: list[dict] = []
    for acct in accts:
        fields = store.list_fields_acct(acct.id, expected_owner_user_id=user["id"])
        if not fields:
            skipped.append({
                "account_id": acct.id,
                "bank": acct.bank,
                "label": acct.label,
                "reason": "尚未設定登入欄位",
            })
            continue
        ready_accts.append(acct)

    # 沒 ready account → 不建 batch (避免 total_jobs=0 batch claim 立刻搶贏卻沒東西可推)
    batch_id: int | None = None
    if ready_accts:
        batch_id = sync_batches_repo.create(
            user_id=user["id"],
            total_jobs=len(ready_accts),
            kind=sync_batches_repo.KIND_MANUAL_ALL,
        )

    job_results: list[dict] = []
    try:
        for acct in ready_accts:
            job_id = run_sync_job_for_account(
                account_id=acct.id, headless=headless, batch_id=batch_id,
            )
            job_results.append({
                "job_id": job_id,
                "account_id": acct.id,
                "bank": acct.bank,
                "label": acct.label,
                "status": "queued",
            })
    finally:
        reconcile_batch_fanout(
            batch_id=batch_id,
            user_id=user["id"],
            job_ids=[item["job_id"] for item in job_results],
        )

    return {
        "queued": len(job_results),
        "skipped": len(skipped),
        "jobs": job_results,
        "skipped_accounts": skipped,
        "batch_id": batch_id,
    }


@router.post("/account/{account_id}", status_code=status.HTTP_202_ACCEPTED)
def trigger_sync_for_account(
    account_id: int,
    body: dict = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict:
    """[L5-1] 觸發指定 account 的 sync job。

    驗 ownership 後 INSERT queued + 開背景 thread。
    """
    acct = AccountsRepo().get(account_id)
    if acct is None or acct.user_id != user["id"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "找不到此帳號")
    headless = bool(body.get("headless", True))
    job_id = run_sync_job_for_account(account_id=account_id, headless=headless)
    return {
        "job_id": job_id,
        "account_id": account_id,
        "bank": acct.bank,
        "label": acct.label,
        "status": "queued",
    }


@router.post("/{bank}", status_code=status.HTTP_202_ACCEPTED)
def trigger_sync(
    bank: str,
    body: dict = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict:
    """[Legacy] 觸發 sync job（背景 thread 跑），立刻回 job_id。

    L5-1 起新 caller 應走 POST /sync/account/{account_id}。本路徑仍能用,
    走 user+bank 直接撈 v1 表 cred (沒 multi-account 概念)。
    """
    bank = bank.lower()
    if bank not in SUPPORTED_BANKS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"unknown bank: {bank!r}; supported: {sorted(SUPPORTED_BANKS)}",
        )
    headless = bool(body.get("headless", True))
    job_id = run_sync_job(user_id=user["id"], bank=bank, headless=headless)
    return {"job_id": job_id, "bank": bank, "status": "queued"}


@router.get("/jobs")
def list_jobs(user: dict = Depends(current_user)) -> list[dict]:
    """近 50 筆 job（本 user）。"""
    return list_recent_jobs(user_id=user["id"], limit=50)


@router.get("/jobs/{job_id}")
def get_job_route(job_id: int, user: dict = Depends(current_user)) -> dict:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"找不到任務 #{job_id}")
    if job["user_id"] != user["id"]:
        # 不洩露存在性 → 統一 404
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"找不到任務 #{job_id}")
    return job
