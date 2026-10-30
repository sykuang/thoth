from __future__ import annotations

from backend.banks.dbs import DbsCrawler, _dbs_card_bill_fact

import pytest


def test_dbs_parse_card_fee_page_extracts_current_bill_due():
    text = """
    轉帳/換匯
    信用卡 (1)
    最近一期帳單金額
    繳款截止日 06月22日
    TWD1,234
    轉帳
    """

    parsed = DbsCrawler._parse_card_fee_page(text)

    assert parsed["bill_due_amount"] == 1234.0
    assert parsed["payment_due_date"].endswith("-06-22")
    assert parsed["currency"] == "TWD"


def test_dbs_same_cycle_payment_reconciles_to_zero():
    out = {
        "dbs_card_fee_page": {
            "bill_due_amount": 5000,
            "payment_due_date": "2026-08-20",
        },
        "api_responses": {"liabilities": [{"resp": {"creditCard": {
            "paymentDetails": {
                "amount": 5000, "alreadyPaid": 5000, "dueDate": "2026-08-20",
            },
        }}}]},
    }

    fact = _dbs_card_bill_fact(out)

    assert fact is not None
    assert fact["remaining_due"] == 0.0


def test_dbs_complete_dashboard_survives_partial_fee_cycle():
    out = {
        "dbs_card_fee_page": {"bill_due_amount": 5000},
        "api_responses": {"liabilities": [{"resp": {"creditCard": {
            "paymentDetails": {
                "amount": 5000, "alreadyPaid": -5000, "dueDate": "2026-08-20",
            },
        }}}]},
    }

    fact = _dbs_card_bill_fact(out)

    assert fact is not None
    assert fact["remaining_due"] == 0
    assert fact["payment_due_date"] == "2026-08-20"
    assert "last_payment_amount" not in fact
    assert "last_payment_date" not in fact


@pytest.mark.parametrize("saved_payment", [False, True])
def test_dbs_partial_fee_refreshes_canonical_due_through_real_persistence(tmp_path, monkeypatch, saved_payment):
    from backend.core import bank_pg
    from backend.core.card_bills import capture_card_bill_snapshot, publish_card_bill_facts
    from backend.core.persist import persist_collected
    from backend.core.store import BankStore

    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(bank_pg, "enabled", lambda: False)
    store = BankStore("dbs", user_id=1)
    payment = {"last_payment_amount": 1200, "last_payment_date": "2026-08-10"} if saved_payment else {}
    try:
        store.upsert_cards([
            {"number": "****7001", "name": "current", "bill_due_amount": 1200,
             "payment_due_date": "2026-08-20", **payment},
            {"number": "****7002", "name": "known sibling", "bill_due_amount": 1200,
             "payment_due_date": "2026-08-20", "active": False, **payment},
        ])
        snapshot = capture_card_bill_snapshot(store)
        out = {
            "dbs_card_fee_page": {"bill_due_amount": 1200},
            "api_responses": {"liabilities": [{"resp": {"creditCard": {
                "paymentDetails": {
                    "amount": 1200, "alreadyPaid": -1200, "dueDate": "2026-08-20",
                },
                "cards": [{"cardNumber": "************7001", "cardDescription": "current",
                           "isDisplayImg": True, "isPrimaryCard": True}],
            }}}]},
        }
        publish_card_bill_facts(out, [_dbs_card_bill_fact(out)])

        delta = persist_collected("dbs", out, store, bill_snapshot=snapshot)

        rows = store.conn.execute(
            "SELECT card_no, bill_due_amount, payment_due_date, last_payment_amount, "
            "last_payment_date FROM cards WHERE user_id = 1 ORDER BY card_no"
        ).fetchall()
        assert delta["card_bill_facts_applied"] == 2
        assert [tuple(row) for row in rows] == [
            ("****7001", 0, "2026-08-20", payment.get("last_payment_amount"), payment.get("last_payment_date")),
            ("****7002", 0, "2026-08-20", payment.get("last_payment_amount"), payment.get("last_payment_date")),
        ]
        from backend.server.db_facade import db_api

        cards = db_api.list_cards(bank="dbs", user_id=1, include_inactive=True)
        assert len(cards) == 2
        assert all(card.bill_due_amount == 0 for card in cards)
        detail = db_api.get_card(bank="dbs", card_no="****7001", user_id=1)
        assert detail is not None
        assert detail.bill_due_amount == 0
    finally:
        store.close()


@pytest.mark.parametrize("paid,remaining", [(0, 1200), (-600, 600), (-1200, 0), (-1300, 0)])
def test_dbs_partial_fee_preserves_real_remainder(paid, remaining):
    out = {
        "dbs_card_fee_page": {"bill_due_amount": 1200},
        "api_responses": {"liabilities": [{"resp": {"creditCard": {
            "paymentDetails": {
                "amount": 1200, "alreadyPaid": paid, "dueDate": "2026-08-20",
            },
        }}}]},
    }
    fact = _dbs_card_bill_fact(out)
    assert fact is not None
    assert fact["remaining_due"] == remaining


@pytest.mark.parametrize("field,value", [
    ("amount", None), ("amount", True), ("amount", "NaN"), ("amount", 1201),
    ("alreadyPaid", None), ("alreadyPaid", True), ("alreadyPaid", "Infinity"),
    ("dueDate", None), ("dueDate", ""), ("dueDate", "malformed"),
    ("dueDate", "2026-02-31"), ("dueDate", "2026-08-20garbage"),
])
def test_dbs_partial_fee_rejects_incomplete_or_conflicting_dashboard(field, value):
    payment = {"amount": 1200, "alreadyPaid": -1200, "dueDate": "2026-08-20"}
    payment[field] = value
    out = {
        "dbs_card_fee_page": {"bill_due_amount": 1200},
        "api_responses": {"liabilities": [{"resp": {"creditCard": {
            "paymentDetails": payment,
        }}}]},
    }
    assert _dbs_card_bill_fact(out) is None


@pytest.mark.parametrize("fee_due", ["2026-09-20", "malformed", False, 0, ""])
def test_dbs_supplied_conflicting_fee_cycle_still_rejects(fee_due):
    out = {
        "dbs_card_fee_page": {"bill_due_amount": 1200, "payment_due_date": fee_due},
        "api_responses": {"liabilities": [{"resp": {"creditCard": {
            "paymentDetails": {
                "amount": 1200, "alreadyPaid": -1200, "dueDate": "2026-08-20",
            },
        }}}]},
    }
    assert _dbs_card_bill_fact(out) is None


@pytest.mark.parametrize("sink", ["_log", "format_error_id"])
def test_dbs_partial_fee_fallback_survives_optional_diagnostic_failure(monkeypatch, sink):
    import backend.banks.dbs as module

    def fail(*args, **kwargs):
        raise BrokenPipeError("synthetic diagnostic failure")

    monkeypatch.setattr(module, sink, fail)
    fact = module._dbs_card_bill_fact({
        "dbs_card_fee_page": {"bill_due_amount": 1200},
        "api_responses": {"liabilities": [{"resp": {"creditCard": {
            "paymentDetails": {
                "amount": 1200, "alreadyPaid": -1200, "dueDate": "2026-08-20",
            },
        }}}]},
    })
    assert fact is not None
    assert fact["remaining_due"] == 0
