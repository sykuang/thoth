from __future__ import annotations

import ast
import hashlib
import html
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call


import pytest

import backend.banks.esun as esun_module
from backend.banks.esun import (
    FIELD_NATIONAL_ID,
    FIELD_PASSWORD,
    FIELD_USER_CODE,
    IFRAME_HINT,
    LOGIN_BTN_ID,
    EsunCrawler,
    EsunLoginError,
    _sel,
)
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointPhase,
    LoginCheckpointBlocked,
    LoginInteractionRequired,
    evaluate_login_checkpoint,
)


LOGIN_FIELD_SELECTOR = ", ".join(
    _sel(field) for field in (FIELD_NATIONAL_ID, FIELD_USER_CODE, FIELD_PASSWORD)
)


def _crawler() -> EsunCrawler:
    crawler = object.__new__(EsunCrawler)
    crawler.name = "esun"
    crawler._credential_origin_allowed = lambda _page: True
    return crawler


def test_esun_shared_login_api_and_terminal_first_rules() -> None:
    rules = _crawler().login_checkpoint_rules()

    assert EsunCrawler.USES_SHARED_LOGIN_CHECKPOINTS is True
    assert [rule.name for rule in rules] == [
        "esun-otp-required-modal",
        "esun-otp-required-dialog",
        "esun-password-change-required-modal",
        "esun-password-change-required-dialog",
        "esun-unknown-modal",
        "esun-unknown-dialog",
        "esun-login-form-still-visible",
    ]
    assert [rule.kind for rule in rules] == [
        CheckpointKind.OTP_REQUIRED,
        CheckpointKind.OTP_REQUIRED,
        CheckpointKind.PASSWORD_CHANGE_REQUIRED,
        CheckpointKind.PASSWORD_CHANGE_REQUIRED,
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
        _sel(FIELD_NATIONAL_ID),
    ]
    assert [rule.phases for rule in rules] == [
        tuple(CheckpointPhase),
        tuple(CheckpointPhase),
        tuple(CheckpointPhase),
        tuple(CheckpointPhase),
        tuple(CheckpointPhase),
        tuple(CheckpointPhase),
        (CheckpointPhase.POST_SUBMIT, CheckpointPhase.POST_SUBMIT_SETTLE),
    ]
    assert all(rule.bank == "esun" for rule in rules)
    assert all(rule.action_texts == () and not rule.is_clickable for rule in rules)


@pytest.mark.parametrize(
    "marker",
    (
        "OTP",
        "一次性密碼",
        "簡訊驗證碼",
        "裝置綁定",
        "安全認證",
        "裝置驗證",
        "信任此裝置",
        "新裝置登入",
    ),
)
def test_esun_otp_pattern_is_anchored_and_explicit(marker: str) -> None:
    pattern = _crawler().login_checkpoint_rules()[0].required_body_pattern

    assert pattern is not None
    assert pattern.fullmatch(f"登入程序\n{marker}\n請繼續")
    assert not pattern.search("一般安全提醒")
    assert not pattern.search("圖形驗證碼已傳送，請重新輸入")


@pytest.mark.parametrize(
    ("positive", "negative"),
    (
        ("請立即修改您的密碼", "可以稍後修改密碼"),
        ("您必須變更密碼", "變更密碼說明"),
        ("請先重設密碼", "重設密碼成功"),
        ("密碼已到期", "密碼設定"),
        ("密碼過期", "密碼安全"),
        ("密碼強制變更", "密碼變更完成"),
    ),
)
def test_esun_password_change_pattern_is_mandatory_only(
    positive: str,
    negative: str,
) -> None:
    pattern = _crawler().login_checkpoint_rules()[2].required_body_pattern

    assert pattern is not None
    assert pattern.fullmatch(f"通知\n{positive}\n請繼續")
    assert not pattern.search(negative)
    assert not pattern.search("密碼輸入錯誤，圖形驗證碼已過期")


