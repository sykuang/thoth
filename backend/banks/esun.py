#!/usr/bin/env python3
"""E.SUN Bank personal e-banking crawler.

玉山銀行 E.SUN ebank 個人網銀抓取器。

登入入口：https://ebank.esunbank.com.tw（JSF 框架，iframe 內 form）
流程：開頁 → 找 iframe1 (`/fco/fco08001/FCO08001_Home.faces`) → 填 3 欄 → 點登入鈕

⚠️ 鐵律（見 wiki/concepts/taiwan-bank-login-retry-account-lockout-lesson.md）：
   login 失敗**絕不自動重打**——max_attempts=1 硬上限。
   玉山表單**無驗證碼**（裝置/OTP 後端風控），login 後可能跳 OTP；headless 階段只 dump UI

第一輪 collect 只先 navigate + dump endpoint，摸清 API 地圖再補 parse。
預設行為：headless browser → 預設 headless=True。
"""
from __future__ import annotations

import contextlib
import math
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.base import ApiHit, BankCollectResult, BankCrawler, ResponseCollector
from backend.core.card_bills import card_bill_money, make_card_bill_fact, publish_card_bill_facts
from backend.core.creds import EsunCreds
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointPhase,
    LoginCheckpointRule,
)

BASE = "https://ebank.esunbank.com.tw"
IFRAME_HINT = "FCO08001_Home.faces"

# 玉山 JSF 欄位 id (escape colon 用 attr selector)
FIELD_NATIONAL_ID = "loginform:custid"   # text, maxlen=10
FIELD_USER_CODE   = "loginform:name"     # password type, maxlen=15
FIELD_PASSWORD    = "loginform:pxsswd"   # password type, maxlen=15 (注意是 pxsswd 不是 password)
LOGIN_BTN_ID      = "loginform:linkCommand"  # <a class="login_btn">
_ESUN_TWD_HISTORY_PATH = "/fco/fao01002/FAO01002.faces"
_ESUN_TWD_FAILURE_RE = re.compile(
    r"(?:錯誤|失敗|異常|逾時|逾期|失效|中斷|請稍後再試|重新登入|"
    r"載入中|讀取中|處理中|查詢中|等待|等候|忙碌|請稍候|timeout|timed?\s*out|"
    r"expired|error|failed|unavailable|loading|waiting|processing|querying|"
    r"please\s+(?:wait|stand\s+by|be\s+patient))",
    re.I,
)


def _esun_history_window(
    crawler: "EsunCrawler",
    identity: str,
    end: date,
) -> tuple[date, date]:
    try:
        floor = end.replace(year=end.year - 1) + timedelta(days=1)
    except ValueError:
        floor = end.replace(year=end.year - 1, day=28) + timedelta(days=1)
    return crawler.transaction_window_start(identity, floor=floor), end


def _log(*a):
    print(*a, file=sys.stderr)


def _sel(field_id: str) -> str:
    """JSF id 含 colon，用 attr selector 才不用 escape。"""
    return f"[id='{field_id}']"


class EsunLoginError(RuntimeError):
    pass


def _esun_card_bill_fact(out: dict):
    bills = out.get("card_bills") or []
    latest_bill = bills[0] if bills and isinstance(bills[0], dict) else {}
    due = card_bill_money(latest_bill.get("due_amount"))
    paid = card_bill_money(latest_bill.get("paid_amount"))
    remaining = max(due - paid, 0) if due is not None and paid is not None else None
    payments = (out.get("card_pay_history") or {}).get("records") or []
    latest_payment = payments[0] if payments and isinstance(payments[0], dict) else {}

    due_date = None
    raw_due = (out.get("card_summary") or {}).get("payment_due_date_roc")
    if isinstance(raw_due, str):
        parts = raw_due.split("/")
        if len(parts) == 3 and all(part.isdigit() for part in parts):
            year, month, day = map(int, parts)
            due_date = f"{year + 1911:04d}-{month:02d}-{day:02d}"
    return make_card_bill_fact(
        remaining_due=remaining,
        payment_due_date=due_date,
        last_payment_amount=latest_payment.get("paid_amount"),
        last_payment_date=latest_payment.get("post_date"),
    )


