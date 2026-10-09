"""Closed product read: ordinary popup transport, bounded public-body projection."""
from types import SimpleNamespace
import traceback

import pytest

from backend.banks.esun_spa.products import (
    _cleanup_all,
    _project_bill,
    _official,
    collect_products,
)


def test_official_boundary_and_public_shape():
    assert _official("https://iesc.esunbank.com/IESC/cardBill")
    assert _official("https://ebank.esunbank.com.tw/esb/card/credit/overview")
    assert not _official("http://iesc.esunbank.com/IESC/")
    assert not _official("https://iesc.esunbank.com.evil.test/IESC/")
    assert not _official("https://iesc.esunbank.com@evil.test/IESC/")
    assert _project_bill("summary", {"rtnCode": "0000", "billInfo": {"billTotalInfoList": [{"billTotalCurrency": "TWD", "billTotalAmount": "123"}], "paymentDueDate": "2026/09/30"}, "paymentInfoList": [], "feeRewardInfoList": []}) == {"shape": "summary", "totals": 1, "payments": 0}
    assert _project_bill("detail", {"transList": [{"year": "2026", "month": "09", "transDetailList": [{"merchantName": "private"}]}], "cardInfoList": [], "currencyInfoList": []}) == {"shape": "detail", "groups": 1, "rows": 1}
    assert _project_bill("detail", {"transList": [{"transDetailList": "bad"}], "cardInfoList": [], "currencyInfoList": []}) is None


def test_cleanup_all_attempts_every_action_and_retries_partial_failures():
    calls = {"listener": 0, "route": 0, "observer": 0}

    def action(name, fail_once=False):
        def run():
            calls[name] += 1
            if fail_once and calls[name] == 1:
                raise RuntimeError("synthetic cleanup failure")
        return run

    _cleanup_all([
        action("listener", fail_once=True),
        action("route"),
        action("observer", fail_once=True),
    ])
    assert calls == {"listener": 2, "route": 1, "observer": 2}

    with pytest.raises(RuntimeError, match="cleanup incomplete") as raised:
        _cleanup_all([action("route", fail_once=True), lambda: (_ for _ in ()).throw(
            RuntimeError("SYNTHETIC-PRIVATE-CLEANUP"),
        )])
    rendered = "".join(traceback.format_exception(raised.value))
    assert "SYNTHETIC-PRIVATE-CLEANUP" not in rendered


def test_native_popup_setup_failure_removes_already_installed_route(monkeypatch):
    from backend.banks.esun_spa import products

    calls = {"route": 0, "unroute": 0}

    class Context:
        def route(self, *_args):
            calls["route"] += 1

        def on(self, *_args):
            raise RuntimeError("setup failed")

        def unroute(self, *_args):
            calls["unroute"] += 1

    button = SimpleNamespace(
        count=lambda: 1, is_visible=lambda: True, is_enabled=lambda: True,
    )
    native = SimpleNamespace(context=Context(), locator=lambda _selector: button)
    monkeypatch.setattr(products._OriginGuardProxy, '_unwrap', lambda _page: native)

    with pytest.raises(RuntimeError, match="setup failed"):
        products._open_bill(native, lambda: None)

    assert calls == {"route": 1, "unroute": 1}


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


def test_unposted_detail_becomes_pending_and_marks_fetch_ok(monkeypatch):
    from backend.banks.esun_spa import products
    body = {"rtnCode": "0000", "transList": [{"year": "2026", "month": "10", "transDetailList": [
        {"merchantName": " Shop ", "paymentCurrency": "TWD", "paymentAmount": 120, "transCurrency": "TWD",
         "transAmount": 120, "cardNo": "0000-XXXX-XXXX-2869", "transMonthDay": "1005"}]}]}
    rows = products._unposted_transactions(body)
    assert rows == [{"card_last4": "2869", "consume_date": "2026-10-05", "post_date": None, "merchant": "Shop",
                     "billed_amount": 120, "billed_currency": "TWD", "consume_currency": None,
                     "consume_amount": None, "status": "未入帳"}]
    assert products._unposted_transactions({"transList": [{"year": "2026", "transDetailList": [{"cardNo": "x"}]}]}) is None

    monkeypatch.setattr(products, "_overview", lambda *a: (True, lambda: None))
    def open_bill(page, bound, selector=products.BILL_BUTTON, urls=products.BILL_URLS):
        return {"_detail": body} if urls is products.UNPOSTED_URLS else {"_statement_cycle": None}
    monkeypatch.setattr(products, "_open_bill", open_bill)
    result = collect_products(SimpleNamespace(_esun_spa_phase=None), None, None, None)
    assert result.card_transactions_ok is True and result.card_transactions == rows
    assert result.telemetry["esun_spa_products"]["unposted_txns"] == 1

    def broken(page, bound, selector=products.BILL_BUTTON, urls=products.BILL_URLS):
        if urls is products.UNPOSTED_URLS:
            raise ValueError("popup missing")
        return {}
    monkeypatch.setattr(products, "_open_bill", broken)
    result = collect_products(SimpleNamespace(_esun_spa_phase=None), None, None, None)
    assert result.card_transactions_ok is False and result.card_transactions is None
