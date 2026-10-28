"""Offline action injection: run real bank methods, never credentials/network."""
from copy import deepcopy
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock
import ast
import inspect
import subprocess
from pathlib import Path

import pytest

from backend.banks.hsbc import HsbcCrawler, HsbcLoginError
from backend.banks.scsb import ScsbCrawler, ScsbLoginError
from backend.core.base import ApiHit, ResponseCollector
from backend.core.error_codes import code_details, exception_error_id


def login_fixture(bank, failure):
    cls, selectors = (HsbcCrawler, ('#userId', '#password', '#captchaInput')) if bank == 'hsbc' else (
        ScsbCrawler, ('#userId', '#idNumber', '#pppd', '#verified'))
    crawler = object.__new__(cls)
    crawler.creds = SimpleNamespace(user_id='synthetic', password='synthetic', national_id='synthetic', user_code='synthetic')
    crawler._logged_in = Mock(return_value=True)
    crawler._response_visible = Mock(return_value=False)
    crawler._visible_blocker = Mock(return_value=False)
    crawler._solve_captcha = Mock(return_value='12345')
    crawler._ocr_captcha = Mock(return_value='12345')
    page = Mock()
    original = TimeoutError('synthetic-private-exception')
    state = {'role': None, 'value': ''}
    events = []

    def action(role, op, result=None):
        events.append((role, op))
        if failure == (role, op):
            raise original
        return result

    def field(role):
        node = Mock()
        node.is_visible.side_effect = lambda: action(role, 'visible', True)
        node.is_enabled.side_effect = lambda: action(role, 'enabled', True)
        def click(**kwargs):
            state['role'] = role
            action(role, 'select' if kwargs.get('click_count') else 'click')
        node.click.side_effect = click
        node.input_value.side_effect = lambda: action(role, 'read', state['value'])
        return node

    nodes = {selector: field(selector) for selector in selectors}
    first, final = field('continue'), field('submit')
    first.inner_text.side_effect = lambda: action('continue', 'text', '繼續')
    final.inner_text.side_effect = lambda: action('submit', 'text', '繼續' if bank == 'hsbc' else 'Log in')
    def locator(selector):
        role = selector if selector in nodes else 'button'
        action(role, 'locate')
        values = [nodes[selector]] if selector in nodes else [first] if 'continueButton' in selector else [final]
        collection = Mock()
        collection.count.side_effect = lambda: action(role, 'count', len(values))
        collection.nth.side_effect = lambda index: values[index]
        return collection
    page.locator.side_effect = locator
    page.wait_for_selector.side_effect = lambda selector, **kwargs: action(selector, 'wait')
    page.wait_for_timeout.side_effect = lambda ms: action('page', f'wait-{ms}')
    page.keyboard.press.side_effect = lambda key: action(state['role'], 'clear')
    def type_value(value, **kwargs):
        action(state['role'], 'type')
        state['value'] = value
    page.keyboard.type.side_effect = type_value
    return crawler, page, original, events


@pytest.mark.parametrize('bank,selectors', [
    ('hsbc', ('#userId', '#password', '#captchaInput')),
    ('scsb', ('#userId', '#idNumber', '#pppd', '#verified')),
])
def test_real_submit_native_failures_distinguish_fields_and_actions(bank, selectors):
    observed = {}
    for role in selectors:
        for op in ('locate', 'count', 'visible', 'enabled', 'click', 'select', 'clear', 'type', 'read'):
            crawler, page, original, events = login_fixture(bank, (role, op))
            with pytest.raises((HsbcLoginError, ScsbLoginError)) as caught:
                crawler.submit_credentials_once(page)
            error_id = exception_error_id(caught.value, bank=bank)
            assert error_id is not None, (role, op)
            assert code_details(error_id, bank=bank)
            assert caught.value.__suppress_context__
            assert original in context_chain(caught.value)
            assert events.count((role, op)) == 1
            assert ('submit', 'click') not in events
            observed[(role, op)] = error_id
    assert len(set(observed.values())) == len(observed), observed


def test_button_native_actions_and_post_submit_wait_are_distinct():
    for bank, roles in [('hsbc', ('continue', 'submit')), ('scsb', ('submit',))]:
        observed = []
        for role in roles:
            for op in ('visible', 'enabled', 'text', 'click'):
                crawler, page, original, events = login_fixture(bank, (role, op))
                with pytest.raises((HsbcLoginError, ScsbLoginError)) as caught:
                    crawler.submit_credentials_once(page)
                error_id = exception_error_id(caught.value, bank=bank)
                assert error_id is not None, (bank, role, op)
                assert original in context_chain(caught.value)
                observed.append(error_id)
        assert len(set(observed)) == len(observed), (bank, observed)


def test_period_native_actions_distinguish_custom_start_end():
    observed = []
    for role in ('custom', 'start', 'end'):
        for operation in ('locator', 'count', 'is_visible', 'apply'):
            original = TimeoutError('synthetic')
            page = Mock()
            def locator(selector):
                actual = next(name for name in ('custom', 'start', 'end') if f'="{name}"' in selector)
                if (actual, 'locator') == (role, operation):
                    raise original
                node = Mock()
                for name, result in [('count', 1), ('is_visible', True), ('check', None), ('fill', None)]:
                    target = node if name == 'count' else node.first
                    mock = getattr(target, name)
                    mock.return_value = result
                    if actual == role and (name == operation or operation == 'apply' and name in ('check', 'fill')):
                        mock.side_effect = original
                return node
            page.locator.side_effect = locator
            with pytest.raises(TimeoutError) as caught:
                ScsbCrawler._apply_twd_period_controls(page, {'ok': True, 'start': '2026/01/01', 'end': '2026/01/02'})
            assert caught.value is original
            observed.append(exception_error_id(caught.value, bank='scsb'))
    assert None not in observed
    assert len(set(observed)) == len(observed), observed


