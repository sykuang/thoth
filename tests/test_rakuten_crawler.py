from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

import backend.banks.rakuten as rakuten_mod
import backend.core.base as base_mod
from backend.banks.rakuten import (
    BASE,
    CAPTCHA_IMG,
    LOADER_SELECTOR,
    RakutenCrawler,
    RakutenLoginError,
    _account_number,
    _any_visible,
    _click_visible_login,
    _is_twd_query_request,
    _month_labels,
    _row_from_dom,
    _selection_matches,
    _six_month_labels,
    _unique_option_index,
    _view_ready,
)
from backend.core.base import ApiHit, BankCollectResult, ResponseCollector
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointOutcome,
    CheckpointPhase,
    LoginBudget,
    LoginCheckpointBlocked,
    evaluate_login_checkpoint,
)


def test_shared_login_api_and_rule_inventory() -> None:
    crawler = object.__new__(RakutenCrawler)
    crawler_source = inspect.getsource(RakutenCrawler)

    assert RakutenCrawler.USES_SHARED_LOGIN_CHECKPOINTS is True
    assert set(RakutenCrawler.__dict__) >= {
        "login",
        "prepare_login_page",
        "is_authenticated",
        "submit_credentials_once",
        "login_checkpoint_rules",
    }
    assert inspect.getsource(RakutenCrawler.login).strip().endswith(
        "return self._shared_login(page)"
    )
    obsolete_helpers = {
        "_otp_visible",
        "_recover_startup_connection",
        "_resolve_duplicate_login_modal",
        "_dismiss_known_promo",
        "_resolve_known_blocking_modals",
        "_blocking_modal_text",
        "_session_ready",
    }
    assert not set(RakutenCrawler.__dict__) & obsolete_helpers
    assert all(name not in crawler_source for name in obsolete_helpers)

    rules = crawler.login_checkpoint_rules()
    assert [rule.name for rule in rules] == [
        "rakuten-startup-connect-error",
        "rakuten-duplicate-session",
        "rakuten-otp-required",
        "rakuten-referral-promo",
        "rakuten-ricb-promo",
        "rakuten-time-deposit-promo",
        "rakuten-unknown-modal",
    ]
    assert all(rule.bank == "rakuten" for rule in rules)
    assert [rule.kind for rule in rules] == [
        CheckpointKind.STARTUP_RECOVERY,
        CheckpointKind.DUPLICATE_SESSION,
        CheckpointKind.OTP_REQUIRED,
        CheckpointKind.DISMISSIBLE_NOTICE,
        CheckpointKind.DISMISSIBLE_NOTICE,
        CheckpointKind.DISMISSIBLE_NOTICE,
        CheckpointKind.UNKNOWN_BLOCKER,
    ]
    assert [rule.container_selector for rule in rules] == [
        "#ib_init_connect_error_popup",
        ".modal.show",
        "input[name='otpCode']",
        ".modal.show",
        ".modal.show",
        ".modal.show",
        ".modal.show",
    ]
    assert [rule.phases for rule in rules] == [
        (CheckpointPhase.PRE_SUBMIT,),
        tuple(CheckpointPhase),
        (CheckpointPhase.POST_SUBMIT, CheckpointPhase.POST_SUBMIT_SETTLE),
        tuple(CheckpointPhase),
        tuple(CheckpointPhase),
        tuple(CheckpointPhase),
        tuple(CheckpointPhase),
    ]
    assert [rule.action_texts for rule in rules] == [
        (),
        ("是，我要登入",),
        (),
        ("稍後再看",),
        ("略過",),
        ("稍後再說", "稍後"),
        (),
    ]
    assert all(rule.max_actions == 1 for rule in rules)

    duplicate, referral, ricb = rules[1], rules[3], rules[4]
    assert duplicate.required_body_pattern.fullmatch(RakutenCrawler.DUP_LOGIN_BODY)
    assert duplicate.required_body_pattern.fullmatch(
        RakutenCrawler.DUP_LOGIN_BODY.replace(" ", "\n")
    )
    assert duplicate.required_body_pattern.fullmatch(
        f"\n{RakutenCrawler.DUP_LOGIN_BODY}\n否，不要登入\n是，我要登入\n"
    )
    assert not duplicate.required_body_pattern.fullmatch(
        f"{RakutenCrawler.DUP_LOGIN_BODY} 請洽客服"
    )
    assert referral.required_body_pattern.search(
        f"{RakutenCrawler.REFERRAL_PROMO_PREFIX}\n活動期間"
    )
    assert not referral.required_body_pattern.search(
        f"非官方{RakutenCrawler.REFERRAL_PROMO_PREFIX}"
    )
    assert referral.required_body_pattern.search(
        "推薦獎金活動更新，新戶另享現金回饋\n稍後再看"
    )
    assert not referral.required_body_pattern.search("推薦獎金活動更新\n稍後再看")
    assert not referral.required_body_pattern.search("新戶另享現金回饋\n稍後再看")
    assert ricb.required_body_pattern.search(
        f"{RakutenCrawler.INSURANCE_PROMO_PREFIX}\n活動期間"
    )
    assert not ricb.required_body_pattern.search(
        f"非官方{RakutenCrawler.INSURANCE_PROMO_PREFIX}"
    )


