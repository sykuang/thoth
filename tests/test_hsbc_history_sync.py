from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import inspect
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

import backend.banks.hsbc as hsbc_module
from backend.banks.hsbc import HsbcCrawler
from backend.core.base import BankCollectResult, ResponseCollector
from backend.core.persist import persist_collected
from backend.core.store import BankStore


def _crawler() -> HsbcCrawler:
    crawler = object.__new__(HsbcCrawler)
    crawler.name = "hsbc"
    crawler.transaction_cursors = {}
    return crawler


def test_hsbc_opts_into_card_billed_history_only() -> None:
    assert HsbcCrawler.HISTORY_COVERAGE_REQUIRED is True
    assert frozenset({
        "card_billed_transactions",
    }) == HsbcCrawler.HISTORY_COVERAGE_DOMAINS


def test_hsbc_full_and_incremental_ranges_use_card_cursor(monkeypatch) -> None:
    crawler = _crawler()
    end = date(2026, 8, 31)
    identity = "4029-****-****-7034"

    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    assert crawler._card_history_range(identity, end=end) == (
        date(2025, 9, 1),
        end,
    )

    crawler.configure_transaction_cursor(
        "card_billed_transactions",
        {identity: date(2026, 8, 20)},
    )
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "incremental")
    assert crawler._card_history_range(identity, end=end) == (
        date(2026, 8, 13),
        end,
    )

    crawler.configure_transaction_cursor(
        "card_billed_transactions",
        {identity: date(2026, 9, 5)},
    )
    with pytest.raises(RuntimeError, match="hsbc-history-cursor"):
        crawler._card_history_range(identity, end=end)


def _card_hit(cards: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(
        url="https://card.hsbc.com.tw/ibk-bff/api/v1/cards",
        method="GET",
        status=200,
        content_type="application/json; charset=utf-8",
        body_size=100,
        resp_json={"success": True, "payload": cards, "error": None},
    )


def _cards() -> list[dict]:
    return [{
        "id": "card-id-7034",
        "maskedCardNumber": "4029-****-****-7034",
        "name": "HSBC card",
        "cardStatusDisplay": "ACTIVATED",
    }]


def test_hsbc_card_inventory_is_exact_and_authoritative() -> None:
    cards = _cards()
    collector = SimpleNamespace(hits=[_card_hit(cards)])

    assert HsbcCrawler._card_inventory(collector) == cards


@pytest.mark.parametrize(
    ("mutation", "guard"),
    (
        ("wrong-url", "hsbc-card-inventory-missing"),
        ("wrong-method", "hsbc-card-inventory-envelope"),
        ("wrong-status", "hsbc-card-inventory-envelope"),
        ("wrong-content-type", "hsbc-card-inventory-envelope"),
        ("jsonp-content-type", "hsbc-card-inventory-envelope"),
        ("missing-content-type", "hsbc-card-inventory-envelope"),
        ("api-failure", "hsbc-card-inventory-envelope"),
        ("non-list", "hsbc-card-inventory-envelope"),
        ("missing-id", "hsbc-card-inventory-identity"),
        ("unsafe-id", "hsbc-card-inventory-identity"),
        ("missing-mask", "hsbc-card-inventory-identity"),
        ("unmasked-pan", "hsbc-card-inventory-identity"),
        ("alternate-mask-format", "hsbc-card-inventory-identity"),
        ("oversized-name", "hsbc-card-inventory-identity"),
        ("oversized-body", "hsbc-card-inventory-byte-budget"),
        ("unknown-status", "hsbc-card-inventory-identity"),
        ("duplicate-id", "hsbc-card-inventory-identity"),
        ("duplicate-mask", "hsbc-card-inventory-identity"),
        ("aggregate-body", "hsbc-card-inventory-byte-budget"),
        ("conflicting-replay", "hsbc-card-inventory-replay"),
        ("non-dict-card", "hsbc-card-inventory-row"),
        ("too-many-cards", "hsbc-card-inventory-count"),
        ("missing-hit", "hsbc-card-inventory-missing"),
    ),
)
def test_hsbc_card_inventory_rejects_ambiguous_or_malformed_source(
    mutation: str, guard: str,
) -> None:
    assert guard in HsbcCrawler.SAFE_COLLECT_GUARDS
    hit = _card_hit(_cards())
    hits = [hit]
    if mutation == "missing-hit":
        hits = []
    elif mutation == "wrong-url":
        hit.url += "/attacker"
    elif mutation == "wrong-method":
        hit.method = "POST"
    elif mutation == "wrong-status":
        hit.status = 204
    elif mutation == "wrong-content-type":
        hit.content_type = "text/html"
    elif mutation == "jsonp-content-type":
        hit.content_type = "application/jsonp"
    elif mutation == "missing-content-type":
        hit.content_type = None
    elif mutation == "api-failure":
        hit.resp_json["success"] = False
    elif mutation == "non-list":
        hit.resp_json["payload"] = {}
    elif mutation == "missing-id":
        hit.resp_json["payload"][0].pop("id")
    elif mutation == "unsafe-id":
        hit.resp_json["payload"][0]["id"] = "../card"
    elif mutation == "missing-mask":
        hit.resp_json["payload"][0].pop("maskedCardNumber")
    elif mutation == "unmasked-pan":
        hit.resp_json["payload"][0]["maskedCardNumber"] = "4029123412347034"
    elif mutation == "alternate-mask-format":
        hit.resp_json["payload"][0]["maskedCardNumber"] = "4029 **** **** 7034"
    elif mutation == "oversized-name":
        hit.resp_json["payload"][0]["name"] = "x" * 257
    elif mutation == "oversized-body":
        hit.body_size = 5_000_001
    elif mutation == "unknown-status":
        hit.resp_json["payload"][0]["cardStatusDisplay"] = "TYPO"
    elif mutation == "duplicate-id":
        hit.resp_json["payload"].append({
            "id": "card-id-7034",
            "maskedCardNumber": "4029-****-****-9999",
        })
    elif mutation == "duplicate-mask":
        hit.resp_json["payload"].append({
            "id": "card-id-9999",
            "maskedCardNumber": "4029-****-****-7034",
        })
    elif mutation == "non-dict-card":
        hit.resp_json["payload"][0] = []
    elif mutation == "too-many-cards":
        hit.resp_json["payload"] = [
            {
                "id": f"card-{index}",
                "maskedCardNumber": f"4029-****-****-{index:04d}",
                "cardStatusDisplay": "ACTIVATED",
            }
            for index in range(101)
        ]
    elif mutation == "aggregate-body":
        hit.body_size = 3_000_000
        other = _card_hit(deepcopy(_cards()))
        other.url += "/suspend"
        other.body_size = 3_000_000
        hits.append(other)
    else:
        other = _card_hit(deepcopy(_cards()))
        other.resp_json["payload"][0]["id"] = "card-id-replaced"
        hits.append(other)

    with pytest.raises(RuntimeError, match=f"^{guard}$"):
        HsbcCrawler._card_inventory(SimpleNamespace(hits=hits))


def _posted_row(day: date) -> dict:
    return {
        "description": f"purchase-{day.isoformat()}",
        "postedDate": f"{day.isoformat()}T00:00",
        "transactionDate": f"{day.isoformat()}T00:00",
        "ntdAmount": "100 TWD",
        "isPositive": True,
        "isForeign": False,
    }


def _page_response(card_id: str, page_number: int, total_pages: int, rows: list[dict]) -> dict:
    return {
        "url": (
            "https://card.hsbc.com.tw/ibk-bff/api/v1/cards/"
            f"{card_id}/transactions/posted?pageSize=10&pageNumber={page_number}"
        ),
        "status": 200,
        "contentType": "application/json; charset=utf-8",
        "redirected": False,
        "body": {
            "success": True,
            "error": None,
            "payload": {
                "pageInfo": {
                    "currentPageIndex": page_number,
                    "totalPages": total_pages,
                },
                "content": rows,
            },
        },
    }


class _Page:
    def __init__(self, responses: list[dict]):
        self.responses = responses
        self.response_index = 0
        self.args = []
        self.waits = []

    def evaluate(self, _script: str, args: dict):
        self.args.append(args)
        response = deepcopy(self.responses[self.response_index % len(self.responses)])
        self.response_index += 1
        response.setdefault(
            "bytes",
            len(json.dumps(response.get("body"), ensure_ascii=False).encode("utf-8")),
        )
        return response

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)


