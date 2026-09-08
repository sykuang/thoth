"""Native login failures must survive the real safe sink without private text."""
import logging

import pytest

from backend.core import base
from backend.banks.sinopac import SinopacCrawler, SinopacLoginError
from tests.test_bank_login_lifecycle import _StagedCrawler, _run


@pytest.mark.parametrize('code,stage', [
    ('captcha_invalid', 'captcha_ocr'),
    ('login_failed', 'input_inventory'),
    ('login_failed', 'credential_submit'),
])
def test_native_code_and_stage_reach_warning_sink(monkeypatch, tmp_path, capsys, code, stage):
    monkeypatch.setattr(base, 'DATA_ROOT', tmp_path / 'initial')
    crawler = _StagedCrawler(name='sinopac')
    error = SinopacLoginError(code, 'SYNTHETIC_PRIVATE_TEXT')
    error.login_stage = stage  # RED must exercise the existing outer sink.
    def fail(_page):
        raise error
    monkeypatch.setattr(crawler, '_shared_login', fail)
    monkeypatch.setattr(logging.getLogger(), 'level', logging.WARNING)
    result, _ = _run(monkeypatch, tmp_path, crawler, None)
    assert f'internal_error_code={code}' in result['error']
    assert f'stage={stage}' in result['error']
    assert 'collect' not in crawler.events and crawler.submissions == 0
    text = capsys.readouterr().err
    assert result['error'] in text
    assert 'SYNTHETIC_PRIVATE_TEXT' not in text


@pytest.mark.parametrize('method,stage', [
    ('prepare_login_page', 'prepare_page'),
    ('submit_credentials_once', 'captcha_image_wait'),
])
def test_native_producer_retains_failure_stage(method, stage):
    class Page:
        def wait_for_timeout(self, *_a, **_kw):
            raise TimeoutError('SYNTHETIC_PRIVATE_TEXT')
        def wait_for_selector(self, *_a, **_kw):
            raise TimeoutError('SYNTHETIC_PRIVATE_TEXT')
    crawler = object.__new__(SinopacCrawler)
    crawler._login_diagnostic_floor = 123
    with pytest.raises(SinopacLoginError) as raised:
        getattr(crawler, method)(Page())
    assert raised.value.login_stage == stage
    assert crawler._login_diagnostic_floor is None


def test_sanitized_bank_message_reaches_only_worker_stderr(monkeypatch, tmp_path, capsys):
    from backend.core.creds import SinopacCreds
    from types import MethodType
    monkeypatch.setattr(base, 'DATA_ROOT', tmp_path / 'initial')
    crawler = _StagedCrawler(name='sinopac')
    crawler.creds = SinopacCreds(national_id='B123456789', user_code='PrivateUser', password='Private&Pass')
    crawler.log_login_failure_diagnostics = MethodType(SinopacCrawler.log_login_failure_diagnostics, crawler)
    def fail(page):
        crawler._login_diagnostic_floor = 0
        crawler.collector.hits.append(base.ApiHit(
            url='https://mma.sinopac.com/ws/member/login/ws_validatecaptcha.ashx',
            raw_url='https://mma.sinopac.com/ws/member/login/ws_validatecaptcha.ashx',
            method='POST', status=200, request_sequence=1, main_frame_request=True,
            request_frame_url='https://mma.sinopac.com/MemberPortal/Member/MMALogin.aspx',
            resp_json=[{'Header': 'FAIL', 'Message': '登入失敗 PrivateUser Private&amp;Pass B123456789 a@b.com 0912-345-678\nFORGED'}],
        ))
        raise SinopacLoginError('login_failed', 'PRIVATE_EXCEPTION')
    monkeypatch.setattr(crawler, '_shared_login', fail)
    result, _ = _run(monkeypatch, tmp_path, crawler, None)
    text = capsys.readouterr().err
    assert '登入失敗' in text and 'WARNING' in text
    assert '"source": "login_response.Message"' in text
    assert '"bank_error_code": null' in text and 'not_verified' in text
    assert '登入失敗' not in result['error']
    for secret in ('PrivateUser', 'Private', 'B123456789', 'a@b.com', '0912-345-678', 'PRIVATE_EXCEPTION', '\nFORGED'):
        assert secret not in text


@pytest.mark.parametrize('stage', [
    'input_inventory', 'input_geometry', 'input_order', 'input_enabled',
    'credential_fill', 'input_length', 'captcha_ocr', 'captcha_fill',
    'login_button', 'credential_submit', 'post_submit_check',
])
def test_every_submit_stage_producer(stage):
    from tests.test_sinopac_login_checkpoints import _submit_fixture
    crawler, page, fields, image, button, inputs, buttons = _submit_fixture()
    fail = RuntimeError('PRIVATE_NATIVE_CAUSE')
    if stage == 'input_inventory': inputs.count.return_value = 0
    elif stage == 'input_geometry': fields[1].bounding_box.return_value = None
    elif stage == 'input_order': fields[2].bounding_box.return_value = fields[1].bounding_box.return_value
    elif stage == 'input_enabled': fields[1].is_enabled.return_value = False
    elif stage == 'credential_fill': fields[0].click.side_effect = fail
    elif stage == 'input_length': fields[0].input_value.return_value = ''
    elif stage == 'captcha_ocr': crawler._ocr_captcha.return_value = None
    elif stage == 'captcha_fill': fields[3].click.side_effect = fail
    elif stage == 'login_button': buttons.count.return_value = 0
    elif stage == 'credential_submit': button.click.side_effect = fail
    elif stage == 'post_submit_check': crawler._logged_in.side_effect = fail
    with pytest.raises(SinopacLoginError) as raised:
        crawler.submit_credentials_once(page)
    assert raised.value.login_stage == stage
    assert 'PRIVATE' not in str(raised.value)
    assert button.click.call_count == (1 if stage in {'credential_submit', 'post_submit_check'} else 0)


@pytest.mark.parametrize('missing', [True, False])
def test_refresh_stage_both_native_raise_sites(missing):
    from tests.test_sinopac_login_checkpoints import _submit_fixture
    crawler, page, fields, image, button, inputs, buttons = _submit_fixture()
    if missing: page.locator('#imgCode').count.return_value = 0
    else: image.click.side_effect = RuntimeError('PRIVATE_NATIVE_CAUSE')
    with pytest.raises(SinopacLoginError) as raised:
        crawler.prepare_captcha_resubmit(page)
    assert raised.value.login_stage == 'captcha_refresh'
    button.click.assert_not_called()


class Hostile:
    def __str__(self):
        raise AssertionError('str must not run')
    def __eq__(self, other):
        raise AssertionError('equality must not run')
    def __bool__(self):
        raise AssertionError('truthiness must not run')


@pytest.mark.parametrize('value', [Hostile(), None, 'PRIVATE', ['captcha_invalid']])
def test_native_sink_rejects_unowned_diagnostic_fields(value):
    text = base._safe_native_login_diagnostics('sinopac', {'code': value, 'login_stage': value})
    assert text == 'internal_error_code=unknown, stage=unknown'
    assert base._safe_native_login_diagnostics('ctbc', {'code': 'captcha_invalid', 'login_stage': 'captcha_ocr'}) == ''
