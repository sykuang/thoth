"""Finite offline residuals; native calls are synthetic, bank code is real."""
import ast
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.error_codes import error_step, exception_error_id, code_details


_PROVEN_IDS = []


@pytest.fixture(autouse=True)
def _proof_ids(request):
    import json
    _PROVEN_IDS.clear()
    yield
    request.node.user_properties.append(('error_ids', json.dumps(sorted(set(_PROVEN_IDS)))))


def _registered(code, bank):
    details = code_details(code, bank=bank)
    assert details is not None
    assert any("\u4e00" <= ch <= "\u9fff" for ch in str(details))
    _PROVEN_IDS.append(code)
    return code


def _old(bank):
    from tests.test_ordered_sinopac_fubon_codes import _baseline_bank
    return _baseline_bank(bank)

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("role", ["inventory", "history"])
@pytest.mark.parametrize("reason,changes", [
    (1, {"url": "http://mma.sinopac.com/x"}),
    (2, {"url": "https://elsewhere.invalid/x"}),
    (3, {"url": "https://mma.sinopac.com:444/x"}),
    (4, {"raw_url": "https://mma.sinopac.com:444/x"}),
    (5, {"url": "https://u@mma.sinopac.com/x"}),
    (6, {"url": "https://mma.sinopac.com/x"}),
    (7, {"url_suffix": ";extra"}),
    (8, {"url_suffix": "#fragment"}),
    (9, {"url_suffix": "?query=1"}),
    (10, {"raw_suffix": "?bad"}),
])
def test_sinopac_exact_url_reason_reaches_required_parent_with_old_read_trace(role, reason, changes):
    from backend.banks.sinopac import SinopacCrawler
    from tests.test_sinopac_history_sync import _history_hit, _inventory_hit, ACCOUNT, LABEL
    traces, errors = [], []
    for cls in (_old("sinopac").SinopacCrawler, SinopacCrawler):
        trace = []
        hit = _history_hit() if role == "history" else _inventory_hit()
        for key, value in changes.items():
            if key.endswith("_suffix"):
                target = "url" if key == "url_suffix" else "raw_url"
                setattr(hit, target, getattr(hit, target).split("?")[0] + value)
            else:
                setattr(hit, key, value)
        class Hit:
            def __getattr__(self, name):
                trace.append(name)
                return getattr(hit, name)
        observed = Hit()
        assert cls._exact_hit_url(observed, cls._TWD_HISTORY_PATH if role == "history" else cls._TWD_INVENTORY_PATH, numeric_query=True) is False
        trace.clear()
        with pytest.raises(RuntimeError) as caught:
            if role == "history":
                cls._validate_history_hit(observed, label=LABEL, identity=ACCOUNT, currency="TWD", start=date(2026, 8, 1), end=date(2026, 8, 31), business_date="20260831", as_of=date(2026, 8, 31))
            else:
                collector = SimpleNamespace(by_endpoint=lambda endpoint: [observed])
                cls._twd_inventory(collector)
        traces.append(trace)
        errors.append(caught.value)
    assert traces[0] == traces[1]
    assert (type(errors[0]), str(errors[0])) == (type(errors[1]), str(errors[1]))
    expected = ("SINOPAC-07-780-" if role == "history" else "SINOPAC-04-780-") + f"{reason:03}"
    assert exception_error_id(errors[1], bank="sinopac") == _registered(expected, "sinopac")


@pytest.mark.parametrize("count", [0, 2])
def test_required_captcha_exhaustion_has_role_reason_but_optional_is_quiet(count, monkeypatch):
    import backend.banks.sinopac as current
    from tests.test_sinopac_login_checkpoints import _submit_fixture
    from unittest.mock import Mock
    outcomes=[];traces=[]
    for module in (_old('sinopac'),current):
        crawler, page, *_ = _submit_fixture()
        crawler.__class__=module.SinopacCrawler
        del crawler._ocr_captcha
        images = Mock()
        images.count.return_value = count
        images.nth.return_value = images
        images.is_visible.return_value = images.is_enabled.return_value = True
        original_locator = page.locator.side_effect
        def locator(selector):
            return images if selector == module.SEL_CAP_IMG else original_locator(selector)
        page.locator.side_effect = locator
        assert crawler._captcha_image(page) is None
        errors=[]
        for action in (crawler.submit_credentials_once,crawler.prepare_captcha_resubmit):
            with pytest.raises(RuntimeError) as caught:action(page)
            errors.append(caught.value)
        outcomes.append(errors)
        traces.append((page.mock_calls,images.mock_calls))
    assert traces[0]==traces[1]
    assert [(type(e).__name__,str(e)) for e in outcomes[0]]==[(type(e).__name__,str(e)) for e in outcomes[1]]
    assert exception_error_id(outcomes[1][0], bank="sinopac") == _registered("SINOPAC-02-780-001" if count == 0 else "SINOPAC-02-780-002", "sinopac")
    assert exception_error_id(outcomes[1][1], bank="sinopac") == _registered("SINOPAC-02-781-001" if count == 0 else "SINOPAC-02-781-002", "sinopac")



