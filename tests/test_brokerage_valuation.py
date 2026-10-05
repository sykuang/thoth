from copy import deepcopy
from decimal import Decimal
from typing import Any

import pytest

from backend.server import db, fx_service, yahoo_finance
from backend.server.snaptrade import SnapTradeService


def raw_snapshot() -> dict[str, Any]:
    return dict(accounts=[dict(id='a', name='Broker', institution_name='Broker', balance_total='999', balance_currency='USD', holdings_unavailable=False, synced_at='2026-08-01')], balances=[dict(account_id='a', currency='USD', cash='-10')], positions=[dict(account_id='a', provider_symbol_id='s', symbol='NVDA', asset_type='cs', quantity='2.5', price='4', market_value='10', average_cost='3', currency='USD')], activities=[], last_synced_at='2026-08-01')


def quote(symbol='NVDA', **updates):
    values: dict[str, Any] = dict(symbol=symbol, name=symbol, currency='USD', exchange_name=None, quote_type='EQUITY', regular_market_price='100.25', regular_market_time=1786080601)
    return yahoo_finance.YahooQuote(**(values | updates))


@pytest.fixture
def market(monkeypatch):
    calls = []
    monkeypatch.setattr(yahoo_finance, 'get_quote', lambda symbol: calls.append(symbol) or quote(symbol))
    monkeypatch.setattr(fx_service, 'get_rates', lambda: pytest.fail('unexpected FX request'))
    return calls


def project(monkeypatch, raw):
    monkeypatch.setattr(db, 'snaptrade_snapshot', lambda _: raw)
    return SnapTradeService.snapshot(1)


def test_read_time_stock_revaluation_preserves_raw(monkeypatch, market):
    raw = raw_snapshot()
    before = deepcopy(raw)
    result = project(monkeypatch, raw)
    assert result['positions'][0]['price'] == '100.25'
    assert result['positions'][0]['market_value'] == '250.625'
    assert result['accounts'][0]['balance_total'] == '240.625'
    assert result['accounts'][0]['valuation_source'] == 'yahoo'
    assert result['accounts'][0]['valuation_as_of'] == '2026-08-07T05:30:01+00:00'
    assert result['accounts'][0]['valuation_reason'] is None
    assert result['positions'][0]['average_cost'] == '3'
    assert raw == before
    assert project(monkeypatch, raw) == result
    assert market == ['NVDA', 'NVDA']


@pytest.mark.parametrize('case', ['short', 'option', 'missing_cash', 'empty_positions', 'holdings_unavailable', 'bad_quantity', 'bad_value', 'bad_currency', 'quote_failure', 'symbol', 'quote_type', 'currency', 'missing_time', 'fx', 'fx_missing', 'no_stocks', 'partial', 'empty', 'alias', 'dedupe'])
def test_conservative_projection(monkeypatch, market, case):
    raw = raw_snapshot()
    p = raw['positions'][0]
    a = raw['accounts'][0]
    fallback = False
    if case == 'short': p['quantity'] = '-0.5'
    if case == 'option': raw['positions'].append(p | dict(asset_type='OPTION', symbol='OPT', market_value='200', price='2', quantity='1'))
    if case == 'missing_cash': raw['balances'] = []; fallback = True
    if case == 'empty_positions': raw['positions'] = []; fallback = True
    if case == 'holdings_unavailable': a['holdings_unavailable'] = True; fallback = True
    if case == 'bad_quantity': p['quantity'] = 'NaN'; fallback = True
    if case == 'bad_value': raw['positions'].append(p | dict(asset_type='OPTION', market_value=None)); fallback = True
    if case == 'bad_currency': raw['balances'][0]['currency'] = None; fallback = True
    if case == 'quote_failure':
        def fail(_): raise RuntimeError('private error')
        monkeypatch.setattr(yahoo_finance, 'get_quote', fail)
        fallback = True
    if case in {'symbol', 'quote_type', 'currency', 'missing_time'}:
        changes = {'symbol': dict(symbol='OTHER'), 'quote_type': dict(quote_type='INDEX'), 'currency': dict(currency='EUR'), 'missing_time': dict(regular_market_time=None)}[case]
        monkeypatch.setattr(yahoo_finance, 'get_quote', lambda _: quote(**changes))
        fallback = case != 'missing_time'
    if case in {'fx', 'fx_missing'}:
        raw['balances'][0]['currency'] = 'EUR'
        monkeypatch.setattr(fx_service, 'get_rates', lambda: {'rates': {'USD': 32, 'EUR': 40}} if case == 'fx' else None)
        fallback = case == 'fx_missing'
    if case == 'no_stocks': p['asset_type'] = 'OPTION'; fallback = True
    if case == 'partial': raw.pop('positions'); raw.pop('balances'); fallback = True
    if case == 'empty': raw['accounts'] = []
    if case == 'alias': p['symbol'] = 'BRK.B'
    if case == 'dedupe': raw['positions'].append(p.copy())
    before = deepcopy(raw)
    result = project(monkeypatch, raw)
    assert raw == before
    if case == 'empty':
        assert result == raw and not market
        return
    account = result['accounts'][0]
    if fallback:
        assert account['balance_total'] == '999'
        assert account['valuation_source'] == 'broker_snapshot'
        assert bool(account['valuation_reason']) == (case != 'no_stocks')
    else:
        expected = {'short': '-60.125', 'option': '440.625', 'fx': '238.125', 'dedupe': '491.250'}.get(case, '240.625')
        assert Decimal(account['balance_total']) == Decimal(expected)
        assert account['valuation_source'] == ('mixed' if case == 'option' else 'yahoo')
    if case == 'missing_time': assert account['valuation_as_of'] is None
    if case == 'alias': assert market == ['BRK-B']
    if case == 'dedupe': assert market == ['NVDA']
    if case == 'option': assert result['positions'][1]['market_value'] == '200'


