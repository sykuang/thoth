"""Synthetic DOM from public CTWA1001 / CTW01001 render contracts, not live proof."""
import json
import pytest
from patchright.sync_api import Page

from backend.banks.esun import EsunCrawler
from backend.core.base import _OriginGuardProxy
from tests.test_esun_spa_menu_control_ownership import browser as browser


def account(no, category, amount=None, currency=None, pairs=()):
    return (f'<div class="widget-balance-detail-down-container">'
            f'<div class="widget-balance-detail-down-type">{category}</div>'
            '<span class="widget-balance-detail-down-account-alias">Alias is not category</span>'
            + (f'<span class="widget-balance-detail-down-cur">{currency}</span>' if currency else '')
            + (f'<span class="widget-balance-detail-down-real-amount">{amount}</span>' if amount is not None else '')
            + f'<span class="widget-balance-detail-down-account-no">{no}</span>'
            + ''.join(f'<div class="widget-balance-detail-down-cur-list"><span>{c}</span><span>{a}</span></div>' for c,a in pairs)
            + '</div>')


@pytest.fixture
def scene(browser, monkeypatch, request):
    # Old implementation's fixed sleeps are irrelevant to this synchronous fixture.
    monkeypatch.setattr(Page, "wait_for_timeout", lambda *a: None)
    context = browser.new_context(viewport={"width": 1200, "height": 900})
    context.route("**/*", lambda route: route.abort())
    page = context.new_page()
    tw = account('0000000000001', '臺幣活存', '1,000.25')
    fx = account('0000000000002', '外幣綜存', pairs=[('USD','3.75'),('JPY','9')])
    fx += account('0000000000003', '歐元定存', '8', 'EUR')
    mutation = getattr(request, 'param', None)
    if mutation:
        mode, before, after = mutation
        if mode == 'empty_fx':
            fx = ''
        elif mode == 'empty_all':
            tw = fx = ''
        elif mode == 'tw':
            tw = tw.replace(before, after)
        else:
            fx = fx.replace(before, after)
    page.set_content('<style>.ctw01001 {min-height:20px}</style><div id="layout-content"></div><script>'
        + 'const tw='+json.dumps(tw)+';const fx='+json.dumps(fx)+';'
        + """
        const home='<div class="ctwA1001"><div class="summary-block"><div class="footer-btn" role="button" onclick="overview(false)">Official task label</div></div></div>';
        function overview(fr){document.querySelector('#layout-content').innerHTML=
            '<div class="mib-tab"><div class="tab-button" role="tab" id="tw" aria-selected="'+!fr+'" onclick="overview(false)">臺幣</div><div class="tab-button" role="tab" id="fr" aria-selected="'+fr+'" onclick="overview(true)">外幣</div></div><div class="ctw01001">'+(fr?fx:tw)+'</div>';}
        function homePage(){document.querySelector('#layout-content').innerHTML=home;}
        homePage();
        </script><button onclick="homePage()">個人首頁</button>
        <button onclick="document.querySelector('#layout-content').innerHTML='<div>Wrong transaction query form</div>'">交易明細</button>""")
    try:
        yield page
    finally:
        context.close()


def test_actual_overview_reader_uses_official_entry_and_each_currency(scene):
    result=object.__new__(EsunCrawler)._spa_overview_accounts(_OriginGuardProxy(scene, lambda: None))
    assert [(a['account_no'],a['category'],a['currency'],a['balance']) for a in result] == [
        ('0000000000001','臺幣活存','TWD',1000.25),
        ('0000000000002','外幣綜存','USD',3.75),
        ('0000000000002','外幣綜存','JPY',9.0),
        ('0000000000003','歐元定存','EUR',8.0),
    ]


@pytest.mark.parametrize("scene,expected", [
    (("tw", "0000000000001", "*************"), "ESUN-06-881-001"),
    (("tw", "臺幣活存", ""), "ESUN-06-881-002"),
    (("tw", "1,000.25", "******"), "ESUN-06-881-004"),
    (("tw", "1,000.25", "--"), "ESUN-06-881-004"),
    (("tw", "1,000.25", "1,00.25"), "ESUN-06-881-004"),
    (("tw", "1,000.25", "9" * 400), "ESUN-06-881-004"),
    (("fr", "0000000000002", "bad-account"), "ESUN-06-882-001"),
    (("fr", "外幣綜存", ""), "ESUN-06-882-002"),
    (("fr", "USD", "TWD"), "ESUN-06-882-003"),
    (("fr", "USD", ""), "ESUN-06-882-003"),
    (("fr", "3.75", "******"), "ESUN-06-882-004"),
    (("fr", "<span>JPY</span>", "<span>USD</span>"), "ESUN-06-882-005"),
], indirect=["scene"])
def test_bad_native_fields_fail_closed_without_partial_accounts(scene, expected):
    from backend.core.error_codes import exception_error_id
    with pytest.raises(ValueError) as caught:
        object.__new__(EsunCrawler)._spa_overview_accounts(scene)
    assert exception_error_id(caught.value) == expected


