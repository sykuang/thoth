"""Offline shared-core contract for the E.SUN main-branch backport."""
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from backend.core import base
from backend.core.login_checkpoints import (
    CheckpointKind, CheckpointPhase, LoginCheckpointRule, evaluate_login_checkpoint,
)


class Page:
    def __init__(self):
        self.url = "https://ebank.esunbank.com.tw/index.jsp"
        self.listeners = {}
        self.frames = []

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event, callback):
        self.listeners[event].remove(callback)


@dataclass
class Crawler(base.BankCrawler):
    CREDENTIAL_HOSTS = frozenset({"ebank.esunbank.com.tw"})
    events: list[str] = field(default_factory=list)
    outcome: base.BankCollectResult = field(default_factory=lambda: base.BankCollectResult(card_bill_facts_ok=False))
    move_after_collect: bool = False
    dialog_after_collect: bool = False
    reject_publication: bool = False
    collector_instance: base.ResponseCollector | None = None

    def login(self, page):
        return self._shared_login(page)

    def _host_filter(self):
        return "esunbank.com.tw"

    def _make_collector(self, page):
        self.events.append("make")
        self.collector_instance = base.ResponseCollector(self._host_filter())
        return self.collector_instance

    def _shared_login(self, page):
        assert self.collector is self.collector_instance
        assert len(page.listeners.get("response", [])) == 1
        self.events.append("login")
        return True

    def collect(self, page, collector):
        assert collector is self.collector_instance
        self.events.append("collect")
        if self.move_after_collect:
            page._target.url = "https://foreign.invalid/"  # guarded proxy
        if self.dialog_after_collect:
            self._shared_dialog_blocked = True
        return self.outcome

    def _validate_collect_publication(self, collector):
        assert collector is self.collector_instance
        self.events.append("proof")
        if self.reject_publication:
            raise RuntimeError("PRIVATE-RESPONSE-BODY")

    def logout(self, page):
        self.events.append("logout")
        return True


def run_offline(monkeypatch, tmp_path, crawler, *, repeat=False):
    crawler.session_dir = tmp_path / "session"
    crawler.session_dir.mkdir()
    page = Page()
    monkeypatch.setattr(crawler, "_enforce_session_freshness", lambda: None)
    monkeypatch.setattr(crawler, "_build_fetch_kwargs", lambda: {"__cleanups__": []})

    def execute(url, *, headless, page_action, fetch_kwargs):
        page_action(page)
        if repeat:
            page_action(page)

    monkeypatch.setattr(crawler, "_execute_browser_flow", execute)
    return crawler.run(page.url, headless=True), page


def test_prelogin_collector_final_proof_and_detach(monkeypatch, tmp_path):
    crawler = Crawler(name="esun")
    result, page = run_offline(monkeypatch, tmp_path, crawler)
    assert result == {"data": {"card_bill_facts_ok": False}}
    assert crawler.events == ["make", "login", "collect", "proof", "logout", "proof"]
    assert page.listeners["response"] == []
    assert page.listeners["request"] == []
    assert page.listeners["requestfailed"] == []


@pytest.mark.parametrize('registered', [False, True])
def test_diagnostic_state_never_executes_adapter_dict_descriptor(monkeypatch, tmp_path, capsys, registered):
    accesses = []

    class Hostile(Crawler):
        SAFE_COLLECT_PHASES = frozenset({'capture_publication'}) if registered else frozenset()

        @property
        def __dict__(self):
            accesses.append(True)
            print('PRIVATE-DESCRIPTOR-PAYLOAD')
            return {'_esun_spa_phase': 'capture_publication'}

    crawler = Hostile(name='esun', reject_publication=True)
    crawler._esun_spa_phase = 'capture_publication'
    result, _ = run_offline(monkeypatch, tmp_path, crawler)
    assert 'data' not in result
    assert not accesses
    assert 'PRIVATE' not in capsys.readouterr().out
    if registered:
        assert 'phase=capture_publication' in result['error']


