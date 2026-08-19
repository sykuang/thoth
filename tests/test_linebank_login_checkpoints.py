from __future__ import annotations

import inspect
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

import backend.banks.linebank as linebank_module
from backend.banks.linebank import LinebankCrawler, LinebankLoginError
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointPhase,
    evaluate_login_checkpoint,
)


def _crawler() -> LinebankCrawler:
    crawler = object.__new__(LinebankCrawler)
    crawler.name = "linebank"
    return crawler


def test_linebank_shared_login_api_and_ordered_rules() -> None:
    rules = _crawler().login_checkpoint_rules()
    post = (CheckpointPhase.POST_SUBMIT, CheckpointPhase.POST_SUBMIT_SETTLE)

    assert LinebankCrawler.USES_SHARED_LOGIN_CHECKPOINTS is True
    assert [rule.name for rule in rules] == [
        "linebank-otp-required",
        "linebank-login-success-notice",
        "linebank-unknown-modal",
        "linebank-login-form-still-visible",
    ]
    assert [rule.kind for rule in rules] == [
        CheckpointKind.OTP_REQUIRED,
        CheckpointKind.DISMISSIBLE_NOTICE,
        CheckpointKind.UNKNOWN_BLOCKER,
        CheckpointKind.UNKNOWN_BLOCKER,
    ]
    assert all(rule.bank == "linebank" and rule.phases == post for rule in rules)
    assert [rule.container_selector for rule in rules] == [
        ".modal.show",
        ".modal.show",
        ".modal.show",
        "#nationalId",
    ]
    assert rules[0].action_texts == ()
    assert rules[1].action_texts == ("確定",)
    assert rules[1].max_actions == 1
    assert rules[2].action_texts == rules[3].action_texts == ()


@pytest.mark.parametrize(
    "marker",
    [
        "簡訊驗證碼",
        "請輸入OTP驗證碼",
        "一次性密碼",
        "驗證碼已傳送",
        "請輸入您收到的簡訊驗證碼",
        "裝置驗證",
        "信任此裝置",
        "新裝置登入",
    ],
)
def test_linebank_otp_pattern_accepts_anchored_observed_markers(marker: str) -> None:
    pattern = _crawler().login_checkpoint_rules()[0].required_body_pattern

    assert pattern is not None
    assert pattern.fullmatch(f"登入程序\n{marker}\n請繼續")


@pytest.mark.parametrize(
    "body",
    [
        "請輸入圖形驗證碼",
        "圖形驗證碼錯誤",
        "一般登入提醒",
        "驗證碼圖片",
        "請輸入您收到的圖形驗證碼",
    ],
)
def test_linebank_otp_pattern_excludes_graphical_and_generic_captcha(body: str) -> None:
    pattern = _crawler().login_checkpoint_rules()[0].required_body_pattern

    assert pattern is not None
    assert not pattern.search(body)


@pytest.mark.parametrize(
    ("positive", "negative"),
    [
        ("登入\n確定", "確定\n登入"),
        ("  登入\n確定  ", "登入成功\n確定"),
        ("登入   確定", "LINE Bank\n登入\n確定"),
        ("登入\n\n確定", "登入\n安全提醒\n確定"),
    ],
)
def test_linebank_success_notice_pattern_is_anchored(
    positive: str,
    negative: str,
) -> None:
    pattern = _crawler().login_checkpoint_rules()[1].required_body_pattern

    assert pattern is not None
    assert pattern.fullmatch(positive)
    assert not pattern.search(negative)


def test_linebank_unsafe_login_helpers_and_private_debug_are_removed() -> None:
    source = inspect.getsource(linebank_module)

    assert not hasattr(LinebankCrawler, "_detect_otp")
    assert not hasattr(LinebankCrawler, "_dismiss_post_login_modal")
    assert not hasattr(linebank_module, "LinebankOtpRequired")
    for forbidden in (
        "_login_snapshot",
        "01_no_fields.png",
        "02_fill_failed.png",
        "03_after_login.png",
        "keyboard type 後",
        "送出後 url=",
    ):
        assert forbidden not in source


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
        bank="linebank",
        phase=phase,
        rules=_crawler().login_checkpoint_rules(),
        is_authenticated=lambda _page: False,
    )


