"""Regression tests for credit-card bill_status derivation."""
from __future__ import annotations

from datetime import date

from backend.server.db_facade.cards import CardSummary
from backend.server.routers import cards as cards_router


class _FakeDate(date):
    @classmethod
    def today(cls) -> "_FakeDate":
        return cls(2026, 7, 3)


def test_bill_status_treats_slash_due_date_with_recent_payment_as_paid(monkeypatch):
    """Fubon stores payment_due_date as YYYY/MM/DD; it must still compare dates.

    Regression: Costco card had due=2026/07/02 and latest payment=2026-07-03.
    The old parser only accepted ISO hyphen dates, returned unknown, and frontend then
    recomputed the slash due date as "逾期 1 天" despite the payment record.
    """
    monkeypatch.setattr(cards_router, "date", _FakeDate)

    status = cards_router._compute_bill_status(CardSummary(
        bank="fubon",
        card_no="****7024",
        bill_due_amount=7473.0,
        payment_due_date="2026/07/02",
        last_payment_amount=7473.0,
        last_payment_date="2026-07-03",
    ))

    assert status == "paid"


def test_bill_status_treats_slash_due_date_without_recent_payment_as_overdue(monkeypatch):
    monkeypatch.setattr(cards_router, "date", _FakeDate)

    status = cards_router._compute_bill_status(CardSummary(
        bank="fubon",
        card_no="****7024",
        bill_due_amount=7473.0,
        payment_due_date="2026/07/02",
        last_payment_amount=0.0,
        last_payment_date="2026-05-05",
    ))

    assert status == "overdue"


class _AfterHsbcDueDate(date):
    @classmethod
    def today(cls) -> "_AfterHsbcDueDate":
        return cls(2026, 7, 12)


def test_bill_status_does_not_treat_previous_statement_payment_as_current_paid(monkeypatch):
    """A payment before the current statement close cannot settle that statement.

    HSBC 8926 regression: current statement closed 2026-06-18 and was due
    2026-07-06, but the latest transaction-table payment was the previous cycle's
    2026-06-08 auto debit.  The old due-30-days heuristic called it paid.
    """
    monkeypatch.setattr(cards_router, "date", _AfterHsbcDueDate)

    status = cards_router._compute_bill_status(CardSummary(
        bank="hsbc",
        card_no="9059-****-****-7059",
        bill_due_amount=34365.0,
        statement_close_date="2026-06-18",
        payment_due_date="2026-07-06",
        last_payment_amount=6145.0,
        last_payment_date="2026-06-08",
    ))

    assert status == "overdue"


def test_bill_status_accepts_payment_after_current_statement_close(monkeypatch):
    monkeypatch.setattr(cards_router, "date", _AfterHsbcDueDate)

    status = cards_router._compute_bill_status(CardSummary(
        bank="hsbc",
        card_no="9059-****-****-7059",
        bill_due_amount=34365.0,
        statement_close_date="2026-06-18",
        payment_due_date="2026-07-06",
        last_payment_amount=34365.0,
        last_payment_date="2026-07-07",
    ))

    assert status == "paid"


def test_previous_month_same_day_clamps_month_end_and_rolls_year():
    assert cards_router._previous_month_same_day(date(2026, 3, 31)) == date(2026, 2, 28)
    assert cards_router._previous_month_same_day(date(2024, 3, 31)) == date(2024, 2, 29)
    assert cards_router._previous_month_same_day(date(2026, 1, 31)) == date(2025, 12, 31)
