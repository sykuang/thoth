"""Owned native TWD query and bounded source-checked window progression."""

import re
from time import monotonic
from backend.core.base import _OriginGuardProxy
from .capture import PATHS, require_current_success
from .navigation import build_menu_plan
from .navigation import MENU, navigate_twd


def normalized(value):
    from unicodedata import normalize

    return re.sub(r"\s+", "", normalize("NFKC", value)) if type(value) is str else ""


TIMEOUT = 2500
BLOCKER_TYPE = r"""n => {
 const types = [
  ['native_menu', '.mib-menu.mib-modal > dialog.menu-modal-container.menu-dialog'],
  ['generic_dialog', 'dialog.mib-modal-container[role="dialog"]'],
  ['driver_tour', '.driver-popover[role="dialog"]'],
  ['login_input', '[id="loginform:custid"], input[name="id"]'],
  ['css_modal', '.modal.show']
 ].filter(([,s]) => n.matches(s));
 return types.length === 1 ? types[0][0] : types.length ? 'unknown' : 'other';
}"""
BLOCKER_SNAPSHOT = (
    r"""(nodes, [querying, menu, selecting]) => {
 const classify = """
    + BLOCKER_TYPE
    + r""";
 if (!nodes.length) return [true, 'unknown', 'none'];
 if ((querying || selecting) && nodes.length !== 1) return [false, 'unknown', 'cardinality'];
 for (const n of nodes) {
   const allowed = selecting ? n.matches('#layout-content .ctw01002 .combo-block:has(> .combo-input-wrapper[name="accountList"][role="button"]) > .clean-error-scope[name="accountList"] > dialog.combo-modal[open][role="dialog"][aria-modal="true"]') : !querying ? n.matches(menu) :
     n.matches('dialog.calendar-select-popup-area[open][role="dialog"]') &&
     !!n.closest('#layout-content .ctw01002 .is-medium-hide > .search-helper > form.search-helper-container .calendar-base') &&
     ['startDate','endDate'].includes(n.closest('.calendar-base').querySelector('input[readonly]')?.name);
   if (!allowed) return [false, classify(n), 'predicate'];
 }
 return [true, selecting ? 'owned_account' : querying ? 'owned_calendar' : 'native_menu', 'none'];
}"""
)
DESTINATION = r"""() => {
 const visible = n => {
   for(let p=n;p;p=p.parentElement) {
     if(p.hidden || p.inert || p.getAttribute('aria-hidden')==='true' || p.getAttribute('aria-disabled')==='true') return false;
   }
   return n.getClientRects().length>0 && getComputedStyle(n).visibility==='visible';
 };
 const layouts=[...document.querySelectorAll('#layout-content')];
 const roots=[...document.querySelectorAll('#layout-content .ctw01002')];
 const tabs=[...document.querySelectorAll('#tw')];
 if(layouts.length!==1 || layouts[0].classList.contains('not-login') || !visible(layouts[0]) ||
    roots.length!==1 || !visible(roots[0]) || tabs.length!==1 || !visible(tabs[0])) return false;
 const body=roots[0].closest('.tab-body'), header=tabs[0].parentElement;
 const container=body && body.parentElement;
 return !!container && container.matches('.mib-tab') && layouts[0].contains(container) &&
 roots[0].closest('.mib-tab')===container && header.matches('.tab-header[role="tablist"]') &&
 header.parentElement===container && container.querySelectorAll(':scope > .tab-header').length===1 &&
 container.querySelectorAll(':scope > .tab-body').length===1 &&
 tabs[0].getAttribute('role')==='tab' && tabs[0].getAttribute('aria-selected')==='true';
}"""


def validate_query_response(response):
    """Frozen conservative shape, not proof of actual bank field types."""
    detail = response.get("queryDeptTxDtlResult") if type(response) is dict else None
    if type(detail) is not dict:
        raise ValueError("SPA probe rejected")
    if not ("displayErrorCode" in detail and detail["displayErrorCode"] is None):
        raise ValueError("SPA probe rejected")
    if not ("displayErrorMsg" in detail and detail["displayErrorMsg"] is None):
        raise ValueError("SPA probe rejected")
    if type(detail.get("startIndex")) is not int:
        raise ValueError("SPA probe rejected")
    if type(detail.get("count")) is not int:
        raise ValueError("SPA probe rejected")
    if type(detail.get("detailListData")) is not list:
        raise ValueError("SPA probe rejected")
    if type(response.get("recentHashtag")) is not list:
        raise ValueError("SPA probe rejected")
    for group in detail["detailListData"]:
        if type(group) is not dict:
            raise ValueError("SPA probe rejected")
        if type(group.get("year")) is not str:
            raise ValueError("SPA probe rejected")
        if type(group.get("month")) is not str:
            raise ValueError("SPA probe rejected")
        if type(group.get("detailInfo")) is not list:
            raise ValueError("SPA probe rejected")
        for row in group["detailInfo"]:
            if type(row) is not dict:
                raise ValueError("SPA probe rejected")
            if type(row.get("debitCredit")) is not str:
                raise ValueError("SPA probe rejected")


EMPTY_MESSAGE = "查詢期間無交易資料,請重新設定查詢區間"


def next_window(end, today):
    """Advance a verified empty explicit window, never a positive page."""
    import calendar
    from datetime import date, timedelta

    if end >= today:
        return None
    start = end + timedelta(days=1)
    year, month = divmod(start.year * 12 + start.month - 1 + 6, 12)
    return start, min(today, date(year, month + 1, min(start.day, calendar.monthrange(year, month + 1)[1])))


