from __future__ import annotations

import ast
import hashlib
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, call

import pytest

import backend.banks.hsbc as hsbc_module
from backend.banks.hsbc import HsbcCrawler, HsbcLoginError
from backend.core.base import BankCrawler
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointOutcome,
    CheckpointPhase,
    evaluate_login_checkpoint,
)


def _crawler() -> HsbcCrawler:
    crawler = object.__new__(HsbcCrawler)
    crawler.name = "hsbc"
    crawler._credential_origin_allowed = lambda _page: True
    crawler.creds = cast("object", SimpleNamespace(
        user_id="USER-PRIVATE", password="PASSWORD-PRIVATE"
    ))
    return crawler


def _launch_browser():
    from patchright.sync_api import sync_playwright

    manager = sync_playwright()
    patchright = manager.start()
    if not Path(patchright.chromium.executable_path).exists():
        manager.__exit__(None, None, None)
        pytest.skip("Patchright browser binary is not installed")
    return manager, patchright.chromium.launch(headless=True)


def _page_proxy(real_page, url: str):
    class Proxy:
        def __getattr__(self, name):
            return getattr(real_page, name)

    proxy = Proxy()
    proxy.url = url
    return proxy


def _evaluate(page, phase: CheckpointPhase):
    crawler = _crawler()
    return evaluate_login_checkpoint(
        page,
        bank="hsbc",
        phase=phase,
        rules=crawler.login_checkpoint_rules(),
        is_authenticated=lambda _page: False,
    )


def test_shared_api_and_terminal_first_rule_contract(monkeypatch) -> None:
    crawler = _crawler()
    shared = Mock(return_value=True)
    monkeypatch.setattr(crawler, "_shared_login", shared)
    page = Mock()

    assert HsbcCrawler.USES_SHARED_LOGIN_CHECKPOINTS is True
    assert crawler.login(page) is True
    shared.assert_called_once_with(page)

    rules = crawler.login_checkpoint_rules()
    assert [rule.kind for rule in rules] == [
        CheckpointKind.OTP_REQUIRED,
        CheckpointKind.OTP_REQUIRED,
        CheckpointKind.PASSWORD_CHANGE_REQUIRED,
        CheckpointKind.PASSWORD_CHANGE_REQUIRED,
        CheckpointKind.EXPLICIT_LOGIN_ERROR,
        CheckpointKind.EXPLICIT_LOGIN_ERROR,
        CheckpointKind.EXPLICIT_LOGIN_ERROR,
        CheckpointKind.DISMISSIBLE_NOTICE,
        CheckpointKind.UNKNOWN_BLOCKER,
        CheckpointKind.UNKNOWN_BLOCKER,
        CheckpointKind.UNKNOWN_BLOCKER,
        CheckpointKind.UNKNOWN_BLOCKER,
        CheckpointKind.UNKNOWN_BLOCKER,
    ]
    assert all(rule.bank == "hsbc" for rule in rules)
    assert rules[7].name == "hsbc-security-notice"
    assert rules[7].action_texts == ("繼續",)
    assert all(rule.action_texts == () for rule in (*rules[:7], *rules[8:]))
    assert rules[4].phases == (
        CheckpointPhase.POST_SUBMIT,
        CheckpointPhase.POST_SUBMIT_SETTLE,
    )
    assert [rule.container_selector for rule in rules[-5:]] == [
        ".modal.show", "[role='dialog']", "#userId", "#password", "#captchaInput"
    ]


