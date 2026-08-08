from __future__ import annotations

import pytest

from backend.server import yahoo_finance


def _register(client) -> dict[str, str]:
    response = client.post(
        "/auth/register",
        json={"email": "yahoo-symbols@palace.example", "password": "SyntheticTestPassword02!"},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_yahoo_search_prioritizes_taiwan_and_quote_parses_decimal(monkeypatch):
    def fake_get_json(url: str):
        if "/finance/search?" in url:
            return {
                "quotes": [
                    {
                        "symbol": "0050.KL",
                        "longname": "Systech Bhd",
                        "exchange": "KLS",
                        "exchDisp": "Kuala Lumpur Stock Exchange",
                        "quoteType": "EQUITY",
                    },
                    {
                        "symbol": "0050.TW",
                        "longname": "Yuanta/P-shares Taiwan Top 50 ETF",
                        "exchange": "TAI",
                        "exchDisp": "Taiwan",
                        "quoteType": "ETF",
                    },
                    {"symbol": "0050-WARRANT", "quoteType": "WARRANT"},
                ],
            }
        assert "/chart/0050.TW?" in url
        return {
            "chart": {
                "result": [{
                    "meta": {
                        "symbol": "0050.TW",
                        "longName": "Yuanta/P-shares Taiwan Top 50 ETF",
                        "currency": "TWD",
                        "fullExchangeName": "Taiwan",
                        "instrumentType": "ETF",
                        "regularMarketPrice": 102.85,
                        "regularMarketTime": 1786080601,
                    },
                }],
            },
        }

    monkeypatch.setattr(yahoo_finance, "_get_json", fake_get_json)
    yahoo_finance.search_symbols.cache_clear()
    yahoo_finance._get_quote_cached.cache_clear()

    matches = yahoo_finance.search_symbols("0050", "TWD")
    assert [row.symbol for row in matches] == ["0050.TW", "0050.KL"]
    assert matches[0].exchange_name == "Taiwan"

    quote = yahoo_finance.get_quote("0050.tw")
    assert quote.symbol == "0050.TW"
    assert quote.currency == "TWD"
    assert quote.regular_market_price == "102.85"


def test_yahoo_symbol_routes_are_authenticated_and_map_upstream_failures(client, monkeypatch):
    assert client.get("/financial-accounts/symbols/search?q=0050").status_code == 401
    headers = _register(client)

    monkeypatch.setattr(
        yahoo_finance,
        "search_symbols",
        lambda query, preferred_currency=None: (
            yahoo_finance.YahooSymbolMatch(
                symbol="0050.TW",
                name="Yuanta/P-shares Taiwan Top 50 ETF",
                exchange="TAI",
                exchange_name="Taiwan",
                quote_type="ETF",
            ),
        ),
    )
    response = client.get(
        "/financial-accounts/symbols/search?q=0050&preferred_currency=TWD",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()[0]["symbol"] == "0050.TW"

    def fail(_symbol: str):
        raise yahoo_finance.YahooFinanceUnavailable("synthetic upstream failure")

    monkeypatch.setattr(yahoo_finance, "get_quote", fail)
    response = client.get("/financial-accounts/symbols/0050.TW/quote", headers=headers)
    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "Yahoo Finance 暫時無法取得現價"}


@pytest.mark.parametrize(
    ("price", "market_time"),
    [
        ("1e100000", 1786080601),
        ("0", 1786080601),
        ("102.85", 10**100),
    ],
)
def test_yahoo_quote_rejects_unbounded_price_and_timestamp(
    monkeypatch,
    price,
    market_time,
):
    monkeypatch.setattr(
        yahoo_finance,
        "_get_json",
        lambda _url: {
            "chart": {
                "result": [{
                    "meta": {
                        "symbol": "0050.TW",
                        "currency": "TWD",
                        "regularMarketPrice": price,
                        "regularMarketTime": market_time,
                    },
                }],
            },
        },
    )
    yahoo_finance._get_quote_cached.cache_clear()
    with pytest.raises(yahoo_finance.YahooFinanceUnavailable):
        yahoo_finance.get_quote("0050.TW")


def test_investment_account_balance_uses_yahoo_market_value_and_clears_summary_cache(
    client,
    monkeypatch,
):
    headers = _register(client)
    account_response = client.post(
        "/financial-accounts",
        headers=headers,
        json={
            "product_type": "investment",
            "name": "Market value regression",
            "currency": "TWD",
            "balance": "0",
            "included_in_net_worth": True,
        },
    )
    assert account_response.status_code == 201, account_response.text
    account_id = account_response.json()["id"]
    initial_summary = client.get("/portfolio/summary", headers=headers)
    assert initial_summary.status_code == 200
    assert initial_summary.json()["manual_assets_twd"] == 0

    monkeypatch.setattr(
        yahoo_finance,
        "get_quote",
        lambda symbol: yahoo_finance.YahooQuote(
            symbol=symbol,
            name="Yuanta/P-shares Taiwan Top 50 ETF",
            currency="TWD",
            exchange_name="Taiwan",
            quote_type="ETF",
            regular_market_price="102.85",
            regular_market_time=1786080601,
        ),
    )
    opening_response = client.post(
        f"/financial-accounts/{account_id}/transactions",
        headers=headers,
        json={
            "kind": "opening",
            "occurred_on": "2026-08-09",
            "symbol": "0050.TW",
            "quantity": "5",
            "unit_price": "80",
            "currency": "TWD",
        },
    )
    assert opening_response.status_code == 201, opening_response.text

    accounts_response = client.get("/financial-accounts?source=manual", headers=headers)
    assert accounts_response.status_code == 200, accounts_response.text
    assert accounts_response.json()[0]["balance"] == "514.25"
    assert accounts_response.json()[0]["valuation_source"] == "yahoo_finance"
    assert accounts_response.json()[0]["as_of"] == "2026-08-07T05:30:01+00:00"

    refreshed_summary = client.get("/portfolio/summary", headers=headers)
    assert refreshed_summary.status_code == 200, refreshed_summary.text
    assert refreshed_summary.json()["manual_assets_twd"] == 514

    def fail_quote(_symbol: str):
        raise yahoo_finance.YahooFinanceUnavailable("synthetic quote outage")

    monkeypatch.setattr(yahoo_finance, "get_quote", fail_quote)
    fallback_response = client.get("/financial-accounts?source=manual", headers=headers)
    assert fallback_response.status_code == 200, fallback_response.text
    assert fallback_response.json()[0]["balance"] == "0"
    assert fallback_response.json()[0]["valuation_source"] == "manual_fallback"
    assert fallback_response.json()[0]["as_of"] is None
