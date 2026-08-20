import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from patchright.sync_api import TimeoutError as PatchrightTimeoutError

from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointOutcome,
    CheckpointPhase,
    DEFAULT_ACTION_SELECTOR,
    LoginCheckpointRule,
    _action_selected,
    evaluate_login_checkpoint,
)


@dataclass
class Node:
    text: str = ""
    visible: bool = True
    attached: bool = True
    enabled: bool = True
    attrs: dict[str, str] = field(default_factory=dict)
    queries: dict[str, list["Node"]] = field(default_factory=dict)
    on_click: Callable[["Node"], None] | None = None
    wait_error: Exception | None = None
    clicks: int = 0


class Locator:
    def __init__(self, nodes: list[Node]):
        self.nodes = nodes

    def locator(self, selector: str):
        return Locator([child for node in self.nodes for child in node.queries.get(selector, [])])

    def count(self):
        return len(self.nodes)

    def nth(self, index: int):
        return Locator([self.nodes[index]])

    def is_visible(self):
        return bool(self.nodes and self.nodes[0].attached and self.nodes[0].visible)

    def inner_text(self):
        return self.nodes[0].text

    def is_enabled(self):
        return self.nodes[0].enabled

    def get_attribute(self, name: str):
        return self.nodes[0].attrs.get(name)

    def click(self):
        node = self.nodes[0]
        node.clicks += 1
        if node.on_click:
            node.on_click(node)

    def wait_for(self, *, state: str, timeout: int):
        assert state == "hidden"
        assert timeout > 0
        if self.nodes[0].wait_error:
            raise self.nodes[0].wait_error
        if self.is_visible():
            raise TimeoutError


class Page:
    def __init__(self, queries: dict[str, list[Node]] | None = None, frames=()):
        self.root = Node(queries=queries or {})
        self.frames = [self, *frames]
        self.main_frame = self

    def locator(self, selector: str):
        return self.root.queries.get(selector) and Locator(self.root.queries[selector]) or Locator([])


def test_foreign_rule_is_rejected_before_authenticated_or_browser_inspection():
    class Page:
        @property
        def frames(self):
            raise AssertionError("browser must not be inspected")

    auth_calls = 0

    def authenticated(page):
        nonlocal auth_calls
        auth_calls += 1
        return True

    foreign_rule = LoginCheckpointRule(
        name="foreign-bank-status",
        bank="foreign-bank",
        phases=(CheckpointPhase.PRE_SUBMIT,),
        kind=CheckpointKind.UNKNOWN_BLOCKER,
        container_selector="#status",
    )
    outcome = evaluate_login_checkpoint(
        Page(),
        bank="test-bank",
        phase=CheckpointPhase.PRE_SUBMIT,
        rules=(foreign_rule,),
        is_authenticated=authenticated,
    )

    assert outcome == CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER)
    assert auth_calls == 0


def test_foreign_bank_rule_never_inspects_or_clicks_colliding_dom():
    action = Node(text="Continue")
    container = Node(
        text="Important security notice",
        queries={"button, a, [role=button]": [action]},
    )
    action.on_click = lambda _: setattr(container, "visible", False)
    rule = LoginCheckpointRule(
        name="foreign-bank-security-notice",
        bank="foreign-bank",
        phases=(CheckpointPhase.POST_SUBMIT,),
        kind=CheckpointKind.DISMISSIBLE_NOTICE,
        container_selector="#notice",
        action_texts=("Continue",),
        required_body_pattern=re.compile(r"security notice"),
    )

    outcome = evaluate_login_checkpoint(
        Page({"#notice": [container]}),
        bank="test-bank",
        phase=CheckpointPhase.POST_SUBMIT,
        rules=(rule,),
        is_authenticated=lambda page: False,
    )

    assert outcome == CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER)
    assert action.clicks == 0


def test_exact_container_body_marker_and_unique_action_clicks_once():
    action = Node(text=" Continue ")
    container = Node(
        text="Important security notice",
        queries={"button, a, [role=button]": [action]},
    )
    action.on_click = lambda _: setattr(container, "visible", False)
    rule = LoginCheckpointRule(
        name="test-bank-security-notice",
        bank="test-bank",
        phases=(CheckpointPhase.POST_SUBMIT,),
        kind=CheckpointKind.DISMISSIBLE_NOTICE,
        container_selector="#notice",
        action_texts=("Continue",),
        required_body_pattern=re.compile(r"security notice"),
    )

    outcome = evaluate_login_checkpoint(
        Page({"#notice": [container]}),
        bank="test-bank",
        phase=CheckpointPhase.POST_SUBMIT,
        rules=(rule,),
        is_authenticated=lambda page: False,
    )

    assert outcome.kind is CheckpointKind.DISMISSIBLE_NOTICE
    assert outcome.rule_name == "test-bank-security-notice"
    assert outcome.action_label == "Continue"
    assert action.clicks == 1