def test_security_notice_is_exact_scoped_and_clicks_once() -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(
            '<button id="outside">繼續</button>'
            '<div role="dialog">資訊安全提醒：請妥善保管密碼<button>繼續</button></div>'
            "<script>document.body.dataset.inside='0';document.body.dataset.outside='0';"
            "outside.onclick=()=>document.body.dataset.outside++;"
            "document.querySelector('[role=dialog] button').onclick=()=>{document.body.dataset.inside++;document.querySelector('[role=dialog]').hidden=true}</script>"
        )
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.kind is CheckpointKind.DISMISSIBLE_NOTICE
        assert outcome.rule_name == "hsbc-security-notice"
        assert page.locator("body").get_attribute("data-inside") == "1"
        assert page.locator("body").get_attribute("data-outside") == "0"

        page.set_content(
            '<div role="dialog">資訊安全：偵測到異常登入，請確認是否本人<button>繼續</button></div>'
            "<script>document.body.dataset.clicks='0';document.querySelector('button').onclick=()=>document.body.dataset.clicks++</script>"
        )
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
        assert page.locator("body").get_attribute("data-clicks") == "0"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


@pytest.mark.parametrize(
    ("index", "positive", "negative"),
    (
        (0, "請輸入OTP", "圖形驗證碼錯誤"),
        (0, "新裝置登入需要信任此裝置", "一般驗證碼"),
        (2, "請立即修改您的密碼", "可以稍後修改密碼"),
        (2, "密碼已過期", "密碼輸入錯誤，圖形驗證碼已過期"),
        (4, "密碼不正確。", "密碼不正確原因說明"),
        (4, "帳號已遭鎖定！", "帳號未鎖定"),
        (4, "登入失敗", "若登入失敗，請確認網路連線"),
        (4, "驗證碼錯誤，請重新輸入。", "驗證碼錯誤排解指南"),
        (4, "Invalid credentials!", "Invalid credentials troubleshooting"),
        (4, "Account locked.", "Account locked help"),
    ),
)
def test_rule_patterns_are_closed(index: int, positive: str, negative: str) -> None:
    pattern = _crawler().login_checkpoint_rules()[index].required_body_pattern
    assert pattern is not None
    assert pattern.fullmatch(positive)
    assert not pattern.search(negative)


def test_prepare_waits_only_and_auth_short_circuits() -> None:
    crawler = _crawler()
    crawler._logged_in = Mock(return_value=True)
    page = Mock()

    crawler.prepare_login_page(page)

    assert page.mock_calls == [call.wait_for_timeout(7000)]
    crawler._logged_in.assert_called_once_with(page)


def test_auth_exact_host_hash_identity_controls_public_menu_and_exception(caplog) -> None:
    manager, browser = _launch_browser()
    try:
        real = browser.new_page()
        crawler = _crawler()
        dashboard = "登出 我的卡片 " + "x" * 300
        real.set_content(f"<main>{dashboard}</main>")
        assert crawler._logged_in(_page_proxy(real, "https://card.hsbc.com.tw/#/dashboard"))
        assert crawler._logged_in(_page_proxy(real, "https://card.hsbc.com.tw/#cards"))
        assert not crawler._logged_in(_page_proxy(real, "https://evil.hsbc.com.tw/#/dashboard"))
        assert not crawler._logged_in(_page_proxy(real, "http://card.hsbc.com.tw/#/dashboard"))
        assert not crawler._logged_in(_page_proxy(real, "https://card.hsbc.com.tw:444/#/dashboard"))
        assert not crawler._logged_in(_page_proxy(real, "https://card.hsbc.com.tw/#/login"))
        assert not crawler._logged_in(_page_proxy(real, "https://card.hsbc.com.tw/#/login/device"))

        real.set_content("<main>登出 信用卡 " + "x" * 300 + "</main>")
        assert crawler._logged_in(_page_proxy(real, "https://card.hsbc.com.tw/#/u/dashboard"))
        real.set_content("<main>帳單 選單 繳款 Statement Menu " + "x" * 300 + "</main>")
        assert not crawler._logged_in(_page_proxy(real, "https://card.hsbc.com.tw/#/dashboard"))
        real.set_content(f"<main>{dashboard}</main><input id='password'>")
        assert not crawler._logged_in(_page_proxy(real, "https://card.hsbc.com.tw/#/dashboard"))
    finally:
        browser.close()
        manager.__exit__(None, None, None)

    page = Mock()
    page.url = "https://card.hsbc.com.tw/#PRIVATE"
    page.locator.side_effect = RuntimeError("PRIVATE-DOM-987654")
    assert crawler._logged_in(page) is False
    page.wait_for_timeout.assert_not_called()
    page.evaluate.assert_not_called()
    assert "PRIVATE" not in caplog.text


