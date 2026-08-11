"""Bank account balance projection shared by portfolio and financial accounts."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel

from backend.core import account_classify
from backend.server import fx_service
from backend.server.db_facade import LatestBalance, db_api

ACCOUNT_STALE_DAYS = 7


class BankAccountBalance(BaseModel):
    bank: str
    account_no: str
    currency: str
    nickname: str | None
    nickname_overwrite: str | None = None
    product_type: str | None
    type: str | None
    balance: float | None
    snapshot_date: str | None
    is_stale: bool
    twd_estimate: int | None = None
    fx_rate_used: float | None = None
    excluded: bool = False


def _normalize_iso_date(raw: str | None) -> str | None:
    if not raw:
        return None
    head = str(raw).strip()[:10]
    for separator in ("-", "/"):
        try:
            datetime.strptime(head, f"%Y{separator}%m{separator}%d")
            return head.replace("/", "-")
        except ValueError:
            continue
    return None


def _is_stale(snapshot_iso: str | None) -> bool:
    if not snapshot_iso:
        return True
    try:
        value = datetime.strptime(snapshot_iso[:10], "%Y-%m-%d").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return True
    return datetime.now(UTC) - value > timedelta(days=ACCOUNT_STALE_DAYS)


def bank_accounts(
    bank: str,
    user_id: int,
    *,
    include_fx_estimates: bool = True,
) -> list[BankAccountBalance]:
    """Return one bank's accounts using the canonical balance precedence.

    Prefer the newer dated fact between the crawler account snapshot and the
    latest transaction balance. Same-day or incomparable facts keep the direct
    crawler snapshot, which avoids ambiguous same-timestamp transaction rows.
    """
    accounts = db_api.list_accounts(bank=bank, user_id=user_id)
    if not accounts:
        return []
    txn_balances = db_api.list_latest_account_txn_balances(bank=bank, user_id=user_id)
    loan = db_api.get_latest_loan_balance(bank=bank, user_id=user_id)
    loan_balance = loan.loan_balance if loan else None
    loan_date = _normalize_iso_date(loan.snapshot_date) if loan else None

    out: list[BankAccountBalance] = []
    for account in accounts:
        balance: float | None = None
        snapshot_date: str | None
        latest = txn_balances.get(account.account_no)
        raw_date = _normalize_iso_date(account.raw_balance_date)
        txn_date = _normalize_iso_date(latest.txn_datetime) if latest else None
        if (
            latest is not None
            and latest.balance is not None
            and raw_date is not None
            and txn_date is not None
            and txn_date > raw_date
        ):
            balance = latest.balance
            snapshot_date = txn_date
        elif account.raw_balance is not None:
            raw = account.raw_balance
            balance = raw if isinstance(raw, float) and raw != int(raw) else int(raw)
            snapshot_date = raw_date
        elif latest is not None:
            balance = latest.balance
            snapshot_date = txn_date
        elif account_classify.is_liability_type(account.product_type) and loan_balance is not None:
            balance = loan_balance
            snapshot_date = loan_date
        else:
            snapshot_date = _normalize_iso_date(account.updated_at)

        balance = account_classify.normalize_account_balance(account.product_type, balance)
        currency = (account.currency or "TWD").upper()
        twd_estimate: int | None = None
        fx_rate_used: float | None = None
        if balance is not None:
            if currency == "TWD":
                twd_estimate = round(balance)
                fx_rate_used = 1.0
            elif include_fx_estimates:
                try:
                    rate = fx_service.get_rate(currency)
                    if rate is not None:
                        twd_estimate = fx_service.convert_to_twd(balance, currency)
                        fx_rate_used = rate
                except Exception:
                    pass

        out.append(BankAccountBalance(
            bank=bank,
            account_no=account.account_no,
            currency=currency,
            nickname=account.nickname,
            nickname_overwrite=account.nickname_overwrite,
            product_type=account.product_type,
            type=account.type,
            balance=balance,
            snapshot_date=snapshot_date,
            is_stale=_is_stale(snapshot_date),
            twd_estimate=twd_estimate,
            fx_rate_used=fx_rate_used,
            excluded=account.excluded,
        ))
    return out


def latest_twd_asset_balance(bank: str, user_id: int) -> LatestBalance | None:
    """Choose the freshest complete TWD asset snapshot for one bank.

    Account rows are the direct crawler facts. Use their sum when every TWD
    asset account has a dated balance and that complete snapshot is newer than
    the bank-level ``balance_history`` aggregate. Otherwise preserve the
    same-date/newer aggregate because it can include accounts absent from the
    account inventory; never publish a partial account sum.
    """
    aggregate = db_api.get_latest_twd_balance(bank=bank, user_id=user_id)
    accounts = bank_accounts(bank, user_id, include_fx_estimates=False)
    twd_non_liabilities = [
        account for account in accounts
        if account.currency == "TWD"
        and not account_classify.is_liability_type(account.product_type)
    ]
    if not twd_non_liabilities:
        return aggregate
    account_dates: list[str] = []
    account_total = 0
    for account in twd_non_liabilities:
        if (
            not account_classify.is_asset_type(account.product_type)
            or account.twd_estimate is None
            or account.snapshot_date is None
        ):
            return aggregate
        account_dates.append(account.snapshot_date)
        account_total += account.twd_estimate

    account_date = min(account_dates)
    aggregate_date = _normalize_iso_date(aggregate.snapshot_date) if aggregate else None
    if aggregate is not None and aggregate_date is not None and aggregate_date >= account_date:
        return aggregate
    return LatestBalance(snapshot_date=account_date, twd_balance=account_total)
