from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator

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
    list_financial_accounts,
    list_investment_holdings,
    list_investment_transactions,
    list_manual_accounts,
    update_investment_transaction,
    update_manual_account,
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
    if not parsed.is_finite() or parsed < 0 or (not allow_zero and parsed == 0):
        raise ValueError("must be a finite non-negative decimal")
    return raw


class ManualAccountPayload(BaseModel):
    product_type: ProductType
    institution_name: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=120)
    account_ref: str | None = Field(default=None, max_length=64)
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")
    balance: str
    as_of: date
    included_in_net_worth: bool = True

    @field_validator("balance")
    @classmethod
    def validate_balance(cls, value: str) -> str:
        return _decimal_text(value)

    def domain_values(self) -> dict:
        values = self.model_dump()
        values["as_of"] = self.as_of.isoformat()
        return values


class InvestmentTransactionPayload(BaseModel):
    kind: Literal["opening", "buy", "sell", "fee"]
    occurred_on: date
    symbol: str | None = Field(default=None, max_length=32)
    quantity: str | None = None
    unit_price: str | None = None
    amount: str | None = None
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_kind_fields(self):
        if self.kind in {"opening", "buy", "sell"}:
            if not self.symbol or self.quantity is None:
                raise ValueError("position transactions require symbol and quantity")
            self.quantity = _decimal_text(self.quantity, allow_zero=False)
            if self.kind != "opening" and self.unit_price is None:
                raise ValueError("buy/sell require unit_price")
            if self.unit_price is not None:
                self.unit_price = _decimal_text(self.unit_price)
        else:
            if self.amount is None:
                raise ValueError("fee requires amount")
            self.amount = _decimal_text(self.amount, allow_zero=False)
        return self

    def domain_values(self) -> dict:
        values = self.model_dump()
        values["occurred_on"] = self.occurred_on.isoformat()
        return values


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


@router.post("", status_code=status.HTTP_201_CREATED, response_model=FinancialAccount)
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


@router.patch("/{account_id}", response_model=FinancialAccount)
def update_account(
    account_id: str,
    body: ManualAccountPayload,
    user: dict = Depends(current_user),
) -> FinancialAccount:
    try:
        account = update_manual_account(user["id"], account_id, **body.domain_values())
    except ManualAccountNotFound as exc:
        raise _not_found(exc) from exc
    except InvalidManualAccount as exc:
        raise _invalid(exc) from exc
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
        return create_investment_transaction(user["id"], account_id, **body.domain_values())
    except ManualAccountNotFound as exc:
        raise _not_found(exc) from exc
    except InvalidManualAccount as exc:
        raise _invalid(exc) from exc


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
        return update_investment_transaction(
            user["id"], account_id, transaction_id, **body.domain_values(),
        )
    except ManualAccountNotFound as exc:
        raise _not_found(exc) from exc
    except InvalidManualAccount as exc:
        raise _invalid(exc) from exc


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