def empty_window_state(response):
    detail = response.get("queryDeptTxDtlResult") if type(response) is dict else None
    if (
        type(detail) is not dict
        or detail.get("displayErrorCode") != "2003"
        or normalized(detail.get("displayErrorMsg")) != EMPTY_MESSAGE
    ):
        return "not_empty"
    groups = detail.get("detailListData")
    if type(groups) is not list or any((type(g) is not dict or type(g.get("detailInfo")) is not list for g in groups)):
        return "empty_shape_unknown"
    if any((group["detailInfo"] for group in groups)):
        return "empty_contradiction"
    return "explicit_empty_candidate"


EMPTY_CARD = r"""() => {
 const nodes=[...document.querySelectorAll('#layout-content .ctw01002 .timeline-query-continer .timeline-card-outer-container > .timeline-card-container > .timeline-card-no-data-msg')];
 const visible=n=>{for(let p=n;p;p=p.parentElement) if(p.hidden||p.inert||p.getAttribute('aria-hidden')==='true'||getComputedStyle(p).visibility!=='visible')return false;return !!n.getClientRects().length};
 return nodes.length===1 && visible(nodes[0]) ? nodes[0].innerText : null;
}"""
TIMELINE = r"""() => {
 const nodes=[...document.querySelectorAll('#layout-content .ctw01002 .timeline-query-continer')];
 if(nodes.length!==1 || !nodes[0].getClientRects().length || getComputedStyle(nodes[0]).visibility!=='visible') return null;
 return nodes[0].innerText;
}"""
RENDERED_OCCURRENCES = r"""errorMessage => {
 const visible=n=>{
   for(let p=n;p;p=p.parentElement) {
     const style=getComputedStyle(p);
     if(p.hidden || p.inert || p.getAttribute('aria-hidden')==='true' ||
        style.display==='none' || style.visibility!=='visible') return false;
   }
   return n.isConnected && [...n.getClientRects()].some(r=>r.width>0 && r.height>0);
 };
 const roots=[...document.querySelectorAll('#layout-content .ctw01002 .timeline-query-continer')];
 if(roots.length!==1 || !visible(roots[0])) return null;
 const children=[...roots[0].children];
 const footer=children.at(-1);
 if(typeof errorMessage==='string' && footer?.matches('.timeline-card-buttom')) {
   const messages=footer.querySelectorAll(':scope > span.timeline-card-buttom-msg');
   if(!visible(footer) || messages.length!==1 || footer.children.length!==1 ||
      !visible(messages[0]) || messages[0].textContent!==errorMessage ||
      footer.querySelector('button,a,input,select,textarea,[role=button],[onclick]')) return null;
   children.pop();
 }
 return children.map(group => {
   const year=group.querySelectorAll('.timeline-top-sub-container .timeline-year');
   const month=group.querySelectorAll('.timeline-top-sub-container .timeline-month');
   if(!visible(group) || year.length!==1 || month.length!==1 || !visible(year[0]) || !visible(month[0])) return null;
   // Reject invisible occurrences rather than filtering them out of the multiset.
   const cards=[...group.querySelectorAll('.timeline-card-outer-container .timeline-card-container')];
   if(cards.some(card=>!visible(card) || !card.querySelector('.timeline-card-sub-container') ||
      card.querySelectorAll('.timeline-card-title').length!==1 || !visible(card.querySelector('.timeline-card-title')))) return null;
   return [year[0].textContent.trim(),month[0].textContent.trim(),
           cards.map(card=>card.querySelector('.timeline-card-title').textContent.trim())];
 });
}"""


def require_rendered_occurrences(page, groups, document=None, *, error_message=None):
    rendered = (
        page.evaluate(RENDERED_OCCURRENCES, error_message)
        if document is None
        else document.evaluate(
            '(d, errorMessage) => d.doc===document && d.node===document.querySelector(".timeline-query-continer") ? ('
            + RENDERED_OCCURRENCES
            + ")(errorMessage) : null", error_message
        )
    )
    if (
        type(rendered) is not list
        or len(rendered) != len(groups)
        or any(
            (
                type(g) is not dict
                or type(g.get("detailInfo")) is not list
                or type(r) is not list
                or (len(r) != 3)
                or (
                    r
                    != [g.get("year"), g.get("month", "").zfill(2), [row.get("detailTitle") for row in g["detailInfo"]]]
                )
                for g, r in zip(groups, rendered)
            )
        )
    ):
        raise ValueError("SPA probe rejected")


def bind_continuation(previous, request, body, issued):
    """9413 response-derived cursor; 7108 ordered occurrence-preserving merge."""
    from copy import deepcopy

    def require(ok):
        if not ok:
            raise ValueError("continuation_rejected")

    keys = {"account", "startDate", "endDate", "startIndex", "count", "customerInputHashtag"}
    require(type(request) is dict and set(request) == keys and (type(issued) is dict) and (set(issued) == keys))
    require(type(previous) is dict and type(body) is dict)
    for value in (previous.get("startIndex"), previous.get("count"), body.get("startIndex"), body.get("count")):
        require(type(value) is int and 0 < value <= 9007199254740991)
    cursor = previous["startIndex"] + previous["count"]
    require(cursor <= 9007199254740991 and body["startIndex"] == cursor)
    require(type(issued["startIndex"]) is int and type(issued["count"]) is int)
    require(issued == dict(request, startIndex=cursor, count=previous["count"]))
    require(body.get("displayErrorCode") is None and body.get("displayErrorMsg") is None)
    groups = deepcopy(previous["detailListData"])
    validate_query_response(
        {"queryDeptTxDtlResult": dict(body, displayErrorCode=None, displayErrorMsg=None), "recentHashtag": []}
    )
    for collection in (groups, body["detailListData"]):
        pairs = [(g["year"], g["month"]) for g in collection]
        require(len(pairs) == len(set(pairs)))
    for group in body["detailListData"]:
        matches = [g for g in groups if (g["year"], g["month"]) == (group["year"], group["month"])]
        if matches:
            matches[0]["detailInfo"].extend(deepcopy(group["detailInfo"]))
        else:
            groups.append(deepcopy(group))
    return groups


