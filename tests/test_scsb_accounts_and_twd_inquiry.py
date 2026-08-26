"""SCSB 帳戶分類與台幣明細導航回歸測試。"""
from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import MagicMock, Mock

import backend.banks.scsb as scsb_module
import backend.core.persist.scsb as scsb_persist_module
import pytest
from backend.banks.scsb import ScsbCrawler, _safe_select_inventory
from backend.core.persist import persist_scsb
from backend.core.store import BankStore


def test_scsb_extract_accounts_does_not_steal_loan_header_for_first_deposit():
    """總覽卡片上方有「我的貸款總餘額」，不能讓第一個活儲帳戶吃到貸款 header。"""
    text = """
我的帳戶總額
我的存款總額
NT$73,549
我的貸款總餘額
NT$20,589,800
我的帳戶摘要
所有帳戶查詢
看總覽
活儲存款
中壢分行
90000000167058
NT$73,500
交易明細
轉帳
轉定存
活儲存款
世貿分行
90000000207039
NT$0
交易明細
轉帳
轉定存
活期存款
中壢分行
90000000237023
USD1.55
交易明細
賣外幣
轉定存
貸款
西湖分行
90000000247044 到期日 140/09/24
NT$20,589,800
明細
基本資料
償還本金
"""
    accounts = ScsbCrawler._extract_accounts(text)

    by_acct = {a["account_no"]: a for a in accounts}
    assert by_acct["90000000167058"]["type_header"] == "活儲存款"
    assert by_acct["90000000207039"]["type_header"] == "活儲存款"
    assert by_acct["90000000237023"]["type_header"] == "活期存款"
    assert by_acct["90000000247044"]["type_header"] == "貸款"


def test_scsb_overview_inventory_does_not_require_visible_balance():
    inventory = ScsbCrawler._extract_overview_twd_inventory("""
活儲存款
90000000167058
NT$••••
活期存款
90000000207039
NT$-50
活期存款
90000000237023
USD1.55
貸款
90000000247044
NT$20,589,800
""")

    assert {account["account_no"] for account in inventory} == {
        "90000000167058", "90000000207039",
    }


def test_scsb_overview_empty_inventory_requires_explicit_empty_marker():
    assert ScsbCrawler._overview_twd_inventory_authoritative("我的帳戶摘要") is False
    assert ScsbCrawler._overview_twd_inventory_authoritative(
        "我的帳戶摘要\n目前沒有台幣存款帳戶",
    ) is True


def test_persist_scsb_first_deposit_remains_asset_not_loan(tmp_path, monkeypatch):
    """2620...8541 必須入成 deposit，否則 portfolio 會把活儲當負債。"""
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("scsb")
    try:
        persist_scsb(
            {
                "accounts": [
                    {"account_no": "90000000167058", "currency": "TWD", "balance": "73500", "type_header": "活儲存款"},
                    {"account_no": "90000000247044", "currency": "TWD", "balance": "20589800", "type_header": "貸款"},
                ],
            },
            store,
        )
        rows = store.conn.execute(
            "SELECT account_no, type, product_type, raw_balance FROM accounts ORDER BY account_no",
        ).fetchall()
    finally:
        store.close()

    got = {r["account_no"]: dict(r) for r in rows}
    assert got["90000000167058"]["type"] == "活儲存款"
    assert got["90000000167058"]["product_type"] == "deposit"
    assert got["90000000167058"]["raw_balance"] == 73500
    assert got["90000000247044"]["product_type"] == "loan"
    assert got["90000000247044"]["raw_balance"] == -20589800


def test_scsb_twd_inquiry_accepts_chinese_menu_labels():
    """SCSB 目前是中文選單；導航關鍵字不能只找 TWD Deposit 英文。"""
    nav = ScsbCrawler._twd_inquiry_nav_script()
    assert "臺幣存匯" in nav
    assert "台幣存匯" in nav
    assert "TWD Deposit" in nav  # fallback only
    assert "交易明細" in nav


def _run_twd_period_script(
    start_min: str = "", *, shared_parent: bool = False, full_history: bool = True,
) -> dict:
    from patchright.sync_api import sync_playwright

    with sync_playwright() as patchright:
        if not Path(patchright.chromium.executable_path).exists():
            pytest.skip("Patchright browser binary is not installed")
        browser = patchright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            date_controls = (
                f'<div>查詢起日 <input id="start" min="{start_min}"> '
                '<span>查詢迄日</span> <input id="end" value="2026/08/22"></div>'
                if shared_parent
                else f"""
                <div>查詢起日 <input id="start" min="{start_min}"></div>
                <div>查詢迄日 <input id="end" value="2026/08/22"></div>
                """
            )
            page.set_content(
                f"""
                <label><input type="radio" name="period">當日</label>
                <label><input type="radio" name="period">近一月</label>
                <label><input id="custom" type="radio" name="period">自訂</label>
                {date_controls}
                """,
            )
            result = page.evaluate(
                ScsbCrawler._twd_inquiry_period_script(full_history=full_history),
            )
            result["custom_checked"] = page.locator("#custom").is_checked()
            result["actual_start"] = page.locator("#start").input_value()
            result["actual_end"] = page.locator("#end").input_value()
            return result
        finally:
            browser.close()


