"""flow_type / income_category 落地驗證.

2026-07-28 root cause: 三個 upsert_* 的 INSERT 從來沒寫這兩欄, 全靠 schema
DEFAULT 'expense' — 樂天「存款利息 +$4」被記成支出, passive_income 永遠 0.
"""
from __future__ import annotations

import pytest

from backend.core.store import BankStore, _flow_fields
from backend.server.seed_rules import DEFAULT_RULES


@pytest.fixture()
def store(tmp_path):
    s = BankStore(str(tmp_path / "t.sqlite"), user_id=1)
    yield s
    s.conn.close()


def _rules() -> list[dict]:
    return sorted(DEFAULT_RULES, key=lambda r: -r.get("priority", 100))


@pytest.mark.parametrize(("cat", "sub", "amount", "txn_type", "expected"), [
    ("利息股息", None, 4, None, ("income", "interest_dividend")),
    ("薪資", None, 50000, None, ("income", "salary")),
    ("獎金", None, 10000, None, ("income", "bonus")),
    ("投資收益", None, 1, None, ("income", "investment_gain")),
    ("轉帳", None, -100, None, ("transfer", None)),
    ("還款", None, -100, None, ("transfer", None)),
    ("投資", None, -100, None, ("investment", None)),
    ("其他", "退稅", 3000, None, ("income", "other")),
    ("飲食", None, -120, None, ("expense", None)),
    (None, None, 777, None, ("income", "other")),      # 未分類但正值 → 收入
    (None, None, -777, None, ("expense", None)),
    # 信用卡: txn_type 優先, amount 一律 None (帳單視角正負不可信)
    ("飲食", None, None, "cashback", ("income", None)),
    ("飲食", None, None, "refund", ("income", None)),
    ("金融", "年費減免", None, "fee_waiver", ("income", None)),
    ("還款", None, None, "payment", ("transfer", None)),
    ("飲食", None, None, "spending", ("expense", None)),
    # txn_type 壓過 category — HSBC「減少消費款利息 txn_type=fee」真實案例:
    # category 命中『利息股息』但那是利息「支出」, 不可算收入
    ("利息股息", None, None, "fee", ("expense", None)),
    ("金融", "年費", None, "annual_fee", ("expense", None)),
    ("購物", None, None, "installment", ("expense", None)),
    # unknown 不在硬映射內 → 落回 category / 金額判斷
    ("薪資", None, None, "unknown", ("income", "salary")),
])
def test_flow_fields_mapping(cat, sub, amount, txn_type, expected):
    assert _flow_fields(cat, sub, amount, txn_type) == expected


def test_twd_interest_row_lands_as_income(store):
    """樂天真實案例: 存款利息 +$4 必須是 income/interest_dividend, 不是 expense."""
    store.upsert_twd_txns([{
        "account_no": "0081200000000000",
        "datetime": "2026-02-21T00:00:00",
        "desc": "存款利息",
        "expend": None, "income": 4, "balance": 4,
    }], rules=_rules())
    row = store.conn.execute(
        "SELECT flow_type, income_category, category FROM twd_transactions",
    ).fetchone()
    assert row["category"] == "利息股息"
    assert row["flow_type"] == "income"
    assert row["income_category"] == "interest_dividend"


def test_twd_expense_row_stays_expense(store):
    store.upsert_twd_txns([{
        "account_no": "0081200000000000",
        "datetime": "2026-02-22T00:00:00",
        "desc": "時代廣場停車費",
        "expend": 4300, "income": None, "balance": 0,
    }], rules=_rules())
    row = store.conn.execute(
        "SELECT flow_type, income_category FROM twd_transactions",
    ).fetchone()
    assert row["flow_type"] == "expense"
    assert row["income_category"] is None


def test_card_cashback_row_is_income_without_fire_category(store):
    """卡片回饋算 income 但 income_category=None — 不可污染 FIRE 被動收入."""
    store.upsert_card_billed([{
        "card_no": "1234", "bill_date": "2026-02-01", "currency": "TWD",
        "date": "2026-01-15", "post_date": "2026-01-16",
        "desc": "刷卡現金回饋", "amount": 300, "txn_type": "cashback",
    }], rules=_rules())
    row = store.conn.execute(
        "SELECT flow_type, income_category FROM card_billed_txns",
    ).fetchone()
    assert row["flow_type"] == "income"
    assert row["income_category"] is None


def test_card_spending_row_is_expense(store):
    store.upsert_card_billed([{
        "card_no": "1234", "bill_date": "2026-02-01", "currency": "TWD",
        "date": "2026-01-15", "post_date": "2026-01-16",
        "desc": "全家便利商店", "amount": -120, "txn_type": "spending",
    }], rules=_rules())
    row = store.conn.execute(
        "SELECT flow_type FROM card_billed_txns",
    ).fetchone()
    assert row["flow_type"] == "expense"


def test_card_pending_row_gets_flow_type(store):
    store.refresh_card_pending("unbilled", [{
        "card_no": "1234", "date": "2026-01-20", "desc": "星巴克",
        "amount": -150, "currency": "TWD", "txn_type": "spending",
    }], rules=_rules())
    row = store.conn.execute(
        "SELECT flow_type FROM card_pending_txns",
    ).fetchone()
    assert row["flow_type"] == "expense"


def test_subscription_row_gets_flagged(store):
    """is_subscription 跟 flow_type 同一批死欄位, 2026-07-28 一併補。"""
    store.upsert_card_billed([{
        "card_no": "1234", "bill_date": "2026-02-01", "currency": "TWD",
        "date": "2026-01-15", "post_date": "2026-01-16",
        "desc": "Netflix", "amount": -390, "txn_type": "spending",
    }], rules=_rules())
    row = store.conn.execute(
        "SELECT subcategory, is_subscription FROM card_billed_txns",
    ).fetchone()
    assert row["subcategory"] == "訂閱"
    assert row["is_subscription"] == 1


def test_non_subscription_row_not_flagged(store):
    store.upsert_card_billed([{
        "card_no": "1234", "bill_date": "2026-02-01", "currency": "TWD",
        "date": "2026-01-15", "post_date": "2026-01-16",
        "desc": "全家便利商店", "amount": -120, "txn_type": "spending",
    }], rules=_rules())
    row = store.conn.execute(
        "SELECT is_subscription FROM card_billed_txns",
    ).fetchone()
    assert row["is_subscription"] == 0


def test_foreign_txn_fee_beats_merchant_rule(store):
    """「國外交易手續費ＡＬＰ＊Ｔａｏｂａｏ」不可被『中國電商』搶成 購物/網購。

    HSBC 真實 row: 手續費金額與原始商家名黏在同一個 description。
    priority 300 的「國外交易手續費」必須壓過 priority 110 的商家 rule。
    """
    store.upsert_card_billed([{
        "card_no": "1234", "bill_date": "2026-02-01", "currency": "TWD",
        "date": "2026-01-15", "post_date": "2026-01-16",
        "desc": "國外交易手續費ＡＬＰ＊Ｔａｏｂａｏ", "amount": -17,
        "txn_type": "fee",
    }], rules=_rules())
    row = store.conn.execute(
        "SELECT category, subcategory, flow_type FROM card_billed_txns",
    ).fetchone()
    assert row["category"] == "金融"
    assert row["subcategory"] == "手續費"
    assert row["flow_type"] == "expense"
