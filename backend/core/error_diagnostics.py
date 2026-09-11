"""Closed diagnostic metadata. Never render exception text or untrusted values."""
from types import GetSetDescriptorType

from backend.core.login_checkpoints import (
    CheckpointPhase, CheckpointReason, CheckpointKind, CheckpointOutcome,
    LoginCheckpointBlocked, LoginInteractionRequired,
)

_SAFE_EXCEPTION_TYPES = (
    (LoginCheckpointBlocked, 'LoginCheckpointBlocked'),
    (LoginInteractionRequired, 'LoginInteractionRequired'),
    (NotImplementedError, 'NotImplementedError'), (TimeoutError, 'TimeoutError'),
    (AssertionError, 'AssertionError'), (AttributeError, 'AttributeError'),
    (IndexError, 'IndexError'), (KeyError, 'KeyError'), (OSError, 'OSError'),
    (RuntimeError, 'RuntimeError'), (TypeError, 'TypeError'),
    (ValueError, 'ValueError'), (Exception, 'Exception'),
)
_SAFE_EXCEPTION_LABELS = (
    *(label for _, label in _SAFE_EXCEPTION_TYPES),
    'PatchrightTargetClosedError', 'PatchrightTimeoutError', 'PatchrightError',
    'PlaywrightTargetClosedError', 'PlaywrightTimeoutError', 'PlaywrightError',
)


def _safe_exception_type(exc):
    for base in _safe_exception_mro(exc):
        for exception_type, label in _SAFE_EXCEPTION_TYPES:
            if base is exception_type:
                return label
    return 'Exception'

_SAFE_CHECKPOINT_KIND_LABELS = (
    (CheckpointKind.AUTHENTICATED, "authenticated"),
    (CheckpointKind.READY_FOR_CREDENTIALS, "ready_for_credentials"),
    (CheckpointKind.DISMISSIBLE_NOTICE, "dismissible_notice"),
    (CheckpointKind.DUPLICATE_SESSION, "duplicate_session"),
    (CheckpointKind.PROTOCOL_RESUBMIT, "protocol_resubmit"),
    (CheckpointKind.CAPTCHA_RETRY, "captcha_retry"),
    (CheckpointKind.STARTUP_RECOVERY, "startup_recovery"),
    (CheckpointKind.OTP_REQUIRED, "otp_required"),
    (CheckpointKind.PASSWORD_CHANGE_OPTIONAL, "password_change_optional"),
    (CheckpointKind.PASSWORD_CHANGE_REQUIRED, "password_change_required"),
    (CheckpointKind.EXPLICIT_LOGIN_ERROR, "explicit_login_error"),
    (CheckpointKind.UNKNOWN_BLOCKER, "unknown_blocker"),
)




def _safe_exception_mro(exc: BaseException) -> tuple[type, ...]:
    try:
        mro = type.__dict__['__mro__'].__get__(type(exc), type(type(exc)))
    except BaseException:
        return ()
    return mro if type(mro) is tuple else ()


def _exception_inherits(exc: BaseException, *targets: type[BaseException]) -> bool:
    return any(base is target for base in _safe_exception_mro(exc) for target in targets)


def _base_exception_state(exc: BaseException) -> dict:
    try:
        reduced = BaseException.__reduce__(exc)
    except BaseException:
        return {}
    if type(reduced) is tuple and len(reduced) >= 3 and type(reduced[2]) is dict:
        return reduced[2]
    return {}


def _base_exception_args(exc: BaseException) -> tuple:
    try:
        reduced = BaseException.__reduce__(exc)
    except BaseException:
        return ()
    if type(reduced) is tuple and len(reduced) >= 2 and type(reduced[1]) is tuple:
        return reduced[1]
    return ()


def _base_exception_context(exc: BaseException) -> BaseException | None:
    try:
        context = BaseException.__dict__["__context__"].__get__(exc, BaseException)
    except BaseException:
        return None
    return context if isinstance(context, BaseException) else None


def _safe_state_value(state: object, field: str) -> object | None:
    if type(state) is not dict or len(state) > 128:
        return None
    try:
        for key, value in dict.items(state):
            if type(key) is str and key == field:
                return value
    except BaseException:
        return None
    return None


def _safe_state_int(state: object, field: str) -> int:
    value = _safe_state_value(state, field)
    return value if type(value) is int else -1


