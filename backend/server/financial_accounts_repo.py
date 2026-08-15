"""Server-DB repository for manual financial accounts and investment ledger."""
from __future__ import annotations

from decimal import Decimal
from typing import Callable

from backend.server import db


_ACCOUNT_COLUMNS = "id, product_type, name, currency, balance, included_in_net_worth"
_TRANSACTION_COLUMNS = (
    "id, account_id, kind, occurred_on, symbol, quantity, "
    "amount, currency, note, created_at, updated_at"
)
_REPAYMENT_COLUMNS = (
    "id, account_id, occurred_on, principal, interest, fee, note, created_at, updated_at"
)


def _lock_account(conn, user_id: int, account_id: int):
    if db.DB_BACKEND == "postgres":
        return conn.execute(
            """SELECT product_type, balance, repayment_revision
               FROM manual_financial_accounts WHERE id=? AND user_id=? FOR UPDATE""",
            (account_id, user_id),
        ).fetchone()
    conn.execute(
        "UPDATE manual_financial_accounts SET updated_at=updated_at WHERE id=? AND user_id=?",
        (account_id, user_id),
    )
    return conn.execute(
        """SELECT product_type, balance, repayment_revision
           FROM manual_financial_accounts WHERE id=? AND user_id=?""",
        (account_id, user_id),
    ).fetchone()


