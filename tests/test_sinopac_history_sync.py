from __future__ import annotations

from calendar import monthrange
from copy import deepcopy
from datetime import date, timedelta
import inspect
from types import SimpleNamespace

import pytest

from backend.banks.sinopac import SinopacCrawler
from backend.core.base import ApiHit, BankCollectResult, ResponseCollector, validate_history_coverage
from backend.core.persist import sinopac as sinopac_persist_module
from backend.core.persist import persist_collected
from backend.core.persist.sinopac import persist_sinopac
from backend.core.store import BankStore


ACCOUNT = "01234567890123"
LABEL = "測試帳戶 01234567890123"


@pytest.fixture(autouse=True)
def _freeze_persistence_today(monkeypatch):
    monkeypatch.setattr(sinopac_persist_module, "_today", lambda: date(2026, 8, 31))


def _inventory_hit() -> ApiHit:
    return ApiHit(
        url="https://mma.sinopac.com/ws/bank/transdetail/ws_debitacct.ashx",
        raw_url="https://mma.sinopac.com/ws/bank/transdetail/ws_debitacct.ashx?1788171564478",
        method="POST",
        status=200,
        content_type="application/json; charset=utf-8",
        body_size=300,
        request_sequence=1,
        main_frame_request=True,
        resp_json=[{
            "Header": "SUCCESS",
            "Message": "",
            "SubInfo": [{
                "DataText": LABEL,
                "DataValue": ACCOUNT,
                "DisplayText": "TWD",
            }],
        }],
    )


def _row(*, when: str = "2026/08/31<br />12:34") -> dict:
    return {
        "DataText1": when,
        "DataText2": "2026/08/31",
        "DataText3": "利息存入",
        "DataText4": "+30",
        "DataText5": "1,030",
        "DataText6": "",
        "DataText7": "測試票號",
        "DataText8": "測試備註",
        "DataText9": "測試用途",
        "DataText10": "",
        "DataText11": "",
    }


def _history_hit(
    *, start: str = "20260801", end: str = "20260831", rows=None,
    message: str | None = None,
) -> ApiHit:
    rows = [_row()] if rows is None else rows
    if message is None:
        message = "" if rows else "查無資料"
    body = {
        "BeginDate": "20250901",
        "DefBeginDate": "20260701",
        "DefEndDate": "20260731",
        "EndDate": "20260831",
        "HeadInfo": [{
            "FieldKey": f"DataText{i}",
            "HeadText": f"欄位{i}",
            "FieldWidth": "10",
            "HeadAlign": "L",
            "DataAlign": "L",
            "OrderIndex": str(i),
            "MainShow": "Y",
            "DetailShow": "Y",
        } for i in range(1, 10)],
        "Header": "SUCCESS",
        "MaxMonth": "3",
        "Message": message,
        "RecordCount": "0" if rows else None,
        "SubInfo": rows,
        "isOBU": "Y",
    }
    return ApiHit(
        url="https://mma.sinopac.com/ws/bank/transdetail/ws_transdetailMerge.ashx",
        raw_url="https://mma.sinopac.com/ws/bank/transdetail/ws_transdetailMerge.ashx?1788171564478",
        method="POST",
        status=200,
        req_body=(
            f"Acct={LABEL}&AcctName=&AcctValue={ACCOUNT}&BusinessDate=20260831&"
            f"Curr=TWD&CurrName=&EndDate={end}&QueryType=3&StartDate={start}&TextType="
        ),
        content_type="application/json; charset=utf-8",
        body_size=2_000,
        request_sequence=2,
        main_frame_request=True,
        resp_json=[body],
    )


def _coverage(*, mode: str = "full", start: str = "2025-09-01", end: str = "2026-08-31") -> dict:
    cursor = date.fromisoformat(start)
    finish = date.fromisoformat(end)
    windows = []
    while cursor <= finish:
        window_end = min(
            finish, date(cursor.year, cursor.month, monthrange(cursor.year, cursor.month)[1]),
        )
        windows.append({
            "identity": ACCOUNT,
            "start": cursor.isoformat(),
            "end": window_end.isoformat(),
            "status": "complete" if window_end == finish else "explicit_empty",
            "pages": 1,
        })
        cursor = window_end + timedelta(days=1)
    return {
        "mode": mode,
        "as_of": end,
        "domains": [{
            "domain": "twd_transactions",
            "expected": [{"identity": ACCOUNT, "start": start, "end": end}],
            "windows": windows,
        }],
    }


