"""Owned native TWD query and bounded source-checked window progression."""

from backend.core.error_codes import exception_error_id, error_step, with_error_code
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


DESTINATION_REASON = r"""() => {
 const visible = n => {
   for(let p=n;p;p=p.parentElement) {
     if(p.hidden || p.inert || p.getAttribute('aria-hidden')==='true' || p.getAttribute('aria-disabled')==='true') return false;
   }
   return n.getClientRects().length>0 && getComputedStyle(n).visibility==='visible';
 };
 const layouts=[...document.querySelectorAll('#layout-content')];
 const roots=[...document.querySelectorAll('#layout-content .ctw01002')];
 const tabs=[...document.querySelectorAll('#tw')];
 if(layouts.length!==1) return [false, 1];
 if(layouts[0].classList.contains('not-login')) return [false, 2];
 if(!visible(layouts[0])) return [false, 3];
 if(roots.length!==1) return [false, 4];
 if(!visible(roots[0])) return [false, 5];
 if(tabs.length!==1) return [false, 6];
 if(!visible(tabs[0])) return [false, 7];
 const body=roots[0].closest('.tab-body'), header=tabs[0].parentElement;
 const container=body && body.parentElement;
 if(!(!!container)) return [false, 8];
 if(!(container.matches('.mib-tab'))) return [false, 9];
 if(!(layouts[0].contains(container))) return [false, 10];
 if(!(roots[0].closest('.mib-tab')===container)) return [false, 11];
 if(!(header.matches('.tab-header[role="tablist"]'))) return [false, 12];
 if(!(header.parentElement===container)) return [false, 13];
 if(!(container.querySelectorAll(':scope > .tab-header').length===1)) return [false, 14];
 if(!(container.querySelectorAll(':scope > .tab-body').length===1)) return [false, 15];
 if(!(tabs[0].getAttribute('role')==='tab')) return [false, 16];
 if(!(tabs[0].getAttribute('aria-selected')==='true')) return [false, 17];
 return [true, 0];
}"""
TIMELINE_REASON = r"""() => {
 const nodes=[...document.querySelectorAll('#layout-content .ctw01002 .timeline-query-continer')];
 if(nodes.length!==1) return [null, 1];
 if(!nodes[0].getClientRects().length) return [null, 2];
 if(getComputedStyle(nodes[0]).visibility!=='visible') return [null, 3];
 return [nodes[0].innerText, 0];
}"""
DESTINATION_CODES = {
    'prequery': ('ESUN-05-865-001', 'ESUN-05-865-002', 'ESUN-05-865-003', 'ESUN-05-865-004', 'ESUN-05-865-005', 'ESUN-05-865-006', 'ESUN-05-865-007', 'ESUN-05-865-008', 'ESUN-05-865-009', 'ESUN-05-865-010', 'ESUN-05-865-011', 'ESUN-05-865-012', 'ESUN-05-865-013', 'ESUN-05-865-014', 'ESUN-05-865-015', 'ESUN-05-865-016', 'ESUN-05-865-017'),
    'initial': ('ESUN-05-865-101', 'ESUN-05-865-102', 'ESUN-05-865-103', 'ESUN-05-865-104', 'ESUN-05-865-105', 'ESUN-05-865-106', 'ESUN-05-865-107', 'ESUN-05-865-108', 'ESUN-05-865-109', 'ESUN-05-865-110', 'ESUN-05-865-111', 'ESUN-05-865-112', 'ESUN-05-865-113', 'ESUN-05-865-114', 'ESUN-05-865-115', 'ESUN-05-865-116', 'ESUN-05-865-117'),
    'query': ('ESUN-05-865-201', 'ESUN-05-865-202', 'ESUN-05-865-203', 'ESUN-05-865-204', 'ESUN-05-865-205', 'ESUN-05-865-206', 'ESUN-05-865-207', 'ESUN-05-865-208', 'ESUN-05-865-209', 'ESUN-05-865-210', 'ESUN-05-865-211', 'ESUN-05-865-212', 'ESUN-05-865-213', 'ESUN-05-865-214', 'ESUN-05-865-215', 'ESUN-05-865-216', 'ESUN-05-865-217'),
    'switch_owner': ('ESUN-05-865-301', 'ESUN-05-865-302', 'ESUN-05-865-303', 'ESUN-05-865-304', 'ESUN-05-865-305', 'ESUN-05-865-306', 'ESUN-05-865-307', 'ESUN-05-865-308', 'ESUN-05-865-309', 'ESUN-05-865-310', 'ESUN-05-865-311', 'ESUN-05-865-312', 'ESUN-05-865-313', 'ESUN-05-865-314', 'ESUN-05-865-315', 'ESUN-05-865-316', 'ESUN-05-865-317'),
    'switch_result': ('ESUN-05-865-401', 'ESUN-05-865-402', 'ESUN-05-865-403', 'ESUN-05-865-404', 'ESUN-05-865-405', 'ESUN-05-865-406', 'ESUN-05-865-407', 'ESUN-05-865-408', 'ESUN-05-865-409', 'ESUN-05-865-410', 'ESUN-05-865-411', 'ESUN-05-865-412', 'ESUN-05-865-413', 'ESUN-05-865-414', 'ESUN-05-865-415', 'ESUN-05-865-416', 'ESUN-05-865-417'),
}
TIMELINE_CODES = ('ESUN-05-866-001', 'ESUN-05-866-002', 'ESUN-05-866-003')


def poll_observation(result, codes):
    if type(result) is list and len(result) == 2:
        value, reason = result
        if type(reason) is int and 1 <= reason <= len(codes):
            return value, codes[reason - 1]
        return value, None
    return result, None


