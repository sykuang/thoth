"""db_facade._base — Plan B core infrastructure.

Held here (not duplicated per-domain):
  - Database class — façade root, picks up per-domain method mixins
  - _TransactionScope class — scoped writes with auto commit/rollback
  - Shared internals: _now_iso, _has_table, _columns
  - Shared domain exceptions: BankNotAvailable (used by every domain)

Per-domain method modules (cards.py, accounts.py, ...) define mixins that
Database inherits from, and per-domain TransactionScope mixins that
_TransactionScope inherits from. This keeps SQL grouped by table family
while preserving the single `db_api` singleton entry point.

Strict rules (enforced by tests/test_db_facade_poc.py):
  - No fastapi / sqlite3 / psycopg / psycopg2 imports anywhere in db_facade/
  - No HTTPException raises — domain exceptions only
  - All Pydantic models extra='forbid'
  - Method signatures take primitives (str/int/bool) or Pydantic models —
    NEVER raw con / cur / Row
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from backend.server import db


# ============================================================
# Shared domain exceptions
# ============================================================


class BankNotAvailable(Exception):
    """Bank db connection unavailable (db_facade.db.open_bank_conn returned None)."""

    def __init__(self, bank: str) -> None:
        self.bank = bank
        super().__init__(f"bank db not available: {bank}")


# ============================================================
# Internal helpers shared across domain mixins
# ============================================================


class _BaseHelpers:
    """Mixed into Database + _TransactionScope so both reuse same helpers."""

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%fZ")

    @staticmethod
    def _has_table(con: Any, table: str) -> bool:
        from backend.core import bank_data
        return bank_data.has_table(con, table)

    @staticmethod
    def _columns(con: Any, table: str) -> set[str]:
        from backend.core import bank_data
        return set(bank_data.columns(con, table))

    @staticmethod
    def _excluded_nos_all_banks_fast(
        *,
        table: str,
        id_col: str,
        user_id: int,
        banks: list[str],
    ) -> dict[str, set[str]] | None:
        """Fast PG path for {bank: excluded account/card nos} without per-bank connections.

        SQLite intentionally stays on the legacy per-bank path because each bank
        is a separate file. In PG, per-bank schemas share one database, so a
        UNION ALL over only schemas that actually have ``table`` avoids one
        connection/schema setup per bank and handles banks without cards.
        """
        if db.DB_BACKEND != "postgres" or not banks:
            return None
        if table not in {"accounts", "cards"} or id_col not in {"account_no", "card_no"}:
            return None
        from backend.core import bank_pg

        schema_to_bank = {bank_pg.schema_name(bank): bank for bank in banks}
        placeholders = ",".join(["?"] * len(schema_to_bank))
        try:
            with db.get_conn() as con:
                table_rows = con.execute(
                    "SELECT table_schema FROM information_schema.tables "
                    f"WHERE table_name = ? AND table_schema IN ({placeholders})",
                    (table, *schema_to_bank.keys()),
                ).fetchall()
                existing_schemas = [r["table_schema"] if isinstance(r, dict) else r[0] for r in table_rows]
                parts: list[str] = []
                params: list[Any] = []
                for schema in existing_schemas:
                    bank = schema_to_bank.get(schema)
                    if not bank:
                        continue
                    safe_bank = bank.replace("'", "''")
                    parts.append(
                        f"SELECT '{safe_bank}' AS bank, {id_col} AS no "
                        f'FROM "{schema}"."{table}" '
                        "WHERE user_id = ? AND COALESCE(excluded, 0) = 1"
                    )
                    params.append(user_id)
                if not parts:
                    return {}
                rows = con.execute(" UNION ALL ".join(parts), tuple(params)).fetchall()
        except Exception:
            return None
        out: dict[str, set[str]] = {}
        for r in rows:
            bank = r["bank"] if isinstance(r, dict) else r[0]
            no = r["no"] if isinstance(r, dict) else r[1]
            if no:
                out.setdefault(bank, set()).add(no)
        return out

    @staticmethod
    def _positive(value: Any) -> float:
        if value is None:
            return 0.0
        try:
            n = float(value)
        except (TypeError, ValueError):
            return 0.0
        return n if n > 0 else 0.0


# ============================================================
# Database — root façade. Domain mixins extend this.
# ============================================================


class _DatabaseBase(_BaseHelpers):
    """Base Database without any domain methods.

    The public `Database` (in db_facade/__init__.py) inherits from this
    and from each per-domain mixin (CardsReads, etc.).
    """

    @contextmanager
    def transaction(self, *, bank: str) -> Iterator[_TransactionScope]:
        """Open per-bank transaction. Commit on exit, rollback on exception.

        Usage:
            with db_api.transaction(bank="hsbc") as tx:
                tx.set_card_excluded(user_id=6, card_no="X", excluded=True)
                tx.set_card_nickname(user_id=6, card_no="X", nickname_overwrite="主力卡")
            # auto-commit here, or rollback if raised

        Raises BankNotAvailable if `db.open_bank_conn(bank)` returns None.
        """
        con = db.open_bank_conn(bank)
        if con is None:
            raise BankNotAvailable(bank)
        try:
            scope = _TransactionScope(self, bank, con)
            yield scope
            con.commit()
        except Exception:
            try:
                con.rollback()
            except Exception:
                pass
            raise
        finally:
            con.close()


# ============================================================
# _TransactionScope — root. Domain write mixins extend this.
# ============================================================


class _TransactionScopeBase(_BaseHelpers):
    """Base scope without any domain write methods. The public
    `_TransactionScope` (in db_facade/__init__.py) inherits per-domain write
    mixins on top of this."""

    def __init__(self, parent: Any, bank: str, con: Any) -> None:
        self._parent = parent
        self._bank = bank
        self._con = con


# Forward reference so _DatabaseBase can annotate yield
class _TransactionScope(_TransactionScopeBase):
    """Placeholder — real composition happens in db_facade/__init__.py."""


__all__ = [
    "BankNotAvailable",
    "_BaseHelpers",
    "_DatabaseBase",
    "_TransactionScope",
    "_TransactionScopeBase",
]
