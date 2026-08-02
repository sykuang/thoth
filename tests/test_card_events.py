"""Tests for card_events: snapshot diff → new_bill / new_payment events.

L14 (2026-06-23 使用者指示):
  「如果發現 信用卡帳單 或是發現新繳款紀錄也要發通知」
"""
from __future__ import annotations

from backend.server.card_events import (
    CardEvent,
    CardSnapshot,
    diff_snapshots,
    mask_card_no,
)


def _snap(
    card_no: str = "9000000000357050",
    bill: float | None = None,
    due: str | None = None,
    pay_amt: float | None = None,
    pay_date: str | None = None,
    nickname: str | None = None,
    bank: str = "cathay",
) -> CardSnapshot:
    return CardSnapshot(
        bank=bank,
        card_no=card_no,
        nickname=nickname,
        bill_due_amount=bill,
        payment_due_date=due,
        last_payment_amount=pay_amt,
        last_payment_date=pay_date,
    )


# ============================================================
# new_bill detection
# ============================================================

class TestNewBill:
    def test_zero_to_positive_emits_new_bill(self) -> None:
        before = [_snap(bill=0.0)]
        after = [_snap(bill=15234.0)]
        events = diff_snapshots(before, after)
        assert len(events) == 1
        assert events[0].kind == "new_bill"
        assert events[0].amount == 15234.0
        assert events[0].prev_amount is None

    def test_none_to_positive_emits_new_bill(self) -> None:
        before = [_snap(bill=None)]
        after = [_snap(bill=8500.5)]
        events = diff_snapshots(before, after)
        assert len(events) == 1
        assert events[0].kind == "new_bill"

    def test_brand_new_card_with_bill_emits(self) -> None:
        # 新辦卡: before 沒這張, after 有且 bill > 0
        before: list[CardSnapshot] = []
        after = [_snap(card_no="9000000000427001", bill=3200.0)]
        events = diff_snapshots(before, after)
        assert len(events) == 1
        assert events[0].kind == "new_bill"

    def test_same_bill_no_event(self) -> None:
        before = [_snap(bill=8000.0)]
        after = [_snap(bill=8000.0)]
        events = diff_snapshots(before, after)
        assert events == []

    def test_minor_fluctuation_below_5pct_no_event(self) -> None:
        # FX rounding noise — 不該 spam
        before = [_snap(bill=10000.0)]
        after = [_snap(bill=10300.0)]  # +3%
        events = diff_snapshots(before, after)
        assert events == []

    def test_significant_increase_above_5pct_emits(self) -> None:
        before = [_snap(bill=10000.0)]
        after = [_snap(bill=12000.0)]  # +20% — 真新增帳單
        events = diff_snapshots(before, after)
        assert len(events) == 1
        assert events[0].kind == "new_bill"
        assert events[0].prev_amount == 10000.0

    def test_bill_decreased_no_event(self) -> None:
        # 還款後帳單變小 — 不算「新帳單」(會被 new_payment 抓)
        before = [_snap(bill=10000.0)]
        after = [_snap(bill=5000.0)]
        events_bills = [e for e in diff_snapshots(before, after) if e.kind == "new_bill"]
        assert events_bills == []

    def test_non_hsbc_shared_bill_is_merged_at_bank_level(self) -> None:
        """整戶帳單複寫到多卡只是一個 source fact，不得推三則卡片帳單。"""
        before = [
            _snap(card_no="1111", bill=0.0),
            _snap(card_no="2222", bill=0.0),
            _snap(card_no="3333", bill=0.0),
        ]
        after = [
            _snap(card_no="1111", bill=27916.0),
            _snap(card_no="2222", bill=27916.0),
            _snap(card_no="3333", bill=27916.0),
        ]

        events = [e for e in diff_snapshots(before, after) if e.kind == "new_bill"]

        assert len(events) == 1
        assert events[0].bank == "cathay"
        assert events[0].card_no is None
        assert events[0].nickname is None
        assert events[0].amount == 27916.0

    def test_hsbc_bills_remain_per_card(self) -> None:
        before = [
            _snap(bank="hsbc", card_no="1111", bill=0.0),
            _snap(bank="hsbc", card_no="2222", bill=0.0),
        ]
        after = [
            _snap(bank="hsbc", card_no="1111", bill=12729.0),
            _snap(bank="hsbc", card_no="2222", bill=12729.0),
        ]

        events = [e for e in diff_snapshots(before, after) if e.kind == "new_bill"]

        assert [event.card_no for event in events] == ["1111", "2222"]

    def test_non_hsbc_same_amount_different_due_dates_remain_distinct(self) -> None:
        before = [
            _snap(card_no="1111", bill=0.0, due="2026-07-05"),
            _snap(card_no="2222", bill=0.0, due="2026-08-05"),
        ]
        after = [
            _snap(card_no="1111", bill=27916.0, due="2026-07-05"),
            _snap(card_no="2222", bill=27916.0, due="2026-08-05"),
        ]

        events = [e for e in diff_snapshots(before, after) if e.kind == "new_bill"]

        assert len(events) == 2
        assert [event.amount for event in events] == [27916.0, 27916.0]

    def test_non_hsbc_new_cycle_emits_when_amount_is_unchanged(self) -> None:
        before = [
            _snap(card_no="1111", bill=27916.0, due="2026-08-05"),
        ]
        after = [
            _snap(card_no="1111", bill=27916.0, due="2026-09-05"),
        ]

        events = [e for e in diff_snapshots(before, after) if e.kind == "new_bill"]

        assert len(events) == 1
        assert events[0].amount == 27916.0


