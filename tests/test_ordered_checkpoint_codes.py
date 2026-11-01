"""Offline injection into the real ordered checkpoint evaluator and reducer."""
from dataclasses import replace

import pytest

from backend.core import login_checkpoints as cp
from backend.core.error_codes import BANKS, code_details, exception_error_id, with_error_code
from tests.test_login_checkpoint_browser_rules import Locator, Node, Page


FORM = "input, select, textarea, [contenteditable]:not([contenteditable='false'])"
ACTIONS = cp.DEFAULT_ACTION_SELECTOR


@pytest.fixture
def notice():
    action = Node(text="Continue")
    container = Node(text="synthetic private body", queries={ACTIONS: [action]})
    rule = cp.LoginCheckpointRule(
        name="fixture-notice", bank="ctbc", phases=tuple(cp.CheckpointPhase),
        kind=cp.CheckpointKind.DISMISSIBLE_NOTICE, container_selector="#notice",
        action_texts=("Continue",),
    )
    return Page({"#notice": [container]}), container, action, rule


def evaluate(notice, **kwargs):
    page, _, _, rule = notice
    return cp.evaluate_login_checkpoint(
        page, bank=rule.bank, phase=cp.CheckpointPhase.POST_SUBMIT,
        rules=(rule,), is_authenticated=lambda _: False, **kwargs,
    )


def terminal(outcome, rules=()):
    validated = cp.validate_login_checkpoint_outcome(outcome, rules)
    with pytest.raises(cp.LoginCheckpointBlocked) as caught:
        cp.reduce_login_checkpoint(cp.CheckpointPhase.POST_SUBMIT, cp.LoginBudget(), validated)
    assert type(caught.value) is cp.LoginCheckpointBlocked
    return caught.value


def test_form_control_reason_survives_evaluate_validate_reduce(notice):
    _, container, action, rule = notice
    container.queries[FORM] = [Node()]
    outcome = evaluate(notice)
    assert exception_error_id(terminal(outcome, (rule,)), bank="ctbc") == "CTBC-03-971-001"
    assert action.clicks == 0


def test_generic_unknown_has_no_source_code():
    assert exception_error_id(terminal(cp.CheckpointOutcome(cp.CheckpointKind.UNKNOWN_BLOCKER))) is None


BLOCKERS = "dialog[open], [role='dialog'], [aria-modal='true'], .modal.show, .modal.in"
NATIVE_ACTIONS = "button, a[href], input, select, textarea, summary, [role=button]"


@pytest.mark.parametrize("site, suffix", [
    ("zero", "972-001"), ("multiple", "972-002"),
    ("containers", "972-003"), ("blockers", "972-004"), ("actions", "972-005"),
    ("native_submit", "973-001"), ("native_error", "973-002"),
    ("enabled_error", "974-001"), ("selected_error", "974-002"),
    ("guard_denied", "975-001"), ("guard_error", "975-002"),
    ("click_error", "976-001"), ("wait_error", "977-001"),
    ("progress_error", "978-011"), ("no_progress", "978-002"),
])
def test_native_blocker_sources_are_distinct(notice, monkeypatch, site, suffix):
    page, container, action, rule = notice
    original = ValueError("synthetic private failure")
    guard_calls = []

    def fail(*args, **kwargs):
        raise original

    def guard():
        guard_calls.append(True)
        if site == "guard_error":
            raise original
        return site != "guard_denied"

    if site == "zero":
        container.queries[ACTIONS] = []
    elif site == "multiple":
        container.queries[ACTIONS] = [action, Node(text="Continue")]
    elif site in {"containers", "blockers", "actions"}:
        rule = replace(rule, require_exclusive_action=True)
        notice = page, container, action, rule
        page.root.queries[BLOCKERS] = [container]
        container.queries[NATIVE_ACTIONS] = [action]
        if site == "containers":
            page.root.queries["#notice"].append(Node())
        elif site == "blockers":
            page.root.queries[BLOCKERS] = []
        else:
            container.queries[NATIVE_ACTIONS].append(Node())
    elif site.startswith("native_"):
        monkeypatch.setattr(Locator, "evaluate", fail if site == "native_error" else lambda *_: True, raising=False)
    elif site == "enabled_error":
        monkeypatch.setattr(Locator, "is_enabled", fail)
    elif site == "selected_error":
        monkeypatch.setattr(Locator, "get_attribute", fail)
    elif site == "click_error":
        action.on_click = fail
    elif site == "wait_error":
        container.wait_error = original
    elif site == "progress_error":
        visible = Locator.is_visible

        def progress_visible(locator):
            if action.clicks:
                raise original
            return visible(locator)

        monkeypatch.setattr(Locator, "is_visible", progress_visible)
        container.wait_error = TimeoutError()

    outcome = evaluate(notice, can_act=guard)
    blocked = terminal(outcome, (rule,))
    assert exception_error_id(blocked, bank="ctbc") == f"CTBC-03-{suffix}"
    assert action.clicks == int(site in {"click_error", "wait_error", "progress_error", "no_progress"})
    assert len(guard_calls) == int(site.startswith("guard_") or bool(action.clicks))
    assert "synthetic private" not in str(blocked)
    assert "synthetic private" not in repr(outcome)


