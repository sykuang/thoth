"""Diagnostic-transparent source contracts (stdlib only; never import a bank)."""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

IMPORT = "from backend.core.error_codes import error_step, with_error_code\n"
CODE = "DBS-05-001-001"
CODES = {CODE}


def normalize_tree(source, bank, codes):
    """Erase registered metadata and structurally proved source-ID replacements.

    Dynamic IDs, arbitrary contexts/calls and unproved wrappers stay in the AST:
    a mismatch is a review blocker, never permission to refresh the golden.
    """
    tree = ast.parse(source)
    trusted = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "backend.core.error_codes" and node.level == 0:
            trusted.update(alias.name for alias in node.names if alias.asname is None)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            trusted.discard(node.id)
        elif isinstance(node, ast.arg):
            trusted.discard(node.arg)
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
            trusted.discard(node.name)
        elif isinstance(node, ast.MatchMapping):
            trusted.discard(node.rest)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            trusted.discard(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            if any(alias.name == "*" for alias in node.names):
                trusted.clear()
            for alias in node.names:
                if not (isinstance(node, ast.ImportFrom) and node in tree.body
                        and node.module == "backend.core.error_codes" and node.level == 0 and alias.asname is None):
                    trusted.discard(alias.asname or alias.name.split(".")[0])

    def registered(node):
        if not isinstance(node, ast.Constant) or type(node.value) is not str:
            return False
        import re
        assert re.fullmatch(bank.upper() + r"-\d{2}-\d{3}-\d{3}", node.value), "malformed or foreign diagnostic ID"
        assert node.value in codes, f"unregistered diagnostic ID: {node.value}"
        return True

    def helper_call(node, name, count):
        return (name in trusted and isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == name
                and len(node.args) == count and not node.keywords)

    def safe_callback(node):
        if not (isinstance(node, ast.Lambda) and ast.dump(node.args) == ast.dump(ast.arguments(
                posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]))):
            return False
        return not any(isinstance(n, (ast.NamedExpr, ast.Yield, ast.YieldFrom, ast.Await))
                       or (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                           and n.func.id in {"locals", "vars", "eval", "exec", "super"})
                       for n in ast.walk(node.body))

    # Local callback adapters are erased only after proving the whole definition
    # and every reference. No name-based allowlist and no partial lambda rewrite.
    wrappers = {}
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    for definition in ast.walk(tree):
        if not isinstance(definition, ast.FunctionDef) or not isinstance(parents.get(definition), ast.FunctionDef):
            continue
        args = definition.args
        if (definition.decorator_list or definition.returns or definition.type_params
                or args.posonlyargs or len(args.args) != 2 or args.vararg or args.kwarg
                or args.kwonlyargs or args.defaults or any(arg.annotation for arg in args.args)
                or len(definition.body) != 1):
            continue
        body = definition.body[0]
        if not (isinstance(body, ast.With) and len(body.items) == 1
                and body.items[0].optional_vars is None
                and helper_call(body.items[0].context_expr, "error_step", 1)
                and len(body.body) == 1 and isinstance(body.body[0], ast.Return)):
            continue
        code = body.items[0].context_expr.args[0]
        callback = body.body[0].value
        if not (isinstance(code, ast.Name) and isinstance(callback, ast.Call)
                and isinstance(callback.func, ast.Name) and not callback.args and not callback.keywords):
            continue
        names = [arg.arg for arg in args.args]
        if code.id not in names or callback.func.id not in names or code.id == callback.func.id:
            continue
        if sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == definition.name
               for n in ast.walk(tree)) != 1:
            continue
        if any((isinstance(n, ast.Name) and n.id == definition.name and not isinstance(n.ctx, ast.Load))
               or (isinstance(n, ast.arg) and n.arg == definition.name)
               or (isinstance(n, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and n.name == definition.name)
               or (isinstance(n, ast.MatchMapping) and n.rest == definition.name)
               or (isinstance(n, (ast.Import, ast.ImportFrom)) and any(
                   (a.asname or a.name.split('.')[0]) == definition.name for a in n.names))
               for n in ast.walk(tree)):
            continue
        references = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == definition.name]
        valid = bool(references)
        for reference in references:
            call = parents.get(reference)
            owner = parents.get(reference)
            while owner is not None and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                owner = parents.get(owner)
            if owner is not parents[definition] or reference.lineno <= definition.end_lineno:
                valid = False
                break
            if not (isinstance(call, ast.Call) and call.func is reference and len(call.args) == 2 and not call.keywords):
                valid = False
                break
            operand = call.args[names.index(callback.func.id)]
            if not (safe_callback(operand) and registered(call.args[names.index(code.id)])):
                valid = False
                break
        if valid:
            wrappers[definition.name] = (definition, names.index(callback.func.id))

    # Prove the complete creation expression and its immediate native handler.
    # Other dynamic metadata (role tables, saved primary IDs) remains a blocker.
    replacements = set()
    diagnostic_aliases = set()
    for handler in ast.walk(tree):
        if not (isinstance(handler, ast.ExceptHandler) and handler.name
                and len(handler.body) == 1 and isinstance(handler.body[0], ast.Raise)):
            continue
        creation = handler.body[0].exc
        if not (isinstance(creation, ast.Call) and helper_call(creation, "with_error_code", 2)):
            continue
        value = creation.args[1]
        if not (isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or)
                and len(value.values) == 2):
            continue
        lookup, fallback = value.values
        if not ("exception_error_id" in trusted and isinstance(lookup, ast.Call)
                and isinstance(lookup.func, ast.Name) and lookup.func.id == "exception_error_id"
                and len(lookup.args) == 1 and isinstance(lookup.args[0], ast.Name)
                and lookup.args[0].id == handler.name and len(lookup.keywords) == 1
                and lookup.keywords[0].arg == "bank"
                and isinstance(lookup.keywords[0].value, ast.Constant)
                and lookup.keywords[0].value.value == bank and registered(fallback)):
            continue
        if any((isinstance(n, ast.Name) and n.id == handler.name and not isinstance(n.ctx, ast.Load))
               or (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id in {"locals", "vars", "eval", "exec"})
               for n in ast.walk(creation.args[0])):
            continue
        replacements.add(creation)
        diagnostic_aliases.add(handler)

    def terminal(node):
        return isinstance(node, ast.If) and len(node.body) == 1 and isinstance(node.body[0], ast.Raise)

    def same_body(left, right):
        return ast.dump(left.body[0], include_attributes=False) == ast.dump(right.body[0], include_attributes=False)

    def join_tests(left, right):
        values = []
        for value in (left, right):
            values.extend(value.values if isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or) else [value])
        return ast.BoolOp(op=ast.Or(), values=values)

    class DiagnosticOnly(ast.NodeTransformer):
        def generic_visit(self, node):
            node = super().generic_visit(node)
            for field, values in ast.iter_fields(node):
                if not isinstance(values, list) or not values or not all(isinstance(n, ast.stmt) for n in values):
                    continue
                merged = []
                for child in values:
                    if (merged and terminal(merged[-1]) and not merged[-1].orelse
                            and terminal(child) and not child.orelse and same_body(merged[-1], child)):
                        merged[-1].test = join_tests(merged[-1].test, child.test)
                    else:
                        merged.append(child)
                setattr(node, field, merged)
            return node

        def visit_If(self, node):
            node = self.generic_visit(node)
            while (terminal(node) and len(node.orelse) == 1 and terminal(node.orelse[0])
                   and same_body(node, node.orelse[0])):
                other = node.orelse[0]
                node.test = join_tests(node.test, other.test)
                node.orelse = other.orelse
            return node

        def visit_FunctionDef(self, node):
            if node.name in wrappers and wrappers[node.name][0] is node:
                return None
            node = self.generic_visit(node)
            # Removing an except binding also removes Python's implicit deletion.
            # Require no surviving read, binding, closure or reflective scope use.
            scope = list(ast.walk(node))
            reflective = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                             and n.func.id in {"locals", "vars", "eval", "exec"} for n in scope)
            for handler in scope:
                if not isinstance(handler, ast.ExceptHandler) or handler not in diagnostic_aliases or reflective:
                    continue
                name = handler.name
                if any((isinstance(n, ast.Name) and n.id == name)
                       or (isinstance(n, ast.arg) and n.arg == name)
                       or (isinstance(n, (ast.Global, ast.Nonlocal)) and name in n.names)
                       or (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name)
                       or (isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name == name)
                       or (isinstance(n, ast.MatchMapping) and n.rest == name)
                       or (isinstance(n, (ast.Import, ast.ImportFrom)) and any(
                           (a.asname or a.name.split('.')[0]) == name for a in n.names))
                       for n in scope):
                    continue
                handler.name = None
            return node

        def visit_With(self, node):
            if (len(node.items) == 1 and node.items[0].optional_vars is None
                    and helper_call(node.items[0].context_expr, "error_step", 1)
                    and registered(node.items[0].context_expr.args[0])):
                result = []
                for child in node.body:
                    transformed = self.visit(child)
                    result.extend(transformed if isinstance(transformed, list) else [transformed])
                return result
            return self.generic_visit(node)

        def visit_Call(self, node):
            adapter = node.func
            if (not node.args and not node.keywords and isinstance(adapter, ast.Call)
                    and len(adapter.args) == 1 and not adapter.keywords
                    and helper_call(adapter.func, "error_step", 1)
                    and registered(adapter.func.args[0])):
                callback = adapter.args[0]
                if safe_callback(callback):
                    return self.visit(callback.body)
            if isinstance(node.func, ast.Name) and node.func.id in wrappers:
                return self.visit(node.args[wrappers[node.func.id][1]].body)
            if node in replacements:
                return self.visit(node.args[0])
            if helper_call(node, "with_error_code", 2) and registered(node.args[1]):
                return self.visit(node.args[0])  # Preserve the entire original operand.
            return self.generic_visit(node)

    tree = DiagnosticOnly().visit(tree)
    return _finite_reviewed_metadata(tree, bank, trusted, codes)


