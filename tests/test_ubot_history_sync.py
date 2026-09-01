from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.banks.ubot import UbotCrawler
from backend.core.base import ApiHit, ResponseCollector, validate_history_coverage
from backend.core.persist import persist_collected
from backend.core.persist import ubot as ubot_persist_module
from backend.core.persist.ubot import persist_ubot
from backend.core.store import BankStore


ACCOUNT = "012345678901"


@pytest.fixture(autouse=True)
def _freeze_persistence_today(monkeypatch):
    if hasattr(ubot_persist_module, "_today"):
        monkeypatch.setattr(ubot_persist_module, "_today", lambda: date(2026, 9, 1))


def _launch_browser(playwright):
    if not Path(playwright.chromium.executable_path).exists():
        pytest.skip("Patchright browser binary is not installed")
    return playwright.chromium.launch(headless=True)


def _crawler(cursor: date | None = None) -> UbotCrawler:
    crawler = object.__new__(UbotCrawler)
    crawler.transaction_cursors = {
        "twd_transactions": ({ACCOUNT: cursor} if cursor else {}),
    }
    return crawler


def test_ubot_opts_into_twd_history_only() -> None:
    assert UbotCrawler.HISTORY_COVERAGE_REQUIRED is True
    assert frozenset({"twd_transactions"}) == UbotCrawler.HISTORY_COVERAGE_DOMAINS


def test_ubot_full_windows_cover_only_native_rolling_months() -> None:
    crawler = _crawler()

    assert crawler._history_range(ACCOUNT, end=date(2026, 9, 1), mode="full") == (
        date(2026, 7, 1), date(2026, 9, 1),
    )
    assert crawler._history_windows(date(2026, 7, 1), date(2026, 9, 1)) == [
        (date(2026, 7, 1), date(2026, 7, 31)),
        (date(2026, 8, 1), date(2026, 8, 31)),
        (date(2026, 9, 1), date(2026, 9, 1)),
    ]


def test_ubot_incremental_floors_overlap_to_month_and_rejects_future_cursor() -> None:
    assert _crawler()._history_range(
        ACCOUNT, end=date(2026, 9, 1), mode="incremental",
    ) == (date(2026, 7, 1), date(2026, 9, 1))

    crawler = _crawler(date(2026, 8, 20))
    assert crawler._history_range(ACCOUNT, end=date(2026, 9, 1), mode="incremental") == (
        date(2026, 8, 1), date(2026, 9, 1),
    )

    crawler = _crawler(date(2026, 9, 2))
    with pytest.raises(RuntimeError, match="ubot-twd-history-cursor"):
        crawler._history_range(ACCOUNT, end=date(2026, 9, 1), mode="incremental")


def _form_snapshot() -> dict:
    return {
        "selects": [
            {"enabled": True, "options": [
                {"text": "請選擇帳號", "value": ""},
                {"text": "活期存款 012-34-5678901", "value": "opaque-token"},
            ]},
            {"enabled": True, "options": [
                {"text": label, "value": str(index)}
                for index, label in enumerate(
                    ["當日", "最近一週", "最近一月", "9月份", "8月份", "7月份", "自選日期"]
                )
            ]},
        ],
        "search_buttons": 1,
    }


def _row() -> dict:
    return {
        "AccountDate": "2026/07/31",
        "Balance": "10,030",
        "Expenditure": "",
        "Income": "30",
        "PS": "",
        "Summary": "利息存入",
        "TraDate": "2026/07/31",
        "TraSum": "測試備註",
        "TraTime": "12:34:56",
    }


def _history_hit(*, code: str = "0000", rows=None) -> ApiHit:
    rows = [_row()] if rows is None else rows
    body = (
        {"Account": ACCOUNT, "NTDetailList": rows, "NTTotal": {}}
        if code == "0000" else {}
    )
    return ApiHit(
        url="https://www.ubot.com.tw/MyBank/IBKB010102",
        raw_url="https://www.ubot.com.tw/MyBank/IBKB010102",
        method="POST",
        status=200,
        req_body=(
            f"acctNo={ACCOUNT}&beginDate=20260701&endDate=20260731&"
            "sessionId=opaque-session&sid=opaque-sid"
        ),
        resp_json={
            "RespCode": {
                "RtnCode": code,
                "RtnDesc": "" if code == "0000" else "查無資料",
                "SvcName": "IBKB010102",
                "Time": "20260901123456",
            },
            "RespBody": body,
        },
        content_type="application/json;charset=utf-8",
        body_size=800,
        request_sequence=2,
        main_frame_request=True,
    )


