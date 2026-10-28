"""Offline action diagnostics: fail real operations, never the code carrier."""
from copy import deepcopy
from datetime import date
from importlib import import_module
from unittest.mock import Mock

import pytest
from playwright.sync_api import TimeoutError as NativeTimeoutError

from backend.core.error_codes import code_details, exception_error_id


def _leaf(error, bank):
    code = exception_error_id(error, bank=bank)
    assert code is not None, f"unattributed {bank} operation"
    assert code.startswith(bank.upper() + "-")
    assert int(code.split("-")[2]) < 900
    assert code_details(code, bank=bank)
    return code


def _contains(error, original):
    seen = set()
    while error is not None and id(error) not in seen:
        if error is original:
            return True
        seen.add(id(error))
        error = error.__cause__ or error.__context__
    return False


def _login(bank):
    fixture = import_module(f"tests.test_{bank}_login_checkpoints")._submit_fixture
    values = fixture()
    crawler, page = values[:2]
    if bank == "sinopac":
        fields, submit = values[2], values[4]
    else:
        fields = [values[3][1], values[3][2], values[3][0], values[5]]
        submit = values[6]
    return crawler, page, fields, submit


@pytest.mark.parametrize("bank", ["sinopac", "fubon"])
def test_credential_roles_and_native_actions_are_distinct(bank):
    codes = []
    for role in range(4):
        for operation in ("click", "select", "erase", "type", "read"):
            crawler, page, fields, submit = _login(bank)
            original = RuntimeError("synthetic-operation-failure")
            if operation in ("click", "select"):
                target = fields[role].click
                target.side_effect = original if operation == "click" else [None, original]
            elif operation == "read":
                target = fields[role].input_value
                target.side_effect = original
            elif bank == "sinopac":
                target = page.keyboard.press if operation == "erase" else page.keyboard.type
                target.side_effect = [None] * role + [original]
            else:
                target = fields[role].press if operation == "erase" else fields[role].press_sequentially
                target.side_effect = original
            with pytest.raises(RuntimeError) as caught:
                crawler.submit_credentials_once(page)
            assert _contains(caught.value, original)
            codes.append(_leaf(caught.value, bank))
            submit.click.assert_not_called()
            assert target.call_count == (2 if operation == "select" else role + 1 if bank == "sinopac" and operation in ("erase", "type") else 1)
    assert len(set(codes)) == len(codes), "credential role/action collision"


@pytest.mark.parametrize("bank", ["sinopac", "fubon"])
def test_submit_and_postauth_native_failures_are_distinct(bank):
    codes = []
    for operation in ("submit", "settle", "auth"):
        crawler, page, fields, submit = _login(bank)
        original = RuntimeError("synthetic-operation-failure")
        target = {"submit": submit.click, "settle": page.wait_for_timeout, "auth": crawler._logged_in}[operation]
        target.side_effect = original
        with pytest.raises(RuntimeError) as caught:
            crawler.submit_credentials_once(page)
        assert _contains(caught.value, original)
        codes.append(_leaf(caught.value, bank))
        submit.click.assert_called_once_with(timeout=8000)
    assert len(set(codes)) == 3


def test_fubon_result_contract_fields_have_distinct_reasons():
    from backend.banks.fubon import FubonCrawler
    from tests.test_fubon_history_sync import _result
    valid = _result("2025-08-30", "2026-02-27", "2025-09-02")
    mutations = [
        ((), "account_no", None), ((), "account_no", "bad"),
        ((), "account_value", None), ((), "account_value", "bad"),
        ((), "preset", "bad"), ((), "status", "bad"),
        ((), "start", None), ((), "end", None),
    ]
    for key in valid["transport"]:
        mutations.append((("transport",), key, None))
    for key in ("gridRows", "gridRowCount", "totalCount", "rawDataRowCount", "gridCandidateCount", "hiddenGridCount", "pagerNodeCount", "structuralErrorCount", "nativeTotalMarkerCount", "malformedRowCount", "hiddenRowCount", "hiddenCellCount"):
        mutations.append((("snapshot",), key, None))
    for key, value in (("present", True), ("actionableNext", None), ("actionableNext", 1)):
        mutations.append((("snapshot", "pager"), key, value))
    codes = []
    for path, key, value in mutations:
        result = deepcopy(valid)
        target = result
        for component in path:
            target = target[component]
        target[key] = value
        # Keep the earlier eager snapshot binding checks satisfied so this
        # injection reaches the existing identity/value contract, not its guard.
        if not path and key in ("account_no", "account_value"):
            result["snapshot"]["selectedIdentity" if key == "account_no" else "selectedValue"] = value
        with pytest.raises(RuntimeError, match="fubon-twd-history-result") as caught:
            FubonCrawler._validated_twd_history_result(result)
        codes.append(_leaf(caught.value, "fubon"))
    assert len(set(codes)) == len(codes), "independent result fields collapsed"


def test_sinopac_response_contract_fields_have_distinct_reasons():
    from backend.banks.sinopac import SinopacCrawler
    from tests.test_sinopac_history_sync import _history_hit, ACCOUNT, LABEL
    start, end = date(2026, 8, 1), date(2026, 8, 31)
    codes = []
    for key, value in (("request_sequence", None), ("main_frame_request", False), ("method", "GET"), ("status", 500), ("redirected", True), ("body_size", None), ("body_size", 5_000_001), ("content_type", "text/html")):
        hit = _history_hit()
        setattr(hit, key, value)
        with pytest.raises(RuntimeError, match="sinopac-twd-history-response") as caught:
            SinopacCrawler._validate_history_hit(hit, label=LABEL, identity=ACCOUNT, currency="TWD", start=start, end=end, business_date="20260831", as_of=end)
        codes.append(_leaf(caught.value, "sinopac"))
    assert len(set(codes)) == len(codes)


@pytest.mark.parametrize("bank", ["sinopac", "fubon"])
def test_inventory_independent_invalid_shapes_have_distinct_codes(bank):
    codes = []
    if bank == "sinopac":
        from backend.banks.sinopac import SinopacCrawler
        from tests.test_sinopac_history_sync import _inventory_hit
        from backend.core.base import ResponseCollector
        for rows in ([None], [{"DataText": "", "DataValue": "01234567890123", "DisplayText": "TWD"}], [{"DataText": "test", "DataValue": "bad", "DisplayText": "TWD"}], [{"DataText": "test", "DataValue": "01234567890123", "DisplayText": "bad"}]):
            hit = _inventory_hit()
            hit.resp_json[0]["SubInfo"] = rows
            collector = ResponseCollector("sinopac.com")
            collector.hits.append(hit)
            with pytest.raises(RuntimeError) as caught:
                SinopacCrawler._twd_inventory(collector)
            codes.append(_leaf(caught.value, bank))
    else:
        from backend.banks.fubon import _validated_fubon_twd_options
        for options in (None, [], [None], [{"index": None, "text": "test", "value": "bad"}], [{"index": 1, "text": "test", "value": "bad"}], [{"index": 0, "text": "test", "value": "bad"}]):
            with pytest.raises(ValueError, match="invalid Fubon TWD inventory") as caught:
                _validated_fubon_twd_options(options)
            codes.append(_leaf(caught.value, bank))
    assert len(set(codes)) == len(codes)


# Existing fixtures construct synthetic controls/collector responses only.
from tests.test_sinopac_loan_diagnostics import repayment_case as repayment_case


def test_loan_native_actions_parser_and_cleanup_are_distinct(repayment_case, monkeypatch):
    c = repayment_case
    import backend.core.persist.sinopac as parser
    codes = []
    operations = [c.link.click, c.page.wait_for_url, c.collector.detach, c.page.on, c.observer.start, c.button.click, c.observer.read, c.page.remove_listener, c.observer.close, c.collector.attach]
    for target in operations:
        c.page.url = "https://mma.sinopac.com/mma/bank/easy_index_loan/mma_loandetail.aspx"
        previous = target.side_effect
        original = RuntimeError("synthetic-operation-failure")
        target.side_effect = original
        with pytest.raises(RuntimeError) as caught:
            c.crawler._collect_loan_repayments(c.page, c.collector, c.account, c.record)
        assert _contains(caught.value, original)
        codes.append(_leaf(caught.value, "sinopac"))
        target.side_effect = previous
    original = RuntimeError("synthetic-operation-failure")
    c.page.url = "https://mma.sinopac.com/mma/bank/easy_index_loan/mma_loandetail.aspx"
    monkeypatch.setattr(parser, "_parse_sinopac_repayment", Mock(side_effect=original))
    with pytest.raises(RuntimeError) as caught:
        c.crawler._collect_loan_repayments(c.page, c.collector, c.account, c.record)
    assert _contains(caught.value, original)
    codes.append(_leaf(caught.value, "sinopac"))
    assert len(set(codes)) == len(codes)


