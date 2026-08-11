"""Canonical, bank-neutral facts for frontend replica partitions."""
from __future__ import annotations

import json
from math import isfinite
from typing import Any

from backend.core import account_classify
from backend.core.card_status import CathayBillStatus, cathay_bill_status
from backend.core.store import canonical_display_description
from backend.server import fx_service
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


def _cashflow(amount: int, txn_type: str | None) -> tuple[str, int]:
    if txn_type in {"cashback", "refund", "fee_waiver"}:
        return "income", abs(amount)
    if txn_type == "payment" or amount == 0:
        return "neutral", 0
    return ("income", amount) if amount > 0 else ("expense", abs(amount))


def _transaction_fact(
    bank: str,
    row: Any,
    excluded_accounts: set[str],
    excluded_cards: set[str],
) -> dict[str, Any]:
    kind = str(row.kind)
    txn_type = _value(row, "txn_type") if kind != "twd" else None
    if kind == "twd":
        amount = int(_value(row, "income", 0)) - int(_value(row, "expend", 0))
        date = _date(_value(row, "txn_datetime")) or _date(_value(row, "account_date"))
    else:
        source_amount = int(_value(row, "amount", 0))
        amount = -source_amount if source_amount > 0 else source_amount
        date = _date(_value(row, "consume_date"))
    direction, cashflow_amount = _cashflow(amount, txn_type)
    account_no = _value(row, "account_no")
    card_no = _value(row, "card_no")
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
        "currency": str(_value(row, "currency", "TWD")).upper(),
        "consume_currency": (
            str(_value(row, "consume_currency")).upper()
            if _value(row, "consume_currency") else None
        ),
        "consume_amount": _value(row, "consume_amount"),
        "category": _value(row, "category"),
        "subcategory": _value(row, "subcategory"),
        "legacy_category": _value(row, "legacy_category"),
        "txn_type": txn_type,
        "flow_type": _value(row, "flow_type"),
        "is_subscription": bool(_value(row, "is_subscription", 0)),
        "income_category": _value(row, "income_category"),
        "account_no": account_no,
        "card_no": card_no,
        "balance": _value(row, "balance"),
        "counterparty_bank": _value(row, "counterparty_bank"),
        "counterparty_acct": _value(row, "counterparty_acct"),
        "memo": _value(row, "memo"),
        "display_description": canonical_display_description(
            _value(row, "description"), _value(row, "counterparty_acct"),
        ),
        "scope": _value(row, "scope"),
        "excluded": (
            account_no in excluded_accounts if kind == "twd"
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


def _to_int(value: Any) -> int | None:
    if value in (None, "", "-") or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "").strip())
        return int(number) if isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _card_unpaid_twd(bank: str, payload: Any) -> int | None:
    if bank == "cathay" and isinstance(payload, dict):
        bill = (payload.get("latest_bill") or {}).get("twd") or {}
        amount = _to_int(bill.get("billAmount"))
        status = cathay_bill_status(bill.get("payBillStatus"))
        if amount is None or status is None:
            return None
        return 0 if status is CathayBillStatus.PAID else amount
    if bank == "ubot" and isinstance(payload, dict):
        return _to_int((payload.get("TotalData") or {}).get("Card"))
    if bank == "hsbc" and isinstance(payload, list):
        values = [
            amount
            for card in payload
            if isinstance(card, dict)
            if (amount := _to_int(card.get("outstanding"))) is not None
        ]
        return sum(values) if values else None
    if bank == "sinopac" and isinstance(payload, list) and payload:
        first = payload[0] if isinstance(payload[0], dict) else {}
        groups = first.get("SubInfo") or []
        entries = groups[0] if groups and isinstance(groups[0], list) else []
        for entry in entries:
            if isinstance(entry, dict) and entry.get("DataText") == "本期應繳":
                return _to_int(entry.get("DataValue"))
    return None


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
            magnitude = account_classify.normalize_liability_magnitude(row.raw_balance)
            if magnitude is None:
                break
            currency = (row.currency or "TWD").upper()
            converted = (
                round(magnitude)
                if currency == "TWD"
                else fx_service.convert_to_twd(magnitude, currency)
            )
            if converted is None:
                break
            total += converted
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
    amount = account_classify.normalize_liability_magnitude(
        _to_int(metric.payload.get("loan")),
    )
    return (
        {
            "snapshot_date": metric.snapshot_date,
            "amount_twd": int(amount),
            "source": "normalized_balance_metric",
        }
        if amount not in (None, 0) else None
    )


def collect_bank_replica_facts(bank: str, user_id: int) -> dict[str, Any]:
    """Return typed canonical facts; never expose bank-private metric payloads."""
    accounts = sorted(
        (row.model_dump() for row in db_api.list_accounts(bank=bank, user_id=user_id)),
        key=lambda row: row["account_no"],
    )
    cards = sorted(
        (
            row.model_dump()
            for row in db_api.list_cards(bank=bank, user_id=user_id, include_inactive=False)
        ),
        key=lambda row: row["card_no"],
    )
    excluded_accounts = {row["account_no"] for row in accounts if row["excluded"]}
    excluded_cards = {row["card_no"] for row in cards if row["excluded"]}
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
        key=lambda row: row["account_no"],
    )
    balance = db_api.get_latest_twd_balance(bank=bank, user_id=user_id)
    card_metric = db_api.get_latest_metric(
        bank=bank,
        category="card_summary",
        user_id=user_id,
    )
    return {
        "accounts": accounts,
        "cards": cards,
        "transactions": transactions,
        "portfolio_facts": {
            "latest_twd_balance": balance.model_dump() if balance else None,
            "latest_account_transaction_balances": txn_balances,
            "loan_balance": _loan_fact(bank, user_id),
            "card_unpaid": (
                {
                    "snapshot_date": card_metric.snapshot_date,
                    "amount_twd": _card_unpaid_twd(bank, card_metric.payload),
                }
                if card_metric else None
            ),
        },
    }
