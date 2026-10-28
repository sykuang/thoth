"""Finite terminal diagnostics; synthetic methods and Node only, no browser/network."""
from datetime import date
from types import SimpleNamespace
import itertools

import pytest

from backend.banks.esun import EsunCrawler
from backend.banks.esun_spa import query
from backend.core.base import _failure_diagnostics, _format_failure_diagnostics
from backend.core.error_codes import with_error_code
from tests.test_ordered_esun_ubot_codes import QueryPage, QueryLocator


def sink(action, expected, kind=ValueError, message='native query rejected'):
    with pytest.raises(kind) as caught:
        action()
    assert str(caught.value) == message
    crawler = object.__new__(EsunCrawler)
    crawler.name = 'esun'
    diagnostic = _failure_diagnostics(crawler, caught.value, 'sync_failed')
    assert diagnostic['error_id'] == expected
    text = _format_failure_diagnostics(diagnostic)
    assert '[' + expected + ']' in text
    assert any('\u4e00' <= c <= '\u9fff' for c in text)
    return caught.value


def run_query(page, guard=lambda: True, revalidate=lambda: 'synthetic', **kwargs):
    return query.query_twd(page, date(2026, 9, 1), date(2026, 9, 1), 'zh-TW',
                           'synthetic', guard, revalidate, **kwargs)


@pytest.mark.parametrize('role,index', [('initial', 0), ('poll', 1), ('before_click', 2),
                                        ('after_click', 3), ('final', 6), ('submit', 7), ('post_submit', 8)])
@pytest.mark.parametrize('callback,offset', [('guard', 0), ('revalidate', 2)])
@pytest.mark.parametrize('reject', [False, True])
def test_checkpoint_callback_role_to_parent_sink(monkeypatch, role, index, callback, offset, reject):
    # Callback order on the unchanged single-day native path.
    positions = {'initial': 0, 'poll': 1, 'before_click': 2, 'after_click': 3,
                 'final': 18, 'submit': 19, 'post_submit': 20}
    seen = []
    original = TimeoutError('synthetic callback')
    def observe(which):
        if which == callback:
            here = len(seen)
            seen.append(which)
            if here == positions[role]:
                if reject: return False if which == 'guard' else 'not the selected account'
                raise original
        return True if which == 'guard' else 'synthetic'
    expected = CHECKPOINT_CODES[role][offset + int(reject)]
    error = sink(lambda: run_query(QueryPage(None), lambda: observe('guard'), lambda: observe('revalidate')),
                 expected, ValueError if reject else TimeoutError, 'native query rejected' if reject else 'synthetic callback')
    if not reject: assert error is original


@pytest.mark.parametrize('field', ['startDate', 'endDate'])
@pytest.mark.parametrize('fault,number', [('mounted', 1), ('popup_count', 3), ('popup_visible', 4),
                                        ('days', 5), ('day', 6), ('closed', 7), ('readback', 8)])
def test_readiness_original_false_operand_reaches_only_deadline(monkeypatch, field, fault, number):
    import sys
    clock = itertools.count()
    monkeypatch.setattr(query.time, 'monotonic', lambda: next(clock) * 10)
    native_count, native_visible, native_value = QueryLocator.count, QueryLocator.is_visible, QueryLocator.input_value
    def pending(target):
        names = []
        frame = sys._getframe(1)
        while frame:
            names.append(frame.f_code.co_name)
            frame = frame.f_back
        return 'ready' in names and 'owner_state' not in names and field in target.selector
    def count(target):
        s = target.selector
        if pending(target):
            if fault == 'mounted' and 'input.combo-input[name=' + field in s: return 0
            if fault == 'popup_count' and 'dialog.calendar' in s: return 0
            if fault == 'days' and s.endswith('.days-block'): return 0
            if fault == 'day' and 'span.day-cell' in s: return 0
            if fault == 'closed' and s.endswith('dialog:visible'): return 1
        return native_count(target)
    def visible(target):
        if pending(target) and fault == 'popup_visible' and 'dialog.calendar' in target.selector: return False
        return native_visible(target)
    def value(target):
        if pending(target) and fault == 'readback': return 'not the selected day'
        return native_value(target)
    monkeypatch.setattr(QueryLocator, 'count', count)
    monkeypatch.setattr(QueryLocator, 'is_visible', visible)
    monkeypatch.setattr(QueryLocator, 'input_value', value)
    expected = READINESS[field][number]
    sink(lambda: run_query(QueryPage(None)), expected)