def test_loan_cleanup_keeps_primary_leaf(repayment_case):
    c = repayment_case
    primary, cleanup = RuntimeError("synthetic-primary"), RuntimeError("synthetic-cleanup")
    c.button.click.side_effect = primary
    with pytest.raises(RuntimeError) as first:
        c.crawler._collect_loan_repayments(c.page, c.collector, c.account, c.record)
    expected = _leaf(first.value, "sinopac")
    c.page.url = "https://mma.sinopac.com/mma/bank/easy_index_loan/mma_loandetail.aspx"
    c.page.remove_listener.side_effect = cleanup
    with pytest.raises(RuntimeError) as second:
        c.crawler._collect_loan_repayments(c.page, c.collector, c.account, c.record)
    assert _leaf(second.value, "sinopac") == expected


@pytest.mark.parametrize("method", ["_collect_loans", "_collect_transactions", "_collect_card_statements", "_collect_card_unbilled"])
def test_sinopac_native_query_navigation_is_attributed_without_promotion(method):
    from backend.banks.sinopac import SinopacCrawler
    crawler = object.__new__(SinopacCrawler)
    original = RuntimeError("synthetic-operation-failure")
    page = Mock()
    page.goto.side_effect = original
    if "card" in method:
        result = getattr(crawler, method)(page, Mock()) if method.endswith("unbilled") else getattr(crawler, method)(page)
        assert result in ({}, [])
    else:
        collector = Mock(request_sequence=0)
        collector.issued_count.return_value = 0
        collector.by_endpoint.return_value = []
        with pytest.raises(RuntimeError) as caught:
            getattr(crawler, method)(page, collector)
        assert caught.value is original
    _leaf(original, "sinopac")
    page.goto.assert_called_once()


@pytest.mark.parametrize("method", ["_open_twd_query", "_collect_attested_twd_history"])
def test_fubon_native_query_navigation_is_attributed(method):
    from backend.banks.fubon import FubonCrawler
    crawler = object.__new__(FubonCrawler)
    original = RuntimeError("synthetic-operation-failure")
    page = Mock()
    page.goto.side_effect = original
    with pytest.raises(RuntimeError) as caught:
        getattr(crawler, method)(page)
    assert caught.value is original
    _leaf(original, "fubon")
    page.goto.assert_called_once()


class _FailGet(dict):
    def __init__(self, value, key, error):
        super().__init__(value)
        self.key, self.error = key, error

    def get(self, key, *default):
        if key == self.key:
            raise self.error
        return super().get(key, *default)


def test_fubon_real_field_reads_are_distinct_and_eager():
    from backend.banks.fubon import FubonCrawler
    from tests.test_fubon_history_sync import _result
    codes = []
    for scope, keys in (("result", ("account_no", "account_value", "preset", "start", "end", "status", "snapshot", "transport")), ("snapshot", ("resultFresh", "busy", "failed", "selectedValue", "selectedIdentity"))):
        for key in keys:
            value = _result("2025-08-30", "2026-02-27", "2025-09-02")
            original = RuntimeError("synthetic-field-failure")
            if scope == "result":
                value = _FailGet(value, key, original)
            else:
                value["snapshot"] = _FailGet(value["snapshot"], key, original)
            with pytest.raises(RuntimeError) as caught:
                FubonCrawler._validated_twd_history_result(value)
            assert caught.value is original
            codes.append(_leaf(caught.value, "fubon"))
    assert len(set(codes)) == len(codes)


def test_sinopac_row_fields_and_money_reasons_are_distinct():
    from backend.banks.sinopac import SinopacCrawler
    from tests.test_sinopac_history_sync import _row
    codes = []
    changes = [(f"DataText{i}", None) for i in range(1, 12)]
    changes += [("DataText4", "bad"), ("DataText5", "bad"), ("DataText4", "2147483648"), ("DataText5", "2147483648")]
    for key, value in changes:
        row = _row()
        row[key] = value
        with pytest.raises(RuntimeError, match="sinopac-twd-history-row") as caught:
            SinopacCrawler._validate_history_row(row, currency="TWD", start=date(2026, 8, 1), end=date(2026, 8, 31))
        codes.append(_leaf(caught.value, "sinopac"))
    assert len(set(codes)) == len(codes)


@pytest.mark.parametrize("bank", ["sinopac", "fubon"])
def test_nonfatal_native_probe_errors_are_tagged_not_promoted(bank):
    module = import_module(f"backend.banks.{bank}")
    crawler = object.__new__(getattr(module, "SinopacCrawler" if bank == "sinopac" else "FubonCrawler"))
    original = RuntimeError("synthetic-operation-failure")
    page = Mock()
    page.url = "https://mma.sinopac.com/MyMMA/home" if bank == "sinopac" else "https://ebank.taipeifubon.com.tw/home"
    page.frames = []
    page.locator.side_effect = original
    assert crawler._logged_in(page) is False
    _leaf(original, bank)


def test_fubon_query_control_roles_have_distinct_native_failures(monkeypatch):
    import backend.banks.fubon as module
    from tests.test_fubon_history_sync import test_fubon_settles_all_same_frame_responses_before_dom_publication as drive
    codes = []
    native = module.bounded_evaluate
    for selector in (r"#form1\:rdoTxDetail", r"#form1\:rdoFast", r"#form1\:rdoDay180"):
        original = RuntimeError("synthetic-operation-failure")
        def fail(scope, expression, arg=None, **diagnostic):
            if arg == selector and "nodes[0].checked===true : null" in expression:
                raise original
            return native(scope, expression, arg, **diagnostic)
        monkeypatch.setattr(module, "bounded_evaluate", fail)
        with pytest.raises(RuntimeError) as caught:
            drive("normal", None)
        assert caught.value is original
        codes.append(_leaf(caught.value, "fubon"))
    assert len(set(codes)) == len(codes)


@pytest.mark.parametrize("bank", ["sinopac", "fubon"])
def test_captcha_native_failures_stay_nonfatal(bank):
    module = import_module(f"backend.banks.{bank}")
    crawler = object.__new__(getattr(module, "SinopacCrawler" if bank == "sinopac" else "FubonCrawler"))
    page = Mock()
    original = RuntimeError("synthetic-operation-failure")
    page.locator.side_effect = original
    assert crawler._ocr_captcha(page, max_attempts=1) is None
    _leaf(original, bank)


def test_date_parser_reasons_and_fields_are_distinct():
    from backend.banks.sinopac import SinopacCrawler
    from backend.banks.fubon import FubonCrawler
    from tests.test_fubon_history_sync import _result
    codes = []
    for value in (None, "bad", "20260230"):
        with pytest.raises(RuntimeError, match="test-date") as caught:
            SinopacCrawler._yyyymmdd(value, "test-date")
        codes.append(_leaf(caught.value, "sinopac"))
    assert len(set(codes)) == len(codes)
    codes = []
    for key in ("start", "end"):
        result = _result("2025-08-30", "2026-02-27", "2025-09-02")
        result[key] = "2026-02-30"
        with pytest.raises(RuntimeError, match="fubon-twd-history-result") as caught:
            FubonCrawler._validated_twd_history_result(result)
        codes.append(_leaf(caught.value, "fubon"))
    assert len(set(codes)) == len(codes)


def test_loan_account_control_roles_have_distinct_native_failures(repayment_case):
    c = repayment_case
    codes = []
    for key in ("AcctValue", "AcctValueFormat"):
        for name in ("count", "input_value"):
            target = getattr(c.controls["#" + key], name)
            original = RuntimeError("synthetic-operation-failure")
            target.side_effect = original
            with pytest.raises(RuntimeError) as caught:
                c.crawler._collect_loan_repayments(c.page, c.collector, c.account, c.record)
            assert _contains(caught.value, original)
            codes.append(_leaf(caught.value, "sinopac"))
            target.side_effect = None
    assert len(set(codes)) == len(codes)


def test_fubon_settled_control_fields_keep_distinct_reason_codes(monkeypatch):
    import backend.banks.fubon as module
    from tests.test_fubon_history_sync import test_fubon_settles_all_same_frame_responses_before_dom_publication as drive
    codes = []
    native = module.bounded_evaluate
    for key, value in (("value", "bad"), ("text", "bad"), ("detail", False), ("fast", False), ("preset", False), ("formBound", False), ("viewState", ""), ("formAction", "bad")):
        def change(scope, expression, arg=None, **diagnostic):
            result = native(scope, expression, arg, **diagnostic)
            if "preset:preset.checked" in expression:
                result[key] = value
            return result
        monkeypatch.setattr(module, "bounded_evaluate", change)
        with pytest.raises(RuntimeError) as caught:
            drive("normal", None)
        codes.append(_leaf(caught.value, "fubon"))
    assert len(set(codes)) == len(codes)


