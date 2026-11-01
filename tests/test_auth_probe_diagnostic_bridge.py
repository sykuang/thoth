"""Offline native auth observations through the real checkpoint and run sink."""
import ast
import asyncio
from importlib import import_module
from types import SimpleNamespace

import pytest

from backend.core import base, login_checkpoints as cp
from backend.core.error_codes import exception_error_id, format_error_id, with_error_code
from tests.test_login_checkpoint_browser_rules import Page, Locator


# Literal acceptance identities, not a catalog-generated certification.
BANK_CASES = [
    ('hsbc', 'HsbcCrawler', 'https://card.hsbc.com.tw/app', 'HSBC-03-500-001'),
    ('scsb', 'ScsbCrawler', 'https://ebank.scsb.com.tw/aply/home', 'SCSB-03-500-001'),
    ('esun', 'EsunCrawler', 'https://ebank.esunbank.com.tw/home', 'ESUN-03-400-001'),
    ('ubot', 'UbotCrawler', 'https://www.ubot.com.tw/home', 'UBOT-03-400-001'),
    ('sinopac', 'SinopacCrawler', 'https://mma.sinopac.com/mymma/home', 'SINOPAC-03-203-001'),
    ('fubon', 'FubonCrawler', 'https://ebank.taipeifubon.com.tw/home', 'FUBON-03-207-001'),
    ('dbs', 'DbsCrawler', 'https://internet-banking.dbs.com.tw/digitw/overview', 'DBS-03-014-002'),
    ('scb', 'ScbCrawler', 'https://ebank.standardchartered.com.tw/home', 'SCB-03-010-001'),
    ('taishin', 'TaishinCrawler', 'https://my.taishinbank.com.tw/home', 'TAISHIN-03-006-001'),
    ('rakuten', 'RakutenCrawler', 'https://www.rakuten-bank.com.tw/ebank/home', 'RAKUTEN-03-003-001'),
    ('linebank', 'LinebankCrawler', 'https://accessibility.linebank.com.tw/overview', 'LINEBANK-03-008-001'),
    ('ctbc', 'CtbcCrawler', 'https://www.ctbcbank.com/home', 'CTBC-03-011-001'),
    ('cathay', 'CathayCrawler', 'https://www.cathaybk.com.tw/home', 'CATHAY-03-011-001'),
]


class NativeFailure(ValueError):
    pass


class AuthPage(Page):
    def __init__(self, url, fault='first'):
        super().__init__()
        self.url = url
        self.frames = []
        self.fault = fault
        self.failure = NativeFailure('PRIVATE synthetic native observation')
        self.trace = []
        self.observation = 0

    def native(self, operation, *args, **kwargs):
        self.trace.append((operation, args, kwargs))
        if self.fault == 'first':
            raise self.failure

    def locator(self, selector):
        self.native('locator', selector)
        return Locator([])

    def evaluate(self, script):
        self.native('evaluate', script)
        return False

    def set_default_timeout(self, value):
        self.trace.append(('timeout', (value,), {}))
        super().set_default_timeout(value)


def crawler_for(case):
    bank, class_name, _, _ = case
    module = import_module('backend.banks.' + bank)
    cls = getattr(module, class_name)
    crawler = object.__new__(cls)  # Never load BankCreds or construct a real session.
    crawler.name = bank
    crawler.prepare_login_page = lambda page: None
    crawler.login_checkpoint_rules = lambda: ()
    crawler._credential_origin_allowed = lambda page: True
    crawler._submit_with_captcha_reloads = lambda page, submit_page: page.trace.append(('submit', (), {}))
    if bank == 'rakuten':
        crawler._semantic_modal_visible_in_owned_scope = lambda page: False
    return crawler


def run_offline(monkeypatch, crawler, page):
    crawler._enforce_session_freshness = lambda: None
    crawler._build_fetch_kwargs = lambda: {}
    crawler._make_collector = lambda page: SimpleNamespace(attach=lambda page: None, detach=lambda page: None)
    crawler.attach_shared_dialog_handler = lambda page: None
    crawler._recover_late_authentication = lambda page, error: False
    crawler.collect = lambda *args: pytest.fail('auth failure must not enter collection')
    crawler.logout = lambda page: None
    crawler._execute_browser_flow = lambda url, **kw: kw['page_action'](page)
    terminals = []
    reduce = base.reduce_login_checkpoint

    def observe(*args):
        try:
            return reduce(*args)
        except cp.LoginCheckpointTerminal as error:
            terminals.append(error)
            raise

    monkeypatch.setattr(base, 'reduce_login_checkpoint', observe)
    result = base.BankCrawler.run(crawler, 'offline:synthetic')
    return result, terminals


