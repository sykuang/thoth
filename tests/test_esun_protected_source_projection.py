"""E.SUN-only closed-site source projection; never rebaseline the candidate."""
import ast
from pathlib import Path
import subprocess
import json
from copy import deepcopy

import pytest

from tests.test_protected_ast import normalize_tree, registered_codes, protected_digest, GOLDEN

ROOT = Path(__file__).resolve().parents[1]


JS_ORIGINAL = {
    'selected': '() => {\n                                const s = document.querySelector(\'select[id="fao01002:dract"]\');\n                                const o = s?.options[s.selectedIndex];\n                                return s && o ? {\n                                    index: s.selectedIndex,\n                                    value: s.value || \'\',\n                                    text: (o.textContent || \'\').trim(),\n                                } : null;\n                            }',
    'clicked_period': '(period) => {\n                                const r = document.querySelector(\'input[id="fao01002:j_id_intervalrdo4"], input[name="fao01002:intervalrdo"][value="4"]\');\n                                if (!r) return {ok: false, error: \'no intervalrdo4\'};\n                                const label = r.closest(\'label\');\n                                if (label) {\n                                    try { label.click(); } catch (e) {}\n                                }\n                                r.checked = true;\n                                r.dispatchEvent(new Event(\'change\', {bubbles: true}));\n                                r.dispatchEvent(new Event(\'click\', {bubbles: true}));\n                                for (const other of document.querySelectorAll(\'input[name="fao01002:intervalrdo"]\')) {\n                                    if (other !== r) other.checked = false;\n                                }\n                                for (const lbl of document.querySelectorAll(\'.radiobutton-group label\')) {\n                                    lbl.classList.toggle(\'checked\', lbl.contains(r));\n                                }\n                                const s = document.querySelector(\'input[id="fao01002:startDate"]\');\n                                const e = document.querySelector(\'input[id="fao01002:endDate"]\');\n                                if (s) {\n                                    s.value = period.start;\n                                    s.dispatchEvent(new Event(\'change\', {bubbles: true}));\n                                }\n                                if (e) {\n                                    e.value = period.end;\n                                    e.dispatchEvent(new Event(\'change\', {bubbles: true}));\n                                }\n                                return {ok: true, checked: r.checked, start: s?.value || \'\', end: e?.value || \'\'};\n                            }',
    'sort_selected': '() => {\n                                const r = document.querySelector(\'input[id="fao01002:j_id_sort1"], input[name="fao01002:txDateOrder"][value="1"]\');\n                                if (!r) return false;\n                                r.checked = true;\n                                r.dispatchEvent(new Event(\'change\', {bubbles: true}));\n                                return r.checked;\n                            }',
    'submit_info': '() => {\n                                    const visible = (el) => {\n                                        const r = el.getBoundingClientRect();\n                                        const st = window.getComputedStyle(el);\n                                        return r.width > 0 && r.height > 0 && st.display !== \'none\' && st.visibility !== \'hidden\';\n                                    };\n                                    for (const el of document.querySelectorAll(\'button,a,input[type="button"],input[type="submit"],input[type="image"]\')) {\n                                        const t = (el.textContent || el.value || el.title || \'\').replace(/\\s+/g, \'\').trim();\n                                        if (t === \'查詢\' && visible(el)) {\n                                            el.click();\n                                            return {clicked: \'visible-query\', tag: el.tagName, id: el.id || \'\', name: el.name || \'\', text: t};\n                                        }\n                                    }\n                                    return {clicked: null};\n                                }',
}

# The only JS substitutions are these four complete byte-exact literal pairs.
# No token/whitespace normalizer, candidate fingerprint, or wildcard call erasure.
JS_ANNOTATED = dict(JS_ORIGINAL)
JS_ANNOTATED['selected'] = JS_ORIGINAL['selected'].replace(
    'return s && o ? {', 'if (!s) return [null, 1];\n                                if (!o) return [null, 2];\n                                return [{'
).replace('} : null;', '}, 0];')
JS_ANNOTATED['clicked_period'] = JS_ORIGINAL['clicked_period'].replace(
    "if (!r) return {ok: false, error: 'no intervalrdo4'};",
    "if (!r) return [{ok: false, error: 'no intervalrdo4'}, 1];"
).replace(
    "return {ok: true, checked: r.checked, start: s?.value || '', end: e?.value || ''};",
    "const result = {ok: true, checked: r.checked, start: s?.value || '', end: e?.value || ''};\n                                return [result, !result.checked ? 2 : result.start !== period.start ? 3 : result.end !== period.end ? 4 : 0];"
)
JS_ANNOTATED['sort_selected'] = JS_ORIGINAL['sort_selected'].replace(
    'if (!r) return false;', 'if (!r) return [false, 1];'
).replace('return r.checked;', 'const checked = r.checked;\n                                return [checked, checked ? 0 : 2];')
JS_ANNOTATED['submit_info'] = JS_ORIGINAL['submit_info'].replace(
    'return {clicked: null};', 'return [{clicked: null}, 1];'
)

