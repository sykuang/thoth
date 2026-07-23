"""2026-06-23 (Plan A) — sync batch summary notification end-to-end tests.

驗:
  * batch 內 success → 個別 sync_done 不推, 最後 1 個 job 完成才推 1 則 batch summary
  * batch 內 failed → 個別 sync_failed 照推 (失敗不能漏)
  * 已推過 → 不再推 (claim atomic)
  * 全綠 / 部分失敗 / 全失敗 三種 body 文案
  * Card events push 完全不受影響 (actionable, 不合併)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from backend.server import sync_batches_repo, sync_jobs_repo, sync_runner
from backend.server.push.base import NotificationPayload, NotifyResult
from backend.server.push.registry import reset_notifier_cache


@dataclass
class _CallRecord:
    """記錄一次 notifier.send_to_user 呼叫."""
    user_id: int
    title: str
    body: str
    category: str | None
    data: dict[str, Any]


class _FakeNotifier:
    """Spy notifier — 收集 send_to_user 呼叫, 不真打 push."""

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[_CallRecord] = []
        self.payment_calls: list[dict[str, Any]] = []

    def send_to_user(self, user_id: int, payload: NotificationPayload) -> NotifyResult:
        self.calls.append(_CallRecord(
            user_id=user_id,
            title=payload.title,
            body=payload.body,
            category=payload.category,
            data=dict(payload.data or {}),
        ))
        return NotifyResult(delivered_count=1)

    def send_to_token(self, *_args, **_kwargs) -> NotifyResult:  # pragma: no cover
        return NotifyResult()


@pytest.fixture
def fake_notifier(monkeypatch):
    """安插 fake notifier, 攔截 sync_runner 跟 push.registry 的 get_notifier."""
    fake = _FakeNotifier()
    payment_calls: list[dict[str, Any]] = []
    fake.payment_calls = payment_calls
    # sync_runner 內部 from backend.server.push import ... 拿的 get_notifier
    monkeypatch.setattr(
        "backend.server.push.registry.get_notifier", lambda: fake,
    )
    monkeypatch.setattr(
        "backend.server.push.get_notifier", lambda: fake,
    )
    monkeypatch.setattr(
        "backend.server.payment_reminder_notifications.dispatch_daily_payment_reminders",
        lambda *, user_id, tz="Asia/Taipei": payment_calls.append({"user_id": user_id, "tz": tz})
        or {"sent": 0, "skipped": 0, "total": 0},
    )
    reset_notifier_cache()
    yield fake
    reset_notifier_cache()


def _setup_user(client) -> int:
    r = client.post("/auth/register",
                    json={"email": "batchpush@palace.example", "password": "secret-pw"})
    assert r.status_code == 201, r.text
    return r.json()["user_id"]


def _setup_batch(client, *, user_id: int, total_jobs: int, banks: list[str]) -> tuple[int, list[int]]:
    """建一個 batch + 對應數量的 queued jobs (不真跑, 直接寫 DB)."""
    assert len(banks) == total_jobs
    bid = sync_batches_repo.create(
        user_id=user_id, total_jobs=total_jobs,
        kind=sync_batches_repo.KIND_MANUAL_ALL,
    )
    job_ids = [
        sync_jobs_repo.queue(user_id=user_id, bank=b, batch_id=bid)
        for b in banks
    ]
    return bid, job_ids


# ---------------------------------------------------------------------------
# Batch summary push — happy paths
# ---------------------------------------------------------------------------

def test_batch_all_ok_sends_one_aggregated_push(client, fake_notifier):
    """全部成功 → 1 則「N 家完成 · 共 X 筆」, 個別 sync_done 不推."""
    user_id = _setup_user(client)
    bid, jobs = _setup_batch(
        client, user_id=user_id, total_jobs=3,
        banks=["cathay", "ubot", "sinopac"],
    )
    # 前 2 個 mark_done — 還有 in-flight, 不該推
    sync_jobs_repo.mark_done(jobs[0], '{"txn_count": 5}')
    sync_runner._maybe_send_batch_summary(batch_id=bid, user_id=user_id)
    assert fake_notifier.calls == []

    sync_jobs_repo.mark_done(jobs[1], '{"txn_count": 3}')
    sync_runner._maybe_send_batch_summary(batch_id=bid, user_id=user_id)
    assert fake_notifier.calls == []

    # 最後 1 個 — claim 拿到, 推 1 則
    sync_jobs_repo.mark_done(jobs[2], '{"deposit_txn_count": 7, "card_txn_count": 2}')
    sync_runner._maybe_send_batch_summary(batch_id=bid, user_id=user_id)

    assert len(fake_notifier.calls) == 1
    call = fake_notifier.calls[0]
    assert call.title == "同步全部完成"
    assert "3 家完成" in call.body
    assert "17 筆" in call.body  # 5 + 3 + (7+2)
    assert call.category == "sync_all_done"
    assert call.data["deep_link"] == "/(tabs)/cards"
    assert call.data["kind"] == "sync_all_done"
    assert call.data["batch_id"] == str(bid)


def test_batch_all_ok_zero_txn_omits_count(client, fake_notifier):
    """全成功但 0 筆 → body 不含「· 共 N 筆」."""
    user_id = _setup_user(client)
    bid, jobs = _setup_batch(
        client, user_id=user_id, total_jobs=2, banks=["cathay", "ubot"],
    )
    for j in jobs:
        sync_jobs_repo.mark_done(j, "{}")
    sync_runner._maybe_send_batch_summary(batch_id=bid, user_id=user_id)

    assert len(fake_notifier.calls) == 1
    call = fake_notifier.calls[0]
    assert call.body == "2 家完成"


def test_batch_partial_failure_lists_failed_banks(client, fake_notifier):
    """部分失敗 → 「N/M 家完成 · 失敗: 國泰、中信」."""
    user_id = _setup_user(client)
    bid, jobs = _setup_batch(
        client, user_id=user_id, total_jobs=4,
        banks=["cathay", "ubot", "ctbc", "sinopac"],
    )
    sync_jobs_repo.mark_done(jobs[0], '{"txn_count": 10}')  # cathay ok
    sync_jobs_repo.mark_failed(jobs[1], "login timeout")     # ubot fail
    sync_jobs_repo.mark_done(jobs[2], '{"txn_count": 5}')   # ctbc... wait, ctbc is failed in body
    sync_jobs_repo.mark_failed(jobs[3], "captcha")           # sinopac fail
    sync_runner._maybe_send_batch_summary(batch_id=bid, user_id=user_id)

    assert len(fake_notifier.calls) == 1
    call = fake_notifier.calls[0]
    assert call.title == "同步全部完成"
    assert "2/4" in call.body
    # _BANK_LABELS: ubot=聯邦銀行, sinopac=永豐銀行
    assert "聯邦銀行" in call.body
    assert "永豐銀行" in call.body
    assert call.category == "sync_all_done"
    assert call.data["kind"] == "sync_all_done"


def test_batch_partial_failure_truncates_long_list(client, fake_notifier):
    """失敗 > 3 家 → 「A、B、C 等 N 家」."""
    user_id = _setup_user(client)
    banks = ["cathay", "ubot", "hsbc", "ctbc", "sinopac"]
    bid, jobs = _setup_batch(
        client, user_id=user_id, total_jobs=5, banks=banks,
    )
    # 1 個 ok, 4 個 fail
    sync_jobs_repo.mark_done(jobs[0], '{"txn_count": 1}')
    for j in jobs[1:]:
        sync_jobs_repo.mark_failed(j, "boom")
    sync_runner._maybe_send_batch_summary(batch_id=bid, user_id=user_id)

    assert len(fake_notifier.calls) == 1
    call = fake_notifier.calls[0]
    assert "1/5" in call.body
    assert "等 4 家" in call.body


def test_batch_all_failed_uses_failed_template(client, fake_notifier):
    """全失敗 → 「同步全部失敗」/「0/N 家成功」."""
    user_id = _setup_user(client)
    bid, jobs = _setup_batch(
        client, user_id=user_id, total_jobs=2, banks=["cathay", "ubot"],
    )
    for j in jobs:
        sync_jobs_repo.mark_failed(j, "anti-bot detected")
    sync_runner._maybe_send_batch_summary(batch_id=bid, user_id=user_id)

    assert len(fake_notifier.calls) == 1
    call = fake_notifier.calls[0]
    assert call.title == "同步全部失敗"
    assert "0/2" in call.body
    assert call.category == "sync_all_failed"
    assert call.data["kind"] == "sync_all_failed"


# ---------------------------------------------------------------------------
# Atomic claim — never double-fire
# ---------------------------------------------------------------------------

def test_batch_summary_does_not_double_fire(client, fake_notifier):
    """已被 claim 過 → 第 2 次呼叫不再推."""
    user_id = _setup_user(client)
    bid, jobs = _setup_batch(
        client, user_id=user_id, total_jobs=1, banks=["cathay"],
    )
    sync_jobs_repo.mark_done(jobs[0], '{"txn_count": 1}')

    sync_runner._maybe_send_batch_summary(batch_id=bid, user_id=user_id)
    assert len(fake_notifier.calls) == 1
    # 再呼一次 — 不該再推 (notified_at 已寫)
    sync_runner._maybe_send_batch_summary(batch_id=bid, user_id=user_id)
    assert len(fake_notifier.calls) == 1


# ---------------------------------------------------------------------------
# Integration via _exec_sync — 模擬 sync_runner 真實 mark_done 完整走一遍
# ---------------------------------------------------------------------------

def test_exec_sync_in_batch_skips_individual_sync_done(client, fake_notifier, monkeypatch):
    """_exec_sync 完成時, batch_id 非 None → 不推 個別 sync_done, 走 batch summary."""
    user_id = _setup_user(client)
    bid, jobs = _setup_batch(
        client, user_id=user_id, total_jobs=2, banks=["cathay", "ubot"],
    )

    # mock crawler dispatch
    monkeypatch.setattr(
        sync_runner, "_dispatch_crawler_and_persist",
        lambda bank, user_id, headless: {"delta": {}, "stats": {}},
    )
    # mock card snapshot/diff 來避真撈 sqlite
    monkeypatch.setattr(
        "backend.server.card_events.snapshot_cards",
        lambda *, bank, user_id: [],
    )
    monkeypatch.setattr(
        "backend.server.card_events.diff_snapshots",
        lambda before, after: [],
    )

    # 跑兩個 job (同步, 不用 thread, 直接呼 _exec_sync)
    sync_runner._exec_sync(jobs[0])
    # 第一個跑完還有 in-flight → 不該推
    assert fake_notifier.calls == []

    sync_runner._exec_sync(jobs[1])
    # 第 2 個跑完 → 1 則 batch summary, 沒有任何個別 sync_done
    assert len(fake_notifier.calls) == 1
    assert fake_notifier.calls[0].title == "同步全部完成"
    assert fake_notifier.calls[0].category == "sync_all_done"
    assert fake_notifier.payment_calls == [
        {"user_id": user_id, "tz": "Asia/Taipei"},
    ]


def test_batch_summary_dispatches_payment_reminders_after_sync(client, fake_notifier, monkeypatch):
    """同步完成後要立刻補跑 payment reminders, 避免 09:00 sweep 早於同步而漏通知."""
    user_id = _setup_user(client)
    bid, jobs = _setup_batch(
        client, user_id=user_id, total_jobs=1, banks=["fubon"],
    )
    monkeypatch.setenv("PAYMENT_REMINDER_TZ", "Asia/Taipei")
    sync_jobs_repo.mark_done(jobs[0], "{}")

    sync_runner._maybe_send_batch_summary(batch_id=bid, user_id=user_id)

    assert fake_notifier.payment_calls == [
        {"user_id": user_id, "tz": "Asia/Taipei"},
    ]


def test_exec_sync_in_batch_still_pushes_individual_failure(client, fake_notifier, monkeypatch):
    """batch 內 job 失敗 → sync_failed 個別推 (失敗不能漏) + batch summary 收尾."""
    user_id = _setup_user(client)
    bid, jobs = _setup_batch(
        client, user_id=user_id, total_jobs=2, banks=["cathay", "ubot"],
    )

    def _fake_dispatch(bank, user_id, headless):
        if bank == "ubot":
            raise RuntimeError("login fail")
        return {"delta": {}, "stats": {}}

    monkeypatch.setattr(sync_runner, "_dispatch_crawler_and_persist", _fake_dispatch)
    monkeypatch.setattr(
        "backend.server.card_events.snapshot_cards",
        lambda *, bank, user_id: [],
    )
    monkeypatch.setattr(
        "backend.server.card_events.diff_snapshots",
        lambda before, after: [],
    )

    sync_runner._exec_sync(jobs[0])
    sync_runner._exec_sync(jobs[1])

    # 預期: ubot 失敗個別推 1 則 + batch summary 1 則 (cathay ok, ubot fail)
    titles = [c.title for c in fake_notifier.calls]
    assert "聯邦銀行 同步失敗" in titles, f"sync_failed missing in {titles}"
    assert "同步全部完成" in titles, f"batch summary missing in {titles}"
    assert len(fake_notifier.calls) == 2


def test_exec_sync_outside_batch_uses_legacy_individual_push(client, fake_notifier, monkeypatch):
    """單支路徑 (batch_id=NULL) → 推個別 sync_done (legacy 行為不變)."""
    user_id = _setup_user(client)
    # batch_id = None
    jid = sync_jobs_repo.queue(user_id=user_id, bank="cathay", batch_id=None)

    monkeypatch.setattr(
        sync_runner, "_dispatch_crawler_and_persist",
        lambda bank, user_id, headless: {"delta": {}, "stats": {}},
    )
    monkeypatch.setattr(
        "backend.server.card_events.snapshot_cards",
        lambda *, bank, user_id: [],
    )
    monkeypatch.setattr(
        "backend.server.card_events.diff_snapshots",
        lambda before, after: [],
    )

    sync_runner._exec_sync(jid)

    # legacy 推「國泰世華 同步完成」, 不該觸發任何 batch summary
    assert len(fake_notifier.calls) == 1
    assert fake_notifier.calls[0].title == "國泰世華 同步完成"
    assert fake_notifier.calls[0].category == "sync_done"
