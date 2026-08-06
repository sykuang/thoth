#!/usr/bin/env python3
"""SinoPac Bank MMA personal e-banking crawler.

永豐 SinoPac MMA 個人網銀抓取器。

登入入口：https://mma.sinopac.com/MemberPortal/Member/MMALogin.aspx（ASP.NET WebForms）
流程：開頁 → 關 cookie bar → 標記 4 個 input（按 maxLength 區分）→ fill 三欄
      → OCR 驗證碼（純數字 6 碼，長度錯換圖重 OCR，送出前安全重試）
      → 點登入鈕 #MMA_Login → 等跳轉。

⚠️ 登入重試規則：
   銀行明確回 `captcha_invalid` 時，換圖後只重送 1 次；
   `credentials_invalid` 或無法分類的錯誤一律立刻停手，避免鎖帳號。

第一輪 collect 只先 dump endpoint，摸清 API 地圖再補 parse（家規：dump 真值不猜測）。
預設行為：headless browser → 預設 headless=True。
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.base import BankCollectResult, BankCrawler, ResponseCollector
from backend.core.creds import SinopacCreds
from backend.core.captcha import solve_captcha, wait_captcha_stable

BASE = "https://mma.sinopac.com/MemberPortal/Member/MMALogin.aspx"
LOAN_DETAIL_URL = "https://mma.sinopac.com/mma/bank/easy_index_loan/mma_detail.aspx"
SEL_CAP_IMG = "#imgCode"

JS_CLOSE_COOKIE = (
    "(() => { const b=[...document.querySelectorAll('button,a,div,span')]"
    ".find(e=>e.offsetParent!==null && /繼續使用|我知道了|同意|接受|關閉/.test((e.textContent||'').trim())"
    "  && (e.textContent||'').trim().length<8);"
    " if(b){ b.click(); return (b.textContent||'').trim(); } return ''; })()"
)

# ASP.NET 欄位 id 是動態 hash，用 maxLength 區分（11=身分證、20×2=代碼/密碼、6=驗證碼）
JS_TAG_INPUTS = r"""
(() => {
  const all = [...document.querySelectorAll('input')].filter(i=>i.offsetParent!==null);
  const sid = all.find(i=>i.maxLength===11);
  const m20 = all.filter(i=>i.maxLength===20);
  const cap = all.find(i=>i.maxLength===6) || document.querySelector('#sino_keyword3');
  if(sid) sid.setAttribute('data-role','sid');
  if(m20[0]) m20[0].setAttribute('data-role','user');
  if(m20[1]) m20[1].setAttribute('data-role','pwd');
  if(cap) cap.setAttribute('data-role','cap');
  return {
    sid: !!sid, user: !!m20[0], pwd: !!m20[1], cap: !!cap,
    sid_id: sid?sid.id:'', user_id: m20[0]?m20[0].id:'', pwd_id: m20[1]?m20[1].id:'', cap_id: cap?cap.id:'',
  };
})()
"""

# 點登入鈕（永豐專用 id）
JS_CLICK_LOGIN = (
    "(() => { const b=document.querySelector('#MMA_Login'); if(b){ b.click(); return true; }"
    " const cand=[...document.querySelectorAll('a,button')].find(e=>e.offsetParent!==null"
    "   && (e.textContent||'').trim()==='登入' && !e.closest('header,nav'));"
    " if(cand){ cand.click(); return true; } return false; })()"
)

# 登入後是否仍停在登入框（看 #imgCode 還在不在）
# NOTE (2026-06-17): _logged_in 已升級成 JS_LOGGED_IN_POSITIVE（4 條件 AND），
# 此 JS_STILL_LOGIN 保留供 OCR retry loop 內精準偵測「驗證碼錯，留在登入框」用。
JS_STILL_LOGIN = (
    "(() => { const i=document.querySelector('#imgCode');"
    " return !!(i && i.offsetParent!==null); })()"
)

# W (2026-06-17): positive signal 4 條件 AND，對齊 SCSB 鐵律
# 1) urlOk: MyMMA / Myasset / mma_ 等登入後路徑（非 MMALogin.aspx）
# 2) lenOk: innerText >= 500 (內銀區滿載)
# 3) kw >= 2 (內銀區關鍵字)
# 4) noLoginForm: #imgCode + maxLength 6 input 都不可見
# 任一 fail → 視為未登入。
JS_LOGGED_IN_POSITIVE = """
() => {
  const url = location.href.toLowerCase();
  const urlOk = /\\/mymma\\/|\\/myasset\\/|\\/mma_|memberportal\\/main/.test(url)
    && !/mmalogin\\.aspx/.test(url);
  const visible = (e) => {
    if (!e) return false;
    const r = e.getBoundingClientRect();
    const cs = getComputedStyle(e);
    return !!(r.width || r.height || e.getClientRects().length)
      && cs.display !== 'none' && cs.visibility !== 'hidden';
  };
  const noLoginForm = !visible(document.querySelector('#imgCode'))
    && ![...document.querySelectorAll('input')].some(i => i.maxLength === 6 && visible(i));
  const body = document.body?.innerText || document.body?.textContent || '';
  const lenOk = body.length >= 500;
  const KW = ['資產總覽','資產分析','存款','轉帳','信用卡','登出',
              '台幣','外幣','基金','投資','貸款','繳費','個人設定','安全','MMA'];
  const kw = KW.filter(k => body.includes(k)).length;
  return urlOk && lenOk && kw >= 2 && noLoginForm;
}
"""

# 抓登入框周邊錯誤訊息
JS_ERR_MSG = (
    "(() => { const txt=[...document.querySelectorAll('div,span,p,label,td')]"
    ".filter(e=>e.offsetParent!==null)"
    ".map(e=>(e.textContent||'').trim())"
    ".filter(t=>t && t.length<60 && /錯誤|不正確|失敗|重新|鎖|無效|請輸入正確|驗證碼|密碼|身分證|代碼|嘗試/.test(t));"
    " return [...new Set(txt)].slice(0,8); })()"
)


def _log(*a):
    print(*a, file=sys.stderr)


class SinopacLoginError(RuntimeError):
    """永豐登入失敗，附可供 retry/UI 判斷的 machine-readable code。"""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"[{code}] {message}")


class SinopacCrawler(BankCrawler):
    CAPTCHA_INVALID = "captcha_invalid"
    CREDENTIALS_INVALID = "credentials_invalid"
    LOGIN_FAILED = "login_failed"
    CAPTCHA_ERROR_KEYWORDS = (
        "驗證碼失效",
        "驗證碼錯誤",
        "驗證碼輸入錯誤",
        "請重新輸入驗證碼",
    )
    CREDENTIAL_ERROR_KEYWORDS = (
        "使用者代碼或網路密碼錯誤",
        "帳號或密碼錯誤",
        "密碼不正確",
        "密碼無效",
        "身分證字號錯誤",
    )

    def __init__(self):
        super().__init__(name="sinopac")
        self.creds = SinopacCreds.load()
        self._last_dialog_message = ""
        self._last_dialog_type = ""

    def _host_filter(self) -> str:
        return "sinopac.com"

    def _logged_in(self, page) -> bool:
        try:
            return bool(page.evaluate(JS_LOGGED_IN_POSITIVE))
        except Exception:
            return False

    def _is_captcha_login_error(self, message: str | None) -> bool:
        text = message or ""
        return bool(text) and any(k in text for k in self.CAPTCHA_ERROR_KEYWORDS)

    def _message_error_code(self, message: str | None) -> str:
        text = message or ""
        if any(k in text for k in self.CREDENTIAL_ERROR_KEYWORDS):
            return self.CREDENTIALS_INVALID
        if self._is_captcha_login_error(text):
            return self.CAPTCHA_INVALID
        return self.LOGIN_FAILED

    def _login_error_code(self, dialog_message: str, errors: list[str]) -> str:
        if dialog_message:
            return self._message_error_code(dialog_message)
        codes = [self._message_error_code(message) for message in errors]
        if self.CREDENTIALS_INVALID in codes:
            return self.CREDENTIALS_INVALID
        if self.CAPTCHA_INVALID in codes:
            return self.CAPTCHA_INVALID
        return self.LOGIN_FAILED

    # ---------- 登入 ----------
    # 註：attach_dialog_handler 已升格到 base.py 預設實作（永豐踩出來的鐵律）。
    # 永豐額外記錄最新 dialog message，讓明確 captcha error 可安全重試一次。
    def attach_dialog_handler(self, page) -> None:
        def _on_dialog(d):
            msg = (d.message or "")[:200]
            self._last_dialog_type = d.type
            self._last_dialog_message = msg
            try:
                print(f"[sinopac][dialog] {d.type} msg={msg!r} -> accept", file=sys.stderr)
                d.accept()
            except Exception as e:
                print(f"[sinopac][dialog] handle 失敗: {e}", file=sys.stderr)
        page.on("dialog", _on_dialog)

    def login(self, page) -> bool:
        """銀行明確回 captcha error 時換圖重試一次，帳密或未知錯誤停手。"""
        page.wait_for_timeout(8000)
        _log(f"[login] 起始 url={page.url}")

        cc = page.evaluate(JS_CLOSE_COOKIE)
        if cc:
            _log(f"[login] 關 cookie bar: {cc!r}")
            page.wait_for_timeout(1000)

        try:
            page.wait_for_selector(SEL_CAP_IMG, state="visible", timeout=10000)
        except Exception as e:
            raise SinopacLoginError(
                self.LOGIN_FAILED, f"永豐登入表單載入失敗: {e}; url={page.url}",
            ) from e

        for submit_attempt in range(1, 3):
            tagged = page.evaluate(JS_TAG_INPUTS)
            _log(f"[login] 標記欄位: {tagged}")
            if not all(tagged.get(k) for k in ("sid", "user", "pwd", "cap")):
                raise SinopacLoginError(self.LOGIN_FAILED, "永豐登入表單欄位不完整")

            try:
                page.fill("input[data-role='sid']", self.creds.national_id)
                page.wait_for_timeout(200)
                page.fill("input[data-role='user']", self.creds.user_code)
                page.wait_for_timeout(200)
                page.fill("input[data-role='pwd']", self.creds.password)
                page.wait_for_timeout(300)
            except Exception as e:
                raise SinopacLoginError(self.LOGIN_FAILED, f"永豐登入欄位填寫失敗: {e}") from e

            captcha = self._ocr_with_regen(page, max_attempts=5)
            if not captcha:
                raise SinopacLoginError(
                    self.CAPTCHA_INVALID, "永豐驗證碼辨識失敗（送出前已換圖 5 次）",
                )
            try:
                page.fill("input[data-role='cap']", captcha)
                page.wait_for_timeout(300)
            except Exception as e:
                raise SinopacLoginError(self.LOGIN_FAILED, f"永豐驗證碼欄位填寫失敗: {e}") from e

            self._last_dialog_message = ""
            _log(f"[login] 送出 login attempt={submit_attempt}/2 (captcha={captcha})")
            if not page.evaluate(JS_CLICK_LOGIN):
                raise SinopacLoginError(self.LOGIN_FAILED, "永豐登入按鈕不存在")
            page.wait_for_timeout(8000)

            if self._logged_in(page):
                _log(f"[login] ✅ 登入成功 -> {page.url}")
                return True

            try:
                errors = page.evaluate(JS_ERR_MSG)
            except Exception:
                errors = []
            dialog_message = self._last_dialog_message
            error_code = self._login_error_code(dialog_message, errors)
            if error_code == self.CAPTCHA_INVALID and submit_attempt == 1:
                _log("[login] bank error_code=captcha_invalid，換圖重試一次")
                page.evaluate("(()=>{const i=document.querySelector('#imgCode'); if(i) i.click();})()")
                page.wait_for_timeout(1500)
                continue

            from backend.banks._login_debug import snapshot as _login_snapshot
            snap = _login_snapshot(page)
            if error_code == self.CREDENTIALS_INVALID:
                reason = "銀行回覆帳號或密碼錯誤；未重試。"
            elif error_code == self.CAPTCHA_INVALID:
                reason = "銀行第二次仍回覆驗證碼錯誤；停止重試。"
            else:
                reason = "銀行未提供可分類的登入錯誤；未重試。"
            messages = [message for message in [dialog_message, *errors] if message]
            msg = (
                f"永豐登入失敗。\n  url={page.url}\n  錯誤訊息: {messages}\n"
                f"  可能原因：{reason}\n{snap}"
            )
            _log(f"[login] ❌ error_code={error_code} {msg}")
            raise SinopacLoginError(error_code, msg)

        raise AssertionError("unreachable")

    def _ocr_with_regen(self, page, max_attempts=5):
        """OCR 驗證碼，長度錯換圖重 OCR（送出前安全重試，不碰 login）。"""
        for n in range(1, max_attempts+1):
            wait_captcha_stable(page, SEL_CAP_IMG, tmp_path=self.captcha_tmp)
            text = solve_captcha(
                page, SEL_CAP_IMG, expected_len=6, alnum_only=True, digits_only=True,
                min_confidence=0.98,
                tmp_path=self.captcha_tmp,
            )
            if text and len(text) == 6 and text.isdigit():
                _log(f"[cap] 第 {n} 次 OCR 成功: {text}")
                return text
            _log(f"[cap] 第 {n}/{max_attempts} 次 OCR 失敗（讀到 {text!r}），換圖")
            if n < max_attempts:
                try:
                    page.evaluate("(()=>{const i=document.querySelector('#imgCode'); if(i) i.click();})()")
                    page.wait_for_timeout(1500)
                except Exception as e:
                    _log(f"[cap] 換圖失敗: {e}")
        return None

    # ---------- 抓取 ----------
    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        """登入後抓帳戶餘額 / 貸款明細 / 信用卡彙總與帳單 / 全卡片 / 資產分析。

        永豐 MMA 資產總覽頁登入後自動觸發 ws_bankbal/cardsum/cardbilling_sp/
        AllCards/ws_mychart，巡訪「資產分析 / 信用卡總覽」頁也會補打。
        台幣交易明細 dropdown 是 jQuery 客製化元件（#divDebitAccount），待下次破。
        """
        out: dict = {}
        page.wait_for_timeout(5000)

        # 關掉登入後可能的提醒 modal
        page.evaluate(
            "(() => { for(let i=0;i<6;i++){"
            "  const b=[...document.querySelectorAll('button,a,div')].find(e=>e.offsetParent!==null"
            "    && /^(Confirm|OK|確認|我知道了|關閉|稍後|下次|略過|確定|不再顯示|同意|繼續|下一步)$/i.test((e.textContent||'').trim()));"
            "  if(b) b.click(); else break; } })()",
        )
        page.wait_for_timeout(3000)

        # 巡訪「資產分析 / 信用卡總覽」頁觸發更多 API
        for url in [
            "https://mma.sinopac.com/MyMMA/Myasset/mma_assets_analysis.aspx",
            "https://mma.sinopac.com/mma/mymma/myasset/cards_summary.aspx",
        ]:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(6000)
            except Exception as e:
                _log(f"[collect] goto {url} 失敗: {e}")

        # 從 collector 取已攔到的 API JSON
        out["bank_balance"] = self._latest_json(collector, "ws_bankbal.ashx")        # 銀行帳戶餘額 list
        out["debit_accounts"] = self._latest_json(collector, "ws_debitacct.ashx")    # 扣款帳戶清單
        out["card_summary"] = self._latest_json(collector, "ws_cardsum.ashx")        # 信用卡彙總
        out["card_billing"] = self._latest_json(collector, "ws_cardbilling_sp.ashx") # 信用卡 3 個月帳單
        out["all_cards"] = self._latest_json(collector, "AllCards")                  # 全卡清單
        out["asset_chart"] = self._latest_json(collector, "ws_mychart.ashx")         # 資產分佈圓餅
        out["alert_info"] = self._latest_json(collector, "ws_alertinfo.ashx")        # 帳戶通知

        # === 貸款明細：每個貸款帳號查本金餘額 / 利率 / 到期日 ===
        out["loan"] = self._collect_loans(page, collector)

        # === 台幣交易明細（jQuery dropdown 破解後，每帳戶查 1 次）===
        out["twd_transactions"] = self._collect_transactions(page, collector)

        # === 信用卡明細：帳單已請款（StatementInquiry HTML）+ 未請款（UnbilledTxInquiry API）===
        out["card_statements"] = self._collect_card_statements(page)
        out["card_unbilled"] = self._collect_card_unbilled(page, collector)

        # 偵測尚未抓到的（log 給 debug 用）
        miss = [k for k, v in out.items() if v is None]
        if miss:
            _log(f"[collect] 未攔到: {miss}")

        out["_final_url"] = page.url
        out["_all_endpoints"] = sorted({h.endpoint for h in collector.hits if h.resp_json})
        return BankCollectResult(**out)

    def _collect_loans(self, page, collector: ResponseCollector) -> dict:
        """逐帳號觸發 ws_loaninfo，回傳銀行原生貸款明細。"""
        page.goto(LOAN_DETAIL_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(8000)

        account_raw = self._latest_json(collector, "ws_loanaccount.ashx")
        if not (isinstance(account_raw, list) and account_raw
                and isinstance(account_raw[0], dict)
                and isinstance(account_raw[0].get("SubInfo"), list)):
            raise RuntimeError("永豐貸款帳號 API 未回傳預期結構")
        accounts = account_raw[0]["SubInfo"]
        details = []
        for account in accounts:
            if not isinstance(account, dict):
                raise RuntimeError("永豐貸款帳號資料格式錯誤")
            account_no = account.get("AcctValue")
            formatted = account.get("AcctValueFormat")
            if not account_no or not formatted:
                raise RuntimeError("永豐貸款帳號缺少 AcctValue/AcctValueFormat")

            before = len(collector.by_endpoint("ws_loaninfo.ashx"))
            clicked = page.evaluate(
                """args => {
                  const account = document.querySelector('#AcctValue');
                  const formatted = document.querySelector('#AcctValueFormat');
                  const button = document.querySelector('#btnQuery');
                  if (!account || !formatted || !button) return false;
                  account.value = args.account;
                  formatted.value = args.formatted;
                  for (const el of [account, formatted]) {
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                  }
                  button.click();
                  return true;
                }""",
                {"account": account_no, "formatted": formatted},
            )
            if not clicked:
                raise RuntimeError("永豐貸款查詢控制項不存在")
            page.wait_for_timeout(5000)

            hits = collector.by_endpoint("ws_loaninfo.ashx")[before:]
            matching_hits = []
            for hit in hits:
                if not isinstance(hit.req_body, str):
                    continue
                params = parse_qs(hit.req_body, keep_blank_values=True)
                if (params.get("AcctValue") == [account_no]
                        and params.get("AcctValueFormat") == [formatted]):
                    matching_hits.append(hit)
            if not matching_hits:
                raise RuntimeError("永豐貸款查詢未收到對應 API 回應")
            hit = matching_hits[-1]
            if not 200 <= hit.status < 300:
                raise RuntimeError("永豐貸款明細 API HTTP 回應失敗")
            info_raw = hit.resp_json
            if not (isinstance(info_raw, list) and info_raw
                    and isinstance(info_raw[0], dict)
                    and isinstance(info_raw[0].get("SubInfo"), list)):
                raise RuntimeError("永豐貸款明細 API 未回傳預期結構")
            body = info_raw[0]
            records = body["SubInfo"]
            required = ("LoanKind", "Currency", "LoanBalance")
            if body.get("Message") or not records or any(
                not isinstance(record, dict)
                or any(record.get(key) in (None, "") for key in required)
                for record in records
            ):
                raise RuntimeError("永豐貸款明細缺少必要欄位或銀行回覆失敗")
            details.append({
                "account": account_no,
                "records": records,
            })
        return {"details": details, "fetch_ok": True}

    def _collect_transactions(self, page, collector) -> list:
        """進往來明細頁、每個帳戶設 hidden inputs + 按 #btnQuery、攔 ws_transdetailMerge.ashx。

        永豐自家 jQuery dropdown #divDebitAccount 點不開（沒掛 click handler），
        但 6 個 hidden inputs (Acct/AcctValue/AcctName/Curr/CurrName/QueryType) 設了
        就生效。從 ws_debitacct.ashx 拿到的 DataValue 直接設進去即可。
        """
        results = []
        # 先進往來明細頁觸發 ws_debitacct（assets_summary 沒打）
        try:
            page.goto("https://mma.sinopac.com/mma/bank/transdetail/mma_transdetail.aspx",
                       wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(8000)
        except Exception as e:
            _log(f"[twd] goto 失敗: {e}")
            return results

        # 進頁後 ws_debitacct 才會打
        debit = self._latest_json(collector, "ws_debitacct.ashx")
        if not (debit and isinstance(debit, list) and len(debit) > 0):
            _log("[twd] ws_debitacct 沒攔到，跳過")
            return results
        sub = debit[0].get("SubInfo", []) if isinstance(debit[0], dict) else []
        if not sub:
            _log("[twd] ws_debitacct SubInfo 空，跳過")
            return results
        _log(f"[twd] 共 {len(sub)} 個帳戶")

        for acct in sub:
            dv = acct.get("DataValue", "")
            dt = acct.get("DataText", "")
            dt_short = (acct.get("DisplayText") or "TWD")
            _log(f"[twd] 查帳戶 {dt[:40]}")

            # 設 6 個 hidden inputs（從 probe 驗證有效）
            set_js = """
            ((args) => {
              const dv = args.dv, dt = args.dt, cur = args.cur;
              const out = [];
              const span = document.querySelector('#spanDebitAccount');
              if(span) { span.textContent = dt; }
              const targets = ['Acct','AcctValue','AcctName','Curr','CurrName','QueryType','TextType'];
              for(const tn of targets){
                const el = document.getElementById(tn) || document.querySelector('[name='+tn+']');
                if(el){
                  let val = dv;
                  if(tn === 'AcctName') val = dt;
                  if(tn === 'Curr' || tn === 'CurrName') val = cur;
                  if(tn === 'QueryType') val = '3';
                  if(tn === 'TextType') val = '';
                  el.value = val;
                  el.dispatchEvent(new Event('change', {bubbles: true}));
                }
              }
              return 'set';
            })
            """
            try:
                page.evaluate(set_js, {"dv": dv, "dt": dt, "cur": dt_short})
                page.wait_for_timeout(800)
                # 按 #btnQuery
                page.evaluate("(() => { const b=document.querySelector('#btnQuery'); if(b) b.click(); })()")
                page.wait_for_timeout(7000)
                # 攔最新的 ws_transdetailMerge.ashx
                hit = collector.latest("ws_transdetailMerge.ashx")
                if hit and hit.resp_json:
                    body = hit.resp_json[0] if isinstance(hit.resp_json, list) and hit.resp_json else hit.resp_json
                    results.append({
                        "account": dv,
                        "account_name": dt,
                        "currency": dt_short,
                        "header": body.get("Header") if isinstance(body, dict) else None,
                        "message": body.get("Message") if isinstance(body, dict) else None,
                        "records": body.get("SubInfo", []) if isinstance(body, dict) else [],
                    })
                    _log(f"  → 抓到 {len(results[-1]['records'])} 筆")
            except Exception as e:
                _log(f"  [twd] 帳戶 {dv} 查詢失敗: {e}")
        return results

    # ---------- 信用卡明細 ----------
    def _collect_card_statements(self, page) -> list:
        """信用卡帳單已請款明細（SinoCard/Account/StatementInquiry）。

        此頁 SSR：整頁 HTML 含 12 個月切換按鈕 + 當月所有消費紀錄。每月一次 goto 即可。
        我們抓最近 3 個月。回傳 list[{month, html_text, records}]。
        """
        results = []
        try:
            page.goto("https://mma.sinopac.com/SinoCard/Account/StatementInquiry",
                      wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(5000)
        except Exception as e:
            _log(f"[card_stmt] goto 失敗: {e}")
            return results

        # 抓「快速查詢」3 個月按鈕——通常是當月與前兩月
        quick_months_raw = page.evaluate("""() => {
            const tds = Array.from(document.querySelectorAll('td, a, span, button, li'));
            return tds.map(e => (e.innerText||'').trim())
              .filter(t => /^20\\d{2}\\/\\d{2}$/.test(t))
              .slice(0, 12);
        }""")
        # 去重：快速查詢和下方 dropdown 各列一份，但只取最近 3 個 unique month
        seen = set()
        quick_months = []
        for m in quick_months_raw:
            if m not in seen:
                seen.add(m)
                quick_months.append(m)
            if len(quick_months) >= 3:
                break
        _log(f"[card_stmt] 可選月份 (unique): {quick_months}")

        # 抓當前頁面（預設顯示最新月份）的整個內文
        def _grab_month(month_label: str | None) -> dict:
            content_text = page.evaluate("""() => document.body.innerText""")
            # 用 regex 抓「消費記錄」表格區塊
            import re
            recs = []
            # 每筆形如: YYYY/MM/DD\tYYYY/MM/DD\t末四碼4\t說明\t金額\t...
            pat = re.compile(
                r"(20\d{2}/\d{2}/\d{2})\t(20\d{2}/\d{2}/\d{2})\t(\d{4})\t([^\t\n]+?)\t(-?[\d,]+)",
                re.MULTILINE,
            )
            for m in pat.finditer(content_text):
                recs.append({
                    "trans_date": m.group(1),
                    "post_date": m.group(2),
                    "card_last4": m.group(3),
                    "description": m.group(4).strip(),
                    "amount": m.group(5).replace(",", ""),
                })
            # 抓帳單彙總
            sum_pat = re.search(
                r"臺幣\t([\d,]+)\t([\d,]+)\t([\d,]+)\t([\d,]+)\t([\d,]+)\t([\d,]+)\t([\d,]+)",
                content_text,
            )
            summary = None
            if sum_pat:
                summary = {
                    "currency": "TWD",
                    "last_billed": sum_pat.group(1).replace(",", ""),
                    "paid": sum_pat.group(2).replace(",", ""),
                    "new_charges": sum_pat.group(3).replace(",", ""),
                    "revolving_interest": sum_pat.group(4).replace(",", ""),
                    "penalty": sum_pat.group(5).replace(",", ""),
                    "current_due": sum_pat.group(6).replace(",", ""),
                    "min_due": sum_pat.group(7).replace(",", ""),
                }
            # 抓結帳日 / 繳款截止日
            due_pat = re.search(r"結帳日：(\d{4}/\d{2}/\d{2})\s*繳款截止日：(\d{4}/\d{2}/\d{2})", content_text)
            return {
                "month": month_label,
                "billing_cycle_date": due_pat.group(1) if due_pat else None,
                "payment_due_date": due_pat.group(2) if due_pat else None,
                "summary": summary,
                "records": recs,
                "record_count": len(recs),
            }

        # 目前月（預設展示）
        results.append(_grab_month(quick_months[0] if quick_months else None))
        # 切換到前兩個月（如果有）
        for label in quick_months[1:3]:
            try:
                page.get_by_text(label, exact=True).first.click(timeout=4000)
                page.wait_for_timeout(4500)
                results.append(_grab_month(label))
            except Exception as e:
                _log(f"[card_stmt] 切換 {label} 失敗: {e}")

        total = sum(r.get("record_count", 0) for r in results)
        _log(f"[card_stmt] 抓 {len(results)} 個月、共 {total} 筆消費紀錄")
        return results

    def _collect_card_unbilled(self, page, collector) -> dict:
        """信用卡未請款明細（SinoCard/Account/UnbilledTxInquiry）。

        此頁靠 POST API：LatestTx（最新交易）+ OutstandingDetail（已請款合計）。
        collector 攔 sinopac.com 全域，已自動收下。
        """
        try:
            page.goto("https://mma.sinopac.com/SinoCard/Account/UnbilledTxInquiry",
                      wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(7000)
        except Exception as e:
            _log(f"[card_unbilled] goto 失敗: {e}")
            return {}

        latest = self._latest_json(collector, "LatestTx")
        outstand = self._latest_json(collector, "OutstandingDetail")
        out = {
            "latest_tx": latest,
            "outstanding_detail": outstand,
        }
        n_latest = 0
        if isinstance(latest, dict):
            items = (latest.get("Result") or {}).get("Items", [])
            n_latest = len(items) if isinstance(items, list) else 0
        n_outstand = 0
        if isinstance(outstand, dict):
            detail = (outstand.get("Result") or {}).get("Detail", [])
            n_outstand = len(detail) if isinstance(detail, list) else 0
        _log(f"[card_unbilled] LatestTx={n_latest} 筆, OutstandingDetail={n_outstand} 筆")
        return out

    @staticmethod
    def _latest_json(collector: ResponseCollector, endpoint: str):
        """取某 endpoint 最新一次回應的 JSON body（dict 或 list）。"""
        hit = collector.latest(endpoint)
        return hit.resp_json if hit else None


if __name__ == "__main__":
    import json
    crawler = SinopacCrawler()
    try:
        result = crawler.run(login_url=BASE, headless=True)
    except SinopacLoginError as e:
        result = {"error": "login_failed_stop", "detail": str(e)}

    out_file = Path(__file__).resolve().parents[1] / "data" / "sinopac_collected.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"\n[done] 已存: {out_file}")

    if result.get("error"):
        _log(f"  ❌ error: {result['error']}")
        if result.get("detail"):
            _log(f"  detail: {result['detail'][:300]}")
    else:
        data = result.get("data", {})
        _log("\n===== 抓取摘要 =====")
        _log(f"  最終 url: {data.get('_final_url')}")
        bb = data.get("bank_balance") or []
        n_acct = sum(len(s.get("SubInfo", [])) for s in bb if isinstance(s, dict))
        _log(f"  銀行帳戶: {n_acct} 個")
        cs = data.get("card_summary") or []
        _log(f"  信用卡彙總: {len(cs) if isinstance(cs, list) else 0} 筆")
        ac = data.get("all_cards") or {}
        if isinstance(ac, dict):
            items = (ac.get("Result") or {}).get("Items", [])
            _log(f"  全卡片: {len(items)} 張")
        _log(f"  攔到 endpoint: {data.get('_all_endpoints', [])}")
