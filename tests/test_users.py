"""Phase 1 — Users CRUD (create / get_by_email / get_by_id)。

Schema 已在 Phase 0 `backend/server/db.py` 建好 users 表。
這層只負責 bcrypt 雜湊 + INSERT/SELECT + 唯一鍵 catch。
"""
from __future__ import annotations

import importlib

import pytest
from cryptography.fernet import Fernet


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """每個 test 一個獨立 server.sqlite + JWT_SECRET 設好。"""
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET", "test-secret-32+bytes-padding-padding-padding!")
    monkeypatch.setenv("SERVER_FERNET_KEY", Fernet.generate_key().decode())

    import backend.server.db as db_mod
    importlib.reload(db_mod)
    # auth 與 users 都 import db，需 reload
    import backend.server.auth as auth_mod
    importlib.reload(auth_mod)
    import backend.server.users as users_mod
    importlib.reload(users_mod)
    return tmp_path


def test_create_user_returns_id(isolated_db):
    from backend.server.users import create_user
    uid = create_user(email="emperor@palace.test", password="hunter2")
    assert isinstance(uid, int)
    assert uid > 0


def test_email_unique_raises(isolated_db):
    from backend.server.users import UserExistsError, create_user
    create_user(email="dup@palace.test", password="pw1")
    with pytest.raises(UserExistsError):
        create_user(email="dup@palace.test", password="pw2")


def test_get_user_by_email(isolated_db):
    from backend.server.users import create_user, get_user_by_email
    uid = create_user(email="lookup@palace.test", password="pw")
    user = get_user_by_email("lookup@palace.test")
    assert user is not None
    assert user["id"] == uid
    assert user["email"] == "lookup@palace.test"
    assert user["password_hash"] != "pw"  # 必須是 bcrypt 雜湊
    assert user["created_at"]  # 不能空


def test_get_user_by_id_missing_returns_none(isolated_db):
    from backend.server.users import get_user_by_id
    assert get_user_by_id(99999) is None
