"""Phase 5.1 — category_rules CRUD repo tests (per-user isolation).

Phase 5.1 — category_rules CRUD repo 測試（per-user 隔離）。

repo 介面（rules_repo.py 將實作）：
  - create_rule(user_id, name, pattern, category, priority=100, enabled=True) -> int
  - list_rules(user_id, enabled_only=False) -> list[dict]   # 已按 priority DESC 排序
  - get_rule(user_id, rule_id) -> dict | None
  - update_rule(user_id, rule_id, **fields) -> bool
  - delete_rule(user_id, rule_id) -> bool
"""
from __future__ import annotations

import importlib

import pytest
from cryptography.fernet import Fernet


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET", "test-secret-32+bytes-padding-padding-padding!")
    monkeypatch.setenv("SERVER_FERNET_KEY", Fernet.generate_key().decode())
    import backend.server.db as db_mod
    importlib.reload(db_mod)
    import backend.server.rules_repo as rr
    importlib.reload(rr)
    return rr


def test_create_and_list_rules(isolated):
    rr = isolated
    rid = rr.create_rule(user_id=1, name="transit", pattern=r"北捷",
                         category="交通", priority=100)
    assert isinstance(rid, int) and rid > 0
    rules = rr.list_rules(user_id=1)
    assert len(rules) == 1
    r = rules[0]
    assert r["name"] == "transit"
    assert r["pattern"] == "北捷"
    assert r["category"] == "交通"
    assert r["priority"] == 100
    assert r["enabled"] == 1
    assert r["created_at"]
    assert r["updated_at"]


def test_list_rules_sorted_by_priority_desc(isolated):
    rr = isolated
    rr.create_rule(user_id=1, name="low", pattern=r"a", category="C", priority=10)
    rr.create_rule(user_id=1, name="high", pattern=r"b", category="C", priority=999)
    rr.create_rule(user_id=1, name="mid", pattern=r"c", category="C", priority=100)
    rules = rr.list_rules(user_id=1)
    names = [r["name"] for r in rules]
    assert names == ["high", "mid", "low"]


def test_list_rules_per_user_isolation(isolated):
    rr = isolated
    rr.create_rule(user_id=1, name="r1", pattern="a", category="C")
    rr.create_rule(user_id=2, name="r2", pattern="b", category="C")
    assert len(rr.list_rules(user_id=1)) == 1
    assert len(rr.list_rules(user_id=2)) == 1
    assert rr.list_rules(user_id=1)[0]["name"] == "r1"


def test_update_rule(isolated):
    rr = isolated
    rid = rr.create_rule(user_id=1, name="x", pattern="a", category="C", priority=50)
    ok = rr.update_rule(user_id=1, rule_id=rid, name="x2",
                        pattern="b", category="D", priority=999, enabled=False)
    assert ok is True
    rule = rr.get_rule(user_id=1, rule_id=rid)
    assert rule is not None
    assert rule["name"] == "x2"
    assert rule["pattern"] == "b"
    assert rule["category"] == "D"
    assert rule["priority"] == 999
    assert rule["enabled"] == 0


def test_update_rule_wrong_user_no_effect(isolated):
    rr = isolated
    rid = rr.create_rule(user_id=1, name="x", pattern="a", category="C")
    ok = rr.update_rule(user_id=2, rule_id=rid, name="hacked")
    assert ok is False
    # 原 rule 不該被改
    rule = rr.get_rule(user_id=1, rule_id=rid)
    assert rule["name"] == "x"


def test_delete_rule(isolated):
    rr = isolated
    rid = rr.create_rule(user_id=1, name="x", pattern="a", category="C")
    ok = rr.delete_rule(user_id=1, rule_id=rid)
    assert ok is True
    assert rr.get_rule(user_id=1, rule_id=rid) is None


def test_delete_rule_wrong_user_no_effect(isolated):
    rr = isolated
    rid = rr.create_rule(user_id=1, name="x", pattern="a", category="C")
    ok = rr.delete_rule(user_id=2, rule_id=rid)
    assert ok is False
    assert rr.get_rule(user_id=1, rule_id=rid) is not None


def test_list_rules_enabled_only(isolated):
    rr = isolated
    rr.create_rule(user_id=1, name="on", pattern="a", category="C")
    rid = rr.create_rule(user_id=1, name="off", pattern="b", category="C")
    rr.update_rule(user_id=1, rule_id=rid, enabled=False)
    all_rules = rr.list_rules(user_id=1)
    on_only = rr.list_rules(user_id=1, enabled_only=True)
    assert len(all_rules) == 2
    assert len(on_only) == 1
    assert on_only[0]["name"] == "on"


def test_get_rule_returns_none_for_missing(isolated):
    rr = isolated
    assert rr.get_rule(user_id=1, rule_id=9999) is None


# ===========================================================================
# Phase 8.1 (2026-06-15): subcategory 子分類
# ===========================================================================

def test_create_rule_with_subcategory(isolated):
    rr = isolated
    rid = rr.create_rule(user_id=1, name="breakfast", pattern=r"早餐",
                         category="飲食", subcategory="早餐", priority=100)
    rule = rr.get_rule(user_id=1, rule_id=rid)
    assert rule is not None
    assert rule["category"] == "飲食"
    assert rule["subcategory"] == "早餐"


