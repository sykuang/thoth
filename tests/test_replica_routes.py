"""Replica bootstrap/pull contract for the local-first frontend read model."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

from backend.server import db


def _register(client, email: str = "replica-user@palace.example") -> str:
    response = client.post(
        "/auth/register",
        json={"email": email, "password": "SyntheticReplicaPassword02!"},
    )
    assert response.status_code == 201, response.text
    return response.json()["token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_replica_partition_upsert_qualifies_postgres_conflict_columns() -> None:
    class _Result:
        @staticmethod
        def fetchone() -> tuple[int]:
            return (1,)

    class _Connection:
        sql = ""

        def execute(self, sql: str, _params: tuple[object, ...]) -> _Result:
            self.sql = sql
            return _Result()

    conn = _Connection()
    assert db.upsert_replica_partition(
        conn,
        user_id=7,
        partition_key="user",
        content_hash="abc",
    ) == 1
    assert "WHEN replica_partitions.content_hash = excluded.content_hash" in conn.sql
    assert "THEN replica_partitions.generation" in conn.sql
    assert "ELSE replica_partitions.generation + 1" in conn.sql


def _seed_bank(data_root: Path, *, user_id: int = 1, bank: str = "cathay") -> None:
    con = sqlite3.connect(data_root / f"{bank}.sqlite")
    con.executescript(
        """
        CREATE TABLE accounts (
            account_no TEXT, user_id INTEGER NOT NULL, currency TEXT, branch TEXT,
            nickname TEXT, type TEXT, product_type TEXT, raw_balance REAL,
            raw_balance_date TEXT, excluded INTEGER NOT NULL DEFAULT 0,
            nickname_overwrite TEXT, updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, account_no)
        );
        CREATE TABLE cards (
            card_no TEXT, user_id INTEGER NOT NULL, name TEXT, association TEXT,
            type TEXT, is_cube INTEGER, credit_limit REAL, used_credit REAL,
            statement_close_date TEXT, payment_due_date TEXT,
            active INTEGER NOT NULL DEFAULT 1, excluded INTEGER NOT NULL DEFAULT 0,
            nickname_overwrite TEXT, updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, card_no)
        );
        CREATE TABLE twd_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, account_no TEXT NOT NULL,
            txn_datetime TEXT NOT NULL, account_date TEXT, description TEXT,
            expend INTEGER, income INTEGER, balance INTEGER,
            counterparty_bank TEXT, counterparty_acct TEXT, memo TEXT,
            first_seen TEXT NOT NULL, dedup_key TEXT NOT NULL,
            category TEXT, subcategory TEXT, description_overwrite TEXT,
            tags_overwrite TEXT, splits_overwrite TEXT,
            auto_excluded INTEGER NOT NULL DEFAULT 0,
            flow_type TEXT, income_category TEXT, is_subscription INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE balance_history (
            user_id INTEGER NOT NULL, snapshot_date TEXT NOT NULL,
            twd_balance INTEGER, fx_balance INTEGER, loan_balance INTEGER,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, snapshot_date)
        );
        CREATE TABLE daily_metrics (
            user_id INTEGER NOT NULL, snapshot_date TEXT NOT NULL,
            category TEXT NOT NULL, payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, snapshot_date, category)
        );
        """
    )
    con.execute(
        """INSERT INTO accounts
           (account_no,user_id,currency,nickname,type,product_type,raw_balance,
            raw_balance_date,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        ("1234567890", user_id, "TWD", "測試帳戶", "活存", "deposit", 1000,
         "2026-08-09", "2026-08-09T10:00:00Z"),
    )
    con.execute(
        """INSERT INTO cards
           (card_no,user_id,name,type,active,updated_at)
           VALUES (?,?,?,?,?,?)""",
        ("****7015", user_id, "測試卡", "credit", 1, "2026-08-09T10:01:00Z"),
    )
    con.execute(
        """INSERT INTO twd_transactions
           (user_id,account_no,txn_datetime,account_date,description,expend,income,
            balance,first_seen,dedup_key,category)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (user_id, "1234567890", "2026-08-09T09:00:00", "2026-08-09", "早餐",
         80, None, 920, "2026-08-09T10:02:00Z", "replica-twd-1", "飲食"),
    )
    con.execute(
        """INSERT INTO balance_history
           (user_id,snapshot_date,twd_balance,fx_balance,loan_balance,updated_at)
           VALUES (?,?,?,?,?,?)""",
        (user_id, "2026-08-09", 1000, None, 200, "2026-08-09T10:03:00Z"),
    )
    con.execute(
        """INSERT INTO daily_metrics
           (user_id,snapshot_date,category,payload_json,updated_at)
           VALUES (?,?,?,?,?)""",
        (
            user_id,
            "2026-08-09",
            "card_summary",
            '{"latest_bill":{"twd":{"billAmount":321,"payBillStatus":"UnPaid"}},'
            '"CardList":[{"private":"must-not-replicate"}]}',
            "2026-08-09T10:04:00Z",
        ),
    )
    con.commit()
    con.close()


def test_replica_bootstrap_returns_versioned_user_and_bank_partitions(
    client, tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    token = _register(client)
    headers = _auth(token)
    assert client.post(
        "/accounts", json={"bank": "cathay", "label": "主帳"}, headers=headers,
    ).status_code == 201
    _seed_bank(tmp_path)

    response = client.get("/replica/bootstrap", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema_version"] == 1
    assert body["owner_id"] == 1
    assert body["generations"]["user"] == 1
    assert body["generations"]["bank:cathay"] == 1
    partitions = {item["name"]: item for item in body["partitions"]}
    user_partition = partitions["user"]["data"]
    assert user_partition["bank_accounts"][0]["label"] == "主帳"
    assert user_partition["preferences"] == {}
    assert user_partition["rules"]
    assert all(rule["user_id"] == 1 for rule in user_partition["rules"])
    assert user_partition["auto_debit_settings"] == []
    assert partitions["manual"]["data"] == {"accounts": [], "transactions": []}
    assert partitions["brokerage"]["data"] == {
        "accounts": [], "balances": [], "positions": [], "activities": [],
        "last_synced_at": None,
    }
    assert partitions["market"]["data"] == {
        "fx": {"source": None, "as_of": None, "rates": {"TWD": 1.0}},
        "quotes": [],
        "unavailable_symbols": [],
    }
    cathay = partitions["bank:cathay"]["data"]
    assert cathay["accounts"][0]["account_no"] == "1234567890"
    assert cathay["cards"][0]["card_no"] == "****7015"
    assert cathay["transactions"][0]["description"] == "早餐"
    assert cathay["portfolio_facts"]["latest_twd_balance"] == {
        "snapshot_date": "2026-08-09",
        "twd_balance": 1000,
    }
    assert cathay["portfolio_facts"]["loan_balance"] == {
        "snapshot_date": "2026-08-09",
        "amount_twd": 200,
        "source": "balance_history",
    }
    assert cathay["portfolio_facts"]["card_unpaid"] == {
        "snapshot_date": "2026-08-09",
        "amount_twd": 321,
    }
    assert "must-not-replicate" not in json.dumps(cathay)

    summary_response = client.get("/portfolio/summary", headers=headers)
    assert summary_response.status_code == 200, summary_response.text
    summary = next(
        row for row in summary_response.json()["by_bank"] if row["bank"] == "cathay"
    )
    assert cathay["portfolio_facts"]["latest_twd_balance"]["twd_balance"] == summary["assets"]
    assert cathay["portfolio_facts"]["loan_balance"]["amount_twd"] == summary["loan_balance"]
    assert cathay["portfolio_facts"]["card_unpaid"]["amount_twd"] == summary["card_unpaid"]


def test_replica_pull_returns_no_partitions_when_generations_match(
    client, tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    token = _register(client, email="replica-stable@palace.example")
    headers = _auth(token)
    _seed_bank(tmp_path)
    bootstrap = client.get("/replica/bootstrap", headers=headers).json()

    response = client.post(
        "/replica/pull",
        json={
            "schema_version": bootstrap["schema_version"],
            "generations": bootstrap["generations"],
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reset_required"] is False
    assert body["generations"] == bootstrap["generations"]
    assert body["partitions"] == []


def test_replica_pull_advances_only_changed_bank_after_transaction_patch(
    client, tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    token = _register(client, email="replica-patch@palace.example")
    headers = _auth(token)
    assert client.post(
        "/accounts", json={"bank": "cathay", "label": "主帳"}, headers=headers,
    ).status_code == 201
    _seed_bank(tmp_path)
    bootstrap = client.get("/replica/bootstrap", headers=headers).json()
    cathay = next(item for item in bootstrap["partitions"] if item["name"] == "bank:cathay")
    txn = cathay["data"]["transactions"][0]

    patch_response = client.patch(
        f"/transactions/cathay/{txn['kind']}/{txn['id']}",
        json={"category": "交通"},
        headers=headers,
    )
    assert patch_response.status_code == 200, patch_response.text

    response = client.post(
        "/replica/pull",
        json={
            "schema_version": bootstrap["schema_version"],
            "generations": bootstrap["generations"],
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["name"] for item in body["partitions"]] == ["bank:cathay"]
    changed = body["partitions"][0]
    assert changed["generation"] == bootstrap["generations"]["bank:cathay"] + 1
    assert changed["data"]["transactions"][0]["category"] == "交通"
    assert "raw" not in changed["data"]["transactions"][0]


def test_replica_bootstrap_is_tenant_scoped(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    alice_token = _register(client, email="replica-alice@palace.example")
    _seed_bank(tmp_path, user_id=1)
    alice = client.get("/replica/bootstrap", headers=_auth(alice_token))
    assert alice.status_code == 200, alice.text

    bob_token = _register(client, email="replica-bob@palace.example")
    response = client.get("/replica/bootstrap", headers=_auth(bob_token))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["owner_id"] == 2
    cathay = next(item for item in body["partitions"] if item["name"] == "bank:cathay")
    assert cathay["data"]["accounts"] == []
    assert cathay["data"]["cards"] == []
    assert cathay["data"]["transactions"] == []
    assert cathay["data"]["portfolio_facts"] == {
        "latest_twd_balance": None,
        "latest_account_transaction_balances": [],
        "loan_balance": None,
        "card_unpaid": None,
    }


def test_replica_pull_requires_reset_on_schema_mismatch(client) -> None:
    token = _register(client, email="replica-reset@palace.example")

    response = client.post(
        "/replica/pull",
        json={"schema_version": 999, "generations": {}},
        headers=_auth(token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema_version"] == 1
    assert body["reset_required"] is True
    assert body["generations"] == {}
    assert body["partitions"] == []


def test_replica_pull_expresses_deletion_by_partition_replacement(
    client, tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    token = _register(client, email="replica-delete@palace.example")
    headers = _auth(token)
    _seed_bank(tmp_path)
    bootstrap = client.get("/replica/bootstrap", headers=headers).json()

    con = sqlite3.connect(tmp_path / "cathay.sqlite")
    con.execute("DELETE FROM twd_transactions WHERE user_id=1")
    con.commit()
    con.close()

    response = client.post(
        "/replica/pull",
        json={
            "schema_version": bootstrap["schema_version"],
            "generations": bootstrap["generations"],
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["name"] for item in body["partitions"]] == ["bank:cathay"]
    changed = body["partitions"][0]
    assert changed["generation"] == bootstrap["generations"]["bank:cathay"] + 1
    assert changed["data"]["transactions"] == []


def test_replica_pull_advances_manual_partition_after_manual_account_create(
    client, tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    token = _register(client, email="replica-manual@palace.example")
    headers = _auth(token)
    bootstrap = client.get("/replica/bootstrap", headers=headers).json()

    created = client.post(
        "/financial-accounts",
        json={
            "product_type": "deposit",
            "name": "現金",
            "currency": "TWD",
            "balance": "5000",
            "included_in_net_worth": True,
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text

    response = client.post(
        "/replica/pull",
        json={
            "schema_version": bootstrap["schema_version"],
            "generations": bootstrap["generations"],
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["name"] for item in body["partitions"]] == ["manual"]
    manual = body["partitions"][0]
    assert manual["generation"] == bootstrap["generations"]["manual"] + 1
    assert manual["data"]["accounts"][0]["name"] == "現金"
    assert manual["data"]["accounts"][0]["balance"] == "5000"


def test_replica_pull_advances_user_partition_after_preference_update(
    client, tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    token = _register(client, email="replica-preferences@palace.example")
    headers = _auth(token)
    bootstrap = client.get("/replica/bootstrap", headers=headers).json()

    updated = client.put(
        "/users/me/preferences",
        json={"fx_display_mode": "always_twd"},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text

    response = client.post(
        "/replica/pull",
        json={
            "schema_version": bootstrap["schema_version"],
            "generations": bootstrap["generations"],
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["name"] for item in body["partitions"]] == ["user"]
    user_partition = body["partitions"][0]
    assert user_partition["generation"] == bootstrap["generations"]["user"] + 1
    assert user_partition["data"]["preferences"]["fx_display_mode"] == "always_twd"


def test_replica_reconciliation_serializes_builders_before_generation_assignment(
    client, tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    from backend.server.replica_repo import reconcile_partitions

    initial = reconcile_partitions(1, lambda: {"bank:cathay": {"value": "old"}})
    assert initial[0].generation == 1

    old_started = threading.Event()
    release_old = threading.Event()
    new_started = threading.Event()
    results: dict[str, int] = {}

    def old_builder() -> dict:
        old_started.set()
        assert release_old.wait(2)
        return {"bank:cathay": {"value": "old"}}

    def new_builder() -> dict:
        new_started.set()
        return {"bank:cathay": {"value": "new"}}

    old_thread = threading.Thread(
        target=lambda: results.setdefault(
            "old", reconcile_partitions(1, old_builder)[0].generation,
        ),
    )
    new_thread = threading.Thread(
        target=lambda: results.setdefault(
            "new", reconcile_partitions(1, new_builder)[0].generation,
        ),
    )
    old_thread.start()
    assert old_started.wait(2)
    new_thread.start()
    assert not new_started.wait(0.1), "new builder ran before older reconciliation committed"
    release_old.set()
    old_thread.join(2)
    new_thread.join(2)
    assert not old_thread.is_alive()
    assert not new_thread.is_alive()

    assert results == {"old": 1, "new": 2}
    final = reconcile_partitions(1, new_builder)
    assert final[0].generation == 2


def test_market_partition_contains_fx_and_manual_symbol_quotes(monkeypatch) -> None:
    from backend.server import yahoo_finance
    from backend.server.routers import replica

    monkeypatch.setattr(
        replica.fx_service,
        "get_rates",
        lambda: {
            "source": "test",
            "as_of": "2026-08-09",
            "rates": {"EUR": 35.0, "USD": 30.5},
        },
    )
    monkeypatch.setattr(
        replica.yahoo_finance,
        "get_quote",
        lambda symbol: yahoo_finance.YahooQuote(
            symbol=symbol,
            name="Apple",
            currency="USD",
            exchange_name="Nasdaq",
            quote_type="EQUITY",
            regular_market_price="200",
            regular_market_time=1,
        ),
    )
    payload = replica._market_payload({
        "manual": {
            "accounts": [{"currency": "TWD"}],
            "transactions": [{"symbol": "AAPL", "currency": "EUR"}],
        },
        "brokerage": {"accounts": [], "balances": [], "positions": []},
    })

    assert payload["fx"] == {
        "source": "test",
        "as_of": "2026-08-09",
        "rates": {"EUR": 35.0, "TWD": 1.0, "USD": 30.5},
    }
    assert payload["quotes"][0]["symbol"] == "AAPL"
    assert payload["quotes"][0]["regular_market_price"] == "200"
    assert payload["unavailable_symbols"] == []


def test_replica_keeps_parent_for_unbalanced_legacy_splits(
    client, tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    token = _register(client, email="replica-splits@palace.example")
    _seed_bank(tmp_path)
    con = sqlite3.connect(tmp_path / "cathay.sqlite")
    con.execute(
        """CREATE TABLE card_billed_txns (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, card_no TEXT,
            consume_date TEXT, post_date TEXT, description TEXT,
            amount INTEGER, currency TEXT, splits_overwrite TEXT,
            auto_excluded INTEGER NOT NULL DEFAULT 0
        )""",
    )
    con.execute(
        """INSERT INTO card_billed_txns
           (id,user_id,card_no,consume_date,post_date,description,amount,currency,splits_overwrite)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (1, 1, "****7015", "2026-08-08", "2026-08-09", "不平衡拆帳", 100, "TWD",
         '[{"amount":40,"category":"飲食","auto_excluded":false}]'),
    )
    con.commit()
    con.close()

    response = client.get("/replica/bootstrap", headers=_auth(token))

    assert response.status_code == 200, response.text
    bank = next(
        item["data"]
        for item in response.json()["partitions"]
        if item["name"] == "bank:cathay"
    )
    billed = next(row for row in bank["transactions"] if row["kind"] == "billed")
    assert billed["id"] == 1
    assert billed["amount"] == -100
    assert billed["splits"] == [
        {"amount": 40, "category": "飲食", "auto_excluded": False},
    ]
    assert not any(str(row["id"]).startswith("1#") for row in bank["transactions"])


