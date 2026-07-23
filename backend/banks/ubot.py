#!/usr/bin/env python3
"""Union Bank of Taiwan (UBOT) personal e-banking crawler.

聯邦銀行(UBOT)個人網銀抓取器。

登入入口：官網 https://www.ubot.com.tw/home 內嵌登入 modal（舊 mybank 已搬家）。
流程：點右上「網銀登入」開 modal → 確認「個人用戶登入」分頁
      → fill #sid/#nickname/#password → OCR 圖形驗證碼(#CAPTCHA, 6碼) → 點「登入」。
驗證碼：img[alt='CAPTCHA'] base64 jpeg 170x50，ddddocr 直接 OCR，錯則點「重新產生」換圖。
無虛擬鍵盤（密碼欄旁鍵盤圖示為選用防側錄，標準 .fill() 即可）。

設計規範：dump 真值不猜測 → 登入後 collect 先 dump endpoint，摸清明細 API 再補 parse。
預設行為：headless browser → 預設 headless=True。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.base import BankCollectResult, BankCrawler, ResponseCollector
from backend.core.creds import UbotCreds
from backend.core.captcha import solve_captcha, wait_captcha_stable

BASE = "https://www.ubot.com.tw/home"

SEL_SID = "#sid"           # 身分證字號 (text)
SEL_NICK = "#nickname"     # 使用者代號 (password)
SEL_PWD = "#password"      # 網路密碼 (password, maxlen=12)
SEL_CAPTCHA = "#CAPTCHA"   # 圖形驗證碼 (tel)
SEL_CAPTCHA_IMG = "img[alt='CAPTCHA']"  # base64 jpeg 170x50

# 開 modal：右上「網銀登入」按鈕（短文字，避開誤點）
JS_OPEN_MODAL = (
    "(() => { const b=[...document.querySelectorAll('button,a')]"
    ".find(e=>e.offsetParent!==null && /網銀登入/.test((e.textContent||'').trim())"
    "  && (e.textContent||'').trim().length<8);"
    " if(b){ b.click(); return true;} return false; })()"
)
# 確保「個人用戶登入」分頁被選中
JS_PERSONAL_TAB = (
    "(() => { const t=[...document.querySelectorAll('a,button,div,li,span')]"
    ".find(e=>e.offsetParent!==null && (e.textContent||'').trim()==='個人用戶登入');"
    " if(t){ t.click(); return true;} return false; })()"
)
# 點 modal 內的「登入」綠按鈕（class 含 ubot-primary-green，避開右上導覽列「網銀登入」）
JS_CLICK_LOGIN = (
    "(() => { const btns=[...document.querySelectorAll('button')].filter(b=>b.offsetParent!==null);"
    " let b=btns.find(x=>/^登入$/.test((x.textContent||'').trim())"
    "   && /green/.test(x.className||''));"
    " if(!b) b=btns.find(x=>/^登入$/.test((x.textContent||'').trim()) && !x.closest('header,nav'));"
    " if(b){ b.click(); return true;} return false; })()"
)
# 點「重新產生」換驗證碼（藍字可點的 div，class 含 text-ubot-primary-blue / cursor）
JS_REGEN = (
    "(() => { const els=[...document.querySelectorAll('div,a,span,button,i')]"
    ".filter(x=>x.offsetParent!==null && (x.textContent||'').trim()==='重新產生');"
    " let el=els.find(x=>/blue|cursor|pointer|text-ubot/.test(x.className||''));"
    " if(!el) el=els[els.length-1];"
    " if(el){ el.click(); return true;} return false; })()"
)
# 登入後是否仍停在登入框（#sid 可見 = 還沒成功）
# 偵測登入 form 還在不在（debug 用；正常邏輯改用 JS_LOGGED_IN_POSITIVE 4 條件 AND）
JS_STILL_LOGIN = (
    "(() => { const s=document.querySelector('#sid');"
    " return !!(s && s.offsetParent!==null); })()"
)
# 抓登入框附近的錯誤/提示訊息（驗證碼錯、密碼錯等）
JS_ERR_MSG = (
    "(() => { const txt=[...document.querySelectorAll('div,span,p,label')]"
    ".filter(e=>e.offsetParent!==null)"
    ".map(e=>(e.textContent||'').trim())"
    ".filter(t=>t && t.length<40 && /錯誤|不正確|失敗|重新|鎖|無效|請輸入正確|驗證碼/.test(t));"
    " return [...new Set(txt)].slice(0,6); })()"
)

# W (2026-06-17): positive signal — 對齊 SCSB 鐵律, 取代「#sid 不在 = 已登入」
# negative-only 訊號。內銀區頁面本來就沒 #sid（外網 modal 才有），單看它不在
# 容易誤判（例如錯誤頁也沒 #sid）。改為 4 條件 AND：
#   1. URL 在 ibank 內銀區（非 modal/login）
#   2. innerText >= 500 字（< 500 = loading / 空白 / 錯誤頁）
#   3. 命中 >= 2 個主菜單字樣
#   4. 登入 form 元素 #sid 已不可見
# 詳見 wiki/concepts/bank-crawler-login-positive-signal-rule.md
#
# 2026-06-18 evidence-driven fix（使用者指示 + cloud log audit）：
# 加 passwordExpiryNag — 偵測「您離上次變更密碼已超過6個月，建議請定期變更密碼」
# 提醒頁 (url=#/I1201001)。這頁是登入「真的成功」後的密碼到期 nag screen,
# 不是 modal 攔截，只是不在內銀首頁所以 urlOk=False/kwOk=False/lenOk 也低
# (txt_len=905, hit=0)。判定 = passwordExpiryNag = 已登入，立即 return True，
# 讓 collect 接手 navigate 去帳戶總覽繞過。使用者明示：不更改密碼繼續同步。
JS_LOGGED_IN_POSITIVE = r"""
(() => {
  const url = location.href || '';
  // ibank 內銀區常見路徑：/IBKx/...，外網首頁 ubot.com.tw 不算
  const urlOk = /\/ibank|\/A0101|\/B0101|\/F0101|\/F0201|\/IBK[A-Z]/i.test(url);
  const txt = (document.body && document.body.innerText) || '';
  const lenOk = txt.length >= 500;
  const keywords = [
    '帳戶總覽', '台幣存款', '外幣存款', '信用卡', '貸款',
    '投資理財', '保險', '我的最愛', '會員專區', '登出',
    '個人用戶', '台幣交易明細', '信用卡明細', '存款明細', '繳費',
  ];
  let hit = 0;
  for (const k of keywords) {
    if (txt.indexOf(k) !== -1) { hit += 1; if (hit >= 2) break; }
  }
  const kwOk = hit >= 2;
  const sidEl = document.querySelector('#sid');
  const noLoginForm = !(sidEl && sidEl.offsetParent !== null);
  // 密碼到期 nag screen — 登入成功，只是被引導到 I1201001 提醒頁
  // 證據：url=#/I1201001 + body 含「變更密碼」字 + 有 logout/Remaining time
  const passwordExpiryNag = (
    /\/I1201001/i.test(url) &&
    /變更密碼|change.*password/i.test(txt) &&
    /logout|登出/i.test(txt)
  );
  return {
    ok: (urlOk && lenOk && kwOk && noLoginForm) || passwordExpiryNag,
    urlOk, lenOk, kwOk, noLoginForm, passwordExpiryNag,
    url, txt_len: txt.length, hit
  };
})()
"""


def _log(*a):
    print(*a, file=sys.stderr)


class UbotLoginError(RuntimeError):
    """聯邦 UBOT login 送出後失敗——立刻中止，絕不自動重打（防鎖帳號）。"""


class UbotCrawler(BankCrawler):
    def __init__(self):
        super().__init__(name="ubot")
        self.creds = UbotCreds.load()

    def _host_filter(self) -> str:
        return "ubot.com.tw"

    def _logged_in(self, page) -> bool:
        """正向訊號：URL 在內銀區 + innerText >= 500 + 命中 >= 2 個菜單字 + 無 login form。

        取代「#sid 不在 = 已登入」negative-only 訊號（內銀區頁面本來就沒 #sid）。
        詳見 wiki/concepts/bank-crawler-login-positive-signal-rule.md
        """
        try:
            r = page.evaluate(JS_LOGGED_IN_POSITIVE)
        except Exception:
            return False
        if isinstance(r, dict):
            ok = bool(r.get("ok"))
            if not ok:
                _log(
                    f"[login] _logged_in=False  "
                    f"urlOk={r.get('urlOk')} lenOk={r.get('lenOk')} "
                    f"kwOk={r.get('kwOk')} noLoginForm={r.get('noLoginForm')} "
                    f"pwNag={r.get('passwordExpiryNag')} "
                    f"txt_len={r.get('txt_len')} hit={r.get('hit')} "
                    f"url={r.get('url','')[:120]}",
                )
            elif r.get("passwordExpiryNag"):
                # 登入成功但停在密碼到期提醒頁 — 使用者指示繼續同步不改密碼
                _log(
                    f"[login] ✅ 已登入 (停在密碼到期 nag, 將由 collect 繞過) "
                    f"url={r.get('url','')[:120]}",
                )
            return ok
        return bool(r)

    # ---------- 登入 ----------
    def login(self, page) -> bool:
        """UBOT 登入——鐵律 max_attempts=1，失敗 raise UbotLoginError。

        OCR 階段（送出前）可換圖重試最多 5 次（安全）。
        一旦點下登入鈕，失敗就 raise UbotLoginError 中止，**絕不重打**——
        聯邦 3 次錯密碼鎖帳號，以前的 `for attempt in range(1, 6)` 是踩雷 candidate。
        """
        page.wait_for_timeout(8000)
        _log(f"[login] 起始 url={page.url}")

        # 開登入 modal
        opened = page.evaluate(JS_OPEN_MODAL)
        _log(f"[login] 開 modal: {opened}")
        page.wait_for_timeout(2500)
        # 確認在「個人用戶登入」分頁
        page.evaluate(JS_PERSONAL_TAB)
        page.wait_for_timeout(800)

        try:
            page.wait_for_selector(SEL_SID, state="visible", timeout=10000)
        except Exception as e:
            msg = f"UBOT 登入框 #sid 未出現 (url={page.url}): {e}"
            _log(f"[login] ❌ {msg}")
            raise UbotLoginError(msg) from e

        # 填三欄
        try:
            page.fill(SEL_SID, self.creds.national_id)
            page.wait_for_timeout(150)
            page.fill(SEL_NICK, self.creds.user_code)
            page.wait_for_timeout(150)
            page.fill(SEL_PWD, self.creds.password)
            page.wait_for_timeout(200)
        except Exception as e:
            msg = f"UBOT 填欄位失敗: {e}"
            _log(f"[login] ❌ {msg}")
            raise UbotLoginError(msg) from e

        # OCR 驗證碼（送出前安全重試：長度錯就換圖最多 5 次）
        captcha = self._ocr_with_regen(page, max_attempts=5)
        if not captcha:
            msg = "OCR 5 次都讀不出 6 碼數字驗證碼，放棄（未送 login，無鎖帳號風險）"
            _log(f"[login] ❌ {msg}")
            raise UbotLoginError(msg)
        try:
            page.fill(SEL_CAPTCHA, captcha)
            page.wait_for_timeout(200)
        except Exception as e:
            msg = f"UBOT 填驗證碼失敗: {e}"
            _log(f"[login] ❌ {msg}")
            raise UbotLoginError(msg) from e

        # 🚨 max_attempts=1：送 login 只此一次
        _log(f"[login] 送出 login (captcha={captcha})")
        clicked = page.evaluate(JS_CLICK_LOGIN)
        if not clicked:
            msg = "UBOT 找不到登入按鈕（form 渲染異常）"
            _log(f"[login] ❌ {msg}")
            raise UbotLoginError(msg)
        page.wait_for_timeout(6000)

        if self._logged_in(page):
            _log(f"[login] ✅ 登入成功 -> {page.url}")
            return True

        # 還在登入框 → 立刻停手、dump 錯誤訊息
        try:
            errs = page.evaluate(JS_ERR_MSG)
        except Exception:
            errs = []
        # Internal policy: max_attempts=1, MUST NOT auto-retry.
        # 聯邦銀行錯誤 3 次即停用網銀,所以絕不在 code 端重打。
        # See wiki/concepts/taiwan-bank-login-retry-account-lockout-lesson.
        from backend.banks._login_debug import snapshot as _login_snapshot
        snap = _login_snapshot(page)
        msg = (
            f"聯邦登入失敗。請檢查帳號、密碼是否正確。"
            f"\n  url={page.url}\n  錯誤訊息: {errs}\n"
            f"  可能原因：(a) 驗證碼辨識錯誤 (機率小) (b) 帳號或密碼錯誤"
            f" (c) 帳號已被停用 (聯邦錯誤 3 次即停用網銀)。"
            f"\n{snap}"
        )
        _log(f"[login] ❌ {msg}")
        raise UbotLoginError(msg)

    def _ocr_with_regen(self, page, max_attempts=5):
        """OCR 聯邦 6 碼純數字驗證碼，失敗換圖重試（送出前安全重試）。"""
        for n in range(1, max_attempts + 1):
            wait_captcha_stable(page, SEL_CAPTCHA_IMG, tmp_path=self.captcha_tmp)
            captcha = solve_captcha(
                page, SEL_CAPTCHA_IMG, expected_len=6, alnum_only=True,
                digits_only=True, min_confidence=0.85,
                tmp_path=self.captcha_tmp,
            )
            if captcha and len(captcha) == 6 and captcha.isdigit():
                _log(f"[cap] 第 {n} 次 OCR 成功: {captcha}")
                return captcha
            _log(f"[cap] 第 {n}/{max_attempts} 次 OCR 失敗（讀到 {captcha!r}），換圖")
            if n < max_attempts:
                try:
                    page.evaluate(JS_REGEN)
                    page.wait_for_timeout(2000)
                except Exception as e:
                    _log(f"[cap] 換圖失敗: {e}")
        return None

    # ---------- 抓取 ----------
    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        """登入後依序抓：帳戶總覽(餘額) → 台幣交易明細 → 信用卡帳單/明細。

        聯邦後端 www.ubot.com.tw/MyBank/IBK{模組}{編號}，明文 POST {sid, sessionId, ...}。
        互動式：餘額/卡彙總進頁自動打；逐筆明細要選帳戶/期別 + 按查詢才觸發。
        """
        out: dict = {}
        page.wait_for_timeout(2500)
        self._close_popups(page)
        page.wait_for_timeout(1000)

        # 1) 帳戶總覽（A0101001）：自動打 IBKA010001~4
        self._goto(page, "/A0101001", wait=6500)
        out["deposit_twd"] = self._latest_body(collector, "IBKA010001")   # 台幣存款 NTList + LoanList
        out["deposit_foreign"] = self._latest_body(collector, "IBKA010002")  # 外幣 FTList
        out["card_summary"] = self._latest_body(collector, "IBKA010003")  # 信用卡 CardList
        out["investment"] = self._latest_body(collector, "IBKA010004")    # 投資 TNRWD

        # 2) 台幣交易明細（B0101001）：選帳戶 + 期間(近一月) + Go → IBKB010102
        out["twd_txns"] = self._collect_twd_txns(page, collector)

        # 3) 信用卡：額度彙總(F0101001 → IBKF010001) + 已出帳逐筆(F0201001 → IBKF020102)
        self._goto(page, "/F0101001", wait=6000)
        out["card_limit"] = self._latest_body(collector, "IBKF010001")
        out["card_billed"] = self._collect_card_billed(page, collector)
        # 未出帳（F0301001 → IBKF030001）
        self._goto(page, "/F0301001", wait=6000)
        out["card_unbilled"] = self._latest_body(collector, "IBKF030001")
        # 2026-06-22 (使用者指示「ubot 有近期繳款紀錄查詢呀」F0801001):
        # 近期繳款紀錄 (F0801001 → IBKF080001), 真實「上次繳款日 + 金額」source.
        # 補在 card_limit lastPayAmt=0 + lastPayDate=00000000 sentinel 無法判定的場景.
        # 進頁 auto-fire (跟 F0101001 / F0301001 同 pattern), 不需點按.
        self._goto(page, "/F0801001", wait=6000)
        out["card_pay_history"] = self._latest_body(collector, "IBKF080001")

        out["_final_url"] = page.url
        out["_all_endpoints"] = sorted({h.endpoint for h in collector.hits if h.resp_json})
        return BankCollectResult(**out)

    # ---------- collect 輔助 ----------
    def _goto(self, page, route: str, wait: int = 6000):
        page.evaluate(f"location.hash='#{route}'")
        page.wait_for_timeout(wait)

    def _close_popups(self, page):
        # 2026-06-18: 加密碼到期 nag 常見按鈕字眼 (以後再說/暫不變更/不變更/下次再說/Later/Skip)
        page.evaluate(
            "(() => { for(let i=0;i<6;i++){"
            "  const b=[...document.querySelectorAll('button,a,div')].find(e=>e.offsetParent!==null"
            "    && /^(Confirm|OK|確認|我知道了|關閉|稍後|下次|略過|確定|不再顯示|同意|以後再說|暫不變更|不變更|下次再說|Later|Skip|Cancel|取消)$/i.test((e.textContent||'').trim()));"
            "  if(b) b.click(); else break; } })()",
        )

    @staticmethod
    def _latest_body(collector: ResponseCollector, endpoint: str):
        """取某 endpoint 最新一次成功回應的 RespBody。"""
        hit = collector.latest(endpoint)
        if hit and isinstance(hit.resp_json, dict):
            rc = hit.resp_json.get("RespCode", {})
            if rc.get("RtnCode") == "0000":
                return hit.resp_json.get("RespBody")
        return None

    def _collect_twd_txns(self, page, collector: ResponseCollector) -> list:
        """台幣交易明細：對每個帳戶選 last month 查一次，收集 IBKB010102。"""
        self._goto(page, "/B0101001", wait=6500)
        results: list = []
        try:
            selects = page.query_selector_all("select")
            if len(selects) < 2:
                _log("[twd] 查詢表單 select 不足，跳過")
                return results
            # 帳戶下拉選項數（index 0=Choose Account）
            acct_opts = page.evaluate(
                "(() => { const s=document.querySelectorAll('select')[0];"
                " return s ? s.options.length : 0; })()",
            )
            for idx in range(1, max(acct_opts, 1)):
                selects = page.query_selector_all("select")
                try:
                    selects[0].select_option(index=idx)
                    page.wait_for_timeout(700)
                    selects[1].select_option(value="3")  # last month
                    page.wait_for_timeout(700)
                except Exception as e:
                    _log(f"[twd] 選 index={idx} 失敗: {e}")
                    continue
                page.evaluate(
                    "(() => { const b=[...document.querySelectorAll('button')].filter(e=>e.offsetParent!==null)"
                    ".find(x=>/^(Go|查詢|Query|Search)$/i.test((x.textContent||'').trim())); if(b) b.click(); })()",
                )
                page.wait_for_timeout(6500)
                body = self._latest_body(collector, "IBKB010102")
                if body:
                    results.append(body)
        except Exception as e:
            _log(f"[twd] 失敗: {e}")
        return results

    def _collect_card_billed(self, page, collector: ResponseCollector) -> list:
        """信用卡已出帳逐筆：進 F0201001，對每個期別點一次，收集 IBKF020102。"""
        self._goto(page, "/F0201001", wait=6500)
        results: list = []
        # 先拿期別清單（IBKF020101 的 DateList）
        date_list = []
        body0 = self._latest_body(collector, "IBKF020101")
        if isinstance(body0, dict):
            date_list = body0.get("DateList", []) or []
        if not date_list:
            # 沒攔到就點「最近一期」觸發一次
            page.evaluate(
                "(() => { const b=[...document.querySelectorAll('button,a,li,div')].filter(e=>e.offsetParent!==null)"
                ".find(x=>/最近一期/.test((x.textContent||'').trim()) && (x.textContent||'').trim().length<8);"
                " if(b) b.click(); })()",
            )
            page.wait_for_timeout(6000)
            body = self._latest_body(collector, "IBKF020102")
            if body:
                results.append(body)
            return results
        # 逐期點按鈕（按鈕文字 = yyyy/mm 或「最近一期」對應第一個）
        for i, dt in enumerate(date_list):
            label = "最近一期" if i == 0 else dt
            page.evaluate(
                "((lbl) => { const b=[...document.querySelectorAll('button,a,li,div')].filter(e=>e.offsetParent!==null)"
                ".find(x=>(x.textContent||'').trim()===lbl && (x.textContent||'').trim().length<10);"
                " if(b){ b.click(); return true;} return false; })",
                label,
            )
            page.wait_for_timeout(6000)
            body = self._latest_body(collector, "IBKF020102")
            if body and body not in results:
                results.append(body)
        return results


if __name__ == "__main__":
    import json
    crawler = UbotCrawler()
    result = crawler.run(login_url=BASE, headless=True)
    out_file = Path(__file__).resolve().parents[1] / "data" / "ubot_collected.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"\n[done] 已存: {out_file}")
    if result.get("error"):
        _log(f"  error: {result['error']} url={result.get('final_url')}")
    data = result.get("data", {})
    _log("\n===== 抓取摘要 =====")
    dt = data.get("deposit_twd") or {}
    if isinstance(dt, dict):
        nt = dt.get("NTList", [])
        _log(f"  台幣存款帳戶: {len(nt)} 個  總額={dt.get('TotalData', {}).get('Deposit', '?')}")
    cs = data.get("card_summary") or {}
    if isinstance(cs, dict):
        _log(f"  信用卡彙總: {len(cs.get('CardList', []))} 筆  本期應繳={cs.get('TotalData', {}).get('Card', '?')}")
    twd = data.get("twd_txns") or []
    n_twd = sum(len(b.get("NTDetailList", [])) for b in twd if isinstance(b, dict))
    _log(f"  台幣交易明細: {n_twd} 筆（{len(twd)} 個帳戶）")
    cb = data.get("card_billed") or []
    n_cb = sum(len(b.get("CardList", [])) for b in cb if isinstance(b, dict))
    _log(f"  信用卡已出帳明細: {n_cb} 筆（{len(cb)} 期）")
    _log(f"\n  攔到的 endpoint: {data.get('_all_endpoints', [])}")
