from __future__ import annotations

import ast
import hashlib
import inspect
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

import backend.banks.sinopac as sinopac_module
from backend.banks.sinopac import SinopacCrawler, SinopacLoginError
from backend.core.base import BankCrawler
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointPhase,
    evaluate_login_checkpoint,
)


def _crawler() -> SinopacCrawler:
    crawler = object.__new__(SinopacCrawler)
    crawler.name = "sinopac"
    crawler._credential_origin_allowed = lambda _page: True
    crawler.creds = SimpleNamespace(
        national_id="B123456789",
        user_code="USER-PRIVATE",
        password="PASSWORD-PRIVATE",
    )
    crawler.captcha_tmp = Path("/tmp/sinopac-test-captcha.png")
    return crawler


def _launch_browser():
    from patchright.sync_api import sync_playwright

    manager = sync_playwright()
    patchright = manager.start()
    if not Path(patchright.chromium.executable_path).exists():
        manager.__exit__(None, None, None)
        pytest.skip("Patchright browser binary is not installed")
    return manager, patchright.chromium.launch(headless=True)


def _evaluate(page, phase: CheckpointPhase):
    crawler = _crawler()
    return evaluate_login_checkpoint(
        page,
        bank="sinopac",
        phase=phase,
        rules=crawler.login_checkpoint_rules(),
        is_authenticated=lambda _page: False,
    )


def test_shared_api_and_terminal_first_rule_contract() -> None:
    crawler = _crawler()
    rules = crawler.login_checkpoint_rules()

    assert SinopacCrawler.USES_SHARED_LOGIN_CHECKPOINTS is True
    assert crawler.login_checkpoint_rules() == rules
    assert [rule.kind for rule in rules] == [
        CheckpointKind.OTP_REQUIRED,
        CheckpointKind.OTP_REQUIRED,
        CheckpointKind.PASSWORD_CHANGE_REQUIRED,
        CheckpointKind.PASSWORD_CHANGE_REQUIRED,
        CheckpointKind.EXPLICIT_LOGIN_ERROR,
        CheckpointKind.EXPLICIT_LOGIN_ERROR,
        CheckpointKind.EXPLICIT_LOGIN_ERROR,
        CheckpointKind.CAPTCHA_RETRY,
        CheckpointKind.CAPTCHA_RETRY,
        CheckpointKind.CAPTCHA_RETRY,
        CheckpointKind.UNKNOWN_BLOCKER,
        CheckpointKind.UNKNOWN_BLOCKER,
        CheckpointKind.UNKNOWN_BLOCKER,
    ]
    assert [rule.container_selector for rule in rules] == [
        ".modal.show",
        "[role='dialog']",
        ".modal.show",
        "[role='dialog']",
        ".error",
        ".alert",
        "[role='alert']",
        ".error",
        ".alert",
        "[role='alert']",
        ".modal.show",
        "[role='dialog']",
        "#imgCode",
    ]
    assert all(rule.bank == "sinopac" for rule in rules)
    assert all(rule.action_texts == () for rule in rules)
    assert all(rule.phases == tuple(CheckpointPhase) for rule in rules[:4])
    assert all(
        rule.phases == (CheckpointPhase.POST_SUBMIT, CheckpointPhase.POST_SUBMIT_SETTLE)
        for rule in rules[4:7]
    )
    assert all(
        rule.phases == (CheckpointPhase.POST_SUBMIT,) for rule in rules[7:10]
    )


@pytest.mark.parametrize(
    ("index", "positive", "negative"),
    (
        (0, "請輸入簡訊驗證碼", "請輸入驗證碼"),
        (0, "新裝置登入需要裝置驗證", "圖形驗證碼錯誤"),
        (2, "您的密碼已到期，請立即修改密碼", "密碼輸入錯誤，圖形驗證碼已過期"),
        (4, "使用者代碼或網路密碼錯誤", "使用者代碼或網路密碼錯誤說明"),
        (4, "帳號或密碼錯誤", "若帳號或密碼錯誤，請聯絡客服"),
        (4, "密碼不正確", "密碼不正確，請稍後再試"),
        (4, "密碼無效", "舊密碼無效說明"),
        (4, "身分證字號錯誤", "身分證字號錯誤原因說明"),
        (7, "驗證碼失效", "驗證碼失效原因說明"),
        (7, "驗證碼錯誤", "圖形驗證碼錯誤說明"),
        (7, "驗證碼輸入錯誤", "若驗證碼輸入錯誤"),
        (7, "請重新輸入驗證碼", "請重新輸入驗證碼以繼續登入"),
        (7, "驗證碼失效或輸入錯誤，請重新輸入。", "驗證碼"),
    ),
)
def test_rule_patterns_are_closed(index: int, positive: str, negative: str) -> None:
    pattern = _crawler().login_checkpoint_rules()[index].required_body_pattern
    assert pattern is not None
    assert pattern.fullmatch(positive)
    assert not pattern.search(negative)


def test_real_evaluator_prioritizes_terminal_and_credentials_before_captcha() -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(
            """
            <div class="modal show">請輸入簡訊驗證碼<button>登入</button></div>
            <div class="alert">驗證碼失效</div>
            <img id="imgCode">
            <script>document.body.dataset.clicks='0';document.querySelector('button').onclick=()=>document.body.dataset.clicks++</script>
            """
        )
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.kind is CheckpointKind.OTP_REQUIRED
        assert page.locator("body").get_attribute("data-clicks") == "0"

        page.set_content(
            '<div class="error">使用者代碼或網路密碼錯誤</div>'
            '<div class="alert">驗證碼失效</div><img id="imgCode">'
        )
        assert _evaluate(page, CheckpointPhase.POST_SUBMIT).kind is CheckpointKind.EXPLICIT_LOGIN_ERROR

        page.set_content('<div class="modal show">您的密碼已到期，請立即修改密碼</div>')
        assert _evaluate(page, CheckpointPhase.POST_SUBMIT_SETTLE).kind is CheckpointKind.PASSWORD_CHANGE_REQUIRED
    finally:
        browser.close()
        manager.__exit__(None, None, None)


