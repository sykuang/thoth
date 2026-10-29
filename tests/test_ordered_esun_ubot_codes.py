"""Offline action/reason controls: real production methods, synthetic failures only."""
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.banks.esun import EsunCrawler, EsunLoginError
from backend.banks.ubot import UbotCrawler, UbotLoginError
from backend.core.error_codes import exception_error_id


def diagnostic(action, bank, exception=RuntimeError):
    with pytest.raises(exception) as caught:
        action()
    code = exception_error_id(caught.value, bank=bank)
    assert code is not None
    return code


def collect_sink(action, bank, exception=RuntimeError):
    from backend.core.base import _collect_failure_diagnostics
    crawler = object.__new__(EsunCrawler if bank == 'esun' else UbotCrawler)
    crawler.name = bank
    with pytest.raises(exception) as caught:
        action()
    return _collect_failure_diagnostics(crawler, caught.value)['error_id']


def test_ubot_select_validation_does_not_truncate_original_iterator():
    from tests.test_ubot_history_sync import _form_snapshot
    class Selects(list):
        def __iter__(self):
            yield from super().__iter__()
            yield None
    value = _form_snapshot()
    value['selects'] = Selects(value['selects'])
    assert collect_sink(lambda: UbotCrawler._validate_twd_form(value, as_of=date(2026, 9, 1)), 'ubot') == 'UBOT-04-451-001'


def test_ubot_select_roles_and_option_operands_reach_collect_sink():
    import copy
    good = {'selects': [{'enabled': True, 'options': []}, {'enabled': True, 'options': []}], 'search_buttons': 1}
    codes = []
    for role in range(2):
        for fault in ('shape', 'keys', 'enabled', 'options', 'option_shape', 'option_keys', 'text', 'value'):
            value = copy.deepcopy(good)
            select = value['selects'][role]
            if fault == 'shape': value['selects'][role] = None
            elif fault == 'keys': select['extra'] = None
            elif fault == 'enabled': select['enabled'] = False
            elif fault == 'options': select['options'] = None
            else:
                option = {'text': 'synthetic', 'value': 'synthetic'}
                if fault == 'option_shape': option = None
                elif fault == 'option_keys': option['extra'] = None
                else: option[fault] = None
                select['options'] = [option]
            codes.append(collect_sink(lambda: UbotCrawler._validate_twd_form(value, as_of=date(2026, 9, 1)), 'ubot'))
    assert len(set(codes)) == len(codes), codes


@pytest.mark.parametrize('bank,cls,error', [('esun', EsunCrawler, EsunLoginError), ('ubot', UbotCrawler, UbotLoginError)])
def test_login_field_and_native_suboperation_are_distinct(bank, cls, error):
    codes = []
    for field_index in range(3):
        for operation in ('focus', 'select', 'clear', 'type', 'settle', 'read'):
            crawler = object.__new__(cls)
            crawler.creds = SimpleNamespace(national_id='A', user_code='B', password='C')
            crawler._credential_origin_allowed = lambda page: True
            crawler._frame_origin_allowed = lambda page, frame: True
            page = Mock()
            page.keyboard = SimpleNamespace(press=Mock(), type=Mock())
            fields = [Mock() for _ in range(3)]
            current = [-1]
            calls = []
            original = TimeoutError('synthetic native failure')

            def perform(op, value=None):
                calls.append((current[0], op))
                if current[0] == field_index and op == operation:
                    raise original
                return value

            def locate(selector):
                current[0] += 1
                return fields[current[0]]

            for field in fields:
                field.count.return_value = 1
                field.nth.return_value = field
                field.is_visible.return_value = field.is_enabled.return_value = True
                field.click.side_effect = lambda **kw: perform('select' if kw.get('click_count') else 'focus')
                field.input_value.side_effect = lambda: perform('read', 'A')
            page.locator.side_effect = locate
            page.keyboard.press.side_effect = lambda *a, **k: perform('clear')
            page.keyboard.type.side_effect = lambda *a, **k: perform('type')
            page.wait_for_timeout.side_effect = lambda *a: perform('settle')
            if bank == 'esun':
                crawler._find_login_frame = lambda page, **kwargs: page
                crawler._main_login_form = lambda frame, **kwargs: None
            codes.append(diagnostic(lambda: crawler.submit_credentials_once(page), bank, error))
            assert calls.count((field_index, operation)) == 1
            assert exception_error_id(original, bank=bank) == codes[-1]
    assert len(set(codes)) == len(codes), codes


def frozen_function(module, name):
    """Compile only the requested immutable baseline body; never import its module."""
    import ast
    from pathlib import Path
    import subprocess
    root = Path(__file__).parents[1]
    path = Path(module.__file__).relative_to(root)
    source = subprocess.check_output(['git', 'show', '16f2d4b:' + str(path)], cwd=root, text=True)
    node = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef) and n.name == name)
    node.decorator_list = []
    namespace = dict(vars(module))
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<frozen-baseline>', 'exec'), namespace)
    return namespace[name]


def test_ubot_candidate_old_new_trace_and_recovery_slot():
    from backend.banks import ubot
    baseline = frozen_function(ubot, '_unique_visible_enabled_exact')
    for fault in (None, 'locator', 'count', 'nth', 'is_visible', 'is_enabled', 'inner_text', 'hidden', 'disabled', 'nonmatch', 'ambiguous'):
        traces = []
        for helper in (baseline, ubot._unique_visible_enabled_exact):
            trace = []
            class Candidate:
                def perform(self, op, result):
                    trace.append(op)
                    if fault == op: raise TimeoutError('synthetic trace')
                    return result
                def is_visible(self): return self.perform('is_visible', fault != 'hidden')
                def is_enabled(self): return self.perform('is_enabled', fault != 'disabled')
                def inner_text(self): return self.perform('inner_text', 'other' if fault == 'nonmatch' else '登入')
                def nth(self, index): return self.perform('nth', self)
                def count(self): return self.perform('count', 2 if fault == 'ambiguous' else 1)
                def locator(self, selector): return self.perform('locator', self)
            candidate = Candidate()
            result = helper(candidate, 'button', '登入')
            traces.append((trace, result is candidate))
        assert traces[0] == traces[1], (fault, traces)
    slot = []
    page = Mock()
    page.locator.side_effect = TimeoutError('synthetic recovered')
    assert ubot._unique_visible_enabled_exact(page, 'button', '登入', failure=slot) is None
    assert len(slot) == 1
    page.locator.side_effect = None
    candidate = page.locator.return_value
    candidate.count.return_value = 1
    candidate.nth.return_value = candidate
    candidate.is_visible.return_value = candidate.is_enabled.return_value = True
    candidate.inner_text.return_value = '登入'
    assert ubot._unique_visible_enabled_exact(page, 'button', '登入', failure=slot) is candidate
    assert slot == []
    candidate.count.return_value = 0
    assert ubot._unique_visible_enabled_exact(page, 'button', '登入', failure=slot) is None
    assert slot == ['UBOT-06-450-001']


def test_esun_legacy_exhausted_required_detection_keeps_last_field_rejection():
    from tests.test_esun_main_review_regressions import StartupPage, _crawler
    codes = []
    for role in range(3):
        page = StartupPage(legacy=True)
        seen = []
        def locate(selector):
            index = len(seen) % (role + 1)
            seen.append(selector)
            return SimpleNamespace(count=lambda: 0 if index == role else 1)
        page.frames[0].locator = locate
        codes.append(collect_sink(lambda: _crawler()._make_collector(page), 'esun', EsunLoginError))
        assert len(seen) == 41 * (role + 1)
        assert page.waited == 10000
    assert len(set(codes)) == len(codes), codes


