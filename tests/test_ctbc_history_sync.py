from __future__ import annotations

from datetime import date

import pytest

import backend.banks.ctbc as ctbc_module
from backend.banks.ctbc import (
    CtbcCrawler,
    _CTBC_EBMW_PATH,
    _ctbc_month_windows,
)
from backend.core.base import ApiHit, ResponseCollector, validate_history_coverage
from backend.core.persist import persist_collected
from backend.core.store import BankStore


def _inventory_hit(accounts: list[str]) -> ApiHit:
    return ApiHit(
        url=f"https://www.ctbcbank.com{_CTBC_EBMW_PATH}",
        method="POST",
        status=200,
        req_body={"resource": "/twrbc-deposit/qu001/010", "rqData": {}},
        resp_json={
            "code": "0000",
            "rsData": {
                "twdAcctSummaryResponse": {
                    "demDepBalSummaryResponse": {
                        "infoList": [
                            {"accountId": account, "balance": "100"}
                            for account in accounts
                        ],
                    },
                },
            },
        },
    )


def _template(account: str = "acct-a") -> ApiHit:
    return ApiHit(
        url=f"https://www.ctbcbank.com{_CTBC_EBMW_PATH}",
        method="POST",
        status=200,
        req_body={
            "resource": "/twrbc-deposit/qu002/011",
            "rqData": {"accountId": account, "type": "m0", "ctry": "TW"},
        },
        resp_json={
            "code": "0000",
            "rsData": {
                "accountId": account,
                "type": "m0",
                "count": 0,
                "totalPages": 1,
                "detailList": [],
            },
        },
    )


class _FetchPage:
    DATES = {
        "m0": "2026-08-20-10.00.00",
        "m1": "2026-07-20-10.00.00",
        "m2": "2026-06-20-10.00.00",
        "m3": "2026-05-20-10.00.00",
        "m4": "2026-04-20-10.00.00",
        "m5": "2026-03-20-10.00.00",
    }

    def __init__(self, *, empty: bool = False, fail_month: str | None = None) -> None:
        self.empty = empty
        self.fail_month = fail_month
        self.payloads: list[dict] = []
        self.url = "https://www.ctbcbank.com/twrbc/twrbc-deposit/qu002/010"

    def goto(self, *args, **kwargs):
        return None

    def wait_for_timeout(self, milliseconds: int) -> None:
        return None

    def evaluate(self, script: str, payload: dict) -> dict:
        assert "AbortController" in script
        assert "redirect: 'error'" in script
        self.payloads.append(payload)
        query = payload["body"]["rqData"]
        month = query["type"]
        if month == self.fail_month:
            return {
                "status": 200,
                "url": payload["url"],
                "redirected": False,
                "contentType": "application/json",
                "json": {"code": "9999", "rsData": {"detailList": []}},
            }
        details = [] if self.empty else [{
            "actDtTm": self.DATES[month],
            "trnDtRaw": self.DATES[month][:10].replace("-", ""),
            "memo1": "test",
            "dbAmt": "1",
            "crAmt": "0",
            "balanceAmt": "99",
        }]
        return {
            "status": 200,
            "url": payload["url"],
            "redirected": False,
            "contentType": "application/json",
            "json": {"code": "0000", "rsData": {
                "accountId": query["accountId"],
                "type": month,
                "count": len(details),
                "totalPages": 1,
                "detailList": details,
            }},
        }


def _collector(accounts: list[str]) -> ResponseCollector:
    collector = ResponseCollector()
    setattr(collector, "auth_token", "Bearer synthetic-token")
    collector.hits.extend([_inventory_hit(accounts), _template(accounts[0] if accounts else "seed")])
    return collector


def test_ctbc_opts_in_only_twd_history():
    assert CtbcCrawler.HISTORY_COVERAGE_REQUIRED is True
    assert frozenset({"twd_transactions"}) == CtbcCrawler.HISTORY_COVERAGE_DOMAINS