def owned_autonomous_continuation(collector, baseline, response, envelope):
    """Accept a native continuation only after its owning explicit query."""
    try:
        if (
            collector.issued_count(PATHS[5]) != baseline["counts"][PATHS[5]] + 1
            or collector.query_issued_count() > collector.MAX_QUERY_REQUESTS
            or require_current_success(collector, baseline, PATHS[2]) != (response, envelope)
            or collector._latest_spa[PATHS[5]][0] <= collector._latest_spa[PATHS[2]][0]
        ):
            raise ValueError
        body, issued = require_current_success(collector, baseline, PATHS[5])
        merged = bind_continuation(response["queryDeptTxDtlResult"], envelope["requestBody"], body, issued["requestBody"])
        return body, issued, merged
    except (KeyError, TypeError, ValueError):
        raise ValueError("continuation_rejected") from None


def continue_twd_once(
    crawler, page, collector, initial_baseline, response, envelope, revalidate, window, action_budget, *,
    _publication=None, previous=None, accumulated=None, prior_baseline=None, _history=None
):
    """Bind one owned continuation, or issue one native desktop wheel."""
    from .query import ROOT, FORM

    native = _OriginGuardProxy._unwrap(page)
    crawler._esun_spa_phase = "continuation_preflight"
    if previous is not None and (
        type(previous) is not tuple or len(previous) != 2 or type(accumulated) is not list or type(prior_baseline) is not dict
    ):
        raise ValueError("continuation_rejected")
    groups = accumulated if previous is not None else response["queryDeptTxDtlResult"]["detailListData"]
    detail = previous[0] if previous is not None else response["queryDeptTxDtlResult"]
    source_request = previous[1]["requestBody"] if previous is not None else envelope["requestBody"]

    def require(ok):
        if not ok:
            raise ValueError("continuation_rejected")

    def reserve():
        if (
            type(action_budget) is not list
            or len(action_budget) != 1
            or type(action_budget[0]) is not int
            or (not 0 <= action_budget[0] < 32)
        ):
            raise ValueError("native_action_budget_exhausted")
        action_budget[0] += 1

    def owner():
        revalidate()
        require(
            all(
                (
                    page.locator(ROOT + " " + FORM + " input[name=" + name + "][readonly]").input_value()
                    == day.strftime("%Y/%m/%d")
                    for name, day in zip(("startDate", "endDate"), window)
                )
            )
        )

    def counts(extra=0):
        require(
            all(
                (
                    collector.issued_count(path)
                    == initial_baseline["counts"][path] + (1 if path == PATHS[2] else extra if path == PATHS[5] else 0)
                    for path in PATHS
                )
            )
        )
        require(collector.query_issued_count() <= collector.MAX_QUERY_REQUESTS)

    existing = collector.issued_count(PATHS[5]) - initial_baseline["counts"][PATHS[5]]
    require(1 <= existing <= collector.MAX_QUERY_REQUESTS if previous is not None else existing in (0, 1))
    if previous is not None:
        require(require_current_success(collector, prior_baseline, PATHS[5]) == previous)
    preissued = existing if previous is None else 0
    counts(existing)
    owner()
    counts(existing)
    require(any((g["detailInfo"] for g in groups)))
    require(all((type(detail.get(k)) is int and 0 < detail[k] <= 9007199254740991 for k in ("startIndex", "count"))))
    require_rendered_occurrences(page, groups)
    geometry_script = r"""d => {
      const all=[...document.querySelectorAll('.timeline-query-continer')];
      if(all.length!==1 || !all[0].closest('#layout-content .ctw01002') || innerWidth<1200 ||
         d && (d.doc!==document || d.node!==all[0])) return null;
      const n=all[0],r=n.getBoundingClientRect(),style=getComputedStyle(n);
      for(let p=n;p;p=p.parentElement) {
        const s=getComputedStyle(p);
        if(p.hidden || p.inert || p.getAttribute('aria-hidden')==='true' ||
           s.display==='none' || s.visibility!=='visible') return null;
      }
      const left=Math.max(0,r.left),right=Math.min(innerWidth,r.right);
      const top=Math.max(0,r.top),bottom=Math.min(innerHeight,r.bottom);
      if(right<=left || bottom<=top) return null;
      const x=(left+right)/2,y=(top+bottom)/2;
      if(!n.contains(document.elementFromPoint(x,y))) return null;
      // Official 7108 switches short timelines to its window-scroll listener.
      if(n.scrollHeight<800) {
        const doc=document.scrollingElement;
        const remaining=doc.scrollHeight-doc.clientHeight-doc.scrollTop;
        return remaining>0 ? [x,y,remaining] : null;
      }
      if(n.scrollHeight<=n.clientHeight+n.scrollTop || !['auto','scroll'].includes(style.overflowY)) return null;
      return [x,y,n.scrollHeight-n.clientHeight-n.scrollTop];
    }"""
    document = native.evaluate_handle("()=>({doc:document,node:document.querySelector('.timeline-query-continer')})")
    accepted = None
    observed_error = None
    baseline = initial_baseline if preissued else collector.snapshot()

    def finish(values, reason):
        crawler._esun_spa_phase = "continuation_finalize"
        if _publication is not None:
            paths = list(PATHS[:3]) + ([PATHS[5]] if accepted is not None or observed_error is not None or previous is not None else [])
            attachment, evidence, prove_records = retain_capture_records(
                collector, paths, _history, observed_error=observed_error is not None)
            def prove():
                owner()
                require(require_current_success(collector, initial_baseline, PATHS[2]) == (response, envelope))
                if accepted is not None:
                    require(require_current_success(collector, baseline, PATHS[5]) == accepted)
                elif observed_error is not None:
                    require(collector._require_current_continuation_observation(baseline) == observed_error)
                elif previous is not None:
                    require(require_current_success(collector, prior_baseline, PATHS[5]) == previous)
                require_rendered_occurrences(page, values, document,
                    error_message=observed_error[0].get('resultDescription') if observed_error is not None else None)
                require(collector._attachment is attachment and collector.page is native)
                for path, latest, observation, hit, observer, record, saved in evidence:
                    now = collector._latest_spa.get(path)
                    require(now is not None and now[0] == latest[0] and (now[1] is latest[1]))
                    require(collector.observers.get(path) is observer and (not observer.bad))
                    require(
                        any((r is record for r in observer.records.values()))
                        and record == saved
                        and (not record["bad"])
                    )
                    if observation is not None:
                        require(collector._continuation_observation is observation)
                    else:
                        require(any((h is hit for h in collector.hits)))
                counts(existing + (1 if (accepted is not None or observed_error is not None) and not preissued else 0))
                for proof in _history if _history is not None else (prove_records,):
                    proof()  # In-memory sweep after every event-pumping call.

            _publication.append(prove)
        return (values, reason)

    crawler._esun_spa_phase = "continuation_geometry"
    request_body = source_request
    page_count = detail.get("count")
    row_count = sum(len(group.get("detailInfo", ())) for group in detail["detailListData"] if type(group) is dict)
    require(
        type(request_body) is dict
        and type(request_body.get("count")) is int
        and type(page_count) is int
        and 0 < page_count <= 9007199254740991
        and row_count <= page_count
    )
    # Product policy: an owned successful page below the matching requested
    # capacity completes the window after cursor/render/publication checks.
    # This is not a bank-issued terminal receipt; never issue a speculative next page.
    if page_count == request_body["count"] and row_count < page_count:
        require(not preissued)
        return finish(groups, "short_page")
    # A bank-adjusted page size does not prove that the next cursor is safe.
    if page_count != request_body["count"]:
        return finish(groups, "cursor_unverified")
    geometry = native.evaluate(geometry_script)

    def current():
        owner()
        require(
            document.evaluate(
                "d=>d.doc===document && d.node===document.querySelector('.timeline-query-continer') && document.querySelectorAll('.timeline-query-continer').length===1"
            )
        )
        require(require_current_success(collector, initial_baseline, PATHS[2]) == (response, envelope))

    primary = None
    try:
        current()
        counts(existing)
        if preissued:
            crawler._esun_spa_phase = "continuation_response"
            body, issued_envelope, merged = owned_autonomous_continuation(
                collector, baseline, response, envelope
            )
            crawler._esun_spa_phase = "continuation_render"
            deadline = monotonic() + TIMEOUT / 1000
            while True:
                current()
                counts(existing)
                try:
                    require_rendered_occurrences(page, merged, document)
                    break
                except ValueError:
                    require(monotonic() < deadline)
                    page.wait_for_timeout(20)
            accepted = (body, issued_envelope)
            return finish(merged, "pagination_unverified")
        ready_deadline = monotonic() + TIMEOUT / 1000
        while geometry is None and monotonic() < ready_deadline:
            page.wait_for_timeout(20)
            current()
            counts(existing)
            geometry = document.evaluate(geometry_script)
        if geometry is None:
            return finish(groups, "native_scroll_unproved")
        if collector.query_issued_count() >= collector.MAX_QUERY_REQUESTS:
            return finish(groups, "query_budget_exhausted")
        require(
            type(action_budget) is list
            and len(action_budget) == 1
            and (type(action_budget[0]) is int)
            and (0 <= action_budget[0] <= 32)
        )
        if action_budget[0] > 30:
            return finish(groups, "native_action_budget_exhausted")
        require(document.evaluate(geometry_script) == geometry)
        counts(existing)
        reserve()
        crawler._esun_spa_phase = "continuation_action"
        native.mouse.move(*geometry[:2])
        current()
        counts(existing)
        if collector.query_issued_count() >= collector.MAX_QUERY_REQUESTS:
            return finish(groups, "query_budget_exhausted")
        require(document.evaluate(geometry_script) == geometry)
        counts(existing)
        reserve()
        native.mouse.wheel(0, geometry[2])
        crawler._esun_spa_phase = "continuation_wait"
        deadline = monotonic() + TIMEOUT / 1000
        while True:
            current()
            require(collector.issued_count(PATHS[5]) <= baseline["counts"][PATHS[5]] + 1)
            require(collector.query_issued_count() <= collector.MAX_QUERY_REQUESTS)
            try:
                body, issued_envelope = require_current_success(collector, baseline, PATHS[5])
            except ValueError:
                try:
                    failed_body, failed_envelope = collector._require_current_continuation_observation(baseline)
                except ValueError:
                    pass
                else:
                    issued = failed_envelope.get("requestBody")
                    wanted = dict(
                        source_request,
                        startIndex=detail["startIndex"] + detail["count"],
                        count=detail["count"],
                    )
                    require(
                        type(issued) is dict
                        and issued == wanted
                        and (type(issued.get("startIndex")) is int)
                        and (type(issued.get("count")) is int)
                    )
                    current()
                    collector._require_current_continuation_observation(baseline)
                    counts(existing + 1)
                    observed_error = (failed_body, failed_envelope)
                    return finish(groups, "continuation_error_observed")
                require(monotonic() < deadline)
                page.wait_for_timeout(20)
                continue
            crawler._esun_spa_phase = "continuation_response"
            merged = bind_continuation(
                dict(detail, detailListData=groups), source_request, body, issued_envelope["requestBody"]
            )
            crawler._esun_spa_phase = "continuation_render"
            try:
                require_rendered_occurrences(page, merged)
            except ValueError:
                require(monotonic() < deadline)
                page.wait_for_timeout(20)
                continue
            current()
            require(require_current_success(collector, baseline, PATHS[5]) == (body, issued_envelope))
            counts(existing + 1)
            accepted = (body, issued_envelope)
            return finish(merged, "pagination_unverified")
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            if _publication is None or primary is not None:
                document.dispose()
        except BaseException:
            if primary is None:
                raise
        if primary is None:
            owner()
            if accepted is not None:
                require_rendered_occurrences(page, merged)
                require(require_current_success(collector, baseline, PATHS[5]) == accepted)
            elif observed_error:
                collector._require_current_continuation_observation(baseline)
            require(require_current_success(collector, initial_baseline, PATHS[2]) == (response, envelope))
            counts(existing + (1 if (accepted is not None or observed_error is not None) and not preissued else 0))


