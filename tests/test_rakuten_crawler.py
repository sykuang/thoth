from __future__ import annotations

from contextlib import nullcontext
from unittest.mock import Mock, call

import pytest

from backend.banks.rakuten import (
    LOADER_SELECTOR,
    RakutenCrawler,
    RakutenLoginError,
    _account_number,
    _click_visible_login,
    _is_twd_query_request,
    _month_labels,
    _row_from_dom,
    _selection_matches,
    _six_month_labels,
    _unique_option_index,
    _view_ready,
)


def test_account_number_accepts_display_separators() -> None:
    assert _account_number("812-3456-7890-123") == "81234567890123"
    assert _account_number("請選擇帳戶") is None
    assert _selection_matches(
        "simple-dropdown2",
        "帳號 81234567890123",
        "81234567890123",
    )
    assert not _selection_matches(
        "simple-dropdown",
        "2026/06 活存明細",
        "2026/05 活存明細",
    )
    assert _unique_option_index([
        "81234567890123",
        "81234567890124",
    ], "81234567890124") == 1
    assert _unique_option_index([
        "81234567890123",
        "81234567890123",
    ], "81234567890123") is None


def test_login_recovers_public_startup_connection_modal_before_using_session(monkeypatch) -> None:
    events: list[str] = []
    page, popup, hidden, reload_link = Mock(), Mock(), Mock(), Mock()
    popup.count.return_value = reload_link.count.return_value = 1
    hidden.count.return_value = 0
    popup.locator.return_value = reload_link
    reload_link.filter.return_value = reload_link
    reload_link.first = reload_link
    reload_link.is_visible.return_value = True
    reload_link.click.side_effect = lambda: events.append("reload")
    page.locator.side_effect = [popup, hidden]
    page.wait_for_timeout.side_effect = lambda milliseconds: events.append(f"wait:{milliseconds}")
    page.expect_navigation.side_effect = (
        lambda **_kwargs: events.append("expect-navigation") or nullcontext()
    )
    crawler = object.__new__(RakutenCrawler)
    monkeypatch.setattr(crawler, "_resolve_known_blocking_modals", lambda _page: False)
    monkeypatch.setattr(crawler, "_blocking_modal_text", lambda _page: None)
    monkeypatch.setattr(crawler, "_logged_in", lambda _page: events.append("logged-in") or True)

    assert crawler.login(page)
    assert events == [
        "wait:20000",
        "expect-navigation",
        "reload",
        "wait:20000",
        "logged-in",
    ]
    page.expect_navigation.assert_called_once_with(wait_until="domcontentloaded", timeout=30000)
    popup.locator.assert_called_once_with("a.btn.btn-primary:visible")
    assert reload_link.filter.call_args.kwargs["has_text"].fullmatch("重新載入")


def test_login_resolves_only_visible_duplicate_session_modal() -> None:
    events: list[str] = []

    class Body:
        @staticmethod
        def inner_text() -> str:
            return (
                "帳號重複登入\n您已在其他裝置登入，繼續登入將會\n"
                "登出前一個裝置，是否以此裝置登入？"
            )

    class Button:
        @staticmethod
        def filter(**kwargs):
            assert kwargs["has_text"].fullmatch("是，我要登入")
            return Button()

        @staticmethod
        def count() -> int:
            return 1

        @property
        def first(self):
            return self

        @staticmethod
        def is_visible() -> bool:
            return True

        @staticmethod
        def click() -> None:
            events.append("click-duplicate")

    class Modal:
        @staticmethod
        def count() -> int:
            return 1

        @staticmethod
        def nth(_index: int):
            return Modal()

        @staticmethod
        def inner_text() -> str:
            return "帳號重複登入…否，不要登入是，我要登入"

        @staticmethod
        def locator(selector: str):
            if selector == ".modal-body":
                return Body()
            assert selector == "a:visible, button:visible, [role=button]:visible"
            return Button()

    class Page:
        @staticmethod
        def locator(selector: str) -> Modal:
            assert selector == "modal-confirm .modal.show:visible, modal-projection .modal.show:visible"
            return Modal()

        @staticmethod
        def wait_for_timeout(milliseconds: int) -> None:
            events.append(f"wait:{milliseconds}")

    crawler = object.__new__(RakutenCrawler)
    assert crawler._resolve_duplicate_login_modal(Page())
    assert events == ["click-duplicate", "wait:5000"]


