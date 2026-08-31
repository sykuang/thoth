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
    return html.unescape(re.sub(r"<[^>]*>", "", value)).replace("\xa0", " ").strip()


LOAN_DETAIL_URL = "https://mma.sinopac.com/mma/bank/easy_index_loan/mma_detail.aspx"
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
    summary_rows = out.get("card_summary") or []
    if summary_rows and isinstance(summary_rows[0], dict):
        for group in summary_rows[0].get("SubInfo") or []:
            if isinstance(group, list):
                for row in group:
                    if isinstance(row, dict) and row.get("DataText"):
                        kv[row["DataText"]] = row.get("DataValue")
    statements = out.get("card_statements") or []
    latest = statements[0] if statements and isinstance(statements[0], dict) else {}
    latest_summary = latest.get("summary") if isinstance(latest, dict) else {}
    remaining = kv.get("本期應繳")
    if remaining is None and isinstance(latest_summary, dict):
        remaining = latest_summary.get("current_due")

    payment_amount = kv.get("最近繳款金額")
    payment_date = kv.get("最近繳款日期")
    if card_bill_money(payment_amount) is None or card_bill_date(payment_date) is None:
        payment_amount = None
        payment_date = None
    return make_card_bill_fact(
        remaining_due=remaining,
        statement_close_date=kv.get("結帳日") or latest.get("billing_cycle_date"),
        payment_due_date=kv.get("繳款截止日") or latest.get("payment_due_date"),
        last_payment_amount=payment_amount,
        last_payment_date=payment_date,
    )


