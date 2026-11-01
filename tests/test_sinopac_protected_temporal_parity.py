"""Offline SinoPac loan proof against the complete verified 16f2 class.

No browser fixture is used. Keep the protected contract red until every
remaining fragment has a concrete, fail-closed projection.
"""
from __future__ import annotations

import ast
from copy import deepcopy
from datetime import date, datetime, timedelta
from functools import lru_cache
import inspect
import json
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo
from decimal import Decimal

import pytest

import backend.banks.sinopac as current
from tests.test_ordered_sinopac_fubon_codes import _baseline_bank
from tests.test_protected_ast import normalize_tree, registered_codes


def _method(tree, name):
    crawler = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SinopacCrawler")
    return next(n for n in crawler.body if isinstance(n, ast.FunctionDef) and n.name == name)


def test_loan_raise_mapping_is_complete_not_positional_zip():
    """The old contract loses three residual raises; never truncate with zip."""
    old = _baseline_bank("sinopac")
    import inspect
    original = _method(ast.parse(inspect.getsource(old)), "_collect_loans")
    candidate = _method(normalize_tree(Path(current.__file__).read_text(), "sinopac", registered_codes("sinopac")), "_collect_loans")
    original_raises = [n for n in ast.walk(original) if isinstance(n, ast.Raise)]
    from tests.test_sinopac_login_checkpoints import _sinopac_loan_projection
    candidate = _sinopac_loan_projection(candidate, original)
    candidate_raises = [n for n in ast.walk(candidate) if isinstance(n, ast.Raise)]
    assert len(original_raises) == 10
    assert len(candidate_raises) == len(original_raises), "unproved split record/control/native guards remain"


@lru_cache(maxsize=1)
def _original():
    return _baseline_bank("sinopac")


def test_repayment_complete_projection_includes_all_primary_state_references():
    source = Path(current.__file__).read_text()
    original = _method(normalize_tree(inspect.getsource(_original()), "sinopac", registered_codes("sinopac")), "_collect_loan_repayments")
    candidate = _method(normalize_tree(source, "sinopac", registered_codes("sinopac")), "_collect_loan_repayments")
    from tests.test_sinopac_login_checkpoints import _sinopac_repayment_projection
    candidate = _sinopac_repayment_projection(candidate, original)
    assert ast.dump(candidate) == ast.dump(original), "repayment roles and primary-before-finally projection remain unproved"


def test_transaction_lazy_predicates_have_complete_source_projection():
    source = Path(current.__file__).read_text()
    original = _method(normalize_tree(inspect.getsource(_original()), "sinopac", registered_codes("sinopac")), "_collect_transactions")
    candidate = _method(normalize_tree(source, "sinopac", registered_codes("sinopac")), "_collect_transactions")
    from tests.test_sinopac_login_checkpoints import _sinopac_transaction_projection
    candidate = _sinopac_transaction_projection(candidate, original)
    assert ast.dump(candidate) == ast.dump(original), "transaction lazy predicates and state references remain unproved"


@lru_cache(maxsize=1)
def _original_parser():
    """The repayment consumer and ALL its local callees, not current helpers."""
    import linecache
    root = Path(current.__file__).resolve().parents[2]
    path = "backend/core/persist/sinopac.py"
    raw = subprocess.check_output(["git", "show", "16f2d4b130f44973d346b420f3d486ac839f71ac:" + path], cwd=root)
    source = raw.decode()
    tree = ast.parse(source)
    names = {"_parse_sinopac_repayment", "_strict_date", "_today"}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in nodes} == names
    namespace = dict(date=date, datetime=datetime, timedelta=timedelta, Decimal=Decimal,
                     json=json, re=re, ZoneInfo=ZoneInfo, __name__="frozen_sinopac_repayment")
    filename = str(root / path) + ".baseline16f2d4b"
    linecache.cache[filename] = (len(source), None, source.splitlines(keepends=True), filename)
    future = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "__future__"]
    exec(compile(ast.Module(body=future + nodes, type_ignores=[]), filename, "exec"), namespace)
    return namespace


def _exception_projection(error, identities):
    if error is None:
        return None
    assert id(error) not in identities.get("seen", set()), "cyclic exception link"
    identities.setdefault("seen", set()).add(id(error))
    return (type(error).__module__, type(error).__qualname__, str(error),
            identities.get(id(error), "replacement"), error.__suppress_context__,
            _exception_projection(error.__cause__, identities),
            _exception_projection(error.__context__, identities))


