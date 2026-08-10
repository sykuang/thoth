"""db_facade.portfolio — portfolio aggregates domain (Plan B B4).

Covers per-bank aggregate tables (bank DB):
  balance_history     daily snapshot of (twd_balance, loan_balance)
  daily_metrics       per-category JSON payload snapshots
  twd_transactions    used for "latest per-account balance" query
  card_billed_txns/card_pending_txns  card amount sum for month spending

This module DOES NOT cover:
  - bank-level cards/accounts queries → see cards.py / accounts.py
  - txn-family CRUD → see transactions.py
  - server-DB user_preferences → out of scope (server DB, not bank DB).
    Handled by a Repo-style class on the server-DB side (PreferencesRepo,
    added alongside creds_store).

Pydantic models:
  LatestMetric        (snapshot_date, payload_dict) from daily_metrics
  LatestBalance       (snapshot_date, twd_balance) from balance_history
  LatestLoanBalance   (snapshot_date, loan_balance) from balance_history
  AccountTxnBalance   per-account latest txn balance (account_no, txn_datetime, balance)
  CardMonthAmountRow  one row of card amount for month spending agg

Mixins:
  PortfolioReadMixin  get_latest_metric, get_latest_twd_balance,
                      get_latest_loan_balance,
                      list_latest_account_txn_balances,
                      sum_card_pending_amounts_for_user,
                      sum_card_billed_amounts_for_month
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict

from backend.server import db

from ._base import _BaseHelpers


# ============================================================
# Pydantic models
# ============================================================


class LatestMetric(BaseModel):
    """One row of daily_metrics — snapshot_date + parsed payload JSON.

    `payload` is typed as Any because real-world bank payloads vary:
      - dict shapes (most banks: TotalData / latest_bill / total_consumption)
      - list shapes (hsbc card_summary = list of cards;
                     sinopac card_summary = list of cards)
    parse_payload() handles both transparently.
    """

    model_config = ConfigDict(extra="forbid")

    snapshot_date: str
    payload: Any


class LatestBalance(BaseModel):
    """balance_history.twd_balance latest entry."""

    model_config = ConfigDict(extra="forbid")

    snapshot_date: str
    twd_balance: int | None


class LatestLoanBalance(BaseModel):
    """balance_history.loan_balance latest entry."""

    model_config = ConfigDict(extra="forbid")

    snapshot_date: str
    loan_balance: int | None


class AccountTxnBalance(BaseModel):
    """Per-account latest txn balance (account_no level)."""

    model_config = ConfigDict(extra="forbid")

    account_no: str
    txn_datetime: str
    balance: int | None


class CardMonthAmountRow(BaseModel):
    """One row of card amount for month-spending aggregate.

    Comes from card_pending_txns / card_billed_txns. caller (router)
    filters by currency/excluded/auto_excluded and sums.
    """

    model_config = ConfigDict(extra="forbid")

    amount: float | int | None
    currency: str | None
    card_no: str | None
    consume_date: str | None = None
    txn_type: str | None = None
    flow_type: str | None = None
    splits_overwrite: str | None = None


# ============================================================
# Internal helpers
# ============================================================


def _to_int_safe(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _parse_payload(raw: Any) -> Any:
    """Parse daily_metrics.payload_json to Python (could be dict OR list).

    Real-world bank payloads vary:
      - dict shapes (cathay / ubot / ctbc)
      - list shapes (hsbc card_summary, sinopac card_summary)
    Return raw parsed value (no shape coercion) so parser fns can branch.
    """
    if raw is None or raw == "":
        return {}
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


# ============================================================
# PortfolioReadMixin
# ============================================================


class PortfolioReadMixin(_BaseHelpers):
    """Read-only portfolio aggregate methods (bank DB)."""

    def get_latest_metric(
        self,
        *,
        bank: str,
        category: str,
        user_id: int,
    ) -> LatestMetric | None:
        """daily_metrics 最新一筆 (該 user, 該 category).

        Bank db 不存在 / 表不存在 / 沒 row → None.
        """
        con = db.open_bank_conn(bank)
        if con is None:
            return None
        try:
            try:
                row = con.execute(
                    """SELECT snapshot_date, payload_json
                       FROM daily_metrics
                       WHERE user_id = ? AND category = ?
                       ORDER BY snapshot_date DESC LIMIT 1""",
                    (user_id, category),
                ).fetchone()
            except db.OperationalError:
                return None
            if not row:
                return None
            return LatestMetric(
                snapshot_date=row["snapshot_date"],
                payload=_parse_payload(row["payload_json"]),
            )
        finally:
            con.close()

    def get_latest_twd_balance(
        self,
        *,
        bank: str,
        user_id: int,
    ) -> LatestBalance | None:
        """balance_history.twd_balance 最新一筆.

        Bank db 不存在 / 表不存在 / 沒 row → None.
        """
        con = db.open_bank_conn(bank)
        if con is None:
            return None
        try:
            try:
                row = con.execute(
                    """SELECT snapshot_date, twd_balance FROM balance_history
                       WHERE user_id = ? AND twd_balance IS NOT NULL
                       ORDER BY snapshot_date DESC LIMIT 1""",
                    (user_id,),
                ).fetchone()
            except db.OperationalError:
                return None
            if not row:
                return None
            return LatestBalance(
                snapshot_date=row["snapshot_date"],
                twd_balance=_to_int_safe(row["twd_balance"]),
            )
        finally:
            con.close()

    def get_latest_loan_balance(
        self,
        *,
        bank: str,
        user_id: int,
    ) -> LatestLoanBalance | None:
        """balance_history.loan_balance 最新一筆 (給貸款餘額 fallback 用)."""
        con = db.open_bank_conn(bank)
        if con is None:
            return None
        try:
            try:
                row = con.execute(
                    """SELECT snapshot_date, loan_balance FROM balance_history
                       WHERE user_id = ? AND loan_balance IS NOT NULL
                       ORDER BY snapshot_date DESC LIMIT 1""",
                    (user_id,),
                ).fetchone()
            except db.OperationalError:
                return None
            if not row:
                return None
            return LatestLoanBalance(
                snapshot_date=row["snapshot_date"],
                loan_balance=_to_int_safe(row["loan_balance"]),
            )
        finally:
            con.close()

    def list_latest_account_txn_balances(
        self,
        *,
        bank: str,
        user_id: int,
    ) -> dict[str, AccountTxnBalance]:
        """Per-account 最新一筆 twd_transactions balance.

        SQL 用 correlated subquery 抓 max txn_datetime → 該帳戶最新有 balance 的 row.
        不 filter currency (sinopac JPY 帳戶 balance 也存在 twd_transactions).
        Bank db 不存在 / 表不存在 / 沒 row → 空 dict.
        """
        con = db.open_bank_conn(bank)
        if con is None:
            return {}
        try:
            out: dict[str, AccountTxnBalance] = {}
            try:
                rows = con.execute(
                    """SELECT t1.account_no, t1.balance, t1.txn_datetime
                       FROM twd_transactions t1
                       WHERE t1.user_id = ? AND t1.balance IS NOT NULL
                         AND t1.txn_datetime = (
                           SELECT MAX(t2.txn_datetime) FROM twd_transactions t2
                           WHERE t2.user_id = ? AND t2.account_no = t1.account_no AND t2.balance IS NOT NULL
                         )""",
                    (user_id, user_id),
                ).fetchall()
            except db.OperationalError:
                return {}
            for r in rows:
                account_no = r["account_no"]
                if not account_no:
                    continue
                out[account_no] = AccountTxnBalance(
                    account_no=account_no,
                    txn_datetime=r["txn_datetime"],
                    balance=_to_int_safe(r["balance"]),
                )
            return out
        finally:
            con.close()

    def list_card_pending_amounts_for_user(
        self,
        *,
        bank: str,
        user_id: int,
    ) -> list[CardMonthAmountRow]:
        """掃 card_pending_txns 全部 row (該 user) 的 (amount, currency, card_no).

        auto_excluded=1 row 過濾掉 (老 schema 沒此欄就不 filter).
        Caller 端做 currency / excluded-card filter 後 sum.
        """
        con = db.open_bank_conn(bank)
        if con is None:
            return []
        try:
            try:
                cols = self._columns(con, "card_pending_txns")
            except db.OperationalError:
                return []
            if not cols:
                return []
            excl_filter = (
                " AND COALESCE(auto_excluded, 0) = 0" if "auto_excluded" in cols else ""
            )
            splits_expr = (
                "splits_overwrite" if "splits_overwrite" in cols
                else "NULL AS splits_overwrite"
            )
            consume_date_expr = "consume_date" if "consume_date" in cols else "NULL AS consume_date"
            txn_type_expr = "txn_type" if "txn_type" in cols else "NULL AS txn_type"
            flow_type_expr = "flow_type" if "flow_type" in cols else "NULL AS flow_type"
            try:
                rows = con.execute(
                    f"""SELECT amount, currency, card_no, {consume_date_expr},
                               {txn_type_expr}, {flow_type_expr}, {splits_expr}
                        FROM card_pending_txns
                        WHERE user_id = ?{excl_filter}""",
                    (user_id,),
                ).fetchall()
            except db.OperationalError:
                return []
            return [
                CardMonthAmountRow(
                    amount=r["amount"],
                    currency=r["currency"],
                    card_no=r["card_no"],
                    consume_date=r["consume_date"],
                    txn_type=r["txn_type"],
                    flow_type=r["flow_type"],
                    splits_overwrite=r["splits_overwrite"],
                )
                for r in rows
            ]
        finally:
            con.close()

    def list_card_billed_amounts_for_month(
        self,
        *,
        bank: str,
        user_id: int,
        month_pattern: str,  # e.g. "2026-06-%"
    ) -> list[CardMonthAmountRow]:
        """掃 card_billed_txns 本月消費日（支援 YYYY-MM-DD／YYYY/MM/DD）。

        auto_excluded=1 過濾掉.
        Caller 端做 currency / excluded-card filter 後 sum.
        """
        month = month_pattern[:7]
        con = db.open_bank_conn(bank)
        if con is None:
            return []
        try:
            try:
                cols = self._columns(con, "card_billed_txns")
            except db.OperationalError:
                return []
            if not cols:
                return []
            excl_filter = (
                " AND COALESCE(auto_excluded, 0) = 0" if "auto_excluded" in cols else ""
            )
            splits_expr = (
                "splits_overwrite" if "splits_overwrite" in cols
                else "NULL AS splits_overwrite"
            )
            txn_type_expr = "txn_type" if "txn_type" in cols else "NULL AS txn_type"
            flow_type_expr = "flow_type" if "flow_type" in cols else "NULL AS flow_type"
            try:
                rows = con.execute(
                    f"""SELECT amount, currency, card_no, consume_date,
                               {txn_type_expr}, {flow_type_expr}, {splits_expr}
                        FROM card_billed_txns
                        WHERE user_id = ?
                          AND REPLACE(SUBSTR(consume_date, 1, 7), '/', '-') = ?{excl_filter}""",
                    (user_id, month),
                ).fetchall()
            except db.OperationalError:
                return []
            return [
                CardMonthAmountRow(
                    amount=r["amount"],
                    currency=r["currency"],
                    card_no=r["card_no"],
                    consume_date=r["consume_date"],
                    txn_type=r["txn_type"],
                    flow_type=r["flow_type"],
                    splits_overwrite=r["splits_overwrite"],
                )
                for r in rows
            ]
        finally:
            con.close()


__all__ = [
    "AccountTxnBalance",
    "CardMonthAmountRow",
    "LatestBalance",
    "LatestLoanBalance",
    "LatestMetric",
    "PortfolioReadMixin",
]