def test_scsb_twd_period_uses_system_earliest_date_when_exposed():
    result = _run_twd_period_script("2024-01-01")

    assert result == {
        "ok": True,
        "period": "system-limit",
        "start": "2024/01/01",
        "end": "2026/08/22",
        "custom_checked": True,
        "actual_start": "2024/01/01",
        "actual_end": "2026/08/22",
    }


def test_scsb_twd_period_falls_back_to_one_calendar_year():
    result = _run_twd_period_script()

    assert result == {
        "ok": True,
        "period": "one-year",
        "start": "2025/08/22",
        "end": "2026/08/22",
        "custom_checked": True,
        "actual_start": "2025/08/22",
        "actual_end": "2026/08/22",
    }


def test_scsb_existing_account_period_uses_one_calendar_month():
    result = _run_twd_period_script("2024-01-01", full_history=False)

    assert result == {
        "ok": True,
        "period": "one-month",
        "start": "2026/07/22",
        "end": "2026/08/22",
        "custom_checked": True,
        "actual_start": "2026/07/22",
        "actual_end": "2026/08/22",
    }


def test_scsb_twd_period_invalid_system_minimum_falls_back_to_one_year():
    result = _run_twd_period_script("2025-13-01")

    assert result["ok"] is True
    assert result["period"] == "one-year"
    assert result["start"] == "2025/08/22"


def test_scsb_twd_period_keeps_start_and_end_distinct_in_shared_parent():
    result = _run_twd_period_script(shared_parent=True)

    assert result["start"] == result["actual_start"] == "2025/08/22"
    assert result["end"] == result["actual_end"] == "2026/08/22"


def test_scsb_twd_period_verification_detects_controlled_input_reversion():
    from patchright.sync_api import sync_playwright

    with sync_playwright() as patchright:
        if not Path(patchright.chromium.executable_path).exists():
            pytest.skip("Patchright browser binary is not installed")
        browser = patchright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(
                """
                <label><input id="custom" type="radio">自訂</label>
                <div>查詢起日 <input id="start"></div>
                <div>查詢迄日 <input id="end" value="2026/08/22"></div>
                """,
            )
            period = page.evaluate(ScsbCrawler._twd_inquiry_period_script())
            page.locator("#start").evaluate("el => { el.value = '2026/08/01'; }")

            verified = page.evaluate(
                ScsbCrawler._twd_inquiry_period_verification_script(), period,
            )

            assert verified == {"ok": False}
        finally:
            browser.close()


def test_scsb_twd_result_readiness_and_extraction_bind_exact_query():
    from patchright.sync_api import sync_playwright

    with sync_playwright() as patchright:
        if not Path(patchright.chromium.executable_path).exists():
            pytest.skip("Patchright browser binary is not installed")
        browser = patchright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(
                """
                <div id="outside">2025/08/22 2026/08/22 90000000654321 OUTSIDE-STALE</div>
                <section id="result-scope">
                  <div id="time">資料時間：2026/08/22 10:00:00</div>
                  <div id="criteria">查詢條件：2025/08/22 2026/08/22 90000000123456</div>
                  <table id="result"><tr><th>時間</th><th>摘要</th><th>支出</th><th>存入</th><th>結餘</th></tr>
                  <tr><td>2026/01/01</td><td>STALE-A</td><td>1</td><td></td><td>9</td></tr></table>
                </section>
                """,
            )
            expected = {
                "start": "2025/08/22", "end": "2026/08/22",
                "account_no": "90000000654321",
            }
            page.evaluate(ScsbCrawler._twd_inquiry_prepare_result_wait_script(), expected)
            page.locator("#time").evaluate(
                "el => { el.textContent = '資料時間：2026/08/22 10:00:01'; }",
            )
            assert page.evaluate(ScsbCrawler._twd_inquiry_result_ready_script(), expected) is False

            page.locator("#criteria").evaluate(
                "el => { el.textContent = '查詢條件：2025/08/22 2026/08/22 90000000654321'; }",
            )
            assert page.evaluate(ScsbCrawler._twd_inquiry_result_ready_script(), expected) is False
            page.locator("#result").evaluate("el => { el.outerHTML = el.outerHTML; }")
            assert page.evaluate(ScsbCrawler._twd_inquiry_result_ready_script(), expected) is True
            extracted = page.evaluate(ScsbCrawler._twd_inquiry_extract_result_script(), expected)
            assert extracted["ok"] is True
            assert extracted["row_count"] == 1
            assert "STALE-A" in extracted["text"]
            assert "OUTSIDE-STALE" not in extracted["text"]

            page.evaluate(ScsbCrawler._twd_inquiry_prepare_result_wait_script(), expected)
            assert page.evaluate(ScsbCrawler._twd_inquiry_result_ready_script(), expected) is False
            page.locator("#time").evaluate(
                "el => { el.textContent = '資料時間：2026/08/22 10:00:02'; }",
            )
            assert page.evaluate(ScsbCrawler._twd_inquiry_result_ready_script(), expected) is False
            page.locator("#result").evaluate("el => { el.outerHTML = el.outerHTML; }")
            assert page.evaluate(ScsbCrawler._twd_inquiry_result_ready_script(), expected) is True

            page.locator("#result-scope").evaluate("""el => {
                el.innerHTML = `
                  <div id="time">資料時間：2026/08/22 10:00:03</div>
                  <div id="criteria">查詢條件：2025/08/22 2026/08/22 90000000654321</div>
                  <div id="empty">查無交易資料</div>`;
            }""")
            page.evaluate(ScsbCrawler._twd_inquiry_prepare_result_wait_script(), expected)
            assert page.evaluate(ScsbCrawler._twd_inquiry_result_ready_script(), expected) is False
            page.locator("#time").evaluate(
                "el => { el.textContent = '資料時間：2026/08/22 10:00:04'; }",
            )
            assert page.evaluate(ScsbCrawler._twd_inquiry_result_ready_script(), expected) is False
            page.locator("#empty").evaluate("el => { el.outerHTML = el.outerHTML; }")
            assert page.evaluate(ScsbCrawler._twd_inquiry_result_ready_script(), expected) is True
        finally:
            browser.close()


