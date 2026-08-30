from __future__ import annotations

from datetime import date

from backend.core.store import BankStore


def _txn(account_no: str, when: str) -> dict:
    return {
        "account_no": account_no,
        "datetime": when,
        "desc": "cursor-test",
        "expend": 1,
        "income": 0,
        "balance": 10,
    }


def _card_txn(
    card_no: str,
    when: str,
    *,
    post_date: str | None = None,
    bill_date: str | None = "2026-08-01",
) -> dict:
    return {
        "card_no": card_no,
        "bill_date": bill_date,
        "currency": "TWD",
        "date": when,
        "post_date": post_date,
        "desc": "cursor-test",
        "amount": 100,
    }


def test_latest_twd_transaction_dates_are_per_credential_account(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    account1 = BankStore("cursorbank", user_id=1, source_account_id=101)
    account2 = BankStore("cursorbank", user_id=1, source_account_id=202)
    legacy = BankStore("cursorbank", user_id=1)
    try:
        account1.upsert_twd_txns([
            _txn("acct-a", "2026/07/30 12:00:00"),
            _txn("acct-a", "2026-08-02 08:30:00"),
            _txn("acct-a", "9999-99-99"),
            _txn("acct-b", "2026-07-31"),
            _txn("calendar-bad", "2026-01-31"),
            _txn("calendar-bad", "2026-02-30"),
            _txn("blank-date", ""),
        ])
        assert account2.latest_twd_transaction_dates() == {}
        account2.upsert_twd_txns([_txn("acct-a", "2026-08-20")])

        assert account1.latest_twd_transaction_dates() == {
            "acct-a": date(2026, 8, 2),
            "acct-b": date(2026, 7, 31),
            "calendar-bad": date(2026, 1, 31),
        }
        assert account2.latest_twd_transaction_dates() == {
            "acct-a": date(2026, 8, 20),
        }
        assert legacy.latest_twd_transaction_dates() == {}
    finally:
        account1.close()
        account2.close()
        legacy.close()


def test_latest_card_transaction_dates_are_per_credential_account(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    account1 = BankStore("cardcursor", user_id=1, source_account_id=101)
    account2 = BankStore("cardcursor", user_id=1, source_account_id=202)
    try:
        account1.upsert_card_billed([
            _card_txn("1111", "2026-08-01", post_date="2026-08-03"),
            _card_txn("1111", "2026-08-05"),
            _card_txn("2222", "2026/07/31"),
            _card_txn("3333", "2026-08-09", post_date="9999-99-99"),
            _card_txn("4444", "2026-01-31", post_date="2026-02-30"),
            _card_txn("", "2026-08-30"),
            _card_txn("blank-date", "", bill_date=None),
        ])
        account1.conn.commit()
        assert account2.latest_card_transaction_dates() == {}
        account2.upsert_card_billed([_card_txn("1111", "2026-08-20")])

        assert account1.latest_card_transaction_dates() == {
            "1111": date(2026, 8, 5),
            "2222": date(2026, 7, 31),
            "3333": date(2026, 8, 9),
            "4444": date(2026, 1, 31),
        }
        assert account2.latest_card_transaction_dates() == {
            "1111": date(2026, 8, 20),
        }
    finally:
        account1.close()
        account2.close()


def test_cursor_queries_use_backend_portable_sql_and_python_date_validation():
    class FakeConnection:
        def __init__(self, rows, expected_params):
            self.rows = rows
            self.expected_params = expected_params
            self.sql = ""

        def execute(self, sql, params):
            self.sql = sql
            assert params == self.expected_params
            return self

        def fetchall(self):
            return self.rows

    store = object.__new__(BankStore)
    store.user_id = 7
    store.source_account_id = 101
    conn = FakeConnection(
        [
            {"identity": "acct", "latest_date": "2026-02-28"},
            {"identity": "bad", "latest_date": "2026-02-30"},
        ],
        (7, 101, "twd_transactions"),
    )
    store.__dict__["conn"] = conn
    assert store.latest_twd_transaction_dates() == {"acct": date(2026, 2, 28)}
    assert "date(" not in conn.sql.lower()

    conn = FakeConnection(
        [
            {"identity": "card", "latest_date": "2026-02-28"},
            {"identity": "bad", "latest_date": "2026-02-30"},
        ],
        (7, 101, "card_billed_transactions"),
    )
    store.__dict__["conn"] = conn
    assert store.latest_card_transaction_dates() == {"card": date(2026, 2, 28)}
    assert "date(" not in conn.sql.lower()
