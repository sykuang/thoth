import sqlite3
from pathlib import Path

import pytest

from migrations.clear_fake_card_post_dates import _sqlite_connection, clear_known_fakes


BEFORE = "2026-08-15"


def _db():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE card_billed_txns (
            id INTEGER PRIMARY KEY, consume_date TEXT, post_date TEXT, first_seen TEXT
        );
        CREATE TABLE card_pending_txns (
            id INTEGER PRIMARY KEY, scope TEXT, consume_date TEXT, post_date TEXT,
            refreshed_at TEXT
        );
        INSERT INTO card_billed_txns VALUES
            (1, '2026-06-01', '2026-06-01', '2026-08-01'),
            (2, '2026-06-01', '2026-06-02', '2026-08-01'),
            (3, '2026-06-01', '2026-06-01', '2026-09-01');
        INSERT INTO card_pending_txns VALUES
            (1, 'unbilled', '2026-06-02', '2026-06-02', '2026-08-01'),
            (2, 'current', '2026-06-03', '2026-06-03', '2026-08-01'),
            (3, 'realtime', '2026-06-04', '2026-06-04', '2026-08-01'),
            (4, 'unbilled', '2026-06-05', '2026-06-05', '2026-09-01'),
            (5, 'realtime', '2026-06-06', '2026-06-07', '2026-08-01');
    """)
    return conn


def test_clear_known_fake_post_dates_is_scoped_and_dry_run_safe() -> None:
    conn = _db()
    assert clear_known_fakes(conn, "esun", execute=False, before=BEFORE) == {
        "card_billed_txns:all": 1,
        "card_pending_txns:all": 3,
    }
    assert conn.execute(
        "SELECT COUNT(*) FROM card_billed_txns WHERE post_date IS NOT NULL"
    ).fetchone()[0] == 3

    clear_known_fakes(conn, "esun", execute=True, before=BEFORE)
    assert clear_known_fakes(conn, "esun", execute=False, before=BEFORE) == {
        "card_billed_txns:all": 0,
        "card_pending_txns:all": 0,
    }
    assert conn.execute(
        "SELECT COUNT(*) FROM card_billed_txns WHERE post_date IS NOT NULL"
    ).fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM card_pending_txns WHERE post_date IS NOT NULL"
    ).fetchone()[0] == 2

    untouched = _db()
    assert clear_known_fakes(untouched, "ctbc", execute=True, before=BEFORE) == {}
    assert untouched.execute(
        "SELECT COUNT(*) FROM card_billed_txns WHERE post_date IS NOT NULL"
    ).fetchone()[0] == 3


def test_sqlite_connection_refuses_to_create_missing_database(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _sqlite_connection(tmp_path, "esun")
    assert list(tmp_path.iterdir()) == []


def test_fubon_cleanup_preserves_independent_real_post_date() -> None:
    conn = _db()
    assert clear_known_fakes(conn, "fubon", execute=True, before=BEFORE) == {
        "card_pending_txns:realtime": 1,
    }
    rows = conn.execute(
        "SELECT consume_date, post_date FROM card_pending_txns "
        "WHERE scope='realtime' ORDER BY id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("2026-06-04", None),
        ("2026-06-06", "2026-06-07"),
    ]