def test_fubon_transport_binding_fields_are_distinguishable(monkeypatch):
    from backend.banks.fubon import FubonCrawler
    from tests.test_fubon_history_sync import test_fubon_settles_all_same_frame_responses_before_dom_publication as drive
    capture = FubonCrawler._capture_twd_response
    collect = FubonCrawler._collect_twd_window
    caught = []
    def observe(self, *args):
        try:
            return collect(self, *args)
        except RuntimeError as error:
            caught.append(error)
            raise
    monkeypatch.setattr(FubonCrawler, "_collect_twd_window", observe)
    codes = []
    for key in ("frameBound", "requestBound", "presetBound", "fieldsBound", "viewStateBound", "actionBound", "formBound"):
        def change(response, hits, *args):
            capture(response, hits, *args)
            hits[-1][key] = False
        monkeypatch.setattr(FubonCrawler, "_capture_twd_response", staticmethod(change))
        suffix = {"frameBound": "frame-bound", "requestBound": "bound", "presetBound": "preset-bound", "fieldsBound": "fields-bound", "viewStateBound": "viewstate-bound", "actionBound": "action-bound", "formBound": "form-bound"}[key]
        drive("normal", "fubon-twd-history-transport-no-" + suffix + "-response")
        codes.append(_leaf(caught[-1], "fubon"))
    assert len(set(codes)) == len(codes)


def test_sinopac_dom_rejection_reasons_are_distinguishable(monkeypatch):
    from backend.banks.sinopac import SinopacCrawler
    from tests.test_sinopac_history_sync import test_sinopac_collect_uses_native_account_date_and_query_controls as drive
    collect = SinopacCrawler._collect_transactions
    caught = []
    def observe(self, *args):
        try:
            return collect(self, *args)
        except RuntimeError as error:
            caught.append(error)
            raise
    observe.__wrapped__ = collect
    monkeypatch.setattr(SinopacCrawler, "_collect_transactions", observe)
    cases = (
        (False, False, 1, False, 0, 0, 0, 0, 0, 0, 0, 0, "nonempty-binding"),
        (True, False, 3, False, 2, 1, 0, 0, 1, 0, 0, 0, "empty-hidden-template-text"),
        (True, False, 3, False, 1, 1, 1, 0, 1, 1, 0, 0, "empty-hidden-header"),
    )
    codes = []
    for args in cases:
        drive(monkeypatch, *args)
        codes.append(_leaf(caught[-1], "sinopac"))
    assert len(set(codes)) == len(codes)


def test_sinopac_nonempty_dom_fields_do_not_collapse_to_attestation(monkeypatch):
    from backend.banks.sinopac import SinopacCrawler
    from tests.test_sinopac_history_sync import test_sinopac_collect_uses_native_account_date_and_query_controls as drive
    collect = SinopacCrawler._collect_transactions
    codes = []
    for key, value in (("rows", None), ("rows", 0), ("visibleRows", 0), ("signature", None), ("signature", []), ("signature", [None, None]), ("signature", [1, 1]), ("emptyMarker", True), ("freshEmpty", True), ("mutations", 0)):
        caught = []
        def observe(self, page, collector):
            evaluate = page.evaluate
            def change(expression, arg=None):
                result = evaluate(expression, arg)
                if isinstance(result, dict) and "signature" in result:
                    result[key] = value
                return result
            page.evaluate = change
            try:
                return collect(self, page, collector)
            except RuntimeError as error:
                caught.append(error)
                raise
        observe.__wrapped__ = collect
        monkeypatch.setattr(SinopacCrawler, "_collect_transactions", observe)
        drive(monkeypatch, False, True, 1, False, 0, 0, 0, 0, 0, 0, 0, 0, "nonempty-attestation")
        codes.append(_leaf(caught[-1], "sinopac"))
    assert len(set(codes)) == len(codes)


def test_fubon_snapshot_predicates_still_evaluate_eagerly():
    from backend.banks.fubon import FubonCrawler
    from tests.test_fubon_history_sync import _result
    result = _result("2025-08-30", "2026-02-27", "2025-09-02")
    result["snapshot"]["resultFresh"] = False
    original = RuntimeError("synthetic-later-field-failure")
    result["snapshot"] = _FailGet(result["snapshot"], "selectedIdentity", original)
    with pytest.raises(RuntimeError) as caught:
        FubonCrawler._validated_twd_history_result(result)
    assert caught.value is original
    _leaf(original, "fubon")


def test_sinopac_captcha_chain_primitives_are_distinct_and_one_shot():
    from backend.banks.sinopac import SinopacCrawler
    codes = []
    for operation in ("locator", "count", "visible_nth", "visible", "enabled_nth", "enabled", "result_nth"):
        page, images, image = Mock(), Mock(), Mock()
        page.locator.return_value = images
        images.count.return_value = 1
        images.nth.return_value = image
        image.is_visible.return_value = image.is_enabled.return_value = True
        original = RuntimeError("synthetic-native")
        if operation.endswith("nth"):
            index = {"visible_nth": 0, "enabled_nth": 1, "result_nth": 2}[operation]
            images.nth.side_effect = [image] * index + [original]
            target = images.nth
        else:
            target = {"locator": page.locator, "count": images.count, "visible": image.is_visible, "enabled": image.is_enabled}[operation]
            target.side_effect = original
        with pytest.raises(RuntimeError) as caught:
            SinopacCrawler._captcha_image(page, enabled=True)
        assert caught.value is original
        codes.append(_leaf(original, "sinopac"))
        assert target.call_count == (index + 1 if operation.endswith("nth") else 1)
    assert len(set(codes)) == len(codes)


def test_sinopac_row_generator_roles_are_distinct():
    from backend.banks.sinopac import SinopacCrawler
    from tests.test_sinopac_history_sync import _row
    codes = []
    for key, value in [("DataText4", "1.5"), ("DataText5", "1.5")] + [(f"DataText{i}", "x" * 2001) for i in range(6, 12)]:
        row = _row()
        row[key] = value
        with pytest.raises(RuntimeError) as caught:
            SinopacCrawler._validate_history_row(row, currency="TWD", start=date(2026, 8, 1), end=date(2026, 8, 31))
        codes.append(_leaf(caught.value, "sinopac"))
    assert len(set(codes)) == len(codes)


def test_fubon_empty_result_rejections_are_distinct():
    from backend.banks.fubon import FubonCrawler
    from tests.test_fubon_history_sync import _result
    codes = []
    changes = ({"hasGrid": True}, {"nativeTotalMarkerCount": 1}, {"gridCandidateCount": 1},
               {"gridRows": [["2025/09/02"]], "gridRowCount": 1, "rawDataRowCount": 1, "totalCount": 1},
               {"gridText": "x"}, {"emptyMarker": "bad"})
    for change in changes:
        result = _result("2025-08-30", "2026-02-27", "2025-09-02", empty=True)
        result["snapshot"].update(change)
        with pytest.raises(RuntimeError) as caught:
            FubonCrawler._validated_twd_history_result(result)
        codes.append(_leaf(caught.value, "fubon"))
    assert len(set(codes)) == len(codes)


def test_sinopac_query_control_roles_and_primitives_are_distinct(monkeypatch):
    from backend.banks.sinopac import SinopacCrawler
    from tests.test_sinopac_history_sync import test_sinopac_collect_uses_native_account_date_and_query_controls as drive
    collect = SinopacCrawler._collect_transactions
    codes = []
    for role in ("#StartDate", "#EndDate", "#btnQuery"):
        for operation in ("count", "visible_nth", "is_visible", "enabled_nth", "is_enabled"):
            original = RuntimeError("synthetic-native")
            calls = []
            def observe(self, page, collector):
                locator = page.locator
                def locate(selector):
                    value = locator(selector)
                    if selector != role:
                        return value
                    nth_calls = 0
                    class Control:
                        def __getattr__(self, name):
                            def invoke(*args, **kwargs):
                                nonlocal nth_calls
                                calls.append(name)
                                if name == "nth":
                                    nth_calls += 1
                                    if operation == ("visible_nth" if nth_calls == 1 else "enabled_nth"):
                                        raise original
                                    getattr(value, name)(*args, **kwargs)
                                    return self
                                if name == operation:
                                    raise original
                                return getattr(value, name)(*args, **kwargs)
                            return invoke
                    return Control()
                page.locator = locate
                return collect(self, page, collector)
            observe.__wrapped__ = collect
            monkeypatch.setattr(SinopacCrawler, "_collect_transactions", observe)
            with pytest.raises(RuntimeError) as caught:
                drive(monkeypatch, False, True, 1, False, 0, 0, 0, 0, 0, 0, 0, 0, None)
            assert caught.value is original
            codes.append(_leaf(original, "sinopac"))
            assert calls.count("count") == 1
            assert calls.count("nth") <= 2
    assert len(set(codes)) == len(codes)