def test_deepest_native_reason_survives_validation(notice):
    _, _, action, rule = notice
    leaf = with_error_code(ValueError("synthetic private leaf"), "CTBC-03-971-001")

    def fail(_):
        try:
            raise leaf
        except ValueError as cause:
            raise ValueError("synthetic private wrapper") from cause

    action.on_click = fail
    outcome = evaluate(notice)
    assert exception_error_id(terminal(outcome, (rule,))) == "CTBC-03-971-001"
    # Provenance rejection must not replace a more precise registered reason.
    assert exception_error_id(terminal(outcome)) == "CTBC-03-971-001"


@pytest.mark.parametrize("site, suffix", [
    ("bank_mismatch", "987-001"), ("authentication", "984-001"),
    ("settle_authentication", "984-002"), ("scopes", "985-001"),
    ("rule", "979-001"), ("timeout_enter", "986-001"),
    ("timeout_restore", "986-002"), ("wrapper", "986-003"),
    ("post_fallback", "988-001"), ("settle_fallback", "988-002"),
])
def test_evaluator_boundaries_keep_operation_reason(notice, monkeypatch, site, suffix):
    page, _, action, rule = notice
    original = ValueError("synthetic private inspection")
    calls = []
    rules = (rule,)
    phase = cp.CheckpointPhase.POST_SUBMIT

    def fail(*args, **kwargs):
        raise original

    def authenticated(_):
        calls.append("authenticated")
        if "authentication" in site:
            raise original
        return False

    def owned(_):
        calls.append("owned")
        raise original

    timeout = page.set_default_timeout

    def set_timeout(value):
        calls.append(value)
        if site == "timeout_enter" and value == 5000 or site == "timeout_restore" and value == 180000:
            raise original
        timeout(value)

    monkeypatch.setattr(page, "set_default_timeout", set_timeout)
    if site == "bank_mismatch":
        rules = (replace(rule, bank="hsbc"),)
    elif site == "scopes":
        page.frames.append(object())
    elif site == "rule":
        monkeypatch.setattr(cp, "_evaluate_rule", fail)
    elif site == "wrapper":
        monkeypatch.setattr(cp, "_evaluate_login_checkpoint", fail)
    elif site in {"timeout_restore", "post_fallback", "settle_fallback", "settle_authentication"}:
        rules = ()
    if site.startswith("settle_"):
        phase = cp.CheckpointPhase.POST_SUBMIT_SETTLE

    outcome = cp.evaluate_login_checkpoint(
        page, bank="ctbc", phase=phase, rules=rules,
        is_authenticated=authenticated, is_scope_owned=owned,
    )
    assert exception_error_id(terminal(outcome, rules)) == f"CTBC-03-{suffix}"
    assert action.clicks == 0
    if site == "bank_mismatch":
        assert calls == []
    elif site == "timeout_enter":
        assert calls == [5000]
    elif site.startswith("settle_"):
        assert calls == [5000, "authenticated", 180000]