def test_scsb_card_navigation_distinguishes_each_leaf_and_menu_action():
    from unittest.mock import MagicMock
    observed = []
    leaves = [('unbilled', '未出帳消費明細'), ('current', '即時消費紀錄'), ('statement', '帳單查詢及繳款')]
    for key, leaf in leaves:
        for label in ('信用卡', '信用卡帳務', leaf):
            for operation in ('locator', 'count', 'is_visible', 'get_attribute', 'click', 'wait'):
                original = TimeoutError('synthetic')
                page = MagicMock()
                state = {'visit': -1, 'role': None}
                def action(role, op, result=None):
                    if (role, op) == ((key, label), operation):
                        raise original
                    return result
                def locator(selector):
                    state['visit'] += 1
                    current_key, current_leaf = leaves[state['visit'] // 3]
                    current_label = ('信用卡', '信用卡帳務', current_leaf)[state['visit'] % 3]
                    role = (current_key, current_label)
                    state['role'] = role
                    action(role, 'locator')
                    node = MagicMock()
                    node.filter.return_value = node
                    for op, result in [('count', 1), ('is_visible', True), ('get_attribute', 'collapsed'), ('click', None)]:
                        target = node if op == 'count' else node.first
                        getattr(target, op).side_effect = lambda *a, r=role, o=op, v=result, **kw: action(r, o, v)
                    return node
                page.locator.side_effect = locator
                page.wait_for_timeout.side_effect = lambda ms: action(state['role'], 'wait') if ms == 1500 else None
                def evaluate(script, *args):
                    if '.map(input => ({id: input.id, value: input.value}))' in script:
                        return []
                    if 'heading_count' in script:
                        return {'ok': True, 'row_count': 0}
                    if '帳單結帳日' in script and not args:
                        return {'ok': True}
                    return args[0] + ' 目前無新增交易 no real-time transaction' if args else {'ok': True}
                page.evaluate.side_effect = evaluate
                with pytest.raises(RuntimeError) as caught:
                    object.__new__(ScsbCrawler)._collect_credit_card_inquiry(page)
                assert original in context_chain(caught.value)
                observed.append(exception_error_id(caught.value, bank='scsb'))
    assert None not in observed
    assert len(set(observed)) == len(observed), observed


def test_scsb_twd_result_error_markers_and_pagination_are_distinct():
    from tests.test_scsb_accounts_and_twd_inquiry import _mock_twd_query_page
    observed = []
    for marker in ('system_error', 'busy', 'retry', 'timeout', 'session', 'failed', None, 'pagination'):
        result_state = {'error': marker != 'pagination', 'pagination': marker == 'pagination', 'error_code': marker}
        page = _mock_twd_query_page(result_state=result_state)
        with pytest.raises(RuntimeError) as caught:
            object.__new__(ScsbCrawler)._collect_twd_inquiry(page, {'90000000123456'})
        observed.append(exception_error_id(caught.value, bank='scsb'))
    assert None not in observed
    assert len(set(observed)) == len(observed), observed


def test_scsb_top_level_navigation_keeps_native_reason_through_false_return():
    from unittest.mock import MagicMock
    observed = []
    for operation in ('query_selector', 'is_visible', 'click', 'wait'):
        page = MagicMock()
        original = TimeoutError('synthetic')
        if operation == 'query_selector':
            page.query_selector.side_effect = original
        elif operation == 'is_visible':
            page.query_selector.return_value.is_visible.side_effect = original
        elif operation == 'click':
            page.query_selector.return_value.click.side_effect = original
        else:
            page.wait_for_timeout.side_effect = [None, None, original]
        with pytest.raises(RuntimeError, match='overview-navigation-failed') as caught:
            object.__new__(ScsbCrawler).collect(page, Mock())
        observed.append(exception_error_id(caught.value, bank='scsb'))
    assert None not in observed
    assert len(set(observed)) == len(observed), observed


def test_scsb_period_rejection_reasons_and_date_parse_fields_are_distinct():
    reasons = []
    for reason in ('custom-period-controls-missing', 'incremental-start-unavailable', 'invalid-period-limit', 'synthetic-unknown'):
        with pytest.raises(RuntimeError) as caught:
            ScsbCrawler._apply_twd_period_controls(Mock(), {'ok': False, 'error': reason})
        reasons.append(exception_error_id(caught.value, bank='scsb'))
    assert None not in reasons
    assert len(set(reasons)) == len(reasons), reasons


def test_scsb_date_parse_fields_are_distinct():
    dates = []
    for start, end in [('bad', '2026/01/02'), ('2026/01/01', 'bad')]:
        with pytest.raises(ValueError) as caught:
            ScsbCrawler._six_month_windows(start, end)
        dates.append(exception_error_id(caught.value, bank='scsb'))
    assert None not in dates
    assert len(set(dates)) == len(dates), dates


def test_scsb_statement_amount_fields_are_distinct():
    observed = []
    labels = ('Current Period Total Amount Due', 'Current Period Total Minimum Amount Due')
    for role in range(2):
        text = '\n'.join(f'{label}\n' + ('invalid' if index == role else '1') for index, label in enumerate(labels))
        with pytest.raises(ValueError) as caught:
            ScsbCrawler._statement_month_summary(text)
        observed.append(exception_error_id(caught.value, bank='scsb'))
    assert None not in observed
    assert len(set(observed)) == len(observed), observed


def test_hsbc_inventory_bounded_fields_have_ordered_type_and_length_reasons():
    observed = []
    valid = {'id': 'synthetic', 'maskedCardNumber': '1234-****-****-5678', 'cardStatusDisplay': 'ACTIVATED'}
    for key in ('name', 'cardType', 'paymentDueDate', 'statementDate'):
        for value in (1, 'x' * 257):
            collector = ResponseCollector()
            collector.hits = [inventory_hit(resp_json={'success': True, 'payload': [{**valid, key: value}]})]
            with pytest.raises(RuntimeError, match='hsbc-card-inventory-identity') as caught:
                HsbcCrawler._card_inventory(collector)
            observed.append(exception_error_id(caught.value, bank='hsbc'))
    assert None not in observed
    assert len(set(observed)) == len(observed), observed


def test_scsb_query_request_and_response_property_reads_are_distinct():
    from unittest.mock import PropertyMock
    from tests.test_scsb_accounts_and_twd_inquiry import _mock_twd_query_page
    observed = []
    for role in ('request', 'response'):
        page = _mock_twd_query_page()
        original = TimeoutError('synthetic')
        target = page.expect_request.return_value.__enter__.return_value if role == 'request' else page._query_response
        attribute = 'value' if role == 'request' else 'ok'
        setattr(type(target), attribute, PropertyMock(side_effect=original))
        with pytest.raises(RuntimeError) as caught:
            object.__new__(ScsbCrawler)._collect_twd_inquiry(page, {'90000000123456'})
        assert original in context_chain(caught.value)
        observed.append(exception_error_id(caught.value, bank='scsb'))
    assert None not in observed
    assert len(set(observed)) == len(observed), observed


def test_scsb_nested_nonempty_table_guard_has_independent_reasons(monkeypatch):
    from unittest.mock import MagicMock
    import backend.core.persist.scsb as persist_module
    observed = []
    for rows, complete, state in [([], True, {'ok': True, 'row_count': 1}),
                                  ([{}], False, {'ok': True, 'row_count': 1}),
                                  ([{}], True, {'ok': False, 'row_count': 1})]:
        page = MagicMock()
        control = page.locator.return_value
        control.filter.return_value = control
        control.count.return_value = 1
        control.first.is_visible.return_value = True
        control.first.get_attribute.return_value = 'collapsed'
        def evaluate(script, arg=None):
            if script == ScsbCrawler._card_leaf_scope_text_script():
                return arg + ' 目前無新增交易'
            if script == ScsbCrawler._card_leaf_table_state_script():
                return state
            if "querySelectorAll('input')" in script:
                return []
            raise AssertionError('unexpected synthetic page read')
        page.evaluate.side_effect = evaluate
        monkeypatch.setattr(persist_module, '_scsb_parse_card_rows',
                            lambda text, *, scope: ([], True) if scope == 'unbilled' else (rows, complete))
        with pytest.raises(RuntimeError) as caught:
            object.__new__(ScsbCrawler)._collect_credit_card_inquiry(page)
        observed.append(exception_error_id(caught.value, bank='scsb'))
    assert None not in observed
    assert len(set(observed)) == len(observed), observed


def test_hsbc_zero_page_total_has_independent_page_and_row_reasons(monkeypatch):
    observed = []
    base = 'https://card.hsbc.com.tw/ibk-bff/api/v1/cards/synthetic/transactions/posted?pageSize=10&pageNumber='
    for page_number in (0, 1):
        responses = []
        for number in range(page_number + 1):
            total, rows = (2, [{}] * 10) if number < page_number else (0, [{}] if number == 0 else [])
            responses.append({'url': base + str(number), 'status': 200, 'contentType': 'application/json',
                              'redirected': False, 'bytes': 1, 'body': {'success': True, 'payload': {
                                  'pageInfo': {'currentPageIndex': number, 'totalPages': total}, 'content': rows}}})
        fetch = Mock(side_effect=responses)
        monkeypatch.setattr(HsbcCrawler, '_fetch_api_page', fetch)
        monkeypatch.setattr(HsbcCrawler, '_validate_posted_row', Mock())
        with pytest.raises(RuntimeError) as caught:
            HsbcCrawler._fetch_posted_history(Mock(), card_id='synthetic', identity='synthetic', token='Bearer synthetic',
                                             start=date(2026, 1, 1), end=date(2026, 1, 2))
        observed.append(exception_error_id(caught.value, bank='hsbc'))
    assert None not in observed
    assert len(set(observed)) == len(observed), observed


def test_hsbc_token_rejections_distinguish_security_checks_without_poisoning_valid_events():
    valid = {'url': 'https://card.hsbc.com.tw/ibk-bff/api/v1/cards', 'token': 'Bearer synthetic', 'redirected': False}
    events = [None, {**valid, 'token': 'invalid'}, {**valid, 'url': 'http://card.hsbc.com.tw/ibk-bff/api/v1/cards'},
              {**valid, 'url': 'https://synthetic.invalid/ibk-bff/api/v1/cards'},
              {**valid, 'url': 'https://card.hsbc.com.tw:444/ibk-bff/api/v1/cards'},
              {**valid, 'url': 'https://synthetic@card.hsbc.com.tw/ibk-bff/api/v1/cards'},
              {**valid, 'url': 'https://synthetic:synthetic@card.hsbc.com.tw/ibk-bff/api/v1/cards'},
              {**valid, 'url': 'https://card.hsbc.com.tw/untrusted'}, {**valid, 'redirected': True}]
    observed = []
    for event in events:
        collector = SimpleNamespace(auth_token_events=[event])
        with pytest.raises(RuntimeError, match='hsbc-history-token') as caught:
            HsbcCrawler._history_token(collector)
        observed.append(exception_error_id(caught.value, bank='hsbc'))
        collector.auth_token_events.append(valid)
        assert HsbcCrawler._history_token(collector) == 'Bearer synthetic'
    # A credential-bearing URL rejects on username before its password check.
    assert observed[5] == observed[6]
    assert None not in observed
    assert len(set(observed)) == len(observed) - 1, observed


def context_chain(error):
    result = []
    while error is not None and error not in result:
        result.append(error)
        error = error.__context__
    return result


def inventory_hit(**changes):
    values = dict(url='https://card.hsbc.com.tw/ibk-bff/api/v1/cards', method='GET', status=200,
                  content_type='application/json', body_size=10,
                  resp_json={'success': True, 'payload': []})
    values.update(changes)
    return ApiHit(**values)


def test_hsbc_inventory_compound_reasons_and_short_circuit():
    observed = []
    for changes in ({'body_size': None}, {'body_size': -1}, {'method': 'POST'}, {'status': 503},
                    {'content_type': 'text/html'}, {'resp_json': []}, {'resp_json': {'success': False}},
                    {'resp_json': {'success': True, 'error': 'synthetic'}},
                    {'resp_json': {'success': True, 'payload': {}}}):
        collector = ResponseCollector()
        collector.hits = [inventory_hit(**changes)]
        with pytest.raises(RuntimeError) as caught:
            HsbcCrawler._card_inventory(collector)
        observed.append(exception_error_id(caught.value, bank='hsbc'))
    assert None not in observed
    assert len(set(observed)) == len(observed)


def test_hsbc_posted_rows_reasons_are_not_the_exception_class():
    valid = dict(postedDate='2026-01-02T00:00', transactionDate='2026-01-01T00:00',
                 ntdAmount='10 TWD', description='synthetic', isPositive=True, isForeign=False)
    observed = []
    for changes in ({'postedDate': 'bad'}, {'transactionDate': 'bad'}, {'ntdAmount': 'bad'},
                    {'ntdAmount': '1.5 TWD'}, {'isPositive': None}, {'isForeign': None},
                    {'description': None}, {'description': ''}):
        with pytest.raises(RuntimeError) as caught:
            HsbcCrawler._validate_posted_row({**deepcopy(valid), **changes}, end=date(2026, 1, 3))
        observed.append(exception_error_id(caught.value, bank='hsbc'))
    assert None not in observed
    assert len(set(observed)) == len(observed)


def frozen_method(cls, name):
    module = inspect.getmodule(cls)
    relative = Path(module.__file__).relative_to(Path(__file__).resolve().parents[1])
    source = subprocess.check_output(['git', 'show', f'16f2d4b130f44973d346b420f3d486ac839f71ac:{relative}'], text=True)
    tree = ast.parse(source)
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == cls.__name__)
    method = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == name)
    method.decorator_list = []
    namespace = dict(vars(module))
    exec(compile(ast.Module(body=[method], type_ignores=[]), '<frozen-bank-method>', 'exec'), namespace)
    return namespace[name]


