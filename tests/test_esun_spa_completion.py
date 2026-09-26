"""Loopback TWD progression and persistence; no live-bank success claims."""
from datetime import date
from types import SimpleNamespace

import pytest

from backend.banks.esun_spa import collection
from backend.banks.esun_spa.capture import PATHS
from backend.banks.esun_spa.collection import next_window
from tests.test_esun_spa_product import browser as browser, product as product, FIXTURES
from tests.test_persist_esun_twd_transactions import store_esun_twd as store_esun_twd


def test_empty_window_progresses_contiguously_to_today():
    today = date(2026, 9, 25)
    first = (date(2025, 9, 24), date(2026, 3, 24))
    second = next_window(first[1], today)
    assert second == (date(2026, 3, 25), date(2026, 9, 25))
    assert next_window(second[1], today) is None


def test_autonomous_continuation_requires_post_query_sequence_and_response_cursor(monkeypatch):
    request = dict(account="0000000000001", startDate="a", endDate="b", startIndex=1,
                   count=3, customerInputHashtag=[])
    row = {"debitCredit": "CR", "detailTitle": "SYNTHETIC"}
    detail = dict(startIndex=1, count=3, detailListData=[{"year": "2026", "month": "09", "detailInfo": [row]}],
                  displayErrorCode=None, displayErrorMsg=None)
    response = {"queryDeptTxDtlResult": detail}
    issued = dict(request, startIndex=4)
    body = dict(startIndex=4, count=3, detailListData=[{"year": "2026", "month": "09", "detailInfo": [row]}],
                displayErrorCode=None, displayErrorMsg=None)
    from backend.banks.esun_spa.capture import SpaCollector
    collector = SimpleNamespace(MAX_QUERY_REQUESTS=SpaCollector.MAX_QUERY_REQUESTS,
                                _latest_spa={PATHS[2]: (20, object()), PATHS[5]: (21, object())},
                                issued_count=lambda path: 1 if path == PATHS[5] else 0,
                                query_issued_count=lambda: 2)
    baseline = {"counts": {p: 0 for p in PATHS}}
    seen = []

    def current(owner, snapshot, path):
        seen.append(path)
        assert owner is collector and snapshot is baseline
        return (body, {"requestBody": issued}) if path == PATHS[5] else (response, {"requestBody": request})

    monkeypatch.setattr(collection, "require_current_success", current)
    accepted = collection.owned_autonomous_continuation(collector, baseline, response, {"requestBody": request})
    assert accepted[:2] == (body, {"requestBody": issued})
    groups = accepted[2]
    assert isinstance(groups, list) and groups[0]["detailInfo"] == [row, row]
    assert detail["detailListData"][0]["detailInfo"] == [row]
    assert seen == [PATHS[2], PATHS[5]]
    collector._latest_spa[PATHS[5]] = (19, object())
    with pytest.raises(ValueError, match="continuation_rejected"):
        collection.owned_autonomous_continuation(collector, baseline, response, {"requestBody": request})
    collector._latest_spa[PATHS[5]] = (21, object())
    issued["account"] = "0000000000002"
    with pytest.raises(ValueError, match="continuation_rejected"):
        collection.owned_autonomous_continuation(collector, baseline, response, {"requestBody": request})


@pytest.mark.parametrize("message,groups,expected", [
    ("查詢期間無交易資料,請重新設定查詢區間", [], "explicit_empty_candidate"),
    ("查詢期間無交易資料，請重新設定查詢區間", [], "explicit_empty_candidate"),
    ("查詢期間無交易資料,請重新設定查詢區間", [{"detailInfo": [{"debitCredit": "CR"}]}], "empty_contradiction"),
    ("查詢期間無交易資料,請重新設定查詢區間", None, "empty_shape_unknown"),
    ("查詢區間錯誤", [], "not_empty"),
])
def test_observed_2003_only_classifies_bound_window_candidate(message, groups, expected):
    response = {"queryDeptTxDtlResult": {"displayErrorCode": "2003", "displayErrorMsg": message,
                                           "detailListData": groups}}
    assert collection.empty_window_state(response) == expected
    if expected == "explicit_empty_candidate":
        assert collection.normalized("(2003)" + message) == "(2003)" + collection.EMPTY_MESSAGE
    assert collection.empty_window_state({"queryDeptTxDtlResult": dict(response["queryDeptTxDtlResult"],
        displayErrorCode="2006")}) == "not_empty"


