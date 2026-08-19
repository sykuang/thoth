from __future__ import annotations

import inspect
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

import backend.banks.ubot as ubot_module
from backend.banks.ubot import UbotCrawler, UbotLoginError
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointPhase,
    evaluate_login_checkpoint,
)


def _crawler() -> UbotCrawler:
    crawler = object.__new__(UbotCrawler)
    crawler.name = "ubot"
    crawler._credential_origin_allowed = lambda _page: True
    return crawler


def test_ubot_shared_login_api_and_ordered_rules() -> None:
    crawler = _crawler()
    rules = crawler.login_checkpoint_rules()
    post = (CheckpointPhase.POST_SUBMIT, CheckpointPhase.POST_SUBMIT_SETTLE)

    assert UbotCrawler.USES_SHARED_LOGIN_CHECKPOINTS is True
    assert [rule.name for rule in rules] == [
        "ubot-password-change-required",
        "ubot-otp-required",
        "ubot-password-change-optional",
        "ubot-unknown-modal",
        "ubot-login-form-still-visible",
    ]
    assert [rule.kind for rule in rules] == [
        CheckpointKind.PASSWORD_CHANGE_REQUIRED,
        CheckpointKind.OTP_REQUIRED,
        CheckpointKind.PASSWORD_CHANGE_OPTIONAL,
        CheckpointKind.UNKNOWN_BLOCKER,
        CheckpointKind.UNKNOWN_BLOCKER,
    ]
    assert all(rule.bank == "ubot" for rule in rules)
    assert rules[0].phases == rules[1].phases == rules[3].phases == tuple(CheckpointPhase)
    assert rules[2].phases == rules[4].phases == post
    assert [rule.container_selector for rule in rules] == [
        ".modal.show",
        ".modal.show",
        ".modal.show",
        ".modal.show",
        "#sid",
    ]
    assert rules[0].action_texts == ()
    assert rules[1].action_texts == ()
    assert rules[2].action_texts == (
        "以後再說",
        "暫不變更",
        "不變更",
        "下次再說",
        "Later",
        "Skip",
    )
    assert rules[2].max_actions == 1
    assert rules[3].action_texts == rules[4].action_texts == ()


@pytest.mark.parametrize(
    ("rule_index", "positive", "negative"),
    [
        (0, "您的密碼已過期，請立即處理", "建議定期變更密碼"),
        (0, "You are required to change your password", "Change is recommended"),
        (1, "請輸入簡訊驗證碼", "一般登入提醒"),
        (1, "Device verification is required", "Device registration complete"),
        (2, "您離上次變更密碼已超過6個月，建議請定期變更密碼", "密碼已過期，必須變更密碼"),
        (2, "We recommend that you change your password", "You are required to change your password"),
        (2, "密碼已超過六個月未變更，建議變更密碼", "查詢紀錄期間已超過6個月"),
    ],
)
def test_ubot_rule_patterns_are_anchored_positive_and_negative(
    rule_index: int,
    positive: str,
    negative: str,
) -> None:
    pattern = _crawler().login_checkpoint_rules()[rule_index].required_body_pattern

    assert pattern is not None
    assert pattern.fullmatch(positive)
    assert not pattern.search(negative)


def test_ubot_unsafe_login_helpers_and_generic_popup_actions_are_removed() -> None:
    source = inspect.getsource(ubot_module)

    for symbol in (
        "JS_OPEN_MODAL",
        "JS_PERSONAL_TAB",
        "JS_CLICK_LOGIN",
        "JS_REGEN",
        "JS_STILL_LOGIN",
        "JS_ERR_MSG",
    ):
        assert not hasattr(ubot_module, symbol)
    assert not hasattr(UbotCrawler, "_close_popups")
    assert "_login_snapshot" not in source
    assert "captcha={captcha}" not in source
    assert "讀到 {captcha" not in source
    assert re.search(r"def collect\([\s\S]*?self\._goto\(page, \"/A0101001\"", source)


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
        bank="ubot",
        phase=phase,
        rules=_crawler().login_checkpoint_rules(),
        is_authenticated=lambda _page: False,
    )