def test_hsbc_inventory_rejects_query_filtered_or_redirected_source() -> None:
    def collect(url: str, *, redirected: bool = False) -> ResponseCollector:
        collector = ResponseCollector("card.hsbc.com.tw")
        request = SimpleNamespace(
            headers={},
            method="GET",
            post_data=None,
            redirected_from=object() if redirected else None,
        )
        collector._on_response(SimpleNamespace(
            url=url,
            request=request,
            headers={"content-type": "application/json", "content-length": "100"},
            status=200,
            json=lambda: {"success": True, "payload": _cards(), "error": None},
        ))
        return collector

    for collector in (
        collect("https://card.hsbc.com.tw/ibk-bff/api/v1/cards?activeOnly=true"),
        collect("https://card.hsbc.com.tw/ibk-bff/api/v1/cards", redirected=True),
    ):
        with pytest.raises(RuntimeError, match="hsbc-card-inventory"):
            HsbcCrawler._card_inventory(collector)


def test_response_collector_binds_authorization_to_exact_https_host() -> None:
    request = SimpleNamespace(
        headers={"authorization": "Bearer synthetic-token"},
        method="GET",
        post_data=None,
        url="https://card.hsbc.com.tw/api/cards",
        redirected_from=None,
    )
    collector = ResponseCollector("hsbc.com.tw")
    attacker = SimpleNamespace(
        url="https://evil.example/path/hsbc.com.tw",
        request=request,
        headers={"content-type": "application/json"},
        status=200,
        json=lambda: {},
    )

    collector._on_response(attacker)

    assert collector.auth_token == ""
    assert collector.hits == []

    unauthorized = SimpleNamespace(
        url="https://card.hsbc.com.tw/api/session",
        request=SimpleNamespace(
            headers={"authorization": "Bearer stale-token"},
            method="GET",
            post_data=None,
            url="https://card.hsbc.com.tw/api/session",
            redirected_from=None,
        ),
        headers={"content-type": "application/json"},
        status=401,
        json=lambda: {},
    )
    collector._on_request(unauthorized.request)
    collector._on_response(unauthorized)
    assert collector.auth_token == ""

    exact = SimpleNamespace(
        url="https://card.hsbc.com.tw/api/cards",
        request=request,
        headers={"content-type": "application/json"},
        status=200,
        json=lambda: {},
    )
    collector._on_request(exact.request)
    collector._on_response(exact)

    assert collector.auth_token == "Bearer synthetic-token"
    assert collector.auth_token_url == "https://card.hsbc.com.tw/api/cards"
    assert len(collector.hits) == 2

    rotated = SimpleNamespace(
        url="https://card.hsbc.com.tw/api/cards/refresh",
        request=SimpleNamespace(
            headers={"authorization": "Bearer rotated-token"},
            method="GET",
            post_data=None,
            redirected_from=None,
            url="https://card.hsbc.com.tw/api/cards/refresh",
        ),
        headers={"content-type": "application/json"},
        status=200,
        json=lambda: {},
    )
    collector._on_request(rotated.request)
    collector._on_response(rotated)
    assert collector.auth_token == "Bearer rotated-token"
    assert collector.auth_token_url == rotated.url

    collector.auth_token_events = [
        {
            "token": "Bearer newer-token",
            "url": "https://card.hsbc.com.tw/ibk-bff/api/v1/cards",
            "redirected": False,
            "sequence": 3,
        },
        {
            "token": "Bearer older-token",
            "url": "https://card.hsbc.com.tw/ibk-bff/api/v1/cards",
            "redirected": False,
            "sequence": 2,
        },
        {
            "token": "Bearer redirected-token",
            "url": "https://card.hsbc.com.tw/ibk-bff/api/v1/cards",
            "redirected": True,
            "sequence": 4,
        },
    ]
    assert HsbcCrawler._history_token(collector) == "Bearer newer-token"


def test_response_collector_keeps_latest_issued_token_when_responses_reorder() -> None:
    collector = ResponseCollector("hsbc.com.tw")
    old_request = SimpleNamespace(
        headers={"authorization": "Bearer older-token"}, method="GET", post_data=None,
        url="https://card.hsbc.com.tw/ibk-bff/api/v1/cards", redirected_from=None,
    )
    new_request = SimpleNamespace(
        headers={"authorization": "Bearer newer-token"}, method="GET", post_data=None,
        url="https://card.hsbc.com.tw/ibk-bff/api/v1/cards", redirected_from=None,
    )
    collector._on_request(old_request)
    collector._on_request(new_request)
    for request in (new_request, old_request):
        collector._on_response(SimpleNamespace(
            url=request.url,
            request=request,
            headers={"content-type": "application/json", "content-length": "2"},
            status=200,
            json=lambda: {},
        ))

    assert collector.auth_token == "Bearer newer-token"
    assert HsbcCrawler._history_token(collector) == "Bearer newer-token"


