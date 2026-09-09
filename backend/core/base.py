#!/usr/bin/env python3
"""Abstract base class for bank crawlers.

銀行爬蟲抽象基類。

每家銀行繼承 BankCrawler，實作 login() 與 collect()。
統一用 Scrapling StealthyFetcher (headful) + user_data_dir session 持久化。
攔截式抓取：讓銀行自己的前端打 API，攔 response 拿 JSON，不逆向加密。
"""
from __future__ import annotations

import base64
import contextlib
import time
import json
import math
import os
import re
import stat
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, fields as dataclass_fields, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, NotRequired, Required, TypedDict
from urllib.parse import urlparse

from scrapling.fetchers import StealthyFetcher
from patchright._impl._errors import TargetClosedError as PatchrightTargetClosedError
from patchright.sync_api import Error as PatchrightError, TimeoutError as PatchrightTimeoutError
from playwright._impl._errors import TargetClosedError as PlaywrightTargetClosedError
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointReason,
    CheckpointOutcome,
    CheckpointPhase,
    DEFAULT_ACTION_SELECTOR,
    LoginBudget,
    LoginCheckpointBlocked,
    LoginCheckpointRule,
    LoginInteractionRequired,
    evaluate_login_checkpoint,
    reduce_login_checkpoint,
    validate_login_checkpoint_outcome,
)


_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_SAFE_EXCEPTION_TYPES = (
    (PatchrightTargetClosedError, "PatchrightTargetClosedError"),
    (PatchrightTimeoutError, "PatchrightTimeoutError"),
    (PatchrightError, "PatchrightError"),
    (PlaywrightTargetClosedError, "PlaywrightTargetClosedError"),
    (PlaywrightTimeoutError, "PlaywrightTimeoutError"),
    (PlaywrightError, "PlaywrightError"),
    (LoginCheckpointBlocked, "LoginCheckpointBlocked"),
    (LoginInteractionRequired, "LoginInteractionRequired"),
    (NotImplementedError, "NotImplementedError"),
    (TimeoutError, "TimeoutError"),
    (AssertionError, "AssertionError"),
    (AttributeError, "AttributeError"),
    (IndexError, "IndexError"),
    (KeyError, "KeyError"),
    (OSError, "OSError"),
    (RuntimeError, "RuntimeError"),
    (TypeError, "TypeError"),
    (ValueError, "ValueError"),
    (Exception, "Exception"),
)


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
        mro = type.__getattribute__(type(exc), "__mro__")
    except BaseException:
        return ()
    return mro if type(mro) is tuple else ()


def _exception_inherits(exc: BaseException, *targets: type[BaseException]) -> bool:
    return any(base is target for base in _safe_exception_mro(exc) for target in targets)


def _safe_exception_type(exc: BaseException) -> str:
    for base in _safe_exception_mro(exc):
        for exception_type, label in _SAFE_EXCEPTION_TYPES:
            if base is exception_type:
                return label
    return "Exception"


def _safe_checkpoint_kind_label(kind: object) -> str:
    return next(
        (
            label
            for checkpoint_kind, label in _SAFE_CHECKPOINT_KIND_LABELS
            if kind is checkpoint_kind
        ),
        "unknown_blocker",
    )


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
    if type(state) is not dict:
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


def _bank_collect_failure_code(function: str) -> str:
    if "deadline" in function or "timeout" in function:
        return "collect_timeout"
    if "inventory" in function:
        return "collect_inventory"
    if "history_range" in function or "history_window" in function:
        return "collect_range"
    if "transport" in function or "fetch" in function or "response" in function:
        return "collect_transport"
    if any(
        marker in function
        for marker in ("frame", "open_", "query", "form", "dialog", "click")
    ):
        return "collect_navigation"
    if any(
        marker in function
        for marker in (
            "validate",
            "normalize",
            "amount",
            "date",
            "row",
            "result",
            "hit",
            "canonical",
            "strict",
            "options",
        )
    ):
        return "collect_validation"
    if any(
        marker in function
        for marker in ("history", "transaction", "details", "loan")
    ):
        return "collect_history"
    return "collect_adapter"


def _safe_collect_failure_code(exc: BaseException) -> str:
    """Map trusted application frames to one fixed, low-cardinality class."""
    if _exception_inherits(exc, LoginCheckpointBlocked, LoginInteractionRequired):
        return "collect_checkpoint"

    code = "collect_external"
    try:
        tb = BaseException.__dict__["__traceback__"].__get__(exc, BaseException)
    except BaseException:
        return code
    while tb is not None:
        frame = tb.tb_frame
        try:
            relative = Path(frame.f_code.co_filename).resolve().relative_to(_BACKEND_ROOT)
        except (OSError, RuntimeError, ValueError):
            tb = tb.tb_next
            continue
        parts = relative.parts
        function = frame.f_code.co_name
        if not (
            parts
            and all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts)
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", function)
        ):
            tb = tb.tb_next
            continue
        if parts[0] == "banks":
            code = _bank_collect_failure_code(function)
        elif parts[:2] == ("core", "login_checkpoints.py"):
            code = "collect_checkpoint"
        elif parts[:2] == ("core", "persist"):
            code = "collect_persistence"
        else:
            code = "collect_contract"
        tb = tb.tb_next
    return code


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


def _class_collect_guard_allowlist(crawler: object) -> frozenset[str]:
    """Read the exact crawler class namespace without invoking descriptors."""
    try:
        namespace = type.__dict__["__dict__"].__get__(
            type(crawler), type(type(crawler))
        )
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


