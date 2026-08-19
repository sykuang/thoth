from __future__ import annotations

import ast
import hashlib
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

import backend.banks.dbs as dbs_module
from backend.banks.dbs import DbsCrawler, DbsLoginError, LOGIN_PATH_HINT
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointPhase,
    LoginCheckpointBlocked,
    evaluate_login_checkpoint,
)


def _crawler() -> DbsCrawler:
    crawler = object.__new__(DbsCrawler)
    crawler.name = "dbs"
    crawler.creds = SimpleNamespace(
        username="USER-PRIVATE",
        password="PASSWORD-PRIVATE",
    )
    return crawler


def test_shared_api_and_terminal_first_rule_contract() -> None:
    rules = _crawler().login_checkpoint_rules()
    all_phases = tuple(CheckpointPhase)
    post = (CheckpointPhase.POST_SUBMIT, CheckpointPhase.POST_SUBMIT_SETTLE)

    assert DbsCrawler.USES_SHARED_LOGIN_CHECKPOINTS is True
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
        "#username",
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
    assert all(rule.bank == "dbs" for rule in rules)
    assert all(rule.action_texts == () for rule in rules)
    assert all(not rule.is_clickable for rule in rules)


@pytest.mark.parametrize(
    ("index", "positive", "negative"),
    [
        (0, "請輸入 OTP", "請輸入圖形驗證碼"),
        (0, "一次性密碼已傳送", "一般驗證碼圖片"),
        (0, "請輸入簡訊驗證碼", "圖形驗證碼錯誤"),
        (0, "請完成裝置驗證", "一般登入提醒"),
        (0, "是否信任此裝置", "請確認裝置"),
        (0, "新裝置登入驗證", "新功能登入"),
        (2, "基於安全性，您必須變更密碼", "您可以稍後變更密碼"),
        (2, "密碼已到期，請立即修改密碼", "密碼不正確"),
        (4, "帳號不存在", "帳戶不存在"),
        (4, "密碼不正確", "密碼規則說明"),
        (4, "帳號因多次錯誤已被鎖定", "帳號" + "x" * 41 + "鎖定"),
        (4, "登入失敗。", "若登入失敗，請確認網路連線後再試"),
        (4, "Invalid Credentials!", "invalid credential policy"),
        (4, "ACCOUNT LOCKED.", "Account locked troubleshooting guide"),
    ],
)
def test_terminal_patterns_are_bounded_positive_and_negative(
    index: int,
    positive: str,
    negative: str,
) -> None:
    pattern = _crawler().login_checkpoint_rules()[index].required_body_pattern
    assert pattern is not None
    assert pattern.fullmatch(positive)
    assert not pattern.search(negative)


def test_legacy_login_debug_and_synthetic_actions_are_absent() -> None:
    source = inspect.getsource(dbs_module)
    login_region = source[: source.index("    def collect(")]

    for forbidden in (
        "_login_snapshot",
        "page.screenshot",
        "page.evaluate",
        "document.getElementById('loginbutton')",
        "keyboard type 後",
        "送出後 ~20s",
        "起始 url=",
        "final_url",
    ):
        assert forbidden not in login_region


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
        bank="dbs",
        phase=phase,
        rules=crawler.login_checkpoint_rules(),
        is_authenticated=lambda _page: False,
    )


