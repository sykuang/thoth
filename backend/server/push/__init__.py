"""Pluggable push notification subsystem (L11, 2026-06-22).

設計鐵令（使用者批示）:
  * 開源 user clone 下來 zero-config 跑 backend → default `PUSH_PROVIDER=none`
  * `expo-notifications` 套件裝在 frontend, 但**不**走 Expo Push Service relay
    — 用 `getDevicePushTokenAsync()` 拿 raw APNs token, backend 直接打 APNs HTTP/2
  * Provider lazy import — 缺 dep 時給友善錯誤 "pip install 'thoth[push-apns]'"
  * Multi-tenant — 每個 user 可同時有多個 token (Kphone + iPad + 老婆手機)

Provider matrix (Day 1):
  * none     — 預設, 不送通知, 開源安全 default
  * apns     — Apple Push Notification service 直連 (HTTP/2 + ES256 JWT)
  * webhook  — generic POST (Discord / Slack / 自架 endpoint), 純 httpx
  * multi    — fanout 多個 provider

Public facade:
  >>> from backend.server.push import get_notifier, NotificationPayload
  >>> notifier = get_notifier()
  >>> notifier.send_to_user(user_id=1, payload=NotificationPayload(
  ...     title="同步完成", body="國泰銀行抓到 23 筆", data={"deep_link": "/sync"},
  ... ))
"""
from __future__ import annotations

from backend.server.push.base import (
    NotificationPayload,
    Notifier,
    NotifyResult,
    PushTarget,
)
from backend.server.push.registry import get_notifier, reset_notifier_cache

__all__ = [
    "NotificationPayload",
    "Notifier",
    "NotifyResult",
    "PushTarget",
    "get_notifier",
    "reset_notifier_cache",
]
