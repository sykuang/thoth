"""Phase 5.1 — /rules CRUD + preview + recategorize endpoints。

Endpoints:
  GET    /rules                  → list user's rules
  POST   /rules                  → create rule
  PUT    /rules/{rule_id}        → update
  DELETE /rules/{rule_id}        → delete
  POST   /rules/preview          → { pattern, sample_texts } → matched indices
  POST   /rules/recategorize     → 全 DB rewrite (per user)
  GET    /rules/categories       → distinct picker categories
  GET    /rules/categories?include_all=true → rules + transaction-only labels
  PUT    /rules/categories       → rename label across rules + transactions
  DELETE /rules/categories       → delete label rules + clear transaction labels

⚠️ recategorize 對 user 的所有 bank.sqlite 全表 rewrite —— 跑 categorize
   並 UPDATE category 欄。實作時用 dedup_key / id 鎖定 row，逐筆更新。
"""
from __future__ import annotations

import re
from typing import Any


from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.server import rules_repo
from backend.server.deps import current_user
from backend.server.categorizer import categorize_with_excluded, safe_match
from backend.server.db_facade import BankNotAvailable, db_api
from backend.server.seed_rules import reset_to_defaults

router = APIRouter(prefix="/rules", tags=["rules"])

# 與 sync_runner.SUPPORTED_BANKS 一致；recategorize 會掃這些 bank.sqlite
SUPPORTED_BANKS = (
    "cathay", "ubot", "hsbc", "ctbc", "sinopac",
    "scsb", "esun", "taishin", "fubon",
    "dbs", "scb", "linebank", "rakuten",
)

CATEGORIZED_TABLES = ("twd_transactions", "card_billed_txns", "card_pending_txns")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class RuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    pattern: str = Field(min_length=1, max_length=500)
    category: str = Field(min_length=1, max_length=80)
    subcategory: str | None = Field(default=None, max_length=80)
    priority: int = Field(default=100, ge=0, le=10_000)
    enabled: bool = True
    # Phase 8.3 (2026-06-15): 命中該 rule 的 txn 在 stats aggregate 自動 skip
    # 收支桶 (信用卡還款/轉帳/退款/回饋等「by definition 不算收支」row)。
    auto_excluded: bool = False


class RuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    pattern: str | None = Field(default=None, min_length=1, max_length=500)
    category: str | None = Field(default=None, min_length=1, max_length=80)
    subcategory: str | None = Field(default=None, max_length=80)
    priority: int | None = Field(default=None, ge=0, le=10_000)
    enabled: bool | None = None
    auto_excluded: bool | None = None


class RuleOut(BaseModel):
    id: int
    user_id: int
    name: str
    pattern: str
    category: str
    subcategory: str | None = None
    priority: int
    enabled: int
    auto_excluded: int = 0
    created_at: str
    updated_at: str


class PreviewIn(BaseModel):
    pattern: str = Field(min_length=1, max_length=500)
    sample_texts: list[str] = Field(default_factory=list, max_length=200)


class PreviewOut(BaseModel):
    pattern: str
    matched_indices: list[int]
    matched_count: int
    total: int


class CategoriesOut(BaseModel):
    categories: list[str]


class CategoryRenameIn(BaseModel):
    old_name: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=80)


class CategoryDeleteIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class CategoryMutationOut(BaseModel):
    category: str
    renamed_to: str | None = None
    rules_updated: int
    transactions_updated: int


class SubcategoriesOut(BaseModel):
    subcategories: list[str]


class SubcategoryRenameIn(BaseModel):
    category: str = Field(min_length=1, max_length=80)
    old_name: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=80)


class SubcategoryDeleteIn(BaseModel):
    category: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=80)


class SubcategoryMutationOut(BaseModel):
    category: str
    subcategory: str
    renamed_to: str | None = None
    rules_updated: int
    transactions_updated: int


