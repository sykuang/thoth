"""Finite whole-call-graph HSBC proof; no browser, native fetch, or bank data."""
from copy import deepcopy
from datetime import date
import ast
import hashlib
from pathlib import Path
import subprocess
import textwrap


import pytest

from tests.test_hsbc_stream_diagnostic_parity import (
    NodePage, TracedDict, collector, crawler, current, original as original, outcome,
)

PROOFS = []


@pytest.mark.parametrize("fault", [None, "bytes", "body", "success", "error", "payload"])
@pytest.mark.parametrize("budgeted", [False, True])
def test_fetch_json_native_getter_and_budget_order(original, monkeypatch, fault, budgeted):
    observed = []
    for module in (original, current):
        trace = []
        class Budget(list):
            def __getitem__(self, key):
                trace.append(["budget", "get", key])
                return super().__getitem__(key)
            def __setitem__(self, key, value):
                trace.append(["budget", "set", key, value])
                return super().__setitem__(key, value)
        body = TracedDict({"success": True, "error": None, "payload": ["synthetic"]}, trace, "body", fault)
        packet = TracedDict({"bytes": 20, "body": body}, trace, "response", fault)
        logs = []
        def fetch(_page, **kwargs):
            native_kwargs = {k: v for k, v in kwargs.items() if k not in {"diagnostics", "diagnostic_role"}}
            trace.append(["fetch", native_kwargs])
            return packet
        budget = Budget([500]) if budgeted else None
        with monkeypatch.context() as patch:
            patch.setattr(module.HsbcCrawler, "_fetch_api_page", fetch)
            patch.setattr(module, "_log", logs.append)
            value = module.HsbcCrawler._fetch_json(None, "cards/synthetic-card", "Bearer synthetic", byte_budget=budget)
        observed.append({"value": value, "budget": list(budget) if budget is not None else None,
                         "trace": trace, "logs": logs})
    assert observed[0]["value"] == observed[1]["value"]
    assert observed[0]["trace"] == observed[1]["trace"]
    assert observed[0]["budget"] == observed[1]["budget"]
    assert observed[0]["logs"] == (["[fetch] HSBC API request failed"] if fault else [])
    assert observed[1]["logs"] == (["[fetch] HSBC API request failed [HSBC-06-640-011]"] if fault else [])
    PROOFS.append({"kind": "fetch-json-native-getters", "fault": fault, "budgeted": budgeted,
                   "old": observed[0], "new": observed[1]})


def test_collect_complete_original_and_current_graph(original, monkeypatch, capsys):
    monkeypatch.setattr(current.time, "monotonic", lambda: 10.0)
    observed = []
    for module in (original, current):
        page = NodePage()
        obj = crawler(module)
        captured = collector()
        captured.detach = lambda target: page.waits.append("detach")
        value = obj.collect(page, captured)
        observed.append({"result": value, "calls": page.calls, "waits": page.waits,
                         "stderr": capsys.readouterr().err})
    assert observed[0] == observed[1]
    assert observed[1]["waits"] == [7000, "detach"]
    base = "https://card.hsbc.com.tw/ibk-bff/api/v1/cards"
    posted = base + "/synthetic-card/transactions/posted?pageSize=10&pageNumber=0"
    assert [call["args"]["url"] for call in observed[1]["calls"]] == [
        base, base + "/synthetic-card", posted, posted,
        base + "/synthetic-card/transactions/unposted?pageSize=200", base,
    ]
    PROOFS.append({"kind": "complete-collect", "old": observed[0], "new": observed[1]})


@pytest.mark.parametrize("path", [None, 42, b"cards/synthetic-card"])
def test_fetch_json_keeps_original_path_validation_exception(original, path):
    observed = []
    for module in (original, current):
        page = NodePage()
        result = outcome(lambda: module.HsbcCrawler._fetch_json(page, path))
        observed.append({"error": result.get("error"), "calls": page.calls})
    assert observed[0] == observed[1]
    PROOFS.append({"kind": "path-type", "type": type(path).__name__, "old": observed[0], "new": observed[1]})


def test_fetch_json_metadata_does_not_read_nonstring_class_descriptor(original):
    observed = []
    for module in (original, current):
        trace = []
        class PathValue:
            @property
            def __class__(self):
                trace.append("class-descriptor")
                raise RuntimeError("synthetic-class-callback")
        result = outcome(lambda: module.HsbcCrawler._fetch_json(None, PathValue()))
        observed.append({"error": result.get("error"), "trace": trace})
    assert observed[0] == observed[1]
    PROOFS.append({"kind": "path-class-descriptor", "old": observed[0], "new": observed[1]})


