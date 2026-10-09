from backend.banks.cathay import CathayCrawler
from backend.banks.esun_spa.products import _statement_transactions


def test_cathay_unbilled_row_uses_merchant_name_and_twd_amount() -> None:
    c = object.__new__(CathayCrawler)
    row = {"consumeDate": "2026-10-05T00:00:00", "merchantName": "店", "twdAmount": 120,
           "currency": "TWD", "cardNo": "1234567890123456"}
    out = c._parse_consume({"twdUnbilledConsumeDetail": [row]})["twdUnbilledConsumeDetail"]
    assert out[0]["desc"] == "店" and out[0]["amount"] == 120
    assert c._parse_consume({"twdUnbilledConsumeDetail": [{"twdAmount": None}]}) == {}


def test_esun_statement_rows_wrap_year_and_reject_malformed() -> None:
    body = {"transList": [{"year": "2025", "month": "12", "transDetailList": [
        {"merchantName": " A ", "paymentAmount": 300, "paymentCurrency": "TWD", "transCurrency": "TWD",
         "transAmount": 300, "transMonthDay": "1230", "postingMonthDay": "0102", "cardNo": "****-1234"},
        {"merchantName": "B", "paymentAmount": 900, "paymentCurrency": "TWD", "transCurrency": "USD",
         "transAmount": 28, "transMonthDay": "0110", "postingMonthDay": "0112", "cardNo": "****-1234"},
    ]}]}
    rows = _statement_transactions(body, "2026-01-18")
    assert [(r["consume_date"], r["post_date"]) for r in rows] == [
        ("2025-12-30", "2026-01-02"), ("2026-01-10", "2026-01-12")]
    assert rows[0]["merchant"] == "A" and rows[0]["consume_currency"] is None
    assert rows[1]["consume_currency"] == "USD" and rows[1]["card_last4"] == "1234"
    body["transList"][0]["transDetailList"][0]["paymentAmount"] = "300"
    assert _statement_transactions(body, "2026-01-18") is None
