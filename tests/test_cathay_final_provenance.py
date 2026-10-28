"""Offline Cathay navigation carriers at original mandatory failure sites."""
from types import SimpleNamespace

import pytest

import backend.banks.cathay as module
from backend.banks.cathay import CathayCrawler, _ACCOUNT_INVENTORY_PATH
from backend.core.base import ApiHit, ResponseCollector
from backend.core.error_codes import exception_error_id
from tests.test_ctbc_final_provenance import SubmitPage, run_sink

from tests.test_ordered_ctbc_cathay_codes import original_banks as original_banks


def inventory(rows=(), status=200):
    return ApiHit(url='https://www.cathaybk.com.tw' + _ACCOUNT_INVENTORY_PATH,
                  method='POST', status=status, req_body={'content': {'queryType': 'TWD'}},
                  resp_json={'success': True, 'returnCode': '0000', 'content': {'datas': list(rows)}})


@pytest.mark.parametrize('case', ['missing', 'invalid', 'recover', 'both', 'repeat'])
def test_inventory_navigation_original_links_first_failure_and_recovery(monkeypatch, original_banks, case):
    outcomes = []
    for namespace in (original_banks['cathay'], vars(module)):
        monkeypatch.setitem(namespace, 'FEATURE_PAGES', {'asset': '/asset', 'twd_txn': '/twd'})
        crawler = object.__new__(namespace['CathayCrawler'])
        crawler.name, crawler.transaction_cursors = 'cathay', {}
        collector = ResponseCollector()
        if case in ('invalid', 'recover'):
            collector.hits.append(inventory(status=503 if case == 'invalid' else 200))
        first, later = ValueError('PRIVATE_SENTINEL first'), LookupError('PRIVATE_SENTINEL later')
        events = []
        def goto(url, **kwargs):
            events.append(('goto', url, kwargs))
            if url.endswith('/asset'):
                raise first
            if case == 'both':
                raise later
        page = SimpleNamespace(goto=goto, wait_for_timeout=lambda ms: events.append(('wait', ms)))
        crawler._collect_card_details = lambda page: events.append(('cards',))
        crawler._parse = lambda collector: {}
        collect = crawler.collect
        if case == 'recover':
            value = collect(page, collector)
            outcome = ('ok', value.twd_transactions, value.history_coverage)
            assert value.twd_transactions == []
        else:
            result, terminals = run_sink(monkeypatch, crawler, page, collector, lambda: collect(page, collector))
            terminal, = terminals
            assert (terminal.__cause__, terminal.__context__, terminal.__suppress_context__) == (None, None, False)
            assert str(terminal) == 'cathay-twd-history-account-inventory'
            outcome = ('error', type(terminal).__name__, str(terminal))
            if namespace is vars(module):
                code = 'CATHAY-04-010-004' if case == 'invalid' else 'CATHAY-04-500-001'
                assert result['collect_diagnostics']['error_id'] == code
                assert 'PRIVATE_SENTINEL' not in repr(result)
                if case != 'invalid':
                    assert f'[{code}] 開啟資產總覽：瀏覽器原生操作失敗' in result['error']
        assert (first.__cause__, first.__context__, first.__suppress_context__) == (None, None, False)
        outcomes.append((outcome, list(events)))
        if case == 'repeat' and namespace is vars(module):
            page.goto = lambda *a, **k: None
            with pytest.raises(RuntimeError) as caught:
                collect(page, ResponseCollector())
            assert exception_error_id(caught.value, bank='cathay') == 'CATHAY-04-010-001'
    assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize('label,returned', [(label, value) for label in ('帳號', '查詢期間')
                                          for value in ('no-group', 'no-input', 'unexpected')])
def test_query_navigation_original_links_at_real_seed_construction(monkeypatch, original_banks, label, returned):
    outcomes = []
    for namespace in (original_banks['cathay'], vars(module)):
        monkeypatch.setitem(namespace, 'FEATURE_PAGES', {'twd_txn': '/twd'})
        crawler = object.__new__(namespace['CathayCrawler'])
        crawler.name, crawler.transaction_cursors = 'cathay', {}
        collector = ResponseCollector()
        collector.hits.append(inventory([{'currency': 'TWD', 'accountNo': 'synthetic'}]))
        failure, events = ValueError('PRIVATE_SENTINEL'), []
        def goto(*args, **kwargs):
            events.append(('goto', args, kwargs))
            raise failure
        def evaluate(script, arg=None):
            events.append(('evaluate', script, arg))
            return 1 if arg is None else returned if arg == label else 'focused'
        page = SimpleNamespace(goto=goto, evaluate=evaluate,
                               wait_for_timeout=lambda ms: events.append(('wait', ms)),
                               keyboard=SimpleNamespace(press=lambda key: events.append(('press', key))))
        collect = crawler.collect
        result, terminals = run_sink(monkeypatch, crawler, page, collector, lambda: collect(page, collector))
        terminal, = terminals
        assert (terminal.__cause__, terminal.__context__, terminal.__suppress_context__) == (None, None, False)
        assert str(terminal) == ('cathay-twd-history-account-control' if label == '帳號'
                                 else 'cathay-twd-history-period-control')
        assert len([e for e in events if e[0] == 'goto']) == 1
        outcomes.append((type(terminal).__name__, str(terminal), events))
        if namespace is vars(module):
            assert result['collect_diagnostics']['error_id'] == 'CATHAY-05-500-001'
            assert '[CATHAY-05-500-001] 開啟台幣交易查詢：瀏覽器原生操作失敗' in result['error']
            assert 'PRIVATE_SENTINEL' not in repr(result)
    assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize('kind', ['cancel', 'base'])
