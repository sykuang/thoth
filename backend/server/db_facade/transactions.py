"""db_facade.transactions — txn-family domain (Plan B B3).

Covers 3 sibling tables across all bank databases:
  twd_transactions    (台幣帳戶 in/out)
  card_billed_txns    (已出帳信用卡)
  card_pending_txns   (未出帳信用卡)

Pydantic models:
  TxnRow              raw row (transform 邏輯仍由 router 端做 — 因為
                      transform 邏輯吃太多 join 資料: bank/excluded/
                      currency 等等)
  TxnUpdateFields     PATCH 可改的欄位 (typed instead of dict[str, Any])
  TxnUpdateResult     PATCH 完的最新 row
  TagAggRow           tags/popular 用

Domain exceptions:
  TxnNotFound
  TxnColumnMissing    老 schema 缺欄

Mixins:
  TransactionsReadMixin:  list_txns_for_bank, get_txn,
                          list_user_txns_with_tags
  TransactionsWriteMixin: update_txn (with auto ALTER for tags_overwrite /
                          auto_excluded), recategorize_user
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from backend.server import db

from ._base import _BaseHelpers

perf_log = logging.getLogger("backend.perf")


# ============================================================
# Constants — kind ↔ table mapping (mirrors router)
# ============================================================


# Valid kinds for txn family
TxnKind = Literal["twd", "billed", "pending"]

_KIND_TO_TABLE: dict[str, str] = {
    "twd": "twd_transactions",
    "billed": "card_billed_txns",
    "pending": "card_pending_txns",
}

_KIND_TO_DATE_COL: dict[str, str] = {
    "twd": "txn_datetime",
    "billed": "consume_date",
    "pending": "consume_date",
}

# Tables that participate in categorize / recategorize
_CATEGORIZED_TABLES: tuple[str, ...] = (
    "twd_transactions",
    "card_billed_txns",
    "card_pending_txns",
)


# ============================================================
# Domain exceptions
# ============================================================


class TxnNotFound(Exception):
    def __init__(self, bank: str, kind: str, txn_id: int) -> None:
        self.bank = bank
        self.kind = kind
        self.txn_id = txn_id
        super().__init__(f"txn not found: {bank}/{kind}/{txn_id}")


class TxnColumnMissing(Exception):
    """老 schema 缺欄 (e.g. category / subcategory / description_overwrite).

    Caller should translate to HTTP 409 Conflict.
    """

    def __init__(self, bank: str, table: str, column: str) -> None:
        self.bank = bank
        self.table = table
        self.column = column
        super().__init__(f"column missing: {bank}.{table}.{column}")


# ============================================================
# Pydantic models
# ============================================================


class TxnRow(BaseModel):
    """Raw txn row — pass-through dict, router transforms it.

    We DO NOT pre-shape txn rows here because:
      - twd vs billed vs pending 三表 schema 不同
      - transform 需要 join bank/excluded/currency 等 caller-owned context
      - router 端有 _twd_to_transaction / _billed_to_transaction /
        _pending_to_transaction 仍是 caller's responsibility

    So we expose the raw row as a typed dict[str, Any] wrapper that
    preserves all columns + bank/kind metadata for downstream transforms.

    `__getitem__` is implemented so existing routers can do `row["amount"]`
    (mirroring sqlite3.Row access). Missing keys return None to match the
    permissive PG Row contract.
    """

    model_config = ConfigDict(extra="allow")

    bank: str
    kind: str  # twd / billed / pending
    table: str  # twd_transactions / card_billed_txns / card_pending_txns

    def __getitem__(self, key: str) -> Any:
        # Pydantic v2 attribute access; extra fields live in model_extra
        try:
            return getattr(self, key)
        except AttributeError:
            extras = getattr(self, "model_extra", None)
            if extras and key in extras:
                return extras[key]
            return None

    def keys(self):
        """Mimic sqlite3.Row.keys() for ``"col" in row.keys()`` checks."""
        out = list(type(self).model_fields.keys())
        extras = getattr(self, "model_extra", None) or {}
        out.extend(k for k in extras if k not in out)
        return out


class TxnUpdateFields(BaseModel):
    """White-list of mutable fields for PATCH /transactions/{bank}/{kind}/{id}.

    All optional — caller sends partial update. None means "skip this
    field"; empty string means "set to NULL" for category/subcategory/
    description_overwrite.

    Note: TxnUpdateFields itself is reserved for future router-side typing
    but the update method currently takes individual kwargs because the
    distinguishing-unset-from-None semantics needs sentinel object, not
    Pydantic field defaults.
    """

    model_config = ConfigDict(extra="forbid")

    category: str | None = None
    subcategory: str | None = None
    description_overwrite: str | None = None
    tags: list[str] | None = None
    tags_mode: Literal["replace", "add"] | None = None
    auto_excluded: bool | None = None


# Sentinel for "field not provided" — distinguishes from None (= set to NULL).
UNSET: Any = object()


class TxnUpdateResult(BaseModel):
    """PATCH 完, 回新 row (給 router transform → response)."""

    model_config = ConfigDict(extra="allow")

    bank: str
    kind: str
    table: str

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError:
            extras = getattr(self, "model_extra", None)
            if extras and key in extras:
                return extras[key]
            return None

    def keys(self):
        out = list(type(self).model_fields.keys())
        extras = getattr(self, "model_extra", None) or {}
        out.extend(k for k in extras if k not in out)
        return out


class TxnStatRow(BaseModel):
    """Lightweight row for /transactions/stats aggregate.

    Avoids SELECT * + raw dict transform when dashboard only needs buckets.
    """

    model_config = ConfigDict(extra="forbid")

    bank: str
    kind: str
    date: str | None
    consume_date: str | None = None
    post_date: str | None = None
    amount: int | float | None
    category: str | None = None
    subcategory: str | None = None
    txn_type: str | None = None
    flow_type: str | None = None
    is_subscription: bool = False
    income_category: str | None = None
    account_no: str | None = None
    card_no: str | None = None
    excluded: bool = False
    auto_excluded: bool = False
    # Phase 10 (2026-07-29) 分類拆帳: raw JSON, 由 router 的 _expand_stat_splits 展開。
    # stats fast path 也必須看得到, 否則已拆帳的交易在 dashboard 仍照母筆算 —
    # 跟 /transactions 列表的口徑會不一致。
    splits_overwrite: str | None = None


class TagAggRow(BaseModel):
    """tags/popular 聚合行 (raw row from per-bank scan)."""

    model_config = ConfigDict(extra="forbid")

    tags_overwrite: str | None
    date: str | None


# ============================================================
# Internal helpers
# ============================================================


def _row_to_dict(row: Any) -> dict[str, Any]:
    """sqlite3.Row / PG Row → dict (preserve all columns)."""
    if row is None:
        return {}
    return {k: row[k] for k in row.keys()}  # noqa: SIM118 — Row must use .keys()


def _has_column(con: Any, table: str, col: str, helpers: _BaseHelpers) -> bool:
    return col in helpers._columns(con, table)


def _normalize_date_for_stats(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    head = text[:10].replace("/", "-")
    return head or None


# ============================================================
# TransactionsReadMixin
# ============================================================


class TransactionsReadMixin(_BaseHelpers):
    """Read-only transactions methods."""

    def list_txns_for_bank(
        self,
        *,
        bank: str,
        user_id: int,
        kinds: list[str],
    ) -> list[TxnRow]:
        """跨 3 表收集該 bank 該 user 所有 txn (raw rows).

        Caller 自己跑 in-memory filter (since/until/q/category) — 沒搬 SQL
        WHERE 進來因為 router 已經這樣寫了, 跟 _collect_transactions 邏輯
        對齊比較重要.

        kinds: 子集 of ['twd', 'billed', 'pending']. 不在子集裡的表不掃.
        Bank db 不存在 / 表不存在 → 略過 (不 raise).
        """
        con = db.open_bank_conn(bank)
        if con is None:
            return []
        try:
            from backend.core import bank_data
            tables = bank_data.table_names(con)
            out: list[TxnRow] = []
            for kind in kinds:
                if kind not in _KIND_TO_TABLE:
                    continue
                table = _KIND_TO_TABLE[kind]
                if table not in tables:
                    continue
                date_col = _KIND_TO_DATE_COL[kind]
                # SELECT * 是 intentional — router transform 吃所有欄
                rows = con.execute(
                    f"SELECT * FROM {table} WHERE user_id = ? ORDER BY {date_col} DESC, id DESC",
                    (user_id,),
                ).fetchall()
                for r in rows:
                    raw = _row_to_dict(r)
                    out.append(TxnRow(bank=bank, kind=kind, table=table, **raw))
            return out
        finally:
            con.close()

    def list_txn_stat_rows_for_bank(
        self,
        *,
        bank: str,
        user_id: int,
        kinds: list[str],
        excluded_accounts_by_bank: dict[str, set[str]] | None = None,
        excluded_cards_by_bank: dict[str, set[str]] | None = None,
    ) -> list[TxnStatRow]:
        """Lightweight stats input rows for /transactions/stats.

        Select only columns needed for aggregate buckets instead of SELECT *.
        Missing optional columns are projected as NULL / 0 so old bank schemas
        still work.
        """
        con_started = time.perf_counter()
        con = db.open_bank_conn(bank)
        open_ms = (time.perf_counter() - con_started) * 1000
        if con is None:
            perf_log.info(
                "event=transactions.stats section=bank_meta user_id=%s bank=%s duration_ms=%.1f open_ms=%.1f tables_ms=0.0 columns_ms=0.0 rows=0 skipped=no_connection",
                user_id, bank, open_ms, open_ms,
            )
            return []
        try:
            from backend.core import bank_data
            tables_started = time.perf_counter()
            tables = bank_data.table_names(con)
            tables_ms = (time.perf_counter() - tables_started) * 1000
            columns_ms = 0.0
            out: list[TxnStatRow] = []
            if excluded_accounts_by_bank is None or excluded_cards_by_bank is None:
                excluded_lookup_started = time.perf_counter()
                excluded_accounts_by_bank = self.list_excluded_account_nos_all_banks(user_id=user_id, banks=[bank])
                excluded_cards_by_bank = self.list_excluded_card_nos_all_banks(user_id=user_id, banks=[bank])
                excluded_ms = (time.perf_counter() - excluded_lookup_started) * 1000
            else:
                excluded_ms = 0.0
            excluded_accounts = excluded_accounts_by_bank.get(bank, set())
            excluded_cards = excluded_cards_by_bank.get(bank, set())
            for kind in kinds:
                if kind not in _KIND_TO_TABLE:
                    continue
                table = _KIND_TO_TABLE[kind]
                if table not in tables:
                    continue
                columns_started = time.perf_counter()
                cols = self._columns(con, table)
                columns_ms += (time.perf_counter() - columns_started) * 1000
                date_col = _KIND_TO_DATE_COL[kind]
                category_expr = "category" if "category" in cols else "NULL"
                subcategory_expr = "subcategory" if "subcategory" in cols else "NULL"
                flow_type_expr = "flow_type" if "flow_type" in cols else "NULL"
                is_subscription_expr = "COALESCE(is_subscription, 0)" if "is_subscription" in cols else "0"
                income_category_expr = "income_category" if "income_category" in cols else "NULL"
                auto_excluded_expr = "COALESCE(auto_excluded, 0)" if "auto_excluded" in cols else "0"
                splits_expr = "splits_overwrite" if "splits_overwrite" in cols else "NULL"
                if kind == "twd":
                    account_expr = "account_no" if "account_no" in cols else "NULL"
                    amount_expr = "COALESCE(income, 0) - COALESCE(expend, 0)"
                    txn_type_expr = "NULL"
                    card_expr = "NULL"
                    consume_date_expr = "NULL"
                    post_date_expr = "NULL"
                else:
                    account_expr = "NULL"
                    amount_expr = "CASE WHEN amount > 0 AND COALESCE(txn_type, '') NOT IN ('refund', 'cashback', 'payment') THEN -amount ELSE amount END" if "txn_type" in cols else "CASE WHEN amount > 0 THEN -amount ELSE amount END"
                    txn_type_expr = "txn_type" if "txn_type" in cols else "NULL"
                    card_expr = "card_no" if "card_no" in cols else "NULL"
                    consume_date_expr = "consume_date" if "consume_date" in cols else "NULL"
                    post_date_expr = "post_date" if "post_date" in cols else "NULL"
                if kind == "pending":
                    date_filter = ""
                else:
                    date_filter = f" AND {date_col} IS NOT NULL"
                sql = f"""
                    SELECT
                        {date_col} AS date,
                        {consume_date_expr} AS consume_date,
                        {post_date_expr} AS post_date,
                        {amount_expr} AS amount,
                        {category_expr} AS category,
                        {subcategory_expr} AS subcategory,
                        {txn_type_expr} AS txn_type,
                        {flow_type_expr} AS flow_type,
                        {is_subscription_expr} AS is_subscription,
                        {income_category_expr} AS income_category,
                        {account_expr} AS account_no,
                        {card_expr} AS card_no,
                        {auto_excluded_expr} AS auto_excluded,
                        {splits_expr} AS splits_overwrite
                    FROM {table}
                    WHERE user_id = ?{date_filter}
                    ORDER BY {date_col} DESC
                """
                try:
                    query_started = time.perf_counter()
                    rows = con.execute(sql, (user_id,)).fetchall()
                    query_ms = (time.perf_counter() - query_started) * 1000
                except db.OperationalError:
                    perf_log.info(
                        "event=transactions.stats section=query user_id=%s bank=%s kind=%s rows=0 duration_ms=0.0 excluded_ms=%.1f skipped=operational_error",
                        user_id, bank, kind, excluded_ms,
                    )
                    continue
                build_started = time.perf_counter()
                for r in rows:
                    account_no = r["account_no"]
                    card_no = r["card_no"]
                    normalized_date = _normalize_date_for_stats(r["date"])
                    normalized_consume_date = _normalize_date_for_stats(r["consume_date"])
                    normalized_post_date = _normalize_date_for_stats(r["post_date"])
                    out.append(TxnStatRow(
                        bank=bank,
                        kind=kind,
                        date=normalized_date,
                        consume_date=normalized_consume_date,
                        post_date=normalized_post_date,
                        amount=r["amount"],
                        category=r["category"],
                        subcategory=r["subcategory"],
                        txn_type=r["txn_type"],
                        flow_type=r["flow_type"],
                        is_subscription=bool(r["is_subscription"] or 0),
                        income_category=r["income_category"],
                        account_no=account_no,
                        card_no=card_no,
                        excluded=(
                            bool(account_no and account_no in excluded_accounts)
                            or bool(card_no and card_no in excluded_cards)
                        ),
                        auto_excluded=bool(r["auto_excluded"] or 0),
                        splits_overwrite=r["splits_overwrite"],
                    ))
                build_ms = (time.perf_counter() - build_started) * 1000
                perf_log.info(
                    "event=transactions.stats section=query user_id=%s bank=%s kind=%s rows=%s duration_ms=%.1f excluded_ms=%.1f build_ms=%.1f",
                    user_id, bank, kind, len(rows), query_ms, excluded_ms, build_ms,
                )
            bank_ms = (time.perf_counter() - con_started) * 1000
            perf_log.info(
                "event=transactions.stats section=bank_meta user_id=%s bank=%s duration_ms=%.1f open_ms=%.1f tables_ms=%.1f columns_ms=%.1f rows=%s",
                user_id, bank, bank_ms, open_ms, tables_ms, columns_ms, len(out),
            )
            return out
        finally:
            con.close()

    def get_txn(
        self,
        *,
        bank: str,
        kind: str,
        txn_id: int,
        user_id: int,
    ) -> TxnRow | None:
        """單筆 txn (給 GET detail + PATCH 前置 lookup 用)."""
        if kind not in _KIND_TO_TABLE:
            return None
        table = _KIND_TO_TABLE[kind]
        con = db.open_bank_conn(bank)
        if con is None:
            return None
        try:
            from backend.core import bank_data
            if not bank_data.has_table(con, table):
                return None
            row = con.execute(
                f"SELECT * FROM {table} WHERE id = ? AND user_id = ?",
                (txn_id, user_id),
            ).fetchone()
            if row is None:
                return None
            raw = _row_to_dict(row)
            return TxnRow(bank=bank, kind=kind, table=table, **raw)
        finally:
            con.close()

    def list_tag_aggregates_for_bank(
        self,
        *,
        bank: str,
        user_id: int,
    ) -> list[TagAggRow]:
        """掃該 bank 3 表 tags_overwrite + date 欄 (給 tags/popular 用).

        Router 端做 aggregate + sort (因為跨 bank merge).
        沒 tags_overwrite 欄 → 該表跳過.
        """
        con = db.open_bank_conn(bank)
        if con is None:
            return []
        try:
            from backend.core import bank_data
            out: list[TagAggRow] = []
            for kind, table in _KIND_TO_TABLE.items():
                if not bank_data.has_table(con, table):
                    continue
                cols = self._columns(con, table)
                if "tags_overwrite" not in cols:
                    continue
                date_col = _KIND_TO_DATE_COL[kind]
                rows = con.execute(
                    f"SELECT tags_overwrite, {date_col} AS date FROM {table} "
                    f"WHERE user_id = ? AND tags_overwrite IS NOT NULL AND tags_overwrite != ''",
                    (user_id,),
                ).fetchall()
                for r in rows:
                    out.append(TagAggRow(
                        tags_overwrite=r["tags_overwrite"],
                        date=r["date"],
                    ))
            return out
        finally:
            con.close()

    def list_category_names(self, *, bank: str, user_id: int) -> list[str]:
        """Read distinct category values without requiring optional txn columns."""
        con = db.open_bank_conn(bank)
        if con is None:
            return []
        try:
            names: set[str] = set()
            for table in _CATEGORIZED_TABLES:
                if not self._has_table(con, table):
                    continue
                if "category" not in self._columns(con, table):
                    continue
                rows = con.execute(
                    f"SELECT DISTINCT category FROM {table} "
                    "WHERE user_id=? AND category IS NOT NULL AND category != ''",
                    (user_id,),
                ).fetchall()
                names.update(row["category"] for row in rows)
            return sorted(names)
        finally:
            con.close()

    def list_subcategory_names(
        self,
        *,
        bank: str,
        user_id: int,
        category: str,
    ) -> list[str]:
        """Read distinct subcategories scoped to one category."""
        con = db.open_bank_conn(bank)
        if con is None:
            return []
        try:
            names: set[str] = set()
            for table in _CATEGORIZED_TABLES:
                if not self._has_table(con, table):
                    continue
                cols = self._columns(con, table)
                if "category" not in cols or "subcategory" not in cols:
                    continue
                rows = con.execute(
                    f"SELECT DISTINCT subcategory FROM {table} "
                    "WHERE user_id=? AND category=? "
                    "AND subcategory IS NOT NULL AND subcategory != ''",
                    (user_id, category),
                ).fetchall()
                names.update(row["subcategory"] for row in rows)
            return sorted(names)
        finally:
            con.close()

    def list_txns_for_recategorize(
        self,
        *,
        bank: str,
        user_id: int,
    ) -> list[dict[str, Any]]:
        """Recategorize 用 — 跨 3 表收 (id, description, category, subcategory,
        auto_excluded, counterparty_acct, memo, table).

        twd 才有 counterparty_acct/memo; card 兩表補 NULL.
        Caller (router) 跑 categorizer 後再 call batch_update_categorization.
        """
        con = db.open_bank_conn(bank)
        if con is None:
            return []
        try:
            from backend.core import bank_data
            out: list[dict[str, Any]] = []
            for table in _CATEGORIZED_TABLES:
                if not bank_data.has_table(con, table):
                    continue
                if table == "twd_transactions":
                    sql = (
                        f"SELECT id, description, category, subcategory, "
                        f"COALESCE(auto_excluded, 0) AS auto_excluded, "
                        f"counterparty_acct, memo FROM {table} WHERE user_id = ?"
                    )
                else:
                    sql = (
                        f"SELECT id, description, category, subcategory, "
                        f"COALESCE(auto_excluded, 0) AS auto_excluded, "
                        f"NULL AS counterparty_acct, NULL AS memo FROM {table} WHERE user_id = ?"
                    )
                try:
                    rows = con.execute(sql, (user_id,)).fetchall()
                except db.OperationalError:
                    continue
                for r in rows:
                    d = _row_to_dict(r)
                    d["table"] = table
                    out.append(d)
            return out
        finally:
            con.close()

    def ensure_recategorize_columns(
        self,
        *,
        bank: str,
    ) -> None:
        """老 schema 沒 category/subcategory/auto_excluded 欄 → ALTER TABLE.

        Router 端 recategorize 前 call 一次 (per bank). Idempotent.
        """
        con = db.open_bank_conn(bank)
        if con is None:
            return
        try:
            from backend.core import bank_data
            for table in _CATEGORIZED_TABLES:
                if not bank_data.has_table(con, table):
                    continue
                cols = self._columns(con, table)
                try:
                    if "category" not in cols:
                        con.execute(f"ALTER TABLE {table} ADD COLUMN category TEXT")
                    if "subcategory" not in cols:
                        con.execute(f"ALTER TABLE {table} ADD COLUMN subcategory TEXT")
                    if "auto_excluded" not in cols:
                        con.execute(
                            f"ALTER TABLE {table} ADD COLUMN auto_excluded INTEGER NOT NULL DEFAULT 0",
                        )
                except db.OperationalError:
                    pass
            con.commit()
        finally:
            con.close()


# ============================================================
# TransactionsWriteMixin
# ============================================================


class TransactionsWriteMixin(_BaseHelpers):
    """Transactions write methods (under transaction scope)."""

    _con: Any
    _bank: str

    def category_snapshot(self, *, user_id: int, name: str) -> list[dict[str, Any]]:
        """Capture only category fields that actually exist and can be mutated."""
        rows: list[dict[str, Any]] = []
        for table in _CATEGORIZED_TABLES:
            if not self._has_table(self._con, table):
                continue
            cols = self._columns(self._con, table)
            if "category" not in cols:
                continue
            selected = ["id", "category"]
            if "subcategory" in cols:
                selected.append("subcategory")
            for row in self._con.execute(
                f"SELECT {', '.join(selected)} FROM {table} "
                "WHERE user_id=? AND category=?",
                (user_id, name),
            ).fetchall():
                snapshot = _row_to_dict(row)
                snapshot["table"] = table
                rows.append(snapshot)
        return rows

    def restore_category_snapshot(
        self,
        *,
        user_id: int,
        snapshots: list[dict[str, Any]],
    ) -> int:
        """Restore exact category fields without touching absent optional columns."""
        restored = 0
        for snapshot in snapshots:
            table = snapshot["table"]
            if table not in _CATEGORIZED_TABLES or not self._has_table(self._con, table):
                continue
            cols = self._columns(self._con, table)
            if "category" not in cols:
                continue
            sets = ["category=?"]
            params: list[Any] = [snapshot["category"]]
            if "subcategory" in snapshot and "subcategory" in cols:
                sets.append("subcategory=?")
                params.append(snapshot["subcategory"])
            params.extend([snapshot["id"], user_id])
            cur = self._con.execute(
                f"UPDATE {table} SET {', '.join(sets)} WHERE id=? AND user_id=?",
                tuple(params),
            )
            restored += cur.rowcount
        return restored

    def replace_category(
        self,
        *,
        user_id: int,
        old_name: str,
        new_name: str | None,
    ) -> int:
        """Rename or clear a category across all categorized tables for one user."""
        changed = 0
        for table in _CATEGORIZED_TABLES:
            if not self._has_table(self._con, table):
                continue
            cols = self._columns(self._con, table)
            if "category" not in cols:
                continue
            if new_name is None:
                subcategory_set = ", subcategory=NULL" if "subcategory" in cols else ""
                cur = self._con.execute(
                    f"UPDATE {table} SET category=NULL{subcategory_set} "
                    "WHERE user_id=? AND category=?",
                    (user_id, old_name),
                )
            else:
                cur = self._con.execute(
                    f"UPDATE {table} SET category=? WHERE user_id=? AND category=?",
                    (new_name, user_id, old_name),
                )
            changed += cur.rowcount
        return changed

    def subcategory_snapshot(
        self,
        *,
        user_id: int,
        category: str,
        name: str,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for table in _CATEGORIZED_TABLES:
            if not self._has_table(self._con, table):
                continue
            cols = self._columns(self._con, table)
            if "category" not in cols or "subcategory" not in cols:
                continue
            for row in self._con.execute(
                f"SELECT id, subcategory FROM {table} "
                "WHERE user_id=? AND category=? AND subcategory=?",
                (user_id, category, name),
            ).fetchall():
                snapshot = _row_to_dict(row)
                snapshot["table"] = table
                rows.append(snapshot)
        return rows

    def restore_subcategory_snapshot(
        self,
        *,
        user_id: int,
        snapshots: list[dict[str, Any]],
    ) -> int:
        restored = 0
        for snapshot in snapshots:
            table = snapshot["table"]
            if table not in _CATEGORIZED_TABLES or not self._has_table(self._con, table):
                continue
            if "subcategory" not in self._columns(self._con, table):
                continue
            cur = self._con.execute(
                f"UPDATE {table} SET subcategory=? WHERE id=? AND user_id=?",
                (snapshot["subcategory"], snapshot["id"], user_id),
            )
            restored += cur.rowcount
        return restored

    def replace_subcategory(
        self,
        *,
        user_id: int,
        category: str,
        old_name: str,
        new_name: str | None,
    ) -> int:
        changed = 0
        for table in _CATEGORIZED_TABLES:
            if not self._has_table(self._con, table):
                continue
            cols = self._columns(self._con, table)
            if "category" not in cols or "subcategory" not in cols:
                continue
            cur = self._con.execute(
                f"UPDATE {table} SET subcategory=? "
                "WHERE user_id=? AND category=? AND subcategory=?",
                (new_name, user_id, category, old_name),
            )
            changed += cur.rowcount
        return changed

    def tag_snapshot(self, *, user_id: int, name: str) -> list[dict[str, Any]]:
        """Capture exact JSON before mutating rows containing one hashtag."""
        rows: list[dict[str, Any]] = []
        for table in _CATEGORIZED_TABLES:
            if not self._has_table(self._con, table):
                continue
            if "tags_overwrite" not in self._columns(self._con, table):
                continue
            for row in self._con.execute(
                f"SELECT id, tags_overwrite FROM {table} "
                "WHERE user_id=? AND tags_overwrite IS NOT NULL AND tags_overwrite != ''",
                (user_id,),
            ).fetchall():
                if name not in _parse_tags_raw(row["tags_overwrite"]):
                    continue
                rows.append({
                    "table": table,
                    "id": row["id"],
                    "tags_overwrite": row["tags_overwrite"],
                })
        return rows

    def restore_tag_snapshot(
        self,
        *,
        user_id: int,
        snapshots: list[dict[str, Any]],
    ) -> int:
        restored = 0
        for snapshot in snapshots:
            table = snapshot["table"]
            if table not in _CATEGORIZED_TABLES or not self._has_table(self._con, table):
                continue
            if "tags_overwrite" not in self._columns(self._con, table):
                continue
            cur = self._con.execute(
                f"UPDATE {table} SET tags_overwrite=? WHERE id=? AND user_id=?",
                (snapshot["tags_overwrite"], snapshot["id"], user_id),
            )
            restored += cur.rowcount
        return restored

    def replace_tag(
        self,
        *,
        user_id: int,
        old_name: str,
        new_name: str | None,
    ) -> int:
        """Rename/remove a hashtag and case-insensitively deduplicate each row."""
        changed = 0
        for snapshot in self.tag_snapshot(user_id=user_id, name=old_name):
            tags = _parse_tags_raw(snapshot["tags_overwrite"])
            final: list[str] = []
            seen: set[str] = set()
            for tag in tags:
                candidate = new_name if tag == old_name else tag
                if candidate is None or candidate.lower() in seen:
                    continue
                final.append(candidate)
                seen.add(candidate.lower())
            self._con.execute(
                f"UPDATE {snapshot['table']} SET tags_overwrite=? WHERE id=? AND user_id=?",
                (
                    json.dumps(final, ensure_ascii=False) if final else None,
                    snapshot["id"],
                    user_id,
                ),
            )
            changed += 1
        return changed

    def update_txn(
        self,
        *,
        kind: str,
        txn_id: int,
        user_id: int,
        category: Any = UNSET,
        subcategory: Any = UNSET,
        description_overwrite: Any = UNSET,
        tags: Any = UNSET,
        tags_mode: str = "replace",  # 'replace' or 'add'
        auto_excluded: Any = UNSET,
        splits: Any = UNSET,
    ) -> TxnUpdateResult:
        """Update a single txn row. Each kwarg uses UNSET sentinel as default
        so caller can distinguish 'not given' from 'set to None' (clear).

        Validation rules (raise TxnColumnMissing for HTTP 409):
          - subcategory/description_overwrite/category require column exists
          - tags / auto_excluded auto-ALTER if missing
          - 'add' mode merges current + new (case-insensitive dedup)

        Raises TxnNotFound if row doesn't exist or belongs to different user.
        """
        if kind not in _KIND_TO_TABLE:
            raise ValueError(f"unsupported kind: {kind}")
        table = _KIND_TO_TABLE[kind]

        cols = self._columns(self._con, table)
        if "category" not in cols:
            raise TxnColumnMissing(self._bank, table, "category")

        # Lookup row (also for 'add' tags merge)
        row = self._con.execute(
            f"SELECT * FROM {table} WHERE id = ? AND user_id = ?",
            (txn_id, user_id),
        ).fetchone()
        if row is None:
            raise TxnNotFound(self._bank, kind, txn_id)

        sets: list[str] = []
        params: list[Any] = []

        if category is not UNSET:
            sets.append("category = ?")
            params.append(category if category else None)

        if subcategory is not UNSET:
            if "subcategory" not in cols:
                raise TxnColumnMissing(self._bank, table, "subcategory")
            sets.append("subcategory = ?")
            params.append(subcategory if subcategory else None)

        if description_overwrite is not UNSET:
            if "description_overwrite" not in cols:
                raise TxnColumnMissing(self._bank, table, "description_overwrite")
            sets.append("description_overwrite = ?")
            params.append(description_overwrite if description_overwrite else None)

        if tags is not UNSET:
            if "tags_overwrite" not in cols:
                self._con.execute(f"ALTER TABLE {table} ADD COLUMN tags_overwrite TEXT")
                cols = self._columns(self._con, table)
            new_tags: list[str] = list(tags) if tags else []
            if tags_mode == "replace":
                final_tags = new_tags
            elif tags_mode == "add":
                # merge with existing, case-insensitive dedup
                current_raw = row["tags_overwrite"] if "tags_overwrite" in row.keys() else None  # noqa: SIM118
                current_tags = _parse_tags_raw(current_raw)
                seen = {t.lower() for t in current_tags}
                for t in new_tags:
                    if t.lower() not in seen:
                        current_tags.append(t)
                        seen.add(t.lower())
                final_tags = current_tags
            else:
                raise ValueError(f"unsupported tags_mode: {tags_mode}")
            sets.append("tags_overwrite = ?")
            params.append(
                json.dumps(final_tags, ensure_ascii=False) if final_tags else None,
            )

        if auto_excluded is not UNSET:
            if "auto_excluded" not in cols:
                self._con.execute(
                    f"ALTER TABLE {table} ADD COLUMN auto_excluded INTEGER NOT NULL DEFAULT 0",
                )
            sets.append("auto_excluded = ?")
            params.append(1 if auto_excluded else 0)

        if splits is not UNSET:
            # Phase 10 (2026-07-29) 分類拆帳. Router 已驗過 shape + 金額總和,
            # 這層只負責序列化。空 list / None → NULL (取消拆帳, 回歸母筆統計).
            if "splits_overwrite" not in cols:
                self._con.execute(
                    f"ALTER TABLE {table} ADD COLUMN splits_overwrite TEXT",
                )
            sets.append("splits_overwrite = ?")
            params.append(
                json.dumps(splits, ensure_ascii=False) if splits else None,
            )

        if not sets:
            # No-op update — return current row
            d = _row_to_dict(row)
            return TxnUpdateResult(bank=self._bank, kind=kind, table=table, **d)

        params.append(txn_id)
        params.append(user_id)
        self._con.execute(
            f"UPDATE {table} SET {', '.join(sets)} WHERE id = ? AND user_id = ?",
            tuple(params),
        )

        # Re-fetch updated row
        new_row = self._con.execute(
            f"SELECT * FROM {table} WHERE id = ? AND user_id = ?",
            (txn_id, user_id),
        ).fetchone()
        d = _row_to_dict(new_row)
        return TxnUpdateResult(bank=self._bank, kind=kind, table=table, **d)

    def batch_update_categorization(
        self,
        *,
        user_id: int,
        updates: list[dict[str, Any]],
    ) -> int:
        """Recategorize batch — write back updated (category, subcategory, auto_excluded).

        Each `updates` entry: {table, id, category, subcategory, auto_excluded}.
        Returns count of rows actually changed (cat/sub/auto_ex 任一不同就算).

        Caller is responsible for skipping no-change updates if it wants to
        track changed count; this method just executes all UPDATEs blindly
        and counts how many rows the DB reports as changed.
        """
        changed = 0
        for u in updates:
            tbl = u["table"]
            if tbl not in _CATEGORIZED_TABLES:
                continue
            row_id = u["id"]
            cat = u.get("category")
            sub = u.get("subcategory")
            auto_ex = u.get("auto_excluded", 0)
            # 比舊值
            old = self._con.execute(
                f"SELECT category, subcategory, COALESCE(auto_excluded, 0) AS auto_excluded "
                f"FROM {tbl} WHERE id = ? AND user_id = ?",
                (row_id, user_id),
            ).fetchone()
            if old is None:
                continue
            if (
                old["category"] == cat
                and old["subcategory"] == sub
                and int(old["auto_excluded"] or 0) == int(auto_ex or 0)
            ):
                continue
            self._con.execute(
                f"UPDATE {tbl} SET category=?, subcategory=?, auto_excluded=? "
                f"WHERE id=? AND user_id=?",
                (cat, sub, int(auto_ex or 0), row_id, user_id),
            )
            changed += 1
        return changed


# ============================================================
# Helpers (module-level — referenced from mixin methods)
# ============================================================


def _parse_tags_raw(raw: Any) -> list[str]:
    """Parse tags_overwrite (JSON str / list / None) → list[str].

    Mirrors router._parse_tags_overwrite — kept here so write mixin doesn't
    pull router code.
    """
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return [str(t) for t in raw if t]
    try:
        v = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(v, list):
        return []
    return [str(t) for t in v if t]


__all__ = [
    "TagAggRow",
    "TransactionsReadMixin",
    "TransactionsWriteMixin",
    "TxnColumnMissing",
    "TxnKind",
    "TxnNotFound",
    "TxnRow",
    "TxnStatRow",
    "TxnUpdateFields",
    "TxnUpdateResult",
]
