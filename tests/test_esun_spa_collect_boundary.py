"""Product entry tests: unsupported SPA must not fall through to JSF or publish.

This is NOT a native-continuation or local-full-sync acceptance test.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.banks.esun import BASE, EsunCrawler
from backend.core.base import ResponseCollector



@pytest.fixture
def product(monkeypatch, tmp_path):
    # No constructor/credentials/browser/DB: exercise real collect/run boundaries.
    crawler = object.__new__(EsunCrawler)
    crawler.name = "esun"
    page = SimpleNamespace(
        url=BASE,
        frames=[],
        evaluate=Mock(return_value=True),
        wait_for_timeout=Mock(),
        on=Mock(),
        remove_listener=Mock(),
    )
    monkeypatch.setenv("BANK_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(crawler, "_navigate_menu", Mock(return_value={"frames": []}))
    return crawler, page, tmp_path


def test_spa_collect_requires_prelogin_capture_before_legacy_actions(product):
    crawler, page, root = product
    with pytest.raises(ValueError, match="pre-login capture"):
        crawler.collect(page, ResponseCollector(host_filter=crawler._host_filter()))
    page.wait_for_timeout.assert_not_called()
    crawler._navigate_menu.assert_not_called()
    assert not (root / "esun_collect").exists()


def test_product_run_keeps_strict_error_barrier_and_cleanup(product, monkeypatch):
    crawler, page, root = product
    monkeypatch.setattr(crawler, "_make_collector", lambda _: ResponseCollector())
    monkeypatch.setattr(crawler, "_enforce_session_freshness", lambda: None)
    monkeypatch.setattr(crawler, "_build_fetch_kwargs", lambda: {"__cleanups__": []})
    monkeypatch.setattr(crawler, "_shared_login", lambda _: True)  # Preauthenticated fake page only.
    monkeypatch.setattr(crawler, "logout", Mock(return_value=True))
    monkeypatch.setattr(crawler, "_execute_browser_flow", lambda _url, **kw: kw["page_action"](page))

    result = crawler.run(BASE, headless=True)
    assert "data" not in result
    assert result["error"] == "collect_failed: ValueError: code=collect_adapter: phase=spa_entry"
    assert crawler._diagnostic_stage == "collect_validation"
    crawler.logout.assert_called_once_with(page)
    assert {c.args[0] for c in page.remove_listener.call_args_list} == {"request", "requestfailed", "response"}
    assert crawler.collector._detached is True
    crawler._navigate_menu.assert_not_called()
    page.wait_for_timeout.assert_not_called()
    assert not (root / "esun_collect").exists()


def test_absent_spa_keeps_legacy_navigation(product):
    crawler, page, _ = product
    page.evaluate.return_value = False
    with pytest.raises(RuntimeError, match="^esun-twd-history$"):
        crawler.collect(page, ResponseCollector())
    crawler._navigate_menu.assert_called_once()


def test_spa_inspection_failure_does_not_fall_through(product):
    crawler, page, root = product
    page.evaluate.side_effect = RuntimeError("synthetic DOM unavailable")
    with pytest.raises(RuntimeError, match="^synthetic DOM unavailable$"):
        crawler.collect(page, ResponseCollector())
    crawler._navigate_menu.assert_not_called()
    page.wait_for_timeout.assert_not_called()
    assert not (root / "esun_collect").exists()


@pytest.mark.parametrize('failure, expected', [
    ('origin', 'origin'), ('snapshot', 'blocker_snapshot'),
    ('blocker', 'blocker_allowed'), ('visible_login', 'login_visible'),
])
def test_spa_navigation_guard_retains_native_failure_and_exact_gate(failure, expected):
    from backend.banks.esun_spa.collection import navigation_guard

    crawler = object.__new__(EsunCrawler)
    original = TimeoutError('private account data\nFORGED_LOG')
    crawler._ensure_collect_origin = Mock(side_effect=original if failure == 'origin' else None)
    crawler.login_checkpoint_rules = Mock(return_value=())
    locator = Mock()
    locator.evaluate_all.side_effect = original if failure == 'snapshot' else None
    locator.evaluate_all.return_value = [False, 'unknown', 'predicate'] if failure == 'blocker' else [True, 'unknown', 'none']
    locator.count.return_value = 1 if failure == 'visible_login' else 0
    page = SimpleNamespace(locator=Mock(return_value=locator))
    expected_type = TimeoutError if failure in ('origin', 'snapshot') else ValueError
    with pytest.raises(expected_type) as caught:
        navigation_guard(crawler, page)()
    if failure in ('origin', 'snapshot'):
        assert caught.value is original
    assert crawler._esun_spa_gate == expected
