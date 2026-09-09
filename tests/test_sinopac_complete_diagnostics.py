"""Synthetic failures through real producers and the worker stderr/result sink."""
import json
from types import MethodType

import pytest

from backend.banks.sinopac import SinopacCrawler, SinopacLoginError
from backend.core import base
from tests.test_bank_login_lifecycle import _StagedCrawler, _run
from tests.test_sinopac_login_checkpoints import _submit_fixture


def diagnostic(capsys):
    text = capsys.readouterr().err
    assert 'PRIVATE_CAUSE' not in text
    return json.loads(next(line.split('WARNING] ', 1)[1] for line in text.splitlines() if 'bank_login_diagnostic' in line))


@pytest.mark.parametrize('driver', ['patchright', 'playwright'])
@pytest.mark.parametrize('error_name', ['TimeoutError', 'TargetClosedError', 'Error'])
@pytest.mark.parametrize('operation', ['post_submit_wait', 'logged_in', 'response_visible'])
def test_real_browser_exceptions_reach_worker(monkeypatch, tmp_path, capsys, driver, error_name, operation):
    from importlib import import_module
    error = getattr(import_module(driver + '._impl._errors'), error_name)('PRIVATE_CAUSE')
    expected = driver.capitalize() + error_name
    producer, page, *_ = _submit_fixture()
    if operation == 'post_submit_wait':
        page.wait_for_timeout.side_effect = error
    elif operation == 'logged_in':
        producer._logged_in.side_effect = error
    else:
        producer._logged_in.return_value = False
        producer._response_visible = lambda p: (_ for _ in ()).throw(error)
    monkeypatch.setattr(base, 'DATA_ROOT', tmp_path / 'initial')
    worker = _StagedCrawler(name='sinopac')
    def fail(_page):
        try:
            producer.submit_credentials_once(page)
        finally:
            worker._sinopac_diagnostics = getattr(producer, '_sinopac_diagnostics', {})
    worker._shared_login = fail
    worker.log_login_failure_diagnostics = MethodType(SinopacCrawler.log_login_failure_diagnostics, worker)
    result, _ = _run(monkeypatch, tmp_path, worker, None)
    report = diagnostic(capsys)
    assert report['underlying_exception_type'] == expected
    assert report['failures']['last']['exception_type'] == expected
    assert report['failures']['last']['operation'] == operation
    assert 'stage=post_submit_check' in result['error']
    assert 'collect' not in worker.events


@pytest.mark.parametrize('operation', ['post_submit_wait', 'logged_in', 'response_visible'])
def test_native_cause_and_operation_reach_real_worker(monkeypatch, tmp_path, capsys, operation):
    producer, page, *_ = _submit_fixture()
    if operation == 'post_submit_wait':
        page.wait_for_timeout.side_effect = TimeoutError('PRIVATE_CAUSE')
    elif operation == 'logged_in':
        producer._logged_in.side_effect = TimeoutError('PRIVATE_CAUSE')
    else:
        producer._logged_in.return_value = False
        producer._response_visible = lambda p: (_ for _ in ()).throw(TimeoutError('PRIVATE_CAUSE'))
    monkeypatch.setattr(base, 'DATA_ROOT', tmp_path / 'initial')
    worker = _StagedCrawler(name='sinopac')
    def fail(_page):
        try:
            producer.submit_credentials_once(page)
        finally:
            worker._sinopac_diagnostics = getattr(producer, '_sinopac_diagnostics', {})
    worker._shared_login = fail
    worker.log_login_failure_diagnostics = MethodType(SinopacCrawler.log_login_failure_diagnostics, worker)
    result, _ = _run(monkeypatch, tmp_path, worker, None)
    report = diagnostic(capsys)
    assert 'stage=post_submit_check' in result['error']
    assert report['failures']['last'] == {'operation': operation, 'exception_type': 'TimeoutError', 'submission_attempt': 1, 'ocr_attempt': None}
    assert report['terminal_exception_type'] == 'RuntimeError'
    assert report['underlying_exception_type'] == 'TimeoutError'
    assert report['bank_error_code'] is None
    assert report['message_unavailable_reason'] != 'unknown'
    assert 'collect' not in worker.events