@pytest.mark.parametrize('case', BANK_CASES, ids=lambda c: c[0])
def test_actual_native_probe_through_terminal_and_run(monkeypatch, case):
    crawler = crawler_for(case)
    page = AuthPage(case[2])
    result, terminals = run_offline(monkeypatch, crawler, page)
    expected = case[3]
    assert exception_error_id(page.failure, bank=case[0]) == expected
    assert result['failure_diagnostics']['error_id'] == expected
    assert format_error_id(expected, bank=case[0]) in result['error']
    assert any('\u4e00' <= c <= '\u9fff' for c in result['error'])
    terminal, = terminals
    assert type(terminal) is cp.LoginCheckpointBlocked
    assert terminal.outcome == cp.CheckpointOutcome(cp.CheckpointKind.UNKNOWN_BLOCKER)
    assert terminal.phase is cp.CheckpointPhase.POST_SUBMIT
    assert terminal.budget == cp.LoginBudget(credential_submissions=1)
    assert terminal.__cause__ is None and terminal.__context__ is None
    assert page.trace.count(('submit', (), {})) == 1
    assert not hasattr(crawler, '_login_auth_probe_ids')
    assert not hasattr(crawler, '_login_inspection_failures')
    assert 'PRIVATE' not in repr(result)


@pytest.mark.parametrize('mismatch', [True, False])
def test_evaluation_finally_clears_even_early_bank_validation(mismatch):
    cell = ['CTBC-03-011-001']
    rule = cp.LoginCheckpointRule(
        name='other', bank='cathay', phases=tuple(cp.CheckpointPhase),
        kind=cp.CheckpointKind.OTP_REQUIRED, container_selector='#otp',
    )
    result = cp.evaluate_login_checkpoint(
        Page(), bank='ctbc', phase=cp.CheckpointPhase.POST_SUBMIT,
        rules=(rule,) if mismatch else (), is_authenticated=lambda page: False,
        _auth_probe_ids=cell,
    )
    assert cell == []
    assert result.error_id == ('CTBC-03-987-001' if mismatch else 'CTBC-03-011-001')


@pytest.mark.parametrize('phase', ['other', 'post_submit', 'post_submit_settle'])
def test_not_enrolled_phase_cannot_consume_native_id(phase):
    cell = ['CTBC-03-011-001']
    outcome = cp.evaluate_login_checkpoint(
        Page(), bank='ctbc', phase=phase, rules=(),
        is_authenticated=lambda page: False, _auth_probe_ids=cell,
    )
    assert outcome.error_id == 'CTBC-03-988-001'
    assert cell == []


# Literal native call roles and expected producer IDs. Same exception class at
# every operation; no exception-name/message classification or catalog sweep.
NATIVE_CASES = {'hsbc': [('#userId.lookup', 'HSBC-03-500-001'),
          ('#userId.count', 'HSBC-03-500-002'),
          ('#userId.nth', 'HSBC-03-500-003'),
          ('#userId.visible', 'HSBC-03-500-004'),
          ('#password.lookup', 'HSBC-03-510-001'),
          ('#password.count', 'HSBC-03-510-002'),
          ('#password.nth', 'HSBC-03-510-003'),
          ('#password.visible', 'HSBC-03-510-004'),
          ('#captchaInput.lookup', 'HSBC-03-520-001'),
          ('#captchaInput.count', 'HSBC-03-520-002'),
          ('#captchaInput.nth', 'HSBC-03-520-003'),
          ('#captchaInput.visible', 'HSBC-03-520-004'),
          ('body.lookup', 'HSBC-03-540-001'),
          ('body.text', 'HSBC-03-540-002')],
 'scsb': [('controls.lookup', 'SCSB-03-500-001'),
          ('controls.count', 'SCSB-03-500-002'),
          ('controls.nth', 'SCSB-03-500-003'),
          ('controls.visible', 'SCSB-03-500-004'),
          ('body.lookup', 'SCSB-03-540-001'),
          ('body.text', 'SCSB-03-540-002')],
 'esun': [('controls.lookup', 'ESUN-03-400-001'),
          ('controls.count', 'ESUN-03-401-102'),
          ('controls.nth', 'ESUN-03-401-100'),
          ('controls.visible', 'ESUN-03-401-101'),
          ('layout.lookup', 'ESUN-03-402-001'),
          ('layout.count', 'ESUN-03-403-001'),
          ('body.evaluate', 'ESUN-03-408-001')],
 'ubot': [('body.evaluate', 'UBOT-03-400-001')],
 'sinopac': [('url', 'SINOPAC-03-200-001'),
             ('url:2', 'SINOPAC-03-202-001'),
             ('frames', 'SINOPAC-03-209-001'),
             ('captcha.lookup', 'SINOPAC-03-203-001'),
             ('captcha.count', 'SINOPAC-03-204-203'),
             ('captcha.nth', 'SINOPAC-03-204-202'),
             ('captcha.visible', 'SINOPAC-03-204-201'),
             ('inputs.lookup', 'SINOPAC-03-205-001'),
             ('inputs.count', 'SINOPAC-03-208-001'),
             ('inputs.nth', 'SINOPAC-03-206-001'),
             ('inputs.visible', 'SINOPAC-03-207-201'),
             ('inputs.attribute', 'SINOPAC-03-207-202'),
             ('body.lookup', 'SINOPAC-03-210-202'),
             ('body.text', 'SINOPAC-03-210-201')],
 'fubon': [('url', 'FUBON-03-200-001'),
           ('frames', 'FUBON-03-201-001'),
           ('frames:2', 'FUBON-03-202-001'),
           ('frames:3', 'FUBON-03-203-001'),
           ('frames:4', 'FUBON-03-205-001'),
           ('frames:5', 'FUBON-03-206-001'),
           ('controls.lookup', 'FUBON-03-207-001'),
           ('controls.nth', 'FUBON-03-208-301'),
           ('controls.handle', 'FUBON-03-208-302'),
           ('controls.visible', 'FUBON-03-208-001'),
           ('body.lookup', 'FUBON-03-209-001'),
           ('body.nth', 'FUBON-03-210-301'),
           ('body.handle', 'FUBON-03-210-302'),
           ('body.text', 'FUBON-03-210-001')],
 'dbs': [('#username.lookup', 'DBS-03-014-002'),
         ('#username.count', 'DBS-03-702-002'),
         ('#username.nth', 'DBS-03-701-002'),
         ('#username.visible', 'DBS-03-700-002'),
         ('#password.lookup', 'DBS-03-014-003'),
         ('#password.count', 'DBS-03-702-003'),
         ('#password.nth', 'DBS-03-701-003'),
         ('#password.visible', 'DBS-03-700-003'),
         ('#loginbutton.lookup', 'DBS-03-014-004'),
         ('#loginbutton.count', 'DBS-03-702-004'),
         ('#loginbutton.nth', 'DBS-03-701-004'),
         ('#loginbutton.visible', 'DBS-03-700-004'),
         ('body.lookup', 'DBS-03-016-001'),
         ('body.count', 'DBS-03-705-001'),
         ('body.nth', 'DBS-03-704-001'),
         ('body.text', 'DBS-03-703-001')],
 'scb': [('controls.lookup', 'SCB-03-010-001'),
         ('controls.count', 'SCB-03-702-001'),
         ('controls.nth', 'SCB-03-701-001'),
         ('controls.visible', 'SCB-03-700-001'),
         ('body.evaluate', 'SCB-03-012-001')],
 'taishin': [('controls.lookup', 'TAISHIN-03-006-001'),
             ('controls.count', 'TAISHIN-03-709-001'),
             ('controls.nth', 'TAISHIN-03-708-001'),
             ('controls.visible', 'TAISHIN-03-707-001'),
             ('body.evaluate', 'TAISHIN-03-008-001')],
 'rakuten': [('body.evaluate', 'RAKUTEN-03-003-001')],
 'linebank': [('body.evaluate', 'LINEBANK-03-008-001')],
 'ctbc': [('url', 'CTBC-03-010-001'), ('body.evaluate', 'CTBC-03-011-001')],
 'cathay': [('url', 'CATHAY-03-010-001'), ('body.evaluate', 'CATHAY-03-011-001')]}
