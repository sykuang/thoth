"""Keep E.SUN in its native narrow-desktop layout, without guide acknowledgments."""
from backend.banks.esun import EsunCrawler
from backend.core.base import BankCrawler
from scrapling.engines._browsers._stealth import StealthySession


def test_esun_context_uses_native_narrow_desktop_viewport():
    crawler = object.__new__(EsunCrawler)
    kwargs = crawler._build_fetch_kwargs()
    cleanups = kwargs.pop('__cleanups__')
    try:
        session = StealthySession(**kwargs)
        assert session._context_options['viewport'] == {'width': 1200, 'height': 1800}
        assert session._context_options['is_mobile'] is False
    finally:
        for cleanup in cleanups:
            cleanup()


def test_viewport_preserves_inherited_fetch_options(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(BankCrawler, '_build_fetch_kwargs', lambda self: {
        'locale': 'zh-TW', 'init_script': sentinel,
        '__cleanups__': [sentinel], 'additional_args': {'service_workers': 'block'},
    })
    kwargs = object.__new__(EsunCrawler)._build_fetch_kwargs()
    assert kwargs['additional_args'] == {
        'service_workers': 'block', 'viewport': {'width': 1200, 'height': 1800},
    }
    assert kwargs['init_script'] is sentinel
    assert kwargs['__cleanups__'] == [sentinel]
    assert kwargs['locale'] == 'zh-TW'
    assert 'viewport' not in BankCrawler._build_fetch_kwargs(object.__new__(EsunCrawler))['additional_args']


def test_real_fetch_context_keeps_reminder_clickable_without_guide_ack(tmp_path):
    from tests.test_esun_password_notice import NOTICE, evaluate
    from backend.core.login_checkpoints import CheckpointKind

    crawler = object.__new__(EsunCrawler)
    crawler.session_dir = tmp_path / 'owned-profile'
    kwargs = crawler._build_fetch_kwargs()
    cleanups = kwargs.pop('__cleanups__')
    html = ('<meta charset="utf-8"><style>.driver-active *{pointer-events:none}</style>'
            '<body data-closed="0" data-changed="0">' + NOTICE +
            '<script>if(!matchMedia("(max-width: 1200px)").matches)'
            'document.body.classList.add("driver-active");</script></body>')
    def setup(page):
        page.context.route('**/*', lambda route: route.fulfill(
            content_type='text/html; charset=utf-8', body=html))
    observed = {}
    def callback(page):
        observed['width'] = page.evaluate('innerWidth')
        observed['guide_active'] = page.locator('body.driver-active').count() != 0
        observed['outcome'] = evaluate(page).kind
        observed['closed'] = page.locator('body').get_attribute('data-closed')
        observed['changed'] = page.locator('body').get_attribute('data-changed')
        return page
    kwargs.update(page_setup=setup, retries=1)
    try:
        crawler._execute_browser_flow('https://ebank.esunbank.com.tw/', headless=True,
                                      page_action=callback, fetch_kwargs=kwargs)
        assert observed == {'width': 1200, 'guide_active': False,
                            'outcome': CheckpointKind.DISMISSIBLE_NOTICE,
                            'closed': '1', 'changed': '0'}
    finally:
        for cleanup in cleanups:
            cleanup()
