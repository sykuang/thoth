"""Offline replacement provenance: real bank producers and the parent's safe sink."""
from datetime import date
from importlib import import_module
from types import SimpleNamespace

import pytest

from backend.core.base import (_failure_diagnostics, _format_failure_diagnostics,
                               _collect_failure_diagnostics, _format_collect_diagnostics)
from backend.core.error_codes import exception_error_id, format_error_id, with_error_code


CLASSES = {"ubot": "Ubot", "taishin": "Taishin", "rakuten": "Rakuten",
           "dbs": "Dbs", "scb": "Scb", "linebank": "Linebank"}


def crawler_for(bank):
    module = import_module(f"backend.banks.{bank}")
    crawler = object.__new__(getattr(module, CLASSES[bank] + "Crawler"))
    crawler.name = bank
    crawler.creds = SimpleNamespace(national_id="synthetic-id", username="synthetic-user",
        user_code="synthetic-user", password="synthetic-password")
    return module, crawler


def assert_sink(crawler, error, expected):
    assert exception_error_id(error, bank=crawler.name) == expected
    diagnostic = _failure_diagnostics(crawler, error, "03-900-001")
    assert diagnostic["error_id"] == expected
    message = _format_failure_diagnostics(diagnostic)
    assert message.startswith(format_error_id(expected, bank=crawler.name))
    assert any("\u4e00" <= char <= "\u9fff" for char in message)
    assert "PRIVATE" not in message
    collected = _collect_failure_diagnostics(crawler, error)
    assert collected["error_id"] == expected
    assert _format_collect_diagnostics(collected).startswith(format_error_id(expected))


class Field:
    def __init__(self, page, role, kind="text"):
        self.page, self.role, self.kind, self.value = page, role, kind, ""

    def action(self, operation):
        self.page.events.append((self.role, operation))
        if self.page.failure == (self.role, operation):
            raise RuntimeError("PRIVATE operation failure")

    def count(self):
        self.action("count")
        return 1

    def nth(self, index):
        self.action("nth")
        return self

    def is_visible(self):
        self.action("visible")
        return self.role != "captcha-image"

    def is_enabled(self):
        self.action("enabled")
        return True

    def click(self, **kwargs):
        self.action("doubleclick" if kwargs.get("click_count") == 3 else "click")
        if self.role == "submit":
            self.page.simulated_submits += 1
            return  # Synthetic boundary only; no browser, request or real form exists.
        self.page.current = self

    def input_value(self):
        self.action("read")
        return self.value

    def press(self, key):
        self.action("press")
        if key == "Backspace":
            self.value = ""

    def press_sequentially(self, value, **kwargs):
        self.action("type")
        self.value = value

    def get_attribute(self, name):
        self.action("attribute")
        if self.role == "submit" and name == "class":
            return "b-bg-green-d"
        return {"type": self.kind, "name": "__reCaptcha" if self.role == "captcha" else self.role,
                "maxlength": "12"}.get(name)

    def inner_text(self):
        self.action("text")
        return "登入"

    def bounding_box(self):
        return {"y": ["id", "user", "password", "captcha"].index(self.role)}


