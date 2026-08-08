# Thoth Domain Context

## Glossary

### Bank Connection
A user's configured login relationship with a supported bank. It may contain credentials and can synchronize zero or more financial accounts and cards. A bank connection is not itself a financial account.

### Financial Account
A user-owned store of financial value or obligation with a stable identity, name, product type, currency, and current valuation. Synchronized accounts may additionally supply an institution, masked account reference, and provider valuation timestamp; manual accounts intentionally do not store those source-specific fields.

### Synchronized Account
A financial account whose authoritative current valuation is supplied by a bank crawler or brokerage provider. Provider data is read-only in Thoth except for user-owned display metadata and inclusion preferences.

### Manual Account
A financial account whose identity and classification are maintained by the user. Its stored valuation is user-maintained; an investment account may expose a Yahoo Finance quote-derived valuation when every non-zero holding can be priced in a compatible currency. Quote failure falls back to the stored valuation with explicit `manual_fallback` provenance. Only manual accounts may be created, fully edited, or deleted through the financial-account interface.

### Product Type
The business classification of a financial account: deposit, time deposit, foreign-currency deposit, checking, loan, mortgage, credit line, or investment. Product type determines whether a valuation contributes to assets, liabilities, or investments.

### Current Valuation
The authoritative value of a financial account in its native currency at an as-of date. It is not a transaction ledger, cost basis, or position lot.

### Investment Transaction
A manually recorded opening position, buy, sell, or fee in an investment account. Opening and buy/sell unit prices are historical cost/execution facts and never current quotes. Symbols can be canonicalized through Yahoo Finance search, but ledger mutation does not depend on Yahoo availability. Dividend transactions are outside the current domain.

### Investment Holding
The current canonical-symbol-level quantity derived from opening, buy, and sell transactions. A holding is a read model, not an independently editable source of truth. Its quote-derived market value is `quantity × Yahoo regularMarketPrice`; a multi-holding account never publishes a partial quoted total when any required quote or currency conversion is unavailable.

### Provenance
The declared origin of a financial account and its valuation. Thoth never infers that two accounts from different sources are identical from names or masked account numbers alone.

### Net-worth Inclusion
A user-controlled decision that allows an account's current valuation to contribute to net-worth aggregation. Exclusion does not delete the account or its historical source data.