@pytest.mark.parametrize('cls,name,roles,operations,swallowed', [
    (HsbcCrawler, '_logged_in', ('#userId', '#password', '#captchaInput', 'body'), ('locator', 'count', 'nth', 'is_visible', 'inner_text'), True),
    (ScsbCrawler, '_logged_in', ('#userId, #idNumber, #pppd, #verified, .ved_img', 'body'), ('locator', 'count', 'nth', 'is_visible', 'inner_text'), True),
    (HsbcCrawler, '_response_visible', ('.modal.show', "[role='dialog']", '.error', '.alert', "[role='alert']"), ('locator', 'count', 'nth', 'is_visible'), False),
    (ScsbCrawler, '_visible_blocker', ('.modal.show', "[role='dialog']", '.error', '.alert', "[role='alert']"), ('locator', 'count', 'nth', 'is_visible'), False),
    (HsbcCrawler, '_refresh_captcha', ("button[aria-label='Refresh Captcha']",), ('locator', 'count', 'nth-1', 'is_visible', 'nth-2', 'is_enabled', 'nth-3', 'click'), False),
    (ScsbCrawler, '_click_unique_refresh', ('.chg_link',), ('locator', 'count', 'nth', 'is_visible', 'is_enabled', 'inner_text', 'click', 'wait_for_timeout'), False),
])
def test_native_probe_matrix_preserves_trace_identity_and_distinct_roles(cls, name, roles, operations, swallowed):
    baseline = frozen_method(cls, name)
    crawler = object.__new__(cls)
    bank = 'hsbc' if cls is HsbcCrawler else 'scsb'
    seen = []
    for role in roles:
        for operation in operations:
            if name == '_logged_in' and ((role == 'body' and operation not in ('locator', 'inner_text')) or (role != 'body' and operation == 'inner_text')):
                continue
            traces = []
            for frozen in (True, False):
                original = TimeoutError('synthetic-sensitive-native')
                trace = []
                counters = {}
                def action(actual, op, result):
                    trace.append((actual, op))
                    if (actual, op) == (role, operation):
                        raise original
                    return result
                def locator(selector):
                    node = Mock()
                    node.count.side_effect = lambda: action(selector, 'count', 1)
                    def nth(index):
                        counters[selector] = counters.get(selector, 0) + 1
                        op = f'nth-{counters[selector]}' if name == '_refresh_captcha' else 'nth'
                        return action(selector, op, node)
                    node.nth.side_effect = nth
                    node.is_visible.side_effect = lambda: action(selector, 'is_visible', name not in ('_logged_in', '_response_visible', '_visible_blocker'))
                    node.is_enabled.side_effect = lambda: action(selector, 'is_enabled', True)
                    node.inner_text.side_effect = lambda: action(selector, 'inner_text', 'Refresh' if name == '_click_unique_refresh' else '')
                    node.click.side_effect = lambda: action(selector, 'click', None)
                    return action(selector, 'locator', node)
                page = SimpleNamespace(url='https://card.hsbc.com.tw/#/cards' if bank == 'hsbc' else 'https://ebank.scsb.com.tw/APLY/main', locator=locator,
                                       wait_for_timeout=lambda ms: action(roles[0], 'wait_for_timeout', None))
                if swallowed:
                    result = baseline(crawler, page) if frozen else getattr(crawler, name)(page)
                    assert result is False
                else:
                    with pytest.raises(TimeoutError) as caught:
                        baseline(page) if frozen else getattr(cls, name)(page)
                    assert caught.value is original
                assert trace.count((role, operation)) == 1
                traces.append(trace)
                if not frozen:
                    error_id = exception_error_id(original, bank=bank)
                    assert code_details(error_id, bank=bank), (name, role, operation)
                    seen.append(error_id)
            assert traces[0] == traces[1]
    assert len(set(seen)) == len(seen), (name, seen)


def history_call(page):
    return HsbcCrawler._fetch_posted_history(page, card_id='synthetic', identity='synthetic', token='Bearer synthetic',
                                            start=date(2026, 1, 1), end=date(2026, 1, 2))


def test_hsbc_api_sentinel_reasons_reach_original_terminal_guards():
    import json
    observed = []
    for role in ('inventory', 'posted', 'attest'):
        for failure in ('native', 'shape', 'text', 'json'):
            page = Mock()
            original = TimeoutError('synthetic-sensitive-native')
            good = {'url': 'https://card.hsbc.com.tw/ibk-bff/api/v1/cards/synthetic/transactions/posted?pageSize=10&pageNumber=0',
                    'status': 200, 'contentType': 'application/json', 'redirected': False, 'bytes': 1,
                    'text': json.dumps({'success': True, 'payload': {'pageInfo': {'currentPageIndex': 0, 'totalPages': 0}, 'content': []}})}
            bad = original if failure == 'native' else None if failure == 'shape' else {'text': 1} if failure == 'text' else {'text': '{'}
            page.evaluate.side_effect = [deepcopy(good), bad] if role == 'attest' else [bad]
            with pytest.raises(RuntimeError) as caught:
                if role == 'inventory':
                    HsbcCrawler._fetch_card_inventory(page, 'Bearer synthetic', [5000000])
                else:
                    history_call(page)
            assert str(caught.value) == ('hsbc-card-inventory-byte-budget' if role == 'inventory' else 'hsbc-posted-history')
            error_id = exception_error_id(caught.value, bank='hsbc')
            assert code_details(error_id, bank='hsbc')
            assert page.evaluate.call_count == (2 if role == 'attest' else 1)
            observed.append(error_id)
    assert len(set(observed)) == len(observed), observed


def test_hsbc_fetch_diagnostics_are_local_bounded_and_reset_on_success():
    import json
    ids = []
    observed = []
    page = Mock()
    for role in ('inventory', 'posted', 'detail', 'unposted'):
        original = TimeoutError('synthetic-sensitive-native')
        page.evaluate.side_effect = original
        assert HsbcCrawler._fetch_api_page(page, url='synthetic', token='synthetic', timeout_ms=1,
                                            diagnostics=ids, diagnostic_role=role) is None
        assert len(ids) == 1 and code_details(ids[0], bank='hsbc')
        assert exception_error_id(original, bank='hsbc') == ids[0]
        observed.append(ids[0])
        page.evaluate.side_effect = None
        page.evaluate.return_value = {'text': json.dumps({'success': True, 'payload': []})}
        assert HsbcCrawler._fetch_api_page(page, url='synthetic', token='synthetic', timeout_ms=1,
                                           diagnostics=ids, diagnostic_role=role)['body']['payload'] == []
        assert ids == []
    assert len(set(observed)) == len(observed)


def test_hsbc_fetch_json_optional_failures_stay_nonfatal_and_warning_is_fixed(capsys):
    page = Mock()
    ids = []
    observed = []
    for role, path in [('detail', 'cards/synthetic'), ('unposted', 'cards/synthetic/transactions/unposted?pageSize=200')]:
        for result in (None, {}, {'bytes': 1, 'body': []}, {'bytes': 1, 'body': {'success': False}},
                       {'bytes': 1, 'body': {'success': True, 'error': 'synthetic-sensitive'}}):
            with __import__('unittest.mock', fromlist=['patch']).patch.object(HsbcCrawler, '_fetch_api_page', return_value=deepcopy(result)):
                assert HsbcCrawler._fetch_json(page, path, 'Bearer synthetic', diagnostics=ids) is None
                assert len(ids) == 1 and code_details(ids[0], bank='hsbc')
                observed.append(ids[0])
    assert len(set(observed)) == len(observed), observed
    assert capsys.readouterr().err == ''


def captcha_fixture(bank, failure, monkeypatch):
    import base64
    module = __import__(f'backend.banks.{bank}', fromlist=['ocr_bytes'])
    crawler, page, original, events = login_fixture(bank, None)
    delattr(crawler, '_solve_captcha' if bank == 'hsbc' else '_ocr_captcha')
    normal_locator = page.locator.side_effect
    node = Mock()
    collection = Mock()
    def action(op, result):
        events.append(('captcha-image', op))
        if failure == op:
            raise original
        return result
    collection.count.side_effect = lambda: action('count', 1)
    collection.nth.side_effect = lambda index: action('nth', node)
    node.is_visible.side_effect = lambda: action('visible', True)
    node.get_attribute.side_effect = lambda name: action('src', 'data:image/jpeg;base64,synthetic')
    node.bounding_box.side_effect = lambda: action('box', {'width': 128, 'height': 40})
    node.screenshot.side_effect = lambda: action('screenshot', b'synthetic')
    node.evaluate.side_effect = lambda script: action('background', 'url("data:image/png;base64,' + base64.b64encode(b'synthetic').decode() + '")')
    def locator(selector):
        if selector == ('img' if bank == 'hsbc' else '.ved_img'):
            return action('locator', collection)
        return normal_locator(selector)
    page.locator.side_effect = locator
    def ocr(*args, **kwargs):
        action('ocr', None)
        return None if failure == 'unreadable' else 'invalid!' if failure == 'format' else '12345'
    monkeypatch.setattr(module, 'ocr_bytes', ocr)
    return crawler, page, original, events


@pytest.mark.parametrize('bank,failures', [
    ('hsbc', ('locator', 'count', 'nth', 'visible', 'src', 'box', 'screenshot', 'ocr', 'unreadable', 'format')),
    ('scsb', ('locator', 'count', 'nth', 'visible', 'background', 'ocr', 'unreadable', 'format')),
])
def test_captcha_native_and_ocr_sentinels_reach_required_guard_without_submit(bank, failures, monkeypatch):
    observed = []
    for failure in failures:
        crawler, page, original, events = captcha_fixture(bank, failure, monkeypatch)
        with pytest.raises((HsbcLoginError, ScsbLoginError), match='無法安全辨識驗證碼；未送出登入') as caught:
            crawler.submit_credentials_once(page)
        error_id = exception_error_id(caught.value, bank=bank)
        assert code_details(error_id, bank=bank), failure
        if failure not in ('unreadable', 'format'):
            assert exception_error_id(original, bank=bank) == error_id
        assert ('submit', 'click') not in events
        observed.append(error_id)
    assert len(set(observed)) == len(observed), observed


@pytest.mark.parametrize('bank', ['hsbc', 'scsb'])
def test_captcha_out_metadata_resets_per_call_and_success(bank, monkeypatch):
    ids = []
    for failure, expected in [('ocr', None), ('unreadable', None), ('success', '12345')]:
        crawler, page, original, events = captcha_fixture(bank, failure, monkeypatch)
        method = crawler._solve_captcha if bank == 'hsbc' else crawler._ocr_captcha
        kwargs = {} if bank == 'hsbc' else {'max_attempts': 1}
        assert method(page, diagnostics=ids, **kwargs) == expected
        if expected is None:
            assert len(ids) == 1 and code_details(ids[0], bank=bank)
        else:
            assert ids == []
    assert not any('error' in key or 'diagnostic' in key for key in vars(crawler))