def test_hsbc_direct_fetches_have_operation_local_deadlines() -> None:
    for function in (HsbcCrawler._fetch_json, HsbcCrawler._fetch_api_page):
        source = inspect.getsource(function)
        assert "AbortController" in source
        assert "signal:controller.signal" in source
        assert "clearTimeout(timer)" in source


def test_hsbc_embedded_fetch_javascript_compiles() -> None:
    scripts = []

    class Page:
        def evaluate(self, script, _args):
            scripts.append(script)
            return None

    page = Page()
    HsbcCrawler._fetch_json(page, "cards/card-id-7034", "Bearer synthetic-token")
    HsbcCrawler._fetch_api_page(
        page,
        url=(
            "https://card.hsbc.com.tw/ibk-bff/api/v1/cards/card-id-7034/"
            "transactions/posted?pageSize=10&pageNumber=0"
        ),
        token="Bearer synthetic-token",
        timeout_ms=30_000,
    )

    assert len(scripts) == 2
    assert all("content-length" in script for script in scripts)
    assert all("getReader()" in script for script in scripts)
    assert all("bytes>maxBytes" in script for script in scripts)
    assert all("TextDecoder" in script for script in scripts)
    assert all("r.text()" not in script for script in scripts)
    subprocess.run(
        [
            "node",
            "-e",
            "for(const s of JSON.parse(process.argv[1]))new Function('return ('+s+');')",
            json.dumps(scripts),
        ],
        check=True,
    )


def test_hsbc_failed_json_semantics_still_consume_operation_bytes() -> None:
    class Page:
        @staticmethod
        def evaluate(_script, _args):
            return {"payload": None, "bytes": 4_900_000}

    budget = [5_000_000]
    assert HsbcCrawler._fetch_json(
        Page(), "cards/card-id-7034", "Bearer synthetic-token", byte_budget=budget,
    ) is None
    assert budget == [100_000]


def test_hsbc_stream_overflow_exhausts_budget_when_cancel_rejects() -> None:
    class Page:
        @staticmethod
        def evaluate(script, args):
            runner = """
const fn=eval('('+process.argv[1]+')');
let sent=false;
global.fetch=async url=>({
  url,status:200,redirected:false,
  headers:{get:name=>name==='content-type'?'application/json':null},
  body:new ReadableStream({
    pull(controller){if(!sent){sent=true;controller.enqueue(new Uint8Array(2));}},
    cancel(){return Promise.reject(Error('cancel failed'));}
  })
});
fn(JSON.parse(process.argv[2])).then(value=>process.stdout.write(JSON.stringify(value)));
"""
            result = subprocess.run(
                ["node", "-e", runner, script, json.dumps(args)],
                check=True,
                capture_output=True,
                text=True,
            )
            return json.loads(result.stdout)

    budget = [1]
    assert HsbcCrawler._fetch_json(
        Page(), "cards/card-id-7034", "Bearer synthetic-token", byte_budget=budget,
    ) is None
    assert budget == [0]


