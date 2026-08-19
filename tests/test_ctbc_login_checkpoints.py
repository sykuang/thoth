from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

import backend.banks.ctbc as ctbc_module
from backend.banks.ctbc import CtbcCrawler, CtbcLoginError
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointPhase,
    LoginCheckpointBlocked,
    evaluate_login_checkpoint,
)


def _crawler() -> CtbcCrawler:
    crawler = object.__new__(CtbcCrawler)
    crawler.name = "ctbc"
    return crawler


def test_ctbc_shared_login_api_and_ordered_rules() -> None:
    crawler = _crawler()
    rules = crawler.login_checkpoint_rules()

    assert CtbcCrawler.USES_SHARED_LOGIN_CHECKPOINTS is True
    assert [rule.name for rule in rules] == [
        "ctbc-entry-announcement",
        "ctbc-otp-required",
        "ctbc-duplicate-session",
        "ctbc-unknown-modal",
    ]
    assert [rule.kind for rule in rules] == [
        CheckpointKind.DISMISSIBLE_NOTICE,
        CheckpointKind.OTP_REQUIRED,
        CheckpointKind.DUPLICATE_SESSION,
        CheckpointKind.UNKNOWN_BLOCKER,
    ]
    assert [rule.phases for rule in rules] == [
        (CheckpointPhase.PRE_SUBMIT,),
        (CheckpointPhase.POST_SUBMIT, CheckpointPhase.POST_SUBMIT_SETTLE),
        (CheckpointPhase.POST_SUBMIT, CheckpointPhase.POST_SUBMIT_SETTLE),
        tuple(CheckpointPhase),
    ]
    assert all(rule.bank == "ctbc" for rule in rules)
    assert all(rule.container_selector == ".modal.show" for rule in rules)
    assert rules[0].action_selector == "a.btn_close"
    assert rules[0].action_texts == ()
    assert rules[0].max_actions == 1
    assert rules[1].action_texts == ()
    assert rules[2].action_texts == ("確認登入",)
    assert rules[2].max_actions == 1
    assert rules[3].action_texts == ()


@pytest.mark.parametrize(
    ("index", "positive", "negative"),
    [
        (0, "\n  重要公告\n系統維護", "非重要公告\n系統維護"),
        (2, "確認訊息\n前次工作階段仍存在\n確認登入", "確認登入\n確認訊息"),
        (1, "請完成一次性密碼後繼續", "請完成密碼後繼續"),
    ],
)
def test_ctbc_rule_patterns_are_anchored_positive_and_negative(
    index: int,
    positive: str,
    negative: str,
) -> None:
    pattern = _crawler().login_checkpoint_rules()[index].required_body_pattern

    assert pattern is not None
    assert pattern.search(positive)
    assert not pattern.search(negative)


@pytest.mark.parametrize(
    "marker",
    ["簡訊驗證", "一次性密碼", "動態密碼", "OTP 驗證", "認證碼"],
)
def test_ctbc_otp_pattern_accepts_only_observed_markers(marker: str) -> None:
    pattern = _crawler().login_checkpoint_rules()[1].required_body_pattern

    assert pattern is not None
    assert pattern.search(f"驗證程序\n{marker}\n請輸入")


def test_ctbc_unsafe_login_helpers_and_snapshot_import_are_removed() -> None:
    source = inspect.getsource(ctbc_module)

    assert not hasattr(ctbc_module, "_close_entry_announcement")
    assert not hasattr(ctbc_module, "JS_CONFIRM_LOGIN")
    assert not hasattr(CtbcCrawler, "_enter_overview_if_interstitial")
    for forbidden in (
        "_login_snapshot",
        "css_first(\"body\")",
        "document.querySelectorAll('button,a,[role=button]')",
    ):
        assert forbidden not in source


def _evaluate(page, phase: CheckpointPhase):
    crawler = _crawler()
    return evaluate_login_checkpoint(
        page,
        bank="ctbc",
        phase=phase,
        rules=crawler.login_checkpoint_rules(),
        is_authenticated=lambda _page: False,
    )


def _launch_browser():
    from patchright.sync_api import sync_playwright

    manager = sync_playwright()
    patchright = manager.start()
    if not Path(patchright.chromium.executable_path).exists():
        manager.__exit__(None, None, None)
        pytest.skip("Patchright browser binary is not installed")
    return manager, patchright.chromium.launch(headless=True)


