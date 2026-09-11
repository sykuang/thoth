"""Offline stage boundaries: real adapters, synthetic browser failures only."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.banks.dbs import DbsCrawler
from backend.banks.linebank import LinebankCrawler
from backend.banks.scb import ScbCrawler
from backend.banks.scsb import ScsbCrawler
from backend.core.base import _safe_collect_guard

BANKS = (DbsCrawler, LinebankCrawler, ScbCrawler, ScsbCrawler)


def crawler(cls):
    obj = object.__new__(cls)
    obj.creds = SimpleNamespace(national_id="x", username="x", user_code="x", password="x")
    obj._diagnostic_stage = "unknown"
    obj._logged_in = lambda page: True
    return obj


def login_page(obj, failure):
    page = Mock()
    page.frames = []
    button = Mock()
    button.inner_text.return_value = "登入"
    button.get_attribute.return_value = "b-bg-green-d"
    fields = []
    for index, kind in enumerate(("text", "password", "password", "tel")):
        field = Mock()
        field.input_value.return_value = "x"
        field.bounding_box.return_value = {"y": index}
        field.get_attribute.side_effect = lambda key, kind=kind: {
            "type": kind, "name": "__reCaptcha", "maxlength": "12"}.get(key)
        fields.append(field)
    def locate(selector):
        if selector.startswith("button") or selector == "#loginbutton":
            if failure == "login_button":
                raise RuntimeError("synthetic")
            items = [button]
        else:
            if failure == "login_field":
                raise RuntimeError("synthetic")
            items = fields if selector == "input" else [fields[0]]
        group = Mock()
        group.count.return_value = len(items)
        group.nth.side_effect = items.__getitem__
        group.input_value.return_value = "x"
        return group
    page.locator.side_effect = locate
    if failure == "login_submit":
        button.click.side_effect = RuntimeError("synthetic")
    if failure == "login_postconfirm":
        obj._logged_in = Mock(side_effect=RuntimeError("synthetic"))
    if isinstance(obj, (ScbCrawler, ScsbCrawler)):
        obj._ocr_captcha = Mock(return_value=None if failure == "login_ocr" else "x")
    if isinstance(obj, ScbCrawler):
        obj._visible_alert_state = lambda page: (False, False, False)
    return page, button


@pytest.mark.parametrize("cls", BANKS)
@pytest.mark.parametrize("stage", ["login_field", "login_button", "login_submit", "login_postconfirm"])
def test_login_failure_boundary(cls, stage):
    obj = crawler(cls)
    page, button = login_page(obj, stage)
    try:
        obj.submit_credentials_once(page)
    except RuntimeError:
        pass
    expected = 'login_field_national_id_count' if cls is ScsbCrawler and stage == 'login_field' else stage
    assert obj._diagnostic_stage == expected
    assert button.click.call_count == (1 if stage in {"login_submit", "login_postconfirm"} else 0)


@pytest.mark.parametrize("cls", [ScbCrawler, ScsbCrawler])
def test_returned_ocr_failure_never_advances_to_submission(cls):
    obj = crawler(cls)
    page, button = login_page(obj, "login_ocr")
    with pytest.raises(RuntimeError):
        obj.submit_credentials_once(page)
    assert obj._diagnostic_stage == "login_ocr"
    button.click.assert_not_called()


@pytest.mark.parametrize("cls", [ScbCrawler, ScsbCrawler])
def test_real_ocr_swallowed_failure_stage(cls):
    obj = crawler(cls)
    page = Mock()
    page.evaluate.side_effect = RuntimeError("synthetic")
    page.locator.side_effect = RuntimeError("synthetic")
    assert obj._ocr_captcha(page, max_attempts=1) is None
    assert obj._diagnostic_stage == "login_ocr"


@pytest.mark.parametrize("cls", BANKS)
def test_collect_entry_failure(cls):
    obj = crawler(cls)
    page = Mock()
    page.wait_for_timeout.side_effect = RuntimeError("synthetic")
    with pytest.raises(RuntimeError):
        obj.collect(page, Mock())
    assert obj._diagnostic_stage == "collect"
    page.wait_for_timeout.assert_called_once()


@pytest.mark.parametrize("cls,needle,stage", [
    (DbsCrawler, "({acctName, acctTail})", "collect_navigation"),
    (DbsCrawler, "slice(0, 40000)", "collect_accounts"),
    (DbsCrawler, "(wanted)", "collect_transactions"),
    (DbsCrawler, "slice(0, 20000)", "collect_cards"),
    (ScbCrawler, "const t = (el.textContent", "collect_navigation"),
    (ScbCrawler, "slice(0, 20000)", "collect_cards"),
    (LinebankCrawler, "const sel = document.querySelector('select')", "collect_accounts"),
    (LinebankCrawler, "for (const b of", "collect_transactions"),
])
def test_collect_injected_failures_keep_best_effort(cls, needle, stage, monkeypatch, tmp_path):
    import importlib
    module = importlib.import_module(cls.__module__)
    monkeypatch.setattr(module, "_debug_dir", lambda: tmp_path)
    obj = crawler(cls)
    page = Mock(url="https://synthetic.invalid/")
    collector = Mock(hits=[])
    collector.latest.return_value = None
    observed = []
    def evaluate(script, *args):
        if needle in script:
            observed.append(obj._diagnostic_stage)
            raise RuntimeError("synthetic")
        if "slice(" in script:
            return ""
        if "return {opts}" in script:
            return {"opts": [{"value": "x"}]}
        if "({acctName, acctTail})" in script or "(wanted)" in script:
            return {"clicked": False}
        if "found: false" in script:
            return {"found": False}
        if "其他月份" in script and "clicked" in script:
            return {"clicked": False}
        return []
    page.evaluate.side_effect = evaluate
    try:
        result = obj.collect(page, collector)
    except RuntimeError:
        # SCB's top-level navigation was already fatal before instrumentation.
        assert cls is ScbCrawler and stage == "collect_navigation"
    else:
        assert result is not None
    assert observed and set(observed) == {stage}


def test_scsb_nested_navigation_guard():
    obj = crawler(ScsbCrawler)
    page = Mock()
    page.evaluate.return_value = {"ok": False}
    with pytest.raises(RuntimeError) as raised:
        obj._collect_twd_inquiry(page, set())
    assert obj._diagnostic_stage == "collect_navigation"
    assert _safe_collect_guard(raised.value, ScsbCrawler.SAFE_COLLECT_GUARDS) == "twd-inquiry-navigation-failed"
    assert _safe_collect_guard(RuntimeError("untrusted-period-error"), ScsbCrawler.SAFE_COLLECT_GUARDS) is None
