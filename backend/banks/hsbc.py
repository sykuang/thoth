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
    BankCollectResult,
    BankCrawler,
    ResponseCollector,
    validate_history_coverage,
)
from backend.core.card_bills import (
    card_bill_date, card_bill_money, make_card_bill_fact, publish_card_bill_facts,
)
from backend.core.creds import HsbcCreds
from backend.core.captcha import ocr_bytes
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
            raise RuntimeError("hsbc-history-cursor")
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
                controls = page.locator(selector)
                if any(
                    controls.nth(index).is_visible()
                    for index in range(controls.count())
                ):
                    return False
            body = page.locator("body").inner_text()
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
        matches = page.locator(selector)
        visible = [
            matches.nth(index)
            for index in range(matches.count())
            if matches.nth(index).is_visible()
        ]
        if not visible and optional:
            return None
        if len(visible) != 1 or not visible[0].is_enabled():
            raise HsbcLoginError("登入欄位無法安全確認；未送出登入")
        return visible[0]

    @staticmethod
    def _keyboard_fill(page, field, value: str) -> None:
        field.click()
        field.click(click_count=3)
        page.keyboard.press("Backspace")
        page.keyboard.type(value, delay=80)
        if len(field.input_value()) != len(value):
            raise HsbcLoginError("登入欄位輸入長度不符；未送出登入")

    @staticmethod
    def _exact_button(page, selector: str, text: str, *, candidate_only: bool = False):
        matches = page.locator(selector)
        eligible = []
        for index in range(matches.count()):
            button = matches.nth(index)
            if not button.is_visible() or not button.is_enabled():
                continue
            if candidate_only and " ".join(button.inner_text().split()) != text:
                continue
            eligible.append(button)
        if len(eligible) != 1 or " ".join(eligible[0].inner_text().split()) != text:
            raise HsbcLoginError("找不到唯一且可操作的登入按鈕；未送出登入")
        return eligible[0]

    @staticmethod
    def _captcha_image(page):
        images = page.locator("img")
        eligible = []
        for index in range(images.count()):
            image = images.nth(index)
            if not image.is_visible():
                continue
            src = image.get_attribute("src") or ""
            box = image.bounding_box()
            if (
                src.startswith("data:image/jpeg;base64,")
                and box is not None
                and 80 <= box["width"] <= 200
                and 25 <= box["height"] <= 60
            ):
                eligible.append(image)
        return eligible[0] if len(eligible) == 1 else None

    @classmethod
    def _stable_captcha(cls, page, previous_digest: bytes | None):
        last_digest = None
        for _ in range(12):
            image = cls._captcha_image(page)
            if image is None:
                return None
            raw = image.screenshot()
            digest = hashlib.sha256(raw).digest()
            if digest != previous_digest and digest == last_digest:
                return raw, digest
            last_digest = digest if digest != previous_digest else None
            page.wait_for_timeout(300)
        return None

    @staticmethod
    def _refresh_captcha(page) -> bool:
        buttons = page.locator("button[aria-label='Refresh Captcha']")
        eligible = [
            buttons.nth(index)
            for index in range(buttons.count())
            if buttons.nth(index).is_visible() and buttons.nth(index).is_enabled()
        ]
        if len(eligible) != 1:
            return False
        eligible[0].click()
        return True

    def _solve_captcha(self, page) -> str | None:
        previous_digest = None
        for attempt in range(8):
            try:
                stable = self._stable_captcha(page, previous_digest)
                if stable is None:
                    return None
                raw, previous_digest = stable
            except Exception:
                return None
            try:
                result = ocr_bytes(
                    raw, expected_len=5, alnum_only=True,
                    min_confidence=HSBC_CAPTCHA_MIN_CONFIDENCE,
                )
            except Exception:
                result = None
            if isinstance(result, str) and re.fullmatch(r"[A-Za-z0-9]{5}", result):
                return result
            try:
                if attempt == 7 or not self._refresh_captcha(page):
                    return None
            except Exception:
                return None
        return None

    @staticmethod
    def _response_visible(page) -> bool:
        for selector in (
            ".modal.show", "[role='dialog']", ".error", ".alert", "[role='alert']",
        ):
            matches = page.locator(selector)
            if any(matches.nth(index).is_visible() for index in range(matches.count())):
                return True
        return False

    def submit_credentials_once(self, page) -> None:
        try:
            user_id = self._visible_enabled(page, SEL_USERID, optional=True)
            if user_id is not None:
                self._keyboard_fill(page, user_id, self.creds.user_id)
                first = self._exact_button(
                    page, "button[data-testid='continueButton']", "繼續"
                )
                try:
                    first.click(timeout=8000)
                    page.wait_for_timeout(6000)
                except Exception:
                    raise HsbcLoginError("帳號階段狀態不明；未送出登入") from None
                if getattr(self, "_shared_dialog_blocked", False):
                    raise HsbcLoginError("帳號階段出現未分類提示；未送出登入")
                if self._response_visible(page):
                    raise HsbcLoginError("帳號階段出現未分類提示；未送出登入")

            password = self._visible_enabled(page, SEL_PWD)
            self._keyboard_fill(page, password, self.creds.password)
            captcha_text = self._solve_captcha(page)
            if captcha_text is None:
                raise HsbcLoginError("無法安全辨識驗證碼；未送出登入")
            captcha = self._visible_enabled(page, SEL_CAPTCHA)
            self._keyboard_fill(page, captcha, captcha_text)
            final = self._exact_button(
                page, "button[type='submit']", "繼續", candidate_only=True
            )
            if getattr(self, "_shared_dialog_blocked", False):
                raise HsbcLoginError("登入前出現未分類提示；未送出登入")
            if self._response_visible(page):
                raise HsbcLoginError("登入前出現未分類提示；未送出登入")
        except HsbcLoginError:
            raise
        except Exception:
            raise HsbcLoginError("登入欄位無法安全填寫；未送出登入") from None

        try:
            final.click(timeout=8000)
        except Exception:
            raise HsbcLoginError("登入送出狀態不明；禁止自動重試") from None

        try:
            for _ in range(22):
                page.wait_for_timeout(1000)
                if self._logged_in(page) or self._response_visible(page):
                    return
        except Exception:
            raise HsbcLoginError("登入送出後狀態無法安全確認；禁止自動重試") from None

    # ---------- 抓取 ----------
    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        """登入後抓信用卡：卡片清單(cards 或 legacy cards/suspend) + 逐卡明細。

        注意：HSBC 2026-07-09 實測已把卡片清單 endpoint 從 `cards/suspend`
        改成 `cards`。兩者 payload[] 都是各卡狀態；crawler 必須先認新端點再
        fallback 舊端點，否則會登入成功但 cards=[]，逐卡 posted/unposted 全不跑。
        明細用 cards/{id}/transactions/{posted,unposted} 直接 fetch。
        """
        out: dict = {}
        page.wait_for_timeout(7000)

        # 1) 卡片清單（dashboard 自動載入 cards；舊版曾用 cards/suspend）
        cards = self._card_inventory(collector)
        out["cards"] = cards
        _log(f"[collect] 卡片清單: {len(out['cards'])} 張")

        # 2) 直接 fetch 已自行限流；先卸載 collector，避免把同一 response 再無界解析/保留。
        detach = getattr(collector, "detach", None)
        if callable(detach):
            detach(page)
        out["card_detail"], out["history_coverage"] = self._collect_card_details(
            page,
            collector,
            out["cards"],
            byte_budget=[5_000_000 - collector.hsbc_inventory_bytes],
        )

        out["_final_url"] = page.url
        publish_card_bill_facts(out, _hsbc_card_bill_facts(out))
        return BankCollectResult(**out)

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
            if type(body_size) is not int or not 0 <= body_size <= 5_000_000:
                raise RuntimeError("hsbc-card-inventory-byte-budget")
            inventory_bytes += body_size
            if inventory_bytes > 5_000_000:
                raise RuntimeError("hsbc-card-inventory-byte-budget")
        current = [hit for hit in collector.hits if hit.url == base + "cards"]
        hits = current or [hit for hit in collector.hits if hit.url == base + "cards/suspend"]
        if not hits:
            raise RuntimeError("hsbc-card-inventory-missing")

        payloads = []
        for hit in hits:
            body = hit.resp_json
            body_size = getattr(hit, "body_size", None)
            if (
                (getattr(hit, "raw_url", "") or hit.url)
                not in {base + "cards", base + "cards/suspend"}
                or getattr(hit, "redirected", False)
                or type(body_size) is not int
                or not 0 <= body_size <= 5_000_000
                or hit.method != "GET"
                or hit.status != 200
                or not _is_json_content_type(hit.content_type)
                or not isinstance(body, dict)
                or body.get("success") is not True
                or body.get("error") not in (None, "", [])
                or not isinstance(body.get("payload"), list)
            ):
                raise RuntimeError("hsbc-card-inventory-envelope")
            payloads.append(body["payload"])
        cards = payloads[0]
        if any(payload != cards for payload in payloads[1:]):
            raise RuntimeError("hsbc-card-inventory-replay")
        if len(cards) > 100:
            raise RuntimeError("hsbc-card-inventory-count")

        card_ids: set[str] = set()
        identities: set[str] = set()
        for card in cards:
            if not isinstance(card, dict):
                raise RuntimeError("hsbc-card-inventory-row")
            card_id = card.get("id")
            identity = card.get("maskedCardNumber")
            bounded_fields = (
                card.get(key)
                for key in (
                    "name", "cardType", "cardStatusDisplay", "paymentDueDate", "statementDate",
                )
            )
            if (
                not isinstance(card_id, str)
                or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", card_id) is None
                or not isinstance(identity, str)
                or re.fullmatch(r"[0-9]{4}-\*{4}-\*{4}-[0-9]{4}", identity) is None
                or card.get("cardStatusDisplay") not in {
                    "ACTIVATED", "NOT_ACTIVATED", "CLOSED",
                }
                or card_id in card_ids
                or identity in identities
                or any(
                    value is not None
                    and (not isinstance(value, str) or len(value) > 256)
                    for value in bounded_fields
                )
            ):
                raise RuntimeError("hsbc-card-inventory-identity")
            card_ids.add(card_id)
            identities.add(identity)
        collector.hsbc_inventory_bytes = inventory_bytes
        return cards

    @staticmethod
    def _history_token(collector: ResponseCollector) -> str:
        events = getattr(collector, "auth_token_events", None)
        if not isinstance(events, list):
            raise RuntimeError("hsbc-history-token")
        candidates = events
        valid = []
        for event in candidates:
            source = urlparse(event.get("url", "")) if isinstance(event, dict) else None
            token = event.get("token", "") if isinstance(event, dict) else ""
            if (
                source is not None
                and re.fullmatch(r"Bearer [^\s\r\n]+", token) is not None
                and source.scheme.lower() == "https"
                and (source.hostname or "").lower() == "card.hsbc.com.tw"
                and source.port in (None, 443)
                and source.username is None
                and source.password is None
                and source.path.startswith("/ibk-bff/api/v1/")
                and event.get("redirected") is False
            ):
                valid.append(event)
        if not valid:
            raise RuntimeError("hsbc-history-token")
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
            raise RuntimeError("hsbc-history-mode")
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
            entry["detail"] = self._fetch_json(
                page, f"cards/{cid}", token, byte_budget=byte_budget,
            )
            if byte_budget[0] <= 0:
                raise RuntimeError("hsbc-history-byte-budget")
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
            unp = self._fetch_json(
                page,
                f"cards/{cid}/transactions/unposted?pageSize=200",
                token,
                byte_budget=byte_budget,
            )
            if byte_budget[0] <= 0:
                raise RuntimeError("hsbc-history-byte-budget")
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
    ):
        """Fetch one exact HSBC card API resource and return its payload."""
        if (
            re.fullmatch(
                r"cards/[A-Za-z0-9_-]{1,128}(?:/transactions/unposted\?pageSize=200)?",
                path,
            ) is None
            or re.fullmatch(r"Bearer [^\s\r\n]+", token) is None
        ):
            return None
        url = "https://card.hsbc.com.tw/ibk-bff/api/v1/" + path
        try:
            max_bytes = byte_budget[0] if byte_budget is not None else 5_000_000
            if max_bytes <= 0:
                return None
            result = page.evaluate(
                "async ({url, tok, maxBytes}) => { const controller=new AbortController();"
                " const timer=setTimeout(()=>controller.abort(),30000); try {"
                " const r=await fetch(url,{credentials:'include',redirect:'error',"
                " signal:controller.signal,"
                " headers:{'Accept':'application/json','Authorization':tok}});"
                " const ct=(r.headers.get('content-type')||'').split(';')[0].trim().toLowerCase();"
                " const length=r.headers.get('content-length');"
                " if(r.url!==url||r.status!==200||ct!=='application/json'||"
                " (length!==null&&(!/^\\d+$/.test(length)||Number(length)>maxBytes)))"
                "   {if(r.body)try{await r.body.cancel();}catch(_){}return null;}"
                " const reader=r.body.getReader(); const chunks=[]; let bytes=0;"
                " while(true){const part=await reader.read();if(part.done)break;"
                " bytes+=part.value.byteLength;if(bytes>maxBytes){"
                " try{await reader.cancel();}catch(_){}"
                " return {payload:null,bytes,exceeded:true};}"
                " chunks.push(part.value);}"
                " const merged=new Uint8Array(bytes);let offset=0;"
                " for(const chunk of chunks){merged.set(chunk,offset);offset+=chunk.byteLength;}"
                " let j=null; try{const text=new TextDecoder('utf-8',{fatal:true}).decode(merged);"
                " j=JSON.parse(text);}catch(_){}"
                " return {payload:j&&j.success===true&&!j.error?j.payload:null,bytes};"
                " } catch(_){ return null; } finally { clearTimeout(timer); } }",
                {"url": url, "tok": token, "maxBytes": max_bytes},
            )
            if (
                not isinstance(result, dict)
                or type(result.get("bytes")) is not int
                or result["bytes"] < 0
            ):
                return None
            if result["bytes"] > max_bytes or result.get("exceeded") is True:
                if byte_budget is not None:
                    byte_budget[0] = 0
                return None
            if byte_budget is not None:
                byte_budget[0] -= result["bytes"]
            return result.get("payload")
        except Exception:
            _log("[fetch] HSBC API request failed")
            return None

    @staticmethod
    def _fetch_api_page(
        page, *, url: str, token: str, timeout_ms: int, max_bytes: int = 5_000_000,
    ):
        try:
            return page.evaluate(
                "async ({url, tok, timeoutMs, maxBytes}) => { const controller=new AbortController();"
                " const timer=setTimeout(()=>controller.abort(),timeoutMs); try {"
                " const r=await fetch(url,{credentials:'include',redirect:'error',"
                " signal:controller.signal,"
                " headers:{'Accept':'application/json','Authorization':tok}});"
                " const ct=(r.headers.get('content-type')||'').split(';')[0].trim().toLowerCase();"
                " const length=r.headers.get('content-length');"
                " if(r.url!==url||r.status!==200||ct!=='application/json'||"
                " (length!==null&&(!/^\\d+$/.test(length)||Number(length)>maxBytes)))"
                "   {if(r.body)try{await r.body.cancel();}catch(_){}return null;}"
                " const reader=r.body.getReader(); const chunks=[]; let bytes=0;"
                " while(true){const part=await reader.read();if(part.done)break;"
                " bytes+=part.value.byteLength;if(bytes>maxBytes){"
                " try{await reader.cancel();}catch(_){}return null;}"
                " chunks.push(part.value);}"
                " const merged=new Uint8Array(bytes);let offset=0;"
                " for(const chunk of chunks){merged.set(chunk,offset);offset+=chunk.byteLength;}"
                " const text=new TextDecoder('utf-8',{fatal:true}).decode(merged);"
                " let body=null; try { body=JSON.parse(text); } catch (_) {}"
                " return {url:r.url,status:r.status,contentType:r.headers.get('content-type')||'',"
                " redirected:r.redirected,bytes,body};"
                " } catch (_) { return null; } finally { clearTimeout(timer); } }",
                {"url": url, "tok": token, "timeoutMs": timeout_ms, "maxBytes": max_bytes},
            )
        except Exception:
            return None

    @staticmethod
    def _api_date(raw: object) -> date:
        if not isinstance(raw, str) or re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?",
            raw,
        ) is None:
            raise RuntimeError("hsbc-posted-history")
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except ValueError:
            raise RuntimeError("hsbc-posted-history") from None

    @classmethod
    def _validate_posted_row(cls, row: object, *, end: date) -> date:
        if not isinstance(row, dict):
            raise RuntimeError("hsbc-posted-history")
        posted = cls._api_date(row.get("postedDate"))
        transaction = cls._api_date(row.get("transactionDate"))
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
        is_foreign = row.get("isForeign")
        if (
            match is None
            or amount_value is None
            or not amount_value.is_finite()
            or amount_value != amount_value.to_integral_value()
            or not 0 <= amount_value <= Decimal("100000000")
            or type(row.get("isPositive")) is not bool
            or type(is_foreign) is not bool
            or not isinstance(description, str)
            or not 0 < len(description.strip()) <= 512
            or transaction > posted
            or transaction > end
            or (not is_foreign and row.get("foreignAmount") not in (None, "", "-"))
        ):
            raise RuntimeError("hsbc-posted-history")
        if is_foreign:
            foreign = row.get("foreignAmount")
            foreign_match = re.fullmatch(
                r"([+]?(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d{1,2})?) ([A-Z]{3})",
                foreign if isinstance(foreign, str) else "",
            )
            try:
                foreign_value = (
                    Decimal(foreign_match.group(1).replace(",", ""))
                    if foreign_match else None
                )
            except InvalidOperation:
                foreign_value = None
            if (
                foreign_match is None
                or foreign_match.group(2) == "TWD"
                or foreign_value is None
                or not foreign_value.is_finite()
                or not 0 <= foreign_value <= Decimal("100000000")
            ):
                raise RuntimeError("hsbc-posted-history")
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
        if (
            re.fullmatch(r"[A-Za-z0-9_-]{1,128}", card_id) is None
            or not isinstance(identity, str)
            or not identity.strip()
            or not isinstance(token, str)
            or re.fullmatch(r"Bearer [^\s\r\n]+", token) is None
            or type(start) is not date
            or type(end) is not date
            or start > end
        ):
            raise RuntimeError("hsbc-posted-history")

        base = f"https://card.hsbc.com.tw/ibk-bff/api/v1/cards/{card_id}/transactions/posted"
        page_size = 10
        total_pages = None
        selected_rows = []
        snapshots: list[str] = []
        snapshot_bytes = 0
        pages = 0
        budget = byte_budget if byte_budget is not None else [5_000_000]
        deadline = time.monotonic() + 120
        for page_number in range(500):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or budget[0] <= 0:
                raise RuntimeError("hsbc-posted-history")
            url = f"{base}?pageSize={page_size}&pageNumber={page_number}"
            response = cls._fetch_api_page(
                page,
                url=url,
                token=token,
                timeout_ms=min(30_000, max(1, int(remaining * 1000))),
                max_bytes=budget[0],
            )
            if time.monotonic() >= deadline:
                raise RuntimeError("hsbc-posted-history")
            if (
                not isinstance(response, dict)
                or type(response.get("bytes")) is not int
                or not 0 <= response["bytes"] <= budget[0]
            ):
                raise RuntimeError("hsbc-posted-history")
            budget[0] -= response["bytes"]
            body = response.get("body")
            if (
                response.get("url") != url
                or response.get("status") != 200
                or not _is_json_content_type(response.get("contentType"))
                or response.get("redirected") is not False
                or not isinstance(body, dict)
                or body.get("success") is not True
                or body.get("error") not in (None, "", [])
            ):
                raise RuntimeError("hsbc-posted-history")
            payload = body.get("payload")
            page_info = payload.get("pageInfo") if isinstance(payload, dict) else None
            rows = payload.get("content") if isinstance(payload, dict) else None
            current_total = page_info.get("totalPages") if isinstance(page_info, dict) else None
            if (
                not isinstance(page_info, dict)
                or type(page_info.get("currentPageIndex")) is not int
                or page_info["currentPageIndex"] != page_number
                or type(current_total) is not int
                or not 0 <= current_total <= 500
                or not isinstance(rows, list)
                or len(rows) > page_size
                or (current_total == 0 and (page_number != 0 or bool(rows)))
            ):
                raise RuntimeError("hsbc-posted-history")
            effective_total = max(1, current_total)
            if (
                page_number + 1 < effective_total and len(rows) != page_size
                or (not rows and effective_total > 1)
            ):
                raise RuntimeError("hsbc-posted-history")
            if total_pages is None:
                total_pages = effective_total
            elif effective_total != total_pages:
                raise RuntimeError("hsbc-posted-history")
            snapshot = json.dumps(
                {"pageInfo": page_info, "content": rows},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            snapshot_bytes += len(snapshot.encode("utf-8"))
            if snapshot_bytes > 5_000_000:
                raise RuntimeError("hsbc-posted-history")
            snapshots.append(snapshot)

            for row in rows:
                row_date = cls._validate_posted_row(row, end=end)
                if row_date > end:
                    raise RuntimeError("hsbc-posted-history")
                if row_date >= start:
                    selected_rows.append(row)
            pages += 1
            if page_number + 1 == total_pages:
                break
            page.wait_for_timeout(400)
        else:
            raise RuntimeError("hsbc-posted-history")

        for page_number, snapshot in enumerate(snapshots):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or budget[0] <= 0:
                raise RuntimeError("hsbc-posted-history")
            url = f"{base}?pageSize={page_size}&pageNumber={page_number}"
            replay = cls._fetch_api_page(
                page,
                url=url,
                token=token,
                timeout_ms=min(30_000, max(1, int(remaining * 1000))),
                max_bytes=budget[0],
            )
            if (
                not isinstance(replay, dict)
                or type(replay.get("bytes")) is not int
                or not 0 <= replay["bytes"] <= budget[0]
            ):
                raise RuntimeError("hsbc-posted-history")
            budget[0] -= replay["bytes"]
            replay_body = replay.get("body")
            replay_payload = (
                replay_body.get("payload") if isinstance(replay_body, dict) else None
            )
            if (
                time.monotonic() >= deadline
                or not isinstance(replay, dict)
                or replay.get("url") != url
                or replay.get("status") != 200
                or not _is_json_content_type(replay.get("contentType"))
                or replay.get("redirected") is not False
                or not isinstance(replay_body, dict)
                or replay_body.get("success") is not True
                or replay_body.get("error") not in (None, "", [])
                or not isinstance(replay_payload, dict)
                or json.dumps(
                    {
                        "pageInfo": replay_payload.get("pageInfo"),
                        "content": replay_payload.get("content"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ) != snapshot
            ):
                raise RuntimeError("hsbc-posted-history")

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
