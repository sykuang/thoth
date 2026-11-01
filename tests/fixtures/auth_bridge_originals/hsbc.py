#!/usr/bin/env python3
"""HSBC Taiwan credit-card crawler with a two-stage SPA login."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.base import (
    captcha_unreadable,
    ApiHit,
    BankCollectResult,
    BankCrawler,
    ResponseCollector,
    _strict_json_loads,
    validate_history_coverage,
)
from backend.core.card_bills import (
    card_bill_date, card_bill_money, make_card_bill_fact, publish_card_bill_facts,
)
from backend.core.creds import HsbcCreds
from backend.core.captcha import ocr_bytes
from backend.core.error_codes import error_step, exception_error_id, with_error_code
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointPhase,
    LoginCheckpointRule,
)

BASE = "https://card.hsbc.com.tw/#/login"

SEL_USERID = "#userId"
SEL_PWD = "#password"
SEL_CAPTCHA = "#captchaInput"
# HSBC 是 5 碼英數 CAPTCHA。2026-07-05 Azure log 證實只檢查
# expected_len/alnum_only 會放行形式合法但內容錯的 OCR false positive，銀行回
#「驗證碼錯誤，請重新輸入。」；送出後不可自動重試，故送出前必須加信心門檻。
HSBC_CAPTCHA_MIN_CONFIDENCE = 0.85

def _log(*a):
    print(*a, file=sys.stderr)


def _is_json_content_type(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.split(";", 1)[0].strip().lower() == "application/json"
    )


def _hsbc_twd_integer(value: object) -> int | None:
    match = re.fullmatch(
        r"([+]?(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d+)?) TWD",
        value if isinstance(value, str) else "",
    )
    try:
        amount = Decimal(match.group(1).replace(",", "")) if match else None
    except InvalidOperation:
        amount = None
    if (
        amount is None
        or not amount.is_finite()
        or amount != amount.to_integral_value()
        or not 0 <= amount <= Decimal("100000000")
    ):
        return None
    return int(amount)


class HsbcLoginError(RuntimeError):
    """HSBC login 送出後失敗——立刻中止，絕不自動重打（防鎖帳號）。"""


def _hsbc_card_bill_facts(out: dict):
    facts = []
    details_by_tail = out.get("card_detail") or {}
    expected_cards = [
        card for card in out.get("cards") or []
        if isinstance(card, dict) and card.get("maskedCardNumber")
    ]
    for card in expected_cards:
        identity = str(card["maskedCardNumber"])
        tail = identity[-4:]
        entry = None
        if isinstance(details_by_tail, dict):
            entry = details_by_tail.get(identity, details_by_tail.get(tail))
        if not isinstance(entry, dict):
            facts.append(None)
            continue
        details = ((entry.get("detail") or {}).get("details") or [])
        detail_keys = [
            str(row.get("key") or "").strip()
            for row in details if isinstance(row, dict) and row.get("key")
        ]
        if len(detail_keys) != len(set(detail_keys)):
            facts.append(None)
            continue
        kv = {
            str(row.get("key") or "").strip(): str(row.get("value") or "").strip()
            for row in details if isinstance(row, dict) and row.get("key")
        }
        statement_amount = card_bill_money(_hsbc_twd_integer(kv.get("Last Statement Amount")))
        payment_amount = card_bill_money(_hsbc_twd_integer(kv.get("Last Payment Amount")))
        statement_date = card_bill_date(kv.get("Last Statement Date"))
        payment_date = card_bill_date(kv.get("Last Payment Date"))
        remaining = statement_amount
        if (remaining is not None and payment_amount is not None and statement_date
                and payment_date and payment_date >= statement_date):
            remaining = max(remaining - payment_amount, 0)
        # The bank's own balance already nets cashback/statement credits that the
        # payment subtraction above cannot see: billed remainder = balance - unbilled.
        outstanding = card_bill_money(card.get("outstandingBalance"))
        unbilled = card_bill_money(_hsbc_twd_integer(kv.get("Unbilled Transactions")))
        if remaining is not None and outstanding is not None and unbilled is not None:
            remaining = min(remaining, max(outstanding - unbilled, 0))
        facts.append(make_card_bill_fact(
            scope="card",
            card_no=entry.get("masked") or card.get("maskedCardNumber"),
            remaining_due=remaining,
            statement_close_date=statement_date or card.get("statementDate"),
            payment_due_date=card.get("paymentDueDate"),
            last_payment_amount=payment_amount,
            last_payment_date=payment_date,
        ))
    return facts


class HsbcCrawler(BankCrawler):
    USES_SHARED_LOGIN_CHECKPOINTS: ClassVar[bool] = True
    SAFE_COLLECT_GUARDS = frozenset({
        "hsbc-card-inventory-byte-budget",
        "hsbc-card-inventory-count",
        "hsbc-card-inventory-envelope",
        "hsbc-card-inventory-identity",
        "hsbc-card-inventory-missing",
        "hsbc-card-inventory-replay",
        "hsbc-card-inventory-row",
        "hsbc-history-byte-budget",
        "hsbc-history-cursor",
        "hsbc-history-mode",
        "hsbc-history-token",
        "hsbc-posted-history",
    })
    HISTORY_COVERAGE_REQUIRED: ClassVar[bool] = True
    HISTORY_COVERAGE_DOMAINS: ClassVar[frozenset[str]] = frozenset({
        "card_billed_transactions",
    })
    CREDENTIAL_HOSTS = frozenset({"card.hsbc.com.tw"})

    def __init__(self):
        super().__init__(name="hsbc")
        self.creds = HsbcCreds.load()

    def _host_filter(self) -> str:
        return "card.hsbc.com.tw"

    @staticmethod
    def _history_floor(end: date) -> date:
        try:
            return end.replace(year=end.year - 1) + timedelta(days=1)
        except ValueError:
            return end.replace(year=end.year - 1, day=28) + timedelta(days=1)

    def _card_history_range(self, identity: str, *, end: date) -> tuple[date, date]:
        cursor = self.transaction_cursors.get("card_billed_transactions", {}).get(identity)
        if isinstance(cursor, date) and cursor > end:
            raise with_error_code(RuntimeError('hsbc-history-cursor'), 'HSBC-06-300-001')
        return (
            self.transaction_window_start(
                identity,
                floor=self._history_floor(end),
                domain="card_billed_transactions",
            ),
            end,
        )


    # ---------- 登入 ----------
    def login(self, page) -> bool:
        return self._shared_login(page)

    def prepare_login_page(self, page) -> None:
        with error_step('HSBC-01-300-001'):
            page.wait_for_timeout(7000)
        self._logged_in(page)

    def is_authenticated(self, page) -> bool:
        return self._logged_in(page)

    def _logged_in(self, page) -> bool:
        try:
            current = urlparse(page.url or "")
            if (
                current.scheme.lower() != "https"
                or (current.hostname or "").lower() != "card.hsbc.com.tw"
                or current.port not in (None, 443)
                or current.username is not None
                or current.password is not None
                or (current.fragment or "").lower().startswith("/login")
            ):
                return False
            for selector in (SEL_USERID, SEL_PWD, SEL_CAPTCHA):
                with error_step({'#userId': 'HSBC-03-500-001', '#password': 'HSBC-03-510-001', '#captchaInput': 'HSBC-03-520-001'}[selector]):
                    controls = page.locator(selector)
                with error_step('HSBC-03-301-001'):
                    if any(
                        error_step({'#userId': 'HSBC-03-500-004', '#password': 'HSBC-03-510-004', '#captchaInput': 'HSBC-03-520-004'}[selector])(lambda: error_step({'#userId': 'HSBC-03-500-003', '#password': 'HSBC-03-510-003', '#captchaInput': 'HSBC-03-520-003'}[selector])(lambda: controls.nth(index))().is_visible())()
                        for index in range(error_step({'#userId': 'HSBC-03-500-002', '#password': 'HSBC-03-510-002', '#captchaInput': 'HSBC-03-520-002'}[selector])(lambda: controls.count())())
                    ):
                        return False
            with error_step('HSBC-03-302-001'):
                body = error_step('HSBC-03-540-002')(lambda: error_step('HSBC-03-540-001')(lambda: page.locator("body"))().inner_text())()
        except Exception:
            return False
        return (
            len(body) >= 300
            and (
                "登出" in body
                or re.search(r"(?<![A-Za-z])Logout(?![A-Za-z])", body, re.IGNORECASE)
                is not None
            )
            and (
                "我的卡片" in body
                or "卡片清單" in body
                or "信用卡" in body
                or re.search(r"(?<![A-Za-z])My Cards(?![A-Za-z])", body, re.IGNORECASE)
                is not None
            )
        )

    def login_checkpoint_rules(self) -> tuple[LoginCheckpointRule, ...]:
        all_phases = tuple(CheckpointPhase)
        post_settle = (
            CheckpointPhase.POST_SUBMIT,
            CheckpointPhase.POST_SUBMIT_SETTLE,
        )
        otp = re.compile(
            r"^[\s\S]{0,400}(?:OTP|一次性(?:密碼|驗證碼)|簡訊驗證碼|動態驗證碼|"
            r"裝置驗證|新裝置登入|信任此裝置)[\s\S]{0,400}$",
            re.IGNORECASE,
        )
        password = re.compile(
            r"^[\s\S]{0,200}(?:密碼(?:已)?(?:到期|過期)|"
            r"(?:立即|必須|請先|需要|需)\s*(?:修改|變更|重設)\s*(?:您的?)?\s*密碼|"
            r"強制\s*(?:修改|變更|重設)\s*(?:您的?)?\s*密碼)[\s\S]{0,200}$"
        )
        error = re.compile(
            r"^\s*(?:密碼不正確|帳號(?:已遭|已被|已)鎖定|登入失敗|"
            r"驗證碼(?:錯誤|不正確)，?請重新輸入|Invalid credentials|Account locked)"
            r"[\s。.!！?？:：,，]*$",
            re.IGNORECASE,
        )
        security_notice = re.compile(
            r"^(?![\s\S]*(?:異常登入|是否本人|裝置驗證|新裝置|OTP|驗證碼|條款|授權|確認交易))"
            r"(?=[\s\S]*資訊安全)(?=[\s\S]*密碼)[\s\S]{1,120}$"
        )
        modal_scopes = (("modal", ".modal.show"), ("dialog", "[role='dialog']"))
        alert_scopes = (
            ("error", ".error"),
            ("alert", ".alert"),
            ("role-alert", "[role='alert']"),
        )
        return (
            *(
                LoginCheckpointRule(
                    name=f"hsbc-otp-required-{suffix}", bank="hsbc",
                    phases=all_phases, kind=CheckpointKind.OTP_REQUIRED,
                    container_selector=selector, required_body_pattern=otp,
                )
                for suffix, selector in modal_scopes
            ),
            *(
                LoginCheckpointRule(
                    name=f"hsbc-password-change-required-{suffix}", bank="hsbc",
                    phases=all_phases, kind=CheckpointKind.PASSWORD_CHANGE_REQUIRED,
                    container_selector=selector, required_body_pattern=password,
                )
                for suffix, selector in modal_scopes
            ),
            *(
                LoginCheckpointRule(
                    name=f"hsbc-explicit-login-error-{suffix}", bank="hsbc",
                    phases=post_settle, kind=CheckpointKind.EXPLICIT_LOGIN_ERROR,
                    container_selector=selector, required_body_pattern=error,
                )
                for suffix, selector in alert_scopes
            ),
            LoginCheckpointRule(
                name="hsbc-security-notice",
                bank="hsbc",
                phases=post_settle,
                kind=CheckpointKind.DISMISSIBLE_NOTICE,
                container_selector="[role='dialog']",
                action_texts=("繼續",),
                required_body_pattern=security_notice,
            ),
            *(
                LoginCheckpointRule(
                    name=f"hsbc-unknown-{suffix}", bank="hsbc", phases=all_phases,
                    kind=CheckpointKind.UNKNOWN_BLOCKER, container_selector=selector,
                )
                for suffix, selector in modal_scopes
            ),
            *(
                LoginCheckpointRule(
                    name=f"hsbc-login-form-still-visible-{selector[1:]}", bank="hsbc",
                    phases=post_settle, kind=CheckpointKind.UNKNOWN_BLOCKER,
                    container_selector=selector,
                )
                for selector in (SEL_USERID, SEL_PWD, SEL_CAPTCHA)
            ),
        )

    @staticmethod
    def _visible_enabled(page, selector: str, *, optional: bool = False):
        with error_step({SEL_USERID: 'HSBC-02-010-001', SEL_PWD: 'HSBC-02-100-001', SEL_CAPTCHA: 'HSBC-02-200-001'}[selector]):
            matches = page.locator(selector)
        with error_step({SEL_USERID: 'HSBC-02-011-001', SEL_PWD: 'HSBC-02-101-001', SEL_CAPTCHA: 'HSBC-02-201-001'}[selector]):
            count = matches.count()
        visible = []
        for index in range(count):
            with error_step({SEL_USERID: 'HSBC-02-012-001', SEL_PWD: 'HSBC-02-102-001', SEL_CAPTCHA: 'HSBC-02-202-001'}[selector]):
                candidate = matches.nth(index)
            with error_step({SEL_USERID: 'HSBC-02-013-001', SEL_PWD: 'HSBC-02-103-001', SEL_CAPTCHA: 'HSBC-02-203-001'}[selector]):
                shown = candidate.is_visible()
            if shown:
                with error_step({SEL_USERID: 'HSBC-02-014-001', SEL_PWD: 'HSBC-02-104-001', SEL_CAPTCHA: 'HSBC-02-204-001'}[selector]):
                    visible.append(matches.nth(index))
        if not visible and optional:
            return None
        if len(visible) != 1:
            raise with_error_code(HsbcLoginError("登入欄位無法安全確認；未送出登入"), {SEL_USERID: 'HSBC-02-015-001', SEL_PWD: 'HSBC-02-105-001', SEL_CAPTCHA: 'HSBC-02-205-001'}[selector])
        with error_step({SEL_USERID: 'HSBC-02-016-001', SEL_PWD: 'HSBC-02-106-001', SEL_CAPTCHA: 'HSBC-02-206-001'}[selector]):
            enabled = visible[0].is_enabled()
        if not enabled:
            raise with_error_code(HsbcLoginError("登入欄位無法安全確認；未送出登入"), {SEL_USERID: 'HSBC-02-017-001', SEL_PWD: 'HSBC-02-107-001', SEL_CAPTCHA: 'HSBC-02-207-001'}[selector])
        return visible[0]

    @staticmethod
    def _keyboard_fill(page, field, value: str, *, field_role="user") -> None:
        with error_step({'user': 'HSBC-02-020-001', 'password': 'HSBC-02-110-001', 'captcha': 'HSBC-02-210-001'}[field_role]):
            field.click()
        with error_step({'user': 'HSBC-02-021-001', 'password': 'HSBC-02-111-001', 'captcha': 'HSBC-02-211-001'}[field_role]):
            field.click(click_count=3)
        with error_step({'user': 'HSBC-02-022-001', 'password': 'HSBC-02-112-001', 'captcha': 'HSBC-02-212-001'}[field_role]):
            page.keyboard.press("Backspace")
        with error_step({'user': 'HSBC-02-023-001', 'password': 'HSBC-02-113-001', 'captcha': 'HSBC-02-213-001'}[field_role]):
            page.keyboard.type(value, delay=80)
        with error_step({'user': 'HSBC-02-024-001', 'password': 'HSBC-02-114-001', 'captcha': 'HSBC-02-214-001'}[field_role]):
            actual_length = len(field.input_value())
        if actual_length != len(value):
            raise with_error_code(HsbcLoginError("登入欄位輸入長度不符；未送出登入"), {'user': 'HSBC-02-025-001', 'password': 'HSBC-02-115-001', 'captcha': 'HSBC-02-215-001'}[field_role])

    @staticmethod
    def _exact_button(page, selector: str, text: str, *, candidate_only: bool = False):
        with error_step({"button[data-testid='continueButton']": 'HSBC-02-300-001', "button[type='submit']": 'HSBC-02-301-001'}[selector]):
            matches = page.locator(selector)
        eligible = []
        with error_step({"button[data-testid='continueButton']": 'HSBC-02-308-001', "button[type='submit']": 'HSBC-02-309-001'}[selector]):
            for index in range(matches.count()):
                with error_step({"button[data-testid='continueButton']": 'HSBC-02-302-001', "button[type='submit']": 'HSBC-02-303-001'}[selector]):
                    button = matches.nth(index)
                with error_step({"button[data-testid='continueButton']": 'HSBC-02-304-001', "button[type='submit']": 'HSBC-02-305-001'}[selector]):
                    if not button.is_visible():
                        continue
                with error_step({"button[data-testid='continueButton']": 'HSBC-02-340-001', "button[type='submit']": 'HSBC-02-341-001'}[selector]):
                    if not button.is_enabled():
                        continue
                with error_step({"button[data-testid='continueButton']": 'HSBC-02-306-001', "button[type='submit']": 'HSBC-02-307-001'}[selector]):
                    if candidate_only and " ".join(button.inner_text().split()) != text:
                        continue
                eligible.append(button)
        if len(eligible) != 1:
            raise with_error_code(HsbcLoginError('找不到唯一且可操作的登入按鈕；未送出登入'), {"button[data-testid='continueButton']": 'HSBC-02-310-001', "button[type='submit']": 'HSBC-02-311-001'}[selector])
        with error_step({"button[data-testid='continueButton']": 'HSBC-02-310-002', "button[type='submit']": 'HSBC-02-311-002'}[selector]):
            if ' '.join(eligible[0].inner_text().split()) != text:
                raise with_error_code(HsbcLoginError('找不到唯一且可操作的登入按鈕；未送出登入'), {"button[data-testid='continueButton']": 'HSBC-02-310-002', "button[type='submit']": 'HSBC-02-311-002'}[selector])
        return eligible[0]

    @staticmethod
    def _captcha_image(page, *, diagnostics: list[str] | None = None):
        if diagnostics is not None:
            diagnostics.clear()
        with error_step('HSBC-02-312-001'):
            images = page.locator("img")
        eligible = []
        with error_step('HSBC-02-317-001'):
            for index in range(images.count()):
                with error_step('HSBC-02-313-001'):
                    image = images.nth(index)
                with error_step('HSBC-02-314-001'):
                    if not image.is_visible():
                        continue
                with error_step('HSBC-02-315-001'):
                    src = image.get_attribute("src") or ""
                with error_step('HSBC-02-316-001'):
                    box = image.bounding_box()
                if (
                    src.startswith("data:image/jpeg;base64,")
                    and box is not None
                    and 80 <= box["width"] <= 200
                    and 25 <= box["height"] <= 60
                ):
                    eligible.append(image)
        if len(eligible) != 1 and diagnostics is not None:
            diagnostics[:] = ['HSBC-02-511-001' if not eligible else 'HSBC-02-511-002']
        return eligible[0] if len(eligible) == 1 else None

    @classmethod
    def _stable_captcha(cls, page, previous_digest: bytes | None, *, diagnostics: list[str] | None = None):
        diagnostics = [] if diagnostics is None else diagnostics
        diagnostics.clear()
        last_digest = None
        for _ in range(12):
            image = cls._captcha_image(page, diagnostics=diagnostics)
            if image is None:
                return None
            with error_step('HSBC-02-318-001'):
                raw = image.screenshot()
            digest = hashlib.sha256(raw).digest()
            if digest != previous_digest and digest == last_digest:
                return raw, digest
            last_digest = digest if digest != previous_digest else None
            with error_step('HSBC-02-319-001'):
                page.wait_for_timeout(300)
        diagnostics[:] = ['HSBC-02-511-003']
        return None

    @staticmethod
    def _refresh_captcha(page) -> bool:
        with error_step('HSBC-02-320-001'):
            buttons = page.locator("button[aria-label='Refresh Captcha']")
        with error_step('HSBC-02-321-001'):
            eligible = [
                error_step('HSBC-02-500-001')(lambda: buttons.nth(index))()
                for index in range(error_step('HSBC-02-500-006')(lambda: buttons.count())())
                if error_step('HSBC-02-500-003')(lambda: error_step('HSBC-02-500-002')(lambda: buttons.nth(index))().is_visible())() and error_step('HSBC-02-500-005')(lambda: error_step('HSBC-02-500-004')(lambda: buttons.nth(index))().is_enabled())()
            ]
        if len(eligible) != 1:
            return False
        with error_step('HSBC-02-322-001'):
            eligible[0].click()
        return True

    def _solve_captcha(self, page, *, diagnostics: list[str] | None = None) -> str | None:
        diagnostics = [] if diagnostics is None else diagnostics
        diagnostics.clear()
        previous_digest = None
        for attempt in range(1):  # unreadable → shared login reloads the page
            try:
                diagnostics.clear()
                stable = self._stable_captcha(page, previous_digest, diagnostics=diagnostics)
                if stable is None:
                    return None
                raw, previous_digest = stable
            except Exception as error:
                diagnostics[:] = [exception_error_id(error, bank="hsbc") or 'HSBC-02-510-001']
                return None
            try:
                with error_step('HSBC-02-510-002'):
                    result = ocr_bytes(
                        raw, expected_len=5, alnum_only=True,
                        min_confidence=HSBC_CAPTCHA_MIN_CONFIDENCE,
                    )
            except Exception as error:
                diagnostics[:] = [exception_error_id(error, bank="hsbc") or 'HSBC-02-510-002']
                result = None
            if isinstance(result, str) and re.fullmatch(r"[A-Za-z0-9]{5}", result):
                diagnostics.clear()
                return result
            if not diagnostics:
                diagnostics[:] = ['HSBC-02-510-003' if result is None else 'HSBC-02-510-004']
            try:
                if attempt == 0 or not self._refresh_captcha(page):
                    return None
            except Exception:
                return None
        return None

    @staticmethod
    def _response_visible(page, *, diagnostic_role="post-submit") -> bool:
        codes = {'post-submit': {'.modal.show': ('HSBC-03-550-001', 'HSBC-03-550-002', 'HSBC-03-550-003', 'HSBC-03-550-004'), "[role='dialog']": ('HSBC-03-560-001', 'HSBC-03-560-002', 'HSBC-03-560-003', 'HSBC-03-560-004'), '.error': ('HSBC-03-570-001', 'HSBC-03-570-002', 'HSBC-03-570-003', 'HSBC-03-570-004'), '.alert': ('HSBC-03-580-001', 'HSBC-03-580-002', 'HSBC-03-580-003', 'HSBC-03-580-004'), "[role='alert']": ('HSBC-03-590-001', 'HSBC-03-590-002', 'HSBC-03-590-003', 'HSBC-03-590-004')}, 'account-stage': {'.modal.show': ('HSBC-03-610-001', 'HSBC-03-610-002', 'HSBC-03-610-003', 'HSBC-03-610-004'), "[role='dialog']": ('HSBC-03-620-001', 'HSBC-03-620-002', 'HSBC-03-620-003', 'HSBC-03-620-004'), '.error': ('HSBC-03-630-001', 'HSBC-03-630-002', 'HSBC-03-630-003', 'HSBC-03-630-004'), '.alert': ('HSBC-03-640-001', 'HSBC-03-640-002', 'HSBC-03-640-003', 'HSBC-03-640-004'), "[role='alert']": ('HSBC-03-650-001', 'HSBC-03-650-002', 'HSBC-03-650-003', 'HSBC-03-650-004')}, 'pre-submit': {'.modal.show': ('HSBC-03-660-001', 'HSBC-03-660-002', 'HSBC-03-660-003', 'HSBC-03-660-004'), "[role='dialog']": ('HSBC-03-670-001', 'HSBC-03-670-002', 'HSBC-03-670-003', 'HSBC-03-670-004'), '.error': ('HSBC-03-680-001', 'HSBC-03-680-002', 'HSBC-03-680-003', 'HSBC-03-680-004'), '.alert': ('HSBC-03-690-001', 'HSBC-03-690-002', 'HSBC-03-690-003', 'HSBC-03-690-004'), "[role='alert']": ('HSBC-03-700-001', 'HSBC-03-700-002', 'HSBC-03-700-003', 'HSBC-03-700-004')}}[diagnostic_role]
        for selector in (
            ".modal.show", "[role='dialog']", ".error", ".alert", "[role='alert']",
        ):
            with error_step(codes[selector][0]):
                matches = page.locator(selector)
            with error_step('HSBC-03-304-001'):
                if any(error_step(codes[selector][3])(lambda: error_step(codes[selector][2])(lambda: matches.nth(index))().is_visible())() for index in range(error_step(codes[selector][1])(lambda: matches.count())())):
                    return True
        return False

    def submit_credentials_once(self, page) -> None:
        try:
            user_id = self._visible_enabled(page, SEL_USERID, optional=True)
            if user_id is not None:
                self._keyboard_fill(page, user_id, self.creds.user_id, field_role="user")
                first = self._exact_button(
                    page, "button[data-testid='continueButton']", "繼續"
                )
                try:
                    with error_step('HSBC-02-323-001'):
                        first.click(timeout=8000)
                    with error_step('HSBC-02-324-001'):
                        page.wait_for_timeout(6000)
                except Exception as error:
                    raise with_error_code(HsbcLoginError('帳號階段狀態不明；未送出登入'), exception_error_id(error, bank="hsbc") or 'HSBC-02-325-001') from None
                if getattr(self, "_shared_dialog_blocked", False):
                    raise with_error_code(HsbcLoginError('帳號階段出現未分類提示；未送出登入'), 'HSBC-02-326-001')
                if self._response_visible(page, diagnostic_role="account-stage"):
                    raise with_error_code(HsbcLoginError('帳號階段出現未分類提示；未送出登入'), 'HSBC-02-327-001')

            password = self._visible_enabled(page, SEL_PWD)
            self._keyboard_fill(page, password, self.creds.password, field_role="password")
            diagnostics: list[str] = []
            captcha_text = self._solve_captcha(page, diagnostics=diagnostics)
            if captcha_text is None:
                raise with_error_code(captcha_unreadable(HsbcLoginError('無法安全辨識驗證碼；未送出登入')), next(iter(diagnostics), 'HSBC-02-328-001'))
            captcha = self._visible_enabled(page, SEL_CAPTCHA)
            self._keyboard_fill(page, captcha, captcha_text, field_role="captcha")
            final = self._exact_button(
                page, "button[type='submit']", "繼續", candidate_only=True
            )
            if getattr(self, "_shared_dialog_blocked", False):
                raise with_error_code(HsbcLoginError('登入前出現未分類提示；未送出登入'), 'HSBC-02-329-001')
            if self._response_visible(page, diagnostic_role="pre-submit"):
                raise with_error_code(HsbcLoginError('登入前出現未分類提示；未送出登入'), 'HSBC-02-330-001')
        except HsbcLoginError:
            raise
        except Exception as error:
            raise with_error_code(HsbcLoginError('登入欄位無法安全填寫；未送出登入'), exception_error_id(error, bank="hsbc") or 'HSBC-02-331-001') from None

        try:
            with error_step('HSBC-02-332-001'):
                final.click(timeout=8000)
        except Exception as error:
            raise with_error_code(HsbcLoginError('登入送出狀態不明；禁止自動重試'), exception_error_id(error, bank="hsbc") or 'HSBC-02-333-001') from None

        try:
            for _ in range(22):
                with error_step('HSBC-03-334-001'):
                    page.wait_for_timeout(1000)
                if self._logged_in(page) or self._response_visible(page):
                    return
        except Exception as error:
            raise with_error_code(HsbcLoginError('登入送出後狀態無法安全確認；禁止自動重試'), exception_error_id(error, bank="hsbc") or 'HSBC-03-335-001') from None

    # ---------- 抓取 ----------
    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        """Stream current card inventory and details, then recheck inventory."""
        out: dict = {}
        with error_step('HSBC-07-300-001'):
            page.wait_for_timeout(7000)

        token = self._history_token(collector)
        detach = getattr(collector, "detach", None)
        if callable(detach):
            detach(page)
        byte_budget = [5_000_000]
        cards = self._fetch_card_inventory(page, token, byte_budget)
        out["cards"] = cards
        _log(f"[collect] 卡片清單: {len(out['cards'])} 張")
        out["card_detail"], out["history_coverage"] = self._collect_card_details(
            page,
            collector,
            out["cards"],
            byte_budget=byte_budget,
        )
        if self._fetch_card_inventory(page, token, byte_budget) != cards:
            raise with_error_code(RuntimeError('hsbc-card-inventory-replay'), 'HSBC-07-301-001')
        publish_card_bill_facts(out, _hsbc_card_bill_facts(out))
        return BankCollectResult(**out)

    @classmethod
    def _fetch_card_inventory(cls, page, token: str, byte_budget: list[int]) -> list[dict]:
        url = "https://card.hsbc.com.tw/ibk-bff/api/v1/cards"
        if byte_budget[0] <= 0:
            raise with_error_code(RuntimeError('hsbc-card-inventory-byte-budget'), 'HSBC-04-300-001')
        diagnostics: list[str] = []
        response = cls._fetch_api_page(
            page, url=url, token=token, timeout_ms=30_000, max_bytes=byte_budget[0],
            diagnostics=diagnostics, diagnostic_role="inventory",
        )
        if not isinstance(response, dict):
            raise with_error_code(RuntimeError('hsbc-card-inventory-byte-budget'), next(iter(diagnostics), 'HSBC-04-301-001'))
        if type(response.get('bytes')) is not int:
            raise with_error_code(RuntimeError('hsbc-card-inventory-byte-budget'), 'HSBC-04-301-002')
        if not 0 <= response['bytes'] <= byte_budget[0]:
            raise with_error_code(RuntimeError('hsbc-card-inventory-byte-budget'), next(iter(diagnostics), 'HSBC-04-301-003'))
        byte_budget[0] -= response["bytes"]
        if response.get('url') != url:
            raise with_error_code(RuntimeError('hsbc-card-inventory-envelope'), 'HSBC-04-302-001')
        if response.get('redirected') is not False:
            raise with_error_code(RuntimeError('hsbc-card-inventory-envelope'), 'HSBC-04-302-002')
        snapshot = ResponseCollector("card.hsbc.com.tw")
        snapshot.hits = [ApiHit(
            url=url,
            method="GET",
            status=response.get("status"),
            content_type=response.get("contentType"),
            body_size=response["bytes"],
            resp_json=response.get("body"),
        )]
        return cls._card_inventory(snapshot)

    @staticmethod
    def _card_inventory(collector: ResponseCollector) -> list[dict]:
        """Return one exact, internally consistent card-list snapshot."""
        base = "https://card.hsbc.com.tw/ibk-bff/api/v1/"
        relevant = [
            hit for hit in collector.hits
            if hit.url in {base + "cards", base + "cards/suspend"}
        ]
        inventory_bytes = 0
        for hit in relevant:
            body_size = getattr(hit, "body_size", None)
            if type(body_size) is not int:
                raise with_error_code(RuntimeError('hsbc-card-inventory-byte-budget'), 'HSBC-04-303-001')
            if not 0 <= body_size <= 5000000:
                raise with_error_code(RuntimeError('hsbc-card-inventory-byte-budget'), 'HSBC-04-303-002')
            inventory_bytes += body_size
            if inventory_bytes > 5_000_000:
                raise with_error_code(RuntimeError('hsbc-card-inventory-byte-budget'), 'HSBC-04-304-001')
        current = [hit for hit in collector.hits if hit.url == base + "cards"]
        hits = current or [hit for hit in collector.hits if hit.url == base + "cards/suspend"]
        if not hits:
            raise with_error_code(RuntimeError('hsbc-card-inventory-missing'), 'HSBC-04-305-001')

        payloads = []
        for hit in hits:
            body = hit.resp_json
            body_size = getattr(hit, "body_size", None)
            if (getattr(hit, 'raw_url', '') or hit.url) not in {base + 'cards', base + 'cards/suspend'}:
                raise with_error_code(RuntimeError('hsbc-card-inventory-envelope'), 'HSBC-04-306-001')
            if getattr(hit, 'redirected', False):
                raise with_error_code(RuntimeError('hsbc-card-inventory-envelope'), 'HSBC-04-306-002')
            if type(body_size) is not int:
                raise with_error_code(RuntimeError('hsbc-card-inventory-envelope'), 'HSBC-04-306-003')
            if not 0 <= body_size <= 5000000:
                raise with_error_code(RuntimeError('hsbc-card-inventory-envelope'), 'HSBC-04-306-004')
            if hit.method != 'GET':
                raise with_error_code(RuntimeError('hsbc-card-inventory-envelope'), 'HSBC-04-306-005')
            if hit.status != 200:
                raise with_error_code(RuntimeError('hsbc-card-inventory-envelope'), 'HSBC-04-306-006')
            if not _is_json_content_type(hit.content_type):
                raise with_error_code(RuntimeError('hsbc-card-inventory-envelope'), 'HSBC-04-306-007')
            if not isinstance(body, dict):
                raise with_error_code(RuntimeError('hsbc-card-inventory-envelope'), 'HSBC-04-306-008')
            if body.get('success') is not True:
                raise with_error_code(RuntimeError('hsbc-card-inventory-envelope'), 'HSBC-04-306-009')
            if body.get('error') not in (None, '', []):
                raise with_error_code(RuntimeError('hsbc-card-inventory-envelope'), 'HSBC-04-306-010')
            if not isinstance(body.get('payload'), list):
                raise with_error_code(RuntimeError('hsbc-card-inventory-envelope'), 'HSBC-04-306-011')
            payloads.append(body["payload"])
        cards = payloads[0]
        if any(payload != cards for payload in payloads[1:]):
            raise with_error_code(RuntimeError('hsbc-card-inventory-replay'), 'HSBC-04-307-001')
        if len(cards) > 100:
            raise with_error_code(RuntimeError('hsbc-card-inventory-count'), 'HSBC-04-308-001')

        card_ids: set[str] = set()
        identities: set[str] = set()
        for card in cards:
            if not isinstance(card, dict):
                raise with_error_code(RuntimeError('hsbc-card-inventory-row'), 'HSBC-04-309-001')
            card_id = card.get("id")
            identity = card.get("maskedCardNumber")
            bounded_fields = (
                (key, card.get(key))
                for key in (
                    "name", "cardType", "cardStatusDisplay", "paymentDueDate", "statementDate",
                )
            )
            if not isinstance(card_id, str):
                raise with_error_code(RuntimeError('hsbc-card-inventory-identity'), 'HSBC-04-310-001')
            if re.fullmatch('[A-Za-z0-9_-]{1,128}', card_id) is None:
                raise with_error_code(RuntimeError('hsbc-card-inventory-identity'), 'HSBC-04-310-002')
            if not isinstance(identity, str):
                raise with_error_code(RuntimeError('hsbc-card-inventory-identity'), 'HSBC-04-310-003')
            if re.fullmatch('[0-9]{4}-\\*{4}-\\*{4}-[0-9]{4}', identity) is None:
                raise with_error_code(RuntimeError('hsbc-card-inventory-identity'), 'HSBC-04-310-004')
            if card.get('cardStatusDisplay') not in {'ACTIVATED', 'NOT_ACTIVATED', 'CLOSED'}:
                raise with_error_code(RuntimeError('hsbc-card-inventory-identity'), 'HSBC-04-310-005')
            if card_id in card_ids:
                raise with_error_code(RuntimeError('hsbc-card-inventory-identity'), 'HSBC-04-310-006')
            if identity in identities:
                raise with_error_code(RuntimeError('hsbc-card-inventory-identity'), 'HSBC-04-310-007')
            for field_name, value in bounded_fields:
                if value is None:
                    continue
                if not isinstance(value, str):
                    raise with_error_code(RuntimeError('hsbc-card-inventory-identity'), {
                        'name': 'HSBC-04-140-001', 'cardType': 'HSBC-04-141-001',
                        'cardStatusDisplay': 'HSBC-04-142-001', 'paymentDueDate': 'HSBC-04-143-001',
                        'statementDate': 'HSBC-04-144-001',
                    }[field_name])
                if len(value) > 256:
                    raise with_error_code(RuntimeError('hsbc-card-inventory-identity'), {
                        'name': 'HSBC-04-140-002', 'cardType': 'HSBC-04-141-002',
                        'cardStatusDisplay': 'HSBC-04-142-002', 'paymentDueDate': 'HSBC-04-143-002',
                        'statementDate': 'HSBC-04-144-002',
                    }[field_name])
            card_ids.add(card_id)
            identities.add(identity)
        collector.hsbc_inventory_bytes = inventory_bytes
        return cards

    @staticmethod
    def _history_token(collector: ResponseCollector) -> str:
        events = getattr(collector, "auth_token_events", None)
        if not isinstance(events, list):
            raise with_error_code(RuntimeError('hsbc-history-token'), 'HSBC-04-311-001')
        candidates = events
        valid = []
        rejected = None
        for event in candidates:
            with error_step('HSBC-04-150-010'):
                source = urlparse(event.get("url", "")) if isinstance(event, dict) else None
            token = event.get("token", "") if isinstance(event, dict) else ""
            # Keep the original AND evaluation order; rejected candidates remain
            # ignorable when a later candidate is valid.
            if source is None:
                rejected = rejected or 'HSBC-04-150-001'
                continue
            with error_step('HSBC-04-150-002'):
                if re.fullmatch(r"Bearer [^\s\r\n]+", token) is None:
                    rejected = rejected or 'HSBC-04-150-002'
                    continue
            if source.scheme.lower() != "https":
                rejected = rejected or 'HSBC-04-150-003'
                continue
            if (source.hostname or "").lower() != "card.hsbc.com.tw":
                rejected = rejected or 'HSBC-04-150-004'
                continue
            with error_step('HSBC-04-150-005'):
                if source.port not in (None, 443):
                    rejected = rejected or 'HSBC-04-150-005'
                    continue
            if source.username is not None:
                rejected = rejected or 'HSBC-04-150-006'
                continue
            if source.password is not None:
                rejected = rejected or 'HSBC-04-150-007'
                continue
            if not source.path.startswith("/ibk-bff/api/v1/"):
                rejected = rejected or 'HSBC-04-150-008'
                continue
            if event.get("redirected") is not False:
                rejected = rejected or 'HSBC-04-150-009'
                continue
            valid.append(event)
        if not valid:
            raise with_error_code(RuntimeError('hsbc-history-token'), rejected or 'HSBC-04-312-001')
        return max(valid, key=lambda event: event.get("sequence", 0))["token"]


    def _collect_card_details(
        self,
        page,
        collector,
        cards: list,
        *,
        end: date | None = None,
        byte_budget: list[int] | None = None,
    ) -> tuple[dict, dict]:
        """直接用 page fetch 打明細 API（帶 Bearer token，繞過點卡片導航）。

        已知 endpoint（實機攔到）：
          GET cards/{id}                              單卡詳情
          GET cards/{id}/transactions/posted?pageSize=10   已出帳（分頁）
          GET cards/{id}/transactions/unposted?pageSize=   未出帳（直接陣列）
        ⚠️ 裸 fetch 回 HTTP 500——必須帶前端的 `Authorization: Bearer <JWT>`（從 collector 攔到）。
        卡 id 從卡片清單 payload[].id 取（新 endpoint `cards`；legacy `cards/suspend`）。
        """
        token = self._history_token(collector)
        mode = os.environ.get("BANK_CRAWLER_HISTORY_MODE", "full")
        if mode not in {"full", "incremental"}:
            raise with_error_code(RuntimeError('hsbc-history-mode'), 'HSBC-06-301-001')
        end = end or date.today()
        details: dict = {}
        expected = []
        windows = []
        byte_budget = byte_budget if byte_budget is not None else [5_000_000]
        for c in cards:
            cid = c.get("id")
            masked = c.get("maskedCardNumber", "")
            start, account_end = self._card_history_range(masked, end=end)
            entry: dict = {"card_id": cid, "masked": masked}
            # 單卡詳情
            diagnostics: list[str] = []
            entry["detail"] = self._fetch_json(
                page, f"cards/{cid}", token, byte_budget=byte_budget, diagnostics=diagnostics,
            )
            if byte_budget[0] <= 0:
                raise with_error_code(RuntimeError('hsbc-history-byte-budget'), next(iter(diagnostics), 'HSBC-06-302-001'))
            history = self._fetch_posted_history(
                page,
                card_id=cid,
                identity=masked,
                token=token,
                start=start,
                end=account_end,
                byte_budget=byte_budget,
            )
            entry["posted"] = history["rows"]
            entry["posted_receipt"] = history["receipt"]
            # 未出帳（陣列）
            diagnostics = []
            unp = self._fetch_json(
                page,
                f"cards/{cid}/transactions/unposted?pageSize=200",
                token,
                byte_budget=byte_budget, diagnostics=diagnostics,
            )
            if byte_budget[0] <= 0:
                raise with_error_code(RuntimeError('hsbc-history-byte-budget'), next(iter(diagnostics), 'HSBC-06-303-001'))
            unposted_rows = unp if isinstance(unp, list) else []
            entry["unposted_ok"] = isinstance(unp, list)
            entry["unposted"] = unposted_rows
            details[masked] = entry
            expected.append({
                "identity": masked,
                "start": start.isoformat(),
                "end": account_end.isoformat(),
            })
            windows.append(history["receipt"])
            n_posted = len(entry["posted"])
            n_unp = len(entry["unposted"])
            _log(f"[collect] 卡片明細: 已出帳 {n_posted} 筆, 未出帳 {n_unp} 筆")
        domain = {
            "domain": "card_billed_transactions",
            "expected": expected,
            "windows": windows,
        }
        if not expected:
            domain["empty_window"] = {
                "start": self._history_floor(end).isoformat(),
                "end": end.isoformat(),
                "status": "explicit_empty",
                "pages": 1,
            }
        coverage = {"version": 1, "mode": mode, "domains": [domain]}
        validate_history_coverage(
            coverage,
            expected_mode=mode,
            expected_domains=self.HISTORY_COVERAGE_DOMAINS,
        )
        return details, coverage

    @staticmethod
    def _fetch_json(
        page, path: str, token: str = "", *, byte_budget: list[int] | None = None,
        diagnostics: list[str] | None = None,
    ):
        """Fetch one exact HSBC card API resource and return its payload."""
        diagnostics = [] if diagnostics is None else diagnostics
        diagnostics.clear()
        diagnostic_role = "unposted" if "/transactions/unposted" in path else "detail"
        codes = {'detail': ('HSBC-06-640-001', 'HSBC-06-640-002', 'HSBC-06-640-003', 'HSBC-06-640-004', 'HSBC-06-640-005', 'HSBC-06-640-006', 'HSBC-06-640-007', 'HSBC-06-640-008', 'HSBC-06-640-009', 'HSBC-06-640-010', 'HSBC-06-640-011',), 'unposted': ('HSBC-06-650-001', 'HSBC-06-650-002', 'HSBC-06-650-003', 'HSBC-06-650-004', 'HSBC-06-650-005', 'HSBC-06-650-006', 'HSBC-06-650-007', 'HSBC-06-650-008', 'HSBC-06-650-009', 'HSBC-06-650-010', 'HSBC-06-650-011',)}[diagnostic_role]
        if re.fullmatch(
            r"cards/[A-Za-z0-9_-]{1,128}(?:/transactions/unposted\?pageSize=200)?", path,
        ) is None:
            diagnostics[:] = [codes[0]]
            return None
        if re.fullmatch(r"Bearer [^\s\r\n]+", token) is None:
            diagnostics[:] = [codes[1]]
            return None
        url = "https://card.hsbc.com.tw/ibk-bff/api/v1/" + path
        try:
            max_bytes = byte_budget[0] if byte_budget is not None else 5_000_000
            if max_bytes <= 0:
                diagnostics[:] = [codes[2]]
                return None
            result = HsbcCrawler._fetch_api_page(
                page,
                url=url,
                token=token,
                timeout_ms=30_000,
                max_bytes=max_bytes, diagnostics=diagnostics, diagnostic_role=diagnostic_role,
            )
            if not isinstance(result, dict):
                if not diagnostics:
                    diagnostics[:] = [codes[3]]
                return None
            rejected = (
                codes[4] if type(result.get("bytes")) is not int else
                codes[5] if result["bytes"] < 0 else
                codes[6] if result["bytes"] > max_bytes else None
            )
            if rejected:
                if not diagnostics:
                    diagnostics[:] = [rejected]
                if byte_budget is not None and isinstance(result, dict) and result.get("exceeded") is True:
                    byte_budget[0] = 0
                return None
            if byte_budget is not None:
                byte_budget[0] -= result["bytes"]
            body = result.get("body")
            if not isinstance(body, dict):
                diagnostics[:] = [codes[7]]
                return None
            if body.get("success") is not True:
                diagnostics[:] = [codes[8]]
                return None
            if body.get("error") not in (None, "", []):
                diagnostics[:] = [codes[9]]
                return None
            return body.get("payload")
        except Exception:
            diagnostics[:] = [codes[10]]
            _log("[fetch] HSBC API request failed [" + codes[10] + "]")
            return None

    @staticmethod
    def _fetch_api_page(
        page, *, url: str, token: str, timeout_ms: int, max_bytes: int = 5_000_000,
        diagnostics: list[str] | None = None, diagnostic_role: str = "posted",
    ):
        diagnostics = [] if diagnostics is None else diagnostics
        diagnostics.clear()
        codes = {
            'inventory': ('HSBC-04-600-001', 'HSBC-04-600-002', 'HSBC-04-600-003', 'HSBC-04-600-004',),
            'posted': ('HSBC-06-600-001', 'HSBC-06-600-002', 'HSBC-06-600-003', 'HSBC-06-600-004',),
            'detail': ('HSBC-06-610-001', 'HSBC-06-610-002', 'HSBC-06-610-003', 'HSBC-06-610-004',),
            'unposted': ('HSBC-06-620-001', 'HSBC-06-620-002', 'HSBC-06-620-003', 'HSBC-06-620-004',),
        }[diagnostic_role]
        js_codes = {
            'inventory': {1: 'HSBC-04-601-001', 2: 'HSBC-04-601-002', 3: 'HSBC-04-601-003', 4: 'HSBC-04-601-004', 5: 'HSBC-04-601-005', 6: 'HSBC-04-601-006', 7: 'HSBC-04-601-007', 8: 'HSBC-04-601-008', 9: 'HSBC-04-601-009', 10: 'HSBC-04-601-010', 11: 'HSBC-04-601-011', 12: 'HSBC-04-601-012'},
            'posted': {1: 'HSBC-06-601-001', 2: 'HSBC-06-601-002', 3: 'HSBC-06-601-003', 4: 'HSBC-06-601-004', 5: 'HSBC-06-601-005', 6: 'HSBC-06-601-006', 7: 'HSBC-06-601-007', 8: 'HSBC-06-601-008', 9: 'HSBC-06-601-009', 10: 'HSBC-06-601-010', 11: 'HSBC-06-601-011', 12: 'HSBC-06-601-012'},
            'detail': {1: 'HSBC-06-611-001', 2: 'HSBC-06-611-002', 3: 'HSBC-06-611-003', 4: 'HSBC-06-611-004', 5: 'HSBC-06-611-005', 6: 'HSBC-06-611-006', 7: 'HSBC-06-611-007', 8: 'HSBC-06-611-008', 9: 'HSBC-06-611-009', 10: 'HSBC-06-611-010', 11: 'HSBC-06-611-011', 12: 'HSBC-06-611-012'},
            'unposted': {1: 'HSBC-06-621-001', 2: 'HSBC-06-621-002', 3: 'HSBC-06-621-003', 4: 'HSBC-06-621-004', 5: 'HSBC-06-621-005', 6: 'HSBC-06-621-006', 7: 'HSBC-06-621-007', 8: 'HSBC-06-621-008', 9: 'HSBC-06-621-009', 10: 'HSBC-06-621-010', 11: 'HSBC-06-621-011', 12: 'HSBC-06-621-012'},
        }[diagnostic_role]
        reason = codes[0]
        try:
            with error_step(codes[0]):
                result = page.evaluate(
                    "async ({url, tok, timeoutMs, maxBytes}) => { const controller=new AbortController(); const timer=setTimeout(()=>controller.abort(),timeoutMs); let diagnostic=7; try { const r=await fetch(url,{credentials:'include',redirect:'error', signal:controller.signal, headers:{'Accept':'application/json','Authorization':tok}}); diagnostic=8; const ct=(r.headers.get('content-type')||'').split(';')[0].trim().toLowerCase(); const length=r.headers.get('content-length'); const rejected=r.url!==url?1:r.status!==200?2:ct!=='application/json'?3: (length!==null?(!/^\\d+$/.test(length)?4:Number(length)>maxBytes?5:0):0); if(rejected) {if(r.body)try{await r.body.cancel();}catch(_){}return {_diagnostic:rejected};} diagnostic=9; const reader=r.body.getReader(); const chunks=[]; let bytes=0; diagnostic=10; while(true){const part=await reader.read();if(part.done)break; bytes+=part.value.byteLength;if(bytes>maxBytes){ try{await reader.cancel();}catch(_){}return {bytes,exceeded:true,_diagnostic:6};} chunks.push(part.value);} const merged=new Uint8Array(bytes);let offset=0; for(const chunk of chunks){merged.set(chunk,offset);offset+=chunk.byteLength;} diagnostic=11; const text=new TextDecoder('utf-8',{fatal:true}).decode(merged); diagnostic=12; return {url:r.url,status:r.status,contentType:r.headers.get('content-type')||'', redirected:r.redirected,bytes,text}; } catch (_) { return {_diagnostic:diagnostic}; } finally { clearTimeout(timer); } }",
                    {"url": url, "tok": token, "timeoutMs": timeout_ms, "maxBytes": max_bytes},
                )
            if isinstance(result, dict):
                marker = result.pop("_diagnostic", None)
                if type(marker) is int and marker in js_codes:
                    diagnostics[:] = [js_codes[marker]]
                    if not (result.get("exceeded") is True and type(result.get("bytes")) is int):
                        return None
            if (
                isinstance(result, dict)
                and result.get("exceeded") is True
                and type(result.get("bytes")) is int
            ):
                return result
            if not isinstance(result, dict):
                diagnostics[:] = [codes[1]]
                return None
            if not isinstance(result.get("text"), str):
                diagnostics[:] = [codes[2]]
                return None
            reason = codes[3]
            with error_step(reason):
                result["body"] = _strict_json_loads(result.pop("text"))
            return result
        except Exception:
            diagnostics[:] = [reason]
            return None

    @staticmethod
    def _attest_api_page(
        page, *, url: str, token: str, timeout_ms: int, max_bytes: int,
        expected_snapshot: dict[str, object], diagnostics: list[str] | None = None,
    ):
        diagnostics = [] if diagnostics is None else diagnostics
        diagnostics.clear()
        codes = ('HSBC-06-630-001', 'HSBC-06-630-002', 'HSBC-06-630-003', 'HSBC-06-630-004', 'HSBC-06-630-005', 'HSBC-06-630-006', 'HSBC-06-630-007', 'HSBC-06-630-008',)
        js_codes = {1: 'HSBC-06-631-001', 2: 'HSBC-06-631-002', 3: 'HSBC-06-631-003', 4: 'HSBC-06-631-004', 5: 'HSBC-06-631-005', 6: 'HSBC-06-631-006', 7: 'HSBC-06-631-007', 8: 'HSBC-06-631-008', 9: 'HSBC-06-631-009', 10: 'HSBC-06-631-010', 11: 'HSBC-06-631-011', 12: 'HSBC-06-631-012'}
        reason = codes[0]
        try:
            with error_step(codes[0]):
                result = page.evaluate(
                    "async ({url,tok,timeoutMs,maxBytes,expectedSnapshot}) => { const controller=new AbortController(); const timer=setTimeout(()=>controller.abort(),timeoutMs); let diagnostic=7; try { const r=await fetch(url,{credentials:'include',redirect:'error', signal:controller.signal, headers:{'Accept':'application/json','Authorization':tok}}); diagnostic=8; const ct=(r.headers.get('content-type')||'').split(';')[0].trim().toLowerCase(); const length=r.headers.get('content-length'); const rejected=r.url!==url?1:r.status!==200?2:ct!=='application/json'?3: (length!==null?(!/^\\d+$/.test(length)?4:Number(length)>maxBytes?5:0):0); if(rejected) {if(r.body)try{await r.body.cancel();}catch(_){}return {_diagnostic:rejected};} diagnostic=9; const reader=r.body.getReader(); const chunks=[]; let bytes=0; diagnostic=10; while(true){const part=await reader.read();if(part.done)break; bytes+=part.value.byteLength;if(bytes>maxBytes){ try{await reader.cancel();}catch(_){}return {_diagnostic:6};} chunks.push(part.value);} const merged=new Uint8Array(bytes);let offset=0; for(const chunk of chunks){merged.set(chunk,offset);offset+=chunk.byteLength;} diagnostic=11; const text=new TextDecoder('utf-8',{fatal:true}).decode(merged); diagnostic=12; return {url:r.url,status:r.status,contentType:r.headers.get('content-type')||'', redirected:r.redirected,bytes,text}; } catch (_) { return {_diagnostic:diagnostic}; } finally { clearTimeout(timer); } }",
                    {
                        "url": url,
                        "tok": token,
                        "timeoutMs": timeout_ms,
                        "maxBytes": max_bytes,
                        "expectedSnapshot": expected_snapshot,
                    },
                )
            if isinstance(result, dict):
                marker = result.pop("_diagnostic", None)
                if type(marker) is int and marker in js_codes:
                    diagnostics[:] = [js_codes[marker]]
                    if not (result.get("exceeded") is True and type(result.get("bytes")) is int):
                        return None
            if not isinstance(result, dict):
                diagnostics[:] = [codes[1]]
                return None
            if not isinstance(result.get("text"), str):
                diagnostics[:] = [codes[2]]
                return None
            reason = codes[3]
            with error_step(reason):
                body = _strict_json_loads(result.pop("text"))
            if not isinstance(body, dict):
                diagnostics[:] = [codes[4]]
                return None
            if body.get("success") is not True:
                diagnostics[:] = [codes[5]]
                return None
            error = body.get("error")
            if error not in (None, "", []):
                diagnostics[:] = [codes[6]]
                return None
            payload = body.get("payload")
            if not isinstance(payload, dict):
                diagnostics[:] = [codes[7]]
                return None
            selected = {
                "pageInfo": payload.get("pageInfo"),
                "content": payload.get("content"),
            }
            result["matched"] = selected == expected_snapshot
            return result
        except Exception:
            diagnostics[:] = [reason]
            return None

    @staticmethod
    def _api_date(raw: object, *, field_role="posted") -> date:
        if not isinstance(raw, str):
            raise with_error_code(RuntimeError('hsbc-posted-history'), {'posted': 'HSBC-06-306-001', 'transaction': 'HSBC-06-307-001'}[field_role])
        if re.fullmatch('\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}(?::\\d{2}(?:\\.\\d+)?)?(?:Z|[+-]\\d{2}:?\\d{2})?', raw) is None:
            raise with_error_code(RuntimeError('hsbc-posted-history'), {'posted': 'HSBC-06-306-002', 'transaction': 'HSBC-06-307-002'}[field_role])
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except ValueError:
            raise with_error_code(RuntimeError('hsbc-posted-history'), {'posted': 'HSBC-06-308-001', 'transaction': 'HSBC-06-309-001'}[field_role]) from None

    @classmethod
    def _validate_posted_row(cls, row: object, *, end: date) -> date:
        if not isinstance(row, dict):
            raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-310-001')
        # This exact native sentinel means unposted, not an ancient posting date.
        if row.get("postedDate") == "0002-11-30T00:00":
            raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-311-001')
        posted = cls._api_date(row.get("postedDate"))
        cls._api_date(row.get("transactionDate"), field_role="transaction")
        amount = row.get("ntdAmount") or row.get("amount")
        match = re.fullmatch(
            r"([+]?(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d+)?) TWD",
            amount if isinstance(amount, str) else "",
        )
        try:
            amount_value = Decimal(match.group(1).replace(",", "")) if match else None
        except InvalidOperation:
            amount_value = None
        description = row.get("description")
        is_positive = row.get("isPositive")
        is_foreign = row.get("isForeign")
        if match is None:
            raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-312-001')
        if amount_value is None:
            raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-312-002')
        if not amount_value.is_finite():
            raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-312-003')
        if amount_value != amount_value.to_integral_value():
            raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-312-004')
        if not 0 <= amount_value <= Decimal('100000000'):
            raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-312-005')
        if type(is_positive) is not bool:
            raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-312-006')
        if type(is_foreign) is not bool:
            raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-312-007')
        if not isinstance(description, str):
            raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-312-008')
        if not 0 < len(description.strip()) <= 512:
            raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-312-009')
        if is_foreign:
            foreign = row.get("foreignAmount")
            foreign_text = foreign.strip() if isinstance(foreign, str) else ""
            foreign_match = re.fullmatch(
                r"([+-]?(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d{1,2})?) ([A-Z]{3})",
                foreign_text,
            )
            try:
                foreign_value = (
                    Decimal(foreign_match.group(1).replace(",", ""))
                    if foreign_match else None
                )
            except InvalidOperation:
                foreign_value = None
            if foreign_match is None:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-313-001')
            if foreign_match.group(2) == 'TWD':
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-313-002')
            if foreign_value is None:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-313-003')
            if not foreign_value.is_finite():
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-313-004')
            if not 0 <= abs(foreign_value) <= Decimal('100000000'):
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-313-005')
            if foreign_value != 0 and foreign_match.group(1).startswith('-') != (row['isPositive'] is False):
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-313-006')
        return posted

    @classmethod
    def _fetch_posted_history(
        cls,
        page,
        *,
        card_id: str,
        identity: str,
        token: str,
        start: date,
        end: date,
        byte_budget: list[int] | None = None,
    ) -> dict:
        if re.fullmatch('[A-Za-z0-9_-]{1,128}', card_id) is None:
            raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-314-001')
        if not isinstance(identity, str):
            raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-314-002')
        if not identity.strip():
            raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-314-003')
        if not isinstance(token, str):
            raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-314-004')
        if re.fullmatch('Bearer [^\\s\\r\\n]+', token) is None:
            raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-314-005')
        if type(start) is not date:
            raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-314-006')
        if type(end) is not date:
            raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-314-007')
        if start > end:
            raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-314-008')

        base = f"https://card.hsbc.com.tw/ibk-bff/api/v1/cards/{card_id}/transactions/posted"
        page_size = 10
        total_pages = None
        selected_rows = []
        snapshots: list[dict[str, object]] = []
        snapshot_bytes = 0
        pages = 0
        budget = byte_budget if byte_budget is not None else [5_000_000]
        deadline = time.monotonic() + 300
        for page_number in range(500):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-315-001')
            if budget[0] <= 0:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-315-002')
            url = f"{base}?pageSize={page_size}&pageNumber={page_number}"
            diagnostics: list[str] = []
            response = cls._fetch_api_page(
                page,
                url=url,
                token=token,
                timeout_ms=min(30_000, max(1, int(remaining * 1000))),
                max_bytes=budget[0], diagnostics=diagnostics,
            )
            if time.monotonic() >= deadline:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-316-001')
            if not isinstance(response, dict):
                raise with_error_code(RuntimeError('hsbc-posted-history'), next(iter(diagnostics), 'HSBC-06-317-001'))
            if type(response.get('bytes')) is not int:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-317-002')
            if not 0 <= response['bytes'] <= budget[0]:
                raise with_error_code(RuntimeError('hsbc-posted-history'), next(iter(diagnostics), 'HSBC-06-317-003'))
            budget[0] -= response["bytes"]
            body = response.get("body")
            if response.get('url') != url:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-318-001')
            if response.get('status') != 200:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-318-002')
            if not _is_json_content_type(response.get('contentType')):
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-318-003')
            if response.get('redirected') is not False:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-318-004')
            if not isinstance(body, dict):
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-318-005')
            if body.get('success') is not True:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-318-006')
            if body.get('error') not in (None, '', []):
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-318-007')
            payload = body.get("payload")
            page_info = payload.get("pageInfo") if isinstance(payload, dict) else None
            rows = payload.get("content") if isinstance(payload, dict) else None
            current_total = page_info.get("totalPages") if isinstance(page_info, dict) else None
            if not isinstance(page_info, dict):
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-319-001')
            if type(page_info.get('currentPageIndex')) is not int:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-319-002')
            if page_info['currentPageIndex'] != page_number:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-319-003')
            if type(current_total) is not int:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-319-004')
            if not 0 <= current_total <= 500:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-319-005')
            if not isinstance(rows, list):
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-319-006')
            if len(rows) > page_size:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-319-007')
            if current_total == 0:
                if page_number != 0:
                    raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-319-008')
                if bool(rows):
                    raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-319-009')
            effective_total = max(1, current_total)
            if page_number + 1 < effective_total and len(rows) != page_size:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-320-001')
            if not rows and effective_total > 1:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-320-002')
            if total_pages is None:
                total_pages = effective_total
            elif effective_total != total_pages:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-321-001')
            snapshot = {"pageInfo": page_info, "content": rows}
            serialized_snapshot = json.dumps(
                snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            snapshot_bytes += len(serialized_snapshot.encode("utf-8"))
            if snapshot_bytes > 5_000_000:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-322-001')
            snapshots.append(snapshot)

            # The endpoint is unfiltered and has no proven date sort order:
            # retain every row and stop only at totalPages (within existing budgets).
            for row in rows:
                cls._validate_posted_row(row, end=end)
                selected_rows.append(row)
            pages += 1
            if page_number + 1 == total_pages:
                break
            with error_step('HSBC-06-323-001'):
                page.wait_for_timeout(400)
        else:
            raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-324-001')

        for page_number, snapshot in enumerate(snapshots):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-325-001')
            if budget[0] <= 0:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-325-002')
            url = f"{base}?pageSize={page_size}&pageNumber={page_number}"
            diagnostics = []
            replay = cls._attest_api_page(
                page,
                url=url,
                token=token,
                timeout_ms=min(30_000, max(1, int(remaining * 1000))),
                max_bytes=budget[0],
                expected_snapshot=snapshot, diagnostics=diagnostics,
            )
            if not isinstance(replay, dict):
                raise with_error_code(RuntimeError('hsbc-posted-history'), next(iter(diagnostics), 'HSBC-06-326-001'))
            if type(replay.get('bytes')) is not int:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-326-002')
            if not 0 <= replay['bytes'] <= budget[0]:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-326-003')
            budget[0] -= replay["bytes"]
            if time.monotonic() >= deadline:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-327-001')
            if replay.get('url') != url:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-327-002')
            if replay.get('status') != 200:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-327-003')
            if not _is_json_content_type(replay.get('contentType')):
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-327-004')
            if replay.get('redirected') is not False:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-327-005')
            if replay.get('matched') is not True:
                raise with_error_code(RuntimeError('hsbc-posted-history'), 'HSBC-06-327-006')
            if page_number + 1 < len(snapshots):
                with error_step('HSBC-06-328-001'):
                    page.wait_for_timeout(400)

        status = "complete" if selected_rows else "explicit_empty"
        return {
            "rows": selected_rows,
            "receipt": {
                "identity": identity,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "status": status,
                "pages": pages,
                "rows": len(selected_rows),
            },
        }
