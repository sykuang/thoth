from datetime import date

import pytest

from backend.banks.esun import EsunCrawler, _esun_history_window, _parse_esun_twd_html_response
from backend.core.base import ApiHit, ResponseCollector, _OriginGuardProxy
from backend.core.persist import persist_collected
from backend.core.persist.esun import (
    _esun_twd_integer,
    _parse_esun_twd_txn_results,
    _validated_esun_twd_row,
)
from backend.core.store import BankStore


def _form_contract() -> dict:
    return {
        "formCount": 1,
        "method": "POST",
        "action": "https://ebank.esunbank.com.tw/fao/fao01002/FAO01002_Home.faces?ajax=true",
        "accountCount": 1,
        "startCount": 1,
        "endCount": 1,
        "actionCount": 1,
        "viewStateCount": 1,
        "periodCount": 1,
        "sortCount": 1,
        "queryCount": 1,
        "sameForm": True,
        "actionValue": "query",
        "viewState": "state",
    }


def test_esun_opts_in_only_twd_transactions():
    assert EsunCrawler.HISTORY_COVERAGE_REQUIRED is True
    assert frozenset({"twd_transactions"}) == EsunCrawler.HISTORY_COVERAGE_DOMAINS


def test_esun_full_and_incremental_windows_use_bank_year_and_cursor_overlap(monkeypatch):
    crawler = object.__new__(EsunCrawler)
    crawler.transaction_cursors = {
        "twd_transactions": {"0900000087022": date(2026, 8, 20)},
    }

    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    assert _esun_history_window(crawler, "0900000087022", date(2026, 8, 30)) == (
        date(2025, 8, 31), date(2026, 8, 30),
    )

    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "incremental")
    assert _esun_history_window(crawler, "0900000087022", date(2026, 8, 30)) == (
        date(2026, 8, 13), date(2026, 8, 30),
    )
    assert _esun_history_window(crawler, "new-account", date(2026, 8, 30))[0] == date(2025, 8, 31)


def test_esun_history_window_handles_leap_day(monkeypatch):
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    crawler = object.__new__(EsunCrawler)
    crawler.transaction_cursors = {}
    assert _esun_history_window(crawler, "acct", date(2024, 2, 29))[0] == date(2023, 3, 1)


def test_esun_requires_one_authoritative_query_frame():
    class Frame:
        def __init__(self, has_form):
            self.has_form = has_form

        def evaluate(self, _script):
            if self.has_form == "error":
                raise RuntimeError("detached")
            return _form_contract() if self.has_form else {}

    owned = Frame(True)
    assert EsunCrawler._unique_twd_query_frame([Frame(False), owned]) is owned
    for frames in ([], [Frame(True), Frame(True)], [owned, Frame("error")]):
        with pytest.raises(RuntimeError, match="esun-twd-history-form"):
            EsunCrawler._unique_twd_query_frame(frames)


def test_esun_query_form_requires_unique_same_form_canonical_controls():
    class Frame:
        def __init__(self, contract):
            self.contract = contract

        def evaluate(self, _script):
            return self.contract

    assert EsunCrawler._twd_form_contract(Frame(_form_contract())) == _form_contract()
    for key, value in (
        ("formCount", 2), ("method", "GET"), ("accountCount", 2),
        ("startCount", 0), ("queryCount", 2), ("viewStateCount", 2),
        ("sameForm", False), ("actionValue", ""), ("viewState", ""),
        ("action", "https://attacker.example/query"),
        ("action", _form_contract()["action"] + "&"),
        ("action", _form_contract()["action"].replace("true", "%74rue")),
    ):
        with pytest.raises(ValueError, match="esun-twd-history-form"):
            EsunCrawler._twd_form_contract(Frame({**_form_contract(), key: value}))


def test_esun_waits_for_home_widget_form_after_navigation():
    class Frame:
        def evaluate(self, _script):
            return _form_contract()

    class Page:
        def __init__(self):
            self.polls = 0
            self.waits = []

        @property
        def frames(self):
            self.polls += 1
            return [] if self.polls < 3 else [Frame()]

        def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

    page = Page()
    assert isinstance(EsunCrawler._wait_for_twd_query_frame(page), Frame)
    assert page.waits == [100, 100]