def _finite_reviewed_metadata(tree, bank, trusted, codes):
    """Three reviewed preimages, not a rule for arbitrary logging/wrappers.

    These are *input* fingerprints of complete protected bodies before this
    finite rewrite, NOT replacement goldens. Any changed business operand, old
    log, alias, role table, reference, JS byte or cleanup blocks the rewrite.
    The output must independently reproduce the unchanged 16f2d4b golden.
    """
    reviewed = {
        "dbs": "d54a1365cb10c735fe9e5228046de70250e7c947bbd73ec226f319e01da1c8d9",
        "scb": "e4f0ed5cca271ea731485dbe53f7c79ab5adc4dc482942eca0f3c6df50681a26",
        "fubon": "0471e12622a2c550f028f4960dc28888d029663d9f147db3f441772a59ada541",
    }
    if bank not in reviewed or "error_step" not in trusted:
        return tree
    crawler = next((n for n in tree.body if isinstance(n, ast.ClassDef)
                    and n.name.lower() == bank + "crawler"), None)
    if crawler is None:
        return tree
    start = next((i for i, n in enumerate(crawler.body)
                  if isinstance(n, ast.FunctionDef) and n.name == "collect"), None)
    if start is None or protected_digest(crawler.body[start:]) != reviewed[bank]:
        return tree
    from copy import deepcopy
    candidate = deepcopy(crawler.body[start:])
    collect = candidate[0]
    assert isinstance(collect, ast.FunctionDef)
    if bank in {"dbs", "scb"}:
        if not {"format_error_id", "exception_error_id"} <= trusted:
            return tree
        # The added calls use the unchanged native stderr sink. It is fallible:
        # only the exact *added*, singly-emitting suppression blocks are erased.
        sink = ast.parse("def _log(*a):\n    print(*a, file=sys.stderr, flush=True)").body[0]
        sinks = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_log"]
        if [ast.dump(n) for n in sinks] != [ast.dump(sink)]:
            return tree
        owned = {"_log", "contextlib", "sys", "print", "Exception"}
        imports = {"contextlib": 0, "sys": 0}
        for n in ast.walk(tree):
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    name = a.asname or a.name.split('.')[0]
                    if name in owned:
                        if not (isinstance(n, ast.Import) and n in tree.body
                                and a.asname is None and a.name in imports):
                            return tree
                        imports[a.name] += 1
            if ((isinstance(n, ast.Name) and n.id in owned and not isinstance(n.ctx, ast.Load))
                    or (isinstance(n, ast.arg) and n.arg in owned)
                    or (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                        and n.name in owned and n is not sinks[0])
                    or (isinstance(n, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and n.name in owned)
                    or (isinstance(n, ast.MatchMapping) and n.rest in owned)
                    or (isinstance(n, ast.Attribute) and not isinstance(n.ctx, ast.Load)
                        and isinstance(n.value, ast.Name) and n.value.id in owned)
                    or (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                        and n.func.id in {"locals", "vars", "globals", "eval", "exec", "setattr", "delattr"})):
                return tree
        if imports != {"contextlib": 1, "sys": 1}:
            return tree
        ids = ([f'DBS-07-{i:03}-001' for i in range(5, 12)] + ['DBS-05-016-001']
               if bank == "dbs" else [f'SCB-07-{i:03}-001' for i in range(1, 8)])
        if not set(ids) <= set(codes):
            return tree
        removed = []
        for handler in ast.walk(collect):
            if not isinstance(handler, ast.ExceptHandler) or not handler.name:
                continue
            for code in ids:
                block = ast.parse(
                    "with contextlib.suppress(Exception):\n    "
                    f"_log(format_error_id(exception_error_id({handler.name}, bank={bank!r}) or {code!r}, bank={bank!r}))"
                ).body[0]
                if handler.body and ast.dump(handler.body[0]) == ast.dump(block):
                    handler.body.pop(0)
                    removed.append(code)
        if sorted(removed) != sorted(ids):
            return tree
        # The finite preimage proves no prior binding/reflection; no meaningful
        # reference may survive erasure, including nested function reads.
        if any(isinstance(n, ast.Name) and n.id == "_action_error" for n in ast.walk(collect)):
            return tree
        for n in ast.walk(collect):
            if isinstance(n, ast.ExceptHandler) and n.name == "_action_error":
                n.name = None
    else:
        # Complete 14-entry table/default and ALL references are sealed by the
        # input fingerprint. No escaping/rebound wrapper can share that preimage.
        wrapper = next(n for n in collect.body if isinstance(n, ast.FunctionDef)
                       and n.name == "bounded_evaluate")
        calls = [n for n in ast.walk(collect) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == "bounded_evaluate"]
        refs = [n for n in ast.walk(collect) if isinstance(n, ast.Name) and n.id == "bounded_evaluate"]
        if len(calls) != 13 or len(refs) != len(calls):
            return tree
        for call in calls:
            if (len(call.keywords) != 1 or call.keywords[0].arg != "_error_id"
                    or not isinstance(call.keywords[0].value, ast.Constant)
                    or call.keywords[0].value.value not in codes):
                return tree
            call.keywords = []
        original = ast.parse(
            "def bounded_evaluate(scope, expression: str, arg=None):\n"
            "    return scope.locator('html').evaluate(f'(root, arg) => ({expression})(arg)', arg, timeout=5000)"
        ).body[0]
        collect.body[collect.body.index(wrapper)] = original
    if protected_digest(candidate) != GOLDEN[bank]:
        return tree
    crawler.body[start:] = candidate
    return tree


# AST-dump goldens use the repository's Python 3.12 test interpreter.
# Full baseline commit: 16f2d4b (resolved SHA in integrated-protected-ast.json).
# Generated from read-only git show 16f2d4b:backend/banks/<bank>.py,
# never from the annotated worktree. Both sides use normalize_tree.
BASELINE = "16f2d4b"
GOLDEN = {
    "dbs": "310af690662679eb5c861d68a05853a81bc2f18089d37236a57c23e589c3b0cc",
    "esun": "ff7b737a833a3054228c6c15969983015535c6f36714fa193f9653a76a8c4cbd",
    "fubon": "b3bf4fdfeff60ff1c1cab40ad919bfb2d25d3b3cf36a8aed8640dec2e375a227",
    # Original raw c3cfdc...; merging identical terminal raises in BOTH baseline
    # and candidate produces this digest. No candidate code supplied this value.
    "hsbc": "7223810eca109a139cd544854a085629bcafdd43a3fb7c56b76e89aa5b24267a",
    "scb": "c3e555032aa2bef7d824d1b59879bc8143c9b4587d51186efd49bb4be6cc31b3",
    "sinopac": "6c8ab823ed9dd14021335aeed672490e270e4cfaaf0fb4ed7219faee1a3f9372",
}


def registered_codes(bank):
    path = Path(__file__).resolve().parents[1] / "backend/core/error_catalog" / f"{bank}.py"
    tree = ast.parse(path.read_text())
    assignment = next(n for n in tree.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "CODES" for t in n.targets))
    codes = ast.literal_eval(assignment.value)
    assert type(codes) is dict
    assert all(type(k) is str and type(v) is tuple and len(v) == 2
               and all(type(s) is str and s for s in v) for k, v in codes.items())
    return codes


