#!/usr/bin/env python3
"""CTBC (Chinatrust) personal e-banking crawler.

中國信託(CTBC)個人網銀抓取器。

CTBC = Akamai Bot Manager + Angular SPA + IBM MobileFirst 後端。
登入：formcontrolname custIxd/userIxd/pxd（無圖形驗證碼）→ 點登入。
  ⚠️ 若前次未正常登出，會跳「確認訊息」彈窗 → 必須點「確認登入」（否則卡登入頁）。
session 持久化（user_data_dir）→ 首次綁定裝置後免 OTP（實測首次就免）。
登入後 API：/IB/api/adapters/IB_Adapter/resource/{name}（參數加密，走 intercept 攔 JSON）。

設計規範：dump 真值不猜測 → collect 先 dump endpoint，摸清明細 API 再補 parse。
"""
from __future__ import annotations
import contextlib
import json
import os
import re
import sys
from calendar import monthrange
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.base import BankCollectResult, BankCrawler, ResponseCollector
from backend.core.card_bills import make_card_bill_fact, publish_card_bill_facts
from backend.core.creds import CtbcCreds
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointPhase,
    LoginCheckpointRule,
)


# W (2026-06-17): ctbc 也統一 raise CtbcLoginError, 跟其他 11 家一致.
# session 還在的 fallback (interstitial / _logged_in) 仍回 True;
# 只在 SEL_ID 找不到 + 不在內銀區 + 登入後仍未進內銀區時 raise.
class CtbcLoginError(RuntimeError):
    """CTBC 登入失敗（絕對失敗，重打會鎖卡）。"""

BASE = "https://www.ctbcbank.com/twrbc/twrbc-general/ot001/010"

SEL_ID = 'input[formcontrolname="custIxd"]'    # 身分證字號
SEL_USER = 'input[formcontrolname="userIxd"]'  # 使用者代號
SEL_PWD = 'input[formcontrolname="pxd"]'       # 網銀密碼
SEL_SUBMIT = "a.btn_submit"


def _submit_login_once(page) -> None:
    """Click the uniquely safe login action once, never retrying unknown dispatch."""
    try:
        candidates = page.locator(SEL_SUBMIT)
        eligible = []
        for index in range(candidates.count()):
            candidate = candidates.nth(index)
            classes = (candidate.get_attribute("class") or "").split()
            if (
                candidate.is_visible()
                and candidate.is_enabled()
                and "disabled" not in classes
                and " ".join(candidate.inner_text().split()) == "登入"
            ):
                eligible.append(candidate)
        if len(eligible) != 1:
            raise CtbcLoginError("找不到唯一且可操作的登入按鈕；未送出登入")
        button = eligible[0]
    except CtbcLoginError:
        raise
    except Exception:
        raise CtbcLoginError("無法安全確認登入按鈕；未送出登入") from None
    try:
        button.click(timeout=8000)
    except Exception:
        raise CtbcLoginError("登入送出狀態不明；禁止自動重試") from None

# W (2026-06-17): positive signal 4 條件 AND，對齊 SCSB 鐵律
# 1) urlOk: twrbc-home / qu000 (login-after path)
# 2) lenOk: innerText >= 500 (內銀區滿載)
# 3) kw >= 2 (內銀區關鍵字)
# 4) noLoginForm: custIxd / userIxd / pxd 都不可見
# 任一 fail → 視為未登入（即使有 logoutBtn 也信不過）。
JS_LOGGED_IN_POSITIVE = """
() => {
  const url = location.href.toLowerCase();
  const urlOk = /twrbc-home|qu000/.test(url);
  const visible = (e) => {
    if (!e) return false;
    const r = e.getBoundingClientRect();
    const cs = getComputedStyle(e);
    return !!(r.width || r.height || e.getClientRects().length)
      && cs.display !== 'none' && cs.visibility !== 'hidden';
  };
  const noLoginForm = !visible(document.querySelector('input[formcontrolname="custIxd"]'))
    && !visible(document.querySelector('input[formcontrolname="userIxd"]'))
    && !visible(document.querySelector('input[formcontrolname="pxd"]'));
  const body = document.body?.innerText || document.body?.textContent || '';
  const lenOk = body.length >= 500;
  const KW = ['帳戶總覽','我的總覽','資產總額','存款','轉帳','信用卡','登出',
              '台幣存款','外幣存款','基金','投資','貸款','繳費','個人設定','安全'];
  const hit = KW.filter(k => body.includes(k)).length;
  const kwOk = hit >= 2;
  return {
    ok: urlOk && lenOk && kwOk && noLoginForm,
    urlOk, lenOk, kwOk, noLoginForm,
    url: location.href, txt_len: body.length, hit
  };
}
"""


def _log(*a):
    print(*a, file=sys.stderr)