@pytest.mark.parametrize("bank", ["sinopac", "fubon"])
def test_ocr_terminal_recovers_only_current_call_reason(bank, monkeypatch):
    from types import MethodType
    module = import_module(f"backend.banks.{bank}")
    crawler, page, fields, submit = _login(bank)
    cls = type(crawler)
    crawler._ocr_captcha = MethodType(cls._ocr_captcha, crawler)
    crawler.captcha_tmp = "synthetic-unused"
    original = RuntimeError("synthetic-private-ocr")
    if bank == "sinopac":
        monkeypatch.setattr(module, "wait_captcha_stable", Mock())
        monkeypatch.setattr(module, "solve_captcha", Mock(side_effect=original))
    else:
        frame = page.frames[0]
        locate = frame.locator.side_effect
        image = Mock()
        image.count.return_value = 1
        image.nth.return_value = image
        image.is_visible.return_value = True
        frame.locator.side_effect = lambda selector: image if selector == "#m1_captchaImage" else locate(selector)
        monkeypatch.setattr(module, "ocr_bytes", Mock(side_effect=original))
    with pytest.raises(RuntimeError) as caught:
        crawler.submit_credentials_once(page)
    assert _leaf(caught.value, bank) == _leaf(original, bank)
    submit.click.assert_not_called()
    assert not any("diagnostic" in key for key in vars(crawler))


def test_fubon_general_tab_candidate_native_primitives_are_distinct():
    from backend.banks.fubon import FubonCrawler
    codes = []
    for operation in ("locator", "filter", "nth", "element_handle", "is_visible", "is_enabled", "inner_text"):
        crawler = object.__new__(FubonCrawler)
        crawler._logged_in = Mock(return_value=False)
        page, header_frame, frame, header, action = [Mock() for _ in range(5)]
        crawler._find_header_frame = Mock(return_value=header_frame)
        crawler._find_login_frame = Mock(return_value=frame)
        for item, text in ((header, "登入"), (action, "一般登入")):
            item.count.return_value = 1
            item.nth.return_value = item
            item.is_visible.return_value = item.is_enabled.return_value = True
            item.inner_text.return_value = text
        header_frame.locator.return_value = header
        frame.locator.return_value.filter.return_value = action
        original = RuntimeError("synthetic-native")
        target = {"locator": frame.locator, "filter": frame.locator.return_value.filter,
                  "nth": action.nth, "element_handle": action.element_handle,
                  "is_visible": action.is_visible, "is_enabled": action.is_enabled, "inner_text": action.inner_text}[operation]
        target.side_effect = original
        with pytest.raises(RuntimeError) as caught:
            crawler.prepare_login_page(page)
        assert _contains(caught.value, original)
        codes.append(_leaf(caught.value, "fubon"))
        target.assert_called_once()
        action.click.assert_not_called()
    assert len(set(codes)) == len(codes)


@pytest.mark.parametrize("bank", ["sinopac", "fubon"])
def test_native_statements_do_not_share_primitive_boundaries(bank):
    import ast
    from pathlib import Path
    module = import_module(f"backend.banks.{bank}")
    tree = ast.parse(Path(module.__file__).read_text())
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    native = {"locator", "nth", "count", "is_visible", "is_enabled", "inner_text", "get_attribute", "fill", "click", "input_value", "filter", "evaluate"}
    groups = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in native):
            continue
        cursor = node
        while cursor in parents:
            cursor = parents[cursor]
            if isinstance(cursor, ast.Lambda):
                break  # Expression-sized error_step boundary.
            if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                break
            if isinstance(cursor, ast.With) and any(isinstance(i.context_expr, ast.Call) and isinstance(i.context_expr.func, ast.Name) and i.context_expr.func.id == "error_step" for i in cursor.items):
                groups.setdefault(cursor.lineno, []).append(node.func.attr)
                break
    assert not {line: calls for line, calls in groups.items() if len(calls) > 1}


def test_sinopac_nested_loan_metadata_predicates_are_distinct(repayment_case):
    c = repayment_case
    original = deepcopy(c.state["payload"])
    codes = []
    cases = [(None, None), ("shape", None)]
    cases += [(key, value) for key in original[0]["HeadInfo"][0] for value in (None, "x" * 2001)]
    for key, value in cases:
        c.state["payload"] = deepcopy(original)
        c.page.url = "https://mma.sinopac.com/mma/bank/easy_index_loan/mma_loandetail.aspx"
        if key is None:
            c.state["payload"][0]["HeadInfo"][0] = None
        elif key == "shape":
            c.state["payload"][0]["HeadInfo"][0].pop(next(iter(original[0]["HeadInfo"][0])))
        else:
            c.state["payload"][0]["HeadInfo"][0][key] = value
        with pytest.raises(RuntimeError) as caught:
            c.crawler._collect_loan_repayments(c.page, c.collector, c.account, c.record)
        codes.append(_leaf(caught.value, "sinopac"))
    assert len(set(codes)) == len(codes)


@pytest.mark.parametrize("error_class", [RuntimeError, NativeTimeoutError])
def test_sinopac_repayment_control_roles_survive_actual_safe_sink(error_class, repayment_case, monkeypatch, tmp_path, capsys):
    from tests.test_bank_login_lifecycle import _run
    c = repayment_case
    codes = []
    monkeypatch.setattr(c.crawler, "_shared_login", lambda _: True)
    monkeypatch.setattr(c.crawler, "_credential_origin_allowed", lambda _: True)
    monkeypatch.setattr(c.crawler, "_build_fetch_kwargs", lambda: {})
    monkeypatch.setattr(c.crawler, "logout", Mock(return_value=True))
    for role in ("#StartDate", "#EndDate", "#btnQuery"):
        for operation in ("count", "is_visible", "is_enabled"):
            c.page.url = "https://mma.sinopac.com/mma/bank/easy_index_loan/mma_loandetail.aspx"
            original = error_class("PRIVATE-SYNTHETIC")
            target = getattr(c.controls[role], operation)
            before = target.call_count
            target.side_effect = original
            def collect(*_):
                c.crawler._collect_loan_repayments(c.page, c.collector, c.account, c.record)
            monkeypatch.setattr(c.crawler, "collect", collect)
            root = tmp_path / (role[1:] + operation)
            root.mkdir()
            result, _ = _run(monkeypatch, root, c.crawler, None)
            code = _leaf(original, "sinopac")
            assert f"[{code}]" in result["error"]
            assert "PRIVATE-SYNTHETIC" not in repr(result) + capsys.readouterr().err
            assert target.call_count == before + 1
            codes.append(code)
            target.side_effect = None
    assert len(set(codes)) == len(codes)


@pytest.mark.parametrize("error_class", [RuntimeError, NativeTimeoutError])
def test_fubon_evaluate_roles_and_primitives_reach_actual_safe_sink(error_class, monkeypatch, tmp_path, capsys):
    from types import MethodType
    from backend.banks.fubon import FubonCrawler
    from backend.core.base import BankCrawler
    from tests.test_bank_login_lifecycle import _run
    codes = []
    for role in ("navigation", "inventory"):
        for operation in ("locator", "evaluate"):
            crawler = object.__new__(FubonCrawler)
            crawler.name = "fubon"
            page, frame = Mock(), Mock()
            crawler._fubon_content_frame = Mock(return_value=frame)
            if role == "inventory":
                crawler._open_twd_query = Mock(return_value=frame)
            original = error_class("PRIVATE-SYNTHETIC")
            target = frame.locator if operation == "locator" else frame.locator.return_value.evaluate
            target.side_effect = original
            def collect(*_):
                return (crawler._open_twd_query(page) if role == "navigation" else crawler._collect_attested_twd_history(page))
            monkeypatch.setattr(crawler, "collect", collect)
            monkeypatch.setattr(crawler, "_shared_login", lambda _: True)
            monkeypatch.setattr(crawler, "_credential_origin_allowed", lambda _: True)
            monkeypatch.setattr(crawler, "_build_fetch_kwargs", lambda: {})
            monkeypatch.setattr(crawler, "logout", Mock(return_value=True))
            monkeypatch.setattr(crawler, "_execute_browser_flow", MethodType(BankCrawler._execute_browser_flow, crawler))
            root = tmp_path / (role + operation)
            root.mkdir()
            result, _ = _run(monkeypatch, root, crawler, None)
            code = _leaf(original, "fubon")
            assert f"[{code}]" in result["error"]
            assert "PRIVATE-SYNTHETIC" not in repr(result) + capsys.readouterr().err
            target.assert_called_once()
            codes.append(code)
    assert len(set(codes)) == len(codes)


