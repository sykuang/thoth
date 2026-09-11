from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('close_fails', [False, True])
def test_cli_closes_before_success_output(monkeypatch, capsys, close_fails):
    from cli import cli
    from backend.core import persist
    from backend.server import rules_repo
    events = []
    def close():
        events.append('close')
        if close_fails:
            raise OSError('synthetic_private_close_detail')
    class Stats(dict):
        def items(self):
            assert events == ['close']
            return super().items()
    crawler = SimpleNamespace(HISTORY_COVERAGE_REQUIRED=False,
                              configure_transaction_cursor=lambda *a: None,
                              run=lambda **kw: {'data': {}})
    store = SimpleNamespace(latest_twd_transaction_dates=lambda: {},
                            latest_card_transaction_dates=lambda: {},
                            stats=lambda: Stats(), close=close, db_path='synthetic-db')
    monkeypatch.setattr(cli, '_get_crawler', lambda bank: (crawler, 'offline'))
    monkeypatch.setattr(cli, 'BankStore', lambda bank: store)
    monkeypatch.setattr(cli, '_write_private_json', lambda *a: None)
    monkeypatch.setattr(rules_repo, 'list_rules', lambda **kw: [])
    monkeypatch.setattr(persist, 'persist_collected', lambda *a, **kw: {})
    result = cli.cmd_sync(SimpleNamespace(bank='cathay', headless=True))
    output = capsys.readouterr().out
    assert result == (1 if close_fails else 0)
    assert events == ['close']
    assert ('增量同步結果' in output) is not close_fails
    if close_fails:
        assert 'stage=cleanup;code=cleanup_failed' in output
    assert 'synthetic_private_close_detail' not in output