def test_time_deposit_promo_rule_is_closed() -> None:
    crawler = object.__new__(RakutenCrawler)
    rule = next(
        rule
        for rule in crawler.login_checkpoint_rules()
        if rule.name == "rakuten-time-deposit-promo"
    )
    body = (
        "免解任務 ✕ 可開多筆 ✕ 萬元起存 "
        "無需解任務！萬元即可開立： "
        "🔒【限額 20 億】 6個月定期存款 2.0% "
        "•鎖住收益，獻給追求穩健的你 "
        "•讓明年出行、旅行的預算更充裕 "
        "•期間：即日起～2026/12/31（額滿即止） "
        "🔥限額高利，額滿即止👇立即開立定存👇 "
        "稍後 立即開立定存"
    )

    assert rule.action_texts == ("稍後再說", "稍後")
    assert rule.required_body_pattern is not None
    assert rule.required_body_pattern.fullmatch(body)
    assert not rule.required_body_pattern.fullmatch(
        body.replace("稍後 立即開立定存", "稍後 立即解約")
    )


def test_account_number_accepts_display_separators() -> None:
    assert _account_number("812-3456-7890-123") == "81234567890123"
    assert _account_number("請選擇帳戶") is None
    assert _selection_matches(
        "simple-dropdown2",
        "帳號 81234567890123",
        "81234567890123",
    )
    assert not _selection_matches(
        "simple-dropdown",
        "2026/06 活存明細",
        "2026/05 活存明細",
    )
    assert _unique_option_index([
        "81234567890123",
        "81234567890124",
    ], "81234567890124") == 1
    assert _unique_option_index([
        "81234567890123",
        "81234567890123",
    ], "81234567890123") is None


def test_login_adapter_and_prepare_delegate_once(monkeypatch) -> None:
    page = Mock()
    page.locator.return_value.count.return_value = 0
    page.frames = []
    crawler = object.__new__(RakutenCrawler)
    shared_login = Mock(return_value=True)
    logged_in = Mock(return_value=True)
    monkeypatch.setattr(crawler, "_shared_login", shared_login)
    monkeypatch.setattr(crawler, "_logged_in", logged_in)

    assert crawler.login(page)
    shared_login.assert_called_once_with(page)
    crawler.prepare_login_page(page)
    page.wait_for_timeout.assert_called_once_with(20000)
    assert crawler.is_authenticated(page)
    logged_in.assert_called_once_with(page)


def test_run_recovers_only_late_authenticated_rakuten_terminal(monkeypatch, tmp_path) -> None:
    page = Mock()
    page.url = "https://www.rakuten-bank.com.tw/ebank/chm/chmqu0001/010"
    page.locator.return_value.count.return_value = 0

    class FakeContext:
        def new_page(self):
            return page

    class FakeSession:
        def __init__(self, **_kwargs):
            self.context = FakeContext()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    crawler = object.__new__(RakutenCrawler)
    crawler.name = "rakuten"
    crawler.session_dir = tmp_path
    crawler.collector = None
    crawler._shared_dialog_blocked = False
    crawler._enforce_session_freshness = Mock()
    crawler._build_fetch_kwargs = Mock(return_value={"__cleanups__": []})
    crawler._shared_login = Mock(side_effect=LoginCheckpointBlocked(
        LoginBudget(credential_submissions=1),
        CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER),
    ))
    crawler._logged_in = Mock(return_value=True)
    crawler.collect = Mock(return_value=BankCollectResult(
        bank="rakuten",
        final_url=page.url,
        twd_txn_results=[],
        card_bill_facts_ok=False,
        card_bill_facts=[],
    ))
    crawler.logout = Mock(return_value=True)
    monkeypatch.setattr(rakuten_mod, "StealthySession", FakeSession, raising=False)
    monkeypatch.setattr(
        base_mod.StealthyFetcher,
        "fetch",
        Mock(side_effect=AssertionError("Rakuten must keep one owned page")),
    )

    result = crawler.run(BASE, headless=True)

    assert "error" not in result
    assert "data" in result
    assert page.wait_for_timeout.call_args_list == [call(1000)] * 3 + [call(2000)]
    assert crawler._logged_in.call_count == 3
    crawler.collect.assert_called_once()
    crawler.logout.assert_called_once_with(page)


def test_empty_modal_skeleton_is_not_semantic_login_blocker() -> None:
    control_selectors: list[str] = []

    class Locator:
        def __init__(self, text: str, interactive: bool = False):
            self.text = text
            self.interactive = interactive

        def count(self) -> int:
            return 1

        def nth(self, _index: int):
            return self

        def is_visible(self) -> bool:
            return True

        def inner_text(self, **_kwargs) -> str:
            return self.text

        def locator(self, _selector: str):
            control_selectors.append(_selector)
            return SimpleNamespace(count=lambda: int(self.interactive), nth=lambda _index: self)

    page = SimpleNamespace(locator=lambda _selector: Locator(""))
    assert rakuten_mod._semantic_modal_visible(page) is False
    page = SimpleNamespace(locator=lambda _selector: Locator("登入通知"))
    assert rakuten_mod._semantic_modal_visible(page) is True
    page = SimpleNamespace(locator=lambda _selector: Locator("", interactive=True))
    assert rakuten_mod._semantic_modal_visible(page) is True
    assert "[role=button]" in control_selectors[-1]

    crawler = object.__new__(RakutenCrawler)
    unknown = crawler.login_checkpoint_rules()[-1]
    assert unknown.required_body_pattern is not None
    assert unknown.required_body_pattern.search("") is None
    assert unknown.required_body_pattern.search("登入通知") is not None