def test_real_dom_rules_are_terminal_first_scoped_and_fail_closed() -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()

        page.set_content(
            """
            <div class="modal show" id="required">
              密碼已過期；您已超過6個月未變更密碼
              <button>Skip</button>
            </div>
            <script>
              document.body.dataset.clicks = '0';
              required.querySelector('button').onclick = () => document.body.dataset.clicks++;
            </script>
            """
        )
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.kind is CheckpointKind.PASSWORD_CHANGE_REQUIRED
        assert page.locator("body").get_attribute("data-clicks") == "0"

        page.set_content(
            """
            <div class="modal show" id="otp">請輸入 OTP 驗證碼<button>Skip</button></div>
            <script>
              document.body.dataset.clicks = '0';
              otp.querySelector('button').onclick = () => document.body.dataset.clicks++;
            </script>
            """
        )
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT_SETTLE)
        assert outcome.kind is CheckpointKind.OTP_REQUIRED
        assert page.locator("body").get_attribute("data-clicks") == "0"

        page.set_content(
            """
            <button id="outside">Skip</button>
            <div class="modal show" id="optional">
              您離上次變更密碼已超過6個月，建議請定期變更密碼
              <button>Skip</button>
            </div>
            <script>
              document.body.dataset.inside = '0';
              document.body.dataset.outside = '0';
              outside.onclick = () => document.body.dataset.outside++;
              optional.querySelector('button').onclick = () => {
                document.body.dataset.inside++;
                optional.hidden = true;
              };
            </script>
            """
        )
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.kind is CheckpointKind.PASSWORD_CHANGE_OPTIONAL
        assert outcome.action_label == "Skip"
        assert page.locator("body").get_attribute("data-inside") == "1"
        assert page.locator("body").get_attribute("data-outside") == "0"

        for label in ("確認", "確定", "Cancel", "取消"):
            page.set_content(
                f"""
                <div class="modal show" id="unknown">一般提醒<button>{label}</button></div>
                <script>
                  document.body.dataset.clicks = '0';
                  unknown.querySelector('button').onclick = () => document.body.dataset.clicks++;
                </script>
                """
            )
            outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
            assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
            assert outcome.rule_name == "ubot-unknown-modal"
            assert page.locator("body").get_attribute("data-clicks") == "0"

        page.set_content(
            """
            <div class="modal show" id="six-month-notice">
              查詢紀錄期間已超過6個月<button>Skip</button>
            </div>
            <script>
              document.body.dataset.clicks = '0';
              document.querySelector('#six-month-notice button').onclick = () =>
                document.body.dataset.clicks++;
            </script>
            """
        )
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
        assert outcome.rule_name == "ubot-unknown-modal"
        assert page.locator("body").get_attribute("data-clicks") == "0"

        secret = "PRIVATE-UBOT-MODAL-987654"
        page.set_content(f'<div class="modal show">{secret}<button>確認</button></div>')
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
        assert secret not in repr(outcome)

        page.set_content('<input id="sid">')
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
        assert outcome.rule_name == "ubot-login-form-still-visible"

        page.set_content(
            """
            <div class="modal show" id="login-form">
              <input id="sid"><label>圖形驗證碼</label><button>登入</button>
            </div>
            <script>
              document.body.dataset.clicks = '0';
              document.querySelector('#login-form button').onclick = () =>
                document.body.dataset.clicks++;
            </script>
            """
        )
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
        assert outcome.rule_name == "ubot-unknown-modal"
        assert page.locator("body").get_attribute("data-clicks") == "0"

        page.set_content('<div class="modal show"><input id="sid"><button>登入</button></div>')
        outcome = _evaluate(page, CheckpointPhase.PRE_SUBMIT)
        assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
        assert outcome.rule_name == "ubot-unknown-modal"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_prepare_uses_unique_native_opener_and_personal_tab(monkeypatch) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        monkeypatch.setattr(page, "wait_for_timeout", lambda _milliseconds: None)
        page.set_content(
            """
            <button id="opener"> 網銀登入 </button>
            <button disabled>網銀登入</button>
            <div id="modal" hidden></div>
            <script>
              document.body.dataset.open = '0';
              document.body.dataset.tab = '0';
              document.querySelector('#opener').onclick = () => {
                document.body.dataset.open++;
                modal.hidden = false;
                modal.innerHTML = '<button role="tab">個人用戶登入</button>';
                modal.querySelector('button').onclick = () => {
                  document.body.dataset.tab++;
                  modal.insertAdjacentHTML('beforeend', '<input id="sid">');
                };
              };
            </script>
            """
        )
        crawler = _crawler()
        monkeypatch.setattr(crawler, "_logged_in", lambda _page: False)

        crawler.prepare_login_page(page)

        assert page.locator("body").get_attribute("data-open") == "1"
        assert page.locator("body").get_attribute("data-tab") == "1"
        assert page.locator("#sid").is_visible()
        assert "evaluate" not in inspect.getsource(UbotCrawler.prepare_login_page)
        assert "evaluate" not in inspect.getsource(ubot_module._unique_visible_enabled_exact)
    finally:
        browser.close()
        manager.__exit__(None, None, None)