def _repayment_run(module, *, changes=None, faults=(), fault_index=None, scope="repayment"):
    """Reuse the synthetic fixture, but bind complete original-class dispatch."""
    import tests.test_sinopac_loan_diagnostics as fixtures
    import backend.core.persist.sinopac as parser
    from backend.core.error_codes import exception_error_id

    trace = []
    injected = {}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(fixtures, "SinopacCrawler", module.SinopacCrawler)
        c = fixtures.repayment_case.__wrapped__(patch)
        patch.setattr(module, "_taipei_today", lambda: date(2026, 9, 7))
        patch.setattr(module, "_LoanRepaymentBodyObserver", lambda *args: observe_constructor(*args))
        if module is not current:
            frozen = _original_parser()
            patch.setitem(frozen, "_today", lambda: date(2026, 9, 7))
            patch.setattr(parser, "_parse_sinopac_repayment", frozen["_parse_sinopac_repayment"])
        if scope == "loan":
            from urllib.parse import urlencode
            c.inventory = deepcopy(fixtures.FIXTURE["account_response"])
            c.info = deepcopy(fixtures.FIXTURE["info_response"])
            c.loan_hit = SimpleNamespace(req_body=urlencode({"AcctValue": c.account["AcctValue"],
                                                            "AcctValueFormat": c.account["AcctValueFormat"]}),
                                         resp_json=c.info, status=200)
            hits = []
            c.collector.latest.side_effect = lambda endpoint: SimpleNamespace(resp_json=c.inventory)
            c.collector.by_endpoint.side_effect = lambda endpoint: list(hits)
            c.page.goto.side_effect = lambda *args, **kwargs: setattr(c.page, "url", "https://mma.sinopac.com/mma/bank/easy_index_loan/mma_loandetail.aspx")
            c.page.go_back.side_effect = lambda **kwargs: setattr(c.page, "url", "https://mma.sinopac.com/mma/bank/easy_index_loan/mma_loandetail.aspx")
            evaluate = c.page.evaluate.side_effect
            def loan_evaluate(script, args=None):
                if args is not None:
                    hits.append(c.loan_hit)
                    return True
                return evaluate(script)
            c.page.evaluate.side_effect = loan_evaluate
        if changes:
            changes(c)

        roles = {id(c.page): "page", id(c.observer): "observer", id(c.collector): "collector",
                 id(c.response): "response", id(c.page.main_frame): "main-frame"}
        handlers = []
        listeners = {}
        fault_map = dict(faults)

        loan_scripts = []
        if scope == "loan":
            for source_module in (_original(), current):
                method = _method(ast.parse(inspect.getsource(source_module)), "_collect_loans")
                loan_scripts.append(next(n.value for n in ast.walk(method) if isinstance(n, ast.Constant)
                                         and isinstance(n.value, str) and 'const account = document.querySelector' in n.value))
            before = 'if (!account || !formatted || !button) return false;'
            after = "if (!account) return {errorId:'SINOPAC-05-780-001'};\n                  if (!formatted) return {errorId:'SINOPAC-05-780-002'};\n                  if (!button) return {errorId:'SINOPAC-05-780-003'};"
            assert loan_scripts[1] == loan_scripts[0].replace(before, after)

        def value(v):
            if isinstance(v, str) and loan_scripts and v == loan_scripts[1]:
                return loan_scripts[0]
            if id(v) in roles:
                return roles[id(v)]
            if callable(v):
                if v not in handlers:
                    handlers.append(v)
                return "handler-" + str(handlers.index(v))
            if isinstance(v, (list, tuple)):
                return tuple(value(x) for x in v)
            if isinstance(v, dict):
                return tuple((k, value(x)) for k, x in v.items())
            if isinstance(v, Mock):
                return "native-mock"
            return v

        def emit(role, args=(), kwargs=None):
            trace.append((role, value(args), value(kwargs or {})))
            error = fault_map.get(role)
            if fault_index == len(trace):
                error = RuntimeError("indexed-native-fault")
            if error is not None:
                injected[id(error)] = role
                raise error

        def wrap(obj, name, role):
            native = getattr(obj, name)
            def call(*args, **kwargs):
                emit(role, args, kwargs)
                if role == "page.on":
                    event, handler = args
                    def callback(raw_response):
                        assert raw_response is c.response
                        emit("callback.admit", (raw_response,))
                        return handler(response)
                    listeners[handler] = callback
                    return native(event, callback, **kwargs)
                if role == "page.remove_listener":
                    event, handler = args
                    assert handler in listeners
                    return native(event, listeners[handler], **kwargs)
                if role == "observer.read":
                    assert args[0] is response and args[1] is c.page.main_frame
                return native(*args, **kwargs)
            patch.setattr(obj, name, call)

        page_methods = ["locator", "wait_for_url", "evaluate", "on", "remove_listener", "wait_for_timeout"]
        if scope == "loan":
            page_methods += ["goto", "go_back"]
        for name in page_methods:
            wrap(c.page, name, "page." + name)
        for selector, control in c.controls.items():
            for name in ("count", "nth", "input_value", "is_visible", "is_enabled", "inner_text", "click"):
                wrap(control, name, selector + "." + name)
        for name in ("get_attribute", "inner_text", "is_visible", "click"):
            wrap(c.link, name, "link." + name)
        for name in ("start", "read", "close"):
            wrap(c.observer, name, "observer." + name)
        for name in (("detach", "attach", "latest", "by_endpoint") if scope == "loan" else ("detach", "attach")):
            wrap(c.collector, name, "collector." + name)
        wrap(parser, "_parse_sinopac_repayment", "parse")

        def observe_constructor(*args):
            emit("observer.constructor", args)
            return c.observer

        class Reads:
            def __init__(self, target, label, fields):
                self.target, self.label, self.fields = target, label, fields
            def __getattr__(self, name):
                if name in self.fields:
                    emit(self.label + "." + name)
                return getattr(self.target, name)

        request = Reads(c.response.request, "request", {"post_data"})
        c.response.request = request
        response = Reads(c.response, "response", {"request", "url", "headers"})
        roles[id(response)] = "response"
        roles[id(request)] = "request"
        page = Reads(c.page, "page", {"url", "frames", "main_frame"})
        roles[id(page)] = "page"
        result = error = None
        try:
            action = c.crawler._collect_loans if scope == "loan" else c.crawler._collect_loan_repayments
            result = action(page, c.collector) if scope == "loan" else action(page, c.collector, c.account, c.record)
        except BaseException as caught:
            error = caught
        return SimpleNamespace(result=result, error=error, trace=trace, injected=injected,
                               projection=_exception_projection(error, dict(injected)),
                               error_id=exception_error_id(error, bank="sinopac") if error else None)