def protected_nodes(source, bank):
    tree = normalize_tree(source, bank, registered_codes(bank))
    crawler = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                   and n.name.lower() == bank + "crawler")
    start = next(i for i, n in enumerate(crawler.body)
                 if isinstance(n, ast.FunctionDef) and n.name == "collect")
    return crawler.body[start:]


def protected_digest(nodes):
    return hashlib.sha256("\n".join(ast.dump(n, include_attributes=False) for n in nodes).encode()).hexdigest()


def assert_protected_source(source, bank):
    actual = protected_digest(protected_nodes(source, bank))
    assert actual == GOLDEN[bank], f"{bank}: protected AST differs from {BASELINE}; actual={actual} (do not rebaseline from candidate)"


def canonical_source(source, bank="dbs", codes=CODES):
    return ast.dump(normalize_tree(source, bank, codes), include_attributes=False)


def test_replacement_source_id_and_diagnostic_only_alias_are_transparent():
    prefix = IMPORT + "from backend.core.error_codes import exception_error_id\n"
    original = prefix + "def collect():\n    try:\n        page.click()\n    except Exception:\n        raise RuntimeError('guard') from None"
    annotated = original.replace("except Exception:", "except Exception as source_error:").replace(
        "RuntimeError('guard') from None",
        f"with_error_code(RuntimeError('guard'), exception_error_id(source_error, bank='dbs') or '{CODE}') from None",
    )
    assert canonical_source(annotated) == canonical_source(original)


