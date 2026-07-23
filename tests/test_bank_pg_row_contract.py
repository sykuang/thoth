"""PG Row class 契約 test —— 鎖住 sqlite3.Row 對齊行為.

PG mode 用 backend.core.bank_pg.Row 替代 sqlite3.Row 給 caller. 任何 caller
都該 backend-agnostic, 所以 PG Row 必須跟 sqlite3.Row 在這 4 個契約上一致:

  1. row[int]   → positional value (column 0, 1, ...)
  2. row[str]   → value by column name
  3. iter(row)  → VALUES in column order (tuple unpack `a, b = row` 靠這個)
  4. row.keys() → column names

Bug 史 (2026-06-18, fixed in 0.2.9):
  Row class 漏寫 __iter__, dict.__iter__ 預設 yield keys, 害 list_popular_tags
  的 `for raw_tags, raw_date in cur.fetchall():` 解出 column name 字串
  ('tags_overwrite', 'consume_date') 而不是真 row value, _parse_tags_overwrite()
  silent 回 [], endpoint 永遠回 {"tags": []}.

這 file 用 import-only test 不需 PG server, 直接 instantiate Row 跑契約檢查.
"""
from __future__ import annotations

import sqlite3


from backend.core.bank_pg import Row


def _make_sqlite_row() -> sqlite3.Row:
    """跑 reference: 拿一個真實 sqlite3.Row 對照行為."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE t(a TEXT, b INTEGER)")
    con.execute("INSERT INTO t VALUES ('hi', 5)")
    return con.execute("SELECT a, b FROM t").fetchone()


def _make_pg_row() -> Row:
    return Row({"a": "hi", "b": 5}, ["a", "b"])


# ─── 契約 1: row[int] → positional value ───────────────────────────────────────


def test_pg_row_positional_index_matches_sqlite():
    sql_r = _make_sqlite_row()
    pg_r = _make_pg_row()
    assert pg_r[0] == sql_r[0] == "hi"
    assert pg_r[1] == sql_r[1] == 5


# ─── 契約 2: row[str] → value by name ──────────────────────────────────────────


def test_pg_row_string_index_matches_sqlite():
    sql_r = _make_sqlite_row()
    pg_r = _make_pg_row()
    assert pg_r["a"] == sql_r["a"] == "hi"
    assert pg_r["b"] == sql_r["b"] == 5


# ─── 契約 3: iter(row) → values (NOT keys!) ────────────────────────────────────


def test_pg_row_iter_yields_values_not_keys():
    """⚠️ 關鍵契約 — 沒這個 tuple unpack 會解出 column name 字串.

    sqlite3.Row 的 __iter__ yield values, dict 的 __iter__ 預設 yield keys.
    PG Row 繼承 dict 必須 override __iter__, 否則 silent bug.
    """
    pg_r = _make_pg_row()
    yielded = list(pg_r)
    assert yielded == ["hi", 5], (
        f"PG Row iter 應該 yield values [list of column values], "
        f"got {yielded!r} (若是 ['a', 'b'] 表示誤 yield 出 keys — 0.2.9 修過的 bug 回歸)"
    )


def test_pg_row_iter_matches_sqlite_row_iter():
    """跨 backend cross-check."""
    sql_r = _make_sqlite_row()
    pg_r = _make_pg_row()
    assert list(pg_r) == list(sql_r) == ["hi", 5]


def test_pg_row_tuple_unpack_yields_values():
    """list_popular_tags pattern: `for a, b in rows: ...`"""
    pg_r = _make_pg_row()
    a, b = pg_r
    assert a == "hi", f"tuple unpack 第一個應是 row[0] value 'hi', got {a!r}"
    assert b == 5


def test_pg_row_for_loop_over_fetchall_unpack():
    """整段 list_popular_tags 路徑的 real-world pattern."""
    rows = [
        Row({"tags_overwrite": '["foo"]', "consume_date": "2026-06-18"},
            ["tags_overwrite", "consume_date"]),
        Row({"tags_overwrite": '["bar"]', "consume_date": "2026-06-17"},
            ["tags_overwrite", "consume_date"]),
    ]
    parsed: list[tuple[str, str]] = []
    for raw_tags, raw_date in rows:
        parsed.append((raw_tags, raw_date))
    assert parsed == [
        ('["foo"]', "2026-06-18"),
        ('["bar"]', "2026-06-17"),
    ], f"unpack 該出 (tags_value, date_value), got {parsed!r}"


# ─── 契約 4: row.keys() → column names ────────────────────────────────────────


def test_pg_row_keys_returns_column_names():
    sql_r = _make_sqlite_row()
    pg_r = _make_pg_row()
    assert list(pg_r.keys()) == list(sql_r.keys()) == ["a", "b"]


# ─── Bonus: column 順序保證 ───────────────────────────────────────────────────


def test_pg_row_iter_respects_column_order_not_dict_insertion():
    """如果 init dict 的順序跟 order list 不同, iter 該照 order list 走.

    這 case 在 _convert() 不會自然發生 (dict(zip(cols, tup)) 順序一致),
    但鎖住 Row 的 self._order 才是 source of truth, dict 順序只是巧合.
    """
    # 故意把 dict 順序倒過來
    r = Row({"b": 5, "a": "hi"}, order=["a", "b"])
    assert list(r) == ["hi", 5], (
        f"iter 該照 order=[a,b] 不該照 dict insert order [b,a], got {list(r)!r}"
    )
    assert r[0] == "hi"
    assert r[1] == 5