def _filter_valid_ctbc_details(
    detail_list_raw: list,
    *,
    start: date | None = None,
    end: date | None = None,
) -> tuple[list[dict], int]:
    """CTBC qu002/011 detailList raw → (filtered, skipped_count).

    Schema validate: 每筆合法 detail 必有 actDtTm (persist 算 txn_datetime 的唯一
    來源, txn_datetime 在 PG 端是 NOT NULL). 缺欄或非 dict → skip + count.

    抽成純函式方便 test (collector 主流程 mock SPA 太重, 此函式不依賴 page/SPA).
    2026-06-22 prod 06-22 04:00 job#152 NotNullViolation 修法.
    """
    out: list[dict] = []
    skipped = 0
    for d in detail_list_raw or []:
        if not isinstance(d, dict):
            skipped += 1
            continue
        if not (d.get("actDtTm") or "").strip():
            skipped += 1
            continue
        if start is not None and end is not None:
            try:
                d = _validated_ctbc_detail(d, start=start, end=end)
            except ValueError:
                skipped += 1
                continue
        out.append(d)
    return out, skipped


# 2026-06-22 (multi-account + m1~m5 拓展) CTBC ebmwResource POST helper
# SPA 用 Angular HttpClient 包裝, 純 fetch() 沒帶 interceptor token 拿到 HTML redirect
# (v5 註解實證). 但 ResponseCollector.auth_token 已攔到 SPA 第一次 auto-fire 的 Bearer
# → 直接帶 Bearer + 從 SPA 已 fire 的 qu002/011 hit 抄 URL/req body 結構, 改 rqData 重打.
_CTBC_MONTHS = ("m0", "m1", "m2", "m3", "m4", "m5")
_CTBC_EBMW_PATH = "/IB/api/adapters/IB_Adapter/resource/ebmwResource"
_CTBC_AMOUNT_RE = re.compile(r"^[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.0+)?$")
_PG_INTEGER_MAX = 2_147_483_647


def _valid_bearer(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"Bearer [^\s]+", value) is not None


def _ctbc_month_windows(as_of: date) -> list[tuple[str, date, date]]:
    """Return CTBC's six exposed calendar-month windows, oldest first."""
    windows = []
    for offset in range(5, -1, -1):
        absolute_month = as_of.year * 12 + as_of.month - 1 - offset
        year, zero_based_month = divmod(absolute_month, 12)
        month = zero_based_month + 1
        start = date(year, month, 1)
        end = min(as_of, date(year, month, monthrange(year, month)[1]))
        windows.append((f"m{offset}", start, end))
    return windows


def _ctbc_amount(value, *, non_negative: bool = False) -> int:
    if isinstance(value, bool):
        raise ValueError("invalid amount")
    raw = str(value).strip()
    if not _CTBC_AMOUNT_RE.fullmatch(raw):
        raise ValueError("invalid amount")
    try:
        amount = Decimal(raw.replace(",", ""))
    except (InvalidOperation, ValueError):
        raise ValueError("invalid amount") from None
    if (
        not amount.is_finite()
        or amount != amount.to_integral_value()
        or not -_PG_INTEGER_MAX <= amount <= _PG_INTEGER_MAX
        or non_negative and amount < 0
    ):
        raise ValueError("invalid amount")
    return int(amount)


def _validated_ctbc_detail(row: dict, *, start: date, end: date) -> dict:
    raw_datetime = row.get("actDtTm")
    if not isinstance(raw_datetime, str) or raw_datetime != raw_datetime.strip():
        raise ValueError("invalid row")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d{2}\.\d{2}\.\d{2}(?:\.\d+)?", raw_datetime):
        raise ValueError("invalid row")
    try:
        txn_date = datetime.strptime(raw_datetime[:19], "%Y-%m-%d-%H.%M.%S").date()
    except ValueError:
        raise ValueError("invalid row") from None
    if not start <= txn_date <= end:
        raise ValueError("invalid row")
    raw_account_date = row.get("trnDtRaw")
    if raw_account_date is not None:
        if not isinstance(raw_account_date, str) or not re.fullmatch(r"\d{8}", raw_account_date):
            raise ValueError("invalid row")
        try:
            account_date = datetime.strptime(raw_account_date, "%Y%m%d").date()
        except ValueError:
            raise ValueError("invalid row") from None
        if not start <= account_date <= end:
            raise ValueError("invalid row")
    if row.get("dbAmt") is None and row.get("crAmt") is None:
        raise ValueError("invalid row")
    normalized = dict(row)
    for field in ("dbAmt", "crAmt", "balanceAmt"):
        value = row.get(field)
        if value is not None:
            normalized[field] = _ctbc_amount(value, non_negative=field != "balanceAmt")
    if any(
        value is not None and not isinstance(value, str)
        for value in (
            row.get("memo1"), row.get("memo2"), row.get("bankId"),
            row.get("trfAcct"), row.get("memoCode"),
        )
    ):
        raise ValueError("invalid row")
    return normalized


def _build_qu002_011_post_body(account_id: str, month_type: str, template_body: dict) -> dict:
    """從 SPA 既有 qu002/011 req_body 當模板, 套新 (accountId, type) 算同層 POST body.

    Template body 範例 (collector 從 SPA 自動 fire 那次抄來):
      {"resource":"/twrbc-deposit/qu002/011", "rqData":{"accountId":"...", "type":"m0", ...}}

    回傳同 shape, rqData.accountId / type 改成 caller 指定. 其他欄位 (e.g. encrypt
    flags, locale) 原樣保留 — SPA 怎麼打我們就怎麼打.

    純函式方便 test. month_type 必須 m0~m5 之一.
    """
    if month_type not in _CTBC_MONTHS:
        raise ValueError(f"month_type 必須是 {_CTBC_MONTHS} 之一, got: {month_type!r}")
    if not isinstance(template_body, dict):
        raise ValueError(f"template_body 必須是 dict, got: {type(template_body).__name__}")
    new_body = {**template_body}
    rq = dict(template_body.get("rqData") or {})
    rq["accountId"] = account_id
    rq["type"] = month_type
    new_body["rqData"] = rq
    return new_body