@pytest.mark.parametrize('failure', ['screenshot', 'inference', 'stability'])
def test_original_ocr_failures_and_freshness(monkeypatch, tmp_path, capsys, failure):
    from unittest.mock import Mock
    from backend.core import captcha
    crawler, page, *_ = _submit_fixture()
    del crawler._ocr_captcha
    crawler.session_dir = tmp_path
    element = Mock()
    element.screenshot.return_value = b'synthetic-image'
    page.query_selector.return_value = element
    page.wait_for_timeout.return_value = None
    if failure == 'screenshot':
        element.screenshot.side_effect = TimeoutError('PRIVATE_CAUSE')
    elif failure == 'stability':
        element.screenshot.side_effect = [TimeoutError('PRIVATE_CAUSE'), b'one', b'one', b'ocr']
    monkeypatch.setattr(captcha, '_ocr_classification', Mock(side_effect=ValueError('PRIVATE_CAUSE')) if failure == 'inference' else Mock(return_value={'text':'123456','confidence':0.99}))
    result = crawler._ocr_captcha(page, max_attempts=1)
    crawler.log_login_failure_diagnostics(page)
    report = diagnostic(capsys)
    first = report['failures']['first']
    assert first['exception_type'] == ('ValueError' if failure == 'inference' else 'TimeoutError')
    assert first['operation'] == ('ocr_inference' if failure == 'inference' else 'captcha_stability_screenshot')
    assert first['ocr_attempt'] == 1
    assert report['captcha']['ocr_attempt'] == 1
    assert report['captcha']['ocr_duration_ms'] >= 0
    assert report['captcha']['accuracy_status'] == 'not_verified'
    assert report['captcha']['bank_expiry_status'] == 'not_verified'
    if failure == 'stability':
        assert result == '123456'
        assert report['captcha']['confidence'] == 0.99
        assert report['captcha']['stability_samples'] == 3


@pytest.mark.parametrize('function', ['solve_captcha', 'wait_captcha_stable'])
def test_diagnostic_callback_failure_does_not_change_ocr_behavior(monkeypatch, tmp_path, function):
    from unittest.mock import Mock
    from backend.core import captcha
    page = Mock()
    page.query_selector.return_value.screenshot.side_effect = ValueError('PRIVATE_CAUSE')
    def broken_callback(*args):
        raise RuntimeError('PRIVATE_CAUSE')
    kwargs = {'tmp_path': tmp_path / 'unused', 'diagnostics': {}, 'on_failure': broken_callback}
    if function == 'wait_captcha_stable':
        kwargs['tries'] = 1
    result = getattr(captcha, function)(page, '#imgCode', **kwargs)
    assert result is (False if function == 'wait_captcha_stable' else None)


def test_ocr_image_age_includes_inference(monkeypatch, tmp_path, capsys):
    from unittest.mock import Mock
    from backend.core import captcha
    from backend.banks import sinopac
    clock = {'now': 1.0}
    monkeypatch.setattr(sinopac.time, 'monotonic', lambda: clock['now'])
    crawler, page, *_ = _submit_fixture()
    del crawler._ocr_captcha
    page.query_selector.return_value.screenshot.return_value = b'synthetic-image'
    def inference(*args, **kwargs):
        clock['now'] = 6.0
        return {'text': '123456', 'confidence': 0.99}
    monkeypatch.setattr(captcha, '_ocr_classification', inference)
    assert crawler._ocr_captcha(page, max_attempts=1) == '123456'
    assert crawler._sinopac_diagnostics['ocr_image_captured_at'] == 1.0
    assert crawler._sinopac_diagnostics['ocr_inference_ms'] == 5000
    clock['now'] = 10.0
    crawler._ocr_captcha = Mock(side_effect=lambda *a, **k: crawler._sinopac_diagnostics.update(ocr_image_captured_at=1.0, ocr_completed_at=6.0) or '123456')
    crawler.submit_credentials_once(page)
    crawler.log_login_failure_diagnostics(page)
    report = diagnostic(capsys)
    assert report['captcha']['image_to_submit_ms'] == 9000
    assert report['captcha']['ocr_to_submit_ms'] == 4000


def test_swallowed_inspection_failure_and_reset(capsys):
    from unittest.mock import Mock
    from backend.banks.sinopac import BASE
    crawler = object.__new__(SinopacCrawler)
    page = Mock(url='https://mma.sinopac.com/MyMMA/home')
    page.frames = []
    page.locator.side_effect = TimeoutError('PRIVATE_CAUSE')
    assert crawler._logged_in(page) is False
    crawler.log_login_failure_diagnostics(page)
    report = diagnostic(capsys)
    assert report['failures']['first']['operation'] == 'logged_in_captcha_inspection'
    assert report['failures']['first']['exception_type'] == 'TimeoutError'
    assert report['failures']['first']['submission_attempt'] is None
    page.url = BASE
    crawler.prepare_login_page(page)
    crawler.log_login_failure_diagnostics(page)
    assert diagnostic(capsys)['failures']['first'] is None


