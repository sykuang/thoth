"""WebhookNotifier — POST 通知到 generic HTTP endpoint (Discord / Slack / 自架).

設計：
  * 不關心對方是誰 — 純 POST JSON
  * Token 欄存的就是 webhook URL
  * Discord webhook 直接吃 `{content, embeds}` shape；這裡轉 Discord format 一次
  * 失敗不 raise — 包 NotifyResult, 由 caller logging
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from backend.server.push import repo
from backend.server.push.base import (
    NotificationPayload,
    NotifyResult,
    PushTarget,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 10.0


class WebhookNotifier:
    name = "webhook"

    def __init__(self, timeout_s: float = DEFAULT_TIMEOUT_S):
        self._timeout = timeout_s

    def send_to_user(
        self, user_id: int, payload: NotificationPayload,
    ) -> NotifyResult:
        targets = repo.list_active_for_user(user_id, provider=self.name)
        if not targets:
            return NotifyResult()
        result = NotifyResult()
        with httpx.Client(timeout=self._timeout) as client:
            for t in targets:
                r = self._post_one(client, t, payload)
                self._merge(result, r)
        return result

    def send_to_token(
        self, target: PushTarget, payload: NotificationPayload,
    ) -> NotifyResult:
        result = NotifyResult()
        with httpx.Client(timeout=self._timeout) as client:
            r = self._post_one(client, target, payload)
            self._merge(result, r)
        return result

    # -----------------------------------------------------------------
    # internals
    # -----------------------------------------------------------------

    def _post_one(
        self,
        client: httpx.Client,
        target: PushTarget,
        payload: NotificationPayload,
    ) -> NotifyResult:
        """POST 一條到 target.token (= webhook URL)。"""
        url = target.token
        body = self._format(url, payload)
        try:
            resp = client.post(url, json=body)
        except httpx.RequestError as e:
            return NotifyResult(
                failed_count=1,
                errors=[(_short(url), f"{type(e).__name__}: {e}")],
            )
        if 200 <= resp.status_code < 300:
            repo.touch(provider=self.name, token=url)
            return NotifyResult(delivered_count=1)
        # 4xx 永久 (webhook deleted, malformed) — invalidate
        if 400 <= resp.status_code < 500:
            return NotifyResult(
                failed_count=1,
                invalid_tokens=[url],
                errors=[(_short(url), f"HTTP {resp.status_code}")],
            )
        # 5xx 暫時 — failed_count 但不 invalidate
        return NotifyResult(
            failed_count=1,
            errors=[(_short(url), f"HTTP {resp.status_code}")],
        )

    def _format(self, url: str, payload: NotificationPayload) -> dict[str, Any]:
        """根據 URL host 決定 payload shape。

        Discord / Slack 都吃 `{content}` 或 `{text}`，這裡寬容 fallback。
        """
        title = payload.title
        body = payload.body
        text = f"**{title}**\n{body}" if title else body
        if "discord.com" in url or "discordapp.com" in url:
            return {"content": text, "username": "Thoth"}
        if "hooks.slack.com" in url:
            return {"text": text}
        # generic — 把整個 payload dict 化, 對方自己解
        return {
            "title": title,
            "body": body,
            "data": payload.data,
            "category": payload.category,
        }

    def _merge(self, into: NotifyResult, other: NotifyResult) -> None:
        # NotifyResult 是 frozen dataclass — 走 object.__setattr__ 繞過 frozen
        object.__setattr__(into, "delivered_count",
                           into.delivered_count + other.delivered_count)
        object.__setattr__(into, "failed_count",
                           into.failed_count + other.failed_count)
        into.invalid_tokens.extend(other.invalid_tokens)
        into.errors.extend(other.errors)


def _short(url: str) -> str:
    """log 用 — webhook URL 含 secret token, log 只留 host + 後 8 字。"""
    if len(url) <= 40:
        return url
    return url[:30] + "…" + url[-8:]
