from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "frontend/src/app/(tabs)/cards/index.tsx"
ACCOUNT_ADD = ROOT / "frontend/src/app/(tabs)/cards/add.tsx"
DETAIL = ROOT / "frontend/src/app/(tabs)/cards/manual/[account_id].tsx"
TYPES = ROOT / "frontend/src/types/api.ts"


def test_accounts_tab_exposes_manual_financial_accounts() -> None:
    source = INDEX.read_text()
    assert "/financial-accounts?source=manual" in source
    assert "manual/[account_id]" in source
    assert source.count('testID="add-account-btn"') == 1
    assert "add-manual-account" not in source
    assert "empty-add-account-btn" not in source
    assert "add-account-cta" not in source
    assert "Yahoo 市值" in source
    assert "Yahoo 查價失敗，顯示手動估值" in source

    add_source = ACCOUNT_ADD.read_text()
    assert "連結銀行帳號" in add_source
    assert "新增手動帳戶" in add_source
    assert "'/(tabs)/cards/new'" in add_source
    assert "'/(tabs)/cards/manual/new'" in add_source


def test_manual_investment_page_has_derived_holdings_and_transaction_crud() -> None:
    source = DETAIL.read_text()
    assert "/holdings" in source
    assert "/transactions" in source
    assert "期初持股" in source
    assert "成本輸入方式" in source
    assert "單位成本" in source
    assert "總成本" in source
    assert 'testID={`cost-mode-${value}`}' in source
    assert "unit_price" not in source
    assert "multiplyDecimal(quantity.trim(), unitPrice.trim(), 12)" in source
    assert "costInputMode === 'total'" in source
    assert "divideDecimal(row.amount, row.quantity, 2)" in source
    assert "買入" in source
    assert "賣出" in source
    assert "費用" in source
    assert "股息" not in source
    assert "toISOString().slice(0, 10)" not in source
    assert "date.getFullYear()" in source
    assert "accountsQ.isError" in source
    assert "holdingsQ.isError" in source
    assert "transactionsQ.isError" in source
    assert "confirmDeleteTrade" in source
    assert 'accessibilityRole="radio"' in source
    assert "accessibilityLabel={label}" in source
    assert 'accessibilityLabel="納入淨資產"' in source
    assert 'label="名稱"' in source
    assert "institution_name:" not in source
    assert "account_ref:" not in source
    assert "as_of:" not in source
    assert "manual-institution" not in source
    assert "帳號末碼" not in source
    assert "估值日期" not in source
    assert "router.dismissTo('/(tabs)/cards')" in source
    assert "router.replace({" not in source
    assert "useDebouncedValue(normalizedSymbol, 350)" in source
    assert "/financial-accounts/symbols/search?q=" in source
    assert "/financial-accounts/symbols/${encodeURIComponent(selectedSymbol!.symbol)}/quote" in source
    assert 'testID="symbol-search-results"' in source
    assert "testID={`symbol-option-${match.symbol}`}" in source
    assert 'testID="symbol-confirmation"' in source
    assert "已確認 {selectedSymbol.symbol} · Yahoo Finance" not in source
    assert "Yahoo 現價 {quoteQ.data.currency}" not in source
    assert "{selectedSymbol.symbol}" in source
    assert "{quoteQ.data.currency} {formatDecimalFixed(quoteQ.data.regular_market_price, 2)}" in source
    assert "queryKey: ['financial-accounts']" in source
    assert "queryKey: ['portfolio', 'summary']" in source
    assert "account.manual_balance ?? account.balance" in source
    assert "manual_balance: balance.trim()" in source
    assert "const ACCOUNT_CURRENCIES" in source
    assert 'label="幣別"' in source
    assert 'testID="manual-currency"' in source
    assert '<View className="w-28"><Field label="幣別"' not in source
    assert "!ACCOUNT_CURRENCIES.some((option) => option.value === normalizedCurrency)" in source


def test_manual_investment_contract_has_no_dividend_kind() -> None:
    source = TYPES.read_text()
    transaction = source[source.index("export type ManualInvestmentTransaction"):]
    transaction = transaction[:transaction.index("};")]
    assert "'opening' | 'buy' | 'sell' | 'fee'" in transaction
    assert "dividend" not in transaction