@pytest.mark.parametrize('field,expected', [('startDate', 'ESUN-05-864-001'), ('endDate', 'ESUN-05-864-002')])
def test_continuation_owner_original_date_mismatch_is_terminal(monkeypatch, field, expected):
    from backend.banks.esun_spa import collection as m
    calls = []
    window = (date(2026, 9, 1), date(2026, 9, 1))
    def locate(selector):
        name = 'startDate' if 'startDate' in selector else 'endDate'
        calls.append(name)
        return SimpleNamespace(input_value=lambda: 'wrong' if name == field else '2026/09/01')
    page = SimpleNamespace(locator=locate)
    collector = SimpleNamespace(MAX_QUERY_REQUESTS=100, issued_count=lambda _: 0, query_issued_count=lambda: 0)
    body = {'detailListData': [], 'startIndex': 1, 'count': 100}
    response = {'queryDeptTxDtlResult': body}
    envelope = {'requestBody': {}}
    sink(lambda: m.continue_twd_once(SimpleNamespace(), page, collector,
          {'counts': {p: (0 if p != m.PATHS[2] else -1) for p in m.PATHS}}, response, envelope,
          lambda: calls.append('revalidate'), window, [0]), expected, message='continuation_rejected')
    assert calls == ['revalidate', 'startDate'] + (['endDate'] if field == 'endDate' else [])


def collection_surface(monkeypatch, *, destination_role=None, marker=1, timeline=False, empty_date=None, recover=False, unavailable=False):
    import sys
    from backend.banks.esun_spa import collection as m
    calls = []
    body = {'demandDeptAcc': '0000000000001', 'accountAlias': 'one',
            'twAccountList': [{'accountNo': '0000000000001', 'accountAlias': 'one'}],
            'twCurrInfo': {'curr': 'TWD'}, 'queryDeptTxDtlResult': {'detailListData': []}}
    envelope = {'requestBody': {'account': '0000000000001', 'startDate': 'start', 'endDate': 'end',
                              'startIndex': 1, 'count': 100, 'customerInputHashtag': []}}
    monkeypatch.setattr(m, 'navigation_guard', lambda *a: lambda: True)
    monkeypatch.setattr(m, 'build_menu_plan', lambda *a: {'locale': 'zh-TW'})
    monkeypatch.setattr(m, 'navigate_twd', lambda *a: calls.append('navigate'))
    monkeypatch.setattr(query, 'query_twd', lambda *a, **k: calls.append('query'))
    monkeypatch.setattr(m, 'empty_window_state', lambda *a: 'explicit_empty_candidate' if empty_date else 'not_empty')
    monkeypatch.setattr(m, 'retain_capture_records', lambda *a, **k: (None, [], lambda: None))
    monkeypatch.setattr(m, 'validate_query_response', lambda *a: None)
    monkeypatch.setattr(m, 'require_rendered_occurrences', lambda *a: None)
    monkeypatch.setattr(m, 'continue_twd_once', lambda *a, **k: ([], 'cursor_unverified'))
    def success(collector, baseline, path):
        calls.append(('response', path))
        if path == m.PATHS[0]: return {'menuList': []}, {'requestBody': {'locale': 'zh-TW'}}
        if path == m.PATHS[1]:
            if unavailable and calls.count(('response', path)) == 1: raise ValueError('synthetic pending response')
            return body, {'requestBody': {'account': None}}
        return {'queryDeptTxDtlResult': {'detailListData': [], 'startIndex': 1}}, envelope
    monkeypatch.setattr(m, 'require_current_success', success)
    clock = itertools.count()
    monkeypatch.setattr(m, 'monotonic', lambda: next(clock) / 10)
    class Field:
        def __init__(self, selector): self.selector = selector
        def count(self): return 1
        def is_visible(self): return True
        def input_value(self):
            if 'input[name=startDate]' not in self.selector and 'input[name=endDate]' not in self.selector: return '0000000000001 one'
            field = 'startDate' if 'startDate' in self.selector else 'endDate'
            names, frame = [], sys._getframe(1)
            while frame:
                names.append(frame.f_code.co_name)
                frame = frame.f_back
            role = 'proof' if 'prove_empty' in names else 'empty'
            calls.append(('date', role, field))
            return 'wrong' if empty_date == (role, field) else '2026/09/01'
    state = {'destination': 0, 'timeline': 0}
    def evaluate(script, *args):
        frame = sys._getframe(1)
        caller = frame.f_code.co_name
        if script == m.DESTINATION or script == getattr(m, 'DESTINATION_REASON', None):
            role = 'query' if caller == 'query_revalidate' else 'prequery' if crawler._esun_spa_phase == 'prequery_response' else 'initial'
            state['destination'] += 1
            calls.append(('destination', role))
            return [False, marker] if destination_role == role and (not recover or state['destination'] == 1) else True
        if script == m.TIMELINE or script == getattr(m, 'TIMELINE_REASON', None):
            state['timeline'] += 1
            calls.append('timeline')
            if timeline and state['timeline'] > 1: return [None, marker]
            return 'old' if state['timeline'] == 1 else 'new'
        if script == m.EMPTY_CARD: return '(2003)' + m.EMPTY_MESSAGE
        if 'new Date();' in script: return [2026, 9, 1]
        if args: return ['start', 'end']
        raise AssertionError('unexpected synthetic evaluation')
    page = SimpleNamespace(locator=Field, evaluate=evaluate,
                           wait_for_function=lambda *a, **k: calls.append('frame'),
                           wait_for_timeout=lambda ms: calls.append(('wait', ms)))
    crawler = object.__new__(EsunCrawler)
    crawler._logged_in = lambda _: True
    crawler.transaction_start_for = lambda *a, **k: None
    crawler.transaction_window_start = lambda *a, **k: date(2026, 9, 1)
    collector = SimpleNamespace(page=page, MAX_QUERY_REQUESTS=100, query_issued_count=lambda: 0,
        issued_count=lambda path: int(path == m.PATHS[2] and 'query' in calls), snapshot=lambda: {'counts': dict.fromkeys(m.PATHS, 0)})
    return lambda: m._collect_rows(crawler, page, collector, {}, [], []), calls


