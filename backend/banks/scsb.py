#!/usr/bin/env python3
"""Shanghai Commercial & Savings Bank (SCSB) iBank crawler.

上海商業儲蓄銀行 SCSB iBank 個人網銀抓取器。

登入入口：https://ibank.scsb.com.tw/ → 跳 https://ebank.scsb.com.tw/ibap/ibap/page#/ibap/tr/10/01
流程：開頁 → 關 modal → 填 #userId/#idNumber/#pppd/#verified
      → OCR `.ved_img` CSS background-image base64（純數字 5 碼）
      → 點橘紅 `button.btn-gradient` text='Log in'

⚠️ 鐵律（見 wiki/concepts/taiwan-bank-login-retry-account-lockout-lesson.md）：
   login API 失敗**絕不自動重打**——max_attempts=1 硬上限。
   只有 OCR 階段（送出前）可換圖重試；點下 Log in 失敗就 raise 中止。
   SCSB 錯 3 次即停用，2026-06-10 已踩過鎖帳號雷一次。

API 加密：SCSB 所有 ResponseData 是 base64 對稱加密（需 ppkey 解），
collect 走「DOM innerText regex」務實路線，不解密 API。
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qsl, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.base import BankCollectResult, BankCrawler, ResponseCollector, captcha_unreadable
from backend.core.card_bills import publish_card_bill_facts
from backend.core.captcha import ocr_bytes
from backend.core.creds import ScsbCreds
from backend.core.error_codes import error_step, exception_error_id, with_error_code
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointPhase,
    LoginCheckpointRule,
)

BASE = "https://ibank.scsb.com.tw/"

SEL_SID = "#userId"
SEL_USER = "#idNumber"
SEL_PWD = "#pppd"
SEL_CAP = "#verified"
SEL_CAP_IMG = ".ved_img"

# The positional row parser needs column headers, not keywords in a summary cell.
_TWD_TABLE_HEADERS_JS = r"""table => {
    const rendered = element => element?.checkVisibility({
        visibilityProperty: true, opacityProperty: true,
    });
    const row = [...table.rows].find(rendered);
    return [
        /時間|Time/i, /摘要|Summary/i, /支出|Expense/i,
        /存入|Deposit/i, /結餘|Balance/i,
    ].every((pattern, index) => {
        const cell = row?.cells[index];
        return rendered(cell) && pattern.test(cell.innerText || '');
    });
}"""

_CAPTCHA_BACKGROUND = re.compile(
    r"^url\((?P<quote>[\"']?)data:image/(?:png|jpe?g|gif|webp);base64,"
    r"(?P<payload>[A-Za-z0-9+/]+={0,2})(?P=quote)\)$",
    re.IGNORECASE,
)


def _log(*a):
    print(*a, file=sys.stderr)


def _safe_select_inventory(selects: list[dict]) -> list[dict]:
    return [
        {"option_count": len(item.get("options") or [])}
        for item in selects
    ]


class ScsbLoginError(RuntimeError):
    """SCSB login 送出後失敗——立刻中止，絕不自動重打（防鎖帳號）。"""


class ScsbCrawler(BankCrawler):
    USES_SHARED_LOGIN_CHECKPOINTS: ClassVar[bool] = True
    HISTORY_COVERAGE_REQUIRED: ClassVar[bool] = True
    HISTORY_COVERAGE_DOMAINS: ClassVar[frozenset[str]] = frozenset({
        "twd_transactions",
    })
    CREDENTIAL_HOSTS = frozenset({"ibank.scsb.com.tw", "ebank.scsb.com.tw"})

    def __init__(self):
        super().__init__(name="scsb")
        self._cleanup_private_artifacts()
        history_mode = os.environ.get("BANK_CRAWLER_HISTORY_MODE", "full")
        if history_mode not in {"full", "incremental"}:
            raise with_error_code(ValueError(f'invalid BANK_CRAWLER_HISTORY_MODE: {history_mode!r}'), 'SCSB-07-300-001')
        self.full_history = history_mode == "full"
        self.creds = ScsbCreds.load()

    @staticmethod
    def _cleanup_private_artifacts() -> None:
        from backend.core.store import _data_root

        debug_dir = _data_root() / "scsb_collect"
        with error_step('SCSB-07-301-001'):
            debug_dir.mkdir(parents=True, exist_ok=True)
        with error_step('SCSB-07-302-001'):
            (Path(__file__).resolve().parents[1] / "data" / "scsb_collected.json").unlink(
                missing_ok=True,
            )
        for legacy_name in (
            "overview.png", "card.png", "twd.png", "twd_inquiry.png",
            "twd_inquiry_form.png", "twd_inquiry_results.png",
            "card_inq_unbilled.png", "card_inq_unbilled_results.png",
            "card_inq_current.png", "card_inq_current_results.png",
            "card_inq_statement.png", "card_inq_statement_results.png",
        ):
            with error_step('SCSB-07-303-001'):
                (debug_dir / legacy_name).unlink(missing_ok=True)

    def _host_filter(self) -> str:
        return "scsb.com"

    def _logged_in(self, page, *, _auth_probe_ids=None) -> bool:
        """Pure one-shot positive check; the shared lifecycle owns waiting."""
        if _auth_probe_ids is not None:
            _auth_probe_ids.clear()
        try:
            current = urlparse(page.url or "")
            path = current.path.lower()
            if (
                not self._exact_https_origin_allowed(
                    page.url, frozenset({"ebank.scsb.com.tw"})
                )
                or not path.startswith(("/aply/", "/ibhm/"))
            ):
                return False
            with error_step('SCSB-03-500-001'):
                controls = page.locator(
                    "#userId, #idNumber, #pppd, #verified, .ved_img"
                )
            with error_step('SCSB-03-301-001'):
                if any(
                    error_step('SCSB-03-500-004')(lambda: error_step('SCSB-03-500-003')(lambda: controls.nth(index))().is_visible())()
                    for index in range(error_step('SCSB-03-500-002')(lambda: controls.count())())
                ):
                    return False
            with error_step('SCSB-03-302-001'):
                body = error_step('SCSB-03-540-002')(lambda: error_step('SCSB-03-540-001')(lambda: page.locator("body"))().inner_text())()
        except Exception as error:
            if _auth_probe_ids is not None:
                error_id = exception_error_id(error, bank='scsb')
                if error_id in ('SCSB-03-500-001', 'SCSB-03-500-002', 'SCSB-03-500-003', 'SCSB-03-500-004',
                 'SCSB-03-540-001', 'SCSB-03-540-002'):
                    _auth_probe_ids[:] = [error_id]
            return False
        identities = ("Hello", "Last Login", "登出", "Logout")
        menus = (
            "My Overview",
            "TWD Deposit",
            "Credit Card",
            "我的總覽",
            "台幣存款",
            "信用卡",
        )
        return (
            len(body) >= 500
            and any(identity in body for identity in identities)
            and sum(menu in body for menu in menus) >= 2
        )

    def login(self, page) -> bool:
        return self._shared_login(page)

    def prepare_login_page(self, page) -> None:
        with error_step('SCSB-01-300-001'):
            page.wait_for_timeout(9000)

    def is_authenticated(self, page) -> bool:
        auth_probe_ids = getattr(self, "_login_auth_probe_ids", None)
        return (
            self._logged_in(page) if auth_probe_ids is None
            else self._logged_in(page, _auth_probe_ids=auth_probe_ids)
        )

    def login_checkpoint_rules(self) -> tuple[LoginCheckpointRule, ...]:
        all_phases = tuple(CheckpointPhase)
        post = (
            CheckpointPhase.POST_SUBMIT,
            CheckpointPhase.POST_SUBMIT_SETTLE,
        )
        otp = re.compile(
            r"^[\s\S]{0,400}(?:(?<![A-Za-z])OTP(?=$|[^A-Za-z]|I got it)|一次性密碼|"
            r"簡訊驗證碼|裝置驗證|信任此裝置|新裝置登入|device\s+verification)"
            r"[\s\S]{0,400}$",
            re.IGNORECASE,
        )
        password = re.compile(
            r"^[\s\S]{0,120}(?:(?:必須|強制|請立即|請先|需要|需)\s*"
            r"(?:變更|修改|更新|重設)\s*(?:您的?)?\s*密碼|"
            r"密碼\s*(?:已)?(?:到期|過期)|mandatory\s+password\s+change)"
            r"[\s\S]{0,120}$",
            re.IGNORECASE,
        )
        error = re.compile(
            r"^\s*(?:E4025|(?:帳號|密碼|使用者代碼)"
            r"(?:不正確|錯誤|已停用|已鎖定)|登入失敗|驗證碼(?:錯誤|不正確)|"
            r"Invalid credentials|Account locked)[。.!！：:\s]*$",
            re.IGNORECASE,
        )
        modal_scopes = (
            ("modal", ".modal.show"),
            ("dialog", "[role='dialog']"),
            ("intro", "#intro_alert.custom-modal.show"),
        )
        alert_scopes = (
            ("error", ".error"),
            ("alert", ".alert"),
            ("role-alert", "[role='alert']"),
        )
        intro = LoginCheckpointRule(
            name="scsb-intro-notice",
            error_id='SCSB-03-800-001',
            bank="scsb",
            phases=(CheckpointPhase.PRE_SUBMIT,),
            kind=CheckpointKind.DISMISSIBLE_NOTICE,
            container_selector="#intro_alert.custom-modal.show",
            action_texts=("I got it",),
            max_actions=1,
        )
        fraud_notice = LoginCheckpointRule(
            name="scsb-fraud-notice",
            error_id='SCSB-03-805-001',
            bank="scsb",
            phases=(CheckpointPhase.PRE_SUBMIT,),
            kind=CheckpointKind.DISMISSIBLE_NOTICE,
            container_selector=".custom-modal.show",
            action_selector="button.btn-gradient",
            # Notice copy changes (fraud, maintenance, FISC drills); match the dismiss button, never the body.
            action_texts=("我知道了",),
            max_actions=1,
            first_match_timeout_ms=5000,
        )
        return (
            *(
                LoginCheckpointRule(
                    name=f"scsb-otp-required-{suffix}",
                    error_id={'modal': 'SCSB-03-050-001', 'dialog': 'SCSB-03-055-001', 'intro': 'SCSB-03-060-001'}[suffix],
                    bank="scsb",
                    phases=all_phases,
                    kind=CheckpointKind.OTP_REQUIRED,
                    container_selector=selector,
                    required_body_pattern=otp,
                )
                for suffix, selector in modal_scopes
            ),
            *(
                LoginCheckpointRule(
                    name=f"scsb-password-change-required-{suffix}",
                    error_id={'modal': 'SCSB-03-065-001', 'dialog': 'SCSB-03-070-001', 'intro': 'SCSB-03-075-001'}[suffix],
                    bank="scsb",
                    phases=all_phases,
                    kind=CheckpointKind.PASSWORD_CHANGE_REQUIRED,
                    container_selector=selector,
                    required_body_pattern=password,
                )
                for suffix, selector in modal_scopes
            ),
            *(
                LoginCheckpointRule(
                    name=f"scsb-explicit-login-error-{suffix}",
                    error_id={'error': 'SCSB-03-080-001', 'alert': 'SCSB-03-085-001', 'role-alert': 'SCSB-03-090-001'}[suffix],
                    bank="scsb",
                    phases=post,
                    kind=CheckpointKind.EXPLICIT_LOGIN_ERROR,
                    container_selector=selector,
                    required_body_pattern=error,
                )
                for suffix, selector in alert_scopes
            ),
            intro,
            fraud_notice,
            LoginCheckpointRule(
                name="scsb-unknown-custom-modal",
                error_id='SCSB-03-095-001',
                bank="scsb",
                phases=all_phases,
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector=".custom-modal.show",
            ),
            LoginCheckpointRule(
                name="scsb-unknown-modal",
                error_id='SCSB-03-100-001',
                bank="scsb",
                phases=all_phases,
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector=".modal.show",
            ),
            LoginCheckpointRule(
                name="scsb-unknown-dialog",
                error_id='SCSB-03-105-001',
                bank="scsb",
                phases=all_phases,
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector="[role='dialog']",
            ),
            LoginCheckpointRule(
                name="scsb-login-form-still-visible",
                error_id='SCSB-03-110-001',
                bank="scsb",
                phases=post,
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector=SEL_CAP,
            ),
        )

    @staticmethod
    def _keyboard_fill(page, field, value: str, *, field_role="user") -> None:
        with error_step({'identity': 'SCSB-02-020-001', 'user': 'SCSB-02-050-001', 'password': 'SCSB-02-080-001', 'captcha': 'SCSB-02-110-001'}[field_role]):
            field.click()
        with error_step({'identity': 'SCSB-02-021-001', 'user': 'SCSB-02-051-001', 'password': 'SCSB-02-081-001', 'captcha': 'SCSB-02-111-001'}[field_role]):
            field.click(click_count=3)
        with error_step({'identity': 'SCSB-02-022-001', 'user': 'SCSB-02-052-001', 'password': 'SCSB-02-082-001', 'captcha': 'SCSB-02-112-001'}[field_role]):
            page.keyboard.press("Backspace")
        with error_step({'identity': 'SCSB-02-023-001', 'user': 'SCSB-02-053-001', 'password': 'SCSB-02-083-001', 'captcha': 'SCSB-02-113-001'}[field_role]):
            page.keyboard.type(value, delay=80)
        with error_step({'identity': 'SCSB-02-024-001', 'user': 'SCSB-02-054-001', 'password': 'SCSB-02-084-001', 'captcha': 'SCSB-02-114-001'}[field_role]):
            actual_length = len(field.input_value())
        if actual_length != len(value):
            raise with_error_code(ScsbLoginError("登入欄位輸入長度不符；未送出登入"), {'identity': 'SCSB-02-025-001', 'user': 'SCSB-02-055-001', 'password': 'SCSB-02-085-001', 'captcha': 'SCSB-02-115-001'}[field_role])

    @staticmethod
    def _click_unique_refresh(page) -> bool:
        with error_step('SCSB-07-304-001'):
            refreshes = page.locator(".chg_link")
        with error_step('SCSB-07-305-001'):
            if refreshes.count() != 1:
                return False
        with error_step('SCSB-07-306-001'):
            refresh = refreshes.nth(0)
        with error_step('SCSB-07-307-001'):
            if not refresh.is_visible() or not error_step('SCSB-07-500-001')(lambda: refresh.is_enabled())():
                return False
        with error_step('SCSB-07-308-001'):
            label = " ".join(refresh.inner_text().split())
        if label not in ("", "重新產生", "Refresh"):
            return False
        with error_step('SCSB-07-309-001'):
            refresh.click()
        with error_step('SCSB-07-310-001'):
            page.wait_for_timeout(1500)
        return True

    def _ocr_captcha(self, page, max_attempts=5, *, diagnostics: list[str] | None = None):
        diagnostics = [] if diagnostics is None else diagnostics
        diagnostics.clear()
        attempts = min(max(int(max_attempts), 0), 5)
        for attempt in range(attempts):
            diagnostics.clear()
            try:
                with error_step('SCSB-02-300-001'):
                    images = page.locator(SEL_CAP_IMG)
                with error_step('SCSB-02-301-001'):
                    if images.count() != 1:
                        diagnostics[:] = ['SCSB-02-510-001']
                        return None
                with error_step('SCSB-02-302-001'):
                    image = images.nth(0)
                with error_step('SCSB-02-303-001'):
                    if not image.is_visible():
                        diagnostics[:] = ['SCSB-02-510-002']
                        return None
                with error_step('SCSB-02-304-001'):
                    background = image.evaluate("el => getComputedStyle(el).backgroundImage")
                match = (
                    _CAPTCHA_BACKGROUND.fullmatch(background)
                    if isinstance(background, str)
                    else None
                )
                if match is None:
                    raise with_error_code(ValueError(), 'SCSB-02-305-001')
                with error_step('SCSB-02-510-003'):
                    raw = base64.b64decode(match.group("payload"), validate=True)
                with error_step('SCSB-02-510-004'):
                    text = ocr_bytes(
                        raw,
                        expected_len=5,
                        alnum_only=True,
                        min_confidence=0.98,
                    )
                if isinstance(text, str) and len(text) == 5 and text.isdigit():
                    diagnostics.clear()
                    return text
                diagnostics[:] = ['SCSB-02-510-005' if text is None else 'SCSB-02-510-006']
            except Exception as error:
                diagnostics[:] = [exception_error_id(error, bank="scsb") or 'SCSB-02-510-007']
            if attempt < attempts - 1:
                try:
                    if not self._click_unique_refresh(page):
                        diagnostics[:] = ['SCSB-02-510-008']
                        return None
                except Exception as error:
                    diagnostics[:] = [exception_error_id(error, bank="scsb") or 'SCSB-02-510-009']
                    return None
        return None

    @staticmethod
    def _visible_blocker(page) -> bool:
        for selector in (
            ".modal.show",
            "[role='dialog']",
            ".error",
            ".alert",
            "[role='alert']",
        ):
            with error_step({'.modal.show': 'SCSB-03-550-001', "[role='dialog']": 'SCSB-03-560-001', '.error': 'SCSB-03-570-001', '.alert': 'SCSB-03-580-001', "[role='alert']": 'SCSB-03-590-001'}[selector]):
                candidates = page.locator(selector)
            with error_step('SCSB-03-304-001'):
                if any(
                    error_step({'.modal.show': 'SCSB-03-550-004', "[role='dialog']": 'SCSB-03-560-004', '.error': 'SCSB-03-570-004', '.alert': 'SCSB-03-580-004', "[role='alert']": 'SCSB-03-590-004'}[selector])(lambda: error_step({'.modal.show': 'SCSB-03-550-003', "[role='dialog']": 'SCSB-03-560-003', '.error': 'SCSB-03-570-003', '.alert': 'SCSB-03-580-003', "[role='alert']": 'SCSB-03-590-003'}[selector])(lambda: candidates.nth(index))().is_visible())()
                    for index in range(error_step({'.modal.show': 'SCSB-03-550-002', "[role='dialog']": 'SCSB-03-560-002', '.error': 'SCSB-03-570-002', '.alert': 'SCSB-03-580-002', "[role='alert']": 'SCSB-03-590-002'}[selector])(lambda: candidates.count())())
                ):
                    return True
        return False

    def submit_credentials_once(self, page) -> None:
        try:
            with error_step('SCSB-02-306-001'):
                page.wait_for_selector(SEL_SID, state="visible", timeout=30000)
            with error_step('SCSB-02-307-001'):
                page.wait_for_selector(SEL_CAP_IMG, state="visible", timeout=15000)
            fields = []
            for selector in (SEL_SID, SEL_USER, SEL_PWD, SEL_CAP):
                with error_step({'#userId': 'SCSB-02-308-001', '#idNumber': 'SCSB-02-309-001', '#pppd': 'SCSB-02-310-001', '#verified': 'SCSB-02-311-001'}[selector]):
                    candidates = page.locator(selector)
                with error_step({'#userId': 'SCSB-02-316-001', '#idNumber': 'SCSB-02-317-001', '#pppd': 'SCSB-02-318-001', '#verified': 'SCSB-02-319-001'}[selector]):
                    if candidates.count() != 1:
                        raise with_error_code(ScsbLoginError('登入欄位無法安全確認；未送出登入'), {'#userId': 'SCSB-02-312-001', '#idNumber': 'SCSB-02-313-001', '#pppd': 'SCSB-02-314-001', '#verified': 'SCSB-02-315-001'}[selector])
                with error_step({'#userId': 'SCSB-02-320-001', '#idNumber': 'SCSB-02-321-001', '#pppd': 'SCSB-02-322-001', '#verified': 'SCSB-02-323-001'}[selector]):
                    field = candidates.nth(0)
                with error_step({'#userId': 'SCSB-02-324-001', '#idNumber': 'SCSB-02-325-001', '#pppd': 'SCSB-02-326-001', '#verified': 'SCSB-02-327-001'}[selector]):
                    if not field.is_visible():
                        raise with_error_code(ScsbLoginError('登入欄位無法安全確認；未送出登入'), {'#userId': 'SCSB-02-324-001', '#idNumber': 'SCSB-02-325-001', '#pppd': 'SCSB-02-326-001', '#verified': 'SCSB-02-327-001'}[selector])
                with error_step({'#userId': 'SCSB-02-324-002', '#idNumber': 'SCSB-02-325-002', '#pppd': 'SCSB-02-326-002', '#verified': 'SCSB-02-327-002'}[selector]):
                    if not field.is_enabled():
                        raise with_error_code(ScsbLoginError('登入欄位無法安全確認；未送出登入'), {'#userId': 'SCSB-02-324-002', '#idNumber': 'SCSB-02-325-002', '#pppd': 'SCSB-02-326-002', '#verified': 'SCSB-02-327-002'}[selector])
                fields.append(field)

            for field_role, field, value in zip(
                ("identity", "user", "password"),
                fields[:3],
                (
                    self.creds.national_id,
                    self.creds.user_code,
                    self.creds.password,
                ),
                strict=True,
            ):
                self._keyboard_fill(page, field, value, field_role=field_role)
            diagnostics: list[str] = []
            captcha = self._ocr_captcha(page, max_attempts=1, diagnostics=diagnostics)
            if not captcha:
                raise with_error_code(captcha_unreadable(ScsbLoginError('無法安全辨識驗證碼；未送出登入')), next(iter(diagnostics), 'SCSB-02-328-001'))
            self._keyboard_fill(page, fields[3], captcha, field_role="captcha")

            with error_step('SCSB-02-329-001'):
                candidates = page.locator(
                    "button, input[type='submit'], input[type='button']"
                )
            eligible = []
            with error_step('SCSB-02-333-001'):
                for index in range(candidates.count()):
                    with error_step('SCSB-02-330-001'):
                        candidate = candidates.nth(index)
                    with error_step('SCSB-02-331-001'):
                        if not candidate.is_visible():
                            continue
                    with error_step('SCSB-02-400-001'):
                        if not candidate.is_enabled():
                            continue
                    with error_step('SCSB-02-332-001'):
                        label = " ".join(
                            (
                                candidate.inner_text()
                                or error_step('SCSB-02-520-001')(lambda: candidate.get_attribute("value"))()
                                or ""
                            ).split()
                        )
                    if label in ("Log in", "登入"):
                        eligible.append(candidate)
            if len(eligible) != 1:
                raise with_error_code(ScsbLoginError('找不到唯一且可操作的登入按鈕；未送出登入'), 'SCSB-02-334-001')
            button = eligible[0]
        except ScsbLoginError:
            raise
        except Exception as error:
            raise with_error_code(ScsbLoginError('登入欄位無法安全填寫；未送出登入'), exception_error_id(error, bank="scsb") or 'SCSB-02-335-001') from None

        try:
            with error_step('SCSB-02-336-001'):
                button.click(timeout=8000)
        except Exception as error:
            raise with_error_code(ScsbLoginError('登入送出狀態不明；禁止自動重試'), exception_error_id(error, bank="scsb") or 'SCSB-02-337-001') from None

        try:
            for wait_ms in (12000, 5000, 5000, 5000):
                with error_step('SCSB-03-338-001'):
                    page.wait_for_timeout(wait_ms)
                if self._logged_in(page) or self._visible_blocker(page):
                    return
        except Exception as error:
            raise with_error_code(ScsbLoginError('登入送出後狀態無法安全確認；禁止自動重試'), exception_error_id(error, bank="scsb") or 'SCSB-03-339-001') from None

    # ---------- 抓取（DOM regex 路線，API 加密暫不解）----------
    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        """SCSB ResponseData 是 base64 對稱加密，第一版走 DOM regex 抽餘額/帳戶。

        SCSB SPA 不接受 page.goto 跳 hash route（會回 ERR_HTTP_RESPONSE_CODE_FAILURE），
        必須改 location.hash 設值或 click 左側選單。
        """
        out: dict = {}
        with error_step('SCSB-07-311-001'):
            page.wait_for_timeout(5000)
        with error_step('SCSB-07-312-001'):
            page.wait_for_timeout(2000)

        # SCSB SPA 菜單是 Vue/React，DOM e.click() 不觸發 vnode handler，
        # 必須用 Playwright 原生 page.click（送真實 mouse event）。
        #
        # 2026-08-26 authenticated live DOM：`臺幣存匯` 是獨立 top-level accordion，
        # 並非「我的總覽」底下的 `台幣存款` leaf。點開後，_collect_twd_inquiry
        # 再走 `臺幣帳戶查詢 → 餘額及交易明細查詢`。
        menu = {
            "My Overview": ("我的總覽", "My Overview"),
            "TWD Deposit": ("臺幣存匯", "TWD Deposit"),
            "Credit Card": ("信用卡", "Credit Card Services"),
        }

        navigation_error_id = None

        def _navigate(label, wait_ms=8000):
            """Click one top-level SCSB accordion/navigation control."""
            nonlocal navigation_error_id
            navigation_error_id = None
            spec = menu.get(label)
            if not spec:
                _log(f"  [nav] ❌ unknown label: {label}")
                return False
            action_codes = {'My Overview': ('SCSB-04-700-001', 'SCSB-04-701-001', 'SCSB-04-702-001', 'SCSB-04-703-001', 'SCSB-04-704-001', 'SCSB-04-705-001', 'SCSB-04-706-001', 'SCSB-04-707-001', 'SCSB-04-708-001', 'SCSB-04-709-001'), 'TWD Deposit': ('SCSB-05-700-001', 'SCSB-05-701-001', 'SCSB-05-702-001', 'SCSB-05-703-001', 'SCSB-05-704-001', 'SCSB-05-705-001', 'SCSB-05-706-001', 'SCSB-05-707-001', 'SCSB-05-708-001', 'SCSB-05-709-001'), 'Credit Card': ('SCSB-06-700-001', 'SCSB-06-701-001', 'SCSB-06-702-001', 'SCSB-06-703-001', 'SCSB-06-704-001', 'SCSB-06-705-001', 'SCSB-06-706-001', 'SCSB-06-707-001', 'SCSB-06-708-001', 'SCSB-06-709-001')}[label]
            accordion_zh, en_fallback = spec
            _log(f"[collect] click → {label} (accordion={accordion_zh!r})")
            try:
                with error_step(action_codes[0]):
                    acc_btn = page.query_selector(f"button:has-text({accordion_zh!r})")
                fallback = not acc_btn
                if not fallback:
                    with error_step(action_codes[1]):
                        fallback = not acc_btn.is_visible()
                if fallback:
                    with error_step(action_codes[2]):
                        acc_btn = page.query_selector(f"button:has-text({en_fallback!r})")
                if not acc_btn:
                    navigation_error_id = action_codes[8]
                    _log(f"  [nav] ❌ accordion button {accordion_zh!r} 找不到")
                    return False
                with error_step(action_codes[3]):
                    visible = acc_btn.is_visible()
                if not visible:
                    navigation_error_id = action_codes[9]
                    _log(f"  [nav] ❌ accordion button {accordion_zh!r} 找不到")
                    return False
                with error_step(action_codes[4]):
                    acc_btn.click()
                with error_step(action_codes[5]):
                    page.wait_for_timeout(2000)
                with error_step(action_codes[6]):
                    page.wait_for_timeout(wait_ms - 2000 if wait_ms > 2000 else 1000)
                with error_step(action_codes[7]):
                    page.wait_for_timeout(1500)
                return True
            except Exception as e:
                navigation_error_id = exception_error_id(e, bank="scsb")
                _log(f"  [nav] 失敗: {type(e).__name__}")
                return False

        # 1. My Overview / 我的總覽 — accordion 點開 = navigate
        if not _navigate("My Overview", wait_ms=10000):
            raise with_error_code(RuntimeError('overview-navigation-failed'), navigation_error_id or 'SCSB-07-331-001')
        # SCSB My Overview 預設眼睛 icon 隱藏餘額，需點開
        try:
            with error_step('SCSB-07-332-001'):
                page.evaluate(
                    "(() => { const eyes=[...document.querySelectorAll('i,button,span,div')]"
                    ".filter(e=>e.offsetParent!==null &&"
                    "  /eye|fa-eye|icon-eye|hide|show/i.test((e.className||'')+(e.getAttribute('title')||'')));"
                    " for(const e of eyes) e.click(); return eyes.length; })()",
                )
            with error_step('SCSB-07-333-001'):
                page.wait_for_timeout(3000)
        except Exception:
            pass
        try:
            with error_step('SCSB-07-334-001'):
                out["overview_text"] = (page.evaluate("document.body.innerText") or "")
        except Exception:
            out["overview_text"] = ""
        _log(f"  overview text len={len(out['overview_text'])}")
        overview_twd_inventory = self._extract_overview_twd_inventory(out["overview_text"])
        expected_twd_accounts = {
            account["account_no"] for account in overview_twd_inventory
        }
        overview_inventory_authoritative = self._overview_twd_inventory_authoritative(
            out["overview_text"],
        )
        if not expected_twd_accounts and not overview_inventory_authoritative:
            raise with_error_code(RuntimeError('overview-twd-inventory-unavailable'), 'SCSB-07-335-001')

        # 2. TWD Deposit / 臺幣存匯 — 獨立 top-level accordion
        if expected_twd_accounts:
            if not _navigate("TWD Deposit", wait_ms=8000):
                raise with_error_code(RuntimeError('twd-navigation-failed'), navigation_error_id or 'SCSB-07-336-001')
            try:
                with error_step('SCSB-07-337-001'):
                    out["twd_text"] = (page.evaluate("document.body.innerText") or "")
            except Exception:
                out["twd_text"] = ""

            # 2b. TWD Deposit → Account Balance and Account Statement (深入點 leaf)
            out["twd_inquiry"] = self._collect_twd_inquiry(
                page, expected_twd_accounts)
        else:
            out["twd_text"] = ""
            out["twd_inquiry"] = {"accounts": [], "records": []}

        # 3. Credit Card / 信用卡 — accordion 本身 = navigate
        if not _navigate("Credit Card", wait_ms=8000):
            raise with_error_code(RuntimeError('card-navigation-failed'), navigation_error_id or 'SCSB-07-338-001')
        try:
            with error_step('SCSB-07-339-001'):
                out["card_text"] = (page.evaluate("document.body.innerText") or "")
        except Exception:
            out["card_text"] = ""

        # 3b. Credit Card → Account Management → 信用卡明細 leaf (設計規範：每家都要抓信用卡明細)
        out["card_inquiry"] = self._collect_credit_card_inquiry(page)

        # 從各頁 innerText regex 抽帳號/餘額/卡號
        all_text = "\n".join([out.get("overview_text", ""), out.get("twd_text", ""), out.get("card_text", "")])
        out["accounts"] = self._extract_accounts(all_text)
        parsed_account_identities = {
            (account["account_no"], account.get("currency") or "TWD")
            for account in out["accounts"]
        }
        out["accounts"].extend(
            account for account in overview_twd_inventory
            if (account["account_no"], account.get("currency") or "TWD")
            not in parsed_account_identities
        )
        out["totals"] = self._extract_totals(all_text)
        for raw_text_key in ("overview_text", "twd_text", "card_text"):
            out.pop(raw_text_key, None)

        publish_card_bill_facts(out, [])
        out["history_coverage"] = self._twd_history_coverage(
            out["twd_inquiry"],
            mode="full" if self.full_history else "incremental",
        )
        return BankCollectResult(**out)

    @staticmethod
    def _extract_accounts(text: str) -> list:
        """從 DOM innerText 抽帳號 + 餘額。

        SCSB My Overview 卡片版型：
          活儲存款 / 中壢分行 / 90000000277063 / NT$ 12,345 / 交易明細 ...
        帳號 = 14 碼純數字；幣別前綴 NT$ 或三碼 USD/JPY 等；金額帶逗號。

        2026-06-24 修正：不可用「上一個 type header → 往後 300 字內第一個帳號」；
        總覽上方有「我的貸款總餘額」，會偷吃第一張活儲卡，將 2620...8541
        誤分類成 loan。改以帳號為 anchor，往前取最近的可用 header，且排除「總額」
        這種摘要區 label。
        """
        type_headers = [
            "活儲存款", "活期儲蓄存款", "活期儲蓄", "活期存款",
            "定期儲蓄存款", "定期儲蓄", "定期存款",
            "外幣存款", "外幣活存", "外幣定存", "Foreign Currency Deposits",
            "支票存款", "支存", "Checking", "綜合存款",
            "貸款", "Loan",
        ]
        sorted_headers = sorted(type_headers, key=len, reverse=True)
        header_alt = "|".join(re.escape(h) for h in sorted_headers)
        acct_pattern = re.compile(
            r"(?P<acct>\d{14})[\s\S]{0,120}?"
            r"(?P<cur>NT\$|USD|JPY|EUR|HKD|GBP|AUD|CNY)\s*(?P<bal>[\d,]+(?:\.\d+)?)",
            re.DOTALL,
        )
        header_pattern = re.compile(rf"(?P<type>{header_alt})")

        accounts = []
        seen: set[tuple[str, str]] = set()
        for m in acct_pattern.finditer(text):
            acct_no = m.group("acct")
            cur = m.group("cur").replace("NT$", "TWD")
            identity = (acct_no, cur)
            if identity in seen:
                continue
            seen.add(identity)
            prefix = text[max(0, m.start() - 180):m.start()]
            candidates = [h for h in header_pattern.finditer(prefix)]
            if not candidates:
                type_header = None
            else:
                type_header = candidates[-1].group("type")
            bal = m.group("bal").replace(",", "")
            accounts.append({
                "account_no": acct_no,
                "currency": cur,
                "balance": bal,
                "type_header": type_header,
            })
        return accounts

    @staticmethod
    def _extract_overview_twd_inventory(text: str) -> list[dict]:
        """Extract TWD deposit identities without requiring a readable balance."""
        headers = re.compile(
            r"活儲存款|活期儲蓄存款|活期儲蓄|活期存款|定期儲蓄存款|"
            r"定期儲蓄|定期存款|綜合存款|支票存款|支存|Checking|"
            r"TWD Deposit|臺幣存款|台幣存款|貸款|Loan",
            re.I,
        )
        inventory = []
        seen = set()
        for account in re.finditer(r"(?<!\d)(\d{14})(?!\d)", text):
            candidates = list(headers.finditer(text[max(0, account.start() - 180):account.start()]))
            if not candidates:
                continue
            type_header = candidates[-1].group()
            account_no = account.group(1)
            if re.search(r"貸款|Loan", type_header, re.I) or account_no in seen:
                continue
            suffix = text[account.end():account.end() + 120]
            currency = re.search(r"NT\$|USD|JPY|EUR|HKD|GBP|AUD|CNY", suffix)
            if currency and currency.group() != "NT$":
                continue
            seen.add(account_no)
            inventory.append({
                "account_no": account_no,
                "currency": "TWD",
                "balance": None,
                "type_header": type_header,
            })
        return inventory

    @staticmethod
    def _overview_twd_inventory_authoritative(text: str) -> bool:
        """An overview heading alone may be a partially rendered page, not a true empty set."""
        has_summary = bool(re.search(
            r"我的帳戶摘要|所有帳戶查詢|Account Summary|All Accounts",
            text, re.I,
        ))
        explicit_empty = bool(re.search(
            r"(?:目前沒有|目前尚無|尚無|查無|沒有|無)(?:任何)?(?:台幣|臺幣|TWD)?(?:存款)?帳戶|"
            r"(?:no|do not have any|currently have no)\s+(?:TWD\s+|deposit\s+)?accounts?",
            text, re.I,
        ))
        return has_summary and explicit_empty

    @staticmethod
    def _extract_totals(text: str) -> dict:
        """抽總額類數字（台幣總餘額、外幣總值等）。"""
        totals = {}
        for label, pat in [
            ("twd_total", r"(?:台幣|新台幣|TWD)\s*(?:總額|總計|存款總計|合計|Total)\s*[:：]?\s*([\d,]+)"),
            ("fx_total_twd", r"(?:外幣|FX|Foreign)\s*(?:換算)?(?:總額|總計|合計|Total)\s*[:：]?\s*([\d,]+)"),
        ]:
            m = re.search(pat, text)
            if m:
                totals[label] = m.group(1).replace(",", "")
        return totals

    @staticmethod
    def _twd_inquiry_nav_script() -> str:
        """JS: 中文優先展開 SCSB 台幣存匯交易明細頁，保留英文 fallback。"""
        return """() => {
                const log = [];
                const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const allBtns = () => [...document.querySelectorAll('button.accordion-button, button, a')]
                    .filter(e => e.offsetParent !== null);
                const byAnyText = (needles) => allBtns().find(e => {
                    const t = norm(e.innerText || e.textContent || '');
                    return needles.some(n => t === n || t.includes(n));
                });
                const clickIfCollapsed = (el, label) => {
                    if (!el) return false;
                    const accordion = el.matches('button.accordion-button,[aria-expanded]');
                    const alreadyOpen = accordion && el.getAttribute('aria-expanded') === 'true'
                        && !el.classList.contains('collapsed');
                    if (!alreadyOpen) el.click();
                    log.push(label + ': ' + norm(el.innerText || el.textContent || '').slice(0, 50));
                    return true;
                };
                let btn1 = byAnyText(['臺幣存匯', '台幣存匯', '臺幣存款', '台幣存款', 'TWD Deposit']);
                if (!btn1) { log.push('L1 not found'); return {ok: false, log, _diagnostic: 'SCSB-05-610-001'}; }
                clickIfCollapsed(btn1, 'L1 click');
                return new Promise(resolve => setTimeout(() => {
                    let btn2 = byAnyText(['帳戶查詢', '存款查詢', 'TWD Account Inquiry']);
                    if (btn2) clickIfCollapsed(btn2, 'L2 click');
                    setTimeout(() => {
                        let leaf = byAnyText(['帳戶餘額及交易明細查詢', '交易明細', 'Account Balance and Account Statement', 'Account Balance']);
                        if (!leaf) {
                            const dump = allBtns().map(b => norm(b.innerText || b.textContent || '').slice(0, 60));
                            log.push('L3 leaf not found, visible buttons: ' + JSON.stringify(dump));
                            resolve({ok: false, log, _diagnostic: 'SCSB-05-610-002'});
                            return;
                        }
                        clickIfCollapsed(leaf, 'L3 click');
                        resolve({ok: true, log, url: location.href});
                    }, 1500);
                }, 1500));
            }"""

    @staticmethod
    def _twd_inquiry_period_script(
        *, full_history: bool = True, end_date: date | None = None,
        start_date: date | None = None,
    ) -> str:
        """JS: 首次/強制回補查全量；既有帳號只查最近一個曆月。"""
        script = r"""() => {
            const fullHistory = __FULL_HISTORY__;
            const configuredEndRaw = __END_DATE__;
            const configuredStartRaw = __START_DATE__;
            const visible = (el) => el && el.offsetParent !== null;
            const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
            const inputs = [...document.querySelectorAll('input')].filter(visible);
            const ownContext = (el) => norm([
                el.id, el.name, el.placeholder, el.getAttribute('aria-label'),
                ...(el.labels ? [...el.labels].map(label => label.textContent) : []),
            ].filter(Boolean).join(' '));
            const custom = inputs.find(el =>
                el.type === 'radio' && /(^|\s)(自訂|Custom)(\s|$)/i.test(
                    norm(`${ownContext(el)} ${el.parentElement?.innerText || ''}`)
                )
            );
            const dateInputs = inputs.filter(el => ['date', 'text'].includes(el.type));
            const placeholderDateInputs = dateInputs.filter(el =>
                /^(請輸入日期|Enter date)$/i.test(norm(el.placeholder))
            );
            let startInput = dateInputs.find(el => /(查詢起日|起日|start)/i.test(ownContext(el)));
            let endInput = dateInputs.find(el => /(查詢迄日|迄日|end)/i.test(ownContext(el)));
            if ((!startInput || !endInput) && placeholderDateInputs.length === 2) {
                [startInput, endInput] = placeholderDateInputs;
            } else if ((!startInput || !endInput) && dateInputs.length === 2) {
                [startInput, endInput] = dateInputs;
            }
            const diagnostic = !custom ? 'SCSB-05-616-001' :
                !startInput ? 'SCSB-05-616-002' : !endInput ? 'SCSB-05-616-003' :
                startInput === endInput ? 'SCSB-05-616-004' : null;
            if (diagnostic) {
                return {ok: false, error: 'custom-period-controls-missing', _diagnostic: diagnostic};
            }

            const parseDate = (value) => {
                const match = String(value || '').match(/^(\d{4})[-/](\d{2})[-/](\d{2})$/);
                if (!match) return null;
                const [year, month, day] = match.slice(1).map(Number);
                const date = new Date(year, month - 1, day);
                return date.getFullYear() === year
                    && date.getMonth() === month - 1
                    && date.getDate() === day ? date : null;
            };
            const end = parseDate(configuredEndRaw) || parseDate(endInput.value) || new Date();
            const systemMin = parseDate(startInput.min);
            let start;
            let period;
            if (fullHistory) {
                const year = end.getFullYear() - 1;
                const day = Math.min(end.getDate(), new Date(year, end.getMonth() + 1, 0).getDate());
                const oneYearFloor = new Date(year, end.getMonth(), day);
                oneYearFloor.setDate(oneYearFloor.getDate() + 1);
                start = systemMin && systemMin > oneYearFloor ? systemMin : oneYearFloor;
                period = systemMin && systemMin > oneYearFloor ? 'system-limit' : 'one-year';
            } else {
                start = parseDate(configuredStartRaw);
                if (!start) return {ok: false, error: 'incremental-start-unavailable'};
                if (systemMin && start < systemMin) start = systemMin;
                period = 'cursor';
            }
            if (start > end) return {ok: false, error: 'invalid-period-limit'};

            const format = (date, separator) => [
                date.getFullYear(),
                String(date.getMonth() + 1).padStart(2, '0'),
                String(date.getDate()).padStart(2, '0'),
            ].join(separator);
            custom.dataset.thothScsbTwdPeriod = 'custom';
            startInput.dataset.thothScsbTwdPeriod = 'start';
            endInput.dataset.thothScsbTwdPeriod = 'end';
            return {
                ok: true,
                period,
                start: format(start, '/'),
                end: format(end, '/'),
            };
        }"""
        return (
            script.replace("__FULL_HISTORY__", "true" if full_history else "false")
            .replace(
                "__END_DATE__",
                json.dumps(end_date.isoformat() if end_date else None),
            )
            .replace(
                "__START_DATE__",
                json.dumps(start_date.isoformat() if start_date else None),
            )
        )

    @staticmethod
    def _one_year_floor(end: date) -> date:
        previous_year = end.year - 1
        day = min(end.day, monthrange(previous_year, end.month)[1])
        return date(previous_year, end.month, day) + timedelta(days=1)

    @classmethod
    def _twd_history_coverage(
        cls,
        inquiry: dict,
        *,
        mode: str,
        as_of: date | None = None,
    ) -> dict:
        accounts = inquiry.get("accounts") or []
        domain = {
            "domain": "twd_transactions",
            "expected": [],
            "windows": [],
        }
        if accounts:
            for account in accounts:
                period = account["period"]
                domain["expected"].append({
                    "identity": account["account_no"],
                    "start": period["start"].replace("/", "-"),
                    "end": period["end"].replace("/", "-"),
                })
                domain["windows"].extend(account["windows"])
        else:
            end = as_of or date.today()
            domain["empty_window"] = {
                "start": cls._one_year_floor(end).isoformat(),
                "end": end.isoformat(),
                "status": "explicit_empty",
                "pages": 1,
            }
        return {"mode": mode, "domains": [domain]}

    @staticmethod
    def _apply_twd_period_controls(page, period: dict) -> None:
        """用真實 browser actions 更新 react-datepicker state。"""
        if not period.get("ok"):
            raise with_error_code(RuntimeError(period.get("error") or "lookback-period-unavailable"), {'custom-period-controls-missing': 'SCSB-05-300-002', 'incremental-start-unavailable': 'SCSB-05-300-003', 'invalid-period-limit': 'SCSB-05-300-004'}.get(period.get("error"), "SCSB-05-300-001"))
        controls = {}
        for role in ("custom", "start", "end"):
            with error_step({'custom': 'SCSB-05-100-001', 'start': 'SCSB-05-110-001', 'end': 'SCSB-05-120-001'}[role]):
                controls[role] = page.locator(f'[data-thoth-scsb-twd-period="{role}"]')
        for role, control in controls.items():
            with error_step({'custom': 'SCSB-05-101-001', 'start': 'SCSB-05-111-001', 'end': 'SCSB-05-121-001'}[role]):
                count = control.count()
            if count != 1:
                raise with_error_code(RuntimeError("lookback-period-control-unavailable"), {'custom': 'SCSB-05-102-001', 'start': 'SCSB-05-112-001', 'end': 'SCSB-05-122-001'}[role])
            with error_step({'custom': 'SCSB-05-103-001', 'start': 'SCSB-05-113-001', 'end': 'SCSB-05-123-001'}[role]):
                visible = control.first.is_visible()
            if not visible:
                raise with_error_code(RuntimeError("lookback-period-control-unavailable"), {'custom': 'SCSB-05-104-001', 'start': 'SCSB-05-114-001', 'end': 'SCSB-05-124-001'}[role])
        _log("[twd_inq] native period controls → custom")
        with error_step('SCSB-05-105-001'):
            controls["custom"].first.check()
        _log("[twd_inq] native period controls → start")
        with error_step('SCSB-05-115-001'):
            controls["start"].first.fill(period["start"])
        _log("[twd_inq] native period controls → end")
        with error_step('SCSB-05-125-001'):
            controls["end"].first.fill(period["end"])
        _log("[twd_inq] native period controls → done")

    @staticmethod
    def _six_month_windows(start: str, end: str) -> list[tuple[str, str]]:
        with error_step('SCSB-05-130-001'):
            start_date = date.fromisoformat(start.replace("/", "-"))
        with error_step('SCSB-05-131-001'):
            end_date = date.fromisoformat(end.replace("/", "-"))
        if start_date > end_date:
            raise with_error_code(ValueError('history start is after end'), 'SCSB-05-307-001')

        windows: list[tuple[str, str]] = []
        cursor = start_date
        while cursor <= end_date:
            month_index = cursor.year * 12 + cursor.month - 1 + 6
            year, month_zero = divmod(month_index, 12)
            month = month_zero + 1
            chunk_end = min(
                date(year, month, min(cursor.day, monthrange(year, month)[1]))
                - timedelta(days=1),
                end_date,
            )
            windows.append((
                cursor.isoformat().replace("-", "/"),
                chunk_end.isoformat().replace("-", "/"),
            ))
            cursor = chunk_end + timedelta(days=1)
        return list(reversed(windows))

    @staticmethod
    def _twd_inquiry_period_verification_script() -> str:
        """JS: Vue/React state 更新後再次核對實際送出的日期。"""
        return r"""(expected) => {
            const normalize = (value) => String(value || '').replaceAll('-', '/');
            const custom = document.querySelector('[data-thoth-scsb-twd-period="custom"]');
            const start = document.querySelector('[data-thoth-scsb-twd-period="start"]');
            const end = document.querySelector('[data-thoth-scsb-twd-period="end"]');
            const diagnostic = !custom?.checked ? 'SCSB-05-611-001' :
                normalize(start?.value) !== expected.start ? 'SCSB-05-611-002' :
                normalize(end?.value) !== expected.end ? 'SCSB-05-611-003' : null;
            return diagnostic ? {ok: false, _diagnostic: diagnostic} : {ok: true};
        }"""

    @staticmethod
    def _twd_inquiry_prepare_result_wait_script() -> str:
        """Inventory existing DOM independently of rendered-header admission."""
        return r"""(expected) => {
            const visible = (el) => el && el.offsetParent !== null;
            const normalize = (value) => String(value || '').replace(/[-.]/g, '/');
            const exactToken = (text, value) => {
                const escaped = normalize(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                return new RegExp(`(^|\\D)${escaped}(?=$|\\D)`).test(normalize(text));
            };
            window.__thothScsbTwdResultScope = null;
            window.__thothScsbTwdResultElement = null;
            window.__thothScsbTwdOldScopes = new WeakMap();
            window.__thothScsbTwdOldTables = new WeakMap();
            for (const table of document.querySelectorAll('table')) {
                if (visible(table)) {
                    window.__thothScsbTwdOldTables.set(table, table.innerText || '');
                }
            }
            const emptyPattern = /^\s*很抱歉，\s*您在指定條件內，\s*沒有任何餘額及交易明細的資料。\s*$/;
            window.__thothScsbTwdOldEmpty = new WeakMap();
            for (const empty of document.querySelectorAll('body *')) {
                if (visible(empty) && emptyPattern.test(empty.innerText || '')
                    && ![...empty.children].some(child => emptyPattern.test(child.innerText || ''))) {
                    window.__thothScsbTwdOldEmpty.set(empty, empty.innerText || '');
                }
            }
            // Preserve the legacy scope inventory; this is not header admission.
            const oldTablePattern = /(?:時間|Time)[\s\S]*(?:摘要|Summary)[\s\S]*(?:支出|Expense)[\s\S]*(?:存入|Deposit)[\s\S]*(?:結餘|Balance)/i;
            for (const table of document.querySelectorAll('table')) {
                if (!visible(table) || !oldTablePattern.test(table.innerText || '')) continue;
                for (let scope = table.parentElement; scope && scope !== document.body; scope = scope.parentElement) {
                    if (['MAIN', 'HTML'].includes(scope.tagName)) continue;
                    const criteria = [...scope.querySelectorAll('*')].filter(el => {
                        if (!visible(el) || [...el.children].some(visible)) return false;
                        const own = el.textContent || '';
                        return exactToken(own, expected.account_no)
                            && exactToken(own, expected.start)
                            && exactToken(own, expected.end);
                    });
                    if (criteria.length === 1) {
                        window.__thothScsbTwdOldScopes.set(scope, scope.innerText || '');
                        break;
                    }
                }
            }
            for (const empty of document.querySelectorAll('body *')) {
                if (!visible(empty) || !emptyPattern.test(empty.innerText || '')
                    || [...empty.children].some(child => emptyPattern.test(child.innerText || ''))) continue;
                for (let scope = empty.parentElement; scope && scope !== document.body; scope = scope.parentElement) {
                    if (['MAIN', 'HTML'].includes(scope.tagName)) continue;
                    const criteria = [...scope.querySelectorAll('*')].filter(el => {
                        if (!visible(el) || [...el.children].some(visible)) return false;
                        const own = el.textContent || '';
                        return exactToken(own, expected.account_no)
                            && exactToken(own, expected.start)
                            && exactToken(own, expected.end);
                    });
                    if (criteria.length === 1) {
                        window.__thothScsbTwdOldScopes.set(scope, scope.innerText || '');
                        break;
                    }
                }
            }
        }"""

    @staticmethod
    def _twd_inquiry_result_ready_script() -> str:
        """JS: criteria 與本次新增/改變的結果必須位於同一個非 body 容器。"""
        return r"""(expected) => {
            const visible = (el) => el && el.offsetParent !== null;
            const oldTables = window.__thothScsbTwdOldTables instanceof WeakMap
                ? window.__thothScsbTwdOldTables : new WeakMap();
            const oldEmpty = window.__thothScsbTwdOldEmpty instanceof WeakMap
                ? window.__thothScsbTwdOldEmpty : new WeakMap();
            const oldScopes = window.__thothScsbTwdOldScopes instanceof WeakMap
                ? window.__thothScsbTwdOldScopes : new WeakMap();
            const normalize = (value) => String(value || '').replaceAll('-', '/');
            const exactToken = (text, value) => {
                const escaped = String(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                return new RegExp(`(^|\\D)${escaped}(?=$|\\D)`).test(text);
            };
            const criteriaNodes = (scope) => [...scope.querySelectorAll('*')].filter(el => {
                const own = normalize(el.innerText);
                return visible(el) && /查詢條件|Query Criteria/i.test(own)
                    && exactToken(own, expected.start) && exactToken(own, expected.end)
                    && exactToken(own, expected.account_no)
                    && ![...el.children].some(child => /查詢條件|Query Criteria/i.test(child.innerText || ''));
            });
            const boundScope = (result) => {
                for (let scope = result.parentElement; scope && scope !== document.body; scope = scope.parentElement) {
                    if (['MAIN', 'HTML'].includes(scope.tagName)) continue;
                    if (criteriaNodes(scope).length === 1) {
                        const scopeText = scope.innerText || '';
                        if (oldScopes.has(scope)
                            && oldScopes.get(scope) === scopeText) continue;
                        return scope;
                    }
                }
                return null;
            };
            const tableMatches = __TWD_TABLE_HEADERS__;
            const tables = [...document.querySelectorAll('table')].filter(el =>
                visible(el)
                && tableMatches(el)
                && (!oldTables.has(el)
                    || oldTables.get(el) !== (el.innerText || ''))
                && boundScope(el)
            );
            const emptyPattern = /^\s*很抱歉，\s*您在指定條件內，\s*沒有任何餘額及交易明細的資料。\s*$/;
            const empties = [...document.querySelectorAll('body *')].filter(el =>
                visible(el)
                && emptyPattern.test(el.innerText || '')
                && ![...el.children].some(child => emptyPattern.test(child.innerText || ''))
                && (!oldEmpty.has(el)
                    || oldEmpty.get(el) !== (el.innerText || ''))
            );
            const tableScopes = tables.map(boundScope).filter(Boolean);
            const emptyScopes = empties.map(empty =>
                boundScope(empty)
                || (empty.parentElement !== document.body ? empty.parentElement : empty)
            ).filter(Boolean);
            const scopes = [...tableScopes, ...emptyScopes];
            const elements = [...tables, ...empties];
            const unique = scopes.length === 1 && elements.length === 1;
            window.__thothScsbTwdResultScope = unique ? scopes[0] : null;
            window.__thothScsbTwdResultElement = unique ? elements[0] : null;
            return unique;
        }""".replace("__TWD_TABLE_HEADERS__", _TWD_TABLE_HEADERS_JS)

    @staticmethod
    def _twd_inquiry_extract_result_script() -> str:
        """JS: 只回傳 readiness 已綁定之容器內的 transaction table／空結果。"""
        return r"""(expected) => {
            const visible = (el) => el && el.offsetParent !== null;
            const normalize = (value) => String(value || '').replaceAll('-', '/');
            const exactToken = (text, value) => {
                const escaped = String(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                return new RegExp(`(^|\\D)${escaped}(?=$|\\D)`).test(text);
            };
            const scope = window.__thothScsbTwdResultScope;
            const result = window.__thothScsbTwdResultElement;
            const diagnostic = !visible(scope) ? 'SCSB-05-612-001' : !visible(result) ? 'SCSB-05-612-002' :
                !scope.contains(result) ? 'SCSB-05-612-003' : null;
            if (diagnostic) {
                return {ok: false, empty: false, row_count: 0, text: '', _diagnostic: diagnostic};
            }
            const emptyPattern = /^\s*很抱歉，\s*您在指定條件內，\s*沒有任何餘額及交易明細的資料。\s*$/;
            const empty = emptyPattern.test(result.innerText || '')
                && ![...result.children].some(child => emptyPattern.test(child.innerText || ''));
            if (empty) return {ok: true, empty: true, row_count: 0, text: ''};
            const criteria = [...scope.querySelectorAll('*')].some(el => {
                const own = normalize(el.innerText);
                return visible(el)
                    && /查詢條件|Query Criteria/i.test(own)
                    && exactToken(own, expected.start)
                    && exactToken(own, expected.end)
                    && exactToken(own, expected.account_no)
                    && ![...el.children].some(child => /查詢條件|Query Criteria/i.test(child.innerText || ''));
            });
            if (!criteria) return {ok: false, empty: false, row_count: 0, text: '', _diagnostic: 'SCSB-05-612-004'};
            const tableMatches = __TWD_TABLE_HEADERS__;
            const table = result.tagName === 'TABLE'
                && tableMatches(result) ? result : null;
            if (table) {
                const rowCount = [...table.rows].filter(row => row.querySelectorAll('td').length).length;
                return {ok: true, empty: false, row_count: rowCount, text: table.innerText || ''};
            }
            return {ok: false, empty: false, row_count: 0, text: '', _diagnostic: 'SCSB-05-612-005'};
        }""".replace("__TWD_TABLE_HEADERS__", _TWD_TABLE_HEADERS_JS)

    @staticmethod
    def _twd_empty_result_close_script() -> str:
        """Tag the unique native Close action inside the bound empty-result scope."""
        return r"""() => {
            const visible = el => el && el.offsetParent !== null;
            const scope = window.__thothScsbTwdResultScope;
            if (!visible(scope)) return {ok: false, _diagnostic: 'SCSB-05-613-001'};
            const emptyPattern = /^\s*很抱歉，\s*您在指定條件內，\s*沒有任何餘額及交易明細的資料。\s*$/;
            const hasEmpty = [scope, ...scope.querySelectorAll('*')].some(el =>
                visible(el)
                && emptyPattern.test(el.innerText || '')
                && ![...el.children].some(child => emptyPattern.test(child.innerText || ''))
            );
            if (!hasEmpty) return {ok: false, _diagnostic: 'SCSB-05-613-002'};
            const closes = [...scope.querySelectorAll('button,a,[role=button]')].filter(el =>
                visible(el) && /^\s*(?:關閉|Close)\s*$/i.test(el.innerText || '')
            );
            if (closes.length !== 1) return {ok: false, _diagnostic: 'SCSB-05-613-003'};
            closes[0].dataset.thothScsbTwdEmptyClose = 'true';
            return {ok: true};
        }"""

    @classmethod
    def _close_twd_empty_result(cls, page) -> None:
        with error_step('SCSB-05-308-001'):
            tagged = page.evaluate(cls._twd_empty_result_close_script())
        if not isinstance(tagged, dict):
            raise with_error_code(RuntimeError('empty-result-close-unavailable'), 'SCSB-05-309-001')
        if tagged.get('ok') is not True:
            raise with_error_code(RuntimeError('empty-result-close-unavailable'), next((code for code in ('SCSB-05-613-001', 'SCSB-05-613-002', 'SCSB-05-613-003',) if tagged.get("_diagnostic") == code), 'SCSB-05-309-002'))
        with error_step('SCSB-05-310-001'):
            close = page.locator('[data-thoth-scsb-twd-empty-close="true"]')
        with error_step('SCSB-05-311-001'):
            if close.count() != 1:
                raise with_error_code(RuntimeError('empty-result-close-unavailable'), 'SCSB-05-311-001')
        with error_step('SCSB-05-311-002'):
            if not close.first.is_visible():
                raise with_error_code(RuntimeError('empty-result-close-unavailable'), 'SCSB-05-311-002')
        with error_step('SCSB-05-312-001'):
            close.first.click()
        with error_step('SCSB-05-313-001'):
            page.wait_for_function(
                """() => {
                const close = document.querySelector('[data-thoth-scsb-twd-empty-close="true"]');
                return !close || close.offsetParent === null;
            }""",
                timeout=120000,
            )
        _log("[twd_inq] empty result → closed")

    @staticmethod
    def _is_twd_query_request(request) -> bool:
        """Match the exact SCSB TWD endpoint and its single encrypted payload field."""
        parsed = urlparse(request.url)
        body = request.post_data or ""
        try:
            payload = json.loads(body)
        except (TypeError, json.JSONDecodeError):
            pairs = parse_qsl(body, keep_blank_values=True)
            payload_ok = (
                len(pairs) == 1
                and pairs[0][0] == "Data"
                and bool(pairs[0][1])
            )
        else:
            payload_ok = (
                isinstance(payload, dict)
                and set(payload) == {"Data"}
                and isinstance(payload["Data"], str)
                and bool(payload["Data"])
            )

        return (
            BankCrawler._exact_https_origin_allowed(
                request.url, frozenset({"ebank.scsb.com.tw"}),
            )
            and parsed.path == "/twde/twdeqr01/query"
            and request.method == "POST"
            and request.resource_type in {"xhr", "fetch"}
            and payload_ok
        )

    @staticmethod
    def _is_statement_month_request(request, month: str) -> bool:
        parsed = urlparse(request.url)
        if not (
            BankCrawler._exact_https_origin_allowed(
                request.url, frozenset({"ebank.scsb.com.tw"}),
            )
            and parsed.path in {"/ibap/api/query", "/ibap/ibap/query"}
            and request.method == "POST"
            and request.resource_type in {"xhr", "fetch"}
        ):
            return False
        body = request.post_data or ""
        fields: dict[str, list[str]] = {}
        invalid: set[str] = set()
        aliases = {"month", "statementMonth", "billMonth", "queryMonth"}

        def add(key, value):
            key = str(key)
            if key not in aliases:
                return
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                fields.setdefault(key, []).append(str(value))
            else:
                invalid.add(key)

        try:
            payload = json.loads(body)
        except (TypeError, json.JSONDecodeError):
            for key, value in parse_qsl(body, keep_blank_values=True):
                add(key, value)
        else:
            def walk(value):
                if isinstance(value, dict):
                    for key, child in value.items():
                        if isinstance(child, (dict, list)):
                            if key in aliases:
                                invalid.add(key)
                            walk(child)
                        else:
                            add(key, child)
                elif isinstance(value, list):
                    for child in value:
                        walk(child)
            walk(payload)
        if invalid:
            return False
        expected = month.replace("/", "")
        values = [
            candidate.replace("/", "")
            for alias in aliases for candidate in fields.get(alias, [])
        ]
        return bool(values) and all(value == expected for value in values)

    @staticmethod
    def _twd_inquiry_result_state_script() -> str:
        """JS: 只在本次綁定結果容器內回傳 error/pagination booleans。"""
        return r"""() => {
            const visible = (el) => el && el.offsetParent !== null;
            const scope = window.__thothScsbTwdResultScope;
            if (!visible(scope)) return {error: true, pagination: false, _diagnostic: 'SCSB-05-614-001'};
            const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
            const disabled = (el) => el.closest('[disabled],[aria-disabled="true"],.disabled');
            const current = (el) => el.closest('[aria-current="page"],.active');
            const enabled = (el) => visible(el) && !disabled(el);
            const scopeText = norm(scope.innerText).toLowerCase();
            const visibleAlerts = [...scope.querySelectorAll('.alert,.toast,[role="alert"]')]
                .filter(visible).map(el => norm(el.innerText)).join(' ');
            const errorMarkers = [
                ['system_error', /系統錯誤|system error|unexpected error/i],
                ['busy', /系統忙碌/i],
                ['retry', /請稍後再試/i],
                ['timeout', /連線(?:已)?逾時|timed out|timeout/i],
                ['session', /請重新登入|登入失效|session expired|login required/i],
                ['failed', /(?:操作|查詢).*失敗|failed/i],
            ];
            const matchedError = errorMarkers.find(([, pattern]) =>
                pattern.test(visibleAlerts) || pattern.test(scopeText)
            );
            const error = !!matchedError;
            const nextPattern = /^(下一頁|下頁|Next(?: page)?|載入更多|Load more|[›»→])$/i;
            const nextHint = /(goToNextPage|nextPage|next-page|page-next)/i;
            const directNext = [...scope.querySelectorAll('button,a,[role="button"]')]
                .some(el => enabled(el) && !current(el) && (
                    el.getAttribute('rel') === 'next'
                    || nextPattern.test(norm(el.textContent))
                    || nextPattern.test(norm(el.getAttribute('aria-label')))
                    || nextHint.test([
                        el.id, el.className, el.getAttribute('onclick'),
                        el.getAttribute('data-action'), el.getAttribute('href'),
                    ].filter(Boolean).join(' '))
                ));
            const numbered = [...scope.querySelectorAll(
                '.pagination,[class*="pagination"],nav[aria-label*="pagination" i],[role="navigation"][aria-label*="pagination" i]'
            )].some(container => [...container.querySelectorAll('button,a,[role="button"]')]
                .some(el => enabled(el)
                    && !current(el)
                    && /^\d+$/.test(norm(el.textContent))));
            const result = {error, pagination: directNext || numbered};
            if (matchedError) result.error_code = matchedError[0];
            return result;
        }"""

    @staticmethod
    def _parse_twd_inquiry_records(text: str, *, diagnostics: list[str] | None = None) -> tuple[list[dict], bool]:
        """解析完整交易 DOM；raw preview 的 8 KB 上限不得截斷入庫資料。"""
        diagnostics = [] if diagnostics is None else diagnostics
        diagnostics.clear()
        records = []
        candidate_count = 0

        def _parse_money(value: str) -> str | None:
            raw = value.replace("NT$", "").strip() if value else ""
            if not raw:
                return ""
            if not re.fullmatch(r"-?(?:\d{1,15}|\d{1,3}(?:,\d{3}){1,4})", raw):
                return None
            return raw.replace(",", "")

        for line in text.split("\n"):
            if not re.match(r"^\s*\d{3,4}[-/]\d{2}[-/]\d{2}\b", line):
                continue
            candidate_count += 1
            cols = line.split("\t")
            if len(cols) not in (5, 6):
                if not diagnostics:
                    diagnostics[:] = ['SCSB-05-600-001']
                continue
            date_str = cols[0].strip()
            if not re.match(r"^\d{3,4}/\d{2}/\d{2}$", date_str):
                if not diagnostics:
                    diagnostics[:] = ['SCSB-05-600-002']
                continue
            year, month, day = (int(part) for part in date_str.split("/"))
            canonical_year = year + 1911 if year < 1000 else year
            try:
                date(canonical_year, month, day)
            except ValueError:
                if not diagnostics:
                    diagnostics[:] = ['SCSB-05-600-003']
                continue
            date_str = f"{canonical_year:04d}/{month:02d}/{day:02d}"
            summary = cols[1].strip()
            expense = _parse_money(cols[2].strip())
            deposit = _parse_money(cols[3].strip())
            balance = _parse_money(cols[4].strip())
            rejected = (
                'SCSB-05-600-004' if not summary else
                'SCSB-05-600-005' if balance in (None, "") else
                'SCSB-05-600-006' if expense is None else
                'SCSB-05-600-007' if deposit is None else
                'SCSB-05-600-008' if bool(expense) == bool(deposit) else
                'SCSB-05-600-009' if expense and expense.startswith("-") else
                'SCSB-05-600-010' if deposit and deposit.startswith("-") else
                None
            )
            if rejected:
                if not diagnostics:
                    diagnostics[:] = [rejected]
                continue

            records.append({
                "date": date_str,
                "summary": summary,
                "expense": expense,
                "deposit": deposit,
                "balance": balance,
                "remarks": cols[5].strip() if len(cols) > 5 else "",
            })
        return records, len(records) == candidate_count

    @staticmethod
    def _statement_month_tabs(text: str) -> list[str]:
        """回傳 Data Time 後銀行實際揭露的全部 YYYY/MM tabs。"""
        data_time = text.find("Data Time")
        if data_time < 0:
            return []
        return list(dict.fromkeys(
            re.findall(r"20\d{2}/(?:0[1-9]|1[0-2])(?![/\d])", text[data_time:])
        ))

    @staticmethod
    def _statement_month_summary(text: str) -> dict:
        lower = text.lower()
        if any(marker in lower for marker in (
            "系統錯誤", "系統忙碌", "請稍後再試", "session expired",
            "system error", "unexpected error", "login required",
            "connection timed out", "gateway timeout", "request timeout",
            "連線逾時", "登入逾時", "重新登入", "please log in",
        )):
            raise with_error_code(ValueError('statement month page error'), 'SCSB-06-300-001')
        required_labels = (
            "Current Period Total Amount Due",
            "Current Period Total Minimum Amount Due",
        )
        if not all(label in text for label in required_labels):
            raise with_error_code(ValueError('incomplete statement month summary'), 'SCSB-06-301-001')

        def grab(label: str) -> int | None:
            start = text.find(label)
            if start < 0:
                return None
            tail = text[start + len(label):].lstrip()
            value = tail.splitlines()[0].strip() if tail else ""
            if value in {"", "-", "--", "---"}:
                return None
            match = re.fullmatch(
                r"(?:NT\$\s*)?(\d+|\d{1,3}(?:,\d{3})+)(?:\.(\d{1,2}))?",
                value,
            )
            if not match:
                raise with_error_code(ValueError('invalid statement month amount'), {'Current Period Total Amount Due': 'SCSB-07-440-001', 'Current Period Total Minimum Amount Due': 'SCSB-07-450-001'}[label])
            try:
                amount = Decimal(value.replace("NT$", "").replace(",", "").strip())
            except InvalidOperation as exc:
                raise with_error_code(ValueError('invalid statement month amount'), {'Current Period Total Amount Due': 'SCSB-07-441-001', 'Current Period Total Minimum Amount Due': 'SCSB-07-451-001'}[label]) from exc
            if not amount.is_finite():
                raise with_error_code(ValueError('fractional statement month amount'), {'Current Period Total Amount Due': 'SCSB-07-442-001', 'Current Period Total Minimum Amount Due': 'SCSB-07-452-001'}[label])
            if amount != amount.to_integral_value():
                raise with_error_code(ValueError('fractional statement month amount'), {'Current Period Total Amount Due': 'SCSB-07-442-002', 'Current Period Total Minimum Amount Due': 'SCSB-07-452-002'}[label])
            return int(amount)

        due = grab("Current Period Total Amount Due")
        return {
            "due_amount": due,
            "min_payment": grab("Current Period Total Minimum Amount Due"),
            "has_data": due is not None,
        }

    @staticmethod
    def _card_leaf_scope_text_script() -> str:
        return r"""(leafText) => {
            const visible = (el) => el && el.offsetParent !== null;
            const exactLeaf = [...document.querySelectorAll('body *')].filter(el =>
                visible(el)
                && (el.textContent || '').trim() === leafText
                && ![...el.children].some(child =>
                    (child.textContent || '').trim() === leafText)
            );
            for (const leaf of exactLeaf) {
                for (let scope = leaf.parentElement; scope && scope !== document.body; scope = scope.parentElement) {
                    if (['MAIN', 'HTML'].includes(scope.tagName)) continue;
                    const text = scope.innerText || '';
                    if ((scope.querySelector('table')
                        || /Data Time|Transaction Date|Current Period Total|no .*transaction|no .*statement|查無|目前無新增交易|交易尚未入帳|帳單結帳日|帳單訊息|信用卡前期餘額/i.test(text))
                        && text.includes(leafText)) return text;
                }
            }
            return {_diagnostic: 'scope-unavailable'};
        }"""

    @staticmethod
    def _card_leaf_table_state_script() -> str:
        return r"""(leafText) => {
            const visible = el => el && el.offsetParent !== null;
            const normalize = value => String(value || '')
                .replace(/\s+/g, '').replaceAll('（', '(').replaceAll('）', ')');
            const expected = [
                '交易日交易時間', '交易卡別卡號末4碼', '商店名稱', '交易金額(台幣)',
                '交易類別', '交易國別', '交易結果',
            ];
            const headings = [...document.querySelectorAll('h2.inner-title')]
                .filter(el => visible(el) && (el.innerText || '').trim() === leafText);
            const owner = headings.length === 1
                ? headings[0].closest('div.container.mainContent') : null;
            const visibleTables = [...document.querySelectorAll('table')].filter(visible);
            const tables = visibleTables.filter(table => {
                if (!owner?.contains(table)) return false;
                const headers = [...table.querySelectorAll('th')]
                    .map(cell => normalize(cell.innerText))
                    .filter(Boolean);
                return headers.length === expected.length
                    && expected.every((header, index) => headers[index] === header);
            });
            if (headings.length !== 1 || visibleTables.length !== 1 || tables.length !== 1) return {
                ok: false,
                row_count: null,
                heading_count: headings.length,
                visible_table_count: visibleTables.length,
                matching_table_count: tables.length,
            };
            const rowCount = [...tables[0].querySelectorAll('tr')]
                .filter(row => row.querySelectorAll('td').length).length;
            return {
                ok: true,
                row_count: rowCount,
                heading_count: headings.length,
                visible_table_count: visibleTables.length,
                matching_table_count: tables.length,
            };
        }"""

    @staticmethod
    def _card_statement_state_script() -> str:
        return r"""() => {
            const visible = el => el && el.offsetParent !== null;
            const exact = text => [...document.querySelectorAll('body *')].filter(el =>
                visible(el) && (el.innerText || '').trim() === text
                && ![...el.children].some(child => visible(child)
                    && (child.innerText || '').trim() === text));
            const headings = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6,.title,.card-title')]
                .filter(el => visible(el) && (el.innerText || '').trim() === '帳單查詢及繳款');
            const diagnostic = headings.length !== 1 ? 'SCSB-06-710-001' :
                exact('帳單結帳日').length !== 1 ? 'SCSB-06-710-002' :
                exact('帳單訊息').length !== 1 ? 'SCSB-06-710-003' :
                exact('信用卡前期餘額').length !== 1 ? 'SCSB-06-710-004' : null;
            return diagnostic ? {ok: false, _diagnostic: diagnostic} : {ok: true};
        }"""

    @staticmethod
    def _statement_month_target_script() -> str:
        return """([month, leafText]) => {
            const visible = (el) => el && el.offsetParent !== null;
            const leaves = [...document.querySelectorAll('body *')].filter(el =>
                visible(el) && (el.textContent || '').trim() === leafText
                && ![...el.children].some(child =>
                    (child.textContent || '').trim() === leafText));
            for (const leaf of leaves) {
                for (let scope = leaf.parentElement; scope && scope !== document.body; scope = scope.parentElement) {
                    if (['MAIN', 'HTML'].includes(scope.tagName)) continue;
                    const text = scope.innerText || '';
                    if (!(scope.querySelector('table')
                        || /Current Period Total|帳單結帳日|帳單訊息|信用卡前期餘額|查無.*帳單|no .*statement/i.test(text))) continue;
                    const matches = [...scope.querySelectorAll('button,a,li')].filter(el =>
                        visible(el) && (el.textContent || '').trim() === month);
                    if (matches.length !== 1) return {ok: false, panel: null};
                    const control = matches[0];
                    const rawTarget = control.getAttribute('data-bs-target')
                        || control.getAttribute('data-target')
                        || control.getAttribute('href')
                        || (control.getAttribute('aria-controls')
                            ? `#${CSS.escape(control.getAttribute('aria-controls'))}` : null);
                    const panel = rawTarget && rawTarget.startsWith('#')
                        && document.querySelectorAll(rawTarget).length === 1
                        && scope.contains(document.querySelector(rawTarget))
                        ? rawTarget : null;
                    return {ok: true, panel};
                }
            }
            return {ok: false, panel: null};
        }"""

    def _collect_twd_period(
        self, page, selector: str, selected_index: int, selected_value: str,
        account_no: str, period: dict,
    ) -> list[dict]:
        """Submit and validate one bank-supported (at most six-month) window."""
        self._apply_twd_period_controls(page, period)
        with error_step('SCSB-05-314-001'):
            page.wait_for_timeout(500)
        with error_step('SCSB-05-315-001'):
            period_check = page.evaluate(
                self._twd_inquiry_period_verification_script(), period,
            )
        if not period_check.get("ok"):
            raise with_error_code(RuntimeError('lookback-period-not-applied'), next((code for code in ('SCSB-05-611-001', 'SCSB-05-611-002', 'SCSB-05-611-003',) if period_check.get("_diagnostic") == code), 'SCSB-05-316-001'))

        with error_step('SCSB-05-317-001'):
            page.wait_for_timeout(1500)
        expected = {**period, "account_no": account_no}
        with error_step('SCSB-05-318-001'):
            page.evaluate(self._twd_inquiry_prepare_result_wait_script(), expected)
        with error_step('SCSB-05-319-001'):
            account_select = page.locator(selector)
        with error_step('SCSB-05-320-001'):
            account_count = account_select.count()
        with error_step('SCSB-05-321-001'):
            current_index = (
                account_select.evaluate("el => el.selectedIndex")
                if account_count == 1 else None
            )
        with error_step('SCSB-05-322-001'):
            current_value = account_select.input_value() if account_count == 1 else None
        identity_match = (
            current_index == selected_index and current_value == selected_value
        )
        _log(
            "[twd_inq] account control "
            f"count={account_count} identity_match={identity_match}"
        )
        if account_count != 1:
            raise with_error_code(RuntimeError('account-control-reverted'), 'SCSB-05-323-001')
        if not identity_match:
            raise with_error_code(RuntimeError('account-control-reverted'), 'SCSB-05-323-002')
        with error_step('SCSB-05-324-001'):
            confirm_buttons = page.locator(
                "button:visible, a:visible, [role=button]:visible",
            ).filter(
                has_text=re.compile(r"^\s*(?:Confirm|查詢|確認|Search)\s*$", re.I),
            )
        with error_step('SCSB-05-325-001'):
            if confirm_buttons.count() != 1:
                raise with_error_code(RuntimeError('query-submit-unavailable'), 'SCSB-05-325-001')
        with error_step('SCSB-05-325-002'):
            if not confirm_buttons.first.is_visible():
                raise with_error_code(RuntimeError('query-submit-unavailable'), 'SCSB-05-325-002')
        with error_step('SCSB-05-327-001'):  # noqa: SIM117
            with page.expect_request(
                lambda request: self._is_twd_query_request(request),
                timeout=120000,
            ) as request_info:
                with error_step('SCSB-05-326-001'):
                    confirm_buttons.first.click()
        _log("[twd_inq] click Confirm → native")
        try:
            with error_step('SCSB-05-135-001'):
                query_request = request_info.value
            with error_step('SCSB-05-328-001'):
                query_response = query_request.response()
            _log("[twd_inq] response captured")
            with error_step('SCSB-05-136-001'):
                response_ok = query_response is not None and query_response.ok
            _log(f"[twd_inq] response ok={bool(response_ok)}")
        except Exception as error:
            _log(f"[twd_inq] response boundary failed: {type(error).__name__}")
            raise
        if not response_ok:
            raise with_error_code(RuntimeError('query-submit-unavailable'), 'SCSB-05-329-001')
        _log("[twd_inq] waiting for bound result")
        try:
            with error_step('SCSB-05-330-001'):
                page.wait_for_function(
                    self._twd_inquiry_result_ready_script(),
                    arg=expected,
                    timeout=120000,
                )
        except Exception as error:
            _log(f"[twd_inq] result wait failed: {type(error).__name__}")
            raise
        with error_step('SCSB-05-332-001'):
            if not page.evaluate(self._twd_inquiry_result_ready_script(), expected):
                raise with_error_code(RuntimeError('query-result-stale'), 'SCSB-05-331-001')
        with error_step('SCSB-05-333-001'):
            result_state = page.evaluate(self._twd_inquiry_result_state_script())
        _log(
            "[twd_inq] result state "
            f"error={bool(result_state.get('error'))} "
            f"error_code={result_state.get('error_code') or 'none'} "
            f"pagination={bool(result_state.get('pagination'))}"
        )
        if result_state.get('error'):
            raise with_error_code(RuntimeError('query-result-incomplete'), 'SCSB-05-614-001' if result_state.get('_diagnostic') == 'SCSB-05-614-001' else {'system_error': 'SCSB-05-334-010', 'busy': 'SCSB-05-334-011', 'retry': 'SCSB-05-334-012', 'timeout': 'SCSB-05-334-013', 'session': 'SCSB-05-334-014', 'failed': 'SCSB-05-334-015'}.get(result_state.get('error_code'), 'SCSB-05-334-001'))
        if result_state.get('pagination'):
            raise with_error_code(RuntimeError('query-result-incomplete'), 'SCSB-05-334-002')

        with error_step('SCSB-05-335-001'):
            extracted = page.evaluate(self._twd_inquiry_extract_result_script(), expected)
        if not extracted.get("ok"):
            raise with_error_code(RuntimeError('query-result-unbound'), next((code for code in ('SCSB-05-612-001', 'SCSB-05-612-002', 'SCSB-05-612-003', 'SCSB-05-612-004', 'SCSB-05-612-005',) if extracted.get("_diagnostic") == code), 'SCSB-05-336-001'))
        result_text = extracted.get("text") or ""
        diagnostics: list[str] = []
        records, parse_complete = self._parse_twd_inquiry_records(result_text, diagnostics=diagnostics)
        if not parse_complete:
            raise with_error_code(RuntimeError('partial-transaction-table'), next(iter(diagnostics), 'SCSB-05-337-001'))
        if len(records) != extracted.get('row_count'):
            raise with_error_code(RuntimeError('partial-transaction-table'), 'SCSB-05-337-002')
        if not records and (not extracted.get('empty')):
            raise with_error_code(RuntimeError('partial-transaction-table'), 'SCSB-05-337-003')
        # Query bounds do not override the dates reported by the bank.
        for record in records:
            record["account_no"] = account_no
        return records

    def _collect_twd_account(self, page, selector: str, account_no: str) -> dict:
        """查一個 SCSB 台幣帳戶；長區間切成銀行允許的六個月窗口。"""
        with error_step('SCSB-05-338-001'):
            page.select_option(selector, value=account_no)
        with error_step('SCSB-05-339-001'):
            page.wait_for_timeout(1500)
        with error_step('SCSB-05-340-001'):
            account_select = page.locator(selector)
        with error_step('SCSB-05-342-001'):
            if account_select.count() != 1:
                raise with_error_code(RuntimeError('account-control-unavailable'), 'SCSB-05-341-001')
        with error_step('SCSB-05-343-001'):
            selected_index = account_select.evaluate("el => el.selectedIndex")
        with error_step('SCSB-05-344-001'):
            selected_value = account_select.input_value()
        if not isinstance(selected_index, int):
            raise with_error_code(RuntimeError('account-control-unavailable'), 'SCSB-05-345-001')
        if selected_index < 0:
            raise with_error_code(RuntimeError('account-control-unavailable'), 'SCSB-05-345-002')
        if not isinstance(selected_value, str):
            raise with_error_code(RuntimeError('account-control-unavailable'), 'SCSB-05-345-003')
        if not selected_value:
            raise with_error_code(RuntimeError('account-control-unavailable'), 'SCSB-05-345-004')
        full_history = getattr(self, "full_history", True)
        as_of = date.today()
        cursor_start = None
        if not full_history:
            cursor_start = self.transaction_window_start(
                account_no,
                floor=self._one_year_floor(as_of),
                domain="twd_transactions",
            )
        with error_step('SCSB-05-346-001'):
            period = page.evaluate(
                self._twd_inquiry_period_script(
                    full_history=full_history,
                    end_date=as_of,
                    start_date=cursor_start,
                ),
            )
        _log(f"[twd_inq] period → {period}")
        if not period.get("ok"):
            raise with_error_code(RuntimeError(period.get('error') or 'lookback-period-unavailable'), next((code for code in ('SCSB-05-616-001', 'SCSB-05-616-002', 'SCSB-05-616-003', 'SCSB-05-616-004') if period.get('_diagnostic') == code), {'custom-period-controls-missing': 'SCSB-05-347-002', 'incremental-start-unavailable': 'SCSB-05-347-003', 'invalid-period-limit': 'SCSB-05-347-004'}.get(period.get('error'), 'SCSB-05-347-001')))

        all_records: list[dict] = []
        coverage_windows: list[dict] = []
        query_windows = self._six_month_windows(period["start"], period["end"])
        for index, (start, end) in enumerate(query_windows, start=1):
            window = {**period, "start": start, "end": end}
            _log(f"[twd_inq] window {index} → {start}..{end}")
            records = self._collect_twd_period(
                page, selector, selected_index, selected_value, account_no, window,
            )
            all_records.extend(records)
            coverage_windows.append({
                "identity": account_no,
                "start": start.replace("/", "-"),
                "end": end.replace("/", "-"),
                "status": "complete" if records else "explicit_empty",
                "pages": 1,
            })
            if not records:
                self._close_twd_empty_result(page)

        return {
            "account_no": account_no,
            "period": period,
            "records": all_records,
            "windows": coverage_windows,
        }

    def _collect_twd_inquiry(
        self, page, expected_accounts: set[str],
    ) -> dict:
        """進入 SCSB 台幣交易明細頁，逐一查完所有帳戶。"""
        try:
            with error_step('SCSB-05-348-001'):
                ret = page.evaluate(self._twd_inquiry_nav_script())
            _log(f"[twd_inq] JS sequence ok={bool(ret and ret.get('ok'))}")
            if not isinstance(ret, dict):
                raise with_error_code(RuntimeError('twd-inquiry-navigation-failed'), 'SCSB-05-349-001')
            if ret.get('ok') is not True:
                raise with_error_code(RuntimeError('twd-inquiry-navigation-failed'), next((code for code in ('SCSB-05-610-001', 'SCSB-05-610-002',) if ret.get("_diagnostic") == code), 'SCSB-05-349-002'))
            with error_step('SCSB-05-350-001'):
                page.wait_for_function(r"""expected => {
                if (location.origin !== 'https://ebank.scsb.com.tw'
                    || location.pathname !== '/twde/twde/page'
                    || location.hash !== '#/twde/qr/01/01') {
                    return false;
                }
                const accounts = new Set(
                    [...document.querySelectorAll('select option')]
                        .map(option => (option.value || '').trim())
                        .filter(value => /\d{10,}/.test(value))
                );
                return expected.every(account => accounts.has(account));
            }""", arg=sorted(expected_accounts), timeout=120000)
            _log("[twd_inq] owned route and expected account controls ready")

            with error_step('SCSB-05-351-001'):
                selects = page.evaluate("""() => [...document.querySelectorAll('select')].map(s => ({
                id: s.id, name: s.name,
                options: [...s.options].map(o => ({value: o.value}))
            }))""")
            _log(f"[twd_inq] 表單 selects: {_safe_select_inventory(selects)}")

            targets = []
            seen_accounts = set()
            for select in selects:
                select_id = select.get("id") or ""
                select_name = select.get("name") or ""
                if not select_id and not select_name:
                    continue
                selector = (
                    f"select[id={json.dumps(select_id)}]"
                    if select_id
                    else f"select[name={json.dumps(select_name)}]"
                )
                for option in select.get("options") or []:
                    account_no = (option.get("value") or "").strip()
                    if not re.search(r"\d{10,}", account_no) or account_no in seen_accounts:
                        continue
                    seen_accounts.add(account_no)
                    targets.append((selector, account_no))
            if not targets:
                raise with_error_code(RuntimeError('account-control-unavailable'), 'SCSB-05-352-001')
            if seen_accounts != expected_accounts:
                raise with_error_code(RuntimeError('account-inventory-incomplete'), 'SCSB-05-353-001')

            account_results = []
            all_records = []
            for index, (selector, account_no) in enumerate(targets, start=1):
                _log(f"[twd_inq] query account index={index}/{len(targets)}")
                account_result = self._collect_twd_account(page, selector, account_no)
                records = account_result.pop("records")
                account_result["record_count"] = len(records)
                account_results.append(account_result)
                all_records.extend(records)

            first = account_results[0]
            result = {
                "account_no": first["account_no"],
                "period": first["period"],
                "records": all_records,
                "accounts": account_results,
            }
            _log(f"[twd_inq] accounts={len(account_results)} records={len(all_records)}")
            return result
        except Exception as error:
            _log(f"[twd_inq] ❌ 整段失敗: {type(error).__name__}")
            raise with_error_code(RuntimeError('SCSB TWD inquiry failed'), exception_error_id(error, bank="scsb") or 'SCSB-05-354-001') from None

    def _collect_credit_card_inquiry(self, page) -> dict:
        """SCSB 信用卡明細 — 真實中文三層 accordion 後抓多個 leaf。

          Level 1: 信用卡（已展開）
          Level 2: 信用卡帳務 ▾
          Level 3 leaves (按優先序抓)：
            - 未出帳消費明細 (= pending)
            - 即時消費紀錄 (= current)
            - 帳單查詢及繳款 (= billed)
        """
        from backend.core.persist.scsb import _scsb_page_error, _scsb_parse_card_rows

        result: dict = {"leaves": {}}
        leaves_to_visit = [
            ("未出帳消費明細", "unbilled", "/card/co/03/01"),
            ("即時消費紀錄", "current", "/card/co/08/01"),
            ("帳單查詢及繳款", "statement", "/card/co/02/01"),
        ]

        for leaf_text, key, route in leaves_to_visit:
            stage = "navigate"
            try:
                for menu_label in ("信用卡", "信用卡帳務", leaf_text):
                    with error_step({('unbilled', '信用卡'): 'SCSB-06-500-001', ('unbilled', '信用卡帳務'): 'SCSB-06-520-001', ('unbilled', '未出帳消費明細'): 'SCSB-06-540-001', ('current', '信用卡'): 'SCSB-06-560-001', ('current', '信用卡帳務'): 'SCSB-06-580-001', ('current', '即時消費紀錄'): 'SCSB-06-600-001', ('statement', '信用卡'): 'SCSB-06-620-001', ('statement', '信用卡帳務'): 'SCSB-06-640-001', ('statement', '帳單查詢及繳款'): 'SCSB-06-660-001'}[(key, menu_label)]):
                        control = page.locator("button.accordion-button:visible").filter(
                            has_text=re.compile(rf"^\s*{re.escape(menu_label)}\s*$"),
                        )
                    with error_step({('unbilled', '信用卡'): 'SCSB-06-501-001', ('unbilled', '信用卡帳務'): 'SCSB-06-521-001', ('unbilled', '未出帳消費明細'): 'SCSB-06-541-001', ('current', '信用卡'): 'SCSB-06-561-001', ('current', '信用卡帳務'): 'SCSB-06-581-001', ('current', '即時消費紀錄'): 'SCSB-06-601-001', ('statement', '信用卡'): 'SCSB-06-621-001', ('statement', '信用卡帳務'): 'SCSB-06-641-001', ('statement', '帳單查詢及繳款'): 'SCSB-06-661-001'}[(key, menu_label)]):
                        if control.count() != 1:
                            raise with_error_code(RuntimeError('card-leaf-navigation-failed'), {('unbilled', '信用卡'): 'SCSB-06-501-001', ('unbilled', '信用卡帳務'): 'SCSB-06-521-001', ('unbilled', '未出帳消費明細'): 'SCSB-06-541-001', ('current', '信用卡'): 'SCSB-06-561-001', ('current', '信用卡帳務'): 'SCSB-06-581-001', ('current', '即時消費紀錄'): 'SCSB-06-601-001', ('statement', '信用卡'): 'SCSB-06-621-001', ('statement', '信用卡帳務'): 'SCSB-06-641-001', ('statement', '帳單查詢及繳款'): 'SCSB-06-661-001'}[(key, menu_label)])
                    with error_step({('unbilled', '信用卡'): 'SCSB-06-502-001', ('unbilled', '信用卡帳務'): 'SCSB-06-522-001', ('unbilled', '未出帳消費明細'): 'SCSB-06-542-001', ('current', '信用卡'): 'SCSB-06-562-001', ('current', '信用卡帳務'): 'SCSB-06-582-001', ('current', '即時消費紀錄'): 'SCSB-06-602-001', ('statement', '信用卡'): 'SCSB-06-622-001', ('statement', '信用卡帳務'): 'SCSB-06-642-001', ('statement', '帳單查詢及繳款'): 'SCSB-06-662-001'}[(key, menu_label)]):
                        if not control.first.is_visible():
                            raise with_error_code(RuntimeError('card-leaf-navigation-failed'), {('unbilled', '信用卡'): 'SCSB-06-502-001', ('unbilled', '信用卡帳務'): 'SCSB-06-522-001', ('unbilled', '未出帳消費明細'): 'SCSB-06-542-001', ('current', '信用卡'): 'SCSB-06-562-001', ('current', '信用卡帳務'): 'SCSB-06-582-001', ('current', '即時消費紀錄'): 'SCSB-06-602-001', ('statement', '信用卡'): 'SCSB-06-622-001', ('statement', '信用卡帳務'): 'SCSB-06-642-001', ('statement', '帳單查詢及繳款'): 'SCSB-06-662-001'}[(key, menu_label)])
                    with error_step({('unbilled', '信用卡'): 'SCSB-06-503-001', ('unbilled', '信用卡帳務'): 'SCSB-06-523-001', ('unbilled', '未出帳消費明細'): 'SCSB-06-543-001', ('current', '信用卡'): 'SCSB-06-563-001', ('current', '信用卡帳務'): 'SCSB-06-583-001', ('current', '即時消費紀錄'): 'SCSB-06-603-001', ('statement', '信用卡'): 'SCSB-06-623-001', ('statement', '信用卡帳務'): 'SCSB-06-643-001', ('statement', '帳單查詢及繳款'): 'SCSB-06-663-001'}[(key, menu_label)]):
                        classes = control.first.get_attribute("class") or ""
                    if menu_label == leaf_text or "collapsed" in classes:
                        with error_step({('unbilled', '信用卡'): 'SCSB-06-504-001', ('unbilled', '信用卡帳務'): 'SCSB-06-524-001', ('unbilled', '未出帳消費明細'): 'SCSB-06-544-001', ('current', '信用卡'): 'SCSB-06-564-001', ('current', '信用卡帳務'): 'SCSB-06-584-001', ('current', '即時消費紀錄'): 'SCSB-06-604-001', ('statement', '信用卡'): 'SCSB-06-624-001', ('statement', '信用卡帳務'): 'SCSB-06-644-001', ('statement', '帳單查詢及繳款'): 'SCSB-06-664-001'}[(key, menu_label)]):
                            control.first.click()
                        with error_step({('unbilled', '信用卡'): 'SCSB-06-505-001', ('unbilled', '信用卡帳務'): 'SCSB-06-525-001', ('unbilled', '未出帳消費明細'): 'SCSB-06-545-001', ('current', '信用卡'): 'SCSB-06-565-001', ('current', '信用卡帳務'): 'SCSB-06-585-001', ('current', '即時消費紀錄'): 'SCSB-06-605-001', ('statement', '信用卡'): 'SCSB-06-625-001', ('statement', '信用卡帳務'): 'SCSB-06-645-001', ('statement', '帳單查詢及繳款'): 'SCSB-06-665-001'}[(key, menu_label)]):
                            page.wait_for_timeout(1500)
                stage = "wait"
                with error_step({'unbilled': 'SCSB-06-317-001', 'current': 'SCSB-06-318-001', 'statement': 'SCSB-06-319-001'}[key]):
                    page.wait_for_function("""([route, leafText]) => {
                    if (location.pathname !== '/card/card/page' || location.hash !== `#${route}`) {
                        return false;
                    }
                    const visible = el => el && el.offsetParent !== null;
                    const headings = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6,.title,.card-title')]
                        .filter(el => visible(el) && (el.innerText || '').trim() === leafText);
                    return headings.length === 1;
                }""", arg=[route, leaf_text], timeout=120000)
                _log(f"[card_inq:{key}] navigation ok")
                stage = "settle"
                with error_step({'unbilled': 'SCSB-06-320-001', 'current': 'SCSB-06-321-001', 'statement': 'SCSB-06-322-001'}[key]):
                    page.wait_for_timeout(8000)
                with error_step({'unbilled': 'SCSB-06-323-001', 'current': 'SCSB-06-324-001', 'statement': 'SCSB-06-325-001'}[key]):
                    page.wait_for_timeout(1500)

                stage = "read"
                leaf_result: dict = {"nav": {"ok": True}}
                try:
                    with error_step({'unbilled': 'SCSB-06-326-001', 'current': 'SCSB-06-327-001', 'statement': 'SCSB-06-328-001'}[key]):
                        leaf_result["text"] = page.evaluate(
                            self._card_leaf_scope_text_script(), leaf_text,
                        ) or ""
                except Exception as source_error:
                    raise with_error_code(RuntimeError('card-leaf-read-failed'), exception_error_id(source_error, bank="scsb") or {'unbilled': 'SCSB-06-329-001', 'current': 'SCSB-06-330-001', 'statement': 'SCSB-06-331-001'}[key]) from None
                stage = "identity"
                scope_error_id = None
                if leaf_result['text'] == {'_diagnostic': 'scope-unavailable'}:
                    scope_error_id = {'unbilled': 'SCSB-06-740-001', 'current': 'SCSB-06-741-001', 'statement': 'SCSB-06-742-001'}[key]
                    leaf_result['text'] = ''
                if leaf_text.lower() not in leaf_result["text"].lower():
                    raise with_error_code(RuntimeError('card-leaf-identity-unverified'), scope_error_id or {'unbilled': 'SCSB-06-332-001', 'current': 'SCSB-06-333-001', 'statement': 'SCSB-06-334-001'}[key])

                # 若有 date input 就填表 + Confirm（同 twd_inq）
                stage = "form"
                if key in ("statement", "current"):
                    try:
                        from datetime import datetime, timedelta
                        start_dt = (datetime.now() - timedelta(days=30)).strftime("%Y/%m/%d")
                        with error_step({'unbilled': 'SCSB-06-335-001', 'current': 'SCSB-06-336-001', 'statement': 'SCSB-06-337-001'}[key]):
                            date_inputs = page.evaluate("""(leafText) => {
                            const visible = (el) => el && el.offsetParent !== null;
                            const leaves = [...document.querySelectorAll('body *')].filter(el =>
                                visible(el) && (el.textContent || '').trim() === leafText
                                && ![...el.children].some(child =>
                                    (child.textContent || '').trim() === leafText));
                            for (const leaf of leaves) {
                                for (let scope = leaf.parentElement; scope && scope !== document.body; scope = scope.parentElement) {
                                    if (['MAIN', 'HTML'].includes(scope.tagName)) continue;
                                    const text = scope.innerText || '';
                                    if (!(scope.querySelector('table')
                                        || /Data Time|Transaction Date|Current Period Total|no .*transaction|no .*statement|查無|目前無新增交易|交易尚未入帳|帳單結帳日|帳單訊息|信用卡前期餘額/i.test(text))) continue;
                                    return [...scope.querySelectorAll('input')]
                                        .filter(input => visible(input)
                                            && /date/i.test((input.id||'')+(input.name||'')+(input.placeholder||'')+(input.type||''))
                                            && input.id
                                            && document.querySelectorAll(`#${CSS.escape(input.id)}`).length === 1)
                                        .map(input => ({id: input.id, value: input.value}));
                                }
                            }
                            return [];
                        }""", leaf_text)
                        pending_date_inputs = [
                            input_data for input_data in date_inputs
                            if not input_data.get("value") and input_data.get("id")
                        ]
                        filled_date = not pending_date_inputs
                        date_error_id = None
                        for di in pending_date_inputs:
                            sel = f"#{di['id']}"
                            try:
                                with error_step({'unbilled': 'SCSB-06-338-001', 'current': 'SCSB-06-339-001', 'statement': 'SCSB-06-340-001'}[key]):
                                    page.click(sel)
                                with error_step({'unbilled': 'SCSB-06-341-001', 'current': 'SCSB-06-342-001', 'statement': 'SCSB-06-343-001'}[key]):
                                    page.wait_for_timeout(200)
                                with error_step({'unbilled': 'SCSB-06-344-001', 'current': 'SCSB-06-345-001', 'statement': 'SCSB-06-346-001'}[key]):
                                    page.keyboard.press("Control+A")
                                with error_step({'unbilled': 'SCSB-06-347-001', 'current': 'SCSB-06-348-001', 'statement': 'SCSB-06-349-001'}[key]):
                                    page.keyboard.press("Delete")
                                with error_step({'unbilled': 'SCSB-06-350-001', 'current': 'SCSB-06-351-001', 'statement': 'SCSB-06-352-001'}[key]):
                                    page.keyboard.type(start_dt, delay=50)
                                with error_step({'unbilled': 'SCSB-06-353-001', 'current': 'SCSB-06-354-001', 'statement': 'SCSB-06-355-001'}[key]):
                                    page.evaluate(
                                        "(sel) => { const el=document.querySelector(sel); if(el){"
                                        "  el.dispatchEvent(new Event('input', {bubbles:true}));"
                                        "  el.dispatchEvent(new Event('change', {bubbles:true}));"
                                        "  el.blur(); } }",
                                        sel,
                                    )
                                with error_step({'unbilled': 'SCSB-06-356-001', 'current': 'SCSB-06-357-001', 'statement': 'SCSB-06-358-001'}[key]):
                                    page.wait_for_timeout(300)
                                filled_date = True
                                date_error_id = None
                                break
                            except Exception as date_error:
                                date_error_id = exception_error_id(date_error, bank='scsb')
                                continue
                        if not filled_date:
                            raise with_error_code(RuntimeError('card-query-date-unavailable'), date_error_id or {'unbilled': 'SCSB-06-359-001', 'current': 'SCSB-06-360-001', 'statement': 'SCSB-06-361-001'}[key])
                        if date_inputs:
                            before_confirm = leaf_result["text"]
                            with error_step({'unbilled': 'SCSB-06-362-001', 'current': 'SCSB-06-363-001', 'statement': 'SCSB-06-364-001'}[key]):
                                clicked = page.evaluate("""(leafText) => {
                                const visible = (el) => el && el.offsetParent !== null;
                                const leaves = [...document.querySelectorAll('body *')].filter(el =>
                                    visible(el) && (el.textContent || '').trim() === leafText
                                    && ![...el.children].some(child =>
                                        (child.textContent || '').trim() === leafText));
                                for (const leaf of leaves) {
                                    for (let scope = leaf.parentElement; scope && scope !== document.body; scope = scope.parentElement) {
                                        if (['MAIN', 'HTML'].includes(scope.tagName)) continue;
                                        const text = scope.innerText || '';
                                        if (!(scope.querySelector('table')
                                            || /Data Time|Transaction Date|Current Period Total|no .*transaction|no .*statement|查無|目前無新增交易|交易尚未入帳|帳單結帳日|帳單訊息|信用卡前期餘額/i.test(text))) continue;
                                        const matches = [...scope.querySelectorAll('button,a')].filter(el =>
                                            visible(el) && /^(Confirm|查詢|確認|Search)$/i.test((el.innerText || '').trim()));
                                        if (matches.length !== 1) return {_diagnostic: 1};
                                        matches[0].click();
                                        return true;
                                    }
                                }
                                return {_diagnostic: 2};
                            }""", leaf_text)
                            confirm_error_id = None
                            if isinstance(clicked, dict) and type(clicked.get('_diagnostic')) is int and clicked['_diagnostic'] in (1, 2):
                                confirm_error_id = {'current': {1: 'SCSB-06-750-001', 2: 'SCSB-06-750-002'}, 'statement': {1: 'SCSB-06-751-001', 2: 'SCSB-06-751-002'}}[key][clicked['_diagnostic']]
                                clicked = None
                            if not clicked:
                                raise with_error_code(RuntimeError('card-query-submit-unavailable'), confirm_error_id or {'unbilled': 'SCSB-06-365-001', 'current': 'SCSB-06-366-001', 'statement': 'SCSB-06-367-001'}[key])
                            _log(f"[card_inq:{key}] Confirm clicked")
                            with error_step({'unbilled': 'SCSB-06-368-001', 'current': 'SCSB-06-369-001', 'statement': 'SCSB-06-370-001'}[key]):
                                page.wait_for_function(r"""([before, scopeName, leafText]) => {
                                const visible = (el) => el && el.offsetParent !== null;
                                const exactLeaf = [...document.querySelectorAll('body *')].filter(el =>
                                    visible(el) && (el.textContent || '').trim() === leafText
                                    && ![...el.children].some(child =>
                                        (child.textContent || '').trim() === leafText));
                                let text = '';
                                for (const leaf of exactLeaf) {
                                    for (let scope = leaf.parentElement; scope && scope !== document.body; scope = scope.parentElement) {
                                        if (['MAIN', 'HTML'].includes(scope.tagName)) continue;
                                        const candidate = scope.innerText || '';
                                        if ((scope.querySelector('table')
                                            || /Data Time|Transaction Date|Current Period Total|no .*transaction|no .*statement|查無|目前無新增交易|交易尚未入帳|帳單結帳日|帳單訊息|信用卡前期餘額/i.test(candidate))
                                            && candidate.includes(leafText)) {
                                            text = candidate;
                                            break;
                                        }
                                    }
                                    if (text) break;
                                }
                                const ready = scopeName === 'current'
                                    ? /即時消費紀錄[\s\S]*(?:Transaction Date|交易日期|no (?:real-time )?transaction|查無)/i.test(text)
                                    : /帳單查詢及繳款[\s\S]*(?:(?:Data Time|資料時間)[\s\S]*20\d{2}\/(?:0[1-9]|1[0-2])|(?:查無.*帳單|no .*statement))/i.test(text);
                                return text !== before && ready;
                            }""", arg=[before_confirm, key, leaf_text], timeout=120000)
                            with error_step({'unbilled': 'SCSB-06-371-001', 'current': 'SCSB-06-372-001', 'statement': 'SCSB-06-373-001'}[key]):
                                leaf_result["text_final"] = page.evaluate(
                                    self._card_leaf_scope_text_script(), leaf_text,
                                ) or ""
                            if leaf_result.get('text_final') == {'_diagnostic': 'scope-unavailable'}:
                                leaf_result['text_final'] = ''
                    except Exception as source_error:
                        raise with_error_code(RuntimeError('card-query-form-failed'), exception_error_id(source_error, bank="scsb") or {'unbilled': 'SCSB-06-374-001', 'current': 'SCSB-06-375-001', 'statement': 'SCSB-06-376-001'}[key]) from None

                # 2026-06-13 升級：Statement Inquiry 加月份 tab 迭代抓帳單
                # SCSB 顯示「2026/05 / 2026/04 / 2026/03」3 個月份 tab，點切換每月帳單
                stage = "statement"
                if key == "statement":
                    month_scope_error_id = None
                    try:
                        leaf_text_now = leaf_result.get("text", "")
                        statement_text = leaf_result.get("text_final") or leaf_text_now
                        month_tabs = self._statement_month_tabs(statement_text)
                        _log(f"[card_inq:{key}] month_tab_count={len(month_tabs)}")
                        with error_step({'unbilled': 'SCSB-06-377-001', 'current': 'SCSB-06-378-001', 'statement': 'SCSB-06-379-001'}[key]):
                            statement_state = page.evaluate(self._card_statement_state_script())
                        statement_summary = (
                            isinstance(statement_state, dict)
                            and statement_state.get("ok") is True
                        )
                        if not month_tabs and not statement_summary and not re.search(
                            r"查無.*帳單|no .*statement", statement_text, re.I,
                        ):
                            raise with_error_code(RuntimeError('statement-month-tabs-unavailable'), next((code for code in ('SCSB-06-710-001', 'SCSB-06-710-002', 'SCSB-06-710-003', 'SCSB-06-710-004',) if statement_state.get("_diagnostic") == code), 'SCSB-06-382-001') if isinstance(statement_state, dict) else {'unbilled': 'SCSB-06-380-001', 'current': 'SCSB-06-381-001', 'statement': 'SCSB-06-382-001'}[key])
                        _log(f"[card_inq:{key}] 月份 tabs: {month_tabs}")

                        # 每個月份點 tab + dump
                        with error_step({'unbilled': 'SCSB-06-383-001', 'current': 'SCSB-06-384-001', 'statement': 'SCSB-06-385-001'}[key]):
                            month_targets = {
                                month: page.evaluate(
                                    self._statement_month_target_script(), [month, leaf_text],
                                )
                                for month in month_tabs
                            }
                        if any(
                            not isinstance(target, dict) or target.get("ok") is not True
                            for target in month_targets.values()
                        ):
                            raise with_error_code(RuntimeError('statement-month-tab-unavailable'), {'unbilled': 'SCSB-06-386-001', 'current': 'SCSB-06-387-001', 'statement': 'SCSB-06-388-001'}[key])
                        panel_selectors = [
                            target.get("panel") for target in month_targets.values()
                            if target.get("panel")
                        ]
                        months_data = []
                        for mo in month_tabs:
                            month_scope_error_id = None
                            target = month_targets[mo]

                            month_error_id = None
                            def click_month():
                                nonlocal month_error_id
                                month_error_id = None
                                with error_step({'unbilled': 'SCSB-06-389-001', 'current': 'SCSB-06-390-001', 'statement': 'SCSB-06-391-001'}[key]):
                                    clicked = page.evaluate("""([month, leafText]) => {
                                    const visible = (el) => el && el.offsetParent !== null;
                                    const leaves = [...document.querySelectorAll('body *')].filter(el =>
                                        visible(el) && (el.textContent || '').trim() === leafText
                                        && ![...el.children].some(child =>
                                            (child.textContent || '').trim() === leafText));
                                    for (const leaf of leaves) {
                                        for (let scope = leaf.parentElement; scope && scope !== document.body; scope = scope.parentElement) {
                                            if (['MAIN', 'HTML'].includes(scope.tagName)) continue;
                                            const matches = [...scope.querySelectorAll('button,a,li')].filter(el =>
                                                visible(el) && (el.textContent || '').trim() === month);
                                            if (matches.length !== 1) return {_diagnostic: 1};
                                            matches[0].click();
                                            return true;
                                        }
                                    }
                                    return {_diagnostic: 2};
                                }""", [mo, leaf_text])
                                if isinstance(clicked, dict) and type(clicked.get('_diagnostic')) is int and clicked['_diagnostic'] in (1, 2):
                                    month_error_id = ({1: 'SCSB-06-730-001', 2: 'SCSB-06-730-002'} if panel_selector else {1: 'SCSB-06-731-001', 2: 'SCSB-06-731-002'}).get(clicked['_diagnostic'])
                                    return False
                                return clicked

                            panel_selector = target.get("panel")
                            with error_step({'unbilled': 'SCSB-06-392-001', 'current': 'SCSB-06-393-001', 'statement': 'SCSB-06-394-001'}[key]):
                                if panel_selectors.count(panel_selector) != 1:
                                    panel_selector = None
                            if panel_selector:
                                if not click_month():
                                    raise with_error_code(RuntimeError('statement-month-tab-unavailable'), month_error_id or {'unbilled': 'SCSB-06-395-001', 'current': 'SCSB-06-396-001', 'statement': 'SCSB-06-397-001'}[key])
                            else:
                                with error_step({'unbilled': 'SCSB-06-401-001', 'current': 'SCSB-06-402-001', 'statement': 'SCSB-06-403-001'}[key]):  # noqa: SIM117
                                    with page.expect_request(
                                        lambda request, month=mo: self._is_statement_month_request(
                                            request, month,
                                        ),
                                        timeout=120000,
                                    ) as month_request_info:
                                        if not click_month():
                                            raise with_error_code(RuntimeError('statement-month-tab-unavailable'), month_error_id or {'unbilled': 'SCSB-06-398-001', 'current': 'SCSB-06-399-001', 'statement': 'SCSB-06-400-001'}[key])
                                with error_step({'unbilled': 'SCSB-06-404-001', 'current': 'SCSB-06-405-001', 'statement': 'SCSB-06-406-001'}[key]):
                                    month_response = month_request_info.value.response()
                                if month_response is None:
                                    raise with_error_code(RuntimeError('statement-month-query-failed'), {'unbilled': 'SCSB-06-407-001', 'current': 'SCSB-06-408-001', 'statement': 'SCSB-06-409-001'}[key])
                                if month_response.ok is not True:
                                    raise with_error_code(RuntimeError('statement-month-query-failed'), {'unbilled': 'SCSB-06-407-002', 'current': 'SCSB-06-408-002', 'statement': 'SCSB-06-409-002'}[key])

                            with error_step({'unbilled': 'SCSB-06-410-001', 'current': 'SCSB-06-411-001', 'statement': 'SCSB-06-412-001'}[key]):
                                page.wait_for_function("""([month, leafText]) => {
                                const visible = (el) => el && el.offsetParent !== null;
                                const leaves = [...document.querySelectorAll('body *')].filter(el =>
                                    visible(el) && (el.textContent || '').trim() === leafText
                                    && ![...el.children].some(child =>
                                        (child.textContent || '').trim() === leafText));
                                for (const leaf of leaves) {
                                    for (let scope = leaf.parentElement; scope && scope !== document.body; scope = scope.parentElement) {
                                        if (['MAIN', 'HTML'].includes(scope.tagName)) continue;
                                        const active = [...scope.querySelectorAll('button,a,li')].filter(el =>
                                            visible(el) && (el.textContent || '').trim() === month
                                            && (el.getAttribute('aria-selected') === 'true'
                                                || el.getAttribute('aria-current') === 'page'
                                                || el.classList.contains('active')));
                                        if (active.length === 1) return true;
                                    }
                                }
                                return false;
                            }""", arg=[mo, leaf_text], timeout=120000)

                            if panel_selector:
                                with error_step({'unbilled': 'SCSB-06-413-001', 'current': 'SCSB-06-414-001', 'statement': 'SCSB-06-415-001'}[key]):
                                    page.wait_for_function("""(selector) => {
                                    const panel = document.querySelector(selector);
                                    return panel && panel.offsetParent !== null;
                                }""", arg=panel_selector, timeout=120000)
                                with error_step({'unbilled': 'SCSB-06-416-001', 'current': 'SCSB-06-417-001', 'statement': 'SCSB-06-418-001'}[key]):
                                    mo_text = page.evaluate(
                                        "(selector) => document.querySelector(selector)?.innerText || ''",
                                        panel_selector,
                                    )
                            else:
                                with error_step({'unbilled': 'SCSB-06-419-001', 'current': 'SCSB-06-420-001', 'statement': 'SCSB-06-421-001'}[key]):
                                    mo_text = page.evaluate(
                                        self._card_leaf_scope_text_script(), leaf_text,
                                    ) or ""
                                if mo_text == {'_diagnostic': 'scope-unavailable'}:
                                    month_scope_error_id = 'SCSB-06-743-001'
                                    mo_text = ''
                            months_data.append({
                                "month": mo,
                                **self._statement_month_summary(mo_text),
                            })
                            _log(f"[card_inq:{key}] {mo} tab click OK, text len={len(mo_text)}")
                        if month_tabs:
                            leaf_result["months"] = months_data
                    except Exception as source_error:
                        raise with_error_code(RuntimeError('statement-month-collection-failed'), month_scope_error_id or exception_error_id(source_error, bank="scsb") or {'unbilled': 'SCSB-06-422-001', 'current': 'SCSB-06-423-001', 'statement': 'SCSB-06-424-001'}[key]) from None

                stage = "parse"
                source_text = leaf_result.get("text_final") or leaf_result.get("text") or ""
                if _scsb_page_error(source_text):
                    raise with_error_code(RuntimeError('card-leaf-page-error'), {'unbilled': 'SCSB-06-425-001', 'current': 'SCSB-06-426-001', 'statement': 'SCSB-06-427-001'}[key])
                if key in ("unbilled", "current"):
                    rows, complete = _scsb_parse_card_rows(source_text, scope=key)
                    with error_step({'unbilled': 'SCSB-06-428-001', 'current': 'SCSB-06-429-001', 'statement': 'SCSB-06-430-001'}[key]):
                        table_state = (
                            page.evaluate(self._card_leaf_table_state_script(), leaf_text)
                            if key == "current" else None
                        )
                    structural_empty = (
                        isinstance(table_state, dict)
                        and table_state.get("ok") is True
                        and table_state.get("row_count") == 0
                    )
                    rows_bound = (
                        key != "current"
                        or (
                            isinstance(table_state, dict)
                            and table_state.get("ok") is True
                            and table_state.get("row_count") == len(rows)
                        )
                    )
                    table_reason = (
                        next((code for key, code in (('heading_count', 'SCSB-06-720-001'), ('visible_table_count', 'SCSB-06-720-002'), ('matching_table_count', 'SCSB-06-720-003')) if key in table_state and table_state.get(key) != 1), 'SCSB-06-720-004' if table_state.get('ok') is True and table_state.get('row_count') != len(rows) else None)
                        if key == "current" and isinstance(table_state, dict) else None
                    )
                    if key == "current" and isinstance(table_state, dict):
                        _log(
                            "[card_inq:current] table "
                            f"headings={table_state.get('heading_count')} "
                            f"visible={table_state.get('visible_table_count')} "
                            f"matching={table_state.get('matching_table_count')} "
                            f"rows={table_state.get('row_count')}"
                        )
                    lower = source_text.lower()
                    explicit_empty = (
                        (key == "unbilled" and (
                            "no new transactions" in lower
                            or "have not yet been recorded" in lower
                            or "目前無新增交易" in source_text
                            or "交易尚未入帳" in source_text
                        ))
                        or (key == "current" and (
                            "no real-time transaction" in lower
                            or "no transaction records" in lower
                        ))
                    )
                    empty_authoritative = (
                        structural_empty or (key != "current" and explicit_empty)
                    )
                    current_table_has_rows = (
                        key == "current"
                        and isinstance(table_state, dict)
                        and isinstance(table_state.get("row_count"), int)
                        and table_state["row_count"] > 0
                    )
                    if current_table_has_rows:
                        if not rows:
                            raise with_error_code(RuntimeError('card-transaction-scope-incomplete'), 'SCSB-06-432-001')
                        if not complete:
                            raise with_error_code(RuntimeError('card-transaction-scope-incomplete'), 'SCSB-06-432-002')
                        if not rows_bound:
                            raise with_error_code(RuntimeError('card-transaction-scope-incomplete'), table_reason or 'SCSB-06-432-003')
                    if rows:
                        if not complete:
                            raise with_error_code(RuntimeError('card-transaction-scope-incomplete'), {'unbilled': 'SCSB-06-434-001', 'current': 'SCSB-06-435-001', 'statement': 'SCSB-06-436-001'}[key])
                        if not rows_bound:
                            raise with_error_code(RuntimeError('card-transaction-scope-incomplete'), table_reason or {'unbilled': 'SCSB-06-434-002', 'current': 'SCSB-06-435-002', 'statement': 'SCSB-06-436-002'}[key])
                        leaf_result["rows"] = rows
                    elif empty_authoritative:
                        leaf_result["empty"] = True
                    else:
                        raise with_error_code(RuntimeError('card-transaction-scope-incomplete'), {'unbilled': 'SCSB-06-437-001', 'current': 'SCSB-06-438-001', 'statement': 'SCSB-06-439-001'}[key])
                leaf_result.pop("text", None)
                leaf_result.pop("text_final", None)
                result["leaves"][key] = leaf_result
            except Exception as source_error:
                _log(f"[card_inq:{key}] failed stage={stage}")
                raise with_error_code(RuntimeError('SCSB credit-card inquiry failed'), exception_error_id(source_error, bank="scsb") or {'unbilled': 'SCSB-06-440-001', 'current': 'SCSB-06-441-001', 'statement': 'SCSB-06-442-001'}[key]) from None

        return result


if __name__ == "__main__":
    crawler = ScsbCrawler()
    try:
        result = crawler.run(login_url=BASE, headless=True)
    except ScsbLoginError as e:
        result = {"error": "login_failed_stop", "detail": str(e)}

    if result.get("error"):
        _log(f"  ❌ error: {result['error']}")
    else:
        data = result.get("data")
        if not isinstance(data, dict):
            data = {}
        _log("\n===== 抓取摘要 =====")
        _log(f"  抓到帳號: {len(data.get('accounts', []))} 個")
        _log(f"  台幣交易: {len((data.get('twd_inquiry') or {}).get('records', []))} 筆")
