"""Backfill: 重算既有 row 的 flow_type / income_category.

背景
====
2026-07-28 root cause: `BankStore.upsert_twd_txns` / `upsert_card_billed` /
`refresh_card_pending` 三個 INSERT **從來沒寫過** flow_type / income_category,
全靠 schema `ALTER TABLE ... DEFAULT 'expense'`。結果:

  - 樂天「存款利息 +$4」flow_type='expense' → 收入被記成支出
  - `amount_by_flow_type` 幾乎全落在 expense 桶
  - `income_category` 全 NULL → `passive_income_total` / `passive_income_pct`
    永遠 0, FIRE 指標形同虛設

store 端已修 (見 `backend/core/store._flow_fields`)，但既有 row 仍是舊值,
需要這支 backfill 重算。

判定邏輯
========
直接重用 `store._flow_fields(category, subcategory, amount, txn_type)` —
**不重寫一份規則**, 避免 migration 與 runtime 漂移。

  - 台幣 (`twd_transactions`): amount = income - expend, 方向可信
  - 信用卡 (`card_billed_txns` / `card_pending_txns`): amount 傳 None,
    帳單視角正負跟 user cashflow 方向不一致, 只信 txn_type / category

已被使用者手動改過的 row 怎麼辦
==============================
category / subcategory 是使用者可覆寫的欄位, 而 flow_type 由它們推導,
所以「重算」等同「尊重使用者最新分類」, 不會蓋掉人工修正。

用法
====
mac local (SQLite, 掃 data_root 下所有 *.sqlite bank DB):
    DB_BACKEND=sqlite uv run python -m migrations.backfill_flow_type_20260728            # dry-run
    DB_BACKEND=sqlite uv run python -m migrations.backfill_flow_type_20260728 --execute

prod pod (PG):
    az containerapp exec -n thoth-backend-vnet -g thoth-rg \\
        --command "python -m migrations.backfill_flow_type_20260728"
    az containerapp exec -n thoth-backend-vnet -g thoth-rg \\
        --command "python -m migrations.backfill_flow_type_20260728 --execute"

退場
====
所有環境跑完即可刪除。這是一次性資料修復, 不是常駐邏輯。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.store import _flow_fields

# table → 是否為信用卡表 (信用卡 amount 方向不可信, 傳 None)
TABLES = {
    "twd_transactions": False,
    "card_billed_txns": True,
    "card_pending_txns": True,
}


def _row_flow(
    table: str, row: tuple, old_flow: str | None, old_ic: str | None,
) -> tuple[str, str | None]:
    """→ 新的 (flow_type, income_category)，保守：沒把握就不動既有值。

    2026-07-28 dry-run 抓到的坑：CLI sync 路徑不傳 rules (`cli/cli.py` 完全沒有
    `rules=`)，所以很多既有 row 的 category 是 NULL。但它們的 flow_type 卻有
    正確的 `transfer` — 來自更早已被 squash 掉的分類流程。若無條件重算，
    這些 row 會被降級成 expense，是資料破壞而非修復。

    規則：
      1. category 有值 → rule/使用者分類是權威，重算。
      2. 信用卡且 txn_type 有值 → txn_type 是權威，重算。
      3. 其餘 (無任何分類訊號)：只修「flow_type='expense' 但金額為正」這種
         明確的 default 誤判；既有非-expense 值一律保留不動。
    """
    category, subcategory, txn_type, amount = row
    is_card = TABLES[table]
    if category or (is_card and txn_type):
        return _flow_fields(category, subcategory, None if is_card else amount, txn_type)
    # 無分類訊號 — 只救「正金額卻被 default 成 expense」
    if old_flow == "expense" and not is_card and amount is not None and amount > 0:
        return ("income", "other")
    return (old_flow or "expense", old_ic)


def _fetch(cur, table: str, is_postgres: bool):
    if table == "twd_transactions":
        amount_expr = "COALESCE(income, 0) - COALESCE(expend, 0)"
        txn_type_expr = "NULL"
    else:
        amount_expr = "amount"
        txn_type_expr = "txn_type"
    cur.execute(
        f"SELECT id, category, subcategory, {txn_type_expr}, {amount_expr}, "
        f"flow_type, income_category FROM {table}",
    )
    return cur.fetchall()


def _backfill_table(conn, table: str, is_postgres: bool, dry_run: bool) -> dict:
    ph = "%s" if is_postgres else "?"
    cur = conn.cursor()
    rows = _fetch(cur, table, is_postgres)
    total = len(rows)
    changed = 0
    sample: list[dict] = []
    for row_id, category, subcategory, txn_type, amount, old_flow, old_ic in rows:
        new_flow, new_ic = _row_flow(
            table, (category, subcategory, txn_type, amount), old_flow, old_ic,
        )
        if new_flow == old_flow and new_ic == old_ic:
            continue
        changed += 1
        if len(sample) < 30:
            sample.append({
                "id": row_id, "category": category, "amount": amount,
                "old": f"{old_flow}/{old_ic}", "new": f"{new_flow}/{new_ic}",
            })
        if not dry_run:
            cur.execute(
                f"UPDATE {table} SET flow_type = {ph}, income_category = {ph} "
                f"WHERE id = {ph}",
                (new_flow, new_ic, row_id),
            )
    if not dry_run and changed > 0:
        conn.commit()
    cur.close()
    return {"total": total, "changed": changed, "sample_changes": sample}


def _table_exists_sqlite(conn, table: str) -> bool:
    return conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,),
    ).fetchone() is not None


def _has_taxonomy_columns_sqlite(conn, table: str) -> bool:
    """舊 DB 可能還沒跑過 Phase 6/7 的 ALTER (只在 BankStore 開檔時才補)。

    缺欄就 skip — 那些 DB 下次被 BankStore 開啟時會自動加欄, 屆時新寫入的 row
    已由 store 端正確填值; 舊 row 再跑一次本 migration 即可。
    """
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    return {"category", "subcategory", "flow_type", "income_category"} <= cols


def backfill_for_sqlite(dry_run: bool = False) -> dict:
    """掃 data_root 下每個 bank sqlite。"""
    import sqlite3

    from backend.core.bank_data import data_root

    out: dict = {}
    for path in sorted(data_root().glob("*.sqlite")):
        if path.name == "server.sqlite":  # server DB 沒有交易表
            continue
        conn = sqlite3.connect(str(path))
        try:
            for table in TABLES:
                if not _table_exists_sqlite(conn, table):
                    continue
                if not _has_taxonomy_columns_sqlite(conn, table):
                    print(f"[skip] {path.name}:{table} 缺 taxonomy 欄位 (舊 schema)",
                          file=sys.stderr)
                    continue
                key = f"{path.name}:{table}"
                out[key] = _backfill_table(
                    conn, table, is_postgres=False, dry_run=dry_run,
                )
        finally:
            conn.close()
    return out


def backfill_for_postgres(dry_run: bool = False) -> dict:
    """掃每家銀行的 PG schema。"""
    import psycopg

    from backend.core.bank_pg import _dsn, schema_name
    from backend.core.creds import ALL_CREDS

    out: dict = {}
    with psycopg.connect(_dsn()) as conn:
        for creds_cls in ALL_CREDS:
            schema = schema_name(creds_cls.BANK.lower())
            with conn.cursor() as c:
                c.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = %s AND table_name = ANY(%s)",
                    (schema, list(TABLES)),
                )
                existing = {r[0] for r in c.fetchall()}
            if not existing:
                continue
            with conn.cursor() as c:
                c.execute(f'SET search_path TO "{schema}", public')
            for table in TABLES:
                if table not in existing:
                    continue
                out[f"{schema}:{table}"] = _backfill_table(
                    conn, table, is_postgres=True, dry_run=dry_run,
                )
    return out


def _print_result(backend: str, result: dict, dry_run: bool) -> None:
    print(f"\n=== backend={backend}  dry_run={dry_run} ===")
    if not result:
        print("  (no tables found)")
        return
    for key, r in result.items():
        print(f"  [{key}] total={r['total']}  changed={r['changed']}")
        for s in r["sample_changes"]:
            print(f"      id={s['id']}  amt={s['amount']}  cat={s['category']!r}  "
                  f"{s['old']}  →  {s['new']}")


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="backfill_flow_type_20260728",
        description="重算 flow_type / income_category (三個 upsert 從未寫入此兩欄)",
    )
    ap.add_argument("--execute", action="store_true",
                    help="真的 UPDATE (預設是 dry-run 只印報告)")
    args = ap.parse_args()
    dry_run = not args.execute

    backend = os.environ.get("DB_BACKEND", "sqlite").lower()
    result = (backfill_for_postgres(dry_run) if backend in ("postgres", "pg")
              else backfill_for_sqlite(dry_run))
    _print_result(backend, result, dry_run)
    if dry_run:
        print("\n(dry-run — 加 --execute 才會真的寫入)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
