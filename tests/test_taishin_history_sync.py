from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from backend.banks.taishin import TaishinCrawler
from backend.core.base import (
    ApiHit,
    BankCollectResult,
    ResponseCollector,
    validate_history_coverage,
)
from backend.core.persist import persist_collected
from backend.core.store import BankStore


ACCOUNT = "01234567890123"


def _crawler() -> TaishinCrawler:
    crawler = object.__new__(TaishinCrawler)
    crawler.transaction_cursors = {"twd_transactions": {}}
    return crawler


def test_taishin_fixture_derives_dates_from_injected_as_of() -> None:
    from tests.taishin_fixtures import with_taishin_history

    payload = with_taishin_history({}, as_of=date(2026, 9, 3))
    expected = payload["history_coverage"]["domains"][0]["expected"][0]

    assert expected["start"] == "2025-09-03"
    assert expected["end"] == "2026-09-03"


def test_taishin_collector_owns_actual_com_tw_host() -> None:
    assert _crawler()._host_filter() == "taishinbank.com.tw"


def test_taishin_browser_uses_taipei_clock_for_native_date_presets() -> None:
    kwargs = _crawler()._build_fetch_kwargs()
    try:
        assert kwargs["timezone_id"] == "Asia/Taipei"
    finally:
        for cleanup in kwargs["__cleanups__"]:
            cleanup()


def test_taishin_opts_into_twd_history_only() -> None:
    assert TaishinCrawler.HISTORY_COVERAGE_REQUIRED is True
    assert frozenset({"twd_transactions"}) == TaishinCrawler.HISTORY_COVERAGE_DOMAINS


def test_taishin_full_history_uses_native_rolling_twelve_months(monkeypatch) -> None:
    crawler = _crawler()
    crawler.transaction_cursors["twd_transactions"]["01234567890123"] = date(2026, 8, 20)
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")

    assert crawler._history_window("01234567890123", date(2026, 9, 2)) == {
        "period": "12_months",
        "start": date(2025, 9, 2),
        "end": date(2026, 9, 2),
    }


def test_taishin_full_history_ignores_future_incremental_cursor(monkeypatch) -> None:
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    crawler = _crawler()
    crawler.transaction_cursors["twd_transactions"][ACCOUNT] = date(2027, 1, 1)

    assert crawler._history_window(ACCOUNT, date(2026, 9, 2))["period"] == "12_months"


def test_taishin_incremental_uses_smallest_native_period_covering_cursor_overlap(monkeypatch) -> None:
    crawler = _crawler()
    crawler.transaction_cursors["twd_transactions"]["01234567890123"] = date(2026, 8, 20)
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "incremental")

    assert crawler._history_window("01234567890123", date(2026, 9, 2)) == {
        "period": "1_months",
        "start": date(2026, 8, 2),
        "end": date(2026, 9, 2),
    }


def _form_snapshot() -> dict:
    periods = [
        ("7天", "7_days"), ("14天", "14_days"),
        ("1個月", "1_months"), ("2個月", "2_months"),
        ("3個月", "3_months"), ("6個月", "6_months"),
        ("12個月", "12_months"), ("自訂一年內期間", "inYear"),
        ("申請查詢逾一年以上", "overYear"),
    ]
    return {
        "query_buttons": 1,
        "query_button": 0,
        "selects": [
            {"index": 0, "options": [{"index": 0, "text": "繁體中文", "value": "tw"}]},
            {"index": 1, "options": [
                {"index": 0, "text": "-- 請選擇查詢帳號 --", "value": ""},
                {"index": 1, "text": "012-34-567890-1-23 Richart", "value": ACCOUNT},
            ]},
            {"index": 2, "options": [
                {"index": 0, "text": "-- 請選擇查詢期間 --", "value": ""},
                *[
                    {"index": index, "text": text, "value": value}
                    for index, (text, value) in enumerate(periods, start=1)
                ],
            ]},
            {"index": 3, "options": [
                {"index": 0, "text": "由新到舊", "value": "forward"},
                {"index": 1, "text": "由舊到新", "value": "reverse"},
            ]},
        ],
    }


def test_taishin_form_snapshot_returns_absolute_visible_control_indexes() -> None:
    from patchright.sync_api import sync_playwright

    manager = sync_playwright()
    playwright = manager.start()
    if not Path(playwright.chromium.executable_path).exists():
        manager.__exit__(None, None, None)
        pytest.skip("Patchright browser binary is not installed")
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    try:
        page.set_content("<main></main>")
        page.evaluate("""snapshot => {
            for (const opacity of ['0', '1']) {
                const root = document.createElement('section');
                root.style.opacity = opacity;
                for (const source of snapshot.selects.slice(1)) {
                    const select = document.createElement('select');
                    for (const item of source.options) {
                        const option = document.createElement('option');
                        option.value = item.value;
                        option.textContent = item.text;
                        select.appendChild(option);
                    }
                    root.appendChild(select);
                }
                const query = document.createElement('input');
                query.setAttribute('value', '查詢');
                root.appendChild(query);
                document.body.appendChild(root);
            }
        }""", _form_snapshot())
        form = TaishinCrawler._validate_history_form(
            TaishinCrawler._history_form_snapshot(page.main_frame),
        )
    finally:
        browser.close()
        manager.__exit__(None, None, None)

    assert (
        form["account_select"], form["period_select"],
        form["sort_select"], form["query_button"],
    ) == (3, 4, 5, 1)


def test_taishin_inventory_accepts_one_semantic_empty_account_select() -> None:
    snapshot = _form_snapshot()
    snapshot["selects"][1]["options"] = snapshot["selects"][1]["options"][:1]

    assert TaishinCrawler._validate_history_form(snapshot)["accounts"] == []


def test_taishin_inventory_ignores_numeric_nickname_suffix() -> None:
    snapshot = _form_snapshot()
    snapshot["selects"][1]["options"][1]["text"] = "012-34-567890-1-23 旅行2026"

    assert TaishinCrawler._validate_history_form(snapshot)["accounts"][0]["identity"] == ACCOUNT


def test_taishin_empty_inventory_waits_full_window_for_late_accounts(monkeypatch) -> None:
    frame = SimpleNamespace(evaluate=lambda *_args: None)
    page = SimpleNamespace(wait_for_timeout=lambda _milliseconds: None)
    calls = 0

    def next_snapshot(_frame):
        nonlocal calls
        calls += 1
        snapshot = _form_snapshot()
        if calls <= 3:
            snapshot["selects"][1]["options"] = snapshot["selects"][1]["options"][:1]
        return snapshot

    crawler = _crawler()
    monkeypatch.setattr(crawler, "_history_frame", lambda _page: frame)
    monkeypatch.setattr(crawler, "_history_form_snapshot", next_snapshot)
    monkeypatch.setattr(crawler, "_history_result_snapshot", lambda _frame: {
        "route_bound": True,
        "busy_count": 0,
        "dialog_count": 0,
        "error_count": 0,
        "table_count": 0,
        "no_result_count": 0,
        "more_button_count": 0,
        "no_more_count": 0,
        "pager_count": 0,
    })
    monkeypatch.setattr(
        crawler, "_history_window",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("populated-observed")),
    )
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")

    with pytest.raises(RuntimeError, match="populated-observed"):
        crawler._collect_attested_twd_history(
            page, ResponseCollector("taishinbank.com.tw"), as_of=date(2026, 9, 2),
        )
    assert calls >= 4