@pytest.mark.parametrize("attrs", ('class="modal show"', 'role="dialog"'))
@pytest.mark.parametrize(
    ("body", "kind"),
    (
        ("請輸入簡訊驗證碼後繼續", CheckpointKind.OTP_REQUIRED),
        ("新裝置登入，請信任此裝置", CheckpointKind.OTP_REQUIRED),
        ("請立即修改您的密碼", CheckpointKind.PASSWORD_CHANGE_REQUIRED),
    ),
)
def test_real_terminal_collisions_have_zero_actions(
    attrs: str, body: str, kind: CheckpointKind
) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(
            f'<div {attrs}>{body}<button>繼續</button></div>'
            "<script>document.body.dataset.clicks='0';"
            "document.querySelector('button').onclick=()=>document.body.dataset.clicks++</script>"
        )
        assert _evaluate(page, CheckpointPhase.POST_SUBMIT).kind is kind
        assert page.locator("body").get_attribute("data-clicks") == "0"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


@pytest.mark.parametrize(
    "html",
    (
        '<div class="modal show">圖形驗證碼<button>繼續</button></div>',
        '<div role="dialog">密碼輸入錯誤，圖形驗證碼已過期<button>繼續</button></div>',
        '<div class="alert">若登入失敗，請確認網路連線</div>',
        '<div class="error">帳號未鎖定</div>',
    ),
)
def test_real_semantic_near_misses_are_unknown_and_never_click(html: str) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(
            html + "<script>document.body.dataset.clicks='0';"
            "for(const b of document.querySelectorAll('button'))b.onclick=()=>document.body.dataset.clicks++</script>"
        )
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
        assert page.locator("body").get_attribute("data-clicks") == "0"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def _collection(*nodes):
    result = Mock()
    result.count.return_value = len(nodes)
    result.nth.side_effect = nodes.__getitem__
    return result


def _submit_fixture(*, with_user: bool = True):
    page = Mock()
    user = Mock()
    password = Mock()
    captcha = Mock()
    first = Mock()
    final_wrong = Mock()
    final = Mock()
    for field, value in (
        (user, "USER-PRIVATE"),
        (password, "PASSWORD-PRIVATE"),
        (captcha, "ab12C"),
    ):
        field.is_visible.return_value = True
        field.is_enabled.return_value = True
        field.input_value.return_value = value
    first.is_visible.return_value = True
    first.is_enabled.return_value = True
    first.inner_text.return_value = " 繼續 "
    final_wrong.is_visible.return_value = True
    final_wrong.is_enabled.return_value = True
    final_wrong.inner_text.return_value = "登入"
    final.is_visible.return_value = True
    final.is_enabled.return_value = True
    final.inner_text.return_value = "繼續"
    empty = _collection()
    selectors = {
        "#userId": _collection(user) if with_user else empty,
        "button[data-testid='continueButton']": _collection(first),
        "#password": _collection(password),
        "#captchaInput": _collection(captcha),
        "button[type='submit']": _collection(final_wrong, final),
        ".modal.show": empty,
        "[role='dialog']": empty,
        ".error": empty,
        ".alert": empty,
        "[role='alert']": empty,
    }
    page.locator.side_effect = selectors.__getitem__
    crawler = _crawler()
    crawler._solve_captcha = Mock(return_value="ab12C")
    crawler._logged_in = Mock(return_value=True)
    return crawler, page, (user, password, captcha), first, final, selectors