def validate_query_response(response):
    """Frozen conservative shape, not proof of actual bank field types."""
    detail = response.get("queryDeptTxDtlResult") if type(response) is dict else None
    if type(detail) is not dict:
        raise with_error_code(ValueError('SPA probe rejected'), 'ESUN-05-545-001')
    if not ("displayErrorCode" in detail and detail["displayErrorCode"] is None):
        raise with_error_code(ValueError('SPA probe rejected'), 'ESUN-05-546-001')
    if not ("displayErrorMsg" in detail and detail["displayErrorMsg"] is None):
        raise with_error_code(ValueError('SPA probe rejected'), 'ESUN-05-547-001')
    if type(detail.get("startIndex")) is not int:
        raise with_error_code(ValueError('SPA probe rejected'), 'ESUN-05-548-001')
    if type(detail.get("count")) is not int:
        raise with_error_code(ValueError('SPA probe rejected'), 'ESUN-05-549-001')
    if type(detail.get("detailListData")) is not list:
        raise with_error_code(ValueError('SPA probe rejected'), 'ESUN-05-550-001')
    if type(response.get("recentHashtag")) is not list:
        raise with_error_code(ValueError('SPA probe rejected'), 'ESUN-05-551-001')
    for group in detail["detailListData"]:
        if type(group) is not dict:
            raise with_error_code(ValueError('SPA probe rejected'), 'ESUN-05-554-001')
        if type(group.get("year")) is not str:
            raise with_error_code(ValueError('SPA probe rejected'), 'ESUN-05-555-001')
        if type(group.get("month")) is not str:
            raise with_error_code(ValueError('SPA probe rejected'), 'ESUN-05-556-001')
        if type(group.get("detailInfo")) is not list:
            raise with_error_code(ValueError('SPA probe rejected'), 'ESUN-05-557-001')
        for row in group["detailInfo"]:
            if type(row) is not dict:
                raise with_error_code(ValueError('SPA probe rejected'), 'ESUN-05-560-001')
            if type(row.get("debitCredit")) is not str:
                raise with_error_code(ValueError('SPA probe rejected'), 'ESUN-05-561-001')


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
    with error_step('ESUN-05-647-001'):
        rendered = (
            error_step('ESUN-05-647-100')(lambda: page.evaluate(RENDERED_OCCURRENCES, error_message))()
            if document is None
            else error_step('ESUN-05-647-101')(lambda: document.evaluate(
                '(d, errorMessage) => d.doc===document && d.node===document.querySelector(".timeline-query-continer") ? ('
                + RENDERED_OCCURRENCES
                + ")(errorMessage) : null", error_message
            ))()
        )
    if type(rendered) is not list:
        raise with_error_code(ValueError('SPA probe rejected'), 'ESUN-05-552-001')
    if len(rendered) != len(groups):
        raise with_error_code(ValueError('SPA probe rejected'), 'ESUN-05-552-002')
    if any(
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
        ):
        raise with_error_code(ValueError('SPA probe rejected'), 'ESUN-05-552-003')


