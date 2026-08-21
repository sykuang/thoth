from __future__ import annotations

from dataclasses import dataclass, field, replace
from importlib import import_module
import inspect
from pathlib import Path
import re
from types import ModuleType, SimpleNamespace
from typing import ClassVar

import pytest

from backend.core.base import BankCollectResult, BankCrawler, ResponseCollector
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointOutcome,
    CheckpointPhase,
    DEFAULT_ACTION_SELECTOR,
    LoginCheckpointBlocked,
    LoginCheckpointRule,
)


BANK_MODULES = sorted((Path(__file__).parents[1] / "backend/banks").glob("*.py"))


def _module_uses_shared_login_checkpoints(module: ModuleType) -> bool:
    return any(
        inspect.isclass(candidate)
        and candidate is not BankCrawler
        and candidate.__module__ == module.__name__
        and issubclass(candidate, BankCrawler)
        and candidate.USES_SHARED_LOGIN_CHECKPOINTS is True
        for candidate in vars(module).values()
    )


def _opted_in_bank_modules() -> set[str]:
    return {
        path.stem
        for path in BANK_MODULES
        if path.stem != "__init__"
        and _module_uses_shared_login_checkpoints(
            import_module(f"backend.banks.{path.stem}")
        )
    }


@dataclass
class _StagedCrawler(BankCrawler):
    USES_SHARED_LOGIN_CHECKPOINTS = True
    CREDENTIAL_HOSTS = frozenset({"example.com"})
    events: list[str] = field(default_factory=list)
    submissions: int = 0
    rules: tuple[LoginCheckpointRule, ...] | None = None

    def _credential_origin_allowed(self, page) -> bool:
        return True

    def login(self, page) -> bool:
        raise AssertionError("legacy login must not run")

    def prepare_login_page(self, page) -> None:
        self.events.append("prepare-login-page")

    def is_authenticated(self, page) -> bool:
        return False

    def submit_credentials_once(self, page) -> None:
        self.submissions += 1
        self.events.append(f"submit-credentials:{self.submissions}")

    def prepare_captcha_resubmit(self, page) -> None:
        self.events.append("prepare-captcha-resubmit")

    def login_checkpoint_rules(self) -> tuple[LoginCheckpointRule, ...]:
        if self.rules is not None:
            return self.rules
        return (
            LoginCheckpointRule(
                name="staged-notice",
                bank=self.name,
                phases=(CheckpointPhase.POST_SUBMIT,),
                kind=CheckpointKind.DISMISSIBLE_NOTICE,
                container_selector="#notice",
                action_texts=("Continue",),
            ),
        )

    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        self.events.append("collect")
        return BankCollectResult(card_bill_facts_ok=False)

    def attach_shared_dialog_handler(self, page) -> None:
        self.events.append("attach-dialog")

    def logout(self, page) -> bool:
        self.events.append("logout")
        return True


def _run(monkeypatch, tmp_path, crawler: BankCrawler, evaluator):
    import backend.core.base as base_mod

    crawler.session_dir = tmp_path / f"{crawler.name}_session"
    crawler.session_dir.mkdir()
    page = SimpleNamespace(
        on=lambda *_args, **_kwargs: None,
        url="https://example.com/app",
        frames=[],
        reload=lambda: getattr(crawler, "events", []).append("reload"),
    )
    monkeypatch.setattr(base_mod, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(base_mod, "evaluate_login_checkpoint", evaluator)
    monkeypatch.setattr(
        base_mod.StealthyFetcher,
        "fetch",
        lambda _url, *, page_action, **_kwargs: page_action(page),
    )
    return crawler.run("https://example.com", headless=True), page


def _outcomes(*outcomes: CheckpointOutcome):
    pending = iter(outcomes)

    def evaluate(*_args, **_kwargs):
        return next(pending)

    return evaluate


def test_shared_dialog_handler_is_opaque_dismiss_only_and_terminal() -> None:
    crawler = _StagedCrawler(name="staged")
    callbacks = {}
    page = SimpleNamespace(on=lambda event, callback: callbacks.setdefault(event, callback))

    class Dialog:
        dismissed = 0

        @property
        def message(self):
            raise AssertionError("shared dialog handler must not inspect private text")

        def accept(self):
            raise AssertionError("shared dialog handler must never accept")

        def dismiss(self):
            self.dismissed += 1

    BankCrawler.attach_shared_dialog_handler(crawler, page)
    dialog = Dialog()
    callbacks["dialog"](dialog)

    assert dialog.dismissed == 1
    assert crawler._shared_dialog_blocked is True
    crawler.submissions = 0
    with pytest.raises(LoginCheckpointBlocked):
        crawler._shared_login(SimpleNamespace())
    assert crawler.submissions == 0


def test_dialog_during_protocol_evaluation_blocks_before_resubmit(monkeypatch) -> None:
    protocol = _rule(
        "protocol",
        CheckpointKind.PROTOCOL_RESUBMIT,
        phases=(CheckpointPhase.POST_SUBMIT,),
    )
    crawler = _StagedCrawler(name="staged", rules=(protocol,))
    crawler._shared_dialog_blocked = False
    calls = 0

    def evaluate(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS)
        crawler._shared_dialog_blocked = True
        return CheckpointOutcome(
            CheckpointKind.PROTOCOL_RESUBMIT,
            rule_name="protocol",
        )

    monkeypatch.setattr("backend.core.base.evaluate_login_checkpoint", evaluate)
    with pytest.raises(LoginCheckpointBlocked):
        crawler._shared_login(SimpleNamespace())

    assert crawler.submissions == 1


def _rule(
    name: str,
    kind: CheckpointKind,
    *,
    phases: tuple[CheckpointPhase, ...] = tuple(CheckpointPhase),
    max_actions: int = 1,
) -> LoginCheckpointRule:
    clickable = kind in {
        CheckpointKind.DISMISSIBLE_NOTICE,
        CheckpointKind.DUPLICATE_SESSION,
        CheckpointKind.PROTOCOL_RESUBMIT,
        CheckpointKind.PASSWORD_CHANGE_OPTIONAL,
    }
    return LoginCheckpointRule(
        name=name,
        bank="staged",
        phases=phases,
        kind=kind,
        container_selector=f"#{name}",
        action_texts=("Continue",) if clickable else (),
        max_actions=max_actions,
    )


def test_staged_login_happy_path_settles_before_collect(monkeypatch, tmp_path):
    crawler = _StagedCrawler(name="staged")
    outcomes = iter(
        (
            CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS),
            CheckpointOutcome(CheckpointKind.DISMISSIBLE_NOTICE, rule_name="staged-notice"),
            CheckpointOutcome(CheckpointKind.AUTHENTICATED),
            CheckpointOutcome(CheckpointKind.AUTHENTICATED),
        )
    )
    labels = iter(("pre-submit", "post-submit", "authenticated", "settle"))

    def evaluate(page, *, bank, phase, rules, is_authenticated, is_scope_owned=None):
        assert bank == "staged"
        assert phase in CheckpointPhase
        crawler.events.append(f"checkpoint:{next(labels)}")
        return next(outcomes)

    result, _ = _run(monkeypatch, tmp_path, crawler, evaluate)

    assert result["data"] == {"card_bill_facts_ok": False}
    assert crawler.events == [
        "attach-dialog",
        "prepare-login-page",
        "checkpoint:pre-submit",
        "submit-credentials:1",
        "checkpoint:post-submit",
        "checkpoint:authenticated",
        "checkpoint:settle",
        "collect",
        "logout",
    ]


