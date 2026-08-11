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
    Step 5: captcha = OCR(#m1_captchaImage src 從 /B2C/captchaImage?timestamp=...)
            實測 3/3 OCR 命中（v3_captcha_t1=418862 等）
    Step 6: click #btnLogin2 (txnFrame 內，<a id="btnLogin2">登入</a>)

  ⚠️ 鐵律 max_attempts=1 — 失敗 raise FubonLoginError，絕不重打（會鎖帳號）
  ⚠️ 換 captcha 用「重新產生」連結，不是 .captcha-refresh
"""
from __future__ import annotations

import base64
import contextlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.base import BankCollectResult, BankCrawler, ResponseCollector
from backend.core.card_bills import make_card_bill_fact, publish_card_bill_facts
from backend.core.captcha import ocr_bytes
from backend.core.creds import TaipeiFubonCreds

BASE = "https://ebank.taipeifubon.com.tw/B2C/common/Index.faces"
PRE_LOGIN_HINT = "PreLogin.faces"
HEADER_LOGIN_BTN_ID = "header_form:header_login"  # 在 frame1，右上「登入」開 modal
GENERAL_LOGIN_TAB = "一般登入"
LOGIN_BTN_ID = "btnLogin2"  # 一般登入 form 的登入鈕（txnFrame 內）

# 一般登入 form 欄位（dry probe v2 揭示，vision 確認 label）
FIELD_M1_NATIONAL_ID = "m1_LJCHUYIFKV"  # 身分證 (maxlen=10)
FIELD_M1_USER_CODE   = "m1_VVYJVIJLIE"  # 使用者代碼 (maxlen=10，實況 XXX1234 = 7 碼)
FIELD_M1_PASSWORD    = "m1_ACXMQTRIBF"  # 密碼 (maxlen=16)
FIELD_M1_CAPTCHA     = "m1_userCaptcha"  # 6 碼純數字
CAPTCHA_IMG_ID       = "m1_captchaImage"  # 158×30 captcha img


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


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
    def __init__(self):
        super().__init__(name="fubon")
        self.creds = TaipeiFubonCreds.load()

    def _host_filter(self) -> str:
        return "taipeifubon.com"

    def _find_login_frame(self, page):
        """找 txnFrame (src 含 PreLogin.faces)。"""
        for f in page.frames:
            if PRE_LOGIN_HINT in (f.url or ""):
                return f
            if f.name == "txnFrame":
                return f
        return None

    def _find_header_frame(self, page):
        """找 frame1 (ContextFrame.faces, 含右上登入鈕)。"""
        for f in page.frames:
            if f.name == "frame1":
                return f
            if "ContextFrame" in (f.url or ""):
                return f
        return None

    def _logged_in(self, page) -> bool:
        """W (2026-06-17): positive signal 4 條件 AND（frameset 版，對齊 SCSB 鐵律）

        Fubon B2C 是 frameset，txnFrame 為主畫面 frame。

        1) urlOk: ebank.fubon.com 網域內
        2) noLoginForm: PreLogin.faces frame 已消失
        3) lenOk: 所有 frame innerText 合計 >= 500
        4) kw >= 2: 內銀區關鍵字命中 ≥ 2 個

        任一 fail → 視為未登入。
        """
        try:
            url = (page.url or "").lower()
            if "fubon" not in url:
                return False

            # noLoginForm: 任何 frame 還含 PreLogin.faces → 未登入
            for f in page.frames:
                if PRE_LOGIN_HINT in (f.url or ""):
                    return False

            # lenOk + kw（main page + 所有 frame）
            texts = []
            for f in [page, *list(page.frames)]:
                try:
                    txt = f.evaluate("() => document.body && document.body.innerText || ''")
                    if txt:
                        texts.append(txt)
                except Exception:
                    pass
            joined = "\n".join(texts)
            if len(joined) < 500:
                return False

            KW = (
                "帳戶總覽", "我的帳戶", "資產總額", "存款", "轉帳", "信用卡",
                "登出", "個人設定", "台幣", "外幣", "基金", "投資", "貸款",
                "繳費", "安全", "信託", "理財",
            )
            kw = sum(1 for k in KW if k in joined)
            return kw >= 2
        except Exception:
            return False

    def _ocr_captcha(self, frame, max_attempts=5):
        """從 #m1_captchaImage 抓 base64 → OCR 6 碼純數字（送出前安全重試）。"""
        for n in range(1, max_attempts + 1):
            try:
                cap_b64 = frame.evaluate("""() => {
                    const img = document.getElementById('m1_captchaImage');
                    if (!img) return null;
                    if (img.naturalWidth < 10) return null;
                    const canvas = document.createElement('canvas');
                    canvas.width = img.naturalWidth;
                    canvas.height = img.naturalHeight;
                    canvas.getContext('2d').drawImage(img, 0, 0);
                    return canvas.toDataURL('image/png').split(',')[1];
                }""")
                if not cap_b64:
                    _log(f"[fubon][cap] 第 {n} 次抓 captcha base64 失敗")
                    continue
                raw = base64.b64decode(cap_b64)
                text = ocr_bytes(raw, expected_len=6, alnum_only=True)
                if text and len(text) == 6 and text.isdigit():
                    _log(f"[fubon][cap] 第 {n} 次 OCR 成功: {text}")
                    return text
                _log(f"[fubon][cap] 第 {n}/{max_attempts} 次 OCR 失敗（讀到 {text!r}），換圖")
                # 換圖：找「重新產生」連結
                try:
                    frame.evaluate("""() => {
                        for (const el of document.querySelectorAll('a, button, span')) {
                            const t = (el.textContent || '').trim();
                            if (t === '重新產生' || t.includes('重新')) {
                                el.click(); return true;
                            }
                        }
                        // 退而求其次：reload captcha img src
                        const img = document.getElementById('m1_captchaImage');
                        if (img) img.src = '/B2C/captchaImage?timestamp=' + Date.now();
                        return false;
                    }""")
                    frame.evaluate("() => new Promise(r => setTimeout(r, 1500))")
                except Exception as e:
                    _log(f"[fubon][cap] 換圖失敗: {e}")
            except Exception as e:
                _log(f"[fubon][cap] OCR 失敗: {e}")
        return None

    def login(self, page) -> bool:
        """富邦 B2C 登入 — 鐵律 max_attempts=1。"""
        page.wait_for_timeout(12000)  # 等 frameset 全載
        _log(f"[fubon][login] 起始 url={page.url}")

        # session 復用偵測
        if self._logged_in(page):
            _log("[fubon][login] ✓ session 仍有效（無 PreLogin frame），跳過 login")
            return True

        # ─── Step 1: 點 frame1 的右上「登入」按鈕（開 modal）───
        header_frame = self._find_header_frame(page)
        if header_frame is None:
            _log("[fubon][login] 找不到 frame1（含右上登入鈕）")
            _log(f"  frames: {[(f.name, (f.url or '')[:80]) for f in page.frames]}")
            return False

        try:
            res = header_frame.evaluate(f"""() => {{
                const el = document.getElementById('{HEADER_LOGIN_BTN_ID}');
                if (!el) return {{ok: false, why: 'not-found'}};
                const r = el.getBoundingClientRect();
                const opts = {{bubbles: true, cancelable: true, view: window,
                              clientX: r.x + r.width/2, clientY: r.y + r.height/2}};
                ['mouseenter', 'mouseover', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(ev => {{
                    el.dispatchEvent(new MouseEvent(ev, opts));
                }});
                el.click();
                return {{ok: true}};
            }}""")
            if not res.get("ok"):
                _log(f"[fubon][login] 點右上「登入」失敗: {res}")
                return False
            _log("[fubon][login] ✓ 已點右上「登入」（開 modal）")
        except Exception as e:
            _log(f"[fubon][login] 點右上「登入」例外: {e}")
            return False

        page.wait_for_timeout(5000)  # 等 modal 開

        # ─── Step 2: 找 txnFrame（PreLogin form 載體）───
        login_frame = self._find_login_frame(page)
        if login_frame is None:
            _log("[fubon][login] 找不到 txnFrame")
            _log(f"  frames: {[(f.name, (f.url or '')[:80]) for f in page.frames]}")
            return False
        _log(f"[fubon][login] PreLogin frame: {login_frame.url[:100]}")

        # ─── Step 3: 切到「一般登入」分頁 ───
        try:
            tab_clicked = login_frame.evaluate("""() => {
                for (const a of document.querySelectorAll('a, span, div')) {
                    const t = (a.textContent || '').trim();
                    if (t !== '一般登入') continue;
                    if (a.offsetParent === null) continue;
                    if (a.tagName === 'DIV' && a.querySelector('a')) continue;
                    a.click();
                    return {ok: true, tag: a.tagName, id: a.id};
                }
                return {ok: false};
            }""")
            _log(f"[fubon][login] 「一般登入」tab click: {tab_clicked}")
            page.wait_for_timeout(2000)
        except Exception as e:
            _log(f"[fubon][login] 切「一般登入」分頁失敗: {e}")

        # ─── Step 4: 填 3 欄帳密 ───
        # 富邦 JSF 每次 page load 重新生成混淆 id (例如 m1_LJCHUYIFKV → m1_DTUZHFJAFO)
        # 不能 hardcode id，改用「visible input 出現順序」定位：
        #   [0] type=password maxlen=10  → 身分證
        #   [1] type=password maxlen=10  → 使用者代碼
        #   [2] type=password maxlen=16  → 密碼
        #   [3] type=text     maxlen=6   → captcha (這個 id 是固定的 m1_userCaptcha)
        #
        # W (2026-06-17): 為什麼用 page.evaluate 而非 page.fill?
        #   富邦 login 不給 password 欄位穩定 id (動態生成), 必須 runtime 掃所有
        #   type=password input + 按 y 座標排序才能對應 [0]=身分證 [1]=user_code [2]=password.
        #   page.fill('#xxx') 用不上, 只能 evaluate 內 querySelectorAll 找.
        #   也必須手動 dispatch input/change/blur 3 events — 富邦 React form
        #   不會聽 nativeSetter 設值, 一定要事件 trigger validation 才生效.
        #   captcha 欄位則回到 page.fill (id 固定, 走標準路徑).
        try:
            fill_result = login_frame.evaluate("""(creds) => {
                // 收集所有 visible password input (skip hidden)
                const pws = [];
                for (const el of document.querySelectorAll('input[type=password]')) {
                    if (el.offsetParent === null) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 1) continue;
                    pws.push({el: el, id: el.id, name: el.name, maxlen: el.maxLength, y: r.y});
                }
                // 依 y 座標排序（top-to-bottom）
                pws.sort((a, b) => a.y - b.y);

                if (pws.length < 3) {
                    return {ok: false, why: `only ${pws.length} visible password inputs`, found: pws.map(p => p.id)};
                }

                const setVal = (el, val) => {
                    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    nativeSetter.call(el, val);
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    el.dispatchEvent(new Event('blur', {bubbles: true}));
                };

                setVal(pws[0].el, creds.national_id);
                setVal(pws[1].el, creds.user_code);
                setVal(pws[2].el, creds.password);

                return {
                    ok: true,
                    national_id_field: pws[0].id,
                    user_code_field:   pws[1].id,
                    password_field:    pws[2].id,
                };
            }""", {
                "national_id": self.creds.national_id,
                "user_code": self.creds.user_code,
                "password": self.creds.password,
            })
            _log(f"[fubon][login] fill 3 欄結果: {fill_result}")

            if not fill_result.get("ok"):
                _log("[fubon][login] ❌ 填欄位失敗 → 中止（不送 login）")
                from backend.core.store import _data_root
                debug_dir = _data_root() / "fubon_collect"
                debug_dir.mkdir(parents=True, exist_ok=True)
                with contextlib.suppress(Exception):
                    page.screenshot(path=str(debug_dir / "fill_FAILED.png"), full_page=True)
                return False
        except Exception as e:
            _log(f"[fubon][login] 填欄位例外: {e}")
            return False

        # ─── Step 5: OCR captcha (送出前安全重試 5 次) ───
        captcha = self._ocr_captcha(login_frame, max_attempts=5)
        if not captcha:
            _log("[fubon][login] OCR 5 次都失敗，放棄（未送 login）")
            return False
        try:
            login_frame.fill(f"#{FIELD_M1_CAPTCHA}", captcha)
            page.wait_for_timeout(300)
        except Exception as e:
            _log(f"[fubon][login] 填 captcha 失敗: {e}")
            return False

        # ─── Step 6: 送出（max_attempts=1 鐵律）───
        _log(f"[fubon][login] 送出 login (captcha={captcha})")
        try:
            login_frame.click(f"#{LOGIN_BTN_ID}", timeout=8000)
        except Exception as e:
            _log(f"[fubon][login] click 登入鈕失敗: {e}")
            return False

        page.wait_for_timeout(10000)

        if self._logged_in(page):
            _log(f"[fubon][login] ✅ 登入成功 -> {page.url}")
            return True

        # 失敗 → dump 截圖 + 錯訊
        from backend.core.store import _data_root
        debug_dir = _data_root() / "fubon_collect"
        debug_dir.mkdir(parents=True, exist_ok=True)
        try:
            page.screenshot(path=str(debug_dir / "login_FAILED.png"), full_page=True)
            _log(f"[fubon][login] 失敗截圖: {debug_dir}/login_FAILED.png")
        except Exception:
            pass

        errs = []
        try:
            frame = self._find_login_frame(page) or page.main_frame
            errs = frame.evaluate(
                "(() => [...document.querySelectorAll('div,span,p,td')]"
                ".filter(e=>e.offsetParent!==null)"
                ".map(e=>(e.textContent||'').trim())"
                ".filter(t=>t && t.length<100 && /錯誤|不正確|失敗|鎖|無效|請|invalid|error/i.test(t))"
                ".slice(0,8))()",
            )
        except Exception:
            pass
        from backend.banks._login_debug import snapshot as _login_snapshot
        snap = _login_snapshot(page)
        msg = (
            f"富邦登入失敗。請檢查帳號、密碼是否正確。"
            f"\n  url={page.url}"
            f"\n  錯誤訊息: {errs}"
            f"\n  可能原因：(a) 驗證碼辨識錯誤 (b) 帳號或密碼錯誤 (c) 帳號已被鎖定"
            f"\n{snap}"
            # Internal policy: max_attempts=1, MUST NOT auto-retry. See wiki
            # concepts/taiwan-bank-login-retry-account-lockout-lesson.
        )
        _log(f"[fubon][login] ❌ {msg}")
        raise FubonLoginError(msg)

    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        """富邦 collect：信用卡 menu 在 txnFrame (CGEQU001_Home) carousel 全渲染。

        關鍵發現 (2026-06-12):
        - top menu 在 frame1，但 mega menu 內容 carousel 全部存在於 txnFrame 內
        - DOM 全部渲染（不靠 hover），視覺上只顯示一頁，但 querySelector 都拿得到
        - 走 txnFrame 直接定位「我的信用卡」/「帳務/繳款」等子項 click 即可
        """
        out: dict = {}
        page.wait_for_timeout(8000)

        from backend.core.store import _data_root
        debug_dir = _data_root() / "fubon_collect"
        debug_dir.mkdir(parents=True, exist_ok=True)

        with contextlib.suppress(Exception):
            page.screenshot(path=str(debug_dir / "00_home.png"), full_page=True)
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
            out["error"] = "txnFrame_not_found"
            return BankCollectResult(**out)

        # === Step 2: 在 txnFrame 找信用卡相關子項 (carousel 全渲染，offscreen 也存在) ===
        # 優先序：直接走「我的信用卡」進信用卡頁，或「帳務/繳款」進帳單查詢
        candidates = content_frame.evaluate("""() => {
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
            _log(f"  - {c['tag']} '{c['text']}' cls='{c['cls'][:30]}' visible={c['visible']} href='{c['href'][:80]}' onclick='{c['onclick'][:80]}'")

        # Telemetry 2026-06-18: 同時 dump 存款/帳戶相關 menu 候選 (給 cloud 看真實有哪些字)
        # 目的: 確認富邦 menu 用「帳戶總覽」「存款查詢」「我的帳戶」哪個字眼, 才能規劃 collect path
        # 同時 dump 所有 <a> visible text 前 100 條 (上限避免 result_summary 爆)
        deposit_audit = content_frame.evaluate(r"""() => {
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
        _log(f"[fubon][collect] [TELEMETRY] 存款相關 menu 候選 {len(deposit_audit)} 條:")
        for c in deposit_audit[:30]:
            _log(f"  - {c['tag']} '{c['text']}' visible={c['visible']} href='{c.get('href','')[:60]}'")

        if not candidates:
            out["error"] = "no_credit_card_items"
            return BankCollectResult(**out)

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
        _log(f"[fubon][collect] 選擇 target: tag={target['tag']} text='{target['text']}' visible={target['visible']} href='{target['href'][:80]}'")

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
        click_result = content_frame.evaluate("""(args) => {
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
        _log(f"[fubon][collect] click result: {click_result}")
        page.wait_for_timeout(6000)
        with contextlib.suppress(Exception):
            page.screenshot(path=str(debug_dir / "02_after_click.png"), full_page=True)

        # === Step 4.5: txnFrame 切換後重新抓 frame（URL 已換）===
        # 點完 <A> 後 txnFrame 會 navigate 到 CCCQU001_Home.faces
        page.wait_for_timeout(2000)
        for f in page.frames:
            url = f.url or ""
            name = f.name or ""
            if name == "txnFrame":
                content_frame = f
                break
        _log(f"[fubon][collect] 切換後 txnFrame url={content_frame.url[:100]}")

        # 立刻抓「我的信用卡」頁卡片清單 (CCCQU001_Home)
        cards_page_text = ""
        with contextlib.suppress(Exception):
            cards_page_text = content_frame.evaluate("() => document.body.innerText.slice(0, 10000)") or ""
        out["cards_page_text"] = cards_page_text
        out["cards_page_url"] = content_frame.url
        _log(f"[fubon][collect] cards 頁 text_len={len(cards_page_text)}")

        # === Step 4.6: 再 click「帳務查詢」進帳單明細頁 ===
        # 富邦右上吊牌 quick links: 帳務查詢 / 網路辦卡 / 申辦進度查詢
        # 必須找 <A> tag（LI 是裝飾外殼）
        bill_click = content_frame.evaluate("""() => {
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
        _log(f"[fubon][collect] 帳務查詢 click: {bill_click}")
        page.wait_for_timeout(6000)
        with contextlib.suppress(Exception):
            page.screenshot(path=str(debug_dir / "03_after_bill_click.png"), full_page=True)

        # === Step 4.7: 再切回 txnFrame 確認 URL ===
        page.wait_for_timeout(2000)
        for f in page.frames:
            if (f.name or "") == "txnFrame":
                content_frame = f
                break
        _log(f"[fubon][collect] 帳務查詢 click 後 txnFrame url={content_frame.url[:120]}")

        # === Step 4.8: 抓「繳款及額度查詢」頁的 text 後，先試點「帳單明細查詢」===
        # 帳務查詢頁有 sub-tabs: 繳款及額度查詢 / 帳單明細查詢 / 未出帳單消費明細 / 消費分析
        # 先抓額度頁，再嘗試切到帳單明細
        amount_page_text = ""
        with contextlib.suppress(Exception):
            amount_page_text = content_frame.evaluate("() => document.body.innerText.slice(0, 15000)") or ""
        out["amount_page_text"] = amount_page_text
        _log(f"[fubon][collect] amount 頁 text_len={len(amount_page_text)}")

        # 嘗試 click 帳單明細查詢
        billed_click = content_frame.evaluate("""() => {
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
        _log(f"[fubon][collect] 帳單明細查詢 click: {billed_click}")
        page.wait_for_timeout(6000)
        with contextlib.suppress(Exception):
            page.screenshot(path=str(debug_dir / "04_after_billed.png"), full_page=True)

        # 切回 txnFrame 抓帳單明細頁 text
        page.wait_for_timeout(2000)
        for f in page.frames:
            if (f.name or "") == "txnFrame":
                content_frame = f
                break
        billed_page_text = ""
        with contextlib.suppress(Exception):
            billed_page_text = content_frame.evaluate("() => document.body.innerText.slice(0, 20000)") or ""
        out["billed_page_text"] = billed_page_text
        out["billed_page_url"] = content_frame.url
        _log(f"[fubon][collect] billed 頁 url={content_frame.url[:120]} text_len={len(billed_page_text)}")

        # 嘗試 click 未出帳單消費明細
        pending_click = content_frame.evaluate("""() => {
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
        _log(f"[fubon][collect] 未出帳單 click: {pending_click}")
        page.wait_for_timeout(6000)
        with contextlib.suppress(Exception):
            page.screenshot(path=str(debug_dir / "05_after_pending.png"), full_page=True)
        # 切回 txnFrame
        page.wait_for_timeout(2000)
        for f in page.frames:
            if (f.name or "") == "txnFrame":
                content_frame = f
                break
        pending_page_text = ""
        with contextlib.suppress(Exception):
            pending_page_text = content_frame.evaluate("() => document.body.innerText.slice(0, 20000)") or ""
        out["pending_page_text"] = pending_page_text
        out["pending_page_url"] = content_frame.url
        _log(f"[fubon][collect] pending 頁 url={content_frame.url[:120]} text_len={len(pending_page_text)}")

        # === Step 5: dump 點完後所有 frames ===
        page.wait_for_timeout(2000)
        frames_data = []
        for f in page.frames:
            try:
                txt = f.evaluate("() => document.body.innerText.slice(0, 15000)")
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
            _log(f"  - {fd['name']} url={fd['url'][:80]} text_len={len(fd['text'])}")

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
            _log(f"[fubon][collect] ✓ 找到信用卡 frame: name={out.get('card_frame_name')} url={out.get('card_frame_url')[:100]} text_len={len(card_frame_text)}")
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
            _log(f"[fubon][collect] 回 home 失敗: {e}")

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
            deposit_click = deposit_frame.evaluate(r"""() => {
                const a = document.querySelector('a.task_CBOQU003, a.menu_CBO03');
                if (!a) return {ok: false, error: 'no_deposit_anchor'};
                try {
                    a.click();
                    return {ok: true, href: a.href || '', text: (a.textContent || '').trim()};
                } catch (e) {
                    return {ok: false, error: String(e)};
                }
            }""")
            _log(f"[fubon][collect] 我的存款 click: {deposit_click}")
            page.wait_for_timeout(8000)
            with contextlib.suppress(Exception):
                page.screenshot(path=str(debug_dir / "06_after_deposit.png"), full_page=True)

            # 重新找 txnFrame (URL 應該換到 CBOQU003_Home.faces)
            page.wait_for_timeout(2000)
            for f in page.frames:
                if (f.name or "") == "txnFrame":
                    deposit_frame = f
                    break

            deposit_page_text = ""
            with contextlib.suppress(Exception):
                deposit_page_text = deposit_frame.evaluate("() => document.body.innerText.slice(0, 20000)") or ""
            out["deposit_page_text"] = deposit_page_text
            out["deposit_page_url"] = deposit_frame.url
            _log(f"[fubon][collect] deposit 頁 url={deposit_frame.url[:120]} text_len={len(deposit_page_text)}")
        else:
            _log("[fubon][collect] ⚠️ 回 home 後找不到 txnFrame, 跳過 deposit step")
            out["deposit_page_text"] = ""
            out["deposit_page_url"] = ""

        # === Step 8 (2026-06-30): 點「存款交易查詢」(CDSQU001 / menu_CDS04) dump 存款交易表 ===
        # 使用者指出帳戶 drilldown 應該有交易明細；README 標示 Fubon TWD Txns ❌。
        # 已知 home menu anchor: <A class="task_CDSQU001 menu_CDS04">存款交易查詢</A>。
        # 先 dump query form/page raw text + response endpoints；persist parser 依真 raw shape 寫。
        try:
            page.goto(
                "https://ebank.taipeifubon.com.tw/B2C/cgequ/cgequ001/CGEQU001_Home.faces",
                wait_until="domcontentloaded", timeout=15000,
            )
            page.wait_for_timeout(5000)
            _log("[fubon][collect] 回 home 準備點 存款交易查詢")
        except Exception as e:
            _log(f"[fubon][collect] 回 home(交易查詢前) 失敗: {e}")

        txn_query_frame = None
        page.wait_for_timeout(2000)
        for f in page.frames:
            url = f.url or ""
            name = f.name or ""
            if "CGEQU001" in url or name == "txnFrame":
                txn_query_frame = f
                break

        if txn_query_frame:
            txn_click = txn_query_frame.evaluate(r"""() => {
                const selectors = [
                    'a.task_CDSQU001', 'a.menu_CDS04',
                    'a.task_CDSQU004', 'a.menu_CDS0103',
                ];
                for (const sel of selectors) {
                    const a = document.querySelector(sel);
                    if (!a) continue;
                    try {
                        a.click();
                        return {ok: true, selector: sel, href: a.href || '', text: (a.textContent || '').trim()};
                    } catch (e) {
                        return {ok: false, selector: sel, error: String(e)};
                    }
                }
                for (const a of document.querySelectorAll('a')) {
                    const t = (a.textContent || '').trim();
                    if (t.includes('存款交易查詢') || t.includes('帳戶明細')) {
                        try {
                            a.click();
                            return {ok: true, selector: 'text-fallback', href: a.href || '', text: t};
                        } catch (e) {
                            return {ok: false, selector: 'text-fallback', error: String(e)};
                        }
                    }
                }
                return {ok: false, error: 'no_deposit_txn_anchor'};
            }""")
            out["deposit_txn_click"] = txn_click
            _log(f"[fubon][collect] 存款交易查詢 click: {txn_click}")
            page.wait_for_timeout(8000)
            with contextlib.suppress(Exception):
                page.screenshot(path=str(debug_dir / "07_after_deposit_txn_query.png"), full_page=True)

            page.wait_for_timeout(2000)
            for f in page.frames:
                if (f.name or "") == "txnFrame":
                    txn_query_frame = f
                    break
            txn_query_text = ""
            with contextlib.suppress(Exception):
                txn_query_text = txn_query_frame.evaluate("() => document.body.innerText.slice(0, 30000)") or ""
            out["deposit_txn_page_text"] = txn_query_text
            out["deposit_txn_page_url"] = txn_query_frame.url
            _log(f"[fubon][collect] deposit txn 頁 url={txn_query_frame.url[:120]} text_len={len(txn_query_text)}")

            # 真正補交易明細: 富邦 CDSQU001 query form 是 native select + radio/buttons。
            # 對每個帳戶選「近1個月」並按「開始查詢」，結果頁 text 用 persist parser 入庫。
            deposit_txn_results = []
            acct_options = []
            with contextlib.suppress(Exception):
                acct_options = txn_query_frame.evaluate(r"""() => {
                    const sel = [...document.querySelectorAll('select')].find(s =>
                        [...s.options].some(o => /\d{10,16}/.test(o.textContent || o.value || ''))
                    );
                    if (!sel) return [];
                    return [...sel.options]
                        .map((o, index) => ({index, value: o.value || '', text: (o.textContent || '').trim()}))
                        .filter(o => /\d{10,16}/.test(o.text || o.value || ''));
                }""") or []
            _log(f"[fubon][collect] deposit txn 帳號選項數={len(acct_options)}")

            for acct_idx, opt in enumerate(acct_options[:10], start=1):
                account_no_match = re.search(r"\d{10,16}", (opt.get("text") or "") + " " + (opt.get("value") or ""))
                account_no = account_no_match.group(0) if account_no_match else None
                try:
                    # JSF/富邦用客製下拉，native <select> 本身 hidden；必須直接設值並呼叫
                    # comboAccountChange/checkAccountType，不能用 Playwright select_option。
                    selected_text = None
                    try:
                        selected_text = txn_query_frame.evaluate(r"""(opt) => {
                            const s = document.getElementById('form1:comboAccount') || document.querySelector('[name="form1:comboAccount"]');
                            if (!s) return null;
                            if (opt.value) s.value = opt.value;
                            else s.selectedIndex = opt.index;
                            s.dispatchEvent(new Event('input', {bubbles: true}));
                            s.dispatchEvent(new Event('change', {bubbles: true}));
                            if (typeof window.comboAccountChange === 'function') window.comboAccountChange();
                            if (typeof window.checkAccountType === 'function') window.checkAccountType();
                            s.dispatchEvent(new Event('blur', {bubbles: true}));
                            return s.options[s.selectedIndex]?.textContent?.trim() || null;
                        }""", opt)
                        page.wait_for_timeout(1200)
                    except Exception as e:
                        _log(f"[fubon][collect] deposit txn select evaluate 失敗: {e}")

                    with contextlib.suppress(Exception):
                        txn_query_frame.check("#form1\\:rdoTxDetail", force=True)
                    with contextlib.suppress(Exception):
                        txn_query_frame.check("#form1\\:rdoFast", force=True)
                    try:
                        txn_query_frame.check("#form1\\:rdoDay30", force=True)
                        picked_period = True
                    except Exception:
                        picked_period = False
                    page.wait_for_timeout(600)

                    try:
                        txn_query_frame.click("#form1\\:doValidateAndSubmit", timeout=8000, force=True)
                        query_result = {"ok": True, "selectedText": selected_text, "pickedPeriod": picked_period}
                    except Exception as e:
                        query_result = {"ok": False, "selectedText": selected_text, "pickedPeriod": picked_period, "error": str(e)}
                    _log(f"[fubon][collect] deposit txn 帳號#{acct_idx} 查詢: {query_result}")
                    page.wait_for_timeout(9000)
                    page.wait_for_timeout(1000)
                    for f in page.frames:
                        if (f.name or "") == "txnFrame":
                            txn_query_frame = f
                            break
                    result_text = txn_query_frame.evaluate("() => document.body.innerText.slice(0, 50000)") or ""
                    result_url = txn_query_frame.url
                    deposit_txn_results.append({
                        "account_no": account_no,
                        "selected_text": opt.get("text"),
                        "query_result": query_result,
                        "url": result_url,
                        "text": result_text,
                    })
                    _log(f"[fubon][collect] deposit txn 帳號#{acct_idx} result url={result_url[:120]} text_len={len(result_text)}")
                    with contextlib.suppress(Exception):
                        page.screenshot(path=str(debug_dir / f"08_deposit_txn_result_{acct_idx}.png"), full_page=True)

                    # 回 query form 查下一個帳號。若回不去就重新開 CDSQU001。
                    with contextlib.suppress(Exception):
                        txn_query_frame.evaluate(r"""() => {
                            const back = [...document.querySelectorAll('a,button,input[type=button]')]
                                .find(b => /回上一頁|重新查詢|回查詢頁|返回/.test((b.textContent || b.value || '').trim()));
                            if (back) back.click();
                        }""")
                        page.wait_for_timeout(2500)
                    if acct_idx < len(acct_options[:10]):
                        try:
                            page.goto(
                                "https://ebank.taipeifubon.com.tw/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces?menuId=CDS04&",
                                wait_until="domcontentloaded", timeout=15000,
                            )
                            page.wait_for_timeout(5000)
                            for f in page.frames:
                                if (f.name or "") == "txnFrame":
                                    txn_query_frame = f
                                    break
                        except Exception as e:
                            _log(f"[fubon][collect] deposit txn 回查詢頁失敗: {e}")
                except Exception as e:
                    _log(f"[fubon][collect] deposit txn 帳號#{acct_idx} 查詢例外: {e}")
                    deposit_txn_results.append({"account_no": account_no, "selected_text": opt.get("text"), "error": str(e)})
            out["deposit_txn_results"] = deposit_txn_results
        else:
            _log("[fubon][collect] ⚠️ 回 home 後找不到 txnFrame, 跳過 deposit txn query step")
            out["deposit_txn_page_text"] = ""
            out["deposit_txn_page_url"] = ""
            out["deposit_txn_results"] = []

        out["final_url"] = page.url
        out["_all_endpoints"] = sorted({h.endpoint for h in collector.hits if h.resp_json})
        publish_card_bill_facts(out, [_fubon_card_bill_fact(out.get("amount_page_text") or "")])
        _log(f"[fubon][collect] 攔到 {len(out['_all_endpoints'])} 個 endpoint: {out['_all_endpoints'][:15]}")
        return BankCollectResult(**out)


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
