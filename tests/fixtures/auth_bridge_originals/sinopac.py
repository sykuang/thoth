#!/usr/bin/env python3
"""SinoPac Bank MMA personal e-banking crawler.

永豐 SinoPac MMA 個人網銀抓取器。

登入入口：https://mma.sinopac.com/MemberPortal/Member/MMALogin.aspx（ASP.NET WebForms）
流程：開頁 → 關 cookie bar → 標記 4 個 input（按 maxLength 區分）→ fill 三欄
      → OCR 驗證碼（純數字 6 碼，長度錯換圖重 OCR，送出前安全重試）
      → 點登入鈕 #MMA_Login → 等跳轉。

⚠️ 登入重試規則：
   銀行明確回 `captcha_invalid` 時，換圖後只重送 1 次；
   `credentials_invalid` 或無法分類的錯誤一律立刻停手，避免鎖帳號。

第一輪 collect 只先 dump endpoint，摸清 API 地圖再補 parse（家規：dump 真值不猜測）。
預設行為：headless browser → 預設 headless=True。
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import html
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.base import (
    captcha_unreadable,
    _HistoryBodyObserver,
    _OriginGuardProxy,
    BankCollectResult,
    BankCrawler,
    ResponseCollector,
    validate_history_coverage,
    write_private_json,
)
from backend.core.card_bills import (
    card_bill_date,
    card_bill_money,
    make_card_bill_fact,
    publish_card_bill_facts,
)
from backend.core.captcha import solve_captcha, wait_captcha_stable
from backend.core.error_codes import error_step, exception_error_id, with_error_code
from backend.core.creds import SinopacCreds
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointPhase,
    LoginCheckpointRule,
)

BASE = "https://mma.sinopac.com/MemberPortal/Member/MMALogin.aspx"


def _taipei_today() -> date:
    return datetime.now(ZoneInfo("Asia/Taipei")).date()


def _plain_text(value: str) -> str:
    with error_step('SINOPAC-07-344-001'):
        return html.unescape(re.sub(r"<[^>]*>", "", value)).replace("\xa0", " ").strip()


LOAN_DETAIL_URL = "https://mma.sinopac.com/mma/bank/easy_index_loan/mma_detail.aspx"
LOAN_REPAYMENT_PATH = "/ws/bank/loan/ws_loandetail.ashx"


class _LoanRepaymentBodyObserver(_HistoryBodyObserver):
    """Reuse bounded, non-replaying CDP reads for the native cache-busted URL."""

    def _request(self, event):
        with error_step('SINOPAC-05-458-001'):
            url = event.get("request", {}).get("url", "")
        with error_step('SINOPAC-05-459-001'):
            parsed = urlparse(url)
        with error_step('SINOPAC-05-460-001'):
            if parsed.path != LOAN_REPAYMENT_PATH:
                return
        with error_step('SINOPAC-05-462-001'):
            if (
                parsed.scheme != "https" or parsed.netloc != "mma.sinopac.com"
                or parsed.params or parsed.fragment
                or (parsed.query and re.fullmatch(r"[0-9]{10,16}", parsed.query) is None)
                or self.records
            ):
                with error_step('SINOPAC-05-461-001'):
                    self.bad = True
                return
        with error_step('SINOPAC-05-463-001'):
            self.url = url
        with error_step('SINOPAC-05-464-001'):
            super()._request(event)

    def read(self, response, *args, **kwargs):
        # Event callbacks receive raw objects; preserve exact frame/request identity
        # by joining the guarded page's existing cache, not comparing URLs instead.
        with error_step('SINOPAC-05-466-001'):
            if isinstance(self.page, _OriginGuardProxy):
                with error_step('SINOPAC-05-465-001'):
                    response = self.page._wrap(response)
        with error_step('SINOPAC-05-467-001'):
            return super().read(response, *args, **kwargs)


SEL_CAP_IMG = "#imgCode"


def _log(*a):
    print(*a, file=sys.stderr)


class SinopacLoginError(RuntimeError):
    """永豐登入失敗，附可供 retry/UI 判斷的 machine-readable code。"""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"[{code}] {message}")


def _sinopac_card_bill_fact(out: dict):
    kv = {}
    with error_step('SINOPAC-07-302-001'):
        summary_rows = out.get("card_summary") or []
    with error_step('SINOPAC-07-308-001'):
        if summary_rows and isinstance(summary_rows[0], dict):
            with error_step('SINOPAC-07-307-001'):
                for group in summary_rows[0].get("SubInfo") or []:
                    with error_step('SINOPAC-07-306-001'):
                        if isinstance(group, list):
                            with error_step('SINOPAC-07-305-001'):
                                for row in group:
                                    with error_step('SINOPAC-07-304-001'):
                                        if isinstance(row, dict) and row.get("DataText"):
                                            with error_step('SINOPAC-07-303-001'):
                                                kv[row["DataText"]] = row.get("DataValue")
    with error_step('SINOPAC-07-309-001'):
        statements = out.get("card_statements") or []
    with error_step('SINOPAC-07-310-001'):
        latest = statements[0] if statements and isinstance(statements[0], dict) else {}
    with error_step('SINOPAC-07-311-001'):
        latest_summary = latest.get("summary") if isinstance(latest, dict) else {}
    with error_step('SINOPAC-07-312-001'):
        remaining = kv.get("本期應繳")
    with error_step('SINOPAC-07-314-001'):
        if remaining is None and isinstance(latest_summary, dict):
            with error_step('SINOPAC-07-313-001'):
                remaining = latest_summary.get("current_due")

    with error_step('SINOPAC-07-315-001'):
        payment_amount = kv.get("最近繳款金額")
    with error_step('SINOPAC-07-316-001'):
        payment_date = kv.get("最近繳款日期")
    with error_step('SINOPAC-07-317-001'):
        if card_bill_money(payment_amount) is None or card_bill_date(payment_date) is None:
            payment_amount = None
            payment_date = None
    with error_step('SINOPAC-07-318-001'):
        return make_card_bill_fact(
            remaining_due=remaining,
            statement_close_date=kv.get("結帳日") or latest.get("billing_cycle_date"),
            payment_due_date=kv.get("繳款截止日") or latest.get("payment_due_date"),
            last_payment_amount=payment_amount,
            last_payment_date=payment_date,
        )


def _sinopac_hidden_changed_reason(state: dict) -> str:
    with error_step('SINOPAC-07-345-001'):
        if state.get("hiddenChangedFullTransactionRows", 0) > 0:
            return "empty-hidden-full-transaction"
    with error_step('SINOPAC-07-346-001'):
        if state.get("hiddenChangedDateRows", 0) > 0:
            return "empty-hidden-date-cell"
    with error_step('SINOPAC-07-347-001'):
        if state.get("hiddenChangedNumericRows", 0) > 0:
            return "empty-hidden-numeric-cell"
    with error_step('SINOPAC-07-348-001'):
        if state.get("hiddenChangedControlRows", 0) > 0:
            return "empty-hidden-control"
    with error_step('SINOPAC-07-349-001'):
        if state.get("hiddenChangedHeaderRows") == state.get("hiddenChangedRows"):
            return "empty-hidden-header"
    return "empty-hidden-template-text"


class SinopacCrawler(BankCrawler):
    USES_SHARED_LOGIN_CHECKPOINTS: ClassVar[bool] = True
    SAFE_COLLECT_GUARDS = frozenset({
        "sinopac-loan-inventory-envelope",
        "sinopac-loan-inventory-row",
        "sinopac-loan-inventory-identity",
        "sinopac-loan-query-control",
        "sinopac-loan-response-missing",
        "sinopac-loan-response-http",
        "sinopac-loan-response-envelope",
        "sinopac-loan-response-records",
        "sinopac-loan-restore-page",
        "sinopac-loan-restore-account",
        "sinopac-loan-repayments",
        "sinopac-loan-repayments-page-state",
        "sinopac-loan-repayments-account-control",
        "sinopac-loan-repayments-link-cardinality",
        "sinopac-loan-repayments-form-control",
        "sinopac-loan-repayments-start-date",
        "sinopac-loan-repayments-end-date",
        "sinopac-loan-repayments-text-type",
        "sinopac-loan-repayments-form-binding",
        "sinopac-loan-repayments-query-control",
        "sinopac-loan-repayments-query-label",
        "sinopac-loan-repayments-response-cardinality",
        "sinopac-loan-repayments-request-binding",
        "sinopac-loan-repayments-response-type",
        "sinopac-loan-repayments-response-body",
        "sinopac-loan-repayments-response-envelope",
        "sinopac-loan-repayments-response-metadata",
        "sinopac-loan-repayments-response-records",
        "sinopac-loan-repayments-result-state",
        "sinopac-loan-repayments-result-rows",
        "sinopac-twd-history-account-control",
        "sinopac-twd-history-byte-budget",
        "sinopac-twd-history-cursor",
        "sinopac-twd-history-deadline",
        "sinopac-twd-history-dialog",
        "sinopac-twd-history-empty-inventory-blocked",
        "sinopac-twd-history-inventory",
        "sinopac-twd-history-inventory-cardinality",
        "sinopac-twd-history-inventory-envelope",
        "sinopac-twd-history-inventory-identity",
        "sinopac-twd-history-inventory-row",
        "sinopac-twd-history-mode",
        "sinopac-twd-history-operation-cardinality",
        "sinopac-twd-history-query-control",
        "sinopac-twd-history-range",
        "sinopac-twd-history-response",
        "sinopac-twd-history-response-cardinality",
        "sinopac-twd-history-result-table",
        "sinopac-twd-history-result-table-binding",
        "sinopac-twd-history-result-table-empty-marker",
        "sinopac-twd-history-result-table-empty-multiple-visible-rows",
        "sinopac-twd-history-result-table-empty-no-row",
        "sinopac-twd-history-result-table-empty-binding",
        "sinopac-twd-history-result-table-empty-extra-hidden-rows",
        "sinopac-twd-history-result-table-empty-hidden-data",
        "sinopac-twd-history-result-table-empty-hidden-header",
        "sinopac-twd-history-result-table-empty-hidden-template-text",
        "sinopac-twd-history-result-table-empty-hidden-transaction",
        "sinopac-twd-history-result-table-empty-hidden-full-transaction",
        "sinopac-twd-history-result-table-empty-hidden-date-cell",
        "sinopac-twd-history-result-table-empty-hidden-numeric-cell",
        "sinopac-twd-history-result-table-empty-hidden-control",
        "sinopac-twd-history-result-table-empty-row-count",
        "sinopac-twd-history-result-table-empty-row-visibility",
        "sinopac-twd-history-result-table-empty-signature",
        "sinopac-twd-history-result-table-error",
        "sinopac-twd-history-result-table-mutation-range",
        "sinopac-twd-history-result-table-mutation-shape",
        "sinopac-twd-history-result-table-nonempty-attestation",
        "sinopac-twd-history-result-table-nonempty-binding",
        "sinopac-twd-history-result-table-pager",
        "sinopac-twd-history-result-table-shape",
        "sinopac-twd-history-result-table-table-count",
        "sinopac-twd-history-result-table-table-visibility",
        "sinopac-twd-history-result-table-unknown",
        "sinopac-twd-history-row",
        "sinopac-twd-history-row-budget",
    })
    CREDENTIAL_HOSTS = frozenset({"mma.sinopac.com"})
    CAPTCHA_INVALID = "captcha_invalid"
    CREDENTIALS_INVALID = "credentials_invalid"
    LOGIN_FAILED = "login_failed"
    HISTORY_COVERAGE_REQUIRED: ClassVar[bool] = True
    HISTORY_COVERAGE_DOMAINS: ClassVar[frozenset[str]] = frozenset({
        "account_transactions",
    })

    _TWD_INVENTORY_PATH = "/ws/bank/transdetail/ws_debitacct.ashx"
    _TWD_HISTORY_PATH = "/ws/bank/transdetail/ws_transdetailMerge.ashx"
    _HISTORY_RESPONSE_KEYS = frozenset({
        "BeginDate", "DefBeginDate", "DefEndDate", "EndDate", "HeadInfo",
        "Header", "MaxMonth", "Message", "RecordCount", "SubInfo", "isOBU",
    })
    _HISTORY_ROW_KEYS = frozenset(f"DataText{i}" for i in range(1, 12))
    _HISTORY_FORM_KEYS = frozenset({
        "Acct", "AcctName", "AcctValue", "BusinessDate", "Curr", "CurrName",
        "EndDate", "QueryType", "StartDate", "TextType",
    })

    def __init__(self):
        super().__init__(name="sinopac")
        self.creds = SinopacCreds.load()

    def _host_filter(self) -> str:
        return "sinopac.com"

    @staticmethod
    def _page_scopes(page):
        return [
            page,
            *(frame for frame in page.frames if frame is not page.main_frame),
        ]

    def _logged_in(self, page) -> bool:
        try:
            with error_step('SINOPAC-03-200-001'):
                current = urlparse(page.url or "")
            with error_step('SINOPAC-03-201-001'):
                path = (current.path or "").lower()
            with error_step('SINOPAC-03-202-001'):
                if (
                    not self._exact_https_origin_allowed(
                        page.url, frozenset({"mma.sinopac.com"})
                    )
                    or "mmalogin.aspx" in path
                    or not path.startswith(
                        ("/mymma/", "/myasset/", "/mma_", "/mma/mymma/",
                         "/mma/bank/easy_index_loan/")
                    )
                ):
                    return False
            with error_step('SINOPAC-03-209-001'):
                for scope in self._page_scopes(page):
                    with error_step('SINOPAC-03-203-001'):
                        captcha_images = scope.locator(SEL_CAP_IMG)
                    with error_step('SINOPAC-03-204-001'):
                        if any(
                            error_step('SINOPAC-03-204-201')(lambda: error_step('SINOPAC-03-204-202')(lambda: captcha_images.nth(index))().is_visible())()
                            for index in range(error_step('SINOPAC-03-204-203')(lambda: captcha_images.count())())
                        ):
                            return False
                    with error_step('SINOPAC-03-205-001'):
                        inputs = scope.locator("input")
                    with error_step('SINOPAC-03-208-001'):
                        for index in range(inputs.count()):
                            with error_step('SINOPAC-03-206-001'):
                                field = inputs.nth(index)
                            with error_step('SINOPAC-03-207-001'):
                                if (
                                    error_step('SINOPAC-03-207-201')(lambda: field.is_visible())()
                                    and error_step('SINOPAC-03-207-202')(lambda: field.get_attribute("maxlength"))() in {"6", "11", "20"}
                                ):
                                    return False
            with error_step('SINOPAC-03-210-001'):
                body = error_step('SINOPAC-03-210-201')(lambda: error_step('SINOPAC-03-210-202')(lambda: page.locator("body"))().inner_text())()
        except Exception:
            return False
        with error_step('SINOPAC-03-211-001'):
            return (
                len(body) >= 500
                and "登出" in body
                and ("資產總覽" in body or "資產分析" in body or "我的帳戶" in body)
            )

    def login(self, page) -> bool:
        return self._shared_login(page)

    def prepare_login_page(self, page) -> None:
        try:
            with error_step('SINOPAC-01-200-001'):
                page.wait_for_timeout(8000)
        except Exception as error:
            raise with_error_code(SinopacLoginError(
                self.LOGIN_FAILED,
                "永豐登入頁面無法安全準備；未送出登入",
            ), exception_error_id(error, bank="sinopac") or 'SINOPAC-01-201-001') from None

    def is_authenticated(self, page) -> bool:
        return self._logged_in(page)

    def login_checkpoint_rules(self) -> tuple[LoginCheckpointRule, ...]:
        all_phases = tuple(CheckpointPhase)
        post_settle = (
            CheckpointPhase.POST_SUBMIT,
            CheckpointPhase.POST_SUBMIT_SETTLE,
        )
        otp = re.compile(
            r"^[\s\S]{0,300}(?:(?<![A-Za-z])OTP(?![A-Za-z])|一次性(?:密碼|驗證碼)|"
            r"簡訊驗證碼|動態驗證碼|裝置驗證|新裝置登入|信任此裝置)[\s\S]{0,300}$",
            re.IGNORECASE,
        )
        password = re.compile(
            r"^[\s\S]{0,200}(?:(?<!驗證)密碼\s*(?:已)?(?:到期|過期)|"
            r"(?:立即|必須|請先|需要|需)\s*(?:修改|變更|重設)\s*(?:您的?)?\s*密碼|"
            r"強制\s*(?:修改|變更|重設)\s*(?:您的?)?\s*密碼)[\s\S]{0,200}$"
        )
        credential_error = re.compile(
            r"^\s*(?:使用者代碼或網路密碼錯誤|帳號或密碼錯誤|密碼不正確|"
            r"密碼無效|身分證字號錯誤)\s*[。.!！?？]?\s*$"
        )
        captcha_error = re.compile(
            r"^\s*(?:(?:驗證碼失效|驗證碼錯誤|驗證碼輸入錯誤|"
            r"請重新輸入驗證碼)\s*[。.!！?？]?|"
            r"驗證碼失效或輸入錯誤，請重新輸入。)\s*$"
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
                    name=f"sinopac-otp-required-{suffix}",
                    bank="sinopac",
                    phases=all_phases,
                    kind=CheckpointKind.OTP_REQUIRED,
                    container_selector=selector,
                    required_body_pattern=otp,
                )
                for suffix, selector in modal_scopes
            ),
            *(
                LoginCheckpointRule(
                    name=f"sinopac-password-change-required-{suffix}",
                    bank="sinopac",
                    phases=all_phases,
                    kind=CheckpointKind.PASSWORD_CHANGE_REQUIRED,
                    container_selector=selector,
                    required_body_pattern=password,
                )
                for suffix, selector in modal_scopes
            ),
            *(
                LoginCheckpointRule(
                    name=f"sinopac-explicit-login-error-{suffix}",
                    bank="sinopac",
                    phases=post_settle,
                    kind=CheckpointKind.EXPLICIT_LOGIN_ERROR,
                    container_selector=selector,
                    required_body_pattern=credential_error,
                )
                for suffix, selector in alert_scopes
            ),
            *(
                LoginCheckpointRule(
                    name=f"sinopac-captcha-retry-{suffix}",
                    bank="sinopac",
                    phases=(CheckpointPhase.POST_SUBMIT,),
                    kind=CheckpointKind.CAPTCHA_RETRY,
                    container_selector=selector,
                    required_body_pattern=captcha_error,
                )
                for suffix, selector in alert_scopes
            ),
            *(
                LoginCheckpointRule(
                    name=f"sinopac-unknown-{suffix}",
                    bank="sinopac",
                    phases=all_phases,
                    kind=CheckpointKind.UNKNOWN_BLOCKER,
                    container_selector=selector,
                )
                for suffix, selector in modal_scopes
            ),
            LoginCheckpointRule(
                name="sinopac-login-form-still-visible",
                bank="sinopac",
                phases=post_settle,
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector=SEL_CAP_IMG,
            ),
        )

    @staticmethod
    def _captcha_image(page, *, enabled: bool = False, _diagnostic=None, _error_ids=()):
        if _diagnostic is not None:
            _diagnostic.clear()
        with error_step('SINOPAC-02-243-001'):
            images = page.locator(SEL_CAP_IMG)
        with error_step('SINOPAC-02-244-001'):
            visible = [
                error_step('SINOPAC-02-244-101')(lambda: images.nth(index))()
                for index in range(error_step('SINOPAC-02-244-102')(lambda: images.count())())
                if error_step('SINOPAC-02-244-103')(lambda: error_step('SINOPAC-02-244-104')(lambda: images.nth(index))().is_visible())()
                and (not enabled or error_step('SINOPAC-02-244-105')(lambda: error_step('SINOPAC-02-244-106')(lambda: images.nth(index))().is_enabled())())
            ]
        with error_step('SINOPAC-02-245-001'):
            visible_count = len(visible)
            if visible_count == 1:
                return visible[0]
            if _diagnostic is not None:
                _diagnostic[:] = [_error_ids[0 if visible_count == 0 else 1]]
            return None

    @staticmethod
    def _keyboard_fill(page, field, value: str, *, error_action="SINOPAC-02-140") -> None:
        with error_step(f"{error_action}-001"):
            field.click()
        with error_step(f"{error_action}-002"):
            field.click(click_count=3)
        with error_step(f"{error_action}-003"):
            page.keyboard.press("Backspace")
        with error_step(f"{error_action}-004"):
            page.keyboard.type(value, delay=80)
        with error_step(f"{error_action}-005"):
            entered_length = len(field.input_value())
        if entered_length != len(value):
            raise with_error_code(SinopacLoginError(
                SinopacCrawler.LOGIN_FAILED,
                "永豐登入欄位輸入長度不符；未送出登入",
            ), f"{error_action}-006")

    def prepare_captcha_resubmit(self, page) -> None:
        try:
            with error_step('SINOPAC-02-200-001'):
                image_reason = []
                image = self._captcha_image(page, enabled=True, _diagnostic=image_reason, _error_ids=('SINOPAC-02-781-001', 'SINOPAC-02-781-002'))
            with error_step('SINOPAC-02-201-001'):
                if image is None:
                    raise with_error_code(SinopacLoginError(
                        self.CAPTCHA_INVALID,
                        "無法安全更新永豐驗證碼；未送出登入",
                    ), image_reason[0] if image_reason else 'SINOPAC-02-201-001')
            with error_step('SINOPAC-02-202-001'):
                image.click()
            with error_step('SINOPAC-02-203-001'):
                page.wait_for_timeout(1500)
        except SinopacLoginError:
            raise
        except Exception as error:
            raise with_error_code(SinopacLoginError(
                self.CAPTCHA_INVALID,
                "無法安全更新永豐驗證碼；未送出登入",
            ), exception_error_id(error, bank="sinopac") or 'SINOPAC-02-204-001') from None

    def _ocr_captcha(self, page, max_attempts=5, *, _diagnostic=None, _required=False):
        # Per-invocation fixed codes only; never retain an exception or OCR text.
        if _diagnostic is not None:
            _diagnostic.clear()
        with error_step('SINOPAC-02-246-001'):
            attempts = min(max(max_attempts, 1), 5)
        with error_step('SINOPAC-02-255-001'):
            for attempt in range(attempts):
                if _diagnostic is not None:
                    _diagnostic.clear()
                try:
                    with error_step('SINOPAC-02-247-001'):
                        if self._captcha_image(page, _diagnostic=_diagnostic if _required else None, _error_ids=('SINOPAC-02-780-001', 'SINOPAC-02-780-002')) is None:
                            return None
                    with error_step('SINOPAC-02-248-001'):
                        wait_captcha_stable(page, SEL_CAP_IMG, tmp_path=self.captcha_tmp)
                    with error_step('SINOPAC-02-249-001'):
                        text = solve_captcha(
                            page,
                            SEL_CAP_IMG,
                            expected_len=6,
                            alnum_only=True,
                            digits_only=True,
                            min_confidence=0.98,
                            tmp_path=self.captcha_tmp,
                        )
                    with error_step('SINOPAC-02-250-001'):
                        if isinstance(text, str) and len(text) == 6 and text.isdigit():
                            if _diagnostic is not None:
                                _diagnostic.clear()
                            return text
                except Exception as error:
                    if _diagnostic is not None:
                        reason = exception_error_id(error, bank="sinopac")
                        _diagnostic[:] = [reason] if reason else []
                    pass
                with error_step('SINOPAC-02-254-001'):
                    if attempt + 1 < attempts:
                        try:
                            with error_step('SINOPAC-02-251-001'):
                                image = self._captcha_image(page, enabled=True)
                            if image is None:
                                return None
                            with error_step('SINOPAC-02-252-001'):
                                image.click()
                            with error_step('SINOPAC-02-253-001'):
                                page.wait_for_timeout(1500)
                        except Exception as error:
                            if _diagnostic is not None:
                                reason = exception_error_id(error, bank="sinopac")
                                _diagnostic[:] = [reason] if reason else []
                            return None
        return None

    @classmethod
    def _response_visible(cls, page) -> bool:
        with error_step('SINOPAC-03-215-001'):
            for scope in cls._page_scopes(page):
                with error_step('SINOPAC-03-214-001'):
                    for selector in (
                        ".modal.show",
                        "[role='dialog']",
                        ".error",
                        ".alert",
                        "[role='alert']",
                    ):
                        with error_step('SINOPAC-03-212-001'):
                            matches = scope.locator(selector)
                        with error_step('SINOPAC-03-213-001'):
                            if any(
                                error_step('SINOPAC-03-213-201')(lambda: error_step('SINOPAC-03-213-202')(lambda: matches.nth(index))().is_visible())()
                                for index in range(error_step('SINOPAC-03-213-203')(lambda: matches.count())())
                            ):
                                return True
        return False

    def submit_credentials_once(self, page) -> None:
        try:
            with error_step('SINOPAC-02-205-001'):
                page.wait_for_selector(SEL_CAP_IMG, state="visible", timeout=10000)
            with error_step('SINOPAC-02-206-001'):
                inputs = page.locator("input")
            groups = {6: [], 11: [], 20: []}
            with error_step('SINOPAC-02-212-001'):
                for index in range(inputs.count()):
                    with error_step('SINOPAC-02-207-001'):
                        field = inputs.nth(index)
                    with error_step('SINOPAC-02-208-001'):
                        if not field.is_visible():
                            continue
                    with error_step('SINOPAC-02-209-001'):
                        maxlength = field.get_attribute("maxlength")
                    with error_step('SINOPAC-02-211-001'):
                        if maxlength in {"6", "11", "20"}:
                            with error_step('SINOPAC-02-210-001'):
                                groups[int(maxlength)].append(field)
            with error_step('SINOPAC-02-213-001'):
                group_counts = tuple((len(groups[length]) for length in (11, 20, 6)))
                if group_counts != (1, 2, 1):
                    raise with_error_code(SinopacLoginError(
                        self.LOGIN_FAILED,
                        "永豐登入欄位無法安全確認；未送出登入",
                    ), ('SINOPAC-02-213-101' if group_counts[0] != 1 else 'SINOPAC-02-213-102' if group_counts[1] != 2 else 'SINOPAC-02-213-103'))
            ordered_twenty = []
            with error_step('SINOPAC-02-217-001'):
                for field in groups[20]:
                    with error_step('SINOPAC-02-214-001'):
                        box = field.bounding_box()
                    with error_step('SINOPAC-02-215-001'):
                        if box is None:
                            raise with_error_code(SinopacLoginError(
                                self.LOGIN_FAILED,
                                "永豐登入欄位無法安全確認；未送出登入",
                            ), 'SINOPAC-02-215-001')
                    with error_step('SINOPAC-02-216-001'):
                        ordered_twenty.append((box["y"], field))
            with error_step('SINOPAC-02-218-001'):
                ordered_twenty.sort(key=lambda item: item[0])
            with error_step('SINOPAC-02-219-001'):
                if ordered_twenty[0][0] == ordered_twenty[1][0]:
                    raise with_error_code(SinopacLoginError(
                        self.LOGIN_FAILED,
                        "永豐登入欄位無法安全確認；未送出登入",
                    ), 'SINOPAC-02-219-001')
            with error_step('SINOPAC-02-220-001'):
                fields = (
                    groups[11][0],
                    ordered_twenty[0][1],
                    ordered_twenty[1][1],
                    groups[6][0],
                )
            with error_step('SINOPAC-02-221-001'):
                for field, error_action in zip(fields, ('SINOPAC-02-100', 'SINOPAC-02-110', 'SINOPAC-02-120', 'SINOPAC-02-130')):
                    with error_step(f'{error_action}-101'):
                        if not field.is_enabled():
                            raise with_error_code(SinopacLoginError(
                                self.LOGIN_FAILED,
                                "永豐登入欄位無法安全確認；未送出登入",
                            ), f'{error_action}-102')
            with error_step('SINOPAC-02-223-001'):
                for field, value, error_action in zip(
                    fields[:3],
                    (self.creds.national_id, self.creds.user_code, self.creds.password),
                    ("SINOPAC-02-100", "SINOPAC-02-110", "SINOPAC-02-120"),
                    strict=True,
                ):
                    with error_step('SINOPAC-02-222-001'):
                        self._keyboard_fill(page, field, value, error_action=error_action)
            with error_step('SINOPAC-02-224-001'):
                ocr_reason = []
                captcha = self._ocr_captcha(page, max_attempts=1, _diagnostic=ocr_reason, _required=True)
            with error_step('SINOPAC-02-225-001'):
                if captcha is None:
                    raise with_error_code(captcha_unreadable(SinopacLoginError(
                        self.CAPTCHA_INVALID,
                        "永豐驗證碼辨識失敗；未送出登入",
                    )), ocr_reason[0] if ocr_reason else 'SINOPAC-02-225-001')
            with error_step('SINOPAC-02-226-001'):
                self._keyboard_fill(page, fields[3], captcha, error_action="SINOPAC-02-130")

            with error_step('SINOPAC-02-227-001'):
                candidates = page.locator("#MMA_Login")
            eligible = []
            with error_step('SINOPAC-02-233-001'):
                for index in range(candidates.count()):
                    with error_step('SINOPAC-02-228-001'):
                        candidate = candidates.nth(index)
                    with error_step('SINOPAC-02-229-001'):
                        if not error_step('SINOPAC-02-229-201')(lambda: candidate.is_visible())() or not error_step('SINOPAC-02-229-202')(lambda: candidate.is_enabled())():
                            continue
                    with error_step('SINOPAC-02-230-001'):
                        label = " ".join(
                            ((error_step('SINOPAC-02-230-201')(lambda: candidate.inner_text())() or error_step('SINOPAC-02-230-202')(lambda: candidate.get_attribute("value"))() or "")).split()
                        )
                    with error_step('SINOPAC-02-232-001'):
                        if label == "登入":
                            with error_step('SINOPAC-02-231-001'):
                                eligible.append(candidate)
            with error_step('SINOPAC-02-234-001'):
                if len(eligible) != 1:
                    raise with_error_code(SinopacLoginError(
                        self.LOGIN_FAILED,
                        "找不到唯一且可操作的永豐登入按鈕；未送出登入",
                    ), 'SINOPAC-02-234-001')
            with error_step('SINOPAC-02-235-001'):
                button = eligible[0]
        except SinopacLoginError:
            raise
        except Exception as error:
            raise with_error_code(SinopacLoginError(
                self.LOGIN_FAILED,
                "永豐登入欄位無法安全填寫；未送出登入",
            ), exception_error_id(error, bank="sinopac") or 'SINOPAC-02-236-001') from None

        try:
            with error_step('SINOPAC-02-237-001'):
                button.click(timeout=8000)
        except Exception as error:
            raise with_error_code(SinopacLoginError(
                self.LOGIN_FAILED,
                "永豐登入送出狀態不明；禁止自動重試",
            ), exception_error_id(error, bank="sinopac") or 'SINOPAC-02-238-001') from None

        try:
            with error_step('SINOPAC-03-216-001'):
                for _ in range(8):
                    with error_step('SINOPAC-03-218-001'):
                        page.wait_for_timeout(1000)
                    with error_step('SINOPAC-03-219-001'):
                        if self._logged_in(page) or self._response_visible(page):
                            return
        except Exception as error:
            raise with_error_code(SinopacLoginError(
                self.LOGIN_FAILED,
                "永豐登入送出後狀態無法安全確認；禁止自動重試",
            ), exception_error_id(error, bank="sinopac") or 'SINOPAC-03-217-001') from None

    # ---------- 抓取 ----------
    @staticmethod
    def _history_floor(end: date) -> date:
        try:
            return end.replace(year=end.year - 1) + timedelta(days=1)
        except ValueError:
            return end.replace(year=end.year - 1, day=28) + timedelta(days=1)

    @staticmethod
    def _history_windows(start: date, end: date) -> list[tuple[date, date]]:
        with error_step('SINOPAC-05-468-001'):
            if start > end:
                raise with_error_code(RuntimeError("sinopac-twd-history-range"), 'SINOPAC-05-468-001')
        windows = []
        cursor = start
        with error_step('SINOPAC-05-472-001'):
            while cursor <= end:
                with error_step('SINOPAC-05-469-001'):
                    window_end = min(end, date(
                        cursor.year, cursor.month, monthrange(cursor.year, cursor.month)[1],
                    ))
                with error_step('SINOPAC-05-470-001'):
                    windows.append((cursor, window_end))
                with error_step('SINOPAC-05-471-001'):
                    cursor = window_end + timedelta(days=1)
        return windows

    @staticmethod
    def _history_identity(identity: str, currency: str) -> str:
        return identity if currency == "TWD" else f"{identity}:{currency}"

    def _history_range(
        self, identity: str, *, end: date, mode: str,
    ) -> tuple[date, date]:
        with error_step('SINOPAC-05-473-001'):
            floor = self._history_floor(end)
        with error_step('SINOPAC-05-474-001'):
            cursor = self.transaction_cursors.get("account_transactions", {}).get(identity)
        with error_step('SINOPAC-05-475-001'):
            if isinstance(cursor, date) and cursor > end:
                raise with_error_code(RuntimeError("sinopac-twd-history-cursor"), 'SINOPAC-05-475-001')
        if mode == "full":
            return floor, end
        with error_step('SINOPAC-05-476-001'):
            if mode != 'incremental':
                raise with_error_code(RuntimeError("sinopac-twd-history-mode"), 'SINOPAC-05-476-001')
        with error_step('SINOPAC-05-477-001'):
            start = max(floor, cursor - timedelta(days=7)) if isinstance(cursor, date) else floor
        return start, end

    @staticmethod
    def _exact_hit_url(hit, path: str, *, numeric_query: bool, _diagnostic=None, _error_ids=()) -> bool:
        if _diagnostic is not None:
            _diagnostic.clear()
        parsed = urlparse(hit.url)
        raw = urlparse(hit.raw_url or hit.url)
        for index, predicate in enumerate((
            lambda: parsed.scheme == raw.scheme == "https",
            lambda: parsed.hostname == raw.hostname == "mma.sinopac.com",
            lambda: parsed.port in (None, 443),
            lambda: raw.port in (None, 443),
            lambda: parsed.username is parsed.password is raw.username is raw.password is None,
            lambda: parsed.path == raw.path == path,
            lambda: parsed.params == raw.params == "",
            lambda: parsed.fragment == raw.fragment == "",
            lambda: parsed.query == "",
            lambda: not numeric_query or re.fullmatch(r"\d{10,16}", raw.query or "") is not None,
        )):
            if not predicate():
                if _diagnostic is not None:
                    _diagnostic[:] = [_error_ids[index]]
                return False
        return True

    @classmethod
    def _twd_inventory(
        cls, collector: ResponseCollector, *, after_sequence: int = 0,
    ) -> list[dict]:
        with error_step('SINOPAC-04-200-001'):
            candidates = [
                candidate for candidate in collector.by_endpoint("ws_debitacct.ashx")
                if type(candidate.request_sequence) is int
                and candidate.request_sequence > after_sequence
            ]
        with error_step('SINOPAC-04-201-001'):
            if len(candidates) != 1:
                raise with_error_code(RuntimeError("sinopac-twd-history-inventory-cardinality"), 'SINOPAC-04-201-001')
        with error_step('SINOPAC-04-202-001'):
            hit = candidates[0]
        with error_step('SINOPAC-04-211-001'):
            if hit.req_body not in (None, ""):
                error = "sinopac-twd-history-inventory-envelope"
                try:
                    with error_step('SINOPAC-04-203-001'):
                        form = (
                            parse_qs(
                                hit.req_body,
                                keep_blank_values=True,
                                strict_parsing=True,
                                max_num_fields=len(cls._HISTORY_FORM_KEYS),
                            )
                            if isinstance(hit.req_body, str)
                            else {}
                        )
                except ValueError as source_error:
                    raise with_error_code(RuntimeError(error), exception_error_id(source_error, bank="sinopac") or 'SINOPAC-04-204-001') from None
                date_keys = {"BusinessDate", "StartDate", "EndDate"}
                with error_step('SINOPAC-04-205-001'):
                    if set(form) != cls._HISTORY_FORM_KEYS:
                        raise with_error_code(RuntimeError(error), 'SINOPAC-04-205-001')
                with error_step('SINOPAC-04-206-001'):
                    for key in cls._HISTORY_FORM_KEYS - date_keys:
                        error_id = {
                            'Acct': 'SINOPAC-04-206-101',
                            'AcctName': 'SINOPAC-04-206-102',
                            'AcctValue': 'SINOPAC-04-206-103',
                            'Curr': 'SINOPAC-04-206-104',
                            'CurrName': 'SINOPAC-04-206-105',
                            'QueryType': 'SINOPAC-04-206-106',
                            'TextType': 'SINOPAC-04-206-107',
                        }[key]
                        if form[key] != ['']:
                            raise with_error_code(RuntimeError(error), error_id)
                with error_step('SINOPAC-04-207-001'):
                    if any((len(form[key]) != 1 for key in date_keys)):
                        raise with_error_code(RuntimeError(error), 'SINOPAC-04-207-001')
                with error_step('SINOPAC-04-208-001'):
                    dates = {key: cls._yyyymmdd(form[key][0], error, error_action={"BusinessDate": "SINOPAC-04-151", "StartDate": "SINOPAC-04-152", "EndDate": "SINOPAC-04-153"}[key]) for key in date_keys}
                with error_step('SINOPAC-04-209-001'):
                    if dates['BusinessDate'] != dates['EndDate']:
                        raise with_error_code(RuntimeError(error), 'SINOPAC-04-209-001')
                with error_step('SINOPAC-04-210-001'):
                    if not 0 <= (dates['EndDate'] - dates['StartDate']).days <= 31:
                        raise with_error_code(RuntimeError(error), 'SINOPAC-04-210-001')
        with error_step('SINOPAC-04-212-001'):
            payload = hit.resp_json if hit else None
        with error_step('SINOPAC-04-213-001'):
            body = payload[0] if isinstance(payload, list) and len(payload) == 1 else None
        with error_step('SINOPAC-04-214-001'):
            rows = body.get("SubInfo") if isinstance(body, dict) else None
        with error_step('SINOPAC-04-215-001'):
            if hit is None:
                raise with_error_code(RuntimeError("sinopac-twd-history-inventory-envelope"), 'SINOPAC-04-215-001')
        with error_step('SINOPAC-04-216-001'):
            if type(hit.request_sequence) is not int:
                raise with_error_code(RuntimeError("sinopac-twd-history-inventory-envelope"), 'SINOPAC-04-216-001')
        with error_step('SINOPAC-04-217-001'):
            if hit.request_sequence <= after_sequence:
                raise with_error_code(RuntimeError("sinopac-twd-history-inventory-envelope"), 'SINOPAC-04-217-001')
        with error_step('SINOPAC-04-218-001'):
            if hit.main_frame_request is not True:
                raise with_error_code(RuntimeError("sinopac-twd-history-inventory-envelope"), 'SINOPAC-04-218-001')
        with error_step('SINOPAC-04-219-001'):
            if hit.method != 'POST':
                raise with_error_code(RuntimeError("sinopac-twd-history-inventory-envelope"), 'SINOPAC-04-219-001')
        with error_step('SINOPAC-04-220-001'):
            if hit.status != 200:
                raise with_error_code(RuntimeError("sinopac-twd-history-inventory-envelope"), 'SINOPAC-04-220-001')
        with error_step('SINOPAC-04-221-001'):
            if hit.redirected:
                raise with_error_code(RuntimeError("sinopac-twd-history-inventory-envelope"), 'SINOPAC-04-221-001')
        with error_step('SINOPAC-04-222-001'):
            if type(hit.body_size) is not int:
                raise with_error_code(RuntimeError("sinopac-twd-history-inventory-envelope"), 'SINOPAC-04-222-001')
        with error_step('SINOPAC-04-223-001'):
            if not 0 <= hit.body_size <= 5000000:
                raise with_error_code(RuntimeError("sinopac-twd-history-inventory-envelope"), 'SINOPAC-04-223-001')
        with error_step('SINOPAC-04-224-001'):
            if hit.content_type.split(';', 1)[0].strip().lower() != 'application/json':
                raise with_error_code(RuntimeError("sinopac-twd-history-inventory-envelope"), 'SINOPAC-04-224-001')
        with error_step('SINOPAC-04-225-001'):
            url_reason = []
            if not cls._exact_hit_url(hit, cls._TWD_INVENTORY_PATH, numeric_query=True, _diagnostic=url_reason, _error_ids=(
                'SINOPAC-04-780-001', 'SINOPAC-04-780-002', 'SINOPAC-04-780-003',
                'SINOPAC-04-780-004', 'SINOPAC-04-780-005', 'SINOPAC-04-780-006',
                'SINOPAC-04-780-007', 'SINOPAC-04-780-008', 'SINOPAC-04-780-009', 'SINOPAC-04-780-010',
            )):
                raise with_error_code(RuntimeError("sinopac-twd-history-inventory-envelope"), url_reason[0] if url_reason else 'SINOPAC-04-225-001')
        with error_step('SINOPAC-04-226-001'):
            if not isinstance(body, dict):
                raise with_error_code(RuntimeError("sinopac-twd-history-inventory-envelope"), 'SINOPAC-04-226-001')
        with error_step('SINOPAC-04-227-001'):
            if set(body) != {'Header', 'Message', 'SubInfo'}:
                raise with_error_code(RuntimeError("sinopac-twd-history-inventory-envelope"), 'SINOPAC-04-227-001')
        with error_step('SINOPAC-04-228-001'):
            if body.get('Header') != 'SUCCESS':
                raise with_error_code(RuntimeError("sinopac-twd-history-inventory-envelope"), 'SINOPAC-04-228-001')
        with error_step('SINOPAC-04-229-001'):
            if body.get('Message') not in (None, ''):
                raise with_error_code(RuntimeError("sinopac-twd-history-inventory-envelope"), 'SINOPAC-04-229-001')
        with error_step('SINOPAC-04-230-001'):
            if not isinstance(rows, list):
                raise with_error_code(RuntimeError("sinopac-twd-history-inventory-envelope"), 'SINOPAC-04-230-001')
        inventory = []
        with error_step('SINOPAC-04-231-001'):
            seen_accounts: set[tuple[str, str, str]] = set()
        with error_step('SINOPAC-04-247-001'):
            for row in rows:
                with error_step('SINOPAC-04-232-001'):
                    if not isinstance(row, dict):
                        raise with_error_code(RuntimeError("sinopac-twd-history-inventory-row"), 'SINOPAC-04-232-001')
                with error_step('SINOPAC-04-233-001'):
                    if set(row) != {'DataText', 'DataValue', 'DisplayText'}:
                        raise with_error_code(RuntimeError("sinopac-twd-history-inventory-row"), 'SINOPAC-04-233-001')
                with error_step('SINOPAC-04-234-001'):
                    label = row.get("DataText")
                with error_step('SINOPAC-04-235-001'):
                    identity = row.get("DataValue")
                with error_step('SINOPAC-04-236-001'):
                    currency = row.get("DisplayText")
                with error_step('SINOPAC-04-237-001'):
                    if not isinstance(label, str):
                        raise with_error_code(RuntimeError("sinopac-twd-history-inventory-identity"), 'SINOPAC-04-237-001')
                with error_step('SINOPAC-04-238-001'):
                    if not label:
                        raise with_error_code(RuntimeError("sinopac-twd-history-inventory-identity"), 'SINOPAC-04-238-001')
                with error_step('SINOPAC-04-239-001'):
                    if label != label.strip():
                        raise with_error_code(RuntimeError("sinopac-twd-history-inventory-identity"), 'SINOPAC-04-239-001')
                with error_step('SINOPAC-04-240-001'):
                    if not isinstance(identity, str):
                        raise with_error_code(RuntimeError("sinopac-twd-history-inventory-identity"), 'SINOPAC-04-240-001')
                with error_step('SINOPAC-04-241-001'):
                    if re.fullmatch('\\d{14}', identity) is None:
                        raise with_error_code(RuntimeError("sinopac-twd-history-inventory-identity"), 'SINOPAC-04-241-001')
                with error_step('SINOPAC-04-242-001'):
                    if not isinstance(currency, str):
                        raise with_error_code(RuntimeError("sinopac-twd-history-inventory-identity"), 'SINOPAC-04-242-001')
                with error_step('SINOPAC-04-243-001'):
                    if re.fullmatch('[A-Z]{3}', currency) is None:
                        raise with_error_code(RuntimeError("sinopac-twd-history-inventory-identity"), 'SINOPAC-04-243-001')
                account = (label, identity, currency)
                with error_step('SINOPAC-04-244-001'):
                    if account in seen_accounts:
                        raise with_error_code(RuntimeError("sinopac-twd-history-inventory-identity"), 'SINOPAC-04-244-001')
                with error_step('SINOPAC-04-245-001'):
                    seen_accounts.add(account)
                with error_step('SINOPAC-04-246-001'):
                    inventory.append({"label": label, "identity": identity, "currency": currency})
        return inventory

    @staticmethod
    def _yyyymmdd(value, error: str, *, error_action="SINOPAC-07-150") -> date:
        type_id, format_id, calendar_id = {
            'SINOPAC-07-150': ('SINOPAC-07-150-001', 'SINOPAC-07-150-002', 'SINOPAC-07-150-003'),
            'SINOPAC-04-151': ('SINOPAC-04-151-001', 'SINOPAC-04-151-002', 'SINOPAC-04-151-003'),
            'SINOPAC-04-152': ('SINOPAC-04-152-001', 'SINOPAC-04-152-002', 'SINOPAC-04-152-003'),
            'SINOPAC-04-153': ('SINOPAC-04-153-001', 'SINOPAC-04-153-002', 'SINOPAC-04-153-003'),
            'SINOPAC-05-151': ('SINOPAC-05-151-001', 'SINOPAC-05-151-002', 'SINOPAC-05-151-003'),
            'SINOPAC-05-152': ('SINOPAC-05-152-001', 'SINOPAC-05-152-002', 'SINOPAC-05-152-003'),
            'SINOPAC-07-151': ('SINOPAC-07-151-001', 'SINOPAC-07-151-002', 'SINOPAC-07-151-003'),
            'SINOPAC-07-152': ('SINOPAC-07-152-001', 'SINOPAC-07-152-002', 'SINOPAC-07-152-003'),
            'SINOPAC-07-153': ('SINOPAC-07-153-001', 'SINOPAC-07-153-002', 'SINOPAC-07-153-003'),
            'SINOPAC-07-154': ('SINOPAC-07-154-001', 'SINOPAC-07-154-002', 'SINOPAC-07-154-003'),
            'SINOPAC-07-155': ('SINOPAC-07-155-001', 'SINOPAC-07-155-002', 'SINOPAC-07-155-003'),
        }[error_action]
        if not isinstance(value, str):
            raise with_error_code(RuntimeError(error), type_id)
        with error_step(format_id):
            if re.fullmatch(r"\d{8}", value) is None:
                raise with_error_code(RuntimeError(error), format_id)
        try:
            with error_step(calendar_id):
                return datetime.strptime(value, "%Y%m%d").date()
        except ValueError:
            raise with_error_code(RuntimeError(error), calendar_id) from None

    @staticmethod
    def _history_amount(value, error: str, *, error_action="SINOPAC-07-140") -> Decimal:
        if not isinstance(value, str):
            raise with_error_code(RuntimeError(error), f"{error_action}-001")
        with error_step(f"{error_action}-002"):
            text = re.sub(r"<[^>]*>", "", value).strip()
        with error_step(f"{error_action}-003"):
            if re.fullmatch(
                r"[+-]?(?:(?:0|[1-9]\d*)(?:\.\d{1,6})?|[1-9]\d{0,2}(?:,\d{3})+(?:\.\d{1,6})?)",
                text,
            ) is None:
                raise with_error_code(RuntimeError(error), f"{error_action}-003")
        try:
            with error_step(f"{error_action}-004"):
                amount = Decimal(text.replace(",", ""))
        except InvalidOperation:
            raise with_error_code(RuntimeError(error), f"{error_action}-004") from None
        if not amount.is_finite():
            raise with_error_code(RuntimeError(error), f"{error_action}-005")
        if abs(amount) > Decimal("2147483647"):
            raise with_error_code(RuntimeError(error), f"{error_action}-006")
        return amount

    @classmethod
    def _validate_history_row(
        cls, row, *, currency: str, start: date, end: date,
    ) -> None:
        error = "sinopac-twd-history-row"
        with error_step('SINOPAC-07-200-001'):
            if not isinstance(row, dict):
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-200-001')
        with error_step('SINOPAC-07-201-001'):
            if set(row) != cls._HISTORY_ROW_KEYS:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-201-001')
        for key in cls._HISTORY_ROW_KEYS:
            # Static schema positions, never the row's private contents.
            error_id = {
                "DataText1": "SINOPAC-07-101-001", "DataText2": "SINOPAC-07-102-001",
                "DataText3": "SINOPAC-07-103-001", "DataText4": "SINOPAC-07-104-001",
                "DataText5": "SINOPAC-07-105-001", "DataText6": "SINOPAC-07-106-001",
                "DataText7": "SINOPAC-07-107-001", "DataText8": "SINOPAC-07-108-001",
                "DataText9": "SINOPAC-07-109-001", "DataText10": "SINOPAC-07-110-001",
                "DataText11": "SINOPAC-07-111-001",
            }[key]
            with error_step(error_id):
                if not isinstance(row[key], str):
                    raise with_error_code(RuntimeError(error), error_id)
        with error_step('SINOPAC-07-203-001'):
            if not re.fullmatch('\\d{4}/\\d{2}/\\d{2}<br />\\d{2}:\\d{2}', row['DataText1']):
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-203-001')
        with error_step('SINOPAC-07-204-001'):
            if not re.fullmatch('\\d{4}/\\d{2}/\\d{2}', row['DataText2']):
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-204-001')
        with error_step('SINOPAC-07-205-001'):
            raw_datetime = re.sub(
                r"<br\s*/?>", " ", row["DataText1"], flags=re.IGNORECASE,
            ).strip()
        try:
            with error_step('SINOPAC-07-206-001'):
                datetime.strptime(raw_datetime, "%Y/%m/%d %H:%M")
            with error_step('SINOPAC-07-207-001'):
                datetime.strptime(row["DataText2"].strip(), "%Y/%m/%d")
        except ValueError as source_error:
            raise with_error_code(RuntimeError(error), exception_error_id(source_error, bank="sinopac") or 'SINOPAC-07-208-001') from None
        with error_step('SINOPAC-07-209-001'):
            description = _plain_text(row["DataText3"])
        with error_step('SINOPAC-07-210-001'):
            if not description:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-210-001')
        with error_step('SINOPAC-07-211-001'):
            if len(description) > 500:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-211-001')
        with error_step('SINOPAC-07-212-001'):
            amount = cls._history_amount(row["DataText4"], error, error_action="SINOPAC-07-120")
        with error_step('SINOPAC-07-213-001'):
            balance = cls._history_amount(row["DataText5"], error, error_action="SINOPAC-07-130")
        with error_step('SINOPAC-07-214-001'):
            if currency == 'TWD':
                for value, error_id in ((amount, 'SINOPAC-07-214-101'), (balance, 'SINOPAC-07-214-102')):
                    if value != value.to_integral_value():
                        raise with_error_code(RuntimeError(error), error_id)
        with error_step('SINOPAC-07-215-001'):
            for i, error_id in ((6, 'SINOPAC-07-215-106'), (7, 'SINOPAC-07-215-107'), (8, 'SINOPAC-07-215-108'), (9, 'SINOPAC-07-215-109'), (10, 'SINOPAC-07-215-110'), (11, 'SINOPAC-07-215-111')):
                if len(row[f'DataText{i}']) > 2000:
                    raise with_error_code(RuntimeError(error), error_id)

    @classmethod
    def _validate_history_hit(
        cls, hit, *, label: str, identity: str, currency: str, start: date, end: date,
        business_date: str, as_of: date, after_sequence: int = 0,
    ) -> dict:
        error = "sinopac-twd-history-response"
        with error_step('SINOPAC-07-216-001'):
            params = parse_qs(hit.req_body, keep_blank_values=True) if isinstance(hit.req_body, str) else {}
        with error_step('SINOPAC-07-217-001'):
            payload = hit.resp_json
        with error_step('SINOPAC-07-218-001'):
            body = payload[0] if isinstance(payload, list) and len(payload) == 1 else None
        with error_step('SINOPAC-07-219-001'):
            if type(hit.request_sequence) is not int:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-219-001')
        with error_step('SINOPAC-07-220-001'):
            if hit.request_sequence <= after_sequence:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-220-001')
        with error_step('SINOPAC-07-221-001'):
            if hit.main_frame_request is not True:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-221-001')
        with error_step('SINOPAC-07-222-001'):
            if hit.method != 'POST':
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-222-001')
        with error_step('SINOPAC-07-223-001'):
            if hit.status != 200:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-223-001')
        with error_step('SINOPAC-07-224-001'):
            if hit.redirected:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-224-001')
        with error_step('SINOPAC-07-225-001'):
            if type(hit.body_size) is not int:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-225-001')
        with error_step('SINOPAC-07-226-001'):
            if not 0 <= hit.body_size <= 5000000:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-226-001')
        with error_step('SINOPAC-07-227-001'):
            if hit.content_type.split(';', 1)[0].strip().lower() != 'application/json':
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-227-001')
        with error_step('SINOPAC-07-228-001'):
            url_reason = []
            if not cls._exact_hit_url(hit, cls._TWD_HISTORY_PATH, numeric_query=True, _diagnostic=url_reason, _error_ids=(
                'SINOPAC-07-780-001', 'SINOPAC-07-780-002', 'SINOPAC-07-780-003',
                'SINOPAC-07-780-004', 'SINOPAC-07-780-005', 'SINOPAC-07-780-006',
                'SINOPAC-07-780-007', 'SINOPAC-07-780-008', 'SINOPAC-07-780-009', 'SINOPAC-07-780-010',
            )):
                raise with_error_code(RuntimeError(error), url_reason[0] if url_reason else 'SINOPAC-07-228-001')
        with error_step('SINOPAC-07-229-001'):
            if set(params) != cls._HISTORY_FORM_KEYS:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-229-001')
        with error_step('SINOPAC-07-230-001'):
            if params.get('Acct') != [label]:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-230-001')
        with error_step('SINOPAC-07-231-001'):
            if params.get('AcctName') != ['']:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-231-001')
        with error_step('SINOPAC-07-232-001'):
            if params.get('AcctValue') != [identity]:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-232-001')
        with error_step('SINOPAC-07-233-001'):
            if params.get('Curr') != [currency]:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-233-001')
        with error_step('SINOPAC-07-234-001'):
            if params.get('CurrName') != ['']:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-234-001')
        with error_step('SINOPAC-07-235-001'):
            if params.get('StartDate') != [start.strftime('%Y%m%d')]:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-235-001')
        with error_step('SINOPAC-07-236-001'):
            if params.get('EndDate') != [end.strftime('%Y%m%d')]:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-236-001')
        with error_step('SINOPAC-07-237-001'):
            if params.get('QueryType') != ['3']:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-237-001')
        with error_step('SINOPAC-07-238-001'):
            if params.get('TextType') != ['']:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-238-001')
        with error_step('SINOPAC-07-239-001'):
            if params.get('BusinessDate') != [business_date]:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-239-001')
        with error_step('SINOPAC-07-240-001'):
            if not isinstance(body, dict):
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-240-001')
        with error_step('SINOPAC-07-241-001'):
            if set(body) != cls._HISTORY_RESPONSE_KEYS:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-241-001')
        with error_step('SINOPAC-07-242-001'):
            if body.get('Header') not in {'SUCCESS', 'FAIL'}:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-242-001')
        with error_step('SINOPAC-07-243-001'):
            if body.get('MaxMonth') != '3':
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-243-001')
        with error_step('SINOPAC-07-244-001'):
            parsed_business_date = cls._yyyymmdd(business_date, error, error_action="SINOPAC-07-151")
        with error_step('SINOPAC-07-245-001'):
            begin = cls._yyyymmdd(body.get("BeginDate"), error, error_action="SINOPAC-07-152")
        with error_step('SINOPAC-07-246-001'):
            response_end = cls._yyyymmdd(body.get("EndDate"), error, error_action="SINOPAC-07-153")
        with error_step('SINOPAC-07-247-001'):
            default_begin = cls._yyyymmdd(body.get("DefBeginDate"), error, error_action="SINOPAC-07-154")
        with error_step('SINOPAC-07-248-001'):
            default_end = cls._yyyymmdd(body.get("DefEndDate"), error, error_action="SINOPAC-07-155")
        with error_step('SINOPAC-07-249-001'):
            if parsed_business_date > as_of:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-249-001')
        with error_step('SINOPAC-07-250-001'):
            if begin > start:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-250-001')
        with error_step('SINOPAC-07-251-001'):
            if response_end < end:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-251-001')
        with error_step('SINOPAC-07-252-001'):
            if not default_begin <= default_end <= as_of:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-252-001')
        with error_step('SINOPAC-07-253-001'):
            if body.get('isOBU') not in (None, 'Y', 'N'):
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-253-001')
        with error_step('SINOPAC-07-254-001'):
            head_info = body.get("HeadInfo")
        with error_step('SINOPAC-07-255-001'):
            rows = body.get("SubInfo")
        with error_step('SINOPAC-07-256-001'):
            if not isinstance(rows, list):
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-256-001')
        with error_step('SINOPAC-07-257-001'):
            if len(rows) > 10000:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-257-001')
        with error_step('SINOPAC-07-262-001'):
            if body["Header"] == "FAIL":
                with error_step('SINOPAC-07-258-001'):
                    if rows:
                        raise with_error_code(RuntimeError(error), 'SINOPAC-07-258-001')
                with error_step('SINOPAC-07-259-001'):
                    if body.get('Message') not in {'查無資料', '該區間查無明細資料！'}:
                        raise with_error_code(RuntimeError(error), 'SINOPAC-07-259-001')
                with error_step('SINOPAC-07-260-001'):
                    if body.get('RecordCount') is not None:
                        raise with_error_code(RuntimeError(error), 'SINOPAC-07-260-001')
                with error_step('SINOPAC-07-261-001'):
                    if head_info is not None:
                        raise with_error_code(RuntimeError(error), 'SINOPAC-07-261-001')
                return {
                    "records": [], "status": "explicit_empty", "rows": 0,
                    "display_fields": [],
                }
        empty = not rows
        with error_step('SINOPAC-07-265-001'):
            if empty:
                with error_step('SINOPAC-07-263-001'):
                    if body.get('Message') != '查無資料':
                        raise with_error_code(RuntimeError(error), 'SINOPAC-07-263-001')
                with error_step('SINOPAC-07-264-001'):
                    if body.get('RecordCount') is not None:
                        raise with_error_code(RuntimeError(error), 'SINOPAC-07-264-001')
                if head_info in (None, []):
                    return {
                        "records": [], "status": "explicit_empty", "rows": 0,
                        "display_fields": [],
                    }
        with error_step('SINOPAC-07-266-001'):
            if not isinstance(head_info, list):
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-266-001')
        with error_step('SINOPAC-07-267-001'):
            if len(head_info) != 9:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-267-001')
        with error_step('SINOPAC-07-268-001'):
            if any((not isinstance(item, dict) for item in head_info)):
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-268-001')
        with error_step('SINOPAC-07-269-001'):
            if any((set(item) != {'DataAlign', 'DetailShow', 'FieldKey', 'FieldWidth', 'HeadAlign', 'HeadText', 'MainShow', 'OrderIndex'} for item in head_info)):
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-269-001')
        with error_step('SINOPAC-07-270-001'):
            for item in head_info:
                for key, value in zip(item, item.values()):
                    error_id = {
                        'DataAlign': 'SINOPAC-07-270-101',
                        'DetailShow': 'SINOPAC-07-270-102',
                        'FieldKey': 'SINOPAC-07-270-103',
                        'FieldWidth': 'SINOPAC-07-270-104',
                        'HeadAlign': 'SINOPAC-07-270-105',
                        'HeadText': 'SINOPAC-07-270-106',
                        'MainShow': 'SINOPAC-07-270-107',
                        'OrderIndex': 'SINOPAC-07-270-108',
                    }[key]
                    if not isinstance(value, str):
                        raise with_error_code(RuntimeError(error), error_id)
        with error_step('SINOPAC-07-271-001'):
            if {item.get('FieldKey') for item in head_info} != {f'DataText{i}' for i in range(1, 10)}:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-271-001')
        with error_step('SINOPAC-07-272-001'):
            orders = [item["OrderIndex"] for item in head_info]
        with error_step('SINOPAC-07-273-001'):
            for order in orders:
                if not order.isdigit():
                    raise with_error_code(RuntimeError(error), 'SINOPAC-07-273-101')
                if len(order) > 2:
                    raise with_error_code(RuntimeError(error), 'SINOPAC-07-273-102')
        with error_step('SINOPAC-07-274-001'):
            numeric_orders = [int(order) for order in orders]
        with error_step('SINOPAC-07-275-001'):
            if not 0 <= min(numeric_orders) <= max(numeric_orders) <= 9:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-275-001')
        with error_step('SINOPAC-07-276-001'):
            if len(set(numeric_orders)) <= 1:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-276-001')
        with error_step('SINOPAC-07-277-001'):
            if numeric_orders != sorted(numeric_orders):
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-277-001')
        with error_step('SINOPAC-07-286-001'):
            for item in head_info:
                with error_step('SINOPAC-07-278-001'):
                    if item['DataAlign'].lower() not in {'', '2', '3', 'l', 'r', 'c', 'left', 'right', 'center'}:
                        raise with_error_code(RuntimeError(error), 'SINOPAC-07-278-001')
                with error_step('SINOPAC-07-279-001'):
                    if item['HeadAlign'].lower() not in {'', '2', '3', 'l', 'r', 'c', 'left', 'right', 'center'}:
                        raise with_error_code(RuntimeError(error), 'SINOPAC-07-279-001')
                with error_step('SINOPAC-07-280-001'):
                    if item['MainShow'].lower() not in {'', '0', '1', 'y', 'n', 'true', 'false'}:
                        raise with_error_code(RuntimeError(error), 'SINOPAC-07-280-001')
                with error_step('SINOPAC-07-281-001'):
                    if item['DetailShow'].lower() not in {'', '0', '1', 'y', 'n', 'true', 'false'}:
                        raise with_error_code(RuntimeError(error), 'SINOPAC-07-281-001')
                with error_step('SINOPAC-07-282-001'):
                    if not item['FieldWidth'].isdigit():
                        raise with_error_code(RuntimeError(error), 'SINOPAC-07-282-001')
                with error_step('SINOPAC-07-283-001'):
                    if not 0 <= int(item['FieldWidth']) <= 1000:
                        raise with_error_code(RuntimeError(error), 'SINOPAC-07-283-001')
                with error_step('SINOPAC-07-284-001'):
                    if not item['HeadText'].strip():
                        raise with_error_code(RuntimeError(error), 'SINOPAC-07-284-001')
                with error_step('SINOPAC-07-285-001'):
                    if len(item['HeadText']) > 100:
                        raise with_error_code(RuntimeError(error), 'SINOPAC-07-285-001')
        with error_step('SINOPAC-07-287-001'):
            display_fields = [
                item["FieldKey"] for item in head_info
                if (
                    item["MainShow"].lower() in {"1", "y", "true"}
                    or item["DetailShow"].lower() in {"1", "y", "true"}
                )
            ]
        with error_step('SINOPAC-07-288-001'):
            if not display_fields:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-288-001')
        if empty:
            return {
                "records": [], "status": "explicit_empty", "rows": 0,
                "display_fields": display_fields,
            }
        with error_step('SINOPAC-07-289-001'):
            record_count = body.get("RecordCount")
        with error_step('SINOPAC-07-290-001'):
            if body.get('Message') not in (None, ''):
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-290-001')
        with error_step('SINOPAC-07-291-001'):
            if not isinstance(record_count, str):
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-291-001')
        with error_step('SINOPAC-07-292-001'):
            if not record_count.isdigit():
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-292-001')
        with error_step('SINOPAC-07-293-001'):
            if len(record_count) > 5:
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-293-001')
        with error_step('SINOPAC-07-294-001'):
            if int(record_count) + 1 != len(rows):
                raise with_error_code(RuntimeError(error), 'SINOPAC-07-294-001')
        with error_step('SINOPAC-07-295-001'):
            seen_rows = set()
        with error_step('SINOPAC-07-300-001'):
            for row in rows:
                with error_step('SINOPAC-07-296-001'):
                    cls._validate_history_row(
                        row, currency=currency, start=start, end=end,
                    )
                with error_step('SINOPAC-07-297-001'):
                    fingerprint = tuple(row[f"DataText{i}"] for i in range(1, 12))
                with error_step('SINOPAC-07-298-001'):
                    if fingerprint in seen_rows:
                        raise with_error_code(RuntimeError(error), 'SINOPAC-07-298-001')
                with error_step('SINOPAC-07-299-001'):
                    seen_rows.add(fingerprint)
        with error_step('SINOPAC-07-301-001'):
            return {
                "records": rows, "status": "complete", "rows": len(rows),
                "display_fields": display_fields,
            }

    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        """登入後抓帳戶餘額 / 貸款明細 / 信用卡彙總與帳單 / 全卡片 / 資產分析。

        永豐 MMA 資產總覽頁登入後自動觸發 ws_bankbal/cardsum/cardbilling_sp/
        AllCards/ws_mychart，巡訪「資產分析 / 信用卡總覽」頁也會補打。
        台幣交易明細 dropdown 是 jQuery 客製化元件（#divDebitAccount），待下次破。
        """
        out: dict = {}
        with error_step('SINOPAC-07-319-001'):
            page.wait_for_timeout(5000)

        # 巡訪「資產分析 / 信用卡總覽」頁觸發更多 API
        with error_step('SINOPAC-07-322-001'):
            for url in [
                "https://mma.sinopac.com/MyMMA/Myasset/mma_assets_analysis.aspx",
                "https://mma.sinopac.com/mma/mymma/myasset/cards_summary.aspx",
            ]:
                try:
                    with error_step('SINOPAC-07-320-001'):
                        page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    with error_step('SINOPAC-07-321-001'):
                        page.wait_for_timeout(6000)
                except Exception:
                    _log("[collect] page_navigation_failed")

        # 從 collector 取已攔到的 API JSON
        with error_step('SINOPAC-07-323-001'):
            out["bank_balance"] = self._latest_json(collector, "ws_bankbal.ashx")        # 銀行帳戶餘額 list
        with error_step('SINOPAC-07-324-001'):
            out["debit_accounts"] = self._latest_json(collector, "ws_debitacct.ashx")    # 扣款帳戶清單
        with error_step('SINOPAC-07-325-001'):
            out["card_summary"] = self._latest_json(collector, "ws_cardsum.ashx")        # 信用卡彙總
        with error_step('SINOPAC-07-326-001'):
            out["card_billing"] = self._latest_json(collector, "ws_cardbilling_sp.ashx") # 信用卡 3 個月帳單
        with error_step('SINOPAC-07-327-001'):
            out["all_cards"] = self._latest_json(collector, "AllCards")                  # 全卡清單
        with error_step('SINOPAC-07-328-001'):
            out["asset_chart"] = self._latest_json(collector, "ws_mychart.ashx")         # 資產分佈圓餅
        with error_step('SINOPAC-07-329-001'):
            out["alert_info"] = self._latest_json(collector, "ws_alertinfo.ashx")        # 帳戶通知

        # === 貸款明細：每個貸款帳號查本金餘額 / 利率 / 到期日 ===
        with error_step('SINOPAC-07-330-001'):
            out["loan"] = self._collect_loans(page, collector)

        # === 全幣別帳戶交易明細：權威 inventory + 月窗 coverage ===
        with error_step('SINOPAC-07-331-001'):
            account_history = self._collect_transactions(page, collector)
        with error_step('SINOPAC-07-332-001'):
            out["account_transactions"] = account_history["results"]
        with error_step('SINOPAC-07-333-001'):
            out["debit_accounts"] = account_history["inventory"]
        with error_step('SINOPAC-07-334-001'):
            out["history_coverage"] = account_history["coverage"]

        # === 信用卡明細：帳單已請款（StatementInquiry HTML）+ 未請款（UnbilledTxInquiry API）===
        with error_step('SINOPAC-07-335-001'):
            out["card_statements"] = self._collect_card_statements(page)
        with error_step('SINOPAC-07-336-001'):
            out["card_unbilled"] = self._collect_card_unbilled(page, collector)

        # 偵測尚未抓到的（log 給 debug 用）
        with error_step('SINOPAC-07-337-001'):
            miss = [k for k, v in out.items() if v is None]
        with error_step('SINOPAC-07-338-001'):
            if miss:
                _log(f"[collect] 未攔到: {miss}")

        with error_step('SINOPAC-07-339-001'):
            publish_card_bill_facts(out, [_sinopac_card_bill_fact(out)])

        with error_step('SINOPAC-07-340-001'):
            out["_all_endpoints"] = sorted({h.endpoint for h in collector.hits if h.resp_json})
        with error_step('SINOPAC-07-341-001'):
            return BankCollectResult(**out)

    def _collect_loans(self, page, collector: ResponseCollector) -> dict:
        """逐帳號觸發 ws_loaninfo，回傳銀行原生貸款明細。"""
        with error_step('SINOPAC-05-200-001'):
            page.goto(LOAN_DETAIL_URL, wait_until="domcontentloaded", timeout=30000)
        with error_step('SINOPAC-05-201-001'):
            page.wait_for_timeout(8000)

        with error_step('SINOPAC-05-202-001'):
            account_raw = self._latest_json(collector, "ws_loanaccount.ashx")
        with error_step('SINOPAC-05-203-001'):
            if not isinstance(account_raw, list):
                raise with_error_code(RuntimeError("sinopac-loan-inventory-envelope"), 'SINOPAC-05-203-001')
        with error_step('SINOPAC-05-204-001'):
            if not account_raw:
                raise with_error_code(RuntimeError("sinopac-loan-inventory-envelope"), 'SINOPAC-05-204-001')
        with error_step('SINOPAC-05-205-001'):
            if not isinstance(account_raw[0], dict):
                raise with_error_code(RuntimeError("sinopac-loan-inventory-envelope"), 'SINOPAC-05-205-001')
        with error_step('SINOPAC-05-206-001'):
            if not isinstance(account_raw[0].get('SubInfo'), list):
                raise with_error_code(RuntimeError("sinopac-loan-inventory-envelope"), 'SINOPAC-05-206-001')
        with error_step('SINOPAC-05-207-001'):
            accounts = account_raw[0]["SubInfo"]
        details = []
        with error_step('SINOPAC-05-247-001'):
            for account in accounts:
                with error_step('SINOPAC-05-208-001'):
                    if not isinstance(account, dict):
                        raise with_error_code(RuntimeError("sinopac-loan-inventory-row"), 'SINOPAC-05-208-001')
                with error_step('SINOPAC-05-209-001'):
                    account_no = account.get("AcctValue")
                with error_step('SINOPAC-05-210-001'):
                    formatted = account.get("AcctValueFormat")
                with error_step('SINOPAC-05-211-001'):
                    if not account_no:
                        raise with_error_code(RuntimeError("sinopac-loan-inventory-identity"), 'SINOPAC-05-211-001')
                with error_step('SINOPAC-05-212-001'):
                    if not formatted:
                        raise with_error_code(RuntimeError("sinopac-loan-inventory-identity"), 'SINOPAC-05-212-001')

                with error_step('SINOPAC-05-213-001'):
                    before = len(collector.by_endpoint("ws_loaninfo.ashx"))
                with error_step('SINOPAC-05-214-001'):
                    clicked = page.evaluate(
                        """args => {
                  const account = document.querySelector('#AcctValue');
                  const formatted = document.querySelector('#AcctValueFormat');
                  const button = document.querySelector('#btnQuery');
                  if (!account) return {errorId:'SINOPAC-05-780-001'};
                  if (!formatted) return {errorId:'SINOPAC-05-780-002'};
                  if (!button) return {errorId:'SINOPAC-05-780-003'};
                  account.value = args.account;
                  formatted.value = args.formatted;
                  for (const el of [account, formatted]) {
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                  }
                  button.click();
                  return true;
                }""",
                        {"account": account_no, "formatted": formatted},
                    )
                loan_control_reason = None
                if isinstance(clicked, dict) and clicked.get('errorId') in (
                    'SINOPAC-05-780-001', 'SINOPAC-05-780-002', 'SINOPAC-05-780-003',
                ):
                    loan_control_reason = clicked['errorId']
                    clicked = False
                with error_step('SINOPAC-05-215-001'):
                    if not clicked:
                        raise with_error_code(RuntimeError("sinopac-loan-query-control"), loan_control_reason or 'SINOPAC-05-215-001')
                with error_step('SINOPAC-05-216-001'):
                    page.wait_for_timeout(5000)

                with error_step('SINOPAC-05-217-001'):
                    hits = collector.by_endpoint("ws_loaninfo.ashx")[before:]
                matching_hits = []
                with error_step('SINOPAC-05-222-001'):
                    for hit in hits:
                        with error_step('SINOPAC-05-218-001'):
                            if not isinstance(hit.req_body, str):
                                continue
                        with error_step('SINOPAC-05-219-001'):
                            params = parse_qs(hit.req_body, keep_blank_values=True)
                        with error_step('SINOPAC-05-221-001'):
                            if (params.get("AcctValue") == [account_no]
                                    and params.get("AcctValueFormat") == [formatted]):
                                with error_step('SINOPAC-05-220-001'):
                                    matching_hits.append(hit)
                with error_step('SINOPAC-05-223-001'):
                    if not matching_hits:
                        raise with_error_code(RuntimeError("sinopac-loan-response-missing"), 'SINOPAC-05-223-001')
                with error_step('SINOPAC-05-224-001'):
                    hit = matching_hits[-1]
                with error_step('SINOPAC-05-225-001'):
                    if not 200 <= hit.status < 300:
                        raise with_error_code(RuntimeError("sinopac-loan-response-http"), 'SINOPAC-05-225-001')
                with error_step('SINOPAC-05-226-001'):
                    info_raw = hit.resp_json
                with error_step('SINOPAC-05-227-001'):
                    if not isinstance(info_raw, list):
                        raise with_error_code(RuntimeError("sinopac-loan-response-envelope"), 'SINOPAC-05-227-001')
                with error_step('SINOPAC-05-228-001'):
                    if not info_raw:
                        raise with_error_code(RuntimeError("sinopac-loan-response-envelope"), 'SINOPAC-05-228-001')
                with error_step('SINOPAC-05-229-001'):
                    if not isinstance(info_raw[0], dict):
                        raise with_error_code(RuntimeError("sinopac-loan-response-envelope"), 'SINOPAC-05-229-001')
                with error_step('SINOPAC-05-230-001'):
                    if not isinstance(info_raw[0].get('SubInfo'), list):
                        raise with_error_code(RuntimeError("sinopac-loan-response-envelope"), 'SINOPAC-05-230-001')
                with error_step('SINOPAC-05-231-001'):
                    body = info_raw[0]
                with error_step('SINOPAC-05-232-001'):
                    records = body["SubInfo"]
                required = ("LoanKind", "Currency", "LoanBalance")
                with error_step('SINOPAC-05-233-001'):
                    if body.get('Message'):
                        raise with_error_code(RuntimeError("sinopac-loan-response-records"), 'SINOPAC-05-233-001')
                with error_step('SINOPAC-05-234-001'):
                    if not records:
                        raise with_error_code(RuntimeError("sinopac-loan-response-records"), 'SINOPAC-05-234-001')
                with error_step('SINOPAC-05-235-001'):
                    for record in records:
                        if not isinstance(record, dict):
                            raise with_error_code(RuntimeError("sinopac-loan-response-records"), 'SINOPAC-05-235-101')
                        for key in required:
                            error_id = {'LoanKind': 'SINOPAC-05-235-102', 'Currency': 'SINOPAC-05-235-103', 'LoanBalance': 'SINOPAC-05-235-104'}[key]
                            if record.get(key) in (None, ''):
                                raise with_error_code(RuntimeError("sinopac-loan-response-records"), error_id)
                repayments = []
                with error_step('SINOPAC-05-245-001'):
                    for record in records:
                        with error_step('SINOPAC-05-236-001'):
                            repayments.append(self._collect_loan_repayments(page, collector, account, record))
                        # Return once only after verified success. Never replay a POST or
                        # retry a failed query if history/selection cannot be restored.
                        with error_step('SINOPAC-05-237-001'):
                            page.go_back(wait_until="domcontentloaded", timeout=15_000)
                        with error_step('SINOPAC-05-238-001'):
                            if page.url != 'https://mma.sinopac.com/mma/bank/easy_index_loan/mma_loandetail.aspx':
                                raise with_error_code(RuntimeError("sinopac-loan-restore-page"), 'SINOPAC-05-238-001')
                        with error_step('SINOPAC-05-239-001'):
                            if getattr(self, '_shared_dialog_blocked', False):
                                raise with_error_code(RuntimeError("sinopac-loan-restore-page"), 'SINOPAC-05-239-001')
                        with error_step('SINOPAC-05-240-001'):
                            if self._response_visible(page):
                                raise with_error_code(RuntimeError("sinopac-loan-restore-page"), 'SINOPAC-05-240-001')
                        with error_step('SINOPAC-05-244-001'):
                            for key, error_action in (("AcctValue", "SINOPAC-05-160"), ("AcctValueFormat", "SINOPAC-05-170")):
                                with error_step(f"{error_action}-001"):
                                    control = page.locator("#" + key)
                                with error_step(f"{error_action}-002"):
                                    if control.count() != 1:
                                        raise with_error_code(RuntimeError("sinopac-loan-restore-account"), f"{error_action}-002")
                                with error_step(f"{error_action}-003"):
                                    if control.input_value() != account[key]:
                                        raise with_error_code(RuntimeError("sinopac-loan-restore-account"), f"{error_action}-003")
                with error_step('SINOPAC-05-246-001'):
                    details.append({
                        "account": account_no,
                        "records": records,
                        "repayments": repayments,
                    })
        return {"details": details, "fetch_ok": True}

    @staticmethod
    def _validated_twd_handlers(
        handlers: object, inventory: list[dict],
    ) -> list[tuple[int, tuple[str, str, str]]]:
        with error_step('SINOPAC-04-248-001'):
            if not isinstance(handlers, list):
                raise with_error_code(RuntimeError("sinopac-twd-history-account-control"), 'SINOPAC-04-248-001')
        with error_step('SINOPAC-04-249-001'):
            pattern = re.compile(
                r"^setDebitAccount\('([^'\r\n]*)',\s*'(\d{14})',\s*'([A-Z]{3})'\)\s*;?$"
            )
        parsed = []
        with error_step('SINOPAC-04-253-001'):
            for position, handler in enumerate(handlers):
                with error_step('SINOPAC-04-250-001'):
                    match = pattern.fullmatch(handler) if isinstance(handler, str) else None
                with error_step('SINOPAC-04-251-001'):
                    if match is None:
                        raise with_error_code(RuntimeError("sinopac-twd-history-account-control"), 'SINOPAC-04-251-001')
                with error_step('SINOPAC-04-252-001'):
                    parsed.append((position, match.groups()))
        with error_step('SINOPAC-04-254-001'):
            expected = [
                (item["label"], item["identity"], item["currency"])
                for item in inventory
            ]
        with error_step('SINOPAC-04-255-001'):
            if [groups for _, groups in parsed] != expected:
                raise with_error_code(RuntimeError("sinopac-twd-history-account-control"), 'SINOPAC-04-255-001')
        return parsed

    def _collect_loan_repayments(self, page, collector, account: dict, record: dict) -> dict:
        """One native default period, not a retention/full-history assertion."""
        from backend.core.persist.sinopac import _parse_sinopac_repayment

        # Retain the query's diagnostic if cleanup masks its exception.
        error = "sinopac-loan-repayments"
        primary_error_id = None

        def ensure_page(url):
            with error_step('SINOPAC-05-248-001'):
                if page.url != url:
                    raise with_error_code(RuntimeError("sinopac-loan-repayments-page-state"), 'SINOPAC-05-248-001')
            with error_step('SINOPAC-05-249-001'):
                if getattr(self, '_shared_dialog_blocked', False):
                    raise with_error_code(RuntimeError("sinopac-loan-repayments-page-state"), 'SINOPAC-05-249-001')
            with error_step('SINOPAC-05-250-001'):
                if self._response_visible(page):
                    raise with_error_code(RuntimeError("sinopac-loan-repayments-page-state"), 'SINOPAC-05-250-001')

        try:
            with error_step('SINOPAC-05-251-001'):
                ensure_page("https://mma.sinopac.com/mma/bank/easy_index_loan/mma_loandetail.aspx")
            with error_step('SINOPAC-05-255-001'):
                for key, error_action in (("AcctValue", "SINOPAC-05-100"), ("AcctValueFormat", "SINOPAC-05-110")):
                    with error_step(f"{error_action}-001"):
                        control = page.locator("#" + key)
                    with error_step(f"{error_action}-002"):
                        if control.count() != 1:
                            raise with_error_code(RuntimeError("sinopac-loan-repayments-account-control"), f"{error_action}-002")
                    with error_step(f"{error_action}-003"):
                        if control.input_value() != account[key]:
                            raise with_error_code(RuntimeError("sinopac-loan-repayments-account-control"), f"{error_action}-003")
            with error_step('SINOPAC-05-256-001'):
                links = page.locator("#tbDetails a[onclick]")
            selected = []
            with error_step('SINOPAC-05-257-001'):
                pattern = re.compile(r"forQuery\(\s*'detail'\s*," + r"\s*'([^'\r\n]*)'\s*," * 5 + r"\s*'([^'\r\n]*)'\s*\)\s*;?")
            with error_step('SINOPAC-05-258-001'):
                expected = tuple(record[key] for key in ("Sub1_Sub2", "Currency", "LoanAmt", "LoanBalance", "LoanKind", "PayName"))
            with error_step('SINOPAC-05-263-001'):
                for i in range(links.count()):
                    with error_step('SINOPAC-05-259-001'):
                        link = links.nth(i)
                    with error_step('SINOPAC-05-260-001'):
                        match = pattern.fullmatch(link.get_attribute("onclick") or "")
                    with error_step('SINOPAC-05-262-001'):
                        if match and match.groups() == expected and "繳款明細" in error_step('SINOPAC-05-262-201')(lambda: link.inner_text())() and error_step('SINOPAC-05-262-202')(lambda: link.is_visible())():
                            with error_step('SINOPAC-05-261-001'):
                                selected.append(link)
            with error_step('SINOPAC-05-264-001'):
                if len(selected) != 1:
                    raise with_error_code(RuntimeError("sinopac-loan-repayments-link-cardinality"), 'SINOPAC-05-264-001')
            # A native locator click can navigate; never replay after an evaluate-context error.
            with error_step('SINOPAC-05-265-001'):
                selected[0].click(timeout=8_000)
            with error_step('SINOPAC-05-266-001'):
                page.wait_for_url(LOAN_DETAIL_URL, wait_until="domcontentloaded", timeout=15_000)
            with error_step('SINOPAC-05-267-001'):
                ensure_page(LOAN_DETAIL_URL)
            with error_step('SINOPAC-05-268-001'):
                if error_step('SINOPAC-05-268-201')(lambda: error_step('SINOPAC-05-268-202')(lambda: page.locator('#tr_mma'))().count())() != 1:
                    raise with_error_code(RuntimeError("sinopac-loan-repayments-form-control"), 'SINOPAC-05-268-001')
            with error_step('SINOPAC-05-269-001'):
                form = page.evaluate("""() => [...new FormData(document.querySelector('#tr_mma')).entries()]""")
            params = {key: [value] for key, value in form}
            with error_step('SINOPAC-05-270-001'):
                start = self._yyyymmdd(params["StartDate"][0], "sinopac-loan-repayments-start-date", error_action="SINOPAC-05-151")
            with error_step('SINOPAC-05-271-001'):
                end = self._yyyymmdd(params["EndDate"][0], "sinopac-loan-repayments-end-date", error_action="SINOPAC-05-152")
            with error_step('SINOPAC-05-272-001'):
                text_type = params.get("TextType", [None])[0]
            with error_step('SINOPAC-05-273-001'):
                if not isinstance(text_type, str):
                    raise with_error_code(RuntimeError("sinopac-loan-repayments-text-type"), 'SINOPAC-05-273-001')
            with error_step('SINOPAC-05-274-001'):
                if len(text_type) > 2000:
                    raise with_error_code(RuntimeError("sinopac-loan-repayments-text-type"), 'SINOPAC-05-274-001')
            with error_step('SINOPAC-05-275-001'):
                bound = {"AcctValue": account["AcctValue"], "AcctValueFormat": account["AcctValueFormat"],
                         "LNMAINACNO": account["AcctValue"], "LNALTNO": record["Sub1_Sub2"],
                         "CURRENCY": record["Currency"], "TextType": text_type,
                         **{key: record[key] for key in ("Sub1_Sub2", "LoanAmt", "LoanBalance", "LoanKind", "PayName")},
                         "StartDate": start.strftime("%Y%m%d"), "EndDate": end.strftime("%Y%m%d")}
            with error_step('SINOPAC-05-276-001'):
                if len(form) != len(bound):
                    raise with_error_code(RuntimeError("sinopac-loan-repayments-form-binding"), 'SINOPAC-05-276-001')
            with error_step('SINOPAC-05-277-001'):
                if params != {key: [value] for key, value in bound.items()}:
                    raise with_error_code(RuntimeError("sinopac-loan-repayments-form-binding"), 'SINOPAC-05-277-001')
            with error_step('SINOPAC-05-278-001'):
                if not start <= end <= _taipei_today():
                    raise with_error_code(RuntimeError("sinopac-loan-repayments-form-binding"), 'SINOPAC-05-278-001')
            with error_step('SINOPAC-05-283-001'):
                for selector, error_action in (("#StartDate", "SINOPAC-05-740"), ("#EndDate", "SINOPAC-05-750"), ("#btnQuery", "SINOPAC-05-760")):
                    with error_step(f'{error_action}-001'):
                        control = page.locator(selector)
                    with error_step(f'{error_action}-002'):
                        if control.count() != 1:
                            raise with_error_code(RuntimeError("sinopac-loan-repayments-query-control"), f'{error_action}-005')
                    with error_step(f'{error_action}-003'):
                        if not control.is_visible():
                            raise with_error_code(RuntimeError("sinopac-loan-repayments-query-control"), f'{error_action}-006')
                    with error_step(f'{error_action}-004'):
                        if not control.is_enabled():
                            raise with_error_code(RuntimeError("sinopac-loan-repayments-query-control"), f'{error_action}-007')
            with error_step('SINOPAC-05-284-001'):
                button = page.locator("#btnQuery")
            with error_step('SINOPAC-05-285-001'):
                if button.inner_text().strip() != '查詢':
                    raise with_error_code(RuntimeError("sinopac-loan-repayments-query-label"), 'SINOPAC-05-285-001')
            # The generic collector truncates request forms and may replay response.body().
            # Detach only during this native query; use the existing read-only CDP observer.
            with error_step('SINOPAC-05-286-001'):
                collector.detach(page)
            with error_step('SINOPAC-05-287-001'):
                observer = _LoanRepaymentBodyObserver(page, "https://mma.sinopac.com" + LOAN_REPAYMENT_PATH)
            responses = []
            with error_step('SINOPAC-05-288-001'):
                handler = lambda response: responses.append(response) if urlparse(response.url).path == LOAN_REPAYMENT_PATH else None
            with error_step('SINOPAC-05-289-001'):
                page.on("response", handler)
            try:
                with error_step('SINOPAC-05-290-001'):
                    observer.start()  # main's observer no longer self-starts on construction.
                with error_step('SINOPAC-05-291-001'):
                    button.click(timeout=8_000)
                with error_step('SINOPAC-05-293-001'):
                    for _ in range(100):
                        if responses:
                            break
                        with error_step('SINOPAC-05-292-001'):
                            page.wait_for_timeout(100)
                with error_step('SINOPAC-05-294-001'):
                    if len(responses) != 1:
                        raise with_error_code(RuntimeError("sinopac-loan-repayments-response-cardinality"), 'SINOPAC-05-294-001')
                with error_step('SINOPAC-05-295-001'):
                    response = responses[0]
                with error_step('SINOPAC-05-296-001'):
                    request = response.request
                with error_step('SINOPAC-05-297-001'):
                    if parse_qs(request.post_data, keep_blank_values=True) != params:
                        raise with_error_code(RuntimeError("sinopac-loan-repayments-request-binding"), 'SINOPAC-05-297-001')
                with error_step('SINOPAC-05-298-001'):
                    if response.headers.get('content-type', '').split(';', 1)[0].lower().strip() != 'application/json':
                        raise with_error_code(RuntimeError("sinopac-loan-repayments-response-type"), 'SINOPAC-05-298-001')
                with error_step('SINOPAC-05-299-001'):
                    raw = observer.read(response, page.main_frame, LOAN_DETAIL_URL, 5_000_000, 1)
                with error_step('SINOPAC-05-300-001'):
                    if raw is None:
                        raise with_error_code(RuntimeError("sinopac-loan-repayments-response-body"), 'SINOPAC-05-300-001')
                with error_step('SINOPAC-05-301-001'):
                    payload = json.loads(raw)
                with error_step('SINOPAC-05-302-001'):
                    if not isinstance(payload, list):
                        raise with_error_code(RuntimeError("sinopac-loan-repayments-response-envelope"), 'SINOPAC-05-302-001')
                with error_step('SINOPAC-05-303-001'):
                    if len(payload) != 1:
                        raise with_error_code(RuntimeError("sinopac-loan-repayments-response-envelope"), 'SINOPAC-05-303-001')
                with error_step('SINOPAC-05-304-001'):
                    if not isinstance(payload[0], dict):
                        raise with_error_code(RuntimeError("sinopac-loan-repayments-response-envelope"), 'SINOPAC-05-304-001')
                with error_step('SINOPAC-05-305-001'):
                    body = payload[0]
                with error_step('SINOPAC-05-306-001'):
                    metadata = body.get("HeadInfo")
                # Native results can include message text; require validated rows/DOM below.
                with error_step('SINOPAC-05-307-001'):
                    if set(body) != {'HeadInfo', 'SubInfo', 'Header', 'Message'}:
                        raise with_error_code(RuntimeError("sinopac-loan-repayments-response-metadata"), 'SINOPAC-05-307-001')
                with error_step('SINOPAC-05-308-001'):
                    if body.get('Header') != 'SUCCESS':
                        raise with_error_code(RuntimeError("sinopac-loan-repayments-response-metadata"), 'SINOPAC-05-308-001')
                with error_step('SINOPAC-05-309-001'):
                    if not isinstance(body.get('Message'), str):
                        raise with_error_code(RuntimeError("sinopac-loan-repayments-response-metadata"), 'SINOPAC-05-309-001')
                with error_step('SINOPAC-05-310-001'):
                    if len(body['Message']) > 2000:
                        raise with_error_code(RuntimeError("sinopac-loan-repayments-response-metadata"), 'SINOPAC-05-310-001')
                with error_step('SINOPAC-05-311-001'):
                    if not isinstance(metadata, list):
                        raise with_error_code(RuntimeError("sinopac-loan-repayments-response-metadata"), 'SINOPAC-05-311-001')
                with error_step('SINOPAC-05-312-001'):
                    if len(metadata) != 3:
                        raise with_error_code(RuntimeError("sinopac-loan-repayments-response-metadata"), 'SINOPAC-05-312-001')
                with error_step('SINOPAC-05-313-001'):
                    for item in metadata:
                        if not isinstance(item, dict):
                            raise with_error_code(RuntimeError("sinopac-loan-repayments-response-metadata"), 'SINOPAC-05-313-101')
                        if set(item) != {'HeadText', 'HeadAlign', 'DataAlign', 'MainShow', 'DetailShow', 'FieldKey', 'OrderIndex', 'FieldWidth'}:
                            raise with_error_code(RuntimeError("sinopac-loan-repayments-response-metadata"), 'SINOPAC-05-313-102')
                        # Keep the original value iteration order and one values() call.
                        for key, value in zip(item, item.values()):
                            error_action = {
                                'HeadText': 'SINOPAC-05-730',
                                'HeadAlign': 'SINOPAC-05-731',
                                'DataAlign': 'SINOPAC-05-732',
                                'MainShow': 'SINOPAC-05-733',
                                'DetailShow': 'SINOPAC-05-734',
                                'FieldKey': 'SINOPAC-05-735',
                                'OrderIndex': 'SINOPAC-05-736',
                                'FieldWidth': 'SINOPAC-05-737',
                            }[key]
                            if not isinstance(value, str):
                                raise with_error_code(RuntimeError("sinopac-loan-repayments-response-metadata"), f'{error_action}-001')
                            if len(value) > 2000:
                                raise with_error_code(RuntimeError("sinopac-loan-repayments-response-metadata"), f'{error_action}-002')
                with error_step('SINOPAC-05-314-001'):
                    records = body["SubInfo"]
                with error_step('SINOPAC-05-315-001'):
                    if not isinstance(records, list):
                        raise with_error_code(RuntimeError("sinopac-loan-repayments-response-records"), 'SINOPAC-05-315-001')
                with error_step('SINOPAC-05-316-001'):
                    if not 1 <= len(records) <= 10000:
                        raise with_error_code(RuntimeError("sinopac-loan-repayments-response-records"), 'SINOPAC-05-316-001')
                with error_step('SINOPAC-05-317-001'):
                    dom = page.evaluate(r"""() => {
                    const visible = e => !!e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
                    const rows = [...document.querySelectorAll('#tbodyDetails tr')];
                    return {
                        rows: rows.map(row => [...row.cells].map(cell => cell.innerText.trim())),
                        visible: rows.every(row => visible(row) && [...row.cells].every(visible)),
                        unique: ['tbDetails','tbodyDetails','tbodyNoDetail','tr_mma'].every(id => document.querySelectorAll('#'+id).length===1),
                        headers: [...document.querySelectorAll('#tbDetails th')].map(e=>e.innerText.trim()).filter(Boolean),
                        empty: visible(document.querySelector('#tbodyNoDetail')),
                        form: [...new FormData(document.querySelector('#tr_mma')).entries()],
                        pager: [...document.querySelectorAll('a,button,input,select,[role=button]')].some(e =>
                            /pager|pagination|nextpage|prevpage|pageindex|cursor/i.test([e.id,e.className,e.name,e.getAttribute('onclick'),e.getAttribute('href')].join(' ')) ||
                            (e.matches('a,button,[role=button]') && /^(next|prev|previous|下一頁|上一頁|下頁|上頁|[0-9]+)$/i.test((e.innerText||'').trim())) || e.getAttribute('rel')==='next')
                    };
                }""")
                with error_step('SINOPAC-05-318-001'):
                    ensure_page(LOAN_DETAIL_URL)
                with error_step('SINOPAC-05-319-001'):
                    if not dom['unique']:
                        raise with_error_code(RuntimeError("sinopac-loan-repayments-result-state"), 'SINOPAC-05-319-001')
                with error_step('SINOPAC-05-320-001'):
                    if not dom['visible']:
                        raise with_error_code(RuntimeError("sinopac-loan-repayments-result-state"), 'SINOPAC-05-320-001')
                with error_step('SINOPAC-05-321-001'):
                    if dom['empty']:
                        raise with_error_code(RuntimeError("sinopac-loan-repayments-result-state"), 'SINOPAC-05-321-001')
                with error_step('SINOPAC-05-322-001'):
                    if dom['pager']:
                        raise with_error_code(RuntimeError("sinopac-loan-repayments-result-state"), 'SINOPAC-05-322-001')
                with error_step('SINOPAC-05-323-001'):
                    if dom['headers'] != ['繳款日', '應繳日', '攤還本金', '繳息金額', '本金餘額', '違約金金額', '繳款金額', '交易狀態']:
                        raise with_error_code(RuntimeError("sinopac-loan-repayments-result-state"), 'SINOPAC-05-323-001')
                with error_step('SINOPAC-05-324-001'):
                    if dom['form'] != form:
                        raise with_error_code(RuntimeError("sinopac-loan-repayments-result-state"), 'SINOPAC-05-324-001')
                with error_step('SINOPAC-05-325-001'):
                    if observer.bad:
                        raise with_error_code(RuntimeError("sinopac-loan-repayments-result-state"), 'SINOPAC-05-325-001')
                with error_step('SINOPAC-05-326-001'):
                    if len(responses) != 1:
                        raise with_error_code(RuntimeError("sinopac-loan-repayments-result-state"), 'SINOPAC-05-326-001')
                with error_step('SINOPAC-05-327-001'):
                    if dom['rows'] != [[_plain_text(row[f'DataValue{i}']) for i in (2, 1, 11, 4, 5, 6, 3, 10)] for row in records]:
                        raise with_error_code(RuntimeError("sinopac-loan-repayments-result-rows"), 'SINOPAC-05-327-001')
                with error_step('SINOPAC-05-328-001'):
                    repayment = {
                        "sub_account": record["Sub1_Sub2"], "currency": record["Currency"],
                        "receipt": {"account": account["AcctValue"], "sub_account": record["Sub1_Sub2"],
                                    "currency": record["Currency"], "start": start.isoformat(), "end": end.isoformat(),
                                    "status": "complete", "period": "native_default", "pages": 1, "rows": len(records)},
                        "records": records,
                    }
                with error_step('SINOPAC-05-329-001'):
                    _parse_sinopac_repayment(account["AcctValue"], repayment)
                return repayment
            except Exception as source_error:
                primary_error_id = exception_error_id(source_error, bank="sinopac")
                raise
            finally:
                with error_step('SINOPAC-07-350-001'):
                    page.remove_listener("response", handler)
                with error_step('SINOPAC-07-351-001'):
                    observer.close()
                with error_step('SINOPAC-07-352-001'):
                    collector.attach(page)
        except Exception as source_error:
            raise with_error_code(RuntimeError(error), primary_error_id or exception_error_id(source_error, bank="sinopac") or 'SINOPAC-05-333-001') from None

    def _collect_transactions(self, page, collector: ResponseCollector) -> dict:
        """Collect every authoritative TWD account across complete month windows."""
        with error_step('SINOPAC-05-334-001'):
            deadline = time.monotonic() + 600

        def ensure_deadline() -> None:
            with error_step('SINOPAC-05-335-001'):
                if getattr(self, '_shared_dialog_blocked', False):
                    raise with_error_code(RuntimeError("sinopac-twd-history-dialog"), 'SINOPAC-05-335-001')
            with error_step('SINOPAC-05-336-001'):
                if time.monotonic() >= deadline:
                    raise with_error_code(RuntimeError("sinopac-twd-history-deadline"), 'SINOPAC-05-336-001')

        with error_step('SINOPAC-05-337-001'):
            inventory_boundary = collector.request_sequence
        with error_step('SINOPAC-05-338-001'):
            inventory_issued_before = collector.issued_count("ws_debitacct.ashx")
        with error_step('SINOPAC-05-339-001'):
            inventory_response_before = len(collector.by_endpoint("ws_debitacct.ashx"))
        with error_step('SINOPAC-05-340-001'):
            history_issued_before = collector.issued_count("ws_transdetailMerge.ashx")
        with error_step('SINOPAC-05-341-001'):
            history_response_before = len(collector.by_endpoint("ws_transdetailMerge.ashx"))
        with error_step('SINOPAC-05-342-001'):
            page.goto(
                "https://mma.sinopac.com/mma/bank/transdetail/mma_transdetail.aspx",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
        with error_step('SINOPAC-05-347-001'):
            for _ in range(40):
                with error_step('SINOPAC-05-343-001'):
                    ensure_deadline()
                with error_step('SINOPAC-05-344-001'):
                    candidate = collector.latest("ws_debitacct.ashx")
                with error_step('SINOPAC-05-345-001'):
                    if candidate is not None and candidate.request_sequence > inventory_boundary:
                        break
                with error_step('SINOPAC-05-346-001'):
                    page.wait_for_timeout(500)
        with error_step('SINOPAC-05-348-001'):
            page.wait_for_timeout(500)
        with error_step('SINOPAC-05-349-001'):
            if collector.issued_count('ws_debitacct.ashx') - inventory_issued_before != 1:
                raise with_error_code(RuntimeError("sinopac-twd-history-inventory"), 'SINOPAC-05-349-001')
        with error_step('SINOPAC-05-350-001'):
            inventory = self._twd_inventory(
                collector, after_sequence=inventory_boundary,
            )
        with error_step('SINOPAC-05-351-001'):
            inventory_hit = collector.latest("ws_debitacct.ashx")
        with error_step('SINOPAC-05-352-001'):
            inventory_body_size = getattr(inventory_hit, "body_size", None)
        with error_step('SINOPAC-05-353-001'):
            if type(inventory_body_size) is not int:
                raise with_error_code(RuntimeError("sinopac-twd-history-byte-budget"), 'SINOPAC-05-353-001')
        operation_bytes = inventory_body_size

        with error_step('SINOPAC-05-354-001'):
            handlers = page.evaluate(
                """() => [...document.querySelectorAll('#divDebitAccount [onclick]')]
              .map(e => e.getAttribute('onclick') || '')"""
            )
        with error_step('SINOPAC-05-355-001'):
            twd_handlers = self._validated_twd_handlers(handlers, inventory)

        with error_step('SINOPAC-05-356-001'):
            mode = os.environ.get("BANK_CRAWLER_HISTORY_MODE", "full")
        with error_step('SINOPAC-05-357-001'):
            if mode not in {'full', 'incremental'}:
                raise with_error_code(RuntimeError("sinopac-twd-history-mode"), 'SINOPAC-05-357-001')
        with error_step('SINOPAC-05-358-001'):
            as_of = _taipei_today()
        results = []
        expected = []
        windows_out = []
        operation_rows = 0
        with error_step('SINOPAC-05-440-001'):
            for index, item in enumerate(inventory):
                with error_step('SINOPAC-05-359-001'):
                    ensure_deadline()
                with error_step('SINOPAC-05-360-001'):
                    toggle = page.locator("#spanDebitAccount")
                with error_step('SINOPAC-05-361-001'):
                    if toggle.count() != 1:
                        raise with_error_code(RuntimeError("sinopac-twd-history-account-control"), 'SINOPAC-05-361-001')
                with error_step('SINOPAC-05-362-001'):
                    if not error_step('SINOPAC-05-362-201')(lambda: error_step('SINOPAC-05-362-202')(lambda: toggle.nth(0))().is_visible())():
                        raise with_error_code(RuntimeError("sinopac-twd-history-account-control"), 'SINOPAC-05-362-001')
                with error_step('SINOPAC-05-363-001'):
                    error_step('SINOPAC-05-363-201')(lambda: error_step('SINOPAC-05-363-202')(lambda: toggle.nth(0))().click(timeout=8_000))()
                with error_step('SINOPAC-05-364-001'):
                    page.wait_for_timeout(300)
                with error_step('SINOPAC-05-365-001'):
                    options = page.locator("#divDebitAccount [onclick]")
                with error_step('SINOPAC-05-366-001'):
                    visible_by_position = {
                        position: error_step('SINOPAC-05-366-201')(lambda: options.nth(position))()
                        for position in range(error_step('SINOPAC-05-366-202')(lambda: options.count())())
                        if error_step('SINOPAC-05-366-203')(lambda: error_step('SINOPAC-05-366-204')(lambda: options.nth(position))().is_visible())()
                    }
                positions = [position for position, _ in twd_handlers]
                with error_step('SINOPAC-05-367-001'):
                    if any((position not in visible_by_position for position in positions)):
                        raise with_error_code(RuntimeError("sinopac-twd-history-account-control"), 'SINOPAC-05-367-001')
                with error_step('SINOPAC-05-368-001'):
                    visible = [visible_by_position[position] for position in positions]
                with error_step('SINOPAC-05-369-001'):
                    visible[index].click(timeout=8_000)
                with error_step('SINOPAC-05-370-001'):
                    page.wait_for_timeout(300)
                with error_step('SINOPAC-05-371-001'):
                    selected = page.evaluate(
                        """() => Object.fromEntries(['Acct','AcctValue','Curr','BusinessDate'].map(
                  id => [id, document.getElementById(id)?.value ?? null]))"""
                    )
                with error_step('SINOPAC-05-372-001'):
                    business_date = selected.get("BusinessDate") if isinstance(selected, dict) else None
                with error_step('SINOPAC-05-373-001'):
                    if not isinstance(selected, dict):
                        raise with_error_code(RuntimeError("sinopac-twd-history-account-control"), 'SINOPAC-05-373-001')
                with error_step('SINOPAC-05-374-001'):
                    if selected.get('Acct') != item['label']:
                        raise with_error_code(RuntimeError("sinopac-twd-history-account-control"), 'SINOPAC-05-374-001')
                with error_step('SINOPAC-05-375-001'):
                    if selected.get('AcctValue') != item['identity']:
                        raise with_error_code(RuntimeError("sinopac-twd-history-account-control"), 'SINOPAC-05-375-001')
                with error_step('SINOPAC-05-376-001'):
                    if selected.get('Curr') != item['currency']:
                        raise with_error_code(RuntimeError("sinopac-twd-history-account-control"), 'SINOPAC-05-376-001')
                with error_step('SINOPAC-05-377-001'):
                    if not isinstance(business_date, str):
                        raise with_error_code(RuntimeError("sinopac-twd-history-account-control"), 'SINOPAC-05-377-001')
                with error_step('SINOPAC-05-378-001'):
                    if re.fullmatch('\\d{8}', business_date) is None:
                        raise with_error_code(RuntimeError("sinopac-twd-history-account-control"), 'SINOPAC-05-378-001')

                with error_step('SINOPAC-05-379-001'):
                    history_identity = self._history_identity(item["identity"], item["currency"])
                with error_step('SINOPAC-05-380-001'):
                    start, end = self._history_range(history_identity, end=as_of, mode=mode)
                with error_step('SINOPAC-05-381-001'):
                    expected.append({
                        "identity": history_identity,
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                    })
                with error_step('SINOPAC-05-382-001'):
                    prior_dom_rows: list[list[object]] = []
                with error_step('SINOPAC-05-439-001'):
                    for window_start, window_end in self._history_windows(start, end):
                        with error_step('SINOPAC-05-383-001'):
                            ensure_deadline()
                        with error_step('SINOPAC-05-384-001'):
                            start_control = page.locator("#StartDate")
                        with error_step('SINOPAC-05-385-001'):
                            end_control = page.locator("#EndDate")
                        with error_step('SINOPAC-05-386-001'):
                            button = page.locator("#btnQuery")
                        with error_step('SINOPAC-05-387-001'):
                            for control, error_action in ((start_control, 'SINOPAC-05-700'), (end_control, 'SINOPAC-05-710'), (button, 'SINOPAC-05-720')):
                                with error_step(f'{error_action}-001'):
                                    if control.count() != 1:
                                        raise with_error_code(RuntimeError("sinopac-twd-history-query-control"), f'{error_action}-006')
                                with error_step(f'{error_action}-002'):
                                    if not error_step(f'{error_action}-003')(lambda: control.nth(0))().is_visible():
                                        raise with_error_code(RuntimeError("sinopac-twd-history-query-control"), f'{error_action}-007')
                                with error_step(f'{error_action}-004'):
                                    if not error_step(f'{error_action}-005')(lambda: control.nth(0))().is_enabled():
                                        raise with_error_code(RuntimeError("sinopac-twd-history-query-control"), f'{error_action}-008')
                        with error_step('SINOPAC-05-388-001'):
                            start_text = window_start.strftime("%Y%m%d")
                        with error_step('SINOPAC-05-389-001'):
                            end_text = window_end.strftime("%Y%m%d")
                        with error_step('SINOPAC-05-390-001'):
                            error_step('SINOPAC-05-390-201')(lambda: error_step('SINOPAC-05-390-202')(lambda: start_control.nth(0))().fill(start_text))()
                        with error_step('SINOPAC-05-391-001'):
                            error_step('SINOPAC-05-391-201')(lambda: error_step('SINOPAC-05-391-202')(lambda: end_control.nth(0))().fill(end_text))()
                        with error_step('SINOPAC-05-392-001'):
                            if error_step('SINOPAC-05-392-201')(lambda: error_step('SINOPAC-05-392-202')(lambda: start_control.nth(0))().input_value())() != start_text:
                                raise with_error_code(RuntimeError("sinopac-twd-history-query-control"), 'SINOPAC-05-392-001')
                        with error_step('SINOPAC-05-393-001'):
                            if error_step('SINOPAC-05-393-201')(lambda: error_step('SINOPAC-05-393-202')(lambda: end_control.nth(0))().input_value())() != end_text:
                                raise with_error_code(RuntimeError("sinopac-twd-history-query-control"), 'SINOPAC-05-393-001')

                        with error_step('SINOPAC-05-394-001'):
                            pre_dom_marker = page.evaluate(
                                """() => { const marker=(() => { window.__hermesSinopacObserver?.disconnect();
                      clearTimeout(window.__hermesSinopacObserverTimer);
                      const width=Object.getOwnPropertyDescriptor(HTMLElement.prototype,'offsetWidth').get;
                      const height=Object.getOwnPropertyDescriptor(HTMLElement.prototype,'offsetHeight').get;
                      const rects=Element.prototype.getClientRects;
                      const visible=e => {const boxed=e instanceof HTMLElement
                        ? width.call(e)||height.call(e)||rects.call(e).length : rects.call(e).length;
                        if(!boxed)return false;
                        for(let n=e;n;n=n.parentElement){const s=getComputedStyle(n);
                          if(n.hidden||n.inert||(n.getAttribute('aria-hidden')||'').toLowerCase()==='true'||
                            s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||Number(s.opacity)===0)return false;}
                        return true;};
                      const isEmpty=e => e?.nodeType===1 && !!e.closest?.('#ListingTable') &&
                        e.children.length===0 && (e.textContent||'').trim()==='查無資料';
                      const stale=new WeakSet([...document.querySelectorAll('body *')].filter(isEmpty));
                      const table=document.querySelector('#ListingTable');
                      if(!table)return null;
                      const staleHiddenRows=new WeakMap();
                      for(const row of table.querySelectorAll('tbody tr')) if(!visible(row)) staleHiddenRows.set(row,row.innerHTML);
                      const state={mutations:0,freshEmptyNodes:new WeakSet(),staleHiddenRows}; window.__hermesSinopacState=state;
                      window.__hermesSinopacObserver=new MutationObserver(records => {
                        state.mutations+=records.length;
                        for(const record of records){
                          const nodes=[...record.addedNodes];
                          if(record.target.nodeType===1)nodes.push(record.target);
                          else if(record.target.parentElement)nodes.push(record.target.parentElement);
                          for(const node of nodes){
                            if(node.nodeType!==1)continue;
                            const candidates=[node,...node.querySelectorAll('*')];
                            for(const e of candidates) if(isEmpty(e) &&
                              (record.type==='characterData' || !stale.has(e))) state.freshEmptyNodes.add(e);
                          }
                        }
                      });
                      window.__hermesSinopacObserver.observe(document.body,{subtree:true,childList:true,attributes:true,characterData:true});
                      window.__hermesSinopacObserverTimer=setTimeout(() => {
                        window.__hermesSinopacObserver?.disconnect();
                        delete window.__hermesSinopacObserverTimer;
                        delete window.__hermesSinopacObserver; delete window.__hermesSinopacState;
                        delete window.__hermesSinopacExpectedRows; delete window.__hermesSinopacPriorRows;
                      },35000);
                      const value=table.innerHTML; let hash=2166136261;
                      for(let i=0;i<value.length;i++){hash^=value.charCodeAt(i);hash=Math.imul(hash,16777619);}
                      return [value.length,hash>>>0]; })();
                      return marker===null?{errorId:'SINOPAC-05-781-001'}:marker; }"""
                            )
                        pre_dom_reason = pre_dom_marker.get('errorId') if isinstance(pre_dom_marker, dict) else None
                        if pre_dom_reason == 'SINOPAC-05-781-001':
                            pre_dom_marker = None
                        else:
                            pre_dom_reason = None
                        with error_step('SINOPAC-05-395-001'):
                            before = len(collector.by_endpoint("ws_transdetailMerge.ashx"))
                        with error_step('SINOPAC-05-396-001'):
                            request_boundary = collector.request_sequence
                        with error_step('SINOPAC-05-397-001'):
                            issued_before = collector.issued_count("ws_transdetailMerge.ashx")
                        with error_step('SINOPAC-05-398-001'):
                            ensure_deadline()
                        with error_step('SINOPAC-05-399-001'):
                            error_step('SINOPAC-05-399-201')(lambda: error_step('SINOPAC-05-399-202')(lambda: button.nth(0))().click(timeout=8_000))()
                        with error_step('SINOPAC-05-403-001'):
                            for _ in range(60):
                                with error_step('SINOPAC-05-400-001'):
                                    ensure_deadline()
                                with error_step('SINOPAC-05-401-001'):
                                    if len(collector.by_endpoint("ws_transdetailMerge.ashx")) > before:
                                        break
                                with error_step('SINOPAC-05-402-001'):
                                    page.wait_for_timeout(500)
                        with error_step('SINOPAC-05-404-001'):
                            hits = collector.by_endpoint("ws_transdetailMerge.ashx")[before:]
                        with error_step('SINOPAC-05-405-001'):
                            if len(hits) != 1:
                                raise with_error_code(RuntimeError("sinopac-twd-history-response-cardinality"), 'SINOPAC-05-405-001')
                        with error_step('SINOPAC-05-406-001'):
                            if hits[0].request_sequence <= request_boundary:
                                raise with_error_code(RuntimeError("sinopac-twd-history-response-cardinality"), 'SINOPAC-05-406-001')
                        with error_step('SINOPAC-05-407-001'):
                            validated = self._validate_history_hit(
                                hits[0],
                                label=item["label"],
                                identity=item["identity"],
                                currency=item["currency"],
                                start=window_start,
                                end=window_end,
                                business_date=business_date,
                                as_of=as_of,
                                after_sequence=request_boundary,
                            )
                        with error_step('SINOPAC-05-408-001'):
                            operation_rows += validated["rows"]
                        with error_step('SINOPAC-05-409-001'):
                            if operation_rows > 50000:
                                raise with_error_code(RuntimeError("sinopac-twd-history-row-budget"), 'SINOPAC-05-409-001')
                        with error_step('SINOPAC-05-410-001'):
                            history_body_size = hits[0].body_size
                        with error_step('SINOPAC-05-411-001'):
                            if type(history_body_size) is not int:
                                raise with_error_code(RuntimeError("sinopac-twd-history-byte-budget"), 'SINOPAC-05-411-001')
                        operation_bytes += history_body_size
                        with error_step('SINOPAC-05-412-001'):
                            if operation_bytes > 5000000:
                                raise with_error_code(RuntimeError("sinopac-twd-history-byte-budget"), 'SINOPAC-05-412-001')
                        with error_step('SINOPAC-05-413-001'):
                            page.evaluate(
                                "(rows) => { window.__hermesSinopacExpectedRows = rows.current; window.__hermesSinopacPriorRows = rows.prior; }",
                                {"current": [
                                    [row[f"DataText{i}"] for i in range(1, 12)]
                                    for row in validated["records"]
                                ], "prior": prior_dom_rows},
                            )
                        dom_probe = r"""() => { const width=Object.getOwnPropertyDescriptor(HTMLElement.prototype,'offsetWidth').get;
                  const height=Object.getOwnPropertyDescriptor(HTMLElement.prototype,'offsetHeight').get;
                  const rects=Element.prototype.getClientRects;
                  const visible=e => {const boxed=e instanceof HTMLElement
                    ? width.call(e)||height.call(e)||rects.call(e).length : rects.call(e).length;
                    if(!boxed)return false;
                  for(let n=e;n;n=n.parentElement){const s=getComputedStyle(n);
                    if(n.hidden||n.inert||(n.getAttribute('aria-hidden')||'').toLowerCase()==='true'||
                      s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||Number(s.opacity)===0)return false;}
                  return true;};
                  const pagers=[...document.querySelectorAll(
                    '.pagination,.pager,[class*=pagination i],[class*=pager i],[data-page],[aria-label],[rel],a,button')]
                    .filter(visible).filter(e => e.hasAttribute('data-page') || /pagination|pager/i.test(e.className||'') ||
                      (e.matches('button[onclick],a[onclick]') && /(?:go|set|change|select)?page\s*\(/i.test(e.getAttribute('onclick')||'')) ||
                      (e.matches('a,button') && /^(?:下一頁|上一頁|下頁|上頁|next|previous|prev|first|last|[<>«»])$/i.test((e.textContent||'').replace(/\s+/g,' ').trim())) ||
                      (e.closest('.pagination,.pager,[class*=pagination i],[class*=pager i]') &&
                        /^\d{1,3}$/.test(((e.value||e.textContent)||'').trim())) ||
                      /^(?:下一頁|上一頁|下頁|上頁|next|previous|prev|first|last|[<>«»])$/i.test(
                        (e.textContent||'').replace(/\s+/g,' ').trim()) ||
                      /page|下一|上一|next|previous|prev|first|last/i.test(e.getAttribute('aria-label')||'') ||
                      /^(?:next|prev)$/i.test(e.getAttribute('rel')||'') ||
                      /(?:[?&](?:page|p)=|javascript:.*(?:page|next|prev))/i.test(e.getAttribute('href')||'')).length;
                  const notices=[...document.querySelectorAll(
                    '[role=alert],[role=dialog],[aria-modal=true],dialog[open],.modal.show,.modal.in,progress,[role=progressbar],[class*=spinner],[class*=loading-overlay],.alert,.error,.loading,.busy,[aria-busy=true]')]
                    .filter(visible);
                  const emptyMarker=notices.some(e => (e.textContent||'').trim()==='查無資料') ||
                    [...document.querySelectorAll('body *')].some(e => visible(e) && e.children.length===0 && (e.textContent||'').trim()==='查無資料');
                  const textErrors=[...document.querySelectorAll('body *')].filter(e => visible(e) &&
                    e.children.length===0 &&
                    /系統錯誤|查詢失敗|請稍後|重試|重新整理|連線中斷|disconnected|retry|error|failed/i.test((e.textContent||'').trim())).length;
                  const errors=notices.filter(e =>
                    (e.textContent||'').trim()!=='查無資料' || e.matches('dialog,[role=dialog],[aria-modal=true],.modal.show,.modal.in,progress,[role=progressbar],[class*=spinner],[class*=loading-overlay]')).length + textErrors;
                  const expected=window.__hermesSinopacExpectedRows||[];
                  const normalize=value => { const node=document.createElement('div'); node.innerHTML=String(value||'');
                    return (node.textContent||'').replace(/\s+/g,' ').trim(); };
                  const tables=[...document.querySelectorAll('#ListingTable')];
                  if(tables.length!==1)return {tables:tables.length,rows:0,visibleRows:0,hiddenRows:0,hiddenNonemptyRows:0,hiddenChangedRows:0,hiddenChangedPriorRows:0,hiddenChangedHeaderRows:0,hiddenChangedFullTransactionRows:0,hiddenChangedDateRows:0,hiddenChangedNumericRows:0,hiddenChangedControlRows:0,pagers,errors,visible:false,emptyMarker,freshEmpty:false,bound:false,signature:null,mutations:window.__hermesSinopacState?.mutations||0};
                  const table=tables[0]; const rows=[...table.querySelectorAll('tbody tr')];
                  const visibleRows=rows.filter(visible);
                  const hiddenRows=rows.filter(row=>!visible(row));
                  const hiddenNonemptyRows=hiddenRows.filter(row=>normalize(row.innerHTML)!=='').length;
                  const hiddenHeaderRows=hiddenRows.filter(row=>normalize(row.innerHTML)!==''&&row.querySelectorAll('th').length>0&&row.querySelectorAll('td').length===0).length;
                  const hiddenTransactionRows=hiddenRows.filter(row=>[...row.querySelectorAll('td')].some(cell=>/^\d{2,4}[\/-]\d{1,2}[\/-]\d{1,2}$/.test(normalize(cell.innerHTML))||/^[+-]?(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d+)?$/.test(normalize(cell.innerHTML)))).length;
                  const hiddenChanged=hiddenRows.filter(row=>window.__hermesSinopacState?.staleHiddenRows?.get(row)!==row.innerHTML);
                  const hiddenChangedRows=hiddenChanged.length;
                  const hiddenChangedHeaderRows=hiddenChanged.filter(row=>normalize(row.innerHTML)!==''&&row.querySelectorAll('th').length>0&&row.querySelectorAll('td').length===0).length;
                  const isDate=value=>/^\d{2,4}[\/-]\d{1,2}[\/-]\d{1,2}$/.test(value);
                  const isNumber=value=>/^[+-]?(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d+)?$/.test(value);
                  const hiddenChangedDateRows=hiddenChanged.filter(row=>[...row.querySelectorAll('td')].some(cell=>isDate(normalize(cell.innerHTML)))).length;
                  const hiddenChangedNumericRows=hiddenChanged.filter(row=>[...row.querySelectorAll('td')].some(cell=>isNumber(normalize(cell.innerHTML)))).length;
                  const hiddenChangedFullTransactionRows=hiddenChanged.filter(row=>{const cells=[...row.querySelectorAll('td')];return cells.length>=6&&cells.some(cell=>isDate(normalize(cell.innerHTML)));}).length;
                  const hiddenChangedControlRows=hiddenChanged.filter(row=>row.querySelector('input,select,textarea,button,a[href]')).length;
                  const cellIndexes=[1,2,3,4,5,7];
                  const rowTuple=row=>{const cells=[...row.querySelectorAll('td')];return JSON.stringify(cellIndexes.map(cellIndex=>normalize(cells[cellIndex]?.innerHTML)));};
                  const expectedTuple=expectedRow => {
                    const signedAmount=normalize(expectedRow[3]);
                    const amountCell=signedAmount.startsWith('-')?3:4;
                    const otherAmountCell=amountCell===3?4:3;
                    const responseCells={1:expectedRow[1],2:expectedRow[2],5:expectedRow[4],7:expectedRow[7]};
                    responseCells[amountCell]=signedAmount.replace(/^[+-]/,'');
                    responseCells[otherAmountCell]='';
                    return JSON.stringify(cellIndexes.map(cellIndex=>normalize(responseCells[cellIndex])));};
                  const domTuples=visibleRows.map(rowTuple).sort();
                  const responseTuples=expected.map(expectedTuple).sort();
                  const priorTuples=new Map();
                  for(const tuple of (window.__hermesSinopacPriorRows||[]).map(expectedTuple))
                    priorTuples.set(tuple,(priorTuples.get(tuple)||0)+1);
                  const hiddenChangedPriorRows=hiddenChanged.filter(row=>{const tuple=rowTuple(row), remaining=priorTuples.get(tuple)||0;
                    if(remaining===0)return false; priorTuples.set(tuple,remaining-1); return true;}).length;
                  const soleEmptyRow=visibleRows.length===1&&
                    [...visibleRows[0].querySelectorAll('td')].length===1&&
                    normalize(visibleRows[0].querySelector('td')?.innerHTML)==='查無資料';
                  const emptyCell=soleEmptyRow?visibleRows[0].querySelector('td'):null;
                  const freshEmpty=!!emptyCell&&window.__hermesSinopacState?.freshEmptyNodes.has(emptyCell)===true;
                  const bound=expected.length===0?soleEmptyRow&&hiddenChangedRows===hiddenChangedPriorRows:
                    visibleRows.length===expected.length&&domTuples.length===responseTuples.length&&
                    domTuples.every((value,index)=>value===responseTuples[index]);
                  const value=table.innerHTML; let hash=2166136261;
                  for(let i=0;i<value.length;i++){hash^=value.charCodeAt(i);hash=Math.imul(hash,16777619);}
                  return {tables:1,rows:rows.length,visibleRows:rows.filter(visible).length,
                    hiddenRows:hiddenRows.length,hiddenNonemptyRows,hiddenHeaderRows,hiddenTransactionRows,hiddenChangedRows,hiddenChangedPriorRows,hiddenChangedHeaderRows,hiddenChangedFullTransactionRows,hiddenChangedDateRows,hiddenChangedNumericRows,hiddenChangedControlRows,pagers,errors,visible:visible(table),emptyMarker,freshEmpty,bound,signature:[value.length,hash>>>0],mutations:window.__hermesSinopacState?.mutations||0}; }"""
                        stable_dom = None
                        stable_count = 0
                        last_dom_reason = "unknown"
                        last_dom_detail_id = None
                        with error_step('SINOPAC-05-426-001'):
                            for _ in range(10):
                                with error_step('SINOPAC-05-414-001'):
                                    ensure_deadline()
                                with error_step('SINOPAC-05-415-001'):
                                    page.wait_for_timeout(500)
                                with error_step('SINOPAC-05-416-001'):
                                    dom_state = page.evaluate(dom_probe)
                                with error_step('SINOPAC-05-417-001'):
                                    common_invalid = False
                                    dom_error_id = None
                                    for predicate_error_id, rejected in (
                                        ('SINOPAC-05-600-001', lambda: not isinstance(dom_state, dict)),
                                        ('SINOPAC-05-601-001', lambda: dom_state.get('pagers') != 0),
                                        ('SINOPAC-05-602-001', lambda: dom_state.get('errors') != 0),
                                        ('SINOPAC-05-603-001', lambda: type(dom_state.get('mutations')) is not int),
                                        ('SINOPAC-05-604-001', lambda: dom_state['mutations'] < 0),
                                    ):
                                        with error_step(predicate_error_id):
                                            if rejected():
                                                common_invalid = True
                                                if predicate_error_id is not None:
                                                    dom_error_id = predicate_error_id
                                                break
                                with error_step('SINOPAC-05-420-001'):
                                    if validated["rows"]:
                                        with error_step('SINOPAC-05-418-001'):
                                            invalid = False
                                            for predicate_error_id, rejected in (
                                                (None, lambda: common_invalid),
                                                ('SINOPAC-05-605-001', lambda: dom_state.get('bound') is not True),
                                                ('SINOPAC-05-606-001', lambda: dom_state['mutations'] <= 0),
                                                ('SINOPAC-05-607-001', lambda: dom_state.get('tables') != 1),
                                                ('SINOPAC-05-608-001', lambda: dom_state.get('visible') is not True),
                                                ('SINOPAC-05-609-001', lambda: dom_state.get('emptyMarker') is not False),
                                                ('SINOPAC-05-610-001', lambda: dom_state.get('freshEmpty') is not False),
                                                ('SINOPAC-05-611-001', lambda: type(dom_state.get('rows')) is not int),
                                                ('SINOPAC-05-612-001', lambda: not validated['rows'] <= dom_state['rows'] <= validated['rows'] + 1),
                                                ('SINOPAC-05-613-001', lambda: dom_state.get('visibleRows') != validated['rows']),
                                                ('SINOPAC-05-614-001', lambda: not isinstance(dom_state.get('signature'), list)),
                                                ('SINOPAC-05-615-001', lambda: len(dom_state['signature']) != 2),
                                                ('SINOPAC-05-616-001', lambda: any((type(value) is not int for value in dom_state['signature']))),
                                                ('SINOPAC-05-617-001', lambda: dom_state['signature'] == pre_dom_marker),
                                            ):
                                                with error_step(predicate_error_id):
                                                    if rejected():
                                                        invalid = True
                                                        if predicate_error_id is not None:
                                                            dom_error_id = predicate_error_id
                                                        break
                                    else:
                                        with error_step('SINOPAC-05-419-001'):
                                            invalid = False
                                            for predicate_error_id, rejected in (
                                                (None, lambda: common_invalid),
                                                ('SINOPAC-05-618-001', lambda: dom_state.get('bound') is not True),
                                                ('SINOPAC-05-619-001', lambda: dom_state.get('tables') != 1),
                                                ('SINOPAC-05-620-001', lambda: type(dom_state.get('rows')) is not int),
                                                ('SINOPAC-05-621-001', lambda: type(dom_state.get('visibleRows')) is not int),
                                                ('SINOPAC-05-622-001', lambda: type(dom_state.get('hiddenRows')) is not int),
                                                ('SINOPAC-05-623-001', lambda: type(dom_state.get('hiddenNonemptyRows')) is not int),
                                                ('SINOPAC-05-624-001', lambda: type(dom_state.get('hiddenChangedRows')) is not int),
                                                ('SINOPAC-05-625-001', lambda: type(dom_state.get('hiddenChangedPriorRows')) is not int),
                                                ('SINOPAC-05-626-001', lambda: dom_state['rows'] < 1),
                                                ('SINOPAC-05-627-001', lambda: dom_state['rows'] != dom_state['visibleRows'] + dom_state['hiddenRows']),
                                                ('SINOPAC-05-628-001', lambda: dom_state['hiddenChangedRows'] != dom_state['hiddenChangedPriorRows']),
                                                ('SINOPAC-05-629-001', lambda: dom_state.get('visibleRows') != 1),
                                                ('SINOPAC-05-630-001', lambda: dom_state.get('visible') is not True),
                                                ('SINOPAC-05-631-001', lambda: dom_state.get('emptyMarker') is not True),
                                                ('SINOPAC-05-632-001', lambda: not isinstance(dom_state.get('signature'), list)),
                                                ('SINOPAC-05-633-001', lambda: len(dom_state['signature']) != 2),
                                                ('SINOPAC-05-634-001', lambda: any((type(value) is not int for value in dom_state['signature']))),
                                            ):
                                                with error_step(predicate_error_id):
                                                    if rejected():
                                                        invalid = True
                                                        if predicate_error_id is not None:
                                                            dom_error_id = predicate_error_id
                                                        break
                                with error_step('SINOPAC-05-424-001'):
                                    if invalid:
                                        last_dom_detail_id = dom_error_id
                                        with error_step('SINOPAC-05-423-001'):
                                            if not isinstance(dom_state, dict):
                                                last_dom_reason = "shape"
                                            elif dom_state.get("pagers") != 0:
                                                last_dom_reason = "pager"
                                            elif dom_state.get("errors") != 0:
                                                last_dom_reason = "error"
                                            elif type(dom_state.get("mutations")) is not int:
                                                last_dom_reason = "mutation-shape"
                                            elif dom_state["mutations"] < 0:
                                                last_dom_reason = "mutation-range"
                                            elif dom_state.get("tables") != 1:
                                                last_dom_reason = "table-count"
                                            elif dom_state.get("visible") is not True:
                                                last_dom_reason = "table-visibility"
                                            elif validated["rows"]:
                                                with error_step('SINOPAC-05-421-001'):
                                                    last_dom_reason = (
                                                        "nonempty-binding"
                                                        if dom_state.get("bound") is not True
                                                        else "nonempty-attestation"
                                                    )
                                            elif type(dom_state.get("rows")) is not int or dom_state["rows"] < 1:
                                                last_dom_reason = "empty-no-row"
                                            elif dom_state.get("hiddenChangedRows") != 0:
                                                with error_step('SINOPAC-05-422-001'):
                                                    last_dom_reason = _sinopac_hidden_changed_reason(dom_state)
                                            elif dom_state.get("rows") != (
                                                dom_state.get("visibleRows", 0) + dom_state.get("hiddenRows", 0)
                                            ):
                                                last_dom_reason = "empty-extra-hidden-rows"
                                            elif dom_state.get("visibleRows") != 1:
                                                last_dom_reason = "empty-multiple-visible-rows"
                                            elif dom_state.get("emptyMarker") is not True:
                                                last_dom_reason = "empty-marker"
                                            elif dom_state.get("bound") is not True:
                                                last_dom_reason = "empty-binding"
                                            else:
                                                last_dom_reason = "empty-signature"
                                        stable_dom = None
                                        stable_count = 0
                                        continue
                                stable_count = stable_count + 1 if dom_state == stable_dom else 1
                                stable_dom = dom_state
                                if stable_count == 2:
                                    break
                            else:
                                raise with_error_code(RuntimeError(
                                    f"sinopac-twd-history-result-table-{last_dom_reason}"
                                ), (pre_dom_reason if last_dom_reason == 'table-count' else None) or (last_dom_detail_id if last_dom_reason in ('nonempty-attestation', 'empty-signature') else None) or {
                                    'unknown': 'SINOPAC-05-500-001',
                                    'shape': 'SINOPAC-05-501-001',
                                    'pager': 'SINOPAC-05-502-001',
                                    'error': 'SINOPAC-05-503-001',
                                    'mutation-shape': 'SINOPAC-05-504-001',
                                    'mutation-range': 'SINOPAC-05-505-001',
                                    'table-count': 'SINOPAC-05-506-001',
                                    'table-visibility': 'SINOPAC-05-507-001',
                                    'nonempty-binding': 'SINOPAC-05-508-001',
                                    'nonempty-attestation': 'SINOPAC-05-509-001',
                                    'empty-no-row': 'SINOPAC-05-510-001',
                                    'empty-hidden-full-transaction': 'SINOPAC-05-511-001',
                                    'empty-hidden-date-cell': 'SINOPAC-05-512-001',
                                    'empty-hidden-numeric-cell': 'SINOPAC-05-513-001',
                                    'empty-hidden-control': 'SINOPAC-05-514-001',
                                    'empty-hidden-header': 'SINOPAC-05-515-001',
                                    'empty-hidden-template-text': 'SINOPAC-05-516-001',
                                    'empty-extra-hidden-rows': 'SINOPAC-05-517-001',
                                    'empty-multiple-visible-rows': 'SINOPAC-05-518-001',
                                    'empty-marker': 'SINOPAC-05-519-001',
                                    'empty-binding': 'SINOPAC-05-520-001',
                                    'empty-signature': 'SINOPAC-05-521-001',
                                }.get(last_dom_reason, 'SINOPAC-05-500-001'))
                        with error_step('SINOPAC-05-427-001'):
                            ensure_deadline()
                        with error_step('SINOPAC-05-428-001'):
                            final_dom = page.evaluate(
                                "async () => { const probe = " + dom_probe + "; let result=null,errorId=null;"
                                " const cleanup=()=>{window.__hermesSinopacObserver?.disconnect();"
                                " clearTimeout(window.__hermesSinopacObserverTimer);"
                                " delete window.__hermesSinopacObserverTimer;"
                                " delete window.__hermesSinopacObserver;"
                                " delete window.__hermesSinopacState;"
                                " delete window.__hermesSinopacExpectedRows; delete window.__hermesSinopacPriorRows;};"
                                " try { for(let attempt=0;attempt<3;attempt++){"
                                " window.__hermesSinopacObserver?.takeRecords();"
                                " const beforeMutations=window.__hermesSinopacState?.mutations;"
                                " result=probe(); await new Promise(resolve=>setTimeout(resolve,0));"
                                " const records=window.__hermesSinopacObserver?.takeRecords()||[];"
                                " const afterMutations=window.__hermesSinopacState?.mutations;"
                                " const settled=probe();"
                                " if((Number.isInteger(beforeMutations)||(errorId='SINOPAC-05-782-001',false))"
                                " &&(afterMutations===beforeMutations||(errorId='SINOPAC-05-782-002',false))"
                                " &&(records.length===0||(errorId='SINOPAC-05-782-003',false))"
                                " &&(JSON.stringify(settled)===JSON.stringify(result)||(errorId='SINOPAC-05-782-004',false))){"
                                " return settled; }"
                                " result=settled;"
                                " }"
                                " return {errorId}; } finally { cleanup(); } }"
                            )
                        final_dom_reason = final_dom.get('errorId') if isinstance(final_dom, dict) else None
                        if final_dom_reason in ('SINOPAC-05-782-001', 'SINOPAC-05-782-002', 'SINOPAC-05-782-003', 'SINOPAC-05-782-004'):
                            final_dom = None
                        else:
                            final_dom_reason = None
                        with error_step('SINOPAC-05-429-001'):
                            if final_dom != stable_dom:
                                raise with_error_code(RuntimeError("sinopac-twd-history-result-table"), final_dom_reason or 'SINOPAC-05-429-001')
                        with error_step('SINOPAC-05-430-001'):
                            prior_dom_rows.extend(
                                [row[f"DataText{i}"] for i in range(1, 12)]
                                for row in validated["records"]
                            )
                        with error_step('SINOPAC-05-431-001'):
                            ensure_deadline()
                        with error_step('SINOPAC-05-432-001'):
                            final_hits = collector.by_endpoint("ws_transdetailMerge.ashx")[before:]
                        with error_step('SINOPAC-05-433-001'):
                            if len(final_hits) != 1:
                                raise with_error_code(RuntimeError("sinopac-twd-history-response-cardinality"), 'SINOPAC-05-433-001')
                        with error_step('SINOPAC-05-434-001'):
                            if final_hits[0] is not hits[0]:
                                raise with_error_code(RuntimeError("sinopac-twd-history-response-cardinality"), 'SINOPAC-05-434-001')
                        with error_step('SINOPAC-05-435-001'):
                            if collector.issued_count('ws_transdetailMerge.ashx') - issued_before != 1:
                                raise with_error_code(RuntimeError("sinopac-twd-history-response-cardinality"), 'SINOPAC-05-435-001')
                        with error_step('SINOPAC-05-436-001'):
                            receipt = {
                                "identity": history_identity,
                                "start": window_start.isoformat(),
                                "end": window_end.isoformat(),
                                "status": validated["status"],
                                "pages": 1,
                                "rows": validated["rows"],
                            }
                        with error_step('SINOPAC-05-437-001'):
                            results.append({
                                "account": item["identity"],
                                "account_name": item["label"],
                                "currency": item["currency"],
                                "records": validated["records"],
                                "receipt": receipt,
                            })
                        with error_step('SINOPAC-05-438-001'):
                            windows_out.append({key: receipt[key] for key in (
                                "identity", "start", "end", "status", "pages",
                            )})

        with error_step('SINOPAC-05-441-001'):
            ensure_deadline()
        with error_step('SINOPAC-05-445-001'):
            if not inventory:
                with error_step('SINOPAC-05-442-001'):
                    blockers = page.evaluate(
                        r"""() => { const visible=e => !!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);
                  const pagers=[...document.querySelectorAll('.pagination,.pager,[class*=pagination i],[class*=pager i],[data-page],[aria-label],[rel],a,button')].filter(visible).filter(e =>
                    e.hasAttribute('data-page') || /pagination|pager/i.test(e.className||'') ||
                    (e.matches('button[onclick],a[onclick]') && /(?:go|set|change|select)?page\s*\(/i.test(e.getAttribute('onclick')||'')) ||
                    (e.closest('.pagination,.pager,[class*=pagination i],[class*=pager i]') && /^\d{1,3}$/.test(((e.value||e.textContent)||'').trim())) ||
                    /next|previous|prev|first|last|下一|上一|page/i.test(e.getAttribute('aria-label')||'') ||
                    /^(?:next|prev)$/i.test(e.getAttribute('rel')||'') ||
                    /(?:[?&](?:page|p)=|javascript:.*(?:page|next|prev))/i.test(e.getAttribute('href')||'') ||
                    (e.matches('a,button') && /^(?:下一頁|上一頁|next|previous|prev|first|last|[<>«»])$/i.test((e.textContent||'').trim())).length;
                  const notices=[...document.querySelectorAll('dialog[open],[aria-modal=true],.modal.show,.modal.in,progress,[role=progressbar],[class*=spinner],[class*=loading-overlay],[role=alert],[role=dialog],.alert,.error,.loading,.busy,[aria-busy=true]')].filter(visible).length;
                  const textNotices=[...document.querySelectorAll('body *')].filter(e => visible(e) &&
                    e.children.length===0 && /系統錯誤|查詢失敗|請稍後|重試|重新整理|連線中斷|disconnected|retry|error|failed/i.test((e.textContent||'').trim())).length;
                  const tables=document.querySelectorAll('table').length;
                  return pagers+notices+textNotices+tables; }"""
                    )
                with error_step('SINOPAC-05-443-001'):
                    ensure_deadline()
                with error_step('SINOPAC-05-444-001'):
                    if blockers != 0:
                        raise with_error_code(RuntimeError("sinopac-twd-history-empty-inventory-blocked"), 'SINOPAC-05-444-001')
        with error_step('SINOPAC-05-446-001'):
            final_inventory_hits = collector.by_endpoint("ws_debitacct.ashx")[inventory_response_before:]
        with error_step('SINOPAC-05-447-001'):
            final_history_hits = collector.by_endpoint("ws_transdetailMerge.ashx")[history_response_before:]
        with error_step('SINOPAC-05-448-001'):
            if collector.issued_count('ws_debitacct.ashx') - inventory_issued_before != 1:
                raise with_error_code(RuntimeError("sinopac-twd-history-operation-cardinality"), 'SINOPAC-05-448-001')
        with error_step('SINOPAC-05-449-001'):
            if collector.issued_count('ws_transdetailMerge.ashx') - history_issued_before != len(windows_out):
                raise with_error_code(RuntimeError("sinopac-twd-history-operation-cardinality"), 'SINOPAC-05-449-001')
        with error_step('SINOPAC-05-450-001'):
            if len(final_inventory_hits) != 1:
                raise with_error_code(RuntimeError("sinopac-twd-history-operation-cardinality"), 'SINOPAC-05-450-001')
        with error_step('SINOPAC-05-451-001'):
            if len(final_history_hits) != len(windows_out):
                raise with_error_code(RuntimeError("sinopac-twd-history-operation-cardinality"), 'SINOPAC-05-451-001')
        with error_step('SINOPAC-05-452-001'):
            if any((type(hit.body_size) is not int for hit in final_history_hits)):
                raise with_error_code(RuntimeError("sinopac-twd-history-operation-cardinality"), 'SINOPAC-05-452-001')
        with error_step('SINOPAC-05-453-001'):
            if operation_bytes != inventory_body_size + sum((hit.body_size for hit in final_history_hits if type(hit.body_size) is int)):
                raise with_error_code(RuntimeError("sinopac-twd-history-operation-cardinality"), 'SINOPAC-05-453-001')

        domain = {
            "domain": "account_transactions",
            "expected": expected,
            "windows": windows_out,
        }
        with error_step('SINOPAC-05-455-001'):
            if not expected:
                with error_step('SINOPAC-05-454-001'):
                    domain["empty_window"] = {
                        "start": self._history_floor(as_of).isoformat(),
                        "end": as_of.isoformat(),
                        "status": "explicit_empty",
                        "pages": 1,
                    }
        with error_step('SINOPAC-05-456-001'):
            coverage = {
                "mode": mode,
                "as_of": as_of.isoformat(),
                "domains": [domain],
            }
        with error_step('SINOPAC-05-457-001'):
            validate_history_coverage(
                coverage,
                expected_mode=mode,
                expected_domains=self.HISTORY_COVERAGE_DOMAINS,
            )
        return {"results": results, "inventory": inventory, "coverage": coverage}

    # ---------- 信用卡明細 ----------
    def _collect_card_statements(self, page) -> list:
        """信用卡帳單已請款明細（SinoCard/Account/StatementInquiry）。

        此頁 SSR：整頁 HTML 含 12 個月切換按鈕 + 當月所有消費紀錄。每月一次 goto 即可。
        我們抓最近 3 個月。回傳 list[{month, html_text, records}]。
        """
        results = []
        try:
            with error_step('SINOPAC-06-200-001'):
                page.goto("https://mma.sinopac.com/SinoCard/Account/StatementInquiry",
                          wait_until="domcontentloaded", timeout=20000)
            with error_step('SINOPAC-06-201-001'):
                page.wait_for_timeout(5000)
        except Exception:
            _log("[card_stmt] page_navigation_failed")
            return results

        # 抓「快速查詢」3 個月按鈕——通常是當月與前兩月
        with error_step('SINOPAC-06-202-001'):
            quick_months_raw = page.evaluate("""() => {
            const tds = Array.from(document.querySelectorAll('td, a, span, button, li'));
            return tds.map(e => (e.innerText||'').trim())
              .filter(t => /^20\\d{2}\\/\\d{2}$/.test(t))
              .slice(0, 12);
        }""")
        # 去重：快速查詢和下方 dropdown 各列一份，但只取最近 3 個 unique month
        with error_step('SINOPAC-06-203-001'):
            seen = set()
        quick_months = []
        with error_step('SINOPAC-06-208-001'):
            for m in quick_months_raw:
                with error_step('SINOPAC-06-206-001'):
                    if m not in seen:
                        with error_step('SINOPAC-06-204-001'):
                            seen.add(m)
                        with error_step('SINOPAC-06-205-001'):
                            quick_months.append(m)
                with error_step('SINOPAC-06-207-001'):
                    if len(quick_months) >= 3:
                        break
        _log(f"[card_stmt] 可選月份 (unique): {quick_months}")

        # 抓當前頁面（預設顯示最新月份）的整個內文
        def _grab_month(month_label: str | None) -> dict:
            with error_step('SINOPAC-06-209-001'):
                content_text = page.evaluate("""() => document.body.innerText""")
            # 用 regex 抓「消費記錄」表格區塊
            import re
            recs = []
            # 每筆形如: YYYY/MM/DD\tYYYY/MM/DD\t末四碼4\t說明\t金額\t...
            with error_step('SINOPAC-06-210-001'):
                pat = re.compile(
                    r"(20\d{2}/\d{2}/\d{2})\t(20\d{2}/\d{2}/\d{2})\t(\d{4})\t([^\t\n]+?)\t(-?[\d,]+)",
                    re.MULTILINE,
                )
            with error_step('SINOPAC-06-212-001'):
                for m in pat.finditer(content_text):
                    with error_step('SINOPAC-06-211-001'):
                        recs.append({
                            "trans_date": m.group(1),
                            "post_date": m.group(2),
                            "card_last4": m.group(3),
                            "description": m.group(4).strip(),
                            "amount": m.group(5).replace(",", ""),
                        })
            # 抓帳單彙總
            with error_step('SINOPAC-06-213-001'):
                sum_pat = re.search(
                    r"臺幣\t([\d,]+)\t([\d,]+)\t([\d,]+)\t([\d,]+)\t([\d,]+)\t([\d,]+)\t([\d,]+)",
                    content_text,
                )
            summary = None
            with error_step('SINOPAC-06-215-001'):
                if sum_pat:
                    with error_step('SINOPAC-06-214-001'):
                        summary = {
                            "currency": "TWD",
                            "last_billed": sum_pat.group(1).replace(",", ""),
                            "paid": sum_pat.group(2).replace(",", ""),
                            "new_charges": sum_pat.group(3).replace(",", ""),
                            "revolving_interest": sum_pat.group(4).replace(",", ""),
                            "penalty": sum_pat.group(5).replace(",", ""),
                            "current_due": sum_pat.group(6).replace(",", ""),
                            "min_due": sum_pat.group(7).replace(",", ""),
                        }
            # 抓結帳日 / 繳款截止日
            with error_step('SINOPAC-06-216-001'):
                due_pat = re.search(r"結帳日：(\d{4}/\d{2}/\d{2})\s*繳款截止日：(\d{4}/\d{2}/\d{2})", content_text)
            with error_step('SINOPAC-06-217-001'):
                return {
                    "month": month_label,
                    "billing_cycle_date": due_pat.group(1) if due_pat else None,
                    "payment_due_date": due_pat.group(2) if due_pat else None,
                    "summary": summary,
                    "records": recs,
                    "record_count": len(recs),
                }

        # 目前月（預設展示）
        with error_step('SINOPAC-06-218-001'):
            results.append(_grab_month(quick_months[0] if quick_months else None))
        # 切換到前兩個月（如果有）
        with error_step('SINOPAC-06-222-001'):
            for label in quick_months[1:3]:
                try:
                    with error_step('SINOPAC-06-219-001'):
                        page.get_by_text(label, exact=True).first.click(timeout=4000)
                    with error_step('SINOPAC-06-220-001'):
                        page.wait_for_timeout(4500)
                    with error_step('SINOPAC-06-221-001'):
                        results.append(_grab_month(label))
                except Exception:
                    _log("[card_stmt] month_switch_failed")

        with error_step('SINOPAC-06-223-001'):
            total = sum(r.get("record_count", 0) for r in results)
        _log(f"[card_stmt] 抓 {len(results)} 個月、共 {total} 筆消費紀錄")
        return results

    def _collect_card_unbilled(self, page, collector) -> dict:
        """信用卡未請款明細（SinoCard/Account/UnbilledTxInquiry）。

        此頁靠 POST API：LatestTx（最新交易）+ OutstandingDetail（已請款合計）。
        collector 攔 sinopac.com 全域，已自動收下。
        """
        try:
            with error_step('SINOPAC-06-224-001'):
                page.goto("https://mma.sinopac.com/SinoCard/Account/UnbilledTxInquiry",
                          wait_until="domcontentloaded", timeout=20000)
            with error_step('SINOPAC-06-225-001'):
                page.wait_for_timeout(7000)
        except Exception:
            _log("[card_unbilled] page_navigation_failed")
            return {}

        with error_step('SINOPAC-06-226-001'):
            latest = self._latest_json(collector, "LatestTx")
        with error_step('SINOPAC-06-227-001'):
            outstand = self._latest_json(collector, "OutstandingDetail")
        out = {
            "latest_tx": latest,
            "outstanding_detail": outstand,
        }
        n_latest = 0
        with error_step('SINOPAC-06-230-001'):
            if isinstance(latest, dict):
                with error_step('SINOPAC-06-228-001'):
                    items = (latest.get("Result") or {}).get("Items", [])
                with error_step('SINOPAC-06-229-001'):
                    n_latest = len(items) if isinstance(items, list) else 0
        n_outstand = 0
        with error_step('SINOPAC-06-233-001'):
            if isinstance(outstand, dict):
                with error_step('SINOPAC-06-231-001'):
                    detail = (outstand.get("Result") or {}).get("Detail", [])
                with error_step('SINOPAC-06-232-001'):
                    n_outstand = len(detail) if isinstance(detail, list) else 0
        _log(f"[card_unbilled] LatestTx={n_latest} 筆, OutstandingDetail={n_outstand} 筆")
        return out

    @staticmethod
    def _latest_json(collector: ResponseCollector, endpoint: str):
        """取某 endpoint 最新一次回應的 JSON body（dict 或 list）。"""
        with error_step('SINOPAC-07-342-001'):
            hit = collector.latest(endpoint)
        with error_step('SINOPAC-07-343-001'):
            return hit.resp_json if hit else None


if __name__ == "__main__":
    crawler = SinopacCrawler()
    try:
        result = crawler.run(login_url=BASE, headless=True)
    except SinopacLoginError:
        result = {"error": "login_failed_stop"}

    out_file = Path(__file__).resolve().parents[1] / "data" / "sinopac_collected.json"
    write_private_json(out_file, result)
    _log(f"\n[done] 已存: {out_file}")

    if result.get("error"):
        _log("  ❌ error: crawler_failed")
    else:
        data = result.get("data", {})
        _log("\n===== 抓取摘要 =====")
        bb = data.get("bank_balance") or []
        n_acct = sum(len(s.get("SubInfo", [])) for s in bb if isinstance(s, dict))
        _log(f"  銀行帳戶: {n_acct} 個")
        cs = data.get("card_summary") or []
        _log(f"  信用卡彙總: {len(cs) if isinstance(cs, list) else 0} 筆")
        ac = data.get("all_cards") or {}
        if isinstance(ac, dict):
            items = (ac.get("Result") or {}).get("Items", [])
            _log(f"  全卡片: {len(items)} 張")
