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
import contextlib
import re
import sys
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

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
        {
            "id": item.get("id"),
            "name": item.get("name"),
            "option_count": len(item.get("options") or []),
        }
        for item in selects
    ]


class ScsbLoginError(RuntimeError):
    """SCSB login 送出後失敗——立刻中止，絕不自動重打（防鎖帳號）。"""


class ScsbCrawler(BankCrawler):
    USES_SHARED_LOGIN_CHECKPOINTS: ClassVar[bool] = True
    CREDENTIAL_HOSTS = frozenset({"ibank.scsb.com.tw", "ebank.scsb.com.tw"})

    def __init__(self):
        super().__init__(name="scsb")
        self.creds = ScsbCreds.load()

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

        # W (2026-06-17): 走 BANK_DATA_ROOT env，避免 hardcode home path
        # — cloud container 沒 ~/src/thoth，會 FileNotFoundError。
        from backend.core.store import _data_root
        debug_dir = _data_root() / "scsb_collect"
        debug_dir.mkdir(parents=True, exist_ok=True)

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
        # 2026-06-18 evidence dump: 先 inventory menu DOM 給 telemetry — 不論成功與否
        try:
            menu_audit = page.evaluate(
                "(() => { "
                "const out=[]; "
                "for (const tag of ['button','a','div','span','li']) {"
                "  for (const e of document.querySelectorAll(tag)) {"
                "    if (e.offsetParent === null) continue;"
                "    const t=(e.textContent||'').trim();"
                "    if (t.length===0 || t.length>40) continue;"
                "    if (/Overview|Deposit|Credit|Card|Loan|Investment|存款|信用卡|貸款|投資|總覽|餘額|轉帳/i.test(t)) {"
                "      out.push({tag, text:t, cls:(e.className||'').slice(0,60)});"
                "      if (out.length >= 40) return out;"
                "    }"
                "  }"
                "}"
                "return out;"
                "})()",
            ) or []
            out["menu_dom_audit"] = menu_audit[:40]
            _log(f"[collect] menu DOM audit: {len(menu_audit)} matching elements")
        except Exception as e:
            out["menu_dom_audit"] = []
            _log(f"[collect] menu DOM audit fail: {type(e).__name__}")

        _navigate("My Overview", wait_ms=10000)
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
        out["overview_url"] = page.url
        try:
            out["overview_text"] = (page.evaluate("document.body.innerText") or "")
        except Exception:
            out["overview_text"] = ""
        _log(f"  overview text len={len(out['overview_text'])}")
        with contextlib.suppress(Exception):
            page.screenshot(path=str(debug_dir / "overview.png"), full_page=True)

        # 2. TWD Deposit / 台幣存款 — 我的總覽 accordion 下 leaf
        _navigate("TWD Deposit", wait_ms=8000)
        out["twd_url"] = page.url
        try:
            out["twd_text"] = (page.evaluate("document.body.innerText") or "")
            page.screenshot(path=str(debug_dir / "twd.png"), full_page=True)
        except Exception:
            out["twd_text"] = ""

        # 2b. TWD Deposit → Account Balance and Account Statement (深入點 leaf)
        out["twd_inquiry"] = self._collect_twd_inquiry(page, debug_dir)

        # 3. Credit Card / 信用卡 — accordion 本身 = navigate
        _navigate("Credit Card", wait_ms=8000)
        out["card_url"] = page.url
        try:
            out["card_text"] = (page.evaluate("document.body.innerText") or "")
            page.screenshot(path=str(debug_dir / "card.png"), full_page=True)
        except Exception:
            out["card_text"] = ""

        # 3b. Credit Card → Account Management → 信用卡明細 leaf (設計規範：每家都要抓信用卡明細)
        out["card_inquiry"] = self._collect_credit_card_inquiry(page, debug_dir)

        # 從各頁 innerText regex 抽帳號/餘額/卡號
        all_text = "\n".join([out.get("overview_text", ""), out.get("twd_text", ""), out.get("card_text", "")])
        out["accounts"] = self._extract_accounts(all_text)
        out["totals"] = self._extract_totals(all_text)

        out["_final_url"] = page.url
        out["_all_endpoints"] = sorted({h.endpoint for h in collector.hits if h.resp_json})
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

    def _collect_twd_inquiry(self, page, debug_dir) -> dict:
        """點 TWD Deposit → TWD Account Inquiry → Account Balance and Account Statement (leaf)。

        SCSB SPA 三層 accordion 結構：
          Level 1: TWD Deposit (accordion-button)
          Level 2: TWD Account Inquiry (accordion-button, nested in Level 1)
          Level 3 (leaf): Account Balance and Account Statement (accordion-button, nested in Level 2)
        三層都是 button.accordion-button——leaf 點下去才會 SPA route 跳轉。
        """
        result: dict = {}
        try:
            # 用一段 JS 把三層都精確展開、最後 click leaf
            # 2026-06-24: SCSB UI 已是中文「臺幣存匯 / 交易明細」，不能只找英文 TWD Deposit。
            ret = page.evaluate(self._twd_inquiry_nav_script())
            _log(f"[twd_inq] JS sequence → {ret}")
            page.wait_for_timeout(8000)
            page.evaluate(JS_KILL_MODAL)
            page.wait_for_timeout(1500)
            page.screenshot(path=str(debug_dir / "twd_inquiry_form.png"), full_page=True)

            # 點完跳到查詢條件頁——填表 + 按 Confirm
            _log("[twd_inq] 已進入查詢頁，填表查詢")
            try:
                # 1) Account dropdown 選第一個（或全部？先試第一個）
                #    SCSB 用 native <select>
                accts_in_select = page.evaluate("""() => {
                    const sels = [...document.querySelectorAll('select')];
                    return sels.map(s => ({
                        id: s.id, name: s.name,
                        options: [...s.options].slice(0, 10).map(o => ({value: o.value, text: o.textContent.trim().slice(0, 50)}))
                    }));
                }""")
                _log(f"[twd_inq] 表單 selects: {_safe_select_inventory(accts_in_select)}")

                # 找含帳號 options 的 select
                target_sel = None
                target_val = None
                for sel in accts_in_select:
                    for opt in sel.get("options", []):
                        if opt.get("value") and re.search(r"\d{10,}", opt.get("value", "")):
                            target_sel = sel.get("id") or sel.get("name")
                            target_val = opt["value"]
                            break
                    if target_sel:
                        break
                if target_sel and target_val:
                    _log(f"[twd_inq] 選帳號: select={target_sel} selected=true")
                    result["account_no"] = target_val.strip()
                    try:
                        page.select_option(f"#{target_sel}" if target_sel else "select", value=target_val)
                        page.wait_for_timeout(1500)
                    except Exception as e:
                        _log(f"[twd_inq] select_option 失敗: {type(e).__name__}")

                # 2) 查詢期間：預設當日會沒有明細，明確選「近一月」較穩。
                clicked_range = page.evaluate("""() => {
                    const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    const els = [...document.querySelectorAll('button,label,a,span,div')]
                        .filter(e => e.offsetParent !== null);
                    const target = els.find(e => ['近一月', '最近一個月', 'Last Month'].includes(norm(e.innerText || e.textContent || '')));
                    if (target) { target.click(); return norm(target.innerText || target.textContent || ''); }
                    return null;
                }""")
                _log(f"[twd_inq] click range → {clicked_range}")
                page.wait_for_timeout(800)

                # 3) 補 start date（30 天前）
                # SCSB SPA 用 Vue/React，page.fill 寫值不會觸發 input event → 前端 binding 不認
                # 改用 keyboard type 或 dispatchEvent 強制觸發
                from datetime import datetime, timedelta
                start_dt = (datetime.now() - timedelta(days=30)).strftime("%Y/%m/%d")
                date_inputs = page.evaluate("""() => {
                    return [...document.querySelectorAll('input')]
                        .filter(i => i.offsetParent !== null)
                        .map(i => ({
                            id: i.id, name: i.name, type: i.type, value: i.value,
                            placeholder: i.placeholder,
                            label: (i.closest('.form-group,.row,div')?.innerText || '').slice(0, 80),
                        }));
                }""")
                _log(f"[twd_inq] inputs: {date_inputs}")
                picked_start = False
                for di in date_inputs:
                    hay = " ".join(str(di.get(k) or "") for k in ("id", "name", "type", "placeholder", "label"))
                    if "迄" in hay or "end" in hay.lower():
                        continue
                    if not ("起" in hay or "start" in hay.lower() or "日期" in hay or "date" in hay.lower()):
                        continue
                    sel = f"#{di['id']}" if di.get("id") else f"input[name='{di['name']}']" if di.get("name") else None
                    if not sel:
                        continue
                    try:
                        page.click(sel)
                        page.wait_for_timeout(300)
                        page.keyboard.press("Control+A")
                        page.keyboard.press("Delete")
                        page.wait_for_timeout(200)
                        page.keyboard.type(start_dt, delay=50)
                        page.wait_for_timeout(300)
                        page.evaluate(
                            "(sel) => { const el=document.querySelector(sel); if(el){"
                            "  el.dispatchEvent(new Event('input', {bubbles:true}));"
                            "  el.dispatchEvent(new Event('change', {bubbles:true}));"
                            "  el.blur(); } }",
                            sel,
                        )
                        page.wait_for_timeout(500)
                        _log(f"[twd_inq] 填 start date (kbd type): {sel} = {start_dt}")
                        picked_start = True
                        break
                    except Exception as e:
                        _log(f"[twd_inq] 填日期失敗: {type(e).__name__}")
                if not picked_start:
                    _log("[twd_inq] 找不到可填的查詢起日 input")

                # 4) 點 Confirm
                page.wait_for_timeout(1500)
                clicked_confirm = page.evaluate(
                    "(() => { const btns=[...document.querySelectorAll('button,a')];"
                    " const b=btns.find(e=>e.offsetParent!==null && /^(Confirm|查詢|確認|Search)$/i.test((e.innerText||'').trim()));"
                    " if(b){ b.click(); return b.innerText; } return null; })()",
                )
                _log(f"[twd_inq] click Confirm → {clicked_confirm}")
                page.wait_for_timeout(10000)
                page.screenshot(path=str(debug_dir / "twd_inquiry_results.png"), full_page=True)

            except Exception as e:
                _log(f"[twd_inq] 填表失敗: {type(e).__name__}")

            # dump 點完後 dom
            result["url"] = page.url
            try:
                result["text"] = (page.evaluate("document.body.innerText") or "")[:8000]
            except Exception:
                result["text"] = ""
            _log(f"[twd_inq] text len={len(result['text'])}")

            # regex 抽明細交易（SCSB 帳戶明細表格用 \t 分隔，欄位：Time/Summary/Expense/Deposit/Balance/Remarks）
            # 金額格式 'NT$ 60,404'，Expense 或 Deposit 其一可為空
            txt = result["text"]
            recs = []
            # pattern: 日期\t摘要\tExpense(可空)\tDeposit(可空)\tBalance\tRemarks
            for line in txt.split("\n"):
                if not line.startswith("20") and not re.match(r"^\d{3}/\d{2}/\d{2}\t", line):
                    continue
                cols = line.split("\t")
                if len(cols) < 5:
                    continue
                date_str = cols[0].strip()
                if not re.match(r"^\d{3,4}/\d{2}/\d{2}$", date_str):
                    continue
                summary = cols[1].strip()
                expense_raw = cols[2].strip()
                deposit_raw = cols[3].strip()
                balance_raw = cols[4].strip()
                remarks = cols[5].strip() if len(cols) > 5 else ""
                def _strip_nt(s):
                    return s.replace("NT$", "").replace(",", "").strip() if s else ""
                recs.append({
                    "date": date_str,
                    "summary": summary,
                    "expense": _strip_nt(expense_raw),
                    "deposit": _strip_nt(deposit_raw),
                    "balance": _strip_nt(balance_raw),
                    "remarks": remarks,
                })
            result["records"] = recs
            _log(f"[twd_inq] 抽到 {len(recs)} 筆交易")

            # 抓 Account Balance / Available Balance / Total Expenditure / Total Deposit
            for label, key in [
                ("Account Balance", "account_balance"),
                ("Available Balance", "available_balance"),
                ("Total Expenditure", "total_expenditure"),
                ("Total Deposit", "total_deposit"),
            ]:
                m = re.search(rf"{label}\s*\n\s*NT\$\s*([\d,]+)", txt)
                if m:
                    result[key] = m.group(1).replace(",", "")

        except Exception as e:
            _log(f"[twd_inq] ❌ 整段失敗: {type(e).__name__}")
            result["error"] = str(e)
        return result

    def _collect_credit_card_inquiry(self, page, debug_dir) -> dict:
        """SCSB 信用卡明細 — 三層 accordion 後抓多個 leaf。

          Level 1: Credit Card Services (已展開)
          Level 2: Credit Card Account Management ▾
          Level 3 leaves (按優先序抓)：
            - Unbilled Transaction Details   未出帳明細 (= pending)
            - Real-Time Transaction Records  即時消費 (= current)
            - Statement Inquiry and Payment  帳單查詢 (= billed)
            - Account Balance Inquiry        卡片餘額
        """
        result: dict = {"leaves": {}}
        leaves_to_visit = [
            ("Unbilled Transaction Details", "unbilled"),
            ("Real-Time Transaction Records", "current"),
            ("Statement Inquiry and Payment", "statement"),
        ]

        for leaf_text, key in leaves_to_visit:
            try:
                ret = page.evaluate("""(leafText) => {
                    const log = [];
                    const allBtns = () => [...document.querySelectorAll('button.accordion-button')];

                    // Level 1: Credit Card Services
                    let btn1 = allBtns().find(b => (b.innerText||'').trim() === 'Credit Card Services');
                    if (!btn1) { log.push('L1 not found'); return {ok: false, log}; }
                    if (btn1.classList.contains('collapsed')) { btn1.click(); log.push('L1 expand'); }

                    return new Promise(resolve => setTimeout(() => {
                        // Level 2: Credit Card Account Management
                        let btn2 = allBtns().find(b => (b.innerText||'').trim() === 'Credit Card Account Management');
                        if (!btn2) { log.push('L2 not found'); resolve({ok: false, log}); return; }
                        if (btn2.classList.contains('collapsed')) { btn2.click(); log.push('L2 expand'); }

                        setTimeout(() => {
                            const leaf = allBtns().find(b =>
                                b.offsetParent !== null &&
                                (b.innerText||'').trim() === leafText
                            );
                            if (!leaf) { log.push('leaf not found: ' + leafText); resolve({ok: false, log}); return; }
                            leaf.click();
                            resolve({ok: true, log, clicked: leafText, url: location.href});
                        }, 1500);
                    }, 1500));
                }""", leaf_text)
                _log(f"[card_inq:{key}] navigate {leaf_text} → {ret}")
                page.wait_for_timeout(8000)
                page.evaluate(JS_KILL_MODAL)
                page.wait_for_timeout(1500)
                page.screenshot(path=str(debug_dir / f"card_inq_{key}.png"), full_page=True)

                leaf_result = {"url": page.url, "nav": ret}
                try:
                    leaf_result["text"] = (page.evaluate("document.body.innerText") or "")[:12000]
                except Exception:
                    leaf_result["text"] = ""

                # 若有 date input 就填表 + Confirm（同 twd_inq）
                if leaf_text in ("Statement Inquiry and Payment", "Real-Time Transaction Records"):
                    try:
                        from datetime import datetime, timedelta
                        start_dt = (datetime.now() - timedelta(days=30)).strftime("%Y/%m/%d")
                        date_inputs = page.evaluate("""() => {
                            return [...document.querySelectorAll('input')]
                                .filter(i => i.offsetParent !== null && /date/i.test((i.id||'')+(i.name||'')+(i.placeholder||'')+(i.type||'')))
                                .map(i => ({id: i.id, name: i.name, value: i.value}));
                        }""")
                        for di in date_inputs:
                            if not di.get("value") and di.get("id"):
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
                                    break
                                except Exception:
                                    pass
                        clicked = page.evaluate(
                            "(() => { const btns=[...document.querySelectorAll('button,a')];"
                            " const b=btns.find(e=>e.offsetParent!==null && /^(Confirm|查詢|確認|Search)$/i.test((e.innerText||'').trim()));"
                            " if(b){ b.click(); return b.innerText; } return null; })()",
                        )
                        if clicked:
                            _log(f"[card_inq:{key}] Confirm clicked")
                            page.wait_for_timeout(8000)
                            page.screenshot(path=str(debug_dir / f"card_inq_{key}_results.png"), full_page=True)
                            leaf_result["text_final"] = (page.evaluate("document.body.innerText") or "")[:12000]
                            leaf_result["url_final"] = page.url
                    except Exception as e:
                        _log(f"[card_inq:{key}] 填表失敗: {type(e).__name__}")

                # 2026-06-13 升級：Statement Inquiry 加月份 tab 迭代抓帳單
                # SCSB 顯示「2026/05 / 2026/04 / 2026/03」3 個月份 tab，點切換每月帳單
                if leaf_text == "Statement Inquiry and Payment":
                    try:
                        import re as _re_local
                        # 從 text 抽月份 tab 候選 (YYYY/MM 格式)
                        leaf_text_now = leaf_result.get("text", "")
                        # 「Data Time：YYYY/MM/DD HH:MM:SS\n2026/05\n2026/04\n2026/03」pattern
                        # 取 Data Time 之後 3-6 個月份 tab
                        month_tabs = []
                        dt_idx = leaf_text_now.find("Data Time")
                        if dt_idx > 0:
                            tail = leaf_text_now[dt_idx:dt_idx+200]
                            for m in _re_local.finditer(r"202[0-9]/\d{2}\b", tail):
                                mo = m.group()
                                if mo not in month_tabs and len(month_tabs) < 6:
                                    month_tabs.append(mo)
                        _log(f"[card_inq:{key}] 月份 tabs: {month_tabs}")

                        # 每個月份點 tab + dump
                        months_data = []
                        for mo in month_tabs:
                            clicked_month = page.evaluate("""(month) => {
                                const all = [...document.querySelectorAll('button,a,span,div,li')];
                                const target = all.find(e =>
                                    e.offsetParent !== null &&
                                    (e.textContent || '').trim() === month
                                );
                                if (target) { target.click(); return true; }
                                return false;
                            }""", mo)
                            if clicked_month:
                                page.wait_for_timeout(3000)
                                mo_text = (page.evaluate("document.body.innerText") or "")[:8000]
                                months_data.append({"month": mo, "text": mo_text})
                                _log(f"[card_inq:{key}] {mo} tab click OK, text len={len(mo_text)}")
                            else:
                                _log(f"[card_inq:{key}] {mo} tab 找不到")
                        leaf_result["months"] = months_data
                    except Exception as e:
                        _log(f"[card_inq:{key}] 月份 tab 切換失敗: {type(e).__name__}")

                result["leaves"][key] = leaf_result
            except Exception as e:
                _log(f"[card_inq:{key}] ❌ 失敗: {type(e).__name__}")
                result["leaves"][key] = {"error": str(e)}

        return result


if __name__ == "__main__":
    import json
    crawler = ScsbCrawler()
    try:
        result = crawler.run(login_url=BASE, headless=True)
    except ScsbLoginError as e:
        result = {"error": "login_failed_stop", "detail": str(e)}

    out_file = Path(__file__).resolve().parents[1] / "data" / "scsb_collected.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"\n[done] 已存: {out_file}")

    if result.get("error"):
        _log(f"  ❌ error: {result['error']}")
    else:
        data = result.get("data")
        if not isinstance(data, dict):
            data = {}
        _log("\n===== 抓取摘要 =====")
        _log(f"  抓到帳號: {len(data.get('accounts', []))} 個")
        _log(f"  攔到 endpoint: {len(data.get('_all_endpoints', []))}")