def test_esun_legacy_login_debug_and_generic_actions_are_absent() -> None:
    source = inspect.getsource(esun_module)
    login_source = inspect.getsource(EsunCrawler.login)

    assert login_source.strip().endswith("return self._shared_login(page)")
    for forbidden in (
        "_login_snapshot",
        "handle_dup_login_modal",
        "page.screenshot",
        "document.body.innerText\")[:2000]",
        "錯誤訊息/全文摘要",
        "確定登入",
        "取消",
    ):
        assert forbidden not in source[: source.index("    # ---------- 抓取 ----------")]


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
        bank="esun",
        phase=phase,
        rules=crawler.login_checkpoint_rules(),
        is_authenticated=lambda _page: False,
    )


@pytest.mark.parametrize("attrs", ('class="modal show"', 'role="dialog"'))
@pytest.mark.parametrize(
    ("body", "expected"),
    (
        ("重複登入，請輸入OTP後確認登入", CheckpointKind.OTP_REQUIRED),
        ("重複登入，請立即修改密碼後確認登入", CheckpointKind.PASSWORD_CHANGE_REQUIRED),
    ),
)
def test_real_terminal_collision_has_precedence_and_zero_click(
    attrs: str,
    body: str,
    expected: CheckpointKind,
) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(
            f"""
            <div {attrs}>{body}<button>確定登入</button></div>
            <script>
              document.body.dataset.clicks = '0';
              document.querySelector('button').onclick = () => document.body.dataset.clicks++;
            </script>
            """
        )

        for phase in CheckpointPhase:
            outcome = _evaluate(page, phase)
            assert outcome.kind is expected
            assert page.locator("body").get_attribute("data-clicks") == "0"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_real_unknown_hidden_pre_ready_and_visible_form_rules_fail_closed() -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        for attrs in ('class="modal show"', 'role="dialog"'):
            page.set_content(
                f"""
                <div {attrs}>重複登入<button>確定登入</button></div>
                <script>
                  document.body.dataset.clicks = '0';
                  document.querySelector('button').onclick = () => document.body.dataset.clicks++;
                </script>
                """
            )
            outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
            assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
            assert page.locator("body").get_attribute("data-clicks") == "0"

        page.set_content('<div class="modal show" hidden>一般通知<button>確定</button></div>')
        assert _evaluate(page, CheckpointPhase.PRE_SUBMIT).kind is CheckpointKind.READY_FOR_CREDENTIALS

        for body in (
            "圖形驗證碼已傳送，請重新輸入",
            "密碼輸入錯誤，圖形驗證碼已過期",
        ):
            page.set_content(f'<div class="modal show">{body}<button>確定</button></div>')
            assert _evaluate(page, CheckpointPhase.POST_SUBMIT).kind is CheckpointKind.UNKNOWN_BLOCKER

        page.set_content(f'<input id="{FIELD_NATIONAL_ID}">')
        assert _evaluate(page, CheckpointPhase.PRE_SUBMIT).kind is CheckpointKind.READY_FOR_CREDENTIALS
        for phase in (CheckpointPhase.POST_SUBMIT, CheckpointPhase.POST_SUBMIT_SETTLE):
            outcome = _evaluate(page, phase)
            assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
            assert outcome.rule_name == "esun-login-form-still-visible"
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def _iframe_srcdoc(body: str) -> str:
    return html.escape(body, quote=True)


def _proxy(real_page):
    class PageProxy:
        url = "https://ebank.esunbank.com.tw/index.jsp"

        def __getattr__(self, name):
            return getattr(real_page, name)

    return PageProxy()


def test_real_auth_accepts_persistent_frame_only_with_eight_dashboard_keywords() -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        dashboard = "訊息中心 個人資訊 登出 帳戶總覽 歡迎使用 存款 轉帳 信用卡 " + "x" * 600
        page.set_content(f'<iframe name="iframe1" srcdoc="{_iframe_srcdoc(dashboard)}"></iframe>')
        page.wait_for_timeout(100)

        crawler = _crawler()
        assert crawler._logged_in(_proxy(page)) is True

        public_menu = "存款 轉帳 信用卡 台幣 外幣 基金 投資 貸款 " + "x" * 600
        hidden_login = public_menu + f'<input id="{FIELD_PASSWORD}" hidden>'
        page.set_content(f'<iframe name="iframe1" srcdoc="{_iframe_srcdoc(hidden_login)}"></iframe>')
        page.wait_for_timeout(100)
        assert crawler._logged_in(_proxy(page)) is False

        with_form = dashboard + f'<input id="{FIELD_PASSWORD}">'
        page.set_content(f'<iframe name="iframe1" srcdoc="{_iframe_srcdoc(with_form)}"></iframe>')
        page.wait_for_timeout(100)
        assert crawler._logged_in(_proxy(page)) is False
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_login_frame_rejects_foreign_origin_even_with_matching_path() -> None:
    crawler = _crawler()
    foreign = Mock()
    foreign.name = "iframe1"
    foreign.url = "https://evil.example/fco/fco08001/FCO08001_Home.faces"

    assert crawler._find_login_frame(SimpleNamespace(
        frames=[foreign], url="https://ebank.esunbank.com.tw/fco/"
    )) is None


