"""Synthetic offline CTBC producers through real history/collect/run sinks."""
from datetime import date
from importlib import import_module
from types import SimpleNamespace

import pytest

from backend.banks.ctbc import CtbcCrawler
from backend.core import base
from backend.core.error_codes import exception_error_id, format_error_id
from tests.test_ctbc_history_sync import _FetchPage, _collector, _inventory_hit
from tests.test_ordered_ctbc_cathay_codes import NativeForm, OfflineScriptPage, _EXPECTED_FOLLOWUP_LABELS

from tests.test_ordered_ctbc_cathay_codes import original_banks as original_banks


def history(crawler, page, collector):
    deposit, identities = crawler._validated_twd_inventory(collector)
    return crawler._collect_twd_deposit_history(
        page, collector, deposit, expected_identities=identities, as_of=date(2026, 8, 30),
    )


def run_sink(monkeypatch, crawler, page, collector, operation):
    """Only browser/session boundaries are fake; typed run failure is production."""
    terminals = []
    def collect(*args):
        try:
            return operation()
        except Exception as error:
            terminals.append(error)
            raise
    monkeypatch.setattr(crawler, '_enforce_session_freshness', lambda: None)
    monkeypatch.setattr(crawler, '_build_fetch_kwargs', lambda: {})
    monkeypatch.setattr(crawler, '_make_collector', lambda page: collector)
    monkeypatch.setattr(collector, 'attach', lambda page: None)
    monkeypatch.setattr(collector, 'detach', lambda page: None)
    monkeypatch.setattr(crawler, 'attach_shared_dialog_handler', lambda page: None)
    monkeypatch.setattr(crawler, '_shared_login', lambda page: True)
    monkeypatch.setattr(crawler, '_ensure_collect_origin', lambda page: None)
    monkeypatch.setattr(crawler, '_credential_origin_allowed', lambda page: True)
    monkeypatch.setattr(crawler, 'collect', collect)
    monkeypatch.setattr(crawler, 'logout', lambda page: None)
    monkeypatch.setattr(crawler, '_execute_browser_flow', lambda url, **kw: kw['page_action'](page))
    return base.BankCrawler.run(crawler, 'offline:synthetic'), terminals


def crawler_for(cls=CtbcCrawler):
    crawler = object.__new__(cls)
    crawler.name = 'ctbc'
    crawler.transaction_cursors = {}
    return crawler


def assert_sink(result, terminal, code):
    assert type(terminal) is RuntimeError
    assert result['collect_diagnostics']['error_id'] == code
    assert format_error_id(code, bank='ctbc') in result['error']
    labels = {
        **_EXPECTED_FOLLOWUP_LABELS,
        'CTBC-05-200-001': ('開啟台幣歷史查詢頁', '瀏覽器原生操作失敗'),
        'CTBC-05-201-001': ('讀取台幣歷史查詢網址', '瀏覽器原生操作失敗'),
        'CTBC-05-300-001': ('讀取原生歷史表單網址', '瀏覽器原生操作失敗'),
        'CTBC-05-301-001': ('定位查詢起日欄位', '瀏覽器原生操作失敗'),
        'CTBC-07-030-002': ('篩選台幣交易明細', '交易項目不是物件'),
        'CTBC-07-030-003': ('篩選台幣交易明細', '交易時間缺失或為空'),
        'CTBC-07-020-003': ('驗證台幣交易明細', '交易時間格式不符'),
        'CTBC-07-020-004': ('驗證台幣交易明細', '交易時間日期無效'),
        'CTBC-05-020-022': ('收集完整台幣歷史', '月份查詢失敗'),
        'CTBC-05-020-023': ('收集完整台幣歷史', '交易明細含不合法資料'),
        'CTBC-04-010-004': ('讀取台幣帳戶清單', 'HTTP狀態未成功'),
        'CTBC-05-820-002': ('驗證歷史回應總頁數證明', '總頁數不等於一'),
        'CTBC-05-821-002': ('驗證歷史回應頁數證明', '頁數不等於一'),
        'CTBC-05-822-002': ('驗證歷史回應筆數證明', '筆數與明細不符'),
        'CTBC-05-823-002': ('驗證歷史回應總筆數證明', '總筆數與明細不符'),
    }
    action, reason = labels[code]
    assert f'[{code}] {action}：{reason}' in result['error']
    assert 'PRIVATE_SENTINEL' not in repr(result)


