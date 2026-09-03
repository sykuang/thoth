from __future__ import annotations

from copy import deepcopy
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.banks.rakuten import RakutenCrawler, _is_twd_query_request
from backend.core.base import ResponseCollector, validate_history_coverage
from backend.core.persist import persist_collected
from backend.core.store import BankStore


ACCOUNT = "81234567890123"
MONTHS = [
    "2026/09 活存明細",
    "2026/08 活存明細",
    "2026/07 活存明細",
    "2026/06 活存明細",
    "2026/05 活存明細",
    "2026/04 活存明細",
]
ENDPOINT = (
    "https://www.rakuten-bank.com.tw/ixtein/adapters/ebank/txns/"
    "channel-ctw/CTWQU0001/011"
)


@pytest.mark.parametrize("url", [
    ENDPOINT.replace(".com.tw/", ".com.tw:444/"),
    ENDPOINT.replace("https://", "https://user@"),
])
def test_rakuten_history_endpoint_requires_exact_origin(url: str) -> None:
    assert not _is_twd_query_request(SimpleNamespace(url=url, method="POST"))


def _crawler(cursor: date | None = None) -> RakutenCrawler:
    crawler = object.__new__(RakutenCrawler)
    crawler.transaction_cursors = {"twd_transactions": {}}
    if cursor is not None:
        crawler.transaction_cursors["twd_transactions"][ACCOUNT] = cursor
    return crawler


def test_rakuten_full_history_declares_six_month_twd_contract(monkeypatch) -> None:
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    crawler = _crawler(date(2027, 1, 1))

    assert RakutenCrawler.HISTORY_COVERAGE_REQUIRED is True
    assert frozenset({"twd_transactions"}) == RakutenCrawler.HISTORY_COVERAGE_DOMAINS
    assert crawler._history_plan(ACCOUNT, MONTHS, date(2026, 9, 3)) == [
        {"label": "2026/04 活存明細", "start": date(2026, 4, 1), "end": date(2026, 4, 30)},
        {"label": "2026/05 活存明細", "start": date(2026, 5, 1), "end": date(2026, 5, 31)},
        {"label": "2026/06 活存明細", "start": date(2026, 6, 1), "end": date(2026, 6, 30)},
        {"label": "2026/07 活存明細", "start": date(2026, 7, 1), "end": date(2026, 7, 31)},
        {"label": "2026/08 活存明細", "start": date(2026, 8, 1), "end": date(2026, 8, 31)},
        {"label": "2026/09 活存明細", "start": date(2026, 9, 1), "end": date(2026, 9, 3)},
    ]


def test_rakuten_incremental_uses_cursor_overlap_month_and_rejects_future(monkeypatch) -> None:
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "incremental")
    crawler = _crawler(date(2026, 8, 20))

    assert crawler._history_plan(ACCOUNT, MONTHS, date(2026, 9, 3)) == [
        {"label": "2026/08 活存明細", "start": date(2026, 8, 1), "end": date(2026, 8, 31)},
        {"label": "2026/09 活存明細", "start": date(2026, 9, 1), "end": date(2026, 9, 3)},
    ]

    with pytest.raises(RuntimeError, match="rakuten-twd-history-cursor"):
        _crawler(date(2026, 9, 4))._history_plan(ACCOUNT, MONTHS, date(2026, 9, 3))


def test_rakuten_history_month_inventory_must_be_current_and_contiguous(monkeypatch) -> None:
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    crawler = _crawler()

    with pytest.raises(RuntimeError, match="rakuten-twd-history-months"):
        crawler._history_plan(ACCOUNT, [*MONTHS[:2], *MONTHS[3:], "2026/03 活存明細"], date(2026, 9, 3))
    with pytest.raises(RuntimeError, match="rakuten-twd-history-months"):
        crawler._history_plan(ACCOUNT, [label.replace("2026/09", "2026/08") for label in MONTHS], date(2026, 9, 3))
    with pytest.raises(RuntimeError, match="rakuten-twd-history-months"):
        crawler._history_plan(ACCOUNT, [*MONTHS, "自訂區間"], date(2026, 9, 3))


