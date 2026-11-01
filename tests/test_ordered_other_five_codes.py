"""Offline action failures exercise real bank methods, never credentials/network."""
from functools import lru_cache
from pathlib import Path
import runpy
from unittest.mock import Mock

import pytest

from backend.core.error_codes import code_details, exception_error_id

BANKS = ("taishin", "rakuten", "dbs", "scb", "linebank")


@lru_cache
def _helpers(bank):
    filename = f"test_{bank}_login_checkpoints.py" if bank != "rakuten" else "test_rakuten_crawler.py"
    return runpy.run_path(str(Path(__file__).with_name(filename)))


def _fixture(bank, monkeypatch):
    helper = _helpers(bank)["_submit_fixture"]
    values = helper(monkeypatch) if bank == "rakuten" else helper()
    crawler, page = values[:2]
    if bank == "taishin":
        fields, button = list(values[3].values())[:3], values[5]
    elif bank == "scb":
        fields, button = values[2][:3], values[3]
    else:
        fields = list(values[2].values())[:(2 if bank == "dbs" else 3)]
        button = values[4] if bank == "linebank" else values[3]
    return crawler, page, fields, button


@pytest.mark.parametrize("bank", BANKS)
def test_native_credential_actions_and_fields_have_distinct_leaf_codes(bank, monkeypatch):
    observed = []
    for index in range(2 if bank == "dbs" else 3):
        for action in ("click", "input_value" if bank != "rakuten" else "press"):
            crawler, page, fields, button = _fixture(bank, monkeypatch)
            failure = RuntimeError("synthetic-private-browser-failure")
            getattr(fields[index], action).side_effect = failure
            with pytest.raises(RuntimeError) as caught:
                crawler.submit_credentials_once(page)
            error_id = exception_error_id(caught.value, bank=bank)
            assert error_id is not None, (bank, index, action)
            assert exception_error_id(failure, bank=bank) == error_id
            assert type(failure) is RuntimeError
            if bank != "rakuten":
                assert type(caught.value).__name__ == bank.capitalize() + "LoginError"
            else:
                assert caught.value is failure
            assert code_details(error_id, bank=bank)
            button.click.assert_not_called()
            observed.append(error_id)
    assert len(set(observed)) == len(observed), observed


@pytest.mark.parametrize("bank", BANKS)
def test_initial_readiness_action_has_leaf_code(bank, monkeypatch):
    crawler, page, _fields, _button = _fixture(bank, monkeypatch)
    failure = RuntimeError("synthetic-wait-failure")
    page.wait_for_timeout.side_effect = failure
    # SCB catches its first goto; fail the second original action as well.
    page.goto.side_effect = failure
    page.url = "https://synthetic.invalid/"
    crawler._logged_in = Mock(return_value=False)
    with pytest.raises(RuntimeError) as caught:
        crawler.prepare_login_page(page)
    assert exception_error_id(caught.value, bank=bank) is not None
    assert exception_error_id(failure, bank=bank) is not None


@pytest.mark.parametrize("bank", ("taishin", "dbs", "scb", "linebank"))
def test_credential_length_guards_are_field_specific(bank, monkeypatch):
    observed = []
    for index in range(2 if bank == "dbs" else 3):
        crawler, page, fields, button = _fixture(bank, monkeypatch)
        fields[index].input_value.return_value = ""
        with pytest.raises(RuntimeError, match="輸入長度不符") as caught:
            crawler.submit_credentials_once(page)
        code = exception_error_id(caught.value, bank=bank)
        assert code is not None
        observed.append(code)
        button.click.assert_not_called()
    assert len(set(observed)) == len(observed)


@pytest.mark.parametrize("bank,method,values", [
    ("taishin", "_validate_history_form", [None, {}, {"query_buttons": 1}, {"query_buttons": 1, "query_button": -1}]),
    ("taishin", "_history_money", [None, "bad", "2147483648"]),
])
def test_history_guard_reasons_are_distinct(bank, method, values):
    from backend.banks.taishin import TaishinCrawler
    observed = []
    for value in values:
        with pytest.raises(RuntimeError) as caught:
            getattr(TaishinCrawler, method)(value)
        code = exception_error_id(caught.value, bank=bank)
        assert code is not None
        observed.append(code)
    assert len(set(observed)) == len(observed), observed


def test_rakuten_required_query_result_reasons_remain_distinct():
    from backend.banks.rakuten import RakutenCrawler
    fixture = runpy.run_path(str(Path(__file__).with_name("test_rakuten_history_sync.py")))["_history_result"]
    observed = []
    for key, value in (("method", "GET"), ("status", 503), ("request_count", 2), ("response_count", 2), ("main_frame", False)):
        result = fixture()
        result["transport"][key] = value
        with pytest.raises(RuntimeError, match="rakuten-twd-history-result") as caught:
            RakutenCrawler._validated_history_result(result)
        code = exception_error_id(caught.value, bank="rakuten")
        assert code is not None
        observed.append(code)
    assert len(set(observed)) == len(observed), observed


@pytest.mark.parametrize("bank", BANKS)
def test_collection_native_failures_have_leaf_codes_without_added_reads(bank, monkeypatch):
    from backend.core.base import ResponseCollector
    crawler, page, _fields, _button = _fixture(bank, monkeypatch)
    failure = RuntimeError("synthetic-collection-private")
    if bank == "rakuten":
        page.wait_for_selector.side_effect = failure
    else:
        page.wait_for_timeout.side_effect = failure
    with pytest.raises(RuntimeError) as caught:
        crawler.collect(page, ResponseCollector())
    assert caught.value is failure
    assert exception_error_id(failure, bank=bank) is not None
    page.evaluate.assert_not_called()


@pytest.mark.parametrize("bank,method", [("scb", "_deposit_accounts"), ("taishin", "_overview_accounts")])
def test_nonfatal_account_browser_failures_keep_partial_behavior_and_fixed_cause(bank, method, monkeypatch, capsys):
    crawler, page, _fields, _button = _fixture(bank, monkeypatch)
    frame = Mock()
    page.frames = [frame]
    failure = RuntimeError("synthetic-private-account-failure")
    page.evaluate.side_effect = failure
    frame.evaluate.side_effect = failure
    assert getattr(crawler, method)(page) == []
    code = exception_error_id(failure, bank=bank)
    assert code is not None
    output = capsys.readouterr()
    assert code in output.err
    assert "synthetic-private-account-failure" not in output.err


