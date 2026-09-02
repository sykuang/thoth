from __future__ import annotations

import inspect
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

import backend.banks.taishin as taishin_module
from backend.banks.taishin import TaishinCrawler, TaishinLoginError
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointOutcome,
    CheckpointPhase,
    LoginCheckpointBlocked,
    evaluate_login_checkpoint,
)


def _crawler() -> TaishinCrawler:
    crawler = object.__new__(TaishinCrawler)
    crawler.name = "taishin"
    crawler._credential_origin_allowed = lambda _page: True
    return crawler


def test_taishin_shared_login_api_and_ordered_rules() -> None:
    rules = _crawler().login_checkpoint_rules()

    assert TaishinCrawler.USES_SHARED_LOGIN_CHECKPOINTS is True
    assert [rule.name for rule in rules] == [
        "taishin-otp-required-modal",
        "taishin-otp-required-dialog",
        "taishin-mandatory-password-modal",
        "taishin-mandatory-password-dialog",
        "taishin-pre-duplicate-modal",
        "taishin-pre-duplicate-dialog",
        "taishin-post-protocol-modal",
        "taishin-post-protocol-dialog",
        "taishin-post-notice-modal",
        "taishin-post-notice-dialog",
        "taishin-unknown-modal",
        "taishin-unknown-dialog",
        "taishin-login-form-still-visible",
    ]
    assert [rule.kind for rule in rules] == [
        CheckpointKind.OTP_REQUIRED,
        CheckpointKind.OTP_REQUIRED,
        CheckpointKind.PASSWORD_CHANGE_REQUIRED,
        CheckpointKind.PASSWORD_CHANGE_REQUIRED,
        CheckpointKind.DUPLICATE_SESSION,
        CheckpointKind.DUPLICATE_SESSION,
        CheckpointKind.PROTOCOL_RESUBMIT,
        CheckpointKind.PROTOCOL_RESUBMIT,
        CheckpointKind.DISMISSIBLE_NOTICE,
        CheckpointKind.DISMISSIBLE_NOTICE,
        CheckpointKind.UNKNOWN_BLOCKER,
        CheckpointKind.UNKNOWN_BLOCKER,
        CheckpointKind.UNKNOWN_BLOCKER,
    ]
    assert [rule.container_selector for rule in rules] == [
        ".modal.show",
        "[role='dialog']",
        ".modal.show",
        "[role='dialog']",
        ".modal.show",
        "[role='dialog']",
        ".modal.show",
        "[role='dialog']",
        ".modal.show",
        "[role='dialog']",
        ".modal.show",
        "[role='dialog']",
        "input[placeholder='身分證字號']",
    ]
    assert [rule.phases for rule in rules] == [
        tuple(CheckpointPhase),
        tuple(CheckpointPhase),
        tuple(CheckpointPhase),
        tuple(CheckpointPhase),
        (CheckpointPhase.PRE_SUBMIT,),
        (CheckpointPhase.PRE_SUBMIT,),
        (CheckpointPhase.POST_SUBMIT,),
        (CheckpointPhase.POST_SUBMIT,),
        (CheckpointPhase.POST_SUBMIT, CheckpointPhase.POST_SUBMIT_SETTLE),
        (CheckpointPhase.POST_SUBMIT, CheckpointPhase.POST_SUBMIT_SETTLE),
        tuple(CheckpointPhase),
        tuple(CheckpointPhase),
        (CheckpointPhase.POST_SUBMIT, CheckpointPhase.POST_SUBMIT_SETTLE),
    ]
    for rule in rules[4:8]:
        assert rule.action_texts == ("重新登入", "重新登錄")
        assert rule.max_actions == 1
        assert rule.required_body_pattern is not None
        assert rule.required_body_pattern.search("上次未正常登出\n請重新登入")
        assert rule.required_body_pattern.search("未正常登出：重新登錄")
        assert not rule.required_body_pattern.search("重新登入\n上次未正常登出")
        assert not rule.required_body_pattern.search("上次正常登出\n重新登入")
    for rule in rules[8:10]:
        assert rule.action_texts == ("我知道了",)
        assert rule.required_body_pattern is not None
        assert rule.required_body_pattern.fullmatch("系統斷信\n訊息通知\n我知道了")
        assert not rule.required_body_pattern.search("一般通知\n我知道了")
    assert all(rule.bank == "taishin" for rule in rules)


