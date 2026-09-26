"""Pure desktop projection; no UI, routing invocation, logging or mutation.

Contract: public portal module 71362 and module 12128 desktop renderer.
positions/counts: filtered roots, filtered level2 groups, RAW level3 wrappers.
The caller must independently check current ownership and native DOM structure.
"""


def build_menu_plan(menu_list, locale, *, target="CTW01002-TW"):

    def require(ok):
        if not ok:
            raise ValueError("invalid menu plan")

    require(type(target) is str and target in ("CTW01002-TW", "CCM01002"))
    target_id = target
    tasks = ("CTW01002", "CTW01002-TW") if target == "CTW01002-TW" else ("CCM01002",)
    require(type(locale) is str and locale in ("zh-TW", "en-US"))
    require(type(menu_list) is list)
    seen, budget = (set(), [0, 0])

    def plain(value, depth=0):
        budget[0] += 1
        require(depth <= 12 and budget[0] <= 20000)
        kind = type(value)
        require(kind in (dict, list, str, int, float, bool, type(None)))
        if kind is str:
            budget[1] += len(value)
            require(len(value) <= 8192 and budget[1] <= 1000000)
        elif kind is float:
            require(value == value and abs(value) != float("inf"))
        elif kind in (dict, list):
            require(id(value) not in seen and len(value) <= 4096)
            seen.add(id(value))
            if kind is dict:
                for key, item in value.items():
                    require(type(key) is str)
                    plain(key, depth + 1)
                    plain(item, depth + 1)
            else:
                for item in value:
                    plain(item, depth + 1)

    plain(menu_list)

    def children(n):
        value = n.get("childMenuList")
        require(value is None or type(value) is list)
        return [] if value is None else value

    def visible(n):
        require(type(n.get("show")) is bool and type(n.get("enable")) is bool)
        return n["show"] and n["enable"]

    def route(n):
        info = n.get("redirectInfo")
        require(info is None or type(info) is dict)
        task = n.get("taskId")
        require(task is None or type(task) is str)
        custom = (info or {}).get("customRoute")
        require(custom is None or type(custom) is str)
        return bool(task or custom)

    matches = []

    def scan(nodes, path):
        for n in nodes:
            require(type(n) is dict)
            current = [*path, n]
            if n.get("taskId") in tasks:
                matches.append(current)
            scan(children(n), current)

    scan(menu_list, [])
    require(len(matches) == 1)
    path = matches[0]
    require(len(path) in (2, 3))
    target = path[-1]
    require(not children(target))
    sub = target.get("subTaskId")
    require(sub is None or (type(sub) is str and sub == ""))
    info = target.get("redirectInfo")
    require(info is None or type(info) is dict)
    if info is not None:
        require(not set(info) - {"customRoute", "params"})
    custom = (info or {}).get("customRoute")
    params = (info or {}).get("params")
    require(params is None or (type(params) is str and params == ""))
    if target["taskId"] == "CTW01002":
        require(custom == "mib:ctw01002-tw-home")
    else:
        require(custom is None or (type(custom) is str and custom == ""))

    def label(n):
        names = n.get("itemName")
        require(type(names) is dict)
        alias = n.get("aliasName")
        require(alias is None or type(alias) is dict)
        if alias is not None:
            names = alias
        text = names.get(locale)
        require(type(text) is str and bool(text.strip()) and (len(text) <= 512))
        require(not any((ord(c) < 32 or ord(c) == 127 for c in text)))
        return text

    roots = [n for n in menu_list if any([visible(c) for c in children(n)])]
    root = path[0]
    require(any((n is root for n in roots)))
    groups = [n for n in children(root) if visible(n) and (route(n) or any((visible(c) for c in children(n))))]
    group = path[1]
    require(any((n is group for n in groups)))
    levels = [roots, groups]
    if len(path) == 3:
        require(not route(group))
        require(visible(target))
        levels.append(children(group))
    labels, positions, counts = ([], [], [])
    for depth, (n, siblings) in enumerate(zip(path, levels)):
        text = label(n)
        displayed = siblings if depth < 2 else [s for s in siblings if visible(s)]
        normalized = " ".join(text.split())
        require(sum((" ".join(label(s).split()) == normalized for s in displayed)) == 1)
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
   if(direct(group,'.level3-menu').length || all(group,actionable).length!==1) return false;
 } else {
   const heading=one(direct(group,'.card-sub-title'));
   if(!text(heading,p.labels[1]) || !safe(heading) || !plain(heading) || heading.matches(actionable)) return false;
   const wrappers=direct(group,'.level3-menu');
   if(wrappers.length!==p.counts[2]) return false;
   const leaves=wrappers.flatMap(w=>direct(w,'.mib-obvious.card-sub-title[role="button"]'));
   if(leaves.filter(n=>text(n,p.labels[2])).length!==1) return false;
   if(all(group,actionable).length!==leaves.length) return false;
   leaf=one(direct(wrappers[p.positions[2]],'.mib-obvious.card-sub-title[role="button"]'));
 }
 return leaf && safe(leaf) && plain(leaf) && leaf.getAttribute('role')==='button' &&
   text(leaf,p.labels.at(-1)) && norm(leaf.getAttribute('aria-label')||'')===norm(p.labels.at(-1)+' '+p.openLink);
}"""


def navigate_twd(page, plan, guard, revalidate, *, target="CTW01002-TW"):
    """Issue one header and one leaf click; never query or certify destination."""

    def require(ok):
        if not ok:
            raise ValueError("native menu rejected")

    require(type(plan) is dict and set(plan) == {"taskId", "locale", "labels", "positions", "counts", "openLink"})
    expected = deepcopy(plan)
    require(type(target) is str and target in ("CTW01002-TW", "CCM01002"))
    require(expected["taskId"] == target and expected["locale"] in ("zh-TW", "en-US"))
    require(expected["openLink"] == ("打開連結" if expected["locale"] == "zh-TW" else "Open link"))
    labels, positions, counts = (expected[k] for k in ("labels", "positions", "counts"))
    require(all((type(v) is list for v in (labels, positions, counts))) and len(labels) in (2, 3))
    require(len(labels) == len(positions) == len(counts))
    require(all((type(s) is str and s.strip() and (len(s) <= 512) for s in labels)))
    require(all((type(i) is int and type(n) is int and (0 <= i < n <= 4096) for i, n in zip(positions, counts))))

    def checkpoint():
        require(guard() is not False)
        current = revalidate()
        require(type(current) is dict and current == expected and (plan == expected))

    def click_once(target):
        try:
            target.click(timeout=TIMEOUT)
        except BaseException as primary:
            try:
                checkpoint()
            except BaseException as secondary:
                raise primary from secondary
            raise
        checkpoint()

    checkpoint()
    require(page.evaluate("() => window.innerWidth") >= 1200)
    menus = page.locator(MENU)
    require(menus.filter(visible=True).count() == 0)
    header = page.locator(HEADER)
    require(header.count() == 1 and header.is_visible() and header.is_enabled())
    require(header.evaluate(INSPECT, [expected, True]))
    checkpoint()
    require(header.evaluate(INSPECT, [expected, True]))
    click_once(header)
    menu = menus.filter(visible=True)
    menu.wait_for(state="visible", timeout=TIMEOUT)
    checkpoint()
    require(menu.count() == 1 and menu.evaluate(INSPECT, [expected, False]))
    root = menu.locator(CARD).nth(positions[0])
    group = root.locator(":scope > .card-content > .level2-menu").nth(positions[1])
    leaf = (
        group.locator(":scope > .level2-link")
        if len(labels) == 2
        else group.locator(":scope > .level3-menu")
        .nth(positions[2])
        .locator(':scope > .mib-obvious.card-sub-title[role="button"]')
    )
    checkpoint()
    require(menu.count() == 1 and menu.evaluate(INSPECT, [expected, False]))
    require(leaf.count() == 1 and leaf.is_visible() and leaf.is_enabled())
    click_once(leaf)
