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

import re
import sys
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.base import BankCollectResult, BankCrawler, ResponseCollector
from backend.core.card_bills import (
    card_bill_date,
    card_bill_money,
    make_card_bill_fact,
    publish_card_bill_facts,
)
from backend.core.creds import UbotCreds
from backend.core.captcha import solve_captcha, wait_captcha_stable
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointPhase,
    LoginCheckpointRule,
)

BASE = "https://www.ubot.com.tw/home"

SEL_SID = "#sid"           # 身分證字號 (text)
SEL_NICK = "#nickname"     # 使用者代號 (password)
SEL_PWD = "#password"      # 網路密碼 (password, maxlen=12)
SEL_CAPTCHA = "#CAPTCHA"   # 圖形驗證碼 (tel)
SEL_CAPTCHA_IMG = "img[alt='CAPTCHA']"  # base64 jpeg 170x50

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


_POST_PHASES = (
    CheckpointPhase.POST_SUBMIT,
    CheckpointPhase.POST_SUBMIT_SETTLE,
)
_REQUIRED_PASSWORD_PATTERN = re.compile(
    r"^[\s\S]*(?:密碼已過期|必須變更密碼|required[\s\S]{0,80}\bchang(?:e|ing)\b)[\s\S]*$",
    re.IGNORECASE,
)
_OTP_PATTERN = re.compile(
    r"^[\s\S]*(?:\bOTP\b|(?:簡訊|一次性|動態)驗證碼|device\s+verification)[\s\S]*$",
    re.IGNORECASE,
)
_OPTIONAL_PASSWORD_PATTERN = re.compile(
    r"^(?![\s\S]*(?:密碼已過期|必須變更密碼|required[\s\S]{0,80}\bchang(?:e|ing)\b))"
    r"[\s\S]*(?:建議[\s\S]{0,80}變更密碼|"
    r"變更密碼[\s\S]{0,80}超過\s*(?:6|六)\s*個月|"
    r"超過\s*(?:6|六)\s*個月[\s\S]{0,80}(?:變更)?密碼|"
    r"recommend(?:ed)?[\s\S]{0,80}\bchang(?:e|ing)\b[\s\S]{0,40}password|"
    r"password[\s\S]{0,80}over\s+(?:6|six)\s+months|"
    r"over\s+(?:6|six)\s+months[\s\S]{0,80}password)[\s\S]*$",
    re.IGNORECASE,
)


def _unique_visible_enabled_exact(page, selector: str, label: str):
    try:
        candidates = page.locator(selector)
        matches = []
        for index in range(candidates.count()):
            candidate = candidates.nth(index)
            if (
                candidate.is_visible()
                and candidate.is_enabled()
                and " ".join(candidate.inner_text().split()) == label
            ):
                matches.append(candidate)
        return matches[0] if len(matches) == 1 else None
    except Exception:
        return None


def _any_visible(page, selector: str) -> bool:
    candidates = page.locator(selector)
    return any(
        candidates.nth(index).is_visible()
        for index in range(candidates.count())
    )


class UbotLoginError(RuntimeError):
    """聯邦 UBOT login 送出後失敗——立刻中止，絕不自動重打（防鎖帳號）。"""


def _ubot_card_bill_fact(out: dict):
    limits = (out.get("card_limit") or {}).get("CardList") or []
    summaries = (out.get("card_summary") or {}).get("CardList") or []
    limit_summary = limits[0] if limits and isinstance(limits[0], dict) else {}
    card_summary = summaries[0] if summaries and isinstance(summaries[0], dict) else {}
    summary = {**card_summary, **limit_summary}

    pay_records = []
    history = out.get("card_pay_history") or {}
    if isinstance(history, dict):
        for key in ("DateList", "PayList", "payList", "records"):
            if isinstance(history.get(key), list):
                pay_records = [row for row in history[key] if isinstance(row, dict)]
                break
    latest_pay = max(
        pay_records,
        default={},
        key=lambda row: str(row.get("postDate") or row.get("effectDate") or ""),
    )
    payment_amount = latest_pay.get("payAmt")
    payment_date = latest_pay.get("postDate") or latest_pay.get("effectDate")
    if card_bill_money(payment_amount) is None or card_bill_date(payment_date) is None:
        payment_amount = summary.get("lastPayAmt")
        payment_date = summary.get("lastPayDate")
    if card_bill_money(payment_amount) is None or card_bill_date(payment_date) is None:
        payment_amount = None
        payment_date = None

    return make_card_bill_fact(
        remaining_due=summary.get("payAmt"),
        payment_due_date=summary.get("dueDate"),
        last_payment_amount=payment_amount,
        last_payment_date=payment_date,
    )