@pytest.mark.parametrize('sdk', ['patchright', 'playwright'])
def test_monthly_sdk_leaf_reaches_typed_run_sink(monkeypatch, sdk):
    # Actual installed parser, no SDK process/browser/channel launched.
    error = import_module(sdk + '._impl._helper').parse_error({
        'name': 'Error', 'message': 'PRIVATE_SENTINEL', 'stack': 'synthetic stack',
    })
    error.synthetic_property = object()
    properties = dict(error.__dict__)
    crawler = crawler_for()
    collector = _collector(['synthetic'])
    page = _FetchPage()
    events = []
    def evaluate(script, payload):
        events.append(payload['body']['rqData']['type'])
        raise error
    page.evaluate = evaluate
    result, terminals = run_sink(monkeypatch, crawler, page, collector,
                                 lambda: history(crawler, page, collector))
    terminal, = terminals
    assert str(terminal) == 'ctbc-twd-history-fetch'
    assert terminal.__cause__ is None
    assert terminal.__context__ is error
    assert terminal.__suppress_context__ is True
    assert error.__cause__ is None and error.__context__ is None
    assert error.__suppress_context__ is False
    assert all(error.__dict__[key] is value for key, value in properties.items())
    assert exception_error_id(error, bank='ctbc') == 'CTBC-05-400-001'
    assert events == ['m5']
    assert_sink(result, terminal, 'CTBC-05-400-001')


def test_cookie_native_form_leaf_reaches_monthly_run_sink(monkeypatch):
    crawler = crawler_for()
    page = NativeForm(fail_at=1)
    page.goto = lambda *a, **k: None
    page.wait_for_timeout = lambda ms: None
    collector = page.collector
    collector.hits.append(_inventory_hit(['synthetic']))
    result, terminals = run_sink(monkeypatch, crawler, page, collector,
                                 lambda: history(crawler, page, collector))
    terminal, = terminals
    assert str(terminal) == 'ctbc-twd-history-fetch'
    assert (terminal.__cause__, terminal.__context__, terminal.__suppress_context__) == (None, page.failure, True)
    assert page.events == ["locator:input[formcontrolname='startDt']"]
    assert_sink(result, terminal, 'CTBC-05-301-001')


@pytest.mark.parametrize('which', ['goto', 'url'])
def test_history_navigation_replacement_copies_current_native_id(monkeypatch, which):
    crawler = crawler_for()
    collector = _collector(['synthetic'])
    error = ValueError('PRIVATE_SENTINEL')
    events = []
    class Page:
        def goto(self, *args, **kwargs):
            events.append('goto')
            if which == 'goto':
                raise error
        @property
        def url(self):
            events.append('url')
            raise error
    page = Page()
    result, terminals = run_sink(monkeypatch, crawler, page, collector,
                                 lambda: history(crawler, page, collector))
    terminal, = terminals
    assert str(terminal) == 'ctbc-twd-history-template'
    assert (terminal.__cause__, terminal.__context__, terminal.__suppress_context__) == (None, error, True)
    assert events == (['goto'] if which == 'goto' else ['goto', 'url'])
    assert_sink(result, terminal, 'CTBC-05-200-001' if which == 'goto' else 'CTBC-05-201-001')


