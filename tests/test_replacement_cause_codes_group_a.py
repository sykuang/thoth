"""Finite replacement provenance regressions; synthetic objects, no browser launch."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.core.base import _failure_diagnostics, _format_failure_diagnostics
from backend.core.error_codes import exception_error_id


def _sink(crawler, error, expected):
    diagnostic = _failure_diagnostics(crawler, error, 'sync_failed')
    assert diagnostic['error_id'] == expected
    text = _format_failure_diagnostics(diagnostic)
    assert f'[{expected}]' in text
    assert 'PRIVATE-SYNTHETIC' not in text


def _esun_login(failure):
    from backend.banks.esun import EsunCrawler
    crawler = object.__new__(EsunCrawler)
    crawler.creds = SimpleNamespace(national_id='A', user_code='B', password='C')
    crawler._credential_origin_allowed = lambda _: True
    crawler._frame_origin_allowed = lambda *_: True
    crawler._find_login_frame = lambda page, **_: page
    crawler._main_login_form = lambda *_: None
    page = Mock()
    page.keyboard = SimpleNamespace(press=Mock(), type=Mock())
    original = TimeoutError('PRIVATE-SYNTHETIC')
    events, state = [], {'role': -1}

    def action(role, op, result=None):
        events.append((role, op))
        if failure == (role, op):
            raise original
        return result

    def locator(selector):
        state['role'] += 1
        role = state['role']
        node = Mock()
        for op, value in [('count', 1), ('nth', node), ('is_visible', True), ('is_enabled', True), ('input_value', 'A')]:
            getattr(node, op).side_effect = lambda *a, r=role, o=op, v=value: action(r, o, v)
        node.click.side_effect = lambda **kw: action(role, 'select' if kw.get('click_count') else 'click')
        return action(role, 'locator', node)

    page.locator.side_effect = locator
    page.keyboard.press.side_effect = lambda *_: action(state['role'], 'clear')
    page.keyboard.type.side_effect = lambda *a, **k: action(state['role'], 'type')
    page.wait_for_timeout.side_effect = lambda *_: action(state['role'], 'settle')
    return crawler, page, original, events


@pytest.mark.parametrize('bank', ['hsbc', 'scsb', 'esun', 'sinopac', 'fubon'])
def test_credential_native_replacements_keep_operation_id_through_parent_sink(bank):
    from tests.test_ordered_hsbc_scsb_codes import login_fixture
    from tests.test_ordered_sinopac_fubon_codes import _login
    observed = []
    if bank in ('hsbc', 'scsb'):
        roles = ('#userId', '#password', '#captchaInput') if bank == 'hsbc' else ('#userId', '#idNumber', '#pppd', '#verified')
        cases = [(role, op) for role in roles for op in ('locate', 'count', 'visible', 'enabled', 'click', 'select', 'clear', 'type', 'read')]
    elif bank == 'esun':
        cases = [(role, op) for role in range(3) for op in ('locator', 'count', 'nth', 'is_visible', 'is_enabled', 'click', 'select', 'clear', 'type', 'settle', 'input_value')]
    else:
        cases = [(role, op) for role in range(4) for op in ('click', 'select', 'erase', 'type', 'read')]
    for role, op in cases:
        if bank in ('hsbc', 'scsb'):
            crawler, page, original, events = login_fixture(bank, (role, op))
        elif bank == 'esun':
            crawler, page, original, events = _esun_login((role, op))
        else:
            crawler, page, fields, submit = _login(bank)
            original = TimeoutError('PRIVATE-SYNTHETIC')
            if op in ('click', 'select'):
                target = fields[role].click
                target.side_effect = original if op == 'click' else [None, original]
            elif op == 'read':
                target = fields[role].input_value
                target.side_effect = original
            elif bank == 'sinopac':
                target = page.keyboard.press if op == 'erase' else page.keyboard.type
                target.side_effect = [None] * role + [original]
            else:
                target = fields[role].press if op == 'erase' else fields[role].press_sequentially
                target.side_effect = original
        crawler.name = bank
        with pytest.raises(RuntimeError) as caught:
            crawler.submit_credentials_once(page)
        expected = exception_error_id(original, bank=bank)
        assert expected is not None, (bank, role, op)
        assert caught.value is not original
        from importlib import import_module
        error_type = getattr(import_module(f'backend.banks.{bank}'), {'hsbc': 'HsbcLoginError', 'scsb': 'ScsbLoginError', 'esun': 'EsunLoginError', 'sinopac': 'SinopacLoginError', 'fubon': 'FubonLoginError'}[bank])
        assert type(caught.value) is error_type
        assert '登入欄位無法安全填寫；未送出登入' in str(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__suppress_context__
        assert exception_error_id(caught.value, bank=bank) == expected, (bank, role, op)
        _sink(crawler, caught.value, expected)
        observed.append(expected)
        if bank in ('sinopac', 'fubon'):
            submit.click.assert_not_called()
        elif bank in ('hsbc', 'scsb'):
            assert ('submit', 'click') not in events
        else:
            assert not any(role == 3 for role, _ in events)
    assert len(set(observed)) == len(observed)


@pytest.mark.parametrize('bank', ['hsbc', 'scsb', 'esun', 'sinopac', 'fubon'])
def test_submit_and_state_replacements_keep_native_leaf(bank):
    from tests.test_ordered_hsbc_scsb_codes import login_fixture
    from tests.test_ordered_sinopac_fubon_codes import _login
    seen = []
    for operation in ('submit', 'settle'):
        if bank in ('hsbc', 'scsb'):
            failure = ('submit', 'click') if operation == 'submit' else ('page', 'wait-1000' if bank == 'hsbc' else 'wait-12000')
            crawler, page, original, events = login_fixture(bank, failure)
        elif bank == 'esun':
            if operation == 'settle':
                continue  # Historical post-submit observation is recovered, not a terminal replacement.
            crawler, page, original, events = _esun_login((3, 'click'))
        else:
            crawler, page, fields, submit = _login(bank)
            original = TimeoutError('PRIVATE-SYNTHETIC')
            target = submit.click if operation == 'submit' else page.wait_for_timeout
            target.side_effect = original
        crawler.name = bank
        with pytest.raises(RuntimeError) as caught:
            crawler.submit_credentials_once(page)
        expected = exception_error_id(original, bank=bank)
        assert expected
        assert exception_error_id(caught.value, bank=bank) == expected
        _sink(crawler, caught.value, expected)
        seen.append(expected)
    assert len(set(seen)) == len(seen)


@pytest.mark.parametrize('bank', ['hsbc', 'scsb'])
def test_button_discovery_and_account_stage_replacements(bank):
    from tests.test_ordered_hsbc_scsb_codes import login_fixture
    seen = []
    roles = ('continue', 'submit') if bank == 'hsbc' else ('submit',)
    for role in roles:
        for operation in ('visible', 'enabled', 'text', 'click'):
            crawler, page, original, events = login_fixture(bank, (role, operation))
            crawler.name = bank
            with pytest.raises(RuntimeError) as caught:
                crawler.submit_credentials_once(page)
            expected = exception_error_id(original, bank=bank)
            assert expected
            _sink(crawler, caught.value, expected)
            seen.append(expected)
    assert len(set(seen)) == len(seen)


@pytest.mark.parametrize('bank', ['sinopac', 'fubon'])
def test_preparation_replacements_keep_native_leaf(bank):
    from tests.test_ordered_sinopac_fubon_codes import _login
    crawler, page, fields, submit = _login(bank)
    original = TimeoutError('PRIVATE-SYNTHETIC')
    page.wait_for_timeout.side_effect = original
    with pytest.raises(RuntimeError) as caught:
        crawler.prepare_login_page(page)
    expected = exception_error_id(original, bank=bank)
    assert expected
    _sink(crawler, caught.value, expected)
    submit.click.assert_not_called()


def test_sinopac_refresh_replacement_keeps_native_leaf():
    from tests.test_ordered_sinopac_fubon_codes import _login
    crawler, page, fields, submit = _login('sinopac')
    original = TimeoutError('PRIVATE-SYNTHETIC')
    page.locator.side_effect = original
    with pytest.raises(RuntimeError) as caught:
        crawler.prepare_captcha_resubmit(page)
    _sink(crawler, caught.value, exception_error_id(original, bank='sinopac'))
    submit.click.assert_not_called()


@pytest.mark.parametrize('operation,expected', [('read', 'SCSB-06-326-001'), ('form', 'SCSB-06-336-001'), ('statement', 'SCSB-06-379-001')])
def test_scsb_nested_card_replacements_keep_native_leaf(operation, expected):
    from unittest.mock import MagicMock
    from backend.banks.scsb import ScsbCrawler
    crawler = object.__new__(ScsbCrawler)
    crawler.name = 'scsb'
    page = MagicMock()
    control = page.locator.return_value
    control.filter.return_value = control
    control.count.return_value = 1
    control.first.is_visible.return_value = True
    control.first.get_attribute.return_value = 'collapsed'
    original = TimeoutError('PRIVATE-SYNTHETIC')

    def evaluate(script, arg=None):
        if script == crawler._card_leaf_scope_text_script():
            if operation == 'read':
                raise original
            return arg + ' 目前無新增交易 no real-time transaction'
        if script == crawler._card_statement_state_script():
            raise original
        if script == crawler._card_leaf_table_state_script():
            return {'ok': True, 'row_count': 0}
        if "querySelectorAll('input')" in script:
            if operation == 'form':
                raise original
            return []
        raise AssertionError('unexpected synthetic page read')

    page.evaluate.side_effect = evaluate
    with pytest.raises(RuntimeError, match='SCSB credit-card inquiry failed') as caught:
        crawler._collect_credit_card_inquiry(page)
    assert exception_error_id(original, bank='scsb') == expected
    _sink(crawler, caught.value, expected)


def test_fubon_inventory_helper_replacement_keeps_actual_predicate(monkeypatch):
    import backend.banks.fubon as module
    crawler = object.__new__(module.FubonCrawler)
    crawler.name = 'fubon'
    crawler._open_twd_query = Mock(return_value=Mock())
    monkeypatch.setattr(module, 'bounded_evaluate', Mock(return_value=None))
    with pytest.raises(RuntimeError, match='fubon-twd-history-inventory') as caught:
        crawler._collect_attested_twd_history(Mock())
    _sink(crawler, caught.value, 'FUBON-04-200-001')


@pytest.mark.parametrize('key,expected', [('DataText1', 'SINOPAC-07-206-001'), ('DataText2', 'SINOPAC-07-207-001')])
def test_sinopac_row_date_replacement_keeps_native_parse_role(key, expected):
    from datetime import date
    from backend.banks.sinopac import SinopacCrawler
    from tests.test_sinopac_history_sync import _row
    row = _row()
    row[key] = '2026/02/30<br />12:00' if key == 'DataText1' else '2026/02/30'
    with pytest.raises(RuntimeError) as caught:
        SinopacCrawler._validate_history_row(row, currency='TWD', start=date(2026, 8, 1), end=date(2026, 8, 31))
    assert exception_error_id(caught.value, bank='sinopac') == expected


def test_sinopac_inventory_form_parse_replacement_keeps_native_parse_role():
    from backend.banks.sinopac import SinopacCrawler
    from backend.core.base import ResponseCollector
    from tests.test_sinopac_history_sync import _inventory_hit
    hit = _inventory_hit()
    hit.req_body = 'not-a-form'
    collector = ResponseCollector()
    collector.hits = [hit]
    with pytest.raises(RuntimeError) as caught:
        SinopacCrawler._twd_inventory(collector)
    assert exception_error_id(caught.value, bank='sinopac') == 'SINOPAC-04-203-001'


def test_spa_continuation_request_mismatch_keeps_specific_leaf_at_parent_sink(monkeypatch):
    from backend.banks.esun import EsunCrawler
    from backend.banks.esun_spa import collection
    from backend.banks.esun_spa.capture import PATHS

    request = dict(account='SYNTHETIC-A', startDate='a', endDate='b', startIndex=1,
                   count=3, customerInputHashtag=[])
    detail = dict(startIndex=1, count=3, detailListData=[],
                  displayErrorCode=None, displayErrorMsg=None)
    response = {'queryDeptTxDtlResult': detail}
    envelope = {'requestBody': request}
    body = dict(detail, startIndex=4)
    issued = dict(request, account='SYNTHETIC-B', startIndex=4)
    collector = SimpleNamespace(
        MAX_QUERY_REQUESTS=10, query_issued_count=lambda: 2,
        issued_count=lambda _: 1,
        _latest_spa={PATHS[2]: (20, None), PATHS[5]: (21, None)},
    )
    baseline = {'counts': {path: 0 for path in PATHS}}
    calls = []

    def current(owner, snapshot, path):
        assert owner is collector and snapshot is baseline
        calls.append(path)
        return (body, {'requestBody': issued}) if path == PATHS[5] else (response, envelope)

    monkeypatch.setattr(collection, 'require_current_success', current)
    with pytest.raises(ValueError, match='^continuation_rejected$') as caught:
        collection.owned_autonomous_continuation(collector, baseline, response, envelope)
    assert type(caught.value) is ValueError
    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is True
    assert exception_error_id(caught.value, bank='esun') == 'ESUN-05-438-001'
    assert calls == [PATHS[2], PATHS[5]]
    crawler = object.__new__(EsunCrawler)
    crawler.name = 'esun'
    _sink(crawler, caught.value, 'ESUN-05-438-001')
    diagnostic = _failure_diagnostics(crawler, caught.value, 'sync_failed')
    assert _format_failure_diagnostics(diagnostic).startswith(
        "[ESUN-05-438-001] 台幣交易查詢：驗證條件未通過：issued == dict(request, startIndex=cursor, count=previous['count'])"
    )


def test_spa_continuation_wrapper_keeps_actual_rejection_and_untagged_fallback():
    from backend.banks.esun_spa.collection import owned_autonomous_continuation
    from backend.banks.esun_spa.capture import PATHS
    collector = SimpleNamespace(issued_count=lambda _: 0)
    with pytest.raises(ValueError, match='continuation_rejected') as caught:
        owned_autonomous_continuation(collector, {'counts': {PATHS[5]: 0}}, {}, {})
    assert exception_error_id(caught.value, bank='esun') == 'ESUN-05-558-001'
    with pytest.raises(ValueError, match='continuation_rejected') as caught:
        owned_autonomous_continuation(collector, {}, {}, {})
    assert exception_error_id(caught.value, bank='esun') == 'ESUN-05-564-001'


def test_spa_observation_wrapper_keeps_actual_rejection_and_clears_slot():
    from backend.banks.esun_spa.capture import SpaCollector
    collector = object.__new__(SpaCollector)
    collector._continuation_observation = None
    with pytest.raises(ValueError, match='no current owned continuation observation') as caught:
        collector._require_current_continuation_observation({})
    assert exception_error_id(caught.value, bank='esun') == 'ESUN-07-505-001'
    assert collector._continuation_observation is None


@pytest.mark.parametrize('operation,expected', [('frame', 'ESUN-05-607-001'), ('inventory', 'ESUN-05-613-001'), ('account', 'ESUN-05-614-100')])
def test_esun_legacy_collect_replacement_layers_keep_actual_native(operation, expected, monkeypatch, tmp_path):
    from backend.banks.esun import EsunCrawler, _ESUN_TWD_HISTORY_PATH
    from backend.core import store
    monkeypatch.setattr(store, '_data_root', lambda: tmp_path)
    crawler = object.__new__(EsunCrawler)
    crawler.name = 'esun'
    crawler._parse_account_overview = Mock(return_value=[])
    crawler._navigate_menu = Mock(return_value={'frames': [{'result': {'clicked': True}}]})
    original = TimeoutError('PRIVATE-SYNTHETIC')
    frame = Mock(url='https://ebank.esunbank.com.tw' + _ESUN_TWD_HISTORY_PATH)

    def evaluate(script, *args):
        if 'Boolean(' in script:
            if operation == 'frame':
                raise original
            return True
        if 'match(/"today"' in script:
            return '2026/08/30'
        if '[...s.options]' in script:
            if operation == 'inventory':
                raise original
            return [{'index': 1, 'text': '臺幣綜存 0900000087022', 'value': 'opaque-a'}]
        return ''

    frame.evaluate.side_effect = evaluate
    frame.locator.side_effect = original
    page = Mock(url='https://ebank.esunbank.com.tw/')
    page.evaluate.return_value = False
    page.main_frame = Mock()
    page.frames = [page.main_frame, frame]
    with pytest.raises(RuntimeError, match='esun-twd-history') as caught:
        crawler.collect(page, Mock())
    assert exception_error_id(original, bank='esun') == expected
    _sink(crawler, caught.value, expected)


def test_esun_parser_replacement_keeps_actual_money_predicate():
    from datetime import date
    from backend.banks.esun import EsunCrawler
    from tests.test_esun_history_sync import _bound_result
    result = _bound_result()
    result['snapshot']['gridRows'][0][4] = 'invalid'
    with pytest.raises(RuntimeError, match='esun-twd-history-result') as caught:
        EsunCrawler._validated_twd_history_result(result, identity=result['account_no'], start=date(2025, 8, 31), end=date(2026, 8, 30))
    context = caught.value.__context__
    expected = exception_error_id(context, bank='esun')
    assert expected and expected != 'ESUN-05-429-001'
    assert exception_error_id(caught.value, bank='esun') == expected





@pytest.mark.parametrize('operation', ['count', 'nth', 'is_visible', 'is_enabled'])
def test_esun_button_discovery_replacement_keeps_native_leaf(operation):
    crawler, page, original, events = _esun_login((3, operation))
    crawler.name = 'esun'
    with pytest.raises(RuntimeError) as caught:
        crawler.submit_credentials_once(page)
    expected = exception_error_id(original, bank='esun')
    assert expected
    _sink(crawler, caught.value, expected)
    assert (3, 'click') not in events


@pytest.mark.parametrize('cells,expected', [(None, 'FUBON-07-285-001'), (['no date'], 'FUBON-07-287-001'), (['2026/02/30'], 'FUBON-07-288-001')])
def test_fubon_grid_replacement_keeps_actual_predicate(cells, expected):
    from backend.banks.fubon import FubonCrawler
    from tests.test_fubon_history_sync import _result
    result = _result('2025-08-30', '2026-02-27', '2025-09-02')
    result['snapshot']['gridRows'][0] = cells
    with pytest.raises(RuntimeError, match='fubon-twd-history-result') as caught:
        FubonCrawler._validated_twd_history_result(result)
    assert exception_error_id(caught.value, bank='fubon') == expected


def test_spa_prequery_wait_replacement_keeps_current_native_not_recovered_readiness(monkeypatch):
    from patchright.sync_api import TimeoutError as NativeTimeoutError
    from backend.banks.esun_spa import collection
    from backend.banks.esun_spa.capture import PATHS, SpaCollector
    page = Mock()
    page.locator.return_value.count.return_value = 1
    original = NativeTimeoutError('PRIVATE-SYNTHETIC')
    page.wait_for_function.side_effect = original
    crawler = SimpleNamespace(_logged_in=lambda _: True)
    collector = SimpleNamespace(page=page, snapshot=lambda: {})
    observation = object.__new__(SpaCollector)
    observation._continuation_observation = None
    recovered = []

    def current(owner, baseline, path):
        if path == PATHS[0]:
            return {'menuList': []}, {'requestBody': {'locale': 'zh-TW'}}
        try:
            observation._require_current_continuation_observation({})
        except ValueError as error:
            recovered.append(exception_error_id(error, bank='esun'))
            raise

    monkeypatch.setattr(collection, 'navigation_guard', lambda *_: lambda: None)
    monkeypatch.setattr(collection, 'build_menu_plan', lambda *_: {'locale': 'zh-TW'})
    monkeypatch.setattr(collection, 'navigate_twd', lambda *_: None)
    monkeypatch.setattr(collection, 'require_current_success', current)
    with pytest.raises(ValueError, match='SPA probe rejected') as caught:
        collection._collect_rows(crawler, page, collector, {}, [], [])
    assert recovered == ['ESUN-07-505-001']
    assert exception_error_id(original, bank='esun') == 'ESUN-05-777-001'
    assert exception_error_id(caught.value, bank='esun') == 'ESUN-05-777-001'
    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is True
    assert caught.value.__context__ is original
    from backend.banks.esun import EsunCrawler
    diagnostic_crawler = object.__new__(EsunCrawler)
    diagnostic_crawler.name = 'esun'
    _sink(diagnostic_crawler, caught.value, 'ESUN-05-777-001')
    diagnostic = _failure_diagnostics(diagnostic_crawler, caught.value, 'sync_failed')
    assert _format_failure_diagnostics(diagnostic).startswith(
        '[ESUN-05-777-001] 台幣交易列收集：條件等待：原生動作失敗'
    )
    page.wait_for_function.assert_called_once()


@pytest.mark.parametrize('bank', ['hsbc', 'scsb', 'esun', 'sinopac', 'fubon'])
def test_successful_submission_stays_successful(bank):
    from tests.test_ordered_hsbc_scsb_codes import login_fixture
    from tests.test_ordered_sinopac_fubon_codes import _login
    if bank in ('hsbc', 'scsb'):
        crawler, page, original, events = login_fixture(bank, None)
    elif bank == 'esun':
        crawler, page, original, events = _esun_login(None)
        crawler._logged_in = Mock(return_value=True)
    else:
        crawler, page, fields, submit = _login(bank)
    assert crawler.submit_credentials_once(page) is None
    if bank in ('sinopac', 'fubon'):
        submit.click.assert_called_once_with(timeout=8000)
    elif bank in ('hsbc', 'scsb'):
        assert events.count(('submit', 'click')) == 1
    else:
        assert events.count((3, 'click')) == 1


def test_untagged_esun_page_replacement_retains_fallback():
    crawler, page, original, events = _esun_login(None)
    crawler._find_login_frame = Mock(side_effect=original)
    with pytest.raises(RuntimeError, match='無法安全確認登入頁面；未送出登入') as caught:
        crawler.submit_credentials_once(page)
    assert exception_error_id(original, bank='esun') is None
    assert exception_error_id(caught.value, bank='esun') == 'ESUN-02-404-001'
    assert caught.value.__context__ is original
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_untagged_native_parse_rejections_keep_actual_predicate_ids():
    from datetime import date
    from backend.banks.hsbc import HsbcCrawler
    from backend.banks.esun_spa.rows import normalize_row
    with pytest.raises(RuntimeError) as caught:
        HsbcCrawler._api_date('2026-02-30T12:00')
    assert exception_error_id(caught.value, bank='hsbc') == 'HSBC-06-308-001'
    assert exception_error_id(caught.value.__context__, bank='hsbc') is None
    with pytest.raises(ValueError) as caught:
        normalize_row({'txDate': '2026/02/30', 'txTime': '12:00:00'}, account_no='0000000000001', currency='TWD', start=date(2026, 1, 1), end=date(2026, 3, 1))
    assert exception_error_id(caught.value, bank='esun') == 'ESUN-07-421-001'
    assert exception_error_id(caught.value.__context__, bank='esun') is None


def test_spa_native_wait_inside_handled_readiness_keeps_current_terminal_id():
    from datetime import date
    from backend.banks.esun_spa.query import query_twd
    page = Mock()
    page.evaluate.side_effect = [[2026, 9, 1], 1400]
    page.locator.return_value.count.return_value = 2
    original = TimeoutError('PRIVATE-SYNTHETIC')
    page.wait_for_timeout.side_effect = original
    with pytest.raises(TimeoutError) as caught:
        query_twd(page, date(2026, 8, 1), date(2026, 8, 31), 'zh-TW', 'synthetic', lambda: True, lambda: 'synthetic')
    assert caught.value is original
    assert exception_error_id(original.__context__, bank='esun') == 'ESUN-05-582-001'
    assert exception_error_id(original, bank='esun') == 'ESUN-05-728-001'
    page.wait_for_timeout.assert_called_once_with(50)
