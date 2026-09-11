"""Offline injected failures through real group-C adapter methods."""
import importlib
from contextlib import suppress
from unittest.mock import Mock

import pytest

BANKS = ('ubot', 'esun', 'taishin', 'fubon')


def fixture(bank):
    values = importlib.import_module(f'tests.test_{bank}_login_checkpoints')._submit_fixture()
    crawler, page = values[:2]
    if bank == 'ubot':
        fields, button = list(page.test_fields.values()), values[3]
    elif bank == 'fubon':
        fields, button = [values[3][1], values[3][2], values[3][0], values[5]], values[6]
    else:
        fields, button = list(values[3].values()), values[5]
    return crawler, page, fields, button


CASES = [(bank, stage) for bank in BANKS for stage in (
    'login_field', 'login_ocr', 'login_button', 'login_submit', 'login_postconfirm'
) if not (bank == 'esun' and stage == 'login_ocr')]


@pytest.mark.parametrize('bank,stage', CASES)
def test_injected_submit_boundary(bank, stage):
    crawler, page, fields, button = fixture(bank)
    crawler._diagnostic_stage = 'cleanup'  # stale run/re-submit evidence
    failure = RuntimeError('synthetic')
    if stage == 'login_field':
        fields[0].click.side_effect = failure
    elif stage == 'login_ocr':
        getattr(crawler, '_ocr_with_regen' if bank == 'ubot' else '_ocr_captcha').side_effect = failure
    elif stage == 'login_button':
        button.is_enabled.side_effect = failure
    elif stage == 'login_submit':
        button.click.side_effect = failure
    else:
        crawler._logged_in.side_effect = failure
    with suppress(RuntimeError):
        crawler.submit_credentials_once(page)
    assert crawler._diagnostic_stage == stage
    assert button.click.call_count == int(stage in ('login_submit', 'login_postconfirm'))
    assert fields[0].click.call_count == (1 if stage == 'login_field' else 2)
    page.evaluate.assert_not_called()
    page.goto.assert_not_called()
    if stage == 'login_postconfirm':
        expected_waits = {'ubot': [150, 150, 200, 200, 6000, 1000],
                          'esun': [200, 200, 300, 10000, 1000],
                          'taishin': [200, 200, 200, 300, 10000, 1000],
                          'fubon': [3000, 1000]}
        assert [c.args[0] for c in page.wait_for_timeout.call_args_list] == expected_waits[bank]


@pytest.mark.parametrize('bank', BANKS)
def test_success_then_second_submit_resets_field_stage(bank):
    crawler, page, fields, button = fixture(bank)
    crawler.submit_credentials_once(page)
    assert crawler._diagnostic_stage == 'login_postconfirm'
    fields[0].click.side_effect = RuntimeError('synthetic')
    with pytest.raises(RuntimeError):
        crawler.submit_credentials_once(page)
    assert crawler._diagnostic_stage == 'login_field'
    assert button.click.call_count == 1


@pytest.mark.parametrize('bank', ('esun', 'taishin', 'fubon'))
def test_frame_lookup_boundary(bank):
    crawler, page, fields, button = fixture(bank)
    crawler._find_login_frame = Mock(side_effect=RuntimeError('synthetic'))
    with pytest.raises(RuntimeError):
        crawler.submit_credentials_once(page)
    assert crawler._diagnostic_stage == 'login_prepare'
    fields[0].click.assert_not_called()
    button.click.assert_not_called()


@pytest.mark.parametrize('stage', ('browser_launch', 'browser_navigation'))
def test_fubon_custom_browser_boundary(monkeypatch, tmp_path, stage):
    from backend.banks import fubon
    crawler, page, _, _ = fixture('fubon')
    crawler.session_dir = tmp_path
    engine = Mock()
    engine.context.new_page.return_value = page
    from unittest.mock import MagicMock
    session = MagicMock()
    session.__enter__.return_value = engine
    factory = Mock(return_value=session)
    monkeypatch.setattr(fubon, 'StealthySession', factory)
    if stage == 'browser_launch':
        factory.side_effect = RuntimeError('synthetic')
    else:
        page.goto.side_effect = RuntimeError('synthetic')
    action = Mock()
    with pytest.raises(RuntimeError):
        crawler._execute_browser_flow('https://example.invalid/', headless=True,
                                      page_action=action, fetch_kwargs={})
    assert crawler._diagnostic_stage == stage
    assert factory.call_count == 1
    assert page.goto.call_count == int(stage == 'browser_navigation')
    action.assert_not_called()


@pytest.mark.parametrize('bank,stage', [('ubot', 'collect_navigation'),
                                     ('esun', 'collect_accounts'),
                                     ('taishin', 'collect_cards'),
                                     ('fubon', 'collect_transactions')])
def test_collection_entry_failure(bank, stage):
    crawler, page, _, _ = fixture(bank)
    failure = RuntimeError('synthetic')
    if bank == 'ubot':
        crawler._goto = Mock(side_effect=failure)
    elif bank == 'fubon':
        crawler._collect_attested_twd_history = Mock(side_effect=failure)
    else:
        page.wait_for_timeout.side_effect = failure
    with pytest.raises(RuntimeError):
        crawler.collect(page, Mock())
    assert crawler._diagnostic_stage == stage
    page.evaluate.assert_not_called()