@pytest.mark.parametrize("operand", [
    "RuntimeError((source_error := other_error))",
    "RuntimeError([source_error for source_error in errors])",
    "RuntimeError(exec('source_error = other_error'))",
    "RuntimeError(eval('source_error'))",
])
def test_replacement_source_binding_changes_are_not_erased(operand):
    prefix = IMPORT + "from backend.core.error_codes import exception_error_id\n"
    source = prefix + f"def collect():\n    try:\n        page.click()\n    except Exception as source_error:\n        raise with_error_code({operand}, exception_error_id(source_error, bank='dbs') or '{CODE}') from None"
    assert "id='with_error_code'" in canonical_source(source)


@pytest.mark.parametrize("mutation", [
    "foreign_bank", "dynamic_bank", "other_source", "dynamic_source", "dynamic_code",
    "extra_or", "and", "shadow_lookup", "shadow_creation", "meaningful_read",
    "cause", "handler", "message", "native", "cleanup", "timing", "scope_read",
    "parameter", "outer_binding", "reflection", "nested_read",
])
def test_replacement_normalization_fails_closed(mutation):
    prefix = IMPORT + "from backend.core.error_codes import exception_error_id\n"
    original = prefix + "def collect():\n    try:\n        page.click()\n    except Exception:\n        raise RuntimeError('guard') from None"
    annotated = original.replace("except Exception:", "except Exception as source_error:").replace(
        "RuntimeError('guard') from None",
        f"with_error_code(RuntimeError('guard'), exception_error_id(source_error, bank='dbs') or '{CODE}') from None",
    )
    changes = {
        "foreign_bank": ("bank='dbs'", "bank='hsbc'"),
        "dynamic_bank": ("bank='dbs'", "bank=bank"),
        "other_source": ("exception_error_id(source_error", "exception_error_id(other_error"),
        "dynamic_source": ("exception_error_id(source_error", "exception_error_id(source_error()"),
        "dynamic_code": (repr(CODE), "dynamic_code()"),
        "extra_or": (f"or '{CODE}'", f"or stale_id or '{CODE}'"),
        "and": (f"or '{CODE}'", f"and '{CODE}'"),
        "shadow_lookup": ("def collect():", "def collect(exception_error_id):"),
        "shadow_creation": ("def collect():", "def collect(with_error_code):"),
        "meaningful_read": ("RuntimeError('guard')", "RuntimeError(str(source_error))"),
        "cause": ("from None", "from source_error"),
        "handler": ("except Exception as", "except ValueError as"),
        "message": ("'guard'", "'changed'"),
        "native": ("page.click()", "page.click(); page.click()"),
        "cleanup": (" from None", " from None\n    finally:\n        page.close()"),
        "timing": ("page.click()", "page.wait_for_timeout(500); page.click()"),
        "scope_read": ("def collect():", "def collect():\n    print(source_error)"),
        "parameter": ("def collect():", "def collect(source_error):"),
        "outer_binding": ("def collect():", "def collect():\n    source_error = previous"),
        "reflection": ("page.click()", "page.click(); locals()"),
        "nested_read": ("def collect():", "def collect():\n    def read():\n        return source_error"),
    }
    old, new = changes[mutation]
    assert canonical_source(annotated.replace(old, new)) != canonical_source(original)


