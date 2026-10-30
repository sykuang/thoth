"""Regression tests for credit-card bill_status derivation."""
from __future__ import annotations

from datetime import date

import pytest

from backend.server.db_facade.cards import CardSummary
from backend.server.routers import cards as cards_router


class _FakeDate(date):
    @classmethod
    def today(cls) -> "_FakeDate":
        return cls(2026, 7, 3)


@pytest.mark.parametrize("bank", ["dbs", "fubon", "hsbc"])
@pytest.mark.parametrize(("due_date", "expected"), [
    ("2026-07-04", "due"),
    ("2026-07-03", "due"),
    ("2026-07-02", "overdue"),
    (None, "unknown"),
])
def test_payment_equal_to_positive_remaining_due_does_not_settle_bill(
    monkeypatch, bank, due_date, expected,
):
    monkeypatch.setattr(cards_router, "date", _FakeDate)
    # Original bill 800, posted payment 400: canonical remaining due is still 400.
    card = CardSummary(
        bank=bank,
        card_no="****0001",
        bill_due_amount=400.0,
        statement_close_date="2026-06-18",
        payment_due_date=due_date,
        last_payment_amount=400.0,
        last_payment_date="2026-07-01",
    )

    response = cards_router._card_to_response(card)

    assert response["bill_status"] == expected
    assert response["bill_due_amount"] == 400.0
    assert response["last_payment_amount"] == 400.0
    assert response["last_payment_date"] == "2026-07-01"


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


class _AfterFubonOctDue(date):
    @classmethod
    def today(cls) -> "_AfterFubonOctDue":
        return cls(2026, 10, 5)


def test_equal_current_cycle_payment_with_positive_remaining_due_is_overdue(monkeypatch):
    monkeypatch.setattr(cards_router, "date", _AfterFubonOctDue)
    card = dict(bank="fubon", card_no="****2099", bill_due_amount=25724.0,
                statement_close_date="2026-09-16", payment_due_date="2026-10-02",
                last_payment_amount=25724.0, last_payment_date="2026-10-05")

    # Canonical remaining due is not the original bill; equality cannot prove settlement.
    assert cards_router._compute_bill_status(CardSummary(**card)) == "overdue"
    # partial or previous-cycle payment still overdue
    assert cards_router._compute_bill_status(
        CardSummary(**{**card, "last_payment_amount": 25000.0})) == "overdue"
    assert cards_router._compute_bill_status(
        CardSummary(**{**card, "last_payment_date": "2026-09-10"})) == "overdue"