def test_rakuten_account_inventory_requires_unique_options_and_selected_membership() -> None:
    assert RakutenCrawler._validated_account_options(
        f"活存總額 {ACCOUNT}", [ACCOUNT],
    ) == [(ACCOUNT, ACCOUNT)]
    with pytest.raises(RuntimeError, match="rakuten-twd-history-inventory"):
        RakutenCrawler._validated_account_options(
            f"活存總額 {ACCOUNT}", [ACCOUNT, f"帳號 {ACCOUNT}"],
        )
    with pytest.raises(RuntimeError, match="rakuten-twd-history-inventory"):
        RakutenCrawler._validated_account_options(
            f"活存總額 {ACCOUNT}", ["81234567890124"],
        )


def _history_result(*, empty: bool = False) -> dict:
    rows = [] if empty else [{
        "sysDate": "2026/09/02",
        "sysTime": "09:30:00",
        "txDesc": "利息",
        "nickNameOrAcct": None,
        "amt": "5",
        "amtSign": True,
        "balance": "105",
        "memo": "",
    }]
    return {
        "account_no": ACCOUNT,
        "accounts": [{"acctNo": ACCOUNT, "balance": "105"}],
        "txDetails": rows,
        "selected_month": "2026/09 活存明細",
        "dom": {
            "table_count": 0 if empty else 1,
            "visible_tables": 0 if empty else 1,
            "headers": [] if empty else [
                "交易時間", "交易說明 對方帳號或暱稱", "轉入", "轉出",
                "帳戶餘額", "備註", "",
            ],
            "raw_rows": len(rows),
            "no_data_count": 1 if empty else 0,
            "invalid_cells": 0,
            "pager": 0,
            "busy": 0,
            "dialogs": 0,
            "alerts": 0,
        },
        "receipt": {
            "identity": ACCOUNT,
            "start": "2026-09-01",
            "end": "2026-09-03",
            "status": "explicit_empty" if empty else "complete",
            "pages": 1,
            "rows": len(rows),
        },
        "transport": {
            "url": ENDPOINT,
            "method": "POST",
            "status": 200,
            "content_type": "application/json",
            "redirected": False,
            "main_frame": True,
            "request_count": 1,
            "response_count": 1,
        },
    }


def test_rakuten_history_result_attests_transport_dom_and_explicit_empty() -> None:
    complete = RakutenCrawler._validated_history_result(_history_result())
    empty = RakutenCrawler._validated_history_result(_history_result(empty=True))

    assert complete["status"] == "complete"
    assert complete["rows"] == 1
    assert empty["status"] == "explicit_empty"
    assert empty["rows"] == 0


@pytest.mark.parametrize("url", [
    f"https://www.rakuten-bank.com.tw:bad{ENDPOINT.split('.tw', 1)[1]}",
    ENDPOINT.replace(".com.tw/", ".com.tw:443/"),
    ENDPOINT.replace("https://", "https://user@"),
])
def test_rakuten_history_result_rejects_noncanonical_transport_url(url: str) -> None:
    result = _history_result()
    result["transport"]["url"] = url

    with pytest.raises(RuntimeError, match="rakuten-twd-history-result"):
        RakutenCrawler._validated_history_result(result)


def test_rakuten_history_result_rejects_visible_pager() -> None:
    result = _history_result()
    result["dom"]["pager"] = 1

    with pytest.raises(RuntimeError, match="rakuten-twd-history-result"):
        RakutenCrawler._validated_history_result(result)


def test_rakuten_collects_only_incremental_months_and_emits_coverage(monkeypatch) -> None:
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "incremental")
    crawler = _crawler(date(2026, 8, 20))
    selected = {"simple-dropdown2": f"帳號 {ACCOUNT}", "simple-dropdown": MONTHS[0]}
    selected_months: list[str] = []

    monkeypatch.setattr(crawler, "_selected_label", lambda _page, root: selected[root])
    monkeypatch.setattr(
        crawler,
        "_visible_labels",
        lambda _page, root: [f"帳號 {ACCOUNT}"] if root == "simple-dropdown2" else MONTHS,
    )

    def select(_page, _collector, root: str, label: str) -> dict:
        selected[root] = label
        if root == "simple-dropdown":
            selected_months.append(label)
        return dict(_history_result()["transport"])

    def scrape(_page, account_no: str) -> dict:
        result = _history_result(empty=selected["simple-dropdown"] != MONTHS[0])
        return {
            key: result[key]
            for key in ("account_no", "accounts", "txDetails", "dom")
        }

    monkeypatch.setattr(crawler, "_select_label", select)
    monkeypatch.setattr(crawler, "_scrape_twd_page", scrape)

    result = crawler._collect_attested_twd_history(
        object(), ResponseCollector("rakuten-bank.com.tw"), as_of=date(2026, 9, 3),
    )

    assert selected_months == MONTHS[1::-1]
    assert result["account_options"] == [{"identity": ACCOUNT}]
    assert len(result["twd_txn_results"]) == 2
    assert validate_history_coverage(
        result["history_coverage"],
        expected_mode="incremental",
        expected_domains=frozenset({"twd_transactions"}),
    ) == {
        "ok": True,
        "mode": "incremental",
        "domains": ["twd_transactions"],
        "identities": 1,
        "windows": 2,
        "start": "2026-08-01",
        "end": "2026-09-03",
    }