@pytest.mark.parametrize('operation', ['goto', 'wait', 'evaluate'])
def test_nonauth_cancellation_never_becomes_navigation_carrier(monkeypatch, original_banks, kind, operation):
    import asyncio
    outcomes = []
    for namespace in (original_banks['cathay'], vars(module)):
        monkeypatch.setitem(namespace, 'FEATURE_PAGES', {'twd_txn': '/twd'})
        crawler = object.__new__(namespace['CathayCrawler'])
        failure = asyncio.CancelledError() if kind == 'cancel' else BaseException('PRIVATE_SENTINEL')
        collector = ResponseCollector()
        collector.hits.append(inventory([{'currency': 'TWD', 'accountNo': 'synthetic'}]))
        events = []
        def native(name, result=None):
            def call(*args, **kwargs):
                events.append((name, args, kwargs))
                if name == operation:
                    raise failure
                return result
            return call
        page = SimpleNamespace(goto=native('goto'), wait_for_timeout=native('wait'), evaluate=native('evaluate', 1))
        with pytest.raises(type(failure)) as caught:
            crawler.collect(page, collector)
        assert caught.value is failure
        assert exception_error_id(failure, bank='cathay') is None
        outcomes.append(events)
    assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize('code', [None, 'CTBC-05-300-001', 'CATHAY-99-999-999', 'PRIVATE_SENTINEL'])
def test_private_navigation_metadata_foreign_and_unknown_fallback(code):
    with pytest.raises(RuntimeError) as caught:
        CathayCrawler._twd_account_inventory(ResponseCollector(), _navigation_id=code)
    assert exception_error_id(caught.value, bank='cathay') == 'CATHAY-04-010-001'
    page = SimpleNamespace(wait_for_timeout=lambda ms: None,
                           evaluate=lambda script, arg=None: 1 if arg is None else 'no-group',
                           keyboard=SimpleNamespace(press=lambda key: None))
    with pytest.raises(RuntimeError) as caught:
        object.__new__(CathayCrawler)._seed_twd_query_templates(page, ResponseCollector(), _navigation_id=code)
    assert exception_error_id(caught.value, bank='cathay') == 'CATHAY-05-010-005'


def test_invocation_local_carriers_are_only_validated_ids(monkeypatch):
    import sys
    monkeypatch.setattr(module, 'FEATURE_PAGES', {'asset': '/asset', 'twd_txn': '/twd'})
    crawler = object.__new__(CathayCrawler)
    failure = ValueError('PRIVATE_SENTINEL')
    captured = []
    target = CathayCrawler.collect.__code__
    def profile(frame, event, arg):
        if frame.f_code is target and event == 'return':
            captured.append(dict(frame.f_locals))
    def goto(*args, **kwargs):
        raise failure
    page = SimpleNamespace(goto=goto, wait_for_timeout=lambda ms: None)
    previous = sys.getprofile()
    try:
        sys.setprofile(profile)
        with pytest.raises(RuntimeError):
            crawler.collect(page, ResponseCollector())
    finally:
        sys.setprofile(previous)
    locals_, = captured
    assert locals_['inventory_navigation_id'] == 'CATHAY-04-500-001'
    # The same injected exception is reused: its first registered ID stays primary.
    assert locals_['stage_navigation_id'] == 'CATHAY-04-500-001'
    assert not any(isinstance(v, BaseException) for v in locals_.values())
    assert not any('navigation_error' in key for key in locals_)
    assert not any('navigation' in key for key in crawler.__dict__)


@pytest.mark.parametrize('terminal', ['query', 'seed'])
def test_recovered_controls_do_not_leak_navigation_to_later_failures(monkeypatch, original_banks, terminal):
    outputs = []
    for namespace in (original_banks['cathay'], vars(module)):
        monkeypatch.setitem(namespace, 'FEATURE_PAGES', {'twd_txn': '/twd'})
        crawler = object.__new__(namespace['CathayCrawler'])
        collector = ResponseCollector()
        collector.hits.append(inventory([{'currency': 'TWD', 'accountNo': 'synthetic'}]))
        events = []
        def goto(*args, **kwargs):
            events.append(('goto', args, kwargs))
            raise ValueError('PRIVATE_SENTINEL')
        def evaluate(script, arg=None):
            events.append(('evaluate', script, arg))
            if arg is not None:
                return 'focused'
            if 'react-select' in script:
                return 1
            return terminal == 'seed'
        page = SimpleNamespace(goto=goto, evaluate=evaluate,
                               wait_for_timeout=lambda ms: events.append(('wait', ms)),
                               keyboard=SimpleNamespace(press=lambda key: events.append(('press', key))))
        with pytest.raises(RuntimeError) as caught:
            crawler.collect(page, collector)
        error = caught.value
        assert (error.__cause__, error.__context__, error.__suppress_context__) == (None, None, False)
        if namespace is vars(module):
            assert exception_error_id(error, bank='cathay') == ('CATHAY-05-010-003' if terminal == 'query' else 'CATHAY-05-010-004')
        outputs.append((str(error), events))
    assert outputs[0] == outputs[1]


