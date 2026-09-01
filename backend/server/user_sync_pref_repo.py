"""user_sync_preferences (server DB) — per-user daily auto-sync schedule.

設計 (L13, 2026-06-23 使用者指示):
  * user_id PK = 1 user 1 schedule (取代 L12 per-account 設計)
  * 固定 10:00 / 12:00 / 18:00 Asia/Taipei，不支援任意時間
  * enabled=0 vs 沒 row 兩種「停掉」, 前者保留時間值方便重啟
  * Azure scheduled job 在固定時段 fan-out 該 user 全部 has_creds account

Plan B (2026-06-19): server-level repo, 允許 raw SQL (見 test_plan_b_sql_audit.py allowlist).

⚠️ SQL placeholder 鐵令 (refresh_tokens.py 0.3.10 踩過的 q() double-encode 雷):
  - 用 `?` placeholder, get_conn().execute() 自動 escape
  - **絕對禁手動呼 q(...)** — 否則 PG double-encode 變 0 placeholder
  - test SQLite 全綠 PG 全爆 (詳 wiki azure-container-apps-pg-flexible-thoth-deploy)
"""
from __future__ import annotations

from typing import Any

from backend.server.db import get_conn, now_iso

_COLS = (
    "user_id, hour, minute, tz, enabled, last_run_at, created_at, updated_at"
)
ALLOWED_SYNC_SLOTS = frozenset({(10, 0), (12, 0), (18, 0)})
_ALLOWED_SYNC_SLOTS_LABEL = "10:00, 12:00, 18:00"


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "user_id": row[0],
        "hour": row[1],
        "minute": row[2],
        "tz": row[3],
        "enabled": (
            bool(row[4])
            and (row[1], row[2]) in ALLOWED_SYNC_SLOTS
            and row[3] == "Asia/Taipei"
        ),
        "last_run_at": row[5],
        "created_at": row[6],
        "updated_at": row[7],
    }


def upsert(
    *,
    user_id: int,
    hour: int,
    minute: int,
    tz: str = "Asia/Taipei",
    enabled: bool = True,
) -> dict[str, Any]:
    """建立或更新使用者的 daily auto-sync 排程 (1:1, user_id 是 PK).

    保留 last_run_at 不歸零 — 改 time 不該重設「上次跑」紀錄.
    新 row 自動 created_at = updated_at = now.
    """
    if not 0 <= hour <= 23:
        raise ValueError(f"hour 必須 0-23, 收到 {hour}")
    if not 0 <= minute <= 59:
        raise ValueError(f"minute 必須 0-59, 收到 {minute}")
    if (hour, minute) not in ALLOWED_SYNC_SLOTS:
        raise ValueError(f"自動同步時間只能是 {_ALLOWED_SYNC_SLOTS_LABEL}")
    if tz != "Asia/Taipei":
        raise ValueError("自動同步時區只能是 Asia/Taipei")

    now = now_iso()
    enabled_int = 1 if enabled else 0
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO user_sync_preferences
                (user_id, hour, minute, tz, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                hour = excluded.hour,
                minute = excluded.minute,
                tz = excluded.tz,
                enabled = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (user_id, hour, minute, tz, enabled_int, now, now),
        )
        row = conn.execute(
            f"SELECT {_COLS} FROM user_sync_preferences WHERE user_id=?",
            (user_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("UPSERT user_sync_preferences 後 SELECT 為 None")
    return _row_to_dict(row)


def get(user_id: int) -> dict[str, Any] | None:
    """取單一 user 的 preference. None = 從沒設過."""
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {_COLS} FROM user_sync_preferences WHERE user_id=?",
            (user_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_all_enabled() -> list[dict[str, Any]]:
    """全 enabled preference, scheduler boot 時撈出來 reload.

    回傳順序 by user_id 穩定.
    """
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {_COLS} FROM user_sync_preferences "
            "WHERE enabled=1 AND minute=0 AND hour IN (10, 12, 18) "
            "AND tz='Asia/Taipei' ORDER BY user_id"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def delete(user_id: int) -> bool:
    """硬刪一筆 — user 主動拔掉排程. 回傳是否真的有 row 被刪.

    Soft-delete 走 upsert(..., enabled=False), 兩種都有.
    """
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM user_sync_preferences WHERE user_id=?",
            (user_id,),
        )
        return (cur.rowcount or 0) > 0


def mark_last_run(*, user_id: int) -> None:
    """Scheduler fire 完一次後呼一次, 純 timestamp 紀錄.

    Phase 1 不算 ok/failed (因為 fire 是 fan-out 多 account, 沒單一狀態,
    且 sync_runner 自己會推 push 通知每個 account 完成/失敗).
    """
    now = now_iso()
    with get_conn() as conn:
        conn.execute(
            "UPDATE user_sync_preferences SET last_run_at=?, updated_at=? "
            "WHERE user_id=?",
            (now, now, user_id),
        )
