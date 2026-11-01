"""Offline joined terminal proofs: native Python callers and Node fake DOM only."""
import ast
import inspect
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from backend.banks import esun
from backend.banks.esun_spa import query
from tests.test_esun_terminal_residual_codes import NODE_LEGACY


def legacy_script(needle):
    # Synthetic block preserves embedded JavaScript whitespace byte for byte.
    tree = ast.parse('if True:\n' + inspect.getsource(esun.EsunCrawler.collect))
    return next(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                and type(n.value) is str and needle in n.value)


def legacy_marker_proof(source, family, fault):
    observed = json.loads(subprocess.run(['node', '-e', NODE_LEGACY],
        input=json.dumps([source, family, fault]), text=True, capture_output=True,
        check=True, timeout=10).stdout)
    expected = 0 if family == 'submit' and fault == 0 else fault
    marker = observed['result'][1] if type(observed['result']) is list else 0
    assert marker == expected, ('producer marker', family, fault, observed)
    return observed


def test_legacy_period_marker_swap_is_rejected():
    source = legacy_script('no intervalrdo4')
    mutant = source.replace('period.start ? 3 : result.end !== period.end ? 4',
                            'period.start ? 4 : result.end !== period.end ? 3')
    assert mutant != source
    with pytest.raises(AssertionError, match='producer marker'):
        legacy_marker_proof(mutant, 'period', 3)


def actual_run(crawler, page, collector):
    """Only browser/login/session I/O is substituted; collect and run are real."""
    crawler.name = 'esun'
    crawler._enforce_session_freshness = lambda: None
    crawler._build_fetch_kwargs = lambda: {}
    crawler._make_collector = lambda _: collector
    crawler._shared_login = getattr(page, 'synthetic_login', lambda _: True)
    crawler._credential_origin_allowed = lambda _: False
    crawler._ensure_collect_origin = lambda _: None
    crawler.attach_shared_dialog_handler = lambda _: None
    from backend.core import base
    fetch_calls=[]
    crawler.session_dir=Path('synthetic-session-no-io')
    def fake_fetch(url,**kwargs):
        recorded={key:('real page_action' if key=='page_action' else value) for key,value in kwargs.items()}
        assert recorded=={'headless':False,'network_idle':False,'load_dom':True,'wait':2000,
            'timeout':180000,'user_data_dir':'synthetic-session-no-io','page_action':'real page_action',
            'google_search':True,'hide_canvas':True,'block_webrtc':True,'dns_over_https':True}
        assert url=='https://synthetic.invalid/'
        fetch_calls.append((url,recorded))
        return kwargs['page_action'](page)
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(base,'StealthyFetcher',SimpleNamespace(fetch=fake_fetch))
        result=crawler.run('https://synthetic.invalid/')
    assert len(fetch_calls)==1
    crawler._joined_fetch_calls=fetch_calls
    return result


def assert_terminal(result, expected, chinese):
    assert result['collect_diagnostics']['error_id'] == expected
    assert result['error'].startswith('[' + expected + '] ' + chinese + ' | ')
    assert 'data' not in result and 'failure_diagnostics' not in result
    assert 'synthetic' not in result['error']


def test_actual_legacy_collect_run_rejects_marker_swap(monkeypatch):
    from tests.test_esun_terminal_residual_codes import legacy_surface
    # Existing fixture must now expose context, not replace collect with a raiser.
    crawler, page, collector, calls = legacy_surface(monkeypatch, ('selected', 1), context=True)
    result = actual_run(crawler, page, collector)
    assert_terminal(result, 'ESUN-05-867-001', '傳統臺幣查詢：帳戶選單不存在')


NODE_BOUND = r'''
const fs=require('fs'),vm=require('vm');
const [source,expected,missing]=JSON.parse(fs.readFileSync(0,'utf8'));
const trace=[];
const wrap=(name,o)=>new Proxy(o,{get(o,k){trace.push(name+'.'+String(k));return o[k]}});
const cells=['2026/08/20','12:00:00','利息','','2','84','活存利息'].map((text,i)=>node('cell'+i,text));
function node(name,text,children=[]) {
 return wrap(name,{textContent:text,children,parentElement:null,hidden:false,tagName:'DIV',id:'',className:'',
  offsetParent:{},outerHTML:'<div>synthetic</div>',
  getBoundingClientRect(){trace.push(name+'.rect');return {width:10,height:10}},
  getAttribute(k){trace.push(name+'.attr:'+k);return null},
  hasAttribute(k){trace.push(name+'.has:'+k);return false},
  querySelectorAll(s){trace.push(name+'.query:'+s);
   if(name==='row'||name==='copyrow')return cells;
   if(name==='grid'&&s==='tr')return [row];
   if(name==='scope'&&s.includes('grid'))return [grid];
   if(name==='copy'&&s.includes('grid'))return [copyrow];
   if(s==='*')return children;
   return [];
  },cloneNode(deep){trace.push(['clone',name,deep]);return copy},
  remove(){trace.push(name+'.remove')}
 });
}
const row=node('row','2026/08/20 12:00:00 利息 2 84 活存利息',cells);
const copyrow=node('copyrow','2026/08/20',cells);
const grid=node('grid','交易日期 時間 摘要 提 存 帳戶餘額\n2026/08/20 12:00:00 利息 2 84 活存利息',[row]);
const title='帳號 '+expected.identity+' 查詢期間 '+expected.start+' 至 '+expected.end+' 交易明細 共1筆';
const scope=node('scope',title,[grid]),copy=node('copy',title);
const root=node('html','');
const document={documentElement:root,querySelectorAll(s){trace.push('document.query:'+s);
 if(s==='*')return [scope,grid,row,...cells];
 if(s.includes('qry'))return missing?[]:[scope];
 if(s==='table')return [grid];
 throw Error('unexpected document selector:'+s);
}};
const window={getComputedStyle(n){trace.push('style');return {display:'block',visibility:'visible',opacity:'1'}}};
const deny=(...a)=>{trace.push(['external',a]);throw Error('offline external')};
const result=vm.runInNewContext('('+source+')',{document,window,location:{href:'https://ebank.esunbank.com.tw/fco/fao01002/FAO01002.faces'},fetch:deny,setTimeout:deny,XMLHttpRequest:deny,WebSocket:deny})(expected);
console.log(JSON.stringify({result,trace}));
'''


def bound_observation(source, expected, missing=False):
    return json.loads(subprocess.run(['node', '-e', NODE_BOUND],
        input=json.dumps([source, expected, missing]), text=True,
        capture_output=True, check=True, timeout=10).stdout)


def frozen_bank():
    """The complete original class AND its module helpers, not an old method
    grafted onto a current class. No constructor, credential or browser call."""
    module=frozen_module('backend/banks/esun.py','frozen_esun_joined')
    module._log = lambda *a: None
    return module


def frozen_module(relative,name):
    from types import ModuleType
    root = Path(__file__).parents[1]
    source = subprocess.check_output(['git', 'show', '16f2d4b:'+relative], cwd=root, text=True)
    module = ModuleType(name)
    module.__file__ = str(root / relative)
    exec(compile(source, '<16f2d4b-whole-'+relative+'>', 'exec'), module.__dict__)
    return module


def legacy_complete_surface(monkeypatch, module=esun, missing=False, control_fault=None):
    from urllib.parse import urlencode
    from backend.core import store
    class NoDisk:
        def __truediv__(self, _): return self
        def mkdir(self, **kw): pass
    monkeypatch.setattr(store, '_data_root', lambda: NoDisk())
    monkeypatch.setattr(module, '_log', lambda *a: None)
    trace=[]
    account='0000000000001'
    option={'index':0,'value':'synthetic','text':'臺幣綜存 '+account}
    overview='臺幣綜存\n'+account+'\n84\n'
    class Frame:
        url='https://ebank.esunbank.com.tw'+module._ESUN_TWD_HISTORY_PATH
        def locator(self, selector):
            trace.append(('locator',selector))
            return SimpleNamespace(select_option=lambda **kw: trace.append(('select',kw)))
        def evaluate(self, script, arg=None):
            if 'scopes.length !== 1' in script:
                out=bound_observation(script,arg,missing)
                trace.append(('binding',out['trace']))
                return out['result']
            for family,needle in [('selected','s?.options[s.selectedIndex]'),('period','no intervalrdo4'),
                                  ('sort','fao01002:j_id_sort1'),('submit',"clicked: 'visible-query'")]:
                if needle not in script: continue
                harness=NODE_LEGACY.replace('[source,family,fault]', '[source,family,fault,arg]')
                harness=harness.replace("({start:'2026/01/01',end:'2026/02/01'})", '(arg)')
                if family=='selected':
                    harness=harness.replace("textContent:'synthetic'", "textContent:"+json.dumps(option['text']))
                fault=control_fault[1] if control_fault and control_fault[0]==family else 0
                out=json.loads(subprocess.run(['node','-e',harness],input=json.dumps([script,family,fault,arg]),
                    text=True,capture_output=True,check=True,timeout=10).stdout)
                trace.append((family,out['trace']))
                if family=='submit' and 'click' in out['trace']:
                    request=SimpleNamespace(method='POST',post_data=urlencode({'fao01002:dract':option['value'],
                        'fao01002:startDate':page.period['start'],'fao01002:endDate':page.period['end']}))
                    response=SimpleNamespace(request=request,url=self.url,status=200,headers={'content-type':'text/html'})
                    trace.append(('request',request.method,request.post_data))
                    page.listeners['response'](response)
                if family=='period': page.period=arg
                return out['result']
            if '#sysInfo' in script: trace.append('bank-date'); return '2026/09/01'
            if 's.options].map' in script: trace.append('inventory'); return [dict(option)]
            if 'linkCommand' in script: trace.append('form-detection'); return True
            if 'data-hermes-stale-evidence' in script: trace.append('stale-mark'); return {'ok':True,'marked':0}
            if 'iframe' in script: trace.append('iframe-read'); return []
            if 'document.body' in script: trace.append('body-read'); return overview
            raise AssertionError('unexpected legacy script')
    class Page:
        url='https://ebank.esunbank.com.tw/'
        main_frame=object()
        frames=[Frame()]
        listeners={}
        def evaluate(self, script):
            trace.append(('main-evaluate',script))
            if '#layout-content' in script:return False
            return overview
        def wait_for_timeout(self, ms):trace.append(('wait',ms))
        def on(self,event,listener):trace.append(('on',event));self.listeners[event]=listener
        def remove_listener(self,event,listener):trace.append(('remove',event));self.listeners.pop(event)
    page=Page()
    crawler=object.__new__(module.EsunCrawler)
    crawler.transaction_cursors={}
    def navigate(page,label,*args):
        trace.append(('menu',label))
        return {'frames':[{'result':{'clicked':label=='存款交易明細查詢'}}]}
    crawler._navigate_menu=navigate
    crawler._navigate_credit_card_bill=lambda *a: {'frames':[]}
    collector=SimpleNamespace(hits=[],attach=lambda _:None,detach=lambda _:None)
    return crawler,page,collector,trace


