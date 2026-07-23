from __future__ import annotations

from backend.banks.dbs import DbsCrawler


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
