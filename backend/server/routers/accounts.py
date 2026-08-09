"""Phase L5-1 — /accounts routes (multi-account per user/bank).

一個 user 在同一銀行可以有多個 account (主帳 / 老婆 / 公司)。每個 account 有獨立的
Fernet-加密 cred 欄位群, 由 /accounts/{id}/fields 操作。

回應一律 metadata only — 絕不回密文/明文。

Endpoints:
  GET    /accounts                    列當前 user 所有 accounts (跨銀行)
  POST   /accounts                    body={bank, label} → 201 BankAccount
  PUT    /accounts/{id}               body={label} → 200 重新命名
  DELETE /accounts/{id}               → 204 (CASCADE 砍 v2 表 cred)
  PUT    /accounts/{id}/fields        body={field: value, ...} → 204 upsert 欄位
  DELETE /accounts/{id}/fields/{name} → 204 清單一欄位
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.core.creds import ALL_CREDS
from backend.server.deps import current_user
from backend.server.creds_store import (
    AccountsRepo,
    LocalFernetBackend,
    list_account_metadata,
)
from backend.server.db import IntegrityError

router = APIRouter(prefix="/accounts", tags=["accounts"])


# ============================================================
# 共用 helpers
# ============================================================

def _bank_map() -> dict[str, type]:
    """bank_name(lower) → BankCreds subclass。"""
    return {cls.BANK.lower(): cls for cls in ALL_CREDS}


def _validate_bank(bank: str) -> type:
    bm = _bank_map()
    if bank not in bm:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"unknown bank: {bank!r}; supported: {sorted(bm)}",
        )
    return bm[bank]


def _get_owned_account(account_id: int, user_id: int):
    """撈 account 並驗證 owner; 否則 raise 404 (不揭露存在與否)。"""
    repo = AccountsRepo()
    acct = repo.get(account_id)
    if acct is None or acct.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "找不到此帳號")
    return acct


# ============================================================
# Pydantic 模型
# ============================================================

class CreateAccountReq(BaseModel):
    bank: str = Field(..., description="bank slug, e.g. 'cathay'")
    label: str = Field(..., min_length=1, max_length=64, description="使用者命名 (主帳/老婆/...)")


class RenameAccountReq(BaseModel):
    label: str = Field(..., min_length=1, max_length=64)


# ============================================================
# Routes
# ============================================================

@router.get("")
def list_accounts(user: dict = Depends(current_user)) -> list[dict]:
    """列當前 user 所有 accounts (跨銀行), 附 fields_set + has_creds。"""
    return list_account_metadata(user["id"])


@router.post("", status_code=status.HTTP_201_CREATED)
def create_account(
    body: CreateAccountReq,
    user: dict = Depends(current_user),
) -> dict:
    """建一個新 account; 若 (user, bank, label) 重複 → 409。"""
    _validate_bank(body.bank)
    repo = AccountsRepo()
    try:
        a = repo.create(user_id=user["id"], bank=body.bank, label=body.label)
    except IntegrityError:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"account with label {body.label!r} already exists for bank {body.bank!r}",
        ) from None
    return {
        "id": a.id,
        "bank": a.bank,
        "label": a.label,
        "created_at": a.created_at,
        "updated_at": a.updated_at,
        "has_creds": False,
        "fields_set": [],
    }


@router.put("/{account_id}", status_code=status.HTTP_200_OK)
def rename_account(
    account_id: int,
    body: RenameAccountReq,
    user: dict = Depends(current_user),
) -> dict:
    """重新命名 account label。若新 label 重複 → 409。"""
    acct = _get_owned_account(account_id, user["id"])
    repo = AccountsRepo()
    try:
        renamed = repo.rename(account_id, body.label)
    except IntegrityError:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"account with label {body.label!r} already exists for bank {acct.bank!r}",
        ) from None
    assert renamed is not None
    return {
        "id": renamed.id,
        "bank": renamed.bank,
        "label": renamed.label,
        "created_at": renamed.created_at,
        "updated_at": renamed.updated_at,
    }


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    account_id: int,
    user: dict = Depends(current_user),
) -> None:
    """刪 account → CASCADE 砍 v2 cred。"""
    _get_owned_account(account_id, user["id"])
    repo = AccountsRepo()
    repo.delete(account_id)


@router.put("/{account_id}/fields", status_code=status.HTTP_204_NO_CONTENT)
def put_account_fields(
    account_id: int,
    body: dict = Body(...),
    user: dict = Depends(current_user),
) -> None:
    """寫一個或多個欄位 (upsert)。"""
    acct = _get_owned_account(account_id, user["id"])
    cls = _validate_bank(acct.bank)
    valid_fields = set(cls._attrs())
    unknown = [k for k in body if k not in valid_fields]
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{acct.bank} 不支援這些欄位: {unknown}; 可填: {sorted(valid_fields)}",
        )
    if not body:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "請至少填一個欄位")
    store = LocalFernetBackend()
    for field, value in body.items():
        if not isinstance(value, str) or not value:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"field {field!r} value must be non-empty string",
            )
        store.put_acct(
            account_id=account_id, field=field, plain=value,
            expected_owner_user_id=user["id"],
        )


@router.delete("/{account_id}/fields/{field}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account_field(
    account_id: int,
    field: str,
    user: dict = Depends(current_user),
) -> None:
    """清單一欄位。"""
    acct = _get_owned_account(account_id, user["id"])
    cls = _validate_bank(acct.bank)
    if field not in cls._attrs():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{acct.bank} 不支援這個欄位: {field}; 可填: {sorted(cls._attrs())}",
        )
    store = LocalFernetBackend()
    store.delete_acct(
        account_id=account_id, field=field,
        expected_owner_user_id=user["id"],
    )