@pytest.mark.parametrize("bank", ("taishin", "dbs"))
def test_credential_visibility_and_enabled_native_failures_are_distinct(bank, monkeypatch):
    observed = []
    for native_action in ("is_visible", "is_enabled"):
        crawler, page, fields, button = _fixture(bank, monkeypatch)
        failure = RuntimeError("synthetic-state-read-failure")
        getattr(fields[0], native_action).side_effect = failure
        with pytest.raises(RuntimeError) as caught:
            crawler.submit_credentials_once(page)
        code = exception_error_id(caught.value, bank=bank)
        assert code is not None
        assert exception_error_id(failure, bank=bank) == code
        observed.append(code)
        button.click.assert_not_called()
    assert len(set(observed)) == 2


def test_dbs_card_evidence_rejections_are_nonfatal_and_reason_specific(capsys):
    from backend.banks.dbs import _dbs_card_bill_fact
    observed = []
    for fee, payment in (
        ({"bill_due_amount": "50", "payment_due_date": "2026-09-10"}, {"dueDate": "2026-09-10"}),
        ({"bill_due_amount": "50"}, {"dueDate": "2026-09-10", "alreadyPaid": "0"}),
        ({"bill_due_amount": "50", "payment_due_date": "2026-09-11"}, {"dueDate": "2026-09-10", "alreadyPaid": "0"}),
    ):
        assert _dbs_card_bill_fact({"dbs_card_fee_page": fee, "api_responses": {"liabilities": [{"resp": {"creditCard": {"paymentDetails": payment}}}]}}) is None
        output = capsys.readouterr().err
        assert "DBS-06-" in output
        observed.append(output)
    assert len(set(observed)) == len(observed)


def test_scb_postsubmit_conflict_reasons_are_distinct_and_never_resubmit(monkeypatch):
    observed = []
    for stale, modal in ((True, True), (True, False), (False, True), (False, False)):
        crawler, page, _fields, button = _fixture("scb", monkeypatch)
        crawler._logged_in = Mock(return_value=False)
        crawler._visible_alert_state = Mock(side_effect=[(stale, False, False), (not stale, False, not modal)])
        original_locator = page.locator.side_effect
        blocker = Mock()
        blocker.count.return_value = int(modal)
        blocker.nth.return_value.is_visible.return_value = modal
        page.locator.side_effect = lambda selector: blocker if selector == ".modal.show" else original_locator(selector)
        with pytest.raises(RuntimeError, match="登入送出後狀態無法安全確認") as caught:
            crawler.submit_credentials_once(page)
        code = exception_error_id(caught.value, bank="scb")
        assert code is not None
        observed.append(code)
        button.click.assert_called_once_with(timeout=8000)
        crawler._visible_alert_state.assert_called_with(page)
        assert crawler._visible_alert_state.call_count == 2
    assert len(set(observed)) == 4


def test_rakuten_length_guards_are_field_specific(monkeypatch):
    observed = []
    for key in ("custNo", "userNo", "pcode"):
        crawler, page, _fields, button = _fixture("rakuten", monkeypatch)
        page.evaluate.return_value = {"custNo": 4, "userNo": 3, "pcode": 4, "captcha": 0, key: -1}
        with pytest.raises(RuntimeError, match="輸入長度不符") as caught:
            crawler.submit_credentials_once(page)
        code = exception_error_id(caught.value, bank="rakuten")
        assert code is not None
        observed.append(code)
        button.click.assert_not_called()
    assert len(set(observed)) == 3


def test_rakuten_ocr_action_preserves_original_exception(monkeypatch):
    import backend.banks.rakuten as module
    observed = []
    for operation in ("wait_captcha_stable", "solve_captcha"):
        values = _helpers("rakuten")["_submit_fixture"](monkeypatch, captcha_visible=True)
        crawler, page = values[:2]
        failure = RuntimeError("synthetic-ocr-failure")
        monkeypatch.setattr(module, "wait_captcha_stable", Mock())
        monkeypatch.setattr(module, "solve_captcha", Mock())
        getattr(module, operation).side_effect = failure
        with pytest.raises(RuntimeError) as caught:
            crawler.submit_credentials_once(page)
        assert caught.value is failure
        code = exception_error_id(failure, bank="rakuten")
        assert code is not None
        observed.append(code)
    assert len(set(observed)) == 2


@pytest.mark.parametrize("bank,operations", [("scb", ("bounding_box", "get_attribute")), ("linebank", ("inner_text", "get_attribute"))])
def test_form_layout_and_button_inspection_have_native_leaf_causes(bank, operations, monkeypatch):
    observed = []
    for operation in operations:
        crawler, page, fields, button = _fixture(bank, monkeypatch)
        target = fields[0] if bank == "scb" else button
        failure = RuntimeError("synthetic-inspection-failure")
        getattr(target, operation).side_effect = failure
        with pytest.raises(RuntimeError) as caught:
            crawler.submit_credentials_once(page)
        code = exception_error_id(caught.value, bank=bank)
        assert code is not None
        assert exception_error_id(failure, bank=bank) == code
        observed.append(code)
        button.click.assert_not_called()
    assert len(set(observed)) == len(operations)


def test_taishin_option_item_guard_reasons_are_distinct():
    from backend.banks.taishin import TaishinCrawler
    fixture = runpy.run_path(str(Path(__file__).with_name("test_taishin_history_sync.py")))["_form_snapshot"]
    observed = []
    for option in (None, {"index": None}, {"index": 2}, {"index": 0, "text": None}, {"index": 0, "text": "ok", "value": None}):
        value = fixture()
        value["selects"][0]["options"][0] = option
        with pytest.raises(RuntimeError, match="taishin-twd-history-form") as caught:
            TaishinCrawler._validate_history_form(value)
        observed.append(exception_error_id(caught.value, bank="taishin"))
    assert None not in observed and len(set(observed)) == len(observed)


def test_rakuten_dom_blocking_keys_have_distinct_codes():
    from backend.banks.rakuten import RakutenCrawler
    fixture = runpy.run_path(str(Path(__file__).with_name("test_rakuten_history_sync.py")))["_history_result"]
    observed = []
    for key in ("invalid_cells", "pager", "busy", "dialogs", "alerts"):
        value = fixture()["dom"]
        value[key] = 1
        with pytest.raises(RuntimeError, match="rakuten-twd-history-dom") as caught:
            RakutenCrawler._validate_history_dom(value, 1)
        observed.append(exception_error_id(caught.value, bank="rakuten"))
    assert None not in observed and len(set(observed)) == len(observed)


