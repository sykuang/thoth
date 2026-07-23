"""Phase 1 — WebSocket /ws/sync/{job_id} end-to-end.

Phase 1 — WebSocket /ws/sync/{job_id} 端到端。

策略：用 DB polling（每 0.3s 撈一次 sync_jobs），偵測 status 變化推給 client。
比 asyncio.Queue 簡單、survive server restart、不必跨 thread/asyncio bridging。

Auth：query param ?token=... （WebSocket 沒有 Authorization header 標準）。

⚠️ starlette 1.0 TestClient quirk（實測 2026-06-11）：
即使 server accept→立刻 close，TestClient `__enter__` 不會 raise；
ws.receive() 會回 {'type':'websocket.close','code':...,'reason':...} dict；
ws.receive_json() 才會 raise WebSocketDisconnect。
最可靠的測法：用 ws.receive() 直接拿 close message dict 驗 code/reason。
詳 wiki/concepts/starlette-testclient-websocket-close-raise-timing-quirk.md
"""
from __future__ import annotations

import time

from starlette.websockets import WebSocketDisconnect


def _register(client, email: str = "wsuser@palace.example"):
    r = client.post("/auth/register",
                    json={"email": email, "password": "SyntheticTestPassword02!"})
    assert r.status_code == 201
    return r.json()["token"]


def _close_msg(ws):
    """Receive raw message; expect a websocket.close envelope. Return (code, reason)."""
    msg = ws.receive()
    assert msg["type"] == "websocket.close", f"expected close, got {msg!r}"
    return msg.get("code", 1000), msg.get("reason", "")


def test_ws_unauthenticated_closes(client):
    """無 token / 假 token → server 立刻 close(code=1008, reason='invalid token')。"""
    with client.websocket_connect("/ws/sync/1?token=not-a-token") as ws:
        code, reason = _close_msg(ws)
    assert code == 1008
    assert "invalid token" in reason.lower(), f"reason={reason!r}"


def test_ws_pushes_status_transitions(client, monkeypatch):
    """訂閱一個 job → 至少收到一筆狀態（queued/running/done）；最終 done。"""
    import backend.server.sync_runner as sr

    # 故意拖延一下、讓 client 有時間看到 running 階段
    def slow_dispatch(bank: str, user_id: int, headless: bool) -> dict:
        time.sleep(0.4)
        return {"delta": {"twd_txn_new": 1}, "stats": {}}

    monkeypatch.setattr(sr, "_dispatch_crawler_and_persist", slow_dispatch)
    token = _register(client)

    job_id = client.post("/sync/sinopac", json={},
                         headers={"Authorization": f"Bearer {token}"}).json()["job_id"]

    msgs = []
    with client.websocket_connect(f"/ws/sync/{job_id}?token={token}") as ws:
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                m = ws.receive_json()
            except WebSocketDisconnect:
                # server 在 terminal 狀態後主動 close；正常結束
                break
            msgs.append(m)
            if m.get("status") in {"done", "failed"}:
                # 繼續 receive 等 close
                continue
            if len(msgs) >= 10:
                break

    assert msgs, "should receive at least one status message"
    final = msgs[-1]
    assert final["status"] == "done", f"last msg unexpected: {msgs}"


def test_ws_for_unknown_job_closes(client):
    """未知 job_id → server 立刻 close(code=1008, reason='job not found')。"""
    token = _register(client, email="wsuser2@palace.example")
    with client.websocket_connect(f"/ws/sync/99999?token={token}") as ws:
        code, reason = _close_msg(ws)
    assert code == 1008
    assert "not found" in reason.lower(), f"reason={reason!r}"


# ─── W6 (2026-06-17): WS auth via Sec-WebSocket-Protocol header ────────────────


def test_ws_auth_via_subprotocol_header_works(client, monkeypatch):
    """W6：用 Sec-WebSocket-Protocol: ['bearer', '<JWT>'] 認證應該成功。

    TestClient.websocket_connect(subprotocols=[...]) 對應到瀏覽器原生
    `new WebSocket(url, ['bearer', token])`，token 不會進 URL → access log 安全。
    """
    import backend.server.sync_runner as sr

    def fast_dispatch(bank: str, user_id: int, headless: bool) -> dict:
        time.sleep(0.1)
        return {"delta": {"twd_txn_new": 0}, "stats": {}}

    monkeypatch.setattr(sr, "_dispatch_crawler_and_persist", fast_dispatch)
    token = _register(client, email="ws-header@palace.example")

    job_id = client.post(
        "/sync/sinopac",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    ).json()["job_id"]

    msgs = []
    # URL 不帶 token，靠 subprotocol header
    with client.websocket_connect(
        f"/ws/sync/{job_id}",
        subprotocols=["bearer", token],
    ) as ws:
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                m = ws.receive_json()
            except WebSocketDisconnect:
                break
            msgs.append(m)
            if m.get("status") in {"done", "failed"}:
                continue
            if len(msgs) >= 10:
                break

    assert msgs, "subprotocol auth 沒收到 message"
    assert msgs[-1]["status"] == "done"


def test_ws_subprotocol_header_invalid_token_closes(client):
    """壞 token 在 subprotocol header 內 → 仍 close(1008)。"""
    with client.websocket_connect(
        "/ws/sync/1",
        subprotocols=["bearer", "not-a-token"],
    ) as ws:
        code, reason = _close_msg(ws)
    assert code == 1008
    assert "invalid token" in reason.lower()


def test_ws_no_token_anywhere_closes(client):
    """完全沒帶 token（沒 subprotocol、沒 ?token）→ close(1008, missing token)。"""
    with client.websocket_connect("/ws/sync/1") as ws:
        code, reason = _close_msg(ws)
    assert code == 1008
    assert "missing token" in reason.lower(), f"reason={reason!r}"