FETCH_TRACE_HARNESS = r'''
const fs = require('fs');
const vm = require('vm');
(async () => {
 const {script, mode} = JSON.parse(fs.readFileSync(0, 'utf8'));
 const trace = [];
 const args = {url:'https://synthetic.invalid/cards',tok:'Bearer synthetic',timeoutMs:1,maxBytes:1000,expectedSnapshot:{pageInfo:{},content:[]}};
 const bytes = mode === 'decode' ? new Uint8Array([255]) : new TextEncoder().encode('{"success":true,"payload":{"pageInfo":{},"content":[]}}');
 let reads = 0;
 const reader = {read: async () => {trace.push('read'); if(mode==='read')throw Error('synthetic'); return reads++ ? {done:true} : {done:false,value:mode==='overflow'?new Uint8Array(1001):bytes};},
                 cancel:async()=>trace.push('reader.cancel')};
 const body = {getReader:()=>{trace.push('getReader');return reader;},cancel:async()=>trace.push('body.cancel')};
 const response = {headers:{get:key=>{trace.push('header:'+key);return key==='content-type'?(mode==='content'?'text/html':'application/json'):mode==='length-format'?'invalid':mode==='length-budget'?'1001':null;}},
                   get url(){trace.push('url');return mode==='url'?'https://other.invalid':args.url;},
                   get status(){trace.push('status');return mode==='status'?503:200;},
                   get body(){trace.push('body');return body;},
                   get redirected(){trace.push('redirected');return false;}};
 const context = {AbortController:class {constructor(){trace.push('controller');this.signal={};}abort(){trace.push('abort');}},
  setTimeout:(fn,ms)=>{trace.push('timer');return 1;},clearTimeout:()=>trace.push('clear'),Uint8Array,
  TextDecoder:class {decode(input){trace.push('decode');return new TextDecoder('utf-8',{fatal:true}).decode(input);}},
  fetch:async(url,options)=>{trace.push(['fetch',url,options]);if(mode==='fetch')throw Error('synthetic');return response;}};
 let result = await vm.runInNewContext('('+script+')',context)(args);
 console.log(JSON.stringify({trace,result}));
})().catch(error=>{console.error(error);process.exit(1);});
'''


def run_offline_js(script, harness, mode):
    import json
    import shutil
    node = shutil.which('node')
    assert node, 'offline JavaScript verification requires Node.js'
    return json.loads(subprocess.check_output([node, '-e', harness], input=json.dumps({'script': script, 'mode': mode}), text=True))


@pytest.mark.parametrize('name', ['_fetch_api_page', '_attest_api_page'])
def test_hsbc_embedded_fetch_reasons_preserve_frozen_request_read_trace_and_public_returns(name):
    kwargs = dict(url='synthetic', token='synthetic', timeout_ms=1, max_bytes=1000)
    if name == '_attest_api_page':
        kwargs['expected_snapshot'] = {'pageInfo': {}, 'content': []}
    old_page, new_page = Mock(), Mock()
    old_page.evaluate.return_value = None
    new_page.evaluate.return_value = None
    frozen_method(HsbcCrawler, name)(old_page, **kwargs)
    getattr(HsbcCrawler, name)(new_page, **kwargs)
    before, after = old_page.evaluate.call_args.args[0], new_page.evaluate.call_args.args[0]
    seen = []
    ids = []
    for mode in ('url', 'status', 'content', 'length-format', 'length-budget', 'overflow', 'fetch', 'read', 'decode', 'success'):
        old = run_offline_js(before, FETCH_TRACE_HARNESS, mode)
        new = run_offline_js(after, FETCH_TRACE_HARNESS, mode)
        assert old['trace'] == new['trace'], mode
        old_page.evaluate.return_value = old['result']
        new_page.evaluate.return_value = new['result']
        expected = frozen_method(HsbcCrawler, name)(old_page, **kwargs)
        actual = getattr(HsbcCrawler, name)(new_page, diagnostics=ids, **kwargs)
        assert actual == expected, mode
        if mode == 'success':
            assert ids == []
        else:
            assert len(ids) == 1 and code_details(ids[0], bank='hsbc'), mode
            seen.append(ids[0])
    assert len(set(seen)) == len(seen), seen


def test_scsb_discarded_required_rows_keep_closed_reasons_without_reporting_normal_nonmatches():
    valid = ['2026/01/01', 'synthetic', '1', '', '10']
    candidates = [valid[:4], ['2026-01-01', *valid[1:]], ['2026/02/30', *valid[1:]]]
    for index, value in [(1, ''), (4, ''), (2, 'invalid'), (3, 'invalid'), (3, '2'), (2, '-1')]:
        candidate = valid.copy()
        candidate[index] = value
        candidates.append(candidate)
    deposit_negative = valid.copy()
    deposit_negative[2:4] = ['', '-1']
    candidates.append(deposit_negative)
    ids = []
    observed = []
    for candidate in candidates:
        text = 'non-row header\n' + '\t'.join(candidate)
        before = frozen_method(ScsbCrawler, '_parse_twd_inquiry_records')(text)
        assert ScsbCrawler._parse_twd_inquiry_records(text, diagnostics=ids) == before == ([], False)
        assert len(ids) == 1 and code_details(ids[0], bank='scsb')
        observed.append(ids[0])
    assert len(set(observed)) == len(observed), observed
    assert ScsbCrawler._parse_twd_inquiry_records('non-row header', diagnostics=ids) == ([], True)
    assert ids == []
    assert ScsbCrawler._parse_twd_inquiry_records('\t'.join(valid), diagnostics=ids)[1] is True
    assert ids == []


DOM_TRACE_HARNESS = r'''
const fs=require('fs'),vm=require('vm');
(async()=>{
 const {script,mode}=JSON.parse(fs.readFileSync(0,'utf8'));
 const {kind,failure}=mode;
 const trace=[];
 const observed=(name,object)=>new Proxy(object,{get(target,key){if(typeof key==='string')trace.push(name+'.'+key);return target[key];}});
 const empty='很抱歉，您在指定條件內，沒有任何餘額及交易明細的資料。';
 const control=(name,text)=>observed(name,{offsetParent:{},innerText:text,textContent:text,children:[],dataset:{},
  matches:()=>{trace.push(name+'.matches()');return false;},click:()=>trace.push(name+'.click()')});
 let stage=0;
 const scope=observed('scope',{offsetParent:failure==='scope'?null:{},children:[],innerText:failure==='empty-missing'?'synthetic':empty,
  contains:()=>{trace.push('contains()');return failure!=='outside';},
  querySelectorAll:selector=>{trace.push('scope.queryAll:'+selector);if(selector==='*')return kind==='extract'&&failure!=='criteria'? [control('criteria','Query Criteria 1234 2026/01/01 2026/01/02')] : [];
   if(kind==='close')return failure==='buttons'?[]:[control('close','Close')];return [];}});
 const result=observed('result',{offsetParent:failure==='result'?null:{},innerText:failure==='empty'?empty:'synthetic',children:[],tagName:'DIV'});
 const period={custom:observed('custom',{checked:failure!=='custom'}),start:observed('start',{value:failure==='start'?'wrong':'2026/01/01'}),end:observed('end',{value:failure==='end'?'wrong':'2026/01/02'})};
 const document={body:{},querySelector:selector=>{trace.push('document.query:'+selector);return period[Object.keys(period).find(key=>selector.includes('="'+key+'"'))];},
  querySelectorAll:selector=>{trace.push('document.queryAll:'+selector);
   if(kind==='nav') {const i=stage++;if(failure==='head'&&i===0||failure==='leaf'&&i===2)return [];return [control('nav'+i,['TWD Deposit','TWD Account Inquiry','Account Balance'][i]||'synthetic')];}
   if(kind==='statement')return selector==='body *'?[control('closeDate','帳單結帳日'),control('message','帳單訊息'),control('balance','信用卡前期餘額')].filter((el,i)=>i!==({closeDate:0,message:1,balance:2}[failure])):failure==='headings'?[]:[control('heading','帳單查詢及繳款')];
   return [];}};
 const window={__thothScsbTwdResultScope:scope,__thothScsbTwdResultElement:result};
 const context={document,window,location:{href:'synthetic'},setTimeout:(fn,ms)=>{trace.push('timer:'+ms);fn();}};
 const output=await vm.runInNewContext('('+script+')',context)({start:'2026/01/01',end:'2026/01/02',account_no:'1234'});
 console.log(JSON.stringify({trace,result:output}));
})().catch(error=>{console.error(error);process.exit(1);});
'''


@pytest.mark.parametrize('name,kind,failures', [
    ('_twd_inquiry_nav_script', 'nav', ('head', 'leaf')),
    ('_twd_inquiry_period_verification_script', 'period', ('custom', 'start', 'end')),
    ('_twd_inquiry_extract_result_script', 'extract', ('scope', 'result', 'outside', 'criteria', 'table')),
    ('_twd_empty_result_close_script', 'close', ('scope', 'empty-missing', 'buttons')),
    ('_card_statement_state_script', 'statement', ('headings', 'closeDate', 'message', 'balance')),
])
def test_scsb_terminal_dom_reasons_keep_frozen_read_order(name, kind, failures):
    before = frozen_method(ScsbCrawler, name)()
    after = getattr(ScsbCrawler, name)()
    observed = []
    for failure in (*failures, 'success'):
        mode = {'kind': kind, 'failure': 'empty' if kind == 'extract' and failure == 'success' else failure}
        old = run_offline_js(before, DOM_TRACE_HARNESS, mode)
        new = run_offline_js(after, DOM_TRACE_HARNESS, mode)
        assert old['trace'] == new['trace'], (name, failure)
        marker = new['result'].pop('_diagnostic', None)
        assert new['result'] == old['result'], (name, failure)
        if failure != 'success':
            assert code_details(marker, bank='scsb'), (name, failure, marker)
            observed.append(marker)
        else:
            assert marker is None
    assert len(set(observed)) == len(observed), observed


