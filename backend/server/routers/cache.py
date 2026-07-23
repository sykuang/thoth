"""Frontend dataset cache endpoints.

`/cache/snapshot` gives the frontend one full local dataset to filter in memory.
The transactions UI intentionally does not call scoped `/transactions?...` or
incremental `/cache/changes?...`; user-visible filter state is the sole filter
source after this whole-DB snapshot is fetched.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from backend.core import bank_data
from backend.server.deps import current_user
from backend.server.db_facade import db_api
from backend.server.routers.cards import _card_to_response
from backend.server.routers.portfolio import portfolio_accounts
from backend.server.routers.transactions import (
    _billed_to_transaction,
    _pending_to_transaction,
    _twd_to_transaction,
)
from backend.server.routers.portfolio import get_excluded_account_nos
from backend.server.routers.cards import get_excluded_card_nos

router = APIRouter(prefix="/cache", tags=["cache"])


def _cursor_max(*values: str | None) -> str:
    vals = [v for v in values if v]
    return max(vals) if vals else "1970-01-01T00:00:00"


def _row_updated_at(row: Any) -> str | None:
    try:
        return row["updated_at"]
    except Exception:
        return getattr(row, "updated_at", None)


def _txn_cursor(row: Any) -> str | None:
    # User edits on transactions do not have updated_at in all schemas, so first_seen
    # is the stable ingestion cursor. New/updated UI fields still self-heal via later
    # full snapshot or mutation write-through.
    try:
        return row["first_seen"] or row["refreshed_at"]
    except Exception:
        return getattr(row, "first_seen", None) or getattr(row, "refreshed_at", None)


def _collect_cache_payload(user_id: int, *, since: str | None = None) -> dict[str, Any]:
    accounts = portfolio_accounts({"id": user_id})

    cards: list[dict[str, Any]] = []
    transactions: list[dict[str, Any]] = []
    cursor = "1970-01-01T00:00:00"
    for a in accounts:
        snapshot_date = getattr(a, "snapshot_date", None)
        cursor = _cursor_max(cursor, f"{snapshot_date}T00:00:00" if snapshot_date else None)

    for bank in bank_data.KNOWN_BANKS:
        bank_cards = [_card_to_response(c) for c in db_api.list_cards(bank=bank, user_id=user_id, include_inactive=False)]
        for c in bank_cards:
            cursor = _cursor_max(cursor, c.get("updated_at"))
        if since:
            bank_cards = [c for c in bank_cards if (c.get("updated_at") or "") > since]
        cards.extend(bank_cards)

    excluded_accounts_map = get_excluded_account_nos(user_id)
    excluded_cards_map = get_excluded_card_nos(user_id)
    for bank in bank_data.KNOWN_BANKS:
        bank_excluded_accounts = excluded_accounts_map.get(bank, set())
        bank_excluded_cards = excluded_cards_map.get(bank, set())
        for txn_row in db_api.list_txns_for_bank(
            bank=bank,
            user_id=user_id,
            kinds=["twd", "billed", "pending"],
        ):
            row_cursor = _txn_cursor(txn_row)
            cursor = _cursor_max(cursor, row_cursor)
            if since and (row_cursor or "") <= since:
                continue
            if txn_row.kind == "twd":
                transactions.append(_twd_to_transaction(bank, txn_row, bank_excluded_accounts))
            elif txn_row.kind == "billed":
                transactions.append(_billed_to_transaction(bank, txn_row, bank_excluded_cards))
            elif txn_row.kind == "pending":
                transactions.append(_pending_to_transaction(bank, txn_row, bank_excluded_cards))

    transactions.sort(key=lambda t: (t.get("date") or "0000-00-00", t.get("datetime") or ""), reverse=True)
    return {
        "cursor": cursor,
        "accounts": [a.model_dump() for a in accounts],
        "cards": cards,
        "transactions": transactions,
    }


@router.get("/snapshot")
def cache_snapshot(user: dict = Depends(current_user)) -> dict[str, Any]:
    return _collect_cache_payload(user["id"])


@router.get("/changes")
def cache_changes(
    since: str = Query(..., description="Cursor returned by /cache/snapshot or previous /cache/changes"),
    user: dict = Depends(current_user),
) -> dict[str, Any]:
    return _collect_cache_payload(user["id"], since=since)