def test_continuation_response_cannot_skip_the_issued_cursor():
    request = dict(account="0000000000001", startDate="START", endDate="END", startIndex=1,
                   count=3, customerInputHashtag=[])
    previous = dict(startIndex=1, count=3, detailListData=[])
    body = dict(startIndex=5, count=3, detailListData=[], displayErrorCode=None, displayErrorMsg=None)
    with pytest.raises(ValueError, match="continuation_rejected"):
        collection.bind_continuation(previous, request, body, dict(request, startIndex=4))


@pytest.fixture
def inventory_product(product, monkeypatch):
    """Loopback accounts; public 9413/2402 switch structure, not live evidence."""
    import json
    from backend.banks.esun_spa import query

    crawler, page, origin, hits, external, captured, state, logout, submit = product
    inventory = [dict(accountNo=f"{i:013}", accountAlias="PRIVATE-NAME") for i in (1, 2)]
    crawler.transaction_cursors = {"twd_transactions": {a["accountNo"]: date(2026, 9, 20) for a in inventory}}
    prequery = json.loads((FIXTURES / "esun_spa_native.json").read_text())["prequery"]
    requests = []

    def respond(route):
        request = route.request.post_data_json["requestBody"]
        path = route.request.url.removeprefix(origin)
        requests.append((path, request))
        account = request.get("account") or inventory[0]["accountNo"]
        if path == PATHS[1]:
            body = dict(prequery, twAccountList=inventory, demandDeptAcc=account)
            if account == inventory[-1]["accountNo"] and request.get("account"):
                if state.get("switch_fault") == "wrong_account":
                    body["demandDeptAcc"] = inventory[0]["accountNo"]
                elif state.get("switch_fault") == "inventory_drift":
                    body["twAccountList"] = inventory[1:]
                elif state.get("switch_fault") == "bank_error":
                    route.fulfill(json={"resultCode": "UNKNOWN", "resultDescription": "SYNTHETIC"})
                    return
        else:
            from datetime import datetime
            start = datetime.fromisoformat(request["startDate"].replace("Z", "+00:00")).astimezone().date().isoformat()
            row = dict(debitCredit="CR", detailTitle=account + start, txDate=start.replace("-", "/"),
                       txTime="12:00:00", amount=1, balance=1, demandDeptAcc=account)
            group = dict(year=start[:4], month=start[5:7], detailInfo=[row])
            if path == PATHS[2] and state.get("page_mode") == "empty_initial":
                body = dict(queryDeptTxDtlResult=dict(startIndex=1, count=100,
                    displayErrorCode="2003", displayErrorMsg="查詢期間無交易資料,請重新設定查詢區間",
                    detailListData=[]), recentHashtag=[])
                route.fulfill(json={"resultCode": "0000", "resultBody": body})
                return
            if path == PATHS[5]:
                if state.get("page_mode") in ("paged", "three_pages", "unbounded"):
                    row["detailTitle"] = str(row["detailTitle"]) + f"-{request['startIndex']}"
                    last = 201 if state["page_mode"] == "paged" else 301 if state["page_mode"] == "three_pages" else None
                    page_rows = state.get("last_page_rows", 0) if last is not None and request["startIndex"] >= last else 100
                    group["detailInfo"] = [dict(row) for _ in range(page_rows)]
                body = dict(startIndex=request["startIndex"], count=100,
                            displayErrorCode=None, displayErrorMsg=None, detailListData=[group])
                if state.get("continuation_fault") == "skip_second" and request["startIndex"] == 201:
                    body["startIndex"] = 301
                if state.get("continuation_fault") == "changed_count":
                    body["count"] = 101
            else:
                # Full-page fixtures must match the native request's 100-row capacity.
                if state.get("page_mode") in ("paged", "three_pages", "unbounded") and account == inventory[0]["accountNo"]:
                    group["detailInfo"] = [dict(row) for _ in range(100)]
                body = dict(queryDeptTxDtlResult=dict(startIndex=1, count=100,
                            displayErrorCode=None, displayErrorMsg=None, detailListData=[group]),
                            recentHashtag=[])
                if state.get("initial_count") is not None:
                    body["queryDeptTxDtlResult"]["count"] = state["initial_count"]
                    group["detailInfo"] = [row]
        route.fulfill(json={"resultCode": "0000", "resultBody": body})

    page.context.route(origin + PATHS[1], respond)
    page.context.route(origin + PATHS[2], respond)
    page.context.route(origin + PATHS[5], respond)
    original = query.query_twd
    installed = []

    def with_inventory(*args, **kwargs):
        if not installed:
            installed.append(True)
            page.evaluate('''args => {
              const install = ([inventory, path, detached]) => {
                const root=document.querySelector('.ctw01002');
                const field=root.querySelector('.combo-input-wrapper[name="accountList"]');
                const block=document.createElement('div');block.className='combo-block';
                field.replaceWith(block);block.append(field);
                const scope=document.createElement('div');
                scope.className='clean-error-scope'; scope.setAttribute('name','accountList');
                (detached ? root : block).append(scope);
                field.addEventListener('click', e=>{
                    if(!e.isTrusted) throw Error('not native');
                    const dialog=document.createElement('dialog');
                    dialog.className='combo-modal';dialog.setAttribute('role','dialog');
                    dialog.setAttribute('aria-modal','true');
                    dialog.innerHTML='<div class="custom-area"><ul class="info-scrollable"></ul></div>';
                    for(const item of inventory) {
                        const li=document.createElement('li');li.setAttribute('role','button');
                        li.textContent=item.accountNo+' '+item.accountAlias;
                        li.onclick=async event=>{
                            if(!event.isTrusted) throw Error('not native');
                            queryState.accountNo=item.accountNo;
                            dialog.remove();
                            const response=await send(path,{account:item.accountNo});
                            field.querySelector('input').value=response.resultBody.demandDeptAcc+' '+response.resultBody.accountAlias;
                            root.querySelector('.timeline-query-continer').textContent='Changed account';
                        };
                        dialog.querySelector('ul').append(li);
                    }
                    scope.append(dialog);dialog.showModal();
                });
              };
              const script=document.createElement('script');
              script.textContent='('+install.toString()+')('+JSON.stringify(args)+')';
              document.body.append(script);
            }''', [inventory, PATHS[1], state.get("switch_fault") == "detached_dialog"])
        return original(*args, **kwargs)

    monkeypatch.setattr(query, "query_twd", with_inventory)
    return product, requests, inventory


