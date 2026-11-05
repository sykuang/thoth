"""Synthetic native dispatch race: admission must not authorize another document."""
from types import SimpleNamespace

import pytest
from patchright._impl._connection import Channel

from backend.banks.esun import EsunCrawler, EsunLoginError
from backend.core.base import _OriginGuardProxy
from tests.test_esun_main_login_form import FORM, page as page


@pytest.mark.parametrize('form_kind', ['legacy', 'main'])
def test_navigation_after_admission_before_character_dispatch_cannot_write_foreign_document(
    page, monkeypatch, form_kind,
):
    crawler = object.__new__(EsunCrawler)
    crawler.creds = SimpleNamespace(national_id='TEST-ID', user_code='TEST-USER', password='TEST-PASS')
    origin = 'https://ebank.esunbank.com.tw'
    foreign = 'https://foreign.invalid/login'
    legacy = '''<form><input id="loginform:custid" maxlength="10">
      <input id="loginform:name" type="password" maxlength="15">
      <input id="loginform:pxsswd" type="password" maxlength="15">
      <a id="loginform:linkCommand" class="login_btn">登入</a></form>'''
    foreign_form = '''<input id="loginform:pxsswd" name="pxssword" type="password" autofocus>
      <script>document.querySelector('input').focus();
      document.body.dataset.received='';
      document.querySelector('input').oninput=e=>document.body.dataset.received+=e.data;</script>'''

    def route(request):
        url = request.request.url
        if url == foreign:
            request.fulfill(content_type='text/html', body=foreign_form)
        elif url == origin + '/':
            body = FORM if form_kind == 'main' else '<iframe name="iframe1" src="/fco/fco08001/FCO08001_Home.faces"></iframe>'
            request.fulfill(content_type='text/html', body=body)
        elif url == origin + '/fco/fco08001/FCO08001_Home.faces':
            request.fulfill(content_type='text/html', body=legacy)
        else:
            request.abort()

    page.route('**/*', route)
    page.goto(origin + '/')
    frame = page.main_frame if form_kind == 'main' else next(f for f in page.frames if f is not page.main_frame)
    original_send = Channel.send
    injected = False

    async def send(channel, method, *args, **kwargs):
        nonlocal injected
        params = kwargs.get('params') if 'params' in kwargs else (args[1] if len(args) > 1 else None)
        if not injected and method in {'keyboardType', 'type'} and params and params.get('text') == 'E':
            at_password = await frame._impl_obj.evaluate(
                "() => document.activeElement?.id === 'loginform:pxsswd' || document.activeElement?.name === 'pxssword'"
            )
            if at_password:
                injected = True
                await frame._impl_obj.goto(foreign, waitUntil='domcontentloaded')
        return await original_send(channel, method, *args, **kwargs)

    monkeypatch.setattr(Channel, 'send', send)
    monkeypatch.setattr(page, 'wait_for_timeout', lambda ms: None)

    def guard():
        if not crawler._credential_origin_allowed(page):
            raise RuntimeError('synthetic foreign origin')

    with pytest.raises(EsunLoginError):
        crawler.submit_credentials_once(_OriginGuardProxy(page, guard))
    assert injected, 'race injection must execute before refusal'
    assert frame.url == foreign
    assert frame.locator('input').input_value() == ''
    assert frame.locator('body').get_attribute('data-received') == ''