def test_esun_query_inventory_is_authoritative_and_unique():
    options = [
        {"index": 1, "text": "臺幣綜存 0900000087022", "value": "opaque-a"},
        {"index": 2, "text": "臺幣活存 0900000087023", "value": "opaque-b"},
    ]
    assert [row["identity"] for row in EsunCrawler._validated_twd_options(options)] == [
        "0900000087022", "0900000087023",
    ]
    assert len(EsunCrawler._validated_twd_options([
        {"index": 0, "text": "===請選擇===", "value": ""},
        *options,
    ])) == 2
    live_options = [
        {"index": 0, "text": "===請選擇===", "value": "dynamic-placeholder"},
        {"index": 1, "text": "外幣活存 0900000087021", "value": "opaque-fx"},
        {"index": 2, "text": "臺幣綜存 0900000087022", "value": "opaque-twd"},
    ]
    assert EsunCrawler._validated_twd_options(live_options) == [{
        **live_options[2], "identity": "0900000087022",
    }]
    with pytest.raises(RuntimeError, match="esun-twd-history-inventory"):
        EsunCrawler._validated_twd_options([
            {**live_options[0], "value": "opaque-twd"}, live_options[2],
        ])

    with pytest.raises(RuntimeError, match="esun-twd-history-inventory"):
        EsunCrawler._validated_twd_options([])
    with pytest.raises(RuntimeError, match="esun-twd-history-inventory"):
        EsunCrawler._validated_twd_options([
            {"index": 0, "text": "===請選擇===", "value": ""},
        ])
    for fake_placeholder in (
        {"index": 0, "text": "請選擇帳戶", "value": ""},
        {"index": 1, "text": "===請選擇===", "value": ""},
    ):
        with pytest.raises(RuntimeError, match="esun-twd-history-inventory"):
            EsunCrawler._validated_twd_options([fake_placeholder])
    with pytest.raises(RuntimeError, match="esun-twd-history-inventory"):
        EsunCrawler._validated_twd_options([
            {"index": 0, "text": "===請選擇===", "value": ""},
            {"index": 1, "text": "外幣帳戶", "value": "junk"},
        ])
    with pytest.raises(RuntimeError, match="esun-twd-history-inventory"):
        EsunCrawler._validated_twd_options([*options, dict(options[0])])
    with pytest.raises(RuntimeError, match="esun-twd-history-inventory"):
        EsunCrawler._validated_twd_options([
            options[0], {**options[1], "value": options[0]["value"]},
        ])
    with pytest.raises(RuntimeError, match="esun-twd-history-inventory"):
        EsunCrawler._validated_twd_options([
            {"index": 1, "text": "臺幣綜存 acct", "value": "opaque-a"},
        ])
    for unknown in (
        {"index": 1, "text": "", "value": ""},
        {"index": 1, "text": "Savings 0900000087022", "value": "opaque-a"},
        {"index": 1, "text": "未分類帳戶", "value": "opaque-a"},
    ):
        with pytest.raises(RuntimeError, match="esun-twd-history-inventory"):
            EsunCrawler._validated_twd_options([*options, unknown])


def test_esun_transport_binds_exact_post_response_account_and_range():
    url = "https://ebank.esunbank.com.tw/fao/fao01002/FAO01002_Home.faces?ajax=true"
    frame_url = "https://ebank.esunbank.com.tw/fco/fco08001/FCO08001_Home.faces"
    hit = ApiHit(
        url=url,
        method="POST",
        status=200,
        content_type="text/html;charset=UTF-8",
        req_body={"fieldsExact": True, "actionExact": True, "viewStateExact": True, "frameExact": True},
    )
    assert EsunCrawler._validated_twd_transport(
        [hit], result_url=frame_url,
    ) is hit

    for bad in (
        ApiHit(**{**hit.__dict__, "method": "GET"}),
        ApiHit(**{**hit.__dict__, "status": 500}),
        ApiHit(**{**hit.__dict__, "url": "https://attacker.example/FAO01002.faces"}),
        ApiHit(**{**hit.__dict__, "url": url.replace("ajax=true", "ajax=false")}),
        ApiHit(**{**hit.__dict__, "url": url.replace("FAO01002_Home.faces", "FAO01002_Home.faces;jsessionid=opaque")}),
        ApiHit(**{
            **hit.__dict__,
            "req_body": {**hit.req_body, "fao01002:dract": ["opaque-b"]},
        }),
        ApiHit(**{
            **hit.__dict__,
            "req_body": {
                "evil": ["opaque-a"],
                "x": ["2025/08/31"],
                "y": ["2026/08/30"],
            },
        }),
        ApiHit(**{
            **hit.__dict__,
            "req_body": {**hit.req_body, "fao01002:dract": ["opaque-a", "opaque-b"]},
        }),
    ):
        with pytest.raises(RuntimeError, match="esun-twd-history-transport"):
            EsunCrawler._validated_twd_transport(
                [bad], result_url=url,
            )
    with pytest.raises(RuntimeError, match="esun-twd-history-transport"):
        EsunCrawler._validated_twd_transport(
            [hit], result_url=f"{frame_url}?unexpected=1",
        )


def _live_ajax_html() -> str:
    return """
    <html><body><table id="fao01002:grid_DataGridBody" class="table_ver">
      <tr><th>交易日期/時間</th><th>摘要</th><th>支出</th><th>存入</th><th>帳戶餘額</th></tr>
      <tr><td><span>2026/08/20</span><span>12:00:00</span></td><td>利息</td><td></td><td>2</td><td>84</td></tr>
    </table></body></html>
    """


def test_esun_live_ajax_html_projects_operation_bound_grid_without_total_label():
    snapshot = _parse_esun_twd_html_response(_live_ajax_html())
    assert snapshot["gridRows"] == [["2026/08/20 12:00:00", "利息", "", "2", "84"]]
    assert snapshot["gridRowCount"] == 1
    assert snapshot["totalCount"] is None
    assert snapshot["pager"] == {"present": False, "actionableNext": 0}


