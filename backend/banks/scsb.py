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
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qsl, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.base import BankCollectResult, BankCrawler, ResponseCollector
from backend.core.card_bills import publish_card_bill_facts
from backend.core.captcha import ocr_bytes
from backend.core.creds import ScsbCreds
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

# 強關 modal（SCSB #intro_alert.custom-modal.show 會擋）
JS_KILL_MODAL = r"""
(() => {
  for(let i=0;i<6;i++){
    let did = false;
    const b = [...document.querySelectorAll('button,a,div,span')].find(e=>e.offsetParent!==null
      && /^(I got it|OK|Confirm|確認|我知道了|關閉|Close|確定|Don't show again today|不再顯示|繼續|同意|稍後|下次|不再提醒|Skip)$/i
        .test((e.textContent||'').trim())
      && (e.textContent||'').trim().length < 25);
    if(b){ b.click(); did=true; }
    if(!did) break;
  }
  document.querySelectorAll('.modal-backdrop,.custom-modal.show,[class*=overlay]').forEach(e=>{
    try{ e.style.display='none'; e.style.opacity=0; e.classList.remove('show'); }catch(x){}
  });
  return 'done';
})()
"""

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
    CREDENTIAL_HOSTS = frozenset({"ibank.scsb.com.tw", "ebank.scsb.com.tw"})

    def __init__(self):
        super().__init__(name="scsb")
        self._cleanup_private_artifacts()
        history_mode = os.environ.get("BANK_CRAWLER_HISTORY_MODE", "full")
        if history_mode not in {"full", "incremental"}:
            raise ValueError(f"invalid BANK_CRAWLER_HISTORY_MODE: {history_mode!r}")
        self.full_history = history_mode == "full"
        self.creds = ScsbCreds.load()

    @staticmethod
    def _cleanup_private_artifacts() -> None:
        from backend.core.store import _data_root

        debug_dir = _data_root() / "scsb_collect"
        debug_dir.mkdir(parents=True, exist_ok=True)
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
            (debug_dir / legacy_name).unlink(missing_ok=True)

    def _host_filter(self) -> str:
        return "scsb.com"

    def _logged_in(self, page) -> bool:
        """Pure one-shot positive check; the shared lifecycle owns waiting."""
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
            controls = page.locator(
                "#userId, #idNumber, #pppd, #verified, .ved_img"
            )
            if any(
                controls.nth(index).is_visible()
                for index in range(controls.count())
            ):
                return False
            body = page.locator("body").inner_text()
        except Exception:
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
        page.wait_for_timeout(9000)

    def is_authenticated(self, page) -> bool:
        return self._logged_in(page)

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
            bank="scsb",
            phases=(CheckpointPhase.PRE_SUBMIT,),
            kind=CheckpointKind.DISMISSIBLE_NOTICE,
            container_selector="#intro_alert.custom-modal.show",
            action_texts=("I got it",),
            max_actions=1,
        )
        fraud_notice = LoginCheckpointRule(
            name="scsb-fraud-notice",
            bank="scsb",
            phases=(CheckpointPhase.PRE_SUBMIT,),
            kind=CheckpointKind.DISMISSIBLE_NOTICE,
            container_selector=".custom-modal.show",
            action_selector="button.btn-gradient",
            action_texts=("我知道了",),
            required_body_pattern=re.compile(
                r"^[\s\S]{0,300}親愛的客戶[\s\S]{0,300}詐騙[\s\S]{0,300}$"
            ),
            max_actions=1,
            first_match_timeout_ms=5000,
        )
        return (
            *(
                LoginCheckpointRule(
                    name=f"scsb-otp-required-{suffix}",
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
                bank="scsb",
                phases=all_phases,
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector=".custom-modal.show",
            ),
            LoginCheckpointRule(
                name="scsb-unknown-modal",
                bank="scsb",
                phases=all_phases,
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector=".modal.show",
            ),
            LoginCheckpointRule(
                name="scsb-unknown-dialog",
                bank="scsb",
                phases=all_phases,
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector="[role='dialog']",
            ),
            LoginCheckpointRule(
                name="scsb-login-form-still-visible",
                bank="scsb",
                phases=post,
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector=SEL_CAP,
            ),
        )

    @staticmethod
    def _keyboard_fill(page, field, value: str) -> None:
        field.click()
        field.click(click_count=3)
        page.keyboard.press("Backspace")
        page.keyboard.type(value, delay=80)
        if len(field.input_value()) != len(value):
            raise ScsbLoginError("登入欄位輸入長度不符；未送出登入")

    @staticmethod
    def _click_unique_refresh(page) -> bool:
        refreshes = page.locator(".chg_link")
        if refreshes.count() != 1:
            return False
        refresh = refreshes.nth(0)
        if not refresh.is_visible() or not refresh.is_enabled():
            return False
        label = " ".join(refresh.inner_text().split())
        if label not in ("", "重新產生", "Refresh"):
            return False
        refresh.click()
        page.wait_for_timeout(1500)
        return True

    def _ocr_captcha(self, page, max_attempts=5):
        attempts = min(max(int(max_attempts), 0), 5)
        for attempt in range(attempts):
            try:
                images = page.locator(SEL_CAP_IMG)
                if images.count() != 1:
                    return None
                image = images.nth(0)
                if not image.is_visible():
                    return None
                background = image.evaluate("el => getComputedStyle(el).backgroundImage")
                match = (
                    _CAPTCHA_BACKGROUND.fullmatch(background)
                    if isinstance(background, str)
                    else None
                )
                if match is None:
                    raise ValueError
                raw = base64.b64decode(match.group("payload"), validate=True)
                text = ocr_bytes(
                    raw,
                    expected_len=5,
                    alnum_only=True,
                    min_confidence=0.98,
                )
                if isinstance(text, str) and len(text) == 5 and text.isdigit():
                    return text
            except Exception:
                pass
            if attempt < attempts - 1:
                try:
                    if not self._click_unique_refresh(page):
                        return None
                except Exception:
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
            candidates = page.locator(selector)
            if any(
                candidates.nth(index).is_visible()
                for index in range(candidates.count())
            ):
                return True
        return False

    def submit_credentials_once(self, page) -> None:
        try:
            page.wait_for_selector(SEL_SID, state="visible", timeout=30000)
            page.wait_for_selector(SEL_CAP_IMG, state="visible", timeout=15000)
            fields = []
            for selector in (SEL_SID, SEL_USER, SEL_PWD, SEL_CAP):
                candidates = page.locator(selector)
                if candidates.count() != 1:
                    raise ScsbLoginError("登入欄位無法安全確認；未送出登入")
                field = candidates.nth(0)
                if not field.is_visible() or not field.is_enabled():
                    raise ScsbLoginError("登入欄位無法安全確認；未送出登入")
                fields.append(field)

            for field, value in zip(
                fields[:3],
                (
                    self.creds.national_id,
                    self.creds.user_code,
                    self.creds.password,
                ),
                strict=True,
            ):
                self._keyboard_fill(page, field, value)
            captcha = self._ocr_captcha(page, max_attempts=5)
            if not captcha:
                raise ScsbLoginError("無法安全辨識驗證碼；未送出登入")
            self._keyboard_fill(page, fields[3], captcha)

            candidates = page.locator(
                "button, input[type='submit'], input[type='button']"
            )
            eligible = []
            for index in range(candidates.count()):
                candidate = candidates.nth(index)
                if not candidate.is_visible() or not candidate.is_enabled():
                    continue
                label = " ".join(
                    (
                        candidate.inner_text()
                        or candidate.get_attribute("value")
                        or ""
                    ).split()
                )
                if label in ("Log in", "登入"):
                    eligible.append(candidate)
            if len(eligible) != 1:
                raise ScsbLoginError(
                    "找不到唯一且可操作的登入按鈕；未送出登入"
                )
            button = eligible[0]
        except ScsbLoginError:
            raise
        except Exception:
            raise ScsbLoginError("登入欄位無法安全填寫；未送出登入") from None

        try:
            button.click(timeout=8000)
        except Exception:
            raise ScsbLoginError("登入送出狀態不明；禁止自動重試") from None

        try:
            for wait_ms in (12000, 5000, 5000, 5000):
                page.wait_for_timeout(wait_ms)
                if self._logged_in(page) or self._visible_blocker(page):
                    return
        except Exception:
            raise ScsbLoginError(
                "登入送出後狀態無法安全確認；禁止自動重試"
            ) from None

    # ---------- 抓取（DOM regex 路線，API 加密暫不解）----------
    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        """SCSB ResponseData 是 base64 對稱加密，第一版走 DOM regex 抽餘額/帳戶。

        SCSB SPA 不接受 page.goto 跳 hash route（會回 ERR_HTTP_RESPONSE_CODE_FAILURE），
        必須改 location.hash 設值或 click 左側選單。
        """
        out: dict = {}
        page.wait_for_timeout(5000)
        page.evaluate(JS_KILL_MODAL)
        page.wait_for_timeout(2000)

        # SCSB SPA 菜單是 Vue/React，DOM e.click() 不觸發 vnode handler，
        # 必須用 Playwright 原生 page.click（送真實 mouse event）。
        #
        # 2026-06-18 evidence-driven fix（job 87 cloud audit, 0.1.36 menu_dom_audit）：
        # 真實 menu 結構是 Bootstrap accordion (cls=accordion-button collapsed)，
        # 字眼用「我的總覽 / 投資 / 貸款 / 信用卡」(不是英文 EN 也不是「個人總覽」)。
        # accordion-button 必先點開展開 → 再點 inner leaf menu。
        # 修法 (a) 改 SCSB_MENU map = {label: [(accordion_label, leaf_label_zh)]}
        # (b) _navigate accordion-aware: 先 query button:has-text(accordion) →
        #     click 展開 → wait 1s → 再 query button/a 配 leaf。
        SCSB_MENU = {
            # label_log_name: (accordion_zh, leaf_zh, optional fallback EN)
            "My Overview": ("我的總覽", None, "My Overview"),  # accordion 本身 = leaf
            "TWD Deposit": ("我的總覽", "台幣存款", "TWD Deposit"),  # 在我的總覽 accordion 下
            "Credit Card": ("信用卡", None, "Credit Card Services"),  # accordion 本身 = leaf
        }

        def _navigate(label, wait_ms=8000):
            """SCSB accordion-aware navigation."""
            spec = SCSB_MENU.get(label)
            if not spec:
                _log(f"  [nav] ❌ unknown label: {label}")
                return False
            accordion_zh, leaf_zh, en_fallback = spec
            _log(f"[collect] click → {label} (accordion={accordion_zh!r} leaf={leaf_zh!r})")
            try:
                # Step 1: 點 accordion 展開（即使 leaf=None 也要點，那就是 navigation 本身）
                acc_btn = page.query_selector(f"button:has-text({accordion_zh!r})")
                if not acc_btn or not acc_btn.is_visible():
                    # EN fallback (給 leaf=None 的 case)
                    if en_fallback:
                        acc_btn = page.query_selector(f"button:has-text({en_fallback!r})")
                if not acc_btn or not acc_btn.is_visible():
                    _log(f"  [nav] ❌ accordion button {accordion_zh!r} 找不到")
                    return False
                acc_btn.click()
                page.wait_for_timeout(2000)
                # 如果 leaf=None, accordion click 本身就是 navigation
                if leaf_zh is None:
                    page.wait_for_timeout(wait_ms - 2000 if wait_ms > 2000 else 1000)
                    page.evaluate(JS_KILL_MODAL)
                    page.wait_for_timeout(1500)
                    return True
                # Step 2: 點 leaf
                leaf_btn = page.query_selector(f"button:has-text({leaf_zh!r}), a:has-text({leaf_zh!r}), div:has-text({leaf_zh!r})")
                if leaf_btn and leaf_btn.is_visible():
                    leaf_btn.click()
                    page.wait_for_timeout(wait_ms)
                    page.evaluate(JS_KILL_MODAL)
                    page.wait_for_timeout(1500)
                    return True
                _log(f"  [nav] ❌ leaf {leaf_zh!r} (展開 {accordion_zh} 後) 找不到")
                return False
            except Exception as e:
                _log(f"  [nav] 失敗: {type(e).__name__}")
                return False

        # 1. My Overview / 我的總覽 — accordion 點開 = navigate
        if not _navigate("My Overview", wait_ms=10000):
            raise RuntimeError("overview-navigation-failed")
        # SCSB My Overview 預設眼睛 icon 隱藏餘額，需點開
        try:
            page.evaluate(
                "(() => { const eyes=[...document.querySelectorAll('i,button,span,div')]"
                ".filter(e=>e.offsetParent!==null &&"
                "  /eye|fa-eye|icon-eye|hide|show/i.test((e.className||'')+(e.getAttribute('title')||'')));"
                " for(const e of eyes) e.click(); return eyes.length; })()",
            )
            page.wait_for_timeout(3000)
        except Exception:
            pass
        try:
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
            raise RuntimeError("overview-twd-inventory-unavailable")

        # 2. TWD Deposit / 台幣存款 — 我的總覽 accordion 下 leaf
        if expected_twd_accounts:
            if not _navigate("TWD Deposit", wait_ms=8000):
                raise RuntimeError("twd-navigation-failed")
            try:
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
            raise RuntimeError("card-navigation-failed")
        try:
            out["card_text"] = (page.evaluate("document.body.innerText") or "")
        except Exception:
            out["card_text"] = ""

        # 3b. Credit Card → Account Management → 信用卡明細 leaf (設計規範：每家都要抓信用卡明細)
        out["card_inquiry"] = self._collect_credit_card_inquiry(page)

        # 從各頁 innerText regex 抽帳號/餘額/卡號
        all_text = "\n".join([out.get("overview_text", ""), out.get("twd_text", ""), out.get("card_text", "")])
        out["accounts"] = self._extract_accounts(all_text)
        parsed_account_numbers = {account["account_no"] for account in out["accounts"]}
        out["accounts"].extend(
            account for account in overview_twd_inventory
            if account["account_no"] not in parsed_account_numbers
        )
        out["totals"] = self._extract_totals(all_text)
        for raw_text_key in ("overview_text", "twd_text", "card_text"):
            out.pop(raw_text_key, None)

        publish_card_bill_facts(out, [])
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
        seen: set[str] = set()
        for m in acct_pattern.finditer(text):
            acct_no = m.group("acct")
            if acct_no in seen:
                continue
            seen.add(acct_no)
            prefix = text[max(0, m.start() - 180):m.start()]
            candidates = [h for h in header_pattern.finditer(prefix)]
            if not candidates:
                type_header = None
            else:
                type_header = candidates[-1].group("type")
            cur = m.group("cur").replace("NT$", "TWD")
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
                    el.click();
                    log.push(label + ': ' + norm(el.innerText || el.textContent || '').slice(0, 50));
                    return true;
                };
                let btn1 = byAnyText(['臺幣存匯', '台幣存匯', '臺幣存款', '台幣存款', 'TWD Deposit']);
                if (!btn1) { log.push('L1 not found'); return {ok: false, log}; }
                clickIfCollapsed(btn1, 'L1 click');
                return new Promise(resolve => setTimeout(() => {
                    let btn2 = byAnyText(['帳戶查詢', '存款查詢', 'TWD Account Inquiry']);
                    if (btn2) clickIfCollapsed(btn2, 'L2 click');
                    setTimeout(() => {
                        let leaf = byAnyText(['帳戶餘額及交易明細查詢', '交易明細', 'Account Balance and Account Statement', 'Account Balance']);
                        if (!leaf) {
                            const dump = allBtns().map(b => norm(b.innerText || b.textContent || '').slice(0, 60));
                            log.push('L3 leaf not found, visible buttons: ' + JSON.stringify(dump));
                            resolve({ok: false, log});
                            return;
                        }
                        clickIfCollapsed(leaf, 'L3 click');
                        resolve({ok: true, log, url: location.href});
                    }, 1500);
                }, 1500));
            }"""

    @staticmethod
    def _twd_inquiry_period_script(*, full_history: bool = True) -> str:
        """JS: 新帳號/強制回補查全量；既有帳號只查最近一個曆月。"""
        script = r"""() => {
            const fullHistory = __FULL_HISTORY__;
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
            let startInput = dateInputs.find(el => /(查詢起日|起日|start)/i.test(ownContext(el)));
            let endInput = dateInputs.find(el => /(查詢迄日|迄日|end)/i.test(ownContext(el)));
            if ((!startInput || !endInput) && dateInputs.length === 2) {
                [startInput, endInput] = dateInputs;
            }
            if (!custom || !startInput || !endInput || startInput === endInput) {
                return {ok: false, error: 'custom-period-controls-missing'};
            }

            custom.click();
            const parseDate = (value) => {
                const match = String(value || '').match(/^(\d{4})[-/](\d{2})[-/](\d{2})$/);
                if (!match) return null;
                const [year, month, day] = match.slice(1).map(Number);
                const date = new Date(year, month - 1, day);
                return date.getFullYear() === year
                    && date.getMonth() === month - 1
                    && date.getDate() === day ? date : null;
            };
            const end = parseDate(endInput.value) || new Date();
            const systemMin = parseDate(startInput.min);
            let start;
            let period;
            if (fullHistory) {
                start = systemMin;
                period = 'system-limit';
                if (!start) {
                    const year = end.getFullYear() - 1;
                    const day = Math.min(end.getDate(), new Date(year, end.getMonth() + 1, 0).getDate());
                    start = new Date(year, end.getMonth(), day);
                    period = 'one-year';
                }
            } else {
                const year = end.getMonth() === 0 ? end.getFullYear() - 1 : end.getFullYear();
                const month = (end.getMonth() + 11) % 12;
                const day = Math.min(end.getDate(), new Date(year, month + 1, 0).getDate());
                start = new Date(year, month, day);
                if (systemMin && start < systemMin) start = systemMin;
                period = 'one-month';
            }
            if (start > end) return {ok: false, error: 'invalid-period-limit'};

            const format = (date, separator) => [
                date.getFullYear(),
                String(date.getMonth() + 1).padStart(2, '0'),
                String(date.getDate()).padStart(2, '0'),
            ].join(separator);
            const setValue = (input, date) => {
                const separator = input.type === 'date' ? '-' : '/';
                const value = format(date, separator);
                const setter = Object.getOwnPropertyDescriptor(
                    Object.getPrototypeOf(input), 'value'
                )?.set;
                if (setter) setter.call(input, value); else input.value = value;
                input.dispatchEvent(new Event('input', {bubbles: true}));
                input.dispatchEvent(new Event('change', {bubbles: true}));
                input.blur();
            };
            custom.dataset.thothScsbTwdPeriod = 'custom';
            startInput.dataset.thothScsbTwdPeriod = 'start';
            endInput.dataset.thothScsbTwdPeriod = 'end';
            setValue(startInput, start);
            setValue(endInput, end);
            const expectedStart = format(start, '/');
            const expectedEnd = format(end, '/');
            const actualStart = parseDate(startInput.value);
            const actualEnd = parseDate(endInput.value);
            return {
                ok: custom.checked
                    && !!actualStart && !!actualEnd
                    && format(actualStart, '/') === expectedStart
                    && format(actualEnd, '/') === expectedEnd,
                period,
                start: expectedStart,
                end: expectedEnd,
            };
        }"""
        return script.replace("__FULL_HISTORY__", "true" if full_history else "false")

    @staticmethod
    def _twd_inquiry_period_verification_script() -> str:
        """JS: Vue/React state 更新後再次核對實際送出的日期。"""
        return r"""(expected) => {
            const normalize = (value) => String(value || '').replaceAll('-', '/');
            const custom = document.querySelector('[data-thoth-scsb-twd-period="custom"]');
            const start = document.querySelector('[data-thoth-scsb-twd-period="start"]');
            const end = document.querySelector('[data-thoth-scsb-twd-period="end"]');
            return {
                ok: !!custom?.checked
                    && normalize(start?.value) === expected.start
                    && normalize(end?.value) === expected.end,
            };
        }"""

    @staticmethod
    def _twd_inquiry_prepare_result_wait_script() -> str:
        """JS: 清除 binding，並記住同條件舊結果以拒絕 stale same-query DOM。"""
        return r"""(expected) => {
            const visible = (el) => el && el.offsetParent !== null;
            const normalize = (value) => String(value || '').replace(/[-.]/g, '/');
            const exactToken = (text, value) => {
                const escaped = normalize(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                return new RegExp(`(^|\\D)${escaped}(?=$|\\D)`).test(normalize(text));
            };
            const tablePattern = /(?:時間|Time)[\s\S]*(?:摘要|Summary)[\s\S]*(?:支出|Expense)[\s\S]*(?:存入|Deposit)[\s\S]*(?:結餘|Balance)/i;
            window.__thothScsbTwdResultScope = null;
            window.__thothScsbTwdOldScopeText = null;
            window.__thothScsbTwdOldTables = new WeakMap();
            for (const table of document.querySelectorAll('table')) {
                if (visible(table) && tablePattern.test(table.innerText || '')) {
                    window.__thothScsbTwdOldTables.set(table, table.innerText || '');
                }
            }
            const emptyPattern = /查無.*(?:交易|資料)|no .*transaction/i;
            window.__thothScsbTwdOldEmpty = new WeakMap();
            for (const empty of document.querySelectorAll('body *')) {
                if (visible(empty) && emptyPattern.test(empty.innerText || '')
                    && ![...empty.children].some(child => emptyPattern.test(child.innerText || ''))) {
                    window.__thothScsbTwdOldEmpty.set(empty, empty.innerText || '');
                }
            }
            for (const table of document.querySelectorAll('table')) {
                if (!visible(table) || !tablePattern.test(table.innerText || '')) continue;
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
                        window.__thothScsbTwdOldScopeText = scope.innerText || '';
                        return;
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
                        window.__thothScsbTwdOldScopeText = scope.innerText || '';
                        return;
                    }
                }
            }
        }"""

    @staticmethod
    def _twd_inquiry_result_ready_script() -> str:
        """JS: criteria 與本次新增/改變的結果必須位於同一個非 body 容器。"""
        return r"""(expected) => {
            const visible = (el) => el && el.offsetParent !== null;
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
                        if (window.__thothScsbTwdOldScopeText !== null
                            && scopeText === window.__thothScsbTwdOldScopeText) continue;
                        return scope;
                    }
                }
                return null;
            };
            const tablePattern = /(?:時間|Time)[\s\S]*(?:摘要|Summary)[\s\S]*(?:支出|Expense)[\s\S]*(?:存入|Deposit)[\s\S]*(?:結餘|Balance)/i;
            const table = [...document.querySelectorAll('table')].find(el =>
                visible(el)
                && tablePattern.test(el.innerText || '')
                && (!window.__thothScsbTwdOldTables.has(el)
                    || window.__thothScsbTwdOldTables.get(el) !== (el.innerText || ''))
                && boundScope(el)
            );
            const emptyPattern = /查無.*(?:交易|資料)|no .*transaction/i;
            const empty = [...document.querySelectorAll('body *')].find(el =>
                visible(el)
                && emptyPattern.test(el.innerText || '')
                && ![...el.children].some(child => emptyPattern.test(child.innerText || ''))
                && (!window.__thothScsbTwdOldEmpty.has(el)
                    || window.__thothScsbTwdOldEmpty.get(el) !== (el.innerText || ''))
                && boundScope(el)
            );
            const result = table || empty;
            window.__thothScsbTwdResultScope = result ? boundScope(result) : null;
            return !!result;
        }"""

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
            if (!visible(scope)) return {ok: false, empty: false, row_count: 0, text: ''};
            const criteria = [...scope.querySelectorAll('*')].some(el => {
                const own = normalize(el.innerText);
                return visible(el)
                    && /查詢條件|Query Criteria/i.test(own)
                    && exactToken(own, expected.start)
                    && exactToken(own, expected.end)
                    && exactToken(own, expected.account_no)
                    && ![...el.children].some(child => /查詢條件|Query Criteria/i.test(child.innerText || ''));
            });
            if (!criteria) return {ok: false, empty: false, row_count: 0, text: ''};
            const tablePattern = /(?:時間|Time)[\s\S]*(?:摘要|Summary)[\s\S]*(?:支出|Expense)[\s\S]*(?:存入|Deposit)[\s\S]*(?:結餘|Balance)/i;
            const table = [...scope.querySelectorAll('table')].find(el =>
                visible(el) && tablePattern.test(el.innerText || '')
            );
            if (table) {
                const rowCount = [...table.rows].filter(row => row.querySelectorAll('td').length).length;
                return {ok: true, empty: false, row_count: rowCount, text: table.innerText || ''};
            }
            const emptyPattern = /查無.*(?:交易|資料)|no .*transaction/i;
            const empty = [...scope.querySelectorAll('*')].some(el =>
                visible(el)
                && emptyPattern.test(el.innerText || '')
                && ![...el.children].some(child => emptyPattern.test(child.innerText || ''))
            );
            return {ok: empty, empty, row_count: 0, text: ''};
        }"""

    @staticmethod
    def _is_twd_query_request(request, expected: dict) -> bool:
        parsed = urlparse(request.url)
        body = request.post_data or ""
        fields: dict[str, list[str]] = {}
        invalid_fields: set[str] = set()

        def add_field(key, value):
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                fields.setdefault(str(key), []).append(str(value))
            else:
                invalid_fields.add(str(key))

        try:
            payload = json.loads(body)
        except (TypeError, json.JSONDecodeError):
            for key, value in parse_qsl(body, keep_blank_values=True):
                add_field(key, value)
        else:
            def walk(value):
                if isinstance(value, dict):
                    for key, child in value.items():
                        if isinstance(child, (dict, list)):
                            invalid_fields.add(str(key))
                            walk(child)
                        else:
                            add_field(key, child)
                elif isinstance(value, list):
                    for child in value:
                        walk(child)
            walk(payload)

        def exact(aliases, value, *, date_value=False):
            if any(alias in invalid_fields for alias in aliases):
                return False
            expected_value = str(value).replace("-", "/") if date_value else str(value)
            candidates = [
                candidate.replace("-", "/") if date_value else candidate
                for alias in aliases for candidate in fields.get(alias, [])
            ]
            return bool(candidates) and all(
                candidate == expected_value for candidate in candidates
            )

        return (
            BankCrawler._exact_https_origin_allowed(
                request.url, frozenset({"ebank.scsb.com.tw"}),
            )
            and parsed.path in {"/ibap/api/query", "/ibap/ibap/query"}
            and request.method == "POST"
            and request.resource_type in {"xhr", "fetch"}
            and exact(
                ("account_no", "accountNo", "account", "accountNumber", "acctNo"),
                expected["account_no"],
            )
            and exact(
                ("start", "startDate", "start_date", "fromDate", "queryStartDate"),
                expected["start"], date_value=True,
            )
            and exact(
                ("end", "endDate", "end_date", "toDate", "queryEndDate"),
                expected["end"], date_value=True,
            )
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
            if (!visible(scope)) return {error: true, pagination: false};
            const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
            const disabled = (el) => el.closest('[disabled],[aria-disabled="true"],.disabled');
            const current = (el) => el.closest('[aria-current="page"],.active');
            const enabled = (el) => visible(el) && !disabled(el);
            const scopeText = norm(scope.textContent).toLowerCase();
            const error = [...scope.querySelectorAll('.alert,.toast,[role="alert"]')]
                .some(el => visible(el) && norm(el.textContent))
                || [
                    '系統錯誤', '系統忙碌', '請稍後再試', '連線逾時', '連線已逾時',
                    '請重新登入', '登入失效', 'system error', 'session expired',
                    'login required', 'timed out', 'timeout', 'unexpected error',
                ].some(marker => scopeText.includes(marker));
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
            return {error, pagination: directNext || numbered};
        }"""

    @staticmethod
    def _parse_twd_inquiry_records(text: str) -> tuple[list[dict], bool]:
        """解析完整交易 DOM；raw preview 的 8 KB 上限不得截斷入庫資料。"""
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
                continue
            date_str = cols[0].strip()
            if not re.match(r"^\d{3,4}/\d{2}/\d{2}$", date_str):
                continue
            year, month, day = (int(part) for part in date_str.split("/"))
            canonical_year = year + 1911 if year < 1000 else year
            try:
                date(canonical_year, month, day)
            except ValueError:
                continue
            date_str = f"{canonical_year:04d}/{month:02d}/{day:02d}"
            summary = cols[1].strip()
            expense = _parse_money(cols[2].strip())
            deposit = _parse_money(cols[3].strip())
            balance = _parse_money(cols[4].strip())
            if (
                not summary
                or balance in (None, "")
                or expense is None
                or deposit is None
                or bool(expense) == bool(deposit)
                or (expense and expense.startswith("-"))
                or (deposit and deposit.startswith("-"))
            ):
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
            raise ValueError("statement month page error")
        required_labels = (
            "Current Period Total Amount Due",
            "Current Period Total Minimum Amount Due",
        )
        if not all(label in text for label in required_labels):
            raise ValueError("incomplete statement month summary")

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
                raise ValueError("invalid statement month amount")
            try:
                amount = Decimal(value.replace("NT$", "").replace(",", "").strip())
            except InvalidOperation as exc:
                raise ValueError("invalid statement month amount") from exc
            if not amount.is_finite() or amount != amount.to_integral_value():
                raise ValueError("fractional statement month amount")
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
                        || /Data Time|Transaction Date|Current Period Total|no .*transaction|no .*statement|查無/i.test(text))
                        && text.includes(leafText)) return text;
                }
            }
            return null;
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
                        || /Current Period Total|查無.*帳單|no .*statement/i.test(text))) continue;
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

    def _collect_twd_account(self, page, selector: str, account_no: str) -> dict:
        """查一個 SCSB 台幣帳戶；任何期間、結果或 parser 不完整都 fail closed。"""
        page.select_option(selector, value=account_no)
        page.wait_for_timeout(1500)

        period = page.evaluate(
            self._twd_inquiry_period_script(
                full_history=getattr(self, "full_history", True),
            ),
        )
        _log(f"[twd_inq] period → {period}")
        if not period.get("ok"):
            raise RuntimeError(period.get("error") or "lookback-period-unavailable")
        page.wait_for_timeout(500)
        period_check = page.evaluate(
            self._twd_inquiry_period_verification_script(), period,
        )
        if not period_check.get("ok"):
            raise RuntimeError("lookback-period-not-applied")

        page.wait_for_timeout(1500)
        expected = {**period, "account_no": account_no}
        page.evaluate(self._twd_inquiry_prepare_result_wait_script(), expected)
        with page.expect_request(
            lambda request: self._is_twd_query_request(request, expected),
            timeout=120000,
        ) as request_info:
            clicked_confirm = page.evaluate(
                "(() => { const btns=[...document.querySelectorAll('button,a')];"
                " const b=btns.find(e=>e.offsetParent!==null && /^(Confirm|查詢|確認|Search)$/i.test((e.innerText||'').trim()));"
                " if(b){ b.click(); return b.innerText; } return null; })()",
            )
        _log(f"[twd_inq] click Confirm → {clicked_confirm}")
        query_response = request_info.value.response()
        if not clicked_confirm or query_response is None or not query_response.ok:
            raise RuntimeError("query-submit-unavailable")
        page.wait_for_function(
            self._twd_inquiry_result_ready_script(),
            arg=expected,
            timeout=120000,
        )
        result_state = page.evaluate(self._twd_inquiry_result_state_script())
        if result_state.get("error") or result_state.get("pagination"):
            raise RuntimeError("query-result-incomplete")

        extracted = page.evaluate(self._twd_inquiry_extract_result_script(), expected)
        if not extracted.get("ok"):
            raise RuntimeError("query-result-unbound")
        result_text = extracted.get("text") or ""
        records, parse_complete = self._parse_twd_inquiry_records(result_text)
        if (
            not parse_complete
            or len(records) != extracted.get("row_count")
            or (not records and not extracted.get("empty"))
        ):
            raise RuntimeError("partial-transaction-table")
        start_date = date.fromisoformat(period["start"].replace("/", "-"))
        end_date = date.fromisoformat(period["end"].replace("/", "-"))
        for record in records:
            record_date = date.fromisoformat(record["date"].replace("/", "-"))
            if not start_date <= record_date <= end_date:
                raise RuntimeError("transaction-outside-query-period")
            record["account_no"] = account_no

        return {
            "account_no": account_no,
            "period": period,
            "records": records,
        }

    def _collect_twd_inquiry(
        self, page, expected_accounts: set[str],
    ) -> dict:
        """進入 SCSB 台幣交易明細頁，逐一查完所有帳戶。"""
        try:
            ret = page.evaluate(self._twd_inquiry_nav_script())
            _log(f"[twd_inq] JS sequence ok={bool(ret and ret.get('ok'))}")
            if not isinstance(ret, dict) or ret.get("ok") is not True:
                raise RuntimeError("twd-inquiry-navigation-failed")
            page.wait_for_timeout(8000)
            page.evaluate(JS_KILL_MODAL)
            page.wait_for_timeout(1500)

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
                raise RuntimeError("account-control-unavailable")
            if seen_accounts != expected_accounts:
                raise RuntimeError("account-inventory-incomplete")

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
            raise RuntimeError("SCSB TWD inquiry failed") from None

    def _collect_credit_card_inquiry(self, page) -> dict:
        """SCSB 信用卡明細 — 三層 accordion 後抓多個 leaf。

          Level 1: Credit Card Services (已展開)
          Level 2: Credit Card Account Management ▾
          Level 3 leaves (按優先序抓)：
            - Unbilled Transaction Details   未出帳明細 (= pending)
            - Real-Time Transaction Records  即時消費 (= current)
            - Statement Inquiry and Payment  帳單查詢 (= billed)
            - Account Balance Inquiry        卡片餘額
        """
        from backend.core.persist.scsb import _scsb_page_error, _scsb_parse_card_rows

        result: dict = {"leaves": {}}
        leaves_to_visit = [
            ("Unbilled Transaction Details", "unbilled"),
            ("Real-Time Transaction Records", "current"),
            ("Statement Inquiry and Payment", "statement"),
        ]

        for leaf_text, key in leaves_to_visit:
            try:
                before_leaf_text = page.evaluate(
                    self._card_leaf_scope_text_script(), leaf_text,
                )
                ret = page.evaluate("""(leafText) => {
                    const log = [];
                    const allBtns = () => [...document.querySelectorAll('button.accordion-button')];
                    const unique = (text, visibleOnly = false) => {
                        const matches = allBtns().filter(button =>
                            (!visibleOnly || button.offsetParent !== null)
                            && (button.innerText || '').trim() === text);
                        return matches.length === 1 ? matches[0] : null;
                    };

                    // Level 1: Credit Card Services
                    let btn1 = unique('Credit Card Services');
                    if (!btn1) { log.push('L1 not found'); return {ok: false, log}; }
                    if (btn1.classList.contains('collapsed')) { btn1.click(); log.push('L1 expand'); }

                    return new Promise(resolve => setTimeout(() => {
                        // Level 2: Credit Card Account Management
                        let btn2 = unique('Credit Card Account Management');
                        if (!btn2) { log.push('L2 not found'); resolve({ok: false, log}); return; }
                        if (btn2.classList.contains('collapsed')) { btn2.click(); log.push('L2 expand'); }

                        setTimeout(() => {
                            const leaf = unique(leafText, true);
                            if (!leaf) { log.push('leaf not found: ' + leafText); resolve({ok: false, log}); return; }
                            leaf.click();
                            resolve({ok: true});
                        }, 1500);
                    }, 1500));
                }""", leaf_text)
                if not isinstance(ret, dict) or ret.get("ok") is not True:
                    raise RuntimeError("card-leaf-navigation-failed")
                page.wait_for_function("""([leafText, before]) => {
                    const visible = (el) => el && el.offsetParent !== null;
                    const leaves = [...document.querySelectorAll('body *')].filter(el =>
                        visible(el) && (el.textContent || '').trim() === leafText
                        && ![...el.children].some(child =>
                            (child.textContent || '').trim() === leafText));
                    for (const leaf of leaves) {
                        for (let scope = leaf.parentElement; scope && scope !== document.body; scope = scope.parentElement) {
                            if (['MAIN', 'HTML'].includes(scope.tagName)) continue;
                            const text = scope.innerText || '';
                            if ((scope.querySelector('table')
                                || /Data Time|Transaction Date|Current Period Total|no .*transaction|no .*statement|查無/i.test(text))
                                && text.includes(leafText)) {
                                return before == null || text !== before;
                            }
                        }
                    }
                    return false;
                }""", arg=[leaf_text, before_leaf_text], timeout=120000)
                _log(f"[card_inq:{key}] navigation ok")
                page.wait_for_timeout(8000)
                page.evaluate(JS_KILL_MODAL)
                page.wait_for_timeout(1500)

                leaf_result: dict = {"nav": {"ok": True}}
                try:
                    leaf_result["text"] = page.evaluate(
                        self._card_leaf_scope_text_script(), leaf_text,
                    ) or ""
                except Exception:
                    raise RuntimeError("card-leaf-read-failed") from None
                if leaf_text.lower() not in leaf_result["text"].lower():
                    raise RuntimeError("card-leaf-identity-unverified")

                # 若有 date input 就填表 + Confirm（同 twd_inq）
                if leaf_text in ("Statement Inquiry and Payment", "Real-Time Transaction Records"):
                    try:
                        from datetime import datetime, timedelta
                        start_dt = (datetime.now() - timedelta(days=30)).strftime("%Y/%m/%d")
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
                                        || /Data Time|Transaction Date|Current Period Total|no .*transaction|no .*statement|查無/i.test(text))) continue;
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
                        for di in pending_date_inputs:
                            sel = f"#{di['id']}"
                            try:
                                page.click(sel)
                                page.wait_for_timeout(200)
                                page.keyboard.press("Control+A")
                                page.keyboard.press("Delete")
                                page.keyboard.type(start_dt, delay=50)
                                page.evaluate(
                                    "(sel) => { const el=document.querySelector(sel); if(el){"
                                    "  el.dispatchEvent(new Event('input', {bubbles:true}));"
                                    "  el.dispatchEvent(new Event('change', {bubbles:true}));"
                                    "  el.blur(); } }",
                                    sel,
                                )
                                page.wait_for_timeout(300)
                                filled_date = True
                                break
                            except Exception:
                                continue
                        if not filled_date:
                            raise RuntimeError("card-query-date-unavailable")
                        before_confirm = leaf_result["text"]
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
                                        || /Data Time|Transaction Date|Current Period Total|no .*transaction|no .*statement|查無/i.test(text))) continue;
                                    const matches = [...scope.querySelectorAll('button,a')].filter(el =>
                                        visible(el) && /^(Confirm|查詢|確認|Search)$/i.test((el.innerText || '').trim()));
                                    if (matches.length !== 1) return null;
                                    matches[0].click();
                                    return true;
                                }
                            }
                            return null;
                        }""", leaf_text)
                        if not clicked:
                            raise RuntimeError("card-query-submit-unavailable")
                        _log(f"[card_inq:{key}] Confirm clicked")
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
                                        || /Data Time|Transaction Date|Current Period Total|no .*transaction|no .*statement|查無/i.test(candidate))
                                        && candidate.includes(leafText)) {
                                        text = candidate;
                                        break;
                                    }
                                }
                                if (text) break;
                            }
                            const ready = scopeName === 'current'
                                ? /Real-Time Transaction Records[\s\S]*(?:Transaction Date|no (?:real-time )?transaction)/i.test(text)
                                : /Statement Inquiry and Payment[\s\S]*(?:(?:Data Time[\s\S]*20\d{2}\/(?:0[1-9]|1[0-2]))|(?:查無.*帳單|no .*statement))/i.test(text);
                            return text !== before && ready;
                        }""", arg=[before_confirm, key, leaf_text], timeout=120000)
                        leaf_result["text_final"] = page.evaluate(
                            self._card_leaf_scope_text_script(), leaf_text,
                        ) or ""
                    except Exception:
                        raise RuntimeError("card-query-form-failed") from None

                # 2026-06-13 升級：Statement Inquiry 加月份 tab 迭代抓帳單
                # SCSB 顯示「2026/05 / 2026/04 / 2026/03」3 個月份 tab，點切換每月帳單
                if leaf_text == "Statement Inquiry and Payment":
                    try:
                        leaf_text_now = leaf_result.get("text", "")
                        statement_text = leaf_result.get("text_final") or leaf_text_now
                        month_tabs = self._statement_month_tabs(statement_text)
                        if not month_tabs and not re.search(
                            r"查無.*帳單|no .*statement", statement_text, re.I,
                        ):
                            raise RuntimeError("statement-month-tabs-unavailable")
                        _log(f"[card_inq:{key}] 月份 tabs: {month_tabs}")

                        # 每個月份點 tab + dump
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
                            raise RuntimeError("statement-month-tab-unavailable")
                        panel_selectors = [
                            target.get("panel") for target in month_targets.values()
                            if target.get("panel")
                        ]
                        months_data = []
                        for mo in month_tabs:
                            target = month_targets[mo]

                            def click_month():
                                return page.evaluate("""([month, leafText]) => {
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
                                            if (matches.length !== 1) return false;
                                            matches[0].click();
                                            return true;
                                        }
                                    }
                                    return false;
                                }""", [mo, leaf_text])

                            panel_selector = target.get("panel")
                            if panel_selectors.count(panel_selector) != 1:
                                panel_selector = None
                            if panel_selector:
                                if not click_month():
                                    raise RuntimeError("statement-month-tab-unavailable")
                            else:
                                with page.expect_request(
                                    lambda request, month=mo: self._is_statement_month_request(
                                        request, month,
                                    ),
                                    timeout=120000,
                                ) as month_request_info:
                                    if not click_month():
                                        raise RuntimeError("statement-month-tab-unavailable")
                                month_response = month_request_info.value.response()
                                if month_response is None or month_response.ok is not True:
                                    raise RuntimeError("statement-month-query-failed")

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
                                page.wait_for_function("""(selector) => {
                                    const panel = document.querySelector(selector);
                                    return panel && panel.offsetParent !== null;
                                }""", arg=panel_selector, timeout=120000)
                                mo_text = page.evaluate(
                                    "(selector) => document.querySelector(selector)?.innerText || ''",
                                    panel_selector,
                                )
                            else:
                                mo_text = page.evaluate(
                                    self._card_leaf_scope_text_script(), leaf_text,
                                ) or ""
                            months_data.append({
                                "month": mo,
                                **self._statement_month_summary(mo_text),
                            })
                            _log(f"[card_inq:{key}] {mo} tab click OK, text len={len(mo_text)}")
                        leaf_result["months"] = months_data
                    except Exception:
                        raise RuntimeError("statement-month-collection-failed") from None

                source_text = leaf_result.get("text_final") or leaf_result.get("text") or ""
                if _scsb_page_error(source_text):
                    raise RuntimeError("card-leaf-page-error")
                if key in ("unbilled", "current"):
                    rows, complete = _scsb_parse_card_rows(source_text, scope=key)
                    lower = source_text.lower()
                    explicit_empty = (
                        (key == "unbilled" and (
                            "no new transactions" in lower
                            or "have not yet been recorded" in lower
                        ))
                        or (key == "current" and (
                            "no real-time transaction" in lower
                            or "no transaction records" in lower
                        ))
                    )
                    if rows and complete:
                        leaf_result["rows"] = rows
                    elif explicit_empty:
                        leaf_result["empty"] = True
                    else:
                        raise RuntimeError("card-transaction-scope-incomplete")
                leaf_result.pop("text", None)
                leaf_result.pop("text_final", None)
                result["leaves"][key] = leaf_result
            except Exception:
                _log(f"[card_inq:{key}] failed")
                raise RuntimeError("SCSB credit-card inquiry failed") from None

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