def _node(expression, arg, setup):
    import json
    import subprocess
    script = """const fs=require('fs'); const input=JSON.parse(fs.readFileSync(0,'utf8'));
    const trace=[]; global.fetch=()=>{throw Error('unexpected fetch')};
    (async()=>{ eval(input.setup); const value=await eval('('+input.expression+')')(input.arg);
    process.stdout.write(JSON.stringify({value,trace})); })().catch(e=>{console.error(e.stack);process.exit(1)});"""
    result = subprocess.run(["node", "-e", script], input=json.dumps(dict(expression=expression, arg=arg, setup=setup)), text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize("missing,expected", [("#AcctValue", "SINOPAC-05-780-001"), ("#AcctValueFormat", "SINOPAC-05-780-002"), ("#btnQuery", "SINOPAC-05-780-003"), (None, None)])
def test_loan_real_js_negative_reaches_parent_and_preserves_old_dom_trace(missing, expected):
    import json
    from backend.banks.sinopac import SinopacCrawler
    from backend.core.base import ResponseCollector
    from tests.test_sinopac_loan import _LoanPage
    outcomes, traces = [], []
    for cls in (_old("sinopac").SinopacCrawler, SinopacCrawler):
        crawler = object.__new__(cls)
        crawler._response_visible = lambda page: False
        collector = ResponseCollector("sinopac.com")
        page = _LoanPage(collector)
        native_evaluate = page.evaluate
        trace = []
        def evaluate(expression, arg=None):
            setup = """global.Event=class{constructor(type){this.type=type}};
            const missing=MISSING;
            global.document={querySelector(selector){trace.push(['query',selector]);
                if(selector===missing)return null;
                return {set value(v){trace.push(['value',selector,v])},
                    dispatchEvent(e){trace.push(['event',selector,e.type])},
                    click(){trace.push(['click',selector])}};}};""".replace("MISSING", json.dumps(missing))
            result = _node(expression, arg, setup)
            trace.extend(result["trace"])
            if missing is None:
                native_evaluate(expression, arg)
            return result["value"]
        page.evaluate = evaluate
        try:
            value = crawler._collect_loans(page, collector)
            outcomes.append(("ok", value))
        except RuntimeError as error:
            outcomes.append((type(error).__name__, str(error), exception_error_id(error, bank="sinopac")))
        traces.append(trace)
    assert traces[0] == traces[1]
    if expected:
        assert outcomes[0][:2] == outcomes[1][:2] == ("RuntimeError", "sinopac-loan-query-control")
        assert outcomes[1][2] == _registered(expected, "sinopac")
    else:
        assert outcomes[0][:2] == outcomes[1][:2]
        assert sum(item[0] == "click" for item in traces[1]) == 1


FUBON_DOM = r"""
global.window={}; global.location={href:'https://ebank.taipeifubon.com.tw/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces'};
global.CSS={escape:x=>x}; global.Event=class{constructor(type){this.type=type}};
const config=CONFIG;
const make=(id)=>new Proxy({id,value:id==='state'?'state-1':'012-000-90000000267053-X-TW',name:'period',checked:true,
    selectedIndex:0,options:[{value:'012-000-90000000267053-X-TW',textContent:'account 90000000267053'}],
    method:'POST',action:location.href,textContent:'account 90000000267053',
    getAttribute(name){trace.push(['attr',id,name]);return config.actionURL??location.href},
    contains(node){trace.push(['contains',node.id]);return config.unowned!==node.id},
    dispatchEvent(e){trace.push(['event',id,e.type])},click(){trace.push(['click',id])},
    querySelectorAll(selector){trace.push(['ownedQuery',selector]);return Array.from({length:config.checkedCount??1},()=>config.wrongChecked?nodes.detail:nodes.preset)}
},{get(target,key){trace.push(['get',id,String(key)]);
    if(config[id+'_'+String(key)]!==undefined)return config[id+'_'+String(key)];return target[key]},
    set(target,key,value){trace.push(['set',id,String(key),value]);target[key]=value;return true}});
const nodes={form:make('form'),account:make('account'),submit:make('submit'),state:make('state'),detail:make('detail'),fast:make('fast'),preset:make('preset')};
global.document={documentElement:{},querySelectorAll(selector){trace.push(['query',selector]);
    let id=selector==='form#form1'?'form':selector.includes('comboAccount')?'account':selector.includes('ViewState')?'state':selector.includes('doValidateAndSubmit')?'submit':selector.includes('rdoTxDetail')?'detail':selector.includes('rdoFast')?'fast':'preset';
    const count=config[id+'Count']??1;return Array.from({length:count},()=>nodes[id]);}};
if(config.sameDocument)window.__hermesFubonSubmitDocument=document;
global.MutationObserver=class{constructor(callback){trace.push(['observer'])} observe(){trace.push(['observe'])} disconnect(){trace.push(['disconnect'])}};
"""


def _observe_native_page(page, trace, frame=None):
    """Record native args/order; changed script bytes are tested separately."""
    def root(value,label):
        for name in ('nth','count','is_visible','is_enabled','click','fill','input_value','evaluate'):
            target=getattr(value,name,None)
            if not callable(target):continue
            def call(*args,_name=name,_target=target,**kwargs):
                trace.append([label,_name,args[1:] if _name=='evaluate' else args,kwargs])
                return _target(*args,**kwargs)
            setattr(value,name,call)
        return value
    for obj,label in ((page,'page'),(frame,'frame')):
        if obj is None:continue
        for name in ('goto','wait_for_timeout','on','remove_listener','close','evaluate','locator'):
            target=getattr(obj,name,None)
            if not callable(target):continue
            def call(*args,_name=name,_target=target,_label=label,**kwargs):
                recorded=args[:1] if _name in ('on','remove_listener') else args[1:] if _name=='evaluate' else args
                trace.append([_label,_name,recorded,kwargs])
                value=_target(*args,**kwargs)
                return root(value,_label+':'+str(args[0])) if _name=='locator' else value
            setattr(obj,name,call)
    emit=getattr(page,'_emit',None)
    if emit:
        def observed_emit(event,value):
            request=value.request if event=='response' else value
            trace.append(['request-event',event,request.method,request.url,request.post_data])
            return emit(event,value)
        page._emit=observed_emit


def _fubon_window_js_case(monkeypatch, marker, config, guard, expected, role=None, parse_fault=None):
    import json
    import backend.banks.fubon as current
    from tests.test_fubon_history_sync import test_fubon_settles_all_same_frame_responses_before_dom_publication as drive
    traces, errors = [], []
    for module in (_old('fubon'), current):
        trace = []
        native = module.bounded_evaluate
        fault_ready = False
        def bounded(scope, expression, arg=None, **kwargs):
            nonlocal fault_ready
            target = marker in expression and (role is None or role in str(arg))
            if target:
                fault_ready = True
                source_scope = scope
                def locator(selector):
                    trace.append(('locator', selector))
                    def evaluate(wrapped, value=None, **options):
                        assert options == {'timeout':5000}
                        result = _node('arg => (' + wrapped + ')(null,arg)', value, FUBON_DOM.replace('CONFIG', json.dumps(config)))
                        trace.extend(result['trace'])
                        if result['value'] is True and 'nodes[0].click()' in expression:
                            source_scope.page.emit_control_request(source_scope)
                        if result['value'] is True and 'submits[0].click()' in expression:
                            source_scope.page.emit_submit_requests(source_scope)
                        return result['value']
                    return SimpleNamespace(evaluate=evaluate)
                scope = SimpleNamespace(locator=locator)
            return native(scope, expression, arg, **kwargs)
        method = module.FubonCrawler._collect_twd_window
        def parent(self, *args):
            _observe_native_page(args[0],trace,args[1])
            try:
                return method(self, *args)
            except RuntimeError as error:
                errors.append(error)
                raise
        with monkeypatch.context() as patch:
            patch.setattr(module, 'bounded_evaluate', bounded)
            if parse_fault:
                form_native=module._fubon_form_action_matches
                split_native=module.urlsplit
                class Parsed:
                    def __init__(self,value): self.value=value
                    def __getattr__(self,name):
                        if name=='port' and parse_fault=='port':raise ValueError('synthetic parser port failure')
                        if name=='username' and parse_fault=='password':return None
                        if name=='password' and parse_fault=='password':return 'synthetic'
                        return getattr(self.value,name)
                def form(*args,**kwargs):
                    if not fault_ready:return form_native(*args,**kwargs)
                    with monkeypatch.context() as nested:
                        nested.setattr(module,'urlsplit',lambda value:Parsed(split_native(value)))
                        return form_native(*args,**kwargs)
                patch.setattr(module,'_fubon_form_action_matches',form)
            import tests.test_fubon_history_sync as fixtures
            patch.setattr(fixtures, 'FubonCrawler', module.FubonCrawler)
            patch.setattr(module.FubonCrawler, '_collect_twd_window', parent)
            drive('normal', guard)
        traces.append(trace)
    assert traces[0] == traces[1]
    assert traces[0]
    if expected is None:
        assert errors == []
    else:
        assert len(errors) == 2
        assert (type(errors[0]),str(errors[0])) == (type(errors[1]),str(errors[1]))
        assert exception_error_id(errors[1], bank='fubon') == _registered(expected, 'fubon')


@pytest.mark.parametrize('config,expected', [
    ({'formCount':0},'FUBON-05-780-001'),
    ({'accountCount':0},'FUBON-05-780-002'),
    ({'submitCount':0},'FUBON-05-780-003'),
    ({'form_method':'GET'},'FUBON-05-780-004'),
    ({'state_value':''},'FUBON-05-780-005'),
])
def test_fubon_initial_controls_real_js_reasons_reach_parent(monkeypatch, config, expected):
    _fubon_window_js_case(monkeypatch, 'account.value=args.value', config, 'fubon-twd-history-controls', expected)


@pytest.mark.parametrize('config,expected', [
    ({'formCount':0},'FUBON-05-781-001'),
    ({'accountCount':0},'FUBON-05-781-002'),
    ({'detailCount':0},'FUBON-05-781-003'),
    ({'fastCount':0},'FUBON-05-781-004'),
    ({'presetCount':0},'FUBON-05-781-005'),
    ({'submitCount':0},'FUBON-05-781-006'),
    ({'form_method':'GET'},'FUBON-05-781-007'),
])
def test_fubon_settled_controls_real_js_reasons_reach_parent(monkeypatch, config, expected):
    _fubon_window_js_case(monkeypatch, 'preset:preset.checked', config, 'fubon-twd-history-controls-settled-shape', expected)


@pytest.mark.parametrize('config,expected', [
    ({'formCount':0},'FUBON-04-780-001'),
    ({'accountCount':0},'FUBON-04-780-002'),
    ({'unowned':'account'},'FUBON-04-780-003'),
])
def test_fubon_inventory_real_js_reasons_reach_parent(config, expected):
    import json
    import backend.banks.fubon as current
    traces, errors = [], []
    for module in (_old('fubon'), current):
        trace = []
        def locator(selector):
            trace.append(['locator',selector])
            def evaluate(expression, arg=None, **kwargs):
                result = _node('arg => (' + expression + ')(null,arg)', arg, FUBON_DOM.replace('CONFIG',json.dumps(config)))
                trace.extend(result['trace'])
                return result['value']
            return SimpleNamespace(evaluate=evaluate)
        crawler=object.__new__(module.FubonCrawler)
        crawler._open_twd_query=lambda page: SimpleNamespace(locator=locator)
        with pytest.raises(RuntimeError, match='^fubon-twd-history-inventory$') as caught:
            crawler._collect_attested_twd_history(SimpleNamespace())
        traces.append(trace); errors.append(caught.value)
        assert caught.value.__suppress_context__ is True
    assert traces[0] == traces[1]
    assert exception_error_id(errors[1],bank='fubon') == _registered(expected,'fubon')


@pytest.mark.parametrize('role,action,node', [('rdoTxDetail','782','detail'),('rdoFast','783','fast'),('rdoDay180','784','preset')])
@pytest.mark.parametrize('stage,reason', [('before',1),('before',2),('before',3),('click',11),('click',12),('click',13),('click',14)])
def test_fubon_control_js_reasons_keep_static_role_and_dom_order(monkeypatch, role, action, node, stage, reason):
    configs={1:{'formCount':0},2:{node+'Count':0},3:{'unowned':node},11:{'formCount':0},12:{node+'Count':0},13:{'unowned':node},14:{node+'_checked':False}}
    marker='nodes[0].checked===true : null' if stage=='before' else 'nodes[0].click()'
    _fubon_window_js_case(monkeypatch, marker, configs[reason], 'fubon-twd-history-controls', f'FUBON-05-{action}-{reason:03}', role)


@pytest.mark.parametrize('config,reason', [
    ({'formCount':0},1), ({'presetCount':0},2), ({'unowned':'preset'},3),
    ({'preset_name':''},4), ({'preset_checked':False},5),
    ({'checkedCount':0},6), ({'checkedCount':2},6), ({'wrongChecked':True},7),
    ({'submitCount':0},8), ({'unowned':'submit'},9),
])
def test_fubon_submit_original_js_rejections_are_distinct(monkeypatch, config, reason):
    _fubon_window_js_case(monkeypatch,'submits[0].click()',config,'fubon-twd-history-controls-preset',f'FUBON-05-785-{reason:03}')


def test_fubon_guard_original_document_rejection_is_source_owned(monkeypatch):
    _fubon_window_js_case(monkeypatch,'__hermesFubonDomGuard=new MutationObserver',{},'fubon-twd-history-result-mutated','FUBON-05-786-001')


@pytest.mark.parametrize('stage,action,guard', [('initial','787','fubon-twd-history-controls'),('settled','788','fubon-twd-history-controls-action')])
@pytest.mark.parametrize('url,reason', [
    ('http://ebank.taipeifubon.com.tw/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces',2),
    ('https://elsewhere.invalid/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces',3),
    ('https://ebank.taipeifubon.com.tw:444/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces',4),
    ('https://ebank.taipeifubon.com.tw/other',5),
    ('https://u@ebank.taipeifubon.com.tw/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces',6),
    ('https://u:p@ebank.taipeifubon.com.tw/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces',6),
    ('https://:p@ebank.taipeifubon.com.tw/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces',6),
    ('https://ebank.taipeifubon.com.tw/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces#x',8),
    ('https://ebank.taipeifubon.com.tw/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces?q=x',9),
])
def test_fubon_required_form_reason_and_role_preserve_old_native_trace(monkeypatch, stage, action, guard, url, reason):
    marker='account.value=args.value' if stage=='initial' else 'preset:preset.checked'
    _fubon_window_js_case(monkeypatch,marker,{'actionURL':url},guard,f'FUBON-05-{action}-{reason:03}')


@pytest.mark.parametrize('role,action', [('menu','789'),('inventory','790'),('publication','791')])
@pytest.mark.parametrize('mutation,reason', [('path',1),('user',2),('password',3),('fragment',4),('query',None)])
def test_fubon_required_route_reason_reaches_real_static_caller(monkeypatch, role, action, mutation, reason):
    import backend.banks.fubon as current
    menu='/B2C/cgequ/cgequ001/CGEQU001_Home.faces'
    history='/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces'
    route=menu if role=='menu' else history
    bad='https://ebank.taipeifubon.com.tw'+route
    if mutation=='path': bad='https://ebank.taipeifubon.com.tw/other'
    if mutation=='user': bad=bad.replace('https://','https://u@')
    if mutation=='fragment': bad+='#x'
    if mutation=='query': bad+='?q=x'; reason=5 if role=='menu' else 6
    traces, errors=[],[]
    for module in (_old('fubon'),current):
        trace=[]
        class Frame:
            @property
            def url(self): trace.append(['url']); return bad if role!='inventory' or page.after else 'https://ebank.taipeifubon.com.tw'+menu
            def locator(self, selector):
                trace.append(['locator',selector])
                def evaluate(expression,arg=None,**kwargs):
                    result=_node('arg => ('+expression+')(null,arg)',arg,"global.document={querySelectorAll(selector){trace.push(['query',selector]);return [{textContent:'臺外幣交易明細查詢',click(){trace.push(['click'])}}]}};")
                    trace.extend(result['trace']);return result['value']
                return SimpleNamespace(evaluate=evaluate)
        frame=Frame()
        class Page:
            after=False
            @property
            def frames(self): trace.append(['frames']);return [frame]
            def goto(self,*args,**kwargs): trace.append(['goto',args,kwargs])
            def wait_for_timeout(self, ms): trace.append(['wait',ms]);self.after=ms==8000
        page=Page(); crawler=object.__new__(module.FubonCrawler)
        def owned(page,candidate): trace.append(['owned']);return True
        crawler._is_owned_frame=owned
        with monkeypatch.context() as patch:
            if mutation=='password':
                split_native=module.urlsplit
                class Parsed:
                    def __init__(self,value):self.value=value
                    def __getattr__(self,name):
                        trace.append(['parsed',name])
                        if name=='username':return None
                        if name=='password':return 'synthetic'
                        return getattr(self.value,name)
                def split(value):
                    parsed=split_native(value)
                    return Parsed(parsed) if value==bad and (role!='inventory' or page.after) else parsed
                patch.setattr(module,'urlsplit',split)
            with pytest.raises(RuntimeError,match='^fubon-twd-history-frame$') as caught:
                if role=='publication': crawler._bound_twd_result_frame(page,frame)
                else: crawler._open_twd_query(page)
        errors.append(caught.value);traces.append(trace)
    assert traces[0]==traces[1]
    assert exception_error_id(errors[1],bank='fubon')==_registered(f'FUBON-05-{action}-{reason:03}','fubon')


SINOPAC_DOM = r"""
const config=CONFIG;let mutationReads=0,tableReads=0;
global.HTMLElement=class{};global.Element=class{};
Object.defineProperties(HTMLElement.prototype,{offsetWidth:{get(){return 1}},offsetHeight:{get(){return 1}}});
Element.prototype.getClientRects=()=>[];
global.window={__hermesSinopacState:{get mutations(){trace.push(['mutations']);return config.mode==='shape'?undefined:config.mode==='changed'?++mutationReads:1}},
    __hermesSinopacObserver:{takeRecords(){trace.push(['records']);return config.mode==='records'?[{}]:[]},disconnect(){trace.push(['disconnect'])}}};
global.document={querySelectorAll(selector){trace.push(['query',selector]);
    return selector==='#ListingTable'&&config.mode==='different'&&++tableReads%2===0?[{},{}]:[]},
    querySelector(selector){trace.push(['one',selector]);return null}};
global.clearTimeout=(value)=>trace.push(['clearTimeout']);
global.setTimeout=(callback,ms)=>{trace.push(['timeout',ms]);callback();return 1};
"""


def _sinopac_history_js_case(monkeypatch, mode, expected, missing_poll=False):
    import json
    from functools import wraps
    import backend.banks.sinopac as current
    from tests.test_sinopac_history_sync import test_sinopac_collect_uses_native_account_date_and_query_controls as drive
    traces, errors=[],[]
    for module in (_old('sinopac'),current):
        trace=[];method=module.SinopacCrawler._collect_transactions
        @wraps(method)
        def parent(self,page,*args):
            _observe_native_page(page,trace)
            native=page.evaluate
            def evaluate(expression,arg=None):
                target=expression.startswith('async () => { const probe =') if not missing_poll else 'ListingTable' in expression
                if target:
                    result=_node(expression,arg,SINOPAC_DOM.replace('CONFIG',json.dumps({'mode':mode})))
                    trace.extend(result['trace']);return result['value']
                return native(expression,arg)
            page.evaluate=evaluate
            try:return method(self,page,*args)
            except RuntimeError as error:errors.append(error);raise
        with monkeypatch.context() as patch:
            import tests.test_sinopac_history_sync as fixtures
            patch.setattr(fixtures, 'SinopacCrawler', module.SinopacCrawler)
            patch.setattr(module.SinopacCrawler,'_collect_transactions',parent)
            with pytest.raises(RuntimeError,match='sinopac-twd-history-result-table'):
                drive(patch,False,True,1,False,0,0,0,0,0,0,0,0,None)
        traces.append(trace)
    assert traces[0]==traces[1]
    assert len(errors)==2
    assert (type(errors[0]),str(errors[0]))==(type(errors[1]),str(errors[1]))
    assert exception_error_id(errors[1],bank='sinopac')==_registered(expected,'sinopac')


@pytest.mark.parametrize('mode,reason',[('shape',1),('changed',2),('records',3),('different',4)])
def test_sinopac_final_bounded_js_null_reasons_reach_parent(monkeypatch,mode,reason):
    _sinopac_history_js_case(monkeypatch,mode,f'SINOPAC-05-782-{reason:03}')


def test_sinopac_missing_observer_table_remains_optional_until_original_table_failure(monkeypatch):
    _sinopac_history_js_case(monkeypatch,'normal','SINOPAC-05-781-001',missing_poll=True)


@pytest.mark.parametrize('marker,config,role', [
    ('account.value=args.value',{},None),('preset:preset.checked',{},None),
    ('nodes[0].click()',{},'rdoTxDetail'),('nodes[0].click()',{},'rdoFast'),('nodes[0].click()',{},'rdoDay180'),
    ('submits[0].click()',{},None),('__hermesFubonDomGuard=new MutationObserver',{'sameDocument':True},None),
])
def test_fubon_real_js_success_keeps_original_query_and_publication(monkeypatch,marker,config,role):
    _fubon_window_js_case(monkeypatch,marker,config,None,None,role)


def test_sinopac_absent_initial_table_recovers_without_new_guard(monkeypatch):
    from functools import wraps
    import backend.banks.sinopac as current
    from tests.test_sinopac_history_sync import test_sinopac_collect_uses_native_account_date_and_query_controls as drive
    traces=[]
    for module in (_old('sinopac'),current):
        trace=[];method=module.SinopacCrawler._collect_transactions
        @wraps(method)
        def parent(self,page,*args):
            _observe_native_page(page,trace)
            native=page.evaluate
            def evaluate(expression,arg=None):
                if 'const stale=new WeakSet' in expression:
                    result=_node(expression,arg,SINOPAC_DOM.replace('CONFIG',"{mode:'normal'}"))
                    trace.extend(result['trace']);return result['value']
                return native(expression,arg)
            page.evaluate=evaluate
            return method(self,page,*args)
        with monkeypatch.context() as patch:
            import tests.test_sinopac_history_sync as fixtures
            patch.setattr(fixtures, 'SinopacCrawler', module.SinopacCrawler)
            patch.setattr(module.SinopacCrawler,'_collect_transactions',parent)
            drive(patch,False,True,1,False,0,0,0,0,0,0,0,0,None)
        traces.append(trace)
    assert traces[0]==traces[1]


def test_optional_route_candidates_and_missing_responses_stay_quiet():
    from backend.banks import fubon
    from backend.banks.sinopac import SinopacCrawler
    from urllib.parse import urlsplit
    from backend.core.base import ResponseCollector
    for module in (_old('fubon'),fubon):
        crawler=object.__new__(module.FubonCrawler)
        crawler._is_owned_frame=lambda page,frame:True
        route='/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces'
        bad=SimpleNamespace(url='https://ebank.taipeifubon.com.tw/other')
        good=SimpleNamespace(url='https://ebank.taipeifubon.com.tw'+route)
        assert crawler._fubon_content_frame(SimpleNamespace(frames=[bad,good]),route) is good
        assert module._fubon_form_action_matches('https://elsewhere.invalid/') is False
        assert module._fubon_route_matches(urlsplit(bad.url),route) is False
    assert SinopacCrawler._latest_json(ResponseCollector('sinopac.com'),'unrelated') is None


@pytest.mark.parametrize('count',[0,2])
def test_optional_captcha_retry_missing_images_stays_quiet_and_recovers(count,monkeypatch):
    from unittest.mock import Mock
    import backend.banks.sinopac as module
    crawler=object.__new__(module.SinopacCrawler);crawler.captcha_tmp='synthetic-unused'
    image=Mock();image.nth.return_value=image
    image.count.side_effect=[1,count]
    image.is_visible.return_value=image.is_enabled.return_value=True
    page=Mock();page.locator.return_value=image
    monkeypatch.setattr(module,'wait_captcha_stable',lambda *args,**kwargs:None)
    solver=Mock(return_value=None);monkeypatch.setattr(module,'solve_captcha',solver)
    diagnostic=[]
    assert crawler._ocr_captcha(page,max_attempts=2,_diagnostic=diagnostic) is None
    assert diagnostic==[]
    image.count.side_effect=None;image.count.return_value=1;solver.return_value='123456'
    assert crawler._ocr_captcha(page,max_attempts=1,_diagnostic=diagnostic)=='123456'
    assert diagnostic==[]


def test_catalog_entries_remain_registered_and_chinese():
    from importlib import import_module
    for bank in ('sinopac','fubon'):
        codes=import_module(f'backend.core.error_catalog.{bank}').CODES
        assert len(codes)==len(set(codes))
        for code in codes:
            details=code_details(code,bank=bank)
            assert details and all(any('\u4e00'<=ch<='\u9fff' for ch in text) for text in details)


@pytest.mark.parametrize('field,value,code', [('Acct',None,'SINOPAC-05-374-001'),('AcctValue',None,'SINOPAC-05-375-001'),('Curr',None,'SINOPAC-05-376-001'),('BusinessDate',None,'SINOPAC-05-377-001'),('BusinessDate','bad','SINOPAC-05-378-001')])
def test_sinopac_existing_context_field_roles_have_real_js_parent_proof(monkeypatch,field,value,code):
    import json
    from functools import wraps
    import backend.banks.sinopac as current
    from tests.test_sinopac_history_sync import ACCOUNT,LABEL,test_sinopac_collect_uses_native_account_date_and_query_controls as drive
    traces=[];errors=[]
    for module in (_old('sinopac'),current):
        trace=[];method=module.SinopacCrawler._collect_transactions
        @wraps(method)
        def parent(self,page,*args):
            _observe_native_page(page,trace)
            native=page.evaluate
            def evaluate(expression,arg=None):
                if 'Object.fromEntries' in expression:
                    values={'Acct':LABEL,'AcctValue':ACCOUNT,'Curr':'TWD','BusinessDate':'20260831'};values[field]=value
                    setup="const values="+json.dumps(values)+";global.document={getElementById(id){trace.push(['id',id]);return {get value(){trace.push(['value',id]);return values[id]}}}};"
                    result=_node(expression,arg,setup);trace.extend(result['trace']);return result['value']
                return native(expression,arg)
            page.evaluate=evaluate
            try:return method(self,page,*args)
            except RuntimeError as error:errors.append(error);raise
        with monkeypatch.context() as patch:
            patch.setattr(current.SinopacCrawler,'_collect_transactions',parent)
            with pytest.raises(RuntimeError,match='^sinopac-twd-history-account-control$'):
                drive(patch,False,True,1,False,0,0,0,0,0,0,0,0,None)
        traces.append(trace)
    assert traces[0]==traces[1]
    assert str(errors[0])==str(errors[1])
    assert exception_error_id(errors[1],bank='sinopac')==_registered(code,'sinopac')


def test_sinopac_history_visibility_filter_script_is_unchanged():
    import inspect
    import textwrap
    import backend.banks.sinopac as module
    probes=[]
    for source in (_old('sinopac'),module):
        tree=ast.parse(textwrap.dedent(inspect.getsource(source.SinopacCrawler._collect_transactions)))
        probes.append(next(n.value.value for n in ast.walk(tree) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='dom_probe' for t in n.targets)))
    assert probes[0]==probes[1]