@pytest.mark.parametrize('kind,action,count,message', [
    ('nav', 610, 2, 'twd-inquiry-navigation-failed'),
    ('period', 611, 3, 'lookback-period-not-applied'),
    ('extract', 612, 5, 'query-result-unbound'),
    ('close', 613, 3, 'empty-result-close-unavailable'),
])
def test_scsb_dom_markers_reach_existing_terminal_guard(kind, action, count, message):
    from tests.test_scsb_accounts_and_twd_inquiry import _mock_twd_query_page
    for reason in range(1, count + 1):
        expected_id = f'SCSB-05-{action:03}-{reason:03}'
        page = _mock_twd_query_page()
        normal = page.evaluate.side_effect
        crawler = object.__new__(ScsbCrawler)
        target = getattr(crawler, {'nav': '_twd_inquiry_nav_script', 'period': '_twd_inquiry_period_verification_script',
                                  'extract': '_twd_inquiry_extract_result_script', 'close': '_twd_empty_result_close_script'}[kind])()
        def evaluate(script, *args, **kwargs):
            value = next(normal)
            if script == target:
                return {'ok': False, '_diagnostic': expected_id}
            return value
        page.evaluate.side_effect = evaluate
        with pytest.raises(RuntimeError, match=message if kind == 'close' else 'SCSB TWD inquiry failed') as caught:
            if kind == 'close':
                crawler._close_twd_empty_result(page)
            else:
                crawler._collect_twd_inquiry(page, {'90000000123456'})
        assert any(str(error) == message for error in context_chain(caught.value))
        assert exception_error_id(caught.value, bank='scsb') == expected_id


def test_hsbc_optional_overflow_reason_reaches_required_budget_guard(monkeypatch):
    crawler = object.__new__(HsbcCrawler)
    crawler.transaction_cursors = {}
    collector = SimpleNamespace(auth_token_events=[{'token': 'Bearer synthetic', 'url': 'https://card.hsbc.com.tw/ibk-bff/api/v1/cards', 'redirected': False}])
    page = Mock()
    page.evaluate.return_value = {'bytes': 5000001, 'exceeded': True, '_diagnostic': 6}
    history = Mock(side_effect=AssertionError('must stop before posted request'))
    monkeypatch.setattr(crawler, '_fetch_posted_history', history)
    with pytest.raises(RuntimeError, match='hsbc-history-byte-budget') as caught:
        crawler._collect_card_details(page, collector, [{'id': 'synthetic', 'maskedCardNumber': '1234-****-****-5678'}], end=date(2026, 1, 2))
    assert exception_error_id(caught.value, bank='hsbc') == 'HSBC-06-611-006'
    assert page.evaluate.call_count == 1
    history.assert_not_called()


def test_scsb_submit_text_or_value_native_reads_keep_distinct_ids_and_laziness():
    seen = []
    for op in ('inner_text', 'get_attribute'):
        crawler, page, original, events = login_fixture('scsb', None)
        normal = page.locator.side_effect
        def locator(selector):
            collection = normal(selector)
            if selector == "button, input[type='submit'], input[type='button']":
                button = collection.nth(0)
                button.inner_text.side_effect = original if op == 'inner_text' else lambda: ''
                button.get_attribute.side_effect = original
            return collection
        page.locator.side_effect = locator
        with pytest.raises(ScsbLoginError) as caught:
            crawler.submit_credentials_once(page)
        assert original in context_chain(caught.value)
        seen.append(exception_error_id(caught.value, bank='scsb'))
    assert None not in seen and len(set(seen)) == 2, seen


def test_scsb_rejected_row_reason_reaches_existing_required_terminal_guard():
    from tests.test_scsb_accounts_and_twd_inquiry import _mock_twd_query_page
    page = _mock_twd_query_page(full_text='2026/01/01\tsynthetic\tinvalid\t\t10')
    with pytest.raises(RuntimeError, match='SCSB TWD inquiry failed') as caught:
        object.__new__(ScsbCrawler)._collect_twd_inquiry(page, {'90000000123456'})
    assert exception_error_id(caught.value, bank='scsb') == 'SCSB-05-600-006'
    assert any(str(error) == 'partial-transaction-table' for error in context_chain(caught.value))


def test_hsbc_response_probe_caller_roles_reach_distinct_final_ids():
    seen = []
    for target in (1, 2, 3):
        crawler, page, original, events = login_fixture('hsbc', None)
        del crawler._response_visible
        crawler._logged_in.return_value = False
        normal = page.locator.side_effect
        visits = 0
        def locator(selector):
            nonlocal visits
            if selector in ('.modal.show', "[role='dialog']", '.error', '.alert', "[role='alert']"):
                if selector == '.modal.show':
                    visits += 1
                    if visits == target:
                        raise original
                collection = Mock()
                collection.count.return_value = 0
                return collection
            return normal(selector)
        page.locator.side_effect = locator
        with pytest.raises(HsbcLoginError) as caught:
            crawler.submit_credentials_once(page)
        assert original in context_chain(caught.value)
        seen.append(exception_error_id(caught.value, bank='hsbc'))
    assert None not in seen and len(set(seen)) == 3, seen


def test_scsb_required_current_table_count_roles_reach_distinct_final_ids(monkeypatch):
    from unittest.mock import MagicMock
    import backend.core.persist.scsb as persist_module
    seen = []
    for changes in ({'heading_count': 0}, {'visible_table_count': 2}, {'matching_table_count': 0}, {'ok': True, 'row_count': 2}):
        state = {'ok': False, 'row_count': None, 'heading_count': 1, 'visible_table_count': 1, 'matching_table_count': 1, **changes}
        page = MagicMock()
        control = page.locator.return_value
        control.filter.return_value = control
        control.count.return_value = 1
        control.first.is_visible.return_value = True
        control.first.get_attribute.return_value = 'collapsed'
        def evaluate(script, arg=None):
            if script == ScsbCrawler._card_leaf_scope_text_script():
                return arg + ' 目前無新增交易'
            if script == ScsbCrawler._card_leaf_table_state_script():
                return state
            if "querySelectorAll('input')" in script:
                return []
            raise AssertionError('unexpected synthetic page read')
        page.evaluate.side_effect = evaluate
        monkeypatch.setattr(persist_module, '_scsb_parse_card_rows',
                            lambda text, *, scope: ([], True) if scope == 'unbilled' else ([{}], True))
        with pytest.raises(RuntimeError) as caught:
            object.__new__(ScsbCrawler)._collect_credit_card_inquiry(page)
        seen.append(exception_error_id(caught.value, bank='scsb'))
    assert None not in seen and len(set(seen)) == 4, seen


def test_hsbc_stream_overflow_reason_reaches_inventory_and_posted_required_guards():
    seen = []
    for role in ('inventory', 'posted'):
        page = Mock()
        page.evaluate.return_value = {'bytes': 5000001, 'exceeded': True, '_diagnostic': 6}
        with pytest.raises(RuntimeError) as caught:
            if role == 'inventory':
                HsbcCrawler._fetch_card_inventory(page, 'Bearer synthetic', [5000000])
            else:
                history_call(page)
        seen.append(exception_error_id(caught.value, bank='hsbc'))
    assert seen == ['HSBC-04-601-006', 'HSBC-06-601-006']


# SCSB finite terminal residuals: real JS, fake DOM, no browser/credentials.
def scsb_assert_actual_sink(error, expected):
    """Exercise the real run() returned-failure sink; replace only I/O/setup."""
    crawler = object.__new__(ScsbCrawler)
    crawler.name = 'scsb'
    crawler._enforce_session_freshness = lambda: None
    crawler._make_collector = lambda page: Mock()
    crawler.attach_shared_dialog_handler = lambda page: None
    crawler._shared_login = lambda page: True
    crawler._ensure_collect_origin = lambda page: None
    crawler._credential_origin_allowed = lambda page: False
    crawler._build_fetch_kwargs = lambda: {}
    crawler._execute_browser_flow = lambda login_url, **kwargs: kwargs['page_action'](object())
    def collect(page, collector):
        raise error
    crawler.collect = collect
    result = crawler.run('https://synthetic.invalid/')
    assert result['collect_diagnostics']['error_id'] == expected
    assert expected in result['error']
    assert any('\u4e00' <= char <= '\u9fff' for char in result['error'])
    assert 'synthetic-' not in result['error']
    assert 'data' not in result
    return result


SCSB_CARD_HARNESS = r'''
const fs=require('fs'),vm=require('vm');
const {script,mode}=JSON.parse(fs.readFileSync(0,'utf8'));
const trace=[];
const observed=(name,o)=>new Proxy(o,{get(o,k){if(typeof k==='string')trace.push(name+'.'+k);return o[k];}});
const leafText=mode.leaf||'帳單查詢及繳款';
const control=observed('control',{offsetParent:{},textContent:'2026/01',innerText:'Confirm',click:()=>trace.push('click')});
const body={};
const scope=observed('scope',{tagName:mode.failure==='scope'?'MAIN':'DIV',parentElement:body,innerText:leafText+' Data Time',
 querySelector:s=>{trace.push('scope.query:'+s);return {};},
 querySelectorAll:s=>{trace.push('scope.queryAll:'+s);return mode.failure==='zero'?[]:mode.failure==='duplicate'?[control,control]:[control];}});
const leaf=observed('leaf',{offsetParent:{},textContent:leafText,innerText:leafText,children:[],parentElement:scope});
const document={body,querySelectorAll:s=>{trace.push('document.queryAll:'+s);return mode.failure==='leaf'?[]:[leaf];}};
const arg=mode.kind==='month'?['2026/01',leafText]:leafText;
const result=vm.runInNewContext('('+script+')',{document})(arg);
console.log(JSON.stringify({trace,result}));
'''


def scsb_inline_script(method, kind):
    from types import CodeType
    def strings(code):
        for value in code.co_consts:
            if isinstance(value, str):
                yield value
            elif isinstance(value, CodeType):
                yield from strings(value)
    return next(script for script in strings(method.__code__)
                if 'matches[0].click();' in script
                and ('button,a,li' in script) == (kind == 'month'))