def test_late_publication_logs_same_structured_error_as_early_failure(monkeypatch, tmp_path, capsys):
    class Late(Crawler):
        SAFE_COLLECT_PHASES = frozenset({'capture_publication'})

        def logout(self, page):
            self.reject_publication = True
            self._esun_spa_phase = 'capture_publication'
            return super().logout(page)

    crawler = Late(name='esun')
    result, page = run_offline(monkeypatch, tmp_path, crawler)
    assert 'data' not in result
    assert result['collect_diagnostics'] == {
        'exception': 'RuntimeError', 'code': 'collect_contract', 'phase': 'capture_publication',
    }
    stderr = capsys.readouterr().err
    assert result['error'] in stderr
    assert 'PRIVATE' not in repr(result) + stderr
    assert page.listeners['response'] == []


@pytest.mark.parametrize('family', ['patchright', 'playwright'])
def test_native_browser_timeout_retains_safe_type_at_collect_sink(monkeypatch, tmp_path, capsys, family):
    from importlib import import_module
    native = import_module(f'{family}.sync_api').TimeoutError
    crawler = Crawler(name='esun')

    def fail(_page, _collector):
        raise native('PRIVATE-BROWSER-LOCATOR')

    monkeypatch.setattr(crawler, 'collect', fail)
    result, _ = run_offline(monkeypatch, tmp_path, crawler)
    assert result['collect_diagnostics']['exception'] == 'TimeoutError'
    assert 'PRIVATE' not in repr(result) + capsys.readouterr().err


def test_collector_selection_failure_is_sanitized_before_login(monkeypatch, tmp_path):
    class Broken(Crawler):
        def _make_collector(self, page):
            raise RuntimeError("PRIVATE-SPA-LOGIN-DOM")

    result, page = run_offline(monkeypatch, tmp_path, Broken(name="esun"))
    assert "data" not in result
    assert "PRIVATE-SPA-LOGIN-DOM" not in str(result)
    assert result["error"].startswith("RuntimeError:")
    assert page.listeners == {}


def test_dialog_handler_setup_failure_detaches_before_login(monkeypatch, tmp_path):
    crawler = Crawler(name="esun")

    def fail(page):
        raise RuntimeError("PRIVATE-DIALOG-DOM")

    crawler.attach_shared_dialog_handler = fail
    result, page = run_offline(monkeypatch, tmp_path, crawler)
    assert result["error"].startswith("RuntimeError:")
    assert "PRIVATE-DIALOG-DOM" not in str(result)
    assert "login" not in crawler.events
    assert page.listeners["response"] == []


def test_repeated_callback_cannot_publish_or_login_again(monkeypatch, tmp_path):
    crawler = Crawler(name="esun")
    result, page = run_offline(monkeypatch, tmp_path, crawler, repeat=True)
    assert result == {"error": "browser_callback_repeated"}
    assert crawler.events == ["make", "login", "collect", "proof", "logout", "proof"]
    assert page.listeners["response"] == []


@pytest.mark.parametrize("timing", ["collect", "publication", "late_publication"])
@pytest.mark.parametrize("reject", [False, True])
def test_reentrant_callback_during_collection_cannot_publish(monkeypatch, tmp_path, timing, reject):
    callback = None

    def repeat():
        assert callback is not None
        callback(page)
        if reject:
            raise ValueError("synthetic failure after repeated callback")

    class Reentrant(Crawler):
        def collect(self, page, collector):
            self.events.append("collect")
            if timing == "collect":
                repeat()
            return self.outcome

        def _validate_collect_publication(self, collector):
            super()._validate_collect_publication(collector)
            expected = 1 if timing == "publication" else 2 if timing == "late_publication" else 0
            if self.events.count("proof") == expected:
                repeat()

    crawler = Reentrant(name="esun")
    crawler.session_dir = tmp_path / "session"
    crawler.session_dir.mkdir()
    page = Page()
    monkeypatch.setattr(crawler, "_enforce_session_freshness", lambda: None)
    monkeypatch.setattr(crawler, "_build_fetch_kwargs", lambda: {"__cleanups__": []})

    def execute(url, *, headless, page_action, fetch_kwargs):
        nonlocal callback
        callback = page_action
        page_action(page)

    monkeypatch.setattr(crawler, "_execute_browser_flow", execute)
    result = crawler.run(page.url, headless=True)
    assert result == {"error": "browser_callback_repeated"}
    assert crawler.events.count("login") == 1
    assert crawler.events.count("collect") == 1
    assert page.listeners["response"] == []