def test_authenticated_state_fails_closed_for_actionable_modal_in_owned_scope(
    monkeypatch,
) -> None:
    main = SimpleNamespace(name="main")
    child = SimpleNamespace(name="child")
    page = SimpleNamespace(main_frame=main, frames=[main, child])
    crawler = object.__new__(RakutenCrawler)
    crawler._logged_in = Mock(return_value=True)
    crawler._frame_origin_allowed = Mock(return_value=True)

    monkeypatch.setattr(
        rakuten_mod,
        "_semantic_modal_visible",
        lambda scope: scope is child,
    )

    assert crawler.is_authenticated(page) is False


def test_submit_credentials_rejects_semantic_modal_before_field_access() -> None:
    crawler = object.__new__(RakutenCrawler)
    crawler._semantic_modal_visible_in_owned_scope = Mock(return_value=True)
    page = Mock()

    with pytest.raises(RakutenLoginError, match="未處理互動視窗"):
        crawler.submit_credentials_once(page)

    page.locator.assert_not_called()


def test_late_auth_recovery_rechecks_named_unknown_loading_modal(monkeypatch) -> None:
    ticks = 0
    page = Mock()

    def wait(_milliseconds):
        nonlocal ticks
        ticks += 1

    page.wait_for_timeout.side_effect = wait
    monkeypatch.setattr(
        rakuten_mod,
        "_any_visible",
        lambda _page, selector: selector == LOADER_SELECTOR and ticks == 1,
    )
    monkeypatch.setattr(rakuten_mod, "_semantic_modal_visible", lambda _page: False)
    crawler = object.__new__(RakutenCrawler)
    crawler.name = "rakuten"
    crawler._shared_dialog_blocked = False
    crawler._credential_origin_allowed = Mock(return_value=True)
    crawler._logged_in = Mock(return_value=True)
    error = LoginCheckpointBlocked(
        LoginBudget(credential_submissions=1),
        CheckpointOutcome(
            CheckpointKind.UNKNOWN_BLOCKER,
            rule_name="rakuten-unknown-modal",
        ),
    )

    assert crawler._recover_late_authentication(page, error)
    assert page.wait_for_timeout.call_args_list == [call(1000)] * 4


def test_late_auth_recovery_never_actions_a_new_modal_without_scope_provenance() -> None:
    modal_visible = True

    class Locator:
        def __init__(self, selector: str):
            self.selector = selector

        def count(self) -> int:
            return int(
                self.selector in {".modal.show", rakuten_mod.SEMANTIC_MODAL_SELECTOR}
                and modal_visible
            )

        def nth(self, _index: int):
            return self

        def is_visible(self) -> bool:
            return (
                self.selector in {".modal.show", rakuten_mod.SEMANTIC_MODAL_SELECTOR}
                and modal_visible
            )

        def inner_text(self, **_kwargs) -> str:
            return "已知通知" if modal_visible else ""

    page = Mock()
    page.locator.side_effect = Locator
    crawler = object.__new__(RakutenCrawler)
    crawler.name = "rakuten"
    crawler._shared_dialog_blocked = False
    crawler._credential_origin_allowed = Mock(return_value=True)
    crawler._logged_in = Mock(return_value=True)

    error = LoginCheckpointBlocked(
        LoginBudget(credential_submissions=1),
        CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER),
    )

    assert crawler._recover_late_authentication(page, error) is False


def test_authenticated_origin_requires_exact_bank_host_and_ebank_prefix() -> None:
    crawler = object.__new__(RakutenCrawler)
    page = Mock()
    page.evaluate.return_value = True

    page.url = "https://www.rakuten-bank.com.tw/ebank/home"
    assert crawler._logged_in(page) is True
    page.url = "http://www.rakuten-bank.com.tw/ebank/home"
    assert crawler._logged_in(page) is False
    page.url = "https://www.rakuten-bank.com.tw:444/ebank/home"
    assert crawler._logged_in(page) is False
    page.url = "https://user@www.rakuten-bank.com.tw/ebank/home"
    assert crawler._logged_in(page) is False
    page.url = "https://evilrakuten-bank.com.tw/ebank/home"
    assert crawler._logged_in(page) is False
    page.url = "https://www.rakuten-bank.com.tw/public/ebank/home"
    assert crawler._logged_in(page) is False