def test_scsb_twd_pagination_detector_handles_aria_and_disabled_controls():
    from patchright.sync_api import sync_playwright

    with sync_playwright() as patchright:
        if not Path(patchright.chromium.executable_path).exists():
            pytest.skip("Patchright browser binary is not installed")
        browser = patchright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content('<section id="scope"><button aria-label="Next page">›</button></section>')
            page.evaluate("window.__thothScsbTwdResultScope = document.querySelector('#scope')")
            assert page.evaluate(ScsbCrawler._twd_inquiry_result_state_script()) == {
                "error": False,
                "pagination": True,
            }
            page.locator("button").evaluate("el => { el.disabled = true; }")
            assert page.evaluate(ScsbCrawler._twd_inquiry_result_state_script()) == {
                "error": False,
                "pagination": False,
            }
            page.set_content(
                '<section id="scope"><nav aria-label="pagination"><button class="active">1</button><button>2</button></nav></section>'
            )
            page.evaluate("window.__thothScsbTwdResultScope = document.querySelector('#scope')")
            assert page.evaluate(ScsbCrawler._twd_inquiry_result_state_script())["pagination"] is True
            page.set_content('<section id="scope"><div class="pagination"><button><span>›</span></button></div></section>')
            page.evaluate("window.__thothScsbTwdResultScope = document.querySelector('#scope')")
            assert page.evaluate(ScsbCrawler._twd_inquiry_result_state_script())["pagination"] is True
            page.set_content(
                '<section id="scope"><button onclick="goToNextPage()">More</button></section>',
            )
            page.evaluate("window.__thothScsbTwdResultScope = document.querySelector('#scope')")
            assert page.evaluate(ScsbCrawler._twd_inquiry_result_state_script())["pagination"] is True
            page.set_content(
                '<section id="scope"><ul class="pagination">'
                '<li class="active"><a>1</a></li><li class="disabled"><a>Next</a></li>'
                '</ul></section>',
            )
            page.evaluate("window.__thothScsbTwdResultScope = document.querySelector('#scope')")
            assert page.evaluate(ScsbCrawler._twd_inquiry_result_state_script())["pagination"] is False
        finally:
            browser.close()


def test_scsb_twd_parser_keeps_records_beyond_preview_limit():
    lines = [
        f"2026/01/01\t摘要{i:03d}\tNT$ 1\t\tNT$ 9,999\t備註{i:03d}-" + "x" * 40
        for i in range(250)
    ]
    text = "\n".join(lines)
    assert len(text) > 8000

    records, complete = ScsbCrawler._parse_twd_inquiry_records(text)

    assert complete is True
    assert len(records) == 250
    assert records[-1]["remarks"].startswith("備註249-")


def test_scsb_twd_parser_rejects_partial_dated_rows():
    records, complete = ScsbCrawler._parse_twd_inquiry_records(
        "2026/01/01\t完整\tNT$ 1\t\tNT$ 9\t備註\n"
        "2026/01/02\t欄位不足"
    )

    assert len(records) == 1
    assert complete is False


def test_scsb_twd_parser_rejects_extra_columns():
    records, complete = ScsbCrawler._parse_twd_inquiry_records(
        "2026/01/01\t摘要\tNT$ 1\t\tNT$ 9\t備註\t額外欄位"
    )

    assert records == []
    assert complete is False


def test_scsb_twd_parser_rejects_empty_or_invalid_money_columns():
    for line in (
        "2026/01/02\tBROKEN\t\t\t\t",
        "2026/01/02\tBROKEN\tnope\t\tinvalid\t",
    ):
        records, complete = ScsbCrawler._parse_twd_inquiry_records(line)
        assert records == []
        assert complete is False


