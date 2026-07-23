"""Phase 1 — /credentials/{bank} routes (end-to-end).

Phase 1 — /credentials/{bank} routes（端到端）。

PUT 寫 / GET 列 / DELETE 全清 / DELETE 單欄。
回傳一律 metadata only，永不回密文/明文。

銀行白名單：backend.core.creds.ALL_CREDS 內每個 cls.BANK.lower()。
欄位白名單：cls._attrs()。
"""
from __future__ import annotations


def _register_and_login(client, email: str = "creds-user@palace.example", password: str = "SyntheticTestPassword02!"):
    r = client.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    return r.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_put_credential_then_get_lists_field(client):
    token = _register_and_login(client)
    # 寫一個 sinopac.national_id
    r = client.put(
        "/credentials/sinopac",
        json={"national_id": "B123456789"},
        headers=_auth(token),
    )
    assert r.status_code == 204, r.text

    r = client.get("/credentials", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    # 找 sinopac 那筆
    sinopac = next((b for b in body if b["bank"] == "sinopac"), None)
    assert sinopac is not None
    assert sinopac["has_creds"] is True
    assert "national_id" in sinopac["fields_set"]
    # 不可回傳明文或密文
    raw = r.text
    assert "B123456789" not in raw


def test_unknown_bank_400(client):
    token = _register_and_login(client)
    r = client.put(
        "/credentials/zionsbank",
        json={"national_id": "x"},
        headers=_auth(token),
    )
    assert r.status_code == 400, r.text


def test_unknown_field_400(client):
    token = _register_and_login(client)
    r = client.put(
        "/credentials/sinopac",
        json={"secret_handshake": "x"},
        headers=_auth(token),
    )
    assert r.status_code == 400, r.text


def test_delete_field_removes_only_that_field(client):
    token = _register_and_login(client)
    client.put(
        "/credentials/sinopac",
        json={"national_id": "A123", "password": "pw"},
        headers=_auth(token),
    )
    r = client.delete("/credentials/sinopac/password", headers=_auth(token))
    assert r.status_code == 204, r.text

    body = client.get("/credentials", headers=_auth(token)).json()
    sinopac = next(b for b in body if b["bank"] == "sinopac")
    assert "national_id" in sinopac["fields_set"]
    assert "password" not in sinopac["fields_set"]


def test_delete_bank_removes_all_fields_for_bank(client):
    token = _register_and_login(client)
    client.put(
        "/credentials/sinopac",
        json={"national_id": "A123", "user_code": "u", "password": "p"},
        headers=_auth(token),
    )
    r = client.delete("/credentials/sinopac", headers=_auth(token))
    assert r.status_code == 204, r.text

    body = client.get("/credentials", headers=_auth(token)).json()
    sinopac = next(b for b in body if b["bank"] == "sinopac")
    assert sinopac["fields_set"] == []
    assert sinopac["has_creds"] is False


def test_credentials_isolated_between_users(client):
    t1 = _register_and_login(client, email="u1@palace.example")
    t2 = _register_and_login(client, email="u2@palace.example")

    client.put(
        "/credentials/sinopac",
        json={"national_id": "A111"},
        headers=_auth(t1),
    )
    # user2 不應看見 user1 的欄位
    body = client.get("/credentials", headers=_auth(t2)).json()
    sinopac = next(b for b in body if b["bank"] == "sinopac")
    assert sinopac["fields_set"] == []
    assert sinopac["has_creds"] is False