def test_actual_binding_zero_scope_reaches_actual_run(monkeypatch):
    crawler,page,collector,trace=legacy_complete_surface(monkeypatch,missing=True)
    result=actual_run(crawler,page,collector)
    assert_terminal(result,'ESUN-05-867-010','傳統臺幣查詢結果綁定：符合帳戶與期間的結果範圍不唯一')
    assert any(type(x) is tuple and x[0]=='binding' for x in trace)


def test_positive_whole_frozen_legacy_collect_and_binding_native_trace(monkeypatch):
    import sys
    monkeypatch.setenv('BANK_CRAWLER_HISTORY_MODE','full')
    traces=[]
    for module in (frozen_bank(),esun):
        with monkeypatch.context() as patcher:
            if module is not esun:
                original_parser=frozen_module('backend/core/persist/esun.py','frozen_esun_parser')
                patcher.setitem(sys.modules,'backend.core.persist.esun',original_parser)
            crawler,page,collector,trace=legacy_complete_surface(patcher,module)
            result=crawler.collect(page,collector)
        assert result.history_coverage['domains'][0]['windows'][0]['status']=='complete'
        assert result.accounts[0]['account_no']=='0000000000001'
        assert result.twd_txn_results[0]['snapshot']['gridRowCount']==1
        traces.append((result.to_dict(),trace))
    assert traces[0]==traces[1]


@pytest.mark.parametrize('fault,code,chinese',[
    (('selected',1),'ESUN-05-867-001','傳統臺幣查詢：帳戶選單不存在'),
    (('selected',2),'ESUN-05-867-002','傳統臺幣查詢：帳戶選項不存在'),
    (('period',1),'ESUN-05-867-003','傳統臺幣查詢：自訂期間控制不存在'),
    (('period',2),'ESUN-05-867-004','傳統臺幣查詢：自訂期間未勾選'),
    (('period',3),'ESUN-05-867-005','傳統臺幣查詢：起日未回寫'),
    (('period',4),'ESUN-05-867-006','傳統臺幣查詢：迄日未回寫'),
    (('sort',1),'ESUN-05-867-007','傳統臺幣查詢：排序控制不存在'),
    (('sort',2),'ESUN-05-867-008','傳統臺幣查詢：排序控制未勾選'),
    (('submit',1),'ESUN-05-867-009','傳統臺幣查詢：查詢按鈕不存在')],
    ids=['selected-missing','option-missing','period-missing','period-unchecked','period-start','period-end','sort-missing','sort-unchecked','submit-missing'])
def test_legacy_actual_js_marker_collect_run_join(monkeypatch,fault,code,chinese):
    crawler,page,collector,trace=legacy_complete_surface(monkeypatch,control_fault=fault)
    assert_terminal(actual_run(crawler,page,collector),code,chinese)
    assert not any(x[0]=='request' for x in trace if type(x) is tuple)


