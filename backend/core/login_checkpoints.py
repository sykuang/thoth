from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import StrEnum
from hashlib import sha256
import re
from typing import Any


class CheckpointPhase(StrEnum):
    PRE_SUBMIT = "pre_submit"
    POST_SUBMIT = "post_submit"
    POST_SUBMIT_SETTLE = "post_submit_settle"


class CheckpointReason(StrEnum):
    UNSPECIFIED = "unspecified"
    ACTION_CLICK_EXCEPTION = 'action_click_exception'
    ACTION_GUARD_DENIED = 'action_guard_denied'
    ACTION_GUARD_EXCEPTION = 'action_guard_exception'
    ACTION_NOT_UNIQUE = 'action_not_unique'
    AUTHENTICATION_EXCEPTION = 'authentication_exception'
    BANK_MISMATCH = 'bank_mismatch'
    COLLECT_ORIGIN_OR_DIALOG = 'collect_origin_or_dialog'
    DIALOG_BLOCKED = 'dialog_blocked'
    DIALOG_DISMISS_EXCEPTION = 'dialog_dismiss_exception'
    ORIGIN_INSPECTION_EXCEPTION = 'origin_inspection_exception'
    DUPLICATE_RULE_NAMES = 'duplicate_rule_names'
    FORM_CONTROLS_PRESENT = 'form_controls_present'
    FRAME_INSPECTION_EXCEPTION = 'frame_inspection_exception'
    INSPECTION_EXCEPTION = 'inspection_exception'
    INVALID_OUTCOME = 'invalid_outcome'
    INVALID_TRANSITION = 'invalid_transition'
    MATCHED_BLOCKER = 'matched_blocker'
    NATIVE_FORM_SUBMISSION = 'native_form_submission'
    NAVIGATION_ORIGIN = 'navigation_origin'
    NO_MATCHING_CHECKPOINT = 'no_matching_checkpoint'
    NO_PROGRESS = 'no_progress'
    ORIGIN_AFTER_EVALUATION = 'origin_after_evaluation'
    ORIGIN_AFTER_PREPARE = 'origin_after_prepare'
    ORIGIN_BEFORE_EVALUATION = 'origin_before_evaluation'
    ORIGIN_BEFORE_PREPARE = 'origin_before_prepare'
    ORIGIN_BEFORE_SUBMIT = 'origin_before_submit'
    PROGRESS_INSPECTION_EXCEPTION = 'progress_inspection_exception'
    PROGRESS_WAIT_EXCEPTION = 'progress_wait_exception'
    RULE_BUDGET_EXHAUSTED = 'rule_budget_exhausted'
    RULE_INSPECTION_EXCEPTION = 'rule_inspection_exception'
    STEP_LIMIT = 'step_limit'


class CheckpointKind(StrEnum):
    AUTHENTICATED = "authenticated"
    READY_FOR_CREDENTIALS = "ready_for_credentials"
    DISMISSIBLE_NOTICE = "dismissible_notice"
    DUPLICATE_SESSION = "duplicate_session"
    PROTOCOL_RESUBMIT = "protocol_resubmit"
    CAPTCHA_RETRY = "captcha_retry"
    STARTUP_RECOVERY = "startup_recovery"
    OTP_REQUIRED = "otp_required"
    PASSWORD_CHANGE_OPTIONAL = "password_change_optional"
    PASSWORD_CHANGE_REQUIRED = "password_change_required"
    EXPLICIT_LOGIN_ERROR = "explicit_login_error"
    UNKNOWN_BLOCKER = "unknown_blocker"


