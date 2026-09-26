"""Offline JSF producer → freshness/transport validation → coverage regressions."""
from __future__ import annotations

import pytest
from patchright.sync_api import sync_playwright

from backend.banks.esun import EsunCrawler
from backend.core.base import ResponseCollector


URL = "https://ebank.esunbank.com.tw/fco/fao01002/FAO01002.faces"
ACCOUNT = "0000000000001"
WINDOW = {"identity": ACCOUNT, "start": "2025-08-31", "end": "2026-08-30"}
GRID = """
<table id="fao01002:grid_DataGridBody"><tr>
<td>2026/08/20</td><td>12:00:00</td><td>利息</td><td></td>
<td>2</td><td>84</td><td>活存利息</td>
</tr></table>
"""


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def jsf_collect(browser, tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    context = browser.new_context(service_workers="block")
    page = context.new_page()
    crawler = object.__new__(EsunCrawler)
    crawler.transaction_cursors = {}
    observed = {}
    requests, blocked = [], []

    # Only unrelated menu navigation and long sleeps are replaced. Every JSF
    # evaluate, query click, response event, parser and coverage check is real.
    def navigate(_page, label, *_args):
        return {"frames": [{"result": {"clicked": label == "存款交易明細查詢"}}]}

    monkeypatch.setattr(crawler, "_navigate_menu", navigate)
    monkeypatch.setattr(crawler, "_navigate_credit_card_bill", lambda *_args: {"frames": []})
    original_wait = page.wait_for_timeout

    def wait(milliseconds):
        if milliseconds == 9000:
            page.wait_for_function("window.queryDone === true")
        else:
            original_wait(1)

    monkeypatch.setattr(page, "wait_for_timeout", wait)
    evaluate = page.main_frame.evaluate

    def observe(script, arg=None):
        value = evaluate(script, arg)
        if "const uniqueTotals" in script:
            observed["snapshot"] = value
        if "return {ok: true, marked" in script:
            observed["stale"] = value
        return value

    monkeypatch.setattr(page.main_frame, "evaluate", observe)

    def collect(result_html, *, fresh=True):
        scope = (
            f'<section class="qryresult">帳號 {ACCOUNT} '
            f'查詢期間 2025/08/31 至 2026/08/30 {result_html}</section>'
        )
        form = f"""
        <script id="sysInfo" type="application/json">{{"today":"2026/08/30"}}</script>
        <form>
          <select id="fao01002:dract" name="fao01002:dract">
            <option value="">===請選擇===</option>
            <option value="opaque-a">臺幣綜存 {ACCOUNT}</option>
          </select>
          <input name="fao01002:linkCommand" type="hidden">
          <input id="fao01002:j_id_intervalrdo4" type="radio" name="fao01002:intervalrdo" value="4">
          <input id="fao01002:startDate" name="fao01002:startDate">
          <input id="fao01002:endDate" name="fao01002:endDate">
          <input id="fao01002:j_id_sort1" type="radio" name="fao01002:txDateOrder" value="1">
          <button type="button">查詢</button>
        </form>
        {scope}
        <script>
          document.querySelector('button').onclick = async () => {{
            const response = await fetch(location.href, {{
              method: 'POST', body: new URLSearchParams(new FormData(document.querySelector('form')))
            }});
            const result = await response.text();
            if ({str(fresh).lower()}) document.querySelector('.qryresult').outerHTML = result;
            window.queryDone = true;
          }};
        </script>
        """

        def route_request(route):
            request = route.request
            if request.url != URL or request.method not in {"GET", "POST"}:
                blocked.append(request.url)
                route.abort()
                return
            requests.append(request.method)
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=form if request.method == "GET" else scope)

        context.route("**/*", route_request)
        page.goto(URL)
        return crawler.collect(page, ResponseCollector()).to_dict()

    yield collect, observed
    context.close()
    assert blocked == []
    assert requests == ["GET", "POST"]