def test_rakuten_rules_with_real_patchright_evaluator() -> None:
    from patchright.sync_api import sync_playwright

    with sync_playwright() as patchright:
        if not Path(patchright.chromium.executable_path).exists():
            pytest.skip("Patchright browser binary is not installed")
        browser = patchright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            crawler = object.__new__(RakutenCrawler)
            rules = crawler.login_checkpoint_rules()

            def evaluate(html: str, phase: CheckpointPhase):
                page.set_content(html)
                return evaluate_login_checkpoint(
                    page,
                    bank="rakuten",
                    phase=phase,
                    rules=rules,
                    is_authenticated=lambda _page: False,
                )

            outcome = evaluate(
                """
                <div id="ib_init_connect_error_popup">
                  初始化失敗<button onclick="this.dataset.clicked='yes'">重新載入</button>
                </div>
                """,
                CheckpointPhase.PRE_SUBMIT,
            )
            assert outcome.kind is CheckpointKind.STARTUP_RECOVERY
            assert page.locator("button").get_attribute("data-clicked") is None

            outcome = evaluate(
                f"""
                <div class="modal show">
                  <div>{RakutenCrawler.DUP_LOGIN_BODY}</div>
                  <button id="cancel">否，不要登入</button>
                  <button id="confirm" onclick="this.dataset.clicked='yes'; this.closest('.modal').classList.remove('show')">
                    是，我要登入
                  </button>
                </div>
                """,
                CheckpointPhase.POST_SUBMIT,
            )
            assert outcome.kind is CheckpointKind.DUPLICATE_SESSION
            assert outcome.action_label == "是，我要登入"
            assert page.locator("#confirm").get_attribute("data-clicked") == "yes"

            secret = "重複登入安全提醒 987654"
            outcome = evaluate(
                f"""
                <div class="modal show">
                  <div>{secret}</div>
                  <button id="unknown-confirm" onclick="this.dataset.clicked='yes'">是，我要登入</button>
                </div>
                """,
                CheckpointPhase.POST_SUBMIT,
            )
            assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
            assert outcome.rule_name == "rakuten-unknown-modal"
            assert page.locator("#unknown-confirm").get_attribute("data-clicked") is None
            assert secret not in repr(outcome)

            for prefix, action, expected_rule in (
                (RakutenCrawler.REFERRAL_PROMO_PREFIX, "稍後再看", "rakuten-referral-promo"),
                (RakutenCrawler.INSURANCE_PROMO_PREFIX, "略過", "rakuten-ricb-promo"),
            ):
                outcome = evaluate(
                    f"""
                    <div class="modal show">
                      <div>{prefix}<br>活動期間</div>
                      <button id="other">立即參加</button>
                      <button id="dismiss" onclick="this.closest('.modal').classList.remove('show')">
                        {action}
                      </button>
                    </div>
                    """,
                    CheckpointPhase.POST_SUBMIT,
                )
                assert outcome.kind is CheckpointKind.DISMISSIBLE_NOTICE
                assert outcome.rule_name == expected_rule
                assert outcome.action_label == action
                assert page.locator("#other").get_attribute("data-clicked") is None

            outcome = evaluate(
                f"""
                <div class="modal show">
                  <div>{RakutenCrawler.TIME_DEPOSIT_PROMO_PREFIX} 未驗證附加內容</div>
                  <button id="time-deposit-dismiss" onclick="this.dataset.clicked='yes'; this.closest('.modal').classList.remove('show')">
                    稍後
                  </button>
                </div>
                """,
                CheckpointPhase.POST_SUBMIT,
            )
            assert outcome.kind is CheckpointKind.UNKNOWN_BLOCKER
            assert outcome.rule_name == "rakuten-unknown-modal"
            assert page.locator("#time-deposit-dismiss").get_attribute("data-clicked") is None

            outcome = evaluate(
                """
                <input name="otpCode">
                <button id="otp-action" onclick="this.dataset.clicked='yes'">送出 OTP</button>
                """,
                CheckpointPhase.POST_SUBMIT_SETTLE,
            )
            assert outcome.kind is CheckpointKind.OTP_REQUIRED
            assert outcome.interaction == "otp"
            assert page.locator("#otp-action").get_attribute("data-clicked") is None
        finally:
            browser.close()


def test_login_click_requires_one_visible_enabled_button() -> None:
    class FakeLocator:
        def __init__(
            self,
            *,
            count: int = 1,
            visible: bool = True,
            enabled: bool = True,
            classes: str = "",
        ):
            self._count = count
            self._visible = visible
            self._enabled = enabled
            self._classes = classes
            self.clicks = 0

        def filter(self, **_kwargs):
            return self

        def count(self) -> int:
            return self._count

        @property
        def first(self):
            return self

        def get_attribute(self, _name: str) -> str:
            return self._classes

        def is_visible(self) -> bool:
            return self._visible

        def is_enabled(self) -> bool:
            return self._enabled

        def click(self) -> None:
            self.clicks += 1

    class FakePage:
        def __init__(self, locator: FakeLocator):
            self.button = locator

        def locator(self, selector: str) -> FakeLocator:
            assert selector == "a.btn.btn-primary:visible"
            return self.button

    hidden = FakeLocator(visible=False)
    disabled = FakeLocator(classes="btn disabled")
    native_disabled = FakeLocator(enabled=False)
    duplicate = FakeLocator(count=2)
    enabled = FakeLocator(classes="btn btn-primary")

    assert not _click_visible_login(FakePage(hidden))
    assert not _click_visible_login(FakePage(disabled))
    assert not _click_visible_login(FakePage(native_disabled))
    assert not _click_visible_login(FakePage(duplicate))
    assert _click_visible_login(FakePage(enabled))
    assert hidden.clicks == disabled.clicks == native_disabled.clicks == duplicate.clicks == 0
    assert enabled.clicks == 1


