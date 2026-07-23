"""Phase L5-1 — /accounts routes end-to-end test.

Phase L5-1 — /accounts routes 端到端測試。

涵蓋:
- POST /accounts (建 account; 409 dup; 400 unknown bank)
- GET  /accounts (列當前 user 的 accounts, 包含 fields_set)
- PUT  /accounts/{id} (rename; 404 not-owned)
- DELETE /accounts/{id} (刪 + CASCADE; 404 not-owned)
- PUT  /accounts/{id}/fields (upsert fields; 400 unknown field; 401 not-owned)
- DELETE /accounts/{id}/fields/{name}

回傳一律 metadata only - 絕不回密文/明文。
"""
from __future__ import annotations


def _register(client, email: str = "acct-user@palace.example", password: str = "SyntheticTestPassword02!"):
    r = client.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    return r.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ============================================================
# POST /accounts
# ============================================================

def test_create_account_returns_201_with_metadata(client):
    token = _register(client)
    r = client.post("/accounts", json={"bank": "cathay", "label": "主帳"}, headers=_auth(token))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["bank"] == "cathay"
    assert body["label"] == "主帳"
    assert body["has_creds"] is False
    assert body["fields_set"] == []
    assert isinstance(body["id"], int) and body["id"] > 0


def test_create_account_duplicate_label_409(client):
    token = _register(client)
    r1 = client.post("/accounts", json={"bank": "cathay", "label": "主帳"}, headers=_auth(token))
    assert r1.status_code == 201
    r2 = client.post("/accounts", json={"bank": "cathay", "label": "主帳"}, headers=_auth(token))
    assert r2.status_code == 409


def test_create_account_different_label_same_bank_ok(client):
    token = _register(client)
    r1 = client.post("/accounts", json={"bank": "cathay", "label": "主帳"}, headers=_auth(token))
    r2 = client.post("/accounts", json={"bank": "cathay", "label": "老婆"}, headers=_auth(token))
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["id"] != r2.json()["id"]


def test_create_account_unknown_bank_400(client):
    token = _register(client)
    r = client.post("/accounts", json={"bank": "zionsbank", "label": "x"}, headers=_auth(token))
    assert r.status_code == 400


def test_create_account_empty_label_422(client):
    """Pydantic min_length=1 → 422 (FastAPI validator)"""
    token = _register(client)
    r = client.post("/accounts", json={"bank": "cathay", "label": ""}, headers=_auth(token))
    assert r.status_code == 422


# ============================================================
# GET /accounts
# ============================================================

