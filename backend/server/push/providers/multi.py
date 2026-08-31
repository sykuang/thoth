"""MultiNotifier — fanout to several providers at once.

PUSH_PROVIDER=multi 時用，由 PUSH_MULTI_PROVIDERS env (CSV) 決定 fanout list:
  PUSH_MULTI_PROVIDERS=apns,webhook

每個 provider 仍 lazy-load (缺 dep 就 skip + log warning，不擋整體)，
回傳 merged NotifyResult。
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from backend.server.push.base import (
    NotificationPayload,
    NotifyResult,
    PushTarget,
)

if TYPE_CHECKING:
    from backend.server.push.base import Notifier

logger = logging.getLogger(__name__)


class MultiNotifier:
    name = "multi"

    def __init__(self) -> None:
        names_csv = os.environ.get("PUSH_MULTI_PROVIDERS", "").strip()
        names = [n.strip().lower() for n in names_csv.split(",") if n.strip()]
        if not names:
            raise RuntimeError(
                "PUSH_PROVIDER=multi 必須設 PUSH_MULTI_PROVIDERS (CSV)",
            )
        if "multi" in names:
            raise RuntimeError("PUSH_MULTI_PROVIDERS 不能包含 'multi' (會無限迴圈)")
        self._children: list["Notifier"] = []
        for n in names:
            try:
                self._children.append(_build_single(n))
            except Exception as exc:
                logger.warning(
                    "[push:multi] provider load failed provider=%s error_type=%s",
                    n, type(exc).__name__,
                )

    def send_to_user(
        self, user_id: int, payload: NotificationPayload,
    ) -> NotifyResult:
        merged = NotifyResult()
        for child in self._children:
            try:
                r = child.send_to_user(user_id, payload)
            except Exception as exc:
                logger.warning(
                    "[push:multi] child user dispatch failed provider=%s error_type=%s",
                    child.name, type(exc).__name__,
                )
                continue
            _merge(merged, r)
        return merged

    def send_to_token(
        self, target: PushTarget, payload: NotificationPayload,
    ) -> NotifyResult:
        # 只 dispatch 給對應 provider name 的 child
        merged = NotifyResult()
        for child in self._children:
            if child.name != target.provider:
                continue
            try:
                r = child.send_to_token(target, payload)
            except Exception as exc:
                logger.warning(
                    "[push:multi] child dispatch failed provider=%s error_type=%s",
                    child.name, type(exc).__name__,
                )
                continue
            _merge(merged, r)
        return merged


def _build_single(name: str) -> "Notifier":
    """子 provider builder — 跟 registry._build 一樣但不接受 multi/recursion."""
    if name in ("", "none", "noop"):
        from backend.server.push.providers.none import NoOpNotifier
        return NoOpNotifier()
    if name == "apns":
        from backend.server.push.providers.apns import ApnsNotifier
        return ApnsNotifier()
    if name == "webhook":
        from backend.server.push.providers.webhook import WebhookNotifier
        return WebhookNotifier()
    if name == "expo":
        from backend.server.push.providers.expo import ExpoPushProvider
        return ExpoPushProvider()
    raise ValueError(f"未知的 multi child provider: {name!r}")


def _merge(into: NotifyResult, other: NotifyResult) -> None:
    object.__setattr__(into, "delivered_count",
                       into.delivered_count + other.delivered_count)
    object.__setattr__(into, "failed_count",
                       into.failed_count + other.failed_count)
    into.invalid_tokens.extend(other.invalid_tokens)
    into.errors.extend(other.errors)
