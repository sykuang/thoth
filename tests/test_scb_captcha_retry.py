from __future__ import annotations

from backend.banks.scb import ScbCrawler
from backend.core.login_checkpoints import CheckpointKind, CheckpointPhase


def _crawler() -> ScbCrawler:
    crawler = object.__new__(ScbCrawler)
    crawler.name = "scb"
    return crawler


def test_explicit_capt_code_is_the_only_declared_captcha_retry() -> None:
    rules = _crawler().login_checkpoint_rules()
    captcha_rules = [rule for rule in rules if rule.kind is CheckpointKind.CAPTCHA_RETRY]

    assert [rule.container_selector for rule in captcha_rules] == [
        ".error",
        ".alert",
        "[role='alert']",
    ]
    assert all(rule.phases == (CheckpointPhase.POST_SUBMIT,) for rule in captcha_rules)
    assert all(rule.action_texts == () and not rule.is_clickable for rule in captcha_rules)
    for rule in captcha_rules:
        pattern = rule.required_body_pattern
        assert pattern is not None
        assert pattern.fullmatch("CAPT001: 驗證碼錯誤，請重新輸入")
        assert not pattern.search("圖形驗證碼錯誤，請重新輸入")
        assert not pattern.search("XCAPT001Y")