def test_real_dom_rules_are_scoped_terminal_first_and_fail_closed() -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(
            """
            <div class="modal show" id="otp">
              登入<br>請輸入簡訊驗證碼<br><button>確定</button>
            </div>
            <script>
              document.body.dataset.clicks = '0';
              otp.querySelector('button').onclick = () => document.body.dataset.clicks++;
            </script>
            """
        )
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.kind is CheckpointKind.OTP_REQUIRED
        assert outcome.rule_name == "linebank-otp-required"
        assert outcome.interaction == "otp"
        assert page.locator("body").get_attribute("data-clicks") == "0"

        page.set_content(
            """
            <button id="outside">確定</button>
            <div class="modal show" id="notice">登入<br><button>確定</button></div>
            <script>
              document.body.dataset.inside = '0';
              document.body.dataset.outside = '0';
              outside.onclick = () => document.body.dataset.outside++;
              notice.querySelector('button').onclick = () => {
                document.body.dataset.inside++;
                notice.hidden = true;
              };
            </script>
            """
        )
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT_SETTLE)
        assert outcome.kind is CheckpointKind.DISMISSIBLE_NOTICE
        assert outcome.rule_name == "linebank-login-success-notice"
        assert outcome.action_label == "確定"
        assert page.locator("body").get_attribute("data-inside") == "1"
        assert page.locator("body").get_attribute("data-outside") == "0"
        assert not page.locator("#notice").is_visible()

        for body in (
            "登入成功<br><button>確認</button>",
            "一般提醒<br><button>確定</button>",
            "登入成功<br><button>確定</button><br>繼續",
            "登入失敗<br><button>確定</button>",
            "登入<br>安全提醒<br><button>確定</button>",
            "登入<br>必須變更密碼<br><button>確定</button>",
        ):
            page.set_content(
                f"""
                <div class="modal show" id="unknown">{body}</div>
                <script>
                  document.body.dataset.clicks = '0';
                  unknown.querySelector('button').onclick = () => document.body.dataset.clicks++;
                </script>
                """
            )
            outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
            assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
            assert outcome.rule_name == "linebank-unknown-modal"
            assert page.locator("body").get_attribute("data-clicks") == "0"

        page.set_content(
            '<div class="modal show">請輸入圖形驗證碼<button>確定</button></div>'
        )
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
        assert outcome.rule_name == "linebank-unknown-modal"

        page.set_content(
            '<div class="modal show">登入<br>請輸入您收到的圖形驗證碼<button>確定</button></div>'
        )
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
        assert outcome.rule_name == "linebank-unknown-modal"

        secret = "PRIVATE-LINEBANK-MODAL-987654"
        page.set_content(f'<div class="modal show">{secret}<button>確定</button></div>')
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
        assert secret not in repr(outcome)

        page.set_content("<p>請輸入簡訊驗證碼</p>")
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
        assert outcome.rule_name is None

        page.set_content('<input id="nationalId">')
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT_SETTLE)
        assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
        assert outcome.rule_name == "linebank-login-form-still-visible"

        page.set_content('<div class="modal show">登入<button>確定</button></div>')
        assert _evaluate(page, CheckpointPhase.PRE_SUBMIT).kind is CheckpointKind.READY_FOR_CREDENTIALS

        page.set_content(
            '<div class="modal show" hidden>登入<button>確定</button></div>'
            '<input id="nationalId">'
        )
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.rule_name == "linebank-login-form-still-visible"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def _submit_fixture():
    page = Mock()
    values = {
        "#nationalId": "ID-PRIVATE",
        "#userId": "USER-PRIVATE",
        "#pw": "PASSWORD-PRIVATE",
    }
    fields = {selector: Mock() for selector in values}
    for selector, locator in fields.items():
        locator.count.return_value = 1
        locator.nth.return_value = locator
        locator.input_value.return_value = values[selector]

    buttons = Mock()
    button = Mock()
    buttons.count.return_value = 1
    buttons.nth.return_value = button
    button.is_visible.return_value = True
    button.is_enabled.return_value = True
    button.inner_text.return_value = "登入"
    button.get_attribute.return_value = None

    modals = Mock()
    modal = Mock()
    modals.count.return_value = 1
    modals.nth.return_value = modal
    modal.is_visible.return_value = True
    page.locator.side_effect = lambda selector: {
        **fields,
        "button": buttons,
        ".modal.show": modals,
    }[selector]

    crawler = _crawler()
    crawler.creds = SimpleNamespace(
        national_id=values["#nationalId"],
        user_code=values["#userId"],
        password=values["#pw"],
    )
    crawler._logged_in = Mock(return_value=False)
    return crawler, page, fields, buttons, button, modals