def test_hsbc_detail_stream_overflow_aborts_before_posted_history(monkeypatch) -> None:
    crawler = _crawler()
    called = False

    def overflow(_page, _path, _token, *, byte_budget):
        byte_budget[0] = 0
        return None

    def fetch_history(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("posted fetch must not run")

    monkeypatch.setattr(crawler, "_fetch_json", overflow)
    monkeypatch.setattr(crawler, "_fetch_posted_history", fetch_history)
    collector = SimpleNamespace(auth_token_events=[{
        "token": "Bearer synthetic-token",
        "url": "https://card.hsbc.com.tw/ibk-bff/api/v1/cards",
        "redirected": False,
        "sequence": 1,
    }])
    with pytest.raises(RuntimeError, match="hsbc-history-byte-budget"):
        crawler._collect_card_details(
            SimpleNamespace(), collector, _cards(), end=date(2026, 8, 31),
        )
    assert called is False


def test_hsbc_direct_api_fetch_is_exact_and_never_logs_token_material() -> None:
    source = inspect.getsource(HsbcCrawler._fetch_json)
    assert "redirect:'error'" in source
    assert "r.url!==url" in source
    assert "r.status!==200" in source
    assert "token[:" not in inspect.getsource(HsbcCrawler._collect_card_details)


def test_hsbc_posted_fetch_refuses_redirects_before_sending_authorization() -> None:
    source = inspect.getsource(HsbcCrawler._fetch_api_page)
    assert "redirect:'error'" in source
    assert "redirect:'follow'" not in source


def test_hsbc_posted_history_has_total_operation_deadline(monkeypatch) -> None:
    clock = iter((0.0, 121.0))
    monkeypatch.setattr(hsbc_module.time, "monotonic", lambda: next(clock))
    page = _Page([])

    with pytest.raises(RuntimeError, match="hsbc-posted-history"):
        HsbcCrawler._fetch_posted_history(
            page,
            card_id="card-id-7034",
            identity="4029-****-****-7034",
            token="Bearer synthetic-token",
            start=date(2025, 9, 1),
            end=date(2026, 8, 31),
        )

    assert page.args == []


def test_hsbc_posted_history_rechecks_deadline_after_fetch(monkeypatch) -> None:
    clock = iter((0.0, 119.0, 121.0))
    monkeypatch.setattr(hsbc_module.time, "monotonic", lambda: next(clock))
    page = _Page([_page_response("card-id-7034", 0, 1, [])])

    with pytest.raises(RuntimeError, match="hsbc-posted-history"):
        HsbcCrawler._fetch_posted_history(
            page,
            card_id="card-id-7034",
            identity="4029-****-****-7034",
            token="Bearer synthetic-token",
            start=date(2025, 9, 1),
            end=date(2026, 8, 31),
        )


def test_hsbc_incremental_history_filters_after_complete_bank_pagination() -> None:
    card_id = "card-id-7034"
    identity = "4029-****-****-7034"
    end = date(2026, 8, 31)
    start = date(2026, 8, 13)
    first = [_posted_row(end - timedelta(days=offset)) for offset in range(10)]
    second = [_posted_row(end - timedelta(days=offset)) for offset in range(10, 20)]
    third = [_posted_row(end - timedelta(days=offset)) for offset in range(20, 30)]
    fourth = [_posted_row(end - timedelta(days=30))]
    page = _Page([
        _page_response(card_id, 0, 4, first),
        _page_response(card_id, 1, 4, second),
        _page_response(card_id, 2, 4, third),
        _page_response(card_id, 3, 4, fourth),
    ])

    result = HsbcCrawler._fetch_posted_history(
        page,
        card_id=card_id,
        identity=identity,
        token="Bearer synthetic-token",
        start=start,
        end=end,
    )

    assert len(result["rows"]) == 19
    assert result["receipt"] == {
        "identity": identity,
        "start": "2026-08-13",
        "end": "2026-08-31",
        "status": "complete",
        "pages": 4,
        "rows": 19,
    }
    assert [args["url"].rsplit("=", 1)[-1] for args in page.args] == [
        "0", "1", "2", "3", "0", "1", "2", "3",
    ]
    assert page.waits == [400, 400, 400]


def test_hsbc_snapshot_replay_ignores_volatile_envelope_fields() -> None:
    card_id = "card-id-7034"
    first = _page_response(card_id, 0, 1, [_posted_row(date(2026, 8, 31))])
    replay = deepcopy(first)
    first["body"]["requestId"] = "first"
    replay["body"]["requestId"] = "second"

    result = HsbcCrawler._fetch_posted_history(
        _Page([first, replay]),
        card_id=card_id,
        identity="4029-****-****-7034",
        token="Bearer synthetic-token",
        start=date(2025, 9, 1),
        end=date(2026, 8, 31),
    )

    assert result["receipt"]["status"] == "complete"


def test_hsbc_history_caps_total_response_bytes_including_replay_envelopes() -> None:
    card_id = "card-id-7034"
    first = _page_response(
        card_id, 0, 2, [_posted_row(date(2026, 8, 31)) for _ in range(10)],
    )
    second = _page_response(card_id, 1, 2, [_posted_row(date(2026, 8, 30))])
    first["body"]["padding"] = "x" * 1_500_000
    second["body"]["padding"] = "y" * 1_500_000

    with pytest.raises(RuntimeError, match="hsbc-posted-history"):
        HsbcCrawler._fetch_posted_history(
            _Page([first, second]),
            card_id=card_id,
            identity="4029-****-****-7034",
            token="Bearer synthetic-token",
            start=date(2025, 9, 1),
            end=date(2026, 8, 31),
        )


def test_hsbc_history_preserves_identical_occurrences_across_pages() -> None:
    card_id = "card-id-7034"
    row = _posted_row(date(2026, 8, 31))
    rows = [deepcopy(row) for _ in range(10)]
    page = _Page([
        _page_response(card_id, 0, 2, rows),
        _page_response(card_id, 1, 2, rows),
    ])

    result = HsbcCrawler._fetch_posted_history(
        page,
        card_id=card_id,
        identity="4029-****-****-7034",
        token="Bearer synthetic-token",
        start=date(2025, 9, 1),
        end=date(2026, 8, 31),
    )

    assert len(result["rows"]) == 20


def test_hsbc_history_rejects_cross_page_snapshot_drift() -> None:
    card_id = "card-id-7034"
    end = date(2026, 8, 31)

    def named(name: str, offset: int) -> dict:
        row = _posted_row(end - timedelta(days=offset))
        row["description"] = name
        return row

    first_page = [named(chr(ord("A") + index), index) for index in range(10)]
    second_page = [named(chr(ord("L") + index), 11 + index) for index in range(9)]
    changed_first = [first_page[0], *first_page[2:], named("K", 10)]
    page = _Page([
        _page_response(card_id, 0, 2, first_page),
        _page_response(card_id, 1, 2, second_page),
        _page_response(card_id, 0, 2, changed_first),
        _page_response(card_id, 1, 2, second_page),
    ])

    with pytest.raises(RuntimeError, match="hsbc-posted-history"):
        HsbcCrawler._fetch_posted_history(
            page,
            card_id=card_id,
            identity="4029-****-****-7034",
            token="Bearer synthetic-token",
            start=date(2025, 9, 1),
            end=end,
        )


def test_hsbc_history_does_not_assume_unproven_page_sort_order() -> None:
    card_id = "card-id-7034"
    end = date(2026, 8, 31)
    rows = [
        _posted_row(date(2026, 8, 31)),
        _posted_row(date(2026, 8, 29)),
        _posted_row(date(2026, 8, 30)),
    ]

    result = HsbcCrawler._fetch_posted_history(
        _Page([_page_response(card_id, 0, 1, rows)]),
        card_id=card_id,
        identity="4029-****-****-7034",
        token="Bearer synthetic-token",
        start=date(2026, 8, 30),
        end=end,
    )

    assert [row["postedDate"] for row in result["rows"]] == [
        "2026-08-31T00:00",
        "2026-08-30T00:00",
    ]


def test_hsbc_posted_history_accepts_bank_zero_page_empty_envelope() -> None:
    card_id = "card-id-7034"
    page = _Page([_page_response(card_id, 0, 0, [])])

    result = HsbcCrawler._fetch_posted_history(
        page,
        card_id=card_id,
        identity="4029-****-****-7034",
        token="Bearer synthetic-token",
        start=date(2025, 9, 1),
        end=date(2026, 8, 31),
    )

    assert result["rows"] == []
    assert result["receipt"]["status"] == "explicit_empty"
    assert result["receipt"]["pages"] == 1


def test_hsbc_posted_history_accepts_exact_empty_first_page() -> None:
    card_id = "card-id-7034"
    identity = "4029-****-****-7034"
    page = _Page([_page_response(card_id, 0, 1, [])])

    result = HsbcCrawler._fetch_posted_history(
        page,
        card_id=card_id,
        identity=identity,
        token="Bearer synthetic-token",
        start=date(2025, 9, 1),
        end=date(2026, 8, 31),
    )

    assert result == {
        "rows": [],
        "receipt": {
            "identity": identity,
            "start": "2025-09-01",
            "end": "2026-08-31",
            "status": "explicit_empty",
            "pages": 1,
            "rows": 0,
        },
    }


@pytest.mark.parametrize(
    "mutation",
    (
        "unsafe-card-id",
        "missing-token",
        "wrong-url",
        "wrong-status",
        "wrong-content-type",
        "jsonp-content-type",
        "redirected",
        "api-failure",
        "wrong-page-index",
        "bad-total-pages",
        "changed-total-pages",
        "non-list-content",
        "oversized-page",
        "short-nonfinal-page",
        "empty-before-last-page",

        "future-row",
        "invalid-transaction-date",
        "invalid-transaction-time",
        "invalid-amount",
        "negative-amount",
        "scientific-amount",
        "fractional-twd",
        "invalid-direction",
        "invalid-foreign-flag",
        "invalid-foreign-amount",
        "foreign-flag-with-twd",
        "foreign-amount-on-domestic",
        "future-transaction-date",
        "empty-description",
        "oversized-description",
    ),
)
def test_hsbc_posted_history_fails_closed_on_transport_or_pagination_drift(
    mutation: str,
) -> None:
    card_id = "card-id-7034"
    end = date(2026, 8, 31)
    rows = [_posted_row(end - timedelta(days=offset)) for offset in range(10)]
    responses = [
        _page_response(card_id, 0, 2, rows),
        _page_response(card_id, 1, 2, [_posted_row(date(2026, 8, 21))]),
    ]
    token = "Bearer synthetic-token"
    if mutation == "unsafe-card-id":
        card_id = "../card"
    elif mutation == "missing-token":
        token = ""
    elif mutation == "wrong-url":
        responses[0]["url"] += "/attacker"
    elif mutation == "wrong-status":
        responses[0]["status"] = 204
    elif mutation == "wrong-content-type":
        responses[0]["contentType"] = "text/html"
    elif mutation == "jsonp-content-type":
        responses[0]["contentType"] = "application/jsonp"
    elif mutation == "redirected":
        responses[0]["redirected"] = True
    elif mutation == "api-failure":
        responses[0]["body"]["success"] = False
    elif mutation == "wrong-page-index":
        responses[0]["body"]["payload"]["pageInfo"]["currentPageIndex"] = 1
    elif mutation == "bad-total-pages":
        responses[0]["body"]["payload"]["pageInfo"]["totalPages"] = "2"
    elif mutation == "changed-total-pages":
        responses[1]["body"]["payload"]["pageInfo"]["totalPages"] = 3
    elif mutation == "non-list-content":
        responses[0]["body"]["payload"]["content"] = {}
    elif mutation == "oversized-page":
        responses[0]["body"]["payload"]["content"].append(_posted_row(date(2026, 8, 1)))
    elif mutation == "short-nonfinal-page":
        responses[0]["body"]["payload"]["content"].pop()
    elif mutation == "empty-before-last-page":
        responses[0]["body"]["payload"]["content"] = []

    elif mutation == "future-row":
        responses[0]["body"]["payload"]["content"][0]["postedDate"] = "2026-09-01T00:00"
    elif mutation == "invalid-transaction-date":
        responses[0]["body"]["payload"]["content"][0]["transactionDate"] = "2026-02-30T00:00"
    elif mutation == "invalid-transaction-time":
        responses[0]["body"]["payload"]["content"][0]["transactionDate"] = "2026-08-30T99:99"
    elif mutation == "invalid-amount":
        responses[0]["body"]["payload"]["content"][0]["ntdAmount"] = "NaN TWD"
    elif mutation == "negative-amount":
        responses[0]["body"]["payload"]["content"][0]["ntdAmount"] = "-100 TWD"
    elif mutation == "scientific-amount":
        responses[0]["body"]["payload"]["content"][0]["ntdAmount"] = "1e3 TWD"
    elif mutation == "fractional-twd":
        responses[0]["body"]["payload"]["content"][0]["ntdAmount"] = "0.1 TWD"
    elif mutation == "invalid-direction":
        responses[0]["body"]["payload"]["content"][0]["isPositive"] = "true"
    elif mutation == "invalid-foreign-flag":
        responses[0]["body"]["payload"]["content"][0]["isForeign"] = 0
    elif mutation == "invalid-foreign-amount":
        row = responses[0]["body"]["payload"]["content"][0]
        row["isForeign"] = True
        row["foreignAmount"] = "NaN USD"
    elif mutation == "foreign-flag-with-twd":
        row = responses[0]["body"]["payload"]["content"][0]
        row["isForeign"] = True
        row["foreignAmount"] = "100 TWD"
    elif mutation == "foreign-amount-on-domestic":
        responses[0]["body"]["payload"]["content"][0]["foreignAmount"] = "12.34 USD"
    elif mutation == "future-transaction-date":
        responses[0]["body"]["payload"]["content"][0]["transactionDate"] = (
            "9999-12-31T00:00"
        )
    elif mutation == "empty-description":
        responses[0]["body"]["payload"]["content"][0]["description"] = " "
    elif mutation == "oversized-description":
        responses[0]["body"]["payload"]["content"][0]["description"] = "x" * 513

    with pytest.raises(RuntimeError, match="hsbc-posted-history"):
        HsbcCrawler._fetch_posted_history(
            _Page(responses),
            card_id=card_id,
            identity="4029-****-****-7034",
            token=token,
            start=date(2026, 8, 1),
            end=end,
        )


def test_hsbc_detail_fetch_requires_token_from_exact_card_origin(monkeypatch) -> None:
    crawler = _crawler()
    assert crawler._host_filter() == "card.hsbc.com.tw"
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    for token_url in (
        "https://api.hsbc.com.tw/session",
        "https://card.hsbc.com.tw/public/ping",
    ):
        collector = SimpleNamespace(auth_token_events=[{
            "token": "Bearer synthetic-token",
            "url": token_url,
            "redirected": False,
            "sequence": 1,
        }])
        with pytest.raises(RuntimeError, match="hsbc-history-token"):
            crawler._collect_card_details(
                SimpleNamespace(), collector, [], end=date(2026, 8, 31),
            )


def test_hsbc_card_detail_collection_publishes_per_card_coverage(monkeypatch) -> None:
    crawler = _crawler()
    cards = _cards()
    identity = cards[0]["maskedCardNumber"]
    end = date(2026, 8, 31)
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    history = {
        "rows": [_posted_row(end)],
        "receipt": {
            "identity": identity,
            "start": "2025-09-01",
            "end": "2026-08-31",
            "status": "complete",
            "pages": 1,
            "rows": 1,
        },
    }
    calls = []

    def fetch_history(_page, **kwargs):
        calls.append(kwargs)
        return deepcopy(history)

    monkeypatch.setattr(crawler, "_fetch_posted_history", fetch_history)
    monkeypatch.setattr(
        crawler,
        "_fetch_json",
        lambda _page, path, _token, **_kwargs: (
            [] if "unposted" in path else {"details": []}
        ),
    )
    collector = SimpleNamespace(auth_token_events=[{
        "token": "Bearer synthetic-token",
        "url": "https://card.hsbc.com.tw/ibk-bff/api/v1/cards",
        "redirected": False,
        "sequence": 1,
    }])

    details, coverage = crawler._collect_card_details(
        SimpleNamespace(), collector, cards, end=end,
    )

    assert list(details) == [identity]
    assert details[identity]["posted"] == history["rows"]
    assert details[identity]["posted_receipt"] == history["receipt"]
    assert details[identity]["unposted_ok"] is True
    assert calls == [{
        "card_id": "card-id-7034",
        "identity": identity,
        "token": "Bearer synthetic-token",
        "start": date(2025, 9, 1),
        "end": end,
        "byte_budget": [5_000_000],
    }]
    assert coverage == {
        "version": 1,
        "mode": "full",
        "domains": [{
            "domain": "card_billed_transactions",
            "expected": [{
                "identity": identity,
                "start": "2025-09-01",
                "end": "2026-08-31",
            }],
            "windows": [history["receipt"]],
        }],
    }


def test_hsbc_empty_card_inventory_has_explicit_empty_domain(monkeypatch) -> None:
    crawler = _crawler()
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "incremental")

    details, coverage = crawler._collect_card_details(
        SimpleNamespace(),
        SimpleNamespace(auth_token_events=[{
            "token": "Bearer synthetic-token",
            "url": "https://card.hsbc.com.tw/ibk-bff/api/v1/cards",
            "redirected": False,
            "sequence": 1,
        }]),
        [],
        end=date(2026, 8, 31),
    )

    assert details == {}
    assert coverage == {
        "version": 1,
        "mode": "incremental",
        "domains": [{
            "domain": "card_billed_transactions",
            "expected": [],
            "windows": [],
            "empty_window": {
                "start": "2025-09-01",
                "end": "2026-08-31",
                "status": "explicit_empty",
                "pages": 1,
            },
        }],
    }