@pytest.mark.parametrize(
    "message",
    (
        "驗證碼失效",
        "驗證碼錯誤",
        "驗證碼輸入錯誤",
        "請重新輸入驗證碼",
        "驗證碼失效或輸入錯誤，請重新輸入。",
    ),
)
def test_real_evaluator_allows_only_exact_scoped_captcha_outcomes(message: str) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(f'<div class="alert">{message}</div><img id="imgCode">')
        assert _evaluate(page, CheckpointPhase.POST_SUBMIT).kind is CheckpointKind.CAPTCHA_RETRY

        page.set_content('<label>驗證碼</label><img id="imgCode">')
        assert _evaluate(page, CheckpointPhase.POST_SUBMIT).kind is CheckpointKind.UNKNOWN_BLOCKER

        page.set_content(f'<div class="alert">{message}</div><img id="imgCode">')
        assert _evaluate(page, CheckpointPhase.POST_SUBMIT_SETTLE).kind is CheckpointKind.UNKNOWN_BLOCKER
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_unknown_modal_and_dialog_block_in_every_phase_without_clicking() -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        for attrs in ('class="modal show"', 'role="dialog"'):
            for phase in CheckpointPhase:
                page.set_content(
                    f'<div {attrs}>PRIVATE-BODY-987654<button>確定</button></div>'
                    "<script>document.body.dataset.clicks='0';document.querySelector('button').onclick=()=>document.body.dataset.clicks++</script>"
                )
                outcome = _evaluate(page, phase)
                assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
                assert "PRIVATE" not in repr(outcome)
                assert page.locator("body").get_attribute("data-clicks") == "0"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def _page_proxy(real_page, url: str):
    class Proxy:
        def __getattr__(self, name):
            return getattr(real_page, name)

    proxy = Proxy()
    proxy.url = url
    return proxy


def test_auth_requires_exact_host_known_path_private_identity_and_no_login_fields() -> None:
    manager, browser = _launch_browser()
    try:
        real = browser.new_page()
        crawler = _crawler()
        dashboard = "登出 資產總覽 " + "x" * 600
        real.set_content(f"<main>{dashboard}</main>")

        for path in (
            "/MyMMA/home",
            "/Myasset/home",
            "/mma_assets/home",
            "/mma/mymma/myasset/mma_assets_summary.aspx",
            "/mma/bank/easy_index_loan/mma_detail.aspx",
        ):
            assert crawler._logged_in(_page_proxy(real, f"https://mma.sinopac.com{path}"))
        assert not crawler._logged_in(
            _page_proxy(real, "http://mma.sinopac.com/mma/mymma/myasset/mma_assets_summary.aspx")
        )
        assert not crawler._logged_in(_page_proxy(real, "https://marketing.sinopac.com/MyMMA/home"))
        assert not crawler._logged_in(_page_proxy(real, "https://mma.sinopac.com/MemberPortal/Member/MMALogin.aspx"))
        assert not crawler._logged_in(_page_proxy(real, "https://mma.sinopac.com/public/home"))
        assert not crawler._logged_in(_page_proxy(real, "https://mma.sinopac.com/public/MyMMA/home"))

        real.set_content("<main>登出 存款 轉帳 信用卡 " + "x" * 600 + "</main>")
        assert not crawler._logged_in(_page_proxy(real, "https://mma.sinopac.com/MyMMA/home"))

        for field in (
            '<img id="imgCode" style="display:block;width:100px;height:30px">',
            '<input maxlength="6">',
            '<input maxlength="11">',
            '<input maxlength="20">',
        ):
            real.set_content(f"<main>{dashboard}</main>{field}")
            assert not crawler._logged_in(_page_proxy(real, "https://mma.sinopac.com/MyMMA/home"))

        real.set_content(
            f'<main>{dashboard}</main><iframe srcdoc="<input maxlength=20>"></iframe>'
        )
        real.wait_for_timeout(100)
        assert not crawler._logged_in(_page_proxy(real, "https://mma.sinopac.com/MyMMA/home"))
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_auth_is_locator_only_exception_safe_and_fieldless(caplog) -> None:
    page = Mock()
    page.url = "https://mma.sinopac.com/MyMMA/PRIVATE-PATH"
    page.frames = []
    page.main_frame = Mock()
    page.locator.side_effect = RuntimeError("PRIVATE-DOM-987654")

    assert _crawler()._logged_in(page) is False
    page.evaluate.assert_not_called()
    assert "PRIVATE" not in caplog.text


