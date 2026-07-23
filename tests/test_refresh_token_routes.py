"""Tests for refresh-token rotation + reuse detection (L9, 2026-06-21).

Covers:
  * /auth/login + /auth/register 都會回 refresh_token + expires_in
  * /auth/refresh rotate 換新 access + 新 refresh；舊 refresh 立即失效
  * Reuse 攻擊：拿已 revoke 的 refresh 再用 → 401 + 整 family 失效
  * /auth/refresh 對 expired refresh 回 401 但不擴及 family
  * /auth/logout 撤銷單一 refresh（idempotent）
"""
from __future__ import annotations

import time


from backend.server import refresh_tokens as RT
from backend.server.db import get_conn, q


# ---------------------------------------------------------------------------
# /auth/login & /auth/register payload shape
# ---------------------------------------------------------------------------

def test_register_returns_refresh_token_and_expires_in(client):
    r = client.post(
        "/auth/register",
        json={"email": "l9-reg@palace.example", "password": "passw0rd-secret"},
    )
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["access_token"]
    assert b["refresh_token"]
    assert b["token"] == b["access_token"]  # back-compat alias
    assert b["expires_in"] == 15 * 60  # default 15min
    assert len(b["refresh_token"]) >= 40  # url-safe base64 of 48 bytes


def test_login_returns_refresh_token_and_expires_in(client):
    client.post(
        "/auth/register",
        json={"email": "l9-login@palace.example", "password": "passw0rd-secret"},
    )
    r = client.post(
        "/auth/login",
        data={"username": "l9-login@palace.example", "password": "passw0rd-secret"},
    )
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["access_token"]
    assert b["refresh_token"]
    assert b["token_type"] == "bearer"
    assert b["expires_in"] == 15 * 60


# ---------------------------------------------------------------------------
# /auth/refresh — happy path rotation
# ---------------------------------------------------------------------------

def test_refresh_rotates_and_old_refresh_dies(client):
    r = client.post(
        "/auth/register",
        json={"email": "l9-rotate@palace.example", "password": "passw0rd-secret"},
    )
    ref1 = r.json()["refresh_token"]
    acc1 = r.json()["access_token"]

    # 等 1 秒讓 jwt iat 不同 (避免 access token 相同被誤判 cache 命中)
    time.sleep(1.0)

    rr = client.post("/auth/refresh", json={"refresh_token": ref1})
    assert rr.status_code == 200, rr.text
    b = rr.json()
    assert b["access_token"] != acc1, "新 access token 必須跟舊的不同"
    assert b["refresh_token"] != ref1, "新 refresh token 必須跟舊的不同"
    assert b["expires_in"] == 15 * 60

    # 舊 refresh 再用一次 → 401 + 觸發 reuse detection
    reuse = client.post("/auth/refresh", json={"refresh_token": ref1})
    assert reuse.status_code == 401
    assert reuse.json()["detail"] == "請重新登入"


def test_refresh_chain_each_token_only_usable_once(client):
    r = client.post(
        "/auth/register",
        json={"email": "l9-chain@palace.example", "password": "passw0rd-secret"},
    )
    ref = r.json()["refresh_token"]

    # rotate 3 次
    for i in range(3):
        rr = client.post("/auth/refresh", json={"refresh_token": ref})
        assert rr.status_code == 200, f"iter {i}: {rr.text}"
        ref = rr.json()["refresh_token"]

    # 最新 refresh 仍可用
    rr = client.post("/auth/refresh", json={"refresh_token": ref})
    assert rr.status_code == 200


# ---------------------------------------------------------------------------
# Reuse detection — 整 family revoke
# ---------------------------------------------------------------------------

def test_reuse_revokes_entire_family(client):
    """攻擊者偷 refresh_A，受害者後續正常 rotate 拿 refresh_B / refresh_C；
    攻擊者拿 refresh_A 去 /refresh → reuse 偵測 → revoke 整個 family →
    受害者下次拿 refresh_C 也會 401（必須重登）。
    """
    r = client.post(
        "/auth/register",
        json={"email": "l9-reuse@palace.example", "password": "passw0rd-secret"},
    )
    ref_a = r.json()["refresh_token"]

    # 受害者 rotate 兩次到 ref_c
    rr1 = client.post("/auth/refresh", json={"refresh_token": ref_a})
    assert rr1.status_code == 200
    ref_b = rr1.json()["refresh_token"]
    rr2 = client.post("/auth/refresh", json={"refresh_token": ref_b})
    assert rr2.status_code == 200
    ref_c = rr2.json()["refresh_token"]

    # 攻擊者拿 ref_a 試 reuse → 401
    attack = client.post("/auth/refresh", json={"refresh_token": ref_a})
    assert attack.status_code == 401

    # 受害者拿 ref_c → 應該 401（family 整批 revoked）
    victim = client.post("/auth/refresh", json={"refresh_token": ref_c})
    assert victim.status_code == 401, (
        "Family revocation 應該擴及 chain 上所有 token，但 ref_c 還活著"
    )


# ---------------------------------------------------------------------------
# Expiry — 不擴及 family
# ---------------------------------------------------------------------------

