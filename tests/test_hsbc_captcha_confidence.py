from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest

import backend.banks.hsbc as hsbc_module
from backend.banks.hsbc import HSBC_CAPTCHA_MIN_CONFIDENCE, HsbcCrawler


@pytest.fixture(autouse=True)
def _stub_hsbc_creds(monkeypatch):
    monkeypatch.setattr("backend.banks.hsbc.HsbcCreds.load", lambda: object())


def _crawler() -> HsbcCrawler:
    crawler = object.__new__(HsbcCrawler)
    crawler.name = "hsbc"
    crawler.creds = cast("object", SimpleNamespace(user_id="user", password="password"))
    return crawler


def _collection(node):
    result = Mock()
    result.count.return_value = 1
    result.nth.return_value = node
    return result


def _page(screenshot: bytes = b"stable"):
    image = Mock()
    image.is_visible.return_value = True
    image.get_attribute.return_value = "data:image/jpeg;base64,opaque"
    image.bounding_box.return_value = {"x": 0, "y": 0, "width": 128, "height": 40}
    image.screenshot.side_effect = [screenshot, screenshot]
    refreshes = Mock()
    refreshes.count.return_value = 0
    page = Mock()
    page.locator.side_effect = lambda selector: (
        _collection(image) if selector == "img" else refreshes
    )
    return page, image


def test_hsbc_native_screenshot_captcha_uses_confidence_gate(monkeypatch) -> None:
    calls = []

    def fake_ocr_bytes(raw, **kwargs):
        calls.append((raw, kwargs))
        return "yw2dp"

    monkeypatch.setattr(hsbc_module, "ocr_bytes", fake_ocr_bytes)
    page, image = _page()

    assert _crawler()._solve_captcha(page) == "yw2dp"
    assert calls == [(b"stable", {
        "expected_len": 5,
        "alnum_only": True,
        "min_confidence": HSBC_CAPTCHA_MIN_CONFIDENCE,
    })]
    assert image.screenshot.call_count == 2
    page.evaluate.assert_not_called()


def test_hsbc_native_screenshot_rejects_low_confidence_false_positive(monkeypatch) -> None:
    def fake_ocr_classification(raw, *, probability=False):
        if probability:
            return {"text": "yw2dp", "confidence": 0.10}
        return "yw2dp"

    monkeypatch.setattr("backend.core.captcha._ocr_classification", fake_ocr_classification)
    page, _image = _page()

    assert _crawler()._solve_captcha(page) is None