def test_esun_ajax_html_rejects_failed_malformed_duplicate_or_paginated_results():
    header = (
        '<tr><th>交易日期/時間</th><th>摘要</th><th>支出</th>'
        '<th>存入</th><th>帳戶餘額</th></tr>'
    )
    valid_row = (
        '<tr><td>2026/08/20 12:00:00</td><td>利息</td>'
        '<td></td><td>2</td><td>84</td></tr>'
    )
    for body in (
        f'<div class="error">系統錯誤</div><table id="fao01002:grid_DataGridBody">{header}{valid_row}</table>',
        f'<table id="fao01002:grid_DataGridBody">{header}{valid_row}<tr><td>broken</td></tr></table>',
        f'<table id="fao01002:grid_DataGridBody">{header}{valid_row}<table id="fao01002:grid_DataGridBody"></table></table>',
        f'<table id="fao01002:grid_DataGridBody">{header}<tr><th>交易日期/錯誤欄</th></tr>{valid_row}</table>',
        f'<table id="fao01002:grid_DataGridBody">{header}{valid_row}<tr><td>2026/08/21',
        f'<table id="fao01002:grid_DataGridBody">{header}<tr><tr>{valid_row}</tr></tr></table>',
        f'<table id="fao01002:grid_DataGridBody">{header}<tr><td>2026/08/21</th></tr></table>',
        f'<table hidden id="fao01002:grid_DataGridBody">{header}{valid_row}</table>',
        f'<div hidden><table id="fao01002:grid_DataGridBody">{header}{valid_row}</table></div>',
        f'<article hidden><table id="fao01002:grid_DataGridBody">{header}{valid_row}</table></article>',
        f'<div style="display:\t none"><table id="fao01002:grid_DataGridBody">{header}{valid_row}</table></div>',
        f'<div style="opacity: 0"><table id="fao01002:grid_DataGridBody">{header}{valid_row}</table></div>',
        f'<div style="opacity:0!important"><table id="fao01002:grid_DataGridBody">{header}{valid_row}</table></div>',
        f'<div style="opacity:0%"><table id="fao01002:grid_DataGridBody">{header}{valid_row}</table></div>',
        f'<div style="opacity:.0"><table id="fao01002:grid_DataGridBody">{header}{valid_row}</table></div>',
        f'<table id="fao01002:grid_DataGridBody">{header}{valid_row.replace("<tr>", "<tr hidden>")}</table>',
        f'<table id="fao01002:grid_DataGridBody">{header}{valid_row.replace("<td>2</td>", "<td hidden>2</td>")}</table>',
        f'<table id="fao01002:grid_DataGridBody">{header}{valid_row.replace("<td>2</td>", "<td><span hidden>2</span></td>")}</table>',
        f'<div aria-busy="true"></div><table id="fao01002:grid_DataGridBody">{header}{valid_row}</table>',
    ):
        with pytest.raises(ValueError, match="invalid E.SUN TWD"):
            _parse_esun_twd_html_response(body)

    for pager in (
        '<a rel="next">Next</a>', '<a title="Next page"></a>',
        '<input value="下一頁">', '<a onclick="goPage(2)">2</a>',
        '<div class="pagination"><a>2</a></div>', '<a>下一頁</a>',
    ):
        snapshot = _parse_esun_twd_html_response(
            f'<table id="fao01002:grid_DataGridBody">{header}{valid_row}</table>{pager}'
        )
        assert snapshot["pager"]["present"] is True


def test_esun_ajax_html_binds_footer_total_and_explicit_empty():
    with pytest.raises(ValueError, match="invalid E.SUN TWD total"):
        _parse_esun_twd_html_response(
            _live_ajax_html() + '<div>共 1 筆</div><div>總計 2 筆</div>'
        )
    with pytest.raises(ValueError, match="invalid E.SUN TWD total"):
        _parse_esun_twd_html_response(
            _live_ajax_html() + '<div>共 1 筆</div><div>共 1 筆</div>'
        )
    snapshot = _parse_esun_twd_html_response(_live_ajax_html() + '<div>共 2 筆</div>')
    assert snapshot["totalCount"] == 2

    empty = _parse_esun_twd_html_response(
        '<table id="fao01002:grid_DataGridBody">'
        '<tr><th>交易日期/時間</th><th>摘要</th><th>支出</th><th>存入</th><th>帳戶餘額</th></tr>'
        '<tr><td>查無交易資料</td></tr></table>'
    )
    assert empty["totalCount"] == 0
    assert empty["emptyMarker"] == "查無交易資料"


def test_esun_response_capture_requires_owned_request_and_bounded_body():
    class Request:
        method = "POST"
        redirected_from: object | None = None
        post_data = (
            "fao01002%3Adract=opaque-a"
            "&fao01002%3AstartDate=2025%2F08%2F31"
            "&fao01002%3AendDate=2026%2F08%2F30"
            "&fao01002%3AlinkCommand=query"
            "&javax.faces.ViewState=state"
        )

        def __init__(self, frame):
            self.frame = frame
            self.url = "https://ebank.esunbank.com.tw/fao/fao01002/FAO01002_Home.faces?ajax=true"

    frame = object()
    owned = Request(frame)
    requests = []
    proxy = _OriginGuardProxy(frame, lambda: None)
    EsunCrawler._capture_twd_request(
        owned, requests, proxy, owned.url, "opaque-a",
        date(2025, 8, 31), date(2026, 8, 30),
        "query", "state",
    )
    assert len(requests) == 1
    EsunCrawler._capture_twd_request(
        Request(object()), requests, proxy, owned.url, "opaque-a",
        date(2025, 8, 31), date(2026, 8, 30),
        "query", "state",
    )
    assert len(requests) == 1

    class Response:
        url = owned.url
        request = owned
        status = 200
        headers = {"content-type": "text/html", "content-length": str(len(_live_ajax_html()))}

        @staticmethod
        def text():
            return _live_ajax_html()

    hits = []
    EsunCrawler._capture_twd_response(Response(), hits, requests)
    assert len(hits) == 1
    EsunCrawler._capture_twd_response(
        type("Oversize", (), {
            **Response.__dict__,
            "headers": {"content-type": "text/html", "content-length": "1000001"},
        })(),
        hits,
        requests,
    )
    assert len(hits) == 1

    missing_length_calls = []

    class MissingLength(Response):
        headers = {"content-type": "text/html"}

        @staticmethod
        def text():
            missing_length_calls.append(True)
            return _live_ajax_html()

    EsunCrawler._capture_twd_response(MissingLength(), hits, requests)
    assert len(hits) == 1
    assert missing_length_calls == []

    for status, redirected_from in ((206, None), (200, object())):
        redirected = Request(frame)
        redirected.redirected_from = redirected_from
        owned_requests = []
        EsunCrawler._capture_twd_request(
            redirected, owned_requests, proxy, owned.url, "opaque-a",
            date(2025, 8, 31), date(2026, 8, 30),
            "query", "state",
        )
        response = Response()
        response.status = status
        response.request = redirected
        EsunCrawler._capture_twd_response(response, hits, owned_requests)
    assert len(hits) == 1