@pytest.mark.parametrize(
    ("attrs", "body", "kind"),
    [
        ('class="modal show"', "請輸入 OTP", CheckpointKind.OTP_REQUIRED),
        ('role="dialog"', "新裝置登入需要裝置驗證", CheckpointKind.OTP_REQUIRED),
        ('class="modal show"', "您必須變更密碼", CheckpointKind.PASSWORD_CHANGE_REQUIRED),
        ('role="dialog"', "密碼已到期，請立即修改密碼", CheckpointKind.PASSWORD_CHANGE_REQUIRED),
    ],
)
def test_real_terminal_modals_win_without_clicks(attrs: str, body: str, kind: CheckpointKind) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(
            f'<div {attrs}>{body}<button>確定</button></div>'
            "<script>document.body.dataset.clicks='0';document.querySelector('button').onclick=()=>document.body.dataset.clicks++</script>"
        )
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.kind is kind
        assert page.locator("body").get_attribute("data-clicks") == "0"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_real_errors_unknown_private_and_generic_captcha_fail_closed() -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content('<div class="alert">密碼不正確</div>')
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.kind is CheckpointKind.EXPLICIT_LOGIN_ERROR

        for body in (
            "若登入失敗，請確認網路連線後再試",
            "登入失敗原因說明",
            "Account locked troubleshooting guide",
        ):
            page.set_content(f'<div class="alert">{body}</div>')
            outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
            assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER

        for attrs, body in (
            ('class="modal show"', "PRIVATE-DBS-MODAL-987654"),
            ('role="dialog"', "一般安全提醒"),
            ('class="modal show"', "請輸入圖形驗證碼"),
        ):
            page.set_content(
                f'<div {attrs}>{body}<button>確定</button></div>'
                "<script>document.body.dataset.clicks='0';document.querySelector('button').onclick=()=>document.body.dataset.clicks++</script>"
            )
            outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
            assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
            assert body not in repr(outcome)
            assert page.locator("body").get_attribute("data-clicks") == "0"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_real_pre_submit_is_ready_but_post_submit_form_is_unknown() -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content('<input id="username">')
        assert _evaluate(page, CheckpointPhase.PRE_SUBMIT).kind is CheckpointKind.READY_FOR_CREDENTIALS
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
        assert outcome.rule_name == "dbs-login-form-still-visible"

        page.set_content('<div class="modal show">PRIVATE<button>確定</button></div>')
        assert _evaluate(page, CheckpointPhase.PRE_SUBMIT).kind is CheckpointKind.UNKNOWN_BLOCKER
    finally:
        browser.close()
        manager.__exit__(None, None, None)


class _PageProxy:
    def __init__(self, page, url: str):
        self._page = page
        self.url = url

    def __getattr__(self, name):
        return getattr(self._page, name)


def test_auth_requires_exact_host_path_identity_and_rejects_public_or_child_forms() -> None:
    manager, browser = _launch_browser()
    try:
        real = browser.new_page()
        crawler = _crawler()
        authenticated = "登出 帳戶總覽 " + "x" * 300
        real.set_content(f"<main>{authenticated}</main>")
        assert crawler._logged_in(_PageProxy(real, "https://internet-banking.dbs.com.tw/digitw/home"))
        assert crawler._logged_in(_PageProxy(real, "https://internet-banking.dbs.com.tw/digitw/home?x=1"))
        real.set_content("<main>lOgOuT 資產總覽 " + "x" * 300 + "</main>")
        assert crawler._logged_in(_PageProxy(real, "https://internet-banking.dbs.com.tw/digitw/home"))
        assert not crawler._logged_in(_PageProxy(real, "https://evil.example/?next=internet-banking.dbs.com.tw"))
        assert not crawler._logged_in(_PageProxy(real, "https://internet-banking.dbs.com.tw.evil.example/digitw/home"))
        assert not crawler._logged_in(_PageProxy(real, f"https://internet-banking.dbs.com.tw{LOGIN_PATH_HINT}"))
        assert not crawler._logged_in(_PageProxy(real, f"https://internet-banking.dbs.com.tw{LOGIN_PATH_HINT}/"))
        assert not crawler._logged_in(_PageProxy(real, f"https://internet-banking.dbs.com.tw{LOGIN_PATH_HINT}/challenge"))

        real.set_content("<main>存款 轉帳 信用卡 投資 " + "x" * 300 + "</main>")
        assert not crawler._logged_in(_PageProxy(real, "https://internet-banking.dbs.com.tw/digitw/home"))
        real.set_content("<main>LoggedOut 帳戶總覽 " + "x" * 300 + "</main>")
        assert not crawler._logged_in(_PageProxy(real, "https://internet-banking.dbs.com.tw/digitw/home"))
        real.set_content("<main>登出 存款 轉帳 信用卡 " + "x" * 300 + "</main>")
        assert not crawler._logged_in(_PageProxy(real, "https://internet-banking.dbs.com.tw/digitw/home"))

        real.set_content(
            f"<main>{authenticated}</main>"
            '<iframe srcdoc="<input id=\'password\'>"></iframe>'
        )
        real.wait_for_timeout(100)
        assert not crawler._logged_in(_PageProxy(real, "https://internet-banking.dbs.com.tw/digitw/home"))
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_auth_is_one_shot_exception_safe_and_fieldless(caplog) -> None:
    page = Mock()
    page.url = "https://internet-banking.dbs.com.tw/PRIVATE-PATH"
    page.frames = []
    page.main_frame = Mock()
    page.locator.side_effect = RuntimeError("PRIVATE-DOM-987654")

    assert _crawler()._logged_in(page) is False
    page.wait_for_timeout.assert_not_called()
    page.evaluate.assert_not_called()
    assert "PRIVATE" not in caplog.text


