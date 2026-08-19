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