def spa_surface(monkeypatch, *, safe_fault=None, destination_fault=None, wrong_date=None,
                two_accounts=False, timeline_fault=None, calendar_state=False, native_observation=None,
                cursor_day='2026-09-08', positive=False):
    """Native guards, plan, capture, admission and query remain production code.
    Only DOM/CDP/request events and preauthenticated setup are synthetic."""
    from datetime import date
    import itertools
    from backend.banks.esun_spa import capture, collection, navigation
    from tests.test_esun_spa_capture import Page as CapturePage, issue
    from tests.test_ordered_esun_ubot_codes import QueryLocator
    trace=[]
    accounts=[{'accountNo':'0000000000001','accountAlias':'synthetic'}]
    if two_accounts:accounts.append({'accountNo':'0000000000002','accountAlias':'second'})
    menu=[{'itemName':{'zh-TW':'synthetic-root'},'show':True,'enable':True,'taskId':None,
        'childMenuList':[{'itemName':{'zh-TW':'synthetic-leaf'},'show':True,'enable':True,
                         'taskId':'CTW01002-TW','childMenuList':None}]}]
    ticks=itertools.count()
    monkeypatch.setattr(collection,'monotonic',lambda:next(ticks)/100)
    monkeypatch.setattr(query.time,'monotonic',lambda:next(ticks)/100)
    monkeypatch.setenv('BANK_CRAWLER_HISTORY_MODE','incremental')
    collector=capture.SpaCollector()
    crawler=object.__new__(esun.EsunCrawler)
    crawler.transaction_cursors={'twd_transactions':{a['accountNo']:date.fromisoformat(cursor_day) for a in accounts}}
    crawler._logged_in=lambda _:True  # Auth is outside this finite terminal audit.
    def request(path,body,request,*,finish=True):
        trace.append(('request',path,request))
        issue(capture,collector,page,path=path,raw=json.dumps({'resultCode':'0000','resultBody':body}),
              post_data=json.dumps({'requestBody':request}),headers={'content-type':'application/json'},finish=finish)
    def body():
        a=accounts[page.selected]
        return {'demandDeptAcc':a['accountNo'],'accountAlias':a['accountAlias'],'twAccountList':accounts,
                'twCurrInfo':{'curr':'TWD'},'queryDeptTxDtlResult':{'detailListData':[]}}
    class Locator(QueryLocator):
        def observation(self,op,value):
            if native_observation:return native_observation(op,self,value)
            return value
        def locator(self,s):return Locator(self.page,self.selector+' '+s)
        def filter(self,has=None,**kw):
            if has is not None:self.selector+=' '+has.selector
            return self
        def nth(self,i):return Locator(self.page,self.selector+' nth='+str(i))
        def count(self):
            return self.observation('count',self.native_count())
        def is_visible(self):return self.observation('visible',True)
        def native_count(self):
            s=self.selector
            if calendar_state:
                if 'month-item' in s:return 1 if 'nth=' in s else 12
                if s.endswith('.months-block'):return int(page.calendar_mode=='months')
                if s.endswith('.days-block'):return int(page.calendar_mode=='days')
            if 'combo-modal' in s:return int(page.account_open)
            if s==navigation.MENU:return int(page.menu_open)
            if 'not-login' in s:return 0
            if s.endswith('input[name=startDate][readonly]') or s.endswith('input[name=endDate][readonly]'):return 1
            return super().count()
        def wait_for(self,**kw):trace.append(('wait_for',self.selector,kw))
        def is_checked(self):return page.radio_checked
        def evaluate_handle(self,script):
            trace.append(('handle',self.selector))
            return SimpleNamespace(evaluate=lambda s:trace.append(('handle-evaluate',s)) or True,
                                   dispose=lambda:trace.append('dispose'))
        def evaluate_all(self,script,*args):
            trace.append(('evaluate_all',self.selector))
            if script==collection.BLOCKER_SNAPSHOT:return [True,'unknown','none']
            return True
        def evaluate(self,script,*args):
            trace.append(('evaluate',self.selector,'SAFE' if script in (query.SAFE,query.SAFE_REASON)
                          else 'SETTLE' if 'getAnimations' in script else script))
            if script in (query.SAFE,query.SAFE_REASON) and safe_fault:
                result=safe_fault(self,script)
                if result is not None:return result[0] if script==query.SAFE and type(result) is list else result
            return self.observation('settle' if 'getAnimations' in script else 'evaluate',True)
        def input_value(self):
            s=self.selector
            if 'accountList' in s or 'input[readonly]' in s:
                a=accounts[page.selected];return a['accountNo']+' '+a['accountAlias']
            field='startDate' if 'startDate' in s else 'endDate'
            if page.fail_date_read:raise TimeoutError('synthetic independent owner read')
            trace.append(('date',page.selected,field,page.values[field]))
            if wrong_date and wrong_date(page,field):return 'wrong'
            return page.values[field]
        def inner_text(self):
            value=self.observation('text',self.native_text())
            if 'header-label' in self.selector:page.rendered_title=value
            return value
        def get_attribute(self,name):
            if 'header-label' in self.selector:return page.rendered_title
            return super().get_attribute(name)
        def native_text(self):
            if 'header-label' in self.selector:
                return str(page.year) if page.calendar_mode=='months' else str(page.year)+'年'+str(page.month).zfill(2)+'月'
            if 'day-cell' in self.selector:
                import re
                return str(int(re.search(r'2026年\d{2}月(\d+)日',self.selector)[1]))
            return super().inner_text()
        def all_text_contents(self):
            if 'month-item' in self.selector:return [str(i) for i in range(12)]
            return [a['accountNo']+' '+a['accountAlias'] for a in accounts]
        def click(self,**kw):
            s=self.selector
            trace.append(('click',s,kw))
            if page.native_failure and page.native_failure in s:raise TimeoutError('synthetic unrelated native failure')
            if s==navigation.HEADER:page.menu_open=True;return
            if '.level2-link' in s:
                page.menu_open=False;request(capture.PATHS[1],body(),{'account':None});return
            if 'li[role="button"]' in s:
                page.selected=int(s.rsplit('nth=',1)[1]);page.account_open=False
                page.values={'startDate':'2026/09/01','endDate':'2026/09/08'}
                request(capture.PATHS[1],body(),{'account':accounts[page.selected]['accountNo']});return
            if 'accountList' in s:
                page.account_open=True;return
            if 'input[type="radio"]' in s:page.radio_checked=True;return
            if 'header-label' in s:page.calendar_mode='months';return
            if 'next-icon' in s:page.year+=1;return
            if 'prev-icon' in s:page.year-=1;return
            if 'month-item' in s:
                page.month=int(s.rsplit('nth=',1)[1])+1;page.calendar_mode='days';return
            if 'button[type="submit"]' in s:
                page.submissions.append(page.selected)
                detail={'displayErrorCode':'2003','displayErrorMsg':collection.EMPTY_MESSAGE,'detailListData':[],
                        'startIndex':1,'count':100}
                if positive:detail.update(displayErrorCode=None,displayErrorMsg=None,
                    detailListData=[{'year':'2026','month':'9','detailInfo':[{'debitCredit':'CR','detailTitle':'synthetic',
                        'txDate':'2026/09/01','txTime':'12:00:00','amount':2,'balance':84}]}])
                request(capture.PATHS[2],{'queryDeptTxDtlResult':detail,'recentHashtag':[]},
                    {'account':accounts[page.selected]['accountNo'],'startDate':page.values['startDate'],
                     'endDate':page.values['endDate'],'startIndex':1,'count':100,'customerInputHashtag':[]})
                return
            field='startDate' if 'startDate' in s else 'endDate'
            if 'day-cell' in s:
                page.values[field]=str(page.year)+'/'+str(page.month).zfill(2)+'/'+self.inner_text().zfill(2);page.opened=False
            else:
                page.opened=True;page.calendar_mode='days'
                page.year,page.month=(2025,8) if calendar_state else (2026,9)
    class Page(CapturePage):
        url='https://ebank.esunbank.com.tw/home'
        opened=False
        selected=0
        account_open=False
        menu_open=False
        calendar_mode='days'
        year,month=2026,9
        native_failure=None
        make_prequery_unavailable=False
        fail_date_read=False
        def __init__(self):
            super().__init__();self.values={'startDate':'2026/09/01','endDate':'2026/09/08'}
            self.submissions=[];self.safe_counts={}
            self.radio_checked=not calendar_state
        def synthetic_login(self,_):
            crawler._spa_login_baseline=collector.snapshot()
            request(capture.PATHS[0],{'menuList':menu},{'locale':'zh-TW'})
            return True
        def locator(self,s):return Locator(self,s)
        def evaluate_handle(self,script):
            trace.append(('document-handle',script))
            return SimpleNamespace(evaluate=lambda s,*args:self.evaluate(s,*args),dispose=lambda:trace.append('document-dispose'))
        def evaluate(self,script,*args):
            if script==collection.RENDERED_OCCURRENCES or collection.RENDERED_OCCURRENCES in script:
                return [['2026','09',['synthetic']]]
            if script in (collection.DESTINATION,collection.DESTINATION_REASON):
                trace.append(('destination',self.selected,crawler._esun_spa_phase))
                if destination_fault:
                    value=destination_fault(self,crawler)
                    if value is not None:return value[0] if script==collection.DESTINATION else value
                return True if script==collection.DESTINATION else [True,0]
            if script in (collection.TIMELINE,collection.TIMELINE_REASON):
                trace.append(('timeline',self.selected,crawler._esun_spa_phase))
                if timeline_fault:return timeline_fault(self,crawler)
                return 'synthetic timeline' if script==collection.TIMELINE else ['synthetic timeline',0]
            if script==collection.EMPTY_CARD:return '(2003)'+collection.EMPTY_MESSAGE
            if '#layout-content' in script:return True
            if 'new Date();' in script:return [2026,9,8]
            if 'ds.map' in script:return [self.values['startDate'],self.values['endDate']]
            if 'innerWidth' in script:return 1200
            if script=='document.body.innerText':return '\n'.join(a['accountNo']+' 臺幣綜存\n84\n' for a in accounts)
            if '(label) =>' in script:return False
            raise AssertionError('unexpected SPA script')
        def wait_for_timeout(self,ms):trace.append(('wait',ms))
        def wait_for_function(self,*args,**kw):
            trace.append(('frame',kw))
            if self.make_prequery_unavailable:
                self.make_prequery_unavailable=False
                request(capture.PATHS[1],body(),{'account':None},finish=False)
    page=Page()
    return crawler,page,collector,trace


@pytest.mark.parametrize('field,code,chinese',[
    ('startDate','ESUN-05-863-205','開始日期：查詢元件：查詢元件驗證：祖先已隱藏'),
    ('endDate','ESUN-05-863-211','結束日期：查詢元件：查詢元件驗證：祖先已隱藏')])
def test_safe_820_actual_js_actual_collect_actual_run(monkeypatch,field,code,chinese):
    from tests.test_esun_terminal_residual_codes import node_observation
    observed=[]
    def fault(target,script):
        if target.selector.endswith('input[name='+field+'][readonly]'):
            out=node_observation(script,'safe',1)
            observed.append(out)
            return out['result']
    crawler,page,collector,trace=spa_surface(monkeypatch,safe_fault=fault)
    result=actual_run(crawler,page,collector)
    assert_terminal(result,code,chinese)
    assert len(observed)==1 and observed[0]['result']==[False,1]
    assert page.submissions==[]


