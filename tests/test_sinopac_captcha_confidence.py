from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import backend.banks.sinopac as sinopac_module
from backend.banks.sinopac import SinopacCrawler


def _crawler() -> SinopacCrawler:
    crawler = object.__new__(SinopacCrawler)
    crawler.name = "sinopac"
    crawler.creds = SimpleNamespace(
        national_id="B123456789",
        user_code="test-user",
        password="SyntheticTestPassword05!",
    )
    crawler.captcha_tmp = "/tmp/sinopac-test-captcha.png"
    return crawler


def _captcha_page() -> tuple[Mock, Mock]:
    page = Mock()
    image = Mock()
    image.is_visible.return_value = True
    image.is_enabled.return_value = True
    images = Mock()
    images.count.return_value = 1
    images.nth.return_value = image
    page.locator.return_value = images
    return page, image


def test_sinopac_captcha_uses_confidence_gate(monkeypatch):
    calls = []

    def fake_wait(page, selector, *, tmp_path):
        calls.append(("wait", selector, tmp_path))

    def fake_solve(page, selector, **kwargs):
        calls.append(("solve", selector, kwargs))
        return "123456"

    monkeypatch.setattr(sinopac_module, "wait_captcha_stable", fake_wait)
    monkeypatch.setattr(sinopac_module, "solve_captcha", fake_solve)
    page, image = _captcha_page()

    assert _crawler()._ocr_captcha(page, max_attempts=1) == "123456"

    solve_call = next(call for call in calls if call[0] == "solve")
    assert solve_call[2] == {
        "expected_len": 6,
        "alnum_only": True,
        "digits_only": True,
        "min_confidence": pytest.approx(0.98),
        "tmp_path": "/tmp/sinopac-test-captcha.png",
    }
    image.click.assert_not_called()


def test_sinopac_captcha_reads_five_times_and_refreshes_at_most_four(monkeypatch):
    page, image = _captcha_page()
    waits = Mock()
    solves = Mock(return_value=None)
    monkeypatch.setattr(sinopac_module, "wait_captcha_stable", waits)
    monkeypatch.setattr(sinopac_module, "solve_captcha", solves)

    assert _crawler()._ocr_captcha(page, max_attempts=99) is None
    assert waits.call_count == 5
    assert solves.call_count == 5
    assert image.click.call_count == 4
    assert page.wait_for_timeout.call_count == 4


def test_sinopac_captcha_ambiguity_and_browser_exceptions_fail_closed(monkeypatch):
    crawler = _crawler()
    page, image = _captcha_page()
    page.locator.return_value.count.return_value = 2
    solve = Mock(return_value="123456")
    monkeypatch.setattr(sinopac_module, "solve_captcha", solve)

    assert crawler._ocr_captcha(page) is None
    solve.assert_not_called()
    image.click.assert_not_called()

    page.locator.side_effect = RuntimeError("PRIVATE-CAPTCHA-987654")
    assert crawler._ocr_captcha(page) is None
