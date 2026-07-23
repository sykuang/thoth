"""Regression: card detail payments section must show native last_payment_*.

Some banks store the latest payment on cards.last_payment_amount/date instead of
per-card card_billed_txns rows (or their payment rows are bank-level and don't
join by card_no). The UI's "繳款紀錄" section consumes CardDetail.payments, so
get_card_detail() must synthesize at least one PaymentRow from native fields when
no per-card payment rows exist.
"""
from __future__ import annotations

import sqlite3

from backend.server.db_facade.cards import CardsReadMixin


class _CardsApi(CardsReadMixin):
    pass


def _make_conn() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE cards (
            user_id INTEGER NOT NULL,
            card_no TEXT NOT NULL,
            name TEXT,
            nickname_overwrite TEXT,
            association TEXT,
            type TEXT,
            is_cube INTEGER DEFAULT 0,
            excluded INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            credit_limit REAL,
            used_credit REAL,
            statement_close_date TEXT,
            payment_due_date TEXT,
            bill_due_amount REAL,
            last_payment_amount REAL,
            last_payment_date TEXT,
            updated_at TEXT
        );
        CREATE TABLE card_billed_txns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            card_no TEXT,
            bill_date TEXT,
            currency TEXT,
            consume_date TEXT,
            post_date TEXT,
            description TEXT,
            amount REAL,
            category TEXT,
            subcategory TEXT,
            txn_type TEXT,
            flow_type TEXT
        );
        CREATE TABLE card_pending_txns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            card_no TEXT,
            consume_date TEXT,
            description TEXT,
            amount REAL,
            currency TEXT,
            category TEXT,
            subcategory TEXT
        );
        """
    )
    return con


def test_card_detail_payments_fallbacks_to_native_last_payment(monkeypatch):
    con = _make_conn()
    con.execute(
        """INSERT INTO cards (
               user_id, card_no, name, bill_due_amount,
               last_payment_amount, last_payment_date, payment_due_date
           ) VALUES (1, 'CARD-1', '聯邦卡', 0, 38647, '2026-06-22', '2026-06-18')"""
    )

    from backend.server import db
    monkeypatch.setattr(db, "open_bank_conn", lambda bank: con, raising=True)

    detail = _CardsApi().get_card_detail(
        bank="ubot", user_id=1, card_no="CARD-1", cycle_start="2026-06-01"
    )

    assert detail is not None
    assert len(detail.payments) == 1
    assert detail.payments[0].date == "2026-06-22"
    assert detail.payments[0].amount == 38647.0
    assert detail.payments[0].description == "最近繳款"


def test_card_detail_keeps_real_payment_rows_over_native_fallback(monkeypatch):
    con = _make_conn()
    con.execute(
        """INSERT INTO cards (
               user_id, card_no, name, bill_due_amount,
               last_payment_amount, last_payment_date
           ) VALUES (1, 'CARD-1', '匯豐卡', 100, 6145, '2026-06-08')"""
    )
    con.execute(
        """INSERT INTO card_billed_txns (
               user_id, card_no, consume_date, post_date, description, amount, txn_type, flow_type
           ) VALUES (1, 'CARD-1', '2026-06-08', '2026-06-08', '匯豐銀行自動扣款', -6145, 'payment', 'expense')"""
    )

    from backend.server import db
    monkeypatch.setattr(db, "open_bank_conn", lambda bank: con, raising=True)

    detail = _CardsApi().get_card_detail(
        bank="hsbc", user_id=1, card_no="CARD-1", cycle_start="2026-06-01"
    )

    assert detail is not None
    assert len(detail.payments) == 1
    assert detail.payments[0].date == "2026-06-08"
    assert detail.payments[0].amount == 6145.0
    assert detail.payments[0].description == "匯豐銀行自動扣款"


def test_card_detail_prepends_newer_native_payment_missing_from_transactions(monkeypatch):
    """HSBC detail native payment can update before posted history catches up."""
    con = _make_conn()
    con.execute(
        """INSERT INTO cards (
               user_id, card_no, name, bill_due_amount,
               last_payment_amount, last_payment_date, payment_due_date
           ) VALUES (1, 'CARD-1', '匯豐卡', 34365, 34365, '2026-07-07', '2026-07-06')"""
    )
    con.execute(
        """INSERT INTO card_billed_txns (
               user_id, card_no, consume_date, post_date, description, amount, txn_type, flow_type
           ) VALUES (1, 'CARD-1', '2026-06-08', '2026-06-08',
                     '匯豐銀行自動扣款', -6145, 'payment', 'transfer')"""
    )

    from backend.server import db
    monkeypatch.setattr(db, "open_bank_conn", lambda bank: con, raising=True)

    detail = _CardsApi().get_card_detail(
        bank="hsbc", user_id=1, card_no="CARD-1", cycle_start="2026-06-01"
    )

    assert detail is not None
    assert [p.date for p in detail.payments] == ["2026-07-07", "2026-06-08"]
    assert detail.payments[0].amount == 34365.0
    assert detail.payments[0].description == "最近繳款"


def test_card_detail_current_statement_uses_post_date_bounded_cycle(monkeypatch):
    """Current statement rows are posted after prior close through current close."""
    con = _make_conn()
    con.execute(
        """INSERT INTO cards (user_id, card_no, name, statement_close_date)
           VALUES (1, 'CARD-1', '匯豐卡', '2026-06-18')"""
    )
    con.executemany(
        """INSERT INTO card_billed_txns (
               user_id, card_no, consume_date, post_date, description, amount, txn_type, flow_type
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (1, 'CARD-1', '2026-05-17', '2026-05-20', '本期消費', 18198, 'spending', 'expense'),
            (1, 'CARD-1', '2026-05-18', '2026-05-18', '上期結尾', 100, 'spending', 'expense'),
            (1, 'CARD-1', '2026-06-18', '2026-06-18T12:00:00', '結帳日消費', 300, 'spending', 'expense'),
            (1, 'CARD-1', '2026-06-19', '2026-06-19', '下期消費', 200, 'spending', 'expense'),
        ],
    )

    from backend.server import db
    monkeypatch.setattr(db, "open_bank_conn", lambda bank: con, raising=True)

    detail = _CardsApi().get_card_detail(
        bank="hsbc",
        user_id=1,
        card_no="CARD-1",
        cycle_start="2026-05-18",
        cycle_end="2026-06-18",
    )

    assert detail is not None
    assert [t.description for t in detail.billed_txns] == ["結帳日消費", "本期消費"]


