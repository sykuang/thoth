# Thoth Domain Context

## Glossary

### Bank Connection
A user's configured login relationship with a supported bank. It may contain credentials and can synchronize zero or more financial accounts and cards. A bank connection is not itself a financial account.

### Financial Account
A user-owned store of financial value or obligation with a stable identity, product type, institution, currency, current valuation, and valuation date. A financial account may be synchronized from a provider or maintained manually.

### Synchronized Account
A financial account whose authoritative current valuation is supplied by a bank crawler or brokerage provider. Provider data is read-only in Thoth except for user-owned display metadata and inclusion preferences.

### Manual Account
A financial account whose identity, classification, and current valuation are maintained by the user. Only manual accounts may be created, fully edited, or deleted through the financial-account interface.

### Product Type
The business classification of a financial account: deposit, time deposit, foreign-currency deposit, checking, loan, mortgage, credit line, or investment. Product type determines whether a valuation contributes to assets, liabilities, or investments.

### Current Valuation
The authoritative value of a financial account in its native currency at an as-of date. It is not a transaction ledger, cost basis, or position lot.

### Investment Transaction
A manually recorded opening position, buy, sell, or fee in an investment account. Investment transactions preserve execution history but do not provide current market prices. Dividend transactions are outside the current domain.

### Investment Holding
The current symbol-level quantity derived from opening, buy, and sell transactions. A holding is a read model, not an independently editable source of truth.

### Provenance
The declared origin of a financial account and its valuation. Thoth never infers that two accounts from different sources are identical from names or masked account numbers alone.

### Net-worth Inclusion
A user-controlled decision that allows an account's current valuation to contribute to net-worth aggregation. Exclusion does not delete the account or its historical source data.