def _empty_locator() -> Mock:
    locator = Mock()
    locator.count.return_value = 0
    return locator


def _submit_fixture(*, response_selector: str | None = ".alert"):
    page = Mock()
    page.frames = []
    page.main_frame = page
    values = {"#username": "USER-PRIVATE", "#password": "PASSWORD-PRIVATE"}
    fields = {selector: Mock() for selector in values}
    for selector, locator in fields.items():
        locator.count.return_value = 1
        locator.nth.return_value = locator
        locator.is_visible.return_value = True
        locator.is_enabled.return_value = True
        locator.input_value.return_value = values[selector]

    submit = Mock()
    submit.count.return_value = 1
    submit.nth.return_value = submit
    submit.is_visible.return_value = True
    submit.is_enabled.return_value = True
    submit.inner_text.return_value = "登入"

    responses = {}
    for selector in (".modal.show", "[role='dialog']", ".error", ".alert", "[role='alert']"):
        response = _empty_locator()
        if selector == response_selector:
            item = Mock()
            item.is_visible.return_value = True
            response.count.return_value = 1
            response.nth.return_value = item
        responses[selector] = response

    page.locator.side_effect = lambda selector: {
        **fields,
        "#loginbutton": submit,
        **responses,
    }[selector]
    crawler = _crawler()
    crawler._logged_in = Mock(return_value=False)
    return crawler, page, fields, submit, responses