def test_ubot_inventory_uses_exact_form_and_canonical_label_identity() -> None:
    assert UbotCrawler._validate_twd_form(
        _form_snapshot(), as_of=date(2026, 9, 1),
    ) == [{
        "label": "活期存款 012-34-5678901",
        "identity": ACCOUNT,
        "currency": "TWD",
        "index": 1,
    }]


@pytest.mark.parametrize("mutation", [
    "select_count", "disabled", "period_missing", "period_extra", "search", "placeholder",
    "account_missing", "account_duplicate", "identity_duplicate",
])
def test_ubot_inventory_fails_closed(mutation: str) -> None:
    snapshot = _form_snapshot()
    if mutation == "select_count":
        snapshot["selects"].append({"enabled": True, "options": []})
    elif mutation == "disabled":
        snapshot["selects"][0]["enabled"] = False
    elif mutation == "period_missing":
        snapshot["selects"][1]["options"].pop()
    elif mutation == "period_extra":
        snapshot["selects"][1]["options"].append({"text": "6月份", "value": "7"})
    elif mutation == "search":
        snapshot["search_buttons"] = 2
    elif mutation == "placeholder":
        snapshot["selects"][0]["options"][0]["text"] = "請選擇帳戶"
    elif mutation == "account_missing":
        snapshot["selects"][0]["options"][1]["text"] = "活期存款"
    elif mutation == "account_duplicate":
        snapshot["selects"][0]["options"][1]["text"] += " 999-99-9999999"
    else:
        snapshot["selects"][0]["options"].append({
            "text": "另一帳戶 012-34-5678901", "value": "other-token",
        })

    with pytest.raises(RuntimeError, match="ubot-twd-history-form"):
        UbotCrawler._validate_twd_form(snapshot, as_of=date(2026, 9, 1))


@pytest.mark.parametrize("mutation", [
    "host", "path", "query", "fragment", "method", "status", "mime", "redirect",
    "frame", "sequence", "body_size", "body_size_zero", "form_keys", "account", "range", "session",
    "response_keys", "code_keys", "body_keys", "body_account", "row_keys", "tra_date",
    "account_date", "time", "money", "money_decimal", "money_overflow", "both_amounts",
    "service", "empty_error", "empty_expired", "nttotal_page", "nttotal_upper",
])
def test_ubot_history_response_fails_closed(mutation: str) -> None:
    hit = _history_hit()
    if mutation == "host":
        hit.url = hit.raw_url = "https://www.ubot.com.tw.evil.example/MyBank/IBKB010102"
    elif mutation == "path":
        hit.url = hit.raw_url = "https://www.ubot.com.tw/evil/IBKB010102"
    elif mutation == "query":
        hit.raw_url += "?extra=1"
    elif mutation == "fragment":
        hit.raw_url += "#fragment"
    elif mutation == "method":
        hit.method = "GET"
    elif mutation == "status":
        hit.status = 500
    elif mutation == "mime":
        hit.content_type = "text/html"
    elif mutation == "redirect":
        hit.redirected = True
    elif mutation == "frame":
        hit.main_frame_request = False
    elif mutation == "sequence":
        hit.request_sequence = 1
    elif mutation == "body_size":
        hit.body_size = 5_000_001
    elif mutation == "body_size_zero":
        hit.body_size = 0
    elif mutation == "form_keys":
        hit.req_body += "&extra=1"
    elif mutation == "account":
        hit.req_body = hit.req_body.replace(ACCOUNT, "999999999999")
    elif mutation == "range":
        hit.req_body = hit.req_body.replace("20260701", "20260601")
    elif mutation == "session":
        hit.req_body = hit.req_body.replace("opaque-session", "")
    elif mutation == "response_keys":
        hit.resp_json["extra"] = {}
    elif mutation == "code_keys":
        hit.resp_json["RespCode"]["extra"] = "bad"
    elif mutation == "body_keys":
        hit.resp_json["RespBody"]["extra"] = "bad"
    elif mutation == "body_account":
        hit.resp_json["RespBody"]["Account"] = "999999999999"
    elif mutation == "row_keys":
        hit.resp_json["RespBody"]["NTDetailList"][0].pop("PS")
    elif mutation == "tra_date":
        hit.resp_json["RespBody"]["NTDetailList"][0]["TraDate"] = "2026/08/01"
    elif mutation == "account_date":
        hit.resp_json["RespBody"]["NTDetailList"][0]["AccountDate"] = "2026/02/30"
    elif mutation == "time":
        hit.resp_json["RespBody"]["NTDetailList"][0]["TraTime"] = "1:02"
    elif mutation == "money":
        hit.resp_json["RespBody"]["NTDetailList"][0]["Income"] = "NaN"
    elif mutation == "money_decimal":
        hit.resp_json["RespBody"]["NTDetailList"][0]["Income"] = "1.5"
    elif mutation == "both_amounts":
        hit.resp_json["RespBody"]["NTDetailList"][0]["Expenditure"] = "30"
    elif mutation == "service":
        hit.resp_json["RespCode"]["SvcName"] = "LOGIN"
    elif mutation in {"empty_error", "empty_expired"}:
        hit = _history_hit(code="UB112", rows=[])
        hit.resp_json["RespCode"]["RtnDesc"] = (
            "請重新登入" if mutation == "empty_error" else "Session expired"
        )
    elif mutation in {"nttotal_page", "nttotal_upper"}:
        hit.resp_json["RespBody"]["NTTotal"] = {
            "totalPage": 2,
        } if mutation == "nttotal_page" else {"hasNextPage": "TRUE"}
    else:
        hit.resp_json["RespBody"]["NTDetailList"][0]["Balance"] = "2147483648"

    with pytest.raises(RuntimeError, match="ubot-twd-history-response"):
        UbotCrawler._validate_history_hit(
            hit, identity=ACCOUNT, start=date(2026, 7, 1), end=date(2026, 7, 31),
            after_sequence=1,
        )


