"""Plan B B7 — Global SQL audit linter.

Enforces the Plan B invariant:
  - **Allowed SQL sources** (the "infrastructure layer"):
      * backend/server/db_facade/**.py       (bank DB typed API)
      * backend/server/db.py                  (server DB connection / schema)
      * backend/server/users.py               (server DB Repo for users)
      * backend/server/creds_store.py         (server DB Repo for credentials)
      * backend/server/rules_repo.py          (server DB Repo for rules)
      * backend/server/sync_jobs_repo.py      (server DB Repo for sync_jobs)
      * backend/server/preferences_repo.py    (server DB Repo for preferences)
      * backend/core/bank_pg.py               (PG adapter)
      * backend/core/bank_data.py             (SQLite adapter)
      * backend/core/store.py                 (bank DB schema owner / sync writer)
  - **Forbidden everywhere else** in backend/:
      `.execute(...)` / `.executemany(...)` calls (the only reliable signal of
      raw SQL — SQL keywords in docstrings are common and benign).

If this test fails, the offending module has leaked SQL into router / business
logic layer. Move the SQL into the appropriate Repo or db_facade method.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"

# Files / directories allowed to contain SQL (infrastructure layer)
ALLOWED_PATHS = {
    "backend/server/db.py",
    "backend/server/users.py",
    "backend/server/creds_store.py",
    "backend/server/rules_repo.py",
    "backend/server/sync_jobs_repo.py",
    "backend/server/sync_batches_repo.py",  # 2026-06-23 (Plan A): batch summary push
    "backend/server/preferences_repo.py",
    "backend/server/refresh_tokens.py",
    "backend/server/auto_debit_settings_repo.py",
    "backend/server/payment_reminder_notifications.py",
    "backend/server/push/repo.py",  # L11 (2026-06-22): push token repo, server-level
    "backend/server/user_sync_pref_repo.py",  # L13 (2026-06-23): per-user auto-sync preference
    "backend/core/bank_pg.py",
    "backend/core/bank_data.py",
    "backend/core/store.py",
}
ALLOWED_DIRS = {
    "backend/server/db_facade",
}


def _is_allowed(path: Path) -> bool:
    """Is this path in the infrastructure layer (allowed to contain SQL)?"""
    rel = path.relative_to(REPO_ROOT).as_posix()
    if rel in ALLOWED_PATHS:
        return True
    return any(rel.startswith(d + "/") for d in ALLOWED_DIRS)


class ExecuteCallVisitor(ast.NodeVisitor):
    """AST walker that finds `.execute(...)` / `.executemany(...)` calls.

    This is the highest-signal way to detect raw SQL: it ignores SQL keywords in
    docstrings / comments / Chinese prose, focuses only on actual DB calls.
    """

    def __init__(self) -> None:
        self.violations: list[tuple[int, str]] = []

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in (
            "execute", "executemany", "executescript"
        ):
            self.violations.append((node.lineno, f"{func.attr}() call"))
        self.generic_visit(node)


def _audit_file(path: Path) -> list[str]:
    """Return list of violations (each = 'path:line: description')."""
    if _is_allowed(path):
        return []
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, OSError) as e:
        return [f"{path}: parse error: {e}"]
    visitor = ExecuteCallVisitor()
    visitor.visit(tree)
    rel = path.relative_to(REPO_ROOT).as_posix()
    return [f"{rel}:{lineno}: {desc}" for lineno, desc in visitor.violations]


def test_no_sql_execute_outside_infrastructure() -> None:
    """No `.execute(...)` calls outside the infrastructure layer (Plan B invariant).

    The infrastructure layer is whitelisted in ALLOWED_PATHS / ALLOWED_DIRS at
    the top of this file. Anything else (routers, business logic, sync runner,
    persist helpers) must go through db_facade or a Repo.
    """
    py_files = list(BACKEND_DIR.rglob("*.py"))
    assert py_files, "no python files found in backend/"

    all_violations: list[str] = []
    for path in sorted(py_files):
        all_violations.extend(_audit_file(path))

    if all_violations:
        msg = "\n".join(all_violations)
        pytest.fail(
            f"Plan B SQL leak detected ({len(all_violations)} .execute() call(s) "
            f"outside infrastructure layer):\n{msg}\n\n"
            "Move SQL into db_facade/, a Repo (e.g., users_repo.py), or use existing "
            "infrastructure (db.py / store.py / bank_pg.py / bank_data.py).\n"
            "If a new file legitimately belongs in the infrastructure layer, add it "
            "to ALLOWED_PATHS in tests/test_plan_b_sql_audit.py."
        )


def test_allowed_paths_exist() -> None:
    """Sanity check: every entry in ALLOWED_PATHS / ALLOWED_DIRS exists.

    Prevents the linter from silently weakening if a file gets renamed.
    """
    for rel in ALLOWED_PATHS:
        p = REPO_ROOT / rel
        assert p.exists(), f"ALLOWED_PATHS entry missing: {rel}"
    for rel in ALLOWED_DIRS:
        p = REPO_ROOT / rel
        assert p.is_dir(), f"ALLOWED_DIRS entry missing: {rel}"


def test_no_sqlite_psycopg_import_in_routers() -> None:
    """Routers must not import sqlite3 / psycopg directly (Plan B invariant)."""
    routers_dir = BACKEND_DIR / "server" / "routers"
    violations: list[str] = []
    for path in sorted(routers_dir.rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in ("sqlite3", "psycopg", "psycopg2"):
                        rel = path.relative_to(REPO_ROOT).as_posix()
                        violations.append(f"{rel}:{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module in ("sqlite3", "psycopg", "psycopg2"):
                    rel = path.relative_to(REPO_ROOT).as_posix()
                    violations.append(f"{rel}:{node.lineno}: from {node.module} import")
    if violations:
        pytest.fail(
            "Routers must not import sqlite3 / psycopg directly:\n"
            + "\n".join(violations)
        )
