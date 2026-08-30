from __future__ import annotations

import ast
import hashlib
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

import backend.banks.fubon as fubon_module
from backend.banks.fubon import FubonCrawler, FubonLoginError, _safe_url
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointPhase,
    evaluate_login_checkpoint,
)


def _crawler() -> FubonCrawler:
    crawler = object.__new__(FubonCrawler)
    crawler.name = "fubon"
    crawler._credential_origin_allowed = lambda _page: True
    crawler.creds = SimpleNamespace(
        national_id="A123456789",
        user_code="USER123456",
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
    crawler = _crawler()
    return evaluate_login_checkpoint(
        page,
        bank="fubon",
        phase=phase,
        rules=crawler.login_checkpoint_rules(),
        is_authenticated=lambda _page: False,
    )


class _PageProxy:
    def __init__(self, page, url: str):
        self._page = page
        self.url = url

    def __getattr__(self, name):
        return getattr(self._page, name)


def test_shared_api_and_terminal_first_rule_contract() -> None:
    rules = _crawler().login_checkpoint_rules()
    all_phases = tuple(CheckpointPhase)
    post = (CheckpointPhase.POST_SUBMIT, CheckpointPhase.POST_SUBMIT_SETTLE)

    assert FubonCrawler.USES_SHARED_LOGIN_CHECKPOINTS is True
    assert [rule.kind for rule in rules] == [
        CheckpointKind.OTP_REQUIRED,
        CheckpointKind.OTP_REQUIRED,
        CheckpointKind.PASSWORD_CHANGE_REQUIRED,
        CheckpointKind.PASSWORD_CHANGE_REQUIRED,
        CheckpointKind.EXPLICIT_LOGIN_ERROR,
        CheckpointKind.EXPLICIT_LOGIN_ERROR,
        CheckpointKind.EXPLICIT_LOGIN_ERROR,
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
        ".modal.show",
        "[role='dialog']",
        "#m1_userCaptcha",
    ]
    assert [rule.phases for rule in rules] == [
        all_phases,
        all_phases,
        all_phases,
        all_phases,
        post,
        post,
        post,
        all_phases,
        all_phases,
        post,
    ]
    assert all(rule.bank == "fubon" for rule in rules)
    assert all(not rule.is_clickable and rule.action_texts == () for rule in rules)


@pytest.mark.parametrize(
    ("index", "positive", "negative"),
    [
        (0, "請輸入 OTP", "請輸入圖形驗證碼"),
        (0, "一次性密碼已傳送", "一般驗證碼圖片"),
        (0, "請輸入簡訊驗證碼", "圖形驗證碼不正確"),
        (0, "請完成裝置驗證", "請確認裝置"),
        (0, "是否信任此裝置", "一般安全提醒"),
        (2, "基於安全性，您必須修改密碼", "您可以稍後修改密碼"),
        (2, "密碼已到期，請立即變更密碼", "密碼不正確"),
        (2, "密碼已過期，請立即變更密碼", "密碼不正確，圖形驗證碼已過期"),
        (4, "密碼不正確。", "密碼規則說明"),
        (4, "帳號因多次錯誤已被鎖定！", "帳號未鎖定"),
        (4, "登入失敗", "若登入失敗請確認網路"),
        (4, "驗證碼不正確。", "圖形驗證碼說明"),
        (4, "Invalid credentials!", "invalid credentials help"),
        (4, "ACCOUNT LOCKED.", "account locked troubleshooting"),
    ],
)
def test_terminal_patterns_are_bounded(index: int, positive: str, negative: str) -> None:
    pattern = _crawler().login_checkpoint_rules()[index].required_body_pattern
    assert pattern is not None
    assert pattern.fullmatch(positive)
    assert not pattern.search(negative)


@pytest.mark.parametrize(
    ("attrs", "body", "kind"),
    [
        ('class="modal show"', "請輸入 OTP", CheckpointKind.OTP_REQUIRED),
        ('role="dialog"', "請完成裝置驗證", CheckpointKind.OTP_REQUIRED),
        ('class="modal show"', "您必須修改密碼", CheckpointKind.PASSWORD_CHANGE_REQUIRED),
        ('role="dialog"', "密碼已到期，請立即變更密碼", CheckpointKind.PASSWORD_CHANGE_REQUIRED),
    ],
)
def test_real_terminal_collisions_have_zero_clicks(attrs: str, body: str, kind: CheckpointKind) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(
            f'<div {attrs}>{body}<button>登入</button></div>'
            "<script>document.body.dataset.clicks='0';document.querySelector('button').onclick=()=>document.body.dataset.clicks++</script>"
        )
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.kind is kind
        assert page.locator("body").get_attribute("data-clicks") == "0"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_real_errors_unknown_modal_and_fixed_captcha_fail_closed_without_actions() -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content('<div class="alert">驗證碼不正確。</div>')
        assert _evaluate(page, CheckpointPhase.POST_SUBMIT).kind is CheckpointKind.EXPLICIT_LOGIN_ERROR

        for body in ("若登入失敗請確認網路", "invalid credentials help"):
            page.set_content(f'<div class="alert">{body}</div>')
            assert _evaluate(page, CheckpointPhase.POST_SUBMIT).kind is CheckpointKind.UNKNOWN_BLOCKER

        page.set_content('<div class="modal show">密碼不正確，圖形驗證碼已過期<button>確定</button></div>')
        assert _evaluate(page, CheckpointPhase.POST_SUBMIT).kind is CheckpointKind.UNKNOWN_BLOCKER

        page.set_content(
            '<div class="modal show">PRIVATE-FUBON-987654<button>確定</button></div>'
            "<script>document.body.dataset.clicks='0';document.querySelector('button').onclick=()=>document.body.dataset.clicks++</script>"
        )
        outcome = _evaluate(page, CheckpointPhase.PRE_SUBMIT)
        assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
        assert "PRIVATE" not in repr(outcome)
        assert page.locator("body").get_attribute("data-clicks") == "0"

        page.set_content('<input id="m1_userCaptcha">')
        assert _evaluate(page, CheckpointPhase.PRE_SUBMIT).kind is CheckpointKind.READY_FOR_CREDENTIALS
        for phase in (CheckpointPhase.POST_SUBMIT, CheckpointPhase.POST_SUBMIT_SETTLE):
            outcome = _evaluate(page, phase)
            assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
            assert outcome.rule_name == "fubon-login-form-still-visible"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_frame_helpers_dedupe_and_fail_closed_on_ambiguity() -> None:
    crawler = _crawler()
    header = Mock(name="header", name_attr="unused")
    header.name = "frame1"
    header.url = "https://ebank.taipeifubon.com.tw/B2C/ContextFrame.faces"
    login = Mock(name="login")
    login.name = "txnFrame"
    login.url = "https://ebank.taipeifubon.com.tw/B2C/PreLogin.faces"
    page = SimpleNamespace(frames=[header, login])

    assert crawler._find_header_frame(page) is header
    assert crawler._find_login_frame(page) is login
    page.frames.append(Mock(name="ambiguous", url=login.url))
    page.frames[-1].name = "other"
    assert crawler._find_login_frame(page) is None
    page.frames = [header, Mock(name="second", url=header.url)]
    page.frames[-1].name = "other"
    assert crawler._find_header_frame(page) is None

    foreign = Mock()
    foreign.name = "txnFrame"
    foreign.url = "https://evil.example/B2C/PreLogin.faces"
    page.frames = [foreign]
    assert crawler._find_login_frame(page) is None


def test_frame_helpers_reject_srcdoc_inherited_from_foreign_parent() -> None:
    crawler = _crawler()
    main = Mock(url="https://ebank.taipeifubon.com.tw/B2C/home")
    foreign = Mock(url="https://evil.example/embedded", parent_frame=main)
    child = Mock(url="about:srcdoc", parent_frame=foreign)
    child.name = "txnFrame"
    page = SimpleNamespace(
        url="https://ebank.taipeifubon.com.tw/B2C/home",
        main_frame=main,
        frames=[main, foreign, child],
    )

    assert crawler._find_login_frame(page) is None
    for unsafe in (
        "http://ebank.taipeifubon.com.tw/B2C/PreLogin.faces",
        "https://ebank.taipeifubon.com.tw:444/B2C/PreLogin.faces",
    ):
        child.url = unsafe
        child.parent_frame = main
        assert crawler._find_login_frame(page) is None


def test_real_prepare_clicks_only_exact_header_and_anchor_once(monkeypatch) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(
            """
            <iframe name="frame1" srcdoc="<a id='header_form:header_login'>  登入  </a><script>document.body.dataset.clicks='0';document.querySelector('a').onclick=()=>document.body.dataset.clicks++</script>"></iframe>
            <iframe name="txnFrame" srcdoc="<div>一般登入</div><a> 一般登入 </a><script>document.body.dataset.clicks='0';document.querySelector('a').onclick=()=>document.body.dataset.clicks++</script>"></iframe>
            """
        )
        page.wait_for_timeout(100)
        crawler = _crawler()
        crawler._logged_in = Mock(return_value=False)
        real_wait = page.wait_for_timeout
        monkeypatch.setattr(page, "wait_for_timeout", lambda _milliseconds: real_wait(1))

        crawler.prepare_login_page(page)

        header = next(frame for frame in page.frames if frame.name == "frame1")
        login = next(frame for frame in page.frames if frame.name == "txnFrame")
        assert header.locator("body").get_attribute("data-clicks") == "1"
        assert login.locator("body").get_attribute("data-clicks") == "1"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


@pytest.mark.parametrize(
    "header_html,login_html",
    [
        ("<a id='header_form:header_login'>登入說明</a>", "<a>一般登入</a>"),
        ("<a id='header_form:header_login'>登入</a>", "<div>一般登入</div><span>一般登入</span>"),
        ("<a id='header_form:header_login'>登入</a>", "<a>一般登入</a><button>一般登入</button>"),
    ],
)
def test_real_prepare_rejects_near_miss_or_ambiguous_actions(
    monkeypatch, header_html: str, login_html: str
) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(
            f'<iframe name="frame1" srcdoc="{header_html}"></iframe>'
            f'<iframe name="txnFrame" srcdoc="{login_html}"></iframe>'
        )
        page.wait_for_timeout(100)
        crawler = _crawler()
        crawler._logged_in = Mock(return_value=False)
        real_wait = page.wait_for_timeout
        monkeypatch.setattr(page, "wait_for_timeout", lambda _milliseconds: real_wait(1))

        with pytest.raises(FubonLoginError, match="未送出登入"):
            crawler.prepare_login_page(page)
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_auth_requires_exact_host_no_prelogin_or_visible_controls_and_dashboard_pair() -> None:
    manager, browser = _launch_browser()
    try:
        real = browser.new_page()
        crawler = _crawler()
        authenticated = "登出 帳戶總覽 " + "x" * 600
        real.set_content(f"<main>{authenticated}</main>")
        assert crawler._logged_in(_PageProxy(real, "https://ebank.taipeifubon.com.tw/B2C/home"))
        assert not crawler._logged_in(_PageProxy(real, "https://evil.example/?next=ebank.taipeifubon.com.tw"))
        assert not crawler._logged_in(_PageProxy(real, "https://ebank.taipeifubon.com.tw.evil.example/B2C/home"))
        assert not crawler._logged_in(_PageProxy(real, "http://ebank.taipeifubon.com.tw/B2C/home"))
        assert not crawler._logged_in(_PageProxy(real, "https://ebank.taipeifubon.com.tw:444/B2C/home"))

        real.set_content("<main>登出 帳戶 資產 " + "x" * 600 + "</main>")
        assert crawler._logged_in(_PageProxy(real, "https://ebank.taipeifubon.com.tw/B2C/common/Index.faces"))
        real.set_content("<main>帳戶 資產 " + "x" * 600 + "</main>")
        assert not crawler._logged_in(_PageProxy(real, "https://ebank.taipeifubon.com.tw/B2C/common/Index.faces"))

        real.set_content("<main>登出 存款 轉帳 信用卡 投資 " + "x" * 600 + "</main>")
        assert not crawler._logged_in(_PageProxy(real, "https://ebank.taipeifubon.com.tw/B2C/home"))

        main = Mock()
        main.url = "https://ebank.taipeifubon.com.tw/B2C/home"
        main.name = ""
        foreign = Mock()
        foreign.url = "https://evil.example/embedded"
        foreign.name = ""
        empty = Mock()
        empty.count.return_value = 0
        main.locator.side_effect = lambda selector: (
            Mock(count=Mock(return_value=1), nth=Mock(return_value=Mock(
                inner_text=Mock(return_value="登出 " + "x" * 600)
            ))) if selector == "body" else empty
        )
        foreign.locator.side_effect = lambda selector: (
            Mock(count=Mock(return_value=1), nth=Mock(return_value=Mock(
                inner_text=Mock(return_value="帳戶 資產")
            ))) if selector == "body" else empty
        )
        page = SimpleNamespace(
            url=main.url,
            main_frame=main,
            frames=[main, foreign],
            locator=main.locator,
        )
        assert not crawler._logged_in(page)
        foreign.locator.assert_not_called()

        real.set_content(f"<main>{authenticated}</main><iframe srcdoc=\"<input type='password'>\"></iframe>")
        real.wait_for_timeout(100)
        assert not crawler._logged_in(_PageProxy(real, "https://ebank.taipeifubon.com.tw/B2C/home"))

        page = Mock()
        page.url = "https://ebank.taipeifubon.com.tw/B2C/home"
        page.frames = [Mock(url="https://ebank.taipeifubon.com.tw/B2C/PreLogin.faces")]
        page.main_frame = Mock()
        assert not crawler._logged_in(page)
        page.locator.assert_not_called()
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_auth_is_one_shot_locator_only_exception_safe_and_private(caplog) -> None:
    page = Mock()
    page.url = "https://ebank.taipeifubon.com.tw/B2C/PRIVATE"
    page.frames = []
    page.main_frame = Mock()
    page.locator.side_effect = RuntimeError("PRIVATE-DOM-987654")
    assert _crawler()._logged_in(page) is False
    page.wait_for_timeout.assert_not_called()
    page.evaluate.assert_not_called()
    assert "PRIVATE" not in caplog.text


def test_auth_bounds_each_owned_scope_body_read() -> None:
    crawler = _crawler()
    main = Mock()
    main.name = ""
    main.url = "https://ebank.taipeifubon.com.tw/B2C/home"
    controls = Mock()
    controls.count.return_value = 0
    controls.nth.side_effect = TimeoutError
    body = Mock()
    body.count.return_value = 1
    body.nth.return_value = body
    body.inner_text.return_value = "登出 帳戶總覽 " + "x" * 600
    main.locator.side_effect = lambda selector: (
        body if selector == "body" else controls
    )
    page = SimpleNamespace(
        url=main.url,
        main_frame=main,
        frames=[main],
        locator=main.locator,
    )

    assert crawler._logged_in(page)
    body.inner_text.assert_called_once_with(timeout=5000)


def test_safe_url_omits_userinfo_query_and_fragment() -> None:
    private = "https://user:pass@ebank.taipeifubon.com.tw/B2C/common/Index.faces?token=PRIVATE#secret"

    safe = _safe_url(private)

    assert safe == "https://ebank.taipeifubon.com.tw/Index.faces"
    assert "PRIVATE" not in safe
    assert "user" not in safe
    assert "pass" not in safe
    assert _safe_url("https://ebank.taipeifubon.com.tw:bad/B2C/home?token=PRIVATE") == "<invalid>"
    assert _safe_url("javascript:PRIVATE") == "<invalid>"
    assert _safe_url("https://ebank.taipeifubon.com.tw/account/1234567890") == (
        "https://ebank.taipeifubon.com.tw/<redacted-path>"
    )
    assert _safe_url("https://ebank.taipeifubon.com.tw/B2C/home;jsessionid=PRIVATE") == (
        "https://ebank.taipeifubon.com.tw/<redacted-path>"
    )
    assert _safe_url("https://ebank.taipeifubon.com.tw/account/alice-private") == (
        "https://ebank.taipeifubon.com.tw/<redacted-path>"
    )


def test_deposit_menu_telemetry_does_not_dump_dynamic_text() -> None:
    source = inspect.getsource(fubon_module.FubonCrawler.collect)

    assert "for c in deposit_audit" not in source
    assert "我的存款 click: {deposit_click}" not in source
    assert "存款交易查詢 click: {txn_click}" not in source
    assert "click result: {click_result}" not in source
    assert "帳務查詢 click: {bill_click}" not in source
    assert "帳單明細查詢 click: {billed_click}" not in source
    assert "未出帳單 click: {pending_click}" not in source


def test_ambiguous_login_frames_fail_run_before_submit_or_collect(monkeypatch, tmp_path) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(
            "<main>登出 帳戶總覽 " + "x" * 600 + "</main>"
            '<iframe name="frame1" srcdoc="<a id=\'header_form:header_login\'>登入</a>"></iframe>'
            '<iframe name="txnFrame" srcdoc="<a>一般登入</a>"></iframe>'
            '<iframe name="txnFrame" srcdoc="<a>一般登入</a>"></iframe>'
        )
        page.wait_for_timeout(100)
        crawler = _crawler()
        crawler.session_dir = tmp_path / "fubon_ambiguous_session"
        crawler.session_dir.mkdir()
        submit = Mock()
        collect = Mock()
        monkeypatch.setattr(crawler, "submit_credentials_once", submit)
        monkeypatch.setattr(crawler, "collect", collect)
        real_wait = page.wait_for_timeout
        monkeypatch.setattr(page, "wait_for_timeout", lambda _milliseconds: real_wait(1))
        monkeypatch.setattr(
            crawler,
            "_execute_browser_flow",
            lambda _url, *, page_action, **_kwargs: page_action(page),
        )

        result = crawler.run("https://ebank.taipeifubon.com.tw/B2C/common/Index.faces")

        assert crawler._find_login_frame(page) is None
        assert "FubonLoginError" in result["error"]
        submit.assert_not_called()
        collect.assert_not_called()
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def _submit_fixture():
    page = Mock()
    frame = Mock()
    frame.name = "txnFrame"
    frame.url = "https://ebank.taipeifubon.com.tw/B2C/PreLogin.faces"
    page.frames = [frame]
    page.main_frame = Mock()

    values = ["A123456789", "USER123456", "PASSWORD-PRIVATE"]
    fields = []
    passwords = Mock()
    passwords.count.return_value = 3
    for y, maxlength, value, token in (
        (30, "16", values[2], "ABCDEFGHIJ"),
        (10, "10", values[0], "KLMNOPQRST"),
        (20, "10", values[1], "UVWXYZABCD"),
    ):
        field = Mock()
        field.is_visible.return_value = True
        field.is_enabled.return_value = True
        field.get_attribute.side_effect = lambda attr, maximum=maxlength, suffix=token: {
            "maxlength": maximum,
            "id": f"m1_{suffix}",
            "name": suffix,
        }.get(attr)
        field.bounding_box.return_value = {"x": 0, "y": y, "width": 100, "height": 20}
        field.input_value.return_value = value
        field.count.return_value = 1
        field.nth.return_value = field
        fields.append(field)
    passwords.nth.side_effect = fields.__getitem__

    captcha = Mock()
    captcha.count.return_value = 1
    captcha.nth.return_value = captcha
    captcha.is_visible.return_value = True
    captcha.is_enabled.return_value = True
    captcha.get_attribute.side_effect = lambda attr: "6" if attr == "maxlength" else None
    captcha.input_value.return_value = "654321"

    submit = Mock()
    submit.count.return_value = 1
    submit.nth.return_value = submit
    submit.is_visible.return_value = True
    submit.is_enabled.return_value = True
    submit.inner_text.return_value = "登入"

    empty = Mock()
    empty.count.return_value = 0
    empty.nth.side_effect = TimeoutError
    frame.locator.side_effect = lambda selector: {
        "input[type='password']": passwords,
        "#m1_userCaptcha": captcha,
        "#btnLogin2": submit,
        ".modal.show": empty,
        "[role='dialog']": empty,
        ".error": empty,
        ".alert": empty,
        "[role='alert']": empty,
    }[selector]
    page.locator.return_value = empty
    crawler = _crawler()
    crawler._ocr_captcha = Mock(return_value="654321")
    crawler._logged_in = Mock(return_value=True)
    return crawler, page, frame, fields, passwords, captcha, submit


def test_submit_uses_exact_dynamic_field_contract_and_true_keyboard() -> None:
    crawler, page, frame, fields, _passwords, captcha, submit = _submit_fixture()
    crawler.submit_credentials_once(page)

    ordered = [fields[1], fields[2], fields[0], captcha]
    values = ["A123456789", "USER123456", "PASSWORD-PRIVATE", "654321"]
    for field, value in zip(ordered, values, strict=True):
        assert field.click.call_args_list == [call(), call(click_count=3)]
        field.press.assert_called_once_with("Backspace", timeout=5000)
        field.press_sequentially.assert_called_once_with(value, delay=80, timeout=5000)
        field.input_value.assert_called_once_with()
    page.keyboard.press.assert_not_called()
    page.keyboard.type.assert_not_called()
    crawler._ocr_captcha.assert_called_once_with(frame, max_attempts=5)
    submit.click.assert_called_once_with(timeout=8000)
    assert frame.locator.call_args_list.count(call("input[type='password']")) == 1
    page.evaluate.assert_not_called()
    page.fill.assert_not_called()


def test_post_submit_inspection_bounds_and_restores_page_timeout() -> None:
    crawler, page, _frame, _fields, _passwords, _captcha, submit = _submit_fixture()
    timeout = 180000

    def set_default_timeout(value: int) -> None:
        nonlocal timeout
        timeout = value

    def logged_in(_page) -> bool:
        assert timeout == 5000
        return True

    page.set_default_timeout.side_effect = set_default_timeout
    crawler._logged_in = Mock(side_effect=logged_in)

    crawler.submit_credentials_once(page)

    submit.click.assert_called_once_with(timeout=8000)
    assert page.set_default_timeout.call_args_list == [call(5000), call(180000)]
    assert timeout == 180000


def test_submit_ignores_hidden_password_variants_but_keeps_visible_cardinality() -> None:
    crawler, page, _frame, fields, passwords, _captcha, submit = _submit_fixture()
    hidden = Mock()
    hidden.is_visible.return_value = False
    passwords.count.return_value = 4
    passwords.nth.side_effect = [*fields, hidden]

    crawler.submit_credentials_once(page)

    hidden.get_attribute.assert_not_called()
    submit.click.assert_called_once_with(timeout=8000)


@pytest.mark.parametrize(
    "failure",
    ("frame", "count", "hidden", "disabled", "id", "name", "maxlen", "order", "length", "captcha", "submit"),
)
def test_submit_pre_click_failures_are_private_and_zero_submit(failure: str, caplog) -> None:
    crawler, page, frame, fields, _passwords, captcha, submit = _submit_fixture()
    secret = f"PRIVATE-FUBON-{failure}-987654"
    if failure == "frame":
        frame.url = "https://evil.example/B2C/PreLogin.faces"
    elif failure == "count":
        _passwords.count.return_value = 4
        decoy = Mock()
        decoy.is_visible.return_value = True
        decoy.is_enabled.return_value = True
        decoy.get_attribute.side_effect = lambda attr: {
            "maxlength": "10", "id": "m1_EFGHIJKLMN", "name": "EFGHIJKLMN"
        }.get(attr)
        decoy.bounding_box.return_value = {"x": 0, "y": 40, "width": 100, "height": 20}
        _passwords.nth.side_effect = [*fields, decoy]
    elif failure == "hidden":
        fields[1].is_visible.return_value = False
    elif failure == "disabled":
        fields[1].is_enabled.return_value = False
    elif failure == "id":
        fields[1].get_attribute.side_effect = lambda attr: {
            "maxlength": "10", "id": "m1_bad", "name": "KLMNOPQRST"
        }.get(attr)
    elif failure == "name":
        fields[1].get_attribute.side_effect = lambda attr: {
            "maxlength": "10", "id": "m1_KLMNOPQRST", "name": "MISMATCHEDX"
        }.get(attr)
    elif failure == "maxlen":
        fields[2].get_attribute.side_effect = lambda attr: "11" if attr == "maxlength" else None
    elif failure == "order":
        fields[2].bounding_box.return_value = fields[1].bounding_box.return_value
    elif failure == "length":
        fields[0].input_value.return_value = "short"
    elif failure == "captcha":
        captcha.get_attribute.side_effect = RuntimeError(secret)
    else:
        duplicate = Mock()
        submit.nth.side_effect = [submit, duplicate]

    with pytest.raises(FubonLoginError, match="未送出登入") as error:
        crawler.submit_credentials_once(page)
    assert error.value.__cause__ is None
    assert "PRIVATE" not in str(error.value)
    assert "PRIVATE" not in caplog.text
    submit.click.assert_not_called()


def test_submit_click_error_is_unknown_and_never_retried(caplog) -> None:
    crawler, page, _frame, _fields, _passwords, _captcha, submit = _submit_fixture()
    submit.click.side_effect = RuntimeError("PRIVATE-CLICK-987654")
    with pytest.raises(FubonLoginError, match="送出狀態不明；禁止自動重試") as error:
        crawler.submit_credentials_once(page)
    assert error.value.__cause__ is None
    assert "PRIVATE" not in str(error.value)
    assert "PRIVATE" not in caplog.text
    submit.click.assert_called_once_with(timeout=8000)


def test_ocr_screenshots_five_times_refreshes_four_and_never_leaks(monkeypatch, caplog, capsys) -> None:
    crawler = _crawler()
    frame = Mock()
    image = Mock()
    image.count.return_value = 1
    image.nth.return_value = image
    image.is_visible.return_value = True
    image.screenshot.return_value = b"PRIVATE-IMAGE-BYTES"
    action = Mock()
    action.is_visible.return_value = True
    action.is_enabled.return_value = True
    action.inner_text.return_value = "重新產生"
    actions = Mock()
    actions.count.return_value = 1
    actions.nth.return_value = action
    frame.locator.side_effect = lambda selector: {
        "#m1_captchaImage": image,
        "a, button": actions,
    }[selector]
    ocr = Mock(return_value="PRIVATE-OCR-TEXT")
    monkeypatch.setattr(fubon_module, "ocr_bytes", ocr)

    assert crawler._ocr_captcha(frame, max_attempts=5) is None
    assert image.screenshot.call_count == ocr.call_count == 5
    assert image.screenshot.call_args_list == [call(timeout=5000)] * 5
    assert all(
        item == call(
            b"PRIVATE-IMAGE-BYTES",
            expected_len=6,
            alnum_only=True,
            min_confidence=0.98,
        )
        for item in ocr.call_args_list
    )
    assert action.click.call_count == 4
    assert frame.wait_for_timeout.call_args_list == [call(1500)] * 4
    assert "PRIVATE" not in caplog.text + capsys.readouterr().err


@pytest.mark.parametrize("count", (0, 2))
def test_ocr_requires_unique_image_and_refresh_action(count: int, monkeypatch) -> None:
    crawler = _crawler()
    frame = Mock()
    image = Mock()
    image.count.return_value = 1 if count else 0
    image.nth.side_effect = [image] if count else TimeoutError
    image.is_visible.return_value = True
    image.screenshot.return_value = b"captcha"
    action = Mock()
    action.is_visible.return_value = True
    action.is_enabled.return_value = True
    action.inner_text.return_value = "重新產生"
    actions = Mock()
    actions.count.return_value = count
    actions.nth.side_effect = [action, Mock()] if count == 2 else TimeoutError
    frame.locator.side_effect = lambda selector: image if selector == "#m1_captchaImage" else actions
    monkeypatch.setattr(fubon_module, "ocr_bytes", Mock(return_value=None))

    assert crawler._ocr_captcha(frame, max_attempts=5) is None
    action.click.assert_not_called()


@pytest.mark.parametrize("message", ("密碼不正確", "驗證碼不正確"))
def test_delayed_explicit_error_with_mounted_fields_blocks_after_one_submit(
    monkeypatch, tmp_path, message: str
) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(
            f"""
            <style>input {{ display:block; margin:10px }}</style>
            <input id="m1_KLMNOPQRST" name="KLMNOPQRST" type="password" maxlength="10">
            <input id="m1_UVWXYZABCD" name="UVWXYZABCD" type="password" maxlength="10">
            <input id="m1_ABCDEFGHIJ" name="ABCDEFGHIJ" type="password" maxlength="16">
            <input id="m1_userCaptcha" maxlength="6">
            <a id="btnLogin2">登入</a>
            <div class="error" hidden>{message}</div>
            <script>
              document.body.dataset.submissions='0';
              btnLogin2.onclick=()=>{{
                document.body.dataset.submissions++;
                setTimeout(()=>document.querySelector('.error').hidden=false, 10);
              }};
            </script>
            """
        )
        crawler = _crawler()
        crawler._ocr_captcha = Mock(return_value="654321")
        crawler.prepare_login_page = lambda _page: None
        crawler.is_authenticated = lambda _page: False
        crawler._logged_in = lambda _page: False
        crawler._find_login_frame = lambda _page: page
        collect = Mock()
        crawler.collect = collect
        crawler.session_dir = tmp_path / "fubon_session"
        crawler.session_dir.mkdir()
        real_wait = page.wait_for_timeout
        monkeypatch.setattr(page, "wait_for_timeout", lambda _milliseconds: real_wait(5))
        monkeypatch.setattr(
            crawler,
            "_execute_browser_flow",
            lambda _url, *, page_action, **_kwargs: page_action(page),
        )

        result = crawler.run("https://ebank.taipeifubon.com.tw/B2C/common/Index.faces")

        assert "LoginCheckpointBlocked" in result["error"]
        assert "explicit_login_error" in result["error"]
        assert page.locator("body").get_attribute("data-submissions") == "1"
        collect.assert_not_called()
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_login_is_thin_and_prepare_short_circuits_authenticated(monkeypatch) -> None:
    crawler = _crawler()
    page = Mock()
    shared = Mock(return_value=True)
    monkeypatch.setattr(crawler, "_shared_login", shared)
    assert crawler.login(page)
    shared.assert_called_once_with(page)

    crawler._logged_in = Mock(return_value=True)
    crawler.prepare_login_page(page)
    assert page.mock_calls == [
        call.set_default_timeout(5000),
        call.wait_for_timeout(12000),
        call.set_default_timeout(180000),
    ]


def test_legacy_login_debug_synthetic_actions_and_raw_ocr_are_absent() -> None:
    source = inspect.getsource(fubon_module)
    login_region = source[: source.index("    def collect(")]
    assert ".count()" not in login_region
    for forbidden in (
        "base64",
        "page.screenshot",
        "_login_snapshot",
        ".evaluate(",
        ".fill(",
        "dispatchEvent",
        "captcha={captcha}",
        "OCR 成功",
        "讀到 {text",
        "captchaImage?timestamp",
    ):
        assert forbidden not in login_region


def test_collect_and_following_helpers_keep_protected_ast_contract() -> None:
    collect_source = inspect.getsource(FubonCrawler.collect)
    assert collect_source.count(".evaluate(") == 1
    assert "timeout=5000" in collect_source
    assert collect_source.count("bounded_evaluate(") > 10

    tree = ast.parse(Path(fubon_module.__file__).read_text())
    crawler = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "FubonCrawler")
    start = next(i for i, node in enumerate(crawler.body) if isinstance(node, ast.FunctionDef) and node.name == "collect")
    payload = "\n".join(ast.dump(node, include_attributes=False) for node in crawler.body[start:])
    assert hashlib.sha256(payload.encode()).hexdigest() == "b3bf4fdfeff60ff1c1cab40ad919bfb2d25d3b3cf36a8aed8640dec2e375a227"
