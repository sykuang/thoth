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
    Step 3: page.evaluate 驗 length；長度不符直接 abort 不送 login（保護帳號）
    Step 4: click 登入鈕 → 等 redirect
    Step 5: 偵測 OTP / 簡訊驗證碼頁（送出後若跳, raise 中止由使用者人工處理）

  ⚠️ 鐵律 max_attempts=1 — 失敗 raise LinebankLoginError，絕不重打
     （LINE Bank 客戶為純位元銀行用戶, 鎖帳號代價極高）

  Collect 流程（已完成 2026-06-14）:
    - dismiss 登入後「確定」modal → goto /transaction
    - 讀 <select> 帳戶清單 → 對每個帳戶 set value + click 查詢 → 攔 API
    - dump 全 api_responses（payables / transactions / informations）
    - persist_linebank() 解析存款餘額 + 交易明細 + 分期信貸推斷
    - LINE Bank 無信用卡產品, 跳過 credit card
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.base import BankCollectResult, BankCrawler, ResponseCollector
from backend.core.creds import LinebankCreds

BASE = "https://accessibility.linebank.com.tw/login"
LOGIN_PATH_HINT = "/login"


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


class LinebankLoginError(RuntimeError):
    """LINE Bank login 送出後失敗——立刻中止，絕不自動重打。"""


class LinebankOtpRequired(RuntimeError):
    """登入送出後跳 OTP / 簡訊驗證 — 第一輪不自動填，raise 中止由使用者人工處理。"""


