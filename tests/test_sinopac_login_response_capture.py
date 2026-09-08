"""Offline native Chromium contract: intercepted synthetic routes only."""
import html
import json
from types import SimpleNamespace

import pytest

from backend.core.base import BankCrawler, ResponseCollector
from backend.core.creds import SinopacCreds
from backend.banks.sinopac import BASE, LOGIN_RESPONSE_URL, SinopacCrawler, _safe_login_message


def test_login_endpoint_is_not_a_history_contract():
    from backend.core import base
    assert LOGIN_RESPONSE_URL not in base._HISTORY_OBSERVER_URLS.values()


@pytest.mark.parametrize('fault', ['missing', 'rejected', 'untracked', 'wrong_document', 'wrong_shape', 'valid'])
def test_login_capture_requires_its_own_document_and_payload(fault):
    from unittest.mock import Mock
    frame = SimpleNamespace(url=LOGIN_RESPONSE_URL if fault == 'wrong_document' else BASE)
    frame.page = SimpleNamespace(main_frame=frame)
    request = SimpleNamespace(url=LOGIN_RESPONSE_URL, method='POST', post_data='synthetic=1',
                              headers={}, frame=frame, redirected_from=None)
    response = Mock(url=LOGIN_RESPONSE_URL, request=request, status=200,
                    headers={'content-type': 'application/json'})
    collector = ResponseCollector('sinopac.com')
    payload = b'{"value": "not a login response"}' if fault == 'wrong_shape' else b'[{"Header":"FAIL","Message":"synthetic bank notice"}]'
    observer = Mock()
    observer.read.return_value = None if fault == 'rejected' else payload
    if fault != 'missing': collector._history_observer = observer
    if fault != 'untracked': collector._on_request(request)
    collector._on_response(response)
    response.body.assert_not_called()
    response.json.assert_not_called()
    assert len(collector.hits) == 1 and collector.hits[0].req_body is None
    assert (collector.hits[0].resp_json is not None) == (fault == 'valid')
    assert observer.read.call_count == (1 if fault in {'rejected', 'wrong_shape', 'valid'} else 0)


@pytest.mark.parametrize('transport', ['fetch', 'sync_xhr_alert'])
def test_native_login_capture_never_replays_and_drops_request_body(capsys, monkeypatch, transport):
    from patchright.sync_api import Response
    calls = []
    def forbidden(*a, **kw):
        calls.append('replay-capable-read')
        raise AssertionError('Response.body/json forbidden')
    monkeypatch.setattr(Response, 'body', forbidden)
    monkeypatch.setattr(Response, 'json', forbidden)
    from patchright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--host-resolver-rules=MAP * ~NOTFOUND', '--disable-background-networking'])
        page = browser.new_page()
        dialog_state = object.__new__(SinopacCrawler)
        dialog_state._shared_dialog_blocked = False
        BankCrawler.attach_shared_dialog_handler(dialog_state, page)
        requests = []
        def route(r):
            requests.append((r.request.method, r.request.url))
            if r.request.url == BASE:
                script = (
                    f"fetch('{LOGIN_RESPONSE_URL}', {{method:'POST', body:'synthetic-secret'}})"
                    if transport == 'fetch' else
                    f"const x=new XMLHttpRequest();x.open('POST','{LOGIN_RESPONSE_URL}',false);"
                    "x.send('synthetic-secret');alert(JSON.parse(x.responseText)[0].Message)"
                )
                r.fulfill(content_type='text/html', body='<button onclick="' + html.escape(script, quote=True) + '">submit</button>')
            elif r.request.url == LOGIN_RESPONSE_URL:
                r.fulfill(content_type='application/json', body=json.dumps([{'Header': 'FAIL', 'Message': '登入錯誤 PrivateUser', 'Other': 'DO_NOT_RETAIN'}]))
            else:
                r.abort()
        page.route('**/*', route)
        page.goto(BASE)
        collector = ResponseCollector('sinopac.com')
        collector.attach(page)
        page.locator('button').click()
        import time
        deadline = time.monotonic() + 3
        while not collector.by_endpoint('ws_validatecaptcha.ashx') and time.monotonic() < deadline:
            page.wait_for_timeout(20)
        hits = collector.by_endpoint('ws_validatecaptcha.ashx')
        assert len(hits) == 1
        assert hits[0].req_body is None
        assert hits[0].resp_json == [{'Header': 'FAIL', 'Message': '登入錯誤 PrivateUser'}]
        assert hits[0].request_frame_url == BASE
        assert requests == [('GET', BASE), ('POST', LOGIN_RESPONSE_URL)]
        assert calls == []
        assert dialog_state._shared_dialog_blocked == (transport == 'sync_xhr_alert')
        crawler = object.__new__(SinopacCrawler)
        crawler.collector = collector
        crawler.creds = SinopacCreds(national_id='B123456789', user_code='PrivateUser', password='PasswordValue')
        crawler._login_diagnostic_floor = 0
        crawler.log_login_failure_diagnostics(page)
        text = capsys.readouterr().err
        assert '登入錯誤 [REDACTED]' in text and 'WARNING' in text
        assert 'PrivateUser' not in text and 'DO_NOT_RETAIN' not in text
        crawler._login_diagnostic_floor = collector.request_sequence
        crawler.log_login_failure_diagnostics(page)
        assert '登入錯誤' not in capsys.readouterr().err
        collector.detach(page)
        browser.close()