@pytest.mark.parametrize("empty,point", [(False, "proof"), (True, "proof"), (True, "retention")])
def test_window_publication_failure_has_own_phase(inventory_product, monkeypatch, empty, point):
    product, _, inventory = inventory_product
    inventory[:] = inventory[:1]
    crawler, _, origin, _, external, captured, state, _, _ = product
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    if empty:
        state["page_mode"] = "empty_initial"
    retain = collection.retain_capture_records
    checked = []

    def retain_with_failure(owner, paths, history, **kwargs):
        if point == "retention":
            checked.append(True)
            raise ValueError("synthetic private revoked receipt")
        attachment, evidence, proof = retain(owner, paths, history, **kwargs)

        def reject():
            checked.append(True)
            raise ValueError("synthetic private publication detail")

        if history is not None:
            assert history[-1] is proof
            history[-1] = reject
        return attachment, evidence, reject

    monkeypatch.setattr(collection, "retain_capture_records", retain_with_failure)
    result = crawler.run(origin + "/synthetic", headless=True)
    assert checked and not captured and not external and "data" not in result
    assert result["collect_diagnostics"]["phase"] == "capture_publication"
    assert "private" not in repr(result)


@pytest.mark.parametrize("empty", [False, True])
def test_second_window_preflight_never_keeps_previous_phase(inventory_product, monkeypatch, empty):
    product, requests, inventory = inventory_product
    inventory[:] = inventory[:1]
    crawler, page, origin, _, external, captured, state, _, _ = product
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    if empty:
        state["page_mode"] = "empty_initial"
    next_window = collection.next_window
    evaluate = page.evaluate
    following_windows = []

    def advance(end, today):
        following = next_window(end, today)
        following_windows.append(following)
        return following

    def fail_next_timeline(expression, arg=None):
        if following_windows and expression == collection.TIMELINE:
            raise ValueError("synthetic private next-window detail")
        return evaluate(expression, arg)

    monkeypatch.setattr(collection, "next_window", advance)
    monkeypatch.setattr(page, "evaluate", fail_next_timeline)
    result = crawler.run(origin + "/synthetic", headless=True)
    assert len(following_windows) == 1 and following_windows[0] is not None
    assert len([p for p, _ in requests if p == PATHS[2]]) == 1
    assert not [p for p, _ in requests if p == PATHS[5]]
    assert not captured and not external and "data" not in result
    assert result["collect_diagnostics"]["phase"] == "transaction_preflight"
    assert "gate" not in result["collect_diagnostics"]
    assert "private" not in repr(result)