@pytest.mark.parametrize("driver", ["query-fubon", "query-sinopac", "js-fubon", "js-sinopac", "js-sinopac-recovery"])
def test_frozen_query_baseline_rejects_subordinate_helper_mutation(driver, monkeypatch):
    """A current-only native/business change must not infect the old run."""
    from tests.test_sinopac_fubon_terminal_residual_codes import (
        _fubon_window_js_case, _sinopac_history_js_case,
    )
    bank = driver.split("-")[1]
    module = import_module(f"backend.banks.{bank}")
    cls = module.FubonCrawler if bank == "fubon" else module.SinopacCrawler
    calls = []
    if bank == "fubon":
        native = cls._bound_twd_result_frame
        def changed(self, page, frame):
            calls.append("current")
            page.wait_for_timeout(17)  # Actual extra native operation, not metadata.
            return native(self, page, frame)
        monkeypatch.setattr(cls, "_bound_twd_result_frame", changed)
    else:
        native = cls._validate_history_hit
        def changed(owner, *args, **kwargs):
            calls.append("current")
            value = deepcopy(native(*args, **kwargs))
            value["records"][0]["DataText3"] = "synthetic-business-mutation"
            return value
        monkeypatch.setattr(cls, "_validate_history_hit", classmethod(changed))
    with pytest.raises(AssertionError):
        if driver.startswith("query"):
            test_baseline_and_current_actual_query_native_script_traces_match(
                bank, "normal" if bank == "fubon" else "nonempty", None, monkeypatch,
            )
        elif driver.endswith("-recovery"):
            from tests.test_sinopac_fubon_terminal_residual_codes import (
                test_sinopac_absent_initial_table_recovers_without_new_guard,
            )
            test_sinopac_absent_initial_table_recovers_without_new_guard(monkeypatch)
        elif bank == "fubon":
            _fubon_window_js_case(monkeypatch, "account.value=args.value", {}, None, None)
        else:
            _sinopac_history_js_case(monkeypatch, "shape", "SINOPAC-05-782-001")
    assert calls == ["current"], "baseline dispatched a current subordinate helper"


@pytest.mark.parametrize("bank", ["sinopac", "fubon"])
def test_frozen_query_baseline_coverage_helper_is_independent(bank, monkeypatch):
    from backend.core import base
    old = _baseline_bank(bank)
    def changed(*args, **kwargs):
        raise AssertionError("current coverage helper mutation")
    monkeypatch.setattr(base, "_history_date", changed)
    coverage = {"mode": "full", "domains": [{
        "domain": "twd_transactions", "expected": [], "windows": [],
        "empty_window": {"start": "2026-01-01", "end": "2026-01-01",
                         "status": "explicit_empty", "pages": 1},
    }]}
    assert old.validate_history_coverage(
        coverage, expected_mode="full", expected_domains=frozenset({"twd_transactions"}),
    )["windows"] == 1
    with pytest.raises(AssertionError, match="current coverage helper mutation"):
        base.validate_history_coverage(
            coverage, expected_mode="full", expected_domains=frozenset({"twd_transactions"}),
        )


@pytest.mark.parametrize("bank", ["sinopac", "fubon"])
def test_frozen_query_baseline_bindings_and_shared_core_sources(bank):
    import ast
    import linecache
    from pathlib import Path
    from types import FunctionType
    from backend.core import base

    old = _baseline_bank(bank)
    cls = old.SinopacCrawler if bank == "sinopac" else old.FubonCrawler
    assert cls is not getattr(import_module(f"backend.banks.{bank}"), cls.__name__)
    functions = [value for value in vars(old).values() if isinstance(value, FunctionType)
                 and value.__module__ == old.__name__]
    for value in vars(cls).values():
        if isinstance(value, (staticmethod, classmethod)):
            value = value.__func__
        if isinstance(value, FunctionType):
            functions.append(value)
    assert functions
    for function in functions:
        assert function.__globals__ is vars(old)
        assert function.__code__.co_filename == old.__file__
    validator = old.validate_history_coverage
    assert validator.__globals__ is not vars(base)
    assert validator.__globals__["_history_date"].__globals__ is validator.__globals__
    assert validator.__code__.co_filename.endswith(".baseline16f2d4b")
    assert validator.__annotations__ == base.validate_history_coverage.__annotations__

    # Shared objects are intentionally not a claim to freeze the whole project.
    # Every explicit method on the used core classes must remain source-equal.
    original = "".join(linecache.getlines(validator.__code__.co_filename))
    current = Path(base.__file__).read_text()
    def methods(source, name):
        node = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.ClassDef) and node.name == name)
        return {node.name: ast.get_source_segment(source, node) for node in node.body
                if isinstance(node, ast.FunctionDef)}
    for name in ("ApiHit", "ResponseCollector", "_OriginGuardProxy"):
        assert methods(current, name) == methods(original, name), name
    # Include the fields driving generated ApiHit dataclass methods.
    def api_hit(source):
        node = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.ClassDef) and node.name == "ApiHit")
        return ast.get_source_segment(source, node)
    assert api_hit(current) == api_hit(original)


def _baseline_bank(bank):
    import linecache
    import subprocess
    from pathlib import Path
    from types import ModuleType
    module = import_module(f"backend.banks.{bank}")
    root = Path(module.__file__).resolve().parents[2]
    from hashlib import sha256
    revision = "16f2d4b130f44973d346b420f3d486ac839f71ac"
    raw = subprocess.check_output(["git", "show", f"{revision}:backend/banks/{bank}.py"], cwd=root)
    assert sha256(raw).hexdigest() == {
        "sinopac": "67821c9e451cd872d8c79203d2ec34082a32e8514f8276042e0fecb1cb668b61",
        "fubon": "b01cb63ca7ddc6e28935a0a96f883503e4192f1ac9576e8706303330cac20f93",
    }[bank]
    source = raw.decode("utf-8")
    filename = str(module.__file__) + ".baseline16f2d4b"
    linecache.cache[filename] = (len(source), None, source.splitlines(keepends=True), filename)
    old = ModuleType(f"backend.banks._baseline_{bank}")
    old.__file__ = filename
    exec(compile(source, filename, "exec"), old.__dict__)
    # Only the exercised imported validator differs from 16f2. Freeze its two
    # functions/constants too; remaining shared core methods are source-checked.
    import ast
    import re
    from datetime import date, timedelta
    raw_core = subprocess.check_output(["git", "show", f"{revision}:backend/core/base.py"], cwd=root)
    assert sha256(raw_core).hexdigest() == "44b363c4376d737b5db19f3a632e2ec0831cad6cc5cedd3f1c1e5070750c872d"
    core_source = raw_core.decode("utf-8")
    core_tree = ast.parse(core_source)
    names = {"_history_date", "validate_history_coverage", "_ISO_DATE_RE", "HISTORY_DOMAINS"}
    nodes: list[ast.stmt] = [node for node in core_tree.body if (
        isinstance(node, ast.FunctionDef) and node.name in names
        or isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in names for target in node.targets)
    )]
    assert len(nodes) == 4
    core_filename = str(root / "backend/core/base.py") + ".baseline16f2d4b"
    linecache.cache[core_filename] = (len(core_source), None, core_source.splitlines(keepends=True), core_filename)
    from typing import Any
    namespace = {"__name__": "backend.core._baseline_history_coverage", "date": date,
                 "timedelta": timedelta, "re": re, "Any": Any}
    future = [node for node in core_tree.body
              if isinstance(node, ast.ImportFrom) and node.module == "__future__"]
    exec(compile(ast.Module(body=future + nodes, type_ignores=[]), core_filename, "exec"), namespace)
    old.__dict__["validate_history_coverage"] = namespace["validate_history_coverage"]
    return old