class UbotCrawler(BankCrawler):
    USES_SHARED_LOGIN_CHECKPOINTS: ClassVar[bool] = True

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
            return bool(r.get("ok"))
        return bool(r)

    # ---------- 登入 ----------
    def login(self, page) -> bool:
        return self._shared_login(page)

    def prepare_login_page(self, page) -> None:
        try:
            page.wait_for_timeout(8000)
        except Exception:
            return
        if self._logged_in(page):
            return

        opener = _unique_visible_enabled_exact(page, "button, a", "網銀登入")
        if opener is not None:
            try:
                opener.click()
            except Exception:
                return
        try:
            page.wait_for_timeout(2500)
        except Exception:
            return

        try:
            sid_visible = _any_visible(page, SEL_SID)
        except Exception:
            return
        if sid_visible:
            return

        tab = _unique_visible_enabled_exact(
            page,
            "a, button, [role=tab]",
            "個人用戶登入",
        )
        if tab is not None:
            try:
                tab.click()
            except Exception:
                return
        try:
            page.wait_for_timeout(800)
        except Exception:
            return

    def is_authenticated(self, page) -> bool:
        return self._logged_in(page)

    def login_checkpoint_rules(self) -> tuple[LoginCheckpointRule, ...]:
        return (
            LoginCheckpointRule(
                name="ubot-password-change-required",
                bank="ubot",
                phases=_POST_PHASES,
                kind=CheckpointKind.PASSWORD_CHANGE_REQUIRED,
                container_selector=".modal.show",
                required_body_pattern=_REQUIRED_PASSWORD_PATTERN,
            ),
            LoginCheckpointRule(
                name="ubot-otp-required",
                bank="ubot",
                phases=_POST_PHASES,
                kind=CheckpointKind.OTP_REQUIRED,
                container_selector=".modal.show",
                required_body_pattern=_OTP_PATTERN,
            ),
            LoginCheckpointRule(
                name="ubot-password-change-optional",
                bank="ubot",
                phases=_POST_PHASES,
                kind=CheckpointKind.PASSWORD_CHANGE_OPTIONAL,
                container_selector=".modal.show",
                action_texts=(
                    "以後再說",
                    "暫不變更",
                    "不變更",
                    "下次再說",
                    "Later",
                    "Skip",
                ),
                required_body_pattern=_OPTIONAL_PASSWORD_PATTERN,
            ),
            LoginCheckpointRule(
                name="ubot-unknown-modal",
                bank="ubot",
                phases=_POST_PHASES,
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector=".modal.show",
            ),
            LoginCheckpointRule(
                name="ubot-login-form-still-visible",
                bank="ubot",
                phases=_POST_PHASES,
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector=SEL_SID,
            ),
        )

    def submit_credentials_once(self, page) -> None:
        try:
            page.wait_for_selector(SEL_SID, state="visible", timeout=10000)
            for selector, value, wait in (
                (SEL_SID, self.creds.national_id, 150),
                (SEL_NICK, self.creds.user_code, 150),
                (SEL_PWD, self.creds.password, 200),
            ):
                page.fill(selector, value)
                page.wait_for_timeout(wait)
        except Exception:
            raise UbotLoginError("登入欄位無法安全填寫；未送出登入") from None

        captcha = self._ocr_with_regen(page, max_attempts=5)
        if not captcha:
            raise UbotLoginError("圖形驗證碼 OCR 失敗；未送出登入")
        try:
            page.fill(SEL_CAPTCHA, captcha)
            page.wait_for_timeout(200)
        except Exception:
            raise UbotLoginError("驗證碼欄位無法安全填寫；未送出登入") from None

        button = _unique_visible_enabled_exact(
            page,
            "button.ubot-primary-green",
            "登入",
        )
        if button is None:
            raise UbotLoginError("找不到唯一且可操作的登入按鈕；未送出登入")
        try:
            classes = (button.get_attribute("class") or "").split()
            if "disabled" in classes:
                raise UbotLoginError("找不到唯一且可操作的登入按鈕；未送出登入")
        except UbotLoginError:
            raise
        except Exception:
            raise UbotLoginError("無法安全確認登入按鈕；未送出登入") from None

        _log("[login] 送出 login attempt=1")
        try:
            button.click()
        except Exception:
            raise UbotLoginError("登入送出狀態不明；禁止自動重試") from None

        try:
            page.wait_for_timeout(6000)
            for _ in range(20):
                page.wait_for_timeout(1000)
                if self._logged_in(page) or _any_visible(page, ".modal.show"):
                    return
        except Exception:
            return

    def _ocr_with_regen(self, page, max_attempts=5):
        """OCR 聯邦 6 碼純數字驗證碼，失敗換圖重試（送出前安全重試）。"""
        for attempt in range(1, max_attempts + 1):
            try:
                wait_captcha_stable(page, SEL_CAPTCHA_IMG, tmp_path=self.captcha_tmp)
                captcha = solve_captcha(
                    page,
                    SEL_CAPTCHA_IMG,
                    expected_len=6,
                    alnum_only=True,
                    digits_only=True,
                    min_confidence=0.85,
                    tmp_path=self.captcha_tmp,
                )
            except Exception:
                _log(f"[cap] OCR attempt={attempt} status=error")
                return None
            if captcha and len(captcha) == 6 and captcha.isdigit():
                _log(f"[cap] OCR attempt={attempt} status=success")
                return captcha
            _log(f"[cap] OCR attempt={attempt} status=invalid")
            if attempt == max_attempts:
                break
            refresh = _unique_visible_enabled_exact(
                page,
                "div,a,span,button,i",
                "重新產生",
            )
            if refresh is None:
                return None
            try:
                refresh.click()
                page.wait_for_timeout(2000)
            except Exception:
                return None
        return None

    # ---------- 抓取 ----------
    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        """登入後依序抓：帳戶總覽(餘額) → 台幣交易明細 → 信用卡帳單/明細。

        聯邦後端 www.ubot.com.tw/MyBank/IBK{模組}{編號}，明文 POST {sid, sessionId, ...}。
        互動式：餘額/卡彙總進頁自動打；逐筆明細要選帳戶/期別 + 按查詢才觸發。
        """
        out: dict = {}

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

        publish_card_bill_facts(out, [_ubot_card_bill_fact(out)])
        return BankCollectResult(**out)

    # ---------- collect 輔助 ----------
    def _goto(self, page, route: str, wait: int = 6000):
        page.evaluate(f"location.hash='#{route}'")
        page.wait_for_timeout(wait)

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
