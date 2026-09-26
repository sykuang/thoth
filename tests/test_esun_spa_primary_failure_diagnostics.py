"""Real loopback action timeouts must not be relabelled by failure-path probes."""

import json

import pytest
from patchright.sync_api import Locator, TimeoutError

from tests.test_esun_spa_product import PATHS, browser as browser, product as product


@pytest.fixture(autouse=True)
def synthetic_credentials_only(monkeypatch):
    monkeypatch.setattr("backend.core.creds._ENV_LOADED", True)


@pytest.mark.parametrize("action", ["header", "leaf", "calendar", "submit"])
@pytest.mark.parametrize("secondary", ["menu_plan", "guard"])
def test_primary_action_timeout_keeps_run_diagnostics(product, monkeypatch, capsys, action, secondary):
    crawler, page, origin, hits, external, captured, _, logout, submit = product
    selector = {
        "header": ".no-margin-right.header-option-button",
        "leaf": ".level2-link",
        "calendar": ".calendar-base .combo-input-wrapper[role=button]",
        "submit": "button[type=submit].btn-main-border",
    }[action]
    phase = "destination_navigation" if action in ("header", "leaf") else "transaction_form"
    click = Locator.click
    collect = crawler.collect
    attempts, failures, escaped = [], [], []

    def obstructed_click(target, *args, **kwargs):
        attempts.append(target)
        assert not failures, "no action or retry after the primary failure"
        if not target.evaluate("(n, selector) => n.matches(selector)", selector):
            return click(target, *args, **kwargs)
        # Obstruct only after production pre-action checks; use a real native timeout.
        target.evaluate("""n => {
            const cover = document.createElement('div');
            cover.style = 'position:fixed;inset:0;z-index:2147483647';
            (n.closest('dialog') || document.body).append(cover);
        }""")
        try:
            return click(target, *args, **dict(kwargs, timeout=100))
        except TimeoutError as error:
            failures.append((error, error.__cause__, error.__context__, len(attempts)))
            assert crawler._esun_spa_phase == phase and crawler._esun_spa_gate is None
            if secondary == "menu_plan":
                owner = crawler.collector._latest_spa[PATHS[0]][1]
                crawler.collector._on_request_failed(owner._native_request)
            else:
                page.evaluate("""() => document.body.insertAdjacentHTML(
                    'beforeend', '<div role="dialog">SYNTHETIC-HOSTILE-BLOCKER</div>')""")
            raise

    def observe_collect(*args):
        try:
            return collect(*args)
        except BaseException as error:
            escaped.append(error)
            raise

    monkeypatch.setattr(Locator, "click", obstructed_click)
    monkeypatch.setattr(crawler, "collect", observe_collect)
    result = crawler.run(origin + "/synthetic", headless=True)

    assert len(failures) == 1
    error, cause, context, attempt_count = failures[0]
    assert escaped == [error] and escaped[0] is error
    assert len(attempts) == attempt_count
    assert not captured and not external and "data" not in result
    assert [path for path, _ in hits] == list(PATHS[:1 if action in ("header", "leaf") else 2])
    submit.assert_called_once()
    logout.assert_called_once()  # Existing best-effort cleanup, not a collection retry.
    assert not crawler.collector.observers
    assert json.loads(page.locator("body").get_attribute("data-counts")) == {
        "login": 1, "header": int(action != "header"), "leaf": int(action not in ("header", "leaf")),
    }
    diagnostics = result["collect_diagnostics"]
    assert diagnostics["exception"] == "TimeoutError"
    assert diagnostics["phase"] == phase
    assert "gate" not in diagnostics and crawler._esun_spa_gate is None
    assert result["error"].startswith("collect_failed: TimeoutError: code=")
    assert f"phase={phase}" in result["error"] and "gate=" not in result["error"]
    assert error.__cause__ is cause and error.__context__ is context
    assert not any(text in repr(result) + capsys.readouterr().err for text in (
        "PRIVATE-", "SYNTHETIC-", "TEST-", "0000000000001", "Locator.click",
    ))