def _persist_payload(*, mode: str = "full") -> dict:
    start = "2025-09-01" if mode == "full" else "2026-08-24"
    coverage = _coverage(mode=mode, start=start)
    history_results = []
    for window in coverage["domains"][0]["windows"]:
        final = window["end"] == "2026-08-31"
        rows = [_row()] if final else []
        history_results.append({
            "account": ACCOUNT,
            "account_name": LABEL,
            "currency": "TWD",
            "records": rows,
            "receipt": {**window, "rows": len(rows)},
        })
    return BankCollectResult(
        bank_balance=[{
            "Header": "SUCCESS",
            "Message": "",
            "SubInfo": [{
                "AcctValue": ACCOUNT,
                "Curr": "TWD",
                "AvailBalance": "1030",
                "AcctText": "測試帳戶",
            }],
        }],
        twd_transactions=history_results,
        debit_accounts=[{"label": LABEL, "identity": ACCOUNT, "currency": "TWD"}],
        history_coverage=coverage,
        card_bill_facts_ok=False,
    ).to_dict()


def test_sinopac_opts_into_twd_history_only() -> None:
    assert frozenset({"twd_transactions"}) == SinopacCrawler.HISTORY_COVERAGE_DOMAINS
    assert SinopacCrawler.HISTORY_COVERAGE_REQUIRED is True


def test_sinopac_full_windows_cover_latest_year_by_calendar_month() -> None:
    end = date(2026, 8, 31)
    start = SinopacCrawler._history_floor(end)
    windows = SinopacCrawler._history_windows(start, end)

    assert start == date(2025, 9, 1)
    assert windows[0] == (date(2025, 9, 1), date(2025, 9, 30))
    assert windows[-1] == (date(2026, 8, 1), end)
    assert len(windows) == 12
    assert all(a_start.month == a_end.month for a_start, a_end in windows)
    assert all(
        right_start == left_end + timedelta(days=1)
        for (_, left_end), (right_start, _) in zip(windows, windows[1:])
    )


def test_sinopac_incremental_range_uses_identity_cursor_and_rejects_future() -> None:
    crawler = object.__new__(SinopacCrawler)
    crawler.transaction_cursors = {"twd_transactions": {ACCOUNT: date(2026, 8, 20)}}
    assert crawler._history_range(ACCOUNT, end=date(2026, 8, 31), mode="incremental") == (
        date(2026, 8, 13), date(2026, 8, 31),
    )
    crawler.transaction_cursors = {"twd_transactions": {ACCOUNT: date(2026, 9, 1)}}
    for mode in ("full", "incremental"):
        with pytest.raises(RuntimeError, match="sinopac-twd-history-cursor"):
            crawler._history_range(ACCOUNT, end=date(2026, 8, 31), mode=mode)


@pytest.mark.parametrize(
    ("mutation", "guard"),
    [
        ("method", "sinopac-twd-history-inventory-envelope"),
        ("body", "sinopac-twd-history-inventory-envelope"),
        ("host", "sinopac-twd-history-inventory-envelope"),
        ("path", "sinopac-twd-history-inventory-envelope"),
        ("status", "sinopac-twd-history-inventory-envelope"),
        ("mime", "sinopac-twd-history-inventory-envelope"),
        ("redirect", "sinopac-twd-history-inventory-envelope"),
        ("duplicate", "sinopac-twd-history-inventory-identity"),
        ("currency", "sinopac-twd-history-inventory-identity"),
        ("identity", "sinopac-twd-history-inventory-identity"),
        ("sequence", "sinopac-twd-history-inventory-cardinality"),
        ("frame", "sinopac-twd-history-inventory-envelope"),
        ("row", "sinopac-twd-history-inventory-row"),
    ],
)
def test_sinopac_inventory_is_exact_authoritative_set(mutation, guard) -> None:
    assert guard in SinopacCrawler.SAFE_COLLECT_GUARDS
    hit = _inventory_hit()
    if mutation == "method":
        hit.method = "GET"
    elif mutation == "body":
        hit.req_body = f"AcctValue={ACCOUNT}"
    elif mutation == "host":
        hit.url = "https://mma.sinopac.com.evil.example/ws/bank/transdetail/ws_debitacct.ashx"
    elif mutation == "path":
        hit.url = "https://mma.sinopac.com/evil/ws_debitacct.ashx"
    elif mutation == "status":
        hit.status = 500
    elif mutation == "mime":
        hit.content_type = "text/html"
    elif mutation == "redirect":
        hit.redirected = True
    elif mutation == "duplicate":
        hit.resp_json[0]["SubInfo"].append(deepcopy(hit.resp_json[0]["SubInfo"][0]))
    elif mutation == "currency":
        hit.resp_json[0]["SubInfo"][0]["DisplayText"] = "USD"
    elif mutation == "identity":
        hit.resp_json[0]["SubInfo"][0]["DataValue"] = "1234"
    elif mutation == "sequence":
        hit.request_sequence = 0
    elif mutation == "row":
        hit.resp_json[0]["SubInfo"][0] = {}
    else:
        hit.main_frame_request = False
    collector = ResponseCollector("sinopac.com")
    collector.hits = [hit]

    with pytest.raises(RuntimeError, match=f"^{guard}$"):
        SinopacCrawler._twd_inventory(collector)