def test_required_body_pattern_mismatch_does_not_click_or_match_rule():
    action = Node(text="Continue")
    container = Node(
        text="Different notice",
        queries={"button, a, [role=button]": [action]},
    )
    rule = LoginCheckpointRule(
        name="test-bank-security-notice",
        bank="test-bank",
        phases=(CheckpointPhase.POST_SUBMIT,),
        kind=CheckpointKind.DISMISSIBLE_NOTICE,
        container_selector="#notice",
        action_texts=("Continue",),
        required_body_pattern=re.compile(r"security notice"),
    )

    outcome = evaluate_login_checkpoint(
        Page({"#notice": [container]}),
        bank="test-bank",
        phase=CheckpointPhase.POST_SUBMIT,
        rules=(rule,),
        is_authenticated=lambda page: False,
    )

    assert outcome == CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER)
    assert action.clicks == 0


def test_hidden_outside_and_nested_duplicate_controls_are_ignored():
    action = Node(text="Continue")
    hidden = Node(text="Continue", visible=False)
    outside = Node(text="Continue")
    inner = Node(
        text="security notice",
        queries={"button, a, [role=button]": [hidden, action]},
    )
    outer = Node(
        text="security notice",
        queries={
            "#notice": [inner],
            "button, a, [role=button]": [action],
        },
    )
    action.on_click = lambda _: setattr(inner, "visible", False)
    rule = LoginCheckpointRule(
        name="test-bank-notice",
        bank="test-bank",
        phases=(CheckpointPhase.POST_SUBMIT,),
        kind=CheckpointKind.DISMISSIBLE_NOTICE,
        container_selector="#notice",
        action_texts=("Continue",),
        required_body_pattern=re.compile("security"),
    )

    outcome = evaluate_login_checkpoint(
        Page({"#notice": [outer, inner], "button, a, [role=button]": [outside]}),
        bank="test-bank",
        phase=CheckpointPhase.POST_SUBMIT,
        rules=(rule,),
        is_authenticated=lambda page: False,
    )

    assert outcome.kind is CheckpointKind.DISMISSIBLE_NOTICE
    assert action.clicks == 1
    assert hidden.clicks == outside.clicks == 0


def test_form_bearing_notice_is_never_dismissed():
    action = Node(text="Continue")
    container = Node(
        text="security notice",
        queries={
            "button, a, [role=button]": [action],
            "input, select, textarea, [contenteditable]:not([contenteditable='false'])": [Node()],
        },
    )
    action.on_click = lambda _: setattr(container, "visible", False)
    rule = LoginCheckpointRule(
        name="test-bank-notice",
        bank="test-bank",
        phases=(CheckpointPhase.POST_SUBMIT,),
        kind=CheckpointKind.DISMISSIBLE_NOTICE,
        container_selector="#notice",
        action_texts=("Continue",),
    )

    outcome = evaluate_login_checkpoint(
        Page({"#notice": [container]}),
        bank="test-bank",
        phase=CheckpointPhase.POST_SUBMIT,
        rules=(rule,),
        is_authenticated=lambda page: False,
    )

    assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
    assert outcome.rule_name == "test-bank-notice"
    assert action.clicks == 0


@pytest.mark.parametrize(
    "values",
    [
        {"name": ""},
        {"bank": ""},
        {"container_selector": ""},
        {"phases": ()},
        {"max_actions": 0},
    ],
)
def test_rule_rejects_invalid_minimum_fields(values):
    defaults = {
        "name": "test-bank-rule",
        "bank": "test-bank",
        "phases": (CheckpointPhase.POST_SUBMIT,),
        "kind": CheckpointKind.DISMISSIBLE_NOTICE,
        "container_selector": "#notice",
        "action_texts": ("Continue",),
    }

    with pytest.raises(ValueError):
        LoginCheckpointRule(**(defaults | values))


@pytest.mark.parametrize(
    "container_selector",
    [
        "body",
        "html",
        "main",
        "form",
        "div",
        "button",
        "a",
        "*",
        "body .modal",
        "div>section",
        "div+section",
        "div~section",
        ":root",
        "#notice, .modal",
        "[disabled]",
        "[role~=button]",
        "[role^=button]",
        "[role$=button]",
        "[role*=button]",
        "[role|=button]",
        "[role=button i]",
    ],
)
def test_container_selector_requires_one_simple_scoped_compound(container_selector):
    with pytest.raises(ValueError):
        LoginCheckpointRule(
            name="test-bank-unsafe-container",
            bank="test-bank",
            phases=(CheckpointPhase.POST_SUBMIT,),
            kind=CheckpointKind.DUPLICATE_SESSION,
            container_selector=container_selector,
            action_texts=("Continue",),
        )


