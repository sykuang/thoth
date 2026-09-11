"""Offline field failures through the real lifecycle and runner sinks."""
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from patchright.sync_api import TimeoutError as PatchrightTimeoutError

from backend.core import base, persist, store
from backend.core.login_checkpoints import CheckpointKind, CheckpointOutcome
from backend.server import card_events, rules_repo, sync_runner as runner
from tests.test_scsb_login_checkpoints import _submit_fixture

FIELDS = ('national_id', 'user_code', 'password', 'captcha')
OPERATIONS = ('count', 'visible', 'enabled', 'click', 'triple_click', 'clear', 'type', 'readback', 'length')
CASES = [('national_id', 'wait')] + [(field, op) for field in FIELDS for op in OPERATIONS]
SECRETS = ('A123456789', 'USER-PRIVATE', 'PASSWORD-PRIVATE', '12345', 'PRIVATE-DOM-ERROR')


def setup_runner(monkeypatch):
    crawler, page, fields, button, _ = _submit_fixture()
    crawler.transaction_cursors = {}
    crawler._enforce_session_freshness = Mock()
    crawler._build_fetch_kwargs = Mock(return_value={})
    crawler._execute_browser_flow = lambda *a, page_action, **kw: page_action(page)
    crawler.collect = Mock(side_effect=AssertionError('must not collect'))
    # Keep BankCrawler.run, _shared_login, reducer and runner dispatch real.
    monkeypatch.setattr(base, 'evaluate_login_checkpoint', lambda *a, **kw: CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS))
    monkeypatch.setattr(runner, '_load_crawler', lambda bank: (SimpleNamespace(BASE='offline'), lambda: crawler))
    monkeypatch.setattr(rules_repo, 'list_rules', lambda **kw: [])
    fake_store = Mock()
    fake_store.latest_twd_transaction_dates.return_value = {}
    fake_store.latest_card_transaction_dates.return_value = {}
    monkeypatch.setattr(store, 'BankStore', Mock(return_value=fake_store))
    monkeypatch.setattr(card_events, 'snapshot_cards', lambda **kw: [])
    monkeypatch.setattr(runner, 'get_job', lambda _: dict(user_id=1, bank='scsb', history_mode='full'))
    monkeypatch.setattr(runner.sync_jobs_repo, 'claim_queued', lambda _: True)
    failed = Mock()
    monkeypatch.setattr(runner.sync_jobs_repo, 'mark_failed', failed)
    monkeypatch.setattr(runner, '_send_sync_notification', Mock())
    persisted = Mock(side_effect=AssertionError('must not persist'))
    monkeypatch.setattr(persist, 'persist_collected', persisted)
    return crawler, page, fields, button, failed, persisted, fake_store


@pytest.mark.parametrize('field,operation', CASES)
@pytest.mark.parametrize('cleanup_fails', [False, True])
def test_field_operation_reaches_runner_and_stderr(monkeypatch, capsys, field, operation, cleanup_fails):
    crawler, page, fields, button, failed, persisted, fake_store = setup_runner(monkeypatch)
    index = FIELDS.index(field)
    group, control = list(fields.values())[index]
    error = PatchrightTimeoutError('PRIVATE-DOM-ERROR ' + ' '.join(SECRETS))
    if operation == 'wait':
        page.wait_for_selector.side_effect = error
    elif operation == 'count':
        group.count.side_effect = error
    elif operation in ('visible', 'enabled'):
        getattr(control, 'is_' + operation).side_effect = error
    elif operation in ('click', 'triple_click'):
        control.click.side_effect = error if operation == 'click' else [None, error]
    elif operation in ('clear', 'type'):
        getattr(page.keyboard, 'press' if operation == 'clear' else 'type').side_effect = [None] * index + [error]
    elif operation == 'readback':
        control.input_value.side_effect = error
    else:
        control.input_value.return_value = ''
    if cleanup_fails:
        monkeypatch.setattr(base.ResponseCollector, 'detach', Mock(side_effect=ValueError('PRIVATE-CLEANUP')))
        fake_store.close.side_effect = ValueError('PRIVATE-CLEANUP')
    assert runner._exec_sync(1)
    failed.assert_called_once()
    text = failed.call_args.args[1]
    stderr = capsys.readouterr().err
    stage = f'login_field_{field}_{operation}'
    for output in (text, stderr):
        assert f';stage={stage};code={stage}_failed' in output
        assert all(secret not in output for secret in (*SECRETS, 'PRIVATE-CLEANUP'))
    if operation != 'length':
        assert 'underlying_exception_type=PatchrightTimeoutError' in text
    assert 'exception_type=RuntimeError' in text
    button.click.assert_not_called()
    crawler.collect.assert_not_called()
    persisted.assert_not_called()
    # A second run must not inherit any field or cleanup diagnostic.
    crawler._enforce_session_freshness.side_effect = RuntimeError('PRIVATE-DOM-ERROR')
    failed.reset_mock()
    assert runner._exec_sync(2)
    assert ';stage=session;code=session_failed' in failed.call_args.args[1]
    persisted.assert_not_called()