# ============================================================
# new_payment detection
# ============================================================

class TestNewPayment:
    def test_first_payment_emits(self) -> None:
        before = [_snap(pay_amt=None, pay_date=None)]
        after = [_snap(pay_amt=5000.0, pay_date="2026-06-23")]
        events = [e for e in diff_snapshots(before, after) if e.kind == "new_payment"]
        assert len(events) == 1
        assert events[0].amount == 5000.0
        assert events[0].date == "2026-06-23"

    def test_payment_date_changed_emits(self) -> None:
        before = [_snap(pay_amt=3000.0, pay_date="2026-05-23")]
        after = [_snap(pay_amt=4500.0, pay_date="2026-06-23")]
        events = [e for e in diff_snapshots(before, after) if e.kind == "new_payment"]
        assert len(events) == 1
        assert events[0].amount == 4500.0

    def test_same_payment_date_no_event(self) -> None:
        before = [_snap(pay_amt=3000.0, pay_date="2026-06-01")]
        after = [_snap(pay_amt=3000.0, pay_date="2026-06-01")]
        events = [e for e in diff_snapshots(before, after) if e.kind == "new_payment"]
        assert events == []

    def test_payment_amount_zero_no_event(self) -> None:
        # Bank API 偶爾回 amount=0 但 date 有 — 不算真繳款
        before = [_snap(pay_amt=None, pay_date=None)]
        after = [_snap(pay_amt=0.0, pay_date="2026-06-23")]
        events = [e for e in diff_snapshots(before, after) if e.kind == "new_payment"]
        assert events == []

    def test_non_hsbc_shared_payment_is_merged_at_bank_level(self) -> None:
        before = [
            _snap(card_no="1111", pay_amt=3000.0, pay_date="2026-05-23"),
            _snap(card_no="2222", pay_amt=3000.0, pay_date="2026-05-23"),
        ]
        after = [
            _snap(card_no="1111", pay_amt=4500.0, pay_date="2026-06-23"),
            _snap(card_no="2222", pay_amt=4500.0, pay_date="2026-06-23"),
        ]

        events = [e for e in diff_snapshots(before, after) if e.kind == "new_payment"]

        assert len(events) == 1
        assert events[0].bank == "cathay"
        assert events[0].card_no is None
        assert events[0].nickname is None
        assert events[0].amount == 4500.0
        assert events[0].date == "2026-06-23"

    def test_non_hsbc_new_card_does_not_reemit_existing_shared_payment(self) -> None:
        before = [
            _snap(card_no="1111", pay_amt=4500.0, pay_date="2026-06-23"),
        ]
        after = [
            _snap(card_no="1111", pay_amt=4500.0, pay_date="2026-06-23"),
            _snap(card_no="2222", pay_amt=4500.0, pay_date="2026-06-23"),
        ]

        events = [e for e in diff_snapshots(before, after) if e.kind == "new_payment"]

        assert events == []

    def test_non_hsbc_same_day_amount_change_emits(self) -> None:
        before = [_snap(pay_amt=5000.0, pay_date="2026-06-23")]
        after = [_snap(pay_amt=7000.0, pay_date="2026-06-23")]

        events = [e for e in diff_snapshots(before, after) if e.kind == "new_payment"]

        assert len(events) == 1
        assert events[0].amount == 7000.0

    def test_non_hsbc_different_payment_facts_are_not_merged(self) -> None:
        before = [
            _snap(card_no="1111", pay_amt=1000.0, pay_date="2026-05-23"),
            _snap(card_no="2222", pay_amt=1000.0, pay_date="2026-05-23"),
        ]
        after = [
            _snap(card_no="1111", pay_amt=4500.0, pay_date="2026-06-23"),
            _snap(card_no="2222", pay_amt=7000.0, pay_date="2026-06-23"),
        ]

        events = [e for e in diff_snapshots(before, after) if e.kind == "new_payment"]

        assert [(e.date, e.amount) for e in events] == [
            ("2026-06-23", 4500.0),
            ("2026-06-23", 7000.0),
        ]

    def test_hsbc_payments_remain_per_card(self) -> None:
        before = [
            _snap(bank="hsbc", card_no="1111", pay_amt=3000.0, pay_date="2026-05-23"),
            _snap(bank="hsbc", card_no="2222", pay_amt=3000.0, pay_date="2026-05-23"),
        ]
        after = [
            _snap(bank="hsbc", card_no="1111", pay_amt=4500.0, pay_date="2026-06-23"),
            _snap(bank="hsbc", card_no="2222", pay_amt=4500.0, pay_date="2026-06-23"),
        ]

        events = [e for e in diff_snapshots(before, after) if e.kind == "new_payment"]

        assert [e.card_no for e in events] == ["1111", "2222"]

    def test_hsbc_same_day_amount_change_emits_per_card(self) -> None:
        before = [
            _snap(bank="hsbc", card_no="1111", pay_amt=5000.0, pay_date="2026-06-23"),
        ]
        after = [
            _snap(bank="hsbc", card_no="1111", pay_amt=7000.0, pay_date="2026-06-23"),
        ]

        events = [e for e in diff_snapshots(before, after) if e.kind == "new_payment"]

        assert len(events) == 1
        assert events[0].card_no == "1111"
        assert events[0].amount == 7000.0