def test_esun_legacy_detection_generator_keeps_field_role_and_lazy_trace():
    from tests.test_esun_main_review_regressions import StartupPage, _crawler
    codes = []
    for role in range(3):
        for fault in ('locator', 'count'):
            page = StartupPage(legacy=True)
            trace = []
            original = TimeoutError('synthetic detector')
            def locate(selector):
                index = len(trace)
                trace.append(selector)
                if index == role and fault == 'locator':
                    raise original
                def count():
                    if index == role:
                        raise original
                    return 1
                return SimpleNamespace(count=count)
            page.frames[0].locator = locate
            codes.append(collect_sink(lambda: _crawler()._make_collector(page), 'esun', TimeoutError))
            assert len(trace) == role + 1
            assert exception_error_id(original, bank='esun') == codes[-1]
    assert len(set(codes)) == len(codes), codes


def test_esun_main_contract_js_terminal_reasons_preserve_frozen_read_trace():
    import json
    import subprocess
    from backend.banks import esun
    baseline = frozen_function(esun, '_main_login_form')
    old_script = next(c for c in baseline.__code__.co_consts if isinstance(c, str) and 'querySelectorAll' in c)
    harness = r'''
const [source,role,fault] = process.argv.slice(1);
const trace=[];
const form={querySelectorAll(selector){
  const name=selector.match(/name="([^"]+)"/)[1]; trace.push('query:'+name);
  const values={form, type:name==='id'?'text':'password', maxLength:name==='id'?10:15};
  const node=new Proxy(values,{get(target,key){trace.push(name+':'+key);return name===role&&key===fault?null:target[key]}});
  return new Proxy(name===role&&fault==='length'?[]:[node],{get(target,key){trace.push(name+':nodes:'+key);return target[key]}});
}};
console.log(JSON.stringify({result:eval('('+source+')')(form),trace}));
'''
    codes = []
    for role in ('id', 'userName', 'pxssword'):
        for fault in ('length', 'form', 'type', 'maxLength'):
            old = json.loads(subprocess.check_output(['node', '-e', harness, old_script, role, fault], text=True))
            observed = []
            form, field, frame, page = Mock(), Mock(), Mock(), Mock()
            frame.url, frame.name, frame.parent_frame = 'https://ebank.esunbank.com.tw/login', '', None
            page.frames = [frame]
            for target in (form, field):
                target.count.return_value = 1
                target.nth.return_value = target
                target.is_visible.return_value = target.is_enabled.return_value = True
            frame.locator.side_effect = lambda selector: form if selector == esun._MAIN_LOGIN_FORM else field
            def evaluate(source):
                current = json.loads(subprocess.check_output(['node', '-e', harness, source, role, fault], text=True))
                assert old['trace'] == current['trace']
                assert old['result'] is False
                observed.append(current['result'])
                return current['result']
            form.evaluate.side_effect = evaluate
            crawler = object.__new__(EsunCrawler)
            crawler._frame_origin_allowed = lambda *a: True
            codes.append(collect_sink(lambda: crawler.submit_credentials_once(page), 'esun', EsunLoginError))
            assert len(observed) == 1
    assert len(set(codes)) == len(codes), codes
    current_script = next(c for c in EsunCrawler._main_login_form.__code__.co_consts if isinstance(c, str) and 'querySelectorAll' in c)
    success = [json.loads(subprocess.check_output(['node', '-e', harness, source, 'none', 'none'], text=True))
               for source in (old_script, current_script)]
    assert success[0] == success[1] and success[0]['result'] is True


def test_esun_main_form_and_button_required_rejections_are_distinct():
    from backend.banks import esun
    codes = []
    for fault in ('forms_missing', 'forms_ambiguous', 'button_missing', 'button_ambiguous', 'button_hidden', 'button_disabled', 'button_owner'):
        form, field, button, frame, page = [Mock() for _ in range(5)]
        frame.url, frame.name, frame.parent_frame = 'https://ebank.esunbank.com.tw/login', '', None
        page.frames = [frame]
        for target in (form, field, button):
            target.count.return_value = 1
            target.nth.return_value = target
            target.is_visible.return_value = target.is_enabled.return_value = target.evaluate.return_value = True
        frame.locator.side_effect = lambda selector: form if selector == esun._MAIN_LOGIN_FORM else field
        form.locator.return_value = button
        button.filter.return_value = button
        if fault.startswith('forms_'): form.count.return_value = 0 if fault == 'forms_missing' else 2
        elif fault in ('button_missing', 'button_ambiguous'): button.count.return_value = 0 if fault == 'button_missing' else 2
        elif fault == 'button_hidden': button.is_visible.return_value = False
        elif fault == 'button_disabled': button.is_enabled.return_value = False
        else: button.evaluate.return_value = False
        crawler = object.__new__(EsunCrawler)
        crawler._frame_origin_allowed = lambda *a: True
        codes.append(collect_sink(lambda: crawler.submit_credentials_once(page), 'esun', EsunLoginError))
        if fault == 'button_hidden': button.is_enabled.assert_not_called()
    assert len(set(codes)) == len(codes), codes


def test_esun_login_frame_absence_and_ambiguity_have_distinct_terminal_ids():
    codes = []
    for number in (0, 2):
        crawler = object.__new__(EsunCrawler)
        crawler._frame_origin_allowed = lambda *a: True
        page = SimpleNamespace(frames=[SimpleNamespace(url='https://ebank.esunbank.com.tw/login', name='iframe1') for _ in range(number)])
        codes.append(collect_sink(lambda: crawler.submit_credentials_once(page), 'esun', EsunLoginError))
    assert len(set(codes)) == 2, codes


def test_esun_main_field_locator_roles_reach_collect_sink():
    from backend.banks import esun
    codes = []
    for role in range(3):
        frame, page, form, field = [Mock() for _ in range(4)]
        frame.url, frame.name, frame.parent_frame = 'https://ebank.esunbank.com.tw/login', '', None
        page.frames = [frame]
        for target in (form, field):
            target.count.return_value = 1
            target.nth.return_value = target
            target.is_visible.return_value = target.is_enabled.return_value = True
        original = TimeoutError('synthetic locator')
        calls = []
        def locate(selector):
            calls.append(selector)
            if selector == esun._MAIN_LOGIN_FIELDS[role]: raise original
            return form if selector == esun._MAIN_LOGIN_FORM else field
        frame.locator.side_effect = locate
        crawler = object.__new__(EsunCrawler)
        crawler._frame_origin_allowed = lambda *a: True
        codes.append(collect_sink(lambda: crawler.submit_credentials_once(page), 'esun', EsunLoginError))
        assert len(calls) == role + 2
        assert exception_error_id(original, bank='esun') == codes[-1]
    assert len(set(codes)) == len(codes), codes


def test_esun_main_form_terminal_rejections_keep_field_roles_at_collect_sink():
    codes = []
    for role in range(3):
        for fault in ('count', 'is_visible', 'is_enabled'):
            crawler = object.__new__(EsunCrawler)
            crawler._frame_origin_allowed = lambda *a: True
            page = Mock()
            frame = Mock(url='https://ebank.esunbank.com.tw/login', name='', parent_frame=None)
            page.frames = [frame]
            form = Mock()
            form.count.return_value = 1
            form.nth.return_value = form
            fields = [Mock() for _ in range(3)]
            for field in fields:
                field.count.return_value = 1
                field.nth.return_value = field
                field.is_visible.return_value = field.is_enabled.return_value = True
            getattr(fields[role], fault).return_value = 0 if fault == 'count' else False
            frame.locator.side_effect = [form, *fields]
            codes.append(collect_sink(lambda: crawler.submit_credentials_once(page), 'esun', EsunLoginError))
            for index in range(role + 1):
                fields[index].count.assert_called_once()
            for index in range(role + 1, 3):
                fields[index].count.assert_not_called()
    assert len(set(codes)) == len(codes), codes


