from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from backend.banks.cathay import CathayCrawler, CathayLoginError
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointPhase,
    LoginCheckpointBlocked,
    evaluate_login_checkpoint,
)


def test_cathay_shared_login_api_and_rule_inventory() -> None:
    crawler = object.__new__(CathayCrawler)
    rules = crawler.login_checkpoint_rules()

    assert CathayCrawler.USES_SHARED_LOGIN_CHECKPOINTS is True
    assert len(rules) == 3
    rule = rules[0]
    assert (
        rule.name,
        rule.bank,
        rule.phases,
        rule.kind,
        rule.container_selector,
        rule.action_texts,
        rule.max_actions,
    ) == (
        "cathay-login-announcement",
        "cathay",
        (CheckpointPhase.PRE_SUBMIT,),
        CheckpointKind.DISMISSIBLE_NOTICE,
        "#divSystemLoginMsgList.show",
        ("下一", "下一則", "我知道了", "關閉", "確定"),
        12,
    )
    assert not hasattr(CathayCrawler, "_dismiss_announcements")
    assert [item.name for item in rules[1:]] == [
        "cathay-unknown-modal", "cathay-unknown-dialog"
    ]

    source = inspect.getsource(CathayCrawler)
    assert "NormalDataCheck" not in source
    assert "modal-backdrop" not in source
    assert ".style.display" not in source


def _evaluate(page):
    crawler = object.__new__(CathayCrawler)
    return evaluate_login_checkpoint(
        page,
        bank="cathay",
        phase=CheckpointPhase.PRE_SUBMIT,
        rules=crawler.login_checkpoint_rules(),
        is_authenticated=lambda _page: False,
    )


def test_cathay_rule_advances_only_the_scoped_announcement() -> None:
    from patchright.sync_api import sync_playwright

    with sync_playwright() as patchright:
        if not Path(patchright.chromium.executable_path).exists():
            pytest.skip("Patchright browser binary is not installed")
        browser = patchright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(
                """
                <button id="outside">確定</button>
                <div id="security-modal" role="dialog"><button>確定</button></div>
                <div id="divSystemLoginMsgList" class="show"><button>下一</button></div>
                <script>
                  document.body.dataset.outsideClicks = '0';
                  document.body.dataset.securityClicks = '0';
                  document.body.dataset.announcementClicks = '0';
                  document.querySelector('#outside').onclick = () =>
                    document.body.dataset.outsideClicks++;
                  document.querySelector('#security-modal button').onclick = () =>
                    document.body.dataset.securityClicks++;
                  const announcement = document.querySelector('#divSystemLoginMsgList');
                  announcement.querySelector('button').onclick = () => {
                    document.body.dataset.announcementClicks++;
                    if (announcement.textContent.trim() === '下一') {
                      announcement.innerHTML = '<button>我知道了</button>';
                      announcement.querySelector('button').onclick = () => {
                        document.body.dataset.announcementClicks++;
                        announcement.hidden = true;
                      };
                    }
                  };
                </script>
                """
            )

            first = _evaluate(page)
            assert first.kind is CheckpointKind.DISMISSIBLE_NOTICE
            assert first.action_label == "下一"
            assert page.locator("body").get_attribute("data-announcement-clicks") == "1"

            second = _evaluate(page)
            assert second.kind is CheckpointKind.DISMISSIBLE_NOTICE
            assert second.action_label == "我知道了"
            assert page.locator("body").get_attribute("data-announcement-clicks") == "2"

            assert _evaluate(page).kind is CheckpointKind.UNKNOWN_BLOCKER
            assert page.locator("body").get_attribute("data-outside-clicks") == "0"
            assert page.locator("body").get_attribute("data-security-clicks") == "0"
            assert page.locator("#security-modal").is_visible()

            page.set_content(
                '<div id="divSystemLoginMsgList" class="show" hidden><button>確定</button></div>'
            )
            assert _evaluate(page).kind is CheckpointKind.READY_FOR_CREDENTIALS

            page.set_content(
                '<div id="divSystemLoginMsgList"><button data-unshown>確定</button></div>'
            )
            assert _evaluate(page).kind is CheckpointKind.READY_FOR_CREDENTIALS
            assert page.locator("[data-unshown]").is_visible()
        finally:
            browser.close()


