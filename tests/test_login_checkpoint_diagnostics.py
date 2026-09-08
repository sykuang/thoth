"""Offline diagnostics: real run sink, no credential/browser access."""
import logging
from types import SimpleNamespace

import pytest
from backend.core import base, login_checkpoints as checkpoints
from tests.test_bank_login_lifecycle import _StagedCrawler, _run


def test_run_emits_phase_reason_and_static_bank_rule_at_warning(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(base, "DATA_ROOT", tmp_path / "initial")
    crawler = _StagedCrawler(name="ctbc")
    outcome = checkpoints.CheckpointOutcome(checkpoints.CheckpointKind.UNKNOWN_BLOCKER,
                                           "ctbc-unknown-modal")
    # Works before the field exists so RED demonstrates the actual lost sink data.
    object.__setattr__(outcome, "reason", "matched_blocker")
    def fail(page):
        raise checkpoints.LoginCheckpointBlocked(checkpoints.LoginBudget(1), outcome,
                                                 phase=checkpoints.CheckpointPhase.POST_SUBMIT)
    monkeypatch.setattr(crawler, "_shared_login", fail)
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)
    result, _ = _run(monkeypatch, tmp_path, crawler, None)
    assert "phase=post_submit" in result["error"]
    assert "rule=ctbc-unknown-modal" in result["error"]
    assert "reason=matched_blocker" in result["error"]
    assert result["error"] in capsys.readouterr().err

from tests.test_login_checkpoint_browser_rules import Page, Node, Locator


def test_terminal_is_lightweight_and_retains_typed_evidence(monkeypatch):
    import builtins
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        assert name != "backend.core.base", "lower layer imports browser runtime"
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    outcome = checkpoints.CheckpointOutcome(checkpoints.CheckpointKind.UNKNOWN_BLOCKER)
    budget = checkpoints.LoginBudget(1)
    error = checkpoints.LoginCheckpointBlocked(budget, outcome, phase=checkpoints.CheckpointPhase.POST_SUBMIT)
    assert str(error) == "terminal login checkpoint; details withheld"
    assert error.outcome is outcome and error.budget is budget
    assert error.phase is checkpoints.CheckpointPhase.POST_SUBMIT


@pytest.mark.parametrize("fault", [False, True])
def test_origin_failure_distinguishes_exception_without_reread(monkeypatch, tmp_path, fault):
    monkeypatch.setattr(base, "DATA_ROOT", tmp_path)
    crawler = _StagedCrawler(name="ctbc")
    monkeypatch.setattr(crawler, "_credential_origin_allowed", base.BankCrawler._credential_origin_allowed.__get__(crawler))
    reads = []
    class OriginPage:
        @property
        def url(self):
            reads.append(True)
            if fault:
                raise RuntimeError("PRIVATE")
            return "https://foreign.invalid/PRIVATE"
    with pytest.raises(checkpoints.LoginCheckpointBlocked) as raised:
        crawler._shared_login(OriginPage())
    assert raised.value.outcome.reason == ("origin_inspection_exception" if fault else "origin_before_prepare")
    assert reads == [True] and crawler.submissions == 0
    assert not crawler.events


@pytest.mark.parametrize("fault", [False, True])
def test_dialog_dismiss_exception_has_distinct_diagnostic(monkeypatch, tmp_path, fault):
    monkeypatch.setattr(base, "DATA_ROOT", tmp_path)
    crawler = _StagedCrawler(name="ctbc")
    callbacks = []
    base.BankCrawler.attach_shared_dialog_handler(crawler, SimpleNamespace(on=lambda _, f: callbacks.append(f)))
    calls = []
    def dismiss():
        calls.append(True)
        if fault:
            raise RuntimeError("PRIVATE")
    callbacks[0](SimpleNamespace(dismiss=dismiss))
    with pytest.raises(checkpoints.LoginCheckpointBlocked) as raised:
        crawler._shared_login(Page())
    assert raised.value.outcome.reason == ("dialog_dismiss_exception" if fault else "dialog_blocked")
    assert calls == [True] and crawler.submissions == 0


def test_evaluator_reason_traces_without_extra_actions():
    outcome = checkpoints.evaluate_login_checkpoint(Page(), bank="ctbc",
        phase=checkpoints.CheckpointPhase.POST_SUBMIT, rules=(), is_authenticated=lambda p: False)
    assert getattr(outcome, "reason", None) == "no_matching_checkpoint"