DASHBOARD_CASES = [
    ('layout.count:2', 'ESUN-03-404-100'), ('layout.nth', 'ESUN-03-404-101'),
    ('layout.visible', 'ESUN-03-404-102'), ('layout.nth:2', 'ESUN-03-405-001'),
    ('dashboard.lookup', 'ESUN-03-406-001'), ('not-login.lookup', 'ESUN-03-407-100'),
    ('not-login.count', 'ESUN-03-407-101'), ('temp.lookup', 'ESUN-03-407-102'),
    ('temp.count', 'ESUN-03-407-103'), ('cpo.lookup', 'ESUN-03-407-104'),
    ('cpo.count', 'ESUN-03-407-105'), ('dashboard.count', 'ESUN-03-407-106'),
    ('dashboard.nth', 'ESUN-03-407-107'), ('dashboard.visible', 'ESUN-03-407-108'),
]
NATIVE_PARAMS = [(case, site, expected, False) for case in BANK_CASES
                 for site, expected in NATIVE_CASES[case[0]]]
NATIVE_PARAMS += [(BANK_CASES[2], site, expected, True) for site, expected in DASHBOARD_CASES]
BODY = '登出 Logout 我的卡片 卡片清單 信用卡 Hello Last Login My Overview TWD Deposit Credit Card 我的總覽 台幣存款 帳戶總覽 資產總覽 我的帳戶 理財總覽 帳戶綜覽 ' + '合成' * 600


class ProbeLocator(Locator):
    def __init__(self, page, role, exists=True):
        super().__init__([])
        self.page, self.role, self.exists = page, role, exists
        self.handle = object()

    def count(self):
        self.page.event(self.role + '.count')
        if self.role in ('not-login', 'temp'):
            return 0
        return int(self.exists)

    def nth(self, index):
        self.page.event(self.role + '.nth', index)
        if index or not self.exists:
            raise IndexError
        return self

    def element_handle(self, **kwargs):
        self.page.event(self.role + '.handle', **kwargs)
        return self.handle if self.exists else None

    def is_visible(self):
        self.page.event(self.role + '.visible')
        return self.role in ('inputs', 'layout', 'dashboard')

    def get_attribute(self, name):
        self.page.event(self.role + '.attribute', name)
        return '5'

    def inner_text(self, **kwargs):
        self.page.event(self.role + '.text', **kwargs)
        return BODY if self.page.healthy else ''

    def locator(self, selector):
        return self.page.locator(selector)


