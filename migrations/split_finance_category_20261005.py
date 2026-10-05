"""Split 金融 into 金融費用 / 保險 / 稅費 and drop the 電子支付 pseudo-category.

電子支付 described the payment rail (ＡＰＥ gateway, 街口, TWQR), not what was bought;
every local row under it was a real merchant purchase. Those rows are re-run through
the user's rules (minus the two rail rules); no match leaves them uncategorized.

Dry-run by default; prints every changed rule and row:

    uv run python -m migrations.split_finance_category_20261005
    uv run python -m migrations.split_finance_category_20261005 --execute

Works on SQLite and PG through ``db.open_bank_conn`` / ``db.get_conn``.
Delete after it reports zero changes in production.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TABLES = ("twd_transactions", "card_billed_txns", "card_pending_txns")
OLD = "金融"
RAIL_SUB = "電子支付"
# Only untouched historical defaults are deleted; user-edited patterns survive (as 金融費用).
RAIL_RULES = {
    "玉山APE付款": r"^ＡＰＥ|^APE\d|ＡＰＥ４９５９|ＡＰＥ４７２２",
    "街口支付": r"街口|ＴＷＱＲ|TWQR|跨機構購物|電子支付",
}


def new_category(sub: str | None, name: str = "") -> tuple[str, str | None]:
    """金融/<sub> → (category, subcategory). 電子支付 is handled by recategorize.

    Legacy rules predate subcategories (sub=None), so fall back to the rule name.
    """
    if sub == "保險" or (sub is None and "保險" in name):
        return ("保險", None)
    if sub in ("稅", "罰款"):
        return ("稅費", sub)
    return ("金融費用", sub)


def migrate_rules(rules: list[dict]) -> tuple[list[dict], list[tuple]]:
    """Return (rules after migration, [(action, rule_id, name, new_cat, new_sub)])."""
    out, actions = [], []
    for r in rules:
        if r["category"] != OLD:
            out.append(r)
            continue
        if RAIL_RULES.get(r["name"]) == r["pattern"]:
            actions.append(("delete", r["id"], r["name"], None, None))
            continue
        cat, sub = new_category(r["subcategory"], r["name"])
        actions.append(("update", r["id"], r["name"], cat, sub))
        out.append({**r, "category": cat, "subcategory": sub})
    return out, actions


def run(*, execute: bool) -> dict:
    from backend.core.store import _flow_fields, _is_subscription
    from backend.server import db, rules_repo
    from backend.server.categorizer import categorize_with_excluded
    from backend.server.routers.rules import SUPPORTED_BANKS

    result = {"rules_updated": 0, "rules_deleted": 0, "rows_renamed": 0,
              "rows_recategorized": 0, "rows_skipped_user": 0}
    with db.get_conn() as conn:
        user_ids = [r[0] for r in conn.execute("SELECT id FROM users ORDER BY id").fetchall()]

    for uid in user_ids:
        rules, actions = migrate_rules(rules_repo.list_rules(user_id=uid, enabled_only=False))
        enabled = [r for r in rules if r["enabled"]]
        for action, rid, name, cat, sub in actions:
            print(f"[rule-{action}] user={uid} id={rid} {name!r} -> {cat}/{sub}")
            result[f"rules_{action}d"] += 1
            if not execute:
                continue
            if action == "delete":
                rules_repo.delete_rule(user_id=uid, rule_id=rid)
            else:
                rules_repo.update_rule(user_id=uid, rule_id=rid, category=cat, subcategory=sub)

        for bank in SUPPORTED_BANKS:
            con = db.open_bank_conn(bank)
            if con is None:
                continue
            try:
                for table in TABLES:
                    cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
                    if "category" not in cols:
                        continue
                    is_card = table != "twd_transactions"
                    amount = "amount" if is_card else "COALESCE(income,0)-COALESCE(expend,0)"
                    txn_type = "txn_type" if is_card else "NULL"
                    rows = con.execute(
                        f"SELECT id, description, subcategory, {amount} AS amt, "
                        f"{txn_type} AS txn_type, description_overwrite, tags_overwrite "
                        f"FROM {table} WHERE user_id=? AND category=?",
                        (uid, OLD),
                    ).fetchall()
                    for row in rows:
                        rid, desc, sub = row[0], row[1], row[2]
                        if sub != RAIL_SUB:
                            cat, nsub = new_category(sub)
                            result["rows_renamed"] += 1
                            if execute:
                                con.execute(
                                    f"UPDATE {table} SET category=?, subcategory=? "
                                    "WHERE id=? AND user_id=?", (cat, nsub, rid, uid))
                            continue
                        if row[5] or row[6]:
                            result["rows_skipped_user"] += 1
                            print(f"[skip-user] {bank}.{table} id={rid} desc={desc!r}")
                            continue
                        cat, nsub, auto_ex = categorize_with_excluded(desc, enabled)
                        flow, inc = _flow_fields(cat, nsub, None if is_card else row[3], row[4])
                        result["rows_recategorized"] += 1
                        print(f"[recat] {bank}.{table} id={rid} desc={desc!r} "
                              f"-> {cat}/{nsub} {flow}")
                        if execute:
                            con.execute(
                                f"UPDATE {table} SET category=?, subcategory=?, auto_excluded=?, "
                                "flow_type=?, income_category=?, is_subscription=? "
                                "WHERE id=? AND user_id=?",
                                (cat, nsub, int(auto_ex), flow, inc,
                                 int(_is_subscription(nsub)), rid, uid))
                if execute:
                    con.commit()
            finally:
                con.close()
    print(f"[total] execute={execute} {result}")
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    run(execute=ap.parse_args().execute)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
