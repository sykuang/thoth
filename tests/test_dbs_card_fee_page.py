from __future__ import annotations

from backend.banks.dbs import DbsCrawler, _dbs_card_bill_fact


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


def test_dbs_incomplete_cycle_identity_is_unavailable():
    out = {
        "dbs_card_fee_page": {"bill_due_amount": 5000},
        "api_responses": {"liabilities": [{"resp": {"creditCard": {
            "paymentDetails": {
                "amount": 5000, "alreadyPaid": 5000, "dueDate": "2026-08-20",
            },
        }}}]},
    }

    assert _dbs_card_bill_fact(out) is None