def test_list_accounts_returns_per_user(client):
    token = _register(client)
    client.post("/accounts", json={"bank": "cathay", "label": "主帳"}, headers=_auth(token))
    client.post("/accounts", json={"bank": "ctbc", "label": "主帳"}, headers=_auth(token))
    r = client.get("/accounts", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 2
    banks = {a["bank"] for a in body}
    assert banks == {"cathay", "ctbc"}


def test_list_accounts_isolation_between_users(client):
    """兩個 user 各自的 account 不能看到對方。"""
    t1 = _register(client, "u1@palace.example")
    t2 = _register(client, "u2@palace.example")
    client.post("/accounts", json={"bank": "cathay", "label": "u1主帳"}, headers=_auth(t1))
    r2 = client.get("/accounts", headers=_auth(t2))
    assert r2.status_code == 200
    assert r2.json() == []


def test_list_accounts_includes_fields_set(client):
    token = _register(client)
    r = client.post("/accounts", json={"bank": "cathay", "label": "主帳"}, headers=_auth(token))
    aid = r.json()["id"]
    client.put(f"/accounts/{aid}/fields", json={"password": "pw"}, headers=_auth(token))
    body = client.get("/accounts", headers=_auth(token)).json()
    target = next(a for a in body if a["id"] == aid)
    assert target["has_creds"] is True
    assert target["fields_set"] == ["password"]


# ============================================================
# PUT /accounts/{id} (rename)
# ============================================================

def test_rename_account_200(client):
    token = _register(client)
    r = client.post("/accounts", json={"bank": "cathay", "label": "原名"}, headers=_auth(token))
    aid = r.json()["id"]
    r2 = client.put(f"/accounts/{aid}", json={"label": "新名"}, headers=_auth(token))
    assert r2.status_code == 200
    assert r2.json()["label"] == "新名"


def test_rename_account_not_owned_404(client):
    t1 = _register(client, "u1@palace.example")
    t2 = _register(client, "u2@palace.example")
    r = client.post("/accounts", json={"bank": "cathay", "label": "x"}, headers=_auth(t1))
    aid = r.json()["id"]
    r2 = client.put(f"/accounts/{aid}", json={"label": "hijack"}, headers=_auth(t2))
    assert r2.status_code == 404


def test_rename_account_to_existing_label_409(client):
    token = _register(client)
    client.post("/accounts", json={"bank": "cathay", "label": "主帳"}, headers=_auth(token))
    r2 = client.post("/accounts", json={"bank": "cathay", "label": "老婆"}, headers=_auth(token))
    aid2 = r2.json()["id"]
    r3 = client.put(f"/accounts/{aid2}", json={"label": "主帳"}, headers=_auth(token))
    assert r3.status_code == 409


# ============================================================
# DELETE /accounts/{id}
# ============================================================

def test_delete_account_204_and_cascade(client):
    token = _register(client)
    r = client.post("/accounts", json={"bank": "cathay", "label": "主帳"}, headers=_auth(token))
    aid = r.json()["id"]
    client.put(f"/accounts/{aid}/fields", json={"password": "x"}, headers=_auth(token))

    rdel = client.delete(f"/accounts/{aid}", headers=_auth(token))
    assert rdel.status_code == 204

    # account 應從 list 消失
    body = client.get("/accounts", headers=_auth(token)).json()
    assert not any(a["id"] == aid for a in body)


def test_delete_account_not_owned_404(client):
    t1 = _register(client, "u1@palace.example")
    t2 = _register(client, "u2@palace.example")
    r = client.post("/accounts", json={"bank": "cathay", "label": "x"}, headers=_auth(t1))
    aid = r.json()["id"]
    r2 = client.delete(f"/accounts/{aid}", headers=_auth(t2))
    assert r2.status_code == 404


# ============================================================
# PUT /accounts/{id}/fields
# ============================================================

def test_put_account_fields_204_and_visible_in_list(client):
    token = _register(client)
    r = client.post("/accounts", json={"bank": "cathay", "label": "主帳"}, headers=_auth(token))
    aid = r.json()["id"]
    rput = client.put(
        f"/accounts/{aid}/fields",
        json={"cust_id": "A123", "user_id": "kk", "password": "secret"},
        headers=_auth(token),
    )
    assert rput.status_code == 204

    body = client.get("/accounts", headers=_auth(token)).json()
    target = next(a for a in body if a["id"] == aid)
    assert set(target["fields_set"]) == {"cust_id", "user_id", "password"}
    # 不可回明文
    assert "secret" not in client.get("/accounts", headers=_auth(token)).text


def test_put_account_fields_unknown_field_400(client):
    token = _register(client)
    r = client.post("/accounts", json={"bank": "cathay", "label": "主帳"}, headers=_auth(token))
    aid = r.json()["id"]
    rput = client.put(
        f"/accounts/{aid}/fields",
        json={"national_id": "wrong-for-cathay"},
        headers=_auth(token),
    )
    assert rput.status_code == 400


def test_put_account_fields_empty_body_400(client):
    token = _register(client)
    r = client.post("/accounts", json={"bank": "cathay", "label": "主帳"}, headers=_auth(token))
    aid = r.json()["id"]
    rput = client.put(f"/accounts/{aid}/fields", json={}, headers=_auth(token))
    assert rput.status_code == 400


def test_put_account_fields_not_owned_404(client):
    t1 = _register(client, "u1@palace.example")
    t2 = _register(client, "u2@palace.example")
    r = client.post("/accounts", json={"bank": "cathay", "label": "x"}, headers=_auth(t1))
    aid = r.json()["id"]
    rput = client.put(
        f"/accounts/{aid}/fields",
        json={"password": "hijack"},
        headers=_auth(t2),
    )
    assert rput.status_code == 404


# ============================================================
# DELETE /accounts/{id}/fields/{name}
# ============================================================

def test_delete_account_field_204(client):
    token = _register(client)
    r = client.post("/accounts", json={"bank": "cathay", "label": "主帳"}, headers=_auth(token))
    aid = r.json()["id"]
    client.put(
        f"/accounts/{aid}/fields",
        json={"password": "x", "user_id": "y"},
        headers=_auth(token),
    )
    rdel = client.delete(f"/accounts/{aid}/fields/password", headers=_auth(token))
    assert rdel.status_code == 204
    body = client.get("/accounts", headers=_auth(token)).json()
    target = next(a for a in body if a["id"] == aid)
    assert target["fields_set"] == ["user_id"]


def test_delete_account_field_unknown_field_400(client):
    token = _register(client)
    r = client.post("/accounts", json={"bank": "cathay", "label": "主帳"}, headers=_auth(token))
    aid = r.json()["id"]
    rdel = client.delete(f"/accounts/{aid}/fields/national_id", headers=_auth(token))
    assert rdel.status_code == 400
