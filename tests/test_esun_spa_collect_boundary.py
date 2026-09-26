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
    assert result["error"] == "collect_failed: ValueError: code=collect_adapter"
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
