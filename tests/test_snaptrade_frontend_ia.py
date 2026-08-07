from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TABS_LAYOUT = ROOT / "frontend/src/app/(tabs)/_layout.tsx"
SETTINGS = ROOT / "frontend/src/app/(tabs)/settings/index.tsx"
ACCOUNTS = ROOT / "frontend/src/app/(tabs)/cards/index.tsx"
CALLBACK = ROOT / "frontend/src/app/(tabs)/investments.tsx"
SECTIONS = ROOT / "frontend/src/components/SnapTradeSections.tsx"


def test_snaptrade_connection_and_accounts_live_on_canonical_surfaces():
    layout = TABS_LAYOUT.read_text()
    settings = SETTINGS.read_text()
    accounts = ACCOUNTS.read_text()
    callback = CALLBACK.read_text()
    sections = SECTIONS.read_text() if SECTIONS.exists() else ""

    assert 'name="investments"' in layout
    assert "href: null" in layout
    assert "ChartCandlestick" not in layout
    assert "<SnapTradeConnectionSettings />" in settings
    assert "<SnapTradeAccountsSection />" in accounts
    assert 'Redirect href="/(tabs)/settings"' in callback
    assert "maybeCompleteAuthSession" in callback
    assert "export function SnapTradeConnectionSettings" in sections
    assert "export function SnapTradeAccountsSection" in sections
    assert "enabled: (statusQuery.data?.connection_count ?? 0) > 0" not in sections
    assert "if (!status?.connection_count) return null" not in sections
    assert "交易資料更新至" in sections
    assert "上次同步：" not in sections