@pytest.mark.parametrize(
    "container_selector",
    [
        "#notice",
        ".modal",
        "div.modal.show",
        "[data-modal=duplicate]",
        "[data-modal='duplicate']",
        '[data-modal="duplicate"]',
        "button.next",
        "#continue",
        "[data-action='continue']",
        "button[data-action='continue']",
    ],
)
def test_simple_scoped_container_selector_is_accepted(container_selector):
    rule = LoginCheckpointRule(
        name="test-bank-safe-container",
        bank="test-bank",
        phases=(CheckpointPhase.POST_SUBMIT,),
        kind=CheckpointKind.DUPLICATE_SESSION,
        container_selector=container_selector,
        action_texts=("Continue",),
    )

    assert rule.container_selector == container_selector


@pytest.mark.parametrize(
    "container_selector",
    [" #dialog", "#dialog ", "\t#dialog", "#dialog\t", "\n#dialog", "#dialog\n"],
)
def test_container_selector_rejects_surrounding_whitespace(container_selector):
    with pytest.raises(ValueError):
        LoginCheckpointRule(
            name="test-bank-padded-container",
            bank="test-bank",
            phases=(CheckpointPhase.POST_SUBMIT,),
            kind=CheckpointKind.DUPLICATE_SESSION,
            container_selector=container_selector,
            action_texts=("Continue",),
        )


@pytest.mark.parametrize(
    "action_texts",
    [(), ("",), ("   ",), ("Continue", "\n")],
)
def test_broad_action_selector_requires_only_nonblank_exact_texts(action_texts):
    with pytest.raises(ValueError):
        LoginCheckpointRule(
            name="test-bank-unsafe-action",
            bank="test-bank",
            phases=(CheckpointPhase.POST_SUBMIT,),
            kind=CheckpointKind.DUPLICATE_SESSION,
            container_selector="#dialog",
            action_texts=action_texts,
        )


@pytest.mark.parametrize(
    "action_selector",
    ["", "*", "button", "a", "[role=button]", "button.primary, a.next"],
)
def test_structural_action_selector_without_text_must_be_narrow(action_selector):
    with pytest.raises(ValueError):
        LoginCheckpointRule(
            name="test-bank-unsafe-structural-action",
            bank="test-bank",
            phases=(CheckpointPhase.POST_SUBMIT,),
            kind=CheckpointKind.DUPLICATE_SESSION,
            container_selector="#dialog",
            action_selector=action_selector,
        )


@pytest.mark.parametrize(
    "action_selector",
    [
        "[role=button i]",
        "[role~=button]",
        "button:not([disabled])",
        "[disabled]",
        "div button.next",
    ],
)
def test_structural_action_selector_cannot_bypass_simple_scoped_grammar(action_selector):
    with pytest.raises(ValueError):
        LoginCheckpointRule(
            name="test-bank-action-selector-bypass",
            bank="test-bank",
            phases=(CheckpointPhase.POST_SUBMIT,),
            kind=CheckpointKind.DUPLICATE_SESSION,
            container_selector="#dialog",
            action_selector=action_selector,
        )


def test_generic_role_action_is_allowed_only_inside_fixed_default_selector():
    with pytest.raises(ValueError):
        LoginCheckpointRule(
            name="test-bank-generic-role-action",
            bank="test-bank",
            phases=(CheckpointPhase.POST_SUBMIT,),
            kind=CheckpointKind.DUPLICATE_SESSION,
            container_selector="#dialog",
            action_selector="[role=button]",
            action_texts=("Continue",),
        )


@pytest.mark.parametrize(
    "action_selector",
    ["button.next", "#continue", "[data-action='continue']", "button[data-action='continue']"],
)
def test_narrow_structural_action_selector_without_text_is_accepted(action_selector):
    rule = LoginCheckpointRule(
        name="test-bank-safe-structural-action",
        bank="test-bank",
        phases=(CheckpointPhase.POST_SUBMIT,),
        kind=CheckpointKind.DUPLICATE_SESSION,
        container_selector="#dialog",
        action_selector=action_selector,
    )

    assert rule.action_selector == action_selector


@pytest.mark.parametrize(
    "action_selector",
    [" button.next", "button.next ", "\tbutton.next", "button.next\n"],
)
def test_custom_action_selector_rejects_surrounding_whitespace(action_selector):
    with pytest.raises(ValueError):
        LoginCheckpointRule(
            name="test-bank-padded-action",
            bank="test-bank",
            phases=(CheckpointPhase.POST_SUBMIT,),
            kind=CheckpointKind.DUPLICATE_SESSION,
            container_selector="#dialog",
            action_selector=action_selector,
        )