def test_sinopac_final_js_recovery_discards_pending_reason():
    import inspect
    import textwrap
    import backend.banks.sinopac as current
    outcomes=[]
    for module in (_old('sinopac'),current):
        tree=ast.parse(textwrap.dedent(inspect.getsource(module.SinopacCrawler._collect_transactions)))
        probe=next(n.value.value for n in ast.walk(tree) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='dom_probe' for t in n.targets))
        arg=next(n.args[0] for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='evaluate' and n.args and isinstance(n.args[0],ast.BinOp) and any(isinstance(v,ast.Name) and v.id=='dom_probe' for v in ast.walk(n.args[0])))
        expression=eval(compile(ast.Expression(arg),'<literal-native-script>','eval'),{'dom_probe':probe})
        setup=SINOPAC_DOM.replace('CONFIG',"{mode:'normal'}").replace("return config.mode==='records'?[{}]:[]","return ++recordReads===2?[{}]:[]").replace('let mutationReads=0,tableReads=0','let mutationReads=0,tableReads=0,recordReads=0')
        outcomes.append(_node(expression,None,setup))
    assert outcomes[0]==outcomes[1]
    assert isinstance(outcomes[1]['value'],dict) and 'errorId' not in outcomes[1]['value']
    assert outcomes[1]['trace'].count(['records'])==4