def test_fetch_json_metadata_does_not_invoke_path_contains(original):
    observed = []
    for module in (original, current):
        trace = []
        class PathValue(str):
            def __contains__(self, value):
                trace.append(["contains", value])
                raise RuntimeError("synthetic-path-callback")
        page = NodePage()
        value = outcome(lambda: module.HsbcCrawler._fetch_json(page, PathValue("cards/synthetic-card"), "Bearer synthetic"))
        observed.append({"value": value, "trace": trace, "calls": page.calls})
    assert observed[0] == observed[1]
    PROOFS.append({"kind": "path-callback", "old": observed[0], "new": observed[1]})


@pytest.mark.parametrize("raises", [False, True])
def test_original_fetch_json_logger_remains_unsuppressed(original, monkeypatch, raises):
    observed = []
    for module in (original, current):
        trace = []
        native = OSError("synthetic-log-failure")
        def log(message):
            trace.append(message)
            if raises:
                raise native
        def fetch(*args, **kwargs):
            raise TimeoutError("synthetic-fetch-failure")
        with monkeypatch.context() as patch:
            patch.setattr(module, "_log", log)
            patch.setattr(module.HsbcCrawler, "_fetch_api_page", fetch)
            try:
                value = module.HsbcCrawler._fetch_json(None, "cards/synthetic-card", "Bearer synthetic")
                result = {"value": value}
            except OSError as error:
                assert error is native
                result = {"error": [type(error).__name__, str(error)]}
        observed.append({"result": result, "log": trace})
    assert observed[0]["result"] == observed[1]["result"]
    assert observed[0]["log"] == ["[fetch] HSBC API request failed"]
    assert observed[1]["log"] == ["[fetch] HSBC API request failed [HSBC-06-640-011]"]
    PROOFS.append({"kind": "old-logger-fallibility", "raises": raises, "old": observed[0], "new": observed[1]})


@pytest.mark.parametrize("raw", [None, "invalid", "2026-02-30T00:00", "2026-01-02T03:04"])
def test_api_date_default_and_transaction_role_preserve_public_result(original, raw):
    old = outcome(lambda: original.HsbcCrawler._api_date(raw))
    for extra in ({}, {"field_role": "transaction"}):
        new = outcome(lambda: current.HsbcCrawler._api_date(raw, **extra))
        assert old.get("result") == new.get("result")
        assert old.get("error") == new.get("error")
        PROOFS.append({"kind": "api-date", "raw": raw, "kwargs": extra, "old": old, "new": new})


@pytest.mark.parametrize("total,rows", [(0, []), (0, [{}]), (1, []), (False, []), (-1, [])])
def test_zero_page_branch_full_caller_and_getter_order(original, monkeypatch, total, rows):
    monkeypatch.setattr(current.time, "monotonic", lambda: 10.0)
    observed = []
    for module in (original, current):
        trace = []
        snapshot = {"pageInfo": {"currentPageIndex": 0, "totalPages": total}, "content": rows}
        body = {"success": True, "error": None, "payload": snapshot}
        def fetch(_page, **kwargs):
            trace.append(["fetch", {k: v for k, v in kwargs.items() if k != "diagnostics"}])
            return TracedDict({"url": kwargs["url"], "status": 200, "contentType": "application/json", "redirected": False,
                               "bytes": 20, "body": body}, trace, "response")
        def attest(_page, **kwargs):
            trace.append(["attest", {k: v for k, v in kwargs.items() if k != "diagnostics"}])
            return {"url": kwargs["url"], "status": 200, "contentType": "application/json", "redirected": False,
                    "bytes": 20, "matched": True}
        with monkeypatch.context() as patch:
            patch.setattr(module.HsbcCrawler, "_fetch_api_page", fetch)
            patch.setattr(module.HsbcCrawler, "_attest_api_page", attest)
            budget = [500]
            result = outcome(lambda: module.HsbcCrawler._fetch_posted_history(
                NodePage(), card_id="synthetic-card", identity="synthetic", token="Bearer synthetic",
                start=date(2026, 1, 1), end=date(2026, 1, 2), byte_budget=budget))
        observed.append({"result": result.get("result"), "error": result.get("error"), "budget": budget, "trace": trace})
    assert observed[0] == observed[1]
    PROOFS.append({"kind": "zero-page", "total": total, "rows": rows, "old": observed[0], "new": observed[1]})


