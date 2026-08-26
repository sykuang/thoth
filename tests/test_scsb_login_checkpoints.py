from __future__ import annotations

import ast
import base64
import hashlib
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

import backend.banks.scsb as scsb_module
import backend.core.base as base_module
from backend.banks.scsb import ScsbCrawler, ScsbLoginError
from backend.core.base import BankCrawler
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointPhase,
    DEFAULT_ACTION_SELECTOR,
    evaluate_login_checkpoint,
)


def _crawler() -> ScsbCrawler:
    crawler = object.__new__(ScsbCrawler)
    crawler.name = "scsb"
    crawler._credential_origin_allowed = lambda _page: True
    crawler.creds = SimpleNamespace(
        national_id="A123456789",
        user_code="USER-PRIVATE",
        password="PASSWORD-PRIVATE",
    )
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
    return evaluate_login_checkpoint(
        page,
        bank="scsb",
        phase=phase,
        rules=_crawler().login_checkpoint_rules(),
        is_authenticated=lambda _page: False,
    )


def test_shared_api_terminal_first_rules_and_no_retry_kinds() -> None:
    rules = _crawler().login_checkpoint_rules()
    all_phases = tuple(CheckpointPhase)
    post = (CheckpointPhase.POST_SUBMIT, CheckpointPhase.POST_SUBMIT_SETTLE)

    assert ScsbCrawler.USES_SHARED_LOGIN_CHECKPOINTS is True
    assert [rule.kind for rule in rules] == [
        CheckpointKind.OTP_REQUIRED,
        CheckpointKind.OTP_REQUIRED,
        CheckpointKind.OTP_REQUIRED,
        CheckpointKind.PASSWORD_CHANGE_REQUIRED,
        CheckpointKind.PASSWORD_CHANGE_REQUIRED,
        CheckpointKind.PASSWORD_CHANGE_REQUIRED,
        CheckpointKind.EXPLICIT_LOGIN_ERROR,
        CheckpointKind.EXPLICIT_LOGIN_ERROR,
        CheckpointKind.EXPLICIT_LOGIN_ERROR,
        CheckpointKind.DISMISSIBLE_NOTICE,
        CheckpointKind.DISMISSIBLE_NOTICE,
        CheckpointKind.UNKNOWN_BLOCKER,
        CheckpointKind.UNKNOWN_BLOCKER,
        CheckpointKind.UNKNOWN_BLOCKER,
        CheckpointKind.UNKNOWN_BLOCKER,
    ]
    assert [rule.container_selector for rule in rules] == [
        ".modal.show",
        "[role='dialog']",
        "#intro_alert.custom-modal.show",
        ".modal.show",
        "[role='dialog']",
        "#intro_alert.custom-modal.show",
        ".error",
        ".alert",
        "[role='alert']",
        "#intro_alert.custom-modal.show",
        ".custom-modal.show",
        ".custom-modal.show",
        ".modal.show",
        "[role='dialog']",
        "#verified",
    ]
    assert [rule.phases for rule in rules] == [
        all_phases,
        all_phases,
        all_phases,
        all_phases,
        all_phases,
        all_phases,
        post,
        post,
        post,
        (CheckpointPhase.PRE_SUBMIT,),
        (CheckpointPhase.PRE_SUBMIT,),
        all_phases,
        all_phases,
        all_phases,
        post,
    ]
    intro = rules[9]
    assert intro.action_selector == DEFAULT_ACTION_SELECTOR
    assert intro.action_texts == ("I got it",)
    assert intro.max_actions == 1
    fraud = rules[10]
    assert fraud.action_selector == "button.btn-gradient"
    assert fraud.action_texts == ("我知道了",)
    assert fraud.required_body_pattern is not None
    assert fraud.first_match_timeout_ms == 5000
    assert all(rule.bank == "scsb" for rule in rules)
    assert all(
        rule.kind not in {CheckpointKind.CAPTCHA_RETRY, CheckpointKind.PROTOCOL_RESUBMIT}
        for rule in rules
    )
    assert all(rule.action_texts == () for rule in (*rules[:9], *rules[11:]))


