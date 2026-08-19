from __future__ import annotations

import ast
import base64
import hashlib
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

import backend.banks.scb as scb_module
from backend.banks.scb import BASE, LOGIN_PATH_HINT, ScbCrawler, ScbLoginError
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointPhase,
    LoginCheckpointBlocked,
    evaluate_login_checkpoint,
)


def _crawler() -> ScbCrawler:
    crawler = object.__new__(ScbCrawler)
    crawler.name = "scb"
    crawler._credential_origin_allowed = lambda _page: True
    crawler.creds = SimpleNamespace(
        national_id="ID-PRIVATE",
        username="USER-PRIVATE",
        password="PASSWORD-PRIVATE",
    )
    return crawler


def test_shared_api_and_terminal_first_rule_contract() -> None:
    rules = _crawler().login_checkpoint_rules()

    assert ScbCrawler.USES_SHARED_LOGIN_CHECKPOINTS is True
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
        CheckpointKind.DUPLICATE_SESSION,
        CheckpointKind.DUPLICATE_SESSION,
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
        ".modal.show",
        "[role='dialog']",
        "[name='__reCaptcha']",
    ]
    assert all(rule.bank == "scb" for rule in rules)
    assert rules[4].phases == (
        CheckpointPhase.POST_SUBMIT,
        CheckpointPhase.POST_SUBMIT_SETTLE,
    )
    assert rules[7].phases == (CheckpointPhase.POST_SUBMIT,)
    assert rules[10].action_texts == ("確定登入",)
    assert rules[10].max_actions == 1
    duplicate_pattern = rules[10].required_body_pattern
    assert duplicate_pattern is not None
    assert duplicate_pattern.fullmatch(
        "您可能先前未正常登出或已經在別台裝置登入，其他裝置將會被登出確定登入"
    )
    assert not duplicate_pattern.search("您可能先前未正常登出，任意私密內容，確定登入")
    assert all(rule.action_texts == () for rule in (*rules[:10], *rules[12:]))


@pytest.mark.parametrize(
    ("index", "positive", "negative"),
    (
        (0, "請輸入OTP", "圖形驗證碼錯誤"),
        (0, "新裝置登入需要信任此裝置", "captcha 驗證碼錯誤"),
        (2, "請立即修改您的密碼", "可以稍後修改密碼"),
        (2, "密碼已到期", "密碼輸入不正確"),
        (7, "CAPT001: 驗證碼錯誤", "XCAPT001Y"),
        (4, "HIBERR_000010 系統忙線", "CAPT001: 驗證碼錯誤"),
        (4, "E1234: 登入失敗", "一般安全提醒"),
    ),
)
def test_rule_patterns_are_bounded(index: int, positive: str, negative: str) -> None:
    pattern = _crawler().login_checkpoint_rules()[index].required_body_pattern
    assert pattern is not None
    assert pattern.fullmatch(positive)
    assert not pattern.search(negative)


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
        bank="scb",
        phase=phase,
        rules=crawler.login_checkpoint_rules(),
        is_authenticated=lambda _page: False,
    )


def test_real_captcha_classifier_and_generic_captcha_never_click() -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content('<div class="alert">CAPT001: 驗證碼錯誤</div>')
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.kind is CheckpointKind.CAPTCHA_RETRY
        assert page.locator("button").count() == 0

        page.set_content(
            '<div class="alert">圖形驗證碼錯誤<button>重新產生</button></div>'
            "<script>document.body.dataset.clicks='0';document.querySelector('button').onclick=()=>document.body.dataset.clicks++</script>"
        )
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
        assert page.locator("body").get_attribute("data-clicks") == "0"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