class ProbePage(AuthPage):
    def __init__(self, case, site=None, *, dashboard=False, healthy=True):
        super().__init__(case[2], fault=None)
        self.bank = case[0]
        self.site = site
        self.dashboard = dashboard
        self.healthy = healthy
        self.counts = {}
        self.in_probe = False

    @property
    def frames(self):
        if getattr(self, 'in_probe', False):
            self.event('frames')
        return self._frames

    @frames.setter
    def frames(self, value):
        self._frames = value

    @property
    def url(self):
        if getattr(self, 'in_probe', False):
            self.event('url')
        return self._url

    @url.setter
    def url(self, value):
        self._url = value

    def event(self, site, *args, **kwargs):
        self.counts[site] = self.counts.get(site, 0) + 1
        self.trace.append((site, args, kwargs))
        key = site if self.counts[site] == 1 else site + ':' + str(self.counts[site])
        if key == self.site:
            raise self.failure

    def locator(self, selector):
        role = ('body' if selector == 'body' else
                'not-login' if selector == '#layout-content.not-login' else
                'layout' if selector == '#layout-content' else
                'temp' if selector == '.temp-index:visible' else
                'cpo' if selector == '.cpo08003' else
                'dashboard' if selector == '.cpo08003 > .index' else
                'inputs' if selector == 'input' else
                'captcha' if self.bank == 'sinopac' else
                selector if self.bank in ('hsbc', 'dbs') else 'controls')
        self.event(role + '.lookup', selector)
        return ProbeLocator(self, role, exists=role != 'layout' or self.dashboard)

    def evaluate(self, script):
        self.event('body.evaluate', script)
        return self.healthy if self.bank in ('ubot', 'rakuten', 'linebank', 'ctbc', 'cathay') else BODY if self.healthy else ''


def instrument_public_probe(crawler, page, *, settle_fault=False):
    original = crawler.is_authenticated
    fault = page.site
    dashboard = page.dashboard
    observations = 0

    def authenticated(page):
        nonlocal observations
        observations += 1
        if settle_fault:
            page.site = fault if observations == 3 else None
            page.healthy = observations == 2
            page.dashboard = dashboard and observations > 1
        page.in_probe = True
        page.counts.clear()
        try:
            return original(page)
        finally:
            page.in_probe = False

    crawler.is_authenticated = authenticated


@pytest.mark.parametrize('case,site,expected,dashboard', NATIVE_PARAMS,
                         ids=[c[0] + '-' + site for c, site, _, _ in NATIVE_PARAMS])
@pytest.mark.parametrize('settle', [False, True], ids=['post', 'settle'])
def test_every_native_role_reaches_real_run_sink(monkeypatch, case, site, expected, dashboard, settle):
    crawler = crawler_for(case)
    page = ProbePage(case, site, dashboard=dashboard)
    instrument_public_probe(crawler, page, settle_fault=settle)
    result, terminals = run_offline(monkeypatch, crawler, page)
    assert exception_error_id(page.failure) == expected
    assert result['failure_diagnostics']['error_id'] == expected
    terminal, = terminals
    assert terminal.outcome.error_id == expected
    assert terminal.phase is (cp.CheckpointPhase.POST_SUBMIT_SETTLE if settle else cp.CheckpointPhase.POST_SUBMIT)
    assert terminal.budget == cp.LoginBudget(credential_submissions=1)
    assert terminal.__cause__ is None and terminal.__context__ is None
    assert format_error_id(expected) in result['error']
    assert any('\u4e00' <= c <= '\u9fff' for c in result['error'])
    assert type(page.failure) is NativeFailure
    assert page.trace.count(('submit', (), {})) == 1
    assert page.default_timeout == 180000


@pytest.mark.parametrize('case', BANK_CASES, ids=lambda c: c[0])
@pytest.mark.parametrize('states,expected_suffix', [
    ((False, False), '988-001'), ((False, True, False), '988-002'),
    ((False, True, True), None),
])
def test_fresh_observations_and_repeated_invocations(case, states, expected_suffix):
    crawler = crawler_for(case)
    real = crawler.is_authenticated
    for _ in range(2):
        page = ProbePage(case)
        pending = iter(states)
        observations = []

        def observe(page):
            positive = next(pending)
            page.site = NATIVE_CASES[case[0]][0][0] if not observations else None
            page.healthy = positive
            page.counts.clear()
            page.in_probe = True
            cell = crawler._login_auth_probe_ids
            assert cell == []
            try:
                value = real(page)
                observations.append((value, list(cell)))
                return value
            finally:
                page.in_probe = False

        crawler.is_authenticated = observe
        if expected_suffix is None:
            assert crawler._shared_login(page) is True
        else:
            with pytest.raises(cp.LoginCheckpointBlocked) as caught:
                crawler._shared_login(page)
            assert caught.value.outcome.error_id == case[0].upper() + '-03-' + expected_suffix
        assert observations[0][1] == [NATIVE_CASES[case[0]][0][1]]
        assert all(cell == [] for _, cell in observations[1:])
        assert page.trace.count(('submit', (), {})) == 1
        assert not hasattr(crawler, '_login_auth_probe_ids')


