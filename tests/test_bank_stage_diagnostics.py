from types import SimpleNamespace

import pytest

from backend.core import base
from backend.server import sync_runner as runner


class Synthetic(base.BankCrawler):
    SAFE_COLLECT_GUARDS = frozenset({'synthetic_guard'})

    def __post_init__(self):
        pass

    def login(self, page):
        return True

    def _shared_login(self, page):
        self._diagnostic_stage = 'login_ocr'
        raise RuntimeError('PRIVATE_SECRET')

    def collect(self, page, collector):
        return base.BankCollectResult(card_bill_facts_ok=False)

    def _credential_origin_allowed(self, page):
        return True

    def _enforce_session_freshness(self):
        pass

    def _build_fetch_kwargs(self):
        return {}

    def _execute_browser_flow(self, login_url, *, page_action, **kwargs):
        page_action(SimpleNamespace(on=lambda *a: None, remove_listener=lambda *a: None))


@pytest.mark.parametrize('bank', sorted(runner.SUPPORTED_BANKS))
def test_run_dispatch_stored_stage(monkeypatch, bank):
    from backend.core import store
    from backend.server import rules_repo, card_events
    crawler = Synthetic(name='cathay')
    monkeypatch.setattr(runner, '_load_crawler', lambda bank: (SimpleNamespace(BASE='offline'), lambda: crawler))
    monkeypatch.setattr(rules_repo, 'list_rules', lambda **kw: [])
    monkeypatch.setattr(store, 'BankStore', lambda *a, **kw: SimpleNamespace(
        latest_twd_transaction_dates=lambda: {}, latest_card_transaction_dates=lambda: {}, close=lambda: None))
    monkeypatch.setattr(card_events, 'snapshot_cards', lambda **kw: [])
    row = dict(user_id=1, bank=bank, history_mode='full')
    monkeypatch.setattr(runner, 'get_job', lambda _: row)
    monkeypatch.setattr(runner.sync_jobs_repo, 'claim_queued', lambda _: True)
    monkeypatch.setattr(runner.sync_jobs_repo, 'mark_failed', lambda _, text: row.update(error_msg=text))
    monkeypatch.setattr(runner, '_send_sync_notification', lambda **kw: None)
    assert runner._exec_sync(1)
    assert ';stage=login_ocr;code=login_ocr_failed' in row['error_msg']
    assert 'PRIVATE_SECRET' not in row['error_msg']


@pytest.mark.parametrize('boundary,stage', [('session','session'), ('launch','browser_launch'), ('attach','browser_navigation'), ('collect','collect'), ('detach','cleanup')])
def test_outer_boundaries_and_reset(monkeypatch, boundary, stage):
    crawler = Synthetic(name='cathay')
    crawler._diagnostic_stage = 'login_ocr'
    monkeypatch.setattr(crawler, '_shared_login', lambda page: True)
    monkeypatch.setattr(crawler, 'logout', lambda page: None)
    def fail(*a, **kw):
        raise RuntimeError('PRIVATE_SECRET')
    if boundary == 'session':
        monkeypatch.setattr(crawler, '_enforce_session_freshness', fail)
    elif boundary == 'launch':
        monkeypatch.setattr(crawler, '_execute_browser_flow', fail)
    elif boundary == 'attach':
        monkeypatch.setattr(base.ResponseCollector, 'attach', fail)
    elif boundary == 'collect':
        monkeypatch.setattr(crawler, 'collect', fail)
    else:
        monkeypatch.setattr(base.ResponseCollector, 'detach', fail)
    if boundary == 'collect':
        result = crawler.run('offline')
        assert result['error_diagnostics']['stage'] == stage
        assert 'PRIVATE_SECRET' not in result['error']
    else:
        with pytest.raises(RuntimeError) as caught:
            crawler.run('offline')
        assert caught.value.error_diagnostics['stage'] == stage


def test_cleanup_keeps_login_cause(monkeypatch):
    crawler = Synthetic(name='cathay')
    def fail(*a):
        raise ValueError('PRIVATE_SECRET')
    monkeypatch.setattr(base.ResponseCollector, 'detach', fail)
    assert crawler.run('offline')['error_diagnostics']['stage'] == 'login_ocr'


