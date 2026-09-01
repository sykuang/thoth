#!/usr/bin/env python3
"""Union Bank of Taiwan (UBOT) personal e-banking crawler.

聯邦銀行(UBOT)個人網銀抓取器。

登入入口：官網 https://www.ubot.com.tw/home 內嵌登入 modal（舊 mybank 已搬家）。
流程：點右上「網銀登入」開 modal → 確認「個人用戶登入」分頁
      → 真鍵盤輸入 #sid/#nickname/#password → OCR 圖形驗證碼(#CAPTCHA, 6碼) → 點「登入」。
驗證碼：img[alt='CAPTCHA'] base64 jpeg 170x50，ddddocr 直接 OCR，錯則點「重新產生」換圖。
密碼欄旁虛擬鍵盤為選用；自動化仍使用真鍵盤輸入並驗證長度。

設計規範：dump 真值不猜測 → 登入後 collect 先 dump endpoint，摸清明細 API 再補 parse。
預設行為：headless browser → 預設 headless=True。
"""
from __future__ import annotations

import re
import os
import sys
from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qsl, urlparse
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.base import (
    BankCollectResult,
    BankCrawler,
    ResponseCollector,
    validate_history_coverage,
)
from backend.core.card_bills import (
    card_bill_date,
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

    def __init__(self, message: str, *, safe_code: str | None = None) -> None:
        self.safe_code = safe_code
        super().__init__(message)


_MAX_BILL_AMOUNT = Decimal("100000000")


def _ubot_bill_amount(value) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    if not amount.is_finite() or amount < 0 or amount > _MAX_BILL_AMOUNT:
        return None
    return amount


def _ubot_card_bill_fact(out: dict):
    limit_payload = out.get("card_limit")
    summary_payload = out.get("card_summary")
    limit_payload = {} if limit_payload is None else limit_payload
    summary_payload = {} if summary_payload is None else summary_payload
    if not isinstance(limit_payload, dict) or not isinstance(summary_payload, dict):
        return None
    limits = limit_payload.get("CardList", [])
    summaries = summary_payload.get("CardList", [])
    if (
        not isinstance(limits, list)
        or not isinstance(summaries, list)
        or any(not isinstance(row, dict) for row in (*limits, *summaries))
    ):
        return None
    limit_summary = limits[0] if limits else {}
    card_summary = summaries[0] if summaries else {}
    summary = {**card_summary, **limit_summary}

    history = out.get("card_pay_history")
    if history is not None and not isinstance(history, dict):
        return None
    pay_records = None
    if isinstance(history, dict):
        for key in ("DateList", "PayList", "payList", "records"):
            if key in history:
                if not isinstance(history[key], list):
                    return None
                pay_records = history[key]
                break
    normalized_payments: list[tuple[str, Decimal]] = []
    if pay_records:
        for row in pay_records:
            if not isinstance(row, dict):
                return None
            payment_date = card_bill_date(
                row.get("postDate") or row.get("effectDate")
                or row.get("payDate") or row.get("PayDate")
            )
            raw_amount = row.get("payAmt")
            if raw_amount is None:
                raw_amount = row.get("amount")
            payment_amount = _ubot_bill_amount(raw_amount)
            if payment_date is None or payment_amount is None:
                return None
            normalized_payments.append((payment_date, payment_amount))
    else:
        raw_payment_date = summary.get("lastPayDate")
        raw_payment_amount = summary.get("lastPayAmt")
        pair_supplied = raw_payment_date is not None or raw_payment_amount is not None
        payment_date = card_bill_date(raw_payment_date)
        payment_amount = _ubot_bill_amount(raw_payment_amount)
        sentinel = payment_amount == 0 and str(raw_payment_date).strip() == "00000000"
        if pair_supplied and not sentinel:
            if payment_date is None or payment_amount is None:
                return None
            normalized_payments.append((payment_date, payment_amount))

    latest_payment = max(normalized_payments, default=None, key=lambda item: item[0])
    payment_date = latest_payment[0] if latest_payment else None
    payment_amount = latest_payment[1] if latest_payment else None

    billed = out.get("card_billed")
    if billed is not None and not isinstance(billed, list):
        return None
    statements: list[tuple[str, str, Decimal, Decimal]] = []
    for body in billed or []:
        if not isinstance(body, dict) or not isinstance(body.get("CardHeader"), dict):
            return None
        header = body["CardHeader"]
        statement_date = card_bill_date(header.get("stmtDate"))
        statement_due_date = card_bill_date(header.get("dueDate"))
        current_balance = _ubot_bill_amount(header.get("currBal"))
        original_due = _ubot_bill_amount(header.get("dueAmt"))
        if (
            statement_date is None
            or statement_due_date is None
            or current_balance is None
            or original_due is None
        ):
            return None
        statements.append((statement_date, statement_due_date, current_balance, original_due))

    if statements:
        latest_statement_date = max(item[0] for item in statements)
        latest_statements = {
            item for item in statements if item[0] == latest_statement_date
        }
        if len(latest_statements) != 1:
            return None
        latest_statement = latest_statements.pop()
    else:
        latest_statement = None
    statement_date = latest_statement[0] if latest_statement else None
    summary_due_date = card_bill_date(summary.get("dueDate"))
    if latest_statement and latest_statement[1] != summary_due_date:
        return None

    remaining = _ubot_bill_amount(summary.get("payAmt"))
    statement_amount = None
    if latest_statement and latest_statement[2] == latest_statement[3]:
        statement_amount = latest_statement[2]
    paid_this_cycle = sum(
        (amount for paid_on, amount in normalized_payments
         if statement_date is not None and paid_on >= statement_date),
        Decimal(0),
    )
    if (
        remaining is not None
        and statement_amount is not None
        and statement_amount > 0
        and paid_this_cycle >= statement_amount
    ):
        remaining = Decimal(0)

    return make_card_bill_fact(
        remaining_due=remaining,
        statement_close_date=statement_date,
        payment_due_date=summary.get("dueDate"),
        last_payment_amount=payment_amount,
        last_payment_date=payment_date,
    )


class UbotCrawler(BankCrawler):
    USES_SHARED_LOGIN_CHECKPOINTS: ClassVar[bool] = True
    HISTORY_COVERAGE_REQUIRED: ClassVar[bool] = True
    HISTORY_COVERAGE_DOMAINS: ClassVar[frozenset[str]] = frozenset({"twd_transactions"})
    CREDENTIAL_HOSTS = frozenset({"www.ubot.com.tw"})

    def __init__(self):
        super().__init__(name="ubot")
        self.creds = UbotCreds.load()

    def _host_filter(self) -> str:
        return "ubot.com.tw"

    @staticmethod
    def _history_floor(end: date) -> date:
        month = end.month - 2
        year = end.year
        if month <= 0:
            month += 12
            year -= 1
        return date(year, month, 1)

    @staticmethod
    def _history_windows(start: date, end: date) -> list[tuple[date, date]]:
        if start > end:
            raise RuntimeError("ubot-twd-history-range")
        windows = []
        cursor = start
        while cursor <= end:
            window_end = min(end, date(
                cursor.year, cursor.month, monthrange(cursor.year, cursor.month)[1],
            ))
            windows.append((cursor, window_end))
            cursor = window_end + timedelta(days=1)
        return windows

    def _history_range(
        self, identity: str, *, end: date, mode: str,
    ) -> tuple[date, date]:
        floor = self._history_floor(end)
        cursor = self.transaction_start_for(identity)
        if isinstance(cursor, date) and cursor > end:
            raise RuntimeError("ubot-twd-history-cursor")
        if mode == "full" or (mode == "incremental" and cursor is None):
            return floor, end
        if mode != "incremental" or not isinstance(cursor, date):
            raise RuntimeError("ubot-twd-history-mode")
        overlap = cursor - timedelta(days=7)
        return max(floor, overlap.replace(day=1)), end

    @classmethod
    def _validate_twd_form(cls, snapshot, *, as_of: date) -> list[dict]:
        error = "ubot-twd-history-form"
        if not isinstance(snapshot, dict) or set(snapshot) != {"selects", "search_buttons"}:
            raise RuntimeError(error)
        selects = snapshot["selects"]
        if (
            not isinstance(selects, list) or len(selects) != 2
            or type(snapshot["search_buttons"]) is not int
            or snapshot["search_buttons"] != 1
        ):
            raise RuntimeError(error)
        for select in selects:
            if (
                not isinstance(select, dict) or set(select) != {"enabled", "options"}
                or select["enabled"] is not True
            ):
                raise RuntimeError(error)
            if not isinstance(select["options"], list) or any(
                not isinstance(option, dict) or set(option) != {"text", "value"}
                or not isinstance(option["text"], str)
                or not isinstance(option["value"], str)
                for option in select["options"]
            ):
                raise RuntimeError(error)

        period_labels = [option["text"] for option in selects[1]["options"]]
        months = []
        cursor = as_of.replace(day=1)
        for _ in range(3):
            months.append(f"{cursor.month}月份")
            cursor = (cursor - timedelta(days=1)).replace(day=1)
        if period_labels != ["當日", "最近一週", "最近一月", *months, "自選日期"]:
            raise RuntimeError(error)

        account_options = selects[0]["options"]
        if len(account_options) < 2:
            raise RuntimeError(error)
        placeholder = account_options[0]
        pattern = re.compile(r"(?<!\d)(\d{3})-(\d{2})-(\d{7})(?!\d)")
        if placeholder != {"text": "請選擇帳號", "value": ""}:
            raise RuntimeError(error)
        inventory = []
        identities: set[str] = set()
        labels: set[str] = set()
        for index, option in enumerate(account_options[1:], 1):
            label = option["text"]
            matches = pattern.findall(label)
            identity = "".join(matches[0]) if len(matches) == 1 else ""
            if (
                not label or label != label.strip() or not option["value"]
                or len(matches) != 1 or identity in identities or label in labels
            ):
                raise RuntimeError(error)
            identities.add(identity)
            labels.add(label)
            inventory.append({
                "label": label, "identity": identity, "currency": "TWD", "index": index,
            })
        return inventory

    @staticmethod
    def _strict_slash_date(value, error: str) -> date:
        if not isinstance(value, str) or re.fullmatch(r"\d{4}/\d{2}/\d{2}", value) is None:
            raise RuntimeError(error)
        try:
            return datetime.strptime(value, "%Y/%m/%d").date()
        except ValueError:
            raise RuntimeError(error) from None

    @staticmethod
    def _strict_twd_amount(value, error: str) -> None:
        if not isinstance(value, str):
            raise RuntimeError(error)
        if value in {"", "-"}:
            return
        if re.fullmatch(r"[+-]?(?:0|[1-9]\d*|[1-9]\d{0,2}(?:,\d{3})+)", value) is None:
            raise RuntimeError(error)
        try:
            amount = Decimal(value.replace(",", ""))
        except InvalidOperation:
            raise RuntimeError(error) from None
        if not amount.is_finite() or abs(amount) > Decimal("2147483647"):
            raise RuntimeError(error)

    @staticmethod
    def _nttotal_claims_more_pages(total) -> bool:
        stack = [total]
        seen = 0
        while stack:
            value = stack.pop()
            seen += 1
            if seen > 1_000:
                return True
            if isinstance(value, dict):
                for key, item in value.items():
                    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
                    false_marker = (
                        item is None or item is False or item == 0
                        or (
                            isinstance(item, str)
                            and item.strip().casefold() in {"", "0", "false", "no", "n", "none", "null"}
                        )
                    )
                    if re.fullmatch(r"has(?:next|more)(?:page|pages)?", normalized):
                        if not false_marker:
                            return True
                    elif re.fullmatch(
                        r"(?:(?:total|last|max)(?:page|pages)|page(?:count|total))", normalized,
                    ):
                        try:
                            if Decimal(str(item)) != 1:
                                return True
                        except InvalidOperation:
                            return True
                    elif normalized == "pageindex":
                        try:
                            if Decimal(str(item)) != 0:
                                return True
                        except InvalidOperation:
                            return True
                    elif normalized in {"page", "currentpage", "pageno", "pagenumber"}:
                        try:
                            if Decimal(str(item)) != 1:
                                return True
                        except InvalidOperation:
                            return True
                    elif "page" in normalized and normalized not in {
                        "pagesize", "perpage", "rowsperpage", "recordsperpage",
                    } and not false_marker:
                        return True
                    stack.append(item)
            elif isinstance(value, list):
                stack.extend(value)
        return False

    @classmethod
    def _validate_history_hit(
        cls, hit, *, identity: str, start: date, end: date, after_sequence: int,
    ) -> dict:
        error = "ubot-twd-history-response"
        parsed = urlparse(hit.url)
        raw = urlparse(hit.raw_url or hit.url)
        expected_path = "/MyBank/IBKB010102"
        if (
            parsed.scheme != "https" or raw.scheme != "https"
            or parsed.hostname != "www.ubot.com.tw" or raw.hostname != "www.ubot.com.tw"
            or parsed.port not in (None, 443) or raw.port not in (None, 443)
            or any(value is not None for value in (
                parsed.username, parsed.password, raw.username, raw.password,
            ))
            or parsed.path != expected_path or raw.path != expected_path
            or parsed.params or raw.params or parsed.query or raw.query
            or parsed.fragment or raw.fragment
            or hit.method != "POST" or hit.status != 200 or hit.redirected
            or hit.main_frame_request is not True
            or type(hit.request_sequence) is not int or hit.request_sequence <= after_sequence
            or type(hit.body_size) is not int or not 0 < hit.body_size <= 5_000_000
            or hit.content_type.split(";", 1)[0].strip().lower() != "application/json"
            or not isinstance(hit.req_body, str)
        ):
            raise RuntimeError(error)
        try:
            pairs = parse_qsl(hit.req_body, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            raise RuntimeError(error) from None
        form: dict[str, list[str]] = {}
        for key, value in pairs:
            form.setdefault(key, []).append(value)
        expected_form = {"acctNo", "beginDate", "endDate", "sessionId", "sid"}
        if (
            set(form) != expected_form or any(len(values) != 1 for values in form.values())
            or form["acctNo"] != [identity]
            or form["beginDate"] != [start.strftime("%Y%m%d")]
            or form["endDate"] != [end.strftime("%Y%m%d")]
            or not form["sessionId"][0] or not form["sid"][0]
        ):
            raise RuntimeError(error)

        payload = hit.resp_json
        if not isinstance(payload, dict) or set(payload) != {"RespCode", "RespBody"}:
            raise RuntimeError(error)
        code = payload["RespCode"]
        body = payload["RespBody"]
        if (
            not isinstance(code, dict)
            or set(code) != {"RtnCode", "RtnDesc", "SvcName", "Time"}
            or any(not isinstance(value, str) for value in code.values())
            or code.get("SvcName") != "IBKB010102"
            or re.search(
                r"(?:登入|失效|逾時|錯誤|error|timeout|session|expired|reauth|log\s*in)",
                code.get("RtnDesc", ""), re.I,
            )
        ):
            raise RuntimeError(error)
        if code["RtnCode"] == "UB112":
            if body != {}:
                raise RuntimeError(error)
            return {"records": [], "status": "explicit_empty", "rows": 0}
        if (
            code["RtnCode"] != "0000" or not isinstance(body, dict)
            or set(body) != {"Account", "NTDetailList", "NTTotal"}
            or body["Account"] != identity or not isinstance(body["NTDetailList"], list)
            or not body["NTDetailList"]
            or not isinstance(body["NTTotal"], dict)
            or cls._nttotal_claims_more_pages(body["NTTotal"])
        ):
            raise RuntimeError(error)
        row_keys = {
            "AccountDate", "Balance", "Expenditure", "Income", "PS", "Summary",
            "TraDate", "TraSum", "TraTime",
        }
        rows = body["NTDetailList"]
        for row in rows:
            if not isinstance(row, dict) or set(row) != row_keys:
                raise RuntimeError(error)
            if any(not isinstance(row[key], str) for key in row_keys):
                raise RuntimeError(error)
            transacted = cls._strict_slash_date(row["TraDate"], error)
            cls._strict_slash_date(row["AccountDate"], error)
            if not start <= transacted <= end:
                raise RuntimeError(error)
            if re.fullmatch(r"\d{2}:\d{2}:\d{2}", row["TraTime"]) is None:
                raise RuntimeError(error)
            try:
                datetime.strptime(row["TraTime"], "%H:%M:%S")
            except ValueError:
                raise RuntimeError(error) from None
            if any(len(row[key]) > 2_000 for key in ("Summary", "TraSum", "PS")):
                raise RuntimeError(error)
            for key in ("Expenditure", "Income", "Balance"):
                cls._strict_twd_amount(row[key], error)
            if row["Expenditure"] not in {"", "-"} and row["Income"] not in {"", "-"}:
                raise RuntimeError(error)
        return {"records": rows, "status": "complete", "rows": len(rows)}

    @staticmethod
    def _validate_twd_dom(state, *, status: str, rows: int) -> None:
        error = "ubot-twd-history-result"
        keys = {
            "visible_tables", "visible_rows", "pagers", "busy", "dialogs",
            "stale_tables", "quiet_ms",
        }
        if (
            not isinstance(state, dict) or set(state) != keys
            or any(type(state[key]) is not int or state[key] < 0 for key in keys)
            or state["pagers"] or state["busy"] or state["dialogs"]
            or state["stale_tables"] or state["quiet_ms"] < 2_000
        ):
            raise RuntimeError(error)
        expected = (2, rows + 1) if status == "complete" else (0, 0)
        if rows < 0 or (state["visible_tables"], state["visible_rows"]) != expected:
            raise RuntimeError(error)

    @staticmethod
    def _twd_form_snapshot(page):
        return page.evaluate(r"""() => {
          const visible = e => {
            if (!e || !(e.offsetWidth || e.offsetHeight || e.getClientRects().length)) return false;
            for (let node = e; node; node = node.parentElement) {
              const style = getComputedStyle(node);
              if (node.hidden || (node.getAttribute('aria-hidden') || '').toLowerCase() === 'true' ||
                  style.display === 'none' || style.visibility === 'hidden' ||
                  style.visibility === 'collapse' || Number(style.opacity) === 0) return false;
            }
            return true;
          };
          const selects = [...document.querySelectorAll('select')].filter(visible).map(select => ({
            enabled: !select.disabled && (select.getAttribute('aria-disabled') || '').toLowerCase() !== 'true',
            options: [...select.options].map(option => ({
              text: (option.textContent || '').replace(/\s+/g, ' ').trim(),
              value: option.value || '',
            })),
          }));
          const search_buttons = [...document.querySelectorAll('button')].filter(button =>
            visible(button) && !button.disabled &&
            (button.textContent || '').replace(/\s+/g, ' ').trim() === '搜尋'
          ).length;
          return {selects, search_buttons};
        }""")

    @staticmethod
    def _mark_twd_dom_boundary(page) -> None:
        page.evaluate(r"""() => {
          if (window.__thothUbotHistoryObserver) window.__thothUbotHistoryObserver.disconnect();
          window.__thothUbotHistoryBoundary = new Map(
            [...document.querySelectorAll('table')].map(table => [table, table.innerHTML])
          );
          window.__thothUbotHistoryLastMutation = performance.now();
          window.__thothUbotHistoryObserver = new MutationObserver(() => {
            window.__thothUbotHistoryLastMutation = performance.now();
          });
          window.__thothUbotHistoryObserver.observe(document.documentElement, {
            subtree: true, childList: true, attributes: true, characterData: true,
          });
        }""")

    @classmethod
    def _wait_for_twd_dom_settle(cls, page):
        state = None
        for elapsed in range(500, 10_001, 500):
            page.wait_for_timeout(500)
            state = cls._twd_dom_snapshot(page)
            if (
                elapsed >= 5_000 and isinstance(state, dict)
                and state.get("quiet_ms", 0) >= 2_000
            ):
                return state
        return state

    @staticmethod
    def _twd_dom_snapshot(page):
        return page.evaluate(r"""() => {
          const visible = e => {
            if (!e || !(e.offsetWidth || e.offsetHeight || e.getClientRects().length)) return false;
            for (let node = e; node; node = node.parentElement) {
              const style = getComputedStyle(node);
              if (node.hidden || (node.getAttribute('aria-hidden') || '').toLowerCase() === 'true' ||
                  style.display === 'none' || style.visibility === 'hidden' ||
                  style.visibility === 'collapse' || Number(style.opacity) === 0) return false;
            }
            return true;
          };
          const enabled = e => !e.disabled && (e.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
          const visibleTables = [...document.querySelectorAll('table')].filter(visible);
          const visibleRows = [...document.querySelectorAll('tbody tr')].filter(visible);
          const boundary = window.__thothUbotHistoryBoundary;
          const staleTables = boundary instanceof Map
            ? visibleTables.filter(table => boundary.has(table) && boundary.get(table) === table.innerHTML)
            : [];
          const interactivePagers = [...document.querySelectorAll('a,button,input,select,[role=button]')].filter(e => {
            if (!visible(e) || !enabled(e)) return false;
            const text = (e.textContent || e.value || '').replace(/\s+/g, ' ').trim();
            const meta = [e.id, e.getAttribute('class'), e.getAttribute('aria-label'), e.getAttribute('rel'),
              e.getAttribute('href'), e.getAttribute('onclick'), e.getAttribute('data-page')]
              .filter(Boolean).join(' ');
            return /^(?:下一頁|下頁|next|[>»]|[2-9]\d*)$/i.test(text) ||
              /(?:pagination|paginator|page[-_: ]?next|rel[=: ]?next|[?&]page=[2-9]\d*)/i.test(meta);
          });
          const structuralPagers = [...document.querySelectorAll(
            'nav,[class*=pagination i],[class*=paginator i],[aria-label*=pagination i],[aria-label*=pages i]'
          )].filter(e => {
            if (!visible(e)) return false;
            const text = (e.textContent || '').replace(/\s+/g, ' ').trim();
            return /(?:下一頁|下頁|next|[>»])/i.test(text) ||
              (text.match(/\b(?:[2-9]|\d{2,})\b/g) || []).length > 0;
          });
          const activeContainer = e => {
            for (let node = e.parentElement; node; node = node.parentElement) {
              const style = getComputedStyle(node);
              if (node.hidden || (node.getAttribute('aria-hidden') || '').toLowerCase() === 'true' ||
                  style.display === 'none' || style.visibility === 'hidden' ||
                  style.visibility === 'collapse' || Number(style.opacity) === 0) return false;
            }
            return true;
          };
          const hiddenPageMetadata = [...document.querySelectorAll('input[type=hidden]')].filter(e => {
            const name = [e.name, e.id, e.getAttribute('data-page')].filter(Boolean).join(' ');
            const value = e.value || e.getAttribute('data-page') || '';
            return activeContainer(e) && /(?:page|頁)/i.test(name) && /^[2-9]\d*$/.test(value);
          });
          const pagers = interactivePagers.length + structuralPagers.length + hiddenPageMetadata.length;
          const busy = [...document.querySelectorAll(
            'progress,[role=progressbar],[aria-busy=true],[class*=loading i],[class*=spinner i],[class*=busy i]'
          )].filter(visible).length;
          const dialogs = [...document.querySelectorAll(
            'dialog[open],[role=dialog],[aria-modal=true],.modal.show,.modal.in,[role=alert],.alert,.error'
          )].filter(visible).length;
          const lastMutation = window.__thothUbotHistoryLastMutation;
          const quietMs = typeof lastMutation === 'number'
            ? Math.max(0, Math.floor(performance.now() - lastMutation)) : 0;
          return {visible_tables: visibleTables.length, visible_rows: visibleRows.length,
            pagers, busy, dialogs, stale_tables: staleTables.length, quiet_ms: quietMs};
        }""")

    def _collect_twd_history(
        self, page, collector: ResponseCollector, *, as_of: date | None = None,
    ) -> dict:
        as_of = as_of or datetime.now(ZoneInfo("Asia/Taipei")).date()
        def ensure_no_dialog() -> None:
            if getattr(self, "_shared_dialog_blocked", False):
                raise RuntimeError("ubot-twd-history-dialog")

        mode = os.environ.get("BANK_CRAWLER_HISTORY_MODE", "full")
        if mode not in {"full", "incremental"}:
            raise RuntimeError("ubot-twd-history-mode")
        endpoint = "IBKB010102"
        response_before = len(collector.by_endpoint(endpoint))
        issued_before = collector.issued_count(endpoint)

        self._goto(page, "/B0101001", wait=6500)
        ensure_no_dialog()
        inventory_with_index = self._validate_twd_form(
            self._twd_form_snapshot(page), as_of=as_of,
        )
        inventory = [
            {key: item[key] for key in ("label", "identity", "currency")}
            for item in inventory_with_index
        ]
        expected = []
        coverage_windows = []
        results = []
        operation_bytes = 0
        expected_queries = 0

        for item in inventory_with_index:
            start, end = self._history_range(item["identity"], end=as_of, mode=mode)
            windows = self._history_windows(start, end)
            expected.append({
                "identity": item["identity"], "start": start.isoformat(), "end": end.isoformat(),
            })
            for window_start, window_end in windows:
                expected_queries += 1
                self._goto(page, "/B0101001", wait=6500)
                ensure_no_dialog()
                current_inventory = self._validate_twd_form(
                    self._twd_form_snapshot(page), as_of=as_of,
                )
                if current_inventory != inventory_with_index:
                    raise RuntimeError("ubot-twd-history-inventory-changed")
                selects = page.query_selector_all("select")
                if len(selects) != 2:
                    raise RuntimeError("ubot-twd-history-form")
                selects[0].select_option(index=item["index"])
                page.wait_for_timeout(500)
                selects[1].select_option(label=f"{window_start.month}月份")
                page.wait_for_timeout(500)
                button = _unique_visible_enabled_exact(page, "button", "搜尋")
                if button is None:
                    raise RuntimeError("ubot-twd-history-form")

                before = len(collector.by_endpoint(endpoint))
                request_boundary = collector.request_sequence
                window_issued_before = collector.issued_count(endpoint)
                ensure_no_dialog()
                self._mark_twd_dom_boundary(page)
                button.click(timeout=8000)
                for _ in range(60):
                    if len(collector.by_endpoint(endpoint)) > before:
                        break
                    page.wait_for_timeout(500)
                hits = collector.by_endpoint(endpoint)[before:]
                if (
                    len(hits) != 1
                    or collector.issued_count(endpoint) - window_issued_before != 1
                ):
                    raise RuntimeError("ubot-twd-history-response-cardinality")
                ensure_no_dialog()
                validated = self._validate_history_hit(
                    hits[0], identity=item["identity"], start=window_start,
                    end=window_end, after_sequence=request_boundary,
                )
                body_size = hits[0].body_size
                if type(body_size) is not int:
                    raise RuntimeError("ubot-twd-history-byte-budget")
                operation_bytes += body_size
                if operation_bytes > 5_000_000:
                    raise RuntimeError("ubot-twd-history-byte-budget")
                ensure_no_dialog()
                self._validate_twd_dom(
                    self._wait_for_twd_dom_settle(page), status=validated["status"],
                    rows=validated["rows"],
                )
                ensure_no_dialog()
                receipt = {
                    "identity": item["identity"],
                    "start": window_start.isoformat(),
                    "end": window_end.isoformat(),
                    "status": validated["status"],
                    "pages": 1,
                    "rows": validated["rows"],
                }
                results.append({
                    "Account": item["identity"],
                    "NTDetailList": validated["records"],
                    "NTTotal": hits[0].resp_json.get("RespBody", {}).get("NTTotal", {}),
                    "receipt": receipt,
                })
                coverage_windows.append({
                    key: receipt[key]
                    for key in ("identity", "start", "end", "status", "pages")
                })

        fresh_hits = collector.by_endpoint(endpoint)[response_before:]
        fresh_sizes = [hit.body_size for hit in fresh_hits if type(hit.body_size) is int]
        if (
            len(fresh_hits) != expected_queries
            or collector.issued_count(endpoint) - issued_before != expected_queries
            or len(fresh_sizes) != len(fresh_hits)
            or operation_bytes != sum(fresh_sizes)
        ):
            raise RuntimeError("ubot-twd-history-operation-cardinality")
        ensure_no_dialog()
        coverage = {
            "mode": mode,
            "as_of": as_of.isoformat(),
            "domains": [{
                "domain": "twd_transactions",
                "expected": expected,
                "windows": coverage_windows,
            }],
        }
        validate_history_coverage(
            coverage, expected_mode=mode, expected_domains=self.HISTORY_COVERAGE_DOMAINS,
        )
        return {"results": results, "inventory": inventory, "coverage": coverage}

    def _logged_in(self, page) -> bool:
        """正向訊號：URL 在內銀區 + innerText >= 500 + 命中 >= 2 個菜單字 + 無 login form。

        取代「#sid 不在 = 已登入」negative-only 訊號（內銀區頁面本來就沒 #sid）。
        詳見 wiki/concepts/bank-crawler-login-positive-signal-rule.md
        """
        try:
            if not self._exact_https_origin_allowed(page.url, self.CREDENTIAL_HOSTS):
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
        all_phases = tuple(CheckpointPhase)
        return (
            LoginCheckpointRule(
                name="ubot-password-change-required",
                bank="ubot",
                phases=all_phases,
                kind=CheckpointKind.PASSWORD_CHANGE_REQUIRED,
                container_selector=".modal.show",
                required_body_pattern=_REQUIRED_PASSWORD_PATTERN,
            ),
            LoginCheckpointRule(
                name="ubot-otp-required",
                bank="ubot",
                phases=all_phases,
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
                    "確認",
                ),
                required_body_pattern=_OPTIONAL_PASSWORD_PATTERN,
            ),
            LoginCheckpointRule(
                name="ubot-unknown-modal",
                bank="ubot",
                phases=all_phases,
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
                candidates = page.locator(selector)
                if candidates.count() != 1:
                    raise UbotLoginError("登入欄位無法安全填寫；未送出登入")
                field = candidates.nth(0)
                if not field.is_visible() or not field.is_enabled():
                    raise UbotLoginError("登入欄位無法安全填寫；未送出登入")
                field.click()
                field.click(click_count=3)
                page.keyboard.press("Backspace")
                page.keyboard.type(value, delay=80)
                page.wait_for_timeout(wait)
                if len(field.input_value()) != len(value):
                    raise UbotLoginError("登入欄位輸入長度不符；未送出登入")
        except Exception:
            raise UbotLoginError("登入欄位無法安全填寫；未送出登入") from None

        captcha = self._ocr_with_regen(page, max_attempts=5)
        if not captcha:
            raise UbotLoginError(
                "圖形驗證碼 OCR 失敗；未送出登入",
                safe_code="captcha_ocr_failed",
            )
        try:
            candidates = page.locator(SEL_CAPTCHA)
            if candidates.count() != 1:
                raise UbotLoginError("驗證碼欄位無法安全填寫；未送出登入")
            field = candidates.nth(0)
            if not field.is_visible() or not field.is_enabled():
                raise UbotLoginError("驗證碼欄位無法安全填寫；未送出登入")
            field.click()
            field.click(click_count=3)
            page.keyboard.press("Backspace")
            page.keyboard.type(captcha, delay=80)
            page.wait_for_timeout(200)
            if len(field.input_value()) != len(captcha):
                raise UbotLoginError("驗證碼欄位輸入長度不符；未送出登入")
        except Exception:
            raise UbotLoginError("驗證碼欄位無法安全填寫；未送出登入") from None

        button = _unique_visible_enabled_exact(
            page,
            "button",
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
            button.click(timeout=8000)
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

        # 2) 台幣交易明細（B0101001）：逐帳戶查銀行原生三個月份
        twd_history = self._collect_twd_history(page, collector)
        out["twd_txns"] = twd_history["results"]
        out["debit_accounts"] = twd_history["inventory"]
        out["history_coverage"] = twd_history["coverage"]

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