def test_sinopac_inventory_returns_exact_live_contract() -> None:
    collector = ResponseCollector("sinopac.com")
    collector.hits = [_inventory_hit()]
    assert SinopacCrawler._twd_inventory(collector) == [{
        "label": LABEL, "identity": ACCOUNT, "currency": "TWD",
    }]


def test_sinopac_inventory_accepts_authoritative_empty_set() -> None:
    hit = _inventory_hit()
    hit.resp_json[0]["SubInfo"] = []
    collector = ResponseCollector("sinopac.com")
    collector.hits = [hit]
    assert SinopacCrawler._twd_inventory(collector) == []


def test_sinopac_inventory_rejects_multiple_authoritative_responses() -> None:
    first = _inventory_hit()
    second = _inventory_hit()
    second.request_sequence = 2
    collector = ResponseCollector("sinopac.com")
    collector.hits = [first, second]

    with pytest.raises(
        RuntimeError, match="^sinopac-twd-history-inventory-cardinality$"
    ):
        SinopacCrawler._twd_inventory(collector)


def test_response_collector_records_non_bearer_request_issuance_sequence() -> None:
    collector = ResponseCollector("sinopac.com")
    page = SimpleNamespace()
    frame = SimpleNamespace(page=page)
    page.main_frame = frame
    request = SimpleNamespace(
        url="https://mma.sinopac.com/ws/bank/transdetail/ws_debitacct.ashx?1788171564478",
        headers={}, method="POST", post_data="{}", redirected_from=None, frame=frame,
    )
    collector._on_request(request)
    collector._on_response(SimpleNamespace(
        url=request.url,
        request=request,
        headers={
            "content-type": "application/json",
            "content-length": "2",
            "content-encoding": "identity",
        },
        status=200,
        body=lambda: b"{}",
        json=lambda: pytest.fail("bounded response must use raw body"),
    ))

    assert collector.request_sequence == 1
    assert collector.issued_count("ws_debitacct.ashx") == 1
    assert collector.hits[0].request_sequence == 1
    assert collector.hits[0].main_frame_request is True
    assert collector.hits[0].body_size == 2
    assert collector.hits[0].resp_json == {}


def test_response_collector_clears_failed_request_state() -> None:
    collector = ResponseCollector("mma.sinopac.com")
    request = SimpleNamespace(
        url="https://mma.sinopac.com/ws/bank/transdetail/ws_debitacct.ashx",
        headers={"authorization": "Bearer opaque"},
        redirected_from=None,
    )
    collector._on_request(request)
    assert len(collector._requests) == 1
    assert len(collector._request_main_frame) == 1
    assert len(collector._auth_requests) == 1

    collector._on_request_failed(request)

    assert collector._requests == {}
    assert collector._request_main_frame == {}
    assert collector._auth_requests == {}


def test_response_collector_frame_error_leaves_no_partial_state() -> None:
    collector = ResponseCollector("mma.sinopac.com")

    class Request:
        url = "https://mma.sinopac.com/ws/bank/transdetail/ws_debitacct.ashx"

        @property
        def frame(self):
            raise RuntimeError("unavailable")

    collector._on_request(Request())

    assert collector.request_sequence == 0
    assert collector._requests == {}
    assert collector._request_main_frame == {}
    assert collector.issued_count("ws_debitacct.ashx") == 0


def test_response_collector_does_not_decode_compressed_sinopac_history() -> None:
    collector = ResponseCollector("sinopac.com")
    request = SimpleNamespace(
        url="https://mma.sinopac.com/ws/bank/transdetail/ws_transdetailMerge.ashx?1788171564478",
        headers={}, method="POST", post_data="{}", redirected_from=None,
    )
    decoded = False

    def decode():
        nonlocal decoded
        decoded = True
        return {}

    collector._on_request(request)
    collector._on_response(SimpleNamespace(
        url=request.url,
        request=request,
        headers={
            "content-type": "application/json",
            "content-length": "100",
            "content-encoding": "gzip",
        },
        status=200,
        json=decode,
    ))

    assert decoded is False
    assert collector.hits[0].resp_json is None