def test_unknown_producers_all_carry_static_reason():
    import ast
    from pathlib import Path
    for path in ("backend/core/login_checkpoints.py", "backend/core/base.py",
                 *(str(p.relative_to(Path(__file__).parents[1]))
                   for p in (Path(__file__).parents[1] / "backend/banks").glob("*.py"))):
        tree = ast.parse((Path(__file__).parents[1] / path).read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "CheckpointOutcome" and node.args
                    and isinstance(node.args[0], ast.Attribute)
                    and node.args[0].attr == "UNKNOWN_BLOCKER"):
                assert any(k.arg == "reason" for k in node.keywords), (path, node.lineno)


class Hostile:
    def __str__(self): raise AssertionError("private str hook")
    def __eq__(self, other): raise AssertionError("private equality hook")
    def __bool__(self): raise AssertionError("private bool hook")


def test_terminal_constructor_never_formats_untrusted_fields():
    outcome = checkpoints.CheckpointOutcome(checkpoints.CheckpointKind.UNKNOWN_BLOCKER,
                                           Hostile(), Hostile(), Hostile())
    error = checkpoints.LoginCheckpointBlocked(Hostile(), outcome, phase=Hostile())
    assert "private" not in str(error)


@pytest.mark.parametrize("value", [Hostile(), "PRIVATE_ACCOUNT_987654", "ctbc-unknown-modal", None])
def test_diagnostics_reject_untrusted_and_crossbank_fields(value):
    text = base._safe_login_diagnostics("ubot", {"phase": value},
                                       {"reason": value, "rule_name": value})
    assert text == "phase=unknown, reason=unspecified, rule=unknown"


