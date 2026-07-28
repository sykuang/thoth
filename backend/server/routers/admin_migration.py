"""One-shot admin endpoint for the 2026-07-28 taxonomy recategorize migration.

為什麼需要這支
==============
`migrations/recategorize_20260728.py` 必須在 production 跑，但：

  - Azure PG 是 VNet-only (`publicNetworkAccess: Disabled`)，本機直連不通
  - `az containerapp exec` 是「SSH-like **interactive** shell」，內部呼叫
    `tty.setcbreak(sys.stdin)`，在非 TTY 環境直接 `termios.error:
    (25, 'Inappropriate ioctl for device')` — 自動化環境拿不到它的 stdout

所以唯一能「看得見輸出」地跑 migration 的方式，就是走 HTTP。

安全性
======
  - 需要已登入的 JWT（`current_user`），不是匿名端點
  - 額外要求 `X-Migration-Token` header 對上 env `MIGRATION_TOKEN`；
    env 沒設就一律 403（預設關閉，不會因為忘了拆而長期敞著）
  - 預設 dry-run，要寫入必須顯式 `?execute=true`

退場
====
migration 跑完即刪除本檔 + `app.py` 的 include_router + ACA 的 MIGRATION_TOKEN。
這是一次性工具，不是常駐 API。
"""
from __future__ import annotations

import os
import secrets
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from backend.server.deps import current_user

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_migration_token(token: str | None) -> None:
    expected = os.environ.get("MIGRATION_TOKEN", "").strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="遷移端點未啟用（未設定 MIGRATION_TOKEN）",
        )
    if not token or not secrets.compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="遷移權杖不正確",
        )


@router.post("/recategorize-20260728")
def run_recategorize(
    execute: bool = Query(False, description="true 才真的寫入，預設 dry-run"),
    x_migration_token: str | None = Header(None, alias="X-Migration-Token"),
    user: dict = Depends(current_user),
) -> dict[str, Any]:
    """重跑 categorizer 補全 taxonomy 欄位。預設 dry-run。"""
    _require_migration_token(x_migration_token)

    from migrations.recategorize_20260728 import (
        _load_rules,
        backfill_for_postgres,
        backfill_for_sqlite,
    )

    rules = _load_rules()
    backend = os.environ.get("DB_BACKEND", "sqlite").lower()
    dry_run = not execute
    result = (backfill_for_postgres(rules, dry_run) if backend in ("postgres", "pg")
              else backfill_for_sqlite(rules, dry_run))

    total = sum(r["total"] for r in result.values())
    changed = sum(r["changed"] for r in result.values())
    skipped = sum(r["skipped_user"] for r in result.values())
    return {
        "backend": backend,
        "dry_run": dry_run,
        "rules_loaded": len(rules),
        "total_rows": total,
        "changed": changed,
        "skipped_user_edited": skipped,
        "tables": {
            k: {"total": v["total"], "changed": v["changed"],
                "skipped_user": v["skipped_user"],
                "sample_changes": v["sample_changes"]}
            for k, v in result.items() if v["changed"] or v["skipped_user"]
        },
    }