# Authoring-time literal oracle, independent of production runtime tables.
SAFE_LITERAL = {
    'ESUN-05-850-008': ('ESUN-05-863-217', 'ESUN-05-863-218', 'ESUN-05-863-219', 'ESUN-05-863-220', 'ESUN-05-863-221', 'ESUN-05-863-222'),
    'ESUN-05-851-008': ('ESUN-05-863-223', 'ESUN-05-863-224', 'ESUN-05-863-225', 'ESUN-05-863-226', 'ESUN-05-863-227', 'ESUN-05-863-228'),
    'ESUN-05-784-008': ('ESUN-05-863-001', 'ESUN-05-863-002', 'ESUN-05-863-003', 'ESUN-05-863-004', 'ESUN-05-863-005', 'ESUN-05-863-006'),
    'ESUN-05-785-008': ('ESUN-05-863-007', 'ESUN-05-863-008', 'ESUN-05-863-009', 'ESUN-05-863-010', 'ESUN-05-863-011', 'ESUN-05-863-012'),
    'ESUN-05-812-008': ('ESUN-05-863-013', 'ESUN-05-863-014', 'ESUN-05-863-015', 'ESUN-05-863-016', 'ESUN-05-863-017', 'ESUN-05-863-018'),
    'ESUN-05-812-508': ('ESUN-05-863-019', 'ESUN-05-863-020', 'ESUN-05-863-021', 'ESUN-05-863-022', 'ESUN-05-863-023', 'ESUN-05-863-024'),
    'ESUN-05-579-003': ('ESUN-05-863-025', 'ESUN-05-863-026', 'ESUN-05-863-027', 'ESUN-05-863-028', 'ESUN-05-863-029', 'ESUN-05-863-030'),
    'ESUN-05-581-003': ('ESUN-05-863-031', 'ESUN-05-863-032', 'ESUN-05-863-033', 'ESUN-05-863-034', 'ESUN-05-863-035', 'ESUN-05-863-036'),
    'ESUN-05-783-008': ('ESUN-05-863-037', 'ESUN-05-863-038', 'ESUN-05-863-039', 'ESUN-05-863-040', 'ESUN-05-863-041', 'ESUN-05-863-042'),
    'ESUN-05-801-008': ('ESUN-05-863-043', 'ESUN-05-863-044', 'ESUN-05-863-045', 'ESUN-05-863-046', 'ESUN-05-863-047', 'ESUN-05-863-048'),
    'ESUN-05-797-008': ('ESUN-05-863-049', 'ESUN-05-863-050', 'ESUN-05-863-051', 'ESUN-05-863-052', 'ESUN-05-863-053', 'ESUN-05-863-054'),
    'ESUN-05-797-508': ('ESUN-05-863-055', 'ESUN-05-863-056', 'ESUN-05-863-057', 'ESUN-05-863-058', 'ESUN-05-863-059', 'ESUN-05-863-060'),
    'ESUN-05-798-008': ('ESUN-05-863-061', 'ESUN-05-863-062', 'ESUN-05-863-063', 'ESUN-05-863-064', 'ESUN-05-863-065', 'ESUN-05-863-066'),
    'ESUN-05-798-508': ('ESUN-05-863-067', 'ESUN-05-863-068', 'ESUN-05-863-069', 'ESUN-05-863-070', 'ESUN-05-863-071', 'ESUN-05-863-072'),
    'ESUN-05-799-008': ('ESUN-05-863-073', 'ESUN-05-863-074', 'ESUN-05-863-075', 'ESUN-05-863-076', 'ESUN-05-863-077', 'ESUN-05-863-078'),
    'ESUN-05-799-508': ('ESUN-05-863-079', 'ESUN-05-863-080', 'ESUN-05-863-081', 'ESUN-05-863-082', 'ESUN-05-863-083', 'ESUN-05-863-084'),
    'ESUN-05-786-008': ('ESUN-05-863-085', 'ESUN-05-863-086', 'ESUN-05-863-087', 'ESUN-05-863-088', 'ESUN-05-863-089', 'ESUN-05-863-090'),
    'ESUN-05-813-008': ('ESUN-05-863-091', 'ESUN-05-863-092', 'ESUN-05-863-093', 'ESUN-05-863-094', 'ESUN-05-863-095', 'ESUN-05-863-096'),
    'ESUN-05-814-008': ('ESUN-05-863-097', 'ESUN-05-863-098', 'ESUN-05-863-099', 'ESUN-05-863-100', 'ESUN-05-863-101', 'ESUN-05-863-102'),
    'ESUN-05-816-008': ('ESUN-05-863-103', 'ESUN-05-863-104', 'ESUN-05-863-105', 'ESUN-05-863-106', 'ESUN-05-863-107', 'ESUN-05-863-108'),
    'ESUN-05-817-008': ('ESUN-05-863-109', 'ESUN-05-863-110', 'ESUN-05-863-111', 'ESUN-05-863-112', 'ESUN-05-863-113', 'ESUN-05-863-114'),
    'ESUN-05-787-008': ('ESUN-05-863-115', 'ESUN-05-863-116', 'ESUN-05-863-117', 'ESUN-05-863-118', 'ESUN-05-863-119', 'ESUN-05-863-120'),
    'ESUN-05-788-008': ('ESUN-05-863-121', 'ESUN-05-863-122', 'ESUN-05-863-123', 'ESUN-05-863-124', 'ESUN-05-863-125', 'ESUN-05-863-126'),
    'ESUN-05-789-008': ('ESUN-05-863-127', 'ESUN-05-863-128', 'ESUN-05-863-129', 'ESUN-05-863-130', 'ESUN-05-863-131', 'ESUN-05-863-132'),
    'ESUN-05-790-008': ('ESUN-05-863-133', 'ESUN-05-863-134', 'ESUN-05-863-135', 'ESUN-05-863-136', 'ESUN-05-863-137', 'ESUN-05-863-138'),
    'ESUN-05-791-008': ('ESUN-05-863-139', 'ESUN-05-863-140', 'ESUN-05-863-141', 'ESUN-05-863-142', 'ESUN-05-863-143', 'ESUN-05-863-144'),
    'ESUN-05-792-008': ('ESUN-05-863-145', 'ESUN-05-863-146', 'ESUN-05-863-147', 'ESUN-05-863-148', 'ESUN-05-863-149', 'ESUN-05-863-150'),
    'ESUN-05-793-008': ('ESUN-05-863-151', 'ESUN-05-863-152', 'ESUN-05-863-153', 'ESUN-05-863-154', 'ESUN-05-863-155', 'ESUN-05-863-156'),
    'ESUN-05-794-008': ('ESUN-05-863-157', 'ESUN-05-863-158', 'ESUN-05-863-159', 'ESUN-05-863-160', 'ESUN-05-863-161', 'ESUN-05-863-162'),
    'ESUN-05-795-008': ('ESUN-05-863-163', 'ESUN-05-863-164', 'ESUN-05-863-165', 'ESUN-05-863-166', 'ESUN-05-863-167', 'ESUN-05-863-168'),
    'ESUN-05-796-008': ('ESUN-05-863-169', 'ESUN-05-863-170', 'ESUN-05-863-171', 'ESUN-05-863-172', 'ESUN-05-863-173', 'ESUN-05-863-174'),
    'ESUN-05-818-008': ('ESUN-05-863-175', 'ESUN-05-863-176', 'ESUN-05-863-177', 'ESUN-05-863-178', 'ESUN-05-863-179', 'ESUN-05-863-180'),
    'ESUN-05-815-008': ('ESUN-05-863-181', 'ESUN-05-863-182', 'ESUN-05-863-183', 'ESUN-05-863-184', 'ESUN-05-863-185', 'ESUN-05-863-186'),
    'ESUN-05-815-508': ('ESUN-05-863-187', 'ESUN-05-863-188', 'ESUN-05-863-189', 'ESUN-05-863-190', 'ESUN-05-863-191', 'ESUN-05-863-192'),
    'ESUN-05-819-008': ('ESUN-05-863-193', 'ESUN-05-863-194', 'ESUN-05-863-195', 'ESUN-05-863-196', 'ESUN-05-863-197', 'ESUN-05-863-198'),
    'ESUN-05-819-508': ('ESUN-05-863-199', 'ESUN-05-863-200', 'ESUN-05-863-201', 'ESUN-05-863-202', 'ESUN-05-863-203', 'ESUN-05-863-204'),
    'ESUN-05-820-008': ('ESUN-05-863-205', 'ESUN-05-863-206', 'ESUN-05-863-207', 'ESUN-05-863-208', 'ESUN-05-863-209', 'ESUN-05-863-210'),
    'ESUN-05-820-508': ('ESUN-05-863-211', 'ESUN-05-863-212', 'ESUN-05-863-213', 'ESUN-05-863-214', 'ESUN-05-863-215', 'ESUN-05-863-216'),
}
DESTINATION_LITERAL = {
    'prequery': ('ESUN-05-865-001', 'ESUN-05-865-002', 'ESUN-05-865-003', 'ESUN-05-865-004', 'ESUN-05-865-005', 'ESUN-05-865-006', 'ESUN-05-865-007', 'ESUN-05-865-008', 'ESUN-05-865-009', 'ESUN-05-865-010', 'ESUN-05-865-011', 'ESUN-05-865-012', 'ESUN-05-865-013', 'ESUN-05-865-014', 'ESUN-05-865-015', 'ESUN-05-865-016', 'ESUN-05-865-017'),
    'initial': ('ESUN-05-865-101', 'ESUN-05-865-102', 'ESUN-05-865-103', 'ESUN-05-865-104', 'ESUN-05-865-105', 'ESUN-05-865-106', 'ESUN-05-865-107', 'ESUN-05-865-108', 'ESUN-05-865-109', 'ESUN-05-865-110', 'ESUN-05-865-111', 'ESUN-05-865-112', 'ESUN-05-865-113', 'ESUN-05-865-114', 'ESUN-05-865-115', 'ESUN-05-865-116', 'ESUN-05-865-117'),
    'query': ('ESUN-05-865-201', 'ESUN-05-865-202', 'ESUN-05-865-203', 'ESUN-05-865-204', 'ESUN-05-865-205', 'ESUN-05-865-206', 'ESUN-05-865-207', 'ESUN-05-865-208', 'ESUN-05-865-209', 'ESUN-05-865-210', 'ESUN-05-865-211', 'ESUN-05-865-212', 'ESUN-05-865-213', 'ESUN-05-865-214', 'ESUN-05-865-215', 'ESUN-05-865-216', 'ESUN-05-865-217'),
    'switch_owner': ('ESUN-05-865-301', 'ESUN-05-865-302', 'ESUN-05-865-303', 'ESUN-05-865-304', 'ESUN-05-865-305', 'ESUN-05-865-306', 'ESUN-05-865-307', 'ESUN-05-865-308', 'ESUN-05-865-309', 'ESUN-05-865-310', 'ESUN-05-865-311', 'ESUN-05-865-312', 'ESUN-05-865-313', 'ESUN-05-865-314', 'ESUN-05-865-315', 'ESUN-05-865-316', 'ESUN-05-865-317'),
    'switch_result': ('ESUN-05-865-401', 'ESUN-05-865-402', 'ESUN-05-865-403', 'ESUN-05-865-404', 'ESUN-05-865-405', 'ESUN-05-865-406', 'ESUN-05-865-407', 'ESUN-05-865-408', 'ESUN-05-865-409', 'ESUN-05-865-410', 'ESUN-05-865-411', 'ESUN-05-865-412', 'ESUN-05-865-413', 'ESUN-05-865-414', 'ESUN-05-865-415', 'ESUN-05-865-416', 'ESUN-05-865-417'),
}