def test_ubot_prepare_candidate_roles_do_not_collide_or_poison_submit():
    codes = []
    for role in ('opener', 'tab'):
        for operation in ('locator', 'count', 'nth', 'is_visible', 'is_enabled', 'inner_text'):
            crawler = object.__new__(UbotCrawler)
            crawler._logged_in = lambda page: False
            page = Mock()
            candidate = Mock()
            candidate.count.return_value = 1
            candidate.nth.return_value = candidate
            candidate.is_visible.return_value = candidate.is_enabled.return_value = True
            candidate.inner_text.return_value = '網銀登入' if role == 'opener' else '個人用戶登入'
            original = TimeoutError('synthetic candidate')
            getattr(candidate, operation).side_effect = original
            absent = Mock()
            absent.count.return_value = 0
            selector = 'button, a' if role == 'opener' else 'a, button, [role=tab]'
            page.locator.side_effect = lambda value: (candidate.locator() if operation == 'locator' else candidate) if value == selector else absent
            crawler.prepare_login_page(page)
            codes.append(exception_error_id(original, bank='ubot'))
            assert codes[-1] is not None
            getattr(candidate, operation).assert_called_once()
            assert collect_sink(lambda: UbotCrawler._validate_twd_form(None, as_of=date(2026, 9, 1)), 'ubot') == 'UBOT-04-400-001'
    assert len(set(codes)) == len(codes), codes


def test_ubot_search_and_refresh_candidate_roles(monkeypatch):
    from backend.banks import ubot
    from tests.test_ubot_history_sync import _form_snapshot
    monkeypatch.setattr(ubot, 'wait_captcha_stable', lambda *a, **k: None)
    monkeypatch.setattr(ubot, 'solve_captcha', lambda *a, **k: '')
    codes = []
    for role in ('search', 'refresh'):
        for fault in ('locator', 'count', 'nth', 'is_visible', 'is_enabled', 'inner_text'):
            page, candidate = Mock(), Mock()
            candidate.count.return_value = 1
            candidate.nth.return_value = candidate
            candidate.is_visible.return_value = candidate.is_enabled.return_value = True
            candidate.inner_text.return_value = '搜尋' if role == 'search' else '重新產生'
            original = TimeoutError('synthetic candidate')
            getattr(candidate, fault).side_effect = original
            page.locator.side_effect = lambda *a: candidate.locator() if fault == 'locator' else candidate
            page.query_selector_all.return_value = [Mock(), Mock()]
            crawler = object.__new__(UbotCrawler)
            crawler._goto = lambda *a, **k: None
            crawler._twd_form_snapshot = lambda page: _form_snapshot()
            crawler._history_range = lambda *a, **k: (date(2026, 9, 1), date(2026, 9, 1))
            crawler.captcha_tmp = None
            collector = SimpleNamespace(by_endpoint=lambda endpoint: [], issued_count=lambda endpoint: 0)
            if role == 'search':
                code = collect_sink(lambda: crawler._collect_twd_history(page, collector, as_of=date(2026, 9, 1)), 'ubot')
                assert exception_error_id(original, bank='ubot') == code
            else:
                assert crawler._ocr_with_regen(page, max_attempts=2) is None
                code = exception_error_id(original, bank='ubot')
            assert code is not None
            codes.append(code)
            getattr(candidate, fault).assert_called_once()
    assert len(set(codes)) == len(codes), codes


def test_ubot_submit_required_candidate_rejections_are_not_native_read_failures():
    from tests.test_ubot_login_checkpoints import _submit_fixture
    codes = []
    for fault in ('malformed_text', 'missing', 'ambiguous'):
        crawler, page, candidates, button, _ = _submit_fixture()
        if fault == 'malformed_text': button.inner_text.return_value = None
        else: candidates.count.return_value = 0 if fault == 'missing' else 2
        codes.append(collect_sink(lambda: crawler.submit_credentials_once(page), 'ubot', UbotLoginError))
        button.click.assert_not_called()
    assert codes == ['UBOT-02-450-007', 'UBOT-02-450-008', 'UBOT-02-450-009']


def test_ubot_submit_candidate_native_failure_survives_required_guard():
    codes = []
    for operation in ('locator', 'count', 'nth', 'is_visible', 'is_enabled', 'inner_text'):
        crawler = object.__new__(UbotCrawler)
        crawler.creds = SimpleNamespace(national_id='A', user_code='B', password='C')
        crawler._ocr_with_regen = lambda *a, **k: '123456'
        page = Mock()
        field = Mock()
        field.count.return_value = 1
        field.nth.return_value = field
        field.is_visible.return_value = field.is_enabled.return_value = True
        field.input_value.side_effect = ['A', 'B', 'C', '123456']
        button = Mock()
        button.count.return_value = 1
        button.nth.return_value = button
        button.is_visible.return_value = button.is_enabled.return_value = True
        button.inner_text.return_value = '登入'
        original = TimeoutError('synthetic candidate')
        getattr(button, operation).side_effect = original
        page.locator.side_effect = lambda selector: (getattr(button, 'locator')() if operation == 'locator' else button) if selector == 'button' else field
        codes.append(collect_sink(lambda: crawler.submit_credentials_once(page), 'ubot', UbotLoginError))
        assert exception_error_id(original, bank='ubot') == codes[-1]
        getattr(button, operation).assert_called_once()
    assert len(set(codes)) == len(codes), codes


def test_spa_money_roles_and_operands_reach_collect_sink():
    from backend.banks.esun_spa.rows import normalize_row
    good = {'txDate': '2026/07/01', 'txTime': '12:00:00', 'detailTitle': 'synthetic', 'debitCredit': 'DB', 'amount': 1, 'balance': 2}
    codes = []
    for role in ('amount', 'balance'):
        for invalid in (None, -1):
            row = {**good, role: invalid}
            codes.append(collect_sink(lambda: normalize_row(row, account_no='0000000000001', currency='TWD',
                start=date(2026, 7, 1), end=date(2026, 7, 31)), 'esun', ValueError))
    assert len(set(codes)) == len(codes), codes


def test_esun_inventory_independent_rejections():
    good = {'index': 1, 'text': '臺幣 0000000000001', 'value': 'synthetic'}
    cases = [None, [], [None]]
    for key, value in [('index', '1'), ('index', -1), ('text', None), ('text', ' 臺幣 0000000000001'), ('text', '外幣 0000000000001'), ('text', '臺幣'), ('value', None), ('value', ''), ('value', ' x')]:
        cases.append([{**good, key: value}])
    cases.append([good, good])
    codes = [diagnostic(lambda v=v: EsunCrawler._validated_twd_options(v), 'esun') for v in cases]
    assert len(set(codes)) == len(codes), codes


def test_ubot_transport_independent_rejections():
    from tests.test_ubot_history_sync import _history_hit, ACCOUNT
    cases = [('url', 'http://www.ubot.com.tw/MyBank/IBKB010102'), ('raw_url', 'http://www.ubot.com.tw/MyBank/IBKB010102'),
             ('method', 'GET'), ('status', 503), ('redirected', True), ('main_frame_request', False),
             ('request_sequence', '2'), ('request_sequence', 0), ('body_size', '8'), ('body_size', 0),
             ('content_type', 'text/html'), ('req_body', [])]
    codes = []
    for key, value in cases:
        hit = _history_hit()
        setattr(hit, key, value)
        codes.append(diagnostic(lambda: UbotCrawler._validate_history_hit(hit, identity=ACCOUNT,
                                start=date(2026, 7, 1), end=date(2026, 7, 31), after_sequence=1), 'ubot'))
    assert len(set(codes)) == len(codes), codes