def test_page_action_attaches_collector_before_dialog_and_prepare(monkeypatch, tmp_path):
    crawler = _StagedCrawler(name="staged", rules=())
    attached_collector: ResponseCollector | None = None

    def attach(collector, page):
        nonlocal attached_collector
        attached_collector = collector
        crawler.events.append("collector-attach")

    def attach_dialog(page):
        assert attached_collector is not None
        assert crawler.collector is attached_collector
        crawler.events.extend(("collector-assigned-at-dialog", "attach-dialog"))

    monkeypatch.setattr(ResponseCollector, "attach", attach)
    monkeypatch.setattr(crawler, "attach_shared_dialog_handler", attach_dialog)

    result, _ = _run(
        monkeypatch,
        tmp_path,
        crawler,
        _outcomes(
            CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS),
            CheckpointOutcome(CheckpointKind.AUTHENTICATED),
            CheckpointOutcome(CheckpointKind.AUTHENTICATED),
        ),
    )

    assert result["data"] == {"card_bill_facts_ok": False}
    assert crawler.events[:4] == [
        "collector-attach",
        "collector-assigned-at-dialog",
        "attach-dialog",
        "prepare-login-page",
    ]


def test_base_run_has_no_legacy_login_or_dialog_path() -> None:
    run_source = inspect.getsource(BankCrawler.run)
    assert "USES_SHARED_LOGIN_CHECKPOINTS" not in run_source
    assert "attach_dialog_handler" not in run_source
    assert "self.login(page)" not in run_source
    assert "self._shared_login(page)" in run_source
    assert "attach_dialog_handler" not in BankCrawler.__dict__
    assert "handle_dup_login_modal" not in BankCrawler.__dict__
    assert "should_kick_other_session" not in BankCrawler.__dict__


def test_run_redacts_untyped_login_exception_text(
    monkeypatch, tmp_path, capsys
) -> None:
    crawler = _StagedCrawler(name="staged")

    def fail(_page):
        raise RuntimeError("PRIVATE-LOGIN-DOM-987654")

    monkeypatch.setattr(crawler, "prepare_login_page", fail)
    result, _ = _run(
        monkeypatch,
        tmp_path,
        crawler,
        lambda *_args, **_kwargs: CheckpointOutcome(
            CheckpointKind.READY_FOR_CREDENTIALS
        ),
    )

    assert result["error"] == "RuntimeError: login failed"
    assert "PRIVATE" not in result["error"]
    assert "PRIVATE" not in capsys.readouterr().err


def test_run_keeps_valid_machine_readable_login_error_code(
    monkeypatch, tmp_path, capsys
) -> None:
    crawler = _StagedCrawler(name="staged")

    class CodedLoginError(RuntimeError):
        safe_code = "captcha_ocr_failed"

    def fail(_page):
        raise CodedLoginError("PRIVATE-LOGIN-DOM-987654")

    monkeypatch.setattr(crawler, "prepare_login_page", fail)
    result, _ = _run(
        monkeypatch,
        tmp_path,
        crawler,
        lambda *_args, **_kwargs: CheckpointOutcome(
            CheckpointKind.READY_FOR_CREDENTIALS
        ),
    )

    assert result["error"] == "CodedLoginError: code=captcha_ocr_failed"
    assert "PRIVATE" not in result["error"]
    assert "PRIVATE" not in capsys.readouterr().err


@pytest.mark.parametrize("safe_code", ("privateaccount1234", object()))
def test_run_rejects_unregistered_or_non_string_login_error_codes(
    monkeypatch, tmp_path, capsys, safe_code
) -> None:
    crawler = _StagedCrawler(name="staged")

    class CodedLoginError(RuntimeError):
        safe_code: object

    error = CodedLoginError("PRIVATE-LOGIN-DOM-987654")
    error.safe_code = safe_code

    def fail(_page):
        raise error

    monkeypatch.setattr(crawler, "prepare_login_page", fail)
    result, _ = _run(
        monkeypatch,
        tmp_path,
        crawler,
        lambda *_args, **_kwargs: CheckpointOutcome(
            CheckpointKind.READY_FOR_CREDENTIALS
        ),
    )

    assert result["error"] == "CodedLoginError: login failed"
    assert "PRIVATE" not in result["error"]
    assert "PRIVATE" not in capsys.readouterr().err