@pytest.mark.parametrize("method", ["_fetch_api_page", "_attest_api_page"])
@pytest.mark.parametrize("before,after,config", [
    ("credentials:'include'", "credentials:'omit'", {}),
    ("reader.read()", "reader.cancel()", {}),
    ("await r.body.cancel()", "void 0", {"reason": 1}),
    ("await reader.cancel()", "void 0", {"reason": 6}),
    ("setTimeout(()=>controller.abort(),timeoutMs)", "setTimeout(()=>controller.abort(),timeoutMs+1)", {}),
    ("clearTimeout(timer)", "void 0", {}),
])
def test_stream_native_trace_mutation_controls(original, method, before, after, config):
    baseline = NodePage(config)
    class MutantPage(NodePage):
        def evaluate(self, script, args):
            assert script.count(before) == 1
            return super().evaluate(script.replace(before, after), args)
    mutant = MutantPage(config)
    kwargs = {"url": "https://synthetic.invalid/cards", "token": "Bearer synthetic", "timeout_ms": 1234, "max_bytes": 500}
    if method == "_attest_api_page":
        kwargs["expected_snapshot"] = {}
    getattr(original.HsbcCrawler, method)(baseline, **kwargs)
    getattr(current.HsbcCrawler, method)(mutant, **kwargs)
    assert baseline.calls[0]["trace"] != mutant.calls[0]["trace"]
    PROOFS.append({"kind": "stream-mutation", "method": method, "before": before,
                   "old": baseline.calls[0], "mutant": mutant.calls[0]})


def _stmts(source):
    return ast.parse(textwrap.dedent(source)).body


def _dump(node):
    return ast.dump(node, include_attributes=False)


def _replace_block(scope, before, after):
    """One exact surrounding statement sequence, never a body fingerprint."""
    expected = [_dump(n) for n in _stmts(before)]
    found = []
    for node in ast.walk(scope):
        for field, values in ast.iter_fields(node):
            if isinstance(values, list) and all(isinstance(n, ast.stmt) for n in values):
                for index in range(len(values) - len(expected) + 1):
                    if [_dump(n) for n in values[index:index + len(expected)]] == expected:
                        found.append((values, index))
    assert len(found) == 1, (scope.name, before, len(found))
    values, index = found[0]
    values[index:index + len(expected)] = _stmts(after)


