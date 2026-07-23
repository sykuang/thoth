"""Regression: unsupported deposit transaction crawlers must explain empty account drilldown.

Some banks sync deposit account balances but do not yet write
`twd_transactions`. Tapping an account row then looks like a broken blank
transaction page unless the empty state says this bank lacks deposit txn sync.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRANSACTIONS_TSX = ROOT / "frontend/src/app/(tabs)/transactions.tsx"


def test_account_drilldown_empty_state_mentions_unsupported_twd_sync() -> None:
    src = TRANSACTIONS_TSX.read_text()

    assert "const TWD_TXN_UNSUPPORTED_BANKS: ReadonlySet<string> = new Set();" in src
    assert "const isUnsupportedAccountDrilldown = Boolean(" in src
    assert "effectiveAccountNo && selectedBanks.length === 1 && TWD_TXN_UNSUPPORTED_BANKS.has(selectedBanks[0])" in src
    assert "此銀行尚未支援存款交易明細同步" in src
    assert "尚未同步存款交易明細" in src
    assert "富邦存款交易明細 crawler 尚未實作" not in src