@pytest.mark.parametrize('case', BANK_CASES, ids=lambda c: c[0])
def test_private_probe_clears_same_cell_on_healthy_positive_and_negative(case):
    crawler = crawler_for(case)
    cell = ['stale']
    failed = AuthPage(case[2])
    assert crawler._logged_in(failed, _auth_probe_ids=cell) is False
    assert cell == [case[3]]
    for healthy in (False, True, False):
        assert crawler._logged_in(ProbePage(case, healthy=healthy), _auth_probe_ids=cell) is healthy
        assert cell == []


class HostileMetadata:
    def fail(self, *args, **kwargs):
        raise AssertionError('metadata hooks must not execute')
    __str__ = __repr__ = __hash__ = __eq__ = __bool__ = __getattr__ = fail


@pytest.mark.parametrize('value', [
    'CTBC-03-999-999', 'private', 'CATHAY-03-011-001', 'SYS-03-988-001',
    'CTBC-03-988-001', 'CTBC-04-010-001', 42, None, HostileMetadata(),
], ids=['unknown', 'malformed', 'foreign', 'system', 'shared', 'unrelated-bank-leaf',
        'integer', 'none', 'hostile'])
def test_consumer_rejects_non_native_metadata_without_coercion(value):
    cell = [value]
    result = cp.evaluate_login_checkpoint(
        Page(), bank='ctbc', phase=cp.CheckpointPhase.POST_SUBMIT,
        rules=(), is_authenticated=lambda page: False, _auth_probe_ids=cell,
    )
    assert result.error_id == 'CTBC-03-988-001'
    assert cell == []


@pytest.mark.parametrize('case', BANK_CASES, ids=lambda c: c[0])
def test_producer_rejects_unrelated_or_foreign_registered_causes(case):
    crawler = crawler_for(case)
    for error_id in ('CTBC-04-010-001', case[0].upper() + '-03-988-001', 'SYS-03-988-001'):
        page = AuthPage(case[2])
        with_error_code(page.failure, error_id)
        cell = []
        assert crawler._logged_in(page, _auth_probe_ids=cell) is False
        assert cell == []


@pytest.mark.parametrize('prior', [False, True])
@pytest.mark.parametrize('escape', [None, RuntimeError, asyncio.CancelledError])
def test_exact_binding_restored_and_cell_cleared_on_every_exit(monkeypatch, prior, escape):
    crawler = crawler_for(BANK_CASES[-2])
    previous = object()
    if prior:
        crawler._login_auth_probe_ids = previous
    cells = []
    original = crawler.is_authenticated

    def authenticated(page):
        cells.append(crawler._login_auth_probe_ids)
        if escape is not None:
            raise escape('synthetic escaping observation')
        return original(page)

    crawler.is_authenticated = authenticated
    page = AuthPage(BANK_CASES[-2][2])
    expected = asyncio.CancelledError if escape is asyncio.CancelledError else cp.LoginCheckpointBlocked
    with pytest.raises(expected):
        crawler._shared_login(page)
    assert cells and all(type(cell) is list and cell == [] for cell in cells)
    if prior:
        assert crawler._login_auth_probe_ids is previous
    else:
        assert '_login_auth_probe_ids' not in crawler.__dict__


def test_nested_binding_restores_outer_cell():
    case = BANK_CASES[-2]
    crawler = crawler_for(case)
    real = crawler.is_authenticated
    previous = object()
    crawler._login_auth_probe_ids = previous
    cells = []
    inside = False

    def authenticated(page):
        nonlocal inside
        current = crawler._login_auth_probe_ids
        assert current == []
        cells.append(current)
        if not inside:
            inside = True
            try:
                with pytest.raises(cp.LoginCheckpointBlocked):
                    crawler._shared_login(AuthPage(case[2]))
                assert crawler._login_auth_probe_ids is current
            finally:
                inside = False
        return real(page)

    crawler.is_authenticated = authenticated
    with pytest.raises(cp.LoginCheckpointBlocked):
        crawler._shared_login(AuthPage(case[2]))
    assert crawler._login_auth_probe_ids is previous
    assert all(cell == [] for cell in cells)
    assert len({id(cell) for cell in cells}) >= 4