@pytest.mark.parametrize('kind', ['missing', 'invalid', 'recover', 'wait', 'repeat'])
def test_navigation_id_only_original_links_and_recovery(monkeypatch, original_banks, kind):
    results = []
    for cls in (original_banks['ctbc']['CtbcCrawler'], CtbcCrawler):
        crawler = crawler_for(cls)
        collector = base.ResponseCollector()
        if kind == 'invalid':
            hit = _inventory_hit([])
            hit.status = 503
            collector.hits.append(hit)
        events = []
        failure = ValueError('PRIVATE_SENTINEL')
        later = LookupError('PRIVATE_SENTINEL later')
        def evaluate(script):
            events.append('evaluate')
            raise failure
        def wait(ms):
            events.append(ms)
            if ms == 3000:
                if kind == 'recover':
                    collector.hits.append(_inventory_hit([]))
                elif kind == 'wait':
                    raise later
        crawler._collect_twd_deposit_history = lambda *a, **k: (_ for _ in ()).throw(later)
        page = SimpleNamespace(evaluate=evaluate, wait_for_timeout=wait)
        original_collect = crawler.collect
        result, terminals = run_sink(monkeypatch, crawler, page, collector,
                                     lambda: original_collect(page, collector))
        terminal, = terminals
        assert terminal.__cause__ is None
        assert terminal.__context__ is None
        assert terminal.__suppress_context__ is False
        assert (failure.__cause__, failure.__context__, failure.__suppress_context__) == (None, None, False)
        assert events == [5000, 'evaluate', 3000]
        if cls is CtbcCrawler:
            if kind in ('missing', 'repeat'):
                assert_sink(result, terminal, 'CTBC-04-100-001')
            elif kind == 'invalid':
                assert_sink(result, terminal, 'CTBC-04-010-004')
            else:
                assert terminal is later
                assert exception_error_id(terminal, bank='ctbc') != 'CTBC-04-100-001'
        results.append((type(terminal), str(terminal), list(events)))
        if kind == 'repeat' and cls is CtbcCrawler:
            # Same instance: a later ordinary missing observation must not inherit carry.
            page.evaluate = lambda script: None
            with pytest.raises(RuntimeError) as caught:
                original_collect(page, base.ResponseCollector())
            assert exception_error_id(caught.value, bank='ctbc') == 'CTBC-04-010-001'
    assert results[0] == results[1]


def test_navigation_sidecar_is_one_fixed_id_and_resets():
    crawler = crawler_for()
    cell = []
    error = ValueError('PRIVATE_SENTINEL')
    def fail(script):
        raise error
    assert crawler._goto_twd_deposit(SimpleNamespace(evaluate=fail), cell) is None
    assert cell == ['CTBC-04-100-001']
    crawler._goto_twd_deposit(SimpleNamespace(evaluate=lambda script: None), cell)
    assert cell == []


@pytest.mark.parametrize('rows,code', [
    ([None, {}, {'actDtTm': 'bad'}] * 100, 'CTBC-07-030-002'),
    ([{}, None] * 100, 'CTBC-07-030-003'),
    ([{'actDtTm': 'bad'}, None], 'CTBC-07-020-003'),
    ([{'actDtTm': '2026-03-99-00.00.00'}, {}], 'CTBC-07-020-004'),
    ([{'actDtTm': '2026-03-01-00.00.00', 'dbAmt': '1', 'memo1': True},
      {'actDtTm': '2026-03-01-00.00.00', 'balanceAmt': True}], 'CTBC-05-810-001'),
    ([{'actDtTm': '2026-03-01-00.00.00', 'dbAmt': '1', 'balanceAmt': True},
      {'actDtTm': '2026-03-01-00.00.00', 'dbAmt': '1', 'memo1': True}], 'CTBC-05-802-001'),
])
def test_first_row_fixed_id_only_matches_original_skip_and_terminal(monkeypatch, original_banks, rows, code):
    import backend.banks.ctbc as module
    start, end = date(2026, 3, 1), date(2026, 3, 31)
    expected = original_banks['ctbc']['_filter_valid_ctbc_details'](rows, start=start, end=end)
    cell = []
    assert module._filter_valid_ctbc_details(rows, start=start, end=end, failures=cell) == expected
    assert cell == [code]
    assert expected == ([], len(rows))
    crawler = crawler_for()
    collector = _collector(['synthetic'])
    page = _FetchPage()
    evaluate = page.evaluate
    def malformed(script, payload):
        value = evaluate(script, payload)
        value['json']['rsData']['detailList'] = rows
        value['json']['rsData']['count'] = len(rows)
        return value
    page.evaluate = malformed
    result, terminals = run_sink(monkeypatch, crawler, page, collector,
                                 lambda: history(crawler, page, collector))
    terminal, = terminals
    assert str(terminal) == 'ctbc-twd-history-row'
    assert (terminal.__cause__, terminal.__context__, terminal.__suppress_context__) == (None, None, False)
    assert len(page.payloads) == 1
    assert_sink(result, terminal, code)
    # No stale metadata when the filter is reused for a successful observation.
    assert module._filter_valid_ctbc_details([], start=start, end=end, failures=cell) == ([], 0)
    assert cell == []


