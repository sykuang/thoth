"""ApnsNotifier — direct Apple Push Notification service via HTTP/2.

直連 APNs (api.push.apple.com / api.sandbox.push.apple.com)，
不走 Expo Push Service relay — 使用者要的 B 路徑。

需要的 deps:
  pyjwt[crypto]>=2.8    # ES256 JWT 簽 .p8 key
  httpx[http2]>=0.27    # HTTP/2 — APNs 強制要求

需要的 env (PUSH_PROVIDER=apns 時):
  APNS_KEY_ID         (10 字英數, e.g. 'ABC1234567')
  APNS_TEAM_ID        ('ABCDE12345' as an example)
  APNS_BUNDLE_ID      ('com.example.thoth')
  APNS_KEY_PATH       (path to .p8 file)
  APNS_USE_SANDBOX    ('1' for dev/TestFlight Internal, '0' for prod App Store)
                      default 0 — 使用者現在用 dev profile 安裝 + 之後 TestFlight 走 production gateway

APNs JWT spec (provider authentication tokens):
  https://developer.apple.com/documentation/usernotifications/establishing-a-token-based-connection-to-apns
  * Header: alg=ES256, kid=<APNS_KEY_ID>
  * Claims: iss=<APNS_TEAM_ID>, iat=<now epoch>
  * Sign with .p8 private key
  * Token TTL: refresh every 50min (Apple 接受最多 60min)

APNs request:
  POST /3/device/<device_token_hex>
  Headers:
    authorization: bearer <jwt>
    apns-topic: <bundle_id>     (no .voip suffix for normal alerts)
    apns-push-type: alert
    apns-priority: 10           (immediate display)
  Body: {"aps":{"alert":{"title":..., "body":...}, "badge":..., "sound":...},
         "custom_data": {...}}

Invalid token responses (per Apple docs):
  410 Gone + reason="Unregistered"   — token 永遠失效, deactivate
  400 + reason="BadDeviceToken"      — token 格式不對 / wrong env (dev vs prod), deactivate
  403 + reason="ExpiredProviderToken" — JWT 過期 — refresh JWT 重試 (內部處理)
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from threading import Lock
from typing import Any

import httpx
import jwt as pyjwt  # pyjwt[crypto]

from backend.server.push import repo
from backend.server.push.base import (
    NotificationPayload,
    NotifyResult,
    PushTarget,
)

logger = logging.getLogger(__name__)

APNS_PROD_HOST = "https://api.push.apple.com"
APNS_SANDBOX_HOST = "https://api.sandbox.push.apple.com"
JWT_TTL_S = 50 * 60        # refresh every 50min (Apple 接受 < 60min)
HTTP2_TIMEOUT_S = 10.0
# 永久失效 reason — 設這些 → deactivate token
PERMANENT_FAILURES = frozenset({
    "Unregistered",
    "BadDeviceToken",
    "DeviceTokenNotForTopic",
})


class ApnsConfigError(RuntimeError):
    """Env 缺值 / .p8 讀不到 / Key ID 格式錯。"""


class ApnsNotifier:
    name = "apns"

    def __init__(self) -> None:
        self.key_id = os.environ.get("APNS_KEY_ID", "").strip()
        self.team_id = os.environ.get("APNS_TEAM_ID", "").strip()
        self.bundle_id = os.environ.get("APNS_BUNDLE_ID", "").strip()
        self.key_path = os.environ.get("APNS_KEY_PATH", "").strip()
        self.use_sandbox = os.environ.get("APNS_USE_SANDBOX", "0").strip() in (
            "1", "true", "yes",
        )
        missing = [
            n for n, v in [
                ("APNS_KEY_ID", self.key_id),
                ("APNS_TEAM_ID", self.team_id),
                ("APNS_BUNDLE_ID", self.bundle_id),
                ("APNS_KEY_PATH", self.key_path),
            ] if not v
        ]
        if missing:
            raise ApnsConfigError(
                f"PUSH_PROVIDER=apns 缺少 env: {', '.join(missing)}",
            )
        if not Path(self.key_path).is_file():
            raise ApnsConfigError(f"APNS_KEY_PATH 找不到檔案: {self.key_path}")
        self._private_key = Path(self.key_path).read_text(encoding="utf-8")
        self._jwt_cache: tuple[str, float] | None = None  # (token, expires_at)
        self._jwt_lock = Lock()
        self._host = APNS_SANDBOX_HOST if self.use_sandbox else APNS_PROD_HOST

    # -----------------------------------------------------------------
    # public Notifier API
    # -----------------------------------------------------------------

    def send_to_user(
        self, user_id: int, payload: NotificationPayload,
    ) -> NotifyResult:
        targets = repo.list_active_for_user(user_id, provider=self.name)
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
        """共用 HTTP/2 client 對多個 device 送 — APNs HTTP/2 同 connection 多 request 是 idiomatic."""
        result = NotifyResult()
        body = self._build_body(payload)
        body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        with httpx.Client(http2=True, timeout=HTTP2_TIMEOUT_S) as client:
            for t in targets:
                r = self._send_one(client, t, body_bytes, payload)
                self._merge(result, r)
        # caller (sender 層) 該拿 invalid_tokens 去 repo.deactivate；這層也順手做一次
        for bad in result.invalid_tokens:
            repo.deactivate(provider=self.name, token=bad)
        return result

    def _send_one(
        self,
        client: httpx.Client,
        target: PushTarget,
        body_bytes: bytes,
        payload: NotificationPayload,
    ) -> NotifyResult:
        url = f"{self._host}/3/device/{target.token}"
        headers = self._build_headers(payload)
        try:
            resp = client.post(url, headers=headers, content=body_bytes)
        except httpx.RequestError as e:
            return NotifyResult(
                failed_count=1,
                errors=[(_short(target.token), f"{type(e).__name__}: {e}")],
            )

        # Happy path — 200 沒 body, 200 即成功
        if resp.status_code == 200:
            repo.touch(provider=self.name, token=target.token)
            return NotifyResult(delivered_count=1)

        # 解 APNs 錯誤 (4xx/5xx 都有 JSON body with reason)
        reason = _extract_reason(resp)
        # 403 ExpiredProviderToken — refresh JWT, 重試一次
        if resp.status_code == 403 and reason == "ExpiredProviderToken":
            self._invalidate_jwt()
            headers = self._build_headers(payload)
            try:
                resp = client.post(url, headers=headers, content=body_bytes)
            except httpx.RequestError as e:
                return NotifyResult(
                    failed_count=1,
                    errors=[(_short(target.token), f"retry/{type(e).__name__}: {e}")],
                )
            if resp.status_code == 200:
                repo.touch(provider=self.name, token=target.token)
                return NotifyResult(delivered_count=1)
            reason = _extract_reason(resp)

        # 永久失效 → invalidate token
        if reason in PERMANENT_FAILURES:
            return NotifyResult(
                failed_count=1,
                invalid_tokens=[target.token],
                errors=[(_short(target.token), f"HTTP {resp.status_code} {reason}")],
            )

        # 暫時失敗 (429 TooManyRequests / 5xx ServerError) — 不 invalidate
        return NotifyResult(
            failed_count=1,
            errors=[(_short(target.token), f"HTTP {resp.status_code} {reason}")],
        )

    def _build_headers(self, payload: NotificationPayload) -> dict[str, str]:
        jwt_token = self._get_jwt()
        headers: dict[str, str] = {
            "authorization": f"bearer {jwt_token}",
            "apns-topic": self.bundle_id,
            "apns-push-type": "alert",
            "apns-priority": "10",
        }
        if payload.category:
            headers["apns-collapse-id"] = payload.category[:64]
        return headers

    def _build_body(self, payload: NotificationPayload) -> dict[str, Any]:
        aps: dict[str, Any] = {
            "alert": {
                "title": payload.title,
                "body": payload.body,
            },
        }
        if payload.badge is not None:
            aps["badge"] = payload.badge
        if payload.sound is not None:
            aps["sound"] = payload.sound
        if payload.category:
            aps["category"] = payload.category
        body: dict[str, Any] = {"aps": aps}
        # custom data 平鋪在 root (APNs 慣例 — 非 aps 內的 key 是 app 自訂)
        for k, v in (payload.data or {}).items():
            if k == "aps":
                continue  # 不該覆蓋 aps
            body[k] = v
        return body

    def _get_jwt(self) -> str:
        """生 / cache provider JWT (ES256 簽 .p8)。"""
        with self._jwt_lock:
            now = time.time()
            if self._jwt_cache:
                token, exp = self._jwt_cache
                if exp - now > 60:  # 還剩 > 60s 就用 cache
                    return token
            iat = int(now)
            payload = {"iss": self.team_id, "iat": iat}
            headers = {"alg": "ES256", "kid": self.key_id}
            token = pyjwt.encode(
                payload, self._private_key, algorithm="ES256", headers=headers,
            )
            self._jwt_cache = (token, now + JWT_TTL_S)
            return token

    def _invalidate_jwt(self) -> None:
        with self._jwt_lock:
            self._jwt_cache = None

    def _merge(self, into: NotifyResult, other: NotifyResult) -> None:
        object.__setattr__(into, "delivered_count",
                           into.delivered_count + other.delivered_count)
        object.__setattr__(into, "failed_count",
                           into.failed_count + other.failed_count)
        into.invalid_tokens.extend(other.invalid_tokens)
        into.errors.extend(other.errors)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _extract_reason(resp: httpx.Response) -> str:
    """APNs 4xx/5xx body 是 `{"reason": "Unregistered"}`，沒 body 就 ''."""
    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError):
        return ""
    return body.get("reason", "") if isinstance(body, dict) else ""


def _short(token: str) -> str:
    if len(token) <= 16:
        return token
    return token[:8] + "…" + token[-4:]