POLL_HELPER = '''def poll_observation(result, codes):
    if type(result) is list and len(result) == 2:
        value, reason = result
        if type(reason) is int and 1 <= reason <= len(codes):
            return value, codes[reason - 1]
        return value, None
    return result, None
'''

# Python's original ordered guards, including complete original exceptions.
CARRIERS = (
    ('selected', 'selected_failure', ('ESUN-05-867-001', 'ESUN-05-867-002'), 'ESUN-05-420-001',
     "selected != {'index': opt['index'], 'value': opt['value'], 'text': opt['text']}", 'esun-twd-history-selected-account'),
    ('clicked_period', 'period_failure', ('ESUN-05-867-003', 'ESUN-05-867-004', 'ESUN-05-867-005', 'ESUN-05-867-006'), 'ESUN-05-421-001',
     "clicked_period != {'ok': True, 'checked': True, **expected_period}", 'esun-twd-history-period'),
    ('sort_selected', 'sort_failure', ('ESUN-05-867-007', 'ESUN-05-867-008'), 'ESUN-05-422-001',
     'sort_selected is not True', 'esun-twd-history-sort'),
    ('submit_info', 'submit_failure', ('ESUN-05-867-009',), 'ESUN-05-424-001',
     "submit_info != {'clicked': 'visible-query', 'tag': submit_info.get('tag'), 'id': submit_info.get('id'), 'name': submit_info.get('name'), 'text': '查詢'}", 'esun-twd-history-submit'),
)


def _dump(node):
    return ast.dump(node, include_attributes=False)


