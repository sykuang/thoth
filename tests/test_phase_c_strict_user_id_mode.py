"""Phase C-Suggestion (2026-06-17): BankStore 嚴格模式測試。

驗 `THOTH_REQUIRE_EXPLICIT_USER_ID=1` 啟用時:
- BankStore() 沒帶 user_id → 直接 raise ValueError (防 multi-tenant data leak)
- BankStore(bank, user_id=N) 顯式傳就照常 work
- 沒設 env 時 fallback user_id=1 保歷史單 user 語意 (CLI / test / script)
- Server bootstrap (backend/server/app.py) 自動設此 env, production 路徑被覆蓋
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def store_mod_isolated(tmp_path, monkeypatch):
    """每個 test 拿乾淨的 store module + 隔離 BANK_DATA_ROOT 避免污染 production."""
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    # delete 已存在 env, autouse fixture 防止 server bootstrap 殘留影響
    monkeypatch.delenv("THOTH_REQUIRE_EXPLICIT_USER_ID", raising=False)
    from backend.core import store as _store

    importlib.reload(_store)
    return _store


def test_strict_mode_raises_when_user_id_omitted(store_mod_isolated, monkeypatch):
    """嚴格模式啟用 + 不傳 user_id → raise ValueError."""
    monkeypatch.setenv("THOTH_REQUIRE_EXPLICIT_USER_ID", "1")
    with pytest.raises(ValueError, match="THOTH_REQUIRE_EXPLICIT_USER_ID"):
        store_mod_isolated.BankStore("cathay")


def test_strict_mode_accepts_explicit_user_id(store_mod_isolated, monkeypatch):
    """嚴格模式啟用 + 顯式傳 user_id → 正常 work."""
    monkeypatch.setenv("THOTH_REQUIRE_EXPLICIT_USER_ID", "1")
    s = store_mod_isolated.BankStore("cathay", user_id=42)
    assert s.user_id == 42


def test_fallback_to_user_id_1_without_strict_env(store_mod_isolated, monkeypatch):
    """沒設 env (CLI / test default 場景) → fallback user_id=1, 不 raise."""
    monkeypatch.delenv("THOTH_REQUIRE_EXPLICIT_USER_ID", raising=False)
    s = store_mod_isolated.BankStore("cathay")
    assert s.user_id == 1


def test_env_value_variations_all_trigger_strict(store_mod_isolated, monkeypatch):
    """env 值 = 1 / true / yes (大小寫不敏感) 都該觸發嚴格模式."""
    for val in ("1", "true", "yes", "True", "YES"):
        monkeypatch.setenv("THOTH_REQUIRE_EXPLICIT_USER_ID", val)
        with pytest.raises(ValueError):
            store_mod_isolated.BankStore("cathay")


def test_env_value_off_does_not_trigger_strict(store_mod_isolated, monkeypatch):
    """env 值 = 0 / false / 空 → 不觸發嚴格, fallback 1."""
    for val in ("0", "false", "no", ""):
        monkeypatch.setenv("THOTH_REQUIRE_EXPLICIT_USER_ID", val)
        s = store_mod_isolated.BankStore("cathay")
        assert s.user_id == 1


def test_server_bootstrap_sets_strict_mode(monkeypatch):
    """import server.app 後 env 必設 (production runtime 預設啟用嚴格模式).

    Note: 用 importlib.reload 確保 bootstrap 段 (setdefault) 真的執行——只 import
    沒 reload 的話, sys.modules 已 cache, module-level code 不會重跑。
    """
    import importlib
    # 先確保 env 不存在 (autouse fixture 也會), 再 reload 觸發 setdefault
    monkeypatch.delenv("THOTH_REQUIRE_EXPLICIT_USER_ID", raising=False)
    from backend.server import app as _app
    importlib.reload(_app)
    import os
    assert os.environ.get("THOTH_REQUIRE_EXPLICIT_USER_ID") == "1"