def test_unknown_bank_uses_system_reason_without_coercion():
    class PrivateBank:
        def __str__(self):
            raise AssertionError("must not stringify bank")

        def __hash__(self):
            raise AssertionError("must not hash bank")

    outcome = cp.evaluate_login_checkpoint(
        Page(), bank=PrivateBank(), phase=cp.CheckpointPhase.POST_SUBMIT,
        rules=(), is_authenticated=lambda _: False,
    )
    assert exception_error_id(terminal(outcome)) == "SYS-03-988-001"


def test_invalid_provenance_gets_its_own_reason():
    outcome = cp.CheckpointOutcome(cp.CheckpointKind.OTP_REQUIRED, rule_name="not-active")
    assert exception_error_id(terminal(outcome)) == "SYS-03-989-001"


@pytest.mark.parametrize("kind, phase, values, rule_name, suffix", [
    (cp.CheckpointKind.READY_FOR_CREDENTIALS, cp.CheckpointPhase.POST_SUBMIT, {}, None, "980-001"),
    (cp.CheckpointKind.READY_FOR_CREDENTIALS, cp.CheckpointPhase.PRE_SUBMIT, {"credential_submissions": 1}, None, "980-002"),
    (cp.CheckpointKind.PROTOCOL_RESUBMIT, cp.CheckpointPhase.PRE_SUBMIT, {}, "rule", "981-001"),
    (cp.CheckpointKind.PROTOCOL_RESUBMIT, cp.CheckpointPhase.POST_SUBMIT, {}, "rule", "981-002"),
    (cp.CheckpointKind.PROTOCOL_RESUBMIT, cp.CheckpointPhase.POST_SUBMIT, {"credential_submissions": 1, "protocol_resubmits": 1}, "rule", "981-003"),
    (cp.CheckpointKind.PROTOCOL_RESUBMIT, cp.CheckpointPhase.POST_SUBMIT, {"credential_submissions": 1}, None, "981-004"),
    (cp.CheckpointKind.CAPTCHA_RETRY, cp.CheckpointPhase.PRE_SUBMIT, {}, "rule", "982-001"),
    (cp.CheckpointKind.CAPTCHA_RETRY, cp.CheckpointPhase.POST_SUBMIT, {}, "rule", "982-002"),
    (cp.CheckpointKind.CAPTCHA_RETRY, cp.CheckpointPhase.POST_SUBMIT, {"credential_submissions": 1, "captcha_resubmits": 1}, "rule", "982-003"),
    (cp.CheckpointKind.CAPTCHA_RETRY, cp.CheckpointPhase.POST_SUBMIT, {"credential_submissions": 1}, None, "982-004"),
    (cp.CheckpointKind.STARTUP_RECOVERY, cp.CheckpointPhase.PRE_SUBMIT, {"reloads": 1}, None, "983-001"),
    ("unsupported", cp.CheckpointPhase.PRE_SUBMIT, {}, None, "989-002"),
])
def test_reducer_rejections_are_condition_specific(kind, phase, values, rule_name, suffix):
    # Inject otherwise unreachable counter combinations to exercise each native
    # independent guard without weakening LoginBudget's production validation.
    budget = object.__new__(cp.LoginBudget)
    for key, value in (vars(cp.LoginBudget()) | values).items():
        object.__setattr__(budget, key, value)
    outcome = cp.CheckpointOutcome(kind, rule_name=rule_name)
    with pytest.raises(cp.LoginCheckpointBlocked) as caught:
        cp.reduce_login_checkpoint(phase, budget, outcome)
    assert type(caught.value) is cp.LoginCheckpointBlocked
    assert caught.value.budget is budget
    assert caught.value.outcome is outcome
    assert exception_error_id(caught.value) == f"SYS-03-{suffix}"


@pytest.mark.parametrize("values, suffix", [
    ({"reloads": True}, "983-002"),
    ({"reloads": 2}, "983-003"),
    ({"credential_submissions": 2}, "983-004"),
])
def test_budget_constructor_keeps_valueerror_with_specific_reason(values, suffix):
    with pytest.raises(ValueError, match="^invalid login budget$") as caught:
        cp.LoginBudget(**values)
    assert type(caught.value) is ValueError
    assert exception_error_id(caught.value) == f"SYS-03-{suffix}"