def test_run_redacts_collect_and_logout_exception_details(
    monkeypatch, tmp_path, capsys
) -> None:
    crawler = _StagedCrawler(name="staged")

    def fail_collect(_page, _collector):
        raise RuntimeError("PRIVATE-COLLECT-DOM-987654")

    def fail_logout(_page):
        raise RuntimeError("PRIVATE-LOGOUT-DOM-654321")

    monkeypatch.setattr(crawler, "collect", fail_collect)
    monkeypatch.setattr(crawler, "logout", fail_logout)
    result, _ = _run(
        monkeypatch,
        tmp_path,
        crawler,
        _outcomes(
            CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS),
            CheckpointOutcome(CheckpointKind.AUTHENTICATED),
            CheckpointOutcome(CheckpointKind.AUTHENTICATED),
        ),
    )

    stderr = capsys.readouterr().err
    assert result["error"] == "collect_failed: RuntimeError"
    assert "PRIVATE" not in repr(result)
    assert "PRIVATE" not in stderr
    assert "Traceback" not in stderr


def test_default_logout_logs_only_fixed_status(capsys) -> None:
    crawler = _StagedCrawler(name="staged")
    frame = SimpleNamespace(
        url="https://example.com/PRIVATE?token=PRIVATE",
        evaluate=lambda _script: "PRIVATE Logout label",
    )
    page = SimpleNamespace(frames=[frame], wait_for_timeout=lambda _milliseconds: None)

    assert BankCrawler.logout(crawler, page)

    stderr = capsys.readouterr().err
    assert "PRIVATE" not in stderr
    assert "已點登出控制" in stderr


def test_default_logout_redacts_frame_errors_and_urls(capsys) -> None:
    crawler = _StagedCrawler(name="staged")

    def fail(_script):
        raise RuntimeError("PRIVATE-LOGOUT-DOM-987654")

    frame = SimpleNamespace(
        url="https://example.com/PRIVATE?token=PRIVATE",
        evaluate=fail,
    )
    page = SimpleNamespace(frames=[frame], wait_for_timeout=lambda _milliseconds: None)

    assert not BankCrawler.logout(crawler, page)

    stderr = capsys.readouterr().err
    assert "PRIVATE" not in stderr
    assert "RuntimeError" in stderr


def test_run_recovery_hook_exception_fails_closed_without_leaking(
    monkeypatch, tmp_path, capsys
) -> None:
    crawler = _StagedCrawler(name="staged")

    def fail_login(_page):
        raise RuntimeError("ORIGINAL-LOGIN-DOM-987654")

    def fail_recovery(_page, _error):
        raise RuntimeError("PRIVATE-RECOVERY-DOM-654321")

    monkeypatch.setattr(crawler, "prepare_login_page", fail_login)
    monkeypatch.setattr(crawler, "_recover_late_authentication", fail_recovery)
    result, _ = _run(
        monkeypatch,
        tmp_path,
        crawler,
        lambda *_args, **_kwargs: CheckpointOutcome(
            CheckpointKind.READY_FOR_CREDENTIALS
        ),
    )

    assert result["error"] == "RuntimeError: login failed"
    assert "PRIVATE" not in result["error"]
    assert "PRIVATE" not in capsys.readouterr().err


def test_shared_login_checkpoint_opt_in_inventory():
    assert _opted_in_bank_modules() == {
        "cathay",
        "ctbc",
        "dbs",
        "esun",
        "fubon",
        "hsbc",
        "linebank",
        "rakuten",
        "scb",
        "scsb",
        "sinopac",
        "taishin",
        "ubot",
    }


def test_production_credential_origins_are_exact_https() -> None:
    crawlers = []
    for path in BANK_MODULES:
        if path.stem == "__init__":
            continue
        module = import_module(f"backend.banks.{path.stem}")
        crawlers.extend(
            candidate
            for candidate in vars(module).values()
            if inspect.isclass(candidate)
            and candidate is not BankCrawler
            and candidate.__module__ == module.__name__
            and issubclass(candidate, BankCrawler)
            and candidate.USES_SHARED_LOGIN_CHECKPOINTS is True
        )

    assert len(crawlers) == 13
    for crawler_type in crawlers:
        crawler = object.__new__(crawler_type)
        assert crawler.CREDENTIAL_HOSTS
        for host in crawler.CREDENTIAL_HOSTS:
            assert BankCrawler._credential_origin_allowed(
                crawler, SimpleNamespace(url=f"https://{host}/")
            )
            assert BankCrawler._credential_origin_allowed(
                crawler, SimpleNamespace(url=f"https://{host}:443/")
            )
            for unsafe in (
                f"http://{host}/",
                f"https://{host}.evil.example/",
                f"https://user@{host}/",
                f"https://{host}:444/",
            ):
                assert not BankCrawler._credential_origin_allowed(
                    crawler, SimpleNamespace(url=unsafe)
                )


def test_origin_change_after_pre_checkpoint_blocks_before_credentials(monkeypatch) -> None:
    crawler = _StagedCrawler(name="staged")
    crawler._credential_origin_allowed = BankCrawler._credential_origin_allowed.__get__(crawler)
    page = SimpleNamespace(url="https://example.com/", frames=[])

    def evaluate(*_args, **_kwargs):
        page.url = "https://evil.example/"
        return CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS)

    monkeypatch.setattr("backend.core.base.evaluate_login_checkpoint", evaluate)
    with pytest.raises(LoginCheckpointBlocked) as error:
        crawler._shared_login(page)

    assert error.value.outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
    assert crawler.submissions == 0