def test_taishin_empty_inventory_rejects_route_error(monkeypatch) -> None:
    snapshot = _form_snapshot()
    snapshot["selects"][1]["options"] = snapshot["selects"][1]["options"][:1]
    frame = SimpleNamespace(evaluate=lambda *_args: None)
    page = SimpleNamespace(wait_for_timeout=lambda _milliseconds: None)
    crawler = _crawler()
    monkeypatch.setattr(crawler, "_history_frame", lambda _page: frame)
    monkeypatch.setattr(crawler, "_history_form_snapshot", lambda _frame: snapshot)
    monkeypatch.setattr(crawler, "_history_result_snapshot", lambda _frame: {
        "route_bound": True,
        "busy_count": 0,
        "dialog_count": 0,
        "error_count": 1,
        "table_count": 0,
        "no_result_count": 0,
        "more_button_count": 0,
        "no_more_count": 0,
        "pager_count": 0,
    })
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")

    with pytest.raises(RuntimeError, match="taishin-twd-history-empty-inventory"):
        crawler._collect_attested_twd_history(
            page, ResponseCollector("taishinbank.com.tw"), as_of=date(2026, 9, 2),
        )


def test_taishin_empty_account_inventory_emits_and_persists_explicit_coverage(
    tmp_path, monkeypatch,
) -> None:
    as_of = datetime.now(ZoneInfo("Asia/Taipei")).date()
    snapshot = _form_snapshot()
    snapshot["selects"][1]["options"] = snapshot["selects"][1]["options"][:1]
    frame = SimpleNamespace(evaluate=lambda *_args: None)
    page = SimpleNamespace(wait_for_timeout=lambda _milliseconds: None)
    crawler = _crawler()
    monkeypatch.setattr(crawler, "_history_frame", lambda _page: frame)
    monkeypatch.setattr(crawler, "_history_form_snapshot", lambda _frame: snapshot)
    monkeypatch.setattr(crawler, "_history_result_snapshot", lambda _frame: {
        "route_bound": True,
        "busy_count": 0,
        "dialog_count": 0,
        "error_count": 0,
        "table_count": 0,
        "no_result_count": 0,
        "more_button_count": 0,
        "no_more_count": 0,
        "pager_count": 0,
    })
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")

    collected = crawler._collect_attested_twd_history(
        page, ResponseCollector("taishinbank.com.tw"), as_of=as_of,
    )
    domain = collected["history_coverage"]["domains"][0]

    assert collected["twd_txn_results"] == []
    assert domain == {
        "domain": "twd_transactions",
        "expected": [],
        "windows": [],
        "empty_window": {
            "start": TaishinCrawler._subtract_months(as_of, 12).isoformat(),
            "end": as_of.isoformat(),
            "status": "explicit_empty",
            "pages": 1,
        },
    }
    serialized = BankCollectResult(**collected).to_dict()
    assert serialized["twd_txn_results"] == []

    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("taishin", user_id=1, source_account_id=7)
    store._record_transaction_cursor("twd_transactions", ACCOUNT, as_of.isoformat())
    store.commit()
    try:
        persist_collected("taishin", serialized, store)
        assert store.latest_twd_transaction_dates() == {}
    finally:
        store.close()


def test_taishin_inventory_finds_semantic_controls_and_canonical_account() -> None:
    assert TaishinCrawler._validate_history_form(_form_snapshot()) == {
        "account_select": 1,
        "period_select": 2,
        "sort_select": 3,
        "query_button": 0,
        "accounts": [{"index": 1, "identity": ACCOUNT, "value": ACCOUNT}],
    }


def test_taishin_inventory_recheck_rejects_late_account() -> None:
    snapshot = _form_snapshot()
    snapshot["selects"][1]["options"].append({
        "index": 2,
        "text": "987-65-432109-8-76 Second",
        "value": "98765432109876",
    })

    with pytest.raises(RuntimeError, match="taishin-twd-history-inventory"):
        TaishinCrawler._require_history_inventory(
            snapshot,
            [(ACCOUNT, ACCOUNT)],
        )


def _history_hit() -> ApiHit:
    return ApiHit(
        url="https://my.taishinbank.com.tw/TIBNetBank/svc/web1/rb0102/query",
        raw_url="https://my.taishinbank.com.tw/TIBNetBank/svc/web1/rb0102/query",
        method="POST",
        status=200,
        req_body={"account": ACCOUNT, "start": "20250902", "end": "20260902"},
        resp_json={
            "RESULT": "NORMAL",
            "OUTPUTDATA": {
                "userList": [{
                    "sysdate": "20260901 12345600",
                    "dateNew": "20260901",
                    "memo": "利息",
                    "txnamt": "5",
                    "txnamtOut": "-",
                    "txnamtIn": "5",
                    "newbal": "105",
                    "message": "測試備註",
                }],
            },
        },
        content_type="application/json;charset=utf-8",
        body_size=800,
        request_sequence=5,
        request_frame_url=(
            "https://my.taishinbank.com.tw/TIBNetBank/svc/rwd/index.html#/RB0102/0100"
        ),
    )


def test_taishin_filters_sensitive_and_history_api_payloads_from_raw_dump() -> None:
    history = _history_hit()
    login = deepcopy(history)
    login.url = login.raw_url = "https://my.taishinbank.com.tw/TIBNetBank/svc/web1/login"
    login.resp_json = {"CUSTNO": "A123456789"}
    realtime = deepcopy(history)
    realtime.url = realtime.raw_url = (
        "https://my.taishinbank.com.tw/TIBNetBank/svc/web4/rb0708rwd/qryRealTime"
    )
    realtime.resp_json = {"value": {"crlimit": "5", "national_id": "A123456789"}}
    profile = deepcopy(history)
    profile.url = profile.raw_url = (
        "https://my.taishinbank.com.tw/customer/profile/qryRealTime"
    )
    profile.resp_json = {"national_id": "A123456789"}
    card_limit = deepcopy(history)
    card_limit.url = card_limit.raw_url = (
        "https://my.taishinbank.com.tw/TIBNetBank/svc/web4/rb0708rwd/doXTPA"
    )
    card_limit.resp_json = {"value": {"001": {
        "OUT-CRLIMIT-PERM": "000000100",
        "OUT-AVAIL-CREDIT": " 000000080",
        "national_id": "A123456789",
    }, "002": {
        "OUT-CRLIMIT-PERM": "200",
        "OUT-AVAIL-CREDIT": "160",
    }, "A123456789": {
        "OUT-CRLIMIT-PERM": "100",
        "OUT-AVAIL-CREDIT": "80",
    }}}
    overview = deepcopy(history)
    overview.url = overview.raw_url = (
        "https://my.taishinbank.com.tw/TIBNetBank/svc/web1/rb0100/query"
    )
    overview.resp_json = {"RESULT": "NORMAL", "OUTPUTDATA": {"SavingAccount": [{
        "accountNo": ACCOUNT,
        "balance": "1,000",
        "accountTypeName": "活期存款",
        "userdefineName": "Richart",
        "national_id": "A123456789",
    }]}}
    points = deepcopy(history)
    points.url = points.raw_url = (
        "https://my.taishinbank.com.tw/TIBNetBank/svc/web/common/qryTaishinPoint"
    )
    points.resp_json = {"value": {
        "balance": "7", "TSPOINT_balance": "8", "national_id": "A123456789",
    }}

    assert TaishinCrawler._non_sensitive_api_responses([
        history, login, realtime, profile, card_limit, overview, points,
    ]) == {
        "qryRealTime": {"value": {"crlimit": 5}},
        "doXTPA": {"value": {"001": {
            "OUT-CRLIMIT-PERM": 100,
            "OUT-AVAIL-CREDIT": 80,
        }}},
        "query": {"RESULT": "NORMAL", "OUTPUTDATA": {"SavingAccount": [{
            "accountNo": ACCOUNT,
            "balance": 1000,
            "accountTypeName": "活期存款",
            "userdefineName": "Richart",
        }]}},
        "qryTaishinPoint": {"value": {"balance": 7, "TSPOINT_balance": 8}},
    }