@pytest.mark.parametrize('fail,code', [(True, 'CTBC-05-040-034'), (False, 'CTBC-05-040-020')])
def test_actual_node_json_catch_through_monthly_run_sink(monkeypatch, fail, code):
    crawler = crawler_for()
    collector = _collector(['synthetic'])
    page = OfflineScriptPage(fail)
    page.goto = lambda *a, **k: None
    page.url = _FetchPage().url
    page.wait_for_timeout = lambda ms: None
    result, terminals = run_sink(monkeypatch, crawler, page, collector,
                                 lambda: history(crawler, page, collector))
    terminal, = terminals
    assert str(terminal) == 'ctbc-twd-history-fetch'
    assert terminal.__cause__ is None and terminal.__suppress_context__ is True
    assert str(terminal.__context__) == 'invalid-response'
    assert len(page.events) == 1
    assert [event if isinstance(event, str) else event[0] for event in page.events[0]] == [
        'controller', 'timeout', 'fetch', 'json', 'clear',
    ]
    assert_sink(result, terminal, code)


@pytest.mark.parametrize('field,value,code', [
    ('totalPages', True, 'CTBC-05-820-001'), ('totalPages', 2, 'CTBC-05-820-002'),
    ('pageCount', True, 'CTBC-05-821-001'), ('pageCount', 2, 'CTBC-05-821-002'),
    ('count', True, 'CTBC-05-822-001'), ('count', 2, 'CTBC-05-822-002'),
    ('totalCount', True, 'CTBC-05-823-001'), ('totalCount', 2, 'CTBC-05-823-002'),
    ('hasNext', 'next', 'CTBC-05-824-001'), ('hasMore', 'next', 'CTBC-05-825-001'),
    ('moreData', 'next', 'CTBC-05-826-001'), ('nextPage', 'next', 'CTBC-05-827-001'),
    ('nextToken', 'next', 'CTBC-05-828-001'),
    ('hasNext', None, 'CTBC-05-829-001'), ('hasMore', None, 'CTBC-05-833-001'),
    ('moreData', None, 'CTBC-05-834-001'),
])
def test_natural_replay_evidence_rejection_through_monthly_run(monkeypatch, field, value, code):
    crawler = crawler_for()
    collector = _collector(['synthetic'])
    page = _FetchPage(empty=True)
    evaluate = page.evaluate
    def malformed(script, payload):
        response = evaluate(script, payload)
        data = response['json']['rsData']
        if field == 'pageCount' or (field in ('hasNext', 'hasMore', 'moreData') and value is None):
            del data['totalPages']
        if field == 'totalCount':
            del data['count']
        data[field] = value
        return response
    page.evaluate = malformed
    result, terminals = run_sink(monkeypatch, crawler, page, collector,
                                 lambda: history(crawler, page, collector))
    terminal, = terminals
    assert str(terminal) == 'ctbc-twd-history-fetch'
    assert terminal.__cause__ is None and terminal.__suppress_context__ is True
    assert len(page.payloads) == 1
    assert_sink(result, terminal, code)


@pytest.mark.parametrize('mode', ['untagged', 'recovered', 'explicit'])
def test_monthly_unknown_fallback_and_native_context_priority(monkeypatch, mode):
    import backend.banks.ctbc as module
    crawler = crawler_for()
    collector = _collector(['synthetic'])
    page = _FetchPage()
    error = ValueError('PRIVATE_SENTINEL')
    leaves = []
    def fail(*args):
        if mode == 'untagged':
            raise error  # No fabricated diagnostic tag on the injected fault.
        try:
            module._validated_ctbc_detail({'actDtTm': 'bad'}, start=date(2026, 3, 1), end=date(2026, 3, 31))
        except ValueError as leaf:
            leaves.append(leaf)
            if mode == 'explicit':
                raise error from leaf
            raise error
    if mode == 'untagged':
        monkeypatch.setattr(crawler, '_fetch_qu002_011', fail)
    else:
        page.evaluate = fail
    result, terminals = run_sink(monkeypatch, crawler, page, collector,
                                 lambda: history(crawler, page, collector))
    terminal, = terminals
    assert (terminal.__cause__, terminal.__context__, terminal.__suppress_context__) == (None, error, True)
    code = {'untagged': 'CTBC-05-020-022', 'recovered': 'CTBC-05-400-001', 'explicit': 'CTBC-07-020-003'}[mode]
    if leaves:
        assert error.__context__ is leaves[0]
        assert error.__cause__ is (leaves[0] if mode == 'explicit' else None)
    assert_sink(result, terminal, code)