@pytest.fixture
def store(tmp_path: Path, monkeypatch):
    from backend.core import store as store_module

    monkeypatch.setattr(store_module, "DATA_ROOT", tmp_path)
    value = BankStore("hsbc", user_id=1, source_account_id=7)
    yield value
    value.close()


def _persist_payload() -> dict:
    identity = "4029-****-****-7034"
    receipt = {
        "identity": identity,
        "start": "2025-09-01",
        "end": "2026-08-31",
        "status": "complete",
        "pages": 1,
        "rows": 1,
    }
    return {
        "cards": _cards(),
        "card_detail": {
            identity: {
                "card_id": "card-id-7034",
                "masked": identity,
                "detail": {"details": []},
                "posted": [_posted_row(date(2026, 8, 31))],
                "posted_receipt": deepcopy(receipt),
                "unposted": [],
                "unposted_ok": True,
            },
        },
        "history_coverage": {
            "version": 1,
            "mode": "full",
            "domains": [{
                "domain": "card_billed_transactions",
                "expected": [{
                    "identity": identity,
                    "start": "2025-09-01",
                    "end": "2026-08-31",
                }],
                "windows": [deepcopy(receipt)],
            }],
        },
        "card_bill_facts_ok": False,
    }


def test_hsbc_persistence_revalidates_history_before_writing(store) -> None:
    payload = _persist_payload()

    delta = persist_collected("hsbc", payload, store)

    assert delta["card_billed_new"] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM card_billed_txns").fetchone()[0] == 1
    assert store.latest_card_transaction_dates() == {
        "4029-****-****-7034": date(2026, 8, 31),
    }


