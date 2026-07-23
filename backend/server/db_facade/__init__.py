"""db_facade — Plan B DB abstraction layer (package root).

All SQL queries against bank databases live inside this package. Callers
(routers, sync runner, helpers) only `from backend.server.db_facade import db_api, ...`
and use typed Pydantic models in / out.

Package layout:
  _base.py          Database root, _TransactionScope root, shared helpers,
                    BankNotAvailable exception
  cards.py          cards / card_billed_txns / card_pending_txns domain
                    (B1, 2026-06-19)
  accounts.py       accounts / account_*_txns domain (B2, planned)
  transactions.py   txn-family domain (B3, planned)
  portfolio.py      portfolio aggregates domain (B4, planned)
  sync.py           sync layer (B5, planned)
  auth.py           users / app metadata (B6, planned)

The single `db_api` singleton inherits from every per-domain ReadMixin so
callers see one flat API surface (no `db_api.cards.list_cards` chains).
"""

from __future__ import annotations

from ._base import BankNotAvailable, _DatabaseBase, _TransactionScopeBase
from .accounts import (
    AccountNotFound,
    AccountRow,
    AccountsReadMixin,
    AccountsWriteMixin,
    LoanAccountRow,
    SetAccountExcludedResult,
    SetAccountNicknameResult,
)
from .cards import (
    BilledTxnRow,
    CardDetail,
    CardNotFound,
    CardsReadMixin,
    CardsTableMissing,
    CardsWriteMixin,
    CardSummary,
    PaymentRow,
    PendingTxnRow,
    SetCardExcludedResult,
    SetCardNicknameResult,
)
from .portfolio import (
    AccountTxnBalance,
    CardMonthAmountRow,
    LatestBalance,
    LatestLoanBalance,
    LatestMetric,
    PortfolioReadMixin,
)
from .transactions import (
    UNSET,
    TagAggRow,
    TransactionsReadMixin,
    TransactionsWriteMixin,
    TxnColumnMissing,
    TxnKind,
    TxnNotFound,
    TxnRow,
    TxnStatRow,
    TxnUpdateFields,
    TxnUpdateResult,
)


class _TransactionScope(
    AccountsWriteMixin,
    CardsWriteMixin,
    TransactionsWriteMixin,
    _TransactionScopeBase,
):
    """All per-domain write mixins layered on top of the base scope."""


class Database(
    AccountsReadMixin,
    CardsReadMixin,
    PortfolioReadMixin,
    TransactionsReadMixin,
    _DatabaseBase,
):
    """All per-domain read mixins layered on top of the base façade."""


# Patch _base._TransactionScope module-level binding so contextmanager yields
# the composed class. Done here (not at import time) so cards/accounts/etc.
# don't need to know about each other.
from . import _base as _base_mod
_base_mod._TransactionScope = _TransactionScope


db_api = Database()


__all__ = [
    "UNSET",
    "AccountNotFound",
    "AccountRow",
    "AccountTxnBalance",
    "BankNotAvailable",
    "BilledTxnRow",
    "CardDetail",
    "CardMonthAmountRow",
    "CardNotFound",
    "CardSummary",
    "CardsTableMissing",
    "Database",
    "LatestBalance",
    "LatestLoanBalance",
    "LatestMetric",
    "LoanAccountRow",
    "PaymentRow",
    "PendingTxnRow",
    "SetAccountExcludedResult",
    "SetAccountNicknameResult",
    "SetCardExcludedResult",
    "SetCardNicknameResult",
    "TagAggRow",
    "TxnColumnMissing",
    "TxnKind",
    "TxnNotFound",
    "TxnRow",
    "TxnStatRow",
    "TxnUpdateFields",
    "TxnUpdateResult",
    "db_api",
]
