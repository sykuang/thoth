"""Phase 1 — /credentials routes（per-user, per-bank, per-field）。

白名單：
  - bank：backend.core.creds.ALL_CREDS 的 cls.BANK.lower()
  - field：對應 cls._attrs()

回應一律 metadata only — 絕不回密文/明文。

Endpoints:
  GET    /credentials                列每 bank 已設欄位
  PUT    /credentials/{bank}         body={field: value, ...}  → 204
  DELETE /credentials/{bank}         清整個 bank → 204
  DELETE /credentials/{bank}/{field} 清單欄 → 204
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, status

from backend.core.creds import ALL_CREDS
from backend.server.deps import current_user
from backend.server.creds_store import LocalFernetBackend

router = APIRouter(prefix="/credentials", tags=["credentials"])


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


@router.get("")
def list_credentials(user: dict = Depends(current_user)) -> list[dict]:
    """列出每 bank 已設欄位（不含值）。"""
    store = LocalFernetBackend()
    out = []
    for cls in ALL_CREDS:
        bank = cls.BANK.lower()
        fields_set = store.list_fields(user_id=user["id"], bank=bank)
        out.append({
            "bank": bank,
            "has_creds": bool(fields_set),
            "fields_set": fields_set,
        })
    return out


@router.put("/{bank}", status_code=status.HTTP_204_NO_CONTENT)
def put_credentials(
    bank: str,
    body: dict = Body(...),
    user: dict = Depends(current_user),
) -> None:
    """寫一個或多個欄位（upsert）。"""
    cls = _validate_bank(bank)
    valid_fields = set(cls._attrs())
    unknown = [k for k in body if k not in valid_fields]
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{bank} 不支援這些欄位: {unknown}; 可填: {sorted(valid_fields)}",
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
        store.put(user_id=user["id"], bank=bank, field=field, plain=value)


@router.delete("/{bank}", status_code=status.HTTP_204_NO_CONTENT)
def delete_bank(bank: str, user: dict = Depends(current_user)) -> None:
    """清掉該 bank 所有欄位。"""
    _validate_bank(bank)
    store = LocalFernetBackend()
    for field in store.list_fields(user_id=user["id"], bank=bank):
        store.delete(user_id=user["id"], bank=bank, field=field)


@router.delete("/{bank}/{field}", status_code=status.HTTP_204_NO_CONTENT)
def delete_field(bank: str, field: str, user: dict = Depends(current_user)) -> None:
    """清單一欄位。"""
    cls = _validate_bank(bank)
    if field not in cls._attrs():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{bank} 不支援這個欄位: {field}; 可填: {sorted(cls._attrs())}",
        )
    store = LocalFernetBackend()
    store.delete(user_id=user["id"], bank=bank, field=field)