@pytest.mark.parametrize(
    "body",
    [
        "<button>網銀登入</button><a>網銀登入</a>",
        "<button>開啟網銀登入</button>",
    ],
)
def test_prepare_ambiguous_or_missing_opener_never_clicks(
    monkeypatch,
    body: str,
) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        monkeypatch.setattr(page, "wait_for_timeout", lambda _milliseconds: None)
        page.set_content(
            body
            + """
              <script>
                document.body.dataset.clicks = '0';
                document.querySelectorAll('button,a').forEach(
                  item => item.onclick = () => document.body.dataset.clicks++
                );
              </script>
            """
        )
        crawler = _crawler()
        monkeypatch.setattr(crawler, "_logged_in", lambda _page: False)

        crawler.prepare_login_page(page)

        assert page.locator("body").get_attribute("data-clicks") == "0"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_prepare_authenticated_returns_before_any_action() -> None:
    page = Mock()
    crawler = _crawler()
    crawler._logged_in = Mock(return_value=True)

    crawler.prepare_login_page(page)

    assert page.mock_calls == [call.wait_for_timeout(8000)]


@pytest.mark.parametrize("tab_count", [0, 2])
def test_prepare_missing_or_ambiguous_personal_tab_never_clicks(
    monkeypatch,
    tab_count: int,
) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        monkeypatch.setattr(page, "wait_for_timeout", lambda _milliseconds: None)
        tabs = "".join("<button role='tab'>個人用戶登入</button>" for _ in range(tab_count))
        page.set_content(
            f"""
            <button id="opener">網銀登入</button><div id="modal" hidden>{tabs}</div>
            <script>
              document.body.dataset.open = '0';
              document.body.dataset.tab = '0';
              document.querySelector('#opener').onclick = () => {{
                document.body.dataset.open++;
                modal.hidden = false;
              }};
              document.querySelectorAll('[role=tab]').forEach(
                tab => tab.onclick = () => document.body.dataset.tab++
              );
            </script>
            """
        )
        crawler = _crawler()
        monkeypatch.setattr(crawler, "_logged_in", lambda _page: False)

        crawler.prepare_login_page(page)

        assert page.locator("body").get_attribute("data-open") == "1"
        assert page.locator("body").get_attribute("data-tab") == "0"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_prepare_locator_exception_is_fieldless(caplog) -> None:
    page = Mock()
    page.locator.side_effect = RuntimeError("PRIVATE-PREPARE-DOM-987654")
    crawler = _crawler()
    crawler._logged_in = Mock(return_value=False)

    crawler.prepare_login_page(page)

    assert "PRIVATE-PREPARE-DOM-987654" not in caplog.text
    page.click.assert_not_called()
    page.evaluate.assert_not_called()


@pytest.mark.parametrize("failing_wait", [8000, 2500, 800])
def test_prepare_wait_exception_is_fieldless(
    failing_wait: int,
    caplog,
) -> None:
    page = Mock()
    empty = Mock()
    empty.count.return_value = 0
    page.locator.return_value = empty
    secret = f"PRIVATE-PREPARE-WAIT-{failing_wait}-987654"

    def wait(milliseconds: int) -> None:
        if milliseconds == failing_wait:
            raise RuntimeError(secret)

    page.wait_for_timeout.side_effect = wait
    crawler = _crawler()
    crawler._logged_in = Mock(return_value=False)

    crawler.prepare_login_page(page)

    assert secret not in caplog.text
    page.click.assert_not_called()
    page.evaluate.assert_not_called()