class Page:
    def __init__(self, module, bank, failure):
        self.failure, self.events, self.current = failure, [], None
        self.simulated_submits = 0
        self.url = "https://synthetic.invalid/"
        self.fields = {role: Field(self, role, kind) for role, kind in
            (("id", "text"), ("user", "password"), ("password", "password"),
             ("captcha", "tel"), ("submit", "submit"), ("captcha-image", "img"))}
        self.selectors = {
            "dbs": {"#username": "user", "#password": "password", "#loginbutton": "submit"},
            "linebank": {"#nationalId": "id", "#userId": "user", "#pw": "password"},
            "rakuten": {"#custNo": "id", "#userNo": "user", "#pcode": "password",
                        "#captcha": "captcha", "captcha-image img": "captcha-image"},
        }.get(bank, {})
        if bank == "ubot":
            self.selectors = {module.SEL_SID: "id", module.SEL_NICK: "user",
                              module.SEL_PWD: "password", module.SEL_CAPTCHA: "captcha"}
        if bank == "taishin":
            self.selectors = {module._ph_sel(module.FIELD_PLACEHOLDERS[key]): role for key, role in
                (("national_id", "id"), ("user_code", "user"), ("password", "password"), ("captcha", "captcha"))}
        self.keyboard = SimpleNamespace(press=self.press, type=self.type)
        self.frames, self.main_frame = [], self
        self.selectors.update({"button": "submit", "button[type='submit']": "submit"})
        if bank == "taishin":
            self.selectors[f"#{module.LOGIN_BTN_ID}"] = "submit"

    def locator(self, selector):
        if selector == "input":
            return SimpleNamespace(count=lambda: 4, nth=lambda index:
                self.fields[["id", "user", "password", "captcha"][index]])
        field = self.fields[self.selectors[selector]]
        field.action("locator")
        return field

    def wait_for_selector(self, *args, **kwargs):
        pass

    def wait_for_timeout(self, *args):
        if self.failure == ("post", "wait") and self.simulated_submits:
            raise RuntimeError("PRIVATE post-submit wait")
        if self.failure == ("prepare", "wait") and args == (8000,):
            raise RuntimeError("PRIVATE prepare wait")

    def goto(self, *args, **kwargs):
        pass

    def press(self, key):
        self.current.press(key)

    def type(self, value, **kwargs):
        self.current.press_sequentially(value, **kwargs)


# Literal field/operation IDs, not generated from the production catalog or AST.
CREDENTIAL_CASES = [
    ("ubot", "id", "click", "UBOT-02-035-001"),
    ("ubot", "id", "type", "UBOT-02-050-001"),
    ("ubot", "id", "read", "UBOT-02-060-001"),
    ("ubot", "user", "click", "UBOT-02-135-001"),
    ("ubot", "user", "type", "UBOT-02-150-001"),
    ("ubot", "user", "read", "UBOT-02-160-001"),
    ("ubot", "password", "click", "UBOT-02-235-001"),
    ("ubot", "password", "type", "UBOT-02-250-001"),
    ("ubot", "password", "read", "UBOT-02-260-001"),
    ("taishin", "id", "click", "TAISHIN-02-040-001"),
    ("taishin", "id", "type", "TAISHIN-02-043-001"),
    ("taishin", "id", "read", "TAISHIN-02-045-001"),
    ("taishin", "user", "click", "TAISHIN-02-055-001"),
    ("taishin", "user", "type", "TAISHIN-02-058-001"),
    ("taishin", "user", "read", "TAISHIN-02-060-001"),
    ("taishin", "password", "click", "TAISHIN-02-070-001"),
    ("taishin", "password", "type", "TAISHIN-02-073-001"),
    ("taishin", "password", "read", "TAISHIN-02-075-001"),
    ("dbs", "user", "click", "DBS-02-025-001"),
    ("dbs", "user", "type", "DBS-02-031-001"),
    ("dbs", "user", "read", "DBS-02-033-001"),
    ("dbs", "password", "click", "DBS-02-043-001"),
    ("dbs", "password", "type", "DBS-02-049-001"),
    ("dbs", "password", "read", "DBS-02-051-001"),
    ("scb", "id", "click", "SCB-02-017-001"),
    ("scb", "id", "type", "SCB-02-023-001"),
    ("scb", "id", "read", "SCB-02-025-001"),
    ("scb", "user", "click", "SCB-02-027-001"),
    ("scb", "user", "type", "SCB-02-033-001"),
    ("scb", "user", "read", "SCB-02-035-001"),
    ("scb", "password", "click", "SCB-02-037-001"),
    ("scb", "password", "type", "SCB-02-043-001"),
    ("scb", "password", "read", "SCB-02-045-001"),
    ("linebank", "id", "click", "LINEBANK-02-013-001"),
    ("linebank", "id", "type", "LINEBANK-02-019-001"),
    ("linebank", "id", "read", "LINEBANK-02-021-001"),
    ("linebank", "user", "click", "LINEBANK-02-027-001"),
    ("linebank", "user", "type", "LINEBANK-02-033-001"),
    ("linebank", "user", "read", "LINEBANK-02-035-001"),
    ("linebank", "password", "click", "LINEBANK-02-041-001"),
    ("linebank", "password", "type", "LINEBANK-02-047-001"),
    ("linebank", "password", "read", "LINEBANK-02-049-001"),
]


