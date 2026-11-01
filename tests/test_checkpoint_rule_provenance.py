"""Natural checkpoint matches must retain source-owned IDs at the real run sink."""
from types import SimpleNamespace

import pytest

from backend.banks.ubot import UbotCrawler
from backend.core import base, login_checkpoints as cp
from backend.core.error_codes import BANKS, format_error_id
from tests.test_login_checkpoint_browser_rules import Node, Page

from tests.test_sync_error_id_survival import offline as offline


@pytest.fixture(autouse=True)
def no_local_configuration(monkeypatch):
    # The isolated ASGI fixture supplies its own settings; never open local .env.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)


@pytest.mark.parametrize("name,expected", [
    ("ubot-unknown-modal", "UBOT-03-065-001"),
    ("ubot-login-form-still-visible", "UBOT-03-070-001"),
])
def test_ubot_natural_rule_match_reaches_run_error(monkeypatch, name, expected):
    crawler = object.__new__(UbotCrawler)
    crawler.name = "ubot"
    rule = next(rule for rule in crawler.login_checkpoint_rules() if rule.name == name)
    page = Page({rule.container_selector: [Node(text="PRIVATE_SYNTHETIC_BODY")]})
    collector = SimpleNamespace(attach=lambda page: None, detach=lambda page: None)
    terminals = []

    def login(page):
        outcome = cp.evaluate_login_checkpoint(
            page, bank="ubot", phase=cp.CheckpointPhase.POST_SUBMIT,
            rules=(rule,), is_authenticated=lambda page: False,
        )
        outcome = cp.validate_login_checkpoint_outcome(outcome, (rule,))
        try:
            cp.reduce_login_checkpoint(
                cp.CheckpointPhase.POST_SUBMIT, cp.LoginBudget(credential_submissions=1), outcome,
            )
        except cp.LoginCheckpointBlocked as error:
            terminals.append(error)
            raise

    monkeypatch.setattr(crawler, "_enforce_session_freshness", lambda: None)
    monkeypatch.setattr(crawler, "_build_fetch_kwargs", lambda: {})
    monkeypatch.setattr(crawler, "_make_collector", lambda page: collector)
    monkeypatch.setattr(crawler, "attach_shared_dialog_handler", lambda page: None)
    monkeypatch.setattr(crawler, "_credential_origin_allowed", lambda page: True)
    monkeypatch.setattr(crawler, "_shared_login", login)
    monkeypatch.setattr(crawler, "logout", lambda page: None)
    monkeypatch.setattr(crawler, "collect", lambda *args: pytest.fail("must not collect after rejection"))
    monkeypatch.setattr(crawler, "_execute_browser_flow", lambda url, **kw: kw["page_action"](page))
    result = base.BankCrawler.run(crawler, "offline:synthetic")
    from backend.core import sync_errors
    with pytest.raises(sync_errors.FailureDiagnosticError) as carrier:
        sync_errors.raise_crawler_failure(crawler, result)
    message = sync_errors.safe_failure_message(carrier.value, "ubot")
    assert format_error_id(expected, bank="ubot") in message
    assert "phase=post_submit" in message
    assert result["failure_diagnostics"] == {
        "exception": "LoginCheckpointBlocked", "error_id": expected, "phase": "post_submit",
    }
    assert format_error_id(expected, bank="ubot") in result["error"]
    assert terminals[0].outcome.rule_name == name
    assert "phase=post_submit" in result["error"]
    assert terminals[0].phase is cp.CheckpointPhase.POST_SUBMIT
    assert "PRIVATE_SYNTHETIC_BODY" not in repr(result)
    assert "data" not in result


def bank_rules(bank):
    from importlib import import_module
    module = import_module("backend.banks." + bank)
    cls, = (value for value in vars(module).values()
            if isinstance(value, type) and value.__module__ == module.__name__
            and issubclass(value, base.BankCrawler))
    crawler = object.__new__(cls)
    crawler.name = bank
    return crawler, crawler.login_checkpoint_rules()