def test_scsb_twd_parser_rejects_bad_grouping_and_impossible_date():
    for line in (
        "2026/01/02\tBROKEN\tNT$ 1,2,3\t\tNT$ 9\t",
        "2026/01/02\tBROKEN\tNT$ 1.25\t\tNT$ 9\t",
        "2026/01/02\tBROKEN\tNT$ 9999999999999999\t\tNT$ 9\t",
        "2026/99/99\tBROKEN\tNT$ 1\t\tNT$ 9\t",
    ):
        records, complete = ScsbCrawler._parse_twd_inquiry_records(line)
        assert records == []
        assert complete is False


def test_scsb_twd_parser_rejects_negative_or_dual_direction_amounts():
    for line in (
        "2026/01/02\tBROKEN\tNT$ -100\t\tNT$ 9\t",
        "2026/01/02\tBROKEN\tNT$ 100\tNT$ 50\tNT$ 9\t",
    ):
        records, complete = ScsbCrawler._parse_twd_inquiry_records(line)
        assert records == []
        assert complete is False


def test_scsb_twd_parser_canonicalizes_roc_year():
    records, complete = ScsbCrawler._parse_twd_inquiry_records(
        "115/01/02\t明細\tNT$ 1\t\tNT$ 9\t"
    )

    assert complete is True
    assert records[0]["date"] == "2026/01/02"


def test_scsb_statement_month_tabs_keep_all_bank_exposed_months():
    months = [f"2026/{month:02d}" for month in range(8, 0, -1)]
    text = "Data Time：2026/08/26 01:00:00\n" + "\n".join(months)

    assert ScsbCrawler._statement_month_tabs(text) == months


def test_scsb_statement_month_summary_discards_raw_dom():
    summary = ScsbCrawler._statement_month_summary(
        "Current Period Total Amount Due\nNT$ 12,345\n"
        "Current Period Total Minimum Amount Due\nNT$ 1,234\nPRIVATE TRANSACTION"
    )

    assert summary == {"due_amount": 12345, "min_payment": 1234, "has_data": True}
    assert "PRIVATE" not in repr(summary)


@pytest.mark.parametrize("text", [
    "System error",
    "Current Period Total Amount Due\n123",
])
def test_scsb_statement_month_summary_rejects_error_or_missing_labels(text):
    with pytest.raises(ValueError):
        ScsbCrawler._statement_month_summary(text)


@pytest.mark.parametrize("due", ["1,2,3", "12.50", "NaN"])
def test_scsb_statement_month_summary_rejects_malformed_amount(due):
    with pytest.raises(ValueError):
        ScsbCrawler._statement_month_summary(
            "Current Period Total Amount Due\n"
            f"{due}\n"
            "Current Period Total Minimum Amount Due\n1,234\n"
        )


def test_scsb_statement_month_summary_rejects_timeout_with_stale_labels():
    with pytest.raises(ValueError):
        ScsbCrawler._statement_month_summary(
            "Connection timed out\n"
            "Current Period Total Amount Due\n12,345\n"
            "Current Period Total Minimum Amount Due\n1,234\n"
        )


def test_scsb_twd_year_query_is_bounded_and_omits_private_debug_sinks():
    source = (
        inspect.getsource(ScsbCrawler.collect)
        + inspect.getsource(ScsbCrawler._collect_twd_inquiry)
        + inspect.getsource(ScsbCrawler._collect_twd_account)
        + inspect.getsource(ScsbCrawler._collect_credit_card_inquiry)
        + inspect.getsource(scsb_persist_module.persist_scsb)
    )

    assert "wait_for_timeout(10000)" not in source
    assert "wait_for_function(" in source
    assert "timeout=120000" in source
    assert "_twd_inquiry_prepare_result_wait_script" in source
    assert "_twd_inquiry_result_ready_script" in source
    assert "page.screenshot" not in source
    assert 'put_daily_metric("overview_text_preview"' not in source
    assert '"snippet"' not in source
    assert '"url"' not in inspect.getsource(ScsbCrawler._collect_credit_card_inquiry)


def _mock_twd_query_page(*, full_text: str = "", result_state: dict | None = None) -> MagicMock:
    page = MagicMock()
    page.url = "https://example.invalid/query"
    query_request = Mock()
    query_request.response.return_value = Mock(ok=True)
    page.expect_request.return_value.__enter__.return_value = Mock(value=query_request)
    page._query_request = query_request
    page.evaluate.side_effect = [
        {"ok": True},
        None,
        [{"id": "account", "name": "account", "options": [{"value": "90000000123456"}]}],
        {"ok": True, "period": "one-year", "start": "2025/08/22", "end": "2026/08/22"},
        {"ok": True},
        None,
        "查詢",
        result_state or {"error": False, "pagination": False},
        {
            "ok": True,
            "empty": not full_text,
            "row_count": len(full_text.splitlines()) if full_text else 0,
            "text": full_text,
        },
    ]
    return page


def test_scsb_twd_query_timeout_propagates_instead_of_returning_empty(tmp_path):
    page = _mock_twd_query_page()
    page.wait_for_function.side_effect = TimeoutError("synthetic timeout")

    with pytest.raises(RuntimeError, match="SCSB TWD inquiry failed"):
        object.__new__(ScsbCrawler)._collect_twd_inquiry(page, {"90000000123456"})
    page.wait_for_function.assert_called_once()
    page._query_request.response.assert_called_once()