@pytest.mark.parametrize('role', ['prequery', 'initial', 'query'])
@pytest.mark.parametrize('marker', range(1, 18))
def test_destination_negative_only_consumed_at_existing_terminal(monkeypatch, role, marker):
    from backend.banks.esun_spa.collection import DESTINATION_CODES
    action, calls = collection_surface(monkeypatch, destination_role=role, marker=marker)
    sink(action, DESTINATION_CODES[role][marker - 1], message='SPA probe rejected')
    assert 'query' not in calls


@pytest.mark.parametrize('marker,code', [(1, 'ESUN-05-866-001'), (2, 'ESUN-05-866-002'), (3, 'ESUN-05-866-003')])
def test_timeline_negative_only_consumed_at_render_deadline(monkeypatch, marker, code):
    action, calls = collection_surface(monkeypatch, marker=marker, timeline=True)
    sink(action, code, message='SPA probe rejected')
    assert calls.count('query') == 1


@pytest.mark.parametrize('role,field,expected', [
    ('empty', 'startDate', 'ESUN-05-864-005'), ('empty', 'endDate', 'ESUN-05-864-006'),
    ('proof', 'startDate', 'ESUN-05-864-009'), ('proof', 'endDate', 'ESUN-05-864-010')])
def test_empty_and_publication_owner_date_roles(monkeypatch, role, field, expected):
    action, calls = collection_surface(monkeypatch, empty_date=(role, field))
    sink(action, expected, message='SPA probe rejected')
    dates = [entry for entry in calls if type(entry) is tuple and entry[0] == 'date' and entry[1] == role]
    assert dates == [('date', role, 'startDate')] + ([('date', role, 'endDate')] if field == 'endDate' else [])


@pytest.mark.parametrize('recover,unavailable', [(False, False), (True, False), (False, True), (True, True)])
def test_actual_collection_old_new_native_order_and_recovery(monkeypatch, recover, unavailable):
    from backend.banks.esun_spa import collection as m
    traces = []
    for old in (True, False):
        with monkeypatch.context() as patcher:
            action, calls = collection_surface(patcher, destination_role='prequery' if recover else None,
                                               recover=recover, unavailable=unavailable)
            if old:
                baseline, _ = frozen_method(m, '_collect_rows')
                patcher.setattr(m, '_collect_rows', baseline)
            result = action()
            traces.append((result, calls))
    assert traces[0] == traces[1]
    if unavailable:
        calls = traces[1][1]
        at = calls.index(('response', m.PATHS[1]))
        assert calls[at + 1] == 'frame'  # No DESTINATION evaluation when body is None.