@pytest.mark.parametrize("bank", sorted(BANKS))
def test_all_bank_terminal_matches_have_unique_final_source_ids(bank):
    crawler, rules = bank_rules(bank)
    bodies = {
        cp.CheckpointKind.OTP_REQUIRED: "OTP 一次性密碼",
        cp.CheckpointKind.PASSWORD_CHANGE_REQUIRED: "必須變更密碼",
        cp.CheckpointKind.EXPLICIT_LOGIN_ERROR: "密碼不正確",
        cp.CheckpointKind.UNKNOWN_BLOCKER: "PRIVATE_SYNTHETIC_BODY",
    }
    seen = set()
    for rule in rules:
        if rule.kind not in bodies:
            continue
        page = Page({rule.container_selector: [Node(text=bodies[rule.kind])]})
        outcome = cp.evaluate_login_checkpoint(
            page, bank=bank, phase=cp.CheckpointPhase.POST_SUBMIT,
            rules=(rule,), is_authenticated=lambda page: False,
        )
        assert outcome.kind is rule.kind
        assert outcome.rule_name == rule.name
        with pytest.raises(cp.LoginCheckpointTerminal) as caught:
            cp.reduce_login_checkpoint(
                cp.CheckpointPhase.POST_SUBMIT, cp.LoginBudget(credential_submissions=1),
                cp.validate_login_checkpoint_outcome(outcome, (rule,)),
            )
        final = base._failure_diagnostics(crawler, caught.value, "03-900-001")
        assert rule.error_id is not None, rule.name
        assert final["error_id"] == rule.error_id
        assert format_error_id(rule.error_id, bank=bank) in base._format_failure_diagnostics(final)
        assert "PRIVATE_SYNTHETIC_BODY" not in repr(final)
        assert rule.error_id not in seen
        seen.add(rule.error_id)
    assert seen


@pytest.mark.parametrize("invalid", [
    "CTBC-03-010-001", "SYS-03-988-001", "UBOT-05-400-001",
    "UBOT-03-999-999", "PRIVATE_SYNTHETIC_ID", None, False, {}, object(),
])
def test_rule_metadata_is_closed_and_bank_bound(invalid):
    from dataclasses import replace
    rule = next(rule for rule in bank_rules("ubot")[1] if rule.name == "ubot-unknown-modal")
    assert replace(rule, error_id=invalid).error_id is None


@pytest.mark.parametrize("kind,phase,budget,expected", [
    (cp.CheckpointKind.CAPTCHA_RETRY, cp.CheckpointPhase.PRE_SUBMIT, cp.LoginBudget(), "SYS-03-982-001"),
    (cp.CheckpointKind.CAPTCHA_RETRY, cp.CheckpointPhase.POST_SUBMIT, cp.LoginBudget(), "SYS-03-982-002"),
    (cp.CheckpointKind.STARTUP_RECOVERY, cp.CheckpointPhase.PRE_SUBMIT, cp.LoginBudget(reloads=1), "SYS-03-983-001"),
])
def test_retry_budget_rejections_keep_existing_ids(kind, phase, budget, expected):
    from backend.core.error_codes import exception_error_id
    rule = cp.LoginCheckpointRule(
        name="fixture-budget", bank="ubot", phases=tuple(cp.CheckpointPhase),
        kind=kind, container_selector="#fixture", error_id="UBOT-03-065-001",
    )
    outcome = cp.evaluate_login_checkpoint(
        Page({"#fixture": [Node(text="fixture")]}), bank="ubot",
        phase=phase, rules=(rule,), is_authenticated=lambda page: False,
    )
    assert outcome.error_id is None
    with pytest.raises(cp.LoginCheckpointBlocked) as caught:
        cp.reduce_login_checkpoint(phase, budget, outcome)
    assert exception_error_id(caught.value, bank="ubot") == expected


@pytest.mark.parametrize("phase", ["pre_submit", "post_submit", "post_submit_settle"])
def test_safe_phase_carrier_roundtrip(phase):
    from backend.core import sync_errors
    crawler, _ = bank_rules("ubot")
    outcome = cp.CheckpointOutcome(cp.CheckpointKind.UNKNOWN_BLOCKER, error_id="UBOT-03-065-001")
    error = cp.LoginCheckpointBlocked(cp.LoginBudget(), outcome, phase=cp.CheckpointPhase(phase))
    diagnostics = base._failure_diagnostics(crawler, error, "03-900-001")
    assert diagnostics == {
        "exception": "LoginCheckpointBlocked", "error_id": "UBOT-03-065-001", "phase": phase,
    }
    with pytest.raises(sync_errors.FailureDiagnosticError) as caught:
        sync_errors.raise_crawler_failure(crawler, {"failure_diagnostics": diagnostics})
    assert sync_errors.safe_failure_message(caught.value, "ubot").endswith(" | phase=" + phase)