def test_expired_refresh_returns_401_but_does_not_revoke_family(client):
    """單純過期 ≠ 攻擊。我們不該 revoke family（否則 user 多裝置時一個過期會
    全部踢出去）。"""
    r = client.post(
        "/auth/register",
        json={"email": "l9-exp@palace.example", "password": "passw0rd-secret"},
    )
    ref1 = r.json()["refresh_token"]
    # 第二個 device login → 同 user 但不同 family
    r2 = client.post(
        "/auth/login",
        data={"username": "l9-exp@palace.example", "password": "passw0rd-secret"},
    )
    ref_other_device = r2.json()["refresh_token"]

    # 手動把 ref1 改成 expired
    from backend.server.auth import hash_token
    with get_conn() as con:
        con.execute(
            q("UPDATE refresh_tokens SET expires_at = ? WHERE token_hash = ?"),
            ("2000-01-01T00:00:00.000Z", hash_token(ref1)),
        )
        con.commit()

    # 過期的 → 401
    rr = client.post("/auth/refresh", json={"refresh_token": ref1})
    assert rr.status_code == 401

    # 另一個 device 的 refresh 應該還活著（不同 family）
    rr2 = client.post("/auth/refresh", json={"refresh_token": ref_other_device})
    assert rr2.status_code == 200, (
        f"Expired token 應該只 revoke 自己，不擴及其他 family: {rr2.text}"
    )


# ---------------------------------------------------------------------------
# /auth/logout
# ---------------------------------------------------------------------------

def test_logout_revokes_refresh_token(client):
    r = client.post(
        "/auth/register",
        json={"email": "l9-logout@palace.example", "password": "passw0rd-secret"},
    )
    ref = r.json()["refresh_token"]

    lo = client.post("/auth/logout", json={"refresh_token": ref})
    assert lo.status_code == 204

    # logout 後 refresh 應該 401
    rr = client.post("/auth/refresh", json={"refresh_token": ref})
    assert rr.status_code == 401


def test_logout_is_idempotent(client):
    """logout 同一個 token 兩次都該回 204；不存在的 token 也回 204。"""
    r = client.post(
        "/auth/register",
        json={"email": "l9-logout-2x@palace.example", "password": "passw0rd-secret"},
    )
    ref = r.json()["refresh_token"]

    assert client.post("/auth/logout", json={"refresh_token": ref}).status_code == 204
    assert client.post("/auth/logout", json={"refresh_token": ref}).status_code == 204
    # 完全不存在的 token
    assert client.post(
        "/auth/logout",
        json={"refresh_token": "this-token-never-existed"},
    ).status_code == 204


# ---------------------------------------------------------------------------
# /auth/refresh 無效 input
# ---------------------------------------------------------------------------

def test_refresh_with_nonexistent_token_returns_401(client):
    r = client.post(
        "/auth/refresh",
        json={"refresh_token": "garbage-not-a-real-token"},
    )
    assert r.status_code == 401


def test_refresh_with_empty_string_returns_422(client):
    r = client.post("/auth/refresh", json={"refresh_token": ""})
    assert r.status_code == 422  # pydantic min_length=1


# ---------------------------------------------------------------------------
# Storage hygiene — DB 不該存明文
# ---------------------------------------------------------------------------

def test_db_only_stores_hashed_token_never_raw(client):
    r = client.post(
        "/auth/register",
        json={"email": "l9-hash@palace.example", "password": "passw0rd-secret"},
    )
    ref = r.json()["refresh_token"]

    with get_conn() as con:
        cur = con.execute(q("SELECT token_hash FROM refresh_tokens"))
        rows = cur.fetchall()
    hashes = [r[0] for r in rows]
    # 任何一個 DB row 都不該等於 raw token
    assert ref not in hashes
    # 但 sha256(ref) 應該在
    from backend.server.auth import hash_token
    assert hash_token(ref) in hashes


# ---------------------------------------------------------------------------
# Repo unit tests (繞過 router, 直接呼叫 refresh_tokens 模組)
# ---------------------------------------------------------------------------

def test_repo_revoke_all_for_user(client, monkeypatch):
    """force logout 所有 device — 改密碼後該清掃所有 session。"""
    r = client.post(
        "/auth/register",
        json={"email": "l9-revoke-all@palace.example", "password": "passw0rd-secret"},
    )
    uid = r.json()["user_id"]
    # 多 device login
    for _ in range(3):
        client.post(
            "/auth/login",
            data={"username": "l9-revoke-all@palace.example", "password": "passw0rd-secret"},
        )
    revoked = RT.revoke_all_for_user(uid)
    assert revoked >= 4  # register 1 + 3 login = 4


def test_repo_prune_expired_deletes_old_rows(client):
    r = client.post(
        "/auth/register",
        json={"email": "l9-prune@palace.example", "password": "passw0rd-secret"},
    )
    ref = r.json()["refresh_token"]
    from backend.server.auth import hash_token
    # Force expiry to past
    with get_conn() as con:
        con.execute(
            q("UPDATE refresh_tokens SET expires_at = ? WHERE token_hash = ?"),
            ("2000-01-01T00:00:00.000Z", hash_token(ref)),
        )
        con.commit()
    deleted = RT.prune_expired()
    assert deleted >= 1