def scsb_residual_crawler(method):
    crawler = object.__new__(ScsbCrawler)
    if method.__code__.co_filename == '<frozen-bank-method>':
        for name in ('_card_leaf_scope_text_script', '_card_statement_state_script', '_card_leaf_table_state_script', '_statement_month_target_script'):
            setattr(crawler, name, frozen_method(ScsbCrawler, name))
    return crawler


def scsb_residual_card_page(method, *, failure='success', panel=True, date_failure=None, leaf_failure=None):
    from unittest.mock import MagicMock
    from contextlib import contextmanager
    trace = []
    state = {'key': 'unbilled', 'evals': 0, 'date': None, 'scope_reads': {}}
    labels = {'unbilled': '未出帳消費明細', 'current': '即時消費紀錄', 'statement': '帳單查詢及繳款'}
    summary = 'Current Period Total Amount Due\n1\nCurrent Period Total Minimum Amount Due\n1'
    page = MagicMock()
    node = page.locator.return_value
    node.filter.return_value = node
    for target, operation, result in [(node, 'count', 1), (node.first, 'is_visible', True),
                                       (node.first, 'get_attribute', 'collapsed'), (node.first, 'click', None)]:
        getattr(target, operation).side_effect = lambda *a, op=operation, r=result, **kw: (trace.append((op, a)), r)[1]
    def locator(selector):
        trace.append(('locator', selector))
        return node
    page.locator.side_effect = locator
    def timeout(ms):
        trace.append(('wait', ms))
        if ms in (200, 300) and date_failure and state['key'] == date_failure[0]:
            edit('wait' + str(ms))
    page.wait_for_timeout.side_effect = timeout
    def wait(script, **kwargs):
        if 'route, leafText' in script:
            state['key'] = next(k for k, v in labels.items() if v == kwargs['arg'][1])
        trace.append(('wait_for_function', tuple(kwargs.items())))
    page.wait_for_function.side_effect = wait
    @contextmanager
    def expect(*args, **kwargs):
        trace.append(('expect_request', kwargs))
        request = SimpleNamespace(response=lambda: (trace.append(('response',)), SimpleNamespace(ok=True))[1])
        yield SimpleNamespace(value=request)
        trace.append(('expect_exit',))
    page.expect_request.side_effect = expect
    month_script = scsb_inline_script(method, 'month')
    confirm_script = scsb_inline_script(method, 'confirm')
    def evaluate(script, arg=None):
        key = state['key']
        state['evals'] += 1
        kind = 'month' if script == month_script else 'confirm' if script == confirm_script else (
            'scope' if 'const exactLeaf' in script else 'dates' if '.map(input => ({id: input.id, value: input.value}))' in script else
            'table' if 'heading_count' in script else 'statement' if "exact('帳單結帳日')" in script else
            'target' if 'rawTarget' in script else 'edit' if 'dispatchEvent' in script else 'panel')
        trace.append(('evaluate', key, kind, arg))
        if kind == 'month':
            value = run_offline_js(script, SCSB_CARD_HARNESS, {'kind': 'month', 'failure': failure})
            trace.extend(('dom', item) for item in value['trace'])
            return value['result']
        if kind == 'confirm':
            value = run_offline_js(script, SCSB_CARD_HARNESS, {'kind': 'confirm', 'failure': failure, 'leaf': labels[key]})
            trace.extend(('dom', item) for item in value['trace'])
            return value['result']
        if kind == 'scope':
            state['scope_reads'][key] = state['scope_reads'].get(key, 0) + 1
            if key == leaf_failure or (key, state['scope_reads'][key]) == leaf_failure:
                value = run_offline_js(script, SCSB_CARD_HARNESS, {'kind': 'scope', 'failure': 'leaf', 'leaf': labels[key]})
                trace.extend(('dom', item) for item in value['trace'])
                return value['result']
            return labels[key] + (' 目前無新增交易' if key == 'unbilled' else ' no real-time transaction' if key == 'current' else ' Data Time 2026/01\n' + summary)
        if kind == 'dates':
            return [{'id': 'date-one', 'value': ''}, {'id': 'date-two', 'value': ''}] if date_failure and key == date_failure[0] else []
        if kind == 'table':
            return {'ok': True, 'row_count': 0, 'heading_count': 1, 'visible_table_count': 1, 'matching_table_count': 1}
        if kind == 'statement':
            return {'ok': True}
        if kind == 'target':
            return {'ok': True, 'panel': '#panel' if panel else None}
        if kind == 'edit':
            edit('edit')
        return summary
    page.evaluate.side_effect = evaluate
    def edit(op, *args, **kwargs):
        trace.append((op, args, kwargs))
        if op == 'date.click':
            state['date'] = args[0]
        if date_failure and state['key'] == date_failure[0] and (date_failure[1] == 'all' or state['date'] == '#date-one') and op == date_failure[2]:
            raise TimeoutError('synthetic-date-native')
    page.click.side_effect = lambda *a, **kw: edit('date.click', *a, **kw)
    page.keyboard.press.side_effect = lambda *a, **kw: edit('select' if a == ('Control+A',) else 'delete', *a, **kw)
    page.keyboard.type.side_effect = lambda *a, **kw: edit('type', *a, **kw)
    return page, trace


@pytest.mark.parametrize('panel,expected', [(True, ('SCSB-06-730-001', 'SCSB-06-730-002')),
                                          (False, ('SCSB-06-731-001', 'SCSB-06-731-002'))])
@pytest.mark.parametrize('failure,index', [('zero', 0), ('duplicate', 0), ('leaf', 1), ('scope', 1)])
def test_scsb_residual_month_actual_js_and_role_sink(panel, expected, failure, index):
    before = frozen_method(ScsbCrawler, '_collect_credit_card_inquiry')
    after = ScsbCrawler._collect_credit_card_inquiry
    outcomes, traces = [], []
    for method in (before, after):
        page, trace = scsb_residual_card_page(method, failure=failure, panel=panel)
        with pytest.raises(RuntimeError, match='SCSB credit-card inquiry failed') as caught:
            method(scsb_residual_crawler(method), page)
        outcomes.append(caught.value)
        traces.append(trace)
    assert traces[0] == traces[1]
    assert exception_error_id(outcomes[1], bank='scsb') == expected[index]
    assert outcomes[1].__suppress_context__
    scsb_assert_actual_sink(outcomes[1], expected[index])
    assert code_details(expected[index], bank='scsb')
    assert sum(item[0] == 'expect_request' for item in traces[1]) == (not panel)
    assert not any(item == ('dom', 'click') for item in traces[1])


@pytest.mark.parametrize('panel', [True, False])
def test_scsb_residual_month_success_keeps_single_click_and_request_alternative(panel):
    traces, results = [], []
    for method in (frozen_method(ScsbCrawler, '_collect_credit_card_inquiry'), ScsbCrawler._collect_credit_card_inquiry):
        page, trace = scsb_residual_card_page(method, panel=panel)
        results.append(method(scsb_residual_crawler(method), page))
        traces.append(trace)
    assert results[0] == results[1]
    assert traces[0] == traces[1]
    assert traces[1].count(('dom', 'click')) == 1
    assert sum(item[0] == 'expect_request' for item in traces[1]) == (not panel)


@pytest.mark.parametrize('leaf,expected', [
    ('current', ('SCSB-06-339-001', 'SCSB-06-342-001', 'SCSB-06-345-001', 'SCSB-06-348-001', 'SCSB-06-351-001', 'SCSB-06-354-001', 'SCSB-06-357-001')),
    ('statement', ('SCSB-06-340-001', 'SCSB-06-343-001', 'SCSB-06-346-001', 'SCSB-06-349-001', 'SCSB-06-352-001', 'SCSB-06-355-001', 'SCSB-06-358-001')),
])
@pytest.mark.parametrize('operation,index', [('date.click', 0), ('wait200', 1), ('select', 2), ('delete', 3), ('type', 4), ('edit', 5), ('wait300', 6)])
def test_scsb_residual_date_exhaustion_reuses_final_native_id_and_recovery_clears(leaf, expected, operation, index):
    for mode in ('all', 'first'):
        traces, outcomes = [], []
        for method in (frozen_method(ScsbCrawler, '_collect_credit_card_inquiry'), ScsbCrawler._collect_credit_card_inquiry):
            page, trace = scsb_residual_card_page(method, date_failure=(leaf, mode, operation))
            if mode == 'all':
                with pytest.raises(RuntimeError, match='SCSB credit-card inquiry failed') as caught:
                    method(scsb_residual_crawler(method), page)
                outcomes.append(caught.value)
            else:
                outcomes.append(method(scsb_residual_crawler(method), page))
            traces.append(trace)
        assert traces[0] == traces[1]
        if mode == 'all':
            assert exception_error_id(outcomes[1], bank='scsb') == expected[index]
            assert any(str(error) == 'card-query-date-unavailable' for error in context_chain(outcomes[1]))
            scsb_assert_actual_sink(outcomes[1], expected[index])
            assert sum(item[0] == 'date.click' for item in traces[1]) == 2
        else:
            assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize('leaf,expected', [('current', ('SCSB-06-750-001', 'SCSB-06-750-002')),
                                         ('statement', ('SCSB-06-751-001', 'SCSB-06-751-002'))])
@pytest.mark.parametrize('failure,index', [('zero', 0), ('duplicate', 0), ('leaf', 1), ('scope', 1)])
def test_scsb_residual_confirm_actual_js_marker_and_existing_sink(leaf, expected, failure, index):
    traces, errors = [], []
    for method in (frozen_method(ScsbCrawler, '_collect_credit_card_inquiry'), ScsbCrawler._collect_credit_card_inquiry):
        page, trace = scsb_residual_card_page(method, failure=failure, date_failure=(leaf, 'all', 'never'))
        with pytest.raises(RuntimeError, match='SCSB credit-card inquiry failed') as caught:
            method(scsb_residual_crawler(method), page)
        errors.append(caught.value)
        traces.append(trace)
    assert traces[0] == traces[1]
    assert exception_error_id(errors[1], bank='scsb') == expected[index]
    assert any(str(error) == 'card-query-submit-unavailable' for error in context_chain(errors[1]))
    scsb_assert_actual_sink(errors[1], expected[index])
    assert code_details(expected[index], bank='scsb')
    assert ('dom', 'click') not in traces[1]


