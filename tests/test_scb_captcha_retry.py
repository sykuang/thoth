from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.banks.scb import ScbCrawler, ScbLoginError


@pytest.fixture(autouse=True)
def _stub_scb_creds(monkeypatch):
    creds = SimpleNamespace(
        national_id="TEST000000",
        username="testusr",
        password="TestPass1234",
    )
    monkeypatch.setattr("backend.banks.scb.ScbCreds.load", lambda: creds)


class _FakeLocator:
    def click(self, **_kwargs):
        return None


class _FakeKeyboard:
    def press(self, _key):
        return None

    def type(self, _value, **_kwargs):
        return None


class _FakeLoginPage:
    def __init__(self):
        self.url = "https://ebank.standardchartered.com.tw/scb/public/login?redirect=%2F"
        self.keyboard = _FakeKeyboard()
        self.submit_count = 0

    def goto(self, _url, **_kwargs):
        self.url = "https://ebank.standardchartered.com.tw/scb/public/login?redirect=%2F"

    def wait_for_timeout(self, _ms):
        return None

    def wait_for_selector(self, *_args, **_kwargs):
        return None

    def locator(self, _selector):
        return _FakeLocator()

    def screenshot(self, **_kwargs):
        return None

    def evaluate(self, script, _arg=None):
        if "visible = [...document.querySelectorAll('input')]" in script:
            return [
                {"name": "dynamic-id", "type": "text", "maxlen": -1},
                {"name": "dynamic-user", "type": "password", "maxlen": 12},
                {"name": "dynamic-password", "type": "password", "maxlen": 12},
                {"name": "__reCaptcha", "type": "tel", "maxlen": 6},
            ]
        if "id_len: get(args.id_name)" in script:
            return {"id_len": 10, "user_len": 7, "pwd_len": 12, "cap_len": 6}
        if "login_btn_not_found" in script:
            self.submit_count += 1
            return {"ok": True, "cls": "m-button b-bg-green-d"}
        if "未正常登出" in script:
            return {"found": False}
        if r"CAPT\d" in script:
            return "CAPT001:驗證碼錯誤，請重新輸入"
        return None


def test_scb_retries_once_only_for_explicit_capt001(monkeypatch):
    crawler = ScbCrawler()
    page = _FakeLoginPage()
    monkeypatch.setattr(crawler, "_logged_in", lambda _page: False)
    monkeypatch.setattr(crawler, "_ocr_captcha", lambda _page, max_attempts: "123456")
    monkeypatch.setattr("backend.banks._login_debug.snapshot", lambda _page: "snapshot")

    with pytest.raises(ScbLoginError) as exc:
        crawler.login(page)

    assert page.submit_count == 2
    assert "CAPT001" in str(exc.value)
    assert "未知原因" not in str(exc.value)