def test_fubon_optional_card_nonmatches_remain_quiet_with_real_js():
    import backend.banks.fubon as current
    outcomes=[];traces=[]
    for module in (_old('fubon'),current):
        trace=[]
        class Frame:
            name='txnFrame';url='https://ebank.taipeifubon.com.tw/B2C/cgequ/cgequ001/CGEQU001_Home.faces'
            def locator(self,selector):
                trace.append(['locator',selector])
                def evaluate(expression,arg=None,**kwargs):
                    trace.append(['evaluate',expression,arg,kwargs])
                    result=_node('arg => ('+expression+')(null,arg)',arg,"global.document={querySelectorAll(selector){trace.push(['query',selector]);return []}};")
                    trace.extend(result['trace']);return result['value']
                return SimpleNamespace(evaluate=evaluate)
        frame=Frame()
        class Page:
            frames=[frame];url=frame.url
            def goto(self,*args,**kwargs):trace.append(['goto',args,kwargs])
            def wait_for_timeout(self,ms):trace.append(['wait',ms])
        crawler=object.__new__(module.FubonCrawler);crawler._collect_attested_twd_history=lambda page:{}
        outcomes.append(crawler.collect(Page(),SimpleNamespace(hits=[])))
        traces.append(trace)
    assert outcomes[0]==outcomes[1]
    assert traces[0]==traces[1]