@pytest.mark.parametrize(
    ("kind", "action_texts"),
    [
        (CheckpointKind.DUPLICATE_SESSION, ("Continue",)),
        (CheckpointKind.CAPTCHA_RETRY, ()),
    ],
)
@pytest.mark.parametrize(
    "action_selector",
    [f" {DEFAULT_ACTION_SELECTOR}", f"{DEFAULT_ACTION_SELECTOR} "],
)
def test_default_action_selector_rejects_surrounding_whitespace(
    kind, action_texts, action_selector
):
    with pytest.raises(ValueError):
        LoginCheckpointRule(
            name="test-bank-padded-default-action",
            bank="test-bank",
            phases=(CheckpointPhase.POST_SUBMIT,),
            kind=kind,
            container_selector="#dialog",
            action_selector=action_selector,
            action_texts=action_texts,
        )


def evaluate_actions(action_labels, action_texts):
    actions = [Node(text=label) for label in action_labels]
    container = Node(queries={"button, a, [role=button]": actions})
    for action in actions:
        action.on_click = lambda _, owner=container: setattr(owner, "visible", False)
    rule = LoginCheckpointRule(
        name="test-bank-action",
        bank="test-bank",
        phases=(CheckpointPhase.POST_SUBMIT,),
        kind=CheckpointKind.DUPLICATE_SESSION,
        container_selector="#dialog",
        action_texts=action_texts,
    )
    outcome = evaluate_login_checkpoint(
        Page({"#dialog": [container]}),
        bank="test-bank",
        phase=CheckpointPhase.POST_SUBMIT,
        rules=(rule,),
        is_authenticated=lambda page: False,
    )
    return outcome, actions


def test_two_exact_actions_are_ambiguous_and_not_clicked():
    outcome, actions = evaluate_actions(["Confirm", "Confirm"], ("Confirm",))

    assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
    assert sum(action.clicks for action in actions) == 0


def test_substring_action_text_is_not_eligible():
    outcome, actions = evaluate_actions(["Confirm details"], ("Confirm",))

    assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
    assert actions[0].clicks == 0


def test_actions_across_two_visible_matching_containers_are_ambiguous():
    first_action = Node(text="Continue")
    second_action = Node(text="Continue")
    first = Node(queries={"button, a, [role=button]": [first_action]})
    second = Node(queries={"button, a, [role=button]": [second_action]})
    first_action.on_click = lambda _: setattr(first, "visible", False)
    second_action.on_click = lambda _: setattr(second, "visible", False)
    rule = LoginCheckpointRule(
        name="test-bank-duplicate-dialogs",
        bank="test-bank",
        phases=(CheckpointPhase.POST_SUBMIT,),
        kind=CheckpointKind.DUPLICATE_SESSION,
        container_selector="#dialog",
        action_texts=("Continue",),
    )

    outcome = evaluate_login_checkpoint(
        Page({"#dialog": [first, second]}),
        bank="test-bank",
        phase=CheckpointPhase.POST_SUBMIT,
        rules=(rule,),
        is_authenticated=lambda page: False,
    )

    assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
    assert first_action.clicks == second_action.clicks == 0


def test_action_and_rule_texts_use_normalized_full_equality():
    outcome, actions = evaluate_actions(["  Confirm\n"], (" Confirm ",))

    assert outcome.kind is CheckpointKind.DUPLICATE_SESSION
    assert outcome.action_label == "Confirm"
    assert actions[0].clicks == 1


@pytest.mark.parametrize(
    "progress", ["container_detached", "action_hidden", "disabled", "selected", "fingerprint"]
)
def test_click_requires_bounded_safe_progress(progress):
    action = Node(text="Continue")
    container = Node(text="private modal body", queries={"button, a, [role=button]": [action]})

    def mutate(_):
        if progress == "container_detached":
            container.attached = False
        elif progress == "action_hidden":
            action.visible = False
        elif progress == "disabled":
            action.enabled = False
        elif progress == "selected":
            action.attrs["aria-selected"] = "true"
        else:
            container.text = "changed private modal body"

    action.on_click = mutate
    rule = LoginCheckpointRule(
        name="test-bank-progress",
        bank="test-bank",
        phases=(CheckpointPhase.POST_SUBMIT,),
        kind=CheckpointKind.DUPLICATE_SESSION,
        container_selector="#dialog",
        action_texts=("Continue",),
    )

    outcome = evaluate_login_checkpoint(
        Page({"#dialog": [container]}),
        bank="test-bank",
        phase=CheckpointPhase.POST_SUBMIT,
        rules=(rule,),
        is_authenticated=lambda page: False,
    )

    assert outcome.kind is CheckpointKind.DUPLICATE_SESSION
    assert action.clicks == 1