@pytest.mark.parametrize(
    ("index", "positive", "negative"),
    [
        (0, "請輸入 OTP", "圖形驗證碼錯誤"),
        (0, "需要完成裝置驗證", "請確認裝置"),
        (3, "基於安全性，您必須變更密碼", "可以稍後變更密碼"),
        (3, "密碼已到期，請立即修改密碼", "密碼輸入不正確"),
        (6, "E4025", "錯誤代碼 E40250"),
        (6, "密碼不正確", "密碼不正確時請參閱說明"),
        (6, "使用者代碼已停用。", "使用者代碼已停用時請洽客服"),
        (6, "帳號已鎖定。", "帳號未鎖定"),
        (6, "登入失敗！", "若登入失敗請確認網路"),
        (6, "驗證碼錯誤", "驗證碼錯誤排除說明"),
        (6, "Invalid credentials.", "Invalid credentials help"),
        (6, "ACCOUNT LOCKED", "Account locked troubleshooting"),
    ],
)
def test_terminal_patterns_are_bounded(index: int, positive: str, negative: str) -> None:
    pattern = _crawler().login_checkpoint_rules()[index].required_body_pattern
    assert pattern is not None
    assert pattern.fullmatch(positive)
    assert not pattern.search(negative)


def test_real_rules_click_only_exact_intro_and_terminal_collisions_never_click() -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(
            """
            <button id="outside">I got it</button>
            <div id="intro_alert" class="custom-modal show"><button>I got it</button></div>
            <script>
              document.body.dataset.inside='0'; document.body.dataset.outside='0';
              outside.onclick=()=>document.body.dataset.outside++;
              intro_alert.querySelector('button').onclick=()=>{document.body.dataset.inside++;intro_alert.hidden=true};
            </script>
            """
        )
        outcome = _evaluate(page, CheckpointPhase.PRE_SUBMIT)
        assert outcome.kind is CheckpointKind.DISMISSIBLE_NOTICE
        assert outcome.action_label == "I got it"
        assert page.locator("body").get_attribute("data-inside") == "1"
        assert page.locator("body").get_attribute("data-outside") == "0"

        page.set_content(
            """
            <div class="custom-modal show">親愛的客戶，請留意詐騙訊息
              <button class="btn-close"></button>
              <a>掌上銀App－行動認證申請操作步驟</a>
              <button class="btn btn-outline-primary">本日不再顯示</button>
              <button class="btn btn-gradient">我知道了</button>
            </div>
            <script>
              document.body.dataset.target='0'; document.body.dataset.other='0';
              document.querySelectorAll('.btn-close,a,.btn-outline-primary').forEach(
                x=>x.onclick=()=>document.body.dataset.other++
              );
              document.querySelector('.btn-gradient').onclick=()=>{
                document.body.dataset.target++;document.querySelector('div').hidden=true
              };
            </script>
            """
        )
        outcome = _evaluate(page, CheckpointPhase.PRE_SUBMIT)
        assert outcome.kind is CheckpointKind.DISMISSIBLE_NOTICE
        assert outcome.rule_name == "scsb-fraud-notice"
        assert page.locator("body").get_attribute("data-target") == "1"
        assert page.locator("body").get_attribute("data-other") == "0"

        page.set_content(
            '<div class="custom-modal show">親愛的客戶，一般通知<button>我知道了</button></div>'
            "<script>document.body.dataset.clicks='0';document.querySelector('button').onclick=()=>document.body.dataset.clicks++</script>"
        )
        outcome = _evaluate(page, CheckpointPhase.PRE_SUBMIT)
        assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
        assert outcome.rule_name == "scsb-unknown-custom-modal"
        assert page.locator("body").get_attribute("data-clicks") == "0"

        for body, kind in (
            ("請輸入 OTP", CheckpointKind.OTP_REQUIRED),
            ("您必須變更密碼", CheckpointKind.PASSWORD_CHANGE_REQUIRED),
        ):
            page.set_content(
                f'<div id="intro_alert" class="custom-modal show">{body}<button>I got it</button></div>'
                "<script>document.body.dataset.clicks='0';document.querySelector('button').onclick=()=>document.body.dataset.clicks++</script>"
            )
            outcome = _evaluate(page, CheckpointPhase.PRE_SUBMIT)
            assert outcome.kind is kind
            assert page.locator("body").get_attribute("data-clicks") == "0"

        page.set_content(
            '<div class="alert">E4025</div>'
            '<div id="intro_alert" class="custom-modal show"><button>I got it</button></div>'
            "<script>document.body.dataset.clicks='0';document.querySelector('button').onclick=()=>document.body.dataset.clicks++</script>"
        )
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.kind is CheckpointKind.EXPLICIT_LOGIN_ERROR
        assert page.locator("body").get_attribute("data-clicks") == "0"

        for label in ("OK", "Confirm", "確定"):
            page.set_content(
                f'<div class="modal show">PRIVATE-SCSB-987654<button>{label}</button></div>'
                "<script>document.body.dataset.clicks='0';document.querySelector('button').onclick=()=>document.body.dataset.clicks++</script>"
            )
            outcome = _evaluate(page, CheckpointPhase.PRE_SUBMIT)
            assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
            assert "PRIVATE" not in repr(outcome)
            assert page.locator("body").get_attribute("data-clicks") == "0"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_unknown_custom_modal_blocks_outer_lifecycle_before_submit(monkeypatch) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(
            '<div class="custom-modal show">一般通知<button>我知道了</button></div>'
            '<input id="userId"><input id="idNumber"><input id="pppd"><input id="verified">'
        )
        crawler = _crawler()
        submit = Mock()
        monkeypatch.setattr(crawler, "prepare_login_page", lambda _page: None)
        monkeypatch.setattr(crawler, "is_authenticated", lambda _page: False)
        monkeypatch.setattr(crawler, "submit_credentials_once", submit)

        with pytest.raises(base_module.LoginCheckpointBlocked):
            crawler._shared_login(page)

        submit.assert_not_called()
    finally:
        browser.close()
        manager.__exit__(None, None, None)


