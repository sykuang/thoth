"""Offline publication and optional card-body regressions."""

import json
from types import SimpleNamespace as NS

import pytest

from backend.banks.esun_spa import products
from backend.banks.esun_spa.collection import retain_capture_records
from tests.test_esun_spa_capture import Page, issue, load


@pytest.mark.parametrize("evicted", [False, True])
def test_late_native_failure_revokes_exact_historical_publication_proof(evicted):
    m = load()
    page, collector = Page(), m.SpaCollector()
    collector.attach(page)
    path = m.PATHS[2]
    old = issue(m, collector, page, path=path)
    history = []
    _, _, old_proof = retain_capture_records(collector, [path], history)
    assert history == [old_proof]
    old_proof()
    baseline = collector.snapshot()
    if evicted:
        issue(m, collector, page, path=path)
        assert m.require_current_success(collector, baseline, path)[0] == {"ok": True}
    collector._on_request_failed(old)
    with pytest.raises(ValueError, match="capture_publication_rejected"):
        old_proof()
    if evicted:
        assert m.require_current_success(collector, baseline, path)[0] == {"ok": True}
    collector.detach(page)


def test_unrelated_native_failure_preserves_historical_publication_proof():
    m = load()
    page, collector = Page(), m.SpaCollector()
    collector.attach(page)
    path = m.PATHS[2]
    old = issue(m, collector, page, path=path)
    _, _, proof = retain_capture_records(collector, [path], [])
    collector._on_request_failed(type(old)(**old.__dict__))  # Same URL, distinct native identity.
    proof()
    collector.detach(page)


@pytest.mark.parametrize("failure", [None, "cdp", "native"])
@pytest.mark.parametrize("detach_index", [0, 2, 5])
def test_runner_rejects_failure_dispatched_during_observer_detach(monkeypatch, tmp_path, failure, detach_index):
    from backend.banks.esun import EsunCrawler
    from tests import test_esun_main_core_backport as runner

    m = load()
    page = Page()
    page.url = page.main_frame.url
    monkeypatch.setattr(runner, "Page", lambda: page)
    detached = []

    class TeardownCrawler(runner.Crawler):
        def _make_collector(self, actual_page):
            self.collector_instance = m.SpaCollector()
            return self.collector_instance

        def _shared_login(self, actual_page):
            return True

        def collect(self, actual_page, collector):
            request = issue(m, collector, page, path=m.PATHS[2])
            retain_capture_records(collector, [m.PATHS[2]], collector.publication_checks)
            query_session = page.sessions[2]
            request_id = next(iter(collector.observers[m.PATHS[2]].records))
            for index, session in enumerate(page.sessions):
                def detach(index=index, session=session):
                    detached.append(index)
                    session.detached = True
                    if index == detach_index:
                        if failure == "cdp":
                            callback = query_session.handlers.get("Network.loadingFailed")
                            if callback:
                                callback({"requestId": request_id})
                        elif failure == "native":
                            callback = page.handlers.get("requestfailed")
                            if callback:
                                callback(request)
                session.detach = detach
            return self.outcome

        _validate_collect_publication = EsunCrawler._validate_collect_publication

    crawler = TeardownCrawler(name="esun")
    result, _ = runner.run_offline(monkeypatch, tmp_path, crawler)
    if failure:
        assert "data" not in result
        assert "error" in result
    else:
        assert result == {"data": {"card_bill_facts_ok": False}}
    assert detached == list(range(len(m.PATHS)))
    assert not crawler.collector_instance.observers
    assert not any(session.handlers for session in page.sessions)


