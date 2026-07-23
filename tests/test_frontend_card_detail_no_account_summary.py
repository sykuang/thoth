"""Regression: credit-card account detail header must not show account-summary raw-ish fields.

The card detail page should keep bill summary + transaction sections, but must not
render account-level summary rows such as unbilled amount, credit limit, statement
close date, or recent payment directly under the card account header/bill summary.
"""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CARD_DETAIL_TSX = ROOT / "frontend/src/app/(tabs)/cards/[bank]/[card_no].tsx"


def test_card_detail_does_not_render_account_summary_rows_under_card_header():
    src = CARD_DETAIL_TSX.read_text()

    forbidden_labels = [
        'label="最近繳款"',
        'label="未出帳消費"',
        'label="信用額度"',
        'label="本期帳單日"',
    ]
    for label in forbidden_labels:
        assert label not in src

    assert "function StatLine" not in src