class _PageProxy:
    def __init__(self, page, url: str):
        self._page = page
        self.url = url

    def __getattr__(self, name):
        return getattr(self._page, name)


def test_auth_requires_exact_private_url_identity_and_fixed_menu_pair() -> None:
    manager, browser = _launch_browser()
    try:
        real = browser.new_page()
        body = "Hello Logout My Overview TWD Deposit " + "x" * 600
        real.set_content(f"<main>{body}</main>")
        crawler = _crawler()
        assert crawler._logged_in(_PageProxy(real, "https://ebank.scsb.com.tw/aply/home"))
        assert crawler._logged_in(_PageProxy(real, "https://ebank.scsb.com.tw/ibhm/home"))
        assert not crawler._logged_in(_PageProxy(real, "http://ebank.scsb.com.tw/aply/home"))
        assert not crawler._logged_in(_PageProxy(real, "https://ebank.scsb.com.tw:444/aply/home"))
        assert not crawler._logged_in(_PageProxy(real, "https://user@ebank.scsb.com.tw/aply/home"))
        assert not crawler._logged_in(_PageProxy(real, "https://ibank.scsb.com.tw/aply/home"))
        assert not crawler._logged_in(_PageProxy(real, "https://ebank.scsb.com.tw/public/home"))
        assert not crawler._logged_in(_PageProxy(real, "https://ebank.scsb.com.tw/public/aply/home"))
        assert not crawler._logged_in(_PageProxy(real, "https://ebank.scsb.com.tw.evil.example/aply/home"))

        real.set_content("<main>Hello Logout Foreign Currency Loan Investment " + "x" * 600 + "</main>")
        assert not crawler._logged_in(_PageProxy(real, "https://ebank.scsb.com.tw/aply/home"))

        real.set_content(f"<main>{body}</main><input id='verified'>")
        assert not crawler._logged_in(_PageProxy(real, "https://ebank.scsb.com.tw/aply/home"))
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_auth_is_locator_only_one_shot_exception_safe_and_prepare_only_waits(caplog) -> None:
    page = Mock()
    page.url = "https://ebank.scsb.com.tw/aply/PRIVATE"
    page.locator.side_effect = RuntimeError("PRIVATE-DOM-987654")
    assert _crawler()._logged_in(page) is False
    page.evaluate.assert_not_called()
    page.wait_for_timeout.assert_not_called()
    assert "PRIVATE" not in caplog.text

    page = Mock()
    _crawler().prepare_login_page(page)
    assert page.mock_calls == [call.wait_for_timeout(9000)]