class RecategorizeOut(BaseModel):
    total_rows: int
    updated: int
    skipped: int
    protected: int  # Phase 8.4 (2026-06-18): 已有 category 被保護的 row 數 (force=False 時)
    per_bank: dict


class ResetOut(BaseModel):
    """Phase 8 (2026-06-15): POST /rules/reset 回傳."""
    deleted: int
    added: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_regex(pattern: str) -> None:
    """編譯失敗 → 400。"""
    try:
        re.compile(pattern)
    except re.error as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"invalid regex pattern: {e}",
        ) from None


def _category_names(user_id: int, *, include_all: bool) -> list[str]:
    names = set(rules_repo.distinct_categories(
        user_id,
        min_priority=0 if include_all else 80,
    ))
    if include_all:
        for bank in SUPPORTED_BANKS:
            names.update(db_api.list_category_names(bank=bank, user_id=user_id))
    return sorted(names)


def _mutate_category(
    user_id: int,
    old_name: str,
    new_name: str | None,
) -> tuple[int, int]:
    """Mutate all bank stores, compensating exact rows if a later write fails."""
    changed = 0
    committed: list[tuple[str, list[dict[str, Any]]]] = []
    try:
        for bank in SUPPORTED_BANKS:
            try:
                with db_api.transaction(bank=bank) as tx:
                    tx_any: Any = tx
                    snapshot = tx_any.category_snapshot(user_id=user_id, name=old_name)
                    bank_changed = tx_any.replace_category(
                        user_id=user_id, old_name=old_name, new_name=new_name,
                    )
                if snapshot:
                    committed.append((bank, snapshot))
                changed += bank_changed
            except BankNotAvailable:
                continue

        if new_name is None:
            rules_changed = rules_repo.delete_category(user_id, old_name)
        else:
            rules_changed = rules_repo.rename_category(user_id, old_name, new_name)
        return rules_changed, changed
    except Exception as mutation_error:
        rollback_errors: list[str] = []
        for bank, snapshot in reversed(committed):
            try:
                with db_api.transaction(bank=bank) as tx:
                    tx_any: Any = tx
                    tx_any.restore_category_snapshot(
                        user_id=user_id, snapshots=snapshot,
                    )
            except Exception as rollback_error:  # pragma: no cover - catastrophic DB outage
                rollback_errors.append(f"{bank}: {rollback_error}")
        if rollback_errors:
            raise RuntimeError(
                "category mutation failed and rollback was incomplete: "
                + "; ".join(rollback_errors),
            ) from mutation_error
        raise


def _subcategory_names(user_id: int, category: str, *, include_all: bool) -> list[str]:
    names = set(rules_repo.distinct_subcategories(
        user_id,
        category=category,
        min_priority=0 if include_all else 80,
    ))
    if include_all:
        for bank in SUPPORTED_BANKS:
            names.update(db_api.list_subcategory_names(
                bank=bank, user_id=user_id, category=category,
            ))
    return sorted(names)