def test_response_collector_uses_actual_bounded_body_size() -> None:
    collector = ResponseCollector("sinopac.com")
    page = SimpleNamespace()
    frame = SimpleNamespace(page=page)
    page.main_frame = frame
    request = SimpleNamespace(
        url="https://mma.sinopac.com/ws/bank/transdetail/ws_transdetailMerge.ashx?1",
        headers={}, method="POST", post_data="", redirected_from=None, frame=frame,
    )
    collector._on_request(request)
    collector._on_response(SimpleNamespace(
        url=request.url,
        request=request,
        status=200,
        headers={
            "content-type": "application/json",
            "content-length": "1",
            "content-encoding": "identity",
        },
        body=lambda: b" " * 5_000_001,
        json=lambda: pytest.fail("bounded response must not call resp.json"),
    ))

    assert collector.hits[0].body_size == 5_000_001
    assert collector.hits[0].resp_json is None


def test_response_collector_preserves_bounded_form_body_for_exact_validation() -> None:
    collector = ResponseCollector("sinopac.com")
    page = SimpleNamespace()
    frame = SimpleNamespace(page=page)
    page.main_frame = frame
    body = "Acct=x&" + "A" * 600
    request = SimpleNamespace(
        url="https://mma.sinopac.com/ws/bank/transdetail/ws_transdetailMerge.ashx?1",
        headers={}, method="POST", post_data=body, redirected_from=None, frame=frame,
    )
    collector._on_request(request)
    collector._on_response(SimpleNamespace(
        url=request.url, request=request, status=200,
        headers={
            "content-type": "application/json",
            "content-length": "2",
            "content-encoding": "identity",
        },
        body=lambda: b"{}",
    ))

    assert collector.hits[0].req_body == body


def test_response_collector_marks_oversized_bounded_request_body() -> None:
    collector = ResponseCollector("sinopac.com")
    page = SimpleNamespace()
    frame = SimpleNamespace(page=page)
    page.main_frame = frame
    request = SimpleNamespace(
        url="https://mma.sinopac.com/ws/bank/transdetail/ws_debitacct.ashx?1",
        headers={}, method="POST", post_data="A" * 16_385,
        redirected_from=None, frame=frame,
    )
    collector._on_request(request)
    collector._on_response(SimpleNamespace(
        url=request.url, request=request, status=200,
        headers={
            "content-type": "application/json",
            "content-length": "2",
            "content-encoding": "identity",
        },
        body=lambda: b"{}",
    ))

    assert collector.hits[0].req_body == {"__oversize__": True}


def test_response_collector_marks_explicit_json_null_request_body() -> None:
    collector = ResponseCollector("sinopac.com")
    page = SimpleNamespace()
    frame = SimpleNamespace(page=page)
    page.main_frame = frame
    request = SimpleNamespace(
        url="https://mma.sinopac.com/ws/bank/transdetail/ws_debitacct.ashx?1",
        headers={}, method="POST", post_data="null",
        redirected_from=None, frame=frame,
    )
    collector._on_request(request)
    collector._on_response(SimpleNamespace(
        url=request.url, request=request, status=200,
        headers={
            "content-type": "application/json",
            "content-length": "2",
            "content-encoding": "identity",
        },
        body=lambda: b"{}",
    ))

    assert collector.hits[0].req_body == {"__json_null__": True}