def test_any_visible_checks_each_match_without_strict_locator_lookup() -> None:
    class Match:
        def __init__(self, visible: bool):
            self.visible = visible

        def is_visible(self) -> bool:
            return self.visible

    class Matches:
        def __init__(self, visibility: list[bool]):
            self.matches = [Match(visible) for visible in visibility]

        def count(self) -> int:
            return len(self.matches)

        def nth(self, index: int) -> Match:
            return self.matches[index]

    class Page:
        def __init__(self, visibility: list[bool]):
            self.matches = Matches(visibility)

        def locator(self, selector: str) -> Matches:
            assert selector == ".checkpoint"
            return self.matches

    assert not _any_visible(Page([False, False]), ".checkpoint")
    assert _any_visible(Page([False, True]), ".checkpoint")


def _submit_fixture(
    monkeypatch,
    *,
    login_count: int = 1,
    login_enabled: bool = True,
    login_classes: str = "btn btn-primary",
    login_error: Exception | None = None,
    captcha_visible: bool = False,
    visible_checkpoint: str | None = None,
):
    page = Mock()
    fields = {selector: Mock() for selector in ("#custNo", "#userNo", "#pcode", "#captcha")}
    captcha = Mock()
    captcha.is_visible.return_value = captcha_visible
    captcha.get_attribute.return_value = "captcha-v1"
    captcha_root, captcha_group, refresh = Mock(), Mock(), Mock()
    captcha_root.locator.return_value = captcha_group
    captcha_group.locator.return_value = refresh

    candidates, button = Mock(), Mock()
    candidates.filter.return_value = candidates
    candidates.count.return_value = login_count
    candidates.first = button
    button.get_attribute.side_effect = lambda name: login_classes if name == "class" else None
    button.is_visible.return_value = True
    button.is_enabled.return_value = login_enabled
    button.click.side_effect = login_error

    checkpoints = {
        selector: Mock()
        for selector in (
            "input[name='otpCode']",
            ".modal.show",
            ".modal.show:not(.modal_loading)",
            "#ib_init_connect_error_popup",
        )
    }
    for selector, locator in checkpoints.items():
        locator.is_visible.return_value = selector == visible_checkpoint
        locator.count.return_value = int(selector == visible_checkpoint)
        locator.nth.return_value = locator

    locators = {
        **fields,
        CAPTCHA_IMG: captcha,
        "captcha-image": captcha_root,
        "a.btn.btn-primary:visible": candidates,
        **checkpoints,
    }
    page.locator.side_effect = locators.__getitem__
    page.evaluate.return_value = {
        "custNo": 4,
        "userNo": 3,
        "pcode": 4,
        "captcha": 4 if captcha_visible else 0,
    }

    crawler = object.__new__(RakutenCrawler)
    crawler.creds = SimpleNamespace(national_id="A123", user_code="U12", password="P123")
    crawler.captcha_tmp = Path("captcha.png")
    monkeypatch.setattr(crawler, "_logged_in", Mock(return_value=False))
    return crawler, page, fields, button, checkpoints, refresh


def test_submit_without_captcha_types_verified_lengths_and_dispatches_once(monkeypatch) -> None:
    crawler, page, fields, button, checkpoints, _ = _submit_fixture(monkeypatch)

    crawler.submit_credentials_once(page)

    for selector, value in (("#custNo", "A123"), ("#userNo", "U12"), ("#pcode", "P123")):
        fields[selector].press_sequentially.assert_called_once_with(value, delay=60)
    fields["#captcha"].press_sequentially.assert_not_called()
    button.click.assert_called_once_with()
    assert page.wait_for_timeout.call_count == 20
    assert all(locator.click.call_count == 0 for locator in checkpoints.values())


@pytest.mark.parametrize(
    ("login_count", "login_enabled", "login_classes"),
    [(0, True, "btn btn-primary"), (2, True, "btn btn-primary"), (1, False, "btn btn-primary")],
)
def test_submit_missing_ambiguous_or_disabled_action_sends_zero(
    monkeypatch,
    login_count: int,
    login_enabled: bool,
    login_classes: str,
) -> None:
    crawler, page, _, button, _, _ = _submit_fixture(
        monkeypatch,
        login_count=login_count,
        login_enabled=login_enabled,
        login_classes=login_classes,
    )

    with pytest.raises(RakutenLoginError, match="未送出登入"):
        crawler.submit_credentials_once(page)
    button.click.assert_not_called()


def test_submit_click_timeout_after_dispatch_has_no_fallback(monkeypatch) -> None:
    crawler, page, _, button, _, _ = _submit_fixture(
        monkeypatch,
        login_error=TimeoutError("dispatch timed out"),
    )

    with pytest.raises(TimeoutError, match="dispatch timed out"):
        crawler.submit_credentials_once(page)
    button.click.assert_called_once_with()