@pytest.mark.parametrize('mode,reason', [('foreign','not_login_document'), ('hidden','label_not_visible'), ('duplicate','label_not_unique'), ('read-failed','label_inspection_exception')])
def test_message_absence_has_source_specific_reason(capsys, mode, reason):
    from unittest.mock import Mock
    from backend.banks.sinopac import BASE
    crawler = object.__new__(SinopacCrawler)
    crawler.collector = base.ResponseCollector('sinopac.com')
    label = Mock()
    label.count.return_value = 2 if mode == 'duplicate' else 1
    label.is_visible.return_value = mode != 'hidden'
    label.inner_text.side_effect = TimeoutError('PRIVATE_CAUSE')
    page = Mock(url='https://example.invalid' if mode == 'foreign' else BASE)
    page.locator.return_value = label
    crawler.log_login_failure_diagnostics(page)
    report = diagnostic(capsys)
    assert report['label_status'] == reason
    assert report['response_status'] == 'submission_boundary_unavailable'


@pytest.mark.parametrize('bad', [None, True, -1, 1.5, 'bad', [], {}, float('inf'), 10**100])
def test_corrupt_diagnostics_do_not_change_browser_operations(monkeypatch, tmp_path, bad):
    from unittest.mock import Mock
    from backend.core import captcha
    crawler = object.__new__(SinopacCrawler)
    crawler._sinopac_diagnostics = {'failure_count': bad}
    page = Mock(url='https://mma.sinopac.com/MyMMA/home')
    page.frames = []
    page.locator.side_effect = TimeoutError('PRIVATE_CAUSE')
    assert crawler._logged_in(page) is False
    assert crawler._sinopac_diagnostics['failure_count'] is None
    producer, page, _, _, button, *_ = _submit_fixture()
    producer._sinopac_diagnostics = {'submission_attempt': bad, 'last': []}
    producer.submit_credentials_once(page)
    assert button.click.call_count == 1
    assert producer._sinopac_diagnostics['submission_attempt'] is None
    page = Mock()
    page.query_selector.return_value.screenshot.return_value = b'same-image'
    stats = {'stability_samples': bad}
    assert captcha.wait_captcha_stable(page, '#imgCode', tries=2, tmp_path=tmp_path/'unused', diagnostics=stats) is True
    assert page.query_selector.return_value.screenshot.call_count == 2
    assert stats['stability_samples'] is None


@pytest.mark.parametrize('bad', [float('inf'), float('nan'), 'bad', [], True, 10**100])
def test_corrupt_timestamps_do_not_prevent_submission(bad):
    crawler, page, _, _, button, *_ = _submit_fixture()
    crawler._ocr_captcha.side_effect = lambda *a, **k: crawler._sinopac_diagnostics.update(ocr_completed_at=bad, ocr_image_captured_at=bad) or '123456'
    crawler.submit_credentials_once(page)
    assert button.click.call_count == 1
    assert crawler._sinopac_diagnostics['ocr_to_submit_ms'] is None
    assert crawler._sinopac_diagnostics['image_to_submit_ms'] is None


def test_invalid_prior_failure_does_not_mask_current_ocr_failure():
    crawler, page, *_ = _submit_fixture()
    crawler._sinopac_diagnostics = {'last': []}
    crawler._ocr_captcha.return_value = None
    with pytest.raises(SinopacLoginError) as raised:
        crawler.submit_credentials_once(page)
    assert raised.value.code == 'captcha_invalid'


def test_success_header_is_not_a_projection_failure(capsys):
    from types import SimpleNamespace
    from backend.banks.sinopac import BASE, LOGIN_RESPONSE_URL
    crawler = object.__new__(SinopacCrawler)
    crawler._login_diagnostic_floor = 0
    crawler.collector = base.ResponseCollector('sinopac.com')
    crawler.collector.hits.append(base.ApiHit(url=LOGIN_RESPONSE_URL, raw_url=LOGIN_RESPONSE_URL,
        method='POST', status=200, main_frame_request=True, request_sequence=1,
        request_frame_url=BASE, resp_json=[{'Header':'SUCCESS','Message':''}]))
    crawler.log_login_failure_diagnostics(SimpleNamespace(url=''))
    report = diagnostic(capsys)
    assert report['response_status'] == 'bank_success_header'
    assert report['message'] is None


