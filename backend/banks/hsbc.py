#!/usr/bin/env python3
"""HSBC credit card web-service crawler.

滙豐(HSBC)信用卡網路服務抓取器。

兩段式登入：
  第一頁：填 #userId → 點「繼續」
  第二頁：填 #password + #captchaInput(OCR) → 點「繼續」
驗證碼：data:image/jpeg base64 128x40 的 <img alt=''>，英數混合 5 碼。
  關鍵時序（2026-06-10 修正）：填密碼後驗證碼圖才非同步 render（會先顯示 'loading'），
  必須 wait_captcha_stable 等圖位元組穩定再 OCR，否則 OCR 到 blank → found=False。
  雙保險：DOM 截圖失敗時，從攔截的 captcha/request API 拿明文 base64 餵 ddddocr。
登入後攔截式抓取信用卡帳單/明細 API（ibk-bff/api/v1/*）。
session 持久化（user_data_dir）：登入成功後短期重跑免重登。
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.base import BankCollectResult, BankCrawler, ResponseCollector
from backend.banks._login_debug import snapshot as _login_snapshot
from backend.core.creds import HsbcCreds
from backend.core.captcha import solve_captcha, wait_captcha_stable, ocr_bytes

BASE = "https://card.hsbc.com.tw/#/login"

SEL_USERID = "#userId"
SEL_PWD = "#password"
SEL_CAPTCHA = "#captchaInput"
# HSBC 是 5 碼英數 CAPTCHA。2026-07-05 Azure log 證實只檢查
# expected_len/alnum_only 會放行形式合法但內容錯的 OCR false positive，銀行回
#「驗證碼錯誤，請重新輸入。」；送出後不可自動重試，故送出前必須加信心門檻。
HSBC_CAPTCHA_MIN_CONFIDENCE = 0.85

# W (2026-06-17): positive signal — 對齊 SCSB 鐵律。HSBC 是 SPA + 信用卡網銀，
# 菜單字較少，innerText 門檻調為 300（< SCSB 的 500）。4 條件 AND：
#   1. URL 在 card.hsbc.com.tw 且不在 #/login hash
#   2. innerText >= 300 字（SPA 動態 render，門檻較低）
#   3. 命中 >= 2 個菜單字樣（卡片清單/帳單/未出帳/已出帳/繳款/登出 等）
#   4. 登入 form 元素 #userId 已不可見
# 詳見 wiki/concepts/bank-crawler-login-positive-signal-rule.md
JS_LOGGED_IN_POSITIVE = r"""
(() => {
  const url = location.href || '';
  const lowerUrl = url.toLowerCase();
  const urlOk = lowerUrl.indexOf('card.hsbc.com.tw') !== -1
    && lowerUrl.indexOf('#/login') === -1;
  const txt = (document.body && document.body.innerText || '');
  const lenOk = txt.length >= 300;
  const keywords = [
    '我的卡片', '卡片清單', '帳單', '未出帳', '已出帳',
    '帳單明細', '應繳金額', '本期應繳', '繳款', '繳費',
    '登出', '我的帳戶', 'Logout', 'My Cards', 'Statement',
  ];
  let hit = 0;
  for (const k of keywords) {
    if (txt.indexOf(k) !== -1) { hit += 1; if (hit >= 2) break; }
  }
  const kwOk = hit >= 2;
  const userIdEl = document.querySelector('#userId');
  const noLoginForm = !(userIdEl && userIdEl.offsetParent !== null);
  return {
    ok: urlOk && lenOk && kwOk && noLoginForm,
    urlOk, lenOk, kwOk, noLoginForm,
    url, txt_len: txt.length, hit
  };
})()
"""

# 驗證碼圖：data:image/jpeg base64，128x40（用 JS 精準定位，不靠脆弱的 alt='' selector）
JS_FIND_CAPTCHA_IMG = (
    "(() => { const im=[...document.querySelectorAll('img')].find(i=>i.offsetParent!==null"
    " && /^data:image\\/jpeg/.test(i.src||'') && i.naturalWidth>=80 && i.naturalWidth<=200"
    " && i.naturalHeight>=25 && i.naturalHeight<=60);"
    " if(im){ im.setAttribute('data-captcha-img','1'); return true;} return false; })()"
)
SEL_CAPTCHA_IMG = "img[data-captcha-img='1']"
# 直接從 DOM 取驗證碼圖的 base64（免截圖，最穩）
JS_CAPTCHA_DATAURL = (
    "(() => { const im=[...document.querySelectorAll('img')].find(i=>i.offsetParent!==null"
    " && /^data:image\\/jpeg/.test(i.src||'') && i.naturalWidth>=80 && i.naturalWidth<=200"
    " && i.naturalHeight>=25 && i.naturalHeight<=60);"
    " return im ? im.src : ''; })()"
)
# 「驗證碼欄是否仍在 loading」（圖還沒 render）
JS_CAPTCHA_LOADING = (
    "(() => { const inp=document.querySelector('#captchaInput');"
    " const root=inp && inp.closest('div,form,section');"
    " return root ? /loading/i.test(root.textContent||'') : true; })()"
)


def _log(*a):
    print(*a, file=sys.stderr)


class HsbcLoginError(RuntimeError):
    """HSBC login 送出後失敗——立刻中止，絕不自動重打（防鎖帳號）。"""


class HsbcCrawler(BankCrawler):
    def __init__(self):
        super().__init__(name="hsbc")
        self.creds = HsbcCreds.load()

    def _host_filter(self) -> str:
        return "hsbc.com.tw"

    def _logged_in(self, page) -> bool:
        """正向訊號：URL 在 dashboard SPA + innerText >= 300 + 命中 >= 2 個菜單字 + 無 login form。

        HSBC 是 SPA + 信用卡網銀，菜單字較少，門檻調為 innerText >= 300（非 500），
        命中關鍵字以信用卡網銀常見字樣為主。詳見 wiki SCSB 鐵律。
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
                    f"txt_len={r.get('txt_len')} hit={r.get('hit')} "
                    f"url={r.get('url','')[:120]}",
                )
            return ok
        return bool(r)

    # ---------- 登入 ----------
    def login(self, page) -> bool:
        page.wait_for_timeout(7000)
        # (2) session 持久化：還在就免登入
        if self._logged_in(page):
            _log(f"[login] ✅ session 還在，免登入 -> {page.url}")
            return True

        # 第一頁：userId（若已記住跳到第二頁，#userId 可能不存在）
        if page.query_selector(SEL_USERID) is not None:
            try:
                page.wait_for_selector(SEL_USERID, state="visible", timeout=8000)
                page.fill(SEL_USERID, self.creds.user_id)
                page.wait_for_timeout(400)
                page.evaluate(
                    "(() => { const b=document.querySelector('[data-testid=continueButton]')"
                    "||[...document.querySelectorAll('button')].find(x=>x.textContent.includes('繼續'));"
                    "if(b) b.click(); })()",
                )
                _log("[login] 第一頁 userId 已送出")
                page.wait_for_timeout(6000)
            except Exception:
                pass

        # 第二頁：password + captcha
        try:
            page.wait_for_selector(SEL_PWD, state="visible", timeout=12000)
        except Exception:
            if self._logged_in(page):
                return True
            _log(f"[login] 第二頁密碼欄未出現，url={page.url}")
            return False

        last_captcha = None
        # 🚨 max_attempts=1 鐵律：密碼 + submit 只送一次（HSBC 多錯鎖卡）
        # OCR 階段允許 8 次重產驗證碼（換圖重 OCR，沒送 submit 不會鎖卡）
        # 找對 captcha 後送一次 submit，失敗 raise HsbcLoginError 中止
        OCR_MAX = 8

        # 先檢查是否已登入
        if self._logged_in(page):
            _log(f"[login] ✅ 已登入（迴圈頂偵測）-> {page.url}")
            return True
        if not page.query_selector(SEL_PWD):
            page.wait_for_timeout(3000)
            if self._logged_in(page):
                _log(f"[login] ✅ 已登入（密碼欄消失後確認）-> {page.url}")
                return True
            msg = f"密碼欄消失但未登入，url={page.url}"
            _log(f"[login] ❌ {msg}")
            raise HsbcLoginError(msg)

        # (1) 填密碼——填密碼才觸發驗證碼圖 render（關鍵時序）
        try:
            page.fill(SEL_PWD, self.creds.password, timeout=8000)
        except Exception as e:
            if self._logged_in(page):
                _log(f"[login] ✅ 已登入（fill 密碼時跳轉）-> {page.url}")
                return True
            msg = f"fill 密碼失敗: {e}"
            _log(f"[login] ❌ {msg}")
            raise HsbcLoginError(msg) from e
        page.wait_for_timeout(300)

        # (2) OCR 階段：最多換 OCR_MAX 次驗證碼（沒送 submit，不會鎖卡）
        captcha = None
        for ocr_attempt in range(1, OCR_MAX + 1):
            # 等 'loading' 文字消失（圖開始 render）
            for _ in range(15):
                try:
                    if not page.evaluate(JS_CAPTCHA_LOADING):
                        break
                except Exception:
                    break
                page.wait_for_timeout(700)
            page.wait_for_timeout(800)

            captcha = self._solve_captcha(page)

            # 跟上一輪同碼 = 沒真的換圖，強制再換一次
            if captcha and captcha == last_captcha:
                _log(f"[login] OCR 第 {ocr_attempt} 次與上輪同碼 {captcha!r}，圖沒換，強制重產")
                self._regen_captcha(page)
                captcha = self._solve_captcha(page)
            last_captcha = captcha

            if captcha:
                break  # OCR 成功，跳出 OCR loop 進 submit 階段
            _log(f"[login] OCR 第 {ocr_attempt}/{OCR_MAX} 次未解出，換驗證碼")
            self._regen_captcha(page)
            page.wait_for_timeout(2500)

        if not captcha:
            msg = f"HSBC 驗證碼辨識失敗 ({OCR_MAX} 次都讀不出)。請稍後再試。"
            _log(f"[login] ❌ {msg}")
            raise HsbcLoginError(msg)

        # (3) submit only once — bank locks the card after a few wrong-password
        # attempts. Internal policy, see
        # wiki/concepts/taiwan-bank-login-retry-account-lockout-lesson.
        #
        # 🚨 2026-06-18 真相 (local debug_hsbc_submit.py)：
        # 之前用 page.evaluate("b.click()") 會被 HSBC client-side anti-bot 吞掉
        # (JS-driven click 是 trusted=false), submit 永遠送不出去，body 凍結在
        # 「您好! ****XXXX 請輸入您的密碼/驗證碼」341 字初始態。
        # 必須用 page.locator().click() — Playwright 模擬真 mouse event
        # (trusted=true, 含 hover/mousedown timing), 與用戶真實點擊一致。
        try:
            page.fill(SEL_CAPTCHA, captcha)
            page.wait_for_timeout(200)
            # Locate the 繼續 submit button (排除 header 的「登入」/「中文」/「註冊」)
            btn = page.locator(
                "button[type=submit]:visible:has-text('繼續'),"
                " button[type=submit]:visible:has-text('登入')"
            ).first
            btn.click(timeout=8000)
        except Exception as e:
            msg = f"submit 失敗: {e}"
            _log(f"[login] ❌ {msg}")
            raise HsbcLoginError(msg) from e

        # (4) 等登入結果 — 2026-06-18 真相 (debug_hsbc_silent_reject.py)：
        # HSBC submit 後 reaction 慢 (~5.5s page 才從 #/login 跳 #/u/dashboard,
        # body 從 341 字 freeze 5 秒才開始更新)。原本 22s loop 有早 break logic
        # (line 260-266 的 JS_CAPTCHA_LOADING + page.query_selector(SEL_PWD))
        # 因 captcha 圖在 submit 後不會消失 (page 跳走才消失), JS_CAPTCHA_LOADING
        # 提前判 False → wait 2s → 找到密碼欄 → break, 整個 loop 在 t+3-4s 就出去,
        # 永遠等不到 t+5.5s 的 dashboard。改為純 polling, 全 22s loop 信任 _logged_in。
        for _ in range(22):
            page.wait_for_timeout(1000)
            if self._logged_in(page):
                _log(f"[login] ✅ 成功 captcha={captcha!r} -> {page.url}")
                return True

        # Internal policy: MUST NOT auto-retry — HSBC locks the card after a
        # few wrong-password attempts.
        # See wiki/concepts/taiwan-bank-login-retry-account-lockout-lesson.
        msg = (
            f"HSBC 登入失敗 (22 秒未進入內銀區)。請檢查帳號、密碼是否正確。url={page.url}\n"
            f"{_login_snapshot(page)}"
        )
        _log(f"[login] ❌ {msg}")
        raise HsbcLoginError(msg)

    def _regen_captcha(self, page):
        """點「重新產生」換驗證碼圖，等圖 src 變化（確認真換了新圖）。

        實測真相（2026-06-10）：
        - 可點元素是 <button aria-label="Refresh Captcha">（文字 label <div> 點了沒用）。
        - HSBC **前端本地生成新驗證碼**：換圖後圖 src 變，但**不重打 captcha/request API**。
          故換圖成功要判斷「圖 src 變化」，不能等 API 重打（永遠等不到 → 迴圈卡死）。
        """
        before_src = page.evaluate(JS_CAPTCHA_DATAURL) or ""
        page.evaluate(
            "(() => {"
            " let b=document.querySelector('button[aria-label=\"Refresh Captcha\"]');"
            " if(!b) b=[...document.querySelectorAll('button')].find(x=>x.offsetParent!==null"
            "   && /重新產生/.test(x.textContent||'') && /IconButton/.test(x.className||''));"
            " if(b) b.click(); })()",
        )
        # 等圖 src 真的變了（最多 6 秒）
        for _ in range(12):
            page.wait_for_timeout(500)
            now = page.evaluate(JS_CAPTCHA_DATAURL) or ""
            if now and now != before_src:
                page.wait_for_timeout(400)  # 等圖 render 穩定
                return
        page.wait_for_timeout(1000)

    def _solve_captcha(self, page) -> str | None:
        """兩路徑解驗證碼（都讀「當前畫面」的圖，換圖後也對）：
        1. DOM <img>.src 的 data:image base64（首選：永遠反映當前畫面的圖）
        2. DOM element 截圖（次選：靠 wait_captcha_stable 等圖穩定）

        ⚠️ 不用攔截的 captcha/request API base64：HSBC 換圖**不重打 API**，
        該 base64 換圖後會過時（拿到舊圖），fill 進去害提交被拒 + 欄位跳轉撞 timeout。
        """
        # 路徑 1：DOM <img>.src base64（當前畫面的圖，最可靠）
        data_url = page.evaluate(JS_CAPTCHA_DATAURL)
        if data_url and "," in data_url:
            try:
                raw = base64.b64decode(data_url.split(",", 1)[1])
                c = ocr_bytes(
                    raw,
                    expected_len=5,
                    alnum_only=True,
                    min_confidence=HSBC_CAPTCHA_MIN_CONFIDENCE,
                )
                if c:
                    _log(f"[login] 路徑1 DOM base64 OCR -> {c!r}")
                    return c
            except Exception as e:
                _log(f"[login] 路徑1 失敗: {e}")

        # 路徑 2：DOM element 截圖
        if page.evaluate(JS_FIND_CAPTCHA_IMG):
            wait_captcha_stable(page, SEL_CAPTCHA_IMG, tmp_path=self.captcha_tmp)
            c = solve_captcha(
                page,
                SEL_CAPTCHA_IMG,
                expected_len=5,
                alnum_only=True,
                min_confidence=HSBC_CAPTCHA_MIN_CONFIDENCE,
                tmp_path=self.captcha_tmp,
            )
            if c:
                _log(f"[login] 路徑2 截圖 OCR -> {c!r}")
                return c
        return None

    # ---------- 抓取 ----------
    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        """登入後抓信用卡：卡片清單(cards 或 legacy cards/suspend) + 逐卡明細。

        注意：HSBC 2026-07-09 實測已把卡片清單 endpoint 從 `cards/suspend`
        改成 `cards`。兩者 payload[] 都是各卡狀態；crawler 必須先認新端點再
        fallback 舊端點，否則會登入成功但 cards=[]，逐卡 posted/unposted 全不跑。
        明細用 cards/{id}/transactions/{posted,unposted} 直接 fetch。
        """
        out: dict = {}
        page.wait_for_timeout(7000)

        # 1) 卡片清單（dashboard 自動載入 cards；舊版曾用 cards/suspend）
        cards = self._card_list_payload(collector)
        out["cards"] = cards if isinstance(cards, list) else []
        _log(f"[collect] 卡片清單: {len(out['cards'])} 張")

        # 2) 逐卡點進去攔明細 API
        out["card_detail"] = self._collect_card_details(page, collector, out["cards"])

        out["_final_url"] = page.url
        out["_all_endpoints"] = sorted({
            h.url.split("?")[0].split("card.hsbc.com.tw/", 1)[-1]
            for h in collector.hits if h.resp_json and "card.hsbc.com.tw" in h.url
            and "/v3.1/" not in h.url
        })
        return BankCollectResult(**out)

    @staticmethod
    def _card_list_payload(collector: ResponseCollector):
        """取 HSBC 卡片清單 payload；先認 2026 新 endpoint `cards`，再 fallback 舊 `suspend`。

        HSBC 2026-07-09 browser evidence：dashboard 已改打
        `ibk-bff/api/v1/cards`。舊 crawler 只讀 `suspend` 會 silent cards=[]，
        導致 posted/unposted 完全不抓。
        """
        cards = HsbcCrawler._latest_payload(collector, "cards")
        if isinstance(cards, list):
            return cards
        return HsbcCrawler._latest_payload(collector, "suspend")

    @staticmethod
    def _latest_payload(collector: ResponseCollector, endpoint: str):
        """取某 endpoint 最新成功回應的 payload（HSBC 格式 {success, payload, error}）。"""
        hits = [h for h in collector.hits
                if h.endpoint == endpoint and isinstance(h.resp_json, dict)
                and h.resp_json.get("success")]
        if hits:
            return hits[-1].resp_json.get("payload")
        return None

    def _collect_card_details(self, page, collector, cards: list) -> dict:
        """直接用 page fetch 打明細 API（帶 Bearer token，繞過點卡片導航）。

        已知 endpoint（實機攔到）：
          GET cards/{id}                              單卡詳情
          GET cards/{id}/transactions/posted?pageSize=10   已出帳（分頁）
          GET cards/{id}/transactions/unposted?pageSize=   未出帳（直接陣列）
        ⚠️ 裸 fetch 回 HTTP 500——必須帶前端的 `Authorization: Bearer <JWT>`（從 collector 攔到）。
        卡 id 從卡片清單 payload[].id 取（新 endpoint `cards`；legacy `cards/suspend`）。
        """
        token = getattr(collector, "auth_token", "") or ""
        _log(f"[collect] auth token: {'有' if token else '無'}（{token[:20]}…）")
        details: dict = {}
        for c in cards:
            cid = c.get("id")
            masked = c.get("maskedCardNumber", "")
            tail = masked.replace("-", "")[-4:] if masked else cid
            if not cid:
                continue
            entry: dict = {"card_id": cid, "masked": masked}
            # 單卡詳情
            entry["detail"] = self._fetch_json(page, f"cards/{cid}", token)
            # 已出帳（翻頁抓全）
            entry["posted"] = self._fetch_paged(page, cid, "posted", token)
            # 未出帳（陣列）
            unp = self._fetch_json(page, f"cards/{cid}/transactions/unposted?pageSize=200", token)
            entry["unposted"] = unp if isinstance(unp, list) else (unp or [])
            details[tail] = entry
            n_posted = len(entry["posted"])
            n_unp = len(entry["unposted"])
            _log(f"[collect] 卡 {tail}({cid}): 已出帳 {n_posted} 筆, 未出帳 {n_unp} 筆")
        return details

    @staticmethod
    def _fetch_json(page, path: str, token: str = ""):
        """在瀏覽器內 fetch HSBC API，帶 Authorization Bearer token（裸 fetch 會 500）。回 payload。"""
        try:
            return page.evaluate(
                "async ({p, tok}) => { try {"
                " const h={'Accept':'application/json'};"
                " if(tok) h['Authorization']=tok;"
                " const r=await fetch('https://card.hsbc.com.tw/ibk-bff/api/v1/'+p,"
                "   {credentials:'include', headers:h});"
                " const j=await r.json(); return j && j.success ? j.payload : null;"
                " } catch(e){ return null; } }",
                {"p": path, "tok": token},
            )
        except Exception as e:
            _log(f"[fetch] {path} 失敗: {e}")
            return None

    def _fetch_paged(self, page, cid: str, kind: str, token: str = "") -> list:
        """已出帳明細翻頁抓全。posted 回 {pageInfo:{totalPages,currentPageIndex}, content:[]}。

        ⚠️ 分頁參數實測（2026-06-10）：HSBC 用 **`pageNumber`**（0-based），不是 `page`！
        `page`/`pageIndex`/`offset`/`size`/大 `pageSize` **全部無效**（HSBC 忽略，每次回同一批
        前 10 筆）→ 若用錯參數逐頁抓，會把同 10 筆抓 N 次（曾誤抓出 410 筆=41 頁×重複 10 筆）。
        正解：`?pageSize=10&pageNumber=M`，並**逐頁比對防重**（某頁內容與上頁全同就停，雙保險）。
        """
        page_size = 10  # HSBC 每頁固定 10 筆
        all_rows: list = []
        prev_sig = None
        # 先抓第 0 頁拿 totalPages
        first = self._fetch_json(
            page, f"cards/{cid}/transactions/{kind}?pageSize={page_size}&pageNumber=0", token)
        if not isinstance(first, dict):
            return first if isinstance(first, list) else []
        rows0 = first.get("content", []) or []
        all_rows.extend(rows0)
        prev_sig = self._page_sig(rows0)
        pi = first.get("pageInfo") or {}
        total_pages = pi.get("totalPages", 1) or 1
        # 安全上限：避免 totalPages 異常導致無限抓（每頁 10 筆，500 頁=5000 筆足夠）
        total_pages = min(total_pages, 500)
        for pn in range(1, total_pages):
            nxt = self._fetch_json(
                page, f"cards/{cid}/transactions/{kind}?pageSize={page_size}&pageNumber={pn}", token)
            rows = nxt.get("content", []) if isinstance(nxt, dict) else []
            if not rows:
                break  # 空頁 = 抓完
            sig = self._page_sig(rows)
            if sig == prev_sig:
                _log(f"[fetch] {kind} pageNumber={pn} 與上頁相同，停止翻頁（防重複抓）")
                break
            all_rows.extend(rows)
            prev_sig = sig
            page.wait_for_timeout(400)
        return all_rows

    @staticmethod
    def _page_sig(rows: list) -> str:
        """一頁內容的簽章（用於偵測「翻頁回到同一批」的假象）。"""
        return "|".join(
            f"{r.get('description','')}_{r.get('transactionDate','')}_{r.get('ntdAmount','')}"
            for r in rows
        )


if __name__ == "__main__":
    import json
    crawler = HsbcCrawler()
    result = crawler.run(login_url=BASE, headless=False)
    out_file = Path(__file__).resolve().parents[1] / "data" / "hsbc_collected.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"\n[done] 已存: {out_file}")
    data = result.get("data", {})
    _log("\n===== 抓取摘要 =====")
    if result.get("error"):
        _log(f"  error: {result['error']} url={result.get('final_url')}")
    _log(f"  卡片: {len(data.get('cards', []))} 張")
    for tail, apis in (data.get("card_detail") or {}).items():
        _log(f"  卡 {tail}: {list(apis.keys())}")
    _log(f"  攔到的 endpoint: {data.get('_all_endpoints', [])}")
