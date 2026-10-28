#!/usr/bin/env python3
"""Taishin Bank personal e-banking crawler (my.taishinbank.com.tw).

台新銀行 my.taishinbank.com.tw 個人網銀抓取器。

登入入口：https://my.taishinbank.com.tw/TIBNetBank/（RWD SPA in iframe）
流程：開頁 → 找 iframe `main` (src 含 `svc/rwd/index.html`) → 4 欄全 type=password
      → 用 placeholder 精準匹配「身分證字號/使用者代號/使用者密碼/驗證碼」→ OCR captcha → 點 #loginBtn

⚠️ 鐵律（見 wiki/concepts/taiwan-bank-login-retry-account-lockout-lesson.md）：
   login 失敗**絕不自動重打**——max_attempts=1。
   OCR 階段（送出前）可換圖重試最多 5 次（安全）。
"""
from __future__ import annotations

import base64
from calendar import monthrange
from datetime import date, datetime, timedelta
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.base import (
    captcha_unreadable,
    BankCollectResult,
    BankCrawler,
    ResponseCollector,
    _OriginGuardProxy,
    write_private_json,
)
from backend.core.card_bills import (
    card_bill_date,
    card_bill_money,
    make_card_bill_fact,
    publish_card_bill_facts,
)
from backend.core.error_codes import error_step, with_error_code, exception_error_id, format_error_id
from backend.core.creds import TaishinCreds
from backend.core.captcha import ocr_bytes
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointPhase,
    LoginCheckpointRule,
)

BASE = "https://my.taishinbank.com.tw/TIBNetBank/"
IFRAME_HINT = "svc/rwd/index.html"
LOGIN_BTN_ID = "loginBtn"

# 4 欄全 type=password，靠 placeholder 精準匹配
FIELD_PLACEHOLDERS = {
    "national_id": "身分證字號",
    "user_code":   "使用者代號",
    "password":    "使用者密碼",
    "captcha":     "驗證碼",
}


def _log(*a):
    print(*a, file=sys.stderr)


def _ph_sel(ph: str) -> str:
    return f"input[placeholder='{ph}']"


class TaishinLoginError(RuntimeError):
    pass


def _taishin_card_bill_fact(parsed: dict):
    if not isinstance(parsed, dict) or parsed.get("fetch_ok") is not True:
        return None
    summary = parsed.get("summary")
    period = parsed.get("billing_period")
    payment_candidates = []
    for row in parsed.get("billed_txns") or []:
        if not isinstance(row, dict) or isinstance(row.get("amount"), bool):
            continue
        raw_amount = row.get("amount")
        if raw_amount is None:
            continue
        try:
            amount = float(raw_amount)
        except (TypeError, ValueError) as _action_error:
            _log(format_error_id(exception_error_id(_action_error, bank="taishin") or 'TAISHIN-06-015-001', bank="taishin"))
            continue
        with error_step('TAISHIN-07-002-001'):
            payment_date = card_bill_date(row.get("post_date") or row.get("txn_date"))
        with error_step('TAISHIN-07-003-001'):
            payment_amount = card_bill_money(abs(amount))
        if amount < 0 and payment_date and payment_amount is not None:
            payment_candidates.append((payment_date, payment_amount))
    payment_date, payment_amount = max(payment_candidates, default=(None, None))
    with error_step('TAISHIN-07-004-001'):
        return make_card_bill_fact(
            remaining_due=summary.get("remaining") if isinstance(summary, dict) else None,
            statement_close_date=period.get("statement_date") if isinstance(period, dict) else None,
            payment_due_date=period.get("pay_due_date") if isinstance(period, dict) else None,
            last_payment_amount=payment_amount,
            last_payment_date=payment_date,
        )