def test_patchright_timeout_falls_back_to_hidden_action_progress():
    action = Node(text="Continue")
    container = Node(
        queries={"button, a, [role=button]": [action]},
        wait_error=PatchrightTimeoutError("bounded wait elapsed"),
    )
    action.on_click = lambda _: setattr(action, "visible", False)
    rule = LoginCheckpointRule(
        name="test-bank-patchright-timeout",
        bank="test-bank",
        phases=(CheckpointPhase.POST_SUBMIT,),
        kind=CheckpointKind.DUPLICATE_SESSION,
        container_selector="#dialog",
        action_texts=("Continue",),
    )

    outcome = evaluate_login_checkpoint(
        Page({"#dialog": [container]}),
        bank="test-bank",
        phase=CheckpointPhase.POST_SUBMIT,
        rules=(rule,),
        is_authenticated=lambda page: False,
    )

    assert outcome.kind is CheckpointKind.DUPLICATE_SESSION
    assert action.clicks == 1


def test_real_patchright_locator_timeout_uses_alternate_progress():
    from patchright.sync_api import sync_playwright

    with sync_playwright() as patchright:
        if not Path(patchright.chromium.executable_path).exists():
            pytest.skip("Patchright browser binary is not installed")
        browser = patchright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(
                """
                <div id="checkpoint">
                  <p>Duplicate session</p>
                  <button id="continue">Continue</button>
                </div>
                <script>
                  document.querySelector('#continue').onclick = event => {
                    event.currentTarget.disabled = true;
                    event.currentTarget.hidden = true;
                  };
                </script>
                """
            )
            rule = LoginCheckpointRule(
                name="ctbc-duplicate-session",
                bank="ctbc",
                phases=(CheckpointPhase.POST_SUBMIT,),
                kind=CheckpointKind.DUPLICATE_SESSION,
                container_selector="#checkpoint",
                action_texts=("Continue",),
            )

            outcome = evaluate_login_checkpoint(
                page,
                bank="ctbc",
                phase=CheckpointPhase.POST_SUBMIT,
                rules=(rule,),
                is_authenticated=lambda page: False,
            )

            assert outcome.kind is CheckpointKind.DUPLICATE_SESSION
            assert page.locator("#checkpoint").is_visible()
            assert page.locator("#continue").is_hidden()
        finally:
            browser.close()


def test_real_patchright_rejects_global_body_confirmation_before_evaluation():
    from patchright.sync_api import sync_playwright

    with sync_playwright() as patchright:
        if not Path(patchright.chromium.executable_path).exists():
            pytest.skip("Patchright browser binary is not installed")
        browser = patchright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(
                """
                <button onclick="this.dataset.clicked = 'yes'">確認</button>
                """
            )

            with pytest.raises(ValueError):
                LoginCheckpointRule(
                    name="ctbc-global-confirmation",
                    bank="ctbc",
                    phases=(CheckpointPhase.POST_SUBMIT,),
                    kind=CheckpointKind.DUPLICATE_SESSION,
                    container_selector="body",
                    action_texts=("確認",),
                )

            assert page.locator("button").get_attribute("data-clicked") is None
        finally:
            browser.close()


@pytest.mark.parametrize("attribute", ["checked", "selected"])
def test_boolean_selection_attributes_are_selected_by_presence(attribute):
    assert _action_selected(Locator([Node(attrs={attribute: ""})])) is True


def test_progress_fingerprint_never_compares_raw_body_text():
    secret_body = "private account body 987654"

    class SensitiveText(str):
        def __eq__(self, other):
            raise RuntimeError(secret_body)

        def __ne__(self, other):
            raise RuntimeError(secret_body)

    action = Node(text="Continue")
    container = Node(
        text=SensitiveText(secret_body),
        queries={"button, a, [role=button]": [action]},
    )
    action.on_click = lambda _: setattr(container, "text", SensitiveText("changed body"))
    rule = LoginCheckpointRule(
        name="test-bank-safe-fingerprint",
        bank="test-bank",
        phases=(CheckpointPhase.POST_SUBMIT,),
        kind=CheckpointKind.DUPLICATE_SESSION,
        container_selector="#dialog",
        action_texts=("Continue",),
    )

    outcome = evaluate_login_checkpoint(
        Page({"#dialog": [container]}),
        bank="test-bank",
        phase=CheckpointPhase.POST_SUBMIT,
        rules=(rule,),
        is_authenticated=lambda page: False,
    )

    assert outcome.kind is CheckpointKind.DUPLICATE_SESSION
    assert secret_body not in repr(outcome)