@pytest.mark.parametrize(
    "mutation",
    [
        "method", "host", "path", "params", "fragment", "query", "status", "mime", "redirect", "keys",
        "form_keys", "account", "range", "query_type", "header", "max_month",
        "coverage", "head_info", "head_order", "row_keys", "row_date", "row_money", "row_overflow",
        "row_description_html", "row_noncanonical_datetime", "row_noncanonical_date",
        "empty_marker", "record_count", "body_size", "business_date", "is_obu",
        "head_keys", "sequence", "duplicate_row", "frame",
    ],
)
def test_sinopac_history_response_fails_closed(mutation) -> None:
    hit = _history_hit()
    if mutation == "method":
        hit.method = "GET"
    elif mutation == "host":
        hit.url = "https://evil.example/ws/bank/transdetail/ws_transdetailMerge.ashx"
    elif mutation == "path":
        hit.url = "https://mma.sinopac.com/evil/ws_transdetailMerge.ashx"
    elif mutation == "params":
        hit.raw_url = hit.raw_url.replace(".ashx?", ".ashx;jsessionid=opaque?")
    elif mutation == "fragment":
        hit.raw_url += "#opaque"
    elif mutation == "query":
        hit.raw_url += "&evil=1"
    elif mutation == "status":
        hit.status = 500
    elif mutation == "mime":
        hit.content_type = "text/html"
    elif mutation == "redirect":
        hit.redirected = True
    elif mutation == "body_size":
        hit.body_size = 5_000_001
    elif mutation == "keys":
        hit.resp_json[0]["extra"] = 1
    elif mutation == "form_keys":
        hit.req_body += "&extra=1"
    elif mutation == "account":
        hit.req_body = hit.req_body.replace(ACCOUNT, "99999999999999")
    elif mutation == "range":
        hit.req_body = hit.req_body.replace("StartDate=20260801", "StartDate=20260701")
    elif mutation == "query_type":
        hit.req_body = hit.req_body.replace("QueryType=3", "QueryType=2")
    elif mutation == "business_date":
        hit.req_body = hit.req_body.replace("BusinessDate=20260831", "BusinessDate=29990101")
    elif mutation == "header":
        hit.resp_json[0]["Header"] = "FAILED"
    elif mutation == "max_month":
        hit.resp_json[0]["MaxMonth"] = "12"
    elif mutation == "coverage":
        hit.resp_json[0]["BeginDate"] = "20260802"
    elif mutation == "head_info":
        hit.resp_json[0]["HeadInfo"].pop()
    elif mutation == "head_order":
        hit.resp_json[0]["HeadInfo"].reverse()
    elif mutation == "head_keys":
        hit.resp_json[0]["HeadInfo"][0]["extra"] = "bad"
    elif mutation == "row_keys":
        hit.resp_json[0]["SubInfo"][0].pop("DataText11")
    elif mutation == "row_date":
        hit.resp_json[0]["SubInfo"][0]["DataText1"] = "2026/09/01<br />12:34"
    elif mutation == "row_money":
        hit.resp_json[0]["SubInfo"][0]["DataText4"] = "NaN"
    elif mutation == "row_overflow":
        hit.resp_json[0]["SubInfo"][0]["DataText4"] = "+2147483648"
    elif mutation == "row_description_html":
        hit.resp_json[0]["SubInfo"][0]["DataText3"] = "<b></b>"
    elif mutation == "row_noncanonical_datetime":
        hit.resp_json[0]["SubInfo"][0]["DataText1"] = "2026/8/1<br />1:02"
    elif mutation == "row_noncanonical_date":
        hit.resp_json[0]["SubInfo"][0]["DataText2"] = "2026/8/1"
    elif mutation == "empty_marker":
        hit = _history_hit(rows=[], message="系統忙碌")
    elif mutation == "is_obu":
        hit.resp_json[0]["isOBU"] = "maybe"
    elif mutation == "sequence":
        hit.request_sequence = 0
    elif mutation == "duplicate_row":
        hit.resp_json[0]["SubInfo"].append(deepcopy(hit.resp_json[0]["SubInfo"][0]))
    elif mutation == "frame":
        hit.main_frame_request = False
    else:
        hit.resp_json[0]["RecordCount"] = "2"

    with pytest.raises(RuntimeError, match="sinopac-twd-history"):
        SinopacCrawler._validate_history_hit(
            hit,
            label=LABEL,
            identity=ACCOUNT,
            currency="TWD",
            start=date(2026, 8, 1),
            end=date(2026, 8, 31),
            business_date="20260831",
            as_of=date(2026, 8, 31),
        )


def test_sinopac_history_accepts_nonempty_and_exact_empty() -> None:
    complete = SinopacCrawler._validate_history_hit(
        _history_hit(), label=LABEL, identity=ACCOUNT, currency="TWD",
        start=date(2026, 8, 1), end=date(2026, 8, 31),
        business_date="20260831", as_of=date(2026, 8, 31),
    )
    empty = SinopacCrawler._validate_history_hit(
        _history_hit(rows=[]), label=LABEL, identity=ACCOUNT, currency="TWD",
        start=date(2026, 8, 1), end=date(2026, 8, 31),
        business_date="20260831", as_of=date(2026, 8, 31),
    )
    assert complete["status"] == "complete"
    assert complete["rows"] == 1
    assert empty == {"records": [], "status": "explicit_empty", "rows": 0}


