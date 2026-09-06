from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

import backend.banks.ctbc as ctbc_module
from backend.banks.ctbc import (
    CtbcCrawler,
    _CTBC_EBMW_PATH,
    _ctbc_month_windows,
    _validated_ctbc_detail,
)
from backend.core.base import (
    ApiHit,
    ResponseCollector,
    _OriginGuardProxy,
    validate_history_coverage,
)
from backend.core.persist import persist_collected
from backend.core.store import BankStore


def _inventory_hit(accounts: list[str]) -> ApiHit:
    frame = object()
    frame_url = "https://www.ctbcbank.com/twrbc/twrbc-deposit/qu001/010"
    url = f"https://www.ctbcbank.com{_CTBC_EBMW_PATH}"
    return ApiHit(
        url=url,
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
        content_type="application/json",
        raw_url=url + "?IIhfvu=synthetic-token",
        redirected=False,
        main_frame_request=True,
        request_frame_url=frame_url,
        request_frame=frame,
    )


def _native_envelope(body):
    # Field names/types only from the bounded native probe; values are synthetic.
    return {
        **dict.fromkeys(('deviceIxd', 'trackingIxd', 'txnIxd', 'model', 'platform',
                         'version', 'runtime', 'network', 'appVer', 'clientNo',
                         'token', 'locale', 'fromSys', 'seed', 'deviceToken'), 'synthetic'),
        'runtimeVer': 1, 'clientTime': 1, **body,
    }


def test_ctbc_inventory_accepts_exact_native_envelope():
    hit = _inventory_hit(['acct-a'])
    hit.req_body = _native_envelope(hit.req_body)
    collector = ResponseCollector()
    collector.hits.append(hit)
    assert CtbcCrawler._validated_twd_inventory(collector, _inventory_page(collector))[1] == {'acct-a'}


def test_ctbc_inventory_allows_native_home_to_deposit_spa_transition():
    hit = _inventory_hit(['acct-a'])
    page = SimpleNamespace(url=hit.request_frame_url, main_frame=hit.request_frame)
    hit.req_body = _native_envelope(hit.req_body)
    hit.request_frame_url = 'https://www.ctbcbank.com/twrbc/twrbc-home/qu000/010'
    hit.request_sequence = 2
    collector = ResponseCollector()
    collector.hits.append(hit)
    assert CtbcCrawler._validated_twd_inventory(collector, page)[1] == {'acct-a'}


def test_ctbc_inventory_rejects_duplicate_matching_responses():
    collector = ResponseCollector()
    hit = _inventory_hit(['acct-a'])
    collector.hits.extend([hit, hit])
    with pytest.raises(RuntimeError, match='ctbc-twd-history-inventory'):
        CtbcCrawler._validated_twd_inventory(collector, _inventory_page(collector))


def _inventory_page(collector: ResponseCollector):
    hit = next(
        (
            item for item in collector.hits
            if isinstance(item.req_body, dict)
            and item.req_body.get("resource") == "/twrbc-deposit/qu001/010"
        ),
        collector.hits[-1],
    )
    return SimpleNamespace(url=hit.request_frame_url, main_frame=hit.request_frame)


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


class _NativeField:
    def __init__(self, page, name: str) -> None:
        self.page = page
        self.name = name

    def count(self): return 1
    def nth(self, _index): return self
    def is_visible(self): return True
    def is_enabled(self): return True
    def click(self, **_kwargs): self.page.active_field = self.name
    def input_value(self): return self.page.values[self.name]


class _NativeQuery:
    def __init__(self, page) -> None: self.page = page
    def is_visible(self): return True
    def is_enabled(self): return True
    def evaluate(self, _script): return False
    def inner_text(self): return "搜尋"
    def get_attribute(self, _name): return None
    def click(self, **_kwargs): self.page.emit_query()


class _NativeCandidates:
    def __init__(self, items) -> None: self.items = items
    def count(self): return len(self.items)
    def nth(self, index): return self.items[index]


