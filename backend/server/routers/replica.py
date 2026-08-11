"""Minimum read API for the local-first frontend replica."""
from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from backend.core import bank_data
from backend.server import (
    auto_debit_settings_repo,
    db,
    fx_service,
    preferences_repo,
    rules_repo,
    yahoo_finance,
)
from backend.server.creds_store import list_account_metadata
from backend.server.deps import current_user
from backend.server.financial_accounts import manual_replica
from backend.server.replica_facts import collect_bank_replica_facts
from backend.server.replica_repo import ReplicaPartition, reconcile_partitions

SCHEMA_VERSION = 2

router = APIRouter(prefix="/replica", tags=["replica"])


class ReplicaPullRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    generations: dict[str, Annotated[int, Field(ge=0)]] = Field(
        default_factory=dict,
        max_length=64,
    )


def _current_payloads(user_id: int) -> dict[str, dict[str, Any]]:
    manual = manual_replica(user_id)
    brokerage = db.snaptrade_snapshot(user_id)
    payloads: dict[str, dict[str, Any]] = {
        "user": {
            "bank_accounts": sorted(
                list_account_metadata(user_id),
                key=lambda row: (row["bank"], row["id"]),
            ),
            "preferences": preferences_repo.get_payload(user_id),
            "rules": sorted(
                rules_repo.list_rules(user_id),
                key=lambda row: (-row["priority"], row["id"]),
            ),
            "auto_debit_settings": sorted(
                (asdict(setting) for setting in auto_debit_settings_repo.list_settings(user_id)),
                key=lambda row: row["card_bank"],
            ),
        },
        "manual": {
            "accounts": sorted(manual["accounts"], key=lambda row: row["id"]),
            "transactions": sorted(
                manual["transactions"],
                key=lambda row: (row["account_id"], row["id"]),
            ),
        },
        "brokerage": {
            **brokerage,
            "accounts": sorted(brokerage["accounts"], key=lambda row: row["id"]),
            "balances": sorted(
                brokerage["balances"],
                key=lambda row: (row["account_id"], row["currency"]),
            ),
            "positions": sorted(
                brokerage["positions"],
                key=lambda row: (row["account_id"], row["provider_symbol_id"]),
            ),
            "activities": sorted(
                brokerage["activities"],
                key=lambda row: (row["account_id"], row["id"]),
            ),
        },
    }
    for bank in bank_data.KNOWN_BANKS:
        payloads[f"bank:{bank}"] = collect_bank_replica_facts(bank, user_id)
    payloads["market"] = _market_payload(payloads)
    return payloads


def _market_payload(payloads: dict[str, dict[str, Any]]) -> dict[str, Any]:
    currencies = {"TWD"}
    symbols: set[str] = set()
    for name, partition in payloads.items():
        if name.startswith("bank:"):
            currencies.update(
                str(row.get("currency") or "TWD").upper()
                for row in partition["accounts"]
            )
        elif name == "manual":
            currencies.update(
                str(row.get("currency") or "TWD").upper()
                for row in partition["accounts"]
            )
            for row in partition["transactions"]:
                currencies.add(str(row.get("currency") or "TWD").upper())
                if row.get("symbol"):
                    symbols.add(str(row["symbol"]).upper())
        elif name == "brokerage":
            currencies.update(
                str(row.get("currency") or "TWD").upper()
                for key in ("balances", "positions")
                for row in partition[key]
            )
            currencies.update(
                str(row.get("balance_currency") or "TWD").upper()
                for row in partition["accounts"]
            )

    quotes: list[dict[str, Any]] = []
    unavailable_symbols: list[str] = []
    for symbol in sorted(symbols):
        try:
            quote = vars(yahoo_finance.get_quote(symbol))
            quotes.append(quote)
            currencies.add(str(quote["currency"]).upper())
        except yahoo_finance.YahooFinanceUnavailable:
            unavailable_symbols.append(symbol)

    rates = {"TWD": 1.0}
    rate_source = rate_as_of = None
    if currencies != {"TWD"}:
        bundle = fx_service.get_rates()
        if bundle:
            rate_source = bundle.get("source")
            rate_as_of = bundle.get("as_of")
            rates.update({
                currency: bundle["rates"][currency]
                for currency in sorted(currencies - {"TWD"})
                if currency in bundle["rates"]
            })
    return {
        "fx": {"source": rate_source, "as_of": rate_as_of, "rates": rates},
        "quotes": quotes,
        "unavailable_symbols": unavailable_symbols,
    }


def _reconcile(user_id: int) -> list[ReplicaPartition]:
    # ponytail: rebuild + hash every partition on pull; add writer-side dirty
    # generations only if profiling shows this personal-scale scan is material.
    return reconcile_partitions(user_id, lambda: _current_payloads(user_id))


def _response(user_id: int, partitions: list[ReplicaPartition], *, reset_required: bool) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "owner_id": user_id,
        "reset_required": reset_required,
        "generations": {partition.name: partition.generation for partition in partitions},
        "partitions": [
            {"name": partition.name, "generation": partition.generation, "data": partition.data}
            for partition in partitions
        ],
    }


@router.get("/bootstrap")
def replica_bootstrap(user: dict = Depends(current_user)) -> dict[str, Any]:
    partitions = _reconcile(user["id"])
    return _response(user["id"], partitions, reset_required=False)


@router.post("/pull")
def replica_pull(
    body: ReplicaPullRequest,
    user: dict = Depends(current_user),
) -> dict[str, Any]:
    if body.schema_version != SCHEMA_VERSION:
        return _response(user["id"], [], reset_required=True)
    current = _reconcile(user["id"])
    changed = [
        partition
        for partition in current
        if body.generations.get(partition.name) != partition.generation
    ]
    response = _response(user["id"], changed, reset_required=False)
    response["generations"] = {partition.name: partition.generation for partition in current}
    return response