def test_real_dom_announcement_is_narrow_and_hidden_static_modal_is_ignored() -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(
            """
            <style>#announcement .btn_close { display: block; width: 10px; height: 10px; }</style>
            <a class="btn_close" id="outside">outside</a>
            <div class="modal show" id="security">安全提醒<a class="btn_close">close</a></div>
            <div class="modal show" id="announcement">重要公告<a class="btn_close"></a></div>
            <script>
              document.body.dataset.outside = '0';
              document.body.dataset.security = '0';
              document.body.dataset.announcement = '0';
              outside.onclick = () => document.body.dataset.outside++;
              security.querySelector('a').onclick = () => document.body.dataset.security++;
              announcement.querySelector('a').onclick = () => {
                document.body.dataset.announcement++;
                announcement.hidden = true;
              };
            </script>
            """
        )

        outcome = _evaluate(page, CheckpointPhase.PRE_SUBMIT)

        assert outcome.kind is CheckpointKind.DISMISSIBLE_NOTICE
        assert outcome.rule_name == "ctbc-entry-announcement"
        assert outcome.action_label is None
        assert page.locator("body").get_attribute("data-announcement") == "1"
        assert page.locator("body").get_attribute("data-security") == "0"
        assert page.locator("body").get_attribute("data-outside") == "0"

        page.set_content(
            """
            <div class="modal show" id="notice">系統公告<a class="btn_close">close</a></div>
            <script>
              document.body.dataset.clicks = '0';
              notice.querySelector('a').onclick = () => document.body.dataset.clicks++;
            </script>
            """
        )
        outcome = _evaluate(page, CheckpointPhase.PRE_SUBMIT)
        assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
        assert outcome.rule_name == "ctbc-unknown-modal"
        assert page.locator("body").get_attribute("data-clicks") == "0"

        page.set_content(
            '<div class="modal show" hidden>重要公告<a class="btn_close"></a></div>'
        )
        assert _evaluate(page, CheckpointPhase.PRE_SUBMIT).kind is CheckpointKind.READY_FOR_CREDENTIALS
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_real_dom_duplicate_otp_and_unknown_modals_never_use_generic_actions() -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(
            """
            <div class="modal show" id="duplicate">
              確認訊息<br>前次工作階段仍存在<br><button>確認登入</button>
            </div>
            <script>
              document.body.dataset.clicks = '0';
              duplicate.querySelector('button').onclick = () => {
                document.body.dataset.clicks++;
                duplicate.hidden = true;
              };
            </script>
            """
        )
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.kind is CheckpointKind.DUPLICATE_SESSION
        assert outcome.action_label == "確認登入"
        assert page.locator("body").get_attribute("data-clicks") == "1"

        page.set_content(
            """
            <div class="modal show" id="otp-collision">
              確認訊息<br>請完成 OTP 驗證<br><button>確認登入</button>
            </div>
            <script>
              document.body.dataset.clicks = '0';
              document.querySelector('#otp-collision button').onclick = () =>
                document.body.dataset.clicks++;
            </script>
            """
        )
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.kind is CheckpointKind.OTP_REQUIRED
        assert page.locator("body").get_attribute("data-clicks") == "0"

        for body in (
            "確認訊息<br><button>確認</button>",
            "確認訊息<br><button>確定</button>",
            "請確認訊息<br><button>確認登入</button>",
            "確認登入<br><button>確認訊息</button>",
        ):
            page.set_content(
                f"""
                <div class="modal show" id="candidate">{body}</div>
                <script>
                  document.body.dataset.clicks = '0';
                  candidate.querySelector('button').onclick = () => document.body.dataset.clicks++;
                </script>
                """
            )
            outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
            assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
            assert page.locator("body").get_attribute("data-clicks") == "0"

        page.set_content(
            """
            <div class="modal show" id="otp">請完成 OTP 驗證<button>確認登入</button></div>
            <script>
              document.body.dataset.clicks = '0';
              otp.querySelector('button').onclick = () => document.body.dataset.clicks++;
            </script>
            """
        )
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.kind is CheckpointKind.OTP_REQUIRED
        assert outcome.interaction == "otp"
        assert page.locator("body").get_attribute("data-clicks") == "0"

        secret = "PRIVATE-MODAL-BODY-987654"
        page.set_content(f'<div class="modal show">{secret}<button>確認登入</button></div>')
        outcome = _evaluate(page, CheckpointPhase.POST_SUBMIT)
        assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
        assert secret not in repr(outcome)
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def _submit_fixture():
    page = Mock()
    candidates = Mock()
    button = Mock()
    modals = Mock()
    modal = Mock()
    candidates.count.return_value = 1
    candidates.first = button
    button.is_visible.return_value = True
    button.is_enabled.return_value = True
    button.get_attribute.return_value = "btn_submit"
    modals.count.return_value = 1
    modals.nth.return_value = modal
    modal.is_visible.return_value = True
    page.locator.side_effect = lambda selector: {
        "a.btn_submit": candidates,
        ".modal.show": modals,
    }[selector]

    crawler = _crawler()
    crawler.creds = SimpleNamespace(
        national_id="ID-PRIVATE",
        user_code="USER-PRIVATE",
        password="PASSWORD-PRIVATE",
    )
    crawler._logged_in = Mock(return_value=False)
    return crawler, page, candidates, button, modals