def bind_continuation(previous, request, body, issued):
    """9413 response-derived cursor; 7108 ordered occurrence-preserving merge."""
    from copy import deepcopy

    def require(ok, *, error_id):
        if not ok:
            raise with_error_code(ValueError("continuation_rejected"), error_id)

    keys = {"account", "startDate", "endDate", "startIndex", "count", "customerInputHashtag"}
    require(type(request) is dict, error_id='ESUN-05-434-001')
    require(set(request) == keys, error_id='ESUN-05-434-002')
    require(type(issued) is dict, error_id='ESUN-05-434-003')
    require(set(issued) == keys, error_id='ESUN-05-434-004')
    require(type(previous) is dict, error_id='ESUN-05-435-001')
    require(type(body) is dict, error_id='ESUN-05-435-002')
    for value in (previous.get("startIndex"), previous.get("count"), body.get("startIndex"), body.get("count")):
        require(type(value) is int, error_id='ESUN-05-450-001')
        require(0 < value <= 9007199254740991, error_id='ESUN-05-450-002')
    cursor = previous["startIndex"] + previous["count"]
    require(cursor <= 9007199254740991, error_id='ESUN-05-436-001')
    require(body["startIndex"] == cursor, error_id='ESUN-05-436-002')
    require(type(issued["startIndex"]) is int, error_id='ESUN-05-437-001')
    require(type(issued["count"]) is int, error_id='ESUN-05-437-002')
    require(issued == dict(request, startIndex=cursor, count=previous["count"]), error_id='ESUN-05-438-001')
    require(body.get("displayErrorCode") is None, error_id='ESUN-05-439-001')
    require(body.get("displayErrorMsg") is None, error_id='ESUN-05-439-002')
    groups = deepcopy(previous["detailListData"])
    validate_query_response(
        {"queryDeptTxDtlResult": dict(body, displayErrorCode=None, displayErrorMsg=None), "recentHashtag": []}
    )
    for collection in (groups, body["detailListData"]):
        pairs = [(g["year"], g["month"]) for g in collection]
        require(len(pairs) == len(set(pairs)), error_id='ESUN-05-451-001')
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
        if collector.issued_count(PATHS[5]) != baseline["counts"][PATHS[5]] + 1:
            raise with_error_code(ValueError(), 'ESUN-05-558-001')
        if collector.query_issued_count() > collector.MAX_QUERY_REQUESTS:
            raise with_error_code(ValueError(), 'ESUN-05-558-002')
        if require_current_success(collector, baseline, PATHS[2]) != (response, envelope):
            raise with_error_code(ValueError(), 'ESUN-05-558-003')
        if collector._latest_spa[PATHS[5]][0] <= collector._latest_spa[PATHS[2]][0]:
            raise with_error_code(ValueError(), 'ESUN-05-558-004')
        body, issued = require_current_success(collector, baseline, PATHS[5])
        merged = bind_continuation(response["queryDeptTxDtlResult"], envelope["requestBody"], body, issued["requestBody"])
        return body, issued, merged
    except (KeyError, TypeError, ValueError) as source_error:
        raise with_error_code(ValueError("continuation_rejected"), exception_error_id(source_error, bank="esun") or 'ESUN-05-564-001') from None


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
        raise with_error_code(ValueError('continuation_rejected'), 'ESUN-05-553-001')
    groups = accumulated if previous is not None else response["queryDeptTxDtlResult"]["detailListData"]
    detail = previous[0] if previous is not None else response["queryDeptTxDtlResult"]
    source_request = previous[1]["requestBody"] if previous is not None else envelope["requestBody"]

    def require(ok, *, error_id):
        if not ok:
            raise with_error_code(ValueError("continuation_rejected"), error_id)

    def reserve():
        if type(action_budget) is not list:
            raise with_error_code(ValueError('native_action_budget_exhausted'), 'ESUN-05-559-001')
        if len(action_budget) != 1:
            raise with_error_code(ValueError('native_action_budget_exhausted'), 'ESUN-05-559-002')
        if type(action_budget[0]) is not int:
            raise with_error_code(ValueError('native_action_budget_exhausted'), 'ESUN-05-559-003')
        if not 0 <= action_budget[0] < 32:
            raise with_error_code(ValueError('native_action_budget_exhausted'), 'ESUN-05-559-004')
        action_budget[0] += 1

    def owner():
        revalidate()
        with error_step('ESUN-05-648-001'):
            for field_index, (name, day) in enumerate(zip(("startDate", "endDate"), window)):
                require(
                    error_step(('ESUN-05-648-101', 'ESUN-05-864-004')[field_index])(lambda: error_step(('ESUN-05-648-100', 'ESUN-05-864-003')[field_index])(lambda: page.locator(ROOT + " " + FORM + " input[name=" + name + "][readonly]"))().input_value())()
                    == day.strftime("%Y/%m/%d"),
                    error_id=('ESUN-05-864-001', 'ESUN-05-864-002')[field_index])

    def counts(extra=0):
        require(all(
                (
                    collector.issued_count(path)
                    == initial_baseline["counts"][path] + (1 if path == PATHS[2] else extra if path == PATHS[5] else 0)
                    for path in PATHS
                )
            ), error_id='ESUN-05-453-001')
        require(collector.query_issued_count() <= collector.MAX_QUERY_REQUESTS, error_id='ESUN-05-454-001')

    existing = collector.issued_count(PATHS[5]) - initial_baseline["counts"][PATHS[5]]
    require(1 <= existing <= collector.MAX_QUERY_REQUESTS if previous is not None else existing in (0, 1), error_id='ESUN-05-440-001')
    if previous is not None:
        require(require_current_success(collector, prior_baseline, PATHS[5]) == previous, error_id='ESUN-05-455-001')
    preissued = existing if previous is None else 0
    counts(existing)
    owner()
    counts(existing)
    require(any((g["detailInfo"] for g in groups)), error_id='ESUN-05-441-001')
    require(all((type(detail.get(k)) is int and 0 < detail[k] <= 9007199254740991 for k in ("startIndex", "count"))), error_id='ESUN-05-442-001')
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
    with error_step('ESUN-05-649-001'):
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
                prior_phase = crawler._esun_spa_phase
                crawler._esun_spa_phase = "capture_publication"
                crawler._esun_spa_gate = None
                owner()
                require(require_current_success(collector, initial_baseline, PATHS[2]) == (response, envelope), error_id='ESUN-05-516-001')
                if accepted is not None:
                    require(require_current_success(collector, baseline, PATHS[5]) == accepted, error_id='ESUN-05-532-001')
                elif observed_error is not None:
                    require(collector._require_current_continuation_observation(baseline) == observed_error, error_id='ESUN-05-538-001')
                elif previous is not None:
                    require(require_current_success(collector, prior_baseline, PATHS[5]) == previous, error_id='ESUN-05-544-001')
                require_rendered_occurrences(page, values, document,
                    error_message=observed_error[0].get('resultDescription') if observed_error is not None else None)
                require(collector._attachment is attachment, error_id='ESUN-05-517-001')
                require(collector.page is native, error_id='ESUN-05-517-002')
                for path, latest, observation, hit, observer, record, saved in evidence:
                    now = collector._latest_spa.get(path)
                    require(now is not None, error_id='ESUN-05-533-001')
                    require(now[0] == latest[0], error_id='ESUN-05-533-002')
                    require(now[1] is latest[1], error_id='ESUN-05-533-003')
                    require(collector.observers.get(path) is observer, error_id='ESUN-05-534-001')
                    require(not observer.bad, error_id='ESUN-05-534-002')
                    require(any((r is record for r in observer.records.values())), error_id='ESUN-05-535-001')
                    require(record == saved, error_id='ESUN-05-535-002')
                    require(not record["bad"], error_id='ESUN-05-535-003')
                    if observation is not None:
                        require(collector._continuation_observation is observation, error_id='ESUN-05-539-001')
                    else:
                        require(any((h is hit for h in collector.hits)), error_id='ESUN-05-540-001')
                counts(existing + (1 if (accepted is not None or observed_error is not None) and not preissued else 0))
                for proof in _history if _history is not None else (prove_records,):
                    proof()  # In-memory sweep after every event-pumping call.
                crawler._esun_spa_phase = prior_phase

            _publication.append(prove)
        return (values, reason)

    crawler._esun_spa_phase = "continuation_geometry"
    request_body = source_request
    page_count = detail.get("count")
    row_count = sum(len(group.get("detailInfo", ())) for group in detail["detailListData"] if type(group) is dict)
    require(type(request_body) is dict, error_id='ESUN-05-443-001')
    require(type(request_body.get("count")) is int, error_id='ESUN-05-443-002')
    require(type(page_count) is int, error_id='ESUN-05-443-003')
    require(0 < page_count <= 9007199254740991, error_id='ESUN-05-443-004')
    require(row_count <= page_count, error_id='ESUN-05-443-005')
    # Product policy: an owned successful page below the matching requested
    # capacity completes the window after cursor/render/publication checks.
    # This is not a bank-issued terminal receipt; never issue a speculative next page.
    if page_count == request_body["count"] and row_count < page_count:
        require(not preissued, error_id='ESUN-05-456-001')
        return finish(groups, "short_page")
    # A bank-adjusted page size does not prove that the next cursor is safe.
    if page_count != request_body["count"]:
        return finish(groups, "cursor_unverified")
    with error_step('ESUN-05-650-001'):
        geometry = native.evaluate(geometry_script)

    def current():
        owner()
        with error_step('ESUN-05-651-001'):
            require(document.evaluate(
                    "d=>d.doc===document && d.node===document.querySelector('.timeline-query-continer') && document.querySelectorAll('.timeline-query-continer').length===1"
                ), error_id='ESUN-05-457-001')
        require(require_current_success(collector, initial_baseline, PATHS[2]) == (response, envelope), error_id='ESUN-05-458-001')

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
                    require(monotonic() < deadline, error_id='ESUN-05-541-001')
                    with error_step('ESUN-05-652-001'):
                        page.wait_for_timeout(20)
            accepted = (body, issued_envelope)
            return finish(merged, "pagination_unverified")
        ready_deadline = monotonic() + TIMEOUT / 1000
        while geometry is None and monotonic() < ready_deadline:
            with error_step('ESUN-05-653-001'):
                page.wait_for_timeout(20)
            current()
            counts(existing)
            with error_step('ESUN-05-654-001'):
                geometry = document.evaluate(geometry_script)
        if geometry is None:
            return finish(groups, "native_scroll_unproved")
        if collector.query_issued_count() >= collector.MAX_QUERY_REQUESTS:
            return finish(groups, "query_budget_exhausted")
        require(type(action_budget) is list, error_id='ESUN-05-459-001')
        require(len(action_budget) == 1, error_id='ESUN-05-459-002')
        require(type(action_budget[0]) is int, error_id='ESUN-05-459-003')
        require(0 <= action_budget[0] <= 32, error_id='ESUN-05-459-004')
        if action_budget[0] > 30:
            return finish(groups, "native_action_budget_exhausted")
        with error_step('ESUN-05-655-001'):
            require(document.evaluate(geometry_script) == geometry, error_id='ESUN-05-460-001')
        counts(existing)
        reserve()
        crawler._esun_spa_phase = "continuation_action"
        with error_step('ESUN-05-775-001'):
            native.mouse.move(*geometry[:2])
        current()
        counts(existing)
        if collector.query_issued_count() >= collector.MAX_QUERY_REQUESTS:
            return finish(groups, "query_budget_exhausted")
        with error_step('ESUN-05-656-001'):
            require(document.evaluate(geometry_script) == geometry, error_id='ESUN-05-461-001')
        counts(existing)
        reserve()
        with error_step('ESUN-05-776-001'):
            native.mouse.wheel(0, geometry[2])
        crawler._esun_spa_phase = "continuation_wait"
        deadline = monotonic() + TIMEOUT / 1000
        while True:
            crawler._esun_spa_phase = "continuation_wait"
            current()
            require(collector.issued_count(PATHS[5]) <= baseline["counts"][PATHS[5]] + 1, error_id='ESUN-05-481-001')
            require(collector.query_issued_count() <= collector.MAX_QUERY_REQUESTS, error_id='ESUN-05-482-001')
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
                    require(type(issued) is dict, error_id='ESUN-05-542-001')
                    require(issued == wanted, error_id='ESUN-05-542-002')
                    require(type(issued.get("startIndex")) is int, error_id='ESUN-05-542-003')
                    require(type(issued.get("count")) is int, error_id='ESUN-05-542-004')
                    current()
                    collector._require_current_continuation_observation(baseline)
                    counts(existing + 1)
                    observed_error = (failed_body, failed_envelope)
                    return finish(groups, "continuation_error_observed")
                require(monotonic() < deadline, error_id='ESUN-05-536-001')
                with error_step('ESUN-05-657-001'):
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
                require(monotonic() < deadline, error_id='ESUN-05-537-001')
                with error_step('ESUN-05-658-001'):
                    page.wait_for_timeout(20)
                continue
            current()
            require(require_current_success(collector, baseline, PATHS[5]) == (body, issued_envelope), error_id='ESUN-05-483-001')
            counts(existing + 1)
            accepted = (body, issued_envelope)
            return finish(merged, "pagination_unverified")
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            if _publication is None or primary is not None:
                with error_step('ESUN-05-659-001'):
                    document.dispose()
        except BaseException:
            if primary is None:
                raise
        if primary is None:
            owner()
            if accepted is not None:
                require_rendered_occurrences(page, merged)
                require(require_current_success(collector, baseline, PATHS[5]) == accepted, error_id='ESUN-05-518-001')
            elif observed_error:
                collector._require_current_continuation_observation(baseline)
            require(require_current_success(collector, initial_baseline, PATHS[2]) == (response, envelope), error_id='ESUN-05-484-001')
            counts(existing + (1 if (accepted is not None or observed_error is not None) and not preissued else 0))