def _submit_fixture():
    page = Mock()
    fields = []
    specs = [
        (10, "11", "B123456789"),
        (20, "20", "USER-PRIVATE"),
        (30, "20", "PASSWORD-PRIVATE"),
        (40, "6", "123456"),
    ]
    inputs = Mock()
    inputs.count.return_value = 4
    for y, maxlength, value in specs:
        field = Mock()
        field.is_visible.return_value = True
        field.is_enabled.return_value = True
        field.bounding_box.return_value = {"x": 0, "y": y, "width": 100, "height": 20}
        field.get_attribute.side_effect = lambda attr, maximum=maxlength: maximum if attr == "maxlength" else None
        field.input_value.return_value = value
        fields.append(field)
    inputs.nth.side_effect = fields.__getitem__

    image = Mock()
    image.is_visible.return_value = True
    image.is_enabled.return_value = True
    images = Mock()
    images.count.return_value = 1
    images.nth.return_value = image

    button = Mock()
    button.is_visible.return_value = True
    button.is_enabled.return_value = True
    button.inner_text.return_value = "登入"
    button.get_attribute.return_value = None
    buttons = Mock()
    buttons.count.return_value = 1
    buttons.nth.return_value = button

    empty = Mock()
    empty.count.return_value = 0
    page.locator.side_effect = lambda selector: {
        "input": inputs,
        "#imgCode": images,
        "#MMA_Login": buttons,
        ".modal.show": empty,
        "[role='dialog']": empty,
        ".error": empty,
        ".alert": empty,
        "[role='alert']": empty,
        "body": Mock(inner_text=Mock(return_value="")),
    }[selector]
    crawler = _crawler()
    crawler._ocr_captcha = Mock(return_value="123456")
    crawler._logged_in = Mock(return_value=True)
    return crawler, page, fields, image, button, inputs, buttons


def test_submit_uses_keyboard_cardinality_fresh_ocr_and_one_native_click() -> None:
    crawler, page, fields, _image, button, _inputs, _buttons = _submit_fixture()
    crawler.submit_credentials_once(page)

    page.wait_for_selector.assert_called_once_with("#imgCode", state="visible", timeout=10000)
    for field in fields:
        assert field.click.call_args_list == [call(), call(click_count=3)]
        field.input_value.assert_called_once_with()
    assert page.keyboard.press.call_args_list == [call("Backspace")] * 4
    assert page.keyboard.type.call_args_list == [
        call("B123456789", delay=80),
        call("USER-PRIVATE", delay=80),
        call("PASSWORD-PRIVATE", delay=80),
        call("123456", delay=80),
    ]
    crawler._ocr_captcha.assert_called_once_with(page, max_attempts=1, _diagnostic=[], _required=True)
    button.click.assert_called_once_with(timeout=8000)
    page.fill.assert_not_called()
    page.evaluate.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    ("sid-count", "user-count", "captcha-count", "same-y", "hidden", "disabled", "length", "button-count", "button-label"),
)
def test_submit_pre_click_failures_are_fieldless_and_zero_submit(failure: str, caplog) -> None:
    crawler, page, fields, _image, button, inputs, buttons = _submit_fixture()
    if failure == "sid-count":
        fields[0].get_attribute.side_effect = lambda attr: "20" if attr == "maxlength" else None
    elif failure == "user-count":
        inputs.count.return_value = 3
    elif failure == "captcha-count":
        fields[3].get_attribute.side_effect = lambda attr: "11" if attr == "maxlength" else None
    elif failure == "same-y":
        fields[2].bounding_box.return_value = fields[1].bounding_box.return_value
    elif failure == "hidden":
        fields[1].is_visible.return_value = False
    elif failure == "disabled":
        fields[1].is_enabled.return_value = False
    elif failure == "length":
        fields[2].input_value.return_value = "short"
    elif failure == "button-count":
        buttons.count.return_value = 2
    else:
        button.inner_text.return_value = "登入其他"

    with pytest.raises(SinopacLoginError, match="未送出登入") as error:
        crawler.submit_credentials_once(page)
    assert error.value.__cause__ is None
    assert "PRIVATE" not in str(error.value)
    assert "PRIVATE" not in caplog.text
    button.click.assert_not_called()


def test_submit_click_and_post_click_errors_are_unknown_and_never_retried(caplog) -> None:
    crawler, page, _fields, _image, button, _inputs, _buttons = _submit_fixture()
    button.click.side_effect = RuntimeError("PRIVATE-CLICK-987654")
    with pytest.raises(SinopacLoginError, match="送出狀態不明；禁止自動重試") as error:
        crawler.submit_credentials_once(page)
    assert error.value.__cause__ is None
    assert "PRIVATE" not in str(error.value)
    button.click.assert_called_once_with(timeout=8000)

    crawler, page, _fields, _image, button, _inputs, _buttons = _submit_fixture()
    crawler._logged_in.side_effect = RuntimeError("PRIVATE-POST-987654")
    with pytest.raises(SinopacLoginError, match="狀態無法安全確認；禁止自動重試"):
        crawler.submit_credentials_once(page)
    button.click.assert_called_once_with(timeout=8000)
    assert "PRIVATE" not in caplog.text


def test_authorized_captcha_retry_hook_refreshes_unique_image_once() -> None:
    crawler, page, _fields, image, _button, _inputs, _buttons = _submit_fixture()
    crawler.prepare_captcha_resubmit(page)
    image.click.assert_called_once_with()
    page.wait_for_timeout.assert_called_once_with(1500)

    page.locator("#imgCode").count.return_value = 2
    with pytest.raises(SinopacLoginError, match="未送出登入"):
        crawler.prepare_captcha_resubmit(page)
    assert image.click.call_count == 1