def test_submit_two_stage_true_keyboard_exact_buttons_and_one_final_click() -> None:
    crawler, page, fields, first, final, _selectors = _submit_fixture()

    crawler.submit_credentials_once(page)

    for field in fields:
        assert field.click.call_args_list == [call(), call(click_count=3)]
        field.input_value.assert_called_once_with()
    assert page.keyboard.press.call_args_list == [call("Backspace")] * 3
    assert page.keyboard.type.call_args_list == [
        call("USER-PRIVATE", delay=80),
        call("PASSWORD-PRIVATE", delay=80),
        call("ab12C", delay=80),
    ]
    first.click.assert_called_once_with(timeout=8000)
    final.click.assert_called_once_with(timeout=8000)
    page.evaluate.assert_not_called()


def test_submit_skips_absent_user_challenge_but_still_submits_final_once() -> None:
    crawler, page, (user, _password, _captcha), first, final, _ = _submit_fixture(
        with_user=False
    )
    crawler.submit_credentials_once(page)
    user.click.assert_not_called()
    first.click.assert_not_called()
    final.click.assert_called_once_with(timeout=8000)


@pytest.mark.parametrize(
    "failure",
    ("user-ambiguous", "user-disabled", "first-label", "first-ambiguous", "password-hidden",
     "password-length", "captcha-disabled", "captcha-length", "final-ambiguous", "final-label"),
)
def test_submit_pre_click_failures_are_fieldless_and_no_final_submit(
    failure: str, caplog
) -> None:
    crawler, page, fields, first, final, selectors = _submit_fixture()
    user, password, captcha = fields
    if failure == "user-ambiguous":
        selectors["#userId"] = _collection(user, Mock())
    elif failure == "user-disabled":
        user.is_enabled.return_value = False
    elif failure == "first-label":
        first.inner_text.return_value = "登入"
    elif failure == "first-ambiguous":
        other = Mock(is_visible=Mock(return_value=True), is_enabled=Mock(return_value=True))
        other.inner_text.return_value = "繼續"
        selectors["button[data-testid='continueButton']"] = _collection(first, other)
    elif failure == "password-hidden":
        password.is_visible.return_value = False
    elif failure == "password-length":
        password.input_value.return_value = "short"
    elif failure == "captcha-disabled":
        captcha.is_enabled.return_value = False
    elif failure == "captcha-length":
        captcha.input_value.return_value = "bad"
    elif failure == "final-ambiguous":
        other = Mock()
        other.is_visible.return_value = other.is_enabled.return_value = True
        other.inner_text.return_value = "繼續"
        selectors["button[type='submit']"] = _collection(final, other)
    else:
        final.inner_text.return_value = "登入"

    with pytest.raises(HsbcLoginError, match="未送出登入") as error:
        crawler.submit_credentials_once(page)
    assert error.value.__cause__ is None
    assert "PRIVATE" not in str(error.value)
    assert "PRIVATE" not in caplog.text
    final.click.assert_not_called()


def test_first_stage_exception_never_attempts_final_submit(caplog) -> None:
    crawler, page, _fields, first, final, _ = _submit_fixture()
    first.click.side_effect = RuntimeError("PRIVATE-FIRST-987654")
    with pytest.raises(HsbcLoginError, match="未送出登入") as error:
        crawler.submit_credentials_once(page)
    assert error.value.__cause__ is None
    assert "PRIVATE" not in str(error.value)
    assert "PRIVATE" not in caplog.text
    first.click.assert_called_once_with(timeout=8000)
    final.click.assert_not_called()


def test_final_click_exception_is_unknown_and_never_retried(caplog) -> None:
    crawler, page, _fields, _first, final, _ = _submit_fixture()
    final.click.side_effect = RuntimeError("PRIVATE-FINAL-987654")
    with pytest.raises(HsbcLoginError, match="送出狀態不明；禁止自動重試") as error:
        crawler.submit_credentials_once(page)
    assert error.value.__cause__ is None
    assert "PRIVATE" not in str(error.value)
    assert "PRIVATE" not in caplog.text
    final.click.assert_called_once_with(timeout=8000)