def test_rakuten_modal_short_circuit_resets_before_whole_callback(monkeypatch):
    case = BANK_CASES[9]
    crawler = crawler_for(case)
    seen = []
    crawler._modal_failure_id = 'unchanged-synthetic-modal'
    crawler._semantic_modal_visible_in_owned_scope = lambda page: True
    crawler._logged_in = lambda *a, **k: pytest.fail('modal must short circuit native auth')
    evaluate = cp.evaluate_login_checkpoint

    def repeat_callback(page, **kwargs):
        cell = kwargs['_auth_probe_ids']
        cell[:] = ['RAKUTEN-03-003-001']
        assert kwargs['is_authenticated'](page) is False
        assert cell == []
        seen.append(cell)
        return evaluate(page, **kwargs)

    monkeypatch.setattr(base, 'evaluate_login_checkpoint', repeat_callback)
    with pytest.raises(cp.LoginCheckpointBlocked) as caught:
        crawler._shared_login(AuthPage(case[2]))
    assert caught.value.outcome.error_id == 'RAKUTEN-03-988-001'
    assert crawler._modal_failure_id == 'unchanged-synthetic-modal'
    assert len(seen) == 2 and seen[0] is not seen[1]
    assert not hasattr(crawler, '_login_auth_probe_ids')


def old_runtime(case):
    """Immutable public source only; missing or changed originals fail closed."""
    import textwrap
    from tests.diagnostic_originals import auth_original_source

    def load(path, name, namespace):
        source = auth_original_source(path)
        node = next(n for n in ast.walk(ast.parse(source))
                    if isinstance(n, ast.FunctionDef) and n.name == name)
        exec(compile(textwrap.dedent(ast.get_source_segment(source, node)), f"auth-original:{path}", 'exec'), namespace)
        return namespace[name]

    cp_globals = vars(cp).copy()
    for name in ('_blocked_checkpoint', '_evaluate_login_checkpoint', 'evaluate_login_checkpoint'):
        load('login_checkpoints.py', name, cp_globals)
    base_globals = vars(base).copy()
    base_globals['evaluate_login_checkpoint'] = cp_globals['evaluate_login_checkpoint']
    shared = load('base.py', '_shared_login', base_globals)
    bank_globals = vars(import_module('backend.banks.' + case[0])).copy()
    logged_in = load(f'{case[0]}.py', '_logged_in', bank_globals)
    authenticated = load(f'{case[0]}.py', 'is_authenticated', bank_globals)
    return shared, logged_in, authenticated


def lifecycle_trace(crawler, page, shared):
    try:
        result = shared(page)
        business = ('success', result)
        links = None
    except cp.LoginCheckpointTerminal as error:
        business = (type(error), str(error), error.phase, error.budget,
                    error.outcome.kind, error.outcome.rule_name,
                    error.outcome.action_label, error.outcome.interaction)
        links = (error.__cause__, error.__context__, error.__suppress_context__)
    return business, page.trace, links


@pytest.mark.parametrize('case,site,expected,dashboard', NATIVE_PARAMS,
                         ids=[c[0] + '-' + site for c, site, _, _ in NATIVE_PARAMS])
@pytest.mark.parametrize('settle', [False, True], ids=['post', 'settle'])
def test_original_source_order_arguments_timeouts_and_terminal_links(case, site, expected, dashboard, settle):
    from types import MethodType
    shared, logged_in, authenticated = old_runtime(case)
    old = crawler_for(case)
    old._logged_in = MethodType(logged_in, old)
    old.is_authenticated = MethodType(authenticated, old)
    old_page = ProbePage(case, site, dashboard=dashboard)
    instrument_public_probe(old, old_page, settle_fault=settle)
    before = lifecycle_trace(old, old_page, MethodType(shared, old))
    current = crawler_for(case)
    page = ProbePage(case, site, dashboard=dashboard)
    instrument_public_probe(current, page, settle_fault=settle)
    after = lifecycle_trace(current, page, current._shared_login)
    assert after == before
    assert (page.failure.__cause__, page.failure.__context__, page.failure.__suppress_context__) == (
        old_page.failure.__cause__, old_page.failure.__context__, old_page.failure.__suppress_context__)
    assert page.trace.count(('submit', (), {})) == 1


@pytest.mark.parametrize('case', BANK_CASES, ids=lambda c: c[0])
@pytest.mark.parametrize('healthy', [False, True])
def test_original_source_normal_positive_negative_traces(case, healthy):
    from types import MethodType
    shared, logged_in, authenticated = old_runtime(case)
    old = crawler_for(case)
    old._logged_in = MethodType(logged_in, old)
    old.is_authenticated = MethodType(authenticated, old)
    old_page = ProbePage(case, healthy=healthy)
    instrument_public_probe(old, old_page)
    before = lifecycle_trace(old, old_page, MethodType(shared, old))
    crawler = crawler_for(case)
    page = ProbePage(case, healthy=healthy)
    instrument_public_probe(crawler, page)
    assert lifecycle_trace(crawler, page, crawler._shared_login) == before
    assert page.trace.count(('submit', (), {})) == int(not healthy)


RULE_KINDS = [cp.CheckpointKind.OTP_REQUIRED, cp.CheckpointKind.PASSWORD_CHANGE_REQUIRED,
              cp.CheckpointKind.EXPLICIT_LOGIN_ERROR, cp.CheckpointKind.UNKNOWN_BLOCKER,
              cp.CheckpointKind.PASSWORD_CHANGE_OPTIONAL, cp.CheckpointKind.DISMISSIBLE_NOTICE,
              cp.CheckpointKind.DUPLICATE_SESSION, cp.CheckpointKind.PROTOCOL_RESUBMIT,
              cp.CheckpointKind.CAPTCHA_RETRY, cp.CheckpointKind.STARTUP_RECOVERY]


