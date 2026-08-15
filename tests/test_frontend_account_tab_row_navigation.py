"""Regression: account tab rows reuse the existing transactions tab.

Do not create a parallel transaction-detail route under the accounts stack. Account
rows should deep-link into the existing 收支表 tab with exact account/card refs,
while rename remains an explicit edit action.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS_TAB_TSX = ROOT / "frontend/src/app/(tabs)/cards/index.tsx"
CARDS_LAYOUT_TSX = ROOT / "frontend/src/app/(tabs)/cards/_layout.tsx"
TRANSACTIONS_TSX = ROOT / "frontend/src/app/(tabs)/transactions.tsx"
ACCOUNT_DETAIL_TSX = ROOT / "frontend/src/app/(tabs)/cards/[bank]/accounts/[account_no].tsx"


def test_account_row_tap_deeplinks_to_existing_transactions_tab_not_new_route():
    src = ACCOUNTS_TAB_TSX.read_text()
    account_row = src[src.index("function AccountRow"):src.index("function CardRow")]

    assert "const router = useRouter();" in account_row
    assert "pathname: '/(tabs)/transactions'" in account_row
    assert "kind: 'twd'" in account_row
    assert "account_no: account.account_no" in account_row
    assert "account_tail: account.account_no.slice(-4)" not in account_row
    assert "testID={`account-detail-${account.account_no}`}" in account_row
    assert "testID={`account-rename-${account.account_no}`}" in account_row
    assert "onPress={() => setEditing(true)}\n        testID={`account-name-${account.account_no}`}" not in account_row
    assert not ACCOUNT_DETAIL_TSX.exists()


def test_card_row_body_deeplinks_to_existing_transactions_tab_not_rename():
    src = ACCOUNTS_TAB_TSX.read_text()
    card_row = src[src.index("function CardRow"):src.index("// ============================================================\n// Helpers")]

    assert "pathname: '/(tabs)/transactions'" in card_row
    assert "kind: 'all'" in card_row
    assert "card_no: card.card_no" in card_row
    assert "account_tail: card.card_no.slice(-4)" not in card_row
    assert "testID={`card-detail-${card.card_no}`}" in card_row
    assert "testID={`card-rename-${card.card_no}`}" in card_row
    assert "onPress={() => setEditing(true)}\n        testID={`card-name-${card.card_no}`}" not in card_row


def test_transactions_tab_accepts_exact_account_and_card_query_filter():
    layout = CARDS_LAYOUT_TSX.read_text()
    txns = TRANSACTIONS_TSX.read_text()

    assert "[bank]/accounts/[account_no]" not in layout
    assert "useLocalSearchParams<{ bank?: string; kind?: string; account_no?: string; card_no?: string; drilldown?: string }>" in txns
    assert "params.account_no" in txns
    assert "params.card_no" in txns
    assert "t.account_no === effectiveAccountNo" in txns
    assert "matchesCardDrilldown(t, effectiveCardNo)" in txns


def test_account_tab_drilldown_pushes_reset_nonce_even_for_same_row_repeat_taps():
    src = ACCOUNTS_TAB_TSX.read_text()
    account_row = src[src.index("function AccountRow"):src.index("function CardRow")]
    card_row = src[src.index("function CardRow"):src.index("// ============================================================\n// Helpers")]

    assert "drilldown: String(Date.now())" in account_row
    assert "drilldown: String(Date.now())" in card_row


def test_renamed_accounts_and_cards_do_not_show_leading_overwrite_pencil_badge():
    src = ACCOUNTS_TAB_TSX.read_text()
    account_row = src[src.index("function AccountRow"):src.index("function CardRow")]
    card_row = src[src.index("function CardRow"):src.index("// ============================================================\n// Helpers")]

    assert "const hasOverwrite" not in account_row
    assert "const hasOverwrite" not in card_row
    assert "{hasOverwrite &&" not in account_row
    assert "{hasOverwrite &&" not in card_row
    # Keep the explicit rename affordance on the row edge.
    assert "testID={`account-rename-${account.account_no}`}" in account_row
    assert "testID={`card-rename-${card.card_no}`}" in card_row