@pytest.mark.parametrize('value', [None, 123, {}, ['text'], 'x' * 4097])
def test_message_invalid_types_and_oversize_suppressed(value):
    creds = SinopacCreds(national_id='B123456789', user_code='PrivateUser', password='PasswordValue')
    assert _safe_login_message(value, creds) is None


@pytest.mark.parametrize('creds', [None, SimpleNamespace(national_id='a', user_code='b', password='c'), SinopacCreds(), SinopacCreds(national_id='id', user_code='user', password=123)])
def test_invalid_credentials_suppress_entire_message(creds):
    assert _safe_login_message('PrivateUser login error', creds) is None


@pytest.mark.parametrize('mode', ['valid', 'foreign', 'hidden', 'duplicate', 'read-failed'])
def test_error_label_owned_visible_unique_only(mode, capsys):
    from unittest.mock import Mock
    crawler = object.__new__(SinopacCrawler)
    crawler.collector = ResponseCollector('sinopac.com')
    crawler.creds = SinopacCreds(national_id='B123456789', user_code='PrivateUser', password='PasswordValue')
    label = Mock()
    label.count.return_value = 2 if mode == 'duplicate' else 1
    label.is_visible.return_value = mode != 'hidden'
    label.inner_text.return_value = '銀行提示 PrivateUser'
    if mode == 'read-failed': label.inner_text.side_effect = RuntimeError('DO_NOT_PRINT')
    page = Mock(url='https://evil.invalid/' if mode == 'foreign' else BASE)
    page.locator.return_value = label
    crawler.log_login_failure_diagnostics(page)
    text = capsys.readouterr().err
    if mode == 'valid':
        assert '銀行提示 [REDACTED]' in text
        label.inner_text.assert_called_once_with(timeout=250)
        page.locator.assert_called_once_with('#ctl00_ctl00_ContentPlaceHolder1__errorLabel')
    else:
        assert '銀行提示' not in text
    assert 'PrivateUser' not in text and 'DO_NOT_PRINT' not in text


def test_hostile_objects_never_execute_or_escape(capsys):
    from tests.test_sinopac_native_login_diagnostics import Hostile
    class HostileString(str):
        def __eq__(self, other): raise AssertionError('no equality')
        def __str__(self): raise AssertionError('no stringify')
    class HostileKey:
        def __hash__(self): return hash('Message')
        def __eq__(self, other): raise AssertionError('no key equality')
    creds = SinopacCreds(national_id='B123456789', user_code='PrivateUser', password='PasswordValue')
    for value in (Hostile(), HostileString('PrivateUser')):
        assert _safe_login_message(value, creds) is None
        creds.password = value
        assert _safe_login_message('PrivateUser', creds) is None
    from backend.core.base import _safe_state_value
    assert _safe_state_value({HostileKey(): 'PRIVATE'}, 'Message') is None


def test_redaction_marker_cannot_echo_a_known_credential():
    creds = SinopacCreds(national_id='B123456789', user_code='REDACTED', password='PasswordValue')
    assert _safe_login_message('失敗 REDACTED', creds) is None


@pytest.mark.parametrize('text', [
    'Private&amp;User', 'Ｐｒｉｖａｔｅ＆Ｕｓｅｒ', 'Private%26User',
    'Private\u200b&User', 'Private&#38;User',
])
def test_known_credentials_encoded_variants(text):
    creds = SinopacCreds(national_id='B123456789', user_code='Private&User', password='PasswordValue')
    assert _safe_login_message('失敗 ' + text, creds) == '失敗 [REDACTED]'


@pytest.mark.parametrize('url', [LOGIN_RESPONSE_URL, LOGIN_RESPONSE_URL + '?unverified=1', LOGIN_RESPONSE_URL.replace('mma.', 'other.')])
def test_missing_cdp_never_falls_back(url):
    from unittest.mock import Mock
    request = Mock(url=url, method='POST', post_data='SECRET', headers={}, redirected_from=None)
    request.frame = SimpleNamespace(url=BASE, page=SimpleNamespace(main_frame=None))
    response = Mock(url=url, request=request, status=200, headers={'content-type': 'application/json'})
    collector = ResponseCollector('sinopac.com')
    collector._on_request(request)
    collector._on_response(response)
    response.body.assert_not_called()
    response.json.assert_not_called()
    assert len(collector.hits) == 1
    assert collector.hits[0].req_body is None and collector.hits[0].resp_json is None


@pytest.mark.parametrize('depth', [4, 8])
def test_nonconvergent_encoding_suppresses_entire_message(depth):
    from urllib.parse import quote
    value = 'Fake&Password'
    creds = SinopacCreds(national_id='B123456789', user_code='SyntheticUser', password=value)
    for _ in range(depth):
        value = quote(value, safe='')
    assert _safe_login_message('失敗 ' + value, creds) is None


def test_controls_cannot_hide_encoded_known_credentials():
    creds = SinopacCreds(national_id='B123456789', user_code='SyntheticUser', password='Fake&Password')
    assert _safe_login_message('失敗 Fake%\u200b26Password', creds) == '失敗 [REDACTED]'
