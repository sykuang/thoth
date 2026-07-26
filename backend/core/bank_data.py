"""Shared access helpers for per-bank data.

DB_BACKEND is the single source of truth for the whole data layer:
- sqlite: server.sqlite + per-bank {bank}.sqlite files under BANK_DATA_ROOT
- postgres: server state tables + per-bank schemas (`bank_hsbc`, `bank_ctbc`, ...)

server state and bank data are different domain models, but not different storage
configuration knobs. One config chooses the backend for both.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from backend.core import bank_pg

KNOWN_BANKS = (
    "cathay", "ctbc", "dbs", "esun", "fubon", "hsbc",
    "linebank", "rakuten", "scb", "scsb", "sinopac", "taishin", "ubot",
)


def data_root() -> Path:
    root = os.environ.get("BANK_DATA_ROOT")
    if root:
        return Path(root)
    return Path(__file__).resolve().parents[1] / "data"


def open_bank_db(bank: str) -> Any | None:
    if bank_pg.enabled():
        return bank_pg.connect(bank)
    path = data_root() / f"{bank}.sqlite"
    if not path.exists():
        return None
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    return con


def has_table(con: Any, table: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def table_names(con: Any) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def columns(con: Any, table: str) -> set[str]:
    return {r["name"] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}


def fallback_banks_with_data() -> list[str]:
    if bank_pg.enabled():
        return list(KNOWN_BANKS)
    return [b for b in KNOWN_BANKS if (data_root() / f"{b}.sqlite").exists()]