EXPECTED_CHINESE = {
    'ESUN-05-863-001': '查詢根元件：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-002': '查詢根元件：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-003': '查詢根元件：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-004': '查詢根元件：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-005': '查詢根元件：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-006': '查詢根元件：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-007': '查詢表單：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-008': '查詢表單：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-009': '查詢表單：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-010': '查詢表單：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-011': '查詢表單：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-012': '查詢表單：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-013': '開始日期：查詢元件：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-014': '開始日期：查詢元件：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-015': '開始日期：查詢元件：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-016': '開始日期：查詢元件：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-017': '開始日期：查詢元件：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-018': '開始日期：查詢元件：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-019': '結束日期：查詢元件：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-020': '結束日期：查詢元件：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-021': '結束日期：查詢元件：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-022': '結束日期：查詢元件：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-023': '結束日期：查詢元件：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-024': '結束日期：查詢元件：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-025': '查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-026': '查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-027': '查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-028': '查詢元件驗證：控制已離開文件',
    'ESUN-05-863-029': '查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-030': '查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-031': '暫態查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-032': '暫態查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-033': '暫態查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-034': '暫態查詢元件驗證：控制已離開文件',
    'ESUN-05-863-035': '暫態查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-036': '暫態查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-037': '查詢元件：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-038': '查詢元件：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-039': '查詢元件：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-040': '查詢元件：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-041': '查詢元件：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-042': '查詢元件：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-043': '查詢元件：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-044': '查詢元件：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-045': '查詢元件：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-046': '查詢元件：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-047': '查詢元件：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-048': '查詢元件：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-049': '開始日期：日曆容器：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-050': '開始日期：日曆容器：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-051': '開始日期：日曆容器：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-052': '開始日期：日曆容器：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-053': '開始日期：日曆容器：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-054': '開始日期：日曆容器：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-055': '結束日期：日曆容器：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-056': '結束日期：日曆容器：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-057': '結束日期：日曆容器：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-058': '結束日期：日曆容器：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-059': '結束日期：日曆容器：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-060': '結束日期：日曆容器：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-061': '開始日期：日曆視窗：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-062': '開始日期：日曆視窗：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-063': '開始日期：日曆視窗：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-064': '開始日期：日曆視窗：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-065': '開始日期：日曆視窗：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-066': '開始日期：日曆視窗：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-067': '結束日期：日曆視窗：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-068': '結束日期：日曆視窗：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-069': '結束日期：日曆視窗：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-070': '結束日期：日曆視窗：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-071': '結束日期：日曆視窗：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-072': '結束日期：日曆視窗：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-073': '開始日期：操作目標：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-074': '開始日期：操作目標：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-075': '開始日期：操作目標：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-076': '開始日期：操作目標：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-077': '開始日期：操作目標：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-078': '開始日期：操作目標：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-079': '結束日期：操作目標：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-080': '結束日期：操作目標：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-081': '結束日期：操作目標：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-082': '結束日期：操作目標：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-083': '結束日期：操作目標：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-084': '結束日期：操作目標：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-085': '自訂日期選項點擊操作目標：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-086': '自訂日期選項點擊操作目標：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-087': '自訂日期選項點擊操作目標：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-088': '自訂日期選項點擊操作目標：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-089': '自訂日期選項點擊操作目標：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-090': '自訂日期選項點擊操作目標：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-091': '日期模式選項：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-092': '日期模式選項：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-093': '日期模式選項：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-094': '日期模式選項：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-095': '日期模式選項：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-096': '日期模式選項：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-097': '日期模式選項：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-098': '日期模式選項：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-099': '日期模式選項：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-100': '日期模式選項：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-101': '日期模式選項：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-102': '日期模式選項：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-103': '日期模式選項：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-104': '日期模式選項：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-105': '日期模式選項：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-106': '日期模式選項：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-107': '日期模式選項：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-108': '日期模式選項：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-109': '日期模式選項：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-110': '日期模式選項：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-111': '日期模式選項：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-112': '日期模式選項：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-113': '日期模式選項：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-114': '日期模式選項：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-115': '開始日期開啟日曆操作目標：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-116': '開始日期開啟日曆操作目標：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-117': '開始日期開啟日曆操作目標：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-118': '開始日期開啟日曆操作目標：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-119': '開始日期開啟日曆操作目標：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-120': '開始日期開啟日曆操作目標：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-121': '結束日期開啟日曆操作目標：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-122': '結束日期開啟日曆操作目標：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-123': '結束日期開啟日曆操作目標：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-124': '結束日期開啟日曆操作目標：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-125': '結束日期開啟日曆操作目標：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-126': '結束日期開啟日曆操作目標：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-127': '開始日期切換年月標題操作目標：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-128': '開始日期切換年月標題操作目標：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-129': '開始日期切換年月標題操作目標：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-130': '開始日期切換年月標題操作目標：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-131': '開始日期切換年月標題操作目標：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-132': '開始日期切換年月標題操作目標：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-133': '結束日期切換年月標題操作目標：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-134': '結束日期切換年月標題操作目標：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-135': '結束日期切換年月標題操作目標：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-136': '結束日期切換年月標題操作目標：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-137': '結束日期切換年月標題操作目標：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-138': '結束日期切換年月標題操作目標：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-139': '開始日期切換年度操作目標：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-140': '開始日期切換年度操作目標：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-141': '開始日期切換年度操作目標：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-142': '開始日期切換年度操作目標：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-143': '開始日期切換年度操作目標：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-144': '開始日期切換年度操作目標：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-145': '結束日期切換年度操作目標：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-146': '結束日期切換年度操作目標：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-147': '結束日期切換年度操作目標：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-148': '結束日期切換年度操作目標：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-149': '結束日期切換年度操作目標：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-150': '結束日期切換年度操作目標：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-151': '開始日期選取月份操作目標：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-152': '開始日期選取月份操作目標：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-153': '開始日期選取月份操作目標：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-154': '開始日期選取月份操作目標：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-155': '開始日期選取月份操作目標：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-156': '開始日期選取月份操作目標：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-157': '結束日期選取月份操作目標：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-158': '結束日期選取月份操作目標：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-159': '結束日期選取月份操作目標：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-160': '結束日期選取月份操作目標：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-161': '結束日期選取月份操作目標：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-162': '結束日期選取月份操作目標：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-163': '開始日期選取日期操作目標：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-164': '開始日期選取日期操作目標：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-165': '開始日期選取日期操作目標：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-166': '開始日期選取日期操作目標：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-167': '開始日期選取日期操作目標：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-168': '開始日期選取日期操作目標：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-169': '結束日期選取日期操作目標：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-170': '結束日期選取日期操作目標：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-171': '結束日期選取日期操作目標：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-172': '結束日期選取日期操作目標：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-173': '結束日期選取日期操作目標：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-174': '結束日期選取日期操作目標：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-175': '日期模式選項：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-176': '日期模式選項：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-177': '日期模式選項：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-178': '日期模式選項：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-179': '日期模式選項：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-180': '日期模式選項：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-181': '開始日期：日期欄位：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-182': '開始日期：日期欄位：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-183': '開始日期：日期欄位：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-184': '開始日期：日期欄位：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-185': '開始日期：日期欄位：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-186': '開始日期：日期欄位：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-187': '結束日期：日期欄位：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-188': '結束日期：日期欄位：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-189': '結束日期：日期欄位：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-190': '結束日期：日期欄位：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-191': '結束日期：日期欄位：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-192': '結束日期：日期欄位：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-193': '開始日期：日曆標題：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-194': '開始日期：日曆標題：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-195': '開始日期：日曆標題：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-196': '開始日期：日曆標題：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-197': '開始日期：日曆標題：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-198': '開始日期：日曆標題：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-199': '結束日期：日曆標題：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-200': '結束日期：日曆標題：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-201': '結束日期：日曆標題：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-202': '結束日期：日曆標題：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-203': '結束日期：日曆標題：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-204': '結束日期：日曆標題：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-205': '開始日期：查詢元件：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-206': '開始日期：查詢元件：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-207': '開始日期：查詢元件：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-208': '開始日期：查詢元件：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-209': '開始日期：查詢元件：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-210': '開始日期：查詢元件：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-211': '結束日期：查詢元件：查詢元件驗證：祖先已隱藏',
    'ESUN-05-863-212': '結束日期：查詢元件：查詢元件驗證：祖先已停用互動',
    'ESUN-05-863-213': '結束日期：查詢元件：查詢元件驗證：祖先禁止操作',
    'ESUN-05-863-214': '結束日期：查詢元件：查詢元件驗證：控制已離開文件',
    'ESUN-05-863-215': '結束日期：查詢元件：查詢元件驗證：控制無可見矩形',
    'ESUN-05-863-216': '結束日期：查詢元件：查詢元件驗證：控制樣式不可見',
    'ESUN-05-863-217': '起日最終讀回控制安全檢查：祖先已隱藏',
    'ESUN-05-863-218': '起日最終讀回控制安全檢查：祖先已停用互動',
    'ESUN-05-863-219': '起日最終讀回控制安全檢查：祖先禁止操作',
    'ESUN-05-863-220': '起日最終讀回控制安全檢查：控制已離開文件',
    'ESUN-05-863-221': '起日最終讀回控制安全檢查：控制無可見矩形',
    'ESUN-05-863-222': '起日最終讀回控制安全檢查：控制樣式不可見',
    'ESUN-05-863-223': '迄日最終讀回控制安全檢查：祖先已隱藏',
    'ESUN-05-863-224': '迄日最終讀回控制安全檢查：祖先已停用互動',
    'ESUN-05-863-225': '迄日最終讀回控制安全檢查：祖先禁止操作',
    'ESUN-05-863-226': '迄日最終讀回控制安全檢查：控制已離開文件',
    'ESUN-05-863-227': '迄日最終讀回控制安全檢查：控制無可見矩形',
    'ESUN-05-863-228': '迄日最終讀回控制安全檢查：控制樣式不可見',
    'ESUN-05-865-001': '預查詢就緒目的頁：版面數量不唯一',
    'ESUN-05-865-002': '預查詢就緒目的頁：版面仍是未登入狀態',
    'ESUN-05-865-003': '預查詢就緒目的頁：版面不可見',
    'ESUN-05-865-004': '預查詢就緒目的頁：臺幣查詢根元件不唯一',
    'ESUN-05-865-005': '預查詢就緒目的頁：臺幣查詢根元件不可見',
    'ESUN-05-865-006': '預查詢就緒目的頁：臺幣頁籤不唯一',
    'ESUN-05-865-007': '預查詢就緒目的頁：臺幣頁籤不可見',
    'ESUN-05-865-008': '預查詢就緒目的頁：頁籤容器不存在',
    'ESUN-05-865-009': '預查詢就緒目的頁：頁籤容器型態不符',
    'ESUN-05-865-010': '預查詢就緒目的頁：版面不持有頁籤容器',
    'ESUN-05-865-011': '預查詢就緒目的頁：根元件頁籤容器不符',
    'ESUN-05-865-012': '預查詢就緒目的頁：頁籤標頭型態不符',
    'ESUN-05-865-013': '預查詢就緒目的頁：頁籤標頭容器不符',
    'ESUN-05-865-014': '預查詢就緒目的頁：頁籤標頭數量不唯一',
    'ESUN-05-865-015': '預查詢就緒目的頁：頁籤內容數量不唯一',
    'ESUN-05-865-016': '預查詢就緒目的頁：臺幣頁籤角色不符',
    'ESUN-05-865-017': '預查詢就緒目的頁：臺幣頁籤未選取',
    'ESUN-05-865-101': '初始帳戶驗證目的頁：版面數量不唯一',
    'ESUN-05-865-102': '初始帳戶驗證目的頁：版面仍是未登入狀態',
    'ESUN-05-865-103': '初始帳戶驗證目的頁：版面不可見',
    'ESUN-05-865-104': '初始帳戶驗證目的頁：臺幣查詢根元件不唯一',
    'ESUN-05-865-105': '初始帳戶驗證目的頁：臺幣查詢根元件不可見',
    'ESUN-05-865-106': '初始帳戶驗證目的頁：臺幣頁籤不唯一',
    'ESUN-05-865-107': '初始帳戶驗證目的頁：臺幣頁籤不可見',
    'ESUN-05-865-108': '初始帳戶驗證目的頁：頁籤容器不存在',
    'ESUN-05-865-109': '初始帳戶驗證目的頁：頁籤容器型態不符',
    'ESUN-05-865-110': '初始帳戶驗證目的頁：版面不持有頁籤容器',
    'ESUN-05-865-111': '初始帳戶驗證目的頁：根元件頁籤容器不符',
    'ESUN-05-865-112': '初始帳戶驗證目的頁：頁籤標頭型態不符',
    'ESUN-05-865-113': '初始帳戶驗證目的頁：頁籤標頭容器不符',
    'ESUN-05-865-114': '初始帳戶驗證目的頁：頁籤標頭數量不唯一',
    'ESUN-05-865-115': '初始帳戶驗證目的頁：頁籤內容數量不唯一',
    'ESUN-05-865-116': '初始帳戶驗證目的頁：臺幣頁籤角色不符',
    'ESUN-05-865-117': '初始帳戶驗證目的頁：臺幣頁籤未選取',
    'ESUN-05-865-201': '查詢准入重驗目的頁：版面數量不唯一',
    'ESUN-05-865-202': '查詢准入重驗目的頁：版面仍是未登入狀態',
    'ESUN-05-865-203': '查詢准入重驗目的頁：版面不可見',
    'ESUN-05-865-204': '查詢准入重驗目的頁：臺幣查詢根元件不唯一',
    'ESUN-05-865-205': '查詢准入重驗目的頁：臺幣查詢根元件不可見',
    'ESUN-05-865-206': '查詢准入重驗目的頁：臺幣頁籤不唯一',
    'ESUN-05-865-207': '查詢准入重驗目的頁：臺幣頁籤不可見',
    'ESUN-05-865-208': '查詢准入重驗目的頁：頁籤容器不存在',
    'ESUN-05-865-209': '查詢准入重驗目的頁：頁籤容器型態不符',
    'ESUN-05-865-210': '查詢准入重驗目的頁：版面不持有頁籤容器',
    'ESUN-05-865-211': '查詢准入重驗目的頁：根元件頁籤容器不符',
    'ESUN-05-865-212': '查詢准入重驗目的頁：頁籤標頭型態不符',
    'ESUN-05-865-213': '查詢准入重驗目的頁：頁籤標頭容器不符',
    'ESUN-05-865-214': '查詢准入重驗目的頁：頁籤標頭數量不唯一',
    'ESUN-05-865-215': '查詢准入重驗目的頁：頁籤內容數量不唯一',
    'ESUN-05-865-216': '查詢准入重驗目的頁：臺幣頁籤角色不符',
    'ESUN-05-865-217': '查詢准入重驗目的頁：臺幣頁籤未選取',
    'ESUN-05-865-301': '切換前持有者目的頁：版面數量不唯一',
    'ESUN-05-865-302': '切換前持有者目的頁：版面仍是未登入狀態',
    'ESUN-05-865-303': '切換前持有者目的頁：版面不可見',
    'ESUN-05-865-304': '切換前持有者目的頁：臺幣查詢根元件不唯一',
    'ESUN-05-865-305': '切換前持有者目的頁：臺幣查詢根元件不可見',
    'ESUN-05-865-306': '切換前持有者目的頁：臺幣頁籤不唯一',
    'ESUN-05-865-307': '切換前持有者目的頁：臺幣頁籤不可見',
    'ESUN-05-865-308': '切換前持有者目的頁：頁籤容器不存在',
    'ESUN-05-865-309': '切換前持有者目的頁：頁籤容器型態不符',
    'ESUN-05-865-310': '切換前持有者目的頁：版面不持有頁籤容器',
    'ESUN-05-865-311': '切換前持有者目的頁：根元件頁籤容器不符',
    'ESUN-05-865-312': '切換前持有者目的頁：頁籤標頭型態不符',
    'ESUN-05-865-313': '切換前持有者目的頁：頁籤標頭容器不符',
    'ESUN-05-865-314': '切換前持有者目的頁：頁籤標頭數量不唯一',
    'ESUN-05-865-315': '切換前持有者目的頁：頁籤內容數量不唯一',
    'ESUN-05-865-316': '切換前持有者目的頁：臺幣頁籤角色不符',
    'ESUN-05-865-317': '切換前持有者目的頁：臺幣頁籤未選取',
    'ESUN-05-865-401': '切換後帳戶驗證目的頁：版面數量不唯一',
    'ESUN-05-865-402': '切換後帳戶驗證目的頁：版面仍是未登入狀態',
    'ESUN-05-865-403': '切換後帳戶驗證目的頁：版面不可見',
    'ESUN-05-865-404': '切換後帳戶驗證目的頁：臺幣查詢根元件不唯一',
    'ESUN-05-865-405': '切換後帳戶驗證目的頁：臺幣查詢根元件不可見',
    'ESUN-05-865-406': '切換後帳戶驗證目的頁：臺幣頁籤不唯一',
    'ESUN-05-865-407': '切換後帳戶驗證目的頁：臺幣頁籤不可見',
    'ESUN-05-865-408': '切換後帳戶驗證目的頁：頁籤容器不存在',
    'ESUN-05-865-409': '切換後帳戶驗證目的頁：頁籤容器型態不符',
    'ESUN-05-865-410': '切換後帳戶驗證目的頁：版面不持有頁籤容器',
    'ESUN-05-865-411': '切換後帳戶驗證目的頁：根元件頁籤容器不符',
    'ESUN-05-865-412': '切換後帳戶驗證目的頁：頁籤標頭型態不符',
    'ESUN-05-865-413': '切換後帳戶驗證目的頁：頁籤標頭容器不符',
    'ESUN-05-865-414': '切換後帳戶驗證目的頁：頁籤標頭數量不唯一',
    'ESUN-05-865-415': '切換後帳戶驗證目的頁：頁籤內容數量不唯一',
    'ESUN-05-865-416': '切換後帳戶驗證目的頁：臺幣頁籤角色不符',
    'ESUN-05-865-417': '切換後帳戶驗證目的頁：臺幣頁籤未選取',
}

