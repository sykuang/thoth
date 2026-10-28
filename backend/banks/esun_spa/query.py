"""Native explicit-window action only; no coverage, data capture or account switch.
Caller revalidate() must freshly validate owned SPA/locale/account provenance and
return the exact selected account display supplied as selected_account.
"""

from backend.core.error_codes import error_step, with_error_code
import calendar
import json
import re
import time
from contextlib import contextmanager
from datetime import date
from backend.core.base import _OriginGuardProxy

TIMEOUT = 2500
ROOT = "#layout-content .ctw01002"
FORM = ".is-medium-hide > .search-helper > form.search-helper-container"
SAFE = r"""n => {
 for(let p=n;p;p=p.parentElement) if(p.hidden || p.inert || p.matches('.disabled,[disabled],[aria-disabled="true"],[aria-hidden="true"]')) return false;
 return n.isConnected && n.getClientRects().length>0 && getComputedStyle(n).visibility==='visible';
}"""


SAFE_REASON = r"""n => {
 for(let p=n;p;p=p.parentElement) {
  if(p.hidden) return [false, 1];
  if(p.inert) return [false, 2];
  if(p.matches('.disabled,[disabled],[aria-disabled="true"],[aria-hidden="true"]')) return [false, 3];
 }
 if(!n.isConnected) return [false, 4];
 if(!n.getClientRects().length) return [false, 5];
 if(getComputedStyle(n).visibility!=='visible') return [false, 6];
 return [true, 0];
}"""
SAFE_CODES = {
    'ESUN-05-850-008': ('ESUN-05-863-217', 'ESUN-05-863-218', 'ESUN-05-863-219', 'ESUN-05-863-220', 'ESUN-05-863-221', 'ESUN-05-863-222'),
    'ESUN-05-851-008': ('ESUN-05-863-223', 'ESUN-05-863-224', 'ESUN-05-863-225', 'ESUN-05-863-226', 'ESUN-05-863-227', 'ESUN-05-863-228'),

    'ESUN-05-784-008': ('ESUN-05-863-001', 'ESUN-05-863-002', 'ESUN-05-863-003', 'ESUN-05-863-004', 'ESUN-05-863-005', 'ESUN-05-863-006'),
    'ESUN-05-785-008': ('ESUN-05-863-007', 'ESUN-05-863-008', 'ESUN-05-863-009', 'ESUN-05-863-010', 'ESUN-05-863-011', 'ESUN-05-863-012'),
    'ESUN-05-812-008': ('ESUN-05-863-013', 'ESUN-05-863-014', 'ESUN-05-863-015', 'ESUN-05-863-016', 'ESUN-05-863-017', 'ESUN-05-863-018'),
    'ESUN-05-812-508': ('ESUN-05-863-019', 'ESUN-05-863-020', 'ESUN-05-863-021', 'ESUN-05-863-022', 'ESUN-05-863-023', 'ESUN-05-863-024'),
    'ESUN-05-579-003': ('ESUN-05-863-025', 'ESUN-05-863-026', 'ESUN-05-863-027', 'ESUN-05-863-028', 'ESUN-05-863-029', 'ESUN-05-863-030'),
    'ESUN-05-581-003': ('ESUN-05-863-031', 'ESUN-05-863-032', 'ESUN-05-863-033', 'ESUN-05-863-034', 'ESUN-05-863-035', 'ESUN-05-863-036'),
    'ESUN-05-783-008': ('ESUN-05-863-037', 'ESUN-05-863-038', 'ESUN-05-863-039', 'ESUN-05-863-040', 'ESUN-05-863-041', 'ESUN-05-863-042'),
    'ESUN-05-801-008': ('ESUN-05-863-043', 'ESUN-05-863-044', 'ESUN-05-863-045', 'ESUN-05-863-046', 'ESUN-05-863-047', 'ESUN-05-863-048'),
    'ESUN-05-797-008': ('ESUN-05-863-049', 'ESUN-05-863-050', 'ESUN-05-863-051', 'ESUN-05-863-052', 'ESUN-05-863-053', 'ESUN-05-863-054'),
    'ESUN-05-797-508': ('ESUN-05-863-055', 'ESUN-05-863-056', 'ESUN-05-863-057', 'ESUN-05-863-058', 'ESUN-05-863-059', 'ESUN-05-863-060'),
    'ESUN-05-798-008': ('ESUN-05-863-061', 'ESUN-05-863-062', 'ESUN-05-863-063', 'ESUN-05-863-064', 'ESUN-05-863-065', 'ESUN-05-863-066'),
    'ESUN-05-798-508': ('ESUN-05-863-067', 'ESUN-05-863-068', 'ESUN-05-863-069', 'ESUN-05-863-070', 'ESUN-05-863-071', 'ESUN-05-863-072'),
    'ESUN-05-799-008': ('ESUN-05-863-073', 'ESUN-05-863-074', 'ESUN-05-863-075', 'ESUN-05-863-076', 'ESUN-05-863-077', 'ESUN-05-863-078'),
    'ESUN-05-799-508': ('ESUN-05-863-079', 'ESUN-05-863-080', 'ESUN-05-863-081', 'ESUN-05-863-082', 'ESUN-05-863-083', 'ESUN-05-863-084'),
    'ESUN-05-786-008': ('ESUN-05-863-085', 'ESUN-05-863-086', 'ESUN-05-863-087', 'ESUN-05-863-088', 'ESUN-05-863-089', 'ESUN-05-863-090'),
    'ESUN-05-813-008': ('ESUN-05-863-091', 'ESUN-05-863-092', 'ESUN-05-863-093', 'ESUN-05-863-094', 'ESUN-05-863-095', 'ESUN-05-863-096'),
    'ESUN-05-814-008': ('ESUN-05-863-097', 'ESUN-05-863-098', 'ESUN-05-863-099', 'ESUN-05-863-100', 'ESUN-05-863-101', 'ESUN-05-863-102'),
    'ESUN-05-816-008': ('ESUN-05-863-103', 'ESUN-05-863-104', 'ESUN-05-863-105', 'ESUN-05-863-106', 'ESUN-05-863-107', 'ESUN-05-863-108'),
    'ESUN-05-817-008': ('ESUN-05-863-109', 'ESUN-05-863-110', 'ESUN-05-863-111', 'ESUN-05-863-112', 'ESUN-05-863-113', 'ESUN-05-863-114'),
    'ESUN-05-787-008': ('ESUN-05-863-115', 'ESUN-05-863-116', 'ESUN-05-863-117', 'ESUN-05-863-118', 'ESUN-05-863-119', 'ESUN-05-863-120'),
    'ESUN-05-788-008': ('ESUN-05-863-121', 'ESUN-05-863-122', 'ESUN-05-863-123', 'ESUN-05-863-124', 'ESUN-05-863-125', 'ESUN-05-863-126'),
    'ESUN-05-789-008': ('ESUN-05-863-127', 'ESUN-05-863-128', 'ESUN-05-863-129', 'ESUN-05-863-130', 'ESUN-05-863-131', 'ESUN-05-863-132'),
    'ESUN-05-790-008': ('ESUN-05-863-133', 'ESUN-05-863-134', 'ESUN-05-863-135', 'ESUN-05-863-136', 'ESUN-05-863-137', 'ESUN-05-863-138'),
    'ESUN-05-791-008': ('ESUN-05-863-139', 'ESUN-05-863-140', 'ESUN-05-863-141', 'ESUN-05-863-142', 'ESUN-05-863-143', 'ESUN-05-863-144'),
    'ESUN-05-792-008': ('ESUN-05-863-145', 'ESUN-05-863-146', 'ESUN-05-863-147', 'ESUN-05-863-148', 'ESUN-05-863-149', 'ESUN-05-863-150'),
    'ESUN-05-793-008': ('ESUN-05-863-151', 'ESUN-05-863-152', 'ESUN-05-863-153', 'ESUN-05-863-154', 'ESUN-05-863-155', 'ESUN-05-863-156'),
    'ESUN-05-794-008': ('ESUN-05-863-157', 'ESUN-05-863-158', 'ESUN-05-863-159', 'ESUN-05-863-160', 'ESUN-05-863-161', 'ESUN-05-863-162'),
    'ESUN-05-795-008': ('ESUN-05-863-163', 'ESUN-05-863-164', 'ESUN-05-863-165', 'ESUN-05-863-166', 'ESUN-05-863-167', 'ESUN-05-863-168'),
    'ESUN-05-796-008': ('ESUN-05-863-169', 'ESUN-05-863-170', 'ESUN-05-863-171', 'ESUN-05-863-172', 'ESUN-05-863-173', 'ESUN-05-863-174'),
    'ESUN-05-818-008': ('ESUN-05-863-175', 'ESUN-05-863-176', 'ESUN-05-863-177', 'ESUN-05-863-178', 'ESUN-05-863-179', 'ESUN-05-863-180'),
    'ESUN-05-815-008': ('ESUN-05-863-181', 'ESUN-05-863-182', 'ESUN-05-863-183', 'ESUN-05-863-184', 'ESUN-05-863-185', 'ESUN-05-863-186'),
    'ESUN-05-815-508': ('ESUN-05-863-187', 'ESUN-05-863-188', 'ESUN-05-863-189', 'ESUN-05-863-190', 'ESUN-05-863-191', 'ESUN-05-863-192'),
    'ESUN-05-819-008': ('ESUN-05-863-193', 'ESUN-05-863-194', 'ESUN-05-863-195', 'ESUN-05-863-196', 'ESUN-05-863-197', 'ESUN-05-863-198'),
    'ESUN-05-819-508': ('ESUN-05-863-199', 'ESUN-05-863-200', 'ESUN-05-863-201', 'ESUN-05-863-202', 'ESUN-05-863-203', 'ESUN-05-863-204'),
    'ESUN-05-820-008': ('ESUN-05-863-205', 'ESUN-05-863-206', 'ESUN-05-863-207', 'ESUN-05-863-208', 'ESUN-05-863-209', 'ESUN-05-863-210'),
    'ESUN-05-820-508': ('ESUN-05-863-211', 'ESUN-05-863-212', 'ESUN-05-863-213', 'ESUN-05-863-214', 'ESUN-05-863-215', 'ESUN-05-863-216'),
}


