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
import json
import os
import re
import sys
from calendar import monthrange
from datetime import date, datetime
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
    _OriginGuardProxy,
)
from backend.core.card_bills import make_card_bill_fact, publish_card_bill_facts
from backend.core.error_codes import error_step, with_error_code, exception_error_id
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
        candidates = _native_step('CTBC-02-100-001', lambda: page.locator(SEL_SUBMIT))
        eligible = []
        for index in range(_native_step('CTBC-02-101-001', lambda: candidates.count())):
            candidate = _native_step('CTBC-02-102-001', lambda: candidates.nth(index))
            classes = (_native_step('CTBC-02-103-001', lambda: candidate.get_attribute("class")) or "").split()
            if (
                _native_step('CTBC-02-104-001', lambda: candidate.is_visible())
                and _native_step('CTBC-02-105-001', lambda: candidate.is_enabled())
                and "disabled" not in classes
                and " ".join(_native_step('CTBC-02-106-001', lambda: candidate.inner_text()).split()) == "登入"
            ):
                eligible.append(candidate)
        if len(eligible) != 1:
            raise with_error_code(CtbcLoginError("找不到唯一且可操作的登入按鈕；未送出登入"), "CTBC-02-050-001")
        button = eligible[0]
    except CtbcLoginError:
        raise
    except Exception as error:
        raise with_error_code(CtbcLoginError("無法安全確認登入按鈕；未送出登入"), exception_error_id(error, bank="ctbc") or "CTBC-02-050-002") from None
    try:
        _native_step('CTBC-02-107-001', lambda: button.click(timeout=8000))
    except Exception as error:
        raise with_error_code(CtbcLoginError("登入送出狀態不明；禁止自動重試"), exception_error_id(error, bank="ctbc") or "CTBC-02-050-003") from None

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


_CARD_NAV_STEPS = {'即時消費明細': ('CTBC-06-500-001',
            'CTBC-06-501-001',
            'CTBC-06-502-001',
            'CTBC-06-503-001',
            'CTBC-06-504-001',
            'CTBC-06-505-001',
            'CTBC-06-506-001',
            'CTBC-06-507-001',
            'CTBC-06-508-001',
            'CTBC-06-509-001',
            'CTBC-06-510-001',
            'CTBC-06-511-001',
            'CTBC-06-512-001',
            'CTBC-06-513-001'),
 '帳單明細查詢': ('CTBC-06-520-001',
            'CTBC-06-521-001',
            'CTBC-06-522-001',
            'CTBC-06-523-001',
            'CTBC-06-524-001',
            'CTBC-06-525-001',
            'CTBC-06-526-001',
            'CTBC-06-527-001',
            'CTBC-06-528-001',
            'CTBC-06-529-001',
            'CTBC-06-530-001',
            'CTBC-06-531-001',
            'CTBC-06-532-001',
            'CTBC-06-533-001'),
 '未出帳單明細': ('CTBC-06-540-001',
            'CTBC-06-541-001',
            'CTBC-06-542-001',
            'CTBC-06-543-001',
            'CTBC-06-544-001',
            'CTBC-06-545-001',
            'CTBC-06-546-001',
            'CTBC-06-547-001',
            'CTBC-06-548-001',
            'CTBC-06-549-001',
            'CTBC-06-550-001',
            'CTBC-06-551-001',
            'CTBC-06-552-001',
            'CTBC-06-553-001'),
 '信用卡繳款記錄': ('CTBC-06-560-001',
             'CTBC-06-561-001',
             'CTBC-06-562-001',
             'CTBC-06-563-001',
             'CTBC-06-564-001',
             'CTBC-06-565-001',
             'CTBC-06-566-001',
             'CTBC-06-567-001',
             'CTBC-06-568-001',
             'CTBC-06-569-001',
             'CTBC-06-570-001',
             'CTBC-06-571-001',
             'CTBC-06-572-001',
             'CTBC-06-573-001')}

_CARD_WAIT_STEPS = {'即時消費明細': 'CTBC-06-600-001',
 '帳單明細查詢': 'CTBC-06-601-001',
 '未出帳單明細': 'CTBC-06-602-001',
 '信用卡繳款記錄': 'CTBC-06-603-001'}

_NATIVE_DATE_STEPS = {'起日': ('CTBC-05-700-001',
        'CTBC-05-700-002',
        'CTBC-05-701-001',
        'CTBC-05-701-002',
        'CTBC-05-702-001',
        'CTBC-05-703-001',
        'CTBC-05-704-001',
        'CTBC-05-705-001',
        'CTBC-05-706-001',
        'CTBC-05-706-002'),
 '迄日': ('CTBC-05-720-001',
        'CTBC-05-720-002',
        'CTBC-05-721-001',
        'CTBC-05-721-002',
        'CTBC-05-722-001',
        'CTBC-05-723-001',
        'CTBC-05-724-001',
        'CTBC-05-725-001',
        'CTBC-05-726-001',
        'CTBC-05-726-002')}

def _native_step(error_id, operation):
    """Keep expression short-circuiting while tagging one native operation."""
    with error_step(error_id):
        return operation()


def _log(*a):
    print(*a, file=sys.stderr)


def _filter_valid_ctbc_details(
    detail_list_raw: list,
    *,
    start: date | None = None,
    end: date | None = None,
    failures: list[str] | None = None,
) -> tuple[list[dict], int]:
    """CTBC qu002/011 detailList raw → (filtered, skipped_count).

    Schema validate: 每筆合法 detail 必有 actDtTm (persist 算 txn_datetime 的唯一
    來源, txn_datetime 在 PG 端是 NOT NULL). 缺欄或非 dict → skip + count.

    抽成純函式方便 test (collector 主流程 mock SPA 太重, 此函式不依賴 page/SPA).
    2026-06-22 prod 06-22 04:00 job#152 NotNullViolation 修法.
    """
    with error_step("CTBC-07-030-001"):
        if failures is not None:
            failures.clear()
        out: list[dict] = []
        skipped = 0
        for d in detail_list_raw or []:
            if not isinstance(d, dict):
                if failures is not None and skipped == 0:
                    failures.append("CTBC-07-030-002")
                skipped += 1
                continue
            if not (d.get("actDtTm") or "").strip():
                if failures is not None and skipped == 0:
                    failures.append("CTBC-07-030-003")
                skipped += 1
                continue
            if start is not None and end is not None:
                try:
                    d = _validated_ctbc_detail(d, start=start, end=end)
                except ValueError as error:
                    if failures is not None and skipped == 0:
                        error_id = exception_error_id(error, bank="ctbc")
                        if error_id is not None:
                            failures.append(error_id)
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
_AMOUNT_FIELD_CODES = {'dbAmt': ('CTBC-05-800-001', 'CTBC-05-800-002', 'CTBC-05-800-003', 'CTBC-05-800-004', 'CTBC-05-800-005', 'CTBC-05-800-006', 'CTBC-05-800-007', 'CTBC-05-800-008'), 'crAmt': ('CTBC-05-801-001', 'CTBC-05-801-002', 'CTBC-05-801-003', 'CTBC-05-801-004', 'CTBC-05-801-005', 'CTBC-05-801-006', 'CTBC-05-801-007', 'CTBC-05-801-008'), 'balanceAmt': ('CTBC-05-802-001', 'CTBC-05-802-002', 'CTBC-05-802-003', 'CTBC-05-802-004', 'CTBC-05-802-005', 'CTBC-05-802-006', 'CTBC-05-802-007', 'CTBC-05-802-008')}


def _valid_bearer(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"Bearer [^\s]+", value) is not None