def _login_html(scenario: str) -> str:
    response = {
        "captcha": "captcha.hidden=false",
        "credential": "credential.hidden=false;captcha.hidden=false",
        "generic": "generic.hidden=false",
        "dialog": "document.body.dataset.dialogs++;alert('PRIVATE-JS-DIALOG-987654')",
    }[scenario]
    return f"""
      <style>input, img, button {{ display:block; width:120px; height:24px; margin:4px; }}</style>
      <input maxlength="11"><input maxlength="20"><input maxlength="20"><input maxlength="6">
      <img id="imgCode" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==">
      <button id="MMA_Login" type="button">登入</button>
      <div class="alert" id="captcha" hidden>驗證碼失效或輸入錯誤，請重新輸入。</div>
      <div class="error" id="credential" hidden>使用者代碼或網路密碼錯誤</div>
      <div class="alert" id="generic" hidden>驗證碼</div>
      <script>
        document.body.dataset.submissions='0';
        document.body.dataset.refreshes='0';
        document.body.dataset.dialogs='0';
        imgCode.onclick=()=>{{document.body.dataset.refreshes++;captcha.hidden=true}};
        MMA_Login.onclick=()=>{{document.body.dataset.submissions++;{response}}};
      </script>
    """


@pytest.mark.parametrize(
    ("scenario", "submissions", "refreshes", "captcha_resubmits"),
    (
        ("captcha", "2", "1", "captcha_resubmits=1"),
        ("credential", "1", "0", "captcha_resubmits=0"),
        ("generic", "1", "0", "captcha_resubmits=0"),
        ("dialog", "1", "0", "captcha_resubmits=0"),
    ),
)
def test_run_level_reducer_flow_has_exact_submit_refresh_and_no_collect(
    monkeypatch,
    tmp_path,
    scenario: str,
    submissions: str,
    refreshes: str,
    captcha_resubmits: str,
) -> None:
    import backend.core.base as base_module

    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(_login_html(scenario))
        monkeypatch.setattr(page, "wait_for_timeout", lambda _milliseconds: None)
        crawler = _crawler()
        crawler.session_dir = tmp_path / scenario
        crawler.session_dir.mkdir()
        crawler._ocr_captcha = Mock(return_value="123456")
        collect = Mock()
        crawler.collect = collect
        monkeypatch.setattr(base_module, "DATA_ROOT", tmp_path)
        monkeypatch.setattr(
            base_module.StealthyFetcher,
            "fetch",
            lambda _url, *, page_action, **_kwargs: page_action(page),
        )

        result = crawler.run("https://example.invalid", headless=True)

        assert page.locator("body").get_attribute("data-submissions") == submissions
        assert page.locator("body").get_attribute("data-refreshes") == refreshes
        assert captcha_resubmits in result["error"]
        assert "PRIVATE" not in result["error"]
        collect.assert_not_called()
        if scenario == "captcha":
            assert "kind=unknown_blocker" in result["error"]
        if scenario == "dialog":
            assert page.locator("body").get_attribute("data-dialogs") == "1"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_login_is_thin_prepare_is_wait_only_and_shared_dialog_is_inherited(monkeypatch) -> None:
    crawler = _crawler()
    page = Mock()
    shared = Mock(return_value=True)
    monkeypatch.setattr(crawler, "_shared_login", shared)

    assert crawler.login(page) is True
    shared.assert_called_once_with(page)
    crawler.prepare_login_page(page)
    page.wait_for_timeout.assert_called_once_with(8000)
    assert SinopacCrawler.attach_shared_dialog_handler is BankCrawler.attach_shared_dialog_handler
    assert "attach_dialog_handler" not in SinopacCrawler.__dict__


def test_login_source_has_no_legacy_synthetic_or_private_debug_paths() -> None:
    source = inspect.getsource(sinopac_module)
    login_region = source[: source.index("    def collect(")]
    for symbol in (
        "JS_CLOSE_COOKIE",
        "JS_TAG_INPUTS",
        "JS_CLICK_LOGIN",
        "JS_STILL_LOGIN",
        "JS_LOGGED_IN_POSITIVE",
        "JS_ERR_MSG",
    ):
        assert not hasattr(sinopac_module, symbol)
    for forbidden in (
        "_last_dialog_message",
        "_last_dialog_type",
        "_login_snapshot",
        "page.fill(",
        "page.evaluate(",
        "captcha={",
        "dialog.message",
        ".accept()",
    ):
        assert forbidden not in login_region
    assert re.search(r"def login\(self, page\).*return self\._shared_login\(page\)", login_region, re.DOTALL)