def _mutate_subcategory(
    user_id: int,
    category: str,
    old_name: str,
    new_name: str | None,
) -> tuple[int, int]:
    changed = 0
    committed: list[tuple[str, list[dict[str, Any]]]] = []
    try:
        for bank in SUPPORTED_BANKS:
            try:
                with db_api.transaction(bank=bank) as tx:
                    tx_any: Any = tx
                    snapshot = tx_any.subcategory_snapshot(
                        user_id=user_id, category=category, name=old_name,
                    )
                    bank_changed = tx_any.replace_subcategory(
                        user_id=user_id,
                        category=category,
                        old_name=old_name,
                        new_name=new_name,
                    )
                if snapshot:
                    committed.append((bank, snapshot))
                changed += bank_changed
            except BankNotAvailable:
                continue

        if new_name is None:
            rules_changed = rules_repo.clear_subcategory(user_id, category, old_name)
        else:
            rules_changed = rules_repo.rename_subcategory(
                user_id, category, old_name, new_name,
            )
        return rules_changed, changed
    except Exception as mutation_error:
        rollback_errors: list[str] = []
        for bank, snapshot in reversed(committed):
            try:
                with db_api.transaction(bank=bank) as tx:
                    tx_any: Any = tx
                    tx_any.restore_subcategory_snapshot(
                        user_id=user_id, snapshots=snapshot,
                    )
            except Exception as rollback_error:  # pragma: no cover
                rollback_errors.append(f"{bank}: {rollback_error}")
        if rollback_errors:
            raise RuntimeError(
                "subcategory mutation failed and rollback was incomplete: "
                + "; ".join(rollback_errors),
            ) from mutation_error
        raise


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@router.get("", response_model=list[RuleOut])
def list_rules(user: dict = Depends(current_user)) -> list[dict]:
    return rules_repo.list_rules(user_id=user["id"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=RuleOut)
def create_rule(body: RuleIn, user: dict = Depends(current_user)) -> dict:
    _validate_regex(body.pattern)
    rid = rules_repo.create_rule(
        user_id=user["id"],
        name=body.name,
        pattern=body.pattern,
        category=body.category,
        subcategory=body.subcategory,
        priority=body.priority,
        enabled=body.enabled,
        auto_excluded=body.auto_excluded,
    )
    rule = rules_repo.get_rule(user_id=user["id"], rule_id=rid)
    if rule is None:  # pragma: no cover — INSERT 後立刻 SELECT 不該失敗
        raise HTTPException(500, "rule created but not found")
    return rule


@router.put("/categories", response_model=CategoryMutationOut)
def rename_category(
    body: CategoryRenameIn,
    user: dict = Depends(current_user),
) -> dict:
    old_name = body.old_name
    new_name = body.name.strip()
    if not new_name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "category name cannot be blank")
    if old_name == new_name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "category name is unchanged")
    if new_name in _category_names(user["id"], include_all=True):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"category {new_name!r} already exists",
        )
    rules_updated, transactions_updated = _mutate_category(
        user["id"], old_name, new_name,
    )
    if rules_updated == 0 and transactions_updated == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"category {old_name!r} not found")
    return {
        "category": old_name,
        "renamed_to": new_name,
        "rules_updated": rules_updated,
        "transactions_updated": transactions_updated,
    }


@router.delete("/categories", response_model=CategoryMutationOut)
def delete_category(
    body: CategoryDeleteIn,
    user: dict = Depends(current_user),
) -> dict:
    name = body.name
    rules_updated, transactions_updated = _mutate_category(user["id"], name, None)
    if rules_updated == 0 and transactions_updated == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"category {name!r} not found")
    return {
        "category": name,
        "renamed_to": None,
        "rules_updated": rules_updated,
        "transactions_updated": transactions_updated,
    }


@router.put("/subcategories", response_model=SubcategoryMutationOut)
def rename_subcategory(
    body: SubcategoryRenameIn,
    user: dict = Depends(current_user),
) -> dict:
    new_name = body.name.strip()
    if not new_name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "subcategory name cannot be blank")
    if body.old_name == new_name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "subcategory name is unchanged")
    if new_name in _subcategory_names(user["id"], body.category, include_all=True):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"subcategory {new_name!r} already exists in {body.category!r}",
        )
    rules_updated, transactions_updated = _mutate_subcategory(
        user["id"], body.category, body.old_name, new_name,
    )
    if rules_updated == 0 and transactions_updated == 0:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"subcategory {body.old_name!r} not found in {body.category!r}",
        )
    return {
        "category": body.category,
        "subcategory": body.old_name,
        "renamed_to": new_name,
        "rules_updated": rules_updated,
        "transactions_updated": transactions_updated,
    }