@pytest.mark.parametrize("primary_type,cleanup,cleanup_type", [
    (primary, cleanup, cleanup_type)
    for primary in (None, RuntimeError, __import__('asyncio').CancelledError, BaseException)
    for cleanup in (None, "page.remove_listener", "observer.close", "collector.attach")
    for cleanup_type in ((RuntimeError,) if cleanup is None else (RuntimeError, __import__('asyncio').CancelledError, BaseException))
])
def test_primary_before_finally_preserves_native_cleanup_policy(primary_type, cleanup, cleanup_type):
    from backend.core.error_codes import exception_error_id
    runs = []
    for module in (_original(), current):
        primary = primary_type("primary-native") if primary_type else None
        cleanup_error = cleanup_type("cleanup-native") if cleanup else None
        faults = []
        if primary:
            faults.append(("observer.read", primary))
        if cleanup_error:
            faults.append((cleanup, cleanup_error))
        run = _repayment_run(module, faults=faults)
        runs.append(run)
        if not primary and not cleanup_error:
            assert run.error is None and run.result is not None
        else:
            escaping = cleanup_error or primary
            assert run.error is not None
            if isinstance(escaping, Exception):
                assert type(run.error) is RuntimeError
                assert str(run.error) == "sinopac-loan-repayments"
                assert run.error.__context__ is escaping
                assert run.error.__cause__ is None and run.error.__suppress_context__ is True
            else:
                assert run.error is escaping
            if cleanup_error and primary:
                assert cleanup_error.__context__ is primary
        cleanups = [t[0] for t in run.trace if t[0] in {"page.remove_listener", "observer.close", "collector.attach"}]
        expected = ["page.remove_listener", "observer.close", "collector.attach"]
        assert cleanups == expected[:expected.index(cleanup) + 1] if cleanup else cleanups == expected
        if module is current and primary and isinstance(primary, Exception):
            assert run.error_id == exception_error_id(primary, bank="sinopac")
    assert runs[0].trace == runs[1].trace
    assert runs[0].result == runs[1].result
    assert runs[0].projection == runs[1].projection