@pytest.mark.parametrize(
    ("empty", "dom_bound"),
    [(False, True), (True, True), (False, False)],
)
def test_sinopac_collect_uses_native_account_date_and_query_controls(
    monkeypatch, empty, dom_bound,
) -> None:
    collector = ResponseCollector("sinopac.com")
    crawler = object.__new__(SinopacCrawler)
    crawler.transaction_cursors = {}
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    monkeypatch.setattr(crawler, "_twd_inventory", lambda _collector, **_kwargs: [{
        "label": LABEL, "identity": ACCOUNT, "currency": "TWD",
    }])
    monkeypatch.setattr(
        crawler, "_history_range",
        lambda _identity, *, end, mode: (date(2026, 8, 1), date(2026, 8, 31)),
    )

    values = {"start": "", "end": ""}
    clicks = []

    class Locator:
        def __init__(self, kind):
            self.kind = kind

        def count(self):
            return 1

        def nth(self, _index):
            return self

        def is_visible(self):
            return True

        def is_enabled(self):
            return True

        def click(self, **_kwargs):
            clicks.append(self.kind)
            if self.kind == "query":
                collector._issued_endpoint_counts["ws_transdetailMerge.ashx"] = 1
                collector.hits.append(_history_hit(
                    start=values["start"], end=values["end"],
                    rows=[] if empty else None,
                ))

        def fill(self, value):
            values[self.kind] = value

        def input_value(self):
            return values[self.kind]

    class Page:
        def goto(self, *_args, **_kwargs):
            collector._issued_endpoint_counts["ws_debitacct.ashx"] = 1
            collector.hits.append(_inventory_hit())
            return None

        def wait_for_timeout(self, _milliseconds):
            return None

        def locator(self, selector):
            return Locator({
                "#spanDebitAccount": "toggle",
                "#divDebitAccount [onclick]": "option",
                "#StartDate": "start",
                "#EndDate": "end",
                "#btnQuery": "query",
            }[selector])

        def evaluate(self, script, _arg=None):
            if "__hermesSinopacExpectedRows = rows" in script:
                return None
            if "map(e => e.getAttribute('onclick')" in script:
                return [f"setDebitAccount('{LABEL}', '{ACCOUNT}', 'TWD')"]
            if "Object.fromEntries" in script:
                return {
                    "Acct": LABEL,
                    "AcctValue": ACCOUNT,
                    "Curr": "TWD",
                    "BusinessDate": "20260831",
                }
            if "if(!table)return null" in script:
                return [1, 1]
            if "ListingTable" in script:
                if empty:
                    return {
                        "tables": 0,
                        "rows": 0,
                        "visibleRows": 0,
                        "pagers": 0,
                        "errors": 0,
                        "visible": False,
                        "emptyMarker": True,
                        "freshEmpty": True,
                        "bound": dom_bound,
                        "signature": None,
                        "mutations": 1,
                    }
                return {
                    "tables": 1,
                    "rows": 2,
                    "visibleRows": 2,
                    "pagers": 0,
                    "errors": 0,
                    "visible": True,
                    "emptyMarker": False,
                    "freshEmpty": False,
                    "bound": dom_bound,
                    "signature": [2, 2],
                    "mutations": 1,
                }
            raise AssertionError("unexpected evaluate")

    if not dom_bound:
        with pytest.raises(RuntimeError, match="sinopac-twd-history-result-table"):
            crawler._collect_transactions(Page(), collector)
        return

    result = crawler._collect_transactions(Page(), collector)

    assert values == {"start": "20260801", "end": "20260831"}
    assert clicks == ["toggle", "option", "query"]
    assert result["results"][0]["receipt"]["status"] == (
        "explicit_empty" if empty else "complete"
    )
    assert result["coverage"]["domains"][0]["expected"][0]["identity"] == ACCOUNT


def test_sinopac_collect_publishes_explicit_empty_inventory_coverage(monkeypatch) -> None:
    collector = ResponseCollector("sinopac.com")
    hit = _inventory_hit()
    hit.resp_json[0]["SubInfo"] = []
    crawler = object.__new__(SinopacCrawler)
    crawler.transaction_cursors = {}
    monkeypatch.delenv("BANK_CRAWLER_HISTORY_MODE", raising=False)

    class Page:
        def goto(self, *_args, **_kwargs):
            collector._issued_endpoint_counts["ws_debitacct.ashx"] = 1
            collector.hits.append(hit)
            return None

        def wait_for_timeout(self, _milliseconds):
            return None

        def evaluate(self, script):
            return 0 if "const tables=document.querySelectorAll('table')" in script else []

    result = crawler._collect_transactions(Page(), collector)
    domain = result["coverage"]["domains"][0]

    assert result["results"] == []
    assert result["inventory"] == []
    assert result["coverage"]["mode"] == "full"
    assert domain["expected"] == []
    assert domain["empty_window"]["status"] == "explicit_empty"


