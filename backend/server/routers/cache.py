"""Legacy full-snapshot frontend dataset endpoint."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from backend.core import bank_data
from backend.server.deps import current_user
from backend.server.db_facade import db_api
from backend.server.routers.cards import _card_to_response, get_excluded_card_nos
from backend.server.routers.portfolio import get_excluded_account_nos, portfolio_accounts
from backend.server.routers.transactions import (
    _billed_to_transaction,
    _expand_splits,
    _pending_to_transaction,
    _twd_to_transaction,
)

router = APIRouter(prefix="/cache", tags=["cache"])


def _cursor_max(*values: str | None) -> str:
    vals = [value for value in values if value]
    return max(vals) if vals else "1970-01-01T00:00:00"


def _txn_cursor(row: Any) -> str | None:
    try:
        return row["first_seen"] or row["refreshed_at"]
    except Exception:
        return getattr(row, "first_seen", None) or getattr(row, "refreshed_at", None)


def _collect_cache_payload(user_id: int) -> dict[str, Any]:
    accounts = portfolio_accounts({"id": user_id})
    cards: list[dict[str, Any]] = []
    transactions: list[dict[str, Any]] = []
    cursor = "1970-01-01T00:00:00"
    for account in accounts:
        snapshot_date = getattr(account, "snapshot_date", None)
        cursor = _cursor_max(
            cursor,
            f"{snapshot_date}T00:00:00" if snapshot_date else None,
        )

    for bank in bank_data.KNOWN_BANKS:
        bank_cards = [
            _card_to_response(card)
            for card in db_api.list_cards(
                bank=bank,
                user_id=user_id,
                include_inactive=False,
            )
        ]
        for card in bank_cards:
            cursor = _cursor_max(cursor, card.get("updated_at"))
        cards.extend(bank_cards)

    excluded_accounts_map = get_excluded_account_nos(user_id)
    excluded_cards_map = get_excluded_card_nos(user_id)
    for bank in bank_data.KNOWN_BANKS:
        excluded_accounts = excluded_accounts_map.get(bank, set())
        excluded_cards = excluded_cards_map.get(bank, set())
        for row in db_api.list_txns_for_bank(
            bank=bank,
            user_id=user_id,
            kinds=["twd", "billed", "pending"],
        ):
            cursor = _cursor_max(cursor, _txn_cursor(row))
            if row.kind == "twd":
                transactions.extend(_expand_splits(
                    _twd_to_transaction(bank, row, excluded_accounts),
                ))
            elif row.kind == "billed":
                transactions.extend(_expand_splits(
                    _billed_to_transaction(bank, row, excluded_cards),
                ))
            elif row.kind == "pending":
                transactions.extend(_expand_splits(
                    _pending_to_transaction(bank, row, excluded_cards),
                ))

    transactions.sort(
        key=lambda transaction: (
            transaction.get("date") or "0000-00-00",
            transaction.get("datetime") or "",
        ),
        reverse=True,
    )
    return {
        "cursor": cursor,
        "accounts": [account.model_dump() for account in accounts],
        "cards": cards,
        "transactions": transactions,
    }


@router.get("/snapshot")
def cache_snapshot(user: dict = Depends(current_user)) -> dict[str, Any]:
    return _collect_cache_payload(user["id"])