def test_rakuten_login_action_guard_reasons_reach_original_error_type(monkeypatch):
    observed = []
    for failure in ("missing", "duplicate", "hidden", "native_disabled", "class_disabled"):
        values = _helpers("rakuten")["_submit_fixture"](
            monkeypatch, login_count=0 if failure == "missing" else 2 if failure == "duplicate" else 1,
            login_enabled=failure != "native_disabled",
            login_classes="btn btn-primary disabled" if failure == "class_disabled" else "btn btn-primary",
        )
        crawler, page, _fields, button = values[:4]
        if failure == "hidden":
            button.is_visible.return_value = False
        with pytest.raises(RuntimeError, match="找不到唯一且可操作的登入按鈕") as caught:
            crawler.submit_credentials_once(page)
        assert type(caught.value).__name__ == "RakutenLoginError"
        observed.append(exception_error_id(caught.value, bank="rakuten"))
        button.click.assert_not_called()
    assert None not in observed and len(set(observed)) == len(observed)
    native_codes = []
    for operation in ("is_visible", "is_enabled"):
        crawler, page, _fields, button = _fixture("rakuten", monkeypatch)
        failure = RuntimeError("synthetic-login-action-inspection")
        getattr(button, operation).side_effect = failure
        with pytest.raises(RuntimeError) as caught:
            crawler.submit_credentials_once(page)
        assert caught.value is failure
        native_codes.append(exception_error_id(failure, bank="rakuten"))
        button.click.assert_not_called()
    assert None not in native_codes and len(set(native_codes)) == 2


@pytest.mark.parametrize("bank", BANKS)
def test_registered_source_codes_are_bank_local_and_complete(bank):
    import ast
    from importlib import import_module
    module = import_module(f"backend.banks.{bank}")
    catalog_module = import_module(f"backend.core.error_catalog.{bank}")
    catalog = catalog_module.CODES
    catalog_tree = ast.parse(Path(catalog_module.__file__).read_text())
    literal_catalog = next(node.value for node in catalog_tree.body if isinstance(node, ast.Assign))
    keys = [key.value for key in literal_catalog.keys]
    assert len(keys) == len(set(keys))
    tree = ast.parse(Path(module.__file__).read_text())
    codes = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
             and isinstance(node.value, str) and node.value.startswith(bank.upper() + "-")}
    assert codes <= set(catalog)  # retired aggregate IDs remain reserved
    for code in codes:
        prefix, stage, action, reason = code.split("-")
        assert prefix == bank.upper() and 1 <= int(stage) <= 7
        assert 1 <= int(action) < 900 and int(reason) >= 1
        assert code_details(code, bank=bank) == catalog[code]
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and node.exc is not None:
            assert isinstance(node.exc, ast.Call) and isinstance(node.exc.func, ast.Name)
            assert node.exc.func.id == "with_error_code"


@pytest.mark.parametrize("bank", BANKS)
def test_original_native_calls_arguments_and_static_counts_are_unchanged(bank):
    # Baseline is the audited original bank source, not a metadata-stripped rewrite.
    import ast
    from collections import Counter
    import hashlib
    from importlib import import_module
    import inspect
    import json
    dump_options = {"include_attributes": False}
    if "show_empty" in inspect.signature(ast.dump).parameters:
        dump_options["show_empty"] = True
    names = ['bounding_box', 'click', 'close', 'count', 'evaluate', 'evaluate_all', 'expect_request', 'finished', 'get_attribute', 'goto', 'inner_text', 'input_value', 'is_enabled', 'is_visible', 'locator', 'new_page', 'nth', 'press', 'press_sequentially', 'response', 'select_option', 'set_default_navigation_timeout', 'set_default_timeout', 'set_extra_http_headers', 'text_content', 'title', 'type', 'wait_for_load_state', 'wait_for_selector', 'wait_for_timeout', 'wait_for_url']
    expected = {'taishin': 'f24ff919aac37d62965b3f214bd04151dffd23a70967c4704ddb9a28c6da0205', 'rakuten': '9cbe998dde0d3d08e0c4744f176178aa1cf15d73f62b98f7aa86177ca330884c', 'dbs': '41b4cd08352d5f19c6324e5afa1bcccc2cd11635c3e9082afc3c5e4d8a9ff2b8', 'scb': '774506ccf53b95c9d012cb2e17d063c03c99da6f08905b7195300c45a9cfb43a', 'linebank': '6e820b008f53c42ae0d4601937ff183477f46ef1eade0f3e67e8cd5aabdc5c7a'}
    tree = ast.parse(Path(import_module(f"backend.banks.{bank}").__file__).read_text())

    class StripDiagnosticExpressions(ast.NodeTransformer):
        def visit_Call(self, node):
            node = self.generic_visit(node)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Call)
                    and len(node.func.args) == 1 and isinstance(node.func.args[0], ast.Lambda)
                    and isinstance(node.func.func, ast.Call)
                    and isinstance(node.func.func.func, ast.Name)
                    and node.func.func.func.id == "error_step"):
                return node.func.args[0].body
            return node

    tree = StripDiagnosticExpressions().visit(tree)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    calls = Counter()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in names):
            continue
        owner = node
        while owner in parents:
            owner = parents[owner]
            if isinstance(owner, ast.FunctionDef):
                break
        calls[(getattr(owner, "name", "module"), ast.dump(node, **dump_options))] += 1
    payload = json.dumps(sorted((fn, call, count) for (fn, call), count in calls.items()), ensure_ascii=False)
    assert hashlib.sha256(payload.encode()).hexdigest() == expected[bank]


@pytest.mark.parametrize("bank", BANKS)
def test_followup_each_native_primitive_has_its_own_boundary(bank):
    import ast
    from collections import Counter
    tree = ast.parse((Path(__file__).parents[1] / f"backend/banks/{bank}.py").read_text())
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    primitives = {"locator", "filter", "nth", "count", "inner_text", "text_content", "is_visible", "is_enabled", "click", "select_option"}
    boundaries = Counter()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in primitives):
            continue
        owner = node
        while owner in parents:
            owner = parents[owner]
            if isinstance(owner, ast.With) and any(isinstance(i.context_expr, ast.Call) and isinstance(i.context_expr.func, ast.Name) and i.context_expr.func.id == "error_step" for i in owner.items):
                break
            if isinstance(owner, ast.Call) and isinstance(owner.func, ast.Call) and isinstance(owner.func.func, ast.Name) and owner.func.func.id == "error_step":
                break
        boundaries[(getattr(owner, "lineno", None), getattr(owner, "col_offset", None))] += 1
    assert all(count == 1 for count in boundaries.values()), boundaries


