"""Push notification abstractions: Notifier Protocol + payload dataclasses.

Push notification abstractions（Notifier Protocol + payload dataclasses）。

設計：
  * `NotificationPayload`: business 端傳「要送什麼」(title / body / data)
  * `PushTarget`: 一個 device token + 它的 provider type + platform metadata
  * `Notifier`: provider 抽象介面，所有 dispatcher 都實作這個 Protocol
  * `NotifyResult`: 一個 send 動作的結果（給 caller / logging / token 失效自動清理用）

Notifier 不直接拿 token list — 它呼叫 `repo.list_active_tokens(user_id)`
自己撈，這樣可以做 provider-specific filtering (例：apns provider 只撈 platform='ios')。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class NotificationPayload:
    """Business 端定義「要送什麼」。Provider-agnostic。"""

    title: str
    body: str
    # Deep-link / arbitrary 結構化 payload；接收端 frontend 看 `data.deep_link` 跳頁
    data: dict[str, Any] = field(default_factory=dict)
    # iOS-specific：app badge 數字（None = 不動）
    badge: int | None = None
    # iOS-specific：通知音檔，"default" / None / 自訂 .caf 名
    sound: str | None = "default"
    # 通知分類（給 client-side 自訂 action button 用，例 'sync_done', 'bill_due'）
    category: str | None = None


@dataclass(frozen=True)
class PushTarget:
    """單一裝置的 push 目標 — provider + token + metadata。"""

    user_id: int
    provider: str          # 'apns' | 'webhook' | 'fcm' | 'expo' ...
    token: str             # 格式依 provider
    platform: str | None   # 'ios' | 'android' | 'web' | 'desktop' | None
    device_label: str | None = None


@dataclass(frozen=True)
class NotifyResult:
    """一個 send 動作的結果。

    `delivered_count`: 成功送出去的 device 數
    `failed_count`: 失敗的 device 數
    `invalid_tokens`: provider 回 "device unregistered" 等永久錯誤的 token
                      caller (registry / sender) 該把這些 token 從 DB 刪掉
    `errors`: 給 logging 用的 (token_short, error_message) tuple list
    """

    delivered_count: int = 0
    failed_count: int = 0
    invalid_tokens: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)


@runtime_checkable
class Notifier(Protocol):
    """Push provider 介面。所有 dispatcher 實作這個 Protocol。

    實作義務：
      * `send_to_user`: 撈該 user 全部 active tokens → filter 自己能處理的 → 送
      * `name`: 給 logging / metric 用的 provider 名稱 (例 'apns')
      * 不該拋 exception — 內部 catch 並包成 NotifyResult.failed_count
    """

    name: str

    def send_to_user(
        self, user_id: int, payload: NotificationPayload,
    ) -> NotifyResult:
        """送通知給某 user 的所有 active devices。

        實作者注意：
          * 從 repo 撈 token 是實作者的責任 (不從這層傳進來)
          * provider 不會自己撈 token 時 (None provider) 直接回 NotifyResult()
          * `invalid_tokens` 由 caller 拿去 repo.deactivate(token)
        """
        ...

    def send_to_token(
        self, target: PushTarget, payload: NotificationPayload,
    ) -> NotifyResult:
        """送通知給單一 device（測試 / debug 用，主流程走 send_to_user）。"""
        ...