class CtbcCrawler(BankCrawler):
    USES_SHARED_LOGIN_CHECKPOINTS: ClassVar[bool] = True
    HISTORY_COVERAGE_REQUIRED: ClassVar[bool] = True
    HISTORY_COVERAGE_DOMAINS: ClassVar[frozenset[str]] = frozenset({
        "twd_transactions",
    })
    CREDENTIAL_HOSTS = frozenset({"www.ctbcbank.com"})

    def __init__(self):
        super().__init__(name="ctbc")
        self.creds = CtbcCreds.load()

    def _host_filter(self) -> str:
        return "ctbcbank.com"

    def _logged_in(self, page) -> bool:
        try:
            current = urlparse(page.url or "")
            if (
                current.scheme.lower() != "https"
                or (current.hostname or "").lower() != "www.ctbcbank.com"
                or current.port not in (None, 443)
                or current.username is not None
                or current.password is not None
            ):
                return False
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
        page.wait_for_timeout(8000)

    def is_authenticated(self, page) -> bool:
        return self._logged_in(page)

    def login_checkpoint_rules(self) -> tuple[LoginCheckpointRule, ...]:
        post_phases = (
            CheckpointPhase.POST_SUBMIT,
            CheckpointPhase.POST_SUBMIT_SETTLE,
        )
        return (
            LoginCheckpointRule(
                name="ctbc-entry-announcement",
                bank="ctbc",
                phases=(CheckpointPhase.PRE_SUBMIT,),
                kind=CheckpointKind.DISMISSIBLE_NOTICE,
                container_selector=".modal.show",
                action_selector="a.btn_close",
                required_body_pattern=re.compile(r"^\s*重要公告(?:\s|$)"),
            ),
            LoginCheckpointRule(
                name="ctbc-otp-required",
                bank="ctbc",
                phases=post_phases,
                kind=CheckpointKind.OTP_REQUIRED,
                container_selector=".modal.show",
                required_body_pattern=re.compile(
                    r"^[\s\S]*(?:簡訊驗證|一次性密碼|動態密碼|OTP\s+驗證|認證碼)[\s\S]*$"
                ),
            ),
            LoginCheckpointRule(
                name="ctbc-duplicate-session",
                bank="ctbc",
                phases=post_phases,
                kind=CheckpointKind.DUPLICATE_SESSION,
                container_selector=".modal.show",
                action_texts=("確認登入",),
                required_body_pattern=re.compile(r"^\s*確認訊息[\s\S]*確認登入\s*$"),
            ),
            LoginCheckpointRule(
                name="ctbc-unknown-modal",
                bank="ctbc",
                phases=tuple(CheckpointPhase),
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector=".modal.show",
            ),
            LoginCheckpointRule(
                name="ctbc-unknown-dialog",
                bank="ctbc",
                phases=tuple(CheckpointPhase),
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector="[role='dialog']",
            ),
        )

    def submit_credentials_once(self, page) -> None:
        try:
            page.wait_for_selector(SEL_ID, state="visible", timeout=15000)
            for selector, value in (
                (SEL_ID, self.creds.national_id),
                (SEL_USER, self.creds.user_code),
                (SEL_PWD, self.creds.password),
            ):
                fields = page.locator(selector)
                if fields.count() != 1:
                    raise CtbcLoginError("登入欄位無法安全填寫；未送出登入")
                field = fields.nth(0)
                if not field.is_visible() or not field.is_enabled():
                    raise CtbcLoginError("登入欄位無法安全填寫；未送出登入")
                field.click()
                field.click(click_count=3)
                page.keyboard.press("Backspace")
                page.keyboard.type(value, delay=80)
                page.wait_for_timeout(300)
                if len(field.input_value()) != len(value):
                    raise CtbcLoginError("登入欄位輸入長度不符；未送出登入")
        except Exception:
            raise CtbcLoginError("登入欄位無法安全填寫；未送出登入") from None

        _submit_login_once(page)
        try:
            page.wait_for_timeout(5000)
            for _ in range(20):
                page.wait_for_timeout(1000)
                if self._logged_in(page):
                    return
                modals = page.locator(".modal.show")
                if any(modals.nth(index).is_visible() for index in range(modals.count())):
                    return
        except Exception:
            return

    # ---------- 抓取 ----------
    def logout(self, page) -> bool:
        """CTBC 登出是 HTML modal 兩段式：點「登出」後還要點「確認」。

        base.logout() 只會點 header 登出，CTBC 會停在「確認訊息 / 確定登出？」modal，
        若不再點確認，server-side session 沒真正結束 → 下次 sync 落入「已經登入了」
        interstitial / 登入表單不出現的 intermittent state。
        """
        import sys as _sys

        clicked = page.evaluate(
            """
            () => {
              const visible = (e) => {
                if (!e) return false;
                const r = e.getBoundingClientRect();
                const cs = getComputedStyle(e);
                return !!(r.width || r.height || e.getClientRects().length)
                  && cs.display !== 'none' && cs.visibility !== 'hidden';
              };
              const btn = document.querySelector('#btnHeaderLogout')
                || [...document.querySelectorAll('a,button,[role=button]')]
                    .find(x => visible(x) && (x.textContent || '').trim() === '登出');
              if (btn && visible(btn)) { btn.click(); return (btn.textContent || '登出').trim(); }
              return null;
            }
            """,
        )
        if not clicked:
            print("[ctbc][logout] ⚠️ 沒找到 header 登出按鈕", file=_sys.stderr)
            return False
        print(f"[ctbc][logout] 已點「{clicked}」，等待確認 modal", file=_sys.stderr)

        confirmed = False
        for attempt in range(8):
            page.wait_for_timeout(1000)
            confirmed_text = page.evaluate(
                """
                () => {
                  const body = document.body?.innerText || document.body?.textContent || '';
                  if (!/確定登出|確認訊息/.test(body)) return null;
                  const visible = (e) => {
                    if (!e) return false;
                    const r = e.getBoundingClientRect();
                    const cs = getComputedStyle(e);
                    return !!(r.width || r.height || e.getClientRects().length)
                      && cs.display !== 'none' && cs.visibility !== 'hidden';
                  };
                  const btn = [...document.querySelectorAll('button,a,[role=button],div,span')]
                    .find(x => visible(x) && (x.textContent || '').trim() === '確認');
                  if (btn) { btn.click(); return (btn.textContent || '').trim(); }
                  return null;
                }
                """,
            )
            if confirmed_text:
                print(f"[ctbc][logout] ✓ 已點登出確認「{confirmed_text}」(attempt={attempt+1})", file=_sys.stderr)
                confirmed = True
                break

        if not confirmed:
            print("[ctbc][logout] ⚠️ 找不到『確定登出？』確認按鈕", file=_sys.stderr)
            return False

        # 等 CTBC 前端把 header 切回「登入」或登入表單，代表 server-side logout 完成。
        for _ in range(12):
            page.wait_for_timeout(1000)
            logged_out = page.evaluate(
                """
                () => {
                  const visible = (e) => {
                    if (!e) return false;
                    const r = e.getBoundingClientRect();
                    const cs = getComputedStyle(e);
                    return !!(r.width || r.height || e.getClientRects().length)
                      && cs.display !== 'none' && cs.visibility !== 'hidden';
                  };
                  const loginBtn = document.querySelector('#btnHeaderLogin');
                  const logoutBtn = document.querySelector('#btnHeaderLogout');
                  const loginForm = document.querySelector('input[formcontrolname="custIxd"]');
                  return visible(loginBtn) || visible(loginForm) || !visible(logoutBtn);
                }
                """,
            )
            if logged_out:
                print(f"[ctbc][logout] ✅ 登出完成 -> {page.url}", file=_sys.stderr)
                return True

        print("[ctbc][logout] ⚠️ 已點確認但頁面仍像登入狀態（best-effort）", file=_sys.stderr)
        return True

    @classmethod
    def _validated_twd_inventory(
        cls, collector: ResponseCollector,
    ) -> tuple[dict, set[str]]:
        hit = next((
            item for item in reversed(collector.hits)
            if isinstance(item.req_body, dict)
            and item.req_body.get("resource") == "/twrbc-deposit/qu001/010"
        ), None)
        parsed = urlparse(hit.url) if hit else None
        response = hit.resp_json if hit else None
        if (
            hit is None
            or hit.method != "POST"
            or type(hit.status) is not int
            or not 200 <= hit.status < 300
            or parsed is None
            or parsed.scheme != "https"
            or parsed.hostname != "www.ctbcbank.com"
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != _CTBC_EBMW_PATH
            or not isinstance(response, dict)
            or response.get("code") != "0000"
            or not isinstance(response.get("rsData"), dict)
        ):
            raise RuntimeError("ctbc-twd-history-inventory")
        deposit = response["rsData"].get("twdAcctSummaryResponse")
        summary = deposit.get("demDepBalSummaryResponse") if isinstance(deposit, dict) else None
        rows = summary.get("infoList") if isinstance(summary, dict) else None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise RuntimeError("ctbc-twd-history-inventory")
        identities = []
        for row in rows:
            identity = row.get("accountId")
            if (
                not isinstance(identity, str)
                or not identity
                or identity != identity.strip()
            ):
                raise RuntimeError("ctbc-twd-history-inventory")
            identities.append(identity)
        if len(identities) != len(set(identities)):
            raise RuntimeError("ctbc-twd-history-inventory")
        return deposit, set(identities)

    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        """登入後抓帳戶彙總 + 台幣帳戶明細。

        ⚠️ CTBC API 機制（2026-06-10 實測摸清）：統一 POST 端點 `IB_Adapter/resource/ebmwResource`，
        body 帶 `resource` 欄位指定功能（像 RPC route），`rqData` 放查詢參數，回明文
        `{sys, code:"0000", rsData:{...}}`。故抓資料**不靠點擊導航**，直接複用前端的 token
        重打 ebmwResource 即可（同 HSBC 直打 API 思路）。

        已驗證 resource：
          /twrbc-home/qu000/010      首頁總覽（含 ebAcctSummaryInq 全帳戶彙總，登入自動載入）
          /twrbc-deposit/qu001/010   台幣存款（twdAcctSummaryResponse 帳號+餘額）
          /twrbc-deposit/qu002/010   台幣存款月份選擇器（dateRanges, 6 個月）
          /twrbc-deposit/qu002/011   台幣存款逐筆交易明細 (2026-06-20 補上)
                                     rqData={accountId, type:"m0"..."m5"}, m0=本月
        """
        out: dict = {}

        page.wait_for_timeout(5000)

        # 1) 首頁總覽彙總（登入後自動載入，攔即可）—— ebAcctSummaryInq 含台幣/信用卡/信貸彙總
        home = self._latest_rsdata(collector, "/twrbc-home/qu000/010")
        out["summary"] = (home or {}).get("ebAcctSummaryInq") if isinstance(home, dict) else None

        # 2) 台幣存款帳戶（點臺幣存款 link 觸發，或直接複用攔到的）
        self._goto_twd_deposit(page)
        page.wait_for_timeout(3000)
        out["twd_deposit"], twd_identities = self._validated_twd_inventory(collector)

        # 2.5) 台幣逐筆交易明細 (2026-06-20: known TODO 補上)
        # SPA route: /twrbc/twrbc-deposit/qu002/010 (date range picker) → fires qu002/011
        # qu002/011 rqData = {accountId, type:"m0"|"m1"|...} (m0=本月, m1=上月, ...)
        # 設計：先 goto qu002/010 載 dateRanges，再 _post_ebmw 各 type
        twd_history = self._collect_twd_deposit_history(
            page, collector, out["twd_deposit"], expected_identities=twd_identities,
        )
        out["twd_history"] = twd_history["accounts"]
        out["history_coverage"] = twd_history["coverage"]

        # 3) 信用卡明細 (2026-06-13 升級：分 pending + billed 兩段抓)
        # CTBC 信用卡 mega menu 子選單：
        #   - 「即時消費明細」 → /twrbc-card/qu041/010 = 9 筆即時未入帳 (pending)
        #   - 「帳單明細查詢」 → /twrbc-card/qu0??/010 = 已出帳逐筆 (billed)
        #   - 「未出帳單明細」 → /twrbc-card/qu0??/010 = 已授權未入帳 (unbilled)
        # 攻法：hover「信用卡/點數」→ 點目標子選單 → wait → 攔 ebmwResource API
        card_targets = ["即時消費明細", "帳單明細查詢", "未出帳單明細", "信用卡繳款記錄"]
        nav_logs: list[dict] = []
        for target_text in card_targets:
            nav_result = self._goto_credit_card(page, target_text=target_text)
            nav_logs.append({"target": target_text, **nav_result})
            _log(f"[collect][card-nav] {target_text} → clicked={bool(nav_result.get('clicked'))}")
            page.wait_for_timeout(6000)
        out["card_nav_probe"] = nav_logs  # 改成多家紀錄
        out["card_nav_probe_2"] = None     # 舊欄位保留向後相容

        # 2026-06-22 (使用者指示「一頁一頁看」, 之前 navProbe 只試 3 個 hard-code menu,
        # 沒探索其他子選單). 加 mega menu 全 dump probe — hover 信用卡/點數 後, 把
        # 出現的所有 sub-menu link text + href dump 出來. 後續用此資料找「繳款紀錄」
        # 類 menu, ship 0.3.32 hard-code 新 target.
        try:
            card_link = page.locator("a:has-text('信用卡/點數')").first
            if card_link.count() > 0:
                card_link.hover()
                page.wait_for_timeout(1500)
                menu_items = page.evaluate("""() => {
                    const items = [];
                    for (const el of document.querySelectorAll('a, button, [role="menuitem"]')) {
                        const t = (el.textContent || '').trim();
                        if (!t || t.length > 50) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width < 5 || r.height < 5) continue;
                        const cs = window.getComputedStyle(el);
                        if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                        // 只收 mega menu 區內 item (y 在 100-400 之間, 經驗值)
                        if (r.y < 100 || r.y > 500) continue;
                        items.push({
                            tag: el.tagName,
                            text: t,
                            href: el.getAttribute('href') || '',
                            x: Math.round(r.x),
                            y: Math.round(r.y),
                        });
                    }
                    return items;
                }""")
                out["card_mega_menu_dump"] = menu_items
                _log(f"[collect][card-mega-menu-dump] {len(menu_items)} items")
        except Exception as e:
            out["card_mega_menu_dump"] = {"error": str(e)}
            _log(f"[collect][card-mega-menu-dump] ERROR: {type(e).__name__}")

        # 抽所有 creditcard / creditCard / card 相關的 resource
        card_resources = sorted({
            (h.req_body or {}).get("resource", "")
            for h in collector.hits
            if "ebmwResource" in h.url and isinstance(h.req_body, dict)
            and any(k in (h.req_body.get("resource") or "").lower() for k in ("credit", "card", "ccrd", "stmt", "bill"))
        } - {""})
        out["card_resources"] = card_resources
        _log(f"[collect][card-resources] count={len(card_resources)}")

        # 把所有 card 相關 resource 的最新 rsData 撈下來
        card_api_dump = {}
        for r in card_resources:
            data = self._latest_rsdata(collector, r)
            if data is not None:
                card_api_dump[r] = data
        out["card_api_dump"] = card_api_dump

        # 最終 url + 截圖（信用卡頁）
        out["card_final_url"] = page.url

        out["_final_url"] = page.url
        out["_all_resources"] = sorted({
            (h.req_body or {}).get("resource", "")
            for h in collector.hits
            if "ebmwResource" in h.url and isinstance(h.req_body, dict)
        } - {""})

        cc = (out.get("summary") or {}).get("creditCardSummary") or {}
        payment_rows = []
        for payload in (out.get("card_api_dump") or {}).values():
            if not isinstance(payload, dict):
                continue
            for key, rows in payload.items():
                if not str(key).startswith("billData") or not isinstance(rows, list):
                    continue
                for row in rows:
                    if isinstance(row, dict) and row.get("payDt") and row.get("amt") is not None:
                        payment_rows.append((str(row["payDt"]), row["amt"]))
        last_date, last_amount = max(payment_rows, default=(None, None), key=lambda item: item[0])
        publish_card_bill_facts(out, [make_card_bill_fact(
            remaining_due=cc.get("unpaidStmt"),
            payment_due_date=cc.get("pmtExpDt"),
            last_payment_amount=last_amount,
            last_payment_date=last_date,
        )])

        return BankCollectResult(**out)

    @staticmethod
    def _latest_rsdata(collector: ResponseCollector, resource: str):
        """取某 resource 最新成功回應的 rsData（CTBC 格式 {sys, code, rsData}）。"""
        hits = [h for h in collector.hits
                if "ebmwResource" in h.url and isinstance(h.req_body, dict)
                and h.req_body.get("resource") == resource
                and isinstance(h.resp_json, dict) and h.resp_json.get("code") == "0000"]
        if hits:
            return hits[-1].resp_json.get("rsData")
        return None

    def _collect_twd_deposit_history(
        self,
        page,
        collector: ResponseCollector,
        twd_deposit,
        *,
        as_of: date | None = None,
        expected_identities: set[str],
    ) -> dict:
        """Collect every bank-exposed month for every authoritative TWD account."""
        end = as_of or datetime.now(ZoneInfo("Asia/Taipei")).date()
        capability = _ctbc_month_windows(end)
        floor = capability[0][1]
        mode = os.environ.get("BANK_CRAWLER_HISTORY_MODE", "full")
        if mode not in {"full", "incremental"}:
            raise RuntimeError("ctbc-twd-history-mode")
        info_list = (twd_deposit or {}).get("demDepBalSummaryResponse", {}).get("infoList")
        if not isinstance(info_list, list):
            raise RuntimeError("ctbc-twd-history-inventory")
        identities: list[str] = []
        for row in info_list:
            identity = row.get("accountId") if isinstance(row, dict) else None
            if (
                not isinstance(identity, str)
                or not identity
                or identity != identity.strip()
            ):
                raise RuntimeError("ctbc-twd-history-inventory")
            identities.append(identity)
        if len(identities) != len(set(identities)) or set(identities) != expected_identities:
            raise RuntimeError("ctbc-twd-history-inventory")
        if not identities:
            return {
                "accounts": [],
                "coverage": {
                    "mode": mode,
                    "domains": [{
                        "domain": "twd_transactions",
                        "expected": [],
                        "windows": [],
                        "empty_window": {
                            "start": floor.isoformat(), "end": end.isoformat(),
                            "status": "explicit_empty", "pages": 1,
                        },
                    }],
                },
            }

        try:
            page.goto(
                "https://www.ctbcbank.com/twrbc/twrbc-deposit/qu002/010",
                wait_until="domcontentloaded", timeout=15000,
            )
        except Exception:
            raise RuntimeError("ctbc-twd-history-template") from None
        try:
            current = urlparse(page.url)
        except Exception:
            raise RuntimeError("ctbc-twd-history-template") from None
        if (
            current.scheme != "https"
            or current.hostname != "www.ctbcbank.com"
            or current.port not in (None, 443)
            or current.username is not None
            or current.password is not None
        ):
            raise RuntimeError("ctbc-twd-history-template")
        page.wait_for_timeout(5000)
        template_hit = self._latest_qu002_011_hit(collector)
        if template_hit is None:
            raise RuntimeError("ctbc-twd-history-template")
        template_body = template_hit.req_body
        template_url = template_hit.url
        bearer = collector.auth_token
        if not _valid_bearer(bearer):
            raise RuntimeError("ctbc-twd-history-template")

        accounts = []
        expected = []
        receipts = []
        for account_index, account_id in enumerate(identities, start=1):
            desired_start = self.transaction_window_start(account_id, floor=floor)
            selected = [window for window in capability if window[2] >= desired_start]
            if not selected:
                raise RuntimeError("ctbc-twd-history-range")
            months_data: dict[str, list[dict]] = {}
            for month, window_start, window_end in selected:
                try:
                    raw_rows = self._fetch_qu002_011(
                        page, template_url, template_body, account_id, month, bearer,
                    )
                except Exception:
                    raise RuntimeError("ctbc-twd-history-fetch") from None
                detail_list, skipped = _filter_valid_ctbc_details(
                    raw_rows, start=window_start, end=window_end,
                )
                if skipped:
                    raise RuntimeError("ctbc-twd-history-row")
                months_data[month] = detail_list
                receipts.append({
                    "identity": account_id,
                    "start": window_start.isoformat(),
                    "end": window_end.isoformat(),
                    "status": "complete" if detail_list else "explicit_empty",
                    "pages": 1,
                })
            actual_start = selected[0][1]
            accounts.append({
                "account_no": account_id,
                "months": months_data,
                "errors": {},
            })
            expected.append({
                "identity": account_id,
                "start": actual_start.isoformat(),
                "end": end.isoformat(),
            })
            _log(
                f"[twd-history] account_index={account_index} "
                f"total={sum(len(rows) for rows in months_data.values())} "
                f"months={list(months_data)}",
            )
        return {
            "accounts": accounts,
            "coverage": {
                "mode": mode,
                "domains": [{
                    "domain": "twd_transactions",
                    "expected": expected,
                    "windows": receipts,
                }],
            },
        }

    @staticmethod
    def _latest_qu002_011_hit(collector: ResponseCollector):
        """Return the newest exact, successful owned history seed request."""
        for hit in reversed(collector.hits):
            parsed = urlparse(hit.url)
            body = hit.req_body
            response = hit.resp_json
            request_data = body.get("rqData") if isinstance(body, dict) else None
            response_data = response.get("rsData") if isinstance(response, dict) else None
            if (
                hit.method == "POST"
                and type(hit.status) is int
                and 200 <= hit.status < 300
                and parsed.scheme == "https"
                and parsed.hostname == "www.ctbcbank.com"
                and parsed.port in (None, 443)
                and parsed.username is None
                and parsed.password is None
                and parsed.path == _CTBC_EBMW_PATH
                and isinstance(body, dict)
                and body.get("resource") == "/twrbc-deposit/qu002/011"
                and isinstance(request_data, dict)
                and isinstance(request_data.get("accountId"), str)
                and bool(request_data["accountId"])
                and request_data["accountId"] == request_data["accountId"].strip()
                and request_data.get("type") in _CTBC_MONTHS
                and isinstance(response, dict)
                and response.get("code") == "0000"
                and isinstance(response_data, dict)
                and isinstance(response_data.get("detailList"), list)
            ):
                return hit
        return None

    @staticmethod
    def _fetch_qu002_011(
        page,
        template_url: str,
        template_body: dict,
        account_id: str,
        month_type: str,
        bearer: str,
    ) -> list:
        """Replay one owned CTBC month request with a bounded same-origin fetch."""
        parsed = urlparse(template_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "www.ctbcbank.com"
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != _CTBC_EBMW_PATH
            or not isinstance(template_body, dict)
            or template_body.get("resource") != "/twrbc-deposit/qu002/011"
            or not isinstance(template_body.get("rqData"), dict)
            or not isinstance(account_id, str)
            or not account_id
            or account_id != account_id.strip()
            or not _valid_bearer(bearer)
        ):
            raise RuntimeError("invalid-request")
        post_body = _build_qu002_011_post_body(account_id, month_type, template_body)
        script = """async (payload) => {
          if (location.protocol !== 'https:' || location.hostname !== 'www.ctbcbank.com'
              || !['', '443'].includes(location.port)) {
            throw new Error('ctbc-origin-mismatch');
          }
          const controller = new AbortController();
          const timeoutId = setTimeout(() => controller.abort(), 30000);
          try {
            const response = await fetch(payload.url, {
              method: 'POST', credentials: 'include', redirect: 'error',
              signal: controller.signal,
              headers: {
                'content-type': 'application/json',
                'authorization': payload.bearer,
              },
              body: JSON.stringify(payload.body),
            });
            let json = null;
            try { json = await response.json(); } catch (_) {}
            return {
              status: response.status,
              url: response.url,
              redirected: response.redirected,
              contentType: response.headers.get('content-type') || '',
              json,
            };
          } finally {
            clearTimeout(timeoutId);
          }
        }"""
        result = page.evaluate(script, {
            "url": template_url,
            "body": post_body,
            "bearer": bearer,
        })
        response = result.get("json") if isinstance(result, dict) else None
        raw_content_type = result.get("contentType") if isinstance(result, dict) else None
        media_type = (
            raw_content_type.split(";", 1)[0].strip().lower()
            if isinstance(raw_content_type, str)
            else ""
        )
        if (
            not isinstance(result, dict)
            or type(result.get("status")) is not int
            or not 200 <= result["status"] < 300
            or result.get("url") != template_url
            or result.get("redirected") is not False
            or not (
                media_type == "application/json"
                or re.fullmatch(
                    r"application/[!#$%&'*+.^_`|~0-9A-Za-z-]+\+json",
                    media_type,
                ) is not None
            )
            or not isinstance(response, dict)
            or response.get("code") != "0000"
            or not isinstance(response.get("rsData"), dict)
            or not isinstance(response["rsData"].get("detailList"), list)
        ):
            raise RuntimeError("invalid-response")
        response_data = response["rsData"]
        detail_list = response_data["detailList"]
        if (
            response_data.get("accountId") != account_id
            or response_data.get("type") != month_type
        ):
            raise RuntimeError("ownership-mismatch")
        for key in ("hasNext", "hasMore", "moreData", "nextPage", "nextToken"):
            if response_data.get(key) not in (None, False, "", 0, "0"):
                raise RuntimeError("pagination-detected")
        page_values = [
            response_data[key] for key in ("totalPages", "pageCount")
            if key in response_data
        ]
        no_next_values = [
            response_data[key] for key in ("hasNext", "hasMore", "moreData")
            if key in response_data
        ]
        if not page_values and not no_next_values:
            raise RuntimeError("missing-pagination-proof")
        if any(
            type(value) is not int or value != 1
            for value in page_values
        ) or any(value is not False for value in no_next_values):
            raise RuntimeError("pagination-detected")
        count_values = [
            response_data[key] for key in ("count", "totalCount")
            if key in response_data
        ]
        if not count_values or any(
            type(value) is not int or value != len(detail_list)
            for value in count_values
        ):
            raise RuntimeError("count-mismatch")
        return detail_list

    def _goto_twd_deposit(self, page):
        """點首頁「臺幣存款」A.link 進存款頁（觸發 /twrbc-deposit/qu001/010）。"""
        with contextlib.suppress(Exception):
            page.evaluate(
                "(() => { const a=[...document.querySelectorAll('a.link')]"
                ".find(x=>x.offsetParent!==null && (x.textContent||'').trim()==='臺幣存款');"
                " if(a){ a.click(); return true;}"
                " const a2=[...document.querySelectorAll('a')].find(x=>x.offsetParent!==null"
                "  && (x.textContent||'').trim()==='臺幣存款' && !/轉帳/.test(x.textContent||'')); if(a2) a2.click(); })()",
            )

    def _goto_credit_card(self, page, target_text: str = "消費明細") -> dict:
        """設計規範：每家都要抓信用卡明細。

        CTBC top nav「信用卡/點數」是 hover trigger（mega menu pattern），
        click 不 navigate 只展開子選單。策略：
          1) 用 Playwright hover() 觸發 mega menu
          2) 等 dropdown 出現（前後 HTML 長度比較 / 等 200ms～1s）
          3) 找展開後新增的子選單 link（target_text exact match）
          4) click 子選單真實 link → 觸發 SPA route 切換

        2026-06-13 升級：target_text 參數化，支援多家子選單迭代抓
        （pending=即時消費明細 / billed=帳單明細查詢 / unbilled=未出帳單明細）
        """
        log: list[str] = []
        try:
            # Step 1: 找「信用卡/點數」<a>
            card_link = page.locator("a:has-text('信用卡/點數')").first
            if card_link.count() == 0:
                log.append("no top-nav 信用卡/點數")
                return {"clicked": None, "log": log}

            # Step 2: hover 觸發 mega menu
            card_link.hover()
            page.wait_for_timeout(1200)
            log.append("hovered top-nav, mega menu should be visible")

            # Step 3: 找 target_text 子選單（exact match，避免「帳單明細查詢」誤撞「未出帳單明細」）
            target = page.evaluate("""(wanted) => {
                for (const el of document.querySelectorAll('a, button, [role="menuitem"]')) {
                    const t = (el.textContent || '').trim();
                    if (t !== wanted) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 5 || r.height < 5) continue;
                    const cs = window.getComputedStyle(el);
                    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                    return {tag: el.tagName, text: t,
                            x: Math.round(r.x), y: Math.round(r.y),
                            w: Math.round(r.width), h: Math.round(r.height)};
                }
                return null;
            }""", target_text)

            if not target:
                log.append(f"no sub-menu match {target_text!r}")
                return {"clicked": None, "log": log}

            log.append(f"will click: {target!r}")
            tlocator = page.locator(f"a:has-text('{target_text}')").first
            if tlocator.count() == 0:
                page.mouse.click(target["x"] + target["w"] // 2, target["y"] + target["h"] // 2)
                log.append("clicked by coords")
            else:
                tlocator.click()
                log.append("clicked by locator")

            page.wait_for_timeout(2500)
            log.append(f"after_click_url={page.url}")
            return {"clicked": target["text"], "after_click_url": page.url, "log": log}
        except Exception as e:
            log.append(f"ERROR: {e}")
            return {"error": str(e), "log": log}


if __name__ == "__main__":
    import json
    crawler = CtbcCrawler()
    result = crawler.run(login_url=BASE, headless=False)
    out_file = Path(__file__).resolve().parents[1] / "data" / "ctbc_collected.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"\n[done] 已存: {out_file}")
    if result.get("error"):
        _log(f"  error: {result['error']}")
    data = result.get("data", {})
    dep = data.get("twd_deposit") or {}
    demdep = (dep.get("demDepBalSummaryResponse") or {}).get("infoList") if isinstance(dep, dict) else None
    _log(f"  台幣帳戶: {len(demdep or [])}")
    _log(f"  攔到的 resource: {len(data.get('_all_resources', []))}")