@pytest.mark.parametrize("phase", ["PRIVATE_SYNTHETIC_PHASE", "persistence", None, False, {}, object()])
def test_safe_phase_rejects_nonclosed_values_without_coercion(phase):
    crawler, _ = bank_rules("ubot")
    assert base._validated_failure_diagnostics(crawler, {
        "exception": "LoginCheckpointBlocked", "error_id": "UBOT-03-065-001", "phase": phase,
    }) == {}


@pytest.mark.parametrize("name,expected", [
    ("ubot-unknown-modal", "UBOT-03-065-001"),
    ("ubot-login-form-still-visible", "UBOT-03-070-001"),
])
def test_natural_checkpoint_reaches_worker_rest_ws_and_push(client, offline, monkeypatch, name, expected):
    from backend.core import persist
    from backend.server import push, sync_job_worker, sync_jobs_repo
    crawler, _ = offline
    crawler.name = "ubot"
    rule = next(rule for rule in bank_rules("ubot")[1] if rule.name == name)
    page = Page({rule.container_selector: [Node(text="PRIVATE_SYNTHETIC_BODY")]})
    collector = SimpleNamespace(attach=lambda page: None, detach=lambda page: None)

    def login(page):
        outcome = cp.evaluate_login_checkpoint(
            page, bank="ubot", phase=cp.CheckpointPhase.POST_SUBMIT,
            rules=(rule,), is_authenticated=lambda page: False,
        )
        cp.reduce_login_checkpoint(
            cp.CheckpointPhase.POST_SUBMIT, cp.LoginBudget(credential_submissions=1),
            cp.validate_login_checkpoint_outcome(outcome, (rule,)),
        )

    monkeypatch.setattr(crawler, "run", base.BankCrawler.run.__get__(crawler))
    monkeypatch.setattr(crawler, "_enforce_session_freshness", lambda: None)
    monkeypatch.setattr(crawler, "_make_collector", lambda page: collector)
    monkeypatch.setattr(crawler, "attach_shared_dialog_handler", lambda page: None)
    monkeypatch.setattr(crawler, "_shared_login", login)
    monkeypatch.setattr(crawler, "_execute_browser_flow", lambda url, **kw: kw["page_action"](page))
    monkeypatch.setattr(crawler, "logout", lambda page: None)
    payloads = []
    monkeypatch.setattr(push, "get_notifier", lambda: SimpleNamespace(
        send_to_user=lambda **kw: payloads.append(kw["payload"]),
    ))
    response = client.post("/auth/register", json={
        "email": "checkpoint@palace.example", "password": "SyntheticPassword03!",
    }).json()
    token = response["token"]
    headers = {"Authorization": "Bearer " + token}
    user_id = client.get("/auth/me", headers=headers).json()["id"]
    job = sync_jobs_repo.queue(user_id=user_id, bank="ubot", history_mode="full")
    assert sync_job_worker.run_queued_jobs() == {"processed": 1, "done": 0, "failed": 1}
    stored = sync_jobs_repo.get(job)
    prefix = format_error_id(expected, bank="ubot")
    assert stored["error_msg"].startswith(prefix)
    assert "phase=post_submit" in stored["error_msg"]
    detail = client.get(f"/sync/jobs/{job}", headers=headers).json()
    listed = client.get("/sync/jobs", headers=headers).json()
    assert detail["error_msg"] == stored["error_msg"]
    assert next(item for item in listed if item["id"] == job)["error_msg"] == stored["error_msg"]
    with client.websocket_connect(f"/ws/sync/{job}", subprotocols=["bearer", token]) as ws:
        message = ws.receive_json()
    assert isinstance(message, dict)
    assert message["error_msg"] == stored["error_msg"]
    import json
    import os
    from pathlib import Path
    fixture_path = os.environ.get("SYNC_ERROR_HTTP_FIXTURE")
    if fixture_path:
        path = Path(fixture_path)
        fixtures = json.loads(path.read_text()) if path.exists() else []
        fixtures = [item for item in fixtures if item["prefix"] != prefix]
        fixtures.append({"prefix": prefix, "detail": detail, "list": listed, "ws": message})
        path.write_text(json.dumps(fixtures, ensure_ascii=False))
        path.chmod(0o600)
    assert len(payloads) == 1 and payloads[0].body.startswith(prefix)
    assert "PRIVATE_SYNTHETIC_BODY" not in stored["error_msg"] + payloads[0].body
    persist.persist_collected.assert_not_called()


