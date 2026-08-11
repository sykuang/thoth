"""GET /cards — Cross-bank credit card list (Plan B refactor: 全走 db_facade).

Migration history:
  - Phase 5 (Plan A pre-Plan-B): 原本 router 內 inline SQL + helper functions.
  - Plan B (2026-06-19): 全境 SQL 移到 backend.server.db_facade. router 只負責:
      * auth (current_user)
      * bank scope 解析 (account_id / bank query / user accounts fallback)
      * 翻譯 domain exception → HTTPException
      * 把 typed Pydantic model 轉成 dict (對齊 frontend contract)
      * derive 純 UI policy 欄位 (bill_status / available_credit) 從 typed model 算

設計思路:
  每家銀行各自一顆 sqlite / 一個 PG schema (backend.server.db_facade 內部 resolve).
  router 不知道 SQL 在哪裡, 只用 db_api.list_cards/get_card_detail/transaction.

Endpoints:
  GET   /cards
  GET   /cards/{bank}/{card_no}
  PATCH /cards/{bank}/{card_no}/nickname
  PATCH /cards/{bank}/{card_no}/excluded

Helper export (給 transactions router 用):
  get_excluded_card_nos(user_id) → {bank: set(card_no)}
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from backend.core import bank_data
from backend.server.deps import current_user
from backend.server.creds_store import AccountsRepo
from backend.server.db_facade import (
    BankNotAvailable,
    CardNotFound,
    CardSummary,
    db_api,
)

router = APIRouter(prefix="/cards", tags=["cards"])


KNOWN_BANKS = bank_data.KNOWN_BANKS


# ============================================================
# Bill-status derivation (純 UI policy, 從 typed CardSummary 算; 不碰 DB)
# ============================================================


def _parse_iso_date(s: str | None) -> date | None:
    """容錯 parse: 'YYYY-MM-DD' / 'YYYY/MM/DD' / ISO datetime 都接受。"""
    if not s:
        return None
    try:
        # Some crawlers (notably Fubon) persist bank-native slash dates such as
        # "2026/07/02".  Keep the router comparison tolerant so bill_status does
        # not fall back to "unknown" and let the frontend re-label paid bills as
        # overdue.
        return date.fromisoformat(s[:10].replace("/", "-"))
    except (ValueError, TypeError):
        return None


def _previous_month_same_day(value: date) -> date:
    """Return the prior calendar-month boundary, clamped for short months."""
    year, month = value.year, value.month - 1
    if month == 0:
        year, month = year - 1, 12
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def _compute_bill_status(card: CardSummary) -> str:
    """Phase 9.4 bill_status 規則 (純算術, 跟 DB 完全無關):

      no_payment_required: remaining due == 0，且沒有本期付款
      paid               : remaining due == 0，且有本期付款
      overdue            : 已過 due_date 且沒最近繳款紀錄
      due                : 還沒到 due_date (正常待繳)
      unknown            : 沒 payment_due_date 也沒 last_payment_date
    """
    due = _parse_iso_date(card.payment_due_date)
    last_pay = _parse_iso_date(card.last_payment_date)
    today = date.today()

    if card.bill_due_amount is None:
        return "unknown"
    statement_close = _parse_iso_date(card.statement_close_date)
    payment_boundary = statement_close or ((due - timedelta(days=30)) if due else None)
    if card.bill_due_amount == 0:
        if last_pay and payment_boundary and last_pay >= payment_boundary:
            return "paid"
        return "no_payment_required"

    if due is None:
        return "unknown"
    if today <= due:
        return "due"
    # today > due. A payment before the current statement close belongs to the
    # previous cycle and cannot settle the displayed statement. Keep the old
    # due-30 fallback only for banks without a statement close date.
    return "overdue"


def _card_to_response(card: CardSummary) -> dict[str, Any]:
    """CardSummary → frontend dict (含 derive 欄位: available_credit + bill_status).

    保留跟現有 frontend contract 100% 對齊的 shape (含老的 None defaults).
    """
    available_credit = (
        max(0.0, card.credit_limit - card.used_credit)
        if card.credit_limit is not None and card.used_credit is not None
        else None
    )
    return {
        "bank": card.bank,
        "card_no": card.card_no,
        "name": card.name,
        "nickname_overwrite": card.nickname_overwrite,
        "association": card.association,
        "type": card.type,
        "is_cube": card.is_cube,
        "updated_at": card.updated_at,
        "excluded": card.excluded,
        "credit_limit": card.credit_limit,
        "used_credit": card.used_credit,
        "available_credit": available_credit,
        "statement_close_date": card.statement_close_date,
        "payment_due_date": card.payment_due_date,
        "bill_due_amount": card.bill_due_amount,
        "unbilled_amount": card.unbilled_amount,
        "bill_status": _compute_bill_status(card),
        "last_payment_date": card.last_payment_date,
        "last_payment_amount": card.last_payment_amount,
        "active": card.active,
    }


# ============================================================
# Bank scope resolver (一樣的三層邏輯, 跟 transactions.py / portfolio.py 對齊)
# ============================================================


def _resolve_banks(
    user_id: int,
    bank: str | None,
    account_id: int | None,
) -> list[str]:
    """三層解析: account_id > bank > user.accounts > fallback all-with-db."""
    # 1) account_id 最強
    if account_id is not None:
        repo = AccountsRepo()
        acct = repo.get(account_id)
        if acct is None or acct.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"找不到帳號 id={account_id}",
            )
        return [acct.bank]

    # 2) bank query string (逗號分隔)
    if bank:
        banks = [b.strip().lower() for b in bank.split(",") if b.strip()]
        unknown = [b for b in banks if b not in KNOWN_BANKS]
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"不支援的銀行: {unknown}",
            )
        return banks

    # 3) user 有的 accounts
    repo = AccountsRepo()
    user_accts = repo.list_for_user(user_id)
    user_banks = sorted({a.bank for a in user_accts})
    if user_banks:
        return user_banks

    # 4) Fallback all banks with db
    return bank_data.fallback_banks_with_data()


# ============================================================
# Endpoints
# ============================================================


@router.get("")
def list_cards(
    bank: str | None = Query(None, description="逗號分隔的 bank list, e.g. 'hsbc,sinopac'"),
    account_id: int | None = Query(None, description="只看該 BankAccount 對應 bank"),
    include_inactive: bool = Query(False, description="True 帶回過期卡 (active=0). 預設 False — 過期卡 UI 不顯示但 txn 仍計算"),
    user: dict = Depends(current_user),
) -> list[dict[str, Any]]:
    """跨銀行信用卡列表.

    預設 include_inactive=False — 過期卡 (active=0) 不會在 list 出現.
    過期卡的歷史 txn 仍在 transactions / stats / current_month_spending 計算,
    這條 filter 只影響「卡片列表 UI 顯示」.
    """
    banks = _resolve_banks(user["id"], bank, account_id)
    cards: list[dict[str, Any]] = []

    for b in banks:
        for card in db_api.list_cards(
            bank=b, user_id=user["id"], include_inactive=include_inactive,
        ):
            cards.append(_card_to_response(card))

    # 排序: bank → name
    cards.sort(key=lambda c: (c["bank"], c["name"] or ""))
    return cards


@router.get("/{bank}/{card_no}")
def get_card_detail(
    bank: str,
    card_no: str,
    user: dict = Depends(current_user),
) -> dict[str, Any]:
    """單張信用卡帳單頁面 detail.

    回 shape:
      {
        ...card 主檔所有欄 (含 bill_due_amount/last_payment_date/bill_status)
        billed_txns:  list[{date, post_date, amount, description, currency,
                            category, subcategory, txn_type, flow_type}]
                       (本期帳單：上一個結帳日後、截至本次結帳日的入帳資料)
        pending_txns: list[{date, amount, description, currency, category, subcategory}]
        payments:     list[{date, amount, description}]  最近 12 筆 (txn_type=payment)
      }
    本期定義: 有 statement_close_date 時用 (前一月同日, statement_close_date]
    的 post_date 範圍；沒有才 fallback 過去 35 天至今。
    """
    if bank not in KNOWN_BANKS:
        raise HTTPException(status_code=404, detail=f"unknown bank: {bank}")

    # 算 cycle_start (純 datetime 算, 不碰 DB)
    # 先拿 card metadata 看 statement_close_date 在不在
    summary = db_api.get_card(bank=bank, user_id=user["id"], card_no=card_no)
    if summary is None:
        # 也可能 bank db 沒 cards 表 → 404 對齊原行為
        raise HTTPException(
            status_code=404, detail=f"card not found: {bank}/{card_no}",
        )

    statement_end = _parse_iso_date(summary.statement_close_date)
    if statement_end is None:
        cycle_start = (date.today() - timedelta(days=35)).isoformat()
        cycle_end = None
    else:
        # A statement dated June 18 contains rows posted after the prior close
        # through June 18. Starting at June 18 would query the next cycle.
        cycle_start = _previous_month_same_day(statement_end).isoformat()
        cycle_end = statement_end.isoformat()

    detail = db_api.get_card_detail(
        bank=bank,
        user_id=user["id"],
        card_no=card_no,
        cycle_start=cycle_start,
        cycle_end=cycle_end,
    )
    if detail is None:
        # race-condition 防護: get_card 還在但 get_card_detail 失敗 (理論不該發生)
        raise HTTPException(
            status_code=404, detail=f"card not found: {bank}/{card_no}",
        )

    # CardDetail.card → response shape, 再 append 3 個 list
    body = _card_to_response(detail.card)
    body["billed_txns"] = [t.model_dump() for t in detail.billed_txns]
    body["pending_txns"] = [t.model_dump() for t in detail.pending_txns]
    body["payments"] = [p.model_dump() for p in detail.payments]
    return body


# ============================================================
# PATCH endpoints (writes go through transaction scope)
# ============================================================


class CardExcludedPayload(BaseModel):
    excluded: bool


class CardNicknamePayload(BaseModel):
    nickname_overwrite: str | None  # None / "" → 清空 (恢復顯示 cards.name)


@router.patch("/{bank}/{card_no}/nickname")
def patch_card_nickname(
    bank: str,
    card_no: str,
    payload: CardNicknamePayload,
    user: dict = Depends(current_user),
) -> dict[str, Any]:
    """設定/清空 user 對單張信用卡的暱稱覆寫.

    鐵則 (對齊 description_overwrite):
      - cards.name 是銀行 API 原文, 重 sync 蓋, 永遠不動.
      - nickname_overwrite 是 user 在 thoth UI 取的名字, 重 sync 不動.
      - UI fallback: nickname_overwrite || name (frontend 負責).
      - payload.nickname_overwrite == None / "" → SQL 寫 NULL (恢復顯示 name).

    Return: {bank, card_no, nickname_overwrite, updated_at}
    """
    if bank not in KNOWN_BANKS:
        raise HTTPException(status_code=404, detail=f"unknown bank: {bank}")
    try:
        with db_api.transaction(bank=bank) as tx:
            result = tx.set_card_nickname(
                user_id=user["id"],
                card_no=card_no,
                nickname_overwrite=payload.nickname_overwrite,
            )
    except BankNotAvailable as e:
        raise HTTPException(
            status_code=404, detail=f"bank db not found: {e.bank}",
        ) from e
    except CardNotFound as e:
        raise HTTPException(
            status_code=404, detail=f"card not found: {e.bank}/{e.card_no}",
        ) from e
    return result.model_dump()


@router.patch("/{bank}/{card_no}/excluded")
def patch_card_excluded(
    bank: str,
    card_no: str,
    payload: CardExcludedPayload,
    user: dict = Depends(current_user),
) -> dict[str, Any]:
    """切換單一信用卡的「納入淨資產統計」flag.

    跟 accounts.excluded 同模式. 影響:
      - GET /transactions item.excluded (該卡 billed/pending 都會 true)
      - GET /transactions/stats: 該卡 txn 金額不算 (raw count 仍算)
      - GET /portfolio/summary: 本月消費 current_month_spending 跳過該卡
      - frontend cards / transactions UI 反灰

    Return: {bank, card_no, excluded, updated_at}
    """
    if bank not in KNOWN_BANKS:
        raise HTTPException(status_code=404, detail=f"unknown bank: {bank}")
    try:
        with db_api.transaction(bank=bank) as tx:
            result = tx.set_card_excluded(
                user_id=user["id"],
                card_no=card_no,
                excluded=payload.excluded,
            )
    except BankNotAvailable as e:
        raise HTTPException(
            status_code=404, detail=f"bank db not found: {e.bank}",
        ) from e
    except CardNotFound as e:
        raise HTTPException(
            status_code=404, detail=f"card not found: {e.bank}/{e.card_no}",
        ) from e
    return result.model_dump()


# ============================================================
# Helper export (給 transactions router 用)
# ============================================================


def get_excluded_card_nos(user_id: int) -> dict[str, set[str]]:
    """掃所有銀行 db, 回 {bank: set(excluded card_no)} — limit 本 user.

    給 transactions stats 用 (跳過 excluded 卡的 txn).
    沒 cards 表 / 沒 excluded 欄 / 全空 都安全 fallback 空 dict.
    """
    return db_api.list_excluded_card_nos_all_banks(
        user_id=user_id, banks=list(KNOWN_BANKS),
    )