@pytest.mark.parametrize("bank,role,operation,expected", CREDENTIAL_CASES)
def test_credential_actions_survive_actual_login_replacement(bank, role, operation, expected):
    module, crawler = crawler_for(bank)
    page = Page(module, bank, (role, operation))
    if bank == "taishin":
        crawler._find_login_frame = lambda _: page
    custom_error = getattr(module, CLASSES[bank] + "LoginError")
    with pytest.raises(custom_error) as caught:
        crawler.submit_credentials_once(page)
    assert type(caught.value) is custom_error
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert type(caught.value.__context__) is RuntimeError
    assert str(caught.value) in {"登入欄位無法安全填寫；未送出登入", "登入欄位無法安全填寫；未送出"}
    assert_sink(crawler, caught.value, expected)
    assert not any(role == "submit" for role, _ in page.events)


@pytest.mark.parametrize("mutation,expected", [
    ("method", "TAISHIN-05-058-009"),
    ("status", "TAISHIN-05-058-010"),
    ("money", "TAISHIN-05-075-002"),
    ("overflow", "TAISHIN-05-076-001"),
    ("direction", "TAISHIN-05-063-004"),
    ("date", "TAISHIN-05-073-001"),
])
def test_exact_taishin_helper_failure_survives_real_parent_wrapper(mutation, expected):
    from tests.test_taishin_history_sync import ACCOUNT, _history_hit
    _, crawler = crawler_for("taishin")
    hit = _history_hit()
    row = hit.resp_json["OUTPUTDATA"]["userList"][0]
    if mutation == "method":
        hit.method = "GET"
    elif mutation == "status":
        hit.status = 503
    elif mutation == "money":
        row["txnamt"] = "bad-money"
    elif mutation == "overflow":
        row["newbal"] = "2147483648"
    elif mutation == "direction":
        row["txnamtIn"] = "6"
    elif mutation == "date":
        row["dateNew"] = "20260230"
    with pytest.raises(RuntimeError, match="^taishin-twd-history-response$") as caught:
        crawler._validate_history_hit(hit, identity=ACCOUNT,
            start=date(2025, 9, 2), end=date(2026, 9, 2), boundary=0)
    assert type(caught.value) is RuntimeError
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert_sink(crawler, caught.value, expected)


@pytest.mark.parametrize("mutation,expected", [
    ("shape", "RAKUTEN-05-053-002"),
    ("busy", "RAKUTEN-05-053-030"),
    ("pager", "RAKUTEN-05-053-029"),
])
def test_exact_rakuten_dom_failure_survives_real_parent_wrapper(mutation, expected):
    from tests.test_rakuten_history_sync import _history_result
    _, crawler = crawler_for("rakuten")
    result = _history_result()
    if mutation == "shape":
        del result["dom"]["headers"]
    else:
        result["dom"][mutation] = 1
    with pytest.raises(RuntimeError, match="^rakuten-twd-history-result$") as caught:
        crawler._validated_history_result(result)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert_sink(crawler, caught.value, expected)