@pytest.mark.parametrize("mode,expected_queries", [("incremental", 1), ("full", 6)])
def test_native_empty_account_persists_coverage_and_cursor(inventory_product, store_esun_twd, monkeypatch, mode, expected_queries):
    from backend.core.persist import persist_collected

    product, requests, inventory = inventory_product
    inventory[:] = inventory[:1]
    crawler, _, origin, _, external, captured, state, _, _ = product
    state["page_mode"] = "empty_initial"
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", mode)
    result = crawler.run(origin + "/synthetic", headless=True)
    assert len([p for p, _ in requests if p == PATHS[2]]) == expected_queries
    assert not [p for p, _ in requests if p == PATHS[5]]
    assert captured and captured[0].error is None and captured[0].twd_txns == []
    assert [w["status"] for w in captured[0].history_coverage["domains"][0]["windows"]] == ["explicit_empty"] * expected_queries
    assert "data" in result and "error" not in result and not external
    delta = persist_collected("esun", result["data"], store_esun_twd)
    assert delta["twd_txn_new"] == 0
    assert store_esun_twd.latest_twd_transaction_dates()[inventory[0]["accountNo"]].isoformat() == "2026-09-24"


@pytest.mark.parametrize("empty,late,mode,fault", [
    (True, False, "incremental", "loading_failed"),
    (True, False, "full", "loading_failed"),
    (True, False, "full", "removed"),
    (True, False, "full", "mutated"),
    (False, True, "incremental", "loading_failed"),
    (True, True, "incremental", "loading_failed"),
    (False, True, "incremental", "detached"),
])
def test_invalidated_query_never_publishes_after_collection(inventory_product, monkeypatch, empty, late, mode, fault):
    from backend.banks.esun_spa import products

    product, _, inventory = inventory_product
    inventory[:] = inventory[:1]
    crawler, _, origin, _, external, _, state, _, _ = product
    if empty:
        state["page_mode"] = "empty_initial"
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", mode)
    fired = []

    def invalidate(collector):
        observer = collector.observers[PATHS[2]]
        key = next(iter(observer.records))
        if fault == "loading_failed":
            observer._event("loadingFailed", {"requestId": key})
        elif fault == "removed":
            del observer.records[key]
        else:
            observer.records[key]["bytes"] += 1
        fired.append(True)

    if late:
        def product_failure(crawler, page, collector, baseline):
            invalidate(collector)
            if fault == "detached":
                collector.detach()
            raise ValueError("synthetic product failure")
        monkeypatch.setattr(products, "collect_products", product_failure)
    else:
        original = collection.collect_twd
        def collect(*args, **kwargs):
            value = original(*args, **kwargs)
            invalidate(args[2])
            return value
        monkeypatch.setattr(collection, "collect_twd", collect)
    result = crawler.run(origin + "/synthetic", headless=True)
    assert fired and not external
    assert "error" in result and "data" not in result


def test_base_final_origin_check_cannot_publish_invalidated_rows(inventory_product, monkeypatch):
    product, _, inventory = inventory_product
    inventory[:] = inventory[:1]
    crawler, _, origin, _, external, _, _, _, _ = product
    original_collect, original_guard = crawler.collect, crawler._ensure_collect_origin
    armed, fired = [], []
    def collect(*args, **kwargs):
        value = original_collect(*args, **kwargs)
        armed.append(True)
        return value
    def guard(page):
        original_guard(page)
        if armed and not fired:
            observer = crawler.collector.observers[PATHS[2]]
            observer._event("loadingFailed", {"requestId": next(iter(observer.records))})
            fired.append(True)
    monkeypatch.setattr(crawler, "collect", collect)
    monkeypatch.setattr(crawler, "_ensure_collect_origin", guard)
    result = crawler.run(origin + "/synthetic", headless=True)
    assert fired and not external
    assert "error" in result and "data" not in result


@pytest.mark.parametrize("cap", [3, 4])
def test_terminal_continuation_at_query_limit_finishes(inventory_product, monkeypatch, cap):
    from backend.banks.esun_spa.capture import SpaCollector
    monkeypatch.setattr(SpaCollector, "MAX_QUERY_REQUESTS", cap)
    product, requests, inventory = inventory_product
    inventory[:] = inventory[:1]
    crawler, page, origin, _, external, captured, state, _, _ = product
    state["page_mode"] = "paged"
    page.add_style_tag(content=".timeline-card-container{height:1000px!important}")
    result = crawler.run(origin + "/synthetic", headless=True)
    assert [r["startIndex"] for p, r in requests if p == PATHS[5]] == [101, 201]
    assert captured and captured[0].error is None
    assert "data" in result and "error" not in result and not external


def test_action_budget_leaves_dense_account_partial_and_switches(inventory_product):
    product, requests, inventory = inventory_product
    crawler, page, origin, _, external, captured, state, _, _ = product
    state["page_mode"] = "unbounded"
    page.add_style_tag(content=".timeline-card-container{height:1000px!important}")
    result = crawler.run(origin + "/synthetic", headless=True)
    assert [r["account"] for p, r in requests if p == PATHS[2]] == [a["accountNo"] for a in inventory]
    assert captured and captured[0].error == "spa_collection_incomplete"
    assert "data" not in result and not external


