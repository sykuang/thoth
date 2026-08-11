from __future__ import annotations

from contextlib import nullcontext
from unittest.mock import Mock

import pytest

from backend.banks.rakuten import (
    RakutenCrawler,
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