def test_collect_checkpoint_preserves_safe_diagnostics(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(base, "DATA_ROOT", tmp_path / "initial")
    crawler = _StagedCrawler(name="sinopac")
    monkeypatch.setattr(crawler, "_shared_login", lambda p: True)
    def collect(*args):
        checkpoints.reduce_login_checkpoint(checkpoints.CheckpointPhase.POST_SUBMIT_SETTLE,
            checkpoints.LoginBudget(1), checkpoints.CheckpointOutcome(
                checkpoints.CheckpointKind.UNKNOWN_BLOCKER, "sinopac-unknown-modal",
                Hostile(), Hostile(), reason=checkpoints.CheckpointReason.MATCHED_BLOCKER))
    monkeypatch.setattr(crawler, "collect", collect)
    result, _ = _run(monkeypatch, tmp_path, crawler, None)
    assert "code=collect_checkpoint" in result["error"]
    assert "phase=post_submit_settle, reason=matched_blocker, rule=sinopac-unknown-modal" in result["error"]
    assert result["error"] in capsys.readouterr().err
    assert crawler.events.count("logout") == 1


@pytest.mark.parametrize("blocked,origin,expected,calls_expected", [
    (True, True, "dialog_blocked", []),
    (False, False, "navigation_origin", ["origin"]),
    (False, True, "matched_blocker", ["origin", "#ib_init_connect_error_popup"]),
])
def test_rakuten_direct_producer_preserves_short_circuit(monkeypatch, blocked, origin, expected, calls_expected):
    from backend.banks import rakuten
    crawler = object.__new__(rakuten.RakutenCrawler)
    crawler._shared_dialog_blocked = blocked
    calls = []
    monkeypatch.setattr(crawler, "_credential_origin_allowed", lambda p: calls.append("origin") or origin)
    monkeypatch.setattr(rakuten, "_any_visible", lambda p, selector: calls.append(selector) or True)
    with pytest.raises(checkpoints.LoginCheckpointBlocked) as raised:
        crawler._goto_twd(object())
    assert calls == calls_expected
    assert raised.value.outcome.reason == expected
    assert raised.value.outcome.rule_name == ("rakuten-startup-connect-error" if expected == "matched_blocker" else None)


@pytest.mark.parametrize("dialog", [False, True])
def test_collect_origin_or_dialog_exception_reason(monkeypatch, tmp_path, capsys, dialog):
    monkeypatch.setattr(base, "DATA_ROOT", tmp_path / "initial")
    crawler = _StagedCrawler(name="ctbc")
    def login(p):
        if dialog:
            crawler._shared_dialog_blocked = True
            crawler._dialog_dismiss_failed = True
        else:
            crawler._origin_inspection_failed = True
        return True
    monkeypatch.setattr(crawler, "_shared_login", login)
    monkeypatch.setattr(crawler, "_credential_origin_allowed", lambda p: False)
    result, _ = _run(monkeypatch, tmp_path, crawler, None)
    assert ("reason=dialog_dismiss_exception" if dialog else "reason=origin_inspection_exception") in result["error"]
    assert "collect" not in crawler.events


def test_outcome_positional_compatibility():
    outcome = checkpoints.CheckpointOutcome(checkpoints.CheckpointKind.UNKNOWN_BLOCKER,
                                           "rule", "label", "interaction")
    assert outcome.interaction == "interaction"
    assert getattr(outcome, "reason", None) == "unspecified"

@pytest.mark.parametrize("phase,kind,budget,reason", [
    (checkpoints.CheckpointPhase.POST_SUBMIT, checkpoints.CheckpointKind.READY_FOR_CREDENTIALS, checkpoints.LoginBudget(1), "invalid_transition"),
    (checkpoints.CheckpointPhase.POST_SUBMIT, checkpoints.CheckpointKind.STARTUP_RECOVERY, checkpoints.LoginBudget(1, 0, 1), "invalid_transition"),
])
def test_reducer_invalid_transition_has_diagnostic(phase, kind, budget, reason):
    with pytest.raises(checkpoints.LoginCheckpointBlocked) as raised:
        checkpoints.reduce_login_checkpoint(phase, budget, checkpoints.CheckpointOutcome(kind))
    assert raised.value.outcome.reason == reason


def test_frame_exception_distinct_from_auth_exception():
    def fail(*args): raise RuntimeError("PRIVATE")
    page = Page(frames=[object()])
    outcome = checkpoints.evaluate_login_checkpoint(page, bank="ctbc",
        phase=checkpoints.CheckpointPhase.POST_SUBMIT, rules=(),
        is_authenticated=lambda p: False, is_scope_owned=fail)
    assert outcome.reason == "frame_inspection_exception"


def test_real_sync_dispatch_receives_run_failure_without_persisting(monkeypatch, tmp_path, capsys):
    from backend.server import sync_runner, rules_repo
    from backend.core import store
    monkeypatch.setattr(base, "DATA_ROOT", tmp_path)
    crawler = _StagedCrawler(name="ctbc")
    def fail(page):
        raise checkpoints.LoginCheckpointBlocked(checkpoints.LoginBudget(1),
            checkpoints.CheckpointOutcome(checkpoints.CheckpointKind.UNKNOWN_BLOCKER,
                "ctbc-unknown-modal", reason=checkpoints.CheckpointReason.MATCHED_BLOCKER),
            phase=checkpoints.CheckpointPhase.POST_SUBMIT)
    monkeypatch.setattr(crawler, "_shared_login", fail)
    page = SimpleNamespace(on=lambda *a: None, frames=[], url="https://example.com")
    monkeypatch.setattr(crawler, "_execute_browser_flow", lambda *a, **kw: kw["page_action"](page))
    monkeypatch.setattr(sync_runner, "_load_crawler", lambda bank: (SimpleNamespace(BASE="https://example.com"), lambda: crawler))
    monkeypatch.setattr(rules_repo, "list_rules", lambda **kw: [])
    closed = []
    monkeypatch.setattr(store, "BankStore", lambda *a, **kw: SimpleNamespace(
        latest_twd_transaction_dates=lambda: {}, latest_card_transaction_dates=lambda: {},
        close=lambda: closed.append(True)))
    with pytest.raises(RuntimeError, match="^crawler_failed$"):
        sync_runner._dispatch_crawler_and_persist("ctbc", 1)
    assert closed == [True]
    assert "collect" not in crawler.events
    assert "phase=post_submit, reason=matched_blocker, rule=ctbc-unknown-modal" in capsys.readouterr().err



@pytest.mark.parametrize("modal", [False, True])
def test_sinopac_real_producer_reducer_run_sink(monkeypatch, tmp_path, capsys, modal):
    from backend.banks.sinopac import SinopacCrawler
    monkeypatch.setattr(base, "DATA_ROOT", tmp_path)
    crawler = _StagedCrawler(name="sinopac")
    crawler.rules = SinopacCrawler.login_checkpoint_rules(object.__new__(SinopacCrawler))
    page = Page()
    page.url = "https://example.com/PRIVATE_URL"
    page.on = lambda *a: None
    def submit(p):
        crawler.submissions += 1
        if modal:
            p.root.queries[".modal.show"] = [Node(text="PRIVATE_BODY")]
    monkeypatch.setattr(crawler, "submit_credentials_once", submit)
    monkeypatch.setattr(crawler, "_execute_browser_flow", lambda *a, **kw: kw["page_action"](page))
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)
    result = crawler.run("https://example.com")
    expected = ("reason=matched_blocker, rule=sinopac-unknown-modal" if modal
                else "reason=no_matching_checkpoint, rule=unknown")
    assert "phase=post_submit, " + expected in result["error"]
    stderr = capsys.readouterr().err
    assert result["error"] in stderr and "PRIVATE" not in stderr + repr(result)
    assert crawler.submissions == 1 and "collect" not in crawler.events


