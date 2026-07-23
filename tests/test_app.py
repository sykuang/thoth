"""Phase 1 — FastAPI app smoke tests (health check + 401 protection).

Phase 1 — FastAPI app smoke tests（健康檢查 + 401 防護）。
"""
from __future__ import annotations


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_me_requires_auth(client):
    r = client.get("/auth/me")
    assert r.status_code == 401
