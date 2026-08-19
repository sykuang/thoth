#!/usr/bin/env python3
"""LINE Bank (Connect Commercial Bank) personal e-banking crawler.

LINE Bank 連線商業銀行 accessibility.linebank.com.tw/login 個人網銀爬蟲。

2026-06-13 初版（dry probe + vision 驗證）：

  登入頁特徵（極友善）:
    URL: https://accessibility.linebank.com.tw/login
    - 純 SPA，無 frameset、無 iframe、form 是 React controlled inputs
    - 3 欄純帳密 + 1 記住身分證 checkbox：
        #nationalId  (type=text  name=nationalId, maxLen 10) → 身分證字號
        #userId      (type=text  name=userId,     maxLen 14) → 使用者代號
        #pw          (type=password name=pw,      maxLen 14) → 密碼
    - 登入鈕: button text="登入" / aria-label「登入友善網路銀行」
    - **無 CAPTCHA**！不用 OCR
    - 無「立即登入」前置 modal，page load 就在登入頁

  登入流程:
    Step 1: page goto → 等 6s SPA hydrate（React mount + lazy chunks）
    Step 2: triple-click + Backspace + keyboard.type 寫 3 欄（DBS 教訓：
            React controlled input 對 native setter + dispatchEvent 不認，必用真鍵盤）
    Step 3: 各欄 locator.input_value() 驗 length；不符直接 abort 不送 login
    Step 4: click 登入鈕 → 等 redirect
    Step 5: shared scoped checkpoints 處理 OTP / 登入成功通知

  ⚠️ 鐵律 max_attempts=1 — 失敗 raise LinebankLoginError，絕不重打
     （LINE Bank 客戶為純位元銀行用戶, 鎖帳號代價極高）

  Collect 流程（已完成 2026-06-14）:
    - shared SETTLE dismiss 登入後「確定」modal → goto /transaction
    - 讀 <select> 帳戶清單 → 對每個帳戶 set value + click 查詢 → 攔 API
    - dump 全 api_responses（payables / transactions / informations）
    - persist_linebank() 解析存款餘額 + 交易明細 + 分期信貸推斷
    - LINE Bank 無信用卡產品, 跳過 credit card
"""
from __future__ import annotations

import contextlib
import re
import sys
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.base import BankCollectResult, BankCrawler, ResponseCollector
from backend.core.card_bills import publish_card_bill_facts
from backend.core.creds import LinebankCreds
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointPhase,
    LoginCheckpointRule,
)

BASE = "https://accessibility.linebank.com.tw/login"


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


class LinebankLoginError(RuntimeError):
    """LINE Bank login 失敗——立刻中止，絕不自動重打。"""