@pytest.mark.parametrize(
    "snapshot",
    [
        {"accountLabel": f"帳號 {ACCOUNT}", "balance": "0", "rows": [["bad"]], "noData": False},
        {"accountLabel": f"帳號 {ACCOUNT}", "balance": "0", "rows": [], "noData": False},
        {
            "accountLabel": f"帳號 {ACCOUNT}",
            "balance": "0",
            "rows": [["2026/09/02 09:30:00", "利息", "5", "", "105", ""]],
            "noData": True,
        },
    ],
)
def test_rakuten_scrape_rejects_parse_loss_and_ambiguous_empty(snapshot) -> None:
    class Page:
        @staticmethod
        def evaluate(_script: str) -> dict:
            value = dict(snapshot)
            rows = value.pop("rows")
            no_data = value.pop("noData")
            value["rows"] = rows
            value["dom"] = {
                "table_count": int(bool(rows)),
                "visible_tables": int(bool(rows)),
                "headers": [
                    "交易時間", "交易說明 對方帳號或暱稱", "轉入", "轉出",
                    "帳戶餘額", "備註", "",
                ] if rows else [],
                "raw_rows": len(rows),
                "no_data_count": int(no_data),
                "invalid_cells": 0,
                "pager": 0,
                "busy": 0,
                "dialogs": 0,
                "alerts": 0,
            }
            return value

    with pytest.raises(RuntimeError, match="rakuten-twd-history-dom"):
        RakutenCrawler._scrape_twd_page(Page(), ACCOUNT)


def test_rakuten_real_dom_attestation_rejects_pager() -> None:
    from patchright.sync_api import sync_playwright

    with sync_playwright() as patchright:
        if not Path(patchright.chromium.executable_path).exists():
            pytest.skip("Patchright browser binary is not installed")
        browser = patchright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(f"""
                <a href="/">首頁</a>
                <main>
                  <simple-dropdown2><a class="txt_dropdown">帳號 {ACCOUNT}</a></simple-dropdown2>
                  <div class="card-title-money">105</div>
                  <div class="card"><table class="tb_mul">
                    <thead><tr>
                      <th>交易時間</th><th>交易說明<br>對方帳號或暱稱</th>
                      <th>轉入</th><th>轉出</th><th>帳戶餘額</th><th>備註</th><th></th>
                    </tr></thead>
                    <tbody><tr>
                      <td>2026/09/02 09:30:00</td><td>利息</td><td>5</td><td></td><td>105</td><td></td>
                    </tr></tbody>
                  </table></div>
                </main>
            """)
            assert RakutenCrawler._scrape_twd_page(page, ACCOUNT)["dom"]["raw_rows"] == 1
            page.locator("main").evaluate(
                "root => root.insertAdjacentHTML('beforeend', '<button class=\"pager\">下一頁</button>')"
            )
            with pytest.raises(RuntimeError, match="rakuten-twd-history-dom"):
                RakutenCrawler._scrape_twd_page(page, ACCOUNT)
            page.locator(".pager").evaluate("el => el.remove()")
            page.locator("main").evaluate(
                "root => root.insertAdjacentHTML('beforeend', '<button id=\"page-2\" onclick=\"nextPage()\">2</button>')"
            )
            with pytest.raises(RuntimeError, match="rakuten-twd-history-dom"):
                RakutenCrawler._scrape_twd_page(page, ACCOUNT)
            page.locator("#page-2").evaluate("el => el.remove()")
            for markup in (
                '<a id="semantic-pager" rel="next" title="Next page" href="?cursor=abc"><i></i></a>',
                '<button id="semantic-pager">Prev</button>',
                '<a id="semantic-pager" rel="next" hidden></a>',
            ):
                page.locator("main").evaluate(
                    "(root, html) => root.insertAdjacentHTML('beforeend', html)", markup,
                )
                with pytest.raises(RuntimeError, match="rakuten-twd-history-dom"):
                    RakutenCrawler._scrape_twd_page(page, ACCOUNT)
                page.locator("#semantic-pager").evaluate("el => el.remove()")
            page.locator("main").evaluate(
                "root => root.insertAdjacentHTML('beforeend', '<div id=\"result-error\" class=\"alert\">載入失敗</div>')"
            )
            with pytest.raises(RuntimeError, match="rakuten-twd-history-dom"):
                RakutenCrawler._scrape_twd_page(page, ACCOUNT)
            page.locator("#result-error").evaluate("el => el.remove()")
            page.locator("tbody").evaluate(
                "body => body.insertAdjacentHTML('beforeend', '<tr hidden><td>stale</td></tr>')"
            )
            with pytest.raises(RuntimeError, match="rakuten-twd-history-dom"):
                RakutenCrawler._scrape_twd_page(page, ACCOUNT)
        finally:
            browser.close()