def _submit_fixture():
    page = Mock()
    fields = {}
    values = {
        "#userId": "A123456789",
        "#idNumber": "USER-PRIVATE",
        "#pppd": "PASSWORD-PRIVATE",
        "#verified": "12345",
    }
    for selector, value in values.items():
        group = Mock()
        field = Mock()
        group.count.return_value = 1
        group.nth.return_value = field
        field.is_visible.return_value = True
        field.is_enabled.return_value = True
        field.input_value.return_value = value
        fields[selector] = (group, field)

    image_group = Mock()
    image = Mock()
    image_group.count.return_value = 1
    image_group.nth.return_value = image
    image.is_visible.return_value = True

    submits = Mock()
    submit = Mock()
    submits.count.return_value = 1
    submits.nth.return_value = submit
    submit.is_visible.return_value = True
    submit.is_enabled.return_value = True
    submit.inner_text.return_value = "Log in"
    submit.get_attribute.return_value = None

    empty = Mock()
    empty.count.return_value = 0
    mapping = {
        **{selector: group for selector, (group, _field) in fields.items()},
        ".ved_img": image_group,
        "button, input[type='submit'], input[type='button']": submits,
        ".modal.show": empty,
        "[role='dialog']": empty,
        ".error": empty,
        ".alert": empty,
        "[role='alert']": empty,
    }
    page.locator.side_effect = mapping.__getitem__
    crawler = _crawler()
    crawler._ocr_captcha = Mock(return_value="12345")
    crawler._logged_in = Mock(return_value=True)
    return crawler, page, fields, submit, submits


def test_submit_uses_exact_keyboard_sequence_cardinality_and_one_native_click() -> None:
    crawler, page, fields, submit, _submits = _submit_fixture()
    crawler.submit_credentials_once(page)

    assert page.wait_for_selector.call_args_list == [
        call("#userId", state="visible", timeout=30000),
        call(".ved_img", state="visible", timeout=15000),
    ]
    for _group, field in fields.values():
        assert field.click.call_args_list == [call(), call(click_count=3)]
        field.input_value.assert_called_once_with()
    assert page.keyboard.press.call_args_list == [call("Backspace")] * 4
    assert page.keyboard.type.call_args_list == [
        call("A123456789", delay=80),
        call("USER-PRIVATE", delay=80),
        call("PASSWORD-PRIVATE", delay=80),
        call("12345", delay=80),
    ]
    submit.click.assert_called_once_with(timeout=8000)
    page.fill.assert_not_called()
    page.evaluate.assert_not_called()


@pytest.mark.parametrize("failure", ("field-count", "hidden", "disabled", "length", "submit-count", "submit-label"))
def test_submit_pre_click_failures_are_private_fieldless_and_zero_submit(failure: str, caplog) -> None:
    crawler, page, fields, submit, submits = _submit_fixture()
    if failure == "field-count":
        fields["#userId"][0].count.return_value = 2
    elif failure == "hidden":
        fields["#idNumber"][1].is_visible.return_value = False
    elif failure == "disabled":
        fields["#pppd"][1].is_enabled.return_value = False
    elif failure == "length":
        fields["#pppd"][1].input_value.return_value = "short"
    elif failure == "submit-count":
        submits.count.return_value = 2
    else:
        submit.inner_text.return_value = "General Log in"

    with pytest.raises(ScsbLoginError, match="未送出登入") as error:
        crawler.submit_credentials_once(page)
    assert error.value.__cause__ is None
    assert "PRIVATE" not in str(error.value)
    assert "PRIVATE" not in caplog.text
    submit.click.assert_not_called()


