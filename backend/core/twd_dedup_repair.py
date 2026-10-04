"""One-off repair for TWD rows duplicated by sync-format drift.

A row stored by one sync (e.g. money as ``80.0``, description with trailing
spaces, or before the multi-currency account split) and the same bank row
stored again by a later sync carry different ``dedup_key`` values, so the
later sync inserted a second copy.

Pairs are matched on account, day and integer money. Only pairs written by
different syncs are touched; same-sync identical rows are real repeats and
are left alone. The older row keeps the user's edits, takes the newer
canonical ``dedup_key`` (so the next sync matches it) and the newer row is
deleted. When the newer row moved to a non-TWD currency, the newer row is
kept instead and inherits the older row's user edits.

Usage: python -m backend.core.twd_dedup_repair <user_id> [--apply]
"""

from __future__ import annotations

import json
import sys

USER_COLUMNS = (
    "category", "subcategory", "auto_excluded", "flow_type", "income_category",
    "is_subscription", "description_overwrite", "tags_overwrite", "splits_overwrite",
)


def _money(value):
    return None if value is None else int(round(float(value)))


def find_pairs(rows: list[dict]) -> list[tuple[dict, dict]]:
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        key = (
            str(row["account_no"]).split(":")[0], str(row["txn_datetime"])[:10],
            _money(row["expend"]), _money(row["income"]), _money(row["balance"]),
        )
        groups.setdefault(key, []).append(row)
    pairs = []
    for group in groups.values():
        if len(group) != 2:
            continue
        old, new = sorted(group, key=lambda r: r["id"])
        if str(old["first_seen"])[:10] == str(new["first_seen"])[:10]:
            continue  # same sync: genuine repeated transaction
        pairs.append((old, new))
    return pairs


def plan(rows: list[dict]) -> list[dict]:
    actions = []
    for old, new in find_pairs(rows):
        currency_moved = (old["currency"] or "TWD") != (new["currency"] or "TWD")
        keep, drop = (new, old) if currency_moved else (old, new)
        actions.append({
            "keep": keep["id"], "drop": drop["id"], "date": str(keep["txn_datetime"])[:10],
            "currency_moved": currency_moved,
            # currency moved: newer row keeps its key and inherits the older row's edits
            "edits": {c: old[c] for c in USER_COLUMNS} if currency_moved else None,
            "dedup_key": None if currency_moved else new["dedup_key"],
        })
    return actions


def main(argv: list[str]) -> None:
    from backend.core.bank_data import KNOWN_BANKS
    from backend.core.store import BankStore

    user_id, apply = int(argv[0]), "--apply" in argv
    total = 0
    for bank in KNOWN_BANKS:
        store = BankStore(bank, user_id=user_id)
        try:
            actions = plan(store.twd_dedup_rows(USER_COLUMNS))
            if apply:
                store.apply_twd_dedup(actions, USER_COLUMNS)
        finally:
            store.close()
        total += len(actions)
        if actions:
            print("DEDUP", bank, json.dumps(
                [{k: a[k] for k in ("keep", "drop", "date", "currency_moved")} for a in actions]
            ), flush=True)
    print("DEDUP_TOTAL", total, "applied" if apply else "dry-run", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
