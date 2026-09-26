"""Collector lifecycle checks with an empty native browser; no bank requests."""
from patchright.sync_api import sync_playwright
import pytest

from backend.core.base import ResponseCollector


@pytest.mark.parametrize("other_listener", [False, True])
def test_detach_twice_is_idempotent(other_listener):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            collector = ResponseCollector("card.hsbc.com.tw")
            collector.attach(page)
            if other_listener:
                page.on("response", lambda response: None)
            collector.detach(page)  # HSBC detaches before bounded API collection.
            collector.detach(page)  # Shared callback detaches again in finally.
            collector.attach(page)  # Sinopac temporarily detaches and reattaches.
            collector.detach(page)
            collector.detach(page)
        finally:
            browser.close()


@pytest.mark.parametrize("error_type", [RuntimeError, KeyError])
def test_detach_does_not_hide_listener_errors(error_type):
    from types import SimpleNamespace

    error = error_type("synthetic cleanup failure")

    def remove(*args):
        raise error

    collector = ResponseCollector("card.hsbc.com.tw")
    with pytest.raises(error_type) as raised:
        collector.detach(SimpleNamespace(remove_listener=remove))
    assert raised.value is error
    assert collector._detached is False


def test_detach_preserves_other_listeners_and_reattach_resumes_capture():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.route("**/*", lambda route: route.fulfill(
                content_type="text/html", body="<html>synthetic</html>",
            ))
            responses = []
            page.on("response", lambda response: responses.append(True))
            collector = ResponseCollector("collector.invalid")
            collector.attach(page)
            page.goto("https://collector.invalid/first")
            assert collector.request_sequence == 1
            collector.detach(page)
            collector.detach(page)
            page.goto("https://collector.invalid/detached")
            assert collector.request_sequence == 1
            collector.attach(page)
            page.goto("https://collector.invalid/reattached")
            assert collector.request_sequence == 2
            collector.detach(page)
            collector.detach(page)
            assert len(responses) == 3
        finally:
            browser.close()
