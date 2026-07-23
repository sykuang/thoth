"""Phase C-Suggestion (2026-06-17): BankStore migration cache 測試。

驗 _migrate 只跑一次, 第二次 BankStore(同 db_path) 直接 skip:
- _MIGRATED_DBS 第一次 BankStore() 後該有 entry
- 同 db_path 再開 BankStore 不重跑 _migrate (用 spy 數呼叫次數)
- 不同 db_path 各自 migrate 一次 (cache key by path)
- _reset_migration_cache() 後重置, 下次再跑
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def store_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("THOTH_REQUIRE_EXPLICIT_USER_ID", raising=False)
    from backend.core import store as _store
    importlib.reload(_store)
    _store._reset_migration_cache()
    return _store


def test_migration_cache_skip_second_call(store_mod, monkeypatch):
    """同 db_path 第二次 BankStore() _migrate 不該被呼叫."""
    call_count = {"n": 0}
    original = store_mod.BankStore._migrate

    def spy(self):
        call_count["n"] += 1
        return original(self)

    monkeypatch.setattr(store_mod.BankStore, "_migrate", spy)

    # 第一次 → _migrate 必跑
    store_mod.BankStore("cathay", user_id=1)
    assert call_count["n"] == 1

    # 第二次同 bank → cache hit 不重跑
    store_mod.BankStore("cathay", user_id=1)
    assert call_count["n"] == 1, "同 db_path 第二次該 cache hit, 不重跑 _migrate"

    # 不同 bank → 不同 path, 重跑一次
    store_mod.BankStore("ubot", user_id=1)
    assert call_count["n"] == 2

    # ubot 第二次 → cache hit
    store_mod.BankStore("ubot", user_id=1)
    assert call_count["n"] == 2


def test_migration_cache_populated_after_open(store_mod):
    """BankStore() 開過後 _MIGRATED_DBS 必含對應 cache key."""
    assert len(store_mod._MIGRATED_DBS) == 0
    s = store_mod.BankStore("cathay", user_id=1)
    assert len(store_mod._MIGRATED_DBS) == 1
    expected_key = store_mod._migration_cache_key(s.db_path, "cathay")
    assert expected_key in store_mod._MIGRATED_DBS


def test_reset_clears_cache(store_mod, monkeypatch):
    """_reset_migration_cache() 後下次 BankStore() 必重跑 _migrate."""
    call_count = {"n": 0}
    original = store_mod.BankStore._migrate

    def spy(self):
        call_count["n"] += 1
        return original(self)

    monkeypatch.setattr(store_mod.BankStore, "_migrate", spy)

    store_mod.BankStore("cathay", user_id=1)
    assert call_count["n"] == 1

    store_mod._reset_migration_cache()
    assert len(store_mod._MIGRATED_DBS) == 0

    store_mod.BankStore("cathay", user_id=1)
    assert call_count["n"] == 2, "reset 後該重跑"


def test_different_data_roots_dont_share_cache(tmp_path, monkeypatch):
    """不同 BANK_DATA_ROOT (不同 user / 不同 test) 各自 migrate 不共享 cache."""
    from backend.core import store as _store
    importlib.reload(_store)
    _store._reset_migration_cache()

    call_count = {"n": 0}
    original = _store.BankStore._migrate

    def spy(self):
        call_count["n"] += 1
        return original(self)

    monkeypatch.setattr(_store.BankStore, "_migrate", spy)

    # Root A
    root_a = tmp_path / "user_a"
    root_a.mkdir()
    monkeypatch.setenv("BANK_DATA_ROOT", str(root_a))
    _store.BankStore("cathay", user_id=1)
    assert call_count["n"] == 1

    # Root B 不同 path
    root_b = tmp_path / "user_b"
    root_b.mkdir()
    monkeypatch.setenv("BANK_DATA_ROOT", str(root_b))
    _store.BankStore("cathay", user_id=1)
    assert call_count["n"] == 2, "不同 BANK_DATA_ROOT 各自 cache, 都要跑 _migrate"