def test_post_submit_origin_change_blocks_authentication_and_collection(
    monkeypatch, tmp_path
) -> None:
    crawler = _StagedCrawler(name="staged")
    crawler._credential_origin_allowed = BankCrawler._credential_origin_allowed.__get__(crawler)
    evaluations = 0

    def evaluate(page, **_kwargs):
        nonlocal evaluations
        evaluations += 1
        if evaluations == 1:
            return CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS)
        page.url = "http://example.com/dashboard"
        return CheckpointOutcome(CheckpointKind.AUTHENTICATED)

    result, _ = _run(monkeypatch, tmp_path, crawler, evaluate)

    assert crawler.submissions == 1
    assert "collect" not in crawler.events
    assert "kind=unknown_blocker" in result["error"]


def test_collect_origin_drift_discards_data_and_skips_logout(
    monkeypatch, tmp_path
) -> None:
    crawler = _StagedCrawler(name="staged")
    crawler._credential_origin_allowed = BankCrawler._credential_origin_allowed.__get__(crawler)

    def collect(page, _collector):
        page.url = "http://example.com/dashboard?PRIVATE-URL-MARKER"
        crawler.events.append("collect-origin-drift")
        return BankCollectResult(card_bill_facts_ok=False)

    monkeypatch.setattr(crawler, "collect", collect)
    result, _ = _run(
        monkeypatch,
        tmp_path,
        crawler,
        _outcomes(
            CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS),
            CheckpointOutcome(CheckpointKind.AUTHENTICATED),
            CheckpointOutcome(CheckpointKind.AUTHENTICATED),
        ),
    )

    assert crawler.submissions == 1
    assert "collect-origin-drift" not in crawler.events
    assert "data" not in result
    assert "kind=unknown_blocker" in result["error"]
    assert "PRIVATE-URL-MARKER" not in repr(result)
    assert "logout" not in crawler.events


@pytest.mark.parametrize(
    "annotations",
    [{}, {"USES_SHARED_LOGIN_CHECKPOINTS": ClassVar[bool]}],
)
def test_shared_login_checkpoint_inventory_detects_flag_spelling(
    annotations: dict[str, object],
) -> None:
    module = ModuleType(f"test_bank_{len(annotations)}")
    crawler = type(
        "FixtureCrawler",
        (BankCrawler,),
        {
            "__module__": module.__name__,
            "__annotations__": annotations,
            "USES_SHARED_LOGIN_CHECKPOINTS": True,
        },
    )
    setattr(module, "FixtureCrawler", crawler)

    assert _module_uses_shared_login_checkpoints(module)


@pytest.mark.parametrize(
    ("kind", "error_type"),
    [
        (CheckpointKind.OTP_REQUIRED, "LoginInteractionRequired"),
        (CheckpointKind.PASSWORD_CHANGE_REQUIRED, "LoginInteractionRequired"),
        (CheckpointKind.EXPLICIT_LOGIN_ERROR, "LoginCheckpointBlocked"),
        (CheckpointKind.UNKNOWN_BLOCKER, "LoginCheckpointBlocked"),
    ],
)
def test_terminal_checkpoint_stops_after_one_submission(
    monkeypatch,
    tmp_path,
    kind: CheckpointKind,
    error_type: str,
):
    terminal_rule = None if kind is CheckpointKind.UNKNOWN_BLOCKER else _rule("terminal", kind)
    crawler = _StagedCrawler(
        name="staged",
        rules=() if terminal_rule is None else (terminal_rule,),
    )
    result, _ = _run(
        monkeypatch,
        tmp_path,
        crawler,
        _outcomes(
            CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS),
            CheckpointOutcome(
                kind,
                rule_name=terminal_rule.name if terminal_rule else None,
                action_label="secret action",
                interaction="secret DOM body",
            ),
        ),
    )

    assert crawler.submissions == 1
    assert "collect" not in crawler.events
    assert "logout" not in crawler.events
    assert error_type in result["error"]
    assert f"kind={kind}" in result["error"]
    assert "credential_submissions=1" in result["error"]
    assert "protocol_resubmits=0" in result["error"]
    assert "captcha_resubmits=0" in result["error"]
    assert "reloads=0" in result["error"]
    assert "secret action" not in result["error"]
    assert "secret DOM body" not in result["error"]


def test_second_pre_submit_recovery_blocks_before_second_reload(monkeypatch, tmp_path):
    startup_rule = _rule("startup", CheckpointKind.STARTUP_RECOVERY)
    crawler = _StagedCrawler(name="staged", rules=(startup_rule,))
    recovery = CheckpointOutcome(CheckpointKind.STARTUP_RECOVERY, rule_name="startup")

    result, _ = _run(
        monkeypatch,
        tmp_path,
        crawler,
        _outcomes(recovery, recovery),
    )

    assert crawler.submissions == 0
    assert crawler.events.count("reload") == 1
    assert crawler.events.count("prepare-login-page") == 2
    assert "LoginCheckpointBlocked" in result["error"]
    assert "collect" not in crawler.events
    assert "logout" not in crawler.events


def test_post_submit_recovery_never_resubmits(monkeypatch, tmp_path):
    startup_rule = _rule("startup", CheckpointKind.STARTUP_RECOVERY)
    crawler = _StagedCrawler(name="staged", rules=(startup_rule,))
    recovery = CheckpointOutcome(CheckpointKind.STARTUP_RECOVERY, rule_name="startup")

    result, _ = _run(
        monkeypatch,
        tmp_path,
        crawler,
        _outcomes(
            CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS),
            recovery,
            recovery,
        ),
    )

    assert crawler.submissions == 1
    assert crawler.events.count("reload") == 1
    assert crawler.events.count("prepare-login-page") == 2
    assert "credential_submissions=1" in result["error"]
    assert "collect" not in crawler.events
    assert "logout" not in crawler.events