def test_scsb_twd_navigation_failure_fails_closed(tmp_path):
    page = _mock_twd_query_page()
    side_effects = list(page.evaluate.side_effect)
    side_effects[0] = {"ok": False, "step": "L1"}
    page.evaluate.side_effect = side_effects

    with pytest.raises(RuntimeError, match="SCSB TWD inquiry failed"):
        object.__new__(ScsbCrawler)._collect_twd_inquiry(page, {"90000000123456"})


def test_scsb_card_leaf_navigation_failure_fails_closed():
    page = MagicMock()
    page.evaluate.return_value = {"ok": False}

    with pytest.raises(RuntimeError, match="credit-card inquiry failed"):
        object.__new__(ScsbCrawler)._collect_credit_card_inquiry(page)


def test_scsb_card_collector_returns_normalized_data_without_raw_dom():
    page = MagicMock()
    state = {"leaf": ""}
    texts = {
        "Unbilled Transaction Details": (
            "Unbilled Transaction Details\nYou currently have no new transactions"
        ),
        "Real-Time Transaction Records": (
            "Real-Time Transaction Records\nNo real-time transaction records"
        ),
        "Statement Inquiry and Payment": (
            "Statement Inquiry and Payment\nData Time：2026/08/26\n2026/05\n2026/04\n"
            "Current Period Total Amount Due\n12,345\n"
            "Current Period Total Minimum Amount Due\n1,500\nPRIVATE TRANSACTION"
        ),
    }

    def evaluate(script, arg=None):
        if script == ScsbCrawler._card_leaf_scope_text_script():
            assert isinstance(arg, str)
            return texts[arg]
        if "const allBtns" in script and isinstance(arg, str) and arg in texts:
            state["leaf"] = arg
            return {"ok": True}
        if script == scsb_module.JS_KILL_MODAL:
            return None
        if script == "document.body.innerText":
            return texts[state["leaf"]]
        if "querySelectorAll('input')" in script:
            return []
        if "^(Confirm|查詢|確認|Search)" in script:
            return True
        if "rawTarget" in script:
            assert isinstance(arg, list)
            return {"ok": True, "panel": f"#month-panel-{arg[0].replace('/', '-')}"}
        if isinstance(arg, str) and arg.startswith("#month-panel-"):
            return texts["Statement Inquiry and Payment"]
        if arg in (
            ["2026/05", "Statement Inquiry and Payment"],
            ["2026/04", "Statement Inquiry and Payment"],
        ):
            return True
        raise AssertionError("unexpected evaluate call")

    page.evaluate.side_effect = evaluate
    result = object.__new__(ScsbCrawler)._collect_credit_card_inquiry(page)

    assert result["leaves"]["unbilled"] == {"nav": {"ok": True}, "empty": True}
    assert result["leaves"]["current"] == {"nav": {"ok": True}, "empty": True}
    assert result["leaves"]["statement"]["months"] == [
        {
            "month": "2026/05", "due_amount": 12345,
            "min_payment": 1500, "has_data": True,
        },
        {
            "month": "2026/04", "due_amount": 12345,
            "min_payment": 1500, "has_data": True,
        },
    ]
    assert "PRIVATE TRANSACTION" not in repr(result)
    assert page.wait_for_function.call_count == 9


def test_scsb_twd_query_request_predicate_is_exact():
    expected = {
        "account_no": "90000000123456",
        "start": "2025/08/22",
        "end": "2026/08/22",
    }
    request = Mock(
        url="https://ebank.scsb.com.tw/ibap/api/query",
        method="POST",
        resource_type="xhr",
        post_data="account_no=90000000123456&start=2025%2F08%2F22&end=2026%2F08%2F22",
    )
    assert ScsbCrawler._is_twd_query_request(request, expected) is True

    for url in (
        "http://ebank.scsb.com.tw/ibap/api/query",
        "https://ebank.scsb.com.tw:444/ibap/api/query",
        "https://ebank.scsb.com.tw/unrelated/query",
        "https://evil.example/ibap/api/query",
    ):
        request.url = url
        assert ScsbCrawler._is_twd_query_request(request, expected) is False
    request.url = "https://ebank.scsb.com.tw/ibap/api/query"
    request.method = "GET"
    assert ScsbCrawler._is_twd_query_request(request, expected) is False
    request.method = "POST"
    request.post_data = "foo=90000000123456&bar=2025%2F08%2F22&baz=2026%2F08%2F22"
    assert ScsbCrawler._is_twd_query_request(request, expected) is False
    request.post_data = (
        "account_no=9000000012345699&start=2025%2F08%2F22&end=2026%2F08%2F22"
    )
    assert ScsbCrawler._is_twd_query_request(request, expected) is False
    request.post_data = (
        "account_no=90000000123456&accountNumber=90000000999999&"
        "start=2025%2F08%2F22&end=2026%2F08%2F22"
    )
    assert ScsbCrawler._is_twd_query_request(request, expected) is False
    request.post_data = (
        '{"account_no":true,"accountNumber":"90000000123456",'
        '"start":"2025/08/22","end":"2026/08/22"}'
    )
    assert ScsbCrawler._is_twd_query_request(request, expected) is False
    request.post_data = "account_no=90000000999999&start=2025%2F08%2F22&end=2026%2F08%2F22"
    assert ScsbCrawler._is_twd_query_request(request, expected) is False
    request.post_data = None
    assert ScsbCrawler._is_twd_query_request(request, expected) is False


