"""Short official timelines continue on window scroll, not element scroll."""
import ast
import inspect
from backend.banks.esun_spa.collection import continue_twd_once
from tests.test_esun_spa_product import browser as browser


def test_short_timeline_uses_document_scroll_geometry(browser):
    tree = ast.parse(inspect.getsource(continue_twd_once))
    script = next(n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                  and isinstance(n.value, ast.Constant)
                  and any(isinstance(t, ast.Name) and t.id == 'geometry_script' for t in n.targets))
    context = browser.new_context(viewport={'width': 1200, 'height': 900})
    try:
        page = context.new_page()
        page.set_content('<div id="layout-content"><div class="ctw01002">'
                         '<div class="timeline-query-continer" style="height:600px">Synthetic</div>'
                         '</div></div><div style="height:1000px"></div>')
        geometry = page.evaluate(script)
        assert geometry is not None and geometry[2] > 0
        page.mouse.move(*geometry[:2])
        page.mouse.wheel(0, geometry[2])
        page.wait_for_function('scrollY > 0')
        assert page.locator('.timeline-query-continer').evaluate('n=>n.scrollTop') == 0
    finally:
        context.close()


def test_error_footer_is_not_a_transaction_group(browser):
    import pytest
    from backend.banks.esun_spa.collection import require_rendered_occurrences
    context = browser.new_context()
    try:
        page = context.new_page()
        page.set_content('''<div id="layout-content"><div class="ctw01002"><div class="timeline-query-continer">
        <div><div class="timeline-top-sub-container"><span class="timeline-year">2026</span><span class="timeline-month">09</span></div>
        <div class="timeline-card-outer-container"><div class="timeline-card-container"><div class="timeline-card-sub-container"><div class="timeline-card-title">SYNTHETIC</div></div></div></div></div>
        <div class="timeline-card-buttom"><span class="timeline-card-buttom-msg">Synthetic error</span></div>
        </div></div></div>''')
        groups = [{'year':'2026', 'month':'9', 'detailInfo':[{'detailTitle':'SYNTHETIC'}]}]
        with pytest.raises(ValueError):
            require_rendered_occurrences(page, groups)
        with pytest.raises(ValueError):
            require_rendered_occurrences(page, groups, error_message='Different error')
        require_rendered_occurrences(page, groups, error_message='Synthetic error')
    finally:
        context.close()