def test_exact_rakuten_month_helper_retains_its_closed_reason():
    _, crawler = crawler_for("rakuten")
    with pytest.raises(RuntimeError, match="^rakuten-twd-history-months$") as caught:
        crawler._history_plan("01234567890123", ["2026/09 活存明細"], date(2026, 9, 2))
    assert_sink(crawler, caught.value, "RAKUTEN-05-068-001")


def ready_login(bank):
    module, crawler = crawler_for(bank)
    crawler._logged_in = lambda _: False
    crawler._ocr_captcha = lambda *a, **kw: "123456"
    crawler._ocr_with_regen = lambda *a, **kw: "123456"
    crawler._visible_alert_state = lambda _: (False, False, False)
    crawler._semantic_modal_visible_in_owned_scope = lambda _: False
    page = Page(module, bank, None)
    if bank == "taishin":
        crawler._find_login_frame = lambda _: page
    return module, crawler, page


@pytest.mark.parametrize("bank,role,operation,expected", [
    ("ubot", "captcha", "click", "UBOT-02-416-001"),
    ("ubot", "captcha", "type", "UBOT-02-419-001"),
    ("ubot", "captcha", "read", "UBOT-02-421-001"),
    ("ubot", "submit", "attribute", "UBOT-02-422-001"),
    ("ubot", "submit", "click", "UBOT-02-423-001"),
    ("taishin", "captcha", "type", "TAISHIN-02-016-001"),
    ("taishin", "submit", "visible", "TAISHIN-02-026-001"),
    ("taishin", "submit", "click", "TAISHIN-02-030-001"),
    ("dbs", "submit", "visible", "DBS-02-010-001"),
    ("dbs", "submit", "click", "DBS-02-015-001"),
    ("dbs", "post", "wait", "DBS-03-001-001"),
    ("scb", "submit", "click", "SCB-02-015-001"),
    ("scb", "post", "wait", "SCB-03-001-001"),
    ("linebank", "submit", "visible", "LINEBANK-02-700-001"),
    ("linebank", "submit", "click", "LINEBANK-02-007-001"),
])
def test_other_source_replacements_keep_the_actual_action(bank, role, operation, expected):
    module, crawler, page = ready_login(bank)
    page.failure = (role, operation)
    with pytest.raises(getattr(module, CLASSES[bank] + "LoginError")) as caught:
        crawler.submit_credentials_once(page)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert type(caught.value.__context__) is RuntimeError
    assert_sink(crawler, caught.value, expected)
    assert page.simulated_submits == (1 if role == "post" else 0)


@pytest.mark.parametrize("method,expected", [
    ("prepare_login_page", "SCB-01-005-001"),
    ("prepare_captcha_resubmit", "SCB-02-078-001"),
])
def test_scb_prepare_source_wrappers(method, expected):
    module, crawler, page = ready_login("scb")
    if method == "prepare_login_page":
        page.failure = ("prepare", "wait")
    else:
        page.selectors["button, a"] = "submit"
        page.failure = ("submit", "locator")
    with pytest.raises(module.ScbLoginError) as caught:
        getattr(crawler, method)(page)
    assert_sink(crawler, caught.value, expected)
    assert page.simulated_submits == 0


@pytest.mark.parametrize("modal", [True, False])
def test_independent_rakuten_modal_terminal_does_not_inherit_failed_click(modal):
    module, crawler = crawler_for("rakuten")
    crawler._semantic_modal_visible_in_owned_scope = lambda _: modal
    page = Page(module, "rakuten", ("deposit", "click"))
    deposit = Field(page, "deposit")
    page.get_by_role = lambda *a, **kw: SimpleNamespace(first=deposit)
    expected_type = module.RakutenLoginError if modal else RuntimeError
    with pytest.raises(expected_type) as caught:
        crawler._goto_twd(page)
    assert_sink(crawler, caught.value, "RAKUTEN-05-003-001" if modal else "RAKUTEN-05-002-001")
    assert page.events == [("deposit", "click")]
    if modal:
        assert caught.value.__cause__ is None and caught.value.__suppress_context__
    else:
        assert str(caught.value) == "PRIVATE operation failure"