@pytest.mark.parametrize("bank", sorted(BANKS))
def test_all_bank_shadow_rule_matches_keep_source_id(bank, monkeypatch):
    from dataclasses import replace
    from tests.test_login_checkpoint_browser_rules import Locator
    original_handle = Locator.element_handle

    def handle(locator, *, timeout):
        assert timeout in (100, 5000)
        return original_handle(locator, timeout=100)

    monkeypatch.setattr(Locator, "element_handle", handle)
    crawler, rules = bank_rules(bank)
    bodies = {
        "cathay-post-login-notice-dialog": "重要通知 服務內容提醒 我知道了",
        "cathay-post-login-announcement": "系統維護公告 維護中 不要再顯示 下次再提醒",
        "ctbc-entry-announcement": "重要公告 測試",
        "ctbc-duplicate-session": "確認訊息 請再次確認登入",
        "esun-spa-duplicate-session": "重複登入提醒 若要在此處登入，請按下「確定登入」，同時其它位置將會自動登出。 取消 確定登入",
        "esun-six-month-password-reminder": "變更密碼提醒 您已經有半年未變更使用者密碼了，為了網路銀行交易安全，請您立即變更。 立即變更",
        "hsbc-security-notice": "資訊安全 密碼 繼續",
        "linebank-login-success-notice": "登入 確定",
        "rakuten-duplicate-session": getattr(crawler, "DUP_LOGIN_BODY", ""),
        "scb-duplicate-session-modal": "已經在別台裝置登入 確定登入",
        "scb-duplicate-session-dialog": "已經在別台裝置登入 確定登入",
        "ubot-password-change-optional": "建議變更密碼",
    }
    terminal_kinds = {cp.CheckpointKind.OTP_REQUIRED, cp.CheckpointKind.PASSWORD_CHANGE_REQUIRED,
                      cp.CheckpointKind.EXPLICIT_LOGIN_ERROR, cp.CheckpointKind.UNKNOWN_BLOCKER}
    seen = {rule.error_id for rule in rules if rule.kind in terminal_kinds}
    for rule in rules:
        if rule.kind in terminal_kinds:
            continue
        body = bodies.get(rule.name, "synthetic notice")
        if rule.kind is cp.CheckpointKind.CAPTCHA_RETRY:
            body = "CAPT1234" if bank == "scb" else "驗證碼錯誤"
        elif bank == "taishin":
            body = "系統斷信 我知道了" if "-post-notice-" in rule.name else "上次未正常登出，請重新登入"
        # Exactly the existing Base active-rule replacement; do not clear patterns.
        shadow = replace(rule, kind=cp.CheckpointKind.UNKNOWN_BLOCKER,
                         action_selector=cp.DEFAULT_ACTION_SELECTOR, action_texts=(), max_actions=1)
        phase = rule.phases[0]
        outcome = cp.evaluate_login_checkpoint(
            Page({shadow.container_selector: [Node(text=body)]}), bank=bank, phase=phase,
            rules=(shadow,), is_authenticated=lambda page: False,
        )
        assert outcome.rule_name == rule.name
        with pytest.raises(cp.LoginCheckpointBlocked) as caught:
            cp.reduce_login_checkpoint(phase, cp.LoginBudget(),
                cp.validate_login_checkpoint_outcome(outcome, (shadow,)))
        final = base._failure_diagnostics(crawler, caught.value, "03-900-001")
        assert rule.error_id is not None, rule.name
        assert final["error_id"] == rule.error_id
        assert final["phase"] == phase.value
        assert rule.error_id not in seen
        seen.add(rule.error_id)