def _query_metadata_contract(bank, script):
    """Apply the finite reviewed delta, not a JS/string/AST normalizer.

    Everything outside these exact replacements remains byte-protected against
    16f2d4b. Runtime getter/action/timer parity is proved independently below.
    """
    def replace(before, after):
        nonlocal script
        assert script.count(before) == 1, (bank, before)
        start = script.index(before)
        indent = script[script.rfind("\n", 0, start) + 1:start]
        # Only the existing line's indentation is copied to added statements.
        assert not indent.strip() or "\n" not in after
        script = script.replace(before, after.replace("\n", "\n" + indent), 1)

    if bank == "sinopac":
        if "const stale=new WeakSet" in script:
            replace("() => { window.__hermesSinopacObserver?.disconnect();",
                    "() => { const marker=(() => { window.__hermesSinopacObserver?.disconnect();")
            replace("return [value.length,hash>>>0]; }",
                    "return [value.length,hash>>>0]; })();\nreturn marker===null?{errorId:'SINOPAC-05-781-001'}:marker; }")
        elif "; let result=null;" in script:
            replace("; let result=null;", "; let result=null,errorId=null;")
            replace("if(Number.isInteger(beforeMutations)&&afterMutations===beforeMutations &&records.length===0&&JSON.stringify(settled)===JSON.stringify(result)){",
                    "if((Number.isInteger(beforeMutations)||(errorId='SINOPAC-05-782-001',false)) &&(afterMutations===beforeMutations||(errorId='SINOPAC-05-782-002',false)) &&(records.length===0||(errorId='SINOPAC-05-782-003',false)) &&(JSON.stringify(settled)===JSON.stringify(result)||(errorId='SINOPAC-05-782-004',false))){")
            replace("return null; } finally { cleanup(); } }",
                    "return {errorId}; } finally { cleanup(); } }")
    elif bank == "fubon":
        if "account.value=args.value" in script:
            replace("if(forms.length!==1)return {ok:false};",
                    "if(forms.length!==1)return {ok:false,errorId:'FUBON-05-780-001'};")
            replace("if(!account||!submit||!formBound||!states[0].value)return {ok:false};",
                    "if(!account)return {ok:false,errorId:'FUBON-05-780-002'};\n"
                    "if(!submit)return {ok:false,errorId:'FUBON-05-780-003'};\n"
                    "if(!formBound)return {ok:false,errorId:'FUBON-05-780-004'};\n"
                    "if(!states[0].value)return {ok:false,errorId:'FUBON-05-780-005'};")
        elif "preset:preset.checked" in script:
            replace("if(forms.length!==1)return null;",
                    "if(forms.length!==1)return {errorId:'FUBON-05-781-001'};")
            replace("if(!account||!detail||!fast||!preset||!submit||!formBound)return null;",
                    "if(!account)return {errorId:'FUBON-05-781-002'};\n"
                    "if(!detail)return {errorId:'FUBON-05-781-003'};\n"
                    "if(!fast)return {errorId:'FUBON-05-781-004'};\n"
                    "if(!preset)return {errorId:'FUBON-05-781-005'};\n"
                    "if(!submit)return {errorId:'FUBON-05-781-006'};\n"
                    "if(!formBound)return {errorId:'FUBON-05-781-007'};")
        elif "nodes[0].checked===true : null" in script:
            replace("return forms.length===1&&nodes.length===1&&forms[0].contains(nodes[0])",
                    "const result=forms.length===1&&nodes.length===1&&forms[0].contains(nodes[0])")
            indent = script.split("const result=", 1)[0].rsplit("\n", 1)[1]
            script = script.replace("? nodes[0].checked===true : null;",
                                    "? nodes[0].checked===true : null;\n" + indent +
                                    "return result===null?{errorReason:forms.length!==1?'form':nodes.length!==1?'count':'owner'}:result;", 1)
        elif "nodes[0].click()" in script:
            replace("if(forms.length!==1)return false;", "if(forms.length!==1)return {errorReason:'form'};")
            replace("if(nodes.length!==1||!forms[0].contains(nodes[0]))return false;",
                    "if(nodes.length!==1)return {errorReason:'count'};\nif(!forms[0].contains(nodes[0]))return {errorReason:'owner'};")
            replace("return nodes[0].checked===true;",
                    "const checked=nodes[0].checked===true;\nreturn checked?true:{errorReason:'unchecked'};")
        elif "submits[0].click()" in script:
            replace("if(forms.length!==1)return false;", "if(forms.length!==1)return {errorId:'FUBON-05-785-001'};")
            replace("if(nodes.length!==1||!form.contains(nodes[0])||!nodes[0].name)return false;",
                    "if(nodes.length!==1)return {errorId:'FUBON-05-785-002'};\n"
                    "if(!form.contains(nodes[0]))return {errorId:'FUBON-05-785-003'};\n"
                    "if(!nodes[0].name)return {errorId:'FUBON-05-785-004'};")
            indent = script.split("if(preset.checked", 1)[0].rsplit("\n", 1)[1]
            replace("if(preset.checked!==true||checked.length!==1||checked[0]!==preset\n" + indent + "    ||submits.length!==1||!form.contains(submits[0]))return false;",
                    "if(preset.checked!==true)return {errorId:'FUBON-05-785-005'};\n"
                    "if(checked.length!==1)return {errorId:'FUBON-05-785-006'};\n"
                    "if(checked[0]!==preset)return {errorId:'FUBON-05-785-007'};\n"
                    "if(submits.length!==1)return {errorId:'FUBON-05-785-008'};\n"
                    "if(!form.contains(submits[0]))return {errorId:'FUBON-05-785-009'};")
        elif "__hermesFubonDomGuard=new MutationObserver" in script:
            replace("if(!navigated&&window.__hermesFubonSubmitDocument!==document)return false;",
                    "if(!navigated&&window.__hermesFubonSubmitDocument!==document)return {errorId:'FUBON-05-786-001'};")
    else:
        raise AssertionError(bank)
    return script


def _query_literal_scripts(bank, module):
    import ast
    import inspect
    method = "_collect_transactions" if bank == "sinopac" else "_collect_twd_window"
    cls = module.SinopacCrawler if bank == "sinopac" else module.FubonCrawler
    # A wrapper parses the indented method without dedenting JS string bytes.
    tree = ast.parse("if True:\n" + inspect.getsource(getattr(cls, method)))
    return [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
            and isinstance(node.value, str) and "=>" in node.value]


@pytest.mark.parametrize("bank", ["sinopac", "fubon"])
def test_query_metadata_contract_accepts_only_reviewed_additions(bank):
    old = _query_literal_scripts(bank, _baseline_bank(bank))
    current = _query_literal_scripts(bank, import_module(f"backend.banks.{bank}"))
    assert len(old) == len(current)
    assert [_query_metadata_contract(bank, script) for script in old] == current


@pytest.mark.parametrize("bank,marker", [
    ("sinopac", "const stale=new WeakSet"),
    ("sinopac", "; let result=null"),
    ("fubon", "account.value=args.value"),
    ("fubon", "preset:preset.checked"),
    ("fubon", "nodes[0].checked===true : null"),
    ("fubon", "nodes[0].click()"),
    ("fubon", "submits[0].click()"),
    ("fubon", "__hermesFubonDomGuard=new MutationObserver"),
])
def test_reviewed_query_scripts_native_faults_are_transparent(bank, marker, request):
    import json
    from tests.test_sinopac_fubon_terminal_residual_codes import _node, FUBON_DOM, SINOPAC_DOM
    scripts = []
    for module in (_baseline_bank(bank), import_module(f"backend.banks.{bank}")):
        literals = _query_literal_scripts(bank, module)
        expression = next(script for script in literals if marker in script)
        if marker == "; let result=null":
            probe = next(script for script in literals if script.startswith("() => { const width="))
            expression = "async () => { const probe = " + probe + expression
        scripts.append(expression)
    if bank == "fubon":
        setup = FUBON_DOM.replace("CONFIG", '{"sameDocument":true}')
        arg = {"value": "012-000-90000000267053-X-TW", "preset": "rdoDay180"}
        if "nodes[0]" in marker:
            arg = r"#form1\:rdoDay180"
        elif marker == "submits[0].click()":
            arg = "rdoDay180"
        elif "MutationObserver" in marker:
            arg = False
    else:
        setup = SINOPAC_DOM.replace("CONFIG", '{"mode":"normal"}')
        arg = None
        if marker == "const stale=new WeakSet":
            setup += """
            const row=new HTMLElement();row.parentElement=null;
            row.getAttribute=(name)=>{trace.push(['attr',name]);return null};
            global.getComputedStyle=()=>{trace.push(['style']);return {display:'block',visibility:'visible',opacity:'1'}};
            const table={get innerHTML(){trace.push(['html']);return '<tbody></tbody>'},
                querySelectorAll(selector){trace.push(['tableQuery',selector]);return [row]}};
            document.querySelector=selector=>{trace.push(['one',selector]);return table};
            document.body={};
            global.MutationObserver=class{constructor(callback){trace.push(['observer'])}
                observe(...args){trace.push(['observe',args[1]])}disconnect(){trace.push(['disconnect'])}};
            global.setTimeout=(callback,ms)=>{trace.push(['timeout',ms]);global.__timer=callback;return 1};
            """
    # Include global observer-state reads/writes/deletes, not just DOM methods.
    setup += """
    global.window=new Proxy(window,{
        get(target,key){trace.push(['window-get',String(key)]);return target[key]},
        set(target,key,value){trace.push(['window-set',String(key)]);target[key]=value;return true},
        deleteProperty(target,key){trace.push(['window-delete',String(key)]);return delete target[key]}});
    """
    if bank == "sinopac":
        setup = setup.replace("offsetWidth:{get(){return 1}},offsetHeight:{get(){return 1}}",
                              "offsetWidth:{get(){trace.push(['width']);return 1}},offsetHeight:{get(){trace.push(['height']);return 1}}")
    # Throw at every recorded native getter/method in the successful path.
    # Catch inside the evaluated function so exception identity/message and the
    # finally/cleanup trace are compared, not subprocess error formatting.
    def run(expression, fault):
        injected = setup + "\n" + """
        const nativePush=trace.push;
        trace.push=function(...items){nativePush.apply(this,items);
            if(this.length===FAULT){global.__nativeError=new TypeError('synthetic-native-'+FAULT);throw global.__nativeError;}return this.length};
        """.replace("FAULT", json.dumps(fault))
        wrapped = "async arg => {try {const value=await (" + expression + ")(arg);if(global.__timer)global.__timer();return {ok:true,value};}catch(error){return {ok:false,type:error.name,message:error.message,sameOriginal:error===global.__nativeError};}}"
        return _node(wrapped, arg, injected)
    success = [run(expression, None) for expression in scripts]
    assert success[0] == success[1]
    assert success[0]["value"]["ok"] is True
    assert success[0]["trace"]
    request.node.user_properties.append(("native_fault_points", str(len(success[0]["trace"]))))
    for fault in range(1, len(success[0]["trace"]) + 1):
        old, current = [run(expression, fault) for expression in scripts]
        assert old == current, (bank, marker, fault)
        assert len(old["trace"]) >= fault
        if old["value"]["ok"] is False:
            assert old["value"]["sameOriginal"] is True