def test_empty_only_coverage_persists_without_synthetic_rows(monkeypatch, store_esun_twd):
    from backend.core.persist import persist_collected

    coverage = {"mode": "incremental", "domains": [{"domain": "twd_transactions",
        "expected": [{"identity": "0000000000001", "start": "2026-09-13", "end": "2026-09-24"}],
        "windows": [{"identity": "0000000000001", "start": "2026-09-13", "end": "2026-09-24",
                     "status": "explicit_empty", "pages": 1}]}]}
    monkeypatch.setattr(collection, "_collect_rows", lambda *args: ([], coverage))
    result = collection.collect_twd(SimpleNamespace(_esun_spa_phase=""), None, None, None, _publication=[])
    assert result.error is None and result.history_coverage == coverage
    data = result.to_dict()
    assert data["twd_txns"] == [] and "twd_txn_results" not in data
    from backend.core.base import BankCollectResult
    legacy = BankCollectResult(bank="esun", twd_txn_results=[], history_coverage=coverage).to_dict()
    assert legacy["twd_txn_results"] == [] and "twd_txns" not in legacy
    delta = persist_collected("esun", data, store_esun_twd)
    assert delta["twd_txn_new"] == 0
    assert store_esun_twd.latest_twd_transaction_dates()["0000000000001"].isoformat() == "2026-09-24"


@pytest.mark.parametrize("mode,expected_queries", [("incremental", 1), ("full", 6)])
def test_short_native_pages_complete_account_windows_in_both_modes(inventory_product, monkeypatch, mode, expected_queries):
    product, requests, inventory = inventory_product
    inventory[:] = inventory[:1]
    crawler, page, origin, _, external, captured, _, _, _ = product
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", mode)
    result = crawler.run(origin + "/synthetic", headless=True)
    queries = [request for path, request in requests if path == PATHS[2]]
    assert len(queries) == expected_queries
    assert not [request for path, request in requests if path == PATHS[5]]
    assert len(captured) == 1 and captured[0].error is None
    coverage = captured[0].history_coverage
    assert coverage is not None and coverage["mode"] == mode
    domain = coverage["domains"][0]
    assert domain["expected"] == [{"identity": inventory[0]["accountNo"],
        "start": "2023-09-24" if mode == "full" else "2026-09-13", "end": "2026-09-24"}]
    assert len(domain["windows"]) == expected_queries
    assert all(window["status"] == "complete" and window["pages"] == 1 for window in domain["windows"])
    assert len(captured[0].twd_txns) == expected_queries
    assert "data" in result and "error" not in result and not external


@pytest.mark.parametrize("mode,expected_rows", [("incremental", 1), ("full", 6)])
def test_complete_short_pages_persist_and_read_back(inventory_product, store_esun_twd, monkeypatch, mode, expected_rows):
    from backend.core.persist import persist_collected

    product, _, inventory = inventory_product
    inventory[:] = inventory[:1]
    crawler, _, origin, _, _, _, _, _, _ = product
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", mode)
    result = crawler.run(origin + "/synthetic", headless=True)
    assert "data" in result and "error" not in result
    delta = persist_collected("esun", result["data"], store_esun_twd)
    assert delta["twd_txn_new"] == expected_rows
    stored = store_esun_twd.conn.execute("SELECT COUNT(*) FROM twd_transactions WHERE account_no = ?",
                                         (inventory[0]["accountNo"],)).fetchone()[0]
    assert stored == expected_rows
    assert store_esun_twd.latest_twd_transaction_dates()[inventory[0]["accountNo"]].isoformat() == "2026-09-24"


def test_incremental_native_switch_queries_each_inventory_account(inventory_product):
    product, requests, inventory = inventory_product
    crawler, page, origin, _, external, captured, _, _, _ = product
    result = crawler.run(origin + "/synthetic", headless=True)
    assert captured, result
    assert [q["account"] for p, q in requests if p == PATHS[2]] == [a["accountNo"] for a in inventory]
    assert [q["account"] for p, q in requests if p == PATHS[1]] == [None, inventory[1]["accountNo"]]
    assert len(captured) == 1 and len(captured[0].twd_txns) == 2
    assert [row["account_no"] for row in captured[0].twd_txns] == [a["accountNo"] for a in inventory]
    assert captured[0].error is None and captured[0].history_coverage == {
        "mode": "incremental", "domains": [{"domain": "twd_transactions",
            "expected": [{"identity": a["accountNo"], "start": "2026-09-13", "end": "2026-09-24"} for a in inventory],
            "windows": [{"identity": a["accountNo"], "start": "2026-09-13", "end": "2026-09-24",
                         "status": "complete", "pages": 1} for a in inventory]}]}
    assert "data" in result and "error" not in result and not external