def test_exhausted_clickable_rule_becomes_classifier_only_shadow(monkeypatch, tmp_path):
    body_pattern = re.compile(r"^known blocker$")
    rule = LoginCheckpointRule(
        name="duplicate",
        bank="staged",
        phases=tuple(CheckpointPhase),
        kind=CheckpointKind.DUPLICATE_SESSION,
        container_selector="#duplicate",
        action_selector="#continue",
        action_texts=("Continue",),
        required_body_pattern=body_pattern,
    )
    crawler = _StagedCrawler(name="staged", rules=(rule,))
    rules_seen: list[tuple[LoginCheckpointRule, ...]] = []

    def evaluate(page, *, bank, phase, rules, is_authenticated, is_scope_owned=None):
        rules_seen.append(rules)
        if len(rules_seen) == 1:
            return CheckpointOutcome(
                CheckpointKind.DUPLICATE_SESSION,
                rule_name="duplicate",
            )
        if len(rules_seen) == 2:
            return CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS)
        return CheckpointOutcome(CheckpointKind.AUTHENTICATED)

    result, _ = _run(monkeypatch, tmp_path, crawler, evaluate)

    shadow = rules_seen[1][0]
    assert rules_seen[0] == (rule,)
    assert all(seen == (shadow,) for seen in rules_seen[1:])
    assert (
        shadow.name,
        shadow.bank,
        shadow.phases,
        shadow.kind,
        shadow.container_selector,
        shadow.required_body_pattern,
        shadow.action_selector,
        shadow.action_texts,
        shadow.max_actions,
    ) == (
        rule.name,
        rule.bank,
        rule.phases,
        CheckpointKind.UNKNOWN_BLOCKER,
        rule.container_selector,
        body_pattern,
        DEFAULT_ACTION_SELECTOR,
        (),
        1,
    )
    assert crawler.submissions == 1
    assert result["data"] == {"card_bill_facts_ok": False}


def test_protocol_resubmit_allows_exactly_one_second_submission(monkeypatch, tmp_path):
    rule = _rule(
        "protocol",
        CheckpointKind.PROTOCOL_RESUBMIT,
        phases=(CheckpointPhase.POST_SUBMIT,),
        max_actions=2,
    )
    crawler = _StagedCrawler(name="staged", rules=(rule,))
    request = CheckpointOutcome(
        CheckpointKind.PROTOCOL_RESUBMIT,
        rule_name="protocol",
    )
    rules_seen: list[tuple[LoginCheckpointRule, ...]] = []
    pending = iter(
        (
            CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS),
            request,
            request,
        )
    )

    def evaluate(page, *, bank, phase, rules, is_authenticated, is_scope_owned=None):
        rules_seen.append(rules)
        return next(pending)

    result, _ = _run(
        monkeypatch,
        tmp_path,
        crawler,
        evaluate,
    )

    assert rules_seen[0] == ()
    assert rules_seen[1] == (rule,)
    assert len(rules_seen[2]) == 1
    assert rules_seen[2][0].name == rule.name
    assert rules_seen[2][0].kind is CheckpointKind.UNKNOWN_BLOCKER
    assert rules_seen[2][0].action_texts == ()
    assert crawler.submissions == 2
    assert "credential_submissions=2" in result["error"]
    assert "protocol_resubmits=1" in result["error"]
    assert "collect" not in crawler.events
    assert "logout" not in crawler.events


def test_exhausted_captcha_classifier_becomes_unknown_shadow(monkeypatch, tmp_path):
    body_pattern = re.compile(r"^captcha invalid$")
    rule = LoginCheckpointRule(
        name="captcha",
        bank="staged",
        phases=(CheckpointPhase.POST_SUBMIT,),
        kind=CheckpointKind.CAPTCHA_RETRY,
        container_selector="#captcha",
        required_body_pattern=body_pattern,
    )
    crawler = _StagedCrawler(name="staged", rules=(rule,))
    rules_seen: list[tuple[LoginCheckpointRule, ...]] = []

    def evaluate(page, *, bank, phase, rules, is_authenticated, is_scope_owned=None):
        rules_seen.append(rules)
        if phase is CheckpointPhase.PRE_SUBMIT:
            return CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS)
        active = rules[0]
        return CheckpointOutcome(active.kind, rule_name=active.name)

    result, _ = _run(monkeypatch, tmp_path, crawler, evaluate)

    shadow = rules_seen[2][0]
    assert rules_seen[1] == (rule,)
    assert (
        shadow.name,
        shadow.bank,
        shadow.phases,
        shadow.kind,
        shadow.container_selector,
        shadow.required_body_pattern,
        shadow.action_texts,
    ) == (
        rule.name,
        rule.bank,
        rule.phases,
        CheckpointKind.UNKNOWN_BLOCKER,
        rule.container_selector,
        body_pattern,
        (),
    )
    assert crawler.submissions == 2
    assert crawler.events.count("prepare-captcha-resubmit") == 1
    assert "kind=unknown_blocker" in result["error"]
    assert "captcha_resubmits=1" in result["error"]
    assert "collect" not in crawler.events


