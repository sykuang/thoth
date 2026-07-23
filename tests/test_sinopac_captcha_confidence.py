from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.banks.sinopac import SinopacCrawler, SinopacLoginError


@pytest.fixture(autouse=True)
def _stub_sinopac_creds(monkeypatch):
    """Unit tests for login helpers must not require real bank credentials."""
    creds = SimpleNamespace(
        national_id="B123456789",
        user_code="test-user",
        password="SyntheticTestPassword05!",
    )
    monkeypatch.setattr("backend.banks.sinopac.SinopacCreds.load", lambda: creds)


class _FakeLoginPage:
    url = "https://mma.sinopac.com/MemberPortal/Member/MMALogin.aspx"

    def __init__(self, crawler: SinopacCrawler, messages: list[str], dom_errors: list[str] | None = None):
        self.crawler = crawler
        self.messages = iter(messages)
        self.dom_errors = dom_errors or []
        self.submit_count = 0

    def wait_for_timeout(self, _ms):
        return None

    def wait_for_selector(self, *_args, **_kwargs):
        return None

    def fill(self, *_args, **_kwargs):
        return None

    def evaluate(self, script):
        if "繼續使用" in script:
            return ""
        if "sid_id" in script:
            return {"sid": True, "user": True, "pwd": True, "cap": True}
        if "#MMA_Login" in script:
            self.submit_count += 1
            self.crawler._last_dialog_message = next(self.messages)
            return True
        if "const txt=" in script:
            return self.dom_errors
        return None


def test_sinopac_captcha_uses_confidence_gate(monkeypatch):
    calls = []

    def fake_wait(page, selector, *, tmp_path):
        calls.append(("wait", selector, tmp_path))

    def fake_solve(page, selector, **kwargs):
        calls.append(("solve", selector, kwargs))
        return "123456"

    monkeypatch.setattr("backend.banks.sinopac.wait_captcha_stable", fake_wait)
    monkeypatch.setattr("backend.banks.sinopac.solve_captcha", fake_solve)

    crawler = SinopacCrawler()
    assert crawler._ocr_with_regen(object(), max_attempts=1) == "123456"

    solve_call = next(c for c in calls if c[0] == "solve")
    assert solve_call[2]["expected_len"] == 6
    assert solve_call[2]["digits_only"] is True
    assert solve_call[2]["min_confidence"] == pytest.approx(0.98)


def test_sinopac_detects_explicit_captcha_login_error():
    crawler = SinopacCrawler()
    assert crawler._is_captcha_login_error("驗證碼失效或輸入錯誤，請重新輸入。") is True
    assert crawler._is_captcha_login_error("驗證碼錯誤") is True
    assert crawler._is_captcha_login_error("使用者代碼或網路密碼錯誤") is False


def test_sinopac_login_form_labels_are_not_error_codes():
    crawler = SinopacCrawler()
    labels = ["驗證碼", "使用者代碼或網路密碼", "忘記帳號或密碼？"]
    assert crawler._login_error_code("", labels) == crawler.LOGIN_FAILED


def test_sinopac_retries_once_only_for_explicit_captcha_error(monkeypatch):
    crawler = SinopacCrawler()
    page = _FakeLoginPage(crawler, ["驗證碼失效或輸入錯誤，請重新輸入。", ""])
    monkeypatch.setattr(crawler, "_ocr_with_regen", lambda _page, max_attempts: "123456")
    login_states = iter([False, True])
    monkeypatch.setattr(crawler, "_logged_in", lambda _page: next(login_states))

    assert crawler.login(page) is True
    assert page.submit_count == 2


def test_sinopac_credentials_error_has_code_and_is_not_retried(monkeypatch):
    crawler = SinopacCrawler()
    page = _FakeLoginPage(
        crawler,
        ["使用者代碼或網路密碼錯誤"],
        dom_errors=["驗證碼錯誤"],
    )
    monkeypatch.setattr(crawler, "_ocr_with_regen", lambda _page, max_attempts: "123456")
    monkeypatch.setattr(crawler, "_logged_in", lambda _page: False)
    monkeypatch.setattr("backend.banks._login_debug.snapshot", lambda _page: "snapshot")

    with pytest.raises(SinopacLoginError) as exc:
        crawler.login(page)

    assert exc.value.code == "credentials_invalid"
    assert str(exc.value).startswith("[credentials_invalid]")
    assert page.submit_count == 1


def test_sinopac_stops_after_second_captcha_error(monkeypatch):
    crawler = SinopacCrawler()
    message = "驗證碼失效或輸入錯誤，請重新輸入。"
    page = _FakeLoginPage(crawler, [message, message])
    monkeypatch.setattr(crawler, "_ocr_with_regen", lambda _page, max_attempts: "123456")
    monkeypatch.setattr(crawler, "_logged_in", lambda _page: False)
    monkeypatch.setattr("backend.banks._login_debug.snapshot", lambda _page: "snapshot")

    with pytest.raises(SinopacLoginError) as exc:
        crawler.login(page)

    assert exc.value.code == "captcha_invalid"
    assert page.submit_count == 2


def test_sinopac_unknown_error_is_not_retried(monkeypatch):
    crawler = SinopacCrawler()
    page = _FakeLoginPage(crawler, ["系統忙碌中"])
    monkeypatch.setattr(crawler, "_ocr_with_regen", lambda _page, max_attempts: "123456")
    monkeypatch.setattr(crawler, "_logged_in", lambda _page: False)
    monkeypatch.setattr("backend.banks._login_debug.snapshot", lambda _page: "snapshot")

    with pytest.raises(SinopacLoginError) as exc:
        crawler.login(page)

    assert exc.value.code == "login_failed"
    assert page.submit_count == 1
