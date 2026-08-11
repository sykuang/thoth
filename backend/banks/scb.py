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

  ⚠️ 鐵律 max_attempts=1 — 失敗 raise ScbLoginError，絕不重打

  Collect 流程（已完成）:
    - collect() 點信用卡 menu, 抓 sharedCards + crditAcctList E2EE 帳單明細
    - persist_scb 解析 per-card 卡號 + 帳單列表（cards/billed/pending 全入庫）
    - 測試: tests/test_persist_scb_per_card.py, tests/test_persist_scb_consumption_detail.py
"""
from __future__ import annotations

import base64
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.base import BankCollectResult, BankCrawler, ResponseCollector
from backend.core.card_bills import publish_card_bill_facts
from backend.core.captcha import ocr_bytes
from backend.core.creds import ScbCreds

BASE = "https://ebank.standardchartered.com.tw/scb/public/login?lang=tw"
LOGIN_PATH_HINT = "/scb/public/login"


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


class ScbLoginError(RuntimeError):
    """SCB login 送出後失敗——立刻中止，絕不自動重打。"""


class ScbCrawler(BankCrawler):
    def __init__(self):
        super().__init__(name="scb")
        self.creds = ScbCreds.load()

    def _host_filter(self) -> str:
        return "standardchartered.com.tw"

    def _logged_in(self, page) -> bool:
        """W (2026-06-17): positive signal 4 條件 AND（純 SPA 動態欄位，對齊 SCSB 鐵律）

        1) urlOk: standardchartered.com.tw 域內 + 不在 /scb/public/login
        2) noLoginForm: __reCaptcha + 4 visible password/text input 都不在
           （SCB 欄位 name 每 reload 變，只能靠 __reCaptcha 這個固定 name + visible input 數量）
        3) lenOk: body innerText >= 500
        4) kw >= 2: 內銀區關鍵字命中 ≥ 2 個
        """
        try:
            url = (page.url or "").lower()
            if "standardchartered.com.tw" not in url or LOGIN_PATH_HINT in url:
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
                  // 登入頁有 __reCaptcha (固定 name) + 多個 type=password input
                  const captchaInput = document.querySelector('[name="__reCaptcha"]');
                  const visiblePwdCount = [...document.querySelectorAll('input[type="password"]')]
                    .filter(visible).length;
                  const noLoginForm = !visible(captchaInput) && visiblePwdCount === 0;
                  const body = document.body && document.body.innerText || '';
                  const lenOk = body.length >= 500;
                  const KW = ['登出','Logout','理財總覽','帳戶綜覽','親愛的客戶',
                              '存款','轉帳','信用卡','台幣','外幣','基金','投資',
                              '貸款','繳費','個人設定','安全','SCB','Standard Chartered'];
                  const kw = KW.filter(k => body.includes(k)).length;
                  return noLoginForm && lenOk && kw >= 2;
                }
            """)
            return bool(ok)
        except Exception:
            return False

    def _ocr_captcha(self, page, max_attempts=5):
        """從 captcha img 抽 base64 → OCR 6 碼純數字（送出前安全重試）。"""
        for n in range(1, max_attempts + 1):
            try:
                # 抽 captcha img 的 src（data:image base64）
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
                if not cap_src or not cap_src.startswith("data:image"):
                    _log(f"[scb][cap] 第 {n} 次抓 captcha src 失敗: {(cap_src or '')[:80]}")
                    continue
                b64 = cap_src.split(",", 1)[1]
                raw = base64.b64decode(b64)
                text = ocr_bytes(raw, expected_len=6, alnum_only=True)
                if text and len(text) == 6 and text.isdigit():
                    _log(f"[scb][cap] 第 {n} 次 OCR 成功: {text}")
                    return text
                _log(f"[scb][cap] 第 {n}/{max_attempts} 次 OCR 失敗（讀到 {text!r}），按「重新產生」")
                # 換圖
                try:
                    page.evaluate("""() => {
                        for (const btn of document.querySelectorAll('button, a')) {
                            const t = (btn.textContent || '').trim();
                            if (t === '重新產生') {
                                btn.click();
                                return true;
                            }
                        }
                        return false;
                    }""")
                    page.wait_for_timeout(1500)
                except Exception as e:
                    _log(f"[scb][cap] 換圖失敗: {e}")
            except Exception as e:
                _log(f"[scb][cap] OCR 例外: {e}")
        return None

    def login(self, page) -> bool:
        """渣打 SCB 登入 — 鐵律 max_attempts=1。

        2026-06-12 v2 session 復用：先試 goto dashboard URL，
        若 session 還在就跳過 login（省一次 login，避免 SCB rate limit）。
        """
        # ─── Step 0: 試 session 復用 — 直接訪問 dashboard ───
        try:
            page.goto("https://ebank.standardchartered.com.tw/scb/", timeout=15000)
            page.wait_for_timeout(5000)
            if self._logged_in(page):
                _log(f"[scb][login] ✓ session 復用成功，跳過 login → {page.url}")
                return True
            _log(f"[scb][login] session 失效，繼續走 login flow（url={page.url}）")
        except Exception as e:
            _log(f"[scb][login] session 試探失敗，走 login flow: {e}")

        # 若被導去 login 頁就 goto 一次正規 login URL
        if LOGIN_PATH_HINT not in (page.url or ""):
            with contextlib.suppress(Exception):
                page.goto(BASE, timeout=15000)

        page.wait_for_timeout(8000)  # 等 SPA 渲染
        _log(f"[scb][login] 起始 url={page.url}")

        if self._logged_in(page):
            _log("[scb][login] ✓ session 仍有效，跳過 login")
            return True

        # ─── Step 1: 等欄位出現 + 取 visible input 用 y-order 定位 ───
        try:
            page.wait_for_selector("input[name='__reCaptcha']", timeout=15000)
        except Exception as e:
            _log(f"[scb][login] ❌ 等欄位 timeout: {e}")
            return False

        # 找 4 個輸入欄（依 visible y-order，跳過 checkbox）
        layout = page.evaluate("""() => {
            const visible = [...document.querySelectorAll('input')]
                .filter(i => i.offsetParent !== null)
                .sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y);
            const out = [];
            for (const i of visible) {
                if (i.type === 'checkbox') continue;
                out.push({name: i.name, type: i.type, maxlen: i.maxLength});
            }
            return out;
        }""") or []
        _log(f"[scb][login] visible input 順序: {layout}")
        if len(layout) < 4:
            _log(f"[scb][login] ❌ 預期至少 4 個 input（id/user/pwd/captcha），實得 {len(layout)}")
            return False

        # 對應：[0]=身分證 (text), [1]=使用者名稱 (password), [2]=網銀密碼 (password), [3]=captcha (tel)
        id_name = layout[0]["name"]
        user_name = layout[1]["name"]
        pwd_name = layout[2]["name"]
        cap_name = layout[3]["name"]
        if cap_name != "__reCaptcha":
            _log(f"[scb][login] ⚠️ captcha name 異常: {cap_name}（預期 __reCaptcha）")

        # ─── Step 2: keyboard.type 填三欄（DBS 教訓）───
        try:
            for name, val in [(id_name, self.creds.national_id),
                              (user_name, self.creds.username),
                              (pwd_name, self.creds.password)]:
                loc = page.locator(f"input[name='{name}']")
                loc.click()
                page.wait_for_timeout(150)
                loc.click(click_count=3)  # triple-click 選文字
                page.wait_for_timeout(100)
                page.keyboard.press("Backspace")  # 清空
                page.wait_for_timeout(100)
                page.keyboard.type(val, delay=80)
                page.wait_for_timeout(300)
        except Exception as e:
            _log(f"[scb][login] ❌ keyboard type 失敗: {e}")
            return False

        # ─── Step 3: OCR captcha + 填入 ───
        cap_text = self._ocr_captcha(page, max_attempts=5)
        if not cap_text:
            _log("[scb][login] ❌ OCR 5 次都失敗，不送 login（保護帳號）")
            return False
        try:
            cap_loc = page.locator("input[name='__reCaptcha']")
            cap_loc.click()
            page.wait_for_timeout(150)
            cap_loc.click(click_count=3)
            page.wait_for_timeout(100)
            page.keyboard.press("Backspace")
            page.wait_for_timeout(100)
            page.keyboard.type(cap_text, delay=80)
            page.wait_for_timeout(300)
        except Exception as e:
            _log(f"[scb][login] ❌ captcha fill 失敗: {e}")
            return False

        # ─── Step 4: 驗 length 不對就 abort ───
        check = page.evaluate(
            """(args) => {
                const get = (name) => {
                    const el = document.querySelector(`input[name='${name}']`);
                    return el ? el.value.length : -1;
                };
                return {
                    id_len: get(args.id_name),
                    user_len: get(args.user_name),
                    pwd_len: get(args.pwd_name),
                    cap_len: get('__reCaptcha'),
                };
            }""",
            {"id_name": id_name, "user_name": user_name, "pwd_name": pwd_name},
        ) or {}
        _log(f"[scb][login] fill 後: {check}")
        targets = {
            "id_len": len(self.creds.national_id),
            "user_len": len(self.creds.username),
            "pwd_len": len(self.creds.password),
            "cap_len": 6,
        }
        for k, want in targets.items():
            if check.get(k, 0) != want:
                _log(f"[scb][login] ❌ {k}={check.get(k)} ≠ {want}，不送 login（保護帳號）")
                with contextlib.suppress(Exception):
                    page.screenshot(path=str(_debug_dir() / "02_fill_failed.png"), full_page=True)
                return False

        # ─── Step 5: click 登入鈕 ───
        _log(f"[scb][login] 送出 login (captcha={cap_text})")
        clicked = page.evaluate("""() => {
            for (const btn of document.querySelectorAll('button')) {
                if (btn.type !== 'submit') continue;
                const cls = (btn.className || '').toString();
                if (cls.includes('b-bg-green-d') && (btn.textContent || '').trim() === '登入') {
                    btn.click();
                    return {ok: true, cls: cls.slice(0, 60)};
                }
            }
            return {ok: false, error: 'login_btn_not_found'};
        }""")
        _log(f"[scb][login] click 登入: {clicked}")
        if not clicked.get("ok"):
            _log("[scb][login] ❌ 找不到登入鈕")
            return False

        # ─── Step 6: 等 redirect（poll 直到 URL 變化或 timeout）───
        for _ in range(30):  # 最多 30 秒
            page.wait_for_timeout(1000)
            if LOGIN_PATH_HINT not in (page.url or ""):
                break
        final_url = page.url or ""
        _log(f"[scb][login] 送出後 url={final_url}")

        # ─── Step 6.5: 處理「重複登入」modal（鐵律：直接踢）───
        # 渣打 modal: 「您可能先前未正常登出或已經在別台裝置登入...〔確定登入〕」
        # 此 modal 出現 = 帳密已驗證通過（by-design 非密碼錯，類同 taishin/fubon dup-login）
        # poll 最多 30s 等 modal 出現或 URL 變化
        dup_modal: dict = {}
        for _ in range(30):
            if LOGIN_PATH_HINT not in (page.url or ""):
                break  # URL 已換 = 不會有 modal 了
            dup_modal = page.evaluate("""() => {
                for (const el of document.querySelectorAll('div, section, dialog, [role=dialog]')) {
                    if (el.offsetParent === null) continue;
                    const t = (el.textContent || '').slice(0, 500);
                    if (t.includes('未正常登出') || t.includes('已經在別台裝置') || t.includes('其他裝置將會被登出')) {
                        return {found: true, preview: t.slice(0, 200)};
                    }
                }
                return {found: false};
            }""") or {}
            if dup_modal.get("found"):
                break
            page.wait_for_timeout(1000)
        if dup_modal.get("found"):
            _log(f"[scb][login] 偵測到重複登入 modal: {dup_modal.get('preview', '')[:80]}")
            _log("[scb][login] 偵測到重複登入,直接踢掉前一個 session")
            try:
                confirmed = page.evaluate("""() => {
                    for (const btn of document.querySelectorAll('button, a')) {
                        if (btn.offsetParent === null) continue;
                        const t = (btn.textContent || '').trim();
                        if (t === '確定登入' || t === '確定' || t === '繼續登入') {
                            btn.click();
                            return {ok: true, text: t};
                        }
                    }
                    return {ok: false};
                }""")
                _log(f"[scb][login] click 〔確定登入〕: {confirmed}")
                # poll 等 dup-modal 處理後 URL 變化
                for _ in range(20):
                    page.wait_for_timeout(1000)
                    if LOGIN_PATH_HINT not in (page.url or ""):
                        break
                final_url = page.url or ""
                _log(f"[scb][login] dup-modal 處理後 url={final_url}")
            except Exception as e:
                _log(f"[scb][login] dup-modal click 失敗: {e}")

        with contextlib.suppress(Exception):
            page.screenshot(path=str(_debug_dir() / "03_after_login.png"), full_page=True)

        if self._logged_in(page):
            _log(f"[scb][login] ✅ 登入成功 → {final_url}")
            return True

        # 失敗：找錯誤訊息（給使用者 debug 用，不重打）
        # 2026-06-18: 套 dbs sibling lesson — keyword whitelist 過濾正常 CTA
        err_msg = ""
        is_server_busy = False
        try:
            err_msg = page.evaluate(r"""() => {
                const ERROR_KW = /錯誤|不正確|失敗|鎖定|逾時|驗證碼|帳號不存在|重試|無效|忙線|稍後|invalid|error|fail|locked|expired|busy|HIBERR|E\d{3,4}/i;
                const sels = ['.error', '.alert', '[class*=error]', '[class*=Error]',
                              '[role=alert]', '[class*=alert]', '[class*=Alert]',
                              '[class*=msg]', '[class*=Msg]'];
                for (const sel of sels) {
                    for (const el of document.querySelectorAll(sel)) {
                        if (el.offsetParent === null) continue;
                        const t = (el.textContent || '').trim();
                        if (t && t.length < 300 && t.length > 5 && ERROR_KW.test(t)) return t;
                    }
                }
                // 找 body 含特定 keyword
                const bodyText = (document.body.innerText || '').slice(0, 2000);
                const m = bodyText.match(/HIBERR_\d+[^\n]{0,100}/);
                if (m) return m[0];
                const m2 = bodyText.match(/E\d{3,4}[:：][^\n]{0,80}/);
                if (m2) return m2[0];
                return '';
            }""") or ""
            # 判斷是不是 server-busy（HIBERR_000010 系統忙線）
            if "HIBERR" in err_msg or "系統忙線" in err_msg or "稍後再試" in err_msg:
                is_server_busy = True
        except Exception:
            pass

        from backend.banks._login_debug import snapshot as _login_snapshot
        snap = _login_snapshot(page)
        if is_server_busy:
            msg = f"SCB server 忙線（非密碼錯，未累計失敗）: {err_msg}\n{snap}"
            _log(f"[scb][login] ⚠️ {msg}")
            _log("[scb][login] 銀行端忙線中,請稍後再試")
        else:
            msg = f"SCB 登入失敗（url={final_url}）: {err_msg or '未知原因'}\n{snap}"
            _log(f"[scb][login] ❌ {msg}")
        raise ScbLoginError(msg)


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