def test_hsbc_direct_persister_remains_durable(tmp_path: Path, monkeypatch) -> None:
    from backend.core import store as store_module
    from backend.core.persist.hsbc import persist_hsbc

    monkeypatch.setattr(store_module, "DATA_ROOT", tmp_path)
    first = BankStore("hsbc", user_id=1, source_account_id=7)
    persist_hsbc(_persist_payload(), first)
    first.close()

    reopened = BankStore("hsbc", user_id=1, source_account_id=7)
    try:
        assert reopened.conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1
        assert reopened.conn.execute("SELECT COUNT(*) FROM card_billed_txns").fetchone()[0] == 1
        assert reopened.conn.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0] == 1
    finally:
        reopened.close()


def test_hsbc_persistence_rolls_back_cards_when_billed_write_fails(store, monkeypatch) -> None:
    def fail_billed(*_args, **_kwargs):
        raise RuntimeError("synthetic billed failure")

    monkeypatch.setattr(store, "upsert_card_billed", fail_billed)

    with pytest.raises(RuntimeError, match="synthetic billed failure"):
        persist_collected("hsbc", _persist_payload(), store)

    assert store.conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0


def test_hsbc_persistence_rolls_back_after_pending_before_metrics(store, monkeypatch) -> None:
    payload = _persist_payload()
    pending = _posted_row(date(2026, 8, 30))
    pending["postedDate"] = "0002-11-30T00:00"
    payload["card_detail"]["4029-****-****-7034"]["unposted"] = [pending]

    def fail_metric(*_args, **_kwargs):
        raise RuntimeError("synthetic metric failure")

    monkeypatch.setattr(store, "put_daily_metric", fail_metric)
    with pytest.raises(RuntimeError, match="synthetic metric failure"):
        persist_collected("hsbc", payload, store)

    for table in ("cards", "card_billed_txns", "card_pending_txns", "daily_metrics", "sync_log"):
        assert store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert store.latest_card_transaction_dates() == {}


def test_hsbc_persistence_rolls_back_when_cursor_write_fails(store, monkeypatch) -> None:
    def fail_cursor(*_args, **_kwargs):
        raise RuntimeError("synthetic cursor failure")

    monkeypatch.setattr(store, "record_history_coverage_cursors", fail_cursor)
    with pytest.raises(RuntimeError, match="synthetic cursor failure"):
        persist_collected("hsbc", _persist_payload(), store)

    for table in ("cards", "card_billed_txns", "card_pending_txns", "daily_metrics", "sync_log"):
        assert store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert store.latest_card_transaction_dates() == {}


