"""Phase 1 — JWT auth + bcrypt password hashing.

TDD: 4 cases — covers happy path, bad hash, bad token, missing JWT_SECRET.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _jwt_secret(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret-do-not-use-in-prod-32bytes-min!")


def test_hash_and_verify_password():
    from backend.server.auth import hash_password, verify_password
    h = hash_password("hunter2")
    assert h != "hunter2"
    assert verify_password("hunter2", h) is True
    assert verify_password("wrong", h) is False


def test_create_and_decode_token():
    from backend.server.auth import create_access_token, decode_access_token
    token = create_access_token(user_id=42, email="emperor@palace.test")
    claims = decode_access_token(token)
    assert claims["sub"] == "42"
    assert claims["email"] == "emperor@palace.test"


def test_decode_invalid_token_raises():
    from backend.server.auth import decode_access_token, AuthError
    with pytest.raises(AuthError):
        decode_access_token("not.a.token")


def test_missing_jwt_secret_raises(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    # 強制 reimport
    import importlib
    import backend.server.auth as m
    importlib.reload(m)
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        m.create_access_token(user_id=1, email="x@y.z")
