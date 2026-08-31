"""NoOpNotifier — default provider, 不送任何通知。

開源 user clone 下來 PUSH_PROVIDER 沒設 → 用這個 → backend boot 正常,
DB 仍會收 token 註冊 (frontend 不會壞), 只是不送出去。
未來 user 想啟用就改 env, 之前累積的 token 立刻 active。
"""
from __future__ import annotations

import logging

from backend.server.push.base import (
    NotificationPayload,
    NotifyResult,
    PushTarget,
)

logger = logging.getLogger(__name__)


class NoOpNotifier:
    name = "none"

    def send_to_user(
        self, user_id: int, payload: NotificationPayload,
    ) -> NotifyResult:
        # 不打 repo 不打網路 — 純 noop, 只 debug log 留痕跡
        logger.debug(
            "[push:none] user_id=%s category=%s skipped",
            user_id, payload.category,
        )
        return NotifyResult()

    def send_to_token(
        self, target: PushTarget, payload: NotificationPayload,
    ) -> NotifyResult:
        logger.debug(
            "[push:none] target=%s/%s category=%s skipped",
            target.provider, target.platform, payload.category,
        )
        return NotifyResult()