def test_submit_uses_exact_true_keyboard_sequence_lengths_and_one_native_click() -> None:
    crawler, page, fields, submit, _responses = _submit_fixture()

    crawler.submit_credentials_once(page)

    assert page.wait_for_selector.call_args_list == [
        call("#username", state="visible", timeout=15000),
        call("#password", state="visible", timeout=5000),
        call("#loginbutton", state="visible", timeout=5000),
    ]
    for locator in fields.values():
        assert locator.method_calls == [
            call.count(),
            call.nth(0),
            call.is_visible(),
            call.is_enabled(),
            call.click(),
            call.click(click_count=3),
            call.input_value(),
        ]
    assert page.keyboard.press.call_args_list == [call("Backspace"), call("Backspace")]
    assert page.keyboard.type.call_args_list == [
        call("USER-PRIVATE", delay=80),
        call("PASSWORD-PRIVATE", delay=80),
    ]
    assert page.wait_for_timeout.call_args_list == [
        call(200), call(100), call(100), call(300),
        call(200), call(100), call(100), call(500),
        call(3000), call(1000),
    ]
    submit.click.assert_called_once_with(timeout=8000)
    page.click.assert_not_called()
    page.evaluate.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    ["wait", "count", "hidden", "disabled", "type", "input-value", "length"],
)
def test_field_failures_are_fieldless_and_zero_submit(failure: str, caplog) -> None:
    crawler, page, fields, submit, _responses = _submit_fixture()
    secret = f"PRIVATE-DBS-FIELD-{failure}-987654"
    if failure == "wait":
        page.wait_for_selector.side_effect = RuntimeError(secret)
    elif failure == "count":
        fields["#username"].count.return_value = 2
    elif failure == "hidden":
        fields["#password"].is_visible.return_value = False
    elif failure == "disabled":
        fields["#password"].is_enabled.return_value = False
    elif failure == "type":
        page.keyboard.type.side_effect = RuntimeError(secret)
    elif failure == "input-value":
        fields["#username"].input_value.side_effect = RuntimeError(secret)
    else:
        fields["#password"].input_value.return_value = "short"

    with pytest.raises(DbsLoginError, match="未送出") as error:
        crawler.submit_credentials_once(page)

    assert error.value.__cause__ is None
    assert secret not in str(error.value)
    assert "PRIVATE" not in str(error.value)
    assert secret not in caplog.text
    submit.click.assert_not_called()
    page.evaluate.assert_not_called()


@pytest.mark.parametrize(
    ("count", "visible", "enabled", "text"),
    [
        (0, True, True, "登入"),
        (2, True, True, "登入"),
        (1, False, True, "登入"),
        (1, True, False, "登入"),
        (1, True, True, "立即登入"),
        (1, True, True, "登入 Login"),
    ],
)
def test_submit_button_must_be_unique_visible_enabled_and_exact_text(
    count: int,
    visible: bool,
    enabled: bool,
    text: str,
) -> None:
    crawler, page, _fields, submit, _responses = _submit_fixture()
    submit.count.return_value = count
    submit.is_visible.return_value = visible
    submit.is_enabled.return_value = enabled
    submit.inner_text.return_value = text

    with pytest.raises(DbsLoginError, match="未送出"):
        crawler.submit_credentials_once(page)

    submit.click.assert_not_called()


def test_submit_accepts_normalized_exact_login_text() -> None:
    crawler, page, _fields, submit, _responses = _submit_fixture()
    submit.inner_text.return_value = " \n 登入 \t "
    crawler.submit_credentials_once(page)
    submit.click.assert_called_once_with(timeout=8000)


def test_submit_button_inspection_exception_is_fieldless_and_zero_click(caplog) -> None:
    crawler, page, _fields, submit, _responses = _submit_fixture()
    secret = "PRIVATE-DBS-BUTTON-987654"
    submit.inner_text.side_effect = RuntimeError(secret)

    with pytest.raises(DbsLoginError, match="未送出") as error:
        crawler.submit_credentials_once(page)

    assert error.value.__cause__ is None
    assert secret not in str(error.value)
    assert secret not in caplog.text
    submit.click.assert_not_called()


def test_submit_click_exception_is_fieldless_unknown_and_attempted_once(caplog) -> None:
    crawler, page, _fields, submit, _responses = _submit_fixture()
    secret = "PRIVATE-DBS-CLICK-987654"
    submit.click.side_effect = RuntimeError(secret)

    with pytest.raises(DbsLoginError, match="送出狀態不明；禁止自動重試") as error:
        crawler.submit_credentials_once(page)

    assert error.value.__cause__ is None
    assert secret not in str(error.value)
    assert secret not in caplog.text
    submit.click.assert_called_once_with(timeout=8000)


