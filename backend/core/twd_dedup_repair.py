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

Card billed rows get the same treatment (``plan_cards``): grouped on posted
row identity with a blank-or-equal card number, each group keeps as many rows
as the largest single sync wrote, the oldest survive, a survivor inherits a
missing card number / user edits from a dropped copy.

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


def plan_cards(rows: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        if not row["post_date"]:
            continue
        key = (str(row["consume_date"])[:10], str(row["post_date"])[:10],
               " ".join(str(row["description"] or "").split()), _money(row["amount"]),
               row["consume_amount"])
        groups.setdefault(key, []).append(row)
    actions = []
    for group in groups.values():
        cards = {r["card_no"] for r in group if r["card_no"]}
        if len(group) < 2 or len(cards) > 1:
            continue  # different cards are different purchases
        per_sync: dict[str, int] = {}
        for r in group:
            day = str(r["first_seen"])[:10]
            per_sync[day] = per_sync.get(day, 0) + 1
        keep_n = max(per_sync.values())
        if keep_n == len(group):
            continue
        group.sort(key=lambda r: r["id"])
        kept, dropped = group[:keep_n], group[keep_n:]
        for i, keep in enumerate(kept):
            edits = {}
            if not keep["card_no"] and cards:
                edits["card_no"] = next(iter(cards))
            donor = next((d for d in dropped if d["category"]), None)
            if keep["category"] is None and donor and i == 0:
                edits.update({c: donor[c] for c in USER_COLUMNS})
            actions.append({"keep": keep["id"], "drop": [d["id"] for d in dropped] if i == 0 else [],
                            "date": str(keep["consume_date"])[:10], "edits": edits or None})
    return [a for a in actions if a["drop"] or a["edits"]]


def main(argv: list[str]) -> None:
    from backend.core.bank_data import KNOWN_BANKS
    from backend.core.store import BankStore

    user_id, apply = int(argv[0]), "--apply" in argv
    total = 0
    for bank in KNOWN_BANKS:
        store = BankStore(bank, user_id=user_id)
        try:
            actions = plan(store.twd_dedup_rows(USER_COLUMNS))
            card_actions = plan_cards(store.card_dedup_rows(USER_COLUMNS))
            if apply:
                store.apply_twd_dedup(actions, USER_COLUMNS)
                store.apply_card_dedup(card_actions)
        finally:
            store.close()
        total += len(actions) + sum(len(a["drop"]) for a in card_actions)
        if card_actions:
            print("CDEDUP", bank, json.dumps(
                [{k: a[k] for k in ("keep", "drop", "date")} | {"edits": sorted(a["edits"] or {})}
                 for a in card_actions]), flush=True)
        if actions:
            print("DEDUP", bank, json.dumps(
                [{k: a[k] for k in ("keep", "drop", "date", "currency_moved")} for a in actions]
            ), flush=True)
    print("DEDUP_TOTAL", total, "applied" if apply else "dry-run", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