DEFAULT_ACTION_SELECTOR = "button, a, [role=button]"
_CLICKABLE_RULE_KINDS = frozenset(
    {
        CheckpointKind.DISMISSIBLE_NOTICE,
        CheckpointKind.DUPLICATE_SESSION,
        CheckpointKind.PROTOCOL_RESUBMIT,
        CheckpointKind.PASSWORD_CHANGE_OPTIONAL,
    }
)
_CLASSIFIER_RULE_KINDS = frozenset(
    {
        CheckpointKind.CAPTCHA_RETRY,
        CheckpointKind.STARTUP_RECOVERY,
        CheckpointKind.OTP_REQUIRED,
        CheckpointKind.PASSWORD_CHANGE_REQUIRED,
        CheckpointKind.EXPLICIT_LOGIN_ERROR,
        CheckpointKind.UNKNOWN_BLOCKER,
    }
)


_CSS_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_-]*"
_EXACT_ATTRIBUTE_VALUE = rf'(?:{_CSS_IDENTIFIER}|\'[^\'\\\r\n]*\'|"[^"\\\r\n]*")'
_SCOPING_ATOM = (
    rf"(?:#{_CSS_IDENTIFIER}|\.{_CSS_IDENTIFIER}|"
    rf"\[{_CSS_IDENTIFIER}={_EXACT_ATTRIBUTE_VALUE}\])"
)
_SIMPLE_SCOPED_SELECTOR = re.compile(rf"(?:[A-Za-z][A-Za-z0-9-]*)?{_SCOPING_ATOM}+")
_GENERIC_ROLE_BUTTON_SELECTOR = re.compile(
    r'''\[role=(?:button|'button'|"button")\]''', re.IGNORECASE
)
_BROWSER_DEFAULT_TIMEOUT_MS = 180000
_LOGIN_INSPECTION_TIMEOUT_MS = 5000
_LOCATOR_SNAPSHOT_TIMEOUT_MS = 100
_MAX_LOCATOR_MATCHES = 500


@contextmanager
def bounded_login_inspection(page: Any):
    page.set_default_timeout(_LOGIN_INSPECTION_TIMEOUT_MS)
    try:
        yield
    finally:
        page.set_default_timeout(_BROWSER_DEFAULT_TIMEOUT_MS)


def bounded_locator_matches(
    locator: Any, *, first_timeout_ms: int = _LOCATOR_SNAPSHOT_TIMEOUT_MS
) -> Iterator[Any]:
    from patchright.sync_api import TimeoutError as PatchrightTimeoutError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    seen_handles: list[Any] = []
    for index in range(_MAX_LOCATOR_MATCHES + 1):
        try:
            candidate = locator.nth(index)
            handle = candidate.element_handle(
                timeout=first_timeout_ms if index == 0 else _LOCATOR_SNAPSHOT_TIMEOUT_MS
            )
        except (IndexError, StopIteration, TimeoutError, PatchrightTimeoutError, PlaywrightTimeoutError):
            return
        if handle is None:
            return
        if any(handle is seen for seen in seen_handles):
            return
        seen_handles.append(handle)
        if index == _MAX_LOCATOR_MATCHES:
            raise RuntimeError("login checkpoint locator exceeded safe match limit")
        yield candidate


def _is_simple_scoped_selector(selector: str) -> bool:
    return _SIMPLE_SCOPED_SELECTOR.fullmatch(selector) is not None