def test_pre_submit_semantic_modal_never_acts_or_submits(monkeypatch) -> None:
    crawler, page, _, button, checkpoints, _ = _submit_fixture(
        monkeypatch,
        visible_checkpoint=".modal.show:not(.modal_loading)",
    )

    with pytest.raises(RakutenLoginError, match="未送出登入"):
        crawler.submit_credentials_once(page)

    button.click.assert_not_called()
    page.wait_for_timeout.assert_not_called()
    assert all(locator.click.call_count == 0 for locator in checkpoints.values())


def test_submit_waits_through_loading_modal_until_authenticated(monkeypatch) -> None:
    crawler, page, _, button, _, _ = _submit_fixture(
        monkeypatch,
        visible_checkpoint=".modal.show",
    )
    logged_in = Mock(side_effect=[False, True])
    monkeypatch.setattr(crawler, "_logged_in", logged_in)

    crawler.submit_credentials_once(page)

    button.click.assert_called_once_with()
    assert logged_in.call_count == 2
    assert page.wait_for_timeout.call_count == 2


def test_real_patchright_multi_modal_post_submit_wait_is_secret_safe(caplog) -> None:
    from patchright.sync_api import sync_playwright

    secret = "PRIVATE_MODAL_SECRET_987654"
    with sync_playwright() as patchright:
        if not Path(patchright.chromium.executable_path).exists():
            pytest.skip("Patchright browser binary is not installed")
        browser = patchright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(
                f"""
                <input id="custNo"><input id="userNo"><input id="pcode"><input id="captcha">
                <a class="btn btn-primary">登入</a>
                <script>
                  document.body.dataset.loginClicks = '0';
                  document.body.dataset.modalActions = '0';
                  document.querySelector('a').onclick = () => {{
                    document.body.dataset.loginClicks = String(
                      Number(document.body.dataset.loginClicks) + 1
                    );
                    for (let i = 0; i < 2; i++) {{
                      const modal = document.createElement('div');
                      modal.className = 'modal show';
                      modal.innerHTML = `{secret}<button>modal action</button>`;
                      modal.querySelector('button').onclick = () => {{
                        document.body.dataset.modalActions = String(
                          Number(document.body.dataset.modalActions) + 1
                        );
                      }};
                      document.body.appendChild(modal);
                    }}
                  }};
                </script>
                """
            )
            crawler = object.__new__(RakutenCrawler)
            crawler.creds = SimpleNamespace(
                national_id="A123",
                user_code="U12",
                password="P123",
            )
            crawler.captcha_tmp = Path("captcha.png")

            error = None
            try:
                crawler.submit_credentials_once(page)
            except Exception as exc:  # regression capture: exception text may expose DOM
                error = exc

            assert secret not in repr(error)
            assert error is None
            assert page.locator("body").get_attribute("data-login-clicks") == "1"
            assert page.locator("body").get_attribute("data-modal-actions") == "0"
            assert secret not in caplog.text
        finally:
            browser.close()


def test_captcha_ocr_failure_never_submits(monkeypatch) -> None:
    crawler, page, _, button, _, refresh = _submit_fixture(monkeypatch, captcha_visible=True)
    solve = Mock(return_value=None)
    monkeypatch.setattr(rakuten_mod, "solve_captcha", solve)
    monkeypatch.setattr(rakuten_mod, "wait_captcha_stable", Mock())

    with pytest.raises(RakutenLoginError, match="OCR 失敗") as error:
        crawler.submit_credentials_once(page)

    assert error.value.safe_code == "captcha_ocr_failed"  # shared login reloads the page
    refresh.click.assert_not_called()
    assert solve.call_count == 1
    button.click.assert_not_called()


def test_logout_confirms_bank_modal_before_reporting_success() -> None:
    events: list[str] = []
    page, frame, links, link, modals, stale, modal, body, buttons, button = (Mock() for _ in range(10))
    page.frames = [frame]
    frame.locator.return_value = links
    links.filter.return_value = links
    links.count.return_value = 1
    links.nth.return_value = link
    link.is_visible.return_value = True
    link.click.side_effect = lambda: events.append("open-logout")
    page.locator.return_value = modals
    modals.count.return_value = 2
    modals.nth.side_effect = [stale, modal]
    stale.inner_text.return_value = "其他可見提示\n取消\n確認"
    stale.locator.return_value.inner_text.return_value = "其他可見提示"
    modal.inner_text.return_value = "登出網路銀行…取消確認"
    body.inner_text.return_value = "登出網路銀行\n確認登出本系統？"
    modal.locator.side_effect = lambda selector: body if selector == ".modal-body" else buttons
    buttons.filter.return_value = buttons
    buttons.count.return_value = 1
    buttons.first = button
    button.is_visible.return_value = True
    button.click.side_effect = lambda: events.append("confirm-logout")
    page.wait_for_selector.side_effect = lambda selector, **kwargs: events.append(
        f"wait:{selector}:{kwargs['state']}:{kwargs['timeout']}"
    )
    crawler = object.__new__(RakutenCrawler)

    assert crawler.logout(page)
    assert events == [
        "open-logout",
        "wait:modal-confirm .modal.show:visible, modal-projection .modal.show:visible:visible:10000",
        "confirm-logout",
        "wait:#custNo:visible:30000",
    ]