def _assert_query_traces(bank, baseline, current):
    expected = []
    for label, name, args, kwargs in baseline:
        if name == "evaluate":
            args = (_query_metadata_contract(bank, args[0]), *args[1:])
        expected.append((label, name, args, kwargs))
    assert expected == current


@pytest.mark.parametrize("mutation", ["guard", "request", "sql", "order", "metadata", "timeout", "context"])
def test_query_trace_contract_rejects_business_mutations(mutation):
    source = next(script for script in _query_literal_scripts("fubon", _baseline_bank("fubon"))
                  if "submits[0].click()" in script)
    old = [("frame:html", "evaluate", (source, "rdoDay180"), {"timeout": 5000}),
           ("request", "POST", ("ajaxAction=query-action&checkedConvenientPeriod=server-value&javax.faces.ViewState=state-1",), {}),
           ("store", "execute", ("SELECT income FROM twd_transactions",), {})]
    current = deepcopy(old)
    script = _query_metadata_contract("fubon", source)
    if mutation == "guard":
        script = script.replace("preset.checked!==true", "preset.checked===true")
    elif mutation == "order":
        indent = script.split("submits[0].click();", 1)[0].rsplit("\n", 1)[1]
        script = script.replace("window.__hermesFubonSubmitDocument=document;\n" + indent + "submits[0].click();",
                                "submits[0].click();\n" + indent + "window.__hermesFubonSubmitDocument=document;")
    elif mutation == "metadata":
        script = script.replace("FUBON-05-785-001", "FUBON-05-785-999")
    current[0] = ("other-frame" if mutation == "context" else "frame:html", "evaluate",
                  (script, "rdoDay180"), {"timeout": 5001 if mutation == "timeout" else 5000})
    if mutation == "request":
        current[1] = ("request", "GET", ("ajaxAction=other",), {})
    elif mutation == "sql":
        current[2] = ("store", "execute", ("DELETE FROM twd_transactions",), {})
    assert current != [(old[0][0], "evaluate", (_query_metadata_contract("fubon", source), "rdoDay180"), old[0][3]), *old[1:]]
    with pytest.raises(AssertionError):
        _assert_query_traces("fubon", old, current)


@pytest.mark.parametrize("bank,scenario,expected", [
    ("sinopac", "nonempty", None), ("sinopac", "empty", None),
    ("fubon", "normal", None), ("fubon", "companion", None),
    ("fubon", "radio-wrong", "fubon-twd-history-controls-preset"),
    ("fubon", "no-update", "fubon-twd-history-result-freshness"),
    ("fubon", "transport-reject", "fubon-twd-history-transport"),
    ("fubon", "listener-error", "fubon-twd-history-listener-cleanup"),
])
def test_baseline_and_current_actual_query_native_script_traces_match(bank, scenario, expected, monkeypatch):
    from functools import wraps
    from copy import deepcopy
    module = import_module(f"backend.banks.{bank}")
    cls_name = "SinopacCrawler" if bank == "sinopac" else "FubonCrawler"
    method = "_collect_transactions" if bank == "sinopac" else "_collect_twd_window"
    fixtures = import_module(f"tests.test_{bank}_history_sync")
    traces = []
    for version in (_baseline_bank(bank), module):
        cls = getattr(version, cls_name)
        native_method = getattr(cls, method)
        trace = []
        wrapped_ids = set()
        wrapped_objects = []  # Keep ephemeral locator identities alive.
        def instrument(value, label):
            if id(value) in wrapped_ids:
                return value
            wrapped_ids.add(id(value))
            wrapped_objects.append(value)
            for name in ("goto", "wait_for_timeout", "locator", "nth", "count", "is_visible", "is_enabled", "click", "fill", "input_value", "evaluate", "on", "remove_listener"):
                target = getattr(value, name, None)
                if not callable(target):
                    continue
                def call(*args, _target=target, _name=name, **kwargs):
                    recorded = args[:1] if _name in ("on", "remove_listener") else args
                    trace.append((label, _name, deepcopy(recorded), deepcopy(kwargs)))
                    result = _target(*args, **kwargs)
                    if _name in ("locator", "nth"):
                        instrument(result, label + ":" + str(args[0]) if _name == "locator" else label)
                    return result
                setattr(value, name, call)
            return value
        @wraps(native_method)
        def observed(self, page, *args):
            instrument(page, "page")
            if bank == "fubon":
                instrument(args[0], "frame")
                emit = page._emit
                def request_event(event, value):
                    request = value.request if event == "response" else value
                    trace.append(("request-event", event,
                                  (request.method, request.url, request.post_data,
                                   request.frame is args[0]),
                                  {"status": value.status, "headers": deepcopy(value.headers)} if event == "response" else {}))
                    return emit(event, value)
                page._emit = request_event
            try:
                result = native_method(self, page, *args)
            except RuntimeError as error:
                trace.append(("outcome", "error", (type(error).__name__, str(error)), {}))
                raise
            else:
                trace.append(("outcome", "return", (deepcopy(result),), {}))
                if bank == "sinopac":
                    fields = ("method", "url", "raw_url", "req_body", "status", "content_type", "body_size", "request_sequence", "main_frame_request", "resp_json")
                    trace.append(("collector", "hits", tuple(
                        tuple(deepcopy(getattr(hit, field)) for field in fields) for hit in args[0].hits), {}))
                return result
        with monkeypatch.context() as patch:
            # The fixture uses object.__new__: no BankCreds or real constructor.
            # Switch its entire class, not just the containing method on new self.
            patch.setattr(fixtures, cls_name, cls)
            patch.setattr(cls, method, observed)
            if bank == "sinopac":
                drive = fixtures.test_sinopac_collect_uses_native_account_date_and_query_controls
                drive(patch, scenario == "empty", True, 1, scenario == "empty", 0, 0, 0, 0, 0, 0, 0, 0, None)
            else:
                drive = fixtures.test_fubon_settles_all_same_frame_responses_before_dom_publication
                drive(scenario, expected)
        traces.append(trace)
    _assert_query_traces(bank, traces[0], traces[1])
    assert any(item[1] == "evaluate" for item in traces[0])


def test_fubon_alternative_total_proofs_preserve_all_failure_pairs():
    from backend.banks.fubon import FubonCrawler
    from tests.test_fubon_history_sync import _result
    codes = []
    for native, single, count in ((False, False, 0), (True, False, 0), (False, True, 1), (True, True, 2)):
        result = _result("2025-08-30", "2026-02-27", "2025-09-02")
        result["snapshot"].update(nativeTotalFound=native, singlePageProof=single, nativeTotalMarkerCount=count)
        with pytest.raises(RuntimeError) as caught:
            FubonCrawler._validated_twd_history_result(result)
        codes.append(_leaf(caught.value, "fubon"))
    assert len(set(codes)) == len(codes)


def test_fubon_settled_pair_type_and_length_rejections_are_distinct():
    from backend.banks.fubon import _fubon_settled_control_guard, TWD_HISTORY_URL
    settled = dict(value="native", text="account 90000000267053", detail=True, fast=True, preset=True,
                   formBound=True, viewState="state", formAction=TWD_HISTORY_URL)
    option = dict(value="native", identity="90000000267053")
    codes = []
    for role in (0, 1):
        for value in (None, "", "x" * 129):
            pair = ["action", "preset"]
            pair[role] = value
            with pytest.raises(RuntimeError) as caught:
                _fubon_settled_control_guard(settled, option, {tuple(pair)}, raise_on_error=True)
            codes.append(_leaf(caught.value, "fubon"))
    assert len(set(codes)) == len(codes)