def safe_observation(result, fallback):
    # The same DOM evaluation returns only its old boolean and a bounded reason.
    if type(result) is list and len(result) == 2:
        ok, reason = result
        codes = SAFE_CODES.get(fallback)
        if codes is not None and type(reason) is int and 1 <= reason <= 6:
            return ok, codes[reason - 1]
        return ok, fallback
    return result, fallback


def query_twd(page, start, end, locale, selected_account, guard, revalidate, *, action_budget=None, before_submit=None, before_action=None, on_failure=None):
    """Select a legal explicit window with native calendar input, submit once.

    All inspected bank values remain in caller RAM; never printed or returned.
    """
    if action_budget is None:
        action_budget = [0]

    @contextmanager
    def site(label):
        # Report only on unwinding: successful nested guards cannot erase a site.
        try:
            yield
        except BaseException:
            if on_failure is not None:
                on_failure(label)
            raise

    @site("native_action_budget")
    def reserve_action():
        if type(action_budget) is not list:
            raise with_error_code(ValueError('native_action_budget_exhausted'), 'ESUN-05-605-001')
        if len(action_budget) != 1:
            raise with_error_code(ValueError('native_action_budget_exhausted'), 'ESUN-05-605-002')
        if type(action_budget[0]) is not int:
            raise with_error_code(ValueError('native_action_budget_exhausted'), 'ESUN-05-605-003')
        if not 0 <= action_budget[0] < 32:
            raise with_error_code(ValueError('native_action_budget_exhausted'), 'ESUN-05-605-004')
        action_budget[0] += 1

    def require(ok, *, error_id):
        if not ok:
            raise with_error_code(ValueError("native query rejected"), error_id)

    def checkpoint(codes):
        with error_step(codes[0]):
            require(guard() is not False, error_id=codes[1])
        with error_step(codes[2]):
            require(revalidate() == selected_account, error_id=codes[3])

    checkpoint(('ESUN-05-861-001', 'ESUN-05-861-002', 'ESUN-05-861-003', 'ESUN-05-861-004'))
    require(type(start) is date, error_id='ESUN-05-566-001')
    require(type(end) is date, error_id='ESUN-05-566-002')
    require(locale in ("zh-TW", "en-US"), error_id='ESUN-05-567-001')
    require(type(selected_account) is str, error_id='ESUN-05-568-001')
    require(bool(selected_account.strip()), error_id='ESUN-05-568-002')
    with error_step("ESUN-05-700-001"):
        today = date(*page.evaluate("() => {const d=new Date();return [d.getFullYear(),d.getMonth()+1,d.getDate()]}"))

    def shift(d, months):
        year, month = divmod(d.year * 12 + d.month - 1 + months, 12)
        return date(year, month + 1, min(d.day, calendar.monthrange(year, month + 1)[1]))

    require(shift(today, -36) <= start <= end <= today, error_id='ESUN-05-569-001')
    require(end <= shift(start, 6), error_id='ESUN-05-569-002')
    with error_step("ESUN-05-701-001"):
        require(page.evaluate("() => innerWidth") >= 1200, error_id='ESUN-05-570-001')
    with error_step('ESUN-05-717-001'):
        root = page.locator(ROOT)
    with error_step('ESUN-05-718-001'):
        form = root.locator(FORM)

    @site("form_owner")
    def one(target, error_ids=('ESUN-05-719-001', 'ESUN-05-593-001', 'ESUN-05-720-001', 'ESUN-05-579-001', 'ESUN-05-721-001', 'ESUN-05-579-002', 'ESUN-05-722-001', 'ESUN-05-579-003')):
        with site("form_cardinality"), error_step(error_ids[0]):
            require(target.count() == 1, error_id=error_ids[1])
        with error_step(error_ids[2]):
            require(target.is_visible(), error_id=error_ids[3])
        with error_step(error_ids[4]):
            require(target.is_enabled(), error_id=error_ids[5])
        with error_step(error_ids[6]):
            safe, safe_error_id = safe_observation(target.evaluate(SAFE_REASON), error_ids[7])
            require(safe, error_id=safe_error_id)
        return target

    active_base = None

    pending_readiness = None

    def observed(ok, error_id, reason_codes=()):
        nonlocal pending_readiness
        if type(ok) is list and len(ok) == 2:
            ok, reason = ok
            if type(reason) is int and 1 <= reason <= len(reason_codes):
                error_id = reason_codes[reason - 1]
        if ok:
            return True
        pending_readiness = error_id
        return False

    def ready(predicate, label="calendar_readiness"):
        nonlocal pending_readiness
        with site(label):
            deadline = time.monotonic() + TIMEOUT / 1000
            while True:
                checkpoint(('ESUN-05-861-005', 'ESUN-05-861-006', 'ESUN-05-861-007', 'ESUN-05-861-008'))
                owner()
                pending_readiness = None
                if predicate():
                    pending_readiness = None
                    return
                require(time.monotonic() < deadline, error_id=pending_readiness or 'ESUN-05-602-001')
                with error_step('ESUN-05-723-001'):
                    page.wait_for_timeout(20)

    def bare_one(target, error_ids=('ESUN-05-724-001', 'ESUN-05-580-001', 'ESUN-05-725-001', 'ESUN-05-581-001', 'ESUN-05-726-001', 'ESUN-05-581-002', 'ESUN-05-727-001', 'ESUN-05-581-003')):
        with error_step(error_ids[0]):
            require(target.count() == 1, error_id=error_ids[1])
        with error_step(error_ids[2]):
            require(target.is_visible(), error_id=error_ids[3])
        with error_step(error_ids[4]):
            require(target.is_enabled(), error_id=error_ids[5])
        with error_step(error_ids[6]):
            safe, safe_error_id = safe_observation(target.evaluate(SAFE_REASON), error_ids[7])
            require(safe, error_id=safe_error_id)
        return target

    @site("form_owner")
    def owner():
        # Live 2026-10-03: the SPA settles after each click (account swap, overlays);
        # a single-instant probe failed nondeterministically on slower hosts. Poll the
        # same predicate until TIMEOUT, then run it once more with labelled sites.
        deadline = time.monotonic() + TIMEOUT / 1000
        while time.monotonic() < deadline:
            try:
                owner_state(bare_one)
                return
            except ValueError:
                with error_step('ESUN-05-728-001'):
                    page.wait_for_timeout(50)
        owner_state(one)

    def owner_state(one):
        with error_step('ESUN-05-729-001'):
            dialogs = page.locator('dialog:visible,[role="dialog"]:visible')
        with error_step('ESUN-05-730-001'):
            require(dialogs.count() <= 1, error_id='ESUN-05-582-001')
        with error_step('ESUN-05-731-001'):
            if dialogs.count():
                require(active_base is not None, error_id='ESUN-05-594-001')
                with error_step('ESUN-05-732-001'):
                    require(error_step('ESUN-05-732-101')(lambda: error_step('ESUN-05-732-100')(lambda: active_base.locator('dialog.calendar-select-popup-area[open][role="dialog"]:visible'))().count())() == 1, error_id='ESUN-05-594-002')
        with error_step('ESUN-05-733-001'):
            require(error_step('ESUN-05-733-101')(lambda: error_step('ESUN-05-733-100')(lambda: page.locator("#layout-content"))().count())() == 1, error_id='ESUN-05-583-001')
        with error_step('ESUN-05-734-001'):
            require(error_step('ESUN-05-734-101')(lambda: error_step('ESUN-05-734-100')(lambda: page.locator(".ctw01002"))().count())() == 1, error_id='ESUN-05-583-002')
        one(root, error_ids=('ESUN-05-784-001', 'ESUN-05-784-002', 'ESUN-05-784-003', 'ESUN-05-784-004', 'ESUN-05-784-005', 'ESUN-05-784-006', 'ESUN-05-784-007', 'ESUN-05-784-008'))
        one(form, error_ids=('ESUN-05-785-001', 'ESUN-05-785-002', 'ESUN-05-785-003', 'ESUN-05-785-004', 'ESUN-05-785-005', 'ESUN-05-785-006', 'ESUN-05-785-007', 'ESUN-05-785-008'))
        with error_step('ESUN-05-735-001'):
            require(error_step('ESUN-05-735-101')(lambda: error_step('ESUN-05-735-100')(lambda: root.locator(".search-helper"))().count())() == 1, error_id='ESUN-05-584-001')
        with error_step('ESUN-05-736-001'):
            require(error_step('ESUN-05-736-101')(lambda: error_step('ESUN-05-736-100')(lambda: root.locator("form"))().count())() == 1, error_id='ESUN-05-584-002')
        with error_step('ESUN-05-737-001'):
            account = one(
                root.locator(
                    '.combo-input-wrapper[name="accountList"][role="button"] > input.combo-input[name="accountList"][readonly]'
                )
            , error_ids=('ESUN-05-801-001', 'ESUN-05-801-002', 'ESUN-05-801-003', 'ESUN-05-801-004', 'ESUN-05-801-005', 'ESUN-05-801-006', 'ESUN-05-801-007', 'ESUN-05-801-008'))
        with error_step('ESUN-05-738-001'):
            require(account.input_value() == selected_account, error_id='ESUN-05-585-001')
        with error_step('ESUN-05-739-001'):
            require(error_step('ESUN-05-739-101')(lambda: error_step('ESUN-05-739-100')(lambda: form.locator(".selected-tag"))().count())() == 0, error_id='ESUN-05-586-001')
        for field, codes in (
            ('keyword', ('ESUN-05-830-001', 'ESUN-05-830-002', 'ESUN-05-830-003', 'ESUN-05-830-004', 'ESUN-05-830-005', 'ESUN-05-830-006')),
            ('textquery', ('ESUN-05-831-001', 'ESUN-05-831-002', 'ESUN-05-831-003', 'ESUN-05-831-004', 'ESUN-05-831-005', 'ESUN-05-831-006')),
        ):
            with error_step(codes[0]):
                inputs = form.locator("input[name=" + field + "]")
            with error_step(codes[1]):
                require(inputs.count() <= 1, error_id=codes[4])
            with error_step(codes[2]):
                if inputs.count():
                    with error_step(codes[3]):
                        require(inputs.input_value() == "", error_id=codes[5])
        with error_step('ESUN-05-744-001'):
            require(error_step('ESUN-05-744-101')(lambda: error_step('ESUN-05-744-100')(lambda: form.locator('input[type="checkbox"]:checked'))().count())() == 0, error_id='ESUN-05-587-001')

    @site("native_action")
    def click(target, error_id="ESUN-05-745-001"):
        checkpoint(('ESUN-05-861-009', 'ESUN-05-861-010', 'ESUN-05-861-011', 'ESUN-05-861-012'))
        owner()
        (one(target) if error_id == "ESUN-05-745-001" else one(target, error_ids={'ESUN-05-800-001': ('ESUN-05-786-001', 'ESUN-05-786-002', 'ESUN-05-786-003', 'ESUN-05-786-004', 'ESUN-05-786-005', 'ESUN-05-786-006', 'ESUN-05-786-007', 'ESUN-05-786-008'), 'ESUN-05-802-001': ('ESUN-05-787-001', 'ESUN-05-787-002', 'ESUN-05-787-003', 'ESUN-05-787-004', 'ESUN-05-787-005', 'ESUN-05-787-006', 'ESUN-05-787-007', 'ESUN-05-787-008'), 'ESUN-05-803-001': ('ESUN-05-788-001', 'ESUN-05-788-002', 'ESUN-05-788-003', 'ESUN-05-788-004', 'ESUN-05-788-005', 'ESUN-05-788-006', 'ESUN-05-788-007', 'ESUN-05-788-008'), 'ESUN-05-804-001': ('ESUN-05-789-001', 'ESUN-05-789-002', 'ESUN-05-789-003', 'ESUN-05-789-004', 'ESUN-05-789-005', 'ESUN-05-789-006', 'ESUN-05-789-007', 'ESUN-05-789-008'), 'ESUN-05-805-001': ('ESUN-05-790-001', 'ESUN-05-790-002', 'ESUN-05-790-003', 'ESUN-05-790-004', 'ESUN-05-790-005', 'ESUN-05-790-006', 'ESUN-05-790-007', 'ESUN-05-790-008'), 'ESUN-05-806-001': ('ESUN-05-791-001', 'ESUN-05-791-002', 'ESUN-05-791-003', 'ESUN-05-791-004', 'ESUN-05-791-005', 'ESUN-05-791-006', 'ESUN-05-791-007', 'ESUN-05-791-008'), 'ESUN-05-807-001': ('ESUN-05-792-001', 'ESUN-05-792-002', 'ESUN-05-792-003', 'ESUN-05-792-004', 'ESUN-05-792-005', 'ESUN-05-792-006', 'ESUN-05-792-007', 'ESUN-05-792-008'), 'ESUN-05-808-001': ('ESUN-05-793-001', 'ESUN-05-793-002', 'ESUN-05-793-003', 'ESUN-05-793-004', 'ESUN-05-793-005', 'ESUN-05-793-006', 'ESUN-05-793-007', 'ESUN-05-793-008'), 'ESUN-05-809-001': ('ESUN-05-794-001', 'ESUN-05-794-002', 'ESUN-05-794-003', 'ESUN-05-794-004', 'ESUN-05-794-005', 'ESUN-05-794-006', 'ESUN-05-794-007', 'ESUN-05-794-008'), 'ESUN-05-810-001': ('ESUN-05-795-001', 'ESUN-05-795-002', 'ESUN-05-795-003', 'ESUN-05-795-004', 'ESUN-05-795-005', 'ESUN-05-795-006', 'ESUN-05-795-007', 'ESUN-05-795-008'), 'ESUN-05-811-001': ('ESUN-05-796-001', 'ESUN-05-796-002', 'ESUN-05-796-003', 'ESUN-05-796-004', 'ESUN-05-796-005', 'ESUN-05-796-006', 'ESUN-05-796-007', 'ESUN-05-796-008')}.get(error_id, ('ESUN-05-786-001', 'ESUN-05-786-002', 'ESUN-05-786-003', 'ESUN-05-786-004', 'ESUN-05-786-005', 'ESUN-05-786-006', 'ESUN-05-786-007', 'ESUN-05-786-008'))))
        action_guard = target._guard if isinstance(target, _OriginGuardProxy) else None
        target = _OriginGuardProxy._unwrap(target)
        if action_guard is not None:
            action_guard()
        if before_action is not None:
            with site("query_issuance_admission"):
                before_action()
        reserve_action()
        with error_step(error_id):
            target.click(timeout=TIMEOUT)
        if action_guard is not None:
            action_guard()
        checkpoint(('ESUN-05-861-013', 'ESUN-05-861-014', 'ESUN-05-861-015', 'ESUN-05-861-016'))
        owner()

    owner()
    with error_step('ESUN-05-746-001'):
        require(error_step('ESUN-05-746-101')(lambda: error_step('ESUN-05-746-100')(lambda: page.locator('dialog:visible,[role="dialog"]:visible'))().count())() == 0, error_id='ESUN-05-571-001')
    with error_step('ESUN-05-747-001'):
        radio = form.locator('input[type="radio"][name="periodValue"][value="customized"]')
    with error_step('ESUN-05-778-001'):
        if not one(radio, error_ids=('ESUN-05-813-001', 'ESUN-05-813-002', 'ESUN-05-813-003', 'ESUN-05-813-004', 'ESUN-05-813-005', 'ESUN-05-813-006', 'ESUN-05-813-007', 'ESUN-05-813-008')).is_checked():
            click(radio, 'ESUN-05-800-001')
    with error_step('ESUN-05-779-001'):
        require(one(radio, error_ids=('ESUN-05-814-001', 'ESUN-05-814-002', 'ESUN-05-814-003', 'ESUN-05-814-004', 'ESUN-05-814-005', 'ESUN-05-814-006', 'ESUN-05-814-007', 'ESUN-05-814-008')).is_checked(), error_id='ESUN-05-572-001')

    def dates_mounted():
        with error_step('ESUN-05-780-001'):
            require(one(radio, error_ids=('ESUN-05-818-001', 'ESUN-05-818-002', 'ESUN-05-818-003', 'ESUN-05-818-004', 'ESUN-05-818-005', 'ESUN-05-818-006', 'ESUN-05-818-007', 'ESUN-05-818-008')).is_checked(), error_id='ESUN-05-588-001')
        with error_step('ESUN-05-748-001'):
            fields = [
                error_step(('ESUN-05-852-001', 'ESUN-05-852-501')[field_index])(lambda: form.locator("input.combo-input[name=" + name + '][readonly][tabindex="-1"]'))()
                for field_index, name in enumerate(("startDate", "endDate"))
            ]
        for field_index, field in enumerate(fields):
            with error_step(('ESUN-05-853-001', 'ESUN-05-853-501')[field_index]):
                require(field.count() <= 1, error_id=('ESUN-05-853-002', 'ESUN-05-853-502')[field_index])
            with error_step(('ESUN-05-854-001', 'ESUN-05-854-501')[field_index]):
                if field.count():
                    one(field, error_ids=(('ESUN-05-815-001', 'ESUN-05-815-002', 'ESUN-05-815-003', 'ESUN-05-815-004', 'ESUN-05-815-005', 'ESUN-05-815-006', 'ESUN-05-815-007', 'ESUN-05-815-008') if field_index == 0 else ('ESUN-05-815-501', 'ESUN-05-815-502', 'ESUN-05-815-503', 'ESUN-05-815-504', 'ESUN-05-815-505', 'ESUN-05-815-506', 'ESUN-05-815-507', 'ESUN-05-815-508')))
        with error_step('ESUN-05-751-001'):
            return all((observed(error_step(('ESUN-05-855-001', 'ESUN-05-855-501')[field_index])(lambda: field.count())() == 1, ('ESUN-05-862-001', 'ESUN-05-862-002')[field_index]) for field_index, field in enumerate(fields)))

    ready(dates_mounted)
    for name, wanted in (("startDate", start), ("endDate", end)):
        with error_step(('ESUN-05-752-001' if name == "startDate" else 'ESUN-05-752-501')):
            field = one(form.locator("input.combo-input[name=" + name + '][readonly][tabindex="-1"]'), error_ids=(('ESUN-05-812-001', 'ESUN-05-812-002', 'ESUN-05-812-003', 'ESUN-05-812-004', 'ESUN-05-812-005', 'ESUN-05-812-006', 'ESUN-05-812-007', 'ESUN-05-812-008') if name == "startDate" else ('ESUN-05-812-501', 'ESUN-05-812-502', 'ESUN-05-812-503', 'ESUN-05-812-504', 'ESUN-05-812-505', 'ESUN-05-812-506', 'ESUN-05-812-507', 'ESUN-05-812-508')))
        with error_step(('ESUN-05-753-001' if name == "startDate" else 'ESUN-05-753-501')):
            base = error_step(('ESUN-05-753-101' if name == "startDate" else 'ESUN-05-753-601'))(lambda: error_step(('ESUN-05-753-100' if name == "startDate" else 'ESUN-05-753-600'))(lambda: form.locator(".calendar-base"))().filter(has=error_step(('ESUN-05-753-102' if name == "startDate" else 'ESUN-05-753-602'))(lambda: page.locator("input[name=" + name + "]"))()))()
        one(base, error_ids=(('ESUN-05-797-001', 'ESUN-05-797-002', 'ESUN-05-797-003', 'ESUN-05-797-004', 'ESUN-05-797-005', 'ESUN-05-797-006', 'ESUN-05-797-007', 'ESUN-05-797-008') if name == "startDate" else ('ESUN-05-797-501', 'ESUN-05-797-502', 'ESUN-05-797-503', 'ESUN-05-797-504', 'ESUN-05-797-505', 'ESUN-05-797-506', 'ESUN-05-797-507', 'ESUN-05-797-508')))
        active_base = base
        with error_step(('ESUN-05-754-001' if name == "startDate" else 'ESUN-05-754-501')):
            click(base.locator('.combo-block > .combo-input-wrapper[role="button"]'), ('ESUN-05-802-001' if name == "startDate" else 'ESUN-05-803-001'))
        with error_step(('ESUN-05-755-001' if name == "startDate" else 'ESUN-05-755-501')):
            popup = base.locator('dialog.calendar-select-popup-area[open][role="dialog"]')
        ready(error_step(('ESUN-05-710-001' if name == "startDate" else 'ESUN-05-710-501'))(lambda: observed(error_step(('ESUN-05-710-100' if name == "startDate" else 'ESUN-05-710-600'))(lambda: popup.count())() == 1, ('ESUN-05-862-003' if name == 'startDate' else 'ESUN-05-862-503')) and observed(error_step(('ESUN-05-710-101' if name == "startDate" else 'ESUN-05-710-601'))(lambda: popup.is_visible())(), ('ESUN-05-862-004' if name == 'startDate' else 'ESUN-05-862-504'))))
        one(popup, error_ids=(('ESUN-05-798-001', 'ESUN-05-798-002', 'ESUN-05-798-003', 'ESUN-05-798-004', 'ESUN-05-798-005', 'ESUN-05-798-006', 'ESUN-05-798-007', 'ESUN-05-798-008') if name == "startDate" else ('ESUN-05-798-501', 'ESUN-05-798-502', 'ESUN-05-798-503', 'ESUN-05-798-504', 'ESUN-05-798-505', 'ESUN-05-798-506', 'ESUN-05-798-507', 'ESUN-05-798-508')))
        with error_step(('ESUN-05-756-001' if name == "startDate" else 'ESUN-05-756-501')):
            require(error_step(('ESUN-05-756-101' if name == "startDate" else 'ESUN-05-756-601'))(lambda: error_step(('ESUN-05-756-100' if name == "startDate" else 'ESUN-05-756-600'))(lambda: page.locator('dialog:visible,[role="dialog"]:visible'))().count())() == 1, error_id=('ESUN-05-589-001' if name == "startDate" else 'ESUN-05-589-501'))
        weekdays = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
        months = (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        )
        with error_step(('ESUN-05-757-001' if name == "startDate" else 'ESUN-05-757-501')):
            header = popup.locator('.calendar-header-block > .header-label[role="button"]')

        @site("calendar_state")
        def title():
            with error_step(('ESUN-05-758-001' if name == "startDate" else 'ESUN-05-758-501')):
                text = one(header, error_ids=(('ESUN-05-819-001', 'ESUN-05-819-002', 'ESUN-05-819-003', 'ESUN-05-819-004', 'ESUN-05-819-005', 'ESUN-05-819-006', 'ESUN-05-819-007', 'ESUN-05-819-008') if name == "startDate" else ('ESUN-05-819-501', 'ESUN-05-819-502', 'ESUN-05-819-503', 'ESUN-05-819-504', 'ESUN-05-819-505', 'ESUN-05-819-506', 'ESUN-05-819-507', 'ESUN-05-819-508'))).inner_text().strip()
            with error_step(('ESUN-05-759-001' if name == "startDate" else 'ESUN-05-759-501')):
                require(header.get_attribute("aria-label") == text, error_id=('ESUN-05-597-001' if name == "startDate" else 'ESUN-05-597-501'))
            return text

        def day_title(year, month):
            return f"{year}年{month:02d}月" if locale == "zh-TW" else f"{months[month - 1][:3]}./{year}"

        with site("calendar_state"):
            initial = title()
            match = (
                re.fullmatch("(\\d{4})年(\\d{2})月", initial)
                if locale == "zh-TW"
                else re.fullmatch("([A-Z][a-z]{2})\\./(\\d{4})", initial)
            )
            require(match is not None, error_id=('ESUN-05-598-001' if name == "startDate" else 'ESUN-05-598-501'))
            if locale == "zh-TW":
                year, month = map(int, match.groups())
            else:
                require(match[1] in [m[:3] for m in months], error_id=('ESUN-05-604-001' if name == "startDate" else 'ESUN-05-604-501'))
                year, month = (int(match[2]), [m[:3] for m in months].index(match[1]) + 1)
            require(shift(today, -36).year <= year <= today.year, error_id=('ESUN-05-599-001' if name == "startDate" else 'ESUN-05-599-501'))
            require(1 <= month <= 12, error_id=('ESUN-05-599-002' if name == "startDate" else 'ESUN-05-599-502'))
        ready(error_step(('ESUN-05-711-001' if name == "startDate" else 'ESUN-05-711-501'))(lambda: observed(error_step(('ESUN-05-711-101' if name == "startDate" else 'ESUN-05-711-601'))(lambda: error_step(('ESUN-05-711-100' if name == "startDate" else 'ESUN-05-711-600'))(lambda: popup.locator(".days-block"))().count())() == 1, ('ESUN-05-862-005' if name == 'startDate' else 'ESUN-05-862-505'))))
        if (year, month) != (wanted.year, wanted.month):
            click(header, ('ESUN-05-804-001' if name == "startDate" else 'ESUN-05-805-001'))
            ready(
                error_step(('ESUN-05-714-001' if name == "startDate" else 'ESUN-05-714-501'))(lambda: (
                    observed(title() == str(year), ('ESUN-05-862-009' if name == 'startDate' else 'ESUN-05-862-509'))
                    and observed(error_step(('ESUN-05-714-101' if name == "startDate" else 'ESUN-05-714-601'))(lambda: error_step(('ESUN-05-714-100' if name == "startDate" else 'ESUN-05-714-600'))(lambda: popup.locator(".months-block > div > .month-item"))().count())() == 12, ('ESUN-05-862-010' if name == 'startDate' else 'ESUN-05-862-510'))
                    and (observed(error_step(('ESUN-05-714-103' if name == "startDate" else 'ESUN-05-714-603'))(lambda: error_step(('ESUN-05-714-102' if name == "startDate" else 'ESUN-05-714-602'))(lambda: popup.locator(".days-block"))().count())() == 0, ('ESUN-05-862-011' if name == 'startDate' else 'ESUN-05-862-511')))
                ))
            )
            steps = abs(wanted.year - year)
            require(steps <= today.year - shift(today, -36).year, error_id=('ESUN-05-600-001' if name == "startDate" else 'ESUN-05-600-501'))
            for _ in range(steps):
                direction = 1 if wanted.year > year else -1
                with error_step(('ESUN-05-760-001' if name == "startDate" else 'ESUN-05-760-501')):
                    click(
                        popup.locator(
                            ".calendar-header-block > "
                            + (".next-icon" if direction > 0 else ".prev-icon")
                            + '[role="button"]'
                        )
                    , ('ESUN-05-806-001' if name == "startDate" else 'ESUN-05-807-001'))
                year += direction
                ready(error_step(('ESUN-05-715-001' if name == "startDate" else 'ESUN-05-715-501'))(lambda: observed(title() == str(year), ('ESUN-05-862-012' if name == 'startDate' else 'ESUN-05-862-512')) and observed(error_step(('ESUN-05-715-101' if name == "startDate" else 'ESUN-05-715-601'))(lambda: error_step(('ESUN-05-715-100' if name == "startDate" else 'ESUN-05-715-600'))(lambda: popup.locator(".months-block > div > .month-item"))().count())() == 12, ('ESUN-05-862-013' if name == 'startDate' else 'ESUN-05-862-513'))))
            with error_step(('ESUN-05-761-001' if name == "startDate" else 'ESUN-05-761-501')):
                items = popup.locator('.months-block > div > span.month-item[role="button"]')
            with error_step(('ESUN-05-762-001' if name == "startDate" else 'ESUN-05-762-501')):
                require(items.count() == 12, error_id=('ESUN-05-601-001' if name == "startDate" else 'ESUN-05-601-501'))
            with error_step(('ESUN-05-763-001' if name == "startDate" else 'ESUN-05-763-501')):
                require(len(set(items.all_text_contents())) == 12, error_id=('ESUN-05-601-002' if name == "startDate" else 'ESUN-05-601-502'))
            with error_step(('ESUN-05-764-001' if name == "startDate" else 'ESUN-05-764-501')):
                transition = popup.evaluate_handle(r"""p => {
                const state={first:null};
                state.observer=new MutationObserver(records=>{
                    for(const record of records) for(const node of record.addedNodes) {
                        if(node.nodeType!==1) continue;
                        const grid=node.matches('.days-block')?node:node.querySelector('.days-block');
                        if(grid && !state.first) state.first=grid;
                    }
                });
                state.observer.observe(p,{childList:true,subtree:true});
                return state;
            }""")
            primary = None
            try:
                with error_step(('ESUN-05-765-001' if name == "startDate" else 'ESUN-05-765-501')):
                    click(items.nth(wanted.month - 1), ('ESUN-05-808-001' if name == "startDate" else 'ESUN-05-809-001'))
                ready(
                    error_step(('ESUN-05-716-001' if name == "startDate" else 'ESUN-05-716-501'))(lambda: (
                        observed(title() == day_title(wanted.year, wanted.month), ('ESUN-05-862-014' if name == 'startDate' else 'ESUN-05-862-514'))
                        and observed(error_step(('ESUN-05-716-101' if name == "startDate" else 'ESUN-05-716-601'))(lambda: error_step(('ESUN-05-716-100' if name == "startDate" else 'ESUN-05-716-600'))(lambda: popup.locator(".months-block"))().count())() == 0, ('ESUN-05-862-015' if name == 'startDate' else 'ESUN-05-862-515'))
                        and observed(error_step(('ESUN-05-716-102' if name == "startDate" else 'ESUN-05-716-602'))(lambda: transition.evaluate("s => !!s.first && !s.first.isConnected"))(), ('ESUN-05-862-016' if name == 'startDate' else 'ESUN-05-862-516'))
                        and observed(error_step(('ESUN-05-716-103' if name == "startDate" else 'ESUN-05-716-603'))(lambda: popup.evaluate(r"""p => {
                        const grids=p.querySelectorAll('.days-block');
                        if(!(grids.length===1)) return [false, 1];
                        if([...grids[0].classList].some(c=>/^slide-(left|right)-(enter|leave|move)/.test(c))) return [false, 2];
                        if(!p.getAnimations({subtree:true}).every(a=>a.playState==='finished')) return [false, 3];
                        return [true, 0];
                    }"""))(), ('ESUN-05-862-017' if name == 'startDate' else 'ESUN-05-862-517'), (('ESUN-05-862-018', 'ESUN-05-862-019', 'ESUN-05-862-020') if name == 'startDate' else ('ESUN-05-862-518', 'ESUN-05-862-519', 'ESUN-05-862-520')))
                    )),
                    "calendar_transition",
                )
            except BaseException as error:
                primary = error
                raise
            finally:
                cleanup_error = None
                try:
                    with site("calendar_cleanup"), error_step(('ESUN-05-766-001' if name == "startDate" else 'ESUN-05-766-501')):
                        transition.evaluate("s => s.observer.disconnect()")
                except BaseException as error:
                    cleanup_error = error
                try:
                    with site("calendar_cleanup"), error_step(('ESUN-05-767-001' if name == "startDate" else 'ESUN-05-767-501')):
                        transition.dispose()
                except BaseException as error:
                    if cleanup_error is None:
                        cleanup_error = error
                if primary is None and cleanup_error is not None:
                    raise with_error_code(cleanup_error, ('ESUN-05-606-001' if name == "startDate" else 'ESUN-05-606-501'))
        require(title() == day_title(wanted.year, wanted.month), error_id=('ESUN-05-590-001' if name == "startDate" else 'ESUN-05-590-501'))
        suffix = (
            wanted.strftime("%Y年%m月%d日") + "星期" + "一二三四五六日"[wanted.weekday()]
            if locale == "zh-TW"
            else f"{weekdays[wanted.weekday()]}, {months[wanted.month - 1]} {wanted.day}, {wanted.year}"
        )
        with error_step(('ESUN-05-768-001' if name == "startDate" else 'ESUN-05-768-501')):
            target = popup.locator(
                '.days-block > .week-area > .day-area > span.day-cell[role="button"][aria-label$='
                + json.dumps(suffix, ensure_ascii=False)
                + "]"
            )
        ready(error_step(('ESUN-05-712-001' if name == "startDate" else 'ESUN-05-712-501'))(lambda: observed(target.count() > 0, ('ESUN-05-862-006' if name == 'startDate' else 'ESUN-05-862-506'))))
        one(target, error_ids=(('ESUN-05-799-001', 'ESUN-05-799-002', 'ESUN-05-799-003', 'ESUN-05-799-004', 'ESUN-05-799-005', 'ESUN-05-799-006', 'ESUN-05-799-007', 'ESUN-05-799-008') if name == "startDate" else ('ESUN-05-799-501', 'ESUN-05-799-502', 'ESUN-05-799-503', 'ESUN-05-799-504', 'ESUN-05-799-505', 'ESUN-05-799-506', 'ESUN-05-799-507', 'ESUN-05-799-508')))
        with error_step(('ESUN-05-769-001' if name == "startDate" else 'ESUN-05-769-501')):
            require(target.inner_text().strip() == str(wanted.day), error_id=('ESUN-05-591-001' if name == "startDate" else 'ESUN-05-591-501'))
        click(target, ('ESUN-05-810-001' if name == "startDate" else 'ESUN-05-811-001'))
        ready(
            error_step(('ESUN-05-713-001' if name == "startDate" else 'ESUN-05-713-501'))(lambda: observed(error_step(('ESUN-05-713-101' if name == "startDate" else 'ESUN-05-713-601'))(lambda: error_step(('ESUN-05-713-100' if name == "startDate" else 'ESUN-05-713-600'))(lambda: base.locator("dialog:visible"))().count())() == 0, ('ESUN-05-862-007' if name == 'startDate' else 'ESUN-05-862-507')) and observed(error_step(('ESUN-05-713-102' if name == "startDate" else 'ESUN-05-713-602'))(lambda: field.input_value())() == wanted.strftime("%Y/%m/%d"), ('ESUN-05-862-008' if name == 'startDate' else 'ESUN-05-862-508')))
        )
        active_base = None
    checkpoint(('ESUN-05-861-017', 'ESUN-05-861-018', 'ESUN-05-861-019', 'ESUN-05-861-020'))
    owner()
    with error_step('ESUN-05-770-001'):
        require(error_step('ESUN-05-770-101')(lambda: error_step('ESUN-05-770-100')(lambda: page.locator('dialog:visible,[role="dialog"]:visible'))().count())() == 0, error_id='ESUN-05-573-001')
    with error_step('ESUN-05-781-001'):
        require(one(radio, error_ids=('ESUN-05-816-001', 'ESUN-05-816-002', 'ESUN-05-816-003', 'ESUN-05-816-004', 'ESUN-05-816-005', 'ESUN-05-816-006', 'ESUN-05-816-007', 'ESUN-05-816-008')).is_checked(), error_id='ESUN-05-574-001')
    for name, wanted in (("startDate", start), ("endDate", end)):
        with error_step(('ESUN-05-771-001' if name == "startDate" else 'ESUN-05-771-501')):
            require(error_step(('ESUN-05-771-100' if name == "startDate" else 'ESUN-05-771-600'))(lambda: one(error_step(('ESUN-05-771-101' if name == "startDate" else 'ESUN-05-771-601'))(lambda: form.locator("input[name=" + name + "][readonly]"))(), error_ids=(('ESUN-05-820-001', 'ESUN-05-820-002', 'ESUN-05-820-003', 'ESUN-05-820-004', 'ESUN-05-820-005', 'ESUN-05-820-006', 'ESUN-05-820-007', 'ESUN-05-820-008') if name == "startDate" else ('ESUN-05-820-501', 'ESUN-05-820-502', 'ESUN-05-820-503', 'ESUN-05-820-504', 'ESUN-05-820-505', 'ESUN-05-820-506', 'ESUN-05-820-507', 'ESUN-05-820-508'))).input_value())() == wanted.strftime("%Y/%m/%d"), error_id=('ESUN-05-592-001' if name == "startDate" else 'ESUN-05-592-501'))
    with error_step('ESUN-05-772-001'):
        submit = form.locator('.btn-area > .btn-main-block > button[type="submit"].btn-main-border')
    checkpoint(('ESUN-05-861-021', 'ESUN-05-861-022', 'ESUN-05-861-023', 'ESUN-05-861-024'))
    owner()
    with error_step('ESUN-05-782-001'):
        require(one(radio, error_ids=('ESUN-05-817-001', 'ESUN-05-817-002', 'ESUN-05-817-003', 'ESUN-05-817-004', 'ESUN-05-817-005', 'ESUN-05-817-006', 'ESUN-05-817-007', 'ESUN-05-817-008')).is_checked(), error_id='ESUN-05-575-001')
    readback_failure = None

    def final_readback(name, wanted):
        nonlocal readback_failure
        codes = (
            ('ESUN-05-850-001', 'ESUN-05-850-002', 'ESUN-05-850-003', 'ESUN-05-850-004', 'ESUN-05-850-005', 'ESUN-05-850-006', 'ESUN-05-850-007', 'ESUN-05-850-008', 'ESUN-05-850-009', 'ESUN-05-850-010', 'ESUN-05-850-011')
            if name == 'startDate' else
            ('ESUN-05-851-001', 'ESUN-05-851-002', 'ESUN-05-851-003', 'ESUN-05-851-004', 'ESUN-05-851-005', 'ESUN-05-851-006', 'ESUN-05-851-007', 'ESUN-05-851-008', 'ESUN-05-851-009', 'ESUN-05-851-010', 'ESUN-05-851-011')
        )
        with error_step(codes[8]):
            value = one(error_step(codes[9])(lambda: form.locator("input[name=" + name + "][readonly]"))(), error_ids=codes[:8]).input_value()
        matches = value == wanted.strftime("%Y/%m/%d")
        if not matches:
            readback_failure = codes[10]
        return matches

    with error_step('ESUN-05-773-001'):
        require(all(final_readback(name, wanted) for name, wanted in (("startDate", start), ("endDate", end))),
                error_id=readback_failure or 'ESUN-05-576-001')
    one(submit, error_ids=('ESUN-05-783-001', 'ESUN-05-783-002', 'ESUN-05-783-003', 'ESUN-05-783-004', 'ESUN-05-783-005', 'ESUN-05-783-006', 'ESUN-05-783-007', 'ESUN-05-783-008'))
    submit_guard = submit._guard if isinstance(submit, _OriginGuardProxy) else None
    submit = _OriginGuardProxy._unwrap(submit)
    if submit_guard is not None:
        submit_guard()
    if before_submit is not None:
        with site("query_issuance_admission"):
            before_submit()
    reserve_action()
    with site("query_submit"), error_step('ESUN-05-774-001'):
        submit.click(timeout=TIMEOUT)
    with site("post_submit_validation"):
        if submit_guard is not None:
            submit_guard()
        checkpoint(('ESUN-05-861-025', 'ESUN-05-861-026', 'ESUN-05-861-027', 'ESUN-05-861-028'))