def test_specific_outcome_reason_wins_over_budget_fallback():
    outcome = cp.CheckpointOutcome(cp.CheckpointKind.CAPTCHA_RETRY, error_id="CTBC-03-971-001")
    with pytest.raises(cp.LoginCheckpointBlocked) as caught:
        cp.reduce_login_checkpoint(cp.CheckpointPhase.PRE_SUBMIT, cp.LoginBudget(), outcome)
    assert exception_error_id(caught.value) == "CTBC-03-971-001"


@pytest.mark.parametrize("site, suffix", [
    ("containers", "970-015"), ("body", "970-002"),
    ("form_controls", "971-011"), ("native_actions", "972-011"),
    ("eligible_actions", "972-015"),
])
def test_rule_inspection_native_exception_identity_and_source(notice, monkeypatch, site, suffix):
    page, container, action, rule = notice
    original = ValueError("synthetic private inspection payload")
    query = Locator.locator
    text = Locator.inner_text
    if site == "native_actions":
        rule = replace(rule, require_exclusive_action=True)
        notice = page, container, action, rule
        page.root.queries[BLOCKERS] = [container]

    def fail(*args, **kwargs):
        raise original

    def locator(current, selector):
        if selector == {"form_controls": FORM, "native_actions": NATIVE_ACTIONS, "eligible_actions": ACTIONS}.get(site):
            raise original
        return query(current, selector)

    def inner_text(current, **kwargs):
        if site == "body" and current.nodes[0] is container:
            raise original
        return text(current, **kwargs)

    if site == "containers":
        monkeypatch.setattr(page, "locator", fail)
    monkeypatch.setattr(Locator, "locator", locator)
    monkeypatch.setattr(Locator, "inner_text", inner_text)
    with pytest.raises(ValueError) as direct:
        cp._evaluate_rule([page], rule)
    assert direct.value is original
    assert exception_error_id(original) == f"CTBC-03-{suffix}"
    assert exception_error_id(terminal(evaluate(notice), (rule,))) == f"CTBC-03-{suffix}"
    assert action.clicks == 0


def test_rule_bank_inspection_failure_preserves_native_throw_and_inner_outcome():
    original = ValueError("synthetic private bank metadata")

    class BrokenRule:
        @property
        def bank(self):
            raise original

    kwargs = dict(
        bank="ctbc", phase=cp.CheckpointPhase.POST_SUBMIT,
        rules=(BrokenRule(),), is_authenticated=lambda _: False,
    )
    with pytest.raises(ValueError) as caught:
        cp.evaluate_login_checkpoint(Page(), **kwargs)
    assert caught.value is original
    assert exception_error_id(original) == "CTBC-03-987-002"
    outcome = cp._evaluate_login_checkpoint(Page(), **kwargs)
    assert exception_error_id(terminal(outcome)) == "CTBC-03-987-002"


@pytest.mark.parametrize("value", [object(), lambda: "private", "private", "CTBC-03-999-999"])
def test_metadata_field_is_closed_and_does_not_change_generic_blocker(value):
    outcome = cp.CheckpointOutcome(cp.CheckpointKind.UNKNOWN_BLOCKER, error_id=value)
    assert outcome.error_id is None
    assert exception_error_id(terminal(outcome)) is None


def test_diagnostic_metadata_never_invokes_private_hooks():
    calls = []

    class Private:
        def hook(self, *args, **kwargs):
            calls.append(True)
            raise AssertionError("private metadata hook must not execute")

        __str__ = __repr__ = __hash__ = __eq__ = __bool__ = __call__ = hook
        __getattr__ = __format__ = hook

    outcome = cp.CheckpointOutcome(cp.CheckpointKind.UNKNOWN_BLOCKER, error_id=Private())
    assert outcome.error_id is None
    assert exception_error_id(terminal(outcome)) is None
    assert calls == []


def test_source_catalog_is_literal_registered_and_complete():
    import ast
    import inspect

    from backend.core.error_catalog import checkpoints

    source = ast.parse(inspect.getsource(cp))
    ids = {node.value for node in ast.walk(source) if isinstance(node, ast.Constant)
           and type(node.value) is str and node.value.startswith("SYS-03-")}
    assert ids == set(checkpoints.CODES)
    for error_id in ids:
        assert 970 <= int(error_id.split("-")[2]) <= 989
        for bank in (*sorted(BANKS), None):
            qualified = cp._checkpoint_code(bank, error_id)
            details = code_details(qualified, bank=bank)
            assert details == checkpoints.CODES[error_id]
            assert all(any("\u4e00" <= character <= "\u9fff" for character in text) for text in details)