def _owned_names(tree, names, imports):
    """Reject rebinding, escaping imports and reflective mutation at full scope."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            assert all(a.name != '*' for a in node.names), 'wildcard ownership'
            if any((a.asname or a.name.split('.')[0]) in names for a in node.names):
                assert _dump(node) in imports, 'import ownership'
                found.append(_dump(node))
        if isinstance(node, ast.Name) and node.id in names:
            assert isinstance(node.ctx, ast.Load), 'name ownership'
        if isinstance(node, ast.arg):
            assert node.arg not in names, 'parameter ownership'
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            assert node.name not in names, 'definition ownership'
        if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
            assert node.name not in names, 'alias ownership'
        if isinstance(node, ast.MatchMapping):
            assert node.rest not in names, 'match ownership'
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            assert not names.intersection(node.names), 'scope ownership'
        if isinstance(node, ast.Attribute) and not isinstance(node.ctx, ast.Load):
            assert not isinstance(node.value, ast.Name) or node.value.id not in names, 'attribute ownership'
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {'locals', 'vars', 'globals', 'eval', 'exec', 'setattr', 'delattr'}, 'reflective ownership'
    assert sorted(found) == sorted(imports), 'import cardinality'


def _passive_poll_import_surface():
    """Close the newly imported local module graph, not arbitrary import erasure.

    Under the same trusted stdlib/core imports as esun itself, initialization
    creates only literal constants, functions and the native collector class.
    No bank callback, DOM/native operation, decorator factory or I/O is run.
    """
    imports = {
        'collection.py': '''from backend.core.error_codes import exception_error_id, error_step, with_error_code
import re
from time import monotonic
from backend.core.base import _OriginGuardProxy
from .capture import PATHS, require_current_success
from .navigation import build_menu_plan
from .navigation import MENU, navigate_twd''',
        'capture.py': '''from backend.core.error_codes import exception_error_id, error_step, with_error_code
import re
from urllib.parse import urlsplit
from backend.core.base import ApiHit, ResponseCollector, _HistoryBodyObserver, _strict_json_loads''',
        'navigation.py': '''from backend.core.error_codes import error_step, with_error_code
from copy import deepcopy''',
        '__init__.py': '',
    }
    for filename, declarations in imports.items():
        tree = ast.parse((ROOT / 'backend/banks/esun_spa' / filename).read_text())
        actual = [_dump(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        assert actual == [_dump(n) for n in ast.parse(declarations).body], 'poll import graph'
        trusted = {a.asname or a.name.split('.')[0] for n in tree.body
                   if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        _owned_names(tree, trusted | {'int', 'str', 'bool', 'dict', 'list', 'tuple', 'staticmethod'}, actual)
        bound = set()
        definitions = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        def literal(node):
            if isinstance(node, ast.Constant):
                return type(node.value) in (str, int, float, bool, type(None))
            if isinstance(node, ast.Name):
                return node.id in bound
            if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                return all(literal(n) for n in node.elts)
            if isinstance(node, ast.Dict):
                return all(k is not None and literal(k) and literal(v) for k, v in zip(node.keys, node.values))
            if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mult)):
                return literal(node.left) and literal(node.right)
            if isinstance(node, ast.Subscript):
                return isinstance(node.value, ast.Name) and node.value.id == 'PATHS' and isinstance(node.slice, ast.Constant) and node.slice.value in (2, 5)
            return False
        def function(node):
            assert all(_dump(d) == _dump(ast.Name(id='staticmethod', ctx=ast.Load())) for d in node.decorator_list), 'active decorator'
            headers = node.args.defaults + [n for n in node.args.kw_defaults if n is not None]
            for default in headers:
                assert literal(default) or _dump(default) == _dump(ast.parse('lambda: False', mode='eval').body), 'active default'
            annotations = [a.annotation for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs) if a.annotation is not None]
            if node.returns is not None:
                annotations.append(node.returns)
            for annotation in annotations:
                assert not any(isinstance(n, (ast.Call, ast.Attribute, ast.NamedExpr)) for n in ast.walk(annotation)), 'active annotation'
                assert all(n.id in {'int', 'str', 'bool', 'dict', 'list', 'tuple', 'None'} for n in ast.walk(annotation) if isinstance(n, ast.Name)), 'annotation ownership'
        def statements(body):
            for node in body:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue  # Exact module imports were checked above.
                if isinstance(node, ast.Expr):
                    assert isinstance(node.value, ast.Constant) and type(node.value.value) is str, 'active initialization'
                elif isinstance(node, ast.Assign):
                    assert all(isinstance(t, ast.Name) and t.id not in definitions for t in node.targets), 'import binding'
                    assert literal(node.value), 'active constant'
                    bound.update(t.id for t in node.targets)
                elif isinstance(node, ast.FunctionDef):
                    function(node)
                elif isinstance(node, ast.ClassDef):
                    assert filename == 'capture.py' and node.name == 'SpaCollector' and not node.decorator_list and not node.keywords, 'import class'
                    assert [_dump(b) for b in node.bases] == [_dump(ast.Name(id='ResponseCollector', ctx=ast.Load()))], 'import class base'
                    statements(node.body)
                else:
                    raise AssertionError('active import statement')
        statements(tree.body)


def project_esun_tree(source):
    """Project only four closed carrier/guard sequences and the result sidecar.

    Everything not explicitly matched is retained and compared in full to 16f2.
    Native snapshot evaluation is retained, never regenerated from candidate data.
    """
    raw = ast.parse(source)
    tree = normalize_tree(source, 'esun', registered_codes('esun'))
    collect = tail(tree)[0]
    import_node = ast.parse('from backend.banks.esun_spa.collection import poll_observation').body[0]
    imports = [n for n in ast.walk(collect) if _dump(n) == _dump(import_node)]
    if not imports:
        return tree  # Baseline or unrecognized source: no speculative rewrite.
    assert len(imports) == 1 and collect.body[3] is imports[0], 'poll import scope/order'
    # The former catch created a second RuntimeError and suppressed its context.
    # The leaf code now reaches the original SINGLE native creation site instead.
    # Pin this entire helper/signature plus its only production caller; never
    # erase a keyword against an unproved callee or subclass-facing signature.
    result_helpers = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_fresh_twd_result']
    expected_helper = ast.parse('''@staticmethod
 def _fresh_twd_result(candidates, *, failure_code=None) -> dict:
    if len(candidates) != 1:
        raise with_error_code(RuntimeError('esun-twd-history-result'), failure_code or 'ESUN-05-403-001')
    result = candidates[0]
    if not isinstance(result, dict) or result.get('evidenceFresh') is not True:
        raise RuntimeError('esun-twd-history-stale-result')
    return result
'''.replace('\n def ', '\ndef ')).body[0]
    assert len(result_helpers) == 1 and _dump(result_helpers[0]) == _dump(expected_helper), 'native result helper contract'
    assert sum(isinstance(n, ast.Attribute) and n.attr == '_fresh_twd_result' for n in ast.walk(raw)) == 1, 'result helper references'
    diagnostic_import = ast.parse('from backend.core.error_codes import error_step, exception_error_id, with_error_code').body[0]
    _owned_names(raw, {'poll_observation', 'error_step', 'exception_error_id', 'with_error_code',
                       'RuntimeError', 'Exception', 'type', 'list', 'len', 'int'},
                 [_dump(import_node), _dump(diagnostic_import)])
    helper_tree = ast.parse((ROOT / 'backend/banks/esun_spa/collection.py').read_text())
    helper = next(n for n in helper_tree.body if isinstance(n, ast.FunctionDef) and n.name == 'poll_observation')
    assert _dump(helper) == _dump(ast.parse(POLL_HELPER).body[0]), 'poll helper changed'
    # Its primitive operations cannot dispatch bank callbacks: exact builtin
    # list/int and closed tuple literals. Pin their module ownership as well.
    _owned_names(helper_tree, {'type', 'list', 'len', 'int', 'str', 'dict', 'bool', 'tuple', 'staticmethod'}, [])
    _passive_poll_import_surface()

    removed = {imports[0]}
    erased_names = {'poll_observation', 'selected_failure', 'period_failure', 'sort_failure', 'submit_failure', 'result_failure', 'result_error'}
    replacements = []
    for name, failure, codes, fallback, predicate, message in CARRIERS:
        for code in (*codes, fallback):
            assert code in registered_codes('esun')
        arg = ', expected_period' if name == 'clicked_period' else ''
        old = ast.parse(f'{name} = query_frame.evaluate({JS_ORIGINAL[name]!r}{arg})\nif {predicate}:\n    raise RuntimeError({message!r})').body
        new = ast.parse(
            f'{name} = query_frame.evaluate({JS_ANNOTATED[name]!r}{arg})\n'
            f'{name}, {failure} = poll_observation({name}, {codes!r})\n'
            f'if {predicate}:\n    raise with_error_code(RuntimeError({message!r}), {failure} or {fallback!r})'
        ).body
        matches = []
        for parent in ast.walk(collect):
            for field, values in ast.iter_fields(parent):
                if isinstance(values, list):
                    for i in range(len(values) - 2):
                        if all(isinstance(n, ast.AST) for n in values[i:i+3]) and [_dump(n) for n in values[i:i+3]] == [_dump(n) for n in new]:
                            matches.append((values, i))
        assert len(matches) == 1, 'closed carrier sequence: ' + name
        values, i = matches[0]
        removed.update(values[i:i+3])
        replacements.append((values, i, 3, old))

    # Native singleton result loop. Only the diagnostic writes/catch/rethrow
    # vanish. The complete evaluate operand, including JS/args, stays untouched.
    matches = []
    for parent in ast.walk(collect):
        for field, values in ast.iter_fields(parent):
            if not isinstance(values, list):
                continue
            for i in range(len(values) - 3):
                if not all(isinstance(n, ast.AST) for n in values[i:i+4]):
                    continue
                if _dump(values[i]) != _dump(ast.parse('result_candidates = []').body[0]):
                    continue
                loop = values[i+2]
                assert isinstance(loop, ast.For) and len(loop.body) == 1 and isinstance(loop.body[0], ast.Try), 'result loop'
                native = loop.body[0].body[0]
                assert isinstance(native, ast.Assign), 'native snapshot'
                template = ast.parse('''result_candidates = []
result_failure = None
for rf in (query_frame,):
    try:
        SNAPSHOT
        if snap.get('bound') is True:
            result_candidates.append(snap)
            result_failure = None
        else:
            result_failure = 'ESUN-05-867-010'
    except Exception as source_error:
        result_failure = exception_error_id(source_error, bank='esun')
bound_result = self._fresh_twd_result(result_candidates, failure_code=result_failure)
''').body
                template[2].body[0].body[0] = deepcopy(native)
                assert [_dump(n) for n in values[i:i+4]] == [_dump(n) for n in template], 'closed result sequence'
                old = ast.parse('''result_candidates = []
for rf in (query_frame,):
    try:
        SNAPSHOT
        if snap.get('bound') is True:
            result_candidates.append(snap)
    except Exception:
        pass
bound_result = self._fresh_twd_result(result_candidates)
''').body
                old[1].body[0].body[0] = deepcopy(native)
                matches.append((values, i, old))
    assert len(matches) == 1, 'result sequence cardinality'
    values, i, old = matches[0]
    removed.update(values[i:i+4])
    replacements.append((values, i, 4, old))

    # Nothing erased may be consumed anywhere else: bindings, closures,
    # exception aliases, walrus/comprehensions, annotation/defaults/reflection.
    inside = {n for block in removed for n in ast.walk(block)}
    for node in ast.walk(collect):
        if isinstance(node, ast.Name) and node.id in erased_names:
            assert node in inside, 'surviving metadata reference'
        if isinstance(node, ast.arg):
            assert node.arg not in erased_names, 'metadata parameter'
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            assert node.name not in erased_names, 'metadata definition'
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            assert not erased_names.intersection(node.names), 'metadata scope'
        if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name in erased_names:
            assert node in inside, 'metadata alias'
    # Execute splices in reverse index order; statement order remains significant.
    for values, i, count, new in sorted(replacements, key=lambda x: x[1], reverse=True):
        values[i:i+count] = new
    collect.body.remove(imports[0])
    # The first shared pass proved each complete source-ID replacement but
    # retained these three aliases because the sidecar used source_error. Once
    # removed, require zero surviving reads/bindings/closures in the full scope.
    assert not any(isinstance(n, ast.Name) and n.id == 'source_error' for n in ast.walk(collect)), 'source alias consumed'
    aliases = [n for n in ast.walk(collect) if isinstance(n, ast.ExceptHandler) and n.name == 'source_error']
    expected_bodies = {message: _dump(ast.parse(f"raise RuntimeError({message!r}) from None").body[0]) for message in (
        'esun-twd-history-inventory', 'esun-twd-history-account', 'esun-twd-history')}
    assert len(aliases) == 3, 'source alias cardinality'
    for handler in aliases:
        assert _dump(handler.type) == _dump(ast.Name(id='Exception', ctx=ast.Load())) and len(handler.body) == 1, 'source handler'
        assert _dump(handler.body[0]) in expected_bodies.values(), 'source replacement'
        handler.name = None
    assert not any(isinstance(n, ast.Name) and n.id in erased_names for n in ast.walk(collect)), 'metadata escaped projection'
    # All containing methods, financial/range/transport validation, listener
    # cleanup/finally, native calls/throws/order/JS bytes remain in these nodes.
    return tree


def tail(tree):
    crawler = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'EsunCrawler')
    start = next(i for i, n in enumerate(crawler.body) if isinstance(n, ast.FunctionDef) and n.name == 'collect')
    return crawler.body[start:]


def test_original_and_current_complete_protected_tail_keep_original_goldens():
    original = subprocess.check_output(['git', 'show', '16f2d4b:backend/banks/esun.py'], cwd=ROOT, text=True)
    current = (ROOT / 'backend/banks/esun.py').read_text()
    for source in (original, current):
        nodes = tail(project_esun_tree(source))
        assert protected_digest(nodes) == GOLDEN['esun']
        assert protected_digest(nodes[0].body[3:] + nodes[1:]) == '3fe2511bcd3716dffebe8df059027a44c2efbeafc95d625e3600d1dc62088dd9'


def test_zero_binding_real_js_collect_preserves_native_exception_chain(monkeypatch):
    import pytest
    from backend.banks import esun
    from tests.test_esun_terminal_joined_proofs import frozen_bank, legacy_complete_surface

    chains = []
    for module in (frozen_bank(), esun):
        with monkeypatch.context() as patcher:
            crawler, page, collector, trace = legacy_complete_surface(patcher, module, missing=True)
            with pytest.raises(RuntimeError) as caught:
                crawler.collect(page, collector)
            chain = []
            error = caught.value
            while error is not None:
                chain.append((type(error).__name__, str(error), error.__suppress_context__, error.__cause__))
                error = error.__context__
            assert any(type(x) is tuple and x[0] == 'binding' for x in trace)
            chains.append(chain)
    assert chains[0] == chains[1]


def _accepts(source):
    try:
        return protected_digest(tail(project_esun_tree(source))) == GOLDEN['esun']
    except AssertionError:
        return False


@pytest.mark.parametrize('before,after', [
    ('timeout=8000', 'timeout=7999'),
    ('range(90)', 'range(89)'),
    ('result_candidates, failure_code=result_failure', '[], failure_code=result_failure'),
    ('bound_result = self._fresh_twd_result(', 'bound_result = other_context('),
    (' from None', ' from source_error'),
    ('except Exception as source_error:', 'except ValueError as source_error:'),
    ('failure_code=result_failure', 'failure_code=None'),
    ("result_failure = 'ESUN-05-867-010'", "result_failure = 'ESUN-05-867-009'"),
    ("selected_failure or 'ESUN-05-420-001'", "period_failure or 'ESUN-05-420-001'"),
    ("('ESUN-05-867-001', 'ESUN-05-867-002')", "('ESUN-05-867-002', 'ESUN-05-867-001')"),
    ('const o = s?.options[s.selectedIndex];', 'const o = s?.options[0];'),
    ('period.start ? 3 : result.end !== period.end ? 4', 'period.start ? 4 : result.end !== period.end ? 3'),
    ("el.click();\n                                            return {clicked: 'visible-query'", "fetch('/changed'); el.click();\n                                            return {clicked: 'visible-query'"),
    ("const checked = r.checked;", "const checked = r.checked; setTimeout(() => {}, 1);"),
    ('catch (e) {}', 'catch (e) { throw e; }'),
    ("scopes.length !== 1", "scopes.length !== 2"),
    ('query_frame,):', 'query_frame, query_frame):'),
    ('page.remove_listener("response", response_listener)', 'page.on("response", response_listener)'),
    ('with error_step(\'ESUN-05-624-001\'):', 'with other_context():'),
], ids=['timeout','range','native-operand','replacement-call','exception-context','exception-class','state-consumer',
        'state-marker','wrong-state-consumer','marker-map','JS-getter','JS-marker','JS-request',
        'JS-timer','JS-catch','snapshot-scope','loop-cardinality','cleanup','context'])
def test_business_mutants_fail_closed(before, after):
    source = (ROOT / 'backend/banks/esun.py').read_text()
    prefix, collect = source.split('    def collect(', 1)
    assert before in collect
    changed = prefix + '    def collect(' + collect.replace(before, after, 1)
    assert not _accepts(changed)


@pytest.mark.parametrize('statement', [
    'out["changed"] = result_failure',
    'out["changed"] = selected_failure',
    'def consume():\n    return source_error',
    'def consume(value=result_failure):\n    return value',
    'out["changed"] = [result_failure for result_failure in ()]',
    'out["changed"] = (result_error := None)',
    'locals()',
    'exec("result_failure = None")',
    'match out:\n    case {"changed": result_failure}:\n        pass',
    'with other_context():\n    pass',
])
def test_all_reference_scope_and_context_mutants_fail_closed(statement):
    tree = ast.parse((ROOT / 'backend/banks/esun.py').read_text())
    collect = tail(tree)[0]
    collect.body[-1:-1] = ast.parse(statement).body
    assert not _accepts(ast.unparse(ast.fix_missing_locations(tree)))


@pytest.mark.parametrize('extra', ['page.click()', 'poll_observation = replacement',
                                  'from foreign import type', 'class Added:\n    page.click()'])
def test_poll_import_cannot_hide_active_module_initialization(monkeypatch, extra):
    source = (ROOT / 'backend/banks/esun.py').read_text()
    path = ROOT / 'backend/banks/esun_spa/collection.py'
    original = Path.read_text
    injected = original(path) + '\n' + extra + '\n'
    monkeypatch.setattr(Path, 'read_text', lambda self, *a, **k: injected if self == path else original(self, *a, **k))
    assert not _accepts(source)


def test_import_moved_across_native_initial_wait_is_not_erased():
    tree = ast.parse((ROOT / 'backend/banks/esun.py').read_text())
    collect = tail(tree)[0]
    collect.body.insert(5, collect.body.pop(3))
    assert not _accepts(ast.unparse(ast.fix_missing_locations(tree)))


def test_statement_order_and_module_binding_mutants_fail_closed():
    source = (ROOT / 'backend/banks/esun.py').read_text()
    tree = ast.parse(source)
    collect = tail(tree)[0]
    # Reverse the output initialization and native initial wait.
    collect.body[4], collect.body[5] = collect.body[5], collect.body[4]
    assert not _accepts(ast.unparse(ast.fix_missing_locations(tree)))
    for suffix in ('\npoll_observation = replacement\n', '\nRuntimeError = ValueError\n',
                   '\nfrom foreign import exception_error_id\n'):
        assert not _accepts(source + suffix)


@pytest.mark.parametrize('name,family', [('selected','selected'), ('clicked_period','period'),
                                        ('sort_selected','sort'), ('submit_info','submit')])
def test_exact_carrier_pairs_keep_every_native_throw_prefix(name, family):
    from tests.test_esun_terminal_residual_codes import NODE_LEGACY
    from backend.banks.esun_spa.collection import poll_observation

    harness = NODE_LEGACY.replace('[source,family,fault]', '[source,family,fault,failAt]')
    harness = harness.replace('const trace=[];', '''const trace=[]; let ordinal=0;
const push=Array.prototype.push;
trace.push=(...a)=>{push.apply(trace,a); if(++ordinal===failAt) throw new TypeError('native fault'); return trace.length};''')
    harness = harness.replace("const result=require('vm')", "let result,error=null; try { result=require('vm')")
    harness = harness.replace('console.log(JSON.stringify({result,trace}));',
                              '} catch(e) { error=[e.name,e.message]; } console.log(JSON.stringify({result,trace,error}));')
    source = (ROOT / 'backend/banks/esun.py').read_text()
    actual = next(n.value.args[0].value for n in ast.walk(tail(ast.parse(source))[0])
                  if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                  and n.targets[0].id == name)
    assert actual == JS_ANNOTATED[name]
    def run(script, at):
        return json.loads(subprocess.run(['node', '-e', harness],
            input=json.dumps([script, family, 0, at]), text=True, capture_output=True,
            check=True, timeout=10).stdout)
    normal = run(JS_ORIGINAL[name], 0)
    for at in range(len(normal['trace']) + 1):
        old, new = run(JS_ORIGINAL[name], at), run(actual, at)
        assert old['trace'] == new['trace'] and old['error'] == new['error'], (name, at, old, new)
        assert old.get('result') == poll_observation(new.get('result'), ())[0]


@pytest.mark.parametrize('mode', ['success', 'lookup', 'ordinary', 'fatal'])
def test_popup_cleanup_existing_expression_wrapper_preserves_native_behavior(mode):
    import contextlib
    from backend.core.error_codes import error_step

    class Cancel(BaseException):
        pass

    original = subprocess.check_output(
        ['git', 'show', '16f2d4b130f44973d346b420f3d486ac839f71ac:backend/banks/esun.py'],
        cwd=ROOT, text=True,
    )
    outcomes = []
    for source in (original, (ROOT / 'backend/banks/esun.py').read_text()):
        trace = []
        primary = Cancel() if mode == 'fatal' else OSError('synthetic close failure')

        class Popup:
            def __getattribute__(self, name):
                if name == 'close':
                    trace.append('lookup')
                    if mode == 'lookup':
                        raise primary
                return object.__getattribute__(self, name)

            def close(self):
                trace.append('close')
                if mode in {'ordinary', 'fatal'}:
                    raise primary

        loop = next(n for n in ast.walk(tail(ast.parse(source))[0])
                    if isinstance(n, ast.For) and isinstance(n.target, ast.Name)
                    and n.target.id == 'popup')
        namespace = {'pages': [Popup()], 'page': None, 'contextlib': contextlib,
                     'error_step': error_step}
        escaped = None
        try:
            exec(compile(ast.Module(body=[deepcopy(loop)], type_ignores=[]), '<popup-cleanup>', 'exec'), namespace)
        except BaseException as error:
            assert error is primary
            escaped = type(error)
        outcomes.append((trace, escaped))
    assert outcomes[0] == outcomes[1]
    assert outcomes[1] == (['lookup'] if mode == 'lookup' else ['lookup', 'close'],
                           Cancel if mode == 'fatal' else None)