@pytest.mark.parametrize("bank", ("linebank", "scb"))
def test_followup_submit_button_primitives_do_not_share_a_code(bank, monkeypatch):
    codes = []
    for action in ("is_visible", "is_enabled", "inner_text", "get_attribute"):
        crawler, page, _fields, button = _fixture(bank, monkeypatch)
        failure = RuntimeError("synthetic-native-leaf")
        getattr(button, action).side_effect = failure
        with pytest.raises(RuntimeError) as caught:
            crawler.submit_credentials_once(page)
        code = exception_error_id(caught.value, bank=bank)
        assert code is not None
        assert exception_error_id(failure, bank=bank) == code
        assert type(failure) is RuntimeError
        codes.append(code)
    assert len(set(codes)) == len(codes), codes


@pytest.mark.parametrize("action", ("locator", "nth", "select_option"))
def test_followup_taishin_history_condition_roles_are_distinct(action, monkeypatch):
    from backend.banks.taishin import TaishinCrawler
    form = {"accounts": [{"identity": "acct", "index": 0}],
            "periods": [{"identity": "period", "index": 1}],
            "sorts": [{"identity": "forward", "index": 2}],
            "account_select": 0, "period_select": 1, "sort_select": 2}
    monkeypatch.setattr(TaishinCrawler, "_history_form_snapshot", Mock(return_value={}))
    monkeypatch.setattr(TaishinCrawler, "_require_history_inventory", Mock())
    codes = []
    for role_index in range(3):
        frame = Mock()
        selects = frame.locator.return_value
        target = selects.nth.return_value
        method = {"locator": frame.locator, "nth": selects.nth, "select_option": target.select_option}[action]
        failure = RuntimeError("synthetic-condition-action")
        value = selects if action == "locator" else target if action == "nth" else None
        method.side_effect = [value] * role_index + [failure]
        with pytest.raises(RuntimeError) as caught:
            TaishinCrawler._select_history_options(frame, form, identity="acct", period="period")
        assert caught.value is failure
        codes.append(exception_error_id(failure, bank="taishin"))
        assert method.call_count == role_index + 1
    assert None not in codes and len(set(codes)) == 3, codes


@pytest.mark.parametrize("action", ("locator", "count", "nth", "is_visible"))
def test_followup_dbs_authentication_field_roles_are_distinct(action, monkeypatch):
    from backend.banks.dbs import DbsCrawler
    codes = []
    for selector in ("#username", "#password", "#loginbutton"):
        crawler, page, _fields, _button = _fixture("dbs", monkeypatch)
        page.url = "https://internet-banking.dbs.com.tw/digitw/overview"
        targets = {key: Mock() for key in ("#username", "#password", "#loginbutton")}
        for item in targets.values():
            item.count.return_value = 1
            item.nth.return_value = item
            item.is_visible.return_value = False
        failure = RuntimeError("synthetic-auth-role")
        if action == "locator":
            page.locator.side_effect = lambda key: (_ for _ in ()).throw(failure) if key == selector else targets[key]
        else:
            page.locator.side_effect = targets.__getitem__
            getattr(targets[selector], action).side_effect = failure
        assert DbsCrawler._logged_in(crawler, page) is False
        codes.append(exception_error_id(failure, bank="dbs"))
    assert None not in codes and len(set(codes)) == 3, codes


@pytest.mark.parametrize("action", ("locator", "count", "nth", "is_visible"))
def test_followup_rakuten_visible_probe_caller_roles_are_distinct(action):
    from backend.banks.rakuten import _any_visible, LOADER_SELECTOR
    codes = []
    for selector in ("input[name='otpCode']", ".modal.show:not(.modal_loading)", "#ib_init_connect_error_popup", LOADER_SELECTOR):
        page = Mock()
        locators = page.locator.return_value
        locators.count.return_value = 1
        locators.nth.return_value = locators
        locators.is_visible.return_value = True
        failure = RuntimeError("synthetic-checkpoint-role")
        getattr(page if action == "locator" else locators, action).side_effect = failure
        with pytest.raises(RuntimeError) as caught:
            _any_visible(page, selector)
        assert caught.value is failure
        codes.append(exception_error_id(failure, bank="rakuten"))
    assert None not in codes and len(set(codes)) == len(codes), codes


@pytest.mark.parametrize("bank", ("taishin", "scb"))
def test_followup_caught_ocr_failure_reaches_required_guard_and_does_not_leak(bank, monkeypatch):
    from importlib import import_module
    module = import_module(f"backend.banks.{bank}")
    cls = getattr(module, bank.capitalize() + "Crawler")
    crawler, page, _fields, button = _fixture(bank, monkeypatch)
    target = page.frames[0] if bank == "taishin" else page
    # Restore the real OCR helper that the successful submit fixture stubs.
    crawler._ocr_captcha = cls._ocr_captcha.__get__(crawler, cls)
    failure = RuntimeError("synthetic-captcha-read")
    target.evaluate.side_effect = failure
    with pytest.raises(RuntimeError) as caught:
        crawler.submit_credentials_once(page)
    native_code = exception_error_id(failure, bank=bank)
    assert native_code is not None
    assert exception_error_id(caught.value, bank=bank) == native_code
    button.click.assert_not_called()
    # A new normal nonmatch must use its own terminal guard, not the prior read error.
    target.evaluate.side_effect = None
    target.evaluate.return_value = None
    with pytest.raises(RuntimeError) as fresh:
        crawler.submit_credentials_once(page)
    assert exception_error_id(fresh.value, bank=bank) != native_code
    assert getattr(crawler, "_ocr_failure_id", None) is None


def test_followup_overview_failure_reaches_required_empty_guard(monkeypatch):
    from backend.core.base import ResponseCollector
    crawler, page, _fields, _button = _fixture("taishin", monkeypatch)
    failure = RuntimeError("synthetic-overview-read")
    page.frames[0].evaluate.side_effect = failure
    with pytest.raises(RuntimeError, match="taishin-accounts-empty") as caught:
        crawler.collect(page, ResponseCollector())
    assert exception_error_id(caught.value, bank="taishin") == exception_error_id(failure, bank="taishin")


def test_followup_rakuten_caught_modal_failure_reaches_required_guard(monkeypatch):
    crawler, page, _fields, button = _fixture("rakuten", monkeypatch)
    failure = RuntimeError("synthetic-modal-count")
    modals = Mock()
    modals.count.side_effect = failure
    page.locator.side_effect = None
    page.locator.return_value = modals
    with pytest.raises(RuntimeError, match="登入前仍有未處理互動視窗") as caught:
        crawler.submit_credentials_once(page)
    assert exception_error_id(caught.value, bank="rakuten") == exception_error_id(failure, bank="rakuten")
    assert getattr(crawler, "_modal_failure_id", None) is None
    button.click.assert_not_called()