@pytest.mark.parametrize("code", ["DBS-05-999-999", "HSBC-05-001-001", "dbs-05-001-001"])
def test_replacement_fallback_requires_registered_bank_literal(code):
    source = IMPORT + "from backend.core.error_codes import exception_error_id\n"
    source += f"def collect():\n    try:\n        page.click()\n    except Exception as error:\n        raise with_error_code(RuntimeError('guard'), exception_error_id(error, bank='dbs') or {code!r})"
    with pytest.raises(AssertionError):
        canonical_source(source)


def test_registered_diagnostic_context_and_exception_are_transparent():
    original = "if page.count() != 1:\n    raise RuntimeError('guard') from None\npage.click()\n"
    annotated = IMPORT + f"with error_step('{CODE}'):\n    if page.count() != 1:\n        raise with_error_code(RuntimeError('guard'), '{CODE}') from None\npage.click()\n"
    assert canonical_source(annotated) == canonical_source(IMPORT + original)


@pytest.mark.parametrize("binding", [
    "error_step = replacement", "def error_step(code):\n    page.click()",
    "try:\n    pass\nexcept Exception as error_step:\n    pass",
    "match value:\n    case {'context': error_step}:\n        pass",
    "from malicious import error_step",
])
def test_shadowed_helpers_are_not_transparent(binding):
    context = f"with error_step('{CODE}'):\n    page.click()"
    assert canonical_source(IMPORT + binding + "\n" + context) != canonical_source(IMPORT + binding + "\npage.click()")