def test_esun_operation_wait_rejects_delayed_second_pair():
    requests = [object()]
    hits = [object()]

    class Page:
        ticks = 0

        def wait_for_timeout(self, milliseconds):
            assert milliseconds == 100
            self.ticks += 1
            if self.ticks == 7:
                requests.append(object())
                hits.append(object())

    with pytest.raises(RuntimeError, match="esun-twd-history-response-timeout"):
        EsunCrawler._wait_for_twd_operation(Page(), requests, hits)


def test_esun_operation_wait_requires_quiet_ticks_after_final_poll_hit():
    requests = []
    hits = []

    class Page:
        ticks = 0

        def wait_for_timeout(self, milliseconds):
            assert milliseconds == 100
            self.ticks += 1
            if self.ticks == 90:
                requests.append(object())
                hits.append(object())

    page = Page()
    EsunCrawler._wait_for_twd_operation(page, requests, hits)
    assert page.ticks == 95


def test_esun_operation_listener_keeps_only_required_fields_from_long_form():
    frame = object()

    class Request:
        method = "POST"
        frame: object | None = None
        url = "https://ebank.esunbank.com.tw/fao/fao01002/FAO01002_Home.faces?ajax=true"
        post_data = (
            "javax.faces.ViewState=" + "x" * 2000
            + "&fao01002%3Adract=opaque-a"
            + "&fao01002%3AstartDate=2025%2F08%2F31"
            + "&fao01002%3AendDate=2026%2F08%2F30"
            + "&fao01002%3AlinkCommand=query"
        )

    Request.frame = frame

    class Response:
        url = "https://ebank.esunbank.com.tw/fao/fao01002/FAO01002_Home.faces?ajax=true"
        request = Request()
        status = 200
        headers = {"content-type": "text/html", "content-length": str(len(_live_ajax_html()))}

        @staticmethod
        def text():
            return _live_ajax_html()

    hits = []
    requests = []
    EsunCrawler._capture_twd_request(
        Response.request, requests, frame, Response.url, "opaque-a",
        date(2025, 8, 31), date(2026, 8, 30),
        "query", "x" * 2000,
    )
    EsunCrawler._capture_twd_response(Response(), hits, requests)
    assert len(hits) == 1
    assert hits[0].req_body == {
        "fieldsExact": True,
        "actionExact": True,
        "viewStateExact": True,
        "frameExact": True,
    }


def test_esun_generic_collector_keeps_history_posts_metadata_only():
    class Frame:
        url = "https://ebank.esunbank.com.tw/frame?account=PRIVATE"
        page = None

    class Request:
        url = "https://ebank.esunbank.com.tw/fao;jsessionid=PRIVATE/fao01002/FAO01002_Home.faces?ajax=true"
        method = "POST"
        headers = {}
        post_data = "javax.faces.ViewState=PRIVATE&fao01002%3Adract=PRIVATE"
        frame = Frame()
        redirected_from = None

    class Response:
        url = Request.url
        request = Request()
        status = 200
        headers = {"content-type": "text/html", "content-length": "10"}

    collector = ResponseCollector("esunbank.com.tw")
    collector._on_request(Response.request)
    collector._on_response(Response())
    assert len(collector.hits) == 1
    hit = collector.hits[0]
    assert hit.req_body is None
    assert hit.url == "https://ebank.esunbank.com.tw/fao/fao01002/FAO01002_Home.faces"
    assert hit.raw_url == hit.url
    assert hit.request_frame is None
    assert "PRIVATE" not in hit.url + hit.raw_url + hit.request_frame_url


def test_esun_result_uses_structured_date_cells_not_concatenated_grid_text():
    compact = _bound_result()
    compact["snapshot"]["gridText"] = "2026/08/2012:00:00利息284活存利息"
    assert EsunCrawler._validated_twd_history_result(
        compact,
        identity="0900000087022",
        start=date(2025, 8, 31),
        end=date(2026, 8, 30),
    )["status"] == "complete"


def test_esun_result_accepts_live_owned_home_widget_frame():
    result = _bound_result()
    result["url"] = "https://ebank.esunbank.com.tw/fco/fco08001/FCO08001_Home.faces"
    assert EsunCrawler._validated_twd_history_result(
        result,
        identity="0900000087022",
        start=date(2025, 8, 31),
        end=date(2026, 8, 30),
    )["status"] == "complete"


def test_esun_result_does_not_require_redundant_dom_echo_after_transport_binding():
    result = _bound_result()
    result["text"] = "交易明細\n2026/08/20\n12:00:00 利息 2 84 活存利息"
    result["snapshot"]["totalCount"] = None
    assert EsunCrawler._validated_twd_history_result(
        result,
        identity="0900000087022",
        start=date(2025, 8, 31),
        end=date(2026, 8, 30),
    )["status"] == "complete"


def test_esun_result_requires_fresh_unique_bound_scope():
    fresh = {"evidenceFresh": True, "resultFingerprint": "same-rows-new-node"}
    assert EsunCrawler._fresh_twd_result([fresh]) is fresh
    for candidates in ([], [fresh, fresh], [{"evidenceFresh": False, "resultFingerprint": "changed-outer-html"}]):
        with pytest.raises(RuntimeError):
            EsunCrawler._fresh_twd_result(candidates)