def test_progress_inspection_error_fails_closed_even_if_action_hides(caplog):
    secret_body = "private progress body 987654"
    action = Node(text="Continue")
    container = Node(
        queries={"button, a, [role=button]": [action]},
        wait_error=RuntimeError(secret_body),
    )
    action.on_click = lambda _: setattr(action, "visible", False)
    rule = LoginCheckpointRule(
        name="test-bank-progress-error",
        bank="test-bank",
        phases=(CheckpointPhase.POST_SUBMIT,),
        kind=CheckpointKind.DUPLICATE_SESSION,
        container_selector="#dialog",
        action_texts=("Continue",),
    )

    outcome = evaluate_login_checkpoint(
        Page({"#dialog": [container]}),
        bank="test-bank",
        phase=CheckpointPhase.POST_SUBMIT,
        rules=(rule,),
        is_authenticated=lambda page: False,
    )

    assert outcome == CheckpointOutcome(
        CheckpointKind.UNKNOWN_BLOCKER,
        rule_name="test-bank-progress-error",
    )
    assert secret_body not in repr(outcome)
    assert secret_body not in caplog.text


def test_click_without_progress_returns_unknown_after_exactly_one_click():
    action = Node(text="Continue")
    container = Node(queries={"button, a, [role=button]": [action]})
    rule = LoginCheckpointRule(
        name="test-bank-no-progress",
        bank="test-bank",
        phases=(CheckpointPhase.POST_SUBMIT,),
        kind=CheckpointKind.DUPLICATE_SESSION,
        container_selector="#dialog",
        action_texts=("Continue",),
    )
    outcome = evaluate_login_checkpoint(
        Page({"#dialog": [container]}),
        bank="test-bank",
        phase=CheckpointPhase.POST_SUBMIT,
        rules=(rule,),
        is_authenticated=lambda page: False,
    )

    assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
    assert action.clicks == 1


@pytest.mark.parametrize(
    ("kind", "interaction"),
    [
        (CheckpointKind.CAPTCHA_RETRY, None),
        (CheckpointKind.STARTUP_RECOVERY, None),
        (CheckpointKind.OTP_REQUIRED, "otp"),
        (CheckpointKind.PASSWORD_CHANGE_REQUIRED, "password_change"),
        (CheckpointKind.EXPLICIT_LOGIN_ERROR, None),
        (CheckpointKind.UNKNOWN_BLOCKER, None),
    ],
)
def test_classifier_rules_never_click(kind, interaction):
    action = Node(text="Continue")
    container = Node(
        queries={
            "button, a, [role=button]": [action],
            "input, select, textarea, [contenteditable]:not([contenteditable='false'])": [Node()],
        }
    )
    action.on_click = lambda _: setattr(container, "visible", False)
    rule = LoginCheckpointRule(
        name="test-bank-terminal",
        bank="test-bank",
        phases=(CheckpointPhase.POST_SUBMIT,),
        kind=kind,
        container_selector="#terminal",
    )

    outcome = evaluate_login_checkpoint(
        Page({"#terminal": [container]}),
        bank="test-bank",
        phase=CheckpointPhase.POST_SUBMIT,
        rules=(rule,),
        is_authenticated=lambda page: False,
    )

    assert outcome == CheckpointOutcome(
        kind,
        rule_name="test-bank-terminal",
        interaction=interaction,
    )
    assert action.clicks == 0


@pytest.mark.parametrize(
    "kind",
    [CheckpointKind.AUTHENTICATED, CheckpointKind.READY_FOR_CREDENTIALS],
)
def test_rule_rejects_evaluator_only_kinds(kind):
    with pytest.raises(ValueError):
        LoginCheckpointRule(
            name="test-bank-invalid-kind",
            bank="test-bank",
            phases=(CheckpointPhase.POST_SUBMIT,),
            kind=kind,
            container_selector="#status",
        )


@pytest.mark.parametrize(
    "configured_action",
    [
        {"action_texts": ("Retry",)},
        {"action_selector": "button.retry"},
    ],
)
def test_classifier_rule_rejects_action_configuration(configured_action):
    with pytest.raises(ValueError):
        LoginCheckpointRule(
            name="test-bank-misleading-classifier",
            bank="test-bank",
            phases=(CheckpointPhase.POST_SUBMIT,),
            kind=CheckpointKind.CAPTCHA_RETRY,
            container_selector="#captcha",
            **configured_action,
        )