def insert_account(user_id: int, values: dict[str, object], now: str) -> int:
    with db.get_conn() as conn:
        row = conn.execute(
            """INSERT INTO manual_financial_accounts
               (user_id, product_type, name, currency, balance,
                included_in_net_worth, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            (
                user_id, values["product_type"], values["name"], values["currency"], values["balance"],
                1 if values["included_in_net_worth"] else 0, now, now,
            ),
        ).fetchone()
    if row is None:
        raise RuntimeError("manual account insert returned no id")
    return int(row[0])


def get_account(user_id: int, account_id: int):
    with db.get_conn() as conn:
        return conn.execute(
            f"SELECT {_ACCOUNT_COLUMNS} FROM manual_financial_accounts WHERE id=? AND user_id=?",
            (account_id, user_id),
        ).fetchone()


def list_accounts(user_id: int):
    with db.get_conn() as conn:
        return conn.execute(
            f"""SELECT {_ACCOUNT_COLUMNS} FROM manual_financial_accounts
                WHERE user_id=? ORDER BY updated_at DESC, id DESC""",
            (user_id,),
        ).fetchall()


def update_account(
    user_id: int,
    account_id: int,
    values: dict[str, object],
    now: str,
    expected_balance: str | None = None,
) -> str:
    with db.get_conn() as conn:
        account = _lock_account(conn, user_id, account_id)
        if account is None:
            return "not_found"
        if account[0] == "investment" and values["product_type"] != "investment":
            count = conn.execute(
                "SELECT COUNT(*) FROM manual_investment_transactions WHERE account_id=? AND user_id=?",
                (account_id, user_id),
            ).fetchone()
            if count and int(count[0]) > 0:
                return "has_transactions"
        if account[0] in {"loan", "mortgage", "credit_line"}:
            count = conn.execute(
                "SELECT COUNT(*) FROM manual_liability_repayments WHERE account_id=? AND user_id=?",
                (account_id, user_id),
            ).fetchone()
            if (
                count and int(count[0]) > 0
                and values["product_type"] not in {"loan", "mortgage", "credit_line"}
            ):
                return "has_repayments"
            if (
                (expected_balance is None and int(account[2] or 0) > 0)
                or (
                    expected_balance is not None
                    and Decimal(str(account[1])) != Decimal(expected_balance)
                )
            ):
                return "balance_conflict"
        cursor = conn.execute(
            """UPDATE manual_financial_accounts SET
                   product_type=?, name=?, currency=?, balance=?, included_in_net_worth=?, updated_at=?
               WHERE id=? AND user_id=?""",
            (
                values["product_type"], values["name"], values["currency"], values["balance"],
                1 if values["included_in_net_worth"] else 0, now, account_id, user_id,
            ),
        )
        return "updated" if cursor.rowcount > 0 else "not_found"


def update_account_inclusion(
    user_id: int,
    account_id: int,
    included_in_net_worth: bool,
    now: str,
) -> bool:
    with db.get_conn() as conn:
        cursor = conn.execute(
            """UPDATE manual_financial_accounts
               SET included_in_net_worth=?, updated_at=?
               WHERE id=? AND user_id=?""",
            (1 if included_in_net_worth else 0, now, account_id, user_id),
        )
        return cursor.rowcount > 0


def delete_account(user_id: int, account_id: int) -> bool:
    with db.get_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM manual_financial_accounts WHERE id=? AND user_id=?",
            (account_id, user_id),
        )
        return cursor.rowcount > 0


def list_holding_rows(user_id: int, account_id: int):
    with db.get_conn() as conn:
        return conn.execute(
            """SELECT id, occurred_on, kind, symbol, quantity, currency
               FROM manual_investment_transactions WHERE account_id=? AND user_id=?""",
            (account_id, user_id),
        ).fetchall()


def mutate_transaction(
    user_id: int,
    account_id: int,
    operation: str,
    values: dict[str, str | None] | None,
    transaction_id: int | None,
    now: str,
    validate: Callable[[list], object],
) -> int | bool | None:
    """Lock account, validate ledger snapshot, and mutate in one DB transaction."""
    with db.get_conn() as conn:
        account = _lock_account(conn, user_id, account_id)
        if account is None or account[0] != "investment":
            return None
        rows = conn.execute(
            """SELECT id, occurred_on, kind, symbol, quantity, currency
               FROM manual_investment_transactions WHERE account_id=? AND user_id=?""",
            (account_id, user_id),
        ).fetchall()
        validate(rows)
        if operation == "insert":
            assert values is not None
            row = conn.execute(
                """INSERT INTO manual_investment_transactions
                   (user_id, account_id, kind, occurred_on, symbol, quantity,
                    amount, currency, note, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
                (
                    user_id, account_id, values["kind"], values["occurred_on"], values["symbol"],
                    values["quantity"], values["amount"], values["currency"],
                    values["note"], now, now,
                ),
            ).fetchone()
            return int(row[0]) if row else None
        assert transaction_id is not None
        if operation == "update":
            assert values is not None
            cursor = conn.execute(
                """UPDATE manual_investment_transactions SET
                       kind=?, occurred_on=?, symbol=?, quantity=?, amount=?,
                       currency=?, note=?, updated_at=?
                   WHERE id=? AND account_id=? AND user_id=?""",
                (
                    values["kind"], values["occurred_on"], values["symbol"], values["quantity"],
                    values["amount"], values["currency"], values["note"], now,
                    transaction_id, account_id, user_id,
                ),
            )
        else:
            cursor = conn.execute(
                "DELETE FROM manual_investment_transactions WHERE id=? AND account_id=? AND user_id=?",
                (transaction_id, account_id, user_id),
            )
        return cursor.rowcount > 0


def insert_transaction(
    user_id: int,
    account_id: int,
    values: dict[str, str | None],
    now: str,
) -> int:
    with db.get_conn() as conn:
        row = conn.execute(
            """INSERT INTO manual_investment_transactions
               (user_id, account_id, kind, occurred_on, symbol, quantity,
                amount, currency, note, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            (
                user_id, account_id, values["kind"], values["occurred_on"], values["symbol"],
                values["quantity"], values["amount"], values["currency"],
                values["note"], now, now,
            ),
        ).fetchone()
    if row is None:
        raise RuntimeError("manual transaction insert returned no id")
    return int(row[0])


def get_transaction(user_id: int, account_id: int, transaction_id: int):
    with db.get_conn() as conn:
        return conn.execute(
            f"""SELECT {_TRANSACTION_COLUMNS} FROM manual_investment_transactions
                WHERE id=? AND account_id=? AND user_id=?""",
            (transaction_id, account_id, user_id),
        ).fetchone()


def list_transactions(user_id: int, account_id: int):
    with db.get_conn() as conn:
        return conn.execute(
            f"""SELECT {_TRANSACTION_COLUMNS} FROM manual_investment_transactions
                WHERE account_id=? AND user_id=? ORDER BY occurred_on DESC, id DESC""",
            (account_id, user_id),
        ).fetchall()


def update_transaction(
    user_id: int,
    account_id: int,
    transaction_id: int,
    values: dict[str, str | None],
    now: str,
) -> bool:
    with db.get_conn() as conn:
        cursor = conn.execute(
            """UPDATE manual_investment_transactions SET
                   kind=?, occurred_on=?, symbol=?, quantity=?, amount=?,
                   currency=?, note=?, updated_at=?
               WHERE id=? AND account_id=? AND user_id=?""",
            (
                values["kind"], values["occurred_on"], values["symbol"], values["quantity"],
                values["amount"], values["currency"], values["note"], now,
                transaction_id, account_id, user_id,
            ),
        )
        return cursor.rowcount > 0


def delete_transaction(user_id: int, account_id: int, transaction_id: int) -> bool:
    with db.get_conn() as conn:
        cursor = conn.execute(
            """DELETE FROM manual_investment_transactions
               WHERE id=? AND account_id=? AND user_id=?""",
            (transaction_id, account_id, user_id),
        )
        return cursor.rowcount > 0


def get_repayment(user_id: int, account_id: int, repayment_id: int):
    with db.get_conn() as conn:
        return conn.execute(
            f"""SELECT {_REPAYMENT_COLUMNS} FROM manual_liability_repayments
                WHERE id=? AND account_id=? AND user_id=?""",
            (repayment_id, account_id, user_id),
        ).fetchone()


def list_repayments(user_id: int, account_id: int):
    with db.get_conn() as conn:
        return conn.execute(
            f"""SELECT {_REPAYMENT_COLUMNS} FROM manual_liability_repayments
                WHERE account_id=? AND user_id=? ORDER BY occurred_on DESC, id DESC""",
            (account_id, user_id),
        ).fetchall()


def mutate_repayment(
    user_id: int,
    account_id: int,
    operation: str,
    values: dict[str, str | None] | None,
    repayment_id: int | None,
    now: str,
    adjust_balance: Callable[[str, str | None, str | None], str],
) -> int | bool | None:
    """Lock the account and mutate one repayment plus its balance atomically."""
    with db.get_conn() as conn:
        account = _lock_account(conn, user_id, account_id)
        if account is None or account[0] not in {"loan", "mortgage", "credit_line"}:
            return None

        existing = None
        if operation != "insert":
            assert repayment_id is not None
            existing = conn.execute(
                """SELECT principal FROM manual_liability_repayments
                   WHERE id=? AND account_id=? AND user_id=?""",
                (repayment_id, account_id, user_id),
            ).fetchone()
            if existing is None:
                return False

        old_principal = str(existing[0]) if existing is not None else None
        new_principal = values["principal"] if values is not None else None
        balance = adjust_balance(str(account[1]), old_principal, new_principal)

        if operation == "insert":
            assert values is not None
            row = conn.execute(
                """INSERT INTO manual_liability_repayments
                   (user_id, account_id, occurred_on, principal, interest, fee,
                    note, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
                (
                    user_id, account_id, values["occurred_on"], values["principal"],
                    values["interest"], values["fee"], values["note"], now, now,
                ),
            ).fetchone()
            result: int | bool = int(row[0]) if row else False
        elif operation == "update":
            assert values is not None and repayment_id is not None
            cursor = conn.execute(
                """UPDATE manual_liability_repayments SET
                       occurred_on=?, principal=?, interest=?, fee=?, note=?, updated_at=?
                   WHERE id=? AND account_id=? AND user_id=?""",
                (
                    values["occurred_on"], values["principal"], values["interest"],
                    values["fee"], values["note"], now, repayment_id, account_id, user_id,
                ),
            )
            result = cursor.rowcount > 0
        else:
            assert repayment_id is not None
            cursor = conn.execute(
                """DELETE FROM manual_liability_repayments
                   WHERE id=? AND account_id=? AND user_id=?""",
                (repayment_id, account_id, user_id),
            )
            result = cursor.rowcount > 0

        conn.execute(
            """UPDATE manual_financial_accounts
               SET balance=?, updated_at=?, repayment_revision=repayment_revision+1
               WHERE id=? AND user_id=?""",
            (balance, now, account_id, user_id),
        )
        return result