# Diagnostic labels only: never used to select or authorize browser actions.
_SAFE_LOGIN_RULES = (
    ('cathay', ('cathay-login-announcement', 'cathay-unknown-dialog', 'cathay-unknown-modal')),
    ('ctbc', ('ctbc-duplicate-session', 'ctbc-entry-announcement', 'ctbc-otp-required', 'ctbc-unknown-dialog', 'ctbc-unknown-modal')),
    ('dbs', ('dbs-explicit-login-error-alert', 'dbs-explicit-login-error-error', 'dbs-explicit-login-error-role-alert', 'dbs-login-form-still-visible', 'dbs-otp-required-dialog', 'dbs-otp-required-modal', 'dbs-password-change-required-dialog', 'dbs-password-change-required-modal', 'dbs-unknown-dialog', 'dbs-unknown-modal')),
    ('esun', ('esun-login-form-still-visible', 'esun-otp-required-dialog', 'esun-otp-required-modal', 'esun-password-change-required-dialog', 'esun-password-change-required-modal', 'esun-unknown-dialog', 'esun-unknown-modal')),
    ('fubon', ('fubon-explicit-login-error-alert', 'fubon-explicit-login-error-error', 'fubon-explicit-login-error-role-alert', 'fubon-login-form-still-visible', 'fubon-otp-required-dialog', 'fubon-otp-required-modal', 'fubon-password-change-required-dialog', 'fubon-password-change-required-modal', 'fubon-unknown-dialog', 'fubon-unknown-modal')),
    ('hsbc', ('hsbc-explicit-login-error-alert', 'hsbc-explicit-login-error-error', 'hsbc-explicit-login-error-role-alert', 'hsbc-login-form-still-visible-password', 'hsbc-login-form-still-visible-userId', 'hsbc-login-form-still-visible-captchaInput', 'hsbc-otp-required-dialog', 'hsbc-otp-required-modal', 'hsbc-password-change-required-dialog', 'hsbc-password-change-required-modal', 'hsbc-security-notice', 'hsbc-unknown-dialog', 'hsbc-unknown-modal')),
    ('linebank', ('linebank-login-form-still-visible', 'linebank-login-success-notice', 'linebank-otp-required', 'linebank-unknown-modal')),
    ('rakuten', ('rakuten-duplicate-session', 'rakuten-otp-required', 'rakuten-referral-promo', 'rakuten-ricb-promo', 'rakuten-startup-connect-error', 'rakuten-time-deposit-promo', 'rakuten-unknown-modal')),
    ('scb', ('scb-captcha-retry-alert', 'scb-captcha-retry-error', 'scb-captcha-retry-role-alert', 'scb-duplicate-session-dialog', 'scb-duplicate-session-modal', 'scb-explicit-login-error-alert', 'scb-explicit-login-error-error', 'scb-explicit-login-error-role-alert', 'scb-login-form-still-visible', 'scb-otp-required-dialog', 'scb-otp-required-modal', 'scb-password-change-required-dialog', 'scb-password-change-required-modal', 'scb-unknown-dialog', 'scb-unknown-modal')),
    ('scsb', ('scsb-explicit-login-error-alert', 'scsb-explicit-login-error-error', 'scsb-explicit-login-error-role-alert', 'scsb-fraud-notice', 'scsb-intro-notice', 'scsb-login-form-still-visible', 'scsb-otp-required-dialog', 'scsb-otp-required-intro', 'scsb-otp-required-modal', 'scsb-password-change-required-dialog', 'scsb-password-change-required-intro', 'scsb-password-change-required-modal', 'scsb-unknown-custom-modal', 'scsb-unknown-dialog', 'scsb-unknown-modal')),
    ('sinopac', ('sinopac-captcha-retry-alert', 'sinopac-captcha-retry-error', 'sinopac-captcha-retry-role-alert', 'sinopac-explicit-login-error-alert', 'sinopac-explicit-login-error-error', 'sinopac-explicit-login-error-role-alert', 'sinopac-login-form-still-visible', 'sinopac-otp-required-dialog', 'sinopac-otp-required-modal', 'sinopac-password-change-required-dialog', 'sinopac-password-change-required-modal', 'sinopac-unknown-dialog', 'sinopac-unknown-modal')),
    ('taishin', ('taishin-login-form-still-visible', 'taishin-mandatory-password-dialog', 'taishin-mandatory-password-modal', 'taishin-otp-required-dialog', 'taishin-otp-required-modal', 'taishin-post-notice-dialog', 'taishin-post-notice-modal', 'taishin-post-protocol-dialog', 'taishin-post-protocol-modal', 'taishin-pre-duplicate-dialog', 'taishin-pre-duplicate-modal', 'taishin-unknown-dialog', 'taishin-unknown-modal')),
    ('ubot', ('ubot-login-form-still-visible', 'ubot-otp-required', 'ubot-password-change-optional', 'ubot-password-change-required', 'ubot-unknown-modal')),
)