def test_login_frame_rejects_srcdoc_inherited_from_foreign_parent() -> None:
    crawler = _crawler()
    main = Mock(url="https://ebank.esunbank.com.tw/fco/")
    foreign = Mock(url="https://evil.example/embedded", parent_frame=main)
    child = Mock(url="about:srcdoc", parent_frame=foreign)
    child.name = "iframe1"
    page = SimpleNamespace(
        url="https://ebank.esunbank.com.tw/fco/",
        main_frame=main,
        frames=[main, foreign, child],
    )

    assert crawler._find_login_frame(page) is None


def test_real_auth_accepts_no_frame_with_two_keywords_and_rejects_ambiguity() -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content("<main>登出 帳戶總覽 " + "x" * 600 + "</main>")
        crawler = _crawler()
        assert crawler._logged_in(_proxy(page)) is True

        page.set_content("<main>存款 信用卡 " + "x" * 600 + "</main>")
        assert crawler._logged_in(_proxy(page)) is False

        dashboard = "訊息中心 個人資訊 登出 帳戶總覽 歡迎使用 存款 轉帳 信用卡 " + "x" * 600
        srcdoc = _iframe_srcdoc(dashboard)
        page.set_content(
            f'<iframe name="iframe1" srcdoc="{srcdoc}"></iframe>'
            f'<iframe name="iframe1" srcdoc="{srcdoc}"></iframe>'
        )
        page.wait_for_timeout(100)
        assert crawler._logged_in(_proxy(page)) is False
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_auth_rejects_esun_text_outside_the_hostname() -> None:
    page = Mock()
    page.url = "https://evil.example/?next=esunbank.com.tw"
    page.frames = []
    page.main_frame = Mock()
    page.locator.return_value.count.return_value = 0
    page.evaluate.return_value = "帳戶總覽 信用卡 " + "x" * 600

    assert _crawler()._logged_in(page) is False

    page.url = "https://marketing.esunbank.com.tw/home"
    page.evaluate.return_value = "登出 帳戶總覽 " + "x" * 600
    assert _crawler()._logged_in(page) is False


def test_auth_is_one_shot_fieldless_and_exception_safe(capsys) -> None:
    page = Mock()
    page.url = "https://ebank.esunbank.com.tw/PRIVATE-PATH"
    page.frames = []
    page.main_frame = Mock()
    page.locator.side_effect = RuntimeError("PRIVATE-DOM-987654")

    assert _crawler()._logged_in(page) is False
    page.wait_for_timeout.assert_not_called()
    captured = capsys.readouterr()
    assert "PRIVATE-PATH" not in captured.err
    assert "PRIVATE-DOM-987654" not in captured.err