def test_hsbc_persistence_uses_full_identity_for_details_and_pending(store) -> None:
    payload = _persist_payload()
    identity = payload["cards"][0]["maskedCardNumber"]
    detail = payload["card_detail"][identity]
    detail["detail"] = {
        "details": [{"key": "Credit Limit", "value": "1,500,000 TWD"}],
    }
    pending = _posted_row(date(2026, 8, 30))
    pending["postedDate"] = "0002-11-30T00:00"
    pending["description"] = "pending-purchase"
    detail["unposted"] = [pending]

    persist_collected("hsbc", payload, store)

    card = store.conn.execute(
        "SELECT credit_limit FROM cards WHERE card_no = ?", (identity,),
    ).fetchone()
    assert card["credit_limit"] == 1_500_000
    assert store.conn.execute(
        "SELECT COUNT(*) FROM card_pending_txns WHERE card_no = ?", (identity,),
    ).fetchone()[0] == 1
    categories = {
        row[0] for row in store.conn.execute("SELECT category FROM daily_metrics")
    }
    assert "card_detail_4029_7034" in categories
    assert "card_detail_7034" not in categories
    assert f"card_detail_{identity}" not in categories


def test_hsbc_authoritative_empty_inventory_survives_collect_serialization(store) -> None:
    coverage = {
        "version": 1,
        "mode": "full",
        "domains": [{
            "domain": "card_billed_transactions",
            "expected": [],
            "windows": [],
            "empty_window": {
                "start": "2025-09-01",
                "end": "2026-08-31",
                "status": "explicit_empty",
                "pages": 1,
            },
        }],
    }
    payload = BankCollectResult(
        cards=[],
        card_detail={},
        history_coverage=coverage,
        card_bill_facts_ok=False,
    ).to_dict()

    delta = persist_collected("hsbc", payload, store)

    assert delta["card_billed_new"] == 0
    assert store.latest_card_transaction_dates() == {}


def test_hsbc_incremental_persistence_binds_existing_cursor_start(store, monkeypatch) -> None:
    payload = _persist_payload()
    identity = payload["cards"][0]["maskedCardNumber"]
    payload["history_coverage"]["mode"] = "incremental"
    detail = payload["card_detail"][identity]
    domain = payload["history_coverage"]["domains"][0]
    for item in (detail["posted_receipt"], domain["expected"][0], domain["windows"][0]):
        item["start"] = "2026-08-31"
    monkeypatch.setattr(
        store, "latest_card_transaction_dates", lambda: {identity: date(2026, 8, 1)},
    )

    with pytest.raises(ValueError, match="HSBC history"):
        persist_collected("hsbc", payload, store)

    assert store.conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0


@pytest.mark.parametrize("mode", ("full", "incremental"))
def test_hsbc_persistence_rejects_future_existing_cursor(store, monkeypatch, mode) -> None:
    payload = _persist_payload()
    identity = payload["cards"][0]["maskedCardNumber"]
    payload["history_coverage"]["mode"] = mode
    detail = payload["card_detail"][identity]
    domain = payload["history_coverage"]["domains"][0]
    if mode == "incremental":
        for item in (
            detail["posted_receipt"], domain["expected"][0], domain["windows"][0],
        ):
            item["start"] = "2026-08-29"
    monkeypatch.setattr(
        store, "latest_card_transaction_dates", lambda: {identity: date(2026, 9, 5)},
    )

    with pytest.raises(ValueError, match="HSBC history cursor"):
        persist_collected("hsbc", payload, store)

    assert store.conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0


def test_hsbc_empty_inventory_rejects_short_full_window_before_write(store) -> None:
    coverage = {
        "version": 1,
        "mode": "full",
        "domains": [{
            "domain": "card_billed_transactions",
            "expected": [],
            "windows": [],
            "empty_window": {
                "start": "2026-08-31",
                "end": "2026-08-31",
                "status": "explicit_empty",
                "pages": 1,
            },
        }],
    }
    payload = BankCollectResult(
        cards=[],
        card_detail={},
        history_coverage=coverage,
        card_bill_facts_ok=False,
    ).to_dict()

    with pytest.raises(ValueError, match="HSBC history"):
        persist_collected("hsbc", payload, store)


