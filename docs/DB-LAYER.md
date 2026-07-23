# Thoth DB Layer

> **Single rule:** application code (routers, services, anything under `backend/server/routers/` or `backend/server/*.py` that isn't `db.py`) **never** imports `sqlite3` or `psycopg` directly. Everything goes through `backend.server.db`.

## Why this rule exists

On 2026-06-17 three production bugs hit cloud (DB_BACKEND=postgres) in one afternoon:

| # | Code | Bug | Severity |
|---|------|-----|----------|
| 1 | `transactions._has_column` used `row[1]` | works on SQLite 6-tuple, IndexError on PG 1-col row | loud (500) |
| 2 | `rules.recategorize` used raw `sqlite3.connect(_bank_db_path(bank))` | bypassed PG dispatcher → 11 banks all skipped → 200 OK + 0/0 updated | **silent** |
| 3 | `accounts.create / rename` used `except sqlite3.IntegrityError` | doesn't match `psycopg.errors.UniqueViolation` → 500 instead of 409 | loud |

All three share the same root cause: **routers reaching into `sqlite3` directly**, leaving room for cross-backend mismatches that SQLite tests can't catch.

The fix is a single facade and a lint rule that enforces it.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│ Application layer (allowed: db.* only)                       │
│                                                              │
│   backend/server/routers/transactions.py                     │
│   backend/server/routers/cards.py                            │
│   backend/server/routers/portfolio.py                        │
│   backend/server/routers/rules.py                            │
│   backend/server/routers/accounts.py                         │
│   backend/server/sync_runner.py                              │
│   backend/server/creds_store.py                              │
│   backend/server/rules_repo.py                               │
│   backend/server/users.py                                    │
│   ...                                                        │
│                                                              │
│           ↓ from backend.server import db                    │
│                                                              │
├──────────────────────────────────────────────────────────────┤
│ DB facade (only db.py imports sqlite3 + psycopg)             │
│                                                              │
│   backend/server/db.py  exports:                             │
│     get_conn()          — server-side state context manager  │
│     open_bank_conn(b)   — per-bank data, dispatches to PG    │
│     IntegrityError      — portable, matches both backends    │
│     OperationalError    — portable (PG re-raised via bank_pg)│
│     Connection / Row    — type aliases (Any, duck-typed)     │
│     q(sql), now_iso()   — placeholder + timestamp helpers    │
│     DB_BACKEND          — "sqlite" or "postgres"             │
│                                                              │
├──────────────────────────────────────────────────────────────┤
│ Driver layer (the 4 files allowed to touch sqlite3/psycopg)  │
│                                                              │
│   backend/server/db.py        — server state implementation  │
│   backend/core/store.py       — BankStore (cli + crawler)    │
│   backend/core/bank_data.py   — bank-side dispatcher         │
│   backend/core/bank_pg.py     — PG adapter (Connection wrap) │
└──────────────────────────────────────────────────────────────┘
```

## How to use the facade

### Open a connection

```python
# Server-side state (users, bank_accounts, rules, ...)
from backend.server import db

with db.get_conn() as conn:
    cur = conn.execute("SELECT id FROM users WHERE email=?", (email,))
    row = cur.fetchone()
```

```python
# Per-bank data (transactions, cards, accounts, ...)
from backend.server import db

con = db.open_bank_conn(bank)
if con is None:
    return  # bank has no data yet
try:
    cur = con.execute("SELECT * FROM cards")
    rows = cur.fetchall()
finally:
    con.close()
```

### Handle exceptions

```python
from backend.server import db

# UNIQUE / CHECK violation → 409
try:
    repo.create(...)
except db.IntegrityError:
    raise HTTPException(409, "duplicate")

# Table/column missing (e.g. bank has no data yet) → degrade gracefully
try:
    rows = con.execute("SELECT amount FROM twd_transactions").fetchall()
except db.OperationalError:
    rows = []
```

### Access row fields

**Always use dict-like access** — `row["col"]`, not `row[0]` or `row[1]`. SQLite tuples and the PG adapter both support string keys, but only SQLite tuples support consistent positional access (the PG adapter's `Row` reflects the actual `SELECT` column count and breaks if you assume a positional shape from another query, e.g. `PRAGMA table_info`).

```python
# ✅ Good
for r in con.execute("SELECT id, description, amount FROM twd_transactions"):
    print(r["description"], r["amount"])

# ❌ Bad — works on SQLite, may IndexError on PG when column count differs
print(r[1], r[2])
```

## Lint rule

`tools/check_db_imports.py` walks `backend/` and fails on any `import sqlite3` / `import psycopg` outside the 4-file whitelist.

Runs as part of pytest via `tests/test_db_layer_encapsulation.py`. If you see this test fail, you added a forbidden import — switch to `from backend.server import db`.

To add a new file to the whitelist (rare — you'd need a new driver-layer module), update `WHITELIST` in `tools/check_db_imports.py` with code review.

## bank_pg.py compatibility contract

The PG adapter (`backend/core/bank_pg.py`) provides these SQLite-compatible behaviours so most SQL written for SQLite "just works":

| SQLite input | PG behaviour |
|---|---|
| `?` placeholder | rewritten to `%s` via `q()` |
| `PRAGMA table_info(t)` | rewritten to `information_schema.columns` (returns 1-col rows aliased `name`) |
| `SELECT name FROM sqlite_master WHERE type='table'` | rewritten to `information_schema.tables` |
| `SELECT 1 FROM sqlite_master WHERE type='table' AND name=?` | rewritten to `information_schema.tables` |
| psycopg `UndefinedTable` raised | re-raised as `sqlite3.OperationalError` (so `except db.OperationalError` works) |
| psycopg `UndefinedColumn` raised | re-raised as `sqlite3.OperationalError` |

**Not yet rewritten** (will syntax-error on PG — add to `bank_pg.py` if you need them):
- `INSERT OR IGNORE` / `INSERT OR REPLACE`
- `AUTOINCREMENT` (use `BIGSERIAL` template via `_PK_TYPE` instead)
- `julianday()` / `datetime()` (SQLite date functions)
- `UNIQUE` constraint violation re-raise (no bank-side write path needs this today)

## Hygiene checklist

When writing a new endpoint or DB-touching function:

- [ ] No `import sqlite3` or `import psycopg` in this file (linter catches it anyway)
- [ ] Bank-side query? Use `db.open_bank_conn(bank)`, check `is None`
- [ ] Server-side query? Use `db.get_conn()` context manager
- [ ] Catching UNIQUE violation? Use `db.IntegrityError`
- [ ] Catching missing table/column (schema drift / new bank)? Use `db.OperationalError`
- [ ] Reading row fields? Use `row["col"]`, never `row[0]`/`row[1]`
- [ ] Writing a PG-only / SQLite-only feature? Update `bank_pg.py` rewrites first

## Related wiki

- `~/wiki/concepts/thoth-dual-backend-audit-2026-06-17.md` — full audit + 3 bug post-mortems
- `~/wiki/concepts/sqlite-pragma-row-positional-access-pg-adapter-trap.md`
- `~/wiki/concepts/raw-sqlite-connect-bypasses-pg-adapter-silent-noop.md`
- `~/wiki/concepts/sqlite-integrityerror-catch-misses-pg-uniqueviolation.md`
- `~/wiki/concepts/dual-backend-storage-sqlite-postgres-disjoint.md`
