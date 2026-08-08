"""Minimal Yahoo Finance adapter for manual-investment symbol confirmation.

Yahoo exposes no supported public Finance API. These web endpoints are therefore
best-effort: keep them behind this adapter and never make ledger writes depend on
quote availability.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

_SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"
_CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart"
_ALLOWED_QUOTE_TYPES = {"EQUITY", "ETF", "MUTUALFUND", "INDEX"}


class YahooFinanceUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class YahooSymbolMatch:
    symbol: str
    name: str
    exchange: str | None
    exchange_name: str | None
    quote_type: str


@dataclass(frozen=True)
class YahooQuote:
    symbol: str
    name: str
    currency: str
    exchange_name: str | None
    quote_type: str | None
    regular_market_price: str
    regular_market_time: int | None


def _get_json(url: str) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 Thoth/0.3"})
    try:
        with urlopen(request, timeout=5) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise YahooFinanceUnavailable("Yahoo Finance request failed") from exc
    if not isinstance(payload, dict):
        raise YahooFinanceUnavailable("Yahoo Finance returned an invalid response")
    return payload


@lru_cache(maxsize=256)
def search_symbols(query: str, preferred_currency: str | None = None) -> tuple[YahooSymbolMatch, ...]:
    normalized = query.strip().upper()
    if not normalized or len(normalized) > 64:
        return ()
    url = f"{_SEARCH_URL}?{urlencode({'q': normalized, 'quotesCount': 8, 'newsCount': 0})}"
    payload = _get_json(url)
    rows = payload.get("quotes")
    if not isinstance(rows, list):
        raise YahooFinanceUnavailable("Yahoo Finance search response is missing quotes")

    matches: list[YahooSymbolMatch] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        quote_type = str(row.get("quoteType") or "").strip().upper()
        if not symbol or quote_type not in _ALLOWED_QUOTE_TYPES:
            continue
        name = str(row.get("longname") or row.get("shortname") or symbol).strip()
        matches.append(YahooSymbolMatch(
            symbol=symbol,
            name=name,
            exchange=str(row["exchange"]).strip() if row.get("exchange") else None,
            exchange_name=str(row["exchDisp"]).strip() if row.get("exchDisp") else None,
            quote_type=quote_type,
        ))

    prefer_tw = (preferred_currency or "").strip().upper() == "TWD"
    matches.sort(key=lambda row: (
        0 if prefer_tw and row.symbol.endswith((".TW", ".TWO")) else 1,
        0 if row.symbol == normalized else 1,
        0 if row.symbol.startswith(normalized) else 1,
    ))
    return tuple(matches)


def get_quote(symbol: str) -> YahooQuote:
    normalized = symbol.strip().upper()
    if not normalized or len(normalized) > 32:
        raise YahooFinanceUnavailable("invalid Yahoo Finance symbol")
    return _get_quote_cached(normalized, int(time.time() // 60))


@lru_cache(maxsize=512)
def _get_quote_cached(normalized: str, _minute_bucket: int) -> YahooQuote:
    url = f"{_CHART_URL}/{quote(normalized, safe='')}?{urlencode({'range': '1d', 'interval': '1d'})}"
    payload = _get_json(url)
    try:
        result = payload["chart"]["result"][0]
        meta = result["meta"]
        raw_price = meta["regularMarketPrice"]
        raw_price_text = str(raw_price)
        whole, dot, fraction = raw_price_text.partition(".")
        if (
            not raw_price_text
            or len(raw_price_text) > 64
            or not whole.isdigit()
            or (bool(dot) and not fraction.isdigit())
            or len(whole.lstrip("0")) > 15
            or len(fraction) > 12
        ):
            raise ValueError("price must be a bounded fixed-point decimal")
        parsed_price = Decimal(raw_price_text)
        currency = str(meta["currency"]).strip().upper()
    except (KeyError, IndexError, TypeError, InvalidOperation, ValueError) as exc:
        raise YahooFinanceUnavailable("Yahoo Finance quote response is incomplete") from exc
    if not parsed_price.is_finite() or parsed_price <= 0 or len(currency) != 3:
        raise YahooFinanceUnavailable("Yahoo Finance quote response is invalid")
    market_time = meta.get("regularMarketTime")
    if (
        market_time is not None
        and (
            not isinstance(market_time, int)
            or market_time < 946_684_800
            or market_time > int(time.time()) + 86_400
        )
    ):
        raise YahooFinanceUnavailable("Yahoo Finance quote timestamp is invalid")
    return YahooQuote(
        symbol=str(meta.get("symbol") or normalized).strip().upper(),
        name=str(meta.get("longName") or meta.get("shortName") or normalized).strip(),
        currency=currency,
        exchange_name=str(meta["fullExchangeName"]).strip() if meta.get("fullExchangeName") else None,
        quote_type=str(meta["instrumentType"]).strip().upper() if meta.get("instrumentType") else None,
        regular_market_price=format(parsed_price, "f"),
        regular_market_time=market_time,
    )
