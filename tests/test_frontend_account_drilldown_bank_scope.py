"""Regression: account drilldown must pass the account's bank.

Without the bank param, the transactions tab may query all banks with only
account_no. Some banks use normalized account numbers differently, and repeated
route-param updates can transiently drop the bank filter. Account body taps must
always deep-link with both bank and canonical account_no.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS_TAB_TSX = ROOT / "frontend/src/app/(tabs)/cards/index.tsx"
TRANSACTIONS_TSX = ROOT / "frontend/src/app/(tabs)/transactions.tsx"


def test_account_row_drilldown_passes_bank_and_account_no_together() -> None:
    src = ACCOUNTS_TAB_TSX.read_text()
    account_row = src[src.index("function AccountRow"):src.index("function CardRow")]

    assert "pathname: '/(tabs)/transactions'" in account_row
    assert "bank: account.bank" in account_row
    assert "kind: 'twd'" in account_row
    assert "account_no: account.account_no" in account_row


def test_transactions_local_filter_includes_bank_when_account_no_present() -> None:
    src = TRANSACTIONS_TSX.read_text()
    block = src[src.index("const rawItems = useMemo(() => {"):src.index("const transactionRefreshing =")]

    assert "if (effectiveAccountNo) items = items.filter((t) => t.account_no === effectiveAccountNo);" in block
    assert "if (selectedBanks.length > 0) items = items.filter((t) => selectedBanks.includes(t.bank));" in block


def test_account_drilldown_scope_is_clearable_local_state() -> None:
    src = TRANSACTIONS_TSX.read_text()

    assert "const [activeAccountNo, setActiveAccountNo] = useState(accountNo);" in src
    assert "const [activeCardNo, setActiveCardNo] = useState(cardNo);" in src
    assert "setActiveAccountNo(accountNo);" in src
    assert "setActiveCardNo(cardNo);" in src
    clear_block = src[src.index("function clearFilters() {"):src.index("// Phase 9 C-2", src.index("function clearFilters()"))]
    assert "setActiveAccountNo('');" in clear_block
    assert "setActiveCardNo('');" in clear_block


def test_manual_bank_chip_change_exits_account_drilldown_scope() -> None:
    src = TRANSACTIONS_TSX.read_text()
    toggle_block = src[src.index("function toggleBank(b: string) {"):src.index("function clearFilters() {")]

    assert "setActiveAccountNo('');" in toggle_block
    assert "setActiveCardNo('');" in toggle_block