class _NativePanel(_NativeCandidates):
    def __init__(self, page) -> None:
        super().__init__([self])
        self.page = page

    def is_visible(self): return True
    def locator(self, _selector): return _NativeCandidates([_NativeQuery(self.page)])


class _NativeKeyboard:
    def __init__(self, page) -> None: self.page = page
    def press(self, key):
        assert key == "Backspace"
        self.page.values[self.page.active_field] = ""
    def type(self, value, delay):
        assert delay == 80
        self.page.values[self.page.active_field] = value


class _NativeHistoryPage:
    def __init__(self, collector: ResponseCollector, *, mutate_hit=None, duplicate=False) -> None:
        self.collector = collector
        self.mutate_hit = mutate_hit
        self.duplicate = duplicate
        self.url = "https://www.ctbcbank.com/twrbc/twrbc-deposit/qu002/010"
        self.values = {"startDt": "", "endDt": ""}
        self.active_field = ""
        self.main_frame = object()
        self.keyboard = _NativeKeyboard(self)
        self.query_count = 0

    def goto(self, *_args, **_kwargs): return None
    def wait_for_timeout(self, _milliseconds): return None

    def locator(self, selector):
        if selector == "input[formcontrolname='startDt']":
            return _NativeField(self, "startDt")
        if selector == "input[formcontrolname='endDt']":
            return _NativeField(self, "endDt")
        if selector == ".tab-pane:has(input[formcontrolname='startDt'])":
            return _NativePanel(self)
        raise AssertionError(selector)

    def emit_query(self):
        self.query_count += 1
        sequence = self.collector.request_sequence + 1
        self.collector._request_sequence = sequence
        url = f"https://www.ctbcbank.com{_CTBC_EBMW_PATH}"
        hit = ApiHit(
            url=url,
            method="POST",
            status=200,
            req_body={
                "resource": "/twrbc-deposit/qu002/011",
                "rqData": {
                    "accountId": "acct-a",
                    "type": "custom",
                    "startDate": self.values["startDt"].replace("/", ""),
                    "endDate": self.values["endDt"].replace("/", ""),
                },
            },
            resp_json={"code": "0000", "rsData": {
                "dataTime": "synthetic",
                "detailList": [],
                "dtSort": "synthetic",
                "nextKey": "",
            }},
            content_type="application/json",
            request_sequence=sequence,
            raw_url=url + "?IIhfvu=synthetic-token",
            request_frame_url=self.url,
            request_frame=self.main_frame,
            main_frame_request=True,
            redirected=False,
        )
        if self.mutate_hit:
            self.mutate_hit(hit)
        self.collector.hits.append(hit)
        if self.duplicate:
            self.collector.hits.append(ApiHit(**hit.__dict__))


@pytest.mark.parametrize('path', ['seed', 'native', 'month'])
def test_ctbc_history_callers_accept_native_envelope(path):
    collector = ResponseCollector()
    if path == 'native':
        page = _NativeHistoryPage(collector, mutate_hit=lambda h: setattr(h, 'req_body', _native_envelope(h.req_body)))
        assert CtbcCrawler._fetch_native_history_window(page, collector, 'acct-a', date(2026, 8, 1), date(2026, 8, 30)) == []
    else:
        hit = _template()
        hit.req_body = _native_envelope(hit.req_body)
        collector.hits.append(hit)
        if path == 'seed':
            assert CtbcCrawler._latest_qu002_011_hit(collector) is hit
        else:
            assert CtbcCrawler._fetch_qu002_011(_FetchPage(empty=True), hit.url, hit.req_body, 'acct-a', 'm0', 'Bearer synthetic') == []


@pytest.mark.parametrize('mutation', [
    lambda b: b.update(extra='unexpected'), lambda b: b.pop('deviceIxd'),
    lambda b: b.update(runtimeVer=True), lambda b: b.update(clientTime='1'),
    lambda b: b.update(token={}), lambda b: b.update(token='x' * 16_384),
    lambda b: b.update(rqData={'unexpected':True}),
])
def test_ctbc_native_inventory_envelope_remains_strict(mutation):
    hit = _inventory_hit(['acct-a'])
    hit.req_body = _native_envelope(hit.req_body)
    mutation(hit.req_body)
    collector = ResponseCollector()
    collector.hits.append(hit)
    with pytest.raises(RuntimeError, match='ctbc-twd-history-inventory'):
        CtbcCrawler._validated_twd_inventory(collector, _inventory_page(collector))