_SAFE_LOGIN_PHASES = ((CheckpointPhase.PRE_SUBMIT, "pre_submit"), (CheckpointPhase.POST_SUBMIT, "post_submit"), (CheckpointPhase.POST_SUBMIT_SETTLE, "post_submit_settle"))
_SAFE_LOGIN_REASONS = (
    (CheckpointReason.UNSPECIFIED, 'unspecified'),
    (CheckpointReason.ACTION_CLICK_EXCEPTION, 'action_click_exception'),
    (CheckpointReason.ACTION_GUARD_DENIED, 'action_guard_denied'),
    (CheckpointReason.ACTION_GUARD_EXCEPTION, 'action_guard_exception'),
    (CheckpointReason.ACTION_NOT_UNIQUE, 'action_not_unique'),
    (CheckpointReason.AUTHENTICATION_EXCEPTION, 'authentication_exception'),
    (CheckpointReason.BANK_MISMATCH, 'bank_mismatch'),
    (CheckpointReason.COLLECT_ORIGIN_OR_DIALOG, 'collect_origin_or_dialog'),
    (CheckpointReason.DIALOG_BLOCKED, 'dialog_blocked'),
    (CheckpointReason.DIALOG_DISMISS_EXCEPTION, 'dialog_dismiss_exception'),
    (CheckpointReason.ORIGIN_INSPECTION_EXCEPTION, 'origin_inspection_exception'),
    (CheckpointReason.DUPLICATE_RULE_NAMES, 'duplicate_rule_names'),
    (CheckpointReason.FORM_CONTROLS_PRESENT, 'form_controls_present'),
    (CheckpointReason.FRAME_INSPECTION_EXCEPTION, 'frame_inspection_exception'),
    (CheckpointReason.INSPECTION_EXCEPTION, 'inspection_exception'),
    (CheckpointReason.INVALID_OUTCOME, 'invalid_outcome'),
    (CheckpointReason.INVALID_TRANSITION, 'invalid_transition'),
    (CheckpointReason.MATCHED_BLOCKER, 'matched_blocker'),
    (CheckpointReason.NATIVE_FORM_SUBMISSION, 'native_form_submission'),
    (CheckpointReason.NAVIGATION_ORIGIN, 'navigation_origin'),
    (CheckpointReason.NO_MATCHING_CHECKPOINT, 'no_matching_checkpoint'),
    (CheckpointReason.NO_PROGRESS, 'no_progress'),
    (CheckpointReason.ORIGIN_AFTER_EVALUATION, 'origin_after_evaluation'),
    (CheckpointReason.ORIGIN_AFTER_PREPARE, 'origin_after_prepare'),
    (CheckpointReason.ORIGIN_BEFORE_EVALUATION, 'origin_before_evaluation'),
    (CheckpointReason.ORIGIN_BEFORE_PREPARE, 'origin_before_prepare'),
    (CheckpointReason.ORIGIN_BEFORE_SUBMIT, 'origin_before_submit'),
    (CheckpointReason.PROGRESS_INSPECTION_EXCEPTION, 'progress_inspection_exception'),
    (CheckpointReason.PROGRESS_WAIT_EXCEPTION, 'progress_wait_exception'),
    (CheckpointReason.RULE_BUDGET_EXHAUSTED, 'rule_budget_exhausted'),
    (CheckpointReason.RULE_INSPECTION_EXCEPTION, 'rule_inspection_exception'),
    (CheckpointReason.STEP_LIMIT, 'step_limit'),
)

def _safe_login_diagnostics(bank, exception_state, outcome_state):
    phase = _safe_state_value(exception_state, "phase")
    phase_label = next((label for item, label in _SAFE_LOGIN_PHASES if phase is item), "unknown")
    reason = _safe_state_value(outcome_state, "reason")
    reason_label = next((label for item, label in _SAFE_LOGIN_REASONS
                         if reason is item or (type(reason) is str and reason == label)), "unspecified")
    rule = _safe_state_value(outcome_state, "rule_name")
    rule_label = next((name for owner, names in _SAFE_LOGIN_RULES
                       if type(bank) is str and bank == owner
                       for name in names if type(rule) is str and rule == name), "unknown")
    return f"phase={phase_label}, reason={reason_label}, rule={rule_label}"


