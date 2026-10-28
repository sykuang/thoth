"""Offline shared terminal guards: native short-circuit traces and safe sink IDs."""
from types import SimpleNamespace

import pytest

from backend.core import base
from backend.core.error_codes import exception_error_id
from backend.core.login_checkpoints import LoginCheckpointBlocked
from backend.core.sync_errors import safe_failure_message


class Truth:
    def __init__(self, value, trace):
        self.value, self.trace = value, trace

    def __bool__(self):
        self.trace.append(("truth", self.value))
        return self.value


class FakeCrawler(base.BankCrawler):
    @property
    def _shared_dialog_blocked(self):
        value = next(self.flags, False)
        self.trace.append(("dialog", value))
        return Truth(value, self.trace)

    def _credential_origin_allowed(self, page):
        value = next(self.origins, True)
        self.trace.append(("origin", value))
        return value

    def prepare_login_page(self, page):
        self.trace.append("prepare")

    def login(self, page):
        return self._shared_login(page)

    def is_authenticated(self, page):
        raise AssertionError("evaluator is the fake boundary")

    def collect(self, page, collector):
        raise AssertionError("no collection allowed")

    def submit_credentials_once(self, page):
        self.trace.append("submit")
        if self.failures:
            self.failures -= 1
            raise self.failure
        page.click()


def fake(*, flags=(), origins=(), failures=0, bank="ctbc", failure=None):
    crawler = object.__new__(FakeCrawler)  # real constructors/credentials stay denied
    crawler.name = bank
    crawler.trace = []
    crawler.flags, crawler.origins = iter(flags), iter(origins)
    crawler.failures = failures
    crawler.failure = failure or base.captcha_unreadable(RuntimeError("synthetic OCR"))
    page = SimpleNamespace(
        reload=lambda: crawler.trace.append("reload"),
        click=lambda: crawler.trace.append("click"),
    )
    return crawler, page


def dialog(value):
    return [("dialog", value), ("truth", value)]


def terminal(call, crawler, suffix, copy):
    with pytest.raises(LoginCheckpointBlocked) as caught:
        call()
    error = caught.value
    error_id = crawler.name.upper() + "-" + suffix
    assert error.outcome.kind is base.CheckpointKind.UNKNOWN_BLOCKER
    assert error.outcome.error_id == error_id
    assert exception_error_id(error, bank=crawler.name) == error_id
    assert safe_failure_message(error, crawler.name) == (
        f"[{error_id}] {copy} | sync_failed:LoginCheckpointBlocked"
    )
    return error


@pytest.mark.parametrize("blocked,origin,suffix", [
    (True, False, "03-940-005"), (False, False, "03-940-006"),
    (False, True, None),
])
def test_reload_guard(blocked, origin, suffix):
    crawler, page = fake(flags=[blocked], origins=[origin], failures=1)
    call = lambda: crawler._submit_with_captcha_reloads(page, page)
    expected = ["submit", "reload", "prepare", *dialog(blocked)]
    if not blocked:
        expected.append(("origin", origin))
    if suffix:
        error = terminal(call, crawler, suffix, "驗證碼重載後：" + (
            "原生JavaScript對話框阻擋" if blocked else "憑證來源檢查未通過"))
        assert error.phase is base.CheckpointPhase.PRE_SUBMIT
        assert error.budget == base.LoginBudget()
    else:
        assert call() is None
        expected += ["submit", "click"]
    assert crawler.trace == expected


def evaluator(monkeypatch, crawler, *, exhaust=False):
    def evaluate(page, *, phase, **kwargs):
        crawler.trace.append(("evaluate", phase.value))
        kind = (base.CheckpointKind.DISMISSIBLE_NOTICE if exhaust else
                base.CheckpointKind.READY_FOR_CREDENTIALS
                if phase is base.CheckpointPhase.PRE_SUBMIT else
                base.CheckpointKind.AUTHENTICATED)
        return base.CheckpointOutcome(kind)
    monkeypatch.setattr(base, "evaluate_login_checkpoint", evaluate)


def presubmit_prefix():
    return [("origin", True), "prepare", ("origin", True), *dialog(False),
            ("origin", True), ("evaluate", "pre_submit"), ("origin", True),
            *dialog(False)]


@pytest.mark.parametrize("blocked,origin,suffix", [
    (True, False, "03-940-007"), (False, False, "03-940-008"),
    (False, True, None),
])
def test_presubmit_guard(monkeypatch, blocked, origin, suffix):
    crawler, page = fake(flags=[False, False, blocked],
                         origins=[True, True, True, True, origin])
    evaluator(monkeypatch, crawler)
    call = lambda: crawler._shared_login(page)
    expected = [*presubmit_prefix(), *dialog(blocked)]
    if not blocked:
        expected.append(("origin", origin))
    if suffix:
        error = terminal(call, crawler, suffix, "登入送出前：" + (
            "原生JavaScript對話框阻擋" if blocked else "憑證來源檢查未通過"))
        assert error.phase is base.CheckpointPhase.PRE_SUBMIT
        assert error.budget == base.LoginBudget()
        assert crawler.trace == expected
    else:
        assert call() is True
        assert crawler.trace[:len(expected) + 2] == [*expected, "submit", "click"]
        assert crawler.trace.count("submit") == 1
        assert crawler.trace.count("reload") == 0
        assert [x for x in crawler.trace if isinstance(x, tuple) and x[0] == "evaluate"] == [
            ("evaluate", "pre_submit"), ("evaluate", "post_submit"),
            ("evaluate", "post_submit_settle")]