@dataclass(frozen=True)
class LoginCheckpointRule:
    name: str
    bank: str
    phases: tuple[CheckpointPhase, ...]
    kind: CheckpointKind
    container_selector: str
    action_selector: str = DEFAULT_ACTION_SELECTOR
    action_texts: tuple[str, ...] = ()
    required_body_pattern: re.Pattern[str] | None = None
    max_actions: int = 1
    first_match_timeout_ms: int = _LOCATOR_SNAPSHOT_TIMEOUT_MS

    @property
    def is_clickable(self) -> bool:
        return self.kind in _CLICKABLE_RULE_KINDS

    def __post_init__(self) -> None:
        if (
            not self.name.strip()
            or not self.bank.strip()
            or not self.container_selector.strip()
            or not self.action_selector.strip()
            or self.container_selector != self.container_selector.strip()
            or self.action_selector != self.action_selector.strip()
            or not self.phases
            or self.max_actions < 1
            or type(self.first_match_timeout_ms) is not int
            or not (1 <= self.first_match_timeout_ms <= _LOGIN_INSPECTION_TIMEOUT_MS)
        ):
            raise ValueError("invalid login checkpoint rule")
        if not _is_simple_scoped_selector(self.container_selector):
            raise ValueError("login checkpoint rule requires a scoped container")
        normalized_action_texts = tuple(" ".join(text.split()) for text in self.action_texts)
        if self.kind in _CLASSIFIER_RULE_KINDS:
            if self.action_texts or self.action_selector != DEFAULT_ACTION_SELECTOR:
                raise ValueError("classifier login checkpoint rules cannot configure actions")
        elif self.is_clickable:
            if (
                any(not text for text in normalized_action_texts)
                or (self.action_selector == DEFAULT_ACTION_SELECTOR and not normalized_action_texts)
                or (
                    self.action_selector != DEFAULT_ACTION_SELECTOR
                    and (
                        not _is_simple_scoped_selector(self.action_selector)
                        or _GENERIC_ROLE_BUTTON_SELECTOR.fullmatch(self.action_selector) is not None
                    )
                )
            ):
                raise ValueError("clickable login checkpoint rule requires a safe action")
        else:
            raise ValueError("login checkpoint kind is evaluator-only")


@dataclass(frozen=True)
class LoginBudget:
    credential_submissions: int = 0
    protocol_resubmits: int = 0
    reloads: int = 0
    captcha_resubmits: int = 0

    def __post_init__(self) -> None:
        submission_state = (
            self.credential_submissions,
            self.protocol_resubmits,
            self.captcha_resubmits,
        )
        if any(
            type(value) is not int
            for value in (*submission_state, self.reloads)
        ) or self.reloads not in (0, 1) or submission_state not in {
            (0, 0, 0),
            (1, 0, 0),
            (2, 1, 0),
            (2, 0, 1),
        }:
            raise ValueError("invalid login budget")


@dataclass(frozen=True)
class CheckpointOutcome:
    kind: CheckpointKind
    rule_name: str | None = None
    action_label: str | None = None
    interaction: str | None = None
    reason: CheckpointReason = field(default=CheckpointReason.UNSPECIFIED, compare=False)


def validate_login_checkpoint_outcome(
    outcome: CheckpointOutcome,
    active_rules: tuple[LoginCheckpointRule, ...],
) -> CheckpointOutcome:
    rules_by_name = {rule.name: rule for rule in active_rules}
    if outcome.kind in {
        CheckpointKind.AUTHENTICATED,
        CheckpointKind.READY_FOR_CREDENTIALS,
    }:
        valid = outcome.rule_name is None
    elif outcome.kind is CheckpointKind.UNKNOWN_BLOCKER:
        valid = outcome.rule_name is None or outcome.rule_name in rules_by_name
    else:
        rule = rules_by_name.get(outcome.rule_name or "")
        valid = rule is not None and rule.kind is outcome.kind
    return outcome if valid else CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, reason=CheckpointReason.INVALID_OUTCOME)


def _action_selected(action: Any) -> bool:
    if any(action.get_attribute(name) is not None for name in ("checked", "selected")):
        return True
    return any(
        (action.get_attribute(name) or "").lower() in {"true", "1", "selected", "checked"}
        for name in ("aria-selected", "aria-checked", "aria-pressed", "data-selected")
    )


def _interaction(kind: CheckpointKind) -> str | None:
    if kind is CheckpointKind.OTP_REQUIRED:
        return "otp"
    if kind in (
        CheckpointKind.PASSWORD_CHANGE_OPTIONAL,
        CheckpointKind.PASSWORD_CHANGE_REQUIRED,
    ):
        return "password_change"
    return None