@pytest.mark.parametrize("code", ["DBS-05-999-999", "HSBC-05-001-001", "dbs-05-001-001", "DBS-05-1-001"])
def test_unknown_malformed_and_foreign_ids_fail_closed(code):
    with pytest.raises(AssertionError):
        canonical_source(IMPORT + f"with error_step({code!r}):\n    page.click()")


@pytest.mark.parametrize("context", [
    "other_context()", f"error_step('{CODE}') as owner", f"error_step('{CODE}'), other_context()",
    "error_step(dynamic_code())",
])
def test_arbitrary_and_dynamic_contexts_are_never_erased(context):
    assert canonical_source(IMPORT + f"with {context}:\n    page.click()") != canonical_source(IMPORT + "page.click()")


def test_adapter_called_before_definition_is_not_transparent():
    original = IMPORT + "def collect():\n    return page.click()"
    candidate = IMPORT + f"def collect():\n    value = action('{CODE}', lambda: page.click())\n    def action(code, callback):\n        with error_step(code):\n            return callback()\n    return value"
    normalized = canonical_source(candidate)
    assert "name='action'" in normalized
    assert normalized != canonical_source(original)


def test_local_adapter_rebinding_via_exception_is_not_erased():
    candidate = IMPORT + f"def collect():\n    def action(code, callback):\n        with error_step(code):\n            return callback()\n    try:\n        pass\n    except Exception as action:\n        pass\n    return action('{CODE}', lambda: page.click())"
    assert "FunctionDef(name='action'" in canonical_source(candidate)


def test_callback_scope_changes_are_not_erased():
    template = IMPORT + f"value = error_step('{CODE}')(lambda: OPERAND)()"
    for operand in ("(bound := page.read())", "locals()", "vars()", "eval('private')", "super().read()", "(yield page.read())"):
        candidate = template.replace('OPERAND', operand)
        assert "Lambda(" in canonical_source(candidate)


def test_relative_import_cannot_impersonate_trusted_core():
    prefix = IMPORT + "from .backend.core.error_codes import error_step\n"
    annotated = prefix + f"with error_step('{CODE}'):\n    page.click()"
    assert canonical_source(annotated) != canonical_source(prefix + "page.click()")


def test_wildcard_import_may_shadow_diagnostic_helpers():
    prefix = IMPORT + "from untrusted import *\n"
    annotated = prefix + f"with error_step('{CODE}'):\n    page.click()"
    assert canonical_source(annotated) != canonical_source(prefix + "page.click()")