@pytest.mark.parametrize("shift", ["origin", "dialog"])
def test_post_collect_origin_or_dialog_change_blocks_proof(monkeypatch, tmp_path, shift):
    crawler = Crawler(name="esun", move_after_collect=shift == "origin", dialog_after_collect=shift == "dialog")
    result, page = run_offline(monkeypatch, tmp_path, crawler)
    assert result["error"].startswith("collect_failed:")
    assert "data" not in result
    assert "proof" not in crawler.events
    assert "logout" in crawler.events if shift == "dialog" else "logout" not in crawler.events
    assert page.listeners["response"] == []


def test_publication_failure_is_redacted_and_detached(monkeypatch, tmp_path):
    crawler = Crawler(name="esun", reject_publication=True)
    result, page = run_offline(monkeypatch, tmp_path, crawler)
    assert result["error"].startswith("collect_failed:")
    assert "PRIVATE-RESPONSE-BODY" not in str(result)
    assert "data" not in result
    assert page.listeners["response"] == []


@pytest.mark.parametrize("error", ["PRIVATE-FAILURE", ""])
def test_collect_error_barrier_and_empty_carrier(monkeypatch, tmp_path, error):
    crawler = Crawler(name="esun", outcome=base.BankCollectResult(error=error, card_bill_facts_ok=False))
    result, _ = run_offline(monkeypatch, tmp_path, crawler)
    if error:
        assert "data" not in result
        assert result["error"].startswith("collect_failed:")
        assert error not in str(result)
        assert "proof" not in crawler.events
    else:
        assert result["data"]["error"] == ""
        assert "proof" in crawler.events


@pytest.mark.parametrize("failure", ["origin", "dialog", None])
def test_shared_login_guards_credential_operations_without_test_proxy(monkeypatch, failure):
    crawler = Crawler(name="esun")
    page = Page()
    typed = []
    attempts = []
    page.keyboard = SimpleNamespace(type=lambda text: typed.append(text))

    def click():
        if failure == "origin":
            page.url = "https://foreign.invalid/"
        elif failure == "dialog":
            crawler._shared_dialog_blocked = True

    page.locator = lambda selector: SimpleNamespace(click=click)
    crawler.prepare_login_page = lambda raw: None
    crawler.login_checkpoint_rules = lambda: ()
    crawler.is_authenticated = lambda raw: False

    def submit(actual_page):
        attempts.append(True)
        actual_page.locator("input").click()
        actual_page.keyboard.type("SYNTHETIC-CREDENTIAL-CANARY")

    crawler.submit_credentials_once = submit
    monkeypatch.setattr(base, "evaluate_login_checkpoint", lambda raw, *, phase, **kw:
                        base.CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS
                            if phase is CheckpointPhase.PRE_SUBMIT else CheckpointKind.AUTHENTICATED))
    if failure:
        with pytest.raises(base.LoginCheckpointBlocked):
            base.BankCrawler._shared_login(crawler, page)
        assert typed == []
    else:
        assert base.BankCrawler._shared_login(crawler, page) is True
        assert typed == ["SYNTHETIC-CREDENTIAL-CANARY"]
    assert attempts == [True]