def test_spa_normalization_reasons_are_distinct():
    from backend.banks.esun_spa.rows import normalize_row
    good = {'txDate': '2026/07/01', 'txTime': '12:00:00', 'detailTitle': 'synthetic', 'debitCredit': 'DB', 'amount': 1, 'balance': 2}
    cases = [(None, {}), (good, {'currency': 'USD'}), (good, {'start': 'bad'}),
             ({**good, 'txDate': None}, {}), ({**good, 'txDate': 'bad'}, {}),
             ({**good, 'txTime': None}, {}), ({**good, 'txTime': 'bad'}, {}),
             ({**good, 'detailTitle': None}, {}), ({**good, 'detailTitle': ''}, {}),
             ({**good, 'debitCredit': '?'}, {})]
    codes = []
    for row, changes in cases:
        kwargs = dict(account_no='0000000000001', currency='TWD', start=date(2026, 7, 1), end=date(2026, 7, 31))
        kwargs.update(changes)
        codes.append(diagnostic(lambda: normalize_row(row, **kwargs), 'esun', ValueError))
    assert len(set(codes)) == len(codes), codes


def test_spa_menu_and_query_admission_distinguish_reasons():
    from backend.banks.esun_spa.navigation import build_menu_plan
    from backend.banks.esun_spa.query import query_twd
    menu_codes = [diagnostic(lambda v=v, l=l: build_menu_plan(v, l), 'esun', ValueError)
                  for v, l in [(None, 'zh-TW'), ([], 'bad'), ([], 'zh-TW')]]
    assert len(set(menu_codes)) == len(menu_codes)
    query_codes = [diagnostic(lambda g=g, r=r: query_twd(None, None, None, 'zh-TW', 'synthetic', g, r), 'esun', ValueError)
                   for g, r in [(lambda: False, lambda: 'synthetic'), (lambda: True, lambda: 'changed'), (lambda: True, lambda: 'synthetic')]]
    assert len(set(query_codes)) == len(query_codes)


@pytest.mark.parametrize('operation', ['today', 'viewport'])
def test_spa_native_query_reads_tag_the_same_exception_once(operation):
    from backend.banks.esun_spa.query import query_twd
    page = Mock()
    error = TimeoutError('synthetic read')
    page.evaluate.side_effect = [error] if operation == 'today' else [[2026, 9, 1], error]
    with pytest.raises(TimeoutError) as caught:
        query_twd(page, date(2026, 8, 1), date(2026, 8, 31), 'zh-TW', 'synthetic', lambda: True, lambda: 'synthetic')
    assert caught.value is error
    code = exception_error_id(error, bank='esun')
    assert code is not None
    assert page.evaluate.call_count == (1 if operation == 'today' else 2)
    assert code == ('ESUN-05-700-001' if operation == 'today' else 'ESUN-05-701-001')


def test_production_guard_inventory_is_explicit():
    import ast
    from pathlib import Path
    base = Path(__file__).parents[1]
    paths = [base / 'backend/banks/esun.py', base / 'backend/banks/ubot.py', *sorted((base / 'backend/banks/esun_spa').glob('*.py'))]
    for path in paths:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise) and node.exc is not None:
                assert isinstance(node.exc, ast.Call) and getattr(node.exc.func, 'id', '') == 'with_error_code', (path.name, node.lineno)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'require':
                assert any(k.arg == 'error_id' for k in node.keywords), (path.name, node.lineno)


@pytest.mark.parametrize('bank,cls', [('esun', EsunCrawler), ('ubot', UbotCrawler)])
def test_prepare_native_failure_is_tagged_without_retry(bank, cls):
    crawler = object.__new__(cls)
    page = Mock()
    error = TimeoutError('synthetic wait')
    page.wait_for_timeout.side_effect = error
    # UBOT intentionally swallows preparation failures; the original observation
    # still gets a code, but must never become a later failure's context.
    if bank == 'esun':
        with pytest.raises(TimeoutError) as caught:
            crawler.prepare_login_page(page)
        assert caught.value is error
    else:
        crawler.prepare_login_page(page)
    assert exception_error_id(error, bank=bank) is not None
    page.wait_for_timeout.assert_called_once()
    assert exception_error_id(RuntimeError('later independent'), bank=bank) is None


def test_native_statement_inventory_has_local_failure_boundary():
    import ast
    from pathlib import Path
    base = Path(__file__).parents[1]
    native = {'evaluate', 'evaluate_all', 'evaluate_handle', 'click', 'wait_for_timeout', 'wait_for_selector',
              'wait_for', 'input_value', 'count', 'nth', 'is_visible', 'is_enabled', 'inner_text', 'get_attribute',
              'all_text_contents', 'locator', 'on', 'remove_listener', 'route', 'unroute', 'dispose',
              'disconnect', 'close', 'start', 'read', 'body', 'expect_popup', 'press', 'type', 'goto',
              'select_option', 'fill', 'check', 'text_content', 'screenshot', 'filter', 'is_checked', 'opener',
              'continue_', 'abort', 'move', 'wheel', 'wait_for_function', 'query_selector_all', 'detach'}
    missing = []
    for path in [base / 'backend/banks/esun.py', base / 'backend/banks/ubot.py', *sorted((base / 'backend/banks/esun_spa').glob('*.py'))]:
        tree = ast.parse(path.read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in native):
                continue
            current = node
            while current in parents and not isinstance(current, ast.FunctionDef):
                current = parents[current]
                if isinstance(current, ast.With) and any(isinstance(i.context_expr, ast.Call) and getattr(i.context_expr.func, 'id', '') == 'error_step' for i in current.items):
                    break
                if isinstance(current, ast.Call) and isinstance(current.func, ast.Call) and getattr(current.func.func, 'id', '') == 'error_step':
                    break
            else:
                missing.append((path.name, node.lineno, node.func.attr))
    assert not missing, missing


def test_spa_current_success_rejection_leaves_do_not_collapse():
    from backend.banks.esun_spa import capture as m
    from tests.test_esun_spa_capture import Page, issue
    codes = []
    for fault in ('collector', 'attachment', 'main_frame', 'sequence', 'method', 'status', 'body_type', 'result_code'):
        page, collector = Page(), m.SpaCollector()
        collector.attach(page)
        baseline = collector.snapshot()
        issue(m, collector, page)
        hit = collector.hits[0]
        if fault in ('collector', 'attachment', 'main_frame'):
            baseline[fault] = object()
        elif fault == 'sequence': baseline['sequence'] = hit.request_sequence
        elif fault == 'method': hit.method = 'GET'
        elif fault == 'status': hit.status = 503
        elif fault == 'body_type': hit.resp_json = []
        elif fault == 'result_code': hit.resp_json['resultCode'] = 'synthetic rejected'
        codes.append(diagnostic(lambda: m.require_current_success(collector, baseline, m.PATHS[0]), 'esun', ValueError))
        collector.detach(page)
    assert len(set(codes)) == len(codes), codes