def test_ctbc_six_month_capability_windows_are_exact():
    windows = _ctbc_month_windows(date(2026, 8, 30))
    assert windows == [
        ("m5", date(2026, 3, 1), date(2026, 3, 31)),
        ("m4", date(2026, 4, 1), date(2026, 4, 30)),
        ("m3", date(2026, 5, 1), date(2026, 5, 31)),
        ("m2", date(2026, 6, 1), date(2026, 6, 30)),
        ("m1", date(2026, 7, 1), date(2026, 7, 31)),
        ("m0", date(2026, 8, 1), date(2026, 8, 30)),
    ]


def test_ctbc_inventory_requires_exact_owned_success_response():
    valid = _inventory_hit(["acct-a"])
    collector = ResponseCollector()
    collector.hits.append(valid)
    payload, identities = CtbcCrawler._validated_twd_inventory(collector)
    assert identities == {"acct-a"}
    assert payload["demDepBalSummaryResponse"]["infoList"][0]["accountId"] == "acct-a"

    mutations = [
        lambda hit: setattr(hit, "status", 500),
        lambda hit: setattr(hit, "method", "GET"),
        lambda hit: setattr(hit, "url", "https://www.ctbcbank.com.evil.example/IB/api/adapters/IB_Adapter/resource/ebmwResource"),
        lambda hit: setattr(hit, "url", "https://www.ctbcbank.com/evil/ebmwResource"),
        lambda hit: hit.req_body.update(resource="/twrbc-foreign/qu001/010"),
        lambda hit: hit.resp_json.update(code="9999"),
        lambda hit: hit.resp_json["rsData"]["twdAcctSummaryResponse"]["demDepBalSummaryResponse"]["infoList"].append({"accountId": {}}),
    ]
    for mutate in mutations:
        hit = _inventory_hit(["acct-a"])
        mutate(hit)
        bad = ResponseCollector()
        bad.hits.append(hit)
        with pytest.raises(RuntimeError, match="ctbc-twd-history-inventory"):
            CtbcCrawler._validated_twd_inventory(bad)


@pytest.mark.parametrize("account", ["", " acct-a", "acct-a "])
def test_ctbc_history_seed_rejects_noncanonical_account(account):
    collector = ResponseCollector()
    collector.hits.append(_template(account))
    assert CtbcCrawler._latest_qu002_011_hit(collector) is None


def test_ctbc_full_history_queries_all_accounts_and_six_months(monkeypatch):
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    crawler = object.__new__(CtbcCrawler)
    crawler.transaction_cursors = {}
    page = _FetchPage()
    collector = _collector(["acct-a", "acct-b"])
    deposit, identities = crawler._validated_twd_inventory(collector)

    result = crawler._collect_twd_deposit_history(
        page, collector, deposit, expected_identities=identities,
        as_of=date(2026, 8, 30),
    )

    assert len(page.payloads) == 12
    assert [p["body"]["rqData"]["type"] for p in page.payloads[:6]] == [
        "m5", "m4", "m3", "m2", "m1", "m0",
    ]
    summary = validate_history_coverage(
        result["coverage"], expected_mode="full",
        expected_domains=frozenset({"twd_transactions"}),
    )
    assert summary["identities"] == 2
    assert summary["windows"] == 12
    assert summary["start"] == "2026-03-01"
    assert summary["end"] == "2026-08-30"


def test_ctbc_checks_final_page_origin_before_forwarding_bearer(monkeypatch):
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    crawler = object.__new__(CtbcCrawler)
    crawler.transaction_cursors = {}
    collector = _collector(["acct-a"])
    deposit, identities = crawler._validated_twd_inventory(collector)
    page = _FetchPage()
    page.url = "https://attacker.example/twrbc/twrbc-deposit/qu002/010"
    with pytest.raises(RuntimeError, match="ctbc-twd-history-template"):
        crawler._collect_twd_deposit_history(
            page, collector, deposit,
            expected_identities=identities, as_of=date(2026, 8, 30),
        )