@pytest.mark.parametrize("root,expected", [
    ("simple-dropdown2", "RAKUTEN-05-029-020"),
    ("simple-dropdown3", "RAKUTEN-05-029-021"),
])
def test_rakuten_existing_explicit_cause_still_wins(root, expected):
    from contextlib import nullcontext
    module, crawler = crawler_for("rakuten")
    page = Page(module, "rakuten", None)
    crawler._open_dropdown = lambda *a: None
    options = page.fields["submit"]
    crawler._selection_options = lambda *a: (options, False)
    page.selectors[f"{root} .dropdown-menu a.dropdown-item:visible"] = "submit"
    crawler._twd_view_state = lambda *a: {"rows": ""}
    collector = SimpleNamespace(request_sequence=0, issued_count=lambda _: 0)
    request = SimpleNamespace(response=lambda: None)
    page.expect_request = lambda *a, **kw: nullcontext(SimpleNamespace(value=request))
    with pytest.raises(RuntimeError, match="樂天臺幣查詢 request 未成功") as caught:
        crawler._select_label(page, collector, root, "登入")
    assert type(caught.value.__cause__) is RuntimeError
    assert_sink(crawler, caught.value, expected)
    assert page.simulated_submits == 1  # Synthetic option click, not a login form.


@pytest.mark.parametrize("empty", [True, False])
def test_validated_helper_success_and_explicit_empty_still_recover(empty):
    from tests.test_rakuten_history_sync import _history_result
    from tests.test_taishin_history_sync import ACCOUNT, _history_hit
    _, rakuten = crawler_for("rakuten")
    _, taishin = crawler_for("taishin")
    # A handled source failure must not contaminate a subsequent successful validator.
    try:
        rakuten._validated_history_result({})
    except RuntimeError:
        result = rakuten._validated_history_result(_history_result(empty=empty))
        assert result["status"] == ("explicit_empty" if empty else "complete")
        hit = _history_hit()
        if empty:
            hit.resp_json["OUTPUTDATA"]["userList"] = []
        result = taishin._validate_history_hit(hit, identity=ACCOUNT,
            start=date(2025, 9, 2), end=date(2026, 9, 2), boundary=0)
        assert result["row_count"] == (0 if empty else 1)


@pytest.mark.parametrize("source_id,expected", [
    (None, "TAISHIN-02-001-001"),
    ("DBS-02-001-001", "TAISHIN-02-001-001"),
])
def test_untagged_or_wrong_bank_frame_failure_keeps_old_fallback(source_id, expected):
    module, crawler = crawler_for("taishin")
    def fail(_):
        raise with_error_code(ValueError("PRIVATE source"), source_id)
    crawler._find_login_frame = fail
    with pytest.raises(module.TaishinLoginError) as caught:
        crawler.submit_credentials_once(None)
    assert_sink(crawler, caught.value, expected)


@pytest.mark.parametrize("bank,role,expected", [
    ("ubot", "id", "UBOT-02-015-002"),
    ("ubot", "user", "UBOT-02-115-002"),
    ("ubot", "password", "UBOT-02-215-002"),
])
def test_ubot_source_custom_login_rejection_is_not_collapsed(bank, role, expected):
    module, crawler, page = ready_login(bank)
    page.fields[role].count = lambda: 2
    with pytest.raises(module.UbotLoginError) as caught:
        crawler.submit_credentials_once(page)
    assert type(caught.value.__context__) is module.UbotLoginError
    assert_sink(crawler, caught.value, expected)
    assert page.simulated_submits == 0


@pytest.mark.parametrize("bank", ["ubot", "taishin", "linebank"])
def test_existing_handled_poll_failure_returns_without_retry(bank):
    _, crawler, page = ready_login(bank)
    page.failure = ("post", "wait")
    assert crawler.submit_credentials_once(page) is None
    assert page.simulated_submits == 1