def test_cathay_rule_handles_current_previous_next_labels() -> None:
    from patchright.sync_api import sync_playwright

    with sync_playwright() as patchright:
        if not Path(patchright.chromium.executable_path).exists():
            pytest.skip("Patchright browser binary is not installed")
        browser = patchright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(
                """
                <div id="divSystemLoginMsgList" class="show">
                  <button id="previous">上一則</button>
                  <button id="next">下一則</button>
                </div>
                <script>
                  document.body.dataset.previousClicks = '0';
                  document.body.dataset.nextClicks = '0';
                  document.querySelector('#previous').onclick = () =>
                    document.body.dataset.previousClicks++;
                  document.querySelector('#next').onclick = () => {
                    document.body.dataset.nextClicks++;
                    document.querySelector('#divSystemLoginMsgList').insertAdjacentText('afterbegin', '第二頁 ');
                  };
                </script>
                """
            )

            outcome = _evaluate(page)
            assert outcome.kind is CheckpointKind.DISMISSIBLE_NOTICE
            assert outcome.action_label == "下一則"
            assert page.locator("body").get_attribute("data-previous-clicks") == "0"
            assert page.locator("body").get_attribute("data-next-clicks") == "1"
        finally:
            browser.close()


def test_cathay_rule_rejects_form_bearing_or_ambiguous_announcement() -> None:
    from patchright.sync_api import sync_playwright

    with sync_playwright() as patchright:
        if not Path(patchright.chromium.executable_path).exists():
            pytest.skip("Patchright browser binary is not installed")
        browser = patchright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            for body in (
                '<input value="private"><button onclick="this.dataset.clicked=\'yes\'">確定</button>',
                '<button onclick="this.dataset.clicked=\'yes\'">下一</button>'
                '<button onclick="this.dataset.clicked=\'yes\'">關閉</button>',
            ):
                page.set_content(
                    f'<div id="divSystemLoginMsgList" class="show">{body}</div>'
                )
                outcome = _evaluate(page)
                assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
                assert outcome.rule_name == "cathay-login-announcement"
                assert page.locator("[data-clicked]").count() == 0
        finally:
            browser.close()


def _set_announcement_pages(page, total: int) -> None:
    page.set_content(
        f"""
        <button id="outside">下一</button>
        <div id="divSystemLoginMsgList" class="show"><button>下一</button></div>
        <script>
          const announcement = document.querySelector('#divSystemLoginMsgList');
          const labels = ['下一', '我知道了', '關閉', '確定'];
          let announcementPage = 1;
          document.body.dataset.announcementClicks = '0';
          document.body.dataset.outsideClicks = '0';
          document.querySelector('#outside').onclick = () =>
            document.body.dataset.outsideClicks++;
          announcement.onclick = event => {{
            if (event.target.tagName !== 'BUTTON') return;
            document.body.dataset.announcementClicks++;
            if (announcementPage >= {total}) {{
              announcement.hidden = true;
              return;
            }}
            announcementPage++;
            announcement.innerHTML = `<button>${{labels[(announcementPage - 1) % labels.length]}}</button>`;
          }};
        </script>
        """
    )


def test_shared_login_blocks_visible_thirteenth_announcement_before_submit(
    monkeypatch,
) -> None:
    from patchright.sync_api import sync_playwright

    with sync_playwright() as patchright:
        if not Path(patchright.chromium.executable_path).exists():
            pytest.skip("Patchright browser binary is not installed")
        browser = patchright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            _set_announcement_pages(page, 13)
            crawler = object.__new__(CathayCrawler)
            crawler.name = "cathay"
            crawler._credential_origin_allowed = lambda _page: True
            submissions = 0

            def submit(_page) -> None:
                nonlocal submissions
                submissions += 1

            monkeypatch.setattr(crawler, "prepare_login_page", lambda _page: None)
            monkeypatch.setattr(crawler, "is_authenticated", lambda _page: False)
            monkeypatch.setattr(crawler, "submit_credentials_once", submit)

            with pytest.raises(LoginCheckpointBlocked) as error:
                crawler._shared_login(page)

            assert error.value.outcome.rule_name == "cathay-login-announcement"
            assert submissions == 0
            assert page.locator("body").get_attribute("data-announcement-clicks") == "12"
            assert page.locator("body").get_attribute("data-outside-clicks") == "0"
            assert page.locator("#divSystemLoginMsgList").is_visible()
        finally:
            browser.close()