def test_postgres_reconcile_lock_uses_dedicated_connection(monkeypatch) -> None:
    from backend.server import db

    executed: list[str] = []

    class Result:
        def fetchone(self):
            return (True,)

    class Connection:
        def execute(self, sql, params):
            executed.append(sql)
            return Result()

        def close(self):
            executed.append("closed")

    class Psycopg:
        @staticmethod
        def connect(*args, **kwargs):
            executed.append("direct-connect")
            return Connection()

    monkeypatch.setattr(db, "DB_BACKEND", "postgres")
    monkeypatch.setattr(db, "psycopg", Psycopg)
    monkeypatch.setattr(db, "_database_url", lambda: "postgresql://test")

    with db.replica_reconcile_lock(1):
        executed.append("builder")

    assert executed == [
        "direct-connect",
        "SELECT pg_try_advisory_lock(%s, %s)",
        "builder",
        "SELECT pg_advisory_unlock(%s, %s)",
        "closed",
    ]


def test_loan_metric_is_ignored_without_a_loan_account(monkeypatch) -> None:
    from backend.server import replica_facts

    monkeypatch.setattr(
        replica_facts.db_api,
        "get_latest_loan_balance",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        replica_facts.db_api,
        "list_loan_accounts",
        lambda **kwargs: [],
    )
    monkeypatch.setattr(
        replica_facts.db_api,
        "get_latest_metric",
        lambda **kwargs: SimpleNamespace(
            snapshot_date="2026-08-09",
            payload={"loan": 500},
        ),
    )

    assert replica_facts._loan_fact("cathay", 1) is None
