"""驗證 persist_hsbc 把 HSBC API card_detail.details[] 的「Last Statement Amount /
Last Payment Amount / Last Payment Date」抽進 cards 表新欄.

2026-06-20 (本期應繳 1,320,961 bug 修):
   HSBC API 不在 card_billed_txns 給 bill_date, 原 db_facade derive 把整
   12 個月歷史消費 SUM 成「本期應繳」, 飆到 1.3M (正確是 71,032).

   修法 = 走 HSBC card_detail.details[] 直接抽 native 欄位寫進 cards 表,
   db_facade _bill_summary_for_cards 後置 overlay 蓋過 derive 結果.

case A: 完整 payload → cards.bill_due_amount=71032, last_payment_amount=622,
        last_payment_date='2026-06-11'
case B: details[] 沒給 Last Payment → 該欄 NULL (向後相容, 其他卡可能沒繳款史)
case C: details[] 沒給 Last Statement → bill_due_amount NULL (尚未出帳新卡)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.banks.hsbc import _hsbc_card_bill_facts
from backend.core.card_bills import publish_card_bill_facts
from backend.core.persist.hsbc import persist_hsbc
from backend.core.store import BankStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch):
    from backend.core import store as store_mod
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path, raising=True)
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(store_mod.bank_pg, "enabled", lambda: False)
    s = BankStore("hsbc_test")
    yield s
    s.close()


def _hsbc_card_payload(tail: str = "7034", details: list[dict] | None = None) -> dict:
    """產一個極簡 HSBC collect() payload (cards + card_detail)."""
    masked = f"4029-****-****-{tail}"
    receipt = {
        "identity": masked,
        "start": "2025-09-01",
        "end": "2026-08-31",
        "status": "explicit_empty",
        "pages": 1,
        "rows": 0,
    }
    return {
        "cards": [
            {
                "id": f"id-{tail}",
                "maskedCardNumber": masked,
                "name": "滙豐旅人無限卡",
                "cardType": "Credit",
                # HSBC API 真實 outstandingBalance 是 numeric 不是字串
                # (對齊 prod card_summary daily_metric: "outstanding": 116458.0)
                "outstandingBalance": 116458.0,
                "paymentDueDate": "05-06-2026",
                "statementDate": "18-05-2026",
                "cardStatusDisplay": "ACTIVATED",
            }
        ],
        "card_detail": {
            masked: {
                "card_id": f"id-{tail}",
                "masked": masked,
                "posted": [],
                "posted_receipt": receipt.copy(),
                "unposted": [],
                "unposted_ok": True,
                "detail": {
                    "details": details
                    or [
                        {"key": "Credit Limit", "value": "1,500,000 TWD"},
                        {"key": "Last Statement Date", "value": "18 May 2026"},
                        {"key": "Last Statement Amount", "value": "71,032 TWD"},
                        {"key": "Last Payment Amount", "value": "622 TWD"},
                        {"key": "Last Payment Date", "value": "11 Jun 2026"},
                    ],
                },
            }
        },
        "history_coverage": {
            "version": 1,
            "mode": "full",
            "domains": [{
                "domain": "card_billed_transactions",
                "expected": [{
                    "identity": masked,
                    "start": receipt["start"],
                    "end": receipt["end"],
                }],
                "windows": [receipt.copy()],
            }],
        },
    }


def test_hsbc_unmatchable_pending_replacement_through_dispatcher_is_durable(store):
    from copy import deepcopy
    from backend.core.persist import persist_collected

    out = _hsbc_card_payload()
    card_no, detail = next(iter(out["card_detail"].items()))
    detail["unposted"] = [{"description": "OLD SHOP", "transactionDate": "2026-07-01T00:00",
                           "ntdAmount": "100 TWD", "isPositive": True, "isForeign": False}]
    persist_collected("hsbc", out, store)
    store.conn.execute("UPDATE card_pending_txns SET category='old category', subcategory='old sub', "
                       "description_overwrite='old note', tags_overwrite='[\"old\"]', auto_excluded=1, "
                       "splits_overwrite='[{\"amount\":40},{\"amount\":60}]'")
    store.conn.commit()
    updated = deepcopy(out)
    updated["card_detail"][card_no]["unposted"][0].update(
        description="NEW SHOP", postedDate="2026-07-03T00:00")
    rules = [{"pattern": "NEW SHOP", "category": "餐飲", "subcategory": "其他"}]
    assert persist_collected("hsbc", updated, store, rules=rules)["card_unbilled"] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM card_pending_txns").fetchone()[0] == 0
    rows = store.conn.execute("SELECT * FROM card_billed_txns").fetchall()
    assert len(rows) == 1 and rows[0]["description"] == "NEW SHOP"
    assert (rows[0]["category"], rows[0]["subcategory"], rows[0]["description_overwrite"],
            rows[0]["tags_overwrite"], rows[0]["auto_excluded"], rows[0]["splits_overwrite"]) == (
                "餐飲", "其他", None, None, 0, None)
    target_id = rows[0]["id"]
    persist_collected("hsbc", updated, store, rules=rules)
    assert [r[0] for r in store.conn.execute("SELECT id FROM card_billed_txns")] == [target_id]
    assert store.conn.execute("SELECT SUM(amount) FROM card_billed_txns").fetchone()[0] == 100
    observer = BankStore("hsbc_test")
    try:
        assert observer.conn.execute("SELECT COUNT(*) FROM card_pending_txns").fetchall()[0][0] == 0
        assert observer.conn.execute("SELECT id, description FROM card_billed_txns").fetchall()[0][0] == target_id
    finally:
        observer.close()


@pytest.mark.parametrize("route", ["direct", "collected", "direct_commit_false"])
@pytest.mark.parametrize("rollback_fault", [None, "before", "after"])
def test_hsbc_outer_operation_error_survives_rollback_fault(store, route, rollback_fault):
    import sqlite3
    from copy import deepcopy

    from backend.core.error_codes import exception_error_id
    from backend.core.persist import persist_collected

    class FaultConnection(sqlite3.Connection):
        operation_error = None
        rollback_calls = 0
        commit_calls = 0

        def execute(self, sql, parameters=()):
            try:
                return super().execute(sql, parameters)
            except sqlite3.IntegrityError as error:
                self.operation_error = error
                raise

        def rollback(self):
            self.rollback_calls += 1
            if rollback_fault == "before":
                raise sqlite3.OperationalError("synthetic rollback failure")
            super().rollback()
            if rollback_fault == "after":
                raise sqlite3.OperationalError("synthetic rollback failure")

        def commit(self):
            self.commit_calls += 1
            super().commit()

    store.conn.close()
    store.conn = sqlite3.connect(store.db_path, factory=FaultConnection)
    store.conn.row_factory = sqlite3.Row
    store.conn.execute(
        "CREATE TEMP TRIGGER fail_native_metric BEFORE INSERT ON daily_metrics "
        "BEGIN SELECT RAISE(ABORT, 'synthetic native INSERT failure'); END"
    )
    sqlite3.Connection.commit(store.conn)
    names = ("_new_billed_ids", "_current_billed_ids", "_adopted_pending_raw_keys",
             "_unsafe_pending_overlay_identities")
    before = {name: deepcopy(getattr(store, name)) for name in names}

    with pytest.raises(Exception) as caught:
        if route == "collected":
            persist_collected("hsbc", _hsbc_card_payload(), store)
        else:
            persist_hsbc(_hsbc_card_payload(), store, commit=route != "direct_commit_false")

    assert caught.value is store.conn.operation_error
    assert type(caught.value) is sqlite3.IntegrityError
    assert str(caught.value) == "synthetic native INSERT failure"
    assert exception_error_id(caught.value, bank="hsbc") == "SYS-08-165-001"
    assert store.conn.commit_calls == 0
    assert store.conn.rollback_calls == (0 if route == "direct_commit_false" else 1)
    assert {name: getattr(store, name) for name in names} == before
    if route == "direct_commit_false" or rollback_fault == "before":
        # No successful rollback is claimed when cleanup failed before SQLite ran.
        assert store.conn.in_transaction
        assert store.conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1
        sqlite3.Connection.rollback(store.conn)
    else:
        assert not store.conn.in_transaction
    for table in ("cards", "card_billed_txns", "card_pending_txns", "daily_metrics", "sync_log"):
        assert store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert store.latest_card_transaction_dates() == {}


def test_hsbc_collector_requires_complete_per_card_coverage():
    out = _hsbc_card_payload()
    out["cards"].append({
        "id": "id-9999",
        "maskedCardNumber": "4029-****-****-9999",
        "paymentDueDate": "05 Jun 2026",
    })

    published = {}
    publish_card_bill_facts(published, _hsbc_card_bill_facts(out))

    assert published == {"card_bill_facts_ok": False, "card_bill_facts": []}


def test_hsbc_collector_subtracts_only_same_cycle_payment():
    facts = _hsbc_card_bill_facts(_hsbc_card_payload())

    assert len(facts) == 1
    assert facts[0] is not None
    assert facts[0]["remaining_due"] == 70410.0


def test_hsbc_persist_extracts_last_statement_amount(store):
    """case A: 完整 HSBC payload → cards.bill_due_amount=71032 (本期應繳官方值)."""
    persist_hsbc(_hsbc_card_payload(), store)

    row = store.conn.execute(
        """SELECT card_no, credit_limit, used_credit,
                  bill_due_amount, last_payment_amount, last_payment_date,
                  statement_close_date, payment_due_date
             FROM cards WHERE card_no = ?""",
        ("4029-****-****-7034",),
    ).fetchone()

    assert row is not None
    assert row["credit_limit"] == 1500000.0
    assert row["used_credit"] == 116458.0
    # 核心斷言: HSBC API「Last Statement Amount」71,032 寫進 cards.bill_due_amount
    # (不是 1,320,961 那個 card_billed_txns SUM 出來的 derive 假象)
    assert row["bill_due_amount"] == 71032.0
    assert row["last_payment_amount"] == 622.0
    assert row["last_payment_date"] == "2026-06-11"
    assert row["statement_close_date"] == "2026-05-18"
    assert row["payment_due_date"] == "2026-06-05"


def test_hsbc_persist_preserves_valid_early_card_dates(store):
    payload = _hsbc_card_payload()
    payload["cards"][0]["paymentDueDate"] = "01-01-0001"
    payload["cards"][0]["statementDate"] = "31-12-9999"
    payload["card_detail"]["4029-****-****-7034"]["detail"] = {"details": []}

    persist_hsbc(payload, store)

    row = store.conn.execute("SELECT payment_due_date, statement_close_date FROM cards").fetchone()
    assert tuple(row) == ("0001-01-01", "9999-12-31")


def test_hsbc_persist_handles_missing_last_payment(store):
    """case B: details 沒給 Last Payment → last_payment_amount/date 都 NULL.

    這是「新卡尚未繳款史」情境, 不該寫 0 (會被誤認「沒繳款=逾期 0 元」).
    """
    details = [
        {"key": "Credit Limit", "value": "1,500,000 TWD"},
        {"key": "Last Statement Amount", "value": "71,032 TWD"},
        # 故意不給 Last Payment Amount / Date
    ]
    persist_hsbc(_hsbc_card_payload(details=details), store)

    row = store.conn.execute(
        """SELECT bill_due_amount, last_payment_amount, last_payment_date
             FROM cards WHERE card_no = ?""",
        ("4029-****-****-7034",),
    ).fetchone()
    assert row["bill_due_amount"] == 71032.0
    assert row["last_payment_amount"] is None
    assert row["last_payment_date"] is None


def test_hsbc_persist_handles_missing_last_statement(store):
    """case C: details 沒給 Last Statement Amount → bill_due_amount NULL.

    NULL 進 db_facade _bill_summary_for_cards 會走 derive fallback
    (不該 hardcode 0, 否則新出帳 cycle 一開始顯示「本期應繳 $0」誤導).
    """
    details = [
        {"key": "Credit Limit", "value": "1,500,000 TWD"},
        {"key": "Last Payment Amount", "value": "622 TWD"},
        {"key": "Last Payment Date", "value": "11 Jun 2026"},
    ]
    persist_hsbc(_hsbc_card_payload(details=details), store)

    row = store.conn.execute(
        """SELECT bill_due_amount, last_payment_amount, last_payment_date
             FROM cards WHERE card_no = ?""",
        ("4029-****-****-7034",),
    ).fetchone()
    assert row["bill_due_amount"] is None
    assert row["last_payment_amount"] == 622.0
    assert row["last_payment_date"] == "2026-06-11"


def test_hsbc_persist_upsert_preserves_existing_native_fields(store):
    """同 card 兩次 sync, 第二次 details 沒給 → 不該被 NULL 沖掉 (COALESCE 防呼).

    場景: 本期繳款後再 sync, HSBC 暫時不回 Last Payment (極端). 老值應該保留.
    """
    # 1st sync: 完整
    persist_hsbc(_hsbc_card_payload(), store)
    # 2nd sync: 只給 Credit Limit
    details = [{"key": "Credit Limit", "value": "1,500,000 TWD"}]
    persist_hsbc(_hsbc_card_payload(details=details), store)

    row = store.conn.execute(
        """SELECT bill_due_amount, last_payment_amount, last_payment_date
             FROM cards WHERE card_no = ?""",
        ("4029-****-****-7034",),
    ).fetchone()
    # 老值都保留 (COALESCE 防呼)
    assert row["bill_due_amount"] == 71032.0
    assert row["last_payment_amount"] == 622.0
    assert row["last_payment_date"] == "2026-06-11"


def test_hsbc_collector_nets_cashback_from_bank_outstanding_balance():
    """Statement 3,701, paid 1,529, cashback 2,172: balance equals unbilled → settled."""
    out = _hsbc_card_payload()
    out["cards"][0]["outstandingBalance"] = 534.0
    detail = next(iter(out["card_detail"].values()))["detail"]
    detail["details"] = [
        {"key": "Last Statement Date", "value": "18 Sep 2026"},
        {"key": "Last Statement Amount", "value": "3,701 TWD"},
        {"key": "Last Payment Amount", "value": "1,529 TWD"},
        {"key": "Last Payment Date", "value": "07 Oct 2026"},
        {"key": "Unbilled Transactions", "value": "534 TWD"},
    ]

    assert _hsbc_card_bill_facts(out)[0]["remaining_due"] == 0.0

    out["cards"][0]["outstandingBalance"] = 1534.0  # 1,000 of the statement still unpaid
    assert _hsbc_card_bill_facts(out)[0]["remaining_due"] == 1000.0
