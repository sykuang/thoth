"""Backfill: 對既有 row 重跑 categorizer，補 category / subcategory / auto_excluded
/ flow_type / income_category / is_subscription 全套 taxonomy 欄位。

背景
====
2026-07-28 追 flow_type 時發現這是一整批死欄位，不只 flow_type：

  1. `backend/core/store` 三個 INSERT 從沒寫過 flow_type / income_category /
     is_subscription（全靠 schema DEFAULT）
  2. `cli/cli.py` 13 個 persist 分支全都沒傳 `rules=` → category / subcategory /
     auto_excluded 也全是 NULL

`migrations/backfill_flow_type_20260728.py` 只修了 (1) 的 flow 方向，但因為
category 是 NULL，樂天「存款利息」只能落到 `income/other` 而非 `interest_dividend`。
要真正修好必須**先補 category，再由 category 推導 flow**，也就是這支。

為什麼不重抓銀行
================
categorizer 的輸入（description / counterparty_acct / memo）全都已經在 DB 裡，
categorize 是純函數。重抓 13 家銀行要真憑證 + CAPTCHA + OTP，而且台幣明細普遍
只給 6 個月（樂天已實證），重抓反而會失去更早的既有資料。

使用者覆寫保護
==============
「修正≠刪除」鐵則。以下 row 一律**完全跳過**，不動任何欄位：

  - `description_overwrite` 有值 → 使用者改過說明
  - `tags_overwrite` 有值 → 使用者加過標籤

這兩欄是「使用者碰過這筆」的唯一可靠信號（category 本身無法區分是 rule 寫的
還是人工設的）。保守起見寧可漏修也不覆蓋人工結果。

用法
====
mac local:
    DB_BACKEND=sqlite uv run python -m migrations.recategorize_20260728            # dry-run
    DB_BACKEND=sqlite uv run python -m migrations.recategorize_20260728 --execute

prod pod (PG):
    az containerapp exec -n thoth-backend-vnet -g thoth-rg \\
        --command "python -m migrations.recategorize_20260728"
    az containerapp exec -n thoth-backend-vnet -g thoth-rg \\
        --command "python -m migrations.recategorize_20260728 --execute"

退場
====
跑完即可刪。這支取代 `backfill_flow_type_20260728.py`（後者是同一問題的較窄版本，
本支跑完後那支會回報 0 changed）。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.store import _flow_fields, _is_subscription
from backend.server.categorizer import categorize_with_excluded

# table → 是否為信用卡表（信用卡 amount 方向不可信，_flow_fields 傳 None）
TABLES = {
    "twd_transactions": False,
    "card_billed_txns": True,
    "card_pending_txns": True,
}

TAXONOMY_COLS = {
    "category", "subcategory", "auto_excluded",
    "flow_type", "income_category", "is_subscription",
    "description_overwrite", "tags_overwrite",
}


def _categorizer_text(desc, counterparty, memo) -> str:
    """跟 store._categorizer_text 同規則：desc | counterparty | memo 去重後 join。

    不 import store 的版本是因為它吃 dict（crawler payload shape），這裡吃的是
    DB row 欄位。規則必須一致，改動時兩邊要同步。
    """
    out, seen = [], set()
    for p in (desc, counterparty, memo):
        s = (p or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return " | ".join(out)


def _load_rules() -> list[dict]:
    """user_id=1 的 rules，沒 seed 過就用 DEFAULT_RULES（對齊 cli/cli.py）。"""
    from backend.server import rules_repo
    from backend.server.seed_rules import DEFAULT_RULES
    try:
        rules = rules_repo.list_rules(user_id=1, enabled_only=True)
    except Exception:
        rules = []
    if not rules:
        rules = sorted(DEFAULT_RULES, key=lambda r: -r.get("priority", 100))
    return rules


def _fetch(cur, table: str) -> list:
    if table == "twd_transactions":
        cols = ("description", "counterparty_acct", "memo",
                "COALESCE(income, 0) - COALESCE(expend, 0)", "NULL")
    else:
        cols = ("description", "NULL", "NULL", "amount", "txn_type")
    cur.execute(
        f"SELECT id, {cols[0]}, {cols[1]}, {cols[2]}, {cols[3]}, {cols[4]}, "
        f"category, subcategory, auto_excluded, flow_type, income_category, "
        f"is_subscription, description_overwrite, tags_overwrite FROM {table}",
    )
    return cur.fetchall()


def _flow_is_authoritative(cat: str | None, sub: str | None,
                           txn_type: str | None) -> bool:
    """這筆的 flow_type 是否有「權威依據」，而非靠金額正負猜的？

    2026-07-28 dry-run 抓到的破壞風險：sinopac 有 row `desc='...轉聯邦'`、
    `category=NULL` 但 `flow_type='transfer'` 是對的（來自已被 squash 掉的
    Phase 4 script，當年有更完整的轉帳判斷）。若無條件用 `_flow_fields` 的
    「金額為正即 income / 為負即 expense」fallback 覆寫，會把正確的 transfer
    降級成 expense —— 那是**資料破壞不是修復**。

    同型風險：`desc='SHOPEE'` amount=+1014 是蝦皮退款，category 命中『購物』
    但『購物』不在 `_FLOW_BY_CATEGORY`，一樣落到金額 fallback → income/other，
    覆蓋掉原本正確的 transfer。

    所以只在下列情況才覆寫 flow_type / income_category：
      - 信用卡 txn_type 有明確映射（銀行給的，最權威）
      - category 命中 `_FLOW_BY_CATEGORY`（薪資/獎金/利息股息/投資收益/轉帳/還款/投資）
      - 其他/退稅 這個明確 subcategory
    其餘一律保留既有值。
    """
    from backend.core.store import _FLOW_BY_CATEGORY
    if txn_type in ("cashback", "refund", "fee_waiver", "payment",
                    "spending", "fee", "annual_fee", "installment"):
        return True
    if cat in _FLOW_BY_CATEGORY:
        return True
    return cat == "其他" and sub == "退稅"


def _backfill_table(conn, table: str, rules: list[dict],
                    is_postgres: bool, dry_run: bool) -> dict:
    ph = "%s" if is_postgres else "?"
    is_card = TABLES[table]
    cur = conn.cursor()
    rows = _fetch(cur, table)
    total = len(rows)
    changed = 0
    skipped_user = 0
    sample: list[dict] = []

    for (row_id, desc, counterparty, memo, amount, txn_type,
         old_cat, old_sub, old_auto, old_flow, old_ic, old_subs,
         desc_ow, tags_ow) in rows:
        # 「修正≠刪除」— 使用者碰過的 row 一律不動
        if desc_ow or tags_ow:
            skipped_user += 1
            continue

        text = _categorizer_text(desc, counterparty, memo)
        cat, sub, auto_ex = categorize_with_excluded(text, rules)
        subs = 1 if _is_subscription(sub) else 0
        auto = 1 if auto_ex else 0

        # flow_type 只在有權威依據時覆寫，否則保留既有值（見 _flow_is_authoritative）
        if _flow_is_authoritative(cat, sub, txn_type):
            flow, income_cat = _flow_fields(
                cat, sub, None if is_card else amount, txn_type,
            )
        else:
            flow, income_cat = old_flow, old_ic

        new = (cat, sub, auto, flow, income_cat, subs)
        old = (old_cat, old_sub, int(old_auto or 0), old_flow, old_ic,
               int(old_subs or 0))
        if new == old:
            continue

        changed += 1
        if len(sample) < 40:
            sample.append({
                "id": row_id, "desc": (desc or "")[:34], "amount": amount,
                "old": f"{old_cat}/{old_sub} {old_flow}/{old_ic}",
                "new": f"{cat}/{sub} {flow}/{income_cat}",
            })
        if not dry_run:
            cur.execute(
                f"UPDATE {table} SET category = {ph}, subcategory = {ph}, "
                f"auto_excluded = {ph}, flow_type = {ph}, income_category = {ph}, "
                f"is_subscription = {ph} WHERE id = {ph}",
                (cat, sub, auto, flow, income_cat, subs, row_id),
            )

    if not dry_run and changed > 0:
        conn.commit()
    cur.close()
    return {"total": total, "changed": changed,
            "skipped_user": skipped_user, "sample_changes": sample}


def _has_taxonomy_columns(conn, table: str) -> bool:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    return cols >= TAXONOMY_COLS


def backfill_for_sqlite(rules: list[dict], dry_run: bool = False) -> dict:
    import sqlite3

    from backend.core.bank_data import data_root

    out: dict = {}
    for path in sorted(data_root().glob("*.sqlite")):
        if path.name == "server.sqlite":
            continue
        conn = sqlite3.connect(str(path))
        try:
            for table in TABLES:
                exists = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if not exists:
                    continue
                if not _has_taxonomy_columns(conn, table):
                    print(f"[skip] {path.name}:{table} 缺 taxonomy 欄位 (舊 schema)",
                          file=sys.stderr)
                    continue
                out[f"{path.name}:{table}"] = _backfill_table(
                    conn, table, rules, is_postgres=False, dry_run=dry_run,
                )
        finally:
            conn.close()
    return out


def backfill_for_postgres(rules: list[dict], dry_run: bool = False) -> dict:
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
                    conn, table, rules, is_postgres=True, dry_run=dry_run,
                )
    return out


def _print_result(backend: str, result: dict, dry_run: bool) -> None:
    print(f"\n=== backend={backend}  dry_run={dry_run} ===")
    if not result:
        print("  (no tables found)")
        return
    tot = ch = sk = 0
    for key, r in result.items():
        tot += r["total"]
        ch += r["changed"]
        sk += r["skipped_user"]
        if r["changed"] == 0 and r["skipped_user"] == 0:
            continue
        print(f"  [{key}] total={r['total']}  changed={r['changed']}  "
              f"skipped_user={r['skipped_user']}")
        for s in r["sample_changes"]:
            print(f"      id={s['id']}  amt={s['amount']}  desc={s['desc']!r}")
            print(f"          {s['old']}  →  {s['new']}")
    print(f"\n  TOTAL rows={tot}  changed={ch}  skipped(user-edited)={sk}")


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="recategorize_20260728",
        description="重跑 categorizer 補全 taxonomy 欄位 (category→flow_type→is_subscription)",
    )
    ap.add_argument("--execute", action="store_true",
                    help="真的 UPDATE (預設 dry-run 只印報告)")
    args = ap.parse_args()
    dry_run = not args.execute

    rules = _load_rules()
    print(f"[rules] {len(rules)} 條 enabled rules 載入", file=sys.stderr)

    backend = os.environ.get("DB_BACKEND", "sqlite").lower()
    result = (backfill_for_postgres(rules, dry_run) if backend in ("postgres", "pg")
              else backfill_for_sqlite(rules, dry_run))
    _print_result(backend, result, dry_run)
    if dry_run:
        print("\n(dry-run — 加 --execute 才會真的寫入)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
