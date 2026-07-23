"""Backfill: 重跑 UBOT card_billed_txns / card_pending_txns 的 txn_type + flow_type.

背景
====
0.3.64 之前 `backend/core/classify.py`:
  - classify_ubot 不認 txCode=55 (貸記/退款) → 掉回 fallback →「年費」desc 命中
    ANNUAL_FEE (flow_type=expense) → UI 顯示 -NT$5,000 紅色, 實際上該筆是「年費減免」
    貸記 (該顯示 +NT$5,000 綠色 income).
  - classify_ubot code=='60' 硬歸 FEE, 讓 'ANNUAL MEMBERSHIP FEE' 失去 annual_fee 分類.

Real evidence (使用者 2026-07-04 反映):
  ****7027 stmt 20260703 的配對:
    txCode=55  txAmt=-5000  txDesc='微風無限卡正卡年費減免'
    txCode=60  txAmt=+5000  txDesc='ANNUAL MEMBERSHIP FEE'

0.3.64 修 classify.py 後**新** sync 進來的 row 已對, 但舊 row 已在 DB 用錯誤
txn_type='annual_fee' / flow_type='expense' 落地; `upsert_card_billed` 走
`ON CONFLICT(user_id, dedup_key) DO NOTHING`, 重跑 sync **不會** update 舊 row.
本 script 一次性掃 ubot card_billed_txns + card_pending_txns 用**新** classifier
重新分類, 只 update 分類跟前一版不同的 row.

策略
====
1. 因為 schema 沒存 txCode, 只能用 description keyword 反推該筆是不是 55/60:
   - desc 含「年費減免」/「退回」/「沖銷」 + amount<0  → 之前 annual_fee, 應改 refund
   - desc 含「ANNUAL MEMBERSHIP FEE」/「ANNUAL FEE」 + amount>0  → 應改 annual_fee
   為了保守, 用**新**版 classify_by_desc_and_sign() 判 (無 txCode 訊號), 只更新
   `new != old` 的 row.
2. 因為 desc-only 判無法完美還原 code=55/60 語意 (舊 code=55 row desc 若沒
   '年費/減免' 關鍵字, backfill 抓不出來). 這種漏網 row 需下次 sync 補 —— 但目前
   `DO NOTHING` 政策讓漏網 row 永久留錯, 是已知 limitation. 若要 100% 覆蓋,
   應該進一步在 store 加 `DO UPDATE SET txn_type` (另立議題).
3. flow_type 從 txn_type 反推:
     spending / annual_fee / fee / installment  → expense
     cashback / refund                          → income
     payment                                     → neutral (schema 存 'income'?)
     unknown                                     → 保守用 amount 符號

用法
====
mac local (SQLite):
    DB_BACKEND=sqlite uv run python -m migrations.backfill_ubot_txn_type_20260704            # dry-run
    DB_BACKEND=sqlite uv run python -m migrations.backfill_ubot_txn_type_20260704 --execute  # 真 UPDATE

prod pod (PG, via `az containerapp exec`):
    az containerapp exec -n thoth-backend-vnet -g thoth-rg \\
        --command "python -m migrations.backfill_ubot_txn_type_20260704"            # dry-run
    az containerapp exec -n thoth-backend-vnet -g thoth-rg \\
        --command "python -m migrations.backfill_ubot_txn_type_20260704 --execute"  # 真跑

退場
====
所有 ubot user 都 backfill 完後刪除. 或若加寬到「所有銀行都掃 txn_type」則 rename
成通用版留著. 這一版 hard-code ubot schema, 因為 code=55 事故只在 ubot 觸發.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 加 path 讓 standalone 跑得起來
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 引入新版 classifier
from backend.core.classify import (
    ANNUAL_FEE,
    CASHBACK,
    FEE,
    FEE_WAIVER,
    INSTALLMENT,
    PAYMENT,
    REFUND,
    SPENDING,
    UNKNOWN,
    classify_by_desc_and_sign,
)


def _flow_type_for(txn_type: str, amount: int | float | None) -> str:
    """把 txn_type 映射到 flow_type (跟 store 一致的規則)."""
    if txn_type in (CASHBACK, REFUND, FEE_WAIVER):
        # FEE_WAIVER (年費/手續費/利息減免): 銀行減免費用, 對 user 是 income (綠色).
        # 語意上獨立於 REFUND (商家退款), 但 flow_type 都歸 income.
        return "income"
    if txn_type == PAYMENT:
        # 還款不算收入不算支出; DB schema 用 'neutral' 或 'income' 依版本, 先給 'neutral'
        # 若 schema 是 CHECK constraint 只接 income/expense, 後端 render 也是把 payment
        # 當 zero, 這裡先保留 'expense' 讓 UI 看到「還款 -X」不會太離譜, 但實際上
        # renderAmount 對 txn_type=payment 一律 direction=zero, 前端顯示中性.
        # 為了不破 CHECK constraint, 先讀 DB 現況決定; 此 script 遇到 payment row
        # 不會改 flow_type (保留舊值), 只改 txn_type.
        return "neutral"
    # spending / annual_fee / fee / installment → expense
    if txn_type in (SPENDING, ANNUAL_FEE, FEE, INSTALLMENT):
        return "expense"
    # unknown → 純看金額符號
    if amount is None:
        return "expense"
    return "expense" if float(amount) < 0 else "income"


def _reclassify_row(desc: str | None, amount: int | float | None) -> str:
    """用**新版**通用 classifier 重新判分類 (無 txCode 訊號可用)."""
    return classify_by_desc_and_sign(desc, amount)


def _fetch_rows(cur, table: str, is_postgres: bool):
    """讀該表 (id, description, amount, txn_type, flow_type)."""
    sql = f"SELECT id, description, amount, txn_type, COALESCE(flow_type, '') FROM {table}"
    cur.execute(sql)
    return cur.fetchall()


def _update_row(cur, table: str, row_id, new_txn_type: str, new_flow_type: str,
                is_postgres: bool, update_flow_type: bool):
    """UPDATE 單筆的 txn_type (+ optional flow_type)."""
    if update_flow_type:
        placeholder = "%s" if is_postgres else "?"
        sql = (f"UPDATE {table} SET txn_type = {placeholder}, "
               f"flow_type = {placeholder} WHERE id = {placeholder}")
        cur.execute(sql, (new_txn_type, new_flow_type, row_id))
    else:
        placeholder = "%s" if is_postgres else "?"
        sql = f"UPDATE {table} SET txn_type = {placeholder} WHERE id = {placeholder}"
        cur.execute(sql, (new_txn_type, row_id))


def _backfill_table(conn, table: str, is_postgres: bool, dry_run: bool) -> dict:
    """對一張表跑 backfill.

    Returns: {'total': int, 'changed': int, 'sample_changes': list[dict]}
    """
    cur = conn.cursor()
    rows = _fetch_rows(cur, table, is_postgres)
    total = len(rows)
    changed = 0
    sample: list[dict] = []
    for r in rows:
        row_id, desc, amount, old_type, old_flow = r
        new_type = _reclassify_row(desc, amount)
        if new_type == (old_type or ""):
            continue
        # 有變化 — 判 flow_type 是否也要 update
        new_flow = _flow_type_for(new_type, amount)
        flow_changed = new_flow != (old_flow or "")
        if len(sample) < 30:
            sample.append({
                "id": row_id,
                "desc": (desc or "")[:60],
                "amount": amount,
                "old": f"{old_type}/{old_flow}",
                "new": f"{new_type}/{new_flow}",
            })
        if not dry_run:
            _update_row(cur, table, row_id, new_type, new_flow,
                        is_postgres, update_flow_type=flow_changed)
        changed += 1
    if not dry_run and changed > 0:
        conn.commit()
    cur.close()
    return {"total": total, "changed": changed, "sample_changes": sample}


def backfill_for_sqlite(dry_run: bool = False) -> dict:
    """對 mac local ubot.sqlite 跑 backfill."""
    import sqlite3
    from backend.core.bank_data import data_root
    path = data_root() / "ubot.sqlite"
    if not path.exists():
        print(f"[skip] {path} 不存在", file=sys.stderr)
        return {"card_billed_txns": None, "card_pending_txns": None}
    conn = sqlite3.connect(str(path))
    out: dict = {}
    try:
        for table in ("card_billed_txns", "card_pending_txns"):
            # 確認表存在
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            )
            if not cur.fetchone():
                out[table] = None
                continue
            out[table] = _backfill_table(conn, table, is_postgres=False, dry_run=dry_run)
    finally:
        conn.close()
    return out


def backfill_for_postgres(dry_run: bool = False) -> dict:
    """對 prod PG bank_ubot schema 跑 backfill."""
    import psycopg
    from backend.core.bank_pg import _dsn, schema_name

    schema = schema_name("ubot")
    dsn = _dsn()
    out: dict = {}
    with psycopg.connect(dsn) as conn:
        # 切 search_path 到 bank_ubot
        with conn.cursor() as c:
            c.execute(f'SET search_path TO "{schema}", public')
            # 表存在檢查
            c.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = %s "
                "AND table_name IN ('card_billed_txns', 'card_pending_txns')",
                (schema,),
            )
            existing = {r[0] for r in c.fetchall()}
        for table in ("card_billed_txns", "card_pending_txns"):
            if table not in existing:
                out[table] = None
                continue
            out[table] = _backfill_table(conn, table, is_postgres=True, dry_run=dry_run)
    return out


def _print_result(backend: str, result: dict, dry_run: bool) -> None:
    print(f"\n=== backend={backend}  dry_run={dry_run} ===")
    for table, r in result.items():
        if r is None:
            print(f"  [{table}] table not found, skipped")
            continue
        print(f"  [{table}] total={r['total']}  changed={r['changed']}")
        if r["sample_changes"]:
            print("    sample changes (first 30):")
            for s in r["sample_changes"]:
                print(f"      id={s['id']}  amt={s['amount']}  "
                      f"{s['old']}  →  {s['new']}    desc={s['desc']!r}")


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="backfill_ubot_txn_type_20260704",
        description="重跑 UBOT card_billed_txns / card_pending_txns txn_type",
    )
    ap.add_argument(
        "--execute",
        action="store_true",
        help="預設 dry-run 只報 sample; --execute 才真跑 UPDATE",
    )
    args = ap.parse_args()

    dry_run = not args.execute
    backend = os.environ.get("DB_BACKEND", "sqlite").lower()

    if backend == "postgres":
        result = backfill_for_postgres(dry_run=dry_run)
    else:
        result = backfill_for_sqlite(dry_run=dry_run)

    _print_result(backend, result, dry_run)

    if dry_run:
        print("\n[dry-run] 尚未實際 UPDATE. 若確認, 重跑加 --execute", file=sys.stderr)
    else:
        print("\n[done] UPDATE 已 commit", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