def assert_hsbc_protected_source(source):
    """Bank-only explicit structural projection, bound to immutable original.

    All untouched expressions/scope/signatures are compared to the full original
    method, then the unchanged aggregate golden. Every removed metadata reference
    must disappear; escaping/extra reads, closures and reflection fail closed.
    Raw JS uses two byte-exact reviewed literal pairs, not a JS normalizer.
    """
    from tests.test_protected_ast import GOLDEN, protected_digest, protected_nodes
    root = Path(__file__).resolve().parents[1]
    baseline = subprocess.check_output(
        ["git", "show", "16f2d4b130f44973d346b420f3d486ac839f71ac:backend/banks/hsbc.py"],
        cwd=root, text=True,
    )
    assert protected_digest(protected_nodes(baseline, "hsbc")) == GOLDEN["hsbc"]
    tree = ast.parse(source)
    old_tree = ast.parse(baseline)
    owner = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "HsbcCrawler")
    old_owner = next(n for n in old_tree.body if isinstance(n, ast.ClassDef) and n.name == "HsbcCrawler")
    methods = {n.name: n for n in owner.body if isinstance(n, ast.FunctionDef)}
    originals = {n.name: n for n in old_owner.body if isinstance(n, ast.FunctionDef)}
    start = owner.body.index(methods["collect"])
    tail = owner.body[start:]
    log = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_log")
    old_log = next(n for n in old_tree.body if isinstance(n, ast.FunctionDef) and n.name == "_log")
    assert _dump(log) == _dump(old_log)
    owned = {"_log", "print", "sys", "issubclass", "type", "str", "list", "dict", "int", "iter", "next"}
    for n in ast.walk(tree):
        assert not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id in {"locals", "globals", "vars", "eval", "exec", "setattr", "delattr"})
        assert not (isinstance(n, ast.Name) and n.id in owned and not isinstance(n.ctx, ast.Load))
        assert not (isinstance(n, ast.arg) and n.arg in owned)
        assert not (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    and n.name in owned and n is not log)
        assert not (isinstance(n, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and n.name in owned)
        assert not (isinstance(n, ast.MatchMapping) and n.rest in owned)
        assert not (isinstance(n, ast.Attribute) and not isinstance(n.ctx, ast.Load)
                    and isinstance(n.value, ast.Name) and n.value.id in owned)
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for alias in n.names:
                if (alias.asname or alias.name.split(".")[0]) in owned:
                    assert isinstance(n, ast.Import) and n in tree.body and alias.name == "sys" and alias.asname is None
    # Dynamic metadata wrappers require the same unshadowed trusted imports as
    # literal wrappers. Check the whole module, including scopes outside collect.
    for name in ("with_error_code", "error_step"):
        imports = [n for n in tree.body if isinstance(n, ast.ImportFrom)
                   and n.module == "backend.core.error_codes" and n.level == 0
                   and any(a.name == name and a.asname is None for a in n.names)]
        assert len(imports) == 1
        for n in ast.walk(tree):
            assert not (isinstance(n, ast.Name) and n.id == name and not isinstance(n.ctx, ast.Load))
            assert not (isinstance(n, ast.arg) and n.arg == name)
            assert not (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name)
            assert not (isinstance(n, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and n.name == name)
            assert not (isinstance(n, ast.MatchMapping) and n.rest == name)
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                for alias in n.names:
                    assert alias.name != "*"
                    if (alias.asname or alias.name.split(".")[0]) == name:
                        assert n in imports and alias.asname is None

    def replace(name, before, after=""):
        _replace_block(methods[name], before, after)

    # Preserve the lazy generator, its position before identity validation, and
    # the complete original AND/OR order. Field maps are finite closed literals.
    fields = ("name", "cardType", "cardStatusDisplay", "paymentDueDate", "statementDate")
    replace("_card_inventory", f"bounded_fields = ((key, card.get(key)) for key in {fields!r})",
            f"bounded_fields = (card.get(key) for key in {fields!r})")
    maps = [{field: f"HSBC-04-{140+i:03d}-{reason:03d}" for i, field in enumerate(fields)} for reason in (1, 2)]
    replace("_card_inventory", f"""
        for field_name, value in bounded_fields:
            if value is None:
                continue
            if not isinstance(value, str):
                raise with_error_code(RuntimeError('hsbc-card-inventory-identity'), {maps[0]!r}[field_name])
            if len(value) > 256:
                raise with_error_code(RuntimeError('hsbc-card-inventory-identity'), {maps[1]!r}[field_name])
    """, """
        if any(value is not None and (not isinstance(value, str) or len(value) > 256) for value in bounded_fields):
            raise RuntimeError('hsbc-card-inventory-identity')
    """)

    predicates = [
        "source is None", 're.fullmatch(r"Bearer [^\\s\\r\\n]+", token) is None',
        'source.scheme.lower() != "https"', '(source.hostname or "").lower() != "card.hsbc.com.tw"',
        "source.port not in (None, 443)", "source.username is not None", "source.password is not None",
        'not source.path.startswith("/ibk-bff/api/v1/")', 'event.get("redirected") is not False',
    ]
    lines = ["for event in candidates:", "    with error_step('HSBC-04-150-010'):",
             '        source = urlparse(event.get("url", "")) if isinstance(event, dict) else None',
             '    token = event.get("token", "") if isinstance(event, dict) else ""']
    for i, predicate in enumerate(predicates, 1):
        nested = i in (2, 5)
        if nested:
            lines.append(f"    with error_step('HSBC-04-150-{i:03d}'):")
        indent = "        " if nested else "    "
        lines += [f"{indent}if {predicate}:", f"{indent}    rejected = rejected or 'HSBC-04-150-{i:03d}'", f"{indent}    continue"]
    lines.append("    valid.append(event)")
    old_loop = next(n for n in originals["_history_token"].body if isinstance(n, ast.For))
    replace("_history_token", "\n".join(lines), ast.unparse(old_loop))
    replace("_history_token", "rejected = None")
    replace("_history_token", """
        if not valid:
            raise with_error_code(RuntimeError('hsbc-history-token'), rejected or 'HSBC-04-312-001')
    """, """
        if not valid:
            raise RuntimeError('hsbc-history-token')
    """)

    # Explicit invalid-byte packet conditional: retain the original duplicate
    # result['bytes'] reads, exceeded read, budget zeroing, and return placement.
    original_byte_guard = next(n for n in ast.walk(originals["_fetch_json"])
                              if isinstance(n, ast.If) and "result.get('bytes')" in ast.unparse(n.test))
    replace("_fetch_json", """
        if not isinstance(result, dict):
            if not diagnostics:
                diagnostics[:] = [codes[3]]
            return None
        rejected = (codes[4] if type(result.get('bytes')) is not int else
                    codes[5] if result['bytes'] < 0 else
                    codes[6] if result['bytes'] > max_bytes else None)
        if rejected:
            if not diagnostics:
                diagnostics[:] = [rejected]
            if byte_budget is not None and isinstance(result, dict) and result.get('exceeded') is True:
                byte_budget[0] = 0
            return None
    """, ast.unparse(original_byte_guard))
    replace("_fetch_json", 'diagnostic_role = "unposted" if issubclass(type(path), str) and str.__contains__(path, "/transactions/unposted") else "detail"')
    replace("_fetch_json", '_log("[fetch] HSBC API request failed [" + codes[10] + "]")',
            '_log("[fetch] HSBC API request failed")')

    # The outer zero-page condition and both original short-circuit operands
    # remain ordered; only distinct metadata allocation necessitated nesting.
    replace("_fetch_posted_history", """
        if current_total == 0:
            if page_number != 0:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-319-008')
            if bool(rows):
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-319-009')
    """, """
        if current_total == 0 and (page_number != 0 or bool(rows)):
            raise RuntimeError('hsbc-posted-history')
    """)

    # Literal maps, not arbitrary metadata-producing calls or expressions.
    tables = {
        "_fetch_json": {role: tuple(f"{prefix}-{i:03d}" for i in range(1, 12))
                        for role, prefix in (("detail", "HSBC-06-640"), ("unposted", "HSBC-06-650"))},
        "_fetch_api_page": {role: tuple(f"{prefix}-{i:03d}" for i in range(1, 5))
                            for role, prefix in (("inventory", "HSBC-04-600"), ("posted", "HSBC-06-600"),
                                                 ("detail", "HSBC-06-610"), ("unposted", "HSBC-06-620"))},
        "_attest_api_page": tuple(f"HSBC-06-630-{i:03d}" for i in range(1, 9)),
    }
    js_tables = {
        "_fetch_api_page": {role: {i: f"{prefix}-{i:03d}" for i in range(1, 13)}
                           for role, prefix in (("inventory", "HSBC-04-601"), ("posted", "HSBC-06-601"),
                                                ("detail", "HSBC-06-611"), ("unposted", "HSBC-06-621"))},
        "_attest_api_page": {i: f"HSBC-06-631-{i:03d}" for i in range(1, 13)},
    }
    for name, table in tables.items():
        suffix = "[diagnostic_role]" if name != "_attest_api_page" else ""
        replace(name, f"codes = {table!r}{suffix}")
    for name, table in js_tables.items():
        suffix = "[diagnostic_role]" if name == "_fetch_api_page" else ""
        replace(name, f"js_codes = {table!r}{suffix}")
        replace(name, """
            if isinstance(result, dict):
                marker = result.pop('_diagnostic', None)
                if type(marker) is int and marker in js_codes:
                    diagnostics[:] = [js_codes[marker]]
                    if not (result.get('exceeded') is True and type(result.get('bytes')) is int):
                        return None
        """)

    js_pairs = {
        "_fetch_api_page": ("080c0646e43ebe97a04d92b6254a8604351c157adfcf7b63a8c0452deb488461", "a169db571434bd1859c88126309771c0125f16ed1689fd11bcd46bffcfb712db"),
        "_attest_api_page": ("8913d9f1f41aafd200871d5295254e65f57c26d4946a3c0b27e7f1541640d099", "b223ed773d0c8d773495e8c094ee33c0d3dd77799a0e222a04e02afd280de0fa"),
    }
    for name, (old_hash, new_hash) in js_pairs.items():
        calls = [n for n in ast.walk(methods[name]) if isinstance(n, ast.Call)
                 and _dump(n.func) == _dump(ast.parse("page.evaluate", mode="eval").body)]
        old_calls = [n for n in ast.walk(originals[name]) if isinstance(n, ast.Call)
                     and _dump(n.func) == _dump(ast.parse("page.evaluate", mode="eval").body)]
        assert len(calls) == len(old_calls) == 1
        call, old_call = calls[0], old_calls[0]
        assert hashlib.sha256(call.args[0].value.encode()).hexdigest() == new_hash
        assert hashlib.sha256(old_call.args[0].value.encode()).hexdigest() == old_hash
        call.args[0] = deepcopy(old_call.args[0])
        assert _dump(call) == _dump(old_call)  # full native expression + arguments

    # Remove only enumerated complete metadata statements, retaining all other
    # references until the whole-method comparison. Never erase unknown calls.
    allowed_assignments = {
        _dump(n) for snippet in (
            "diagnostics: list[str] = []", "diagnostics = []",
            "diagnostics = [] if diagnostics is None else diagnostics", "diagnostics.clear()",
            "reason = codes[0]", "reason = codes[3]",
            *(f"diagnostics[:] = [codes[{i}]]" for i in range(11)),
            "diagnostics[:] = [reason]",
        ) for n in _stmts(snippet)
    }
    fallbacks = {"_fetch_card_inventory": {"HSBC-04-301-001", "HSBC-04-301-003"},
                 "_collect_card_details": {"HSBC-06-302-001", "HSBC-06-303-001"},
                 "_fetch_posted_history": {"HSBC-06-317-001", "HSBC-06-317-003", "HSBC-06-326-001"}}
    role_maps = [
        {"posted": "HSBC-06-306-001", "transaction": "HSBC-06-307-001"},
        {"posted": "HSBC-06-306-002", "transaction": "HSBC-06-307-002"},
        {"posted": "HSBC-06-308-001", "transaction": "HSBC-06-309-001"},
    ]
    role_expressions = {_dump(ast.parse(f"{table!r}[field_role]", mode="eval").body) for table in role_maps}
    kw_calls = {
        "_fetch_card_inventory": {"cls._fetch_api_page": {"diagnostics": "diagnostics", "diagnostic_role": "'inventory'"}},
        "_collect_card_details": {"self._fetch_json": {"diagnostics": "diagnostics"}},
        "_fetch_json": {"HsbcCrawler._fetch_api_page": {"diagnostics": "diagnostics", "diagnostic_role": "diagnostic_role"}},
        "_validate_posted_row": {"cls._api_date": {"field_role": "'transaction'"}},
        "_fetch_posted_history": {"cls._fetch_api_page": {"diagnostics": "diagnostics"},
                                  "cls._attest_api_page": {"diagnostics": "diagnostics"}},
    }
    class Project(ast.NodeTransformer):
        def __init__(self, name):
            self.name = name
        def visit(self, node):
            if isinstance(node, ast.stmt) and _dump(node) in allowed_assignments:
                return None
            return super().visit(node)
        def visit_With(self, node):
            if self.name in js_pairs and len(node.items) == 1 and node.items[0].optional_vars is None:
                expr = ast.unparse(node.items[0].context_expr)
                if expr in ("error_step(codes[0])", "error_step(reason)"):
                    assert len(node.body) == 1 and isinstance(node.body[0], ast.Assign)
                    return self.visit(node.body[0])
            return self.generic_visit(node)
        def visit_Call(self, node):
            if (isinstance(node.func, ast.Name) and node.func.id == "with_error_code"
                    and len(node.args) == 2 and not node.keywords):
                value = node.args[1]
                allowed = {f"next(iter(diagnostics), {code!r})" for code in fallbacks.get(self.name, ())}
                if ast.unparse(value) in allowed or (self.name == "_api_date" and _dump(value) in role_expressions):
                    return self.visit(node.args[0])
            permitted = kw_calls.get(self.name, {}).get(ast.unparse(node.func), {})
            for keyword in list(node.keywords):
                if keyword.arg in permitted:
                    assert _dump(keyword.value) == _dump(ast.parse(permitted[keyword.arg], mode="eval").body)
                    node.keywords.remove(keyword)
            return self.generic_visit(node)
    for method in tail:
        if not isinstance(method, ast.FunctionDef):
            continue
        name = method.name
        # All existing positional/default/keyword forwarding stays significant.
        for arg in list(method.args.kwonlyargs):
            if arg.arg in {"diagnostics", "diagnostic_role", "field_role"}:
                expected_args = {
                    "diagnostics": ("_fetch_json", "_fetch_api_page", "_attest_api_page"),
                    "diagnostic_role": ("_fetch_api_page",), "field_role": ("_api_date",),
                }
                assert name in expected_args[arg.arg]
                annotation = {"diagnostics": "list[str] | None", "diagnostic_role": "str", "field_role": None}[arg.arg]
                expected_arg = ast.arg(arg=arg.arg, annotation=ast.parse(annotation, mode="eval").body if annotation else None)
                assert _dump(arg) == _dump(expected_arg)
                index = method.args.kwonlyargs.index(arg)
                default = method.args.kw_defaults[index]
                expected = "None" if arg.arg == "diagnostics" else "'posted'"
                assert _dump(default) == _dump(ast.parse(expected, mode="eval").body)
                method.args.kwonlyargs.pop(index)
                method.args.kw_defaults.pop(index)
        assert _dump(method.args) == _dump(originals[name].args)
        Project(name).visit(method)
        # Only the three original adjacent None-return guards were split.
        if name in {"_fetch_json", "_fetch_api_page", "_attest_api_page"}:
            for node in ast.walk(method):
                for field, values in ast.iter_fields(node):
                    if not isinstance(values, list):
                        continue
                    i = 0
                    while i + 1 < len(values):
                        a, b = values[i:i+2]
                        def none_return(n):
                            return isinstance(n, ast.If) and not n.orelse and len(n.body) == 1 and isinstance(n.body[0], ast.Return) and isinstance(n.body[0].value, ast.Constant) and n.body[0].value.value is None
                        if none_return(a) and none_return(b):
                            combined = ast.BoolOp(op=ast.Or(), values=[a.test, b.test])
                            original_tests = {_dump(n.test) for n in ast.walk(originals[name]) if isinstance(n, ast.If)}
                            if _dump(combined) in original_tests:
                                a.test = combined
                                values.pop(i+1)
                                continue
                        i += 1
        introduced = {"diagnostics", "diagnostic_role", "codes", "js_codes", "reason", "marker", "rejected", "field_name", "field_role"}
        assert not any(isinstance(n, ast.Name) and n.id in introduced for n in ast.walk(method)), name
    projected_source = ast.unparse(tree)
    actual = protected_nodes(projected_source, "hsbc")
    expected = protected_nodes(baseline, "hsbc")
    assert len(actual) == len(expected)
    for a, b in zip(actual, expected):
        assert _dump(a) == _dump(b), a.name
    assert protected_digest(actual) == GOLDEN["hsbc"]


@pytest.mark.parametrize("method,before,after", [
    ("collect", "page.wait_for_timeout(7000)", "page.wait_for_timeout(7001)"),
    ("_fetch_card_inventory", "timeout_ms=30000", "timeout_ms=29999"),
    ("_fetch_card_inventory", "diagnostic_role='inventory'", "diagnostic_role='posted'"),
    ("_card_inventory", "len(value) > 256", "len(value) > 257"),
    ("_history_token", "source.port not in (None, 443)", "source.port not in (None, 444)"),
    ("_history_token", "valid.append(event)", "valid.insert(0, event)"),
    ("_collect_card_details", "byte_budget[0] <= 0", "byte_budget[0] < 0"),
    ("_fetch_json", "diagnostic_role=diagnostic_role", "diagnostic_role='inventory'"),
    ("_fetch_json", "result['bytes'] < 0", "result['bytes'] <= 0"),
    ("_fetch_json", "byte_budget[0] = 0", "byte_budget[0] = 1"),
    ("_fetch_json", "_log('[fetch] HSBC API request failed [' + codes[10] + ']')", "pass"),
    ("_fetch_api_page", "diagnostic_role: str='posted'", "diagnostic_role: str='inventory'"),
    ("_fetch_api_page", "marker = result.pop('_diagnostic', None)", "marker = result.get('_diagnostic')"),
    ("_fetch_api_page", "return result", "return None"),
    ("_attest_api_page", "selected == expected_snapshot", "selected != expected_snapshot"),
    ("_api_date", "field_role='posted'", "field_role='transaction'"),
    ("_validate_posted_row", "field_role='transaction'", "field_role='posted'"),
    ("_fetch_posted_history", "if bool(rows):", "if not rows:"),
    ("_fetch_posted_history", "page.wait_for_timeout(400)", "page.wait_for_timeout(401)"),
    ("_fetch_posted_history", "budget[0] -= replay['bytes']", "budget[0] += replay['bytes']"),
])
def test_protected_projection_rejects_business_scope_mutations(method, before, after):
    tree = ast.parse(Path(current.__file__).read_text())
    owner = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "HsbcCrawler")
    target = next(n for n in owner.body if isinstance(n, ast.FunctionDef) and n.name == method)
    text = ast.unparse(target)
    assert before in text
    owner.body[owner.body.index(target)] = ast.parse(text.replace(before, after, 1)).body[0]
    with pytest.raises(AssertionError):
        assert_hsbc_protected_source(ast.unparse(tree))


@pytest.mark.parametrize("method", ["_fetch_api_page", "_attest_api_page"])
@pytest.mark.parametrize("before,after", [
    ("credentials:'include'", "credentials:'omit'"),
    ("reader.read()", "reader.cancel()"),
    ("await r.body.cancel()", "void 0"),
    ("await reader.cancel()", "void 0"),
    ("setTimeout(()=>controller.abort(),timeoutMs)", "setTimeout(()=>controller.abort(),timeoutMs+1)"),
    ("clearTimeout(timer)", "void 0"),
    (" const controller", "  const controller"),
])
def test_protected_projection_rejects_changed_js_bytes(method, before, after):
    tree = ast.parse(Path(current.__file__).read_text())
    target = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == method)
    native = next(n for n in ast.walk(target) if isinstance(n, ast.Call) and ast.unparse(n.func) == "page.evaluate")
    assert native.args[0].value.count(before) == 1
    native.args[0].value = native.args[0].value.replace(before, after)
    with pytest.raises(AssertionError):
        assert_hsbc_protected_source(ast.unparse(tree))


