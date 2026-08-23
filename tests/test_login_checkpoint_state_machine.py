from typing import cast

import pytest

from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointOutcome,
    CheckpointPhase,
    LoginBudget,
    LoginCheckpointBlocked,
    LoginCheckpointRule,
    LoginInteractionRequired,
    _evaluate_rule,
    _matching_body_fingerprint,
    bounded_locator_matches,
    evaluate_login_checkpoint,
    reduce_login_checkpoint,
    validate_login_checkpoint_outcome,
)


def _checkpoint_rule(name: str, kind: CheckpointKind) -> LoginCheckpointRule:
    return LoginCheckpointRule(
        name=name,
        bank="test-bank",
        phases=tuple(CheckpointPhase),
        kind=kind,
        container_selector=f"#{name}",
        action_texts=("Continue",) if kind is CheckpointKind.DUPLICATE_SESSION else (),
    )


def test_checkpoint_body_fingerprint_uses_operation_local_timeout() -> None:
    class Container:
        timeout: int | None = None

        def inner_text(self, *, timeout: int | None = None) -> str:
            self.timeout = timeout
            return "blocked"

    container = Container()

    assert _matching_body_fingerprint(container, None) is not None
    assert container.timeout == 5000


def test_checkpoint_evaluator_bounds_and_restores_page_timeout() -> None:
    class Page:
        timeout = 180000
        calls: list[int] = []
        frames: list[object] = []
        main_frame = object()

        def set_default_timeout(self, timeout: int) -> None:
            self.timeout = timeout
            self.calls.append(timeout)

    page = Page()

    def is_authenticated(current: Page) -> bool:
        assert current.timeout == 5000
        return False

    outcome = evaluate_login_checkpoint(
        page,
        bank="test-bank",
        phase=CheckpointPhase.PRE_SUBMIT,
        rules=(),
        is_authenticated=is_authenticated,
    )

    assert outcome == CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS)
    assert page.calls == [5000, 180000]
    assert page.timeout == 180000


def test_locator_snapshot_is_bounded_without_query_count() -> None:
    class Match:
        def __init__(self, exists: bool) -> None:
            self.exists = exists

        def element_handle(self, *, timeout: int):
            assert timeout == 100
            if not self.exists:
                raise TimeoutError("bounded snapshot elapsed")
            return object()

    class Locator:
        def count(self):
            raise AssertionError("queryCount must not be used")

        def nth(self, index: int) -> Match:
            return Match(index < 2)

    matches = list(bounded_locator_matches(Locator()))

    assert len(matches) == 2


def test_rule_can_raise_first_locator_timeout_without_using_query_count() -> None:
    timeouts: list[int] = []

    class Missing:
        def element_handle(self, *, timeout: int):
            timeouts.append(timeout)
            raise TimeoutError(f"missing after {timeout}")

    class MissingLocator:
        def nth(self, _index: int) -> Missing:
            return Missing()

    class Action:
        def __init__(self, container) -> None:
            self.container = container

        def element_handle(self, *, timeout: int):
            timeouts.append(timeout)
            return object()

        def is_visible(self) -> bool:
            return True

        def inner_text(self) -> str:
            return "Continue"

        def is_enabled(self) -> bool:
            return True

        def get_attribute(self, _name: str):
            return None

        def click(self) -> None:
            self.container.visible = False

    class Actions:
        def __init__(self, container) -> None:
            self.container = container

        def nth(self, index: int):
            return Action(self.container) if index == 0 else Missing()

    class Container:
        visible = True

        def element_handle(self, *, timeout: int):
            timeouts.append(timeout)
            if timeout < 5000:
                raise TimeoutError("cloud locator not ready")
            return object()

        def is_visible(self) -> bool:
            return self.visible

        def locator(self, selector: str):
            if selector == "button, a, [role=button]":
                return Actions(self)
            return MissingLocator()

        def inner_text(self, *, timeout: int) -> str:
            return "known notice"

        def wait_for(self, *, state: str, timeout: int) -> None:
            assert state == "hidden"
            assert timeout == 500
            assert not self.visible

    container = Container()

    class Containers:
        def nth(self, index: int):
            return container if index == 0 else Missing()

    class Scope:
        def locator(self, _selector: str) -> Containers:
            return Containers()

    rule = LoginCheckpointRule(
        name="slow-cloud-notice",
        bank="test-bank",
        phases=(CheckpointPhase.PRE_SUBMIT,),
        kind=CheckpointKind.DISMISSIBLE_NOTICE,
        container_selector="#notice",
        action_texts=("Continue",),
        first_match_timeout_ms=5000,
    )

    assert _evaluate_rule([Scope()], rule) == CheckpointOutcome(
        CheckpointKind.DISMISSIBLE_NOTICE,
        rule_name="slow-cloud-notice",
        action_label="Continue",
    )
    assert timeouts == [5000, 100, 100, 100, 5000, 100]


