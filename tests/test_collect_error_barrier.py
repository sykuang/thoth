"""Offline regression for the shared collect-result publication barrier."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.core import base


class SyntheticCrawler(base.BankCrawler):
    payload: base.BankCollectResult
    HISTORY_COVERAGE_REQUIRED = True
    HISTORY_COVERAGE_DOMAINS = frozenset({'twd_transactions'})

    def __post_init__(self):
        pass

    def login(self, page):
        return True

    def _shared_login(self, page):
        return True

    def _credential_origin_allowed(self, page):
        return True

    def _enforce_session_freshness(self):
        pass

    def _build_fetch_kwargs(self):
        return {}

    def _execute_browser_flow(self, login_url, *, page_action, **kwargs):
        page_action(SimpleNamespace(on=lambda *a: None, remove_listener=lambda *a: None))

    def collect(self, page, collector):
        return self.payload


class Hostile(str):
    def __bool__(self):
        raise AssertionError('synthetic sentinel bool')

    def __eq__(self, other):
        raise AssertionError('synthetic sentinel eq')

    def __str__(self):
        raise AssertionError('synthetic sentinel str')


@pytest.fixture
def crawler():
    value = SyntheticCrawler(name='cathay')
    coverage = {'mode': 'full', 'domains': [{
        'domain': 'twd_transactions', 'expected': [], 'windows': [],
        'empty_window': {'start': '2026-01-01', 'end': '2026-01-01',
                         'status': 'explicit_empty', 'pages': 1},
    }]}
    base.validate_history_coverage(coverage, expected_mode='full',
                                  expected_domains=value.HISTORY_COVERAGE_DOMAINS)
    value.payload = base.BankCollectResult(
        error='synthetic sentinel', history_coverage=coverage, card_bill_facts_ok=False,
    )
    value.logout = Mock()
    return value


@pytest.mark.parametrize('error', ['synthetic sentinel', False, 0, [], {}, Hostile('')],
                         ids=['text', 'false', 'zero', 'list', 'dict', 'hostile'])
def test_collect_error_is_sanitized_before_serialization(crawler, error, capsys):
    crawler.payload.error = error
    crawler.payload.to_dict = Mock(side_effect=AssertionError('must not serialize'))
    result = crawler.run('offline')
    assert result['error'] == 'collect_failed: ValueError: code=collect_contract'
    assert 'data' not in result
    assert set(result) == {'error', 'collect_diagnostics'}
    assert result['collect_diagnostics'] == {'exception': 'ValueError', 'code': 'collect_contract'}
    crawler.payload.to_dict.assert_not_called()
    crawler.logout.assert_called_once()
    assert 'synthetic sentinel' not in repr(result) + capsys.readouterr().err


@pytest.mark.parametrize('error', [None, ''])
def test_empty_error_preserves_success(crawler, error):
    crawler.payload.error = error
    crawler.payload.credit_card_parsed = {'error': 'synthetic sentinel'}
    crawler.payload.twd_txn_error = 'synthetic sentinel'
    result = crawler.run('offline')
    assert 'error' not in result
    assert result['data']['credit_card_parsed'] == {'error': 'synthetic sentinel'}
    assert result['data']['history_coverage'] == crawler.payload.history_coverage
    crawler.logout.assert_called_once()


@pytest.mark.parametrize('consumer', ['server', 'cli'])
def test_consumers_stop_before_coverage_persist_and_cursor_write(monkeypatch, crawler, consumer, capsys):
    from backend.core import persist, store
    from backend.server import rules_repo, sync_runner
    from cli import cli

    fake_store = SimpleNamespace(
        latest_twd_transaction_dates=lambda: {}, latest_card_transaction_dates=lambda: {},
        close=Mock(), stats=Mock(), record_history_coverage_cursors=Mock(),
    )
    persist_call = Mock(side_effect=AssertionError('must not persist or advance cursor'))
    coverage_call = Mock(side_effect=AssertionError('must not reach coverage'))
    monkeypatch.setattr(persist, 'persist_collected', persist_call)
    monkeypatch.setattr(base, 'validate_history_coverage', coverage_call)
    monkeypatch.setattr(rules_repo, 'list_rules', lambda **kw: [])
    monkeypatch.setattr(store, 'BankStore', lambda *a, **kw: fake_store)
    monkeypatch.setattr(cli, 'BankStore', lambda *a, **kw: fake_store)
    monkeypatch.setattr(sync_runner, '_load_crawler', lambda bank: (
        SimpleNamespace(BASE='offline'), lambda: crawler))
    monkeypatch.setattr(cli, '_get_crawler', lambda bank: (crawler, 'offline'))
    if consumer == 'server':
        with pytest.raises(RuntimeError) as caught:
            sync_runner._dispatch_crawler_and_persist('cathay', 1)
        text = str(caught.value)
    else:
        assert cli.cmd_sync(SimpleNamespace(bank='cathay', headless=True)) == 1
        text = capsys.readouterr().out
    assert 'crawler_failed' in text
    assert 'synthetic sentinel' not in text
    coverage_call.assert_not_called()
    persist_call.assert_not_called()
    fake_store.record_history_coverage_cursors.assert_not_called()
    fake_store.stats.assert_not_called()
    fake_store.close.assert_called_once()
    crawler.logout.assert_called_once()