def test_spa_native_cleanup_matches_frozen_precedence_and_call_trace():
    from contextlib import contextmanager
    from backend.banks.esun_spa import products
    baseline = frozen_function(products, '_open_bill')
    traces = []
    for function in (baseline, products._open_bill):
        trace = []
        primary = TimeoutError('synthetic popup')
        def operation(name):
            trace.append(name)
            if name == 'click': raise primary
            if name in ('remove', 'unroute'): raise RuntimeError('synthetic cleanup')
        page = Mock()
        page.context = SimpleNamespace(route=lambda *a: operation('route'), on=lambda *a: operation('on'),
            remove_listener=lambda *a: operation('remove'), unroute=lambda *a: operation('unroute'))
        button = page.locator.return_value
        button.count.return_value = 1
        button.is_visible.return_value = button.is_enabled.return_value = True
        button.click.side_effect = lambda **k: operation('click')
        @contextmanager
        def popup(**kwargs):
            yield SimpleNamespace(value=None)
        page.expect_popup.side_effect = popup
        with pytest.raises(RuntimeError) as caught:
            function(page, lambda: None)
        assert str(caught.value) == 'E.SUN native bill cleanup incomplete'
        assert caught.value.__context__ is primary
        traces.append(trace)
    assert traces == [['route', 'on', 'click', 'remove', 'unroute', 'remove', 'unroute']] * 2


def test_spa_exhausted_cleanup_keeps_callback_id_and_baseline_retry_trace():
    from backend.banks.esun_spa.products import _cleanup_all
    from backend.core.error_codes import error_step
    codes = []
    for failed in range(2):
        trace = []
        def action(index):
            trace.append(index)
            if index == failed:
                raise TimeoutError('synthetic cleanup')
        callbacks = [error_step(code)(lambda i=i: action(i)) for i, code in enumerate(('ESUN-06-425-001', 'ESUN-06-424-001'))]
        codes.append(collect_sink(lambda: _cleanup_all(callbacks), 'esun'))
        assert trace == [0, 1, failed]
    assert codes == ['ESUN-06-425-001', 'ESUN-06-424-001']


def test_spa_bill_cleanup_keeps_baseline_masking_and_primary_context(monkeypatch):
    from contextlib import contextmanager
    from backend.banks.esun_spa import products
    page = Mock()
    page.context = SimpleNamespace(route=Mock(), on=Mock(), remove_listener=Mock(), unroute=Mock())
    button = page.locator.return_value
    button.count.return_value = 1
    button.is_visible.return_value = button.is_enabled.return_value = True
    original = TimeoutError('synthetic popup click')
    button.click.side_effect = original

    @contextmanager
    def popup(**kwargs):
        yield SimpleNamespace(value=None)
    page.expect_popup.side_effect = popup
    cleanup_error = RuntimeError('synthetic cleanup')
    cleanup = Mock(side_effect=cleanup_error)
    monkeypatch.setattr(products, '_cleanup_all', cleanup)
    with pytest.raises(RuntimeError) as caught:
        products._open_bill(page, lambda: None)
    assert caught.value is cleanup_error
    assert caught.value.__context__ is original
    assert exception_error_id(caught.value, bank='esun') == exception_error_id(original, bank='esun')
    assert exception_error_id(original, bank='esun') is not None
    button.click.assert_called_once()
    cleanup.assert_called_once()


class QueryLocator:
    """Minimal native calendar surface; no scripts, browser, or transport."""
    def __init__(self, page, selector=''):
        self.page, self.selector = page, selector
    def locator(self, selector):
        return QueryLocator(self.page, self.selector + ' ' + selector)
    def filter(self, has):
        self.selector += ' ' + has.selector
        return self
    def count(self):
        s = self.selector
        if s == 'dialog:visible,[role="dialog"]:visible': return int(self.page.opened)
        if 'input[name=keyword]' in s or 'input[name=textquery]' in s or '.selected-tag' in s or 'checkbox' in s: return 0
        if 'dialog:visible' in s or 'dialog.calendar' in s: return int(self.page.opened)
        return 1
    def is_visible(self): return True
    def is_enabled(self): return True
    def is_checked(self): return True
    def evaluate(self, script, *args) -> object: return True
    def input_value(self):
        return 'synthetic' if 'accountList' in self.selector else '2026/09/01'
    def inner_text(self):
        return '2026年09月' if 'header-label' in self.selector else '1'
    def get_attribute(self, name): return self.inner_text()
    def click(self, **kwargs):
        s = self.selector
        field = 'startDate' if 'startDate' in s else 'endDate'
        operation = 'open' if 'combo-block > .combo-input-wrapper' in s else 'day'
        self.page.calls.append((field, operation))
        if (field, operation) == self.page.fault: raise self.page.error
        self.page.opened = operation == 'open'


class QueryPage:
    def __init__(self, fault):
        self.fault, self.opened, self.calls = fault, False, []
        self.error = TimeoutError('synthetic calendar action')
    def evaluate(self, expression):
        return [2026, 9, 1] if 'new Date' in expression else 1200
    def locator(self, selector): return QueryLocator(self, selector)
    def wait_for_timeout(self, ms): raise AssertionError('unexpected wait')