def test_taishin_legacy_login_and_popup_surgery_are_absent() -> None:
    source = inspect.getsource(TaishinCrawler)

    for forbidden in (
        "handle_dup_login_modal",
        "_submit_login_once",
        "_close_popups",
        "_dump_login_failed",
        "modal-backdrop",
        "login_FAILED.png",
        "captcha={captcha}",
        "OCR 成功: {text}",
        "讀到 {text",
    ):
        assert forbidden not in source
    assert not re.search(r"def collect[\s\S]*01_after_close_popups", source)


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
        bank="taishin",
        phase=phase,
        rules=_crawler().login_checkpoint_rules(),
        is_authenticated=lambda _page: False,
    )


@pytest.mark.parametrize("selector", ('.modal.show', "[role='dialog']"))
@pytest.mark.parametrize(
    ("phase", "expected"),
    (
        (CheckpointPhase.PRE_SUBMIT, CheckpointKind.DUPLICATE_SESSION),
        (CheckpointPhase.POST_SUBMIT, CheckpointKind.PROTOCOL_RESUBMIT),
    ),
)
def test_real_duplicate_checkpoint_clicks_exact_action_once(
    selector: str,
    phase: CheckpointPhase,
    expected: CheckpointKind,
) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        attrs = 'class="modal show"' if selector == ".modal.show" else 'role="dialog"'
        page.set_content(
            f"""
            <div {attrs}>上次未正常登出<br><button>重新登入</button></div>
            <script>
              document.body.dataset.clicks = '0';
              document.querySelector('button').onclick = event => {{
                document.body.dataset.clicks++;
                event.target.closest('div').hidden = true;
              }};
            </script>
            """
        )

        outcome = _evaluate(page, phase)

        assert outcome.kind is expected
        assert outcome.action_label == "重新登入"
        assert page.locator("body").get_attribute("data-clicks") == "1"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


@pytest.mark.parametrize("selector", ('.modal.show', "[role='dialog']"))
def test_real_known_information_notice_clicks_only_scoped_exact_action(
    selector: str,
) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        attrs = 'class="modal show"' if selector == ".modal.show" else 'role="dialog"'
        page.set_content(
            f"""
            <button id="outside">我知道了</button>
            <div {attrs} id="notice">系統斷信<br>訊息通知<br><button>我知道了</button></div>
            <script>
              document.body.dataset.inside = '0';
              document.body.dataset.outside = '0';
              outside.onclick = () => document.body.dataset.outside++;
              notice.querySelector('button').onclick = () => {{
                document.body.dataset.inside++;
                notice.hidden = true;
              }};
            </script>
            """
        )

        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT_SETTLE)

        assert outcome.kind is CheckpointKind.DISMISSIBLE_NOTICE
        assert outcome.action_label == "我知道了"
        assert page.locator("body").get_attribute("data-inside") == "1"
        assert page.locator("body").get_attribute("data-outside") == "0"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


@pytest.mark.parametrize(
    ("phase", "body", "expected"),
    (
        (
            CheckpointPhase.POST_SUBMIT,
            "上次未正常登出，安全驗證需輸入OTP後才能重新登入<button>重新登入</button>",
            CheckpointKind.OTP_REQUIRED,
        ),
        (
            CheckpointPhase.PRE_SUBMIT,
            "上次未正常登出，請立即修改密碼後重新登入<button>重新登入</button>",
            CheckpointKind.PASSWORD_CHANGE_REQUIRED,
        ),
        (
            CheckpointPhase.POST_SUBMIT_SETTLE,
            "訊息通知：基於安全性請立即修改密碼，否則無法繼續<button>我知道了</button>",
            CheckpointKind.PASSWORD_CHANGE_REQUIRED,
        ),
        (
            CheckpointPhase.POST_SUBMIT,
            "系統斷信，請輸入OTP<button>我知道了</button>",
            CheckpointKind.OTP_REQUIRED,
        ),
    ),
)
def test_terminal_markers_precede_all_clickable_rules(
    phase: CheckpointPhase,
    body: str,
    expected: CheckpointKind,
) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(
            f"""
            <div class="modal show">{body}</div>
            <script>
              document.body.dataset.clicks = '0';
              document.querySelector('button').onclick = () => document.body.dataset.clicks++;
            </script>
            """
        )

        outcome = _evaluate(page, phase)

        assert outcome.kind is expected
        assert page.locator("body").get_attribute("data-clicks") == "0"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