def test_scb_recovered_prepare_failure_does_not_become_a_later_terminal():
    module, crawler, page = ready_login("scb")
    calls = []
    def goto(*a, **kw):
        calls.append(a[0])
        if len(calls) == 1:
            raise RuntimeError("PRIVATE handled preparation")
    page.goto = goto
    assert crawler.prepare_login_page(page) is None
    assert len(calls) == 2
    page.failure = ("password", "read")
    with pytest.raises(module.ScbLoginError) as caught:
        crawler.submit_credentials_once(page)
    assert_sink(crawler, caught.value, "SCB-02-045-001")


@pytest.mark.parametrize("role,operation,expected", [
    ("id", "click", "RAKUTEN-02-013-001"),
    ("id", "type", "RAKUTEN-02-016-001"),
    ("user", "click", "RAKUTEN-02-019-001"),
    ("user", "type", "RAKUTEN-02-022-001"),
    ("password", "click", "RAKUTEN-02-025-001"),
    ("password", "type", "RAKUTEN-02-028-001"),
])
def test_rakuten_unwrapped_credentials_preserve_old_runtimeerror(role, operation, expected):
    _, crawler, page = ready_login("rakuten")
    page.failure = (role, operation)
    with pytest.raises(RuntimeError, match="PRIVATE operation failure") as caught:
        crawler.submit_credentials_once(page)
    assert type(caught.value) is RuntimeError
    assert_sink(crawler, caught.value, expected)
    assert page.simulated_submits == 0


@pytest.mark.parametrize("which,expected", [
    ("dom", "RAKUTEN-05-058-001"),
    ("months", "RAKUTEN-05-012-001"),
    ("transport", "TAISHIN-05-059-001"),
    ("money", "TAISHIN-05-065-001"),
])
@pytest.mark.parametrize("foreign", [False, True])
def test_changed_helper_wrappers_keep_old_fallback_without_owned_source(monkeypatch, which, expected, foreign):
    from tests.test_rakuten_history_sync import _history_result
    from tests.test_taishin_history_sync import ACCOUNT, _history_hit
    bank = "rakuten" if which in {"dom", "months"} else "taishin"
    module, crawler = crawler_for(bank)
    def fail(*a, **kw):
        raise with_error_code(RuntimeError("PRIVATE source"), "DBS-02-001-001" if foreign else None)
    if which == "dom":
        monkeypatch.setattr(type(crawler), "_validate_history_dom", staticmethod(fail))
        action = lambda: crawler._validated_history_result(_history_result())
    elif which == "months":
        monkeypatch.setattr(module, "_six_month_labels", fail)
        action = lambda: crawler._history_plan(ACCOUNT, ["2026/09 活存明細"], date(2026, 9, 2))
    else:
        monkeypatch.setattr(type(crawler), "_validate_history_transport" if which == "transport" else "_history_money", staticmethod(fail))
        action = lambda: crawler._validate_history_hit(_history_hit(), identity=ACCOUNT,
            start=date(2025, 9, 2), end=date(2026, 9, 2), boundary=0)
    with pytest.raises(RuntimeError) as caught:
        action()
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert_sink(crawler, caught.value, expected)