def test_repayment_each_native_fault_retains_exact_trace_identity_and_links(request):
    old, candidate = [_repayment_run(module) for module in (_original(), current)]
    assert old.result == candidate.result
    assert old.error is candidate.error is None
    assert old.trace == candidate.trace
    assert old.trace
    request.node.user_properties.append(("native_fault_points", str(len(old.trace))))
    for index in range(1, len(old.trace) + 1):
        old, candidate = [_repayment_run(module, fault_index=index) for module in (_original(), current)]
        assert old.trace == candidate.trace, index
        assert old.result == candidate.result, index
        assert old.projection == candidate.projection, index
        assert old.error is not None and candidate.error is not None, index


def test_joined_loan_repayment_native_faults_and_public_results(request):
    old, candidate = [_repayment_run(module, scope="loan") for module in (_original(), current)]
    assert old.error is candidate.error is None, (old.projection, candidate.projection)
    assert old.trace == candidate.trace
    assert old.result == candidate.result
    assert old.result["fetch_ok"] is True
    assert len(old.result["details"][0]["repayments"]) == 1
    assert sum(t[0] == "#btnQuery.click" for t in old.trace) == 1
    assert sum(t[0] == "page.go_back" for t in old.trace) == 1
    request.node.user_properties.append(("native_fault_points", str(len(old.trace))))
    for index in range(1, len(old.trace) + 1):
        old, candidate = [_repayment_run(module, scope="loan", fault_index=index) for module in (_original(), current)]
        assert old.trace == candidate.trace, index
        assert old.result == candidate.result, index
        assert old.projection == candidate.projection, index
        assert old.error is not None and candidate.error is not None, index


@pytest.mark.parametrize("field", ["inventory", "info"])
@pytest.mark.parametrize("payload", [None, False, 0, "not-an-envelope", {}, [], [None], [False], [{}], [{"SubInfo": None}], [{"SubInfo": {}}]])
def test_loan_envelopes_keep_original_short_circuit_and_native_admission(field, payload):
    def change(c):
        if field == "inventory":
            c.inventory = deepcopy(payload)
        else:
            c.loan_hit.resp_json = deepcopy(payload)
    old, candidate = [_repayment_run(module, scope="loan", changes=change) for module in (_original(), current)]
    assert old.projection == candidate.projection
    assert old.trace == candidate.trace
    assert old.error is not None and candidate.error is not None
    assert not any(t[0] == "link.click" for t in old.trace)


@pytest.mark.parametrize("kind", ["row", "account", "formatted", "message", "empty", "record", "LoanKind", "Currency", "LoanBalance", "request", "request-nonobject", "http", "restore-count", "restore-value"])
def test_loan_records_identity_request_and_restore_guards_keep_old_order(kind):
    def change(c):
        if kind == "row":
            c.inventory[0]["SubInfo"] = [None]
        elif kind in {"account", "formatted"}:
            c.inventory[0]["SubInfo"][0]["AcctValue" if kind == "account" else "AcctValueFormat"] = ""
        elif kind == "message":
            c.info[0]["Message"] = "synthetic failure"
        elif kind == "empty":
            c.info[0]["SubInfo"] = []
        elif kind == "record":
            c.info[0]["SubInfo"] = [None]
        elif kind in {"LoanKind", "Currency", "LoanBalance"}:
            c.info[0]["SubInfo"][0][kind] = ""
        elif kind.startswith("request"):
            c.loan_hit.req_body = "AcctValue=other" if kind == "request" else None
        elif kind == "http":
            c.loan_hit.status = 500
        else:
            native = c.page.go_back.side_effect
            count = c.controls["#AcctValue"].count
            read = c.controls["#AcctValue"].input_value
            def restore(**kwargs):
                native(**kwargs)
                if kind == "restore-count":
                    count.return_value = 2
                else:
                    read.return_value = "other"
            c.page.go_back.side_effect = restore
    old, candidate = [_repayment_run(module, scope="loan", changes=change) for module in (_original(), current)]
    assert old.projection == candidate.projection
    assert old.trace == candidate.trace
    assert old.error is not None and candidate.error is not None


@pytest.mark.parametrize("field,value", [("StartDate", None), ("StartDate", "bad"), ("StartDate", "20260230"), ("EndDate", None), ("EndDate", "bad"), ("EndDate", "20260230"), ("LNALTNO", "99-0002"), ("LNMAINACNO", "9"), ("CURRENCY", "USD"), ("TextType", None), ("TextType", "x" * 2001)])
def test_repayment_source_owned_date_and_form_forwarding(field, value):
    def change(c):
        for entry in c.state["form"]:
            if entry[0] == field:
                entry[1] = value
    old, candidate = [_repayment_run(module, changes=change) for module in (_original(), current)]
    assert old.projection == candidate.projection
    assert old.trace == candidate.trace
    assert candidate.error is not None
    assert not any(t[0] == "collector.detach" for t in candidate.trace)
    if field in {"StartDate", "EndDate"}:
        reason = 1 if value is None else 2 if value == "bad" else 3
        assert candidate.error_id == f"SINOPAC-05-{151 if field == 'StartDate' else 152}-{reason:03}"