def test_shared_login_submits_after_twelfth_announcement_hides(monkeypatch) -> None:
    from patchright.sync_api import sync_playwright

    with sync_playwright() as patchright:
        if not Path(patchright.chromium.executable_path).exists():
            pytest.skip("Patchright browser binary is not installed")
        browser = patchright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            _set_announcement_pages(page, 12)
            crawler = object.__new__(CathayCrawler)
            crawler.name = "cathay"
            crawler._credential_origin_allowed = lambda _page: True
            submissions = 0

            def submit(_page) -> None:
                nonlocal submissions
                submissions += 1

            monkeypatch.setattr(crawler, "prepare_login_page", lambda _page: None)
            monkeypatch.setattr(crawler, "is_authenticated", lambda _page: submissions == 1)
            monkeypatch.setattr(crawler, "submit_credentials_once", submit)

            assert crawler._shared_login(page) is True
            assert submissions == 1
            assert page.locator("body").get_attribute("data-announcement-clicks") == "12"
            assert page.locator("body").get_attribute("data-outside-clicks") == "0"
            assert not page.locator("#divSystemLoginMsgList").is_visible()
        finally:
            browser.close()


def _submit_fixture(
    *,
    count: int = 1,
    visible: bool = True,
    enabled: bool = True,
    classes: str = "js-login",
    click_error: Exception | None = None,
):
    page = Mock()
    values = {
        "#CustID": "CUST-PRIVATE",
        "#UserIdKeyin": "USER-PRIVATE",
        "#PasswordKeyin": "PASSWORD-PRIVATE",
    }
    fields = {selector: Mock() for selector in values}
    for selector, field in fields.items():
        field.count.return_value = 1
        field.nth.return_value = field
        field.is_visible.return_value = True
        field.is_enabled.return_value = True
        field.input_value.return_value = values[selector]
    candidates = Mock()
    button = Mock()
    candidates.count.return_value = count
    candidates.first = button
    button.is_visible.return_value = visible
    button.is_enabled.return_value = enabled
    button.get_attribute.return_value = classes
    button.click.side_effect = click_error
    page.locator.side_effect = lambda selector: (
        candidates if selector == "button.js-login" else fields[selector]
    )
    page.test_fields = fields

    crawler = object.__new__(CathayCrawler)
    crawler.creds = SimpleNamespace(
        cust_id="CUST-PRIVATE",
        user_id="USER-PRIVATE",
        password="PASSWORD-PRIVATE",
    )
    return crawler, page, button


def test_submit_uses_true_keyboard_and_clicks_once_with_original_timing() -> None:
    crawler, page, button = _submit_fixture()

    crawler.submit_credentials_once(page)

    page.wait_for_selector.assert_called_once_with(
        "#CustID", state="visible", timeout=12000
    )
    for field in page.test_fields.values():
        assert field.method_calls == [
            call.count(), call.nth(0), call.is_visible(), call.is_enabled(),
            call.click(), call.click(click_count=3), call.input_value(),
        ]
    assert page.keyboard.press.call_args_list == [call("Backspace")] * 3
    assert page.keyboard.type.call_args_list == [
        call("CUST-PRIVATE", delay=80),
        call("USER-PRIVATE", delay=80),
        call("PASSWORD-PRIVATE", delay=80),
    ]
    assert page.wait_for_timeout.call_args_list == [
        ((200,),),
        ((200,),),
        ((200,),),
        ((9000,),),
    ]
    assert call("button.js-login") in page.locator.call_args_list
    button.click.assert_called_once_with(timeout=8000)
    page.fill.assert_not_called()
    page.click.assert_not_called()
    page.evaluate.assert_not_called()