def test_hostile_diagnostic_state_is_never_serialized(capsys):
    from tests.test_sinopac_native_login_diagnostics import Hostile
    from types import SimpleNamespace
    crawler = object.__new__(SinopacCrawler)
    crawler._sinopac_diagnostics = {'first': Hostile(), 'last': {'operation':'PRIVATE_CAUSE','exception_type':Hostile()}, 'failure_count': True, 'ocr_duration_ms':float('inf')}
    crawler._login_terminal_exception_type = Hostile()
    crawler.log_login_failure_diagnostics(SimpleNamespace(url=''))
    report = diagnostic(capsys)
    assert report['failures']['first'] is None
    assert report['failures']['failure_count'] is None
    assert report['captcha']['ocr_duration_ms'] is None
    assert report['terminal_exception_type'] is None


def test_submission_resets_measurements_but_preserves_indexed_failure(capsys):
    crawler, page, fields, image, button, *_ = _submit_fixture()
    button.click.side_effect = TimeoutError('PRIVATE_CAUSE')
    with pytest.raises(SinopacLoginError):
        crawler.submit_credentials_once(page)
    first = crawler._sinopac_diagnostics['first']
    crawler._sinopac_diagnostics.update(confidence=0.99, ocr_duration_ms=50, ocr_completed_at=1)
    with pytest.raises(SinopacLoginError):
        crawler.submit_credentials_once(page)
    crawler.log_login_failure_diagnostics(page)
    report = diagnostic(capsys)
    assert report['failures']['first'] == first
    assert first['submission_attempt'] == 1
    assert report['failures']['last']['submission_attempt'] == 2
    assert report['captcha']['confidence'] is None
    assert report['captcha']['ocr_to_submit_ms'] is None


@pytest.mark.parametrize('terminal', ['generic', 'checkpoint'])
def test_generic_and_checkpoint_worker_keep_prior_inspection_evidence(monkeypatch, tmp_path, capsys, terminal):
    from backend.core.login_checkpoints import LoginCheckpointBlocked, LoginBudget, CheckpointOutcome, CheckpointKind, CheckpointPhase
    monkeypatch.setattr(base, 'DATA_ROOT', tmp_path / 'initial')
    worker = _StagedCrawler(name='sinopac')
    worker.log_login_failure_diagnostics = MethodType(SinopacCrawler.log_login_failure_diagnostics, worker)
    producer = object.__new__(SinopacCrawler)
    producer._record_login_failure('logged_in_body', TimeoutError('PRIVATE_CAUSE'))
    worker._sinopac_diagnostics = producer._sinopac_diagnostics
    def fail(page):
        if terminal == 'generic':
            raise RuntimeError('PRIVATE_CAUSE') from TimeoutError('PRIVATE_CAUSE')
        raise LoginCheckpointBlocked(LoginBudget(credential_submissions=1), CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER), phase=CheckpointPhase.POST_SUBMIT)
    worker._shared_login = fail
    result, _ = _run(monkeypatch, tmp_path, worker, None)
    report = diagnostic(capsys)
    assert report['failures']['first']['operation'] == 'logged_in_body'
    assert report['failures']['last']['exception_type'] == 'TimeoutError'
    if terminal == 'generic':
        assert report['underlying_exception_type'] == 'TimeoutError'
    else:
        assert 'phase=post_submit' in result['error']


@pytest.mark.parametrize('payload,status', [
    ({'text':'12345', 'confidence':0.99}, 'length_rejected'),
    ({'text':'123456'}, 'confidence_unavailable'),
    ({'text':'123456','confidence':0.5}, 'confidence_rejected'),
])
def test_original_ocr_gate_metadata(monkeypatch, payload, status, capsys):
    from unittest.mock import Mock
    from backend.core import captcha
    crawler, page, *_ = _submit_fixture()
    del crawler._ocr_captcha
    page.query_selector.return_value.screenshot.return_value = b'synthetic-image'
    monkeypatch.setattr(captcha, '_ocr_classification', Mock(return_value=payload))
    assert crawler._ocr_captcha(page, max_attempts=1) is None
    crawler.log_login_failure_diagnostics(page)
    report = diagnostic(capsys)
    assert report['captcha']['ocr_status'] == status
    assert report['captcha']['stability_matched'] is True
    assert report['captcha']['ocr_result_type'] == 'str'