@pytest.mark.parametrize('stage,action,guard',[('initial','787','fubon-twd-history-controls'),('settled','788','fubon-twd-history-controls-action')])
@pytest.mark.parametrize('fault,reason',[('port',1),('password',7)])
def test_fubon_required_form_parser_fault_operand_survives_parent(monkeypatch,stage,action,guard,fault,reason):
    marker='account.value=args.value' if stage=='initial' else 'preset:preset.checked'
    _fubon_window_js_case(monkeypatch,marker,{},guard,f'FUBON-05-{action}-{reason:03}',parse_fault=fault)


def test_fubon_static_route_roles_do_not_allocate_impossible_query_branches():
    table=next(ast.literal_eval(n) for n in ast.walk(_functions('fubon','_fubon_content_frame')[0]) if isinstance(n,ast.Dict) and any(isinstance(k,ast.Constant) and k.value=='menu' for k in n.keys))
    assert table['menu'][5] is None
    assert table['inventory'][4] is None
    assert table['publication'][4] is None


def test_sinopac_optional_retry_has_no_required_caller_role_allocations():
    node=_functions('sinopac','_ocr_captcha')[0]
    retry=next(n for n in ast.walk(node) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='_captcha_image' and any(k.arg=='enabled' for k in n.keywords))
    assert [k.arg for k in retry.keywords]==['enabled']