def _submit_fixture():
    page = Mock()
    values = {
        "#sid": "ID-PRIVATE",
        "#nickname": "USER-PRIVATE",
        "#password": "PASSWORD-PRIVATE",
        "#CAPTCHA": "654321",
    }
    fields = {selector: Mock() for selector in values}
    for selector, field in fields.items():
        field.count.return_value = 1
        field.nth.return_value = field
        field.is_visible.return_value = True
        field.is_enabled.return_value = True
        field.input_value.return_value = values[selector]
    login_candidates = Mock()
    button = Mock()
    modals = Mock()
    modal = Mock()
    login_candidates.count.return_value = 1
    login_candidates.nth.return_value = button
    button.is_visible.return_value = True
    button.is_enabled.return_value = True
    button.inner_text.return_value = "登入"
    button.get_attribute.return_value = "ubot-primary-green"
    modals.count.return_value = 1
    modals.nth.return_value = modal
    modal.is_visible.return_value = True
    page.locator.side_effect = lambda selector: {
        **fields,
        "button.ubot-primary-green": login_candidates,
        ".modal.show": modals,
    }[selector]
    page.test_fields = fields

    crawler = _crawler()
    crawler.creds = SimpleNamespace(
        national_id="ID-PRIVATE",
        user_code="USER-PRIVATE",
        password="PASSWORD-PRIVATE",
    )
    crawler._ocr_with_regen = Mock(return_value="654321")
    crawler._logged_in = Mock(return_value=False)
    return crawler, page, login_candidates, button, modals


def test_submit_uses_true_keyboard_one_native_click_and_preserves_timing(capsys) -> None:
    crawler, page, _candidates, button, _modals = _submit_fixture()

    crawler.submit_credentials_once(page)

    page.wait_for_selector.assert_called_once_with("#sid", state="visible", timeout=10000)
    for field in page.test_fields.values():
        assert field.method_calls == [
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
        call(150),
        call(150),
        call(200),
        call(200),
        call(6000),
        call(1000),
    ]
    button.click.assert_called_once_with(timeout=8000)
    page.fill.assert_not_called()
    page.click.assert_not_called()
    page.evaluate.assert_not_called()
    assert "654321" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("count", "visible", "enabled", "label", "classes"),
    [
        (0, True, True, "登入", "ubot-primary-green"),
        (2, True, True, "登入", "ubot-primary-green"),
        (1, False, True, "登入", "ubot-primary-green"),
        (1, True, False, "登入", "ubot-primary-green"),
        (1, True, True, "登入其他", "ubot-primary-green"),
        (1, True, True, "登入", "ubot-primary-green disabled"),
    ],
)
def test_submit_action_preconditions_fail_before_click(
    count: int,
    visible: bool,
    enabled: bool,
    label: str,
    classes: str,
) -> None:
    crawler, page, candidates, button, _modals = _submit_fixture()
    candidates.count.return_value = count
    button.is_visible.return_value = visible
    button.is_enabled.return_value = enabled
    button.inner_text.return_value = label
    button.get_attribute.return_value = classes

    with pytest.raises(UbotLoginError, match="未送出登入"):
        crawler.submit_credentials_once(page)

    button.click.assert_not_called()
    page.click.assert_not_called()
    page.evaluate.assert_not_called()


@pytest.mark.parametrize("failure", ["wait", "keyboard"])
def test_submit_field_failure_is_fieldless_and_zero_click(
    failure: str,
    caplog,
) -> None:
    crawler, page, _candidates, button, _modals = _submit_fixture()
    secret = "PRIVATE-FIELD-DOM-987654"
    if failure == "wait":
        page.wait_for_selector.side_effect = RuntimeError(secret)
    else:
        page.test_fields["#nickname"].click.side_effect = RuntimeError(secret)

    with pytest.raises(UbotLoginError, match="未送出登入") as error:
        crawler.submit_credentials_once(page)

    assert secret not in str(error.value)
    assert "ID-PRIVATE" not in str(error.value)
    assert "USER-PRIVATE" not in str(error.value)
    assert "PASSWORD-PRIVATE" not in str(error.value)
    assert secret not in caplog.text
    button.click.assert_not_called()
    _candidates.count.assert_not_called()