def test_submit_fills_once_clicks_once_and_preserves_timing() -> None:
    crawler, page, _candidates, button, _modals = _submit_fixture()

    crawler.submit_credentials_once(page)

    page.wait_for_selector.assert_called_once_with(
        'input[formcontrolname="custIxd"]', state="visible", timeout=15000
    )
    assert page.fill.call_args_list == [
        call('input[formcontrolname="custIxd"]', "ID-PRIVATE"),
        call('input[formcontrolname="userIxd"]', "USER-PRIVATE"),
        call('input[formcontrolname="pxd"]', "PASSWORD-PRIVATE"),
    ]
    assert page.wait_for_timeout.call_args_list == [
        call(300),
        call(300),
        call(300),
        call(5000),
        call(1000),
    ]
    button.click.assert_called_once_with(timeout=8000)
    page.click.assert_not_called()


@pytest.mark.parametrize(
    ("count", "visible", "enabled", "classes"),
    [
        (0, True, True, "btn_submit"),
        (2, True, True, "btn_submit"),
        (1, False, True, "btn_submit"),
        (1, True, False, "btn_submit"),
        (1, True, True, "btn_submit disabled"),
    ],
)
def test_submit_preconditions_fail_before_click(
    count: int,
    visible: bool,
    enabled: bool,
    classes: str,
) -> None:
    crawler, page, candidates, button, _modals = _submit_fixture()
    candidates.count.return_value = count
    button.is_visible.return_value = visible
    button.is_enabled.return_value = enabled
    button.get_attribute.return_value = classes

    with pytest.raises(CtbcLoginError, match="未送出登入"):
        crawler.submit_credentials_once(page)

    button.click.assert_not_called()
    page.click.assert_not_called()


def test_submit_button_inspection_exception_is_fieldless_and_zero_click(caplog) -> None:
    crawler, page, candidates, button, _modals = _submit_fixture()
    secret = "PRIVATE-BUTTON-DOM-987654"
    candidates.count.side_effect = RuntimeError(secret)

    with pytest.raises(CtbcLoginError, match="無法安全確認.*未送出登入") as error:
        crawler.submit_credentials_once(page)

    assert secret not in str(error.value)
    assert secret not in caplog.text
    button.click.assert_not_called()


def test_submit_click_exception_is_fieldless_unknown_status_and_exactly_one_attempt(caplog) -> None:
    crawler, page, _candidates, button, _modals = _submit_fixture()
    secret = "PRIVATE-CLICK-DOM-987654"
    button.click.side_effect = RuntimeError(secret)

    with pytest.raises(CtbcLoginError, match="送出狀態不明.*禁止自動重試") as error:
        crawler.submit_credentials_once(page)

    assert secret not in str(error.value)
    assert secret not in caplog.text
    button.click.assert_called_once_with(timeout=8000)
    page.click.assert_not_called()


@pytest.mark.parametrize("failure", ["wait", "fill"])
def test_field_failure_is_fieldless_and_zero_click(failure: str, caplog) -> None:
    crawler, page, _candidates, button, _modals = _submit_fixture()
    secret = "PRIVATE-FIELD-DOM-987654"
    if failure == "wait":
        page.wait_for_selector.side_effect = RuntimeError(secret)
    else:
        page.fill.side_effect = RuntimeError(secret)

    with pytest.raises(CtbcLoginError, match="欄位無法安全填寫.*未送出登入") as error:
        crawler.submit_credentials_once(page)

    rendered = str(error.value)
    assert secret not in rendered
    assert "ID-PRIVATE" not in rendered
    assert "USER-PRIVATE" not in rendered
    assert "PASSWORD-PRIVATE" not in rendered
    assert secret not in caplog.text
    button.click.assert_not_called()
    page.locator.assert_not_called()