@pytest.mark.parametrize(
    ("totals", "accepted", "count"),
    [
        ((), True, None),
        ((1,), True, 1),
        ((1, 1), True, 1),
        ((2,), False, 2),
        ((1, 2), False, None),
        ((2, 1), False, None),
    ],
    ids=["missing", "unique", "repeated-equal", "mismatch", "conflicting", "conflicting-reversed"],
)
def test_jsf_dom_totals_do_not_collapse_conflict_into_absence(jsf_collect, totals, accepted, count):
    collect, observed = jsf_collect
    result_html = GRID + "".join(f"<p>共 {total} 筆</p>" for total in totals)
    if accepted:
        data = collect(result_html)
        assert data["history_coverage"] == {
            "mode": "full",
            "domains": [{
                "domain": "twd_transactions",
                "expected": [WINDOW],
                "windows": [{**WINDOW, "status": "complete", "pages": 1}],
            }],
        }
        assert data["twd_txn_results"][0]["snapshot"]["totalCount"] == count
    else:
        with pytest.raises(RuntimeError, match="^esun-twd-history$"):
            collect(result_html)
    assert observed["stale"] == {"ok": True, "marked": 1}
    assert observed["snapshot"]["evidenceFresh"] is True
    if len(set(totals)) <= 1:
        assert observed["snapshot"]["totalCount"] == count
    else:
        assert observed["snapshot"]["totalCount"] == list(dict.fromkeys(totals))


@pytest.mark.parametrize("memo", ["共同生活費", "總計支出", "總筆數備註", "資料筆數備註", "共 1,000 筆"])
@pytest.mark.parametrize("total_position", ["outside", "footer", "absent"])
def test_transaction_memo_is_not_a_result_total(jsf_collect, memo, total_position):
    collect, observed = jsf_collect
    result_html = GRID.replace("活存利息", memo)
    if total_position == "outside":
        result_html += "<p>共 1 筆</p>"
    elif total_position == "footer":
        result_html = result_html.replace("</table>", '<tfoot><tr><td colspan="7">共 1 筆</td></tr></tfoot></table>')
    data = collect(result_html)
    assert data["history_coverage"]["domains"][0]["windows"][0]["status"] == "complete"
    assert memo in observed["snapshot"]["gridText"]
    assert observed["snapshot"]["totalCount"] == (None if total_position == "absent" else 1)


@pytest.mark.parametrize("total", ["共 2 筆", "共 1,000 筆", "共 1 筆 共 2 筆"])
def test_table_footer_total_is_not_removed_with_transaction_rows(jsf_collect, total):
    collect, observed = jsf_collect
    result_html = GRID.replace("活存利息", "共同生活費").replace(
        "</table>", f'<tfoot><tr><td colspan="7">{total}</td></tr></tfoot></table>',
    )
    with pytest.raises(RuntimeError, match="^esun-twd-history$"):
        collect(result_html)
    assert observed["snapshot"]["totalCount"] is not None


@pytest.mark.parametrize("label", [
    "共 1,000 筆",
    "共 1 筆</p><p>共 1,000 筆",
    "總計 1.0 筆",
    "總筆數 -1 筆",
    "資料筆數 １ 筆",
    "共 1",
    "總計 未知 筆",
    "資料筆數 1e3 筆",
])
def test_present_unparsed_jsf_total_is_not_absence(jsf_collect, label):
    collect, observed = jsf_collect
    with pytest.raises(RuntimeError, match="^esun-twd-history$"):
        collect(GRID + f"<p>{label}</p>")
    assert observed["snapshot"]["totalCount"] is not None


@pytest.mark.parametrize("fresh", [True, False], ids=["fresh", "stale"])
def test_exact_jsf_empty_marker_is_marked_and_only_fresh_result_covers(jsf_collect, fresh):
    collect, observed = jsf_collect
    result_html = "<p>查無符合資料！</p><p>共 0 筆</p>"
    if fresh:
        data = collect(result_html, fresh=True)
        assert data["history_coverage"] == {
            "mode": "full",
            "domains": [{
                "domain": "twd_transactions",
                "expected": [WINDOW],
                "windows": [{**WINDOW, "status": "explicit_empty", "pages": 1}],
            }],
        }
        assert data["twd_txn_results"][0]["snapshot"]["emptyMarker"] == "查無符合資料！"
    else:
        with pytest.raises(RuntimeError, match="^esun-twd-history$"):
            collect(result_html, fresh=False)
    assert observed["stale"] == {"ok": True, "marked": 1}
    assert observed["snapshot"]["emptyMarker"] == "查無符合資料！"
    assert observed["snapshot"]["evidenceFresh"] is fresh
    assert observed["snapshot"]["totalCount"] == 0