@pytest.mark.parametrize(
    "body",
    (
        "上次未正常登出<br><button>確定</button>",
        "上次未正常登出<br><button>確認</button>",
        "<button>重新登入</button><br>上次未正常登出",
        "PRIVATE-UNKNOWN-987654<button>重新登入</button>",
    ),
)
def test_real_unknown_or_near_miss_modal_never_clicks(body: str) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(
            f"""
            <div class="modal show">{body}</div>
            <script>
              document.body.dataset.clicks = '0';
              document.querySelector('button').onclick = () => document.body.dataset.clicks++;
            </script>
            """
        )

        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)

        assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
        assert page.locator("body").get_attribute("data-clicks") == "0"
        assert "PRIVATE-UNKNOWN-987654" not in repr(outcome)
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_real_hidden_checkpoint_is_ignored_and_friction_is_not_authenticated() -> None:
    manager, browser = _launch_browser()
    try:
        real_page = browser.new_page()
        real_page.set_content(
            '<div class="modal show" hidden>上次未正常登出<button>重新登入</button></div>'
            f"<main>{'我知道了 系統斷信 3個月後提醒 前往修改 訊息通知 大陸身份 外國身份 清除 虛擬鍵盤 ' * 20}</main>"
        )

        class PageProxy:
            url = "https://my.taishinbank.com.tw/TIBNetBank/home"

            def __getattr__(self, name):
                return getattr(real_page, name)

        page = PageProxy()
        assert _evaluate(page, CheckpointPhase.PRE_SUBMIT).kind is CheckpointKind.READY_FOR_CREDENTIALS
        assert _crawler()._logged_in(page) is False
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_real_visible_login_form_is_unknown_after_submit_and_settle() -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content('<input placeholder="身分證字號">')

        for phase in (
            CheckpointPhase.POST_SUBMIT,
            CheckpointPhase.POST_SUBMIT_SETTLE,
        ):
            outcome = _evaluate(page, phase)
            assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
            assert outcome.rule_name == "taishin-login-form-still-visible"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_real_child_frame_login_form_prevents_auth_shortcut() -> None:
    manager, browser = _launch_browser()
    try:
        real_page = browser.new_page()
        real_page.set_content(
            f"<main>帳戶總覽 我的資產 {'x' * 600}</main>"
            '<iframe srcdoc="<input placeholder=\'身分證字號\'>"></iframe>'
        )
        real_page.wait_for_timeout(100)

        class PageProxy:
            url = "https://my.taishinbank.com.tw/TIBNetBank/home"

            def __getattr__(self, name):
                return getattr(real_page, name)

        page = PageProxy()
        crawler = _crawler()
        assert crawler._logged_in(page) is False

        outcome = evaluate_login_checkpoint(
            page,
            bank="taishin",
            phase=CheckpointPhase.POST_SUBMIT,
            rules=crawler.login_checkpoint_rules(),
            is_authenticated=crawler._logged_in,
            is_scope_owned=lambda frame: crawler._frame_origin_allowed(page, frame),
        )
        assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
        assert outcome.rule_name == "taishin-login-form-still-visible"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_shared_login_second_protocol_popup_is_shadowed_before_third_submit(
    monkeypatch,
) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(
            """
            <button id="credentials">submit</button>
            <div class="modal show" hidden>上次未正常登出<button>重新登入</button></div>
            <script>
              document.body.dataset.submissions = '0';
              document.body.dataset.protocolClicks = '0';
              const modal = document.querySelector('.modal');
              credentials.onclick = () => {
                document.body.dataset.submissions++;
                modal.hidden = false;
              };
              modal.querySelector('button').onclick = () => {
                document.body.dataset.protocolClicks++;
                modal.hidden = true;
              };
            </script>
            """
        )
        crawler = _crawler()
        collect = Mock()
        monkeypatch.setattr(crawler, "prepare_login_page", lambda _page: None)
        monkeypatch.setattr(crawler, "is_authenticated", lambda _page: False)
        monkeypatch.setattr(crawler, "submit_credentials_once", lambda p: p.locator("#credentials").click())
        monkeypatch.setattr(crawler, "collect", collect)

        with pytest.raises(LoginCheckpointBlocked) as error:
            crawler._shared_login(page)

        assert error.value.outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
        assert page.locator("body").get_attribute("data-submissions") == "2"
        assert page.locator("body").get_attribute("data-protocol-clicks") == "1"
        collect.assert_not_called()
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_shared_login_preexisting_duplicate_is_not_a_protocol_resubmit(monkeypatch) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(
            """
            <div class="modal show">上次未正常登出<button>重新登錄</button></div>
            <script>
              document.body.dataset.submissions = '0';
              document.body.dataset.duplicateClicks = '0';
              document.querySelector('.modal button').onclick = event => {
                document.body.dataset.duplicateClicks++;
                event.target.closest('.modal').hidden = true;
              };
            </script>
            """
        )
        crawler = _crawler()
        submissions = 0

        def submit(_page) -> None:
            nonlocal submissions
            submissions += 1

        monkeypatch.setattr(crawler, "prepare_login_page", lambda _page: None)
        monkeypatch.setattr(crawler, "is_authenticated", lambda _page: submissions == 1)
        monkeypatch.setattr(crawler, "submit_credentials_once", submit)

        assert crawler._shared_login(page) is True
        assert submissions == 1
        assert page.locator("body").get_attribute("data-duplicate-clicks") == "1"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_login_prepare_and_authentication_are_thin_one_shot_adapters(monkeypatch) -> None:
    page = Mock()
    crawler = _crawler()
    shared = Mock(return_value=True)
    authenticated = Mock(return_value=True)
    monkeypatch.setattr(crawler, "_shared_login", shared)
    monkeypatch.setattr(crawler, "_logged_in", authenticated)

    assert crawler.login(page)
    shared.assert_called_once_with(page)
    crawler.prepare_login_page(page)
    assert page.mock_calls == [call.wait_for_timeout(10000)]
    assert crawler.is_authenticated(page)
    authenticated.assert_called_once_with(page)