def write_private_json(path: Path, payload: dict) -> None:
    """Atomically replace a private JSON file without following links."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = os.lstat(path)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(current.st_mode):
            raise OSError("private JSON target must be a regular file")
        if current.st_nlink != 1:
            raise RuntimeError("private JSON target must be a single-link regular file")
        path.chmod(0o600, follow_symlinks=False)

    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError("private JSON temporary must be a single-link regular file")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary).replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            Path(temporary).unlink()


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PROJECT_ROOT / "data"

MACOS_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
MACOS_SPOOF_JS = r"""
(() => {
    try {
        Object.defineProperty(navigator, 'platform', {
            get: () => 'MacIntel',
            configurable: true,
        });
    } catch (e) {}
    try {
        if (navigator.userAgentData) {
            const orig = navigator.userAgentData;
            const fake = {
                brands: orig.brands,
                mobile: orig.mobile,
                platform: 'macOS',
                getHighEntropyValues: orig.getHighEntropyValues
                    ? (hints) => orig.getHighEntropyValues.call(orig, hints).then(v => ({
                        ...v, platform: 'macOS', platformVersion: '15.0.0',
                        architecture: 'x86', bitness: '64', model: '',
                    }))
                    : undefined,
                toJSON: () => ({ brands: orig.brands, mobile: orig.mobile, platform: 'macOS' }),
            };
            Object.defineProperty(navigator, 'userAgentData', {
                get: () => fake,
                configurable: true,
            });
        }
    } catch (e) {}
    try {
        Object.defineProperty(navigator, 'appVersion', {
            get: () => '5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            configurable: true,
        });
    } catch (e) {}
})();
"""


@dataclass
class ApiHit:
    """一次攔截到的 API 呼叫（request + response 配對）。"""
    url: str
    method: str
    status: int
    req_body: Any = None
    resp_json: Any = None
    content_type: str = ""
    raw_url: str = ""
    redirected: bool = False
    body_size: int | None = None
    request_sequence: int = 0
    main_frame_request: bool = False
    request_frame_url: str = ""
    request_frame: Any = None

    @property
    def endpoint(self) -> str:
        return self.url.split("?")[0].rsplit("/", 1)[-1]


_SINOPAC_LOGIN_RESPONSE_URL = "https://mma.sinopac.com/ws/member/login/ws_validatecaptcha.ashx"
_HISTORY_OBSERVER_URLS = {
    "ubot.com.tw": "https://www.ubot.com.tw/MyBank/IBKB010102",
    "taishinbank.com.tw": "https://my.taishinbank.com.tw/TIBNetBank/svc/web1/rb0102/query",
}


class _HistoryBodyObserver:
    """Read-only CDP size proof; bounds admitted decoded bytes, not browser RSS."""

    LIMIT: int = 5_000_000
    MAX_RECORDS: int = 64
    WAIT_SECONDS: float = 10

    def __init__(self, page, url):
        self.page, self.url = page, url
        self.records = {}
        self.native_requests = []
        self.total = 0
        self.bad = False
        self.session = page.context.new_cdp_session(page)
        self.handlers = {"Network.requestWillBeSent": self._request}
        for name in ("responseReceived", "dataReceived", "loadingFinished", "loadingFailed"):
            self.handlers["Network." + name] = lambda event, kind=name: self._event(kind, event)
        try:
            for name, handler in self.handlers.items():
                self.session.on(name, handler)
            self.session.send("Network.enable", {"maxPostDataSize": 16_384})
        except Exception:
            self.close()
            raise

    def close(self):
        self.bad = True
        for name, handler in self.handlers.items():
            with contextlib.suppress(Exception):
                self.session.remove_listener(name, handler)
        with contextlib.suppress(Exception):
            self.session.detach()
        self.records.clear()
        self.native_requests.clear()

    def _request(self, event):
        request_id = event.get("requestId")
        if request_id in self.records:
            self.records[request_id]["bad"] = True  # Redirect/reused ID.
            return
        request = event.get("request", {})
        if request.get("url") != self.url:
            return
        if len(self.records) >= self.MAX_RECORDS:
            self.bad = True
            return
        post = request.get("postData")
        if (request.get("method") != "POST" or not isinstance(post, str)
                or len(post.encode("utf-8")) > 16_384 or not event.get("frameId")):
            self.bad = True
            return
        self.records[request_id] = {
            "key": (request["url"], request["method"], post, event["frameId"]),
            "loader": event.get("loaderId"), "document_url": event.get("documentURL", ""),
            "bytes": 0, "done": False, "bad": "redirectResponse" in event,
            "response": False, "used": False,
        }

    def _event(self, kind, event):
        record = self.records.get(event.get("requestId"))
        if record is None:
            return
        if kind == "dataReceived":
            size = event.get("dataLength")
            if type(size) is not int or size < 0 or record["done"] or "data" in event:
                record["bad"] = True
                return
            record["bytes"] = min(self.LIMIT + 1, record["bytes"] + size)
            self.total = min(self.LIMIT + 1, self.total + size)
            self.bad |= self.total > self.LIMIT
        elif kind == "loadingFinished":
            record["bad"] |= record["done"]
            record["done"] = True
        elif kind == "loadingFailed":
            record["bad"] = True
        else:
            response = event.get("response", {})
            record["response"] = (
                response.get("status") == 200 and response.get("url") == self.url
                and not response.get("fromServiceWorker")
                and not response.get("fromDiskCache")
                and not response.get("fromPrefetchCache")
                and bool(record["loader"]) and event.get("loaderId") == record["loader"]
                and event.get("frameId") == record["key"][3]
            )
            record["bad"] |= not record["response"]

    def _document(self, frame, frame_url):
        """Bind a native Frame to its current CDP document, never by URL alone."""
        if frame is None:
            return None
        tree = self.session.send("Page.getFrameTree")["frameTree"]
        # Read the native URL AFTER CDP dispatch: SPA routing may run during send.
        current_url = frame.url
        if (not any(native is frame for native in self.page.frames)
                or urlparse(current_url)[:2] != urlparse(frame_url)[:2]):
            return None
        if frame is self.page.main_frame:
            matches = [tree["frame"]]
        else:
            native = [f for f in self.page.frames if f.url == current_url]
            if len(native) != 1 or native[0] is not frame:
                return None
            pending, matches = [tree], []
            while pending:
                node = pending.pop()
                if node["frame"]["url"] + node["frame"].get("urlFragment", "") == current_url:
                    matches.append(node["frame"])
                pending.extend(node.get("childFrames", []))
        if (len(matches) != 1
                or matches[0]["url"] + matches[0].get("urlFragment", "") != current_url
                or not matches[0].get("loaderId")):
            return None
        return matches[0]["id"], matches[0]["loaderId"]

    def read(self, resp, frame, frame_url, remaining, minimum, admit=None):
        req = resp.request
        if (self.bad or resp.status != 200 or req.url != self.url or resp.url != self.url
                or req.method != "POST" or req.frame is not frame
                or getattr(req, "redirected_from", None) is not None
                or getattr(req, "redirected_to", None) is not None):
            return None
        if (any(native is req for native in self.native_requests)
                or len(self.native_requests) >= self.MAX_RECORDS):
            return None
        self.native_requests.append(req)
        document = self._document(frame, frame_url)
        if document is None:
            return None
        key = (req.url, req.method, req.post_data, document[0])
        deadline = time.monotonic() + min(30, self.WAIT_SECONDS)
        while not self.bad:
            matches = [(rid, r) for rid, r in self.records.items() if r["key"] == key and not r["used"]]
            if len(matches) > 1:
                return None
            if matches:
                request_id, record = matches[0]
                if (record["bad"] or record.get("native_request", req) is not req
                        or record["loader"] != document[1]
                        or urlparse(record["document_url"])[:2] != urlparse(frame_url)[:2]):
                    return None
                record["native_request"] = req
                if record["done"]:
                    available = remaining() if callable(remaining) else remaining
                    if not record["response"] or not minimum <= record["bytes"] <= available:
                        return None
                    if admit is not None and not admit(record["bytes"]):
                        return None
                    if (self._document(frame, frame_url) != document or self.bad or record["bad"]
                            or sum(r["key"] == key and not r["used"] for r in self.records.values()) != 1):
                        return None
                    # Direct observer buffer read cannot enter Patchright's
                    # Network.loadNetworkResource replay fallback (even with CL).
                    result = self.session.send("Network.getResponseBody", {"requestId": request_id})
                    body = (base64.b64decode(result["body"], validate=True)
                            if result.get("base64Encoded") else result["body"].encode("utf-8"))
                    if (self._document(frame, frame_url) == document
                            and not self.bad and not record["bad"] and len(body) == record["bytes"]
                            and sum(r["key"] == key and not r["used"] for r in self.records.values()) == 1):
                        record["used"] = True
                        return body
                    return None
            if time.monotonic() >= deadline:
                return None
            self.page.wait_for_timeout(20)
        return None


class ResponseCollector:
    """掛在 Playwright page 上，攔截所有 XHR/fetch 的 request+response。"""

    SKIP_RE = re.compile(
        r"(\.js|\.css|\.ico|\.png|\.jpg|\.svg|\.woff2?|\.gif)(\?|$)"
        r"|/locales/|google|gtm|omtrdc|doubleclick|analytics|datalayer|celebrus|faro|/assets/",
        re.I,
    )

    def __init__(self, host_filter: str = ""):
        self.hits: list[ApiHit] = []
        self.host_filter = host_filter
        self.auth_token: str = ""  # 攔到的 Authorization 標頭（如 'Bearer eyJ...'），給直接 fetch 用
        self.auth_token_url: str = ""
        self.auth_token_events: list[dict[str, Any]] = []
        self.hsbc_inventory_bytes = 0
        self._taishin_json_bytes = 0
        self._taishin_json_responses = 0
        self._auth_event_sequence = 0
        self._request_sequence = 0
        self._requests: dict[int, int] = {}
        self._request_main_frame: dict[int, bool] = {}
        self._request_frame_urls: dict[int, str] = {}
        self._request_frames: dict[int, Any] = {}
        self._issued_endpoint_counts: dict[str, int] = {}
        self._auth_requests: dict[int, dict[str, Any]] = {}
        self._latest_auth_request_sequence = 0
        self._response_handler = self._on_response
        self._request_handler = self._on_request
        self._request_failed_handler = self._on_request_failed
        self._history_observer = None

    def attach(self, page):
        observer_url = (
            _SINOPAC_LOGIN_RESPONSE_URL if self.host_filter == "sinopac.com"
            else _HISTORY_OBSERVER_URLS.get(self.host_filter)
        )
        if observer_url is not None and self._history_observer is None:
            with contextlib.suppress(Exception):
                self._history_observer = _HistoryBodyObserver(
                    page, observer_url
                )
                if self.host_filter == "sinopac.com":
                    self._history_observer.LIMIT = 16_384
                    self._history_observer.MAX_RECORDS = 4
                    self._history_observer.WAIT_SECONDS = 0.25
        page.on("request", self._request_handler)
        page.on("requestfailed", self._request_failed_handler)
        page.on("response", self._response_handler)

    def detach(self, page) -> None:
        if self._history_observer is not None:
            self._history_observer.close()
            self._history_observer = None
        remove = getattr(page, "remove_listener", None)
        if callable(remove):
            remove("request", self._request_handler)
            remove("requestfailed", self._request_failed_handler)
            remove("response", self._response_handler)

    @property
    def request_sequence(self) -> int:
        return self._request_sequence

    def issued_count(self, endpoint: str) -> int:
        return self._issued_endpoint_counts.get(endpoint, 0)

    def _on_request(self, req) -> None:
        try:
            if self.SKIP_RE.search(req.url):
                return
            parsed = urlparse(req.url)
            expected = self.host_filter.lower().strip(".")
            hostname = (parsed.hostname or "").lower()
            canonical_path = "/".join(
                segment.split(";", 1)[0] for segment in parsed.path.split("/")
            )
            if self.host_filter and (
                parsed.scheme.lower() != "https"
                or (hostname != expected and not hostname.endswith("." + expected))
            ):
                return
            rakuten_metadata_only = self.host_filter == "rakuten-bank.com.tw"
            esun_history_metadata_only = (
                self.host_filter == "esunbank.com.tw"
                and hostname == "ebank.esunbank.com.tw"
                and canonical_path in {
                    "/fco/fao01002/FAO01002.faces",
                    "/fao/fao01002/FAO01002_Home.faces",
                }
                and req.method == "POST"
            )
            metadata_only = rakuten_metadata_only or esun_history_metadata_only
            if rakuten_metadata_only and (
                parsed.netloc != "www.rakuten-bank.com.tw"
                or parsed.path != "/ixtein/adapters/ebank/txns/channel-ctw/CTWQU0001/011"
                or parsed.params
                or parsed.query
                or parsed.fragment
                or req.method != "POST"
            ):
                return
            frame = getattr(req, "frame", None)
            page = getattr(frame, "page", None)
            main_frame_request = (
                frame is not None and frame is getattr(page, "main_frame", None)
            )
            frame_url = getattr(frame, "url", "")
            frame_url = frame_url if isinstance(frame_url, str) else ""
            self._request_sequence += 1
            self._requests[id(req)] = self._request_sequence
            self._request_main_frame[id(req)] = main_frame_request
            self._request_frame_urls[id(req)] = "" if metadata_only else frame_url
            self._request_frames[id(req)] = None if metadata_only else frame
            # Preserve basename callers; full paths distinguish same-named APIs.
            for endpoint in {parsed.path, parsed.path.rsplit("/", 1)[-1]}:
                self._issued_endpoint_counts[endpoint] = (
                    self._issued_endpoint_counts.get(endpoint, 0) + 1
                )
            if metadata_only:
                return
            auth = req.headers.get("authorization", "")
            if re.fullmatch(r"Bearer [^\s\r\n]+", auth) is None:
                return
            self._auth_event_sequence += 1
            self._auth_requests[id(req)] = {
                "token": auth,
                "url": req.url,
                "redirected": getattr(req, "redirected_from", None) is not None,
                "sequence": self._auth_event_sequence,
            }
        except Exception:
            return

    def _on_request_failed(self, req) -> None:
        self._requests.pop(id(req), None)
        self._request_main_frame.pop(id(req), None)
        self._request_frame_urls.pop(id(req), None)
        self._request_frames.pop(id(req), None)
        self._auth_requests.pop(id(req), None)

    def _on_response(self, resp):
        try:
            url = resp.url
            if self.SKIP_RE.search(url):
                return
            parsed = urlparse(url)
            hostname = (parsed.hostname or "").lower()
            canonical_path = "/".join(
                segment.split(";", 1)[0] for segment in parsed.path.split("/")
            )
            if self.host_filter:
                expected = self.host_filter.lower().strip(".")
                if (
                    parsed.scheme.lower() != "https"
                    or (hostname != expected and not hostname.endswith("." + expected))
                ):
                    return
            req = resp.request
            rakuten_metadata_only = self.host_filter == "rakuten-bank.com.tw"
            esun_history_metadata_only = (
                self.host_filter == "esunbank.com.tw"
                and parsed.hostname == "ebank.esunbank.com.tw"
                and canonical_path in {
                    "/fco/fao01002/FAO01002.faces",
                    "/fao/fao01002/FAO01002_Home.faces",
                }
                and req.method == "POST"
            )
            metadata_only = rakuten_metadata_only or esun_history_metadata_only
            if rakuten_metadata_only and (
                parsed.netloc != "www.rakuten-bank.com.tw"
                or parsed.path != "/ixtein/adapters/ebank/txns/channel-ctw/CTWQU0001/011"
                or parsed.params
                or parsed.query
                or parsed.fragment
                or req.method != "POST"
            ):
                self._on_request_failed(req)
                return
            request_sequence = self._requests.pop(id(req), 0)
            main_frame_request = self._request_main_frame.pop(id(req), False)
            request_frame_url = self._request_frame_urls.pop(id(req), "")
            request_frame = self._request_frames.pop(id(req), None)
            auth_event = self._auth_requests.pop(id(req), None)
            ct = resp.headers.get("content-type", "")
            content_length = resp.headers.get("content-length", "")
            content_encoding = resp.headers.get("content-encoding", "")
            is_ubot_history = (
                self.host_filter == "ubot.com.tw"
                and parsed.hostname == "www.ubot.com.tw"
                and parsed.path == "/MyBank/IBKB010102"
            )
            is_taishin_history = (
                self.host_filter == "taishinbank.com.tw"
                and parsed.hostname == "my.taishinbank.com.tw"
                and parsed.path == "/TIBNetBank/svc/web1/rb0102/query"
            )
            is_taishin_projected_api = (
                self.host_filter == "taishinbank.com.tw"
                and parsed.hostname == "my.taishinbank.com.tw"
                and parsed.path in {
                    "/TIBNetBank/svc/web1/rb0100/query",
                    "/TIBNetBank/svc/web/common/qryTaishinPoint",
                    "/TIBNetBank/svc/web4/rb0708rwd/qryRealTime",
                    "/TIBNetBank/svc/web4/rb0708rwd/doXTPA",
                }
            )
            if (
                self.host_filter == "taishinbank.com.tw"
                and not (is_taishin_history or is_taishin_projected_api)
            ):
                return
            is_taishin_json = is_taishin_history or is_taishin_projected_api
            if (
                is_taishin_json
                and "json" in ct
                and (
                    self._taishin_json_bytes >= 5_000_000
                    or self._taishin_json_responses >= 64
                )
            ):
                return
            is_bounded_json = (
                (
                    self.host_filter == "card.hsbc.com.tw"
                    and parsed.path in {
                        "/ibk-bff/api/v1/cards", "/ibk-bff/api/v1/cards/suspend",
                    }
                )
                or is_ubot_history
                or is_taishin_history
                or is_taishin_projected_api
                or (
                    self.host_filter == "sinopac.com"
                    and parsed.hostname == "mma.sinopac.com"
                    and parsed.path in {
                        "/ws/bank/transdetail/ws_debitacct.ashx",
                        "/ws/bank/transdetail/ws_transdetailMerge.ashx",
                    }
                )
            )
            body_size = int(content_length) if content_length.isdigit() else None
            auth = "" if metadata_only else req.headers.get("authorization", "")
            # 只按 request 發出順序更新 token；response 亂序不得降回舊 token。
            if (
                auth_event is not None
                and auth == auth_event["token"]
                and 200 <= resp.status < 300
                and re.fullmatch(r"Bearer [^\s\r\n]+", auth)
            ):
                event = {
                    "token": auth_event["token"],
                    "url": url,
                    "redirected": auth_event["redirected"],
                    "sequence": auth_event["sequence"],
                }
                self.auth_token_events.append(event)
                if event["sequence"] > self._latest_auth_request_sequence:
                    self._latest_auth_request_sequence = event["sequence"]
                    self.auth_token = event["token"]
                    self.auth_token_url = url
            is_data = ("json" in ct) or (req.method == "POST") or bool(auth)
            if not is_data:
                return
            req_body = None
            is_sinopac_login = (
                self.host_filter == "sinopac.com"
                and canonical_path == "/ws/member/login/ws_validatecaptcha.ashx"
            )
            try:
                pd = None if metadata_only or is_sinopac_login else req.post_data
                if pd:
                    if is_bounded_json:
                        if len(pd.encode("utf-8")) > 16_384:
                            req_body = {"__oversize__": True}
                        elif is_ubot_history:
                            # Preserve duplicate fields for the history validator.
                            req_body = pd
                        else:
                            try:
                                parsed_body = json.loads(pd)
                                req_body = (
                                    {"__json_null__": True}
                                    if parsed_body is None
                                    else parsed_body
                                )
                            except Exception:
                                req_body = pd
                    else:
                        try:
                            req_body = json.loads(pd)
                        except Exception:
                            req_body = pd[:500]
            except Exception:
                pass
            resp_json = None
            if is_sinopac_login:
                # Never use Response.json/body: Patchright may replay a bank POST.
                req_body = None
                capture = self._sinopac_capture_diagnostics = {
                    "request_sequence": request_sequence, "status": "request_binding_rejected",
                    "exception_type": None,
                }
                if (url == _SINOPAC_LOGIN_RESPONSE_URL
                        and main_frame_request and request_sequence > 0
                        and request_frame_url == "https://mma.sinopac.com/MemberPortal/Member/MMALogin.aspx"):
                    capture["status"] = "observer_unavailable"
                if (url == _SINOPAC_LOGIN_RESPONSE_URL
                        and main_frame_request and request_sequence > 0
                        and request_frame_url == "https://mma.sinopac.com/MemberPortal/Member/MMALogin.aspx"
                        and self._history_observer is not None):
                    capture["status"] = "cdp_read_rejected"
                    try:
                        raw_body = self._history_observer.read(
                            resp, request_frame, request_frame_url, 16_384, 1,
                        )
                        if raw_body is not None:
                            capture["status"] = "projection_rejected"
                            body_size = len(raw_body)
                            payload = json.loads(raw_body)
                            row = payload[0] if type(payload) is list and len(payload) == 1 else None
                            header = _safe_state_value(row, "Header")
                            message = _safe_state_value(row, "Message")
                            if (type(header) is str and 0 < len(header) <= 64
                                    and type(message) is str and len(message) <= 4096):
                                resp_json = [{"Header": header, "Message": message}]
                                capture["status"] = "captured"
                    except Exception as exc:
                        capture["exception_type"] = _safe_exception_type(exc)
            elif "json" in ct and not metadata_only:
                if is_bounded_json:
                    minimum_size = 64 if is_ubot_history else 0
                    if is_ubot_history or is_taishin_history:
                        # Every history body requires native CDP proof. In particular,
                        # identity + Content-Length must not enter resp.body()'s replay fallback.
                        identity_encoding = content_encoding.lower() in {"", "identity"}
                        declared_ok = not content_length or (
                            body_size is not None
                            and (minimum_size if identity_encoding else 0) <= body_size <= 5_000_000
                        )
                        if self._history_observer is not None and request_sequence > 0 and declared_ok:
                            with contextlib.suppress(Exception):
                                remaining = lambda: 5_000_000 - (self._taishin_json_bytes if is_taishin_json else 0)
                                raw_body = self._history_observer.read(
                                    resp, request_frame, request_frame_url, remaining, minimum_size,
                                    self._reserve_taishin_body if is_taishin_json else None,
                                )
                                if raw_body is not None:
                                    declared_size = body_size
                                    body_size = len(raw_body)
                                    if not content_length or not identity_encoding or body_size == declared_size:
                                        resp_json = json.loads(raw_body)
                    elif (
                        content_encoding.lower() in {"", "identity"}
                        and body_size is not None
                        and minimum_size <= body_size <= 5_000_000
                    ):
                        with contextlib.suppress(Exception):
                            declared_size = body_size
                            raw_body = resp.body()
                            body_size = len(raw_body)
                            within_taishin_budget = True
                            if is_taishin_json:
                                self._taishin_json_responses += 1
                                total = self._taishin_json_bytes + body_size
                                within_taishin_budget = (
                                    self._taishin_json_responses <= 64
                                    and total <= 5_000_000
                                )
                                self._taishin_json_bytes = min(total, 5_000_000)
                            if body_size <= 5_000_000 and within_taishin_budget and (
                                not (
                                    is_ubot_history
                                    or is_taishin_history
                                    or is_taishin_projected_api
                                )
                                or body_size == declared_size
                            ):
                                resp_json = json.loads(raw_body)
                else:
                    with contextlib.suppress(Exception):
                        resp_json = resp.json()
            stored_url = (
                "https://www.rakuten-bank.com.tw/ixtein/adapters/ebank/txns/"
                "channel-ctw/CTWQU0001/011"
                if rakuten_metadata_only
                else f"https://ebank.esunbank.com.tw{canonical_path}"
                if esun_history_metadata_only
                else url.split("?")[0]
            )
            self.hits.append(ApiHit(
                url=stored_url, method=req.method, status=resp.status,
                req_body=req_body, resp_json=resp_json, content_type=ct,
                raw_url=stored_url if metadata_only else url,
                redirected=getattr(req, "redirected_from", None) is not None,
                body_size=body_size,
                request_sequence=request_sequence,
                main_frame_request=main_frame_request,
                request_frame_url=request_frame_url,
                request_frame=request_frame,
            ))
        except Exception:
            pass

    def _reserve_taishin_body(self, size):
        if self._taishin_json_responses >= 64 or self._taishin_json_bytes + size > 5_000_000:
            return False
        # Reserve before the CDP call can dispatch another response callback.
        self._taishin_json_responses += 1
        self._taishin_json_bytes += size
        return True

    def by_endpoint(self, name: str) -> list[ApiHit]:
        return [h for h in self.hits if h.endpoint == name and h.resp_json is not None]

    def latest(self, name: str) -> ApiHit | None:
        hits = self.by_endpoint(name)
        return hits[-1] if hits else None


IsoDate = str  # Contract: normalized calendar date text, exactly YYYY-MM-DD.
Money = int | float


# Normalized payload aliases. They intentionally remain dict-compatible while
# `BankCollectResult.__post_init__` enforces the cross-bank date invariants.
NormalizedAccount = dict[str, Any]
NormalizedCard = dict[str, Any]
NormalizedTwdTxn = dict[str, Any]
NormalizedCardBilledTxn = dict[str, Any]
NormalizedCardPendingTxn = dict[str, Any]
class NormalizedCardBillFact(TypedDict, total=False):
    scope: Required[str]
    status: Required[str]
    remaining_due: Required[float]
    card_no: NotRequired[str]
    statement_close_date: NotRequired[str]
    payment_due_date: NotRequired[str]
    last_payment_amount: NotRequired[float]
    last_payment_date: NotRequired[str]
NormalizedBalanceHistory = dict[str, Any]
DailyMetric = dict[str, Any]


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?$")


def _require_iso_date(value: Any, *, path: str) -> None:
    if value in (None, ""):
        return
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        raise ValueError(f"{path} must be IsoDate YYYY-MM-DD, got {value!r}")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{path} must be IsoDate YYYY-MM-DD, got {value!r}") from exc


def _require_iso_date_or_datetime(value: Any, *, path: str) -> None:
    if value in (None, ""):
        return
    if not isinstance(value, str) or not (_ISO_DATE_RE.match(value) or _ISO_DATETIME_RE.match(value)):
        raise ValueError(f"{path} must be IsoDate/ISO datetime, got {value!r}")
    try:
        offset = re.search(r"[+-](\d{2}):?(\d{2})$", value)
        if offset and (int(offset.group(1)) > 23 or int(offset.group(2)) > 59):
            raise ValueError
        (datetime.fromisoformat if "T" in value or " " in value else date.fromisoformat)(value)
    except ValueError as exc:
        raise ValueError(f"{path} must be IsoDate/ISO datetime, got {value!r}") from exc


_CARD_BILL_FACT_FIELDS = frozenset({
    "scope", "status", "card_no", "remaining_due", "statement_close_date",
    "payment_due_date", "last_payment_amount", "last_payment_date",
})


def validate_card_bill_facts(facts: list[NormalizedCardBillFact], *, facts_ok: bool | None) -> None:
    """Validate canonical remaining-due facts at the collector seam."""
    if facts_ok is True and not facts:
        raise ValueError("card_bill_facts_ok=True requires at least one fact")
    if facts and facts_ok is not True:
        raise ValueError("card_bill_facts require card_bill_facts_ok=True")
    scopes: set[str] = set()
    card_nos: set[str] = set()
    for i, fact in enumerate(facts):
        if not isinstance(fact, dict):
            raise ValueError(f"card_bill_facts[{i}] must be a dict")
        unknown = set(fact) - _CARD_BILL_FACT_FIELDS
        if unknown:
            raise ValueError(f"card_bill_facts[{i}] has unknown fields: {sorted(unknown)}")
        scope = fact.get("scope")
        if scope not in {"bank", "card"}:
            raise ValueError(f"card_bill_facts[{i}].scope must be 'bank' or 'card'")
        scopes.add(scope)
        card_no = fact.get("card_no")
        if scope == "card":
            if not isinstance(card_no, str) or not card_no.strip():
                raise ValueError(f"card_bill_facts[{i}].card_no is required for card scope")
            if card_no in card_nos:
                raise ValueError(f"card_bill_facts[{i}].card_no is duplicated")
            card_nos.add(card_no)
        elif card_no not in (None, ""):
            raise ValueError(f"card_bill_facts[{i}].card_no is forbidden for bank scope")
        status = fact.get("status")
        if status not in {"paid", "unpaid", "no_payment_required"}:
            raise ValueError(f"card_bill_facts[{i}].status is not canonical")
        remaining = fact.get("remaining_due")
        if (isinstance(remaining, bool) or not isinstance(remaining, (int, float))
                or not math.isfinite(float(remaining)) or remaining < 0
                or remaining > 100_000_000):
            raise ValueError(f"card_bill_facts[{i}].remaining_due must be finite and non-negative")
        if (status == "unpaid") != (remaining > 0):
            raise ValueError(f"card_bill_facts[{i}] status conflicts with remaining_due")
        payment_amount = fact.get("last_payment_amount")
        payment_date = fact.get("last_payment_date")
        if (payment_amount is None) != (payment_date is None):
            raise ValueError(f"card_bill_facts[{i}] last payment must be an atomic pair")
        if payment_amount is not None and (
            isinstance(payment_amount, bool)
            or not isinstance(payment_amount, (int, float))
            or not math.isfinite(float(payment_amount))
            or payment_amount < 0
            or payment_amount > 100_000_000
        ):
            raise ValueError(
                f"card_bill_facts[{i}].last_payment_amount must be finite and non-negative"
            )
        for key in ("statement_close_date", "payment_due_date", "last_payment_date"):
            _require_iso_date(fact.get(key), path=f"card_bill_facts[{i}].{key}")
    if len(scopes) > 1 or ("bank" in scopes and len(facts) > 1):
        raise ValueError("card_bill_facts must be one bank fact or card-scoped facts")


HISTORY_DOMAINS = frozenset({"twd_transactions", "card_billed_transactions"})


def _history_date(value: Any, error: str) -> date:
    if not isinstance(value, str) or not _ISO_DATE_RE.fullmatch(value):
        raise ValueError(error)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(error) from None


def validate_history_coverage(
    coverage: Any,
    *,
    expected_mode: str,
    expected_domains: frozenset[str],
) -> dict[str, Any]:
    """Validate complete, contiguous, non-sensitive history evidence."""
    error = "invalid history coverage"
    if (
        expected_mode not in {"full", "incremental"}
        or not isinstance(expected_domains, frozenset)
        or not expected_domains
        or not expected_domains <= HISTORY_DOMAINS
    ):
        raise ValueError(error)
    if not isinstance(coverage, dict) or coverage.get("mode") != expected_mode:
        raise ValueError(error)
    domains = coverage.get("domains")
    if not isinstance(domains, list) or not domains:
        raise ValueError(error)

    domain_names: list[str] = []
    identity_count = 0
    window_count = 0
    starts: list[date] = []
    ends: list[date] = []
    for domain in domains:
        if not isinstance(domain, dict):
            raise ValueError(error)
        name = domain.get("domain")
        expected = domain.get("expected")
        windows = domain.get("windows")
        if (
            name not in HISTORY_DOMAINS
            or not isinstance(expected, list)
            or not isinstance(windows, list)
        ):
            raise ValueError(error)

        if not expected:
            empty_window = domain.get("empty_window")
            if not isinstance(empty_window, dict) or windows:
                raise ValueError(error)
            try:
                empty_start = _history_date(empty_window["start"], error)
                empty_end = _history_date(empty_window["end"], error)
            except (KeyError, TypeError, ValueError):
                raise ValueError(error) from None
            if (
                empty_start > empty_end
                or empty_window.get("status") != "explicit_empty"
                or type(empty_window.get("pages")) is not int
                or empty_window["pages"] < 1
            ):
                raise ValueError(error)
            domain_names.append(name)
            window_count += 1
            starts.append(empty_start)
            ends.append(empty_end)
            continue

        expected_ranges: dict[str, tuple[date, date]] = {}
        for item in expected:
            if not isinstance(item, dict):
                raise ValueError(error)
            identity = item.get("identity")
            try:
                expected_start = _history_date(item["start"], error)
                expected_end = _history_date(item["end"], error)
            except (KeyError, TypeError, ValueError):
                raise ValueError(error) from None
            if (
                not isinstance(identity, str) or not identity.strip()
                or identity in expected_ranges
                or expected_start > expected_end
            ):
                raise ValueError(error)
            expected_ranges[identity] = (expected_start, expected_end)

        by_identity: dict[str, list[tuple[date, date]]] = {
            identity: [] for identity in expected_ranges
        }
        for window in windows:
            if not isinstance(window, dict):
                raise ValueError(error)
            identity = window.get("identity")
            if identity not in by_identity:
                raise ValueError(error)
            try:
                start = _history_date(window["start"], error)
                end = _history_date(window["end"], error)
            except (KeyError, TypeError, ValueError):
                raise ValueError(error) from None
            if (
                start > end
                or window.get("status") not in {"complete", "explicit_empty"}
                or type(window.get("pages")) is not int
                or window["pages"] < 1
            ):
                raise ValueError(error)
            by_identity[identity].append((start, end))

        for identity, (expected_start, expected_end) in expected_ranges.items():
            identity_windows = sorted(by_identity[identity])
            if not identity_windows or identity_windows[0][0] != expected_start:
                raise ValueError(error)
            covered_end = identity_windows[0][1]
            for start, end in identity_windows[1:]:
                if start != covered_end + timedelta(days=1):
                    raise ValueError(error)
                covered_end = end
            if covered_end != expected_end:
                raise ValueError(error)
            starts.append(expected_start)
            ends.append(expected_end)

        domain_names.append(name)
        identity_count += len(expected_ranges)
        window_count += len(windows)

    if len(domain_names) != len(set(domain_names)) or set(domain_names) != expected_domains:
        raise ValueError(error)

    return {
        "ok": True,
        "mode": expected_mode,
        "domains": domain_names,
        "identities": identity_count,
        "windows": window_count,
        "start": min(starts).isoformat(),
        "end": max(ends).isoformat(),
    }


@dataclass(kw_only=True)
class BankCollectResult:
    """Shared return contract for every `BankCrawler.collect()`.

    The contract is explicit: collectors may only return fields declared on this
    dataclass. There is intentionally no opaque `raw` dict escape hatch. Current
    bank-specific parser payloads are represented as named transitional fields
    until each domain is migrated into the normalized lists above.
    """
    # Normalized cross-bank fields.
    bank: str | None = None
    accounts: list[NormalizedAccount] = field(default_factory=list)
    cards: list[NormalizedCard] = field(default_factory=list)
    twd_txns: list[NormalizedTwdTxn] = field(default_factory=list)
    card_billed_txns: list[NormalizedCardBilledTxn] = field(default_factory=list)
    card_pending_txns: list[NormalizedCardPendingTxn] = field(default_factory=list)
    card_bill_facts: list[NormalizedCardBillFact] = field(default_factory=list)
    card_bill_facts_ok: bool | None = None
    balance_history: list[NormalizedBalanceHistory] = field(default_factory=list)
    daily_metrics: list[DailyMetric] = field(default_factory=list)
    telemetry: dict[str, Any] = field(default_factory=dict)
    history_coverage: dict[str, Any] | None = None

    # Explicit transitional collect fields consumed by existing persist_<bank>()
    # adapters. These replace the previous opaque `raw` escape hatch: every key
    # still allowed through the collect contract must be declared here by name.
    _all_endpoints: Any = None
    _all_resources: Any = None
    _endpoint_count: Any = None
    _final_url: Any = None
    account_options: Any = None
    accounts_queried: Any = None
    after_card_click_url: Any = None
    alert_info: Any = None
    all_cards: Any = None
    all_pages: Any = None
    amount_page_text: Any = None
    api_responses: Any = None
    asset_chart: Any = None
    available_credit_twd: Any = None
    balance_latest: Any = None
    bank_balance: Any = None
    bill_due_amount: Any = None
    bill_text: Any = None
    bill_url: Any = None
    billed_page_text: Any = None
    billed_page_url: Any = None
    billed_txns: Any = None
    billing_period: Any = None
    billing_summary: Any = None
    card_all_frames_meta: Any = None
    card_api_dump: Any = None
    card_billed: Any = None
    card_billing: Any = None
    card_bills: Any = None
    card_bill_details: Any = None
    card_detail: Any = None
    card_final_url: Any = None
    card_frame_match: Any = None
    card_frame_name: Any = None
    card_frame_text: Any = None
    card_frame_url: Any = None
    card_frames: Any = None
    card_inquiry: Any = None
    card_limit: Any = None
    card_mega_menu_dump: Any = None
    card_nav_probe: Any = None
    card_nav_probe_2: Any = None
    card_pay_frames: Any = None
    card_pay_history: Any = None
    card_pay_nav_probe: Any = None
    card_quota: Any = None
    card_quota_frames: Any = None
    card_quota_nav_probe: Any = None
    card_resources: Any = None
    card_statement_transactions: Any = None
    card_statements: Any = None
    card_submenu: Any = None
    card_summary: Any = None
    card_text: Any = None
    card_transactions: Any = None
    card_transactions_ok: bool | None = None
    card_txn_form_submitted: Any = None
    card_txn_frames: Any = None
    card_txn_nav_probe: Any = None
    card_unbilled: Any = None
    card_url: Any = None
    cards_detail: Any = None
    cards_page_text: Any = None
    cards_page_url: Any = None
    clicked_credit_card: Any = None
    credit_card: Any = None
    credit_card_frame_url: Any = None
    credit_card_month_options: Any = None
    credit_card_page_text: Any = None
    credit_card_parsed: Any = None
    credit_limit_twd: Any = None
    currency: Any = None
    dbs_card_fee_click: Any = None
    dbs_card_fee_endpoints: Any = None
    dbs_card_fee_error: Any = None
    dbs_card_fee_page: Any = None
    dbs_card_fee_page_text: Any = None
    debit_accounts: Any = None
    deposit_foreign: Any = None
    deposit_menu_audit: Any = None
    deposit_page_text: Any = None
    deposit_page_url: Any = None
    deposit_twd: Any = None
    deposit_txn_click: Any = None
    deposit_txn_page_text: Any = None
    deposit_txn_page_url: Any = None
    deposit_txn_results: Any = None
    error: Any = None
    final_url: Any = None
    frames: Any = None
    home_text: Any = None
    initial_url: Any = None
    insurance: Any = None
    investment: Any = None
    limits: Any = None
    loan: Any = None
    main_text: Any = None
    menu_dom_audit: Any = None
    nav_items: Any = None
    net_present: Any = None
    overview_text: Any = None
    overview_url: Any = None
    payment_due_date: Any = None
    pending_click_ok: bool | None = None
    pending_page_text: Any = None
    pending_page_url: Any = None
    pending_txns: Any = None
    points: Any = None
    raw_text_sample: Any = None
    summary: Any = None
    title: Any = None
    top_summary: Any = None
    totals: Any = None
    transaction_text: Any = None
    transaction_url: Any = None
    twd_account_detail_api_endpoints: Any = None
    twd_account_detail_controls: Any = None
    twd_account_detail_text: Any = None
    twd_account_detail_url: Any = None
    twd_account_drilldown_click: Any = None
    twd_account_drilldown_error: Any = None
    twd_account_drilldown_target: Any = None
    twd_deposit: Any = None
    twd_history: Any = None
    twd_inquiry: Any = None
    twd_text: Any = None
    twd_transactions: Any = None
    twd_txn_error: Any = None
    twd_txn_form_controls: Any = None
    twd_txn_frame_url: Any = None
    twd_txn_frames: Any = None
    twd_txn_month_click_endpoints: Any = None
    twd_txn_month_clicks: Any = None
    twd_txn_nav_probe: Any = None
    twd_txn_other_month_clicks: Any = None
    twd_txn_other_months_probe: Any = None
    twd_txn_page_text: Any = None
    twd_txn_results: Any = None
    twd_url: Any = None
    used_credit_twd: Any = None

    def __post_init__(self) -> None:
        self._validate_normalized_dates()
        validate_card_bill_facts(self.card_bill_facts, facts_ok=self.card_bill_facts_ok)

    def _validate_normalized_dates(self) -> None:
        for i, a in enumerate(self.accounts):
            _require_iso_date(a.get("raw_balance_date"), path=f"accounts[{i}].raw_balance_date")
        for i, c in enumerate(self.cards):
            for key in ("statement_close_date", "payment_due_date", "last_payment_date"):
                _require_iso_date(c.get(key), path=f"cards[{i}].{key}")
        for i, t in enumerate(self.twd_txns):
            _require_iso_date_or_datetime(t.get("datetime"), path=f"twd_txns[{i}].datetime")
            _require_iso_date(t.get("account_date"), path=f"twd_txns[{i}].account_date")
        for i, t in enumerate(self.card_billed_txns):
            for key in ("bill_date", "date", "post_date"):
                _require_iso_date(t.get(key), path=f"card_billed_txns[{i}].{key}")
        for i, t in enumerate(self.card_pending_txns):
            for key in ("date", "post_date"):
                _require_iso_date(t.get(key), path=f"card_pending_txns[{i}].{key}")
        for i, r in enumerate(self.balance_history):
            _require_iso_date(r.get("snapshotDate"), path=f"balance_history[{i}].snapshotDate")

    def to_dict(self) -> dict[str, Any]:
        """Serialize non-empty contract fields for existing persist adapters."""
        out: dict[str, Any] = {}
        for f in dataclass_fields(self):
            name = f.name
            if name in {"bank", "telemetry"}:
                continue
            value = getattr(self, name)
            if value is None:
                continue
            if value == []:
                if name == "twd_txn_results" and self.history_coverage is not None:
                    out[name] = []
                continue
            if value == {}:
                continue
            if isinstance(value, list):
                out[name] = [dict(x) if isinstance(x, dict) else x for x in value]
            else:
                out[name] = value
        if self.telemetry:
            out["_collect_telemetry"] = self.telemetry
        return out


class _OriginGuardProxy:
    """Fail closed around every browser object operation during collection."""

    __slots__ = ("_target", "_guard", "_cache")

    def __init__(self, target, guard, cache=None):
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_guard", guard)
        object.__setattr__(self, "_cache", cache if cache is not None else {})
        self._cache[id(target)] = self

    def _wrap(self, value):
        if value is None or isinstance(value, (str, bytes, int, float, bool)):
            return value
        if isinstance(value, list):
            return [self._wrap(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._wrap(item) for item in value)
        if isinstance(value, dict):
            return {key: self._wrap(item) for key, item in value.items()}
        cached = self._cache.get(id(value))
        return cached if cached is not None else type(self)(value, self._guard, self._cache)

    @staticmethod
    def _unwrap(value):
        return value._target if isinstance(value, _OriginGuardProxy) else value

    def __getattr__(self, name):
        self._guard()
        value = getattr(self._target, name)
        self._guard()
        if not callable(value):
            return self._wrap(value)

        def guarded(*args, **kwargs):
            self._guard()
            result = value(
                *(self._unwrap(arg) for arg in args),
                **{key: self._unwrap(item) for key, item in kwargs.items()},
            )
            self._guard()
            return self._wrap(result)

        return guarded

    def __setattr__(self, name, value):
        if name in self.__slots__:
            object.__setattr__(self, name, value)
            return
        self._guard()
        setattr(self._target, name, self._unwrap(value))
        self._guard()

    def __enter__(self):
        self._guard()
        result = self._target.__enter__()
        self._guard()
        return self._wrap(result)

    def __exit__(self, *args):
        self._guard()
        result = self._target.__exit__(*args)
        self._guard()
        return result

    def __bool__(self):
        self._guard()
        return bool(self._target)

    def __repr__(self):
        return "<origin-guarded browser object>"


@dataclass
class BankCrawler(ABC):
    """銀行爬蟲基類。"""
    name: str
    session_dir: Path = field(init=False)
    collector: ResponseCollector | None = field(init=False, default=None)
    transaction_cursors: dict[str, dict[str, date]] = field(
        init=False, default_factory=dict,
    )

    HISTORY_COVERAGE_REQUIRED: ClassVar[bool] = False
    HISTORY_COVERAGE_DOMAINS: ClassVar[frozenset[str]] = frozenset()

    def configure_transaction_cursor(
        self,
        domain: str,
        cursor: dict[str, date],
    ) -> None:
        if (
            domain not in HISTORY_DOMAINS
            or any(
                not isinstance(identity, str) or not identity.strip() or type(value) is not date
                for identity, value in cursor.items()
            )
        ):
            raise ValueError("transaction cursor must map non-empty identities to dates")
        self.transaction_cursors[domain] = dict(cursor)

    def transaction_start_for(
        self,
        identity: str,
        *,
        domain: str = "twd_transactions",
    ) -> date | None:
        return getattr(self, "transaction_cursors", {}).get(domain, {}).get(identity)

    def transaction_window_start(
        self,
        identity: str,
        *,
        floor: date,
        overlap_days: int = 7,
        domain: str = "twd_transactions",
    ) -> date:
        if type(overlap_days) is not int or not 0 <= overlap_days <= 31:
            raise ValueError("overlap_days must be an integer from 0 through 31")
        mode = os.environ.get("BANK_CRAWLER_HISTORY_MODE", "full")
        if mode not in {"full", "incremental"}:
            raise ValueError(f"invalid BANK_CRAWLER_HISTORY_MODE: {mode!r}")
        if mode == "full":
            return floor
        cursor = self.transaction_start_for(identity, domain=domain)
        if cursor is None:
            return floor
        return max(floor, cursor - timedelta(days=overlap_days))

    # 一個 user_data_dir 持久化 session 可信的最長秒數。
    # 預設 3 分鐘（使用者指示 2026-06-17）— 為什麼這麼短：
    #   1. 銀行 server-side session 通常 5-15 分鐘逾時，client cookie 仍在但已
    #      被踢；下次再用會出現「pseudo logged-in 但頁面是 stub（text len<200）」
    #      灰色狀態，crawler 看 cookie 存在以為登入成功，所有 navigation 卻全
    #      fail，但 result 卻被誤判 status=done。詳見 SCSB job 43 案例
    #      (2026-06-16 17:02-17:03)。
    #   2. 我們的 sync 通常 1 分鐘內結束，3 分鐘 buffer 涵蓋連續手測的場景，
    #      但避開所有「跨 sync 殘留」風險。
    #   3. 重 login 成本不算貴（每家 10-30s），比 stale session 抓 0 筆值得。
    # 子類可 override：例 HSBC 首次登入有裝置綁定 OTP，可拉到 600 秒減少 OTP 觸發。
    SESSION_MAX_AGE_SECONDS: int = 180
    USES_SHARED_LOGIN_CHECKPOINTS: ClassVar[bool] = False
    CREDENTIAL_HOSTS: ClassVar[frozenset[str]] = frozenset()
    SAFE_COLLECT_GUARDS: ClassVar[frozenset[str]] = frozenset()
    _shared_dialog_blocked: bool = False
    _dialog_dismiss_failed: bool = False
    _origin_inspection_failed: bool = False

    def __post_init__(self):
        self.session_dir = DATA_ROOT / f"{self.name}_session"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        # C-3 修法 (2026-06-17): per-bank captcha 暫存檔路徑 (放 session_dir 內)。
        # 12 家共用 /tmp/captcha_tmp.png 已踩過 race condition,
        # 改放 session_dir/captcha.png 確保每家獨立, sync 並行不互踩.
        # solve_captcha / wait_captcha_stable 都需 caller 傳 tmp_path=self.captcha_tmp.
        self.captcha_tmp: Path = self.session_dir / "captcha.png"

    def _session_age_seconds(self) -> float | None:
        """回傳 session_dir 內任一 cookie/state 檔的最新 mtime 距現在秒數。

        無檔（首次 / 已被清）回 None；用於 _enforce_session_freshness。
        看的檔：Chromium 持久 profile 內常見的 Cookies / Default/Cookies /
        Local State，挑最新的 mtime。
        """
        candidates: list[float] = []
        for sub in ("Cookies", "Default/Cookies", "Local State",
                    "Default/Local Storage", "Default/Session Storage"):
            p = self.session_dir / sub
            try:
                if p.exists():
                    candidates.append(p.stat().st_mtime)
            except OSError:
                pass
        if not candidates:
            return None
        import time as _t
        return _t.time() - max(candidates)

    def _enforce_session_freshness(self) -> None:
        """若 session_dir 上次活動超過 SESSION_MAX_AGE_SECONDS，整個 dir 砍掉重建。

        為什麼整個砍而不只刪 cookies：Chromium user_data_dir 內 Cookies、
        Session Storage、Local State、IndexedDB 互相依賴；只刪 Cookies 容易
        造成「半 stale」反而更難 debug。砍掉重建 = 強制走完整 login，path
        well-tested。

        什麼時機觸發：BankCrawler.run() 開瀏覽器前。
        """
        import shutil
        import sys as _sys
        age = self._session_age_seconds()
        if age is None:
            return  # 首次 / 已空，沒事
        if age <= self.SESSION_MAX_AGE_SECONDS:
            return  # 還新鮮，沿用
        # 過期了
        print(
            f"[{self.name}][session] age={age:.0f}s > max={self.SESSION_MAX_AGE_SECONDS}s "
            f"→ 砍 {self.session_dir} 強制重 login",
            file=_sys.stderr,
        )
        try:
            shutil.rmtree(self.session_dir)
        except OSError as e:
            print(f"[{self.name}][session] rmtree 失敗（best-effort）: "
                  f"{_safe_exception_type(e)}; details withheld",
                  file=_sys.stderr)
        self.session_dir.mkdir(parents=True, exist_ok=True)

    @abstractmethod
    def login(self, page) -> bool:
        """填表登入，回傳是否成功。"""

    def prepare_login_page(self, page) -> None:
        raise NotImplementedError

    def is_authenticated(self, page) -> bool:
        raise NotImplementedError

    def submit_credentials_once(self, page) -> None:
        raise NotImplementedError

    def prepare_captcha_resubmit(self, page) -> None:
        """Prepare one reducer-authorized CAPTCHA resubmission, if needed."""

    def login_checkpoint_rules(self) -> tuple[LoginCheckpointRule, ...]:
        return ()

    def _recover_late_authentication(self, page, error: Exception) -> bool:
        """Allow a bank-specific, non-resubmitting auth recheck after a terminal."""
        return False

    def _shared_login(self, page) -> bool:
        if self.name == "sinopac":
            self._sinopac_diagnostics = {}
            self._login_diagnostic_floor = None
            self._login_terminal_exception_type = None
            self._login_underlying_exception_type = None
        if not self._credential_origin_allowed(page):
            reduce_login_checkpoint(
                CheckpointPhase.PRE_SUBMIT,
                LoginBudget(),
                CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, reason=self._origin_failure_reason(CheckpointReason.ORIGIN_BEFORE_PREPARE)),
            )
        self.prepare_login_page(page)
        if not self._credential_origin_allowed(page):
            reduce_login_checkpoint(
                CheckpointPhase.PRE_SUBMIT,
                LoginBudget(),
                CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, reason=self._origin_failure_reason(CheckpointReason.ORIGIN_AFTER_PREPARE)),
            )
        rules = self.login_checkpoint_rules()
        if len({rule.name for rule in rules}) != len(rules):
            reduce_login_checkpoint(
                CheckpointPhase.PRE_SUBMIT,
                LoginBudget(),
                CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, reason=CheckpointReason.DUPLICATE_RULE_NAMES),
            )
        if any(rule.bank != self.name for rule in rules):
            reduce_login_checkpoint(
                CheckpointPhase.PRE_SUBMIT,
                LoginBudget(),
                CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, reason=CheckpointReason.BANK_MISMATCH),
            )

        action_counts = {rule.name: 0 for rule in rules}
        phase = CheckpointPhase.PRE_SUBMIT
        budget = LoginBudget()
        max_steps = sum(
            rule.max_actions for rule in rules if rule.is_clickable
        ) + 8

        for _ in range(max_steps):
            if getattr(self, "_shared_dialog_blocked", False):
                reduce_login_checkpoint(
                    phase,
                    budget,
                    CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, reason=self._dialog_failure_reason()),
                )
            if not self._credential_origin_allowed(page):
                reduce_login_checkpoint(
                    phase,
                    budget,
                    CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, reason=self._origin_failure_reason(CheckpointReason.ORIGIN_BEFORE_EVALUATION)),
                )
            active_rules = tuple(
                rule
                if (
                    (not rule.is_clickable or action_counts[rule.name] < rule.max_actions)
                    and (
                        rule.kind is not CheckpointKind.PROTOCOL_RESUBMIT
                        or (
                            phase is CheckpointPhase.POST_SUBMIT
                            and budget.credential_submissions == 1
                            and budget.protocol_resubmits == 0
                        )
                    )
                    and (
                        rule.kind is not CheckpointKind.CAPTCHA_RETRY
                        or (
                            phase is CheckpointPhase.POST_SUBMIT
                            and budget.credential_submissions == 1
                            and budget.captcha_resubmits == 0
                        )
                    )
                    and (
                        rule.kind is not CheckpointKind.STARTUP_RECOVERY
                        or budget.reloads == 0
                    )
                )
                else replace(
                    rule,
                    kind=CheckpointKind.UNKNOWN_BLOCKER,
                    action_selector=DEFAULT_ACTION_SELECTOR,
                    action_texts=(),
                    max_actions=1,
                )
                for rule in rules
                if phase in rule.phases
            )
            outcome = evaluate_login_checkpoint(
                page,
                bank=self.name,
                phase=phase,
                rules=active_rules,
                is_authenticated=self.is_authenticated,
                is_scope_owned=lambda frame: self._frame_origin_allowed(page, frame),
                can_act=lambda: (
                    not getattr(self, "_shared_dialog_blocked", False)
                    and self._credential_origin_allowed(page)
                    and not getattr(self, "_shared_dialog_blocked", False)
                ),
            )
            if not self._credential_origin_allowed(page):
                reduce_login_checkpoint(
                    phase,
                    budget,
                    CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, reason=self._origin_failure_reason(CheckpointReason.ORIGIN_AFTER_EVALUATION)),
                )
            active_rules_by_name = {rule.name: rule for rule in active_rules}
            outcome = validate_login_checkpoint_outcome(outcome, active_rules)
            if getattr(self, "_shared_dialog_blocked", False):
                reduce_login_checkpoint(
                    phase,
                    budget,
                    CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, reason=self._dialog_failure_reason()),
                )
            if (outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
                    and any(rule.name == outcome.rule_name and rule.kind is not CheckpointKind.UNKNOWN_BLOCKER
                            for rule in rules)
                    and outcome.reason is CheckpointReason.MATCHED_BLOCKER):
                outcome = replace(outcome, reason=CheckpointReason.RULE_BUDGET_EXHAUSTED)
            next_phase, next_budget = reduce_login_checkpoint(phase, budget, outcome)

            if (
                outcome.rule_name in active_rules_by_name
                and active_rules_by_name[outcome.rule_name].is_clickable
            ):
                action_counts[outcome.rule_name] += 1
            if next_budget.credential_submissions == budget.credential_submissions + 1:
                if next_budget.captcha_resubmits == budget.captcha_resubmits + 1:
                    self.prepare_captcha_resubmit(page)
                if not self._credential_origin_allowed(page):
                    reduce_login_checkpoint(
                        phase,
                        budget,
                        CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, reason=self._origin_failure_reason(CheckpointReason.ORIGIN_BEFORE_SUBMIT)),
                    )
                self.submit_credentials_once(page)
            if next_budget.reloads == budget.reloads + 1:
                page.reload()
                self.prepare_login_page(page)
            if (
                phase is CheckpointPhase.POST_SUBMIT_SETTLE
                and outcome.kind is CheckpointKind.AUTHENTICATED
            ):
                return True
            phase, budget = next_phase, next_budget

        reduce_login_checkpoint(
            phase,
            budget,
            CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, reason=CheckpointReason.STEP_LIMIT),
        )
        return False  # pragma: no cover - reducer always raises

    def _credential_origin_allowed(self, page) -> bool:
        self._origin_inspection_failed = False
        try:
            return self._exact_https_origin_allowed(
                page.url, self.CREDENTIAL_HOSTS, _raise_on_error=True
            )
        except Exception:
            self._origin_inspection_failed = True
            return False

    def _origin_failure_reason(self, fallback):
        return (CheckpointReason.ORIGIN_INSPECTION_EXCEPTION
                if getattr(self, "_origin_inspection_failed", False) is True else fallback)

    def _dialog_failure_reason(self):
        return (CheckpointReason.DIALOG_DISMISS_EXCEPTION
                if getattr(self, "_dialog_dismiss_failed", False) is True
                else CheckpointReason.DIALOG_BLOCKED)

    @staticmethod
    def _exact_https_origin_allowed(
        url: str, hosts: frozenset[str], *, _raise_on_error: bool = False
    ) -> bool:
        try:
            current = urlparse(url or "")
            return (
                current.scheme.lower() == "https"
                and (current.hostname or "").lower() in hosts
                and current.port in (None, 443)
                and current.username is None
                and current.password is None
            )
        except Exception:
            if _raise_on_error:
                raise
            return False

    def _frame_origin_allowed(self, page, frame) -> bool:
        current = frame
        main_frame = getattr(page, "main_frame", None)
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if main_frame is not None and current is main_frame:
                return self._credential_origin_allowed(page)
            try:
                parsed = urlparse(current.url or "")
            except Exception:
                return False
            if parsed.scheme == "about" and parsed.path in {"blank", "srcdoc"}:
                current = getattr(current, "parent_frame", None)
                continue
            return self._exact_https_origin_allowed(
                current.url, self.CREDENTIAL_HOSTS
            )
        return False

    @abstractmethod
    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        """造訪各功能頁、觸發查詢，回傳共同 BankCollectResult contract。"""

    # ─────────────────────────────────────────────────────────
    # Shared macOS browser fingerprint（所有銀行預設繼承）
    # ─────────────────────────────────────────────────────────
    # UA / Client Hints / navigator properties 必須一起 macOS 化，避免互相矛盾。
    # 子類仍可覆寫 FETCH_*，但 production banks 預設共用這套設定。
    # 詳見 wiki/concepts/bank-crawler-platform-spoof-rule.md。
    FETCH_USERAGENT: ClassVar[str] = MACOS_UA
    FETCH_EXTRA_HEADERS: ClassVar[dict[str, str]] = {
        "sec-ch-ua-platform": '"macOS"',
        "sec-ch-ua-platform-version": '"15.0.0"',
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    }
    FETCH_LOCALE: ClassVar[str] = "zh-TW"
    FETCH_TIMEZONE_ID: ClassVar[str | None] = None
    FETCH_INIT_SCRIPT: ClassVar[str] = MACOS_SPOOF_JS
    FETCH_REAL_CHROME: ClassVar[bool] = False

    def _build_fetch_kwargs(self) -> dict:
        """組裝 StealthyFetcher.fetch 額外參數，把 init script 寫成臨時檔。

        子類覆寫上面 FETCH_* class var 即可生效。
        回傳 dict 內含 __cleanups__ key (list of callable)，呼叫者跑完要逐一呼叫。
        """
        import tempfile
        kw: dict = {}
        cleanups: list = []
        if self.FETCH_USERAGENT:
            kw["useragent"] = self.FETCH_USERAGENT
        if self.FETCH_EXTRA_HEADERS:
            kw["extra_headers"] = dict(self.FETCH_EXTRA_HEADERS)
        if self.FETCH_LOCALE:
            kw["locale"] = self.FETCH_LOCALE
        if self.FETCH_TIMEZONE_ID:
            kw["timezone_id"] = self.FETCH_TIMEZONE_ID
        if self.FETCH_INIT_SCRIPT:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".js", delete=False, encoding="utf-8",
            ) as f:
                f.write(self.FETCH_INIT_SCRIPT)
            kw["init_script"] = f.name
            def _cleanup(path=f.name):
                with contextlib.suppress(OSError): Path(path).unlink()
            cleanups.append(_cleanup)
        if self.FETCH_REAL_CHROME:
            kw["real_chrome"] = True
        kw["__cleanups__"] = cleanups
        return kw

    def _execute_browser_flow(
        self,
        login_url: str,
        *,
        headless: bool,
        page_action,
        fetch_kwargs: dict,
    ) -> None:
        StealthyFetcher.fetch(
            login_url,
            headless=headless,
            network_idle=False,
            load_dom=True,
            wait=2000,
            timeout=180000,
            user_data_dir=str(self.session_dir),
            page_action=page_action,
            google_search=True,
            # 2026-06-18 evidence: these three protections pass CTBC's
            # PerimeterX/HUMAN BotManager; solve_cloudflare breaks this SPA path.
            hide_canvas=True,
            block_webrtc=True,
            dns_over_https=True,
            **fetch_kwargs,
        )

    def log_login_failure_diagnostics(self, page) -> None:
        """Optional adapter-owned stderr diagnostics; never affects login decisions."""

    def run(self, login_url: str, headless: bool = False) -> dict:
        """完整流程：開瀏覽器 → 登入 → 抓取 → **登出** → 回傳資料。

        logout 走 finally 保證執行（包含 collect raise 的 case），best-effort
        不影響 result。為什麼：crawler 不正常登出會讓銀行 server-side session
        殘留，下次登入撞「重複登入」/「上次未正常登出」彈窗（CTBC, 台新 …）。
        """
        # StealthyFetcher is imported at module scope so tests can monkeypatch
        # backend.core.base.StealthyFetcher.fetch and exercise run() without a browser.

        # 開瀏覽器前先檢查 session 是否過期；過期就砍掉強制重 login。
        # 詳見 SESSION_MAX_AGE_SECONDS docstring（SCSB 2026-06-16 案例）。
        self._enforce_session_freshness()

        collector = ResponseCollector(host_filter=self._host_filter())
        result: dict = {}

        def page_action(page):
            logged_in = False
            try:
                collector.attach(page)
                self.collector = collector  # 讓 login() 能用攔截到的 API（如 captcha base64）
                self._shared_dialog_blocked = False
                self._dialog_dismiss_failed = False
                self.attach_shared_dialog_handler(page)
                try:
                    ok = self._shared_login(page)
                except Exception as e:
                    try:
                        recovered = self._recover_late_authentication(page, e)
                    except Exception:
                        recovered = False
                    if recovered:
                        ok = True
                    else:
                        # login 子類可能 raise（例：ScsbLoginError、TaishinLoginError）
                        # — 不讓 StealthyFetcher 內部 swallow 變成 silent done。
                        # 只寫固定診斷欄位，由 sync_runner 轉成 status=error。
                        import sys as _sys
                        exception_type = _safe_exception_type(e)
                        exception_state = _base_exception_state(e)
                        self._login_terminal_exception_type = exception_type
                        context = BaseException.__dict__["__cause__"].__get__(e, BaseException)
                        if context is None:
                            context = _base_exception_context(e)
                        self._login_underlying_exception_type = (
                            _safe_exception_type(context) if context is not None else None
                        )
                        safe_code = _safe_state_value(exception_state, "safe_code")
                        if _exception_inherits(
                            e, LoginCheckpointBlocked, LoginInteractionRequired
                        ):
                            outcome = _safe_state_value(exception_state, "outcome")
                            try:
                                outcome_state = (
                                    object.__getattribute__(outcome, "__dict__")
                                    if type(outcome) is CheckpointOutcome
                                    else {}
                                )
                            except BaseException:
                                outcome_state = {}
                            kind = _safe_state_value(outcome_state, "kind")
                            kind_label = _safe_checkpoint_kind_label(kind)
                            raw_budget = _safe_state_value(exception_state, "budget")
                            budget = LoginBudget()
                            if type(raw_budget) is LoginBudget:
                                try:
                                    budget_state = object.__getattribute__(
                                        raw_budget, "__dict__"
                                    )
                                    if type(budget_state) is dict:
                                        budget = LoginBudget(
                                            credential_submissions=_safe_state_int(
                                                budget_state, "credential_submissions"
                                            ),
                                            protocol_resubmits=_safe_state_int(
                                                budget_state, "protocol_resubmits"
                                            ),
                                            captcha_resubmits=_safe_state_int(
                                                budget_state, "captcha_resubmits"
                                            ),
                                            reloads=_safe_state_int(
                                                budget_state, "reloads"
                                            ),
                                        )
                                except (AttributeError, TypeError, ValueError):
                                    budget = LoginBudget()
                            msg = (
                                f"{exception_type}: kind={kind_label}, "
                                f"credential_submissions={budget.credential_submissions}, "
                                f"protocol_resubmits={budget.protocol_resubmits}, "
                                f"captcha_resubmits={budget.captcha_resubmits}, "
                                f"reloads={budget.reloads}, "
                                f"{_safe_login_diagnostics(self.name, exception_state, outcome_state)}"
                            )
                        elif type(safe_code) is str and safe_code == "captcha_ocr_failed":
                            msg = f"{exception_type}: code=captcha_ocr_failed"
                        else:
                            msg = f"{exception_type}: login failed"
                        native_diagnostics = _safe_native_login_diagnostics(self.name, exception_state)
                        if native_diagnostics:
                            msg += f", {native_diagnostics}"
                        print(
                            f"[{self.name}][login] raise → {msg}; details withheld",
                            file=_sys.stderr,
                        )
                        with contextlib.suppress(Exception):
                            self.log_login_failure_diagnostics(page)
                        result["error"] = msg
                        return page

                if not ok:
                    with contextlib.suppress(Exception):
                        self.log_login_failure_diagnostics(page)
                    result["error"] = "login_failed"
                    return page
                logged_in = True
                try:
                    def ensure_collect_origin() -> None:
                        if (
                            getattr(self, "_shared_dialog_blocked", False)
                            or not self._credential_origin_allowed(page)
                        ):
                            reduce_login_checkpoint(
                                CheckpointPhase.POST_SUBMIT_SETTLE,
                                LoginBudget(credential_submissions=1),
                                CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER, reason=(
                                    self._dialog_failure_reason() if self._shared_dialog_blocked
                                    else self._origin_failure_reason(CheckpointReason.COLLECT_ORIGIN_OR_DIALOG))),
                            )

                    ensure_collect_origin()
                    collect_result = self.collect(
                        _OriginGuardProxy(page, ensure_collect_origin), collector
                    )
                    ensure_collect_origin()
                    if not isinstance(collect_result, BankCollectResult):
                        raise TypeError(
                            f"{self.__class__.__name__}.collect() must return "
                            f"BankCollectResult, got {type(collect_result).__name__}"
                        )
                    if collect_result.error is None and collect_result.card_bill_facts_ok is None:
                        raise ValueError(
                            f"{self.__class__.__name__}.collect() must publish "
                            "card_bill_facts_ok at the crawler boundary"
                        )
                    result["data"] = collect_result.to_dict()
                except Exception as e:
                    # collect 階段 raise（包含 SCSB/Taishin 等明細查詢 raise）
                    # 一樣寫進 error，但 logged_in 仍 True → finally 會跑 logout。
                    import sys as _sys
                    msg = (
                        f"collect_failed: {_safe_exception_type(e)}: "
                        f"code={_safe_collect_failure_code(e)}"
                    )
                    if _exception_inherits(e, LoginCheckpointBlocked, LoginInteractionRequired):
                        exception_state = _base_exception_state(e)
                        outcome = _safe_state_value(exception_state, "outcome")
                        try:
                            outcome_state = (object.__getattribute__(outcome, "__dict__")
                                             if type(outcome) is CheckpointOutcome else {})
                        except BaseException:
                            outcome_state = {}
                        msg += f", {_safe_login_diagnostics(self.name, exception_state, outcome_state)}"
                    guard = _safe_collect_guard(
                        e, _class_collect_guard_allowlist(self)
                    )
                    if guard is not None:
                        msg += f": guard={guard}"
                    print(
                        f"[{self.name}][collect] raise → {msg}; details withheld",
                        file=_sys.stderr,
                    )
                    result["error"] = msg
            finally:
                # 已登入且仍在owned origin才best-effort logout；foreign origin零互動。
                if logged_in and self._credential_origin_allowed(page):
                    try:
                        self.logout(page)
                    except Exception as e:
                        import sys as _sys
                        print(
                            f"[{self.name}][logout] exception {_safe_exception_type(e)} "
                            "(details withheld; best-effort, swallow)",
                            file=_sys.stderr,
                        )
                collector.detach(page)
            return page

        # 所有 crawler 從 base 繼承同一套 macOS fingerprint spoof。
        # 詳見 wiki/concepts/bank-crawler-platform-spoof-rule.md
        fetch_kwargs = self._build_fetch_kwargs()
        cleanups = fetch_kwargs.pop("__cleanups__", [])

        try:
            # Stealth defaults shared by all banks: no solve_cloudflare hook; keep the
            # proven canvas/WebRTC/DNS protections and each adapter's fetch kwargs.
            self._execute_browser_flow(
                login_url,
                headless=headless,
                page_action=page_action,
                fetch_kwargs=fetch_kwargs,
            )
        finally:
            for c in cleanups:
                with contextlib.suppress(Exception): c()
        return result


    def attach_shared_dialog_handler(self, page) -> None:
        """Dismiss opaque JS dialogs and let the typed lifecycle fail closed."""
        def _on_dialog(dialog):
            self._shared_dialog_blocked = True
            try:
                dialog.dismiss()
            except Exception:
                self._dialog_dismiss_failed = True

        page.on("dialog", _on_dialog)

    def _host_filter(self) -> str:
        return ""

    # ─────────────────────────────────────────────────────────
    # 通用「登出」處理（所有銀行共用）
    # ─────────────────────────────────────────────────────────
    # 預設登出按鈕文字優先順序（找第一個可見且符合的元素點下）。
    # 為什麼必須做：crawler 不主動登出 → server-side session 殘留 →
    # 下次登入會被銀行視為「重複登入」/「上次未正常登出」，
    # 不少銀行（CTBC, 台新…）會跳「確認登入」彈窗甚至直接擋登入。
    #
    # ⚠️ W (2026-06-17): 移除 "結束" — 太籠統會誤點到 "結束查詢"、"結束會員"、
    # "結束作業" 之類非登出按鈕，反而讓 session 沒真正結束 → 下次撞 ghost。
    # 若某家銀行真的只有 "結束" 字樣，請在該 crawler subclass override LOGOUT_BTN_TEXTS。
    LOGOUT_BTN_TEXTS = (
        "登出", "安全登出", "Sign Out", "Sign out", "Logout", "Log Out", "Log out",
        "登出系統", "離開系統",
    )
    LOGOUT_AVOID_BTN_TEXTS = ("取消", "Cancel", "No", "否", "返回",)

    def logout(self, page) -> bool:
        """嘗試讓 user 從銀行 server 正常登出。

        預設實作：掃所有 frame 找含 LOGOUT_BTN_TEXTS 的可見按鈕/連結，點第一個。
        子類可 override：例如 SPA 銀行 (CTBC, HSBC) 走 frontend route，
        或某些銀行登出按鈕藏在 user menu 裡需先 hover 父選單。

        回傳：True=成功點到登出按鈕；False=找不到（已 best-effort 不必 raise）。
        """
        import sys as _sys
        kick_json = json.dumps(list(self.LOGOUT_BTN_TEXTS))
        avoid_json = json.dumps(list(self.LOGOUT_AVOID_BTN_TEXTS))
        for f in page.frames:
            try:
                clicked = f.evaluate(f"""
                    () => {{
                      const wants = {kick_json};
                      const avoidSet = new Set({avoid_json});
                      const cand = document.querySelectorAll(
                        'button, a, input[type=button], input[type=submit], [role=button]'
                      );
                      const visible = [];
                      for (const b of cand) {{
                        const t = (b.textContent || b.value || '').trim();
                        if (!t || t.length > 30) continue;
                        if (avoidSet.has(t)) continue;
                        const rect = b.getBoundingClientRect();
                        if (rect.width <= 0 || rect.height <= 0) continue;
                        const cs = window.getComputedStyle(b);
                        if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                        visible.push({{ el: b, text: t, tag: b.tagName }});
                      }}
                      // 依優先順序找匹配（exact match 優先，含 partial 次之）
                      for (const want of wants) {{
                        for (const v of visible) {{
                          if (v.text === want) {{
                            v.el.click();
                            return v.text + ' (' + v.tag + ', exact)';
                          }}
                        }}
                      }}
                      for (const want of wants) {{
                        for (const v of visible) {{
                          if (v.text.includes(want)) {{
                            v.el.click();
                            return v.text + ' (' + v.tag + ', partial)';
                          }}
                        }}
                      }}
                      return null;
                    }}
                """)
                if clicked:
                    print(f"[{self.name}][logout] ✓ 已點登出控制", file=_sys.stderr)
                    try:
                        page.wait_for_timeout(3000)  # 等 server 處理 logout
                    except Exception:
                        pass
                    return True
            except Exception as e:
                print(
                    f"[{self.name}][logout] frame 掃描失敗: {_safe_exception_type(e)}",
                    file=_sys.stderr,
                )
                continue
        print(f"[{self.name}][logout] ⚠️ 沒找到登出按鈕（best-effort，繼續）", file=_sys.stderr)
        return False

    @staticmethod
    def mask_card(num: str) -> str:
        s = re.sub(r"\D", "", str(num or ""))
        if len(s) >= 8:
            return f"{s[:4]}****{s[-4:]}"
        return num or ""