def test_rakuten_account_options_wait_for_stable_nonblank_inventory() -> None:
    from patchright.sync_api import sync_playwright

    with sync_playwright() as patchright:
        if not Path(patchright.chromium.executable_path).exists():
            pytest.skip("Patchright browser binary is not installed")
        browser = patchright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(f"""
                <simple-dropdown2>
                  <a class="txt_dropdown">帳號 {ACCOUNT}</a>
                  <div class="dropdown-menu"><a class="dropdown-item">{ACCOUNT}</a></div>
                </simple-dropdown2>
            """)
            page.evaluate("""() => setTimeout(() => {
                document.querySelector('.dropdown-menu').insertAdjacentHTML(
                    'beforeend', '<a class="dropdown-item">81234567890124</a>');
            }, 300)""")
            assert RakutenCrawler._visible_labels(page, "simple-dropdown2") == [
                ACCOUNT, "81234567890124",
            ]
            page.locator(".dropdown-menu").evaluate(
                "menu => menu.insertAdjacentHTML('beforeend', '<a class=\"dropdown-item\"></a>')"
            )
            with pytest.raises(RuntimeError, match="rakuten-twd-history-inventory"):
                RakutenCrawler._visible_labels(page, "simple-dropdown2")
        finally:
            browser.close()


def test_rakuten_collector_keeps_only_transport_metadata() -> None:
    page = SimpleNamespace()
    frame_obj = SimpleNamespace(page=page, url="https://www.rakuten-bank.com.tw/ebank/ctw/ctwqu0001/010")
    page.main_frame = frame_obj

    class Request:
        url = ENDPOINT
        method = "POST"
        redirected_from = None
        frame = frame_obj

        @property
        def headers(self):
            raise AssertionError("authorization must not be read")

        @property
        def post_data(self):
            raise AssertionError("encrypted request body must not be read")

    request_obj = Request()

    class Response:
        url = ENDPOINT
        status = 200
        request = request_obj
        headers = {"content-type": "application/json", "content-length": "999999999"}

        @staticmethod
        def json():
            raise AssertionError("encrypted response JSON must not be decoded")

        @staticmethod
        def body():
            raise AssertionError("encrypted response body must not be retained")

    collector = ResponseCollector("rakuten-bank.com.tw")
    collector._on_request(request_obj)
    collector._on_response(Response())

    assert collector.auth_token == ""
    assert len(collector.hits) == 1
    assert collector.hits[0].req_body is None
    assert collector.hits[0].resp_json is None
    assert collector.hits[0].body_size == 999999999
    assert collector.hits[0].raw_url == ENDPOINT
    assert collector.hits[0].request_frame_url == ""
    assert collector.hits[0].request_frame is None

    unrelated_urls = [
        "https://www.rakuten-bank.com.tw/login;jsessionid=SENSITIVE_MARKER?sid=SENSITIVE_MARKER",
        ENDPOINT.replace(".com.tw/", ".com.tw:444/"),
        ENDPOINT.replace("https://", "https://user@"),
    ]
    for unrelated_url in unrelated_urls:
        unrelated = SimpleNamespace(
            url=unrelated_url,
            method="POST",
            redirected_from=None,
            frame=frame_obj,
        )
        collector._on_request(unrelated)
        collector._on_response(SimpleNamespace(
            url=unrelated.url,
            status=200,
            request=unrelated,
            headers={"content-type": "application/json", "content-length": "10"},
        ))
    assert len(collector.hits) == 1
    assert collector._requests == {}
    assert collector._request_frames == {}