@pytest.mark.parametrize("escape", [
    "leak = diagnostics", "leak = lambda: diagnostics", "locals()",
    "def escaped(value=diagnostics):\n    return value", "diagnostics.append(native_callback())",
])
def test_protected_projection_rejects_unproved_metadata_references(escape):
    tree = ast.parse(Path(current.__file__).read_text())
    target = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_fetch_api_page")
    target.body[2:2] = _stmts(escape)
    with pytest.raises(AssertionError):
        assert_hsbc_protected_source(ast.unparse(tree))


@pytest.mark.parametrize("binding", [
    "_log = replacement", "print = replacement", "sys.stderr = replacement",
    "with_error_code = replacement", "error_step = replacement",
    "from foreign import error_step", "def print(*args):\n    pass",
    "globals()['_log'] = replacement", "issubclass = replacement", "str = replacement", "type = replacement",
])
def test_protected_projection_rejects_changed_emission_and_helper_ownership(binding):
    source = Path(current.__file__).read_text() + "\n" + binding + "\n"
    with pytest.raises(AssertionError):
        assert_hsbc_protected_source(source)


def test_native_date_getter_truth_and_exact_role_forwarding(original):
    observed = []
    for module in (original, current):
        trace = []
        class Probe(module.HsbcCrawler):
            @staticmethod
            def _api_date(raw, **kwargs):
                trace.append(["date", raw, kwargs])
                return date(2026, 1, 2)
        class Amount(str):
            def __bool__(self):
                trace.append(["amount", "truth"])
                return False
        row = TracedDict({"postedDate": "2026-01-02T00:00", "transactionDate": "2026-01-01T00:00",
                          "ntdAmount": Amount("0 TWD"), "amount": "1 TWD", "description": "synthetic",
                          "isPositive": True, "isForeign": False}, trace, "row")
        result = Probe._validate_posted_row(row, end=date(2026, 1, 2))
        observed.append({"result": result, "trace": trace})
    expected_current = deepcopy(observed[0])
    date_calls = [entry for entry in expected_current["trace"] if entry[0] == "date"]
    assert [entry[2] for entry in date_calls] == [{}, {}]
    date_calls[1][2] = {"field_role": "transaction"}
    assert observed[1] == expected_current
    PROOFS.append({"kind": "date-forwarding-native-truth", "old": observed[0], "new": observed[1]})
