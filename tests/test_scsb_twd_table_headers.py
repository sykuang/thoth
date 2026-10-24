"""SCSB positional data must be attested by headers, not merchant/summary text."""
from contextlib import nullcontext
from html import escape
from types import SimpleNamespace

import pytest
from patchright.sync_api import Error as BrowserError, sync_playwright

from backend.banks.scsb import ScsbCrawler

EXPECTED = {
    "start": "2026/04/24", "end": "2026/10/23",
    "account_no": "90000000123456",
}
BAD_HEADERS = ["時間", "產品", "價格", "貨幣", "項目", "備註"]


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch(headless=True, args=[
                "--disable-background-networking", "--disable-component-update",
                "--proxy-server=http://127.0.0.1:9",
            ])
        except BrowserError as error:
            if "Executable doesn't exist at " in str(error):
                pytest.skip("Patchright headless browser is not installed")
            raise
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def page(browser):
    context = browser.new_context(service_workers="block")
    context.route("**/*", lambda route: route.abort())
    try:
        yield context.new_page()
    finally:
        context.close()


def _scope(page):
    page.set_content(
        '<section id="scope"><div>查詢條件：2026/04/24 2026/10/23 '
        '90000000123456</div></section>',
    )


def _table(headers):
    cells = "".join(f"<th>{value}</th>" for value in headers)
    return (
        f'<table id="result"><tr>{cells}</tr><tr><td>2026/07/03</td>'
        '<td>摘要 支出 存入 結餘</td><td>1</td><td></td><td>9</td>'
        '<td></td></tr></table>'
    )


def _insert(page, headers):
    page.locator("#scope").evaluate(
        "(element, html) => element.insertAdjacentHTML('beforeend', html)",
        _table(headers),
    )


def test_prepare_inventories_tables_without_attesting_their_schema(page):
    _scope(page)
    _insert(page, BAD_HEADERS)
    page.evaluate(ScsbCrawler._twd_inquiry_prepare_result_wait_script(), EXPECTED)
    assert page.evaluate(
        "() => window.__thothScsbTwdOldTables.has(document.querySelector('#result'))",
    ) is True
    assert page.evaluate(
        "() => window.__thothScsbTwdOldScopes.has(document.querySelector('#scope'))",
    ) is True
    assert page.evaluate(ScsbCrawler._twd_inquiry_result_ready_script(), EXPECTED) is False


def test_readiness_rejects_body_keywords_with_incompatible_column_headers(page):
    _scope(page)
    page.evaluate(ScsbCrawler._twd_inquiry_prepare_result_wait_script(), EXPECTED)
    _insert(page, BAD_HEADERS)
    assert page.evaluate(ScsbCrawler._twd_inquiry_result_ready_script(), EXPECTED) is False


def test_extraction_rechecks_changed_header_semantics_after_binding(page):
    _scope(page)
    page.evaluate(ScsbCrawler._twd_inquiry_prepare_result_wait_script(), EXPECTED)
    _insert(page, ["時間", "摘要", "支出", "存入", "結餘", "備註"])
    assert page.evaluate(ScsbCrawler._twd_inquiry_result_ready_script(), EXPECTED) is True
    page.locator("#result tr").first.evaluate(
        "(row, headers) => [...row.cells].forEach((cell, index) => "
        "{ cell.textContent = headers[index]; })", BAD_HEADERS,
    )
    extracted = page.evaluate(ScsbCrawler._twd_inquiry_extract_result_script(), EXPECTED)
    assert extracted == {"ok": False, "empty": False, "row_count": 0, "text": ""}


@pytest.mark.parametrize("headers", [
    ["時間", "摘要", "支出", "存入", "結餘", "備註"],
    ["Time", "Summary", "Expense", "Deposit", "Balance", "Note"],
])
def test_supported_ordered_headers_still_bind_and_extract(page, headers):
    _scope(page)
    page.evaluate(ScsbCrawler._twd_inquiry_prepare_result_wait_script(), EXPECTED)
    _insert(page, headers)
    assert page.evaluate(ScsbCrawler._twd_inquiry_result_ready_script(), EXPECTED) is True
    extracted = page.evaluate(ScsbCrawler._twd_inquiry_extract_result_script(), EXPECTED)
    assert extracted["ok"] is True
    assert extracted["row_count"] == 1
    records, complete = ScsbCrawler._parse_twd_inquiry_records(extracted["text"])
    assert complete is True
    assert len(records) == 1
    page.evaluate(ScsbCrawler._twd_inquiry_prepare_result_wait_script(), EXPECTED)
    assert page.evaluate(
        "() => window.__thothScsbTwdOldTables.has(document.querySelector('#result'))",
    ) is True


@pytest.mark.parametrize("hidden_style", [
    "display:none", "visibility:hidden", "visibility:collapse", "opacity:0",
])
def test_hidden_header_cannot_attest_reversed_visible_directions(page, hidden_style):
    _scope(page)
    html = _table(["時間", "摘要", "存入", "支出", "結餘", "備註"])
    hidden = '<tr style="' + hidden_style + '">' + ''.join(
        f'<th>{label}</th>' for label in ["時間", "摘要", "支出", "存入", "結餘", "備註"]
    ) + '</tr>'
    html = html.replace('<table id="result">', '<table id="result">' + hidden)
    html = html.replace('摘要 支出 存入 結餘', 'SYNTHETIC DEPOSIT')
    page.locator("#scope").evaluate(
        "(element, value) => element.insertAdjacentHTML('beforeend', value)", html,
    )
    page.evaluate(ScsbCrawler._twd_inquiry_prepare_result_wait_script(), EXPECTED)
    assert page.evaluate(
        "() => window.__thothScsbTwdOldTables.has(document.querySelector('#result'))",
    ) is True
    assert page.evaluate(ScsbCrawler._twd_inquiry_result_ready_script(), EXPECTED) is False
    page.evaluate("""() => {
        window.__thothScsbTwdResultScope = document.querySelector('#scope');
        window.__thothScsbTwdResultElement = document.querySelector('#result');
    }""")
    assert page.evaluate(
        ScsbCrawler._twd_inquiry_extract_result_script(), EXPECTED,
    ) == {"ok": False, "empty": False, "row_count": 0, "text": ""}


