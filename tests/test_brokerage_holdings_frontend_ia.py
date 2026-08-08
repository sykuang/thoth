from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DETAIL = ROOT / "frontend/src/app/(tabs)/cards/brokerage/[account_id].tsx"


def test_brokerage_holdings_has_accounts_navigation_fallback() -> None:
    source = DETAIL.read_text()
    assert "router.dismissTo('/(tabs)/cards')" in source
    assert "headerBackVisible: false" in source
    assert "const backHeader" in source
    assert "{backHeader}" in source