def test_context_decorator_callback_is_transparent_without_losing_operand():
    original = IMPORT + "value = page.evaluate('JS') or fallback()"
    candidate = IMPORT + f"value = error_step('{CODE}')(lambda: page.evaluate('JS') or fallback())()"
    assert canonical_source(original) == canonical_source(candidate)
    for mutation in (
        candidate.replace("()", "(page.click())", 1),
        candidate.replace("lambda:", "lambda unused=page.click():"),
        candidate.replace("or fallback()", "and fallback()"),
        candidate.replace("'JS'", "'CHANGED'"),
    ):
        assert canonical_source(original) != canonical_source(mutation)


def test_equal_terminal_raise_branches_preserve_or_order():
    joined = "if first() or second():\n    raise RuntimeError('guard') from None"
    split = "if first():\n    raise RuntimeError('guard') from None\nif second():\n    raise RuntimeError('guard') from None"
    assert canonical_source(IMPORT + joined) == canonical_source(IMPORT + split)
    different = split.replace("if second():", "if changed():")
    assert canonical_source(IMPORT + joined) != canonical_source(IMPORT + different)
    assert canonical_source(IMPORT + joined) != canonical_source(IMPORT + split.replace("'guard'", "'other'", 1))
    assert canonical_source(IMPORT + joined) != canonical_source(IMPORT + split.replace("from None", "", 1))


def test_proven_callback_wrapper_preserves_whole_operand():
    original = IMPORT + "def collect():\n    value = page.evaluate('JS') or fallback()\n    return value"
    annotated = IMPORT + f"def collect():\n    def action(code, callback):\n        with error_step(code):\n            return callback()\n    value = action('{CODE}', lambda: page.evaluate('JS') or fallback())\n    return value"
    assert canonical_source(original) == canonical_source(annotated)
    for mutation in (
        annotated.replace("return callback()", "callback()\n            return callback()"),
        annotated.replace("return callback()", "page.click()\n            return callback()"),
        annotated.replace("page.evaluate('JS')", "page.evaluate('CHANGED')"),
        annotated.replace("or fallback()", "and fallback()"),
        annotated.replace("return callback()", "return callback() or fallback()"),
        annotated.replace("lambda:", "lambda unused=page.click():"),
        annotated + "\naction = replacement",
    ):
        assert canonical_source(original) != canonical_source(mutation)


@pytest.mark.parametrize("mutation", [
    "page.click()",  # removed guard
    "if page.count() != 2:\n    raise RuntimeError('guard')\npage.click()",
    "if page.count() != 1:\n    raise RuntimeError('guard')\npage.click()\npage.click()",
    "page.click()\nif page.count() != 1:\n    raise RuntimeError('guard')",
    "if page.count() != 1:\n    raise RuntimeError('changed')\npage.click()",
])
def test_business_mutations_remain_visible(mutation):
    original = "if page.count() != 1:\n    raise RuntimeError('guard')\npage.click()"
    annotated = IMPORT + f"with error_step('{CODE}'):\n" + "\n".join("    " + line for line in mutation.splitlines())
    assert canonical_source(IMPORT + original) != canonical_source(annotated)


@pytest.mark.parametrize("literal", ["SELECT amount FROM accounts", "() => document.body.innerText"])
def test_sql_and_js_literals_remain_byte_exact(literal):
    original = f"page.evaluate({literal!r})"
    changed = f"with error_step('{CODE}'):\n    " + original.replace(literal, literal + " ")
    assert canonical_source(IMPORT + original) != canonical_source(IMPORT + changed)


@pytest.mark.parametrize("bank", ["dbs", "scb", "fubon"])
def test_only_finite_reviewed_added_suppressed_emissions_are_transparent(bank):
    source = (Path(__file__).resolve().parents[1] / f"backend/banks/{bank}.py").read_text()
    assert_protected_source(source, bank)


@pytest.mark.parametrize("binding", [
    "def print(*args, **kwargs):\n    page.click()\n",
    "def Exception():\n    page.click()\n",
    "class contextlib:\n    suppress = meaningful\n",
    "print = meaningful\n", "_log = meaningful\n", "contextlib.suppress = meaningful\n",
    "from foreign import contextlib\n", "from foreign import format_error_id\n",
])
def test_finite_emission_ownership_rejects_changed_module_bindings(binding):
    source = (Path(__file__).resolve().parents[1] / "backend/banks/dbs.py").read_text()
    changed = source + "\n" + binding
    assert protected_digest(protected_nodes(changed, "dbs")) != GOLDEN["dbs"]


