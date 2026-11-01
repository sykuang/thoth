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
from backend.core.error_codes import error_step, with_error_code, exception_error_id, format_error_id
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

    def _logged_in(self, page, *, _auth_probe_ids=None) -> bool:
        """W (2026-06-17): positive signal 4 條件 AND（純 SPA，對齊 SCSB 鐵律）

        1) urlOk: linebank.com.tw 域內 + 不在 /login
        2) noLoginForm: #nationalId + #userId + #pw 都不可見
        3) lenOk: body innerText >= 500
        4) kw >= 2: 內銀區關鍵字命中 ≥ 2 個
        """
        if _auth_probe_ids is not None:
            _auth_probe_ids.clear()
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

            with error_step('LINEBANK-03-008-001'):
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
        except Exception as error:
            if _auth_probe_ids is not None:
                error_id = exception_error_id(error, bank='linebank')
                if error_id in ('LINEBANK-03-008-001',):
                    _auth_probe_ids[:] = [error_id]
            return False

    def login(self, page) -> bool:
        return self._shared_login(page)

    def prepare_login_page(self, page) -> None:
        with error_step('LINEBANK-01-001-001'):
            page.wait_for_timeout(6000)

    def is_authenticated(self, page) -> bool:
        auth_probe_ids = getattr(self, "_login_auth_probe_ids", None)
        return (
            self._logged_in(page) if auth_probe_ids is None
            else self._logged_in(page, _auth_probe_ids=auth_probe_ids)
        )

    def login_checkpoint_rules(self) -> tuple[LoginCheckpointRule, ...]:
        all_phases = tuple(CheckpointPhase)
        post = (CheckpointPhase.POST_SUBMIT, CheckpointPhase.POST_SUBMIT_SETTLE)
        return (
            LoginCheckpointRule(
                name="linebank-otp-required",
                error_id='LINEBANK-03-050-001',
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
                error_id='LINEBANK-03-800-001',
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
                error_id='LINEBANK-03-055-001',
                bank="linebank",
                phases=all_phases,
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector=".modal.show",
            ),
            LoginCheckpointRule(
                name="linebank-login-form-still-visible",
                error_id='LINEBANK-03-060-001',
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
                with error_step({'#nationalId': 'LINEBANK-02-009-001', '#userId': 'LINEBANK-02-023-001', '#pw': 'LINEBANK-02-037-001'}[selector]):
                    page.wait_for_selector(selector, state="visible", timeout=timeout)

            for selector, value, _timeout in fields:
                with error_step({'#nationalId': 'LINEBANK-02-010-001', '#userId': 'LINEBANK-02-024-001', '#pw': 'LINEBANK-02-038-001'}[selector]):
                    locator = page.locator(selector)
                with error_step({'#nationalId': 'LINEBANK-02-011-001', '#userId': 'LINEBANK-02-025-001', '#pw': 'LINEBANK-02-039-001'}[selector]):
                    if locator.count() != 1:
                        raise with_error_code(LinebankLoginError('登入欄位無法安全填寫；未送出登入'), {'#nationalId': 'LINEBANK-02-012-001', '#userId': 'LINEBANK-02-026-001', '#pw': 'LINEBANK-02-040-001'}[selector])
                with error_step({'#nationalId': 'LINEBANK-02-013-001', '#userId': 'LINEBANK-02-027-001', '#pw': 'LINEBANK-02-041-001'}[selector]):
                    locator.click()
                with error_step({'#nationalId': 'LINEBANK-02-014-001', '#userId': 'LINEBANK-02-028-001', '#pw': 'LINEBANK-02-042-001'}[selector]):
                    page.wait_for_timeout(150)
                with error_step({'#nationalId': 'LINEBANK-02-015-001', '#userId': 'LINEBANK-02-029-001', '#pw': 'LINEBANK-02-043-001'}[selector]):
                    locator.click(click_count=3)
                with error_step({'#nationalId': 'LINEBANK-02-016-001', '#userId': 'LINEBANK-02-030-001', '#pw': 'LINEBANK-02-044-001'}[selector]):
                    page.wait_for_timeout(100)
                with error_step({'#nationalId': 'LINEBANK-02-017-001', '#userId': 'LINEBANK-02-031-001', '#pw': 'LINEBANK-02-045-001'}[selector]):
                    page.keyboard.press("Backspace")
                with error_step({'#nationalId': 'LINEBANK-02-018-001', '#userId': 'LINEBANK-02-032-001', '#pw': 'LINEBANK-02-046-001'}[selector]):
                    page.wait_for_timeout(100)
                with error_step({'#nationalId': 'LINEBANK-02-019-001', '#userId': 'LINEBANK-02-033-001', '#pw': 'LINEBANK-02-047-001'}[selector]):
                    page.keyboard.type(value, delay=60)
                with error_step({'#nationalId': 'LINEBANK-02-020-001', '#userId': 'LINEBANK-02-034-001', '#pw': 'LINEBANK-02-048-001'}[selector]):
                    page.wait_for_timeout(250)
                with error_step({'#nationalId': 'LINEBANK-02-021-001', '#userId': 'LINEBANK-02-035-001', '#pw': 'LINEBANK-02-049-001'}[selector]):
                    if len(locator.input_value()) != len(value):
                        raise with_error_code(LinebankLoginError('登入欄位輸入長度不符；未送出登入'), {'#nationalId': 'LINEBANK-02-022-001', '#userId': 'LINEBANK-02-036-001', '#pw': 'LINEBANK-02-050-001'}[selector])
        except LinebankLoginError:
            raise
        except Exception as error:
            raise with_error_code(LinebankLoginError('登入欄位無法安全填寫；未送出登入'), exception_error_id(error, bank="linebank") or 'LINEBANK-02-001-001') from None

        try:
            with error_step('LINEBANK-02-002-001'):
                buttons = page.locator("button")
            matches = []
            with error_step('LINEBANK-02-051-001'):
                for index in range(buttons.count()):
                    with error_step('LINEBANK-02-003-001'):
                        button = buttons.nth(index)
                    with error_step('LINEBANK-02-004-001'):
                        if not error_step('LINEBANK-02-700-001')(lambda: button.is_visible())() or not error_step('LINEBANK-02-701-001')(lambda: button.is_enabled())():
                            continue
                    with error_step('LINEBANK-02-053-001'):
                        text = " ".join(button.inner_text().split())
                    with error_step('LINEBANK-02-052-001'):
                        aria_label = button.get_attribute("aria-label")
                    if text in {"登入", "登入友善網路銀行"} or aria_label == "登入友善網路銀行":
                        matches.append(button)
        except Exception as error:
            raise with_error_code(LinebankLoginError('無法安全確認登入按鈕；未送出登入'), exception_error_id(error, bank="linebank") or 'LINEBANK-02-005-001') from None
        if len(matches) != 1:
            raise with_error_code(LinebankLoginError('找不到唯一且可操作的登入按鈕；未送出登入'), 'LINEBANK-02-006-001')

        try:
            with error_step('LINEBANK-02-007-001'):
                matches[0].click(timeout=8000)
        except Exception as error:
            raise with_error_code(LinebankLoginError('登入送出狀態不明；禁止自動重試'), exception_error_id(error, bank="linebank") or 'LINEBANK-02-008-001') from None

        try:
            with error_step('LINEBANK-03-001-001'):
                page.wait_for_timeout(10000)
            for _ in range(20):
                with error_step('LINEBANK-03-002-001'):
                    page.wait_for_timeout(1000)
                if self._logged_in(page):
                    return
                with error_step('LINEBANK-03-003-001'):
                    modals = page.locator(".modal.show")
                with error_step('LINEBANK-03-004-001'):
                    if any(error_step('LINEBANK-03-702-001')(lambda: error_step('LINEBANK-03-703-001')(lambda: modals.nth(index))().is_visible())() for index in range(error_step('LINEBANK-03-704-001')(lambda: modals.count())())):
                        return
                with error_step('LINEBANK-03-005-001'):
                    login_fields = page.locator("#nationalId")
                with error_step('LINEBANK-03-006-001'):
                    if any(
                        error_step('LINEBANK-03-705-001')(lambda: error_step('LINEBANK-03-706-001')(lambda: login_fields.nth(index))().is_visible())()
                        for index in range(error_step('LINEBANK-03-707-001')(lambda: login_fields.count())())
                    ):
                        return
        except Exception as _action_error:
            with contextlib.suppress(Exception):
                _log(format_error_id(exception_error_id(_action_error, bank="linebank") or 'LINEBANK-03-007-001', bank="linebank"))
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
        with error_step('LINEBANK-04-001-001'):
            page.wait_for_timeout(4000)
        debug_dir = _debug_dir()

        with contextlib.suppress(Exception):
            page.screenshot(path=str(debug_dir / "00_home.png"), full_page=True)

        out["initial_url"] = page.url
        try:
            with error_step('LINEBANK-04-002-001'):
                out["title"] = page.title()
        except Exception as _action_error:
            with contextlib.suppress(Exception):
                _log(format_error_id(exception_error_id(_action_error, bank="linebank") or 'LINEBANK-07-001-001', bank="linebank"))
            out["title"] = ""

        try:
            with error_step('LINEBANK-04-003-001'):
                txt = page.evaluate("() => (document.body.innerText || '').slice(0, 15000)") or ""
        except Exception as _action_error:
            with contextlib.suppress(Exception):
                _log(format_error_id(exception_error_id(_action_error, bank="linebank") or 'LINEBANK-07-002-001', bank="linebank"))
            txt = ""
        out["home_text"] = txt
        _log(f"[linebank][collect] home text_len={len(txt)}")

        # dump 主 page 內所有可見 nav 元素（找選單）
        try:
            with error_step('LINEBANK-04-004-001'):
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
        except Exception as _action_error:
            with contextlib.suppress(Exception):
                _log(format_error_id(exception_error_id(_action_error, bank="linebank") or 'LINEBANK-07-003-001', bank="linebank"))
            nav_items = []
        out["nav_items"] = nav_items[:80]
        _log(f"[linebank][collect] nav items: {len(nav_items)}（含 menu/button）")

        # ─── Step 2: 進「帳戶交易明細查詢」 /transaction ───
        # 無障礙版本要點 menu link, SPA 內部 routing
        _log("[linebank][collect] → goto /transaction（帳戶交易明細查詢）")
        try:
            with error_step('LINEBANK-04-005-001'):
                page.goto("https://accessibility.linebank.com.tw/transaction", wait_until="domcontentloaded", timeout=30000)
            with error_step('LINEBANK-05-001-001'):
                page.wait_for_timeout(6000)  # 等 SPA fetch
        except Exception as e:
            with contextlib.suppress(Exception):
                _log(format_error_id(exception_error_id(e, bank="linebank") or 'LINEBANK-07-004-001', bank="linebank"))
            _log(f"[linebank][collect] goto /transaction 失敗: {type(e).__name__}")

        with contextlib.suppress(Exception):
            page.screenshot(path=str(debug_dir / "10_transaction.png"), full_page=True)

        try:
            txn_url = page.url
            with error_step('LINEBANK-04-006-001'):
                txn_text = page.evaluate("() => (document.body.innerText || '').slice(0, 20000)") or ""
        except Exception as _action_error:
            with contextlib.suppress(Exception):
                _log(format_error_id(exception_error_id(_action_error, bank="linebank") or 'LINEBANK-07-005-001', bank="linebank"))
            txn_url, txn_text = "", ""
        out["transaction_url"] = txn_url
        out["transaction_text"] = txn_text
        _log(f"[linebank][collect] transaction text_len={len(txn_text)}")

        # ─── Step 2.5: 讀 <select> 帳戶清單 → 對每個帳戶選 → 查詢 ───
        # 無障礙版必須先在 dropdown 選一個帳戶, 點「查詢」才會 fetch 該帳戶餘額 + 交易
        accounts_queried: list[dict] = []
        try:
            with error_step('LINEBANK-05-002-001'):
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
            with contextlib.suppress(Exception):
                _log(format_error_id(exception_error_id(e, bank="linebank") or 'LINEBANK-07-006-001', bank="linebank"))
            select_info = {"error": type(e).__name__}
            _log(f"[linebank][collect] 讀 select 失敗: {type(e).__name__}")

        opts_raw = (select_info or {}).get("opts") or []
        # 強制 type narrow：每個 opt 預期 dict[str, str]
        opts: list[dict] = [o for o in opts_raw if isinstance(o, dict)]
        _log(f"[linebank][collect] dropdown 有 {len(opts)} 個帳戶")
        out["account_options"] = opts

        for i, opt in enumerate(opts):
            opt_value = str(opt.get("value", ""))
            _log(f"[linebank][collect] → 查詢帳戶 [{i+1}/{len(opts)}]")
            try:
                # 用 JS 直接 set <select>.value 並 dispatch change/input（React onChange 認得）
                with error_step('LINEBANK-05-003-001'):
                    page.evaluate("""(value) => {
                    const sel = document.querySelector('select');
                    if (!sel) return false;
                    sel.value = value;
                    sel.dispatchEvent(new Event('change', {bubbles: true}));
                    sel.dispatchEvent(new Event('input',  {bubbles: true}));
                    return true;
                }""", opt_value)
                with error_step('LINEBANK-05-004-001'):
                    page.wait_for_timeout(500)

                # 點「查詢」按鈕
                with error_step('LINEBANK-05-005-001'):
                    clicked = page.evaluate("""() => {
                    for (const b of document.querySelectorAll('button')) {
                        if (b.offsetParent === null) continue;
                        const t = (b.textContent || '').trim();
                        if (t === '查詢') { b.click(); return true; }
                    }
                    return false;
                }""")
                if not clicked:
                    _log(f"[linebank][collect] ⚠️ 帳戶#{i+1} 沒找到「查詢」按鈕")
                    continue
                with error_step('LINEBANK-05-006-001'):
                    page.wait_for_timeout(6000)  # 等 API + 表格 render

                with contextlib.suppress(Exception):
                    page.screenshot(
                        path=str(debug_dir / f"11_account_{i+1}_{opt_value.replace('-','')[:12]}.png"),
                        full_page=True,
                    )

                with error_step('LINEBANK-05-007-001'):
                    acct_text = page.evaluate("() => (document.body.innerText || '').slice(0, 25000)") or ""
                accounts_queried.append({
                    "option": opt,
                    "url": page.url,
                    "text": acct_text,
                })
                _log(f"[linebank][collect] 帳戶#{i+1} 查詢後 text_len={len(acct_text)}")
            except Exception as e:
                with contextlib.suppress(Exception):
                    _log(format_error_id(exception_error_id(e, bank="linebank") or 'LINEBANK-07-007-001', bank="linebank"))
                _log(f"[linebank][collect] 查詢帳戶#{i+1} 失敗: {type(e).__name__}")

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
        with error_step('LINEBANK-07-008-001'):
            publish_card_bill_facts(out, [])
        _log(f"[linebank][collect] dump {len(api_responses)} 個 endpoint 的 resp_json")
        _log(f"[linebank][collect] 攔到 {len(out['_all_endpoints'])} 個 endpoint")
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