class LinebankCrawler(BankCrawler):
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
            url = (page.url or "").lower()
            if "linebank.com.tw" not in url:
                return False
            # 精準 path 判斷：/login 結尾才當登入頁（/overview 等不算）
            path_tail = url.rstrip("/").split("/")[-1].split("?")[0]
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

    def _detect_otp(self, page) -> str | None:
        """偵測登入後是否跳 OTP / 簡訊驗證頁。回傳訊息或 None。"""
        try:
            txt = page.evaluate("() => (document.body.innerText || '').slice(0, 3000)") or ""
            otp_kw = [
                "簡訊驗證碼", "OTP", "一次性密碼", "驗證碼已傳送", "請輸入您收到的",
                "裝置驗證", "信任此裝置", "新裝置登入",
            ]
            for kw in otp_kw:
                if kw in txt:
                    # 取附近 80 字當錯誤訊息
                    idx = txt.find(kw)
                    return txt[max(0, idx - 20):idx + 80].strip()
            return None
        except Exception:
            return None

    def login(self, page) -> bool:
        """LINE Bank 登入 — 鐵律 max_attempts=1。"""
        page.wait_for_timeout(6000)  # 等 SPA hydrate
        _log(f"[linebank][login] 起始 url={page.url}")

        # session 復用偵測
        if self._logged_in(page):
            _log("[linebank][login] ✓ session 仍有效（已不在 login 頁），跳過 login")
            return True

        if LOGIN_PATH_HINT not in (page.url or ""):
            _log(f"[linebank][login] ⚠️ 不在 login 頁: {page.url}")

        # ─── Step 1: 等欄位出現 ───
        try:
            page.wait_for_selector("#nationalId", timeout=15000)
            page.wait_for_selector("#userId", timeout=5000)
            page.wait_for_selector("#pw", timeout=5000)
        except Exception as e:
            _log(f"[linebank][login] ❌ 等欄位 timeout: {e}")
            with contextlib.suppress(Exception):
                page.screenshot(path=str(_debug_dir() / "01_no_fields.png"), full_page=True)
            return False

        # ─── Step 2: fill 3 欄（純 keyboard type，仿 DBS 策略）───
        # 為何不用 page.fill：React controlled input + onChange 可能吞 batch 填值
        # 策略：triple-click 選整行 → Backspace 清 → keyboard.type 一字一字打
        creds_map = [
            ("#nationalId", self.creds.national_id, "national_id"),
            ("#userId",     self.creds.user_code,   "user_code"),
            ("#pw",         self.creds.password,    "password"),
        ]
        try:
            for sel, val, _label in creds_map:
                loc = page.locator(sel)
                loc.click()
                page.wait_for_timeout(150)
                loc.click(click_count=3)
                page.wait_for_timeout(100)
                page.keyboard.press("Backspace")
                page.wait_for_timeout(100)
                page.keyboard.type(val, delay=60)
                page.wait_for_timeout(250)
        except Exception as e:
            _log(f"[linebank][login] ❌ keyboard type 失敗: {e}")
            return False

        # ─── Step 2.5: 驗 length 不符不送出 ───
        check = page.evaluate("""() => {
            const get = id => document.getElementById(id);
            return {
                n_len: (get('nationalId') || {value:''}).value.length,
                u_len: (get('userId')     || {value:''}).value.length,
                p_len: (get('pw')         || {value:''}).value.length,
            };
        }""") or {}
        _log(f"[linebank][login] keyboard type 後: {check}")

        targets = {
            "n_len": len(self.creds.national_id),
            "u_len": len(self.creds.user_code),
            "p_len": len(self.creds.password),
        }
        if any(check.get(k, 0) != v for k, v in targets.items()):
            _log(f"[linebank][login] ❌ fill 長度不符 (got={check}, target={targets})")
            _log("[linebank][login] 為保護帳號，**不送出 login**（純 fill 失敗，未累計密碼錯）")
            with contextlib.suppress(Exception):
                page.screenshot(path=str(_debug_dir() / "02_fill_failed.png"), full_page=True)
            return False

        # ─── Step 3: click 登入鈕 ───
        _log("[linebank][login] 送出 login")
        try:
            # LINE Bank 登入鈕：text="登入"（無 form submit, 直接 onClick）
            # 用 evaluate 找第一個可見 + textContent==='登入' 的 button
            clicked = page.evaluate("""() => {
                for (const b of document.querySelectorAll('button')) {
                    if (b.offsetParent === null) continue;
                    const t = (b.textContent || '').trim();
                    if (t === '登入' || t === '登入友善網路銀行') {
                        b.click();
                        return true;
                    }
                }
                return false;
            }""")
            if not clicked:
                _log("[linebank][login] ❌ 找不到 '登入' 按鈕")
                return False
        except Exception as e:
            _log(f"[linebank][login] ❌ click 登入鈕 失敗: {e}")
            return False

        # ─── Step 4: 等 redirect ───
        page.wait_for_timeout(10000)
        final_url = page.url or ""
        _log(f"[linebank][login] 送出後 url={final_url}")

        with contextlib.suppress(Exception):
            page.screenshot(path=str(_debug_dir() / "03_after_login.png"), full_page=True)

        # OTP / 裝置驗證
        otp_msg = self._detect_otp(page)
        if otp_msg:
            _log(f"[linebank][login] ⚠️ 偵測到 OTP 驗證: {otp_msg}")
            raise LinebankOtpRequired(
                f"LINE Bank 跳 OTP/裝置驗證（url={final_url}）: {otp_msg}\n"
                "第一輪不自動填，請使用者 headless=False 手動輸入後，session 會存進 user_data_dir，"
                "下次同機可跳過。",
            )

        if self._logged_in(page):
            _log(f"[linebank][login] ✅ 登入成功 → {final_url}")
            return True

        # 失敗：找錯誤訊息
        # 2026-06-18: 套 dbs sibling lesson — keyword whitelist 過濾正常 CTA
        err_msg = ""
        with contextlib.suppress(Exception):
            err_msg = page.evaluate(r"""() => {
                const ERROR_KW = /錯誤|不正確|失敗|鎖定|逾時|驗證碼|帳號不存在|重試|無效|invalid|error|fail|locked|expired/i;
                const sels = ['.error', '.alert', '[class*=error]', '[class*=Error]',
                              '[role=alert]', '[class*=alert]', '[class*=Alert]'];
                for (const sel of sels) {
                    for (const el of document.querySelectorAll(sel)) {
                        if (el.offsetParent === null) continue;
                        const t = (el.textContent || '').trim();
                        if (t && t.length < 200 && ERROR_KW.test(t)) return t;
                    }
                }
                return '';
            }""") or ""

        from backend.banks._login_debug import snapshot as _login_snapshot
        snap = _login_snapshot(page)
        msg = f"LINE Bank 登入失敗（url={final_url}）: {err_msg or '未知原因'}\n{snap}"
        _log(f"[linebank][login] ❌ {msg}")
        raise LinebankLoginError(msg)

    def _dismiss_post_login_modal(self, page) -> None:
        """登入後會跳一個「登入 確定」 modal，點「確定」進入正常頁面。

        不是「重複登入」modal（沒提示語），純粹是登入成功確認，因此走獨立 handler，
        不沾染 base.handle_dup_login_modal 的 logic。
        """
        try:
            clicked = page.evaluate("""() => {
                for (const b of document.querySelectorAll('button, a, [role=button]')) {
                    if (b.offsetParent === null) continue;
                    const t = (b.textContent || '').trim();
                    if (t === '確定' || t === '確認') {
                        b.click();
                        return true;
                    }
                }
                return false;
            }""")
            if clicked:
                _log("[linebank][collect] 已點掉登入後「確定」modal")
                page.wait_for_timeout(1500)
        except Exception as e:
            _log(f"[linebank][collect] dismiss post-login modal 失敗（忽略）: {e}")

    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        """LINE Bank collect 第二輪：點 menu 抓帳戶 + 交易明細。

        無障礙網銀首頁是 sitemap-like，**沒有 dashboard**，必須主動 navigate 到
        /transaction（帳戶交易明細查詢）才能拿到資料。

        步驟：
          0. dismiss 登入後「確定」modal
          1. dump 主首頁 home_text + nav_items（保留第一輪行為）
          2. goto /transaction → 攔截 API + screenshot
          3. 全 dump api_responses（給後續 parser 升 dedicated persist_linebank）

        LINE Bank 無信用卡產品，跳過 credit card。
        """
        out: dict = {}
        page.wait_for_timeout(4000)
        debug_dir = _debug_dir()

        # ─── Step 0: dismiss post-login modal ───
        self._dismiss_post_login_modal(page)

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
    except (LinebankLoginError, LinebankOtpRequired) as e:
        result = {"error": str(e)}
    print(json.dumps(result, ensure_ascii=False, indent=2)[:4000])
