"""Push provider registry: env-driven factory with lazy import.

`PUSH_PROVIDER` env 決定走哪個 provider；缺對應 dep 時給友善錯誤。

Default = `none` — 開源 user 不設 env 也能 boot backend。
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.server.push.base import Notifier


# Module-level cache，避免每次呼叫都重建 (provider 通常持 httpx.Client/ssl context)
_NOTIFIER_CACHE: dict[str, "Notifier"] = {}


def get_notifier() -> "Notifier":
    """根據 `PUSH_PROVIDER` env 回傳 singleton notifier。

    支援值: none (default) / apns / webhook / multi
    """
    name = os.environ.get("PUSH_PROVIDER", "none").lower().strip()
    if name in _NOTIFIER_CACHE:
        return _NOTIFIER_CACHE[name]
    notifier = _build(name)
    _NOTIFIER_CACHE[name] = notifier
    return notifier


def reset_notifier_cache() -> None:
    """測試用 — clear singleton, 讓改 env 立即生效。"""
    _NOTIFIER_CACHE.clear()


def _build(name: str) -> "Notifier":
    if name in ("", "none", "noop", "disabled"):
        from backend.server.push.providers.none import NoOpNotifier
        return NoOpNotifier()

    if name == "apns":
        try:
            from backend.server.push.providers.apns import ApnsNotifier
        except ImportError as e:
            raise RuntimeError(
                f"APNs provider 缺少 dependency ({e})。請執行：\n"
                f"  uv pip install 'thoth[push-apns]'\n"
                f"或在 pyproject.toml 加 pyjwt[crypto] + httpx[http2]",
            ) from e
        return ApnsNotifier()

    if name == "webhook":
        from backend.server.push.providers.webhook import WebhookNotifier
        return WebhookNotifier()

    if name == "expo":
        from backend.server.push.providers.expo import ExpoPushProvider
        return ExpoPushProvider()

    if name == "multi":
        from backend.server.push.providers.multi import MultiNotifier
        return MultiNotifier()

    raise ValueError(
        f"未知的 PUSH_PROVIDER={name!r}。"
        f"合法值: none / apns / webhook / expo / multi",
    )
