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
        except (TypeError, ValueError):
            continue
        payment_date = card_bill_date(row.get("post_date") or row.get("txn_date"))
        payment_amount = card_bill_money(abs(amount))
        if amount < 0 and payment_date and payment_amount is not None:
            payment_candidates.append((payment_date, payment_amount))
    payment_date, payment_amount = max(payment_candidates, default=(None, None))
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
        if mode not in {"full", "incremental"}:
            raise ValueError(f"invalid BANK_CRAWLER_HISTORY_MODE: {mode!r}")
        cursor = self.transaction_start_for(identity, domain="twd_transactions")
        if mode == "full":
            return {
                "period": "12_months",
                "start": self._subtract_months(as_of, 12),
                "end": as_of,
            }
        if cursor is not None and cursor > as_of:
            raise RuntimeError("taishin-twd-history-cursor")
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
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("query_buttons") != 1
            or type(snapshot.get("query_button")) is not int
            or snapshot["query_button"] < 0
        ):
            raise RuntimeError(error)
        selects = snapshot.get("selects")
        if not isinstance(selects, list):
            raise RuntimeError(error)
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
            if (
                not isinstance(select, dict)
                or type(select.get("index")) is not int
                or select["index"] <= last_index
            ):
                raise RuntimeError(error)
            last_index = select["index"]
            options = select.get("options")
            if not isinstance(options, list) or any(
                not isinstance(option, dict) or option.get("index") != index
                for index, option in enumerate(options)
            ):
                raise RuntimeError(error)
            pairs = [(option.get("text"), option.get("value")) for option in options]
            if pairs == expected_periods:
                period_selects.append(select["index"])
                continue
            if pairs == expected_sort:
                sort_selects.append(select["index"])
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
            accounts, identities, values = [], set(), set()
            for option in options[1:]:
                text, value = option.get("text"), option.get("value")
                digits = re.findall(r"(?<!\d)\d(?:[\d-]*\d)?(?!\d)", text or "")
                matching = (
                    [re.sub(r"\D", "", token) for token in digits
                     if re.sub(r"\D", "", token) == value]
                    if isinstance(value, str)
                    else []
                )
                identity = matching[0] if len(matching) == 1 else ""
                if (
                    not isinstance(value, str)
                    or not re.fullmatch(r"\d{12,14}", value)
                    or identity != value
                    or identity in identities
                    or value in values
                ):
                    accounts = []
                    break
                identities.add(identity)
                values.add(value)
                accounts.append({"index": option["index"], "identity": identity, "value": value})
            if (
                accounts
                and first.get("value") == ""
                and "請選擇" in str(first.get("text") or "")
            ):
                account_selects.append((select["index"], accounts))
        if len(period_selects) != 1 or len(sort_selects) != 1 or len(account_selects) != 1:
            raise RuntimeError(error)
        account_select, accounts = account_selects[0]
        return {
            "account_select": account_select,
            "period_select": period_selects[0],
            "sort_select": sort_selects[0],
            "query_button": snapshot["query_button"],
            "accounts": accounts,
        }

    @classmethod
    def _require_history_inventory(
        cls,
        snapshot: dict,
        expected: list[tuple[str, str]],
    ) -> dict:
        form = cls._validate_history_form(snapshot)
        actual = [(item["identity"], item["value"]) for item in form["accounts"]]
        if actual != expected:
            raise RuntimeError("taishin-twd-history-inventory")
        return form

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
            page.wait_for_timeout(500)
            if self._history_frame(page) is not submitted_frame:
                raise RuntimeError("taishin-twd-history-frame")
            matching = [
                hit for hit in collector.hits
                if hit.request_sequence > boundary
                and urlparse(hit.raw_url or hit.url).path
                == "/TIBNetBank/svc/web1/rb0102/query"
            ]
            if (
                collector.request_sequence != request_sequence
                or collector.issued_count("query") != issued_count
                or len(matching) != 1
            ):
                raise RuntimeError("taishin-twd-history-response")
        return matching[0]

    @staticmethod
    def _history_money(value: object, *, allow_dash: bool = False) -> int | None:
        if allow_dash and value == "-":
            return None
        if not isinstance(value, str) or not re.fullmatch(
            r"-?(?:0|[1-9]\d{0,9}|[1-9]\d{0,2}(?:,\d{3}){1,3})",
            value,
        ):
            raise RuntimeError("taishin-twd-history-dom")
        parsed = int(value.replace(",", ""))
        if abs(parsed) > 2_147_483_647:
            raise RuntimeError("taishin-twd-history-dom")
        return parsed

    @staticmethod
    def _add_history_response_bytes(total: int, body_size: int) -> int:
        if (
            type(total) is not int
            or type(body_size) is not int
            or total < 0
            or body_size <= 0
            or total + body_size > 5_000_000
        ):
            raise RuntimeError("taishin-twd-history-response-budget")
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
            raise RuntimeError("taishin-twd-history-binding") from None
        if len(encoded) > 5_000_000:
            raise RuntimeError("taishin-twd-history-response-budget")
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
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise RuntimeError("taishin-api-projection")
            raw = str(value).strip()
            if re.fullmatch(r"-?(?:\d{1,12}|[1-9]\d{0,2}(?:,\d{3}){1,3})", raw) is None:
                raise RuntimeError("taishin-api-projection")
            parsed = int(raw.replace(",", ""))
            if abs(parsed) > 2_147_483_647:
                raise RuntimeError("taishin-api-projection")
            return parsed

        def text(value: object, limit: int, *, empty: bool = False) -> str:
            if not isinstance(value, str) or len(value) > limit or (not empty and not value):
                raise RuntimeError("taishin-api-projection")
            if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
                raise RuntimeError("taishin-api-projection")
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
                            raise RuntimeError("taishin-api-projection")
                        account_no = text(account.get("accountNo"), 14)
                        if not re.fullmatch(r"\d{12,14}", account_no) or account_no in identities:
                            raise RuntimeError("taishin-api-projection")
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
            except RuntimeError:
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
        if (
            len(row) != 7
            or not all(isinstance(value, str) for value in row)
            or any(len(value) > limit for value, limit in zip(row, limits, strict=True))
            or any(
                ord(char) < 32 and char not in "\t\n\r"
                for value in row for char in value
            )
        ):
            raise RuntimeError(error)
        row = [re.sub(r"\s+", " ", value).strip() for value in row]
        try:
            txn_at = datetime.strptime(row[0], "%Y/%m/%d %H:%M:%S")
            account_day = datetime.strptime(row[1], "%Y/%m/%d").date()
        except ValueError:
            raise RuntimeError(error) from None
        if (
            not start <= txn_at.date() <= end
            or not start <= account_day <= end
            or abs((account_day - txn_at.date()).days) > 7
        ):
            raise RuntimeError(error)
        description, memo = row[2], row[5]
        if not description:
            raise RuntimeError(error)
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
        if (
            not isinstance(transport, dict)
            or set(transport) != keys
            or not isinstance(transport.get("url"), str)
            or not isinstance(transport.get("request_frame_url"), str)
        ):
            raise RuntimeError(error)
        try:
            parsed = urlparse(transport["url"])
            frame_url = urlparse(transport["request_frame_url"])
        except ValueError:
            raise RuntimeError(error) from None
        if (
            parsed.scheme != "https"
            or parsed.hostname != "my.taishinbank.com.tw"
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != "/TIBNetBank/svc/web1/rb0102/query"
            or parsed.query
            or parsed.fragment
            or transport.get("method") != "POST"
            or transport.get("status") != 200
            or transport.get("content_type") != "application/json"
            or transport.get("redirected") is not False
            or transport.get("main_frame_request") is not False
            or frame_url.scheme != "https"
            or frame_url.hostname != "my.taishinbank.com.tw"
            or frame_url.port not in (None, 443)
            or frame_url.username is not None
            or frame_url.password is not None
            or frame_url.path not in {
                "/TIBNetBank/svc/rwd/", "/TIBNetBank/svc/rwd/index.html",
            }
            or frame_url.query
            or re.fullmatch(r"/RB0102/0100(?:\?ts=\d+)?", frame_url.fragment) is None
            or transport.get("request_body") != {
                "account": identity,
                "start": start.strftime("%Y%m%d"),
                "end": end.strftime("%Y%m%d"),
            }
            or transport.get("response_result") != "NORMAL"
            or type(transport.get("body_size")) is not int
            or not 0 < transport["body_size"] <= 5_000_000
            or type(transport.get("request_sequence")) is not int
            or transport["request_sequence"] <= 0
        ):
            raise RuntimeError(error)
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
        except RuntimeError:
            raise RuntimeError(error) from None
        expected_frame = _OriginGuardProxy._unwrap(expected_frame)
        if (
            hit.url != transport["url"]
            or hit.request_sequence <= boundary
            or (expected_frame is not None and hit.request_frame is not expected_frame)
            or not isinstance(response, dict)
            or response.get("RESULT") != "NORMAL"
            or not isinstance(output, dict)
            or not isinstance(rows, list)
            or len(rows) > 10_000
            or any(not isinstance(row, dict) or not required <= set(row) for row in rows)
        ):
            raise RuntimeError(error)
        normalized = []
        for row in rows:
            if (
                any(
                    not isinstance(row[key], str)
                    for key in ("sysdate", "dateNew", "memo", "txnamtOut", "txnamtIn")
                )
                or not isinstance(row["message"], (str, type(None)))
                or isinstance(row["txnamt"], bool)
                or not isinstance(row["txnamt"], (str, int))
                or isinstance(row["newbal"], bool)
                or not isinstance(row["newbal"], (str, int, type(None)))
            ):
                raise RuntimeError(error)
            stamp = re.fullmatch(r"(\d{8})\s?(\d{1,8})", row["sysdate"])
            account_day = re.fullmatch(r"\d{8}", row["dateNew"])
            description = row["memo"]
            note = row["message"] or ""
            incoming = row["txnamtIn"] != "-"
            outgoing = row["txnamtOut"] != "-"
            if (
                stamp is None
                or account_day is None
                or (incoming and outgoing)
            ):
                raise RuntimeError(error)
            time_digits = stamp[2].zfill(8)
            try:
                amount = None
                if incoming or outgoing:
                    amount = cls._history_money(str(row["txnamt"]))
                    direction_amount = cls._history_money(
                        row["txnamtIn"] if incoming else row["txnamtOut"],
                    )
                    if (
                        direction_amount is None
                        or direction_amount <= 0
                        or amount is None
                        or direction_amount != abs(amount)
                    ):
                        raise RuntimeError(error)
                elif row["txnamt"] != "-" and cls._history_money(str(row["txnamt"])) != 0:
                    raise RuntimeError(error)
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
            except (RuntimeError, TypeError, ValueError):
                raise RuntimeError(error) from None
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
        if (
            not isinstance(snapshot, dict)
            or set(snapshot) != keys
            or snapshot.get("evidence_fresh") is not True
            or snapshot.get("route_bound") is not True
            or snapshot.get("result_scope_bound") is not True
            or snapshot.get("selected_identity") != identity
            or snapshot.get("selected_period") != period
            or snapshot.get("selected_sort") != "forward"
            or any(
                type(snapshot.get(key)) is not int or snapshot[key] < 0
                for key in (
                    "mutation_count", "quiet_ms", "busy_count", "dialog_count",
                    "error_count", "table_count",
                    "total_count", "more_button_count", "no_more_count", "pager_count",
                    "no_result_count",
                )
            )
            or snapshot["busy_count"] != 0
            or snapshot["dialog_count"] != 0
            or snapshot["error_count"] != 0
            or snapshot["pager_count"] != 0
            or snapshot["mutation_count"] <= 0
            or snapshot["quiet_ms"] < 1500
            or type(api_row_count) is not int
            or api_row_count < 0
            or not isinstance(snapshot.get("headers"), list)
            or not isinstance(snapshot.get("rows"), list)
        ):
            raise RuntimeError(error)
        if api_row_count == 0:
            if (
                snapshot["table_count"] != 0
                or snapshot["headers"]
                or snapshot["rows"]
                or snapshot["total_count"] != 0
                or snapshot["more_button_count"] != 0
                or snapshot["no_more_count"] != 0
                or snapshot["no_result_count"] != 1
            ):
                raise RuntimeError(error)
            return {"status": "explicit_empty", "rows": []}
        if (
            snapshot["table_count"] != 1
            or snapshot["headers"] != ["交易日", "帳務日", "摘要", "金額", "餘額", "備註", ""]
            or len(snapshot["rows"]) != api_row_count
            or snapshot["total_count"] != api_row_count
            or snapshot["more_button_count"] != 0
            or snapshot["no_more_count"] != 1
            or snapshot["no_result_count"] != 0
        ):
            raise RuntimeError(error)

        normalized = []
        for row in snapshot["rows"]:
            if not isinstance(row, list) or len(row) != 7 or not all(isinstance(v, str) for v in row):
                raise RuntimeError(error)
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
                raise RuntimeError(error) from None
            if bound_dom != bound_api:
                raise RuntimeError(error)
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
            raise RuntimeError("taishin-twd-history-frame")
        return matches[0]

    @staticmethod
    def _history_form_snapshot(frame) -> dict:
        return frame.evaluate(r"""() => {
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
                    options: [...item.select.options].map((option, optionIndex) => ({
                        index: optionIndex,
                        text: (option.textContent || '').replace(/\s+/g, ' ').trim(),
                        value: option.value || '',
                    })),
                })),
            };
        }""")

    @staticmethod
    def _history_result_snapshot(frame) -> dict:
        return frame.evaluate(r"""() => {
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
            const selects = [...document.querySelectorAll('select')].filter(visible);
            const byOptions = labels => selects.find(select => {
                const texts = [...select.options].map(option => norm(option.textContent));
                return labels.every(label => texts.includes(label));
            });
            const accountSelect = selects.find(select =>
                [...select.options].some(option => /^\d(?:[\d-]*\d)?(?:\s|$)/.test(norm(option.textContent)))
            );
            const periodSelect = byOptions(['7天', '12個月', '申請查詢逾一年以上']);
            const sortSelect = byOptions(['由新到舊', '由舊到新']);
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
                selected_identity: accountSelect ? accountSelect.value : '',
                selected_period: periodSelect ? periodSelect.value : '',
                selected_sort: sortSelect ? sortSelect.value : '',
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

    def _collect_attested_twd_history(
        self,
        page,
        collector: ResponseCollector,
        *,
        as_of: date | None = None,
    ) -> dict:
        as_of = as_of or datetime.now(ZoneInfo("Asia/Taipei")).date()
        mode = os.environ.get("BANK_CRAWLER_HISTORY_MODE", "full")
        if mode not in {"full", "incremental"}:
            raise ValueError(f"invalid BANK_CRAWLER_HISTORY_MODE: {mode!r}")
        frame = self._history_frame(page)
        frame.evaluate("location.hash = '#/RB0102/0100?ts=' + Date.now()")
        form = None
        inventory_signature = None
        inventory_stable_count = 0
        for _ in range(60):
            page.wait_for_timeout(500)
            frame = self._history_frame(page)
            try:
                current_form = self._validate_history_form(self._history_form_snapshot(frame))
            except RuntimeError:
                inventory_signature = None
                inventory_stable_count = 0
                continue
            current_signature = [
                (item["identity"], item["value"]) for item in current_form["accounts"]
            ]
            if current_signature == inventory_signature:
                inventory_stable_count += 1
            else:
                inventory_signature = current_signature
                inventory_stable_count = 0
            form = current_form
            if inventory_stable_count >= 2 and current_signature:
                break
        if form is None or inventory_stable_count < 2:
            raise RuntimeError("taishin-twd-history-form")
        inventory = form["accounts"]
        if not inventory:
            state = self._history_result_snapshot(frame)
            if (
                state.get("route_bound") is not True
                or any(
                    type(state.get(key)) is not int or state[key] != 0
                    for key in (
                        "busy_count", "dialog_count", "error_count", "table_count",
                        "no_result_count", "more_button_count", "no_more_count", "pager_count",
                    )
                )
                or getattr(self, "_shared_dialog_blocked", False)
            ):
                raise RuntimeError("taishin-twd-history-empty-inventory")
        results, expected, receipts = [], [], []
        response_bytes = 0
        inventory_signature = [(item["identity"], item["value"]) for item in inventory]

        for position, account in enumerate(inventory):
            if position:
                frame.evaluate("location.hash = '#/RB0102/0100?ts=' + Date.now()")
                for _ in range(60):
                    page.wait_for_timeout(500)
                    frame = self._history_frame(page)
                    try:
                        form = self._validate_history_form(self._history_form_snapshot(frame))
                    except RuntimeError:
                        continue
                    if [(item["identity"], item["value"]) for item in form["accounts"]] == inventory_signature:
                        break
                else:
                    raise RuntimeError("taishin-twd-history-inventory")
            window = self._history_window(account["identity"], as_of)
            selects = frame.locator("select")
            selects.nth(form["account_select"]).select_option(value=account["value"])
            selects.nth(form["period_select"]).select_option(value=window["period"])
            selects.nth(form["sort_select"]).select_option(value="forward")
            selected = frame.evaluate("""indices => indices.map(index =>
                [...document.querySelectorAll('select')][index].value
            )""", [form["account_select"], form["period_select"], form["sort_select"]])
            if selected != [account["value"], window["period"], "forward"]:
                raise RuntimeError("taishin-twd-history-selection")

            before_result = self._history_result_snapshot(frame)
            if (
                before_result["table_count"] != 0
                or before_result["no_result_count"] != 0
                or before_result["busy_count"] != 0
                or before_result["dialog_count"] != 0
                or before_result["error_count"] != 0
            ):
                raise RuntimeError("taishin-twd-history-stale-before-query")
            frame.evaluate("""() => {
                window.__thothTaishinHistoryObserver?.disconnect();
                const state = {count: 0, last: 0};
                window.__thothTaishinHistoryMutation = state;
                window.__thothTaishinHistoryObserver = new MutationObserver(records => {
                    state.count += records.length;
                    state.last = Date.now();
                });
                window.__thothTaishinHistoryObserver.observe(document.body, {
                    childList: true, subtree: true, characterData: true,
                });
            }""")
            boundary = collector.request_sequence
            issued_before = collector.issued_count("query")
            query = frame.locator("input[value='查詢']")
            if query.count() <= form["query_button"]:
                raise RuntimeError("taishin-twd-history-query")
            submitted_frame = frame
            query.nth(form["query_button"]).click(timeout=8000)
            matching = []
            for _ in range(120):
                page.wait_for_timeout(500)
                matching = [
                    hit for hit in collector.hits
                    if hit.request_sequence > boundary
                    and urlparse(hit.raw_url or hit.url).path == "/TIBNetBank/svc/web1/rb0102/query"
                ]
                if matching:
                    break
            if len(matching) != 1:
                raise RuntimeError("taishin-twd-history-response")
            response = self._validate_history_hit(
                matching[0], identity=account["identity"], start=window["start"],
                end=window["end"], boundary=boundary, expected_frame=submitted_frame,
            )
            body_size = matching[0].body_size
            if type(body_size) is not int:
                raise RuntimeError("taishin-twd-history-response-budget")
            response_bytes = self._add_history_response_bytes(response_bytes, body_size)

            frame = self._history_frame(page)
            if frame is not submitted_frame:
                raise RuntimeError("taishin-twd-history-frame")
            for _ in range(min(response["row_count"] + 2, 10_002)):
                if self._history_frame(page) is not submitted_frame:
                    raise RuntimeError("taishin-twd-history-frame")
                snapshot = self._history_result_snapshot(frame)
                if snapshot["more_button_count"] == 0:
                    break
                before = len(snapshot["rows"])
                buttons = frame.locator("._table_more__btn:visible")
                if buttons.count() != 1:
                    raise RuntimeError("taishin-twd-history-pagination")
                buttons.nth(0).click(timeout=5000)
                page.wait_for_timeout(300)
                if len(self._history_result_snapshot(frame)["rows"]) <= before:
                    raise RuntimeError("taishin-twd-history-pagination")
            else:
                raise RuntimeError("taishin-twd-history-pagination")

            stable = None
            stable_count = 0
            for _ in range(60):
                page.wait_for_timeout(500)
                frame = self._history_frame(page)
                if frame is not submitted_frame:
                    raise RuntimeError("taishin-twd-history-frame")
                snapshot = self._history_result_snapshot(frame)
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
                raise RuntimeError("taishin-twd-history-settle")
            if self._shared_dialog_blocked:
                raise RuntimeError("taishin-twd-history-dialog")
            matching = [
                hit for hit in collector.hits
                if hit.request_sequence > boundary
                and urlparse(hit.raw_url or hit.url).path == "/TIBNetBank/svc/web1/rb0102/query"
            ]
            if len(matching) != 1 or collector.issued_count("query") != issued_before + 1:
                raise RuntimeError("taishin-twd-history-response")
            quiescent_hit = self._require_history_network_quiescence(
                page,
                collector,
                submitted_frame=submitted_frame,
                boundary=boundary,
                request_sequence=collector.request_sequence,
                issued_count=issued_before + 1,
            )
            if quiescent_hit is not matching[0]:
                raise RuntimeError("taishin-twd-history-response")
            snapshot = self._history_result_snapshot(submitted_frame)
            snapshot["evidence_fresh"] = True
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
                login_fields = scope.locator("input[placeholder='身分證字號']")
                if any(
                    login_fields.nth(index).is_visible()
                    for index in range(login_fields.count())
                ):
                    return False
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
        except Exception:
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
            for fel in page.query_selector_all("iframe"):
                src = fel.get_attribute("src") or ""
                if "svc/rwd" in src:
                    fbox = fel.bounding_box()
                    if fbox:
                        iframe_offset_x = fbox["x"]
                        iframe_offset_y = fbox["y"]
                        _log(f"[taishin][collect] iframe offset: ({int(iframe_offset_x)},{int(iframe_offset_y)})")
                    break
        except Exception:
            _log("[taishin][collect] 取 iframe offset 失敗（用 0,0）")

        # Step 3: 真實 mouse.move 到 LI 中心
        li_center_x = iframe_offset_x + li_bbox["x"] + li_bbox["w"] / 2
        li_center_y = iframe_offset_y + li_bbox["y"] + li_bbox["h"] / 2
        _log(f"[taishin][collect] mouse.move → page coord ({li_center_x:.0f},{li_center_y:.0f})")

        try:
            # 先 move 到主畫面中央（避開所有 nav）→ 再 move 到 LI（保證觸發 mouseenter）
            page.mouse.move(li_center_x, 400)
            page.wait_for_timeout(200)
            page.mouse.move(li_center_x, li_center_y, steps=10)
            page.wait_for_timeout(1500)  # 等 mega menu 展開動畫
        except Exception:
            _log("[taishin][collect] mouse.move 例外")
            return False

        # Step 4: dump mega menu 內可見 link（hover 還在，menu 還展開）
        try:
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
        except Exception:
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
            page.mouse.move(click_x, click_y, steps=5)
            page.wait_for_timeout(300)
            page.mouse.click(click_x, click_y)
            _log(f"[taishin][collect] page.mouse.click({click_x:.0f},{click_y:.0f})")
            page.wait_for_timeout(6000)
            return True
        except Exception:
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
        attempts = min(max(max_attempts, 0), 5)
        for attempt in range(attempts):
            try:
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
            except Exception:
                return None
            if text and len(text) == 6 and text.isdigit():
                return text
            if attempt == attempts - 1:
                break
            try:
                candidates = frame.locator(
                    "[class*='refresh'], [class*='reload'], i.fa-sync, .icon-refresh"
                )
                eligible = []
                for index in range(candidates.count()):
                    action = candidates.nth(index)
                    if action.is_visible() and action.is_enabled():
                        eligible.append(action)
                if len(eligible) != 1:
                    return None
                eligible[0].click()
                frame.wait_for_timeout(1500)
            except Exception:
                return None
        return None

    def submit_credentials_once(self, page) -> None:
        try:
            frame = self._find_login_frame(page)
        except Exception:
            raise TaishinLoginError("無法安全確認登入頁面；未送出登入") from None
        if frame is None:
            raise TaishinLoginError("找不到登入頁面；未送出登入") from None

        try:
            for label in ("national_id", "user_code", "password"):
                value = getattr(self.creds, label)
                candidates = frame.locator(_ph_sel(FIELD_PLACEHOLDERS[label]))
                if candidates.count() != 1:
                    raise TaishinLoginError("登入欄位無法安全填寫；未送出登入")
                field = candidates.nth(0)
                if not field.is_visible() or not field.is_enabled():
                    raise TaishinLoginError("登入欄位無法安全填寫；未送出登入")
                field.click()
                field.click(click_count=3)
                page.keyboard.press("Backspace")
                page.keyboard.type(value, delay=80)
                page.wait_for_timeout(200)
                if len(field.input_value()) != len(value):
                    raise TaishinLoginError("登入欄位輸入長度不符；未送出登入")
        except TaishinLoginError:
            raise
        except Exception:
            raise TaishinLoginError("登入欄位無法安全填寫；未送出登入") from None

        captcha = self._ocr_captcha(frame, max_attempts=5)
        if not captcha or len(captcha) != 6 or not captcha.isdigit():
            raise TaishinLoginError("圖形驗證碼 OCR 失敗；未送出登入")
        try:
            candidates = frame.locator(_ph_sel(FIELD_PLACEHOLDERS["captcha"]))
            if candidates.count() != 1:
                raise TaishinLoginError("驗證碼欄位無法安全填寫；未送出登入")
            field = candidates.nth(0)
            if not field.is_visible() or not field.is_enabled():
                raise TaishinLoginError("驗證碼欄位無法安全填寫；未送出登入")
            field.click()
            field.click(click_count=3)
            page.keyboard.press("Backspace")
            page.keyboard.type(captcha, delay=80)
            page.wait_for_timeout(300)
            if len(field.input_value()) != 6:
                raise TaishinLoginError("驗證碼欄位輸入長度不符；未送出登入")
        except TaishinLoginError:
            raise
        except Exception:
            raise TaishinLoginError("驗證碼欄位無法安全填寫；未送出登入") from None

        try:
            candidates = frame.locator(f"#{LOGIN_BTN_ID}")
            if candidates.count() != 1:
                raise TaishinLoginError("找不到唯一且可操作的登入按鈕；未送出登入")
            button = candidates.nth(0)
            if not button.is_visible() or not button.is_enabled():
                raise TaishinLoginError("找不到唯一且可操作的登入按鈕；未送出登入")
        except TaishinLoginError:
            raise
        except Exception:
            raise TaishinLoginError("無法安全確認登入按鈕；未送出登入") from None
        try:
            button.click(timeout=8000)
        except Exception:
            raise TaishinLoginError("登入送出狀態不明；禁止自動重試") from None

        try:
            page.wait_for_timeout(10000)
            for _ in range(30):
                page.wait_for_timeout(1000)
                if self._logged_in(page):
                    return
                scopes = [
                    page,
                    *(child for child in page.frames if child is not page.main_frame),
                ]
                for scope in scopes:
                    for selector in (".modal.show", "[role='dialog']"):
                        checkpoints = scope.locator(selector)
                        if any(
                            checkpoints.nth(index).is_visible()
                            for index in range(checkpoints.count())
                        ):
                            return
        except Exception:
            return

    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        """台新 collect — C 策略：popup 強行 hide → 直接點 top nav「信用卡」。

        2026-06-11 實測揭示：台新主畫面 top nav 在主 page（非 iframe）有 9 個 menu,
        「信用卡」是第 5 個，約 x:1390/2160 寬。直接從 DOM 找元素點，不依賴座標猜測。
        """
        out: dict = {}
        page.wait_for_timeout(8000)

        # ── Step 2: 從所有 frames（含主 page）找 top nav「信用卡」DOM 元素並點 ──
        # 台新 SPA 整個介面在 svc/rwd iframe 內，top nav 也在裡面（不在主 page）
        clicked_credit_card = False
        target_frame = None
        target_info = None
        try:
            for f in [page] + [fr for fr in page.frames if fr != page.main_frame]:
                kind = "page" if f == page else "frame"
                try:
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
                except Exception:
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
        except Exception:
            _log("[taishin][collect] 找信用卡 nav 例外")

        # ── Step 3: 等信用卡頁載入 + 等 API call 跑完（10 秒） ──
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
                    ct = f.evaluate("() => document.body.innerText.slice(0, 12000)")
                    if ct and ("信用卡" in ct or "帳單" in ct or "消費" in ct or "應繳" in ct):
                        credit_card_frame = f
                        page_text = ct
                        _log(f"[taishin][collect] 信用卡頁 frame text len={len(ct)}")
                        break
                except Exception:
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
                        month_options = credit_card_frame.evaluate("""() => {
                            const sels = [...document.querySelectorAll('select')];
                            const monthSel = sels.find(s => [...s.options].some(o => /^\\d{4}\\/\\d{2}$/.test((o.textContent || '').trim())));
                            if (!monthSel) return [];
                            return [...monthSel.options]
                                .map((o, index) => ({index, text: (o.textContent || '').trim()}))
                                .filter(o => /^\\d{4}\\/\\d{2}$/.test(o.text));
                        }""") or []
                    except Exception:
                        _log("[taishin][collect] 月份下拉 dump 失敗")
                    # 穩定按時間降序排 (2026/06, 2026/05, ...)，log 才不會神秘跳
                    month_options.sort(key=lambda o: o.get("text") or "", reverse=True)

                    seen_billed_keys = {
                        (r.get("txn_date"), r.get("post_date"), r.get("desc"), r.get("amount"))
                        for r in parsed.get("billed_txns", [])
                    }
                    for opt in month_options[:12]:
                        try:
                            credit_card_frame.evaluate("location.hash = '#/RB0708/0100?ts=' + Date.now()")
                            page.wait_for_timeout(4000)
                            for f in page.frames:
                                if "svc/rwd" in (f.url or ""):
                                    credit_card_frame = f
                                    break
                            credit_card_frame.locator("select").nth(1).select_option(index=opt.get("index") or 0)
                            page.wait_for_timeout(5000)
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
                        except Exception:
                            _log(f"[taishin][collect] 月份 {opt.get('text')} 查詢失敗")

                out["credit_card_parsed"] = parsed
                _log(f"[taishin][collect] parser 結果: "
                     f"cards={len(parsed.get('cards', []))} "
                     f"pending={len(parsed.get('pending_txns', []))} "
                     f"billed={len(parsed.get('billed_txns', []))} "
                     f"summary={'有' if parsed.get('summary') else '無'}")
            except Exception:
                _log("[taishin][collect] parser 例外")
                out["credit_card_parsed"] = {"error": "parse_failed"}

        # ── Step 5: attested TWD transaction history ──
        out.update(self._collect_attested_twd_history(page, collector))

        # ── Step 6: retain only non-sensitive API responses needed by persistence ──
        hits_by_endpoint = self._non_sensitive_api_responses(collector.hits)
        out["api_responses"] = hits_by_endpoint
        parsed = out.get("credit_card_parsed") or {}
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