def _functions(bank, name):
    tree = ast.parse((ROOT / f"backend/banks/{bank}.py").read_text())
    return [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name]


def _compile_function(node, **globals_):
    node.decorator_list = []
    namespace = {"error_step": error_step, **globals_}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<offline-native-helper>", "exec"), namespace)
    return namespace[node.name]


@pytest.mark.parametrize("inner", [False, True])
def test_fubon_bounded_helpers_use_closed_literals_and_keep_native_calls(inner):
    node = _functions("fubon", "bounded_evaluate")[int(inner)]
    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "rpartition" for n in ast.walk(node)), "derived action IDs must be closed literals"
    literal_tables = [ast.literal_eval(n) for n in ast.walk(node) if isinstance(n, ast.Dict)]
    table = next(t for t in literal_tables if "FUBON-06-214-001" in t) if inner else next(t for t in literal_tables if "FUBON-05-243-001" in t)
    fn = _compile_function(node)
    for key, ids in table.items():
        action, reason = key.rsplit("-", 1)
        assert ids == (f"{action}-{int(reason)+100:03}", f"{action}-{int(reason)+200:03}")
        for fail in (None, "locator", "evaluate"):
            trace = []
            original = RuntimeError("synthetic-native-failure")
            def evaluate(*args, **kwargs):
                trace.append(("evaluate", args, kwargs))
                if fail == "evaluate":
                    raise original
                return False
            def locator(*args):
                trace.append(("locator", args))
                if fail == "locator":
                    raise original
                return SimpleNamespace(evaluate=evaluate)
            if fail:
                with pytest.raises(RuntimeError) as caught:
                    fn(SimpleNamespace(locator=locator), "x => x", {"x": 1}, _error_id=key)
                assert caught.value is original
                assert exception_error_id(original, bank="fubon") == _registered(ids[int(fail == "evaluate")], 'fubon')
            else:
                assert fn(SimpleNamespace(locator=locator), "x => x", {"x": 1}, _error_id=key) is False
            assert trace == [("locator", ("html",))] + ([] if fail == "locator" else [("evaluate", ("(root, arg) => (x => x)(arg)", {"x": 1}), {"timeout": 5000})])
    call = next(n for n in ast.walk(node) if isinstance(n, ast.Return)).value
    assert (isinstance(call.func, ast.Attribute) and call.func.attr == "evaluate") is inner


def test_sinopac_date_ids_are_closed_existing_role_tuples():
    from backend.banks.sinopac import SinopacCrawler
    node = _functions("sinopac", "_yyyymmdd")[0]
    assert not any(isinstance(n, ast.JoinedStr) for n in ast.walk(node)), "date roles must use literal ID tuples"
    roles = ["SINOPAC-07-150", "SINOPAC-04-151", "SINOPAC-04-152", "SINOPAC-04-153", "SINOPAC-05-151", "SINOPAC-05-152", "SINOPAC-07-151", "SINOPAC-07-152", "SINOPAC-07-153", "SINOPAC-07-154", "SINOPAC-07-155"]
    for role in roles:
        assert SinopacCrawler._yyyymmdd("20260831", "unchanged", error_action=role) == date(2026, 8, 31)
        for value, suffix in ((None, "001"), ("bad", "002"), ("20260230", "003")):
            with pytest.raises(RuntimeError, match="^unchanged$") as caught:
                SinopacCrawler._yyyymmdd(value, "unchanged", error_action=role)
            assert exception_error_id(caught.value, bank="sinopac") == _registered(role + "-" + suffix, 'sinopac')