def test_success_global_browser_action_order():
    crawler, page, fields, button, _ = _submit_fixture()
    trace = Mock()
    trace.attach_mock(page.wait_for_selector, 'wait')
    trace.attach_mock(page.wait_for_timeout, 'pause')
    trace.attach_mock(page.locator, 'locator')
    trace.attach_mock(page.keyboard.press, 'clear')
    trace.attach_mock(page.keyboard.type, 'type')
    trace.attach_mock(crawler._ocr_captcha, 'ocr')
    trace.attach_mock(button.click, 'submit')
    for index, (group, field) in enumerate(fields.values()):
        trace.attach_mock(group.count, f'count{index}')
        trace.attach_mock(group.nth, f'nth{index}')
        for op in ('is_visible', 'is_enabled', 'click', 'input_value'):
            trace.attach_mock(getattr(field, op), f'{op}{index}')
    crawler.submit_credentials_once(page)
    expected = [call.wait('#userId', state='visible', timeout=30000), call.wait('.ved_img', state='visible', timeout=15000)]
    for index, selector in enumerate(fields):
        expected += [call.locator(selector), getattr(call, f'count{index}')(), getattr(call, f'nth{index}')(0), getattr(call, f'is_visible{index}')(), getattr(call, f'is_enabled{index}')()]
    for index, value in enumerate(SECRETS[:4]):
        if index == 3:
            expected.append(call.ocr(page, max_attempts=5))
        expected += [getattr(call, f'click{index}')(), getattr(call, f'click{index}')(click_count=3), call.clear('Backspace'), call.type(value, delay=80), getattr(call, f'input_value{index}')()]
    expected += [call.locator("button, input[type='submit'], input[type='button']"), call.submit(timeout=8000), call.pause(12000)]
    assert trace.mock_calls == expected
    assert crawler._diagnostic_stage == 'login_postconfirm'


@pytest.mark.parametrize('role', FIELDS)
@pytest.mark.parametrize('operation', ('count', 'visible', 'enabled'))
def test_inventory_guard_short_circuits_without_submission(role, operation):
    from backend.banks.scsb import ScsbLoginError
    crawler, page, fields, button, _ = _submit_fixture()
    group, control = list(fields.values())[FIELDS.index(role)]
    if operation == 'count':
        group.count.return_value = 2
    else:
        getattr(control, 'is_' + operation).return_value = False
    with pytest.raises(ScsbLoginError, match='登入欄位無法安全確認；未送出登入'):
        crawler.submit_credentials_once(page)
    assert crawler._diagnostic_stage == f'login_field_{role}_{operation}'
    if operation == 'count':
        control.is_visible.assert_not_called()
    if operation in ('count', 'visible'):
        control.is_enabled.assert_not_called()
    page.keyboard.type.assert_not_called()
    button.click.assert_not_called()


@pytest.mark.parametrize('operation,stage', [('image_wait', 'login_ocr'), ('ocr', 'login_ocr'), ('button', 'login_button'), ('submit', 'login_submit'), ('post', 'login_postconfirm')])
def test_later_failures_reset_field_stage(operation, stage):
    from backend.banks.scsb import ScsbLoginError
    crawler, page, _, button, submits = _submit_fixture()
    error = PatchrightTimeoutError('PRIVATE-DOM-ERROR')
    if operation == 'image_wait':
        page.wait_for_selector.side_effect = [None, error]
    elif operation == 'ocr':
        crawler._ocr_captcha.side_effect = error
    elif operation == 'button':
        submits.count.side_effect = error
    elif operation == 'submit':
        button.click.side_effect = error
    else:
        page.wait_for_timeout.side_effect = error
    with pytest.raises(ScsbLoginError):
        crawler.submit_credentials_once(page)
    assert crawler._diagnostic_stage == stage
    assert button.click.call_count == (1 if operation in ('submit', 'post') else 0)


def test_field_stage_allowlist_rejects_untrusted_roles_and_operations():
    from backend.core.error_diagnostics import make_diagnostics
    for stage in ('login_field_PASSWORD-PRIVATE_click', 'login_field_password_PRIVATE-DOM-ERROR', 'login_field_unknown_click'):
        assert make_diagnostics(stage) == {'stage': 'unknown', 'code': 'crawler_failed'}