def test_structural_wait_handles_multiple_visible_modals_without_action_or_resubmit() -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content(
            """
            <input formcontrolname="custIxd">
            <input formcontrolname="userIxd">
            <input formcontrolname="pxd">
            <a class="btn_submit">登入</a>
            <script>
              document.body.dataset.submitClicks = '0';
              document.body.dataset.modalClicks = '0';
              document.querySelector('.btn_submit').onclick = () => {
                document.body.dataset.submitClicks++;
                document.body.insertAdjacentHTML('beforeend', `
                  <div class="modal show">PRIVATE-FIRST<button>確認</button></div>
                  <div class="modal show">PRIVATE-SECOND<button>確定</button></div>
                `);
                document.querySelectorAll('.modal button').forEach(button => {
                  button.onclick = () => document.body.dataset.modalClicks++;
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
        crawler._logged_in = Mock(return_value=False)

        crawler.submit_credentials_once(page)

        assert page.locator("body").get_attribute("data-submit-clicks") == "1"
        assert page.locator("body").get_attribute("data-modal-clicks") == "0"
        assert page.locator(".modal.show").count() == 2
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_post_submit_inspection_exception_returns_without_click_or_retry(caplog) -> None:
    crawler, page, _candidates, button, modals = _submit_fixture()
    secret = "PRIVATE-WAIT-DOM-987654"
    modals.count.side_effect = RuntimeError(secret)

    crawler.submit_credentials_once(page)

    assert secret not in caplog.text
    button.click.assert_called_once_with(timeout=8000)
    page.click.assert_not_called()


@pytest.mark.parametrize("failing_wait", [5000, 1000])
def test_post_submit_wait_exception_is_fieldless_after_one_click(
    failing_wait: int,
    caplog,
) -> None:
    crawler, page, _candidates, button, _modals = _submit_fixture()
    secret = f"PRIVATE-WAIT-{failing_wait}-DOM-987654"

    def wait(milliseconds: int) -> None:
        if milliseconds == failing_wait:
            raise RuntimeError(secret)

    page.wait_for_timeout.side_effect = wait

    crawler.submit_credentials_once(page)

    assert secret not in caplog.text
    button.click.assert_called_once_with(timeout=8000)
    page.click.assert_not_called()


def test_login_prepare_and_authentication_are_thin_adapters(monkeypatch) -> None:
    page = Mock()
    crawler = _crawler()
    shared_login = Mock(return_value=True)
    logged_in = Mock(return_value=True)
    monkeypatch.setattr(crawler, "_shared_login", shared_login)
    monkeypatch.setattr(crawler, "_logged_in", logged_in)

    assert crawler.login(page)
    shared_login.assert_called_once_with(page)

    crawler.prepare_login_page(page)
    assert page.mock_calls == [call.wait_for_timeout(3500)]
    assert crawler.is_authenticated(page)
    logged_in.assert_called_once_with(page)


def test_authentication_inspection_does_not_log_url_or_body_metadata(capsys) -> None:
    page = Mock()
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


def test_missing_form_interstitial_fails_closed_without_global_overview_click(monkeypatch) -> None:
    manager, browser = _launch_browser()
    try:
        real_page = browser.new_page()
        real_page.set_content(
            """
            <p>您已經登入了</p>
            <button id="overview">我的總覽</button>
            <script>
              document.body.dataset.overviewClicks = '0';
              overview.onclick = () => document.body.dataset.overviewClicks++;
            </script>
            """
        )

        class PageProxy:
            def __getattr__(self, name):
                return getattr(real_page, name)

            def wait_for_selector(self, *_args, **_kwargs):
                raise RuntimeError("PRIVATE-INTERSTITIAL-DOM-987654")

        crawler = _crawler()
        crawler.creds = SimpleNamespace(
            national_id="ID-PRIVATE",
            user_code="USER-PRIVATE",
            password="PASSWORD-PRIVATE",
        )
        monkeypatch.setattr(crawler, "prepare_login_page", lambda _page: None)
        monkeypatch.setattr(crawler, "_logged_in", lambda _page: False)

        with pytest.raises(CtbcLoginError, match="欄位無法安全填寫.*未送出登入") as error:
            crawler._shared_login(PageProxy())

        assert "PRIVATE-INTERSTITIAL" not in str(error.value)
        assert real_page.locator("body").get_attribute("data-overview-clicks") == "0"
        assert not hasattr(CtbcCrawler, "_enter_overview_if_interstitial")
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_unknown_modal_blocks_shared_login_without_submission(monkeypatch) -> None:
    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        page.set_content('<div class="modal show">PRIVATE UNKNOWN</div>')
        crawler = _crawler()
        submissions = Mock()
        monkeypatch.setattr(crawler, "prepare_login_page", lambda _page: None)
        monkeypatch.setattr(crawler, "_logged_in", lambda _page: False)
        monkeypatch.setattr(crawler, "submit_credentials_once", submissions)

        with pytest.raises(LoginCheckpointBlocked) as error:
            crawler._shared_login(page)

        assert error.value.outcome.rule_name == "ctbc-unknown-modal"
        submissions.assert_not_called()
        assert "PRIVATE UNKNOWN" not in str(error.value)
    finally:
        browser.close()
        manager.__exit__(None, None, None)
