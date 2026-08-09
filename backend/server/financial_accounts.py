"""Canonical financial-account read model and manual-account write store.

Provider stores remain authoritative. Only source='manual' is writable here.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from typing import Literal

from pydantic import BaseModel, ConfigDict

from backend.core import account_classify
from backend.server import db, financial_accounts_repo as repo, fx_service, yahoo_finance

AccountSource = Literal["manual", "bank_sync", "brokerage_sync"]
TransactionKind = Literal["opening", "buy", "sell", "fee"]
ValuationSource = Literal["manual", "yahoo_finance", "manual_fallback"]


_ALLOWED_PRODUCT_TYPES = (
    account_classify.ASSET_TYPES
    | account_classify.LIABILITY_TYPES
    | {account_classify.ProductType.INVESTMENT}
)


class FinancialAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    source: AccountSource
    source_ref: str
    institution_name: str | None
    name: str
    account_ref: str | None
    product_type: str
    currency: str
    balance: str | None
    manual_balance: str | None = None
    as_of: str | None
    valuation_source: ValuationSource | None = None
    included_in_net_worth: bool
    editable: bool
    deletable: bool


class InvestmentTransaction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    account_id: str
    kind: TransactionKind
    occurred_on: str
    symbol: str | None
    quantity: str | None
    amount: str
    currency: str
    note: str | None
    created_at: str
    updated_at: str


class InvestmentHolding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    quantity: str
    currency: str


class ManualAccountNotFound(LookupError):
    pass


class InvalidManualAccount(ValueError):
    pass


def _decimal(value: object) -> Decimal:
    raw = str(value).strip()
    whole, dot, fraction = raw.partition(".")
    if (
        not raw
        or len(raw) > 64
        or not whole.isdigit()
        or (bool(dot) and not fraction.isdigit())
        or len(whole.lstrip("0")) > 15
        or len(fraction) > 12
    ):
        raise InvalidManualAccount("invalid decimal value")
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise InvalidManualAccount("invalid decimal value") from exc
    if not parsed.is_finite():
        raise InvalidManualAccount("decimal value must be finite")
    if parsed != 0 and parsed.adjusted() > 14:
        raise InvalidManualAccount("decimal value is too large")
    exponent = parsed.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -12:
        raise InvalidManualAccount("decimal value has too many fractional digits")
    return parsed


def _decimal_text(value: object) -> str | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return format(parsed, "f") if parsed.is_finite() else None


def _manual_id(row_id: int) -> str:
    return f"manual:{row_id}"


def _row_value(row, index: int, key: str):
    """Portable access for SQLite tuples and dict-like PostgreSQL rows."""
    return row[key] if hasattr(row, "keys") else row[index]


def parse_manual_id(account_id: str) -> int:
    prefix, separator, raw_id = account_id.partition(":")
    if prefix != "manual" or separator != ":" or not raw_id.isdigit():
        raise ManualAccountNotFound(account_id)
    return int(raw_id)


def _normalize_account_values(
    *,
    product_type: str,
    name: str,
    currency: str,
    balance: object,
    included_in_net_worth: bool,
) -> dict[str, object]:
    product_type = product_type.strip().lower()
    if product_type not in _ALLOWED_PRODUCT_TYPES:
        raise InvalidManualAccount("unsupported product type")
    amount = _decimal(balance)
    if amount < 0:
        raise InvalidManualAccount("enter balance as a non-negative magnitude")
    if account_classify.is_liability_type(product_type):
        amount = -amount
    name = name.strip()
    currency = currency.strip().upper()
    if not name:
        raise InvalidManualAccount("name is required")
    if len(currency) != 3 or not currency.isalpha():
        raise InvalidManualAccount("currency must be a three-letter code")
    return {
        "product_type": product_type,
        "name": name,
        "currency": currency,
        "balance": format(amount, "f"),
        "included_in_net_worth": bool(included_in_net_worth),
    }


def _row_to_manual_account(row) -> FinancialAccount:
    return FinancialAccount(
        id=_manual_id(int(_row_value(row, 0, "id"))),
        source="manual",
        source_ref=str(_row_value(row, 0, "id")),
        institution_name=None,
        name=_row_value(row, 2, "name"),
        account_ref=None,
        product_type=_row_value(row, 1, "product_type"),
        currency=_row_value(row, 3, "currency"),
        balance=str(_row_value(row, 4, "balance")),
        manual_balance=str(_row_value(row, 4, "balance")),
        as_of=None,
        valuation_source="manual",
        included_in_net_worth=bool(_row_value(row, 5, "included_in_net_worth")),
        editable=True,
        deletable=True,
    )


def create_manual_account(user_id: int, **values) -> FinancialAccount:
    normalized = _normalize_account_values(**values)
    row_id = repo.insert_account(user_id, normalized, db.now_iso())
    return get_manual_account(user_id, _manual_id(row_id))


def get_manual_account(user_id: int, account_id: str) -> FinancialAccount:
    row_id = parse_manual_id(account_id)
    row = repo.get_account(user_id, row_id)
    if row is None:
        raise ManualAccountNotFound(account_id)
    return _row_to_manual_account(row)


def list_manual_accounts(user_id: int) -> list[FinancialAccount]:
    accounts: list[FinancialAccount] = []
    for row in repo.list_accounts(user_id):
        account = _row_to_manual_account(row)
        if account.product_type == account_classify.ProductType.INVESTMENT:
            balance, valuation_source, valuation_as_of = _investment_market_balance(
                user_id,
                int(_row_value(row, 0, "id")),
                account.currency,
                account.balance,
            )
            account = account.model_copy(update={
                "balance": balance,
                "valuation_source": valuation_source,
                "as_of": valuation_as_of,
            })
        accounts.append(account)
    return accounts


def _investment_market_balance(
    user_id: int,
    account_row_id: int,
    account_currency: str,
    fallback_balance: str | None,
) -> tuple[str | None, ValuationSource, str | None]:
    """Best-effort quote projection; ledger writes never depend on Yahoo."""
    try:
        totals = _holding_totals(user_id, account_row_id)
        if not totals:
            return fallback_balance, "manual", None
        market_value = Decimal(0)
        quote_times: list[int] = []
        for (symbol, ledger_currency), quantity in totals.items():
            if quantity == 0:
                continue
            quote = yahoo_finance.get_quote(symbol)
            if quote.currency != ledger_currency:
                return fallback_balance, "manual_fallback", None
            if quote.regular_market_time is not None:
                quote_times.append(quote.regular_market_time)
            value = quantity * Decimal(quote.regular_market_price)
            if quote.currency == account_currency:
                market_value += value
            elif account_currency == "TWD":
                converted = fx_service.convert_to_twd(format(value, "f"), quote.currency)
                if converted is None:
                    return fallback_balance, "manual_fallback", None
                market_value += Decimal(converted)
            else:
                return fallback_balance, "manual_fallback", None
        valuation_as_of = (
            datetime.fromtimestamp(min(quote_times), UTC).isoformat()
            if quote_times else None
        )
        return format(market_value, "f"), "yahoo_finance", valuation_as_of
    except (
        InvalidManualAccount,
        InvalidOperation,
        OSError,
        OverflowError,
        yahoo_finance.YahooFinanceUnavailable,
    ):
        return fallback_balance, "manual_fallback", None


def update_manual_account(user_id: int, account_id: str, **values) -> FinancialAccount:
    row_id = parse_manual_id(account_id)
    normalized = _normalize_account_values(**values)
    outcome = repo.update_account(user_id, row_id, normalized, db.now_iso())
    if outcome == "has_transactions":
        raise InvalidManualAccount("delete investment transactions before changing account type")
    if outcome == "not_found":
        raise ManualAccountNotFound(account_id)
    return get_manual_account(user_id, account_id)


def update_manual_account_inclusion(
    user_id: int,
    account_id: str,
    included_in_net_worth: bool,
) -> FinancialAccount:
    row_id = parse_manual_id(account_id)
    if not repo.update_account_inclusion(
        user_id,
        row_id,
        included_in_net_worth,
        db.now_iso(),
    ):
        raise ManualAccountNotFound(account_id)
    return get_manual_account(user_id, account_id)


def delete_manual_account(user_id: int, account_id: str) -> None:
    row_id = parse_manual_id(account_id)
    if not repo.delete_account(user_id, row_id):
        raise ManualAccountNotFound(account_id)


def _bank_accounts(user_id: int) -> list[FinancialAccount]:
    # Lazy import avoids coupling portfolio aggregation back into this module at import time.
    from backend.server.routers.portfolio import KNOWN_BANKS, _bank_accounts as read_bank_accounts

    result: list[FinancialAccount] = []
    for bank in KNOWN_BANKS:
        try:
            accounts = read_bank_accounts(bank, user_id)
        except Exception:
            continue
        for account in accounts:
            label = account.nickname_overwrite or account.nickname or account.account_no
            result.append(FinancialAccount(
                id=f"bank_sync:{bank}:{account.account_no}",
                source="bank_sync",
                source_ref=f"{bank}:{account.account_no}",
                institution_name=bank,
                name=label,
                account_ref=account.account_no,
                product_type=account.product_type or account_classify.ProductType.UNKNOWN,
                currency=account.currency,
                balance=_decimal_text(account.balance) if account.balance is not None else None,
                as_of=account.snapshot_date,
                included_in_net_worth=not account.excluded,
                editable=False,
                deletable=False,
            ))
    return result


def _brokerage_accounts(user_id: int) -> list[FinancialAccount]:
    snapshot = db.snaptrade_snapshot(user_id)
    result: list[FinancialAccount] = []
    for account in snapshot["accounts"]:
        provider_id = str(account["id"])
        result.append(FinancialAccount(
            id=f"brokerage_sync:snaptrade:{provider_id}",
            source="brokerage_sync",
            source_ref=f"snaptrade:{provider_id}",
            institution_name=account["institution_name"],
            name=account["name"],
            account_ref=account.get("number"),
            product_type=account_classify.ProductType.INVESTMENT,
            currency=(account.get("balance_currency") or "TWD").upper(),
            balance=account.get("balance_total"),
            as_of=account.get("synced_at"),
            included_in_net_worth=True,
            editable=False,
            deletable=False,
        ))
    return result


def list_financial_accounts(user_id: int) -> list[FinancialAccount]:
    """Return one canonical read model across all current account sources."""
    result = list_manual_accounts(user_id)
    result.extend(_bank_accounts(user_id))
    try:
        result.extend(_brokerage_accounts(user_id))
    except Exception:
        pass
    return result


def _require_investment_account(user_id: int, account_id: str) -> int:
    account = get_manual_account(user_id, account_id)
    if account.product_type != account_classify.ProductType.INVESTMENT:
        raise InvalidManualAccount("transactions are supported only for manual investment accounts")
    return parse_manual_id(account_id)


def _normalize_transaction_values(
    *,
    kind: str,
    occurred_on: str,
    currency: str,
    symbol: str | None = None,
    quantity: object | None = None,
    amount: object | None = None,
    note: str | None = None,
) -> dict[str, str | None]:
    kind = kind.strip().lower()
    if kind not in {"opening", "buy", "sell", "fee"}:
        raise InvalidManualAccount("unsupported transaction kind")
    currency = currency.strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise InvalidManualAccount("currency must be a three-letter code")
    if kind in {"opening", "buy", "sell"}:
        symbol = (symbol or "").strip().upper()
        if not symbol:
            raise InvalidManualAccount("symbol is required for position transactions")
        parsed_quantity = _decimal(quantity)
        if parsed_quantity <= 0:
            raise InvalidManualAccount("quantity must be positive")
        quantity_text = format(parsed_quantity, "f")
        parsed_amount = _decimal(amount)
        if parsed_amount < 0:
            raise InvalidManualAccount("total cost must be non-negative")
        if kind == "opening" and parsed_amount == 0:
            raise InvalidManualAccount("opening cost must be positive")
        amount_text = format(parsed_amount, "f")
    else:
        parsed_amount = _decimal(amount)
        if parsed_amount <= 0:
            raise InvalidManualAccount("fee amount must be positive")
        symbol = None
        quantity_text = None
        amount_text = format(parsed_amount, "f")
    return {
        "kind": kind,
        "occurred_on": occurred_on,
        "symbol": symbol,
        "quantity": quantity_text,
        "amount": amount_text,
        "currency": currency,
        "note": note.strip() if note and note.strip() else None,
    }


def _row_to_transaction(row) -> InvestmentTransaction:
    return InvestmentTransaction(
        id=int(_row_value(row, 0, "id")),
        account_id=_manual_id(int(_row_value(row, 1, "account_id"))),
        kind=_row_value(row, 2, "kind"),
        occurred_on=_row_value(row, 3, "occurred_on"),
        symbol=_row_value(row, 4, "symbol"),
        quantity=_row_value(row, 5, "quantity"),
        amount=_row_value(row, 6, "amount"),
        currency=_row_value(row, 7, "currency"),
        note=_row_value(row, 8, "note"),
        created_at=_row_value(row, 9, "created_at"),
        updated_at=_row_value(row, 10, "updated_at"),
    )


def _holding_totals(
    user_id: int,
    account_row_id: int,
    *,
    exclude_transaction_id: int | None = None,
    replacement: dict[str, str | None] | None = None,
    rows=None,
) -> dict[tuple[str, str], Decimal]:
    if rows is None:
        rows = repo.list_holding_rows(user_id, account_row_id)
    entries: list[tuple[str, int, str, str | None, str | None, str]] = []
    max_id = 0
    for row in rows:
        transaction_id = int(_row_value(row, 0, "id"))
        max_id = max(max_id, transaction_id)
        if exclude_transaction_id == transaction_id:
            continue
        entries.append((
            _row_value(row, 1, "occurred_on"),
            transaction_id,
            _row_value(row, 2, "kind"),
            _row_value(row, 3, "symbol"),
            _row_value(row, 4, "quantity"),
            _row_value(row, 5, "currency"),
        ))
    if replacement is not None:
        entries.append((
            str(replacement["occurred_on"]),
            exclude_transaction_id if exclude_transaction_id is not None else max_id + 1,
            str(replacement["kind"]),
            replacement["symbol"],
            replacement["quantity"],
            str(replacement["currency"]),
        ))

    totals: dict[tuple[str, str], Decimal] = {}
    for _, _, kind, symbol, quantity, currency in sorted(entries):
        if kind == "fee" or not symbol or quantity is None:
            continue
        key = (symbol, currency)
        signed = _decimal(quantity) * (-1 if kind == "sell" else 1)
        totals[key] = totals.get(key, Decimal(0)) + signed
        if totals[key] < 0:
            raise InvalidManualAccount("sell quantity exceeds recorded holdings on trade date")
    return totals


def list_investment_holdings(user_id: int, account_id: str) -> list[InvestmentHolding]:
    row_id = _require_investment_account(user_id, account_id)
    totals = _holding_totals(user_id, row_id)
    return [
        InvestmentHolding(symbol=symbol, quantity=format(quantity, "f"), currency=currency)
        for (symbol, currency), quantity in sorted(totals.items())
        if quantity != 0
    ]


def create_investment_transaction(user_id: int, account_id: str, **values) -> InvestmentTransaction:
    row_id = parse_manual_id(account_id)
    normalized = _normalize_transaction_values(**values)
    transaction_id = repo.mutate_transaction(
        user_id,
        row_id,
        "insert",
        normalized,
        None,
        db.now_iso(),
        lambda rows: _holding_totals(user_id, row_id, replacement=normalized, rows=rows),
    )
    if transaction_id is None:
        _require_investment_account(user_id, account_id)
        raise RuntimeError("manual transaction insert returned no id")
    return get_investment_transaction(user_id, account_id, int(transaction_id))


def get_investment_transaction(
    user_id: int,
    account_id: str,
    transaction_id: int,
) -> InvestmentTransaction:
    row_id = _require_investment_account(user_id, account_id)
    row = repo.get_transaction(user_id, row_id, transaction_id)
    if row is None:
        raise ManualAccountNotFound(str(transaction_id))
    return _row_to_transaction(row)


def list_investment_transactions(user_id: int, account_id: str) -> list[InvestmentTransaction]:
    row_id = _require_investment_account(user_id, account_id)
    return [_row_to_transaction(row) for row in repo.list_transactions(user_id, row_id)]


def update_investment_transaction(
    user_id: int,
    account_id: str,
    transaction_id: int,
    **values,
) -> InvestmentTransaction:
    row_id = parse_manual_id(account_id)
    normalized = _normalize_transaction_values(**values)
    updated = repo.mutate_transaction(
        user_id,
        row_id,
        "update",
        normalized,
        transaction_id,
        db.now_iso(),
        lambda rows: _holding_totals(
            user_id,
            row_id,
            exclude_transaction_id=transaction_id,
            replacement=normalized,
            rows=rows,
        ),
    )
    if not updated:
        raise ManualAccountNotFound(str(transaction_id))
    return get_investment_transaction(user_id, account_id, transaction_id)


def delete_investment_transaction(user_id: int, account_id: str, transaction_id: int) -> None:
    row_id = parse_manual_id(account_id)
    deleted = repo.mutate_transaction(
        user_id,
        row_id,
        "delete",
        None,
        transaction_id,
        db.now_iso(),
        lambda rows: _holding_totals(
            user_id,
            row_id,
            exclude_transaction_id=transaction_id,
            rows=rows,
        ),
    )
    if not deleted:
        raise ManualAccountNotFound(str(transaction_id))