def test_ctbc_default_date_uses_taipei_calendar(monkeypatch):
    class FakeDateTime:
        @classmethod
        def now(cls, timezone):
            assert str(timezone) == "Asia/Taipei"
            return cls()

        def date(self):
            return date(2026, 8, 30)

    monkeypatch.setattr(ctbc_module, "datetime", FakeDateTime)
    crawler = object.__new__(CtbcCrawler)
    crawler.transaction_cursors = {}
    collector = _collector([])
    deposit, identities = crawler._validated_twd_inventory(collector)
    result = crawler._collect_twd_deposit_history(
        _FetchPage(), collector, deposit, expected_identities=identities,
    )
    assert result["coverage"]["domains"][0]["empty_window"]["end"] == "2026-08-30"


def test_ctbc_incremental_queries_only_months_covering_cursor_overlap(monkeypatch):
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "incremental")
    crawler = object.__new__(CtbcCrawler)
    crawler.transaction_cursors = {
        "twd_transactions": {"acct-a": date(2026, 8, 20)},
    }
    page = _FetchPage()
    collector = _collector(["acct-a"])
    deposit, identities = crawler._validated_twd_inventory(collector)

    result = crawler._collect_twd_deposit_history(
        page, collector, deposit, expected_identities=identities,
        as_of=date(2026, 8, 30),
    )

    assert [p["body"]["rqData"]["type"] for p in page.payloads] == ["m0"]
    expected = result["coverage"]["domains"][0]["expected"][0]
    assert expected == {"identity": "acct-a", "start": "2026-08-01", "end": "2026-08-30"}


def test_ctbc_explicit_empty_is_complete_but_any_failed_month_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    crawler = object.__new__(CtbcCrawler)
    crawler.transaction_cursors = {}
    collector = _collector(["acct-a"])
    deposit, identities = crawler._validated_twd_inventory(collector)

    empty = crawler._collect_twd_deposit_history(
        _FetchPage(empty=True), collector, deposit,
        expected_identities=identities, as_of=date(2026, 8, 30),
    )
    windows = empty["coverage"]["domains"][0]["windows"]
    assert len(windows) == 6
    assert all(window["status"] == "explicit_empty" for window in windows)
    store = BankStore("ctbc", user_id=7, source_account_id=91)
    try:
        persist_collected(
            "ctbc",
            {
                "summary": {},
                "twd_deposit": deposit,
                "twd_history": empty["accounts"],
                "history_coverage": empty["coverage"],
                "card_api_dump": {},
            },
            store,
        )
        assert store.latest_twd_transaction_dates() == {"acct-a": date(2026, 8, 30)}
    finally:
        store.close()

    with pytest.raises(RuntimeError, match="ctbc-twd-history-fetch"):
        crawler._collect_twd_deposit_history(
            _FetchPage(fail_month="m3"), collector, deposit,
            expected_identities=identities, as_of=date(2026, 8, 30),
        )


def test_ctbc_zero_twd_accounts_emit_canonical_explicit_empty(monkeypatch):
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    crawler = object.__new__(CtbcCrawler)
    crawler.transaction_cursors = {}
    collector = _collector([])
    deposit, identities = crawler._validated_twd_inventory(collector)

    result = crawler._collect_twd_deposit_history(
        _FetchPage(), collector, deposit,
        expected_identities=identities, as_of=date(2026, 8, 30),
    )
    summary = validate_history_coverage(
        result["coverage"], expected_mode="full",
        expected_domains=frozenset({"twd_transactions"}),
    )
    assert result["accounts"] == []
    assert summary["identities"] == 0
    assert summary["start"] == "2026-03-01"
    assert summary["end"] == "2026-08-30"


