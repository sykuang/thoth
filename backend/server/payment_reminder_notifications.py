"""Daily payment reminder push notification dispatcher.

This module bridges the dashboard reminder logic (`/cards/auto-debit/reminders`)
to the pluggable push subsystem. It is intentionally server-side only:
- compute the same reminders as Dashboard
- claim only reminders that have not been notified for the user's local day
- send one aggregated notification to avoid noisy per-card fanout

SQL placeholder rule: always write `?`; `get_conn()` adapts SQLite/PG.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.server.db import IntegrityError, get_conn, now_iso

logger = logging.getLogger("backend.payment_reminders.push")

_BANK_LABELS = {
    "cathay": "國泰世華",
    "ubot": "聯邦銀行",
    "hsbc": "匯豐銀行",
    "ctbc": "中國信託",
    "sinopac": "永豐銀行",
    "scsb": "上海商銀",
    "esun": "玉山銀行",
    "taishin": "台新銀行",
    "fubon": "富邦銀行",
    "dbs": "星展銀行",
    "scb": "渣打銀行",
    "linebank": "LINE Bank",
}


def _today_plus(days: int, *, base: date | None = None) -> date:
    """Test helper + date arithmetic seam aligned with dispatcher local day."""
    return (base or _local_date("Asia/Taipei")) + timedelta(days=days)


def _local_date(tz: str) -> date:
    try:
        zone = ZoneInfo(tz)
    except ZoneInfoNotFoundError:
        logger.warning("[payment-reminder] unknown tz=%r, fallback Asia/Taipei", tz)
        zone = ZoneInfo("Asia/Taipei")
    return datetime.now(zone).date()


def _claim_card_key(reminder: dict[str, Any]) -> str:
    """Map API card identity to the existing NOT NULL DB dedupe column.

    Non-HSBC whole-bank reminders intentionally expose ``card_no=''`` so UI/push
    cannot pretend the bill belongs to one card.  The DB key still needs the
    amount because two distinct facts can share bank, due date, and reason.
    """
    card_no = str(reminder.get("card_no") or "")
    if card_no:
        return card_no
    amount = round(float(reminder["bill_due_amount"]), 2)
    return f"__bank__:{amount:.2f}"


def _claim_once_today(
    *,
    user_id: int,
    reminder: dict[str, Any],
    reminder_date: str,
) -> bool:
    """Return True only the first time this reminder is claimed for local date."""
    ts = now_iso()
    try:
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO payment_reminder_notifications
                    (user_id, card_bank, card_no, payment_due_date, reason,
                     reminder_date, created_at, notified_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    reminder["card_bank"],
                    _claim_card_key(reminder),
                    reminder["payment_due_date"],
                    reminder["reason"],
                    reminder_date,
                    ts,
                    ts,
                ),
            )
        return True
    except IntegrityError:
        return False


def _fmt_twd(amount: float | int) -> str:
    return f"NT${round(float(amount)):,.0f}"


def _days_label(days: int) -> str:
    if days == 0:
        return "今天到期"
    if days == 1:
        return "明天到期"
    return f"{days} 天後到期"


def _card_label(reminder: dict[str, Any]) -> str:
    card_bank = str(reminder["card_bank"])
    bank = _BANK_LABELS.get(card_bank, card_bank)
    name = reminder.get("card_name")
    return f"{bank}・{name}" if name else bank


def _body_for(claimed: list[dict[str, Any]]) -> str:
    first = claimed[0]
    first_line = (
        f"{_card_label(first)} {_days_label(int(first['days_until_due']))}，"
        f"應繳 {_fmt_twd(first['bill_due_amount'])}"
    )
    if first.get("reason") == "insufficient" and first.get("shortfall") is not None:
        first_line += f"（扣繳戶差 {_fmt_twd(first['shortfall'])}）"
    elif first.get("reason") == "no_account":
        first_line += "（未設定自動扣繳）"

    if len(claimed) == 1:
        return first_line
    return f"{first_line}；另 {len(claimed) - 1} 筆。"


def dispatch_daily_payment_reminders(*, user_id: int, tz: str = "Asia/Taipei") -> dict[str, int]:
    """Send one aggregated daily push for this user's current payment reminders.

    Returns counters for scheduler logs/tests:
      {"sent": claimed_count, "skipped": duplicate_count, "total": reminders_count}

    Push failures are swallowed/logged. Dedupe rows are claimed before delivery so a
    broken provider cannot spam the same reminder repeatedly within the same day.
    """
    today = _local_date(tz)
    reminder_date = today.isoformat()

    # Import lazily: routers import this subsystem too, avoid app bootstrap cycles.
    from backend.server.routers.auto_debit import build_payment_reminders

    reminders = build_payment_reminders(user_id=user_id, today=today)
    if not reminders:
        return {"sent": 0, "skipped": 0, "total": 0}

    claimed: list[dict[str, Any]] = []
    skipped = 0
    for reminder in reminders:
        if _claim_once_today(user_id=user_id, reminder=reminder, reminder_date=reminder_date):
            claimed.append(reminder)
        else:
            skipped += 1

    if not claimed:
        return {"sent": 0, "skipped": skipped, "total": len(reminders)}

    try:
        from backend.server.push import NotificationPayload, get_notifier

        payload = NotificationPayload(
            title="繳費提醒",
            body=_body_for(claimed),
            data={
                "deep_link": "/(tabs)/cards",
                "kind": "payment_reminder",
                "count": str(len(claimed)),
                "reminder_date": reminder_date,
            },
            category="payment_reminder",
        )
        notifier = get_notifier()
        logger.info(
            "[payment-reminder] dispatch user_id=%s count=%d skipped=%d notifier=%s",
            user_id, len(claimed), skipped, notifier.__class__.__name__,
        )
        result = notifier.send_to_user(user_id=user_id, payload=payload)
        logger.info(
            "[payment-reminder] result user_id=%s delivered=%s failed=%s",
            user_id,
            getattr(result, "delivered_count", "?"),
            getattr(result, "failed_count", "?"),
        )
    except Exception as exc:
        logger.warning(
            "[payment-reminder] dispatch failed user_id=%s error_type=%s",
            user_id, type(exc).__name__,
        )

    return {"sent": len(claimed), "skipped": skipped, "total": len(reminders)}


def _list_all_user_ids() -> list[int]:
    with get_conn() as conn:
        rows = conn.execute("SELECT id FROM users ORDER BY id").fetchall()
    return [int(r[0]) for r in rows]


def dispatch_daily_payment_reminders_for_all_users(tz: str = "Asia/Taipei") -> dict[str, int]:
    """Run payment reminder push for all users once per day.

    This is a reminder-specific daily sweep, independent from auto-sync settings:
    even a user who does not enable automatic bank sync can still receive payment
    due alerts from already-synced card data.
    """
    user_ids = _list_all_user_ids()
    totals = {"users": len(user_ids), "sent": 0, "skipped": 0, "total": 0}
    for user_id in user_ids:
        try:
            result = dispatch_daily_payment_reminders(user_id=user_id, tz=tz)
            totals["sent"] += result["sent"]
            totals["skipped"] += result["skipped"]
            totals["total"] += result["total"]
        except Exception as exc:
            logger.warning(
                "[payment-reminder] user sweep failed user_id=%s error_type=%s",
                user_id, type(exc).__name__,
            )
    return totals