def test_unactionable_scroll_with_full_page_does_not_claim_completion(inventory_product):
    product, requests, inventory = inventory_product
    inventory[:] = inventory[:1]
    crawler, page, origin, _, external, captured, state, _, _ = product
    state["page_mode"] = "unbounded"
    page.add_style_tag(content=".timeline-query-continer{overflow:hidden!important;height:1000px!important}")
    result = crawler.run(origin + "/synthetic", headless=True)
    assert not [r for path, r in requests if path == PATHS[5]]
    assert captured and captured[0].history_coverage is None
    assert captured[0].error == "spa_collection_incomplete" and "data" not in result and not external


@pytest.mark.parametrize("mode", ["incremental", "full"])
@pytest.mark.parametrize("declared_count", [1, 99, 101])
def test_initial_capacity_mismatch_never_continues_or_publishes(inventory_product, monkeypatch, declared_count, mode):
    product, requests, inventory = inventory_product
    inventory[:] = inventory[:1]
    crawler, page, origin, _, external, captured, state, _, _ = product
    state.update(page_mode="paged", initial_count=declared_count)
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", mode)
    page.add_style_tag(content=".timeline-card-container{height:1000px!important}")
    result = crawler.run(origin + "/synthetic", headless=True)
    assert [r["count"] for p, r in requests if p == PATHS[2]] == [100]
    assert not [r for p, r in requests if p == PATHS[5]]
    assert captured and captured[0].history_coverage is None
    assert captured[0].error == "spa_collection_incomplete"
    assert "data" not in result and not external


@pytest.mark.parametrize("last_page_rows", [0, 99])
def test_owned_short_success_after_full_pages_persists_without_another_request(inventory_product, store_esun_twd, last_page_rows):
    product, requests, inventory = inventory_product
    inventory[:] = inventory[:1]
    crawler, page, origin, _, external, captured, state, _, _ = product
    state.update(page_mode="paged", last_page_rows=last_page_rows)
    page.add_style_tag(content=".timeline-card-container{height:1000px!important}")
    result = crawler.run(origin + "/synthetic", headless=True)
    assert [r["startIndex"] for path, r in requests if path == PATHS[5]] == [101, 201]
    assert all(r["count"] == 100 for p, r in requests if p in (PATHS[2], PATHS[5]))
    assert captured and captured[0].error is None
    assert captured[0].history_coverage["domains"][0]["windows"] == [{
        "identity": inventory[0]["accountNo"], "start": "2026-09-13", "end": "2026-09-24",
        "status": "complete", "pages": 3}]
    assert len(captured[0].twd_txns) == 200 + last_page_rows and "data" in result and "error" not in result and not external
    from backend.core.persist import persist_collected
    delta = persist_collected("esun", result["data"], store_esun_twd)
    assert delta["twd_txn_new"] == 200 + last_page_rows
    assert store_esun_twd.conn.execute("SELECT COUNT(*) FROM twd_transactions").fetchone()[0] == 200 + last_page_rows
    assert store_esun_twd.latest_twd_transaction_dates()[inventory[0]["accountNo"]] == date(2026, 9, 24)


def test_native_switch_after_verified_positive_continuation(inventory_product):
    product, requests, inventory = inventory_product
    crawler, page, origin, _, external, captured, state, _, _ = product
    state["page_mode"] = "paged"
    page.add_style_tag(content=".timeline-card-container{height:1000px!important}")
    result = crawler.run(origin + "/synthetic", headless=True)
    assert len(captured) == 1, result
    assert [q["account"] for p, q in requests if p == PATHS[5]] == [inventory[0]["accountNo"]] * 2
    assert [q["account"] for p, q in requests if p == PATHS[2]] == [a["accountNo"] for a in inventory]
    assert len(captured) == 1 and len(captured[0].twd_txns) == 201
    assert [row["account_no"] for row in captured[0].twd_txns] == [inventory[0]["accountNo"]] * 200 + [inventory[1]["accountNo"]]
    assert captured[0].error is None
    assert [w["pages"] for w in captured[0].history_coverage["domains"][0]["windows"]] == [3, 1]
    assert "data" in result and "error" not in result and not external