def legacy_surface(monkeypatch, fault, *, context=False):
    from backend.banks import esun
    from backend.core import store
    class NoDisk:
        def __truediv__(self, _): return self
        def mkdir(self, **kw): pass
    monkeypatch.setattr(store, '_data_root', lambda: NoDisk())
    monkeypatch.setattr(esun, '_log', lambda *a: None)
    calls = []
    class Frame:
        url = 'https://ebank.esunbank.com.tw/fco/FAO01002.faces'
        def locator(self, selector):
            calls.append(('locator', selector))
            return SimpleNamespace(select_option=lambda **kw: calls.append(('select', kw)))
        def evaluate(self, script, arg=None):
            if '#sysInfo' in script: return '2026/09/01'
            if 's.options].map' in script: return []
            if 's?.options[s.selectedIndex]' in script:
                return [None, fault[1]] if fault[0] == 'selected' else {'index': 0, 'value': 'synthetic', 'text': 'synthetic'}
            if 'no intervalrdo4' in script:
                return [{'ok': False, 'error': 'no intervalrdo4'}, fault[1]] if fault[0] == 'period' else {'ok': True, 'checked': True, **arg}
            if 'fao01002:j_id_sort1' in script:
                return [False, fault[1]] if fault[0] == 'sort' else True
            if 'data-hermes-stale-evidence' in script and 'expected' not in script: return {'ok': True, 'marked': 0}
            if "clicked: 'visible-query'" in script:
                if fault[0].startswith('result'):
                    return {'clicked': 'visible-query', 'tag': 'BUTTON', 'id': '', 'name': '', 'text': '查詢'}
                return [{'clicked': None}, 1]
            if 'scopes.length !== 1' in script:
                if fault[0] == 'result_native': raise TimeoutError('synthetic result read')
                return {'bound': False, 'scopeCount': 0}
            return ''
    frame = Frame()
    crawler = object.__new__(EsunCrawler)
    crawler.transaction_cursors = {}
    crawler._parse_account_overview = lambda _: []
    crawler._navigate_menu = lambda *a: {'frames': [{'result': {'clicked': True}}]}
    crawler._unique_twd_query_frame = lambda _: frame
    crawler._validated_twd_options = lambda _: [{'index': 0, 'value': 'synthetic', 'text': 'synthetic', 'identity': '0000000000001'}]
    # Use the actual accepted path constant, not a guessed endpoint.
    frame.url = 'https://ebank.esunbank.com.tw' + esun._ESUN_TWD_HISTORY_PATH
    crawler._capture_twd_response = lambda response, hits: hits.append(None)
    def on(event, listener):
        calls.append('on')
        if fault[0].startswith('result'): listener(None)
    page = SimpleNamespace(evaluate=lambda _: False, wait_for_timeout=lambda ms: calls.append(('wait', ms)),
                           frames=[], url='https://ebank.esunbank.com.tw/', on=on,
                           remove_listener=lambda *a: calls.append('remove'))
    collector = SimpleNamespace(hits=[], attach=lambda _: None, detach=lambda _: None)
    if context:
        return crawler, page, collector, calls
    return lambda: crawler.collect(page, collector), calls


@pytest.mark.parametrize('fault,code', [(('selected', 1), 'ESUN-05-867-001'), (('selected', 2), 'ESUN-05-867-002'),
                                       (('period', 1), 'ESUN-05-867-003'), (('period', 2), 'ESUN-05-867-004'),
                                       (('period', 3), 'ESUN-05-867-005'), (('period', 4), 'ESUN-05-867-006'),
                                       (('sort', 1), 'ESUN-05-867-007'),
                                       (('sort', 2), 'ESUN-05-867-008'), (('submit', 1), 'ESUN-05-867-009'),
                                       (('result', 1), 'ESUN-05-867-010'), (('result_native', 1), 'ESUN-05-626-001')])
def test_legacy_collect_jsf_negative_to_parent_sink(monkeypatch, fault, code):
    action, calls = legacy_surface(monkeypatch, fault)
    sink(action, code, RuntimeError, 'esun-twd-history')
    assert calls.count('on') == calls.count('remove')


def node_observation(source, family, fault):
    import json
    import subprocess
    completed = subprocess.run(['node', '-e', NODE_TRACE], input=json.dumps([source, family, fault]),
                               text=True, capture_output=True, check=True)
    return json.loads(completed.stdout)


