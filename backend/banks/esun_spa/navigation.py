"""Pure desktop projection; no UI, routing invocation, logging or mutation.

Contract: public portal module 71362 and module 12128 desktop renderer.
positions/counts: filtered roots, filtered level2 groups, RAW level3 wrappers.
The caller must independently check current ownership and native DOM structure.
"""


from backend.core.error_codes import error_step, with_error_code
def build_menu_plan(menu_list, locale, *, target="CTW01002-TW"):

    def require(ok, *, error_id):
        if not ok:
            raise with_error_code(ValueError("invalid menu plan"), error_id)

    require(type(target) is str, error_id='ESUN-04-407-001')
    require(target in ("CTW01002-TW", "CCM01002"), error_id='ESUN-04-407-002')
    target_id = target
    tasks = ("CTW01002", "CTW01002-TW") if target == "CTW01002-TW" else ("CCM01002",)
    require(type(locale) is str, error_id='ESUN-04-408-001')
    require(locale in ("zh-TW", "en-US"), error_id='ESUN-04-408-002')
    require(type(menu_list) is list, error_id='ESUN-04-409-001')
    seen, budget = (set(), [0, 0])

    def plain(value, depth=0):
        budget[0] += 1
        require(depth <= 12, error_id='ESUN-04-434-001')
        require(budget[0] <= 20000, error_id='ESUN-04-434-002')
        kind = type(value)
        require(kind in (dict, list, str, int, float, bool, type(None)), error_id='ESUN-04-435-001')
        if kind is str:
            budget[1] += len(value)
            require(len(value) <= 8192, error_id='ESUN-04-453-001')
            require(budget[1] <= 1000000, error_id='ESUN-04-453-002')
        elif kind is float:
            require(value == value, error_id='ESUN-04-455-001')
            require(abs(value) != float("inf"), error_id='ESUN-04-455-002')
        elif kind in (dict, list):
            require(id(value) not in seen, error_id='ESUN-04-456-001')
            require(len(value) <= 4096, error_id='ESUN-04-456-002')
            seen.add(id(value))
            if kind is dict:
                for key, item in value.items():
                    require(type(key) is str, error_id='ESUN-04-457-001')
                    plain(key, depth + 1)
                    plain(item, depth + 1)
            else:
                for item in value:
                    plain(item, depth + 1)

    plain(menu_list)

    def children(n):
        value = n.get("childMenuList")
        require(value is None or type(value) is list, error_id='ESUN-04-436-001')
        return [] if value is None else value

    def visible(n):
        require(type(n.get("show")) is bool, error_id='ESUN-04-437-001')
        require(type(n.get("enable")) is bool, error_id='ESUN-04-437-002')
        return n["show"] and n["enable"]

    def route(n):
        info = n.get("redirectInfo")
        require(info is None or type(info) is dict, error_id='ESUN-04-438-001')
        task = n.get("taskId")
        require(task is None or type(task) is str, error_id='ESUN-04-439-001')
        custom = (info or {}).get("customRoute")
        require(custom is None or type(custom) is str, error_id='ESUN-04-440-001')
        return bool(task or custom)

    matches = []

    def scan(nodes, path):
        for n in nodes:
            require(type(n) is dict, error_id='ESUN-04-454-001')
            current = [*path, n]
            if n.get("taskId") in tasks:
                matches.append(current)
            scan(children(n), current)

    scan(menu_list, [])
    require(len(matches) == 1, error_id='ESUN-04-410-001')
    path = matches[0]
    require(len(path) in (2, 3), error_id='ESUN-04-411-001')
    target = path[-1]
    require(not children(target), error_id='ESUN-04-412-001')
    sub = target.get("subTaskId")
    require(sub is None or (type(sub) is str and sub == ""), error_id='ESUN-04-413-001')
    info = target.get("redirectInfo")
    require(info is None or type(info) is dict, error_id='ESUN-04-414-001')
    if info is not None:
        require(not set(info) - {"customRoute", "params"}, error_id='ESUN-04-441-001')
    custom = (info or {}).get("customRoute")
    params = (info or {}).get("params")
    require(params is None or (type(params) is str and params == ""), error_id='ESUN-04-415-001')
    if target["taskId"] == "CTW01002":
        require(custom == "mib:ctw01002-tw-home", error_id='ESUN-04-442-001')
    else:
        require(custom is None or (type(custom) is str and custom == ""), error_id='ESUN-04-443-001')

    def label(n):
        names = n.get("itemName")
        require(type(names) is dict, error_id='ESUN-04-444-001')
        alias = n.get("aliasName")
        require(alias is None or type(alias) is dict, error_id='ESUN-04-445-001')
        if alias is not None:
            names = alias
        text = names.get(locale)
        require(type(text) is str, error_id='ESUN-04-446-001')
        require(bool(text.strip()), error_id='ESUN-04-446-002')
        require(len(text) <= 512, error_id='ESUN-04-446-003')
        require(not any((ord(c) < 32 or ord(c) == 127 for c in text)), error_id='ESUN-04-447-001')
        return text

    roots = [n for n in menu_list if any([visible(c) for c in children(n)])]
    root = path[0]
    require(any((n is root for n in roots)), error_id='ESUN-04-416-001')
    groups = [n for n in children(root) if visible(n) and (route(n) or any((visible(c) for c in children(n))))]
    group = path[1]
    require(any((n is group for n in groups)), error_id='ESUN-04-417-001')
    levels = [roots, groups]
    if len(path) == 3:
        require(not route(group), error_id='ESUN-04-448-001')
        require(visible(target), error_id='ESUN-04-449-001')
        levels.append(children(group))
    labels, positions, counts = ([], [], [])
    for depth, (n, siblings) in enumerate(zip(path, levels)):
        text = label(n)
        displayed = siblings if depth < 2 else [s for s in siblings if visible(s)]
        normalized = " ".join(text.split())
        require(sum((" ".join(label(s).split()) == normalized for s in displayed)) == 1, error_id='ESUN-04-450-001')
        labels.append(text)
        positions.append(next((i for i, s in enumerate(siblings) if s is n)))
        counts.append(len(siblings))
    return dict(
        taskId=target_id,
        locale=locale,
        labels=labels,
        positions=positions,
        counts=counts,
        openLink="打開連結" if locale == "zh-TW" else "Open link",
    )