@pytest.mark.parametrize('leaf,expected', [('unbilled', 'SCSB-06-740-001'), ('current', 'SCSB-06-741-001'), ('statement', 'SCSB-06-742-001')])
def test_scsb_residual_scope_actual_js_marker_and_identity_sink(leaf, expected):
    traces = []
    for method in (frozen_method(ScsbCrawler, '_collect_credit_card_inquiry'), ScsbCrawler._collect_credit_card_inquiry):
        page, trace = scsb_residual_card_page(method, leaf_failure=leaf)
        with pytest.raises(RuntimeError, match='SCSB credit-card inquiry failed') as caught:
            method(scsb_residual_crawler(method), page)
        traces.append(trace)
    assert traces[0] == traces[1]
    assert exception_error_id(caught.value, bank='scsb') == expected
    assert any(str(error) == 'card-leaf-identity-unverified' for error in context_chain(caught.value))
    scsb_assert_actual_sink(caught.value, expected)
    assert code_details(expected, bank='scsb')


def test_scsb_residual_month_missing_scope_reaches_original_summary_terminal():
    traces, errors = [], []
    for method in (frozen_method(ScsbCrawler, '_collect_credit_card_inquiry'), ScsbCrawler._collect_credit_card_inquiry):
        page, trace = scsb_residual_card_page(method, panel=False, leaf_failure=('statement', 2))
        with pytest.raises(RuntimeError, match='SCSB credit-card inquiry failed') as caught:
            method(scsb_residual_crawler(method), page)
        traces.append(trace)
        errors.append(caught.value)
    assert traces[0] == traces[1]
    assert exception_error_id(errors[1], bank='scsb') == 'SCSB-06-743-001'
    scsb_assert_actual_sink(errors[1], 'SCSB-06-743-001')
    assert any(str(error) == 'incomplete statement month summary' for error in context_chain(errors[1]))


def test_scsb_residual_optional_final_scope_absence_keeps_old_text_fallback():
    traces, results = [], []
    for method in (frozen_method(ScsbCrawler, '_collect_credit_card_inquiry'), ScsbCrawler._collect_credit_card_inquiry):
        page, trace = scsb_residual_card_page(method, date_failure=('current', 'all', 'never'), leaf_failure=('current', 2))
        results.append(method(scsb_residual_crawler(method), page))
        traces.append(trace)
    assert results[0] == results[1]
    assert traces[0] == traces[1]


@pytest.mark.parametrize('kind,expected', [('native', 'SCSB-05-348-001'), ('close', 'SCSB-05-312-001'), ('fallback', 'SCSB-05-354-001')])
def test_scsb_residual_existing_twd_outer_copy_reaches_actual_returned_sink(kind, expected):
    from tests.test_scsb_accounts_and_twd_inquiry import _mock_twd_query_page
    page = _mock_twd_query_page()
    crawler = object.__new__(ScsbCrawler)
    original = TimeoutError('synthetic-private-native')
    if kind == 'native':
        page.evaluate.side_effect = original
    elif kind == 'close':
        page._confirm_buttons.first.click.side_effect = [None, original]
    else:
        crawler._collect_twd_account = Mock(side_effect=RuntimeError('synthetic-unannotated'))
    with pytest.raises(RuntimeError, match='SCSB TWD inquiry failed') as caught:
        crawler._collect_twd_inquiry(page, {'90000000123456'})
    assert exception_error_id(caught.value, bank='scsb') == expected
    assert caught.value.__suppress_context__
    scsb_assert_actual_sink(caught.value, expected)


@pytest.mark.parametrize('leaf,expected', [('unbilled', 'SCSB-06-326-001'), ('current', 'SCSB-06-327-001'), ('statement', 'SCSB-06-328-001')])
def test_scsb_residual_existing_credit_outer_copy_keeps_native_read_leaf(leaf, expected):
    method = ScsbCrawler._collect_credit_card_inquiry
    page, trace = scsb_residual_card_page(method)
    normal = page.evaluate.side_effect
    label = {'unbilled': '未出帳消費明細', 'current': '即時消費紀錄', 'statement': '帳單查詢及繳款'}[leaf]
    def evaluate(script, arg=None):
        if 'const exactLeaf' in script and arg == label:
            raise TimeoutError('synthetic-sensitive-native')
        return normal(script, arg)
    page.evaluate.side_effect = evaluate
    with pytest.raises(RuntimeError, match='SCSB credit-card inquiry failed') as caught:
        method(object.__new__(ScsbCrawler), page)
    scsb_assert_actual_sink(caught.value, expected)
    assert any(str(error) == 'card-leaf-read-failed' for error in context_chain(caught.value))


@pytest.mark.parametrize('field,value,expected', [('heading_count', 0, 'SCSB-06-720-001'), ('visible_table_count', 2, 'SCSB-06-720-002'), ('matching_table_count', 0, 'SCSB-06-720-003'), ('row_count', 2, 'SCSB-06-720-004')])
def test_scsb_residual_existing_table_ids_keep_actual_terminal_sink(field, value, expected, monkeypatch):
    import backend.core.persist.scsb as persist_module
    method = ScsbCrawler._collect_credit_card_inquiry
    page, trace = scsb_residual_card_page(method)
    normal = page.evaluate.side_effect
    def evaluate(script, arg=None):
        result = normal(script, arg)
        if 'heading_count' in script:
            result[field] = value
            result['ok'] = field == 'row_count'
        return result
    page.evaluate.side_effect = evaluate
    monkeypatch.setattr(persist_module, '_scsb_parse_card_rows', lambda text, *, scope: ([{}], True) if scope == 'current' else ([], True))
    with pytest.raises(RuntimeError, match='SCSB credit-card inquiry failed') as caught:
        method(object.__new__(ScsbCrawler), page)
    scsb_assert_actual_sink(caught.value, expected)


@pytest.mark.parametrize('mode,kwargs', [
    ('success', {'end_date': date(2026, 1, 2)}),
    ('bad-min', {'end_date': date(2026, 1, 2)}),
    ('system-limit', {'end_date': date(2026, 1, 2)}),
    ('success', {'end_date': SimpleNamespace(isoformat=lambda: 'bad')}),
    ('success', {'full_history': False, 'end_date': date(2026, 1, 2), 'start_date': date(2026, 1, 1)}),
    ('success', {'full_history': False, 'end_date': date(2026, 1, 2)}),
    ('success', {'full_history': False, 'end_date': date(2026, 1, 2), 'start_date': date(2026, 1, 3)}),
])
def test_scsb_residual_period_fallback_cursor_and_limits_preserve_full_trace(mode, kwargs):
    old = run_offline_js(frozen_method(ScsbCrawler, '_twd_inquiry_period_script')(**kwargs), SCSB_PERIOD_HARNESS, mode)
    new = run_offline_js(ScsbCrawler._twd_inquiry_period_script(**kwargs), SCSB_PERIOD_HARNESS, mode)
    assert old == new
    if new['result']['ok']:
        start, end = new['result']['start'], new['result']['end']
        assert ScsbCrawler._six_month_windows(start, end) == frozen_method(ScsbCrawler, '_six_month_windows')(start, end)


@pytest.mark.parametrize('marker', [999, True, 'SCSB-06-730-001'])
def test_scsb_residual_month_consumes_only_closed_marker(marker):
    traces, results = [], []
    for method in (frozen_method(ScsbCrawler, '_collect_credit_card_inquiry'), ScsbCrawler._collect_credit_card_inquiry):
        page, trace = scsb_residual_card_page(method)
        normal = page.evaluate.side_effect
        target = scsb_inline_script(method, 'month')
        def evaluate(script, arg=None):
            value = normal(script, arg)
            return {'_diagnostic': marker} if script == target else value
        page.evaluate.side_effect = evaluate
        results.append(method(scsb_residual_crawler(method), page))
        traces.append(trace)
    assert results[0] == results[1]
    assert traces[0] == traces[1]


def test_scsb_residual_last_candidate_id_only_and_recovered_reason_cannot_poison_later_leaf():
    method = ScsbCrawler._collect_credit_card_inquiry
    page, trace = scsb_residual_card_page(method, date_failure=('current', 'all', 'type'))
    click = page.click.side_effect
    def click_first_fails(selector):
        click(selector)
        if selector == '#date-one':
            raise TimeoutError('synthetic-first-candidate')
    page.click.side_effect = click_first_fails
    with pytest.raises(RuntimeError, match='SCSB credit-card inquiry failed') as caught:
        method(object.__new__(ScsbCrawler), page)
    scsb_assert_actual_sink(caught.value, 'SCSB-06-351-001')
    page, trace = scsb_residual_card_page(method, date_failure=('current', 'first', 'type'), leaf_failure='statement')
    with pytest.raises(RuntimeError, match='SCSB credit-card inquiry failed') as caught:
        method(object.__new__(ScsbCrawler), page)
    scsb_assert_actual_sink(caught.value, 'SCSB-06-742-001')


def test_scsb_residual_stale_boolean_reuses_original_terminal_without_extra_evaluation():
    from tests.test_scsb_accounts_and_twd_inquiry import _mock_twd_query_page
    page = _mock_twd_query_page()
    normal = page.evaluate.side_effect
    false = run_offline_js(ScsbCrawler._twd_inquiry_result_ready_script(), SCSB_RESULT_HARNESS, 'poll')['result']
    def evaluate(script, *args):
        value = next(normal)
        return false if script == ScsbCrawler._twd_inquiry_result_ready_script() else value
    page.evaluate.side_effect = evaluate
    with pytest.raises(RuntimeError, match='SCSB TWD inquiry failed') as caught:
        object.__new__(ScsbCrawler)._collect_twd_inquiry(page, {'90000000123456'})
    assert page.evaluate.call_count == 6
    scsb_assert_actual_sink(caught.value, 'SCSB-05-331-001')


