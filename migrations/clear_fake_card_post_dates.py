"""Clear posting dates that old adapters manufactured from consumption dates.

Dry-run by default. Pass --execute to update known-fake rows only.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


TARGETS: dict[str, tuple[tuple[str, str | None], ...]] = {
    "esun": (("card_billed_txns", None), ("card_pending_txns", None)),
    "taishin": (("card_pending_txns", "realtime"),),
    "fubon": (("card_pending_txns", "realtime"),),
    "sinopac": (("card_pending_txns", "unbilled"),),
    "scsb": (("card_pending_txns", "unbilled"), ("card_pending_txns", "current")),
}


def _has_post_date(conn, table: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any((row["name"] if hasattr(row, "keys") else row[1]) == "post_date" for row in rows)


def clear_known_fakes(conn, bank: str, *, execute: bool, before: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for table, scope in TARGETS.get(bank, ()):
        key = f"{table}:{scope or 'all'}"
        if not _has_post_date(conn, table):
            result[key] = 0
            continue
        timestamp_column = "first_seen" if table == "card_billed_txns" else "refreshed_at"
        where = f"post_date = consume_date AND {timestamp_column} < ?"
        params: tuple[str, ...] = (before,)
        if scope is not None:
            where += " AND scope = ?"
            params += (scope,)
        count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]
        result[key] = int(count)
        if execute and count:
            conn.execute(f"UPDATE {table} SET post_date = NULL WHERE {where}", params)
    if execute:
        conn.commit()
    return result


def _postgres_connection(bank: str):
    from backend.core import bank_pg

    return bank_pg.connect(bank)


def _sqlite_connection(root: Path, bank: str, *, readonly: bool = True):
    path = root / f"{bank}.sqlite"
    if not path.is_file():
        raise FileNotFoundError(f"Refusing to create missing bank database: {path}")
    mode = "ro" if readonly else "rw"
    return sqlite3.connect(f"file:{path}?mode={mode}", uri=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--before", help="Only clear legacy rows older than this ISO timestamp")
    args = parser.parse_args()
    if args.execute and not args.before:
        parser.error("--execute requires --before")
    before = args.before or datetime.now(UTC).isoformat()
    backend = os.environ.get("DB_BACKEND", "sqlite").lower()
    if backend not in {"sqlite", "postgres"}:
        parser.error(f"unsupported DB_BACKEND: {backend}")
    postgres = backend == "postgres"
    root = Path(os.environ.get("BANK_DATA_ROOT", Path(__file__).resolve().parents[1] / "backend/data"))
    report = {}
    for bank in TARGETS:
        conn = (_postgres_connection(bank) if postgres
                else _sqlite_connection(root, bank, readonly=not args.execute))
        try:
            report[bank] = clear_known_fakes(conn, bank, execute=args.execute, before=before)
        finally:
            conn.close()
    print(json.dumps({"execute": args.execute, "before": before, "banks": report}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
