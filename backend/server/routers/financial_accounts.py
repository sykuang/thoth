from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from backend.server import yahoo_finance
from backend.server.deps import current_user
from backend.server.financial_accounts import (
    FinancialAccount,
    InvalidManualAccount,
    InvestmentHolding,
    InvestmentTransaction,
    ManualAccountNotFound,
    create_investment_transaction,
    create_manual_account,
    delete_investment_transaction,
    delete_manual_account,
    get_manual_account,
    list_financial_accounts,
    list_investment_holdings,
    list_investment_transactions,
    list_manual_accounts,
    update_investment_transaction,
    update_manual_account,
    update_manual_account_inclusion,
)
from backend.server.routers.portfolio import clear_dashboard_cache

router = APIRouter(prefix="/financial-accounts", tags=["financial-accounts"])

ProductType = Literal[
    "deposit", "time_deposit", "fx_deposit", "checking",
    "loan", "mortgage", "credit_line", "investment",
]


def _decimal_text(value: str, *, allow_zero: bool = True) -> str:
    raw = value.strip()
    whole, dot, fraction = raw.partition(".")
    if (
        not raw
        or len(raw) > 64
        or not whole.isdigit()
        or (bool(dot) and not fraction.isdigit())
        or len(whole.lstrip("0")) > 15
        or len(fraction) > 12
    ):
        raise ValueError("must be a bounded fixed-point decimal string")
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("must be a decimal string") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("must be a finite non-negative decimal")
    if not allow_zero and parsed == 0:
        raise ValueError("must be a positive decimal")
    return raw


class ManualAccountPayload(BaseModel):
    product_type: ProductType
    name: str = Field(min_length=1, max_length=120)
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")
    balance: str
    manual_balance: str | None = None
    included_in_net_worth: bool = True

    @field_validator("balance")
    @classmethod
    def validate_balance(cls, value: str) -> str:
        return _decimal_text(value)

    @field_validator("manual_balance")
    @classmethod
    def validate_manual_balance(cls, value: str | None) -> str | None:
        return _decimal_text(value) if value is not None else None

    def domain_values(self) -> dict:
        return self.model_dump(exclude={"manual_balance"})


class ManualAccountInclusionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    included_in_net_worth: StrictBool


class InvestmentTransactionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["opening", "buy", "sell", "fee"]
    occurred_on: date
    symbol: str | None = Field(default=None, max_length=32)
    quantity: str | None = None
    amount: str | None = None
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_kind_fields(self):
        if self.kind in {"opening", "buy", "sell"}:
            if not self.symbol or self.quantity is None:
                raise ValueError("position transactions require symbol and quantity")
            self.quantity = _decimal_text(self.quantity, allow_zero=False)
            if self.amount is None:
                raise ValueError("position transactions require total amount")
            self.amount = _decimal_text(
                self.amount,
                allow_zero=self.kind != "opening",
            )
        else:
            if self.amount is None:
                raise ValueError("fee requires amount")
            self.amount = _decimal_text(self.amount, allow_zero=False)
        return self

    def domain_values(self) -> dict:
        values = self.model_dump()
        values["occurred_on"] = self.occurred_on.isoformat()
        return values


class YahooSymbolMatchResponse(BaseModel):
    symbol: str
    name: str
    exchange: str | None
    exchange_name: str | None
    quote_type: str


class YahooQuoteResponse(BaseModel):
    symbol: str
    name: str
    currency: str
    exchange_name: str | None
    quote_type: str | None
    regular_market_price: str
    regular_market_time: int | None


def _not_found(exc: ManualAccountNotFound) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "找不到此手動帳戶或交易")


def _invalid(exc: InvalidManualAccount) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


@router.get("", response_model=list[FinancialAccount])
def list_accounts(
    source: Literal["manual", "bank_sync", "brokerage_sync"] | None = Query(default=None),
    user: dict = Depends(current_user),
) -> list[FinancialAccount]:
    if source == "manual":
        return list_manual_accounts(user["id"])
    accounts = list_financial_accounts(user["id"])
    return accounts if source is None else [row for row in accounts if row.source == source]


@router.get("/symbols/search", response_model=list[YahooSymbolMatchResponse])
def search_symbols(
    q: str = Query(min_length=1, max_length=64),
    preferred_currency: str | None = Query(default=None, min_length=3, max_length=3),
    _user: dict = Depends(current_user),
) -> list[YahooSymbolMatchResponse]:
    try:
        rows = yahoo_finance.search_symbols(q, preferred_currency)
    except yahoo_finance.YahooFinanceUnavailable as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Yahoo Finance 暫時無法查詢代號",
        ) from exc
    return [YahooSymbolMatchResponse(**vars(row)) for row in rows]