class CathaySubmitPage(SubmitPage):
    @property
    def first(self):
        self.events.append(('first', (), {}))
        if len(self.events) == self.fail_at:
            raise self.failure
        return self


@pytest.mark.parametrize('sdk', ['patchright', 'playwright'])
@pytest.mark.parametrize('at,code,action', [
    (1, 'CATHAY-02-100-001', '定位登入按鈕'),
    (2, 'CATHAY-02-101-001', '讀取登入按鈕數量'),
    (3, 'CATHAY-02-102-001', '取得唯一登入按鈕'),
    (4, 'CATHAY-02-103-001', '讀取登入按鈕樣式'),
    (5, 'CATHAY-02-104-001', '檢查登入按鈕可見性'),
    (6, 'CATHAY-02-105-001', '檢查登入按鈕可用性'),
    (7, 'CATHAY-02-106-001', '送出登入'),
])
def test_submit_native_leaf_preserves_original_links_and_budget(original_banks, sdk, at, code, action):
    from importlib import import_module
    from backend.core import base

    outputs = []
    for namespace in (original_banks['cathay'], vars(module)):
        native = import_module(sdk + '._impl._helper').parse_error({
            'name': 'Error', 'message': 'PRIVATE_SENTINEL', 'stack': 'offline stack',
        })
        page = CathaySubmitPage(at, native)
        with pytest.raises(namespace['CathayLoginError']) as caught:
            namespace['_click_login_once'](page)
        error = caught.value
        assert (error.__cause__, error.__context__, error.__suppress_context__) == (None, native, True)
        assert (native.__cause__, native.__context__, native.__suppress_context__) == (None, None, False)
        assert [e[0] for e in page.events] == [
            'locator', 'count', 'first', 'get_attribute', 'is_visible', 'is_enabled', 'click',
        ][:at]
        if at == 7:
            assert page.events[-1] == ('click', (), {'timeout': 8000})
        outputs.append((type(error).__name__, str(error), page.events))
        if namespace is vars(module):
            crawler = object.__new__(CathayCrawler)
            crawler.name = 'cathay'
            diagnostic = base._failure_diagnostics(crawler, error, '02-900-001')
            assert diagnostic['error_id'] == code
            text = base._format_failure_diagnostics(diagnostic)
            assert f'[{code}] {action}：瀏覽器原生操作失敗' in text
            assert 'PRIVATE_SENTINEL' not in repr(diagnostic) + text
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize('at', [1, 7])
@pytest.mark.parametrize('kind', ['cancel', 'base'])
def test_submit_fatal_propagation_is_original(original_banks, at, kind):
    import asyncio

    traces = []
    for namespace in (original_banks['cathay'], vars(module)):
        native = asyncio.CancelledError() if kind == 'cancel' else BaseException('PRIVATE_SENTINEL')
        page = CathaySubmitPage(at, native)
        with pytest.raises(type(native)) as caught:
            namespace['_click_login_once'](page)
        assert caught.value is native
        assert exception_error_id(native, bank='cathay') is None
        traces.append(page.events)
    assert traces[0] == traces[1]


def test_submit_success_keeps_single_fake_click(original_banks):
    traces = []
    for namespace in (original_banks['cathay'], vars(module)):
        page = CathaySubmitPage(None, None)
        assert namespace['_click_login_once'](page) is None
        assert len(page.events) == 7
        assert sum(event[0] == 'click' for event in page.events) == 1
        traces.append(page.events)
    assert traces[0] == traces[1]


@pytest.mark.parametrize('at,fallback', [(1, 'CATHAY-02-050-005'), (7, 'CATHAY-02-050-006')])
@pytest.mark.parametrize('mode', ['first', 'foreign', 'untagged'])
def test_submit_injected_carrier_priority_and_legacy_fallback(monkeypatch, at, fallback, mode):
    from backend.core.error_codes import with_error_code

    native = ValueError('PRIVATE_SENTINEL')
    prior = 'CATHAY-02-101-001' if mode == 'first' else 'CTBC-02-100-001'
    if mode != 'untagged':
        with_error_code(native, prior)
    # Injected carrier/fallback contract, not evidence of a natural native producer.
    if mode == 'untagged':
        monkeypatch.setattr(module, '_native_step', lambda code, operation: operation())
    with pytest.raises(module.CathayLoginError) as caught:
        module._click_login_once(CathaySubmitPage(at, native))
    assert exception_error_id(caught.value, bank='cathay') == (prior if mode == 'first' else fallback)
    assert (caught.value.__cause__, caught.value.__context__, caught.value.__suppress_context__) == (None, native, True)