def test_actual_esun_shared_login_stops_typing_after_native_navigation(monkeypatch):
    from patchright.sync_api import sync_playwright
    from backend.banks.esun import EsunCrawler, EsunLoginError
    from tests.test_esun_main_login_form import FORM

    crawler = object.__new__(EsunCrawler)
    crawler.name = "esun"
    crawler.creds = SimpleNamespace(national_id="TEST-ID", user_code="TEST-USER", password="TEST-PASS")
    monkeypatch.setattr(crawler, "prepare_login_page", lambda page: None)
    monkeypatch.setattr(crawler, "login_checkpoint_rules", lambda: ())
    monkeypatch.setattr(base, "evaluate_login_checkpoint", lambda *args, **kw:
                        base.CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            requests = []

            def route(request):
                requests.append(request.request.url)
                request.fulfill(content_type="text/html", body=FORM)

            page.route("**/*", route)
            page.goto("https://ebank.esunbank.com.tw/")
            page.locator("input[name=id]").evaluate(
                "el => el.onclick = () => { location.href = 'https://foreign.invalid/'; }")
            with pytest.raises(EsunLoginError):
                crawler._shared_login(page)
            page.wait_for_url("https://foreign.invalid/")
            assert requests == ["https://ebank.esunbank.com.tw/", "https://foreign.invalid/"]
            assert page.locator('input:not([type=checkbox])').evaluate_all(
                'els => els.every(e => !e.value)') is True
            assert page.locator('body').get_attribute('data-submits') == "0"
        finally:
            browser.close()


def test_logout_invalidated_publication_never_returns_data(monkeypatch, tmp_path):
    class LateFailure(Crawler):
        def logout(self, page):
            self.reject_publication = True
            return super().logout(page)

    result, page = run_offline(monkeypatch, tmp_path, LateFailure(name="esun"))
    assert "data" not in result
    assert result["error"].startswith("collect_failed:")
    assert "PRIVATE-RESPONSE-BODY" not in str(result)
    assert page.listeners["response"] == []


def test_observer_bounds_decoded_total_and_matches_exact_request():
    class CDP:
        def __init__(self):
            self.events = {}
            self.body_reads = 0
        def on(self, name, callback): self.events[name] = callback
        def remove_listener(self, name, callback): self.events.pop(name)
        def detach(self): pass
        def send(self, name, params=None):
            if name == "Page.getFrameTree":
                return {"frameTree": {"frame": {"id": "frame", "loaderId": "loader", "url": url}}}
            if name == "Network.getResponseBody":
                self.body_reads += 1
                return {"body": "abc", "base64Encoded": False}
            return {}

    url = "https://ebank.esunbank.com.tw/fco/fao01002/FAO01002.faces"
    cdp = CDP()
    frame = SimpleNamespace(url=url)
    page = SimpleNamespace(frames=[frame], main_frame=frame, context=SimpleNamespace(new_cdp_session=lambda p: cdp))
    observer = base._HistoryBodyObserver(page, url)
    observer.start()
    observer.LIMIT = 3
    observer.TOTAL_LIMIT = 3
    observer._request({"requestId": "bad", "request": {"url": url + "?other", "method": "POST", "postData": "x"}, "frameId": "frame"})
    assert not observer.records
    observer._request({"requestId": "ok", "request": {"url": url, "method": "POST", "postData": "x"}, "frameId": "frame", "loaderId": "loader", "documentURL": url})
    observer._event("responseReceived", {"requestId": "ok", "response": {"status": 200, "url": url}, "frameId": "frame", "loaderId": "loader"})
    observer._event("dataReceived", {"requestId": "ok", "dataLength": 3})
    observer._event("loadingFinished", {"requestId": "ok"})
    request = SimpleNamespace(url=url, method="POST", frame=frame, post_data="x", redirected_from=None, redirected_to=None)
    response = SimpleNamespace(request=request, url=url, status=200)
    assert observer.read(response, frame, url, 3, 1) == b"abc"
    observer._request({"requestId": "second", "request": {"url": url, "method": "POST", "postData": "y"}, "frameId": "frame", "loaderId": "loader", "documentURL": url})
    observer._event("dataReceived", {"requestId": "second", "dataLength": 1})
    assert observer.bad is True
    assert cdp.body_reads == 1
    observer.close()
    assert cdp.events == {}


def test_click_checks_origin_and_dialog_immediately_before_action():
    checked = []
    class Locator:
        def __init__(self, nodes): self.nodes = nodes
        def nth(self, i): return Locator(self.nodes[i:i + 1])
        def element_handle(self, *, timeout):
            if not self.nodes: raise TimeoutError
            return self.nodes[0]
        def locator(self, selector): return Locator(self.nodes[0].children.get(selector, []))
        def is_visible(self): return self.nodes[0].visible
        def inner_text(self, **kwargs): return self.nodes[0].text
        def is_enabled(self): return True
        def get_attribute(self, attr): return None
        def evaluate(self, expression):
            assert "el.form" in expression
            return False
        def click(self): self.nodes[0].clicks += 1

    class Node:
        def __init__(self, text="", children=None):
            self.text, self.children, self.visible, self.clicks = text, children or {}, True, 0
    action = Node("Continue")
    container = Node("Session", {"button, a, [role=button]": [action]})
    page = SimpleNamespace(locator=lambda selector: Locator([container] if selector == "#notice" else []), frames=[], set_default_timeout=lambda _: None)
    rule = LoginCheckpointRule(name="esun-notice", bank="esun", phases=(CheckpointPhase.POST_SUBMIT,), kind=CheckpointKind.DISMISSIBLE_NOTICE, container_selector="#notice", action_texts=("Continue",))
    def can_act():
        checked.append(True)
        return False
    outcome = evaluate_login_checkpoint(page, bank="esun", phase=CheckpointPhase.POST_SUBMIT, rules=(rule,), is_authenticated=lambda _: False, can_act=can_act)
    assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
    assert action.clicks == 0
    assert checked == [True]


@pytest.mark.parametrize("change", ["origin", "dialog"])
def test_shared_login_passes_last_moment_origin_and_dialog_guard(monkeypatch, change):
    crawler = Crawler(name="esun")
    page = Page()
    crawler.prepare_login_page = lambda page: None
    crawler.login_checkpoint_rules = lambda: (
        LoginCheckpointRule(name="esun-notice", bank="esun", phases=(CheckpointPhase.PRE_SUBMIT,), kind=CheckpointKind.DISMISSIBLE_NOTICE, container_selector="#notice", action_texts=("Continue",)),
    )
    crawler.is_authenticated = lambda page: False
    calls = []

    def evaluate(_page, *, can_act, **kwargs):
        calls.append("inspect")
        if change == "origin":
            page.url = "https://foreign.invalid/"
        else:
            crawler._shared_dialog_blocked = True
        assert can_act() is False
        calls.append("denied")
        return base.CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER)

    monkeypatch.setattr(base, "evaluate_login_checkpoint", evaluate)
    with pytest.raises(base.LoginCheckpointBlocked):
        base.BankCrawler._shared_login(crawler, page)
    assert calls == ["inspect", "denied"]


