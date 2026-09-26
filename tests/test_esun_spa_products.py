"""Closed product read: ordinary popup transport, bounded public-body projection."""
from types import SimpleNamespace

from backend.banks.esun_spa.products import _project_bill, _official, collect_products


def test_official_boundary_and_public_shape():
    assert _official("https://iesc.esunbank.com/IESC/cardBill")
    assert _official("https://ebank.esunbank.com.tw/esb/card/credit/overview")
    assert not _official("http://iesc.esunbank.com/IESC/")
    assert not _official("https://iesc.esunbank.com.evil.test/IESC/")
    assert not _official("https://iesc.esunbank.com@evil.test/IESC/")
    assert _project_bill("summary", {"rtnCode": "0000", "billInfo": {"billTotalInfoList": [{"billTotalCurrency": "TWD", "billTotalAmount": "123"}], "paymentDueDate": "2026/09/30"}, "paymentInfoList": [], "feeRewardInfoList": []}) == {"shape": "summary", "totals": 1, "payments": 0}
    assert _project_bill("detail", {"transList": [{"year": "2026", "month": "09", "transDetailList": [{"merchantName": "private"}]}], "cardInfoList": [], "currencyInfoList": []}) == {"shape": "detail", "groups": 1, "rows": 1}
    assert _project_bill("detail", {"transList": [{"transDetailList": "bad"}], "cardInfoList": [], "currencyInfoList": []}) is None


def test_native_popup_reads_only_owned_official_responses(monkeypatch):
    from backend.banks.esun_spa import products
    from tests.test_esun_spa_capture import Page

    class Context:
        def __init__(self): self.handlers = {}
        def route(self, pattern, callback): self.handlers['route'] = callback
        def unroute(self, pattern, callback): self.handlers.pop('route')
        def on(self, event, callback): self.handlers[event] = callback
        def remove_listener(self, event, callback): self.handlers.pop(event)
        def new_cdp_session(self, page): return page.new_session(page)

    context = Context()
    popup = Page()
    popup.main_frame.url = 'https://iesc.esunbank.com/IESC/cardBill'
    setattr(popup, 'url', popup.main_frame.url)
    setattr(popup, 'context', context)
    native = SimpleNamespace(context=context)
    setattr(popup, 'opener', lambda: native)
    forbidden = []
    def no_native_body():
        forbidden.append(True)
        raise AssertionError('no native response body read')
    response = SimpleNamespace(url='https://iesc.esunbank.com/GW/creditBill/getSummaryResult', status=200,
                               headers={'content-type': 'application/json'},
                               request=SimpleNamespace(frame=popup.main_frame, method='POST',
                                                       url='https://iesc.esunbank.com/GW/creditBill/getSummaryResult',
                                                       redirected_from=None, redirected_to=None),
                               body=no_native_body, json=no_native_body)
    class Pending:
        def __enter__(self): return SimpleNamespace(value=popup)
        def __exit__(self, *args): pass
    native.expect_popup = lambda **kwargs: Pending()
    native.wait_for_timeout = lambda ms: context.handlers['response'](response)
    routing = []
    def click(**kwargs):
        handler = context.handlers['route']
        for url in ('https://evil.test/steal', 'https://iesc.esunbank.com/GW/creditBill/getDetailResult'):
            route = SimpleNamespace(request=SimpleNamespace(url=url),
                                    continue_=lambda: routing.append('allow'), abort=lambda: routing.append('deny'))
            handler(route)
    button = SimpleNamespace(count=lambda: 1, is_visible=lambda: True, is_enabled=lambda: True,
                             click=click)
    native.locator = lambda selector: button
    monkeypatch.setattr(products._OriginGuardProxy, '_unwrap', lambda page: page)
    clock = iter([0, 0, 0, 7])
    monkeypatch.setattr(products, 'monotonic', lambda: next(clock))
    receipt = products._open_bill(native, lambda: None)
    assert receipt['popup'] is True and receipt['summary'] is False
    assert receipt['detail'] is False and not context.handlers and routing == ['deny', 'allow']
    assert not forbidden and all(s.detached for s in popup.sessions)
    assert not any('Network.getResponseBody' in s.commands for s in popup.sessions)


def test_native_failure_returns_closed_partial(monkeypatch):
    from backend.banks.esun_spa import products
    monkeypatch.setattr(products, '_overview', lambda *args: (True, lambda: None))
    def fail(*args): raise ValueError('sensitive bank exception')
    monkeypatch.setattr(products, '_open_bill', fail)
    crawler = SimpleNamespace(_esun_spa_phase=None)
    result = collect_products(crawler, None, None, None)
    assert result.error == 'spa_collection_incomplete' and result.card_bill_facts_ok is False
    assert result.telemetry['esun_spa_products']['native_bill_unavailable'] is True
    assert 'sensitive' not in str(result.telemetry)


def test_no_card_never_opens_popup(monkeypatch):
    from backend.banks.esun_spa import products
    called = []
    monkeypatch.setattr(products, "_overview", lambda *args: (False, lambda: None))
    monkeypatch.setattr(products, "_open_bill", lambda *args: called.append(1))
    result = collect_products(None, None, None, None)
    assert result.error == "spa_collection_incomplete"
    assert result.card_bill_facts_ok is False
    assert result.telemetry["esun_spa_products"]["card_presence"] is False
    assert not called