def _bound_result(*, grid: bool = True, empty: bool = False) -> dict:
    return {
        "account_no": "0900000087022",
        "start": "2025-08-31",
        "end": "2026-08-30",
        "status": "explicit_empty" if empty else "complete",
        "selected_identity": "0900000087022",
        "clicked_period": {"start": "2025/08/31", "end": "2026/08/30", "checked": True},
        "submit": {"clicked": "visible-query"},
        "url": "https://ebank.esunbank.com.tw/fco/fao01002/FAO01002.faces",
        "text": (
            "帳號 0900000087022 查詢期間 2025/08/31 至 2026/08/30\n"
            + ("查無交易資料" if empty else "交易明細")
        ),
        "snapshot": {
            "busy": False,
            "evidenceFresh": True,
            "hasGrid": grid,
            "gridCandidateCount": 1 if grid else 0,
            "gridText": (
                "交易日期 時間 摘要 提 存 帳戶餘額\n"
                "2026/08/20\n12:00:00 利息 2 84 活存利息\n"
                if grid else ""
            ),
            "gridRowCount": 1 if grid else 0,
            "gridRows": ([
                ["2026/08/20", "12:00:00", "利息", "", "2", "84", "活存利息"],
            ] if grid else []),
            "totalCount": 1 if grid else 0,
            "pager": {"present": False, "actionableNext": 0},
            "emptyMarker": "查無交易資料" if empty else None,
        },
    }


def _bound_coverage(*, status: str = "complete") -> dict:
    return {
        "mode": "full",
        "domains": [{
            "domain": "twd_transactions",
            "expected": [{
                "identity": "0900000087022",
                "start": "2025-08-31",
                "end": "2026-08-30",
            }],
            "windows": [{
                "identity": "0900000087022",
                "start": "2025-08-31",
                "end": "2026-08-30",
                "status": status,
                "pages": 1,
            }],
        }],
    }


def test_esun_result_requires_exact_account_range_and_pagination_binding():
    receipt = EsunCrawler._validated_twd_history_result(
        _bound_result(), identity="0900000087022",
        start=date(2025, 8, 31), end=date(2026, 8, 30),
    )
    assert receipt == {
        "identity": "0900000087022",
        "start": "2025-08-31",
        "end": "2026-08-30",
        "status": "complete",
        "pages": 1,
    }

    for mutation in (
        "account", "start", "end", "url", "pager", "total_count",
        "pager_types", "grid_count", "empty_marker", "busy",
    ):
        bad = _bound_result()
        if mutation == "account":
            bad["selected_identity"] = "0900000087023"
        elif mutation in {"start", "end"}:
            bad["clicked_period"][mutation] = "1999/01/01"
        elif mutation == "url":
            bad["url"] = "https://attacker.example/FAO01002.faces"
        elif mutation == "pager":
            bad["snapshot"]["pager"] = {"present": True, "actionableNext": 0}
        elif mutation == "pager_types":
            bad["snapshot"]["pager"] = {"present": 0, "actionableNext": False}
        elif mutation == "grid_count":
            bad["snapshot"]["gridCandidateCount"] = 2
        elif mutation == "empty_marker":
            bad["snapshot"]["emptyMarker"] = "查無交易資料"
        elif mutation == "busy":
            bad["snapshot"]["busy"] = True
        else:
            bad["snapshot"]["totalCount"] = 2
        with pytest.raises(RuntimeError, match="esun-twd-history-result"):
            EsunCrawler._validated_twd_history_result(
                bad, identity="0900000087022",
                start=date(2025, 8, 31), end=date(2026, 8, 30),
            )

    next_page = _bound_result()
    next_page["text"] += " 下一頁"
    contained_date = _bound_result()
    contained_date["snapshot"]["gridRows"][0][0] = "*12026/08/20"
    for bad in (next_page, contained_date):
        with pytest.raises(RuntimeError, match="esun-twd-history-result"):
            EsunCrawler._validated_twd_history_result(
                bad, identity="0900000087022",
                start=date(2025, 8, 31), end=date(2026, 8, 30),
            )


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("marker", [
    "系統錯誤，請稍後再試",
    "連線逾時",
    "連線已逾時",
    "操作時間已逾時",
    "查詢錯誤",
    "請重新登入",
    "登入狀態已失效",
    "資料載入中",
    "資料讀取中",
    "等待中",
    "請等待",
    "請耐心等候",
    "請耐心等待",
    "系統忙碌中",
    "Waiting",
    "Processing...",
    "Querying",
    "Please stand by",
    "Please be patient",
    "Session expired",
    "Session has expired",
])
def test_esun_result_rejects_failure_markers_before_stale_data(empty, marker):
    result = _bound_result(grid=not empty, empty=empty)
    result["text"] += f"\n{marker}"
    with pytest.raises(RuntimeError, match="esun-twd-history-result"):
        EsunCrawler._validated_twd_history_result(
            result,
            identity="0900000087022",
            start=date(2025, 8, 31),
            end=date(2026, 8, 30),
        )


def test_esun_result_scans_failure_markers_beyond_old_text_limit():
    result = _bound_result()
    result["text"] += "x" * 50_000 + "系統錯誤，請稍後再試"
    with pytest.raises(RuntimeError, match="esun-twd-history-result"):
        EsunCrawler._validated_twd_history_result(
            result,
            identity="0900000087022",
            start=date(2025, 8, 31),
            end=date(2026, 8, 30),
        )


def test_esun_empty_requires_explicit_bound_marker():
    receipt = EsunCrawler._validated_twd_history_result(
        _bound_result(grid=False, empty=True), identity="0900000087022",
        start=date(2025, 8, 31), end=date(2026, 8, 30),
    )
    assert receipt["status"] == "explicit_empty"

    ambiguous = _bound_result(grid=False, empty=False)
    with pytest.raises(RuntimeError, match="esun-twd-history-result"):
        EsunCrawler._validated_twd_history_result(
            ambiguous, identity="0900000087022",
            start=date(2025, 8, 31), end=date(2026, 8, 30),
        )

    instructional = _bound_result(grid=False, empty=True)
    instructional["text"] = (
        "操作說明：查無資料時請重新查詢 0900000087022 2025/08/31 2026/08/30"
    )
    instructional["snapshot"]["emptyMarker"] = None
    with pytest.raises(RuntimeError, match="esun-twd-history-result"):
        EsunCrawler._validated_twd_history_result(
            instructional, identity="0900000087022",
            start=date(2025, 8, 31), end=date(2026, 8, 30),
        )