def test_ctbc_inventory_requires_fresh_request_sequence():
    hit = _inventory_hit(['acct-a'])
    hit.request_sequence = 1
    collector = ResponseCollector()
    collector.hits.append(hit)
    with pytest.raises(RuntimeError, match='ctbc-twd-history-inventory'):
        CtbcCrawler._validated_twd_inventory(collector, _inventory_page(collector), after_sequence=1)
    fresh = _inventory_hit(['acct-b'])
    fresh.request_frame = hit.request_frame
    fresh.request_sequence = 2
    collector.hits.append(fresh)
    assert CtbcCrawler._validated_twd_inventory(collector, _inventory_page(collector), after_sequence=1)[1] == {'acct-b'}


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


@pytest.mark.parametrize('replacement', [False, True])
def test_ctbc_deposit_navigation_waits_for_native_hydration_and_fresh_inventory(replacement):
    from tests.test_ctbc_login_checkpoints import _launch_browser

    manager, browser = _launch_browser()
    try:
        page = browser.new_page()
        home = "https://www.ctbcbank.com/twrbc/twrbc-home/qu000/010"
        deposit = "https://www.ctbcbank.com/twrbc/twrbc-deposit/qu001/010"
        page.route("**/*", lambda route: route.fulfill(body="<body></body>", content_type="text/html"))
        page.goto(home)
        page.evaluate("""replacement => {
          window.clicks = 0;
          setTimeout(() => {
            const a = document.createElement('a');
            a.className = 'link'; a.textContent = '臺幣存款';
            a.onclick = () => { window.clicks++;
              if (replacement) location.href = '/twrbc/twrbc-deposit/qu001/010';
              else setTimeout(() => history.pushState({}, '', '/twrbc/twrbc-deposit/qu001/010'), 200);
            };
            document.body.append(a);
          }, 200);
        }""", replacement)
        collector = ResponseCollector()
        stale = _inventory_hit(["acct-a"])
        collector.hits.append(stale)
        original_wait = page.wait_for_timeout
        def wait(milliseconds):
            original_wait(milliseconds)
            if page.url == deposit and len(collector.hits) == 1:
                fresh = _inventory_hit(["acct-a"])
                fresh.request_sequence = 1
                collector.hits.append(fresh)
        page.wait_for_timeout = wait
        if replacement:
            with pytest.raises(RuntimeError, match='ctbc-twd-history-inventory'):
                CtbcCrawler.__new__(CtbcCrawler)._goto_twd_deposit(page, collector)
            return
        CtbcCrawler.__new__(CtbcCrawler)._goto_twd_deposit(page, collector)
        assert page.url == deposit
        assert page.evaluate("window.clicks") == 1
        assert len(collector.hits) == 2
    finally:
        browser.close()
        manager.__exit__(None, None, None)


def test_ctbc_inventory_requires_exact_owned_success_response():
    valid = _inventory_hit(["acct-a"])
    collector = ResponseCollector()
    collector.hits.append(valid)
    payload, identities = CtbcCrawler._validated_twd_inventory(collector, _inventory_page(collector))
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
            CtbcCrawler._validated_twd_inventory(bad, _inventory_page(bad))