def test_spa_account_switch_caller_roles_reach_collect_sink(monkeypatch):
    import sys
    from backend.banks.esun_spa import collection as m, query

    codes = []
    inventory = [{'accountNo': '0000000000001', 'accountAlias': 'one'}, {'accountNo': '0000000000002', 'accountAlias': 'two'}]
    body = {'demandDeptAcc': inventory[0]['accountNo'], 'accountAlias': 'one', 'twAccountList': inventory,
            'twCurrInfo': {'curr': 'TWD'}, 'queryDeptTxDtlResult': {'detailListData': []}}
    plan = {'locale': 'zh-TW'}
    monkeypatch.setattr(m, 'navigation_guard', lambda *a: lambda: True)
    monkeypatch.setattr(m, 'build_menu_plan', lambda *a: plan)
    monkeypatch.setattr(m, 'navigate_twd', lambda *a: None)
    monkeypatch.setattr(query, 'query_twd', lambda *a, **k: None)
    monkeypatch.setattr(m, 'empty_window_state', lambda *a: 'not_empty')
    monkeypatch.setattr(m, 'validate_query_response', lambda *a: None)
    monkeypatch.setattr(m, 'require_rendered_occurrences', lambda *a: None)
    monkeypatch.setattr(m, 'continue_twd_once', lambda *a, **k: ([], 'cursor_unverified'))
    envelope = {'requestBody': {'account': inventory[0]['accountNo'], 'startDate': 'start', 'endDate': 'end',
                              'startIndex': 1, 'count': 100, 'customerInputHashtag': []}}
    def success(collector, baseline, path):
        if path == m.PATHS[0]: return {'menuList': []}, {'requestBody': {'locale': 'zh-TW'}}
        if path == m.PATHS[1]: return body, {'requestBody': {'account': None}}
        return {'queryDeptTxDtlResult': {'detailListData': [], 'startIndex': 1}}, envelope
    monkeypatch.setattr(m, 'require_current_success', success)
    for role in ('opener', 'item'):
        for fault in ('count', 'visible', 'enabled', 'safe', 'click', 'reject_count', 'reject_visible', 'reject_enabled', 'reject_safe'):
            trace, native, observations = [], [], []
            original = TimeoutError('synthetic switch')
            class Page:
                elapsed = 0
                timeline_reads = 0
                opened = mounted = False

                def locator(self, selector): return Target(selector)

                def evaluate(self, script, *args):
                    if script == m.DESTINATION_REASON:
                        assert not args
                        observations.append('destination')
                        return [True, 0]
                    if script == '() => {const d=new Date();return [d.getFullYear(),d.getMonth()+1,d.getDate()]}':
                        assert not args
                        observations.append('today')
                        return [2026, 9, 1]
                    if script == 'ds=>ds.map(d=>new Date(d[0],d[1]-1,d[2]).toISOString())':
                        assert args == ([[2026, 9, 1], [2026, 9, 1]],)
                        observations.append('dates')
                        return ['start', 'end']
                    assert script == m.TIMELINE_REASON and not args
                    self.timeline_reads += 1
                    observations.append('timeline')
                    # Real poll contract: missing observation recovers only after
                    # one event-pumping wait; never compare a generic True to True.
                    return (['before', 0] if self.timeline_reads == 1 else
                            [None, 1] if self.timeline_reads == 2 else ['after', 0])

                def wait_for_timeout(self, milliseconds):
                    assert milliseconds == 20
                    assert self.elapsed < (40 if role == 'item' else 20)
                    assert self.timeline_reads == (3 if self.opened else 2)
                    observations.append('wait')
                    self.elapsed += milliseconds
                    self.mounted = self.opened

            page = Page()
            monkeypatch.setattr(m, 'monotonic', lambda: page.elapsed / 1000)

            class Target:
                def __init__(self, selector): self.selector = selector
                def operation(self, name, value):
                    # Inject only at the production shared click callee. Owner
                    # reads remain real and must precede every native action.
                    caller = sys._getframe(2)
                    assert caller.f_code.co_filename == m.__file__
                    here = caller.f_code.co_name == 'click'
                    target_role = 'item' if 'li[role=' in self.selector else 'opener'
                    native.append((target_role, name, caller.f_code.co_name))
                    if here:
                        trace.append((target_role, name))
                        if (target_role, name) == (role, fault): raise original
                        if target_role == role and fault == 'reject_' + name: return False
                    return value
                def count(self):
                    value = int(page.mounted) if 'dialog.combo-modal' in self.selector and 'li[role=' not in self.selector else 1
                    return self.operation('count', value)
                def is_visible(self): return self.operation('visible', True)
                def is_enabled(self): return self.operation('enabled', True)
                def evaluate(self, script):
                    assert script == query.SAFE
                    return self.operation('safe', True)
                def click(self, **kw):
                    assert kw == {'timeout': 2500}
                    result = self.operation('click', None)
                    assert not page.opened
                    page.opened = True
                    return result
                def input_value(self): return '0000000000001 one'
                def locator(self, selector):
                    assert selector == 'input.combo-input[name="accountList"][readonly]' or selector == ':scope > .custom-area > ul.info-scrollable > li[role="button"]'
                    return Target(self.selector + ' ' + selector)
                def nth(self, index):
                    assert index == 1 and page.mounted
                    return self
                def all_text_contents(self):
                    assert page.mounted
                    return ['0000000000001 one', '0000000000002 two']
                def evaluate_all(self, script):
                    assert script == 'nodes=>nodes.every(n=>(' + query.SAFE + ')(n))' and page.mounted
                    return True
            crawler = object.__new__(EsunCrawler)
            crawler._logged_in = lambda page: True
            crawler.transaction_start_for = lambda *a, **k: None
            crawler.transaction_window_start = lambda *a, **k: date(2026, 9, 1)
            collector = SimpleNamespace(page=page, MAX_QUERY_REQUESTS=100, query_issued_count=lambda: 0,
                issued_count=lambda path: 0, snapshot=lambda: {'counts': dict.fromkeys(m.PATHS, 0)})
            codes.append(collect_sink(lambda: m._collect_rows(crawler, page, collector, {}, [], []), 'esun', ValueError if fault.startswith('reject_') else TimeoutError))
            if not fault.startswith('reject_'):
                assert exception_error_id(original, bank='esun') == codes[-1]
            terminal = fault.removeprefix('reject_')
            operations = ['count', 'visible', 'enabled', 'safe', 'click']
            prefix = operations[:operations.index(terminal) + 1]
            assert trace == ([('opener', op) for op in operations] if role == 'item' else []) + [(role, op) for op in prefix]
            assert trace.count((role, terminal)) == 1
            owner = [('opener', op, 'owner') for op in ('count', 'visible', 'safe')]
            expected_native = [('opener', 'count', '<lambda>')]
            expected_native += [('opener', op, 'query_revalidate') for _ in range(6) for op in ('count', 'visible')]
            expected_native += owner
            if role == 'item':
                expected_native += [('opener', op, 'click') for op in operations[:-1]] + owner + [('opener', 'click', 'click')]
                expected_native += [('opener', 'count', 'switch_account'), *owner]
                expected_native += [('opener', 'count', 'switch_account'), *owner]
                expected_native += [('opener', 'count', 'switch_account'), ('opener', 'visible', 'switch_account')]
            expected_native += [(role, op, 'click') for op in (prefix[:-1] if terminal == 'click' else prefix)]
            if terminal == 'click':
                expected_native += [*owner, (role, 'click', 'click')]
            assert native == expected_native, (role, fault, native)
            assert observations == [
                'destination', 'destination', 'today', 'destination', 'timeline',
                'destination', 'dates', 'destination', 'timeline', 'wait',
                'destination', 'timeline', 'destination', 'destination', 'destination',
            ] + (['destination', 'destination', 'wait', 'destination'] if role == 'item' else []) + (['destination'] if terminal == 'click' else [])
            assert page.timeline_reads == 3
            assert page.elapsed == (40 if role == 'item' else 20)
    assert codes == [
        'ESUN-05-840-001', 'ESUN-05-840-002', 'ESUN-05-840-003',
        'ESUN-05-840-004', 'ESUN-05-840-005', 'ESUN-05-840-006',
        'ESUN-05-840-007', 'ESUN-05-840-008', 'ESUN-05-840-009',
        'ESUN-05-841-001', 'ESUN-05-841-002', 'ESUN-05-841-003',
        'ESUN-05-841-004', 'ESUN-05-841-005', 'ESUN-05-841-006',
        'ESUN-05-841-007', 'ESUN-05-841-008', 'ESUN-05-841-009',
    ]
    assert len(set(codes)) == len(codes), codes


def test_spa_keyword_and_textquery_roles_and_operands_reach_collect_sink(monkeypatch):
    import itertools
    from backend.banks.esun_spa import query
    codes = []
    native_locator, native_count, native_value = QueryLocator.locator, QueryLocator.count, QueryLocator.input_value
    for role in ('keyword', 'textquery'):
        for fault in ('locator', 'count_first', 'count_second', 'read', 'cardinality', 'nonempty'):
            clock = itertools.count()
            monkeypatch.setattr(query.time, 'monotonic', lambda: next(clock) * 10)
            seen = []
            original = TimeoutError('synthetic filter')
            def locate(target, selector):
                if selector == 'input[name=' + role + ']' and fault == 'locator':
                    seen.append('locator')
                    raise original
                return native_locator(target, selector)
            def count(target):
                if 'input[name=' + role + ']' in target.selector:
                    seen.append('count')
                    if fault == ('count_first' if seen.count('count') == 1 else 'count_second'):
                        raise original
                    return 2 if fault == 'cardinality' else 1
                return native_count(target)
            def value(target):
                if 'input[name=' + role + ']' in target.selector:
                    seen.append('read')
                    if fault == 'read': raise original
                    return 'synthetic rejected' if fault == 'nonempty' else ''
                return native_value(target)
            monkeypatch.setattr(QueryLocator, 'locator', locate)
            monkeypatch.setattr(QueryLocator, 'count', count)
            monkeypatch.setattr(QueryLocator, 'input_value', value)
            error = ValueError if fault in ('cardinality', 'nonempty') else TimeoutError
            page = QueryPage(None)
            codes.append(collect_sink(lambda: query.query_twd(page, date(2026, 9, 1), date(2026, 9, 1),
                'zh-TW', 'synthetic', lambda: True, lambda: 'synthetic'), 'esun', error))
            assert seen == (['locator'] if fault == 'locator' else ['count'] if fault in ('count_first', 'cardinality') else ['count', 'count'] if fault == 'count_second' else ['count', 'count', 'read'])
    assert len(set(codes)) == len(codes), codes


