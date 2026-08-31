"""Canonical credit-card remaining-due facts shared by all bank adapters."""
from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from backend.core.base import NormalizedCardBillFact, validate_card_bill_facts
from backend.core.store import BankStore


MAX_CARD_BILL_MONEY = Decimal("100000000")
_CANONICAL_BILL_COLUMNS = {
    "bill_due_amount", "statement_close_date", "payment_due_date",
    "last_payment_amount", "last_payment_date",
}


class CardBillWriteBarrier:
    """Delegate BankStore except bank-specific persist cannot write bill semantics."""

    def __init__(self, store: BankStore) -> None:
        self._store = store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def upsert_cards(
        self, rows: list[dict[str, Any]], commit: bool = True,
    ) -> Any:
        sanitized = [
            {key: value for key, value in row.items() if key not in _CANONICAL_BILL_COLUMNS}
            for row in rows
        ]
        return self._store.upsert_cards(sanitized, commit=commit)


def _money(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    if not amount.is_finite() or amount < 0 or amount > MAX_CARD_BILL_MONEY:
        return None
    result = float(amount)
    return result if math.isfinite(result) else None


def card_bill_money(value: Any) -> float | None:
    """Validate a bank-native money scalar before bill arithmetic."""
    return _money(value)


def _iso_date(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    match = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    compact = re.match(r"^(\d{4})(\d{2})(\d{2})$", text)
    if match:
        year, month, day = map(int, match.groups())
    elif compact:
        year, month, day = map(int, compact.groups())
    elif "T" in text:
        from datetime import datetime

        try:
            normalized = text.replace("/", "-").replace("Z", "+00:00")
            return datetime.fromisoformat(normalized).date().isoformat()
        except ValueError:
            return None
    else:
        from datetime import datetime

        for fmt in ("%d %b %Y", "%d %B %Y", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                continue
        return None
    try:
        from datetime import date

        return date(year, month, day).isoformat()
    except ValueError:
        return None


def card_bill_date(value: Any) -> str | None:
    """Normalize a bank-native bill date before cycle comparisons."""
    return _iso_date(value)


def summarize_persisted_card_bills(
    bank: str,
    cards: list[dict[str, Any]],
) -> tuple[str, int] | None:
    """Return one bank liability from complete canonical persisted card facts."""
    if not cards:
        return None
    amounts: list[Decimal] = []
    dates: list[str] = []
    for card in cards:
        amount = _money(card.get("bill_due_amount"))
        updated_on = _iso_date(card.get("updated_at"))
        if amount is None or updated_on is None:
            return None
        amounts.append(Decimal(str(amount)))
        dates.append(updated_on)
    if bank == "hsbc":
        total = sum(amounts, Decimal(0))
    elif any(amount != amounts[0] for amount in amounts[1:]):
        return None
    else:
        total = amounts[0]
    return min(dates), int(total)


def make_card_bill_fact(
    *,
    remaining_due: Any,
    scope: str = "bank",
    card_no: str | None = None,
    status: str | None = None,
    statement_close_date: Any = None,
    payment_due_date: Any = None,
    last_payment_amount: Any = None,
    last_payment_date: Any = None,
) -> NormalizedCardBillFact | None:
    """Build one validated canonical fact; malformed raw input is unavailable."""
    remaining = _money(remaining_due)
    if remaining is None:
        return None
    statement_date = _iso_date(statement_close_date)
    due_date = _iso_date(payment_due_date)
    if statement_close_date not in (None, "") and statement_date is None:
        return None
    if payment_due_date not in (None, "") and due_date is None:
        return None
    payment_amount = _money(last_payment_amount)
    payment_date = _iso_date(last_payment_date)
    payment_supplied = last_payment_amount not in (None, "") or last_payment_date not in (None, "")
    if payment_supplied and (payment_amount is None or payment_date is None):
        return None
    if not payment_supplied:
        payment_amount = None
        payment_date = None
    canonical_status = status or (
        "unpaid" if remaining > 0 else ("paid" if payment_amount and payment_amount > 0 else "no_payment_required")
    )
    fact: NormalizedCardBillFact = {
        "scope": scope,
        "status": canonical_status,
        "remaining_due": remaining,
    }
    if scope == "card" and card_no:
        fact["card_no"] = card_no
    for key, value in (
        ("statement_close_date", statement_date),
        ("payment_due_date", due_date),
        ("last_payment_amount", payment_amount),
        ("last_payment_date", payment_date),
    ):
        if value is not None:
            fact[key] = value  # type: ignore[literal-required]
    try:
        validate_card_bill_facts([fact], facts_ok=True)
    except ValueError:
        return None
    return fact


def publish_card_bill_facts(
    out: dict[str, Any], facts: list[NormalizedCardBillFact | None]
) -> None:
    complete = bool(facts) and all(fact is not None for fact in facts)
    out["card_bill_facts_ok"] = complete
    out["card_bill_facts"] = [fact for fact in facts if fact is not None] if complete else []


def apply_card_bill_facts(
    store: BankStore,
    *,
    facts_ok: bool | None,
    facts: list[NormalizedCardBillFact],
    commit: bool = True,
) -> int:
    """Apply authoritative bill facts to known card inventory, fail-closed."""
    validate_card_bill_facts(facts, facts_ok=facts_ok)
    if facts_ok is not True or not facts:
        return 0

    known = {row["card_no"]: row for row in store.list_cards()}
    fact = facts[0]
    targets: list[tuple[dict[str, Any], NormalizedCardBillFact]] = []
    if fact["scope"] == "bank":
        targets = [(row, fact) for row in known.values()]
    else:
        targets = [
            (known[card_no], card_fact)
            for card_fact in facts
            if (card_no := card_fact["card_no"]) in known
        ]

    rows = []
    for card, card_fact in targets:
        row = {
            "number": card["card_no"],
            "name": card["name"],
            "association": card["association"],
            "type": card["type"],
            "is_cube": bool(card["is_cube"]),
            "active": bool(card["active"]),
            "bill_due_amount": card_fact["remaining_due"],
        }
        for key in ("statement_close_date", "payment_due_date"):
            if key in card_fact:
                row[key] = card_fact.get(key)
        if "last_payment_date" in card_fact:
            row["last_payment_amount"] = card_fact.get("last_payment_amount")
            row["last_payment_date"] = card_fact["last_payment_date"]
        rows.append(row)
    return store.update_card_bill_facts(rows, commit=commit)