def test_submit_click_and_post_click_errors_are_unknown_and_never_retried(caplog) -> None:
    crawler, page, _fields, submit, _submits = _submit_fixture()
    submit.click.side_effect = RuntimeError("PRIVATE-CLICK-987654")
    with pytest.raises(ScsbLoginError, match="送出狀態不明；禁止自動重試") as error:
        crawler.submit_credentials_once(page)
    assert error.value.__cause__ is None
    assert "PRIVATE" not in str(error.value)
    assert "PRIVATE" not in caplog.text
    submit.click.assert_called_once_with(timeout=8000)

    crawler, page, _fields, submit, _submits = _submit_fixture()
    page.wait_for_timeout.side_effect = RuntimeError("PRIVATE-WAIT-987654")
    with pytest.raises(ScsbLoginError, match="狀態無法安全確認；禁止自動重試"):
        crawler.submit_credentials_once(page)
    submit.click.assert_called_once_with(timeout=8000)


def test_ocr_reads_css_background_five_times_refreshes_four_and_never_leaks(monkeypatch, caplog) -> None:
    crawler = _crawler()
    page = Mock()
    images = Mock()
    image = Mock()
    images.count.return_value = 1
    images.nth.return_value = image
    image.is_visible.return_value = True
    image.evaluate.return_value = (
        'url("data:image/png;base64,' + base64.b64encode(b"PRIVATE-CAPTCHA").decode() + '")'
    )
    refreshes = Mock()
    refresh = Mock()
    refreshes.count.return_value = 1
    refreshes.nth.return_value = refresh
    refresh.is_visible.return_value = True
    refresh.is_enabled.return_value = True
    refresh.inner_text.return_value = "重新產生"
    page.locator.side_effect = lambda selector: images if selector == ".ved_img" else refreshes
    ocr = Mock(return_value=None)
    monkeypatch.setattr(scsb_module, "ocr_bytes", ocr)

    assert crawler._ocr_captcha(page, max_attempts=99) is None
    assert image.evaluate.call_args_list == [
        call("el => getComputedStyle(el).backgroundImage")
    ] * 5
    assert refresh.click.call_count == 4
    assert page.wait_for_timeout.call_args_list == [call(1500)] * 4
    assert all(item.kwargs["min_confidence"] == 0.98 for item in ocr.call_args_list)
    assert "PRIVATE-CAPTCHA" not in caplog.text


@pytest.mark.parametrize(
    "background",
    [
        "none",
        'url("https://example.test/captcha.png")',
        'url("data:text/plain;base64,QUJD")',
        'url("data:image/svg+xml;base64,QUJD")',
        'url("data:image/png;base64,not base64")',
        'linear-gradient(red, blue), url("data:image/png;base64,QUJD")',
    ],
)
def test_ocr_background_parser_is_exact_and_ambiguous_refresh_stops(background: str, monkeypatch) -> None:
    crawler = _crawler()
    page = Mock()
    images = Mock()
    image = Mock()
    images.count.return_value = 1
    images.nth.return_value = image
    image.is_visible.return_value = True
    image.evaluate.return_value = background
    refreshes = Mock()
    refreshes.count.return_value = 2
    page.locator.side_effect = lambda selector: images if selector == ".ved_img" else refreshes
    ocr = Mock()
    monkeypatch.setattr(scsb_module, "ocr_bytes", ocr)

    assert crawler._ocr_captcha(page, max_attempts=5) is None
    ocr.assert_not_called()
    assert image.evaluate.call_count == 1