def test_ctbc_rows_without_money_or_with_invalid_dates_fail_closed(monkeypatch):
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "incremental")
    crawler = object.__new__(CtbcCrawler)
    crawler.transaction_cursors = {}
    collector = _collector(["acct-a"])
    deposit, identities = crawler._validated_twd_inventory(collector)

    class BadPage(_FetchPage):
        def evaluate(self, script: str, payload: dict) -> dict:
            result = super().evaluate(script, payload)
            row = result["json"]["rsData"]["detailList"][0]
            row["actDtTm"] = "2026-99-99-99.99.99"
            row["dbAmt"] = None
            row["crAmt"] = None
            return result

    with pytest.raises(RuntimeError, match="ctbc-twd-history-row"):
        crawler._collect_twd_deposit_history(
            BadPage(), collector, deposit,
            expected_identities=identities, as_of=date(2026, 8, 30),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("totalPages", 2),
        ("totalPages", "1"),
        ("totalPages", True),
        ("hasMore", True),
        ("count", 99),
        ("count", "1"),
        ("count", True),
    ],
)
def test_ctbc_pagination_or_count_mismatch_fails_closed(field, value):
    class PagedPage(_FetchPage):
        def evaluate(self, script: str, payload: dict) -> dict:
            result = super().evaluate(script, payload)
            result["json"]["rsData"][field] = value
            return result

    with pytest.raises(RuntimeError):
        CtbcCrawler._fetch_qu002_011(
            PagedPage(),
            f"https://www.ctbcbank.com{_CTBC_EBMW_PATH}",
            _template().req_body,
            "acct-a", "m0", "Bearer synthetic-token",
        )


@pytest.mark.parametrize("content_type", ["application/+json", "application/foo/bar+json"])
def test_ctbc_replay_rejects_malformed_json_media_types(content_type):
    class BadMediaPage(_FetchPage):
        def evaluate(self, script: str, payload: dict) -> dict:
            result = super().evaluate(script, payload)
            result["contentType"] = content_type
            return result

    with pytest.raises(RuntimeError):
        CtbcCrawler._fetch_qu002_011(
            BadMediaPage(),
            f"https://www.ctbcbank.com{_CTBC_EBMW_PATH}",
            _template().req_body,
            "acct-a", "m0", "Bearer synthetic-token",
        )


@pytest.mark.parametrize("mutation", ["content_type", "bearer", "account", "month", "metadata"])
def test_ctbc_replay_requires_strict_media_ownership_and_completeness(mutation):
    class MutatedPage(_FetchPage):
        def evaluate(self, script: str, payload: dict) -> dict:
            result = super().evaluate(script, payload)
            rs = result["json"]["rsData"]
            if mutation == "content_type":
                result["contentType"] = "text/notjson"
            elif mutation == "account":
                rs["accountId"] = "acct-b"
            elif mutation == "month":
                rs["type"] = "m1"
            elif mutation == "metadata":
                rs.pop("count")
                rs.pop("totalPages")
            return result

    bearer = "Bearer " if mutation == "bearer" else "Bearer synthetic-token"
    with pytest.raises(RuntimeError):
        CtbcCrawler._fetch_qu002_011(
            MutatedPage(),
            f"https://www.ctbcbank.com{_CTBC_EBMW_PATH}",
            _template().req_body,
            "acct-a", "m0", bearer,
        )


def test_ctbc_replay_rejects_wrong_resource_template():
    body = _template().req_body
    body["resource"] = "/twrbc-foreign/qu999/999"
    with pytest.raises(RuntimeError, match="invalid-request"):
        CtbcCrawler._fetch_qu002_011(
            _FetchPage(),
            f"https://www.ctbcbank.com{_CTBC_EBMW_PATH}",
            body,
            "acct-a", "m0", "Bearer synthetic-token",
        )


def test_ctbc_attested_history_persists_and_advances_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "incremental")
    crawler = object.__new__(CtbcCrawler)
    crawler.transaction_cursors = {
        "twd_transactions": {"acct-a": date(2026, 8, 20)},
    }
    collector = _collector(["acct-a"])
    deposit, identities = crawler._validated_twd_inventory(collector)
    result = crawler._collect_twd_deposit_history(
        _FetchPage(), collector, deposit,
        expected_identities=identities, as_of=date(2026, 8, 30),
    )
    store = BankStore("ctbc", user_id=7, source_account_id=91)
    try:
        delta = persist_collected(
            "ctbc",
            {
                "summary": {},
                "twd_deposit": deposit,
                "twd_history": result["accounts"],
                "history_coverage": result["coverage"],
                "card_api_dump": {},
            },
            store,
        )
        assert delta["twd_txn_new"] == 1
        assert store.latest_twd_transaction_dates() == {"acct-a": date(2026, 8, 30)}
    finally:
        store.close()