def rule_flow(case, kind, *, old=False):
    from types import MethodType
    from tests.test_login_checkpoint_browser_rules import Node
    crawler = crawler_for(case)
    shared = crawler._shared_login
    if old:
        shared_fn, logged_in, authenticated = old_runtime(case)
        crawler._logged_in = MethodType(logged_in, crawler)
        crawler.is_authenticated = MethodType(authenticated, crawler)
        shared = MethodType(shared_fn, crawler)
    page = ProbePage(case, NATIVE_CASES[case[0]][0][0], healthy=False)
    action = Node(text='Continue')
    container = Node(text='synthetic rule', queries={cp.DEFAULT_ACTION_SELECTOR: [action]})
    classifier = kind in (cp.CheckpointKind.OTP_REQUIRED, cp.CheckpointKind.PASSWORD_CHANGE_REQUIRED,
                          cp.CheckpointKind.EXPLICIT_LOGIN_ERROR, cp.CheckpointKind.UNKNOWN_BLOCKER)
    phases = (cp.CheckpointPhase.PRE_SUBMIT,) if classifier or kind is cp.CheckpointKind.STARTUP_RECOVERY else (cp.CheckpointPhase.POST_SUBMIT,)
    clickable = kind in (cp.CheckpointKind.PASSWORD_CHANGE_OPTIONAL, cp.CheckpointKind.DISMISSIBLE_NOTICE,
                         cp.CheckpointKind.DUPLICATE_SESSION, cp.CheckpointKind.PROTOCOL_RESUBMIT)
    rule = cp.LoginCheckpointRule(
        name='synthetic-rule', bank=case[0], phases=phases, kind=kind,
        container_selector='#checkpoint', action_texts=('Continue',) if clickable else (),
    )
    crawler.login_checkpoint_rules = lambda: (rule,)
    native_locator = page.locator

    def locator(selector):
        if selector == '#checkpoint':
            page.trace.append(('rule.lookup', (selector,), {}))
            return Locator([container] if container.visible else [])
        return native_locator(selector)

    page.locator = locator

    def progressed(label):
        assert '_login_auth_probe_ids' not in crawler.__dict__
        page.trace.append((label, (), {}))
        page.site = None
        container.visible = False

    action.on_click = lambda node: progressed('rule.click')
    page.reload = lambda: progressed('reload')
    crawler.prepare_captcha_resubmit = lambda page: progressed('captcha.prepare')
    submit = crawler._submit_with_captcha_reloads

    def submission(page, submit_page):
        submit(page, submit_page)
        if kind is cp.CheckpointKind.CAPTCHA_RETRY and page.trace.count(('submit', (), {})) == 2:
            container.visible = False

    crawler._submit_with_captcha_reloads = submission
    instrument_public_probe(crawler, page)
    result = lifecycle_trace(crawler, page, shared)
    return result, page.trace.count(('submit', (), {}))


@pytest.mark.parametrize('case', BANK_CASES, ids=lambda c: c[0])
@pytest.mark.parametrize('kind', RULE_KINDS)
def test_rules_recovery_stage_clear_match_original_source(case, kind):
    before, sends = rule_flow(case, kind, old=True)
    after, current_sends = rule_flow(case, kind)
    assert after == before
    assert current_sends == sends
    if kind in (cp.CheckpointKind.OTP_REQUIRED, cp.CheckpointKind.PASSWORD_CHANGE_REQUIRED,
                cp.CheckpointKind.EXPLICIT_LOGIN_ERROR, cp.CheckpointKind.UNKNOWN_BLOCKER):
        assert sends == 0
    elif kind in (cp.CheckpointKind.PROTOCOL_RESUBMIT, cp.CheckpointKind.CAPTCHA_RETRY):
        assert sends == 2
    else:
        assert sends == 1


@pytest.mark.parametrize('case', BANK_CASES, ids=lambda c: c[0])
@pytest.mark.parametrize('kind', [cp.CheckpointKind.OTP_REQUIRED, cp.CheckpointKind.PASSWORD_CHANGE_REQUIRED,
                                cp.CheckpointKind.EXPLICIT_LOGIN_ERROR, cp.CheckpointKind.UNKNOWN_BLOCKER])
def test_pending_id_never_replaces_known_rules_in_real_run(monkeypatch, case, kind):
    from tests.test_login_checkpoint_browser_rules import Node
    crawler = crawler_for(case)
    page = AuthPage(case[2])
    native = page.locator
    page.locator = lambda selector: Locator([Node(text='synthetic')]) if selector == '#known' else native(selector)
    rule = cp.LoginCheckpointRule(name='known', bank=case[0], phases=tuple(cp.CheckpointPhase),
                                  kind=kind, container_selector='#known')
    crawler.login_checkpoint_rules = lambda: (rule,)
    result, terminals = run_offline(monkeypatch, crawler, page)
    terminal, = terminals
    assert terminal.outcome.kind is kind
    assert terminal.outcome.rule_name == 'known'
    assert terminal.outcome.error_id is None
    assert result['failure_diagnostics']['error_id'] != case[3]
    assert page.trace.count(('submit', (), {})) == 0