def test_submit_ocr_failure_sends_zero() -> None:
    crawler, page, _candidates, button, _modals = _submit_fixture()
    crawler._ocr_with_regen.return_value = None

    with pytest.raises(UbotLoginError, match="未送出登入"):
        crawler.submit_credentials_once(page)

    button.click.assert_not_called()
    _candidates.count.assert_not_called()


def test_submit_click_exception_is_fieldless_unknown_status_and_one_attempt(caplog) -> None:
    crawler, page, _candidates, button, _modals = _submit_fixture()
    secret = "PRIVATE-CLICK-DOM-987654"
    button.click.side_effect = RuntimeError(secret)

    with pytest.raises(UbotLoginError, match="送出狀態不明.*禁止自動重試") as error:
        crawler.submit_credentials_once(page)

    assert error.value.__cause__ is None
    assert secret not in str(error.value)
    assert secret not in caplog.text
    button.click.assert_called_once_with(timeout=8000)


def _refresh_fixture(count: int = 1):
    page = Mock()
    candidates = Mock()
    action = Mock()
    candidates.count.return_value = count
    candidates.nth.return_value = action
    action.is_visible.return_value = True
    action.is_enabled.return_value = True
    action.inner_text.return_value = "重新產生"
    page.locator.return_value = candidates
    return page, action


def test_ocr_reads_five_times_and_refreshes_at_most_four(monkeypatch, capsys) -> None:
    crawler = _crawler()
    crawler.captcha_tmp = Path("/tmp/ubot-captcha-test")
    page, refresh = _refresh_fixture()
    wait = Mock()
    solve = Mock(return_value="PRIVATE-OCR-MARKER")
    monkeypatch.setattr(ubot_module, "wait_captcha_stable", wait)
    monkeypatch.setattr(ubot_module, "solve_captcha", solve)

    assert crawler._ocr_with_regen(page, max_attempts=5) is None

    assert wait.call_count == solve.call_count == 5
    assert refresh.click.call_count == 4
    assert page.wait_for_timeout.call_args_list == [call(2000)] * 4
    assert "PRIVATE-OCR-MARKER" not in capsys.readouterr().err
    page.evaluate.assert_not_called()


def test_ocr_refresh_ambiguity_stops_without_click_or_submit(monkeypatch) -> None:
    crawler = _crawler()
    crawler.captcha_tmp = Path("/tmp/ubot-captcha-test")
    page, refresh = _refresh_fixture(count=2)
    wait = Mock()
    solve = Mock(return_value=None)
    monkeypatch.setattr(ubot_module, "wait_captcha_stable", wait)
    monkeypatch.setattr(ubot_module, "solve_captcha", solve)

    assert crawler._ocr_with_regen(page, max_attempts=5) is None

    assert wait.call_count == solve.call_count == 1
    refresh.click.assert_not_called()
    page.evaluate.assert_not_called()


def test_ocr_success_after_one_refresh(monkeypatch, capsys) -> None:
    crawler = _crawler()
    crawler.captcha_tmp = Path("/tmp/ubot-captcha-test")
    page, refresh = _refresh_fixture()
    solve = Mock(side_effect=[None, "123456"])
    monkeypatch.setattr(ubot_module, "wait_captcha_stable", Mock())
    monkeypatch.setattr(ubot_module, "solve_captcha", solve)

    assert crawler._ocr_with_regen(page, max_attempts=5) == "123456"
    assert solve.call_count == 2
    refresh.click.assert_called_once_with()
    assert "123456" not in capsys.readouterr().err


@pytest.mark.parametrize("failing_wait", [6000, 1000])
def test_post_submit_wait_exception_is_fieldless_after_one_click(
    failing_wait: int,
    caplog,
) -> None:
    crawler, page, _candidates, button, _modals = _submit_fixture()
    secret = f"PRIVATE-WAIT-{failing_wait}-987654"

    def wait(milliseconds: int) -> None:
        if milliseconds == failing_wait:
            raise RuntimeError(secret)

    page.wait_for_timeout.side_effect = wait

    crawler.submit_credentials_once(page)

    assert secret not in caplog.text
    button.click.assert_called_once_with(timeout=8000)
    page.click.assert_not_called()