def test_inspection_error_fails_closed_without_leaking_secret(caplog):
    secret_body = "private account body 987654"

    class BrokenPage(Page):
        def locator(self, selector: str):
            raise RuntimeError(secret_body)

    rule = LoginCheckpointRule(
        name="test-bank-safe-inspection",
        bank="test-bank",
        phases=(CheckpointPhase.POST_SUBMIT,),
        kind=CheckpointKind.DUPLICATE_SESSION,
        container_selector="#dialog",
        action_texts=("Continue",),
    )

    outcome = evaluate_login_checkpoint(
        BrokenPage(),
        bank="test-bank",
        phase=CheckpointPhase.POST_SUBMIT,
        rules=(rule,),
        is_authenticated=lambda page: False,
    )

    assert outcome == CheckpointOutcome(
        CheckpointKind.UNKNOWN_BLOCKER,
        rule_name="test-bank-safe-inspection",
    )
    assert secret_body not in repr(outcome)
    assert secret_body not in caplog.text


def test_authentication_error_fails_closed_without_leaking_secret(caplog):
    secret_body = "private authenticated body 987654"

    def fail_authentication(page):
        raise RuntimeError(secret_body)

    outcome = evaluate_login_checkpoint(
        Page(),
        bank="test-bank",
        phase=CheckpointPhase.POST_SUBMIT,
        rules=(),
        is_authenticated=fail_authentication,
    )

    assert outcome == CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER)
    assert secret_body not in repr(outcome)
    assert secret_body not in caplog.text


def test_body_text_never_escapes_outcome_when_click_fails():
    secret_body = "private account body 987654"
    action = Node(text="Continue")
    container = Node(text=secret_body, queries={"button, a, [role=button]": [action]})

    def fail(_):
        raise RuntimeError(secret_body)

    action.on_click = fail
    rule = LoginCheckpointRule(
        name="test-bank-safe-error",
        bank="test-bank",
        phases=(CheckpointPhase.POST_SUBMIT,),
        kind=CheckpointKind.DUPLICATE_SESSION,
        container_selector="#dialog",
        action_texts=("Continue",),
        required_body_pattern=re.compile("private account"),
    )

    outcome = evaluate_login_checkpoint(
        Page({"#dialog": [container]}),
        bank="test-bank",
        phase=CheckpointPhase.POST_SUBMIT,
        rules=(rule,),
        is_authenticated=lambda page: False,
    )

    assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
    assert secret_body not in repr(outcome)
    assert action.clicks == 1


def test_rule_matches_inside_child_frame():
    action = Node(text="Continue")
    container = Node(queries={"button, a, [role=button]": [action]})
    action.on_click = lambda _: setattr(container, "visible", False)
    child = Page({"#child-dialog": [container]})
    rule = LoginCheckpointRule(
        name="test-bank-child-frame",
        bank="test-bank",
        phases=(CheckpointPhase.POST_SUBMIT,),
        kind=CheckpointKind.DUPLICATE_SESSION,
        container_selector="#child-dialog",
        action_texts=("Continue",),
    )

    outcome = evaluate_login_checkpoint(
        Page(frames=(child,)),
        bank="test-bank",
        phase=CheckpointPhase.POST_SUBMIT,
        rules=(rule,),
        is_authenticated=lambda page: False,
        is_scope_owned=lambda frame: frame is child,
    )

    assert outcome.kind is CheckpointKind.DUPLICATE_SESSION
    assert action.clicks == 1


def test_rule_never_acts_inside_unowned_child_frame():
    action = Node(text="Continue")
    container = Node(queries={"button, a, [role=button]": [action]})
    action.on_click = lambda _: setattr(container, "visible", False)
    child = Page({"#child-dialog": [container]})
    rule = LoginCheckpointRule(
        name="test-bank-child-frame",
        bank="test-bank",
        phases=(CheckpointPhase.POST_SUBMIT,),
        kind=CheckpointKind.DUPLICATE_SESSION,
        container_selector="#child-dialog",
        action_texts=("Continue",),
    )

    outcome = evaluate_login_checkpoint(
        Page(frames=(child,)),
        bank="test-bank",
        phase=CheckpointPhase.POST_SUBMIT,
        rules=(rule,),
        is_authenticated=lambda page: False,
        is_scope_owned=lambda _frame: False,
    )

    assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
    assert action.clicks == 0


