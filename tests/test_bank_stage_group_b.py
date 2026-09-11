"""Offline adapter-stage probes; reuse synthetic fixtures, never constructors."""
import importlib
from unittest.mock import Mock

import pytest

BANKS = ('cathay', 'ctbc', 'hsbc', 'sinopac', 'rakuten')


def fixture(bank, monkeypatch):
    suffix = 'crawler' if bank == 'rakuten' else 'login_checkpoints'
    module = importlib.import_module(f'tests.test_{bank}_{suffix}')
    parts = module._submit_fixture(monkeypatch) if bank == 'rakuten' else module._submit_fixture()
    button = parts[{'cathay': 2, 'ctbc': 3, 'hsbc': 4, 'sinopac': 4, 'rakuten': 3}[bank]]
    return parts[0], parts[1], button, parts


@pytest.mark.parametrize('bank', BANKS)
@pytest.mark.parametrize('failure,stage', [('field', 'login_field'), ('button', 'login_button'), ('click', 'login_submit'), ('post', 'login_postconfirm')])
def test_submit_failure_stage(bank, failure, stage, monkeypatch):
    crawler, page, button, _ = fixture(bank, monkeypatch)
    crawler._diagnostic_stage = 'stale'
    error = RuntimeError('synthetic')
    if failure == 'field':
        if bank == 'hsbc':
            page.locator.side_effect = error
        else:
            page.wait_for_selector.side_effect = error
    elif failure == 'button':
        button.get_attribute.side_effect = error
        if bank in ('hsbc', 'sinopac'):
            button.is_visible.side_effect = error
    elif failure == 'click':
        button.click.side_effect = error
    else:
        old = page.wait_for_timeout.side_effect
        def wait(ms):
            if button.click.called:
                raise error
            if old:
                return old(ms)
        page.wait_for_timeout.side_effect = wait
    try:
        crawler.submit_credentials_once(page)
    except RuntimeError:
        pass
    assert crawler._diagnostic_stage == stage
    assert button.click.call_count == (1 if failure in ('click', 'post') else 0)


def test_hsbc_username_continue_is_not_submit(monkeypatch):
    crawler, page, final, parts = fixture('hsbc', monkeypatch)
    parts[3].click.side_effect = RuntimeError('synthetic')
    with pytest.raises(RuntimeError):
        crawler.submit_credentials_once(page)
    assert crawler._diagnostic_stage == 'login_username_continue'
    final.click.assert_not_called()


@pytest.mark.parametrize('bank', ('hsbc', 'sinopac', 'rakuten'))
def test_ocr_stage(bank, monkeypatch):
    crawler, page, button, _ = fixture(bank, monkeypatch)
    if bank == 'rakuten':
        module = importlib.import_module('backend.banks.rakuten')
        page.locator(module.CAPTCHA_IMG).is_visible.return_value = True
        monkeypatch.setattr(module, 'wait_captcha_stable', Mock(side_effect=RuntimeError('synthetic')))
    else:
        getattr(crawler, '_solve_captcha' if bank == 'hsbc' else '_ocr_captcha').side_effect = RuntimeError('synthetic')
    with pytest.raises(RuntimeError):
        crawler.submit_credentials_once(page)
    assert crawler._diagnostic_stage == 'login_ocr'
    button.click.assert_not_called()


@pytest.mark.parametrize('bank', BANKS)
def test_prepare_failure_stage(bank, monkeypatch):
    crawler, page, _, _ = fixture(bank, monkeypatch)
    page.wait_for_timeout.side_effect = RuntimeError('synthetic')
    with pytest.raises(RuntimeError):
        crawler.prepare_login_page(page)
    assert crawler._diagnostic_stage == 'login_prepare'


@pytest.mark.parametrize('bank', BANKS)
def test_collect_initial_action(bank, monkeypatch):
    crawler, page, _, _ = fixture(bank, monkeypatch)
    crawler._diagnostic_stage = 'login_submit'
    if bank == 'rakuten':
        crawler._goto_twd = Mock(side_effect=RuntimeError('synthetic'))
    else:
        page.wait_for_timeout.side_effect = RuntimeError('synthetic')
    with pytest.raises(RuntimeError):
        crawler.collect(page, Mock())
    assert crawler._diagnostic_stage == ('collect_navigation' if bank == 'rakuten' else 'collect')


@pytest.mark.parametrize('bank,helper', [('cathay', '_click_login_once'), ('ctbc', '_submit_login_once'), ('rakuten', '_click_visible_login')])
def test_optional_diagnostic_hook_is_nonfatal(bank, helper, monkeypatch):
    _, page, button, _ = fixture(bank, monkeypatch)
    module = importlib.import_module(f'backend.banks.{bank}')
    getattr(module, helper)(page, before_dispatch=Mock(side_effect=RuntimeError('diagnostic only')))
    assert button.click.call_count == 1


