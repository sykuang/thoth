from __future__ import annotations

from datetime import date, timedelta

import pytest

from backend.banks.cathay import CathayCrawler
from backend.core.base import ApiHit, ResponseCollector, validate_history_coverage
from backend.core.persist import persist_collected
from backend.core.store import BankStore


def _template(account: str) -> ApiHit:
    return ApiHit(
        url=(
            "https://www.cathaybk.com.tw/OnlineBankingApi/ClientBank/Api/"
            "ClientBank/B_ACCT_Q_TransferDetail"
        ),
        method="POST",
        status=200,
        req_body={
            "content": {
                "customerId": "opaque-customer",
                "queryFilters": [{
                    "accountNumber": account,
                    "startDate": "2026-08-01",
                    "endDate": "2026-08-30",
                }],
            },
        },
        resp_json={},
    )


class _FetchPage:
    def __init__(self, *, empty: bool = False, redirected: bool = False) -> None:
        self.payloads: list[dict] = []
        self.empty = empty
        self.redirected = redirected

    def evaluate(self, script: str, payload: dict) -> dict:
        assert "credentials: 'include'" in script
        self.payloads.append(payload)
        query = payload["body"]["content"]["queryFilters"][0]
        details = [] if self.empty else [{
            "txnDateTime": f'{query["endDate"]}T10:00:00',
            "accountDate": query["endDate"],
            "description": "測試交易",
            "expendAmt": "1",
            "incomeAmt": "0",
            "balance": "99",
        }]
        return {
            "status": 200,
            "url": "https://www.cathaybk.com.tw/redirected" if self.redirected else payload["url"],
            "redirected": self.redirected,
            "json": {
                "success": "true",
                "returnCode": "0000",
                "content": {
                    "datas": [{
                        "queryStatus": "SUCCESS",
                        "accountNumber": query["accountNumber"],
                        "count": len(details),
                        "startDate": query["startDate"],
                        "endDate": query["endDate"],
                        "details": details,
                    }],
                },
            },
        }


def test_cathay_history_windows_are_exact_contiguous_and_at_most_30_days():
    windows = CathayCrawler._history_windows(date(2025, 8, 31), date(2026, 8, 30))

    assert windows[0][0] == date(2025, 8, 31)
    assert windows[-1][1] == date(2026, 8, 30)
    assert all((end - start).days < 30 for start, end in windows)
    assert all(
        right_start == left_end + timedelta(days=1)
        for (_, left_end), (right_start, _) in zip(windows, windows[1:])
    )


def test_cathay_twd_response_fails_closed_on_wrong_account_or_truncation():
    valid = {
        "success": "true",
        "returnCode": "0000",
        "content": {"datas": [{
            "queryStatus": "SUCCESS",
            "accountNumber": "00001234",
            "count": 1,
            "startDate": "2026-08-01",
            "endDate": "2026-08-30",
            "details": [{
                "txnDateTime": "2026-08-30T10:00:00",
                "expendAmt": "1",
                "incomeAmt": "0",
            }],
        }]},
    }

    assert CathayCrawler._validated_twd_account(
        valid, "00001234", start=date(2026, 8, 1), end=date(2026, 8, 30),
    )["count"] == 1

    wrong = {**valid, "content": {"datas": [{**valid["content"]["datas"][0], "accountNumber": "00009999"}]}}
    truncated = {**valid, "content": {"datas": [{**valid["content"]["datas"][0], "count": 2}]}}
    for response in (wrong, truncated, {**valid, "success": "false"}):
        with pytest.raises(RuntimeError, match="cathay-twd-history"):
            CathayCrawler._validated_twd_account(
                response, "00001234",
                start=date(2026, 8, 1), end=date(2026, 8, 30),
            )


