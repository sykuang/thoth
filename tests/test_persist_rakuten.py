from __future__ import annotations

from pathlib import Path

import pytest

from backend.core import store as store_mod
from backend.core.store import BankStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BankStore:
    monkeypatch.setattr(store_mod, "DATA_ROOT", tmp_path)
    value = BankStore("rakuten_test")
    yield value
    value.close()


def _fixture() -> dict:
    return {
        "twd_txn_results": [{
            "accounts": [{"acctNo": "81234567890123", "balance": "12,345"}],
            "txDetails": [
                {
                    "sysDate": "2026/07/26",
                    "sysTime": "09:30:00",
                    "txDesc": "跨行轉入",
                    "amt": "1,500",
                    "amtSign": True,
                    "balance": "12,345",
                    "memo": "薪資",
                },
                {
                    "sysDate": "2026/07/25",
                    "sysTime": "18:05:00",
                    "txDesc": "轉帳支出",
                    "amt": "200",
                    "amtSign": False,
                    "balance": "10,845",
                    "memo": "生活費",
                },
            ],
        }],
        "_all_endpoints": ["CTWQU0001_010", "CTWQU0001_011"],
    }


def test_rakuten_endpoint_key_keeps_task_and_action() -> None:
    from backend.banks.rakuten import _endpoint_key

    assert _endpoint_key(
        "https://www.rakuten-bank.com.tw/ixtein/adapters/ebank/txns/"
        "channel-ctw/CTWQU0001/011",
    ) == "CTWQU0001_011"


def test_persist_rakuten_writes_accounts_balances_and_transactions(store: BankStore) -> None:
    from backend.core.persist import persist_rakuten

    delta = persist_rakuten(_fixture(), store)

    account = store.conn.execute(
        "SELECT account_no, currency, product_type, raw_balance FROM accounts",
    ).fetchone()
    assert dict(account) == {
        "account_no": "81234567890123",
        "currency": "TWD",
        "product_type": "deposit",
        "raw_balance": 12345.0,
    }

    txns = store.conn.execute(
        "SELECT txn_datetime, description, raw_description, expend, income, balance, memo "
        "FROM twd_transactions ORDER BY txn_datetime",
    ).fetchall()
    assert [dict(row) for row in txns] == [
        {
            "txn_datetime": "2026-07-25T18:05:00",
            "description": "轉帳支出 - 生活費",
            "raw_description": "轉帳支出",
            "expend": 200.0,
            "income": None,
            "balance": 10845.0,
            "memo": "生活費",
        },
        {
            "txn_datetime": "2026-07-26T09:30:00",
            "description": "跨行轉入 - 薪資",
            "raw_description": "跨行轉入",
            "expend": None,
            "income": 1500.0,
            "balance": 12345.0,
            "memo": "薪資",
        },
    ]
    assert delta["bank"] == "rakuten"
    assert delta["accounts"] == 1
    assert delta["balance_days"] == 1
    assert delta["twd_txn_new"] == 2


def test_persist_rakuten_is_idempotent(store: BankStore) -> None:
    from backend.core.persist import persist_rakuten

    assert persist_rakuten(_fixture(), store)["twd_txn_new"] == 2
    assert persist_rakuten(_fixture(), store)["twd_txn_new"] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1


def test_persist_rakuten_drops_repeated_month_snapshot(store: BankStore) -> None:
    from backend.core.persist import persist_rakuten

    data = _fixture()
    data["twd_txn_results"].append(data["twd_txn_results"][0])

    assert persist_rakuten(data, store)["twd_txn_new"] == 2
    assert store.conn.execute("SELECT COUNT(*) FROM twd_transactions").fetchone()[0] == 2


def test_persist_rakuten_skips_transactions_without_account_number(store: BankStore) -> None:
    from backend.core.persist import persist_rakuten

    data = _fixture()
    data["twd_txn_results"][0]["accounts"] = []

    delta = persist_rakuten(data, store)
    assert delta["twd_txn_new"] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM twd_transactions").fetchone()[0] == 0


def test_persist_rakuten_parses_formatted_zero_balance(store: BankStore) -> None:
    from backend.core.persist import persist_rakuten

    data = _fixture()
    data["twd_txn_results"][0]["accounts"][0]["balance"] = "NT$ 0"
    data["twd_txn_results"][0]["txDetails"] = []

    delta = persist_rakuten(data, store)
    row = store.conn.execute("SELECT raw_balance FROM accounts").fetchone()
    assert row["raw_balance"] == 0
    assert delta["balance_days"] == 1


def test_persist_rakuten_parses_numeric_zero_balance(store: BankStore) -> None:
    from backend.core.persist import persist_rakuten

    data = _fixture()
    data["twd_txn_results"][0]["accounts"][0]["balance"] = 0
    data["twd_txn_results"][0]["txDetails"] = []

    delta = persist_rakuten(data, store)
    row = store.conn.execute("SELECT raw_balance FROM accounts").fetchone()
    assert row["raw_balance"] == 0
    assert delta["balance_days"] == 1


def test_rakuten_creds_are_registered() -> None:
    from backend.core.creds import ALL_CREDS, RakutenCreds

    assert RakutenCreds in ALL_CREDS
    assert RakutenCreds.BANK == "RAKUTEN"
    assert RakutenCreds._attrs() == ["national_id", "user_code", "password"]