def test_undefined_first_row_producer_keeps_fallback(monkeypatch):
    import backend.banks.ctbc as module
    crawler = crawler_for()
    collector = _collector(['synthetic'])
    page = _FetchPage()
    def unknown(*args, **kwargs):
        raise ValueError('PRIVATE_SENTINEL')
    monkeypatch.setattr(module, '_validated_ctbc_detail', unknown)
    result, terminals = run_sink(monkeypatch, crawler, page, collector,
                                 lambda: history(crawler, page, collector))
    terminal, = terminals
    assert (terminal.__cause__, terminal.__context__, terminal.__suppress_context__) == (None, None, False)
    assert_sink(result, terminal, 'CTBC-05-020-023')


@pytest.mark.parametrize('case', ['success', 'bank-reject', 'rows', 'native-error'])
def test_original_current_history_order_counts_and_public_failure_links(monkeypatch, original_banks, case):
    results = []
    for cls in (original_banks['ctbc']['CtbcCrawler'], CtbcCrawler):
        crawler = crawler_for(cls)
        collector = _collector(['synthetic-a', 'synthetic-b'])
        page = _FetchPage(empty=True, fail_month='m3' if case == 'bank-reject' else None)
        evaluate = page.evaluate
        native = import_module('patchright._impl._helper').parse_error({
            'name': 'Error', 'message': 'PRIVATE_SENTINEL', 'stack': 'offline stack',
        })
        calls = []
        def observe(script, payload):
            calls.append((payload['body']['rqData']['accountId'], payload['body']['rqData']['type']))
            if case == 'native-error':
                raise native
            response = evaluate(script, payload)
            if case == 'rows':
                response['json']['rsData'].update(detailList=[None, {}], count=2)
            return response
        page.evaluate = observe
        try:
            result = history(crawler, page, collector)
            outcome = ('ok', result)
        except RuntimeError as error:
            assert error.__cause__ is None
            assert error.__suppress_context__ is (case in ('bank-reject', 'native-error'))
            if case == 'native-error':
                assert error.__context__ is native
            elif case == 'rows':
                assert error.__context__ is None
            else:
                assert str(error.__context__) == 'invalid-response'
            outcome = ('error', type(error), str(error))
        results.append((outcome, calls))
    assert results[0] == results[1]
    calls = results[1][1]
    assert len(calls) == {'success': 12, 'bank-reject': 3, 'rows': 1, 'native-error': 1}[case]
    assert [month for _, month in calls[:6]] == (['m5', 'm4', 'm3', 'm2', 'm1', 'm0'] if case == 'success' else
                                                 ['m5', 'm4', 'm3'] if case == 'bank-reject' else ['m5'])


@pytest.mark.parametrize('sdk', ['patchright', 'playwright'])
def test_native_url_replacement_through_month_and_typed_final_sink(monkeypatch, sdk):
    crawler = crawler_for()
    collector = _collector(['synthetic'])
    collector.auth_token = None
    native = import_module(sdk + '._impl._helper').parse_error({
        'name': 'Error', 'message': 'PRIVATE_SENTINEL', 'stack': 'offline stack',
    })
    events = []
    class Page:
        reads = 0
        def goto(self, *args, **kwargs):
            events.append(('goto', args, kwargs))
        def wait_for_timeout(self, ms):
            events.append(('wait', ms))
        @property
        def url(self):
            self.reads += 1
            events.append(('url', self.reads))
            if self.reads == 2:
                raise native
            return _FetchPage().url
    page = Page()
    result, terminals = run_sink(monkeypatch, crawler, page, collector,
                                 lambda: history(crawler, page, collector))
    terminal, = terminals
    replacement = terminal.__context__
    assert type(replacement) is RuntimeError and str(replacement) == 'ctbc-twd-history-form'
    assert (replacement.__cause__, replacement.__context__, replacement.__suppress_context__) == (None, native, True)
    assert (terminal.__cause__, terminal.__suppress_context__) == (None, True)
    assert (native.__cause__, native.__context__, native.__suppress_context__) == (None, None, False)
    assert [event[0] for event in events] == ['goto', 'url', 'wait', 'url']
    assert_sink(result, terminal, 'CTBC-05-300-001')