def test_logged_in_is_one_shot_and_requires_exact_origin_logout_and_dashboard_identity() -> None:
    page = Mock()
    page.url = "https://my.taishinbank.com.tw/TIBNetBank/home"
    page.frames = []
    page.main_frame = Mock()
    page.locator.return_value.count.return_value = 0
    page.evaluate.return_value = "登出 帳戶總覽 " + "x" * 500

    crawler = _crawler()
    assert crawler._logged_in(page) is True
    page.wait_for_timeout.assert_not_called()
    page.evaluate.assert_called_once()

    page.url = "https://evil-taishinbank.com/TIBNetBank/home"
    assert crawler._logged_in(page) is False
    assert not crawler._is_login_frame_url("http://my.taishinbank.com.tw/svc/rwd/index.html")
    assert not crawler._is_login_frame_url("https://my.taishinbank.com.tw:444/svc/rwd/index.html")
    assert not crawler._is_login_frame_url("https://user@my.taishinbank.com.tw/svc/rwd/index.html")
    page.url = "http://my.taishinbank.com.tw/TIBNetBank/home"
    assert crawler._logged_in(page) is False
    page.url = "https://my.taishinbank.com.tw/TIBNetBank/home"
    page.evaluate.return_value = "帳戶總覽 " + "x" * 500
    assert crawler._logged_in(page) is False
    page.frames = [Mock(url="https://evil-taishinbank.com/svc/rwd/index.html")]
    assert crawler._find_login_frame(page) is None


def test_authentication_ignores_foreign_frame_identity() -> None:
    main = Mock()
    main.url = "https://my.taishinbank.com.tw/TIBNetBank/home"
    main.evaluate.return_value = "登出 " + "x" * 500
    empty = Mock()
    empty.count.return_value = 0
    main.locator.return_value = empty
    foreign = Mock()
    foreign.url = "https://evil.example/embedded"
    foreign.locator.return_value = empty
    foreign.evaluate.return_value = "帳戶總覽"
    page = SimpleNamespace(
        url=main.url,
        main_frame=main,
        frames=[main, foreign],
        locator=main.locator,
        evaluate=main.evaluate,
    )

    assert _crawler()._logged_in(page) is False
    foreign.locator.assert_not_called()
    foreign.evaluate.assert_not_called()