def test_post_submit_exception_is_fieldless_and_never_retried(caplog) -> None:
    crawler, page, _fields, _first, final, _ = _submit_fixture()
    crawler._logged_in = Mock(side_effect=RuntimeError("PRIVATE-POST-987654"))
    with pytest.raises(HsbcLoginError, match="狀態無法安全確認；禁止自動重試") as error:
        crawler.submit_credentials_once(page)
    assert error.value.__cause__ is None
    assert "PRIVATE" not in str(error.value)
    assert "PRIVATE" not in caplog.text
    final.click.assert_called_once_with(timeout=8000)


def _captcha_page(image_nodes, refresh_nodes):
    page = Mock()
    page.locator.side_effect = lambda selector: {
        "img": _collection(*image_nodes),
        "button[aria-label='Refresh Captcha']": _collection(*refresh_nodes),
    }[selector]
    return page


def _image(*screenshots: bytes):
    image = Mock()
    image.is_visible.return_value = True
    image.get_attribute.return_value = "data:image/jpeg;base64,opaque"
    image.bounding_box.return_value = {"x": 0, "y": 0, "width": 128, "height": 40}
    image.screenshot.side_effect = screenshots
    return image


def _refresh():
    refresh = Mock()
    refresh.is_visible.return_value = True
    refresh.is_enabled.return_value = True
    return refresh


def test_captcha_reads_eight_stable_native_screenshots_and_refreshes_at_most_seven(
    monkeypatch,
) -> None:
    crawler = _crawler()
    image = _image(*(blob for n in range(8) for blob in (bytes([n]), bytes([n]))))
    refresh = _refresh()
    page = _captcha_page([image], [refresh])
    ocr = Mock(return_value=None)
    monkeypatch.setattr(hsbc_module, "ocr_bytes", ocr)

    assert crawler._solve_captcha(page) is None
    assert ocr.call_count == 8
    assert image.screenshot.call_count == 16
    assert refresh.click.call_count == 7


def test_captcha_ocr_exceptions_are_failed_reads_with_the_same_eight_seven_budget(
    monkeypatch,
) -> None:
    crawler = _crawler()
    image = _image(*(blob for n in range(8) for blob in (bytes([n]), bytes([n]))))
    refresh = _refresh()
    page = _captcha_page([image], [refresh])
    ocr = Mock(side_effect=RuntimeError("PRIVATE-OCR-987654"))
    monkeypatch.setattr(hsbc_module, "ocr_bytes", ocr)

    assert crawler._solve_captcha(page) is None
    assert ocr.call_count == 8
    assert refresh.click.call_count == 7


def test_captcha_image_and_refresh_ambiguity_stop_before_submit(monkeypatch) -> None:
    crawler = _crawler()
    ocr = Mock(return_value=None)
    monkeypatch.setattr(hsbc_module, "ocr_bytes", ocr)
    image1 = _image(b"a", b"a")
    image2 = _image(b"b", b"b")
    refresh1 = _refresh()
    refresh2 = _refresh()

    assert crawler._solve_captcha(_captcha_page([image1, image2], [refresh1])) is None
    assert ocr.call_count == 0
    assert refresh1.click.call_count == 0

    assert crawler._solve_captcha(_captcha_page([image1], [refresh1, refresh2])) is None
    assert ocr.call_count == 1
    assert refresh1.click.call_count == refresh2.click.call_count == 0