_NATIVE_TRACE_ACTIONS = {
    "locator", "filter", "nth", "count", "inner_text", "text_content", "is_visible",
    "is_enabled", "click", "move", "query_selector_all", "select_option", "get_attribute", "get_by_role",
    "wait_for_timeout", "wait_for_selector", "wait_for_url", "press", "type",
    "press_sequentially", "input_value", "bounding_box", "evaluate", "evaluate_all", "expect_request", "response", "finished",
}


def _trace_value(value):
    import hashlib
    import re
    if isinstance(value, str):
        return value if len(value) < 160 else {"literal_sha256": hashlib.sha256(value.encode()).hexdigest()}
    if isinstance(value, re.Pattern):
        return (value.pattern, value.flags)
    if callable(value):
        return "callback"
    if isinstance(value, (list, tuple)):
        return [_trace_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _trace_value(item) for key, item in value.items()}
    return value


@lru_cache
def _frozen_bank_module(bank):
    import subprocess
    from types import ModuleType
    module = ModuleType(f"backend.banks.{bank}.frozen_offline")
    module.__file__ = str(Path(__file__).parents[1] / f"backend/banks/{bank}.py")
    source = subprocess.check_output(
        ["git", "show", f"16f2d4b:backend/banks/{bank}.py"],
        cwd=Path(__file__).parents[1], text=True,
    )
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def _native_scenario(bank, scenario, module, monkeypatch):
    cls = getattr(module, bank.capitalize() + "Crawler")
    crawler, page, fields, button = _fixture(bank, monkeypatch)
    crawler.__class__ = cls
    if scenario == "submit_hidden":
        button.is_visible.return_value = False
        scenario = "submit"
    if scenario == "post_form":
        page.locator(".modal.show").count.return_value = 0
        fields[0].is_visible.return_value = True
        return lambda: crawler.submit_credentials_once(page)
    if scenario.startswith("post_"):
        target_selector = {"modal": ".modal.show", "dialog": "[role='dialog']", "error": ".error", "alert": ".alert", "role_alert": "[role='alert']"}[scenario[5:]]
        previous = page.locator.side_effect
        responses = {}
        for selector in (".modal.show", "[role='dialog']", ".error", ".alert", "[role='alert']"):
            item = Mock()
            item.count.return_value = int(selector == target_selector)
            item.nth.return_value = item
            item.is_visible.return_value = True
            responses[selector] = item
        page.locator.side_effect = lambda selector: responses[selector] if selector in responses else previous(selector)
        crawler._logged_in = Mock(side_effect=[False, True])
        if bank == "taishin":
            page.frames = [page.frames[0]]
        if bank == "scb":
            crawler._visible_alert_state = Mock(return_value=(False, False, False))
        return lambda: crawler.submit_credentials_once(page)
    if scenario == "submit":
        if bank == "rakuten":
            crawler._semantic_modal_visible_in_owned_scope = Mock(return_value=False)
        return lambda: crawler.submit_credentials_once(page)
    if scenario == "auth":
        page.url = {
            "taishin": "https://my.taishinbank.com.tw/overview",
            "rakuten": "https://www.rakuten-bank.com.tw/ebank/overview",
            "dbs": "https://internet-banking.dbs.com.tw/digitw/overview",
            "scb": "https://ebank.standardchartered.com.tw/scb/overview",
            "linebank": "https://accessibility.linebank.com.tw/overview",
        }[bank]
        page.frames = []
        page.main_frame = page
        locator = Mock()
        locator.count.return_value = 1
        locator.nth.return_value = locator
        locator.is_visible.return_value = False
        locator.inner_text.return_value = "帳戶總覽 資產總覽 登出 " * 100
        page.locator.side_effect = None
        page.locator.return_value = locator
        page.evaluate.return_value = "帳戶總覽 理財總覽 登出 " * 100 if bank in {"taishin", "scb"} else True
        return lambda: cls._logged_in(crawler, page)
    if scenario == "conditions":
        form = {"accounts": [{"identity": "acct", "index": 0}],
                "periods": [{"identity": "period", "index": 1}],
                "sorts": [{"identity": "forward", "index": 2}],
                "account_select": 0, "period_select": 1, "sort_select": 2}
        monkeypatch.setattr(cls, "_history_form_snapshot", Mock(return_value={}))
        monkeypatch.setattr(cls, "_require_history_inventory", Mock())
        monkeypatch.setattr(cls, "_history_selected_options", Mock(return_value={
            "selected_identity": "acct", "selected_period": "period", "selected_sort": "forward"}))
        page.locator.side_effect = None
        return lambda: cls._select_history_options(page, form, identity="acct", period="period")
    if scenario in {"collect_empty", "collect_noquery", "collect_selected_false"}:
        from backend.core.base import ResponseCollector
        monkeypatch.setattr(module, "_debug_dir", Mock(return_value=Path("offline-no-write")))
        opts = [] if scenario == "collect_empty" else [{"value": "synthetic-account", "text": "synthetic-account"}]
        results = ["home", [], "transaction", {"opts": opts}]
        if opts:
            # Preserve the existing ignored false selection result and missing-query policy.
            results += [False, scenario == "collect_selected_false"]
            if scenario == "collect_selected_false":
                results += ["account-text"]
        page.evaluate.side_effect = results
        return lambda: crawler.collect(page, ResponseCollector())
    if scenario in {"history", "history_retry"}:
        from datetime import date
        from types import SimpleNamespace
        frame = Mock()
        page.frames = [frame]
        crawler._history_frame = Mock(return_value=frame)
        crawler._history_form_snapshot = Mock(return_value={})
        crawler._validate_history_form = Mock(return_value={"accounts": [{"identity": "acct"}], "query_button": 0})
        if scenario == "history_retry":
            from backend.core.error_codes import with_error_code
            failure = with_error_code(RuntimeError("synthetic-reinventory"), "TAISHIN-05-041-001")
            crawler._history_form_snapshot.side_effect = [{}, {}, {}, {}] + [failure] * 60
            crawler._validate_history_form.return_value["accounts"].append({"identity": "second"})
        crawler._select_history_options = Mock()
        crawler._history_window = Mock(return_value={"start": date(2026, 1, 1), "end": date(2026, 2, 1), "period": "period"})
        state = {"table_count": 1, "no_result_count": 0, "dialog_count": 0, "error_count": 0,
                 "busy_count": 0, "more_button_count": 0, "quiet_ms": 1500, "rows": [1]}
        counter = iter(range(100))
        def snapshot(*args, **kwargs):
            index = next(counter)
            if index == 0:
                return {**state, "table_count": 0}
            if index == 1:
                return {**state, "more_button_count": 1, "rows": []}
            return dict(state)
        crawler._history_result_snapshot = snapshot
        crawler._validate_history_hit = Mock(return_value={"row_count": 1, "rows": [], "transport": {}})
        crawler._validate_history_snapshot = Mock(return_value={"status": "complete", "rows": []})
        crawler._require_history_inventory = Mock()
        crawler._shared_dialog_blocked = False
        hit = SimpleNamespace(request_sequence=1, raw_url="https://my.taishinbank.com.tw/TIBNetBank/svc/web1/rb0102/query", body_size=100)
        collector = SimpleNamespace(request_sequence=0, issued_count=Mock(side_effect=[0, 1]), hits=[hit])
        crawler._require_history_network_quiescence = Mock(return_value=hit)
        locators = frame.locator.return_value
        locators.count.return_value = 1
        locators.nth.return_value = locators
        return lambda: crawler._collect_attested_twd_history(page, collector, as_of=date(2026, 2, 1))
    if scenario == "ancestor":
        frame = page.frames[0]
        frame.evaluate.side_effect = [
            {"x": 1, "y": 1, "w": 10, "h": 10},
            [{"text": "查詢信用卡明細", "x": 1, "y": 1, "w": 10, "h": 10}],
        ]
        element = Mock()
        element.get_attribute.return_value = "svc/rwd"
        element.bounding_box.return_value = {"x": 0, "y": 0}
        page.query_selector_all.return_value = [element]
        return lambda: crawler._try_ancestor_clicks(frame, page)
    if scenario == "card_month":
        from backend.core.base import ResponseCollector
        frame = page.frames[0]
        found = [{"y": 100, "click_visible": True}]
        page.evaluate.return_value = found
        frame.evaluate.side_effect = [found, "信用卡", [{"index": 1, "text": "2026/01"}], None, "信用卡"]
        crawler._overview_accounts = Mock(return_value=[{"accountNo": "acct", "balance": "0"}])
        crawler._try_ancestor_clicks = Mock(return_value=True)
        crawler._parse_credit_card_page = Mock(return_value={})
        crawler._collect_attested_twd_history = Mock(return_value={})
        crawler._non_sensitive_api_responses = Mock(return_value={})
        frame.locator.side_effect = None
        return lambda: crawler.collect(page, ResponseCollector())
    if scenario.startswith("wait_"):
        role = scenario[5:]
        mismatch = role.startswith("mismatch_")
        role = role.removeprefix("mismatch_")
        label = "123456789012" if role == "account" else "2026/01"
        crawler._selected_label = Mock(side_effect=["nonmatch", label, label, label]) if mismatch else Mock(return_value=label)
        page.evaluate.return_value = {"rows": "ready", "account": "123456789012", "month": "2026/01", "balance": "1"}
        monkeypatch.setattr(module.time, "monotonic", Mock(side_effect=[0, 0.1, 0.2, 0.3, 0.4]))
        kwargs = {} if role == "initial" else {"selected_root": "simple-dropdown2" if role == "account" else "simple-dropdown", "selected_label": label}
        return lambda: crawler._wait_for_twd_view(page, **kwargs)
    if scenario == "logout":
        frame = Mock()
        page.frames = [frame]
        candidates = frame.locator.return_value
        candidates.filter.return_value = candidates
        candidates.count.return_value = 1
        candidates.nth.return_value = candidates
        candidates.is_visible.return_value = True
        page.locator.side_effect = None
        modals = page.locator.return_value
        modals.count.return_value = 1
        modal = modals.nth.return_value
        body, submit = Mock(), Mock()
        body.inner_text.return_value = cls.LOGOUT_BODY
        modal.locator.side_effect = lambda selector: body if selector == ".modal-body" else submit
        submit.filter.return_value = submit
        submit.count.return_value = 1
        submit.first.is_visible.return_value = True
        return lambda: crawler.logout(page)
    if scenario.startswith(("alerts_", "resubmit_")):
        prefix, role = scenario.split("_", 1)
        target_selector = {"error": ".error", "alert": ".alert", "role_alert": "[role='alert']"}[role]
        page.frames = []
        locators = {}
        for selector in (".error", ".alert", "[role='alert']"):
            item = Mock()
            item.count.return_value = int(selector == target_selector)
            item.nth.return_value = item
            item.is_visible.return_value = True
            item.inner_text.return_value = "normal"
            locators[selector] = item
        page.locator.side_effect = locators.__getitem__
        if prefix == "alerts":
            return lambda: cls._visible_alert_state(page)
        crawler._click_unique_refresh = Mock(return_value=True)
        return lambda: crawler.prepare_captcha_resubmit(page)
    if scenario.startswith("ocr_"):
        target = page.frames[0] if bank == "taishin" else page
        target.locator.side_effect = None
        actions = target.locator.return_value
        actions.count.return_value = 1
        actions.nth.return_value = actions
        actions.is_visible.return_value = scenario != "ocr_hidden"
        actions.is_enabled.return_value = True
        actions.inner_text.return_value = "重新產生"
        if bank == "taishin":
            target.evaluate.return_value = None
        else:
            page.frames = []
            target.evaluate.return_value = "data:image/png;base64,QQ=="
            monkeypatch.setattr(module, "ocr_bytes", Mock(return_value=None))
        return lambda: cls._ocr_captcha(crawler, target, max_attempts=2)
    if scenario == "refresh":
        page.frames = []
        page.locator.side_effect = None
        action = page.locator.return_value
        action.count.return_value = 1
        action.nth.return_value = action
        action.is_visible.return_value = True
        action.is_enabled.return_value = True
        action.inner_text.return_value = "重新產生"
        return lambda: cls._click_unique_refresh(page)
    root = "simple-dropdown2" if scenario.endswith("account") else "simple-dropdown"
    page.locator.side_effect = None
    locators = page.locator.return_value
    locators.nth.return_value = locators
    locators.locator.return_value = locators
    locators.count.return_value = 1
    locators.is_visible.return_value = True
    locators.inner_text.return_value = "label"
    locators.text_content.return_value = "label"
    if scenario.startswith("labels_"):
        locators.evaluate_all.return_value = [{"label": "label", "visible": True}]
        monkeypatch.setattr(module.time, "monotonic", Mock(side_effect=[0, 0.1, 0.2, 0.3, 1.3]))
        return lambda: cls._visible_labels(page, root)
    if scenario.startswith("selected_"):
        return lambda: cls._selected_label(page, root)
    if scenario.startswith("open_"):
        return lambda: cls._open_dropdown(page, root)
    if scenario.startswith("options_"):
        return lambda: cls._selection_options(page, root)
    if scenario.startswith("select_"):
        if scenario == "select_visible_account":
            trigger = Mock()
            trigger.count.return_value = 0
            page.locator.side_effect = lambda selector: trigger if "aria-expanded" in selector else locators
        from contextlib import nullcontext
        from types import SimpleNamespace
        request = Mock()
        request.frame = page.main_frame
        request.response.return_value.status = 200
        page.expect_request.return_value = nullcontext(SimpleNamespace(value=request))
        collector = SimpleNamespace(request_sequence=0, issued_count=Mock(side_effect=[0, 1, 1, 1, 1, 1]), hits=[SimpleNamespace(
            request_sequence=1, raw_url="https://www.rakuten-bank.com.tw" + module.QUERY_PATH,
            url="https://www.rakuten-bank.com.tw" + module.QUERY_PATH,
            method="POST", status=200, content_type="application/json", redirected=False,
            main_frame_request=True)])
        page.evaluate.return_value = {"rows": "before"}
        crawler._wait_for_twd_view = Mock()
        return lambda: crawler._select_label(page, collector, root, "label")
    if scenario == "goto":
        return lambda: crawler._goto_twd(page)
    if scenario.startswith("visible"):
        selector = {"visible_modal": ".modal.show:not(.modal_loading)", "visible_error": "#ib_init_connect_error_popup", "visible_loader": module.LOADER_SELECTOR}.get(scenario, "input[name='otpCode']")
        return lambda: module._any_visible(page, selector)
    if scenario == "semantic":
        locators.inner_text.return_value = ""
        return lambda: module._semantic_modal_visible(page)
    raise AssertionError(scenario)