def test_sinopac_order_generator_rejection_predicates_are_distinct():
    from backend.banks.sinopac import SinopacCrawler
    from tests.test_sinopac_history_sync import _history_hit, ACCOUNT, LABEL
    codes = []
    for value in ("x", "111"):
        hit = _history_hit()
        hit.resp_json[0]["HeadInfo"][0]["OrderIndex"] = value
        with pytest.raises(RuntimeError) as caught:
            SinopacCrawler._validate_history_hit(hit, label=LABEL, identity=ACCOUNT, currency="TWD", start=date(2026, 8, 1), end=date(2026, 8, 31), business_date="20260831", as_of=date(2026, 8, 31))
        codes.append(_leaf(caught.value, "sinopac"))
    assert len(set(codes)) == len(codes)


def test_sinopac_loan_record_generator_nested_rejections_are_distinct():
    from backend.banks.sinopac import SinopacCrawler
    from backend.core.base import ResponseCollector
    from tests.test_sinopac_loan import _LoanPage
    codes = []
    for field in (None, "LoanKind", "Currency", "LoanBalance"):
        crawler = object.__new__(SinopacCrawler)
        crawler._response_visible = Mock(return_value=False)
        collector = ResponseCollector("sinopac.com")
        page = _LoanPage(collector)
        evaluate = page.evaluate
        def query(*args):
            value = evaluate(*args)
            collector.hits[-1].resp_json = deepcopy(collector.hits[-1].resp_json)
            if field is None:
                collector.hits[-1].resp_json[0]["SubInfo"] = [None]
            else:
                collector.hits[-1].resp_json[0]["SubInfo"][0][field] = None
            return value
        page.evaluate = query
        with pytest.raises(RuntimeError) as caught:
            crawler._collect_loans(page, collector)
        codes.append(_leaf(caught.value, "sinopac"))
        assert len(page.query_args) == 1
    assert len(set(codes)) == len(codes)


def test_sinopac_login_generator_roles_are_distinct():
    codes = []
    for role in range(4):
        crawler, page, fields, submit = _login("sinopac")
        fields[role].is_enabled.return_value = False
        with pytest.raises(RuntimeError) as caught:
            crawler.submit_credentials_once(page)
        codes.append(_leaf(caught.value, "sinopac"))
        submit.click.assert_not_called()
        for later in fields[role + 1:]:
            later.is_enabled.assert_not_called()
    assert len(set(codes)) == len(codes)
    codes = []
    for role in (0, 1, 3):
        crawler, page, fields, submit = _login("sinopac")
        fields[role].is_visible.return_value = False
        with pytest.raises(RuntimeError) as caught:
            crawler.submit_credentials_once(page)
        codes.append(_leaf(caught.value, "sinopac"))
        submit.click.assert_not_called()
    assert len(set(codes)) == len(codes)


def test_sinopac_inventory_form_field_rejections_are_distinct():
    from urllib.parse import urlencode
    from backend.banks.sinopac import SinopacCrawler
    from backend.core.base import ResponseCollector
    from tests.test_sinopac_history_sync import _inventory_hit
    codes = []
    date_keys = {"BusinessDate", "StartDate", "EndDate"}
    for key in SinopacCrawler._HISTORY_FORM_KEYS - date_keys:
        hit = _inventory_hit()
        form = {name: "20260831" if name in date_keys else "" for name in SinopacCrawler._HISTORY_FORM_KEYS}
        form[key] = "synthetic"
        hit.req_body = urlencode(form)
        collector = ResponseCollector("sinopac.com")
        collector.hits.append(hit)
        with pytest.raises(RuntimeError) as caught:
            SinopacCrawler._twd_inventory(collector)
        codes.append(_leaf(caught.value, "sinopac"))
    assert len(set(codes)) == len(codes)


def test_sinopac_header_value_generator_fields_are_distinct():
    from backend.banks.sinopac import SinopacCrawler
    from tests.test_sinopac_history_sync import _history_hit, ACCOUNT, LABEL
    codes = []
    keys = _history_hit().resp_json[0]["HeadInfo"][0].keys()
    for key in keys:
        hit = _history_hit()
        hit.resp_json[0]["HeadInfo"][0][key] = None
        with pytest.raises(RuntimeError) as caught:
            SinopacCrawler._validate_history_hit(hit, label=LABEL, identity=ACCOUNT, currency="TWD", start=date(2026, 8, 1), end=date(2026, 8, 31), business_date="20260831", as_of=date(2026, 8, 31))
        codes.append(_leaf(caught.value, "sinopac"))
    assert len(set(codes)) == len(codes)


@pytest.mark.parametrize("bank", ["sinopac", "fubon"])
def test_ocr_metadata_clears_on_success_new_call_and_retry(bank, monkeypatch):
    module = import_module(f"backend.banks.{bank}")
    cls = module.SinopacCrawler if bank == "sinopac" else module.FubonCrawler
    crawler = object.__new__(cls)
    crawler.captcha_tmp = "synthetic-unused"
    image, scope = Mock(), Mock()
    image.count.return_value = 1
    image.nth.return_value = image
    image.is_visible.return_value = image.is_enabled.return_value = True
    scope.locator.return_value = image
    original = RuntimeError("synthetic-native")
    if bank == "sinopac":
        monkeypatch.setattr(module, "wait_captcha_stable", Mock())
        solver = Mock(side_effect=original)
        monkeypatch.setattr(module, "solve_captcha", solver)
    else:
        solver = Mock(side_effect=original)
        monkeypatch.setattr(module, "ocr_bytes", solver)
    diagnostic = []
    assert crawler._ocr_captcha(scope, max_attempts=1, _diagnostic=diagnostic) is None
    assert diagnostic == [_leaf(original, bank)]
    solver.side_effect = None
    solver.return_value = "123456"
    assert crawler._ocr_captcha(scope, max_attempts=1, _diagnostic=diagnostic) == "123456"
    assert diagnostic == []
    if bank == "sinopac":
        solver.side_effect = [RuntimeError("synthetic-first-attempt"), None]
        assert crawler._ocr_captcha(scope, max_attempts=2, _diagnostic=diagnostic) is None
        assert diagnostic == []
    diagnostic[:] = [_leaf(original, bank)]
    image.is_visible.return_value = False
    assert crawler._ocr_captcha(scope, max_attempts=1, _diagnostic=diagnostic) is None
    assert diagnostic == []


def test_sinopac_rejected_record_iteration_is_one_shot_and_short_circuited():
    from backend.banks.sinopac import SinopacCrawler
    from backend.core.base import ResponseCollector
    from tests.test_sinopac_loan import _LoanPage
    old = _baseline_bank("sinopac").SinopacCrawler._collect_loans
    traces = []
    for method in (old, SinopacCrawler._collect_loans):
        trace = []
        class Record(dict):
            def get(self, key, *default):
                trace.append(("get", key))
                return super().get(key, *default)
        class OneShotRows(list):
            def __iter__(self):
                trace.append(("iter",))
                assert trace.count(("iter",)) == 1
                yield Record(LoanKind=None, Currency="TWD", LoanBalance="1")
                raise AssertionError("consumed a later row after terminal rejection")
        crawler = object.__new__(SinopacCrawler)
        crawler._response_visible = Mock(return_value=False)
        collector = ResponseCollector("sinopac.com")
        page = _LoanPage(collector)
        evaluate = page.evaluate
        def query(*args):
            value = evaluate(*args)
            collector.hits[-1].resp_json = deepcopy(collector.hits[-1].resp_json)
            collector.hits[-1].resp_json[0]["SubInfo"] = OneShotRows([None])
            return value
        page.evaluate = query
        with pytest.raises(RuntimeError, match="sinopac-loan-response-records"):
            method(crawler, page, collector)
        traces.append(trace)
    assert traces == [[("iter",), ("get", "LoanKind")]] * 2


def test_fubon_bounded_match_callers_own_native_suboperation_codes():
    import ast
    from pathlib import Path
    import backend.banks.fubon as module
    tree = ast.parse(Path(module.__file__).read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "bounded_locator_matches"]
    assert calls
    assert all(any(keyword.arg == "error_ids" for keyword in node.keywords) for node in calls)


@pytest.mark.parametrize("bank", ["sinopac", "fubon"])
def test_catalog_is_literal_registered_bank_owned_and_chinese(bank):
    import ast
    from pathlib import Path
    import re
    catalog = import_module(f"backend.core.error_catalog.{bank}")
    codes = ast.literal_eval(ast.parse(Path(catalog.__file__).read_text()).body[1].value)
    assert codes == catalog.CODES
    assert len(codes) < 4096
    for code, (action, reason) in codes.items():
        assert re.fullmatch(bank.upper() + r"-0[1-7]-[0-8][0-9]{2}-[0-9]{3}", code)
        assert 1 <= int(code.split("-")[2]) <= 899
        assert code_details(code, bank=bank) == (action, reason)
        assert re.search(r"[\u4e00-\u9fff]", action)
        assert re.search(r"[\u4e00-\u9fff]", reason)