def _record_rejection(rejections, error_id):
    if rejections is not None:
        rejections.append(error_id)
    return False


def _ctbc_request_envelope_valid(body, resource: str, *, rejections=None) -> bool:
    if not isinstance(body, dict):
        return _record_rejection(rejections, "CTBC-05-051-001")
    if body.get("resource") != resource:
        return _record_rejection(rejections, "CTBC-05-051-002")
    if not isinstance(body.get("rqData"), dict):
        return _record_rejection(rejections, "CTBC-05-051-003")
    strings = {
        "deviceIxd", "trackingIxd", "txnIxd", "model", "platform", "version",
        "runtime", "network", "appVer", "clientNo", "token", "locale",
        "fromSys", "seed", "deviceToken",
    }
    integers = {"runtimeVer", "clientTime"}
    if set(body) != {"resource", "rqData"}:
        if set(body) != strings | integers | {"resource", "rqData"}:
            return _record_rejection(rejections, "CTBC-05-051-004")
        for key in strings:
            if type(body[key]) is not str:
                return _record_rejection(rejections, "CTBC-05-051-005")
        for key in integers:
            if type(body[key]) is not int:
                return _record_rejection(rejections, "CTBC-05-051-006")
    try:
        if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > 16_384:
            return _record_rejection(rejections, "CTBC-05-051-007")
        return True
    except (TypeError, ValueError, RecursionError):
        return _record_rejection(rejections, "CTBC-05-051-008")