def test_scsb_statement_month_request_predicate_is_exact():
    request = Mock(
        url="https://ebank.scsb.com.tw/ibap/api/query",
        method="POST",
        resource_type="xhr",
        post_data="statementMonth=2026%2F05",
    )

    assert ScsbCrawler._is_statement_month_request(request, "2026/05") is True
    request.url = "https://ebank.scsb.com.tw/ibap/api/profile"
    assert ScsbCrawler._is_statement_month_request(request, "2026/05") is False
    request.url = "https://ebank.scsb.com.tw/ibap/api/query"
    request.post_data = "statementMonth=2026%2F04"
    assert ScsbCrawler._is_statement_month_request(request, "2026/05") is False
    request.post_data = '{"statementMonth":true,"month":"2026/05"}'
    assert ScsbCrawler._is_statement_month_request(request, "2026/05") is False


@pytest.mark.parametrize("result_state", [{"error": True, "pagination": False}, {"error": False, "pagination": True}])
def test_scsb_twd_rejected_or_paginated_result_fails_closed(tmp_path, result_state):
    page = _mock_twd_query_page(result_state=result_state)

    with pytest.raises(RuntimeError, match="SCSB TWD inquiry failed"):
        object.__new__(ScsbCrawler)._collect_twd_inquiry(page, {"90000000123456"})
    assert page.evaluate.call_args_list[7].args[0] == ScsbCrawler._twd_inquiry_result_state_script()


def test_scsb_twd_collector_parses_full_dom_beyond_preview(tmp_path):
    full_text = "\n".join(
        f"2026/01/01\t摘要{i:03d}\tNT$ 1\t\tNT$ 9,999\t備註{i:03d}-" + "x" * 40
        for i in range(250)
    )
    page = _mock_twd_query_page(full_text=full_text)
    crawler = object.__new__(ScsbCrawler)
    crawler.full_history = False

    result = crawler._collect_twd_inquiry(page, {"90000000123456"})

    assert page.evaluate.call_args_list[3].args[0] == (
        ScsbCrawler._twd_inquiry_period_script(full_history=False)
    )
    assert "text" not in result
    assert len(result["records"]) == 250


def test_scsb_twd_collector_dom_read_failure_fails_closed(tmp_path):
    page = _mock_twd_query_page()
    side_effects = list(page.evaluate.side_effect)
    side_effects[-1] = RuntimeError("synthetic DOM read failure")
    page.evaluate.side_effect = side_effects

    with pytest.raises(RuntimeError, match="SCSB TWD inquiry failed"):
        object.__new__(ScsbCrawler)._collect_twd_inquiry(page, {"90000000123456"})


def test_scsb_twd_nonempty_result_without_rows_fails_closed(tmp_path):
    page = _mock_twd_query_page(full_text="查詢完成但沒有表格，也沒有明確查無資料訊息")

    with pytest.raises(RuntimeError, match="SCSB TWD inquiry failed"):
        object.__new__(ScsbCrawler)._collect_twd_inquiry(page, {"90000000123456"})


def test_scsb_twd_collector_rejects_unrecognized_table_row(tmp_path):
    page = _mock_twd_query_page(full_text=(
        "2026/01/01\t完整\tNT$ 1\t\tNT$ 9\t\n"
        "NOT-A-DATE\t損壞\tNT$ 1\t\tNT$ 8\t"
    ))

    with pytest.raises(RuntimeError, match="SCSB TWD inquiry failed"):
        object.__new__(ScsbCrawler)._collect_twd_inquiry(page, {"90000000123456"})


def test_scsb_twd_collector_queries_every_account(tmp_path):
    page = _mock_twd_query_page(full_text="2026/01/01\t第一帳戶\tNT$ 1\t\tNT$ 9\t")
    side_effects = list(page.evaluate.side_effect)
    side_effects[2] = [{
        "id": "account",
        "name": "account",
        "options": [{"value": "90000000123456"}, {"value": "90000000654321"}],
    }]
    side_effects.extend(side_effects[3:])
    page.evaluate.side_effect = side_effects

    result = object.__new__(ScsbCrawler)._collect_twd_inquiry(
        page, {"90000000123456", "90000000654321"})

    assert page.select_option.call_count == 2
    assert [call.kwargs["value"] for call in page.select_option.call_args_list] == [
        "90000000123456", "90000000654321",
    ]
    assert {row["account_no"] for row in result["records"]} == {
        "90000000123456", "90000000654321",
    }
    assert "text" not in result
    assert all("text" not in account for account in result["accounts"])
    assert all("records" not in account for account in result["accounts"])


def test_scsb_twd_collector_rejects_partial_account_dropdown(tmp_path):
    page = _mock_twd_query_page()

    with pytest.raises(RuntimeError, match="SCSB TWD inquiry failed"):
        object.__new__(ScsbCrawler)._collect_twd_inquiry(
            page, {"90000000123456", "90000000654321"})
    page.select_option.assert_not_called()