"""Desktop native clicks only. Caller owns origin/checkpoint/full-SPA provenance
and must validate the destination independently after this returns None.
Callbacks raise on failure; revalidate returns a freshly owned current plan.
No labels, DOM, or native exception messages are logged or exported here.
"""
from copy import deepcopy

HEADER = '#mib-portal-header-component-container .mib-header .header-option > div.no-margin-right[role="button"]'
MENU = ".mib-menu.mib-modal > dialog.menu-modal-container.menu-dialog"
CARD = ".widget.mib-txn-card.level1-card"
TIMEOUT = 2500
INSPECT = r"""(el, args) => {
 const [p, header] = args;
 const norm = s => s.replace(/\s+/gu,' ').trim();
 const all = (n,s) => Array.from(n.querySelectorAll(s));
 const direct = (n,s) => all(n,':scope > '+s);
 const safe = n => {
   for(let a=n;a;a=a.parentElement) {
     if(a.hidden || a.inert || a.hasAttribute('disabled') ||
        a.getAttribute('aria-disabled')==='true' || a.getAttribute('aria-hidden')==='true') return false;
   }
   return n.getClientRects().length>0 && getComputedStyle(n).visibility==='visible';
 };
 const actionable='[role="button"],button,a,input,select,textarea,[onclick],[tabindex]';
 const plain = n => all(n,actionable).length===0;
 const one = a => a.length===1 ? a[0] : null;
 const text = (n,t) => n && norm(n.textContent)===norm(t);
 if(!safe(el)) return false;
 if(header) {
   const owner=one(all(document,'#mib-portal-header-component-container'));
   const head=owner && one(all(owner,'.mib-header'));
   const option=head && one(all(head,'.header-option'));
   const label=one(direct(el,'div.mib-font-bold.services-button'));
   const title=p.locale==='zh-TW'?'所有服務':'All Features';
   return el.tagName==='DIV' && el.parentElement===option &&
     el.classList.contains('no-margin-right') && el.getAttribute('role')==='button' &&
     el.classList.contains('header-option-button') && !el.classList.contains('header-option-button-second') &&
     plain(el) && label && safe(label) && text(label,title) && text(el,title) &&
     all(el,'.services-button').length===1;
 }
 const roots=all(el,'.widget.mib-txn-card.level1-card');
 if(roots.length!==p.counts[0]) return false;
 let rootLabels=[];
 for(let i=0;i<roots.length;i++) {
   if(roots[i].id!=='menu-tab-card-'+i || !safe(roots[i])) return false;
   let label=one(direct(roots[i],'.widget-label'));
   if(!label || !safe(label) || !plain(label)) return false;
   rootLabels.push(norm(label.textContent));
 }
 if(rootLabels.filter(t=>t===norm(p.labels[0])).length!==1) return false;
 const root=roots[p.positions[0]];
 if(!text(one(direct(root,'.widget-label')),p.labels[0])) return false;
 const content=one(direct(root,'.card-content'));
 if(!content || !safe(content)) return false;
 const groups=direct(content,'.level2-menu');
 if(groups.length!==p.counts[1]) return false;
 const headings=groups.map(g=>one(direct(g,'.level2-link, :scope > .card-sub-title')));
 if(headings.some(n=>!n) || headings.filter(n=>text(n,p.labels[1])).length!==1) return false;
 const group=groups[p.positions[1]];
 if(!safe(group)) return false;
 let leaf;
 if(p.labels.length===2) {
   leaf=one(direct(group,'.level2-link'));
   if(direct(group,'.level3-menu').length) return false;
 } else {
   const heading=one(direct(group,'.card-sub-title'));
   if(!text(heading,p.labels[1]) || !safe(heading) || !plain(heading) || heading.matches(actionable)) return false;
   const wrappers=direct(group,'.level3-menu');
   if(wrappers.length!==p.counts[2]) return false;
   const leaves=wrappers.flatMap(w=>direct(w,'.mib-obvious.card-sub-title[role="button"]'));
   if(leaves.filter(n=>text(n,p.labels[2])).length!==1) return false;
   leaf=one(direct(wrappers[p.positions[2]],'.mib-obvious.card-sub-title[role="button"]'));
 }
 return leaf && safe(leaf) && plain(leaf) && leaf.getAttribute('role')==='button' &&
   text(leaf,p.labels.at(-1)) && norm(leaf.getAttribute('aria-label')||'')===norm(p.labels.at(-1)+' '+p.openLink);
}"""