NODE_TRACE = r'''
const fs=require('fs');
const [source,family,fault]=JSON.parse(fs.readFileSync(0,'utf8'));
const trace=[];
const record=(name,value)=>{trace.push(name);return value};
// Every possible external side effect is a synthetic trace, never a real call.
global.fetch=(...args)=>{trace.push(['fetch',args]);throw Error('offline fetch')};
global.setTimeout=(...args)=>{trace.push(['timer',args.length]);return 1};
global.clearTimeout=(id)=>trace.push(['clear',id]);
const nodes={};
function wrap(name,values){return new Proxy(values,{get(o,k){trace.push(name+'.'+String(k));return o[k]}})}
function list(name,values){return new Proxy(values,{get(o,k){trace.push(name+'.'+String(k));return o[k]}})}
for(const name of ['layout','root','tab','body','header','container','timeline','safe']) {
 const values={hidden:false,inert:false,isConnected:true,parentElement:null,
  matches:(s)=>record(name+'.matches:'+s, !(fault===9&&name==='container'||fault===12&&name==='header')),
  getClientRects:()=>record(name+'.rects',list(name+'.rects',fault===5&&family==='safe'||fault===2&&family==='timeline'?[]:[{}])),
  getAttribute:(k)=>record(name+'.attr:'+k,name==='tab'?(k==='role'?(fault===16?'bad':'tab'):k==='aria-selected'?(fault===17?'false':'true'):null):null),
  closest:(s)=>record(name+'.closest:'+s,s==='.tab-body'?(fault===8?null:nodes.body):fault===11?nodes.header:nodes.container),
  contains:(n)=>record(name+'.contains',fault!==10),
  querySelectorAll:(s)=>record(name+'.query:'+s,list(name+'.query-result',fault===14&&s.includes('header')||fault===15&&s.includes('body')?[]:[{}])),
  classList:{contains:(s)=>record(name+'.class:'+s,fault===2&&family==='destination')},
  innerText:'synthetic timeline'};
 nodes[name]=wrap(name,values);
 nodes[name+'Values']=values;
}
nodes.rootValues.parentElement=nodes.body;
nodes.bodyValues.parentElement=nodes.container;
nodes.containerValues.parentElement=nodes.layout;
nodes.tabValues.parentElement=nodes.header;
nodes.headerValues.parentElement=nodes.container;
if(family==='safe') {
 nodes.safeValues.hidden=fault===1;
 nodes.safeValues.inert=fault===2;
 nodes.safeValues.matches=(s)=>record('safe.matches:'+s,fault===3);
 nodes.safeValues.isConnected=fault!==4;
}
if(family==='destination') {
 for(const [number,name] of [[3,'layout'],[5,'root'],[7,'tab']]) if(fault===number) nodes[name+'Values'].hidden=true;
 if(fault===13) nodes.headerValues.parentElement=null;
}
global.getComputedStyle=(node)=>{
 const name=Object.keys(nodes).find(k=>nodes[k]===node);
 return record('style:'+name,wrap('style:'+name,{visibility:(family==='safe'&&fault===6||family==='timeline'&&fault===3)?'hidden':'visible'}));
};
global.document={querySelectorAll(s){
 trace.push('document.query:'+s);
 const name=s==='#layout-content'?'layout':s==='#tw'?'tab':s.includes('timeline-query-continer')?'timeline':'root';
 const missing=family==='destination'&&((fault===1&&name==='layout')||(fault===4&&name==='root')||(fault===6&&name==='tab'))||family==='timeline'&&fault===1;
 return list('document.result:'+name,missing?[]:[nodes[name]]);
}};
if(family==='settle') {
 const grid=wrap('grid',{classList:list('classes',fault===2?['slide-left-enter-active']:[])});
 nodes.safeValues.querySelectorAll=(s)=>record('settle.query:'+s,list('grids',fault===1?[]:[grid]));
 nodes.safeValues.getAnimations=(arg)=>record('settle.animations:'+JSON.stringify(arg),list('animations',[wrap('animation',{playState:fault===3?'running':'finished'})]));
}
const result=require('vm').runInNewContext('('+source+')',{
 document:global.document,getComputedStyle:global.getComputedStyle,
 fetch:global.fetch,setTimeout:global.setTimeout,clearTimeout:global.clearTimeout
})(family==='safe'||family==='settle'?nodes.safe:undefined);
console.log(JSON.stringify({result,trace}));
'''


@pytest.mark.parametrize('family,markers', [('safe', 6), ('destination', 17), ('timeline', 3), ('settle', 3)])
def test_original_and_diagnostic_js_exact_native_read_and_side_effect_traces(family, markers):
    from backend.banks.esun_spa import collection as m
    import ast
    import inspect
    _, baseline_node = frozen_method(query, 'query_twd')
    current_node = ast.parse(inspect.getsource(query.query_twd))
    settle = lambda tree: next(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and type(n.value) is str and 'getAnimations' in n.value)
    old, new = {'settle': (settle(baseline_node), settle(current_node)),
                'safe': (query.SAFE, query.SAFE_REASON),
                'destination': (m.DESTINATION, m.DESTINATION_REASON),
                'timeline': (m.TIMELINE, m.TIMELINE_REASON)}[family]
    for fault in range(markers + 1):
        a, b = node_observation(old, family, fault), node_observation(new, family, fault)
        assert a['trace'] == b['trace'], (family, fault, a, b)
        assert a['result'] == b['result'][0]
        assert b['result'][1] == fault


def frozen_method(module, name):
    import ast
    import subprocess
    from pathlib import Path
    relative = Path(module.__file__).relative_to(Path(__file__).parents[1])
    source = subprocess.check_output(['git', 'show', '16f2d4b:' + str(relative)],
                                     cwd=Path(__file__).parents[1], text=True)
    node = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef) and n.name == name)
    node.decorator_list = []
    namespace = dict(vars(module))
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<immutable-original>', 'exec'), namespace)
    return namespace[name], node