class TaishinCrawler(BankCrawler):
    USES_SHARED_LOGIN_CHECKPOINTS: ClassVar[bool] = True
    SAFE_COLLECT_GUARDS = frozenset({
        "taishin-api-projection",
        "taishin-twd-history-binding",
        "taishin-twd-history-cursor",
        "taishin-twd-history-dialog",
        "taishin-twd-history-dom",
        "taishin-twd-history-empty-inventory",
        "taishin-twd-history-form",
        "taishin-twd-history-frame",
        "taishin-twd-history-inventory",
        "taishin-twd-history-pagination",
        "taishin-twd-history-query",
        "taishin-twd-history-response",
        "taishin-twd-history-response-budget",
        "taishin-twd-history-selection",
        "taishin-twd-history-settle",
        "taishin-twd-history-stale-before-query",
    })
    HISTORY_COVERAGE_REQUIRED: ClassVar[bool] = True
    HISTORY_COVERAGE_DOMAINS: ClassVar[frozenset[str]] = frozenset({"twd_transactions"})
    FETCH_TIMEZONE_ID: ClassVar[str | None] = "Asia/Taipei"
    CREDENTIAL_HOSTS = frozenset({"my.taishinbank.com.tw"})

    def __init__(self):
        super().__init__(name="taishin")
        self.creds = TaishinCreds.load()

    def _host_filter(self) -> str:
        return "taishinbank.com.tw"

    @staticmethod
    def _subtract_months(value: date, months: int) -> date:
        month_index = value.year * 12 + value.month - 1 - months
        year, month_zero = divmod(month_index, 12)
        month = month_zero + 1
        return date(year, month, min(value.day, monthrange(year, month)[1]))

    def _history_window(self, identity: str, as_of: date) -> dict:
        mode = os.environ.get("BANK_CRAWLER_HISTORY_MODE", "full")
        if mode not in {'full', 'incremental'}:
            raise with_error_code(ValueError(f'invalid BANK_CRAWLER_HISTORY_MODE: {mode!r}'), 'TAISHIN-05-048-001')
        cursor = self.transaction_start_for(identity, domain="twd_transactions")
        if mode == "full":
            return {
                "period": "12_months",
                "start": self._subtract_months(as_of, 12),
                "end": as_of,
            }
        if cursor is not None and cursor > as_of:
            raise with_error_code(RuntimeError('taishin-twd-history-cursor'), 'TAISHIN-05-049-001')
        if cursor is not None:
            target = max(self._subtract_months(as_of, 12), cursor - timedelta(days=7))
            periods = (
                ("7_days", as_of - timedelta(days=7)),
                ("14_days", as_of - timedelta(days=14)),
                ("1_months", self._subtract_months(as_of, 1)),
                ("2_months", self._subtract_months(as_of, 2)),
                ("3_months", self._subtract_months(as_of, 3)),
                ("6_months", self._subtract_months(as_of, 6)),
                ("12_months", self._subtract_months(as_of, 12)),
            )
            period, start = next(item for item in periods if item[1] <= target)
            return {"period": period, "start": start, "end": as_of}
        return {
            "period": "12_months",
            "start": self._subtract_months(as_of, 12),
            "end": as_of,
        }

    @staticmethod
    def _validate_history_form(snapshot: dict) -> dict:
        error = "taishin-twd-history-form"
        if not isinstance(snapshot, dict):
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-042-001')
        elif snapshot.get('query_buttons') != 1:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-042-002')
        elif type(snapshot.get('query_button')) is not int:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-042-003')
        elif snapshot['query_button'] < 0:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-042-004')
        selects = snapshot.get("selects")
        if not isinstance(selects, list):
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-043-001')
        expected_periods = [
            ("-- 請選擇查詢期間 --", ""),
            ("7天", "7_days"), ("14天", "14_days"),
            ("1個月", "1_months"), ("2個月", "2_months"),
            ("3個月", "3_months"), ("6個月", "6_months"),
            ("12個月", "12_months"), ("自訂一年內期間", "inYear"),
            ("申請查詢逾一年以上", "overYear"),
        ]
        expected_sort = [("由新到舊", "forward"), ("由舊到新", "reverse")]
        period_selects, sort_selects, account_selects = [], [], []
        last_index = -1
        for select in selects:
            if not isinstance(select, dict):
                raise with_error_code(RuntimeError(error), 'TAISHIN-05-044-001')
            elif type(select.get('index')) is not int:
                raise with_error_code(RuntimeError(error), 'TAISHIN-05-044-002')
            elif select['index'] <= last_index:
                raise with_error_code(RuntimeError(error), 'TAISHIN-05-044-003')
            last_index = select["index"]
            options = select.get("options")
            if not isinstance(options, list):
                raise with_error_code(RuntimeError(error), 'TAISHIN-05-045-001')
            for (index, option) in enumerate(options):
                if not isinstance(option, dict):
                    raise with_error_code(RuntimeError(error), 'TAISHIN-05-045-003')
                if type(option.get('index')) is not int:
                    raise with_error_code(RuntimeError(error), 'TAISHIN-05-045-004')
                if option['index'] != index:
                    raise with_error_code(RuntimeError(error), 'TAISHIN-05-045-005')
                if not isinstance(option.get('text'), str):
                    raise with_error_code(RuntimeError(error), 'TAISHIN-05-045-006')
                if not isinstance(option.get('value'), str):
                    raise with_error_code(RuntimeError(error), 'TAISHIN-05-045-007')
            matched = False
            for expected, matches in ((expected_periods, period_selects), (expected_sort, sort_selects)):
                if len(options) == len(expected) and all(
                    option["text"] == text
                    and option["value"] in ((value, "[object Object]") if value else ("",))
                    for option, (text, value) in zip(options, expected)
                ):
                    matches.append((select["index"], [
                        {**option, "identity": value}
                        for option, (_, value) in zip(options, expected)
                    ]))
                    matched = True
            if matched:
                continue
            if len(options) == 1:
                first = options[0]
                if (
                    first.get("value") == ""
                    and re.sub(r"\s+", " ", str(first.get("text") or "")).strip()
                    == "-- 請選擇查詢帳號 --"
                ):
                    account_selects.append((select["index"], []))
                continue
            if len(options) < 2:
                continue
            first = options[0]
            accounts, identities = [], set()
            for option in options[1:]:
                text, value = option.get("text"), option.get("value")
                digits = re.findall(r"(?<!\d)\d(?:[\d-]*\d)?(?!\d)", text or "")
                matching = [re.sub(r"\D", "", token) for token in digits
                            if re.fullmatch(r"\d{12,14}", re.sub(r"\D", "", token))]
                identity = matching[0] if len(matching) == 1 else ""
                if (
                    not identity
                    or value not in (identity, "[object Object]")
                    or identity in identities
                ):
                    accounts = []
                    break
                identities.add(identity)
                accounts.append({**option, "identity": identity})
            if (
                accounts
                and first.get("value") == ""
                and "請選擇" in str(first.get("text") or "")
            ):
                account_selects.append((select["index"], accounts))
        if len(period_selects) != 1:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-046-001')
        elif len(sort_selects) != 1:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-046-002')
        elif len(account_selects) != 1:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-046-003')
        account_select, accounts = account_selects[0]
        return {
            "account_select": account_select,
            "period_select": period_selects[0][0],
            "sort_select": sort_selects[0][0],
            "query_button": snapshot["query_button"],
            "accounts": accounts,
            "periods": period_selects[0][1],
            "sorts": sort_selects[0][1],
        }

    @classmethod
    def _require_history_inventory(
        cls,
        snapshot: dict,
        expected: dict,
    ) -> dict:
        form = cls._validate_history_form(snapshot)
        if form != expected:
            raise with_error_code(RuntimeError('taishin-twd-history-inventory'), 'TAISHIN-05-047-001')
        return form

    @staticmethod
    def _history_selected_options(snapshot: dict, form: dict) -> dict:
        selected = {}
        for name, options, key in (
            ("account", "accounts", "selected_identity"),
            ("period", "periods", "selected_period"),
            ("sort", "sorts", "selected_sort"),
        ):
            control = next(item for item in snapshot["selects"] if item["index"] == form[f"{name}_select"])
            index = control.get("selected_index")
            selected[key] = next((
                option["identity"] for option in form[options]
                if type(index) is int and option["index"] == index
            ), "")
        return selected

    @classmethod
    def _select_history_options(cls, frame, form: dict, *, identity: str, period: str) -> None:
        cls._require_history_inventory(cls._history_form_snapshot(frame), form)
        for name, options, value in (
            ("account", "accounts", identity), ("period", "periods", period),
            ("sort", "sorts", "forward"),
        ):
            option = next((item for item in form[options] if item["identity"] == value), None)
            if option is None:
                raise with_error_code(RuntimeError('taishin-twd-history-selection'), 'TAISHIN-05-050-001')
            # Native object-valued options share .value; only the validated index is unique.
            with error_step('TAISHIN-05-051-001'):
                error_step({'account': 'TAISHIN-05-700-002', 'period': 'TAISHIN-05-700-003', 'sort': 'TAISHIN-05-700-004'}.get(name, 'TAISHIN-05-700-001'))(lambda: error_step({'account': 'TAISHIN-05-701-002', 'period': 'TAISHIN-05-701-003', 'sort': 'TAISHIN-05-701-004'}.get(name, 'TAISHIN-05-701-001'))(lambda: error_step({'account': 'TAISHIN-05-702-002', 'period': 'TAISHIN-05-702-003', 'sort': 'TAISHIN-05-702-004'}.get(name, 'TAISHIN-05-702-001'))(lambda: frame.locator("select"))().nth(form[f"{name}_select"]))().select_option(index=option["index"]))()
        snapshot = cls._history_form_snapshot(frame)
        cls._require_history_inventory(snapshot, form)
        if cls._history_selected_options(snapshot, form) != {'selected_identity': identity, 'selected_period': period, 'selected_sort': 'forward'}:
            raise with_error_code(RuntimeError('taishin-twd-history-selection'), 'TAISHIN-05-052-001')

    def _require_history_network_quiescence(
        self,
        page,
        collector: ResponseCollector,
        *,
        submitted_frame,
        boundary: int,
        request_sequence: int,
        issued_count: int,
    ):
        matching = []
        for _ in range(10):
            with error_step('TAISHIN-05-053-001'):
                page.wait_for_timeout(500)
            if self._history_frame(page) is not submitted_frame:
                raise with_error_code(RuntimeError('taishin-twd-history-frame'), 'TAISHIN-05-054-001')
            matching = [
                hit for hit in collector.hits
                if hit.request_sequence > boundary
                and urlparse(hit.raw_url or hit.url).path
                == "/TIBNetBank/svc/web1/rb0102/query"
            ]
            if collector.request_sequence != request_sequence:
                raise with_error_code(RuntimeError('taishin-twd-history-response'), 'TAISHIN-05-055-001')
            elif collector.issued_count('query') != issued_count:
                raise with_error_code(RuntimeError('taishin-twd-history-response'), 'TAISHIN-05-055-002')
            elif len(matching) != 1:
                raise with_error_code(RuntimeError('taishin-twd-history-response'), 'TAISHIN-05-055-003')
        return matching[0]

    @staticmethod
    def _history_money(value: object, *, allow_dash: bool = False) -> int | None:
        if allow_dash and value == "-":
            return None
        if not isinstance(value, str):
            raise with_error_code(RuntimeError('taishin-twd-history-dom'), 'TAISHIN-05-075-001')
        elif not re.fullmatch('-?(?:0|[1-9]\\d{0,9}|[1-9]\\d{0,2}(?:,\\d{3}){1,3})', value):
            raise with_error_code(RuntimeError('taishin-twd-history-dom'), 'TAISHIN-05-075-002')
        parsed = int(value.replace(",", ""))
        if abs(parsed) > 2147483647:
            raise with_error_code(RuntimeError('taishin-twd-history-dom'), 'TAISHIN-05-076-001')
        return parsed

    @staticmethod
    def _add_history_response_bytes(total: int, body_size: int) -> int:
        if type(total) is not int:
            raise with_error_code(RuntimeError('taishin-twd-history-response-budget'), 'TAISHIN-05-077-001')
        elif type(body_size) is not int:
            raise with_error_code(RuntimeError('taishin-twd-history-response-budget'), 'TAISHIN-05-077-002')
        elif total < 0:
            raise with_error_code(RuntimeError('taishin-twd-history-response-budget'), 'TAISHIN-05-077-003')
        elif body_size <= 0:
            raise with_error_code(RuntimeError('taishin-twd-history-response-budget'), 'TAISHIN-05-077-004')
        elif total + body_size > 5000000:
            raise with_error_code(RuntimeError('taishin-twd-history-response-budget'), 'TAISHIN-05-077-005')
        return total + body_size

    @staticmethod
    def _history_rows_digest(rows: list[dict]) -> str:
        try:
            serialized_rows = sorted(
                json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                for row in rows
            )
            encoded = ("[" + ",".join(serialized_rows) + "]").encode("utf-8")
        except (TypeError, ValueError):
            raise with_error_code(RuntimeError('taishin-twd-history-binding'), 'TAISHIN-05-078-001') from None
        if len(encoded) > 5000000:
            raise with_error_code(RuntimeError('taishin-twd-history-response-budget'), 'TAISHIN-05-079-001')
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _non_sensitive_api_responses(hits: list) -> dict:
        responses = {}
        routes = {
            "/TIBNetBank/svc/web1/rb0100/query": "query",
            "/TIBNetBank/svc/web/common/qryTaishinPoint": "qryTaishinPoint",
            "/TIBNetBank/svc/web4/rb0708rwd/qryRealTime": "qryRealTime",
            "/TIBNetBank/svc/web4/rb0708rwd/doXTPA": "doXTPA",
        }

        def money(value: object) -> int:
            if isinstance(value, bool):
                raise with_error_code(RuntimeError('taishin-api-projection'), 'TAISHIN-07-015-001')
            elif not isinstance(value, (str, int)):
                raise with_error_code(RuntimeError('taishin-api-projection'), 'TAISHIN-07-015-002')
            raw = str(value).strip()
            if re.fullmatch('-?(?:\\d{1,12}|[1-9]\\d{0,2}(?:,\\d{3}){1,3})', raw) is None:
                raise with_error_code(RuntimeError('taishin-api-projection'), 'TAISHIN-07-016-001')
            parsed = int(raw.replace(",", ""))
            if abs(parsed) > 2147483647:
                raise with_error_code(RuntimeError('taishin-api-projection'), 'TAISHIN-07-017-001')
            return parsed

        def text(value: object, limit: int, *, empty: bool = False) -> str:
            if not isinstance(value, str):
                raise with_error_code(RuntimeError('taishin-api-projection'), 'TAISHIN-07-018-001')
            elif len(value) > limit:
                raise with_error_code(RuntimeError('taishin-api-projection'), 'TAISHIN-07-018-002')
            elif not empty and (not value):
                raise with_error_code(RuntimeError('taishin-api-projection'), 'TAISHIN-07-018-003')
            for char in value:
                if ord(char) < 32 and char not in '\t\n\r':
                    raise with_error_code(RuntimeError('taishin-api-projection'), 'TAISHIN-07-019-001')
            return value

        for hit in hits:
            parsed = urlparse(hit.raw_url or hit.url)
            endpoint = routes.get(parsed.path)
            mime = (hit.content_type or "").split(";", 1)[0].strip().lower()
            if (
                endpoint is None
                or parsed.scheme != "https"
                or parsed.hostname != "my.taishinbank.com.tw"
                or parsed.port not in (None, 443)
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or hit.url != f"https://my.taishinbank.com.tw{parsed.path}"
                or hit.method != "POST"
                or hit.status != 200
                or hit.redirected
                or mime != "application/json"
                or type(hit.body_size) is not int
                or not 0 < hit.body_size <= 5_000_000
                or not isinstance(hit.resp_json, dict)
            ):
                continue
            try:
                if endpoint == "query":
                    output = hit.resp_json.get("OUTPUTDATA")
                    savings = output.get("SavingAccount") if isinstance(output, dict) else None
                    if hit.resp_json.get("RESULT") != "NORMAL" or not isinstance(savings, list) or len(savings) > 100:
                        continue
                    projected_accounts = []
                    identities = set()
                    for account in savings:
                        if not isinstance(account, dict):
                            raise with_error_code(RuntimeError('taishin-api-projection'), 'TAISHIN-07-005-001')
                        account_no = text(account.get("accountNo"), 14)
                        if not re.fullmatch('\\d{12,14}', account_no):
                            raise with_error_code(RuntimeError('taishin-api-projection'), 'TAISHIN-07-006-001')
                        elif account_no in identities:
                            raise with_error_code(RuntimeError('taishin-api-projection'), 'TAISHIN-07-006-002')
                        identities.add(account_no)
                        projected_accounts.append({
                            "accountNo": account_no,
                            "balance": money(account.get("balance")),
                            "accountTypeName": text(account.get("accountTypeName"), 100),
                            "userdefineName": text(account.get("userdefineName") or "", 200, empty=True),
                        })
                    projected = {
                        "RESULT": "NORMAL",
                        "OUTPUTDATA": {"SavingAccount": projected_accounts},
                    }
                elif endpoint == "qryTaishinPoint":
                    value = hit.resp_json.get("value")
                    if not isinstance(value, dict):
                        continue
                    projected_value = {"balance": money(value.get("balance"))}
                    if value.get("TSPOINT_balance") is not None:
                        projected_value["TSPOINT_balance"] = money(value["TSPOINT_balance"])
                    projected = {"value": projected_value}
                elif endpoint == "qryRealTime":
                    value = hit.resp_json.get("value")
                    if not isinstance(value, dict):
                        continue
                    projected = {"value": {"crlimit": money(value.get("crlimit"))}}
                else:
                    value = hit.resp_json.get("value")
                    card = value.get("001") if isinstance(value, dict) else None
                    if not isinstance(card, dict):
                        continue
                    projected = {"value": {"001": {
                        name: money(card.get(name))
                        for name in ("OUT-CRLIMIT-PERM", "OUT-AVAIL-CREDIT")
                    }}}
            except RuntimeError as _action_error:
                _log(format_error_id(exception_error_id(_action_error, bank="taishin") or 'TAISHIN-07-007-001', bank="taishin"))
                continue
            responses.setdefault(endpoint, projected)
        return responses

    @classmethod
    def _normalize_history_cells(
        cls,
        row: list[str],
        *,
        identity: str,
        start: date,
        end: date,
    ) -> dict:
        error = "taishin-twd-history-dom"
        limits = (100, 100, 500, 100, 100, 1000, 100)
        if len(row) != 7:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-072-001')
        for value in row:
            if not isinstance(value, str):
                raise with_error_code(RuntimeError(error), 'TAISHIN-05-072-002')
        for (value, limit) in zip(row, limits, strict=True):
            if len(value) > limit:
                raise with_error_code(RuntimeError(error), 'TAISHIN-05-072-003')
        for value in row:
            for char in value:
                if ord(char) < 32 and char not in '\t\n\r':
                    raise with_error_code(RuntimeError(error), 'TAISHIN-05-072-004')
        row = [re.sub(r"\s+", " ", value).strip() for value in row]
        try:
            txn_at = datetime.strptime(row[0], "%Y/%m/%d %H:%M:%S")
            account_day = datetime.strptime(row[1], "%Y/%m/%d").date()
        except ValueError:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-073-001') from None
        description, memo = row[2], row[5]
        if not description:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-074-001')
        amount = cls._history_money(row[3], allow_dash=True)
        balance = cls._history_money(row[4], allow_dash=True)
        return {
            "account_no": identity,
            "datetime": txn_at.strftime("%Y-%m-%d %H:%M:%S"),
            "account_date": account_day.isoformat(),
            "desc": description,
            "expend": abs(amount) if amount is not None and amount < 0 else None,
            "income": amount if amount is not None and amount >= 0 else None,
            "balance": balance,
            "counterparty_bank": None,
            "counterparty_acct": memo[:30] if memo else None,
            "memo": memo or None,
        }

    @staticmethod
    def _validate_history_transport(
        transport: dict,
        *,
        identity: str,
        start: date,
        end: date,
    ) -> dict:
        error = "taishin-twd-history-response"
        keys = {
            "url", "method", "status", "content_type", "redirected",
            "main_frame_request", "request_frame_url", "request_body",
            "response_result", "body_size", "request_sequence",
        }
        if not isinstance(transport, dict):
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-056-001')
        elif set(transport) != keys:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-056-002')
        elif not isinstance(transport.get('url'), str):
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-056-003')
        elif not isinstance(transport.get('request_frame_url'), str):
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-056-004')
        try:
            parsed = urlparse(transport["url"])
            frame_url = urlparse(transport["request_frame_url"])
        except ValueError:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-057-001') from None
        if parsed.scheme != 'https':
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-058-001')
        elif parsed.hostname != 'my.taishinbank.com.tw':
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-058-002')
        elif parsed.port not in (None, 443):
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-058-003')
        elif parsed.username is not None:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-058-004')
        elif parsed.password is not None:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-058-005')
        elif parsed.path != '/TIBNetBank/svc/web1/rb0102/query':
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-058-006')
        elif parsed.query:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-058-007')
        elif parsed.fragment:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-058-008')
        elif transport.get('method') != 'POST':
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-058-009')
        elif transport.get('status') != 200:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-058-010')
        elif transport.get('content_type') != 'application/json':
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-058-011')
        elif transport.get('redirected') is not False:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-058-012')
        elif transport.get('main_frame_request') is not False:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-058-013')
        elif frame_url.scheme != 'https':
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-058-014')
        elif frame_url.hostname != 'my.taishinbank.com.tw':
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-058-015')
        elif frame_url.port not in (None, 443):
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-058-016')
        elif frame_url.username is not None:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-058-017')
        elif frame_url.password is not None:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-058-018')
        elif frame_url.path not in {'/TIBNetBank/svc/rwd/', '/TIBNetBank/svc/rwd/index.html'}:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-058-019')
        elif frame_url.query:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-058-020')
        elif re.fullmatch('/RB0102/0100(?:\\?ts=\\d+)?', frame_url.fragment) is None:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-058-021')
        elif transport.get('request_body') != {'account': identity, 'start': start.strftime('%Y%m%d'), 'end': end.strftime('%Y%m%d')}:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-058-022')
        elif transport.get('response_result') != 'NORMAL':
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-058-023')
        elif type(transport.get('body_size')) is not int:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-058-024')
        elif not 0 < transport['body_size'] <= 5000000:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-058-025')
        elif type(transport.get('request_sequence')) is not int:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-058-026')
        elif transport['request_sequence'] <= 0:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-058-027')
        return transport

    @classmethod
    def _validate_history_hit(
        cls,
        hit,
        *,
        identity: str,
        start: date,
        end: date,
        boundary: int,
        expected_frame=None,
    ) -> dict:
        error = "taishin-twd-history-response"
        response = hit.resp_json
        output = response.get("OUTPUTDATA") if isinstance(response, dict) else None
        rows = output.get("userList") if isinstance(output, dict) else None
        required = {
            "sysdate", "dateNew", "memo", "txnamt",
            "txnamtOut", "txnamtIn", "newbal", "message",
        }
        transport = {
            "url": hit.raw_url or hit.url,
            "method": hit.method,
            "status": hit.status,
            "content_type": (hit.content_type or "").split(";", 1)[0].strip().lower(),
            "redirected": hit.redirected,
            "main_frame_request": hit.main_frame_request,
            "request_frame_url": hit.request_frame_url,
            "request_body": hit.req_body,
            "response_result": response.get("RESULT") if isinstance(response, dict) else None,
            "body_size": hit.body_size,
            "request_sequence": hit.request_sequence,
        }
        try:
            cls._validate_history_transport(
                transport, identity=identity, start=start, end=end,
            )
        except RuntimeError as source_error:
            raise with_error_code(RuntimeError(error), exception_error_id(source_error, bank="taishin") or 'TAISHIN-05-059-001') from None
        expected_frame = _OriginGuardProxy._unwrap(expected_frame)
        if hit.url != transport['url']:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-060-001')
        if hit.request_sequence <= boundary:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-060-002')
        if expected_frame is not None and hit.request_frame is not expected_frame:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-060-003')
        if not isinstance(response, dict):
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-060-004')
        if response.get('RESULT') != 'NORMAL':
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-060-005')
        if not isinstance(output, dict):
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-060-006')
        if not isinstance(rows, list):
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-060-007')
        if len(rows) > 10000:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-060-008')
        for row in rows:
            if not isinstance(row, dict):
                raise with_error_code(RuntimeError(error), 'TAISHIN-05-060-010')
            if not required <= set(row):
                raise with_error_code(RuntimeError(error), 'TAISHIN-05-060-011')
        normalized = []
        for row in rows:
            for key in ('sysdate', 'dateNew', 'memo', 'txnamtOut', 'txnamtIn'):
                if not isinstance(row[key], str):
                    raise with_error_code(RuntimeError(error), {'sysdate': 'TAISHIN-05-061-007', 'dateNew': 'TAISHIN-05-061-008', 'memo': 'TAISHIN-05-061-009', 'txnamtOut': 'TAISHIN-05-061-010', 'txnamtIn': 'TAISHIN-05-061-011'}[key])
            if not isinstance(row['message'], (str, type(None))):
                raise with_error_code(RuntimeError(error), 'TAISHIN-05-061-002')
            if isinstance(row['txnamt'], bool):
                raise with_error_code(RuntimeError(error), 'TAISHIN-05-061-003')
            if not isinstance(row['txnamt'], (str, int)):
                raise with_error_code(RuntimeError(error), 'TAISHIN-05-061-004')
            if isinstance(row['newbal'], bool):
                raise with_error_code(RuntimeError(error), 'TAISHIN-05-061-005')
            if not isinstance(row['newbal'], (str, int, type(None))):
                raise with_error_code(RuntimeError(error), 'TAISHIN-05-061-006')
            stamp = re.fullmatch(r"(\d{8})\s?(\d{1,8})", row["sysdate"])
            account_day = re.fullmatch(r"\d{8}", row["dateNew"])
            description = row["memo"]
            note = row["message"] or ""
            incoming = row["txnamtIn"] != "-"
            outgoing = row["txnamtOut"] != "-"
            if stamp is None:
                raise with_error_code(RuntimeError(error), 'TAISHIN-05-062-001')
            elif account_day is None:
                raise with_error_code(RuntimeError(error), 'TAISHIN-05-062-002')
            elif incoming and outgoing:
                raise with_error_code(RuntimeError(error), 'TAISHIN-05-062-003')
            time_digits = stamp[2].zfill(8)
            try:
                amount = None
                if incoming or outgoing:
                    amount = cls._history_money(str(row["txnamt"]))
                    direction_amount = cls._history_money(
                        row["txnamtIn"] if incoming else row["txnamtOut"],
                    )
                    if direction_amount is None:
                        raise with_error_code(RuntimeError(error), 'TAISHIN-05-063-001')
                    elif direction_amount <= 0:
                        raise with_error_code(RuntimeError(error), 'TAISHIN-05-063-002')
                    elif amount is None:
                        raise with_error_code(RuntimeError(error), 'TAISHIN-05-063-003')
                    elif direction_amount != abs(amount):
                        raise with_error_code(RuntimeError(error), 'TAISHIN-05-063-004')
                elif row['txnamt'] != '-' and cls._history_money(str(row['txnamt'])) != 0:
                    raise with_error_code(RuntimeError(error), 'TAISHIN-05-064-001')
                balance = cls._history_money(
                    "-" if row["newbal"] is None else str(row["newbal"]),
                    allow_dash=True,
                )
                cells = [
                    f"{stamp[1][:4]}/{stamp[1][4:6]}/{stamp[1][6:8]} "
                    f"{time_digits[:2]}:{time_digits[2:4]}:{time_digits[4:6]}",
                    f"{row['dateNew'][:4]}/{row['dateNew'][4:6]}/{row['dateNew'][6:8]}",
                    description,
                    "-" if amount is None else str(-abs(amount) if outgoing else abs(amount)),
                    "-" if balance is None else str(balance),
                    note,
                    "",
                ]
                normalized.append(cls._normalize_history_cells(
                    cells, identity=identity, start=start, end=end,
                ))
            except (RuntimeError, TypeError, ValueError) as source_error:
                raise with_error_code(RuntimeError(error), exception_error_id(source_error, bank="taishin") or 'TAISHIN-05-065-001') from None
        return {"row_count": len(normalized), "rows": normalized, "transport": transport}

    @classmethod
    def _validate_history_snapshot(
        cls,
        snapshot: dict,
        *,
        identity: str,
        period: str,
        start: date,
        end: date,
        api_row_count: int,
        api_rows: list[dict] | None = None,
    ) -> dict:
        error = "taishin-twd-history-dom"
        keys = {
            "evidence_fresh", "mutation_count", "quiet_ms", "route_bound",
            "selected_identity", "selected_period",
            "selected_sort", "busy_count", "dialog_count", "error_count", "table_count",
            "headers", "rows", "total_count", "more_button_count", "no_more_count",
            "pager_count", "no_result_count", "result_scope_bound",
        }
        if not isinstance(snapshot, dict):
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-066-001')
        if set(snapshot) != keys:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-066-002')
        if snapshot.get('evidence_fresh') is not True:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-066-003')
        if snapshot.get('route_bound') is not True:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-066-004')
        if snapshot.get('result_scope_bound') is not True:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-066-005')
        if snapshot.get('selected_identity') != identity:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-066-006')
        if snapshot.get('selected_period') != period:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-066-007')
        if snapshot.get('selected_sort') != 'forward':
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-066-008')
        for key in ('mutation_count', 'quiet_ms', 'busy_count', 'dialog_count', 'error_count', 'table_count', 'total_count', 'more_button_count', 'no_more_count', 'pager_count', 'no_result_count'):
            if type(snapshot.get(key)) is not int:
                raise with_error_code(RuntimeError(error), {'mutation_count': 'TAISHIN-05-066-020', 'quiet_ms': 'TAISHIN-05-066-021', 'busy_count': 'TAISHIN-05-066-022', 'dialog_count': 'TAISHIN-05-066-023', 'error_count': 'TAISHIN-05-066-024', 'table_count': 'TAISHIN-05-066-025', 'total_count': 'TAISHIN-05-066-026', 'more_button_count': 'TAISHIN-05-066-027', 'no_more_count': 'TAISHIN-05-066-028', 'pager_count': 'TAISHIN-05-066-029', 'no_result_count': 'TAISHIN-05-066-030'}[key])
            if snapshot[key] < 0:
                raise with_error_code(RuntimeError(error), {'mutation_count': 'TAISHIN-05-066-031', 'quiet_ms': 'TAISHIN-05-066-032', 'busy_count': 'TAISHIN-05-066-033', 'dialog_count': 'TAISHIN-05-066-034', 'error_count': 'TAISHIN-05-066-035', 'table_count': 'TAISHIN-05-066-036', 'total_count': 'TAISHIN-05-066-037', 'more_button_count': 'TAISHIN-05-066-038', 'no_more_count': 'TAISHIN-05-066-039', 'pager_count': 'TAISHIN-05-066-040', 'no_result_count': 'TAISHIN-05-066-041'}[key])
        if snapshot['busy_count'] != 0:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-066-010')
        if snapshot['dialog_count'] != 0:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-066-011')
        if snapshot['error_count'] != 0:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-066-012')
        if snapshot['pager_count'] != 0:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-066-013')
        if snapshot['mutation_count'] <= 0:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-066-014')
        if snapshot['quiet_ms'] < 1500:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-066-015')
        if type(api_row_count) is not int:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-066-016')
        if api_row_count < 0:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-066-017')
        if not isinstance(snapshot.get('headers'), list):
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-066-018')
        if not isinstance(snapshot.get('rows'), list):
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-066-019')
        if api_row_count == 0:
            if snapshot['table_count'] != 0:
                raise with_error_code(RuntimeError(error), 'TAISHIN-05-067-001')
            elif snapshot['headers']:
                raise with_error_code(RuntimeError(error), 'TAISHIN-05-067-002')
            elif snapshot['rows']:
                raise with_error_code(RuntimeError(error), 'TAISHIN-05-067-003')
            elif snapshot['total_count'] != 0:
                raise with_error_code(RuntimeError(error), 'TAISHIN-05-067-004')
            elif snapshot['more_button_count'] != 0:
                raise with_error_code(RuntimeError(error), 'TAISHIN-05-067-005')
            elif snapshot['no_more_count'] != 0:
                raise with_error_code(RuntimeError(error), 'TAISHIN-05-067-006')
            elif snapshot['no_result_count'] != 1:
                raise with_error_code(RuntimeError(error), 'TAISHIN-05-067-007')
            return {"status": "explicit_empty", "rows": []}
        if snapshot['table_count'] != 1:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-068-001')
        elif snapshot['headers'] != ['交易日', '帳務日', '摘要', '金額', '餘額', '備註', '']:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-068-002')
        elif len(snapshot['rows']) != api_row_count:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-068-003')
        elif snapshot['total_count'] != api_row_count:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-068-004')
        elif snapshot['more_button_count'] != 0:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-068-005')
        elif snapshot['no_more_count'] != 1:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-068-006')
        elif snapshot['no_result_count'] != 0:
            raise with_error_code(RuntimeError(error), 'TAISHIN-05-068-007')

        normalized = []
        for row in snapshot["rows"]:
            if not isinstance(row, list):
                raise with_error_code(RuntimeError(error), 'TAISHIN-05-069-001')
            if len(row) != 7:
                raise with_error_code(RuntimeError(error), 'TAISHIN-05-069-002')
            for v in row:
                if not isinstance(v, str):
                    raise with_error_code(RuntimeError(error), 'TAISHIN-05-069-003')
            normalized.append(cls._normalize_history_cells(
                row, identity=identity, start=start, end=end,
            ))
        if api_rows is not None:
            try:
                bound_dom = sorted(
                    json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    for row in normalized
                )
                bound_api = sorted(
                    json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    for row in api_rows
                )
            except (TypeError, ValueError):
                raise with_error_code(RuntimeError(error), 'TAISHIN-05-070-001') from None
            if bound_dom != bound_api:
                raise with_error_code(RuntimeError(error), 'TAISHIN-05-071-001')
        return {"status": "complete", "rows": normalized}

    @staticmethod
    def _history_frame(page):
        matches = []
        for frame in page.frames:
            parsed = urlparse(frame.url or "")
            if (
                parsed.scheme == "https"
                and parsed.hostname == "my.taishinbank.com.tw"
                and parsed.port in (None, 443)
                and parsed.username is None
                and parsed.password is None
                and parsed.path in {
                    "/TIBNetBank/svc/rwd/",
                    "/TIBNetBank/svc/rwd/index.html",
                }
            ):
                matches.append(frame)
        if len(matches) != 1:
            raise with_error_code(RuntimeError('taishin-twd-history-frame'), 'TAISHIN-05-040-001')
        return matches[0]

    _HISTORY_FORM_JS = r"""() => {
            const visible = el => {
                if (!el || el.hidden || (el.getAttribute('aria-hidden') || '').toLowerCase() === 'true') return false;
                for (let p = el; p; p = p.parentElement) {
                    const style = getComputedStyle(p);
                    if (p.hidden || (p.getAttribute('aria-hidden') || '').toLowerCase() === 'true' ||
                        style.display === 'none' || ['hidden', 'collapse'].includes(style.visibility) ||
                        Number(style.opacity) === 0) return false;
                }
                const box = el.getBoundingClientRect();
                return box.width > 0 && box.height > 0;
            };
            const selects = [...document.querySelectorAll('select')]
                .map((select, index) => ({select, index})).filter(item => visible(item.select));
            const queryButtons = [...document.querySelectorAll("input[value='查詢']")]
                .map((button, index) => ({button, index})).filter(item => visible(item.button));
            return {
                query_buttons: queryButtons.length,
                query_button: queryButtons.length === 1 ? queryButtons[0].index : -1,
                selects: selects.map(item => ({
                    index: item.index,
                    selected_index: item.select.selectedIndex,
                    options: [...item.select.options].map((option, optionIndex) => ({
                        index: optionIndex,
                        text: (option.textContent || '').replace(/\s+/g, ' ').trim(),
                        value: option.value || '',
                    })),
                })),
            };
        }"""

    @classmethod
    def _history_form_snapshot(cls, frame) -> dict:
        with error_step('TAISHIN-05-041-001'):
            return frame.evaluate(cls._HISTORY_FORM_JS)

    @classmethod
    def _history_result_snapshot(cls, frame, *, expected_form: dict | None = None) -> dict:
        with error_step('TAISHIN-05-080-001'):
            snapshot = frame.evaluate("() => { const form = (" + cls._HISTORY_FORM_JS + r""")();
            const visible = el => {
                if (!el || el.hidden || (el.getAttribute('aria-hidden') || '').toLowerCase() === 'true') return false;
                for (let p = el; p; p = p.parentElement) {
                    const style = getComputedStyle(p);
                    if (p.hidden || (p.getAttribute('aria-hidden') || '').toLowerCase() === 'true' ||
                        style.display === 'none' || ['hidden', 'collapse'].includes(style.visibility) ||
                        Number(style.opacity) === 0) return false;
                }
                const box = el.getBoundingClientRect();
                return box.width > 0 && box.height > 0;
            };
            const norm = value => (value || '').replace(/\s+/g, ' ').trim();

            const tables = [...document.querySelectorAll('#savingAccountTransactionTable')].filter(visible);
            const table = tables.length === 1 ? tables[0] : null;
            let container = table && table.parentElement;
            while (container && !container.querySelector('._table_more')) container = container.parentElement;
            const total = norm(table && table.querySelector('tfoot')?.textContent).match(/共\s*(\d+)\s*筆(?:資料)?/);
            const bodyText = norm(document.body?.innerText);
            const noResults = [...document.querySelectorAll('._section_inquiry-result--noresult')]
                .filter(visible).filter(node => norm(node.textContent).includes('查無資料'));
            const resultAnchor = table || (noResults.length === 1 ? noResults[0] : null);
            const resultRoot = resultAnchor && (
                resultAnchor.closest('._section_inquiry-result, ._section_content, section') ||
                (container && container !== document.body ? container : null) ||
                (noResults.length === 1 && noResults[0].parentElement !== document.body
                    ? noResults[0].parentElement : null)
            );
            const structuralErrors = [...document.querySelectorAll("[role='alert'], .alert, .error")]
                .filter(visible);
            const pagerControls = resultRoot ? [...resultRoot.querySelectorAll(
                ".pagination, [class~='pagination'], [rel='next'], [aria-label='下一頁'], [title='下一頁']"
            )] : [];
            for (const node of resultRoot?.querySelectorAll("button, a, [role='button']") || []) {
                const label = norm(node.textContent || node.getAttribute('aria-label') || node.title);
                const handler = node.getAttribute('onclick') || '';
                if (/^\d+$/.test(label) || /^(?:下一頁|next|›|»)>?$/i.test(label) || /(?:next|page)/i.test(handler)) {
                    pagerControls.push(node);
                }
            }
            const mutation = window.__thothTaishinHistoryMutation;
            return {
                evidence_fresh: false,
                mutation_count: mutation?.count || 0,
                quiet_ms: mutation?.count ? Math.max(0, Date.now() - mutation.last) : 0,
                route_bound: /^#\/RB0102\/0100(?:\?ts=\d+)?$/.test(location.hash),
                result_scope_bound: Boolean(resultRoot),
                form,
                busy_count: [...document.querySelectorAll(
                    "[aria-busy='true'], [role='progressbar'], .loading, .loader, .spinner"
                )].filter(visible).length,
                dialog_count: [...document.querySelectorAll("[role='dialog'], .modal.show")]
                    .filter(visible).length,
                error_count: structuralErrors.length + (
                    /(?:系統錯誤|系統忙碌|請稍後再試|登入逾時|請重新登入|session expired)/i
                        .test(bodyText) ? 1 : 0
                ),
                table_count: tables.length,
                headers: table ? [...table.querySelectorAll('thead th')].filter(visible).map(node => norm(node.textContent)) : [],
                rows: table ? [...table.querySelectorAll('tbody tr')].filter(visible).map(row =>
                    [...row.querySelectorAll(':scope > th, :scope > td')].filter(visible).map(cell => cell.textContent || '')
                ) : [],
                total_count: total ? Number(total[1]) : 0,
                more_button_count: container ? container.querySelectorAll('._table_more__btn').length : 0,
                no_more_count: container ? [...container.querySelectorAll('._table_more__nomore')]
                    .filter(visible).filter(node => norm(node.textContent) === '沒有更多資料了').length : 0,
                pager_count: new Set(pagerControls).size,
                no_result_count: noResults.length,
            };
        }""")
        raw_form = snapshot.pop("form")
        try:
            form = (
                cls._require_history_inventory(raw_form, expected_form)
                if expected_form is not None else cls._validate_history_form(raw_form)
            )
        except RuntimeError:
            if expected_form is not None:
                raise
            # Structural snapshots remain usable, but no identity is trusted without the full form.
            snapshot.update(selected_identity="", selected_period="", selected_sort="")
        else:
            snapshot.update(cls._history_selected_options(raw_form, form))
        return snapshot

    def _collect_attested_twd_history(
        self,
        page,
        collector: ResponseCollector,
        *,
        as_of: date | None = None,
    ) -> dict:
        as_of = as_of or datetime.now(ZoneInfo("Asia/Taipei")).date()
        mode = os.environ.get("BANK_CRAWLER_HISTORY_MODE", "full")
        if mode not in {'full', 'incremental'}:
            raise with_error_code(ValueError(f'invalid BANK_CRAWLER_HISTORY_MODE: {mode!r}'), 'TAISHIN-05-001-001')
        frame = self._history_frame(page)
        with error_step('TAISHIN-05-002-001'):
            frame.evaluate("location.hash = '#/RB0102/0100?ts=' + Date.now()")
        form = None
        inventory_signature = None
        inventory_stable_count = 0
        inventory_failure_id = None
        for _ in range(60):
            inventory_failure_id = None
            with error_step('TAISHIN-05-003-001'):
                page.wait_for_timeout(500)
            frame = self._history_frame(page)
            try:
                current_form = self._validate_history_form(self._history_form_snapshot(frame))
            except RuntimeError as _action_error:
                inventory_failure_id = exception_error_id(_action_error, bank="taishin") or 'TAISHIN-05-004-001'
                _log(format_error_id(inventory_failure_id, bank="taishin"))
                inventory_signature = None
                inventory_stable_count = 0
                continue
            current_signature = current_form
            if current_signature == inventory_signature:
                inventory_stable_count += 1
            else:
                inventory_signature = current_signature
                inventory_stable_count = 0
            form = current_form
            if inventory_stable_count >= 2 and current_form["accounts"]:
                break
        if form is None:
            raise with_error_code(RuntimeError('taishin-twd-history-form'), inventory_failure_id or 'TAISHIN-05-005-001')
        elif inventory_stable_count < 2:
            raise with_error_code(RuntimeError('taishin-twd-history-form'), inventory_failure_id or 'TAISHIN-05-005-002')
        inventory = form["accounts"]
        if not inventory:
            state = self._history_result_snapshot(frame)
            if state.get('route_bound') is not True:
                raise with_error_code(RuntimeError('taishin-twd-history-empty-inventory'), 'TAISHIN-05-006-001')
            for key in ('busy_count', 'dialog_count', 'error_count', 'table_count', 'no_result_count', 'more_button_count', 'no_more_count', 'pager_count'):
                if type(state.get(key)) is not int:
                    raise with_error_code(RuntimeError('taishin-twd-history-empty-inventory'), {'busy_count': 'TAISHIN-05-006-004', 'dialog_count': 'TAISHIN-05-006-005', 'error_count': 'TAISHIN-05-006-006', 'table_count': 'TAISHIN-05-006-007', 'no_result_count': 'TAISHIN-05-006-008', 'more_button_count': 'TAISHIN-05-006-009', 'no_more_count': 'TAISHIN-05-006-010', 'pager_count': 'TAISHIN-05-006-011'}[key])
                if state[key] != 0:
                    raise with_error_code(RuntimeError('taishin-twd-history-empty-inventory'), {'busy_count': 'TAISHIN-05-006-012', 'dialog_count': 'TAISHIN-05-006-013', 'error_count': 'TAISHIN-05-006-014', 'table_count': 'TAISHIN-05-006-015', 'no_result_count': 'TAISHIN-05-006-016', 'more_button_count': 'TAISHIN-05-006-017', 'no_more_count': 'TAISHIN-05-006-018', 'pager_count': 'TAISHIN-05-006-019'}[key])
            if getattr(self, '_shared_dialog_blocked', False):
                raise with_error_code(RuntimeError('taishin-twd-history-empty-inventory'), 'TAISHIN-05-006-003')
        results, expected, receipts = [], [], []
        response_bytes = 0
        inventory_signature = form

        for position, account in enumerate(inventory):
            if position:
                with error_step('TAISHIN-05-007-001'):
                    frame.evaluate("location.hash = '#/RB0102/0100?ts=' + Date.now()")
                inventory_failure_id = None
                for _ in range(60):
                    inventory_failure_id = None
                    with error_step('TAISHIN-05-008-001'):
                        page.wait_for_timeout(500)
                    frame = self._history_frame(page)
                    try:
                        form = self._validate_history_form(self._history_form_snapshot(frame))
                    except RuntimeError as _action_error:
                        inventory_failure_id = exception_error_id(_action_error, bank="taishin") or 'TAISHIN-05-009-001'
                        _log(format_error_id(inventory_failure_id, bank="taishin"))
                        continue
                    if form == inventory_signature:
                        break
                else:
                    raise with_error_code(RuntimeError('taishin-twd-history-inventory'), inventory_failure_id or 'TAISHIN-05-010-001')
            window = self._history_window(account["identity"], as_of)
            self._select_history_options(
                frame, form, identity=account["identity"], period=window["period"],
            )

            # Release hover menus before checking readiness; never force the query click.
            with error_step('TAISHIN-05-850-001'):
                page.mouse.move(-1, -1)
            # Selection can briefly show a loader before any history submission.
            for attempt in range(21):
                if self._history_frame(page) is not frame:
                    raise with_error_code(RuntimeError('taishin-twd-history-stale-before-query'), 'TAISHIN-05-011-001')
                before_result = self._history_result_snapshot(frame, expected_form=inventory_signature)
                for key in ('table_count', 'no_result_count', 'dialog_count', 'error_count'):
                    if type(before_result.get(key)) is not int:
                        raise with_error_code(RuntimeError('taishin-twd-history-stale-before-query'), {'table_count': 'TAISHIN-05-012-002', 'no_result_count': 'TAISHIN-05-012-003', 'dialog_count': 'TAISHIN-05-012-004', 'error_count': 'TAISHIN-05-012-005'}[key])
                    if before_result[key] != 0:
                        raise with_error_code(RuntimeError('taishin-twd-history-stale-before-query'), {'table_count': 'TAISHIN-05-012-006', 'no_result_count': 'TAISHIN-05-012-007', 'dialog_count': 'TAISHIN-05-012-008', 'error_count': 'TAISHIN-05-012-009'}[key])
                busy = before_result.get('busy_count')
                if type(busy) is not int:
                    raise with_error_code(RuntimeError('taishin-twd-history-stale-before-query'), 'TAISHIN-05-013-001')
                elif busy < 0:
                    raise with_error_code(RuntimeError('taishin-twd-history-stale-before-query'), 'TAISHIN-05-013-002')
                if busy == 0:
                    break
                if attempt == 20:
                    raise with_error_code(RuntimeError('taishin-twd-history-stale-before-query'), 'TAISHIN-05-014-001')
                with error_step('TAISHIN-05-015-001'):
                    page.wait_for_timeout(500)
            with error_step('TAISHIN-05-016-001'):
                frame.evaluate("""() => {
                window.__thothTaishinHistoryObserver?.disconnect();
                const state = {count: 0, last: 0};
                window.__thothTaishinHistoryMutation = state;
                const selector = '#savingAccountTransactionTable, ._section_inquiry-result--noresult, ._table_more';
                window.__thothTaishinHistoryObserver = new MutationObserver(records => {
                    // Watch insertion/removal too: the result need not exist before query.
                    const relevant = records.filter(record => {
                        const target = record.target.nodeType === Node.ELEMENT_NODE
                            ? record.target : record.target.parentElement;
                        return target?.closest(selector) || [...record.addedNodes, ...record.removedNodes]
                            .some(node => node.nodeType === Node.ELEMENT_NODE &&
                                (node.matches(selector) || node.querySelector(selector)));
                    });
                    if (!relevant.length) return;
                    state.count += relevant.length;
                    state.last = Date.now();
                });
                window.__thothTaishinHistoryObserver.observe(document.body, {
                    childList: true, subtree: true, characterData: true,
                });
            }""")
            boundary = collector.request_sequence
            issued_before = collector.issued_count("query")
            with error_step('TAISHIN-05-017-001'):
                query = frame.locator("input[value='查詢']")
            with error_step('TAISHIN-05-018-001'):
                if query.count() <= form['query_button']:
                    raise with_error_code(RuntimeError('taishin-twd-history-query'), 'TAISHIN-05-019-001')
            submitted_frame = frame
            with error_step('TAISHIN-05-020-001'):
                error_step('TAISHIN-05-703-001')(lambda: error_step('TAISHIN-05-704-001')(lambda: query.nth(form["query_button"]))().click(timeout=8000))()
            matching = []
            for _ in range(120):
                with error_step('TAISHIN-05-021-001'):
                    page.wait_for_timeout(500)
                matching = [
                    hit for hit in collector.hits
                    if hit.request_sequence > boundary
                    and urlparse(hit.raw_url or hit.url).path == "/TIBNetBank/svc/web1/rb0102/query"
                ]
                if matching:
                    break
            if len(matching) != 1:
                raise with_error_code(RuntimeError('taishin-twd-history-response'), 'TAISHIN-05-022-001')
            response = self._validate_history_hit(
                matching[0], identity=account["identity"], start=window["start"],
                end=window["end"], boundary=boundary, expected_frame=submitted_frame,
            )
            body_size = matching[0].body_size
            if type(body_size) is not int:
                raise with_error_code(RuntimeError('taishin-twd-history-response-budget'), 'TAISHIN-05-023-001')
            response_bytes = self._add_history_response_bytes(response_bytes, body_size)

            frame = self._history_frame(page)
            if frame is not submitted_frame:
                raise with_error_code(RuntimeError('taishin-twd-history-frame'), 'TAISHIN-05-024-001')
            for _ in range(min(response["row_count"] + 2, 10_002)):
                if self._history_frame(page) is not submitted_frame:
                    raise with_error_code(RuntimeError('taishin-twd-history-frame'), 'TAISHIN-05-025-001')
                snapshot = self._history_result_snapshot(frame, expected_form=inventory_signature)
                if snapshot["more_button_count"] == 0:
                    break
                before = len(snapshot["rows"])
                with error_step('TAISHIN-05-026-001'):
                    buttons = frame.locator("._table_more__btn:visible")
                with error_step('TAISHIN-05-027-001'):
                    if buttons.count() != 1:
                        raise with_error_code(RuntimeError('taishin-twd-history-pagination'), 'TAISHIN-05-028-001')
                with error_step('TAISHIN-05-029-001'):
                    error_step('TAISHIN-05-705-001')(lambda: error_step('TAISHIN-05-706-001')(lambda: buttons.nth(0))().click(timeout=5000))()
                with error_step('TAISHIN-05-030-001'):
                    page.wait_for_timeout(300)
                if len(self._history_result_snapshot(frame, expected_form=inventory_signature)['rows']) <= before:
                    raise with_error_code(RuntimeError('taishin-twd-history-pagination'), 'TAISHIN-05-031-001')
            else:
                raise with_error_code(RuntimeError('taishin-twd-history-pagination'), 'TAISHIN-05-032-001')

            stable = None
            stable_count = 0
            for _ in range(60):
                with error_step('TAISHIN-05-033-001'):
                    page.wait_for_timeout(500)
                frame = self._history_frame(page)
                if frame is not submitted_frame:
                    raise with_error_code(RuntimeError('taishin-twd-history-frame'), 'TAISHIN-05-034-001')
                snapshot = self._history_result_snapshot(frame, expected_form=inventory_signature)
                signature = repr({**snapshot, "evidence_fresh": False, "quiet_ms": 0})
                if signature == stable and snapshot["busy_count"] == 0:
                    stable_count += 1
                else:
                    stable, stable_count = signature, 0
                terminal = (
                    snapshot["no_result_count"] == 1
                    if response["row_count"] == 0
                    else snapshot["table_count"] == 1 and snapshot["more_button_count"] == 0
                )
                if stable_count >= 4 and terminal and snapshot["quiet_ms"] >= 1500:
                    snapshot["evidence_fresh"] = True
                    break
            else:
                raise with_error_code(RuntimeError('taishin-twd-history-settle'), 'TAISHIN-05-035-001')
            if self._shared_dialog_blocked:
                raise with_error_code(RuntimeError('taishin-twd-history-dialog'), 'TAISHIN-05-036-001')
            matching = [
                hit for hit in collector.hits
                if hit.request_sequence > boundary
                and urlparse(hit.raw_url or hit.url).path == "/TIBNetBank/svc/web1/rb0102/query"
            ]
            if len(matching) != 1:
                raise with_error_code(RuntimeError('taishin-twd-history-response'), 'TAISHIN-05-037-001')
            elif collector.issued_count('query') != issued_before + 1:
                raise with_error_code(RuntimeError('taishin-twd-history-response'), 'TAISHIN-05-037-002')
            quiescent_hit = self._require_history_network_quiescence(
                page,
                collector,
                submitted_frame=submitted_frame,
                boundary=boundary,
                request_sequence=collector.request_sequence,
                issued_count=issued_before + 1,
            )
            if quiescent_hit is not matching[0]:
                raise with_error_code(RuntimeError('taishin-twd-history-response'), 'TAISHIN-05-038-001')
            snapshot = self._history_result_snapshot(submitted_frame, expected_form=inventory_signature)
            snapshot["evidence_fresh"] = True
            with error_step('TAISHIN-05-039-001'):
                submitted_frame.evaluate("window.__thothTaishinHistoryObserver?.disconnect()")
            self._require_history_inventory(
                self._history_form_snapshot(frame), inventory_signature,
            )
            validated = self._validate_history_snapshot(
                snapshot, identity=account["identity"], period=window["period"],
                start=window["start"], end=window["end"],
                api_row_count=response["row_count"],
                api_rows=response["rows"],
            )
            binding_digest = self._history_rows_digest(response["rows"])
            receipt = {
                "identity": account["identity"],
                "start": window["start"].isoformat(),
                "end": window["end"].isoformat(),
                "status": validated["status"],
                "pages": 1,
            }
            results.append({
                **receipt,
                "period": window["period"],
                "rows": validated["rows"],
                "snapshot": snapshot,
                "api_row_count": response["row_count"],
                "api_rows": response["rows"],
                "transport": response["transport"],
                "binding_digest": binding_digest,
                "request_count": 1,
                "response_count": 1,
            })
            expected.append({
                "identity": account["identity"],
                "start": window["start"].isoformat(),
                "end": window["end"].isoformat(),
            })
            receipts.append(receipt)

        domain = {
            "domain": "twd_transactions",
            "expected": expected,
            "windows": receipts,
        }
        if not expected:
            domain["empty_window"] = {
                "start": self._subtract_months(as_of, 12).isoformat(),
                "end": as_of.isoformat(),
                "status": "explicit_empty",
                "pages": 1,
            }
        coverage = {
            "version": 1,
            "mode": mode,
            "domains": [domain],
        }
        from backend.core.base import validate_history_coverage

        with error_step('TAISHIN-07-001-001'):
            validate_history_coverage(
                coverage,
                expected_mode=mode,
                expected_domains=self.HISTORY_COVERAGE_DOMAINS,
            )
        return {"twd_txn_results": results, "history_coverage": coverage}

    def _find_login_frame(self, page):
        matches = [frame for frame in page.frames if self._is_login_frame_url(frame.url)]
        return matches[0] if len(matches) == 1 else None

    @classmethod
    def _is_login_frame_url(cls, url: str | None) -> bool:
        current = urlparse(url or "")
        return (
            cls._exact_https_origin_allowed(url or "", cls.CREDENTIAL_HOSTS)
            and IFRAME_HINT in (current.path or "").lower()
        )

    def _logged_in(self, page) -> bool:
        """Pure one-shot positive check; lifecycle owns all waiting."""
        try:
            if not self._exact_https_origin_allowed(page.url, self.CREDENTIAL_HOSTS):
                return False
            if any(self._is_login_frame_url(frame.url) for frame in page.frames):
                return False
            scopes = [
                page,
                *(
                    frame
                    for frame in page.frames
                    if frame is not page.main_frame
                    and self._frame_origin_allowed(page, frame)
                ),
            ]
            for scope in scopes:
                with error_step('TAISHIN-03-006-001'):
                    login_fields = scope.locator("input[placeholder='身分證字號']")
                with error_step('TAISHIN-03-007-001'):
                    if any(
                        error_step('TAISHIN-03-707-001')(lambda: error_step('TAISHIN-03-708-001')(lambda: login_fields.nth(index))().is_visible())()
                        for index in range(error_step('TAISHIN-03-709-001')(lambda: login_fields.count())())
                    ):
                        return False
            with error_step('TAISHIN-03-008-001'):
                body = "\n".join(
                    scope.evaluate("() => document.body && document.body.innerText || ''") or ""
                    for scope in scopes
                )
        except Exception:
            return False
        keywords = (
            "帳戶總覽",
            "我的資產",
            "台幣存款",
            "我的帳戶",
            "信用卡管理",
            "網銀首頁",
            "資產總額",
            "存款餘額",
        )
        return (
            len(body) >= 500
            and "登出" in body
            and any(keyword in body for keyword in keywords)
        )

    def login(self, page) -> bool:
        return self._shared_login(page)

    def prepare_login_page(self, page) -> None:
        with error_step('TAISHIN-01-001-001'):
            page.wait_for_timeout(10000)

    def is_authenticated(self, page) -> bool:
        return self._logged_in(page)

    def login_checkpoint_rules(self) -> tuple[LoginCheckpointRule, ...]:
        duplicate_body = re.compile(
            r"^\s*(?:上次未正常登出|未正常登出)\s*"
            r"(?:[，。:：-]\s*)?(?:請\s*)?(?:重新登入|重新登錄)\s*$"
        )
        all_phases = tuple(CheckpointPhase)
        post_settle = (
            CheckpointPhase.POST_SUBMIT,
            CheckpointPhase.POST_SUBMIT_SETTLE,
        )
        notice_body = re.compile(
            r"^\s*(?:(?:系統斷信|訊息通知)\s*[：:]?\s*){1,2}我知道了\s*$"
        )
        otp_body = re.compile(
            r"^[\s\S]*(?:OTP|一次性密碼|簡訊驗證碼|驗證碼已傳送|"
            r"裝置驗證|信任此裝置|新裝置登入)[\s\S]*$",
            re.IGNORECASE,
        )
        mandatory_password_body = re.compile(
            r"^[\s\S]*(?:(?:立即|必須|請先|需要|需)\s*(?:修改|變更|重設)\s*"
            r"(?:您的?)?\s*密碼|密碼[\s\S]*(?:已到期|過期|必須修改|需要修改|"
            r"需修改|強制變更))[\s\S]*$"
        )
        return (
            *(
                LoginCheckpointRule(
                    name=f"taishin-otp-required-{suffix}",
                    bank="taishin",
                    phases=all_phases,
                    kind=CheckpointKind.OTP_REQUIRED,
                    container_selector=selector,
                    required_body_pattern=otp_body,
                )
                for suffix, selector in (
                    ("modal", ".modal.show"),
                    ("dialog", "[role='dialog']"),
                )
            ),
            *(
                LoginCheckpointRule(
                    name=f"taishin-mandatory-password-{suffix}",
                    bank="taishin",
                    phases=all_phases,
                    kind=CheckpointKind.PASSWORD_CHANGE_REQUIRED,
                    container_selector=selector,
                    required_body_pattern=mandatory_password_body,
                )
                for suffix, selector in (
                    ("modal", ".modal.show"),
                    ("dialog", "[role='dialog']"),
                )
            ),
            *(
                LoginCheckpointRule(
                    name=f"taishin-pre-duplicate-{suffix}",
                    bank="taishin",
                    phases=(CheckpointPhase.PRE_SUBMIT,),
                    kind=CheckpointKind.DUPLICATE_SESSION,
                    container_selector=selector,
                    action_texts=("重新登入", "重新登錄"),
                    required_body_pattern=duplicate_body,
                )
                for suffix, selector in (
                    ("modal", ".modal.show"),
                    ("dialog", "[role='dialog']"),
                )
            ),
            *(
                LoginCheckpointRule(
                    name=f"taishin-post-protocol-{suffix}",
                    bank="taishin",
                    phases=(CheckpointPhase.POST_SUBMIT,),
                    kind=CheckpointKind.PROTOCOL_RESUBMIT,
                    container_selector=selector,
                    action_texts=("重新登入", "重新登錄"),
                    required_body_pattern=duplicate_body,
                )
                for suffix, selector in (
                    ("modal", ".modal.show"),
                    ("dialog", "[role='dialog']"),
                )
            ),
            *(
                LoginCheckpointRule(
                    name=f"taishin-post-notice-{suffix}",
                    bank="taishin",
                    phases=post_settle,
                    kind=CheckpointKind.DISMISSIBLE_NOTICE,
                    container_selector=selector,
                    action_texts=("我知道了",),
                    required_body_pattern=notice_body,
                )
                for suffix, selector in (
                    ("modal", ".modal.show"),
                    ("dialog", "[role='dialog']"),
                )
            ),
            LoginCheckpointRule(
                name="taishin-unknown-modal",
                bank="taishin",
                phases=all_phases,
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector=".modal.show",
            ),
            LoginCheckpointRule(
                name="taishin-unknown-dialog",
                bank="taishin",
                phases=all_phases,
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector="[role='dialog']",
            ),
            LoginCheckpointRule(
                name="taishin-login-form-still-visible",
                bank="taishin",
                phases=post_settle,
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector="input[placeholder='身分證字號']",
            ),
        )

    def _try_ancestor_clicks(self, target_frame, page) -> bool:
        """台新信用卡 mega menu hover 策略 v2 (2026-06-11 真實 mouse)。

        實測揭示：信用卡 menu 是 `<li class="_nav_menu__item">` hover 觸發 mega menu。
        JS dispatch hover 無效（Vue/React 認得真實 mouse event），必須用 page.mouse.move
        真實 hover 到 LI 中心點 → 等 mega menu 展開 → 直接 click 子項。

        子項目錄（vision 揭示）：
          【帳單/查詢】 信用卡總覽、權益概覽、**查詢信用卡明細**、附卡人消費查詢、信用卡消費分析
          【點數/里數】 查詢與兌換點數
          【額度/分期理財】 查詢分期訂單、單筆消費分期、調升信用卡額度
          【預借現金】 申請預借現金
          【繳費】 繳信用卡費
        """
        # Step 1: 找 LI bbox（page coordinate，需加 iframe offset）
        try:
            with error_step('TAISHIN-06-001-001'):
                li_bbox = target_frame.evaluate("""() => {
                for (const sp of document.querySelectorAll('span._nav_menu__text')) {
                    if ((sp.textContent || '').trim() !== '信用卡') continue;
                    if (sp.offsetParent === null) continue;
                    // d2: SPAN → STRONG → LI
                    const li = sp.parentElement.parentElement;
                    if (!li || li.tagName !== 'LI') return null;
                    const r = li.getBoundingClientRect();
                    return {x: r.x, y: r.y, w: r.width, h: r.height};
                }
                return null;
            }""")
        except Exception as _action_error:
            _log(format_error_id(exception_error_id(_action_error, bank="taishin") or 'TAISHIN-06-002-001', bank="taishin"))
            _log("[taishin][collect] 取 LI bbox 例外")
            return False

        if not li_bbox:
            _log("[taishin][collect] ❌ 找不到信用卡 LI")
            return False

        _log(f"[taishin][collect] 信用卡 LI bbox @({int(li_bbox['x'])},{int(li_bbox['y'])}) {int(li_bbox['w'])}x{int(li_bbox['h'])}")

        # Step 2: 找 iframe 在主 page 的位置（加 offset）
        iframe_offset_x = 0
        iframe_offset_y = 0
        try:
            for fel in error_step('TAISHIN-06-850-001')(lambda: page.query_selector_all("iframe"))():
                with error_step('TAISHIN-06-003-001'):
                    src = fel.get_attribute("src") or ""
                if "svc/rwd" in src:
                    with error_step('TAISHIN-06-004-001'):
                        fbox = fel.bounding_box()
                    if fbox:
                        iframe_offset_x = fbox["x"]
                        iframe_offset_y = fbox["y"]
                        _log(f"[taishin][collect] iframe offset: ({int(iframe_offset_x)},{int(iframe_offset_y)})")
                    break
        except Exception as _action_error:
            _log(format_error_id(exception_error_id(_action_error, bank="taishin") or 'TAISHIN-06-005-001', bank="taishin"))
            _log("[taishin][collect] 取 iframe offset 失敗（用 0,0）")

        # Step 3: 真實 mouse.move 到 LI 中心
        li_center_x = iframe_offset_x + li_bbox["x"] + li_bbox["w"] / 2
        li_center_y = iframe_offset_y + li_bbox["y"] + li_bbox["h"] / 2
        _log(f"[taishin][collect] mouse.move → page coord ({li_center_x:.0f},{li_center_y:.0f})")

        try:
            # 先 move 到主畫面中央（避開所有 nav）→ 再 move 到 LI（保證觸發 mouseenter）
            with error_step('TAISHIN-06-851-001'):
                page.mouse.move(li_center_x, 400)
            with error_step('TAISHIN-06-006-001'):
                page.wait_for_timeout(200)
            with error_step('TAISHIN-06-852-001'):
                page.mouse.move(li_center_x, li_center_y, steps=10)
            with error_step('TAISHIN-06-007-001'):
                page.wait_for_timeout(1500)  # 等 mega menu 展開動畫
        except Exception as _action_error:
            _log(format_error_id(exception_error_id(_action_error, bank="taishin") or 'TAISHIN-06-008-001', bank="taishin"))
            _log("[taishin][collect] mouse.move 例外")
            return False

        # Step 4: dump mega menu 內可見 link（hover 還在，menu 還展開）
        try:
            with error_step('TAISHIN-06-009-001'):
                mega_links = target_frame.evaluate("""(li_y) => {
                const out = [];
                for (const a of document.querySelectorAll('a, button, li')) {
                    if (a.offsetParent === null) continue;
                    const r = a.getBoundingClientRect();
                    if (r.width < 5 || r.height < 5) continue;
                    if (r.y < li_y + 30) continue;  // 必須在 LI 下方
                    if (r.y > li_y + 700) continue;  // mega menu 範圍 (~700px 高)
                    const t = (a.textContent || '').trim();
                    if (!t || t.length > 25) continue;
                    if (a.tagName === 'LI' && a.querySelector('a, button')) continue;
                    out.push({tag: a.tagName, text: t, y: r.y, x: r.x, w: r.width, h: r.height});
                }
                // dedupe by (text, ~y)
                const seen = new Set();
                return out.filter(o => {
                    const k = `${o.text}_${Math.round(o.y/3)}`;
                    if (seen.has(k)) return false;
                    seen.add(k);
                    return true;
                });
            }""", li_bbox["y"])
        except Exception as _action_error:
            _log(format_error_id(exception_error_id(_action_error, bank="taishin") or 'TAISHIN-06-010-001', bank="taishin"))
            _log("[taishin][collect] dump mega links 失敗")
            mega_links = []

        _log(f"[taishin][collect] mega menu 可見 link 數={len(mega_links)}")

        # 判斷 mega menu 是否真的開了（找信用卡相關字樣）
        card_indicators = ["信用卡", "帳單", "消費明細", "紅利", "預借現金", "繳信"]
        opened = sum(1 for l in mega_links if any(k in l["text"] for k in card_indicators))
        _log(f"[taishin][collect] mega menu 含信用卡關鍵字 link 數 = {opened}")

        if opened == 0:
            _log("[taishin][collect] ❌ mega menu 未展開（無信用卡關鍵字 link）")
            return False

        # Step 5: 找優先目標並 click
        priority_targets = [
            "查詢信用卡明細",
            "信用卡消費分析",
            "附卡人消費查詢",
            "信用卡總覽",
        ]
        target_link = None
        for pri in priority_targets:
            for l in mega_links:
                if l["text"] == pri:
                    target_link = l
                    break
            if target_link is None:
                for l in mega_links:
                    if pri in l["text"]:
                        target_link = l
                        break
            if target_link:
                break

        if not target_link:
            _log("[taishin][collect] ❌ mega menu 內找不到優先目標")
            return False

        _log("[taishin][collect] 點 allowlisted mega menu 子項")
        # 真實 mouse.move 到子項 → click（保 hover 同時 click）
        try:
            click_x = iframe_offset_x + target_link["x"] + target_link["w"] / 2
            click_y = iframe_offset_y + target_link["y"] + target_link["h"] / 2
            with error_step('TAISHIN-06-853-001'):
                page.mouse.move(click_x, click_y, steps=5)
            with error_step('TAISHIN-06-011-001'):
                page.wait_for_timeout(300)
            with error_step('TAISHIN-06-012-001'):
                page.mouse.click(click_x, click_y)
            _log(f"[taishin][collect] page.mouse.click({click_x:.0f},{click_y:.0f})")
            with error_step('TAISHIN-06-013-001'):
                page.wait_for_timeout(6000)
            return True
        except Exception as _action_error:
            _log(format_error_id(exception_error_id(_action_error, bank="taishin") or 'TAISHIN-06-014-001', bank="taishin"))
            _log("[taishin][collect] click 子項例外")
            return False

    def _parse_credit_card_page(self, text: str) -> dict:
        """從「查詢信用卡明細」frame text (RB0708/0100) 抽結構化資料。

        2026-06-11 sample frame text 包含以下 5 段：
          [1] 頂部卡片：即時消費紀錄 N 筆 / 未出帳 TWD M / 分期金額 / 筆數
          [2] 當期應繳款項明細：6 欄（前期/新增/帳單/最低/已繳/剩餘）
          [3] 上期實繳金額明細：5-column table（消費日期/入帳起息日/明細/幣別/金額）
          [4] 即時消費紀錄（卡片末四碼）：6-column table（日期/時間/明細/金額/國別/授權）
          [5] 卡名：「Richart卡(原FlyGo鈦金商務) (卡號末四碼:1409)」

        Returns:
          {
            cards: [{number, name, ...}],
            pending_txns: [{txn_date, desc, amount, card_no_suffix}],
            billed_txns: [{txn_date, post_date, desc, currency, amount, card_no_suffix}],
            summary: {prev_balance, new_charges, bill_amount, min_pay, paid, remaining, ...},
            top_summary: {pending_count, pending_amount_twd, installment_amount, installment_count},
            billing_period: {start, end, bill_date, pay_due_date, bill_amount, min_pay},
          }
        """
        import re
        error_text = text.lower()
        error_markers = (
            "系統錯誤", "系統忙碌", "請稍後再試", "登入逾時", "連線逾時",
            "連線已逾時", "請重新登入", "登入失效",
            "system error", "try again later", "session expired", "login required",
            "timed out", "timeout", "log in again", "login again", "unexpected error",
        )
        realtime_headers = ("即時消費紀錄", "消費日期", "消費時間", "消費明細", "授權結果")
        out: dict = {
            "fetch_ok": (not any(marker in error_text for marker in error_markers)
                         and all(header in text for header in realtime_headers)),
            "cards": [], "pending_txns": [], "billed_txns": [],
            "summary": {}, "top_summary": {}, "billing_period": {},
        }

        # ─── [5] 卡名 + 末四碼 ───
        # 「Richart卡(原FlyGo鈦金商務) (卡號末四碼:1409)」
        card_match = re.search(r"([^\n]+?)\s*\(卡號末四碼[::]\s*(\d{4})\)", text)
        card_no_suffix = None
        if card_match:
            card_name = card_match.group(1).strip()
            card_no_suffix = card_match.group(2)
            out["cards"].append({
                "number": f"****{card_no_suffix}",
                "name": card_name,
                "currency": "TWD",
            })

        # ─── [1] 頂部卡片摘要 ───
        # latest_n: 「即時消費紀錄\n最新一筆紀錄\nN」(N 是最近一筆 row index，非總數)
        m = re.search(r"即時消費紀錄\s*\n\s*最新一筆紀錄\s*\n\s*(\d+)\s*\n\s*消費日期", text)
        if m:
            out["top_summary"]["latest_n_marker"] = int(m.group(1))
        # unbilled: 「未出帳明細\nTWD\n80」
        m = re.search(r"未出帳明細\s*\n\s*TWD\s*\n\s*([\d,]+)", text)
        if m:
            out["top_summary"]["pending_amount_twd"] = int(m.group(1).replace(",", ""))
        # installment: 「分期交易金額\nN\n分期交易筆數\nM」
        m = re.search(r"分期交易金額\s*\n\s*([\d,]+)\s*\n\s*分期交易筆數\s*\n\s*([\d,]+)", text)
        if m:
            out["top_summary"]["installment_amount"] = int(m.group(1).replace(",", ""))
            out["top_summary"]["installment_count"] = int(m.group(2).replace(",", ""))
        # 即時消費紀錄總筆數: 「共 N 筆資料資料日期：YYYY/MM/DD HH:MM:SS」
        m = re.search(r"共\s*(\d+)\s*筆資料\s*資料日期[:：](\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}:\d{2})", text)
        if m:
            out["top_summary"]["pending_total_count"] = int(m.group(1))
            out["top_summary"]["snapshot_at"] = m.group(2)

        # ─── [2] 當期應繳款項明細 ───
        # 6 欄：前期餘額、本期新增款項、本期帳單金額、本期最低應繳金額、本期已繳總金額、本期剩餘應繳款項
        for label, key in [
            ("前期餘額", "prev_balance"),
            ("本期新增款項", "new_charges"),
            ("本期帳單金額", "bill_amount"),
            ("本期最低應繳金額", "min_pay"),
            ("本期已繳總金額", "paid"),
            ("本期剩餘應繳款項", "remaining"),
        ]:
            # 格式可能是「label\n\nN\n」或「label\n\n N\n」
            m = re.search(rf"{label}\s*\n+\s*([\d,]+(?:\.\d+)?)\s*\n", text)
            if m:
                out["summary"][key] = float(m.group(1).replace(",", ""))

        # 自動扣款帳號 (sensitive 但 frame text 已 masked 為 00288*****072722)
        m = re.search(r"自動扣款帳號[:：]([\d*]+)", text)
        if m:
            out["summary"]["auto_debit_account_masked"] = m.group(1)

        # ─── [3] 當月帳單期間 ───
        # 「2026/05 信用卡明細」+「入帳期間\n\n2026/4/13 - 2026/5/12」
        m = re.search(r"入帳期間\s*\n+\s*(\d{4}/\d{1,2}/\d{1,2})\s*-\s*(\d{4}/\d{1,2}/\d{1,2})", text)
        if m:
            out["billing_period"]["statement_start"] = m.group(1)
            out["billing_period"]["statement_end"] = m.group(2)
        m = re.search(r"帳單結帳日\s*\n+\s*(\d{4}/\d{1,2}/\d{1,2})", text)
        if m:
            out["billing_period"]["statement_date"] = m.group(1)
        m = re.search(r"繳款截止日\s*\n+\s*(\d{4}/\d{1,2}/\d{1,2})", text)
        if m:
            out["billing_period"]["pay_due_date"] = m.group(1)
        # 本期帳單金額（已在 summary 抓過，這裡再抓一次走 billing_period.bill_amount）
        m = re.search(r"本期帳單金額\s*\n+\s*新臺幣\s*([\d,]+(?:\.\d+)?)", text)
        if m:
            out["billing_period"]["bill_amount"] = float(m.group(1).replace(",", ""))
        m = re.search(r"最低應繳金額\s*\n+\s*新臺幣\s*([\d,]+(?:\.\d+)?)", text)
        if m:
            out["billing_period"]["min_pay"] = float(m.group(1).replace(",", ""))

        # ─── [3] 上期實繳金額明細表（billed / payment section） ───
        # 5-column: 消費日期 / 入帳起息日 / 消費明細 / 約定幣別 / 消費金額
        #
        # 2026-07-03 修正 (0.3.64): 舊 regex 用「消費金額」當 anchor + 「沒有更多資料了」
        # 收尾，但當上期實繳查無資料時 regex 會貪婪抓到下方「當期消費明細」的
        # rows，把當期消費（如捷運 40 元）誤當扣繳紀錄。
        #
        # 修法：改用 section partition。
        #   text 內三個段落順序：上期實繳金額明細 → 當期消費明細 → 當期帳單說明
        # 只 parse「上期實繳金額明細」到「當期消費明細」中間的區塊；
        # 若該區塊包含「查無資料」，直接視為空 → billed_txns 不含此頁 row。
        if "上期實繳金額明細" in text:
            after_paid = text.split("上期實繳金額明細", 1)[1]
            paid_block, _sep, _rest = after_paid.partition("當期消費明細")
            if "查無資料" not in paid_block:
                # 只抓真實 rows，格式：yyyy/MM/dd 開頭
                entries = re.findall(
                    r"(\d{4}/\d{1,2}/\d{1,2})\s*\n\s*\t?\s*\n?\s*"
                    r"(\d{4}/\d{1,2}/\d{1,2})\s*\n\s*\t?\s*\n?\s*"
                    r"([^\n]+?)\s*\n\s*\t?\s*\n?\s*"
                    r"(新臺幣|美金|日圓|歐元|港幣|人民幣)\s*\n\s*\t?\s*\n?\s*"
                    r"(-?[\d,]+(?:\.\d+)?)",
                    paid_block,
                )
                for txn_date, post_date, desc, currency, amount in entries:
                    out["billed_txns"].append({
                        "txn_date": txn_date,
                        "post_date": post_date,
                        "desc": desc.strip(),
                        "currency": currency,
                        "amount": float(amount.replace(",", "")),
                        "card_no_suffix": card_no_suffix,
                    })

        # ─── [4] 即時消費紀錄表（pending） ───
        # 6-column: 消費日期 / 消費時間 / 消費明細 / 新臺幣消費金額 / 消費國別 / 授權結果
        # marker：「依消費日期排序」開始，「沒有更多資料了」結束
        # 但即時消費紀錄段在文件下方：「即時消費紀錄\n返回\nRichart卡(原...) (卡號末四碼:1409)\n下載\n列印\n依消費日期排序...」
        pending_section = re.search(
            r"依消費日期排序\s*\n\s*消費日期.*?授權結果\s*\n(.*?)\n\s*共\s*(\d+)\s*筆資料",
            text, re.DOTALL,
        )
        if pending_section:
            block = pending_section.group(1)
            # 每筆: 2026/05/21\n\t\n18:55:41\n\t\n台北大眾捷運股份有限公司\n\t\n1\n\t\nTW\n\t\n成功
            entries = re.findall(
                r"(\d{4}/\d{1,2}/\d{1,2})\s*\n\s*\t?\s*\n?\s*"
                r"(\d{1,2}:\d{2}:\d{2})\s*\n\s*\t?\s*\n?\s*"
                r"([^\n]+?)\s*\n\s*\t?\s*\n?\s*"
                r"(-?[\d,]+(?:\.\d+)?)\s*\n\s*\t?\s*\n?\s*"
                r"([A-Z]{2,3}|TW|JP|US|HK|CN|KR|SG|MY|TH|VN|UK|DE|FR|IT|ES|AU|NZ|CA)\s*\n\s*\t?\s*\n?\s*"
                r"([^\n]+?)\s*(?:\n|$)",
                block,
            )
            for txn_date, txn_time, desc, amount, country, status in entries:
                out["pending_txns"].append({
                    "txn_date": txn_date,
                    "txn_time": txn_time,
                    "desc": desc.strip(),
                    "amount": float(amount.replace(",", "")),
                    "country": country,
                    "status": status.strip(),
                    "card_no_suffix": card_no_suffix,
                })

        return out

    def _ocr_captcha(self, frame, max_attempts=5):
        """Read a six-digit CAPTCHA, refreshing natively at most four times."""
        self._ocr_failure_id = None
        attempts = min(max(max_attempts, 0), 5)
        for attempt in range(attempts):
            self._ocr_failure_id = None
            try:
                with error_step('TAISHIN-02-077-001'):
                    cap_b64 = frame.evaluate("""() => {
                    const inputs = [...document.querySelectorAll('input')];
                    const capInput = inputs.find(i => i.placeholder === '驗證碼');
                    if (!capInput) return null;
                    let parent = capInput.parentElement;
                    for (let i = 0; i < 5 && parent; i++) {
                        const img = parent.querySelector('img');
                        if (img && img.naturalWidth >= 80 && img.naturalWidth <= 250) {
                            const canvas = document.createElement('canvas');
                            canvas.width = img.naturalWidth;
                            canvas.height = img.naturalHeight;
                            canvas.getContext('2d').drawImage(img, 0, 0);
                            return canvas.toDataURL('image/png').split(',')[1];
                        }
                        parent = parent.parentElement;
                    }
                    return null;
                }""")
                with error_step('TAISHIN-02-078-001'):
                    text = (
                        ocr_bytes(
                            base64.b64decode(cap_b64, validate=True),
                            expected_len=6,
                            alnum_only=True,
                            min_confidence=0.98,
                        )
                        if cap_b64
                        else None
                    )
            except Exception as error:
                self._ocr_failure_id = exception_error_id(error, bank="taishin")
                return None
            if text and len(text) == 6 and text.isdigit():
                return text
            if attempt == attempts - 1:
                break
            try:
                with error_step('TAISHIN-02-079-001'):
                    candidates = frame.locator(
                        "[class*='refresh'], [class*='reload'], i.fa-sync, .icon-refresh"
                    )
                eligible = []
                with error_step('TAISHIN-02-080-001'):
                    for index in range(candidates.count()):
                        with error_step('TAISHIN-02-081-001'):
                            action = candidates.nth(index)
                        with error_step('TAISHIN-02-082-001'):
                            if error_step('TAISHIN-02-710-001')(lambda: action.is_visible())() and error_step('TAISHIN-02-711-001')(lambda: action.is_enabled())():
                                eligible.append(action)
                if len(eligible) != 1:
                    return None
                with error_step('TAISHIN-02-083-001'):
                    eligible[0].click()
                with error_step('TAISHIN-02-084-001'):
                    frame.wait_for_timeout(1500)
            except Exception as error:
                self._ocr_failure_id = exception_error_id(error, bank="taishin")
                return None
        return None

    def submit_credentials_once(self, page) -> None:
        try:
            frame = self._find_login_frame(page)
        except Exception:
            raise with_error_code(TaishinLoginError('無法安全確認登入頁面；未送出登入'), 'TAISHIN-02-001-001') from None
        if frame is None:
            raise with_error_code(TaishinLoginError('找不到登入頁面；未送出登入'), 'TAISHIN-02-002-001') from None

        try:
            for label in ("national_id", "user_code", "password"):
                value = getattr(self.creds, label)
                with error_step({'national_id': 'TAISHIN-02-032-001', 'user_code': 'TAISHIN-02-047-001', 'password': 'TAISHIN-02-062-001'}[label]):
                    candidates = frame.locator(_ph_sel(FIELD_PLACEHOLDERS[label]))
                with error_step({'national_id': 'TAISHIN-02-033-001', 'user_code': 'TAISHIN-02-048-001', 'password': 'TAISHIN-02-063-001'}[label]):
                    if candidates.count() != 1:
                        raise with_error_code(TaishinLoginError('登入欄位無法安全填寫；未送出登入'), {'national_id': 'TAISHIN-02-034-001', 'user_code': 'TAISHIN-02-049-001', 'password': 'TAISHIN-02-064-001'}[label])
                with error_step({'national_id': 'TAISHIN-02-035-001', 'user_code': 'TAISHIN-02-050-001', 'password': 'TAISHIN-02-065-001'}[label]):
                    field = candidates.nth(0)
                with error_step({'national_id': 'TAISHIN-02-036-001', 'user_code': 'TAISHIN-02-051-001', 'password': 'TAISHIN-02-066-001'}[label]):
                    with error_step({'national_id': 'TAISHIN-02-037-001', 'user_code': 'TAISHIN-02-052-001', 'password': 'TAISHIN-02-067-001'}[label]):
                        _error_condition = not field.is_visible()
                    if _error_condition:
                        raise with_error_code(TaishinLoginError('登入欄位無法安全填寫；未送出登入'), {'national_id': 'TAISHIN-02-038-001', 'user_code': 'TAISHIN-02-053-001', 'password': 'TAISHIN-02-068-001'}[label])
                    else:
                        with error_step({'national_id': 'TAISHIN-02-039-001', 'user_code': 'TAISHIN-02-054-001', 'password': 'TAISHIN-02-069-001'}[label]):
                            _error_condition = not field.is_enabled()
                        if _error_condition:
                            raise with_error_code(TaishinLoginError('登入欄位無法安全填寫；未送出登入'), {'national_id': 'TAISHIN-02-038-002', 'user_code': 'TAISHIN-02-053-002', 'password': 'TAISHIN-02-068-002'}[label])
                with error_step({'national_id': 'TAISHIN-02-040-001', 'user_code': 'TAISHIN-02-055-001', 'password': 'TAISHIN-02-070-001'}[label]):
                    field.click()
                with error_step({'national_id': 'TAISHIN-02-041-001', 'user_code': 'TAISHIN-02-056-001', 'password': 'TAISHIN-02-071-001'}[label]):
                    field.click(click_count=3)
                with error_step({'national_id': 'TAISHIN-02-042-001', 'user_code': 'TAISHIN-02-057-001', 'password': 'TAISHIN-02-072-001'}[label]):
                    page.keyboard.press("Backspace")
                with error_step({'national_id': 'TAISHIN-02-043-001', 'user_code': 'TAISHIN-02-058-001', 'password': 'TAISHIN-02-073-001'}[label]):
                    page.keyboard.type(value, delay=80)
                with error_step({'national_id': 'TAISHIN-02-044-001', 'user_code': 'TAISHIN-02-059-001', 'password': 'TAISHIN-02-074-001'}[label]):
                    page.wait_for_timeout(200)
                with error_step({'national_id': 'TAISHIN-02-045-001', 'user_code': 'TAISHIN-02-060-001', 'password': 'TAISHIN-02-075-001'}[label]):
                    if len(field.input_value()) != len(value):
                        raise with_error_code(TaishinLoginError('登入欄位輸入長度不符；未送出登入'), {'national_id': 'TAISHIN-02-046-001', 'user_code': 'TAISHIN-02-061-001', 'password': 'TAISHIN-02-076-001'}[label])
        except TaishinLoginError:
            raise
        except Exception as error:
            raise with_error_code(TaishinLoginError('登入欄位無法安全填寫；未送出登入'), exception_error_id(error, bank="taishin") or 'TAISHIN-02-003-001') from None

        self._ocr_failure_id = None
        try:
            captcha = self._ocr_captcha(frame, max_attempts=1)
        finally:
            ocr_failure_id = self._ocr_failure_id
            self._ocr_failure_id = None
        if not captcha:
            raise with_error_code(captcha_unreadable(TaishinLoginError('圖形驗證碼 OCR 失敗；未送出登入')), ocr_failure_id or 'TAISHIN-02-004-001')
        elif len(captcha) != 6:
            raise with_error_code(captcha_unreadable(TaishinLoginError('圖形驗證碼 OCR 失敗；未送出登入')), 'TAISHIN-02-004-002')
        elif not captcha.isdigit():
            raise with_error_code(captcha_unreadable(TaishinLoginError('圖形驗證碼 OCR 失敗；未送出登入')), 'TAISHIN-02-004-003')
        try:
            with error_step('TAISHIN-02-005-001'):
                candidates = frame.locator(_ph_sel(FIELD_PLACEHOLDERS["captcha"]))
            with error_step('TAISHIN-02-006-001'):
                if candidates.count() != 1:
                    raise with_error_code(TaishinLoginError('驗證碼欄位無法安全填寫；未送出登入'), 'TAISHIN-02-007-001')
            with error_step('TAISHIN-02-008-001'):
                field = candidates.nth(0)
            with error_step('TAISHIN-02-009-001'):
                with error_step('TAISHIN-02-010-001'):
                    _error_condition = not field.is_visible()
                if _error_condition:
                    raise with_error_code(TaishinLoginError('驗證碼欄位無法安全填寫；未送出登入'), 'TAISHIN-02-011-001')
                else:
                    with error_step('TAISHIN-02-012-001'):
                        _error_condition = not field.is_enabled()
                    if _error_condition:
                        raise with_error_code(TaishinLoginError('驗證碼欄位無法安全填寫；未送出登入'), 'TAISHIN-02-011-002')
            with error_step('TAISHIN-02-013-001'):
                field.click()
            with error_step('TAISHIN-02-014-001'):
                field.click(click_count=3)
            with error_step('TAISHIN-02-015-001'):
                page.keyboard.press("Backspace")
            with error_step('TAISHIN-02-016-001'):
                page.keyboard.type(captcha, delay=80)
            with error_step('TAISHIN-02-017-001'):
                page.wait_for_timeout(300)
            with error_step('TAISHIN-02-018-001'):
                if len(field.input_value()) != 6:
                    raise with_error_code(TaishinLoginError('驗證碼欄位輸入長度不符；未送出登入'), 'TAISHIN-02-019-001')
        except TaishinLoginError:
            raise
        except Exception as error:
            raise with_error_code(TaishinLoginError('驗證碼欄位無法安全填寫；未送出登入'), exception_error_id(error, bank="taishin") or 'TAISHIN-02-020-001') from None

        try:
            with error_step('TAISHIN-02-021-001'):
                candidates = frame.locator(f"#{LOGIN_BTN_ID}")
            with error_step('TAISHIN-02-022-001'):
                if candidates.count() != 1:
                    raise with_error_code(TaishinLoginError('找不到唯一且可操作的登入按鈕；未送出登入'), 'TAISHIN-02-023-001')
            with error_step('TAISHIN-02-024-001'):
                button = candidates.nth(0)
            with error_step('TAISHIN-02-025-001'):
                with error_step('TAISHIN-02-026-001'):
                    _error_condition = not button.is_visible()
                if _error_condition:
                    raise with_error_code(TaishinLoginError('找不到唯一且可操作的登入按鈕；未送出登入'), 'TAISHIN-02-027-001')
                else:
                    with error_step('TAISHIN-02-028-001'):
                        _error_condition = not button.is_enabled()
                    if _error_condition:
                        raise with_error_code(TaishinLoginError('找不到唯一且可操作的登入按鈕；未送出登入'), 'TAISHIN-02-027-002')
        except TaishinLoginError:
            raise
        except Exception as error:
            raise with_error_code(TaishinLoginError('無法安全確認登入按鈕；未送出登入'), exception_error_id(error, bank="taishin") or 'TAISHIN-02-029-001') from None
        try:
            with error_step('TAISHIN-02-030-001'):
                button.click(timeout=8000)
        except Exception as error:
            raise with_error_code(TaishinLoginError('登入送出狀態不明；禁止自動重試'), exception_error_id(error, bank="taishin") or 'TAISHIN-02-031-001') from None

        try:
            with error_step('TAISHIN-03-001-001'):
                page.wait_for_timeout(10000)
            for _ in range(30):
                with error_step('TAISHIN-03-002-001'):
                    page.wait_for_timeout(1000)
                if self._logged_in(page):
                    return
                scopes = [
                    page,
                    *(child for child in page.frames if child is not page.main_frame),
                ]
                for scope in scopes:
                    for selector in (".modal.show", "[role='dialog']"):
                        with error_step({'.modal.show': 'TAISHIN-03-003-002', "[role='dialog']": 'TAISHIN-03-003-003'}[selector]):
                            checkpoints = scope.locator(selector)
                        with error_step('TAISHIN-03-004-001'):
                            if any(
                                error_step({'.modal.show': 'TAISHIN-03-712-002', "[role='dialog']": 'TAISHIN-03-712-003'}[selector])(lambda: error_step({'.modal.show': 'TAISHIN-03-713-002', "[role='dialog']": 'TAISHIN-03-713-003'}[selector])(lambda: checkpoints.nth(index))().is_visible())()
                                for index in range(error_step({'.modal.show': 'TAISHIN-03-714-002', "[role='dialog']": 'TAISHIN-03-714-003'}[selector])(lambda: checkpoints.count())())
                            ):
                                return
        except Exception as _action_error:
            _log(format_error_id(exception_error_id(_action_error, bank="taishin") or 'TAISHIN-03-005-001', bank="taishin"))
            return

    @staticmethod
    def _overview_accounts(page, *, _failure=None) -> list[dict]:
        """Deposit rows from the overview table: 帳號別名 | 帳號 | 利率 | 累計利息 | 帳戶餘額."""
        js = """() => [...document.querySelectorAll('tr')].map(tr =>
            [...tr.querySelectorAll('td')].map(td => (td.innerText || '').trim()))"""
        seen, out = set(), []
        for f in page.frames:
            try:
                with error_step('TAISHIN-04-001-001'):
                    rows = f.evaluate(js) or []
            except Exception as _action_error:
                if _failure is not None:
                    _failure[:] = [exception_error_id(_action_error, bank="taishin") or 'TAISHIN-04-002-001']
                _log(format_error_id(exception_error_id(_action_error, bank="taishin") or 'TAISHIN-04-002-001', bank="taishin"))
                continue
            for cells in rows:
                hit = [i for i, c in enumerate(cells) if re.fullmatch(r"\d{4}-\d{2}-\d{7}-\d", c.split("\n")[0])]
                if len(hit) != 1 or len(cells) < hit[0] + 4:
                    continue
                i = hit[0]
                first, *rest = cells[i].split("\n")
                acct = first.replace("-", "")
                bal = cells[i + 3].replace(",", "")
                if acct in seen or not re.fullmatch(r"-?\d+(?:\.\d+)?", bal):
                    continue
                seen.add(acct)
                out.append({"accountNo": acct, "balance": bal,
                            "accountTypeName": rest[0] if rest else None,
                            "userdefineName": None if cells[i - 1] in ("", "未設定") else cells[i - 1]})
        return out

    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        """台新 collect — C 策略：popup 強行 hide → 直接點 top nav「信用卡」。

        2026-06-11 實測揭示：台新主畫面 top nav 在主 page（非 iframe）有 9 個 menu,
        「信用卡」是第 5 個，約 x:1390/2160 寬。直接從 DOM 找元素點，不依賴座標猜測。
        """
        out: dict = {}
        with error_step('TAISHIN-04-003-001'):
            page.wait_for_timeout(8000)
        # Live 2026-10-02: the rb0100 response body is not capturable, but the
        # landing overview table renders every deposit account; read it first.
        overview_failure = []
        dom_accounts = self._overview_accounts(page, _failure=overview_failure)
        if not dom_accounts:
            raise with_error_code(RuntimeError('taishin-accounts-empty'), overview_failure[0] if overview_failure else 'TAISHIN-04-004-001')

        # ── Step 2: 從所有 frames（含主 page）找 top nav「信用卡」DOM 元素並點 ──
        # 台新 SPA 整個介面在 svc/rwd iframe 內，top nav 也在裡面（不在主 page）
        clicked_credit_card = False
        target_frame = None
        target_info = None
        try:
            for f in [page] + [fr for fr in page.frames if fr != page.main_frame]:
                kind = "page" if f == page else "frame"
                try:
                    with error_step('TAISHIN-06-016-001'):
                        found = f.evaluate("""() => {
                        // 找 text='信用卡' 的元素，但更要找它的可點父鏈（a / button / [onclick] / [role=button]）
                        const all = document.querySelectorAll('a, button, [role="menuitem"], li, span, div');
                        const out = [];
                        for (const el of all) {
                            const t = (el.textContent || el.innerText || '').trim();
                            if (t !== '信用卡' && t !== '信用卡 ') continue;
                            if (el.offsetParent === null) continue;
                            const r = el.getBoundingClientRect();
                            if (r.width < 5 || r.height < 5) continue;
                            // 排除巢狀容器（內部還有 a/button → 點外層 onclick 可能無效）
                            if (['DIV', 'LI', 'SPAN'].includes(el.tagName)) {
                                if (el.querySelector('a, button')) continue;
                            }
                            // 找可點祖先（最近的 a/button/[onclick]/[role=button]）
                            let click_target = el;
                            let p = el;
                            for (let i = 0; i < 8 && p; i++) {
                                if (p.tagName === 'A' || p.tagName === 'BUTTON' ||
                                    p.onclick || p.getAttribute('role') === 'button' ||
                                    p.classList.contains('_nav_menu__item') ||
                                    (p.className && /menu|nav|item/i.test(p.className))) {
                                    click_target = p;
                                    break;
                                }
                                p = p.parentElement;
                            }
                            const ct_r = click_target.getBoundingClientRect();
                            out.push({
                                tag: el.tagName, x: r.x, y: r.y, w: r.width, h: r.height,
                                href: el.getAttribute('href') || '',
                                click_tag: click_target.tagName,
                                click_class: click_target.className || '',
                                click_x: ct_r.x, click_y: ct_r.y, click_w: ct_r.width, click_h: ct_r.height,
                                click_visible: window.getComputedStyle(click_target).display !== 'none',
                            });
                        }
                        return out;
                    }""")
                except Exception as _action_error:
                    _log(format_error_id(exception_error_id(_action_error, bank="taishin") or 'TAISHIN-07-008-001', bank="taishin"))
                    found = []
                if found:
                    _log(f"[taishin][collect] {kind} 信用卡候選數={len(found)}")
                    if target_info is None:
                        # 優先 top nav (y < 250) + visible click_target
                        top_nav = [n for n in found if n["y"] < 250 and n["click_visible"]]
                        target_info = top_nav[0] if top_nav else (found[0] if found else None)
                        target_frame = f
                        if target_info:
                            _log(f"[taishin][collect] 採用 {kind} 第一個 top-nav 候選")

            if target_info and target_frame:
                # 2026-06-11 (C 路徑教訓): 直接 click SPAN 文字無效 (toggle 'on' class 但無 routing)
                # 需逐層 click ancestors，找到真正能展開 dropdown / 跳頁的層級
                clicked_credit_card = self._try_ancestor_clicks(target_frame, page)
            else:
                _log("[taishin][collect] 全 frames 都找不到「信用卡」元素")
        except Exception as _action_error:
            _log(format_error_id(exception_error_id(_action_error, bank="taishin") or 'TAISHIN-07-009-001', bank="taishin"))
            _log("[taishin][collect] 找信用卡 nav 例外")

        # ── Step 3: 等信用卡頁載入 + 等 API call 跑完（10 秒） ──
        with error_step('TAISHIN-06-017-001'):
            page.wait_for_timeout(10000)

        # ── Step 4: dump 信用卡頁 frame text + 攔 API ──
        # 已經點過「查詢信用卡明細」（在 _try_ancestor_clicks 內），現在直接 dump
        credit_card_frame = None
        page_text = ""
        if clicked_credit_card:
            for f in page.frames:
                if f == page.main_frame:
                    continue
                try:
                    with error_step('TAISHIN-06-018-001'):
                        ct = f.evaluate("() => document.body.innerText.slice(0, 12000)")
                    if ct and ("信用卡" in ct or "帳單" in ct or "消費" in ct or "應繳" in ct):
                        credit_card_frame = f
                        page_text = ct
                        _log(f"[taishin][collect] 信用卡頁 frame text len={len(ct)}")
                        break
                except Exception as _action_error:
                    _log(format_error_id(exception_error_id(_action_error, bank="taishin") or 'TAISHIN-07-010-001', bank="taishin"))
                    pass

        # ── Step 4b: parse 信用卡頁 frame text 抽結構化資料 ──
        if page_text:
            try:
                parsed = self._parse_credit_card_page(page_text)

                # 2026-07-03 修正 (0.3.64): 台新繳款事實在「切到某月帳單頁後，
                # 該頁的『上期實繳金額明細』」，post_date = 該月帳單被扣繳日。
                # 例：切到 05 月頁 → 抓到 04/27 -56 (04 月帳單被扣)。
                # 舊碼三個 bug 一次修：
                #   (1) month_options[:12] 含 index=0 「其他月份」placeholder,
                #       select_option(index=0) 觸發空重載造成 rows 錯亂。
                #   (2) 順序不穩定 (log 有時 06→05→04, 有時反過來)。
                #   (3) parser 內 billed_txns 也含當期消費明細退款 row (負值 desc
                #       如「年費減免」「一卡通餘額退款」)，crawler filter 用
                #       「'信用卡' in desc」勉強擋掉但非明確 partition。這裡改成
                #       直接吃 parser 給的 billed_txns (parser 已 partition 到
                #       「上期實繳金額明細」section)。
                if credit_card_frame:
                    month_options = []
                    try:
                        with error_step('TAISHIN-06-019-001'):
                            month_options = credit_card_frame.evaluate("""() => {
                            const sels = [...document.querySelectorAll('select')];
                            const monthSel = sels.find(s => [...s.options].some(o => /^\\d{4}\\/\\d{2}$/.test((o.textContent || '').trim())));
                            if (!monthSel) return [];
                            return [...monthSel.options]
                                .map((o, index) => ({index, text: (o.textContent || '').trim()}))
                                .filter(o => /^\\d{4}\\/\\d{2}$/.test(o.text));
                        }""") or []
                    except Exception as _action_error:
                        _log(format_error_id(exception_error_id(_action_error, bank="taishin") or 'TAISHIN-07-011-001', bank="taishin"))
                        _log("[taishin][collect] 月份下拉 dump 失敗")
                    # 穩定按時間降序排 (2026/06, 2026/05, ...)，log 才不會神秘跳
                    month_options.sort(key=lambda o: o.get("text") or "", reverse=True)

                    seen_billed_keys = {
                        (r.get("txn_date"), r.get("post_date"), r.get("desc"), r.get("amount"))
                        for r in parsed.get("billed_txns", [])
                    }
                    for opt in month_options[:12]:
                        try:
                            with error_step('TAISHIN-06-020-001'):
                                credit_card_frame.evaluate("location.hash = '#/RB0708/0100?ts=' + Date.now()")
                            with error_step('TAISHIN-06-021-001'):
                                page.wait_for_timeout(4000)
                            for f in page.frames:
                                if "svc/rwd" in (f.url or ""):
                                    credit_card_frame = f
                                    break
                            with error_step('TAISHIN-06-022-001'):
                                error_step('TAISHIN-06-715-001')(lambda: error_step('TAISHIN-06-716-001')(lambda: error_step('TAISHIN-06-717-001')(lambda: credit_card_frame.locator("select"))().nth(1))().select_option(index=opt.get("index") or 0))()
                            with error_step('TAISHIN-06-023-001'):
                                page.wait_for_timeout(5000)
                            with error_step('TAISHIN-06-024-001'):
                                month_text = credit_card_frame.evaluate("() => document.body.innerText.slice(0, 50000)") or ""
                            month_parsed = self._parse_credit_card_page(month_text)
                            added = 0
                            for row in month_parsed.get("billed_txns", []):
                                key = (row.get("txn_date"), row.get("post_date"), row.get("desc"), row.get("amount"))
                                if key in seen_billed_keys:
                                    continue
                                parsed.setdefault("billed_txns", []).append(row)
                                seen_billed_keys.add(key)
                                # parser 已 partition 到「上期實繳金額明細」section，
                                # 所有負值 row 都是真扣繳。仍過濾 desc 有「扣繳/轉帳」
                                # 字樣保險。
                                desc = str(row.get("desc") or "")
                                amt = float(row.get("amount") or 0)
                                if amt < 0 and ("扣繳" in desc or "轉帳" in desc):
                                    added += 1
                            _log(f"[taishin][collect] 月份 {opt.get('text')} 補 payment rows={added}")
                        except Exception as _action_error:
                            _log(format_error_id(exception_error_id(_action_error, bank="taishin") or 'TAISHIN-07-012-001', bank="taishin"))
                            _log(f"[taishin][collect] 月份 {opt.get('text')} 查詢失敗")

                out["credit_card_parsed"] = parsed
                _log(f"[taishin][collect] parser 結果: "
                     f"cards={len(parsed.get('cards', []))} "
                     f"pending={len(parsed.get('pending_txns', []))} "
                     f"billed={len(parsed.get('billed_txns', []))} "
                     f"summary={'有' if parsed.get('summary') else '無'}")
            except Exception as _action_error:
                _log(format_error_id(exception_error_id(_action_error, bank="taishin") or 'TAISHIN-07-013-001', bank="taishin"))
                _log("[taishin][collect] parser 例外")
                out["credit_card_parsed"] = {"error": "parse_failed"}

        # ── Step 5: attested TWD transaction history ──
        out.update(self._collect_attested_twd_history(page, collector))

        # ── Step 6: retain only non-sensitive API responses needed by persistence ──
        hits_by_endpoint = self._non_sensitive_api_responses(collector.hits)
        if not (((hits_by_endpoint.get("query") or {}).get("OUTPUTDATA") or {}).get("SavingAccount")):
            hits_by_endpoint["query"] = {"OUTPUTDATA": {"SavingAccount": dom_accounts}}
        out["api_responses"] = hits_by_endpoint
        parsed = out.get("credit_card_parsed") or {}
        with error_step('TAISHIN-07-014-001'):
            publish_card_bill_facts(out, [_taishin_card_bill_fact(parsed)])
        _log(f"[taishin][collect] 攔到 {len(hits_by_endpoint)} 個 endpoint")
        return BankCollectResult(**out)


if __name__ == "__main__":
    crawler = TaishinCrawler()
    try:
        result = crawler.run(login_url=BASE, headless=True)
    except TaishinLoginError:
        result = {"error": "login_failed_stop"}

    out_file = Path(__file__).resolve().parents[1] / "data" / "taishin_collected.json"
    write_private_json(out_file, result)
    _log(f"\n[taishin][done] 已存: {out_file}")
    if result.get("error"):
        _log(f"  ❌ error: {result['error']}")