@pytest.mark.parametrize("kind,occurrence,expected", [
    ("home", 1, "ESUN-06-414-001"),
    ("wait", 1, "ESUN-06-880-002"),
    ("click", 1, "ESUN-06-880-003"),
    ("wait", 2, "ESUN-06-880-004"),
    ("read", 1, "ESUN-06-418-001"),
    ("handle", 1, "ESUN-06-880-005"),
    ("click", 2, "ESUN-06-880-006"),
    ("gone", 1, "ESUN-06-880-007"),
    ("wait", 3, "ESUN-06-881-006"),
    ("wait", 4, "ESUN-06-880-008"),
    ("wait", 5, "ESUN-06-882-006"),
    ("read", 2, "ESUN-06-421-001"),
    ("locator", 1, "ESUN-06-880-009"),
    ("locator", 2, "ESUN-06-880-010"),
    ("locator", 3, "ESUN-06-881-007"),
    ("locator", 4, "ESUN-06-880-011"),
    ("locator", 5, "ESUN-06-882-007"),
])
def test_native_fault_keeps_identity_and_leaf_through_actual_collect(scene, monkeypatch, kind, occurrence, expected):
    from patchright.sync_api import Locator, ElementHandle
    from backend.banks.esun_spa.capture import SpaCollector
    from backend.core.base import BankCollectResult
    from backend.core.error_codes import exception_error_id
    from backend.banks.esun_spa import collection, products

    monkeypatch.setattr(collection, 'collect_twd', lambda *a, **k: BankCollectResult())
    monkeypatch.setattr(products, 'collect_products', lambda *a, **k: BankCollectResult())
    monkeypatch.setattr(EsunCrawler, '_ensure_collect_origin', lambda *a: None)
    crawler=object.__new__(EsunCrawler)
    crawler._spa_login_baseline={'synthetic': True}
    fault=RuntimeError('synthetic-native-fault')
    cls, method = {
        'home': (Page, 'evaluate'), 'wait': (Locator, 'wait_for'),
        'click': (Locator, 'click'), 'read': (Locator, 'evaluate'),
        'handle': (Locator, 'element_handle'), 'gone': (ElementHandle, 'wait_for_element_state'),
        'locator': (Page, 'locator'),
    }[kind]
    original=getattr(cls,method)
    calls=[]
    def inject(self,*args,**kwargs):
        # collect's original SPA admission read is not an overview action.
        is_admission = kind == 'home' and args and '#layout-content' in args[0]
        if not is_admission:
            calls.append(1)
            if len(calls)==occurrence:
                raise fault
        return original(self,*args,**kwargs)
    monkeypatch.setattr(cls,method,inject)
    with pytest.raises(RuntimeError) as caught:
        crawler.collect(scene,SpaCollector())
    assert caught.value is fault
    assert exception_error_id(caught.value)==expected
    assert len(calls)==occurrence


@pytest.mark.parametrize("mode,code", [('tw','ESUN-06-881-006'),('fr','ESUN-06-882-006')])
def test_read_requires_the_destination_tab_to_be_selected(scene, monkeypatch, mode, code):
    from patchright.sync_api import Locator, Error
    from backend.core.error_codes import exception_error_id
    scene.evaluate("""mode => document.addEventListener('click',()=> {
        const tab=document.querySelector('.mib-tab #'+mode);
        if(tab)tab.setAttribute('aria-selected','false');
    })""",mode)
    original=Locator.wait_for
    monkeypatch.setattr(Locator,'wait_for',lambda self,**kw: original(self,**{**kw,'timeout':50}))
    with pytest.raises(Error) as caught:
        object.__new__(EsunCrawler)._spa_overview_accounts(scene)
    assert exception_error_id(caught.value)==code


@pytest.mark.parametrize("scene,key,expected", [
    (("tw","1,000.25","0"),('0000000000001','TWD'),0.0),
    (("fr","3.75","-3.75"),('0000000000002','USD'),-3.75),
],indirect=["scene"])
def test_explicit_native_zero_and_negative_balances_are_preserved(scene,key,expected):
    rows=object.__new__(EsunCrawler)._spa_overview_accounts(scene)
    assert next(a['balance'] for a in rows if (a['account_no'],a['currency'])==key)==expected


@pytest.mark.parametrize("scene",[("empty_fx","","")],indirect=True)
def test_successful_empty_fx_page_does_not_invent_an_account(scene):
    rows=object.__new__(EsunCrawler)._spa_overview_accounts(scene)
    assert [(a['account_no'],a['currency']) for a in rows]==[('0000000000001','TWD')]


@pytest.mark.parametrize("scene",[("empty_all","","")],indirect=True)
def test_actual_collect_still_rejects_empty_account_inventory(scene,monkeypatch):
    from backend.banks.esun_spa import collection, products
    from backend.banks.esun_spa.capture import SpaCollector
    from backend.core.base import BankCollectResult
    from backend.core.error_codes import exception_error_id
    monkeypatch.setattr(collection,'collect_twd',lambda *a,**k: BankCollectResult())
    monkeypatch.setattr(products,'collect_products',lambda *a,**k: BankCollectResult())
    monkeypatch.setattr(EsunCrawler,'_ensure_collect_origin',lambda *a: None)
    crawler=object.__new__(EsunCrawler)
    crawler._spa_login_baseline={'synthetic': True}
    with pytest.raises(RuntimeError,match='esun-accounts-empty') as caught:
        crawler.collect(scene,SpaCollector())
    assert exception_error_id(caught.value)=='ESUN-05-416-001'