@pytest.mark.parametrize(
    "mutation", ["method", "status", "host", "path", "userinfo", "customer_id"],
)
def test_cathay_history_template_requires_exact_owned_endpoint(mutation):
    hit = _template("00001234")
    if mutation == "method":
        hit.method = "GET"
    elif mutation == "status":
        hit.status = 500
    elif mutation == "host":
        hit.url = "https://www.cathaybk.com.tw.evil.example/OnlineBankingApi/ClientBank/Api/ClientBank/B_ACCT_Q_TransferDetail"
    elif mutation == "path":
        hit.url = "https://www.cathaybk.com.tw/evil/B_ACCT_Q_TransferDetail"
    elif mutation == "userinfo":
        hit.url = "https://user@www.cathaybk.com.tw/OnlineBankingApi/ClientBank/Api/ClientBank/B_ACCT_Q_TransferDetail"
    else:
        hit.req_body["content"]["customerId"] = {"bad": 1}

    with pytest.raises(RuntimeError, match="cathay-twd-history-template"):
        CathayCrawler._twd_template_account(hit)


@pytest.mark.parametrize(
    "mutation",
    [
        "query_status", "account_normalized", "start", "start_suffix", "end",
        "end_slash", "fractional_count", "invalid_amount", "missing_datetime",
        "bad_time", "bad_account_date", "bad_description", "oversized_amount",
        "precision_loss_amount", "account_date_out_of_range", "negative_expend",
        "negative_income", "missing_flow_amounts",
    ],
)
def test_cathay_twd_response_binds_status_range_count_and_persistable_rows(mutation):
    account = {
        "queryStatus": "SUCCESS",
        "accountNumber": "00001234",
        "count": 1,
        "startDate": "2026-08-01",
        "endDate": "2026-08-30",
        "details": [{
            "txnDateTime": "2026-08-20T10:00:00",
            "accountDate": "2026-08-20",
            "expendAmt": "1",
            "incomeAmt": None,
            "balance": "99",
        }],
    }
    if mutation == "query_status":
        account["queryStatus"] = "FAILED"
    elif mutation == "account_normalized":
        account["accountNumber"] = "1234"
    elif mutation == "start":
        account["startDate"] = "1999-01-01"
    elif mutation == "start_suffix":
        account["startDate"] = "2026-08-01-junk"
    elif mutation == "end":
        account["endDate"] = "1999-01-02"
    elif mutation == "end_slash":
        account["endDate"] = "2026/08/30"
    elif mutation == "fractional_count":
        account["count"] = -0.5
        account["details"] = []
    elif mutation == "invalid_amount":
        account["details"][0]["expendAmt"] = "not-a-number"
    elif mutation == "missing_datetime":
        account["details"][0].pop("txnDateTime")
    elif mutation == "bad_account_date":
        account["details"][0]["accountDate"] = {}
    elif mutation == "bad_description":
        account["details"][0]["description"] = {}
    elif mutation == "oversized_amount":
        account["details"][0]["expendAmt"] = "1e30"
    elif mutation == "precision_loss_amount":
        account["details"][0]["expendAmt"] = "9007199254740993"
    elif mutation == "account_date_out_of_range":
        account["details"][0]["accountDate"] = "1999-01-01"
    elif mutation == "negative_expend":
        account["details"][0]["expendAmt"] = "-1"
    elif mutation == "negative_income":
        account["details"][0]["incomeAmt"] = "-1"
    elif mutation == "missing_flow_amounts":
        account["details"][0]["expendAmt"] = None
        account["details"][0]["incomeAmt"] = None
    else:
        account["details"][0]["txnDateTime"] = "2026-08-20T99:99:99"
    response = {"success": True, "returnCode": "0000", "content": {"datas": [account]}}

    with pytest.raises(RuntimeError, match="cathay-twd-history"):
        CathayCrawler._validated_twd_account(
            response, "00001234",
            start=date(2026, 8, 1), end=date(2026, 8, 30),
        )


