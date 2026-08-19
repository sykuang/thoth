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

        for path in ("/MyMMA/home", "/Myasset/home", "/mma_assets/home"):
            assert crawler._logged_in(_page_proxy(real, f"https://mma.sinopac.com{path}"))
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
    crawler._ocr_captcha.assert_called_once_with(page, max_attempts=5)
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


def test_collect_and_following_helpers_keep_protected_ast_contract() -> None:
    tree = ast.parse(Path(sinopac_module.__file__).read_text())
    crawler = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SinopacCrawler"
    )
    start = next(
        index for index, node in enumerate(crawler.body)
        if isinstance(node, ast.FunctionDef) and node.name == "collect"
    )
    payload = "\n".join(
        ast.dump(node, include_attributes=False) for node in crawler.body[start:]
    )
    assert hashlib.sha256(payload.encode()).hexdigest() == (
        "d1f993c7b1c4c7a263424d5aa78385bd7f42a73da9e81c2d3cd9ae69d04f4336"
    )
