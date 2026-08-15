from backend.banks.esun import EsunCrawler


def test_esun_statement_detail_parses_independent_post_dates() -> None:
    details = [{
        "bill_month": "2026-07",
        "text_preview": """
交易日期
入帳日期
交易項目/交易國家與地區 外幣折算日
幣別 / 金額
繳款幣別 / 金額
本期消費明細：
07/09
07/15
測試商店
TWD30
07/11
07/14
海外商店 US 07/13
USD10.50
TWD330
本期合計：
TWD360
本期應繳總金額：
TWD360
""",
    }]

    rows = EsunCrawler._parse_card_bill_details(details)

    assert rows == [
        {
            "card_no": "",
            "consume_date": "2026/07/09",
            "post_date": "2026/07/15",
            "merchant": "測試商店",
            "consume_currency": None,
            "consume_amount": None,
            "billed_currency": "TWD",
            "billed_amount": 30.0,
            "status": "已入帳",
            "bill_month": "2026-07",
        },
        {
            "card_no": "",
            "consume_date": "2026/07/11",
            "post_date": "2026/07/14",
            "merchant": "海外商店 US 07/13",
            "consume_currency": "USD",
            "consume_amount": 10.5,
            "billed_currency": "TWD",
            "billed_amount": 330.0,
            "status": "已入帳",
            "bill_month": "2026-07",
        },
    ]


def test_esun_statement_rows_replace_matching_date_less_rows() -> None:
    current = [
        {"consume_date": "2026/07/09", "merchant": "測試商店", "billed_amount": 30,
         "card_no": "9064-XXXX-XXXX-7032", "card_last4": "7032"},
        {"consume_date": "2026/08/14", "merchant": "尚未入帳商店", "billed_amount": 50},
    ]
    statement = [{
        "consume_date": "2026/07/09",
        "post_date": "2026/07/15",
        "merchant": "測試商店",
        "billed_amount": 30.0,
    }]

    expected_statement = {
        **statement[0],
        "card_no": "9064-XXXX-XXXX-7032",
        "card_last4": "7032",
    }
    assert EsunCrawler._merge_card_transactions(current, statement) == [
        current[1],
        expected_statement,
    ]


def test_esun_statement_merge_fails_closed_on_ambiguous_duplicates() -> None:
    current = [
        {"consume_date": "2026/07/09", "merchant": "同店", "billed_amount": 30,
         "card_last4": "1111"},
        {"consume_date": "2026/07/09", "merchant": "同店", "billed_amount": 30,
         "card_last4": "2222"},
    ]
    statement = [
        {"consume_date": "2026/07/09", "post_date": "2026/07/15",
         "merchant": "同店", "billed_amount": 30.0},
        {"consume_date": "2026/07/09", "post_date": "2026/07/15",
         "merchant": "同店", "billed_amount": 30.0},
    ]

    merged = EsunCrawler._merge_card_transactions(current, statement)

    assert merged == current


def test_esun_statement_parser_ignores_session_error_popup() -> None:
    assert EsunCrawler._parse_card_bill_details([{
        "bill_month": "2026-07",
        "text_preview": "連線逾時，請重新登入。",
    }]) == []