def test_card_detail_payment_dedup_uses_post_date(monkeypatch):
    """Native payment date and transaction effective date share post-date semantics."""
    con = _make_conn()
    con.execute(
        """INSERT INTO cards (
               user_id, card_no, name, last_payment_amount, last_payment_date
           ) VALUES (1, 'CARD-1', '匯豐卡', 34365, '2026-07-07')"""
    )
    con.execute(
        """INSERT INTO card_billed_txns (
               user_id, card_no, consume_date, post_date, description, amount, txn_type, flow_type
           ) VALUES (1, 'CARD-1', '2026-07-06', '2026-07-07T08:30:00',
                     '匯豐銀行自動扣款', -34365, 'payment', 'transfer')"""
    )

    from backend.server import db
    monkeypatch.setattr(db, "open_bank_conn", lambda bank: con, raising=True)

    detail = _CardsApi().get_card_detail(
        bank="hsbc", user_id=1, card_no="CARD-1", cycle_start="2026-06-01"
    )

    assert detail is not None
    assert len(detail.payments) == 1
    assert detail.payments[0].date == "2026-07-07"
    assert detail.payments[0].amount == 34365.0
    assert detail.payments[0].description == "匯豐銀行自動扣款"


def test_card_detail_legacy_cycle_start_is_consume_date_inclusive(monkeypatch):
    """Callers without cycle_end retain the old consume_date >= start contract."""
    con = _make_conn()
    con.execute(
        """INSERT INTO cards (user_id, card_no, name)
           VALUES (1, 'CARD-1', '測試卡')"""
    )
    con.executemany(
        """INSERT INTO card_billed_txns (
               user_id, card_no, consume_date, post_date, description, amount, txn_type, flow_type
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (1, 'CARD-1', '2026-06-01T12:00:00', '2026-05-31', '起點消費', 100, 'spending', 'expense'),
            (1, 'CARD-1', '2026-05-31', '2026-06-02', '起點前消費', 200, 'spending', 'expense'),
        ],
    )

    from backend.server import db
    monkeypatch.setattr(db, "open_bank_conn", lambda bank: con, raising=True)

    detail = _CardsApi().get_card_detail(
        bank="hsbc", user_id=1, card_no="CARD-1", cycle_start="2026-06-01"
    )

    assert detail is not None
    assert [t.description for t in detail.billed_txns] == ["起點消費"]


def test_card_detail_exposes_category_subcategory_instead_of_txn_type_only(monkeypatch):
    """Card detail rows must carry user-facing category/subcategory.

    The frontend card detail page should not need to invent extra txn_type badges
    like 「退款」「手續費」. Those meanings live in category/subcategory already;
    txn_type stays as the internal cashflow/display-direction signal.
    """
    con = _make_conn()
    con.execute(
        """INSERT INTO cards (user_id, card_no, name, statement_close_date)
           VALUES (1, 'CARD-1', '測試卡', '2026-06-01')"""
    )
    con.execute(
        """INSERT INTO card_billed_txns (
               user_id, card_no, consume_date, post_date, description, amount,
               category, subcategory, txn_type, flow_type
           ) VALUES (
               1, 'CARD-1', '2026-06-10', '2026-06-11', '國外交易手續費',
               30, '金融', '手續費', 'fee', 'expense'
           )"""
    )
    con.execute(
        """INSERT INTO card_pending_txns (
               user_id, card_no, consume_date, description, amount, currency,
               category, subcategory
           ) VALUES (
               1, 'CARD-1', '2026-06-12', 'Amazon refund pending', -100, 'TWD',
               '其他', '退款'
           )"""
    )

    from backend.server import db
    monkeypatch.setattr(db, "open_bank_conn", lambda bank: con, raising=True)

    detail = _CardsApi().get_card_detail(
        bank="hsbc", user_id=1, card_no="CARD-1", cycle_start="2026-06-01"
    )

    assert detail is not None
    assert len(detail.billed_txns) == 1
    billed = detail.billed_txns[0]
    assert billed.category == "金融"
    assert billed.subcategory == "手續費"
    assert billed.txn_type == "fee"  # still present for internal direction/audit

    assert len(detail.pending_txns) == 1
    pending = detail.pending_txns[0]
    assert pending.category == "其他"
    assert pending.subcategory == "退款"