@pytest.mark.parametrize("attrs", ('class="modal show"', 'role="dialog"'))
@pytest.mark.parametrize(
    ("body", "kind"),
    (
        ("您可能先前未正常登出，請輸入OTP後確定登入", CheckpointKind.OTP_REQUIRED),
        ("已經在別台裝置登入，請立即修改密碼後確定登入", CheckpointKind.PASSWORD_CHANGE_REQUIRED),
    ),
)
def test_terminal_collision_has_zero_click(attrs: str, body: str, kind: CheckpointKind) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(
            f'<div {attrs}>{body}<button>確定登入</button></div>'
            "<script>document.body.dataset.clicks='0';document.querySelector('button').onclick=()=>document.body.dataset.clicks++</script>"
        )
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.kind is kind
        assert page.locator("body").get_attribute("data-clicks") == "0"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_real_duplicate_clicks_exact_action_once_and_near_miss_never_clicks() -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(
            """
            <div class="modal show">您可能先前未正常登出或已經在別台裝置登入，其他裝置將會被登出<button>確定登入</button></div>
            <script>document.body.dataset.clicks='0';document.querySelector('button').onclick=e=>{document.body.dataset.clicks++;e.target.closest('div').hidden=true}</script>
            """
        )
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.kind is CheckpointKind.DUPLICATE_SESSION
        assert outcome.action_label == "確定登入"
        assert page.locator("body").get_attribute("data-clicks") == "1"

        for label in ("確定", "繼續登入"):
            page.set_content(
                f'<div class="modal show">您可能先前未正常登出<button>{label}</button></div>'
                "<script>document.body.dataset.clicks='0';document.querySelector('button').onclick=()=>document.body.dataset.clicks++</script>"
            )
            assert _evaluate(page, CheckpointPhase.POST_SUBMIT).kind is CheckpointKind.UNKNOWN_BLOCKER
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


def test_auth_requires_exact_host_path_identity_and_rejects_child_login_controls() -> None:
    manager, browser = _launch_browser()
    try:
        real = browser.new_page()
        crawler = _crawler()
        dashboard = "登出 理財總覽 " + "x" * 600
        real.set_content(f"<main>{dashboard}</main>")
        assert crawler._logged_in(_page_proxy(real, "https://ebank.standardchartered.com.tw/scb/"))
        assert not crawler._logged_in(_page_proxy(real, "https://evil.example/scb/"))
        assert not crawler._logged_in(_page_proxy(real, f"https://ebank.standardchartered.com.tw{LOGIN_PATH_HINT}"))

        real.set_content("<main>存款 信用卡 基金 投資 " + "x" * 600 + "</main>")
        assert not crawler._logged_in(_page_proxy(real, "https://ebank.standardchartered.com.tw/scb/"))

        real.set_content(f"<main>{dashboard}</main><iframe srcdoc=\"<input type='password'>\"></iframe>")
        real.wait_for_timeout(100)
        assert not crawler._logged_in(_page_proxy(real, "https://ebank.standardchartered.com.tw/scb/"))
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_auth_is_one_shot_exception_safe_and_fieldless(caplog) -> None:
    page = Mock()
    page.url = "https://ebank.standardchartered.com.tw/PRIVATE-PATH"
    page.frames = []
    page.main_frame = Mock()
    page.locator.side_effect = RuntimeError("PRIVATE-DOM-987654")
    assert _crawler()._logged_in(page) is False
    page.wait_for_timeout.assert_not_called()
    assert "PRIVATE" not in caplog.text


def test_prepare_probes_dashboard_then_navigates_login_and_is_fieldless(caplog) -> None:
    crawler = _crawler()
    crawler._logged_in = Mock(return_value=False)
    page = Mock()
    page.url = "https://elsewhere.example/"
    crawler.prepare_login_page(page)
    assert page.mock_calls == [
        call.goto("https://ebank.standardchartered.com.tw/scb/", timeout=15000),
        call.wait_for_timeout(5000),
        call.goto(BASE, timeout=15000),
        call.wait_for_timeout(8000),
    ]

    foreign = Mock()
    foreign.url = "https://evil.example/scb/public/login"
    crawler.prepare_login_page(foreign)
    assert foreign.mock_calls == [
        call.goto("https://ebank.standardchartered.com.tw/scb/", timeout=15000),
        call.wait_for_timeout(5000),
        call.goto(BASE, timeout=15000),
        call.wait_for_timeout(8000),
    ]

    page = Mock()
    page.goto.side_effect = [RuntimeError("PRIVATE-URL-987654"), RuntimeError("PRIVATE-BODY-987654")]
    page.url = "https://elsewhere.example/"
    with pytest.raises(ScbLoginError, match="未送出登入") as error:
        crawler.prepare_login_page(page)
    assert error.value.__cause__ is None
    assert "PRIVATE" not in str(error.value)
    assert "PRIVATE" not in caplog.text