def test_post_submit_inspection_exception_is_fieldless_after_one_click(caplog) -> None:
    crawler, page, _candidates, button, modals = _submit_fixture()
    secret = "PRIVATE-INSPECTION-DOM-987654"
    modals.count.side_effect = RuntimeError(secret)

    crawler.submit_credentials_once(page)

    assert secret not in caplog.text
    button.click.assert_called_once_with(timeout=8000)
    page.click.assert_not_called()


def test_post_submit_authentication_exception_is_fieldless_after_one_click(caplog) -> None:
    crawler, page, _candidates, button, _modals = _submit_fixture()
    secret = "PRIVATE-AUTH-DOM-987654"
    crawler._logged_in.side_effect = RuntimeError(secret)

    crawler.submit_credentials_once(page)

    assert secret not in caplog.text
    button.click.assert_called_once_with(timeout=8000)
    page.click.assert_not_called()


def test_real_submit_structural_wait_stops_at_multiple_modals_without_actions(monkeypatch) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(
            """
            <input id="sid"><input id="nickname"><input id="password"><input id="CAPTCHA">
            <button class="ubot-primary-green">登入</button>
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
        monkeypatch.setattr(crawler, "_ocr_with_regen", lambda *_args, **_kwargs: "654321")
        monkeypatch.setattr(crawler, "_logged_in", lambda _page: False)
        original_wait = page.wait_for_timeout

        def fast_wait(milliseconds: int) -> None:
            original_wait(1 if milliseconds >= 1000 else milliseconds)

        monkeypatch.setattr(page, "wait_for_timeout", fast_wait)

        crawler.submit_credentials_once(page)

        assert page.locator("body").get_attribute("data-submit") == "1"
        assert page.locator("body").get_attribute("data-modal") == "0"
        assert page.locator(".modal.show").count() == 2
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_password_expiry_route_remains_authenticated_and_prepare_does_not_reopen(
    monkeypatch,
) -> None:
    manager, browser = _launch_browser()
    try:
        real_page = browser.new_page()
        real_page.set_content(
            """
            <button>網銀登入</button>
            <main>您離上次變更密碼已超過6個月，建議變更密碼 <a>登出 logout</a></main>
            <script>
              document.body.dataset.clicks = '0';
              document.querySelector('button').onclick = () => document.body.dataset.clicks++;
              location.hash = '#/I1201001';
            </script>
            """
        )

        class PageProxy:
            url = "https://www.ubot.com.tw/ibank/#/I1201001"

            def __getattr__(self, name):
                return getattr(real_page, name)

        page = PageProxy()
        crawler = _crawler()
        assert crawler._logged_in(page) is True
        monkeypatch.setattr(real_page, "wait_for_timeout", lambda _milliseconds: None)

        crawler.prepare_login_page(page)

        assert page.locator("body").get_attribute("data-clicks") == "0"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_login_and_authentication_are_thin_adapters(monkeypatch) -> None:
    page = Mock()
    crawler = _crawler()
    shared = Mock(return_value=True)
    logged = Mock(return_value=True)
    monkeypatch.setattr(crawler, "_shared_login", shared)
    monkeypatch.setattr(crawler, "_logged_in", logged)

    assert crawler.login(page) is True
    assert crawler.is_authenticated(page) is True
    shared.assert_called_once_with(page)
    logged.assert_called_once_with(page)


def test_authentication_does_not_log_url_or_body_metadata(capsys) -> None:
    page = Mock()
    page.url = "https://www.ubot.com.tw/ibank/home"
    page.evaluate.return_value = {
        "ok": False,
        "url": "https://bank.invalid/PRIVATE-PATH",
        "txt_len": 987654,
        "hit": 1,
    }

    assert _crawler()._logged_in(page) is False
    captured = capsys.readouterr()
    assert "PRIVATE-PATH" not in captured.err
    assert "987654" not in captured.err


def test_authentication_requires_exact_ubot_host() -> None:
    page = Mock()
    page.evaluate.return_value = {"ok": True}
    crawler = _crawler()

    page.url = "https://www.ubot.com.tw/ibank/home"
    assert crawler._logged_in(page) is True
    page.url = "https://evil-ubot.com.tw/ibank/home"
    assert crawler._logged_in(page) is False
