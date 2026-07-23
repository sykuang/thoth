"""db_facade.cards — cards table domain (Plan B B1).

Pydantic models:
  CardSummary           cards row + bill summary (used in list + detail)
  BilledTxnRow          single billed txn (card detail page)
  PendingTxnRow         single pending txn (card detail page)
  PaymentRow            single payment txn (card detail page)
  CardDetail            full card detail (CardSummary + 3 lists)
  SetCardExcludedResult write return value
  SetCardNicknameResult write return value

Domain exceptions:
  CardNotFound          card not found / not owned by user

Mixins:
  CardsReadMixin        list_cards / get_card / get_card_detail /
                        list_excluded_card_nos_all_banks
  CardsWriteMixin       set_card_excluded / set_card_nickname
                        (used by _TransactionScope only)
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from backend.server import db

from ._base import _BaseHelpers


# ============================================================
# Domain exceptions
# ============================================================


class CardNotFound(Exception):
    def __init__(self, bank: str, card_no: str) -> None:
        self.bank = bank
        self.card_no = card_no
        super().__init__(f"card not found: {bank}/{card_no}")


class CardsTableMissing(Exception):
    """Bank db has no cards table (e.g. linebank pure-cash bank)."""

    def __init__(self, bank: str) -> None:
        self.bank = bank
        super().__init__(f"cards table missing in {bank}")


# ============================================================
# Pydantic result models
# ============================================================


class CardSummary(BaseModel):
    """卡片 metadata + bill summary (used in /cards list + detail)."""

    model_config = ConfigDict(extra="forbid")

    bank: str
    card_no: str
    name: str | None = None
    nickname_overwrite: str | None = None
    association: str | None = None
    type: str | None = None
    is_cube: bool = False
    excluded: bool = False
    active: bool = True
    credit_limit: float | None = None
    used_credit: float | None = None
    statement_close_date: str | None = None
    payment_due_date: str | None = None
    updated_at: str | None = None
    bill_due_amount: float = 0.0
    unbilled_amount: float = 0.0
    last_payment_date: str | None = None
    last_payment_amount: float | None = None


class BilledTxnRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str | None
    post_date: str | None = None
    amount: float
    description: str
    currency: str | None = None
    category: str | None = None
    subcategory: str | None = None
    txn_type: str | None = None
    flow_type: str | None = None


class PendingTxnRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str | None
    amount: float
    description: str
    currency: str | None = None
    category: str | None = None
    subcategory: str | None = None


class PaymentRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str
    amount: float
    description: str


class CardDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    card: CardSummary
    billed_txns: list[BilledTxnRow] = []
    pending_txns: list[PendingTxnRow] = []
    payments: list[PaymentRow] = []


class SetCardExcludedResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bank: str
    card_no: str
    excluded: bool
    updated_at: str


class SetCardNicknameResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bank: str
    card_no: str
    nickname_overwrite: str | None
    updated_at: str


# ============================================================
# Internal helpers (cards-specific)
# ============================================================


def _nick_col_expr(con: Any, helpers: _BaseHelpers) -> str:
    cols = helpers._columns(con, "cards")
    return "nickname_overwrite" if "nickname_overwrite" in cols else "NULL AS nickname_overwrite"


def _row_to_card_dict(bank: str, row: Any) -> dict[str, Any]:
    keys = set(row.keys())

    def _get(col: str, default: Any = None) -> Any:
        return row[col] if col in keys else default

    return {
        "bank": bank,
        "card_no": _get("card_no") or "",
        "name": _get("name"),
        "nickname_overwrite": _get("nickname_overwrite"),
        "association": _get("association"),
        "type": _get("type"),
        "is_cube": bool(_get("is_cube")) if _get("is_cube") is not None else False,
        "excluded": bool(_get("excluded", 0)),
        "active": bool(_get("active", 1)),
        "credit_limit": float(_get("credit_limit")) if _get("credit_limit") is not None else None,
        "used_credit": float(_get("used_credit")) if _get("used_credit") is not None else None,
        "statement_close_date": _get("statement_close_date"),
        "payment_due_date": _get("payment_due_date"),
        "updated_at": _get("updated_at"),
    }


def _bill_summary_for_cards(
    helpers: _BaseHelpers, con: Any, user_id: int, card_nos: list[str]
) -> dict[str, dict[str, Any]]:
    """產出每張卡的 bill_due_amount / unbilled_amount / last_payment_* summary.

    2026-06-20 升級 (HSBC bill_due 1.3M bug 修): 先讀 cards 表 native 欄
    (HSBC API 直給的「本期應繳/最近繳款」)，再走 card_billed_txns derive 路徑
    當 fallback. native 為主, derive 補 NULL 那些卡.
    """
    summary: dict[str, dict[str, Any]] = {
        no: {
            "bill_due_amount": 0.0,
            "unbilled_amount": 0.0,
            "last_payment_date": None,
            "last_payment_amount": None,
        }
        for no in card_nos
    }
    if not card_nos:
        return summary

    # === Step 0 (2026-06-20): 讀 cards 表 native 欄作為初始值 ===
    # HSBC persist 直接從 HSBC API card_detail.details[] 抽 Last Statement Amount /
    # Last Payment Amount / Last Payment Date 寫進 cards 表新欄.
    # 其他銀行未抽 → 三欄都 NULL → 走下面 derive fallback.
    native_by_card: dict[str, dict[str, Any]] = {}
    if helpers._has_table(con, "cards"):
        cards_cols = helpers._columns(con, "cards")
        if "bill_due_amount" in cards_cols:
            placeholders = ",".join(["?"] * len(card_nos))
            for r in con.execute(
                f"""SELECT card_no, bill_due_amount, last_payment_amount, last_payment_date
                    FROM cards
                    WHERE user_id = ? AND card_no IN ({placeholders})""",
                (user_id, *card_nos),
            ):
                native_by_card[r["card_no"]] = {
                    "bill_due_amount": r["bill_due_amount"],
                    "last_payment_amount": r["last_payment_amount"],
                    "last_payment_date": r["last_payment_date"],
                }

    if helpers._has_table(con, "card_billed_txns"):
        cols = helpers._columns(con, "card_billed_txns")
        if {"card_no", "bill_date", "amount"}.issubset(cols):
            latest_by_card: dict[str, str | None] = {}
            for r in con.execute(
                """SELECT COALESCE(NULLIF(card_no, ''), '__bank__') AS card_key,
                          MAX(bill_date) AS latest_bill_date
                   FROM card_billed_txns
                   WHERE user_id = ?
                   GROUP BY COALESCE(NULLIF(card_no, ''), '__bank__')""",
                (user_id,),
            ):
                latest_by_card[r["card_key"]] = r["latest_bill_date"]

            for card_key, latest_bill_date in latest_by_card.items():
                if card_key == "__bank__":
                    if len(card_nos) != 1:
                        continue
                    target = card_nos[0]
                    rows = con.execute(
                        """SELECT amount FROM card_billed_txns
                           WHERE user_id = ?
                             AND (card_no IS NULL OR card_no = '')
                             AND (bill_date IS ? OR bill_date = ?)""",
                        (user_id, latest_bill_date, latest_bill_date),
                    )
                else:
                    if card_key not in summary:
                        continue
                    target = card_key
                    rows = con.execute(
                        """SELECT amount FROM card_billed_txns
                           WHERE user_id = ?
                             AND card_no = ?
                             AND (bill_date IS ? OR bill_date = ?)""",
                        (user_id, card_key, latest_bill_date, latest_bill_date),
                    )
                summary[target]["bill_due_amount"] = sum(
                    helpers._positive(r["amount"]) for r in rows
                )

        if "txn_type" in cols:
            payment_date_expr = (
                "COALESCE(NULLIF(post_date, ''), consume_date)"
                if "post_date" in cols
                else "consume_date"
            )
            seen: set[str] = set()
            for r in con.execute(
                f"""SELECT COALESCE(NULLIF(card_no, ''), '__bank__') AS card_key,
                           SUBSTR({payment_date_expr}, 1, 10) AS payment_date, amount
                    FROM card_billed_txns
                    WHERE user_id = ?
                      AND txn_type = 'payment'
                      AND {payment_date_expr} IS NOT NULL
                      AND {payment_date_expr} != ''
                    ORDER BY {payment_date_expr} DESC, id DESC""",
                (user_id,),
            ):
                card_key = r["card_key"]
                if card_key in seen:
                    continue
                seen.add(card_key)
                if card_key == "__bank__":
                    if len(card_nos) != 1:
                        continue
                    target = card_nos[0]
                else:
                    if card_key not in summary:
                        continue
                    target = card_key
                try:
                    amt = float(r["amount"])
                except (TypeError, ValueError):
                    amt = 0.0
                summary[target]["last_payment_date"] = r["payment_date"]
                summary[target]["last_payment_amount"] = abs(amt) if amt else 0.0

    if helpers._has_table(con, "card_pending_txns"):
        cols = helpers._columns(con, "card_pending_txns")
        if {"card_no", "amount"}.issubset(cols):
            for r in con.execute(
                """SELECT COALESCE(NULLIF(card_no, ''), '__bank__') AS card_key,
                          SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) AS amount_sum
                   FROM card_pending_txns
                   WHERE user_id = ?
                   GROUP BY COALESCE(NULLIF(card_no, ''), '__bank__')""",
                (user_id,),
            ):
                card_key = r["card_key"]
                if card_key == "__bank__":
                    if len(card_nos) != 1:
                        continue
                    target = card_nos[0]
                else:
                    if card_key not in summary:
                        continue
                    target = card_key
                summary[target]["unbilled_amount"] = float(r["amount_sum"] or 0)

    # === Native overlay (2026-06-20): cards 表 native 欄優先, derive 退為 fallback ===
    # 邏輯: native 不是 NULL → 蓋過 derive 算出的值 (HSBC 「本期應繳」 71,032 蓋 derive 1.3M).
    # native 是 NULL → 保留 derive 算出的值 (其他銀行如 cathay 不變).
    for card_no, native in native_by_card.items():
        if card_no not in summary:
            continue
        if native.get("bill_due_amount") is not None:
            summary[card_no]["bill_due_amount"] = float(native["bill_due_amount"])
        if native.get("last_payment_amount") is not None:
            summary[card_no]["last_payment_amount"] = float(native["last_payment_amount"])
        if native.get("last_payment_date"):
            summary[card_no]["last_payment_date"] = native["last_payment_date"]

    return summary


# ============================================================
# CardsReadMixin — mixed into Database
# ============================================================


class CardsReadMixin(_BaseHelpers):
    """Read-only cards methods. Each opens + closes its own connection."""

    def list_cards(
        self,
        *,
        bank: str,
        user_id: int,
        include_inactive: bool = False,
    ) -> list[CardSummary]:
        """單一 bank — 該 user 所有卡片 (含 bill summary).

        老 schema 沒 nickname_overwrite/active 欄走 NULL/COALESCE placeholder.
        Bank db 不存在 / 沒 cards 表 → 回 [] (跨銀行聚合時 caller 不希望 raise).
        """
        con = db.open_bank_conn(bank)
        if con is None:
            return []
        try:
            if not self._has_table(con, "cards"):
                return []
            nick_col = _nick_col_expr(con, self)
            where_extra = "" if include_inactive else " AND COALESCE(active, 1) = 1"
            # 2026-06-20: 新欄 bill_due_amount/last_payment_amount/last_payment_date
            #   用 _has_column 判, 沒有的舊 schema 走 NULL placeholder (向後相容)
            cols = self._columns(con, "cards")
            bill_due_col = "bill_due_amount" if "bill_due_amount" in cols else "NULL AS bill_due_amount"
            lp_amt_col = "last_payment_amount" if "last_payment_amount" in cols else "NULL AS last_payment_amount"
            lp_date_col = "last_payment_date" if "last_payment_date" in cols else "NULL AS last_payment_date"
            sql = (
                f"SELECT card_no, name, {nick_col}, association, type, is_cube,"
                f"       COALESCE(excluded, 0) AS excluded,"
                f"       COALESCE(active, 1) AS active,"
                f"       credit_limit, used_credit,"
                f"       statement_close_date, payment_due_date,"
                f"       {bill_due_col}, {lp_amt_col}, {lp_date_col},"
                f"       updated_at"
                f"  FROM cards"
                f" WHERE user_id = ?{where_extra}"
                f" ORDER BY name"
            )
            rows = list(con.execute(sql, (user_id,)))
            card_nos = [r["card_no"] for r in rows if r["card_no"]]
            summaries = _bill_summary_for_cards(self, con, user_id, card_nos)
            result: list[CardSummary] = []
            for r in rows:
                base = _row_to_card_dict(bank, r)
                bs = summaries.get(base["card_no"], {})
                result.append(CardSummary(**base, **bs))
            return result
        finally:
            con.close()

    def get_card(
        self,
        *,
        bank: str,
        user_id: int,
        card_no: str,
    ) -> CardSummary | None:
        """單張卡 metadata + bill summary. 找不到 → None."""
        con = db.open_bank_conn(bank)
        if con is None:
            return None
        try:
            if not self._has_table(con, "cards"):
                return None
            nick_col = _nick_col_expr(con, self)
            # 2026-06-20: 新欄向後相容 (老 SQLite 無此欄走 NULL)
            cols = self._columns(con, "cards")
            bill_due_col = "bill_due_amount" if "bill_due_amount" in cols else "NULL AS bill_due_amount"
            lp_amt_col = "last_payment_amount" if "last_payment_amount" in cols else "NULL AS last_payment_amount"
            lp_date_col = "last_payment_date" if "last_payment_date" in cols else "NULL AS last_payment_date"
            sql = (
                f"SELECT card_no, name, {nick_col}, association, type, is_cube,"
                f"       COALESCE(excluded, 0) AS excluded,"
                f"       COALESCE(active, 1) AS active,"
                f"       credit_limit, used_credit,"
                f"       statement_close_date, payment_due_date,"
                f"       {bill_due_col}, {lp_amt_col}, {lp_date_col},"
                f"       updated_at"
                f"  FROM cards"
                f" WHERE card_no = ? AND user_id = ?"
            )
            row = con.execute(sql, (card_no, user_id)).fetchone()
            if row is None:
                return None
            base = _row_to_card_dict(bank, row)
            summaries = _bill_summary_for_cards(self, con, user_id, [card_no])
            bs = summaries.get(card_no, {})
            return CardSummary(**base, **bs)
        finally:
            con.close()

    def get_card_detail(
        self,
        *,
        bank: str,
        user_id: int,
        card_no: str,
        cycle_start: str,
        cycle_end: str | None = None,
    ) -> CardDetail | None:
        """單張卡 detail (含本期 billed/pending + 最近 12 筆 payments).

        cycle_start / cycle_end 由 caller 算 (純 datetime, UI policy).
        有 cycle_end 時使用 (cycle_start, cycle_end] 的入帳日範圍。
        找不到卡 → None.
        """
        con = db.open_bank_conn(bank)
        if con is None:
            return None
        try:
            if not self._has_table(con, "cards"):
                return None
            nick_col = _nick_col_expr(con, self)
            # 2026-06-20: 新欄向後相容 (老 SQLite 無此欄走 NULL)
            cols = self._columns(con, "cards")
            bill_due_col = "bill_due_amount" if "bill_due_amount" in cols else "NULL AS bill_due_amount"
            lp_amt_col = "last_payment_amount" if "last_payment_amount" in cols else "NULL AS last_payment_amount"
            lp_date_col = "last_payment_date" if "last_payment_date" in cols else "NULL AS last_payment_date"
            sql = (
                f"SELECT card_no, name, {nick_col}, association, type, is_cube,"
                f"       COALESCE(excluded, 0) AS excluded,"
                f"       COALESCE(active, 1) AS active,"
                f"       credit_limit, used_credit,"
                f"       statement_close_date, payment_due_date,"
                f"       {bill_due_col}, {lp_amt_col}, {lp_date_col},"
                f"       updated_at"
                f"  FROM cards"
                f" WHERE card_no = ? AND user_id = ?"
            )
            row = con.execute(sql, (card_no, user_id)).fetchone()
            if row is None:
                return None
            base = _row_to_card_dict(bank, row)
            summaries = _bill_summary_for_cards(self, con, user_id, [card_no])
            bs = summaries.get(card_no, {})
            card_summary = CardSummary(**base, **bs)

            billed_txns: list[BilledTxnRow] = []
            if self._has_table(con, "card_billed_txns"):
                cb_cols = self._columns(con, "card_billed_txns")
                txn_col = ", txn_type" if "txn_type" in cb_cols else ", NULL AS txn_type"
                flow_col = ", flow_type" if "flow_type" in cb_cols else ", NULL AS flow_type"
                category_col = ", category" if "category" in cb_cols else ", NULL AS category"
                subcategory_col = ", subcategory" if "subcategory" in cb_cols else ", NULL AS subcategory"
                date_value_expr = (
                    "COALESCE(NULLIF(post_date, ''), consume_date)"
                    if "post_date" in cb_cols
                    else "consume_date"
                )
                # Bank dates can be ISO timestamps; compare their YYYY-MM-DD
                # prefix so a closing-day row is included by a date-only bound.
                date_expr = f"SUBSTR({date_value_expr}, 1, 10)"
                if cycle_end is None:
                    # Preserve legacy callers: consume-date basis and inclusive start.
                    cycle_where = "SUBSTR(consume_date, 1, 10) >= ?"
                else:
                    cycle_where = f"{date_expr} > ? AND {date_expr} <= ?"
                cycle_params: tuple[Any, ...] = (user_id, card_no, cycle_start)
                if cycle_end is not None:
                    cycle_params += (cycle_end,)
                for r in con.execute(
                    f"""SELECT consume_date, post_date, amount, description, currency
                              {txn_col}{flow_col}{category_col}{subcategory_col}
                       FROM card_billed_txns
                       WHERE user_id = ? AND card_no = ? AND {cycle_where}
                       ORDER BY {date_expr} DESC, id DESC""",
                    cycle_params,
                ):
                    r_keys = set(r.keys())
                    billed_txns.append(BilledTxnRow(
                        date=r["consume_date"],
                        post_date=r["post_date"] if "post_date" in r_keys else None,
                        amount=float(r["amount"]) if r["amount"] is not None else 0.0,
                        description=r["description"] or "",
                        currency=r["currency"] if "currency" in r_keys else None,
                        category=r["category"],
                        subcategory=r["subcategory"],
                        txn_type=r["txn_type"],
                        flow_type=r["flow_type"],
                    ))

            pending_txns: list[PendingTxnRow] = []
            if self._has_table(con, "card_pending_txns"):
                cp_cols = self._columns(con, "card_pending_txns")
                category_col = ", category" if "category" in cp_cols else ", NULL AS category"
                subcategory_col = ", subcategory" if "subcategory" in cp_cols else ", NULL AS subcategory"
                for r in con.execute(
                    f"""SELECT consume_date, amount, description, currency
                              {category_col}{subcategory_col}
                       FROM card_pending_txns
                       WHERE user_id = ? AND card_no = ?
                       ORDER BY consume_date DESC, id DESC""",
                    (user_id, card_no),
                ):
                    pending_txns.append(PendingTxnRow(
                        date=r["consume_date"],
                        amount=float(r["amount"]) if r["amount"] is not None else 0.0,
                        description=r["description"] or "",
                        currency=r["currency"] if "currency" in cp_cols else None,
                        category=r["category"],
                        subcategory=r["subcategory"],
                    ))

            payments: list[PaymentRow] = []
            if self._has_table(con, "card_billed_txns"):
                cb_cols = self._columns(con, "card_billed_txns")
                if "txn_type" in cb_cols:
                    payment_date_expr = (
                        "COALESCE(NULLIF(post_date, ''), consume_date)"
                        if "post_date" in cb_cols
                        else "consume_date"
                    )
                    for r in con.execute(
                        f"""SELECT SUBSTR({payment_date_expr}, 1, 10) AS payment_date,
                                   amount, description
                           FROM card_billed_txns
                           WHERE user_id = ? AND card_no = ? AND txn_type = 'payment'
                             AND {payment_date_expr} IS NOT NULL
                             AND {payment_date_expr} != ''
                           ORDER BY {payment_date_expr} DESC, id DESC
                           LIMIT 12""",
                        (user_id, card_no),
                    ):
                        amt = float(r["amount"]) if r["amount"] is not None else 0.0
                        payments.append(PaymentRow(
                            date=r["payment_date"],
                            amount=abs(amt),
                            description=r["description"] or "",
                        ))
            if card_summary.last_payment_date and card_summary.last_payment_amount is not None:
                # Native card detail can update before the posted transaction-history
                # endpoint catches up (HSBC 2026-07-07 payment). Merge it unless an
                # identical dated amount is already present.
                native_date = card_summary.last_payment_date[:10]
                native_amount = abs(float(card_summary.last_payment_amount))
                if not any(
                    payment.date == native_date and payment.amount == native_amount
                    for payment in payments
                ):
                    payments.append(PaymentRow(
                        date=native_date,
                        amount=native_amount,
                        description="最近繳款",
                    ))
                    payments.sort(key=lambda payment: payment.date, reverse=True)
                    del payments[12:]

            return CardDetail(
                card=card_summary,
                billed_txns=billed_txns,
                pending_txns=pending_txns,
                payments=payments,
            )
        finally:
            con.close()

    def list_excluded_card_nos_all_banks(
        self,
        *,
        user_id: int,
        banks: list[str],
    ) -> dict[str, set[str]]:
        """掃指定 banks, 回 {bank: set(excluded card_no)} — limit 本 user."""
        fast = self._excluded_nos_all_banks_fast(
            table="cards", id_col="card_no", user_id=user_id, banks=banks,
        )
        if fast is not None:
            return fast
        out = {}
        for bank in banks:
            con = db.open_bank_conn(bank)
            if con is None:
                continue
            try:
                if not self._has_table(con, "cards"):
                    continue
                try:
                    rows = con.execute(
                        "SELECT card_no FROM cards WHERE user_id = ? AND COALESCE(excluded, 0) = 1",
                        (user_id,),
                    ).fetchall()
                    if rows:
                        out[bank] = {r["card_no"] for r in rows if r["card_no"]}
                except db.OperationalError:
                    pass
            finally:
                con.close()
        return out


# ============================================================
# CardsWriteMixin — mixed into _TransactionScope
# ============================================================


class CardsWriteMixin(_BaseHelpers):
    """Cards write methods. Used inside `with db_api.transaction() as tx:`."""

    # type hints — set by _TransactionScopeBase.__init__
    _con: Any
    _bank: str

    def set_card_excluded(
        self,
        *,
        user_id: int,
        card_no: str,
        excluded: bool,
    ) -> SetCardExcludedResult:
        """切換卡片「納入統計」flag. raise CardNotFound 若卡不存在."""
        row = self._con.execute(
            "SELECT card_no FROM cards WHERE card_no = ? AND user_id = ?",
            (card_no, user_id),
        ).fetchone()
        if row is None:
            raise CardNotFound(self._bank, card_no)
        now = self._now_iso()
        self._con.execute(
            "UPDATE cards SET excluded = ?, updated_at = ? WHERE card_no = ? AND user_id = ?",
            (1 if excluded else 0, now, card_no, user_id),
        )
        return SetCardExcludedResult(
            bank=self._bank, card_no=card_no, excluded=excluded, updated_at=now,
        )

    def set_card_nickname(
        self,
        *,
        user_id: int,
        card_no: str,
        nickname_overwrite: str | None,
    ) -> SetCardNicknameResult:
        """設/清 user 取的卡片暱稱. 老 db 沒此欄會 ALTER TABLE 補. raise CardNotFound."""
        cols = self._columns(self._con, "cards")
        if "nickname_overwrite" not in cols:
            self._con.execute("ALTER TABLE cards ADD COLUMN nickname_overwrite TEXT")
        row = self._con.execute(
            "SELECT card_no FROM cards WHERE card_no = ? AND user_id = ?",
            (card_no, user_id),
        ).fetchone()
        if row is None:
            raise CardNotFound(self._bank, card_no)
        now = self._now_iso()
        new_nick = nickname_overwrite if nickname_overwrite else None
        self._con.execute(
            "UPDATE cards SET nickname_overwrite = ?, updated_at = ? WHERE card_no = ? AND user_id = ?",
            (new_nick, now, card_no, user_id),
        )
        return SetCardNicknameResult(
            bank=self._bank,
            card_no=card_no,
            nickname_overwrite=new_nick,
            updated_at=now,
        )


__all__ = [
    "BilledTxnRow",
    "CardDetail",
    "CardNotFound",
    "CardSummary",
    "CardsReadMixin",
    "CardsTableMissing",
    "CardsWriteMixin",
    "PaymentRow",
    "PendingTxnRow",
    "SetCardExcludedResult",
    "SetCardNicknameResult",
]
