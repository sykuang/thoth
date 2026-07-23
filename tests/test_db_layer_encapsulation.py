"""Regression test for DB layer encapsulation rule.

Ensures only the whitelisted DB layer files import sqlite3/psycopg directly.
See tools/check_db_imports.py for the actual checker.

Add a new violation? This test will fail with the file:line list. Either
remove the import or — if you really are adding a new DB layer module —
update the WHITELIST in tools/check_db_imports.py with a code review.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_no_forbidden_sqlite3_psycopg_imports_outside_db_layer() -> None:
    """No `import sqlite3` / `import psycopg` outside the 4 DB layer files."""
    result = subprocess.run(
        [sys.executable, "tools/check_db_imports.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # show full output so failures are diagnosable in pytest -v
        raise AssertionError(
            "DB layer encapsulation violated:\n"
            + result.stdout
            + ("\n" + result.stderr if result.stderr else "")
        )