@pytest.mark.parametrize(
    "mutation",
    (
        "missing-coverage",
        "wrong-domain",
        "missing-detail",
        "wrong-detail-key",
        "wrong-masked",
        "wrong-card-id",
        "unsafe-card-id",
        "invalid-mask",
        "unmasked-pan",
        "alternate-mask-format",
        "missing-receipt",
        "receipt-mismatch",
        "row-count",
        "status",
        "row-outside-window",
        "invalid-posted-date",
        "invalid-transaction-date",
        "invalid-transaction-time",
        "invalid-amount",
        "negative-amount",
        "scientific-amount",
        "fractional-twd",
        "invalid-direction",
        "future-transaction-date",
        "invalid-foreign-amount",
        "foreign-flag-with-twd",
        "foreign-amount-on-domestic",
        "invalid-unposted",
        "oversized-description",
        "short-full-window",
        "excessive-pages",
        "negative-credit-limit",
        "infinite-outstanding",
        "fractional-outstanding",
        "malformed-outstanding",
        "fractional-credit-limit",
        "invalid-detail-date",
        "invalid-card-date",
        "duplicate-detail-key",
        "posted-unposted-row",
        "garbage-unposted-placeholder",
        "alternate-unposted-placeholder",
        "missing-card-status",
        "future-coverage-end",
    ),
)
def test_hsbc_persistence_rejects_unbound_or_malformed_history_before_write(
    mutation: str,
    store,
) -> None:
    payload = _persist_payload()
    identity = payload["cards"][0]["maskedCardNumber"]
    detail = payload["card_detail"][identity]
    domain = payload["history_coverage"]["domains"][0]
    if mutation == "missing-coverage":
        payload.pop("history_coverage")
    elif mutation == "wrong-domain":
        domain["domain"] = "twd_transactions"
    elif mutation == "missing-detail":
        payload["card_detail"] = {}
    elif mutation == "wrong-detail-key":
        payload["card_detail"] = {"4029-****-****-9999": detail}
    elif mutation == "wrong-masked":
        detail["masked"] = "4029-****-****-9999"
    elif mutation == "wrong-card-id":
        detail["card_id"] = "card-id-9999"
    elif mutation == "unsafe-card-id":
        payload["cards"][0]["id"] = "../card"
        detail["card_id"] = "../card"
    elif mutation == "invalid-mask":
        new_identity = "evil"
        payload["cards"][0]["maskedCardNumber"] = new_identity
        payload["card_detail"] = {new_identity: detail}
        detail["masked"] = new_identity
        detail["posted_receipt"]["identity"] = new_identity
        domain["expected"][0]["identity"] = new_identity
        domain["windows"][0]["identity"] = new_identity
    elif mutation == "unmasked-pan":
        new_identity = "4029123412347034"
        payload["cards"][0]["maskedCardNumber"] = new_identity
        payload["card_detail"] = {new_identity: detail}
        detail["masked"] = new_identity
        detail["posted_receipt"]["identity"] = new_identity
        domain["expected"][0]["identity"] = new_identity
        domain["windows"][0]["identity"] = new_identity
    elif mutation == "alternate-mask-format":
        new_identity = "4029 **** **** 7034"
        payload["cards"][0]["maskedCardNumber"] = new_identity
        payload["card_detail"] = {new_identity: detail}
        detail["masked"] = new_identity
        detail["posted_receipt"]["identity"] = new_identity
        domain["expected"][0]["identity"] = new_identity
        domain["windows"][0]["identity"] = new_identity
    elif mutation == "missing-receipt":
        detail.pop("posted_receipt")
    elif mutation == "receipt-mismatch":
        detail["posted_receipt"]["end"] = "2026-08-30"
    elif mutation == "row-count":
        detail["posted_receipt"]["rows"] = 2
        domain["windows"][0]["rows"] = 2
    elif mutation == "status":
        detail["posted_receipt"]["status"] = "explicit_empty"
        domain["windows"][0]["status"] = "explicit_empty"
    elif mutation == "row-outside-window":
        detail["posted"][0]["postedDate"] = "2025-08-31T00:00"
    elif mutation == "invalid-posted-date":
        detail["posted"][0]["postedDate"] = "2026-02-30T00:00"
    elif mutation == "invalid-transaction-date":
        detail["posted"][0]["transactionDate"] = "2026-02-30T00:00"
    elif mutation == "invalid-transaction-time":
        detail["posted"][0]["transactionDate"] = "2026-08-30T99:99"
    elif mutation == "invalid-amount":
        detail["posted"][0]["ntdAmount"] = "NaN TWD"
    elif mutation == "negative-amount":
        detail["posted"][0]["ntdAmount"] = "-100 TWD"
    elif mutation == "scientific-amount":
        detail["posted"][0]["ntdAmount"] = "1e3 TWD"
    elif mutation == "fractional-twd":
        detail["posted"][0]["ntdAmount"] = "0.1 TWD"
    elif mutation == "future-transaction-date":
        detail["posted"][0]["transactionDate"] = "9999-12-31T00:00"
    elif mutation == "invalid-foreign-amount":
        detail["posted"][0]["isForeign"] = True
        detail["posted"][0]["foreignAmount"] = "Infinity USD"
    elif mutation == "foreign-flag-with-twd":
        detail["posted"][0]["isForeign"] = True
        detail["posted"][0]["foreignAmount"] = "100 TWD"
    elif mutation == "foreign-amount-on-domestic":
        detail["posted"][0]["foreignAmount"] = "12.34 USD"
    elif mutation == "invalid-unposted":
        detail["unposted"] = "bad"
    elif mutation == "oversized-description":
        detail["posted"][0]["description"] = "x" * 513
    elif mutation == "short-full-window":
        for item in (
            detail["posted_receipt"], domain["expected"][0], domain["windows"][0],
        ):
            item["start"] = "2026-08-31"
    elif mutation == "excessive-pages":
        detail["posted_receipt"]["pages"] = 999
        domain["windows"][0]["pages"] = 999
    elif mutation == "negative-credit-limit":
        detail["detail"] = {
            "details": [{"key": "Credit Limit", "value": "-1 TWD"}],
        }
    elif mutation == "infinite-outstanding":
        payload["cards"][0]["outstandingBalance"] = "Infinity"
    elif mutation == "fractional-outstanding":
        payload["cards"][0]["outstandingBalance"] = "0.1"
    elif mutation == "malformed-outstanding":
        payload["cards"][0]["outstandingBalance"] = "1,2,3"
    elif mutation == "fractional-credit-limit":
        detail["detail"] = {
            "details": [{"key": "Credit Limit", "value": "0.1 TWD"}],
        }
    elif mutation == "invalid-detail-date":
        detail["detail"] = {
            "details": [{"key": "Last Statement Date", "value": "31 Feb 2026"}],
        }
    elif mutation == "invalid-card-date":
        payload["cards"][0]["paymentDueDate"] = "31-02-2026"
    elif mutation == "duplicate-detail-key":
        detail["detail"] = {
            "details": [
                {"key": "Credit Limit", "value": "100 TWD"},
                {"key": "Credit Limit", "value": "200 TWD"},
            ],
        }
    elif mutation == "posted-unposted-row":
        detail["unposted"] = [_posted_row(date(2026, 8, 30))]
    elif mutation == "garbage-unposted-placeholder":
        row = _posted_row(date(2026, 8, 30))
        row["postedDate"] = "0002-garbage"
        detail["unposted"] = [row]
    elif mutation == "alternate-unposted-placeholder":
        row = _posted_row(date(2026, 8, 30))
        row["postedDate"] = "0001-01-01T00:00"
        detail["unposted"] = [row]
    elif mutation == "missing-card-status":
        payload["cards"][0].pop("cardStatusDisplay")
    elif mutation == "future-coverage-end":
        for item in (
            detail["posted_receipt"],
            payload["history_coverage"]["domains"][0]["expected"][0],
            payload["history_coverage"]["domains"][0]["windows"][0],
        ):
            item["start"] = "9999-01-01"
            item["end"] = "9999-12-31"
    else:
        detail["posted"][0]["isPositive"] = "true"

    with pytest.raises(ValueError, match="HSBC history|history coverage|HSBC card detail"):
        persist_collected("hsbc", payload, store)

    assert store.conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM card_billed_txns").fetchone()[0] == 0
    assert store.latest_card_transaction_dates() == {}


def test_direct_hsbc_persistence_requires_coverage_even_without_rows(store) -> None:
    payload = {
        "cards": [{"id": "card-id", "maskedCardNumber": "4111111111111111"}],
        "card_detail": {},
    }

    from backend.core.persist.hsbc import persist_hsbc

    with pytest.raises(ValueError, match="HSBC history"):
        persist_hsbc(payload, store)

    assert store.conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0


def test_direct_hsbc_persistence_rejects_billed_rows_without_coverage(store) -> None:
    payload = _persist_payload()
    payload.pop("history_coverage")

    from backend.core.persist.hsbc import persist_hsbc

    with pytest.raises(ValueError, match="HSBC history"):
        persist_hsbc(payload, store)

    assert store.conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM card_billed_txns").fetchone()[0] == 0