def test_goto_twd_semantic_modal_blocks_without_action_or_retry() -> None:
    secret = "PRIVATE_LATE_CLICK_SECRET_987654"
    page, nav = Mock(), Mock()
    nav.first = nav
    nav.click.side_effect = RuntimeError(secret)
    page.get_by_role.return_value = nav
    crawler = object.__new__(RakutenCrawler)
    crawler.name = "rakuten"
    crawler._semantic_modal_visible_in_owned_scope = Mock(return_value=True)

    with pytest.raises(RakutenLoginError, match="未執行視窗操作") as raised:
        crawler._goto_twd(page)

    nav.click.assert_called_once_with(timeout=5000)
    assert secret not in str(raised.value)


def test_goto_twd_authenticated_outcome_rethrows_original_click_error() -> None:
    original = RuntimeError("deposit click failed")
    page, nav = Mock(), Mock()
    nav.first = nav
    nav.click.side_effect = original
    page.get_by_role.return_value = nav
    crawler = object.__new__(RakutenCrawler)
    crawler.name = "rakuten"
    crawler._semantic_modal_visible_in_owned_scope = Mock(return_value=False)

    with pytest.raises(RuntimeError) as raised:
        crawler._goto_twd(page)

    assert raised.value is original
    nav.click.assert_called_once_with(timeout=5000)


def test_query_request_and_dom_readiness_fail_closed() -> None:
    class Request:
        def __init__(self, url: str, method: str = "POST"):
            self.url = url
            self.method = method

    endpoint = (
        "https://www.rakuten-bank.com.tw/ixtein/adapters/ebank/txns/"
        "channel-ctw/CTWQU0001/011"
    )
    assert _is_twd_query_request(Request(endpoint))
    assert not _is_twd_query_request(Request(endpoint, method="GET"))
    assert not _is_twd_query_request(Request(
        "https://www.rakuten-bank.com.tw/ixtein/adapters/ebank/txns/"
        "channel-ctw/CTWQU0001/010",
    ))
    assert not _is_twd_query_request(Request(
        "https://evil.example/telemetry?target=/channel-ctw/CTWQU0001/011",
    ))
    assert not _is_twd_query_request(Request(f"{endpoint};evil"))

    initial = {
        "rows": "current rows",
        "account": "帳號 81234567890123",
        "month": "2026/07 活存明細",
        "balance": "NT$ 0",
        "noData": False,
    }
    assert _view_ready(None, initial)
    assert not _view_ready(None, {**initial, "rows": ""})
    assert _view_ready(None, {**initial, "rows": "", "noData": True})
    assert not _view_ready("old rows", {**initial, "rows": "old rows"})
    assert _view_ready("old rows", {**initial, "rows": "new rows"})
    assert not _view_ready("old rows", {**initial, "rows": ""})
    assert _view_ready("old rows", {**initial, "rows": "", "noData": True})


def test_select_waits_for_bound_request_and_loader_transition(monkeypatch) -> None:
    events: list[str] = []
    label = "2026/06 活存明細"
    endpoint = (
        "https://www.rakuten-bank.com.tw/ixtein/adapters/ebank/txns/"
        "channel-ctw/CTWQU0001/011"
    )
    frame_obj = object()
    collector = ResponseCollector("rakuten-bank.com.tw")

    class Target:
        def count(self) -> int:
            return 1

        def nth(self, _index: int):
            return self

        def inner_text(self) -> str:
            return label

        def click(self) -> None:
            events.append("click")
            collector._request_sequence = 1
            collector._issued_endpoint_counts["011"] = 1
            collector.hits.append(ApiHit(
                url=endpoint,
                raw_url=endpoint,
                method="POST",
                status=200,
                content_type="application/json; charset=utf-8",
                request_sequence=1,
                main_frame_request=True,
                request_frame=frame_obj,
            ))

    class Response:
        status = 200

        def finished(self) -> None:
            events.append("response-finished")

    class Request:
        method = "POST"
        url = endpoint
        frame = frame_obj

        def response(self) -> Response:
            events.append("bound-response")
            return Response()

    class ExpectedRequest:
        value = Request()

        def __enter__(self):
            events.append("expect-request")
            return self

        def __exit__(self, *_args) -> None:
            events.append("request-captured")

    class Page:
        keyboard = object()
        main_frame = frame_obj

        def locator(self, _selector: str) -> Target:
            return Target()

        def wait_for_timeout(self, milliseconds: int) -> None:
            if milliseconds == 500:
                events.append("network-quiet")

        def wait_for_selector(self, _selector: str, *, state: str, timeout: int) -> None:
            events.append(f"loader-{state}")

        def expect_request(self, predicate, *, timeout: int) -> ExpectedRequest:
            assert predicate(Request())
            return ExpectedRequest()

    crawler = object.__new__(RakutenCrawler)
    monkeypatch.setattr(crawler, "_open_dropdown", lambda *_args: None)
    monkeypatch.setattr(crawler, "_twd_view_state", lambda *_args: {"rows": ""})
    monkeypatch.setattr(crawler, "_wait_for_twd_view", lambda *_args, **_kwargs: events.append("dom-ready"))

    assert crawler._select_label(Page(), collector, "simple-dropdown", label) == {
        "url": endpoint,
        "method": "POST",
        "status": 200,
        "content_type": "application/json",
        "redirected": False,
        "main_frame": True,
        "request_count": 1,
        "response_count": 1,
    }
    assert events == [
        "loader-hidden",
        "expect-request",
        "click",
        "loader-visible",
        "request-captured",
        "bound-response",
        "response-finished",
        "loader-hidden",
        "dom-ready",
        "network-quiet",
        "network-quiet",
        "network-quiet",
        "network-quiet",
    ]


