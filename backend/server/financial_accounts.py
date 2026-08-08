"""Canonical financial-account read model and manual-account write store.

Provider stores remain authoritative. Only source='manual' is writable here.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from typing import Literal

from pydantic import BaseModel, ConfigDict

from backend.core import account_classify
from backend.server import db, financial_accounts_repo as repo

AccountSource = Literal["manual", "bank_sync", "brokerage_sync"]
TransactionKind = Literal["opening", "buy", "sell", "fee"]


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
    institution_name: str
    name: str
    account_ref: str | None
    product_type: str
    currency: str
    balance: str | None
    as_of: str | None
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
    unit_price: str | None
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


def _decimal_text(value: object) -> str:
    return format(_decimal(value), "f")


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
    institution_name: str,
    name: str,
    account_ref: str | None,
    currency: str,
    balance: object,
    as_of: str,
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
    institution_name = institution_name.strip()
    name = name.strip()
    currency = currency.strip().upper()
    if not institution_name or not name:
        raise InvalidManualAccount("institution and name are required")
    if len(currency) != 3 or not currency.isalpha():
        raise InvalidManualAccount("currency must be a three-letter code")
    return {
        "product_type": product_type,
        "institution_name": institution_name,
        "name": name,
        "account_ref": account_ref.strip() if account_ref and account_ref.strip() else None,
        "currency": currency,
        "balance": format(amount, "f"),
        "as_of": as_of,
        "included_in_net_worth": bool(included_in_net_worth),
    }


def _row_to_manual_account(row) -> FinancialAccount:
    return FinancialAccount(
        id=_manual_id(int(_row_value(row, 0, "id"))),
        source="manual",
        source_ref=str(_row_value(row, 0, "id")),
        institution_name=_row_value(row, 2, "institution_name"),
        name=_row_value(row, 3, "name"),
        account_ref=_row_value(row, 4, "account_ref"),
        product_type=_row_value(row, 1, "product_type"),
        currency=_row_value(row, 5, "currency"),
        balance=_row_value(row, 6, "balance"),
        as_of=_row_value(row, 7, "as_of"),
        included_in_net_worth=bool(_row_value(row, 8, "included_in_net_worth")),
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
    return [_row_to_manual_account(row) for row in repo.list_accounts(user_id)]


def update_manual_account(user_id: int, account_id: str, **values) -> FinancialAccount:
    row_id = parse_manual_id(account_id)
    normalized = _normalize_account_values(**values)
    outcome = repo.update_account(user_id, row_id, normalized, db.now_iso())
    if outcome == "has_transactions":
        raise InvalidManualAccount("delete investment transactions before changing account type")
    if outcome == "not_found":
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
    unit_price: object | None = None,
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
        if kind == "opening" and unit_price is None:
            price_text = None
            amount_text = "0"
        else:
            parsed_price = _decimal(unit_price)
            if parsed_price < 0:
                raise InvalidManualAccount("unit price must be non-negative")
            price_text = format(parsed_price, "f")
            amount_text = format(parsed_quantity * parsed_price, "f")
    else:
        parsed_amount = _decimal(amount)
        if parsed_amount <= 0:
            raise InvalidManualAccount("fee amount must be positive")
        symbol = None
        quantity_text = None
        price_text = None
        amount_text = format(parsed_amount, "f")
    return {
        "kind": kind,
        "occurred_on": occurred_on,
        "symbol": symbol,
        "quantity": quantity_text,
        "unit_price": price_text,
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
        unit_price=_row_value(row, 6, "unit_price"),
        amount=_row_value(row, 7, "amount"),
        currency=_row_value(row, 8, "currency"),
        note=_row_value(row, 9, "note"),
        created_at=_row_value(row, 10, "created_at"),
        updated_at=_row_value(row, 11, "updated_at"),
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