def test_rakuten_cli_does_not_write_customer_bearing_collected_backup(monkeypatch) -> None:
    from cli import cli
    from backend.core import persist as persist_module
    from backend.server import rules_repo

    class Crawler:
        HISTORY_COVERAGE_REQUIRED = True
        HISTORY_COVERAGE_DOMAINS = frozenset({"twd_transactions"})
        failed = False

        @staticmethod
        def configure_transaction_cursor(_domain, _cursor):
            pass

        @staticmethod
        def run(*, login_url, headless):
            if Crawler.failed:
                return {"error": "collect_failed"}
            return {"data": _persist_payload()}

    class Store:
        db_path = "private"

        @staticmethod
        def latest_twd_transaction_dates():
            return {}

        @staticmethod
        def latest_card_transaction_dates():
            return {}

        @staticmethod
        def stats():
            return {}

        @staticmethod
        def close():
            pass

    monkeypatch.setattr(cli, "_get_crawler", lambda _bank: (Crawler(), "https://example.com"))
    monkeypatch.setattr(cli, "BankStore", lambda _bank: Store())
    monkeypatch.setattr(rules_repo, "list_rules", lambda **_kwargs: [{}])
    monkeypatch.setattr(persist_module, "persist_collected", lambda *_args, **_kwargs: {})
    removed = []
    monkeypatch.setattr(
        cli,
        "_write_private_json",
        lambda *_args, **_kwargs: pytest.fail("Rakuten collected backup must stay disabled"),
    )
    monkeypatch.setattr(cli, "_remove_private_json", lambda path: removed.append(path.name))
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")

    assert cli.cmd_sync(SimpleNamespace(bank="rakuten", headless=True)) == 0
    assert removed == ["rakuten_collected.json"]
    removed.clear()
    Crawler.failed = True
    assert cli.cmd_sync(SimpleNamespace(bank="rakuten", headless=True)) == 1
    assert removed == ["rakuten_collected.json"]