@pytest.mark.parametrize("total", [
    {"totalPage": 2}, {"totalPages": "2"}, {"pageCount": 2}, {"pageTotal": 2},
    {"lastPage": 2}, {"maxPage": 2}, {"hasNextPage": " TRUE "},
    {"hasMore": 2}, {"nextPageToken": "opaque"}, {"totalPage": -1},
    {"totalPages": 0.5}, {"pageIndex": 0.5}, {"currentPage": -1},
])
def test_ubot_nttotal_rejects_any_positive_or_unknown_paging_claim(total: dict) -> None:
    assert UbotCrawler._nttotal_claims_more_pages(total) is True


@pytest.mark.parametrize("total", [
    {}, {"totalPage": 1}, {"hasNextPage": " FALSE "}, {"hasMore": 0},
    {"pageIndex": 0}, {"currentPage": 1}, {"pageSize": 20}, {"rowsPerPage": 20},
])
def test_ubot_nttotal_allows_only_explicit_single_page_metadata(total: dict) -> None:
    assert UbotCrawler._nttotal_claims_more_pages(total) is False


def test_ubot_accepts_complete_and_exact_ub112_empty() -> None:
    complete = UbotCrawler._validate_history_hit(
        _history_hit(), identity=ACCOUNT, start=date(2026, 7, 1),
        end=date(2026, 7, 31), after_sequence=1,
    )
    empty = UbotCrawler._validate_history_hit(
        _history_hit(code="UB112", rows=[]), identity=ACCOUNT,
        start=date(2026, 7, 1), end=date(2026, 7, 31), after_sequence=1,
    )

    assert complete == {"records": [_row()], "status": "complete", "rows": 1}
    assert empty == {"records": [], "status": "explicit_empty", "rows": 0}


def test_ubot_allows_valid_account_date_after_transaction_window(tmp_path, monkeypatch) -> None:
    hit = _history_hit()
    hit.resp_json["RespBody"]["NTDetailList"][0]["AccountDate"] = "2026/08/01"
    assert UbotCrawler._validate_history_hit(
        hit, identity=ACCOUNT, start=date(2026, 7, 1),
        end=date(2026, 7, 31), after_sequence=1,
    )["rows"] == 1

    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(ubot_persist_module, "_today", lambda: date(2026, 9, 1), raising=False)
    store = BankStore("ubot", user_id=1, source_account_id=7)
    payload = _persist_payload()
    payload["twd_txns"][0]["NTDetailList"][0]["AccountDate"] = "2026/08/01"
    try:
        assert persist_collected("ubot", payload, store)["twd_txn_new"] == 1
    finally:
        store.close()