@pytest.mark.parametrize('fault', [IndexError, StopIteration, TimeoutError])
def test_fubon_optional_candidate_nonmatch_is_not_failure(fault):
    case = BANK_CASES[5]
    crawler = crawler_for(case)
    page = ProbePage(case)
    original = page.event

    def event(site, *args, **kwargs):
        original(site, *args, **kwargs)
        if site == 'controls.handle':
            raise fault()

    page.event = event
    cell = ['stale']
    assert crawler._logged_in(page, _auth_probe_ids=cell) is True
    assert cell == []


@pytest.mark.parametrize('case', BANK_CASES, ids=lambda c: c[0])
@pytest.mark.parametrize('guard', ['origin', 'dialog'])
def test_pending_probe_does_not_override_origin_or_dialog_rejection(case, guard):
    crawler = crawler_for(case)
    page = AuthPage(case[2])
    original = crawler.is_authenticated
    admitted = True

    def authenticated(page):
        nonlocal admitted
        result = original(page)
        if guard == 'origin':
            admitted = False
        else:
            crawler._shared_dialog_blocked = True
        return result

    crawler.is_authenticated = authenticated
    crawler._credential_origin_allowed = lambda page: admitted
    with pytest.raises(cp.LoginCheckpointBlocked) as caught:
        crawler._shared_login(page)
    suffix = '940-003' if guard == 'origin' else '940-004'
    assert exception_error_id(caught.value) == case[0].upper() + '-03-' + suffix
    assert caught.value.outcome.error_id is None
    assert page.trace.count(('submit', (), {})) == 0
    assert '_login_auth_probe_ids' not in crawler.__dict__


@pytest.mark.parametrize('site', ['bank_validation', 'timeout_enter', 'timeout_restore', 'authentication'])
def test_evaluation_escape_always_clears_the_cell(site):
    cell = ['CTBC-03-011-001']
    page = Page()
    error = asyncio.CancelledError()

    def fail():
        raise error

    class Rule:
        @property
        def bank(self):
            return fail()

    if site.startswith('timeout_'):
        timeout = page.set_default_timeout

        def set_timeout(value):
            if value == (5000 if site == 'timeout_enter' else 180000):
                fail()
            timeout(value)

        page.set_default_timeout = set_timeout
    with pytest.raises(asyncio.CancelledError) as caught:
        cp.evaluate_login_checkpoint(
            page, bank='ctbc', phase=cp.CheckpointPhase.POST_SUBMIT,
            rules=(Rule(),) if site == 'bank_validation' else (),
            is_authenticated=lambda page: fail() if site == 'authentication' else False,
            _auth_probe_ids=cell,
        )
    assert caught.value is error
    assert cell == []


def test_terminal_receives_native_outcome_before_constructor(monkeypatch):
    seen = []
    constructor = cp.LoginCheckpointTerminal.__init__

    def init(self, budget, outcome, **kwargs):
        seen.append(outcome.error_id)
        constructor(self, budget, outcome, **kwargs)

    monkeypatch.setattr(cp.LoginCheckpointTerminal, '__init__', init)
    case = BANK_CASES[-2]
    crawler = crawler_for(case)
    with pytest.raises(cp.LoginCheckpointBlocked) as caught:
        crawler._shared_login(AuthPage(case[2]))
    assert seen == ['CTBC-03-011-001']
    assert exception_error_id(caught.value) == seen[0]
    assert caught.value.__cause__ is None


@pytest.mark.parametrize('cause_id', ['CTBC-03-010-001', 'CTBC-04-010-001'])
def test_existing_explicit_cause_precedence_and_python_links_are_unchanged(cause_id):
    case = BANK_CASES[-2]
    crawler = crawler_for(case)
    page = AuthPage(case[2])
    cause = with_error_code(NativeFailure('synthetic cause'), cause_id)
    page.failure.__cause__ = cause
    before = (page.failure.__cause__, page.failure.__context__, page.failure.__suppress_context__)
    cell = []
    assert crawler._logged_in(page, _auth_probe_ids=cell) is False
    assert cell == (['CTBC-03-010-001'] if cause_id == 'CTBC-03-010-001' else [])
    assert (page.failure.__cause__, page.failure.__context__, page.failure.__suppress_context__) == before


def test_inherited_prior_binding_restores_without_instance_shadow(monkeypatch):
    case = BANK_CASES[-2]
    crawler = crawler_for(case)
    inherited = ['preexisting binding']
    monkeypatch.setattr(type(crawler), '_login_auth_probe_ids', inherited, raising=False)
    with pytest.raises(cp.LoginCheckpointBlocked):
        crawler._shared_login(AuthPage(case[2]))
    assert crawler._login_auth_probe_ids is inherited
    assert '_login_auth_probe_ids' not in crawler.__dict__
    assert inherited == ['preexisting binding']