def test_duplicate_like_unknown_modal_is_not_confirmed() -> None:
    class Body:
        @staticmethod
        def inner_text() -> str:
            return "重複登入安全提醒\n請洽客服確認後續操作"

    class Modal:
        @staticmethod
        def count() -> int:
            return 1

        @staticmethod
        def nth(_index: int):
            return Modal()

        @staticmethod
        def inner_text() -> str:
            return "重複登入安全提醒\n請洽客服確認後續操作"

        @staticmethod
        def locator(selector: str) -> Body:
            assert selector == ".modal-body"
            return Body()

    class Page:
        @staticmethod
        def locator(selector: str) -> Modal:
            assert selector == "modal-confirm .modal.show:visible, modal-projection .modal.show:visible"
            return Modal()

    crawler = object.__new__(RakutenCrawler)
    assert not crawler._resolve_duplicate_login_modal(Page())


def test_known_referral_modal_clicks_only_later_action() -> None:
    events: list[str] = []
    page, modals, modal, body, actions, later = (Mock() for _ in range(6))
    page.locator.return_value = modals
    modals.count.return_value = 1
    modals.nth.return_value = modal
    body.inner_text.return_value = (
        "推薦獎金NT$500無上限+抽沖繩來回機票，新戶也享NT$300現金~\n"
        "★推薦禮：活動說明\n活動期間: 2026/7/1~2026/9/30"
    )
    modal.locator.side_effect = lambda selector: body if selector == ".modal-body" else actions
    actions.filter.return_value = actions
    actions.count.return_value = 1
    actions.first = later
    later.is_visible.return_value = True
    later.click.side_effect = lambda: events.append("later")
    modal.wait_for.side_effect = lambda **kwargs: events.append(
        f"wait:{kwargs['state']}:{kwargs['timeout']}"
    )
    crawler = object.__new__(RakutenCrawler)

    assert crawler._dismiss_referral_promo(page)
    assert events == ["later", "wait:hidden:10000"]
    assert actions.filter.call_args.kwargs["has_text"].fullmatch("稍後再看")


def test_hidden_static_modals_are_not_blockers() -> None:
    class Hidden:
        @staticmethod
        def count() -> int:
            return 0

    class Page:
        @staticmethod
        def locator(selector: str) -> Hidden:
            assert selector == (
                "modal-confirm .modal.show:visible, modal-projection .modal.show:visible, "
                "#ib_init_connect_error_popup:visible"
            )
            return Hidden()

    assert RakutenCrawler._blocking_modal_text(Page()) is None


def test_session_ready_rejects_unknown_visible_blocking_modal(monkeypatch) -> None:
    class Modal:
        @staticmethod
        def count() -> int:
            return 1

        @property
        def first(self):
            return self

        @staticmethod
        def inner_text() -> str:
            return "尚有未處理的銀行提示\n取消\n確認"

    class Page:
        @staticmethod
        def locator(selector: str) -> Modal:
            assert selector == (
                "modal-confirm .modal.show:visible, modal-projection .modal.show:visible, "
                "#ib_init_connect_error_popup:visible"
            )
            return Modal()

    crawler = object.__new__(RakutenCrawler)
    monkeypatch.setattr(crawler, "_resolve_known_blocking_modals", lambda _page: False)
    monkeypatch.setattr(crawler, "_logged_in", lambda _page: True)

    with pytest.raises(RakutenLoginError, match="尚有未處理的銀行提示"):
        crawler._session_ready(Page())


def test_login_resolves_duplicate_session_before_accepting_logged_in_shell(monkeypatch) -> None:
    events: list[str] = []
    page = Mock()
    page.wait_for_timeout.side_effect = lambda milliseconds: events.append(f"wait:{milliseconds}")
    crawler = object.__new__(RakutenCrawler)
    monkeypatch.setattr(
        crawler,
        "_recover_startup_connection",
        lambda _page: events.append("recover-startup"),
    )
    monkeypatch.setattr(
        crawler,
        "_resolve_known_blocking_modals",
        lambda _page: events.append("resolve-known") or True,
    )
    monkeypatch.setattr(crawler, "_blocking_modal_text", lambda _page: None)
    monkeypatch.setattr(
        crawler,
        "_logged_in",
        lambda _page: events.append("logged-in") or True,
    )

    assert crawler.login(page)
    assert events == [
        "wait:20000",
        "recover-startup",
        "resolve-known",
        "logged-in",
    ]


def test_login_click_requires_one_visible_enabled_button() -> None:
    class FakeLocator:
        def __init__(self, *, count: int = 1, visible: bool = True, classes: str = ""):
            self._count = count
            self._visible = visible
            self._classes = classes
            self.clicks = 0

        def filter(self, **_kwargs):
            return self

        def count(self) -> int:
            return self._count

        @property
        def first(self):
            return self

        def get_attribute(self, _name: str) -> str:
            return self._classes

        def is_visible(self) -> bool:
            return self._visible

        def click(self) -> None:
            self.clicks += 1

    class FakePage:
        def __init__(self, locator: FakeLocator):
            self.button = locator

        def locator(self, selector: str) -> FakeLocator:
            assert selector == "a.btn.btn-primary:visible"
            return self.button

    hidden = FakeLocator(visible=False)
    disabled = FakeLocator(classes="btn disabled")
    duplicate = FakeLocator(count=2)
    enabled = FakeLocator(classes="btn btn-primary")

    assert not _click_visible_login(FakePage(hidden))
    assert not _click_visible_login(FakePage(disabled))
    assert not _click_visible_login(FakePage(duplicate))
    assert _click_visible_login(FakePage(enabled))
    assert hidden.clicks == disabled.clicks == duplicate.clicks == 0
    assert enabled.clicks == 1


