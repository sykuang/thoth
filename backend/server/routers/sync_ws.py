"""Phase 1 - /ws/sync/{job_id} WebSocket (DB-polling pushes status transitions).

Phase 1 — /ws/sync/{job_id} WebSocket（DB-polling 推 status transitions）。

設計：
  - Auth：優先讀 `Sec-WebSocket-Protocol` header 內的 `bearer, <JWT>` pair（W6），
    其次讀 `Authorization: Bearer <JWT>` header；query param 已棄用以避免 access
    log 把 token 寫進 URL。為相容舊 client，仍保留 `?token=` fallback，但會印 warning。
  - 推送：每 0.3s 撈一次 sync_jobs，status 有變就 send_json；done/failed 就主動 close
  - 不掛 asyncio.Queue：保持 sync_runner 純 threading；推播由 WS endpoint 自己拉

W6 修正（2026-06-17）：
  原本 `?token=<JWT>` 會被 nginx/uvicorn access log 完整紀錄 URL 留 token 明文。
  改走 `Sec-WebSocket-Protocol: bearer, <JWT>` —— 這是業界慣例 (e.g. K8s API,
  Kubernetes dashboard, Hasura)，瀏覽器原生 WebSocket 透過第二參數 subprotocols
  陣列傳，header 不會被 access log 抓。Server accept 時要回 echo 同個 subprotocol
  讓 client 完成 handshake。
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from backend.server.auth import AuthError, decode_access_token
from backend.server.sync_runner import get_job
from backend.server.users import get_user_by_id

router = APIRouter(prefix="/ws", tags=["ws"])
log = logging.getLogger(__name__)

POLL_INTERVAL = 0.3
MAX_WAIT_SECONDS = 600  # safety: 10 分鐘沒結束就 close
TERMINAL_STATES = frozenset({"done", "failed"})


def _extract_ws_token(ws: WebSocket) -> tuple[str, str | None]:
    """Return (token, accept_subprotocol_or_None).

    依序嘗試：
      1. Sec-WebSocket-Protocol: `bearer, <JWT>`（首選）—— 要回 echo subprotocol
      2. Authorization: Bearer <JWT>
      3. Query ?token=<JWT>（向後相容，棄用警告）

    accept_subprotocol：若用方案 1 命中，要在 ws.accept() 時 echo `bearer`，
    否則回 None 由 accept() 不指定 subprotocol。
    """
    # 1) Sec-WebSocket-Protocol subprotocol pair: "bearer, <token>"
    raw_proto = ws.headers.get("sec-websocket-protocol", "")
    if raw_proto:
        parts = [p.strip() for p in raw_proto.split(",") if p.strip()]
        # 預期格式 ["bearer", "<JWT>"]（順序可能反過來，client 端 implementation 不一）
        if len(parts) >= 2 and "bearer" in [p.lower() for p in parts]:
            # 取非 "bearer" 的那個當 token
            for p in parts:
                if p.lower() != "bearer":
                    return p, "bearer"

    # 2) Authorization: Bearer <token>
    auth = ws.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip(), None

    # 3) Query ?token=<JWT>（向後相容，warn）
    token = ws.query_params.get("token", "")
    if token:
        log.warning(
            "WS auth via ?token= query param is deprecated (token leaks to access log). "
            "Use Sec-WebSocket-Protocol: 'bearer, <JWT>' instead.",
        )
        return token, None

    return "", None


async def _authenticate_ws(ws: WebSocket) -> dict | None:
    """從 header/query 取 token、驗 + 撈 user。失敗回 None 並 close。

    accept 必須在 token 取出後做（因為要決定是否 echo subprotocol）。
    """
    token, accept_subprotocol = _extract_ws_token(ws)
    # 先 accept（必須要 accept 才能 send close frame；要記得 echo subprotocol）
    await ws.accept(subprotocol=accept_subprotocol)
    if not token:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION, reason="missing token")
        return None
    try:
        claims = decode_access_token(token)
    except AuthError:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION, reason="invalid token")
        return None
    user = get_user_by_id(int(claims["sub"]))
    if not user:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION, reason="user not found")
        return None
    return user


@router.websocket("/sync/{job_id}")
async def sync_status_ws(ws: WebSocket, job_id: int) -> None:
    """訂閱單 job 的 status transition stream。"""
    # W6: accept 已搬進 _authenticate_ws（需要先看 header 決定 subprotocol echo）
    user = await _authenticate_ws(ws)
    if user is None:
        return

    job = get_job(job_id)
    if job is None or job["user_id"] != user["id"]:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION, reason="job not found")
        return

    last_status = None
    elapsed = 0.0
    try:
        while elapsed < MAX_WAIT_SECONDS:
            job = get_job(job_id)
            if job is None:
                await ws.close(code=status.WS_1011_INTERNAL_ERROR, reason="job vanished")
                return
            if job["status"] != last_status:
                await ws.send_json({
                    "job_id": job["id"],
                    "status": job["status"],
                    "started_at": job["started_at"],
                    "finished_at": job["finished_at"],
                    "error_msg": job["error_msg"],
                    "result_summary": job["result_summary"],
                })
                last_status = job["status"]
                if job["status"] in TERMINAL_STATES:
                    await ws.close()
                    return
            await asyncio.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL
        # 超時
        await ws.close(code=status.WS_1011_INTERNAL_ERROR, reason="timeout waiting for terminal state")
    except WebSocketDisconnect:
        return