# Explicit caller-role inventory. Defaults 579/581 are present in signatures but
# every current caller supplies a table: static-only, NOT actual runtime coverage.
SAFE_ROLES = {
 '784-008':('owner_state','root'), '785-008':('owner_state','form'),
 '801-008':('owner_state','account'), '783-008':('query_twd','submit'),
 '813-008':('query_twd','radio-before'), '814-008':('query_twd','radio-after'),
 '818-008':('dates_mounted','radio'), '816-008':('query_twd','radio-final'),
 '817-008':('query_twd','radio-submit'),
 '815-008':('dates_mounted','startDate'), '815-508':('dates_mounted','endDate'),
 '812-008':('query_twd','startDate-input'), '812-508':('query_twd','endDate-input'),
 '797-008':('query_twd','startDate-base'), '797-508':('query_twd','endDate-base'),
 '798-008':('query_twd','startDate-popup'), '798-508':('query_twd','endDate-popup'),
 '799-008':('query_twd','startDate-day'), '799-508':('query_twd','endDate-day'),
 '819-008':('title','startDate'), '819-508':('title','endDate'),
 '820-008':('query_twd','startDate-readonly'), '820-508':('query_twd','endDate-readonly'),
 '850-008':('final_readback','startDate'), '851-008':('final_readback','endDate'),
 '786-008':('click','radio'), '787-008':('click','startDate-open'),
 '788-008':('click','endDate-open'), '789-008':('click','startDate-header'),
 '790-008':('click','endDate-header'), '791-008':('click','startDate-year'),
 '792-008':('click','endDate-year'), '793-008':('click','startDate-month'),
 '794-008':('click','endDate-month'), '795-008':('click','startDate-day'),
 '796-008':('click','endDate-day'),
}