def test_scsb_residual_trace_oracle_rejects_extra_native_reads_and_clicks():
    month = scsb_inline_script(ScsbCrawler._collect_credit_card_inquiry, 'month')
    for kind, script, mode in [('month', month, {'kind': 'month', 'failure': 'success'}),
                               ('scope', ScsbCrawler._card_leaf_scope_text_script(), {'kind': 'scope', 'failure': 'success'})]:
        original = run_offline_js(script, SCSB_CARD_HARNESS, mode)
        mutated = script.replace('const visible =', "document.querySelectorAll('body *'); const visible =", 1)
        assert run_offline_js(mutated, SCSB_CARD_HARNESS, mode)['trace'] != original['trace'], kind
    original = run_offline_js(month, SCSB_CARD_HARNESS, {'kind': 'month', 'failure': 'success'})
    for changed in ('', 'matches[0].click(); matches[0].click();'):
        mutated = month.replace('matches[0].click();', changed)
        assert run_offline_js(mutated, SCSB_CARD_HARNESS, {'kind': 'month', 'failure': 'success'})['trace'] != original['trace']


SCSB_RESULT_HARNESS = r'''
const fs=require('fs'),vm=require('vm');
const {script,mode}=JSON.parse(fs.readFileSync(0,'utf8'));
const trace=[];
const observed=(name,o)=>new Proxy(o,{get(o,k){if(typeof k==='string')trace.push(name+'.'+k);return o[k];}});
const body={};
const text='很抱歉，您在指定條件內，沒有任何餘額及交易明細的資料。';
const scope=observed('scope',{offsetParent:mode==='missing'?null:{},tagName:'DIV',innerText:'synthetic',parentElement:body,
 querySelectorAll:s=>{trace.push('scope.queryAll:'+s);return [];}});
const empty=observed('empty',{offsetParent:{},innerText:text,children:[],parentElement:scope});
const document={body,querySelectorAll:s=>{trace.push('document.queryAll:'+s);return s==='body *'&&mode!=='poll'? [empty]:[];}};
const window={__thothScsbTwdResultScope:scope};
const context=vm.createContext({document,window});
const first=vm.runInContext('('+script+')',context)({start:'2026/01/01',end:'2026/01/02',account_no:'1234'});
console.log(JSON.stringify({trace,result:first}));
'''


def test_scsb_residual_result_scope_actual_js_marker_reaches_outer_sink():
    from tests.test_scsb_accounts_and_twd_inquiry import _mock_twd_query_page
    before = frozen_method(ScsbCrawler, '_twd_inquiry_result_state_script')()
    after = ScsbCrawler._twd_inquiry_result_state_script()
    old = run_offline_js(before, SCSB_RESULT_HARNESS, 'missing')
    new = run_offline_js(after, SCSB_RESULT_HARNESS, 'missing')
    assert new['trace'] == old['trace']
    assert new['result'].get('_diagnostic') == 'SCSB-05-614-001'
    assert {k: v for k, v in new['result'].items() if k != '_diagnostic'} == old['result']
    page = _mock_twd_query_page(result_state=new['result'])
    with pytest.raises(RuntimeError, match='SCSB TWD inquiry failed') as caught:
        object.__new__(ScsbCrawler)._collect_twd_inquiry(page, {'90000000123456'})
    assert exception_error_id(caught.value, bank='scsb') == 'SCSB-05-614-001'
    scsb_assert_actual_sink(caught.value, 'SCSB-05-614-001')
    assert any(str(error) == 'query-result-incomplete' for error in context_chain(caught.value))


@pytest.mark.parametrize('failure,expected', [('headings', 'SCSB-06-710-001'), ('closeDate', 'SCSB-06-710-002'), ('message', 'SCSB-06-710-003'), ('balance', 'SCSB-06-710-004')])
def test_scsb_residual_existing_statement_js_reasons_reach_actual_sink(failure, expected):
    method = ScsbCrawler._collect_credit_card_inquiry
    page, trace = scsb_residual_card_page(method)
    normal = page.evaluate.side_effect
    def evaluate(script, arg=None):
        value = normal(script, arg)
        if 'const exactLeaf' in script and arg == '帳單查詢及繳款':
            return '帳單查詢及繳款 synthetic'
        if script == ScsbCrawler._card_statement_state_script():
            return run_offline_js(script, DOM_TRACE_HARNESS, {'kind': 'statement', 'failure': failure})['result']
        return value
    page.evaluate.side_effect = evaluate
    with pytest.raises(RuntimeError, match='SCSB credit-card inquiry failed') as caught:
        method(object.__new__(ScsbCrawler), page)
    scsb_assert_actual_sink(caught.value, expected)


@pytest.mark.parametrize('mode,expected', [('poll', False), ('empty', True)])
def test_scsb_residual_readiness_false_and_authoritative_empty_keep_boolean_trace(mode, expected):
    old = run_offline_js(frozen_method(ScsbCrawler, '_twd_inquiry_result_ready_script')(), SCSB_RESULT_HARNESS, mode)
    new = run_offline_js(ScsbCrawler._twd_inquiry_result_ready_script(), SCSB_RESULT_HARNESS, mode)
    assert old == new
    assert new['result'] is expected


@pytest.mark.parametrize('start,expected', [(None, 'SCSB-05-347-003'), (date(2026, 1, 3), 'SCSB-05-347-004')])
def test_scsb_residual_existing_period_failure_ids_reach_actual_sink(start, expected):
    from tests.test_scsb_accounts_and_twd_inquiry import _mock_twd_query_page
    script = ScsbCrawler._twd_inquiry_period_script(full_history=False, start_date=start, end_date=date(2026, 1, 2))
    period = run_offline_js(script, SCSB_PERIOD_HARNESS, 'success')['result']
    page = _mock_twd_query_page()
    normal = page.evaluate.side_effect
    def evaluate(script, *args):
        value = next(normal)
        return period if 'configuredEndRaw' in script else value
    page.evaluate.side_effect = evaluate
    with pytest.raises(RuntimeError, match='SCSB TWD inquiry failed') as caught:
        object.__new__(ScsbCrawler)._collect_twd_inquiry(page, {'90000000123456'})
    scsb_assert_actual_sink(caught.value, expected)


@pytest.mark.parametrize('failure', ['leaf', 'scope', 'success'])
def test_scsb_residual_scope_script_sentinel_and_success_keep_exact_dom_trace(failure):
    mode = {'kind': 'scope', 'failure': failure}
    old = run_offline_js(frozen_method(ScsbCrawler, '_card_leaf_scope_text_script')(), SCSB_CARD_HARNESS, mode)
    new = run_offline_js(ScsbCrawler._card_leaf_scope_text_script(), SCSB_CARD_HARNESS, mode)
    assert old['trace'] == new['trace']
    if failure == 'success':
        assert old['result'] == new['result']
    else:
        assert old['result'] is None
        assert new['result'] == {'_diagnostic': 'scope-unavailable'}


def test_scsb_residual_result_state_success_keeps_exact_dom_trace():
    old = run_offline_js(frozen_method(ScsbCrawler, '_twd_inquiry_result_state_script')(), SCSB_RESULT_HARNESS, 'valid')
    new = run_offline_js(ScsbCrawler._twd_inquiry_result_state_script(), SCSB_RESULT_HARNESS, 'valid')
    assert old == new
    assert new['result'] == {'error': False, 'pagination': False}


SCSB_PERIOD_HARNESS = r'''
const fs=require('fs'),vm=require('vm');
const {script,mode}=JSON.parse(fs.readFileSync(0,'utf8'));
const trace=[];
const node=(name,props)=>new Proxy({offsetParent:{},id:name,name:'',placeholder:'',labels:[],dataset:{},
 getAttribute:key=>{trace.push(name+'.attr:'+key);return '';},...props},
 {get(o,k){if(typeof k==='string' && k!=='dataset')trace.push(name+'.'+k);return o[k];}});
const custom=node('custom',{type:'radio',parentElement:{innerText:'Custom'}});
const start=node(mode==='same'?'start end':'start',{type:'date',min:mode==='bad-min'?'bad':mode==='system-limit'?'2026-01-01':'',value:''});
const end=node('end',{type:'date',value:'2026-01-02'});
let inputs=mode==='same'?[custom,start]:[custom,start,end];
inputs=inputs.filter(el=>el!==({custom,start,end}[mode]));
trace.length=0;
const document={querySelectorAll:s=>{trace.push('query:'+s);return inputs;}};
const result=vm.runInNewContext('('+script+')',{document,Date})();
console.log(JSON.stringify({trace,result}));
'''


@pytest.mark.parametrize('failure,expected', [
    ('custom', 'SCSB-05-616-001'), ('start', 'SCSB-05-616-002'),
    ('end', 'SCSB-05-616-003'), ('same', 'SCSB-05-616-004'),
])
def test_scsb_residual_period_actual_js_marker_reaches_outer_sink(failure, expected):
    from tests.test_scsb_accounts_and_twd_inquiry import _mock_twd_query_page
    before = frozen_method(ScsbCrawler, '_twd_inquiry_period_script')(end_date=date(2026, 1, 2))
    after = ScsbCrawler._twd_inquiry_period_script(end_date=date(2026, 1, 2))
    old = run_offline_js(before, SCSB_PERIOD_HARNESS, failure)
    new = run_offline_js(after, SCSB_PERIOD_HARNESS, failure)
    assert new['trace'] == old['trace']
    assert new['result'].get('_diagnostic') == expected
    assert {k: v for k, v in new['result'].items() if k != '_diagnostic'} == old['result']
    page = _mock_twd_query_page()
    normal = page.evaluate.side_effect
    def evaluate(script, *args, **kwargs):
        value = next(normal)
        return new['result'] if 'configuredEndRaw' in script else value
    page.evaluate.side_effect = evaluate
    with pytest.raises(RuntimeError, match='SCSB TWD inquiry failed') as caught:
        object.__new__(ScsbCrawler)._collect_twd_inquiry(page, {'90000000123456'})
    assert exception_error_id(caught.value, bank='scsb') == expected
    assert caught.value.__suppress_context__
    from backend.core.error_codes import format_error_id
    assert expected in format_error_id(expected, bank='scsb')
    assert code_details(expected, bank='scsb')
    scsb_assert_actual_sink(caught.value, expected)