def test_real_protocol_rule_is_removed_after_one_resubmit(monkeypatch, tmp_path):
    from patchright.sync_api import sync_playwright

    import backend.core.base as base_mod

    rule = _rule(
        "protocol",
        CheckpointKind.PROTOCOL_RESUBMIT,
        phases=(CheckpointPhase.POST_SUBMIT,),
        max_actions=3,
    )
    crawler = _StagedCrawler(name="staged", rules=(rule,))
    crawler.session_dir = tmp_path / "staged_session"
    crawler.session_dir.mkdir()
    monkeypatch.setattr(base_mod, "DATA_ROOT", tmp_path)

    with sync_playwright() as patchright:
        if not Path(patchright.chromium.executable_path).exists():
            pytest.skip("Patchright browser binary is not installed")
        browser = patchright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(
                """
                <button id="credentials">Submit credentials</button>
                <div id="protocol" hidden>
                  <button>Continue</button>
                </div>
                <script>
                  document.body.dataset.protocolClicks = '0';
                  document.querySelector('#credentials').onclick = () => {
                    document.querySelector('#protocol').hidden = false;
                  };
                  document.querySelector('#protocol button').onclick = () => {
                    document.body.dataset.protocolClicks = String(
                      Number(document.body.dataset.protocolClicks) + 1
                    );
                    document.querySelector('#protocol').hidden = true;
                  };
                </script>
                """
            )

            def submit(page):
                crawler.submissions += 1
                crawler.events.append(f"submit-credentials:{crawler.submissions}")
                page.locator("#credentials").click()

            monkeypatch.setattr(crawler, "submit_credentials_once", submit)
            monkeypatch.setattr(
                base_mod.StealthyFetcher,
                "fetch",
                lambda _url, *, page_action, **_kwargs: page_action(page),
            )

            result = crawler.run("https://example.com", headless=True)

            assert crawler.submissions == 2
            assert page.locator("body").get_attribute("data-protocol-clicks") == "1"
            assert "LoginCheckpointBlocked" in result["error"]
            assert "collect" not in crawler.events
            assert "logout" not in crawler.events
        finally:
            browser.close()


def test_rule_owned_outcome_must_match_active_rule_kind(monkeypatch, tmp_path):
    otp_rule = _rule("otp-only", CheckpointKind.OTP_REQUIRED)
    crawler = _StagedCrawler(name="staged", rules=(otp_rule,))

    result, _ = _run(
        monkeypatch,
        tmp_path,
        crawler,
        _outcomes(
            CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS),
            CheckpointOutcome(
                CheckpointKind.PROTOCOL_RESUBMIT,
                rule_name="otp-only",
            ),
        ),
    )

    assert crawler.submissions == 1
    assert "kind=unknown_blocker" in result["error"]
    assert "rule_name" not in result["error"]
    assert "collect" not in crawler.events
    assert "logout" not in crawler.events


def test_unknown_outcome_may_name_active_different_kind_rule(monkeypatch, tmp_path):
    otp_rule = _rule("otp-only", CheckpointKind.OTP_REQUIRED)
    crawler = _StagedCrawler(name="staged", rules=(otp_rule,))

    result, _ = _run(
        monkeypatch,
        tmp_path,
        crawler,
        _outcomes(
            CheckpointOutcome(
                CheckpointKind.UNKNOWN_BLOCKER,
                rule_name="otp-only",
            ),
        ),
    )

    assert crawler.submissions == 0
    assert "kind=unknown_blocker, rule_name=otp-only" in result["error"]
    assert "collect" not in crawler.events
    assert "logout" not in crawler.events


def test_captcha_retry_allows_exactly_one_bank_coded_second_submission(
    monkeypatch,
    tmp_path,
):
    rule = _rule("captcha", CheckpointKind.CAPTCHA_RETRY)
    crawler = _StagedCrawler(name="staged", rules=(rule,))
    request = CheckpointOutcome(CheckpointKind.CAPTCHA_RETRY, rule_name="captcha")

    result, _ = _run(
        monkeypatch,
        tmp_path,
        crawler,
        _outcomes(
            CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS),
            request,
            request,
        ),
    )

    assert crawler.submissions == 2
    assert crawler.events.count("prepare-captcha-resubmit") == 1
    assert crawler.events.index("prepare-captcha-resubmit") < crawler.events.index(
        "submit-credentials:2"
    )
    assert "credential_submissions=2" in result["error"]
    assert "captcha_resubmits=1" in result["error"]
    assert "collect" not in crawler.events
    assert "logout" not in crawler.events


def test_phase_ineligible_captcha_rule_cannot_authorize_resubmit(monkeypatch, tmp_path):
    rule = _rule(
        "settle-captcha",
        CheckpointKind.CAPTCHA_RETRY,
        phases=(CheckpointPhase.POST_SUBMIT_SETTLE,),
    )
    crawler = _StagedCrawler(name="staged", rules=(rule,))
    rules_seen: list[tuple[str, ...]] = []
    pending = iter(
        (
            CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS),
            CheckpointOutcome(
                CheckpointKind.CAPTCHA_RETRY,
                rule_name="settle-captcha",
            ),
        )
    )

    def evaluate(page, *, bank, phase, rules, is_authenticated, is_scope_owned=None):
        rules_seen.append(tuple(item.name for item in rules))
        return next(pending)

    result, _ = _run(monkeypatch, tmp_path, crawler, evaluate)

    assert rules_seen == [(), ()]
    assert crawler.submissions == 1
    assert "kind=unknown_blocker" in result["error"]
    assert "captcha_resubmits=0" in result["error"]
    assert "collect" not in crawler.events
    assert "logout" not in crawler.events


def test_captcha_words_in_unknown_outcome_never_request_resubmit(monkeypatch, tmp_path):
    crawler = _StagedCrawler(name="staged", rules=())

    result, _ = _run(
        monkeypatch,
        tmp_path,
        crawler,
        _outcomes(
            CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS),
            CheckpointOutcome(
                CheckpointKind.UNKNOWN_BLOCKER,
                rule_name="captcha-invalid-text",
            ),
        ),
    )

    assert crawler.submissions == 1
    assert "kind=unknown_blocker" in result["error"]
    assert "captcha_resubmits=0" in result["error"]


