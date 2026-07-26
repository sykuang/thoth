"""Regression sweep: ensure no test-fixture rows leak into production *.sqlite.

This sweep guards against the 2026-06-14 incident where
``test_cards_routes.py::test_upsert_cards_persists_step2_fields`` (and four
sibling tests) used ``monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path)``
to redirect ``BankStore`` into ``tmp_path`` for isolation, BUT the version of
``backend/core/store.py`` deployed at that time read ``DATA_ROOT`` once at
import-time inside ``BankStore.__init__`` and never re-resolved it. The
setattr therefore had no effect, and six fake-card rows
(``S2-CARD-001``, ``PROTECT-001``, ``EXPIRED-001``, ``ACTIVE-001``,
``DEFAULT-001``, ``STABLE-001``) wrote into the user's real ubot.sqlite and
dbs.sqlite, then surfaced in the frontend's bank-accounts list as ghost
cards (``Step 2 card``, ``v2``, ...). The user noticed visually and asked
"why is this junk here?".

The root cause was fixed by introducing ``store.py::_data_root()`` which
reads the ``BANK_DATA_ROOT`` env var on every call and falls back to the
module-level constant; ``BankStore.__init__`` now calls this helper. But the
*leaked rows themselves* did not auto-clean — they had to be manually
deleted. This test is the long-term tripwire so a future regression in
``store.py`` (or a new test that takes a shortcut on isolation) does NOT go
unnoticed until the user spots ghost data in their UI.

The sweep deliberately runs on every pytest invocation against every
production sqlite file. It does NOT delete anything; it only fails loudly
if fixture-shaped rows appear in production paths.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# Repo-relative path. Matches store.py::DATA_ROOT module default.
PRODUCTION_DATA_ROOT = Path(__file__).resolve().parents[1] / "backend" / "data"

# Banks that currently exist in production (extend when new banks ship).
PRODUCTION_BANKS = (
    "cathay", "ubot", "ctbc", "sinopac", "taishin",
    "dbs", "esun", "linebank", "rakuten", "scsb", "hsbc", "fubon", "scb",
)

# SQL LIKE patterns that match the fixture vocabulary used across tests.
# Add more patterns here if new tests introduce new sentinel prefixes.
#
# The mask-style patterns (****7002..****7010) catch fixtures that pre-mask
# their card numbers to ****<last4> with low-numbered sentinels like 0001/
# 0002/0003 — this is how persist_dbs hand-crafts its test cards from
# raw "************7002" before storing as "****7002". Real bank cards
# almost never end in 0001..0009 (BIN ranges issue from 4-digit pools,
# rarely starting at 0001), so these low-numbered mask values are a strong
# fixture signal.
FIXTURE_CARD_PATTERNS = (
    "%-001", "%-002", "%-003",
    "TEST%", "FAKE%",
    "STABLE-%", "EXPIRED-%", "ACTIVE-%", "DEFAULT-%",
    "S2-%", "PROTECT%", "STEP%",
    "PARTIAL", "EMPTY01",
    # mask-style fixtures (persist_dbs test pattern, 2026-06-14 leak):
    "****7002", "****7003", "****7004",
    "****7005", "****7006", "****7007",
    "****7008", "****7009", "****7010",
)

FIXTURE_ACCOUNT_PATTERNS = (
    "%-001", "TEST%", "FAKE%",
    "STABLE-%", "EXPIRED-%", "PROTECT%",
)

# Nicknames / names that are obvious fixture artifacts.
# Bank-real product names (e.g. "CUBE卡") never look like these.
FIXTURE_NAME_PATTERNS = (
    "v1", "v2", "Step 2 card",
    "Default", "Expired", "Active",
    "TestCard", "FakeCard",
)


def _build_where(field: str, patterns: tuple[str, ...]) -> tuple[str, list[str]]:
    clauses = " OR ".join(f"{field} LIKE ?" for _ in patterns)
    return clauses, list(patterns)


def _scan_table(db_path: Path, table: str, key_field: str,
                key_patterns: tuple[str, ...],
                name_field: str | None = None,
                name_patterns: tuple[str, ...] = ()) -> list[tuple]:
    """Return rows from `table` matching any fixture pattern."""
    if not db_path.exists():
        return []
    con = sqlite3.connect(str(db_path))
    try:
        # Confirm table exists; older DBs may pre-date a schema.
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if table not in tables:
            return []
        key_clause, key_args = _build_where(key_field, key_patterns)
        if name_field and name_patterns:
            name_clause, name_args = _build_where(name_field, name_patterns)
            sql = (f"SELECT {key_field}, COALESCE({name_field}, '') "
                   f"FROM {table} WHERE ({key_clause}) OR ({name_clause})")
            args = key_args + name_args
        else:
            sql = f"SELECT {key_field} FROM {table} WHERE {key_clause}"
            args = key_args
        return list(con.execute(sql, args).fetchall())
    finally:
        con.close()


@pytest.mark.parametrize("bank", PRODUCTION_BANKS)
def test_no_fixture_cards_leaked_into_production_sqlite(bank: str) -> None:
    """For every production *.sqlite, cards table must not contain fixture rows."""
    db = PRODUCTION_DATA_ROOT / f"{bank}.sqlite"
    leaks = _scan_table(
        db, "cards", "card_no", FIXTURE_CARD_PATTERNS,
        name_field="name", name_patterns=FIXTURE_NAME_PATTERNS,
    )
    assert not leaks, (
        f"\n\n⚠️  TEST FIXTURE LEAK in production DB: {db}\n"
        f"   Found {len(leaks)} cards rows matching fixture patterns:\n"
        + "\n".join(f"    - {row}" for row in leaks[:10])
        + "\n\n   Root cause is almost certainly a test using "
        "BankStore() without BANK_DATA_ROOT env-isolation. "
        "Fix the test fixture, then manually clean the leaked rows with:\n"
        f"   sqlite3 {db} \"DELETE FROM cards WHERE card_no IN (...)\"\n"
    )


@pytest.mark.parametrize("bank", PRODUCTION_BANKS)
def test_no_fixture_accounts_leaked_into_production_sqlite(bank: str) -> None:
    """For every production *.sqlite, accounts table must not contain fixture rows."""
    db = PRODUCTION_DATA_ROOT / f"{bank}.sqlite"
    leaks = _scan_table(
        db, "accounts", "account_no", FIXTURE_ACCOUNT_PATTERNS,
        name_field="nickname", name_patterns=FIXTURE_NAME_PATTERNS,
    )
    assert not leaks, (
        f"\n\n⚠️  TEST FIXTURE LEAK in production DB: {db}\n"
        f"   Found {len(leaks)} accounts rows matching fixture patterns:\n"
        + "\n".join(f"    - {row}" for row in leaks[:10])
        + "\n\n   See test_no_fixture_cards_leaked_into_production_sqlite "
        "for root cause guidance.\n"
    )
