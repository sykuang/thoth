#!/usr/bin/env python3
"""Taipei Fubon Bank personal e-banking crawler (ebank.taipeifubon.com.tw).

台北富邦 ebank.taipeifubon.com.tw 個人網銀爬蟲。

2026-06-12 schema 校正（vision 驗證 + dry probe v2/v3 揭示）:

  frame 結構（4 frames）:
    - frame[0] = main (Index.faces, frameset shell)
    - frame[1] = frame1 (ContextFrame.faces, 含右上「登入」按鈕)
    - frame[2] = QAiFrame (客服, about:blank)
    - frame[3] = txnFrame (PreLogin.faces, 一般登入 form)

  登入流程:
    Step 1: page goto Index.faces → 等 frameset 全載 (12s)
    Step 2: frame1 內 click #header_form:header_login（右上「登入」開 modal）
    Step 3: 等 modal → txnFrame 內 click 「一般登入」tab (<a> text='一般登入')
    Step 4: txnFrame 內 fill 4 欄:
              #m1_LJCHUYIFKV  → 身分證 (maxlen=10)
              #m1_VVYJVIJLIE  → 使用者代碼 (maxlen=10，實況 7 碼 XXX1234)
              #m1_ACXMQTRIBF  → 密碼 (maxlen=16)
              #m1_userCaptcha → 6 碼純數字 captcha
    Step 5: captcha = OCR(#m1_captchaImage locator screenshot bytes)
            實測 3/3 OCR 命中（v3_captcha_t1=418862 等）
    Step 6: click #btnLogin2 (txnFrame 內，<a id="btnLogin2">登入</a>)

  ⚠️ 鐵律 max_attempts=1 — 失敗 raise FubonLoginError，絕不重打（會鎖帳號）
  ⚠️ 換 captcha 用「重新產生」連結，不是 .captcha-refresh
"""
from __future__ import annotations

import contextlib
import os
import re
import sys
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

from scrapling.fetchers import StealthySession

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.base import BankCollectResult, BankCrawler, ResponseCollector, validate_history_coverage
from backend.core.card_bills import make_card_bill_fact, publish_card_bill_facts
from backend.core.captcha import ocr_bytes
from backend.core.creds import TaipeiFubonCreds
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointPhase,
    LoginCheckpointRule,
    bounded_login_inspection,
    bounded_locator_matches,
)

BASE = "https://ebank.taipeifubon.com.tw/B2C/common/Index.faces"
TWD_HISTORY_URL = "https://ebank.taipeifubon.com.tw/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces"
PRE_LOGIN_HINT = "PreLogin.faces"
HEADER_LOGIN_BTN_ID = "header_form:header_login"  # 在 frame1，右上「登入」開 modal
GENERAL_LOGIN_TAB = "一般登入"
LOGIN_BTN_ID = "btnLogin2"  # 一般登入 form 的登入鈕（txnFrame 內）

FIELD_M1_CAPTCHA     = "m1_userCaptcha"  # 6 碼純數字
CAPTCHA_IMG_ID       = "m1_captchaImage"  # 158×30 captcha img
_DYNAMIC_LOGIN_FIELD_ID = re.compile(r"^m1_[A-Z]{10}$")
_FUBON_OPTION_VALUE_RE = re.compile(r"^012-\d{3}-(\d{14})-[A-Z]-TW$")


def _fubon_one_year_floor(end: date) -> date:
    year = end.year - 1
    return date(year, end.month, min(end.day, monthrange(year, end.month)[1]))


def _fubon_history_windows(as_of: date) -> list[dict]:
    recent_start = as_of - timedelta(days=180)
    return [
        {"preset": "rdoDay180_365", "start": _fubon_one_year_floor(as_of).isoformat(), "end": (recent_start - timedelta(days=1)).isoformat()},
        {"preset": "rdoDay180", "start": recent_start.isoformat(), "end": as_of.isoformat()},
    ]


def _validated_fubon_twd_options(options) -> list[dict]:
    if not isinstance(options, list) or not options:
        raise ValueError("invalid Fubon TWD inventory")
    validated, seen_identities, seen_values = [], set(), set()
    for position, option in enumerate(options):
        if not isinstance(option, dict):
            raise ValueError("invalid Fubon TWD inventory")
        text, value, index = option.get("text"), option.get("value"), option.get("index")
        if position == 0 and index == 0 and text == "==請選擇==" and value in {"none", "_none"}:
            continue
        identities = re.findall(r"(?<!\d)\d{10,16}(?!\d)", text or "")
        value_match = _FUBON_OPTION_VALUE_RE.fullmatch(value) if isinstance(value, str) else None
        canonical_value = len(identities) == 1 and value_match is not None and identities[0] == value_match.group(1)
        if (type(index) is not int or index != position or not isinstance(text, str)
                or not canonical_value or value in seen_values
                or identities[0] in seen_identities):
            raise ValueError("invalid Fubon TWD inventory")
        seen_identities.add(identities[0])
        seen_values.add(value)
        validated.append({**option, "identity": identities[0]})
    if not validated:
        raise ValueError("invalid Fubon TWD inventory")
    return validated


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


def bounded_evaluate(scope, expression: str, arg=None):
    return getattr(scope.locator("html"), "evaluate")(
        f"(root, arg) => ({expression})(arg)", arg, timeout=5000,
    )