def _sinopac_loan_projection(method, original):
    """Only the four reviewed loan fragments; never drop unmatched raises.

    Repayment and transaction fragments belong to their separate exact adapters;
    this projection must never consume any part of either method.
    """
    from copy import deepcopy

    method = deepcopy(method)
    dump = lambda node: ast.dump(node, include_attributes=False)

    def replace(before, after):
        expected = ast.parse(before).body
        replacement = ast.parse(after).body
        matches = 0
        for parent in ast.walk(method):
            for field, children in ast.iter_fields(parent):
                if not isinstance(children, list) or not children or not all(isinstance(n, ast.stmt) for n in children):
                    continue
                for index in range(len(children) - len(expected) + 1):
                    if [dump(n) for n in children[index:index + len(expected)]] == [dump(n) for n in expected]:
                        children[index:index + len(expected)] = deepcopy(replacement)
                        matches += 1
                        break
        assert matches == 1, ("unproved loan fragment", before, matches)

    for value, guard in (("account_raw", "inventory"), ("info_raw", "response")):
        replace(
            f"if not isinstance({value}, list) or not {value} or not isinstance({value}[0], dict) or not isinstance({value}[0].get('SubInfo'), list):\n"
            f"    raise RuntimeError('sinopac-loan-{guard}-envelope')",
            f"if not (isinstance({value}, list) and {value} and isinstance({value}[0], dict) and isinstance({value}[0].get('SubInfo'), list)):\n"
            f"    raise RuntimeError('sinopac-loan-{guard}-envelope')",
        )
    replace(
        """if body.get('Message') or not records:
    raise RuntimeError('sinopac-loan-response-records')
for record in records:
    if not isinstance(record, dict):
        raise RuntimeError('sinopac-loan-response-records')
    for key in required:
        error_id = {'LoanKind': 'SINOPAC-05-235-102', 'Currency': 'SINOPAC-05-235-103', 'LoanBalance': 'SINOPAC-05-235-104'}[key]
        if record.get(key) in (None, ''):
            raise with_error_code(RuntimeError('sinopac-loan-response-records'), error_id)""",
        """if body.get('Message') or not records or any(
    not isinstance(record, dict) or any(record.get(key) in (None, '') for key in required)
    for record in records
):
    raise RuntimeError('sinopac-loan-response-records')""",
    )
    replace(
        """for key, error_action in (('AcctValue', 'SINOPAC-05-160'), ('AcctValueFormat', 'SINOPAC-05-170')):
    with error_step(f'{error_action}-001'):
        control = page.locator('#' + key)
    with error_step(f'{error_action}-002'):
        if control.count() != 1:
            raise with_error_code(RuntimeError('sinopac-loan-restore-account'), f'{error_action}-002')
    with error_step(f'{error_action}-003'):
        if control.input_value() != account[key]:
            raise with_error_code(RuntimeError('sinopac-loan-restore-account'), f'{error_action}-003')""",
        """for key in ('AcctValue', 'AcctValueFormat'):
    control = page.locator('#' + key)
    if control.count() != 1 or control.input_value() != account[key]:
        raise RuntimeError('sinopac-loan-restore-account')""",
    )
    replace(
        """loan_control_reason = None
if isinstance(clicked, dict) and clicked.get('errorId') in ('SINOPAC-05-780-001', 'SINOPAC-05-780-002', 'SINOPAC-05-780-003'):
    loan_control_reason = clicked['errorId']
    clicked = False
if not clicked:
    raise with_error_code(RuntimeError('sinopac-loan-query-control'), loan_control_reason or 'SINOPAC-05-215-001')""",
        """if not clicked:
    raise RuntimeError('sinopac-loan-query-control')""",
    )
    old_js = [n for n in ast.walk(original) if isinstance(n, ast.Constant)
              and isinstance(n.value, str) and 'const account = document.querySelector' in n.value]
    new_js = [n for n in ast.walk(method) if isinstance(n, ast.Constant)
              and isinstance(n.value, str) and 'const account = document.querySelector' in n.value]
    assert len(old_js) == len(new_js) == 1
    before = 'if (!account || !formatted || !button) return false;'
    after = "if (!account) return {errorId:'SINOPAC-05-780-001'};\n                  if (!formatted) return {errorId:'SINOPAC-05-780-002'};\n                  if (!button) return {errorId:'SINOPAC-05-780-003'};"
    assert old_js[0].value.count(before) == 1
    assert new_js[0].value == old_js[0].value.replace(before, after)
    new_js[0].value = old_js[0].value
    assert dump(method) == dump(original), "loan projection left an unproved operand/reference"
    assert sum(isinstance(n, ast.Raise) for n in ast.walk(method)) == 10
    return method