def test_esun_parser_uses_only_one_authoritative_surface():
    raw = "2026/08/20\n12:00:00 利息 2 84 活存利息\n"
    rows = _parse_esun_twd_txn_results([{
        "account_no": "0900000087022",
        "snapshot": {
            "hasGrid": True,
            "gridText": raw,
            "gridRows": [["2026/08/20", "12:00:00", "利息", "", "2", "84", "活存利息"]],
            "tables": [{"text": raw}],
            "qryResult": [{"text": raw}],
        },
    }])
    assert len(rows) == 1


def test_esun_direction_comes_from_dom_columns_not_description():
    rows = _parse_esun_twd_txn_results([{
        "account_no": "0900000087022",
        "snapshot": {
            "hasGrid": True,
            "gridText": "2026/08/20 跨行轉帳 1 84\n2026/08/21 跨行轉帳 1 85",
            "gridRows": [
                ["2026/08/20", "12:00:00", "跨行轉帳", "1", "", "84"],
                ["2026/08/21", "12:00:00", "跨行轉帳", "", "1", "85"],
            ],
        },
    }])
    assert (rows[0]["expend"], rows[0]["income"]) == (1, None)
    assert (rows[1]["expend"], rows[1]["income"]) == (None, 1)


def test_esun_money_rejects_bad_grouping_and_ambiguous_columns():
    assert _esun_twd_integer("-2,147,483,648", non_negative=False) == -2_147_483_648
    for value in ("1,2,3", "01", 1.0):
        with pytest.raises(ValueError):
            _esun_twd_integer(value, non_negative=False)
    with pytest.raises(ValueError, match="money columns"):
        _parse_esun_twd_txn_results([{
            "account_no": "0900000087022",
            "snapshot": {
                "hasGrid": True,
                "gridText": "2026/08/20\n12:00:00 轉帳 1 2 84\n",
                "gridRows": [["2026/08/20", "12:00:00", "轉帳", "1", "2", "84"]],
            },
        }])
    with pytest.raises(ValueError, match="account"):
        _parse_esun_twd_txn_results([{
            "selected_text": "10900000087022 臺幣綜存",
            "snapshot": {"gridText": "2026/08/20\n12:00:00 轉帳 1 84\n"},
        }])
    with pytest.raises(ValueError, match="date columns"):
        _parse_esun_twd_txn_results([{
            "account_no": "0900000087022",
            "snapshot": {
                "hasGrid": True,
                "gridText": "12026/08/20 2026/08/20 轉帳 1 84\n",
                "gridRows": [["12026/08/20", "2026/08/20", "轉帳", "1", "", "84"]],
            },
        }])


@pytest.mark.parametrize("snapshot", [
    {},
    {"hasGrid": False, "gridText": None, "gridRows": []},
    {"hasGrid": True, "gridText": "row"},
    {"hasGrid": True, "gridText": "row", "gridRows": []},
    {"hasGrid": False, "gridText": "", "gridRows": [["unexpected"]]},
])
def test_esun_parser_rejects_inconsistent_structured_grid(snapshot):
    with pytest.raises(ValueError):
        _parse_esun_twd_txn_results([{
            "account_no": "0900000087022",
            "snapshot": snapshot,
        }])


def test_esun_parser_accepts_scrubbed_empty_grid():
    assert _parse_esun_twd_txn_results([{
        "account_no": "0900000087022",
        "snapshot": {"hasGrid": False, "gridRows": []},
    }]) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_no", " 0900000087022"),
        ("datetime", "2026-02-30 01:02:03"),
        ("datetime", "2026-8-20"),
        ("expend", -1),
        ("expend", 2_147_483_648),
        ("expend", 9_007_199_254_740_993),
        ("income", 1),
        ("balance", float("inf")),
    ],
)
def test_esun_persistence_row_rejects_malformed_values(field, value):
    row = {
        "account_no": "0900000087022",
        "datetime": "2026-08-20 01:02:03",
        "account_date": "2026-08-20",
        "desc": "轉帳",
        "expend": 1,
        "income": None,
        "balance": 84,
        "counterparty_bank": None,
        "counterparty_acct": None,
        "memo": None,
    }
    row[field] = value
    with pytest.raises(ValueError):
        _validated_esun_twd_row(row)


def test_esun_attested_result_persists_and_advances_account_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    result = _bound_result()
    result["snapshot"]["totalCount"] = None
    coverage = _bound_coverage()
    store = BankStore("esun", user_id=7, source_account_id=91)
    try:
        delta = persist_collected(
            "esun",
            {
                "accounts": [{
                    "account_no": "0900000087022",
                    "category": "臺幣綜存",
                    "currency": "TWD",
                    "balance": 84,
                }],
                "twd_txn_results": [result],
                "history_coverage": coverage,
            },
            store,
        )
        assert delta["twd_txn_new"] == 1
        assert store.latest_twd_transaction_dates() == {
            "0900000087022": date(2026, 8, 30),
        }
    finally:
        store.close()