@pytest.mark.parametrize(
    "outcome",
    [
        CheckpointOutcome(CheckpointKind.AUTHENTICATED),
        CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS),
        CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER),
        CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, rule_name="otp"),
        CheckpointOutcome(CheckpointKind.OTP_REQUIRED, rule_name="otp"),
        CheckpointOutcome(CheckpointKind.DUPLICATE_SESSION, rule_name="duplicate"),
    ],
)
def test_login_checkpoint_outcome_provenance_accepts_valid_outcomes(
    outcome: CheckpointOutcome,
) -> None:
    rules = (
        _checkpoint_rule("otp", CheckpointKind.OTP_REQUIRED),
        _checkpoint_rule("duplicate", CheckpointKind.DUPLICATE_SESSION),
    )

    assert validate_login_checkpoint_outcome(outcome, rules) is outcome


@pytest.mark.parametrize(
    "outcome",
    [
        CheckpointOutcome(CheckpointKind.AUTHENTICATED, rule_name="otp"),
        CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS, rule_name="otp"),
        CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, rule_name="foreign"),
        CheckpointOutcome(CheckpointKind.OTP_REQUIRED),
        CheckpointOutcome(CheckpointKind.OTP_REQUIRED, rule_name="foreign"),
        CheckpointOutcome(CheckpointKind.DUPLICATE_SESSION, rule_name="otp"),
    ],
)
def test_login_checkpoint_outcome_provenance_replaces_invalid_outcomes(
    outcome: CheckpointOutcome,
) -> None:
    rules = (
        _checkpoint_rule("otp", CheckpointKind.OTP_REQUIRED),
        _checkpoint_rule("duplicate", CheckpointKind.DUPLICATE_SESSION),
    )

    validated = validate_login_checkpoint_outcome(outcome, rules)

    assert validated == CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER)
    assert validated is not outcome


def test_unknown_state_never_requests_resubmit():
    budget = LoginBudget(credential_submissions=1)

    with pytest.raises(LoginCheckpointBlocked) as raised:
        reduce_login_checkpoint(
            CheckpointPhase.POST_SUBMIT,
            budget,
            CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER),
        )

    assert raised.value.budget == budget
    assert raised.value.outcome.action_label is None
    assert raised.value.phase is CheckpointPhase.POST_SUBMIT
    assert "phase=post_submit" in str(raised.value)
    assert "rule_name" not in str(raised.value)


def test_otp_is_terminal_interaction_required_without_action():
    outcome = CheckpointOutcome(CheckpointKind.OTP_REQUIRED, interaction="otp")

    with pytest.raises(LoginInteractionRequired) as raised:
        reduce_login_checkpoint(CheckpointPhase.POST_SUBMIT, LoginBudget(credential_submissions=1), outcome)

    assert raised.value.outcome == outcome
    assert raised.value.outcome.action_label is None


@pytest.mark.parametrize(
    "kind",
    [
        CheckpointKind.DISMISSIBLE_NOTICE,
        CheckpointKind.DUPLICATE_SESSION,
        CheckpointKind.PASSWORD_CHANGE_OPTIONAL,
    ],
)
@pytest.mark.parametrize("phase", list(CheckpointPhase))
def test_nonterminal_actions_keep_current_phase(kind: CheckpointKind, phase: CheckpointPhase):
    budget = LoginBudget()

    assert reduce_login_checkpoint(phase, budget, CheckpointOutcome(kind, action_label="continue")) == (phase, budget)


def test_mandatory_password_change_is_terminal():
    outcome = CheckpointOutcome(
        CheckpointKind.PASSWORD_CHANGE_REQUIRED,
        interaction="password_change",
    )

    with pytest.raises(LoginInteractionRequired) as raised:
        reduce_login_checkpoint(CheckpointPhase.POST_SUBMIT, LoginBudget(credential_submissions=1), outcome)

    assert raised.value.outcome == outcome