@pytest.mark.parametrize("field,value", [("DataValue1", "2026/02/30"), ("DataValue2", "2026/02/30"), ("DataValue3", "10,301"), ("DataValue4", "30,0"), ("DataValue6", "-1"), ("DataValue11", "NaN")])
def test_joined_repayment_amount_and_date_validation_uses_frozen_original_consumer(field, value):
    def change(c):
        row = c.state["payload"][0]["SubInfo"][0]
        row[field] = value
        c.state["dom"]["rows"] = [[row[f"DataValue{i}"] for i in (2, 1, 11, 4, 5, 6, 3, 10)]]
    old, candidate = [_repayment_run(module, scope="loan", changes=change) for module in (_original(), current)]
    assert old.projection == candidate.projection
    assert old.trace == candidate.trace
    assert candidate.error is not None
    assert any(t[0] == "parse" for t in candidate.trace)
    assert not any(t[0] == "page.go_back" for t in candidate.trace)


@pytest.mark.parametrize("selector", ["#StartDate", "#EndDate", "#btnQuery"])
@pytest.mark.parametrize("operation,rejected", [("count", 0), ("count", 2), ("is_visible", False), ("is_enabled", False)])
def test_source_owned_query_control_tuples_keep_short_circuit_counts(selector, operation, rejected):
    def change(c):
        getattr(c.controls[selector], operation).return_value = rejected
    old, candidate = [_repayment_run(module, changes=change) for module in (_original(), current)]
    assert old.projection == candidate.projection
    assert old.trace == candidate.trace
    assert candidate.error is not None
    action = {"#StartDate": 740, "#EndDate": 750, "#btnQuery": 760}[selector]
    reason = {"count": 5, "is_visible": 6, "is_enabled": 7}[operation]
    assert candidate.error_id == f"SINOPAC-05-{action}-{reason:03}"
    assert not any(t[0] == "collector.detach" for t in candidate.trace)
    calls = [t[0] for t in candidate.trace]
    if operation == "count":
        assert selector + ".is_visible" not in calls
        assert selector + ".is_enabled" not in calls
    elif operation == "is_visible":
        assert selector + ".is_enabled" not in calls


def test_empty_loan_inventory_is_positive_not_an_envelope_rejection():
    def change(c):
        c.inventory[0]["SubInfo"] = []
    old, candidate = [_repayment_run(module, scope="loan", changes=change) for module in (_original(), current)]
    assert old.result == candidate.result == {"details": [], "fetch_ok": True}
    assert old.trace == candidate.trace
    assert old.error is candidate.error is None
    assert not any(t[0] in {"page.evaluate", "link.click", "collector.detach"} for t in candidate.trace)


@pytest.mark.parametrize("payload", [None, False, 0, "bad", {}, [], [None], [False], [{}], [{"SubInfo": []}], [{"SubInfo": [None]}]])
def test_repayment_calldata_and_record_envelopes_remain_fail_closed(payload):
    def change(c):
        c.state["payload"] = deepcopy(payload)
    old, candidate = [_repayment_run(module, changes=change) for module in (_original(), current)]
    assert old.projection == candidate.projection
    assert old.trace == candidate.trace
    assert old.error is not None and candidate.error is not None
    assert [t[0] for t in candidate.trace][-3:] == ["page.remove_listener", "observer.close", "collector.attach"]


@pytest.mark.parametrize("key", ["HeadText", "HeadAlign", "DataAlign", "MainShow", "DetailShow", "FieldKey", "OrderIndex", "FieldWidth"])
@pytest.mark.parametrize("value", [None, False, 1, [], {}, "x" * 2001])
def test_repayment_each_metadata_field_preserves_value_iteration_and_admission(key, value):
    def change(c):
        c.state["payload"][0]["HeadInfo"][1][key] = value
    old, candidate = [_repayment_run(module, changes=change) for module in (_original(), current)]
    assert old.projection == candidate.projection
    assert old.trace == candidate.trace
    assert candidate.error is not None
    assert not any(t[0] == "parse" for t in candidate.trace)
    action = 730 + ["HeadText", "HeadAlign", "DataAlign", "MainShow", "DetailShow", "FieldKey", "OrderIndex", "FieldWidth"].index(key)
    reason = 2 if isinstance(value, str) else 1
    assert candidate.error_id == f"SINOPAC-05-{action}-{reason:03}"