def _matching_body_fingerprint(
    container: Any, required_pattern: re.Pattern[str] | None
) -> bytes | None:
    body = container.inner_text(timeout=5000)
    if required_pattern and not required_pattern.search(body):
        return None
    return sha256(body.encode()).digest()


def _evaluate_rule(
    scopes: list[Any],
    rule: LoginCheckpointRule,
    can_act: Callable[[], bool] | None = None,
) -> CheckpointOutcome | None:
    from patchright.sync_api import TimeoutError as PatchrightTimeoutError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    matched = []
    for scope in scopes:
        containers = scope.locator(rule.container_selector)
        for container in bounded_locator_matches(
            containers, first_timeout_ms=rule.first_match_timeout_ms
        ):
            if not container.is_visible():
                continue
            nested = container.locator(rule.container_selector)
            if any(item.is_visible() for item in bounded_locator_matches(nested)):
                continue
            fingerprint = _matching_body_fingerprint(container, rule.required_body_pattern)
            if fingerprint is None:
                continue
            matched.append((container, fingerprint))

    if not matched:
        return None
    if rule.kind in _CLASSIFIER_RULE_KINDS:
        return CheckpointOutcome(
            rule.kind,
            rule_name=rule.name,
            interaction=_interaction(rule.kind),
            reason=CheckpointReason.MATCHED_BLOCKER if rule.kind is CheckpointKind.UNKNOWN_BLOCKER else CheckpointReason.UNSPECIFIED,
        )
    if rule.kind in {CheckpointKind.DISMISSIBLE_NOTICE, CheckpointKind.DUPLICATE_SESSION}:
        for container, _ in matched:
            form_controls = container.locator(
                "input, select, textarea, [contenteditable]:not([contenteditable='false'])"
            )
            # Duplicate confirmation must not submit even hidden credentials/OTP:
            # native form submissions are outside the reducer's credential budget.
            if any(
                rule.kind is CheckpointKind.DUPLICATE_SESSION or item.is_visible()
                for item in bounded_locator_matches(form_controls)
            ):
                return CheckpointOutcome(
                    CheckpointKind.UNKNOWN_BLOCKER, reason=CheckpointReason.FORM_CONTROLS_PRESENT,
                    rule_name=rule.name,
                )

    eligible = []
    action_labels = {
        normalized: normalized
        for text in rule.action_texts
        if (normalized := " ".join(text.split()))
    }
    for container, fingerprint in matched:
        actions = container.locator(rule.action_selector)
        for action in bounded_locator_matches(
            actions, first_timeout_ms=rule.first_match_timeout_ms
        ):
            if not action.is_visible():
                continue
            if action_labels:
                label = action_labels.get(" ".join(action.inner_text().split()))
                if label is None:
                    continue
            else:
                label = None
            eligible.append((container, fingerprint, action, label))
    if len(eligible) != 1:
        return CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, reason=CheckpointReason.ACTION_NOT_UNIQUE, rule_name=rule.name)

    container, fingerprint, action, label = eligible[0]
    # Native form ownership includes ancestors and external form= targets.
    # A duplicate confirmation must never bypass the credential budget.
    if rule.kind is CheckpointKind.DUPLICATE_SESSION and action.evaluate(
        "el => (el instanceof HTMLButtonElement || el instanceof HTMLInputElement)"
        " && el.form !== null && ['submit', 'image'].includes(el.type)"
    ):
        return CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, reason=CheckpointReason.NATIVE_FORM_SUBMISSION, rule_name=rule.name)
    was_enabled = action.is_enabled()
    was_selected = _action_selected(action)
    try:
        if can_act is not None and not can_act():
            return CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, reason=CheckpointReason.ACTION_GUARD_DENIED, rule_name=rule.name)
    except Exception:
        return CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, reason=CheckpointReason.ACTION_GUARD_EXCEPTION, rule_name=rule.name)
    try:
        action.click()
    except Exception:
        return CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, reason=CheckpointReason.ACTION_CLICK_EXCEPTION, rule_name=rule.name)
    try:
        container.wait_for(state="hidden", timeout=500)
        progressed = True
    except (TimeoutError, PatchrightTimeoutError, PlaywrightTimeoutError):
        try:
            progressed = (
                not container.is_visible()
                or not action.is_visible()
                or (was_enabled and not action.is_enabled())
                or (not was_selected and _action_selected(action))
                or _matching_body_fingerprint(container, None) != fingerprint
            )
        except Exception:
            return CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, rule_name=rule.name,
                                     reason=CheckpointReason.PROGRESS_INSPECTION_EXCEPTION)
    except Exception:
        return CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, reason=CheckpointReason.PROGRESS_WAIT_EXCEPTION, rule_name=rule.name)
    if not progressed:
        return CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, reason=CheckpointReason.NO_PROGRESS, rule_name=rule.name)
    return CheckpointOutcome(
        rule.kind,
        rule_name=rule.name,
        action_label=label,
        interaction=_interaction(rule.kind),
    )