def test_ctbc_inventory_rejects_untrusted_transport():
    mutations = (
        lambda hit: setattr(hit, "redirected", True),
        lambda hit: setattr(hit, "main_frame_request", False),
        lambda hit: setattr(hit, "request_frame", object()),
        lambda hit: setattr(hit, "request_frame_url", "https://www.ctbcbank.com/sibling"),
        lambda hit: setattr(hit, "url", hit.url + ";unexpected"),
        lambda hit: setattr(hit, "raw_url", hit.raw_url + "&unexpected=1"),
        lambda hit: setattr(hit, "content_type", "text/html"),
        lambda hit: hit.req_body.update(extra="unexpected"),
        lambda hit: hit.req_body.update(rqData={"unexpected": True}),
    )
    for mutate in mutations:
        hit = _inventory_hit(["acct-a"])
        page = SimpleNamespace(url=hit.request_frame_url, main_frame=hit.request_frame)
        mutate(hit)
        collector = ResponseCollector()
        collector.hits.append(hit)
        with pytest.raises(RuntimeError, match="ctbc-twd-history-inventory"):
            CtbcCrawler._validated_twd_inventory(collector, page=page)

    for url in (
        "https://attacker.example/twrbc/twrbc-deposit/qu001/010",
        "https://www.ctbcbank.com/twrbc/twrbc-deposit/sibling#unexpected",
    ):
        hit = _inventory_hit(["acct-a"])
        hit.request_frame_url = url
        page = SimpleNamespace(url=url, main_frame=hit.request_frame)
        collector = ResponseCollector()
        collector.hits.append(hit)
        with pytest.raises(RuntimeError, match="ctbc-twd-history-inventory"):
            CtbcCrawler._validated_twd_inventory(collector, page=page)


@pytest.mark.parametrize("account", ["", " acct-a", "acct-a "])
def test_ctbc_history_seed_rejects_noncanonical_account(account):
    collector = ResponseCollector()
    collector.hits.append(_template(account))
    assert CtbcCrawler._latest_qu002_011_hit(collector) is None


def test_ctbc_history_seed_rejects_untrusted_template():
    mutations = (
        lambda hit: setattr(hit, "redirected", True),
        lambda hit: setattr(hit, "url", hit.url + ";unexpected"),
        lambda hit: setattr(hit, "url", hit.url + "?unexpected=1"),
        lambda hit: hit.req_body.update(unexpected="PRIVATE"),
    )
    for mutate in mutations:
        hit = _template()
        mutate(hit)
        collector = ResponseCollector()
        collector.hits.append(hit)
        assert CtbcCrawler._latest_qu002_011_hit(collector) is None


def test_ctbc_full_history_queries_all_accounts_and_six_months(monkeypatch):
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    crawler = object.__new__(CtbcCrawler)
    crawler.transaction_cursors = {}
    page = _FetchPage()
    collector = _collector(["acct-a", "acct-b"])
    deposit, identities = crawler._validated_twd_inventory(collector, _inventory_page(collector))

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


def test_ctbc_cookie_session_uses_native_search_form(monkeypatch):
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    crawler = object.__new__(CtbcCrawler)
    crawler.transaction_cursors = {}
    collector = ResponseCollector()
    collector.hits.append(_inventory_hit(["acct-a"]))
    page = _NativeHistoryPage(collector)
    deposit, identities = crawler._validated_twd_inventory(collector, _inventory_page(collector))

    result = crawler._collect_twd_deposit_history(
        page, collector, deposit,
        expected_identities=identities, as_of=date(2026, 8, 30),
    )

    assert page.query_count == 6
    assert len(result["coverage"]["domains"][0]["windows"]) == 6
    assert all(
        window["status"] == "explicit_empty"
        for window in result["coverage"]["domains"][0]["windows"]
    )


