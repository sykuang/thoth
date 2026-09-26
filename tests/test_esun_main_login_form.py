"""Native synthetic reproduction of E.SUN's public main-page login form."""
from types import SimpleNamespace

import pytest
from patchright.sync_api import sync_playwright

from backend.banks.esun import EsunCrawler, EsunLoginError
from backend.core.login_checkpoints import CheckpointKind, CheckpointPhase, evaluate_login_checkpoint


FORM = '''<meta charset="utf-8"><form>
<input id="dynamic-a" name="id" type="text" maxlength="10">
<input name="rememberId" type="checkbox">
<input id="dynamic-b" name="userName" type="password" maxlength="15">
<input id="dynamic-c" name="pxssword" type="password" maxlength="15">
<button type="button">登入</button></form>
<script>document.body.dataset.submits='0';document.querySelector('button').onclick=()=>document.body.dataset.submits=String(Number(document.body.dataset.submits)+1);</script>'''


@pytest.fixture
def page():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.route("**/*", lambda route: route.fulfill(content_type="text/html", body=FORM))
        page.goto("https://ebank.esunbank.com.tw/")
        try:
            yield page
        finally:
            browser.close()


def test_main_page_form_is_discovered_without_legacy_iframe(page):
    crawler = object.__new__(EsunCrawler)
    assert crawler._find_login_frame(page) is page.main_frame
    assert crawler._logged_in(page) is False


def test_main_page_submit_uses_names_once_without_remembering_id(page, monkeypatch):
    crawler = object.__new__(EsunCrawler)
    crawler.creds = SimpleNamespace(national_id="TEST-ID", user_code="TEST-USER", password="TEST-PASS")
    monkeypatch.setattr(page, "wait_for_timeout", lambda ms: None)
    from backend.core.base import _OriginGuardProxy
    def guard():
        if not crawler._credential_origin_allowed(page):
            raise RuntimeError('synthetic origin denied')
    crawler.submit_credentials_once(_OriginGuardProxy(page, guard))
    assert page.locator('input[name="id"]').input_value() == "TEST-ID"
    assert page.locator('input[name="userName"]').input_value() == "TEST-USER"
    assert page.locator('input[name="pxssword"]').input_value() == "TEST-PASS"
    assert not page.locator('input[name="rememberId"]').is_checked()
    assert page.locator('body').get_attribute('data-submits') == "1"


@pytest.mark.parametrize("drift_at", ["click", "backspace", "typing"])
def test_legacy_iframe_navigation_to_foreign_origin_stops_before_password(page, monkeypatch, drift_at):
    from backend.core.base import _OriginGuardProxy

    crawler = object.__new__(EsunCrawler)
    crawler.creds = SimpleNamespace(national_id="TEST-ID", user_code="TEST-USER", password="TEST-PASS")
    base = "https://ebank.esunbank.com.tw"
    foreign = "https://foreign.invalid"
    frame_form = '''<form>
      <input id="loginform:custid" maxlength="10">
      <input id="loginform:name" type="password" maxlength="15">
      <input id="loginform:pxsswd" type="password" maxlength="15"
        onclick="location.href='https://foreign.invalid/login'">
      <a id="loginform:linkCommand" class="login_btn">登入</a>
    </form>'''
    if drift_at == "backspace":
        frame_form = frame_form.replace("onclick=\"location.href='https://foreign.invalid/login'\"", "")
    elif drift_at == "typing":
        frame_form = frame_form.replace("onclick=", "oninput=")
    foreign_form = '''<form><input id="loginform:custid" maxlength="10">
      <input id="loginform:name" type="password" maxlength="15">
      <input id="loginform:pxsswd" type="password" maxlength="15" autofocus>
      <a id="loginform:linkCommand" class="login_btn" onclick="document.body.dataset.submitted='yes'">登入</a>
    </form><script>document.getElementById('loginform:pxsswd').focus()</script>'''

    def route(request):
        url = request.request.url
        if url == base + "/":
            request.fulfill(content_type="text/html", body='<iframe name="iframe1" src="/fco/fco08001/FCO08001_Home.faces"></iframe>')
        elif url == base + "/fco/fco08001/FCO08001_Home.faces":
            request.fulfill(content_type="text/html", body=frame_form)
        elif url == foreign + "/login":
            request.fulfill(content_type="text/html", body=foreign_form)
        else:
            request.abort()

    page.route("**/*", route)
    page.goto(base + "/")
    if drift_at == "backspace":
        original_press = page.keyboard.press

        def press(key):
            frame = next(frame for frame in page.frames if frame is not page.main_frame)
            at_password = frame.locator(":focus").get_attribute("id") == "loginform:pxsswd"
            original_press(key)
            if at_password:
                frame.goto(foreign + "/login")
                frame.locator("#loginform\\:pxsswd").focus()

        monkeypatch.setattr(page.keyboard, "press", press)
    page.wait_for_timeout = lambda ms: None
    guard = lambda: None if crawler._credential_origin_allowed(page) else (_ for _ in ()).throw(RuntimeError("foreign top origin"))
    guarded_page = _OriginGuardProxy(page, guard)
    with pytest.raises(EsunLoginError):
        crawler.submit_credentials_once(guarded_page)
    frame = next(frame for frame in page.frames if frame is not page.main_frame)
    assert frame.url == foreign + "/login"
    assert frame.locator("#loginform\\:pxsswd").input_value() == ""
    assert frame.locator("body").get_attribute("data-submitted") is None