def test_cathay_incremental_uses_account_cursor_and_emits_valid_coverage(monkeypatch):
    crawler = object.__new__(CathayCrawler)
    crawler.transaction_cursors = {
        "twd_transactions": {"1234": date(2026, 8, 20)},
    }
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "incremental")
    monkeypatch.setattr(crawler, "_seed_twd_query_templates", lambda page, collector: [_template("00001234")])
    page = _FetchPage()

    result = crawler._collect_twd_history(
        page, ResponseCollector(), as_of=date(2026, 8, 30),
    )

    query = page.payloads[0]["body"]["content"]["queryFilters"][0]
    assert query == {
        "accountNumber": "00001234",
        "startDate": "2026-08-13",
        "endDate": "2026-08-30",
    }
    assert result["accounts"][0]["account"] == "00001234"
    transaction = result["accounts"][0]["transactions"][0]
    assert transaction["expend"] == 1
    assert transaction["income"] == 0
    assert transaction["balance"] == 99
    summary = validate_history_coverage(
        result["coverage"],
        expected_mode="incremental",
        expected_domains=frozenset({"twd_transactions"}),
    )
    assert summary["identities"] == 1
    assert summary["windows"] == 1
    assert "1234" not in repr(summary)


def test_cathay_new_identity_falls_back_to_one_year_floor(monkeypatch):
    crawler = object.__new__(CathayCrawler)
    crawler.transaction_cursors = {"twd_transactions": {}}
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "incremental")
    monkeypatch.setattr(crawler, "_seed_twd_query_templates", lambda page, collector: [_template("00001234")])
    page = _FetchPage()

    result = crawler._collect_twd_history(
        page, ResponseCollector(), as_of=date(2026, 8, 30),
    )

    assert page.payloads[0]["body"]["content"]["queryFilters"][0]["startDate"] == "2025-08-31"
    assert page.payloads[-1]["body"]["content"]["queryFilters"][0]["endDate"] == "2026-08-30"
    assert len(page.payloads) == 13
    validate_history_coverage(
        result["coverage"],
        expected_mode="incremental",
        expected_domains=frozenset({"twd_transactions"}),
    )


def test_cathay_rejects_duplicate_account_templates(monkeypatch):
    crawler = object.__new__(CathayCrawler)
    crawler.transaction_cursors = {"twd_transactions": {}}
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    monkeypatch.setattr(
        crawler,
        "_seed_twd_query_templates",
        lambda page, collector: [_template("00001234"), _template("00001234")],
    )

    with pytest.raises(RuntimeError, match="cathay-twd-history-account-inventory"):
        crawler._collect_twd_history(
            _FetchPage(), ResponseCollector(), as_of=date(2026, 8, 30),
        )


def test_cathay_account_inventory_is_authoritative_and_twd_only():
    collector = ResponseCollector()
    collector.hits.append(ApiHit(
        url=(
            "https://www.cathaybk.com.tw/OnlineBankingApi/Common/Api/"
            "ClientCommon/G_CUST_Q_TransAccountList"
        ),
        method="POST",
        status=200,
        req_body={"content": {"queryType": "TWD"}},
        resp_json={
            "success": True,
            "returnCode": "0000",
            "content": {"datas": [
                {"accountNo": "00001234", "currency": "TWD"},
                {"accountNo": "00005678", "currency": "USD"},
            ]},
        },
    ))

    assert CathayCrawler._twd_account_inventory(collector) == {"1234"}


@pytest.mark.parametrize(
    "mutation", ["status", "method", "host", "path", "query_type", "currency"],
)
def test_cathay_account_inventory_requires_exact_successful_twd_request(mutation):
    hit = ApiHit(
        url=(
            "https://www.cathaybk.com.tw/OnlineBankingApi/Common/Api/"
            "ClientCommon/G_CUST_Q_TransAccountList"
        ),
        method="POST",
        status=200,
        req_body={"content": {"queryType": "TWD"}},
        resp_json={"success": True, "returnCode": "0000", "content": {"datas": []}},
    )
    if mutation == "status":
        hit.status = 500
    elif mutation == "method":
        hit.method = "GET"
    elif mutation == "host":
        hit.url = "https://www.cathaybk.com.tw.evil.example/api/G_CUST_Q_TransAccountList"
    elif mutation == "path":
        hit.url = "https://www.cathaybk.com.tw/evil/G_CUST_Q_TransAccountList"
    elif mutation == "query_type":
        hit.req_body["content"]["queryType"] = "FX"
    else:
        hit.resp_json["content"]["datas"] = [{"currency": {}, "accountNo": "1234"}]
    collector = ResponseCollector()
    collector.hits.append(hit)

    with pytest.raises(RuntimeError, match="cathay-twd-history-account-inventory"):
        CathayCrawler._twd_account_inventory(collector)