def native_safe_role(target):
    """Use native selector + Python caller location, NEVER diagnostic locals/IDs.
    Source address resolves repeated calls on the SAME radio element."""
    import sys
    frames=[]
    frame=sys._getframe(1)
    while frame:
        frames.append((frame.f_code.co_name,frame.f_lineno,frame.f_code.co_filename))
        frame=frame.f_back
    names=[f[0] for f in frames]
    s=target.selector
    field='startDate' if 'startDate' in s else 'endDate'
    if 'owner_state' in names:
        return ('owner_state','root' if s==query.ROOT else 'form' if s==query.ROOT+' '+query.FORM else 'account')
    if 'dates_mounted' in names:
        return ('dates_mounted','radio' if 'radio' in s else field)
    if 'final_readback' in names:return ('final_readback',field)
    if 'title' in names:return ('title',field)
    if 'click' in names:
        role='radio' if 'radio' in s else field+('-open' if 'combo-block' in s else '-header' if 'header-label' in s
             else '-year' if 'prev-icon' in s or 'next-icon' in s else '-month' if 'month-item' in s else '-day')
        return ('click',role)
    if 'radio' in s:
        # Match source expressions, not metadata strings or a current error table.
        lines=Path(query.__file__).read_text().splitlines()
        line=next(line for name,line,path in frames if name=='query_twd' and path==query.__file__)
        tree = ast.parse('\n'.join(lines))
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'query_twd')
        calls = sorted(n.lineno for statement in function.body if not isinstance(statement, ast.FunctionDef)
                       for n in ast.walk(statement) if isinstance(n, ast.Call)
                       and isinstance(n.func, ast.Name) and n.func.id == 'one'
                       and n.args and isinstance(n.args[0], ast.Name) and n.args[0].id == 'radio')
        assert len(calls) == 4
        role = ('radio-before', 'radio-after', 'radio-final', 'radio-submit')[calls.index(line)]
        return ('query_twd',role)
    if 'submit' in s:return ('query_twd','submit')
    return ('query_twd',field+('-input' if 'input.combo-input' in s and '.calendar-base' not in s
            else '-readonly' if ' input[name=' in s and '.calendar-base' not in s
            else '-day' if 'day-cell' in s else '-popup' if 'dialog.calendar' in s else '-base'))


@pytest.mark.parametrize('fallback',list(SAFE_ROLES))
@pytest.mark.parametrize('reason',range(1,7))
def test_safe_native_role_joined_to_returned_run(monkeypatch,fallback,reason):
    from tests.test_esun_terminal_residual_codes import node_observation
    role=SAFE_ROLES[fallback]
    observed=node_observation(query.SAFE_REASON,'safe',reason)
    assert observed['result']==[False,reason]
    seen=[]
    def fault(target,script):
        if native_safe_role(target)==role:
            seen.append((target.selector,role))
            return observed['result']
    crawler,page,collector,trace=spa_surface(monkeypatch,safe_fault=fault,calendar_state=fallback in list(SAFE_ROLES)[25:])
    result=actual_run(crawler,page,collector)
    # This is runtime caller proof, not a catalog-membership loop.
    expected=SAFE_LITERAL['ESUN-05-'+fallback][reason-1]
    assert_terminal(result,expected,EXPECTED_CHINESE[expected])
    assert seen and not page.submissions


def test_safe_inventory_all_tables_have_explicit_runtime_or_static_status():
    assert {'ESUN-05-'+key for key in SAFE_ROLES}|{'ESUN-05-579-003','ESUN-05-581-003'}==set(query.SAFE_CODES)


@pytest.mark.parametrize('role',['switch_owner','switch_result'])
@pytest.mark.parametrize('marker',range(1,18))
def test_two_accounts_switch_destination_js_to_actual_run(monkeypatch,role,marker):
    from backend.banks.esun_spa import collection
    from tests.test_esun_terminal_residual_codes import node_observation
    observed=node_observation(collection.DESTINATION_REASON,'destination',marker)
    assert observed['result']==[False,marker]
    consumed=[]
    def fault(page,crawler):
        if page.submissions==[0] and crawler._esun_spa_phase=='account_validation' and page.selected==(role=='switch_result'):
            consumed.append(page.selected);return observed['result']
    crawler,page,collector,trace=spa_surface(monkeypatch,two_accounts=True,destination_fault=fault)
    result=actual_run(crawler,page,collector)
    expected=DESTINATION_LITERAL[role][marker-1]
    assert_terminal(result,expected,EXPECTED_CHINESE[expected])
    assert consumed==[int(role=='switch_result')]
    assert page.submissions==[0]  # No query on the unproved second owner.


@pytest.mark.parametrize('field,code,chinese',[
    ('startDate','ESUN-05-864-009','發佈證據日期持有者：起日不符'),
    ('endDate','ESUN-05-864-010','發佈證據日期持有者：迄日不符')])
def test_switch_rechecks_prior_date_owner_before_native_account_action(monkeypatch,field,code,chinese):
    crawler,page,collector,trace=spa_surface(monkeypatch,two_accounts=True,
        wrong_date=lambda page,name:in_caller('switch_account') and name==field)
    result=actual_run(crawler,page,collector)
    assert_terminal(result,code,chinese)
    assert page.submissions==[0]
    assert not any(x[0]=='click' and 'accountList' in x[1] for x in trace if type(x) is tuple)


def test_two_accounts_success_retains_complete_coverage(monkeypatch):
    crawler,page,collector,trace=spa_surface(monkeypatch,two_accounts=True)
    result=actual_run(crawler,page,collector)
    assert 'error' not in result,result
    assert page.submissions==[0,1]
    windows=result['data']['history_coverage']['domains'][0]['windows']
    assert [w['identity'] for w in windows]==['0000000000001','0000000000002']
    assert all(w['status']=='explicit_empty' for w in windows)


def in_caller(name):
    import sys
    frame=sys._getframe(1)
    while frame:
        if frame.f_code.co_name==name:return True
        frame=frame.f_back
    return False


SAFE_CHAIN_NODE = r'''
const fs=require('fs'),vm=require('vm');
const [source,mode]=JSON.parse(fs.readFileSync(0,'utf8'));
const trace=[];let reads=0;
function node(name,parent){return new Proxy({parentElement:parent,
 hidden:mode==='ancestor_hidden'&&name==='ancestor',inert:mode==='ancestor_inert'&&name==='ancestor',
 matches:s=>{trace.push(name+'.matches:'+s);return false},
 get isConnected(){return mode!=='changing'||++reads>1},getClientRects(){trace.push(name+'.rects');return [{}]}
},{get(o,k){trace.push(name+'.'+String(k));return o[k]}})}
const ancestor=node('ancestor',null),parent=node('parent',ancestor),leaf=node('leaf',parent);
const denied=()=>{throw Error('offline external')};
const fn=vm.runInNewContext('('+source+')',{getComputedStyle(n){trace.push('style');return {visibility:'visible'}},fetch:denied,setTimeout:denied,XMLHttpRequest:denied,WebSocket:denied});
const results=[fn(leaf),fn(leaf)];
console.log(JSON.stringify({results,trace}));
'''


def safe_chain(source,mode):
    return json.loads(subprocess.run(['node','-e',SAFE_CHAIN_NODE],input=json.dumps([source,mode]),
        text=True,capture_output=True,check=True,timeout=10).stdout)


@pytest.mark.parametrize('mode,reasons',[
    ('ancestor_hidden',[1,1]),('ancestor_inert',[2,2]),('changing',[4,0])])
def test_safe_ancestor_and_changing_reads_have_frozen_native_order(mode,reasons):
    old,new=safe_chain(query.SAFE,mode),safe_chain(query.SAFE_REASON,mode)
    assert old['trace']==new['trace']
    assert old['results']==[r[0] for r in new['results']]
    assert [r[1] for r in new['results']]==reasons


