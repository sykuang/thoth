"""Canonical, bank-neutral facts for frontend replica partitions."""
from __future__ import annotations

import json
from typing import Any

from backend.core import account_classify
from backend.core.card_bills import summarize_persisted_card_bills
from backend.core.money import native_money
from backend.core.store import canonical_display_description
from backend.server import fx_service
from backend.server.bank_account_projection import (
    latest_twd_asset_balance,
    metric_loan_balance_twd,
)
from backend.server.db_facade import db_api


def _value(row: Any, key: str, default: Any = None) -> Any:
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        value = getattr(row, key, default)
    return default if value is None else value


def _date(value: Any) -> str | None:
    if not value:
        return None
    head = str(value).strip()[:10].replace("/", "-")
    return head or None


def _json_list(value: Any) -> list[Any] | None:
    if value in (None, ""):
        return []
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) else None


MAX_SAFE_INTEGER = 9_007_199_254_740_991


def _number(value: Any, currency: str) -> int | float:
    try:
        number = native_money(0 if value is None else value, currency)
    except ValueError:
        raise ValueError("invalid persisted monetary value") from None
    assert number is not None
    return number


def _optional_number(value: Any, currency: str) -> int | float | None:
    return None if value is None else _number(value, currency)


def _cashflow(amount: int | float, txn_type: str | None) -> tuple[str, int | float]:
    if txn_type in {"cashback", "refund", "fee_waiver"}:
        return "income", abs(amount)
    if txn_type == "payment" or amount == 0:
        return "neutral", 0
    return ("income", amount) if amount > 0 else ("expense", abs(amount))


def _transaction_fact(
    bank: str,
    row: Any,
    excluded_accounts: set[tuple[str, str]],
    excluded_cards: set[str],
) -> dict[str, Any]:
    kind = str(row.kind)
    txn_type = _value(row, "txn_type") if kind != "twd" else None
    currency = str(_value(row, "currency", "TWD")).strip().upper()
    if kind == "twd":
        amount = _number(
            _number(_value(row, "income", 0), currency)
            - _number(_value(row, "expend", 0), currency),
            currency,
        )
        date = _date(_value(row, "txn_datetime")) or _date(_value(row, "account_date"))
    else:
        source_amount = _number(_value(row, "amount", 0), currency)
        amount = -source_amount if source_amount > 0 else source_amount
        date = _date(_value(row, "consume_date"))
    direction, cashflow_amount = _cashflow(amount, txn_type)
    account_no = _value(row, "account_no")
    card_no = _value(row, "card_no")
    consume_currency = (
        str(_value(row, "consume_currency")).strip().upper()
        if _value(row, "consume_currency") else None
    )
    return {
        "id": _value(row, "id"),
        "bank": bank,
        "kind": kind,
        "date": date,
        "datetime": _value(row, "txn_datetime") if kind == "twd" else None,
        "account_date": _date(_value(row, "account_date")),
        "consume_date": _date(_value(row, "consume_date")),
        "post_date": _date(_value(row, "post_date")),
        "bill_date": _date(_value(row, "bill_date")),
        "description": _value(row, "description"),
        "description_overwrite": _value(row, "description_overwrite"),
        "amount": amount,
        "cashflow_direction": direction,
        "cashflow_amount": cashflow_amount,
        "display_amount": abs(amount),
        "currency": currency,
        "consume_currency": consume_currency,
        "consume_amount": (
            _optional_number(_value(row, "consume_amount", None), consume_currency)
            if consume_currency else None
        ),
        "category": _value(row, "category"),
        "subcategory": _value(row, "subcategory"),
        "legacy_category": _value(row, "legacy_category"),
        "txn_type": txn_type,
        "flow_type": _value(row, "flow_type"),
        "is_subscription": bool(_value(row, "is_subscription", 0)),
        "income_category": _value(row, "income_category"),
        "account_no": account_no,
        "account_key": (
            f"{bank}:account:{account_no}:{currency}"
            if kind == "twd" and account_no else None
        ),
        "card_no": card_no,
        "balance": _optional_number(_value(row, "balance", None), currency),
        "counterparty_bank": _value(row, "counterparty_bank"),
        "counterparty_acct": _value(row, "counterparty_acct"),
        "memo": _value(row, "memo"),
        "display_description": canonical_display_description(
            _value(row, "description"), _value(row, "counterparty_acct"),
        ),
        "scope": _value(row, "scope"),
        "excluded": (
            (account_no, currency) in excluded_accounts if kind == "twd"
            else card_no in excluded_cards
        ),
        "auto_excluded": bool(_value(row, "auto_excluded", 0)),
        "tags": _json_list(_value(row, "tags_overwrite")),
        # Keep the parent and authoritative split facts. Local projection validates
        # the sum before expanding, so malformed legacy rows fall back to parent.
        "splits": _json_list(_value(row, "splits_overwrite")),
        "first_seen": _value(row, "first_seen"),
        "refreshed_at": _value(row, "refreshed_at"),
    }


