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

import re
import sys
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.base import BankCollectResult, BankCrawler, ResponseCollector
from backend.core.card_bills import (
    card_bill_date,
    card_bill_money,
    make_card_bill_fact,
    publish_card_bill_facts,
)
from backend.core.captcha import solve_captcha, wait_captcha_stable
from backend.core.creds import SinopacCreds
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointPhase,
    LoginCheckpointRule,
)

BASE = "https://mma.sinopac.com/MemberPortal/Member/MMALogin.aspx"
LOAN_DETAIL_URL = "https://mma.sinopac.com/mma/bank/easy_index_loan/mma_detail.aspx"
SEL_CAP_IMG = "#imgCode"


def _log(*a):
    print(*a, file=sys.stderr)


class SinopacLoginError(RuntimeError):
    """永豐登入失敗，附可供 retry/UI 判斷的 machine-readable code。"""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"[{code}] {message}")


def _sinopac_card_bill_fact(out: dict):
    kv = {}
    summary_rows = out.get("card_summary") or []
    if summary_rows and isinstance(summary_rows[0], dict):
        for group in summary_rows[0].get("SubInfo") or []:
            if isinstance(group, list):
                for row in group:
                    if isinstance(row, dict) and row.get("DataText"):
                        kv[row["DataText"]] = row.get("DataValue")
    statements = out.get("card_statements") or []
    latest = statements[0] if statements and isinstance(statements[0], dict) else {}
    latest_summary = latest.get("summary") if isinstance(latest, dict) else {}
    remaining = kv.get("本期應繳")
    if remaining is None and isinstance(latest_summary, dict):
        remaining = latest_summary.get("current_due")

    payment_amount = kv.get("最近繳款金額")
    payment_date = kv.get("最近繳款日期")
    if card_bill_money(payment_amount) is None or card_bill_date(payment_date) is None:
        payment_amount = None
        payment_date = None
    return make_card_bill_fact(
        remaining_due=remaining,
        statement_close_date=kv.get("結帳日") or latest.get("billing_cycle_date"),
        payment_due_date=kv.get("繳款截止日") or latest.get("payment_due_date"),
        last_payment_amount=payment_amount,
        last_payment_date=payment_date,
    )