def test_bank_rule_must_explicitly_list_generic_confirmation_label():
    explicit_action = Node(text="確認")
    explicit_container = Node(queries={"button, a, [role=button]": [explicit_action]})
    explicit_action.on_click = lambda _: setattr(explicit_container, "visible", False)
    explicit_rule = LoginCheckpointRule(
        name="test-bank-confirm",
        bank="test-bank",
        phases=(CheckpointPhase.POST_SUBMIT,),
        kind=CheckpointKind.DUPLICATE_SESSION,
        container_selector="#bank-dialog",
        action_texts=("確認",),
    )

    outcome = evaluate_login_checkpoint(
        Page({"#bank-dialog": [explicit_container]}),
        bank="test-bank",
        phase=CheckpointPhase.POST_SUBMIT,
        rules=(explicit_rule,),
        is_authenticated=lambda page: False,
    )

    assert outcome.action_label == "確認"
    assert explicit_action.clicks == 1

    global_action = Node(text="確認")
    outcome = evaluate_login_checkpoint(
        Page({"button, a, [role=button]": [global_action]}),
        bank="test-bank",
        phase=CheckpointPhase.POST_SUBMIT,
        rules=(),
        is_authenticated=lambda page: False,
    )

    assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
    assert global_action.clicks == 0


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        (CheckpointPhase.PRE_SUBMIT, CheckpointKind.READY_FOR_CREDENTIALS),
        (CheckpointPhase.POST_SUBMIT, CheckpointKind.UNKNOWN_BLOCKER),
        (CheckpointPhase.POST_SUBMIT_SETTLE, CheckpointKind.UNKNOWN_BLOCKER),
    ],
)
def test_phase_fallbacks(phase, expected):
    outcome = evaluate_login_checkpoint(
        Page(),
        bank="test-bank",
        phase=phase,
        rules=(),
        is_authenticated=lambda page: False,
    )

    assert outcome == CheckpointOutcome(expected)


def test_only_rules_for_current_phase_are_evaluated():
    action = Node(text="Continue")
    container = Node(queries={"button, a, [role=button]": [action]})
    action.on_click = lambda _: setattr(container, "visible", False)
    rule = LoginCheckpointRule(
        name="test-bank-pre-only",
        bank="test-bank",
        phases=(CheckpointPhase.PRE_SUBMIT,),
        kind=CheckpointKind.DISMISSIBLE_NOTICE,
        container_selector="#pre-dialog",
        action_texts=("Continue",),
    )

    outcome = evaluate_login_checkpoint(
        Page({"#pre-dialog": [container]}),
        bank="test-bank",
        phase=CheckpointPhase.POST_SUBMIT,
        rules=(rule,),
        is_authenticated=lambda page: False,
    )

    assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
    assert action.clicks == 0


def test_rules_are_evaluated_in_declared_order():
    first_action = Node(text="First")
    second_action = Node(text="Second")
    first_container = Node(queries={"button, a, [role=button]": [first_action]})
    second_container = Node(queries={"button, a, [role=button]": [second_action]})
    first_action.on_click = lambda _: setattr(first_container, "visible", False)
    second_action.on_click = lambda _: setattr(second_container, "visible", False)
    rules = (
        LoginCheckpointRule(
            "test-bank-first",
            "test-bank",
            (CheckpointPhase.POST_SUBMIT,),
            CheckpointKind.DISMISSIBLE_NOTICE,
            "#first",
            action_texts=("First",),
        ),
        LoginCheckpointRule(
            "test-bank-second",
            "test-bank",
            (CheckpointPhase.POST_SUBMIT,),
            CheckpointKind.DUPLICATE_SESSION,
            "#second",
            action_texts=("Second",),
        ),
    )

    outcome = evaluate_login_checkpoint(
        Page({"#first": [first_container], "#second": [second_container]}),
        bank="test-bank",
        phase=CheckpointPhase.POST_SUBMIT,
        rules=rules,
        is_authenticated=lambda page: False,
    )

    assert outcome.rule_name == "test-bank-first"
    assert first_action.clicks == 1
    assert second_action.clicks == 0


def test_structural_selector_without_text_never_returns_private_dom_label():
    secret_label = "Ken Kuang account 987654"

    class SensitiveActionText(str):
        def split(self, *args, **kwargs):
            raise AssertionError(secret_label)

    action = Node(text=SensitiveActionText(secret_label))
    container = Node(queries={"button.next": [action]})
    action.on_click = lambda _: setattr(container, "visible", False)
    rule = LoginCheckpointRule(
        name="test-bank-structural",
        bank="test-bank",
        phases=(CheckpointPhase.POST_SUBMIT,),
        kind=CheckpointKind.PASSWORD_CHANGE_OPTIONAL,
        container_selector="#password-change",
        action_selector="button.next",
    )

    outcome = evaluate_login_checkpoint(
        Page({"#password-change": [container]}),
        bank="test-bank",
        phase=CheckpointPhase.POST_SUBMIT,
        rules=(rule,),
        is_authenticated=lambda page: False,
    )

    assert outcome == CheckpointOutcome(
        CheckpointKind.PASSWORD_CHANGE_OPTIONAL,
        rule_name="test-bank-structural",
        interaction="password_change",
    )
    assert secret_label not in repr(outcome)
    assert action.clicks == 1
