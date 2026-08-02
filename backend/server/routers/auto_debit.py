"""GET /cards/auto-debit — settings + reminders for credit-card auto-debit accounts.

Phase L10 (2026-06-20). Spec 決議：A2 per-bank, B2 cross-bank account, C3 strict
balance check, D3 0 ≤ days_until_due ≤ 3, E1 提醒未設定, F2 reason in
{no_account, insufficient}, G4 TWD-only picker, H2 dashboard 位於 KPI 後.

Endpoints:
  GET    /cards/auto-debit/settings                     全部設定
  PUT    /cards/auto-debit/settings/{card_bank}         body={account_bank, account_no}
  DELETE /cards/auto-debit/settings/{card_bank}         清除單一設定
  GET    /cards/auto-debit/eligible-accounts            TWD picker 列表
  GET    /cards/auto-debit/reminders                    繳費提醒 (dashboard 用)

Design 鐵則：
  - account_no 必須是 currency='TWD' 且 excluded=0 且 product_type NOT IN
    (loan, mortgage, credit_line) — 即可活儲扣繳的台幣帳戶
  - settings 寫在 server.db，不寫進 bank.sqlite（sync wipe / 跨銀行不適合）
  - reminder logic 在這層算，不污染 cards router 的 bill_status derivation
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.core import bank_data
from backend.server import auto_debit_settings_repo as repo
from backend.server.creds_store import AccountsRepo
from backend.server.db_facade import db_api
from backend.server.deps import current_user

router = APIRouter(prefix="/cards/auto-debit", tags=["cards", "auto-debit"])
logger = logging.getLogger("backend.auto_debit")


# ============================================================
# Pydantic request / response models
# ============================================================


class AutoDebitSettingBody(BaseModel):
    account_bank: str = Field(..., description="扣繳戶所在銀行 (可跨銀行)")
    account_no: str = Field(..., description="扣繳戶帳號 (TWD 活儲)")


class AutoDebitSettingOut(BaseModel):
    card_bank: str
    account_bank: str
    account_no: str
    updated_at: str


class EligibleAccountOut(BaseModel):
    """For picker UI: TWD active deposit accounts user can set as 扣繳戶."""
    bank: str
    account_no: str
    nickname: str | None = None        # API original (e.g. '主存錢筒')
    nickname_overwrite: str | None = None  # User override
    type: str | None = None
    raw_balance: float | None = None


class PaymentReminderOut(BaseModel):
    """Dashboard reminder entry; 非 HSBC 整戶帳單用空 card_no + null card_name."""
    reason: str = Field(..., description="'no_account' 或 'insufficient'")
    card_bank: str
    card_no: str
    card_name: str | None = None
    bill_due_amount: float
    payment_due_date: str
    days_until_due: int
    # populated 只有 reason='insufficient'
    account_bank: str | None = None
    account_no: str | None = None
    account_balance: float | None = None
    shortfall: float | None = None


# ============================================================
# Helpers
# ============================================================


def _bank_to_dict(s) -> dict[str, Any]:
    """AutoDebitSetting → frontend dict."""
    return {
        "card_bank": s.card_bank,
        "account_bank": s.account_bank,
        "account_no": s.account_no,
        "updated_at": s.updated_at,
    }


def _is_eligible_picker_account(acct) -> bool:
    """G4: TWD + active + 非貸款型才能當扣繳戶."""
    if acct.excluded:
        return False
    if acct.currency != "TWD":
        return False
    return acct.product_type not in ("loan", "mortgage", "credit_line")


def _user_banks(user_id: int) -> list[str]:
    """所有 user 有資料的銀行 (跟 cards/portfolio router 同 pattern)."""
    accts_repo = AccountsRepo()
    user_accts = accts_repo.list_for_user(user_id)
    if user_accts:
        return sorted({a.bank for a in user_accts})
    return bank_data.fallback_banks_with_data()


def _parse_iso_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _local_date(tz: str) -> date:
    try:
        zone = ZoneInfo(tz)
    except ZoneInfoNotFoundError:
        logger.warning("[auto-debit] unknown tz=%r, fallback Asia/Taipei", tz)
        zone = ZoneInfo("Asia/Taipei")
    return datetime.now(zone).date()


def _reminder_tz() -> str:
    return os.environ.get("PAYMENT_REMINDER_TZ", "Asia/Taipei")


def _validate_account_is_eligible(
    user_id: int, account_bank: str, account_no: str,
) -> None:
    """Raise HTTPException 422 if account 不符 G4 規則."""
    if account_bank not in bank_data.KNOWN_BANKS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"未知銀行 {account_bank}",
        )
    try:
        accts = db_api.list_accounts(bank=account_bank, user_id=user_id)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"無法讀取 {account_bank} 帳戶清單",
        ) from None
    match = next((a for a in accts if a.account_no == account_no), None)
    if match is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"找不到帳號 {account_no} (在 {account_bank})",
        )
    if not _is_eligible_picker_account(match):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"扣繳戶必須是 TWD 活儲帳戶 (該帳號 currency={match.currency} type={match.type})",
        )


# ============================================================
# Settings CRUD
# ============================================================


@router.get("/settings")
def list_settings_endpoint(
    user: dict = Depends(current_user),
) -> list[dict[str, Any]]:
    """全部自動扣繳設定 (per card_bank)."""
    rows = repo.list_settings(user["id"])
    return [_bank_to_dict(s) for s in rows]


@router.put("/settings/{card_bank}")
def upsert_setting_endpoint(
    card_bank: str,
    body: AutoDebitSettingBody,
    user: dict = Depends(current_user),
) -> dict[str, Any]:
    """設定 card_bank 銀行底下所有卡的自動扣繳戶 (A2 per-bank)."""
    if card_bank not in bank_data.KNOWN_BANKS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"未知銀行 {card_bank}",
        )
    _validate_account_is_eligible(user["id"], body.account_bank, body.account_no)
    saved = repo.upsert_setting(
        user["id"], card_bank, body.account_bank, body.account_no,
    )
    return _bank_to_dict(saved)


@router.delete("/settings/{card_bank}", status_code=status.HTTP_204_NO_CONTENT)
def delete_setting_endpoint(
    card_bank: str,
    user: dict = Depends(current_user),
) -> None:
    """清除 card_bank 銀行的自動扣繳設定."""
    repo.delete_setting(user["id"], card_bank)


# ============================================================
# Eligible accounts (picker)
# ============================================================


@router.get("/eligible-accounts")
def eligible_accounts_endpoint(
    user: dict = Depends(current_user),
) -> list[dict[str, Any]]:
    """G4: 全 user 跨銀行的 TWD 活儲帳戶 (可當扣繳戶 picker)."""
    out: list[dict[str, Any]] = []
    for bank in _user_banks(user["id"]):
        try:
            accts = db_api.list_accounts(bank=bank, user_id=user["id"])
        except Exception:
            continue
        for acct in accts:
            if not _is_eligible_picker_account(acct):
                continue
            out.append({
                "bank": bank,
                "account_no": acct.account_no,
                "nickname": acct.nickname,
                "nickname_overwrite": acct.nickname_overwrite,
                "type": acct.type,
                "raw_balance": acct.raw_balance,
            })
    # 排序: bank → balance desc (有錢的優先 default 推薦)
    out.sort(key=lambda a: (a["bank"], -(a["raw_balance"] or 0)))
    return out


# ============================================================
# Reminders (dashboard)
# ============================================================


def build_payment_reminders(user_id: int, today: date | None = None) -> list[dict[str, Any]]:
    """Dashboard 繳費提醒.

    觸發條件 (Q3+Q4+Q5 決議):
      due_date 0~3 天內 (含今天, 不含過期) AND bill_due_amount > 0
      AND (no setting → reason='no_account')
        OR (setting + balance < bill_due_amount → reason='insufficient')

    過期 (today > due_date) 走 cards router 的 bill_status='overdue', 不在這裡.

    Source scope:
      * HSBC 是 per-card bill，保留逐卡提醒。
      * 其他銀行是整戶帳單複寫到多卡，依 (bank, due_date, amount) 去重，
        金額只取一次而非相加，輸出也不冒充任一卡。
    """
    today = today or _local_date(_reminder_tz())
    settings = repo.settings_by_card_bank(user_id)
    reminders: list[dict[str, Any]] = []
    # 除 HSBC 外，native bill_due_amount / payment_due_date 是整戶帳單，persist
    # 只是把同一事實複寫到每張卡。依 source identity 去重，不能相加（否則
    # CTBC 四卡 27,916 會被誤算成 111,664），也不能冒充任一卡。
    seen_shared_bills: set[tuple[str, str, float]] = set()

    for bank in _user_banks(user_id):
        try:
            cards = db_api.list_cards(bank=bank, user_id=user_id, include_inactive=False)
        except Exception:
            continue
        for card in cards:
            if card.excluded:
                continue
            if not card.bill_due_amount or card.bill_due_amount <= 0:
                continue  # no_payment_required, skip (C3)
            due = _parse_iso_date(card.payment_due_date)
            if due is None:
                continue
            days = (due - today).days
            if days < 0 or days > 3:  # D3: 0..3 含今天，過期另計
                continue

            shared_bill = bank != "hsbc"
            if shared_bill:
                bill_key = (bank, due.isoformat(), float(card.bill_due_amount))
                if bill_key in seen_shared_bills:
                    continue
                seen_shared_bills.add(bill_key)

            reminder_card_no = "" if shared_bill else card.card_no
            reminder_card_name = None if shared_bill else card.name

            setting = settings.get(bank)
            if setting is None:
                # E1+F2: no setting → 'no_account'
                reminders.append({
                    "reason": "no_account",
                    "card_bank": bank,
                    "card_no": reminder_card_no,
                    "card_name": reminder_card_name,
                    "bill_due_amount": card.bill_due_amount,
                    "payment_due_date": card.payment_due_date,
                    "days_until_due": days,
                    "account_bank": None,
                    "account_no": None,
                    "account_balance": None,
                    "shortfall": None,
                })
                continue

            # 有 setting → 抓 account balance 比對
            try:
                accts = db_api.list_accounts(
                    bank=setting.account_bank, user_id=user_id,
                )
            except Exception:
                accts = []
            acct = next(
                (a for a in accts if a.account_no == setting.account_no),
                None,
            )
            balance = acct.raw_balance if (acct and acct.raw_balance is not None) else 0.0
            if balance < card.bill_due_amount:
                # C3+F2: balance < bill_due_amount → 'insufficient'
                reminders.append({
                    "reason": "insufficient",
                    "card_bank": bank,
                    "card_no": reminder_card_no,
                    "card_name": reminder_card_name,
                    "bill_due_amount": card.bill_due_amount,
                    "payment_due_date": card.payment_due_date,
                    "days_until_due": days,
                    "account_bank": setting.account_bank,
                    "account_no": setting.account_no,
                    "account_balance": balance,
                    "shortfall": round(card.bill_due_amount - balance, 2),
                })

    # 排序: days_until_due asc (越急越上面), 再 bill_due_amount desc
    reminders.sort(key=lambda r: (r["days_until_due"], -r["bill_due_amount"]))
    return reminders


@router.get("/reminders")
def reminders_endpoint(
    user: dict = Depends(current_user),
) -> list[dict[str, Any]]:
    return build_payment_reminders(user["id"])