class SinopacCrawler(BankCrawler):
    USES_SHARED_LOGIN_CHECKPOINTS: ClassVar[bool] = True
    CAPTCHA_INVALID = "captcha_invalid"
    CREDENTIALS_INVALID = "credentials_invalid"
    LOGIN_FAILED = "login_failed"

    def __init__(self):
        super().__init__(name="sinopac")
        self.creds = SinopacCreds.load()

    def _host_filter(self) -> str:
        return "sinopac.com"

    @staticmethod
    def _page_scopes(page):
        return [
            page,
            *(frame for frame in page.frames if frame is not page.main_frame),
        ]

    def _logged_in(self, page) -> bool:
        try:
            current = urlparse(page.url or "")
            path = (current.path or "").lower()
            if (
                (current.hostname or "").lower() != "mma.sinopac.com"
                or "mmalogin.aspx" in path
                or not path.startswith(("/mymma/", "/myasset/", "/mma_"))
            ):
                return False
            for scope in self._page_scopes(page):
                captcha_images = scope.locator(SEL_CAP_IMG)
                if any(
                    captcha_images.nth(index).is_visible()
                    for index in range(captcha_images.count())
                ):
                    return False
                inputs = scope.locator("input")
                for index in range(inputs.count()):
                    field = inputs.nth(index)
                    if (
                        field.is_visible()
                        and field.get_attribute("maxlength") in {"6", "11", "20"}
                    ):
                        return False
            body = page.locator("body").inner_text()
        except Exception:
            return False
        return (
            len(body) >= 500
            and "登出" in body
            and ("資產總覽" in body or "資產分析" in body or "我的帳戶" in body)
        )

    def login(self, page) -> bool:
        return self._shared_login(page)

    def prepare_login_page(self, page) -> None:
        try:
            page.wait_for_timeout(8000)
        except Exception:
            raise SinopacLoginError(
                self.LOGIN_FAILED,
                "永豐登入頁面無法安全準備；未送出登入",
            ) from None

    def is_authenticated(self, page) -> bool:
        return self._logged_in(page)

    def login_checkpoint_rules(self) -> tuple[LoginCheckpointRule, ...]:
        all_phases = tuple(CheckpointPhase)
        post_settle = (
            CheckpointPhase.POST_SUBMIT,
            CheckpointPhase.POST_SUBMIT_SETTLE,
        )
        otp = re.compile(
            r"^[\s\S]{0,300}(?:(?<![A-Za-z])OTP(?![A-Za-z])|一次性(?:密碼|驗證碼)|"
            r"簡訊驗證碼|動態驗證碼|裝置驗證|新裝置登入|信任此裝置)[\s\S]{0,300}$",
            re.IGNORECASE,
        )
        password = re.compile(
            r"^[\s\S]{0,200}(?:(?<!驗證)密碼\s*(?:已)?(?:到期|過期)|"
            r"(?:立即|必須|請先|需要|需)\s*(?:修改|變更|重設)\s*(?:您的?)?\s*密碼|"
            r"強制\s*(?:修改|變更|重設)\s*(?:您的?)?\s*密碼)[\s\S]{0,200}$"
        )
        credential_error = re.compile(
            r"^\s*(?:使用者代碼或網路密碼錯誤|帳號或密碼錯誤|密碼不正確|"
            r"密碼無效|身分證字號錯誤)\s*[。.!！?？]?\s*$"
        )
        captcha_error = re.compile(
            r"^\s*(?:(?:驗證碼失效|驗證碼錯誤|驗證碼輸入錯誤|"
            r"請重新輸入驗證碼)\s*[。.!！?？]?|"
            r"驗證碼失效或輸入錯誤，請重新輸入。)\s*$"
        )
        modal_scopes = (("modal", ".modal.show"), ("dialog", "[role='dialog']"))
        alert_scopes = (
            ("error", ".error"),
            ("alert", ".alert"),
            ("role-alert", "[role='alert']"),
        )
        return (
            *(
                LoginCheckpointRule(
                    name=f"sinopac-otp-required-{suffix}",
                    bank="sinopac",
                    phases=all_phases,
                    kind=CheckpointKind.OTP_REQUIRED,
                    container_selector=selector,
                    required_body_pattern=otp,
                )
                for suffix, selector in modal_scopes
            ),
            *(
                LoginCheckpointRule(
                    name=f"sinopac-password-change-required-{suffix}",
                    bank="sinopac",
                    phases=all_phases,
                    kind=CheckpointKind.PASSWORD_CHANGE_REQUIRED,
                    container_selector=selector,
                    required_body_pattern=password,
                )
                for suffix, selector in modal_scopes
            ),
            *(
                LoginCheckpointRule(
                    name=f"sinopac-explicit-login-error-{suffix}",
                    bank="sinopac",
                    phases=post_settle,
                    kind=CheckpointKind.EXPLICIT_LOGIN_ERROR,
                    container_selector=selector,
                    required_body_pattern=credential_error,
                )
                for suffix, selector in alert_scopes
            ),
            *(
                LoginCheckpointRule(
                    name=f"sinopac-captcha-retry-{suffix}",
                    bank="sinopac",
                    phases=(CheckpointPhase.POST_SUBMIT,),
                    kind=CheckpointKind.CAPTCHA_RETRY,
                    container_selector=selector,
                    required_body_pattern=captcha_error,
                )
                for suffix, selector in alert_scopes
            ),
            *(
                LoginCheckpointRule(
                    name=f"sinopac-unknown-{suffix}",
                    bank="sinopac",
                    phases=all_phases,
                    kind=CheckpointKind.UNKNOWN_BLOCKER,
                    container_selector=selector,
                )
                for suffix, selector in modal_scopes
            ),
            LoginCheckpointRule(
                name="sinopac-login-form-still-visible",
                bank="sinopac",
                phases=post_settle,
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector=SEL_CAP_IMG,
            ),
        )

    @staticmethod
    def _captcha_image(page, *, enabled: bool = False):
        images = page.locator(SEL_CAP_IMG)
        visible = [
            images.nth(index)
            for index in range(images.count())
            if images.nth(index).is_visible()
            and (not enabled or images.nth(index).is_enabled())
        ]
        return visible[0] if len(visible) == 1 else None

    @staticmethod
    def _keyboard_fill(page, field, value: str) -> None:
        field.click()
        field.click(click_count=3)
        page.keyboard.press("Backspace")
        page.keyboard.type(value, delay=80)
        if len(field.input_value()) != len(value):
            raise SinopacLoginError(
                SinopacCrawler.LOGIN_FAILED,
                "永豐登入欄位輸入長度不符；未送出登入",
            )

    def prepare_captcha_resubmit(self, page) -> None:
        try:
            image = self._captcha_image(page, enabled=True)
            if image is None:
                raise SinopacLoginError(
                    self.CAPTCHA_INVALID,
                    "無法安全更新永豐驗證碼；未送出登入",
                )
            image.click()
            page.wait_for_timeout(1500)
        except SinopacLoginError:
            raise
        except Exception:
            raise SinopacLoginError(
                self.CAPTCHA_INVALID,
                "無法安全更新永豐驗證碼；未送出登入",
            ) from None

    def _ocr_captcha(self, page, max_attempts=5):
        attempts = min(max(max_attempts, 1), 5)
        for attempt in range(attempts):
            try:
                if self._captcha_image(page) is None:
                    return None
                wait_captcha_stable(page, SEL_CAP_IMG, tmp_path=self.captcha_tmp)
                text = solve_captcha(
                    page,
                    SEL_CAP_IMG,
                    expected_len=6,
                    alnum_only=True,
                    digits_only=True,
                    min_confidence=0.98,
                    tmp_path=self.captcha_tmp,
                )
                if isinstance(text, str) and len(text) == 6 and text.isdigit():
                    return text
            except Exception:
                pass
            if attempt + 1 < attempts:
                try:
                    image = self._captcha_image(page, enabled=True)
                    if image is None:
                        return None
                    image.click()
                    page.wait_for_timeout(1500)
                except Exception:
                    return None
        return None

    @classmethod
    def _response_visible(cls, page) -> bool:
        for scope in cls._page_scopes(page):
            for selector in (
                ".modal.show",
                "[role='dialog']",
                ".error",
                ".alert",
                "[role='alert']",
            ):
                matches = scope.locator(selector)
                if any(
                    matches.nth(index).is_visible()
                    for index in range(matches.count())
                ):
                    return True
        return False

    def submit_credentials_once(self, page) -> None:
        try:
            page.wait_for_selector(SEL_CAP_IMG, state="visible", timeout=10000)
            inputs = page.locator("input")
            groups = {6: [], 11: [], 20: []}
            for index in range(inputs.count()):
                field = inputs.nth(index)
                if not field.is_visible():
                    continue
                maxlength = field.get_attribute("maxlength")
                if maxlength in {"6", "11", "20"}:
                    groups[int(maxlength)].append(field)
            if tuple(len(groups[length]) for length in (11, 20, 6)) != (1, 2, 1):
                raise SinopacLoginError(
                    self.LOGIN_FAILED,
                    "永豐登入欄位無法安全確認；未送出登入",
                )
            ordered_twenty = []
            for field in groups[20]:
                box = field.bounding_box()
                if box is None:
                    raise SinopacLoginError(
                        self.LOGIN_FAILED,
                        "永豐登入欄位無法安全確認；未送出登入",
                    )
                ordered_twenty.append((box["y"], field))
            ordered_twenty.sort(key=lambda item: item[0])
            if ordered_twenty[0][0] == ordered_twenty[1][0]:
                raise SinopacLoginError(
                    self.LOGIN_FAILED,
                    "永豐登入欄位無法安全確認；未送出登入",
                )
            fields = (
                groups[11][0],
                ordered_twenty[0][1],
                ordered_twenty[1][1],
                groups[6][0],
            )
            if any(not field.is_enabled() for field in fields):
                raise SinopacLoginError(
                    self.LOGIN_FAILED,
                    "永豐登入欄位無法安全確認；未送出登入",
                )
            for field, value in zip(
                fields[:3],
                (self.creds.national_id, self.creds.user_code, self.creds.password),
                strict=True,
            ):
                self._keyboard_fill(page, field, value)
            captcha = self._ocr_captcha(page, max_attempts=5)
            if captcha is None:
                raise SinopacLoginError(
                    self.CAPTCHA_INVALID,
                    "永豐驗證碼辨識失敗；未送出登入",
                )
            self._keyboard_fill(page, fields[3], captcha)

            candidates = page.locator("#MMA_Login")
            eligible = []
            for index in range(candidates.count()):
                candidate = candidates.nth(index)
                if not candidate.is_visible() or not candidate.is_enabled():
                    continue
                label = " ".join(
                    ((candidate.inner_text() or candidate.get_attribute("value") or "")).split()
                )
                if label == "登入":
                    eligible.append(candidate)
            if len(eligible) != 1:
                raise SinopacLoginError(
                    self.LOGIN_FAILED,
                    "找不到唯一且可操作的永豐登入按鈕；未送出登入",
                )
            button = eligible[0]
        except SinopacLoginError:
            raise
        except Exception:
            raise SinopacLoginError(
                self.LOGIN_FAILED,
                "永豐登入欄位無法安全填寫；未送出登入",
            ) from None

        try:
            button.click(timeout=8000)
        except Exception:
            raise SinopacLoginError(
                self.LOGIN_FAILED,
                "永豐登入送出狀態不明；禁止自動重試",
            ) from None

        try:
            for _ in range(8):
                page.wait_for_timeout(1000)
                if self._logged_in(page) or self._response_visible(page):
                    return
        except Exception:
            raise SinopacLoginError(
                self.LOGIN_FAILED,
                "永豐登入送出後狀態無法安全確認；禁止自動重試",
            ) from None

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

        publish_card_bill_facts(out, [_sinopac_card_bill_fact(out)])

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