def test_esun_history_rows_and_cursor_rollback_together(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    result = _bound_result()
    result["snapshot"]["totalCount"] = None
    payload = {
        "accounts": [{
            "account_no": "0900000087022",
            "category": "臺幣綜存",
            "currency": "TWD",
            "balance": 84,
        }],
        "twd_txn_results": [result],
        "history_coverage": _bound_coverage(),
    }
    store = BankStore("esun", user_id=7, source_account_id=91)
    monkeypatch.setattr(
        store,
        "record_history_coverage_cursors",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cursor failed")),
    )
    try:
        with pytest.raises(RuntimeError, match="cursor failed"):
            persist_collected("esun", payload, store)
        assert store.conn.execute("SELECT COUNT(*) FROM twd_transactions").fetchone()[0] == 0
        assert store.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
        assert store.latest_twd_transaction_dates() == {}
    finally:
        store.close()


@pytest.mark.parametrize("results", [
    None,
    [],
    [_bound_result(grid=False, empty=True)],
    [{
        "account_no": "0900000087022",
        "snapshot": {
            "hasGrid": False,
            "gridText": "",
            "gridRows": [],
            "gridRowCount": 1,
            "totalCount": 1,
            "emptyMarker": None,
        },
    }],
    [{
        "account_no": "0900000087023",
        "snapshot": {
            "hasGrid": True,
            "gridText": "2026/08/20 transfer 1 84",
            "gridRows": [["2026/08/20", "12:00:00", "transfer", "1", "", "84"]],
            "gridRowCount": 1,
            "totalCount": 1,
        },
    }],
])
def test_esun_persistence_binds_results_to_coverage_before_cursor(tmp_path, monkeypatch, results):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("esun", user_id=7, source_account_id=92)
    coverage = _bound_coverage()
    try:
        with pytest.raises(ValueError):
            persist_collected(
                "esun",
                {"twd_txn_results": results, "history_coverage": coverage},
                store,
            )
        assert store.latest_twd_transaction_dates() == {}
    finally:
        store.close()


def test_esun_persistence_rechecks_pagination_before_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    for pager in (
        {"present": True, "actionableNext": 1},
        {"present": 0, "actionableNext": False},
    ):
        result = _bound_result()
        result["snapshot"]["pager"] = pager
        store = BankStore("esun", user_id=7, source_account_id=96)
        try:
            with pytest.raises(ValueError, match="pagination"):
                persist_collected(
                    "esun",
                    {"twd_txn_results": [result], "history_coverage": _bound_coverage()},
                    store,
                )
            assert all(count == 0 for count in store.stats().values())
            assert store.latest_twd_transaction_dates() == {}
        finally:
            store.close()


def test_esun_persistence_rejects_rows_outside_attested_window(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    for row in (
        ["2024/01/01", "12:00:00", "利息", "", "2", "84"],
        ["2026/08/20", "2024/01/01", "利息", "", "2", "84"],
    ):
        result = _bound_result()
        result["snapshot"]["gridRows"] = [row]
        store = BankStore("esun", user_id=7, source_account_id=93)
        try:
            with pytest.raises(ValueError):
                persist_collected(
                    "esun",
                    {"twd_txn_results": [result], "history_coverage": _bound_coverage()},
                    store,
                )
            assert all(count == 0 for count in store.stats().values())
        finally:
            store.close()


def test_esun_persistence_rejects_boolean_result_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    cases = [
        (_bound_result(), _bound_coverage()),
        (_bound_result(grid=False, empty=True), _bound_coverage(status="explicit_empty")),
    ]
    cases[0][0]["snapshot"]["totalCount"] = True
    cases[1][0]["snapshot"]["gridRowCount"] = False
    cases[1][0]["snapshot"]["totalCount"] = False
    for result, coverage in cases:
        store = BankStore("esun", user_id=7, source_account_id=94)
        try:
            with pytest.raises(ValueError):
                persist_collected(
                    "esun",
                    {"twd_txn_results": [result], "history_coverage": coverage},
                    store,
                )
            assert all(count == 0 for count in store.stats().values())
        finally:
            store.close()


def test_persist_collected_requires_esun_coverage_before_any_write(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("esun", user_id=7, source_account_id=97)
    try:
        with pytest.raises(ValueError, match="requires history coverage"):
            persist_collected(
                "esun",
                {
                    "accounts": [{
                        "account_no": "0900000087022",
                        "currency": "TWD",
                        "balance": 84,
                    }],
                    "twd_txn_results": [_bound_result()],
                },
                store,
            )
        assert all(count == 0 for count in store.stats().values())
        assert store.latest_twd_transaction_dates() == {}
    finally:
        store.close()


def test_persist_collected_validates_coverage_before_any_write(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    invalid_mode = _bound_coverage()
    invalid_mode["mode"] = "bogus"
    malformed_empty = {
        "mode": "full",
        "domains": [{
            "domain": "twd_transactions",
            "expected": [],
            "windows": [],
            "empty_window": {
                "start": "2025-08-31",
                "end": "2026-08-30",
                "status": "explicit_empty",
                "pages": False,
            },
        }],
    }
    for coverage in (invalid_mode, malformed_empty):
        store = BankStore("esun", user_id=7, source_account_id=95)
        try:
            with pytest.raises(ValueError):
                persist_collected(
                    "esun",
                    {
                        "accounts": [{
                            "account_no": "0900000087022",
                            "currency": "TWD",
                            "balance": 84,
                        }],
                        "twd_txn_results": [_bound_result()],
                        "history_coverage": coverage,
                    },
                    store,
                )
            assert all(count == 0 for count in store.stats().values())
            assert store.latest_twd_transaction_dates() == {}
        finally:
            store.close()


def test_esun_collect_wires_authoritative_twd_flow_to_coverage(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    collector = ResponseCollector()
    account = "0900000087022"
    frame_url = "https://ebank.esunbank.com.tw/fco/fco08001/FCO08001_Home.faces"
    request_listeners = []
    response_listeners = []
    state = {"dom_ready": False}

    class Request:
        method = "POST"
        frame: object | None = None
        url = "https://ebank.esunbank.com.tw/fao/fao01002/FAO01002_Home.faces?ajax=true"
        post_data = (
            "fao01002%3Adract=opaque-a&"
            "fao01002%3AstartDate=2025%2F08%2F31&"
            "fao01002%3AendDate=2026%2F08%2F30&"
            "fao01002%3AlinkCommand=query&"
            "javax.faces.ViewState=state"
        )

    class Response:
        url = "https://ebank.esunbank.com.tw/fao/fao01002/FAO01002_Home.faces?ajax=true"
        status = 200
        headers = {
            "content-type": "text/html;charset=UTF-8",
            "content-length": str(len(_live_ajax_html())),
        }

        def __init__(self, request):
            self.request = request

        @staticmethod
        def text():
            return _live_ajax_html()

    class Locator:
        def select_option(self, **kwargs):
            assert kwargs == {"index": 1, "timeout": 8000}

    class Frame:
        name = "history"
        url = frame_url

        def locator(self, selector):
            assert selector == "select[id='fao01002:dract']"
            return Locator()

        def evaluate(self, script, arg=None):
            if "formCount" in script and "sameForm" in script:
                return _form_contract()
            if "[...s.options]" in script:
                return [{"index": 1, "text": f"臺幣綜存 {account}", "value": "opaque-a"}]
            if "s.selectedIndex" in script:
                return {"index": 1, "text": f"臺幣綜存 {account}", "value": "opaque-a"}
            if 'match(/"today"' in script:
                return "2026/08/30"
            if "period.start" in script:
                assert isinstance(arg, dict)
                return {"ok": True, "checked": True, **arg}
            if "j_id_sort1" in script:
                return True
            if "return {ok: true, marked" in script:
                return {"ok": True, "marked": 0}
            if "visible-query" in script:
                request = Request()
                request.frame = self
                for listener in request_listeners:
                    listener(request)
                for listener in response_listeners:
                    listener(Response(request))
                return {"clicked": "visible-query", "tag": "BUTTON", "id": "q", "name": "", "text": "查詢"}
            if "const bodyText" in script:
                if not state["dom_ready"]:
                    return {"bound": False, "scopeCount": 0}
                text = "交易 共 1 筆\n2026/08/20\n12:00:00 利息 2 84 活存利息"
                return {
                    "bound": True,
                    "href": frame_url,
                    "bodyText": text,
                    "busy": False,
                    "evidenceFresh": True,
                    "resultFingerprint": "fresh-result",
                    "gridText": "2026/08/20\n12:00:00 利息 2 84 活存利息\n",
                    "hasGrid": True,
                    "gridCandidateCount": 1,
                    "gridRowCount": 1,
                    "gridRows": [
                        ["2026/08/20", "12:00:00", "利息", "", "2", "84", "活存利息"],
                    ],
                    "totalCount": 1,
                    "pager": {"present": False, "actionableNext": 0},
                    "emptyMarker": None,
                    "gridHtml": "",
                    "qryResult": [],
                    "tables": [],
                }
            if "body.innerText.slice" in script or "body.textContent.slice" in script:
                return f"臺幣帳戶總覽\n臺幣綜存\n{account}\n84\n存款交易明細查詢"
            if script.startswith("() => ({"):
                return {}
            return ""

    class MainFrame:
        name = "main"
        url = "https://ebank.esunbank.com.tw/"

        def evaluate(self, script, arg=None):
            if "body.innerText" in script:
                return ""
            if "querySelectorAll('iframe').length" in script:
                return 1
            return ""

    class SiblingFrame:
        name = "stale-sibling"
        url = Frame.url

        def __init__(self):
            self.inner = Frame()

        def evaluate(self, script, arg=None):
            if "formCount" in script and "sameForm" in script:
                return {}
            return self.inner.evaluate(script, arg)

    class Context:
        pages = []

    class Page:
        url = "https://ebank.esunbank.com.tw/"

        def __init__(self):
            self.main_frame = MainFrame()
            self.frames = [self.main_frame, Frame(), SiblingFrame()]
            self.context = Context()
            self.context.pages = [self]
            self.waits = []

        def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)
            if milliseconds == 9000:
                state["dom_ready"] = True

        def on(self, event, listener):
            {"request": request_listeners, "response": response_listeners}[event].append(listener)

        def remove_listener(self, event, listener):
            {"request": request_listeners, "response": response_listeners}[event].remove(listener)

        def evaluate(self, script, arg=None):
            return self.main_frame.evaluate(script, arg)

    crawler = object.__new__(EsunCrawler)
    crawler.transaction_cursors = {}

    def navigate(_page, label, _debug_dir, _screenshot_name=None):
        return {"frames": [{"result": {"clicked": "actionable" if label == "存款交易明細查詢" else None}}]}

    monkeypatch.setattr(crawler, "_navigate_menu", navigate)
    monkeypatch.setattr(crawler, "_navigate_credit_card_bill", lambda *_args: {"frames": []})
    page = Page()
    result = crawler.collect(page, collector).to_dict()
    assert 9000 not in page.waits
    assert result["history_coverage"]["domains"][0]["windows"] == [{
        "identity": account,
        "start": "2025-08-31",
        "end": "2026-08-30",
        "status": "complete",
        "pages": 1,
    }]
    assert "gridText" not in result["twd_txn_results"][0]["snapshot"]
    assert not ({
        "final_url", "main_text", "frames", "card_all_frames_meta",
        "twd_txn_nav_probe", "card_nav_probe", "card_frames",
        "card_bill_details", "card_statement_transactions", "card_txn_form_submitted",
        "card_txn_nav_probe", "card_txn_frames",
        "card_quota_nav_probe", "card_quota_frames",
        "card_pay_nav_probe", "card_pay_frames",
    } & result.keys())