def test_settle_notice_is_dismissed_before_collect(monkeypatch, tmp_path):
    rule = _rule(
        "settle-notice",
        CheckpointKind.DISMISSIBLE_NOTICE,
        phases=(CheckpointPhase.POST_SUBMIT_SETTLE,),
    )
    crawler = _StagedCrawler(name="staged", rules=(rule,))
    phases: list[CheckpointPhase] = []
    pending = iter(
        (
            CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS),
            CheckpointOutcome(CheckpointKind.AUTHENTICATED),
            CheckpointOutcome(
                CheckpointKind.DISMISSIBLE_NOTICE,
                rule_name="settle-notice",
            ),
            CheckpointOutcome(CheckpointKind.AUTHENTICATED),
        )
    )

    def evaluate(page, *, bank, phase, rules, is_authenticated, is_scope_owned=None):
        phases.append(phase)
        return next(pending)

    result, _ = _run(monkeypatch, tmp_path, crawler, evaluate)

    assert result["data"] == {"card_bill_facts_ok": False}
    assert phases == [
        CheckpointPhase.PRE_SUBMIT,
        CheckpointPhase.POST_SUBMIT,
        CheckpointPhase.POST_SUBMIT_SETTLE,
        CheckpointPhase.POST_SUBMIT_SETTLE,
    ]
    assert crawler.events[-2:] == ["collect", "logout"]


def test_real_settle_notice_is_clicked_before_authenticated_collect(monkeypatch, tmp_path):
    from patchright.sync_api import sync_playwright

    import backend.core.base as base_mod

    rule = _rule(
        "settle-notice",
        CheckpointKind.DISMISSIBLE_NOTICE,
        phases=(CheckpointPhase.POST_SUBMIT_SETTLE,),
    )
    crawler = _StagedCrawler(name="staged", rules=(rule,))
    crawler.session_dir = tmp_path / "staged_session"
    crawler.session_dir.mkdir()
    monkeypatch.setattr(base_mod, "DATA_ROOT", tmp_path)

    with sync_playwright() as patchright:
        if not Path(patchright.chromium.executable_path).exists():
            pytest.skip("Patchright browser binary is not installed")
        browser = patchright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(
                """
                <button id="credentials">Submit credentials</button>
                <div id="settle-notice" hidden>
                  <button>Continue</button>
                </div>
                <script>
                  document.body.dataset.authenticated = 'no';
                  document.body.dataset.noticeClicks = '0';
                  document.querySelector('#credentials').onclick = () => {
                    document.body.dataset.authenticated = 'yes';
                    document.querySelector('#settle-notice').hidden = false;
                  };
                  document.querySelector('#settle-notice button').onclick = () => {
                    document.body.dataset.noticeClicks = String(
                      Number(document.body.dataset.noticeClicks) + 1
                    );
                    document.querySelector('#settle-notice').hidden = true;
                  };
                </script>
                """
            )
            monkeypatch.setattr(
                crawler,
                "is_authenticated",
                lambda page: page.locator("body").get_attribute("data-authenticated") == "yes",
            )

            def submit(page):
                crawler.submissions += 1
                crawler.events.append(f"submit-credentials:{crawler.submissions}")
                page.locator("#credentials").click()

            monkeypatch.setattr(crawler, "submit_credentials_once", submit)
            monkeypatch.setattr(
                base_mod.StealthyFetcher,
                "fetch",
                lambda _url, *, page_action, **_kwargs: page_action(page),
            )

            result = crawler.run("https://example.com", headless=True)

            assert result["data"] == {"card_bill_facts_ok": False}
            assert page.locator("body").get_attribute("data-notice-clicks") == "1"
            assert crawler.submissions == 1
            assert crawler.events[-2:] == ["collect", "logout"]
        finally:
            browser.close()


def test_duplicate_rule_names_fail_after_prepare_before_evaluation(monkeypatch, tmp_path):
    duplicate = _rule("duplicate-name", CheckpointKind.DUPLICATE_SESSION)
    crawler = _StagedCrawler(name="staged", rules=(duplicate, duplicate))
    evaluator_calls = 0

    def evaluate(*_args, **_kwargs):
        nonlocal evaluator_calls
        evaluator_calls += 1
        raise AssertionError("duplicate rules must fail before evaluation")

    result, _ = _run(monkeypatch, tmp_path, crawler, evaluate)

    assert crawler.events == ["attach-dialog", "prepare-login-page"]
    assert evaluator_calls == 0
    assert crawler.submissions == 0
    assert "LoginCheckpointBlocked" in result["error"]


def test_foreign_bank_rule_fails_after_prepare_before_evaluation_or_auth(
    monkeypatch,
    tmp_path,
):
    foreign = replace(
        _rule("foreign-rule", CheckpointKind.OTP_REQUIRED),
        bank="foreign",
    )
    crawler = _StagedCrawler(name="staged", rules=(foreign,))
    evaluator_calls = 0
    auth_calls = 0

    def evaluate(*_args, **_kwargs):
        nonlocal evaluator_calls
        evaluator_calls += 1
        raise AssertionError("foreign rule must fail before evaluation")

    def is_authenticated(_page):
        nonlocal auth_calls
        auth_calls += 1
        return True

    monkeypatch.setattr(crawler, "is_authenticated", is_authenticated)
    result, _ = _run(monkeypatch, tmp_path, crawler, evaluate)

    assert crawler.events == ["attach-dialog", "prepare-login-page"]
    assert evaluator_calls == 0
    assert auth_calls == 0
    assert crawler.submissions == 0
    assert "LoginCheckpointBlocked" in result["error"]
    assert "kind=unknown_blocker" in result["error"]
    assert "collect" not in crawler.events
    assert "logout" not in crawler.events


