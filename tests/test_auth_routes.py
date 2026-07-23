"""Phase 1 — /auth/register, /auth/login, /auth/me routes (end-to-end).

Phase 1 — /auth/register, /auth/login, /auth/me routes（端到端）。
"""
from __future__ import annotations


def test_register_creates_user_returns_token(client):
    r = client.post(
        "/auth/register",
        json={"email": "newuser@palace.example", "password": "SyntheticTestPassword02!"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body.get("token")
    assert body["user_id"] >= 1
    assert body["email"] == "newuser@palace.example"


def test_register_duplicate_email_409(client):
    client.post(
        "/auth/register",
        json={"email": "dup2@palace.example", "password": "pw-strong"},
    )
    r = client.post(
        "/auth/register",
        json={"email": "dup2@palace.example", "password": "pw-strong"},
    )
    assert r.status_code == 409, r.text


def test_register_weak_email_422(client):
    r = client.post(
        "/auth/register",
        json={"email": "not-an-email", "password": "pw-strong"},
    )
    # pydantic EmailStr 應該擋下 → 422
    assert r.status_code == 422, r.text


def test_login_correct_password_returns_token(client):
    client.post(
        "/auth/register",
        json={"email": "login@palace.example", "password": "SyntheticTestPassword02!"},
    )
    # OAuth2PasswordRequestForm 用 form data
    r = client.post(
        "/auth/login",
        data={"username": "login@palace.example", "password": "SyntheticTestPassword02!"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]


def test_login_wrong_password_401(client):
    client.post(
        "/auth/register",
        json={"email": "wrongpw@palace.example", "password": "SyntheticTestPassword02!"},
    )
    r = client.post(
        "/auth/login",
        data={"username": "wrongpw@palace.example", "password": "WRONG"},
    )
    assert r.status_code == 401, r.text


def test_me_with_valid_token_returns_user(client):
    reg = client.post(
        "/auth/register",
        json={"email": "me@palace.example", "password": "SyntheticTestPassword02!"},
    )
    token = reg.json()["token"]
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"] == "me@palace.example"
    assert body["id"] == reg.json()["user_id"]


# ─── W3 (2026-06-17): register rate limit + constant-time delay ────────────────


def test_register_rate_limit_kicks_in_after_5_failures(client, monkeypatch):
    """同 IP 在 register 路徑連續產 5 個 409，第 5 次應被 429 擋下。

    W3：register 共用 login_limiter 池，攻擊者枚舉 email 會被 IP 鎖。
    """
    # 先建一個 user 製造重複 email
    r = client.post(
        "/auth/register",
        json={"email": "victim@palace.example", "password": "SyntheticTestPassword02!"},
    )
    assert r.status_code == 201, r.text

    # 前 4 次 409
    for i in range(4):
        r = client.post(
            "/auth/register",
            json={"email": "victim@palace.example", "password": "SyntheticTestPassword02!"},
        )
        assert r.status_code == 409, f"attempt {i+1}: {r.status_code} {r.text}"

    # 第 5 次達門檻 → 鎖 → 之後一定 429（不論 email 對不對）
    r = client.post(
        "/auth/register",
        json={"email": "victim@palace.example", "password": "SyntheticTestPassword02!"},
    )
    assert r.status_code == 429, r.text
    # 鎖後連新 email 也擋
    r = client.post(
        "/auth/register",
        json={"email": "another@palace.example", "password": "SyntheticTestPassword02!"},
    )
    assert r.status_code == 429, r.text


def test_register_constant_time_delay_when_enabled(client, monkeypatch):
    """設 REGISTER_DELAY_SECONDS=0.3 → 成功 register 至少花 ~0.3s。

    驗證 timing 補償邏輯有實際效果（沒被 conftest 的 =0 把整段邏輯短路）。
    """
    import time
    monkeypatch.setenv("REGISTER_DELAY_SECONDS", "0.3")
    start = time.monotonic()
    r = client.post(
        "/auth/register",
        json={"email": "slow@palace.example", "password": "SyntheticTestPassword02!"},
    )
    elapsed = time.monotonic() - start
    assert r.status_code == 201, r.text
    assert elapsed >= 0.25, f"delay 沒生效，只花 {elapsed:.2f}s"


# ─── W4 → L9 (2026-06-21): access token TTL env-controllable ──────────────────
# L9 拆 access/refresh：access 預設 15 分鐘（不是 W4 時的 4h），refresh 30 天


def test_access_ttl_default_is_15_minutes():
    """L9：預設 15 min（從 W4 的 4h 縮短）。"""
    from backend.server.auth import (
        ACCESS_TTL_MINUTES_DEFAULT,
        current_access_ttl_minutes,
    )
    assert ACCESS_TTL_MINUTES_DEFAULT == 15
    assert current_access_ttl_minutes() == 15


def test_refresh_ttl_default_is_30_days():
    """L9：refresh token TTL 預設 30 天。"""
    from backend.server.auth import (
        REFRESH_TTL_DAYS_DEFAULT,
        current_refresh_ttl_days,
    )
    assert REFRESH_TTL_DAYS_DEFAULT == 30
    assert current_refresh_ttl_days() == 30


def test_access_ttl_env_overrides(monkeypatch):
    """JWT_ACCESS_TTL_MINUTES env 設 30 → 簽出來的 token exp - iat == 30*60。"""
    monkeypatch.setenv("JWT_ACCESS_TTL_MINUTES", "30")
    monkeypatch.setenv("JWT_SECRET", "test-secret-32+bytes-padding-padding-padding!")
    from backend.server.auth import create_access_token, decode_access_token
    token = create_access_token(user_id=1, email="ttl@palace.example")
    claims = decode_access_token(token)
    assert claims["exp"] - claims["iat"] == 30 * 60


def test_legacy_jwt_ttl_hours_still_works(monkeypatch):
    """向下相容：舊 JWT_TTL_HOURS env（小時）仍生效，轉成分鐘。"""
    monkeypatch.delenv("JWT_ACCESS_TTL_MINUTES", raising=False)
    monkeypatch.setenv("JWT_TTL_HOURS", "12")
    monkeypatch.setenv("JWT_SECRET", "test-secret-32+bytes-padding-padding-padding!")
    from backend.server.auth import create_access_token, decode_access_token
    token = create_access_token(user_id=1, email="legacy@palace.example")
    claims = decode_access_token(token)
    assert claims["exp"] - claims["iat"] == 12 * 3600


def test_access_ttl_invalid_env_falls_back_to_default(monkeypatch):
    """非法 JWT_ACCESS_TTL_MINUTES → fallback 預設 15 min。"""
    monkeypatch.delenv("JWT_TTL_HOURS", raising=False)
    monkeypatch.setenv("JWT_ACCESS_TTL_MINUTES", "not-a-number")
    monkeypatch.setenv("JWT_SECRET", "test-secret-32+bytes-padding-padding-padding!")
    from backend.server.auth import create_access_token, decode_access_token
    token = create_access_token(user_id=1, email="ttl2@palace.example")
    claims = decode_access_token(token)
    assert claims["exp"] - claims["iat"] == 15 * 60
