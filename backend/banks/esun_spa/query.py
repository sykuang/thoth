"""Native explicit-window action only; no coverage, data capture or account switch.
Caller revalidate() must freshly validate owned SPA/locale/account provenance and
return the exact selected account display supplied as selected_account.
"""

import calendar
import json
import re
import time
from datetime import date
from backend.core.base import _OriginGuardProxy

TIMEOUT = 2500
ROOT = "#layout-content .ctw01002"
FORM = ".is-medium-hide > .search-helper > form.search-helper-container"
SAFE = r"""n => {
 for(let p=n;p;p=p.parentElement) if(p.hidden || p.inert || p.matches('.disabled,[disabled],[aria-disabled="true"],[aria-hidden="true"]')) return false;
 return n.isConnected && n.getClientRects().length>0 && getComputedStyle(n).visibility==='visible';
}"""


def query_twd(page, start, end, locale, selected_account, guard, revalidate, *, action_budget=None, before_submit=None, before_action=None):
    """Select a legal explicit window with native calendar input, submit once.

    All inspected bank values remain in caller RAM; never printed or returned.
    """
    if action_budget is None:
        action_budget = [0]

    def reserve_action():
        if (
            type(action_budget) is not list
            or len(action_budget) != 1
            or type(action_budget[0]) is not int
            or (not 0 <= action_budget[0] < 32)
        ):
            raise ValueError("native_action_budget_exhausted")
        action_budget[0] += 1

    def require(ok):
        if not ok:
            raise ValueError("native query rejected")

    def checkpoint():
        require(guard() is not False)
        require(revalidate() == selected_account)

    checkpoint()
    require(type(start) is date and type(end) is date)
    require(locale in ("zh-TW", "en-US"))
    require(type(selected_account) is str and bool(selected_account.strip()))
    today = date(*page.evaluate("() => {const d=new Date();return [d.getFullYear(),d.getMonth()+1,d.getDate()]}"))

    def shift(d, months):
        year, month = divmod(d.year * 12 + d.month - 1 + months, 12)
        return date(year, month + 1, min(d.day, calendar.monthrange(year, month + 1)[1]))

    require(shift(today, -36) <= start <= end <= today and end <= shift(start, 6))
    require(page.evaluate("() => innerWidth") >= 1200)
    root = page.locator(ROOT)
    form = root.locator(FORM)

    def one(target):
        require(target.count() == 1)
        require(target.is_visible() and target.is_enabled() and target.evaluate(SAFE))
        return target

    active_base = None

    def ready(predicate):
        deadline = time.monotonic() + TIMEOUT / 1000
        while True:
            checkpoint()
            owner()
            if predicate():
                return
            require(time.monotonic() < deadline)
            page.wait_for_timeout(20)

    def owner():
        dialogs = page.locator('dialog:visible,[role="dialog"]:visible')
        require(dialogs.count() <= 1)
        if dialogs.count():
            require(
                active_base is not None
                and active_base.locator('dialog.calendar-select-popup-area[open][role="dialog"]:visible').count() == 1
            )
        require(page.locator("#layout-content").count() == 1 and page.locator(".ctw01002").count() == 1)
        one(root)
        one(form)
        require(root.locator(".search-helper").count() == 1 and root.locator("form").count() == 1)
        account = one(
            root.locator(
                '.combo-input-wrapper[name="accountList"][role="button"] > input.combo-input[name="accountList"][readonly]'
            )
        )
        require(account.input_value() == selected_account)
        require(form.locator(".selected-tag").count() == 0)
        for field in ("keyword", "textquery"):
            inputs = form.locator("input[name=" + field + "]")
            require(inputs.count() <= 1)
            if inputs.count():
                require(inputs.input_value() == "")
        require(form.locator('input[type="checkbox"]:checked').count() == 0)

    def click(target):
        checkpoint()
        owner()
        one(target)
        action_guard = target._guard if isinstance(target, _OriginGuardProxy) else None
        target = _OriginGuardProxy._unwrap(target)
        if action_guard is not None:
            action_guard()
        if before_action is not None:
            before_action()
        reserve_action()
        target.click(timeout=TIMEOUT)
        if action_guard is not None:
            action_guard()
        checkpoint()
        owner()

    owner()
    require(page.locator('dialog:visible,[role="dialog"]:visible').count() == 0)
    radio = form.locator('input[type="radio"][name="periodValue"][value="customized"]')
    if not one(radio).is_checked():
        click(radio)
    require(one(radio).is_checked())

    def dates_mounted():
        require(one(radio).is_checked())
        fields = [
            form.locator("input.combo-input[name=" + name + '][readonly][tabindex="-1"]')
            for name in ("startDate", "endDate")
        ]
        for field in fields:
            require(field.count() <= 1)
            if field.count():
                one(field)
        return all((field.count() == 1 for field in fields))

    ready(dates_mounted)
    for name, wanted in (("startDate", start), ("endDate", end)):
        field = one(form.locator("input.combo-input[name=" + name + '][readonly][tabindex="-1"]'))
        base = form.locator(".calendar-base").filter(has=page.locator("input[name=" + name + "]"))
        one(base)
        active_base = base
        click(base.locator('.combo-block > .combo-input-wrapper[role="button"]'))
        popup = base.locator('dialog.calendar-select-popup-area[open][role="dialog"]')
        ready(lambda: popup.count() == 1 and popup.is_visible())
        one(popup)
        require(page.locator('dialog:visible,[role="dialog"]:visible').count() == 1)
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
        header = popup.locator('.calendar-header-block > .header-label[role="button"]')

        def title():
            text = one(header).inner_text().strip()
            require(header.get_attribute("aria-label") == text)
            return text

        def day_title(year, month):
            return f"{year}年{month:02d}月" if locale == "zh-TW" else f"{months[month - 1][:3]}./{year}"

        initial = title()
        match = (
            re.fullmatch("(\\d{4})年(\\d{2})月", initial)
            if locale == "zh-TW"
            else re.fullmatch("([A-Z][a-z]{2})\\./(\\d{4})", initial)
        )
        require(match is not None)
        if locale == "zh-TW":
            year, month = map(int, match.groups())
        else:
            require(match[1] in [m[:3] for m in months])
            year, month = (int(match[2]), [m[:3] for m in months].index(match[1]) + 1)
        require(shift(today, -36).year <= year <= today.year and 1 <= month <= 12)
        ready(lambda: popup.locator(".days-block").count() == 1)
        if (year, month) != (wanted.year, wanted.month):
            click(header)
            ready(
                lambda: (
                    title() == str(year)
                    and popup.locator(".months-block > div > .month-item").count() == 12
                    and (popup.locator(".days-block").count() == 0)
                )
            )
            steps = abs(wanted.year - year)
            require(steps <= today.year - shift(today, -36).year)
            for _ in range(steps):
                direction = 1 if wanted.year > year else -1
                click(
                    popup.locator(
                        ".calendar-header-block > "
                        + (".next-icon" if direction > 0 else ".prev-icon")
                        + '[role="button"]'
                    )
                )
                year += direction
                ready(lambda: title() == str(year) and popup.locator(".months-block > div > .month-item").count() == 12)
            items = popup.locator('.months-block > div > span.month-item[role="button"]')
            require(items.count() == 12 and len(set(items.all_text_contents())) == 12)
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
                click(items.nth(wanted.month - 1))
                ready(
                    lambda: (
                        title() == day_title(wanted.year, wanted.month)
                        and popup.locator(".months-block").count() == 0
                        and transition.evaluate("s => !!s.first && !s.first.isConnected")
                        and popup.evaluate(r"""p => {
                        const grids=p.querySelectorAll('.days-block');
                        return grids.length===1
                            && ![...grids[0].classList].some(c=>/^slide-(left|right)-(enter|leave|move)/.test(c))
                            && p.getAnimations({subtree:true}).every(a=>a.playState==='finished');
                    }""")
                    )
                )
            except BaseException as error:
                primary = error
                raise
            finally:
                cleanup_error = None
                try:
                    transition.evaluate("s => s.observer.disconnect()")
                except BaseException as error:
                    cleanup_error = error
                try:
                    transition.dispose()
                except BaseException as error:
                    if cleanup_error is None:
                        cleanup_error = error
                if primary is None and cleanup_error is not None:
                    raise cleanup_error
        require(title() == day_title(wanted.year, wanted.month))
        suffix = (
            wanted.strftime("%Y年%m月%d日") + "星期" + "一二三四五六日"[wanted.weekday()]
            if locale == "zh-TW"
            else f"{weekdays[wanted.weekday()]}, {months[wanted.month - 1]} {wanted.day}, {wanted.year}"
        )
        target = popup.locator(
            '.days-block > .week-area > .day-area > span.day-cell[role="button"][aria-label$='
            + json.dumps(suffix, ensure_ascii=False)
            + "]"
        )
        ready(lambda: target.count() > 0)
        one(target)
        require(target.inner_text().strip() == str(wanted.day))
        click(target)
        ready(
            lambda: base.locator("dialog:visible").count() == 0 and field.input_value() == wanted.strftime("%Y/%m/%d")
        )
        active_base = None
    checkpoint()
    owner()
    require(page.locator('dialog:visible,[role="dialog"]:visible').count() == 0)
    require(one(radio).is_checked())
    for name, wanted in (("startDate", start), ("endDate", end)):
        require(one(form.locator("input[name=" + name + "][readonly]")).input_value() == wanted.strftime("%Y/%m/%d"))
    submit = form.locator('.btn-area > .btn-main-block > button[type="submit"].btn-main-border')
    checkpoint()
    owner()
    require(one(radio).is_checked())
    require(
        all(
            (
                one(form.locator("input[name=" + name + "][readonly]")).input_value() == wanted.strftime("%Y/%m/%d")
                for name, wanted in (("startDate", start), ("endDate", end))
            )
        )
    )
    one(submit)
    submit_guard = submit._guard if isinstance(submit, _OriginGuardProxy) else None
    submit = _OriginGuardProxy._unwrap(submit)
    if submit_guard is not None:
        submit_guard()
    if before_submit is not None:
        before_submit()
    reserve_action()
    submit.click(timeout=TIMEOUT)
    if submit_guard is not None:
        submit_guard()
    checkpoint()