def retain_capture_records(collector, paths, history, *, observed_error=False):
    """Keep immutable record evidence after the UI leaves a window/account."""
    def require(ok, *, error_id):
        if not ok:
            raise with_error_code(ValueError("capture_publication_rejected"), error_id)

    native = collector.page
    attachment = collector._attachment
    evidence = []
    for path in paths:
        latest = collector._latest_spa[path]
        observation = (
            collector._continuation_observation if path == PATHS[5] and observed_error else None
        )
        hit = observation[2] if observation is not None else latest[1]
        require(hit is not None, error_id='ESUN-05-462-001')
        observer = collector.observers[path]
        records = [r for r in observer.records.values() if r.get("native_request") is hit._native_request]
        require(len(records) == 1, error_id='ESUN-05-463-001')
        record = records[0]
        evidence.append((path, latest, observation, hit, observer, record, dict(record)))

    def prove_records():
        # Old hits are deliberately evicted on the next request. Their
        # ownership and observer records must still survive publication.
        require(collector._attachment is attachment, error_id='ESUN-05-464-001')
        require(collector.page is native, error_id='ESUN-05-464-002')
        for path, latest, observation, hit, observer, record, saved in evidence:
            require(collector.observers.get(path) is observer, error_id='ESUN-05-485-001')
            require(not observer.bad, error_id='ESUN-05-485-002')
            require(any(r is record for r in observer.records.values()), error_id='ESUN-05-486-001')
            require(record == saved, error_id='ESUN-05-486-002')
            require(not record["bad"], error_id='ESUN-05-486-003')

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

    def require(ok, *, error_id):
        if not ok:
            raise with_error_code(ValueError("SPA probe rejected"), error_id)

    native = _OriginGuardProxy._unwrap(page)
    original_guard = lambda: crawler._ensure_collect_origin(native)

    def guard():
        gate = 'origin'
        try:
            original_guard()
            gate = 'blocker_snapshot'
            selectors = {"dialog", '[role="dialog"]', '[aria-modal="true"]'}
            selectors.update((rule.container_selector for rule in crawler.login_checkpoint_rules()))
            with error_step('ESUN-05-660-001'):
                dialogs = page.locator(", ".join((selector + ":visible" for selector in sorted(selectors))))
            with error_step('ESUN-05-661-001'):
                snapshot = dialogs.evaluate_all(BLOCKER_SNAPSHOT, [querying(), MENU, selecting()])
            gate = 'blocker_contract'
            require(type(snapshot) is list, error_id='ESUN-05-487-001')
            require(len(snapshot) == 3, error_id='ESUN-05-487-002')
            require(type(snapshot[0]) is bool, error_id='ESUN-05-487-003')
            require(all((type(v) is str for v in snapshot[1:])), error_id='ESUN-05-487-004')
            allowed, kind, reason = snapshot
            gate = 'blocker_kind'
            require(allowed
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
                ), error_id='ESUN-05-488-001')
            gate = 'blocker_allowed'
            require(allowed, error_id='ESUN-05-489-001')
            gate = 'login_visible'
            with error_step('ESUN-05-662-001'):
                require(error_step('ESUN-05-662-101')(lambda: error_step('ESUN-05-662-100')(lambda: page.locator(
                        '#layout-content.not-login, .temp-index:visible, input[name="id"]:visible, input[name="pxssword"]:visible'
                    ))().count())()
                    == 0, error_id='ESUN-05-490-001')
        except Exception:
            crawler._esun_spa_gate = gate
            raise
        crawler._esun_spa_gate = None

    return guard


