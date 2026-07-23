"""Regression tests for dashboard performance infrastructure."""
from __future__ import annotations

import logging
import time


def _register(client, email: str = "perf-user@palace.example") -> str:
    r = client.post("/auth/register", json={"email": email, "password": "SyntheticTestPassword02!"})
    assert r.status_code == 201, r.text
    return r.json()["token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_request_timing_log_includes_method_path_status_and_ms(client, caplog):
    """P0: every HTTP request should emit a compact timing log."""
    caplog.set_level(logging.INFO, logger="backend.request")
    r = client.get("/healthz")
    assert r.status_code == 200

    messages = [rec.getMessage() for rec in caplog.records if rec.name == "backend.request"]
    assert any("method=GET" in m and "path=/healthz" in m and "status=200" in m and "duration_ms=" in m for m in messages)


def test_dashboard_ttl_cache_reuses_value_and_can_be_invalidated():
    """TTL cache should be per-key, hit within TTL, and explicit invalidation should force recompute."""
    from backend.server.dashboard_cache import clear_dashboard_cache, get_or_set_dashboard_cache

    clear_dashboard_cache()
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return {"value": calls["n"]}

    first = get_or_set_dashboard_cache("test.portfolio.summary", user_id=1, params=("all",), ttl_seconds=30, compute=compute)
    second = get_or_set_dashboard_cache("test.portfolio.summary", user_id=1, params=("all",), ttl_seconds=30, compute=compute)
    other_user = get_or_set_dashboard_cache("test.portfolio.summary", user_id=2, params=("all",), ttl_seconds=30, compute=compute)

    assert first == {"value": 1}
    assert second == {"value": 1}
    assert other_user == {"value": 2}
    assert calls["n"] == 2

    clear_dashboard_cache(user_id=1)
    third = get_or_set_dashboard_cache("test.portfolio.summary", user_id=1, params=("all",), ttl_seconds=30, compute=compute)
    assert third == {"value": 3}
    assert calls["n"] == 3


def test_dashboard_ttl_cache_expires():
    from backend.server import dashboard_cache

    dashboard_cache.clear_dashboard_cache()
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return calls["n"]

    assert dashboard_cache.get_or_set_dashboard_cache("test.x", user_id=1, params=(), ttl_seconds=0.01, compute=compute) == 1
    time.sleep(0.02)
    assert dashboard_cache.get_or_set_dashboard_cache("test.x", user_id=1, params=(), ttl_seconds=0.01, compute=compute) == 2


def test_portfolio_summary_uses_ttl_cache(client, monkeypatch):
    """P3 short-term materialized snapshot: second summary call should not recompute within TTL."""
    from backend.server.routers import portfolio

    token = _register(client, "portfolio-cache@palace.example")
    calls = {"n": 0}

    def fake_compute(user_id: int):
        calls["n"] += 1
        return {
            "total_assets": calls["n"],
            "fx_assets_twd": 0,
            "total_assets_with_fx": calls["n"],
            "total_liabilities": 0,
            "total_card_unpaid": 0,
            "total_loan": 0,
            "current_month_spending": 0,
            "net_worth": calls["n"],
            "net_worth_with_fx": calls["n"],
            "as_of": None,
            "by_bank": [],
            "skipped": [],
        }

    monkeypatch.setattr(portfolio, "_compute_portfolio_summary", fake_compute)
    portfolio.clear_dashboard_cache()
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    r1 = client.get("/portfolio/summary", headers=_auth(token))
    r2 = client.get("/portfolio/summary", headers=_auth(token))
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["total_assets"] == 1
    assert r2.json()["total_assets"] == 1
    assert calls["n"] == 1

    portfolio.clear_dashboard_cache()
    r3 = client.get("/portfolio/summary", headers=_auth(token))
    assert r3.json()["total_assets"] == 2


def test_transactions_stats_emits_section_timing_logs(client, monkeypatch, caplog):
    """Stats cold path should log collect/aggregate timings for production profiling."""
    import backend.server.routers.transactions as tx

    class Row:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    def fake_rows(banks, kinds, since, until, q, user_id, card_date_basis="consume"):
        assert card_date_basis == "consume"
        return [
            Row(bank="hsbc", kind="billed", date="2026-06-12", amount=-1000,
                category="飲食", subcategory=None, txn_type="spending", flow_type="expense",
                is_subscription=False, income_category=None, account_no=None, card_no="CARD-1",
                excluded=False, auto_excluded=False),
        ]

    monkeypatch.setattr(tx, "_collect_transaction_stat_rows", fake_rows)
    caplog.set_level(logging.INFO, logger="backend.perf")

    body = tx._compute_transactions_stats(
        banks=["hsbc"], kinds=["billed"], since=None, until=None,
        q=None, category=None, card_date_basis="consume", user_id=1,
    )

    assert body["total_expense"] == 1000
    messages = [rec.getMessage() for rec in caplog.records if rec.name == "backend.perf"]
    assert any("event=transactions.stats" in m and "section=collect" in m and "rows=1" in m for m in messages)
    assert any("event=transactions.stats" in m and "section=aggregate" in m and "rows=1" in m for m in messages)


def test_portfolio_summary_emits_bank_and_total_timing_logs(monkeypatch, caplog):
    """Portfolio summary cold path should log per-bank and total timings."""
    from backend.server.routers import portfolio

    monkeypatch.setattr(portfolio, "KNOWN_BANKS", ["hsbc"])
    monkeypatch.setattr(portfolio, "_latest_balance", lambda bank, user_id: ("2026-06-24", 1000))
    monkeypatch.setattr(portfolio, "_latest_payload", lambda bank, category, user_id: ("2026-06-24", []))
    monkeypatch.setattr(portfolio, "_latest_loan_balance", lambda bank, user_id: None)
    monkeypatch.setattr(portfolio, "_bank_current_month_spending", lambda bank, user_id: 123)
    monkeypatch.setattr(portfolio, "_bank_accounts", lambda bank, user_id: [])
    monkeypatch.setattr(portfolio, "LIABILITY_PARSERS", {"hsbc": ("card_summary", lambda payload: 0)})
    caplog.set_level(logging.INFO, logger="backend.perf")

    body = portfolio._compute_portfolio_summary(user_id=1)

    assert body["total_assets"] == 1000
    messages = [rec.getMessage() for rec in caplog.records if rec.name == "backend.perf"]
    assert any("event=portfolio.summary" in m and "section=bank" in m and "bank=hsbc" in m for m in messages)
    assert any("event=portfolio.summary" in m and "section=total" in m and "banks=1" in m for m in messages)


def test_transactions_stats_uses_ttl_cache(client, monkeypatch):
    """P2/P3: stats endpoint should serve repeated dashboard calls from per-user TTL cache."""
    import backend.server.routers.transactions as tx

    token = _register(client, "tx-cache@palace.example")
    calls = {"n": 0}

    def fake_compute(*, banks, kinds, since, until, q, category, card_date_basis, user_id):
        assert card_date_basis == "consume"
        calls["n"] += 1
        return {
            "total": calls["n"],
            "by_bank": {},
            "by_kind": {},
            "by_month": {},
            "by_category": {},
            "by_subcategory": {},
            "banks_queried": banks,
            "amount_by_month": {},
            "amount_by_category": {},
            "total_income": 0,
            "total_expense": 0,
            "total_net": 0,
            "amount_by_flow_type": {"expense": 0, "income": 0, "transfer": 0, "investment": 0},
            "subscription_total": 0,
            "subscription_by_month": {},
            "amount_by_income_category": {"salary": 0, "bonus": 0, "interest_dividend": 0, "investment_gain": 0, "other": 0},
            "passive_income_total": 0,
            "passive_income_by_month": {},
            "passive_income_pct": 0.0,
            "income_unclassified_count": 0,
        }

    monkeypatch.setattr(tx, "_compute_transactions_stats", fake_compute)
    from backend.server.dashboard_cache import clear_dashboard_cache

    clear_dashboard_cache()
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    r1 = client.get("/transactions/stats", headers=_auth(token))
    r2 = client.get("/transactions/stats", headers=_auth(token))
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["total"] == 1
    assert r2.json()["total"] == 1
    assert calls["n"] == 1

    clear_dashboard_cache()
    r3 = client.get("/transactions/stats", headers=_auth(token))
    assert r3.json()["total"] == 2