_TRACE_SCENARIOS = [(bank, scenario) for bank in BANKS for scenario in ("submit", "submit_hidden", "auth")] + [
    ("taishin", "conditions"), ("taishin", "history"), ("taishin", "card_month"), ("taishin", "ancestor"),
    *(("linebank", scenario) for scenario in ("collect_empty", "collect_noquery", "collect_selected_false")),
    ("scb", "refresh"), ("rakuten", "logout"), ("linebank", "post_form"),
    *((bank, scenario) for bank in ("taishin", "scb") for scenario in ("ocr_refresh", "ocr_hidden")),
    *(("rakuten", f"wait_{role}") for role in ("initial", "account", "month")),
    *(("scb", f"{operation}_{role}") for operation in ("alerts", "resubmit") for role in ("error", "alert", "role_alert")),
    *( ("rakuten", f"{operation}_{role}") for operation in ("selected", "open", "options", "select", "labels") for role in ("account", "month") ),
    ("rakuten", "goto"), ("rakuten", "visible"), ("rakuten", "semantic"),
    *(("rakuten", scenario) for scenario in ("visible_modal", "visible_error", "visible_loader", "select_visible_account", "wait_mismatch_account", "wait_mismatch_month")),
    *((bank, f"post_{role}") for bank in ("taishin", "dbs", "scb") for role in ("modal", "dialog")),
    *(("dbs", f"post_{role}") for role in ("error", "alert", "role_alert")),
]