def test_ubot_rejects_success_code_without_rows() -> None:
    with pytest.raises(RuntimeError, match="ubot-twd-history-response"):
        UbotCrawler._validate_history_hit(
            _history_hit(rows=[]), identity=ACCOUNT, start=date(2026, 7, 1),
            end=date(2026, 7, 31), after_sequence=1,
        )


def test_ubot_rejects_other_error_code() -> None:
    with pytest.raises(RuntimeError, match="ubot-twd-history-response"):
        UbotCrawler._validate_history_hit(
            _history_hit(code="EH86", rows=[]), identity=ACCOUNT,
            start=date(2026, 7, 1), end=date(2026, 7, 31), after_sequence=1,
        )


@pytest.mark.parametrize("mutation", ["tables", "rows", "pager", "busy", "dialog", "stale"])
def test_ubot_result_dom_fails_closed(mutation: str) -> None:
    state = {
        "visible_tables": 2, "visible_rows": 2, "pagers": 0, "busy": 0,
        "dialogs": 0, "stale_tables": 0, "quiet_ms": 2000,
    }
    state[{"tables": "visible_tables", "rows": "visible_rows", "pager": "pagers",
           "busy": "busy", "dialog": "dialogs", "stale": "stale_tables"}[mutation]] += 1
    with pytest.raises(RuntimeError, match="ubot-twd-history-result"):
        UbotCrawler._validate_twd_dom(state, status="complete", rows=1)


def test_ubot_exact_empty_requires_clean_zero_result_dom() -> None:
    UbotCrawler._validate_twd_dom(
        {"visible_tables": 0, "visible_rows": 0, "pagers": 0, "busy": 0,
         "dialogs": 0, "stale_tables": 0, "quiet_ms": 2000},
        status="explicit_empty", rows=0,
    )


def test_ubot_dom_tests_skip_when_patchright_browser_is_not_installed() -> None:
    fake = SimpleNamespace(
        chromium=SimpleNamespace(
            executable_path="/definitely/missing/chromium",
            launch=lambda **_kwargs: pytest.fail("missing browser must not launch"),
        ),
    )
    with pytest.raises(pytest.skip.Exception):
        _launch_browser(fake)


def test_ubot_dom_snapshots_reject_aria_hidden_ancestor_content() -> None:
    from patchright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page()
        page.set_content("""
          <div aria-hidden="true">
            <select><option>請選擇帳號</option></select>
            <button>搜尋</button>
            <table><tbody><tr><td>stale</td></tr></tbody></table>
            <button aria-label="next">下一頁</button>
            <div role="progressbar"></div>
            <div role="dialog">stale</div>
          </div>
        """)
        try:
            assert UbotCrawler._twd_form_snapshot(page) == {
                "selects": [], "search_buttons": 0,
            }
            assert UbotCrawler._twd_dom_snapshot(page) == {
                "visible_tables": 0, "visible_rows": 0,
                "pagers": 0, "busy": 0, "dialogs": 0,
                "stale_tables": 0, "quiet_ms": 0,
            }
        finally:
            browser.close()


@pytest.mark.parametrize("pager_html", [
    '<nav class="pagination"><span>1</span><span>2</span></nav>',
    '<input type="hidden" name="page" value="2">',
])
def test_ubot_dom_snapshot_detects_structural_pagination(pager_html: str) -> None:
    from patchright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page()
        page.set_content(f'<main><table><tbody><tr><td>row</td></tr></tbody></table>{pager_html}</main>')
        try:
            assert UbotCrawler._twd_dom_snapshot(page)["pagers"] > 0
        finally:
            browser.close()


@pytest.mark.parametrize("pager_html", [
    '<nav class="pagination"><span>1</span></nav>',
    '<input type="hidden" name="page" value="1">',
])
def test_ubot_dom_snapshot_accepts_single_page_metadata(pager_html: str) -> None:
    from patchright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page()
        page.set_content(f'<main><table><tbody><tr><td>row</td></tr></tbody></table>{pager_html}</main>')
        try:
            assert UbotCrawler._twd_dom_snapshot(page)["pagers"] == 0
        finally:
            browser.close()