def _submit_fixture():
    page = Mock()
    page.frames = []
    page.main_frame = Mock()
    fields = []
    specs = [
        (10, "text", "dynamic-id", "ID-PRIVATE"),
        (20, "password", "dynamic-user", "USER-PRIVATE"),
        (30, "password", "dynamic-password", "PASSWORD-PRIVATE"),
        (40, "tel", "__reCaptcha", "123456"),
    ]
    inputs = Mock()
    inputs.count.return_value = 4
    for y, field_type, name, value in specs:
        field = Mock()
        field.is_visible.return_value = True
        field.is_enabled.return_value = True
        field.bounding_box.return_value = {"x": 0, "y": y, "width": 100, "height": 20}
        field.get_attribute.side_effect = lambda attr, t=field_type, n=name: {
            "type": t,
            "name": n,
            "maxlength": "6" if t == "tel" else "12",
        }.get(attr)
        field.input_value.return_value = value
        fields.append(field)
    inputs.nth.side_effect = fields.__getitem__

    empty = Mock()
    empty.count.return_value = 0
    submits = Mock()
    submit = Mock()
    submits.count.return_value = 1
    submits.nth.return_value = submit
    submit.is_visible.return_value = True
    submit.is_enabled.return_value = True
    submit.inner_text.return_value = "登入"
    submit.get_attribute.return_value = "m-button b-bg-green-d b-block"
    page.locator.side_effect = lambda selector: {
        "input": inputs,
        "button[type='submit']": submits,
        ".error": empty,
        ".alert": empty,
        "[role='alert']": empty,
        ".modal.show": empty,
        "[role='dialog']": empty,
        "[name='__reCaptcha']": empty,
    }[selector]
    crawler = _crawler()
    crawler._ocr_captcha = Mock(return_value="123456")
    crawler._logged_in = Mock(return_value=True)
    return crawler, page, fields, submit, submits


def test_submit_uses_real_keyboard_sequence_lengths_and_one_native_click() -> None:
    crawler, page, fields, submit, _submits = _submit_fixture()
    crawler.submit_credentials_once(page)

    for field in fields:
        assert field.click.call_args_list == [call(), call(click_count=3)]
        field.input_value.assert_called_once_with()
    assert page.keyboard.press.call_args_list == [call("Backspace")] * 4
    assert page.keyboard.type.call_args_list == [
        call("ID-PRIVATE", delay=80),
        call("USER-PRIVATE", delay=80),
        call("PASSWORD-PRIVATE", delay=80),
        call("123456", delay=80),
    ]
    submit.click.assert_called_once_with(timeout=8000)
    page.evaluate.assert_not_called()


@pytest.mark.parametrize("failure", ("count", "type", "captcha-name", "hidden", "disabled", "length", "submit-count"))
def test_submit_pre_click_failures_are_fieldless_and_zero_submit(failure: str, caplog) -> None:
    crawler, page, fields, submit, submits = _submit_fixture()
    inputs = page.locator("input")
    if failure == "count":
        inputs.count.return_value = 3
    elif failure == "type":
        fields[1].get_attribute.side_effect = lambda attr: {"type": "text", "name": "u", "maxlength": "12"}.get(attr)
    elif failure == "captcha-name":
        fields[3].get_attribute.side_effect = lambda attr: {"type": "tel", "name": "wrong", "maxlength": "6"}.get(attr)
    elif failure == "hidden":
        fields[1].is_visible.return_value = False
    elif failure == "disabled":
        fields[1].is_enabled.return_value = False
    elif failure == "length":
        fields[2].input_value.return_value = "short"
    else:
        submits.count.return_value = 2

    with pytest.raises(ScbLoginError, match="未送出登入") as error:
        crawler.submit_credentials_once(page)
    assert error.value.__cause__ is None
    assert "PRIVATE" not in str(error.value)
    assert "PRIVATE" not in caplog.text
    submit.click.assert_not_called()


