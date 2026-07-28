"""一次性 migration 端點的安全守衛測試。

這支端點能改動全 DB 的 taxonomy 欄位，守衛壞掉的代價很高。
migration 跑完刪除端點時，本測試檔一併刪除。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.server.app import app

PATH = "/admin/recategorize-20260728"


@pytest.fixture()
def client():
    return TestClient(app)


def test_requires_login(client):
    assert client.post(PATH).status_code == 401


def test_rejects_when_token_env_unset(client, monkeypatch):
    """env 沒設 MIGRATION_TOKEN → 一律 403（預設關閉，不會忘了拆而長期敞著）。"""
    monkeypatch.delenv("MIGRATION_TOKEN", raising=False)
    from backend.server.routers.admin_migration import _require_migration_token
    with pytest.raises(Exception) as exc:
        _require_migration_token("whatever")
    assert "403" in str(exc.value) or "未啟用" in str(exc.value)


def test_rejects_wrong_token(monkeypatch):
    monkeypatch.setenv("MIGRATION_TOKEN", "correct-token")
    from backend.server.routers.admin_migration import _require_migration_token
    with pytest.raises(Exception):
        _require_migration_token("wrong-token")
    with pytest.raises(Exception):
        _require_migration_token(None)


def test_accepts_correct_token(monkeypatch):
    monkeypatch.setenv("MIGRATION_TOKEN", "correct-token")
    from backend.server.routers.admin_migration import _require_migration_token
    _require_migration_token("correct-token")  # 不 raise 即通過
