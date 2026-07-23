"""驗證 persist_hsbc 把 HSBC API card_detail.details[] 的「Last Statement Amount /
Last Payment Amount / Last Payment Date」抽進 cards 表新欄.

2026-06-20 (本期應繳 1,320,961 bug 修):
   HSBC API 不在 card_billed_txns 給 bill_date, 原 db_facade derive 把整
   12 個月歷史消費 SUM 成「本期應繳」, 飆到 1.3M (正確是 71,032).

   修法 = 走 HSBC card_detail.details[] 直接抽 native 欄位寫進 cards 表,
   db_facade _bill_summary_for_cards 後置 overlay 蓋過 derive 結果.

case A: 完整 payload → cards.bill_due_amount=71032, last_payment_amount=622,
        last_payment_date='2026-06-11'
case B: details[] 沒給 Last Payment → 該欄 NULL (向後相容, 其他卡可能沒繳款史)
case C: details[] 沒給 Last Statement → bill_due_amount NULL (尚未出帳新卡)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.persist.hsbc import persist_hsbc
from backend.core.store import BankStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch):
    from backend.core import store as store_mod
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path, raising=True)
    s = BankStore("hsbc_test")
    yield s
    s.close()


def _hsbc_card_payload(tail: str = "7034", details: list[dict] | None = None) -> dict:
    """產一個極簡 HSBC collect() payload (cards + card_detail)."""
    masked = f"4029-****-****-{tail}"
    return {
        "cards": [
            {
                "maskedCardNumber": masked,
                "name": "滙豐旅人無限卡",
                "cardType": "Credit",
                # HSBC API 真實 outstandingBalance 是 numeric 不是字串
                # (對齊 prod card_summary daily_metric: "outstanding": 116458.0)
                "outstandingBalance": 116458.0,
                "paymentDueDate": "05-06-2026",
                "statementDate": "18-05-2026",
                "cardStatusDisplay": "ACTIVATED",
            }
        ],
        "card_detail": {
            tail: {
                "masked": masked,
                "posted": [],
                "unposted": [],
                "detail": {
                    "details": details
                    or [
                        {"key": "Credit Limit", "value": "1,500,000 TWD"},
                        {"key": "Last Statement Date", "value": "18 May 2026"},
                        {"key": "Last Statement Amount", "value": "71,032 TWD"},
                        {"key": "Last Payment Amount", "value": "622 TWD"},
                        {"key": "Last Payment Date", "value": "11 Jun 2026"},
                    ],
                },
            }
        },
    }


def test_hsbc_persist_extracts_last_statement_amount(store):
    """case A: 完整 HSBC payload → cards.bill_due_amount=71032 (本期應繳官方值)."""
    persist_hsbc(_hsbc_card_payload(), store)

    row = store.conn.execute(
        """SELECT card_no, credit_limit, used_credit,
                  bill_due_amount, last_payment_amount, last_payment_date,
                  statement_close_date, payment_due_date
             FROM cards WHERE card_no = ?""",
        ("4029-****-****-7034",),
    ).fetchone()

    assert row is not None
    assert row["credit_limit"] == 1500000.0
    assert row["used_credit"] == 116458.0
    # 核心斷言: HSBC API「Last Statement Amount」71,032 寫進 cards.bill_due_amount
    # (不是 1,320,961 那個 card_billed_txns SUM 出來的 derive 假象)
    assert row["bill_due_amount"] == 71032.0
    assert row["last_payment_amount"] == 622.0
    assert row["last_payment_date"] == "2026-06-11"
    assert row["statement_close_date"] == "2026-05-18"
    assert row["payment_due_date"] == "2026-06-05"


def test_hsbc_persist_handles_missing_last_payment(store):
    """case B: details 沒給 Last Payment → last_payment_amount/date 都 NULL.

    這是「新卡尚未繳款史」情境, 不該寫 0 (會被誤認「沒繳款=逾期 0 元」).
    """
    details = [
        {"key": "Credit Limit", "value": "1,500,000 TWD"},
        {"key": "Last Statement Amount", "value": "71,032 TWD"},
        # 故意不給 Last Payment Amount / Date
    ]
    persist_hsbc(_hsbc_card_payload(details=details), store)

    row = store.conn.execute(
        """SELECT bill_due_amount, last_payment_amount, last_payment_date
             FROM cards WHERE card_no = ?""",
        ("4029-****-****-7034",),
    ).fetchone()
    assert row["bill_due_amount"] == 71032.0
    assert row["last_payment_amount"] is None
    assert row["last_payment_date"] is None


def test_hsbc_persist_handles_missing_last_statement(store):
    """case C: details 沒給 Last Statement Amount → bill_due_amount NULL.

    NULL 進 db_facade _bill_summary_for_cards 會走 derive fallback
    (不該 hardcode 0, 否則新出帳 cycle 一開始顯示「本期應繳 $0」誤導).
    """
    details = [
        {"key": "Credit Limit", "value": "1,500,000 TWD"},
        {"key": "Last Payment Amount", "value": "622 TWD"},
        {"key": "Last Payment Date", "value": "11 Jun 2026"},
    ]
    persist_hsbc(_hsbc_card_payload(details=details), store)

    row = store.conn.execute(
        """SELECT bill_due_amount, last_payment_amount, last_payment_date
             FROM cards WHERE card_no = ?""",
        ("4029-****-****-7034",),
    ).fetchone()
    assert row["bill_due_amount"] is None
    assert row["last_payment_amount"] == 622.0
    assert row["last_payment_date"] == "2026-06-11"


def test_hsbc_persist_upsert_preserves_existing_native_fields(store):
    """同 card 兩次 sync, 第二次 details 沒給 → 不該被 NULL 沖掉 (COALESCE 防呼).

    場景: 本期繳款後再 sync, HSBC 暫時不回 Last Payment (極端). 老值應該保留.
    """
    # 1st sync: 完整
    persist_hsbc(_hsbc_card_payload(), store)
    # 2nd sync: 只給 Credit Limit
    details = [{"key": "Credit Limit", "value": "1,500,000 TWD"}]
    persist_hsbc(_hsbc_card_payload(details=details), store)

    row = store.conn.execute(
        """SELECT bill_due_amount, last_payment_amount, last_payment_date
             FROM cards WHERE card_no = ?""",
        ("4029-****-****-7034",),
    ).fetchone()
    # 老值都保留 (COALESCE 防呼)
    assert row["bill_due_amount"] == 71032.0
    assert row["last_payment_amount"] == 622.0
    assert row["last_payment_date"] == "2026-06-11"