# ============================================================
# Combined scenarios
# ============================================================

class TestCombined:
    def test_new_bill_and_payment_both_emit(self) -> None:
        # 卡 A 新帳單, 卡 B 新繳款 — 同 sync 各推一則
        before = [
            _snap(card_no="9000000000337001", bill=0.0, pay_amt=2000.0, pay_date="2026-05-01"),
            _snap(card_no="9000000000377001", bill=8000.0, pay_amt=None, pay_date=None),
        ]
        after = [
            _snap(card_no="9000000000337001", bill=15000.0, pay_amt=2000.0, pay_date="2026-05-01"),
            _snap(card_no="9000000000377001", bill=8000.0, pay_amt=3500.0, pay_date="2026-06-23"),
        ]
        events = diff_snapshots(before, after)
        kinds = [(e.kind, e.card_no) for e in events]
        assert ("new_bill", None) in kinds
        assert ("new_payment", None) in kinds
        assert len(events) == 2

    def test_empty_snapshots_no_events(self) -> None:
        assert diff_snapshots([], []) == []

    def test_card_disappeared_no_events(self) -> None:
        # before 有卡, after 沒 (cancelled) — 不該 emit
        before = [_snap(bill=5000.0, pay_amt=3000.0, pay_date="2026-06-01")]
        after: list[CardSnapshot] = []
        assert diff_snapshots(before, after) == []


# ============================================================
# Utility
# ============================================================

class TestMaskCardNo:
    def test_full_card_no(self) -> None:
        assert mask_card_no("9000000000357050") == "*7050"

    def test_short_string(self) -> None:
        assert mask_card_no("12") == "12"

    def test_empty(self) -> None:
        assert mask_card_no("") == "***"
        assert mask_card_no("   ") == "***"

    def test_four_chars(self) -> None:
        assert mask_card_no("7050") == "*7050"


# ============================================================
# CardEvent dataclass sanity
# ============================================================

def test_card_event_is_frozen() -> None:
    """CardEvent / CardSnapshot 都 frozen — 防意外 mutate."""
    import dataclasses
    snap = _snap(bill=1.0)
    event = CardEvent(
        kind="new_bill", bank="cathay", card_no="1234", nickname=None,
        amount=1000.0, date=None,
    )
    assert dataclasses.is_dataclass(snap)
    assert dataclasses.is_dataclass(event)


def test_bank_level_payment_notification_omits_fake_card_label(monkeypatch) -> None:
    from backend.server import sync_runner
    from backend.server.push.base import NotifyResult

    calls = []

    class _Notifier:
        def send_to_user(self, *, user_id, payload):
            calls.append((user_id, payload))
            return NotifyResult(delivered_count=1)

    monkeypatch.setattr("backend.server.push.get_notifier", lambda: _Notifier())
    event = CardEvent(
        kind="new_payment",
        bank="cathay",
        card_no=None,
        nickname=None,
        amount=4500.0,
        date="2026-06-23",
    )

    sync_runner._send_card_event_notification(user_id=1, event=event)

    assert len(calls) == 1
    assert calls[0][1].body == "2026-06-23 繳款 NT$4,500"
    assert "card_no" not in calls[0][1].data


def test_bank_level_bill_notification_omits_fake_card_label(monkeypatch) -> None:
    from backend.server import sync_runner
    from backend.server.push.base import NotifyResult

    calls = []

    class _Notifier:
        def send_to_user(self, *, user_id, payload):
            calls.append((user_id, payload))
            return NotifyResult(delivered_count=1)

    monkeypatch.setattr("backend.server.push.get_notifier", lambda: _Notifier())
    event = CardEvent(
        kind="new_bill",
        bank="ctbc",
        card_no=None,
        nickname=None,
        amount=27916.0,
        date=None,
    )

    sync_runner._send_card_event_notification(user_id=1, event=event)

    assert len(calls) == 1
    assert calls[0][1].body == "本期應繳 NT$27,916"
    assert "card_no" not in calls[0][1].data