def test_credentials_tagged_without_source_fallback(monkeypatch):
    from backend.core.creds import BankCreds
    from backend.core.error_diagnostics import format_failure
    monkeypatch.delenv('BANK_CRAWLER_ACCOUNT_ID', raising=False)
    monkeypatch.delenv('BANK_CRAWLER_USER_ID', raising=False)
    def fail(cls):
        raise ValueError('PRIVATE_SECRET')
    monkeypatch.setattr(BankCreds, 'from_env', classmethod(fail))
    with pytest.raises(ValueError) as caught:
        BankCreds.load()
    assert ';stage=credentials;code=credentials_failed' in format_failure(caught.value)


def test_hostile_metadata_is_not_executed():
    from backend.core.error_diagnostics import validate_diagnostics, result_failure
    class Hostile(str):
        def __hash__(self):
            raise AssertionError('hash called')
        def __eq__(self, other):
            raise AssertionError('eq called')
        def __bool__(self):
            raise AssertionError('bool called')
    class HostileDict(dict):
        def get(self, *a):
            raise AssertionError('get called')
    for value in [HostileDict(stage='login_ocr'), {'stage': Hostile('login_ocr'), 'code': 'login_ocr_failed'}, {'stage':'login_ocr','code':Hostile('login_ocr_failed')}]:
        assert validate_diagnostics(value)['stage'] == 'unknown'
    assert result_failure({'error': Hostile('secret')}) is not None


@pytest.mark.parametrize('where,stage', [('init','init'), ('store','init'), ('cursor','init'), ('coverage','coverage'), ('persist','persist'), ('stats','persist_summary'), ('close','cleanup')])
def test_dispatch_fallbacks(monkeypatch, where, stage):
    from backend.core import store, persist
    from backend.core.error_diagnostics import format_failure
    from backend.server import rules_repo
    def fail(*a, **kw):
        raise ValueError('PRIVATE_SECRET')
    crawler = Synthetic(name='cathay')
    crawler.HISTORY_COVERAGE_REQUIRED = True
    monkeypatch.setattr(crawler, 'run', lambda **kw: {'data': {}})
    monkeypatch.setattr(runner, '_load_crawler', fail if where == 'init' else lambda bank: (SimpleNamespace(BASE='offline'), lambda: crawler))
    monkeypatch.setattr(rules_repo, 'list_rules', lambda **kw: [])
    fake_store = SimpleNamespace(latest_twd_transaction_dates=fail if where == 'cursor' else lambda: {}, latest_card_transaction_dates=lambda: {}, stats=fail if where == 'stats' else lambda: {}, close=fail if where == 'close' else lambda: None)
    monkeypatch.setattr(store, 'BankStore', fail if where == 'store' else lambda *a, **kw: fake_store)
    monkeypatch.setattr(base, 'validate_history_coverage', fail if where == 'coverage' else lambda *a, **kw: {})
    monkeypatch.setattr(persist, 'persist_collected', fail if where == 'persist' else lambda *a, **kw: {})
    with pytest.raises(ValueError) as caught:
        runner._dispatch_crawler_and_persist('cathay', 1)
    assert ';stage=' + stage + ';' in format_failure(caught.value)


def test_cli_uses_safe_stage(monkeypatch, capsys):
    from cli import cli
    crawler = Synthetic(name='cathay')
    monkeypatch.setattr(cli, '_get_crawler', lambda bank: (crawler, 'offline'))
    monkeypatch.setattr(cli, 'BankStore', lambda bank: SimpleNamespace(latest_twd_transaction_dates=lambda: {}, latest_card_transaction_dates=lambda: {}, close=lambda: None))
    assert cli.cmd_sync(SimpleNamespace(bank='cathay', headless=True)) == 1
    assert ';stage=login_ocr;code=login_ocr_failed' in capsys.readouterr().out