@pytest.mark.parametrize('mode,code,chinese',[
    ('ancestor_hidden','ESUN-05-863-205','開始日期：查詢元件：查詢元件驗證：祖先已隱藏'),
    ('ancestor_inert','ESUN-05-863-206','開始日期：查詢元件：查詢元件驗證：祖先已停用互動')])
def test_safe_ancestor_js_output_is_consumed_by_820_then_run(monkeypatch,mode,code,chinese):
    observation=safe_chain(query.SAFE_REASON,mode)['results'][0]
    def fault(target,script):
        if target.selector.endswith(' input[name=startDate][readonly]'):return observation
    crawler,page,collector,trace=spa_surface(monkeypatch,safe_fault=fault)
    assert_terminal(actual_run(crawler,page,collector),code,chinese)


@pytest.mark.parametrize('recover',[False,True])
def test_original_current_switch_and_recovery_native_order_budget(monkeypatch,recover):
    from backend.banks.esun_spa import collection
    from tests.test_esun_terminal_residual_codes import node_observation
    original_rows=frozen_module('backend/banks/esun_spa/collection.py','backend.banks.esun_spa.frozen_collection')._collect_rows
    original_query=frozen_module('backend/banks/esun_spa/query.py','backend.banks.esun_spa.frozen_query').query_twd
    current_query=query.query_twd
    observations=[node_observation(query.SAFE_REASON,'safe',n)['result'] for n in (1,2)]
    outputs=[]
    for old in (True,False):
        with monkeypatch.context() as patcher:
            seen=[]
            def fault(target,script):
                if recover and target.selector==query.ROOT and len(seen)<2:
                    value=observations[len(seen)];seen.append(value);return value
            crawler,page,collector,trace=spa_surface(patcher,two_accounts=True,safe_fault=fault)
            budgets=[]
            def spy(*args,**kwargs):
                try:return (original_query if old else current_query)(*args,**kwargs)
                finally:budgets.append(list(kwargs['action_budget']))
            patcher.setattr(query,'query_twd',spy)
            if old:patcher.setattr(collection,'_collect_rows',original_rows)
            result=actual_run(crawler,page,collector)
            assert 'error' not in result,result
            assert page.submissions==[0,1]
            assert budgets==[[5],[5]]
            outputs.append((result,trace,budgets,seen))
    assert outputs[0]==outputs[1]


def test_safe_reason_a_b_success_then_independent_native_failure_does_not_leak(monkeypatch):
    from tests.test_esun_terminal_residual_codes import node_observation
    observations=[node_observation(query.SAFE_REASON,'safe',n)['result'] for n in (1,2)]
    seen=[]
    def fault(target,script):
        if target.selector==query.ROOT and len(seen)<2:
            value=observations[len(seen)];seen.append(value);return value
    crawler,page,collector,trace=spa_surface(monkeypatch,safe_fault=fault)
    page.native_failure='button[type="submit"]'
    result=actual_run(crawler,page,collector)
    assert result['collect_diagnostics']['error_id']=='ESUN-05-774-001'
    assert seen==[[False,1],[False,2]]
    assert [x for x in trace if x==('wait',50)]==[('wait',50),('wait',50)]
    assert page.submissions==[]


@pytest.mark.parametrize('field,code,chinese',[
    ('startDate','ESUN-05-862-003','查詢日期就緒期限：彈窗數量尚未就緒'),
    ('endDate','ESUN-05-862-503','迄日查詢就緒期限：彈窗數量尚未就緒')])
def test_actual_collect_readiness_deadline_to_actual_run(monkeypatch,field,code,chinese):
    def observation(op,target,value):
        if op=='count' and field in target.selector and 'dialog.calendar' in target.selector and in_caller('ready') and not in_caller('owner_state'):
            return 0
        return value
    crawler,page,collector,trace=spa_surface(monkeypatch,native_observation=observation)
    assert_terminal(actual_run(crawler,page,collector),code,chinese)
    assert ('wait',20) in trace and not page.submissions


@pytest.mark.parametrize('field',['startDate','endDate'])
@pytest.mark.parametrize('mechanism',['popup','calendar','settle'])
def test_pending_readiness_a_b_success_then_new_native_failure(monkeypatch,field,mechanism):
    seen=[]
    def observation(op,target,value):
        s=target.selector
        if field not in s or not in_caller('ready') or in_caller('owner_state'):return value
        if mechanism=='popup' and 'dialog.calendar' in s and s.endswith('[role="dialog"]'):
            if not seen and op=='count':seen.append('A');return 0
            if seen==['A'] and op=='visible':seen.append('B');return False
            if seen==['A','B'] and op=='visible':seen.append('success')
        if mechanism=='calendar' and target.page.calendar_mode=='months':
            if not seen and op=='text' and 'header-label' in s:seen.append('A');return 'pending'
            if seen==['A'] and op=='count' and 'month-item' in s:seen.append('B');return 11
            if seen==['A','B'] and op=='count' and 'month-item' in s:seen.append('success')
        if mechanism=='settle' and op=='settle':
            if len(seen)<2:seen.append('A' if not seen else 'B');return [False,len(seen)]
            if len(seen)==2:seen.append('success')
        return value
    crawler,page,collector,trace=spa_surface(monkeypatch,native_observation=observation,
        calendar_state=mechanism!='popup')
    page.native_failure='button[type="submit"]'
    result=actual_run(crawler,page,collector)
    assert_terminal(result,'ESUN-05-774-001','台幣日期查詢：點擊：原生動作失敗')
    assert seen==['A','B','success']
    assert not page.submissions


def test_destination_reason_then_body_unavailable_deadline_clears_old_reason(monkeypatch):
    from backend.banks.esun_spa import collection
    from tests.test_esun_terminal_residual_codes import node_observation
    observed=node_observation(collection.DESTINATION_REASON,'destination',1)['result']
    seen=[]
    def fault(page,crawler):
        if crawler._esun_spa_phase=='prequery_response':
            seen.append('A');page.make_prequery_unavailable=True;return observed
    crawler,page,collector,trace=spa_surface(monkeypatch,destination_fault=fault)
    result=actual_run(crawler,page,collector)
    assert_terminal(result,'ESUN-05-468-001','台幣交易列收集：驗證條件未通過：monotonic() < deadline')
    assert seen==['A']
    assert len([x for x in trace if x[0]=='destination'])==1
    assert not page.submissions


def test_destination_a_b_success_then_independent_native_failure(monkeypatch):
    from backend.banks.esun_spa import collection
    from tests.test_esun_terminal_residual_codes import node_observation
    observations=[node_observation(collection.DESTINATION_REASON,'destination',n)['result'] for n in (1,2)]
    seen=[]
    def fault(page,crawler):
        if crawler._esun_spa_phase=='prequery_response':
            if len(seen)<2:value=observations[len(seen)];seen.append(value);return value
            if len(seen)==2:seen.append('success')
    crawler,page,collector,trace=spa_surface(monkeypatch,destination_fault=fault)
    page.native_failure='button[type="submit"]'
    assert_terminal(actual_run(crawler,page,collector),'ESUN-05-774-001','台幣日期查詢：點擊：原生動作失敗')
    assert seen==[[False,1],[False,2],'success']


def test_original_current_window_then_account_transition_order_and_budget(monkeypatch):
    from backend.banks.esun_spa import collection
    original_rows=frozen_module('backend/banks/esun_spa/collection.py','backend.banks.esun_spa.frozen_collection')._collect_rows
    original_query=frozen_module('backend/banks/esun_spa/query.py','backend.banks.esun_spa.frozen_query').query_twd
    current_query=query.query_twd
    outputs=[]
    for old in (True,False):
        with monkeypatch.context() as patcher:
            crawler,page,collector,trace=spa_surface(patcher,two_accounts=True,calendar_state=True,cursor_day='2026-01-08')
            budgets=[]
            def spy(*args,**kwargs):
                try:return (original_query if old else current_query)(*args,**kwargs)
                finally:budgets.append(list(kwargs['action_budget']))
            patcher.setattr(query,'query_twd',spy)
            if old:patcher.setattr(collection,'_collect_rows',original_rows)
            result=actual_run(crawler,page,collector)
            assert 'error' not in result,result
            assert page.submissions==[0,0,1,1]
            assert budgets==[[12],[11],[11],[11]]
            outputs.append((result,trace,budgets))
    assert outputs[0]==outputs[1]


def test_timeline_a_b_success_then_independent_native_owner_failure(monkeypatch):
    from backend.banks.esun_spa import collection
    from tests.test_esun_terminal_residual_codes import node_observation
    observations=[node_observation(collection.TIMELINE_REASON,'timeline',n)['result'] for n in (1,2)]
    seen=[]
    def timeline(page,crawler):
        if crawler._esun_spa_phase=='transaction_preflight':return ['old',0]
        if len(seen)<2:value=observations[len(seen)];seen.append(value);return value
        seen.append('success');page.fail_date_read=True;return ['new',0]
    crawler,page,collector,trace=spa_surface(monkeypatch,timeline_fault=timeline,positive=True)
    result=actual_run(crawler,page,collector)
    assert_terminal(result,'ESUN-05-648-101','查詢所有權檢查：欄位讀回：原生動作失敗')
    assert seen==[[None,1],[None,2],'success']
    assert page.submissions==[0]