def _sinopac_repayment_projection(method, original):
    """Finite role/primary-error projection, with the original finally intact."""
    from copy import deepcopy
    method = deepcopy(method)
    dump = lambda node: ast.dump(node, include_attributes=False)

    def replace(before, after):
        expected, replacement = ast.parse(before).body, ast.parse(after).body
        matches = 0
        for parent in ast.walk(method):
            for field, children in ast.iter_fields(parent):
                if not isinstance(children, list) or not children or not all(isinstance(n, ast.stmt) for n in children):
                    continue
                for index in range(len(children) - len(expected) + 1):
                    if [dump(n) for n in children[index:index + len(expected)]] == [dump(n) for n in expected]:
                        children[index:index + len(expected)] = deepcopy(replacement)
                        matches += 1
                        break
        assert matches == 1, ("unproved repayment fragment", before, matches)

    replace('primary_error_id = None', '')
    # Exact handler body and alias lifetime: bare re-raise, no return/cause policy
    # change, and no removal/reordering/suppression of any native cleanup.
    inner = [n for n in ast.walk(method) if isinstance(n, ast.Try) and n.finalbody]
    assert len(inner) == 1
    expected_handler = ast.parse("try:\n    pass\nexcept Exception as source_error:\n    primary_error_id = exception_error_id(source_error, bank='sinopac')\n    raise").body[0].handlers[0]
    assert [dump(n) for n in inner[0].handlers] == [dump(expected_handler)]
    assert [dump(n) for n in inner[0].finalbody] == [dump(n) for n in ast.parse("page.remove_listener('response', handler)\nobserver.close()\ncollector.attach(page)").body]
    inner[0].handlers = []
    outer = [n for n in ast.walk(method) if isinstance(n, ast.ExceptHandler)]
    assert len(outer) == 1
    expected_outer = ast.parse("try:\n    pass\nexcept Exception as source_error:\n    raise with_error_code(RuntimeError(error), primary_error_id or exception_error_id(source_error, bank='sinopac') or 'SINOPAC-05-333-001') from None").body[0].handlers[0]
    assert dump(outer[0]) == dump(expected_outer)
    outer[0].name = None
    outer[0].body = ast.parse('raise RuntimeError(error) from None').body

    replace(
        """for key, error_action in (('AcctValue', 'SINOPAC-05-100'), ('AcctValueFormat', 'SINOPAC-05-110')):
    with error_step(f'{error_action}-001'):
        control = page.locator('#' + key)
    with error_step(f'{error_action}-002'):
        if control.count() != 1:
            raise with_error_code(RuntimeError('sinopac-loan-repayments-account-control'), f'{error_action}-002')
    with error_step(f'{error_action}-003'):
        if control.input_value() != account[key]:
            raise with_error_code(RuntimeError('sinopac-loan-repayments-account-control'), f'{error_action}-003')""",
        """for key in ('AcctValue', 'AcctValueFormat'):
    control = page.locator('#' + key)
    if control.count() != 1 or control.input_value() != account[key]:
        raise RuntimeError('sinopac-loan-repayments-account-control')""",
    )
    replace(
        """for selector, error_action in (('#StartDate', 'SINOPAC-05-740'), ('#EndDate', 'SINOPAC-05-750'), ('#btnQuery', 'SINOPAC-05-760')):
    with error_step(f'{error_action}-001'):
        control = page.locator(selector)
    with error_step(f'{error_action}-002'):
        if control.count() != 1:
            raise with_error_code(RuntimeError('sinopac-loan-repayments-query-control'), f'{error_action}-005')
    with error_step(f'{error_action}-003'):
        if not control.is_visible():
            raise with_error_code(RuntimeError('sinopac-loan-repayments-query-control'), f'{error_action}-006')
    with error_step(f'{error_action}-004'):
        if not control.is_enabled():
            raise with_error_code(RuntimeError('sinopac-loan-repayments-query-control'), f'{error_action}-007')""",
        """for selector in ('#StartDate', '#EndDate', '#btnQuery'):
    control = page.locator(selector)
    if control.count() != 1 or not control.is_visible() or not control.is_enabled():
        raise RuntimeError('sinopac-loan-repayments-query-control')""",
    )
    replace(
        """if set(body) != {'HeadInfo', 'SubInfo', 'Header', 'Message'} or body.get('Header') != 'SUCCESS' or not isinstance(body.get('Message'), str) or len(body['Message']) > 2000 or not isinstance(metadata, list) or len(metadata) != 3:
    raise RuntimeError('sinopac-loan-repayments-response-metadata')
for item in metadata:
    if not isinstance(item, dict) or set(item) != {'HeadText', 'HeadAlign', 'DataAlign', 'MainShow', 'DetailShow', 'FieldKey', 'OrderIndex', 'FieldWidth'}:
        raise RuntimeError('sinopac-loan-repayments-response-metadata')
    for key, value in zip(item, item.values()):
        error_action = {'HeadText': 'SINOPAC-05-730', 'HeadAlign': 'SINOPAC-05-731', 'DataAlign': 'SINOPAC-05-732', 'MainShow': 'SINOPAC-05-733', 'DetailShow': 'SINOPAC-05-734', 'FieldKey': 'SINOPAC-05-735', 'OrderIndex': 'SINOPAC-05-736', 'FieldWidth': 'SINOPAC-05-737'}[key]
        if not isinstance(value, str):
            raise with_error_code(RuntimeError('sinopac-loan-repayments-response-metadata'), f'{error_action}-001')
        if len(value) > 2000:
            raise with_error_code(RuntimeError('sinopac-loan-repayments-response-metadata'), f'{error_action}-002')""",
        """if (set(body) != {'HeadInfo', 'SubInfo', 'Header', 'Message'} or body.get('Header') != 'SUCCESS'
    or not isinstance(body.get('Message'), str) or len(body['Message']) > 2000
    or not isinstance(metadata, list) or len(metadata) != 3
    or any(not isinstance(item, dict) or set(item) != {'HeadText', 'HeadAlign', 'DataAlign', 'MainShow', 'DetailShow', 'FieldKey', 'OrderIndex', 'FieldWidth'}
           or any(not isinstance(v, str) or len(v) > 2000 for v in item.values()) for item in metadata)):
    raise RuntimeError('sinopac-loan-repayments-response-metadata')""",
    )
    for target, field, role in (('start', 'StartDate', 'SINOPAC-05-151'), ('end', 'EndDate', 'SINOPAC-05-152')):
        suffix = 'start' if target == 'start' else 'end'
        replace(
            f"{target} = self._yyyymmdd(params[{field!r}][0], 'sinopac-loan-repayments-{suffix}-date', error_action={role!r})",
            f"{target} = self._yyyymmdd(params[{field!r}][0], 'sinopac-loan-repayments-{suffix}-date')",
        )
    assert not any(isinstance(n, ast.Name) and n.id in {'primary_error_id', 'source_error', 'error_action'} for n in ast.walk(method)), 'unproved diagnostic state reference'
    assert dump(method) == dump(original), 'repayment projection left unproved operand/reference'
    return method