def test_login_loop_has_a_fixed_safe_bound(monkeypatch, tmp_path):
    optional_rule = _rule(
        "optional-password",
        CheckpointKind.PASSWORD_CHANGE_OPTIONAL,
        max_actions=7,
    )
    crawler = _StagedCrawler(name="staged", rules=(optional_rule,))
    evaluator_calls = 0

    def evaluate(*_args, **_kwargs):
        nonlocal evaluator_calls
        evaluator_calls += 1
        return CheckpointOutcome(
            CheckpointKind.PASSWORD_CHANGE_OPTIONAL,
            rule_name="optional-password",
        )

    result, _ = _run(monkeypatch, tmp_path, crawler, evaluate)

    assert evaluator_calls == 8
    assert crawler.submissions == 0
    assert "LoginCheckpointBlocked" in result["error"]
    assert "kind=unknown_blocker" in result["error"]
    assert "collect" not in crawler.events
    assert "logout" not in crawler.events


@dataclass
class _MissingStagedMethodsCrawler(BankCrawler):
    USES_SHARED_LOGIN_CHECKPOINTS = True
    CREDENTIAL_HOSTS = frozenset({"example.com"})
    events: list[str] = field(default_factory=list)

    def login(self, page) -> bool:
        self.events.append("legacy-login")
        return True

    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        self.events.append("collect")
        return BankCollectResult(card_bill_facts_ok=False)

    def attach_shared_dialog_handler(self, page) -> None:
        self.events.append("attach-dialog")

    def logout(self, page) -> bool:
        self.events.append("logout")
        return True


def test_missing_opt_in_method_is_safe_and_never_falls_back(monkeypatch, tmp_path):
    crawler = _MissingStagedMethodsCrawler(name="missing")

    result, _ = _run(
        monkeypatch,
        tmp_path,
        crawler,
        lambda *_args, **_kwargs: CheckpointOutcome(CheckpointKind.AUTHENTICATED),
    )

    assert crawler.events == ["attach-dialog"]
    assert result["error"].startswith("NotImplementedError:")
    assert "final_url" not in result


def test_rules_keep_order_and_twelve_action_budget_is_enforced(monkeypatch, tmp_path):
    action_rule = _rule(
        "announcement",
        CheckpointKind.DUPLICATE_SESSION,
        max_actions=12,
    )
    classifier_rule = _rule("otp", CheckpointKind.OTP_REQUIRED)
    crawler = _StagedCrawler(
        name="staged",
        rules=(action_rule, classifier_rule),
    )
    rules_seen: list[tuple[tuple[str, CheckpointKind], ...]] = []

    def evaluate(page, *, bank, phase, rules, is_authenticated, is_scope_owned=None):
        assert bank == "staged"
        assert all(rule.bank == bank for rule in rules)
        rules_seen.append(tuple((rule.name, rule.kind) for rule in rules))
        if len(rules_seen) <= 12:
            return CheckpointOutcome(
                CheckpointKind.DUPLICATE_SESSION,
                rule_name="announcement",
            )
        return CheckpointOutcome(
            CheckpointKind.UNKNOWN_BLOCKER,
            rule_name="announcement",
        )

    result, _ = _run(monkeypatch, tmp_path, crawler, evaluate)

    assert rules_seen[:12] == [
        (
            ("announcement", CheckpointKind.DUPLICATE_SESSION),
            ("otp", CheckpointKind.OTP_REQUIRED),
        )
    ] * 12
    assert rules_seen[12] == (
        ("announcement", CheckpointKind.UNKNOWN_BLOCKER),
        ("otp", CheckpointKind.OTP_REQUIRED),
    )
    assert crawler.submissions == 0
    assert "LoginCheckpointBlocked" in result["error"]


def test_twelve_actions_still_leave_room_for_successful_login(monkeypatch, tmp_path):
    action_rule = _rule(
        "announcement",
        CheckpointKind.DUPLICATE_SESSION,
        max_actions=12,
    )
    classifier_rule = _rule("otp", CheckpointKind.OTP_REQUIRED)
    crawler = _StagedCrawler(
        name="staged",
        rules=(action_rule, classifier_rule),
    )
    calls: list[tuple[CheckpointPhase, tuple[str, ...]]] = []

    def evaluate(page, *, bank, phase, rules, is_authenticated, is_scope_owned=None):
        assert bank == "staged"
        assert all(rule.bank == bank for rule in rules)
        names = tuple(rule.name for rule in rules)
        calls.append((phase, names))
        if len(calls) <= 12:
            assert phase is CheckpointPhase.PRE_SUBMIT
            assert names == ("announcement", "otp")
            return CheckpointOutcome(
                CheckpointKind.DUPLICATE_SESSION,
                rule_name="announcement",
            )
        assert names == ("announcement", "otp")
        assert rules[0].kind is CheckpointKind.UNKNOWN_BLOCKER
        if len(calls) == 13:
            assert phase is CheckpointPhase.PRE_SUBMIT
            return CheckpointOutcome(CheckpointKind.READY_FOR_CREDENTIALS)
        assert phase is (
            CheckpointPhase.POST_SUBMIT
            if len(calls) == 14
            else CheckpointPhase.POST_SUBMIT_SETTLE
        )
        return CheckpointOutcome(CheckpointKind.AUTHENTICATED)

    result, _ = _run(monkeypatch, tmp_path, crawler, evaluate)

    assert result["data"] == {"card_bill_facts_ok": False}
    assert crawler.submissions == 1
    assert len(calls) == 15
    assert [phase for phase, _ in calls] == [
        *([CheckpointPhase.PRE_SUBMIT] * 13),
        CheckpointPhase.POST_SUBMIT,
        CheckpointPhase.POST_SUBMIT_SETTLE,
    ]
    assert crawler.events[-2:] == ["collect", "logout"]