@pytest.mark.parametrize(
    ("count", "visible", "enabled", "classes"),
    [
        (0, True, True, "js-login"),
        (2, True, True, "js-login"),
        (1, False, True, "js-login"),
        (1, True, False, "js-login"),
        (1, True, True, "js-login disabled"),
    ],
)
def test_submit_missing_ambiguous_hidden_or_disabled_action_sends_zero(
    count: int,
    visible: bool,
    enabled: bool,
    classes: str,
) -> None:
    crawler, page, button = _submit_fixture(
        count=count,
        visible=visible,
        enabled=enabled,
        classes=classes,
    )

    with pytest.raises(CathayLoginError, match="未送出登入"):
        crawler.submit_credentials_once(page)
    button.click.assert_not_called()
    page.click.assert_not_called()
    page.evaluate.assert_not_called()


def test_submit_click_timeout_after_dispatch_is_sanitized_without_second_click(
    caplog,
) -> None:
    secret = "PRIVATE-DOM-BODY-987654"
    crawler, page, button = _submit_fixture(
        click_error=TimeoutError(secret)
    )

    with pytest.raises(CathayLoginError, match="送出狀態不明.*禁止自動重試") as error:
        crawler.submit_credentials_once(page)
    button.click.assert_called_once_with(timeout=8000)
    page.click.assert_not_called()
    page.evaluate.assert_not_called()
    assert secret not in str(error.value)
    assert secret not in caplog.text


def test_submit_button_inspection_error_is_sanitized_before_click(caplog) -> None:
    secret = "PRIVATE-BUTTON-DOM-987654"
    crawler, page, button = _submit_fixture()
    def locate(selector):
        if selector == "button.js-login":
            raise RuntimeError(secret)
        return page.test_fields[selector]

    page.locator.side_effect = locate

    with pytest.raises(CathayLoginError, match="無法安全確認.*未送出登入") as error:
        crawler.submit_credentials_once(page)

    button.click.assert_not_called()
    assert secret not in str(error.value)
    assert secret not in caplog.text


@pytest.mark.parametrize("failure", ["wait", "keyboard"])
def test_field_failure_sends_zero_without_disclosing_secrets(
    failure: str, caplog
) -> None:
    crawler, page, button = _submit_fixture()
    secret = "SECRET-FROM-DOM-987654"
    if failure == "wait":
        page.wait_for_selector.side_effect = RuntimeError(secret)
    else:
        page.test_fields["#UserIdKeyin"].click.side_effect = RuntimeError(secret)

    with pytest.raises(CathayLoginError) as error:
        crawler.submit_credentials_once(page)

    assert secret not in str(error.value)
    assert "CUST-PRIVATE" not in str(error.value)
    assert "USER-PRIVATE" not in str(error.value)
    assert "PASSWORD-PRIVATE" not in str(error.value)
    assert secret not in caplog.text
    button.click.assert_not_called()
    page.evaluate.assert_not_called()


def test_login_prepare_and_authentication_are_thin_adapters(monkeypatch) -> None:
    page = Mock()
    crawler = object.__new__(CathayCrawler)
    shared_login = Mock(return_value=True)
    logged_in = Mock(return_value=True)
    monkeypatch.setattr(crawler, "_shared_login", shared_login)
    monkeypatch.setattr(crawler, "_logged_in", logged_in)

    assert crawler.login(page)
    shared_login.assert_called_once_with(page)

    crawler.prepare_login_page(page)
    assert page.mock_calls == [call.wait_for_timeout(2500)]
    assert crawler.is_authenticated(page)
    logged_in.assert_called_once_with(page)


def test_authentication_requires_exact_https_cathay_origin() -> None:
    page = Mock()
    page.evaluate.return_value = {"ok": True}
    crawler = object.__new__(CathayCrawler)

    page.url = "https://www.cathaybk.com.tw/OnlineBanking/home"
    assert crawler._logged_in(page) is True
    for unsafe in (
        "http://www.cathaybk.com.tw/OnlineBanking/home",
        "https://www.cathaybk.com.tw.evil.example/OnlineBanking/home",
        "data:text/html,/OnlineBanking/home",
    ):
        page.url = unsafe
        assert crawler._logged_in(page) is False
