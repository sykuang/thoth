from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest


def _register(client, email: str) -> str:
    response = client.post(
        "/auth/register",
        json={"email": email, "password": "SyntheticTestPassword02!"},
    )
    assert response.status_code == 201, response.text
    return response.json()["token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _payload(**overrides):
    payload = {
        "product_type": "deposit",
        "name": "Emergency Fund",
        "currency": "TWD",
        "balance": "1000.50",
        "included_in_net_worth": True,
    }
    payload.update(overrides)
    return payload


def test_manual_account_schema_drops_obsolete_columns(tmp_path, monkeypatch):
    import importlib
    import sqlite3

    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    from backend.server import db
    importlib.reload(db)

    with sqlite3.connect(db.server_db_path()) as conn:
        conn.execute(
            """CREATE TABLE manual_financial_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_type TEXT NOT NULL,
                institution_name TEXT NOT NULL,
                name TEXT NOT NULL,
                account_ref TEXT,
                currency TEXT NOT NULL,
                balance TEXT NOT NULL,
                as_of TEXT NOT NULL,
                included_in_net_worth INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
        )
        conn.execute(
            """INSERT INTO manual_financial_accounts
               (user_id, product_type, institution_name, name, account_ref, currency,
                balance, as_of, included_in_net_worth, created_at, updated_at)
               VALUES (1, 'deposit', 'Legacy Bank', 'Emergency Fund', '1234', 'TWD',
                       '1000', '2026-08-08', 1, 'now', 'now')""",
        )
        conn.execute(
            """CREATE TABLE manual_investment_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                occurred_on TEXT NOT NULL,
                symbol TEXT,
                quantity TEXT,
                unit_price TEXT,
                amount TEXT NOT NULL,
                currency TEXT NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
        )
        conn.execute(
            """INSERT INTO manual_investment_transactions
               (user_id, account_id, kind, occurred_on, symbol, quantity, unit_price,
                amount, currency, note, created_at, updated_at)
               VALUES (1, 1, 'opening', '2026-08-08', 'AAA', '5', '80',
                       '400', 'USD', NULL, 'now', 'now')""",
        )

    with db.get_conn() as conn:
        columns = db._columns(conn, "manual_financial_accounts")
        transaction_columns = db._columns(conn, "manual_investment_transactions")
        row = conn.execute(
            "SELECT name, balance FROM manual_financial_accounts WHERE id=1",
        ).fetchone()
        transaction_row = conn.execute(
            "SELECT quantity, amount FROM manual_investment_transactions WHERE id=1",
        ).fetchone()

    assert {"institution_name", "account_ref", "as_of"}.isdisjoint(columns)
    assert tuple(row) == ("Emergency Fund", "1000")
    # Compatibility release: 0.3.90 stops reading/writing the legacy column,
    # but leaves it in an existing DB until all 0.3.89 revisions are drained.
    assert "unit_price" in transaction_columns
    assert tuple(transaction_row) == ("5", "400")


def test_fresh_manual_investment_schema_has_only_total_cost(client):
    from backend.server import db

    with db.get_conn() as conn:
        columns = db._columns(conn, "manual_investment_transactions")

    assert "amount" in columns
    assert "unit_price" not in columns


def test_manual_financial_account_crud_and_liability_normalization(client):
    token = _register(client, "manual-account@palace.example")
    headers = _auth(token)

    created = client.post("/financial-accounts", headers=headers, json=_payload())
    assert created.status_code == 201, created.text
    account = created.json()
    assert account["id"].startswith("manual:")
    assert account["source"] == "manual"
    assert account["product_type"] == "deposit"
    assert account["balance"] == "1000.50"
    assert account["institution_name"] is None
    assert account["account_ref"] is None
    assert account["as_of"] is None
    assert account["editable"] is True
    assert account["deletable"] is True

    listed = client.get("/financial-accounts", headers=headers)
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json() if row["source"] == "manual"] == [account["id"]]

    updated = client.patch(
        f"/financial-accounts/{account['id']}",
        headers=headers,
        json=_payload(product_type="loan", name="Mortgage", balance="250.00"),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["product_type"] == "loan"
    assert updated.json()["balance"] == "-250.00"

    deleted = client.delete(f"/financial-accounts/{account['id']}", headers=headers)
    assert deleted.status_code == 204, deleted.text
    assert client.get("/financial-accounts", headers=headers).json() == []


def test_investment_patch_preserves_fallback_for_legacy_clients(client):
    token = _register(client, "manual-investment-legacy@palace.example")
    headers = _auth(token)
    created = client.post(
        "/financial-accounts",
        headers=headers,
        json=_payload(product_type="investment", balance="40"),
    ).json()

    legacy_patch = client.patch(
        f"/financial-accounts/{created['id']}",
        headers=headers,
        json=_payload(product_type="investment", name="Renamed", balance="514.25"),
    )
    assert legacy_patch.status_code == 200, legacy_patch.text
    assert legacy_patch.json()["name"] == "Renamed"
    assert legacy_patch.json()["manual_balance"] == "40"

    explicit_patch = client.patch(
        f"/financial-accounts/{created['id']}",
        headers=headers,
        json={
            **_payload(product_type="investment", name="Explicit fallback", balance="999"),
            "manual_balance": "55",
        },
    )
    assert explicit_patch.status_code == 200, explicit_patch.text
    assert explicit_patch.json()["manual_balance"] == "55"

    invalid_patch = client.patch(
        f"/financial-accounts/{created['id']}",
        headers=headers,
        json={
            **_payload(product_type="investment", balance="999"),
            "manual_balance": "1e2",
        },
    )
    assert invalid_patch.status_code == 422, invalid_patch.text


def test_manual_financial_accounts_are_tenant_isolated(client):
    owner = _register(client, "manual-owner@palace.example")
    other = _register(client, "manual-other@palace.example")
    created = client.post("/financial-accounts", headers=_auth(owner), json=_payload()).json()

    assert client.get("/financial-accounts", headers=_auth(other)).json() == []
    assert client.patch(
        f"/financial-accounts/{created['id']}",
        headers=_auth(other),
        json=_payload(name="Stolen"),
    ).status_code == 404
    assert client.delete(
        f"/financial-accounts/{created['id']}",
        headers=_auth(other),
    ).status_code == 404


def test_manual_financial_account_rejects_invalid_values(client):
    token = _register(client, "manual-invalid@palace.example")
    headers = _auth(token)

    for balance in ("NaN", "Infinity", "-1"):
        response = client.post(
            "/financial-accounts",
            headers=headers,
            json=_payload(balance=balance),
        )
        assert response.status_code == 422, (balance, response.text)

    assert client.post(
        "/financial-accounts",
        headers=headers,
        json=_payload(product_type="unknown"),
    ).status_code == 422
    assert client.post(
        "/financial-accounts",
        headers=headers,
        json=_payload(currency="US D"),
    ).status_code == 422


def test_manual_account_values_use_existing_summary_categories(client):
    token = _register(client, "manual-summary@palace.example")
    headers = _auth(token)

    rows = (
        _payload(name="Cash", product_type="deposit", balance="1000"),
        _payload(name="Loan", product_type="loan", balance="250"),
        _payload(name="Portfolio", product_type="investment", balance="300"),
    )
    for row in rows:
        response = client.post("/financial-accounts", headers=headers, json=row)
        assert response.status_code == 201, response.text

    summary = client.get("/portfolio/summary", headers=headers)
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["total_assets"] == 0
    assert body["brokerage_assets_twd"] == 0
    assert body["total_liabilities"] == 250
    assert body["total_loan"] == 250
    assert body["manual_assets_twd"] == 1300
    assert body["manual_liabilities_twd"] == 250
    assert body["total_assets_with_fx"] == 1300
    assert body["net_worth_with_fx"] == 1050


def test_excluded_manual_account_does_not_enter_summary(client):
    token = _register(client, "manual-excluded@palace.example")
    headers = _auth(token)
    response = client.post(
        "/financial-accounts",
        headers=headers,
        json=_payload(balance="900", included_in_net_worth=False),
    )
    assert response.status_code == 201, response.text

    body = client.get("/portfolio/summary", headers=headers).json()
    assert body["manual_assets_twd"] == 0
    assert body["total_assets_with_fx"] == 0


def test_manual_store_failure_does_not_publish_zero_net_worth(client, monkeypatch):
    token = _register(client, "manual-store-failure@palace.example")
    headers = _auth(token)
    from backend.server import financial_accounts

    def fail(_user_id: int):
        raise RuntimeError("synthetic manual store failure")

    monkeypatch.setattr(financial_accounts, "list_manual_accounts", fail)
    with pytest.raises(RuntimeError, match="synthetic manual store failure"):
        client.get("/portfolio/summary", headers=headers)


def test_manual_fx_failure_is_disclosed_in_skipped_accounts(client, monkeypatch):
    token = _register(client, "manual-fx-failure@palace.example")
    headers = _auth(token)
    account = client.post(
        "/financial-accounts",
        headers=headers,
        json=_payload(currency="USD", balance="100"),
    ).json()
    from backend.server.routers import portfolio

    monkeypatch.setattr(portfolio.fx_service, "convert_to_twd", lambda amount, currency: None)
    body = client.get("/portfolio/summary", headers=headers).json()
    assert account["id"] in body["skipped"]
    assert body["manual_assets_twd"] == 0


def test_manual_investment_transaction_crud_without_dividends(client):
    token = _register(client, "manual-trades@palace.example")
    headers = _auth(token)
    account = client.post(
        "/financial-accounts",
        headers=headers,
        json=_payload(product_type="investment", name="Manual Portfolio", balance="5000"),
    ).json()

    missing_opening_cost = client.post(
        f"/financial-accounts/{account['id']}/transactions",
        headers=headers,
        json={
            "kind": "opening",
            "occurred_on": "2026-07-31",
            "symbol": "AAA",
            "quantity": "5",
            "currency": "USD",
        },
    )
    assert missing_opening_cost.status_code == 422, missing_opening_cost.text
    legacy_unit_cost = client.post(
        f"/financial-accounts/{account['id']}/transactions",
        headers=headers,
        json={
            "kind": "opening",
            "occurred_on": "2026-07-31",
            "symbol": "AAA",
            "quantity": "5",
            "unit_price": "80",
            "currency": "USD",
        },
    )
    assert legacy_unit_cost.status_code == 422, legacy_unit_cost.text
    assert "unit_price" in legacy_unit_cost.text
    assert "extra_forbidden" in legacy_unit_cost.text
    zero_opening_cost = client.post(
        f"/financial-accounts/{account['id']}/transactions",
        headers=headers,
        json={
            "kind": "opening",
            "occurred_on": "2026-07-31",
            "symbol": "AAA",
            "quantity": "5",
            "amount": "0",
            "currency": "USD",
        },
    )
    assert zero_opening_cost.status_code == 422, zero_opening_cost.text

    opening_from_total_cost = client.post(
        f"/financial-accounts/{account['id']}/transactions",
        headers=headers,
        json={
            "kind": "opening",
            "occurred_on": "2026-07-30",
            "symbol": "TOTAL",
            "quantity": "3",
            "amount": "100",
            "currency": "USD",
        },
    )
    assert opening_from_total_cost.status_code == 201, opening_from_total_cost.text
    assert opening_from_total_cost.json()["amount"] == "100"
    cleanup_total_cost = client.delete(
        f"/financial-accounts/{account['id']}/transactions/"
        f"{opening_from_total_cost.json()['id']}",
        headers=headers,
    )
    assert cleanup_total_cost.status_code == 204, cleanup_total_cost.text

    opening = client.post(
        f"/financial-accounts/{account['id']}/transactions",
        headers=headers,
        json={
            "kind": "opening",
            "occurred_on": "2026-07-31",
            "symbol": "AAA",
            "quantity": "5",
            "amount": "400.00",
            "currency": "USD",
            "note": "opening position",
        },
    )
    assert opening.status_code == 201, opening.text
    assert opening.json()["amount"] == "400.00"

    backdated_sell = client.post(
        f"/financial-accounts/{account['id']}/transactions",
        headers=headers,
        json={
            "kind": "sell",
            "occurred_on": "2026-07-30",
            "symbol": "AAA",
            "quantity": "1",
            "amount": "99",
            "currency": "USD",
        },
    )
    assert backdated_sell.status_code == 422, backdated_sell.text

    created = client.post(
        f"/financial-accounts/{account['id']}/transactions",
        headers=headers,
        json={
            "kind": "buy",
            "occurred_on": "2026-08-01",
            "symbol": "AAA",
            "quantity": "2.5",
            "amount": "250.00",
            "currency": "USD",
            "note": "opening trade",
        },
    )
    assert created.status_code == 201, created.text
    trade = created.json()
    assert trade["account_id"] == account["id"]
    assert trade["kind"] == "buy"
    assert trade["quantity"] == "2.5"
    assert trade["amount"] == "250.00"
    assert client.get(
        f"/financial-accounts/{account['id']}/holdings",
        headers=headers,
    ).json() == [{"symbol": "AAA", "quantity": "7.5", "currency": "USD"}]

    listed = client.get(
        f"/financial-accounts/{account['id']}/transactions",
        headers=headers,
    )
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()] == [trade["id"], opening.json()["id"]]

    updated = client.patch(
        f"/financial-accounts/{account['id']}/transactions/{trade['id']}",
        headers=headers,
        json={
            "kind": "sell",
            "occurred_on": "2026-08-02",
            "symbol": "AAA",
            "quantity": "1.25",
            "amount": "150.00",
            "currency": "USD",
            "note": None,
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["kind"] == "sell"
    assert updated.json()["amount"] == "150.00"
    assert client.get(
        f"/financial-accounts/{account['id']}/holdings",
        headers=headers,
    ).json() == [{"symbol": "AAA", "quantity": "3.75", "currency": "USD"}]

    oversell = client.post(
        f"/financial-accounts/{account['id']}/transactions",
        headers=headers,
        json={
            "kind": "sell",
            "occurred_on": "2026-08-03",
            "symbol": "AAA",
            "quantity": "99",
            "amount": "11880",
            "currency": "USD",
        },
    )
    assert oversell.status_code == 422, oversell.text

    assert client.patch(
        f"/financial-accounts/{account['id']}",
        headers=headers,
        json=_payload(product_type="deposit", name="Wrong Type", balance="5000"),
    ).status_code == 422
    assert client.delete(
        f"/financial-accounts/{account['id']}/transactions/{opening.json()['id']}",
        headers=headers,
    ).status_code == 422

    assert client.post(
        f"/financial-accounts/{account['id']}/transactions",
        headers=headers,
        json={
            "kind": "dividend",
            "occurred_on": "2026-08-03",
            "amount": "5",
            "currency": "USD",
        },
    ).status_code == 422

    deleted = client.delete(
        f"/financial-accounts/{account['id']}/transactions/{trade['id']}",
        headers=headers,
    )
    assert deleted.status_code == 204, deleted.text
    assert client.get(
        f"/financial-accounts/{account['id']}/holdings",
        headers=headers,
    ).json() == [{"symbol": "AAA", "quantity": "5", "currency": "USD"}]


def test_manual_investment_transactions_are_tenant_isolated_and_investment_only(client):
    owner = _register(client, "manual-trade-owner@palace.example")
    other = _register(client, "manual-trade-other@palace.example")
    owner_headers = _auth(owner)
    account = client.post(
        "/financial-accounts",
        headers=owner_headers,
        json=_payload(product_type="investment", balance="5000"),
    ).json()
    deposit = client.post(
        "/financial-accounts",
        headers=owner_headers,
        json=_payload(product_type="deposit", name="Cash"),
    ).json()
    trade_payload = {
        "kind": "fee",
        "occurred_on": "2026-08-01",
        "amount": "3.50",
        "currency": "USD",
        "note": "commission",
    }

    assert client.post(
        f"/financial-accounts/{deposit['id']}/transactions",
        headers=owner_headers,
        json=trade_payload,
    ).status_code == 422
    assert client.get(
        f"/financial-accounts/{account['id']}/transactions",
        headers=_auth(other),
    ).status_code == 404


def test_manual_investment_concurrent_sells_cannot_create_negative_holdings(client):
    token = _register(client, "manual-trade-race@palace.example")
    headers = _auth(token)
    user_id = client.get("/auth/me", headers=headers).json()["id"]
    account = client.post(
        "/financial-accounts",
        headers=headers,
        json=_payload(product_type="investment", balance="100"),
    ).json()
    assert client.post(
        f"/financial-accounts/{account['id']}/transactions",
        headers=headers,
        json={
            "kind": "opening",
            "occurred_on": "2026-08-01",
            "symbol": "AAA",
            "quantity": "1",
            "amount": "10",
            "currency": "USD",
        },
    ).status_code == 201

    from backend.server.financial_accounts import (
        InvalidManualAccount,
        create_investment_transaction,
    )

    def sell_once() -> bool:
        try:
            create_investment_transaction(
                user_id,
                account["id"],
                kind="sell",
                occurred_on="2026-08-02",
                symbol="AAA",
                quantity="1",
                amount="10",
                currency="USD",
                note=None,
            )
            return True
        except InvalidManualAccount:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _: sell_once(), range(2))) == [False, True]
    assert client.get(
        f"/financial-accounts/{account['id']}/holdings",
        headers=headers,
    ).json() == []