@pytest.mark.parametrize("message", ["", "native nonempty notice", "x" * 2000])
def test_joined_loan_keeps_native_nonempty_message_and_exact_single_query(message):
    def change(c):
        c.state["payload"][0]["Message"] = message
    old, candidate = [_repayment_run(module, scope="loan", changes=change) for module in (_original(), current)]
    assert old.projection == candidate.projection is None
    assert old.trace == candidate.trace
    assert old.result == candidate.result
    assert sum(t[0] == "#btnQuery.click" for t in candidate.trace) == 1


@pytest.mark.parametrize("failure", ["inventory", "identity", "record", "restore"])
def test_loan_native_getter_and_predicate_counts_are_original_order(failure):
    traces = []
    for module in (_original(), current):
        trace = []
        class Reads(dict):
            def get(self, key, *default):
                trace.append(("get", key))
                return super().get(key, *default)
            def __getitem__(self, key):
                trace.append(("item", key))
                return super().__getitem__(key)
        class Items(list):
            def __bool__(self):
                trace.append(("bool", len(self)))
                return len(self) > 0
            def __getitem__(self, key):
                trace.append(("index", key))
                return super().__getitem__(key)
            def __iter__(self):
                trace.append(("iter",))
                return super().__iter__()
        def change(c):
            c.inventory = Items([Reads(c.inventory[0])])
            account = Reads(c.inventory[0]["SubInfo"][0])
            c.inventory[0]["SubInfo"] = Items([account])
            record = Reads(c.info[0]["SubInfo"][0])
            c.loan_hit.resp_json = Items([Reads({**c.info[0], "SubInfo": Items([record])})])
            if failure == "inventory":
                c.inventory = Items([])
            elif failure == "identity":
                account["AcctValueFormat"] = ""
            elif failure == "record":
                record["LoanBalance"] = ""
            # Restore is the positive full path; account reads still stay exact.
        run = _repayment_run(module, scope="loan", changes=change)
        if failure != "restore":
            assert run.error is not None
        else:
            assert run.error is None
        traces.append(trace)
    assert traces[0] == traces[1]


def _transaction_run(empty, changes=(), *, fault_index=None, recover=False):
    import tests.test_sinopac_history_sync as fixtures
    from tests.test_sinopac_fubon_terminal_residual_codes import _observe_native_page
    from functools import wraps
    runs = []
    for module in (_original(), current):
        trace, getters, outcome, metadata_reads = [], [], [], []
        native_fault = RuntimeError("state-native-fault")
        native = module.SinopacCrawler._collect_transactions
        polls = 0
        class State(dict):
            final_result = False
            def read(self, operation, key):
                getters.append((operation, key))
                if len(getters) == fault_index:
                    raise native_fault
            def get(self, key, *default):
                if key == 'errorId':
                    # Exactly the source-owned final-result diagnostic read, not
                    # a business getter/native SDK call. Polling reads fail closed.
                    assert self.final_result is True and module is current
                    metadata_reads.append(('final-result', 'get', key))
                    assert len(metadata_reads) == 1
                    return super().get(key, *default)
                self.read('get', key)
                return super().get(key, *default)
            def __getitem__(self, key):
                self.read('item', key)
                return super().__getitem__(key)
        @wraps(native)
        def parent(self, page, collector):
            nonlocal polls
            _observe_native_page(page, trace)
            evaluate = page.evaluate
            def observed(expression, arg=None):
                nonlocal polls
                result = evaluate(expression, arg)
                if isinstance(result, dict) and 'signature' in result:
                    polls += 1
                    result = State(result)
                    result.final_result = 'const cleanup=()=>{' in expression
                    if not recover or polls == 1:
                        result.update(changes)
                return result
            page.evaluate = observed
            try:
                result = native(self, page, collector)
                outcome.append(result)
                return result
            except BaseException as error:
                outcome.append(error)
                raise
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(fixtures, 'SinopacCrawler', module.SinopacCrawler)
            patch.setattr(module.SinopacCrawler, '_collect_transactions', parent)
            try:
                fixtures.test_sinopac_collect_uses_native_account_date_and_query_controls(
                    patch, empty, True, 1, empty, 0, 0, 0, 0, 0, 0, 0, 0, None)
            except BaseException as error:
                assert outcome and outcome[-1] is error
            assert len(outcome) == 1
        value = outcome[0]
        if isinstance(value, BaseException):
            result, error = None, value
        else:
            result, error = value, None
        assert metadata_reads == ([('final-result', 'get', 'errorId')] if module is current and error is None else [])
        runs.append((trace, getters, result, _exception_projection(error, {id(native_fault): 'state-native'}), error))
    assert runs[0][:4] == runs[1][:4]
    return runs