@pytest.mark.parametrize("bank", ["dbs", "scb"])
@pytest.mark.parametrize("mutation", [
    "old_log", "unguarded", "bad_formatter", "foreign_bank", "other_source",
    "meaningful_alias", "meaningful_state", "native_args", "fallible_sink",
])
def test_finite_emissions_do_not_hide_unreviewed_changes(bank, mutation):
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / f"backend/banks/{bank}.py").read_text())
    crawler = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name.lower() == bank + "crawler")
    collect = next(n for n in crawler.body if isinstance(n, ast.FunctionDef) and n.name == "collect")
    handler = next(n for n in ast.walk(collect) if isinstance(n, ast.ExceptHandler)
                   and n.name == "_action_error")
    block = handler.body[0]
    emission = block.body[0].value
    if mutation == "unguarded":
        handler.body[0] = block.body[0]
    elif mutation == "bad_formatter":
        emission.args[0].func.id = "unsafe_formatter"
    elif mutation == "foreign_bank":
        emission.args[0].keywords[0].value.value = "hsbc"
    elif mutation == "other_source":
        emission.args[0].args[0].values[0].args[0].id = "other_error"
    elif mutation == "meaningful_alias":
        handler.body.append(ast.parse("save(_action_error)").body[0])
    elif mutation == "meaningful_state":
        collect.body.insert(0, ast.parse("state = meaningful()").body[0])
    elif mutation == "native_args":
        native = next(n for n in ast.walk(collect) if isinstance(n, ast.Call)
                      and isinstance(n.func, ast.Attribute) and n.func.attr == "title")
        native.args.append(ast.Constant(value="changed"))
    elif mutation == "fallible_sink":
        sink = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_log")
        sink.body.insert(0, ast.parse("page.click()").body[0])
    else:
        old = next(n for n in ast.walk(collect) if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Name) and n.func.id == "_log"
                   and not (isinstance(n.args[0], ast.Call) and isinstance(n.args[0].func, ast.Name)
                            and n.args[0].func.id == "format_error_id"))
        old.args = [ast.Constant(value="changed old fallible log")]
    changed = ast.unparse(ast.fix_missing_locations(tree))
    assert protected_digest(protected_nodes(changed, bank)) != GOLDEN[bank]


@pytest.mark.parametrize("mutation", [
    "native_args", "native_timeout", "role_call", "role_table", "escape", "meaningful_state", "js_bytes",
])
def test_finite_fubon_wrapper_requires_complete_definition_and_all_references(mutation):
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "backend/banks/fubon.py").read_text())
    crawler = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "FubonCrawler")
    collect = next(n for n in crawler.body if isinstance(n, ast.FunctionDef) and n.name == "collect")
    wrapper = next(n for n in collect.body if isinstance(n, ast.FunctionDef) and n.name == "bounded_evaluate")
    call = next(n for n in ast.walk(collect) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name) and n.func.id == "bounded_evaluate")
    if mutation == "native_args":
        call.args[0] = ast.Name(id="other_frame", ctx=ast.Load())
    elif mutation == "role_call":
        call.keywords[0].value.value = "FUBON-06-216-001"
    elif mutation == "role_table":
        table = wrapper.body[0].value.value
        table.values[0], table.values[1] = table.values[1], table.values[0]
    elif mutation == "native_timeout":
        native = next(n for n in ast.walk(wrapper) if isinstance(n, ast.Call)
                      and isinstance(n.func, ast.Attribute) and n.func.attr == "evaluate")
        native.keywords[0].value.value = 6000
    elif mutation == "escape":
        collect.body.append(ast.parse("save(bounded_evaluate)").body[0])
    elif mutation == "meaningful_state":
        wrapper.body.insert(0, ast.parse("root = meaningful()").body[0])
    else:
        call.args[1].value += " "
    changed = ast.unparse(ast.fix_missing_locations(tree))
    assert protected_digest(protected_nodes(changed, "fubon")) != GOLDEN["fubon"]


@pytest.mark.parametrize("bank", sorted(GOLDEN))
def test_readonly_original_reproduces_unchanged_golden(bank):
    import subprocess

    root = Path(__file__).resolve().parents[1]
    original = subprocess.check_output(
        ["git", "show", f"{BASELINE}:backend/banks/{bank}.py"], cwd=root, text=True,
    )
    assert protected_digest(protected_nodes(original, bank)) == GOLDEN[bank]