def _collect_rows(crawler, page, collector, login_baseline, _publication, _history):
    import os

    history = [] if _history is None else _history

    def prove_history():
        prior_phase = crawler._esun_spa_phase
        crawler._esun_spa_phase = "capture_publication"
        crawler._esun_spa_gate = None
        for prove in history:
            prove()
        crawler._esun_spa_phase = prior_phase

    def require(ok, gate=None, *, error_id):
        if not ok:
            crawler._esun_spa_gate = gate
            raise with_error_code(ValueError("SPA probe rejected"), error_id)

    native = _OriginGuardProxy._unwrap(page)
    crawler._diagnostic_stage = "collect_navigation"
    crawler._esun_spa_phase = "navigation"
    require(collector.page is native, error_id='ESUN-05-444-001')
    querying = selecting = False
    guard = navigation_guard(crawler, page, lambda: querying, lambda: selecting)

    def current_plan():
        guard()
        return read_plan()

    def read_plan():
        prior_phase = crawler._esun_spa_phase
        crawler._esun_spa_phase = 'menu_plan'
        body, envelope = require_current_success(collector, login_baseline, PATHS[0])
        request = envelope.get("requestBody")
        require(type(request) is dict, error_id='ESUN-05-465-001')
        locale = request.get("locale")
        require(type(locale) is str, error_id='ESUN-05-466-001')
        require(locale in ("zh-TW", "en-US"), error_id='ESUN-05-466-002')
        plan = build_menu_plan(body.get("menuList"), locale)
        crawler._esun_spa_phase = prior_phase
        return plan

    guard()
    with error_step('ESUN-05-663-001'):
        require(error_step('ESUN-05-663-101')(lambda: error_step('ESUN-05-663-100')(lambda: page.locator("#layout-content"))().count())() == 1, error_id='ESUN-05-445-001')
    require(crawler._logged_in(page) is True, error_id='ESUN-05-446-001')
    plan = current_plan()

    def revalidate():
        current = current_plan()
        require(current == plan, error_id='ESUN-05-467-001')
        return current

    baseline = collector.snapshot()
    crawler._esun_spa_phase = "destination_navigation"
    navigate_twd(page, plan, guard, revalidate)
    crawler._diagnostic_stage = "collect_accounts"
    crawler._esun_spa_phase = "prequery_response"
    deadline = monotonic() + TIMEOUT / 1000
    destination_failure = None
    while True:
        revalidate()
        require(monotonic() < deadline, error_id=destination_failure or 'ESUN-05-468-001')
        try:
            body, envelope = require_current_success(collector, baseline, PATHS[1])
        except ValueError:
            body = envelope = None
        destination_failure = None
        with error_step('ESUN-05-664-001'):
            if body is not None:
                destination, destination_failure = poll_observation(page.evaluate(DESTINATION_REASON), DESTINATION_CODES['prequery'])
                if destination is True:
                    destination_failure = None
                    break
        try:
            with error_step('ESUN-05-777-001'):
                page.wait_for_function(
                    "()=>new Promise(resolve=>requestAnimationFrame(()=>resolve(true)))",
                    timeout=max(1, int((deadline - monotonic()) * 1000)),
                )
        except Exception as source_error:
            raise with_error_code(ValueError("SPA probe rejected"), exception_error_id(source_error, bank="esun") or 'ESUN-05-565-001') from None
    revalidate()
    crawler._esun_spa_phase = "account_validation"
    body, envelope = require_current_success(collector, baseline, PATHS[1])
    from datetime import date
    from .query import query_twd, ROOT, FORM, SAFE

    def validate_account(body, envelope):
        request = envelope.get("requestBody")
        require(type(request) is dict, 'account_request', error_id='ESUN-05-469-001')
        require("account" in request, 'account_request', error_id='ESUN-05-469-002')
        account = request["account"]
        require(account is None
            or (
                type(account) is str
                and re.fullmatch("[0-9]{13}", account) is not None
                and (account == body.get("demandDeptAcc"))
            ), 'account_owner', error_id='ESUN-05-470-001')
        inventory = body.get("twAccountList")
        detail = body.get("queryDeptTxDtlResult")
        currency = body.get("twCurrInfo")
        require(type(inventory) is list, 'inventory_shape', error_id='ESUN-05-471-001')
        require(len(inventory) <= 64, 'inventory_shape', error_id='ESUN-05-471-002')
        require(all((type(a) is dict for a in inventory)), 'inventory_shape', error_id='ESUN-05-471-003')
        require(type(detail) is dict, 'inventory_shape', error_id='ESUN-05-471-004')
        require(type(currency) is dict, 'inventory_shape', error_id='ESUN-05-471-005')
        groups = detail.get("detailListData")
        require(type(groups) is list, 'inventory_groups', error_id='ESUN-05-472-001')
        require(len(groups) <= 128, 'inventory_groups', error_id='ESUN-05-472-002')
        row_count = 0
        for group in groups:
            require(type(group) is dict, 'inventory_rows', error_id='ESUN-05-491-001')
            require(type(group.get("detailInfo")) is list, 'inventory_rows', error_id='ESUN-05-491-002')
            require(all((type(row) is dict for row in group["detailInfo"])), 'inventory_rows', error_id='ESUN-05-491-003')
            row_count += len(group["detailInfo"])
            require(row_count <= 1000, 'inventory_budget', error_id='ESUN-05-492-001')
        accounts = [a.get("accountNo") for a in inventory]
        require(all((type(a) is str and re.fullmatch("[0-9]{13}", a) for a in accounts)), 'inventory_accounts', error_id='ESUN-05-473-001')
        require(len(set(accounts)) == len(accounts), 'inventory_selected', error_id='ESUN-05-474-001')
        require(body.get("demandDeptAcc") in accounts, 'inventory_selected', error_id='ESUN-05-474-002')
        require(currency.get("curr") == "TWD", 'currency', error_id='ESUN-05-475-001')
        require(all(type(a.get("accountAlias")) is str for a in inventory), 'inventory_alias', error_id='ESUN-05-476-001')

    with error_step('ESUN-05-665-001'):
        destination, destination_failure = poll_observation(page.evaluate(DESTINATION_REASON), DESTINATION_CODES['initial'])
        require(destination is True, error_id=destination_failure or 'ESUN-05-447-001')
    validate_account(body, envelope)
    revalidate()
    require_current_success(collector, baseline, PATHS[1])
    inventory = body["twAccountList"]
    selected_first = [body["demandDeptAcc"]] + [a["accountNo"] for a in inventory if a["accountNo"] != body["demandDeptAcc"]]
    mode = os.environ.get("BANK_CRAWLER_HISTORY_MODE", "full")
    require(mode in ("full", "incremental"), error_id='ESUN-05-448-001')
    expected_windows, covered_windows, finished_accounts = [], [], []

    def collect_account(body, baseline, remaining_accounts):
        nonlocal querying
        selected = body["demandDeptAcc"]
        alias = body.get("accountAlias")
        inventory = [a for a in body["twAccountList"] if a["accountNo"] == selected]
        require(type(alias) is str, error_id='ESUN-05-477-001')
        require(len(inventory) == 1, error_id='ESUN-05-477-002')
        require(inventory[0].get("accountAlias") == alias, error_id='ESUN-05-477-003')
        import calendar

        crawler._esun_spa_phase = "transaction_preflight"
        with error_step('ESUN-05-666-001'):
            today = date(*page.evaluate("() => {const d=new Date();return [d.getFullYear(),d.getMonth()+1,d.getDate()]}"))
        floor = date(today.year - 3, today.month, min(today.day, calendar.monthrange(today.year - 3, today.month)[1]))
        cursor = crawler.transaction_start_for(selected, domain="twd_transactions")
        require(cursor is None or (type(cursor) is date and cursor <= today), error_id='ESUN-05-478-001')
        start = crawler.transaction_window_start(selected, floor=floor, domain="twd_transactions")
        require(type(start) is date, error_id='ESUN-05-479-001')
        require(floor <= start <= today, error_id='ESUN-05-479-002')
        expected_windows.append({"identity": selected, "start": start.isoformat(), "end": today.isoformat()})
        year, month = divmod(start.year * 12 + start.month - 1 + 6, 12)
        end = min(today, date(year, month + 1, min(start.day, calendar.monthrange(year, month + 1)[1])))
        window = (start, end)
        require(type(window) is tuple, error_id='ESUN-05-480-001')
        require(len(window) == 2, error_id='ESUN-05-480-002')
        require(all((type(d) is date for d in window)), error_id='ESUN-05-480-003')
        display = selected + " " + alias
        rows_all = []

        def require_query_capacity():
            if collector.query_issued_count() >= collector.MAX_QUERY_REQUESTS:
                raise with_error_code(ValueError('query_budget_exhausted'), 'ESUN-05-562-001')

        def query_revalidate():
            revalidate()
            current, _ = require_current_success(collector, baseline, PATHS[1])
            require(current == body, error_id='ESUN-05-493-001')
            with error_step('ESUN-05-667-001'):
                destination, destination_failure = poll_observation(page.evaluate(DESTINATION_REASON), DESTINATION_CODES['query'])
                require(destination is True, error_id=destination_failure or 'ESUN-05-493-002')
            with error_step('ESUN-05-668-001'):
                field = page.locator(
                    ROOT
                    + ' .combo-input-wrapper[name="accountList"][role="button"] > input.combo-input[name="accountList"][readonly]'
                )
            with error_step('ESUN-05-669-001'):
                require(field.count() == 1, error_id='ESUN-05-494-001')
            with error_step('ESUN-05-670-001'):
                require(field.is_visible(), error_id='ESUN-05-494-002')
            with error_step('ESUN-05-671-001'):
                require(field.input_value() == display, error_id='ESUN-05-494-003')
            return display

        while True:
            crawler._esun_spa_phase = "transaction_preflight"
            crawler._esun_spa_gate = None
            if collector.query_issued_count() >= collector.MAX_QUERY_REQUESTS:
                return rows_all, False
            crawler._diagnostic_stage = "collect_transactions"
            query_revalidate()
            with error_step('ESUN-05-672-001'):
                previous_timeline, _ = poll_observation(page.evaluate(TIMELINE_REASON), TIMELINE_CODES)
            query_baseline = collector.snapshot()

            def require_unchanged_query_issuance():
                require_query_capacity()
                require(all(collector.issued_count(path) == query_baseline['counts'][path] for path in PATHS), error_id='ESUN-05-519-001')

            require_unchanged_query_issuance()
            querying = True
            crawler._esun_spa_phase = "transaction_form"
            failure = None

            def remember_failure(site):
                nonlocal failure
                if failure is None:
                    phase, gate = crawler._esun_spa_phase, crawler._esun_spa_gate
                    if gate is None and type(phase) is str and phase == "transaction_form":
                        gate = site
                    failure = (phase, gate)

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
                    on_failure=remember_failure,
                )
            except BaseException:
                if failure is not None:
                    crawler._esun_spa_phase, crawler._esun_spa_gate = failure
                raise
            finally:
                querying = False
            crawler._esun_spa_phase = "transaction_response"
            deadline = monotonic() + TIMEOUT / 1000
            while True:
                query_revalidate()
                require(monotonic() < deadline, error_id='ESUN-05-520-001')
                try:
                    response, envelope = require_current_success(collector, query_baseline, PATHS[2])
                    break
                except ValueError:
                    with error_step('ESUN-05-673-001'):
                        page.wait_for_timeout(20)
            crawler._esun_spa_phase = "transaction_request"
            request = envelope.get("requestBody")
            require(type(request) is dict, error_id='ESUN-05-495-001')
            require(set(request) == {"account", "startDate", "endDate", "startIndex", "count", "customerInputHashtag"}, error_id='ESUN-05-496-001')
            with error_step('ESUN-05-674-001'):
                expected = page.evaluate(
                    "ds=>ds.map(d=>new Date(d[0],d[1]-1,d[2]).toISOString())", [[d.year, d.month, d.day] for d in window]
                )
            require(request["account"] == selected, error_id='ESUN-05-497-001')
            require(request["startDate"] == expected[0], error_id='ESUN-05-498-001')
            require(request["endDate"] == expected[1], error_id='ESUN-05-499-001')
            require(type(request["startIndex"]) is int, error_id='ESUN-05-500-001')
            require(request["startIndex"] == 1, error_id='ESUN-05-501-001')
            require(type(request["count"]) is int, error_id='ESUN-05-502-001')
            require(request["count"] == 100, error_id='ESUN-05-503-001')
            require(request["customerInputHashtag"] == [], error_id='ESUN-05-504-001')
            crawler._esun_spa_phase = "transaction_shape"
            empty_state = empty_window_state(response)
            if empty_state in ("empty_shape_unknown", "empty_contradiction"):
                raise with_error_code(ValueError('ambiguous empty result'), 'ESUN-05-563-001')
            empty = empty_state == "explicit_empty_candidate"
            if not empty:
                validate_query_response(response)
            crawler._esun_spa_phase = "transaction_render"
            while True:
                query_revalidate()
                require_current_success(collector, query_baseline, PATHS[2])
                with error_step('ESUN-05-675-001'):
                    rendered, timeline_failure = poll_observation(page.evaluate(TIMELINE_REASON), TIMELINE_CODES)
                if empty:
                    with error_step('ESUN-05-676-001'):
                        card = page.evaluate(EMPTY_CARD)
                    if normalized(card) == "(2003)" + EMPTY_MESSAGE:
                        with error_step('ESUN-05-677-001'):
                            for field_index, (name, wanted) in enumerate(zip(("startDate", "endDate"), window)):
                                require(
                                    error_step(('ESUN-05-677-101', 'ESUN-05-864-008')[field_index])(lambda: error_step(('ESUN-05-677-100', 'ESUN-05-864-007')[field_index])(lambda: page.locator(ROOT + " " + FORM + " input[name=" + name + "][readonly]"))().input_value())()
                                    == wanted.strftime("%Y/%m/%d"),
                                    error_id=('ESUN-05-864-005', 'ESUN-05-864-006')[field_index])
                        break
                elif rendered is not None and rendered != previous_timeline:
                    break
                require(monotonic() < deadline, error_id=(timeline_failure if not empty and rendered is None else None) or 'ESUN-05-521-001')
                with error_step('ESUN-05-678-001'):
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
            require(all(collector.issued_count(path) == query_baseline["counts"][path] for path in PATHS if path != PATHS[2]), error_id='ESUN-05-505-001')
            prior_phase = crawler._esun_spa_phase
            crawler._esun_spa_phase = "capture_publication"
            crawler._esun_spa_gate = None
            _, _, prove_records = retain_capture_records(collector, PATHS[:3], history)
            crawler._esun_spa_phase = prior_phase

            def prove_empty(snapshot=query_baseline, saved=response, dates=window, records=prove_records):
                prior_phase = crawler._esun_spa_phase
                crawler._esun_spa_phase = "capture_publication"
                crawler._esun_spa_gate = None
                query_revalidate()
                require(require_current_success(collector, snapshot, PATHS[2])[0] == saved, error_id='ESUN-05-522-001')
                with error_step('ESUN-05-679-001'):
                    require(normalized(page.evaluate(EMPTY_CARD)) == "(2003)" + EMPTY_MESSAGE, error_id='ESUN-05-523-001')
                with error_step('ESUN-05-680-001'):
                    for field_index, (name, day) in enumerate(zip(("startDate", "endDate"), dates)):
                        require(error_step(('ESUN-05-680-101', 'ESUN-05-864-012')[field_index])(lambda: error_step(('ESUN-05-680-100', 'ESUN-05-864-011')[field_index])(lambda: page.locator(ROOT + " " + FORM + " input[name=" + name + "][readonly]"))().input_value())()
                                == day.strftime("%Y/%m/%d"), error_id=('ESUN-05-864-009', 'ESUN-05-864-010')[field_index])
                require(all(collector.issued_count(p) == snapshot["counts"][p] + (p == PATHS[2]) for p in PATHS), error_id='ESUN-05-525-001')
                records()
                prove_history()
                crawler._esun_spa_phase = prior_phase

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
        with error_step('ESUN-05-681-001'):
            field = page.locator(ROOT + ' .combo-input-wrapper[name="accountList"][role="button"]')
        with error_step('ESUN-05-682-001'):
            dialog = page.locator(ROOT + ' .combo-block:has(> .combo-input-wrapper[name="accountList"][role="button"]) > .clean-error-scope[name="accountList"] > dialog.combo-modal[open][role="dialog"][aria-modal="true"]')
        labels = [a["accountNo"] + " " + a["accountAlias"] for a in inventory]

        def unchanged():
            require(collector.query_issued_count() < collector.MAX_QUERY_REQUESTS, error_id='ESUN-05-506-001')
            require(collector.issued_count(PATHS[1]) < collector.MAX_QUERY_REQUESTS, error_id='ESUN-05-506-002')
            require(all(collector.issued_count(p) == before["counts"][p] for p in PATHS), error_id='ESUN-05-507-001')

        def owner():
            revalidate()
            with error_step('ESUN-05-683-001'):
                destination, destination_failure = poll_observation(page.evaluate(DESTINATION_REASON), DESTINATION_CODES['switch_owner'])
                require(destination is True, error_id=destination_failure or 'ESUN-05-508-001')
            require(require_current_success(collector, baseline, PATHS[1])[0] == body, error_id='ESUN-05-509-001')
            with error_step('ESUN-05-684-001'):
                require(field.count() == 1, error_id='ESUN-05-510-001')
            with error_step('ESUN-05-685-001'):
                require(field.is_visible(), error_id='ESUN-05-510-002')
            with error_step('ESUN-05-686-001'):
                require(field.evaluate(SAFE), error_id='ESUN-05-510-003')
            with error_step('ESUN-05-687-001'):
                require(error_step('ESUN-05-687-101')(lambda: error_step('ESUN-05-687-100')(lambda: field.locator('input.combo-input[name="accountList"][readonly]'))().input_value())()
                        == body["demandDeptAcc"] + " " + body["accountAlias"], error_id='ESUN-05-511-001')
            for prove in _publication:
                prove()
            unchanged()

        def click(target, codes):
            with error_step(codes[0]):
                require(target.count() == 1, error_id=codes[5])
            with error_step(codes[1]):
                require(target.is_visible(), error_id=codes[6])
            with error_step(codes[2]):
                require(target.is_enabled(), error_id=codes[7])
            with error_step(codes[3]):
                require(target.evaluate(SAFE), error_id=codes[8])
            action_guard = target._guard if isinstance(target, _OriginGuardProxy) else None
            target = _OriginGuardProxy._unwrap(target)
            if action_guard is not None:
                action_guard()
            owner()
            with error_step(codes[4]):
                target.click(timeout=TIMEOUT)

        owner()
        selecting = True
        try:
            click(field, ('ESUN-05-840-001', 'ESUN-05-840-002', 'ESUN-05-840-003', 'ESUN-05-840-004', 'ESUN-05-840-005', 'ESUN-05-840-006', 'ESUN-05-840-007', 'ESUN-05-840-008', 'ESUN-05-840-009'))
            deadline = monotonic() + TIMEOUT / 1000
            with error_step('ESUN-05-693-001'):
                while not dialog.count():
                    owner()
                    require(monotonic() < deadline, error_id='ESUN-05-526-001')
                    with error_step('ESUN-05-694-001'):
                        page.wait_for_timeout(20)
            owner()
            with error_step('ESUN-05-695-001'):
                require(dialog.count() == 1, error_id='ESUN-05-513-001')
            with error_step('ESUN-05-696-001'):
                require(dialog.is_visible(), error_id='ESUN-05-513-002')
            with error_step('ESUN-05-697-001'):
                items = dialog.locator(':scope > .custom-area > ul.info-scrollable > li[role="button"]')
            with error_step('ESUN-05-698-001'):
                require(items.all_text_contents() == labels, error_id='ESUN-05-514-001')
            with error_step('ESUN-05-699-001'):
                require(items.evaluate_all('nodes=>nodes.every(n=>(' + SAFE + ')(n))'), error_id='ESUN-05-515-001')
            with error_step('ESUN-05-702-001'):
                click(items.nth([a["accountNo"] for a in inventory].index(target)), ('ESUN-05-841-001', 'ESUN-05-841-002', 'ESUN-05-841-003', 'ESUN-05-841-004', 'ESUN-05-841-005', 'ESUN-05-841-006', 'ESUN-05-841-007', 'ESUN-05-841-008', 'ESUN-05-841-009'))
            _publication.clear()  # Prior page proven before the native account transition.
            while True:
                revalidate()
                require(monotonic() < deadline, error_id='ESUN-05-527-001')
                require(collector.query_issued_count() < collector.MAX_QUERY_REQUESTS, error_id='ESUN-05-528-001')
                require(all(collector.issued_count(p) == before["counts"][p] + (p == PATHS[1]) for p in PATHS), error_id='ESUN-05-529-001')
                try:
                    current, issued = require_current_success(collector, before, PATHS[1])
                except ValueError:
                    with error_step('ESUN-05-703-001'):
                        page.wait_for_timeout(20)
                    continue
                validate_account(current, issued)
                require(issued["requestBody"] == {"account": target}, error_id='ESUN-05-530-001')
                require(current["demandDeptAcc"] == target, error_id='ESUN-05-530-002')
                require(current["twAccountList"] == inventory, error_id='ESUN-05-531-001')
                with error_step('ESUN-05-704-001'):
                    destination, destination_failure = poll_observation(page.evaluate(DESTINATION_REASON), DESTINATION_CODES['switch_result'])
                    require(destination is True, error_id=destination_failure or 'ESUN-05-531-002')
                expected = target + " " + current["accountAlias"]
                with error_step('ESUN-05-705-001'):
                    if not error_step('ESUN-05-705-100')(lambda: dialog.count())() and error_step('ESUN-05-705-102')(lambda: error_step('ESUN-05-705-101')(lambda: field.locator('input[readonly]'))().input_value())() == expected:
                        return current, before
                with error_step('ESUN-05-706-001'):
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
    require(os.environ.get("BANK_CRAWLER_HISTORY_MODE", "full") == mode, error_id='ESUN-05-449-001')
    coverage = ({"mode": mode, "domains": [{"domain": "twd_transactions",
                 "expected": expected_windows, "windows": covered_windows}]}
                if len(finished_accounts) == len(selected_first) and len(expected_windows) == len(selected_first)
                else None)
    return rows_all, coverage