def test_rakuten_persistence_requires_attested_history(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("rakuten", user_id=1, source_account_id=7)
    try:
        with pytest.raises(ValueError, match="requires history coverage"):
            persist_collected("rakuten", {"card_bill_facts_ok": False}, store)
    finally:
        store.close()


def _persist_payload() -> dict:
    crawler = _crawler()
    plan = crawler._history_plan(ACCOUNT, MONTHS, date(2026, 9, 3))
    results = []
    windows = []
    for window in plan:
        result = _history_result(empty=window["label"] != MONTHS[0])
        result["selected_month"] = window["label"]
        result["receipt"].update({
            "start": window["start"].isoformat(),
            "end": window["end"].isoformat(),
            "status": "complete" if result["txDetails"] else "explicit_empty",
            "rows": len(result["txDetails"]),
        })
        results.append(result)
        windows.append({
            key: result["receipt"][key]
            for key in ("identity", "start", "end", "status", "pages")
        })
    return {
        "account_options": [{"identity": ACCOUNT}],
        "twd_txn_results": results,
        "history_coverage": {
            "version": 1,
            "mode": "full",
            "as_of": "2026-09-03",
            "domains": [{
                "domain": "twd_transactions",
                "expected": [{
                    "identity": ACCOUNT,
                    "start": "2026-04-01",
                    "end": "2026-09-03",
                }],
                "windows": windows,
            }],
        },
        "card_bill_facts_ok": False,
    }


def _incremental_payload() -> dict:
    data = _persist_payload()
    data["twd_txn_results"] = data["twd_txn_results"][-2:]
    coverage = data["history_coverage"]
    coverage["mode"] = "incremental"
    domain = coverage["domains"][0]
    domain["expected"][0]["start"] = "2026-08-01"
    domain["windows"] = domain["windows"][-2:]
    return data


def test_rakuten_persistence_revalidates_history_and_advances_cursor(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    from backend.core.persist import rakuten as persist_module

    monkeypatch.setattr(persist_module, "_today", lambda: date(2026, 9, 3), raising=False)
    store = BankStore("rakuten", user_id=1, source_account_id=7)
    try:
        delta = persist_collected("rakuten", _persist_payload(), store)
        assert delta["twd_txn_new"] == 1
        assert store.latest_twd_transaction_dates() == {ACCOUNT: date(2026, 9, 3)}
    finally:
        store.close()


def test_rakuten_incremental_persistence_binds_existing_identity_cursor(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    from backend.core.persist import rakuten as persist_module

    monkeypatch.setattr(persist_module, "_today", lambda: date(2026, 9, 3), raising=False)
    store = BankStore("rakuten", user_id=1, source_account_id=7)
    store._record_transaction_cursor("twd_transactions", ACCOUNT, "2026-08-20")
    store.commit()
    try:
        assert persist_collected("rakuten", _incremental_payload(), store)["twd_txn_new"] == 1
        assert store.latest_twd_transaction_dates() == {ACCOUNT: date(2026, 9, 3)}
    finally:
        store.close()


def test_rakuten_incremental_without_cursor_requires_full_native_floor(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    from backend.core.persist import rakuten as persist_module

    monkeypatch.setattr(persist_module, "_today", lambda: date(2026, 9, 3), raising=False)
    store = BankStore("rakuten", user_id=1, source_account_id=7)
    try:
        with pytest.raises(ValueError, match="Rakuten history coverage"):
            persist_collected("rakuten", _incremental_payload(), store)
        assert all(value == 0 for value in store.stats().values())
    finally:
        store.close()


def test_rakuten_forced_full_repairs_future_cursor(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    from backend.core.persist import rakuten as persist_module

    monkeypatch.setattr(persist_module, "_today", lambda: date(2026, 9, 3), raising=False)
    store = BankStore("rakuten", user_id=1, source_account_id=7)
    store._record_transaction_cursor("twd_transactions", ACCOUNT, "2027-01-01")
    store.commit()
    try:
        persist_collected("rakuten", _persist_payload(), store)
        assert store.latest_twd_transaction_dates() == {ACCOUNT: date(2026, 9, 3)}
    finally:
        store.close()


def test_rakuten_missing_authoritative_inventory_leaves_tables_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    from backend.core.persist import rakuten as persist_module

    monkeypatch.setattr(persist_module, "_today", lambda: date(2026, 9, 3), raising=False)
    payload = _persist_payload()
    payload.pop("account_options")
    store = BankStore("rakuten", user_id=1, source_account_id=7)
    try:
        with pytest.raises(ValueError, match="Rakuten history coverage"):
            persist_collected("rakuten", payload, store)
        assert all(value == 0 for value in store.stats().values())
    finally:
        store.close()


def test_rakuten_malformed_receipt_leaves_all_tables_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    from backend.core.persist import rakuten as persist_module

    monkeypatch.setattr(persist_module, "_today", lambda: date(2026, 9, 3), raising=False)
    payload = _persist_payload()
    payload["twd_txn_results"][0]["transport"]["request_count"] = True
    store = BankStore("rakuten", user_id=1, source_account_id=7)
    try:
        with pytest.raises(ValueError, match="Rakuten history coverage"):
            persist_collected("rakuten", payload, store)
        assert all(value == 0 for value in store.stats().values())
        assert store.latest_twd_transaction_dates() == {}
    finally:
        store.close()


def test_rakuten_outer_transaction_rolls_back_when_cursor_write_fails(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    from backend.core.persist import rakuten as persist_module

    monkeypatch.setattr(persist_module, "_today", lambda: date(2026, 9, 3), raising=False)
    store = BankStore("rakuten", user_id=1, source_account_id=7)
    monkeypatch.setattr(
        store,
        "record_history_coverage_cursors",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cursor failed")),
    )
    try:
        with pytest.raises(RuntimeError, match="cursor failed"):
            persist_collected("rakuten", deepcopy(_persist_payload()), store)
        assert all(value == 0 for value in store.stats().values())
        assert store.latest_twd_transaction_dates() == {}
    finally:
        store.close()