def test_taishin_projected_overview_and_points_reach_persistence(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    overview = _history_hit()
    overview.url = overview.raw_url = (
        "https://my.taishinbank.com.tw/TIBNetBank/svc/web1/rb0100/query"
    )
    overview.resp_json = {"RESULT": "NORMAL", "OUTPUTDATA": {"SavingAccount": [{
        "accountNo": ACCOUNT,
        "balance": "1,000",
        "accountTypeName": "活期存款",
        "userdefineName": "Richart",
    }]}}
    points = deepcopy(overview)
    points.url = points.raw_url = (
        "https://my.taishinbank.com.tw/TIBNetBank/svc/web/common/qryTaishinPoint"
    )
    points.resp_json = {"value": {"balance": "7", "TSPOINT_balance": "8"}}
    payload = _attested_payload()
    payload["api_responses"] = TaishinCrawler._non_sensitive_api_responses([
        overview, points,
    ])
    store = BankStore("taishin", user_id=1, source_account_id=7)
    try:
        persist_collected("taishin", payload, store)
        account_count = store.conn.execute(
            "SELECT COUNT(*) FROM accounts WHERE account_no = ?", (ACCOUNT,),
        ).fetchone()[0]
        categories = {
            row[0] for row in store.conn.execute(
                "SELECT category FROM daily_metrics WHERE category IN (?, ?)",
                ("balance_latest", "taishin_points"),
            ).fetchall()
        }
    finally:
        store.close()

    assert account_count == 1
    assert categories == {"balance_latest", "taishin_points"}


def test_taishin_history_hit_binds_exact_request_and_response() -> None:
    result = TaishinCrawler._validate_history_hit(
        _history_hit(),
        identity=ACCOUNT,
        start=date(2025, 9, 2),
        end=date(2026, 9, 2),
        boundary=4,
    )

    assert result["row_count"] == 1
    assert result["rows"][0]["desc"] == "利息"
    assert result["rows"][0]["income"] == 5


def test_taishin_history_hit_collapses_rendered_whitespace() -> None:
    hit = _history_hit()
    row = hit.resp_json["OUTPUTDATA"]["userList"][0]
    row["memo"] = "ATM  提款\n測試"
    row["message"] = "跨行\t提款"

    result = TaishinCrawler._validate_history_hit(
        hit,
        identity=ACCOUNT,
        start=date(2025, 9, 2),
        end=date(2026, 9, 2),
        boundary=4,
    )

    assert result["rows"][0]["desc"] == "ATM 提款 測試"
    assert result["rows"][0]["memo"] == "跨行 提款"


def test_taishin_history_hit_matches_bank_left_padded_time_format() -> None:
    hit = _history_hit()
    hit.resp_json["OUTPUTDATA"]["userList"][0]["sysdate"] = "20260901 123456"

    result = TaishinCrawler._validate_history_hit(
        hit,
        identity=ACCOUNT,
        start=date(2025, 9, 2),
        end=date(2026, 9, 2),
        boundary=4,
    )

    assert result["rows"][0]["datetime"] == "2026-09-01 00:12:34"


def test_taishin_history_hit_rejects_disagreeing_normalized_url() -> None:
    hit = _history_hit()
    hit.url = "https://evil.example/TIBNetBank/svc/web1/rb0102/query"

    with pytest.raises(RuntimeError, match="taishin-twd-history-response"):
        TaishinCrawler._validate_history_hit(
            hit,
            identity=ACCOUNT,
            start=date(2025, 9, 2),
            end=date(2026, 9, 2),
            boundary=4,
        )


def test_taishin_history_hit_rejects_main_frame_transport() -> None:
    hit = _history_hit()
    hit.main_frame_request = True

    with pytest.raises(RuntimeError, match="taishin-twd-history-response"):
        TaishinCrawler._validate_history_hit(
            hit,
            identity=ACCOUNT,
            start=date(2025, 9, 2),
            end=date(2026, 9, 2),
            boundary=4,
        )


def test_taishin_history_hit_rejects_unrelated_child_frame() -> None:
    hit = _history_hit()
    hit.request_frame_url = (
        "https://my.taishinbank.com.tw/TIBNetBank/svc/rwd/index.html#/RB0708/0100"
    )

    with pytest.raises(RuntimeError, match="taishin-twd-history-response"):
        TaishinCrawler._validate_history_hit(
            hit,
            identity=ACCOUNT,
            start=date(2025, 9, 2),
            end=date(2026, 9, 2),
            boundary=4,
        )


def test_taishin_history_hit_accepts_origin_guarded_expected_frame() -> None:
    from backend.core.base import _OriginGuardProxy

    raw_frame = object()
    guarded_frame = _OriginGuardProxy(raw_frame, lambda: None)
    hit = _history_hit()
    hit.request_frame = raw_frame

    result = TaishinCrawler._validate_history_hit(
        hit,
        identity=ACCOUNT,
        start=date(2025, 9, 2),
        end=date(2026, 9, 2),
        boundary=4,
        expected_frame=guarded_frame,
    )

    assert result["row_count"] == 1


def test_taishin_history_hit_rejects_replaced_same_url_frame() -> None:
    hit = _history_hit()
    hit.request_frame = object()

    with pytest.raises(RuntimeError, match="taishin-twd-history-response"):
        TaishinCrawler._validate_history_hit(
            hit,
            identity=ACCOUNT,
            start=date(2025, 9, 2),
            end=date(2026, 9, 2),
            boundary=4,
            expected_frame=object(),
        )


def test_taishin_history_hit_rejects_jsonp_mime() -> None:
    hit = _history_hit()
    hit.content_type = "application/jsonp"

    with pytest.raises(RuntimeError, match="taishin-twd-history-response"):
        TaishinCrawler._validate_history_hit(
            hit,
            identity=ACCOUNT,
            start=date(2025, 9, 2),
            end=date(2026, 9, 2),
            boundary=4,
        )


def test_taishin_history_hit_rejects_oversize_row_count() -> None:
    hit = _history_hit()
    hit.resp_json["OUTPUTDATA"]["userList"] *= 10_001

    with pytest.raises(RuntimeError, match="taishin-twd-history-response"):
        TaishinCrawler._validate_history_hit(
            hit,
            identity=ACCOUNT,
            start=date(2025, 9, 2),
            end=date(2026, 9, 2),
            boundary=4,
        )


def test_taishin_history_hit_rejects_wrong_scalar_types_fail_closed() -> None:
    hit = _history_hit()
    hit.resp_json["OUTPUTDATA"]["userList"][0]["sysdate"] = 20260901

    with pytest.raises(RuntimeError, match="taishin-twd-history-response"):
        TaishinCrawler._validate_history_hit(
            hit,
            identity=ACCOUNT,
            start=date(2025, 9, 2),
            end=date(2026, 9, 2),
            boundary=4,
        )


def test_taishin_history_hit_rejects_malformed_direction_amount() -> None:
    hit = _history_hit()
    hit.resp_json["OUTPUTDATA"]["userList"][0]["txnamtIn"] = "garbage"

    with pytest.raises(RuntimeError, match="taishin-twd-history-response"):
        TaishinCrawler._validate_history_hit(
            hit,
            identity=ACCOUNT,
            start=date(2025, 9, 2),
            end=date(2026, 9, 2),
            boundary=4,
        )


def test_taishin_history_hit_rejects_nonzero_amount_without_direction() -> None:
    hit = _history_hit()
    row = hit.resp_json["OUTPUTDATA"]["userList"][0]
    row["txnamtIn"] = row["txnamtOut"] = "-"

    with pytest.raises(RuntimeError, match="taishin-twd-history-response"):
        TaishinCrawler._validate_history_hit(
            hit,
            identity=ACCOUNT,
            start=date(2025, 9, 2),
            end=date(2026, 9, 2),
            boundary=4,
        )


def test_taishin_history_hit_rejects_negative_direction_amount() -> None:
    hit = _history_hit()
    hit.resp_json["OUTPUTDATA"]["userList"][0]["txnamtIn"] = "-5"

    with pytest.raises(RuntimeError, match="taishin-twd-history-response"):
        TaishinCrawler._validate_history_hit(
            hit,
            identity=ACCOUNT,
            start=date(2025, 9, 2),
            end=date(2026, 9, 2),
            boundary=4,
        )


def test_taishin_response_collector_discards_nonallowlisted_json_before_decode() -> None:
    page = SimpleNamespace(main_frame=None)
    request = SimpleNamespace(
        url="https://my.taishinbank.com.tw/TIBNetBank/svc/web1/login",
        method="POST",
        headers={"authorization": "Bearer PRIVATE"},
        post_data="{}",
        frame=SimpleNamespace(page=page),
        redirected_from=None,
    )
    decoded = []

    class Response:
        url = request.url
        status = 200
        headers = {"content-type": "application/json"}

        def __init__(self) -> None:
            self.request = request

        def json(self) -> dict:
            decoded.append(True)
            return {"padding": "x" * 6_000_000}

    collector = ResponseCollector("taishinbank.com.tw")
    collector._on_request(request)
    collector._on_response(Response())

    assert decoded == []
    assert collector.hits == []
    assert collector._auth_requests == {}


def test_taishin_response_collector_measures_actual_body_before_json_parse() -> None:
    body = json.dumps({"padding": "x" * 5_000_001}).encode()
    page = SimpleNamespace(main_frame=None)
    request = SimpleNamespace(
        url="https://my.taishinbank.com.tw/TIBNetBank/svc/web1/rb0102/query",
        method="POST",
        headers={},
        post_data=json.dumps({"account": ACCOUNT, "start": "20250902", "end": "20260902"}),
        frame=SimpleNamespace(page=page),
        redirected_from=None,
    )

    class Response:
        url = request.url
        status = 200
        headers = {
            "content-type": "application/json",
            "content-length": "1",
            "content-encoding": "",
        }

        def __init__(self) -> None:
            self.request = request

        def body(self) -> bytes:
            return body

        def json(self) -> dict:
            return json.loads(body)

    collector = ResponseCollector("taishinbank.com.tw")
    collector._on_request(request)
    collector._on_response(Response())

    assert len(collector.hits) == 1
    assert collector.hits[0].body_size == len(body)
    assert collector.hits[0].resp_json is None


def test_taishin_projected_api_measures_actual_body_before_json_parse() -> None:
    body = json.dumps({"padding": "x" * 5_000_001}).encode()
    page = SimpleNamespace(main_frame=None)
    request = SimpleNamespace(
        url="https://my.taishinbank.com.tw/TIBNetBank/svc/web4/rb0708rwd/qryRealTime",
        method="POST",
        headers={},
        post_data="{}",
        frame=SimpleNamespace(page=page, url="https://my.taishinbank.com.tw/TIBNetBank/svc/rwd/"),
        redirected_from=None,
    )

    class Response:
        url = request.url
        status = 200
        headers = {
            "content-type": "application/json",
            "content-length": "1",
            "content-encoding": "",
        }

        def __init__(self) -> None:
            self.request = request

        def body(self) -> bytes:
            return body

        def json(self) -> dict:
            return json.loads(body)

    collector = ResponseCollector("taishinbank.com.tw")
    collector._on_request(request)
    collector._on_response(Response())

    assert len(collector.hits) == 1
    assert collector.hits[0].body_size == len(body)
    assert collector.hits[0].resp_json is None


def test_taishin_response_collector_caps_aggregate_decoded_json() -> None:
    body = json.dumps({"padding": "x" * 899_900}).encode()
    collector = ResponseCollector("taishinbank.com.tw")

    for _ in range(6):
        page = SimpleNamespace(main_frame=None)
        request = SimpleNamespace(
            url="https://my.taishinbank.com.tw/TIBNetBank/svc/web1/rb0100/query",
            method="POST",
            headers={},
            post_data="{}",
            frame=SimpleNamespace(page=page),
            redirected_from=None,
        )
        response = SimpleNamespace(
            url=request.url,
            status=200,
            headers={
                "content-type": "application/json",
                "content-length": str(len(body)),
                "content-encoding": "",
            },
            request=request,
            body=lambda: body,
        )
        collector._on_request(request)
        collector._on_response(response)

    retained = sum(
        hit.body_size or 0 for hit in collector.hits if hit.resp_json is not None
    )
    assert retained <= 5_000_000


def test_taishin_history_enforces_aggregate_response_budget() -> None:
    assert TaishinCrawler._add_history_response_bytes(0, 5_000_000) == 5_000_000
    with pytest.raises(RuntimeError, match="taishin-twd-history-response-budget"):
        TaishinCrawler._add_history_response_bytes(5_000_000, 1)


def _result_snapshot() -> dict:
    return {
        "evidence_fresh": True,
        "mutation_count": 1,
        "quiet_ms": 2500,
        "route_bound": True,
        "result_scope_bound": True,
        "selected_identity": ACCOUNT,
        "selected_period": "12_months",
        "selected_sort": "forward",
        "busy_count": 0,
        "dialog_count": 0,
        "error_count": 0,
        "table_count": 1,
        "headers": ["交易日", "帳務日", "摘要", "金額", "餘額", "備註", ""],
        "rows": [[
            "2026/09/01 12:34:56", "2026/09/01", "利息", "5", "105", "測試備註", "未設定",
        ]],
        "total_count": 1,
        "more_button_count": 0,
        "no_more_count": 1,
        "pager_count": 0,
        "no_result_count": 0,
    }


def test_taishin_snapshot_counts_hidden_stale_load_more_control() -> None:
    from patchright.sync_api import sync_playwright

    manager = sync_playwright()
    playwright = manager.start()
    if not Path(playwright.chromium.executable_path).exists():
        manager.__exit__(None, None, None)
        pytest.skip("Patchright browser binary is not installed")
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    try:
        page.set_content("""
            <table id="savingAccountTransactionTable"><tbody><tr><td>x</td></tr></tbody></table>
            <div class="_table_more">
              <button class="_table_more__btn" style="display:none">看更多資料</button>
              <span class="_table_more__nomore">沒有更多資料了</span>
            </div>
        """)
        snapshot = TaishinCrawler._history_result_snapshot(page.main_frame)
    finally:
        browser.close()
        manager.__exit__(None, None, None)

    assert snapshot["more_button_count"] == 1


def test_taishin_snapshot_rejects_prefix_only_history_route() -> None:
    from patchright.sync_api import sync_playwright

    manager = sync_playwright()
    playwright = manager.start()
    if not Path(playwright.chromium.executable_path).exists():
        manager.__exit__(None, None, None)
        pytest.skip("Patchright browser binary is not installed")
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    try:
        page.set_content("<main>history</main>")
        page.evaluate("location.hash = '#/RB0102/0100evil'")
        snapshot = TaishinCrawler._history_result_snapshot(page.main_frame)
    finally:
        browser.close()
        manager.__exit__(None, None, None)

    assert snapshot["route_bound"] is False


def test_taishin_snapshot_ignores_unrelated_numeric_controls_outside_result_root() -> None:
    from patchright.sync_api import sync_playwright

    manager = sync_playwright()
    playwright = manager.start()
    if not Path(playwright.chromium.executable_path).exists():
        manager.__exit__(None, None, None)
        pytest.skip("Patchright browser binary is not installed")
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    try:
        page.set_content("""
            <section class="_section_inquiry-result">
              <table id="savingAccountTransactionTable"><tbody><tr><td>x</td></tr></tbody></table>
              <div class="_table_more"><div class="_table_more__nomore">沒有更多資料了</div></div>
            </section>
            <aside><button>1</button></aside>
        """)
        snapshot = TaishinCrawler._history_result_snapshot(page.main_frame)
    finally:
        browser.close()
        manager.__exit__(None, None, None)

    assert snapshot["pager_count"] == 0


def test_taishin_snapshot_detects_structural_alert_and_numeric_next_control() -> None:
    from patchright.sync_api import sync_playwright

    manager = sync_playwright()
    playwright = manager.start()
    if not Path(playwright.chromium.executable_path).exists():
        manager.__exit__(None, None, None)
        pytest.skip("Patchright browser binary is not installed")
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    try:
        page.set_content("""
            <section class="_section_content">
              <div class="result">
                <table id="savingAccountTransactionTable"><tbody><tr><td>x</td></tr></tbody></table>
                <div class="_table_more"></div>
                <button onclick="nextPage()">2</button>
              </div>
              <div role="alert">資料讀取失敗</div>
            </section>
        """)
        snapshot = TaishinCrawler._history_result_snapshot(page.main_frame)
    finally:
        browser.close()
        manager.__exit__(None, None, None)

    assert snapshot["error_count"] >= 1
    assert snapshot["pager_count"] >= 1


def test_taishin_network_quiescence_rejects_delayed_duplicate(monkeypatch) -> None:
    frame = object()
    hit = _history_hit()
    collector = SimpleNamespace(
        request_sequence=5,
        hits=[hit],
        issued_count=lambda _endpoint: 1,
    )

    class Page:
        waits = 0

        def wait_for_timeout(self, _milliseconds: int) -> None:
            self.waits += 1
            if self.waits == 4:
                collector.request_sequence = 6
                duplicate = deepcopy(hit)
                duplicate.request_sequence = 6
                collector.hits.append(duplicate)

    crawler = _crawler()
    monkeypatch.setattr(crawler, "_history_frame", lambda _page: frame)

    with pytest.raises(RuntimeError, match="taishin-twd-history-response"):
        crawler._require_history_network_quiescence(
            Page(), collector, submitted_frame=frame,
            boundary=4, request_sequence=5, issued_count=1,
        )


def test_taishin_snapshot_preserves_raw_cell_size_for_python_validation() -> None:
    from patchright.sync_api import sync_playwright

    manager = sync_playwright()
    playwright = manager.start()
    if not Path(playwright.chromium.executable_path).exists():
        manager.__exit__(None, None, None)
        pytest.skip("Patchright browser binary is not installed")
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    try:
        oversized = "x" + (" " * 1001) + "y"
        page.set_content(f"""
            <section class="_section_inquiry-result">
              <table id="savingAccountTransactionTable"><tbody><tr>
                <td>2026/09/01 12:34:56</td><td>2026/09/01</td><td>{oversized}</td>
                <td>5</td><td>105</td><td>備註</td><td></td>
              </tr></tbody></table>
            </section>
        """)
        row = TaishinCrawler._history_result_snapshot(page.main_frame)["rows"][0]
    finally:
        browser.close()
        manager.__exit__(None, None, None)

    with pytest.raises(RuntimeError, match="taishin-twd-history-dom"):
        TaishinCrawler._normalize_history_cells(
            row, identity=ACCOUNT, start=date(2025, 9, 2), end=date(2026, 9, 2),
        )


def test_taishin_snapshot_rejects_opacity_zero_result_ancestors() -> None:
    from patchright.sync_api import sync_playwright

    manager = sync_playwright()
    playwright = manager.start()
    if not Path(playwright.chromium.executable_path).exists():
        manager.__exit__(None, None, None)
        pytest.skip("Patchright browser binary is not installed")
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    try:
        page.set_content("""
            <div style="opacity: 0">
              <table id="savingAccountTransactionTable"><tbody><tr><td>x</td></tr></tbody></table>
              <div class="_table_more__nomore">沒有更多資料了</div>
            </div>
        """)
        snapshot = TaishinCrawler._history_result_snapshot(page.main_frame)
    finally:
        browser.close()
        manager.__exit__(None, None, None)

    assert snapshot["table_count"] == 0
    assert snapshot["no_more_count"] == 0


def test_taishin_result_snapshot_binds_rows_and_terminal_table() -> None:
    assert TaishinCrawler._validate_history_snapshot(
        _result_snapshot(),
        identity=ACCOUNT,
        period="12_months",
        start=date(2025, 9, 2),
        end=date(2026, 9, 2),
        api_row_count=1,
    ) == {
        "status": "complete",
        "rows": [{
            "account_no": ACCOUNT,
            "datetime": "2026-09-01 12:34:56",
            "account_date": "2026-09-01",
            "desc": "利息",
            "expend": None,
            "income": 5,
            "balance": 105,
            "counterparty_bank": None,
            "counterparty_acct": "測試備註",
            "memo": "測試備註",
        }],
    }


def test_taishin_binding_digest_is_occurrence_preserving_and_order_independent() -> None:
    first = {"id": 1}
    second = {"id": 2}

    assert TaishinCrawler._history_rows_digest([first, second]) == (
        TaishinCrawler._history_rows_digest([second, first])
    )
    assert TaishinCrawler._history_rows_digest([first, first]) != (
        TaishinCrawler._history_rows_digest([first])
    )


def test_taishin_result_binding_preserves_duplicates_but_ignores_client_sort_order() -> None:
    snapshot = _result_snapshot()
    second = list(snapshot["rows"][0])
    second[0] = "2026/08/31 11:00:00"
    second[1] = "2026/08/31"
    second[2] = "轉帳"
    snapshot["rows"].append(second)
    snapshot["total_count"] = 2
    api_rows = TaishinCrawler._validate_history_snapshot(
        snapshot,
        identity=ACCOUNT,
        period="12_months",
        start=date(2025, 9, 2),
        end=date(2026, 9, 2),
        api_row_count=2,
    )["rows"][::-1]

    assert TaishinCrawler._validate_history_snapshot(
        snapshot,
        identity=ACCOUNT,
        period="12_months",
        start=date(2025, 9, 2),
        end=date(2026, 9, 2),
        api_row_count=2,
        api_rows=api_rows,
    )["status"] == "complete"


def test_taishin_result_snapshot_accepts_informational_row_without_amount() -> None:
    snapshot = _result_snapshot()
    snapshot["rows"][0][3] = "-"

    validated = TaishinCrawler._validate_history_snapshot(
        snapshot,
        identity=ACCOUNT,
        period="12_months",
        start=date(2025, 9, 2),
        end=date(2026, 9, 2),
        api_row_count=1,
    )

    assert validated["rows"][0]["income"] is None
    assert validated["rows"][0]["expend"] is None


def test_taishin_result_snapshot_rejects_malformed_grouped_money() -> None:
    snapshot = _result_snapshot()
    snapshot["rows"][0][3] = "1234,567"

    with pytest.raises(RuntimeError, match="taishin-twd-history-dom"):
        TaishinCrawler._validate_history_snapshot(
            snapshot,
            identity=ACCOUNT,
            period="12_months",
            start=date(2025, 9, 2),
            end=date(2026, 9, 2),
            api_row_count=1,
        )


def test_taishin_result_snapshot_rejects_unrelated_account_date() -> None:
    snapshot = _result_snapshot()
    snapshot["rows"][0][1] = "2035/01/01"

    with pytest.raises(RuntimeError, match="taishin-twd-history-dom"):
        TaishinCrawler._validate_history_snapshot(
            snapshot,
            identity=ACCOUNT,
            period="12_months",
            start=date(2025, 9, 2),
            end=date(2026, 9, 2),
            api_row_count=1,
        )


def test_taishin_result_snapshot_rejects_account_date_outside_query_window() -> None:
    snapshot = _result_snapshot()
    snapshot["rows"][0][1] = "2026/09/03"

    with pytest.raises(RuntimeError, match="taishin-twd-history-dom"):
        TaishinCrawler._validate_history_snapshot(
            snapshot,
            identity=ACCOUNT,
            period="12_months",
            start=date(2025, 9, 2),
            end=date(2026, 9, 2),
            api_row_count=1,
        )


def test_taishin_result_snapshot_rejects_same_count_stale_dom() -> None:
    snapshot = _result_snapshot()
    snapshot["rows"][0][2] = "舊畫面"

    with pytest.raises(RuntimeError, match="taishin-twd-history-dom"):
        TaishinCrawler._validate_history_snapshot(
            snapshot,
            identity=ACCOUNT,
            period="12_months",
            start=date(2025, 9, 2),
            end=date(2026, 9, 2),
            api_row_count=1,
            api_rows=[{
                "account_no": ACCOUNT,
                "datetime": "2026-09-01 12:34:56",
                "account_date": "2026-09-01",
                "desc": "利息",
                "expend": None,
                "income": 5,
                "balance": 105,
                "counterparty_bank": None,
                "counterparty_acct": "測試備註",
                "memo": "測試備註",
            }],
        )


def test_taishin_collects_attested_history_from_exact_response_and_fresh_dom(
    monkeypatch,
) -> None:
    from patchright.sync_api import sync_playwright

    manager = sync_playwright()
    playwright = manager.start()
    if not Path(playwright.chromium.executable_path).exists():
        manager.__exit__(None, None, None)
        pytest.skip("Patchright browser binary is not installed")
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    html = f"""
    <select><option value="tw">繁體中文</option></select>
    <select id="account">
      <option value="">-- 請選擇查詢帳號 --</option>
      <option value="{ACCOUNT}">012-34-567890-1-23 Richart</option>
    </select>
    <select id="period">
      <option value="">-- 請選擇查詢期間 --</option>
      <option value="7_days">7天</option><option value="14_days">14天</option>
      <option value="1_months">1個月</option><option value="2_months">2個月</option>
      <option value="3_months">3個月</option><option value="6_months">6個月</option>
      <option value="12_months">12個月</option><option value="inYear">自訂一年內期間</option>
      <option value="overYear">申請查詢逾一年以上</option>
    </select>
    <select id="sort"><option value="forward">由新到舊</option><option value="reverse">由舊到新</option></select>
    <input id="query" type="button" value="查詢">
    <div id="result"></div>
    <script>
    const stale = document.createElement('div');
    stale.style.opacity = '0';
    for (const node of [account, period, sort, query]) {{
      const clone = node.cloneNode(true);
      clone.removeAttribute('id');
      stale.appendChild(clone);
    }}
    document.body.prepend(stale);
    query.onclick = async () => {{
      const response = await fetch('/TIBNetBank/svc/web1/rb0102/query', {{
        method: 'POST', headers: {{'content-type': 'application/json'}},
        body: JSON.stringify({{account: account.value, start: '20250902', end: '20260902'}})
      }});
      await response.json();
      result.innerHTML = `<h2>交易明細</h2>
        <table id="savingAccountTransactionTable"><thead><tr>
          <th>交易日</th><th>帳務日</th><th>摘要</th><th>金額</th><th>餘額</th><th>備註</th><th></th>
        </tr></thead><tbody><tr>
          <td>2026/09/01 12:34:56</td><td>2026/09/01</td><td>利息</td><td>5</td><td>105</td><td>測試備註</td><td>未設定</td>
        </tr></tbody><tfoot><tr><td colspan="7">共 1 筆資料 資料日期：2026/09/02 10:00:00</td></tr></tfoot></table>
        <div class="_table_more"><span class="_table_more__nomore">沒有更多資料了</span></div>`;
    }};
    </script>
    """

    def route_handler(route) -> None:
        if route.request.url.endswith("/rb0102/query"):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "RESULT": "NORMAL",
                    "OUTPUTDATA": {"userList": [{
                        "sysdate": "20260901 12345600", "dateNew": "20260901",
                        "memo": "利息", "txnamt": "5", "txnamtOut": "-",
                        "txnamtIn": "5", "newbal": "105", "message": "測試備註",
                    }]},
                }),
            )
        elif route.request.url.endswith("/svc/rwd/index.html"):
            route.fulfill(
                status=200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=html,
            )
        else:
            route.fulfill(
                status=200,
                headers={"content-type": "text/html; charset=utf-8"},
                body='<iframe src="/TIBNetBank/svc/rwd/index.html"></iframe>',
            )

    page.route("**/*", route_handler)
    page.goto("https://my.taishinbank.com.tw/TIBNetBank/")
    collector = ResponseCollector("taishinbank.com.tw")
    collector.attach(page)
    crawler = _crawler()
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    try:
        result = crawler._collect_attested_twd_history(
            page, collector, as_of=date(2026, 9, 2),
        )
    finally:
        collector.detach(page)
        browser.close()
        manager.__exit__(None, None, None)

    expected_payload = _attested_payload(as_of=date(2026, 9, 2))
    assert result["twd_txn_results"][0]["rows"] == expected_payload["twd_txn_results"][0]["rows"]
    assert result["twd_txn_results"][0]["binding_digest"] == expected_payload["twd_txn_results"][0]["binding_digest"]
    assert validate_history_coverage(
        result["history_coverage"],
        expected_mode="full",
        expected_domains=frozenset({"twd_transactions"}),
    ) == {
        "ok": True, "mode": "full", "domains": ["twd_transactions"],
        "identities": 1, "windows": 1, "start": "2025-09-02", "end": "2026-09-02",
    }