def _run_native_trace(bank, scenario, module, fail_index=None):
    # Instrument mock native primitives, not the production method or its branches.
    trace = []
    failure = RuntimeError("synthetic-native-trace")
    original_call = Mock.__call__
    with pytest.MonkeyPatch.context() as monkeypatch:
        run = _native_scenario(bank, scenario, module, monkeypatch)

        def native_call(mock, *args, **kwargs):
            name = mock._mock_name
            if name in _NATIVE_TRACE_ACTIONS:
                trace.append([name, _trace_value(args), _trace_value(kwargs)])
                if len(trace) - 1 == fail_index:
                    raise failure
            return original_call(mock, *args, **kwargs)

        monkeypatch.setattr(Mock, "__call__", native_call)
        try:
            result = run()
            outcome = ("returned", result if isinstance(result, (str, bool, type(None))) else type(result).__name__)
        except Exception as error:
            outcome = ("raised", type(error).__name__, str(error))
    return trace, outcome, failure


@pytest.mark.parametrize("bank,scenario", _TRACE_SCENARIOS)
def test_followup_actual_method_native_trace_matrix(bank, scenario):
    from importlib import import_module
    module = import_module(f"backend.banks.{bank}")
    frozen = _frozen_bank_module(bank)
    before, before_outcome, _ = _run_native_trace(bank, scenario, frozen)
    after, after_outcome, _ = _run_native_trace(bank, scenario, module)
    assert before, (bank, scenario, before_outcome)
    assert before == after
    assert before_outcome == after_outcome
    for index in range(len(before)):
        old_trace, old_outcome, _ = _run_native_trace(bank, scenario, frozen, index)
        new_trace, new_outcome, failure = _run_native_trace(bank, scenario, module, index)
        assert old_trace == new_trace, (bank, scenario, index)
        assert old_outcome == new_outcome, (bank, scenario, index, old_outcome, new_outcome)
        assert exception_error_id(failure, bank=bank) is not None, (bank, scenario, index, before[index])


@pytest.mark.parametrize("bank,roles", [
    ("taishin", ("modal", "dialog")), ("scb", ("modal", "dialog")),
    ("dbs", ("modal", "dialog", "error", "alert", "role_alert")),
])
def test_followup_postsubmit_blocker_roles_do_not_collide(bank, roles):
    from importlib import import_module
    module = import_module(f"backend.banks.{bank}")
    role_codes = []
    for role in roles:
        scenario = f"post_{role}"
        trace, _outcome, _failure = _run_native_trace(bank, scenario, module)
        selector = {"modal": ".modal.show", "dialog": "[role='dialog']", "error": ".error", "alert": ".alert", "role_alert": "[role='alert']"}[role]
        start = next(index for index, call in enumerate(trace) if call[0] == "locator" and call[1] == [selector])
        observed = []
        for index in range(start, start + 4):
            _trace, _outcome, failure = _run_native_trace(bank, scenario, module, index)
            observed.append(exception_error_id(failure, bank=bank))
        assert None not in observed and len(set(observed)) == 4
        role_codes.extend(observed)
    assert len(set(role_codes)) == len(role_codes), role_codes