@pytest.mark.parametrize("progress", ["hidden", "action_hidden", "disabled", "selected", "fingerprint"])
def test_successful_progress_keeps_original_short_circuit_reads(notice, monkeypatch, progress):
    _, container, action, _ = notice
    calls = []
    for method in ("is_visible", "is_enabled", "get_attribute", "inner_text"):
        original = getattr(Locator, method)

        def tracked(self, *args, _name=method, _original=original, **kwargs):
            calls.append((_name, "container" if self.nodes[0] is container else "action"))
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(Locator, method, tracked)

    def mutate(_):
        if progress == "hidden":
            container.visible = False
        elif progress == "action_hidden":
            action.visible = False
        elif progress == "disabled":
            action.enabled = False
        elif progress == "selected":
            action.attrs["aria-selected"] = "true"
        else:
            container.text = "changed synthetic body"

    action.on_click = mutate
    outcome = evaluate(notice)
    assert outcome.kind is cp.CheckpointKind.DISMISSIBLE_NOTICE
    assert outcome.error_id is None
    assert action.clicks == 1
    baseline = [
        ("is_visible", "container"), ("inner_text", "container"),
        ("is_visible", "action"), ("inner_text", "action"),
        ("is_enabled", "action"), *[("get_attribute", "action")] * 6,
        ("is_visible", "container"),
    ]
    tails = {
        "hidden": [],
        "action_hidden": [("is_visible", "container"), ("is_visible", "action")],
        "disabled": [("is_visible", "container"), ("is_visible", "action"), ("is_enabled", "action")],
        "selected": [("is_visible", "container"), ("is_visible", "action"), ("is_enabled", "action"), *[("get_attribute", "action")] * 3],
        "fingerprint": [("is_visible", "container"), ("is_visible", "action"), ("is_enabled", "action"), *[("get_attribute", "action")] * 6, ("inner_text", "container")],
    }
    assert calls == baseline + tails[progress]


LEAF_SITES = {
    "blocker.lookup": "970-011", "blocker.nth": "970-012", "blocker.handle": "970-013", "blocker.visible": "970-014",
    "container.lookup": "970-015", "container.nth": "970-016", "container.handle": "970-017", "container.visible": "970-018",
    "nested.lookup": "970-019", "nested.nth": "970-020", "nested.handle": "970-021", "nested.visible": "970-022",
    "form.lookup": "971-011", "form.nth": "971-012", "form.handle": "971-013", "form.visible": "971-014",
    "native.lookup": "972-011", "native.nth": "972-012", "native.handle": "972-013", "native.visible": "972-014",
    "candidate.lookup": "972-015", "candidate.nth": "972-016", "candidate.handle": "972-017", "candidate.visible": "972-018", "candidate.label": "972-019",
    "post.container": "978-011", "post.action": "978-012", "post.enabled": "978-013", "post.selected": "978-014", "post.body": "978-015",
}