def test_checkpoint_and_native_details_survive_run(monkeypatch):
    from backend.core.login_checkpoints import CheckpointOutcome, CheckpointKind, CheckpointReason, CheckpointPhase, LoginBudget, reduce_login_checkpoint
    crawler = Synthetic(name='cathay')
    def blocked(page):
        crawler._diagnostic_stage = 'login_checkpoint'
        reduce_login_checkpoint(CheckpointPhase.PRE_SUBMIT, LoginBudget(), CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, reason=CheckpointReason.NO_MATCHING_CHECKPOINT))
    monkeypatch.setattr(crawler, '_shared_login', blocked)
    detail = crawler.run('offline')['error_diagnostics']
    assert detail['reason'] == 'no_matching_checkpoint'
    assert detail['kind'] == 'unknown_blocker'
    crawler.name = 'sinopac'
    def native(page):
        crawler._diagnostic_stage = 'login_ocr'
        exc = RuntimeError('PRIVATE_SECRET')
        exc.code = 'captcha_invalid'
        exc.login_stage = 'captcha_ocr'
        raise exc
    monkeypatch.setattr(crawler, '_shared_login', native)
    detail = crawler.run('offline')['error_diagnostics']
    assert detail['native_code'] == 'captcha_invalid'
    assert detail['native_stage'] == 'captcha_ocr'


@pytest.mark.parametrize('oversized', [False, True])
def test_legacy_result_keeps_unknown_fallback(monkeypatch, oversized):
    from backend.core import store
    from backend.core.error_diagnostics import format_failure
    from backend.server import rules_repo
    crawler = Synthetic(name='cathay')
    crawler._diagnostic_stage = 'login_ocr'
    result = {'error': 'PRIVATE_SECRET'}
    if oversized:
        result.update({str(i): i for i in range(128)})
    monkeypatch.setattr(crawler, 'run', lambda **kw: result)
    monkeypatch.setattr(runner, '_load_crawler', lambda bank: (SimpleNamespace(BASE='offline'), lambda: crawler))
    monkeypatch.setattr(rules_repo, 'list_rules', lambda **kw: [])
    monkeypatch.setattr(store, 'BankStore', lambda *a, **kw: SimpleNamespace(latest_twd_transaction_dates=lambda: {}, latest_card_transaction_dates=lambda: {}, close=lambda: None))
    with pytest.raises(RuntimeError) as caught:
        runner._dispatch_crawler_and_persist('cathay', 1)
    assert format_failure(caught.value) == 'sync_failed:RuntimeError;stage=unknown;code=crawler_failed'


def test_rerun_success_and_nonfatal_logout(monkeypatch):
    crawler = Synthetic(name='cathay')
    assert crawler.run('offline')['error_diagnostics']['stage'] == 'login_ocr'
    monkeypatch.setattr(crawler, '_shared_login', lambda page: True)
    def fail(page):
        raise ValueError('PRIVATE_SECRET')
    monkeypatch.setattr(crawler, 'logout', fail)
    result = crawler.run('offline')
    assert 'error' not in result
    assert 'error_diagnostics' not in result


@pytest.mark.parametrize('cleanup_fails', [False, True])
def test_escaped_page_error_preserves_original(monkeypatch, cleanup_fails):
    crawler = Synthetic(name='cathay')
    original = RuntimeError('PRIVATE_SECRET')
    def fail(page):
        raise original
    def detach(page_self, page):
        if cleanup_fails:
            raise ValueError('PRIVATE_CLEANUP')
    monkeypatch.setattr(crawler, 'attach_shared_dialog_handler', fail)
    monkeypatch.setattr(base.ResponseCollector, 'detach', detach)
    with pytest.raises(RuntimeError) as caught:
        crawler.run('offline')
    assert caught.value is original
    assert original.error_diagnostics['stage'] == 'browser_navigation'


@pytest.mark.parametrize('collect', [False, True])
def test_standard_safe_stderr_has_stage_code(monkeypatch, capsys, collect):
    crawler = Synthetic(name='cathay')
    if collect:
        monkeypatch.setattr(crawler, '_shared_login', lambda page: True)
        monkeypatch.setattr(crawler, 'logout', lambda page: None)
        def fail(page, collector):
            raise RuntimeError('PRIVATE_SECRET')
        monkeypatch.setattr(crawler, 'collect', fail)
    crawler.run('offline')
    text = capsys.readouterr().err
    stage = 'collect' if collect else 'login_ocr'
    assert ';stage=' + stage + ';code=' + stage + '_failed' in text
    assert 'PRIVATE_SECRET' not in text
    assert len(text) < 1000


def test_registry_is_thirteen():
    from cli.cli import BANKS
    assert len(runner.SUPPORTED_BANKS) == 13
    assert runner.SUPPORTED_BANKS == BANKS == runner._CRAWLER_MODULE_MAP.keys()