def test_startup_recovery_increments_only_reload_budget():
    budget = LoginBudget(credential_submissions=1)

    phase, next_budget = reduce_login_checkpoint(
        CheckpointPhase.POST_SUBMIT,
        budget,
        CheckpointOutcome(CheckpointKind.STARTUP_RECOVERY, action_label="reload"),
    )

    assert phase is CheckpointPhase.PRE_SUBMIT
    assert next_budget == LoginBudget(credential_submissions=1, reloads=1)


def test_second_startup_reload_is_terminal():
    budget = LoginBudget(reloads=1)

    with pytest.raises(LoginCheckpointBlocked) as raised:
        reduce_login_checkpoint(
            CheckpointPhase.PRE_SUBMIT,
            budget,
            CheckpointOutcome(CheckpointKind.STARTUP_RECOVERY, action_label="reload"),
        )

    assert raised.value.budget == budget


def test_ready_for_credentials_consumes_one_submission():
    assert reduce_login_checkpoint(
        CheckpointPhase.PRE_SUBMIT,
        LoginBudget(),
        CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS),
    ) == (CheckpointPhase.POST_SUBMIT, LoginBudget(credential_submissions=1))


@pytest.mark.parametrize("phase", [CheckpointPhase.POST_SUBMIT, CheckpointPhase.POST_SUBMIT_SETTLE])
def test_ready_for_credentials_requires_pre_submit_phase(phase: CheckpointPhase):
    with pytest.raises(LoginCheckpointBlocked):
        reduce_login_checkpoint(phase, LoginBudget(), CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS))


def test_second_ordinary_submission_is_impossible():
    budget = LoginBudget(credential_submissions=1)

    with pytest.raises(LoginCheckpointBlocked):
        reduce_login_checkpoint(
            CheckpointPhase.PRE_SUBMIT,
            budget,
            CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS),
        )


def test_explicit_protocol_resubmit_consumes_only_protocol_budget():
    assert reduce_login_checkpoint(
        CheckpointPhase.POST_SUBMIT,
        LoginBudget(credential_submissions=1),
        CheckpointOutcome(CheckpointKind.PROTOCOL_RESUBMIT, rule_name="taishin-duplicate"),
    ) == (
        CheckpointPhase.POST_SUBMIT,
        LoginBudget(credential_submissions=2, protocol_resubmits=1, captcha_resubmits=0),
    )


def test_explicit_captcha_retry_consumes_only_captcha_budget():
    assert reduce_login_checkpoint(
        CheckpointPhase.POST_SUBMIT,
        LoginBudget(credential_submissions=1),
        CheckpointOutcome(CheckpointKind.CAPTCHA_RETRY, rule_name="sinopac-captcha-invalid"),
    ) == (
        CheckpointPhase.POST_SUBMIT,
        LoginBudget(credential_submissions=2, protocol_resubmits=0, captcha_resubmits=1),
    )


@pytest.mark.parametrize("kind", [CheckpointKind.PROTOCOL_RESUBMIT, CheckpointKind.CAPTCHA_RETRY])
@pytest.mark.parametrize("rule_name", [None, ""])
def test_retry_requires_nonempty_rule_name(kind: CheckpointKind, rule_name: str | None):
    with pytest.raises(LoginCheckpointBlocked):
        reduce_login_checkpoint(
            CheckpointPhase.POST_SUBMIT,
            LoginBudget(credential_submissions=1),
            CheckpointOutcome(kind, rule_name=rule_name),
        )


@pytest.mark.parametrize("kind", [CheckpointKind.PROTOCOL_RESUBMIT, CheckpointKind.CAPTCHA_RETRY])
def test_retry_requires_post_submit_phase(kind: CheckpointKind):
    with pytest.raises(LoginCheckpointBlocked):
        reduce_login_checkpoint(
            CheckpointPhase.PRE_SUBMIT,
            LoginBudget(credential_submissions=1),
            CheckpointOutcome(kind, rule_name="explicit-rule"),
        )


