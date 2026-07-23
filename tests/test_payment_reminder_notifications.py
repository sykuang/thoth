"""Daily payment reminder push notifications.

Coverage:
  * daily scheduler job sends one aggregated push for current reminders
  * same reminder is only pushed once per local day (dedupe table)
  * no reminders => no push and no dedupe rows
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.server.push.base import NotificationPayload, NotifyResult
from backend.server.push.registry import reset_notifier_cache


def test_scheduler_registers_global_daily_payment_reminder_job(monkeypatch):
    """Scheduler boot 會註冊一個全域每日提醒 job, 不依賴自動同步 preference。"""
    from backend.server import scheduler

    s = scheduler.get_scheduler()
    s.remove_all_jobs()
    monkeypatch.setenv("PAYMENT_REMINDER_HOUR", "8")
    monkeypatch.setenv("PAYMENT_REMINDER_MINUTE", "30")

    scheduler.add_or_replace_payment_reminder_sweep()

    jobs = {j.id: j for j in s.get_jobs()}
    assert "payment-reminders-daily" in jobs
    assert jobs["payment-reminders-daily"].name == "daily-payment-reminders"
    s.remove_all_jobs()


def test_today_plus_uses_taipei_local_date(monkeypatch):
    """CI runner UTC 日期落後台北時，測試 helper 要跟 dispatcher 的 local day 對齊。"""
    from datetime import date as real_date

    from backend.server import payment_reminder_notifications as prn

    monkeypatch.setattr(prn, "_local_date", lambda _tz: real_date(2026, 6, 30))

    assert prn._today_plus(0) == real_date(2026, 6, 30)
    assert prn._today_plus(1) == real_date(2026, 7, 1)


def test_dashboard_reminders_endpoint_uses_taipei_local_day(client, monkeypatch):
    """Dashboard endpoint 不可用 runner local date；要跟 payment reminder business day 一致。"""
    from datetime import date as real_date

    from backend.server import payment_reminder_notifications as prn
    from backend.server.routers import auto_debit

    class _UtcRunnerDate(real_date):
        @classmethod
        def today(cls):
            return cls(2026, 6, 29)

    monkeypatch.setattr(auto_debit, "date", _UtcRunnerDate)
    monkeypatch.setattr(auto_debit, "_local_date", lambda _tz: real_date(2026, 6, 30))
    monkeypatch.setattr(prn, "_local_date", lambda _tz: real_date(2026, 6, 30))

    user_id, token = _register(client, email="endpoint-tz-paypush@palace.example")
    _setup_bank_data("cathay", user_id, cards=[{
        "number": "****7035", "name": "國泰世界卡",
        "payment_due_date": prn._today_plus(0).isoformat(),
        "bill_due_amount": 30000.0,
    }])

    r = client.get("/cards/auto-debit/reminders", headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 200, r.text
    reminders = r.json()
    assert len(reminders) == 1
    assert reminders[0]["payment_due_date"] == "2026-06-30"
    assert reminders[0]["days_until_due"] == 0


@dataclass
class _CallRecord:
    user_id: int
    title: str
    body: str
    category: str | None
    data: dict[str, Any]


class _FakeNotifier:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[_CallRecord] = []

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


def _register(client, email: str = "paypush@palace.example") -> tuple[int, str]:
    r = client.post("/auth/register", json={"email": email, "password": "secret-pw"})
    assert r.status_code == 201, r.text
    body = r.json()
    return body["user_id"], body["token"]


def _setup_bank_data(bank: str, user_id: int, *, accounts=None, cards=None) -> None:
    from backend.core.store import BankStore
    store = BankStore(bank, user_id=user_id)
    if accounts:
        store.upsert_accounts(accounts)
    if cards:
        store.upsert_cards(cards)
    store.close()


def test_daily_payment_reminder_sends_aggregated_push_once(client, monkeypatch):
    """兩筆 dashboard reminders → 一則聚合 push, 並記錄今日 dedupe rows."""
    from backend.server import payment_reminder_notifications as prn

    fake = _FakeNotifier()
    monkeypatch.setattr("backend.server.push.registry.get_notifier", lambda: fake)
    monkeypatch.setattr("backend.server.push.get_notifier", lambda: fake)
    reset_notifier_cache()

    user_id, _token = _register(client)
    _setup_bank_data("ctbc", user_id, cards=[{
        "number": "****7016", "name": "中信慶豐卡",
        "payment_due_date": prn._today_plus(1).isoformat(),
        "bill_due_amount": 15000.0,
    }])
    _setup_bank_data("cathay", user_id, cards=[{
        "number": "****7035", "name": "國泰世界卡",
        "payment_due_date": prn._today_plus(0).isoformat(),
        "bill_due_amount": 30000.0,
    }])

    result = prn.dispatch_daily_payment_reminders(user_id=user_id, tz="Asia/Taipei")

    assert result["sent"] == 2
    assert result["skipped"] == 0
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call.user_id == user_id
    assert call.title == "繳費提醒"
    assert "國泰世華・國泰世界卡 今天到期" in call.body
    assert "另 1 筆" in call.body
    assert call.category == "payment_reminder"
    assert call.data["deep_link"] == "/(tabs)/cards"
    assert call.data["kind"] == "payment_reminder"
    assert call.data["count"] == "2"

    # 同一天再跑一次不該重推
    result2 = prn.dispatch_daily_payment_reminders(user_id=user_id, tz="Asia/Taipei")
    assert result2["sent"] == 0
    assert result2["skipped"] == 2
    assert len(fake.calls) == 1


def test_daily_payment_reminder_no_rows_when_no_reminders(client, monkeypatch):
    """沒有 dashboard reminders 時不推、不 claim dedupe."""
    from backend.server import payment_reminder_notifications as prn

    fake = _FakeNotifier()
    monkeypatch.setattr("backend.server.push.registry.get_notifier", lambda: fake)
    monkeypatch.setattr("backend.server.push.get_notifier", lambda: fake)
    reset_notifier_cache()

    user_id, _token = _register(client, email="empty-paypush@palace.example")
    _setup_bank_data("ctbc", user_id, cards=[{
        "number": "****7016", "name": "中信慶豐卡",
        "payment_due_date": prn._today_plus(7).isoformat(),
        "bill_due_amount": 15000.0,
    }])

    result = prn.dispatch_daily_payment_reminders(user_id=user_id, tz="Asia/Taipei")

    assert result == {"sent": 0, "skipped": 0, "total": 0}
    assert fake.calls == []


def test_daily_payment_reminder_sweep_covers_users_without_auto_sync(client, monkeypatch):
    """每日提醒是獨立全 user sweep；沒設自動同步 preference 也會收到。"""
    from backend.server import payment_reminder_notifications as prn

    fake = _FakeNotifier()
    monkeypatch.setattr("backend.server.push.registry.get_notifier", lambda: fake)
    monkeypatch.setattr("backend.server.push.get_notifier", lambda: fake)
    reset_notifier_cache()

    user_id, _token = _register(client, email="sweep-paypush@palace.example")
    _setup_bank_data("ctbc", user_id, cards=[{
        "number": "****7016", "name": "中信慶豐卡",
        "payment_due_date": prn._today_plus(1).isoformat(),
        "bill_due_amount": 15000.0,
    }])

    result = prn.dispatch_daily_payment_reminders_for_all_users(tz="Asia/Taipei")

    assert result["users"] == 1
    assert result["sent"] == 1
    assert len(fake.calls) == 1
    assert fake.calls[0].user_id == user_id
