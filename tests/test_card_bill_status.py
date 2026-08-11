"""Regression tests for credit-card bill_status derivation."""
from __future__ import annotations

from datetime import date

from backend.server.db_facade.cards import CardSummary
from backend.server.routers import cards as cards_router


class _FakeDate(date):
    @classmethod
    def today(cls) -> "_FakeDate":
        return cls(2026, 7, 3)


def test_positive_remaining_due_is_overdue_even_with_recent_partial_payment(monkeypatch):
    monkeypatch.setattr(cards_router, "date", _FakeDate)

    status = cards_router._compute_bill_status(CardSummary(
        bank="fubon",
        card_no="****7024",
        bill_due_amount=7473.0,
        payment_due_date="2026/07/02",
        last_payment_amount=3000.0,
        last_payment_date="2026-07-03",
    ))

    assert status == "overdue"


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


def test_positive_remaining_due_stays_overdue_after_current_statement_payment(monkeypatch):
    monkeypatch.setattr(cards_router, "date", _AfterHsbcDueDate)

    status = cards_router._compute_bill_status(CardSummary(
        bank="hsbc",
        card_no="9059-****-****-7059",
        bill_due_amount=1000.0,
        statement_close_date="2026-06-18",
        payment_due_date="2026-07-06",
        last_payment_amount=33365.0,
        last_payment_date="2026-07-07",
    ))

    assert status == "overdue"


def test_zero_remaining_due_with_current_cycle_payment_is_paid(monkeypatch):
    monkeypatch.setattr(cards_router, "date", _AfterHsbcDueDate)

    status = cards_router._compute_bill_status(CardSummary(
        bank="hsbc",
        card_no="9059-****-****-7059",
        bill_due_amount=0.0,
        statement_close_date="2026-06-18",
        payment_due_date="2026-07-06",
        last_payment_amount=34365.0,
        last_payment_date="2026-07-07",
    ))

    assert status == "paid"


def test_zero_remaining_due_without_current_payment_needs_no_payment(monkeypatch):
    monkeypatch.setattr(cards_router, "date", _AfterHsbcDueDate)

    status = cards_router._compute_bill_status(CardSummary(
        bank="hsbc",
        card_no="9059-****-****-7059",
        bill_due_amount=0.0,
        statement_close_date="2026-06-18",
        payment_due_date="2026-07-06",
    ))

    assert status == "no_payment_required"


def test_missing_canonical_remaining_due_is_unknown(monkeypatch):
    monkeypatch.setattr(cards_router, "date", _AfterHsbcDueDate)

    status = cards_router._compute_bill_status(CardSummary(
        bank="scb",
        card_no="****7001",
        bill_due_amount=None,
        payment_due_date="2026-07-06",
    ))

    assert status == "unknown"


def test_previous_month_same_day_clamps_month_end_and_rolls_year():
    assert cards_router._previous_month_same_day(date(2026, 3, 31)) == date(2026, 2, 28)
    assert cards_router._previous_month_same_day(date(2024, 3, 31)) == date(2024, 2, 29)
    assert cards_router._previous_month_same_day(date(2026, 1, 31)) == date(2025, 12, 31)