def test_submit_uses_true_keyboard_length_gate_and_one_native_click() -> None:
    crawler, page, fields, _buttons, button, _modals = _submit_fixture()

    crawler.submit_credentials_once(page)

    assert page.wait_for_selector.call_args_list == [
        call("#nationalId", state="visible", timeout=15000),
        call("#userId", state="visible", timeout=5000),
        call("#pw", state="visible", timeout=5000),
    ]
    for locator in fields.values():
        assert locator.method_calls == [
            call.count(),
            call.click(),
            call.click(click_count=3),
            call.input_value(),
        ]
    assert page.keyboard.press.call_args_list == [call("Backspace")] * 3
    assert page.keyboard.type.call_args_list == [
        call("ID-PRIVATE", delay=60),
        call("USER-PRIVATE", delay=60),
        call("PASSWORD-PRIVATE", delay=60),
    ]
    assert page.wait_for_timeout.call_args_list == [
        call(150), call(100), call(100), call(250),
        call(150), call(100), call(100), call(250),
        call(150), call(100), call(100), call(250),
        call(10000), call(1000),
    ]
    button.click.assert_called_once_with(timeout=8000)
    page.click.assert_not_called()
    page.evaluate.assert_not_called()


@pytest.mark.parametrize("failure", ["wait", "type", "input_value", "duplicate"])
def test_submit_field_browser_errors_or_nonunique_id_are_fieldless_and_zero_click(
    failure: str,
    caplog,
) -> None:
    crawler, page, fields, _buttons, button, _modals = _submit_fixture()
    secret = "PRIVATE-LINEBANK-FIELD-987654"
    if failure == "wait":
        page.wait_for_selector.side_effect = RuntimeError(secret)
    elif failure == "type":
        page.keyboard.type.side_effect = RuntimeError(secret)
    elif failure == "input_value":
        fields["#userId"].input_value.side_effect = RuntimeError(secret)
    else:
        fields["#nationalId"].count.return_value = 2

    with pytest.raises(LinebankLoginError, match="未送出登入") as error:
        crawler.submit_credentials_once(page)

    assert error.value.__cause__ is None
    assert secret not in str(error.value)
    assert "ID-PRIVATE" not in str(error.value)
    assert "USER-PRIVATE" not in str(error.value)
    assert "PASSWORD-PRIVATE" not in str(error.value)
    assert secret not in caplog.text
    button.click.assert_not_called()
    page.click.assert_not_called()
    page.evaluate.assert_not_called()