def test_cathay_zero_twd_accounts_emit_explicit_empty_without_query(monkeypatch):
    crawler = object.__new__(CathayCrawler)
    crawler.transaction_cursors = {"twd_transactions": {}}
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    monkeypatch.setattr(
        crawler,
        "_seed_twd_query_templates",
        lambda page, collector: pytest.fail("zero-account domain must not query"),
    )

    result = crawler._collect_twd_history(
        _FetchPage(), ResponseCollector(),
        as_of=date(2026, 8, 30), expected_identities=set(),
    )

    assert result["accounts"] == []
    summary = validate_history_coverage(
        result["coverage"], expected_mode="full",
        expected_domains=frozenset({"twd_transactions"}),
    )
    assert summary["identities"] == 0
    assert summary["start"] == "2025-08-31"


def test_cathay_templates_must_exactly_match_authoritative_inventory(monkeypatch):
    crawler = object.__new__(CathayCrawler)
    crawler.transaction_cursors = {"twd_transactions": {}}
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    monkeypatch.setattr(
        crawler,
        "_seed_twd_query_templates",
        lambda page, collector: [_template("00001234")],
    )

    with pytest.raises(RuntimeError, match="cathay-twd-history-account-inventory"):
        crawler._collect_twd_history(
            _FetchPage(), ResponseCollector(), as_of=date(2026, 8, 30),
            expected_identities={"1234", "5678"},
        )


def test_cathay_replayed_history_rejects_redirected_response(monkeypatch):
    crawler = object.__new__(CathayCrawler)
    crawler.transaction_cursors = {}
    monkeypatch.setattr(
        crawler, "_seed_twd_query_templates",
        lambda page, collector: [_template("00001234")],
    )
    with pytest.raises(RuntimeError, match="cathay-twd-history-fetch"):
        crawler._collect_twd_history(
            _FetchPage(redirected=True), ResponseCollector(), as_of=date(2026, 8, 30),
        )


def test_cathay_fetch_forbids_redirects():
    script = CathayCrawler._twd_fetch_script()
    assert "redirect: 'error'" in script
    assert "AbortController" in script
    assert "setTimeout(() => controller.abort(), 30000)" in script


def test_cathay_attested_rows_persist_and_advance_account_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "incremental")
    crawler = object.__new__(CathayCrawler)
    crawler.transaction_cursors = {
        "twd_transactions": {"1234": date(2026, 8, 20)},
    }
    monkeypatch.setattr(
        crawler,
        "_seed_twd_query_templates",
        lambda page, collector: [_template("00001234")],
    )
    result = crawler._collect_twd_history(
        _FetchPage(), ResponseCollector(), as_of=date(2026, 8, 30),
    )
    store = BankStore("cathay", user_id=7, source_account_id=91)
    try:
        delta = persist_collected(
            "cathay",
            {
                "twd_transactions": result["accounts"],
                "history_coverage": result["coverage"],
                "credit_card": {},
            },
            store,
        )
        assert delta["twd_txn_new"] == 1
        assert store.latest_twd_transaction_dates() == {"1234": date(2026, 8, 30)}
    finally:
        store.close()


def test_cathay_explicit_empty_history_advances_account_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    crawler = object.__new__(CathayCrawler)
    crawler.transaction_cursors = {}
    monkeypatch.setattr(
        crawler, "_seed_twd_query_templates",
        lambda page, collector: [_template("00001234")],
    )
    result = crawler._collect_twd_history(
        _FetchPage(empty=True), ResponseCollector(), as_of=date(2026, 8, 30),
    )
    store = BankStore("cathay", user_id=7, source_account_id=91)
    try:
        persist_collected(
            "cathay",
            {
                "twd_transactions": result["accounts"],
                "history_coverage": result["coverage"],
                "credit_card": {},
            },
            store,
        )
        assert store.latest_twd_transaction_dates() == {"1234": date(2026, 8, 30)}
    finally:
        store.close()