@pytest.mark.parametrize(
    "blocker_token",
    ["document.querySelectorAll('table')", ".modal.in", "[aria-modal=true]", "[class*=loading-overlay]"],
)
def test_sinopac_empty_inventory_rejects_result_or_overlay_blocker(
    monkeypatch, blocker_token,
) -> None:
    collector = ResponseCollector("sinopac.com")
    hit = _inventory_hit()
    hit.resp_json[0]["SubInfo"] = []
    crawler = object.__new__(SinopacCrawler)
    crawler.transaction_cursors = {}

    class Page:
        def goto(self, *_args, **_kwargs):
            collector._issued_endpoint_counts["ws_debitacct.ashx"] = 1
            collector.hits.append(hit)

        def wait_for_timeout(self, _milliseconds):
            return None

        def evaluate(self, script):
            if "const tables=document.querySelectorAll('table')" in script:
                return 1 if blocker_token in script else 0
            return []

    with pytest.raises(RuntimeError, match="sinopac-twd-history-empty-inventory-blocked"):
        crawler._collect_transactions(Page(), collector)


def test_sinopac_empty_inventory_rechecks_dialog_after_dom_probe(monkeypatch) -> None:
    collector = ResponseCollector("sinopac.com")
    hit = _inventory_hit()
    hit.resp_json[0]["SubInfo"] = []
    crawler = object.__new__(SinopacCrawler)
    crawler.transaction_cursors = {}
    crawler._shared_dialog_blocked = False

    class Page:
        def goto(self, *_args, **_kwargs):
            collector._issued_endpoint_counts["ws_debitacct.ashx"] = 1
            collector.hits.append(hit)

        def wait_for_timeout(self, _milliseconds):
            return None

        def evaluate(self, script):
            if "const tables=document.querySelectorAll('table')" in script:
                crawler._shared_dialog_blocked = True
                return 0
            return []

    with pytest.raises(RuntimeError, match="sinopac-twd-history-dialog"):
        crawler._collect_transactions(Page(), collector)


def test_sinopac_coverage_fixture_is_valid() -> None:
    assert validate_history_coverage(
        _coverage(), expected_mode="full", expected_domains=frozenset({"twd_transactions"}),
    )["identities"] == 1


