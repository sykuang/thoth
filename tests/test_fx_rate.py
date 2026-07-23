"""Tests for fx_rate field on /transactions billed row (Phase 6).

驗:
- 外幣消費 (EUR + TWD amount 都有) → fx_rate = |amount| / |consume_amount|, source='bank_billed'
- 純台幣消費 (consume_currency = TWD or None) → fx_rate = None
- 缺 consume_amount → fx_rate = None
- 缺 amount → fx_rate = None
- 退款 (negative amount) → fx_rate 仍正值 (取絕對值)
- 禁推算 — 沒 source 不該 fabricate (regression: 之前說過 EUR 662 → "推算 33.7" 是禁區)
"""
from __future__ import annotations

import sqlite3

import pytest

from backend.server.routers.transactions import _billed_to_transaction


def _row(**kwargs) -> sqlite3.Row:
    """造一個假 sqlite3.Row (用真 in-memory DB 以拿到正確 Row type)。

    Col types 自動推斷: int → INTEGER, float → REAL, 其他 → TEXT。
    這樣 SQLite 把 100 存成 int 而非 '100', _billed_to_transaction 比較
    `amt > 0` 才不會 TypeError。
    """
    def _col_type(v):
        if isinstance(v, bool):
            return "INTEGER"
        if isinstance(v, int):
            return "INTEGER"
        if isinstance(v, float):
            return "REAL"
        return "TEXT"

    cols = list(kwargs.keys())
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    placeholders = ", ".join("?" * len(cols))
    col_def = ", ".join(f"{c} {_col_type(kwargs[c])}" for c in cols)
    cur.execute(f"CREATE TABLE t ({col_def})")
    cur.execute(f"INSERT INTO t ({', '.join(cols)}) VALUES ({placeholders})", list(kwargs.values()))
    return cur.execute("SELECT * FROM t").fetchone()


def test_fx_rate_computed_for_foreign_currency() -> None:
    """EUR 100 → TWD 3370 (e.g. HSBC) → fx_rate=33.7"""
    row = _row(
        id=1,
        card_no="****7016",
        consume_date="2026-06-06",
        post_date="2026-06-10",
        description="APE Make-In-Sila",
        amount=3370,         # TWD billed (正值, 銀行視角)
        currency="TWD",
        consume_currency="EUR",
        consume_amount=100.0,
    )
    t = _billed_to_transaction("hsbc", row)
    assert t["fx_rate"] == pytest.approx(33.7)
    assert t["fx_rate_source"] == "bank_billed"
    assert t["consume_currency"] == "EUR"
    assert t["consume_amount"] == 100.0


def test_fx_rate_handles_negative_amount() -> None:
    """退款 → amount 是 negative, fx_rate 仍應正值 (絕對值)"""
    row = _row(
        id=2,
        amount=-3370,
        consume_currency="EUR",
        consume_amount=-100.0,
    )
    t = _billed_to_transaction("hsbc", row)
    assert t["fx_rate"] == pytest.approx(33.7)


def test_fx_rate_null_for_twd_consume() -> None:
    """consume_currency == 'TWD' → fx_rate=None (純台幣消費沒匯率)"""
    row = _row(
        id=3,
        amount=500,
        consume_currency="TWD",
        consume_amount=500.0,
    )
    t = _billed_to_transaction("cathay", row)
    assert t["fx_rate"] is None
    assert t["fx_rate_source"] is None


def test_fx_rate_null_when_consume_currency_missing() -> None:
    """consume_currency=None → fx_rate=None (老資料 / 國內消費)"""
    row = _row(
        id=4,
        amount=500,
        consume_currency=None,
        consume_amount=None,
    )
    t = _billed_to_transaction("hsbc", row)
    assert t["fx_rate"] is None


def test_fx_rate_null_when_consume_amount_zero() -> None:
    """consume_amount=0 → 避免 div by zero, fx_rate=None"""
    row = _row(
        id=5,
        amount=100,
        consume_currency="EUR",
        consume_amount=0.0,
    )
    t = _billed_to_transaction("hsbc", row)
    assert t["fx_rate"] is None


def test_fx_rate_null_when_consume_amount_missing() -> None:
    """consume_amount=None → fx_rate=None"""
    row = _row(
        id=6,
        amount=100,
        consume_currency="USD",
        consume_amount=None,
    )
    t = _billed_to_transaction("ctbc", row)
    assert t["fx_rate"] is None


def test_fx_rate_does_not_estimate_when_no_billed_data() -> None:
    """REGRESSION: 使用者明確禁止推算/spot rate fallback。
    若 banks 沒提供 consume_amount, fx_rate 必須 None, 絕不能去 web/cache/spot 推算。
    這個 test 鎖死契約: input 缺資料 → output null, 不要試圖填補。
    """
    row = _row(
        id=7,
        amount=22308,        # TWD 22308
        consume_currency="EUR",
        consume_amount=None,  # 銀行沒給原幣金額 (e.g. 老 schema)
    )
    t = _billed_to_transaction("hsbc", row)
    # 即使我們知道大概 1 EUR ≈ 33.7, 仍必須回 None
    assert t["fx_rate"] is None, "禁推算 - 沒 consume_amount 就回 null"
    assert t["fx_rate_source"] is None