def _safe_url(value: object) -> str:
    try:
        parsed = urlsplit(str(value or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return "<invalid>"
        port = f":{parsed.port}" if parsed.port else ""
        route = (parsed.path.rsplit("/", 1)[-1] or "").split(";", 1)[0]
        if not re.fullmatch(
            r"(?:Index|ContextFrame|PreLogin)\.faces|[A-Z0-9]{6,}_Home\.faces|dispatcher",
            route,
        ):
            route = "<redacted-path>"
        return f"{parsed.scheme}://{parsed.hostname}{port}/{route}"
    except (TypeError, ValueError):
        return "<invalid>"


class FubonLoginError(RuntimeError):
    """Fubon login 送出後失敗——立刻中止，絕不自動重打。"""


def _fubon_card_bill_fact(amount_text: str):
    payment = re.search(
        r"繳款狀態.*?自動扣繳帳號.*?\n"
        r"([^\t\n]+)\s*\t\s*(\d{4}/\d{1,2}/\d{1,2})\s*\t\s*"
        r"([\d,]+)\s*\t\s*([\d,]+)\s*\t",
        amount_text,
    )
    bill = re.search(
        r"本期帳單結帳日.*?繳款截止日.*?\n"
        r"(\d{4}/\d{1,2}/\d{1,2})\s*\t\s*[\d,]+\s*\t\s*[\d,]+"
        r"\s*\t\s*([^\t\n]+)",
        amount_text,
    )
    due_text = bill.group(2).strip() if bill else None
    if due_text == "無需繳款":
        due_text = None
    return make_card_bill_fact(
        remaining_due=payment.group(4) if payment else None,
        statement_close_date=bill.group(1) if bill else None,
        payment_due_date=due_text,
        last_payment_amount=payment.group(3) if payment else None,
        last_payment_date=payment.group(2) if payment else None,
    )


class FubonCrawler(BankCrawler):
    USES_SHARED_LOGIN_CHECKPOINTS: ClassVar[bool] = True
    HISTORY_COVERAGE_REQUIRED: ClassVar[bool] = True
    HISTORY_COVERAGE_DOMAINS: ClassVar[frozenset[str]] = frozenset({"twd_transactions"})
    CREDENTIAL_HOSTS = frozenset({"ebank.taipeifubon.com.tw"})

    def __init__(self):
        super().__init__(name="fubon")
        self.creds = TaipeiFubonCreds.load()

    def _history_windows(self, identity: str, as_of: date) -> list[dict]:
        full = _fubon_history_windows(as_of)
        mode = os.environ.get("BANK_CRAWLER_HISTORY_MODE", "full")
        cursor = self.transaction_start_for(identity, domain="twd_transactions")
        if mode == "full" or cursor is None:
            return full
        return full[1:] if cursor >= date.fromisoformat(full[1]["start"]) else full

    @staticmethod
    def _validated_twd_history_result(result: dict) -> dict:
        if not isinstance(result, dict):
            raise RuntimeError("fubon-twd-history-result")
        identity, account_value = result.get("account_no"), result.get("account_value")
        preset, start, end = result.get("preset"), result.get("start"), result.get("end")
        status, snapshot, transport = result.get("status"), result.get("snapshot"), result.get("transport")
        if not isinstance(start, str) or not isinstance(end, str):
            raise RuntimeError("fubon-twd-history-result")
        try:
            start_day, end_day = date.fromisoformat(start), date.fromisoformat(end)
            parsed_url = urlsplit(result.get("url") or "")
        except (TypeError, ValueError):
            raise RuntimeError("fubon-twd-history-result") from None
        if (
            not isinstance(identity, str)
            or not re.fullmatch(r"\d{10,16}", identity)
            or not isinstance(account_value, str)
            or (value_match := _FUBON_OPTION_VALUE_RE.fullmatch(account_value)) is None
            or value_match.group(1) != identity
            or preset not in {"rdoDay180_365", "rdoDay180"}
            or start_day > end_day
            or status not in {"complete", "explicit_empty"}
            or parsed_url.scheme != "https"
            or parsed_url.hostname != "ebank.taipeifubon.com.tw"
            or parsed_url.port not in (None, 443)
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
            or parsed_url.path != "/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces"
            or not isinstance(transport, dict)
            or set(transport) != {
                "status", "contentType", "responseCount", "frameBound", "presetBound",
                "fieldsBound", "viewStateBound", "actionBound", "formBound",
            }
            or type(transport.get("status")) is not int
            or transport.get("status") != 200
            or transport.get("contentType") != "text/plain"
            or type(transport.get("responseCount")) is not int
            or transport.get("responseCount") != 1
            or transport.get("frameBound") is not True
            or transport.get("presetBound") is not True
            or transport.get("fieldsBound") is not True
            or transport.get("viewStateBound") is not True
            or transport.get("actionBound") is not True
            or transport.get("formBound") is not True
            or not isinstance(snapshot, dict)
            or snapshot.get("evidenceFresh") is not True
            or snapshot.get("busy") is not False
            or snapshot.get("failed") is not False
            or snapshot.get("selectedValue") != account_value
            or snapshot.get("selectedIdentity") != identity
            or snapshot.get("selectedPreset") != preset
            or snapshot.get("windowBound") is not True
            or snapshot.get("resultContainerBound") is not True
            or snapshot.get("displayedStart") != start
            or snapshot.get("displayedEnd") != end
        ):
            raise RuntimeError("fubon-twd-history-result")
        pager = snapshot.get("pager")
        if (
            not isinstance(pager, dict)
            or set(pager) != {"present", "actionableNext"}
            or pager.get("present") is not False
            or type(pager.get("actionableNext")) is not int
            or pager["actionableNext"] != 0
        ):
            raise RuntimeError("fubon-twd-history-result")
        rows, row_count = snapshot.get("gridRows"), snapshot.get("gridRowCount")
        total_count, raw_count = snapshot.get("totalCount"), snapshot.get("rawDataRowCount")
        grid_count = snapshot.get("gridCandidateCount")
        if (
            not isinstance(rows, list)
            or type(row_count) is not int
            or type(total_count) is not int
            or type(raw_count) is not int
            or type(grid_count) is not int
            or type(snapshot.get("hiddenGridCount")) is not int
            or snapshot.get("hiddenGridCount") != 0
            or type(snapshot.get("pagerNodeCount")) is not int
            or snapshot.get("pagerNodeCount") != 0
            or type(snapshot.get("structuralErrorCount")) is not int
            or snapshot.get("structuralErrorCount") != 0
            or type(snapshot.get("nativeTotalMarkerCount")) is not int
            or len(rows) != row_count
            or total_count != raw_count
            or raw_count != row_count
            or type(snapshot.get("malformedRowCount")) is not int
            or snapshot.get("malformedRowCount") != 0
            or type(snapshot.get("hiddenRowCount")) is not int
            or snapshot.get("hiddenRowCount") != 0
            or type(snapshot.get("hiddenCellCount")) is not int
            or snapshot.get("hiddenCellCount") != 0
        ):
            raise RuntimeError("fubon-twd-history-result")
        if status == "complete":
            dates = []
            try:
                for cells in rows:
                    if not isinstance(cells, list):
                        raise ValueError
                    matched = [
                        match.group(1)
                        for cell in cells
                        if isinstance(cell, str)
                        if (match := re.fullmatch(r"\*?(20\d{2}/\d{1,2}/\d{1,2})", cell))
                    ]
                    if len(matched) != 1:
                        raise ValueError
                    dates.append(date.fromisoformat(matched[0].replace("/", "-")))
            except (TypeError, ValueError):
                raise RuntimeError("fubon-twd-history-result") from None
            if (
                snapshot.get("hasGrid") is not True
                or snapshot.get("nativeTotalFound") is not True
                or snapshot.get("nativeTotalMarkerCount") != 1
                or grid_count != 1
                or row_count <= 0
                or snapshot.get("emptyMarker") is not None
                or any(day < start_day or day > end_day for day in dates)
            ):
                raise RuntimeError("fubon-twd-history-result")
        elif (
            snapshot.get("hasGrid") is not False
            or snapshot.get("nativeTotalMarkerCount") != 0
            or grid_count != 0
            or rows != []
            or row_count != 0
            or snapshot.get("gridText") != ""
            or snapshot.get("emptyMarker") not in {"查無相關資料", "查無交易資料"}
        ):
            raise RuntimeError("fubon-twd-history-result")
        return {"identity": identity, "start": start, "end": end, "status": status, "pages": 1}

    def _execute_browser_flow(
        self,
        login_url: str,
        *,
        headless: bool,
        page_action,
        fetch_kwargs: dict,
    ) -> None:
        _log("[fubon][phase] browser_start")
        with StealthySession(
            headless=headless,
            user_data_dir=str(self.session_dir),
            hide_canvas=True,
            block_webrtc=True,
            dns_over_https=True,
            **fetch_kwargs,
        ) as engine:
            _log("[fubon][phase] browser_ready")
            if engine.context is None:
                raise RuntimeError("富邦瀏覽器 context 未建立")
            page = engine.context.new_page()
            page.set_default_navigation_timeout(180000)
            page.set_default_timeout(180000)
            if headers := fetch_kwargs.get("extra_headers"):
                page.set_extra_http_headers(headers)
            try:
                _log("[fubon][phase] goto_start")
                page.goto(login_url, referer="https://www.google.com/")
                page.wait_for_load_state("load")
                page.wait_for_load_state("domcontentloaded")
                _log("[fubon][phase] goto_done")
                _log("[fubon][phase] page_action_start")
                page_action(page)
                _log("[fubon][phase] page_action_done")
                page.wait_for_timeout(2000)
            finally:
                _log("[fubon][phase] browser_close")
                page.close()

    def _host_filter(self) -> str:
        return "taipeifubon.com"

    def _is_owned_frame(self, page, frame) -> bool:
        return self._frame_origin_allowed(page, frame)

    def _find_login_frame(self, page):
        matches = {
            id(frame): frame
            for frame in page.frames
            if self._is_owned_frame(page, frame)
            and (frame.name == "txnFrame" or PRE_LOGIN_HINT in (frame.url or ""))
        }
        return next(iter(matches.values())) if len(matches) == 1 else None

    def _find_header_frame(self, page):
        matches = {
            id(frame): frame
            for frame in page.frames
            if self._is_owned_frame(page, frame)
            and (frame.name == "frame1" or "ContextFrame" in (frame.url or ""))
        }
        return next(iter(matches.values())) if len(matches) == 1 else None

    def _logged_in(self, page) -> bool:
        """Pure one-shot positive check; lifecycle owns all waiting."""
        try:
            if not self._exact_https_origin_allowed(page.url, self.CREDENTIAL_HOSTS):
                return False
            if any(
                not self._is_owned_frame(page, frame)
                for frame in page.frames
                if frame.name in {"txnFrame", "frame1"}
                or PRE_LOGIN_HINT in (frame.url or "")
                or "ContextFrame" in (frame.url or "")
            ):
                return False
            login_frames = {
                id(frame)
                for frame in page.frames
                if self._is_owned_frame(page, frame)
                and (frame.name == "txnFrame" or PRE_LOGIN_HINT in (frame.url or ""))
            }
            header_frames = {
                id(frame)
                for frame in page.frames
                if self._is_owned_frame(page, frame)
                and (frame.name == "frame1" or "ContextFrame" in (frame.url or ""))
            }
            if len(login_frames) > 1 or len(header_frames) > 1:
                return False
            if any(PRE_LOGIN_HINT in (frame.url or "") for frame in page.frames):
                return False
            scopes = [
                page,
                *(
                    frame
                    for frame in page.frames
                    if frame is not page.main_frame and self._is_owned_frame(page, frame)
                ),
            ]
            body_parts = []
            controls_selector = (
                "input[type='password'], #m1_userCaptcha, #btnLogin2, "
                "#header_form\\:header_login"
            )
            for scope in scopes:
                controls = scope.locator(controls_selector)
                if any(item.is_visible() for item in bounded_locator_matches(controls)):
                    return False
                bodies = scope.locator("body")
                body_parts.extend(
                    body.inner_text(timeout=5000)
                    for body in bounded_locator_matches(bodies)
                )
            body = "\n".join(body_parts)
            return (
                len(body) >= 500
                and "登出" in body
                and (
                    any(item in body for item in ("帳戶總覽", "我的帳戶", "資產總額"))
                    or ("帳戶" in body and "資產" in body)
                )
            )
        except Exception:
            return False

    def login(self, page) -> bool:
        return self._shared_login(page)

    def prepare_login_page(self, page) -> None:
        _log("[fubon][phase] prepare_start")
        with bounded_login_inspection(page):
            self._prepare_login_page(page)
        _log("[fubon][phase] prepare_done")

    def _prepare_login_page(self, page) -> None:
        try:
            page.wait_for_timeout(12000)
            if self._logged_in(page):
                return
            _log("[fubon][phase] prepare_auth_checked")
            header_frame = self._find_header_frame(page)
            if header_frame is None:
                raise FubonLoginError("找不到唯一的登入頁首；未送出登入")
            headers = tuple(bounded_locator_matches(
                header_frame.locator("#header_form\\:header_login"),
                first_timeout_ms=5000,
            ))
            if len(headers) != 1:
                raise FubonLoginError("找不到唯一且可操作的頁首登入按鈕；未送出登入")
            header = headers[0]
            if (
                not header.is_visible()
                or not header.is_enabled()
                or " ".join(header.inner_text().split()) != "登入"
            ):
                raise FubonLoginError("找不到唯一且可操作的頁首登入按鈕；未送出登入")
            header.click(timeout=8000)
            _log("[fubon][phase] header_clicked")
            page.wait_for_timeout(5000)

            login_frame = self._find_login_frame(page)
            if login_frame is None:
                raise FubonLoginError("找不到唯一的一般登入頁面；未送出登入")
            _log("[fubon][phase] login_frame_ready")
            actions = login_frame.locator("a, button").filter(
                has_text=re.compile(r"^\s*一般登入\s*$")
            )
            eligible = []
            for action in bounded_locator_matches(actions, first_timeout_ms=5000):
                if (
                    action.is_visible()
                    and action.is_enabled()
                    and " ".join(action.inner_text().split()) == GENERAL_LOGIN_TAB
                ):
                    eligible.append(action)
            if len(eligible) != 1:
                raise FubonLoginError("找不到唯一且可操作的一般登入分頁；未送出登入")
            eligible[0].click(timeout=8000)
            page.wait_for_timeout(2000)
        except FubonLoginError:
            raise
        except Exception:
            raise FubonLoginError("登入頁面準備狀態不明；未送出登入") from None

    def is_authenticated(self, page) -> bool:
        return self._logged_in(page)

    def login_checkpoint_rules(self) -> tuple[LoginCheckpointRule, ...]:
        all_phases = tuple(CheckpointPhase)
        post = (CheckpointPhase.POST_SUBMIT, CheckpointPhase.POST_SUBMIT_SETTLE)
        otp = re.compile(
            r"^[\s\S]{0,400}(?:(?<![A-Za-z])OTP(?![A-Za-z])|一次性密碼|簡訊驗證碼|裝置驗證|信任此裝置)[\s\S]{0,400}$",
            re.IGNORECASE,
        )
        password = re.compile(
            r"^[\s\S]{0,120}(?:(?:必須|強制|請立即|請先)(?:變更|修改|更新|重設)(?:您的?)?密碼|"
            r"密碼(?:已到期|已過期|必須修改|需要修改|需修改|強制變更))[\s\S]{0,120}$"
        )
        error = re.compile(
            r"^\s*(?:密碼不正確|帳號(?:因多次錯誤)?(?:已遭|已被|已)鎖定|登入失敗|驗證碼不正確|invalid credentials|account locked)[。.!！\s]*$",
            re.IGNORECASE,
        )
        rules = []
        for suffix, selector in (("modal", ".modal.show"), ("dialog", "[role='dialog']")):
            rules.append(LoginCheckpointRule(
                name=f"fubon-otp-required-{suffix}", bank="fubon", phases=all_phases,
                kind=CheckpointKind.OTP_REQUIRED, container_selector=selector,
                required_body_pattern=otp,
            ))
        for suffix, selector in (("modal", ".modal.show"), ("dialog", "[role='dialog']")):
            rules.append(LoginCheckpointRule(
                name=f"fubon-password-change-required-{suffix}", bank="fubon", phases=all_phases,
                kind=CheckpointKind.PASSWORD_CHANGE_REQUIRED, container_selector=selector,
                required_body_pattern=password,
            ))
        for suffix, selector in (("error", ".error"), ("alert", ".alert"), ("role-alert", "[role='alert']")):
            rules.append(LoginCheckpointRule(
                name=f"fubon-explicit-login-error-{suffix}", bank="fubon", phases=post,
                kind=CheckpointKind.EXPLICIT_LOGIN_ERROR, container_selector=selector,
                required_body_pattern=error,
            ))
        for suffix, selector in (("modal", ".modal.show"), ("dialog", "[role='dialog']")):
            rules.append(LoginCheckpointRule(
                name=f"fubon-unknown-{suffix}", bank="fubon", phases=all_phases,
                kind=CheckpointKind.UNKNOWN_BLOCKER, container_selector=selector,
            ))
        rules.append(LoginCheckpointRule(
            name="fubon-login-form-still-visible", bank="fubon", phases=post,
            kind=CheckpointKind.UNKNOWN_BLOCKER, container_selector="#m1_userCaptcha",
        ))
        return tuple(rules)

    def _ocr_captcha(self, frame, max_attempts=5):
        attempts = min(max(max_attempts, 0), 5)
        for attempt in range(attempts):
            try:
                images = tuple(bounded_locator_matches(
                    frame.locator("#m1_captchaImage"), first_timeout_ms=5000
                ))
                if len(images) != 1:
                    return None
                image = images[0]
                if not image.is_visible():
                    return None
                raw = image.screenshot(timeout=5000)
                text = ocr_bytes(
                    raw,
                    expected_len=6,
                    alnum_only=True,
                    min_confidence=0.98,
                )
            except Exception:
                return None
            if text and len(text) == 6 and text.isdigit():
                return text
            if attempt == attempts - 1:
                break
            try:
                candidates = frame.locator("a, button")
                eligible = []
                for action in bounded_locator_matches(candidates, first_timeout_ms=5000):
                    if (
                        action.is_visible()
                        and action.is_enabled()
                        and " ".join(action.inner_text().split()) == "重新產生"
                    ):
                        eligible.append(action)
                if len(eligible) != 1:
                    return None
                eligible[0].click()
                frame.wait_for_timeout(1500)
            except Exception:
                return None
        return None

    def submit_credentials_once(self, page) -> None:
        _log("[fubon][phase] submit_start")
        with bounded_login_inspection(page):
            self._submit_credentials_once(page)
        _log("[fubon][phase] submit_done")

    def _submit_credentials_once(self, page) -> None:
        try:
            frame = self._find_login_frame(page)
            if frame is None:
                raise FubonLoginError("找不到唯一的登入頁面；未送出登入")
            candidates = frame.locator("input[type='password']")
            ordered = []
            for field in bounded_locator_matches(candidates, first_timeout_ms=5000):
                if not field.is_visible():
                    continue
                field_id = field.get_attribute("id") or ""
                field_name = field.get_attribute("name") or ""
                maxlength = field.get_attribute("maxlength") or ""
                box = field.bounding_box()
                if (
                    not field.is_enabled()
                    or not _DYNAMIC_LOGIN_FIELD_ID.fullmatch(field_id)
                    or field_id != f"m1_{field_name}"
                    or maxlength not in {"10", "16"}
                    or not box
                    or box["width"] <= 0
                    or box["height"] <= 0
                ):
                    raise FubonLoginError("登入欄位無法安全填寫；未送出登入")
                ordered.append((box["y"], maxlength, field))
            ordered.sort(key=lambda item: item[0])
            if (
                [item[1] for item in ordered] != ["10", "10", "16"]
                or len({item[0] for item in ordered}) != 3
            ):
                raise FubonLoginError("登入欄位無法安全填寫；未送出登入")
            fields = [item[2] for item in ordered]
            values = (self.creds.national_id, self.creds.user_code, self.creds.password)
            for field, value in zip(fields, values, strict=True):
                field.click()
                field.click(click_count=3)
                field.press("Backspace", timeout=5000)
                field.press_sequentially(value, delay=80, timeout=5000)
                if len(field.input_value()) != len(value):
                    raise FubonLoginError("登入欄位輸入長度不符；未送出登入")

            captchas = tuple(bounded_locator_matches(
                frame.locator("#m1_userCaptcha"), first_timeout_ms=5000
            ))
            if len(captchas) != 1:
                raise FubonLoginError("驗證碼欄位無法安全填寫；未送出登入")
            captcha_field = captchas[0]
            if (
                not captcha_field.is_visible()
                or not captcha_field.is_enabled()
                or captcha_field.get_attribute("maxlength") != "6"
            ):
                raise FubonLoginError("驗證碼欄位無法安全填寫；未送出登入")
            captcha = self._ocr_captcha(frame, max_attempts=5)
            if not captcha or len(captcha) != 6 or not captcha.isdigit():
                raise FubonLoginError("圖形驗證碼 OCR 失敗；未送出登入")
            captcha_field.click()
            captcha_field.click(click_count=3)
            captcha_field.press("Backspace", timeout=5000)
            captcha_field.press_sequentially(captcha, delay=80, timeout=5000)
            if len(captcha_field.input_value()) != 6:
                raise FubonLoginError("驗證碼欄位輸入長度不符；未送出登入")

            submits = tuple(bounded_locator_matches(
                frame.locator("#btnLogin2"), first_timeout_ms=5000
            ))
            if len(submits) != 1:
                raise FubonLoginError("找不到唯一且可操作的登入按鈕；未送出登入")
            submit = submits[0]
            if (
                not submit.is_visible()
                or not submit.is_enabled()
                or " ".join(submit.inner_text().split()) != "登入"
            ):
                raise FubonLoginError("找不到唯一且可操作的登入按鈕；未送出登入")
        except FubonLoginError:
            raise
        except Exception:
            raise FubonLoginError("登入欄位無法安全填寫；未送出登入") from None

        try:
            submit.click(timeout=8000)
        except Exception:
            raise FubonLoginError("登入送出狀態不明；禁止自動重試") from None

        try:
            page.wait_for_timeout(3000)
            for _ in range(10):
                page.wait_for_timeout(1000)
                if self._logged_in(page):
                    return
                scopes = [
                    page,
                    *(child for child in page.frames if child is not page.main_frame),
                ]
                for scope in scopes:
                    for selector in (
                        ".modal.show", "[role='dialog']", ".error", ".alert", "[role='alert']",
                    ):
                        checkpoints = scope.locator(selector)
                        if any(
                            item.is_visible()
                            for item in bounded_locator_matches(checkpoints)
                        ):
                            return
        except Exception:
            raise FubonLoginError("登入後狀態無法安全確認；禁止自動重試") from None

    def _fubon_content_frame(self, page, *routes):
        matches = {}
        for candidate in page.frames:
            parsed = urlsplit(candidate.url or "")
            path = parsed.path
            if (
                self._is_owned_frame(page, candidate)
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
                and any(path == route for route in routes)
            ):
                matches[id(candidate)] = candidate
        if len(matches) != 1:
            raise RuntimeError("fubon-twd-history-frame")
        return next(iter(matches.values()))

    def _open_twd_query(self, page):
        page.goto(
            "https://ebank.taipeifubon.com.tw/B2C/cgequ/cgequ001/CGEQU001_Home.faces",
            wait_until="domcontentloaded", timeout=15000,
        )
        page.wait_for_timeout(5000)
        frame = self._fubon_content_frame(page, "/B2C/cgequ/cgequ001/CGEQU001_Home.faces")
        clicked = bounded_evaluate(frame, r"""() => {
            const links = [...document.querySelectorAll('a.task_CDSQU001.menu_CDS0401')]
                .filter((a) => (a.textContent || '').trim() === '臺外幣交易明細查詢');
            if (links.length !== 1) return {ok:false, count:links.length};
            links[0].click(); return {ok:true};
        }""")
        if clicked != {"ok": True}:
            raise RuntimeError("fubon-twd-history-navigation")
        page.wait_for_timeout(8000)
        return self._fubon_content_frame(page, "/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces")

    @staticmethod
    def _capture_twd_response(
        response, hits: list[dict], expected_frame, preset: str,
        expected_view_state: str, expected_action: str, form_bound: bool,
    ) -> None:
        try:
            parsed = urlsplit(response.url or "")
            request = response.request
            fields = parse_qsl(request.post_data or "", keep_blank_values=True)
            names = [key for key, _value in fields]
            preset_values = [value for key, value in fields if key == "checkedConvenientPeriod"]
            view_states = [value for key, value in fields if key == "javax.faces.ViewState"]
            actions = [value for key, value in fields if key == "ajaxAction"]
            if (
                parsed.scheme == "https"
                and parsed.hostname == "ebank.taipeifubon.com.tw"
                and parsed.port in (None, 443)
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
                and parsed.path == "/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces"
                and request.method == "POST"
            ):
                hits.append({
                    "status": response.status,
                    "contentType": (response.headers.get("content-type") or "").split(";", 1)[0].lower(),
                    "frameBound": request.frame is expected_frame,
                    "presetBound": preset_values == [preset],
                    "fieldsBound": sorted(names) == ["ajaxAction", "checkedConvenientPeriod", "javax.faces.ViewState"],
                    "viewStateBound": view_states == [expected_view_state],
                    "actionBound": actions == [expected_action],
                    "formBound": form_bound is True,
                })
        except Exception:
            return

    def _bound_twd_result_frame(self, page, submit_frame):
        result_frame = self._fubon_content_frame(
            page, "/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces",
        )
        if result_frame is not submit_frame:
            raise RuntimeError("fubon-twd-history-result-frame")
        return result_frame

    def _collect_twd_window(self, page, frame, option: dict, window: dict) -> tuple[dict, dict]:
        controls = bounded_evaluate(frame, r"""(args) => {
            const forms=[...document.querySelectorAll('form#form1')];
            if(forms.length!==1)return {ok:false};
            const form=forms[0];
            const one = (s) => { const xs=[...document.querySelectorAll(s)]; return xs.length===1&&form.contains(xs[0]) ? xs[0] : null; };
            const account=one('#form1\\:comboAccount'), detail=one('#form1\\:rdoTxDetail');
            const fast=one('#form1\\:rdoFast');
            const preset=one(`#form1\\:${CSS.escape(args.preset)}`);
            const states=[...document.querySelectorAll('[name="javax.faces.ViewState"]')];
            const actions=[...document.querySelectorAll('[name="ajaxAction"]')];
            const submit=one('#form1\\:doValidateAndSubmit');
            let formAction='';
            try{formAction=new URL(form.getAttribute('action')||form.action,location.href).href;}catch(_e){}
            const formBound=form.method.toUpperCase()==='POST'&&formAction===args.formAction
                &&states.length===1&&form.contains(states[0])&&actions.length===1&&form.contains(actions[0]);
            if (!account || !detail || !fast || !preset
                || !submit || !formBound || !states[0].value || !actions[0].value) return {ok:false};
            account.value=args.value;
            for (const n of ['input','change']) account['dispatch' + 'Event'](new Event(n,{bubbles:true}));
            if (typeof comboAccountChange==='function') comboAccountChange();
            if (typeof checkAccountType==='function') checkAccountType();
            account['dispatch' + 'Event'](new Event('blur',{bubbles:true})); detail.click();
            fast.click(); preset.click();
            const selected=account.options[account.selectedIndex];
            return {ok:true,value:selected?.value||'',text:(selected?.textContent||'').trim(),detail:detail.checked,
                fast:fast.checked,preset:preset.checked,
                viewState:states[0].value,ajaxAction:actions[0].value,formBound};
        }""", {**window, "value": option["value"], "formAction": TWD_HISTORY_URL})
        if (
            not isinstance(controls, dict)
            or controls.get("ok") is not True
            or controls.get("value") != option["value"]
            or re.findall(r"(?<!\d)\d{10,16}(?!\d)", controls.get("text") or "") != [option["identity"]]
            or controls.get("detail") is not True
            or controls.get("preset") is not True
            or not isinstance(controls.get("viewState"), str)
            or not controls["viewState"]
            or not isinstance(controls.get("ajaxAction"), str)
            or not controls["ajaxAction"]
            or controls.get("formBound") is not True
            or controls.get("fast") is not True
        ):
            raise RuntimeError("fubon-twd-history-controls")
        marked = bounded_evaluate(frame, r"""() => {
            const labels=new Set(['查無相關資料','查無交易資料']), evidence=[];
            for (const table of document.querySelectorAll('table')) if (/帳務日期/.test(table.textContent||'') && /交易時間/.test(table.textContent||'')) evidence.push(table);
            for (const el of document.querySelectorAll('*')) if (labels.has((el.textContent||'').trim())) evidence.push(el);
            for (const el of new Set(evidence)) el.setAttribute('data-hermes-stale-evidence','1');
            return new Set(evidence).size;
        }""")
        if type(marked) is not int:
            raise RuntimeError("fubon-twd-history-stale-result")
        hits = []
        listener = lambda response: self._capture_twd_response(
            response, hits, frame, window["preset"], controls["viewState"], controls["ajaxAction"],
            controls["formBound"],
        )
        page.on("response", listener)
        try:
            frame.click("#form1\\:doValidateAndSubmit", timeout=8000)
            page.wait_for_timeout(9000)
            stable_ticks = 0
            prior_count = len(hits)
            for _ in range(120):
                page.wait_for_timeout(100)
                if not hits:
                    continue
                if len(hits) == prior_count:
                    stable_ticks += 1
                else:
                    prior_count = len(hits)
                    stable_ticks = 0
                if stable_ticks >= 5:
                    break
            if len(hits) != 1 or stable_ticks < 5:
                raise RuntimeError("fubon-twd-history-transport")
        finally:
            page.remove_listener("response", listener)
        transport = {**hits[0], "responseCount": len(hits)}
        if (
            transport["status"] != 200
            or transport["contentType"] != "text/plain"
            or transport["frameBound"] is not True
            or transport["presetBound"] is not True
            or transport["fieldsBound"] is not True
            or transport["viewStateBound"] is not True
            or transport["actionBound"] is not True
            or transport["formBound"] is not True
        ):
            raise RuntimeError("fubon-twd-history-transport")
        result_frame = self._bound_twd_result_frame(page, frame)
        snapshot = bounded_evaluate(result_frame, r"""(args) => {
            const visible=(el)=>{const r=el.getBoundingClientRect();if(r.width<=0||r.height<=0)return false;
                for(let n=el;n;n=n.parentElement){const s=getComputedStyle(n);if(s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||Number(s.opacity)===0||n.hidden||(n.getAttribute('aria-hidden')||'').toLowerCase()==='true')return false;}return true;};
            const labels=new Set(['查無相關資料','查無交易資料']);
            const empty=[...document.querySelectorAll('*')].filter(el=>visible(el)&&labels.has((el.textContent||'').trim())&&![...el.children].some(c=>labels.has((c.textContent||'').trim())));
            const headers=['帳務日期','交易時間','摘要','支出金額','存入金額','即時餘額','附註'];
            const directRows=(table)=>[...table.querySelectorAll(':scope > tr,:scope > thead > tr,:scope > tbody > tr,:scope > tfoot > tr')];
            const directCells=(row)=>[...row.querySelectorAll(':scope > th,:scope > td')];
            const cellTexts=(row)=>directCells(row).map(c=>(c.textContent||'').trim().replaceAll('\u3000',''));
            const isHeader=(row)=>{const values=cellTexts(row);return values.length===headers.length&&values.every((value,index)=>value===headers[index]);};
            const allCandidates=[...document.querySelectorAll('table')].filter(table=>directRows(table).some(isHeader));
            const candidates=allCandidates.filter(table=>{const header=directRows(table).find(isHeader);return visible(table)&&visible(header)&&directCells(header).every(visible);});
            const hiddenGridCount=allCandidates.length-candidates.length;
            const grid=candidates.length===1?candidates[0]:null, projected=[];
            let rawDataRowCount=0, malformedRowCount=0, hiddenRowCount=0, hiddenCellCount=0;
            if(grid){const rows=directRows(grid), headerAt=rows.findIndex(isHeader);
                for(const row of rows.slice(headerAt+1)){const cells=[...row.querySelectorAll(':scope > th,:scope > td')], values=cellTexts(row);
                    if(!values.some(Boolean))continue; rawDataRowCount++;
                    if(!visible(row)){hiddenRowCount++;continue;} const hidden=cells.filter(c=>!visible(c)).length; hiddenCellCount+=hidden;
                    const dates=values.filter(value=>/^\*?20\d{2}\/\d{1,2}\/\d{1,2}$/.test(value));
                    if(hidden||values.length!==7||dates.length!==1){malformedRowCount++;continue;} projected.push(values);
                }}
            const pagerControls=[...document.querySelectorAll('a,button,input,select,[role="button"]')].filter(el=>{
                const raw=(el.textContent||el.value||'').trim();
                const pageNumber=/^\d{1,3}$/.test(raw)&&Number(raw)>1;
                const meta=[el.textContent,el.value,el.title,el.getAttribute('aria-label'),el.getAttribute('rel'),el.getAttribute('href'),el.getAttribute('onclick'),el.id,el.getAttribute('class'),el.getAttribute('data-page')].filter(Boolean).join(' ');
                return pageNumber||/(?:下一頁|下頁|next\s*page|page[-_: ]?next|pagenext|rel[=: ]?next|[?&]page=[2-9]\d*)/i.test(meta);
            });
            const pagerStructures=[...document.querySelectorAll('[class*="pagination" i],[class*="paginator" i],[id*="pagination" i],[id*="paginator" i],[aria-label*="pagination" i],[rel="next" i],[data-page]:not([data-page="1"])')];
            const pagerNodes=[...new Set([...pagerControls,...pagerStructures])];
            const account=[...document.querySelectorAll('select#form1\\:comboAccount')], selected=account.length===1?account[0].options[account[0].selectedIndex]:null;
            const selectedIds=selected?(selected.textContent||'').match(/(?<!\d)\d{10,16}(?!\d)/g)||[]:[];
            const preset=[...document.querySelectorAll(`#form1\\:${CSS.escape(args.preset)}`)];
            const own=(el)=>[...el.childNodes].filter(n=>n.nodeType===Node.TEXT_NODE).map(n=>n.textContent||'').join(' ');
            const periodContainers=[...new Set([...document.querySelectorAll('td,th,label,span,div,p')].filter(el=>visible(el)&&/查詢期間/.test(own(el))).map(el=>el.closest('tr')||el.parentElement||el))];
            const period=periodContainers.length===1?(periodContainers[0].innerText||''):'';
            const evidence=grid||(empty.length===1?empty[0]:null);
            let resultContainer=periodContainers.length===1?evidence:null;
            while(resultContainer&&!resultContainer.contains(periodContainers[0]))resultContainer=resultContainer.parentElement;
            const resultContainerBound=!!resultContainer&&!['HTML','BODY','FORM'].includes(resultContainer.tagName);
            const canonical=(raw)=>{const parts=raw.replaceAll('-','/').split('/').map(Number);return `${parts[0]}-${String(parts[1]).padStart(2,'0')}-${String(parts[2]).padStart(2,'0')}`;};
            const displayed=(period.match(/20\d{2}[\/-]\d{1,2}[\/-]\d{1,2}/g)||[]).map(canonical);
            const displayedStart=displayed.length===2?displayed[0]:'';
            const displayedEnd=displayed.length===2?displayed[1]:'';
            const windowBound=displayedStart===args.start&&displayedEnd===args.end;
            const text=document.body?.innerText||'';
            const totalAdjacentToGrid=(el)=>{
                if(!grid)return false;
                let node=el;
                while(node.parentElement&&node.parentElement!==grid.parentElement)node=node.parentElement;
                if(node.parentElement!==grid.parentElement)return false;
                const siblings=[...grid.parentElement.children];
                return Math.abs(siblings.indexOf(node)-siblings.indexOf(grid))===1;
            };
            const nativeTotalMarkers=resultContainerBound?[resultContainer,...resultContainer.querySelectorAll('td,th,label,span,div,p')].map(el=>({el,match:visible(el)?own(el).match(/^\s*共\s*([\d,]+)\s*筆\s*$/):null})).filter(item=>item.match):[];
            const nativeTotals=nativeTotalMarkers.length===1&&totalAdjacentToGrid(nativeTotalMarkers[0].el)?[Number(nativeTotalMarkers[0].match[1].replaceAll(',',''))].filter(Number.isSafeInteger):[];
            const nativeTotalFound=nativeTotals.length===1;
            const totalCount=nativeTotalFound?nativeTotals[0]:(empty.length===1?0:-1);
            const busyText=/(?:資料(?:載入|查詢|處理)中|載入中|查詢中|處理中|請稍候|請稍待|系統忙碌|system is busy|loading|processing|querying|waiting|\bbusy\b)/i.test(text);
            const busy=busyText||[document.documentElement,...document.querySelectorAll('*')].some(el=>visible(el)&&((el.getAttribute('aria-busy')||'').toLowerCase()==='true'||(el.getAttribute('role')||'').toLowerCase()==='progressbar'||el.tagName.toLowerCase()==='progress'||/(?:loading|loader|spinner|progress|processing|querying|waiting|busy|blockui)/i.test([el.id,el.getAttribute('class')].filter(Boolean).join(' '))));
            const structuralErrors=[...document.querySelectorAll('.error,.errorMessage,.alert,.ui-message-error,.ui-messages-error,[role="alert"],dialog,[role="dialog"],[aria-invalid="true"]')].filter(visible);
            const failed=structuralErrors.length>0||/(?:錯誤|失敗|異常|逾時|失效|重新登入|請重新查詢|請稍後再試|連線中斷|連線失敗|無法處理|system error|\berror\b|timeout|expired|failed|retry|try again|disconnected)/i.test(text);
            return {href:location.href,failed,busy,selectedValue:selected?.value||'',selectedIdentity:selectedIds.length===1?selectedIds[0]:'',selectedPreset:preset.length===1&&preset[0].checked?args.preset:'',windowBound,resultContainerBound,displayedStart,displayedEnd,evidenceFresh:grid?!grid.hasAttribute('data-hermes-stale-evidence'):empty.length===1&&!empty[0].hasAttribute('data-hermes-stale-evidence'),hasGrid:!!grid,gridCandidateCount:candidates.length,hiddenGridCount,pagerNodeCount:pagerNodes.length,structuralErrorCount:structuralErrors.length,gridRows:projected,gridRowCount:projected.length,rawDataRowCount,malformedRowCount,hiddenRowCount,hiddenCellCount,totalCount,nativeTotalFound,nativeTotalMarkerCount:nativeTotalMarkers.length,gridText:grid?'structured':'',emptyMarker:empty.length===1?(empty[0].textContent||'').trim():null,pager:{present:pagerNodes.length>0,actionableNext:pagerNodes.length}};
        }""", {"identity": option["identity"], "preset": window["preset"], "start": window["start"], "end": window["end"]})
        if not isinstance(snapshot, dict) or snapshot.get("failed") is not False:
            raise RuntimeError("fubon-twd-history-result")
        result = {
            "account_no": option["identity"],
            "account_value": option["value"],
            "preset": window["preset"],
            "start": window["start"],
            "end": window["end"],
            "status": "complete" if snapshot.get("hasGrid") else "explicit_empty",
            "url": snapshot.pop("href", ""),
            "transport": transport,
            "snapshot": snapshot,
        }
        return result, self._validated_twd_history_result(result)

    def _collect_attested_twd_history(self, page) -> dict:
        query_frame = self._open_twd_query(page)
        raw_options = bounded_evaluate(query_frame, r"""() => {
            const forms=[...document.querySelectorAll('form#form1')];
            const xs=[...document.querySelectorAll('select#form1\\:comboAccount')];
            if(forms.length!==1||xs.length!==1||!forms[0].contains(xs[0]))return null;
            return [...xs[0].options].map((option,index)=>({index,value:option.value||'',text:(option.textContent||'').trim()}));
        }""")
        try:
            options = _validated_fubon_twd_options(raw_options)
        except ValueError:
            raise RuntimeError("fubon-twd-history-inventory") from None
        as_of = datetime.now(ZoneInfo("Asia/Taipei")).date()
        mode = os.environ.get("BANK_CRAWLER_HISTORY_MODE", "full")
        expected, receipts, results = [], [], []
        for option in options:
            windows = self._history_windows(option["identity"], as_of)
            expected.append({"identity": option["identity"], "start": windows[0]["start"], "end": windows[-1]["end"]})
            for window in windows:
                result, receipt = self._collect_twd_window(page, self._open_twd_query(page), option, window)
                results.append(result); receipts.append(receipt)
        coverage = {"version": 1, "mode": mode, "domains": [{"domain": "twd_transactions", "expected": expected, "windows": receipts}]}
        validate_history_coverage(coverage, expected_mode=mode, expected_domains=self.HISTORY_COVERAGE_DOMAINS)
        accounts = [
            {"account_no": option["identity"], "currency": "TWD", "type": "deposit", "name": "台幣存款"}
            for option in options
        ]
        return {
            "accounts": accounts,
            "deposit_txn_results": results,
            "history_coverage": coverage,
        }

    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        """富邦 collect：信用卡 menu 在 txnFrame (CGEQU001_Home) carousel 全渲染。

        關鍵發現 (2026-06-12):
        - top menu 在 frame1，但 mega menu 內容 carousel 全部存在於 txnFrame 內
        - DOM 全部渲染（不靠 hover），視覺上只顯示一頁，但 querySelector 都拿得到
        - 走 txnFrame 直接定位「我的信用卡」/「帳務/繳款」等子項 click 即可
        """
        _log("[fubon][phase] collect_start")
        def bounded_evaluate(scope, expression: str, arg=None):
            return scope.locator("html").evaluate(
                f"(root, arg) => ({expression})(arg)",
                arg,
                timeout=5000,
            )

        out: dict = {}
        def finish() -> BankCollectResult:
            out["final_url"] = page.url
            out["_all_endpoints"] = sorted({hit.endpoint for hit in collector.hits if hit.resp_json})
            publish_card_bill_facts(out, [_fubon_card_bill_fact(out.get("amount_page_text") or "")])
            return BankCollectResult(**out)

        out.update(self._collect_attested_twd_history(page))
        page.goto(
            "https://ebank.taipeifubon.com.tw/B2C/cgequ/cgequ001/CGEQU001_Home.faces",
            wait_until="domcontentloaded", timeout=15000,
        )
        page.wait_for_timeout(5000)

        out["initial_url"] = page.url

        # === Step 1: 找 txnFrame (內容區，含 carousel mega menu) ===
        content_frame = None
        for f in page.frames:
            url = f.url or ""
            name = f.name or ""
            if "CGEQU001" in url or name == "txnFrame":
                content_frame = f
                break
        _log(f"[fubon][collect] content_frame={'OK' if content_frame else 'MISS'}")
        if not content_frame:
            return finish()

        # === Step 2: 在 txnFrame 找信用卡相關子項 (carousel 全渲染，offscreen 也存在) ===
        # 優先序：直接走「我的信用卡」進信用卡頁，或「帳務/繳款」進帳單查詢
        candidates = bounded_evaluate(content_frame, """() => {
            const targets = ['我的信用卡', '帳務/繳款', '消費分期', '紅利/哩程', '預借現金', '信用卡帳單', '信用卡明細', '帳單查詢'];
            const out = [];
            for (const el of document.querySelectorAll('a, button, span, div, li')) {
                const t = (el.textContent || '').trim();
                if (!targets.includes(t)) continue;
                const r = el.getBoundingClientRect();
                out.push({
                    tag: el.tagName, text: t, cls: (el.className || '').slice(0, 80),
                    href: el.href || (el.getAttribute && el.getAttribute('href')) || '',
                    onclick: (el.getAttribute && el.getAttribute('onclick')) || '',
                    x: r.x, y: r.y, w: r.width, h: r.height,
                    visible: r.width > 0 && r.height > 0 && el.offsetParent !== null
                });
            }
            return out;
        }""")
        _log(f"[fubon][collect] 找到 {len(candidates)} 個信用卡子項候選")
        for c in candidates:
            _log(
                f"  - {c['tag']} '{c['text']}' cls='{c['cls'][:30]}' "
                f"visible={c['visible']} href='{_safe_url(c['href'])}'"
            )

        # Telemetry 2026-06-18: 同時 dump 存款/帳戶相關 menu 候選 (給 cloud 看真實有哪些字)
        # 目的: 確認富邦 menu 用「帳戶總覽」「存款查詢」「我的帳戶」哪個字眼, 才能規劃 collect path
        # 同時 dump 所有 <a> visible text 前 100 條 (上限避免 result_summary 爆)
        deposit_audit = bounded_evaluate(content_frame, r"""() => {
            const KW = /(帳戶|存款|餘額|台幣|外幣|定存|數位帳戶|主帳|匯款|轉帳|資產)/;
            const out = [];
            for (const el of document.querySelectorAll('a, button, li, span, div')) {
                const t = (el.textContent || '').trim();
                if (!t || t.length > 30 || !KW.test(t)) continue;
                const r = el.getBoundingClientRect();
                out.push({
                    tag: el.tagName, text: t,
                    cls: (el.className || '').slice(0, 60),
                    href: el.href || (el.getAttribute && el.getAttribute('href')) || '',
                    visible: r.width > 0 && r.height > 0 && el.offsetParent !== null,
                });
                if (out.length >= 60) break;
            }
            // 去重 (tag+text)
            const seen = new Set();
            return out.filter(x => {
                const k = x.tag + ':' + x.text;
                if (seen.has(k)) return false;
                seen.add(k);
                return true;
            });
        }""")
        out["deposit_menu_audit"] = deposit_audit
        _log(f"[fubon][collect] [TELEMETRY] 存款相關 menu 候選 {len(deposit_audit)} 條")

        if not candidates:
            return finish()

        # === Step 3: 選最優先且 visible 的目標 ===
        # 富邦 menu 元素三種 tag: A (真正連結 href) / DIV (裝飾) / SPAN (icon)
        # 必須優先選 <A>，DIV/SPAN click 不 routing
        priority = ["我的信用卡", "帳務/繳款", "信用卡帳單", "信用卡明細", "帳單查詢", "消費分期", "紅利/哩程"]
        target = None
        for p in priority:
            # 先找 <A> visible
            for c in candidates:
                if c["text"] == p and c["visible"] and c["tag"] == "A":
                    target = c
                    break
            if target:
                break
        # fallback: 任何 visible <A>
        if not target:
            for c in candidates:
                if c["visible"] and c["tag"] == "A":
                    target = c
                    break
        # fallback2: 任何 <A>（即使 offscreen）
        if not target:
            for c in candidates:
                if c["tag"] == "A":
                    target = c
                    break
        # fallback3: 真的沒 <A> 才退 DIV/SPAN
        if not target and candidates:
            target = candidates[0]
        _log(
            f"[fubon][collect] 選擇 target: tag={target['tag']} "
            f"text='{target['text']}' visible={target['visible']} "
            f"href='{_safe_url(target['href'])}'"
        )

        # === Step 4: click target ===
        # 算 txnFrame offset
        offset_x = 0
        offset_y = 0
        for fel in page.query_selector_all("iframe, frame"):
            try:
                name_attr = fel.get_attribute("name") or ""
                if name_attr == "txnFrame":
                    fbox = fel.bounding_box()
                    if fbox:
                        offset_x = fbox["x"]
                        offset_y = fbox["y"]
                        break
            except Exception:
                pass
        _log(f"[fubon][collect] txnFrame offset=({offset_x}, {offset_y})")

        # 走 frame.evaluate 找 A tag (相同 text 多個時優先 <A>，並用 cls 區分 menu_CCCxx)
        click_result = bounded_evaluate(content_frame, """(args) => {
            const {targetText, targetCls} = args;
            // 第一順位：tag=A 且 cls 含 targetCls
            for (const el of document.querySelectorAll('a')) {
                const t = (el.textContent || '').trim();
                if (t !== targetText) continue;
                if (targetCls && !el.className.includes(targetCls.split(' ')[0])) continue;
                try {
                    el.click();
                    return {ok: true, method: 'a.click()', cls: el.className, href: el.href || ''};
                } catch (e) {
                    return {ok: false, error: String(e)};
                }
            }
            // 第二順位：任何 A 同文
            for (const el of document.querySelectorAll('a')) {
                const t = (el.textContent || '').trim();
                if (t !== targetText) continue;
                try {
                    el.click();
                    return {ok: true, method: 'a.click()-fallback', cls: el.className, href: el.href || ''};
                } catch (e) {}
            }
            return {ok: false, error: 'no_anchor_found'};
        }""", {"targetText": target["text"], "targetCls": target["cls"]})
        _log(
            f"[fubon][collect] click result: "
            f"ok={bool(isinstance(click_result, dict) and click_result.get('ok'))}"
        )
        page.wait_for_timeout(6000)

        # === Step 4.5: txnFrame 切換後重新抓 frame（URL 已換）===
        # 點完 <A> 後 txnFrame 會 navigate 到 CCCQU001_Home.faces
        page.wait_for_timeout(2000)
        for f in page.frames:
            url = f.url or ""
            name = f.name or ""
            if name == "txnFrame":
                content_frame = f
                break
        _log(f"[fubon][collect] 切換後 txnFrame url={_safe_url(content_frame.url)}")

        # 立刻抓「我的信用卡」頁卡片清單 (CCCQU001_Home)
        cards_page_text = ""
        with contextlib.suppress(Exception):
            cards_page_text = bounded_evaluate(content_frame, "() => document.body.innerText.slice(0, 10000)") or ""
        out["cards_page_text"] = cards_page_text
        out["cards_page_url"] = content_frame.url
        _log(f"[fubon][collect] cards 頁 text_len={len(cards_page_text)}")

        # === Step 4.6: 再 click「帳務查詢」進帳單明細頁 ===
        # 富邦右上吊牌 quick links: 帳務查詢 / 網路辦卡 / 申辦進度查詢
        # 必須找 <A> tag（LI 是裝飾外殼）
        bill_click = bounded_evaluate(content_frame, """() => {
            const targets = ['帳務查詢', '帳單查詢', '消費明細查詢', '消費明細', '帳單'];
            const found = [];
            for (const t of targets) {
                for (const el of document.querySelectorAll('a')) {
                    if ((el.textContent || '').trim() !== t) continue;
                    found.push({text: t, tag: el.tagName, href: el.href || '', cls: (el.className || '').slice(0, 80), visible: el.offsetParent !== null});
                }
            }
            if (found.length === 0) {
                return {ok: false, error: 'no_anchor_for_bill_tabs', found: []};
            }
            // 優先選 visible 且帶 href 的
            let chosen = found.find(f => f.visible && f.href) || found.find(f => f.visible) || found[0];
            // 真正 click 那個 A
            for (const el of document.querySelectorAll('a')) {
                if ((el.textContent || '').trim() !== chosen.text) continue;
                if (chosen.href && el.href !== chosen.href) continue;
                try {
                    el.click();
                    return {ok: true, clicked: chosen.text, tag: 'A', href: el.href || '', cls: (el.className || '').slice(0, 80), found_count: found.length};
                } catch (e) {
                    return {ok: false, error: String(e)};
                }
            }
            return {ok: false, error: 'click_failed_after_match', found: found};
        }""")
        _log(
            f"[fubon][collect] 帳務查詢 click: "
            f"ok={bool(isinstance(bill_click, dict) and bill_click.get('ok'))}"
        )
        page.wait_for_timeout(6000)

        # === Step 4.7: 再切回 txnFrame 確認 URL ===
        page.wait_for_timeout(2000)
        for f in page.frames:
            if (f.name or "") == "txnFrame":
                content_frame = f
                break
        _log(f"[fubon][collect] 帳務查詢 click 後 txnFrame url={_safe_url(content_frame.url)}")

        # === Step 4.8: 抓「繳款及額度查詢」頁的 text 後，先試點「帳單明細查詢」===
        # 帳務查詢頁有 sub-tabs: 繳款及額度查詢 / 帳單明細查詢 / 未出帳單消費明細 / 消費分析
        # 先抓額度頁，再嘗試切到帳單明細
        amount_page_text = ""
        with contextlib.suppress(Exception):
            amount_page_text = bounded_evaluate(content_frame, "() => document.body.innerText.slice(0, 15000)") or ""
        out["amount_page_text"] = amount_page_text
        _log(f"[fubon][collect] amount 頁 text_len={len(amount_page_text)}")

        # 嘗試 click 帳單明細查詢
        billed_click = bounded_evaluate(content_frame, """() => {
            const targets = ['帳單明細查詢', '帳單明細'];
            for (const t of targets) {
                for (const el of document.querySelectorAll('a')) {
                    if ((el.textContent || '').trim() !== t) continue;
                    try {
                        el.click();
                        return {ok: true, clicked: t, href: el.href || '', cls: (el.className || '').slice(0, 80)};
                    } catch (e) {}
                }
            }
            return {ok: false, error: 'no_billed_tab'};
        }""")
        _log(
            f"[fubon][collect] 帳單明細查詢 click: "
            f"ok={bool(isinstance(billed_click, dict) and billed_click.get('ok'))}"
        )
        page.wait_for_timeout(6000)

        # 切回 txnFrame 抓帳單明細頁 text
        page.wait_for_timeout(2000)
        for f in page.frames:
            if (f.name or "") == "txnFrame":
                content_frame = f
                break
        billed_page_text = ""
        with contextlib.suppress(Exception):
            billed_page_text = bounded_evaluate(content_frame, "() => document.body.innerText.slice(0, 20000)") or ""
        out["billed_page_text"] = billed_page_text
        out["billed_page_url"] = content_frame.url
        _log(f"[fubon][collect] billed 頁 url={_safe_url(content_frame.url)} text_len={len(billed_page_text)}")

        # 嘗試 click 未出帳單消費明細
        pending_click = bounded_evaluate(content_frame, """() => {
            const targets = ['未出帳單消費明細', '未出帳消費明細', '未出帳明細'];
            for (const t of targets) {
                for (const el of document.querySelectorAll('a')) {
                    if ((el.textContent || '').trim() !== t) continue;
                    try {
                        el.click();
                        return {ok: true, clicked: t, href: el.href || ''};
                    } catch (e) {}
                }
            }
            return {ok: false, error: 'no_pending_tab'};
        }""")
        out["pending_click_ok"] = (
            isinstance(pending_click, dict) and pending_click.get("ok") is True)
        _log(
            f"[fubon][collect] 未出帳單 click: "
            f"ok={bool(isinstance(pending_click, dict) and pending_click.get('ok'))}"
        )
        page.wait_for_timeout(6000)

        # 切回 txnFrame
        page.wait_for_timeout(2000)
        for f in page.frames:
            if (f.name or "") == "txnFrame":
                content_frame = f
                break
        pending_page_text = ""
        with contextlib.suppress(Exception):
            pending_page_text = bounded_evaluate(content_frame, "() => document.body.innerText.slice(0, 20000)") or ""
        out["pending_page_text"] = pending_page_text
        out["pending_page_url"] = content_frame.url
        _log(f"[fubon][collect] pending 頁 url={_safe_url(content_frame.url)} text_len={len(pending_page_text)}")

        # === Step 5: dump 點完後所有 frames ===
        page.wait_for_timeout(2000)
        frames_data = []
        for f in page.frames:
            try:
                txt = bounded_evaluate(f, "() => document.body.innerText.slice(0, 15000)")
                if txt and len(txt) > 50:
                    frames_data.append({
                        "name": f.name or "",
                        "url": (f.url or "")[:300],
                        "text": txt,
                    })
            except Exception:
                pass
        out["frames"] = frames_data
        _log(f"[fubon][collect] 點完後 dump {len(frames_data)} frames")
        for fd in frames_data:
            _log(f"  - {fd['name']} url={_safe_url(fd['url'])} text_len={len(fd['text'])}")

        # === Step 6: 找信用卡明細頁 frame ===
        card_frame_text = None
        for fd in frames_data:
            t = fd["text"]
            url = fd["url"]
            # 信用卡頁特徵：URL 含 CGCRE/CGCC/CARD，或 text 含「卡號末四碼/應繳金額/結帳日」
            url_hit = any(k in url.upper() for k in ["CGCRE", "CGCC", "CARD", "CRE"])
            text_hit = any(k in t for k in ["卡號末四碼", "應繳金額", "結帳日", "繳款截止", "本期帳單"])
            if (url_hit or text_hit) and len(t) > len(card_frame_text or ""):
                card_frame_text = t
                out["card_frame_url"] = url
                out["card_frame_name"] = fd["name"]
                out["card_frame_match"] = "url" if url_hit else "text"
        if card_frame_text:
            _log(f"[fubon][collect] ✓ 找到信用卡 frame: name={out.get('card_frame_name')} url={_safe_url(out.get('card_frame_url'))} text_len={len(card_frame_text)}")
            out["card_frame_text"] = card_frame_text
        else:
            _log("[fubon][collect] ⚠️ 沒命中信用卡 frame，僅 dump 待分析")

        # === Step 7 (2026-06-18): 點「我的存款」(CBO_03 / CBOQU003) dump 存款帳戶 ===
        # 富邦 home menu 有 <A class="task_CBOQU003 menu_CBO03" href="...ParamValue=...">我的存款</A>
        # 點下去 navigate 到 txnFrame 的 CBOQU003_Home.faces, body 有 tab-separated table:
        #   header: 帳號 帳戶暱稱 存款類別 分行 幣別 即時餘額 可用餘額 存單號碼 到期日 功能
        # local probe 證實 (debug_fubon_deposit.py): 使用者真有 2 個臺幣活儲帳戶 (餘額 0):
        #   00900000147012 數位活儲 營業部 / 00900000157046 活儲存款 松高分行
        # 之前 collect 完全沒走 deposit path → accounts:0 是 by-design gap 不是漏抓.
        # 修法: 重新 navigate 到 home (CGEQU001) → 點 CBOQU003 → dump deposit frame text.
        try:
            # 直接回已知 home URL；舊 collector hit 掃描結果未被使用。
            home_back = page.goto(
                "https://ebank.taipeifubon.com.tw/B2C/cgequ/cgequ001/CGEQU001_Home.faces",
                wait_until="domcontentloaded", timeout=15000,
            )
            page.wait_for_timeout(5000)
            _log(f"[fubon][collect] 回 home navigate ok={home_back is not None}")
        except Exception as e:
            _log(f"[fubon][collect] 回 home 失敗: {type(e).__name__}")

        # 重新抓 txnFrame
        deposit_frame = None
        page.wait_for_timeout(2000)
        for f in page.frames:
            url = f.url or ""
            name = f.name or ""
            if "CGEQU001" in url or name == "txnFrame":
                deposit_frame = f
                break

        if deposit_frame:
            # 點 a.task_CBOQU003 (我的存款) — 不是 click text 因為 DIV/SPAN 同名沒 routing
            deposit_click = bounded_evaluate(deposit_frame, r"""() => {
                const a = document.querySelector('a.task_CBOQU003, a.menu_CBO03');
                if (!a) return {ok: false, error: 'no_deposit_anchor'};
                try {
                    a.click();
                    return {ok: true, href: a.href || '', text: (a.textContent || '').trim()};
                } catch (e) {
                    return {ok: false, error: String(e)};
                }
            }""")
            _log(
                f"[fubon][collect] 我的存款 click: "
                f"ok={bool(isinstance(deposit_click, dict) and deposit_click.get('ok'))}"
            )
            page.wait_for_timeout(8000)

            # 重新找 txnFrame (URL 應該換到 CBOQU003_Home.faces)
            page.wait_for_timeout(2000)
            for f in page.frames:
                if (f.name or "") == "txnFrame":
                    deposit_frame = f
                    break

            deposit_page_text = ""
            with contextlib.suppress(Exception):
                deposit_page_text = bounded_evaluate(deposit_frame, "() => document.body.innerText.slice(0, 20000)") or ""
            out["deposit_page_text"] = deposit_page_text
            out["deposit_page_url"] = deposit_frame.url
            _log(f"[fubon][collect] deposit 頁 url={_safe_url(deposit_frame.url)} text_len={len(deposit_page_text)}")
        else:
            _log("[fubon][collect] ⚠️ 回 home 後找不到 txnFrame, 跳過 deposit step")
            out["deposit_page_text"] = ""
            out["deposit_page_url"] = ""

        return finish()


if __name__ == "__main__":
    import json
    crawler = FubonCrawler()
    try:
        result = crawler.run(login_url=BASE, headless=False)
    except FubonLoginError as e:
        result = {"error": "login_failed_stop", "detail": str(e)}

    out_file = Path(__file__).resolve().parents[1] / "data" / "fubon_collected.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"\n[done] 已存: {out_file}")