def test_ubot_dom_snapshot_rejects_unchanged_pre_submit_tables() -> None:
    from patchright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page()
        page.set_content('<table><tbody><tr><td>old</td></tr></tbody></table>')
        try:
            UbotCrawler._mark_twd_dom_boundary(page)
            assert UbotCrawler._twd_dom_snapshot(page)["stale_tables"] == 1
            page.locator("td").evaluate("node => { node.textContent = 'fresh'; }")
            assert UbotCrawler._twd_dom_snapshot(page)["stale_tables"] == 0
        finally:
            browser.close()


def test_ubot_dom_settle_observes_delayed_pager() -> None:
    from patchright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page()
        page.set_content('<main><table><tbody><tr><td>row</td></tr></tbody></table></main>')
        try:
            UbotCrawler._mark_twd_dom_boundary(page)
            page.evaluate("""() => setTimeout(() => {
              document.querySelector('main').insertAdjacentHTML(
                'beforeend', '<nav class="pagination"><span>1</span><span>2</span></nav>');
            }, 500)""")
            assert UbotCrawler._wait_for_twd_dom_settle(page)["pagers"] > 0
        finally:
            browser.close()


def test_response_collector_rejects_lying_ubot_content_length_before_body_read() -> None:
    collector = ResponseCollector("ubot.com.tw")
    page = SimpleNamespace()
    frame = SimpleNamespace(page=page)
    page.main_frame = frame
    form = (
        f"acctNo={ACCOUNT}&beginDate=20260701&endDate=20260731&"
        "sessionId=" + "x" * 600 + "&sid=opaque-sid"
    )
    request = SimpleNamespace(
        url="https://www.ubot.com.tw/MyBank/IBKB010102",
        headers={}, method="POST", post_data=form, redirected_from=None, frame=frame,
    )

    collector._on_request(request)
    collector._on_response(SimpleNamespace(
        url=request.url, request=request, status=200,
        headers={
            "content-type": "application/json;charset=utf-8",
            "content-length": "1",
            "content-encoding": "identity",
        },
        body=lambda: pytest.fail("implausible declared size must reject before body read"),
        json=lambda: pytest.fail("UBOT bounded response must use raw body"),
    ))

    assert collector.hits[0].req_body == form
    assert collector.hits[0].body_size == 1
    assert collector.hits[0].resp_json is None


def test_response_collector_preserves_exact_bounded_ubot_json_bytes() -> None:
    collector = ResponseCollector("ubot.com.tw")
    page = SimpleNamespace()
    frame = SimpleNamespace(page=page)
    page.main_frame = frame
    request = SimpleNamespace(
        url="https://www.ubot.com.tw/MyBank/IBKB010102",
        headers={}, method="POST",
        post_data=(
            f"acctNo={ACCOUNT}&beginDate=20260701&endDate=20260731&"
            "sessionId=opaque-session&sid=opaque-sid"
        ),
        redirected_from=None, frame=frame,
    )
    raw = json.dumps(_history_hit().resp_json).encode()

    collector._on_request(request)
    collector._on_response(SimpleNamespace(
        url=request.url, request=request, status=200,
        headers={
            "content-type": "application/json;charset=utf-8",
            "content-length": str(len(raw)),
            "content-encoding": "identity",
        },
        body=lambda: raw,
        json=lambda: pytest.fail("UBOT bounded response must use raw body"),
    ))

    assert collector.hits[0].body_size == len(raw)
    assert collector.hits[0].resp_json == _history_hit().resp_json