def _native_leaf_run(fault=None, *, proxy=False):
    """Real evaluator/validator/reducer; only native DOM dependencies are fake."""
    trace = []
    failure = ValueError("synthetic leaf injection")
    action = Node(text="Continue")
    container = Node(text="body")
    rule = cp.LoginCheckpointRule(
        name="leaf", bank="ctbc", phases=tuple(cp.CheckpointPhase),
        kind=cp.CheckpointKind.DISMISSIBLE_NOTICE, container_selector="#notice",
        action_texts=("Continue",), require_exclusive_action=True,
    )

    class BrokenProxy:
        def fail(self, *args):
            raise failure
        __bool__ = __radd__ = split = lower = encode = fail

    def record(site):
        trace.append(site)
        if site == fault:
            if proxy:
                return BrokenProxy()
            raise failure
        return None

    class NativeLocator(Locator):
        def __init__(self, nodes, role):
            super().__init__(nodes)
            self.role = role

        def locator(self, selector):
            role = {"#notice": "nested", FORM: "form", NATIVE_ACTIONS: "native", ACTIONS: "candidate"}[selector]
            record(role + ".lookup")
            nodes = {"nested": [Node(visible=False)], "form": [Node(visible=False)], "native": [action], "candidate": [action]}[role]
            return NativeLocator(nodes, role)

        def nth(self, index):
            record(self.role + ".nth")
            return NativeLocator([self.nodes[index]], self.role)

        def element_handle(self, **kwargs):
            record(self.role + ".handle")
            return super().element_handle(**kwargs)

        def is_visible(self):
            site = ("post.container" if self.role == "container" else "post.action") if action.clicks else self.role + ".visible"
            result = record(site)
            return result if result is not None else super().is_visible()

        def inner_text(self, **kwargs):
            result = record("post.body" if action.clicks else "candidate.label" if self.role == "candidate" else "container.body")
            return result if result is not None else super().inner_text(**kwargs)

        def is_enabled(self):
            result = record("post.enabled" if action.clicks else "initial.enabled")
            return result if result is not None else super().is_enabled()

        def get_attribute(self, name):
            result = record("post.selected" if action.clicks else "initial.selected")
            if proxy and fault == "post.selected" and name in {"checked", "selected"}:
                return None
            return result if result is not None else super().get_attribute(name)

        def click(self):
            record("click")
            return super().click()

        def wait_for(self, **kwargs):
            record("wait")
            raise TimeoutError()

    class NativePage(Page):
        def locator(self, selector):
            role = "blocker" if selector == BLOCKERS else "container"
            record(role + ".lookup")
            return NativeLocator([container], role)

        def set_default_timeout(self, value):
            record("timeout." + str(value))
            super().set_default_timeout(value)

    page = NativePage()
    outcome = cp.evaluate_login_checkpoint(
        page, bank="ctbc", phase=cp.CheckpointPhase.POST_SUBMIT,
        rules=(rule,), is_authenticated=lambda _: False,
    )
    blocked = terminal(outcome, (rule,))
    return exception_error_id(blocked), trace, action.clicks, failure, page.default_timeout


@pytest.mark.parametrize("site, suffix", LEAF_SITES.items())
def test_native_leaves_survive_actual_evaluate_validate_reduce(site, suffix):
    _, baseline, _, _, _ = _native_leaf_run()
    error_id, trace, clicks, original, timeout = _native_leaf_run(site)
    assert error_id == f"CTBC-03-{suffix}"
    assert exception_error_id(original) == error_id
    assert type(original) is ValueError
    assert trace == [*baseline[:baseline.index(site) + 1], "timeout.180000"]
    assert clicks == int(site.startswith("post."))
    assert timeout == 180000


@pytest.mark.parametrize("site", [
    "blocker.visible", "container.visible", "nested.visible", "form.visible", "native.visible",
    "candidate.visible", "candidate.label", "post.container", "post.action", "post.enabled", "post.selected", "post.body",
])
def test_leaf_wraps_complete_proxy_operand(site):
    error_id, trace, clicks, original, timeout = _native_leaf_run(site, proxy=True)
    assert error_id == f"CTBC-03-{LEAF_SITES[site]}"
    assert exception_error_id(original) == error_id
    assert trace.count(site) == (3 if site == "post.selected" else 1)
    assert trace[-1] == "timeout.180000"
    assert clicks == int(site.startswith("post."))
    assert timeout == 180000


@pytest.mark.parametrize("operand, fallback", [
    ("970-018", "970-001"), ("971-014", "971-002"),
    ("972-014", "972-006"), ("972-018", "972-007"), ("978-011", "978-001"),
])
def test_previous_broad_ids_remain_last_resort_boundaries(monkeypatch, operand, fallback):
    original = cp._checkpoint_operand

    def broken_boundary(bank, error_id, operation):
        if error_id == f"SYS-03-{operand}":
            raise ValueError("synthetic boundary failure before leaf entry")
        return original(bank, error_id, operation)

    monkeypatch.setattr(cp, "_checkpoint_operand", broken_boundary)
    error_id, _, clicks, _, timeout = _native_leaf_run()
    assert error_id == f"CTBC-03-{fallback}"
    assert clicks == int(operand.startswith("978"))
    assert timeout == 180000
