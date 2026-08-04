"""Repair loan-interest expenses misclassified as passive income.

Dry-run by default. Production usage runs inside the VNet Container Apps Job:

    uv run python -m migrations.fix_loan_interest_20260804
    uv run python -m migrations.fix_loan_interest_20260804 --execute

The migration also installs the new high-priority rule for existing users. New
users receive the same rule from ``DEFAULT_RULES``.
"""
from __future__ import annotations

import argparse
import os
import re
from collections.abc import Mapping


RULE = {
    "name": "貸款利息支出",
    "pattern": r"放款利息|貸款利息|借款利息|循環息|循環利息",
    "category": "金融",
    "subcategory": "貸款利息",
    "priority": 300,
}
_LOAN_INTEREST = re.compile(RULE["pattern"], re.IGNORECASE)


def _should_repair(row: Mapping) -> bool:
    """Only repair untouched expense rows with the exact stale income shape."""
    return bool(
        _LOAN_INTEREST.search(str(row.get("description") or ""))
        and (row.get("expend") or 0) > 0
        and (row.get("income") or 0) <= 0
        and row.get("category") == "利息股息"
        and row.get("flow_type") == "income"
        and row.get("income_category") == "interest_dividend"
        and not row.get("description_overwrite")
        and not row.get("tags_overwrite")
    )


def run(*, execute: bool) -> dict:
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL 未設")

    result = {"rules_added": 0, "rows_repaired": 0, "rows_skipped_user": 0}
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, email FROM users ORDER BY id")
            users = cur.fetchall()
            print(f"[users] count={len(users)}")
            for user in users:
                cur.execute(
                    "SELECT 1 FROM category_rules WHERE user_id=%s AND name=%s LIMIT 1",
                    (user["id"], RULE["name"]),
                )
                if cur.fetchone():
                    continue
                print(f"[rule] add user_id={user['id']} email={user['email']}")
                result["rules_added"] += 1
                if execute:
                    cur.execute(
                        "INSERT INTO category_rules "
                        "(user_id, name, pattern, category, subcategory, priority, enabled, "
                        "auto_excluded, created_at, updated_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,1,0,"
                        "to_char(now() at time zone 'utc', 'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"'),"
                        "to_char(now() at time zone 'utc', 'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"'))",
                        (user["id"], RULE["name"], RULE["pattern"], RULE["category"],
                         RULE["subcategory"], RULE["priority"]),
                    )

            cur.execute(
                "SELECT table_schema FROM information_schema.tables "
                "WHERE table_name='twd_transactions' AND table_schema LIKE 'bank_%' "
                "ORDER BY table_schema",
            )
            schemas = [row["table_schema"] for row in cur.fetchall()]
            for schema in schemas:
                cur.execute(sql.SQL(
                    "SELECT id, user_id, description, expend, income, category, subcategory, "
                    "flow_type, income_category, description_overwrite, tags_overwrite "
                    "FROM {}.twd_transactions "
                    "WHERE description ~* %s ORDER BY user_id, id",
                ).format(sql.Identifier(schema)), (RULE["pattern"],))
                for row in cur.fetchall():
                    if row["description_overwrite"] or row["tags_overwrite"]:
                        result["rows_skipped_user"] += 1
                        print(
                            f"[skip-user] {schema} id={row['id']} user_id={row['user_id']} "
                            f"desc={row['description']!r}",
                        )
                        continue
                    if not _should_repair(row):
                        print(
                            f"[skip-shape] {schema} id={row['id']} user_id={row['user_id']} "
                            f"desc={row['description']!r} flow={row['flow_type']}/{row['income_category']}",
                        )
                        continue
                    result["rows_repaired"] += 1
                    print(
                        f"[repair] {schema} id={row['id']} user_id={row['user_id']} "
                        f"desc={row['description']!r} expend={row['expend']} "
                        "利息股息/income/interest_dividend -> 金融/貸款利息/expense/NULL",
                    )
                    if execute:
                        cur.execute(sql.SQL(
                            "UPDATE {}.twd_transactions SET "
                            "category='金融', subcategory='貸款利息', auto_excluded=0, "
                            "flow_type='expense', income_category=NULL, is_subscription=0 "
                            "WHERE id=%s AND user_id=%s",
                        ).format(sql.Identifier(schema)), (row["id"], row["user_id"]))

        if execute:
            conn.commit()
        else:
            conn.rollback()
    print(f"[total] execute={execute} {result}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    run(execute=args.execute)
    if not args.execute:
        print("[dry-run] no changes written; add --execute after reviewing every row")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