def test_submit_length_mismatch_is_fieldless_and_zero_click(caplog) -> None:
    crawler, page, fields, _buttons, button, _modals = _submit_fixture()
    fields["#pw"].input_value.return_value = "short"

    with pytest.raises(LinebankLoginError, match="輸入長度不符.*未送出登入") as error:
        crawler.submit_credentials_once(page)

    assert error.value.__cause__ is None
    assert "short" not in str(error.value)
    assert "PASSWORD-PRIVATE" not in str(error.value)
    assert "short" not in caplog.text
    button.click.assert_not_called()
    page.evaluate.assert_not_called()


@pytest.mark.parametrize(
    ("count", "visible", "enabled", "text", "aria"),
    [
        (0, True, True, "登入", None),
        (2, True, True, "登入", None),
        (1, False, True, "登入", None),
        (1, True, False, "登入", None),
        (1, True, True, "立即登入", None),
        (1, True, True, "", "登入友善網路銀行其他"),
    ],
)
def test_submit_action_missing_ambiguous_hidden_disabled_or_wrong_is_zero_click(
    count: int,
    visible: bool,
    enabled: bool,
    text: str,
    aria: str | None,
) -> None:
    crawler, page, _fields, buttons, button, _modals = _submit_fixture()
    buttons.count.return_value = count
    button.is_visible.return_value = visible
    button.is_enabled.return_value = enabled
    button.inner_text.return_value = text
    button.get_attribute.return_value = aria

    with pytest.raises(LinebankLoginError, match="唯一且可操作.*未送出登入"):
        crawler.submit_credentials_once(page)

    button.click.assert_not_called()
    page.click.assert_not_called()
    page.evaluate.assert_not_called()


@pytest.mark.parametrize(
    ("text", "aria"),
    [
        (" 登入 ", None),
        ("登入友善網路銀行", None),
        ("", "登入友善網路銀行"),
    ],
)
def test_submit_accepts_only_exact_text_or_exact_accessible_label(
    text: str,
    aria: str | None,
) -> None:
    crawler, _page, _fields, _buttons, button, _modals = _submit_fixture()
    button.inner_text.return_value = text
    button.get_attribute.return_value = aria

    crawler.submit_credentials_once(_page)

    button.click.assert_called_once_with(timeout=8000)


def test_submit_action_inspection_exception_is_fieldless_and_zero_click(caplog) -> None:
    crawler, page, _fields, buttons, button, _modals = _submit_fixture()
    secret = "PRIVATE-LINEBANK-ACTION-987654"
    buttons.count.side_effect = RuntimeError(secret)

    with pytest.raises(LinebankLoginError, match="無法安全確認.*未送出登入") as error:
        crawler.submit_credentials_once(page)

    assert error.value.__cause__ is None
    assert secret not in str(error.value)
    assert secret not in caplog.text
    button.click.assert_not_called()
    page.evaluate.assert_not_called()


def test_submit_click_exception_is_fieldless_unknown_status_and_one_attempt(caplog) -> None:
    crawler, page, _fields, _buttons, button, _modals = _submit_fixture()
    secret = "PRIVATE-LINEBANK-CLICK-987654"
    button.click.side_effect = RuntimeError(secret)

    with pytest.raises(LinebankLoginError, match="送出狀態不明.*禁止自動重試") as error:
        crawler.submit_credentials_once(page)

    assert error.value.__cause__ is None
    assert secret not in str(error.value)
    assert secret not in caplog.text
    button.click.assert_called_once_with(timeout=8000)
    page.click.assert_not_called()
    page.evaluate.assert_not_called()


@pytest.mark.parametrize("failing_wait", [10000, 1000])
def test_post_submit_wait_exception_is_fieldless_after_one_click(
    failing_wait: int,
    caplog,
) -> None:
    crawler, page, _fields, _buttons, button, _modals = _submit_fixture()
    secret = f"PRIVATE-LINEBANK-WAIT-{failing_wait}-987654"

    def wait(milliseconds: int) -> None:
        if milliseconds == failing_wait:
            raise RuntimeError(secret)

    page.wait_for_timeout.side_effect = wait

    crawler.submit_credentials_once(page)

    assert secret not in caplog.text
    button.click.assert_called_once_with(timeout=8000)
    page.click.assert_not_called()