def _submit_fixture():
    page = Mock()
    page.url = "https://ebank.esunbank.com.tw/fco/"
    frame = Mock(url=f"https://ebank.esunbank.com.tw/fco/{IFRAME_HINT}")
    page.frames = [frame]
    page.main_frame = Mock()

    values = {
        _sel(FIELD_NATIONAL_ID): "ID-PRIVATE",
        _sel(FIELD_USER_CODE): "USER-PRIVATE",
        _sel(FIELD_PASSWORD): "PASSWORD-PRIVATE",
    }
    fields = {selector: Mock() for selector in values}
    for selector, field in fields.items():
        field.count.return_value = 1
        field.nth.return_value = field
        field.is_visible.return_value = True
        field.is_enabled.return_value = True
        field.input_value.return_value = values[selector]

    actions = Mock()
    action = Mock()
    actions.count.return_value = 1
    actions.nth.return_value = action
    action.is_visible.return_value = True
    action.is_enabled.return_value = True

    empty = Mock()
    empty.count.return_value = 0
    locator_map = {
        **fields,
        _sel(LOGIN_BTN_ID): actions,
        LOGIN_FIELD_SELECTOR: empty,
        ".modal.show": empty,
        "[role='dialog']": empty,
    }
    frame.locator.side_effect = locator_map.__getitem__
    page.locator.side_effect = locator_map.__getitem__

    crawler = _crawler()
    crawler.creds = SimpleNamespace(
        national_id=values[_sel(FIELD_NATIONAL_ID)],
        user_code=values[_sel(FIELD_USER_CODE)],
        password=values[_sel(FIELD_PASSWORD)],
    )
    crawler._logged_in = Mock(return_value=True)
    return crawler, page, frame, fields, actions, action, empty


def test_submit_requires_unique_frame_fields_lengths_and_action_then_clicks_once() -> None:
    crawler, page, _frame, fields, _actions, action, _empty = _submit_fixture()

    crawler.submit_credentials_once(page)

    for field in fields.values():
        assert field.method_calls == [
            call.count(),
            call.nth(0),
            call.is_visible(),
            call.is_enabled(),
            call.click(),
            call.click(click_count=3),
            call.input_value(),
        ]
    assert page.keyboard.press.call_args_list == [call("Backspace")] * 3
    assert page.keyboard.type.call_args_list == [
        call("ID-PRIVATE", delay=80),
        call("USER-PRIVATE", delay=80),
        call("PASSWORD-PRIVATE", delay=80),
    ]
    page.fill.assert_not_called()
    assert page.wait_for_timeout.call_args_list == [
        call(200),
        call(200),
        call(300),
        call(10000),
        call(1000),
    ]
    action.click.assert_called_once_with(timeout=8000)
    page.click.assert_not_called()
    page.evaluate.assert_not_called()


def test_submit_frame_inspection_error_is_fieldless_and_zero_click(caplog) -> None:
    crawler, page, _frame, _fields, _actions, action, _empty = _submit_fixture()
    secret = "PRIVATE-FRAME-DOM-987654"
    crawler._find_login_frame = Mock(side_effect=RuntimeError(secret))

    with pytest.raises(EsunLoginError, match="未送出登入") as error:
        crawler.submit_credentials_once(page)

    assert error.value.__cause__ is None
    assert secret not in str(error.value)
    assert secret not in caplog.text
    action.click.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    ("no-frame", "ambiguous-frame", "duplicate", "hidden", "disabled", "length", "keyboard", "input"),
)
def test_submit_frame_and_field_failures_are_fieldless_and_zero_click(
    failure: str,
    caplog,
) -> None:
    crawler, page, _frame, fields, _actions, action, _empty = _submit_fixture()
    secret = "PRIVATE-FIELD-DOM-987654"
    if failure == "no-frame":
        page.frames = []
    elif failure == "ambiguous-frame":
        page.frames.append(Mock(url=f"https://ebank.esunbank.com.tw/{IFRAME_HINT}?two"))
    elif failure == "duplicate":
        fields[_sel(FIELD_NATIONAL_ID)].count.return_value = 2
    elif failure == "hidden":
        fields[_sel(FIELD_USER_CODE)].is_visible.return_value = False
    elif failure == "disabled":
        fields[_sel(FIELD_PASSWORD)].is_enabled.return_value = False
    elif failure == "length":
        fields[_sel(FIELD_PASSWORD)].input_value.return_value = "short"
    elif failure == "keyboard":
        fields[_sel(FIELD_USER_CODE)].click.side_effect = RuntimeError(secret)
    else:
        fields[_sel(FIELD_USER_CODE)].input_value.side_effect = RuntimeError(secret)

    with pytest.raises(EsunLoginError, match="未送出登入") as error:
        crawler.submit_credentials_once(page)

    assert error.value.__cause__ is None
    assert secret not in str(error.value)
    assert "ID-PRIVATE" not in str(error.value)
    assert "PASSWORD-PRIVATE" not in str(error.value)
    assert secret not in caplog.text
    action.click.assert_not_called()