def test_two_owned_native_continuations_before_account_switch(inventory_product):
    product, requests, inventory = inventory_product
    crawler, page, origin, _, external, captured, state, _, _ = product
    state["page_mode"] = "three_pages"
    page.add_style_tag(content=".timeline-card-container{height:1000px!important}")
    result = crawler.run(origin + "/synthetic", headless=True)
    assert captured, result
    assert [(q["account"], q["startIndex"]) for p, q in requests if p == PATHS[5]] == [
        (inventory[0]["accountNo"], 101), (inventory[0]["accountNo"], 201),
        (inventory[0]["accountNo"], 301)]
    assert [q["account"] for p, q in requests if p == PATHS[2]] == [a["accountNo"] for a in inventory]
    assert [r["account_no"] for r in captured[0].twd_txns] == [inventory[0]["accountNo"]] * 300 + [inventory[1]["accountNo"]]
    assert captured[0].error is None
    assert [w["pages"] for w in captured[0].history_coverage["domains"][0]["windows"]] == [4, 1]
    assert "data" in result and "error" not in result and not external


@pytest.mark.parametrize("fault", ["loading_failed", "removed", "mutated"])
@pytest.mark.parametrize("owner", [1, 2, 5])
def test_final_publication_rechecks_historical_continuation(inventory_product, monkeypatch, fault, owner):
    product, requests, inventory = inventory_product
    crawler, page, origin, _, external, captured, state, _, _ = product
    state["page_mode"] = "three_pages"
    page.add_style_tag(content=".timeline-card-container{height:1000px!important}")
    saved, injected = [], []
    original_step = collection.continue_twd_once
    original_collect = collection.collect_twd

    def step(*args, **kwargs):
        result = original_step(*args, **kwargs)
        if not saved:
            collector = crawler.collector
            hit = collector._latest_spa[PATHS[owner]][1]
            observer = collector.observers[PATHS[owner]]
            key, record = next((k, r) for k, r in observer.records.items()
                               if r.get("native_request") is hit._native_request)
            saved.append((observer, key, record, hit._native_request))
        return result

    def collect(*args, **kwargs):
        result = original_collect(*args, **kwargs)
        observer, key, record, request = saved[0]
        assert crawler.collector._latest_spa[PATHS[owner]][1]._native_request is not request
        if fault == "loading_failed":
            observer._event("loadingFailed", {"requestId": key})
        elif fault == "removed":
            del observer.records[key]
        else:
            record["bytes"] += 1
        injected.append(True)
        return result

    monkeypatch.setattr(collection, "continue_twd_once", step)
    monkeypatch.setattr(collection, "collect_twd", collect)
    result = crawler.run(origin + "/synthetic", headless=True)
    assert [q["account"] for p, q in requests if p == PATHS[2]] == [a["accountNo"] for a in inventory]
    assert injected and not captured and "data" not in result and "error" in result and not external


def test_empty_continuation_keeps_render_proof_until_account_switch(inventory_product, monkeypatch):
    from backend.banks.esun_spa import rows

    product, requests, inventory = inventory_product
    crawler, page, origin, _, external, captured, state, _, _ = product
    state["page_mode"] = "three_pages"
    page.add_style_tag(content=".timeline-card-container{height:1000px!important}")
    injected = []
    original = rows.normalize_row

    def normalize(*args, **kwargs):
        result = original(*args, **kwargs)
        if not injected:
            page.evaluate("document.querySelector('.timeline-query-continer').textContent = 'DRIFT'")
            injected.append(True)
        return result

    monkeypatch.setattr(rows, "normalize_row", normalize)
    result = crawler.run(origin + "/synthetic", headless=True)
    assert [q["startIndex"] for p, q in requests if p == PATHS[5]] == [101, 201, 301]
    assert injected and not captured and "data" not in result and "error" in result and not external
    assert [q["account"] for p, q in requests if p == PATHS[2]] == [inventory[0]["accountNo"]]


def test_second_continuation_cursor_skip_rejects_all_rows(inventory_product):
    product, requests, inventory = inventory_product
    crawler, page, origin, _, external, captured, state, _, _ = product
    state.update(page_mode="three_pages", continuation_fault="skip_second")
    page.add_style_tag(content=".timeline-card-container{height:1000px!important}")
    result = crawler.run(origin + "/synthetic", headless=True)
    assert [q["startIndex"] for p, q in requests if p == PATHS[5]] == [101, 201]
    assert not captured and "data" not in result and not external
    assert "error" in result


def test_changed_page_size_stops_before_an_unbound_cursor(inventory_product):
    product, requests, inventory = inventory_product
    crawler, page, origin, _, external, captured, state, _, _ = product
    state.update(page_mode="unbounded", continuation_fault="changed_count")
    page.add_style_tag(content=".timeline-card-container{height:1000px!important}")
    result = crawler.run(origin + "/synthetic", headless=True)
    assert [q["startIndex"] for p, q in requests if p == PATHS[5]] == [101]
    assert [q["account"] for p, q in requests if p == PATHS[2]] == [a["accountNo"] for a in inventory]
    assert captured and captured[0].history_coverage is None and captured[0].error == "spa_collection_incomplete"
    assert "data" not in result and not external


