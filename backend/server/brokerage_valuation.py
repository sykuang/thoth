"""Best-effort read projection, never persisted into provider snapshots.

Completeness is observable only: a provider's unreported partial list cannot be
inferred from its independently timed account total.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from backend.server import fx_service, yahoo_finance

_STOCK_TYPES = {'CS', 'STOCK', 'EQUITY', 'ETF', 'ET'}


def _decimal(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and abs(number) < Decimal('1e30') else None
    except (InvalidOperation, ValueError):
        return None


def _currency(value):
    # Do not uppercase minor-unit GBp into GBP.
    return value if isinstance(value, str) and len(value) == 3 and value.isascii() and value.isalpha() and value.isupper() else None


def _stock(position):
    return str(position.get('asset_type') or '').upper() in _STOCK_TYPES


def _symbol(position):
    symbol = str(position.get('symbol') or '').strip().upper()
    return 'BRK-B' if symbol == 'BRK.B' and position.get('currency') == 'USD' else symbol


def _quote(symbol):
    try:
        return yahoo_finance.get_quote(symbol)
    except Exception:
        return None


def value_snapshot(raw):
    result = deepcopy(raw)
    if not result.get('accounts'):
        return result
    positions = result.get('positions') or []
    symbols = sorted({_symbol(p) for p in positions if _stock(p)})
    # ponytail: request-scoped bounded workers; existing adapter owns minute cache.
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(symbols)))) as pool:
        quotes = dict(zip(symbols, pool.map(_quote, symbols)))
    for p in positions:
        p.update(valuation_source='broker_snapshot', valuation_as_of=None, valuation_reason=None)
        for field in ('price', 'market_value'):
            if _decimal(p.get(field)) is None:
                p[field] = None
        if not _stock(p):
            continue
        q = quotes.get(_symbol(p))
        quantity = _decimal(p.get('quantity'))
        price = _decimal(q.regular_market_price) if q else None
        if (q is None or q.symbol != _symbol(p) or q.quote_type not in {'EQUITY', 'ETF'}
                or _currency(p.get('currency')) is None or q.currency != p['currency']
                or quantity is None or price is None or price <= 0):
            p['valuation_reason'] = 'quote_unavailable'
            continue
        try:
            as_of = datetime.fromtimestamp(q.regular_market_time, UTC).isoformat() if q.regular_market_time is not None else None
        except (ValueError, OverflowError, OSError, TypeError):
            p['valuation_reason'] = 'quote_unavailable'
            continue
        p.update(price=format(price, 'f'), market_value=format(quantity * price, 'f'),
                 valuation_source='yahoo', valuation_as_of=as_of)

    bundle = None
    fx_loaded = False
    for account in result['accounts']:
        account.update(valuation_source='broker_snapshot', valuation_as_of=None, valuation_reason=None)
        holdings = [p for p in positions if p.get('account_id') == account.get('id')]
        cash = [b for b in (result.get('balances') or []) if b.get('account_id') == account.get('id')]
        reason = None
        if account.get('holdings_unavailable') is not False:
            reason = 'holdings_unavailable'
        elif not holdings:
            reason = 'positions_unavailable'
        elif not cash:
            reason = 'cash_unavailable'
        elif any(p['valuation_reason'] for p in holdings):
            reason = 'quote_unavailable'
        target = _currency(account.get('balance_currency'))
        values = [(p.get('market_value'), p.get('currency')) for p in holdings] + [(b.get('cash'), b.get('currency')) for b in cash]
        if reason is None and (not target or any(_decimal(v) is None or _currency(c) is None for v, c in values)):
            reason = 'invalid_data'
        eligible = [p for p in holdings if _stock(p)]
        if reason is not None or not eligible:
            account['valuation_reason'] = reason
            continue
        total = Decimal(0)
        for amount, currency in values:
            value = _decimal(amount)
            assert value is not None  # validated above before any account arithmetic
            if currency != target:
                if not fx_loaded:
                    fx_loaded = True
                    try:
                        bundle = fx_service.get_rates()
                    except Exception:
                        bundle = None
                rates = (bundle or {}).get('rates', {})
                source_rate = Decimal(1) if currency == 'TWD' else _decimal(rates.get(currency))
                target_rate = Decimal(1) if target == 'TWD' else _decimal(rates.get(target))
                if source_rate is None or target_rate is None or source_rate <= 0 or target_rate <= 0:
                    reason = 'fx_unavailable'
                    break
                value = value * source_rate / target_rate
            total += value
        if reason:
            account['valuation_reason'] = reason
            continue
        times = [p['valuation_as_of'] for p in eligible]
        account.update(balance_total=format(total, 'f'),
                       valuation_source='yahoo' if len(eligible) == len(holdings) else 'mixed',
                       valuation_as_of=min(times) if all(times) else None)
    return result