def test_ubot_collection_reenters_form_for_every_native_month_and_publishes_coverage(
    monkeypatch,
) -> None:
    collector = ResponseCollector("ubot.com.tw")
    crawler = _crawler()
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    routes: list[str] = []
    selected = {"account": None, "period": None}
    last_status = "explicit_empty"

    class Select:
        def __init__(self, kind: str):
            self.kind = kind

        def select_option(self, *, index=None, label=None):
            selected[self.kind] = index if index is not None else label

    class Button:
        def is_visible(self):
            return True

        def is_enabled(self):
            return True

        def inner_text(self):
            return "搜尋"

        def click(self, **_kwargs):
            nonlocal last_status
            month = {"7月份": 7, "8月份": 8, "9月份": 9}[selected["period"]]
            start = date(2026, month, 1)
            end = date(2026, month, 31 if month in {7, 8} else 1)
            rows = [_row()] if month == 7 else []
            hit = _history_hit(code="0000" if rows else "UB112", rows=rows)
            hit.req_body = (
                f"acctNo={ACCOUNT}&beginDate={start:%Y%m%d}&endDate={end:%Y%m%d}&"
                "sessionId=opaque-session&sid=opaque-sid"
            )
            hit.request_sequence = collector.request_sequence + 1
            collector._request_sequence += 1
            collector._issued_endpoint_counts["IBKB010102"] = (
                collector.issued_count("IBKB010102") + 1
            )
            collector.hits.append(hit)
            last_status = "complete" if rows else "explicit_empty"

    class Locator:
        def count(self):
            return 1

        def nth(self, _index):
            return Button()

    class Page:
        def wait_for_timeout(self, _milliseconds):
            return None

        def query_selector_all(self, selector):
            assert selector == "select"
            return [Select("account"), Select("period")]

        def locator(self, selector):
            assert selector == "button"
            return Locator()

        def evaluate(self, script):
            if "location.hash" in script:
                routes.append("/B0101001")
                return None
            if "search_buttons" in script:
                return _form_snapshot()
            if "visible_tables" in script:
                return (
                    {"visible_tables": 2, "visible_rows": 2, "pagers": 0, "busy": 0,
                     "dialogs": 0, "stale_tables": 0, "quiet_ms": 2000}
                    if last_status == "complete" else
                    {"visible_tables": 0, "visible_rows": 0, "pagers": 0, "busy": 0,
                     "dialogs": 0, "stale_tables": 0, "quiet_ms": 2000}
                )
            if "__thothUbotHistoryBoundary" in script:
                return None
            raise AssertionError("unexpected evaluate")

    result = crawler._collect_twd_history(Page(), collector, as_of=date(2026, 9, 1))

    assert routes == ["/B0101001"] * 4
    assert [item["receipt"]["status"] for item in result["results"]] == [
        "complete", "explicit_empty", "explicit_empty",
    ]
    assert result["inventory"] == [{
        "label": "活期存款 012-34-5678901", "identity": ACCOUNT, "currency": "TWD",
    }]
    assert validate_history_coverage(
        result["coverage"], expected_mode="full",
        expected_domains=frozenset({"twd_transactions"}),
    )["windows"] == 3


def test_ubot_history_stops_on_opaque_dialog_latch(monkeypatch) -> None:
    crawler = _crawler()
    crawler._shared_dialog_blocked = True
    monkeypatch.setattr(crawler, "_goto", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="ubot-twd-history-dialog"):
        crawler._collect_twd_history(
            SimpleNamespace(), ResponseCollector("ubot.com.tw"), as_of=date(2026, 9, 1),
        )


