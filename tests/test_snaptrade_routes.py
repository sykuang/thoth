from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any


REDIRECT_URI = "thoth:///investments"


class FakeSnapTradeGateway:
    def __init__(self) -> None:
        self.fail_positions = False
        self.partial_positions_only = False
        self.partial_balances_only = False
        self.empty_accounts = False
        self.holdings_initial_sync_completed = True
        self.transactions_initial_sync_completed = True
        self.holdings_unavailable = False
        self.activity_calls = 0
        self.activity_id = "activity-1"
        self.slug = "SCHWAB"
        self.registered: list[str] = []
        self.remote_users: set[str] = set()
        self.deleted: list[str] = []
        self.option_positions: list[dict[str, Any]] = []
        self.account_total = "1250.00"

    def register_user(self, user_id: str) -> dict[str, str]:
        if user_id in self.remote_users:
            raise RuntimeError("user already registered")
        self.registered.append(user_id)
        self.remote_users.add(user_id)
        return {"userId": user_id, "userSecret": f"secret-{user_id}"}

    def list_registered_user_ids(self) -> set[str]:
        return set(self.remote_users)

    def delete_user(self, user_id: str) -> None:
        self.remote_users.discard(user_id)
        self.deleted.append(user_id)

    def connection_url(self, user_id: str, user_secret: str, redirect_uri: str) -> str:
        assert user_secret == f"secret-{user_id}"
        assert redirect_uri == REDIRECT_URI
        return "https://connect.snaptrade.example/portal"

    def list_connections(self, user_id: str, user_secret: str) -> list[dict[str, Any]]:
        return [{"id": "auth-1", "brokerage": {"slug": self.slug}}]

    def list_accounts(self, user_id: str, user_secret: str) -> list[dict[str, Any]]:
        if self.empty_accounts:
            return []
        return [{
            "id": "account-high",
            "name": "Schwab Brokerage",
            "number": "••1234",
            "institution_name": "Schwab",
            "brokerage_authorization": "auth-1",
            "sync_status": {
                "holdings": {
                    "initial_sync_completed": self.holdings_initial_sync_completed,
                    "holdings_unavailable": self.holdings_unavailable,
                },
                "transactions": {
                    "initial_sync_completed": self.transactions_initial_sync_completed,
                },
            },
            "balance": {"total": {"amount": self.account_total, "currency": "USD"}},
        }]

    def list_balances(
        self, user_id: str, user_secret: str, account_id: str,
    ) -> list[dict[str, Any]]:
        assert account_id == "account-high"
        if self.holdings_unavailable or self.partial_balances_only:
            return []
        return [{
            "currency": {"code": "USD"},
            "cash": "250.00",
            "buying_power": "300.00",
        }]

    def list_positions(
        self, user_id: str, user_secret: str, account_id: str,
    ) -> list[dict[str, Any]]:
        if self.fail_positions:
            raise RuntimeError("upstream positions failed")
        if self.partial_positions_only or self.holdings_unavailable:
            return []
        return [{
            "symbol": {
                "id": "symbol-nvda",
                "symbol": {
                    "symbol": "NVDA",
                    "description": "NVIDIA",
                    "type": {"code": "cs"},
                    "currency": {"code": "USD"},
                },
            },
            "units": "2.5",
            # SnapTrade v11 also emits fractional_units. It is metadata, not
            # an extra quantity to add to units.
            "fractional_units": "2.5",
            "price": "400.00",
            "average_purchase_price": "350.00",
            "currency": {"code": "USD"},
        }]

    def list_option_positions(
        self, user_id: str, user_secret: str, account_id: str,
    ) -> list[dict[str, Any]]:
        return self.option_positions

    def list_activities(
        self, user_id: str, user_secret: str, account_id: str,
    ) -> list[dict[str, Any]]:
        self.activity_calls += 1
        return [{
            "id": self.activity_id,
            "type": "BUY",
            "trade_date": "2026-08-01T12:00:00Z",
            "settlement_date": "2026-08-03T12:00:00Z",
            "units": "2.5",
            "price": "350.00",
            "amount": "875.00",
            "currency": {"code": "USD"},
            "symbol": {"symbol": "NVDA", "description": "NVIDIA"},
            "fee": "1.00",
            "description": "BUY NVIDIA",
        }]