def test_submit_click_error_is_unknown_and_never_retried(caplog) -> None:
    crawler, page, _fields, submit, _submits = _submit_fixture()
    submit.click.side_effect = RuntimeError("PRIVATE-CLICK-987654")
    with pytest.raises(ScbLoginError, match="送出狀態不明；禁止自動重試") as error:
        crawler.submit_credentials_once(page)
    assert error.value.__cause__ is None
    assert "PRIVATE" not in str(error.value)
    assert "PRIVATE" not in caplog.text
    submit.click.assert_called_once_with(timeout=8000)


def test_post_click_inspection_error_is_fieldless_and_never_retried(
    caplog,
    monkeypatch,
) -> None:
    crawler, page, _fields, submit, _submits = _submit_fixture()
    monkeypatch.setattr(
        crawler,
        "_logged_in",
        Mock(side_effect=RuntimeError("PRIVATE-INSPECTION-987654")),
    )

    with pytest.raises(ScbLoginError, match="狀態無法安全確認；禁止自動重試") as error:
        crawler.submit_credentials_once(page)

    assert error.value.__cause__ is None
    assert "PRIVATE" not in str(error.value)
    assert "PRIVATE" not in caplog.text
    submit.click.assert_called_once_with(timeout=8000)


def test_stale_capt_dom_does_not_refresh_during_ordinary_submit() -> None:
    crawler, page, _fields, submit, _submits = _submit_fixture()
    alert = Mock()
    alert.is_visible.return_value = True
    alert.inner_text.return_value = "CAPT001: 驗證碼錯誤"
    alerts = Mock()
    alerts.count.return_value = 1
    alerts.nth.return_value = alert
    refresh = Mock()
    actions = Mock()
    actions.count.return_value = 1
    actions.nth.return_value = refresh
    original_locator = page.locator.side_effect
    page.locator.side_effect = (
        lambda selector: actions
        if selector == "button, a"
        else alerts
        if selector == ".alert"
        else original_locator(selector)
    )

    crawler.submit_credentials_once(page)

    refresh.click.assert_not_called()
    crawler._ocr_captcha.assert_called_once_with(page, max_attempts=5)
    submit.click.assert_called_once_with(timeout=8000)


def test_authorized_retry_hook_refreshes_once_and_requires_stale_alert_clear() -> None:
    crawler, page, _fields, _submit, _submits = _submit_fixture()
    alert = Mock()
    alert.is_visible.side_effect = [False]
    alert.inner_text.return_value = "CAPT001: 驗證碼錯誤"
    alerts = Mock()
    alerts.count.return_value = 1
    alerts.nth.return_value = alert
    refresh = Mock()
    refresh.is_visible.return_value = True
    refresh.is_enabled.return_value = True
    refresh.inner_text.return_value = "重新產生"
    actions = Mock()
    actions.count.return_value = 1
    actions.nth.return_value = refresh
    original_locator = page.locator.side_effect
    page.locator.side_effect = (
        lambda selector: actions
        if selector == "button, a"
        else alerts
        if selector == ".alert"
        else original_locator(selector)
    )

    crawler.prepare_captcha_resubmit(page)

    refresh.click.assert_called_once_with()
    assert page.wait_for_timeout.call_args_list == [call(1500)]


def test_ocr_reads_five_times_and_refreshes_at_most_four_natively(monkeypatch) -> None:
    crawler = _crawler()
    page = Mock()
    page.frames = []
    page.main_frame = Mock()
    page.evaluate.return_value = "data:image/jpeg;base64," + base64.b64encode(b"captcha").decode()
    monkeypatch.setattr(scb_module, "ocr_bytes", lambda *_args, **_kwargs: None)
    refresh = Mock()
    refresh.is_visible.return_value = True
    refresh.is_enabled.return_value = True
    refresh.inner_text.return_value = "重新產生"
    actions = Mock()
    actions.count.return_value = 1
    actions.nth.return_value = refresh
    page.locator.return_value = actions

    assert crawler._ocr_captcha(page, max_attempts=5) is None
    assert page.evaluate.call_count == 5
    assert refresh.click.call_count == 4
    assert page.wait_for_timeout.call_args_list == [call(1500)] * 4


