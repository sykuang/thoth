"""Per-user fixed-slot auto-sync preferences.

Each user may select any subset of 10:00, 12:00, and 18:00 Asia/Taipei.
``slots_json IS NULL`` identifies a legacy single-slot row; ``[]`` disables
scheduling.  The hour/minute/enabled columns remain for older clients.
"""
from __future__ import annotations

import json
from typing import Any

from backend.server.db import get_conn, now_iso

_COLS = (
    "user_id, hour, minute, tz, enabled, slots_json, last_run_at, created_at, updated_at"
)
ALLOWED_SYNC_SLOTS = frozenset({(10, 0), (12, 0), (18, 0)})
ALLOWED_SYNC_SLOT_LABELS = ("10:00", "12:00", "18:00")
_ALLOWED_SYNC_SLOTS_LABEL = "10:00, 12:00, 18:00"


def _canonical_slots(slots: list[str]) -> list[str]:
    if (
        len(slots) > len(ALLOWED_SYNC_SLOT_LABELS)
        or len(set(slots)) != len(slots)
        or any(slot not in ALLOWED_SYNC_SLOT_LABELS for slot in slots)
    ):
        raise ValueError(
            f"自動同步時間只能從 {_ALLOWED_SYNC_SLOTS_LABEL} 選擇 0-3 個"
        )
    selected = set(slots)
    return [slot for slot in ALLOWED_SYNC_SLOT_LABELS if slot in selected]


def _slots_from_row(row: Any) -> list[str]:
    raw = row[5]
    if raw is None:
        if type(row[1]) is not int or type(row[2]) is not int:
            return []
        legacy = f"{row[1]:02d}:{row[2]:02d}"
        return (
            [legacy]
            if bool(row[4])
            and (row[1], row[2]) in ALLOWED_SYNC_SLOTS
            and row[3] == "Asia/Taipei"
            else []
        )
    try:
        decoded = json.loads(raw)
        if not isinstance(decoded, list) or not all(
            isinstance(value, str) for value in decoded
        ):
            return []
        return _canonical_slots(decoded)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _row_to_dict(row: Any) -> dict[str, Any]:
    slots = _slots_from_row(row)
    return {
        "user_id": row[0],
        "hour": row[1],
        "minute": row[2],
        "tz": row[3],
        "enabled": bool(slots),
        "slots": slots,
        "last_run_at": row[6],
        "created_at": row[7],
        "updated_at": row[8],
    }


def _write_slots(
    *,
    user_id: int,
    slots: list[str],
    tz: str,
    fallback_hour: int = 10,
) -> dict[str, Any]:
    canonical = _canonical_slots(slots)
    if tz != "Asia/Taipei":
        raise ValueError("自動同步時區只能是 Asia/Taipei")
    hour = int(canonical[0].split(":", 1)[0]) if canonical else fallback_hour
    now = now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO user_sync_preferences
                (user_id, hour, minute, tz, enabled, slots_json, created_at, updated_at)
            VALUES (?, ?, 0, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                hour = excluded.hour,
                minute = 0,
                tz = excluded.tz,
                enabled = excluded.enabled,
                slots_json = excluded.slots_json,
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                hour,
                tz,
                int(bool(canonical)),
                json.dumps(canonical),
                now,
                now,
            ),
        )
        row = conn.execute(
            f"SELECT {_COLS} FROM user_sync_preferences WHERE user_id=?",
            (user_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("UPSERT user_sync_preferences 後 SELECT 為 None")
    return _row_to_dict(row)


def upsert_slots(
    *,
    user_id: int,
    slots: list[str],
    tz: str = "Asia/Taipei",
) -> dict[str, Any]:
    """Persist the selected subset of the three fixed Taipei slots."""
    return _write_slots(user_id=user_id, slots=slots, tz=tz)


def upsert(
    *,
    user_id: int,
    hour: int,
    minute: int,
    tz: str = "Asia/Taipei",
    enabled: bool = True,
) -> dict[str, Any]:
    """Backward-compatible single-slot write for older clients and callers."""
    if not 0 <= hour <= 23:
        raise ValueError(f"hour 必須 0-23, 收到 {hour}")
    if not 0 <= minute <= 59:
        raise ValueError(f"minute 必須 0-59, 收到 {minute}")
    if (hour, minute) not in ALLOWED_SYNC_SLOTS:
        raise ValueError(f"自動同步時間只能是 {_ALLOWED_SYNC_SLOTS_LABEL}")
    label = f"{hour:02d}:{minute:02d}"
    return _write_slots(
        user_id=user_id,
        slots=[label] if enabled else [],
        tz=tz,
        fallback_hour=hour,
    )


def get(user_id: int) -> dict[str, Any] | None:
    """Return one user's preference, or None when it was never configured."""
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {_COLS} FROM user_sync_preferences WHERE user_id=?",
            (user_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_all_enabled() -> list[dict[str, Any]]:
    """Return enabled preferences in stable user-id order."""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {_COLS} FROM user_sync_preferences "
            "WHERE enabled=1 AND tz='Asia/Taipei' ORDER BY user_id"
        ).fetchall()
    return [pref for row in rows if (pref := _row_to_dict(row))["slots"]]


def delete(user_id: int) -> bool:
    """Hard-delete one user's preference."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM user_sync_preferences WHERE user_id=?",
            (user_id,),
        )
        return (cur.rowcount or 0) > 0


def mark_last_run(*, user_id: int) -> None:
    """Record the most recent scheduled fan-out timestamp."""
    now = now_iso()
    with get_conn() as conn:
        conn.execute(
            "UPDATE user_sync_preferences SET last_run_at=?, updated_at=? "
            "WHERE user_id=?",
            (now, now, user_id),
        )
