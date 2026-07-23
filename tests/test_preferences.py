"""Tests for /users/me/preferences (Phase 6).

驗:
- GET 沒設過 → 回 default { fx_display_mode: 'auto' }
- PUT 改 fx_display_mode → GET 拿到新值
- Partial update: PUT {fx_display_mode: ...} 不會清空其他欄 (目前沒其他欄, 但 contract)
- 非法 enum value → 422
- 401 (沒 token) 擋下
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path) -> Iterator[TestClient]:
    """乾淨 DB + JWT secret 的 TestClient (帶完整 router stack)."""
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET", "test-secret-1234567890" * 4)
    monkeypatch.delenv("SERVER_API_KEY", raising=False)

    # 重 import app 確保 BANK_DATA_ROOT 生效
    import importlib

    import backend.server.app as app_mod
    importlib.reload(app_mod)
    with TestClient(app_mod.app) as c:
        yield c


def _register_and_login(client: TestClient) -> str:
    """快速 register → 直接拿 register response 的 token (省一次 login)."""
    r = client.post(
        "/auth/register",
        json={"email": "u@test.com", "password": "real-password-123"},
    )
    assert r.status_code == 201, r.text
    return r.json()["token"]


def test_get_preferences_returns_default_when_unset(client: TestClient) -> None:
    token = _register_and_login(client)
    r = client.get(
        "/users/me/preferences", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
    assert r.json() == {
        "fx_display_mode": "auto",
        "card_date_basis": "consume",
    }


def test_put_preferences_persists_and_get_reads_back(client: TestClient) -> None:
    token = _register_and_login(client)
    h = {"Authorization": f"Bearer {token}"}

    r = client.put(
        "/users/me/preferences",
        headers=h,
        json={"fx_display_mode": "always_twd"},
    )
    assert r.status_code == 200
    assert r.json()["fx_display_mode"] == "always_twd"

    # GET 確認 persisted
    r = client.get("/users/me/preferences", headers=h)
    assert r.json()["fx_display_mode"] == "always_twd"


def test_put_preferences_partial_update_preserves_unknown_fields(
    client: TestClient,
) -> None:
    """未來加新欄時, partial update 不該清掉舊欄."""
    token = _register_and_login(client)
    h = {"Authorization": f"Bearer {token}"}

    # 先 set 一個值
    client.put(
        "/users/me/preferences", headers=h, json={"fx_display_mode": "always_original"}
    )

    # 再傳「不含 fx_display_mode」的 partial body (e.g. 未來其他欄)
    # → 既有值應保留
    r = client.put("/users/me/preferences", headers=h, json={})
    assert r.status_code == 200
    assert r.json()["fx_display_mode"] == "always_original"


def test_put_preferences_invalid_enum_rejected(client: TestClient) -> None:
    token = _register_and_login(client)
    r = client.put(
        "/users/me/preferences",
        headers={"Authorization": f"Bearer {token}"},
        json={"fx_display_mode": "garbage_mode"},
    )
    # Pydantic Literal validator → 422
    assert r.status_code == 422


def test_preferences_requires_auth(client: TestClient) -> None:
    r = client.get("/users/me/preferences")
    assert r.status_code == 401

    r = client.put(
        "/users/me/preferences", json={"fx_display_mode": "auto"}
    )
    assert r.status_code == 401


def test_put_card_date_basis_persists_and_get_reads_back(client: TestClient) -> None:
    """信用卡日期認列方式 (consume / post) 能寫 + 讀, 不會干擾 fx_display_mode."""
    token = _register_and_login(client)
    h = {"Authorization": f"Bearer {token}"}

    # 切到 'post'
    r = client.put(
        "/users/me/preferences",
        headers=h,
        json={"card_date_basis": "post"},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["card_date_basis"] == "post"
    assert payload["fx_display_mode"] == "auto"  # 沒動到 fx 欄

    # GET 確認 persisted
    r = client.get("/users/me/preferences", headers=h)
    assert r.json()["card_date_basis"] == "post"


def test_put_card_date_basis_invalid_enum_rejected(client: TestClient) -> None:
    token = _register_and_login(client)
    r = client.put(
        "/users/me/preferences",
        headers={"Authorization": f"Bearer {token}"},
        json={"card_date_basis": "bogus_date"},
    )
    # Pydantic Literal validator → 422
    assert r.status_code == 422


def test_put_two_prefs_at_once_both_persist(client: TestClient) -> None:
    """同時設 fx_display_mode + card_date_basis, 兩個都該存."""
    token = _register_and_login(client)
    h = {"Authorization": f"Bearer {token}"}

    r = client.put(
        "/users/me/preferences",
        headers=h,
        json={"fx_display_mode": "always_twd", "card_date_basis": "post"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["fx_display_mode"] == "always_twd"
    assert body["card_date_basis"] == "post"

    # GET 確認
    r = client.get("/users/me/preferences", headers=h)
    body = r.json()
    assert body["fx_display_mode"] == "always_twd"
    assert body["card_date_basis"] == "post"