@pytest.mark.parametrize("operation", ("selected", "open", "options", "select", "labels"))
def test_followup_rakuten_account_and_month_native_roles_do_not_collide(operation):
    import backend.banks.rakuten as module
    all_codes = []
    for role in ("account", "month"):
        scenario = f"{operation}_{role}"
        trace, _outcome, _failure = _run_native_trace("rakuten", scenario, module)
        codes = set()
        for index in range(len(trace)):
            _trace, _outcome, failure = _run_native_trace("rakuten", scenario, module, index)
            codes.add(exception_error_id(failure, bank="rakuten"))
        assert None not in codes
        all_codes.append(codes)
    assert all_codes[0].isdisjoint(all_codes[1]), all_codes[0] & all_codes[1]


@pytest.mark.parametrize("operation", ("alerts", "resubmit"))
def test_followup_scb_alert_caller_roles_are_distinct(operation):
    import backend.banks.scb as module
    all_codes = []
    for role in ("error", "alert", "role_alert"):
        scenario = f"{operation}_{role}"
        trace, _outcome, _failure = _run_native_trace("scb", scenario, module)
        selector = {"error": ".error", "alert": ".alert", "role_alert": "[role='alert']"}[role]
        start = next(index for index, call in enumerate(trace) if call[0] == "locator" and call[1] == [selector])
        codes = set()
        for index in range(start, start + 5):
            _trace, _outcome, failure = _run_native_trace("scb", scenario, module, index)
            codes.add(exception_error_id(failure, bank="scb"))
        assert None not in codes and len(codes) == 5
        all_codes.extend(codes)
    assert len(set(all_codes)) == len(all_codes), all_codes


def test_followup_taishin_required_inventory_guard_keeps_last_caught_read():
    import backend.banks.taishin as module
    from backend.core.base import ResponseCollector
    from datetime import date
    crawler = object.__new__(module.TaishinCrawler)
    page, frame = Mock(), Mock()
    crawler._history_frame = Mock(return_value=frame)
    failure = RuntimeError("synthetic-required-form-read")
    frame.evaluate.side_effect = [None] + [failure] * 60
    with pytest.raises(RuntimeError, match="taishin-twd-history-form") as caught:
        crawler._collect_attested_twd_history(page, ResponseCollector(), as_of=date(2026, 2, 1))
    assert exception_error_id(failure, bank="taishin") is not None
    assert exception_error_id(caught.value, bank="taishin") == exception_error_id(failure, bank="taishin")
    assert frame.evaluate.call_count == 61


def test_followup_scb_caught_captcha_decode_has_fixed_reason(monkeypatch):
    import backend.banks.scb as module
    crawler, page, _fields, button = _fixture("scb", monkeypatch)
    crawler._ocr_captcha = module.ScbCrawler._ocr_captcha.__get__(crawler, module.ScbCrawler)
    page.evaluate.return_value = "data:image/png;base64,invalid"
    failure = RuntimeError("synthetic-decode")
    monkeypatch.setattr(module.base64, "b64decode", Mock(side_effect=failure))
    with pytest.raises(RuntimeError) as caught:
        crawler.submit_credentials_once(page)
    assert exception_error_id(failure, bank="scb") is not None
    assert exception_error_id(caught.value, bank="scb") == exception_error_id(failure, bank="scb")
    button.click.assert_not_called()



def test_followup_rakuten_required_view_read_roles_are_distinct():
    import backend.banks.rakuten as module
    codes = []
    for scenario in ("wait_initial", "wait_account", "wait_month", "select_account", "select_month"):
        trace, _outcome, _failure = _run_native_trace("rakuten", scenario, module)
        index = next(index for index, call in enumerate(trace) if call[0] == "evaluate")
        _trace, _outcome, failure = _run_native_trace("rakuten", scenario, module, index)
        codes.append(exception_error_id(failure, bank="rakuten"))
    assert None not in codes and len(set(codes)) == len(codes), codes



def test_followup_taishin_required_reinventory_guard_keeps_last_caught_failure():
    import backend.banks.taishin as module
    with pytest.MonkeyPatch.context() as monkeypatch:
        run = _native_scenario("taishin", "history_retry", module, monkeypatch)
        with pytest.raises(RuntimeError, match="taishin-twd-history-inventory") as caught:
            run()
    assert exception_error_id(caught.value, bank="taishin") == "TAISHIN-05-041-001"


@pytest.mark.parametrize("bank", ("taishin", "scb"))
def test_followup_ocr_carry_resets_on_retry_success_and_new_operation(bank, monkeypatch):
    from importlib import import_module
    module = import_module(f"backend.banks.{bank}")
    cls = getattr(module, bank.capitalize() + "Crawler")
    crawler, page, _fields, _button = _fixture(bank, monkeypatch)
    target = page.frames[0] if bank == "taishin" else page
    failure = RuntimeError("synthetic-ocr-transient")
    image = "QQ==" if bank == "taishin" else "data:image/png;base64,QQ=="
    monkeypatch.setattr(module, "ocr_bytes", Mock(return_value="123456"))
    if bank == "scb":
        crawler._click_unique_refresh = Mock(return_value=True)
        target.evaluate.side_effect = [failure, image]
        assert cls._ocr_captcha(crawler, target, max_attempts=2) == "123456"
    else:
        target.evaluate.side_effect = failure
        assert cls._ocr_captcha(crawler, target, max_attempts=1) is None
        target.evaluate.side_effect = None
        target.evaluate.return_value = image
        assert cls._ocr_captcha(crawler, target, max_attempts=1) == "123456"
    assert crawler._ocr_failure_id is None
    target.evaluate.side_effect = None
    target.evaluate.return_value = image
    assert cls._ocr_captcha(crawler, target, max_attempts=1) == "123456"
    assert crawler._ocr_failure_id is None


def test_followup_overview_partial_success_and_new_operation_do_not_use_stale_cause(monkeypatch):
    from backend.core.base import ResponseCollector
    crawler, page, _fields, _button = _fixture("taishin", monkeypatch)
    bad, good = Mock(), Mock()
    bad.evaluate.side_effect = RuntimeError("synthetic-optional-frame")
    good.evaluate.return_value = [["未設定", "1234-56-1234567-8", "0", "0", "10"]]
    page.frames = [bad, good]
    crawler._collect_attested_twd_history = Mock(return_value={})
    crawler._non_sensitive_api_responses = Mock(return_value={})
    result = crawler.collect(page, ResponseCollector())
    assert result.api_responses["query"]["OUTPUTDATA"]["SavingAccount"][0]["balance"] == "10"
    page.frames = []
    with pytest.raises(RuntimeError, match="taishin-accounts-empty") as caught:
        crawler.collect(page, ResponseCollector())
    assert exception_error_id(caught.value, bank="taishin") == "TAISHIN-04-004-001"