def _bill_popup(monkeypatch, payloads):
    """Popup-owned responses are read in-handler (live: XHRs finish before a late CDP attach)."""
    class Context:
        def __init__(self):
            self.handlers = {}
        def route(self, pattern, callback):
            self.handlers['route'] = callback
        def unroute(self, pattern, callback):
            self.handlers.pop('route')
        def on(self, name, callback):
            self.handlers[name] = callback
        def remove_listener(self, name, callback):
            self.handlers.pop(name)
        def new_cdp_session(self, page):
            return page.new_session(page)

    context = Context()
    popup = Page()
    popup.main_frame.url = 'https://iesc.esunbank.com/IESC/cardBill'
    setattr(popup, 'url', popup.main_frame.url)
    setattr(popup, 'context', context)
    native = NS(context=context)
    setattr(popup, 'opener', lambda: native)
    called = []

    def forbidden(kind):
        called.append(kind)
        raise AssertionError('native response body/json forbidden')

    def on_wait(ms):
        if not payloads:
            return
        kind, raw = payloads.pop(0)
        url = 'https://iesc.esunbank.com/GW/creditBill/get' + ('Summary' if kind == 'summary' else 'Detail') + 'Result'
        req = NS(url=url, method='POST', frame=popup.main_frame, post_data='{}',
                 redirected_from=None, redirected_to=None)
        resp = NS(url=url, request=req, status=200, headers={'content-type': 'application/json'},
                  body=lambda: (called.append('body'), raw)[1], json=lambda: forbidden('json'))
        if popup.sessions:
            session = popup.sessions[0 if kind == 'summary' else 1]
            rid = kind
            session.handlers['Network.requestWillBeSent']({
                'requestId': rid, 'request': {'url': url, 'method': 'POST', 'postData': '{}'},
                'frameId': 'main', 'loaderId': 'doc', 'documentURL': popup.main_frame.url,
            })
            session.handlers['Network.responseReceived']({
                'requestId': rid, 'frameId': 'main', 'loaderId': 'doc',
                'response': {'url': url, 'status': 200},
            })
            session.handlers['Network.dataReceived']({'requestId': rid, 'dataLength': len(raw)})
            session.handlers['Network.loadingFinished']({'requestId': rid})
            session.bodies[rid] = raw.decode()
        context.handlers['response'](resp)

    class Pending:
        def __enter__(self):
            return NS(value=popup)
        def __exit__(self, *args):
            pass

    native.expect_popup = lambda **kwargs: Pending()
    native.wait_for_timeout = on_wait
    native.locator = lambda selector: NS(count=lambda: 1, is_visible=lambda: True,
                                         is_enabled=lambda: True, click=lambda **kwargs: None)
    monkeypatch.setattr(products._OriginGuardProxy, '_unwrap', lambda page: page)
    clock = iter(range(10_000))
    monkeypatch.setattr(products, 'monotonic', lambda: next(clock))
    receipt = products._open_bill(native, lambda: None)
    return receipt, popup.sessions, called, context.handlers


def test_card_diagnostics_read_bounded_observer_not_native_response(monkeypatch):
    raw = json.dumps({'status': '200', 'body': {'billInfo': {'billTotalInfoList': []}, 'paymentInfoList': []}}).encode()
    receipt, sessions, called, handlers = _bill_popup(monkeypatch, [('summary', raw)])
    assert receipt['summary'] == {'shape': 'summary', 'totals': 0, 'payments': 0}
    assert called == ['body'] and handlers == {}


def test_card_diagnostics_reject_oversize_before_body_read(monkeypatch):
    monkeypatch.setattr(products, 'LIMIT', 80, raising=False)
    raw = json.dumps({'status': '200', 'body': {'billInfo': {'billTotalInfoList': []}, 'paymentInfoList': []}}).encode()
    assert len(raw) > products.LIMIT
    receipt, sessions, called, _ = _bill_popup(monkeypatch, [('summary', raw)])
    assert receipt['summary'] is False  # ponytail: size known only after body(); Playwright exposes no pre-read length


def test_card_diagnostics_shared_budget_rejects_second_before_read(monkeypatch):
    summary = json.dumps({'status': '200', 'body': {'billInfo': {'billTotalInfoList': []}, 'paymentInfoList': []}}).encode()
    detail = json.dumps({'status': '200', 'body': {'transList': [], 'cardInfoList': [], 'currencyInfoList': []}}).encode()
    monkeypatch.setattr(products, 'BILL_TOTAL', len(summary) + len(detail) - 1)
    receipt, sessions, called, _ = _bill_popup(monkeypatch, [('summary', summary), ('detail', detail)])
    assert receipt['summary'] == {'shape': 'summary', 'totals': 0, 'payments': 0}
    assert receipt['detail'] is False and called == ['body', 'body']
