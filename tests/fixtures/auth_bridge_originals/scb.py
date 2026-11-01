#!/usr/bin/env python3
"""Standard Chartered Bank Taiwan personal e-banking crawler.

台灣渣打銀行 ebank.standardchartered.com.tw/scb/ 個人網銀爬蟲。

2026-06-12 初版（dry probe v1/v2 揭示）:

  登入頁特徵:
    URL: https://ebank.standardchartered.com.tw/scb/public/login?lang=tw
    （注意：HTTP 預設 404 但 SPA 內容會渲染 — 是技術詐欺反爬）

    5 個 visible input（依 y 順序）:
      [0] type=text     name=jKZM6e659qV2yIt (每次 reload 變)  → 身分證字號
      [1] type=checkbox name=                                  → 記住身分證
      [2] type=password name=YTg6aHg3 (動態)         maxlen=12 → 使用者名稱
      [3] type=password name=yWZoDcyQ1 (動態)        maxlen=12 → 網銀密碼
      [4] type=tel      name=__reCaptcha (**固定**)  maxlen=6  → captcha (純數字)

    Captcha: `<img cls="is-max-width is-max-height" 155x50 src="data:image/jpeg;base64,...">`
      → 直接抽 base64 餵 ddddocr，免 element.screenshot
    「重新產生」: `<button class="b-text-green-d">重新產生</button>` (visible, 56x20)
    登入鈕: `<button type=submit class="m-button b-bg-green-d b-block">登入</button>` (300x42)

  登入流程:
    Step 1: page goto → 等 8s SPA 渲染
    Step 2: visible input y-order 定位 → triple-click + Backspace + keyboard.type
            (DBS React password 教訓：必用真實鍵盤模擬)
    Step 3: 抽 captcha img base64 → ocr_bytes → 填 #__reCaptcha
    Step 4: page.evaluate 驗 length 不對就 abort 不送 login
    Step 5: click 登入鈕 (type=submit b-bg-green-d)

  ⚠️ 鐵律：預設只送一次；僅銀行明確回 CAPT* 驗證碼錯誤時換圖限重送一次

  Collect 流程（已完成）:
    - collect() 點信用卡 menu, 抓 sharedCards + crditAcctList E2EE 帳單明細
    - persist_scb 解析 per-card 卡號 + 帳單列表（cards/billed/pending 全入庫）
    - 測試: tests/test_persist_scb_per_card.py, tests/test_persist_scb_consumption_detail.py
"""
from __future__ import annotations

import base64
import contextlib
import re
import sys
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.base import BankCollectResult, BankCrawler, ResponseCollector, captcha_unreadable
from backend.core.card_bills import publish_card_bill_facts
from backend.core.captcha import ocr_bytes
from backend.core.error_codes import error_step, with_error_code, exception_error_id, format_error_id
from backend.core.creds import ScbCreds
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointPhase,
    LoginCheckpointRule,
)

BASE = "https://ebank.standardchartered.com.tw/scb/public/login?lang=tw"
LOGIN_PATH_HINT = "/scb/public/login"


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


class ScbLoginError(RuntimeError):
    """SCB login 無法安全準備、送出，或送出狀態不明。"""