def test_persist_scsb_uses_each_record_account_number(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("scsb")
    first = {
        "account_no": "90000000123456", "date": "2026/01/01",
        "summary": "第一帳戶", "expense": "1", "deposit": "", "balance": "9", "remarks": "",
    }
    second = {
        "account_no": "90000000654321", "date": "2026/01/02",
        "summary": "第二帳戶", "expense": "", "deposit": "2", "balance": "11", "remarks": "",
    }
    try:
        persist_scsb({
            "accounts": [
                {"account_no": first["account_no"], "currency": "TWD", "balance": "9", "type_header": "活儲存款"},
                {"account_no": second["account_no"], "currency": "TWD", "balance": "11", "type_header": "活儲存款"},
            ],
            "twd_inquiry": {
                "records": [first, second],
                "accounts": [
                    {"account_no": first["account_no"], "record_count": 1},
                    {"account_no": second["account_no"], "record_count": 1},
                ],
            },
        }, store)
        account_nos = {
            row[0] for row in store.conn.execute("SELECT account_no FROM twd_transactions")
        }
    finally:
        store.close()

    assert account_nos == {"90000000123456", "90000000654321"}


def test_persist_scsb_rejects_transaction_without_account_provenance(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("scsb")
    try:
        with pytest.raises(ValueError, match="inventory"):
            persist_scsb({
                "accounts": [{
                    "account_no": "90000000123456",
                    "currency": "TWD",
                    "balance": "9",
                    "type_header": "活儲存款",
                }],
                "twd_inquiry": {"records": [{
                    "date": "2026/01/01", "summary": "缺帳號",
                    "expense": "1", "deposit": "", "balance": "9", "remarks": "",
                }]},
            }, store)
        row = store.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()
        assert row is not None and row[0] == 0
    finally:
        store.close()


def test_persist_scsb_multi_account_rejects_top_level_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("scsb")
    try:
        with pytest.raises(ValueError, match="account_no"):
            persist_scsb({"twd_inquiry": {
                "account_no": "90000000111111",
                "accounts": [
                    {"account_no": "90000000111111", "record_count": 0},
                    {"account_no": "90000000222222", "record_count": 1},
                ],
                "records": [{
                    "date": "2026/01/01", "summary": "第二帳戶交易",
                    "expense": "1", "deposit": "", "balance": "9", "remarks": "",
                }],
            }}, store)
    finally:
        store.close()


def test_persist_scsb_rejects_incomplete_account_inventory(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("scsb")
    try:
        with pytest.raises(ValueError, match="incomplete"):
            persist_scsb({
                "accounts": [{
                    "account_no": "90000000111111", "currency": "TWD",
                    "balance": "9", "type_header": "活儲存款",
                }],
                "twd_inquiry": {
                    "accounts": [{"account_no": "90000000111111", "record_count": 2}],
                    "records": [{
                        "account_no": "90000000111111", "date": "2026/01/01",
                        "summary": "只有一筆", "expense": "1", "deposit": "",
                        "balance": "9", "remarks": "",
                    }],
                },
            }, store)
    finally:
        store.close()


def test_persist_scsb_accepts_authoritative_empty_twd_inventory(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("scsb")
    try:
        delta = persist_scsb({
            "accounts": [],
            "twd_inquiry": {"accounts": [], "records": []},
        }, store)
    finally:
        store.close()

    assert delta["twd_txn_new"] == 0


def test_persist_scsb_rejects_empty_or_boolean_inventory(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("scsb")
    account = {
        "account_no": "90000000111111", "currency": "TWD",
        "balance": "9", "type_header": "活儲存款",
    }
    try:
        with pytest.raises(ValueError, match="payload"):
            persist_scsb({"accounts": [account], "twd_inquiry": None}, store)
        with pytest.raises(ValueError, match="payload"):
            persist_scsb({"accounts": [account], "twd_inquiry": {}}, store)
        with pytest.raises(ValueError, match="inventory"):
            persist_scsb({
                "accounts": [account],
                "twd_inquiry": {
                    "accounts": [{"account_no": account["account_no"], "record_count": True}],
                    "records": [],
                },
            }, store)
    finally:
        store.close()


def test_persist_scsb_rejects_incomplete_normalized_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("scsb")
    account = {
        "account_no": "90000000111111", "currency": "TWD",
        "balance": "9", "type_header": "活儲存款",
    }
    try:
        with pytest.raises(ValueError, match="provenance"):
            persist_scsb({
                "accounts": [account],
                "twd_inquiry": {
                    "accounts": [{"account_no": account["account_no"], "record_count": 1}],
                    "records": [{"account_no": account["account_no"]}],
                },
            }, store)
        with pytest.raises(ValueError, match="card inquiry payload"):
            persist_scsb({"card_inquiry": None}, store)
        with pytest.raises(ValueError, match="card inquiry payload"):
            persist_scsb({"card_inquiry": False}, store)
        with pytest.raises(ValueError, match="statement month summary"):
            persist_scsb({
                "card_inquiry": {"leaves": {
                    "statement": {"months": [{
                        "month": "2026/05", "due_amount": -1,
                        "min_payment": 0, "has_data": True,
                    }]},
                }},
            }, store)
        with pytest.raises(ValueError, match="unbilled rows"):
            persist_scsb({
                "card_inquiry": {"leaves": {
                    "unbilled": {"nav": {"ok": True}, "rows": [{}]},
                }},
            }, store)
    finally:
        store.close()


def test_persist_scsb_missing_balance_preserves_saved_date_and_aggregate(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("scsb")
    try:
        store.upsert_accounts([{
            "account_no": "90000000111111", "currency": "TWD",
            "type": "活儲存款", "product_type": "deposit",
            "raw_balance": 100, "raw_balance_date": "2026-01-01",
        }])
        delta = persist_scsb({"accounts": [{
            "account_no": "90000000111111", "currency": "TWD",
            "balance": None, "type_header": "活儲存款",
        }]}, store)
        row = store.conn.execute(
            "SELECT raw_balance, raw_balance_date FROM accounts WHERE account_no=?",
            ("90000000111111",),
        ).fetchone()
        metrics = store.conn.execute(
            "SELECT COUNT(*) FROM daily_metrics WHERE category='balance_latest'"
        ).fetchone()
    finally:
        store.close()

    assert row is not None and tuple(row) == (100, "2026-01-01")
    assert metrics is not None and metrics[0] == 0
    assert delta["balance_days"] == 0


def test_persist_scsb_cleans_legacy_private_metrics_and_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("scsb")
    try:
        store.put_daily_metric("overview_text_preview", {"snippet": "PRIVATE"})
        store.put_daily_metric("twd_inquiry_summary", {"account_no": "PRIVATE"})
        store.log_sync({"private": "PRIVATE"})
        persist_scsb({"accounts": [{
            "account_no": "90000000111111", "currency": "TWD",
            "balance": "0", "type_header": "活儲存款",
        }]}, store)
        categories = {
            row[0] for row in store.conn.execute(
                "SELECT category FROM daily_metrics"
            ).fetchall()
        }
        logs = store.conn.execute("SELECT summary FROM sync_log").fetchall()
    finally:
        store.close()

    assert "overview_text_preview" not in categories
    assert "twd_inquiry_summary" not in categories
    assert len(logs) == 1 and "PRIVATE" not in logs[0][0]


def test_persist_scsb_zero_balance_remains_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("scsb")
    commit = MagicMock(wraps=store.commit)
    store.commit = commit
    try:
        persist_scsb({"accounts": [{
            "account_no": "90000000111111", "currency": "TWD",
            "balance": "0", "type_header": "活儲存款",
        }]}, store)
        row = store.conn.execute(
            "SELECT twd_balance FROM balance_history ORDER BY snapshot_date DESC LIMIT 1"
        ).fetchone()
    finally:
        store.close()

    assert row is not None and row[0] == 0
    commit.assert_called_once_with()


@pytest.mark.parametrize("balance", ["NaN", "Infinity", "-Infinity"])
def test_persist_scsb_nonfinite_balance_is_incomplete(tmp_path, monkeypatch, balance):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("scsb")
    try:
        delta = persist_scsb({"accounts": [{
            "account_no": "90000000111111", "currency": "TWD",
            "balance": balance, "type_header": "活儲存款",
        }]}, store)
        account = store.conn.execute(
            "SELECT raw_balance, raw_balance_date FROM accounts WHERE account_no = ?",
            ("90000000111111",),
        ).fetchone()
        history = store.conn.execute("SELECT COUNT(*) FROM balance_history").fetchone()
    finally:
        store.close()

    assert account is not None and tuple(account) == (None, None)
    assert history is not None and history[0] == 0
    assert delta["balance_days"] == 0


def test_scsb_twd_name_only_account_select_uses_name_selector(tmp_path):
    page = _mock_twd_query_page(full_text="2026/01/01\t明細\tNT$ 1\t\tNT$ 9\t")
    side_effects = list(page.evaluate.side_effect)
    side_effects[2] = [{
        "id": "",
        "name": "accountPicker",
        "options": [{"value": "90000000123456"}],
    }]
    page.evaluate.side_effect = side_effects

    object.__new__(ScsbCrawler)._collect_twd_inquiry(page, {"90000000123456"})

    assert page.select_option.call_args.args[0] == 'select[name="accountPicker"]'


def test_scsb_select_telemetry_omits_account_values_and_labels():
    private_account = "90000000987654"

    audit = _safe_select_inventory(
        [
            {
                "id": "account",
                "name": "account",
                "options": [
                    {"value": "", "text": "請選擇查詢帳號"},
                    {"value": private_account, "text": private_account},
                ],
            }
        ]
    )

    assert audit == [{"option_count": 2}]
    assert private_account not in repr(audit)


def test_scsb_module_never_logs_account_number_or_balance() -> None:
    source = inspect.getsource(scsb_module)

    assert "value={target_val" not in source
    assert "{a['account_no']} | {a['currency']} | {a['balance']}" not in source