class SubmitPage:
    """Fake button only: no fields, SDK connection or real submission."""
    def __init__(self, fail_at, failure):
        self.fail_at, self.failure, self.events = fail_at, failure, []

    def __getattr__(self, operation):
        def native(*args, **kwargs):
            self.events.append((operation, args, kwargs))
            if len(self.events) == self.fail_at:
                raise self.failure
            return {'locator': self, 'count': 1, 'nth': self, 'get_attribute': '',
                    'is_visible': True, 'is_enabled': True, 'inner_text': '登入'}.get(operation)
        return native


_SUBMIT_LABELS = ['定位登入按鈕', '讀取登入候選按鈕數量', '取得登入候選按鈕',
                  '讀取登入候選按鈕樣式', '檢查登入候選按鈕可見性', '檢查登入候選按鈕可用性',
                  '讀取登入候選按鈕文字', '送出登入']


@pytest.mark.parametrize('sdk', ['patchright', 'playwright'])
@pytest.mark.parametrize('fail_at', range(1, 9))
def test_submit_replacement_leaf_to_final_diagnostic(monkeypatch, original_banks, sdk, fail_at):
    import backend.banks.ctbc as module
    outputs = []
    for namespace in (original_banks['ctbc'], vars(module)):
        crawler = crawler_for(namespace['CtbcCrawler'])
        failure = import_module(sdk + '._impl._helper').parse_error({
            'name': 'Error', 'message': 'PRIVATE_SENTINEL', 'stack': 'offline stack',
        })
        page = SubmitPage(fail_at, failure)
        with pytest.raises(namespace['CtbcLoginError']) as caught:
            namespace['_submit_login_once'](page)
        error = caught.value
        assert (error.__cause__, error.__context__, error.__suppress_context__) == (None, failure, True)
        assert (failure.__cause__, failure.__context__, failure.__suppress_context__) == (None, None, False)
        assert len(page.events) == fail_at
        assert [e[0] for e in page.events] == [
            'locator', 'count', 'nth', 'get_attribute', 'is_visible', 'is_enabled', 'inner_text', 'click',
        ][:fail_at]
        if fail_at == 8:
            assert page.events[-1] == ('click', (), {'timeout': 8000})
        outputs.append((type(error).__name__, str(error), page.events))
        if namespace is vars(module):
            code = f'CTBC-02-{99 + fail_at:03d}-001'
            diagnostic = base._failure_diagnostics(crawler, error, '02-900-001')
            assert diagnostic['error_id'] == code
            text = base._format_failure_diagnostics(diagnostic)
            assert f'[{code}] {_SUBMIT_LABELS[fail_at - 1]}：瀏覽器原生操作失敗' in text
            assert 'PRIVATE_SENTINEL' not in repr(diagnostic) + text
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize('fail_at', [1, 8])
@pytest.mark.parametrize('kind', ['cancel', 'base'])
def test_submit_cancellation_and_baseexception_are_not_replaced(original_banks, fail_at, kind):
    import asyncio
    import backend.banks.ctbc as module
    outputs = []
    for namespace in (original_banks['ctbc'], vars(module)):
        failure = asyncio.CancelledError() if kind == 'cancel' else BaseException('PRIVATE_SENTINEL')
        page = SubmitPage(fail_at, failure)
        with pytest.raises(type(failure)) as caught:
            namespace['_submit_login_once'](page)
        assert caught.value is failure
        assert exception_error_id(failure, bank='ctbc') is None
        outputs.append(page.events)
    assert outputs[0] == outputs[1]


def test_submit_success_has_same_single_fake_dispatch_budget(original_banks):
    import backend.banks.ctbc as module
    events = []
    for namespace in (original_banks['ctbc'], vars(module)):
        page = SubmitPage(None, None)
        assert namespace['_submit_login_once'](page) is None
        assert len(page.events) == 8
        assert [e[0] for e in page.events].count('click') == 1
        events.append(page.events)
    assert events[0] == events[1]


