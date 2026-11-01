from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import StrEnum
from hashlib import sha256
import re
from typing import Any

from patchright.sync_api import TimeoutError as PatchrightTimeoutError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from backend.core.error_codes import BANKS, code_details, error_step, exception_error_id, with_error_code


class CheckpointPhase(StrEnum):
    PRE_SUBMIT = "pre_submit"
    POST_SUBMIT = "post_submit"
    POST_SUBMIT_SETTLE = "post_submit_settle"


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
def bounded_login_inspection(page: Any, *, bank: str | None = None):
    with error_step(_checkpoint_code(bank, "SYS-03-986-001")):
        page.set_default_timeout(_LOGIN_INSPECTION_TIMEOUT_MS)
    try:
        yield
    finally:
        with error_step(_checkpoint_code(bank, "SYS-03-986-002")):
            page.set_default_timeout(_BROWSER_DEFAULT_TIMEOUT_MS)


def _checkpoint_operand(bank: str | None, error_id: str | None, operation: Callable[[], Any]) -> Any:
    if error_id is None:
        return operation()
    with error_step(_checkpoint_code(bank, error_id)):
        return operation()


def bounded_locator_matches(
    locator: Any, *, first_timeout_ms: int = _LOCATOR_SNAPSHOT_TIMEOUT_MS,
    bank: str | None = None, error_ids: tuple[str, str] | None = None,
) -> Iterator[Any]:
    seen_handles: list[Any] = []
    for index in range(_MAX_LOCATOR_MATCHES + 1):
        try:
            candidate = _checkpoint_operand(
                bank, error_ids[0] if error_ids else None, lambda: locator.nth(index),
            )
            handle = _checkpoint_operand(
                bank, error_ids[1] if error_ids else None, lambda: candidate.element_handle(
                    timeout=first_timeout_ms if index == 0 else _LOCATOR_SNAPSHOT_TIMEOUT_MS
                ),
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
    require_exclusive_action: bool = False

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
            or type(self.require_exclusive_action) is not bool
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
        ):
            raise with_error_code(ValueError("invalid login budget"), "SYS-03-983-002")
        if self.reloads not in (0, 1):
            raise with_error_code(ValueError("invalid login budget"), "SYS-03-983-003")
        if submission_state not in {
            (0, 0, 0),
            (1, 0, 0),
            (2, 1, 0),
            (2, 0, 1),
        }:
            raise with_error_code(ValueError("invalid login budget"), "SYS-03-983-004")


@dataclass(frozen=True)
class CheckpointOutcome:
    kind: CheckpointKind
    rule_name: str | None = None
    action_label: str | None = None
    interaction: str | None = None
    error_id: str | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        # Metadata is a closed registered ID, never DOM text or a callable value.
        if self.error_id is not None and code_details(self.error_id) is None:
            object.__setattr__(self, "error_id", None)


def _checkpoint_code(bank: str | None, error_id: str) -> str:
    if type(bank) is str and bank in BANKS:
        return bank.upper() + error_id[3:]
    return error_id