def test_dense_account_reserves_query_for_remaining_inventory(inventory_product, monkeypatch):
    from backend.banks.esun_spa.capture import SpaCollector
    monkeypatch.setattr(SpaCollector, "MAX_QUERY_REQUESTS", 8)
    product, requests, inventory = inventory_product
    crawler, page, origin, _, external, captured, state, _, _ = product
    state["page_mode"] = "unbounded"
    page.add_style_tag(content=".timeline-card-container{height:1000px!important}")
    result = crawler.run(origin + "/synthetic", headless=True)
    assert [q["account"] for p, q in requests if p == PATHS[2]] == [a["accountNo"] for a in inventory]
    assert len([q for p, q in requests if p in (PATHS[2], PATHS[5])]) == 3
    assert [q["startIndex"] for p, q in requests if p == PATHS[5]] == [101]
    assert captured and captured[0].history_coverage is None
    assert captured[0].error == "spa_collection_incomplete" and "data" not in result and not external


def test_full_dense_account_reserves_all_remaining_windows(inventory_product, monkeypatch):
    from backend.banks.esun_spa.capture import SpaCollector

    monkeypatch.setattr(SpaCollector, "MAX_QUERY_REQUESTS", 12)
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    product, requests, inventory = inventory_product
    crawler, page, origin, _, external, captured, state, _, _ = product
    state["page_mode"] = "unbounded"
    page.add_style_tag(content=".timeline-card-container{height:1000px!important}")
    result = crawler.run(origin + "/synthetic", headless=True)
    assert [q["account"] for p, q in requests if p == PATHS[2]] == (
        [inventory[0]["accountNo"]] + [inventory[1]["accountNo"]] * 6)
    assert len([q for p, q in requests if p in (PATHS[2], PATHS[5])]) == 12
    assert captured and captured[0].error == "spa_collection_incomplete"
    assert captured[0].history_coverage is None and "data" not in result and not external


def test_full_native_inventory_persists_both_accounts(inventory_product, store_esun_twd, monkeypatch):
    from datetime import datetime, timedelta
    from backend.core.persist import persist_collected
    product, requests, inventory = inventory_product
    crawler, page, origin, _, external, captured, _, _, _ = product
    monkeypatch.setenv("BANK_CRAWLER_HISTORY_MODE", "full")
    result = crawler.run(origin + "/synthetic", headless=True)
    queries = [q for p, q in requests if p == PATHS[2]]
    assert len(queries) == 12, result
    assert [q["account"] for q in queries] == [inventory[0]["accountNo"]] * 6 + [inventory[1]["accountNo"]] * 6
    days = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone().date()
    assert days(queries[0]["startDate"]) == date(2023, 9, 24)
    assert days(queries[5]["endDate"]) == date(2026, 9, 24)
    assert all(days(b["startDate"]) == days(a["endDate"]) + timedelta(days=1)
               for a, b in zip(queries[:5], queries[1:6]))
    assert days(queries[6]["startDate"]) == date(2023, 9, 24)
    assert len(captured) == 1 and len(captured[0].twd_txns) == 12
    assert captured[0].error is None and captured[0].history_coverage is not None
    assert "data" in result and "error" not in result and not external
    delta = persist_collected("esun", result["data"], store_esun_twd)
    assert delta["twd_txn_new"] == 12
    assert store_esun_twd.latest_twd_transaction_dates() == {
        a["accountNo"]: date(2026, 9, 24) for a in inventory}


def test_native_switch_rejects_detached_dialog_before_account_request(inventory_product):
    product, requests, inventory = inventory_product
    crawler, page, origin, _, external, captured, state, _, _ = product
    state["switch_fault"] = "detached_dialog"
    result = crawler.run(origin + "/synthetic", headless=True)
    assert [q["account"] for p, q in requests if p == PATHS[1]] == [None]
    assert [q["account"] for p, q in requests if p == PATHS[2]] == [inventory[0]["accountNo"]]
    assert not captured and "data" not in result and not external


@pytest.mark.parametrize("fault", ["wrong_account", "inventory_drift", "bank_error"])
def test_native_switch_rejects_unbound_prequery(inventory_product, fault):
    product, requests, inventory = inventory_product
    crawler, page, origin, _, external, captured, state, _, _ = product
    state["switch_fault"] = fault
    result = crawler.run(origin + "/synthetic", headless=True)
    assert [q["account"] for p, q in requests if p == PATHS[2]] == [inventory[0]["accountNo"]]
    assert [q["account"] for p, q in requests if p == PATHS[1]] == [None, inventory[1]["accountNo"]]
    assert not captured and "data" not in result and not external
