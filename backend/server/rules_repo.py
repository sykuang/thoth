"""Phase 5.1 — category_rules CRUD repo（per-user）。

存於 server.sqlite 的 `category_rules` 表（建表在 backend/server/db.py）。
所有 query 都帶 user_id 篩選——絕不跨 user。

回傳 dict 結構：
  {
    "id": int, "user_id": int, "name": str, "pattern": str,
    "category": str, "priority": int, "enabled": int (0/1),
    "created_at": str, "updated_at": str,
  }
"""
from __future__ import annotations

from datetime import datetime, UTC

from backend.server.db import get_conn

_COLS = ("id", "user_id", "name", "pattern", "category", "subcategory",
         "priority", "enabled", "auto_excluded", "created_at", "updated_at")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_dict(row) -> dict:
    return dict(zip(_COLS, row, strict=True))


def create_rule(
    user_id: int,
    name: str,
    pattern: str,
    category: str,
    priority: int = 100,
    enabled: bool = True,
    subcategory: str | None = None,
    auto_excluded: bool = False,
) -> int:
    """新增一條 rule，回 lastrowid。

    Phase 9 (2026-06-16) portable backend fix: 改用 ``INSERT ... RETURNING id``，
    SQLite 跟 PostgreSQL 都吃；舊版用 ``cur.lastrowid`` 在 PG 永遠回 None 而炸 500
    (cloud user 6 點 POST /rules/reset 時揭發)。
    """
    now = _now()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO category_rules "
            "(user_id, name, pattern, category, subcategory, priority, enabled, "
            "auto_excluded, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (user_id, name, pattern, category, subcategory, priority,
             1 if enabled else 0, 1 if auto_excluded else 0, now, now),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("INSERT category_rules 後 RETURNING 為 None")
    return int(row[0])


def list_rules(user_id: int, enabled_only: bool = False) -> list[dict]:
    """列該 user 的所有 rules，已按 priority DESC, id ASC 排序。"""
    sql = (
        "SELECT id, user_id, name, pattern, category, subcategory, priority, enabled, "
        "auto_excluded, created_at, updated_at FROM category_rules WHERE user_id=?"
    )
    args: tuple = (user_id,)
    if enabled_only:
        sql += " AND enabled=1"
    sql += " ORDER BY priority DESC, id ASC"
    with get_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_rule(user_id: int, rule_id: int) -> dict | None:
    """單一 rule（帶 user 隔離）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, user_id, name, pattern, category, subcategory, priority, enabled, "
            "auto_excluded, created_at, updated_at FROM category_rules WHERE id=? AND user_id=?",
            (rule_id, user_id),
        ).fetchone()
    return _row_to_dict(row) if row else None


_UPDATABLE = {"name", "pattern", "category", "subcategory", "priority", "enabled", "auto_excluded"}


def update_rule(user_id: int, rule_id: int, **fields) -> bool:
    """更新指定欄位；只有屬於該 user 的 rule 才會被改。
    回 True 若有 row 真的被更新，False 若沒 row 受影響（rule 不存在或屬於別 user）。
    """
    sets = []
    args: list = []
    for k, v in fields.items():
        if k not in _UPDATABLE:
            continue
        if k in ("enabled", "auto_excluded"):
            v = 1 if v else 0
        sets.append(f"{k}=?")
        args.append(v)
    if not sets:
        return False
    sets.append("updated_at=?")
    args.append(_now())
    args.extend([rule_id, user_id])
    sql = f"UPDATE category_rules SET {', '.join(sets)} WHERE id=? AND user_id=?"
    with get_conn() as conn:
        cur = conn.execute(sql, tuple(args))
        return cur.rowcount > 0


def delete_rule(user_id: int, rule_id: int) -> bool:
    """刪 rule（帶 user 隔離）。回 True 若真的刪掉。"""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM category_rules WHERE id=? AND user_id=?",
            (rule_id, user_id),
        )
        return cur.rowcount > 0


def distinct_categories(user_id: int, min_priority: int = 80) -> list[str]:
    """列該 user 已用過的 distinct category（給 UI 建議用）。

    Phase 8.2 D 路線 (2026-06-14): 預設 min_priority=80 過濾掉
    `_legacy` rule (priority=50) — 避免老 Phase 5 命名混入 chip 顯示。
    傳 0 可解除過濾撈完整 (給 admin 工具用)。
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT category FROM category_rules "
            "WHERE user_id=? AND priority >= ? ORDER BY category",
            (user_id, min_priority),
        ).fetchall()
    return [r[0] for r in rows]


def rename_category(user_id: int, old_name: str, new_name: str) -> int:
    """Rename one category across this user's rules; return changed rule count."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE category_rules SET category=?, updated_at=? "
            "WHERE user_id=? AND category=?",
            (new_name, _now(), user_id, old_name),
        )
        return cur.rowcount


def delete_category(user_id: int, name: str) -> int:
    """Delete every rule assigning one category; return deleted rule count."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM category_rules WHERE user_id=? AND category=?",
            (user_id, name),
        )
        return cur.rowcount


def distinct_subcategories(
    user_id: int, category: str | None = None, min_priority: int = 80,
) -> list[str]:
    """列該 user 已用過的 distinct subcategory（給 UI 子分類 chip 用）。

    若帶 category，只列該主類下的子分類；否則列全部。
    NULL / 空字串會被過濾掉。
    Phase 8.2 D 路線: 預設 min_priority=80 過濾掉 `_legacy` rule (priority=50)。
    """
    sql = (
        "SELECT DISTINCT subcategory FROM category_rules "
        "WHERE user_id=? AND priority >= ? "
        "AND subcategory IS NOT NULL AND subcategory != ''"
    )
    args: tuple = (user_id, min_priority)
    if category:
        sql += " AND category=?"
        args = (user_id, min_priority, category)
    sql += " ORDER BY subcategory"
    with get_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [r[0] for r in rows]


def rename_subcategory(
    user_id: int,
    category: str,
    old_name: str,
    new_name: str,
) -> int:
    """Rename one category-scoped subcategory without touching sibling categories."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE category_rules SET subcategory=?, updated_at=? "
            "WHERE user_id=? AND category=? AND subcategory=?",
            (new_name, _now(), user_id, category, old_name),
        )
        return cur.rowcount


def clear_subcategory(user_id: int, category: str, name: str) -> int:
    """Clear one subcategory while keeping its rules active for the parent category."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE category_rules SET subcategory=NULL, updated_at=? "
            "WHERE user_id=? AND category=? AND subcategory=?",
            (_now(), user_id, category, name),
        )
        return cur.rowcount