@pytest.mark.parametrize('empty', [False, True])
@pytest.mark.parametrize('changes', [
    {'pagers': 1}, {'errors': 1}, {'mutations': None}, {'mutations': -1},
    {'bound': False}, {'tables': 2}, {'visible': False}, {'rows': None},
    {'visibleRows': None}, {'signature': None}, {'signature': []}, {'signature': [None, None]},
])
def test_containing_transaction_common_and_state_predicates_keep_exact_getter_order(empty, changes):
    runs = _transaction_run(empty, changes.items())
    assert all(run[-1] is not None for run in runs)


@pytest.mark.parametrize('changes', [
    {'mutations': 0}, {'emptyMarker': True}, {'freshEmpty': True}, {'rows': 0},
    {'rows': 3}, {'visibleRows': 0}, {'signature': [1, 1]},
])
def test_containing_transaction_nonempty_lazy_predicates_keep_exact_getter_order(changes):
    runs = _transaction_run(False, changes.items())
    assert all(run[-1] is not None for run in runs)


@pytest.mark.parametrize('changes', [
    {'hiddenRows': None}, {'hiddenNonemptyRows': None}, {'hiddenChangedRows': None},
    {'hiddenChangedPriorRows': None}, {'rows': 0}, {'rows': 2},
    {'hiddenChangedRows': 1}, {'rows': 2, 'visibleRows': 2}, {'emptyMarker': False},
])
def test_containing_transaction_empty_lazy_predicates_keep_exact_getter_order(changes):
    runs = _transaction_run(True, changes.items())
    assert all(run[-1] is not None for run in runs)


@pytest.mark.parametrize('empty', [False, True])
def test_containing_transaction_all_success_getter_faults_and_recovery(empty, request):
    runs = _transaction_run(empty)
    assert all(run[-1] is None for run in runs)
    count = len(runs[0][1])
    assert count > 0
    request.node.user_properties.append(('native_getter_fault_points', str(count)))
    for index in range(1, count + 1):
        errors = _transaction_run(empty, fault_index=index)
        assert all(run[-1] is not None for run in errors)
    recovered = _transaction_run(empty, [('bound', False)], recover=True)
    assert all(run[-1] is None for run in recovered)


@pytest.mark.parametrize("before,after", [
    ("200 <= hit.status < 300", "200 <= hit.status < 301"),
    ('params.get("AcctValue") == [account_no]', 'params.get("AcctValue") == [formatted]'),
    ('page.go_back(wait_until="domcontentloaded", timeout=15_000)', 'page.go_back(wait_until="domcontentloaded", timeout=15_001)'),
    ('loan_control_reason = clicked[\'errorId\']', "loan_control_reason = other_source"),
    ("SINOPAC-05-235-102", "SINOPAC-05-235-103"),
    ('return {"details": details, "fetch_ok": True}', 'return {"details": details, "fetch_ok": False}'),
    ("const account = document.querySelector('#AcctValue');", "const account = document.querySelector('#Other');"),
    ('if not account_no:', 'if account_no:'),
])
def test_exact_loan_adapter_rejects_native_request_context_metadata_and_state_mutations(before, after):
    from tests.test_sinopac_login_checkpoints import _sinopac_loan_projection
    source = Path(current.__file__).read_text()
    assert source.count(before) == 1
    original = _method(ast.parse(inspect.getsource(_original())), "_collect_loans")
    candidate = _method(normalize_tree(source.replace(before, after, 1), "sinopac", registered_codes("sinopac")), "_collect_loans")
    with pytest.raises(AssertionError):
        _sinopac_loan_projection(candidate, original)