def test_sinopac_persistence_accepts_authoritative_empty_inventory(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    payload = BankCollectResult(
        debit_accounts=[],
        twd_transactions=[],
        history_coverage={
            "mode": "full",
            "as_of": "2026-08-31",
            "domains": [{
                "domain": "twd_transactions",
                "expected": [],
                "windows": [],
                "empty_window": {
                    "start": "2025-09-01",
                    "end": "2026-08-31",
                    "status": "explicit_empty",
                    "pages": 1,
                },
            }],
        },
        card_bill_facts_ok=False,
    ).to_dict()
    assert "debit_accounts" not in payload
    assert "twd_transactions" not in payload
    try:
        delta = persist_collected("sinopac", payload, store)
        assert delta["twd_txn_new"] == 0
    finally:
        store.close()


def test_sinopac_persistence_requires_attested_coverage_before_write(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    payload = _persist_payload()
    payload.pop("history_coverage")
    try:
        with pytest.raises(ValueError, match="sinopac persistence requires history coverage"):
            persist_collected("sinopac", payload, store)
        assert store.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    finally:
        store.close()


def test_sinopac_persistence_rejects_operation_over_5mb(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    payload = _persist_payload()
    target = next(item for item in payload["twd_transactions"] if item["records"])
    base = target["records"][0]
    target["records"] = [
        {**base, "DataText6": f"{index:04d}" + "x" * 1900}
        for index in range(3000)
    ]
    target["receipt"]["rows"] = len(target["records"])
    try:
        with pytest.raises(ValueError, match="invalid SinoPac history coverage"):
            persist_sinopac(payload, store)
        row = store.conn.execute("SELECT COUNT(*) FROM twd_transactions").fetchone()
        assert row is not None and row[0] == 0
    finally:
        store.close()


def test_sinopac_persistence_rejects_boolean_page_and_row_counts(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    payload = _persist_payload()
    target = payload["twd_transactions"][-1]
    target["receipt"]["pages"] = True
    target["receipt"]["rows"] = True
    payload["history_coverage"]["domains"][0]["windows"][-1]["pages"] = True
    try:
        with pytest.raises(ValueError, match="history coverage"):
            persist_sinopac(payload, store)
    finally:
        store.close()


def test_sinopac_direct_persister_requires_history_coverage(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    try:
        with pytest.raises(ValueError, match="invalid SinoPac history coverage"):
            persist_sinopac({"bank_balance": []}, store)
        row = store.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()
        assert row is not None and row[0] == 0
    finally:
        store.close()


def test_sinopac_persistence_uses_coverage_as_of_for_card_expiry(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(sinopac_persist_module, "_today", lambda: date(2026, 9, 1))
    expiry_months = []
    original_expired = sinopac_persist_module._mmyy_expired

    def expired(value, today_yyyy_mm=None):
        expiry_months.append(today_yyyy_mm)
        return original_expired(value, today_yyyy_mm)

    monkeypatch.setattr(sinopac_persist_module, "_mmyy_expired", expired)
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    payload = _persist_payload()
    payload["all_cards"] = {
        "Result": {"Items": [{"CardNo": "1234", "ExpDate": "0826"}]},
    }
    try:
        persist_sinopac(payload, store)
        row = store.conn.execute(
            "SELECT active FROM cards WHERE card_no = ?", ("1234",),
        ).fetchone()
        assert row is not None and row[0] == 1
        assert expiry_months == ["2026-08"]
    finally:
        store.close()


def test_sinopac_card_expiry_uses_canonical_year_month() -> None:
    assert sinopac_persist_module._mmyy_expired("0926", "2026-10") is True


def test_sinopac_dom_binding_is_positional_not_substring_only() -> None:
    source = inspect.getsource(SinopacCrawler._collect_transactions)
    assert "cells.length===row.length" in source
    assert "cells[index]===text" in source
    assert "連線中斷|disconnected|retry" in source
    assert "!e.closest('#ListingTable')" not in source


def test_sinopac_persistence_rejects_html_only_description(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    payload = _persist_payload()
    payload["twd_transactions"][-1]["records"][0]["DataText3"] = "<b>&nbsp;</b>"
    try:
        with pytest.raises(ValueError, match="history coverage"):
            persist_sinopac(payload, store)
    finally:
        store.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [("DataText1", "2026/8/1<br />1:02"), ("DataText2", "2026/8/1")],
)
def test_sinopac_persistence_rejects_noncanonical_dates(
    tmp_path, monkeypatch, field, value,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    payload = _persist_payload()
    payload["twd_transactions"][-1]["records"][0][field] = value
    try:
        with pytest.raises(ValueError, match="history coverage"):
            persist_sinopac(payload, store)
    finally:
        store.close()


def test_sinopac_direct_persister_remains_durable(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    persist_sinopac(_persist_payload(), store)
    store.close()

    reopened = BankStore("sinopac", user_id=1, source_account_id=7)
    try:
        row = reopened.conn.execute(
            "SELECT txn_datetime FROM twd_transactions",
        ).fetchone()
        assert row["txn_datetime"] == "2026-08-31T12:34:00"
    finally:
        reopened.close()


def test_sinopac_direct_persister_rolls_back_on_late_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    monkeypatch.setattr(
        store, "refresh_card_pending",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("pending failed")),
    )
    try:
        with pytest.raises(RuntimeError, match="pending failed"):
            persist_sinopac(_persist_payload(), store)
        store.commit()
        assert store.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
        assert store.conn.execute("SELECT COUNT(*) FROM twd_transactions").fetchone()[0] == 0
    finally:
        store.close()


def test_sinopac_persistence_rolls_back_all_writes_on_cursor_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    monkeypatch.setattr(
        store, "record_history_coverage_cursors",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cursor write failed")),
    )
    try:
        with pytest.raises(RuntimeError, match="cursor write failed"):
            persist_collected("sinopac", _persist_payload(), store)
        assert store.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
        assert store.conn.execute("SELECT COUNT(*) FROM twd_transactions").fetchone()[0] == 0
        assert store.conn.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0] == 0
    finally:
        store.close()


def test_sinopac_incremental_persistence_binds_existing_cursor(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    monkeypatch.setattr(
        store, "latest_twd_transaction_dates", lambda: {ACCOUNT: date(2026, 8, 1)},
    )
    try:
        with pytest.raises(ValueError, match="SinoPac history"):
            persist_collected("sinopac", _persist_payload(mode="incremental"), store)
        assert store.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    finally:
        store.close()


@pytest.mark.parametrize("mode", ("full", "incremental"))
def test_sinopac_persistence_rejects_future_cursor_in_all_modes(tmp_path, monkeypatch, mode) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    monkeypatch.setattr(
        store, "latest_twd_transaction_dates", lambda: {ACCOUNT: date(2026, 9, 1)},
    )
    try:
        with pytest.raises(ValueError, match="SinoPac history"):
            persist_collected("sinopac", _persist_payload(mode=mode), store)
        assert store.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    finally:
        store.close()


def test_sinopac_persistence_rejects_identity_inventory_mismatch(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("sinopac", user_id=1, source_account_id=7)
    payload = _persist_payload()
    payload["debit_accounts"][0]["identity"] = "99999999999999"
    try:
        with pytest.raises(ValueError, match="SinoPac history"):
            persist_collected("sinopac", payload, store)
        assert store.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    finally:
        store.close()