def test_pre_checkpoint_runs_before_any_user_input(monkeypatch) -> None:
    crawler = _crawler()
    crawler.prepare_login_page = Mock()
    events = []
    crawler.submit_credentials_once = lambda _page: events.append("input")
    outcomes = iter((
        CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS),
        CheckpointOutcome(CheckpointKind.AUTHENTICATED),
        CheckpointOutcome(CheckpointKind.AUTHENTICATED),
    ))

    def evaluate(_page, *, phase, **_kwargs):
        events.append(phase)
        return next(outcomes)

    monkeypatch.setattr("backend.core.base.evaluate_login_checkpoint", evaluate)
    assert crawler._shared_login(Mock()) is True
    assert events[0] is CheckpointPhase.PRE_SUBMIT
    assert events[1] == "input"


@pytest.mark.parametrize("error_text", ("密碼不正確", "驗證碼錯誤，請重新輸入。"))
def test_real_delayed_error_with_mounted_fields_submits_final_once_and_never_collects(
    error_text: str, monkeypatch, tmp_path
) -> None:
    import backend.core.base as base_mod

    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(f"""
            <input id="userId"><button data-testid="continueButton">繼續</button>
            <input id="password" hidden><input id="captchaInput" hidden>
            <button type="submit" id="final" hidden>繼續</button>
            <div class="error" hidden>{error_text}</div>
            <script>
              document.body.dataset.challenge='0'; document.body.dataset.final='0';
              document.querySelector('[data-testid=continueButton]').onclick = event => {{
                event.preventDefault(); document.body.dataset.challenge++;
                userId.hidden=true; event.target.hidden=true;
                password.hidden=false; captchaInput.hidden=false; final.hidden=false;
              }};
              final.onclick = event => {{
                event.preventDefault(); document.body.dataset.final++;
                setTimeout(() => document.querySelector('.error').hidden=false, 100);
              }};
            </script>
        """)
        crawler = _crawler()
        crawler.session_dir = tmp_path / "hsbc_session"
        crawler.session_dir.mkdir()
        monkeypatch.setattr(crawler, "prepare_login_page", lambda page: None)
        monkeypatch.setattr(crawler, "_solve_captcha", Mock(return_value="ab12C"))
        collect = Mock()
        crawler.collect = collect
        monkeypatch.setattr(base_mod, "DATA_ROOT", tmp_path)
        monkeypatch.setattr(
            base_mod.StealthyFetcher, "fetch",
            lambda _url, *, page_action, **_kwargs: page_action(page),
        )

        result = crawler.run("https://example.invalid", headless=True)

        assert "LoginCheckpointBlocked" in result["error"]
        assert page.locator("body").get_attribute("data-challenge") == "1"
        assert page.locator("body").get_attribute("data-final") == "1"
        collect.assert_not_called()
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_first_stage_dom_modal_blocks_final_submit(monkeypatch) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content("""
            <input id="userId"><button data-testid="continueButton">繼續</button>
            <input id="password" hidden><input id="captchaInput" hidden>
            <button type="submit" id="final" hidden>繼續</button>
            <div class="modal show" id="blocker" hidden>PRIVATE-DOM-BLOCKER</div>
            <script>
              document.body.dataset.challenge='0'; document.body.dataset.final='0';
              document.querySelector('[data-testid=continueButton]').onclick = event => {
                event.preventDefault(); document.body.dataset.challenge++;
                userId.hidden=true; event.target.hidden=true;
                password.hidden=false; captchaInput.hidden=false; final.hidden=false;
                blocker.hidden=false;
              };
              final.onclick = event => {
                event.preventDefault(); document.body.dataset.final++;
              };
            </script>
        """)
        crawler = _crawler()
        monkeypatch.setattr(crawler, "_solve_captcha", Mock(return_value="ab12C"))
        real_wait = page.wait_for_timeout
        monkeypatch.setattr(page, "wait_for_timeout", lambda _milliseconds: real_wait(1))

        with pytest.raises(HsbcLoginError, match="未分類提示"):
            crawler.submit_credentials_once(page)

        assert page.locator("body").get_attribute("data-challenge") == "1"
        assert page.locator("body").get_attribute("data-final") == "0"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_shared_js_dialog_runtime_is_inherited_and_terminal() -> None:
    assert "attach_dialog_handler" not in HsbcCrawler.__dict__
    assert HsbcCrawler.attach_shared_dialog_handler is BankCrawler.attach_shared_dialog_handler
    callbacks = {}
    page = SimpleNamespace(on=lambda event, callback: callbacks.setdefault(event, callback))
    crawler = _crawler()
    crawler._shared_dialog_blocked = False
    dialog = Mock()
    type(dialog).message = property(lambda _self: (_ for _ in ()).throw(AssertionError))
    dialog.accept.side_effect = AssertionError

    crawler.attach_shared_dialog_handler(page)
    callbacks["dialog"](dialog)

    dialog.dismiss.assert_called_once_with()
    assert crawler._shared_dialog_blocked is True