def test_code_owned_inventory_matches_every_bank_without_credentials():
    from importlib import import_module
    import inspect
    from tests.test_bank_login_lifecycle import _opted_in_bank_modules
    inventory = dict(base._SAFE_LOGIN_RULES)
    assert len(inventory) == 13
    assert set(inventory) == _opted_in_bank_modules()
    for bank, names in inventory.items():
        module = import_module(f"backend.banks.{bank}")
        cls, = [c for c in vars(module).values() if inspect.isclass(c)
                and c.__module__ == module.__name__ and issubclass(c, base.BankCrawler)]
        rules = cls.login_checkpoint_rules(object.__new__(cls))
        assert len(names) == len(set(names))
        assert set(names) == {r.name for r in rules}, bank
        for rule in rules:
            assert rule.bank == bank
            assert f"rule={rule.name}" in base._safe_login_diagnostics(bank, {}, {"rule_name": rule.name})
            assert "rule=unknown" in base._safe_login_diagnostics("other", {}, {"rule_name": rule.name})


@pytest.mark.parametrize("case,reason,clicks", [
    ("guard_denied", "action_guard_denied", 0),
    ("guard_exception", "action_guard_exception", 0),
    ("click_exception", "action_click_exception", 1),
    ("wait_exception", "progress_wait_exception", 1),
    ("progress_exception", "progress_inspection_exception", 1),
    ("no_progress", "no_progress", 1),
    ("nonunique", "action_not_unique", 0),
    ("form", "form_controls_present", 0),
    ("native", "native_form_submission", 0),
    ("rule_exception", "rule_inspection_exception", 0),
])
def test_evaluator_negative_matrix(monkeypatch, case, reason, clicks):
    def fail(*a, **kw): raise RuntimeError("PRIVATE")
    action = Node(text="Continue")
    container = Node(text="PRIVATE", queries={checkpoints.DEFAULT_ACTION_SELECTOR: [action]})
    rule = checkpoints.LoginCheckpointRule(name="ctbc-entry-announcement", bank="ctbc",
        phases=(checkpoints.CheckpointPhase.POST_SUBMIT,), kind=checkpoints.CheckpointKind.DISMISSIBLE_NOTICE,
        container_selector="#notice", action_texts=("Continue",))
    guard = None
    if case == "guard_denied": guard = lambda: False
    if case == "guard_exception": guard = fail
    if case == "click_exception": action.on_click = fail
    if case == "wait_exception": container.wait_error = RuntimeError("PRIVATE")
    if case == "progress_exception":
        container.wait_error = TimeoutError()
        action.on_click = lambda _: monkeypatch.setattr(Locator, "is_visible", fail)
    if case == "nonunique": container.queries[checkpoints.DEFAULT_ACTION_SELECTOR].append(Node(text="Continue"))
    if case == "form": container.queries["input, select, textarea, [contenteditable]:not([contenteditable='false'])"] = [Node()]
    if case == "native":
        from dataclasses import replace
        rule = replace(rule, kind=checkpoints.CheckpointKind.DUPLICATE_SESSION)
        monkeypatch.setattr(Locator, "evaluate", lambda *a: True)
    page = Page({"#notice": [container]})
    if case == "rule_exception": monkeypatch.setattr(page, "locator", fail)
    outcome = checkpoints.evaluate_login_checkpoint(page, bank="ctbc", phase=checkpoints.CheckpointPhase.POST_SUBMIT,
        rules=(rule,), is_authenticated=lambda p: False, can_act=guard)
    assert outcome.reason == reason
    assert outcome.kind is checkpoints.CheckpointKind.UNKNOWN_BLOCKER
    assert action.clicks == clicks