class ScbCrawler(BankCrawler):
    USES_SHARED_LOGIN_CHECKPOINTS: ClassVar[bool] = True
    CREDENTIAL_HOSTS = frozenset({"ebank.standardchartered.com.tw"})

    def __init__(self):
        super().__init__(name="scb")
        self.creds = ScbCreds.load()

    def _host_filter(self) -> str:
        return "standardchartered.com.tw"

    def _logged_in(self, page) -> bool:
        """Pure one-shot positive check; lifecycle owns all waiting."""
        try:
            parsed = urlparse(page.url or "")
            if (
                (parsed.hostname or "").lower() != "ebank.standardchartered.com.tw"
                or LOGIN_PATH_HINT in parsed.path.lower()
            ):
                return False
            scopes = [
                page,
                *(frame for frame in page.frames if frame is not page.main_frame),
            ]
            for scope in scopes:
                with error_step('SCB-03-010-001'):
                    controls = scope.locator(
                        "[name='__reCaptcha'], input[type='password']"
                    )
                with error_step('SCB-03-011-001'):
                    if any(
                        error_step('SCB-03-700-001')(lambda: error_step('SCB-03-701-001')(lambda: controls.nth(index))().is_visible())()
                        for index in range(error_step('SCB-03-702-001')(lambda: controls.count())())
                    ):
                        return False
            with error_step('SCB-03-012-001'):
                body = "\n".join(
                    scope.evaluate("() => document.body && document.body.innerText || ''")
                    or ""
                    for scope in scopes
                )
        except Exception:
            return False
        return (
            len(body) >= 500
            and ("登出" in body or "Logout" in body)
            and ("理財總覽" in body or "帳戶綜覽" in body)
        )

    def login(self, page) -> bool:
        return self._shared_login(page)

    def prepare_login_page(self, page) -> None:
        try:
            with error_step('SCB-01-001-001'):
                page.goto("https://ebank.standardchartered.com.tw/scb/", wait_until="commit", timeout=15000)
            with error_step('SCB-01-002-001'):
                page.wait_for_timeout(5000)
        except Exception as _action_error:
            _log(format_error_id(exception_error_id(_action_error, bank="scb") or 'SCB-01-003-001', bank="scb"))
            pass
        if self._logged_in(page):
            return
        try:
            current = urlparse(page.url or "")
            if (
                (current.hostname or "").lower()
                != "ebank.standardchartered.com.tw"
                or LOGIN_PATH_HINT not in current.path.lower()
            ):
                with error_step('SCB-01-004-001'):
                    page.goto(BASE, wait_until="commit", timeout=15000)
            with error_step('SCB-01-005-001'):
                page.wait_for_timeout(8000)
        except Exception as error:
            raise with_error_code(ScbLoginError('無法安全準備登入頁面；未送出登入'), exception_error_id(error, bank="scb") or 'SCB-01-006-001') from None

    def is_authenticated(self, page) -> bool:
        return self._logged_in(page)

    def login_checkpoint_rules(self) -> tuple[LoginCheckpointRule, ...]:
        all_phases = tuple(CheckpointPhase)
        post_settle = (
            CheckpointPhase.POST_SUBMIT,
            CheckpointPhase.POST_SUBMIT_SETTLE,
        )
        otp_body = re.compile(
            r"^[\s\S]*(?:OTP|一次性密碼|簡訊驗證碼|裝置驗證|信任此裝置|新裝置登入)[\s\S]*$",
            re.IGNORECASE,
        )
        password_body = re.compile(
            r"^[\s\S]*(?:(?:立即|必須|請先|需要|需)\s*(?:修改|變更|重設)\s*"
            r"(?:您的?)?\s*密碼|密碼\s*(?:已)?(?:到期|過期)|"
            r"密碼\s*強制\s*(?:修改|變更|重設)|"
            r"強制\s*(?:修改|變更|重設)\s*(?:您的?)?\s*密碼)[\s\S]*$"
        )
        captcha_body = re.compile(
            r"^[\s\S]*(?<![A-Za-z0-9_])CAPT\d+(?![A-Za-z0-9_])[\s\S]*$",
            re.IGNORECASE,
        )
        error_body = re.compile(
            r"^[\s\S]*(?:HIBERR_\d+|(?<![A-Za-z0-9_])E\d{3,4}(?!\d)|"
            r"帳號不存在|密碼不正確|帳號[^\r\n]{0,40}鎖定|登入失敗)[\s\S]*$",
            re.IGNORECASE,
        )
        duplicate_body = re.compile(
            r"^\s*(?:您可能先前未正常登出(?:\s*或\s*已經在別台裝置登入)?|"
            r"已經在別台裝置登入)(?:[\s.…，,。]*其他裝置將會被登出)?"
            r"[\s.…，,。]*確定登入\s*$"
        )
        modal_scopes = (("modal", ".modal.show"), ("dialog", "[role='dialog']"))
        simple_scopes = (("error", ".error"), ("alert", ".alert"), ("role-alert", "[role='alert']"))
        return (
            *(
                LoginCheckpointRule(
                    name=f"scb-otp-required-{suffix}",
                    bank="scb",
                    phases=all_phases,
                    kind=CheckpointKind.OTP_REQUIRED,
                    container_selector=selector,
                    required_body_pattern=otp_body,
                )
                for suffix, selector in modal_scopes
            ),
            *(
                LoginCheckpointRule(
                    name=f"scb-password-change-required-{suffix}",
                    bank="scb",
                    phases=all_phases,
                    kind=CheckpointKind.PASSWORD_CHANGE_REQUIRED,
                    container_selector=selector,
                    required_body_pattern=password_body,
                )
                for suffix, selector in modal_scopes
            ),
            *(
                LoginCheckpointRule(
                    name=f"scb-explicit-login-error-{suffix}",
                    bank="scb",
                    phases=post_settle,
                    kind=CheckpointKind.EXPLICIT_LOGIN_ERROR,
                    container_selector=selector,
                    required_body_pattern=error_body,
                )
                for suffix, selector in simple_scopes
            ),
            *(
                LoginCheckpointRule(
                    name=f"scb-captcha-retry-{suffix}",
                    bank="scb",
                    phases=(CheckpointPhase.POST_SUBMIT,),
                    kind=CheckpointKind.CAPTCHA_RETRY,
                    container_selector=selector,
                    required_body_pattern=captcha_body,
                )
                for suffix, selector in simple_scopes
            ),
            *(
                LoginCheckpointRule(
                    name=f"scb-duplicate-session-{suffix}",
                    bank="scb",
                    phases=post_settle,
                    kind=CheckpointKind.DUPLICATE_SESSION,
                    container_selector=selector,
                    action_texts=("確定登入",),
                    required_body_pattern=duplicate_body,
                )
                for suffix, selector in modal_scopes
            ),
            LoginCheckpointRule(
                name="scb-unknown-modal",
                bank="scb",
                phases=all_phases,
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector=".modal.show",
            ),
            LoginCheckpointRule(
                name="scb-unknown-dialog",
                bank="scb",
                phases=all_phases,
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector="[role='dialog']",
            ),
            LoginCheckpointRule(
                name="scb-login-form-still-visible",
                bank="scb",
                phases=post_settle,
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector="[name='__reCaptcha']",
            ),
        )

    @staticmethod
    def _page_scopes(page):
        return [
            page,
            *(frame for frame in page.frames if frame is not page.main_frame),
        ]

    @classmethod
    def _click_unique_refresh(cls, page) -> bool:
        matches = []
        for scope in cls._page_scopes(page):
            with error_step('SCB-02-078-001'):
                actions = scope.locator("button, a")
            with error_step('SCB-02-079-001'):
                for index in range(actions.count()):
                    with error_step('SCB-02-080-001'):
                        action = actions.nth(index)
                    with error_step('SCB-02-081-001'):
                        if (
                            error_step('SCB-02-703-001')(lambda: action.is_visible())()
                            and error_step('SCB-02-704-001')(lambda: action.is_enabled())()
                            and " ".join(error_step('SCB-02-705-001')(lambda: action.inner_text())().split()) == "重新產生"
                        ):
                            matches.append(action)
        if len(matches) != 1:
            return False
        with error_step('SCB-02-082-001'):
            matches[0].click()
        with error_step('SCB-02-083-001'):
            page.wait_for_timeout(1500)
        return True

    @staticmethod
    def _keyboard_fill(page, field, value: str, *, _error_role="generic") -> None:
        with error_step({0: 'SCB-02-017-001', 1: 'SCB-02-027-001', 2: 'SCB-02-037-001', 'captcha': 'SCB-02-047-001', 'generic': 'SCB-02-057-001'}[_error_role]):
            field.click()
        with error_step({0: 'SCB-02-018-001', 1: 'SCB-02-028-001', 2: 'SCB-02-038-001', 'captcha': 'SCB-02-048-001', 'generic': 'SCB-02-058-001'}[_error_role]):
            page.wait_for_timeout(150)
        with error_step({0: 'SCB-02-019-001', 1: 'SCB-02-029-001', 2: 'SCB-02-039-001', 'captcha': 'SCB-02-049-001', 'generic': 'SCB-02-059-001'}[_error_role]):
            field.click(click_count=3)
        with error_step({0: 'SCB-02-020-001', 1: 'SCB-02-030-001', 2: 'SCB-02-040-001', 'captcha': 'SCB-02-050-001', 'generic': 'SCB-02-060-001'}[_error_role]):
            page.wait_for_timeout(100)
        with error_step({0: 'SCB-02-021-001', 1: 'SCB-02-031-001', 2: 'SCB-02-041-001', 'captcha': 'SCB-02-051-001', 'generic': 'SCB-02-061-001'}[_error_role]):
            page.keyboard.press("Backspace")
        with error_step({0: 'SCB-02-022-001', 1: 'SCB-02-032-001', 2: 'SCB-02-042-001', 'captcha': 'SCB-02-052-001', 'generic': 'SCB-02-062-001'}[_error_role]):
            page.wait_for_timeout(100)
        with error_step({0: 'SCB-02-023-001', 1: 'SCB-02-033-001', 2: 'SCB-02-043-001', 'captcha': 'SCB-02-053-001', 'generic': 'SCB-02-063-001'}[_error_role]):
            page.keyboard.type(value, delay=80)
        with error_step({0: 'SCB-02-024-001', 1: 'SCB-02-034-001', 2: 'SCB-02-044-001', 'captcha': 'SCB-02-054-001', 'generic': 'SCB-02-064-001'}[_error_role]):
            page.wait_for_timeout(300)
        with error_step({0: 'SCB-02-025-001', 1: 'SCB-02-035-001', 2: 'SCB-02-045-001', 'captcha': 'SCB-02-055-001', 'generic': 'SCB-02-065-001'}[_error_role]):
            if len(field.input_value()) != len(value):
                raise with_error_code(ScbLoginError('登入欄位輸入長度不符；未送出登入'), {0: 'SCB-02-026-001', 1: 'SCB-02-036-001', 2: 'SCB-02-046-001', 'captcha': 'SCB-02-056-001', 'generic': 'SCB-02-066-001'}[_error_role])

    def prepare_captcha_resubmit(self, page) -> None:
        try:
            if not self._click_unique_refresh(page):
                raise with_error_code(ScbLoginError('無法安全更新驗證碼；未送出登入'), 'SCB-02-070-001')
            for _ in range(10):
                stale_error = False
                for scope in self._page_scopes(page):
                    for selector in (".error", ".alert", "[role='alert']"):
                        with error_step({'.error': 'SCB-02-071-030', '.alert': 'SCB-02-071-031', "[role='alert']": 'SCB-02-071-032'}[selector]):
                            alerts = scope.locator(selector)
                        with error_step({'.error': 'SCB-02-072-030', '.alert': 'SCB-02-072-031', "[role='alert']": 'SCB-02-072-032'}[selector]):
                            for index in range(alerts.count()):
                                with error_step({'.error': 'SCB-02-073-030', '.alert': 'SCB-02-073-031', "[role='alert']": 'SCB-02-073-032'}[selector]):
                                    alert = alerts.nth(index)
                                with error_step('SCB-02-074-001'):
                                    if error_step({'.error': 'SCB-02-706-030', '.alert': 'SCB-02-706-031', "[role='alert']": 'SCB-02-706-032'}[selector])(lambda: alert.is_visible())() and re.search(
                                        r"(?<![A-Za-z0-9_])CAPT\d+(?![A-Za-z0-9_])",
                                        " ".join(error_step({'.error': 'SCB-02-707-030', '.alert': 'SCB-02-707-031', "[role='alert']": 'SCB-02-707-032'}[selector])(lambda: alert.inner_text())().split()),
                                        re.IGNORECASE,
                                    ):
                                        stale_error = True
                if not stale_error:
                    return
                with error_step('SCB-02-075-001'):
                    page.wait_for_timeout(300)
        except ScbLoginError:
            raise
        except Exception as error:
            raise with_error_code(ScbLoginError('無法安全更新驗證碼；未送出登入'), exception_error_id(error, bank="scb") or 'SCB-02-076-001') from None
        raise with_error_code(ScbLoginError('驗證碼錯誤狀態未清除；未送出登入'), 'SCB-02-077-001')

    @classmethod
    def _visible_alert_state(cls, page) -> tuple[bool, bool, bool]:
        captcha = explicit = other = False
        for scope in cls._page_scopes(page):
            for selector in (".error", ".alert", "[role='alert']"):
                with error_step({'.error': 'SCB-03-013-030', '.alert': 'SCB-03-013-031', "[role='alert']": 'SCB-03-013-032'}[selector]):
                    alerts = scope.locator(selector)
                with error_step({'.error': 'SCB-03-014-030', '.alert': 'SCB-03-014-031', "[role='alert']": 'SCB-03-014-032'}[selector]):
                    for index in range(alerts.count()):
                        with error_step({'.error': 'SCB-03-015-030', '.alert': 'SCB-03-015-031', "[role='alert']": 'SCB-03-015-032'}[selector]):
                            alert = alerts.nth(index)
                        with error_step({'.error': 'SCB-03-016-030', '.alert': 'SCB-03-016-031', "[role='alert']": 'SCB-03-016-032'}[selector]):
                            if not alert.is_visible():
                                continue
                        with error_step({'.error': 'SCB-03-017-030', '.alert': 'SCB-03-017-031', "[role='alert']": 'SCB-03-017-032'}[selector]):
                            text = " ".join(alert.inner_text().split())
                        if re.search(
                            r"(?:HIBERR_\d+|(?<![A-Za-z0-9_])E\d{3,4}(?!\d)|"
                            r"帳號不存在|密碼不正確|帳號[^\r\n]{0,40}鎖定|登入失敗)",
                            text,
                            re.IGNORECASE,
                        ):
                            explicit = True
                        elif re.search(
                            r"(?<![A-Za-z0-9_])CAPT\d+(?![A-Za-z0-9_])",
                            text,
                            re.IGNORECASE,
                        ):
                            captcha = True
                        else:
                            other = True
        return captcha, explicit, other

    def _ocr_captcha(self, page, max_attempts=5):
        """從 captcha img 抽 base64 → OCR 6 碼純數字（送出前安全重試）。"""
        attempts = min(max(max_attempts, 1), 5)
        for n in range(1, attempts + 1):
            self._ocr_failure_id = None
            try:
                with error_step('SCB-02-067-001'):
                    cap_src = page.evaluate("""() => {
                    for (const img of document.querySelectorAll('img')) {
                        if (img.offsetParent === null) continue;
                        const cls = (img.className || '').toString();
                        const w = img.naturalWidth || img.width;
                        const h = img.naturalHeight || img.height;
                        if (cls.includes('is-max-width') && w >= 100 && w <= 250 && h >= 30 && h <= 80) {
                            return img.src;
                        }
                    }
                    return null;
                }""")
                if not isinstance(cap_src, str):
                    raise with_error_code(ValueError(), 'SCB-02-068-001')
                elif not cap_src.startswith('data:image'):
                    raise with_error_code(ValueError(), 'SCB-02-068-002')
                elif ',' not in cap_src:
                    raise with_error_code(ValueError(), 'SCB-02-068-003')
                with error_step('SCB-02-850-001'):
                    raw = base64.b64decode(cap_src.split(",", 1)[1], validate=True)
                with error_step('SCB-02-069-001'):
                    text = ocr_bytes(raw, expected_len=6, alnum_only=True)
                if isinstance(text, str) and len(text) == 6 and text.isdigit():
                    return text
            except Exception as error:
                self._ocr_failure_id = exception_error_id(error, bank="scb")
            if n < attempts:
                try:
                    if not self._click_unique_refresh(page):
                        return None
                except Exception as error:
                    self._ocr_failure_id = exception_error_id(error, bank="scb")
                    return None
        return None

    def submit_credentials_once(self, page) -> None:
        try:
            with error_step('SCB-02-001-001'):
                page.wait_for_selector(
                    "input[name='__reCaptcha']", state="visible", timeout=15000
                )
            with error_step('SCB-02-002-001'):
                inputs = page.locator("input")
            layout = []
            with error_step('SCB-02-084-001'):
                for index in range(inputs.count()):
                    with error_step('SCB-02-003-001'):
                        field = inputs.nth(index)
                    with error_step('SCB-02-004-001'):
                        if not field.is_visible():
                            continue
                    with error_step('SCB-02-088-001'):
                        field_type = (field.get_attribute("type") or "text").lower()
                    if field_type == "checkbox":
                        continue
                    with error_step('SCB-02-085-001'):
                        box = field.bounding_box()
                    if box is None:
                        raise with_error_code(ScbLoginError('登入欄位無法安全確認；未送出登入'), 'SCB-02-005-001')
                    with error_step('SCB-02-087-001'):
                        layout.append(
                            (
                                box["y"],
                                field_type,
                                error_step('SCB-02-708-001')(lambda: field.get_attribute("name"))() or "",
                                error_step('SCB-02-709-001')(lambda: field.get_attribute("maxlength"))(),
                                field,
                            )
                        )
            layout.sort(key=lambda item: item[0])
            with error_step('SCB-02-006-001'):
                if len(layout) != 4:
                    raise with_error_code(ScbLoginError('登入欄位無法安全確認；未送出登入'), 'SCB-02-007-001')
                if tuple((item[1] for item in layout)) != ('text', 'password', 'password', 'tel'):
                    raise with_error_code(ScbLoginError('登入欄位無法安全確認；未送出登入'), 'SCB-02-007-002')
                if layout[3][2] != '__reCaptcha':
                    raise with_error_code(ScbLoginError('登入欄位無法安全確認；未送出登入'), 'SCB-02-007-003')
                for _error_layout_index, item in enumerate(layout):
                    with error_step({0: 'SCB-02-007-009', 1: 'SCB-02-007-010', 2: 'SCB-02-007-011', 3: 'SCB-02-007-012'}[_error_layout_index]):
                        if not item[4].is_visible():
                            raise with_error_code(ScbLoginError('登入欄位無法安全確認；未送出登入'), {0: 'SCB-02-007-005', 1: 'SCB-02-007-006', 2: 'SCB-02-007-007', 3: 'SCB-02-007-008'}[_error_layout_index])
                    with error_step({0: 'SCB-02-007-017', 1: 'SCB-02-007-018', 2: 'SCB-02-007-019', 3: 'SCB-02-007-020'}[_error_layout_index]):
                        if not item[4].is_enabled():
                            raise with_error_code(ScbLoginError('登入欄位無法安全確認；未送出登入'), {0: 'SCB-02-007-013', 1: 'SCB-02-007-014', 2: 'SCB-02-007-015', 3: 'SCB-02-007-016'}[_error_layout_index])

            for error_role, (item, value) in enumerate(zip(
                layout[:3],
                (self.creds.national_id, self.creds.username, self.creds.password),
                strict=True,
            )):
                self._keyboard_fill(page, item[4], value, _error_role=error_role)
            self._ocr_failure_id = None
            try:
                captcha = self._ocr_captcha(page, max_attempts=1)
            finally:
                ocr_failure_id = self._ocr_failure_id
                self._ocr_failure_id = None
            if not captcha:
                raise with_error_code(captcha_unreadable(ScbLoginError('無法安全辨識驗證碼；未送出登入')), ocr_failure_id or 'SCB-02-008-001')
            self._keyboard_fill(page, layout[3][4], captcha, _error_role="captcha")

            with error_step('SCB-02-009-001'):
                candidates = page.locator("button[type='submit']")
            eligible = []
            with error_step('SCB-02-086-001'):
                for index in range(candidates.count()):
                    with error_step('SCB-02-010-001'):
                        candidate = candidates.nth(index)
                    with error_step('SCB-02-011-001'):
                        if (
                            error_step('SCB-02-710-001')(lambda: candidate.is_visible())()
                            and error_step('SCB-02-711-001')(lambda: candidate.is_enabled())()
                            and " ".join(error_step('SCB-02-712-001')(lambda: candidate.inner_text())().split()) == "登入"
                            and "b-bg-green-d" in (error_step('SCB-02-713-001')(lambda: candidate.get_attribute("class"))() or "").split()
                        ):
                            eligible.append(candidate)
            if len(eligible) != 1:
                raise with_error_code(ScbLoginError('找不到唯一且可操作的登入按鈕；未送出登入'), 'SCB-02-012-001')
            button = eligible[0]
            stale_captcha, stale_explicit, stale_other = self._visible_alert_state(page)
            if stale_explicit:
                raise with_error_code(ScbLoginError('登入頁已有未解決提示；未送出登入'), 'SCB-02-013-001')
            elif stale_other:
                raise with_error_code(ScbLoginError('登入頁已有未解決提示；未送出登入'), 'SCB-02-013-002')
        except ScbLoginError:
            raise
        except Exception as error:
            raise with_error_code(ScbLoginError('登入欄位無法安全填寫；未送出登入'), exception_error_id(error, bank="scb") or 'SCB-02-014-001') from None

        try:
            with error_step('SCB-02-015-001'):
                button.click(timeout=8000)
        except Exception as error:
            raise with_error_code(ScbLoginError('登入送出狀態不明；禁止自動重試'), exception_error_id(error, bank="scb") or 'SCB-02-016-001') from None

        try:
            for _ in range(30):
                with error_step('SCB-03-001-001'):
                    page.wait_for_timeout(1000)
                if self._logged_in(page):
                    return
                modal_visible = False
                for scope in self._page_scopes(page):
                    for selector in (".modal.show", "[role='dialog']"):
                        with error_step({'.modal.show': 'SCB-03-002-002', "[role='dialog']": 'SCB-03-002-003'}[selector]):
                            blockers = scope.locator(selector)
                        with error_step('SCB-03-003-001'):
                            if any(
                                error_step({'.modal.show': 'SCB-03-714-002', "[role='dialog']": 'SCB-03-714-003'}[selector])(lambda: error_step({'.modal.show': 'SCB-03-715-002', "[role='dialog']": 'SCB-03-715-003'}[selector])(lambda: blockers.nth(index))().is_visible())()
                                for index in range(error_step({'.modal.show': 'SCB-03-716-002', "[role='dialog']": 'SCB-03-716-003'}[selector])(lambda: blockers.count())())
                            ):
                                modal_visible = True
                captcha, explicit, other = self._visible_alert_state(page)
                if explicit:
                    return
                if stale_captcha:
                    if modal_visible:
                        raise with_error_code(ScbLoginError('登入送出後出現衝突狀態；禁止自動重試'), 'SCB-03-004-001')
                    elif other:
                        raise with_error_code(ScbLoginError('登入送出後出現衝突狀態；禁止自動重試'), 'SCB-03-005-001')
                elif captcha:
                    if modal_visible:
                        raise with_error_code(ScbLoginError('登入送出後出現衝突狀態；禁止自動重試'), 'SCB-03-006-001')
                    elif other:
                        raise with_error_code(ScbLoginError('登入送出後出現衝突狀態；禁止自動重試'), 'SCB-03-007-001')
                if stale_captcha:
                    if not captcha:
                        stale_captcha = False
                elif captcha:
                    return
                if modal_visible or other:
                    return
        except Exception as error:
            raise with_error_code(ScbLoginError('登入送出後狀態無法安全確認；禁止自動重試'), exception_error_id(error, bank="scb") or 'SCB-03-008-001') from None
        if stale_captcha:
            raise with_error_code(ScbLoginError('登入送出後沒有新的驗證碼結果；禁止自動重試'), 'SCB-03-009-001')


    @staticmethod
    def _deposit_accounts(page) -> list[dict]:
        # Live 2026-10-03: from card pages only the menu <em> navigates; the <li>/<a>
        # wrappers swallow the click. Try innermost first, stop once the page shows.
        click = """(s) => { const e = [...document.querySelectorAll(s)]
            .find(e => e.offsetParent && (e.innerText || '').trim() === '帳戶綜覽');
            if (!e) return false; e.click(); return true; }"""
        text = ""
        try:
            for sel in ("em", "a", "li"):
                with error_step('SCB-04-001-001'):
                    if page.evaluate(click, sel) is not True:
                        continue
                with error_step('SCB-04-002-001'):
                    page.wait_for_timeout(8000)
                with error_step('SCB-04-003-001'):
                    text = page.evaluate("() => document.body.innerText") or ""
                if "結構型商品查詢" in text:
                    break
            else:
                return []
        except Exception as _action_error:
            with contextlib.suppress(Exception):
                _log(format_error_id(exception_error_id(_action_error, bank="scb") or 'SCB-04-004-001', bank="scb"))
            return []
        out, seen = [], set()
        for m in re.finditer(r"(?P<name>[^\n]*存款) (?P<acct>\d{3,6}●+\d{3,6})\s*\n\s*存款總金額 (?P<ccy>[A-Z]{3}) (?P<bal>-?[\d,]+(?:\.\d+)?)", text):
            key = (m["acct"], m["ccy"])
            if key in seen:
                continue
            seen.add(key)
            out.append({"account_no": m["acct"], "currency": m["ccy"], "type": m["name"].strip(),
                        "balance": m["bal"].replace(",", "")})
        return out

    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        """SCB collect：dashboard + 信用卡綜覽 + 每張卡消費明細 + 帳單查詢 dump。

        2026-06-12 v3 (使用者指正「我有渣打信用卡」+ 要明細):
          1. dashboard text dump (home_text)
          2. 點「信用卡綜覽」→ 攔 crditAcctList API → dump card_text
          3. 依序點每張卡「消費明細」→ dump cards_detail[]
          4. 點「帳單查詢」→ dump bill_text
        """
        out: dict = {}
        with error_step('SCB-06-001-001'):
            page.wait_for_timeout(8000)

        debug_dir = _debug_dir()
        with contextlib.suppress(Exception):
            page.screenshot(path=str(debug_dir / "00_home.png"), full_page=True)

        out["initial_url"] = page.url
        try:
            with error_step('SCB-04-005-001'):
                out["title"] = page.title()
        except Exception as _action_error:
            with contextlib.suppress(Exception):
                _log(format_error_id(exception_error_id(_action_error, bank="scb") or 'SCB-07-001-001', bank="scb"))
            out["title"] = ""

        try:
            with error_step('SCB-06-002-001'):
                txt = page.evaluate("() => (document.body.innerText || '').slice(0, 15000)") or ""
        except Exception as _action_error:
            with contextlib.suppress(Exception):
                _log(format_error_id(exception_error_id(_action_error, bank="scb") or 'SCB-07-002-001', bank="scb"))
            txt = ""
        out["home_text"] = txt
        _log(f"[scb][collect] home text_len={len(txt)}")

        # === A. 點「信用卡綜覽」進信用卡頁 ===
        with error_step('SCB-06-003-001'):
            cc_click = page.evaluate("""() => {
            for (const el of document.querySelectorAll('a, button, span, div, li')) {
                if (el.offsetParent === null) continue;
                const t = (el.textContent || '').trim();
                if (t !== '信用卡綜覽') continue;
                try { el.click(); return {ok: true, tag: el.tagName}; } catch (e) {}
            }
            return {ok: false};
        }""")
        _log(f"[scb][collect] click 信用卡綜覽: {cc_click}")
        with error_step('SCB-06-004-001'):
            page.wait_for_timeout(8000)
        with contextlib.suppress(Exception):
            page.screenshot(path=str(debug_dir / "01_credit_overview.png"), full_page=True)
        out["card_url"] = page.url
        try:
            with error_step('SCB-06-005-001'):
                out["card_text"] = page.evaluate("() => (document.body.innerText || '').slice(0, 15000)") or ""
        except Exception as _action_error:
            with contextlib.suppress(Exception):
                _log(format_error_id(exception_error_id(_action_error, bank="scb") or 'SCB-07-003-001', bank="scb"))
            out["card_text"] = ""
        _log(f"[scb][collect] card 頁 url={out['card_url']} text_len={len(out['card_text'])}")

        # === B. 抓全部「消費明細」按鈕（DOM 全渲染）— 用 bbox dedup ===
        with error_step('SCB-06-006-001'):
            consumption_btns_raw = page.evaluate("""() => {
            const out = [];
            for (const el of document.querySelectorAll('a, button, span, div')) {
                if (el.offsetParent === null) continue;
                const t = (el.textContent || '').trim();
                if (t !== '消費明細') continue;
                if (el.children && el.children.length > 2) continue;
                const r = el.getBoundingClientRect();
                out.push({tag: el.tagName, x: r.x, y: r.y, w: r.width, h: r.height});
            }
            return out;
        }""") or []
        # bbox dedup（同 x±5,y±5 視為同按鈕）
        consumption_btns = []
        for b in consumption_btns_raw:
            is_dup = False
            for c in consumption_btns:
                if abs(b["x"] - c["x"]) < 10 and abs(b["y"] - c["y"]) < 10:
                    is_dup = True
                    break
            if not is_dup:
                consumption_btns.append(b)
        _log(f"[scb][collect] 「消費明細」按鈕 raw={len(consumption_btns_raw)} dedup={len(consumption_btns)}")

        # === C. 依序點每張卡「消費明細」→ mouse.click 查詢按鈕 → dump ===
        cards_detail: list = []
        for i, btn in enumerate(consumption_btns):
            _log(f"[scb][collect] === 卡 {i+1} 消費明細 click ({btn['x']:.0f},{btn['y']:.0f}) ===")
            try:
                with error_step('SCB-06-007-001'):
                    page.mouse.click(btn["x"] + btn["w"] / 2, btn["y"] + btn["h"] / 2)
                with error_step('SCB-06-008-001'):
                    page.wait_for_timeout(8000)
                cur_url = page.url
                if "expense" not in cur_url:
                    _log(f"[scb][collect] 卡 {i+1} URL 未跳 expense ({cur_url[:80]})，跳過")
                    continue

                # 找「查詢」BUTTON 的 bbox（限定 button tag，避開 wrapper）
                # 用 mouse.click 觸發真實 form submit
                with error_step('SCB-06-009-001'):
                    query_btn = page.evaluate("""() => {
                    for (const el of document.querySelectorAll('button')) {
                        if (el.offsetParent === null) continue;
                        const t = (el.textContent || '').trim();
                        if (t !== '查詢') continue;
                        const r = el.getBoundingClientRect();
                        return {x: r.x, y: r.y, w: r.width, h: r.height,
                                cls: (el.className || '').toString().slice(0, 80),
                                type: el.type || ''};
                    }
                    return null;
                }""")
                _log(f"[scb][collect] 卡 {i+1} 查詢 button bbox: {query_btn}")
                if query_btn:
                    with error_step('SCB-06-010-001'):
                        page.mouse.click(query_btn["x"] + query_btn["w"] / 2,
                                         query_btn["y"] + query_btn["h"] / 2)
                    _log(f"[scb][collect] 卡 {i+1} mouse.click 查詢 done")
                    with error_step('SCB-06-011-001'):
                        page.wait_for_timeout(8000)  # 等 API + 渲染表格

                with contextlib.suppress(Exception):
                    page.screenshot(path=str(debug_dir / f"02_card_{i+1}_detail.png"), full_page=True)
                detail = {"card_index": i + 1, "url": page.url}
                try:
                    with error_step('SCB-06-012-001'):
                        detail["text"] = page.evaluate("() => (document.body.innerText || '').slice(0, 25000)") or ""
                except Exception as _action_error:
                    with contextlib.suppress(Exception):
                        _log(format_error_id(exception_error_id(_action_error, bank="scb") or 'SCB-07-004-001', bank="scb"))
                    detail["text"] = ""
                _log(f"[scb][collect] 卡 {i+1} url={detail['url']} text_len={len(detail['text'])}")
                cards_detail.append(detail)

                # 回信用卡綜覽
                if i + 1 < len(consumption_btns):
                    with error_step('SCB-06-013-001'):
                        page.evaluate("""() => {
                        for (const el of document.querySelectorAll('a, button, span, div, li')) {
                            if (el.offsetParent === null) continue;
                            const t = (el.textContent || '').trim();
                            if (t !== '信用卡綜覽') continue;
                            try { el.click(); return; } catch (e) {}
                        }
                    }""")
                    with error_step('SCB-06-014-001'):
                        page.wait_for_timeout(6000)
                    # 重新抓 dedup btns
                    with error_step('SCB-06-015-001'):
                        new_raw = page.evaluate("""() => {
                        const out = [];
                        for (const el of document.querySelectorAll('a, button, span, div')) {
                            if (el.offsetParent === null) continue;
                            const t = (el.textContent || '').trim();
                            if (t !== '消費明細') continue;
                            if (el.children && el.children.length > 2) continue;
                            const r = el.getBoundingClientRect();
                            out.push({x: r.x, y: r.y, w: r.width, h: r.height});
                        }
                        return out;
                    }""") or []
                    deduped = []
                    for nb in new_raw:
                        if not any(abs(nb["x"] - d["x"]) < 10 and abs(nb["y"] - d["y"]) < 10 for d in deduped):
                            deduped.append(nb)
                    if len(deduped) > i + 1:
                        consumption_btns[i + 1] = {**consumption_btns[i + 1], **deduped[i + 1]}
            except Exception as e:
                with contextlib.suppress(Exception):
                    _log(format_error_id(exception_error_id(e, bank="scb") or 'SCB-07-005-001', bank="scb"))
                _log(f"[scb][collect] 卡 {i+1} 例外: {e}")

        out["cards_detail"] = cards_detail

        # === D. 點「帳單查詢」 ===
        try:
            with error_step('SCB-06-016-001'):
                page.evaluate("""() => {
                for (const el of document.querySelectorAll('a, button, span, div, li')) {
                    if (el.offsetParent === null) continue;
                    const t = (el.textContent || '').trim();
                    if (t !== '信用卡綜覽') continue;
                    try { el.click(); return; } catch (e) {}
                }
            }""")
            with error_step('SCB-06-017-001'):
                page.wait_for_timeout(6000)
        except Exception as _action_error:
            with contextlib.suppress(Exception):
                _log(format_error_id(exception_error_id(_action_error, bank="scb") or 'SCB-07-006-001', bank="scb"))
            pass

        with error_step('SCB-06-018-001'):
            bill_click = page.evaluate("""() => {
            for (const el of document.querySelectorAll('a, button, span, div')) {
                if (el.offsetParent === null) continue;
                const t = (el.textContent || '').trim();
                if (t !== '帳單查詢') continue;
                if (el.children && el.children.length > 2) continue;
                try { el.click(); return {ok: true, tag: el.tagName}; } catch (e) {}
            }
            return {ok: false};
        }""")
        _log(f"[scb][collect] click 帳單查詢: {bill_click}")
        with error_step('SCB-06-019-001'):
            page.wait_for_timeout(8000)
        with contextlib.suppress(Exception):
            page.screenshot(path=str(debug_dir / "03_bill_query.png"), full_page=True)
        out["bill_url"] = page.url
        try:
            with error_step('SCB-06-020-001'):
                out["bill_text"] = page.evaluate("() => (document.body.innerText || '').slice(0, 20000)") or ""
        except Exception as _action_error:
            with contextlib.suppress(Exception):
                _log(format_error_id(exception_error_id(_action_error, bank="scb") or 'SCB-07-007-001', bank="scb"))
            out["bill_text"] = ""
        _log(f"[scb][collect] bill 頁 url={out['bill_url']} text_len={len(out['bill_text'])}")

        out["final_url"] = page.url
        out["_all_endpoints"] = sorted({h.endpoint for h in collector.hits if h.resp_json})

        # 完整 dump API JSON
        api_responses: dict = {}
        for h in collector.hits:
            if h.resp_json is None:
                continue
            ep = h.endpoint
            api_responses.setdefault(ep, []).append({
                "url": h.url, "method": h.method, "status": h.status,
                "resp": h.resp_json, "req_body": h.req_body,
            })
        out["api_responses"] = api_responses
        # Live 2026-10-03: deposit accounts were never collected. API values are
        # E2EE hex, so read the rendered 帳戶綜覽 page (masked no. + balance).
        out["accounts"] = self._deposit_accounts(page)
        if not out['accounts']:
            raise with_error_code(RuntimeError('scb-accounts-empty'), 'SCB-04-006-001')
        with error_step('SCB-07-008-001'):
            publish_card_bill_facts(out, [])
        _log(f"[scb][collect] dump {len(api_responses)} 個 endpoint resp_json")
        _log(f"[scb][collect] 攔到 {len(out['_all_endpoints'])} 個 endpoint: {out['_all_endpoints'][:15]}")
        return BankCollectResult(**out)

def _debug_dir() -> Path:
    from backend.core.store import _data_root
    d = _data_root() / "scb_collect"
    d.mkdir(parents=True, exist_ok=True)
    return d


if __name__ == "__main__":
    import json
    crawler = ScbCrawler()
    try:
        result = crawler.run(login_url=BASE, headless=False)
    except ScbLoginError as e:
        result = {"error": "login_failed_stop", "detail": str(e)}

    out_file = Path(__file__).resolve().parents[1] / "data" / "scb_collected.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"\n[done] 已存: {out_file}")