def test_month_labels_require_canonical_months() -> None:
    labels = [
        "請選擇",
        "2026/07 活存明細",
        "2026/06 活存明細",
        "2026/06 活存明細",
        "2026/05 活存明細",
        "2026/04 活存明細",
        "2026/03 活存明細",
        "2026/02 活存明細",
        "自訂區間",
        "2026/01 其他明細",
    ]

    expected = [
        "2026/07 活存明細",
        "2026/06 活存明細",
        "2026/05 活存明細",
        "2026/04 活存明細",
        "2026/03 活存明細",
        "2026/02 活存明細",
    ]
    assert _month_labels(labels) == expected
    assert _six_month_labels(labels) == expected
    with pytest.raises(RuntimeError):
        _six_month_labels(expected[:5])
    with pytest.raises(RuntimeError):
        _six_month_labels([*expected, "2026/01 活存明細"])


def test_row_from_dom_maps_the_six_bank_columns() -> None:
    assert _row_from_dom([
        "2026/07/26\n09:30:00",
        "跨行轉入\n王小明 81200000000000",
        "1,500",
        "",
        "12,345",
        "薪資",
    ]) == {
        "sysDate": "2026/07/26",
        "sysTime": "09:30:00",
        "txDesc": "跨行轉入",
        "nickNameOrAcct": "王小明 81200000000000",
        "amt": "1,500",
        "amtSign": True,
        "balance": "12,345",
        "memo": "薪資",
    }

    assert _row_from_dom([
        "2026/07/25 18:05:00", "轉帳支出", "", "200", "10,845", "",
    ])["amtSign"] is False
    assert _row_from_dom([
        "2026/07/25 18:05:00", "矛盾", "100", "200", "10,845", "",
    ]) is None
    assert _row_from_dom([
        "2026/07/25 18:05:00", "摘要\n對方\n額外未解析資料", "100", "", "10,845", "",
    ]) is None
    assert _row_from_dom([
        "2026/07/25 18:05:00", "A" * 4001, "100", "", "10,845", "",
    ]) is None
    assert _row_from_dom([
        "2026/07/25 18:05:00", "摘要", "100", "", "10,845", "bad\x00memo",
    ]) is None


def test_scrape_twd_page_returns_only_normalized_fields() -> None:
    class FakePage:
        @staticmethod
        def evaluate(_script: str) -> dict:
            return {
                "accountLabel": "帳號 812-3456-7890-123",
                "balance": "NT$ 0",
                "rows": [[
                    "2026/07/26\n09:30:00",
                    "跨行轉入\n王小明 81200000000000",
                    "1,500",
                    "",
                    "12,345",
                    "薪資",
                ]],
                "dom": {
                    "table_count": 1,
                    "visible_tables": 1,
                    "headers": [
                        "交易時間", "交易說明 對方帳號或暱稱", "轉入", "轉出",
                        "帳戶餘額", "備註", "",
                    ],
                    "raw_rows": 1,
                    "no_data_count": 0,
                    "invalid_cells": 0,
                    "pager": 0,
                    "busy": 0,
                    "dialogs": 0,
                    "alerts": 0,
                },
            }

    result = RakutenCrawler._scrape_twd_page(FakePage())
    assert set(result) == {"account_no", "accounts", "txDetails", "dom"}
    assert result["account_no"] == "81234567890123"
    assert result["accounts"] == [{
        "acctNo": "81234567890123",
        "balance": "NT$ 0",
    }]
    assert result["txDetails"][0]["txDesc"] == "跨行轉入"
    with pytest.raises(RuntimeError, match="rakuten-twd-history-dom"):
        RakutenCrawler._scrape_twd_page(FakePage(), "81234567890124")


def test_modal_shape_keeps_title_and_buttons_but_masks_digits() -> None:
    from backend.banks.rakuten import _modal_shape

    page = Mock()
    page.evaluate.return_value = [{"id": "m1", "title": "提醒 2026", "buttons": ["確定", "第3頁"]}]
    assert _modal_shape(page) == [{"id": "m#", "title": "提醒 ####", "buttons": ["確定", "第#頁"]}]
    page.evaluate.side_effect = RuntimeError("boom")
    assert _modal_shape(page) == ["RuntimeError"]