@pytest.mark.parametrize('fail_at', [1, 8])
@pytest.mark.parametrize('mode', ['first', 'foreign', 'untagged'])
def test_submit_copy_first_code_and_old_fallback(monkeypatch, fail_at, mode):
    import backend.banks.ctbc as module
    from backend.core.error_codes import with_error_code
    failure = ValueError('PRIVATE_SENTINEL')
    if mode != 'untagged':
        with_error_code(failure, 'CTBC-07-020-003' if mode == 'first' else 'CATHAY-05-500-001')
    else:
        monkeypatch.setattr(module, '_native_step', lambda code, operation: operation())
    with pytest.raises(module.CtbcLoginError) as caught:
        module._submit_login_once(SubmitPage(fail_at, failure))
    expected = 'CTBC-07-020-003' if mode == 'first' else 'CTBC-02-050-002' if fail_at == 1 else 'CTBC-02-050-003'
    assert exception_error_id(caught.value, bank='ctbc') == expected
    assert (caught.value.__cause__, caught.value.__context__, caught.value.__suppress_context__) == (None, failure, True)


@pytest.mark.parametrize('kind', ['url', 'cancel', 'base', 'parse'])
def test_original_current_native_url_failure_projection(original_banks, kind):
    import asyncio
    results = []
    for cls in (original_banks['ctbc']['CtbcCrawler'], CtbcCrawler):
        failure = asyncio.CancelledError() if kind == 'cancel' else BaseException('PRIVATE_SENTINEL') if kind == 'base' else ValueError('PRIVATE_SENTINEL')
        events = []
        class Page:
            @property
            def url(self):
                events.append('url')
                if kind == 'parse':
                    return 123
                raise failure
        with pytest.raises(BaseException) as caught:
            cls._fetch_native_history_window(Page(), base.ResponseCollector(), 'synthetic', date(2026, 8, 1), date(2026, 8, 31))
        error = caught.value
        if kind in ('cancel', 'base'):
            assert error is failure
        else:
            assert (error.__cause__, error.__suppress_context__) == (None, True)
            if kind == 'url':
                assert error.__context__ is failure
            if kind == 'parse' and cls is CtbcCrawler:
                assert exception_error_id(error, bank='ctbc') == 'CTBC-05-030-001'
        results.append((type(error).__name__, str(error), events))
    assert results[0] == results[1]


def test_original_current_native_form_all_fault_traces(original_banks):
    results = []
    for cls in (original_banks['ctbc']['CtbcCrawler'], CtbcCrawler):
        good = NativeForm()
        assert cls._fetch_native_history_window(good, good.collector, 'synthetic', date(2026, 8, 1), date(2026, 8, 31)) == []
        cases = [(None, good.events)]
        for index in range(1, len(good.events) + 1):
            page = NativeForm(fail_at=index)
            with pytest.raises(Exception) as caught:
                cls._fetch_native_history_window(page, page.collector, 'synthetic', date(2026, 8, 1), date(2026, 8, 31))
            assert caught.value is page.failure
            cases.append((type(caught.value).__name__, str(caught.value), page.events))
        results.append(cases)
    assert results[0] == results[1]


@pytest.mark.parametrize('mode', ['first', 'foreign'])
def test_native_url_copy_first_code_and_foreign_fallback(mode):
    from backend.core.error_codes import with_error_code
    failure = with_error_code(ValueError('PRIVATE_SENTINEL'),
                              'CTBC-07-020-003' if mode == 'first' else 'CATHAY-05-500-001')
    class Page:
        @property
        def url(self):
            raise failure
    with pytest.raises(RuntimeError) as caught:
        CtbcCrawler._fetch_native_history_window(Page(), base.ResponseCollector(), 'synthetic', date(2026, 8, 1), date(2026, 8, 31))
    assert exception_error_id(caught.value, bank='ctbc') == ('CTBC-07-020-003' if mode == 'first' else 'CTBC-05-030-001')
    assert (caught.value.__cause__, caught.value.__context__, caught.value.__suppress_context__) == (None, failure, True)