def test_create_rule_subcategory_default_none(isolated):
    """沒傳 subcategory → DB 存 NULL → 讀出 None。"""
    rr = isolated
    rid = rr.create_rule(user_id=1, name="x", pattern="a", category="C")
    rule = rr.get_rule(user_id=1, rule_id=rid)
    assert rule["subcategory"] is None


def test_update_rule_subcategory(isolated):
    rr = isolated
    rid = rr.create_rule(user_id=1, name="x", pattern="a", category="飲食",
                         subcategory="餐廳")
    ok = rr.update_rule(user_id=1, rule_id=rid, subcategory="火鍋")
    assert ok is True
    rule = rr.get_rule(user_id=1, rule_id=rid)
    assert rule["subcategory"] == "火鍋"


def test_update_rule_subcategory_to_null(isolated):
    """update_rule(subcategory=None) → 把子分類清空 (從子類降回主類)."""
    rr = isolated
    rid = rr.create_rule(user_id=1, name="x", pattern="a", category="飲食",
                         subcategory="餐廳")
    ok = rr.update_rule(user_id=1, rule_id=rid, subcategory=None)
    assert ok is True
    rule = rr.get_rule(user_id=1, rule_id=rid)
    assert rule["subcategory"] is None


def test_distinct_subcategories_all(isolated):
    rr = isolated
    rr.create_rule(user_id=1, name="r1", pattern="a", category="飲食", subcategory="餐廳")
    rr.create_rule(user_id=1, name="r2", pattern="b", category="飲食", subcategory="早餐")
    rr.create_rule(user_id=1, name="r3", pattern="c", category="交通", subcategory="加油")
    rr.create_rule(user_id=1, name="r4", pattern="d", category="購物")  # 無 sub
    subs = rr.distinct_subcategories(user_id=1)
    # SQLite ORDER BY 對中文用 codepoint 排序, 順序由 SQLite 決定; 只驗集合
    assert set(subs) == {"加油", "早餐", "餐廳"}
    assert len(subs) == 3  # 無 sub 的 r4 不出現


def test_distinct_subcategories_filtered_by_category(isolated):
    rr = isolated
    rr.create_rule(user_id=1, name="r1", pattern="a", category="飲食", subcategory="餐廳")
    rr.create_rule(user_id=1, name="r2", pattern="b", category="飲食", subcategory="早餐")
    rr.create_rule(user_id=1, name="r3", pattern="c", category="交通", subcategory="加油")
    subs = rr.distinct_subcategories(user_id=1, category="飲食")
    assert set(subs) == {"早餐", "餐廳"}
    subs2 = rr.distinct_subcategories(user_id=1, category="交通")
    assert subs2 == ["加油"]


def test_distinct_subcategories_per_user(isolated):
    rr = isolated
    rr.create_rule(user_id=1, name="r1", pattern="a", category="飲食", subcategory="餐廳")
    rr.create_rule(user_id=2, name="r2", pattern="b", category="飲食", subcategory="早餐")
    assert rr.distinct_subcategories(user_id=1) == ["餐廳"]
    assert rr.distinct_subcategories(user_id=2) == ["早餐"]


def test_distinct_subcategories_filters_empty_and_null(isolated):
    """空字串子分類也算「無」, 不出現在 distinct list."""
    rr = isolated
    rr.create_rule(user_id=1, name="r1", pattern="a", category="飲食", subcategory="")
    rr.create_rule(user_id=1, name="r2", pattern="b", category="飲食", subcategory=None)
    rr.create_rule(user_id=1, name="r3", pattern="c", category="飲食", subcategory="正常")
    assert rr.distinct_subcategories(user_id=1) == ["正常"]


# ============================================================
# Phase 8.3 (2026-06-15) — auto_excluded
# ============================================================

def test_create_rule_default_auto_excluded_false(isolated):
    """create_rule 不傳 auto_excluded → 預設 0."""
    rr = isolated
    rid = rr.create_rule(user_id=1, name="r1", pattern="a", category="飲食")
    rule = rr.get_rule(user_id=1, rule_id=rid)
    assert rule["auto_excluded"] == 0


def test_create_rule_with_auto_excluded_true(isolated):
    """create_rule auto_excluded=True → SQLite 存 1."""
    rr = isolated
    rid = rr.create_rule(user_id=1, name="r1", pattern="a",
                          category="還款", auto_excluded=True)
    rule = rr.get_rule(user_id=1, rule_id=rid)
    assert rule["auto_excluded"] == 1


def test_update_rule_toggles_auto_excluded(isolated):
    """update_rule auto_excluded 可正反翻."""
    rr = isolated
    rid = rr.create_rule(user_id=1, name="r1", pattern="a", category="飲食")
    assert rr.update_rule(user_id=1, rule_id=rid, auto_excluded=True) is True
    assert rr.get_rule(user_id=1, rule_id=rid)["auto_excluded"] == 1
    assert rr.update_rule(user_id=1, rule_id=rid, auto_excluded=False) is True
    assert rr.get_rule(user_id=1, rule_id=rid)["auto_excluded"] == 0


def test_list_rules_includes_auto_excluded(isolated):
    """list_rules 回傳 dict 包含 auto_excluded 欄."""
    rr = isolated
    rr.create_rule(user_id=1, name="r1", pattern="a", category="還款", auto_excluded=True)
    rr.create_rule(user_id=1, name="r2", pattern="b", category="飲食")
    rules = rr.list_rules(user_id=1)
    assert {r["name"]: r["auto_excluded"] for r in rules} == {"r1": 1, "r2": 0}