@pytest.mark.parametrize('name,before,after', [
    ('_collect_loan_repayments', 'primary_error_id = exception_error_id(source_error, bank=\'sinopac\')', 'primary_error_id = exception_error_id(cleanup_error, bank=\'sinopac\')'),
    ('_collect_loan_repayments', "error_action='SINOPAC-05-151'", "error_action='SINOPAC-05-152'"),
    ('_collect_loan_repayments', "('#StartDate', 'SINOPAC-05-740')", "('#EndDate', 'SINOPAC-05-740')"),
    ('_collect_loan_repayments', "'HeadText': 'SINOPAC-05-730'", "'HeadText': 'SINOPAC-05-731'"),
    ('_collect_loan_repayments', 'observer.close()', 'observer.close_wrong_context()'),
    ('_collect_loan_repayments', 'collector.attach(page)', 'collector.attach(other_page)'),
    ('_collect_loan_repayments', 'parse_qs(request.post_data, keep_blank_values=True)', 'parse_qs(request.other_data, keep_blank_values=True)'),
    ('_collect_loan_repayments', 'observer.read(response, page.main_frame, LOAN_DETAIL_URL, 5000000, 1)', 'observer.read(response, page.other_frame, LOAN_DETAIL_URL, 5000000, 1)'),
    ('_collect_transactions', "(start_control, 'SINOPAC-05-700')", "(end_control, 'SINOPAC-05-700')"),
    ('_collect_transactions', "('SINOPAC-05-600-001', lambda: not isinstance(dom_state, dict))", "('SINOPAC-05-600-001', lambda: isinstance(dom_state, dict))"),
    ('_collect_transactions', "'table-count': 'SINOPAC-05-506-001'", "'table-count': 'SINOPAC-05-507-001'"),
    ('_collect_transactions', 'last_dom_detail_id = dom_error_id', 'last_dom_detail_id = other_source'),
    ('_collect_transactions', 'last_dom_detail_id = None', 'last_dom_detail_id = existing_state'),
    ('_collect_transactions', "dom_error_id = predicate_error_id", "dom_error_id = 'SINOPAC-05-600-001'"),
    ('_collect_transactions', 'common_invalid = False', 'common_invalid = True'),
    ('_collect_transactions', "final_dom.get('errorId')", "dom.get('errorId')"),
])
def test_exact_role_projections_reject_wrong_sources_native_guards_context_and_state(name, before, after):
    from tests.test_sinopac_login_checkpoints import _sinopac_repayment_projection, _sinopac_transaction_projection
    original = _method(normalize_tree(inspect.getsource(_original()), 'sinopac', registered_codes('sinopac')), name)
    candidate = _method(normalize_tree(Path(current.__file__).read_text(), 'sinopac', registered_codes('sinopac')), name)
    source = ast.unparse(candidate)
    assert source.count(before) == (3 if before == 'dom_error_id = predicate_error_id' else 1)
    candidate = ast.parse(source.replace(before, after, 1)).body[0]
    project = _sinopac_repayment_projection if name == '_collect_loan_repayments' else _sinopac_transaction_projection
    with pytest.raises(AssertionError):
        project(candidate, original)


def _temporal_mutant(kind):
    """Compile a full isolated candidate class; never patch production source."""
    from types import ModuleType
    source = Path(current.__file__).read_text()
    tree = ast.parse(source)
    method = _method(tree, "_collect_loan_repayments")
    if kind == "priority":
        assignment = next(n for n in ast.walk(method) if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == "primary_error_id" for t in n.targets)
                          and isinstance(n.value, ast.Call))
        assignment.value = ast.Constant(None)
    elif kind == "wrong-source":
        assignment = next(n for n in ast.walk(method) if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == "primary_error_id" for t in n.targets)
                          and isinstance(n.value, ast.Call))
        assignment.value.args[0] = ast.Name(id="cleanup_error", ctx=ast.Load())
    else:
        cleanup = next(n for n in ast.walk(method) if isinstance(n, ast.Try) and len(n.finalbody) == 3)
        cleanup.finalbody[0], cleanup.finalbody[1] = cleanup.finalbody[1], cleanup.finalbody[0]
    module = ModuleType("backend.banks._synthetic_temporal_mutant")
    module.__file__ = current.__file__
    exec(compile(ast.fix_missing_locations(tree), current.__file__, "exec"), module.__dict__)
    return module


@pytest.mark.parametrize("kind", ["priority", "wrong-source", "cleanup-order"])
def test_temporal_oracle_rejects_primary_classification_and_cleanup_order_mutations(kind):
    def run(module):
        return _repayment_run(module, faults=(("observer.read", RuntimeError("primary-native")),
                                               ("observer.close", RuntimeError("cleanup-native"))))
    expected = run(current)
    assert expected.error_id == "SINOPAC-05-299-001"
    mutant = run(_temporal_mutant(kind))
    with pytest.raises(AssertionError):
        assert (mutant.trace, mutant.projection, mutant.error_id) == (expected.trace, expected.projection, expected.error_id)
