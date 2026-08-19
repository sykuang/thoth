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
from backend.core.base import BankCollectResult, BankCrawler, ResponseCollector
from backend.core.card_bills import publish_card_bill_facts
from backend.core.captcha import ocr_bytes
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
                controls = scope.locator(
                    "[name='__reCaptcha'], input[type='password']"
                )
                if any(
                    controls.nth(index).is_visible()
                    for index in range(controls.count())
                ):
                    return False
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
            page.goto("https://ebank.standardchartered.com.tw/scb/", timeout=15000)
            page.wait_for_timeout(5000)
        except Exception:
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
                page.goto(BASE, timeout=15000)
            page.wait_for_timeout(8000)
        except Exception:
            raise ScbLoginError("無法安全準備登入頁面；未送出登入") from None

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
            actions = scope.locator("button, a")
            for index in range(actions.count()):
                action = actions.nth(index)
                if (
                    action.is_visible()
                    and action.is_enabled()
                    and " ".join(action.inner_text().split()) == "重新產生"
                ):
                    matches.append(action)
        if len(matches) != 1:
            return False
        matches[0].click()
        page.wait_for_timeout(1500)
        return True

    @staticmethod
    def _keyboard_fill(page, field, value: str) -> None:
        field.click()
        page.wait_for_timeout(150)
        field.click(click_count=3)
        page.wait_for_timeout(100)
        page.keyboard.press("Backspace")
        page.wait_for_timeout(100)
        page.keyboard.type(value, delay=80)
        page.wait_for_timeout(300)
        if len(field.input_value()) != len(value):
            raise ScbLoginError("登入欄位輸入長度不符；未送出登入")

    def prepare_captcha_resubmit(self, page) -> None:
        try:
            if not self._click_unique_refresh(page):
                raise ScbLoginError("無法安全更新驗證碼；未送出登入")
            for _ in range(10):
                stale_error = False
                for scope in self._page_scopes(page):
                    for selector in (".error", ".alert", "[role='alert']"):
                        alerts = scope.locator(selector)
                        for index in range(alerts.count()):
                            alert = alerts.nth(index)
                            if alert.is_visible() and re.search(
                                r"(?<![A-Za-z0-9_])CAPT\d+(?![A-Za-z0-9_])",
                                " ".join(alert.inner_text().split()),
                                re.IGNORECASE,
                            ):
                                stale_error = True
                if not stale_error:
                    return
                page.wait_for_timeout(300)
        except ScbLoginError:
            raise
        except Exception:
            raise ScbLoginError("無法安全更新驗證碼；未送出登入") from None
        raise ScbLoginError("驗證碼錯誤狀態未清除；未送出登入")

    @classmethod
    def _visible_alert_state(cls, page) -> tuple[bool, bool, bool]:
        captcha = explicit = other = False
        for scope in cls._page_scopes(page):
            for selector in (".error", ".alert", "[role='alert']"):
                alerts = scope.locator(selector)
                for index in range(alerts.count()):
                    alert = alerts.nth(index)
                    if not alert.is_visible():
                        continue
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
            try:
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
                if not isinstance(cap_src, str) or not cap_src.startswith("data:image") or "," not in cap_src:
                    raise ValueError
                raw = base64.b64decode(cap_src.split(",", 1)[1], validate=True)
                text = ocr_bytes(raw, expected_len=6, alnum_only=True)
                if isinstance(text, str) and len(text) == 6 and text.isdigit():
                    return text
            except Exception:
                pass
            if n < attempts:
                try:
                    if not self._click_unique_refresh(page):
                        return None
                except Exception:
                    return None
        return None

    def submit_credentials_once(self, page) -> None:
        try:
            page.wait_for_selector(
                "input[name='__reCaptcha']", state="visible", timeout=15000
            )
            inputs = page.locator("input")
            layout = []
            for index in range(inputs.count()):
                field = inputs.nth(index)
                if not field.is_visible():
                    continue
                field_type = (field.get_attribute("type") or "text").lower()
                if field_type == "checkbox":
                    continue
                box = field.bounding_box()
                if box is None:
                    raise ScbLoginError("登入欄位無法安全確認；未送出登入")
                layout.append(
                    (
                        box["y"],
                        field_type,
                        field.get_attribute("name") or "",
                        field.get_attribute("maxlength"),
                        field,
                    )
                )
            layout.sort(key=lambda item: item[0])
            if (
                len(layout) != 4
                or tuple(item[1] for item in layout) != ("text", "password", "password", "tel")
                or layout[3][2] != "__reCaptcha"
                or any(not item[4].is_visible() or not item[4].is_enabled() for item in layout)
            ):
                raise ScbLoginError("登入欄位無法安全確認；未送出登入")

            for item, value in zip(
                layout[:3],
                (self.creds.national_id, self.creds.username, self.creds.password),
                strict=True,
            ):
                self._keyboard_fill(page, item[4], value)
            captcha = self._ocr_captcha(page, max_attempts=5)
            if not captcha:
                raise ScbLoginError("無法安全辨識驗證碼；未送出登入")
            self._keyboard_fill(page, layout[3][4], captcha)

            candidates = page.locator("button[type='submit']")
            eligible = []
            for index in range(candidates.count()):
                candidate = candidates.nth(index)
                if (
                    candidate.is_visible()
                    and candidate.is_enabled()
                    and " ".join(candidate.inner_text().split()) == "登入"
                    and "b-bg-green-d" in (candidate.get_attribute("class") or "").split()
                ):
                    eligible.append(candidate)
            if len(eligible) != 1:
                raise ScbLoginError("找不到唯一且可操作的登入按鈕；未送出登入")
            button = eligible[0]
            stale_captcha, stale_explicit, stale_other = self._visible_alert_state(page)
            if stale_explicit or stale_other:
                raise ScbLoginError("登入頁已有未解決提示；未送出登入")
        except ScbLoginError:
            raise
        except Exception:
            raise ScbLoginError("登入欄位無法安全填寫；未送出登入") from None

        try:
            button.click(timeout=8000)
        except Exception:
            raise ScbLoginError("登入送出狀態不明；禁止自動重試") from None

        try:
            for _ in range(30):
                page.wait_for_timeout(1000)
                if self._logged_in(page):
                    return
                modal_visible = False
                for scope in self._page_scopes(page):
                    for selector in (".modal.show", "[role='dialog']"):
                        blockers = scope.locator(selector)
                        if any(
                            blockers.nth(index).is_visible()
                            for index in range(blockers.count())
                        ):
                            modal_visible = True
                captcha, explicit, other = self._visible_alert_state(page)
                if explicit:
                    return
                if (stale_captcha or captcha) and (modal_visible or other):
                    raise ScbLoginError("登入送出後出現衝突狀態；禁止自動重試")
                if stale_captcha:
                    if not captcha:
                        stale_captcha = False
                elif captcha:
                    return
                if modal_visible or other:
                    return
        except Exception:
            raise ScbLoginError("登入送出後狀態無法安全確認；禁止自動重試") from None
        if stale_captcha:
            raise ScbLoginError("登入送出後沒有新的驗證碼結果；禁止自動重試")


    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        """SCB collect：dashboard + 信用卡綜覽 + 每張卡消費明細 + 帳單查詢 dump。

        2026-06-12 v3 (使用者指正「我有渣打信用卡」+ 要明細):
          1. dashboard text dump (home_text)
          2. 點「信用卡綜覽」→ 攔 crditAcctList API → dump card_text
          3. 依序點每張卡「消費明細」→ dump cards_detail[]
          4. 點「帳單查詢」→ dump bill_text
        """
        out: dict = {}
        page.wait_for_timeout(8000)

        debug_dir = _debug_dir()
        with contextlib.suppress(Exception):
            page.screenshot(path=str(debug_dir / "00_home.png"), full_page=True)

        out["initial_url"] = page.url
        try:
            out["title"] = page.title()
        except Exception:
            out["title"] = ""

        try:
            txt = page.evaluate("() => (document.body.innerText || '').slice(0, 15000)") or ""
        except Exception:
            txt = ""
        out["home_text"] = txt
        _log(f"[scb][collect] home text_len={len(txt)}")

        # === A. 點「信用卡綜覽」進信用卡頁 ===
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
        page.wait_for_timeout(8000)
        with contextlib.suppress(Exception):
            page.screenshot(path=str(debug_dir / "01_credit_overview.png"), full_page=True)
        out["card_url"] = page.url
        try:
            out["card_text"] = page.evaluate("() => (document.body.innerText || '').slice(0, 15000)") or ""
        except Exception:
            out["card_text"] = ""
        _log(f"[scb][collect] card 頁 url={out['card_url']} text_len={len(out['card_text'])}")

        # === B. 抓全部「消費明細」按鈕（DOM 全渲染）— 用 bbox dedup ===
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
                page.mouse.click(btn["x"] + btn["w"] / 2, btn["y"] + btn["h"] / 2)
                page.wait_for_timeout(8000)
                cur_url = page.url
                if "expense" not in cur_url:
                    _log(f"[scb][collect] 卡 {i+1} URL 未跳 expense ({cur_url[:80]})，跳過")
                    continue

                # 找「查詢」BUTTON 的 bbox（限定 button tag，避開 wrapper）
                # 用 mouse.click 觸發真實 form submit
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
                    page.mouse.click(query_btn["x"] + query_btn["w"] / 2,
                                     query_btn["y"] + query_btn["h"] / 2)
                    _log(f"[scb][collect] 卡 {i+1} mouse.click 查詢 done")
                    page.wait_for_timeout(8000)  # 等 API + 渲染表格

                with contextlib.suppress(Exception):
                    page.screenshot(path=str(debug_dir / f"02_card_{i+1}_detail.png"), full_page=True)
                detail = {"card_index": i + 1, "url": page.url}
                try:
                    detail["text"] = page.evaluate("() => (document.body.innerText || '').slice(0, 25000)") or ""
                except Exception:
                    detail["text"] = ""
                _log(f"[scb][collect] 卡 {i+1} url={detail['url']} text_len={len(detail['text'])}")
                cards_detail.append(detail)

                # 回信用卡綜覽
                if i + 1 < len(consumption_btns):
                    page.evaluate("""() => {
                        for (const el of document.querySelectorAll('a, button, span, div, li')) {
                            if (el.offsetParent === null) continue;
                            const t = (el.textContent || '').trim();
                            if (t !== '信用卡綜覽') continue;
                            try { el.click(); return; } catch (e) {}
                        }
                    }""")
                    page.wait_for_timeout(6000)
                    # 重新抓 dedup btns
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
                _log(f"[scb][collect] 卡 {i+1} 例外: {e}")

        out["cards_detail"] = cards_detail

        # === D. 點「帳單查詢」 ===
        try:
            page.evaluate("""() => {
                for (const el of document.querySelectorAll('a, button, span, div, li')) {
                    if (el.offsetParent === null) continue;
                    const t = (el.textContent || '').trim();
                    if (t !== '信用卡綜覽') continue;
                    try { el.click(); return; } catch (e) {}
                }
            }""")
            page.wait_for_timeout(6000)
        except Exception:
            pass

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
        page.wait_for_timeout(8000)
        with contextlib.suppress(Exception):
            page.screenshot(path=str(debug_dir / "03_bill_query.png"), full_page=True)
        out["bill_url"] = page.url
        try:
            out["bill_text"] = page.evaluate("() => (document.body.innerText || '').slice(0, 20000)") or ""
        except Exception:
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