@pytest.mark.parametrize(
    "mutate_hit",
    [
        lambda hit: setattr(hit, "request_sequence", 0),
        lambda hit: setattr(hit, "status", 204),
        lambda hit: setattr(hit, "redirected", True),
        lambda hit: setattr(hit, "url", hit.url + "?unexpected=1"),
        lambda hit: setattr(hit, "url", hit.url + ";unexpected"),
        lambda hit: setattr(hit, "raw_url", hit.raw_url + "&unexpected=1"),
        lambda hit: setattr(
            hit,
            "raw_url",
            hit.url + ";unexpected?IIhfvu=synthetic-token",
        ),
        lambda hit: setattr(hit, "request_frame", object()),
        lambda hit: setattr(hit, "request_frame_url", "https://www.ctbcbank.com/sibling"),
        lambda hit: setattr(hit, "main_frame_request", False),
        lambda hit: hit.req_body["rqData"].update({"extra": "unexpected"}),
        lambda hit: hit.req_body["rqData"].update(type="unexpected"),
        lambda hit: hit.req_body["rqData"].update(type="search"),
        lambda hit: hit.req_body.update(unexpected="PRIVATE"),
    ],
)
def test_ctbc_native_history_rejects_unbound_response(mutate_hit):
    collector = ResponseCollector()
    page = _NativeHistoryPage(collector, mutate_hit=mutate_hit)
    with pytest.raises(RuntimeError, match="ctbc-twd-history-fetch"):
        CtbcCrawler._fetch_native_history_window(
            page, collector, "acct-a", date(2026, 8, 1), date(2026, 8, 31),
        )


def test_ctbc_native_history_rejects_unbound_response_payload():
    mutations = (
        lambda data: data.update(accountId="other-account"),
        lambda data: data.update(startDate="20260731"),
        lambda data: data.update(endDate="20260901"),
        lambda data: data.update(count=99),
        lambda data: data.update(totalCount=99),
        lambda data: data.update(totalPages=2),
        lambda data: data.update(hasMore=True),
        lambda data: data.update(hasNext=True),
        lambda data: data.update(nextPage=2),
        lambda data: data.pop("dataTime"),
        lambda data: data.pop("dtSort"),
        lambda data: data.update(hasNextPage=True),
    )
    for mutate in mutations:
        collector = ResponseCollector()

        def mutate_hit(hit):
            mutate(hit.resp_json["rsData"])

        page = _NativeHistoryPage(collector, mutate_hit=mutate_hit)
        with pytest.raises(RuntimeError, match="ctbc-twd-history-fetch"):
            CtbcCrawler._fetch_native_history_window(
                page, collector, "acct-a", date(2026, 8, 1), date(2026, 8, 31),
            )


def test_ctbc_native_history_accepts_only_live_encrypted_raw_query():
    collector = ResponseCollector()
    page = _NativeHistoryPage(collector)
    assert CtbcCrawler._fetch_native_history_window(
        page, collector, "acct-a", date(2026, 8, 1), date(2026, 8, 31),
    ) == []

    for raw_query in (
        "unexpected=synthetic-token",
        "IIhfvu=",
        "IIhfvu=one&IIhfvu=two",
        "IIhfvu=synthetic-token&unexpected=1",
    ):
        collector = ResponseCollector()

        def invalid_query(hit, query=raw_query):
            hit.raw_url = hit.url + "?" + query

        page = _NativeHistoryPage(collector, mutate_hit=invalid_query)
        with pytest.raises(RuntimeError, match="ctbc-twd-history-fetch"):
            CtbcCrawler._fetch_native_history_window(
                page, collector, "acct-a", date(2026, 8, 1), date(2026, 8, 31),
            )


def test_ctbc_native_history_rejects_missing_encrypted_raw_query():
    collector = ResponseCollector()

    def remove_query(hit):
        hit.raw_url = hit.url

    page = _NativeHistoryPage(collector, mutate_hit=remove_query)
    with pytest.raises(RuntimeError, match="ctbc-twd-history-fetch"):
        CtbcCrawler._fetch_native_history_window(
            page, collector, "acct-a", date(2026, 8, 1), date(2026, 8, 31),
        )


def test_ctbc_native_history_unwraps_origin_guarded_main_frame():
    collector = ResponseCollector()
    page = _NativeHistoryPage(collector)
    guarded = _OriginGuardProxy(page, lambda: None)
    assert CtbcCrawler._fetch_native_history_window(
        guarded, collector, "acct-a", date(2026, 8, 1), date(2026, 8, 31),
    ) == []