def _blocked_checkpoint(
    error_id: str, *, bank: str | None = None,
    rule: LoginCheckpointRule | None = None, error: Exception | None = None,
) -> CheckpointOutcome:
    if rule is not None:
        bank = rule.bank
    return CheckpointOutcome(
        CheckpointKind.UNKNOWN_BLOCKER,
        rule_name=rule.name if rule is not None else None,
        error_id=exception_error_id(error) or _checkpoint_code(bank, error_id),
    )


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
    return outcome if valid else CheckpointOutcome(
        CheckpointKind.UNKNOWN_BLOCKER, error_id=outcome.error_id or "SYS-03-989-001",
    )


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
    scopes: list[Any], rule: LoginCheckpointRule,
    can_act: Callable[[], bool] | None = None,
) -> CheckpointOutcome | None:
    matched = []
    visible_container_count = 0
    visible_blocker_count = 0
    with error_step(_checkpoint_code(rule.bank, "SYS-03-970-001")):
        for scope in scopes:
            if rule.require_exclusive_action:
                with error_step(_checkpoint_code(rule.bank, "SYS-03-970-011")):
                    blockers = scope.locator(
                        "dialog[open], [role='dialog'], [aria-modal='true'], .modal.show, .modal.in"
                    )
                visible_blocker_count += _checkpoint_operand(rule.bank, "SYS-03-970-014", lambda: sum(
                    item.is_visible() for item in bounded_locator_matches(
                        blockers, bank=rule.bank, error_ids=("SYS-03-970-012", "SYS-03-970-013"),
                    )
                ))
            with error_step(_checkpoint_code(rule.bank, "SYS-03-970-015")):
                containers = scope.locator(rule.container_selector)
            for container in bounded_locator_matches(
                containers, first_timeout_ms=rule.first_match_timeout_ms,
                bank=rule.bank, error_ids=("SYS-03-970-016", "SYS-03-970-017"),
            ):
                if _checkpoint_operand(rule.bank, "SYS-03-970-018", lambda: not container.is_visible()):
                    continue
                visible_container_count += 1
                with error_step(_checkpoint_code(rule.bank, "SYS-03-970-019")):
                    nested = container.locator(rule.container_selector)
                if _checkpoint_operand(rule.bank, "SYS-03-970-022", lambda: any(
                    item.is_visible() for item in bounded_locator_matches(
                        nested, bank=rule.bank, error_ids=("SYS-03-970-020", "SYS-03-970-021"),
                    )
                )):
                    continue
                with error_step(_checkpoint_code(rule.bank, "SYS-03-970-002")):
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
        )
    if rule.kind in {CheckpointKind.DISMISSIBLE_NOTICE, CheckpointKind.DUPLICATE_SESSION}:
        for container, _ in matched:
            with error_step(_checkpoint_code(rule.bank, "SYS-03-971-002")):
                with error_step(_checkpoint_code(rule.bank, "SYS-03-971-011")):
                    form_controls = container.locator(
                        "input, select, textarea, [contenteditable]:not([contenteditable='false'])"
                    )
                if _checkpoint_operand(rule.bank, "SYS-03-971-014", lambda: any(
                    rule.kind is CheckpointKind.DUPLICATE_SESSION or item.is_visible()
                    for item in bounded_locator_matches(
                        form_controls, bank=rule.bank, error_ids=("SYS-03-971-012", "SYS-03-971-013"),
                    )
                )):
                    return _blocked_checkpoint("SYS-03-971-001", rule=rule)

    eligible = []
    visible_actions = 0
    action_labels = {
        normalized: normalized
        for text in rule.action_texts
        if (normalized := " ".join(text.split()))
    }
    for container, fingerprint in matched:
        if rule.require_exclusive_action:
            with error_step(_checkpoint_code(rule.bank, "SYS-03-972-006")):
                with error_step(_checkpoint_code(rule.bank, "SYS-03-972-011")):
                    native_actions = container.locator(
                        "button, a[href], input, select, textarea, summary, [role=button]"
                    )
                visible_actions += _checkpoint_operand(rule.bank, "SYS-03-972-014", lambda: sum(
                    item.is_visible() for item in bounded_locator_matches(
                        native_actions, bank=rule.bank, error_ids=("SYS-03-972-012", "SYS-03-972-013"),
                    )
                ))
        with error_step(_checkpoint_code(rule.bank, "SYS-03-972-007")):
            with error_step(_checkpoint_code(rule.bank, "SYS-03-972-015")):
                actions = container.locator(rule.action_selector)
            for action in bounded_locator_matches(
                actions, first_timeout_ms=rule.first_match_timeout_ms,
                bank=rule.bank, error_ids=("SYS-03-972-016", "SYS-03-972-017"),
            ):
                if _checkpoint_operand(rule.bank, "SYS-03-972-018", lambda: not action.is_visible()):
                    continue
                if not rule.require_exclusive_action:
                    visible_actions += 1
                if action_labels:
                    with error_step(_checkpoint_code(rule.bank, "SYS-03-972-019")):
                        label = action_labels.get(" ".join(action.inner_text().split()))
                    if label is None:
                        continue
                else:
                    label = None
                eligible.append((container, fingerprint, action, label))
    if len(eligible) == 0:
        return _blocked_checkpoint("SYS-03-972-001", rule=rule)
    if len(eligible) != 1:
        return _blocked_checkpoint("SYS-03-972-002", rule=rule)
    if rule.require_exclusive_action:
        if visible_container_count != 1:
            return _blocked_checkpoint("SYS-03-972-003", rule=rule)
        if visible_blocker_count != 1:
            return _blocked_checkpoint("SYS-03-972-004", rule=rule)
        if visible_actions != 1:
            return _blocked_checkpoint("SYS-03-972-005", rule=rule)

    container, fingerprint, action, label = eligible[0]
    # Native form submissions bypass the reducer's credential budget, even
    # when the submit button targets a form outside the visible notice.
    with error_step(_checkpoint_code(rule.bank, "SYS-03-973-002")):
        native_action = getattr(action, "evaluate", None)
        if (rule.kind in {CheckpointKind.DUPLICATE_SESSION, CheckpointKind.DISMISSIBLE_NOTICE}
                and native_action is not None and native_action(
                    "el => (el instanceof HTMLButtonElement || el instanceof HTMLInputElement)"
                    " && el.form !== null && ['submit', 'image'].includes(el.type)"
                )):
            return _blocked_checkpoint("SYS-03-973-001", rule=rule)
    with error_step(_checkpoint_code(rule.bank, "SYS-03-974-001")):
        was_enabled = action.is_enabled()
    with error_step(_checkpoint_code(rule.bank, "SYS-03-974-002")):
        was_selected = _action_selected(action)
    try:
        if can_act is not None and not can_act():
            return _blocked_checkpoint("SYS-03-975-001", rule=rule)
    except Exception as error:
        return _blocked_checkpoint("SYS-03-975-002", rule=rule, error=error)
    try:
        action.click()
    except Exception as error:
        return _blocked_checkpoint("SYS-03-976-001", rule=rule, error=error)
    try:
        container.wait_for(state="hidden", timeout=500)
        progressed = True
    except (TimeoutError, PatchrightTimeoutError, PlaywrightTimeoutError):
        try:
            progressed = (
                _checkpoint_operand(rule.bank, "SYS-03-978-011", lambda: not container.is_visible())
                or _checkpoint_operand(rule.bank, "SYS-03-978-012", lambda: not action.is_visible())
                or _checkpoint_operand(rule.bank, "SYS-03-978-013", lambda: bool(was_enabled and not action.is_enabled()))
                or _checkpoint_operand(rule.bank, "SYS-03-978-014", lambda: bool(not was_selected and _action_selected(action)))
                or _checkpoint_operand(rule.bank, "SYS-03-978-015", lambda: _matching_body_fingerprint(container, None) != fingerprint)
            )
        except Exception as error:
            return _blocked_checkpoint("SYS-03-978-001", rule=rule, error=error)
    except Exception as error:
        return _blocked_checkpoint("SYS-03-977-001", rule=rule, error=error)
    if not progressed:
        return _blocked_checkpoint("SYS-03-978-002", rule=rule)
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
    inspection_error_id = "SYS-03-987-002"
    try:
        if any(rule.bank != bank for rule in rules):
            return _blocked_checkpoint("SYS-03-987-001", bank=bank)
        inspection_error_id = "SYS-03-984-001"
        if (
            phase is not CheckpointPhase.POST_SUBMIT_SETTLE
            and is_authenticated(page)
        ):
            return CheckpointOutcome(CheckpointKind.AUTHENTICATED)
        scopes = [page]
        inspection_error_id = "SYS-03-985-001"
        if is_scope_owned is not None:
            scopes.extend(
                frame
                for frame in page.frames
                if frame is not page.main_frame and is_scope_owned(frame)
            )
    except Exception as error:
        return _blocked_checkpoint(inspection_error_id, bank=bank, error=error)

    for rule in rules:
        if phase not in rule.phases:
            continue
        try:
            outcome = _evaluate_rule(scopes, rule, can_act)
        except Exception as error:
            return _blocked_checkpoint("SYS-03-979-001", rule=rule, error=error)
        if outcome:
            return outcome

    if phase is CheckpointPhase.POST_SUBMIT_SETTLE:
        try:
            if is_authenticated(page):
                return CheckpointOutcome(CheckpointKind.AUTHENTICATED)
        except Exception as error:
            return _blocked_checkpoint("SYS-03-984-002", bank=bank, error=error)
    if phase is CheckpointPhase.PRE_SUBMIT:
        return CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS)
    return _blocked_checkpoint(
        "SYS-03-988-002" if phase is CheckpointPhase.POST_SUBMIT_SETTLE else "SYS-03-988-001",
        bank=bank,
    )


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
    with error_step(_checkpoint_code(bank, "SYS-03-987-002")):
        if any(rule.bank != bank for rule in rules):
            return _blocked_checkpoint("SYS-03-987-001", bank=bank)
    try:
        with bounded_login_inspection(page, bank=bank):
            return _evaluate_login_checkpoint(
                page,
                bank=bank,
                phase=phase,
                rules=rules,
                is_authenticated=is_authenticated,
                is_scope_owned=is_scope_owned,
                can_act=can_act,
            )
    except Exception as error:
        return _blocked_checkpoint("SYS-03-986-003", bank=bank, error=error)


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
        rule = f", rule_name={outcome.rule_name}" if outcome.rule_name else ""
        phase_text = f", phase={phase}" if phase else ""
        super().__init__(
            f"terminal login checkpoint: kind={outcome.kind}{rule}{phase_text}, "
            f"credential_submissions={budget.credential_submissions}, "
            f"protocol_resubmits={budget.protocol_resubmits}, "
            f"captcha_resubmits={budget.captcha_resubmits}, reloads={budget.reloads}"
        )
        with_error_code(self, outcome.error_id)


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
        if phase is not CheckpointPhase.PRE_SUBMIT:
            raise with_error_code(LoginCheckpointBlocked(budget, outcome, phase=phase), "SYS-03-980-001")
        if budget.credential_submissions != 0:
            raise with_error_code(LoginCheckpointBlocked(budget, outcome, phase=phase), "SYS-03-980-002")
        return CheckpointPhase.POST_SUBMIT, replace(budget, credential_submissions=1)
    if outcome.kind is CheckpointKind.PROTOCOL_RESUBMIT:
        if phase is not CheckpointPhase.POST_SUBMIT:
            raise with_error_code(LoginCheckpointBlocked(budget, outcome, phase=phase), "SYS-03-981-001")
        if budget.credential_submissions != 1:
            raise with_error_code(LoginCheckpointBlocked(budget, outcome, phase=phase), "SYS-03-981-002")
        if budget.protocol_resubmits >= 1:
            raise with_error_code(LoginCheckpointBlocked(budget, outcome, phase=phase), "SYS-03-981-003")
        if not outcome.rule_name:
            raise with_error_code(LoginCheckpointBlocked(budget, outcome, phase=phase), "SYS-03-981-004")
        return CheckpointPhase.POST_SUBMIT, replace(
            budget,
            credential_submissions=2,
            protocol_resubmits=1,
        )
    if outcome.kind is CheckpointKind.CAPTCHA_RETRY:
        if phase is not CheckpointPhase.POST_SUBMIT:
            raise with_error_code(LoginCheckpointBlocked(budget, outcome, phase=phase), "SYS-03-982-001")
        if budget.credential_submissions != 1:
            raise with_error_code(LoginCheckpointBlocked(budget, outcome, phase=phase), "SYS-03-982-002")
        if budget.captcha_resubmits >= 1:
            raise with_error_code(LoginCheckpointBlocked(budget, outcome, phase=phase), "SYS-03-982-003")
        if not outcome.rule_name:
            raise with_error_code(LoginCheckpointBlocked(budget, outcome, phase=phase), "SYS-03-982-004")
        return CheckpointPhase.POST_SUBMIT, replace(
            budget,
            credential_submissions=2,
            captcha_resubmits=1,
        )
    if outcome.kind is CheckpointKind.STARTUP_RECOVERY:
        if budget.reloads >= 1:
            raise with_error_code(LoginCheckpointBlocked(budget, outcome, phase=phase), "SYS-03-983-001")
        return CheckpointPhase.PRE_SUBMIT, replace(budget, reloads=1)
    raise with_error_code(LoginCheckpointBlocked(budget, outcome, phase=phase), "SYS-03-989-002")
