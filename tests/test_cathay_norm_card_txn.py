"""CathayCrawler._norm_card_txn unit test — 防 consume_currency 空字串 bug 復發。

2026-06-13 發現 cathay DB 4 筆 billed `consume_currency=''` 是因為 cathay 帳單
API 對台幣消費根本沒給 `consumeCurrency` 欄,直接 `t.get("consumeCurrency")`
回 None,經過某轉換變成 `''` 寫進 DB → 之後跨銀行查外幣交易會撞到髒資料。

修法在 cathay._norm_card_txn:
  • 有 consumeAmount + consumeCurrency 非空 → 外幣（保留）
  • 否則 → 統一 TWD，consume_amount=None

這份 test 鎖住規範化規則,將來 cathay collect 或 API 結構改了不會偷偷退化。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.banks.cathay import CathayCrawler, _cathay_card_bill_fact


@pytest.fixture
def crawler(monkeypatch):
    """繞過 CathayCrawler.__init__ 的 cred 載入。"""
    c = CathayCrawler.__new__(CathayCrawler)
    # _norm_card_txn 會呼叫 self.mask_card(); 給個簡化版
    c.mask_card = lambda num: f"****{str(num)[-4:]}" if num else ""
    return c


def test_twd_consumption_no_consume_currency_field(crawler):
    """台幣消費: cathay API 沒給 consumeCurrency → 必填 TWD, consume_amount=None。"""
    out = crawler._norm_card_txn({
        "cardNo": "9000000000367037",
        "consumeDate": "2026-05-01",
        "transDesc": "全聯",
        "amount": 500,
        "currency": "TWD",
        # 注意: 故意不放 consumeCurrency/consumeAmount
    })
    assert out["consume_currency"] == "TWD"
    assert out["consume_amount"] is None
    assert out["currency"] == "TWD"
    assert out["card_no"] == "****7037"


def test_foreign_consumption_preserved(crawler):
    """外幣消費: consumeCurrency + consumeAmount 都有 → 原值保留。"""
    out = crawler._norm_card_txn({
        "cardNo": "9000000000367037",
        "consumeDate": "2026-05-02",
        "transDesc": "Apple Store",
        "amount": 3200,
        "currency": "TWD",
        "consumeCurrency": "USD",
        "consumeAmount": 99.99,
        "consumeCountry": "US",
    })
    assert out["consume_currency"] == "USD"
    assert out["consume_amount"] == 99.99
    assert out["consume_country"] == "US"
    assert out["currency"] == "TWD"           # 入帳幣別仍是台幣
    assert out["amount"] == 3200


def test_empty_string_consume_currency_normalized(crawler):
    """consumeCurrency='' (空字串) → 視為台幣, 不寫進 DB 變污染。"""
    out = crawler._norm_card_txn({
        "cardNo": "9000000000367037",
        "consumeDate": "2026-05-03",
        "transDesc": "X",
        "amount": 100,
        "currency": "TWD",
        "consumeCurrency": "",       # ← bug 來源
        "consumeAmount": 0,
    })
    assert out["consume_currency"] == "TWD", \
        f"空字串 consumeCurrency 應該被當 TWD, 不該污染 DB; got {out['consume_currency']!r}"
    assert out["consume_amount"] is None


def test_summary_row_no_card_no(crawler):
    """「上期帳單總額」「自動扣繳」沒 cardNo → card_no='' 但 currency 必填 TWD。"""
    out = crawler._norm_card_txn({
        "consumeDate": None,
        "transDesc": "上期帳單總額",
        "amount": 2130,
    })
    assert out["card_no"] == ""
    assert out["consume_currency"] == "TWD"
    assert out["consume_amount"] is None


def test_consume_currency_with_zero_amount_treated_as_twd(crawler):
    """consumeCurrency='USD' 但 consumeAmount=0 → 看作台幣(避免假外幣污染)。"""
    out = crawler._norm_card_txn({
        "cardNo": "9000000000367037",
        "consumeDate": "2026-05-04",
        "transDesc": "退款 0",
        "amount": 0,
        "currency": "TWD",
        "consumeCurrency": "USD",
        "consumeAmount": 0,           # ← 邊界
    })
    # 沒實際外幣金額 → 視為台幣
    assert out["consume_currency"] == "TWD"
    assert out["consume_amount"] is None


def test_country_empty_string_normalized_to_none(crawler):
    """consumeCountry='' → None (避免空字串污染)。"""
    out = crawler._norm_card_txn({
        "cardNo": "9000000000367037",
        "consumeDate": "2026-05-05",
        "transDesc": "X",
        "amount": 100,
        "currency": "TWD",
        "consumeCountry": "",
    })
    assert out["consume_country"] is None


@pytest.mark.parametrize(("fields", "expected", "post_date"), [
    ({"postingDate": "2026-05-08"}, "booked", "2026-05-08"),
    ({"convertDate": "2026-05-07", "postingDate": "2026-05-08"}, "booked", "2026-05-08"),
    ({"convertDate": "2026-05-07"}, None, None),
    ({"beginValueDate": "2026-05-07"}, None, None),
    ({"postingDate": "2026-05-08T00:00:00"}, "booked", "2026-05-08"),
    ({"postingDate": "2099-01-01"}, "booked", "2099-01-01"),
    *[({"postingDate": f"2026-06-23T00:30:45.123456{offset}"}, "booked", "2026-06-23")
      for offset in ("Z", "+08:00", "+0800", "-08:00", "-0800", "+23:59", "-2359")],
    ({"postingDate": "2026-06-23T00:00:00+08:60", "convertDate": "2026-06-23",
      "beginValueDate": "2026-06-23"}, None, None),
    *[({"postingDate": value}, None, None) for value in (
        "2026-02-30", "0000-00-00", "2026-05-08junk", True, 20260508,
        "2026-06-23T00:00:00+08:60", "2026-06-23T00:00:00+0860",
        "2026-06-23T00:00:00-08:60", "2026-06-23T00:00:00-0860",
    )],
])
def test_booking_evidence_keeps_posting_separate_from_conversion(crawler, fields, expected, post_date):
    row = crawler._norm_card_txn({
        "cardNo": "9000000000367037", "consumeDate": "2026-05-05",
        "transDesc": "Synthetic purchase", "amount": 100, **fields,
    })
    assert row.get("booking_status") == expected
    assert row["post_date"] == post_date


@pytest.mark.parametrize("parser,container", [
    ("_parse_bill_detail", "twdBillDetailInfo"),
    ("_parse_consume", "consumeList"),
])
@pytest.mark.parametrize("posting_date", ["1999-01-01", "2099-01-01"])
def test_card_parsers_preserve_bank_posting_dates(crawler, parser, container, posting_date):
    raw = {
        "consumeDate": "2026-05-05", "postingDate": posting_date,
        "amount": 100, "transDesc": "Synthetic purchase",
    }
    parsed = getattr(crawler, parser)({container: [{"tradeData": [raw]}]})
    row = next(iter(parsed.values()))[0]

    assert row["date"] == "2026-05-05"
    assert row["post_date"] == posting_date
    assert row["booking_status"] == "booked"


class _LatestBillCollector:
    hits = []

    def __init__(self, status):
        self.status = status

    def latest(self, endpoint):
        if endpoint != "C_CardInfo_Q_LatestBill":
            return None
        return SimpleNamespace(resp_json={
            "success": True,
            "content": {
                "twdBillDetail": {"billAmount": 4321, "payBillStatus": self.status},
                "usdBillDetail": None,
            },
        })


@pytest.mark.parametrize(("raw", "canonical"), [
    ("Paid", "paid"),
    ("Payed", "paid"),
    ("UnPaid", "unpaid"),
    # Live bill overview shows an outstanding statement and「我要繳費」for Payment.
    ("Payment", "unpaid"),
])
def test_latest_bill_status_is_canonical_enum_value(crawler, raw, canonical):
    result = crawler._parse(_LatestBillCollector(raw))
    assert result["credit_card"]["latest_bill"]["twd"]["payBillStatus"] == canonical
    assert result["card_bill_facts_ok"] is True
    assert result["card_bill_facts"] == [{
        "scope": "bank",
        "status": canonical,
        "remaining_due": 0.0 if canonical == "paid" else 4321.0,
    }]


@pytest.mark.parametrize(("raw", "remaining"), [("Payment", 4321), ("Payed", 0)])
def test_legacy_latest_bill_liability_uses_shared_status(raw, remaining):
    from backend.server.routers.portfolio import _liab_cathay

    assert _liab_cathay({
        "latest_bill": {"twd": {"billAmount": 4321, "payBillStatus": raw}},
    }) == remaining


def test_latest_bill_rejects_unknown_status(crawler):
    with pytest.raises(ValueError, match="unsupported Cathay bill status"):
        crawler._parse(_LatestBillCollector("MaybePaid"))


def test_latest_paid_bill_with_malformed_amount_is_unavailable(crawler):
    collector = _LatestBillCollector("Payed")
    original_latest = collector.latest

    def latest(endpoint):
        hit = original_latest(endpoint)
        if hit is not None:
            hit.resp_json["content"]["twdBillDetail"]["billAmount"] = True
        return hit

    collector.latest = latest
    result = crawler._parse(collector)

    assert result["card_bill_facts_ok"] is False
    assert result["card_bill_facts"] == []


def test_cathay_canonical_fact_carries_dates_and_newest_real_payment():
    out = {
        "credit_card": {
            "latest_bill": {
                "due_date": "2026-08-20",
                "twd": {"payBillStatus": "paid", "billAmount": 1000},
            },
            "bill_summary": {
                "currencies": [{"billDate": "2026-08-01"}],
            },
            "billed_detail": {"TWD": [{
                "desc": "本行自動扣繳", "amount": -900,
                "post_date": "2026-08-05",
            }]},
        },
        "twd_transactions": [{"transactions": [{
            "desc": "信用卡款", "expend": 1000,
            "account_date": "2026-08-10",
        }]}],
    }

    fact = _cathay_card_bill_fact(out)

    assert fact is not None
    assert fact.get("statement_close_date") == "2026-08-01"
    assert fact.get("payment_due_date") == "2026-08-20"
    assert fact.get("last_payment_amount") == 1000.0
    assert fact.get("last_payment_date") == "2026-08-10"