def test_multiple_matching_login_frames_fail_closed_in_shared_lifecycle(
    monkeypatch,
) -> None:
    import backend.core.base as base_module

    page = Mock()
    page.url = "https://my.taishinbank.com.tw/TIBNetBank/home"
    page.frames = [
        Mock(url="https://my.taishinbank.com.tw/svc/rwd/index.html?a"),
        Mock(url="https://my.taishinbank.com.tw/svc/rwd/index.html?b"),
    ]
    page.main_frame = Mock()
    crawler = _crawler()

    assert crawler._logged_in(page) is False
    page.evaluate.assert_not_called()
    monkeypatch.setattr(crawler, "prepare_login_page", lambda _page: None)
    monkeypatch.setattr(
        base_module,
        "evaluate_login_checkpoint",
        lambda *_args, **_kwargs: CheckpointOutcome(
            CheckpointKind.READY_FOR_CREDENTIALS
        ),
    )

    with pytest.raises(TaishinLoginError, match="找不到登入頁面.*未送出登入"):
        crawler._shared_login(page)

    for frame in page.frames:
        frame.locator.assert_not_called()


def _submit_fixture():
    page = Mock()
    frame = Mock()
    frame.url = "https://my.taishinbank.com.tw/svc/rwd/index.html"
    page.frames = [frame]
    page.main_frame = Mock()

    values = {
        "input[placeholder='身分證字號']": "ID-PRIVATE",
        "input[placeholder='使用者代號']": "USER-PRIVATE",
        "input[placeholder='使用者密碼']": "PASSWORD-PRIVATE",
        "input[placeholder='驗證碼']": "654321",
    }
    fields = {selector: Mock() for selector in values}
    for selector, locator in fields.items():
        locator.count.return_value = 1
        locator.nth.return_value = locator
        locator.is_visible.return_value = True
        locator.is_enabled.return_value = True
        locator.input_value.return_value = values[selector]

    buttons = Mock()
    button = Mock()
    buttons.count.return_value = 1
    buttons.nth.return_value = button
    button.is_visible.return_value = True
    button.is_enabled.return_value = True

    frame.locator.side_effect = lambda selector: {
        **fields,
        "#loginBtn": buttons,
    }[selector]

    crawler = _crawler()
    crawler.creds = SimpleNamespace(
        national_id=values["input[placeholder='身分證字號']"],
        user_code=values["input[placeholder='使用者代號']"],
        password=values["input[placeholder='使用者密碼']"],
    )
    crawler._ocr_captcha = Mock(return_value="654321")
    crawler._logged_in = Mock(return_value=True)
    return crawler, page, frame, fields, buttons, button


def test_submit_verifies_field_cardinality_and_lengths_then_clicks_once() -> None:
    crawler, page, _frame, fields, _buttons, button = _submit_fixture()

    crawler.submit_credentials_once(page)

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
    assert page.keyboard.press.call_args_list == [call("Backspace")] * 4
    assert page.keyboard.type.call_args_list == [
        call("ID-PRIVATE", delay=80),
        call("USER-PRIVATE", delay=80),
        call("PASSWORD-PRIVATE", delay=80),
        call("654321", delay=80),
    ]
    assert page.wait_for_timeout.call_args_list == [
        call(200),
        call(200),
        call(200),
        call(300),
        call(10000),
        call(1000),
    ]
    button.click.assert_called_once_with(timeout=8000)
    page.click.assert_not_called()


