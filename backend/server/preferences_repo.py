"""user_preferences (server DB) — single-row-per-user, JSON blob payload."""

from __future__ import annotations

import json
from typing import Any

from backend.server.db import get_conn, now_iso


def get_payload(user_id: int) -> dict[str, Any]:
    """Return parsed payload (empty dict if no row / bad JSON)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT payload_json FROM user_preferences WHERE user_id=?",
            (user_id,),
        ).fetchone()
    if not row or not row[0]:
        return {}
    try:
        parsed = json.loads(row[0])
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def upsert_payload(user_id: int, payload: dict[str, Any]) -> None:
    """UPSERT one row per user."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO user_preferences (user_id, payload_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
            """,
            (user_id, json.dumps(payload, ensure_ascii=False), now_iso()),
        )