def _safe_native_login_diagnostics(bank, exception_state):
    # Adapter classifications are not bank response codes and never authorize actions.
    if type(bank) is not str or bank != "sinopac":
        return ""
    code = _safe_state_value(exception_state, "code")
    code_label = next((label for label in (
        "captcha_invalid", "credentials_invalid", "login_failed",
    ) if type(code) is str and code == label), "unknown")
    stage = _safe_state_value(exception_state, "login_stage")
    stage_label = next((label for label in (
        "prepare_page", "input_length", "captcha_refresh", "captcha_image_wait",
        "input_inventory", "input_geometry", "input_order", "input_enabled",
        "credential_fill", "captcha_ocr", "captcha_fill", "login_button",
        "credential_submit", "post_submit_check",
    ) if type(stage) is str and stage == label), "unknown")
    return f"internal_error_code={code_label}, stage={stage_label}"



STAGES = frozenset(('init', 'credentials', 'session', 'browser_launch', 'browser_navigation',
    'login_prepare', 'login_checkpoint', 'login_field', 'login_ocr', 'login_button',
    'login_submit', 'login_postconfirm', 'login_username_continue', 'collect',
    'collect_navigation', 'collect_accounts', 'collect_transactions', 'collect_cards',
    'collect_loans', 'collect_validation', 'coverage', 'persist', 'persist_summary',
    'cleanup', 'unknown', 'login_field_national_id_wait',
    *(f'login_field_{role}_{operation}'
      for role in ('national_id', 'user_code', 'password', 'captcha')
      for operation in ('count', 'visible', 'enabled', 'click', 'triple_click',
                        'clear', 'type', 'readback', 'length'))))


def instance_stage(crawler, fallback='unknown'):
    state = {}
    try:
        cls = type(crawler)
        for owner in type.__dict__['__mro__'].__get__(cls, type(cls)):
            namespace = type.__dict__['__dict__'].__get__(owner, type(owner))
            descriptor = namespace.get('__dict__')
            if descriptor is not None:
                if (type(descriptor) is GetSetDescriptorType
                        and descriptor.__objclass__ is owner
                        and descriptor.__name__ == '__dict__'):
                    state = descriptor.__get__(crawler, cls)
                break
    except BaseException:
        pass
    stage = _safe_state_value(state, '_diagnostic_stage')
    return stage if type(stage) is str and stage in STAGES else make_diagnostics(fallback)['stage']


def make_diagnostics(stage='unknown'):
    stage = stage if type(stage) is str and stage in STAGES else 'unknown'
    return {'stage': stage, 'code': 'crawler_failed' if stage == 'unknown' else stage + '_failed'}


def validate_diagnostics(value, *, bank=None, guards=frozenset()):
    stage = _safe_state_value(value, 'stage')
    result = make_diagnostics(stage)
    code = _safe_state_value(value, 'code')
    if type(code) is not str or code != result['code']:
        return make_diagnostics()
    # Only exact builtin containers/labels cross the boundary.
    allowed = {
        'exception_type': _SAFE_EXCEPTION_LABELS,
        'underlying_exception_type': _SAFE_EXCEPTION_LABELS,
        'kind': tuple(label for _, label in _SAFE_CHECKPOINT_KIND_LABELS),
        'phase': (*[label for _, label in _SAFE_LOGIN_PHASES], 'unknown'),
        'reason': tuple(label for _, label in _SAFE_LOGIN_REASONS),
        'rule': tuple(name for owner, names in _SAFE_LOGIN_RULES
                      if type(bank) is str and bank == owner for name in names),
        'native_code': ('captcha_invalid', 'credentials_invalid', 'login_failed'),
        'native_stage': ('prepare_page', 'input_length', 'captcha_refresh', 'captcha_image_wait',
            'input_inventory', 'input_geometry', 'input_order', 'input_enabled', 'credential_fill',
            'captcha_ocr', 'captcha_fill', 'login_button', 'credential_submit', 'post_submit_check'),
        'collect_code': ('collect_timeout', 'collect_inventory', 'collect_range', 'collect_transport',
            'collect_navigation', 'collect_validation', 'collect_history', 'collect_adapter',
            'collect_checkpoint', 'collect_persistence', 'collect_external', 'collect_contract'),
    }
    if type(guards) is frozenset and len(guards) <= 128 and all(type(x) is str and len(x) <= 128 for x in guards):
        allowed['guard'] = guards
    for key, labels in allowed.items():
        item = _safe_state_value(value, key)
        if key.startswith('native_') and not (type(bank) is str and bank == 'sinopac'):
            continue
        if type(item) is str and item in labels:
            result[key] = item
    return result


