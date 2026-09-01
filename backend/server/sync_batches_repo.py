"""sync_batches (server DB) — 聚合「同步全部」一次性 batch summary push.

設計 (2026-06-23, 使用者 Plan A):
  * `/sync/all` 跟 scheduler fan-out 觸發前 INSERT 一筆 batch, 把同次的 N 個
    sync_jobs 都 stamp 同個 batch_id.
  * 每個 job 收尾 (`sync_runner._exec_sync` mark_done/mark_failed 之後) 呼
    `claim_for_notification(batch_id)`. 兩階段 atomic:
      (a) 看 batch 內還有沒有 queued/running job → 有就 return None (還沒收完).
      (b) UPDATE sync_batches SET notified_at=now WHERE id=? AND notified_at IS NULL
          RETURNING ...  — race 輸的 (其他 thread 同時搶) 拿不到 row, return None.
    SQLite ≥3.35 + PG 都吃 `UPDATE ... RETURNING`, 一次走完原子.
  * `claim_for_notification` 拿到 row 的 caller 是唯一推 batch summary 的, 個別
    sync_done push 在 batch 內 skip (sync_runner 看 batch_id 是否 NULL 判定).
    sync_failed 不在這層處理 — sync_runner 失敗一律個別推 (失敗不能漏, 使用者同意).

⚠️ SQL placeholder 鐵令 (refresh_tokens.py 0.3.10 踩過的 q() double-encode 雷):
  - 用 `?` placeholder, get_conn().execute() 自動 escape
  - **絕對禁手動呼 q(...)** — 否則 PG double-encode 變 0 placeholder
  - test SQLite 全綠 PG 全爆 (詳 wiki azure-container-apps-pg-flexible-thoth-deploy)
"""

from __future__ import annotations

from typing import Any

from backend.server.db import get_conn, now_iso

# Kind constants — sync_batches.kind 唯二合法值. 任何新 kind 都該明確列在這
KIND_MANUAL_ALL = "manual_all"        # UI 按「同步全部」(POST /sync/all)
KIND_SCHEDULED_ALL = "scheduled_all"  # Azure scheduled job 自動 fan-out

_VALID_KINDS = frozenset({KIND_MANUAL_ALL, KIND_SCHEDULED_ALL})

_COLS = "id, user_id, total_jobs, kind, created_at, finished_at, notified_at"


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row[0],
        "user_id": row[1],
        "total_jobs": row[2],
        "kind": row[3],
        "created_at": row[4],
        "finished_at": row[5],
        "notified_at": row[6],
    }


def create(*, user_id: int, total_jobs: int, kind: str) -> int:
    """INSERT 一筆 batch, 回新 batch_id.

    `total_jobs`: 該 user has_creds 的 account 數 = 預期會排幾個 sync_job.
    `kind`: KIND_MANUAL_ALL | KIND_SCHEDULED_ALL.
    """
    if kind not in _VALID_KINDS:
        raise ValueError(f"kind={kind!r} 不合法, 必須是 {sorted(_VALID_KINDS)}")
    if total_jobs < 0:
        raise ValueError(f"total_jobs 不能負, 收到 {total_jobs}")
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO sync_batches
                (user_id, total_jobs, kind, created_at)
            VALUES (?, ?, ?, ?) RETURNING id
            """,
            (user_id, total_jobs, kind, now_iso()),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("INSERT sync_batches 後 RETURNING 為 None")
    return int(row[0])


def get(batch_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {_COLS} FROM sync_batches WHERE id=?",
            (batch_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def set_total_jobs(batch_id: int, total_jobs: int) -> None:
    if total_jobs <= 0:
        raise ValueError("total_jobs must be positive; delete an empty batch")
    with get_conn() as conn:
        conn.execute(
            "UPDATE sync_batches SET total_jobs=? WHERE id=? AND notified_at IS NULL",
            (total_jobs, batch_id),
        )


def delete(batch_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM sync_batches WHERE id=? AND notified_at IS NULL",
            (batch_id,),
        )


def claim_for_notification(batch_id: int) -> dict[str, Any] | None:
    """收尾 atomic CAS — 拿到 row 的 caller 是唯一推 batch summary 的.

    兩階段:
      (a) 確認 sync_jobs row 數已達 total_jobs，且沒有 queued/running；避免
          fan-out 尚未排完時，第一個快完成的 job 提早 claim.
      (b) UPDATE sync_batches SET notified_at=now, finished_at=now
          WHERE id=? AND notified_at IS NULL RETURNING ...
          — race 輸的 (notified_at 已被別人寫) 拿不到 row return None.

    回傳 None = 不該推 (還沒收完 / 已被別人推過).
    回傳 dict = 可以推, dict 是更新後的 batch row.

    注意: total_jobs=0 的 batch 也能 claim — 但 `/sync/all` route 在 total=0
    時根本不會建 batch (見 routers/sync.py), 所以實務上不會發生.
    """
    now = now_iso()
    with get_conn() as conn:
        # (a) batch membership 未齊或還有 in-flight job 就 return
        cur = conn.execute(
            "SELECT b.total_jobs, COUNT(j.id), "
            "COALESCE(SUM(CASE WHEN j.status IN ('queued', 'running') "
            "THEN 1 ELSE 0 END), 0) "
            "FROM sync_batches b "
            "LEFT JOIN sync_jobs j ON j.batch_id=b.id "
            "WHERE b.id=? GROUP BY b.total_jobs",
            (batch_id,),
        )
        row = cur.fetchone()
        if row is None or int(row[1]) != int(row[0]) or int(row[2]) > 0:
            return None

        # (b) atomic CAS: 只有 notified_at 仍 NULL 才贏
        cur = conn.execute(
            f"""
            UPDATE sync_batches
            SET notified_at=?, finished_at=?
            WHERE id=? AND notified_at IS NULL
            RETURNING {_COLS}
            """,
            (now, now, batch_id),
        )
        row = cur.fetchone()
    return _row_to_dict(row) if row else None