def _sinopac_transaction_projection(method, original):
    """Project only the reviewed lazy roles and bounded JS metadata delta."""
    from copy import deepcopy
    from tests.test_ordered_sinopac_fubon_codes import _query_metadata_contract
    method = deepcopy(method)
    dump = lambda node: ast.dump(node, include_attributes=False)

    def replace(before, after):
        expected = ast.parse(before).body if isinstance(before, str) else before
        replacement = ast.parse(after).body if isinstance(after, str) else after
        matches = 0
        for parent in ast.walk(method):
            for field, children in ast.iter_fields(parent):
                if not isinstance(children, list) or not children or not all(isinstance(n, ast.stmt) for n in children):
                    continue
                for index in range(len(children) - len(expected) + 1):
                    if [dump(n) for n in children[index:index + len(expected)]] == [dump(n) for n in expected]:
                        children[index:index + len(expected)] = deepcopy(replacement)
                        matches += 1
                        break
        assert matches == 1, ("unproved transaction fragment", before, matches)

    replace(
        """for control, error_action in ((start_control, 'SINOPAC-05-700'), (end_control, 'SINOPAC-05-710'), (button, 'SINOPAC-05-720')):
    with error_step(f'{error_action}-001'):
        if control.count() != 1:
            raise with_error_code(RuntimeError('sinopac-twd-history-query-control'), f'{error_action}-006')
    with error_step(f'{error_action}-002'):
        if not error_step(f'{error_action}-003')(lambda: control.nth(0))().is_visible():
            raise with_error_code(RuntimeError('sinopac-twd-history-query-control'), f'{error_action}-007')
    with error_step(f'{error_action}-004'):
        if not error_step(f'{error_action}-005')(lambda: control.nth(0))().is_enabled():
            raise with_error_code(RuntimeError('sinopac-twd-history-query-control'), f'{error_action}-008')""",
        """if any(control.count() != 1 or not control.nth(0).is_visible() or not control.nth(0).is_enabled()
       for control in (start_control, end_control, button)):
    raise RuntimeError('sinopac-twd-history-query-control')""",
    )
    # Reconstruct the ENTIRE expected lowering from the original ordered OR
    # operands, including common_invalid's leading None role. No zip truncation.
    originals = [n for n in ast.walk(original) if isinstance(n, ast.Assign)
                 and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                 and n.targets[0].id in {'common_invalid', 'invalid'}]
    assert len(originals) == 3
    for assignment, roles in zip(originals, (
        tuple(f'SINOPAC-05-{i}-001' for i in range(600, 605)),
        (None, *(f'SINOPAC-05-{i}-001' for i in range(605, 618))),
        (None, *(f'SINOPAC-05-{i}-001' for i in range(618, 635))),
    ), strict=True):
        target = assignment.targets[0].id
        assert isinstance(assignment.value, ast.BoolOp) and isinstance(assignment.value.op, ast.Or)
        predicates = assignment.value.values
        assert len(predicates) == len(roles)
        loop = ast.parse(f"""for predicate_error_id, rejected in ():
    with error_step(predicate_error_id):
        if rejected():
            {target} = True
            if predicate_error_id is not None:
                dom_error_id = predicate_error_id
            break""").body[0]
        empty_args = ast.parse('lambda: None', mode='eval').body.args
        loop.iter = ast.Tuple(elts=[ast.Tuple(elts=[ast.Constant(role), ast.Lambda(args=deepcopy(empty_args), body=deepcopy(predicate))], ctx=ast.Load())
                                   for role, predicate in zip(roles, predicates, strict=True)], ctx=ast.Load())
        before = [*ast.parse(target + ' = False' + ('\ndom_error_id = None' if target == 'common_invalid' else '')).body, loop]
        replace(before, [assignment])
    replace('last_dom_detail_id = None', '')
    replace('last_dom_detail_id = dom_error_id', '')
    replace(
        """pre_dom_reason = pre_dom_marker.get('errorId') if isinstance(pre_dom_marker, dict) else None
if pre_dom_reason == 'SINOPAC-05-781-001':
    pre_dom_marker = None
else:
    pre_dom_reason = None""", '',
    )
    replace(
        """final_dom_reason = final_dom.get('errorId') if isinstance(final_dom, dict) else None
if final_dom_reason in ('SINOPAC-05-782-001', 'SINOPAC-05-782-002', 'SINOPAC-05-782-003', 'SINOPAC-05-782-004'):
    final_dom = None
else:
    final_dom_reason = None
if final_dom != stable_dom:
    raise with_error_code(RuntimeError('sinopac-twd-history-result-table'), final_dom_reason or 'SINOPAC-05-429-001')""",
        """if final_dom != stable_dom:
    raise RuntimeError('sinopac-twd-history-result-table')""",
    )
    reasons = ('unknown', 'shape', 'pager', 'error', 'mutation-shape', 'mutation-range', 'table-count', 'table-visibility',
               'nonempty-binding', 'nonempty-attestation', 'empty-no-row', 'empty-hidden-full-transaction', 'empty-hidden-date-cell',
               'empty-hidden-numeric-cell', 'empty-hidden-control', 'empty-hidden-header', 'empty-hidden-template-text',
               'empty-extra-hidden-rows', 'empty-multiple-visible-rows', 'empty-marker', 'empty-binding', 'empty-signature')
    metadata = {reason: f'SINOPAC-05-{500 + index}-001' for index, reason in enumerate(reasons)}
    expected = ast.parse("raise with_error_code(RuntimeError(f'sinopac-twd-history-result-table-{last_dom_reason}'), "
                         "(pre_dom_reason if last_dom_reason == 'table-count' else None) or "
                         "(last_dom_detail_id if last_dom_reason in ('nonempty-attestation', 'empty-signature') else None) or "
                         + repr(metadata) + ".get(last_dom_reason, 'SINOPAC-05-500-001'))").body
    replace(expected, ast.parse("raise RuntimeError(f'sinopac-twd-history-result-table-{last_dom_reason}')").body)
    # Only two exact literal preimages change. Every other JS byte is retained,
    # including the unchanged DOM probe and native cleanup/timer/state access.
    old_literals = [n for n in ast.walk(original) if isinstance(n, ast.Constant) and isinstance(n.value, str) and '=>' in n.value]
    changed = 0
    for old in old_literals:
        reviewed = _query_metadata_contract('sinopac', old.value)
        if reviewed == old.value:
            continue
        matches = [n for n in ast.walk(method) if isinstance(n, ast.Constant) and n.value == reviewed]
        assert len(matches) == 1
        matches[0].value = old.value
        changed += 1
    assert changed == 2
    forbidden = {'error_action', 'predicate_error_id', 'rejected', 'dom_error_id', 'last_dom_detail_id', 'pre_dom_reason', 'final_dom_reason'}
    assert not any(isinstance(n, ast.Name) and n.id in forbidden for n in ast.walk(method)), 'unproved transaction state reference'
    assert dump(method) == dump(original), 'transaction projection left unproved operand/reference'
    return method


