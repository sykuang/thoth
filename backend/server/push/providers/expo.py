"""ExpoPushProvider — Expo Push Service relay.

POST 通知到 https://exp.host/--/api/v2/push/send,Expo 內部 batch 後轉發 APNs/FCM。
跟 ApnsNotifier 互補 — Expo 自家管 .p8 簽 + HTTP/2 + sandbox 切換,backend 只需要 httpx POST。

需要 env:
  EXPO_ACCESS_TOKEN  (optional, 提高 rate limit; 不設仍可用 free tier)

Frontend 必須 export EXPO_PUBLIC_PUSH_PROVIDER=expo 並走 getExpoPushTokenAsync({projectId}),
token 格式: `ExponentPushToken[xxxxxxxxxxxxxxxxxxxxxx]`

API 文件: https://docs.expo.dev/push-notifications/sending-notifications/
Response shape:
  POST /--/api/v2/push/send  → {"data": [{"status": "ok"|"error", "id": "...", "details": {...}}]}
  Errors (details.error):
    - DeviceNotRegistered    永久失效 → deactivate token
    - InvalidCredentials     server 設定問題,不 deactivate
    - MessageTooBig / MessageRateExceeded   暫時,不 deactivate

Batching: 單 POST 可送多 message,Expo 文件建議 ≤ 100/batch。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from backend.server.push import repo
from backend.server.push.base import (
    NotificationPayload,
    NotifyResult,
    PushTarget,
)

logger = logging.getLogger(__name__)

EXPO_PUSH_ENDPOINT = "https://exp.host/--/api/v2/push/send"
DEFAULT_TIMEOUT_S = 15.0
BATCH_SIZE = 100   # Expo 文件建議 ≤ 100/batch

# 永久失效 reason — 設這些 → deactivate token
PERMANENT_FAILURES = frozenset({
    "DeviceNotRegistered",
})


class ExpoPushProvider:
    name = "expo"

    def __init__(self, timeout_s: float = DEFAULT_TIMEOUT_S):
        # Optional — Expo allows free use without token,有 token 提高 rate limit
        self._access_token = os.environ.get("EXPO_ACCESS_TOKEN", "").strip() or None
        self._timeout = timeout_s

    # -----------------------------------------------------------------
    # public Notifier API
    # -----------------------------------------------------------------

    def send_to_user(
        self, user_id: int, payload: NotificationPayload,
    ) -> NotifyResult:
        targets = repo.list_active_for_user(user_id, provider=self.name)
        logger.info(
            "[expo] send_to_user user_id=%s found %d active token(s) provider=%s",
            user_id, len(targets), self.name,
        )
        if not targets:
            return NotifyResult()
        return self._send_many(targets, payload)

    def send_to_token(
        self, target: PushTarget, payload: NotificationPayload,
    ) -> NotifyResult:
        return self._send_many([target], payload)

    # -----------------------------------------------------------------
    # internals
    # -----------------------------------------------------------------

    def _send_many(
        self,
        targets: list[PushTarget],
        payload: NotificationPayload,
    ) -> NotifyResult:
        """Batch 送多 device (Expo 允許單 POST 多 message)。"""
        result = NotifyResult()
        with httpx.Client(timeout=self._timeout) as client:
            for chunk_start in range(0, len(targets), BATCH_SIZE):
                chunk = targets[chunk_start:chunk_start + BATCH_SIZE]
                self._send_batch(client, chunk, payload, result)
        # caller (sender 層) 該拿 invalid_tokens 去 repo.deactivate;這層也順手做一次
        for bad in result.invalid_tokens:
            repo.deactivate(provider=self.name, token=bad)
        return result

    def _send_batch(
        self,
        client: httpx.Client,
        targets: list[PushTarget],
        payload: NotificationPayload,
        result: NotifyResult,
    ) -> None:
        body = [self._build_message(t, payload) for t in targets]
        headers = self._build_headers()
        logger.info(
            "[expo] POST %s batch_size=%d title=%r body=%r",
            EXPO_PUSH_ENDPOINT, len(targets), payload.title, payload.body,
        )
        try:
            resp = client.post(EXPO_PUSH_ENDPOINT, headers=headers, json=body)
        except httpx.RequestError as e:
            # Transport 失敗 — 整 batch 算 failed,不 invalidate
            logger.warning("[expo] httpx transport error: %s: %s", type(e).__name__, e)
            for t in targets:
                _merge_one(result, NotifyResult(
                    failed_count=1,
                    errors=[(_short(t.token), f"{type(e).__name__}: {e}")],
                ))
            return

        logger.info("[expo] http %s %s body[:200]=%r",
                    resp.status_code, EXPO_PUSH_ENDPOINT, _safe_text(resp))

        # HTTP-level error (5xx Expo 自身爆)
        if resp.status_code >= 500:
            for t in targets:
                _merge_one(result, NotifyResult(
                    failed_count=1,
                    errors=[(_short(t.token), f"HTTP {resp.status_code} (Expo server)")],
                ))
            return

        # 4xx 通常是 request shape 不對,但也可能是 401 (bad EXPO_ACCESS_TOKEN)
        if 400 <= resp.status_code < 500:
            err_text = _safe_text(resp)
            for t in targets:
                _merge_one(result, NotifyResult(
                    failed_count=1,
                    errors=[(_short(t.token), f"HTTP {resp.status_code}: {err_text}")],
                ))
            return

        # 2xx — 解 per-message data array
        try:
            body_json = resp.json()
        except (ValueError, json.JSONDecodeError):
            for t in targets:
                _merge_one(result, NotifyResult(
                    failed_count=1,
                    errors=[(_short(t.token), "Expo 回 200 但 body 不是 JSON")],
                ))
            return

        data = body_json.get("data") if isinstance(body_json, dict) else None
        if not isinstance(data, list) or len(data) != len(targets):
            # Shape 不對 (Expo 文件保證 data.length == request.length)
            for t in targets:
                _merge_one(result, NotifyResult(
                    failed_count=1,
                    errors=[(_short(t.token), "Expo response shape unexpected")],
                ))
            return

        for t, item in zip(targets, data, strict=True):
            status = item.get("status") if isinstance(item, dict) else "error"
            if status == "ok":
                repo.touch(provider=self.name, token=t.token)
                _merge_one(result, NotifyResult(delivered_count=1))
                continue
            # status == "error"
            details = item.get("details") or {}
            err_reason = details.get("error", "")
            msg = item.get("message") or err_reason or "unknown"
            if err_reason in PERMANENT_FAILURES:
                _merge_one(result, NotifyResult(
                    failed_count=1,
                    invalid_tokens=[t.token],
                    errors=[(_short(t.token), f"{err_reason}: {msg}")],
                ))
            else:
                _merge_one(result, NotifyResult(
                    failed_count=1,
                    errors=[(_short(t.token), f"{err_reason or 'error'}: {msg}")],
                ))

    def _build_message(
        self, target: PushTarget, payload: NotificationPayload,
    ) -> dict[str, Any]:
        msg: dict[str, Any] = {
            "to": target.token,
            "title": payload.title,
            "body": payload.body,
        }
        if payload.data:
            msg["data"] = payload.data
        if payload.sound is not None:
            msg["sound"] = payload.sound  # 'default' or null
        if payload.badge is not None:
            msg["badge"] = payload.badge
        if payload.category:
            msg["categoryId"] = payload.category
        # priority high → 立即送達 (跟 APNs apns-priority: 10 對應)
        msg["priority"] = "high"
        # iOS-only — ttl 0 = 不存 retry queue (跟 APNs apns-push-type: alert 對應)
        # 不設讓 Expo 用 default,避免限制 retry
        return msg

    def _build_headers(self) -> dict[str, str]:
        h = {
            "accept": "application/json",
            "accept-encoding": "gzip, deflate",
            "content-type": "application/json",
        }
        if self._access_token:
            h["authorization"] = f"Bearer {self._access_token}"
        return h


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _merge_one(into: NotifyResult, other: NotifyResult) -> None:
    object.__setattr__(into, "delivered_count",
                       into.delivered_count + other.delivered_count)
    object.__setattr__(into, "failed_count",
                       into.failed_count + other.failed_count)
    into.invalid_tokens.extend(other.invalid_tokens)
    into.errors.extend(other.errors)


def _short(token: str) -> str:
    if len(token) <= 24:
        return token
    return token[:16] + "…" + token[-4:]


def _safe_text(resp: httpx.Response) -> str:
    try:
        return resp.text[:200]
    except Exception:
        return "(no body)"
