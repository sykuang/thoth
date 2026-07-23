"""user_push_tokens (server DB) — multi-device-per-user, multi-provider tokens.

設計（呼應 `push/base.py`）:
  * 一個 user 可同時有多 device (Kphone + iPad + 老婆手機 + ...)
  * 一個 device 一個 row (PRIMARY by id, UNIQUE on (provider, token))
  * provider 欄區隔 — 同個 user 可同時有 apns token + webhook URL
  * `last_used_at` 給 active filter 用 (90 天沒用的 token 自動 prune)
  * `active` flag — 收到 provider "device unregistered" 回 → 設 0, 不刪 row,
    保留 audit log (debug 「為什麼 user 收不到」用)

Plan B (2026-06-19): server-level repo, 允許 raw SQL (見 test_plan_b_sql_audit.py allowlist).
"""
from __future__ import annotations

from typing import Any

from backend.server.db import get_conn, now_iso
from backend.server.push.base import PushTarget


# 90 天沒用的 token 視為 stale；list_active_tokens 不會回傳
ACTIVE_THRESHOLD_DAYS = 90


def register(
    user_id: int,
    provider: str,
    token: str,
    platform: str | None = None,
    device_label: str | None = None,
) -> dict[str, Any]:
    """註冊 / 刷新一個 device token (UPSERT on (provider, token))。

    iOS / Android 重裝後 token 會變，所以這個是 idempotent UPSERT。
    若同 token 已存在但綁不同 user (轉手手機 / 多帳號)，更新到新 user_id。
    """
    now = now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO user_push_tokens
                (user_id, provider, token, platform, device_label,
                 created_at, last_used_at, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(provider, token) DO UPDATE SET
                user_id      = excluded.user_id,
                platform     = excluded.platform,
                device_label = excluded.device_label,
                last_used_at = excluded.last_used_at,
                active       = 1
            """,
            (user_id, provider, token, platform, device_label, now, now),
        )
        row = conn.execute(
            "SELECT id, user_id, provider, token, platform, device_label, "
            "created_at, last_used_at, active "
            "FROM user_push_tokens WHERE provider=? AND token=?",
            (provider, token),
        ).fetchone()
    return _row_to_dict(row)


def deactivate(provider: str, token: str) -> bool:
    """標記 token 失效 (provider 回 "device unregistered" 時)。

    不刪 row — 保留歷史 audit。回傳是否真的有 row 被更新。
    """
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE user_push_tokens SET active=0 "
            "WHERE provider=? AND token=? AND active=1",
            (provider, token),
        )
        return (cur.rowcount or 0) > 0


def remove(user_id: int, token_id: int) -> bool:
    """硬刪一筆 (user logout / 手動移除 device)。

    必驗 user_id 防止 user 刪別人的 token。
    """
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM user_push_tokens WHERE id=? AND user_id=?",
            (token_id, user_id),
        )
        return (cur.rowcount or 0) > 0


def list_active_for_user(
    user_id: int,
    provider: str | None = None,
) -> list[PushTarget]:
    """撈 user 的 active tokens (可選 filter 單一 provider)。

    給 Notifier.send_to_user 用 — 撈完直接送。
    """
    sql = (
        "SELECT user_id, provider, token, platform, device_label "
        "FROM user_push_tokens WHERE user_id=? AND active=1"
    )
    params: list[Any] = [user_id]
    if provider:
        sql += " AND provider=?"
        params.append(provider)
    sql += " ORDER BY last_used_at DESC"
    with get_conn() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [
        PushTarget(
            user_id=r[0],
            provider=r[1],
            token=r[2],
            platform=r[3],
            device_label=r[4],
        )
        for r in rows
    ]


def list_devices_for_user(user_id: int) -> list[dict[str, Any]]:
    """給 GET /me/push-tokens 用 — 含 active flag + timestamps。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, user_id, provider, token, platform, device_label, "
            "created_at, last_used_at, active "
            "FROM user_push_tokens WHERE user_id=? "
            "ORDER BY active DESC, last_used_at DESC",
            (user_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def touch(provider: str, token: str) -> None:
    """更新 last_used_at — Notifier 成功送出後可選呼叫。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE user_push_tokens SET last_used_at=? "
            "WHERE provider=? AND token=?",
            (now_iso(), provider, token),
        )


def _row_to_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    # Truncate token in API output — full token 是 secret, 別 leak 出 list endpoint
    token_full = row[3] or ""
    token_preview = (
        token_full[:8] + "…" + token_full[-4:] if len(token_full) > 14 else token_full
    )
    return {
        "id": row[0],
        "user_id": row[1],
        "provider": row[2],
        "token_preview": token_preview,  # 只給 preview, 不回 full token
        "platform": row[4],
        "device_label": row[5],
        "created_at": row[6],
        "last_used_at": row[7],
        "active": bool(row[8]),
    }