@router.delete("/subcategories", response_model=SubcategoryMutationOut)
def delete_subcategory(
    body: SubcategoryDeleteIn,
    user: dict = Depends(current_user),
) -> dict:
    rules_updated, transactions_updated = _mutate_subcategory(
        user["id"], body.category, body.name, None,
    )
    if rules_updated == 0 and transactions_updated == 0:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"subcategory {body.name!r} not found in {body.category!r}",
        )
    return {
        "category": body.category,
        "subcategory": body.name,
        "renamed_to": None,
        "rules_updated": rules_updated,
        "transactions_updated": transactions_updated,
    }


@router.put("/{rule_id}", response_model=RuleOut)
def update_rule(
    rule_id: int,
    body: RuleUpdate,
    user: dict = Depends(current_user),
) -> dict:
    if body.pattern is not None:
        _validate_regex(body.pattern)
    # 排除 None 欄位; subcategory 空字串 → 視為「清掉」, 寫成 NULL
    raw = body.model_dump()
    if raw.get("subcategory") == "":
        raw["subcategory"] = None  # 留在 dict, 下面 fields 會包含這個 None
        explicit_clear_sub = True
    else:
        explicit_clear_sub = False
    fields = {k: v for k, v in raw.items() if v is not None}
    if explicit_clear_sub:
        fields["subcategory"] = None
    if not fields:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty update body")
    ok = rules_repo.update_rule(user_id=user["id"], rule_id=rule_id, **fields)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"rule {rule_id} not found")
    rule = rules_repo.get_rule(user_id=user["id"], rule_id=rule_id)
    if rule is None:  # pragma: no cover
        raise HTTPException(404, f"rule {rule_id} not found")
    return rule


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rule(rule_id: int, user: dict = Depends(current_user)) -> None:
    ok = rules_repo.delete_rule(user_id=user["id"], rule_id=rule_id)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"rule {rule_id} not found")


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

@router.post("/preview", response_model=PreviewOut)
def preview(body: PreviewIn, user: dict = Depends(current_user)) -> dict:
    _validate_regex(body.pattern)
    matched = [i for i, t in enumerate(body.sample_texts)
               if safe_match(body.pattern, t)]
    return {
        "pattern": body.pattern,
        "matched_indices": matched,
        "matched_count": len(matched),
        "total": len(body.sample_texts),
    }


# ---------------------------------------------------------------------------
# Recategorize all
# ---------------------------------------------------------------------------