def test_spa_final_readback_generator_keeps_roles_and_lazy_failure(monkeypatch):
    from backend.banks.esun_spa.query import query_twd
    operations = ('count', 'is_visible', 'is_enabled', 'evaluate', 'input_value')
    native = {name: getattr(QueryLocator, name) for name in ('locator', *operations)}
    codes = []
    for role in ('startDate', 'endDate'):
        for fault in ('locator', *operations, 'reject_count', 'reject_is_visible', 'reject_is_enabled', 'reject_evaluate', 'wrong_value'):
            seen, trace = {}, []
            original = TimeoutError('synthetic final readback')
            def locate(target, selector):
                field = next((name for name in ('startDate', 'endDate') if selector == 'input[name=' + name + '][readonly]'), None)
                if field:
                    seen[field] = seen.get(field, 0) + 1
                    if seen[field] == 2:
                        trace.append((field, 'locator'))
                        if field == role and fault == 'locator': raise original
                found = native['locator'](target, selector)
                found.final_probe = field if field and seen[field] == 2 else None
                return found
            monkeypatch.setattr(QueryLocator, 'locator', locate)
            for operation in operations:
                def perform(target, *args, op=operation):
                    field = getattr(target, 'final_probe', None)
                    if field:
                        trace.append((field, op))
                        if field == role:
                            if fault == op: raise original
                            if fault == 'reject_' + op: return False
                            if fault == 'wrong_value' and op == 'input_value': return 'synthetic wrong date'
                    return native[op](target, *args)
                monkeypatch.setattr(QueryLocator, operation, perform)
            page = QueryPage(None)
            error = ValueError if fault.startswith('reject_') or fault == 'wrong_value' else TimeoutError
            codes.append(collect_sink(lambda: query_twd(page, date(2026, 9, 1), date(2026, 9, 1), 'zh-TW',
                'synthetic', lambda: True, lambda: 'synthetic'), 'esun', error))
            if role == 'startDate': assert not any(field == 'endDate' for field, _ in trace)
            if error is TimeoutError: assert exception_error_id(original, bank='esun') == codes[-1]
    assert len(set(codes)) == len(codes), codes


def test_spa_date_mount_roles_and_primitive_reads_are_distinct(monkeypatch):
    from backend.banks.esun_spa.query import query_twd
    original_locator, original_count = QueryLocator.locator, QueryLocator.count
    codes = []
    for role in ('startDate', 'endDate'):
        for fault in ('locator', 'cardinality', 'presence', 'all', 'reject_cardinality'):
            counts = []
            original = TimeoutError('synthetic mounted field')
            selector = 'input.combo-input[name=' + role + '][readonly][tabindex="-1"]'
            def locate(target, value):
                if value == selector and fault == 'locator': raise original
                return original_locator(target, value)
            def count(target):
                if target.selector.endswith(selector):
                    counts.append(role)
                    if fault == 'reject_cardinality': return 2
                    if len(counts) == {'cardinality': 1, 'presence': 2, 'all': 4}.get(fault): raise original
                return original_count(target)
            monkeypatch.setattr(QueryLocator, 'locator', locate)
            monkeypatch.setattr(QueryLocator, 'count', count)
            page = QueryPage(None)
            codes.append(collect_sink(lambda: query_twd(page, date(2026, 9, 1), date(2026, 9, 1), 'zh-TW',
                'synthetic', lambda: True, lambda: 'synthetic'), 'esun', ValueError if fault == 'reject_cardinality' else TimeoutError))
            if fault != 'reject_cardinality': assert exception_error_id(original, bank='esun') == codes[-1]
            assert len(counts) == {'locator': 0, 'cardinality': 1, 'presence': 2, 'all': 4, 'reject_cardinality': 1}[fault]
    assert len(set(codes)) == len(codes), codes


def test_spa_calendar_fields_and_subactions_have_distinct_native_ids():
    from backend.banks.esun_spa.query import query_twd
    codes = []
    for field in ('startDate', 'endDate'):
        for operation in ('open', 'day'):
            page = QueryPage((field, operation))
            with pytest.raises(TimeoutError) as caught:
                query_twd(page, date(2026, 9, 1), date(2026, 9, 1), 'zh-TW', 'synthetic', lambda: True, lambda: 'synthetic')
            assert caught.value is page.error
            assert page.calls.count((field, operation)) == 1
            codes.append(exception_error_id(page.error, bank='esun'))
    assert None not in codes
    assert len(set(codes)) == len(codes), codes


def test_ubot_history_parser_fields_keep_distinct_reason_ids():
    from tests.test_ubot_history_sync import _history_hit, ACCOUNT
    codes = []
    for key in ('TraDate', 'AccountDate', 'Expenditure', 'Income', 'Balance'):
        hit = _history_hit()
        hit.resp_json['RespBody']['NTDetailList'][0][key] = 'invalid synthetic field'
        codes.append(diagnostic(lambda: UbotCrawler._validate_history_hit(hit, identity=ACCOUNT,
                                start=date(2026, 7, 1), end=date(2026, 7, 31), after_sequence=1), 'ubot'))
    assert len(set(codes)) == len(codes), codes


def test_ubot_navigation_routes_and_suboperations_are_distinct():
    crawler = object.__new__(UbotCrawler)
    codes = []
    for route in ('/A0101001', '/B0101001', '/F0101001', '/F0201001', '/F0301001', '/F0801001'):
        for operation in ('evaluate', 'wait_for_timeout'):
            page = Mock()
            original = TimeoutError('synthetic navigation')
            getattr(page, operation).side_effect = original
            with pytest.raises(TimeoutError) as caught:
                crawler._goto(page, route)
            assert caught.value is original
            getattr(page, operation).assert_called_once()
            codes.append(exception_error_id(original, bank='ubot'))
    assert None not in codes
    assert len(set(codes)) == len(codes), codes


def test_spa_radio_state_read_preserves_native_failure():
    from backend.banks.esun_spa.query import query_twd
    page = QueryPage(None)
    original = TimeoutError('synthetic checked read')
    from unittest.mock import patch as mock_patch
    with (
        mock_patch.object(QueryLocator, 'is_checked', side_effect=original) as read,
        pytest.raises(TimeoutError) as caught,
    ):
        query_twd(page, date(2026, 9, 1), date(2026, 9, 1), 'zh-TW', 'synthetic', lambda: True, lambda: 'synthetic')
    assert caught.value is original
    assert exception_error_id(original, bank='esun') is not None
    read.assert_called_once()


def test_esun_main_form_field_roles_and_native_suboperations_are_distinct():
    from backend.banks.esun import _MAIN_LOGIN_FIELDS
    codes = []
    for index in range(3):
        for operation in ('count', 'nth', 'is_visible', 'is_enabled'):
            frame = Mock()
            frame.parent_frame = None
            form = Mock()
            forms = Mock()
            forms.count.return_value = 1
            forms.nth.return_value = form
            fields = [Mock() for _ in range(3)]
            for field in fields:
                field.count.return_value = 1
                field.nth.return_value = field
                field.is_visible.return_value = field.is_enabled.return_value = True
            original = TimeoutError('synthetic main field')
            getattr(fields[index], operation).side_effect = original
            frame.locator.side_effect = lambda selector: fields[_MAIN_LOGIN_FIELDS.index(selector)] if selector in _MAIN_LOGIN_FIELDS else forms
            with pytest.raises(TimeoutError) as caught:
                EsunCrawler._main_login_form(frame)
            assert caught.value is original
            getattr(fields[index], operation).assert_called_once()
            codes.append(exception_error_id(original, bank='esun'))
    assert None not in codes
    assert len(set(codes)) == len(codes), codes