@pytest.mark.parametrize(
    ("count", "visible", "enabled"),
    ((0, True, True), (2, True, True), (1, False, True), (1, True, False)),
)
def test_submit_action_must_be_unique_visible_and_enabled(
    count: int,
    visible: bool,
    enabled: bool,
) -> None:
    crawler, page, _frame, _fields, actions, action, _empty = _submit_fixture()
    actions.count.return_value = count
    action.is_visible.return_value = visible
    action.is_enabled.return_value = enabled

    with pytest.raises(EsunLoginError, match="未送出登入"):
        crawler.submit_credentials_once(page)

    action.click.assert_not_called()
    page.click.assert_not_called()


def test_submit_action_inspection_error_is_fieldless_and_zero_click(caplog) -> None:
    crawler, page, _frame, _fields, actions, action, _empty = _submit_fixture()
    secret = "PRIVATE-ACTION-DOM-987654"
    actions.count.side_effect = RuntimeError(secret)

    with pytest.raises(EsunLoginError, match="未送出登入") as error:
        crawler.submit_credentials_once(page)

    assert error.value.__cause__ is None
    assert secret not in str(error.value)
    assert secret not in caplog.text
    action.click.assert_not_called()


def test_submit_click_exception_is_fieldless_unknown_status_and_one_attempt(caplog) -> None:
    crawler, page, _frame, _fields, _actions, action, _empty = _submit_fixture()
    secret = "PRIVATE-CLICK-DOM-987654"
    action.click.side_effect = RuntimeError(secret)

    with pytest.raises(EsunLoginError, match="送出狀態不明.*禁止自動重試") as error:
        crawler.submit_credentials_once(page)

    assert error.value.__cause__ is None
    assert secret not in str(error.value)
    assert secret not in caplog.text
    action.click.assert_called_once_with(timeout=8000)
    page.click.assert_not_called()


@pytest.mark.parametrize("failing_wait", (10000, 1000))
def test_post_submit_wait_exception_returns_fieldlessly_after_one_click(
    failing_wait: int,
    caplog,
) -> None:
    crawler, page, _frame, _fields, _actions, action, _empty = _submit_fixture()
    secret = f"PRIVATE-WAIT-{failing_wait}-987654"

    def wait(milliseconds: int) -> None:
        if milliseconds == failing_wait:
            raise RuntimeError(secret)

    page.wait_for_timeout.side_effect = wait
    crawler.submit_credentials_once(page)

    assert secret not in caplog.text
    action.click.assert_called_once_with(timeout=8000)


@pytest.mark.parametrize("failure", ("auth", "modal", "dialog", "form"))
def test_post_submit_inspection_exception_returns_fieldlessly_after_one_click(
    failure: str,
    caplog,
) -> None:
    crawler, page, frame, _fields, _actions, action, empty = _submit_fixture()
    secret = f"PRIVATE-INSPECTION-{failure}-987654"
    crawler._logged_in.return_value = False
    if failure == "auth":
        crawler._logged_in.side_effect = RuntimeError(secret)
    elif failure == "modal":
        empty.count.side_effect = RuntimeError(secret)
    elif failure == "dialog":
        special = Mock()
        special.count.side_effect = RuntimeError(secret)
        frame.locator.side_effect = lambda selector: special if selector == "[role='dialog']" else {
            **_fields,
            _sel(LOGIN_BTN_ID): _actions,
            LOGIN_FIELD_SELECTOR: empty,
            ".modal.show": Mock(count=Mock(return_value=0)),
        }[selector]
    else:
        _fields[_sel(FIELD_NATIONAL_ID)].count.side_effect = [
            1,
            RuntimeError(secret),
        ]

    crawler.submit_credentials_once(page)

    assert secret not in caplog.text
    action.click.assert_called_once_with(timeout=8000)


def test_post_submit_timeout_returns_to_evaluator_after_thirty_polls() -> None:
    crawler, page, _frame, fields, _actions, action, _empty = _submit_fixture()
    crawler._logged_in.return_value = False
    fields[_sel(FIELD_NATIONAL_ID)].is_visible.side_effect = [True] + [False] * 60

    crawler.submit_credentials_once(page)

    assert page.wait_for_timeout.call_args_list[-31:] == [call(10000)] + [call(1000)] * 30
    assert crawler._logged_in.call_count == 30
    action.click.assert_called_once_with(timeout=8000)