@pytest.fixture
def transport_page(page):
    class TransportPage:
        def __getattr__(self, name):
            return getattr(page, name)

        def wait_for_timeout(self, milliseconds):
            pass

        def wait_for_function(self, expression, *, arg, timeout):
            # Evaluate the native guard once; the collector independently checks it again.
            return page.evaluate(expression, arg)

        def expect_request(self, predicate, timeout):
            request = SimpleNamespace(
                url="https://ebank.scsb.com.tw/twde/twdeqr01/query", method="POST",
                resource_type="xhr", post_data="Data=synthetic",
                response=lambda: SimpleNamespace(ok=True),
            )
            assert predicate(request)
            return nullcontext(SimpleNamespace(value=request))

    return TransportPage()


@pytest.mark.parametrize("selector", ["#result tr:first-child", "#result", "#scope"])
def test_revealing_existing_result_cannot_certify_query_freshness(page, transport_page, selector):
    _scope(page)
    _insert(page, ["時間", "摘要", "支出", "存入", "結餘", "備註"])
    page.locator(selector).evaluate("element => element.style.opacity = '0'")
    before = page.locator("#result").inner_text()
    controls = (
        '<input type="radio" data-thoth-scsb-twd-period="custom">'
        '<input data-thoth-scsb-twd-period="start">'
        '<input data-thoth-scsb-twd-period="end">'
        '<select id="account"><option value="synthetic">synthetic</option></select>'
        '<button id="submit">Confirm</button>'
    )
    page.locator("body").evaluate(
        "(element, html) => element.insertAdjacentHTML('afterbegin', html)", controls,
    )
    page.locator("#submit").evaluate(
        "(button, target) => button.onclick = () => "
        "document.querySelector(target).style.opacity = '1'", selector,
    )

    crawler = object.__new__(ScsbCrawler)
    with pytest.raises(RuntimeError, match="query-result-stale"):
        crawler._collect_twd_period(
            transport_page, "#account", 0, "synthetic", EXPECTED["account_no"],
            {"ok": True, "start": EXPECTED["start"], "end": EXPECTED["end"]},
        )
    assert page.locator("#result").inner_text() == before
    assert page.evaluate(
        "window.__thothScsbTwdOldTables.has(document.querySelector('#result'))",
    ) is True


@pytest.mark.parametrize("unrelated", [False, True])
def test_unrelated_table_does_not_make_fresh_equal_text_result_stale(
    page, transport_page, unrelated,
):
    table = (
        '<table id="result"><tr><th>時間</th><th>摘要</th><th>支出</th>'
        '<th>存入</th><th>結餘</th></tr><tr><td>2026/07/03</td>'
        '<td>SYNTHETIC</td><td>123</td><td></td><td>900</td></tr></table>'
    )
    page.set_content(table)
    preview = '<pre id="preview">' + escape(page.locator("#result").inner_text()) + '</pre>'
    _scope(page)
    page.locator("#scope").evaluate(
        "(element, html) => element.insertAdjacentHTML('beforeend', html)",
        ('<table><tr><td>UNRELATED</td></tr></table>' if unrelated else '') + preview,
    )
    before = page.locator("#scope").inner_text()
    page.locator("body").evaluate(
        "element => element.insertAdjacentHTML('afterbegin', "
        "'<input type=radio data-thoth-scsb-twd-period=custom>' + "
        "'<input data-thoth-scsb-twd-period=start><input data-thoth-scsb-twd-period=end>' + "
        "'<select id=account><option value=synthetic>synthetic</option></select>' + "
        "'<button id=submit>Confirm</button>')",
    )
    page.locator("#submit").evaluate(
        "(button, html) => button.onclick = () => "
        "document.querySelector('#preview').outerHTML = html", table,
    )
    records = object.__new__(ScsbCrawler)._collect_twd_period(
        transport_page, "#account", 0, "synthetic", EXPECTED["account_no"],
        {"ok": True, "start": EXPECTED["start"], "end": EXPECTED["end"]},
    )
    assert len(records) == 1
    assert records[0]["expense"] == "123"
    assert page.locator("#scope").inner_text() == before


def test_hidden_template_does_not_hide_correct_rendered_headers(page):
    _scope(page)
    page.evaluate(ScsbCrawler._twd_inquiry_prepare_result_wait_script(), EXPECTED)
    hidden = '<tr style="display:none">' + ''.join(
        f'<th>{label}</th>' for label in BAD_HEADERS
    ) + '</tr>'
    html = _table(["時間", "摘要", "支出", "存入", "結餘", "備註"])
    html = html.replace('<table id="result">', '<table id="result">' + hidden)
    page.locator("#scope").evaluate(
        "(element, value) => element.insertAdjacentHTML('beforeend', value)", html,
    )
    assert page.evaluate(ScsbCrawler._twd_inquiry_result_ready_script(), EXPECTED) is True
    extracted = page.evaluate(ScsbCrawler._twd_inquiry_extract_result_script(), EXPECTED)
    assert extracted["ok"] is True
    assert extracted["row_count"] == 1