class SinopacCrawler(BankCrawler):
    USES_SHARED_LOGIN_CHECKPOINTS: ClassVar[bool] = True
    CREDENTIAL_HOSTS = frozenset({"mma.sinopac.com"})
    CAPTCHA_INVALID = "captcha_invalid"
    CREDENTIALS_INVALID = "credentials_invalid"
    LOGIN_FAILED = "login_failed"
    HISTORY_COVERAGE_REQUIRED: ClassVar[bool] = True
    HISTORY_COVERAGE_DOMAINS: ClassVar[frozenset[str]] = frozenset({
        "twd_transactions",
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
            current = urlparse(page.url or "")
            path = (current.path or "").lower()
            if (
                not self._exact_https_origin_allowed(
                    page.url, frozenset({"mma.sinopac.com"})
                )
                or "mmalogin.aspx" in path
                or not path.startswith(
                    ("/mymma/", "/myasset/", "/mma_", "/mma/mymma/")
                )
            ):
                return False
            for scope in self._page_scopes(page):
                captcha_images = scope.locator(SEL_CAP_IMG)
                if any(
                    captcha_images.nth(index).is_visible()
                    for index in range(captcha_images.count())
                ):
                    return False
                inputs = scope.locator("input")
                for index in range(inputs.count()):
                    field = inputs.nth(index)
                    if (
                        field.is_visible()
                        and field.get_attribute("maxlength") in {"6", "11", "20"}
                    ):
                        return False
            body = page.locator("body").inner_text()
        except Exception:
            return False
        return (
            len(body) >= 500
            and "登出" in body
            and ("資產總覽" in body or "資產分析" in body or "我的帳戶" in body)
        )

    def login(self, page) -> bool:
        return self._shared_login(page)

    def prepare_login_page(self, page) -> None:
        try:
            page.wait_for_timeout(8000)
        except Exception:
            raise SinopacLoginError(
                self.LOGIN_FAILED,
                "永豐登入頁面無法安全準備；未送出登入",
            ) from None

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
    def _captcha_image(page, *, enabled: bool = False):
        images = page.locator(SEL_CAP_IMG)
        visible = [
            images.nth(index)
            for index in range(images.count())
            if images.nth(index).is_visible()
            and (not enabled or images.nth(index).is_enabled())
        ]
        return visible[0] if len(visible) == 1 else None

    @staticmethod
    def _keyboard_fill(page, field, value: str) -> None:
        field.click()
        field.click(click_count=3)
        page.keyboard.press("Backspace")
        page.keyboard.type(value, delay=80)
        if len(field.input_value()) != len(value):
            raise SinopacLoginError(
                SinopacCrawler.LOGIN_FAILED,
                "永豐登入欄位輸入長度不符；未送出登入",
            )

    def prepare_captcha_resubmit(self, page) -> None:
        try:
            image = self._captcha_image(page, enabled=True)
            if image is None:
                raise SinopacLoginError(
                    self.CAPTCHA_INVALID,
                    "無法安全更新永豐驗證碼；未送出登入",
                )
            image.click()
            page.wait_for_timeout(1500)
        except SinopacLoginError:
            raise
        except Exception:
            raise SinopacLoginError(
                self.CAPTCHA_INVALID,
                "無法安全更新永豐驗證碼；未送出登入",
            ) from None

    def _ocr_captcha(self, page, max_attempts=5):
        attempts = min(max(max_attempts, 1), 5)
        for attempt in range(attempts):
            try:
                if self._captcha_image(page) is None:
                    return None
                wait_captcha_stable(page, SEL_CAP_IMG, tmp_path=self.captcha_tmp)
                text = solve_captcha(
                    page,
                    SEL_CAP_IMG,
                    expected_len=6,
                    alnum_only=True,
                    digits_only=True,
                    min_confidence=0.98,
                    tmp_path=self.captcha_tmp,
                )
                if isinstance(text, str) and len(text) == 6 and text.isdigit():
                    return text
            except Exception:
                pass
            if attempt + 1 < attempts:
                try:
                    image = self._captcha_image(page, enabled=True)
                    if image is None:
                        return None
                    image.click()
                    page.wait_for_timeout(1500)
                except Exception:
                    return None
        return None

    @classmethod
    def _response_visible(cls, page) -> bool:
        for scope in cls._page_scopes(page):
            for selector in (
                ".modal.show",
                "[role='dialog']",
                ".error",
                ".alert",
                "[role='alert']",
            ):
                matches = scope.locator(selector)
                if any(
                    matches.nth(index).is_visible()
                    for index in range(matches.count())
                ):
                    return True
        return False

    def submit_credentials_once(self, page) -> None:
        try:
            page.wait_for_selector(SEL_CAP_IMG, state="visible", timeout=10000)
            inputs = page.locator("input")
            groups = {6: [], 11: [], 20: []}
            for index in range(inputs.count()):
                field = inputs.nth(index)
                if not field.is_visible():
                    continue
                maxlength = field.get_attribute("maxlength")
                if maxlength in {"6", "11", "20"}:
                    groups[int(maxlength)].append(field)
            if tuple(len(groups[length]) for length in (11, 20, 6)) != (1, 2, 1):
                raise SinopacLoginError(
                    self.LOGIN_FAILED,
                    "永豐登入欄位無法安全確認；未送出登入",
                )
            ordered_twenty = []
            for field in groups[20]:
                box = field.bounding_box()
                if box is None:
                    raise SinopacLoginError(
                        self.LOGIN_FAILED,
                        "永豐登入欄位無法安全確認；未送出登入",
                    )
                ordered_twenty.append((box["y"], field))
            ordered_twenty.sort(key=lambda item: item[0])
            if ordered_twenty[0][0] == ordered_twenty[1][0]:
                raise SinopacLoginError(
                    self.LOGIN_FAILED,
                    "永豐登入欄位無法安全確認；未送出登入",
                )
            fields = (
                groups[11][0],
                ordered_twenty[0][1],
                ordered_twenty[1][1],
                groups[6][0],
            )
            if any(not field.is_enabled() for field in fields):
                raise SinopacLoginError(
                    self.LOGIN_FAILED,
                    "永豐登入欄位無法安全確認；未送出登入",
                )
            for field, value in zip(
                fields[:3],
                (self.creds.national_id, self.creds.user_code, self.creds.password),
                strict=True,
            ):
                self._keyboard_fill(page, field, value)
            captcha = self._ocr_captcha(page, max_attempts=5)
            if captcha is None:
                raise SinopacLoginError(
                    self.CAPTCHA_INVALID,
                    "永豐驗證碼辨識失敗；未送出登入",
                )
            self._keyboard_fill(page, fields[3], captcha)

            candidates = page.locator("#MMA_Login")
            eligible = []
            for index in range(candidates.count()):
                candidate = candidates.nth(index)
                if not candidate.is_visible() or not candidate.is_enabled():
                    continue
                label = " ".join(
                    ((candidate.inner_text() or candidate.get_attribute("value") or "")).split()
                )
                if label == "登入":
                    eligible.append(candidate)
            if len(eligible) != 1:
                raise SinopacLoginError(
                    self.LOGIN_FAILED,
                    "找不到唯一且可操作的永豐登入按鈕；未送出登入",
                )
            button = eligible[0]
        except SinopacLoginError:
            raise
        except Exception:
            raise SinopacLoginError(
                self.LOGIN_FAILED,
                "永豐登入欄位無法安全填寫；未送出登入",
            ) from None

        try:
            button.click(timeout=8000)
        except Exception:
            raise SinopacLoginError(
                self.LOGIN_FAILED,
                "永豐登入送出狀態不明；禁止自動重試",
            ) from None

        try:
            for _ in range(8):
                page.wait_for_timeout(1000)
                if self._logged_in(page) or self._response_visible(page):
                    return
        except Exception:
            raise SinopacLoginError(
                self.LOGIN_FAILED,
                "永豐登入送出後狀態無法安全確認；禁止自動重試",
            ) from None

    # ---------- 抓取 ----------
    @staticmethod
    def _history_floor(end: date) -> date:
        try:
            return end.replace(year=end.year - 1) + timedelta(days=1)
        except ValueError:
            return end.replace(year=end.year - 1, day=28) + timedelta(days=1)

    @staticmethod
    def _history_windows(start: date, end: date) -> list[tuple[date, date]]:
        if start > end:
            raise RuntimeError("sinopac-twd-history-range")
        windows = []
        cursor = start
        while cursor <= end:
            window_end = min(end, date(
                cursor.year, cursor.month, monthrange(cursor.year, cursor.month)[1],
            ))
            windows.append((cursor, window_end))
            cursor = window_end + timedelta(days=1)
        return windows

    def _history_range(
        self, identity: str, *, end: date, mode: str,
    ) -> tuple[date, date]:
        floor = self._history_floor(end)
        cursor = self.transaction_cursors.get("twd_transactions", {}).get(identity)
        if isinstance(cursor, date) and cursor > end:
            raise RuntimeError("sinopac-twd-history-cursor")
        if mode == "full":
            return floor, end
        if mode != "incremental":
            raise RuntimeError("sinopac-twd-history-mode")
        start = max(floor, cursor - timedelta(days=7)) if isinstance(cursor, date) else floor
        return start, end

    @staticmethod
    def _exact_hit_url(hit, path: str, *, numeric_query: bool) -> bool:
        parsed = urlparse(hit.url)
        raw = urlparse(hit.raw_url or hit.url)
        return (
            parsed.scheme == raw.scheme == "https"
            and parsed.hostname == raw.hostname == "mma.sinopac.com"
            and parsed.port in (None, 443)
            and raw.port in (None, 443)
            and parsed.username is parsed.password is raw.username is raw.password is None
            and parsed.path == raw.path == path
            and parsed.params == raw.params == ""
            and parsed.fragment == raw.fragment == ""
            and parsed.query == ""
            and (not numeric_query or re.fullmatch(r"\d{10,16}", raw.query or "") is not None)
        )

    @classmethod
    def _twd_inventory(
        cls, collector: ResponseCollector, *, after_sequence: int = 0,
    ) -> list[dict]:
        candidates = [
            candidate for candidate in collector.by_endpoint("ws_debitacct.ashx")
            if type(candidate.request_sequence) is int
            and candidate.request_sequence > after_sequence
        ]
        if len(candidates) != 1:
            raise RuntimeError("sinopac-twd-history-inventory")
        hit = candidates[0]
        payload = hit.resp_json if hit else None
        body = payload[0] if isinstance(payload, list) and len(payload) == 1 else None
        rows = body.get("SubInfo") if isinstance(body, dict) else None
        if (
            hit is None
            or type(hit.request_sequence) is not int
            or hit.request_sequence <= after_sequence
            or hit.main_frame_request is not True
            or hit.method != "POST"
            or hit.req_body not in (None, "")
            or hit.status != 200
            or hit.redirected
            or type(hit.body_size) is not int
            or not 0 <= hit.body_size <= 5_000_000
            or hit.content_type.split(";", 1)[0].strip().lower() != "application/json"
            or not cls._exact_hit_url(hit, cls._TWD_INVENTORY_PATH, numeric_query=True)
            or not isinstance(body, dict)
            or set(body) != {"Header", "Message", "SubInfo"}
            or body.get("Header") != "SUCCESS"
            or body.get("Message") not in (None, "")
            or not isinstance(rows, list)
        ):
            raise RuntimeError("sinopac-twd-history-inventory")
        inventory = []
        seen_labels: set[str] = set()
        seen_identities: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"DataText", "DataValue", "DisplayText"}:
                raise RuntimeError("sinopac-twd-history-inventory")
            label = row.get("DataText")
            identity = row.get("DataValue")
            currency = row.get("DisplayText")
            if (
                not isinstance(label, str) or not label or label != label.strip()
                or not isinstance(identity, str) or re.fullmatch(r"\d{14}", identity) is None
                or currency != "TWD"
                or label in seen_labels or identity in seen_identities
            ):
                raise RuntimeError("sinopac-twd-history-inventory")
            seen_labels.add(label)
            seen_identities.add(identity)
            inventory.append({"label": label, "identity": identity, "currency": currency})
        return inventory

    @staticmethod
    def _yyyymmdd(value, error: str) -> date:
        if not isinstance(value, str) or re.fullmatch(r"\d{8}", value) is None:
            raise RuntimeError(error)
        try:
            return datetime.strptime(value, "%Y%m%d").date()
        except ValueError:
            raise RuntimeError(error) from None

    @staticmethod
    def _history_amount(value, error: str) -> Decimal:
        if not isinstance(value, str):
            raise RuntimeError(error)
        text = re.sub(r"<[^>]*>", "", value).strip()
        if re.fullmatch(r"[+-]?(?:0|[1-9]\d*|[1-9]\d{0,2}(?:,\d{3})+)", text) is None:
            raise RuntimeError(error)
        try:
            amount = Decimal(text.replace(",", ""))
        except InvalidOperation:
            raise RuntimeError(error) from None
        if not amount.is_finite() or amount != amount.to_integral_value() or abs(amount) > Decimal("2147483647"):
            raise RuntimeError(error)
        return amount

    @classmethod
    def _validate_history_row(cls, row, *, start: date, end: date) -> None:
        error = "sinopac-twd-history-row"
        if not isinstance(row, dict) or set(row) != cls._HISTORY_ROW_KEYS:
            raise RuntimeError(error)
        if any(not isinstance(row[key], str) for key in cls._HISTORY_ROW_KEYS):
            raise RuntimeError(error)
        if (
            not re.fullmatch(r"\d{4}/\d{2}/\d{2}<br />\d{2}:\d{2}", row["DataText1"])
            or not re.fullmatch(r"\d{4}/\d{2}/\d{2}", row["DataText2"])
        ):
            raise RuntimeError(error)
        raw_datetime = re.sub(
            r"<br\s*/?>", " ", row["DataText1"], flags=re.IGNORECASE,
        ).strip()
        try:
            transacted = datetime.strptime(raw_datetime, "%Y/%m/%d %H:%M")
            account_date = datetime.strptime(row["DataText2"].strip(), "%Y/%m/%d").date()
        except ValueError:
            raise RuntimeError(error) from None
        if not start <= transacted.date() <= end or not start <= account_date <= end:
            raise RuntimeError(error)
        description = _plain_text(row["DataText3"])
        if not description or len(description) > 500:
            raise RuntimeError(error)
        cls._history_amount(row["DataText4"], error)
        cls._history_amount(row["DataText5"], error)
        if any(len(row[f"DataText{i}"]) > 2_000 for i in range(6, 12)):
            raise RuntimeError(error)

    @classmethod
    def _validate_history_hit(
        cls, hit, *, label: str, identity: str, currency: str, start: date, end: date,
        business_date: str, as_of: date, after_sequence: int = 0,
    ) -> dict:
        error = "sinopac-twd-history-response"
        params = parse_qs(hit.req_body, keep_blank_values=True) if isinstance(hit.req_body, str) else {}
        payload = hit.resp_json
        body = payload[0] if isinstance(payload, list) and len(payload) == 1 else None
        if (
            type(hit.request_sequence) is not int
            or hit.request_sequence <= after_sequence
            or hit.main_frame_request is not True
            or hit.method != "POST"
            or hit.status != 200
            or hit.redirected
            or type(hit.body_size) is not int
            or not 0 <= hit.body_size <= 5_000_000
            or hit.content_type.split(";", 1)[0].strip().lower() != "application/json"
            or not cls._exact_hit_url(hit, cls._TWD_HISTORY_PATH, numeric_query=True)
            or set(params) != cls._HISTORY_FORM_KEYS
            or params.get("Acct") != [label]
            or params.get("AcctName") != [""]
            or params.get("AcctValue") != [identity]
            or params.get("Curr") != [currency]
            or params.get("CurrName") != [""]
            or params.get("StartDate") != [start.strftime("%Y%m%d")]
            or params.get("EndDate") != [end.strftime("%Y%m%d")]
            or params.get("QueryType") != ["3"]
            or params.get("TextType") != [""]
            or params.get("BusinessDate") != [business_date]
            or not isinstance(body, dict)
            or set(body) != cls._HISTORY_RESPONSE_KEYS
            or body.get("Header") != "SUCCESS"
            or body.get("MaxMonth") != "3"
        ):
            raise RuntimeError(error)
        parsed_business_date = cls._yyyymmdd(business_date, error)
        begin = cls._yyyymmdd(body.get("BeginDate"), error)
        response_end = cls._yyyymmdd(body.get("EndDate"), error)
        default_begin = cls._yyyymmdd(body.get("DefBeginDate"), error)
        default_end = cls._yyyymmdd(body.get("DefEndDate"), error)
        if (
            parsed_business_date > as_of
            or begin > start
            or response_end < end
            or not begin <= default_begin <= default_end <= response_end
            or body.get("isOBU") not in (None, "Y", "N")
        ):
            raise RuntimeError(error)
        head_info = body.get("HeadInfo")
        if (
            not isinstance(head_info, list)
            or len(head_info) != 9
            or any(not isinstance(item, dict) for item in head_info)
            or any(set(item) != {
                "DataAlign", "DetailShow", "FieldKey", "FieldWidth", "HeadAlign",
                "HeadText", "MainShow", "OrderIndex",
            } for item in head_info)
            or any(not all(isinstance(value, str) for value in item.values()) for item in head_info)
            or [item.get("FieldKey") for item in head_info] != [
                f"DataText{i}" for i in range(1, 10)
            ]
        ):
            raise RuntimeError(error)
        orders = [item["OrderIndex"] for item in head_info]
        if orders not in (
            [str(i) for i in range(9)],
            [str(i) for i in range(1, 10)],
        ):
            raise RuntimeError(error)
        for item in head_info:
            if (
                item["DataAlign"].lower() not in {"", "l", "r", "c", "left", "right", "center"}
                or item["HeadAlign"].lower() not in {"", "l", "r", "c", "left", "right", "center"}
                or item["MainShow"].lower() not in {"", "0", "1", "y", "n", "true", "false"}
                or item["DetailShow"].lower() not in {"", "0", "1", "y", "n", "true", "false"}
                or not item["FieldWidth"].isdigit()
                or not 0 <= int(item["FieldWidth"]) <= 1000
                or not item["HeadText"].strip()
                or len(item["HeadText"]) > 50
            ):
                raise RuntimeError(error)
        rows = body.get("SubInfo")
        if not isinstance(rows, list) or len(rows) > 10_000:
            raise RuntimeError(error)
        if not rows:
            if body.get("Message") != "查無資料" or body.get("RecordCount") is not None:
                raise RuntimeError(error)
            return {"records": [], "status": "explicit_empty", "rows": 0}
        if body.get("Message") not in (None, "") or body.get("RecordCount") != "0":
            raise RuntimeError(error)
        seen_rows = set()
        for row in rows:
            cls._validate_history_row(row, start=start, end=end)
            fingerprint = tuple(row[f"DataText{i}"] for i in range(1, 12))
            if fingerprint in seen_rows:
                raise RuntimeError(error)
            seen_rows.add(fingerprint)
        return {"records": rows, "status": "complete", "rows": len(rows)}

    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        """登入後抓帳戶餘額 / 貸款明細 / 信用卡彙總與帳單 / 全卡片 / 資產分析。

        永豐 MMA 資產總覽頁登入後自動觸發 ws_bankbal/cardsum/cardbilling_sp/
        AllCards/ws_mychart，巡訪「資產分析 / 信用卡總覽」頁也會補打。
        台幣交易明細 dropdown 是 jQuery 客製化元件（#divDebitAccount），待下次破。
        """
        out: dict = {}
        page.wait_for_timeout(5000)

        # 巡訪「資產分析 / 信用卡總覽」頁觸發更多 API
        for url in [
            "https://mma.sinopac.com/MyMMA/Myasset/mma_assets_analysis.aspx",
            "https://mma.sinopac.com/mma/mymma/myasset/cards_summary.aspx",
        ]:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(6000)
            except Exception:
                _log("[collect] page_navigation_failed")

        # 從 collector 取已攔到的 API JSON
        out["bank_balance"] = self._latest_json(collector, "ws_bankbal.ashx")        # 銀行帳戶餘額 list
        out["debit_accounts"] = self._latest_json(collector, "ws_debitacct.ashx")    # 扣款帳戶清單
        out["card_summary"] = self._latest_json(collector, "ws_cardsum.ashx")        # 信用卡彙總
        out["card_billing"] = self._latest_json(collector, "ws_cardbilling_sp.ashx") # 信用卡 3 個月帳單
        out["all_cards"] = self._latest_json(collector, "AllCards")                  # 全卡清單
        out["asset_chart"] = self._latest_json(collector, "ws_mychart.ashx")         # 資產分佈圓餅
        out["alert_info"] = self._latest_json(collector, "ws_alertinfo.ashx")        # 帳戶通知

        # === 貸款明細：每個貸款帳號查本金餘額 / 利率 / 到期日 ===
        out["loan"] = self._collect_loans(page, collector)

        # === 台幣交易明細：權威帳戶 inventory + 月窗 coverage ===
        twd_history = self._collect_transactions(page, collector)
        out["twd_transactions"] = twd_history["results"]
        out["debit_accounts"] = twd_history["inventory"]
        out["history_coverage"] = twd_history["coverage"]

        # === 信用卡明細：帳單已請款（StatementInquiry HTML）+ 未請款（UnbilledTxInquiry API）===
        out["card_statements"] = self._collect_card_statements(page)
        out["card_unbilled"] = self._collect_card_unbilled(page, collector)

        # 偵測尚未抓到的（log 給 debug 用）
        miss = [k for k, v in out.items() if v is None]
        if miss:
            _log(f"[collect] 未攔到: {miss}")

        publish_card_bill_facts(out, [_sinopac_card_bill_fact(out)])

        out["_all_endpoints"] = sorted({h.endpoint for h in collector.hits if h.resp_json})
        return BankCollectResult(**out)

    def _collect_loans(self, page, collector: ResponseCollector) -> dict:
        """逐帳號觸發 ws_loaninfo，回傳銀行原生貸款明細。"""
        page.goto(LOAN_DETAIL_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(8000)

        account_raw = self._latest_json(collector, "ws_loanaccount.ashx")
        if not (isinstance(account_raw, list) and account_raw
                and isinstance(account_raw[0], dict)
                and isinstance(account_raw[0].get("SubInfo"), list)):
            raise RuntimeError("永豐貸款帳號 API 未回傳預期結構")
        accounts = account_raw[0]["SubInfo"]
        details = []
        for account in accounts:
            if not isinstance(account, dict):
                raise RuntimeError("永豐貸款帳號資料格式錯誤")
            account_no = account.get("AcctValue")
            formatted = account.get("AcctValueFormat")
            if not account_no or not formatted:
                raise RuntimeError("永豐貸款帳號缺少 AcctValue/AcctValueFormat")

            before = len(collector.by_endpoint("ws_loaninfo.ashx"))
            clicked = page.evaluate(
                """args => {
                  const account = document.querySelector('#AcctValue');
                  const formatted = document.querySelector('#AcctValueFormat');
                  const button = document.querySelector('#btnQuery');
                  if (!account || !formatted || !button) return false;
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
            if not clicked:
                raise RuntimeError("永豐貸款查詢控制項不存在")
            page.wait_for_timeout(5000)

            hits = collector.by_endpoint("ws_loaninfo.ashx")[before:]
            matching_hits = []
            for hit in hits:
                if not isinstance(hit.req_body, str):
                    continue
                params = parse_qs(hit.req_body, keep_blank_values=True)
                if (params.get("AcctValue") == [account_no]
                        and params.get("AcctValueFormat") == [formatted]):
                    matching_hits.append(hit)
            if not matching_hits:
                raise RuntimeError("永豐貸款查詢未收到對應 API 回應")
            hit = matching_hits[-1]
            if not 200 <= hit.status < 300:
                raise RuntimeError("永豐貸款明細 API HTTP 回應失敗")
            info_raw = hit.resp_json
            if not (isinstance(info_raw, list) and info_raw
                    and isinstance(info_raw[0], dict)
                    and isinstance(info_raw[0].get("SubInfo"), list)):
                raise RuntimeError("永豐貸款明細 API 未回傳預期結構")
            body = info_raw[0]
            records = body["SubInfo"]
            required = ("LoanKind", "Currency", "LoanBalance")
            if body.get("Message") or not records or any(
                not isinstance(record, dict)
                or any(record.get(key) in (None, "") for key in required)
                for record in records
            ):
                raise RuntimeError("永豐貸款明細缺少必要欄位或銀行回覆失敗")
            details.append({
                "account": account_no,
                "records": records,
            })
        return {"details": details, "fetch_ok": True}

    def _collect_transactions(self, page, collector: ResponseCollector) -> dict:
        """Collect every authoritative TWD account across complete month windows."""
        deadline = time.monotonic() + 600

        def ensure_deadline() -> None:
            if getattr(self, "_shared_dialog_blocked", False):
                raise RuntimeError("sinopac-twd-history-dialog")
            if time.monotonic() >= deadline:
                raise RuntimeError("sinopac-twd-history-deadline")

        inventory_boundary = collector.request_sequence
        inventory_issued_before = collector.issued_count("ws_debitacct.ashx")
        inventory_response_before = len(collector.by_endpoint("ws_debitacct.ashx"))
        history_issued_before = collector.issued_count("ws_transdetailMerge.ashx")
        history_response_before = len(collector.by_endpoint("ws_transdetailMerge.ashx"))
        page.goto(
            "https://mma.sinopac.com/mma/bank/transdetail/mma_transdetail.aspx",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        for _ in range(40):
            ensure_deadline()
            candidate = collector.latest("ws_debitacct.ashx")
            if candidate is not None and candidate.request_sequence > inventory_boundary:
                break
            page.wait_for_timeout(500)
        page.wait_for_timeout(500)
        if collector.issued_count("ws_debitacct.ashx") - inventory_issued_before != 1:
            raise RuntimeError("sinopac-twd-history-inventory")
        inventory = self._twd_inventory(
            collector, after_sequence=inventory_boundary,
        )
        inventory_hit = collector.latest("ws_debitacct.ashx")
        inventory_body_size = getattr(inventory_hit, "body_size", None)
        if type(inventory_body_size) is not int:
            raise RuntimeError("sinopac-twd-history-byte-budget")
        operation_bytes = inventory_body_size

        handlers = page.evaluate(
            """() => [...document.querySelectorAll('#divDebitAccount [onclick]')]
              .map(e => e.getAttribute('onclick') || '')"""
        )
        if not isinstance(handlers, list) or len(handlers) != len(inventory):
            raise RuntimeError("sinopac-twd-history-account-control")
        pattern = re.compile(
            r"^setDebitAccount\('([^'\r\n]*)',\s*'(\d{14})',\s*'([A-Z]{3})'\)\s*;?$"
        )
        for handler, item in zip(handlers, inventory, strict=True):
            match = pattern.fullmatch(handler) if isinstance(handler, str) else None
            if match is None or match.groups() != (
                item["label"], item["identity"], item["currency"],
            ):
                raise RuntimeError("sinopac-twd-history-account-control")

        mode = os.environ.get("BANK_CRAWLER_HISTORY_MODE", "full")
        if mode not in {"full", "incremental"}:
            raise RuntimeError("sinopac-twd-history-mode")
        as_of = _taipei_today()
        results = []
        expected = []
        windows_out = []
        operation_rows = 0
        for index, item in enumerate(inventory):
            ensure_deadline()
            toggle = page.locator("#spanDebitAccount")
            if toggle.count() != 1 or not toggle.nth(0).is_visible():
                raise RuntimeError("sinopac-twd-history-account-control")
            toggle.nth(0).click(timeout=8_000)
            page.wait_for_timeout(300)
            options = page.locator("#divDebitAccount [onclick]")
            visible = [
                options.nth(i) for i in range(options.count()) if options.nth(i).is_visible()
            ]
            if len(visible) != len(inventory):
                raise RuntimeError("sinopac-twd-history-account-control")
            visible[index].click(timeout=8_000)
            page.wait_for_timeout(300)
            selected = page.evaluate(
                """() => Object.fromEntries(['Acct','AcctValue','Curr','BusinessDate'].map(
                  id => [id, document.getElementById(id)?.value ?? null]))"""
            )
            business_date = selected.get("BusinessDate") if isinstance(selected, dict) else None
            if (
                not isinstance(selected, dict)
                or selected.get("Acct") != item["label"]
                or selected.get("AcctValue") != item["identity"]
                or selected.get("Curr") != item["currency"]
                or not isinstance(business_date, str)
                or re.fullmatch(r"\d{8}", business_date) is None
            ):
                raise RuntimeError("sinopac-twd-history-account-control")

            start, end = self._history_range(item["identity"], end=as_of, mode=mode)
            expected.append({
                "identity": item["identity"],
                "start": start.isoformat(),
                "end": end.isoformat(),
            })
            for window_start, window_end in self._history_windows(start, end):
                ensure_deadline()
                start_control = page.locator("#StartDate")
                end_control = page.locator("#EndDate")
                button = page.locator("#btnQuery")
                if any(
                    control.count() != 1
                    or not control.nth(0).is_visible()
                    or not control.nth(0).is_enabled()
                    for control in (start_control, end_control, button)
                ):
                    raise RuntimeError("sinopac-twd-history-query-control")
                start_text = window_start.strftime("%Y%m%d")
                end_text = window_end.strftime("%Y%m%d")
                start_control.nth(0).fill(start_text)
                end_control.nth(0).fill(end_text)
                if (
                    start_control.nth(0).input_value() != start_text
                    or end_control.nth(0).input_value() != end_text
                ):
                    raise RuntimeError("sinopac-twd-history-query-control")

                pre_dom_marker = page.evaluate(
                    """() => { window.__hermesSinopacObserver?.disconnect();
                      clearTimeout(window.__hermesSinopacObserverTimer);
                      const isEmpty=e => e?.nodeType===1 && e.children.length===0 && (e.textContent||'').trim()==='查無資料';
                      const stale=new WeakSet([...document.querySelectorAll('body *')].filter(isEmpty));
                      const state={mutations:0,freshEmpty:false}; window.__hermesSinopacState=state;
                      window.__hermesSinopacObserver=new MutationObserver(records => {
                        state.mutations+=records.length;
                        for(const record of records){
                          const nodes=[...record.addedNodes];
                          if(record.target.nodeType===1)nodes.push(record.target);
                          else if(record.target.parentElement)nodes.push(record.target.parentElement);
                          for(const node of nodes){
                            if(node.nodeType!==1)continue;
                            const candidates=[node,...node.querySelectorAll('*')];
                            if(candidates.some(e => isEmpty(e) &&
                              (record.type==='characterData' || !stale.has(e)))) state.freshEmpty=true;
                          }
                        }
                      });
                      window.__hermesSinopacObserver.observe(document.body,{subtree:true,childList:true,attributes:true,characterData:true});
                      window.__hermesSinopacObserverTimer=setTimeout(() => {
                        window.__hermesSinopacObserver?.disconnect();
                        delete window.__hermesSinopacObserver; delete window.__hermesSinopacState;
                        delete window.__hermesSinopacExpectedRows;
                      },35000);
                      const table=document.querySelector('#ListingTable');
                      if(!table)return null; const value=table.innerHTML; let hash=2166136261;
                      for(let i=0;i<value.length;i++){hash^=value.charCodeAt(i);hash=Math.imul(hash,16777619);}
                      return [value.length,hash>>>0]; }"""
                )
                before = len(collector.by_endpoint("ws_transdetailMerge.ashx"))
                request_boundary = collector.request_sequence
                issued_before = collector.issued_count("ws_transdetailMerge.ashx")
                ensure_deadline()
                button.nth(0).click(timeout=8_000)
                for _ in range(60):
                    ensure_deadline()
                    if len(collector.by_endpoint("ws_transdetailMerge.ashx")) > before:
                        break
                    page.wait_for_timeout(500)
                hits = collector.by_endpoint("ws_transdetailMerge.ashx")[before:]
                if (
                    len(hits) != 1
                    or hits[0].request_sequence <= request_boundary
                ):
                    raise RuntimeError("sinopac-twd-history-response-cardinality")
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
                operation_rows += validated["rows"]
                if operation_rows > 50_000:
                    raise RuntimeError("sinopac-twd-history-row-budget")
                history_body_size = hits[0].body_size
                if type(history_body_size) is not int:
                    raise RuntimeError("sinopac-twd-history-byte-budget")
                operation_bytes += history_body_size
                if operation_bytes > 5_000_000:
                    raise RuntimeError("sinopac-twd-history-byte-budget")
                page.evaluate(
                    "(rows) => { window.__hermesSinopacExpectedRows = rows; }",
                    [
                        [row[f"DataText{i}"] for i in range(1, 10)]
                        for row in validated["records"]
                    ],
                )
                dom_probe = r"""() => { const visible=e => !!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);
                  const pagers=[...document.querySelectorAll(
                    '.pagination,.pager,[class*=pagination i],[class*=pager i],[data-page],[aria-label],[rel],[onclick],[name*=page i],[id*=page i],input,select,option,a,button')]
                    .filter(e => e.hasAttribute('data-page') || /pagination|pager/i.test(e.className||'') ||
                      /page/i.test((e.getAttribute('name')||'')+' '+(e.id||'')) ||
                      (e.matches('input,select,option') && /^(?:下一頁|上一頁|下頁|上頁|next|previous|prev|first|last|[<>«»])$/i.test((e.value||'').trim())) ||
                      (e.matches('input[type=button],input[type=submit],select,option') && /^\d{1,3}$/.test((e.value||'').trim())) ||
                      /^(?:下一頁|上一頁|下頁|上頁|next|previous|prev|first|last|[<>«»]|\d{1,3})$/i.test(
                        (e.textContent||'').replace(/\s+/g,' ').trim()) ||
                      /page|下一|上一|next|previous|prev|first|last/i.test(e.getAttribute('aria-label')||'') ||
                      /^(?:next|prev)$/i.test(e.getAttribute('rel')||'') ||
                      /(?:page|next|prev|first|last|下一|上一)/i.test(e.getAttribute('onclick')||'') ||
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
                  if(tables.length!==1)return {tables:tables.length,rows:0,visibleRows:0,pagers,errors,visible:false,emptyMarker,freshEmpty:window.__hermesSinopacState?.freshEmpty===true,bound:expected.length===0,signature:null,mutations:window.__hermesSinopacState?.mutations||0};
                  const table=tables[0]; const rows=[...table.querySelectorAll('tbody tr')];
                  const bound=expected.length>0 && rows.length===expected.length*2 && expected.every((row,rowIndex) => {
                    const cells=[...rows[rowIndex*2].querySelectorAll('td')].map(cell => normalize(cell.innerHTML));
                    return cells.length===row.length && row.every((value,index) => {
                      const text=normalize(value); return cells[index]===text; }); });
                  const value=table.innerHTML; let hash=2166136261;
                  for(let i=0;i<value.length;i++){hash^=value.charCodeAt(i);hash=Math.imul(hash,16777619);}
                  return {tables:1,rows:rows.length,visibleRows:rows.filter(visible).length,
                    pagers,errors,visible:visible(table),emptyMarker,freshEmpty:window.__hermesSinopacState?.freshEmpty===true,bound,signature:[value.length,hash>>>0],mutations:window.__hermesSinopacState?.mutations||0}; }"""
                stable_dom = None
                stable_count = 0
                for _ in range(10):
                    ensure_deadline()
                    page.wait_for_timeout(500)
                    dom_state = page.evaluate(dom_probe)
                    common_invalid = (
                        not isinstance(dom_state, dict)
                        or dom_state.get("pagers") != 0
                        or dom_state.get("errors") != 0
                        or dom_state.get("bound") is not True
                        or type(dom_state.get("mutations")) is not int
                        or dom_state["mutations"] <= 0
                    )
                    if validated["rows"]:
                        invalid = (
                            common_invalid
                            or dom_state.get("tables") != 1
                            or dom_state.get("visible") is not True
                            or dom_state.get("emptyMarker") is not False
                            or dom_state.get("freshEmpty") is not False
                            or type(dom_state.get("rows")) is not int
                            or dom_state["rows"] != validated["rows"] * 2
                            or dom_state.get("visibleRows") != dom_state["rows"]
                            or not isinstance(dom_state.get("signature"), list)
                            or len(dom_state["signature"]) != 2
                            or any(type(value) is not int for value in dom_state["signature"])
                            or dom_state["signature"] == pre_dom_marker
                        )
                    else:
                        invalid = (
                            common_invalid
                            or dom_state.get("tables") != 0
                            or dom_state.get("rows") != 0
                            or dom_state.get("visibleRows") != 0
                            or dom_state.get("emptyMarker") is not True
                            or dom_state.get("freshEmpty") is not True
                            or dom_state.get("signature") is not None
                        )
                    if invalid:
                        stable_dom = None
                        stable_count = 0
                        continue
                    stable_count = stable_count + 1 if dom_state == stable_dom else 1
                    stable_dom = dom_state
                    if stable_count == 2:
                        break
                else:
                    raise RuntimeError("sinopac-twd-history-result-table")
                ensure_deadline()
                final_dom = page.evaluate(
                    "() => { const probe = " + dom_probe + "; const result=probe();"
                    " window.__hermesSinopacObserver?.disconnect();"
                    " clearTimeout(window.__hermesSinopacObserverTimer);"
                    " delete window.__hermesSinopacObserverTimer;"
                    " delete window.__hermesSinopacObserver;"
                    " delete window.__hermesSinopacState;"
                    " delete window.__hermesSinopacExpectedRows; return result; }"
                )
                if final_dom != stable_dom:
                    raise RuntimeError("sinopac-twd-history-result-table")
                ensure_deadline()
                final_hits = collector.by_endpoint("ws_transdetailMerge.ashx")[before:]
                if (
                    len(final_hits) != 1
                    or final_hits[0] is not hits[0]
                    or collector.issued_count("ws_transdetailMerge.ashx") - issued_before != 1
                ):
                    raise RuntimeError("sinopac-twd-history-response-cardinality")
                receipt = {
                    "identity": item["identity"],
                    "start": window_start.isoformat(),
                    "end": window_end.isoformat(),
                    "status": validated["status"],
                    "pages": 1,
                    "rows": validated["rows"],
                }
                results.append({
                    "account": item["identity"],
                    "account_name": item["label"],
                    "currency": item["currency"],
                    "records": validated["records"],
                    "receipt": receipt,
                })
                windows_out.append({key: receipt[key] for key in (
                    "identity", "start", "end", "status", "pages",
                )})

        ensure_deadline()
        if not inventory:
            blockers = page.evaluate(
                r"""() => { const visible=e => !!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);
                  const pagers=[...document.querySelectorAll('.pagination,.pager,[class*=pagination i],[class*=pager i],[data-page],[aria-label],[rel],[onclick],[name*=page i],[id*=page i],input,select,option,a,button')].filter(e =>
                    e.hasAttribute('data-page') || /pagination|pager/i.test(e.className||'') ||
                    /page/i.test((e.getAttribute('name')||'')+' '+(e.id||'')) ||
                    /next|previous|prev|first|last|下一|上一|page/i.test(e.getAttribute('aria-label')||'') ||
                    /^(?:next|prev)$/i.test(e.getAttribute('rel')||'') ||
                    /(?:page|next|prev|first|last|下一|上一)/i.test((e.getAttribute('onclick')||'')+(e.getAttribute('href')||'')) ||
                    (e.matches('input,select,option') && /^(?:下一頁|上一頁|next|previous|prev|first|last|[<>«»]|\d{1,3})$/i.test((e.value||'').trim())) ||
                    /^(?:下一頁|上一頁|next|previous|prev|first|last|[<>«»]|\d{1,3})$/i.test((e.textContent||'').trim())).length;
                  const notices=[...document.querySelectorAll('dialog[open],[aria-modal=true],.modal.show,.modal.in,progress,[role=progressbar],[class*=spinner],[class*=loading-overlay],[role=alert],[role=dialog],.alert,.error,.loading,.busy,[aria-busy=true]')].filter(visible).length;
                  const textNotices=[...document.querySelectorAll('body *')].filter(e => visible(e) &&
                    e.children.length===0 && /系統錯誤|查詢失敗|請稍後|重試|重新整理|連線中斷|disconnected|retry|error|failed/i.test((e.textContent||'').trim())).length;
                  const tables=document.querySelectorAll('table').length;
                  return pagers+notices+textNotices+tables; }"""
            )
            ensure_deadline()
            if blockers != 0:
                raise RuntimeError("sinopac-twd-history-empty-inventory-blocked")
        final_inventory_hits = collector.by_endpoint("ws_debitacct.ashx")[inventory_response_before:]
        final_history_hits = collector.by_endpoint("ws_transdetailMerge.ashx")[history_response_before:]
        if (
            collector.issued_count("ws_debitacct.ashx") - inventory_issued_before != 1
            or collector.issued_count("ws_transdetailMerge.ashx") - history_issued_before != len(windows_out)
            or len(final_inventory_hits) != 1
            or len(final_history_hits) != len(windows_out)
            or any(type(hit.body_size) is not int for hit in final_history_hits)
            or operation_bytes != inventory_body_size + sum(
                hit.body_size for hit in final_history_hits if type(hit.body_size) is int
            )
        ):
            raise RuntimeError("sinopac-twd-history-operation-cardinality")

        domain = {
            "domain": "twd_transactions",
            "expected": expected,
            "windows": windows_out,
        }
        if not expected:
            domain["empty_window"] = {
                "start": self._history_floor(as_of).isoformat(),
                "end": as_of.isoformat(),
                "status": "explicit_empty",
                "pages": 1,
            }
        coverage = {
            "mode": mode,
            "as_of": as_of.isoformat(),
            "domains": [domain],
        }
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
            page.goto("https://mma.sinopac.com/SinoCard/Account/StatementInquiry",
                      wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(5000)
        except Exception:
            _log("[card_stmt] page_navigation_failed")
            return results

        # 抓「快速查詢」3 個月按鈕——通常是當月與前兩月
        quick_months_raw = page.evaluate("""() => {
            const tds = Array.from(document.querySelectorAll('td, a, span, button, li'));
            return tds.map(e => (e.innerText||'').trim())
              .filter(t => /^20\\d{2}\\/\\d{2}$/.test(t))
              .slice(0, 12);
        }""")
        # 去重：快速查詢和下方 dropdown 各列一份，但只取最近 3 個 unique month
        seen = set()
        quick_months = []
        for m in quick_months_raw:
            if m not in seen:
                seen.add(m)
                quick_months.append(m)
            if len(quick_months) >= 3:
                break
        _log(f"[card_stmt] 可選月份 (unique): {quick_months}")

        # 抓當前頁面（預設顯示最新月份）的整個內文
        def _grab_month(month_label: str | None) -> dict:
            content_text = page.evaluate("""() => document.body.innerText""")
            # 用 regex 抓「消費記錄」表格區塊
            import re
            recs = []
            # 每筆形如: YYYY/MM/DD\tYYYY/MM/DD\t末四碼4\t說明\t金額\t...
            pat = re.compile(
                r"(20\d{2}/\d{2}/\d{2})\t(20\d{2}/\d{2}/\d{2})\t(\d{4})\t([^\t\n]+?)\t(-?[\d,]+)",
                re.MULTILINE,
            )
            for m in pat.finditer(content_text):
                recs.append({
                    "trans_date": m.group(1),
                    "post_date": m.group(2),
                    "card_last4": m.group(3),
                    "description": m.group(4).strip(),
                    "amount": m.group(5).replace(",", ""),
                })
            # 抓帳單彙總
            sum_pat = re.search(
                r"臺幣\t([\d,]+)\t([\d,]+)\t([\d,]+)\t([\d,]+)\t([\d,]+)\t([\d,]+)\t([\d,]+)",
                content_text,
            )
            summary = None
            if sum_pat:
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
            due_pat = re.search(r"結帳日：(\d{4}/\d{2}/\d{2})\s*繳款截止日：(\d{4}/\d{2}/\d{2})", content_text)
            return {
                "month": month_label,
                "billing_cycle_date": due_pat.group(1) if due_pat else None,
                "payment_due_date": due_pat.group(2) if due_pat else None,
                "summary": summary,
                "records": recs,
                "record_count": len(recs),
            }

        # 目前月（預設展示）
        results.append(_grab_month(quick_months[0] if quick_months else None))
        # 切換到前兩個月（如果有）
        for label in quick_months[1:3]:
            try:
                page.get_by_text(label, exact=True).first.click(timeout=4000)
                page.wait_for_timeout(4500)
                results.append(_grab_month(label))
            except Exception:
                _log("[card_stmt] month_switch_failed")

        total = sum(r.get("record_count", 0) for r in results)
        _log(f"[card_stmt] 抓 {len(results)} 個月、共 {total} 筆消費紀錄")
        return results

    def _collect_card_unbilled(self, page, collector) -> dict:
        """信用卡未請款明細（SinoCard/Account/UnbilledTxInquiry）。

        此頁靠 POST API：LatestTx（最新交易）+ OutstandingDetail（已請款合計）。
        collector 攔 sinopac.com 全域，已自動收下。
        """
        try:
            page.goto("https://mma.sinopac.com/SinoCard/Account/UnbilledTxInquiry",
                      wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(7000)
        except Exception:
            _log("[card_unbilled] page_navigation_failed")
            return {}

        latest = self._latest_json(collector, "LatestTx")
        outstand = self._latest_json(collector, "OutstandingDetail")
        out = {
            "latest_tx": latest,
            "outstanding_detail": outstand,
        }
        n_latest = 0
        if isinstance(latest, dict):
            items = (latest.get("Result") or {}).get("Items", [])
            n_latest = len(items) if isinstance(items, list) else 0
        n_outstand = 0
        if isinstance(outstand, dict):
            detail = (outstand.get("Result") or {}).get("Detail", [])
            n_outstand = len(detail) if isinstance(detail, list) else 0
        _log(f"[card_unbilled] LatestTx={n_latest} 筆, OutstandingDetail={n_outstand} 筆")
        return out

    @staticmethod
    def _latest_json(collector: ResponseCollector, endpoint: str):
        """取某 endpoint 最新一次回應的 JSON body（dict 或 list）。"""
        hit = collector.latest(endpoint)
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