def _register(client, email: str) -> dict[str, str]:
    response = client.post(
        "/auth/register",
        json={"email": email, "password": "SyntheticTestPassword02!"},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _install_fake(monkeypatch, fake: FakeSnapTradeGateway) -> None:
    from backend.server.routers import snaptrade as router_module
    from backend.server.snaptrade import SnapTradeService

    monkeypatch.setenv("SNAPTRADE_CLIENT_ID", "test-client")
    monkeypatch.setenv("SNAPTRADE_CONSUMER_KEY", "test-consumer")
    monkeypatch.setattr(router_module, "get_service", lambda: SnapTradeService(fake))


def _connect(client, headers: dict[str, str]):
    return client.post(
        "/snaptrade/connect",
        headers=headers,
        json={"redirect_uri": REDIRECT_URI},
    )


def test_snaptrade_routes_require_auth(client):
    assert client.get("/snaptrade/status").status_code == 401
    assert client.get("/snaptrade/portfolio").status_code == 401
    assert client.post("/snaptrade/connect", json={"redirect_uri": REDIRECT_URI}).status_code == 401
    assert client.post("/snaptrade/sync").status_code == 401


def test_connect_registers_user_encrypts_secret_and_returns_read_only_portal(client, monkeypatch):
    fake = FakeSnapTradeGateway()
    _install_fake(monkeypatch, fake)
    headers = _register(client, "snaptrade-connect@example.com")

    response = _connect(client, headers)

    assert response.status_code == 200, response.text
    assert response.json() == {"redirect_uri": "https://connect.snaptrade.example/portal"}
    assert len(fake.registered) == 1

    from backend.server.db import get_conn

    with get_conn() as conn:
        row = conn.execute("SELECT encrypted_user_secret FROM snaptrade_users").fetchone()
    assert row is not None
    encrypted = row[0]
    if isinstance(encrypted, memoryview):
        encrypted = encrypted.tobytes()
    assert b"secret-" not in bytes(encrypted)

    status = client.get("/snaptrade/status", headers=headers)
    assert status.status_code == 200
    assert status.json() == {
        "configured": True,
        "registered": True,
        "connection_count": 1,
        "last_synced_at": None,
    }
    assert "secret" not in json.dumps(status.json()).lower()


def test_sync_persists_cash_positions_and_real_activities_without_float_rounding(client, monkeypatch):
    fake = FakeSnapTradeGateway()
    _install_fake(monkeypatch, fake)
    headers = _register(client, "snaptrade-sync@example.com")
    assert _connect(client, headers).status_code == 200

    synced = client.post("/snaptrade/sync", headers=headers)

    assert synced.status_code == 200, synced.text
    assert synced.json()["counts"] == {
        "accounts": 1,
        "balances": 1,
        "positions": 1,
        "activities": 1,
    }
    body = client.get("/snaptrade/portfolio", headers=headers).json()
    assert [row["id"] for row in body["accounts"]] == ["account-high"]
    assert body["accounts"][0]["brokerage_slug"] == "SCHWAB"
    assert body["accounts"][0]["activities_supported"] is True
    assert body["balances"][0]["cash"] == "250.00"
    assert body["positions"][0]["symbol"] == "NVDA"
    assert body["positions"][0]["quantity"] == "2.5"
    assert body["positions"][0]["market_value"] == "1000.000"
    assert body["activities"][0]["amount"] == "875.00"


def test_failed_refresh_preserves_previous_snapshot(client, monkeypatch):
    fake = FakeSnapTradeGateway()
    _install_fake(monkeypatch, fake)
    headers = _register(client, "snaptrade-rollback@example.com")
    assert _connect(client, headers).status_code == 200
    assert client.post("/snaptrade/sync", headers=headers).status_code == 200
    before = client.get("/snaptrade/portfolio", headers=headers).json()

    fake.fail_positions = True
    failed = client.post("/snaptrade/sync", headers=headers)

    assert failed.status_code == 502
    assert client.get("/snaptrade/portfolio", headers=headers).json() == before


def test_partial_positions_response_preserves_previous_snapshot(client, monkeypatch):
    fake = FakeSnapTradeGateway()
    _install_fake(monkeypatch, fake)
    headers = _register(client, "snaptrade-partial@example.com")
    assert _connect(client, headers).status_code == 200
    assert client.post("/snaptrade/sync", headers=headers).status_code == 200
    before = client.get("/snaptrade/portfolio", headers=headers).json()

    fake.partial_positions_only = True
    failed = client.post("/snaptrade/sync", headers=headers)

    assert failed.status_code == 502
    assert client.get("/snaptrade/portfolio", headers=headers).json() == before


def test_partial_balances_response_preserves_previous_snapshot(client, monkeypatch):
    fake = FakeSnapTradeGateway()
    _install_fake(monkeypatch, fake)
    headers = _register(client, "snaptrade-partial-balances@example.com")
    assert _connect(client, headers).status_code == 200
    assert client.post("/snaptrade/sync", headers=headers).status_code == 200
    before = client.get("/snaptrade/portfolio", headers=headers).json()

    fake.partial_balances_only = True
    failed = client.post("/snaptrade/sync", headers=headers)

    assert failed.status_code == 502
    assert client.get("/snaptrade/portfolio", headers=headers).json() == before


def test_incomplete_holdings_sync_status_preserves_previous_snapshot(client, monkeypatch):
    fake = FakeSnapTradeGateway()
    _install_fake(monkeypatch, fake)
    headers = _register(client, "snaptrade-holdings-status@example.com")
    assert _connect(client, headers).status_code == 200
    assert client.post("/snaptrade/sync", headers=headers).status_code == 200
    before = client.get("/snaptrade/portfolio", headers=headers).json()

    fake.holdings_initial_sync_completed = False
    failed = client.post("/snaptrade/sync", headers=headers)

    assert failed.status_code == 502
    assert client.get("/snaptrade/portfolio", headers=headers).json() == before


def test_incomplete_transactions_sync_status_preserves_previous_snapshot(client, monkeypatch):
    fake = FakeSnapTradeGateway()
    _install_fake(monkeypatch, fake)
    headers = _register(client, "snaptrade-transactions-status@example.com")
    assert _connect(client, headers).status_code == 200
    assert client.post("/snaptrade/sync", headers=headers).status_code == 200
    before = client.get("/snaptrade/portfolio", headers=headers).json()

    fake.transactions_initial_sync_completed = False
    failed = client.post("/snaptrade/sync", headers=headers)

    assert failed.status_code == 502
    assert client.get("/snaptrade/portfolio", headers=headers).json() == before


def test_holdings_unavailable_keeps_reported_total_without_fake_detail(client, monkeypatch):
    fake = FakeSnapTradeGateway()
    leaked_balances = fake.list_balances("user", "secret", "account-high")
    leaked_positions = fake.list_positions("user", "secret", "account-high")
    fake.holdings_unavailable = True
    monkeypatch.setattr(fake, "list_balances", lambda *_: leaked_balances)
    monkeypatch.setattr(fake, "list_positions", lambda *_: leaked_positions)
    _install_fake(monkeypatch, fake)
    headers = _register(client, "snaptrade-holdings-unavailable@example.com")
    assert _connect(client, headers).status_code == 200

    synced = client.post("/snaptrade/sync", headers=headers)
    portfolio = client.get("/snaptrade/portfolio", headers=headers).json()

    assert synced.status_code == 200
    assert portfolio["accounts"][0]["holdings_unavailable"] is True
    assert portfolio["accounts"][0]["balance_total"] == "1250.00"
    assert portfolio["balances"] == []
    assert portfolio["positions"] == []


def test_empty_accounts_with_live_connection_preserves_previous_snapshot(client, monkeypatch):
    fake = FakeSnapTradeGateway()
    _install_fake(monkeypatch, fake)
    headers = _register(client, "snaptrade-empty-accounts@example.com")
    assert _connect(client, headers).status_code == 200
    assert client.post("/snaptrade/sync", headers=headers).status_code == 200
    before = client.get("/snaptrade/portfolio", headers=headers).json()

    fake.empty_accounts = True
    failed = client.post("/snaptrade/sync", headers=headers)

    assert failed.status_code == 502
    assert client.get("/snaptrade/portfolio", headers=headers).json() == before


def test_refresh_replaces_stale_activity_ids(client, monkeypatch):
    fake = FakeSnapTradeGateway()
    _install_fake(monkeypatch, fake)
    headers = _register(client, "snaptrade-activity-replace@example.com")
    assert _connect(client, headers).status_code == 200
    assert client.post("/snaptrade/sync", headers=headers).status_code == 200

    fake.activity_id = "activity-reimported"
    assert client.post("/snaptrade/sync", headers=headers).status_code == 200

    activities = client.get("/snaptrade/portfolio", headers=headers).json()["activities"]
    assert [row["id"] for row in activities] == ["activity-reimported"]


def test_snapshot_is_isolated_per_user(client, monkeypatch):
    fake = FakeSnapTradeGateway()
    _install_fake(monkeypatch, fake)
    first = _register(client, "snaptrade-owner@example.com")
    second = _register(client, "snaptrade-other@example.com")
    assert _connect(client, first).status_code == 200
    assert client.post("/snaptrade/sync", headers=first).status_code == 200

    other = client.get("/snaptrade/portfolio", headers=second)

    assert other.status_code == 200
    assert other.json() == {
        "accounts": [],
        "balances": [],
        "positions": [],
        "activities": [],
        "last_synced_at": None,
    }


def test_same_name_accounts_are_preserved_and_exact_id_duplicates_are_collapsed():
    from backend.server.snaptrade import _dedupe_accounts

    rows = [
        {"id": "one", "name": "Individual", "number": "••1234", "institution_name": "Schwab"},
        {"id": "two", "name": "Individual", "number": "••1234", "institution_name": "Schwab"},
        {"id": "one", "name": "Individual", "number": "••1234", "institution_name": "Schwab"},
    ]
    assert [row["id"] for row in _dedupe_accounts(rows)] == ["one", "two"]


def test_short_position_is_preserved_and_units_are_not_added_to_fractional_units():
    from backend.server.snaptrade import SnapTradeService

    rows = [{
        "symbol": {"id": "short", "symbol": {"symbol": "TSLA"}},
        "units": "-1.25",
        "fractional_units": "0.25",
        "price": "200.00",
    }]
    mapped = SnapTradeService._map_positions("account", rows, "now")
    assert mapped[0]["quantity"] == "-1.25"
    assert mapped[0]["market_value"] == "-250.0000"


def test_option_position_does_not_synthesize_market_value_without_multiplier():
    from backend.server.snaptrade import SnapTradeService

    rows = [{
        "symbol": {
            "id": "option-1",
            "symbol": {
                "symbol": "AAPL 260918C00200000",
                "type": {"code": "OPTION"},
            },
        },
        "units": "2",
        "price": "10",
        "currency": {"code": "USD"},
    }]

    mapped = SnapTradeService._map_positions("account", rows, "now")

    assert mapped[0]["market_value"] is None


def test_sdk_v11_option_holdings_are_saved(client, monkeypatch):
    fake = FakeSnapTradeGateway()
    fake.account_total = "3250.00"
    fake.option_positions = [{
        "symbol": {
            "id": "broker-option-id",
            "description": "AAPL Sep 2026 200 Call",
            "option_symbol": {
                "id": "universal-option-id",
                "ticker": "AAPL 260918C00200000",
                "option_type": "CALL",
                "strike_price": "200",
                "expiration_date": "2026-09-18",
                "underlying_symbol": {
                    "symbol": "AAPL",
                    "currency": {"code": "USD"},
                },
            },
        },
        "units": "2",
        "price": "10",
        "average_purchase_price": "8",
    }]
    _install_fake(monkeypatch, fake)
    headers = _register(client, "snaptrade-options@example.com")
    assert _connect(client, headers).status_code == 200

    assert client.post("/snaptrade/sync", headers=headers).status_code == 200
    positions = client.get("/snaptrade/portfolio", headers=headers).json()["positions"]
    position = next(
        row for row in positions if row["provider_symbol_id"] == "broker-option-id"
    )

    assert position["provider_symbol_id"] == "broker-option-id"
    assert position["symbol"] == "AAPL 260918C00200000"
    assert position["asset_type"] == "OPTION"
    assert position["currency"] == "USD"
    assert position["market_value"] == "2000"


def test_unsupported_brokerage_never_synthesizes_activities(client, monkeypatch):
    fake = FakeSnapTradeGateway()
    fake.slug = "UNSUPPORTED"
    _install_fake(monkeypatch, fake)
    headers = _register(client, "snaptrade-unsupported@example.com")
    assert _connect(client, headers).status_code == 200
    assert client.post("/snaptrade/sync", headers=headers).status_code == 200
    assert fake.activity_calls == 0
    body = client.get("/snaptrade/portfolio", headers=headers).json()
    assert body["accounts"][0]["activities_supported"] is False
    assert body["activities"] == []


def test_sdk_v11_connection_contract_accepts_redirect_uri_camel_case():
    from backend.server.snaptrade import SnapTradeSDKGateway

    gateway: Any = object.__new__(SnapTradeSDKGateway)
    gateway.client = SimpleNamespace(authentication=SimpleNamespace(
        login_snap_trade_user=lambda **kwargs: SimpleNamespace(
            body={"redirectURI": "https://connect.snaptrade.example/session"},
        ),
    ))
    assert gateway.connection_url("user", "secret", REDIRECT_URI) == (
        "https://connect.snaptrade.example/session"
    )


def test_sdk_v11_raw_json_preserves_decimal_precision():
    from backend.server.snaptrade import SnapTradeSDKGateway, SnapTradeService

    class RawBalanceAPI:
        @staticmethod
        def _get_user_account_balance_mapped_args(**kwargs):
            return SimpleNamespace(
                query={"userId": kwargs["user_id"], "userSecret": kwargs["user_secret"]},
                path={"accountId": kwargs["account_id"]},
            )

        @staticmethod
        def _get_user_account_balance_oapg(**kwargs):
            assert kwargs["skip_deserialization"] is True
            return SimpleNamespace(
                status=200,
                body=b'[{"currency":{"code":"USD"},"cash":0.123456789123456789}]',
            )

    gateway: Any = object.__new__(SnapTradeSDKGateway)
    gateway.client = SimpleNamespace(account_information=RawBalanceAPI())

    rows = gateway.list_balances("user", "secret", "account")

    assert rows[0]["cash"] == Decimal("0.123456789123456789")
    assert SnapTradeService._map_balances("account", rows, "now")[0]["cash"] == (
        "0.123456789123456789"
    )


def test_sdk_activities_are_paginated_without_truncation(monkeypatch):
    from backend.server import snaptrade
    from backend.server.snaptrade import SnapTradeSDKGateway

    calls: list[int] = []

    class RawActivityAPI:
        @staticmethod
        def _get_account_activities_mapped_args(
            *, account_id, user_id, user_secret, start_date, end_date,
            limit=None, offset=None,
        ):
            assert (account_id, user_id, user_secret) == ("account-1", "user", "secret")
            return SimpleNamespace(
                query={"startDate": start_date, "endDate": end_date, "limit": limit, "offset": offset},
                path={"accountId": account_id},
            )

        @staticmethod
        def _get_account_activities_oapg(*, query_params, path_params, skip_deserialization):
            assert skip_deserialization is True
            assert path_params == {"accountId": "account-1"}
            offset = query_params["offset"]
            calls.append(offset)
            rows = [{"id": f"activity-{i}"} for i in range(offset, min(offset + 2, 3))]
            return SimpleNamespace(status=200, body=json.dumps(rows).encode())

    monkeypatch.setattr(snaptrade, "_ACTIVITY_PAGE_SIZE", 2)
    gateway: Any = object.__new__(SnapTradeSDKGateway)
    gateway.client = SimpleNamespace(account_information=RawActivityAPI())

    rows = gateway.list_activities("user", "secret", "account-1")

    assert [row["id"] for row in rows] == ["activity-0", "activity-1", "activity-2"]
    assert calls == [0, 2]


def test_registration_storage_failure_deletes_remote_user_and_retry_succeeds(
    client, monkeypatch,
):
    from backend.server import db

    fake = FakeSnapTradeGateway()
    _install_fake(monkeypatch, fake)
    headers = _register(client, "snaptrade-registration-recovery@example.com")
    original_insert = db.snaptrade_insert_credentials
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("synthetic local storage failure")
        return original_insert(*args, **kwargs)

    monkeypatch.setattr(db, "snaptrade_insert_credentials", fail_once)

    first = _connect(client, headers)
    second = _connect(client, headers)

    assert first.status_code == 502
    assert second.status_code == 200
    assert fake.remote_users == {"thoth-1"}
    assert fake.deleted == ["thoth-1"]


def test_sync_is_rejected_while_same_user_sync_lock_is_held(client, monkeypatch):
    from backend.server import db

    fake = FakeSnapTradeGateway()
    _install_fake(monkeypatch, fake)
    headers = _register(client, "snaptrade-sync-lock@example.com")
    assert _connect(client, headers).status_code == 200
    with db.get_conn() as conn:
        user_id = conn.execute(
            "SELECT id FROM users WHERE email = ?",
            ("snaptrade-sync-lock@example.com",),
        ).fetchone()[0]
    assert db.snaptrade_acquire_lock(
        user_id,
        "sync",
        "first",
        "2000-01-01T00:00:00Z",
        "2999-01-01T00:00:00Z",
    )

    response = client.post("/snaptrade/sync", headers=headers)

    assert response.status_code == 409
    db.snaptrade_release_lock(user_id, "sync", "first")


def test_registration_credentials_write_requires_current_lock_owner(client):
    from backend.server import db

    _register(client, "snaptrade-registration-fence@example.com")
    with db.get_conn() as conn:
        user_id = conn.execute(
            "SELECT id FROM users WHERE email = ?",
            ("snaptrade-registration-fence@example.com",),
        ).fetchone()[0]
    assert db.snaptrade_acquire_lock(
        user_id,
        "registration",
        "current-owner",
        "2026-08-08T00:00:00.000Z",
        "2099-08-08T00:00:00.000Z",
    )

    inserted = db.snaptrade_insert_credentials(
        user_id,
        f"thoth-{user_id}",
        b"encrypted",
        "2026-08-08T00:00:01.000Z",
        lock_owner="stale-owner",
    )

    assert inserted is False
    assert db.snaptrade_get_credentials(user_id) is None
    db.snaptrade_release_lock(user_id, "registration", "current-owner")


def test_postgres_snapshot_requests_repeatable_read(monkeypatch):
    from backend.server import db

    statements: list[str] = []

    class Cursor:
        rowcount = 0

        def fetchall(self):
            return []

    class Connection:
        def execute(self, sql, params=()):
            statements.append(sql)
            return Cursor()

    class Context:
        def __enter__(self):
            return Connection()

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(db, "DB_BACKEND", "postgres")
    monkeypatch.setattr(db, "get_conn", Context)

    db.snaptrade_snapshot(1)

    assert statements[0] == "BEGIN ISOLATION LEVEL REPEATABLE READ"


def test_snapshot_replace_requires_current_sync_lock_owner(client, monkeypatch):
    from backend.server import db

    fake = FakeSnapTradeGateway()
    _install_fake(monkeypatch, fake)
    headers = _register(client, "snaptrade-fenced-write@example.com")
    assert _connect(client, headers).status_code == 200
    assert client.post("/snaptrade/sync", headers=headers).status_code == 200
    with db.get_conn() as conn:
        user_id = conn.execute(
            "SELECT id FROM users WHERE email = ?",
            ("snaptrade-fenced-write@example.com",),
        ).fetchone()[0]
    assert db.snaptrade_acquire_lock(
        user_id,
        "sync",
        "current-owner",
        "2026-08-08T00:00:00.000Z",
        "2099-08-08T00:00:00.000Z",
    )

    replaced = db.snaptrade_replace_snapshot(
        user_id,
        [],
        [],
        [],
        [],
        lock_owner="stale-owner",
        lock_now="2026-08-08T00:00:01.000Z",
    )

    assert replaced is False
    assert client.get("/snaptrade/portfolio", headers=headers).json()["accounts"]
    db.snaptrade_release_lock(user_id, "sync", "current-owner")


def test_snapshot_starts_explicit_read_transaction(monkeypatch):
    from backend.server import db

    statements: list[str] = []

    class Result:
        @staticmethod
        def fetchall():
            return []

    class Connection:
        def execute(self, sql, params=()):
            statements.append(sql)
            return Result()

    class Context:
        def __enter__(self):
            return Connection()

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(db, "get_conn", Context)

    db.snaptrade_snapshot(1)

    assert statements[0] == "BEGIN"


def test_sdk_non_success_response_fails_closed():
    from backend.server.snaptrade import _response

    try:
        _response(SimpleNamespace(status=401, body={"detail": "unauthorized"}))
    except RuntimeError as error:
        assert str(error) == "SnapTrade HTTP 401"
    else:
        raise AssertionError("non-success response must fail closed")


def test_malformed_success_payload_fails_closed():
    from backend.server.snaptrade import _rows

    for payload in ({"unexpected": []}, [{"id": "ok"}, "malformed"]):
        try:
            _rows(payload)
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"malformed payload accepted: {payload!r}")