def test_login_is_thin_and_delayed_second_capt_blocks_before_third_submission(
    monkeypatch,
) -> None:
    crawler = _crawler()
    page = Mock()
    shared = Mock(return_value=True)
    monkeypatch.setattr(crawler, "_shared_login", shared)
    assert crawler.login(page)
    shared.assert_called_once_with(page)

    manager, browser = _launch_browser()
    try:
        real = browser.new_page()
        real.set_content(
            """
            <style>input { display:block; margin:10px; }</style>
            <input type="text" name="dynamic-id">
            <input type="password" name="dynamic-user">
            <input type="password" name="dynamic-password">
            <input type="tel" name="__reCaptcha">
            <button type="button" id="refresh">重新產生</button>
            <button type="submit" class="m-button b-bg-green-d b-block" id="login">登入</button>
            <div class="alert" hidden>CAPT001: 驗證碼錯誤</div>
            <script>
              document.body.dataset.submissions = '0';
              document.body.dataset.refreshes = '0';
              const alert = document.querySelector('.alert');
              refresh.onclick = () => {
                document.body.dataset.refreshes++;
                alert.hidden = true;
              };
              login.onclick = event => {
                event.preventDefault();
                document.body.dataset.submissions++;
                setTimeout(() => alert.hidden = false, 100);
              };
            </script>
            """
        )
        crawler = _crawler()
        crawler._ocr_captcha = Mock(return_value="123456")
        collect = Mock()
        monkeypatch.setattr(crawler, "prepare_login_page", lambda _page: None)
        monkeypatch.setattr(crawler, "is_authenticated", lambda _page: False)
        monkeypatch.setattr(crawler, "_logged_in", lambda _page: False)
        monkeypatch.setattr(crawler, "collect", collect)

        with pytest.raises(LoginCheckpointBlocked):
            crawler._shared_login(real)

        assert real.locator("body").get_attribute("data-submissions") == "2"
        assert real.locator("body").get_attribute("data-refreshes") == "1"
        collect.assert_not_called()
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_stale_capt_plus_delayed_explicit_error_never_refreshes_or_resubmits(
    monkeypatch,
) -> None:
    manager, browser = _launch_browser()
    try:
        real = browser.new_page()
        real.set_content(
            """
            <style>input { display:block; margin:10px; }</style>
            <input type="text" name="dynamic-id">
            <input type="password" name="dynamic-user">
            <input type="password" name="dynamic-password">
            <input type="tel" name="__reCaptcha">
            <button type="button" id="refresh">重新產生</button>
            <button type="submit" class="m-button b-bg-green-d b-block" id="login">登入</button>
            <div class="alert">CAPT001: 舊驗證碼錯誤</div>
            <div class="error" hidden>E007: 密碼不正確</div>
            <script>
              document.body.dataset.submissions = '0';
              document.body.dataset.refreshes = '0';
              refresh.onclick = () => document.body.dataset.refreshes++;
              login.onclick = event => {
                event.preventDefault();
                document.body.dataset.submissions++;
                setTimeout(() => document.querySelector('.error').hidden = false, 100);
              };
            </script>
            """
        )
        crawler = _crawler()
        crawler._ocr_captcha = Mock(return_value="123456")
        collect = Mock()
        monkeypatch.setattr(crawler, "prepare_login_page", lambda _page: None)
        monkeypatch.setattr(crawler, "is_authenticated", lambda _page: False)
        monkeypatch.setattr(crawler, "_logged_in", lambda _page: False)
        monkeypatch.setattr(crawler, "collect", collect)

        with pytest.raises(LoginCheckpointBlocked) as error:
            crawler._shared_login(real)

        assert error.value.outcome.kind is CheckpointKind.EXPLICIT_LOGIN_ERROR
        assert real.locator("body").get_attribute("data-submissions") == "1"
        assert real.locator("body").get_attribute("data-refreshes") == "0"
        collect.assert_not_called()
    finally:
        browser.close()
        manager.__exit__(None, None, None)


