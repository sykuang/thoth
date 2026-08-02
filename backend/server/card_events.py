"""Card event detection — diff sync snapshots to find new bills + new payments.

L14 (2026-06-23 使用者指示):
  「如果發現 信用卡帳單 或是發現新繳款紀錄也要發通知」

設計:
  * sync 前 snapshot 該 user 全部信用卡的 (bill_due_amount, last_payment_date)
  * sync 後重 snapshot, diff 看哪些 card 有變動
  * 任何「新出帳單」(bill_due_amount 從 0 變正, 或變大)
    → push 一則「{銀行} 新帳單 ${金額}」
  * 任何「新繳款」(last_payment_date 變動)
    → push 一則「{銀行} 已繳款 ${金額}」

跟 sync_done push 並存:
  * sync_done 一定推 (整體完成 / 失敗)
  * 帳單/繳款事件**額外**推 — user 在意的就是這兩件事 (狀態變動 vs 過程資訊)

跨 bank: 一次 sync 一家銀行, snapshot 只看該 bank cards (避免拉跨銀行 cards 抓到別家被誤判).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CardSnapshot:
    """Snapshot of one card's payment-relevant fields at a point in time."""
    bank: str
    card_no: str
    nickname: str | None
    bill_due_amount: float | None  # 本期應繳 (None / 0 / 正數)
    payment_due_date: str | None  # 非 HSBC 整戶帳單 fact identity 的 cycle boundary
    last_payment_amount: float | None  # 最近一次繳款金額
    last_payment_date: str | None  # 最近一次繳款日 ISO


@dataclass(frozen=True)
class CardEvent:
    """A detected change worth notifying the user about."""
    kind: str  # "new_bill" | "new_payment"
    bank: str
    card_no: str | None  # 非 HSBC 的整戶帳單／繳款事件沒有單一卡號
    nickname: str | None
    amount: float  # 帳單金額 or 繳款金額
    date: str | None  # 繳款日 (new_payment only)
    # For new_bill: 之前金額 (推「上次 X → 這次 Y」用)
    prev_amount: float | None = None


def snapshot_cards(*, bank: str, user_id: int) -> list[CardSnapshot]:
    """Snapshot all cards for (bank, user_id) at this moment.

    回 list[CardSnapshot] — 沒卡片或 bank db 不存在回 [].
    用 db_facade.cards.list_cards 避 raw SQL.
    """
    from backend.server.db_facade import db_api
    try:
        cards = db_api.list_cards(bank=bank, user_id=user_id, include_inactive=False)
    except Exception:
        # Bank DB 不存在 / migration race — 給 [] 不要擋 sync
        return []
    out: list[CardSnapshot] = []
    for c in cards:
        out.append(CardSnapshot(
            bank=bank,
            card_no=c.card_no,
            nickname=c.nickname_overwrite or c.name,
            bill_due_amount=_safe_float(c.bill_due_amount),
            payment_due_date=c.payment_due_date,
            last_payment_amount=_safe_float(c.last_payment_amount),
            last_payment_date=c.last_payment_date,
        ))
    return out


def diff_snapshots(
    before: list[CardSnapshot],
    after: list[CardSnapshot],
) -> list[CardEvent]:
    """比對 before/after snapshot, 抓「新帳單」+「新繳款」事件.

    判定邏輯:
      * 新帳單 = before.bill_due_amount 不存在 / 為 0  AND  after.bill_due_amount > 0
                 OR after.bill_due_amount > before.bill_due_amount * 1.05
                 (帳單一旦出爐就是穩定值, 多 5% buffer 防 fx round noise)
      * 新帳單 = 非 HSBC 以銀行層 (due date, amount) fact 比較，HSBC 逐卡比較。
      * 新繳款 = (last_payment_date, last_payment_amount) 事實在 before 不存在；
                 非 HSBC 以銀行層集合比較，HSBC 逐卡比較。

    新卡 (before 沒這張) — HSBC bill > 0 算新帳單；非 HSBC 已存在的
    整戶帳單／繳款不因新增卡或卡號格式改變而重發。
    """
    by_no_before: dict[str, CardSnapshot] = {c.card_no: c for c in before}
    before_shared_bills = {
        (c.bank, c.payment_due_date or "", c.bill_due_amount or 0.0)
        for c in before
        if c.bank != "hsbc" and (c.bill_due_amount or 0.0) > 0
    }
    before_shared_payments = {
        (c.bank, c.last_payment_date, c.last_payment_amount or 0.0)
        for c in before
        if c.bank != "hsbc" and c.last_payment_date and (c.last_payment_amount or 0.0) > 0
    }
    events: list[CardEvent] = []
    seen_shared_bills: set[tuple[str, str, float]] = set()
    seen_shared_payments: set[tuple[str, str, float]] = set()

    for a in after:
        b = by_no_before.get(a.card_no)
        # 新帳單偵測
        a_bill = a.bill_due_amount or 0.0
        b_bill = (b.bill_due_amount or 0.0) if b else 0.0
        shared_bill = a.bank != "hsbc"
        bill_key = (a.bank, a.payment_due_date or "", a_bill)
        if shared_bill:
            same_cycle_amounts = {
                amount
                for bank, due, amount in before_shared_bills
                if bank == a.bank and due == (a.payment_due_date or "")
            }
            is_new_bill = (
                a_bill > 0
                and bill_key not in seen_shared_bills
                and (
                    not same_cycle_amounts
                    or (
                        a_bill not in same_cycle_amounts
                        and a_bill > max(same_cycle_amounts) * 1.05
                    )
                )
            )
        else:
            is_new_bill = (
                a_bill > 0
                and (b is None or b_bill <= 0 or a_bill > b_bill * 1.05)
            )
        if is_new_bill:
            if shared_bill:
                seen_shared_bills.add(bill_key)
            events.append(CardEvent(
                kind="new_bill",
                bank=a.bank,
                card_no=None if shared_bill else a.card_no,
                nickname=None if shared_bill else a.nickname,
                amount=a_bill,
                date=None,
                prev_amount=b_bill if b_bill > 0 else None,
            ))
        # 新繳款偵測
        pay_amt = a.last_payment_amount or 0.0
        if a.last_payment_date and pay_amt > 0:
            shared_payment = a.bank != "hsbc"
            payment_key = (a.bank, a.last_payment_date, pay_amt)
            if shared_payment:
                is_new_payment = (
                    payment_key not in before_shared_payments
                    and payment_key not in seen_shared_payments
                )
            else:
                before_payment = (
                    (b.last_payment_date, b.last_payment_amount or 0.0)
                    if b and b.last_payment_date and (b.last_payment_amount or 0.0) > 0
                    else None
                )
                is_new_payment = (a.last_payment_date, pay_amt) != before_payment
            if is_new_payment:
                if shared_payment:
                    seen_shared_payments.add(payment_key)
                events.append(CardEvent(
                    kind="new_payment",
                    bank=a.bank,
                    card_no=None if shared_payment else a.card_no,
                    nickname=None if shared_payment else a.nickname,
                    amount=pay_amt,
                    date=a.last_payment_date,
                ))
    return events


def _safe_float(v: Any) -> float | None:
    """tolerate Decimal / int / str / None — return float or None."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def mask_card_no(card_no: str) -> str:
    """卡號末四碼: '9000000000357050' → '*5678'. Push body 顯示用."""
    s = (card_no or "").strip()
    if len(s) >= 4:
        return f"*{s[-4:]}"
    return s or "***"