@pytest.mark.parametrize("guard_index", range(4))
@pytest.mark.parametrize("operand,suffix", [
    ("before", "03-940-009"), ("origin", "03-940-010"),
    ("after", "03-940-011"),
])
def test_esun_operation_guard(monkeypatch, guard_index, operand, suffix):
    crawler, page = fake(
        bank="esun", flags=[False] * (3 + 2 * guard_index) + [operand == "before"],
        origins=[True] * (5 + guard_index) + [operand != "origin"],
    )
    evaluator(monkeypatch, crawler)
    original_origin = crawler._credential_origin_allowed
    origin_calls = 0

    def origin(raw):
        nonlocal origin_calls
        origin_calls += 1
        result = original_origin(raw)
        if operand == "after" and origin_calls == 6 + guard_index:
            # Native origin inspection itself raises a JavaScript dialog.
            crawler.flags = iter([True])
        return result

    monkeypatch.setattr(crawler, "_credential_origin_allowed", origin)
    error = terminal(lambda: crawler._shared_login(page), crawler, suffix,
                     "玉山登入操作來源驗證：" + {
                         "before": "來源檢查前的原生JavaScript對話框阻擋",
                         "origin": "憑證來源檢查未通過",
                         "after": "來源檢查後的原生JavaScript對話框阻擋",
                     }[operand])
    clean = [*dialog(False), ("origin", True), *dialog(False)]
    expected = [*presubmit_prefix(), *dialog(False), ("origin", True), "submit"]
    for index in range(guard_index):
        expected += clean
        if index == 2:
            expected.append("click")
    expected += dialog(operand == "before")
    if operand != "before":
        expected.append(("origin", operand != "origin"))
    if operand == "after":
        expected += dialog(True)
    assert crawler.trace == expected
    assert error.budget == base.LoginBudget()
    assert error.phase is base.CheckpointPhase.PRE_SUBMIT


def test_esun_operation_guard_allows_unchanged_success(monkeypatch):
    crawler, page = fake(bank="esun")
    evaluator(monkeypatch, crawler)
    assert crawler._shared_login(page) is True
    clean = [*dialog(False), ("origin", True), *dialog(False)]
    expected = [*presubmit_prefix(), *dialog(False), ("origin", True), "submit",
                *clean, *clean, *clean, "click", *clean]
    for phase in ("post_submit", "post_submit_settle"):
        expected += [*dialog(False), ("origin", True), ("evaluate", phase),
                     ("origin", True), *dialog(False)]
    assert crawler.trace == expected


def test_final_step_budget_guard(monkeypatch):
    crawler, page = fake()
    evaluator(monkeypatch, crawler, exhaust=True)
    # Adversarial evaluator/validator boundary: force the final defensive guard;
    # the loop and the reducer (including its original budget) remain native.
    monkeypatch.setattr(base, "validate_login_checkpoint_outcome", lambda outcome, rules: outcome)
    error = terminal(lambda: crawler._shared_login(page), crawler, "03-960-001",
                     "登入檢查：檢查步數預算已耗盡")
    step = [*dialog(False), ("origin", True), ("evaluate", "pre_submit"),
            ("origin", True), *dialog(False)]
    assert crawler.trace == [("origin", True), "prepare", ("origin", True), *step * 8]
    assert error.budget == base.LoginBudget()
    assert error.phase is base.CheckpointPhase.PRE_SUBMIT


@pytest.mark.parametrize("blocked,origin,suffix", [
    (True, False, "07-970-001"), (False, False, "07-970-002"),
    (False, True, None),
])
def test_collect_origin_guard(blocked, origin, suffix):
    crawler, page = fake(flags=[blocked], origins=[origin])
    call = lambda: crawler._ensure_collect_origin(page)
    if suffix:
        error = terminal(call, crawler, suffix, "收集來源驗證：" + (
            "原生JavaScript對話框阻擋" if blocked else "憑證來源檢查未通過"))
        assert error.phase is base.CheckpointPhase.POST_SUBMIT_SETTLE
        assert error.budget == base.LoginBudget(credential_submissions=1)
    else:
        assert call() is None
    assert crawler.trace == [*dialog(blocked), *([] if blocked else [("origin", origin)])]


@pytest.mark.parametrize("marked,failures,submits,reloads", [
    (False, 1, 1, 0), (True, 5, 5, 4), (True, 4, 5, 4),
])
def test_original_submit_catch_and_retry_budget(marked, failures, submits, reloads):
    failure = RuntimeError("synthetic native failure")
    if marked:
        base.captcha_unreadable(failure)
    crawler, page = fake(failures=failures, failure=failure)
    if failures == submits:
        with pytest.raises(RuntimeError) as caught:
            crawler._submit_with_captcha_reloads(page, page)
        assert caught.value is failure
        assert failure.__cause__ is failure.__context__ is None
    else:
        assert crawler._submit_with_captcha_reloads(page, page) is None
    assert exception_error_id(failure) is None
    retry = ["submit", "reload", "prepare", *dialog(False), ("origin", True)]
    assert crawler.trace == [*retry * reloads, "submit", *([] if failures == submits else ["click"])]
    assert crawler.trace.count("submit") == submits