class EsunCrawler(BankCrawler):
    USES_SHARED_LOGIN_CHECKPOINTS: ClassVar[bool] = True
    HISTORY_COVERAGE_REQUIRED: ClassVar[bool] = True
    HISTORY_COVERAGE_DOMAINS: ClassVar[frozenset[str]] = frozenset({
        "twd_transactions",
    })
    CREDENTIAL_HOSTS = frozenset({"ebank.esunbank.com.tw"})

    def __init__(self):
        super().__init__(name="esun")
        self.creds = EsunCreds.load()

    def _host_filter(self) -> str:
        return "esunbank.com"

    def _find_login_frame(self, page):
        matches = [
            frame for frame in page.frames
            if self._is_login_frame(page, frame)
        ]
        return matches[0] if len(matches) == 1 else None

    def _is_login_frame(self, page, frame) -> bool:
        current = urlparse(frame.url or "")
        return self._frame_origin_allowed(page, frame) and (
            frame.name == "iframe1" or IFRAME_HINT in (current.path or "")
        )

    def _logged_in(self, page) -> bool:
        """Pure one-shot positive check; lifecycle owns all waiting."""
        try:
            current = urlparse(page.url or "")
            if (
                current.scheme.lower() != "https"
                or (current.hostname or "").lower() != "ebank.esunbank.com.tw"
                or current.port not in (None, 443)
                or current.username is not None
                or current.password is not None
            ):
                return False
            if any(
                (frame.name == "iframe1" or IFRAME_HINT in (frame.url or ""))
                and not self._is_login_frame(page, frame)
                for frame in page.frames
            ):
                return False
            login_frames = [
                frame for frame in page.frames if self._is_login_frame(page, frame)
            ]
            if len(login_frames) > 1:
                return False
            scopes = [
                page,
                *(frame for frame in page.frames if frame is not page.main_frame),
            ]
            login_fields_selector = ", ".join(
                _sel(field)
                for field in (FIELD_NATIONAL_ID, FIELD_USER_CODE, FIELD_PASSWORD)
            )
            for scope in scopes:
                fields = scope.locator(login_fields_selector)
                if any(
                    fields.nth(index).is_visible()
                    for index in range(fields.count())
                ):
                    return False
            body = "\n".join(
                scope.evaluate("() => document.body && document.body.innerText || ''") or ""
                for scope in scopes
            )
        except Exception:
            return False

        keywords = (
            "訊息中心",
            "個人資訊",
            "登出",
            "帳戶總覽",
            "歡迎使用",
            "存款",
            "轉帳",
            "信用卡",
            "台幣",
            "外幣",
            "基金",
            "投資",
            "貸款",
            "繳費",
        )
        hits = sum(keyword in body for keyword in keywords)
        dashboard_identity = "登出" in body and "帳戶總覽" in body
        return len(body) >= 500 and dashboard_identity and (
            (not login_frames and hits >= 2) or (len(login_frames) == 1 and hits >= 8)
        )

    def login(self, page) -> bool:
        return self._shared_login(page)

    def prepare_login_page(self, page) -> None:
        page.wait_for_timeout(10000)

    def is_authenticated(self, page) -> bool:
        return self._logged_in(page)

    def login_checkpoint_rules(self) -> tuple[LoginCheckpointRule, ...]:
        all_phases = tuple(CheckpointPhase)
        post_settle = (
            CheckpointPhase.POST_SUBMIT,
            CheckpointPhase.POST_SUBMIT_SETTLE,
        )
        otp_body = re.compile(
            r"^[\s\S]*(?:OTP|一次性密碼|簡訊驗證碼|裝置綁定|"
            r"安全認證|裝置驗證|信任此裝置|新裝置登入)[\s\S]*$",
            re.IGNORECASE,
        )
        password_body = re.compile(
            r"^[\s\S]*(?:(?:立即|必須|請先|需要|需)\s*(?:修改|變更|重設)\s*"
            r"(?:您的?)?\s*密碼|密碼\s*(?:已)?(?:到期|過期)|"
            r"密碼\s*強制\s*(?:修改|變更|重設)|"
            r"強制\s*(?:修改|變更|重設)\s*(?:您的?)?\s*密碼)[\s\S]*$"
        )
        return (
            *(
                LoginCheckpointRule(
                    name=f"esun-otp-required-{suffix}",
                    bank="esun",
                    phases=all_phases,
                    kind=CheckpointKind.OTP_REQUIRED,
                    container_selector=selector,
                    required_body_pattern=otp_body,
                )
                for suffix, selector in (
                    ("modal", ".modal.show"),
                    ("dialog", "[role='dialog']"),
                )
            ),
            *(
                LoginCheckpointRule(
                    name=f"esun-password-change-required-{suffix}",
                    bank="esun",
                    phases=all_phases,
                    kind=CheckpointKind.PASSWORD_CHANGE_REQUIRED,
                    container_selector=selector,
                    required_body_pattern=password_body,
                )
                for suffix, selector in (
                    ("modal", ".modal.show"),
                    ("dialog", "[role='dialog']"),
                )
            ),
            LoginCheckpointRule(
                name="esun-unknown-modal",
                bank="esun",
                phases=all_phases,
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector=".modal.show",
            ),
            LoginCheckpointRule(
                name="esun-unknown-dialog",
                bank="esun",
                phases=all_phases,
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector="[role='dialog']",
            ),
            LoginCheckpointRule(
                name="esun-login-form-still-visible",
                bank="esun",
                phases=post_settle,
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector=_sel(FIELD_NATIONAL_ID),
            ),
        )

    def submit_credentials_once(self, page) -> None:
        try:
            frame = self._find_login_frame(page)
        except Exception:
            raise EsunLoginError("無法安全確認登入頁面；未送出登入") from None
        if frame is None:
            raise EsunLoginError("找不到唯一登入頁面；未送出登入") from None

        try:
            for selector, value, wait in (
                (_sel(FIELD_NATIONAL_ID), self.creds.national_id, 200),
                (_sel(FIELD_USER_CODE), self.creds.user_code, 200),
                (_sel(FIELD_PASSWORD), self.creds.password, 300),
            ):
                candidates = frame.locator(selector)
                if candidates.count() != 1:
                    raise EsunLoginError("登入欄位無法安全填寫；未送出登入")
                field = candidates.nth(0)
                if not field.is_visible() or not field.is_enabled():
                    raise EsunLoginError("登入欄位無法安全填寫；未送出登入")
                field.click()
                field.click(click_count=3)
                page.keyboard.press("Backspace")
                page.keyboard.type(value, delay=80)
                page.wait_for_timeout(wait)
                if len(field.input_value()) != len(value):
                    raise EsunLoginError("登入欄位輸入長度不符；未送出登入")
        except EsunLoginError:
            raise
        except Exception:
            raise EsunLoginError("登入欄位無法安全填寫；未送出登入") from None

        try:
            candidates = frame.locator(_sel(LOGIN_BTN_ID))
            if candidates.count() != 1:
                raise EsunLoginError("找不到唯一且可操作的登入按鈕；未送出登入")
            button = candidates.nth(0)
            if not button.is_visible() or not button.is_enabled():
                raise EsunLoginError("找不到唯一且可操作的登入按鈕；未送出登入")
        except EsunLoginError:
            raise
        except Exception:
            raise EsunLoginError("無法安全確認登入按鈕；未送出登入") from None

        try:
            button.click(timeout=8000)
        except Exception:
            raise EsunLoginError("登入送出狀態不明；禁止自動重試") from None

        try:
            page.wait_for_timeout(10000)
            for _ in range(30):
                page.wait_for_timeout(1000)
                if self._logged_in(page):
                    return
                scopes = [
                    page,
                    *(child for child in page.frames if child is not page.main_frame),
                ]
                for scope in scopes:
                    for selector in (
                        ".modal.show",
                        "[role='dialog']",
                        _sel(FIELD_NATIONAL_ID),
                    ):
                        checkpoints = scope.locator(selector)
                        if any(
                            checkpoints.nth(index).is_visible()
                            for index in range(checkpoints.count())
                        ):
                            return
        except Exception:
            return

    @staticmethod
    def _unique_twd_query_frame(frames):
        matches = []
        for frame in frames:
            try:
                if frame.evaluate("""() => Boolean(
                    document.querySelector('select[id="fao01002:dract"]') &&
                    document.querySelector('input[name="fao01002:linkCommand"]')
                )""") is True:
                    matches.append(frame)
            except Exception:
                raise RuntimeError("esun-twd-history-form") from None
        if len(matches) != 1:
            raise RuntimeError("esun-twd-history-form")
        return matches[0]

    @staticmethod
    def _validated_twd_options(options) -> list[dict]:
        if not isinstance(options, list):
            raise RuntimeError("esun-twd-history-inventory")
        out = []
        for option in options:
            if not isinstance(option, dict):
                raise RuntimeError("esun-twd-history-inventory")
            index = option.get("index")
            text = option.get("text")
            value = option.get("value")
            if (
                type(index) is not int
                or index < 0
                or not isinstance(text, str)
                or text != text.strip()
            ):
                raise RuntimeError("esun-twd-history-inventory")
            if option == {"index": 0, "text": "===請選擇===", "value": ""}:
                continue
            if not any(marker in text for marker in ("臺幣", "台幣")):
                raise RuntimeError("esun-twd-history-inventory")
            matches = re.findall(r"(?<!\d)\d{13}(?!\d)", text or "")
            if (
                len(matches) != 1
                or not isinstance(value, str)
                or not value
                or value != value.strip()
            ):
                raise RuntimeError("esun-twd-history-inventory")
            out.append({**option, "identity": matches[0]})
        identities = [row["identity"] for row in out]
        if len(identities) != len(set(identities)):
            raise RuntimeError("esun-twd-history-inventory")
        if not identities:
            raise RuntimeError("esun-twd-history-inventory")
        return out

    @staticmethod
    def _capture_twd_response(response, hits: list[ApiHit]) -> None:
        try:
            request = response.request
            current = urlparse(response.url or "")
            if (
                request.method != "POST"
                or current.scheme != "https"
                or current.hostname != "ebank.esunbank.com.tw"
                or current.port not in (None, 443)
                or current.username is not None
                or current.password is not None
                or current.path != _ESUN_TWD_HISTORY_PATH
                or current.query
                or current.fragment
            ):
                return
            fields = parse_qs(request.post_data or "", keep_blank_values=True)
            safe_fields = {
                key: fields.get(key)
                for key in (
                    "fao01002:dract",
                    "fao01002:startDate",
                    "fao01002:endDate",
                )
            }
            hits.append(ApiHit(
                url=response.url,
                method=request.method,
                status=response.status,
                req_body=safe_fields,
                content_type=response.headers.get("content-type", ""),
            ))
        except Exception:
            return

    @staticmethod
    def _validated_twd_transport(
        hits,
        *,
        result_url: str,
        account_value: str,
        start: date,
        end: date,
    ) -> None:
        parsed_result = urlparse(result_url)
        if (
            parsed_result.scheme != "https"
            or parsed_result.hostname != "ebank.esunbank.com.tw"
            or parsed_result.port not in (None, 443)
            or parsed_result.username is not None
            or parsed_result.password is not None
            or parsed_result.path != _ESUN_TWD_HISTORY_PATH
            or parsed_result.query
            or parsed_result.fragment
        ):
            raise RuntimeError("esun-twd-history-transport")
        expected_fields = {
            "fao01002:dract": account_value,
            "fao01002:startDate": start.strftime("%Y/%m/%d"),
            "fao01002:endDate": end.strftime("%Y/%m/%d"),
        }
        matches = []
        for hit in hits:
            content_type = (
                hit.content_type.split(";", 1)[0].strip().lower()
                if isinstance(hit.content_type, str)
                else ""
            )
            if (
                hit.url == result_url
                and hit.method == "POST"
                and type(hit.status) is int
                and 200 <= hit.status < 300
                and content_type in {"text/html", "application/xhtml+xml", "text/xml", "application/xml"}
                and isinstance(hit.req_body, dict)
            ):
                fields = hit.req_body
                if all(fields.get(key) == [value] for key, value in expected_fields.items()):
                    matches.append(hit)
        if len(matches) != 1:
            raise RuntimeError("esun-twd-history-transport")

    @staticmethod
    def _fresh_twd_result(candidates) -> dict:
        if len(candidates) != 1:
            raise RuntimeError("esun-twd-history-result")
        result = candidates[0]
        if not isinstance(result, dict) or result.get("evidenceFresh") is not True:
            raise RuntimeError("esun-twd-history-stale-result")
        return result

    @staticmethod
    def _validated_twd_history_result(
        result,
        *,
        identity: str,
        start: date,
        end: date,
    ) -> dict:
        if not isinstance(result, dict):
            raise RuntimeError("esun-twd-history-result")
        period = result.get("clicked_period")
        submit = result.get("submit")
        snapshot = result.get("snapshot")
        text = result.get("text")
        current = urlparse(result.get("url") or "")
        if (
            result.get("account_no") != identity
            or result.get("selected_identity") != identity
            or not isinstance(period, dict)
            or period.get("checked") is not True
            or period.get("start") != start.strftime("%Y/%m/%d")
            or period.get("end") != end.strftime("%Y/%m/%d")
            or not isinstance(submit, dict)
            or submit.get("clicked") != "visible-query"
            or current.scheme != "https"
            or current.hostname != "ebank.esunbank.com.tw"
            or current.port not in (None, 443)
            or current.username is not None
            or current.password is not None
            or current.path != _ESUN_TWD_HISTORY_PATH
            or not isinstance(text, str)
            or _ESUN_TWD_FAILURE_RE.search(text) is not None
            or re.search(rf"(?<!\d){re.escape(identity)}(?!\d)", text) is None
            or re.search(
                rf"(?<!\d){re.escape(start.strftime('%Y/%m/%d'))}(?!\d)", text,
            ) is None
            or re.search(
                rf"(?<!\d){re.escape(end.strftime('%Y/%m/%d'))}(?!\d)", text,
            ) is None
            or not isinstance(snapshot, dict)
            or snapshot.get("busy") is not False
        ):
            raise RuntimeError("esun-twd-history-result")
        pager = snapshot.get("pager")
        if (
            not isinstance(pager, dict)
            or set(pager) != {"present", "actionableNext"}
            or pager.get("present") is not False
            or type(pager.get("actionableNext")) is not int
            or pager["actionableNext"] != 0
            or re.search(r"(?:下一頁|下頁)", text)
        ):
            raise RuntimeError("esun-twd-history-result")
        total_count = snapshot.get("totalCount")
        if type(total_count) is not int or total_count < 0:
            raise RuntimeError("esun-twd-history-result")
        has_grid = snapshot.get("hasGrid")
        grid_candidate_count = snapshot.get("gridCandidateCount")
        grid_text = snapshot.get("gridText")
        if (
            type(has_grid) is not bool
            or type(grid_candidate_count) is not int
            or grid_candidate_count != (1 if has_grid else 0)
            or not isinstance(grid_text, str)
        ):
            raise RuntimeError("esun-twd-history-result")
        if has_grid:
            grid_rows = snapshot.get("gridRows")
            if not isinstance(grid_rows, list):
                raise RuntimeError("esun-twd-history-result")
            try:
                dates = [
                    date.fromisoformat(match.group(1).replace("/", "-"))
                    for cells in grid_rows
                    for cell in cells
                    if (match := re.fullmatch(r"\*?(20\d{2}/\d{2}/\d{2})", cell))
                ]
            except (TypeError, ValueError):
                raise RuntimeError("esun-twd-history-result") from None
            row_count = snapshot.get("gridRowCount")
            if (
                type(row_count) is not int
                or row_count <= 0
                or total_count != row_count
                or not grid_text.strip()
                or len(dates) != row_count
                or snapshot.get("emptyMarker") is not None
                or any(day < start or day > end for day in dates)
            ):
                raise RuntimeError("esun-twd-history-result")
            from backend.core.persist.esun import _parse_esun_twd_txn_results

            try:
                rows = _parse_esun_twd_txn_results([result])
            except (TypeError, ValueError):
                raise RuntimeError("esun-twd-history-result") from None
            if len(rows) != row_count:
                raise RuntimeError("esun-twd-history-result")
            for row in rows:
                try:
                    txn_day = date.fromisoformat(str(row["datetime"])[:10])
                    account_day = date.fromisoformat(row["account_date"])
                except (KeyError, TypeError, ValueError):
                    raise RuntimeError("esun-twd-history-result") from None
                money = (row.get("expend"), row.get("income"))
                if (
                    row.get("account_no") != identity
                    or not start <= txn_day <= end
                    or not start <= account_day <= end
                    or not isinstance(row.get("desc"), str)
                    or not row["desc"].strip()
                    or not any(value is not None for value in money)
                    or any(
                        value is not None
                        and (type(value) not in (int, float) or not math.isfinite(value) or value < 0)
                        for value in money
                    )
                    or type(row.get("balance")) not in (int, float)
                    or not math.isfinite(row["balance"])
                ):
                    raise RuntimeError("esun-twd-history-result")
            status = "complete"
        else:
            if (
                grid_text
                or snapshot.get("gridRows") != []
                or snapshot.get("gridRowCount") != 0
                or total_count != 0
                or snapshot.get("emptyMarker") not in {
                    "查無交易資料", "查無資料", "無交易明細",
                }
            ):
                raise RuntimeError("esun-twd-history-result")
            status = "explicit_empty"
        return {
            "identity": identity,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "status": status,
            "pages": 1,
        }

    # ---------- 抓取 ----------
    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        """玉山 collect：解析首頁帳戶總覽 + navigate 信用卡帳單 + endpoint 地圖。"""
        out: dict = {}
        page.wait_for_timeout(8000)

        from backend.core.store import _data_root
        debug_dir = _data_root() / "esun_collect"
        debug_dir.mkdir(parents=True, exist_ok=True)

        # 第 1 階段：首頁帳戶總覽 dump
        out["final_url"] = page.url
        try:
            out["main_text"] = (page.evaluate("document.body.innerText") or "")[:5000]
        except Exception:
            out["main_text"] = ""

        frames_data = []
        for f in page.frames:
            if f == page.main_frame:
                continue
            try:
                txt = f.evaluate("() => document.body.innerText.slice(0, 8000)")
                frames_data.append({
                    "url": f.url[:200],
                    "text_preview": txt,
                })
            except Exception:
                pass
        out["frames"] = frames_data

        # 從 frame text 抽帳戶總覽（regex）
        out["accounts"] = self._parse_account_overview(frames_data)
        _log(f"[esun][collect] 解析到 {len(out['accounts'])} 個帳戶")


        # 第 1b 階段：navigate 臺幣「存款交易明細查詢」
        # 2026-06-30: 補 account drilldown 真正資料源。玉山 menu 裡 TWD / FX 都有
        # 同名「存款交易明細查詢」，_navigate_menu 會避開我的最愛並優先點第一個
        # actionable 候選；目前首頁左側順序第一個就是「臺幣存匯 → 臺幣帳戶查詢」。
        try:
            twd_nav = self._navigate_menu(page, "存款交易明細查詢", debug_dir, "twd_txn_form.png")
            out["twd_txn_nav_probe"] = twd_nav
            twd_clicked = any(
                fr.get("result", {}).get("clicked") for fr in twd_nav.get("frames", [])
            )
            if not twd_clicked:
                raise RuntimeError("esun-twd-history-navigation")
            else:
                # 對每個臺幣帳戶提交近一個月查詢。玉山 JSF combo 的 visible select
                # 需要 select_option 讓前端 helper 更新 hidden JSON value；radio label click
                # 比直接 checked 更穩，跟信用卡 FCM01004 pattern 一致。
                twd_results = []
                coverage_receipts = []
                coverage_expected = []
                expected_twd_identities: set[str] = set()
                submitted_accounts: set[str] = set()
                query_frame = self._unique_twd_query_frame(page.frames)
                form_url = urlparse(query_frame.url or "")
                if (
                    form_url.scheme != "https"
                    or form_url.hostname != "ebank.esunbank.com.tw"
                    or form_url.port not in (None, 443)
                    or form_url.username is not None
                    or form_url.password is not None
                    or form_url.path != _ESUN_TWD_HISTORY_PATH
                ):
                    raise RuntimeError("esun-twd-history-form")
                bank_today_raw = query_frame.evaluate(r"""() =>
                    (document.querySelector('#sysInfo')?.textContent || '')
                        .match(/"today":"(\d{4}\/\d{2}\/\d{2})"/)?.[1] || ''
                """)
                try:
                    bank_today = date.fromisoformat(bank_today_raw.replace("/", "-"))
                except (AttributeError, ValueError):
                    raise RuntimeError("esun-twd-history-bank-date") from None
                if query_frame is not None:
                    try:
                        acct_options = query_frame.evaluate(r"""() => {
                            const s = document.querySelector('select[id="fao01002:dract"]');
                            if (!s) return [];
                            return [...s.options].map((o, index) => ({
                                index,
                                text: (o.textContent || '').trim(),
                                value: o.value || '',
                            }));
                        }""")
                    except Exception:
                        raise RuntimeError("esun-twd-history-inventory") from None
                    validated_options = self._validated_twd_options(acct_options)
                    expected_twd_identities = {
                        option["identity"] for option in validated_options
                    }
                    for account_index, opt in enumerate(validated_options, start=1):
                        text = opt["text"]
                        account_no = opt["identity"]
                        submitted_accounts.add(account_no)
                        try:
                            query_frame.locator("select[id='fao01002:dract']").select_option(index=int(opt["index"]), timeout=8000)
                            page.wait_for_timeout(800)
                            selected = query_frame.evaluate(r"""() => {
                                const s = document.querySelector('select[id="fao01002:dract"]');
                                const o = s?.options[s.selectedIndex];
                                return s && o ? {
                                    index: s.selectedIndex,
                                    value: s.value || '',
                                    text: (o.textContent || '').trim(),
                                } : null;
                            }""")
                            if selected != {
                                "index": opt["index"],
                                "value": opt["value"],
                                "text": opt["text"],
                            }:
                                raise RuntimeError("esun-twd-history-selected-account")
                            # FAO01002 retains the latest year. Incremental queries use the
                            # account-scoped cursor with the shared seven-day overlap.
                            history_start, history_end = _esun_history_window(
                                self, account_no, bank_today,
                            )
                            expected_period = {
                                "start": history_start.strftime("%Y/%m/%d"),
                                "end": history_end.strftime("%Y/%m/%d"),
                            }
                            clicked_period = query_frame.evaluate(r"""(period) => {
                                const r = document.querySelector('input[id="fao01002:j_id_intervalrdo4"], input[name="fao01002:intervalrdo"][value="4"]');
                                if (!r) return {ok: false, error: 'no intervalrdo4'};
                                const label = r.closest('label');
                                if (label) {
                                    try { label.click(); } catch (e) {}
                                }
                                r.checked = true;
                                r.dispatchEvent(new Event('change', {bubbles: true}));
                                r.dispatchEvent(new Event('click', {bubbles: true}));
                                for (const other of document.querySelectorAll('input[name="fao01002:intervalrdo"]')) {
                                    if (other !== r) other.checked = false;
                                }
                                for (const lbl of document.querySelectorAll('.radiobutton-group label')) {
                                    lbl.classList.toggle('checked', lbl.contains(r));
                                }
                                const s = document.querySelector('input[id="fao01002:startDate"]');
                                const e = document.querySelector('input[id="fao01002:endDate"]');
                                if (s) {
                                    s.value = period.start;
                                    s.dispatchEvent(new Event('change', {bubbles: true}));
                                }
                                if (e) {
                                    e.value = period.end;
                                    e.dispatchEvent(new Event('change', {bubbles: true}));
                                }
                                return {ok: true, checked: r.checked, start: s?.value || '', end: e?.value || ''};
                            }""", expected_period)
                            if clicked_period != {"ok": True, "checked": True, **expected_period}:
                                raise RuntimeError("esun-twd-history-period")
                            page.wait_for_timeout(500)
                            sort_selected = query_frame.evaluate(r"""() => {
                                const r = document.querySelector('input[id="fao01002:j_id_sort1"], input[name="fao01002:txDateOrder"][value="1"]');
                                if (!r) return false;
                                r.checked = true;
                                r.dispatchEvent(new Event('change', {bubbles: true}));
                                return r.checked;
                            }""")
                            if sort_selected is not True:
                                raise RuntimeError("esun-twd-history-sort")
                            stale_evidence = query_frame.evaluate(r"""() => {
                                const selector = '[id="fao01002:grid_DataGridBody"], [id*="fao01002:grid"]';
                                const evidence = [...document.querySelectorAll(selector)];
                                const emptyLabels = new Set(['查無交易資料', '查無資料', '無交易明細']);
                                for (const el of document.querySelectorAll('*')) {
                                    if (emptyLabels.has((el.textContent || '').trim()) && ![...el.children].some(
                                        (child) => emptyLabels.has((child.textContent || '').trim())
                                    )) evidence.push(el);
                                }
                                for (const el of new Set(evidence)) {
                                    el.setAttribute('data-hermes-stale-evidence', '1');
                                }
                                return {ok: true, marked: new Set(evidence).size};
                            }""")
                            if (
                                not isinstance(stale_evidence, dict)
                                or stale_evidence.get("ok") is not True
                                or type(stale_evidence.get("marked")) is not int
                            ):
                                raise RuntimeError("esun-twd-history-stale-result")
                            operation_hits: list[ApiHit] = []
                            response_listener = lambda response: self._capture_twd_response(
                                response, operation_hits,
                            )
                            page.on("response", response_listener)
                            try:
                                submit_info = query_frame.evaluate(r"""() => {
                                    const visible = (el) => {
                                        const r = el.getBoundingClientRect();
                                        const st = window.getComputedStyle(el);
                                        return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
                                    };
                                    for (const el of document.querySelectorAll('button,a,input[type="button"],input[type="submit"],input[type="image"]')) {
                                        const t = (el.textContent || el.value || el.title || '').replace(/\s+/g, '').trim();
                                        if (t === '查詢' && visible(el)) {
                                            el.click();
                                            return {clicked: 'visible-query', tag: el.tagName, id: el.id || '', name: el.name || '', text: t};
                                        }
                                    }
                                    return {clicked: null};
                                }""")
                                if submit_info != {
                                    "clicked": "visible-query",
                                    "tag": submit_info.get("tag"),
                                    "id": submit_info.get("id"),
                                    "name": submit_info.get("name"),
                                    "text": "查詢",
                                }:
                                    raise RuntimeError("esun-twd-history-submit")
                                for _ in range(90):
                                    if operation_hits:
                                        break
                                    page.wait_for_timeout(100)
                                if not operation_hits:
                                    raise RuntimeError("esun-twd-history-response-timeout")
                            finally:
                                page.remove_listener("response", response_listener)
                            # The response event exposes headers before JSF finishes replacing the result DOM.
                            page.wait_for_timeout(9000)
                            result_candidates = []
                            for rf in (query_frame,):
                                try:
                                    snap = rf.evaluate(r"""(expected) => {
                                        const exactToken = (text, token) => new RegExp(
                                            `(^|\\D)${token}(?!\\d)`
                                        ).test(text);
                                        const visible = (el) => {
                                            const r = el.getBoundingClientRect();
                                            if (r.width <= 0 || r.height <= 0) return false;
                                            for (let node = el; node; node = node.parentElement) {
                                                const st = window.getComputedStyle(node);
                                                if (st.display === 'none'
                                                    || st.visibility === 'hidden'
                                                    || st.visibility === 'collapse'
                                                    || Number(st.opacity) === 0
                                                    || node.hidden
                                                    || (node.getAttribute('aria-hidden') || '').toLowerCase() === 'true') return false;
                                            }
                                            return true;
                                        };
                                        const scopes = [...document.querySelectorAll(
                                            '.qryresult, [id^="fao01002:"][id*="qry" i]'
                                        )].filter((el) => {
                                            if (!visible(el)) return false;
                                            const text = el.textContent || '';
                                            return exactToken(text, expected.identity)
                                                && exactToken(text, expected.start)
                                                && exactToken(text, expected.end);
                                        });
                                        if (scopes.length !== 1) return {bound: false, scopeCount: scopes.length};
                                        const resultScope = scopes[0];
                                        const bodyText = resultScope.textContent || '';
                                        const grids = [...resultScope.querySelectorAll(
                                            '[id="fao01002:grid_DataGridBody"], [id*="fao01002:grid"]'
                                        )].filter(visible);
                                        const grid = grids.length === 1 ? grids[0] : null;
                                        const gridText = grid ? (grid.textContent || '') : '';
                                        const gridRows = grid ? [...grid.querySelectorAll('tr')].map((row) => {
                                            const cells = [...row.querySelectorAll(':scope > th, :scope > td')];
                                            if (!visible(row) || cells.some((cell) => !visible(cell))) return [];
                                            return cells.map((cell) => (cell.textContent || '').trim());
                                        }).filter((cells) => cells.some(
                                            (cell) => /(^|\D)20\d{2}\/\d{1,2}\/\d{1,2}(?!\d)/.test(cell)
                                        )) : [];
                                        const pagerRoots = [...resultScope.querySelectorAll(
                                            '[id*="pager" i], [class*="pager" i], [id*="paginator" i], [class*="paginator" i]'
                                        )];
                                        const nextControls = [...resultScope.querySelectorAll('a,button,input')].filter((el) => {
                                            const marker = [
                                                el.textContent, el.value, el.title,
                                                el.getAttribute('aria-label'), el.getAttribute('rel'),
                                                el.id, el.className,
                                            ].filter(Boolean).join(' ').replace(/\s+/g, '');
                                            return /(?:下一頁|下頁|next|page-next|pagenext|>)/i.test(marker);
                                        });
                                        const hasNextText = /(?:下一頁|下頁)/.test(bodyText);
                                        const emptyLabels = new Set(['查無交易資料', '查無資料', '無交易明細']);
                                        const emptyMarkers = [resultScope, ...resultScope.querySelectorAll('*')].filter((el) => {
                                            if (!visible(el)) return false;
                                            const label = (el.textContent || '').trim();
                                            return emptyLabels.has(label) && ![...el.children].some(
                                                (child) => emptyLabels.has((child.textContent || '').trim())
                                            );
                                        });
                                        const totals = [...bodyText.matchAll(
                                            /(?:共|總計|總筆數|資料筆數)\s*(\d+)\s*筆/g
                                        )].map((match) => Number(match[1]));
                                        const uniqueTotals = [...new Set(totals)];
                                        return {
                                            bound: true,
                                            href: location.href,
                                            bodyText,
                                            busy: [document.documentElement, ...document.querySelectorAll('*')].some((el) =>
                                                visible(el) && (
                                                    (el.getAttribute('aria-busy') || '').toLowerCase() === 'true'
                                                    || (el.getAttribute('role') || '').toLowerCase() === 'progressbar'
                                                    || el.tagName.toLowerCase() === 'progress'
                                                    || /(?:loading|loader|spinner|progress|processing|querying|waiting|busy|blockui)/i.test([
                                                        el.id, el.getAttribute('class'),
                                                    ].filter(Boolean).join(' '))
                                                )
                                            ),
                                            evidenceFresh: grid
                                                ? !grid.hasAttribute('data-hermes-stale-evidence')
                                                : emptyMarkers.length === 1
                                                    && !emptyMarkers[0].hasAttribute('data-hermes-stale-evidence'),
                                            gridText,
                                            gridRows,
                                            hasGrid: !!grid,
                                            gridCandidateCount: grids.length,
                                            gridRowCount: gridRows.length,
                                            totalCount: uniqueTotals.length === 1 ? uniqueTotals[0] : null,
                                            pager: {
                                                present: pagerRoots.length > 0 || hasNextText,
                                                actionableNext: nextControls.length,
                                            },
                                            emptyMarker: emptyMarkers.length === 1
                                                ? (emptyMarkers[0].textContent || '').trim()
                                                : null,
                                            gridHtml: grid ? grid.outerHTML.slice(0, 20000) : '',
                                            qryResult: [...document.querySelectorAll('.qryresult, [class*=qryresult], [id*=qry]')].map((el) => ({
                                                id: el.id || '', cls: (el.className || '').toString(), visible: el.offsetParent !== null,
                                                text: (el.textContent || '').slice(0, 20000),
                                                html: el.outerHTML.slice(0, 20000),
                                            })).slice(0, 10),
                                            tables: [...document.querySelectorAll('table')].map((t, idx) => ({
                                                idx, id: t.id || '', cls: (t.className || '').toString(), visible: t.offsetParent !== null,
                                                text: (t.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 4000),
                                                html: t.outerHTML.slice(0, 12000),
                                            })).filter(t => /交易|日期|金額|餘額|摘要|明細|TWD|查詢/.test(t.text) || t.id.includes('fao01002')).slice(0, 30),
                                        };
                                    }""", {"identity": account_no, **expected_period})
                                    if snap.get("bound") is True:
                                        result_candidates.append(snap)
                                except Exception:
                                    pass
                            bound_result = self._fresh_twd_result(result_candidates)
                            result_text = bound_result["bodyText"]
                            result_url = bound_result["href"]
                            result_snapshot = {
                                key: bound_result[key]
                                for key in (
                                    "hasGrid", "gridCandidateCount", "gridText", "gridRowCount", "totalCount", "pager",
                                    "busy", "evidenceFresh",
                                    "emptyMarker", "gridRows",
                                )
                            }
                            self._validated_twd_transport(
                                operation_hits,
                                result_url=result_url,
                                account_value=opt["value"],
                                start=history_start,
                                end=history_end,
                            )
                            result = {
                                "account_no": account_no,
                                "selected_identity": account_no,
                                "selected_text": text,
                                "period": "custom",
                                "clicked_period": clicked_period,
                                "submit": submit_info,
                                "url": result_url,
                                "text": result_text,
                                "snapshot": result_snapshot,
                            }
                            receipt = self._validated_twd_history_result(
                                result,
                                identity=account_no,
                                start=history_start,
                                end=history_end,
                            )
                            coverage_receipts.append(receipt)
                            coverage_expected.append({
                                "identity": account_no,
                                "start": history_start.isoformat(),
                                "end": history_end.isoformat(),
                            })
                            twd_results.append({
                                "account_no": account_no,
                                "start": receipt["start"],
                                "end": receipt["end"],
                                "status": receipt["status"],
                                "snapshot": result_snapshot,
                            })
                            _log(
                                f"[esun][collect][twd] account {account_index}/{len(validated_options)} "
                                f"submit={submit_info.get('clicked')} text_len={len(result_text)}"
                            )
                        except Exception:
                            raise RuntimeError("esun-twd-history-account") from None
                if submitted_accounts != expected_twd_identities:
                    raise RuntimeError("esun-twd-history-inventory")
                out["twd_txn_results"] = twd_results
                mode = os.environ.get("BANK_CRAWLER_HISTORY_MODE", "full")
                if mode not in {"full", "incremental"}:
                    raise RuntimeError("esun-twd-history-mode")
                out["history_coverage"] = {
                    "mode": mode,
                    "domains": [{
                        "domain": "twd_transactions",
                        "expected": coverage_expected,
                        "windows": coverage_receipts,
                    }],
                }
        except Exception:
            raise RuntimeError("esun-twd-history") from None

        # 第 2 階段：navigate 信用卡帳單資訊（hover mega menu，禁用我的最愛）
        # 玉山是 widget 切換（_leftMenuLoadWidget），不是新分頁——點完後 iframe 內 widget 替換
        try:
            nav_info = self._navigate_credit_card_bill(page, debug_dir)
            out["card_nav_probe"] = nav_info
            # 玉山 widget 載入慢且可能掛新 iframe — 等 10 秒讓 Playwright 註冊新 frame
            page.wait_for_timeout(10000)

            # debug: 對比 page.frames vs DOM iframe 真實數
            try:
                dom_iframe_count = page.evaluate("() => document.querySelectorAll('iframe').length")
                _log(f"[esun][collect] DOM iframe count={dom_iframe_count} vs page.frames={len(page.frames)}")
                # 各 frame iframe count
                for f in page.frames:
                    try:
                        sub = f.evaluate("() => document.querySelectorAll('iframe').length")
                        sub_srcs = f.evaluate("() => [...document.querySelectorAll('iframe')].map(e => (e.src || e.id || '').slice(0, 150))")
                        _log(f"[esun][collect][nest] {f.url[:60]} sub_iframes={sub} srcs={sub_srcs[:3]}")
                    except Exception:
                        pass
            except Exception as e:
                _log(f"[esun][collect] iframe count debug failed: {e}")

            # debug: dump 所有 frame URL + body innerText 長度
            all_frames_meta = []
            for f in page.frames:
                try:
                    url = f.url[:200]
                    # 用 textContent（不受 CSS hidden / visibility 影響）+ innerText 兩種對比
                    txt_inner = f.evaluate("() => document.body.innerText.slice(0, 15000)")
                    txt_content = f.evaluate("() => document.body.textContent.slice(0, 20000)")
                    all_frames_meta.append({
                        "url": url,
                        "inner_len": len(txt_inner),
                        "content_len": len(txt_content),
                        "has_card_kw_inner": any(k in txt_inner for k in ("歸戶信用額度", "預借現金額度")),
                        "has_card_kw_content": any(k in txt_content for k in ("歸戶信用額度", "預借現金額度")),
                        "first_200": txt_inner[:200],
                    })
                except Exception as e:
                    all_frames_meta.append({"url": getattr(f, "url", "?")[:80], "error": str(e)})
            out["card_all_frames_meta"] = all_frames_meta
            for m in all_frames_meta:
                _log(f"[esun][collect][meta] {m.get('url', '')[:60]} inner_len={m.get('inner_len')} content_len={m.get('content_len')} card_kw inner={m.get('has_card_kw_inner')} content={m.get('has_card_kw_content')}")

            card_frames = []
            for f in page.frames:
                if f == page.main_frame:
                    continue
                try:
                    url = f.url[:200]
                    # 改用 textContent 而非 innerText（widget 可能在 overflow:hidden 容器內）
                    txt = f.evaluate("() => document.body.textContent.slice(0, 30000)")
                    # 收集真正帶信用卡帳單資料的 frame（嚴格 keyword，排掉只有選單名稱的）
                    if any(k in txt for k in ("歸戶信用額度", "本期繳款截止", "預借現金額度", "e point", "應繳總金額", "ATM 繳款編號")):
                        card_frames.append({
                            "url": url,
                            "text_preview": txt,
                            "page_url": page.url[:120],
                        })
                except Exception:
                    pass
            out["card_frames"] = card_frames

            # 從 card_frames 解析信用卡 summary + 帳單列表
            if card_frames:
                full_text = card_frames[0].get("text_preview", "")
                out["card_summary"] = self._parse_card_summary(full_text)
                out["card_bills"] = self._parse_card_bills(full_text)
                _log(f"[esun][collect] card_summary={out['card_summary']}")
                _log(f"[esun][collect] card_bills count={len(out['card_bills'])}")

                # 帳單列表只含月份／總額；逐月點橘色「明細」才有真實入帳日。
                bill_details = []
                for bill_index, bill in enumerate(out["card_bills"]):
                    if bill_index:
                        self._navigate_credit_card_bill(page, debug_dir)
                        page.wait_for_timeout(5000)
                    click_result = None
                    for frame in page.frames:
                        try:
                            click_result = frame.evaluate(r"""(index) => {
                                const body = document.body?.textContent || '';
                                if (!body.includes('帳單月份') || !body.includes('應繳總金額')) return null;
                                const visible = (el) => {
                                    const r = el.getBoundingClientRect();
                                    const s = getComputedStyle(el);
                                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                                };
                                const buttons = [...document.querySelectorAll('a,button,input[type="button"],input[type="submit"]')]
                                    .filter((el) => ((el.textContent || el.value || '').replace(/\s+/g, '') === '明細') && visible(el));
                                const target = buttons[index];
                                if (!target) return {clicked: false, count: buttons.length};
                                target.click();
                                return {clicked: true, count: buttons.length, tag: target.tagName, id: target.id || '', name: target.name || ''};
                            }""", bill_index)
                            if click_result and click_result.get("clicked"):
                                break
                        except Exception:
                            pass
                    if not click_result or not click_result.get("clicked"):
                        bill_details.append({"bill_month": bill.get("bill_month"), "click": click_result})
                        continue
                    page.wait_for_timeout(7000)
                    detail = None
                    pages = list(page.context.pages)
                    for detail_page in reversed(pages):
                        for frame in detail_page.frames:
                            try:
                                text = frame.evaluate("() => document.body.textContent.slice(0, 50000)")
                                has_detail_headers = all(marker in text for marker in (
                                    "交易日期", "入帳日期", "本期消費明細：", "本期合計：",
                                ))
                                if has_detail_headers:
                                    detail = {
                                        "bill_month": bill.get("bill_month"),
                                        "url": frame.url[:200],
                                        "text_preview": text,
                                        "click": click_result,
                                        "popup": detail_page != page,
                                    }
                                    break
                            except Exception:
                                pass
                        if detail:
                            break
                    for popup in pages:
                        if popup != page:
                            with contextlib.suppress(Exception):
                                popup.close()
                    bill_details.append(detail or {
                        "bill_month": bill.get("bill_month"),
                        "click": click_result,
                        "error": "detail page not found",
                        "page_count": len(pages),
                    })
                out["card_bill_details"] = bill_details
                _log(f"[esun][collect] card_bill_details={len(bill_details)}")
            _log(f"[esun][collect] card_frames={len(card_frames)} (widget mode)")
        except Exception as e:
            _log(f"[esun][collect] 信用卡 navigate 失敗: {e}")
            out["card_frames"] = []
            out["card_nav_probe"] = {"error": str(e)}

        # 第 3 階段：navigate 信用卡消費明細查詢（設計規範：每家銀行都要抓信用卡明細）
        try:
            txn_nav = self._navigate_menu(page, "信用卡消費明細查詢", debug_dir, "card_txn_form.png")
            out["card_txn_nav_probe"] = txn_nav
            txn_clicked = any(
                fr.get("result", {}).get("clicked") for fr in txn_nav.get("frames", [])
            )
            if not txn_clicked:
                out["card_txn_frames"] = []
                _log("[esun][collect] 信用卡消費明細查詢 menu 未點到，跳過")
            else:
                # 2026-06-13 升級：迭代 query 三個期間累積 transactions
                # 「最近一個月」抓最新，「最近二個月」抓更早，「最近一星期」備用驗證
                # Esun widget mode 每次 query 後 frame 內容會 replace，不能並行
                all_txns: list[dict] = []
                seen_keys: set = set()  # dedup: (date, merchant, billed_amount)
                periods_results = []
                all_periods_ok = True
                for period_index, period_label in enumerate(("最近一個月", "最近二個月")):
                    if period_index:
                        # 查詢結果會取代 FCM01004 表單；下一期間必須從 menu 重載 widget。
                        period_nav = self._navigate_menu(
                            page, "信用卡消費明細查詢", debug_dir,
                            f"card_txn_form_{period_index}.png",
                        )
                        if not any(
                            fr.get("result", {}).get("clicked")
                            for fr in period_nav.get("frames", [])
                        ):
                            all_periods_ok = False
                            periods_results.append({
                                "period": period_label,
                                "navigation": period_nav,
                                "submitted": {"strategy": None},
                                "result_seen": False,
                            })
                            _log(f"[esun][collect] {period_label} 查詢表單未重新載入，跳過")
                            continue
                    page.wait_for_timeout(5000)
                    form_submitted = self._submit_card_txn_query(page, debug_dir, period_label)
                    period_result = {"period": period_label, "submitted": form_submitted,
                                     "result_seen": False}
                    periods_results.append(period_result)
                    if not form_submitted.get("strategy"):
                        all_periods_ok = False
                        _log(f"[esun][collect] {period_label} 表單未提交，跳過")
                        continue
                    page.wait_for_timeout(8000)  # 等查詢結果載入

                    # 從 frame 抽 transactions
                    for f in page.frames:
                        if f == page.main_frame:
                            continue
                        try:
                            txt = f.evaluate("() => document.body.textContent.slice(0, 40000)")
                            if any(k in txt for k in ("消費日", "請款日", "入帳日", "消費金額", "授權碼", "特店名稱", "消費明細")):
                                period_result["result_seen"] = True
                                txns_this_round = self._parse_card_transactions(txt)
                                for t in txns_this_round:
                                    key = (t.get("consume_date"), t.get("merchant"), t.get("billed_amount"))
                                    if key in seen_keys:
                                        continue
                                    seen_keys.add(key)
                                    all_txns.append(t)
                                break
                        except Exception:
                            pass
                    if not period_result["result_seen"]:
                        all_periods_ok = False
                    _log(f"[esun][collect] {period_label} 累計 transactions={len(all_txns)}")

                out["card_txn_form_submitted"] = periods_results  # 多 period 結果
                out["card_transactions_ok"] = all_periods_ok
                out["card_transactions"] = all_txns
                _log(f"[esun][collect] card_transactions 總計 (dedup) count={len(all_txns)}")

                # card_txn_frames 仍用最後一輪 frame snapshot 給 daily_metric
                card_txn_frames = []
                for f in page.frames:
                    if f == page.main_frame:
                        continue
                    try:
                        url = f.url[:200]
                        txt = f.evaluate("() => document.body.textContent.slice(0, 40000)")
                        if any(k in txt for k in ("消費日", "請款日", "入帳日", "消費金額", "授權碼", "特店名稱", "消費明細")):
                            card_txn_frames.append({
                                "url": url,
                                "text_preview": txt,
                                "page_url": page.url[:120],
                            })
                    except Exception:
                        pass
                out["card_txn_frames"] = card_txn_frames
                _log(f"[esun][collect] card_txn_frames={len(card_txn_frames)} (widget mode, 累計)")
        except Exception as e:
            _log(f"[esun][collect] 信用卡消費明細 navigate 失敗: {e}")
            out["card_txn_frames"] = []
            out["card_txn_nav_probe"] = {"error": str(e)}

        # 第 4 階段：navigate 信用卡額度查詢（信用卡 > 信用卡帳單/明細 > 信用卡額度查詢）
        # 玉山的「已使用額度 / 可用餘額」官方數字在此頁，不是帳單頁。
        # 2026-06-18 新增：原本 used_credit 從 card_transactions sum 已入帳，會少算未入帳 +
        # 上期未繳，導致顯示 NT$2,085 而使用者實際 used=-807 (溢繳)。改為直接抓原生欄位 (B 路線)。
        # widget onclick: _leftMenuLoadWidget(event,'FCM01006','FCM','MFCM0204')
        # 重要 pitfall (2026-06-18 第三輪):
        # 玉山這個 widget **塞進 main DOM，不是 iframe**！原本 collect 程式 `for f in
        # page.frames if f != main_frame` 會跳過 main，永遠抓不到 widget 內容。
        # 必須 include main_frame, 且 widget 是替換 main DOM (frame URL 不變)，要靠
        # textContent 內含「已用額度」「可用餘額」label 來辨識。
        try:
            quota_nav = self._navigate_menu(page, "信用卡額度查詢", debug_dir, "card_quota_form.png")
            out["card_quota_nav_probe"] = quota_nav
            quota_clicked = any(
                fr.get("result", {}).get("clicked") for fr in quota_nav.get("frames", [])
            )
            if not quota_clicked:
                out["card_quota_frames"] = []
                out["card_quota"] = {}
                _log("[esun][collect] 信用卡額度查詢 menu 未點到，跳過")
            else:
                # 玉山 widget 載入很慢，前次 8 秒不夠 — 拉到 15 秒
                page.wait_for_timeout(15000)
                quota_frames = []
                # 收集所有 candidate frame **含 main_frame**，挑「最像額度資料頁」的
                # 條件：textContent 內**同時**含「已用額度」**和**「可用餘額」+ 數字
                # 排掉 home 頁的 menu noscript（只有 menu 字串，沒實際表格）
                import re as _re_quota
                for f in page.frames:  # ← include main_frame (widget 在 main DOM)
                    try:
                        url = f.url[:200]
                        txt = f.evaluate("() => document.body.textContent.slice(0, 40000)")
                        # strict: 必須**同時**有「已用額度」+「可用餘額」label
                        # menu 列表不可能同時有這兩個 label，自動排除
                        has_both_labels = "已用額度" in txt and "可用餘額" in txt
                        # 必須有逗號數字 (e.g. 400,807) 或負號開頭數字 (-807)
                        has_number = bool(_re_quota.search(r"-?\d{1,3}(?:,\d{3})+|\b-\d+\b", txt))
                        if has_both_labels and has_number:
                            quota_frames.append({
                                "url": url,
                                "text_preview": txt,
                                "page_url": page.url[:120],
                                "txt_len": len(txt),
                                "is_main": f == page.main_frame,
                            })
                    except Exception:
                        pass
                out["card_quota_frames"] = quota_frames
                if quota_frames:
                    # 多個 candidate 取 textContent 最長那個（widget 主內容通常最豐富）
                    best = max(quota_frames, key=lambda x: x.get("txt_len", 0))
                    full_text = best.get("text_preview", "")
                    out["card_quota"] = self._parse_card_quota(full_text)
                    _log(f"[esun][collect] card_quota={out['card_quota']}")
                else:
                    out["card_quota"] = {}
                    _log("[esun][collect] 信用卡額度查詢 frame 沒抓到 (已用額度+可用餘額+number)")
                _log(f"[esun][collect] card_quota_frames={len(quota_frames)} (widget mode)")
        except Exception as e:
            _log(f"[esun][collect] 信用卡額度查詢 navigate 失敗: {e}")
            out["card_quota_frames"] = []
            out["card_quota"] = {}
            out["card_quota_nav_probe"] = {"error": str(e)}

        # 第 5 階段：navigate 信用卡繳款明細查詢 (2026-06-22 使用者指出 frames menu 有此 item)
        # menu path: 信用卡 > 信用卡帳單/明細 > 信用卡繳款明細查詢
        # 預期內含「繳款日 + 繳款金額」歷史 record list (跟 ubot F0801001 同性質).
        # 跟 quota / txn step 同 pattern: navigate → 等 widget load → dump frames text.
        # 用 raw dump 留底, 不在此 hard-code parser; persist 端用 defensive regex.
        try:
            pay_nav = self._navigate_menu(
                page, "信用卡繳款明細查詢", debug_dir, "card_pay_form.png",
            )
            out["card_pay_nav_probe"] = pay_nav
            pay_clicked = any(
                fr.get("result", {}).get("clicked") for fr in pay_nav.get("frames", [])
            )
            if not pay_clicked:
                out["card_pay_frames"] = []
                out["card_pay_history"] = {}
                _log("[esun][collect] 信用卡繳款明細查詢 menu 未點到，跳過")
            else:
                # 玉山 widget pattern (跟 quota 同), wait 15s
                page.wait_for_timeout(15000)
                pay_frames = []
                # 收 main_frame + 所有 iframe textContent, 留 raw 給明早 PG probe
                for f in page.frames:
                    try:
                        url = f.url[:200]
                        txt = f.evaluate("() => document.body.textContent.slice(0, 40000)")
                        if not txt or len(txt.strip()) < 30:
                            continue
                        # 留 raw frame 進 dump, 明早從 daily_metric 撈出來看真實 shape
                        pay_frames.append({
                            "url": url,
                            "text_preview": txt[:30000],  # 30k 足容 menu(2.7k) + 表格(實測表格在 5000+ 後)
                            "page_url": page.url[:120],
                            "txt_len": len(txt),
                            "is_main": f == page.main_frame,
                        })
                    except Exception:
                        pass
                out["card_pay_frames"] = pay_frames
                # 2026-06-23 v2 (local crawl 確認 shape): 從 frames text 解析繳款表格.
                # 真實 shape (text block):
                #   繳款日期\n繳款方式\n繳款行庫\n幣別\n應繳款金額\n繳款金額\n
                #   2026/03/30\n玉山自動扣繳　\n玉山自動轉帳\n臺幣 TWD\n65,714\n65,714\n
                #   2026/03/06\n玉山自動扣繳　\n玉山自動轉帳\n臺幣 TWD\n12,792\n12,792\n
                # records 排序新→舊 (page 預設), records[0] 是最新一筆.
                full_text = "\n".join(
                    f.get("text_preview", "") for f in pay_frames
                )
                out["card_pay_history"] = self._parse_card_pay_history(full_text)
                _log(f"[esun][collect] card_pay_frames={len(pay_frames)} "
                     f"records={len(out['card_pay_history'].get('records', []))}")
        except Exception as e:
            _log(f"[esun][collect] 信用卡繳款明細查詢 navigate 失敗: {e}")
            out["card_pay_frames"] = []
            out["card_pay_history"] = {}
            out["card_pay_nav_probe"] = {"error": str(e)}

        out["_all_endpoints"] = sorted({h.endpoint for h in collector.hits if h.resp_json})
        out["_endpoint_count"] = len(out["_all_endpoints"])
        out["card_statement_transactions"] = self._parse_card_bill_details(
            out.get("card_bill_details") or [],
        )
        out["card_transactions"] = self._merge_card_transactions(
            out.get("card_transactions") or [],
            out["card_statement_transactions"],
        )
        publish_card_bill_facts(out, [_esun_card_bill_fact(out)])
        _log(f"[esun][collect] 攔到 {out['_endpoint_count']} 個 API endpoint")
        return BankCollectResult(**out)

    @staticmethod
    def _parse_card_pay_history(text: str) -> dict:
        """從「信用卡繳款明細查詢」頁 textContent 抽繳款 records.

        2026-06-23 (使用者 local crawl 驗證): 玉山 widget 表格 text 結構:
          繳款日期\\n繳款方式\\n繳款行庫\\n幣別\\n應繳款金額\\n繳款金額\\n
          2026/03/30\\n玉山自動扣繳　\\n玉山自動轉帳\\n臺幣 TWD\\n65,714\\n65,714
          2026/03/06\\n玉山自動扣繳　\\n玉山自動轉帳\\n臺幣 TWD\\n12,792\\n12,792

        records 排序新→舊 (page 預設), records[0] 是最新一筆.
        本查詢僅提供最近半年資料 (玉山官方提醒).

        回傳 {"records": [{post_date, method, bank, currency, due_amount, paid_amount}]}
        """
        import re as _re
        records = []
        # 抓「YYYY/MM/DD\n方式\n行庫\n幣別 CCY\n應繳\n已繳」pattern
        # 方式 / 行庫 可能含全形空白 \u3000, 用 \S+ 抓詞 (不含換行)
        for m in _re.finditer(
            r"(?P<date>\d{4}/\d{2}/\d{2})\s*\n"
            r"\s*(?P<method>\S+)\s*\n"
            r"\s*(?P<bank>\S+)\s*\n"
            r"\s*\S+\s+(?P<ccy>[A-Z]{3})\s*\n"
            r"\s*(?P<due>[\d,]+)\s*\n"
            r"\s*(?P<paid>[\d,]+)",
            text,
        ):
            try:
                due_amt = int(m.group("due").replace(",", "") or 0)
                paid_amt = int(m.group("paid").replace(",", "") or 0)
            except ValueError:
                continue
            # YYYY/MM/DD → ISO (slash → dash)
            date_iso = m.group("date").replace("/", "-")
            records.append({
                "post_date": date_iso,
                "method": m.group("method").strip(),
                "bank": m.group("bank").strip(),
                "currency": m.group("ccy"),
                "due_amount": due_amt,
                "paid_amount": paid_amt,
            })
        return {"records": records}

    @staticmethod
    def _parse_card_quota(text: str) -> dict:
        """從「信用卡額度查詢」頁 textContent 抽額度欄位。

        玉山「信用卡額度查詢」實際表格結構 (2026-06-18 vision 確認):

          信用卡額度查詢
          查詢時間：2026/06/18 18:08:16
          ┌──────────┬──────────┬──────────┐
          │ 信用狀態  │ 已用額度  │ 可用餘額  │
          ├──────────┼──────────┼──────────┤
          │ 歸戶      │   -807    │ 400,807  │   ← 整戶層 (可能是負數=溢繳)
          ├──────────┼──────────┼──────────┤
          │ 指定額度  │ 已用額度  │ (空白)   │   ← per-card 表頭, 下方是各卡 row
          └──────────┴──────────┴──────────┘

        textContent flattened 後:
          信用卡額度查詢
          查詢時間：2026/06/18 18:08:16
          信用狀態
          已用額度
          可用餘額
          歸戶
          -807
          400,807
          指定額度
          已用額度
          ...

        策略：
          1) 找「歸戶」row 後緊接的兩個數字 = (used_credit, available_credit)
          2) used + available = credit_limit (歸戶總額度 = 已用 + 可用)
             玉山這頁不直接顯示「歸戶信用額度」，要靠 used+available 算出來

        Returns:
          {
            "used_credit_twd": -807,             # 可能為負 (溢繳)
            "available_credit_twd": 400807,
            "credit_limit_twd": 400000,          # = used + available (玉山這頁不顯示)
            "raw_text_sample": "...",
          }
        """
        import re as _re
        out: dict = {}

        # 找「歸戶」後緊接的兩個數字
        # textContent 範例: "...信用狀態\n已用額度\n可用餘額\n歸戶\n-807\n400,807\n指定額度..."
        # pattern: 「歸戶」 + (數字1) + (數字2), 中間允許空白/換行
        # 數字格式: 可選負號 + 1-3 digit + (,3digit)* 或裸 -digit
        num_pat = r"(-?\d{1,3}(?:,\d{3})*)"
        m = _re.search(
            rf"歸戶\s*\n?\s*{num_pat}\s*\n?\s*{num_pat}",
            text,
        )
        if m:
            try:
                used = int(m.group(1).replace(",", ""))
                available = int(m.group(2).replace(",", ""))
                out["used_credit_twd"] = used
                out["available_credit_twd"] = available
                out["credit_limit_twd"] = used + available
            except ValueError:
                pass

        # debug：永遠留 sample，命中失敗時使用者才能 audit
        out["raw_text_sample"] = text[:500]
        return out

    # ---------- 解析帳戶 ----------
    @staticmethod
    def _parse_account_overview(frames_data: list[dict]) -> list[dict]:
        """從 frame text 抽臺幣/外幣帳戶總覽。

        玉山頁面結構（tab-separated）：
          臺幣帳戶總覽
          帳號類別  帳號  帳戶餘額  功能
          臺幣綜存  0900000097060  臺幣綜存  1  ===請選擇===
          總計  1

          外幣帳戶總覽
          帳號類別  帳號  帳戶餘額  功能
          外幣活存  0900000107061  外幣活存  USD 0.00  ===請選擇===
        """
        import re as _re
        accounts: list[dict] = []
        for fd in frames_data:
            text = fd.get("text_preview") or ""
            # 抓所有 13 碼帳號 + 上下文
            for m in _re.finditer(r"(?P<cat>臺幣綜存|臺幣活存|外幣活存|外幣綜存|外幣定存|臺幣定存|定期儲蓄存款|薪資戶)\s*\n\s*(?P<acct>\d{13})\s*\n", text):
                acct_no = m.group("acct")
                category = m.group("cat")
                # 從 acct 後面再 grep 餘額（接著找到下個換行 + 數字 或 USD/TWD pattern）
                after = text[m.end():m.end() + 200]
                # 餘額 pattern：純數字、或「USD/TWD 0.00」
                bal_match = _re.search(r"(?:USD|TWD|JPY|EUR|GBP|CNY|HKD|AUD|CAD|CHF|NZD|SGD|THB|ZAR|SEK)?\s*([\d,]+(?:\.\d+)?)", after)
                bal_str = bal_match.group(1).replace(",", "") if bal_match else "0"
                # 幣別判定：臺幣帳戶強制 TWD（不被後文 USD 字串汙染），外幣帳戶才 grep
                if "臺幣" in category:
                    currency = "TWD"
                else:
                    currency = "USD"
                    ccy_match = _re.search(r"\b(USD|JPY|EUR|GBP|CNY|HKD|AUD|CAD|CHF|NZD|SGD|THB|ZAR|SEK)\b", after)
                    if ccy_match:
                        currency = ccy_match.group(1)
                try:
                    balance = float(bal_str)
                except ValueError:
                    balance = 0.0
                # 去重（同帳號可能在「臺幣帳戶總覽 / 我的最愛」雙處出現）
                if any(a["account_no"] == acct_no for a in accounts):
                    continue
                accounts.append({
                    "account_no": acct_no,
                    "category": category,
                    "currency": currency,
                    "balance": balance,
                    "source_frame": fd.get("url", "")[:80],
                })
        return accounts

    # ---------- 解析信用卡帳單 ----------
    @staticmethod
    def _parse_card_summary(text: str) -> dict:
        """從信用卡帳單頁 textContent 抽 summary 欄位。

        玉山頁面 pattern：
          歸戶信用額度-臺幣\n 400,000\n
          目前e point\n 1,042\n
          預借現金額度-臺幣\n 40,000\n
          本期繳款截止日\n115/05/28\n
          ATM繳款編號\n9977701加身分證編號後９位數字\n
        """
        import re as _re
        out: dict = {}
        patterns = {
            "credit_limit_twd": r"歸戶信用額度-?臺幣\s*([\d,]+)",
            "epoint": r"目前\s*e\s*point\s*([\d,]+)",
            "cash_advance_limit_twd": r"預借現金額度-?臺幣\s*([\d,]+)",
            "payment_due_date_roc": r"本期繳款截止日\s*(\d{3}/\d{2}/\d{2})",
            "atm_payment_prefix": r"ATM\s*繳款編號\s*(\d+)",
        }
        for key, pat in patterns.items():
            m = _re.search(pat, text)
            if m:
                val = m.group(1).replace(",", "")
                try:
                    out[key] = int(val) if val.isdigit() else val
                except ValueError:
                    out[key] = val
        return out

    @staticmethod
    def _parse_card_bills(text: str) -> list[dict]:
        """從信用卡帳單頁 textContent 抽帳單列表。

        玉山 row pattern (tab-separated)：
          0115/04\n臺幣 TWD\n0\n0\n 明細
          0115/03\n臺幣 TWD\n0\n0\n 明細
          0115/02\n臺幣 TWD\n65,714\n65,714\n 明細

        帳單月份格式 = 0YYY/MM（民國年）
        """
        import re as _re
        bills = []
        # 抓「0XXX/XX\n幣別 CCY\n金額\n金額」pattern
        for m in _re.finditer(
            r"(?P<bill_month>0\d{3}/\d{2})\s*\n\s*(?P<ccy_name>\S+)\s+(?P<ccy>[A-Z]{3})\s*\n\s*(?P<due>[\d,]+)\s*\n\s*(?P<paid>[\d,]+)",
            text,
        ):
            roc_month = m.group("bill_month")
            # 0115/04 → 民國 115 年 04 月 → 西元 2026 年 04 月
            # （民國年 = 西元年 - 1911；115 → 2026）
            try:
                roc_y = int(roc_month[:4])
                month_num = int(roc_month[5:7])
                bill_month = f"{roc_y + 1911}-{month_num:02d}"
            except ValueError:
                bill_month = roc_month
            bills.append({
                "bill_month_roc": roc_month,
                "bill_month": bill_month,
                "currency": m.group("ccy"),
                "due_amount": int(m.group("due").replace(",", "") or 0),
                "paid_amount": int(m.group("paid").replace(",", "") or 0),
            })
        return bills

    @staticmethod
    def _parse_card_bill_details(details: list[dict]) -> list[dict]:
        """逐月帳單 popup：交易日、入帳日、交易項目、原幣／繳款幣別金額。"""
        import re as _re

        date_re = _re.compile(r"^(\d{1,2})/(\d{1,2})$")
        amount_re = _re.compile(r"^([A-Z]{3})\s*(-?[\d,]+(?:\.\d+)?)$")
        rows = []
        for detail in details:
            bill_month = str(detail.get("bill_month") or "")
            if not _re.fullmatch(r"\d{4}-\d{2}", bill_month):
                continue
            bill_year, bill_month_number = map(int, bill_month.split("-"))
            text = str(detail.get("text_preview") or "")
            section = text.split("本期消費明細：", 1)[-1].split("本期合計：", 1)[0]
            tokens = [line.strip() for line in section.splitlines() if line.strip()]
            index = 0
            while index + 1 < len(tokens):
                consume_match = date_re.fullmatch(tokens[index])
                post_match = date_re.fullmatch(tokens[index + 1])
                if not consume_match or not post_match:
                    index += 1
                    continue
                end = index + 2
                while end < len(tokens):
                    if end + 1 < len(tokens) and date_re.fullmatch(tokens[end]) and date_re.fullmatch(tokens[end + 1]):
                        break
                    end += 1
                body = tokens[index + 2:end]
                amounts = []
                description = None
                for token in body:
                    amount_match = amount_re.fullmatch(token.replace(" ", ""))
                    if amount_match:
                        amounts.append((
                            amount_match.group(1),
                            float(amount_match.group(2).replace(",", "")),
                        ))
                    elif description is None:
                        description = token
                if description and amounts:
                    def iso(match):
                        month, day = map(int, match.groups())
                        year = bill_year - 1 if month > bill_month_number + 6 else bill_year
                        return f"{year:04d}/{month:02d}/{day:02d}"

                    billed_currency, billed_amount = amounts[-1]
                    consume_currency = consume_amount = None
                    if len(amounts) > 1:
                        consume_currency, consume_amount = amounts[0]
                    rows.append({
                        "card_no": "",
                        "consume_date": iso(consume_match),
                        "post_date": iso(post_match),
                        "merchant": description,
                        "consume_currency": consume_currency,
                        "consume_amount": consume_amount,
                        "billed_currency": billed_currency,
                        "billed_amount": billed_amount,
                        "status": "已入帳",
                        "bill_month": bill_month,
                    })
                index = max(end, index + 1)
        return rows

    @staticmethod
    def _merge_card_transactions(current: list[dict], statement: list[dict]) -> list[dict]:
        """同日期／商戶／金額時，以有真實入帳日的帳單 row 取代消費查詢 row。"""
        from collections import Counter

        def key(row):
            try:
                amount = float(row.get("billed_amount"))
            except (TypeError, ValueError):
                amount = row.get("billed_amount")
            return (row.get("consume_date"), row.get("merchant"), amount)

        current_by_key = {}
        for index, row in enumerate(current):
            current_by_key.setdefault(key(row), []).append((index, row))
        statement_counts = Counter(key(row) for row in statement)
        matched_current = set()
        enriched_statement = []
        for row in statement:
            enriched = dict(row)
            matches = current_by_key.get(key(row), [])
            if len(matches) == 1 and statement_counts[key(row)] == 1:
                current_index, current_row = matches.pop(0)
                matched_current.add(current_index)
                for field in ("card_no", "card_last4"):
                    if not enriched.get(field) and current_row.get(field):
                        enriched[field] = current_row[field]
            elif matches:
                # 帳單 popup 沒有卡號；同日同店同額多筆時無穩定 join key，寧可保留
                # 原 consumption rows（post_date=NULL），不可按 DOM 順序猜卡。
                continue
            enriched_statement.append(enriched)
        return [
            row for index, row in enumerate(current) if index not in matched_current
        ] + enriched_statement

    @staticmethod
    def _parse_card_transactions(text: str) -> list[dict]:
        """從信用卡消費明細頁 textContent 抽交易列表。

        玉山消費明細 6 欄結構（textContent 用 \\n 分隔）：
          消費日期\\n商店\\n消費幣別 金額\\n繳款幣別 金額\\n卡號(5242-XXXX-XXXX-XXXX)\\n狀態(未入帳/已入帳)

        e.g.
          2026/06/08\\n街口電支－【中油條碼】台灣中油\\nTWD 1,727\\nTWD 1,727\\n9064-XXXX-XXXX-7032\\n未入帳
        """
        import re as _re
        txns = []
        pattern = _re.compile(
            r"(?P<date>20\d{2}/\d{2}/\d{2})\s*\n"
            r"\s*(?P<merchant>[^\n]{2,80})\s*\n"
            r"\s*(?P<ccy1>[A-Z]{3})\s+(?P<amt1>[\d,]+(?:\.\d+)?)\s*\n"
            r"\s*(?P<ccy2>[A-Z]{3})\s+(?P<amt2>[\d,]+(?:\.\d+)?)\s*\n"
            r"\s*(?P<card>[\d\-X]{16,25})\s*\n"
            r"\s*(?P<status>未入帳|已入帳|入帳中)",
        )
        for m in pattern.finditer(text):
            card_raw = m.group("card")
            last4 = card_raw[-4:] if card_raw[-4:].isdigit() else None
            try:
                consume_amt = float(m.group("amt1").replace(",", ""))
                billed_amt = float(m.group("amt2").replace(",", ""))
            except ValueError:
                continue
            txns.append({
                "consume_date": m.group("date"),
                "merchant": m.group("merchant").strip(),
                "consume_currency": m.group("ccy1"),
                "consume_amount": consume_amt,
                "billed_currency": m.group("ccy2"),
                "billed_amount": billed_amt,
                "card_no": card_raw,
                "card_last4": last4,
                "status": m.group("status"),
            })
        return txns

    # ---------- 通用 menu navigate ----------
    def _navigate_menu(self, page, label: str, debug_dir, screenshot_name: str | None = None) -> dict:
        """通用主選單 navigation：找文字為 label 的可點 element 並 click。

        設計需求「禁用我的最愛」+「li 純 wrapper 陷阱」+「AJAX widget swap」→
        所有銀行的 menu navigation 都該走這條 helper。

        策略：
          1. 蒐集所有 textContent === label 的候選，排除 ancestor 含 favorite/shortcut/bookmark/sidebar
          2. 找有 onclick/href 屬性的 actionable 元素 → click
          3. fallback: 任一可見非 fav 元素 → click（謹慎用，可能是 <li> 純 wrapper）
        """
        probe_info = {"label": label, "frames": []}
        for f in page.frames:
            try:
                result = f.evaluate(
                    """
                    (label) => {
                      const isFavoriteAncestor = (el) => {
                        let cur = el;
                        for (let i = 0; i < 12 && cur && cur !== document.body; i++) {
                          const id = (cur.id || '').toLowerCase();
                          const cls = (cur.className || '').toString().toLowerCase();
                          if (/favorite|favor|shortcut|bookmark|sidebar|portlet|widget|mark|aside/.test(id+' '+cls)) {
                            return true;
                          }
                          cur = cur.parentElement;
                        }
                        return false;
                      };
                      const candidates = [];
                      for (const el of document.querySelectorAll('a,button,li,span,div')) {
                        if ((el.textContent || '').trim() !== label) continue;
                        const r = el.getBoundingClientRect();
                        candidates.push({
                          el, rect: r,
                          visible: r.width > 0 && r.height > 0,
                          isFav: isFavoriteAncestor(el),
                          tag: el.tagName, id: el.id || null,
                          cls: (el.className || '').toString().slice(0, 60),
                          href: el.getAttribute('href'),
                          onclick: el.getAttribute('onclick'),
                        });
                      }
                      const dump = candidates.map(c => ({
                        tag: c.tag, id: c.id, cls: c.cls,
                        href: c.href, onclick: c.onclick,
                        visible: c.visible, isFav: c.isFav,
                        x: Math.round(c.rect.x), y: Math.round(c.rect.y),
                      }));
                      // 策略 1: 有 onclick/href 的可點元素
                      const actionable = candidates.filter(c => c.visible && !c.isFav && (c.href || c.onclick));
                      if (actionable.length > 0) {
                        actionable[0].el.click();
                        return {clicked: 'actionable', dump, target: actionable[0].tag, onclick: actionable[0].onclick};
                      }
                      // 策略 1b: fallback
                      const ready = candidates.filter(c => c.visible && !c.isFav);
                      if (ready.length > 0) {
                        ready[0].el.click();
                        return {clicked: 'fallback', dump, target: ready[0].tag};
                      }
                      return {clicked: null, dump, fail: 'no_actionable_candidate'};
                    }
                    """,
                    label,
                )
                probe_info["frames"].append({"url": f.url[:120], "result": result})
                _log(f"[esun][collect][nav][{label}] {f.url[:50]} clicked={result and result.get('clicked')!r}")
                if result and result.get("clicked"):
                    return probe_info
            except Exception as e:
                _log(f"[esun][collect][nav][{label}] frame {f.url[:60]} 失敗: {e}")
                probe_info["frames"].append({"url": f.url[:120], "error": str(e)})
                continue
        return probe_info

    # ---------- 信用卡 navigate（用通用 helper）----------
    def _navigate_credit_card_bill(self, page, debug_dir) -> dict:
        """navigate 信用卡帳單資訊（背後用 _navigate_menu）。"""
        return self._navigate_menu(page, "信用卡帳單資訊", debug_dir, "card.png")

    def _submit_card_txn_query(self, page, debug_dir, period_label: str = "最近一個月") -> dict:
        """玉山「信用卡消費明細查詢」表單 widget 自動點期間 + 查詢。

        玉山 form id = `fcm01004`，查詢期間 radio 通常用 `:intervalrdo1/2/3/4` 序列：
          1=最近一星期、2=最近一個月、3=最近二個月、4=其它期間
        改用 textContent 找 radio label + 「查詢」按鈕（不依賴脆弱 id）。

        2026-06-13 升級：period_label 參數化，支援 "最近一星期" / "最近一個月" / "最近二個月"
        （"其它期間" 因要填日期欄較複雜，暫不支援）
        """
        info = {"strategy": None, "errors": [], "period_label": period_label}
        for f in page.frames:
            try:
                result = f.evaluate("""
                    (wanted) => {
                      const log = [];
                      // 結果頁證明「點到同文字 span/div」不代表 radio 真正切換；
                      // 直接鎖定 JSF intervalrdo1/2/3，並以 checked 作成功條件。
                      const periodIndex = {
                        '最近一星期': '1',
                        '最近一個月': '2',
                        '最近二個月': '3',
                      }[wanted];
                      const radio = periodIndex ? document.querySelector(
                        `input[type="radio"][id$="intervalrdo${periodIndex}"], ` +
                        `input[type="radio"][id*="intervalrdo${periodIndex}"], ` +
                        `input[type="radio"][value="${periodIndex}"]`
                      ) : null;
                      let monthClicked = false;
                      if (radio) {
                        const label = [...document.querySelectorAll('label')].find(
                          (lbl) => lbl.htmlFor === radio.id || lbl.contains(radio)
                            || (lbl.textContent || '').trim() === wanted
                        );
                        if (label) label.click();
                        if (!radio.checked) radio.click();
                        if (!radio.checked) {
                          radio.checked = true;
                          radio.dispatchEvent(new Event('input', {bubbles: true}));
                          radio.dispatchEvent(new Event('change', {bubbles: true}));
                        }
                        monthClicked = radio.checked;
                        if (monthClicked) log.push('selected radio ' + wanted);
                      }
                      const periodSelected = Boolean(radio && radio.checked);

                      // 只有期間 radio 已確認 selected 才能送查詢，否則會默默查預設一星期。
                      let queryClicked = false;
                      if (periodSelected) {
                        for (const btn of document.querySelectorAll('button,a,input[type=button],input[type=submit]')) {
                          const t = (btn.textContent || btn.value || '').trim();
                          if (t === '查詢' || t === '查 詢') {
                            const r = btn.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) {
                              btn.click();
                              queryClicked = true;
                              log.push('clicked 查詢');
                              break;
                            }
                          }
                        }
                      }
                      return {
                        monthClicked,
                        periodSelected,
                        queryClicked,
                        selectedRadioId: radio?.id || null,
                        log,
                        frame_url: location.href,
                      };
                    }
                """, period_label)
                if result and result.get("periodSelected") and result.get("queryClicked"):
                    info["strategy"] = result
                    _log(f"[esun][collect][txn-form] {f.url[:60]} {result.get('log')}")
                    return info
            except Exception as e:
                info["errors"].append({"url": f.url[:80], "error": str(e)})
                continue
        _log("[esun][collect][txn-form] 無 frame 能點到表單（probe 已存）")
        return info


if __name__ == "__main__":
    import json
    crawler = EsunCrawler()
    try:
        result = crawler.run(login_url=BASE, headless=True)
    except EsunLoginError as e:
        result = {"error": "login_failed_stop", "detail": str(e)}

    out_file = Path(__file__).resolve().parents[1] / "data" / "esun_collected.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"\n[esun][done] 已存: {out_file}")

    if result.get("error"):
        _log(f"  ❌ error: {result['error']}")
    else:
        data = result.get("data", {})
        _log(f"  url: {data.get('final_url')}")
        _log(f"  frames: {len(data.get('frames', []))}")
        _log(f"  endpoints: {data.get('_all_endpoints', [])}")