def _evaluate_login_checkpoint(
    page: Any,
    *,
    bank: str,
    phase: CheckpointPhase,
    rules: tuple[LoginCheckpointRule, ...],
    is_authenticated: Callable[[Any], bool],
    is_scope_owned: Callable[[Any], bool] | None = None,
    can_act: Callable[[], bool] | None = None,
) -> CheckpointOutcome:
    inspection_reason = CheckpointReason.AUTHENTICATION_EXCEPTION
    try:
        if any(rule.bank != bank for rule in rules):
            return CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, reason=CheckpointReason.BANK_MISMATCH)
        if (
            phase is not CheckpointPhase.POST_SUBMIT_SETTLE
            and is_authenticated(page)
        ):
            return CheckpointOutcome(CheckpointKind.AUTHENTICATED)
        inspection_reason = CheckpointReason.FRAME_INSPECTION_EXCEPTION
        scopes = [page]
        if is_scope_owned is not None:
            scopes.extend(
                frame
                for frame in page.frames
                if frame is not page.main_frame and is_scope_owned(frame)
            )
    except Exception:
        return CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, reason=inspection_reason)

    for rule in rules:
        if phase not in rule.phases:
            continue
        try:
            outcome = _evaluate_rule(scopes, rule, can_act)
        except Exception:
            return CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, reason=CheckpointReason.RULE_INSPECTION_EXCEPTION, rule_name=rule.name)
        if outcome:
            return outcome

    if phase is CheckpointPhase.POST_SUBMIT_SETTLE:
        try:
            if is_authenticated(page):
                return CheckpointOutcome(CheckpointKind.AUTHENTICATED)
        except Exception:
            return CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, reason=CheckpointReason.AUTHENTICATION_EXCEPTION)
    fallback = (
        CheckpointKind.READY_FOR_CREDENTIALS
        if phase is CheckpointPhase.PRE_SUBMIT
        else CheckpointKind.UNKNOWN_BLOCKER
    )
    return CheckpointOutcome(fallback, reason=CheckpointReason.NO_MATCHING_CHECKPOINT
                             if fallback is CheckpointKind.UNKNOWN_BLOCKER else CheckpointReason.UNSPECIFIED)


def evaluate_login_checkpoint(
    page: Any,
    *,
    bank: str,
    phase: CheckpointPhase,
    rules: tuple[LoginCheckpointRule, ...],
    is_authenticated: Callable[[Any], bool],
    is_scope_owned: Callable[[Any], bool] | None = None,
    can_act: Callable[[], bool] | None = None,
) -> CheckpointOutcome:
    if any(rule.bank != bank for rule in rules):
        return CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, reason=CheckpointReason.BANK_MISMATCH)
    try:
        with bounded_login_inspection(page):
            return _evaluate_login_checkpoint(
                page,
                bank=bank,
                phase=phase,
                rules=rules,
                is_authenticated=is_authenticated,
                is_scope_owned=is_scope_owned,
                can_act=can_act,
            )
    except Exception:
        return CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, reason=CheckpointReason.INSPECTION_EXCEPTION)