@pytest.mark.parametrize("collect", [False, True])
def test_run_ignores_hostile_exception_hooks_and_class_allowlists(monkeypatch, tmp_path, capsys, collect):
    monkeypatch.setattr(base, "DATA_ROOT", tmp_path / "initial")
    crawler = _StagedCrawler(name="sinopac")
    class Evil(checkpoints.LoginCheckpointBlocked):
        def __str__(self): raise AssertionError("PRIVATE")
        def __repr__(self): raise AssertionError("PRIVATE")
        def __getattribute__(self, name):
            if name in {"phase", "outcome", "budget", "__dict__"}: raise AssertionError("PRIVATE")
            return super().__getattribute__(name)
    error = Evil(checkpoints.LoginBudget(1), checkpoints.CheckpointOutcome(
        checkpoints.CheckpointKind.UNKNOWN_BLOCKER, "ctbc-unknown-modal", Hostile(), Hostile()), phase=Hostile())
    monkeypatch.setattr(_StagedCrawler, "_SAFE_LOGIN_RULES", (("sinopac", ("ctbc-unknown-modal",)),), raising=False)
    monkeypatch.setattr(crawler, "login_checkpoint_rules", lambda: (_ for _ in ()).throw(AssertionError("provider called")))
    def fail(*args): raise error
    monkeypatch.setattr(crawler, "_shared_login", (lambda p: True) if collect else fail)
    if collect: monkeypatch.setattr(crawler, "collect", fail)
    result, _ = _run(monkeypatch, tmp_path, crawler, None)
    assert "phase=unknown, reason=unspecified, rule=unknown" in result["error"]
    assert "PRIVATE" not in repr(result) + capsys.readouterr().err


@pytest.mark.parametrize("stage,reason,submissions", [
    (1, "origin_before_prepare", 0), (2, "origin_after_prepare", 0),
    (3, "origin_before_evaluation", 0), (4, "origin_after_evaluation", 0),
    (5, "origin_before_submit", 0),
])
def test_origin_stage_matrix(monkeypatch, tmp_path, stage, reason, submissions):
    monkeypatch.setattr(base, "DATA_ROOT", tmp_path)
    crawler = _StagedCrawler(name="ctbc", rules=())
    calls = []
    def allowed(p):
        calls.append(True)
        return len(calls) != stage
    monkeypatch.setattr(crawler, "_credential_origin_allowed", allowed)
    with pytest.raises(checkpoints.LoginCheckpointBlocked) as raised:
        crawler._shared_login(Page())
    assert raised.value.outcome.reason == reason
    assert len(calls) == stage and crawler.submissions == submissions


@pytest.mark.parametrize("case,reason", [
    ("auth", "authentication_exception"), ("frame", "frame_inspection_exception"),
    ("boundary", "inspection_exception"), ("bank", "bank_mismatch"),
])
def test_evaluator_boundary_matrix(monkeypatch, case, reason):
    def fail(*a, **kw): raise RuntimeError("PRIVATE")
    page = Page(frames=[object()])
    auth = fail if case == "auth" else lambda p: False
    scope = fail if case == "frame" else None
    if case == "boundary": monkeypatch.setattr(page, "set_default_timeout", fail)
    rules = ()
    if case == "bank":
        rules = (checkpoints.LoginCheckpointRule(name="foreign", bank="foreign",
            phases=(checkpoints.CheckpointPhase.POST_SUBMIT,), kind=checkpoints.CheckpointKind.UNKNOWN_BLOCKER,
            container_selector="#private"),)
        auth = fail
        monkeypatch.setattr(page, "locator", fail)
    outcome = checkpoints.evaluate_login_checkpoint(page, bank="ctbc", phase=checkpoints.CheckpointPhase.POST_SUBMIT,
        rules=rules, is_authenticated=auth, is_scope_owned=scope)
    assert outcome.reason == reason