def retain_capture_records(collector, paths, history, *, observed_error=False):
    """Keep immutable record evidence after the UI leaves a window/account."""
    def require(ok):
        if not ok:
            raise ValueError("capture_publication_rejected")

    native = collector.page
    attachment = collector._attachment
    evidence = []
    for path in paths:
        latest = collector._latest_spa[path]
        observation = (
            collector._continuation_observation if path == PATHS[5] and observed_error else None
        )
        hit = observation[2] if observation is not None else latest[1]
        require(hit is not None)
        observer = collector.observers[path]
        records = [r for r in observer.records.values() if r.get("native_request") is hit._native_request]
        require(len(records) == 1)
        record = records[0]
        evidence.append((path, latest, observation, hit, observer, record, dict(record)))

    def prove_records():
        # Old hits are deliberately evicted on the next request. Their
        # ownership and observer records must still survive publication.
        require(collector._attachment is attachment and collector.page is native)
        for path, latest, observation, hit, observer, record, saved in evidence:
            require(collector.observers.get(path) is observer and not observer.bad)
            require(any(r is record for r in observer.records.values())
                    and record == saved and not record["bad"])

    if history is not None:
        history.append(prove_records)
    return attachment, evidence, prove_records


def collect_twd(crawler, page, collector, login_baseline, *, _publication, _history=None):
    from backend.core.base import BankCollectResult, validate_history_coverage

    rows, coverage = _collect_rows(crawler, page, collector, login_baseline, _publication, _history)
    crawler._esun_spa_phase = "capture_publication"
    for prove in _publication:
        prove()
    if coverage is not None:
        validate_history_coverage(coverage, expected_mode=coverage["mode"],
                                  expected_domains=frozenset({"twd_transactions"}))
    else:
        crawler._esun_spa_phase = "incomplete_result"
    return BankCollectResult(bank="esun", error=None if coverage is not None else "spa_collection_incomplete",
                             card_bill_facts_ok=False, twd_txns=rows, history_coverage=coverage)