def test_submit_frame_inspection_error_is_fieldless_and_zero_click(caplog) -> None:
    crawler, page, _frame, _fields, _buttons, button = _submit_fixture()
    secret = "PRIVATE-FRAME-DOM-987654"
    crawler._find_login_frame = Mock(side_effect=RuntimeError(secret))

    with pytest.raises(TaishinLoginError, match="未送出登入") as error:
        crawler.submit_credentials_once(page)

    assert error.value.__cause__ is None
    assert secret not in str(error.value)
    assert secret not in caplog.text
    button.click.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    ("no_frame", "ambiguous_frame", "duplicate", "length", "input_error"),
)
def test_submit_field_failures_are_fieldless_and_zero_click(failure: str, caplog) -> None:
    crawler, page, _frame, fields, _buttons, button = _submit_fixture()
    secret = "PRIVATE-FIELD-DOM-987654"
    if failure == "no_frame":
        page.frames = []
    elif failure == "ambiguous_frame":
        page.frames.append(Mock(url="https://my.taishinbank.com.tw/svc/rwd/index.html"))
    elif failure == "duplicate":
        fields["input[placeholder='身分證字號']"].count.return_value = 2
    elif failure == "length":
        fields["input[placeholder='使用者密碼']"].input_value.return_value = "short"
    else:
        fields["input[placeholder='使用者代號']"].input_value.side_effect = RuntimeError(secret)

    with pytest.raises(TaishinLoginError, match="未送出登入") as error:
        crawler.submit_credentials_once(page)

    assert error.value.__cause__ is None
    assert secret not in str(error.value)
    assert "ID-PRIVATE" not in str(error.value)
    assert "PASSWORD-PRIVATE" not in str(error.value)
    assert secret not in caplog.text
    button.click.assert_not_called()


@pytest.mark.parametrize(
    ("count", "visible", "enabled"),
    ((0, True, True), (2, True, True), (1, False, True), (1, True, False)),
)
def test_submit_action_must_be_unique_visible_and_enabled(
    count: int,
    visible: bool,
    enabled: bool,
) -> None:
    crawler, page, _frame, _fields, buttons, button = _submit_fixture()
    buttons.count.return_value = count
    button.is_visible.return_value = visible
    button.is_enabled.return_value = enabled

    with pytest.raises(TaishinLoginError, match="未送出登入"):
        crawler.submit_credentials_once(page)

    button.click.assert_not_called()
    page.click.assert_not_called()


def test_submit_click_exception_is_fieldless_unknown_status_and_one_attempt(caplog) -> None:
    crawler, page, _frame, _fields, _buttons, button = _submit_fixture()
    secret = "PRIVATE-CLICK-DOM-987654"
    button.click.side_effect = RuntimeError(secret)

    with pytest.raises(TaishinLoginError, match="送出狀態不明.*禁止自動重試") as error:
        crawler.submit_credentials_once(page)

    assert error.value.__cause__ is None
    assert secret not in str(error.value)
    assert secret not in caplog.text
    button.click.assert_called_once_with(timeout=8000)
    page.click.assert_not_called()


@pytest.mark.parametrize("failing_wait", (10000, 1000))
def test_post_submit_wait_exception_returns_fieldless_after_one_click(
    failing_wait: int,
    caplog,
) -> None:
    crawler, page, _frame, _fields, _buttons, button = _submit_fixture()
    secret = f"PRIVATE-WAIT-{failing_wait}-987654"

    def wait(milliseconds: int) -> None:
        if milliseconds == failing_wait:
            raise RuntimeError(secret)

    page.wait_for_timeout.side_effect = wait

    crawler.submit_credentials_once(page)

    assert secret not in caplog.text
    button.click.assert_called_once_with(timeout=8000)


def test_post_submit_auth_or_inspection_error_returns_after_one_click(caplog) -> None:
    crawler, page, _frame, _fields, _buttons, button = _submit_fixture()
    secret = "PRIVATE-AUTH-DOM-987654"
    crawler._logged_in.side_effect = RuntimeError(secret)

    crawler.submit_credentials_once(page)

    assert secret not in caplog.text
    button.click.assert_called_once_with(timeout=8000)


def test_post_submit_checkpoint_inspection_error_returns_after_one_click(caplog) -> None:
    crawler, page, _frame, _fields, _buttons, button = _submit_fixture()
    secret = "PRIVATE-INSPECTION-DOM-987654"
    crawler._logged_in.return_value = False
    page.locator.side_effect = RuntimeError(secret)

    crawler.submit_credentials_once(page)

    assert secret not in caplog.text
    button.click.assert_called_once_with(timeout=8000)


def _refresh_fixture(count: int = 1):
    frame = Mock()
    frame.evaluate.return_value = "ZmFrZQ=="
    candidates = Mock()
    actions = [Mock() for _ in range(count)]
    candidates.count.return_value = count
    candidates.nth.side_effect = actions.__getitem__
    for action in actions:
        action.is_visible.return_value = True
        action.is_enabled.return_value = True
    frame.locator.return_value = candidates
    return frame, actions


