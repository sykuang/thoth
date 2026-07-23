"""Phase 2 / L4 — BankCreds.load() strict layered behavior validation.

Phase 2 / L4 — `BankCreds.load()` 嚴格分層行為驗證 (plan T2.6, L4 strict)。

L4 嚴格模式（(2026-06-12) 拍板）— load() 不再 fall through：

  - BANK_CRAWLER_USER_ID 設了 → **server-mode**：只走 DB，缺就直接 raise；
    **絕不** fall through 到 .env（避免拿 maintainer 本人 .env cred 跑別 user 的爬蟲）。
  - BANK_CRAWLER_USER_ID 沒設 → **CLI/MCP-mode**：走 env (含 .env)；缺就 raise。

Cases:
  - test_load_uses_db_when_user_id_set        — server-mode 正常路徑
  - test_load_raises_when_server_mode_db_miss — server-mode 嚴格：DB 缺 → raise，不 fall through
  - test_load_uses_env_when_no_user_id        — CLI-mode 正常路徑
  - test_load_raises_when_neither_db_nor_env  — CLI-mode 沒 env → raise
"""
from __future__ import annotations

import importlib

import pytest
from cryptography.fernet import Fernet


@pytest.fixture
def fresh_env(tmp_path, monkeypatch):
    """clear 所有相關 env，並把 server.sqlite + .env load flag 都隔離乾淨。

    - BANK_DATA_ROOT → tmp_path（server.sqlite 在 tmp）
    - SERVER_FERNET_KEY → 新 key
    - 清掉所有 BANK_CRAWLER_USER_ID 與 SINOPAC_* 環境變數
    - reset backend.core.creds._ENV_LOADED 阻止它讀真 .env
    """
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("SERVER_FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("BANK_CRAWLER_USER_ID", raising=False)
    for k in ("SINOPAC_NATIONAL_ID", "SINOPAC_USER_CODE", "SINOPAC_PASSWORD"):
        monkeypatch.delenv(k, raising=False)

    import backend.server.db as db_mod
    import backend.server.creds_store as cs_mod
    import backend.core.creds as creds_mod
    importlib.reload(db_mod)
    importlib.reload(cs_mod)
    monkeypatch.setattr(creds_mod, "_ENV_LOADED", True, raising=True)
    return tmp_path


def test_load_uses_db_when_user_id_set(fresh_env, monkeypatch):
    """有 user_id env 且 DB 三欄齊 → 走 DB 路徑、回 SinopacCreds 物件。"""
    from backend.core.creds import SinopacCreds
    from backend.server.creds_store import LocalFernetBackend

    store = LocalFernetBackend()
    store.put(user_id=1, bank="sinopac", field="national_id", plain="B123456789")
    store.put(user_id=1, bank="sinopac", field="user_code",   plain="db-user-code")
    store.put(user_id=1, bank="sinopac", field="password",    plain="db-password")

    monkeypatch.setenv("BANK_CRAWLER_USER_ID", "1")

    creds = SinopacCreds.load()

    assert isinstance(creds, SinopacCreds)
    assert creds.national_id == "B123456789"
    assert creds.user_code == "db-user-code"
    assert creds.password == "db-password"


def test_load_raises_when_server_mode_db_miss(fresh_env, monkeypatch):
    """user_id 設了但 DB 沒這 user → **嚴格 raise**，不再 fall through 到 env。

    (2026-06-12) 拍板：server-mode (BANK_CRAWLER_USER_ID 設了) 一定只走 DB，
    DB 缺就 raise CredError，逼使用者從 Settings UI 補齊；不會拿 .env 本人 cred
    跑別 user 的爬蟲。
    """
    from backend.core.creds import CredError, SinopacCreds

    # DB 是空的（fresh_env tmp_path 新 sqlite）
    monkeypatch.setenv("BANK_CRAWLER_USER_ID", "999")  # 不存在的 user
    # 故意設 env cred，證明嚴格模式**不**會 fall through 用它
    monkeypatch.setenv("SINOPAC_NATIONAL_ID", "env-id-should-not-be-used")
    monkeypatch.setenv("SINOPAC_USER_CODE",   "env-code-should-not-be-used")
    monkeypatch.setenv("SINOPAC_PASSWORD",    "env-pw-should-not-be-used")

    with pytest.raises(CredError) as excinfo:
        SinopacCreds.load()

    msg = str(excinfo.value)
    assert "db:" in msg              # 錯誤訊息來自 from_db()
    assert "user_id=999" in msg
    assert "sinopac" in msg


def test_load_uses_env_when_no_user_id(fresh_env, monkeypatch):
    """BANK_CRAWLER_USER_ID 沒設 → 直接走 env 路徑。"""
    from backend.core.creds import SinopacCreds

    # user_id 沒設（fresh_env delenv 過）
    monkeypatch.setenv("SINOPAC_NATIONAL_ID", "env-id-only")
    monkeypatch.setenv("SINOPAC_USER_CODE",   "env-code-only")
    monkeypatch.setenv("SINOPAC_PASSWORD",    "env-pw-only")

    creds = SinopacCreds.load()

    assert isinstance(creds, SinopacCreds)
    assert creds.national_id == "env-id-only"
    assert creds.user_code == "env-code-only"
    assert creds.password == "env-pw-only"


def test_load_raises_when_neither_db_nor_env(fresh_env):
    """都沒設 → CredError raise（env 缺欄位）。"""
    from backend.core.creds import CredError, SinopacCreds

    # fresh_env 已 clear user_id + SINOPAC_*
    with pytest.raises(CredError) as excinfo:
        SinopacCreds.load()

    msg = str(excinfo.value)
    assert "SINOPAC" in msg
    # 應提示缺哪幾欄
    assert any(k in msg for k in ("NATIONAL_ID", "USER_CODE", "PASSWORD"))
