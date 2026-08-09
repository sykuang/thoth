"""Regression guards for the transaction tab's long category view."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TRANSACTIONS = ROOT / "frontend/src/app/(tabs)/transactions.tsx"


def test_transaction_category_empty_state_counts_only_rendered_rows() -> None:
    src = TRANSACTIONS.read_text()

    assert (
        "const filteredCount = viewMode === 'list' ? timelineItems.length "
        ": groupedByCategory.reduce((sum, group) => sum + group.count, 0);"
    ) in src


def test_transaction_category_rows_have_one_recoverable_scroll_owner() -> None:
    src = TRANSACTIONS.read_text()
    root_start = src.index(
        '<ScrollView\n      className="flex-1 bg-ink-50 dark:bg-ink-950"',
    )
    root_end = src.index("</ScrollView>", root_start)
    modal_start = src.index("<Modal", root_end)
    root_scroll = src[root_start:root_end]

    assert "import { KeyboardAwareScrollView }" not in src
    assert "<KeyboardAwareScrollView" not in src
    assert root_end < modal_start
    assert root_scroll.count("<ScrollView") == 1
    assert "groupedByCategory.map" in root_scroll
    assert 'contentInsetAdjustmentBehavior="automatic"' in root_scroll
    assert "contentContainerStyle={{ paddingBottom: 32 }}" in root_scroll