def _loan_fact(bank: str, user_id: int) -> dict[str, Any] | None:
    direct = db_api.get_latest_loan_balance(bank=bank, user_id=user_id)
    if direct is not None:
        amount = account_classify.normalize_liability_magnitude(direct.loan_balance)
        return {
            "snapshot_date": direct.snapshot_date,
            "amount_twd": int(amount) if amount is not None else None,
            "source": "balance_history",
        }

    loans = db_api.list_loan_accounts(bank=bank, user_id=user_id)
    if not loans:
        return None
    if all(row.raw_balance is not None for row in loans):
        total = 0
        dates: list[str] = []
        for row in loans:
            currency = (row.currency or "TWD").strip().upper()
            try:
                magnitude = native_money(row.raw_balance, currency, absolute=True)
            except ValueError:
                return None
            if magnitude is None:
                return None
            converted = (
                int(magnitude)
                if currency == "TWD"
                else fx_service.convert_to_twd(magnitude, currency)
            )
            if converted is None:
                break
            checked_total = native_money(total + converted, "TWD")
            assert isinstance(checked_total, int)
            total = checked_total
            if row.raw_balance_date:
                dates.append(row.raw_balance_date)
        else:
            return {
                "snapshot_date": min(dates) if len(dates) == len(loans) else None,
                "amount_twd": total,
                "source": "accounts",
            }

    metric = db_api.get_latest_metric(bank=bank, category="balance_latest", user_id=user_id)
    if metric is None or not isinstance(metric.payload, dict):
        return None
    amount = metric_loan_balance_twd(
        metric.payload,
        (row.currency for row in loans),
    )
    return (
        {
            "snapshot_date": metric.snapshot_date,
            "amount_twd": amount,
            "source": "normalized_balance_metric",
        }
        if amount is not None else None
    )


def collect_bank_replica_facts(bank: str, user_id: int) -> dict[str, Any]:
    """Return typed canonical facts; never expose bank-private metric payloads."""
    accounts = sorted(
        (row.model_dump() for row in db_api.list_accounts(bank=bank, user_id=user_id)),
        key=lambda row: (row["account_no"], row.get("currency") or "TWD"),
    )
    all_cards = sorted(
        (
            row.model_dump()
            for row in db_api.list_cards(bank=bank, user_id=user_id, include_inactive=True)
        ),
        key=lambda row: row["card_no"],
    )
    cards = [row for row in all_cards if row["active"]]
    excluded_accounts = {
        (row["account_no"], row.get("currency") or "TWD")
        for row in accounts if row["excluded"]
    }
    excluded_cards = db_api.list_excluded_card_nos_all_banks(
        user_id=user_id,
        banks=[bank],
    ).get(bank, set())
    transactions = sorted(
        (
            _transaction_fact(bank, row, excluded_accounts, excluded_cards)
            for row in db_api.list_txns_for_bank(
                bank=bank,
                user_id=user_id,
                kinds=["twd", "billed", "pending"],
            )
        ),
        key=lambda row: (row["kind"], str(row["id"])),
    )
    txn_balances = sorted(
        (
            row.model_dump()
            for row in db_api.list_latest_account_txn_balances(
                bank=bank,
                user_id=user_id,
            ).values()
        ),
        key=lambda row: (row["account_no"], row["currency"]),
    )
    balance = latest_twd_asset_balance(bank, user_id)
    card_summary = summarize_persisted_card_bills(bank, all_cards)
    return {
        "accounts": accounts,
        "cards": cards,
        "transactions": transactions,
        "loan_repayments": [fact.model_dump() for fact in db_api.list_loan_repayments(bank=bank, user_id=user_id)],
        "portfolio_facts": {
            "latest_twd_balance": balance.model_dump() if balance else None,
            "latest_account_transaction_balances": txn_balances,
            "loan_balance": _loan_fact(bank, user_id),
            "card_unpaid": (
                {
                    "snapshot_date": card_summary[0],
                    "amount_twd": card_summary[1],
                    "recognized": True,
                }
                if card_summary else None
            ),
        },
    }
