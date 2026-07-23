"""Phase L11 — push token registration routes (PUT/GET/DELETE /me/push-tokens).

Phase L11 — /me/push-tokens 三個 endpoint 的端到端 happy + edge case 測試.
"""
from __future__ import annotations



def _register(client, email="push@palace.example", pw="SyntheticTestPassword02!"):
    r = client.post("/auth/register", json={"email": email, "password": pw})
    assert r.status_code == 201, r.text
    return r.json()["token"]


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_register_push_token_apns(client):
    token = _register(client)
    r = client.put(
        "/me/push-tokens",
        json={
            "provider": "apns",
            "token": "a" * 64,
            "platform": "ios",
            "device_label": "Kphone",
        },
        headers=_bearer(token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "apns"
    assert body["platform"] == "ios"
    assert body["device_label"] == "Kphone"
    assert body["active"] is True
    # token_preview 不應該回完整 token
    assert "token_preview" in body
    assert body["token_preview"] != "a" * 64


def test_register_idempotent_same_token_twice(client):
    token = _register(client, "idem@palace.example")
    body = {
        "provider": "apns",
        "token": "b" * 64,
        "platform": "ios",
        "device_label": "Kphone",
    }
    r1 = client.put("/me/push-tokens", json=body, headers=_bearer(token))
    r2 = client.put("/me/push-tokens", json=body, headers=_bearer(token))
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]


def test_register_webhook_provider(client):
    token = _register(client, "webhook@palace.example")
    r = client.put(
        "/me/push-tokens",
        json={
            "provider": "webhook",
            "token": "https://discord.com/api/webhooks/123/abc",
            "platform": "web",
            "device_label": "Discord 私訊",
        },
        headers=_bearer(token),
    )
    assert r.status_code == 200, r.text


def test_register_invalid_provider_422(client):
    token = _register(client, "badprov@palace.example")
    r = client.put(
        "/me/push-tokens",
        json={"provider": "bogus", "token": "x"},
        headers=_bearer(token),
    )
    assert r.status_code == 422


def test_register_missing_token_422(client):
    token = _register(client, "notoken@palace.example")
    r = client.put(
        "/me/push-tokens",
        json={"provider": "apns"},
        headers=_bearer(token),
    )
    assert r.status_code == 422


def test_register_requires_auth_401(client):
    r = client.put(
        "/me/push-tokens",
        json={"provider": "apns", "token": "x" * 64, "platform": "ios"},
    )
    assert r.status_code == 401


def test_list_returns_only_own_tokens(client):
    # user A 註冊兩個 device
    token_a = _register(client, "listA@palace.example")
    client.put("/me/push-tokens", json={
        "provider": "apns", "token": "AAAAAAAAAA64chars" + "0" * 47,
        "platform": "ios", "device_label": "iPhone A",
    }, headers=_bearer(token_a))
    client.put("/me/push-tokens", json={
        "provider": "apns", "token": "BBBBBBBBBB64chars" + "1" * 47,
        "platform": "ios", "device_label": "iPad A",
    }, headers=_bearer(token_a))

    # user B 註冊一個
    token_b = _register(client, "listB@palace.example")
    client.put("/me/push-tokens", json={
        "provider": "apns", "token": "CCCCCCCCCC64chars" + "2" * 47,
        "platform": "ios", "device_label": "iPhone B",
    }, headers=_bearer(token_b))

    # A 只應該看到自己的 2 個
    r = client.get("/me/push-tokens", headers=_bearer(token_a))
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    labels = sorted(item["device_label"] for item in body["items"])
    assert labels == ["iPad A", "iPhone A"]

    # B 只應該看到自己的 1 個
    r = client.get("/me/push-tokens", headers=_bearer(token_b))
    assert r.json()["count"] == 1


def test_delete_own_token_204(client):
    token = _register(client, "del@palace.example")
    r = client.put("/me/push-tokens", json={
        "provider": "apns", "token": "D" * 64, "platform": "ios",
    }, headers=_bearer(token))
    tid = r.json()["id"]

    r = client.delete(f"/me/push-tokens/{tid}", headers=_bearer(token))
    assert r.status_code == 204

    # GET 應該空了
    r = client.get("/me/push-tokens", headers=_bearer(token))
    assert r.json()["count"] == 0


def test_delete_other_user_token_404(client):
    """user A 不能刪 user B 的 token (ownership check)."""
    token_a = _register(client, "ownA@palace.example")
    token_b = _register(client, "ownB@palace.example")
    r = client.put("/me/push-tokens", json={
        "provider": "apns", "token": "E" * 64, "platform": "ios",
    }, headers=_bearer(token_b))
    b_token_id = r.json()["id"]

    # A 嘗試刪 B 的 → 404 (假裝不存在,不洩漏資訊)
    r = client.delete(f"/me/push-tokens/{b_token_id}", headers=_bearer(token_a))
    assert r.status_code == 404


def test_delete_nonexistent_404(client):
    token = _register(client, "del404@palace.example")
    r = client.delete("/me/push-tokens/999999", headers=_bearer(token))
    assert r.status_code == 404


def test_register_handles_handoff_between_users(client):
    """同 token 在兩個 user 之間轉手 (手機賣掉場景) — UPSERT 改 user_id."""
    token_a = _register(client, "handoff_a@palace.example")
    token_b = _register(client, "handoff_b@palace.example")
    shared_token = "F" * 64

    r1 = client.put("/me/push-tokens", json={
        "provider": "apns", "token": shared_token, "platform": "ios",
        "device_label": "舊主人",
    }, headers=_bearer(token_a))
    id_first = r1.json()["id"]

    r2 = client.put("/me/push-tokens", json={
        "provider": "apns", "token": shared_token, "platform": "ios",
        "device_label": "新主人",
    }, headers=_bearer(token_b))
    # 同個 row (UNIQUE(provider, token)) 但 user_id 已換到 B
    assert r2.json()["id"] == id_first
    assert r2.json()["device_label"] == "新主人"

    # A 看不到 (因為 user_id 已換)
    assert client.get("/me/push-tokens", headers=_bearer(token_a)).json()["count"] == 0
    # B 看到
    assert client.get("/me/push-tokens", headers=_bearer(token_b)).json()["count"] == 1