def test_actual_query_old_new_native_trace_and_false_then_true_recovery(monkeypatch):
    import sys
    baseline, _ = frozen_method(query, 'query_twd')
    methods = ('locator', 'filter', 'count', 'is_visible', 'is_enabled', 'is_checked', 'evaluate',
               'input_value', 'inner_text', 'get_attribute', 'click')
    native = {name: getattr(QueryLocator, name) for name in methods}
    for recovery in (False, True):
        results = []
        for method in (baseline, query.query_twd):
            clock = itertools.count()
            monkeypatch.setattr(query.time, 'monotonic', lambda: next(clock) / 100)
            trace, recover, mount_counts = [], [recovery], {}
            for name in methods:
                def operation(target, *a, op=name, **kw):
                    args = tuple('<SAFE>' if type(v) is str and v in (query.SAFE, query.SAFE_REASON) else v.selector if isinstance(v, QueryLocator) else v for v in a)
                    trace.append((target.selector, op, args, tuple(sorted((k, v.selector if isinstance(v, QueryLocator) else v) for k,v in kw.items()))))
                    caller = sys._getframe(1)
                    names = []
                    while caller:
                        names.append(caller.f_code.co_name)
                        caller = caller.f_back
                    if op == 'count' and 'dates_mounted' in names and 'input.combo-input[name=startDate]' in target.selector:
                        mount_counts[target.selector] = mount_counts.get(target.selector, 0) + 1
                        if recover[0] and mount_counts[target.selector] == 4:
                            recover[0] = False
                            return 0
                    return native[op](target, *a, **kw)
                monkeypatch.setattr(QueryLocator, name, operation)
            page = QueryPage(None)
            page.wait_for_timeout = lambda ms: trace.append(('wait', ms))
            budget = [0]
            method(page, date(2026, 9, 1), date(2026, 9, 1), 'zh-TW', 'synthetic',
                   lambda: trace.append('guard') or True, lambda: trace.append('revalidate') or 'synthetic',
                   action_budget=budget, before_action=lambda: trace.append('admission'),
                   before_submit=lambda: trace.append('submit-admission'))
            results.append((trace, budget))
        assert results[0] == results[1]
        if recovery: assert ('wait', 20) in results[1][0]


def test_deeper_callback_id_and_handled_owner_failure_do_not_poison_later_wait(monkeypatch):
    tagged = with_error_code(TimeoutError('synthetic callback'), 'ESUN-05-700-001')
    sink(lambda: run_query(QueryPage(None), lambda: (_ for _ in ()).throw(tagged)),
         'ESUN-05-700-001', TimeoutError, 'synthetic callback')
    page = QueryPage(None)
    old_count = QueryLocator.count
    def count(target):
        if target.selector == 'dialog:visible,[role="dialog"]:visible': return 2
        return old_count(target)
    monkeypatch.setattr(QueryLocator, 'count', count)
    page.wait_for_timeout = lambda ms: (_ for _ in ()).throw(TimeoutError('synthetic wait'))
    clock = itertools.count()
    monkeypatch.setattr(query.time, 'monotonic', lambda: next(clock) / 100)
    sink(lambda: run_query(page), 'ESUN-05-728-001', TimeoutError, 'synthetic wait')


class CalendarPage(QueryPage):
    def __init__(self):
        super().__init__(None)
        self.mode, self.year, self.stage, self.active = 'days', 2026, None, 'startDate'
        self.values = {'startDate': '2026/09/01', 'endDate': '2026/09/01'}
        self.trace = []
    def locator(self, selector): return CalendarLocator(self, selector)


class CalendarLocator(QueryLocator):
    def locator(self, selector): return CalendarLocator(self.page, self.selector + ' ' + selector)
    def count(self):
        if 'month-item' in self.selector: return 1 if self.selector.endswith(' nth') else 12
        if self.selector.endswith('.months-block'): return int(self.page.mode == 'months')
        if self.selector.endswith('.days-block'): return int(self.page.mode == 'days')
        return super().count()
    def inner_text(self):
        if 'header-label' in self.selector:
            return str(self.page.year) if self.page.mode == 'months' else str(self.page.year) + '年09月'
        return super().inner_text()
    def input_value(self):
        if 'accountList' in self.selector: return 'synthetic'
        name = 'startDate' if 'startDate' in self.selector else 'endDate'
        return self.page.values[name]
    def nth(self, index): return CalendarLocator(self.page, self.selector + ' nth')
    def all_text_contents(self): return [str(n) for n in range(12)]
    def evaluate_handle(self, script):
        page = self.page
        return SimpleNamespace(evaluate=lambda s: page.trace.append(('handle', s)) or True,
                               dispose=lambda: page.trace.append('dispose'))
    def click(self, **kw):
        page, s = self.page, self.selector
        page.active = 'startDate' if 'startDate' in s else 'endDate'
        if 'header-label' in s: page.mode, page.stage = 'months', 'month_view'
        elif 'prev-icon' in s: page.year, page.stage = page.year - 1, 'year_step'
        elif 'month-item' in s: page.mode, page.stage = 'days', 'transition'
        elif 'span.day-cell' in s:
            page.values[page.active] = '2025/09/01'
            page.opened = False
        else:
            page.opened, page.mode, page.year, page.stage = True, 'days', 2026, 'open'
        page.trace.append(('click', s, kw))


