from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TABS_LAYOUT = ROOT / "frontend/src/app/(tabs)/_layout.tsx"
SETTINGS = ROOT / "frontend/src/app/(tabs)/settings/index.tsx"
ACCOUNTS = ROOT / "frontend/src/app/(tabs)/cards/index.tsx"
CALLBACK = ROOT / "frontend/src/app/(tabs)/investments.tsx"
TRANSACTIONS = ROOT / "frontend/src/app/(tabs)/transactions.tsx"
SECTIONS = ROOT / "frontend/src/components/SnapTradeSections.tsx"


def test_snaptrade_connection_and_accounts_live_on_canonical_surfaces():
    layout = TABS_LAYOUT.read_text()
    settings = SETTINGS.read_text()
    accounts = ACCOUNTS.read_text()
    callback = CALLBACK.read_text()
    transactions = TRANSACTIONS.read_text()
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
    assert "export function SnapTradeActivitiesSection" in sections
    accounts_section = sections[
        sections.index("export function SnapTradeAccountsSection"):
        sections.index("function ActionButton")
    ]
    assert "portfolio.activities" not in accounts_section
    assert "<SnapTradeActivitiesSection" in transactions
    assert "brokerage_account_id" in transactions
    assert "enabled: (statusQuery.data?.connection_count ?? 0) > 0" not in sections
    assert "if (!status?.connection_count) return null" not in sections
    assert "交易資料更新至" in sections
    assert "帳戶總覽顯示於「帳戶」；交易明細顯示於「交易」" in sections
    assert "交易明細顯示於「明細」" not in sections
    assert "資料顯示於「帳戶」" not in sections
    assert "上次同步：" not in sections


def test_brokerage_account_row_deeplinks_to_existing_transactions_tab():
    sections = SECTIONS.read_text()
    transactions = TRANSACTIONS.read_text()
    account_card = sections[sections.index("function AccountCard"):sections.index("function Activities")]

    assert "pathname: '/(tabs)/transactions'" in sections
    assert "brokerage_account_id: account.id" in sections
    assert "drilldown: String(Date.now())" in sections
    assert "testID={`brokerage-account-detail-${account.id}`}" in account_card
    assert "onPress={() => router.replace('/(tabs)/transactions')}" in transactions
    assert 'accessibilityLabel="顯示全部交易明細"' in transactions


def test_brokerage_detail_reports_query_errors_and_unsupported_snapshots():
    sections = SECTIONS.read_text()

    assert "if (!accountId) return null" not in sections
    assert "if (accountId && account?.activities_supported === false)" in sections
    assert "此帳戶目前未提供交易明細" in sections