@router.post("/recategorize", response_model=RecategorizeOut)
def recategorize(
    force: bool = False,
    user: dict = Depends(current_user),
) -> dict:
    """對該 user 的所有 bank 跑 categorize。

    Phase 8.4 (2026-06-18) — 保護手動分類:
      使用者指示：「分類規則應該只套用在未分類吧而不是所有交易」。
      User 在 UI 手動改過的 category 不該被 rule 默默蓋掉。

      預設行為 (force=False)：
        - 只對 ``category IS NULL`` 的 row 跑 rule (真未分類)
        - ``category`` 已有值的 row 一律視為「使用者已選」，全 skip 計入 protected
        - auto_excluded 仍會更新 (跟著 rule 命中走，因為 user 通常不直接改它)

      強制覆寫 (force=True, query param ``?force=true``)：
        - 走原本邏輯 — 比對 (cat, sub, auto_ex) 不同就 UPDATE 全表
        - 給 ``POST /rules/reset`` 或 admin 工具用
        - **慎用**：會覆寫所有手動分類

      回傳 protected 欄位讓 UI 顯示「已保護 N 筆手動分類」.

    Phase 5.1 (origin): 對 user 所有 bank 跑全表 rewrite category。
    Phase 11 (2026-06-17) PG 修：走 ``db.open_bank_conn()`` facade。
    """
    rules = rules_repo.list_rules(user_id=user["id"], enabled_only=True)
    total = 0
    updated = 0
    skipped = 0
    protected = 0  # Phase 8.4: 已有 category 被保護不動的數量
    per_bank: dict = {}

    for bank in SUPPORTED_BANKS:
        # ensure_recategorize_columns: 老 schema 沒 category/subcategory/auto_excluded
        # 自動 ALTER (idempotent)
        db_api.ensure_recategorize_columns(bank=bank)

        rows = db_api.list_txns_for_recategorize(bank=bank, user_id=user["id"])
        if not rows:
            per_bank[bank] = {"total": 0, "updated": 0, "protected": 0}
            continue

        bank_total = 0
        bank_upd = 0
        bank_protected = 0
        updates: list[dict[str, Any]] = []

        for r in rows:
            bank_total += 1
            total += 1

            # Phase 8.4 (2026-06-18): 保護手動分類
            # category IS NOT NULL → 視為使用者已選 (或上次 rule 命中)
            # force=False 預設不動，避免 rule 變動默默蓋掉 user 編輯
            if not force and r["category"] is not None:
                bank_protected += 1
                protected += 1
                skipped += 1
                continue

            # 用同 helper 跟 store.upsert_* 對齊 (desc + counterparty + memo join)
            cat_text_parts = [
                r["description"] or "",
                r["counterparty_acct"] or "",
                r["memo"] or "",
            ]
            seen_set = set()
            cat_text_clean = []
            for p in cat_text_parts:
                s = p.strip() if p else ""
                if s and s not in seen_set:
                    seen_set.add(s)
                    cat_text_clean.append(s)
            cat_text = " | ".join(cat_text_clean)
            new_cat, new_sub, new_auto_ex = categorize_with_excluded(
                cat_text, rules,
            )
            new_auto_ex_int = 1 if new_auto_ex else 0
            if (new_cat == r["category"]
                    and new_sub == r["subcategory"]
                    and new_auto_ex_int == r["auto_excluded"]):
                skipped += 1
                continue
            updates.append({
                "table": r["table"],
                "id": r["id"],
                "category": new_cat,
                "subcategory": new_sub,
                "auto_excluded": new_auto_ex_int,
            })

        if updates:
            try:
                with db_api.transaction(bank=bank) as tx:
                    changed = tx.batch_update_categorization(
                        user_id=user["id"], updates=updates,
                    )
                bank_upd = changed
                updated += changed
            except BankNotAvailable:
                continue

        per_bank[bank] = {
            "total": bank_total, "updated": bank_upd, "protected": bank_protected,
        }

    return {
        "total_rows": total,
        "updated": updated,
        "skipped": skipped,
        "protected": protected,
        "per_bank": per_bank,
    }


# ---------------------------------------------------------------------------
# Distinct categories
# ---------------------------------------------------------------------------

@router.get("/categories", response_model=CategoriesOut)
def get_categories(
    include_all: bool = False,
    user: dict = Depends(current_user),
) -> dict:
    return {"categories": _category_names(user["id"], include_all=include_all)}


@router.get("/subcategories", response_model=SubcategoriesOut)
def get_subcategories(
    category: str | None = None,
    include_all: bool = False,
    user: dict = Depends(current_user),
) -> dict:
    """List distinct subcategories, optionally including transaction-only labels."""
    if category and include_all:
        subcategories = _subcategory_names(user["id"], category, include_all=True)
    else:
        subcategories = rules_repo.distinct_subcategories(
            user_id=user["id"],
            category=category,
            min_priority=0 if include_all else 80,
        )
    return {"subcategories": subcategories}


# ---------------------------------------------------------------------------
# Phase 8 (2026-06-15 使用者指示): 一鍵恢復預設 — 砍掉重塞 DEFAULT_RULES
# ---------------------------------------------------------------------------

@router.post("/reset", response_model=ResetOut)
def reset_rules(user: dict = Depends(current_user)) -> dict:
    """砍掉該 user 所有 rule, 重塞 DEFAULT_RULES (給手滑救援).

    重要: 此 endpoint 不可逆 — frontend 必須 alert 二次確認.
    回傳 {deleted, added} 給 frontend 顯示成果.
    """
    return reset_to_defaults(user_id=user["id"])