@pytest.mark.parametrize('field', ['startDate', 'endDate'])
@pytest.mark.parametrize('marker,number', [(1, 18), (2, 19), (3, 20)])
def test_calendar_settle_js_negative_terminal_reason(monkeypatch, field, marker, number):
    page = CalendarPage()
    clock = itertools.count()
    monkeypatch.setattr(query.time, 'monotonic', lambda: next(clock) * 10)
    original = CalendarLocator.evaluate
    def evaluate(target, script):
        if 'getAnimations' in script and field in target.selector:
            return [False, marker]
        return original(target, script)
    monkeypatch.setattr(CalendarLocator, 'evaluate', evaluate)
    code = TRANSITION_CODES[field][marker - 1]
    sink(lambda: query.query_twd(page, date(2025, 9, 1), date(2025, 9, 1), 'zh-TW', 'synthetic',
                                lambda: True, lambda: 'synthetic'), code)
    assert page.trace[-2][0] == 'handle'
    assert page.trace[-1] == 'dispose'


def in_readiness():
    import sys
    names, frame = [], sys._getframe(1)
    while frame:
        names.append(frame.f_code.co_name)
        frame = frame.f_back
    return 'ready' in names and 'owner_state' not in names


@pytest.mark.parametrize('field', ['startDate', 'endDate'])
@pytest.mark.parametrize('number', range(9, 17))
def test_calendar_compound_transition_last_failed_original_operand(monkeypatch, field, number):
    page = CalendarPage()
    clock = itertools.count()
    monkeypatch.setattr(query.time, 'monotonic', lambda: next(clock) * 10)
    stage = 'month_view' if number < 12 else 'year_step' if number < 14 else 'transition'
    old_count, old_text, old_handle = CalendarLocator.count, CalendarLocator.inner_text, CalendarLocator.evaluate_handle
    def matches(target): return field in target.selector and page.stage == stage and in_readiness()
    def count(target):
        if matches(target):
            if number in (10, 13) and 'month-item' in target.selector: return 11
            if number == 11 and target.selector.endswith('.days-block'): return 1
            if number == 15 and target.selector.endswith('.months-block'): return 1
        return old_count(target)
    def text(target):
        if matches(target) and number in (9, 12, 14) and 'header-label' in target.selector: return 'pending'
        return old_text(target)
    def handle(target, script):
        result = old_handle(target, script)
        old_evaluate = result.evaluate
        def evaluate(expression):
            if number == 16 and field in target.selector and 's.first' in expression: return False
            return old_evaluate(expression)
        result.evaluate = evaluate
        return result
    monkeypatch.setattr(CalendarLocator, 'count', count)
    monkeypatch.setattr(CalendarLocator, 'inner_text', text)
    monkeypatch.setattr(CalendarLocator, 'evaluate_handle', handle)
    expected = CALENDAR_EXPECTED[field][number - 9]
    sink(lambda: query.query_twd(page, date(2025, 9, 1), date(2025, 9, 1), 'zh-TW', 'synthetic',
                                lambda: True, lambda: 'synthetic'), expected)


CALENDAR_EXPECTED = {
    'startDate': ('ESUN-05-862-009', 'ESUN-05-862-010', 'ESUN-05-862-011', 'ESUN-05-862-012',
                  'ESUN-05-862-013', 'ESUN-05-862-014', 'ESUN-05-862-015', 'ESUN-05-862-016'),
    'endDate': ('ESUN-05-862-509', 'ESUN-05-862-510', 'ESUN-05-862-511', 'ESUN-05-862-512',
                'ESUN-05-862-513', 'ESUN-05-862-514', 'ESUN-05-862-515', 'ESUN-05-862-516'),
}


TRANSITION_CODES = {
    'startDate': ('ESUN-05-862-018', 'ESUN-05-862-019', 'ESUN-05-862-020'),
    'endDate': ('ESUN-05-862-518', 'ESUN-05-862-519', 'ESUN-05-862-520'),
}