@router.get("/symbols/{symbol}/quote", response_model=YahooQuoteResponse)
def get_symbol_quote(
    symbol: str,
    _user: dict = Depends(current_user),
) -> YahooQuoteResponse:
    try:
        row = yahoo_finance.get_quote(symbol)
    except yahoo_finance.YahooFinanceUnavailable as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Yahoo Finance 暫時無法取得現價",
        ) from exc
    return YahooQuoteResponse(**vars(row))


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=FinancialAccount,
)
def create_account(
    body: ManualAccountPayload,
    user: dict = Depends(current_user),
) -> FinancialAccount:
    try:
        account = create_manual_account(user["id"], **body.domain_values())
    except InvalidManualAccount as exc:
        raise _invalid(exc) from exc
    clear_dashboard_cache(user["id"])
    return account


@router.patch(
    "/{account_id}",
    response_model=FinancialAccount,
)
def update_account(
    account_id: str,
    body: ManualAccountPayload,
    user: dict = Depends(current_user),
) -> FinancialAccount:
    try:
        values = body.domain_values()
        existing = get_manual_account(user["id"], account_id)
        if existing.product_type == "investment":
            values["balance"] = body.manual_balance or (existing.balance or "0").lstrip("-")
        account = update_manual_account(user["id"], account_id, **values)
    except ManualAccountNotFound as exc:
        raise _not_found(exc) from exc
    except InvalidManualAccount as exc:
        raise _invalid(exc) from exc
    clear_dashboard_cache(user["id"])
    return account


@router.patch(
    "/{account_id}/included",
    response_model=FinancialAccount,
)
def update_account_inclusion(
    account_id: str,
    body: ManualAccountInclusionPayload,
    user: dict = Depends(current_user),
) -> FinancialAccount:
    try:
        account = update_manual_account_inclusion(
            user["id"], account_id, body.included_in_net_worth,
        )
    except ManualAccountNotFound as exc:
        raise _not_found(exc) from exc
    clear_dashboard_cache(user["id"])
    return account


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(account_id: str, user: dict = Depends(current_user)) -> None:
    try:
        delete_manual_account(user["id"], account_id)
    except ManualAccountNotFound as exc:
        raise _not_found(exc) from exc
    clear_dashboard_cache(user["id"])


@router.get("/{account_id}/transactions", response_model=list[InvestmentTransaction])
def list_transactions(
    account_id: str,
    user: dict = Depends(current_user),
) -> list[InvestmentTransaction]:
    try:
        return list_investment_transactions(user["id"], account_id)
    except ManualAccountNotFound as exc:
        raise _not_found(exc) from exc
    except InvalidManualAccount as exc:
        raise _invalid(exc) from exc


@router.get("/{account_id}/holdings", response_model=list[InvestmentHolding])
def list_holdings(
    account_id: str,
    user: dict = Depends(current_user),
) -> list[InvestmentHolding]:
    try:
        return list_investment_holdings(user["id"], account_id)
    except ManualAccountNotFound as exc:
        raise _not_found(exc) from exc
    except InvalidManualAccount as exc:
        raise _invalid(exc) from exc


@router.post(
    "/{account_id}/transactions",
    status_code=status.HTTP_201_CREATED,
    response_model=InvestmentTransaction,
)
def create_transaction(
    account_id: str,
    body: InvestmentTransactionPayload,
    user: dict = Depends(current_user),
) -> InvestmentTransaction:
    try:
        transaction = create_investment_transaction(user["id"], account_id, **body.domain_values())
    except ManualAccountNotFound as exc:
        raise _not_found(exc) from exc
    except InvalidManualAccount as exc:
        raise _invalid(exc) from exc
    clear_dashboard_cache(user["id"])
    return transaction


@router.patch(
    "/{account_id}/transactions/{transaction_id}",
    response_model=InvestmentTransaction,
)
def update_transaction(
    account_id: str,
    transaction_id: int,
    body: InvestmentTransactionPayload,
    user: dict = Depends(current_user),
) -> InvestmentTransaction:
    try:
        transaction = update_investment_transaction(
            user["id"], account_id, transaction_id, **body.domain_values(),
        )
    except ManualAccountNotFound as exc:
        raise _not_found(exc) from exc
    except InvalidManualAccount as exc:
        raise _invalid(exc) from exc
    clear_dashboard_cache(user["id"])
    return transaction


@router.delete(
    "/{account_id}/transactions/{transaction_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_transaction(
    account_id: str,
    transaction_id: int,
    user: dict = Depends(current_user),
) -> None:
    try:
        delete_investment_transaction(user["id"], account_id, transaction_id)
    except ManualAccountNotFound as exc:
        raise _not_found(exc) from exc
    except InvalidManualAccount as exc:
        raise _invalid(exc) from exc
    clear_dashboard_cache(user["id"])