def test_native_boundaries_do_not_collapse_multiple_calls():
    import ast
    from collections import Counter
    from pathlib import Path
    base = Path(__file__).parents[1]
    native = {'evaluate', 'evaluate_all', 'evaluate_handle', 'click', 'wait_for_timeout', 'wait_for_selector',
              'wait_for', 'input_value', 'count', 'nth', 'is_visible', 'is_enabled', 'inner_text', 'get_attribute',
              'all_text_contents', 'locator', 'on', 'remove_listener', 'route', 'unroute', 'dispose',
              'disconnect', 'close', 'start', 'read', 'body', 'expect_popup', 'press', 'type', 'goto',
              'select_option', 'filter', 'is_checked', 'opener', 'continue_', 'abort', 'move', 'wheel',
              'wait_for_function', 'query_selector_all', 'detach'}
    collapsed = []
    for path in [base / 'backend/banks/esun.py', base / 'backend/banks/ubot.py', *sorted((base / 'backend/banks/esun_spa').glob('*.py'))]:
        tree = ast.parse(path.read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        counts = Counter()
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in native): continue
            current = node
            while current in parents:
                current = parents[current]
                if isinstance(current, ast.With) and any(isinstance(i.context_expr, ast.Call) and getattr(i.context_expr.func, 'id', '') == 'error_step' for i in current.items): break
                if isinstance(current, ast.Call) and isinstance(current.func, ast.Call) and getattr(current.func.func, 'id', '') == 'error_step': break
            counts[current] += 1
        collapsed.extend((path.name, boundary.lineno, count) for boundary, count in counts.items() if count > 1)
    assert not collapsed, collapsed


def test_spa_date_readback_roles_are_distinct(monkeypatch):
    from backend.banks.esun_spa.query import query_twd
    original_read = QueryLocator.input_value
    codes = []
    for field in ('startDate', 'endDate'):
        page = QueryPage(None)
        def read(target):
            if field in target.selector:
                raise page.error
            return original_read(target)
        monkeypatch.setattr(QueryLocator, 'input_value', read)
        with pytest.raises(TimeoutError) as caught:
            query_twd(page, date(2026, 9, 1), date(2026, 9, 1), 'zh-TW', 'synthetic', lambda: True, lambda: 'synthetic')
        assert caught.value is page.error
        codes.append(exception_error_id(page.error, bank='esun'))
    assert None not in codes and len(set(codes)) == 2, codes


def test_spa_billed_and_unposted_popup_clicks_are_distinct(monkeypatch):
    from contextlib import contextmanager
    from backend.banks.esun_spa import products
    codes = []
    for selector, urls in ((products.BILL_BUTTON, products.BILL_URLS), (products.UNPOSTED_BUTTON, products.UNPOSTED_URLS)):
        page = Mock()
        page.context = SimpleNamespace(route=Mock(), on=Mock())
        button = page.locator.return_value
        button.count.return_value = 1
        button.is_visible.return_value = button.is_enabled.return_value = True
        original = TimeoutError('synthetic product click')
        button.click.side_effect = original
        @contextmanager
        def popup(**kwargs): yield SimpleNamespace(value=None)
        page.expect_popup.side_effect = popup
        monkeypatch.setattr(products, '_cleanup_all', Mock())
        with pytest.raises(TimeoutError) as caught:
            products._open_bill(page, lambda: None, selector, urls)
        assert caught.value is original
        button.click.assert_called_once()
        codes.append(exception_error_id(original, bank='esun'))
    assert None not in codes and len(set(codes)) == 2, codes


def test_spa_observer_endpoint_roles_are_distinct_and_nonfatal():
    from backend.banks.esun_spa import capture as m
    from tests.test_esun_spa_capture import Page, issue
    codes = []
    for path in m.PATHS:
        page, collector = Page(), m.SpaCollector()
        collector.attach(page)
        original = TimeoutError('synthetic observer read')
        collector.observers[path].read = Mock(side_effect=original)
        issue(m, collector, page, path)
        collector.observers[path].read.assert_called_once()
        assert collector.rejected == 1 and not collector.hits
        codes.append(exception_error_id(original, bank='esun'))
        collector.detach(page)
    assert None not in codes and len(set(codes)) == len(codes), codes


def test_spa_query_helper_component_roles_and_operations_are_distinct(monkeypatch):
    from backend.banks.esun_spa.query import query_twd, ROOT, FORM
    codes = []
    for role in (ROOT, ROOT + ' ' + FORM):
        for operation in ('count', 'is_visible', 'is_enabled', 'evaluate'):
            page = QueryPage(None)
            original_method = getattr(QueryLocator, operation)
            def fail(target, *args, **kwargs):
                if target.selector == role: raise page.error
                return original_method(target, *args, **kwargs)
            with monkeypatch.context() as patcher:
                patcher.setattr(QueryLocator, operation, fail)
                with pytest.raises(TimeoutError) as caught:
                    query_twd(page, date(2026, 9, 1), date(2026, 9, 1), 'zh-TW', 'synthetic', lambda: True, lambda: 'synthetic')
            assert caught.value is page.error
            codes.append(exception_error_id(page.error, bank='esun'))
    assert None not in codes and len(set(codes)) == len(codes), codes


@pytest.mark.parametrize('target', ['CTW01002-TW', 'CCM01002'])
def test_spa_menu_header_and_leaf_native_clicks_are_distinct(target):
    from backend.banks.esun_spa.navigation import navigate_twd, HEADER, MENU
    plan = dict(taskId=target, locale='zh-TW', labels=['synthetic', 'synthetic leaf'], positions=[0, 0], counts=[1, 1], openLink='打開連結')
    codes = []
    for fault in ('header', 'leaf'):
        page = SimpleNamespace(opened=False, error=TimeoutError('synthetic menu click'), calls=[])
        class Locator:
            def __init__(self, selector): self.selector, self.filtered = selector, False
            def filter(self, **kwargs):
                result = Locator(self.selector)
                result.filtered = True
                return result
            def locator(self, selector): return Locator(self.selector + ' ' + selector)
            def nth(self, index): return self
            def count(self): return int(page.opened) if self.selector == MENU and self.filtered else 1
            def is_visible(self): return True
            def is_enabled(self): return True
            def evaluate(self, *args): return True
            def wait_for(self, **kwargs): return None
            def click(self, **kwargs):
                role = 'header' if self.selector == HEADER else 'leaf'
                page.calls.append(role)
                if role == fault: raise page.error
                page.opened = True
        page.locator = Locator
        page.evaluate = lambda *args: 1200
        with pytest.raises(TimeoutError) as caught:
            navigate_twd(page, plan, lambda: True, lambda: plan, target=target)
        assert caught.value is page.error and page.calls.count(fault) == 1
        codes.append(exception_error_id(page.error, bank='esun'))
    assert None not in codes and len(set(codes)) == 2, codes
    assert codes == (['ESUN-04-700-001', 'ESUN-04-710-001'] if target == 'CTW01002-TW' else ['ESUN-06-700-001', 'ESUN-06-710-001'])