@pytest.mark.parametrize("failure", ["auth", "modal", "form"])
def test_post_submit_inspection_exception_is_fieldless_after_one_click(
    failure: str,
    caplog,
) -> None:
    crawler, page, fields, _buttons, button, modals = _submit_fixture()
    secret = f"PRIVATE-LINEBANK-INSPECTION-{failure}-987654"
    if failure == "auth":
        crawler._logged_in.side_effect = RuntimeError(secret)
    elif failure == "modal":
        modals.count.side_effect = RuntimeError(secret)
    else:
        modals.count.return_value = 0
        fields["#nationalId"].count.side_effect = [1, RuntimeError(secret)]

    crawler.submit_credentials_once(page)

    assert secret not in caplog.text
    button.click.assert_called_once_with(timeout=8000)
    page.click.assert_not_called()


def test_post_submit_timeout_returns_for_outer_evaluator_after_twenty_polls() -> None:
    crawler, page, fields, _buttons, button, modals = _submit_fixture()
    modals.count.return_value = 0
    fields["#nationalId"].is_visible.return_value = False

    crawler.submit_credentials_once(page)

    assert page.wait_for_timeout.call_args_list[-21:] == [call(10000)] + [call(1000)] * 20
    assert crawler._logged_in.call_count == 20
    button.click.assert_called_once_with(timeout=8000)


def test_real_submit_structural_wait_stops_at_multiple_modals_without_actions(monkeypatch) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(
            """
            <input id="nationalId"><input id="userId"><input id="pw">
            <button>登入</button>
            <script>
              document.body.dataset.submit = '0';
              document.body.dataset.modal = '0';
              document.querySelector('button').onclick = () => {
                document.body.dataset.submit++;
                document.body.insertAdjacentHTML('beforeend', `
                  <div class="modal show">PRIVATE-ONE<button>確認</button></div>
                  <div class="modal show">PRIVATE-TWO<button>確定</button></div>
                `);
                document.querySelectorAll('.modal button').forEach(button => {
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
        monkeypatch.setattr(crawler, "_logged_in", lambda _page: False)
        original_wait = page.wait_for_timeout
        monkeypatch.setattr(
            page,
            "wait_for_timeout",
            lambda milliseconds: original_wait(1 if milliseconds >= 1000 else milliseconds),
        )

        crawler.submit_credentials_once(page)

        assert page.locator("body").get_attribute("data-submit") == "1"
        assert page.locator("body").get_attribute("data-modal") == "0"
        assert page.locator(".modal.show").count() == 2
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_login_prepare_authentication_and_collect_are_direct_adapters(monkeypatch) -> None:
    page = Mock()
    crawler = _crawler()
    shared_login = Mock(return_value=True)
    logged_in = Mock(return_value=True)
    monkeypatch.setattr(crawler, "_shared_login", shared_login)
    monkeypatch.setattr(crawler, "_logged_in", logged_in)

    assert crawler.login(page)
    shared_login.assert_called_once_with(page)
    crawler.prepare_login_page(page)
    assert page.mock_calls == [call.wait_for_timeout(6000)]
    assert crawler.is_authenticated(page)
    logged_in.assert_called_once_with(page)

    collect_source = inspect.getsource(LinebankCrawler.collect)
    assert "_dismiss_post_login_modal" not in collect_source
    assert re.search(r"page\.goto\(\s*\"https://accessibility\.linebank\.com\.tw/transaction\"", collect_source)


def test_authentication_inspection_does_not_log_private_metadata(capsys) -> None:
    page = Mock(url="https://accessibility.linebank.com.tw/PRIVATE-PATH")
    page.evaluate.return_value = False

    assert _crawler()._logged_in(page) is False
    captured = capsys.readouterr()
    assert "PRIVATE-PATH" not in captured.err