class LoginCheckpointTerminal(RuntimeError):
    def __init__(
        self,
        budget: LoginBudget,
        outcome: CheckpointOutcome,
        *,
        phase: CheckpointPhase | None = None,
    ) -> None:
        self.budget = budget
        self.outcome = outcome
        self.phase = phase
        # Only the bank-aware run sink may render diagnostic evidence.
        super().__init__("terminal login checkpoint; details withheld")


class LoginCheckpointBlocked(LoginCheckpointTerminal):
    pass


class LoginInteractionRequired(LoginCheckpointTerminal):
    pass


def reduce_login_checkpoint(
    phase: CheckpointPhase,
    budget: LoginBudget,
    outcome: CheckpointOutcome,
) -> tuple[CheckpointPhase, LoginBudget]:
    if outcome.kind in (
        CheckpointKind.EXPLICIT_LOGIN_ERROR,
        CheckpointKind.UNKNOWN_BLOCKER,
    ):
        raise LoginCheckpointBlocked(budget, outcome, phase=phase)
    if outcome.kind in (
        CheckpointKind.OTP_REQUIRED,
        CheckpointKind.PASSWORD_CHANGE_REQUIRED,
    ):
        raise LoginInteractionRequired(budget, outcome, phase=phase)
    if outcome.kind is CheckpointKind.AUTHENTICATED:
        return CheckpointPhase.POST_SUBMIT_SETTLE, budget
    if outcome.kind in (
        CheckpointKind.DISMISSIBLE_NOTICE,
        CheckpointKind.DUPLICATE_SESSION,
        CheckpointKind.PASSWORD_CHANGE_OPTIONAL,
    ):
        return phase, budget
    if outcome.kind is CheckpointKind.READY_FOR_CREDENTIALS:
        if phase is not CheckpointPhase.PRE_SUBMIT or budget.credential_submissions != 0:
            raise LoginCheckpointBlocked(budget, replace(outcome, reason=CheckpointReason.INVALID_TRANSITION), phase=phase)
        return CheckpointPhase.POST_SUBMIT, replace(budget, credential_submissions=1)
    if outcome.kind is CheckpointKind.PROTOCOL_RESUBMIT:
        if (
            phase is not CheckpointPhase.POST_SUBMIT
            or budget.credential_submissions != 1
            or budget.protocol_resubmits >= 1
            or not outcome.rule_name
        ):
            raise LoginCheckpointBlocked(budget, replace(outcome, reason=CheckpointReason.INVALID_TRANSITION), phase=phase)
        return CheckpointPhase.POST_SUBMIT, replace(
            budget,
            credential_submissions=2,
            protocol_resubmits=1,
        )
    if outcome.kind is CheckpointKind.CAPTCHA_RETRY:
        if (
            phase is not CheckpointPhase.POST_SUBMIT
            or budget.credential_submissions != 1
            or budget.captcha_resubmits >= 1
            or not outcome.rule_name
        ):
            raise LoginCheckpointBlocked(budget, replace(outcome, reason=CheckpointReason.INVALID_TRANSITION), phase=phase)
        return CheckpointPhase.POST_SUBMIT, replace(
            budget,
            credential_submissions=2,
            captcha_resubmits=1,
        )
    if outcome.kind is CheckpointKind.STARTUP_RECOVERY:
        if budget.reloads >= 1:
            raise LoginCheckpointBlocked(budget, replace(outcome, reason=CheckpointReason.INVALID_TRANSITION), phase=phase)
        return CheckpointPhase.PRE_SUBMIT, replace(budget, reloads=1)
    raise LoginCheckpointBlocked(budget, replace(outcome, reason=CheckpointReason.INVALID_TRANSITION), phase=phase)
