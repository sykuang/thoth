"""Regression test for backend.server.db._split_statements.

History (2026-06-16): SQL 註解 (`-- ...`) 內含 `;` 字元時，舊版 `split(";")`
會把註解切成兩段，後半段被當 SQL 送進 PG 觸發 SyntaxError。修法是先剝註解
再 split — 這個 test 保證下次再爆同樣 case 不會回歸。
"""
from __future__ import annotations

from backend.server.db import _split_statements


def test_split_statements_handles_semicolon_inside_comment():
    """使用者 WIP 引爆的真實案例: 行內 -- 註解含 ; 不可被當 statement 邊界."""
    sql = """
-- Phase 12 (2026-06-16): per-user table
-- UNIQUE 確保: (a) 重複灌; (b) 手動加的不會撞 bank_auto
CREATE TABLE IF NOT EXISTS bill_payments (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_bp_user ON bill_payments(user_id);
"""
    stmts = _split_statements(sql)
    assert len(stmts) == 2, f"預期 2 個 stmt, 實際 {len(stmts)}: {stmts}"
    assert "CREATE TABLE" in stmts[0]
    assert "CREATE INDEX" in stmts[1]
    # 註解內的 (b) 不可洩漏到任何 stmt 內
    for s in stmts:
        assert "(b)" not in s
        assert "手動加的" not in s


def test_split_statements_handles_trailing_comment_with_semicolon():
    """行尾 -- 註解內有 ; 也不該切."""
    sql = """
CREATE TABLE c (id INTEGER);  -- 註解; 也有分號但不該切
CREATE TABLE d (id INTEGER);
"""
    stmts = _split_statements(sql)
    assert len(stmts) == 2, f"預期 2, 實際 {len(stmts)}: {stmts}"


def test_split_statements_skips_pure_comment_lines():
    """純註解行不該變成 stmt (老 behavior 保留)."""
    sql = """
-- 純註解
-- 又一條註解
CREATE TABLE e (id INTEGER);
"""
    stmts = _split_statements(sql)
    assert len(stmts) == 1
    assert "CREATE TABLE e" in stmts[0]


def test_split_statements_multi_statement_no_comments():
    """沒註解, 純多 stmt — 確保基本 split 仍 work."""
    sql = """
CREATE TABLE a (id INTEGER);
CREATE TABLE b (id INTEGER);
"""
    stmts = _split_statements(sql)
    assert len(stmts) == 2


def test_split_statements_strips_empty_segments():
    """連續 ;; 不該產生空 stmt."""
    sql = """
CREATE TABLE a (id INTEGER);;

CREATE TABLE b (id INTEGER);
"""
    stmts = _split_statements(sql)
    assert len(stmts) == 2
    for s in stmts:
        assert s.strip(), "empty stmt leaked"