def test_logout_confirms_bank_modal_before_reporting_success() -> None:
    events: list[str] = []
    page, frame, links, link, modals, stale, modal, body, buttons, button = (Mock() for _ in range(10))
    page.frames = [frame]
    frame.locator.return_value = links
    links.filter.return_value = links
    links.count.return_value = 1
    links.nth.return_value = link
    link.is_visible.return_value = True
    link.click.side_effect = lambda: events.append("open-logout")
    page.locator.return_value = modals
    modals.count.return_value = 2
    modals.nth.side_effect = [stale, modal]
    stale.inner_text.return_value = "其他可見提示\n取消\n確認"
    stale.locator.return_value.inner_text.return_value = "其他可見提示"
    modal.inner_text.return_value = "登出網路銀行…取消確認"
    body.inner_text.return_value = "登出網路銀行\n確認登出本系統？"
    modal.locator.side_effect = lambda selector: body if selector == ".modal-body" else buttons
    buttons.filter.return_value = buttons
    buttons.count.return_value = 1
    buttons.first = button
    button.is_visible.return_value = True
    button.click.side_effect = lambda: events.append("confirm-logout")
    page.wait_for_selector.side_effect = lambda selector, **kwargs: events.append(
        f"wait:{selector}:{kwargs['state']}:{kwargs['timeout']}"
    )
    crawler = object.__new__(RakutenCrawler)

    assert crawler.logout(page)
    assert events == [
        "open-logout",
        "wait:modal-confirm .modal.show:visible, modal-projection .modal.show:visible:visible:10000",
        "confirm-logout",
        "wait:#custNo:visible:30000",
    ]


def test_goto_twd_recovers_one_late_duplicate_modal_before_retrying(monkeypatch) -> None:
    events: list[str] = []
    page, nav, subnav = Mock(), Mock(), Mock()
    nav.first = nav
    nav.click.side_effect = [RuntimeError("modal intercepted click"), None]
    page.get_by_role.return_value = nav
    page.locator.return_value = subnav
    subnav.first = subnav
    subnav.click.side_effect = lambda: events.append("click-twd")
    page.wait_for_selector.side_effect = lambda selector, **kwargs: events.append(
        f"wait:{selector}:{kwargs['state']}:{kwargs['timeout']}"
    )
    page.wait_for_url.side_effect = lambda _predicate, **kwargs: events.append(
        f"wait-url:{kwargs['timeout']}"
    )
    crawler = object.__new__(RakutenCrawler)
    monkeypatch.setattr(
        crawler,
        "_resolve_known_blocking_modals",
        lambda _page: events.append("resolve-late-modal") or True,
    )
    monkeypatch.setattr(crawler, "_blocking_modal_text", lambda _page: None)

    crawler._goto_twd(page)

    assert nav.click.call_args_list == [call(timeout=5000), call(timeout=5000)]
    assert events == [
        f"wait:{LOADER_SELECTOR}:hidden:60000",
        "resolve-late-modal",
        "wait:a.sub-nav-link:has-text('臺幣存款'):visible:15000",
        "click-twd",
        "wait-url:30000",
    ]


def test_query_request_and_dom_readiness_fail_closed() -> None:
    class Request:
        def __init__(self, url: str, method: str = "POST"):
            self.url = url
            self.method = method

    endpoint = (
        "https://www.rakuten-bank.com.tw/ixtein/adapters/ebank/txns/"
        "channel-ctw/CTWQU0001/011"
    )
    assert _is_twd_query_request(Request(endpoint))
    assert not _is_twd_query_request(Request(endpoint, method="GET"))
    assert not _is_twd_query_request(Request(
        "https://www.rakuten-bank.com.tw/ixtein/adapters/ebank/txns/"
        "channel-ctw/CTWQU0001/010",
    ))
    assert not _is_twd_query_request(Request(
        "https://evil.example/telemetry?target=/channel-ctw/CTWQU0001/011",
    ))
    assert not _is_twd_query_request(Request(f"{endpoint};evil"))

    initial = {
        "rows": "current rows",
        "account": "帳號 81234567890123",
        "month": "2026/07 活存明細",
        "balance": "NT$ 0",
        "noData": False,
    }
    assert _view_ready(None, initial)
    assert not _view_ready(None, {**initial, "rows": ""})
    assert _view_ready(None, {**initial, "rows": "", "noData": True})
    assert not _view_ready("old rows", {**initial, "rows": "old rows"})
    assert _view_ready("old rows", {**initial, "rows": "new rows"})
    assert not _view_ready("old rows", {**initial, "rows": ""})
    assert _view_ready("old rows", {**initial, "rows": "", "noData": True})