@pytest.mark.parametrize("failure", ["initial-wait", "poll-wait", "auth", "locator"])
def test_post_click_exceptions_are_fieldless_unknown_and_never_retry(
    failure: str,
    caplog,
) -> None:
    crawler, page, _fields, submit, responses = _submit_fixture()
    secret = f"PRIVATE-DBS-POST-{failure}-987654"
    if failure == "initial-wait":
        page.wait_for_timeout.side_effect = lambda value: (_ for _ in ()).throw(RuntimeError(secret)) if value == 3000 else None
    elif failure == "poll-wait":
        page.wait_for_timeout.side_effect = lambda value: (_ for _ in ()).throw(RuntimeError(secret)) if value == 1000 else None
    elif failure == "auth":
        crawler._logged_in.side_effect = RuntimeError(secret)
    else:
        responses[".modal.show"].count.side_effect = RuntimeError(secret)

    with pytest.raises(DbsLoginError, match="狀態無法安全確認；禁止自動重試") as error:
        crawler.submit_credentials_once(page)

    assert error.value.__cause__ is None
    assert secret not in str(error.value)
    assert secret not in caplog.text
    submit.click.assert_called_once_with(timeout=8000)


def test_post_click_timeout_returns_only_after_twenty_polls() -> None:
    crawler, page, _fields, submit, _responses = _submit_fixture(response_selector=None)

    crawler.submit_credentials_once(page)

    assert page.wait_for_timeout.call_args_list[-21:] == [call(3000)] + [call(1000)] * 20
    assert crawler._logged_in.call_count == 20
    submit.click.assert_called_once_with(timeout=8000)


def test_delayed_alert_while_login_fields_remain_blocks_without_resubmit_or_collect(monkeypatch) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(
            """
            <input id="username"><input id="password"><button id="loginbutton">登入</button>
            <div class="alert" hidden>密碼不正確</div>
            <script>
              document.body.dataset.submissions = '0';
              loginbutton.onclick = () => {
                document.body.dataset.submissions++;
                setTimeout(() => document.querySelector('.alert').hidden = false, 20);
              };
            </script>
            """
        )
        crawler = _crawler()
        collect = Mock()
        monkeypatch.setattr(crawler, "prepare_login_page", lambda _page: None)
        monkeypatch.setattr(crawler, "is_authenticated", lambda _page: False)
        monkeypatch.setattr(crawler, "_logged_in", lambda _page: False)
        monkeypatch.setattr(crawler, "collect", collect)
        original_wait = page.wait_for_timeout
        monkeypatch.setattr(
            page,
            "wait_for_timeout",
            lambda milliseconds: original_wait(30 if milliseconds >= 1000 else milliseconds),
        )

        with pytest.raises(LoginCheckpointBlocked) as error:
            crawler._shared_login(page)

        assert error.value.outcome.kind is CheckpointKind.EXPLICIT_LOGIN_ERROR
        assert page.locator("body").get_attribute("data-submissions") == "1"
        collect.assert_not_called()
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_login_prepare_and_authentication_are_direct_adapters(monkeypatch) -> None:
    crawler = _crawler()
    page = Mock()
    shared = Mock(return_value=True)
    authenticated = Mock(return_value=True)
    monkeypatch.setattr(crawler, "_shared_login", shared)
    monkeypatch.setattr(crawler, "_logged_in", authenticated)

    assert crawler.login(page)
    shared.assert_called_once_with(page)
    crawler.prepare_login_page(page)
    assert page.mock_calls == [call.wait_for_timeout(8000)]
    assert crawler.is_authenticated(page)
    authenticated.assert_called_once_with(page)


def test_collect_and_following_helpers_keep_protected_ast_contract() -> None:
    tree = ast.parse(Path(dbs_module.__file__).read_text())
    crawler = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "DbsCrawler"
    )
    start = next(
        index for index, node in enumerate(crawler.body)
        if isinstance(node, ast.FunctionDef) and node.name == "collect"
    )
    payload = "\n".join(
        ast.dump(node, include_attributes=False) for node in crawler.body[start:]
    )
    assert hashlib.sha256(payload.encode()).hexdigest() == "2d30dc5d76db3a54f88aabadcf27fadd888459e0ee085d0101eeb504b78e22a2"