def test_manual_account_decimal_input_is_bounded(client, monkeypatch):
    token = _register(client, "manual-decimal-bound@palace.example")
    response = client.post(
        "/financial-accounts",
        headers=_auth(token),
        json=_payload(balance="1e1000000"),
    )
    assert response.status_code == 422, response.text
    assert client.post(
        "/financial-accounts",
        headers=_auth(token),
        json=_payload(balance="1e2"),
    ).status_code == 422

    account = client.post(
        "/financial-accounts",
        headers=_auth(token),
        json=_payload(product_type="investment", balance="100"),
    ).json()
    base_trade = {
        "occurred_on": "2026-08-01",
        "symbol": "AAA",
        "currency": "USD",
    }
    for payload in (
        {**base_trade, "kind": "opening", "quantity": "1e2", "amount": "1"},
        {**base_trade, "kind": "opening", "quantity": "1", "amount": "1e2"},
        {**base_trade, "kind": "buy", "quantity": "1", "amount": "1e2"},
        {"kind": "fee", "occurred_on": "2026-08-01", "amount": "1e2", "currency": "USD"},
    ):
        assert client.post(
            f"/financial-accounts/{account['id']}/transactions",
            headers=_auth(token),
            json=payload,
        ).status_code == 422

    from backend.server import fx_service

    monkeypatch.setattr(fx_service, "get_rate", lambda currency: 1.0)
    assert fx_service.convert_to_twd("9007199254740993", "TWD") == 9007199254740993