def navigation_guard(crawler, page, querying=lambda: False, selecting=lambda: False):

    def require(ok):
        if not ok:
            raise ValueError("SPA probe rejected")

    native = _OriginGuardProxy._unwrap(page)
    original_guard = lambda: crawler._ensure_collect_origin(native)

    def guard():
        try:
            original_guard()
            selectors = {"dialog", '[role="dialog"]', '[aria-modal="true"]'}
            selectors.update((rule.container_selector for rule in crawler.login_checkpoint_rules()))
            dialogs = page.locator(", ".join((selector + ":visible" for selector in sorted(selectors))))
            snapshot = dialogs.evaluate_all(BLOCKER_SNAPSHOT, [querying(), MENU, selecting()])
            require(
                type(snapshot) is list
                and len(snapshot) == 3
                and (type(snapshot[0]) is bool)
                and all((type(v) is str for v in snapshot[1:]))
            )
            allowed, kind, reason = snapshot
            require(
                allowed
                and reason == "none"
                and (kind in ("unknown", "owned_account" if selecting() else "owned_calendar" if querying() else "native_menu"))
                or (
                    not allowed
                    and (
                        reason == "cardinality"
                        and (querying() or selecting())
                        and (kind == "unknown")
                        or (
                            reason == "predicate"
                            and kind
                            in (
                                "unknown",
                                "native_menu",
                                "generic_dialog",
                                "driver_tour",
                                "login_input",
                                "css_modal",
                                "other",
                            )
                        )
                    )
                )
            )
            require(allowed)
            require(
                page.locator(
                    '#layout-content.not-login, .temp-index:visible, input[name="id"]:visible, input[name="pxssword"]:visible'
                ).count()
                == 0
            )
        except Exception:
            raise ValueError("SPA probe rejected") from None

    return guard


