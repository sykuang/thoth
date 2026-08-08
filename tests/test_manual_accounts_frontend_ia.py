from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "frontend/src/app/(tabs)/cards/index.tsx"
DETAIL = ROOT / "frontend/src/app/(tabs)/cards/manual/[account_id].tsx"
TYPES = ROOT / "frontend/src/types/api.ts"


def test_accounts_tab_exposes_manual_financial_accounts() -> None:
    source = INDEX.read_text()
    assert "/financial-accounts?source=manual" in source
    assert "manual/[account_id]" in source
    assert "add-manual-account" in source
    assert "Yahoo 市值" in source
    assert "Yahoo 查價失敗，顯示手動估值" in source


def test_manual_investment_page_has_derived_holdings_and_transaction_crud() -> None:
    source = DETAIL.read_text()
    assert "/holdings" in source
    assert "/transactions" in source
    assert "期初持股" in source
    assert 'label={kind === \'opening\' ? \'期初單位成本\' : \'成交單價\'}' in source
    assert "kind === 'fee' ? null : unitPrice.trim()" in source
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
    assert "已確認 {selectedSymbol.symbol} · Yahoo Finance" in source
    assert "queryKey: ['financial-accounts']" in source
    assert "queryKey: ['portfolio', 'summary']" in source


def test_manual_investment_contract_has_no_dividend_kind() -> None:
    source = TYPES.read_text()
    transaction = source[source.index("export type ManualInvestmentTransaction"):]
    transaction = transaction[:transaction.index("};")]
    assert "'opening' | 'buy' | 'sell' | 'fee'" in transaction
    assert "dividend" not in transaction
