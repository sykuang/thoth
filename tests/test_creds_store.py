"""Phase 0 — DB-backed encrypted credentials store (LocalFernetBackend).

TDD: 此檔三個 case 對應 plan T0.2，先寫測試後實作。

Cases:
  1. test_encrypt_decrypt_roundtrip — put → get 回原文
  2. test_user_isolation — user_id=1 put 完，user_id=2 同 bank/field 應拿 None
  3. test_missing_fernet_key_raises — 缺 SERVER_FERNET_KEY → LocalFernetBackend() raise RuntimeError 含 'SERVER_FERNET_KEY'
"""
from __future__ import annotations

import importlib

import pytest
from cryptography.fernet import Fernet


@pytest.fixture
def isolated_server_db(tmp_path, monkeypatch):
    """每個 test 一個獨立 server.sqlite + 重新 import server.db 模組。

    流程：
      - 設 BANK_DATA_ROOT 指向 tmp_path
      - 設 SERVER_FERNET_KEY 為新 key
      - reload backend.server.db 與 backend.server.creds_store 確保新 path 生效
    """
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("SERVER_FERNET_KEY", Fernet.generate_key().decode())

    # 確保乾淨 import（萬一前面 test 已 import 過，path 仍會用 env 動態解析則無妨）
    import backend.server.db as db_mod
    import backend.server.creds_store as cs_mod
    importlib.reload(db_mod)
    importlib.reload(cs_mod)
    return tmp_path


def test_encrypt_decrypt_roundtrip(isolated_server_db):
    """put 一個明文 → get 同 user/bank/field 應拿回原文。"""
    from backend.server.creds_store import LocalFernetBackend

    backend = LocalFernetBackend()
    backend.put(user_id=1, bank="sinopac", field="national_id", plain="B123456789")

    got = backend.get(user_id=1, bank="sinopac", field="national_id")
    assert got == "B123456789"


def test_user_isolation(isolated_server_db):
    """user_id=1 put 後，user_id=2 同 bank/field 不該拿到值（None）。"""
    from backend.server.creds_store import LocalFernetBackend

    backend = LocalFernetBackend()
    backend.put(user_id=1, bank="sinopac", field="password", plain="secret-1")

    # user_id=2 沒寫過任何東西
    assert backend.get(user_id=2, bank="sinopac", field="password") is None
    # 另外驗證 user_id=1 自己仍拿得到
    assert backend.get(user_id=1, bank="sinopac", field="password") == "secret-1"


def test_missing_fernet_key_raises(tmp_path, monkeypatch):
    """SERVER_FERNET_KEY 未設 → LocalFernetBackend() 必須 raise RuntimeError 且訊息含 'SERVER_FERNET_KEY'。"""
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("SERVER_FERNET_KEY", raising=False)

    # 重新 import 避免拿到已 cache 的 Fernet
    import backend.server.creds_store as cs_mod
    importlib.reload(cs_mod)

    with pytest.raises(RuntimeError, match="SERVER_FERNET_KEY"):
        cs_mod.LocalFernetBackend()