def test_status_does_not_value(monkeypatch, market):
    from backend.server import snaptrade
    monkeypatch.setattr(snaptrade, '_configured', lambda: False)
    monkeypatch.setattr(db, 'snaptrade_snapshot', lambda _: raw_snapshot())
    assert SnapTradeService().status(1)['last_synced_at'] == '2026-08-01'
    assert market == []


@pytest.mark.parametrize('currency', ['GBp', 'GBX'])
def test_minor_units(monkeypatch, currency):
    monkeypatch.setattr(yahoo_finance, '_get_json', lambda _: {'chart': {'result': [{'meta': dict(symbol='VOD.L', currency=currency, regularMarketPrice='125.5', instrumentType='EQUITY')}]}})
    yahoo_finance._get_quote_cached.cache_clear()
    result = yahoo_finance.get_quote('VOD.L')
    assert result.currency == 'GBP'
    assert result.regular_market_price == '1.255'


@pytest.mark.parametrize('path', ['/snaptrade/portfolio', '/financial-accounts', '/replica/bootstrap', '/portfolio/summary'])
def test_all_read_paths(client, monkeypatch, market, path):
    from backend.server.routers import portfolio
    monkeypatch.setattr(portfolio, 'KNOWN_BANKS', [])
    monkeypatch.setattr(db, 'snaptrade_snapshot', lambda _: raw_snapshot())
    monkeypatch.setattr(fx_service, 'get_rates', lambda: {'rates': {'USD': 32, 'TWD': 1}})
    monkeypatch.setattr(fx_service, 'convert_to_twd', lambda amount, currency: round(Decimal(amount) * 32))
    response = client.post('/auth/register', json={'email': 'valuation@example.com', 'password': 'SyntheticTestPassword02!'})
    headers = {'Authorization': 'Bearer ' + response.json()['token']}
    response = client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()
    if path == '/portfolio/summary':
        assert data['brokerage_assets_twd'] == 7700
        assert data['brokerage_valuation_incomplete'] is False
    elif path == '/financial-accounts':
        assert data[0]['balance'] == '240.625'
        assert data[0]['valuation_source'] == 'yahoo'
        assert data[0]['valuation_as_of'] == '2026-08-07T05:30:01+00:00'
    else:
        if path == '/replica/bootstrap':
            generation = data['generations']['brokerage']
            assert client.get(path, headers=headers).json()['generations']['brokerage'] == generation
            data = next(p['data'] for p in data['partitions'] if p['name'] == 'brokerage')
        assert data['accounts'][0]['balance_total'] == '240.625'
        assert data['positions'][0]['price'] == '100.25'


@pytest.mark.parametrize('failure', ['quote', 'conversion'])
def test_summary_marks_incomplete(monkeypatch, market, failure):
    from backend.server import financial_accounts
    from backend.server.routers import portfolio
    monkeypatch.setattr(portfolio, 'KNOWN_BANKS', [])
    monkeypatch.setattr(financial_accounts, 'list_manual_accounts', lambda _: [])
    monkeypatch.setattr(db, 'snaptrade_snapshot', lambda _: raw_snapshot())
    monkeypatch.setattr(fx_service, 'convert_to_twd', lambda *args: None if failure == 'conversion' else 123)
    if failure == 'quote': monkeypatch.setattr(yahoo_finance, 'get_quote', lambda _: None)
    result = portfolio._compute_portfolio_summary(1)
    assert result['brokerage_valuation_incomplete'] is True


def test_quote_requires_explicit_returned_symbol(monkeypatch):
    monkeypatch.setattr(yahoo_finance, '_get_json', lambda _: {'chart': {'result': [{'meta': dict(currency='USD', regularMarketPrice='100', instrumentType='EQUITY')}]}})
    yahoo_finance._get_quote_cached.cache_clear()
    with pytest.raises(yahoo_finance.YahooFinanceUnavailable):
        yahoo_finance.get_quote('NVDA')


def test_dashboard_fast_cache_keeps_valuation(monkeypatch, market):
    from backend.server import dashboard_cache, financial_accounts
    from backend.server.routers import portfolio
    monkeypatch.delenv('PYTEST_CURRENT_TEST', raising=False)
    dashboard_cache.clear_dashboard_cache()
    monkeypatch.setattr(portfolio, 'KNOWN_BANKS', [])
    monkeypatch.setattr(financial_accounts, 'list_manual_accounts', lambda _: [])
    monkeypatch.setattr(db, 'snaptrade_snapshot', lambda _: raw_snapshot())
    monkeypatch.setattr(fx_service, 'convert_to_twd', lambda amount, currency: round(Decimal(amount) * 32))
    first = portfolio.portfolio_summary({'id': 1})
    second = portfolio.portfolio_summary({'id': 1})
    assert first == second
    assert first['brokerage_assets_twd'] == 7700
    assert first['brokerage_valuation_incomplete'] is False
    assert market == ['NVDA']
    dashboard_cache.clear_dashboard_cache()