def test_real_submit_stops_at_multiple_modals_without_actions(monkeypatch) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        frame_html = f"""
          <input id="{FIELD_NATIONAL_ID}"><input id="{FIELD_USER_CODE}">
          <input id="{FIELD_PASSWORD}"><a id="{LOGIN_BTN_ID}">登入</a>
          <script>
            document.body.dataset.submit = '0';
            document.body.dataset.modal = '0';
            document.querySelector("[id='{LOGIN_BTN_ID}']").onclick = () => {{
              document.body.dataset.submit++;
              document.body.insertAdjacentHTML('beforeend', `
                <div class="modal show">PRIVATE-ONE<button>確認</button></div>
                <div role="dialog">PRIVATE-TWO<button>確定</button></div>
              `);
              document.querySelectorAll('.modal button, [role=dialog] button').forEach(button => {{
                button.onclick = () => document.body.dataset.modal++;
              }});
            }};
          </script>
        """
        page.set_content(f'<iframe name="iframe1" srcdoc="{_iframe_srcdoc(frame_html)}"></iframe>')
        page.wait_for_timeout(100)
        crawler = _crawler()
        crawler.creds = SimpleNamespace(
            national_id="ID-PRIVATE",
            user_code="USER-PRIVATE",
            password="PASSWORD-PRIVATE",
        )
        monkeypatch.setattr(crawler, "_logged_in", lambda _page: False)
        original_wait = page.wait_for_timeout
        monkeypatch.setattr(page, "wait_for_timeout", lambda _milliseconds: original_wait(1))

        crawler.submit_credentials_once(_proxy(page))

        frame = crawler._find_login_frame(_proxy(page))
        assert frame.locator("body").get_attribute("data-submit") == "1"
        assert frame.locator("body").get_attribute("data-modal") == "0"
        assert frame.locator(".modal.show").count() == 1
        assert frame.locator("[role='dialog']").count() == 1
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_login_prepare_and_authentication_are_thin_adapters(monkeypatch) -> None:
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


def test_shared_login_terminal_stops_before_collect_without_retry(monkeypatch) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        crawler = _crawler()
        submissions = 0
        collect = Mock()

        def submit(page) -> None:
            nonlocal submissions
            submissions += 1
            page.set_content('<div class="modal show">請輸入OTP<button>確定登入</button></div>')

        monkeypatch.setattr(crawler, "prepare_login_page", lambda _page: None)
        monkeypatch.setattr(crawler, "is_authenticated", lambda _page: False)
        monkeypatch.setattr(crawler, "submit_credentials_once", submit)
        monkeypatch.setattr(crawler, "collect", collect)

        with pytest.raises(LoginInteractionRequired):
            crawler._shared_login(page)

        assert submissions == 1
        collect.assert_not_called()
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_shared_login_unknown_modal_blocks_before_submission(monkeypatch) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content('<div class="modal show">重複登入<button>確定登入</button></div>')
        crawler = _crawler()
        submit = Mock()
        monkeypatch.setattr(crawler, "prepare_login_page", lambda _page: None)
        monkeypatch.setattr(crawler, "is_authenticated", lambda _page: False)
        monkeypatch.setattr(crawler, "submit_credentials_once", submit)

        with pytest.raises(LoginCheckpointBlocked):
            crawler._shared_login(page)

        submit.assert_not_called()
        assert page.locator("button").get_attribute("data-clicked") is None
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_collect_and_following_helpers_keep_protected_ast_contract() -> None:
    current_source = Path(esun_module.__file__).read_text()

    tree = ast.parse(current_source)
    crawler = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "EsunCrawler"
    )
    start = next(
        index
        for index, node in enumerate(crawler.body)
        if isinstance(node, ast.FunctionDef) and node.name == "collect"
    )
    payload = "\n".join(
        ast.dump(node, include_attributes=False) for node in crawler.body[start:]
    ).encode()

    assert hashlib.sha256(payload).hexdigest() == (
        "70cecc251be6aa98914b9d71b7d9ad01b5973f23a19fd4b600e7cc16cdc9099f"
    )
