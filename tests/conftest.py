"""Phase 1 — pytest shared fixture: isolated FastAPI client.

Phase 1 — pytest 共用 fixture：isolated FastAPI client。

每個 test 一個 tmp_path → server.sqlite + 新 Fernet key + 新 JWT secret。
reload 三個 server 模組以確保 env 變動生效。
"""
from __future__ import annotations

import importlib

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _disable_scheduler_in_tests(monkeypatch):
    """L12 (2026-06-22): test 期間禁 APScheduler 啟動.

    test 用 TestClient(app) 會觸發 FastAPI startup event → scheduler.start()
    → BackgroundScheduler 開 daemon thread + 載 DB schedules. 對 isolated test
    (tmp_path 各自 sqlite) 沒意義且製造 noise + 可能 race.
    THOTH_DISABLE_SCHEDULER=1 在 app.py startup 短路.
    """
    monkeypatch.setenv("THOTH_DISABLE_SCHEDULER", "1")
    yield


@pytest.fixture(autouse=True)
def _unset_strict_user_id_env(monkeypatch):
    """Phase C-Suggestion (2026-06-17): autouse — 防 server bootstrap import 後殘留
    `THOTH_REQUIRE_EXPLICIT_USER_ID=1` 污染後續 test (test_cards_routes 等大量 test
    用 `BankStore("ubot")` 沒帶 user_id, fallback user_id=1 是 test 預期語意)。
    要 production strict 行為的 test (test_phase_c_strict_user_id_mode.py) 自己
    monkeypatch.setenv 顯式啟用。
    yield-style: 前 + 後都 delenv 雙保險, 因為 test body 可能 import server.app
    觸發 setdefault 寫值, 不 teardown 會污染下個 test。
    """
    monkeypatch.delenv("THOTH_REQUIRE_EXPLICIT_USER_ID", raising=False)
    yield
    # 兜底 teardown: monkeypatch 已會 restore, 但有些 test 用 os.environ 直設
    # 跳過 monkeypatch 寫進去, 真的 delenv 一次穩。
    import os as _os
    _os.environ.pop("THOTH_REQUIRE_EXPLICIT_USER_ID", None)


@pytest.fixture(autouse=True)
def _reset_bankstore_migration_cache():
    """Phase C-Suggestion (2026-06-17): autouse — 每個 test 前清 BankStore migration cache,
    防 process-level _MIGRATED_DBS state leak (test 用 tmp_path 不同但 cache 殘留
    其他 test path 不影響, 不過 reload + same path scenarios 還是清掉最安全)。

    Phase C (2026-06-18): 同步清 bank_pg._PHASE_C_PG_MIGRATED 防 PG schema
    migration cache 在 test 間污染 (修 prod 500「column user_id does not exist」
    時加的 _ensure_phase_c_user_id_pg 同樣是 module-level set)。
    """
    try:
        from backend.core import store as _store
        _store._reset_migration_cache()
    except ImportError:
        pass
    try:
        from backend.core import bank_pg as _bank_pg
        _bank_pg._reset_phase_c_pg_cache()
    except ImportError:
        pass
    yield
    try:
        from backend.core import store as _store
        _store._reset_migration_cache()
    except ImportError:
        pass
    try:
        from backend.core import bank_pg as _bank_pg
        _bank_pg._reset_phase_c_pg_cache()
    except ImportError:
        pass


@pytest.fixture
def client(tmp_path, monkeypatch):
    """完全 isolated server.sqlite + fresh JWT secret + fresh Fernet key per test。"""
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET", "test-secret-32+bytes-padding-padding-padding!")
    monkeypatch.setenv("SERVER_FERNET_KEY", Fernet.generate_key().decode())
    # W3: 測試環境關掉 register 的 constant-time delay 避免拖慢 pytest（每筆 register +1s 太誇張）
    monkeypatch.setenv("REGISTER_DELAY_SECONDS", "0")

    # reload server modules so they see new envs
    import backend.server.db as db_mod
    import backend.server.creds_store as cs_mod
    import backend.server.auth as auth_mod
    import backend.server.users as users_mod
    importlib.reload(db_mod)
    importlib.reload(cs_mod)
    importlib.reload(auth_mod)
    importlib.reload(users_mod)

    # routers 也要 reload（它們 import 上面那些）
    try:
        import backend.server.routers.auth as r_auth
        importlib.reload(r_auth)
    except ModuleNotFoundError:
        pass
    try:
        import backend.server.routers.credentials as r_creds
        importlib.reload(r_creds)
    except ModuleNotFoundError:
        pass
    try:
        import backend.server.routers.snaptrade as r_snaptrade
        importlib.reload(r_snaptrade)
    except ModuleNotFoundError:
        pass
    try:
        import backend.server.routers.sync as r_sync
        importlib.reload(r_sync)
    except ModuleNotFoundError:
        pass
    try:
        import backend.server.routers.sync_ws as r_sync_ws
        importlib.reload(r_sync_ws)
    except ModuleNotFoundError:
        pass
    try:
        import backend.server.routers.rules as r_rules
        importlib.reload(r_rules)
    except ModuleNotFoundError:
        pass
    try:
        import backend.server.routers.accounts as r_accounts
        importlib.reload(r_accounts)
    except ModuleNotFoundError:
        pass
    try:
        import backend.server.routers.transactions as r_txns
        importlib.reload(r_txns)
    except ModuleNotFoundError:
        pass
    try:
        import backend.server.rules_repo as rules_repo_mod
        importlib.reload(rules_repo_mod)
    except ModuleNotFoundError:
        pass
    try:
        import backend.server.seed_rules as seed_rules_mod
        importlib.reload(seed_rules_mod)
    except ModuleNotFoundError:
        pass
    try:
        import backend.server.sync_runner as sync_runner_mod
        importlib.reload(sync_runner_mod)
    except ModuleNotFoundError:
        pass
    try:
        import backend.server.sync_jobs_repo as sync_jobs_repo_mod
        importlib.reload(sync_jobs_repo_mod)
    except ModuleNotFoundError:
        pass
    try:
        import backend.server.sync_batches_repo as sync_batches_repo_mod
        importlib.reload(sync_batches_repo_mod)
    except ModuleNotFoundError:
        pass

    import backend.server.app as app_mod
    importlib.reload(app_mod)

    # Phase C-Suggestion (2026-06-17): app reload 會 setdefault THOTH_REQUIRE_EXPLICIT_USER_ID=1
    # test 環境用 fallback user_id=1 (大量舊 test 期望 `BankStore("ubot")` 不傳 user_id),
    # reload 後立即清回去防污染.
    monkeypatch.delenv("THOTH_REQUIRE_EXPLICIT_USER_ID", raising=False)

    # W3: 重置 in-memory rate limiter singleton，避免跨 test 累積 register/login 失敗計數
    try:
        from backend.server.security import login_limiter
        login_limiter.reset()
    except ImportError:
        pass

    return TestClient(app_mod.app)