@pytest.mark.parametrize('method,stage', [('_latest_json', 'collect_accounts'), ('_collect_loans', 'collect_loans'), ('_collect_transactions', 'collect_transactions'), ('_collect_card_statements', 'collect_cards')])
def test_sinopac_collection_resets_after_recovered_navigation(method, stage, monkeypatch):
    crawler, page, _, _ = fixture('sinopac', monkeypatch)
    page.goto.side_effect = RuntimeError('recoverable navigation')
    crawler._latest_json = Mock(return_value=None)
    crawler._collect_loans = Mock(return_value={})
    crawler._collect_transactions = Mock(return_value={'results': [], 'inventory': [], 'coverage': []})
    crawler._collect_card_statements = Mock(return_value=[])
    getattr(crawler, method).side_effect = RuntimeError('synthetic terminal')
    with pytest.raises(RuntimeError, match='synthetic terminal'):
        crawler.collect(page, Mock())
    assert crawler._diagnostic_stage == stage
    assert page.goto.call_count == 2

@pytest.mark.parametrize("navigation_fails", [False, True])
def test_sinopac_real_final_card_helper_resets_publication_stage(monkeypatch, navigation_fails):
    crawler, page, _, _ = fixture('sinopac', monkeypatch)
    page.goto.side_effect = RuntimeError('recoverable navigation') if navigation_fails else None
    crawler._latest_json = Mock(return_value=None)
    crawler._collect_loans = Mock(return_value={})
    crawler._collect_transactions = Mock(return_value={'results': [], 'inventory': [], 'coverage': []})
    crawler._collect_card_statements = Mock(return_value=[])
    # _collect_card_unbilled stays real, including its swallowed goto failure.
    module = importlib.import_module('backend.banks.sinopac')
    publish = Mock(side_effect=RuntimeError('synthetic publication'))
    monkeypatch.setattr(module, 'publish_card_bill_facts', publish)
    with pytest.raises(RuntimeError, match='synthetic publication'):
        crawler.collect(page, Mock(hits=[]))
    publish.assert_called_once()
    assert page.goto.call_count == 3
    assert crawler._diagnostic_stage == 'collect_validation'


@pytest.mark.parametrize('bank', ['cathay', 'ctbc', 'dbs', 'esun', 'fubon', 'hsbc',
                                 'linebank', 'rakuten', 'scb', 'scsb', 'sinopac', 'taishin', 'ubot'])
def test_final_publication_has_local_stage_reset(bank):
    import ast
    import inspect
    module = importlib.import_module(f'backend.banks.{bank}')
    tree = ast.parse(inspect.getsource(module))
    collect = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'collect')
    blocks = [collect.body]
    blocks += [n.body for n in ast.walk(collect) if isinstance(n, ast.FunctionDef) and n.name == 'finish']
    targets = 0
    for block in blocks:
        stage = None
        for node in block:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Attribute)
                    and node.targets[0].attr == '_diagnostic_stage'
                    and isinstance(node.value, ast.Constant)):
                stage = node.value.value
            if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id == 'publish_card_bill_facts') or (
                    isinstance(node, ast.Return) and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name) and node.value.func.id == 'BankCollectResult'):
                targets += 1
                assert stage == ('collect' if bank in ('dbs', 'scb') else 'collect_validation')
    assert targets


@pytest.mark.parametrize('bank', ['esun', 'fubon', 'hsbc', 'sinopac'])
@pytest.mark.parametrize('statement', [
    'self.financial_value = "collect_cards"',
    'self._diagnostic_stage = "not_allowed"',
    'self._diagnostic_stage = stage_from_call()',
    'other._diagnostic_stage = "collect_cards"',
    'self._diagnostic_stage = other = "collect_cards"',
    'non_diagnostic_call()',
])
def test_protected_ast_rejects_non_stage_changes(bank, statement, monkeypatch):
    import ast
    from pathlib import Path
    module = importlib.import_module(f'tests.test_{bank}_login_checkpoints')
    source_path = Path(importlib.import_module(f'backend.banks.{bank}').__file__)
    source = source_path.read_text()
    collect = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef) and n.name == 'collect')
    lines = source.splitlines(keepends=True)
    lines.insert(collect.end_lineno, '        ' + statement + '\n')
    original = Path.read_text
    monkeypatch.setattr(Path, 'read_text', lambda self, *a, **kw: ''.join(lines) if self == source_path else original(self, *a, **kw))
    test = (module.test_legacy_login_sources_are_absent_and_collect_ast_is_unchanged if bank == 'hsbc'
            else module.test_collect_and_following_helpers_keep_protected_ast_contract)
    with pytest.raises(AssertionError):
        test()
