from __future__ import annotations

import pytest

from backend.banks.hsbc import HsbcCrawler


@pytest.fixture(autouse=True)
def _stub_hsbc_creds(monkeypatch):
    """Unit tests for login helpers must not require real bank credentials."""
    monkeypatch.setattr("backend.banks.hsbc.HsbcCreds.load", lambda: object())


class _FakePage:
    def __init__(
        self,
        data_url: str = "data:image/jpeg;base64,ZmFrZQ==",
        *,
        captcha_img_visible: bool = False,
    ):
        self.data_url = data_url
        self.captcha_img_visible = captcha_img_visible

    def evaluate(self, script):
        if "return im ? im.src" in script:
            return self.data_url
        if "data-captcha-img" in script:
            return self.captcha_img_visible
        raise AssertionError(f"unexpected evaluate script: {script[:80]!r}")


def test_hsbc_dom_captcha_uses_confidence_gate(monkeypatch):
    calls = []

    def fake_ocr_bytes(raw, **kwargs):
        calls.append((raw, kwargs))
        return "yw2dp"

    monkeypatch.setattr("backend.banks.hsbc.ocr_bytes", fake_ocr_bytes)

    crawler = HsbcCrawler()
    assert crawler._solve_captcha(_FakePage()) == "yw2dp"

    assert len(calls) == 1
    assert calls[0][1]["expected_len"] == 5
    assert calls[0][1]["alnum_only"] is True
    assert calls[0][1]["min_confidence"] > 0


def test_hsbc_dom_captcha_rejects_low_confidence_false_positive(monkeypatch):
    def fake_ocr_classification(raw, *, probability=False):
        if probability:
            return {"text": "yw2dp", "confidence": 0.10}
        return "yw2dp"

    monkeypatch.setattr("backend.core.captcha._ocr_classification", fake_ocr_classification)

    crawler = HsbcCrawler()
    assert crawler._solve_captcha(_FakePage()) is None


def test_hsbc_screenshot_captcha_uses_confidence_gate(monkeypatch):
    calls = []

    def fake_wait(page, selector, *, tmp_path):
        calls.append(("wait", selector, tmp_path))
        return True

    def fake_solve(page, selector, **kwargs):
        calls.append(("solve", selector, kwargs))
        return "ab12c"

    monkeypatch.setattr("backend.banks.hsbc.wait_captcha_stable", fake_wait)
    monkeypatch.setattr("backend.banks.hsbc.solve_captcha", fake_solve)

    crawler = HsbcCrawler()
    assert crawler._solve_captcha(_FakePage(data_url="", captcha_img_visible=True)) == "ab12c"

    solve_call = next(c for c in calls if c[0] == "solve")
    assert solve_call[2]["expected_len"] == 5
    assert solve_call[2]["alnum_only"] is True
    assert solve_call[2]["min_confidence"] > 0
    assert solve_call[2]["tmp_path"] == crawler.captcha_tmp