def test_duplicate_hidden_form_control_and_native_submit_block_offline():
    from patchright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content('<form id="credentials"><input type="password" hidden></form><div id="notice"><button form="credentials" type="submit">Continue</button></div>')
            for kind in (CheckpointKind.DUPLICATE_SESSION, CheckpointKind.DISMISSIBLE_NOTICE):
                rule = LoginCheckpointRule(name="esun-confirm", bank="esun", phases=(CheckpointPhase.POST_SUBMIT,), kind=kind, container_selector="#notice", action_texts=("Continue",))
                outcome = evaluate_login_checkpoint(page, bank="esun", phase=CheckpointPhase.POST_SUBMIT, rules=(rule,), is_authenticated=lambda _: False)
                assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
            assert page.url == "about:blank"
        finally:
            browser.close()


def test_duplicate_hidden_control_inside_notice_is_not_submitted():
    from patchright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content('<div id="notice"><input type="password" hidden><button type="button">Continue</button></div>')
            rule = LoginCheckpointRule(name="esun-confirm", bank="esun", phases=(CheckpointPhase.POST_SUBMIT,), kind=CheckpointKind.DUPLICATE_SESSION, container_selector="#notice", action_texts=("Continue",))
            outcome = evaluate_login_checkpoint(page, bank="esun", phase=CheckpointPhase.POST_SUBMIT, rules=(rule,), is_authenticated=lambda _: False)
            assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
        finally:
            browser.close()