@pytest.mark.parametrize("stale_captcha", (True, False))
def test_captcha_plus_delayed_duplicate_never_refreshes_or_resubmits(
    stale_captcha: bool,
    monkeypatch,
) -> None:
    manager, browser = _launch_browser()
    try:
        real = browser.new_page()
        captcha_hidden = "" if stale_captcha else "hidden"
        real.set_content(
            f"""
            <style>input {{ display:block; margin:10px; }}</style>
            <input type="text" name="dynamic-id">
            <input type="password" name="dynamic-user">
            <input type="password" name="dynamic-password">
            <input type="tel" name="__reCaptcha">
            <button type="button" id="refresh">重新產生</button>
            <button type="submit" class="m-button b-bg-green-d b-block" id="login">登入</button>
            <div class="alert" {captcha_hidden}>CAPT001: 驗證碼錯誤</div>
            <div class="modal show" hidden id="duplicate">您可能先前未正常登出<button>確定登入</button></div>
            <script>
              document.body.dataset.submissions = '0';
              document.body.dataset.refreshes = '0';
              document.body.dataset.duplicates = '0';
              const captchaAlert = document.querySelector('.alert');
              refresh.onclick = () => document.body.dataset.refreshes++;
              duplicate.querySelector('button').onclick = () => document.body.dataset.duplicates++;
              login.onclick = event => {{
                event.preventDefault();
                document.body.dataset.submissions++;
                setTimeout(() => {{
                  captchaAlert.hidden = false;
                  duplicate.hidden = false;
                }}, 100);
              }};
            </script>
            """
        )
        crawler = _crawler()
        crawler._ocr_captcha = Mock(return_value="123456")
        collect = Mock()
        monkeypatch.setattr(crawler, "prepare_login_page", lambda _page: None)
        monkeypatch.setattr(crawler, "is_authenticated", lambda _page: False)
        monkeypatch.setattr(crawler, "_logged_in", lambda _page: False)
        monkeypatch.setattr(crawler, "collect", collect)

        with pytest.raises(ScbLoginError, match="禁止自動重試"):
            crawler._shared_login(real)

        assert real.locator("body").get_attribute("data-submissions") == "1"
        assert real.locator("body").get_attribute("data-refreshes") == "0"
        assert real.locator("body").get_attribute("data-duplicates") == "0"
        collect.assert_not_called()
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_unknown_private_modal_blocks_without_action_or_submit() -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(
            '<div class="modal show">PRIVATE-BODY-987654<button>確定</button></div>'
            "<script>document.body.dataset.clicks='0';document.querySelector('button').onclick=()=>document.body.dataset.clicks++</script>"
        )
        crawler = _crawler()
        crawler.prepare_login_page = lambda _page: None
        crawler.is_authenticated = lambda _page: False
        crawler.submit_credentials_once = Mock()
        with pytest.raises(LoginCheckpointBlocked) as error:
            crawler._shared_login(page)
        assert "PRIVATE" not in str(error.value)
        crawler.submit_credentials_once.assert_not_called()
        assert page.locator("body").get_attribute("data-clicks") == "0"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_legacy_login_debug_and_synthetic_actions_are_absent() -> None:
    source = inspect.getsource(scb_module)
    login_region = source[: source.index("    def collect(")]
    for forbidden in (
        "_captcha_retry_used",
        "_login_snapshot",
        "page.screenshot",
        "captcha={cap_text}",
        "確定' || t === '繼續登入",
        "btn.click()",
        "preview:",
    ):
        assert forbidden not in login_region


def test_collect_and_following_helpers_keep_protected_ast_contract() -> None:
    tree = ast.parse(Path(scb_module.__file__).read_text())
    crawler = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ScbCrawler")
    start = next(i for i, node in enumerate(crawler.body) if isinstance(node, ast.FunctionDef) and node.name == "collect")
    payload = "\n".join(ast.dump(node, include_attributes=False) for node in crawler.body[start:])
    assert hashlib.sha256(payload.encode()).hexdigest() == "ecdd1ba52307cc48f7be0ec46556e30bb0cbec61182867e76830483cba62aaa0"