def test_dialog_during_final_password_read_cannot_submit(page, monkeypatch):
    from patchright.sync_api import Locator
    from backend.core import base

    crawler = object.__new__(EsunCrawler)
    crawler.name = "esun"
    crawler.creds = SimpleNamespace(national_id="TEST-ID", user_code="TEST-USER", password="TEST-PASS")
    crawler.attach_shared_dialog_handler(page)
    monkeypatch.setattr(crawler, "prepare_login_page", lambda page: None)
    monkeypatch.setattr(crawler, "login_checkpoint_rules", lambda: ())
    monkeypatch.setattr(base, "evaluate_login_checkpoint", lambda *args, **kwargs:
                        base.CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS))
    monkeypatch.setattr(page, "wait_for_timeout", lambda ms: None)
    original_read = Locator.input_value

    def read_with_alert(locator, **kwargs):
        value = original_read(locator, **kwargs)
        if locator.get_attribute("name") == "pxssword":
            page.evaluate("alert('SYNTHETIC-OPAQUE-DIALOG')")
        return value

    monkeypatch.setattr(Locator, "input_value", read_with_alert)
    with pytest.raises((EsunLoginError, base.LoginCheckpointBlocked)):
        crawler._shared_login(page)
    assert crawler._shared_dialog_blocked is True
    assert page.locator("body").get_attribute("data-submits") == "0"


def test_visible_main_login_is_not_an_authenticated_dashboard(page):
    page.locator('body').evaluate("el => el.append(document.createTextNode('訊息中心 個人資訊 登出 帳戶總覽 歡迎使用 存款 轉帳 信用卡 ' + 'x'.repeat(600)))")
    assert object.__new__(EsunCrawler)._logged_in(page) is False


def test_visible_main_login_stops_post_submit_checkpoint(page):
    crawler = object.__new__(EsunCrawler)
    outcome = evaluate_login_checkpoint(
        page, bank="esun", phase=CheckpointPhase.POST_SUBMIT,
        rules=crawler.login_checkpoint_rules(), is_authenticated=lambda page: False,
    )
    assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
    assert outcome.rule_name == 'esun-main-login-form-still-visible'
    assert page.locator('body').get_attribute('data-submits') == "0"


@pytest.mark.parametrize('mutation', [
    "document.body.append(document.querySelector('form').cloneNode(true))",
    "document.querySelector('form').append(document.querySelector('[name=id]').cloneNode())",
    "document.querySelector('[name=userName]').type='text'",
    "document.querySelector('[name=pxssword]').maxLength=99",
    "document.querySelector('[name=id]').hidden=true",
    "document.querySelector('[name=id]').disabled=true",
    "document.querySelector('form').append(document.querySelector('button').cloneNode(true))",
    "document.querySelector('button').textContent='確認轉帳'",
    "document.querySelector('button').setAttribute('form','nonexistent')",
    "document.querySelector('[name=id]').setAttribute('form','nonexistent')",
])
def test_invalid_main_contract_stops_before_any_credential_write(page, mutation):
    page.evaluate(mutation)
    crawler = object.__new__(EsunCrawler)
    crawler.creds = SimpleNamespace(national_id='TEST-ID', user_code='TEST-USER', password='TEST-PASS')
    with pytest.raises(EsunLoginError, match='未送出登入'):
        crawler.submit_credentials_once(page)
    assert page.locator('body').get_attribute('data-submits') == '0'
    assert page.locator('input:not([type=checkbox])').evaluate_all('els => els.every(e => !e.value)') is True


def test_foreign_main_form_stops_before_any_credential_write(page):
    page.goto('https://foreign.invalid/')
    crawler = object.__new__(EsunCrawler)
    assert crawler._find_login_frame(page) is None
    assert page.locator('body').get_attribute('data-submits') == '0'
