"""card_auto_debit_settings (server DB) — per-user, per-card-bank 扣繳帳號設定.

Phase L10 (2026-06-20). 設計 spec:
  * 一個 user 一個 card_bank 一筆 setting (A2 per-bank, Q1 決議)
  * account 跨銀行允許 (B2, Q2)
  * G4: account_no 必須 currency='TWD' (Q7)，由 caller 在寫入前驗證
  * 自動扣繳設定不存進 bank.sqlite 是因為 sync 會 wipe / 跨銀行不適合
    (詳見 db.py CREATE TABLE 上方註解)
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.server.db import get_conn, now_iso


@dataclass(frozen=True)
class AutoDebitSetting:
    card_bank: str         # 信用卡所屬銀行 ('cathay', 'ctbc', ...)
    account_bank: str      # 扣繳戶所在銀行 (可跨 — 'sinopac' 等)
    account_no: str        # 扣繳戶帳號
    updated_at: str


def list_settings(user_id: int) -> list[AutoDebitSetting]:
    """List all auto-debit settings for a user, ordered by card_bank."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT card_bank, account_bank, account_no, updated_at
               FROM card_auto_debit_settings
               WHERE user_id = ?
               ORDER BY card_bank""",
            (user_id,),
        ).fetchall()
    return [
        AutoDebitSetting(
            card_bank=r[0],
            account_bank=r[1],
            account_no=r[2],
            updated_at=r[3],
        )
        for r in rows
    ]


def get_setting(user_id: int, card_bank: str) -> AutoDebitSetting | None:
    """Get single setting for (user, card_bank). None if not set."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT card_bank, account_bank, account_no, updated_at
               FROM card_auto_debit_settings
               WHERE user_id = ? AND card_bank = ?""",
            (user_id, card_bank),
        ).fetchone()
    if not row:
        return None
    return AutoDebitSetting(
        card_bank=row[0],
        account_bank=row[1],
        account_no=row[2],
        updated_at=row[3],
    )


def upsert_setting(
    user_id: int,
    card_bank: str,
    account_bank: str,
    account_no: str,
) -> AutoDebitSetting:
    """UPSERT setting (PK = user_id, card_bank). Returns saved row."""
    ts = now_iso()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO card_auto_debit_settings
                 (user_id, card_bank, account_bank, account_no, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, card_bank) DO UPDATE SET
                 account_bank = excluded.account_bank,
                 account_no   = excluded.account_no,
                 updated_at   = excluded.updated_at""",
            (user_id, card_bank, account_bank, account_no, ts),
        )
    return AutoDebitSetting(
        card_bank=card_bank,
        account_bank=account_bank,
        account_no=account_no,
        updated_at=ts,
    )


def delete_setting(user_id: int, card_bank: str) -> bool:
    """Delete setting. Returns True if a row was deleted."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM card_auto_debit_settings WHERE user_id = ? AND card_bank = ?",
            (user_id, card_bank),
        )
        return (cur.rowcount or 0) > 0


def settings_by_card_bank(user_id: int) -> dict[str, AutoDebitSetting]:
    """Convenience: {card_bank -> AutoDebitSetting} for fast lookup in
    reminder logic (one DB hit, then dict-lookup per card)."""
    return {s.card_bank: s for s in list_settings(user_id)}