def test_taishin_collect_does_not_swallow_required_history_failure() -> None:
    source = inspect.getsource(TaishinCrawler.collect)
    assert "self._collect_attested_twd_history(page, collector)" in source
    assert 'out["twd_txn_error"]' not in source


def test_taishin_collect_does_not_publish_raw_debug_artifacts() -> None:
    source = inspect.getsource(TaishinCrawler.collect)
    click_source = inspect.getsource(TaishinCrawler._try_ancestor_clicks)

    assert "screenshot(" not in source + click_source
    for token in ("l['text']!r", "target_link['text']!r", "n['click_class']"):
        assert token not in source + click_source
    for field in (
        "initial_url", "after_card_click_url", "credit_card_page_text",
        "credit_card_frame_url", "card_submenu", "credit_card_month_options", "final_url",
    ):
        assert f'out["{field}"]' not in source


def _attested_payload(*, as_of: date | None = None) -> dict:
    as_of = as_of or datetime.now(ZoneInfo("Asia/Taipei")).date()
    start = TaishinCrawler._subtract_months(as_of, 12)
    txn_day = as_of - timedelta(days=1)
    snapshot = _result_snapshot()
    snapshot["rows"][0][0] = f"{txn_day:%Y/%m/%d} 12:34:56"
    snapshot["rows"][0][1] = f"{txn_day:%Y/%m/%d}"
    rows = [{
        "account_no": ACCOUNT,
        "datetime": f"{txn_day:%Y-%m-%d} 12:34:56",
        "account_date": txn_day.isoformat(),
        "desc": "利息",
        "expend": None,
        "income": 5,
        "balance": 105,
        "counterparty_bank": None,
        "counterparty_acct": "測試備註",
        "memo": "測試備註",
    }]
    receipt = {
        "identity": ACCOUNT,
        "start": start.isoformat(),
        "end": as_of.isoformat(),
        "status": "complete",
        "pages": 1,
    }
    return {
        "card_bill_facts_ok": False,
        "twd_txn_results": [{
            **receipt,
            "period": "12_months",
            "rows": rows,
            "snapshot": snapshot,
            "api_row_count": 1,
            "api_rows": deepcopy(rows),
            "transport": {
                "url": "https://my.taishinbank.com.tw/TIBNetBank/svc/web1/rb0102/query",
                "method": "POST",
                "status": 200,
                "content_type": "application/json",
                "redirected": False,
                "main_frame_request": False,
                "request_frame_url": (
                    "https://my.taishinbank.com.tw/TIBNetBank/svc/rwd/"
                    "index.html#/RB0102/0100"
                ),
                "request_body": {
                    "account": ACCOUNT,
                    "start": start.strftime("%Y%m%d"),
                    "end": as_of.strftime("%Y%m%d"),
                },
                "response_result": "NORMAL",
                "body_size": 800,
                "request_sequence": 5,
            },
            "binding_digest": TaishinCrawler._history_rows_digest(rows),
            "request_count": 1,
            "response_count": 1,
        }],
        "history_coverage": {
            "version": 1,
            "mode": "full",
            "domains": [{
                "domain": "twd_transactions",
                "expected": [{
                    "identity": ACCOUNT,
                    "start": start.isoformat(),
                    "end": as_of.isoformat(),
                }],
                "windows": [receipt],
            }],
        },
    }