def _ctbc_raw_url_matches(stored, raw, *, rejections=None) -> bool:
    if raw.scheme != stored.scheme:
        return _record_rejection(rejections, "CTBC-05-061-001")
    if raw.hostname != stored.hostname:
        return _record_rejection(rejections, "CTBC-05-061-002")
    if raw.port != stored.port:
        return _record_rejection(rejections, "CTBC-05-061-003")
    if raw.username is not None:
        return _record_rejection(rejections, "CTBC-05-061-004")
    if raw.password is not None:
        return _record_rejection(rejections, "CTBC-05-061-005")
    if raw.path != stored.path:
        return _record_rejection(rejections, "CTBC-05-061-006")
    if stored.params:
        return _record_rejection(rejections, "CTBC-05-061-007")
    if raw.params:
        return _record_rejection(rejections, "CTBC-05-061-008")
    if raw.fragment:
        return _record_rejection(rejections, "CTBC-05-061-009")
    if not raw.query:
        return _record_rejection(rejections, "CTBC-05-061-010")
    try:
        pairs = parse_qsl(raw.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return _record_rejection(rejections, "CTBC-05-061-011")
    if len(pairs) != 1:
        return _record_rejection(rejections, "CTBC-05-061-012")
    if pairs[0][0] != "IIhfvu":
        return _record_rejection(rejections, "CTBC-05-061-013")
    if not 1 <= len(pairs[0][1]) <= 4096:
        return _record_rejection(rejections, "CTBC-05-061-014")
    if not all(0x21 <= ord(char) <= 0x7E for char in pairs[0][1]):
        return _record_rejection(rejections, "CTBC-05-061-015")
    return True

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


def _ctbc_amount(value, *, non_negative: bool = False, _codes=('CTBC-07-010-001', 'CTBC-07-010-002', 'CTBC-07-010-003', 'CTBC-07-010-004', 'CTBC-07-010-005', 'CTBC-07-010-006', 'CTBC-07-010-007', 'CTBC-07-011-001')) -> int:
    with error_step(_codes[7]):
        if isinstance(value, bool):
            raise with_error_code(ValueError("invalid amount"), _codes[0])
        raw = str(value).strip()
        if not _CTBC_AMOUNT_RE.fullmatch(raw):
            raise with_error_code(ValueError("invalid amount"), _codes[1])
        try:
            amount = Decimal(raw.replace(",", ""))
        except (InvalidOperation, ValueError):
            raise with_error_code(ValueError("invalid amount"), _codes[2]) from None
        if not amount.is_finite():
            raise with_error_code(ValueError("invalid amount"), _codes[3])
        if amount != amount.to_integral_value():
            raise with_error_code(ValueError("invalid amount"), _codes[4])
        if not -_PG_INTEGER_MAX <= amount <= _PG_INTEGER_MAX:
            raise with_error_code(ValueError("invalid amount"), _codes[5])
        if non_negative and amount < 0:
            raise with_error_code(ValueError("invalid amount"), _codes[6])
        return int(amount)


def _validated_ctbc_detail(row: dict, *, start: date, end: date) -> dict:
    with error_step("CTBC-07-021-001"):
        raw_datetime = row.get("actDtTm")
        if not isinstance(raw_datetime, str):
            raise with_error_code(ValueError("invalid row"), "CTBC-07-020-001")
        if raw_datetime != raw_datetime.strip():
            raise with_error_code(ValueError("invalid row"), "CTBC-07-020-002")
        # Live 2026-10-02: the native qu002 form returns "YYYY-MM-DD HH:MM:SS.ffff"; canonicalize to the API form.
        if native := re.fullmatch(r"(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2})(\.\d+)?", raw_datetime):
            day, hh, mm, ss, frac = native.groups()
            raw_datetime = f"{day}-{hh}.{mm}.{ss}{frac or ''}"
        if not re.fullmatch('\\d{4}-\\d{2}-\\d{2}-\\d{2}\\.\\d{2}\\.\\d{2}(?:\\.\\d+)?', raw_datetime):
            raise with_error_code(ValueError("invalid row"), "CTBC-07-020-003")
        try:
            datetime.strptime(raw_datetime[:19], "%Y-%m-%d-%H.%M.%S")
        except ValueError:
            raise with_error_code(ValueError("invalid row"), "CTBC-07-020-004") from None
        raw_account_date = row.get("trnDtRaw")
        if raw_account_date is not None:
            if not isinstance(raw_account_date, str):
                raise with_error_code(ValueError("invalid row"), "CTBC-07-020-005")
            if not re.fullmatch('\\d{8}', raw_account_date):
                raise with_error_code(ValueError("invalid row"), "CTBC-07-020-006")
            try:
                datetime.strptime(raw_account_date, "%Y%m%d")
            except ValueError:
                raise with_error_code(ValueError("invalid row"), "CTBC-07-020-007") from None
        if row.get('dbAmt') is None and row.get('crAmt') is None:
            raise with_error_code(ValueError("invalid row"), "CTBC-07-020-008")
        normalized = dict(row)
        normalized["actDtTm"] = raw_datetime
        for field in ("dbAmt", "crAmt", "balanceAmt"):
            value = row.get(field)
            if value is not None:
                normalized[field] = _ctbc_amount(value, non_negative=field != "balanceAmt", _codes=_AMOUNT_FIELD_CODES[field])
        for field, code in (('memo1', 'CTBC-05-810-001'), ('memo2', 'CTBC-05-811-001'), ('bankId', 'CTBC-05-812-001'), ('trfAcct', 'CTBC-05-813-001'), ('memoCode', 'CTBC-05-814-001')):
            value = row.get(field)
            if value is not None and (not isinstance(value, str)):
                raise with_error_code(ValueError("invalid row"), code)
        return normalized


def _build_qu002_011_post_body(account_id: str, month_type: str, template_body: dict) -> dict:
    """從 SPA 既有 qu002/011 req_body 當模板, 套新 (accountId, type) 算同層 POST body.

    Template body 範例 (collector 從 SPA 自動 fire 那次抄來):
      {"resource":"/twrbc-deposit/qu002/011", "rqData":{"accountId":"...", "type":"m0", ...}}

    回傳同 shape, rqData.accountId / type 改成 caller 指定. 其他欄位 (e.g. encrypt
    flags, locale) 原樣保留 — SPA 怎麼打我們就怎麼打.

    純函式方便 test. month_type 必須 m0~m5 之一.
    """
    with error_step("CTBC-05-011-001"):
        if month_type not in _CTBC_MONTHS:
            raise with_error_code(ValueError(f"month_type 必須是 {_CTBC_MONTHS} 之一, got: {month_type!r}"), "CTBC-05-010-001")
        if not isinstance(template_body, dict):
            raise with_error_code(ValueError(f"template_body 必須是 dict, got: {type(template_body).__name__}"), "CTBC-05-010-002")
        new_body = {**template_body}
        rq = dict(template_body.get("rqData") or {})
        rq["accountId"] = account_id
        rq["type"] = month_type
        new_body["rqData"] = rq
        return new_body


class CtbcCrawler(BankCrawler):
    USES_SHARED_LOGIN_CHECKPOINTS: ClassVar[bool] = True
    SAFE_COLLECT_GUARDS = frozenset({
        "count-mismatch",
        "ctbc-twd-history-fetch",
        "ctbc-twd-history-inventory",
        "ctbc-twd-history-mode",
        "ctbc-twd-history-range",
        "ctbc-twd-history-row",
        "ctbc-twd-history-template",
        "invalid-request",
        "invalid-response",
        "missing-pagination-proof",
        "ownership-mismatch",
        "pagination-detected",
    })
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

    def _logged_in(self, page, *, _auth_probe_ids=None) -> bool:
        if _auth_probe_ids is not None:
            _auth_probe_ids.clear()
        try:
            current = urlparse(_native_step('CTBC-03-010-001', lambda: page.url) or "")
            if (
                current.scheme.lower() != "https"
                or (current.hostname or "").lower() != "www.ctbcbank.com"
                or current.port not in (None, 443)
                or current.username is not None
                or current.password is not None
            ):
                return False
            r = _native_step('CTBC-03-011-001', lambda: page.evaluate(JS_LOGGED_IN_POSITIVE))
        except Exception as error:
            if _auth_probe_ids is not None:
                error_id = exception_error_id(error, bank='ctbc')
                if error_id in ('CTBC-03-010-001', 'CTBC-03-011-001'):
                    _auth_probe_ids[:] = [error_id]
            return False
        if isinstance(r, dict):
            return bool(r.get("ok"))
        return bool(r)

    # ---------- 登入 ----------
    def login(self, page) -> bool:
        return self._shared_login(page)


    def prepare_login_page(self, page) -> None:
        _native_step('CTBC-01-010-001', lambda: page.wait_for_timeout(8000))

    def is_authenticated(self, page) -> bool:
        auth_probe_ids = getattr(self, "_login_auth_probe_ids", None)
        return (
            self._logged_in(page) if auth_probe_ids is None
            else self._logged_in(page, _auth_probe_ids=auth_probe_ids)
        )

    def login_checkpoint_rules(self) -> tuple[LoginCheckpointRule, ...]:
        post_phases = (
            CheckpointPhase.POST_SUBMIT,
            CheckpointPhase.POST_SUBMIT_SETTLE,
        )
        return (
            LoginCheckpointRule(
                name="ctbc-entry-announcement",
                error_id='CTBC-03-800-001',
                bank="ctbc",
                phases=(CheckpointPhase.PRE_SUBMIT,),
                kind=CheckpointKind.DISMISSIBLE_NOTICE,
                container_selector=".modal.show",
                action_selector="a.btn_close",
                required_body_pattern=re.compile(r"^\s*重要公告(?:\s|$)"),
            ),
            LoginCheckpointRule(
                name="ctbc-otp-required",
                error_id='CTBC-03-050-001',
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
                error_id='CTBC-03-805-001',
                bank="ctbc",
                phases=post_phases,
                kind=CheckpointKind.DUPLICATE_SESSION,
                container_selector=".modal.show",
                action_texts=("確認登入",),
                required_body_pattern=re.compile(r"^\s*確認訊息[\s\S]*確認登入\s*$"),
            ),
            LoginCheckpointRule(
                name="ctbc-unknown-modal",
                error_id='CTBC-03-055-001',
                bank="ctbc",
                phases=tuple(CheckpointPhase),
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector=".modal.show",
            ),
            LoginCheckpointRule(
                name="ctbc-unknown-dialog",
                error_id='CTBC-03-060-001',
                bank="ctbc",
                phases=tuple(CheckpointPhase),
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector="[role='dialog']",
            ),
        )

    def submit_credentials_once(self, page) -> None:
        try:
            with error_step('CTBC-02-001-001'):
                page.wait_for_selector(SEL_ID, state="visible", timeout=15000)
            for selector, value, codes in (
                (SEL_ID, self.creds.national_id, ('CTBC-02-010-001', 'CTBC-02-010-002', 'CTBC-02-010-003', 'CTBC-02-010-004', 'CTBC-02-010-005', 'CTBC-02-010-006', 'CTBC-02-010-007', 'CTBC-02-010-008', 'CTBC-02-010-009', 'CTBC-02-010-010', 'CTBC-02-010-011', 'CTBC-02-010-012', 'CTBC-02-010-013', 'CTBC-02-010-014', 'CTBC-02-010-015')),
                (SEL_USER, self.creds.user_code, ('CTBC-02-020-001', 'CTBC-02-020-002', 'CTBC-02-020-003', 'CTBC-02-020-004', 'CTBC-02-020-005', 'CTBC-02-020-006', 'CTBC-02-020-007', 'CTBC-02-020-008', 'CTBC-02-020-009', 'CTBC-02-020-010', 'CTBC-02-020-011', 'CTBC-02-020-012', 'CTBC-02-020-013', 'CTBC-02-020-014', 'CTBC-02-020-015')),
                (SEL_PWD, self.creds.password, ('CTBC-02-030-001', 'CTBC-02-030-002', 'CTBC-02-030-003', 'CTBC-02-030-004', 'CTBC-02-030-005', 'CTBC-02-030-006', 'CTBC-02-030-007', 'CTBC-02-030-008', 'CTBC-02-030-009', 'CTBC-02-030-010', 'CTBC-02-030-011', 'CTBC-02-030-012', 'CTBC-02-030-013', 'CTBC-02-030-014', 'CTBC-02-030-015')),
            ):
                with error_step(codes[0]):
                    fields = page.locator(selector)
                with error_step(codes[1]):
                    count = fields.count()
                if count != 1:
                    raise with_error_code(CtbcLoginError('登入欄位無法安全填寫；未送出登入'), codes[2])
                with error_step(codes[3]):
                    field = fields.nth(0)
                with error_step(codes[4]):
                    visible = field.is_visible()
                if not visible:
                    raise with_error_code(CtbcLoginError('登入欄位無法安全填寫；未送出登入'), codes[5])
                with error_step(codes[6]):
                    enabled = field.is_enabled()
                if not enabled:
                    raise with_error_code(CtbcLoginError('登入欄位無法安全填寫；未送出登入'), codes[7])
                with error_step(codes[8]):
                    field.click()
                with error_step(codes[9]):
                    field.click(click_count=3)
                with error_step(codes[10]):
                    page.keyboard.press("Backspace")
                with error_step(codes[11]):
                    page.keyboard.type(value, delay=80)
                with error_step(codes[12]):
                    page.wait_for_timeout(300)
                with error_step(codes[13]):
                    actual_length = len(field.input_value())
                if actual_length != len(value):
                    raise with_error_code(CtbcLoginError('登入欄位輸入長度不符；未送出登入'), codes[14])
        except Exception as error:
            raise with_error_code(
                CtbcLoginError('登入欄位無法安全填寫；未送出登入'),
                exception_error_id(error, bank="ctbc") or 'CTBC-02-040-001',
            ) from None

        _submit_login_once(page)
        try:
            _native_step('CTBC-03-100-001', lambda: page.wait_for_timeout(5000))
            for _ in range(20):
                _native_step('CTBC-03-101-001', lambda: page.wait_for_timeout(1000))
                if self._logged_in(page):
                    return
                modals = _native_step('CTBC-03-102-001', lambda: page.locator(".modal.show"))
                if any(_native_step('CTBC-03-103-001', lambda: _native_step('CTBC-03-104-001', lambda: modals.nth(index)).is_visible()) for index in range(_native_step('CTBC-03-105-001', lambda: modals.count()))):
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

        clicked = _native_step('CTBC-07-100-001', lambda: page.evaluate(
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
        ))
        if not clicked:
            print("[ctbc][logout] ⚠️ 沒找到 header 登出按鈕", file=_sys.stderr)
            return False
        print(f"[ctbc][logout] 已點「{clicked}」，等待確認 modal", file=_sys.stderr)

        confirmed = False
        for attempt in range(8):
            _native_step('CTBC-07-101-001', lambda: page.wait_for_timeout(1000))
            confirmed_text = _native_step('CTBC-07-102-001', lambda: page.evaluate(
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
            ))
            if confirmed_text:
                print(f"[ctbc][logout] ✓ 已點登出確認「{confirmed_text}」(attempt={attempt+1})", file=_sys.stderr)
                confirmed = True
                break

        if not confirmed:
            print("[ctbc][logout] ⚠️ 找不到『確定登出？』確認按鈕", file=_sys.stderr)
            return False

        # 等 CTBC 前端把 header 切回「登入」或登入表單，代表 server-side logout 完成。
        for _ in range(12):
            _native_step('CTBC-07-103-001', lambda: page.wait_for_timeout(1000))
            logged_out = _native_step('CTBC-07-104-001', lambda: page.evaluate(
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
            ))
            if logged_out:
                print(f"[ctbc][logout] ✅ 登出完成 -> {_native_step('CTBC-07-105-001', lambda: page.url)}", file=_sys.stderr)
                return True

        print("[ctbc][logout] ⚠️ 已點確認但頁面仍像登入狀態（best-effort）", file=_sys.stderr)
        return True

    @classmethod
    def _validated_twd_inventory(
        cls, collector: ResponseCollector, *, _navigation_ids=(),
    ) -> tuple[dict, set[str]]:
        with error_step("CTBC-04-011-001"):
            hit = next((
                item for item in reversed(collector.hits)
                if isinstance(item.req_body, dict)
                and item.req_body.get("resource") == "/twrbc-deposit/qu001/010"
            ), None)
            parsed = urlparse(hit.url) if hit else None
            response = hit.resp_json if hit else None
            if hit is None:
                raise with_error_code(RuntimeError("ctbc-twd-history-inventory"), _navigation_ids[0] if _navigation_ids else "CTBC-04-010-001")
            if hit.method != "POST":
                raise with_error_code(RuntimeError("ctbc-twd-history-inventory"), "CTBC-04-010-002")
            if type(hit.status) is not int:
                raise with_error_code(RuntimeError("ctbc-twd-history-inventory"), "CTBC-04-010-003")
            if not 200 <= hit.status < 300:
                raise with_error_code(RuntimeError("ctbc-twd-history-inventory"), "CTBC-04-010-004")
            if parsed is None:
                raise with_error_code(RuntimeError("ctbc-twd-history-inventory"), "CTBC-04-010-005")
            if parsed.scheme != "https":
                raise with_error_code(RuntimeError("ctbc-twd-history-inventory"), "CTBC-04-010-016")
            if parsed.hostname != "www.ctbcbank.com":
                raise with_error_code(RuntimeError("ctbc-twd-history-inventory"), "CTBC-04-010-017")
            if parsed.port not in (None, 443):
                raise with_error_code(RuntimeError("ctbc-twd-history-inventory"), "CTBC-04-010-018")
            if parsed.username is not None:
                raise with_error_code(RuntimeError("ctbc-twd-history-inventory"), "CTBC-04-010-019")
            if parsed.password is not None:
                raise with_error_code(RuntimeError("ctbc-twd-history-inventory"), "CTBC-04-010-020")
            if parsed.path != _CTBC_EBMW_PATH:
                raise with_error_code(RuntimeError("ctbc-twd-history-inventory"), "CTBC-04-010-021")
            if not isinstance(response, dict):
                raise with_error_code(RuntimeError("ctbc-twd-history-inventory"), "CTBC-04-010-006")
            if response.get("code") != "0000":
                raise with_error_code(RuntimeError("ctbc-twd-history-inventory"), "CTBC-04-010-007")
            if not isinstance(response.get("rsData"), dict):
                raise with_error_code(RuntimeError("ctbc-twd-history-inventory"), "CTBC-04-010-008")
            deposit = response["rsData"].get("twdAcctSummaryResponse")
            summary = deposit.get("demDepBalSummaryResponse") if isinstance(deposit, dict) else None
            rows = summary.get("infoList") if isinstance(summary, dict) else None
            if not isinstance(deposit, dict):
                raise with_error_code(RuntimeError("ctbc-twd-history-inventory"), "CTBC-04-010-009")
            if not isinstance(summary, dict):
                raise with_error_code(RuntimeError("ctbc-twd-history-inventory"), "CTBC-04-010-022")
            if not isinstance(rows, list):
                raise with_error_code(RuntimeError("ctbc-twd-history-inventory"), "CTBC-04-010-010")
            if any(not isinstance(row, dict) for row in rows):
                raise with_error_code(RuntimeError("ctbc-twd-history-inventory"), "CTBC-04-010-011")
            identities = []
            for row in rows:
                identity = row.get("accountId")
                if not isinstance(identity, str):
                    raise with_error_code(RuntimeError("ctbc-twd-history-inventory"), "CTBC-04-010-012")
                if not identity:
                    raise with_error_code(RuntimeError("ctbc-twd-history-inventory"), "CTBC-04-010-013")
                if identity != identity.strip():
                    raise with_error_code(RuntimeError("ctbc-twd-history-inventory"), "CTBC-04-010-014")
                identities.append(identity)
            if len(identities) != len(set(identities)):
                raise with_error_code(RuntimeError("ctbc-twd-history-inventory"), "CTBC-04-010-015")
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

        _native_step('CTBC-04-050-001', lambda: page.wait_for_timeout(5000))

        # 1) 首頁總覽彙總（登入後自動載入，攔即可）—— ebAcctSummaryInq 含台幣/信用卡/信貸彙總
        home = self._latest_rsdata(collector, "/twrbc-home/qu000/010", "CTBC-04-040-001")
        out["summary"] = (home or {}).get("ebAcctSummaryInq") if isinstance(home, dict) else None

        # 2) 台幣存款帳戶（點臺幣存款 link 觸發，或直接複用攔到的）
        navigation_ids = []
        self._goto_twd_deposit(page, navigation_ids)
        _native_step('CTBC-04-060-001', lambda: page.wait_for_timeout(3000))
        out["twd_deposit"], twd_identities = self._validated_twd_inventory(collector, _navigation_ids=navigation_ids)
        navigation_ids.clear()

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
            _native_step(_CARD_WAIT_STEPS[target_text], lambda: page.wait_for_timeout(6000))
        out["card_nav_probe"] = nav_logs  # 改成多家紀錄
        out["card_nav_probe_2"] = None     # 舊欄位保留向後相容

        # 2026-06-22 (使用者指示「一頁一頁看」, 之前 navProbe 只試 3 個 hard-code menu,
        # 沒探索其他子選單). 加 mega menu 全 dump probe — hover 信用卡/點數 後, 把
        # 出現的所有 sub-menu link text + href dump 出來. 後續用此資料找「繳款紀錄」
        # 類 menu, ship 0.3.32 hard-code 新 target.
        try:
            card_link = _native_step('CTBC-06-103-001', lambda: _native_step('CTBC-06-104-001', lambda: page.locator("a:has-text('信用卡/點數')")).first)
            if _native_step('CTBC-06-105-001', lambda: card_link.count()) > 0:
                _native_step('CTBC-06-106-001', lambda: card_link.hover())
                _native_step('CTBC-06-107-001', lambda: page.wait_for_timeout(1500))
                menu_items = _native_step('CTBC-06-108-001', lambda: page.evaluate("""() => {
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
                }"""))
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
            data = self._latest_rsdata(collector, r, "CTBC-06-040-001")
            if data is not None:
                card_api_dump[r] = data
        if "/twrbc-card/qu006/011" not in card_api_dump and self._qu006_is_empty(collector):
            card_api_dump["/twrbc-card/qu006/011"] = {"allItems": []}
        out["card_api_dump"] = card_api_dump

        # 最終 url + 截圖（信用卡頁）
        out["card_final_url"] = _native_step('CTBC-06-109-001', lambda: page.url)

        out["_final_url"] = _native_step('CTBC-06-110-001', lambda: page.url)
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
        with error_step("CTBC-07-050-001"):
            bill_fact = make_card_bill_fact(
                remaining_due=cc.get("unpaidStmt"),
                payment_due_date=cc.get("pmtExpDt"),
                last_payment_amount=last_amount,
                last_payment_date=last_date,
            )
        with error_step("CTBC-07-060-001"):
            publish_card_bill_facts(out, [bill_fact])

        with error_step("CTBC-07-070-001"):
            return BankCollectResult(**out)

    @staticmethod
    def _latest_rsdata(collector: ResponseCollector, resource: str, _code='CTBC-07-040-001'):
        """取某 resource 最新成功回應的 rsData（CTBC 格式 {sys, code, rsData}）。"""
        with error_step(_code):
            hits = [h for h in collector.hits
                    if "ebmwResource" in h.url and isinstance(h.req_body, dict)
                    and h.req_body.get("resource") == resource
                    and isinstance(h.resp_json, dict) and h.resp_json.get("code") == "0000"]
            if hits:
                return hits[-1].resp_json.get("rsData")
            return None

    @staticmethod
    def _qu006_is_empty(collector: ResponseCollector) -> bool:
        """Live 2026-10-17: no unbilled rows answers code 8888, rsData null, exact desc below."""
        with error_step("CTBC-06-050-001"):
            hits = [h for h in collector.hits
                    if "ebmwResource" in h.url and isinstance(h.req_body, dict)
                    and h.req_body.get("resource") == "/twrbc-card/qu006/011"]
            if not hits or hits[-1].status != 200 or not isinstance(hits[-1].resp_json, dict):
                return False
            resp = hits[-1].resp_json
            return (resp.get("code") == "8888" and resp.get("rsData") is None
                    and resp.get("desc") == "您目前無此資料。")

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
        if mode not in {'full', 'incremental'}:
            raise with_error_code(RuntimeError("ctbc-twd-history-mode"), "CTBC-05-020-001")
        info_list = (twd_deposit or {}).get("demDepBalSummaryResponse", {}).get("infoList")
        if not isinstance(info_list, list):
            raise with_error_code(RuntimeError("ctbc-twd-history-inventory"), "CTBC-05-020-002")
        identities: list[str] = []
        for row in info_list:
            identity = row.get("accountId") if isinstance(row, dict) else None
            if not isinstance(identity, str):
                raise with_error_code(RuntimeError("ctbc-twd-history-inventory"), "CTBC-05-020-003")
            if not identity:
                raise with_error_code(RuntimeError("ctbc-twd-history-inventory"), "CTBC-05-020-004")
            if identity != identity.strip():
                raise with_error_code(RuntimeError("ctbc-twd-history-inventory"), "CTBC-05-020-005")
            identities.append(identity)
        if len(identities) != len(set(identities)):
            raise with_error_code(RuntimeError("ctbc-twd-history-inventory"), "CTBC-05-020-006")
        if set(identities) != expected_identities:
            raise with_error_code(RuntimeError("ctbc-twd-history-inventory"), "CTBC-05-020-007")
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
            _native_step('CTBC-05-200-001', lambda: page.goto(
                "https://www.ctbcbank.com/twrbc/twrbc-deposit/qu002/010",
                wait_until="domcontentloaded", timeout=15000,
            ))
        except Exception as error:
            raise with_error_code(RuntimeError("ctbc-twd-history-template"), exception_error_id(error, bank="ctbc") or "CTBC-05-020-008") from None
        try:
            current = urlparse(_native_step('CTBC-05-201-001', lambda: page.url))
        except Exception as error:
            raise with_error_code(RuntimeError("ctbc-twd-history-template"), exception_error_id(error, bank="ctbc") or "CTBC-05-020-009") from None
        if current.scheme != 'https':
            raise with_error_code(RuntimeError("ctbc-twd-history-template"), "CTBC-05-020-010")
        if current.hostname != 'www.ctbcbank.com':
            raise with_error_code(RuntimeError("ctbc-twd-history-template"), "CTBC-05-020-011")
        if current.port not in (None, 443):
            raise with_error_code(RuntimeError("ctbc-twd-history-template"), "CTBC-05-020-012")
        if current.username is not None:
            raise with_error_code(RuntimeError("ctbc-twd-history-template"), "CTBC-05-020-013")
        if current.password is not None:
            raise with_error_code(RuntimeError("ctbc-twd-history-template"), "CTBC-05-020-014")
        if current.path != '/twrbc/twrbc-deposit/qu002/010':
            raise with_error_code(RuntimeError("ctbc-twd-history-template"), "CTBC-05-020-015")
        if current.params:
            raise with_error_code(RuntimeError("ctbc-twd-history-template"), "CTBC-05-020-016")
        if current.query:
            raise with_error_code(RuntimeError("ctbc-twd-history-template"), "CTBC-05-020-017")
        if current.fragment:
            raise with_error_code(RuntimeError("ctbc-twd-history-template"), "CTBC-05-020-018")
        _native_step('CTBC-05-202-001', lambda: page.wait_for_timeout(5000))
        bearer = collector.auth_token
        use_native_form = not _valid_bearer(bearer)
        if use_native_form:
            # ponytail: one selected account is proven; add dropdown selection
            # only when a real multi-account session establishes its contract.
            if len(identities) != 1:
                raise with_error_code(RuntimeError("ctbc-twd-history-account-selection"), "CTBC-05-020-019")
            template_body = None
            template_url = None
        else:
            rejection = []
            template_hit = self._latest_qu002_011_hit(collector, _rejection=rejection)
            if template_hit is None:
                raise with_error_code(RuntimeError("ctbc-twd-history-template"), rejection[0] if rejection else "CTBC-05-020-020")
            template_body = template_hit.req_body
            template_url = template_hit.url

        accounts = []
        expected = []
        receipts = []
        for account_index, account_id in enumerate(identities, start=1):
            desired_start = self.transaction_window_start(account_id, floor=floor)
            selected = [window for window in capability if window[2] >= desired_start]
            if not selected:
                raise with_error_code(RuntimeError("ctbc-twd-history-range"), "CTBC-05-020-021")
            months_data: dict[str, list[dict]] = {}
            for month, window_start, window_end in selected:
                try:
                    if use_native_form:
                        raw_rows = self._fetch_native_history_window(
                            page, collector, account_id, window_start, window_end,
                        )
                    else:
                        raw_rows = self._fetch_qu002_011(
                            page, template_url, template_body, account_id, month, bearer,
                        )
                except Exception as error:
                    raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), exception_error_id(error, bank="ctbc") or "CTBC-05-020-022") from None
                row_failures = []
                detail_list, skipped = _filter_valid_ctbc_details(
                    raw_rows, start=window_start, end=window_end, failures=row_failures,
                )
                if skipped:
                    raise with_error_code(RuntimeError("ctbc-twd-history-row"), row_failures[0] if row_failures else "CTBC-05-020-023")
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
    def _fetch_native_history_window(
        page,
        collector: ResponseCollector,
        account_id: str,
        start: date,
        end: date,
    ) -> list:
        try:
            submitting_url = _native_step('CTBC-05-300-001', lambda: page.url)
            current = urlparse(submitting_url)
        except Exception as error:
            raise with_error_code(RuntimeError("ctbc-twd-history-form"), exception_error_id(error, bank="ctbc") or "CTBC-05-030-001") from None
        if current.scheme != 'https':
            raise with_error_code(RuntimeError("ctbc-twd-history-form"), "CTBC-05-030-002")
        if current.hostname != 'www.ctbcbank.com':
            raise with_error_code(RuntimeError("ctbc-twd-history-form"), "CTBC-05-030-003")
        if current.port not in (None, 443):
            raise with_error_code(RuntimeError("ctbc-twd-history-form"), "CTBC-05-030-004")
        if current.username is not None:
            raise with_error_code(RuntimeError("ctbc-twd-history-form"), "CTBC-05-030-005")
        if current.password is not None:
            raise with_error_code(RuntimeError("ctbc-twd-history-form"), "CTBC-05-030-006")
        if current.path != '/twrbc/twrbc-deposit/qu002/010':
            raise with_error_code(RuntimeError("ctbc-twd-history-form"), "CTBC-05-030-007")
        if current.params:
            raise with_error_code(RuntimeError("ctbc-twd-history-form"), "CTBC-05-030-008")
        if current.query:
            raise with_error_code(RuntimeError("ctbc-twd-history-form"), "CTBC-05-030-009")
        if current.fragment:
            raise with_error_code(RuntimeError("ctbc-twd-history-form"), "CTBC-05-030-010")

        start_fields = _native_step('CTBC-05-301-001', lambda: page.locator("input[formcontrolname='startDt']"))
        end_fields = _native_step('CTBC-05-302-001', lambda: page.locator("input[formcontrolname='endDt']"))
        if _native_step('CTBC-05-303-001', lambda: start_fields.count()) != 1:
            raise with_error_code(RuntimeError("ctbc-twd-history-form"), "CTBC-05-030-011")
        if _native_step('CTBC-05-304-001', lambda: end_fields.count()) != 1:
            raise with_error_code(RuntimeError("ctbc-twd-history-form"), "CTBC-05-030-012")
        start_field, end_field = _native_step('CTBC-05-305-001', lambda: start_fields.nth(0)), _native_step('CTBC-05-306-001', lambda: end_fields.nth(0))
        if not (_native_step('CTBC-05-307-001', lambda: start_field.is_visible()) and _native_step('CTBC-05-308-001', lambda: end_field.is_visible())):
            navs = _native_step('CTBC-05-309-001', lambda: page.locator("twrbc-deposit-qu002-010 nav-tabs"))
            if _native_step('CTBC-05-310-001', lambda: navs.count()) != 1:
                raise with_error_code(RuntimeError("ctbc-twd-history-form"), "CTBC-05-030-013")
            actions = _native_step('CTBC-05-311-001', lambda: _native_step('CTBC-05-312-001', lambda: navs.nth(0)).locator("a,button,[role='tab']"))
            search = []
            for index in range(_native_step('CTBC-05-313-001', lambda: actions.count())):
                action = _native_step('CTBC-05-314-001', lambda: actions.nth(index))
                if (
                    _native_step('CTBC-05-315-001', lambda: action.is_visible())
                    and _native_step('CTBC-05-316-001', lambda: action.is_enabled())
                    and " ".join(_native_step('CTBC-05-317-001', lambda: action.inner_text()).split()) == "搜尋"
                ):
                    search.append(action)
            if len(search) != 1:
                raise with_error_code(RuntimeError("ctbc-twd-history-form"), "CTBC-05-030-014")
            _native_step('CTBC-05-318-001', lambda: search[0].click(timeout=8000))
            _native_step('CTBC-05-319-001', lambda: page.wait_for_timeout(3000))
        for field, codes in ((start_field, _NATIVE_DATE_STEPS["起日"]),
                             (end_field, _NATIVE_DATE_STEPS["迄日"])):
            if not _native_step(codes[0], lambda: field.is_visible()):
                raise with_error_code(RuntimeError("ctbc-twd-history-form"), codes[1])
            if not _native_step(codes[2], lambda: field.is_enabled()):
                raise with_error_code(RuntimeError("ctbc-twd-history-form"), codes[3])

        for field, value, codes in (
            (start_field, start.strftime("%Y/%m/%d"), _NATIVE_DATE_STEPS["起日"]),
            (end_field, end.strftime("%Y/%m/%d"), _NATIVE_DATE_STEPS["迄日"]),
        ):
            _native_step(codes[4], lambda: field.click())
            _native_step(codes[5], lambda: field.click(click_count=3))
            _native_step(codes[6], lambda: page.keyboard.press("Backspace"))
            _native_step(codes[7], lambda: page.keyboard.type(value, delay=80))
            if _native_step(codes[8], lambda: field.input_value()) != value:
                raise with_error_code(RuntimeError("ctbc-twd-history-form"), codes[9])

        panels = _native_step('CTBC-05-327-001', lambda: page.locator(".tab-pane:has(input[formcontrolname='startDt'])"))
        if _native_step('CTBC-05-328-001', lambda: panels.count()) != 1:
            raise with_error_code(RuntimeError("ctbc-twd-history-form"), "CTBC-05-030-018")
        if not _native_step('CTBC-05-329-001', lambda: _native_step('CTBC-05-330-001', lambda: panels.nth(0)).is_visible()):
            raise with_error_code(RuntimeError("ctbc-twd-history-form"), "CTBC-05-030-019")
        actions = _native_step('CTBC-05-332-001', lambda: _native_step('CTBC-05-331-001', lambda: panels.nth(0)).locator(
            "button, a, input[type='submit'], input[type='button']"
        ))
        query = []
        for index in range(_native_step('CTBC-05-333-001', lambda: actions.count())):
            action = _native_step('CTBC-05-334-001', lambda: actions.nth(index))
            label = (
                _native_step('CTBC-05-335-001', lambda: action.get_attribute("value"))
                if _native_step('CTBC-05-336-001', lambda: action.evaluate("el => el.tagName === 'INPUT'"))
                else _native_step('CTBC-05-337-001', lambda: action.inner_text())
            )
            if (
                _native_step('CTBC-05-338-001', lambda: action.is_visible())
                and _native_step('CTBC-05-339-001', lambda: action.is_enabled())
                and " ".join((label or "").split()) == "搜尋"
            ):
                query.append(action)
        if len(query) != 1:
            raise with_error_code(RuntimeError("ctbc-twd-history-form"), "CTBC-05-030-020")

        before = len(collector.hits)
        baseline_sequence = collector.request_sequence
        _native_step('CTBC-05-340-001', lambda: query[0].click(timeout=8000))
        hits = []
        for _ in range(20):
            _native_step('CTBC-05-341-001', lambda: page.wait_for_timeout(1000))
            hits = [
                hit for hit in collector.hits[before:]
                if isinstance(hit.req_body, dict)
                and hit.req_body.get("resource") == "/twrbc-deposit/qu002/011"
                and hit.request_sequence > baseline_sequence
            ]
            if hits:
                break
        if hits:
            _native_step('CTBC-05-342-001', lambda: page.wait_for_timeout(1000))
            hits = [
                hit for hit in collector.hits[before:]
                if isinstance(hit.req_body, dict)
                and hit.req_body.get("resource") == "/twrbc-deposit/qu002/011"
                and hit.request_sequence > baseline_sequence
            ]
        if len(hits) != 1:
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-021")
        hit = hits[0]
        parsed = urlparse(hit.url)
        parsed_raw = urlparse(hit.raw_url)
        parsed_frame = urlparse(hit.request_frame_url)
        request_data = hit.req_body.get("rqData")
        response = hit.resp_json
        response_data = response.get("rsData") if isinstance(response, dict) else None
        media_type = hit.content_type.split(";", 1)[0].strip().lower()
        main_frame = _OriginGuardProxy._unwrap(_native_step('CTBC-05-343-001', lambda: page.main_frame))
        if hit.method != 'POST':
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-022")
        if hit.status != 200:
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-023")
        if hit.redirected is not False:
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-024")
        if hit.main_frame_request is not True:
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-025")
        if hit.request_frame is not main_frame:
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-026")
        if hit.request_frame_url != submitting_url:
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-027")
        if parsed.scheme != 'https':
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-028")
        if parsed.hostname != 'www.ctbcbank.com':
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-029")
        if parsed.port not in (None, 443):
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-030")
        if parsed.username is not None:
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-031")
        if parsed.password is not None:
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-032")
        if parsed.path != _CTBC_EBMW_PATH:
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-033")
        if parsed.params:
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-034")
        if parsed.query:
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-035")
        if parsed.fragment:
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-036")
        rejections = []
        if not _ctbc_raw_url_matches(parsed, parsed_raw, rejections=rejections):
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), rejections[0] if rejections else "CTBC-05-030-037")
        if parsed_frame.params:
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-038")
        if parsed_frame != current:
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-039")
        if media_type != 'application/json':
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-040")
        if not _ctbc_request_envelope_valid(hit.req_body, '/twrbc-deposit/qu002/011', rejections=rejections):
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), rejections[0] if rejections else "CTBC-05-030-041")
        if not isinstance(request_data, dict):
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-042")
        if set(request_data) != {'accountId', 'startDate', 'endDate', 'type'}:
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-043")
        if request_data.get('accountId') != account_id:
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-044")
        if request_data.get('startDate') != start.strftime('%Y%m%d'):
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-045")
        if request_data.get('endDate') != end.strftime('%Y%m%d'):
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-046")
        if request_data.get('type') != 'custom':
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-047")
        if not isinstance(response, dict):
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-048")
        # Live 2026-10-02: a window with no transactions answers code 9201, rsData null, exact desc below.
        if (
            response.get("code") == "9201"
            and response.get("rsData") is None
            and response.get("desc") == "您查詢的日期區間無明細資料"
            and response.get("backHome") is False
            and response.get("kickSimpleIdentity") is False
        ):
            return []
        if response.get('code') != '0000':
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-049")
        if not isinstance(response_data, dict):
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-050")
        if set(response_data) != {'dataTime', 'detailList', 'dtSort', 'nextKey'}:
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-051")
        if not isinstance(response_data.get('detailList'), list):
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-052")
        if response_data.get('nextKey') != '':
            raise with_error_code(RuntimeError("ctbc-twd-history-fetch"), "CTBC-05-030-053")
        return response_data["detailList"]

    @staticmethod
    def _latest_qu002_011_hit(collector: ResponseCollector, *, _rejection=None):
        """Return the newest exact, successful owned history seed request."""
        with error_step("CTBC-05-070-001"):
            if _rejection is not None:
                _rejection.clear()
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
                    if _rejection is not None:
                        _rejection.clear()
                    return hit
                if (_rejection is not None and not _rejection
                        and parsed.scheme == "https" and parsed.hostname == "www.ctbcbank.com"
                        and parsed.path == _CTBC_EBMW_PATH and isinstance(body, dict)
                        and body.get("resource") == "/twrbc-deposit/qu002/011"):
                    _rejection.append("CTBC-05-071-001" if type(hit.status) is not int or not 200 <= hit.status < 300 else "CTBC-05-071-002")
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
        if parsed.scheme != 'https':
            raise with_error_code(RuntimeError("invalid-request"), "CTBC-05-040-001")
        if parsed.hostname != 'www.ctbcbank.com':
            raise with_error_code(RuntimeError("invalid-request"), "CTBC-05-040-002")
        if parsed.port not in (None, 443):
            raise with_error_code(RuntimeError("invalid-request"), "CTBC-05-040-003")
        if parsed.username is not None:
            raise with_error_code(RuntimeError("invalid-request"), "CTBC-05-040-004")
        if parsed.password is not None:
            raise with_error_code(RuntimeError("invalid-request"), "CTBC-05-040-005")
        if parsed.path != _CTBC_EBMW_PATH:
            raise with_error_code(RuntimeError("invalid-request"), "CTBC-05-040-006")
        if not isinstance(template_body, dict):
            raise with_error_code(RuntimeError("invalid-request"), "CTBC-05-040-007")
        if template_body.get('resource') != '/twrbc-deposit/qu002/011':
            raise with_error_code(RuntimeError("invalid-request"), "CTBC-05-040-008")
        if not isinstance(template_body.get('rqData'), dict):
            raise with_error_code(RuntimeError("invalid-request"), "CTBC-05-040-009")
        if not isinstance(account_id, str):
            raise with_error_code(RuntimeError("invalid-request"), "CTBC-05-040-010")
        if not account_id:
            raise with_error_code(RuntimeError("invalid-request"), "CTBC-05-040-011")
        if account_id != account_id.strip():
            raise with_error_code(RuntimeError("invalid-request"), "CTBC-05-040-012")
        if not _valid_bearer(bearer):
            raise with_error_code(RuntimeError("invalid-request"), "CTBC-05-040-013")
        post_body = _build_qu002_011_post_body(account_id, month_type, template_body)
        script = """async (payload) => {
          if (location.protocol !== 'https:') return {origin_rejection: 'protocol'};
          if (location.hostname !== 'www.ctbcbank.com') return {origin_rejection: 'hostname'};
          if (!['', '443'].includes(location.port)) return {origin_rejection: 'port'};
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
            let jsonParseFailed = false;
            try { json = await response.json(); } catch (_) { jsonParseFailed = true; }
            return {
              status: response.status,
              url: response.url,
              redirected: response.redirected,
              contentType: response.headers.get('content-type') || '',
              json, jsonParseFailed,
            };
          } finally {
            clearTimeout(timeoutId);
          }
        }"""
        result = _native_step('CTBC-05-400-001', lambda: page.evaluate(script, {
            "url": template_url,
            "body": post_body,
            "bearer": bearer,
        }))
        if type(result) is dict and len(result) == 1:
            origin_rejection = dict.get(result, 'origin_rejection')
            if type(origin_rejection) is str:
                origin_code = {
                    'protocol': 'CTBC-05-041-001',
                    'hostname': 'CTBC-05-041-002',
                    'port': 'CTBC-05-041-003',
                }.get(origin_rejection)
                if origin_code is not None:
                    raise with_error_code(RuntimeError('ctbc-origin-mismatch'), origin_code)
        response = result.get("json") if isinstance(result, dict) else None
        raw_content_type = result.get("contentType") if isinstance(result, dict) else None
        media_type = (
            raw_content_type.split(";", 1)[0].strip().lower()
            if isinstance(raw_content_type, str)
            else ""
        )
        if not isinstance(result, dict):
            raise with_error_code(RuntimeError("invalid-response"), "CTBC-05-040-014")
        if type(result.get('status')) is not int:
            raise with_error_code(RuntimeError("invalid-response"), "CTBC-05-040-015")
        if not 200 <= result['status'] < 300:
            raise with_error_code(RuntimeError("invalid-response"), "CTBC-05-040-016")
        if result.get('url') != template_url:
            raise with_error_code(RuntimeError("invalid-response"), "CTBC-05-040-017")
        if result.get('redirected') is not False:
            raise with_error_code(RuntimeError("invalid-response"), "CTBC-05-040-018")
        if not (media_type == 'application/json' or re.fullmatch("application/[!#$%&'*+.^_`|~0-9A-Za-z-]+\\+json", media_type) is not None):
            raise with_error_code(RuntimeError("invalid-response"), "CTBC-05-040-019")
        if not isinstance(response, dict):
            raise with_error_code(RuntimeError("invalid-response"), "CTBC-05-040-034" if result.get("jsonParseFailed") is True else "CTBC-05-040-020")
        if response.get('code') != '0000':
            raise with_error_code(RuntimeError("invalid-response"), "CTBC-05-040-021")
        if not isinstance(response.get('rsData'), dict):
            raise with_error_code(RuntimeError("invalid-response"), "CTBC-05-040-022")
        if not isinstance(response['rsData'].get('detailList'), list):
            raise with_error_code(RuntimeError("invalid-response"), "CTBC-05-040-023")
        response_data = response["rsData"]
        detail_list = response_data["detailList"]
        if response_data.get('accountId') != account_id:
            raise with_error_code(RuntimeError("ownership-mismatch"), "CTBC-05-040-024")
        if response_data.get('type') != month_type:
            raise with_error_code(RuntimeError("ownership-mismatch"), "CTBC-05-040-025")
        for key, code in (('hasNext', 'CTBC-05-824-001'), ('hasMore', 'CTBC-05-825-001'), ('moreData', 'CTBC-05-826-001'), ('nextPage', 'CTBC-05-827-001'), ('nextToken', 'CTBC-05-828-001')):
            if response_data.get(key) not in (None, False, '', 0, '0'):
                raise with_error_code(RuntimeError("pagination-detected"), code)
        page_values = [
            (response_data[key], codes) for key, codes in (('totalPages', ('CTBC-05-820-001', 'CTBC-05-820-002')), ('pageCount', ('CTBC-05-821-001', 'CTBC-05-821-002')))
            if key in response_data
        ]
        no_next_values = [
            (response_data[key], code) for key, code in (('hasNext', 'CTBC-05-829-001'), ('hasMore', 'CTBC-05-833-001'), ('moreData', 'CTBC-05-834-001'))
            if key in response_data
        ]
        if not page_values and (not no_next_values):
            raise with_error_code(RuntimeError("missing-pagination-proof"), "CTBC-05-040-027")
        for value, codes in page_values:
            if type(value) is not int:
                raise with_error_code(RuntimeError("pagination-detected"), codes[0])
            if value != 1:
                raise with_error_code(RuntimeError("pagination-detected"), codes[1])
        for value, code in no_next_values:
            if value is not False:
                raise with_error_code(RuntimeError("pagination-detected"), code)
        count_values = [
            (response_data[key], codes) for key, codes in (('count', ('CTBC-05-822-001', 'CTBC-05-822-002')), ('totalCount', ('CTBC-05-823-001', 'CTBC-05-823-002')))
            if key in response_data
        ]
        if not count_values:
            raise with_error_code(RuntimeError("count-mismatch"), "CTBC-05-040-031")
        for value, codes in count_values:
            if type(value) is not int:
                raise with_error_code(RuntimeError("count-mismatch"), codes[0])
            if value != len(detail_list):
                raise with_error_code(RuntimeError("count-mismatch"), codes[1])
        return detail_list

    def _goto_twd_deposit(self, page, _failures=None):
        """點首頁「臺幣存款」A.link 進存款頁（觸發 /twrbc-deposit/qu001/010）。"""
        if _failures is not None:
            _failures.clear()
        try:
            _native_step('CTBC-04-100-001', lambda: page.evaluate(
                "(() => { const a=[...document.querySelectorAll('a.link')]"
                ".find(x=>x.offsetParent!==null && (x.textContent||'').trim()==='臺幣存款');"
                " if(a){ a.click(); return true;}"
                " const a2=[...document.querySelectorAll('a')].find(x=>x.offsetParent!==null"
                "  && (x.textContent||'').trim()==='臺幣存款' && !/轉帳/.test(x.textContent||'')); if(a2) a2.click(); })()",
            ))

        except Exception as error:
            if _failures is not None:
                error_id = exception_error_id(error, bank="ctbc")
                if error_id is not None:
                    _failures[:] = [error_id]

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
        codes = _CARD_NAV_STEPS.get(target_text, ('CTBC-06-200-001', 'CTBC-06-201-001', 'CTBC-06-202-001', 'CTBC-06-203-001', 'CTBC-06-204-001', 'CTBC-06-205-001', 'CTBC-06-206-001', 'CTBC-06-207-001', 'CTBC-06-208-001', 'CTBC-06-209-001', 'CTBC-06-210-001', 'CTBC-06-211-001', 'CTBC-06-212-001', 'CTBC-06-213-001'))
        log: list[str] = []
        try:
            # Step 1: 找「信用卡/點數」<a>
            card_link = _native_step(codes[0], lambda: _native_step(codes[1], lambda: page.locator("a:has-text('信用卡/點數')")).first)
            if _native_step(codes[2], lambda: card_link.count()) == 0:
                log.append("no top-nav 信用卡/點數")
                return {"clicked": None, "log": log}

            # Step 2: hover 觸發 mega menu
            _native_step(codes[3], lambda: card_link.hover())
            _native_step(codes[4], lambda: page.wait_for_timeout(1200))
            log.append("hovered top-nav, mega menu should be visible")

            # Step 3: 找 target_text 子選單（exact match，避免「帳單明細查詢」誤撞「未出帳單明細」）
            target = _native_step(codes[5], lambda: page.evaluate("""(wanted) => {
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
            }""", target_text))

            if not target:
                log.append(f"no sub-menu match {target_text!r}")
                return {"clicked": None, "log": log}

            log.append(f"will click: {target!r}")
            tlocator = _native_step(codes[6], lambda: _native_step(codes[7], lambda: page.locator(f"a:has-text('{target_text}')")).first)
            if _native_step(codes[8], lambda: tlocator.count()) == 0:
                _native_step(codes[9], lambda: page.mouse.click(target["x"] + target["w"] // 2, target["y"] + target["h"] // 2))
                log.append("clicked by coords")
            else:
                _native_step(codes[10], lambda: tlocator.click())
                log.append("clicked by locator")

            _native_step(codes[11], lambda: page.wait_for_timeout(2500))
            log.append(f"after_click_url={_native_step(codes[12], lambda: page.url)}")
            return {"clicked": target["text"], "after_click_url": _native_step(codes[13], lambda: page.url), "log": log}
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
