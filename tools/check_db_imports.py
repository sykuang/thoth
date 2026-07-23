"""Enforce DB layer encapsulation: only allowed files may `import sqlite3` / `psycopg`.

Run via:
  python tools/check_db_imports.py
or in CI / pytest pre-flight.

Exit code 0 = clean, 1 = violation found (with file:line listing).

Whitelist (the 4 "DB layer" files):
  - backend/server/db.py        — server-side state facade
  - backend/core/store.py       — BankStore (CLI + crawler + server share)
  - backend/core/bank_data.py   — bank-side dispatcher
  - backend/core/bank_pg.py     — PostgreSQL adapter

Everywhere else under backend/ should import from `backend.server.db`:
  from backend.server import db
  con = db.open_bank_conn(bank)
  except db.IntegrityError: ...
  except db.OperationalError: ...

Why: 2026-06-17 three production bugs all came from routers reaching into
sqlite3 directly. See ~/wiki/concepts/thoth-dual-backend-audit-2026-06-17.md
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Files allowed to touch sqlite3 / psycopg directly. Anything else is a violation.
WHITELIST = {
    "backend/server/db.py",
    "backend/core/store.py",
    "backend/core/bank_data.py",
    "backend/core/bank_pg.py",
}

# Modules under backend/ are subject to the rule. Tests and cli scripts are
# checked too — they should also go through the facade.
SCAN_DIRS = ["backend"]

FORBIDDEN_MODULES = {"sqlite3", "psycopg", "psycopg2", "psycopg_pool"}


def find_violations(path: Path) -> list[tuple[int, str]]:
    """Return [(lineno, import_string), ...] for forbidden imports in this file."""
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except SyntaxError:
        return []

    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in FORBIDDEN_MODULES:
                    violations.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".")[0]
                if root in FORBIDDEN_MODULES:
                    names = ", ".join(a.name for a in node.names)
                    violations.append((node.lineno, f"from {node.module} import {names}"))
    return violations


def main() -> int:
    total_violations = 0
    for scan_dir in SCAN_DIRS:
        base = REPO_ROOT / scan_dir
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in WHITELIST:
                continue
            for lineno, stmt in find_violations(path):
                print(f"{rel}:{lineno}: {stmt}")
                total_violations += 1

    if total_violations:
        print()
        print(f"FAIL: {total_violations} forbidden import(s) found.")
        print()
        print("DB layer rule: only these 4 files may import sqlite3/psycopg directly:")
        for w in sorted(WHITELIST):
            print(f"  - {w}")
        print()
        print("Everywhere else under backend/ should use:")
        print("  from backend.server import db")
        print("  con = db.open_bank_conn(bank)")
        print("  except db.IntegrityError: ...")
        print("  except db.OperationalError: ...")
        print()
        print("See ~/wiki/concepts/thoth-dual-backend-audit-2026-06-17.md")
        return 1
    print("OK: no forbidden sqlite3/psycopg imports outside DB layer whitelist.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