def test_canonical_list_adapts_manual_bank_and_brokerage_sources(client, monkeypatch):
    token = _register(client, "canonical-accounts@palace.example")
    headers = _auth(token)
    assert client.post("/financial-accounts", headers=headers, json=_payload()).status_code == 201

    from backend.server import db, financial_accounts
    from backend.server.routers import portfolio

    monkeypatch.setattr(portfolio, "KNOWN_BANKS", ["demo"])
    monkeypatch.setattr(
        portfolio,
        "_bank_accounts",
        lambda bank, user_id: [SimpleNamespace(
            account_no="bank-1",
            nickname="Savings",
            nickname_overwrite=None,
            product_type="deposit",
            currency="TWD",
            balance=88,
            snapshot_date="2026-08-08",
            excluded=False,
        )],
    )
    monkeypatch.setattr(
        db,
        "snaptrade_snapshot",
        lambda user_id: {
            "accounts": [{
                "id": "broker-1",
                "institution_name": "Broker",
                "name": "Individual",
                "number": "9999",
                "balance_total": "123.45",
                "balance_currency": "USD",
                "synced_at": "2026-08-08T00:00:00Z",
            }],
        },
    )

    user_id = client.get("/auth/me", headers=headers).json()["id"]
    rows = financial_accounts.list_financial_accounts(user_id)
    assert {row.source for row in rows} == {"manual", "bank_sync", "brokerage_sync"}
    assert [row.editable for row in rows if row.source != "manual"] == [False, False]