def test_ocr_captures_five_times_and_refreshes_at_most_four(monkeypatch, capsys) -> None:
    crawler = _crawler()
    frame, actions = _refresh_fixture()
    ocr = Mock(return_value="PRIVATE-OCR-MARKER")
    monkeypatch.setattr(taishin_module, "ocr_bytes", ocr)

    assert crawler._ocr_captcha(frame, max_attempts=5) is None

    assert frame.evaluate.call_count == ocr.call_count == 5
    assert all(item.kwargs["min_confidence"] == 0.98 for item in ocr.call_args_list)
    assert actions[0].click.call_count == 4
    assert frame.wait_for_timeout.call_args_list == [call(1500)] * 4
    assert "PRIVATE-OCR-MARKER" not in capsys.readouterr().err


def test_ocr_refresh_ambiguity_or_capture_error_stops_without_click(monkeypatch) -> None:
    crawler = _crawler()
    frame, actions = _refresh_fixture(count=2)
    monkeypatch.setattr(taishin_module, "ocr_bytes", Mock(return_value=None))

    assert crawler._ocr_captcha(frame, max_attempts=5) is None
    assert all(action.click.call_count == 0 for action in actions)
    assert frame.evaluate.call_count == 1

    frame, actions = _refresh_fixture()
    frame.evaluate.side_effect = RuntimeError("PRIVATE-CAPTURE-DOM-987654")
    assert crawler._ocr_captcha(frame, max_attempts=5) is None
    assert actions[0].click.call_count == 0


def test_ocr_refresh_error_returns_none_without_further_capture(monkeypatch) -> None:
    crawler = _crawler()
    frame, actions = _refresh_fixture()
    actions[0].click.side_effect = RuntimeError("PRIVATE-REFRESH-DOM-987654")
    monkeypatch.setattr(taishin_module, "ocr_bytes", Mock(return_value=None))

    assert crawler._ocr_captcha(frame, max_attempts=9) is None
    assert frame.evaluate.call_count == 1
    actions[0].click.assert_called_once_with()


def test_real_submit_stops_at_multiple_modals_without_actions(monkeypatch) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(
            """
            <input placeholder="身分證字號"><input placeholder="使用者代號">
            <input placeholder="使用者密碼"><input placeholder="驗證碼">
            <button id="loginBtn">登入</button>
            <script>
              document.body.dataset.submit = '0';
              document.body.dataset.modal = '0';
              loginBtn.onclick = () => {
                document.body.dataset.submit++;
                document.body.insertAdjacentHTML('beforeend', `
                  <div class="modal show">PRIVATE-ONE<button>確認</button></div>
                  <div role="dialog">PRIVATE-TWO<button>確定</button></div>
                `);
                document.querySelectorAll('.modal button, [role=dialog] button').forEach(button => {
                  button.onclick = () => document.body.dataset.modal++;
                });
              };
            </script>
            """
        )
        crawler = _crawler()
        crawler.creds = SimpleNamespace(
            national_id="ID-PRIVATE",
            user_code="USER-PRIVATE",
            password="PASSWORD-PRIVATE",
        )
        monkeypatch.setattr(crawler, "_find_login_frame", lambda _page: page)
        monkeypatch.setattr(crawler, "_ocr_captcha", lambda *_args, **_kwargs: "654321")
        monkeypatch.setattr(crawler, "_logged_in", lambda _page: False)
        original_wait = page.wait_for_timeout
        monkeypatch.setattr(page, "wait_for_timeout", lambda milliseconds: original_wait(1))

        crawler.submit_credentials_once(page)

        assert page.locator("body").get_attribute("data-submit") == "1"
        assert page.locator("body").get_attribute("data-modal") == "0"
        assert page.locator(".modal.show").count() == 1
        assert page.locator("[role='dialog']").count() == 1
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_collect_keeps_credit_card_navigation_and_parser_without_popup_surgery() -> None:
    source = inspect.getsource(TaishinCrawler.collect)

    assert "_close_popups" not in source
    assert "01_after_close_popups.png" not in source
    assert "self._try_ancestor_clicks(target_frame, page)" in source
    assert "self._parse_credit_card_page(page_text)" in source
    assert "self._parse_credit_card_page(month_text)" in source
