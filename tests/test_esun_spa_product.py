"""Real run/shared-login/collect on loopback; synthetic env credentials only."""

import inspect
import json
import textwrap
import threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import MethodType
from unittest.mock import Mock

import pytest
from patchright.sync_api import sync_playwright

from backend.banks.esun import EsunCrawler
from backend.core.base import BankCollectResult
from backend.core.creds import EsunCreds

PATHS = (
    "/esb/mib-auth-portal/cpo08/cpo08003/home/init",
    "/esb/mib-ctw-portal/ctw01/ctw01002/home/preQueryTWTransactionDetail",
    "/esb/mib-ctw-portal/ctw01/ctw01002/search/queryTWTransactionDetail",
    "/esb/mib-ctw-portal/ctw01/ctw01002/home/continueQueryTWTransactionDetail",
)
FIXTURES = Path(__file__).with_name("fixtures")


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as engine:
        browser = engine.chromium.launch(
            headless=True,
            args=[
                "--disable-background-networking",
                "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
            ],
        )
        yield browser
        browser.close()


@pytest.fixture
def product(browser, monkeypatch, tmp_path):
    # Use inherited BankCreds.from_env via the real local constructor, not cloud/DB.
    for name in ("BANK_CRAWLER_ACCOUNT_ID", "BANK_CRAWLER_USER_ID", "PYTHON_DOTENV_DISABLED"):
        monkeypatch.delenv(name, raising=False)
    for name, value in dict(NATIONAL_ID="TEST-ID", USER_CODE="TEST-USER", PASSWORD="TEST-PASS").items():
        monkeypatch.setenv("ESUN_" + name, value)
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "incremental")
    env_loader = EsunCreds.from_env
    loaded = []

    def from_env(cls):
        loaded.append(True)
        return env_loader()

    monkeypatch.setattr(EsunCreds, "from_env", classmethod(from_env))
    monkeypatch.setattr("backend.core.base.DATA_ROOT", tmp_path)
    crawler = EsunCrawler()
    assert loaded == [True]
    crawler.transaction_cursors = {"twd_transactions": {"0000000000001": date(2026, 9, 20)}}
    row = dict(
        debitCredit="CR", detailTitle="SYNTHETIC-ROW", txDate="2026/09/20", txTime="12:00:00", amount=1, balance=1
    )
    group = dict(year="2026", month="9", detailInfo=[row, dict(row)])
    fixture = json.loads((FIXTURES / "esun_spa_native.json").read_text())
    bodies = {
        PATHS[0]: {"menuList": fixture["menu"]},
        PATHS[1]: fixture["prequery"],
        PATHS[2]: dict(
            queryDeptTxDtlResult=dict(
                startIndex=7, count=100, displayErrorCode=None, displayErrorMsg=None,
                detailListData=[dict(group, detailInfo=[dict(row) for _ in range(100)])]
            ),
            recentHashtag=[],
        ),
        PATHS[3]: dict(startIndex=107, count=2, detailListData=[group]),
    }
    html = (FIXTURES / "esun_spa_native.html").read_text()
    hits, external, captured = [], [], []
    state = {"mode": "ok"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            raw = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            hits.append((self.path, request))
            body = {"resultCode": "0000", "resultBody": bodies[self.path]}
            if state["mode"] == "short_page" and self.path == PATHS[2]:
                body["resultBody"] = dict(body["resultBody"])
                detail = dict(body["resultBody"]["queryDeptTxDtlResult"], detailListData=[group])
                body["resultBody"]["queryDeptTxDtlResult"] = detail
            if state["mode"] == "non_success" and self.path == PATHS[3]:
                body = {"resultCode": "UNKNOWN", "resultDescription": "SYNTHETIC-PRIVATE"}
            if state["mode"] == "empty_success" and self.path == PATHS[3]:
                body["resultBody"] = dict(bodies[self.path], detailListData=[])
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        from backend.banks.esun_spa import capture
    except ImportError:
        pass  # Product RED must fail on missing collection, not missing import.
    else:
        monkeypatch.setattr(capture, "ORIGIN", origin)
        monkeypatch.setattr(capture, "_origin", lambda url: url.startswith(origin + "/"))
    context = browser.new_context(viewport={"width": 1200, "height": 900}, service_workers="block")

    def route(r):
        if r.request.url.startswith(origin + "/"):
            r.continue_()
        else:
            external.append(r.request.url)
            r.abort()

    context.route("**/*", route)
    page = context.new_page()
    page.goto(origin + "/synthetic")
    page.clock.set_fixed_time(datetime(2026, 9, 24, 12, tzinfo=timezone.utc))
    # Patchright's page.clock affects the main world, not evaluate's isolated
    # world. Freeze only the read-only clock query too; no DOM/response mocks.
    evaluate = page.evaluate

    def fixed_clock(expression, arg=None):
        if expression == "() => {const d=new Date();return [d.getFullYear(),d.getMonth()+1,d.getDate()]}":
            return [2026, 9, 24]
        return evaluate(expression, arg)

    monkeypatch.setattr(page, "evaluate", fixed_clock)
    # Only origin constants adapted; preserve actual positive DOM authentication.
    source = (
        textwrap.dedent(inspect.getsource(EsunCrawler._logged_in))
        .replace('"https"', '"http"')
        .replace('"ebank.esunbank.com.tw"', '"127.0.0.1"')
        .replace("(None, 443)", f"(None, {server.server_port})")
    )
    ns = dict(EsunCrawler._logged_in.__globals__)
    exec(source, ns)
    monkeypatch.setattr(crawler, "_logged_in", MethodType(ns["_logged_in"], crawler))
    monkeypatch.setattr(crawler, "_credential_origin_allowed", lambda p: p.url.startswith(origin + "/"))
    monkeypatch.setattr(
        crawler, "_frame_origin_allowed",
        lambda p, f: getattr(f, "_target", f) is page.main_frame and p.url.startswith(origin + "/"),
    )
    monkeypatch.setattr(crawler, "_enforce_session_freshness", lambda: None)
    monkeypatch.setattr(crawler, "_build_fetch_kwargs", lambda: {"__cleanups__": []})
    monkeypatch.setattr(crawler, "_execute_browser_flow", lambda _url, **kw: kw["page_action"](page))
    monkeypatch.setattr(crawler, "prepare_login_page", lambda p: None)
    wait = page.wait_for_timeout
    monkeypatch.setattr(page, "wait_for_timeout", lambda ms: wait(min(ms, 20)))
    logout = Mock(return_value=True)
    monkeypatch.setattr(crawler, "logout", logout)
    original = crawler.collect

    def collect(*args):
        result = original(*args)
        captured.append(result)
        return result

    monkeypatch.setattr(crawler, "collect", collect)
    submit = Mock(wraps=crawler.submit_credentials_once)
    monkeypatch.setattr(crawler, "submit_credentials_once", submit)
    try:
        yield crawler, page, origin, hits, external, captured, state, logout, submit
    finally:
        context.close()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('height, expected, production_viewport', [(1000, 1, False), (2000, 0, False), (1000, 0, True)])
def test_native_window_scroll_can_continue_before_query(browser, height, expected, production_viewport):
    # Reuse the frozen public handler, mounted with prequery data as in the bank.
    # Synthetic geometry proves the mechanism, not the live failure's cause.
    html = (FIXTURES / 'esun_spa_native.html').read_text()
    handler = html.split('<script>window.installContinuation=', 1)[1].split('</script>', 1)[0]
    content = (
        '<style>.timeline-query-continer{height:400px;overflow:auto}'
        f'.row{{height:{height}px}}</style><div style="height:1500px"></div>'
        '<button id="calendar">Calendar</button>'
        '<div class="timeline-query-continer"><div class="row"></div></div>'
        '<script>window.installContinuation=' + handler + '''
          window.installContinuation({timeLineRq:{startIndex:1,count:100},
            timeLineQueryData:{initData:[]}});
          document.body.dataset.wheels='0';
          window.addEventListener('wheel',()=>document.body.dataset.wheels=
            String(Number(document.body.dataset.wheels)+1));
        </script>'''
    )
    hits = []
    viewport = (object.__new__(EsunCrawler)._build_fetch_kwargs()['additional_args']['viewport']
                if production_viewport else {'width': 1200, 'height': 900})
    context = browser.new_context(viewport=viewport)
    try:
        def route(r):
            if r.request.url == 'http://127.0.0.1/native-scroll':
                r.fulfill(content_type='text/html', body=content)
            elif r.request.url == 'http://127.0.0.1' + PATHS[3]:
                hits.append(r.request.method)
                r.fulfill(json={'resultCode': 'SYNTHETIC_STOP', 'resultDescription': 'Synthetic stop'})
            else:
                r.abort()

        context.route('**/*', route)
        page = context.new_page()
        page.goto('http://127.0.0.1/native-scroll')
        page.locator('#calendar').click()
        page.wait_for_timeout(500)
        assert len(hits) == expected
        assert page.locator('body').get_attribute('data-wheels') == '0'
        assert (page.evaluate('window.scrollY') == 0) if production_viewport else (page.evaluate('window.scrollY') > 0)
        assert all(method == 'POST' for method in hits)
    finally:
        context.close()


def test_spa_failure_reports_navigation_phase(product, monkeypatch):
    from backend.banks.esun_spa import collection
    crawler, page, origin, _, _, captured, _, _, _ = product

    def fail_plan(*_args):
        raise ValueError('synthetic navigation failure')

    monkeypatch.setattr(collection, 'build_menu_plan', fail_plan)
    result = crawler.run(origin + '/synthetic', headless=True)
    assert not captured
    assert 'data' not in result and result['error'].startswith('collect_failed: ValueError: code=')
    assert crawler._diagnostic_stage == 'collect_navigation'
    assert 'synthetic navigation failure' not in repr(result)


def test_spa_transaction_form_failure_keeps_closed_phase(product, monkeypatch):
    from backend.banks.esun_spa import query
    crawler, page, origin, _, _, captured, _, _, _ = product

    def fail_form(*_args, **_kwargs):
        raise ValueError('private page text')

    monkeypatch.setattr(query, 'query_twd', fail_form)
    result = crawler.run(origin + '/synthetic', headless=True)
    assert not captured and 'data' not in result
    assert result['error'].startswith('collect_failed: ValueError: code=')
    assert crawler._diagnostic_stage == 'collect_transactions'
    assert crawler._esun_spa_phase == 'transaction_form'
    assert 'private page text' not in repr(result)


def test_spa_continuation_action_failure_reports_subphase(product, monkeypatch):
    crawler, page, origin, _, _, captured, _, _, _ = product

    def fail_wheel(*_args, **_kwargs):
        raise ValueError('private action detail')

    monkeypatch.setattr(page.mouse, 'wheel', fail_wheel)
    result = crawler.run(origin + '/synthetic', headless=True)
    assert not captured and 'data' not in result
    assert result['error'].startswith('collect_failed: ValueError: code=')
    assert crawler._diagnostic_stage == 'collect_transactions'
    assert crawler._esun_spa_phase == 'continuation_action'
    assert 'private action detail' not in repr(result)


def test_spa_collection_updates_diagnostic_stage_per_owned_phase(product, monkeypatch):
    from backend.banks.esun_spa import collection, query
    crawler, page, origin, _, _, captured, _, _, _ = product
    account_stages, transaction_stages, account_phases, transaction_phases = [], [], [], []
    original_require = collection.require_current_success
    original_query = query.query_twd

    def observe_response(collector, baseline, path):
        if path == collection.PATHS[1]:
            account_stages.append(crawler._diagnostic_stage)
            account_phases.append(crawler._esun_spa_phase)
        return original_require(collector, baseline, path)

    def observe_query(*args, **kwargs):
        transaction_stages.append(crawler._diagnostic_stage)
        transaction_phases.append(crawler._esun_spa_phase)
        return original_query(*args, **kwargs)

    monkeypatch.setattr(collection, 'require_current_success', observe_response)
    monkeypatch.setattr(query, 'query_twd', observe_query)
    result = crawler.run(origin + '/synthetic', headless=True)
    assert account_stages and account_stages[0] == 'collect_accounts'
    assert set(account_stages) == {'collect_accounts', 'collect_transactions', 'collect_validation'}
    assert transaction_stages == ['collect_transactions']
    assert transaction_phases == ['transaction_form']
    assert account_phases[0] == 'prequery_response'
    assert 'account_validation' in account_phases
    assert len(captured) == 1 and captured[0].error == 'spa_collection_incomplete'
    assert 'data' not in result and result['error'] == 'collect_failed: ValueError: code=collect_contract'
    assert crawler._diagnostic_stage == 'collect_validation'
    assert crawler._esun_spa_phase == 'incomplete_result'


@pytest.mark.parametrize('when', ['before_form', 'before_submit'])
def test_unexpected_continuation_stops_before_query_submission(product, monkeypatch, when):
    from backend.banks.esun_spa import query

    crawler, page, origin, hits, external, captured, _, _, _ = product
    original = query.query_twd
    injected = []
    wheel = Mock(wraps=page.mouse.wheel)
    monkeypatch.setattr(page.mouse, 'wheel', wheel)

    def inject():
        # Actual loopback request, independent of the explicit-query renderer.
        page.evaluate('''path => fetch(path, {method:'POST',
            headers:{'content-type':'application/json'},
            body:JSON.stringify({requestBody:{startIndex:101,count:100}})
        }).then(r=>r.json())''', PATHS[3])
        assert crawler.collector.issued_count(PATHS[3]) == 1
        injected.append(True)

    def query_with_arrival(*args, **kwargs):
        if when == 'before_form':
            inject()
        else:
            admit = kwargs['before_submit']

            def late_arrival():
                inject()
                admit()

            kwargs['before_submit'] = late_arrival
        return original(*args, **kwargs)

    monkeypatch.setattr(query, 'query_twd', query_with_arrival)
    result = crawler.run(origin + '/synthetic', headless=True)
    assert injected and not captured and not external and 'data' not in result
    assert [path for path, _ in hits] == [PATHS[0], PATHS[1], PATHS[3]]
    if when == 'before_form':
        assert not page.locator('input[value="customized"]').is_checked()
    wheel.assert_not_called()


def test_calendar_admission_runs_after_proxy_guard():
    import ast
    from typing import Any
    from backend.banks.esun_spa.query import query_twd
    from backend.core.base import _OriginGuardProxy

    source = ast.parse(inspect.getsource(query_twd))
    function = source.body[0]
    assert isinstance(function, ast.FunctionDef)
    click = next(n for n in function.body if isinstance(n, ast.FunctionDef) and n.name == 'click')
    issued, actions = [], []
    stop = ValueError('synthetic issuance drift')

    def guard():
        issued.append(True)  # Model request arrival during the proxy's dispatch guard.

    def admission():
        if issued:
            raise stop

    native = Mock()
    scope: dict[str, Any] = dict(checkpoint=lambda: None, owner=lambda: None, one=lambda x: x,
                 reserve_action=lambda: actions.append(True), before_action=admission,
                 _OriginGuardProxy=_OriginGuardProxy, TIMEOUT=1)
    exec(compile(ast.Module(body=[click], type_ignores=[]), '<real-query-click>', 'exec'), scope)
    with pytest.raises(ValueError) as error:
        scope['click'](_OriginGuardProxy(native, guard))
    assert error.value is stop and issued and not actions
    native.click.assert_not_called()


def test_continuation_waits_for_loading_overlay_to_clear(product, monkeypatch):
    from backend.banks.esun_spa import collection
    crawler, page, origin, hits, _, captured, _, _, _ = product
    original = collection.continue_twd_once

    def with_loading(*args, **kwargs):
        # A real temporary hit-test obstruction, not a mocked geometry result.
        page.evaluate("""() => {
            const layer=document.createElement('div');
            layer.id='synthetic-loading';
            layer.style='position:fixed;inset:0;z-index:99999';
            document.body.append(layer);
            setTimeout(()=>layer.remove(),1000);
        }""")
        return original(*args, **kwargs)

    monkeypatch.setattr(collection, 'continue_twd_once', with_loading)
    crawler.run(origin + '/synthetic', headless=True)
    assert len(captured) == 1 and len(captured[0].twd_txns) == 102
    assert [p for p, _ in hits] == list(PATHS)


@pytest.mark.parametrize(
    'mode,cursor,expected',
    [
        ('full', date(2020, 1, 1), date(2023, 9, 24)),
        ('incremental', date(2020, 1, 1), date(2023, 9, 24)),
        ('incremental', date(2026, 9, 20), date(2026, 9, 13)),
        ('incremental', None, date(2023, 9, 24)),
    ],
)
def test_spa_search_window_respects_history_mode(product, monkeypatch, mode, cursor, expected):
    from backend.banks.esun_spa import query

    crawler, _, origin, hits, _, captured, _, _, _ = product
    monkeypatch.setenv('BANK_CRAWLER_HISTORY_MODE', mode)
    crawler.transaction_cursors = {'twd_transactions': {'0000000000001': cursor} if cursor else {}}
    selected = []

    def stop_after_window(_page, start, end, *_args, **_kwargs):
        selected.append((start, end))
        raise ValueError('synthetic stop before query')

    monkeypatch.setattr(query, 'query_twd', stop_after_window)
    result = crawler.run(origin + '/synthetic', headless=True)
    assert selected and selected[0][0] == expected
    assert selected[0][1] == date(2026, 9, 24) if expected.year == 2026 else selected[0][1] > expected
    assert not captured and [p for p, _ in hits] == list(PATHS[:2])
    assert 'data' not in result


def test_short_initial_page_does_not_issue_a_100_row_continuation(product):
    crawler, page, origin, hits, external, captured, state, _, submit = product
    state["mode"] = "short_page"
    result = crawler.run(origin + "/synthetic", headless=True)
    assert len(captured) == 1
    data = captured[0]
    assert len(data.twd_txns) == 2
    assert data.error == "spa_collection_incomplete"
    assert data.history_coverage is None and data.card_bill_facts_ok is False
    assert [path for path, _ in hits] == list(PATHS[:3])
    assert "data" not in result and not external
    submit.assert_called_once()


def test_run_collects_native_continuation_but_keeps_error_barrier(product):
    crawler, page, origin, hits, external, captured, _, logout, submit = product
    result = crawler.run(origin + "/synthetic", headless=True)
    assert len(captured) == 1
    data = captured[0]
    assert type(data) is BankCollectResult
    assert len(data.twd_txns) == 102  # Duplicate occurrences in both batches survive.
    assert all(row == data.twd_txns[0] for row in data.twd_txns)
    assert data.error == "spa_collection_incomplete"
    assert data.history_coverage is None and data.card_bill_facts_ok is False
    assert [path for path, _ in hits] == list(PATHS)
    assert hits[-1][1]["requestBody"]["startIndex"] == 107
    assert hits[-1][1]["requestBody"]["count"] == 100
    assert crawler._spa_login_baseline["counts"][PATHS[0]] == 0
    assert "data" not in result and result["error"] == "collect_failed: ValueError: code=collect_contract"
    assert crawler._diagnostic_stage == "collect_validation"
    assert not external
    assert not any(value in json.dumps(result) for value in ("TEST-", "SYNTHETIC-", "0000000000001"))
    submit.assert_called_once()
    logout.assert_called_once()
    assert page.evaluate("JSON.parse(document.body.dataset.counts)") == dict(login=1, header=1, leaf=1)


@pytest.mark.parametrize("mode,expected", [("non_success", 100), ("no_scroll", 100), ("empty_success", 100)])
def test_partial_outcomes_never_claim_coverage(product, mode, expected):
    crawler, page, origin, hits, external, captured, state, _, submit = product
    state["mode"] = mode
    if mode == "no_scroll":
        page.add_style_tag(content=".timeline-query-continer{height:auto!important;overflow:visible!important}")
    if mode == "empty_success":
        pass  # Server supplies empty success; source-owned extra issuance must reject.
    result = crawler.run(origin + "/synthetic", headless=True)
    assert "data" not in result and not external
    submit.assert_called_once()
    if captured:
        data = captured[0]
        assert data.error == "spa_collection_incomplete"
        assert data.history_coverage is None and data.card_bill_facts_ok is False
        assert len(data.twd_txns) == expected
    else:
        # The legacy synthetic page lacks the official short-list window handler;
        # attempting that supported path must time out, never imply completion.
        assert mode in {"empty_success", "no_scroll"} and "error" in result


@pytest.mark.parametrize(
    "fault",
    [
        "home_failed",
        "prequery_failed",
        "query_failed",
        "continuation_failed",
        "loading_failed",
        "row_drift",
        "document_drift",
        "unknown",
        "otp",
    ],
)
def test_product_rejects_late_owner_response_render_or_prompt_drift(product, monkeypatch, fault):
    from backend.banks.esun_spa import rows, capture

    crawler, page, origin, hits, external, captured, _, logout, submit = product
    original = rows.normalize_row
    injected = []

    def normalize(*args, **kwargs):
        result = original(*args, **kwargs)
        if not injected:
            injected.append(True)
            collector = crawler.collector
            if fault in ("row_drift", "document_drift", "unknown", "otp"):
                script = {
                    "row_drift": "document.querySelector('.timeline-card-title').textContent='DRIFT'",
                    "document_drift": "const n=document.querySelector('.timeline-query-continer');n.replaceWith(n.cloneNode(true))",
                    "unknown": "document.body.insertAdjacentHTML('beforeend','<div role=dialog>UNKNOWN</div>')",
                    "otp": "document.body.insertAdjacentHTML('beforeend','<div role=dialog>OTP<input autocomplete=one-time-code></div>')",
                }[fault]
                page.evaluate(script)
            else:
                path = capture.PATHS[{"home_failed": 0, "prequery_failed": 1, "query_failed": 2}.get(fault, 5)]
                hit = collector._latest_spa[path][1]
                if fault == "loading_failed":
                    observer = collector.observers[path]
                    key = next(
                        k
                        for k, record in observer.records.items()
                        if record.get("native_request") is hit._native_request
                    )
                    observer._event("loadingFailed", {"requestId": key})
                else:
                    collector._on_request_failed(hit._native_request)
        return result

    monkeypatch.setattr(rows, "normalize_row", normalize)
    result = crawler.run(origin + "/synthetic", headless=True)
    assert injected and not captured and not external and "data" not in result and "error" in result
    submit.assert_called_once()
    logout.assert_called_once()
    assert [p for p, _ in hits] == list(PATHS)


def test_prelogin_home_is_not_part_of_login_receipt(product, monkeypatch):
    crawler, page, origin, hits, external, captured, _, _, submit = product

    def prepare(p):
        p.evaluate(
            'path => fetch(path, {method:"POST", headers:{"content-type":"application/json"}, body:JSON.stringify({requestBody:{locale:"zh-TW"}})}).then(r=>r.json())',
            PATHS[0],
        )
        p.wait_for_function("()=>true")

    monkeypatch.setattr(crawler, "prepare_login_page", prepare)
    result = crawler.run(origin + "/synthetic", headless=True)
    assert len(captured) == 1 and len(captured[0].twd_txns) == 102
    assert crawler._spa_login_baseline["counts"][PATHS[0]] == 1
    assert [p for p, _ in hits] == [PATHS[0], *PATHS]
    assert "data" not in result and not external
    submit.assert_called_once()


def test_repeated_browser_callback_never_reenters_login(product, monkeypatch):
    crawler, page, origin, hits, external, captured, _, _, submit = product

    def repeated(_url, **kwargs):
        kwargs["page_action"](page)
        kwargs["page_action"](page)

    monkeypatch.setattr(crawler, "_execute_browser_flow", repeated)
    result = crawler.run(origin + "/synthetic", headless=True)
    submit.assert_called_once()
    assert len(captured) == 1 and not external
    assert "data" not in result and result["error"] == "browser_callback_repeated"
    assert [p for p, _ in hits] == list(PATHS)


@pytest.mark.parametrize("owner", [0, 1, 2])
def test_final_publication_rechecks_every_owner_after_cdp_pumps(product, monkeypatch, owner):
    from backend.banks.esun_spa import collection, capture, rows

    crawler, page, origin, _, external, captured, _, _, submit = product
    armed, injected, installed = [], [], []
    original_collect = collection.collect_twd
    original_normalize = rows.normalize_row

    def collect(*args, **kwargs):
        result = original_collect(*args, **kwargs)
        armed.append(True)  # Only the product collect's last publication proof.
        return result

    def normalize(*args, **kwargs):
        result = original_normalize(*args, **kwargs)
        if not installed:
            installed.append(True)
            collector = crawler.collector
            observer = collector.observers[capture.PATHS[5]]
            document = observer._document

            def pump(*a):
                value = document(*a)
                if armed and not injected:
                    injected.append(True)
                    hit = collector._latest_spa[capture.PATHS[owner]][1]
                    collector._on_request_failed(hit._native_request)
                return value

            monkeypatch.setattr(observer, "_document", pump)
        return result

    monkeypatch.setattr(collection, "collect_twd", collect)
    monkeypatch.setattr(rows, "normalize_row", normalize)
    result = crawler.run(origin + "/synthetic", headless=True)
    assert armed and injected and not captured and not external
    assert "data" not in result and "error" in result
    submit.assert_called_once()


@pytest.mark.parametrize("prompt", ["Unknown action", "OTP verification"])
def test_unknown_after_login_stops_without_resubmission(product, monkeypatch, prompt):
    crawler, page, origin, hits, external, captured, _, logout, submit = product
    real_submit = crawler.submit_credentials_once

    def submit_then_prompt(p):
        real_submit(p)
        p.evaluate("t=>document.body.insertAdjacentHTML('beforeend','<div role=dialog>'+t+'</div>')", prompt)

    monkeypatch.setattr(crawler, "submit_credentials_once", submit_then_prompt)
    result = crawler.run(origin + "/synthetic", headless=True)
    assert "error" in result and "data" not in result
    assert not captured and not external and [p for p, _ in hits] == [PATHS[0]]
    submit.assert_called_once()
    logout.assert_not_called()


@pytest.mark.parametrize("prompt", ["Unknown action", "OTP verification"])
def test_unknown_login_prompt_stops_before_credentials(product, prompt):
    crawler, page, origin, hits, external, captured, _, logout, submit = product
    page.evaluate("t=>document.body.insertAdjacentHTML('beforeend','<div role=dialog>'+t+'</div>')", prompt)
    result = crawler.run(origin + "/synthetic", headless=True)
    assert "error" in result and "data" not in result
    submit.assert_not_called()
    logout.assert_not_called()
    assert not hits and not external and not captured