def test_rejects_untrusted_connection_callback(client, monkeypatch):
    fake = FakeSnapTradeGateway()
    _install_fake(monkeypatch, fake)
    headers = _register(client, "snaptrade-redirect@example.com")

    response = client.post(
        "/snaptrade/connect",
        headers=headers,
        json={"redirect_uri": "https://attacker.example/callback"},
    )
    custom_scheme_attack = client.post(
        "/snaptrade/connect",
        headers=headers,
        json={"redirect_uri": "thoth://attacker.example/callback"},
    )

    assert response.status_code == 400
    assert custom_scheme_attack.status_code == 400
    assert fake.registered == []


def test_callback_allowlist_rejects_trusted_origin_wrong_path(monkeypatch):
    from backend.server.snaptrade import _validate_redirect_uri

    monkeypatch.setenv("CORS_ORIGINS", "https://app.example")
    for uri in (
        "https://app.example/attacker",
        "http://localhost:8081/attacker",
        "tauri://localhost/attacker",
    ):
        try:
            _validate_redirect_uri(uri)
        except ValueError:
            pass
        else:
            raise AssertionError(f"wrong callback path accepted: {uri}")

    assert _validate_redirect_uri("https://app.example/investments") == (
        "https://app.example/investments"
    )
    assert _validate_redirect_uri("http://localhost:8081/investments") == (
        "http://localhost:8081/investments"
    )
    assert _validate_redirect_uri("tauri://localhost/investments") == (
        "tauri://localhost/investments"
    )