@pytest.mark.parametrize("case,reason", [
    ("duplicates", "duplicate_rule_names"), ("bank", "bank_mismatch"),
    ("invalid", "invalid_outcome"), ("steps", "step_limit"),
    ("exhausted", "rule_budget_exhausted"),
])
def test_lifecycle_control_flow_matrix(monkeypatch, tmp_path, case, reason):
    monkeypatch.setattr(base, "DATA_ROOT", tmp_path)
    rule = checkpoints.LoginCheckpointRule(name="ctbc-entry-announcement", bank="ctbc",
        phases=(checkpoints.CheckpointPhase.PRE_SUBMIT,), kind=checkpoints.CheckpointKind.DISMISSIBLE_NOTICE,
        container_selector="#notice", action_texts=("Continue",))
    crawler = _StagedCrawler(name="ctbc", rules=(rule,))
    if case == "duplicates": crawler.rules = (rule, rule)
    if case == "bank": crawler.name = "sinopac"
    if case == "invalid":
        monkeypatch.setattr(base, "evaluate_login_checkpoint", lambda *a, **kw:
            checkpoints.CheckpointOutcome(checkpoints.CheckpointKind.OTP_REQUIRED, "foreign"))
    if case == "steps":
        monkeypatch.setattr(base, "evaluate_login_checkpoint", lambda *a, **kw:
            checkpoints.CheckpointOutcome(checkpoints.CheckpointKind.DISMISSIBLE_NOTICE, rule.name))
        monkeypatch.setattr(base, "validate_login_checkpoint_outcome", lambda o, r: o)
    action = Node(text="Continue")
    container = Node(text="PRIVATE", queries={checkpoints.DEFAULT_ACTION_SELECTOR: [action]})
    action.on_click = lambda _: setattr(container, "text", "PRIVATE_CHANGED")
    page = Page({"#notice": [container]})
    with pytest.raises(checkpoints.LoginCheckpointBlocked) as raised:
        crawler._shared_login(page)
    assert raised.value.outcome.reason == reason
    assert crawler.submissions == 0
    assert action.clicks == (1 if case == "exhausted" else 0)


def test_reason_does_not_change_outcome_equality():
    from dataclasses import replace
    outcome = checkpoints.CheckpointOutcome(checkpoints.CheckpointKind.UNKNOWN_BLOCKER, "rule", "label", "interaction")
    assert outcome == replace(outcome, reason=checkpoints.CheckpointReason.MATCHED_BLOCKER)


def test_origin_parser_exception_is_distinct_without_second_read(monkeypatch, tmp_path):
    monkeypatch.setattr(base, "DATA_ROOT", tmp_path)
    crawler = _StagedCrawler(name="ctbc")
    monkeypatch.setattr(crawler, "_credential_origin_allowed", base.BankCrawler._credential_origin_allowed.__get__(crawler))
    calls = []
    def fail(url):
        calls.append(True)
        raise RuntimeError("PRIVATE")
    monkeypatch.setattr(base, "urlparse", fail)
    with pytest.raises(checkpoints.LoginCheckpointBlocked) as raised:
        crawler._shared_login(SimpleNamespace(url="https://example.com"))
    assert raised.value.outcome.reason == "origin_inspection_exception"
    assert calls == [True] and crawler.submissions == 0


def test_origin_exception_flag_does_not_survive_a_successful_inspection(monkeypatch, tmp_path):
    monkeypatch.setattr(base, "DATA_ROOT", tmp_path)
    crawler = _StagedCrawler(name="ctbc")
    crawler._origin_inspection_failed = True
    assert base.BankCrawler._credential_origin_allowed(crawler, SimpleNamespace(url="https://example.com"))
    assert crawler._origin_inspection_failed is False


def test_mutated_enum_values_are_never_rendered(monkeypatch):
    monkeypatch.setattr(checkpoints.CheckpointPhase.POST_SUBMIT, "_value_", Hostile())
    monkeypatch.setattr(checkpoints.CheckpointReason.MATCHED_BLOCKER, "_value_", Hostile())
    assert base._safe_login_diagnostics("ctbc", {"phase": checkpoints.CheckpointPhase.POST_SUBMIT},
        {"reason": checkpoints.CheckpointReason.MATCHED_BLOCKER, "rule_name": "ctbc-unknown-modal"}) == "phase=post_submit, reason=matched_blocker, rule=ctbc-unknown-modal"