def navigate_twd(page, plan, guard, revalidate, *, target="CTW01002-TW"):
    """Issue one header and one leaf click; never query or certify destination."""

    def require(ok, *, error_id):
        if not ok:
            raise with_error_code(ValueError("native menu rejected"), error_id)

    require(type(plan) is dict, error_id='ESUN-04-418-001')
    require(set(plan) == {"taskId", "locale", "labels", "positions", "counts", "openLink"}, error_id='ESUN-04-418-002')
    expected = deepcopy(plan)
    require(type(target) is str, error_id='ESUN-04-419-001')
    require(target in ("CTW01002-TW", "CCM01002"), error_id='ESUN-04-419-002')
    require(expected["taskId"] == target, error_id='ESUN-04-420-001')
    require(expected["locale"] in ("zh-TW", "en-US"), error_id='ESUN-04-420-002')
    require(expected["openLink"] == ("打開連結" if expected["locale"] == "zh-TW" else "Open link"), error_id='ESUN-04-421-001')
    labels, positions, counts = (expected[k] for k in ("labels", "positions", "counts"))
    require(all((type(v) is list for v in (labels, positions, counts))), error_id='ESUN-04-422-001')
    require(len(labels) in (2, 3), error_id='ESUN-04-422-002')
    require(len(labels) == len(positions) == len(counts), error_id='ESUN-04-423-001')
    require(all((type(s) is str and s.strip() and (len(s) <= 512) for s in labels)), error_id='ESUN-04-424-001')
    require(all((type(i) is int and type(n) is int and (0 <= i < n <= 4096) for i, n in zip(positions, counts))), error_id='ESUN-04-425-001')

    def checkpoint():
        require(guard() is not False, error_id='ESUN-04-451-001')
        current = revalidate()
        require(type(current) is dict, error_id='ESUN-04-452-001')
        require(current == expected, error_id='ESUN-04-452-002')
        require(plan == expected, error_id='ESUN-04-452-003')

    def click_once(target, error_id):
        with error_step(error_id):
            target.click(timeout=TIMEOUT)
        checkpoint()

    checkpoint()
    with error_step('ESUN-04-459-001'):
        require(page.evaluate("() => window.innerWidth") >= 1200, error_id='ESUN-04-426-001')
    with error_step('ESUN-04-460-001'):
        menus = page.locator(MENU)
    with error_step('ESUN-04-461-001'):
        require(error_step('ESUN-04-461-101')(lambda: error_step('ESUN-04-461-100')(lambda: menus.filter(visible=True))().count())() == 0, error_id='ESUN-04-427-001')
    with error_step('ESUN-04-462-001'):
        header = page.locator(HEADER)
    with error_step('ESUN-04-463-001'):
        require(header.count() == 1, error_id='ESUN-04-428-001')
    with error_step('ESUN-04-464-001'):
        require(header.is_visible(), error_id='ESUN-04-428-002')
    with error_step('ESUN-04-465-001'):
        require(header.is_enabled(), error_id='ESUN-04-428-003')
    with error_step('ESUN-04-466-001'):
        require(header.evaluate(INSPECT, [expected, True]), error_id='ESUN-04-429-001')
    checkpoint()
    with error_step('ESUN-04-467-001'):
        require(header.evaluate(INSPECT, [expected, True]), error_id='ESUN-04-430-001')
    click_once(header, "ESUN-06-700-001" if target == "CCM01002" else "ESUN-04-700-001")
    with error_step('ESUN-04-479-001'):
        menu = menus.filter(visible=True)
    with error_step('ESUN-04-468-001'):
        menu.wait_for(state="visible", timeout=TIMEOUT)
    checkpoint()
    with error_step('ESUN-04-469-001'):
        require(menu.count() == 1, error_id='ESUN-04-431-001')
    with error_step('ESUN-04-470-001'):
        require(menu.evaluate(INSPECT, [expected, False]), error_id='ESUN-04-431-002')
    with error_step('ESUN-04-471-001'):
        root = error_step('ESUN-04-471-101')(lambda: error_step('ESUN-04-471-100')(lambda: menu.locator(CARD))().nth(positions[0]))()
    with error_step('ESUN-04-472-001'):
        group = error_step('ESUN-04-472-101')(lambda: error_step('ESUN-04-472-100')(lambda: root.locator(":scope > .card-content > .level2-menu"))().nth(positions[1]))()
    with error_step('ESUN-04-473-001'):
        leaf = (
            error_step('ESUN-04-473-100')(lambda: group.locator(":scope > .level2-link"))()
            if len(labels) == 2
            else error_step('ESUN-04-473-103')(lambda: error_step('ESUN-04-473-101')(lambda: error_step('ESUN-04-473-102')(lambda: group.locator(":scope > .level3-menu"))()
            .nth(positions[2]))()
            .locator(':scope > .mib-obvious.card-sub-title[role="button"]'))()
        )
    checkpoint()
    with error_step('ESUN-04-474-001'):
        require(menu.count() == 1, error_id='ESUN-04-432-001')
    with error_step('ESUN-04-475-001'):
        require(menu.evaluate(INSPECT, [expected, False]), error_id='ESUN-04-432-002')
    with error_step('ESUN-04-476-001'):
        require(leaf.count() == 1, error_id='ESUN-04-433-001')
    with error_step('ESUN-04-477-001'):
        require(leaf.is_visible(), error_id='ESUN-04-433-002')
    with error_step('ESUN-04-478-001'):
        require(leaf.is_enabled(), error_id='ESUN-04-433-003')
    click_once(leaf, "ESUN-06-710-001" if target == "CCM01002" else "ESUN-04-710-001")
