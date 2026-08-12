from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TABS_LAYOUT = ROOT / "frontend/src/app/(tabs)/_layout.tsx"
SETTINGS = ROOT / "frontend/src/app/(tabs)/settings/index.tsx"
ACCOUNTS = ROOT / "frontend/src/app/(tabs)/cards/index.tsx"
CALLBACK = ROOT / "frontend/src/app/(tabs)/investments.tsx"
TRANSACTIONS = ROOT / "frontend/src/app/(tabs)/transactions.tsx"
SECTIONS = ROOT / "frontend/src/components/SnapTradeSections.tsx"
BROKERAGE_DETAIL = ROOT / "frontend/src/app/(tabs)/cards/brokerage/[account_id].tsx"
BROKERAGE_TXN_ROW = ROOT / "frontend/src/components/transactions/BrokerageTxnRow.tsx"
DASHBOARD = ROOT / "frontend/src/app/(tabs)/dashboard.tsx"
API_TYPES = ROOT / "frontend/src/types/api.ts"
CURRENCY = ROOT / "frontend/src/lib/currency.ts"


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
    assert "export function SnapTradeActivitiesSection" not in sections
    accounts_section = sections[
        sections.index("export function SnapTradeAccountsSection"):
        sections.index("function ActionButton")
    ]
    assert "portfolio.activities" not in accounts_section
    assert "SnapTradeActivitiesSection" not in transactions
    assert "mergeTransactionTimeline" in transactions
    assert "BrokerageTxnRow" in transactions
    assert "(activity.trade_date ?? activity.settlement_date ?? '').slice(0, 10)" in transactions
    assert "enabled: (statusQuery.data?.connection_count ?? 0) > 0" not in sections
    assert "if (!status?.connection_count) return null" not in sections
    assert "持股資料更新至" in sections
    assert "交易資料更新至" not in sections
    assert "帳戶總覽顯示於「帳戶」；交易明細顯示於「交易」" in sections
    assert "交易明細顯示於「明細」" not in sections
    assert "資料顯示於「帳戶」" not in sections
    assert "上次同步：" not in sections


def test_brokerage_account_row_opens_holdings_detail_not_transactions():
    sections = SECTIONS.read_text()
    transactions = TRANSACTIONS.read_text()
    assert BROKERAGE_DETAIL.exists()
    brokerage_detail = BROKERAGE_DETAIL.read_text()
    accounts_section = sections[
        sections.index("export function SnapTradeAccountsSection"):
        sections.index("function ActionButton")
    ]
    account_card = sections[
        sections.index("function AccountCard"):
        sections.index("export function SnapTradeHoldingsSection")
    ]

    assert "pathname: '/(tabs)/cards/brokerage/[account_id]'" in accounts_section
    assert "account_id: account.id" in accounts_section
    assert "pathname: '/(tabs)/transactions'" not in accounts_section
    assert "testID={`brokerage-account-detail-${account.id}`}" in account_card
    assert "account.balance_total" in account_card
    assert "positions.map" not in account_card
    assert "balances.map" not in account_card
    assert "查看 ${accountLabel(account)} 持股明細" in account_card
    assert "<SnapTradeHoldingsSection accountId={accountId} />" in brokerage_detail
    assert "<SnapTradeHoldingsSection" not in transactions
    assert "SnapTradeActivitiesSection" not in transactions
    assert "brokerage_account_id" not in transactions

    holdings_section = sections[sections.index("export function SnapTradeHoldingsSection"):]
    assert "持股資料更新至：{account.synced_at}" in holdings_section
    assert "transactions_last_successful_sync" not in holdings_section
    assert "transactions_first_transaction_date" not in holdings_section


def test_brokerage_detail_reports_query_errors_without_global_unsupported_banner():
    sections = SECTIONS.read_text()
    transactions = TRANSACTIONS.read_text()

    assert "if (!accountId) return null" not in sections
    assert "部分券商帳戶目前未提供交易明細" not in transactions
    assert "券商交易讀取失敗" in transactions


def test_bank_scoped_transactions_ignore_unrelated_brokerage_query_states():
    transactions = TRANSACTIONS.read_text()

    assert "const brokerageScopeActive = selectedBanks.length === 0 && !effectiveAccountNo && !effectiveCardNo;" in transactions
    assert "brokerageScopeActive && brokerageQ.isError" in transactions
    assert "brokerageScopeActive && brokerageQ.isLoading" in transactions
    assert "enabled: brokerageScopeActive" in transactions
    assert "const activeBrokeragePortfolio = brokerageScopeActive ? brokerageQ.data : undefined;" in transactions
    assert "const brokerageAccountCount = activeBrokeragePortfolio?.accounts.length ?? 0;" in transactions


def test_brokerage_amounts_use_shared_currency_formatter():
    sections = SECTIONS.read_text()
    transaction_row = BROKERAGE_TXN_ROW.read_text()
    currency = CURRENCY.read_text()

    expected = "return formatDecimalCurrency(value, currency ?? '') ?? '—';"
    assert expected in sections
    assert expected in transaction_row
    assert "USD: 2" in currency


def test_brokerage_desktop_transaction_date_stays_in_fixed_width_column():
    row = BROKERAGE_TXN_ROW.read_text()

    assert "const displayDate = date?.slice(0, 10) ?? '—';" in row
    assert "{displayDate}" in row


def test_account_snapshot_does_not_report_unrelated_live_status_error():
    sections = SECTIONS.read_text()
    accounts_section = sections[
        sections.index("export function SnapTradeAccountsSection"):
        sections.index("function ActionButton")
    ]

    assert "sync.error ?? portfolioQuery.error ?? (!hasSnapshot ? statusQuery.error : null)" in accounts_section


def test_accounts_tab_uses_plain_scroll_view_without_keyboard_insets():
    accounts = ACCOUNTS.read_text()

    assert 'className="flex-1 bg-ink-50 dark:bg-ink-950"' in accounts
    assert 'contentInsetAdjustmentBehavior="automatic"' in accounts
    assert "contentContainerStyle={{ paddingBottom: 32 }}" in accounts
    assert '<KeyboardAwareScrollView className="flex-1 bg-ink-50 dark:bg-ink-950">' not in accounts


def test_brokerage_assets_prevent_dashboard_empty_state():
    dashboard = DASHBOARD.read_text()
    api_types = API_TYPES.read_text()

    assert "brokerage_assets_twd: number;" in api_types
    assert "portfolio.brokerage_assets_twd === 0" in dashboard
