"""驗證 db_facade _bill_summary_for_cards 的 native overlay 邏輯.

2026-06-20 (HSBC bill_due 1.3M bug 修):
   _bill_summary_for_cards 在 derive (card_billed_txns SUM latest bill_date)
   完成後, 多一個 native overlay: 若 cards 表 bill_due_amount / last_payment_*
   不是 NULL → 蓋過 derive. NULL → 保留 derive.

   重點驗證:
   1. native 不是 NULL → bill_due_amount 用 native (蓋 derive 假象)
   2. native 是 NULL → bill due 維持 unknown，不從交易加總猜測
   3. 部分 native (只 bill_due 有, last_payment_amount NULL) → 對應欄獨立覆寫
"""
from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture
def con_with_native_and_derive():
    """cards 表有 native + card_billed_txns 也有 row, 模擬 overlay 場景."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE cards (
            user_id INTEGER NOT NULL,
            card_no TEXT NOT NULL,
            name TEXT,
            association TEXT,
            type TEXT,
            is_cube INTEGER,
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
        )
    """)
    con.execute("""
        CREATE TABLE card_billed_txns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            card_no TEXT,
            bill_date TEXT,
            consume_date TEXT,
            amount REAL,
            description TEXT,
            txn_type TEXT
        )
    """)
    con.execute("CREATE TABLE card_pending_txns (id INTEGER, user_id INTEGER, card_no TEXT, amount REAL)")

    # CARD-HSBC: native 都填了 (HSBC 場景), derive 也算得出 (用 _不該_ 蓋掉)
    # CARD-DERIVE: native 三欄都 NULL (其他銀行場景), 走 derive
    # CARD-PARTIAL: 只有 bill_due_amount native, last_payment_* NULL
    con.execute(
        "INSERT INTO cards (user_id, card_no, bill_due_amount, last_payment_amount, last_payment_date) "
        "VALUES (1, 'CARD-HSBC', 71032.0, 622.0, '2026-06-11')"
    )
    con.execute(
        "INSERT INTO cards (user_id, card_no, bill_due_amount, last_payment_amount, last_payment_date) "
        "VALUES (1, 'CARD-DERIVE', NULL, NULL, NULL)"
    )
    con.execute(
        "INSERT INTO cards (user_id, card_no, bill_due_amount, last_payment_amount, last_payment_date) "
        "VALUES (1, 'CARD-PARTIAL', 12345.0, NULL, NULL)"
    )

    # 三張卡 derive (bill_date NULL → 一鍋 SUM, 模擬 HSBC bill_date NULL bug)
    # 都灌 999999 看 native 有沒蓋
    for cn in ("CARD-HSBC", "CARD-DERIVE", "CARD-PARTIAL"):
        con.execute(
            "INSERT INTO card_billed_txns (user_id, card_no, bill_date, consume_date, amount, txn_type) "
            f"VALUES (1, '{cn}', NULL, '2026-06-10', 999999.0, 'consume')"
        )
        # 灌 derive payment (-99999)
        con.execute(
            "INSERT INTO card_billed_txns (user_id, card_no, bill_date, consume_date, amount, txn_type) "
            f"VALUES (1, '{cn}', NULL, '2026-06-15', -99999.0, 'payment')"
        )
    con.commit()
    yield con
    con.close()


def test_native_overlay_overrides_derive_for_hsbc(con_with_native_and_derive):
    """CARD-HSBC: native 三欄都填 → bill_due/last_payment_* 都用 native (71032/622/'2026-06-11')."""
    from backend.server.db_facade.cards import _bill_summary_for_cards
    from backend.server.db_facade._base import _BaseHelpers

    class _Helpers(_BaseHelpers):
        def __init__(self):
            pass

    summary = _bill_summary_for_cards(
        _Helpers(), con_with_native_and_derive, user_id=1,
        card_nos=["CARD-HSBC"],
    )
    s = summary["CARD-HSBC"]
    assert s["bill_due_amount"] == 71032.0, (
        f"native 71032 應蓋過 derive 999999, 實得 {s['bill_due_amount']}"
    )
    assert s["last_payment_amount"] == 622.0
    assert s["last_payment_date"] == "2026-06-11"


def test_missing_native_bill_due_does_not_derive_from_transaction_sum(con_with_native_and_derive):
    from backend.server.db_facade.cards import _bill_summary_for_cards
    from backend.server.db_facade._base import _BaseHelpers

    class _Helpers(_BaseHelpers):
        def __init__(self):
            pass

    summary = _bill_summary_for_cards(
        _Helpers(), con_with_native_and_derive, user_id=1,
        card_nos=["CARD-DERIVE"],
    )
    s = summary["CARD-DERIVE"]
    assert s["bill_due_amount"] is None
    # Real payment rows may still supply history; they do not settle the bill fact.
    assert s["last_payment_amount"] == 99999.0
    assert s["last_payment_date"] == "2026-06-15"


def test_native_overlay_partial_only_overrides_filled_fields(con_with_native_and_derive):
    """CARD-PARTIAL: bill_due native=12345 (蓋 derive); last_payment_* NULL → 仍走 derive."""
    from backend.server.db_facade.cards import _bill_summary_for_cards
    from backend.server.db_facade._base import _BaseHelpers

    class _Helpers(_BaseHelpers):
        def __init__(self):
            pass

    summary = _bill_summary_for_cards(
        _Helpers(), con_with_native_and_derive, user_id=1,
        card_nos=["CARD-PARTIAL"],
    )
    s = summary["CARD-PARTIAL"]
    assert s["bill_due_amount"] == 12345.0  # native 蓋掉 derive 999999
    assert s["last_payment_amount"] == 99999.0  # derive 留著 (native NULL)
    assert s["last_payment_date"] == "2026-06-15"