class LinebankCrawler(BankCrawler):
    USES_SHARED_LOGIN_CHECKPOINTS: ClassVar[bool] = True
    CREDENTIAL_HOSTS = frozenset({"accessibility.linebank.com.tw"})

    def __init__(self):
        super().__init__(name="linebank")
        self.creds = LinebankCreds.load()

    def _host_filter(self) -> str:
        return "linebank.com.tw"

    def _logged_in(self, page) -> bool:
        """W (2026-06-17): positive signal 4 條件 AND（純 SPA，對齊 SCSB 鐵律）

        1) urlOk: linebank.com.tw 域內 + 不在 /login
        2) noLoginForm: #nationalId + #userId + #pw 都不可見
        3) lenOk: body innerText >= 500
        4) kw >= 2: 內銀區關鍵字命中 ≥ 2 個
        """
        try:
            current = urlparse(page.url or "")
            if (
                current.scheme.lower() != "https"
                or (current.hostname or "").lower() != "accessibility.linebank.com.tw"
                or current.port not in (None, 443)
                or current.username is not None
                or current.password is not None
            ):
                return False
            # 精準 path 判斷：/login 結尾才當登入頁（/overview 等不算）
            path_tail = (current.path or "").rstrip("/").split("/")[-1].lower()
            if path_tail == "login":
                return False

            ok = page.evaluate("""
                () => {
                  const visible = (e) => {
                    if (!e) return false;
                    const r = e.getBoundingClientRect();
                    const cs = getComputedStyle(e);
                    return !!(r.width || r.height || e.getClientRects().length)
                      && cs.display !== 'none' && cs.visibility !== 'hidden';
                  };
                  const noLoginForm = !visible(document.querySelector('#nationalId'))
                    && !visible(document.querySelector('#userId'))
                    && !visible(document.querySelector('#pw'));
                  const body = document.body && document.body.innerText || '';
                  const lenOk = body.length >= 500;
                  const KW = ['登出','Logout','帳戶總覽','帳戶明細','我的帳戶',
                              '存款','轉帳','台幣','外幣','貸款','繳費',
                              '個人設定','安全','LINE Bank','一般帳戶','分期'];
                  const kw = KW.filter(k => body.includes(k)).length;
                  return noLoginForm && lenOk && kw >= 2;
                }
            """)
            return bool(ok)
        except Exception:
            return False

    def login(self, page) -> bool:
        return self._shared_login(page)

    def prepare_login_page(self, page) -> None:
        page.wait_for_timeout(6000)

    def is_authenticated(self, page) -> bool:
        return self._logged_in(page)

    def login_checkpoint_rules(self) -> tuple[LoginCheckpointRule, ...]:
        all_phases = tuple(CheckpointPhase)
        post = (CheckpointPhase.POST_SUBMIT, CheckpointPhase.POST_SUBMIT_SETTLE)
        return (
            LoginCheckpointRule(
                name="linebank-otp-required",
                bank="linebank",
                phases=all_phases,
                kind=CheckpointKind.OTP_REQUIRED,
                container_selector=".modal.show",
                required_body_pattern=re.compile(
                    r"^[\s\S]*(?:簡訊驗證碼|OTP|一次性密碼|驗證碼已傳送|"
                    r"請輸入您收到的簡訊驗證碼|裝置驗證|信任此裝置|新裝置登入)[\s\S]*$"
                ),
            ),
            LoginCheckpointRule(
                name="linebank-login-success-notice",
                bank="linebank",
                phases=post,
                kind=CheckpointKind.DISMISSIBLE_NOTICE,
                container_selector=".modal.show",
                action_texts=("確定",),
                required_body_pattern=re.compile(r"^\s*登入\s*確定\s*$"),
                max_actions=1,
            ),
            LoginCheckpointRule(
                name="linebank-unknown-modal",
                bank="linebank",
                phases=all_phases,
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector=".modal.show",
            ),
            LoginCheckpointRule(
                name="linebank-login-form-still-visible",
                bank="linebank",
                phases=post,
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector="#nationalId",
            ),
        )

    def submit_credentials_once(self, page) -> None:
        fields = (
            ("#nationalId", self.creds.national_id, 15000),
            ("#userId", self.creds.user_code, 5000),
            ("#pw", self.creds.password, 5000),
        )
        try:
            for selector, _value, timeout in fields:
                page.wait_for_selector(selector, state="visible", timeout=timeout)

            for selector, value, _timeout in fields:
                locator = page.locator(selector)
                if locator.count() != 1:
                    raise LinebankLoginError("登入欄位無法安全填寫；未送出登入")
                locator.click()
                page.wait_for_timeout(150)
                locator.click(click_count=3)
                page.wait_for_timeout(100)
                page.keyboard.press("Backspace")
                page.wait_for_timeout(100)
                page.keyboard.type(value, delay=60)
                page.wait_for_timeout(250)
                if len(locator.input_value()) != len(value):
                    raise LinebankLoginError("登入欄位輸入長度不符；未送出登入")
        except LinebankLoginError:
            raise
        except Exception:
            raise LinebankLoginError("登入欄位無法安全填寫；未送出登入") from None

        try:
            buttons = page.locator("button")
            matches = []
            for index in range(buttons.count()):
                button = buttons.nth(index)
                if not button.is_visible() or not button.is_enabled():
                    continue
                text = " ".join(button.inner_text().split())
                aria_label = button.get_attribute("aria-label")
                if text in {"登入", "登入友善網路銀行"} or aria_label == "登入友善網路銀行":
                    matches.append(button)
        except Exception:
            raise LinebankLoginError("無法安全確認登入按鈕；未送出登入") from None
        if len(matches) != 1:
            raise LinebankLoginError("找不到唯一且可操作的登入按鈕；未送出登入")

        try:
            matches[0].click(timeout=8000)
        except Exception:
            raise LinebankLoginError("登入送出狀態不明；禁止自動重試") from None

        try:
            page.wait_for_timeout(10000)
            for _ in range(20):
                page.wait_for_timeout(1000)
                if self._logged_in(page):
                    return
                modals = page.locator(".modal.show")
                if any(modals.nth(index).is_visible() for index in range(modals.count())):
                    return
                login_fields = page.locator("#nationalId")
                if any(
                    login_fields.nth(index).is_visible()
                    for index in range(login_fields.count())
                ):
                    return
        except Exception:
            return

    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        """LINE Bank collect 第二輪：點 menu 抓帳戶 + 交易明細。

        無障礙網銀首頁是 sitemap-like，**沒有 dashboard**，必須主動 navigate 到
        /transaction（帳戶交易明細查詢）才能拿到資料。

        步驟：
          1. dump 主首頁 home_text + nav_items（保留第一輪行為）
          2. goto /transaction → 攔截 API + screenshot
          3. 全 dump api_responses（給後續 parser 升 dedicated persist_linebank）

        LINE Bank 無信用卡產品，跳過 credit card。
        """
        out: dict = {}
        page.wait_for_timeout(4000)
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
        _log(f"[linebank][collect] home text_len={len(txt)}")

        # dump 主 page 內所有可見 nav 元素（找選單）
        try:
            nav_items = page.evaluate("""() => {
                const out = [];
                for (const el of document.querySelectorAll('a, button, [role=button], [role=link]')) {
                    if (el.offsetParent === null) continue;
                    const t = (el.textContent || '').trim();
                    if (!t || t.length > 30) continue;
                    const r = el.getBoundingClientRect();
                    out.push({
                        tag: el.tagName, text: t, href: el.href || '',
                        x: r.x, y: r.y, w: r.width, h: r.height,
                    });
                }
                return out;
            }""")
        except Exception:
            nav_items = []
        out["nav_items"] = nav_items[:80]
        _log(f"[linebank][collect] nav items: {len(nav_items)}（含 menu/button）")

        # ─── Step 2: 進「帳戶交易明細查詢」 /transaction ───
        # 無障礙版本要點 menu link, SPA 內部 routing
        _log("[linebank][collect] → goto /transaction（帳戶交易明細查詢）")
        try:
            page.goto("https://accessibility.linebank.com.tw/transaction", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(6000)  # 等 SPA fetch
        except Exception as e:
            _log(f"[linebank][collect] goto /transaction 失敗: {e}")

        with contextlib.suppress(Exception):
            page.screenshot(path=str(debug_dir / "10_transaction.png"), full_page=True)

        try:
            txn_url = page.url
            txn_text = page.evaluate("() => (document.body.innerText || '').slice(0, 20000)") or ""
        except Exception:
            txn_url, txn_text = "", ""
        out["transaction_url"] = txn_url
        out["transaction_text"] = txn_text
        _log(f"[linebank][collect] transaction url={txn_url} text_len={len(txn_text)}")

        # ─── Step 2.5: 讀 <select> 帳戶清單 → 對每個帳戶選 → 查詢 ───
        # 無障礙版必須先在 dropdown 選一個帳戶, 點「查詢」才會 fetch 該帳戶餘額 + 交易
        accounts_queried: list[dict] = []
        try:
            select_info = page.evaluate("""() => {
                const sel = document.querySelector('select');
                if (!sel) return {error: 'no_select_found'};
                const opts = [];
                for (const o of sel.options) {
                    const t = (o.textContent || '').trim();
                    const v = o.value;
                    // 跳過 placeholder option (value="" 或 text 是「請選擇查詢帳戶」)
                    if (!v || v === '' || /^請選擇/.test(t)) continue;
                    opts.push({value: v, text: t});
                }
                return {opts};
            }""")
        except Exception as e:
            select_info = {"error": str(e)}
            _log(f"[linebank][collect] 讀 select 失敗: {e}")

        opts_raw = (select_info or {}).get("opts") or []
        # 強制 type narrow：每個 opt 預期 dict[str, str]
        opts: list[dict] = [o for o in opts_raw if isinstance(o, dict)]
        _log(f"[linebank][collect] dropdown 有 {len(opts)} 個帳戶: {opts}")
        out["account_options"] = opts

        for i, opt in enumerate(opts):
            opt_value = str(opt.get("value", ""))
            opt_text = str(opt.get("text", ""))
            _log(f"[linebank][collect] → 查詢帳戶 [{i+1}/{len(opts)}] value={opt_value!r} text={opt_text!r}")
            try:
                # 用 JS 直接 set <select>.value 並 dispatch change/input（React onChange 認得）
                page.evaluate("""(value) => {
                    const sel = document.querySelector('select');
                    if (!sel) return false;
                    sel.value = value;
                    sel.dispatchEvent(new Event('change', {bubbles: true}));
                    sel.dispatchEvent(new Event('input',  {bubbles: true}));
                    return true;
                }""", opt_value)
                page.wait_for_timeout(500)

                # 點「查詢」按鈕
                clicked = page.evaluate("""() => {
                    for (const b of document.querySelectorAll('button')) {
                        if (b.offsetParent === null) continue;
                        const t = (b.textContent || '').trim();
                        if (t === '查詢') { b.click(); return true; }
                    }
                    return false;
                }""")
                if not clicked:
                    _log(f"[linebank][collect] ⚠️ 帳戶 {opt_value} 沒找到「查詢」按鈕")
                    continue
                page.wait_for_timeout(6000)  # 等 API + 表格 render

                with contextlib.suppress(Exception):
                    page.screenshot(
                        path=str(debug_dir / f"11_account_{i+1}_{opt_value.replace('-','')[:12]}.png"),
                        full_page=True,
                    )

                acct_text = page.evaluate("() => (document.body.innerText || '').slice(0, 25000)") or ""
                accounts_queried.append({
                    "option": opt,
                    "url": page.url,
                    "text": acct_text,
                })
                _log(f"[linebank][collect] 帳戶 {opt_value} 查詢後 text_len={len(acct_text)}")
            except Exception as e:
                _log(f"[linebank][collect] 查詢帳戶 {opt_value!r} 失敗: {e}")

        out["accounts_queried"] = accounts_queried

        # ─── Step 3: dump 全 API responses（為 dedicated parser 鋪路）───
        out["final_url"] = page.url
        out["_all_endpoints"] = sorted({h.endpoint for h in collector.hits if h.resp_json})

        api_responses: dict = {}
        for h in collector.hits:
            if h.resp_json is None:
                continue
            api_responses.setdefault(h.endpoint, []).append({
                "url": h.url, "method": h.method, "status": h.status,
                "resp": h.resp_json, "req_body": h.req_body,
            })
        out["api_responses"] = api_responses
        publish_card_bill_facts(out, [])
        _log(f"[linebank][collect] dump {len(api_responses)} 個 endpoint 的 resp_json")
        _log(f"[linebank][collect] 攔到 {len(out['_all_endpoints'])} 個 endpoint: {out['_all_endpoints'][:20]}")
        return BankCollectResult(**out)


def _debug_dir() -> Path:
    from backend.core.store import _data_root
    d = _data_root() / "linebank_collect"
    d.mkdir(parents=True, exist_ok=True)
    return d


if __name__ == "__main__":
    import json
    crawler = LinebankCrawler()
    try:
        result = crawler.run(login_url=BASE, headless=False)
    except LinebankLoginError as e:
        result = {"error": str(e)}
    print(json.dumps(result, ensure_ascii=False, indent=2)[:4000])