@pytest.mark.parametrize(
    ("budget", "kind"),
    [
        (LoginBudget(), CheckpointKind.PROTOCOL_RESUBMIT),
        (LoginBudget(), CheckpointKind.CAPTCHA_RETRY),
        (LoginBudget(credential_submissions=2, protocol_resubmits=1), CheckpointKind.PROTOCOL_RESUBMIT),
        (LoginBudget(credential_submissions=2, captcha_resubmits=1), CheckpointKind.CAPTCHA_RETRY),
    ],
)
def test_retry_requires_one_prior_submission_and_unused_relevant_budget(
    budget: LoginBudget,
    kind: CheckpointKind,
):
    with pytest.raises(LoginCheckpointBlocked):
        reduce_login_checkpoint(
            CheckpointPhase.POST_SUBMIT,
            budget,
            CheckpointOutcome(kind, rule_name="explicit-rule"),
        )


@pytest.mark.parametrize(
    "values",
    [
        {"credential_submissions": -1},
        {"protocol_resubmits": -1},
        {"captcha_resubmits": -1},
        {"reloads": -1},
        {"reloads": 2},
        {"credential_submissions": 3},
        {"credential_submissions": 1, "protocol_resubmits": 1},
        {"credential_submissions": 1, "captcha_resubmits": 1},
        {"credential_submissions": 2},
        {"credential_submissions": 2, "protocol_resubmits": 1, "captcha_resubmits": 1},
        {"credential_submissions": 2, "protocol_resubmits": 2},
        {"credential_submissions": 2, "captcha_resubmits": 2},
    ],
)
def test_login_budget_rejects_impossible_states(values: dict[str, int]):
    with pytest.raises(ValueError):
        LoginBudget(**values)


@pytest.mark.parametrize(
    "budget",
    [
        LoginBudget(),
        LoginBudget(reloads=1),
        LoginBudget(credential_submissions=1),
        LoginBudget(credential_submissions=1, reloads=1),
        LoginBudget(credential_submissions=2, protocol_resubmits=1),
        LoginBudget(credential_submissions=2, protocol_resubmits=1, reloads=1),
        LoginBudget(credential_submissions=2, captcha_resubmits=1),
        LoginBudget(credential_submissions=2, captcha_resubmits=1, reloads=1),
    ],
)
def test_login_budget_accepts_only_reachable_states(budget: LoginBudget):
    assert budget.reloads in (0, 1)


def test_explicit_login_error_message_has_safe_diagnostic_evidence():
    budget = LoginBudget(credential_submissions=2, protocol_resubmits=1, reloads=1)
    outcome = CheckpointOutcome(
        CheckpointKind.EXPLICIT_LOGIN_ERROR,
        rule_name="bad-password-rule",
        action_label="secret-action",
        interaction="secret-interaction",
    )

    with pytest.raises(LoginCheckpointBlocked) as raised:
        reduce_login_checkpoint(CheckpointPhase.POST_SUBMIT, budget, outcome)

    message = str(raised.value)
    assert "kind=explicit_login_error" in message
    assert "rule_name=bad-password-rule" in message
    assert "credential_submissions=2" in message
    assert "protocol_resubmits=1" in message
    assert "captcha_resubmits=0" in message
    assert "reloads=1" in message
    assert "secret-action" not in message
    assert "secret-interaction" not in message


@pytest.mark.parametrize("phase", list(CheckpointPhase))
def test_authenticated_advances_to_settle_without_consuming_budget(phase: CheckpointPhase):
    budget = LoginBudget(credential_submissions=1)

    assert reduce_login_checkpoint(
        phase,
        budget,
        CheckpointOutcome(CheckpointKind.AUTHENTICATED),
    ) == (CheckpointPhase.POST_SUBMIT_SETTLE, budget)


def test_explicit_captcha_retry_is_not_inferred_from_unknown_error():
    budget = LoginBudget(credential_submissions=1)
    outcome = CheckpointOutcome(
        CheckpointKind.UNKNOWN_BLOCKER,
        rule_name="text-mentioned-captcha-invalid",
    )

    with pytest.raises(LoginCheckpointBlocked) as raised:
        reduce_login_checkpoint(CheckpointPhase.POST_SUBMIT, budget, outcome)

    assert raised.value.budget == budget
    assert raised.value.outcome.kind is CheckpointKind.UNKNOWN_BLOCKER


def test_captcha_text_is_not_a_retry_kind():
    outcome = CheckpointOutcome(cast(CheckpointKind, "captcha_invalid"))

    with pytest.raises(LoginCheckpointBlocked):
        reduce_login_checkpoint(CheckpointPhase.POST_SUBMIT, LoginBudget(credential_submissions=1), outcome)