def test_callback_allowlist_requires_exact_uri(monkeypatch):
    from backend.server.snaptrade import _validate_redirect_uri

    monkeypatch.setenv("CORS_ORIGINS", "https://app.example")
    for uri in (
        "thoth:/investments",
        "thoth:///investments?next=https://attacker.example",
        "thoth:///investments#fragment",
        "http://localhost:65535/investments",
        "https://app.example/investments?next=/attacker",
    ):
        try:
            _validate_redirect_uri(uri)
        except ValueError:
            pass
        else:
            raise AssertionError(f"non-exact callback accepted: {uri}")


def test_unexpected_value_error_detail_is_not_exposed():
    from fastapi import HTTPException

    from backend.server.routers.snaptrade import _raise_http

    try:
        _raise_http(ValueError("SENSITIVE_SENTINEL"))
    except HTTPException as error:
        assert error.status_code == 502
        assert "SENSITIVE_SENTINEL" not in error.detail
    else:
        raise AssertionError("expected HTTPException")


def test_missing_server_configuration_fails_closed(client, monkeypatch):
    monkeypatch.delenv("SNAPTRADE_CLIENT_ID", raising=False)
    monkeypatch.delenv("SNAPTRADE_CONSUMER_KEY", raising=False)
    headers = _register(client, "snaptrade-unconfigured@example.com")

    status = client.get("/snaptrade/status", headers=headers)
    connect = _connect(client, headers)

    assert status.status_code == 200
    assert status.json()["configured"] is False
    assert connect.status_code == 503
    assert "尚未設定" in connect.json()["detail"]