def test_collect_and_following_helpers_keep_protected_ast_contract() -> None:
    from tests.test_protected_ast import normalize_tree, registered_codes

    # Diagnostic transparency is proved against read-only baseline 16f2d4b.
    tree = normalize_tree(Path(sinopac_module.__file__).read_text(), "sinopac", registered_codes("sinopac"))
    crawler = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SinopacCrawler"
    )
    start = next(
        index for index, node in enumerate(crawler.body)
        if isinstance(node, ast.FunctionDef) and node.name == "collect"
    )
    # Exact bank-owned loan projections. These cannot admit transactions or any
    # other residual: the existing whole-body fingerprints below still apply.
    from tests.test_ordered_sinopac_fubon_codes import _baseline_bank
    original_tree = normalize_tree(inspect.getsource(_baseline_bank("sinopac")), "sinopac", registered_codes("sinopac"))
    original_crawler = next(n for n in original_tree.body if isinstance(n, ast.ClassDef) and n.name == "SinopacCrawler")
    for name, project in (("_collect_loans", _sinopac_loan_projection), ("_collect_loan_repayments", _sinopac_repayment_projection), ("_collect_transactions", _sinopac_transaction_projection)):
        index = next(i for i, n in enumerate(crawler.body) if isinstance(n, ast.FunctionDef) and n.name == name)
        original = next(n for n in original_crawler.body if isinstance(n, ast.FunctionDef) and n.name == name)
        crawler.body[index] = project(crawler.body[index], original)
    # Keep a separate source projection for the unchanged 16f2 source golden;
    # the older legacy-message fingerprint below is independently preserved.
    from copy import deepcopy
    projected_source = ast.unparse(ast.fix_missing_locations(deepcopy(tree)))
    # Normalize only the reviewed diagnostic arguments, never predicates/calls/JS.
    # The original fingerprint below remains unchanged.
    from tests.test_sinopac_loan_diagnostics import LOAN_GUARDS, REPAYMENT_GUARDS

    old_loan_messages = (
        "永豐貸款帳號 API 未回傳預期結構", "永豐貸款帳號資料格式錯誤",
        "永豐貸款帳號缺少 AcctValue/AcctValueFormat", "永豐貸款查詢控制項不存在",
        "永豐貸款查詢未收到對應 API 回應", "永豐貸款明細 API HTTP 回應失敗",
        "永豐貸款明細 API 未回傳預期結構", "永豐貸款明細缺少必要欄位或銀行回覆失敗",
        "sinopac-loan-repayments", "sinopac-loan-repayments",
    )
    for method_name, expected, originals in (
        ("_collect_loans", LOAN_GUARDS, old_loan_messages),
        ("_collect_loan_repayments", (*(
            "sinopac-loan-repayments-" + suffix for suffix in REPAYMENT_GUARDS
            if suffix not in {"start-date", "end-date"}
        ), None), (None,) * 18),
    ):
        method = next(node for node in crawler.body if isinstance(node, ast.FunctionDef) and node.name == method_name)
        # Projection templates have synthetic locations. Walk lexical AST order,
        # not line-number sorting; retain EVERY raise and the strict count check.
        raises = []
        class RaiseOrder(ast.NodeVisitor):
            def visit_Raise(self, node):
                raises.append(node)
        RaiseOrder().visit(method)
        assert len(raises) == len(expected) == len(originals)
        for node, guard, original in zip(raises, expected, originals, strict=True):
            assert isinstance(node.exc, ast.Call) and isinstance(node.exc.func, ast.Name)
            assert node.exc.func.id == "RuntimeError" and len(node.exc.args) == 1 and not node.exc.keywords
            argument = node.exc.args[0]
            if guard is None:  # Keep the pre-existing suppressed-context wrapper.
                assert isinstance(argument, ast.Name) and argument.id == "error"
            else:
                assert isinstance(argument, ast.Constant) and argument.value == guard
                assert guard in SinopacCrawler.SAFE_COLLECT_GUARDS
                node.exc.args[0] = ast.Constant(original) if original is not None else ast.Name(id="error", ctx=ast.Load())
        if method_name == "_collect_loan_repayments":
            dates = sorted((node for node in ast.walk(method) if isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Attribute) and node.func.attr == "_yyyymmdd"),
                           key=lambda node: node.lineno)
            assert len(dates) == 2
            for node, suffix in zip(dates, ("start-date", "end-date"), strict=True):
                assert isinstance(node.args[1], ast.Constant)
                assert node.args[1].value == "sinopac-loan-repayments-" + suffix
                node.args[1] = ast.Name(id="error", ctx=ast.Load())
    payload = "\n".join(
        ast.dump(node, include_attributes=False) for node in crawler.body[start:]
    )
    # Rebaseline: loan repayment collection (f987d42..dbc420d) layered on main hardening.
    # Browser/guard/persistence regressions cover this intentional contract change;
    # preserve the exact AST of collect and every following helper.
    assert hashlib.sha256(payload.encode()).hexdigest() == (
        "f37aab629a4130f879bd4a473db9cbec868df564f76d45a782fd2da4d371f1d8"
    )
    # Also pin the source-owned messages before the legacy argument mapping.
    from tests.test_protected_ast import assert_protected_source

    assert_protected_source(projected_source, "sinopac")