def _collect_rows(crawler, page, collector, login_baseline, _publication, _history):
    import os

    history = [] if _history is None else _history

    def prove_history():
        for prove in history:
            prove()

    def require(ok):
        if not ok:
            raise ValueError("SPA probe rejected")

    native = _OriginGuardProxy._unwrap(page)
    crawler._diagnostic_stage = "collect_navigation"
    crawler._esun_spa_phase = "navigation"
    require(collector.page is native)
    querying = selecting = False
    guard = navigation_guard(crawler, page, lambda: querying, lambda: selecting)

    def current_plan():
        try:
            guard()
        except Exception:
            raise ValueError("SPA probe rejected") from None
        return read_plan()

    def read_plan():
        try:
            body, envelope = require_current_success(collector, login_baseline, PATHS[0])
            request = envelope.get("requestBody")
            require(type(request) is dict)
            locale = request.get("locale")
            require(type(locale) is str and locale in ("zh-TW", "en-US"))
            return build_menu_plan(body.get("menuList"), locale)
        except Exception:
            raise ValueError("SPA probe rejected") from None

    guard()
    require(page.locator("#layout-content").count() == 1)
    require(crawler._logged_in(page) is True)
    plan = current_plan()

    def revalidate():
        current = current_plan()
        require(current == plan)
        return current

    baseline = collector.snapshot()
    crawler._esun_spa_phase = "navigation"
    navigate_twd(page, plan, guard, revalidate)
    crawler._diagnostic_stage = "collect_accounts"
    crawler._esun_spa_phase = "prequery_response"
    deadline = monotonic() + TIMEOUT / 1000
    while True:
        revalidate()
        require(monotonic() < deadline)
        try:
            body, envelope = require_current_success(collector, baseline, PATHS[1])
        except ValueError:
            body = envelope = None
        if body is not None and page.evaluate(DESTINATION) is True:
            break
        try:
            page.wait_for_function(
                "()=>new Promise(resolve=>requestAnimationFrame(()=>resolve(true)))",
                timeout=max(1, int((deadline - monotonic()) * 1000)),
            )
        except Exception:
            raise ValueError("SPA probe rejected") from None
    revalidate()
    crawler._esun_spa_phase = "account_validation"
    body, envelope = require_current_success(collector, baseline, PATHS[1])
    from datetime import date
    from .query import query_twd, ROOT, FORM, SAFE

    def validate_account(body, envelope):
        request = envelope.get("requestBody")
        require(type(request) is dict and "account" in request)
        account = request["account"]
        require(
            account is None
            or (
                type(account) is str
                and re.fullmatch("[0-9]{13}", account) is not None
                and (account == body.get("demandDeptAcc"))
            )
        )
        inventory = body.get("twAccountList")
        detail = body.get("queryDeptTxDtlResult")
        currency = body.get("twCurrInfo")
        require(
            type(inventory) is list
            and len(inventory) <= 64
            and all((type(a) is dict for a in inventory))
            and (type(detail) is dict)
            and (type(currency) is dict)
        )
        groups = detail.get("detailListData")
        require(type(groups) is list and len(groups) <= 128)
        row_count = 0
        for group in groups:
            require(
                type(group) is dict
                and type(group.get("detailInfo")) is list
                and all((type(row) is dict for row in group["detailInfo"]))
            )
            row_count += len(group["detailInfo"])
            require(row_count <= 1000)
        accounts = [a.get("accountNo") for a in inventory]
        require(all((type(a) is str and re.fullmatch("[0-9]{13}", a) for a in accounts)))
        require(len(set(accounts)) == len(accounts) and body.get("demandDeptAcc") in accounts)
        require(currency.get("curr") == "TWD")
        require(all(type(a.get("accountAlias")) is str for a in inventory))

    require(page.evaluate(DESTINATION) is True)
    validate_account(body, envelope)
    revalidate()
    require_current_success(collector, baseline, PATHS[1])
    inventory = body["twAccountList"]
    selected_first = [body["demandDeptAcc"]] + [a["accountNo"] for a in inventory if a["accountNo"] != body["demandDeptAcc"]]
    mode = os.environ.get("BANK_CRAWLER_HISTORY_MODE", "full")
    require(mode in ("full", "incremental"))
    expected_windows, covered_windows, finished_accounts = [], [], []

    def collect_account(body, baseline, remaining_accounts):
        nonlocal querying
        selected = body["demandDeptAcc"]
        alias = body.get("accountAlias")
        inventory = [a for a in body["twAccountList"] if a["accountNo"] == selected]
        require(type(alias) is str and len(inventory) == 1 and (inventory[0].get("accountAlias") == alias))
        import calendar

        crawler._esun_spa_phase = "transaction_preflight"
        today = date(*page.evaluate("() => {const d=new Date();return [d.getFullYear(),d.getMonth()+1,d.getDate()]}"))
        floor = date(today.year - 3, today.month, min(today.day, calendar.monthrange(today.year - 3, today.month)[1]))
        cursor = crawler.transaction_start_for(selected, domain="twd_transactions")
        require(cursor is None or (type(cursor) is date and cursor <= today))
        start = crawler.transaction_window_start(selected, floor=floor, domain="twd_transactions")
        require(type(start) is date and floor <= start <= today)
        expected_windows.append({"identity": selected, "start": start.isoformat(), "end": today.isoformat()})
        year, month = divmod(start.year * 12 + start.month - 1 + 6, 12)
        end = min(today, date(year, month + 1, min(start.day, calendar.monthrange(year, month + 1)[1])))
        window = (start, end)
        require(type(window) is tuple and len(window) == 2 and all((type(d) is date for d in window)))
        display = selected + " " + alias
        rows_all = []

        def require_query_capacity():
            if collector.query_issued_count() >= collector.MAX_QUERY_REQUESTS:
                raise ValueError("query_budget_exhausted")

        def query_revalidate():
            revalidate()
            current, _ = require_current_success(collector, baseline, PATHS[1])
            require(current == body and page.evaluate(DESTINATION) is True)
            field = page.locator(
                ROOT
                + ' .combo-input-wrapper[name="accountList"][role="button"] > input.combo-input[name="accountList"][readonly]'
            )
            require(field.count() == 1 and field.is_visible() and (field.input_value() == display))
            return display

        while True:
            if collector.query_issued_count() >= collector.MAX_QUERY_REQUESTS:
                return rows_all, False
            crawler._diagnostic_stage = "collect_transactions"
            query_revalidate()
            previous_timeline = page.evaluate(TIMELINE)
            query_baseline = collector.snapshot()

            def require_unchanged_query_issuance():
                require_query_capacity()
                require(all(collector.issued_count(path) == query_baseline['counts'][path] for path in PATHS))

            require_unchanged_query_issuance()
            querying = True
            crawler._esun_spa_phase = "transaction_form"
            try:
                action_budget = [0]
                query_twd(
                    page,
                    *window,
                    plan["locale"],
                    display,
                    guard,
                    query_revalidate,
                    action_budget=action_budget,
                    before_submit=require_unchanged_query_issuance,
                    before_action=require_unchanged_query_issuance,
                )
            finally:
                querying = False
            crawler._esun_spa_phase = "transaction_response"
            deadline = monotonic() + TIMEOUT / 1000
            while True:
                query_revalidate()
                require(monotonic() < deadline)
                try:
                    response, envelope = require_current_success(collector, query_baseline, PATHS[2])
                    break
                except ValueError:
                    page.wait_for_timeout(20)
            crawler._esun_spa_phase = "transaction_result"
            request = envelope.get("requestBody")
            require(type(request) is dict)
            require(set(request) == {"account", "startDate", "endDate", "startIndex", "count", "customerInputHashtag"})
            expected = page.evaluate(
                "ds=>ds.map(d=>new Date(d[0],d[1]-1,d[2]).toISOString())", [[d.year, d.month, d.day] for d in window]
            )
            require(request["account"] == selected)
            require(request["startDate"] == expected[0])
            require(request["endDate"] == expected[1])
            require(type(request["startIndex"]) is int)
            require(request["startIndex"] == 1)
            require(type(request["count"]) is int)
            require(request["count"] == 100)
            require(request["customerInputHashtag"] == [])
            empty_state = empty_window_state(response)
            if empty_state in ("empty_shape_unknown", "empty_contradiction"):
                raise ValueError("ambiguous empty result")
            empty = empty_state == "explicit_empty_candidate"
            if not empty:
                validate_query_response(response)
            while True:
                query_revalidate()
                require_current_success(collector, query_baseline, PATHS[2])
                crawler._esun_spa_phase = "transaction_result"
                rendered = page.evaluate(TIMELINE)
                if empty:
                    card = page.evaluate(EMPTY_CARD)
                    if normalized(card) == "(2003)" + EMPTY_MESSAGE:
                        require(
                            all(
                                (
                                    page.locator(ROOT + " " + FORM + " input[name=" + name + "][readonly]").input_value()
                                    == wanted.strftime("%Y/%m/%d")
                                    for name, wanted in zip(("startDate", "endDate"), window)
                                )
                            )
                        )
                        break
                elif rendered is not None and rendered != previous_timeline:
                    break
                require(monotonic() < deadline)
                page.wait_for_timeout(20)
            query_revalidate()
            require_current_success(collector, query_baseline, PATHS[2])
            if not empty:
                from .rows import normalize_row

                groups = response["queryDeptTxDtlResult"]["detailListData"]
                require_rendered_occurrences(page, groups)
                query_revalidate()
                require_current_success(collector, query_baseline, PATHS[2])
                crawler._esun_spa_phase = "continuation"
                previous = prior_baseline = None
                while True:
                    continuation_baseline = collector.snapshot() if previous is not None else query_baseline
                    groups, continuation_reason = continue_twd_once(
                        crawler, page, collector, query_baseline, response, envelope, query_revalidate,
                        window, action_budget, _publication=_publication, previous=previous,
                        accumulated=groups if previous is not None else None, prior_baseline=prior_baseline,
                        _history=history,
                    )
                    if continuation_reason != "pagination_unverified":
                        break
                    previous = require_current_success(collector, continuation_baseline, PATHS[5])
                    prior_baseline = continuation_baseline
                    last_page, issued = previous
                    if (last_page["count"] == issued["requestBody"]["count"]
                            and sum(len(g["detailInfo"]) for g in last_page["detailListData"]) < last_page["count"]):
                        continuation_reason = "short_page"
                        break  # Classify the received page before denying another request.
                    # ponytail: reserve six initial windows per unseen account.
                    if collector.query_issued_count() >= collector.MAX_QUERY_REQUESTS - 6 * remaining_accounts:
                        break
                    for prove in _publication:
                        prove()
                    _publication.clear()
                crawler._esun_spa_phase = "transaction_normalization"
                rows = []
                for group in groups:
                    for row in group["detailInfo"]:
                        rows.append(normalize_row(row, account_no=selected, currency="TWD", start=window[0], end=window[1]))
                rows_all.extend(rows)
                if continuation_reason == "short_page" and response["queryDeptTxDtlResult"]["startIndex"] == request["startIndex"]:
                    covered_windows.append({"identity": selected, "start": window[0].isoformat(),
                                            "end": window[1].isoformat(), "status": "complete",
                                            "pages": 1 + collector.issued_count(PATHS[5]) - query_baseline["counts"][PATHS[5]]})
                    following = next_window(window[1], today)
                    if following is not None:
                        for prove in _publication:
                            prove()
                        _publication.clear()
                        window = following
                        continue
                    finished_accounts.append(selected)
                # Pagination remains unverified, but an owned rendered success
                # may be left partial while inventory collection proceeds.
                return rows_all, continuation_reason in (
                    "short_page", "pagination_unverified", "cursor_unverified", "native_action_budget_exhausted")
            # Only an owned exact 2003 response and visible matching card advance dates.
            require(
                all(collector.issued_count(path) == query_baseline["counts"][path] for path in PATHS if path != PATHS[2])
            )
            _, _, prove_records = retain_capture_records(collector, PATHS[:3], history)

            def prove_empty(snapshot=query_baseline, saved=response, dates=window, records=prove_records):
                query_revalidate()
                require(require_current_success(collector, snapshot, PATHS[2])[0] == saved)
                require(normalized(page.evaluate(EMPTY_CARD)) == "(2003)" + EMPTY_MESSAGE)
                require(all(page.locator(ROOT + " " + FORM + " input[name=" + name + "][readonly]").input_value()
                            == day.strftime("%Y/%m/%d")
                            for name, day in zip(("startDate", "endDate"), dates)))
                require(all(collector.issued_count(p) == snapshot["counts"][p] + (p == PATHS[2]) for p in PATHS))
                records()
                prove_history()

            _publication.append(prove_empty)
            prove_empty()
            covered_windows.append({"identity": selected, "start": window[0].isoformat(),
                                    "end": window[1].isoformat(), "status": "explicit_empty", "pages": 1})
            following = next_window(window[1], today)
            if following is None:
                finished_accounts.append(selected)
                return rows_all, True
            _publication.clear()  # Current empty DOM proven; historical records retained.
            window = following


    def switch_account(target):
        # Public 9413/2402: native combo -> inventory li -> preQuery(accountNo).
        nonlocal selecting
        crawler._diagnostic_stage = "collect_accounts"
        crawler._esun_spa_phase = "account_validation"
        revalidate()
        before = collector.snapshot()
        field = page.locator(ROOT + ' .combo-input-wrapper[name="accountList"][role="button"]')
        dialog = page.locator(ROOT + ' .combo-block:has(> .combo-input-wrapper[name="accountList"][role="button"]) > .clean-error-scope[name="accountList"] > dialog.combo-modal[open][role="dialog"][aria-modal="true"]')
        labels = [a["accountNo"] + " " + a["accountAlias"] for a in inventory]

        def unchanged():
            require(collector.query_issued_count() < collector.MAX_QUERY_REQUESTS and collector.issued_count(PATHS[1]) < collector.MAX_QUERY_REQUESTS)
            require(all(collector.issued_count(p) == before["counts"][p] for p in PATHS))

        def owner():
            revalidate()
            require(page.evaluate(DESTINATION) is True)
            require(require_current_success(collector, baseline, PATHS[1])[0] == body)
            require(field.count() == 1 and field.is_visible() and field.evaluate(SAFE))
            require(field.locator('input.combo-input[name="accountList"][readonly]').input_value()
                    == body["demandDeptAcc"] + " " + body["accountAlias"])
            for prove in _publication:
                prove()
            unchanged()

        def click(target):
            require(target.count() == 1 and target.is_visible() and target.is_enabled() and target.evaluate(SAFE))
            action_guard = target._guard if isinstance(target, _OriginGuardProxy) else None
            target = _OriginGuardProxy._unwrap(target)
            if action_guard is not None:
                action_guard()
            owner()
            target.click(timeout=TIMEOUT)

        owner()
        selecting = True
        try:
            click(field)
            deadline = monotonic() + TIMEOUT / 1000
            while not dialog.count():
                owner()
                require(monotonic() < deadline)
                page.wait_for_timeout(20)
            owner()
            require(dialog.count() == 1 and dialog.is_visible())
            items = dialog.locator(':scope > .custom-area > ul.info-scrollable > li[role="button"]')
            require(items.all_text_contents() == labels)
            require(items.evaluate_all('nodes=>nodes.every(n=>(' + SAFE + ')(n))'))
            click(items.nth([a["accountNo"] for a in inventory].index(target)))
            _publication.clear()  # Prior page proven before the native account transition.
            while True:
                revalidate()
                require(monotonic() < deadline)
                require(collector.query_issued_count() < collector.MAX_QUERY_REQUESTS)
                require(all(collector.issued_count(p) == before["counts"][p] + (p == PATHS[1]) for p in PATHS))
                try:
                    current, issued = require_current_success(collector, before, PATHS[1])
                except ValueError:
                    page.wait_for_timeout(20)
                    continue
                validate_account(current, issued)
                require(issued["requestBody"] == {"account": target} and current["demandDeptAcc"] == target)
                require(current["twAccountList"] == inventory and page.evaluate(DESTINATION) is True)
                expected = target + " " + current["accountAlias"]
                if not dialog.count() and field.locator('input[readonly]').input_value() == expected:
                    return current, before
                page.wait_for_timeout(20)
        finally:
            selecting = False

    rows_all = []
    for index, selected in enumerate(selected_first):
        if index:
            body, baseline = switch_account(selected)
        rows, may_switch = collect_account(body, baseline, len(selected_first) - index - 1)
        rows_all.extend(rows)
        if not may_switch or collector.query_issued_count() >= collector.MAX_QUERY_REQUESTS or collector.issued_count(PATHS[1]) >= collector.MAX_QUERY_REQUESTS:
            break
    _publication.append(prove_history)
    require(os.environ.get("BANK_CRAWLER_HISTORY_MODE", "full") == mode)
    coverage = ({"mode": mode, "domains": [{"domain": "twd_transactions",
                 "expected": expected_windows, "windows": covered_windows}]}
                if len(finished_accounts) == len(selected_first) and len(expected_windows) == len(selected_first)
                else None)
    return rows_all, coverage