def test_taishin_persistence_does_not_store_api_endpoint_names(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("taishin", user_id=1, source_account_id=7)
    payload = _attested_payload()
    payload["api_responses"] = {"login:A123456789": {}}
    try:
        persist_collected("taishin", payload, store)
        count = store.conn.execute(
            "SELECT COUNT(*) FROM daily_metrics WHERE category = ?",
            ("taishin_endpoints",),
        ).fetchone()[0]
    finally:
        store.close()

    assert count == 0


def test_taishin_persistence_rejects_boolean_transport_counts(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("taishin", user_id=1, source_account_id=7)
    payload = _attested_payload()
    payload["twd_txn_results"][0]["request_count"] = True
    payload["twd_txn_results"][0]["response_count"] = True
    try:
        with pytest.raises(ValueError, match="invalid Taishin history"):
            persist_collected("taishin", payload, store)
    finally:
        store.close()


def test_taishin_persistence_rejects_missing_transport_receipt(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("taishin", user_id=1, source_account_id=7)
    payload = _attested_payload()
    payload["twd_txn_results"][0].pop("transport", None)
    try:
        with pytest.raises(ValueError, match="invalid Taishin history"):
            persist_collected("taishin", payload, store)
    finally:
        store.close()


def test_taishin_persistence_revalidates_and_writes_attested_dom_rows(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("taishin", user_id=1)
    payload = _attested_payload()
    try:
        delta = persist_collected("taishin", payload, store)
        row = store.conn.execute(
            "SELECT account_no, txn_datetime, account_date, description, income, balance, memo "
            "FROM twd_transactions",
        ).fetchone()
    finally:
        store.close()

    assert delta["twd_txn_new"] == 1
    txn_day = date.fromisoformat(payload["twd_txn_results"][0]["rows"][0]["account_date"])
    assert tuple(row) == (
        ACCOUNT, f"{txn_day.isoformat()} 12:34:56", txn_day.isoformat(),
        "利息 - 測試備註", 5, 105, "測試備註",
    )


def test_taishin_persist_collected_requires_history_coverage(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("taishin", user_id=1)
    try:
        with pytest.raises(ValueError, match="requires history coverage"):
            persist_collected("taishin", {"card_bill_facts_ok": False}, store)
    finally:
        store.close()


def test_direct_taishin_persister_requires_history_coverage(tmp_path, monkeypatch) -> None:
    from backend.core.persist.taishin import persist_taishin

    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("taishin", user_id=1)
    try:
        with pytest.raises(ValueError, match="invalid Taishin history"):
            persist_taishin({"card_bill_facts_ok": False}, store)
    finally:
        store.close()


def test_bank_pg_connection_exposes_rollback_for_atomic_persisters() -> None:
    from backend.core import bank_pg

    calls = []
    connection = object.__new__(bank_pg.Connection)
    connection._conn = SimpleNamespace(rollback=lambda: calls.append("rollback"))

    connection.rollback()

    assert calls == ["rollback"]


def test_direct_taishin_persister_surfaces_rollback_failure(monkeypatch) -> None:
    from backend.core.persist import taishin as taishin_persist

    monkeypatch.setattr(
        taishin_persist,
        "_persist_taishin",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("forward failed")),
    )
    store = object.__new__(BankStore)
    store.rollback = lambda: (_ for _ in ()).throw(OSError("rollback failed"))

    with pytest.raises(OSError, match="rollback failed"):
        taishin_persist.persist_taishin({}, store)


def test_direct_taishin_persister_rolls_back_all_writes_on_late_failure(
    tmp_path, monkeypatch,
) -> None:
    from backend.core.persist.taishin import persist_taishin

    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("taishin", user_id=1)
    payload = _attested_payload()
    payload["api_responses"] = {"query": {"OUTPUTDATA": {"SavingAccount": [{
        "accountNo": "99999999999999",
        "balance": "1",
        "accountTypeName": "活期存款",
    }]}}}
    monkeypatch.setattr(
        store, "log_sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("late failure")),
    )
    try:
        with pytest.raises(RuntimeError, match="late failure"):
            persist_taishin(payload, store)
        count = store.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    finally:
        store.close()

    assert count == 0


def test_taishin_persister_uses_taipei_calendar_date(tmp_path, monkeypatch) -> None:
    from backend.core.persist import taishin as taishin_persist

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return cls(2026, 9, 2, 16, 30)
            return cls(2026, 9, 3, 0, 30, tzinfo=tz)

    monkeypatch.setattr(taishin_persist, "datetime", FixedDatetime)
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    payload = _attested_payload(as_of=date(2026, 9, 3))
    payload["api_responses"] = {"query": {"OUTPUTDATA": {"SavingAccount": [{
        "accountNo": ACCOUNT,
        "balance": "1",
        "accountTypeName": "活期存款",
    }]}}}
    store = BankStore("taishin", user_id=1)
    try:
        taishin_persist.persist_taishin(payload, store)
        row = store.conn.execute(
            "SELECT snapshot_date FROM balance_history",
        ).fetchone()
        assert row is not None
        snapshot_date = row[0]
    finally:
        store.close()

    assert snapshot_date == "2026-09-03"


def test_taishin_persistence_rejects_future_coverage_end(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("taishin", user_id=1, source_account_id=7)
    future = datetime.now(ZoneInfo("Asia/Taipei")).date() + timedelta(days=1)
    payload = _attested_payload(as_of=future)
    try:
        with pytest.raises(ValueError, match="invalid Taishin history"):
            persist_collected("taishin", payload, store)
        cursors = store.latest_twd_transaction_dates()
    finally:
        store.close()

    assert cursors == {}


def test_taishin_persistence_rejects_stale_coverage_end(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("taishin", user_id=1, source_account_id=7)
    stale = datetime.now(ZoneInfo("Asia/Taipei")).date() - timedelta(days=1)
    payload = _attested_payload(as_of=stale)
    try:
        with pytest.raises(ValueError, match="invalid Taishin history"):
            persist_collected("taishin", payload, store)
        cursors = store.latest_twd_transaction_dates()
    finally:
        store.close()

    assert cursors == {}


def test_taishin_persister_does_not_store_customer_identifier_metric(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("taishin", user_id=1)
    payload = _attested_payload()
    payload["api_responses"] = {"login": {"CUSTNO": "A123456789"}}
    try:
        persist_collected("taishin", payload, store)
        count = store.conn.execute(
            "SELECT COUNT(*) FROM daily_metrics WHERE category='taishin_login_meta'",
        ).fetchone()[0]
    finally:
        store.close()

    assert count == 0


def test_taishin_history_rows_and_cursor_rollback_together(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("taishin", user_id=1)
    monkeypatch.setattr(
        store,
        "record_history_coverage_cursors",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cursor failed")),
    )
    try:
        with pytest.raises(RuntimeError, match="cursor failed"):
            persist_collected("taishin", _attested_payload(), store)
        count = store.conn.execute("SELECT COUNT(*) FROM twd_transactions").fetchone()[0]
    finally:
        store.close()

    assert count == 0


def test_taishin_persistence_rejects_oversize_attestation_artifact(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("taishin", user_id=1)
    payload = _attested_payload()
    payload["twd_txn_results"][0]["padding"] = "x" * 5_000_000
    try:
        with pytest.raises(ValueError, match="invalid Taishin history"):
            persist_collected("taishin", payload, store)
        count = store.conn.execute("SELECT COUNT(*) FROM twd_transactions").fetchone()[0]
    finally:
        store.close()

    assert count == 0


def test_taishin_persistence_rejects_non_single_query_cardinality(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("taishin", user_id=1)
    payload = _attested_payload()
    payload["twd_txn_results"][0]["request_count"] = 2
    try:
        with pytest.raises(ValueError, match="invalid Taishin history"):
            persist_collected("taishin", payload, store)
        count = store.conn.execute("SELECT COUNT(*) FROM twd_transactions").fetchone()[0]
    finally:
        store.close()

    assert count == 0


def test_taishin_persistence_rejects_forged_same_count_dom_rows(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("taishin", user_id=1)
    payload = deepcopy(_attested_payload())
    payload["twd_txn_results"][0]["snapshot"]["rows"][0][2] = "舊畫面"
    payload["twd_txn_results"][0]["rows"][0]["desc"] = "舊畫面"
    payload["twd_txn_results"][0]["binding_digest"] = TaishinCrawler._history_rows_digest(
        payload["twd_txn_results"][0]["rows"],
    )
    try:
        with pytest.raises(ValueError, match="invalid Taishin history"):
            persist_collected("taishin", payload, store)
        count = store.conn.execute("SELECT COUNT(*) FROM twd_transactions").fetchone()[0]
    finally:
        store.close()

    assert count == 0


def test_taishin_persistence_rejects_incremental_gap_from_existing_cursor(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("taishin", user_id=1, source_account_id=7)
    as_of = datetime.now(ZoneInfo("Asia/Taipei")).date()
    cursor_day = as_of - timedelta(days=240)
    store.upsert_twd_txns([{
        "account_no": ACCOUNT,
        "datetime": f"{cursor_day.isoformat()} 00:00:00",
        "account_date": cursor_day.isoformat(),
        "desc": "既有資料",
        "income": 1,
        "balance": 1,
    }])
    payload = deepcopy(_attested_payload(as_of=as_of))
    payload["history_coverage"]["mode"] = "incremental"
    expected = payload["history_coverage"]["domains"][0]["expected"][0]
    receipt = payload["history_coverage"]["domains"][0]["windows"][0]
    result = payload["twd_txn_results"][0]
    start = as_of - timedelta(days=7)
    for item in (expected, receipt, result):
        item["start"] = start.isoformat()
    result["period"] = "7_days"
    result["snapshot"]["selected_period"] = "7_days"
    result["transport"]["request_body"]["start"] = start.strftime("%Y%m%d")

    try:
        with pytest.raises(ValueError, match="invalid Taishin history"):
            persist_collected("taishin", payload, store)
        count = store.conn.execute("SELECT COUNT(*) FROM twd_transactions").fetchone()[0]
    finally:
        store.close()

    assert count == 1


def test_taishin_full_coverage_replaces_removed_cursor_identities(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("taishin", user_id=1, source_account_id=7)
    as_of = datetime.now(ZoneInfo("Asia/Taipei")).date()
    store._record_transaction_cursor("twd_transactions", ACCOUNT, as_of.isoformat())
    store._record_transaction_cursor(
        "twd_transactions", "99999999999999", (as_of + timedelta(days=365)).isoformat(),
    )
    store.commit()
    try:
        persist_collected("taishin", _attested_payload(as_of=as_of), store)
        cursors = store.latest_twd_transaction_dates()
    finally:
        store.close()

    assert cursors == {ACCOUNT: as_of}


def test_taishin_full_coverage_repairs_future_cursor(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    store = BankStore("taishin", user_id=1, source_account_id=7)
    as_of = datetime.now(ZoneInfo("Asia/Taipei")).date()
    future = as_of + timedelta(days=365)
    store._record_transaction_cursor("twd_transactions", ACCOUNT, future.isoformat())
    store.commit()
    try:
        persist_collected("taishin", _attested_payload(as_of=as_of), store)
        cursor = store.latest_twd_transaction_dates()[ACCOUNT]
    finally:
        store.close()

    assert cursor == as_of