def test_ubot_collect_publishes_history_result_without_changing_card_collection(monkeypatch) -> None:
    crawler = _crawler()
    history = {
        "results": [{
            "Account": ACCOUNT, "NTDetailList": [_row()], "NTTotal": {},
            "receipt": {
                "identity": ACCOUNT, "start": "2026-07-01", "end": "2026-07-31",
                "status": "complete", "pages": 1, "rows": 1,
            },
        }],
        "inventory": [{
            "label": "活期存款 012-34-5678901", "identity": ACCOUNT, "currency": "TWD",
        }],
        "coverage": {
            "mode": "full", "as_of": "2026-07-31",
            "domains": [{
                "domain": "twd_transactions",
                "expected": [{"identity": ACCOUNT, "start": "2026-07-01", "end": "2026-07-31"}],
                "windows": [{
                    "identity": ACCOUNT, "start": "2026-07-01", "end": "2026-07-31",
                    "status": "complete", "pages": 1,
                }],
            }],
        },
    }
    monkeypatch.setattr(crawler, "_goto", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(crawler, "_latest_body", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(crawler, "_collect_twd_history", lambda *_args, **_kwargs: history)
    monkeypatch.setattr(crawler, "_collect_card_billed", lambda *_args, **_kwargs: [])

    result = crawler.collect(SimpleNamespace(url="https://www.ubot.com.tw/ubot/"), ResponseCollector())

    assert result.twd_txns == history["results"]
    assert result.debit_accounts == history["inventory"]
    assert result.history_coverage == history["coverage"]


def _coverage(*, mode: str = "full", start: str = "2026-07-01") -> dict:
    crawler = _crawler()
    windows = [
        {
            "identity": ACCOUNT,
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
            "status": "complete" if window_start.month == 7 else "explicit_empty",
            "pages": 1,
        }
        for window_start, window_end in crawler._history_windows(
            date.fromisoformat(start), date(2026, 9, 1),
        )
    ]
    return {
        "mode": mode,
        "as_of": "2026-09-01",
        "domains": [{
            "domain": "twd_transactions",
            "expected": [{"identity": ACCOUNT, "start": start, "end": "2026-09-01"}],
            "windows": windows,
        }],
    }


def _persist_payload(*, mode: str = "full", start: str = "2026-07-01") -> dict:
    coverage = _coverage(mode=mode, start=start)
    results = []
    for window in coverage["domains"][0]["windows"]:
        rows = [_row()] if window["start"] == "2026-07-01" else []
        results.append({
            "Account": ACCOUNT,
            "NTDetailList": rows,
            "NTTotal": {},
            "receipt": {**window, "rows": len(rows)},
        })
    return {
        "deposit_twd": {
            "NTList": [{
                "Account": ACCOUNT, "AccountType": "活期存款", "AccountBal": "10030",
                "Branch": "012",
            }],
            "LoanList": [],
            "TotalData": {"Deposit": "10030", "Loan": "0"},
        },
        "twd_txns": results,
        "debit_accounts": [{
            "label": "活期存款 012-34-5678901", "identity": ACCOUNT, "currency": "TWD",
        }],
        "history_coverage": coverage,
        "card_bill_facts_ok": False,
    }


def _assert_all_empty(store: BankStore) -> None:
    assert all(value == 0 for value in store.stats().values())
    assert store.conn.execute("SELECT COUNT(*) FROM history_transaction_cursors").fetchone()[0] == 0


def test_ubot_persistence_writes_verified_history_and_cursor(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(ubot_persist_module, "_today", lambda: date(2026, 9, 1), raising=False)
    store = BankStore("ubot", user_id=1, source_account_id=7)
    try:
        delta = persist_collected("ubot", _persist_payload(), store)
        assert delta["twd_txn_new"] == 1
        assert store.latest_twd_transaction_dates() == {ACCOUNT: date(2026, 9, 1)}
    finally:
        store.close()


@pytest.mark.parametrize(
    "mutation", ["missing", "mismatch", "extra", "bad_row", "both_amounts", "nttotal_page"],
)
def test_ubot_bad_history_leaves_every_table_and_cursor_empty(
    tmp_path, monkeypatch, mutation: str,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(ubot_persist_module, "_today", lambda: date(2026, 9, 1), raising=False)
    store = BankStore("ubot", user_id=1, source_account_id=7)
    payload = _persist_payload()
    if mutation == "missing":
        payload["twd_txns"].pop()
    elif mutation == "mismatch":
        payload["twd_txns"][0]["receipt"]["end"] = "2026-07-30"
    elif mutation == "extra":
        payload["twd_txns"].append(dict(payload["twd_txns"][0]))
    elif mutation == "both_amounts":
        payload["twd_txns"][0]["NTDetailList"][0]["Expenditure"] = "30"
    elif mutation == "nttotal_page":
        payload["twd_txns"][0]["NTTotal"] = {"hasNextPage": True}
    else:
        payload["twd_txns"][0]["NTDetailList"][0]["Income"] = "1.5"
    try:
        with pytest.raises(ValueError, match="UBOT history coverage"):
            persist_collected("ubot", payload, store)
        _assert_all_empty(store)
    finally:
        store.close()


def test_ubot_outer_transaction_rolls_back_on_cursor_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(ubot_persist_module, "_today", lambda: date(2026, 9, 1), raising=False)
    store = BankStore("ubot", user_id=1, source_account_id=7)
    payload = _persist_payload()
    payload["card_billed"] = [{
        "CardHeader": {"stmtDate": "20260703"},
        "CardList": [{
            "cardNo": "****2302", "effectDate": "20260715", "postDate": "20260716",
            "txCode": "40", "txAmt": "100", "Currency": "TWD", "oriAmt": "100",
            "txDesc": "TEST", "typeName": "聯邦卡",
        }],
    }]
    monkeypatch.setattr(
        store, "record_history_coverage_cursors",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cursor failed")),
    )
    try:
        with pytest.raises(RuntimeError, match="cursor failed"):
            persist_collected("ubot", payload, store)
        _assert_all_empty(store)
    finally:
        store.close()


def test_ubot_incremental_persistence_binds_existing_cursor(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(ubot_persist_module, "_today", lambda: date(2026, 9, 1), raising=False)
    store = BankStore("ubot", user_id=1, source_account_id=7)
    monkeypatch.setattr(
        store, "latest_twd_transaction_dates", lambda: {ACCOUNT: date(2026, 8, 20)},
    )
    try:
        delta = persist_collected(
            "ubot", _persist_payload(mode="incremental", start="2026-08-01"), store,
        )
        assert delta["twd_txn_new"] == 0
    finally:
        store.close()


def test_ubot_direct_persister_is_durable_in_fresh_store(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(ubot_persist_module, "_today", lambda: date(2026, 9, 1), raising=False)
    store = BankStore("ubot", user_id=1, source_account_id=7)
    persist_ubot(_persist_payload(), store)
    store.close()

    reopened = BankStore("ubot", user_id=1, source_account_id=7)
    try:
        assert reopened.conn.execute("SELECT COUNT(*) FROM twd_transactions").fetchone()[0] == 1
    finally:
        reopened.close()


def test_ubot_preserves_legitimate_identical_source_occurrences(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(ubot_persist_module, "_today", lambda: date(2026, 9, 1), raising=False)
    store = BankStore("ubot", user_id=1, source_account_id=7)
    payload = _persist_payload()
    payload["twd_txns"][0]["NTDetailList"] = [_row(), _row()]
    payload["twd_txns"][0]["receipt"]["rows"] = 2
    try:
        assert persist_collected("ubot", payload, store)["twd_txn_new"] == 2
    finally:
        store.close()


@pytest.mark.parametrize("edit_billed_after_partial", [False, True])
def test_ubot_delayed_pending_transition_preserves_user_overlay(
    tmp_path, monkeypatch, edit_billed_after_partial: bool,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    card_no = "****2302"
    pending = {
        "cardNo": card_no, "effectiveDate": "20260715", "postingDate": "20260716",
        "txCode": "40", "txAmt": "100", "Currency": "TWD", "oriAmt": "100",
        "txDesc": "TEST", "typeName": "聯邦卡",
    }
    billed = {
        "CardHeader": {"stmtDate": "20260703"},
        "CardList": [{
            "cardNo": card_no, "effectDate": "20260715", "postDate": "20260716",
            "txCode": "40", "txAmt": "100", "Currency": "TWD", "oriAmt": "100",
            "txDesc": "TEST", "typeName": "聯邦卡",
        }],
    }

    store = BankStore("ubot", user_id=1, source_account_id=7)
    seeded = _persist_payload()
    seeded["card_unbilled"] = {"CardList": [pending]}
    persist_collected("ubot", seeded, store)
    splits = '[{"amount":40,"category":"旅遊"},{"amount":60,"category":"購物"}]'
    store.conn.execute(
        "UPDATE card_pending_txns SET category='旅遊', subcategory='機票', "
        "description_overwrite='KEEP', tags_overwrite='[\"keep\"]', "
        "auto_excluded=1, splits_overwrite=?",
        (splits,),
    )
    store.commit()
    store.close()

    store = BankStore("ubot", user_id=1, source_account_id=7)
    partial = _persist_payload()
    partial["card_billed"] = [billed]
    partial["card_unbilled"] = {"error": "session expired", "CardList": []}
    persist_collected("ubot", partial, store)
    assert store.conn.execute("SELECT COUNT(*) FROM card_pending_txns").fetchone()[0] == 1
    if edit_billed_after_partial:
        store.conn.execute(
            "UPDATE card_billed_txns SET category='餐飲', subcategory='聚餐', "
            "description_overwrite='NEW', tags_overwrite='[\"new\"]', auto_excluded=0, "
            "splits_overwrite=NULL"
        )
        store.commit()
    store.close()

    store = BankStore("ubot", user_id=1, source_account_id=7)
    complete = _persist_payload()
    complete["card_billed"] = [billed]
    complete["card_unbilled"] = {"CardList": []}
    persist_collected("ubot", complete, store)
    try:
        assert store.conn.execute("SELECT COUNT(*) FROM card_pending_txns").fetchone()[0] == 0
        row = store.conn.execute(
            "SELECT category, subcategory, description_overwrite, tags_overwrite, "
            "auto_excluded, splits_overwrite FROM card_billed_txns"
        ).fetchone()
        expected = (
            ("餐飲", "聚餐", "NEW", '["new"]', 0, None)
            if edit_billed_after_partial else
            ("旅遊", "機票", "KEEP", '["keep"]', 1, splits)
        )
        assert tuple(row) == expected
    finally:
        store.close()