def test_first_stage_dialog_blocks_final_submit_and_collect(monkeypatch, tmp_path) -> None:
    import backend.core.base as base_mod

    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content("""
            <input id="userId"><button data-testid="continueButton">繼續</button>
            <input id="password" hidden><input id="captchaInput" hidden>
            <button type="submit" id="final" hidden>繼續</button>
            <script>
              document.body.dataset.challenge='0'; document.body.dataset.final='0';
              document.querySelector('[data-testid=continueButton]').onclick = event => {
                event.preventDefault(); document.body.dataset.challenge++;
                alert('PRIVATE-DIALOG-987654');
                userId.hidden=true; event.target.hidden=true;
                password.hidden=false; captchaInput.hidden=false; final.hidden=false;
              };
              final.onclick = event => {
                event.preventDefault(); document.body.dataset.final++;
              };
            </script>
        """)
        crawler = _crawler()
        crawler.session_dir = tmp_path / "hsbc_dialog_session"
        crawler.session_dir.mkdir()
        monkeypatch.setattr(crawler, "prepare_login_page", lambda page: None)
        monkeypatch.setattr(crawler, "_solve_captcha", Mock(return_value="ab12C"))
        collect = Mock()
        crawler.collect = collect
        real_wait = page.wait_for_timeout
        monkeypatch.setattr(page, "wait_for_timeout", lambda _milliseconds: real_wait(5))
        monkeypatch.setattr(base_mod, "DATA_ROOT", tmp_path)
        monkeypatch.setattr(
            base_mod.StealthyFetcher, "fetch",
            lambda _url, *, page_action, **_kwargs: page_action(page),
        )

        result = crawler.run("https://example.invalid", headless=True)

        assert result["error"] == "RuntimeError: login failed"
        assert "PRIVATE" not in result["error"]
        assert page.locator("body").get_attribute("data-challenge") == "1"
        assert page.locator("body").get_attribute("data-final") == "0"
        collect.assert_not_called()
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_legacy_login_sources_are_absent_and_collect_ast_is_unchanged() -> None:
    source = inspect.getsource(hsbc_module)
    login_region = source[: source.index("    def collect(")]
    for forbidden in (
        "import base64", "from backend.core.captcha import solve_captcha", "wait_captcha_stable", "_login_snapshot",
        "page.fill", "query_selector", "page.evaluate", "JS_CAPTCHA", "JS_LOGGED_IN",
        "captcha={", "page.screenshot", "b.click()",
    ):
        assert forbidden not in login_region

    tree = ast.parse(Path(hsbc_module.__file__).read_text())
    crawler = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "HsbcCrawler"
    )
    start = next(
        i for i, node in enumerate(crawler.body)
        if isinstance(node, ast.FunctionDef) and node.name == "collect"
    )
    payload = "\n".join(
        ast.dump(node, include_attributes=False) for node in crawler.body[start:]
    )
    assert hashlib.sha256(payload.encode()).hexdigest() == (
        "a6e43f79a469c35268b7c5296a9c6016ac092d5fe58e5bd92809aa08e22438e0"
    )
