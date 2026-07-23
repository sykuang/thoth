"""Regression: /transactions/stats should use lightweight stats rows, not full list transform."""
from __future__ import annotations


def _register(client, email: str = "tx-stats-fast@palace.example") -> str:
    r = client.post("/auth/register", json={"email": email, "password": "SyntheticTestPassword02!"})
    assert r.status_code == 201, r.text
    return r.json()["token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_pg_fast_excluded_prefetch_returns_empty_without_falling_back(monkeypatch):
    """PG fast path should return {} for no excluded rows instead of falling back per bank."""
    import backend.server.db_facade.accounts as accounts_mod

    calls = {"fast": 0, "open": 0}

    class FakeAccounts(accounts_mod.AccountsReadMixin):
        @staticmethod
        def _excluded_nos_all_banks_fast(**kwargs):
            calls["fast"] += 1
            return {}

    def fail_open(bank):
        calls["open"] += 1
        raise AssertionError("fast-path empty result must not fall back to per-bank connections")

    monkeypatch.setattr(accounts_mod.db, "open_bank_conn", fail_open)
    out = FakeAccounts().list_excluded_account_nos_all_banks(user_id=6, banks=["hsbc", "ubot"])

    assert out == {}
    assert calls == {"fast": 1, "open": 0}


def test_transactions_stats_logs_excluded_prefetch_duration(monkeypatch, caplog):
    """Stats collection should log the once-per-request excluded prefetch time."""
    import logging
    import backend.server.routers.transactions as tx

    class FakeApi:
        def list_excluded_account_nos_all_banks(self, *, user_id, banks):
            return {}

        def list_excluded_card_nos_all_banks(self, *, user_id, banks):
            return {}

        def list_txn_stat_rows_for_bank(self, *, bank, user_id, kinds,
                                        excluded_accounts_by_bank=None,
                                        excluded_cards_by_bank=None):
            return []

    monkeypatch.setattr(tx, "db_api", FakeApi())
    caplog.set_level(logging.INFO, logger="backend.perf")

    rows = tx._collect_transaction_stat_rows(
        banks=["hsbc", "ubot"], kinds=["twd"], since=None, until=None, q=None, user_id=6,
    )

    assert rows == []
    messages = [rec.getMessage() for rec in caplog.records if rec.name == "backend.perf"]
    assert any("event=transactions.stats" in m and "section=excluded_prefetch" in m and "banks=2" in m for m in messages)


def test_transactions_stats_prefetches_excluded_maps_once_for_all_banks(monkeypatch):
    """Stats collection should prefetch excluded account/card maps once, not per bank."""
    import backend.server.routers.transactions as tx

    calls = {"accounts": [], "cards": [], "rows": []}

    class FakeApi:
        def list_excluded_account_nos_all_banks(self, *, user_id, banks):
            calls["accounts"].append(tuple(banks))
            return {"hsbc": {"A-EX"}}

        def list_excluded_card_nos_all_banks(self, *, user_id, banks):
            calls["cards"].append(tuple(banks))
            return {"hsbc": {"C-EX"}}

        def list_txn_stat_rows_for_bank(self, *, bank, user_id, kinds,
                                        excluded_accounts_by_bank=None,
                                        excluded_cards_by_bank=None):
            calls["rows"].append((bank, excluded_accounts_by_bank, excluded_cards_by_bank))
            return []

    monkeypatch.setattr(tx, "db_api", FakeApi())

    rows = tx._collect_transaction_stat_rows(
        banks=["hsbc", "ubot"], kinds=["twd"], since=None, until=None, q=None, user_id=6,
    )

    assert rows == []
    assert calls["accounts"] == [("hsbc", "ubot")]
    assert calls["cards"] == [("hsbc", "ubot")]
    assert len(calls["rows"]) == 2
    for _, account_map, card_map in calls["rows"]:
        assert account_map == {"hsbc": {"A-EX"}}
        assert card_map == {"hsbc": {"C-EX"}}


def test_transactions_stats_uses_lightweight_stats_rows_not_full_txn_rows(client, monkeypatch):
    import backend.server.routers.transactions as tx

    token = _register(client)

    def fail_full_rows(*args, **kwargs):
        raise AssertionError("/transactions/stats must not call list_txns_for_bank SELECT * path")

    class Row:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    def fake_stats_rows(*, bank, user_id, kinds):
        assert bank in tx.KNOWN_BANKS
        if bank != "hsbc":
            return []
        return [
            Row(bank="hsbc", kind="billed", date="2026/06/11", amount=-622,
                category="退稅", subcategory=None, txn_type="refund", flow_type="income",
                is_subscription=False, income_category=None, account_no=None, card_no="CARD-1",
                excluded=False, auto_excluded=False),
            Row(bank="hsbc", kind="billed", date="2026-06-12", amount=-1000,
                category="飲食", subcategory=None, txn_type="spending", flow_type="expense",
                is_subscription=False, income_category=None, account_no=None, card_no="CARD-1",
                excluded=False, auto_excluded=False),
        ]

    class FakeApi:
        def list_excluded_account_nos_all_banks(self, *, user_id, banks):
            return {}

        def list_excluded_card_nos_all_banks(self, *, user_id, banks):
            return {}

        def list_txn_stat_rows_for_bank(self, *, bank, user_id, kinds,
                                        excluded_accounts_by_bank=None,
                                        excluded_cards_by_bank=None):
            return fake_stats_rows(bank=bank, user_id=user_id, kinds=kinds)

        def list_txns_for_bank(self, *args, **kwargs):
            return fail_full_rows(*args, **kwargs)

    monkeypatch.setattr(tx, "db_api", FakeApi())
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    from backend.server.dashboard_cache import clear_dashboard_cache

    clear_dashboard_cache()

    r = client.get("/transactions/stats?bank=hsbc", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    assert body["total_income"] == 622
    assert body["total_expense"] == 1000
    assert body["total_net"] == -378
    assert body["amount_by_month"]["2026-06"]["income"] == 622
    assert body["amount_by_month"]["2026-06"]["expense"] == 1000
    assert body["amount_by_category"]["飲食"] == 1000