def _mounted_form(error_text: str, monkeypatch):
    manager, browser = _launch_browser()
    page = browser.new_page()
    encoded = base64.b64encode(b"captcha").decode()
    page.set_content(
        f"""
        <style>.ved_img {{ display:block; background-image:url('data:image/png;base64,{encoded}'); }}</style>
        <input id="userId"><input id="idNumber"><input id="pppd"><input id="verified">
        <div class="ved_img">captcha</div><a class="chg_link">重新產生</a>
        <button id="login">Log in</button><div class="alert" hidden>{error_text}</div>
        <script>
          document.body.dataset.submissions='0'; document.body.dataset.refreshes='0';
          login.onclick=()=>{{document.body.dataset.submissions++;setTimeout(()=>document.querySelector('.alert').hidden=false,20)}};
          document.querySelector('.chg_link').onclick=()=>document.body.dataset.refreshes++;
        </script>
        """
    )
    crawler = _crawler()
    monkeypatch.setattr(crawler, "_ocr_captcha", Mock(return_value="12345"))
    monkeypatch.setattr(crawler, "prepare_login_page", lambda page: None)
    monkeypatch.setattr(crawler, "is_authenticated", lambda page: False)
    monkeypatch.setattr(crawler, "_logged_in", lambda page: False)
    real_wait = page.wait_for_timeout
    monkeypatch.setattr(page, "wait_for_timeout", lambda milliseconds: real_wait(min(milliseconds, 50)))
    return manager, browser, page, crawler


@pytest.mark.parametrize("error_text", ("E4025", "驗證碼不正確"))
def test_real_terminal_error_after_submit_is_exactly_one_submit_no_refresh_or_collect(
    error_text: str, monkeypatch, tmp_path
) -> None:
    manager, browser, page, crawler = _mounted_form(error_text, monkeypatch)
    collect = Mock()
    monkeypatch.setattr(crawler, "collect", collect)
    crawler.session_dir = tmp_path / "scsb_session"
    crawler.session_dir.mkdir()
    monkeypatch.setattr(base_module, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(
        base_module.StealthyFetcher,
        "fetch",
        lambda _url, *, page_action, **_kwargs: page_action(page),
    )
    try:
        result = crawler.run("https://ibank.scsb.com.tw/", headless=True)
        assert "LoginCheckpointBlocked" in result["error"]
        assert "explicit_login_error" in result["error"]
        assert page.locator("body").get_attribute("data-submissions") == "1"
        assert page.locator("body").get_attribute("data-refreshes") == "0"
        collect.assert_not_called()
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_login_is_thin_dialog_handler_inherited_and_legacy_login_code_removed() -> None:
    crawler = _crawler()
    page = Mock()
    shared = Mock(return_value=True)
    crawler._shared_login = shared
    assert crawler.login(page)
    shared.assert_called_once_with(page)
    assert ScsbCrawler.attach_shared_dialog_handler is BankCrawler.attach_shared_dialog_handler

    source = inspect.getsource(ScsbCrawler)
    login_region = source[: source.index("    def collect(")]
    for forbidden in (
        "JS_KILL_MODAL",
        "JS_CLICK_LOGIN",
        "JS_GRAB_BG",
        "_login_snapshot",
        "page.screenshot",
        "page.fill(",
        "page.evaluate(",
        "captcha={",
        "repr(",
    ):
        assert forbidden not in login_region
    assert login_region.count(".evaluate(") == 1
    assert 'evaluate("el => getComputedStyle(el).backgroundImage")' in login_region


def test_collect_and_following_helpers_keep_protected_ast_contract() -> None:
    tree = ast.parse(Path(scsb_module.__file__).read_text())
    crawler = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ScsbCrawler"
    )
    start = next(
        index
        for index, node in enumerate(crawler.body)
        if isinstance(node, ast.FunctionDef) and node.name == "collect"
    )
    payload = "\n".join(ast.dump(node, include_attributes=False) for node in crawler.body[start:])
    assert hashlib.sha256(payload.encode()).hexdigest() == (
        "9eb7eef8a2f13e7832a5f08c8dc84e7ed5449e97b41238135ee94a3514bc1565"
    )