NODE_LEGACY = r'''
const fs=require('fs');
const [source,family,fault]=JSON.parse(fs.readFileSync(0,'utf8'));
const trace=[];
const wrap=(name,obj)=>new Proxy(obj,{get(o,k){trace.push(name+'.'+String(k));return o[k]},set(o,k,v){trace.push(['set',name,String(k),v]);if(!(k==='checked'&&fault===2))o[k]=v;return true}});
const option=wrap('option',{textContent:'synthetic',value:'synthetic'});
const select=wrap('select',{options:wrap('options',family==='selected'&&fault===2?[]:[option]),selectedIndex:0,value:'synthetic'});
const radio=wrap('radio',{checked:false,closest(s){trace.push('closest:'+s);return null},dispatchEvent(e){trace.push(['event','radio',e.type])}});
const field=name=>wrap(name,{value:'',dispatchEvent(e){trace.push(['event',name,e.type])}});
const start=field('start'),end=field('end');
const button=wrap('button',{textContent:'查詢',value:'',title:'',tagName:'BUTTON',id:'',name:'',
 getBoundingClientRect(){trace.push('rect');return {width:1,height:1}},click(){trace.push('click')}});
global.Event=class {constructor(type,options){trace.push(['Event',type,options]);this.type=type}};
global.window={getComputedStyle(n){trace.push('style');return {display:'block',visibility:'visible'}}};
global.getComputedStyle=global.window.getComputedStyle;
global.document={querySelector(s){trace.push('query:'+s);
 if(s.includes('dract'))return fault===1?null:select;
 if(s.includes('intervalrdo4')||s.includes('j_id_sort1'))return fault===1?null:radio;
 if(s.includes('startDate'))return fault===3?null:start;
 if(s.includes('endDate'))return fault===4?null:end;
 throw Error('unexpected selector');
},querySelectorAll(s){trace.push('queryAll:'+s);
 if(s==='input[name="fao01002:intervalrdo"]')return [radio];
 if(s==='.radiobutton-group label')return [];
 if(s.includes('button,a,input'))return fault===1?[]:[button];
 throw Error('unexpected selectors');
}};
global.fetch=(...a)=>{trace.push(['fetch',a]);throw Error('offline fetch')};
global.setTimeout=(...a)=>{trace.push(['timer',a.length]);return 1};
global.clearTimeout=(id)=>trace.push(['clear',id]);
const result=require('vm').runInNewContext('('+source+')',{
 document:global.document,window:global.window,Event:global.Event,
 getComputedStyle:global.getComputedStyle,fetch:global.fetch,
 setTimeout:global.setTimeout,clearTimeout:global.clearTimeout
})({start:'2026/01/01',end:'2026/02/01'});
console.log(JSON.stringify({result,trace}));
'''


@pytest.mark.parametrize('family,needle,markers', [
    ('selected', 's?.options[s.selectedIndex]', [0, 1, 2]),
    ('period', 'no intervalrdo4', [0, 1, 2, 3, 4]),
    ('sort', 'fao01002:j_id_sort1', [0, 1, 2]),
    ('submit', "clicked: 'visible-query'", [0, 1])])
def test_legacy_collect_original_js_click_event_read_and_transport_trace(family, needle, markers):
    import ast
    import inspect
    import json
    import subprocess
    from backend.banks import esun
    _, old_node = frozen_method(esun, 'collect')
    new_node = ast.parse('if True:\n' + inspect.getsource(esun.EsunCrawler.collect))
    def script(tree):
        return next(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and type(n.value) is str and needle in n.value)
    old, new = script(old_node), script(new_node)
    for fault in markers:
        outputs=[]
        for source in (old,new):
            outputs.append(json.loads(subprocess.run(['node','-e',NODE_LEGACY], input=json.dumps([source,family,fault]),
                                                    text=True,capture_output=True,check=True).stdout))
        a,b=outputs
        value=b['result'][0] if type(b['result']) is list else b['result']
        assert a['result'] == value, (family,fault,a,b)
        assert a['trace'] == b['trace'], (family,fault,a,b)
        marker = b['result'][1] if type(b['result']) is list else 0
        assert marker == fault, ('producer marker', family, fault, b)


READINESS = {
    'startDate': {1: 'ESUN-05-862-001', 3: 'ESUN-05-862-003', 4: 'ESUN-05-862-004',
                  5: 'ESUN-05-862-005', 6: 'ESUN-05-862-006', 7: 'ESUN-05-862-007', 8: 'ESUN-05-862-008'},
    'endDate': {1: 'ESUN-05-862-002', 3: 'ESUN-05-862-503', 4: 'ESUN-05-862-504',
                5: 'ESUN-05-862-505', 6: 'ESUN-05-862-506', 7: 'ESUN-05-862-507', 8: 'ESUN-05-862-508'},
}


CHECKPOINT_CODES = {
    'initial': ('ESUN-05-861-001', 'ESUN-05-861-002', 'ESUN-05-861-003', 'ESUN-05-861-004'),
    'poll': ('ESUN-05-861-005', 'ESUN-05-861-006', 'ESUN-05-861-007', 'ESUN-05-861-008'),
    'before_click': ('ESUN-05-861-009', 'ESUN-05-861-010', 'ESUN-05-861-011', 'ESUN-05-861-012'),
    'after_click': ('ESUN-05-861-013', 'ESUN-05-861-014', 'ESUN-05-861-015', 'ESUN-05-861-016'),
    'final': ('ESUN-05-861-017', 'ESUN-05-861-018', 'ESUN-05-861-019', 'ESUN-05-861-020'),
    'submit': ('ESUN-05-861-021', 'ESUN-05-861-022', 'ESUN-05-861-023', 'ESUN-05-861-024'),
    'post_submit': ('ESUN-05-861-025', 'ESUN-05-861-026', 'ESUN-05-861-027', 'ESUN-05-861-028'),
}