@pytest.mark.parametrize("which,expected", [
    ("taishin-date", "TAISHIN-05-073-001"),
    ("taishin-url", "TAISHIN-05-057-001"),
    ("taishin-digest", "TAISHIN-05-078-001"),
    ("taishin-binding", "TAISHIN-05-070-001"),
    ("rakuten-date", "RAKUTEN-05-059-001"),
    ("rakuten-time", "RAKUTEN-05-062-001"),
    ("ubot-date", "UBOT-07-402-001"),
    ("ubot-form", "UBOT-05-428-001"),
    ("ubot-time", "UBOT-05-429-001"),
])
def test_local_stdlib_conversion_predicates_keep_specific_old_terminal(which, expected):
    from tests.test_rakuten_history_sync import _history_result
    from tests.test_taishin_history_sync import ACCOUNT, _history_hit, _result_snapshot
    from tests.test_ubot_history_sync import ACCOUNT as UBOT_ACCOUNT, _history_hit as ubot_hit
    bank = which.split("-")[0]
    _, crawler = crawler_for(bank)
    if which == "taishin-date":
        action = lambda: crawler._normalize_history_cells(
            ["2026/02/30 12:00:00", "2026/09/01", "利息", "5", "105", "", ""],
            identity=ACCOUNT, start=date(2025, 9, 2), end=date(2026, 9, 2))
    elif which == "taishin-url":
        hit = _history_hit()
        transport = crawler._validate_history_hit(hit, identity=ACCOUNT,
            start=date(2025, 9, 2), end=date(2026, 9, 2), boundary=0)["transport"]
        transport["url"] = "https://[invalid/"
        action = lambda: crawler._validate_history_transport(transport, identity=ACCOUNT,
            start=date(2025, 9, 2), end=date(2026, 9, 2))
    elif which == "taishin-digest":
        action = lambda: crawler._history_rows_digest([{"bad": object()}])
    elif which == "taishin-binding":
        action = lambda: crawler._validate_history_snapshot(_result_snapshot(), identity=ACCOUNT,
            period="12_months", start=date(2025, 9, 2), end=date(2026, 9, 2),
            api_row_count=1, api_rows=[{"bad": object()}])
    elif which.startswith("rakuten"):
        result = _history_result()
        if which == "rakuten-date":
            result["receipt"]["start"] = "2026-02-30"
        else:
            result["txDetails"][0]["sysTime"] = "25:00:00"
        action = lambda: crawler._validated_history_result(result)
    elif which == "ubot-date":
        action = lambda: crawler._strict_slash_date("2026/02/30", "local-date")
    else:
        hit = ubot_hit()
        if which == "ubot-form":
            hit.req_body = "malformed-form"
        else:
            hit.resp_json["RespBody"]["NTDetailList"][0]["TraTime"] = "25:00:00"
        action = lambda: crawler._validate_history_hit(hit, identity=UBOT_ACCOUNT,
            start=date(2026, 7, 1), end=date(2026, 7, 31), after_sequence=0)
    with pytest.raises(RuntimeError) as caught:
        action()
    assert type(caught.value) is RuntimeError
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert_sink(crawler, caught.value, expected)


def test_ubot_decimal_conversion_fallback_is_not_speculatively_reclassified(monkeypatch):
    from decimal import InvalidOperation
    module, crawler = crawler_for("ubot")
    def failed_decimal(value):
        raise InvalidOperation("PRIVATE synthetic decimal boundary")
    monkeypatch.setattr(module, "Decimal", failed_decimal)
    with pytest.raises(RuntimeError, match="^amount$") as caught:
        crawler._strict_twd_amount("1", "amount")
    assert_sink(crawler, caught.value, "UBOT-05-427-001")


@pytest.mark.parametrize("explicit,expected", [
    (False, "UBOT-02-050-001"),
    (True, "UBOT-02-060-001"),
])
def test_actual_replacement_preserves_current_priority_and_explicit_cause(explicit, expected):
    module, crawler, page = ready_login("ubot")
    def click(**kwargs):
        try:
            raise with_error_code(ValueError("PRIVATE previous failure"), "UBOT-02-060-001")
        except ValueError as previous:
            current = with_error_code(RuntimeError("PRIVATE current operation"), "UBOT-02-050-001")
            if explicit:
                raise current from previous
            raise current
    page.fields["id"].click = click
    with pytest.raises(module.UbotLoginError) as caught:
        crawler.submit_credentials_once(page)
    assert_sink(crawler, caught.value, expected)
    assert page.simulated_submits == 0