def test_ctbc_native_history_rejects_duplicate_response_after_quiescence():
    collector = ResponseCollector()
    page = _NativeHistoryPage(collector, duplicate=True)
    with pytest.raises(RuntimeError, match="ctbc-twd-history-fetch"):
        CtbcCrawler._fetch_native_history_window(
            page, collector, "acct-a", date(2026, 8, 1), date(2026, 8, 31),
        )


def test_ctbc_native_history_rejects_query_or_params_on_page_url():
    for suffix in ("?unexpected=1", ";unexpected"):
        collector = ResponseCollector()
        page = _NativeHistoryPage(collector)
        page.url += suffix
        with pytest.raises(RuntimeError, match="ctbc-twd-history-form"):
            CtbcCrawler._fetch_native_history_window(
                page, collector, "acct-a", date(2026, 8, 1), date(2026, 8, 31),
            )


@pytest.mark.parametrize("raw_datetime", ["2026-08-20 10:00:00.00000"])
def test_ctbc_detail_accepts_strict_iso_datetime(raw_datetime):
    row = _validated_ctbc_detail(
        {
            "actDtTm": raw_datetime,
            "trnDtRaw": "20260820",
            "dbAmt": "1",
            "crAmt": "0",
            "balanceAmt": "99",
        },
        start=date(2026, 8, 1), end=date(2026, 8, 31),
    )

    assert row["dbAmt"] == 1
    assert row["actDtTm"] == "2026-08-20-10.00.00"


@pytest.mark.parametrize("raw_datetime", [
    "2026-08-20T10:00:00.000+08:00",
    "2026-08-20 10:00:00",
])
def test_ctbc_detail_rejects_unobserved_datetime_shapes(raw_datetime):
    with pytest.raises(ValueError, match="invalid row"):
        _validated_ctbc_detail(
            {
                "actDtTm": raw_datetime,
                "trnDtRaw": "20260820",
                "dbAmt": "1",
                "crAmt": "0",
                "balanceAmt": "99",
            },
            start=date(2026, 8, 1), end=date(2026, 8, 31),
        )


@pytest.mark.parametrize("page_url", [
    "https://attacker.example/twrbc/twrbc-deposit/qu002/010",
    "https://www.ctbcbank.com/twrbc/twrbc-deposit/sibling",
    "https://www.ctbcbank.com/twrbc/twrbc-deposit/qu002/010?unexpected=1",
])
def test_ctbc_checks_final_page_origin_before_forwarding_bearer(monkeypatch, page_url):
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    crawler = object.__new__(CtbcCrawler)
    crawler.transaction_cursors = {}
    collector = _collector(["acct-a"])
    deposit, identities = crawler._validated_twd_inventory(collector, _inventory_page(collector))
    page = _FetchPage()
    page.url = page_url
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
    deposit, identities = crawler._validated_twd_inventory(collector, _inventory_page(collector))
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
    deposit, identities = crawler._validated_twd_inventory(collector, _inventory_page(collector))

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
    deposit, identities = crawler._validated_twd_inventory(collector, _inventory_page(collector))

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
    deposit, identities = crawler._validated_twd_inventory(collector, _inventory_page(collector))

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
    deposit, identities = crawler._validated_twd_inventory(collector, _inventory_page(collector))

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


def test_ctbc_replay_rejects_untrusted_template_shape():
    body = _template().req_body
    body["unexpected"] = "PRIVATE"
    with pytest.raises(RuntimeError, match="invalid-request"):
        CtbcCrawler._fetch_qu002_011(
            _FetchPage(),
            f"https://www.ctbcbank.com{_CTBC_EBMW_PATH}",
            body,
            "acct-a", "m0", "Bearer synthetic-token",
        )
    for suffix in (";unexpected", "?unexpected=1"):
        with pytest.raises(RuntimeError, match="invalid-request"):
            CtbcCrawler._fetch_qu002_011(
                _FetchPage(),
                f"https://www.ctbcbank.com{_CTBC_EBMW_PATH}{suffix}",
                _template().req_body,
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
    deposit, identities = crawler._validated_twd_inventory(collector, _inventory_page(collector))
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
