"""Derive card payments from the user's configured auto-debit account.

Only cards with a `card_auto_debit_settings` row are touched. The debit account's
own TWD history is the evidence: a bank-specific description pattern picks that
card bank's debits out of an account that may pay several banks. Holidays can push
a debit past the due date, so a statement is matched by "debited after the close
date" with no upper bound.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from backend.core.card_bills import apply_card_bill_facts, make_card_bill_fact
from backend.core.store import BankStore
from backend.server import auto_debit_settings_repo

# card bank -> description of that bank's card-fee debit, observed in prod history.
DEBIT_PATTERNS = {
    "cathay": re.compile(r"^信用卡款.*國泰"),
    "ctbc": re.compile(r"中信卡"),
    "dbs": re.compile(r"ACH代收.*星展"),
    "esun": re.compile(r"玉山卡款扣繳"),
    "fubon": re.compile(r"富邦信用卡款"),
    "sinopac": re.compile(r"永豐卡費"),
    "taishin": re.compile(r"台新卡費"),
    "ubot": re.compile(r"^信用卡款$"),
}
LOOKBACK_DAYS = 120


def card_debits(store: BankStore, account_no: str, card_bank: str,
                today: date | None = None) -> list[tuple[str, int]]:
    """(date, amount) debits of `card_bank`'s card fee from one account, oldest first."""
    pattern = DEBIT_PATTERNS.get(card_bank)
    if pattern is None:
        return []
    since = ((today or date.today()) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    return [(day, amount) for day, desc, amount in store.twd_debits_since(account_no, since)
            if pattern.search(desc.strip())]


def statement_fact(cycle: dict[str, Any], debits: list[tuple[str, int]]):
    """Remaining due = statement total minus debits after the close date, floored at 0."""
    paid = sum(amount for day, amount in debits if day > cycle["statement_close_date"])
    latest = debits[-1] if debits else None
    return make_card_bill_fact(
        remaining_due=max(cycle["statement_amount"] - paid, 0),
        statement_close_date=cycle["statement_close_date"],
        payment_due_date=cycle["payment_due_date"],
        last_payment_amount=latest[1] if latest else None,
        last_payment_date=latest[0] if latest else None,
    )


def apply_auto_debit_payments(bank: str, user_id: int, store: BankStore, data: dict) -> int:
    """Run after `bank` persisted; covers it as the card bank or as the debit account."""
    applied = 0
    for setting in auto_debit_settings_repo.list_settings(user_id):
        if bank not in (setting.card_bank, setting.account_bank):
            continue
        debit_store = store if setting.account_bank == bank else BankStore(setting.account_bank, user_id=user_id)
        card_store = store if setting.card_bank == bank else BankStore(setting.card_bank, user_id=user_id)
        try:
            debits = card_debits(debit_store, setting.account_no, setting.card_bank)
            cycle = data.get("card_statement_cycle") if setting.card_bank == bank else None
            # Bank-native remaining due wins; derive it only when the bank published none.
            # ponytail: statement total is not persisted, so a later debit-account-only
            # sync advances the payment pair but not remaining due until the next card sync.
            if cycle and data.get("card_bill_facts_ok") is not True:
                fact = statement_fact(cycle, debits)
                if fact is not None:
                    applied += apply_card_bill_facts(card_store, facts_ok=True, facts=[fact])
                    continue
            if debits:
                applied += card_store.record_auto_debit_payment(*debits[-1])
        finally:
            for other in {id(s): s for s in (debit_store, card_store) if s is not store}.values():
                other.close()
    return applied
