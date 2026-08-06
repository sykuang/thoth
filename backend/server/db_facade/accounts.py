"""db_facade.accounts — accounts table domain (Plan B B2).

Scope: bank DB `accounts` table only (server DB's `bank_accounts` table is
managed by creds_store.py — out of scope per 使用者 2026-06-19 decision B2a).

Pydantic models:
  AccountRow             single accounts row (raw_balance / nickname / etc.)
  SetAccountExcludedResult
  SetAccountNicknameResult

Domain exceptions:
  AccountNotFound

Mixins:
  AccountsReadMixin      list_accounts / list_loan_accounts /
                         list_excluded_account_nos_all_banks
  AccountsWriteMixin     set_account_excluded / set_account_nickname
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from backend.server import db

from ._base import _BaseHelpers


# ============================================================
# Domain exceptions
# ============================================================


class AccountNotFound(Exception):
    def __init__(self, bank: str, account_no: str) -> None:
        self.bank = bank
        self.account_no = account_no
        super().__init__(f"account not found: {bank}/{account_no}")


# ============================================================
# Pydantic result models
# ============================================================


class AccountRow(BaseModel):
    """單一 accounts row — 給 portfolio.py 拼餘額時用.

    保留 raw_balance / raw_balance_date 給 caller 套餘額優先順序邏輯
    (raw_balance > twd_transactions latest > balance_history.loan_balance > None).
    """

    model_config = ConfigDict(extra="forbid")

    bank: str
    account_no: str
    currency: str | None = None
    nickname: str | None = None
    nickname_overwrite: str | None = None
    type: str | None = None
    product_type: str | None = None
    raw_balance: float | None = None
    raw_balance_date: str | None = None
    excluded: bool = False
    updated_at: str | None = None


class LoanAccountRow(BaseModel):
    """貸款帳戶 row — 給 _latest_loan_balance fallback 用 (product_type IN loan/mortgage/credit_line)."""

    model_config = ConfigDict(extra="forbid")

    bank: str
    account_no: str
    currency: str | None = None
    product_type: str | None = None
    raw_balance: float | None = None
    raw_balance_date: str | None = None
    updated_at: str | None = None


class SetAccountExcludedResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bank: str
    account_no: str
    excluded: bool
    updated_at: str


class SetAccountNicknameResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bank: str
    account_no: str
    nickname_overwrite: str | None
    updated_at: str


# ============================================================
# Internal helpers
# ============================================================


def _nick_col_expr(con: Any, helpers: _BaseHelpers) -> str:
    cols = helpers._columns(con, "accounts")
    return ", nickname_overwrite" if "nickname_overwrite" in cols else ""


def _row_to_account_dict(bank: str, row: Any, has_nick_overwrite: bool) -> dict[str, Any]:
    keys = set(row.keys())

    def _get(col: str, default: Any = None) -> Any:
        return row[col] if col in keys else default

    raw_bal = _get("raw_balance")
    raw_bal_float: float | None = None
    if raw_bal is not None:
        try:
            raw_bal_float = float(raw_bal)
        except (TypeError, ValueError):
            raw_bal_float = None

    return {
        "bank": bank,
        "account_no": _get("account_no") or "",
        "currency": _get("currency"),
        "nickname": _get("nickname"),
        "nickname_overwrite": _get("nickname_overwrite") if has_nick_overwrite else None,
        "type": _get("type"),
        "product_type": _get("product_type"),
        "raw_balance": raw_bal_float,
        "raw_balance_date": _get("raw_balance_date"),
        "excluded": bool(_get("excluded", 0)),
        "updated_at": _get("updated_at"),
    }


# ============================================================
# AccountsReadMixin
# ============================================================


class AccountsReadMixin(_BaseHelpers):
    """Read-only accounts methods. Each opens + closes its own connection."""

    def list_accounts(
        self,
        *,
        bank: str,
        user_id: int,
    ) -> list[AccountRow]:
        """單一 bank 的所有 accounts (給 portfolio.portfolio_accounts 用).

        Bank db 不存在 / 沒 accounts 表 / OperationalError → []. caller 在
        跨 bank 聚合時不希望 raise.
        """
        con = db.open_bank_conn(bank)
        if con is None:
            return []
        try:
            try:
                acc_cols = self._columns(con, "accounts")
            except db.OperationalError:
                return []
            if not acc_cols:
                return []
            has_nick_overwrite = "nickname_overwrite" in acc_cols
            nick_extra = _nick_col_expr(con, self)
            try:
                rows = con.execute(
                    f"""SELECT account_no, currency, nickname, type, product_type,
                              raw_balance, raw_balance_date,
                              COALESCE(excluded, 0) AS excluded, updated_at{nick_extra}
                       FROM accounts WHERE user_id = ? ORDER BY account_no""",
                    (user_id,),
                ).fetchall()
            except db.OperationalError:
                return []
            return [
                AccountRow(**_row_to_account_dict(bank, r, has_nick_overwrite))
                for r in rows
            ]
        finally:
            con.close()

    def list_loan_accounts(
        self,
        *,
        bank: str,
        user_id: int,
    ) -> list[LoanAccountRow]:
        """貸款型帳戶 (product_type IN loan/mortgage/credit_line) — 給 _latest_loan_balance fallback 用."""
        con = db.open_bank_conn(bank)
        if con is None:
            return []
        try:
            try:
                rows = con.execute(
                    """SELECT account_no, currency, product_type,
                              raw_balance, raw_balance_date, updated_at
                       FROM accounts WHERE user_id = ? AND product_type IN ('loan', 'mortgage', 'credit_line')""",
                    (user_id,),
                ).fetchall()
            except db.OperationalError:
                return []
            out: list[LoanAccountRow] = []
            for r in rows:
                raw_balance = r["raw_balance"]
                out.append(LoanAccountRow(
                    bank=bank,
                    account_no=r["account_no"] or "",
                    currency=r["currency"],
                    product_type=r["product_type"],
                    raw_balance=float(raw_balance) if raw_balance is not None else None,
                    raw_balance_date=r["raw_balance_date"],
                    updated_at=r["updated_at"],
                ))
            return out
        finally:
            con.close()

    def list_excluded_account_nos_all_banks(
        self,
        *,
        user_id: int,
        banks: list[str],
    ) -> dict[str, set[str]]:
        """掃指定 banks, 回 {bank: set(excluded account_no)} — limit 本 user.

        給 transactions stats 用 (跳過 excluded 帳戶的 txn).
        """
        fast = self._excluded_nos_all_banks_fast(
            table="accounts", id_col="account_no", user_id=user_id, banks=banks,
        )
        if fast is not None:
            return fast
        out = {}
        for bank in banks:
            con = db.open_bank_conn(bank)
            if con is None:
                continue
            try:
                try:
                    rows = con.execute(
                        "SELECT account_no FROM accounts WHERE user_id = ? AND COALESCE(excluded, 0) = 1",
                        (user_id,),
                    ).fetchall()
                    if rows:
                        out[bank] = {r["account_no"] for r in rows if r["account_no"]}
                except db.OperationalError:
                    pass
            finally:
                con.close()
        return out


# ============================================================
# AccountsWriteMixin
# ============================================================


class AccountsWriteMixin(_BaseHelpers):
    """Accounts write methods. Used inside `with db_api.transaction() as tx:`."""

    _con: Any
    _bank: str

    def set_account_excluded(
        self,
        *,
        user_id: int,
        account_no: str,
        excluded: bool,
    ) -> SetAccountExcludedResult:
        """切換帳戶「納入淨資產統計」flag. raise AccountNotFound 若帳戶不存在."""
        row = self._con.execute(
            "SELECT account_no FROM accounts WHERE account_no = ? AND user_id = ?",
            (account_no, user_id),
        ).fetchone()
        if row is None:
            raise AccountNotFound(self._bank, account_no)
        now = self._now_iso()
        self._con.execute(
            "UPDATE accounts SET excluded = ?, updated_at = ? WHERE account_no = ? AND user_id = ?",
            (1 if excluded else 0, now, account_no, user_id),
        )
        return SetAccountExcludedResult(
            bank=self._bank, account_no=account_no, excluded=excluded, updated_at=now,
        )

    def set_account_nickname(
        self,
        *,
        user_id: int,
        account_no: str,
        nickname_overwrite: str | None,
    ) -> SetAccountNicknameResult:
        """設/清 user 取的帳戶暱稱. 老 db 沒此欄會 ALTER TABLE 補. raise AccountNotFound."""
        cols = self._columns(self._con, "accounts")
        if "nickname_overwrite" not in cols:
            self._con.execute("ALTER TABLE accounts ADD COLUMN nickname_overwrite TEXT")
        row = self._con.execute(
            "SELECT account_no FROM accounts WHERE account_no = ? AND user_id = ?",
            (account_no, user_id),
        ).fetchone()
        if row is None:
            raise AccountNotFound(self._bank, account_no)
        now = self._now_iso()
        new_nick = nickname_overwrite if nickname_overwrite else None
        self._con.execute(
            "UPDATE accounts SET nickname_overwrite = ?, updated_at = ? WHERE account_no = ? AND user_id = ?",
            (new_nick, now, account_no, user_id),
        )
        return SetAccountNicknameResult(
            bank=self._bank,
            account_no=account_no,
            nickname_overwrite=new_nick,
            updated_at=now,
        )


__all__ = [
    "AccountNotFound",
    "AccountRow",
    "AccountsReadMixin",
    "AccountsWriteMixin",
    "LoanAccountRow",
    "SetAccountExcludedResult",
    "SetAccountNicknameResult",
]
