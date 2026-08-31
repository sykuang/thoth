"""Regression: Sinopac card_summary latest payment fields persist into cards.

永豐 raw metric/card_summary already exposes 最近繳款金額 + 最近繳款日期.
These must populate cards.last_payment_amount/date for the matching card so
frontend 最近繳款 + 繳款紀錄 can show correctly.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.banks.sinopac import _sinopac_card_bill_fact
from backend.core.persist.sinopac import _persist_sinopac as persist_sinopac
from backend.core.store import BankStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch):
    from backend.core import store as store_mod

    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path, raising=True)
    s = BankStore("sinopac_last_payment_test", user_id=1)
    yield s
    s.close()


def test_sinopac_realtime_remaining_due_outranks_static_statement_total():
    out = {
        "card_summary": [{"SubInfo": [[
            {"DataText": "本期應繳", "DataValue": "0"},
            {"DataText": "繳款截止日", "DataValue": "2026/08/20"},
        ]]}],
        "card_statements": [{
            "summary": {"current_due": 5000},
            "payment_due_date": "2026/08/20",
        }],
    }

    fact = _sinopac_card_bill_fact(out)

    assert fact is not None
    assert fact["remaining_due"] == 0.0


def test_sinopac_card_summary_latest_payment_writes_card_native_fields(store):
    data = {
        "all_cards": {
            "Result": {
                "Items": [
                    {
                        "CardNo": "9000000000417020",
                        "Name": "DAWHO現金回饋Debit卡",
                        "CardTypeDesc": "Debit",
                        "CardBrand": "VISA",
                        "ExpDate": "0829",
                    }
                ]
            }
        },
        "card_summary": [
            {
                "TitleInfo": "",
                "SubInfo": [[
                    {"DataText": "本期應繳", "DataValue": "0"},
                    {"DataText": "最近繳款金額", "DataValue": "69"},
                    {"DataText": "最近繳款日期", "DataValue": "2026/05/04"},
                    {"DataText": "繳款截止日", "DataValue": "2026/07/01"},
                    {"DataText": "結帳日", "DataValue": "2026/06/16"},
                    {"DataText": "信用額度(臺幣)", "DataValue": "409,000"},
                    {"DataText": "可用額度(臺幣)", "DataValue": "409,000"},
                ]],
            }
        ],
    }

    persist_sinopac(data, store)

    row = store.conn.execute(
        """SELECT card_no, bill_due_amount, last_payment_amount, last_payment_date,
                  payment_due_date, statement_close_date
           FROM cards
           WHERE user_id = 1"""
    ).fetchone()

    assert row is not None
    assert row["card_no"] == "9000000000417020"
    assert row["bill_due_amount"] == 0.0
    assert row["last_payment_amount"] == 69.0
    assert row["last_payment_date"] == "2026-05-04"
    assert row["payment_due_date"] == "2026-07-01"
    assert row["statement_close_date"] == "2026-06-16"