def annotate_failure(exc, stage='unknown', diagnostics=None, *, bank=None, guards=frozenset()):
    existing = _safe_state_value(_base_exception_state(exc), 'error_diagnostics')
    value = validate_diagnostics(existing, bank=bank, guards=guards)
    if existing is None:
        value = validate_diagnostics(diagnostics, bank=bank, guards=guards) if diagnostics is not None else make_diagnostics(stage)
    try:
        state = BaseException.__dict__['__dict__'].__get__(exc, BaseException)
        if type(state) is dict and len(state) <= 128 and all(type(key) is str for key in state):
            state['error_diagnostics'] = value
    except BaseException:
        pass
    return exc


def result_failure(result, *, bank=None, guards=frozenset()):
    # Control flow is independent of the capped diagnostic extractor. Reject
    # oversized/foreign-key containers before callers can read data or persist.
    if type(result) is dict and len(result) <= 128 and all(type(key) is str for key in result):
        error = dict.get(result, 'error')
        data = dict.get(result, 'data')
        if (error is None or (type(error) is str and error == '')) and (data is None or type(data) is dict):
            return None
    return annotate_failure(RuntimeError('crawler_failed'), diagnostics=_safe_state_value(result, 'error_diagnostics'), bank=bank, guards=guards)


def format_failure(exc, *, bank=None, guards=frozenset(), fallback='unknown'):
    value = _safe_state_value(_base_exception_state(exc), 'error_diagnostics')
    value = validate_diagnostics(value, bank=bank, guards=guards) if value is not None else make_diagnostics(fallback)
    return 'sync_failed:' + _safe_exception_type(exc) + ''.join(';' + key + '=' + item for key, item in value.items())


def _safe_collect_guard(exc: BaseException, allowlist: object) -> str | None:
    """Return only a code-owned static guard, including suppressed contexts."""
    if type(allowlist) is not frozenset or len(allowlist) > 128 or any(
        type(value) is not str or len(value) > 128 for value in allowlist
    ):
        return None
    guard = None
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(seen) < 16:
        seen.add(id(current))
        args = _base_exception_args(current)
        if (
            _exception_inherits(current, RuntimeError)
            and len(args) == 1
            and type(args[0]) is str
            and len(args[0]) <= 128
            and args[0] in allowlist
        ):
            guard = args[0]
        current = _base_exception_context(current)
    return guard


def _class_collect_guard_allowlist(crawler: object, *, crawler_class=None) -> frozenset[str]:
    """Read the exact crawler class namespace without invoking descriptors."""
    try:
        cls = type(crawler) if crawler_class is None else crawler_class
        namespace = type.__dict__["__dict__"].__get__(cls, type(cls))
        if type(namespace) is not type(type.__dict__) or len(namespace) > 128:
            return frozenset()
        allowlist = next(
            (
                value
                for key, value in namespace.items()
                if type(key) is str and key == "SAFE_COLLECT_GUARDS"
            ),
            None,
        )
    except BaseException:
        return frozenset()
    if type(allowlist) is not frozenset or len(allowlist) > 128 or any(
        type(value) is not str or len(value) > 128 for value in allowlist
    ):
        return frozenset()
    return allowlist


def exception_diagnostics(exc, crawler, *, exception_type=_safe_exception_type):
    state = _base_exception_state(exc)
    outcome = _safe_state_value(state, 'outcome')
    try:
        outcome_state = object.__getattribute__(outcome, '__dict__') if type(outcome) is CheckpointOutcome else {}
    except BaseException:
        outcome_state = {}
    value = make_diagnostics(instance_stage(crawler))
    phase = _safe_state_value(state, 'phase')
    value['phase'] = next((label for item, label in _SAFE_LOGIN_PHASES if phase is item), 'unknown')
    kind = _safe_state_value(outcome_state, 'kind')
    value['kind'] = next((label for item, label in _SAFE_CHECKPOINT_KIND_LABELS if kind is item), 'unknown_blocker')
    reason = _safe_state_value(outcome_state, 'reason')
    value['reason'] = next((label for item, label in _SAFE_LOGIN_REASONS if reason is item or (type(reason) is str and reason == label)), 'unspecified')
    value['rule'] = _safe_state_value(outcome_state, 'rule_name')
    value['native_code'] = _safe_state_value(state, 'code')
    value['native_stage'] = _safe_state_value(state, 'login_stage')
    guards = _class_collect_guard_allowlist(crawler)
    value['guard'] = _safe_collect_guard(exc, guards)
    value['exception_type'] = exception_type(exc)
    context = BaseException.__dict__['__cause__'].__get__(exc, BaseException)
    if context is None:
        context = _base_exception_context(exc)
    if context is not None:
        value['underlying_exception_type'] = exception_type(context)
    value = validate_diagnostics(value, bank=crawler.name, guards=guards)
    return value
