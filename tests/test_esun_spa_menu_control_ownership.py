"""Synthetic native menu: unrelated controls must not block an owned target."""
import pytest
from patchright.sync_api import sync_playwright

from backend.banks.esun_spa.navigation import navigate_twd


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=[
            "--disable-background-networking",
            "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
        ])
        yield browser
        browser.close()


def menu(page, depth, extra=""):
    labels = ["Credit", "Statements"] + (["Bill"] if depth == 3 else [])
    leaf = labels[-1]
    target = f'<div class="{("mib-obvious card-sub-title" if depth == 3 else "level2-link")}" role="button" aria-label="{leaf} Open link" onclick="document.documentElement.dataset.targetClicks++">{leaf}</div>'
    children = f'<div class="card-sub-title">Statements</div><div class="level3-menu">{target}</div>' if depth == 3 else target
    page.set_content(f"""<body>
      <script>document.documentElement.dataset.targetClicks='0';document.documentElement.dataset.helpClicks='0'</script>
      <div id="mib-portal-header-component-container"><div class="mib-header"><div class="header-option">
      <div class="no-margin-right header-option-button" role="button" onclick="document.querySelector('dialog').showModal()">
      <div class="mib-font-bold services-button">All Features</div></div></div></div></div>
      <div class="mib-menu mib-modal"><dialog class="menu-modal-container menu-dialog">
      <div class="widget mib-txn-card level1-card" id="menu-tab-card-0">
      <div class="widget-label">Credit</div><div class="card-content"><div class="level2-menu">
      {children}{extra}</div></div></div></dialog></div></body>""")
    return dict(taskId="CCM01002", locale="en-US", labels=labels,
                positions=[0] * depth, counts=[1] * depth, openLink="Open link")


@pytest.mark.parametrize("depth", [2, 3])
def test_owned_leaf_click_ignores_unrelated_help_control(browser, depth):
    context = browser.new_context(viewport={"width": 1200, "height": 900})
    context.route("**/*", lambda route: route.abort())
    try:
        page = context.new_page()
        plan = menu(page, depth, '<button id="help" onclick="document.documentElement.dataset.helpClicks++">Help</button>')
        navigate_twd(page, plan, lambda: True, lambda: plan, target="CCM01002")
        assert page.evaluate("Number(document.documentElement.dataset.targetClicks)") == 1
        assert page.evaluate("Number(document.documentElement.dataset.helpClicks)") == 0
    finally:
        context.close()


@pytest.mark.parametrize("depth", [2, 3])
@pytest.mark.parametrize("mutation", ["nested_action", "duplicate_leaf", "wrong_label", "disabled_leaf"])
def test_ambiguous_or_unowned_leaf_still_rejects_before_dispatch(browser, depth, mutation):
    context = browser.new_context(viewport={"width": 1200, "height": 900})
    context.route("**/*", lambda route: route.abort())
    try:
        page = context.new_page()
        plan = menu(page, depth)
        leaf = page.locator(".level2-menu [role=button]")
        leaf.evaluate("""(el, mutation) => {
            if(mutation==='nested_action')el.innerHTML+='<button>nested</button>';
            if(mutation==='duplicate_leaf')el.after(el.cloneNode(true));
            if(mutation==='wrong_label')el.textContent='Other target';
            if(mutation==='disabled_leaf')el.setAttribute('aria-disabled','true');
        }""", mutation)
        with pytest.raises(ValueError, match="native menu rejected"):
            navigate_twd(page, plan, lambda: True, lambda: plan, target="CCM01002")
        assert page.evaluate("Number(document.documentElement.dataset.targetClicks)") == 0
    finally:
        context.close()