def test_select_waits_for_bound_request_and_loader_transition(monkeypatch) -> None:
    events: list[str] = []
    label = "2026/06 活存明細"

    class Target:
        def count(self) -> int:
            return 1

        def nth(self, _index: int):
            return self

        def inner_text(self) -> str:
            return label

        def click(self) -> None:
            events.append("click")

    class Response:
        status = 200

        def finished(self) -> None:
            events.append("response-finished")

    class Request:
        method = "POST"
        url = (
            "https://www.rakuten-bank.com.tw/ixtein/adapters/ebank/txns/"
            "channel-ctw/CTWQU0001/011"
        )

        def response(self) -> Response:
            events.append("bound-response")
            return Response()

    class ExpectedRequest:
        value = Request()

        def __enter__(self):
            events.append("expect-request")
            return self

        def __exit__(self, *_args) -> None:
            events.append("request-captured")

    class Page:
        keyboard = object()

        def locator(self, _selector: str) -> Target:
            return Target()

        def wait_for_timeout(self, _milliseconds: int) -> None:
            pass

        def wait_for_selector(self, _selector: str, *, state: str, timeout: int) -> None:
            events.append(f"loader-{state}")

        def expect_request(self, predicate, *, timeout: int) -> ExpectedRequest:
            assert predicate(Request())
            return ExpectedRequest()

    crawler = object.__new__(RakutenCrawler)
    monkeypatch.setattr(crawler, "_open_dropdown", lambda *_args: None)
    monkeypatch.setattr(crawler, "_twd_view_state", lambda *_args: {"rows": ""})
    monkeypatch.setattr(crawler, "_wait_for_twd_view", lambda *_args, **_kwargs: events.append("dom-ready"))

    crawler._select_label(Page(), "simple-dropdown", label)
    assert events == [
        "loader-hidden",
        "expect-request",
        "click",
        "loader-visible",
        "request-captured",
        "bound-response",
        "response-finished",
        "loader-hidden",
        "dom-ready",
    ]


def test_month_labels_require_canonical_months() -> None:
    labels = [
        "請選擇",
        "2026/07 活存明細",
        "2026/06 活存明細",
        "2026/06 活存明細",
        "2026/05 活存明細",
        "2026/04 活存明細",
        "2026/03 活存明細",
        "2026/02 活存明細",
        "自訂區間",
        "2026/01 其他明細",
    ]

    expected = [
        "2026/07 活存明細",
        "2026/06 活存明細",
        "2026/05 活存明細",
        "2026/04 活存明細",
        "2026/03 活存明細",
        "2026/02 活存明細",
    ]
    assert _month_labels(labels) == expected
    assert _six_month_labels(labels) == expected
    with pytest.raises(RuntimeError):
        _six_month_labels(expected[:5])
    with pytest.raises(RuntimeError):
        _six_month_labels([*expected, "2026/01 活存明細"])


def test_row_from_dom_maps_the_six_bank_columns() -> None:
    assert _row_from_dom([
        "2026/07/26\n09:30:00",
        "跨行轉入\n王小明 81200000000000",
        "1,500",
        "",
        "12,345",
        "薪資",
    ]) == {
        "sysDate": "2026/07/26",
        "sysTime": "09:30:00",
        "txDesc": "跨行轉入",
        "nickNameOrAcct": "王小明 81200000000000",
        "amt": "1,500",
        "amtSign": True,
        "balance": "12,345",
        "memo": "薪資",
    }

    assert _row_from_dom([
        "2026/07/25 18:05:00", "轉帳支出", "", "200", "10,845", "",
    ])["amtSign"] is False


def test_scrape_twd_page_returns_only_normalized_fields() -> None:
    class FakePage:
        @staticmethod
        def evaluate(_script: str) -> dict:
            return {
                "accountLabel": "帳號 812-3456-7890-123",
                "balance": "NT$ 0",
                "rows": [[
                    "2026/07/26\n09:30:00",
                    "跨行轉入\n王小明 81200000000000",
                    "1,500",
                    "",
                    "12,345",
                    "薪資",
                ]],
            }

    result = RakutenCrawler._scrape_twd_page(FakePage())
    assert set(result) == {"account_no", "accounts", "txDetails"}
    assert result["account_no"] == "81234567890123"
    assert result["accounts"] == [{
        "acctNo": "81234567890123",
        "balance": "NT$ 0",
    }]
    assert result["txDetails"][0]["txDesc"] == "跨行轉入"
