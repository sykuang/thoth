"""Security tests: X-API-Key middleware + login rate limiter (Phase L8.5).

Security 測：X-API-Key middleware + login rate limiter (Phase L8.5)。

涵蓋：
- API key 沒設 → 任 request 過
- API key 設了 → 缺 key 401 / 錯 key 401 / 對 key 200
- API key 例外 path `/healthz`
- API key fallback `?api_key=` query string
- API key OPTIONS (CORS preflight) 不擋
- Rate limit：4 次失敗仍 401、第 5 次 429、鎖定窗內再來也 429
- 成功 login 清計數
- 成功登入後又失敗 N 次，計數歸零重來
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient


# ─── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path: Path) -> Iterator[None]:
    """每個 test 開乾淨 data dir + JWT secret + 清掉 SERVER_API_KEY。"""
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET", "test-secret-do-not-use-in-prod-please-use-32+-bytes")
    monkeypatch.delenv("SERVER_API_KEY", raising=False)
    monkeypatch.delenv("LOGIN_MAX_FAILURES", raising=False)
    monkeypatch.delenv("LOGIN_LOCKOUT_SECONDS", raising=False)
    yield


@pytest.fixture
def client_no_api_key() -> Iterator[TestClient]:
    """無 SERVER_API_KEY 設定的 client。"""
    # 確保 app 模組重新 import（middleware 在 import 時讀 env 不會，所以實際每 request 才讀）
    from backend.server.app import app
    from backend.server.security import login_limiter

    login_limiter.reset()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client_with_api_key(monkeypatch) -> Iterator[TestClient]:
    """有 SERVER_API_KEY=secret-123 的 client。"""
    monkeypatch.setenv("SERVER_API_KEY", "secret-123")
    from backend.server.app import app
    from backend.server.security import login_limiter

    login_limiter.reset()
    with TestClient(app) as c:
        yield c


# ─── X-API-Key middleware ──────────────────────────────────────────────────────


def test_api_key_no_env_allows_all_requests(client_no_api_key: TestClient) -> None:
    """沒設 SERVER_API_KEY → no-op 不檢查"""
    r = client_no_api_key.get("/healthz")
    assert r.status_code == 200


def test_api_key_set_but_missing_header_returns_401(client_with_api_key: TestClient) -> None:
    r = client_with_api_key.post(
        "/auth/register", json={"email": "a@b.com", "password": "secret123"}
    )
    assert r.status_code == 401
    assert "X-API-Key" in r.json()["detail"]


def test_api_key_set_with_wrong_header_returns_401(client_with_api_key: TestClient) -> None:
    r = client_with_api_key.post(
        "/auth/register",
        json={"email": "a@b.com", "password": "secret123"},
        headers={"X-API-Key": "wrong"},
    )
    assert r.status_code == 401


def test_api_key_set_with_correct_header_passes(client_with_api_key: TestClient) -> None:
    r = client_with_api_key.post(
        "/auth/register",
        json={"email": "a@b.com", "password": "secret123"},
        headers={"X-API-Key": "secret-123"},
    )
    assert r.status_code == 201


def test_api_key_exempt_healthz_no_key_needed(client_with_api_key: TestClient) -> None:
    """容器健康檢查不該被 API key 卡住"""
    r = client_with_api_key.get("/healthz")
    assert r.status_code == 200


def test_api_key_query_string_fallback(client_with_api_key: TestClient) -> None:
    """fallback: ?api_key= query string 也接受（行動端開圖片用）"""
    r = client_with_api_key.post(
        "/auth/register?api_key=secret-123",
        json={"email": "a@b.com", "password": "secret123"},
    )
    assert r.status_code == 201


def test_api_key_options_preflight_passes(client_with_api_key: TestClient) -> None:
    """瀏覽器 CORS preflight 不會帶 X-API-Key，必須放行"""
    r = client_with_api_key.options(
        "/auth/login",
        headers={
            "Origin": "http://localhost:8081",
            "Access-Control-Request-Method": "POST",
        },
    )
    # CORS middleware 會回 200，這裡只驗 API key 沒擋（不是 401）
    assert r.status_code != 401


# ─── Login rate limit ──────────────────────────────────────────────────────────


def _register(client: TestClient, email: str = "u@test.com", pw: str = "real-password-123") -> int:
    r = client.post("/auth/register", json={"email": email, "password": pw})
    assert r.status_code == 201, r.text
    return r.json()["user_id"]


def _login(client: TestClient, email: str, pw: str):
    return client.post("/auth/login", data={"username": email, "password": pw})


def test_login_rate_limit_locks_after_5_failures(client_no_api_key: TestClient) -> None:
    _register(client_no_api_key)

    # 前 4 次：401（密碼錯，未鎖）
    for i in range(4):
        r = _login(client_no_api_key, "u@test.com", "wrong-pw")
        assert r.status_code == 401, f"attempt {i+1}: {r.json()}"
        assert "剩餘" in r.json()["detail"]

    # 第 5 次：429（達門檻 → 鎖）
    r = _login(client_no_api_key, "u@test.com", "wrong-pw")
    assert r.status_code == 429
    assert "鎖定" in r.json()["detail"]


def test_login_lockout_blocks_subsequent_attempts(client_no_api_key: TestClient) -> None:
    """鎖定後即便密碼對也被擋（鎖的是 IP 不是帳號）"""
    _register(client_no_api_key)

    for _ in range(5):
        _login(client_no_api_key, "u@test.com", "wrong-pw")

    # 鎖定窗內，連對的密碼也被擋
    r = _login(client_no_api_key, "u@test.com", "real-password-123")
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_login_success_resets_failure_counter(client_no_api_key: TestClient) -> None:
    """成功登入後失敗計數歸零，重新可以再失敗 4 次"""
    _register(client_no_api_key)

    # 失敗 3 次
    for _ in range(3):
        _login(client_no_api_key, "u@test.com", "wrong-pw")

    # 成功一次（清計數）
    r = _login(client_no_api_key, "u@test.com", "real-password-123")
    assert r.status_code == 200

    # 再失敗 4 次：仍是 401（不該因前面的 3 次累計到 429）
    for i in range(4):
        r = _login(client_no_api_key, "u@test.com", "wrong-pw")
        assert r.status_code == 401, f"after success, attempt {i+1}: {r.json()}"


def test_login_lockout_seconds_env_overrides_default(monkeypatch, tmp_path) -> None:
    """LOGIN_LOCKOUT_SECONDS env 設小一點 → 鎖定後等過再來能解鎖"""
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET", "test-secret-do-not-use-in-prod-please-use-32+-bytes")
    monkeypatch.setenv("LOGIN_LOCKOUT_SECONDS", "1")
    monkeypatch.setenv("LOGIN_MAX_FAILURES", "3")

    from backend.server.app import app
    from backend.server.security import login_limiter

    login_limiter.reset()
    with TestClient(app) as client:
        _register(client)

        # 3 次失敗 → 鎖
        for _ in range(3):
            _login(client, "u@test.com", "wrong-pw")

        r = _login(client, "u@test.com", "real-password-123")
        assert r.status_code == 429

        # 等過鎖定窗
        time.sleep(1.1)

        # 解鎖：對的密碼可以登
        r = _login(client, "u@test.com", "real-password-123")
        assert r.status_code == 200


def test_login_max_failures_env_overrides(monkeypatch, tmp_path) -> None:
    """LOGIN_MAX_FAILURES=2 → 第 2 次就 429"""
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET", "test-secret-do-not-use-in-prod-please-use-32+-bytes")
    monkeypatch.setenv("LOGIN_MAX_FAILURES", "2")

    from backend.server.app import app
    from backend.server.security import login_limiter

    login_limiter.reset()
    with TestClient(app) as client:
        _register(client)

        r1 = _login(client, "u@test.com", "wrong-pw")
        assert r1.status_code == 401

        r2 = _login(client, "u@test.com", "wrong-pw")
        assert r2.status_code == 429


def test_rate_limit_works_alongside_api_key(client_with_api_key: TestClient) -> None:
    """API key 過了還要過 rate limit（兩層獨立）"""
    headers = {"X-API-Key": "secret-123"}
    _ = client_with_api_key.post(
        "/auth/register",
        json={"email": "u@test.com", "password": "real-password-123"},
        headers=headers,
    )

    r = None
    for _ in range(5):
        r = client_with_api_key.post(
            "/auth/login",
            data={"username": "u@test.com", "password": "wrong-pw"},
            headers=headers,
        )
    assert r is not None
    assert r.status_code == 429


# ─── C8 (2026-06-17): API key timing-attack-safe comparison ────────────────────


def test_api_key_uses_constant_time_compare(client_with_api_key: TestClient) -> None:
    """C8：API key 比較走 secrets.compare_digest，
    確保即便傳了長度差很大的 key 也不會在 ~== 處短路漏 timing info。

    這個 test 不直接量 timing（太脆），改用「結果一致性」+ source-level inspect 證明。
    """
    # 對的：通
    r_ok = client_with_api_key.post(
        "/auth/register",
        json={"email": "ct@test.com", "password": "real-password-123"},
        headers={"X-API-Key": "secret-123"},
    )
    assert r_ok.status_code == 201

    # 長度極不一樣的錯 key：仍 401（且 server 不該因長度差炸）
    r_wrong_short = client_with_api_key.post(
        "/auth/register",
        json={"email": "ct2@test.com", "password": "real-password-123"},
        headers={"X-API-Key": "x"},
    )
    assert r_wrong_short.status_code == 401

    r_wrong_long = client_with_api_key.post(
        "/auth/register",
        json={"email": "ct3@test.com", "password": "real-password-123"},
        headers={"X-API-Key": "x" * 200},
    )
    assert r_wrong_long.status_code == 401

    # source-level guarantee：security.py 必須真的 import + 使用 secrets.compare_digest
    from pathlib import Path
    sec_src = Path(__file__).parent.parent.joinpath("backend/server/security.py").read_text(
        encoding="utf-8",
    )
    assert "import secrets" in sec_src, "security.py 沒 import secrets module"
    assert "secrets.compare_digest" in sec_src, "API key 比較沒走 secrets.compare_digest"
