#!/usr/bin/env python3
"""Taipei Fubon Bank personal e-banking crawler (ebank.taipeifubon.com.tw).

台北富邦 ebank.taipeifubon.com.tw 個人網銀爬蟲。

2026-06-12 schema 校正（vision 驗證 + dry probe v2/v3 揭示）:

  frame 結構（4 frames）:
    - frame[0] = main (Index.faces, frameset shell)
    - frame[1] = frame1 (ContextFrame.faces, 含右上「登入」按鈕)
    - frame[2] = QAiFrame (客服, about:blank)
    - frame[3] = txnFrame (PreLogin.faces, 一般登入 form)

  登入流程:
    Step 1: page goto Index.faces → 等 frameset 全載 (12s)
    Step 2: frame1 內 click #header_form:header_login（右上「登入」開 modal）
    Step 3: 等 modal → txnFrame 內 click 「一般登入」tab (<a> text='一般登入')
    Step 4: txnFrame 內 fill 4 欄:
              #m1_LJCHUYIFKV  → 身分證 (maxlen=10)
              #m1_VVYJVIJLIE  → 使用者代碼 (maxlen=10，實況 7 碼 XXX1234)
              #m1_ACXMQTRIBF  → 密碼 (maxlen=16)
              #m1_userCaptcha → 6 碼純數字 captcha
    Step 5: captcha = OCR(#m1_captchaImage locator screenshot bytes)
            實測 3/3 OCR 命中（v3_captcha_t1=418862 等）
    Step 6: click #btnLogin2 (txnFrame 內，<a id="btnLogin2">登入</a>)

  ⚠️ 鐵律 max_attempts=1 — 失敗 raise FubonLoginError，絕不重打（會鎖帳號）
  ⚠️ 換 captcha 用「重新產生」連結，不是 .captcha-refresh
"""
from __future__ import annotations

import contextlib
import os
import re
import sys
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

from scrapling.fetchers import StealthySession

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.base import (
    captcha_unreadable,
    BankCollectResult,
    BankCrawler,
    ResponseCollector,
    _OriginGuardProxy,
    validate_history_coverage,
)
from backend.core.card_bills import make_card_bill_fact, publish_card_bill_facts
from backend.core.captcha import ocr_bytes
from backend.core.error_codes import error_step, exception_error_id, with_error_code
from backend.core.creds import TaipeiFubonCreds
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointPhase,
    LoginCheckpointRule,
    bounded_login_inspection,
    bounded_locator_matches,
)

BASE = "https://ebank.taipeifubon.com.tw/B2C/common/Index.faces"
TWD_HISTORY_URL = "https://ebank.taipeifubon.com.tw/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces"
PRE_LOGIN_HINT = "PreLogin.faces"


def _fubon_route_matches(parsed, route: str, *, _diagnostic=None, _error_ids=()) -> bool:
    if _diagnostic is not None:
        _diagnostic.clear()
    for index, rejected in enumerate((
        lambda: parsed.path != route,
        lambda: parsed.username is not None,
        lambda: parsed.password is not None,
        lambda: parsed.fragment,
    )):
        if rejected():
            if _diagnostic is not None:
                _diagnostic[:] = [_error_ids[index]]
            return False
    is_history = route == urlsplit(TWD_HISTORY_URL).path
    if not parsed.query:
        return True
    if not is_history:
        if _diagnostic is not None:
            _diagnostic[:] = [_error_ids[4]]
        return False
    matched = parsed.query in {"menuId=CDS0401", "menuId=CDS0401&"}
    if not matched and _diagnostic is not None:
        _diagnostic[:] = [_error_ids[5]]
    return matched


def _fubon_form_action_matches(value: object, *, _diagnostic=None, _error_ids=()) -> bool:
    if _diagnostic is not None:
        _diagnostic.clear()
    parsed = urlsplit(str(value or ""))
    try:
        port = parsed.port
    except ValueError:
        if _diagnostic is not None:
            _diagnostic[:] = [_error_ids[0]]
        return False
    for index, accepted in enumerate((
        lambda: parsed.scheme == "https",
        lambda: parsed.hostname == "ebank.taipeifubon.com.tw",
        lambda: port in (None, 443),
    ), 1):
        if not accepted():
            if _diagnostic is not None:
                _diagnostic[:] = [_error_ids[index]]
            return False
    return _fubon_route_matches(parsed, urlsplit(TWD_HISTORY_URL).path, _diagnostic=_diagnostic, _error_ids=_error_ids[4:])


def _fubon_twd_post_matches(value: object, method: object) -> bool:
    parsed = urlsplit(str(value or ""))
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        method == "POST"
        and parsed.scheme == "https"
        and parsed.hostname == "ebank.taipeifubon.com.tw"
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and parsed.path == "/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces"
    )


HEADER_LOGIN_BTN_ID = "header_form:header_login"  # 在 frame1，右上「登入」開 modal
GENERAL_LOGIN_TAB = "一般登入"
LOGIN_BTN_ID = "btnLogin2"  # 一般登入 form 的登入鈕（txnFrame 內）

FIELD_M1_CAPTCHA     = "m1_userCaptcha"  # 6 碼純數字
CAPTCHA_IMG_ID       = "m1_captchaImage"  # 158×30 captcha img
_DYNAMIC_LOGIN_FIELD_ID = re.compile(r"^m1_[A-Z]{10}$")
_FUBON_OPTION_VALUE_RE = re.compile(
    r"^012-(?:\d{3}-(\d{14})-[A-Z]-TW|\d{2}(\d{14})-TWD-\d{2})$"
)


def _fubon_one_year_floor(end: date) -> date:
    year = end.year - 1
    return date(year, end.month, min(end.day, monthrange(year, end.month)[1]))


def _fubon_months_back(end: date, months: int) -> date:
    index = end.year * 12 + end.month - 1 - months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(end.day, monthrange(year, month)[1]))


def _fubon_history_windows(as_of: date) -> list[dict]:
    # Live 2026-10-02: the native rdoDay180 preset returned rows dated six calendar months back, not 180 days.
    recent_start = _fubon_months_back(as_of, 6)
    return [
        {"preset": "rdoDay180_365", "start": _fubon_one_year_floor(as_of).isoformat(), "end": (recent_start - timedelta(days=1)).isoformat()},
        {"preset": "rdoDay180", "start": recent_start.isoformat(), "end": as_of.isoformat()},
    ]


def _validated_fubon_twd_options(options) -> list[dict]:
    with error_step('FUBON-04-200-001'):
        if not isinstance(options, list):
            raise with_error_code(ValueError("invalid Fubon TWD inventory"), 'FUBON-04-200-001')
    with error_step('FUBON-04-201-001'):
        if not options:
            raise with_error_code(ValueError("invalid Fubon TWD inventory"), 'FUBON-04-201-001')
    with error_step('FUBON-04-202-001'):
        validated, seen_identities, seen_values = [], set(), set()
    with error_step('FUBON-04-218-001'):
        for position, option in enumerate(options):
            with error_step('FUBON-04-203-001'):
                if not isinstance(option, dict):
                    raise with_error_code(ValueError("invalid Fubon TWD inventory"), 'FUBON-04-203-001')
            with error_step('FUBON-04-204-001'):
                text, value, index = option.get("text"), option.get("value"), option.get("index")
            if position == 0 and index == 0 and text == "==請選擇==" and value in {"none", "_none"}:
                continue
            with error_step('FUBON-04-205-001'):
                identities = re.findall(r"(?<!\d)\d{10,16}(?!\d)", text or "")
            with error_step('FUBON-04-206-001'):
                value_match = _FUBON_OPTION_VALUE_RE.fullmatch(value) if isinstance(value, str) else None
            with error_step('FUBON-04-207-001'):
                value_identity = next((group for group in value_match.groups() if group), None) if value_match else None
            with error_step('FUBON-04-208-001'):
                canonical_value = len(identities) == 1 and value_identity == identities[0]
            with error_step('FUBON-04-209-001'):
                if type(index) is not int:
                    raise with_error_code(ValueError("invalid Fubon TWD inventory"), 'FUBON-04-209-001')
            with error_step('FUBON-04-210-001'):
                if index != position:
                    raise with_error_code(ValueError("invalid Fubon TWD inventory"), 'FUBON-04-210-001')
            with error_step('FUBON-04-211-001'):
                if not isinstance(text, str):
                    raise with_error_code(ValueError("invalid Fubon TWD inventory"), 'FUBON-04-211-001')
            with error_step('FUBON-04-212-001'):
                if not canonical_value:
                    raise with_error_code(ValueError("invalid Fubon TWD inventory"), 'FUBON-04-212-001')
            with error_step('FUBON-04-213-001'):
                if value in seen_values:
                    raise with_error_code(ValueError("invalid Fubon TWD inventory"), 'FUBON-04-213-001')
            with error_step('FUBON-04-214-001'):
                if identities[0] in seen_identities:
                    raise with_error_code(ValueError("invalid Fubon TWD inventory"), 'FUBON-04-214-001')
            with error_step('FUBON-04-215-001'):
                seen_identities.add(identities[0])
            with error_step('FUBON-04-216-001'):
                seen_values.add(value)
            with error_step('FUBON-04-217-001'):
                validated.append({**option, "identity": identities[0]})
    with error_step('FUBON-04-219-001'):
        if not validated:
            raise with_error_code(ValueError("invalid Fubon TWD inventory"), 'FUBON-04-219-001')
    return validated


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


def bounded_evaluate(scope, expression: str, arg=None, *, _error_id="FUBON-05-800-001"):
    locator_id, evaluate_id = {
        'FUBON-05-800-001': ('FUBON-05-800-101', 'FUBON-05-800-201'),
        'FUBON-05-209-001': ('FUBON-05-209-101', 'FUBON-05-209-201'),
        'FUBON-05-243-001': ('FUBON-05-243-101', 'FUBON-05-243-201'),
        'FUBON-05-100-005': ('FUBON-05-100-105', 'FUBON-05-100-205'),
        'FUBON-05-100-010': ('FUBON-05-100-110', 'FUBON-05-100-210'),
        'FUBON-05-110-005': ('FUBON-05-110-105', 'FUBON-05-110-205'),
        'FUBON-05-110-010': ('FUBON-05-110-110', 'FUBON-05-110-210'),
        'FUBON-05-120-005': ('FUBON-05-120-105', 'FUBON-05-120-205'),
        'FUBON-05-120-010': ('FUBON-05-120-110', 'FUBON-05-120-210'),
        'FUBON-05-281-001': ('FUBON-05-281-101', 'FUBON-05-281-201'),
        'FUBON-05-284-001': ('FUBON-05-284-101', 'FUBON-05-284-201'),
        'FUBON-05-307-001': ('FUBON-05-307-101', 'FUBON-05-307-201'),
        'FUBON-05-316-001': ('FUBON-05-316-101', 'FUBON-05-316-201'),
        'FUBON-05-365-001': ('FUBON-05-365-101', 'FUBON-05-365-201'),
        'FUBON-05-370-001': ('FUBON-05-370-101', 'FUBON-05-370-201'),
        'FUBON-05-382-001': ('FUBON-05-382-101', 'FUBON-05-382-201'),
        'FUBON-07-311-001': ('FUBON-07-311-101', 'FUBON-07-311-201'),
    }[_error_id]
    with error_step(locator_id):
        root = scope.locator("html")
    with error_step(evaluate_id):
        return getattr(root, "evaluate")(
            f"(root, arg) => ({expression})(arg)", arg, timeout=5000,
        )


def _safe_url(value: object) -> str:
    try:
        parsed = urlsplit(str(value or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return "<invalid>"
        port = f":{parsed.port}" if parsed.port else ""
        route = (parsed.path.rsplit("/", 1)[-1] or "").split(";", 1)[0]
        if not re.fullmatch(
            r"(?:Index|ContextFrame|PreLogin)\.faces|[A-Z0-9]{6,}_Home\.faces|dispatcher",
            route,
        ):
            route = "<redacted-path>"
        return f"{parsed.scheme}://{parsed.hostname}{port}/{route}"
    except (TypeError, ValueError):
        return "<invalid>"


class FubonLoginError(RuntimeError):
    """Fubon login 送出後失敗——立刻中止，絕不自動重打。"""


def _fubon_card_bill_fact(amount_text: str):
    with error_step('FUBON-07-298-001'):
        payment = re.search(
            r"繳款狀態.*?自動扣繳帳號.*?\n"
            r"([^\t\n]+)\s*\t\s*(\d{4}/\d{1,2}/\d{1,2})\s*\t\s*"
            r"([\d,]+)\s*\t\s*([\d,]+)\s*\t",
            amount_text,
        )
    with error_step('FUBON-07-299-001'):
        bill = re.search(
            r"本期帳單結帳日.*?繳款截止日.*?\n"
            r"(\d{4}/\d{1,2}/\d{1,2})\s*\t\s*[\d,]+\s*\t\s*[\d,]+"
            r"\s*\t\s*([^\t\n]+)",
            amount_text,
        )
    with error_step('FUBON-07-300-001'):
        due_text = bill.group(2).strip() if bill else None
    if due_text == "無需繳款":
        due_text = None
    with error_step('FUBON-07-301-001'):
        return make_card_bill_fact(
            remaining_due=payment.group(4) if payment else None,
            statement_close_date=bill.group(1) if bill else None,
            payment_due_date=due_text,
            last_payment_amount=payment.group(3) if payment else None,
            last_payment_date=payment.group(2) if payment else None,
        )


def _fubon_preset_diagnostic_guard(
    hits: list[dict], preset_shapes: list[str],
) -> str | None:
    with error_step('FUBON-05-394-001'):
        if any(hit.get("presetBound") is True for hit in hits):
            return None
    with error_step('FUBON-05-395-001'):
        if preset_shapes and all(shape == "missing" for shape in preset_shapes):
            return "fubon-twd-history-transport-preset-missing"
    if "duplicate" in preset_shapes:
        return "fubon-twd-history-transport-preset-duplicate"
    if "mismatch" in preset_shapes:
        return "fubon-twd-history-transport-preset-mismatch"
    return None


def _fubon_settled_control_guard(
    settled: object,
    option: dict,
    observed_pairs: set[tuple[str, str]],
    *, raise_on_error: bool = False, _shape_error_id=None,
) -> str | None:
    def reject(guard, error_id):
        if raise_on_error:
            raise with_error_code(RuntimeError(guard), error_id)
        return guard

    with error_step('FUBON-05-396-001'):
        if not isinstance(settled, dict):
            return reject("fubon-twd-history-controls-settled-shape", _shape_error_id or 'FUBON-05-500-001')
    with error_step('FUBON-05-397-001'):
        identities = re.findall(r"(?<!\d)\d{10,16}(?!\d)", settled.get("text") or "")
    with error_step('FUBON-05-501-001'):
        if settled.get("value") != option.get("value"):
            return reject("fubon-twd-history-controls-account", 'FUBON-05-501-001')
    with error_step('FUBON-05-502-001'):
        if identities != [option.get("identity")]:
            return reject("fubon-twd-history-controls-account", 'FUBON-05-502-001')
    for key, error_id in (("detail", 'FUBON-05-503-001'), ("fast", 'FUBON-05-504-001'), ("preset", 'FUBON-05-505-001')):
        with error_step(error_id):
            if settled.get(key) is not True:
                return reject(f"fubon-twd-history-controls-{key}", error_id)
    with error_step('FUBON-05-401-001'):
        if len(observed_pairs) != 1:
            return reject("fubon-twd-history-controls-preset", 'FUBON-05-506-001')
    with error_step('FUBON-05-402-001'):
        action, preset = next(iter(observed_pairs))
    for value, error_id in ((action, 'FUBON-05-507-001'), (preset, 'FUBON-05-508-001')):
        with error_step(error_id):
            if not isinstance(value, str):
                return reject("fubon-twd-history-controls-preset", error_id[:-3] + '101')
            value_length = len(value)
            if not 0 < value_length:
                return reject("fubon-twd-history-controls-preset", error_id[:-3] + '102')
            if not value_length <= 128:
                return reject("fubon-twd-history-controls-preset", error_id[:-3] + '103')
    with error_step('FUBON-05-509-001'):
        if settled.get("formBound") is not True:
            return reject("fubon-twd-history-controls-form", 'FUBON-05-509-001')
    with error_step('FUBON-05-510-001'):
        if not isinstance(settled.get("viewState"), str):
            return reject("fubon-twd-history-controls-viewstate", 'FUBON-05-510-001')
    with error_step('FUBON-05-511-001'):
        if not settled["viewState"]:
            return reject("fubon-twd-history-controls-viewstate", 'FUBON-05-511-001')
    with error_step('FUBON-05-512-001'):
        form_reason = []
        if not _fubon_form_action_matches(settled.get("formAction"), _diagnostic=form_reason, _error_ids=(
            'FUBON-05-788-001', 'FUBON-05-788-002', 'FUBON-05-788-003', 'FUBON-05-788-004',
            'FUBON-05-788-005', 'FUBON-05-788-006', 'FUBON-05-788-007', 'FUBON-05-788-008', None, 'FUBON-05-788-009',
        )):
            return reject("fubon-twd-history-controls-action", form_reason[0] if form_reason else 'FUBON-05-512-001')
    return None


class FubonCrawler(BankCrawler):
    USES_SHARED_LOGIN_CHECKPOINTS: ClassVar[bool] = True
    SAFE_COLLECT_GUARDS = frozenset({
        "fubon-twd-history-controls",
        "fubon-twd-history-controls-action",
        "fubon-twd-history-controls-account",
        "fubon-twd-history-controls-detail",
        "fubon-twd-history-controls-fast",
        "fubon-twd-history-controls-form",
        "fubon-twd-history-controls-preset",
        "fubon-twd-history-controls-settled-shape",
        "fubon-twd-history-controls-viewstate",
        "fubon-twd-history-frame",
        "fubon-twd-history-inventory",
        "fubon-twd-history-navigation",
        "fubon-twd-history-result",
        "fubon-twd-history-result-account-identity",
        "fubon-twd-history-result-account-value",
        "fubon-twd-history-result-busy",
        "fubon-twd-history-result-failed",
        "fubon-twd-history-result-frame",
        "fubon-twd-history-result-freshness",
        "fubon-twd-history-result-mutated",

        "fubon-twd-history-transport",
        "fubon-twd-history-transport-failed",
        "fubon-twd-history-transport-no-bound-response",
        "fubon-twd-history-transport-no-action-bound-response",
        "fubon-twd-history-transport-no-fields-bound-response",
        "fubon-twd-history-transport-no-form-bound-response",
        "fubon-twd-history-transport-no-frame-bound-response",
        "fubon-twd-history-transport-no-preset-bound-response",
        "fubon-twd-history-transport-preset-duplicate",
        "fubon-twd-history-transport-preset-mismatch",
        "fubon-twd-history-transport-preset-missing",
        "fubon-twd-history-transport-no-viewstate-bound-response",
        "fubon-twd-history-transport-no-response",
        "fubon-twd-history-transport-pending",
        "fubon-twd-history-transport-quiescence",
        "fubon-twd-history-transport-request-count",
        "fubon-twd-history-transport-too-many-responses",
    })
    HISTORY_COVERAGE_REQUIRED: ClassVar[bool] = True
    HISTORY_COVERAGE_DOMAINS: ClassVar[frozenset[str]] = frozenset({"twd_transactions"})
    CREDENTIAL_HOSTS = frozenset({"ebank.taipeifubon.com.tw"})

    def __init__(self):
        super().__init__(name="fubon")
        self.creds = TaipeiFubonCreds.load()

    def _history_windows(self, identity: str, as_of: date) -> list[dict]:
        with error_step('FUBON-05-407-001'):
            full = _fubon_history_windows(as_of)
        with error_step('FUBON-05-408-001'):
            mode = os.environ.get("BANK_CRAWLER_HISTORY_MODE", "full")
        with error_step('FUBON-05-409-001'):
            cursor = self.transaction_start_for(identity, domain="twd_transactions")
        if mode == "full" or cursor is None:
            return full
        with error_step('FUBON-05-410-001'):
            return full[1:] if cursor >= date.fromisoformat(full[1]["start"]) else full

    @staticmethod
    def _validated_twd_history_result(result: dict) -> dict:
        with error_step('FUBON-07-200-001'):
            if not isinstance(result, dict):
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-200-001')
        with error_step('FUBON-07-100-001'):
            identity = result.get("account_no")
        with error_step('FUBON-07-101-001'):
            account_value = result.get("account_value")
        with error_step('FUBON-07-102-001'):
            preset = result.get("preset")
        with error_step('FUBON-07-103-001'):
            start = result.get("start")
        with error_step('FUBON-07-104-001'):
            end = result.get("end")
        with error_step('FUBON-07-105-001'):
            status = result.get("status")
        with error_step('FUBON-07-106-001'):
            snapshot = result.get("snapshot")
        with error_step('FUBON-07-107-001'):
            transport = result.get("transport")
        with error_step('FUBON-07-204-001'):
            if not isinstance(start, str):
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-204-001')
        with error_step('FUBON-07-205-001'):
            if not isinstance(end, str):
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-205-001')
        try:
            with error_step('FUBON-07-115-001'):
                start_day = date.fromisoformat(start)
            with error_step('FUBON-07-116-001'):
                end_day = date.fromisoformat(end)
            with error_step('FUBON-07-207-001'):
                parsed_url = urlsplit(result.get("url") or "")
        except (TypeError, ValueError) as source_error:
            raise with_error_code(RuntimeError("fubon-twd-history-result"), exception_error_id(source_error, bank="fubon") or 'FUBON-07-208-001') from None
        # Evaluate every snapshot predicate eagerly, in its original order,
        # before choosing the first rejection. Do not turn this into a lazy loop.
        with error_step('FUBON-07-110-001'):
            freshness = isinstance(snapshot, dict) and snapshot.get("resultFresh") is True
        with error_step('FUBON-07-111-001'):
            busy = isinstance(snapshot, dict) and snapshot.get("busy") is False
        with error_step('FUBON-07-112-001'):
            failed = isinstance(snapshot, dict) and snapshot.get("failed") is False
        with error_step('FUBON-07-113-001'):
            selected_value = isinstance(snapshot, dict) and snapshot.get("selectedValue") == account_value
        with error_step('FUBON-07-114-001'):
            selected_identity = isinstance(snapshot, dict) and snapshot.get("selectedIdentity") == identity
        snapshot_guards = (
            ("freshness", freshness, 'FUBON-07-110-002'),
            ("busy", busy, 'FUBON-07-111-002'),
            ("failed", failed, 'FUBON-07-112-002'),
            ("account-value", selected_value, 'FUBON-07-113-002'),
            ("account-identity", selected_identity, 'FUBON-07-114-002'),
        )
        if isinstance(snapshot, dict):
            for guard, valid, error_id in snapshot_guards:
                if not valid:
                    raise with_error_code(RuntimeError(f"fubon-twd-history-result-{guard}"), error_id)
        with error_step('FUBON-07-213-001'):
            if not isinstance(identity, str):
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-213-001')
        with error_step('FUBON-07-214-001'):
            if not re.fullmatch('\\d{10,16}', identity):
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-214-001')
        with error_step('FUBON-07-215-001'):
            if not isinstance(account_value, str):
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-215-001')
        with error_step('FUBON-07-216-001'):
            if (value_match := _FUBON_OPTION_VALUE_RE.fullmatch(account_value)) is None:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-216-001')
        with error_step('FUBON-07-217-001'):
            if next((group for group in value_match.groups() if group), None) != identity:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-217-001')
        with error_step('FUBON-07-218-001'):
            if preset not in {'rdoDay180_365', 'rdoDay180'}:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-218-001')
        with error_step('FUBON-07-219-001'):
            if start_day > end_day:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-219-001')
        with error_step('FUBON-07-220-001'):
            if status not in {'complete', 'explicit_empty'}:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-220-001')
        with error_step('FUBON-07-221-001'):
            if parsed_url.scheme != 'https':
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-221-001')
        with error_step('FUBON-07-222-001'):
            if parsed_url.hostname != 'ebank.taipeifubon.com.tw':
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-222-001')
        with error_step('FUBON-07-223-001'):
            if parsed_url.port not in (None, 443):
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-223-001')
        with error_step('FUBON-07-224-001'):
            if parsed_url.username is not None:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-224-001')
        with error_step('FUBON-07-225-001'):
            if parsed_url.password is not None:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-225-001')
        with error_step('FUBON-07-226-001'):
            if parsed_url.query:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-226-001')
        with error_step('FUBON-07-227-001'):
            if parsed_url.fragment:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-227-001')
        with error_step('FUBON-07-228-001'):
            if parsed_url.path != '/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces':
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-228-001')
        with error_step('FUBON-07-229-001'):
            if not isinstance(transport, dict):
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-229-001')
        with error_step('FUBON-07-230-001'):
            if set(transport) != {'status', 'contentType', 'responseCount', 'matchingResponseCount', 'frameBound', 'requestBound', 'presetBound', 'fieldsBound', 'viewStateBound', 'actionBound', 'formBound', 'allResponsesBound'}:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-230-001')
        with error_step('FUBON-07-231-001'):
            if type(transport.get('status')) is not int:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-231-001')
        with error_step('FUBON-07-232-001'):
            if transport.get('status') != 200:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-232-001')
        with error_step('FUBON-07-233-001'):
            if transport.get('contentType') != 'text/plain':
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-233-001')
        with error_step('FUBON-07-234-001'):
            if type(transport.get('responseCount')) is not int:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-234-001')
        with error_step('FUBON-07-235-001'):
            if transport['responseCount'] < 1:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-235-001')
        with error_step('FUBON-07-236-001'):
            if transport['responseCount'] > 20:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-236-001')
        with error_step('FUBON-07-237-001'):
            if type(transport.get('matchingResponseCount')) is not int:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-237-001')
        with error_step('FUBON-07-238-001'):
            if transport['matchingResponseCount'] < 1:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-238-001')
        with error_step('FUBON-07-239-001'):
            if transport['matchingResponseCount'] > 20:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-239-001')
        with error_step('FUBON-07-240-001'):
            if transport['matchingResponseCount'] > transport['responseCount']:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-240-001')
        with error_step('FUBON-07-241-001'):
            if transport.get('frameBound') is not True:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-241-001')
        with error_step('FUBON-07-242-001'):
            if transport.get('requestBound') is not True:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-242-001')
        with error_step('FUBON-07-243-001'):
            if transport.get('presetBound') is not True:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-243-001')
        with error_step('FUBON-07-244-001'):
            if transport.get('fieldsBound') is not True:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-244-001')
        with error_step('FUBON-07-245-001'):
            if transport.get('viewStateBound') is not True:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-245-001')
        with error_step('FUBON-07-246-001'):
            if transport.get('actionBound') is not True:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-246-001')
        with error_step('FUBON-07-247-001'):
            if transport.get('formBound') is not True:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-247-001')
        with error_step('FUBON-07-248-001'):
            if transport.get('allResponsesBound') is not True:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-248-001')
        with error_step('FUBON-07-249-001'):
            if not isinstance(snapshot, dict):
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-249-001')
        with error_step('FUBON-07-250-001'):
            if snapshot.get('resultFresh') is not True:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-250-001')
        with error_step('FUBON-07-251-001'):
            if snapshot.get('busy') is not False:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-251-001')
        with error_step('FUBON-07-252-001'):
            if snapshot.get('failed') is not False:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-252-001')
        with error_step('FUBON-07-253-001'):
            if snapshot.get('selectedValue') != account_value:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-253-001')
        with error_step('FUBON-07-254-001'):
            if snapshot.get('selectedIdentity') != identity:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-254-001')
        with error_step('FUBON-07-255-001'):
            pager = snapshot.get("pager")
        with error_step('FUBON-07-256-001'):
            if not isinstance(pager, dict):
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-256-001')
        with error_step('FUBON-07-257-001'):
            if set(pager) != {'present', 'actionableNext'}:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-257-001')
        with error_step('FUBON-07-258-001'):
            if pager.get('present') is not False:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-258-001')
        with error_step('FUBON-07-259-001'):
            if type(pager.get('actionableNext')) is not int:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-259-001')
        with error_step('FUBON-07-260-001'):
            if pager['actionableNext'] != 0:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-260-001')
        with error_step('FUBON-07-261-001'):
            rows, row_count = snapshot.get("gridRows"), snapshot.get("gridRowCount")
        with error_step('FUBON-07-262-001'):
            total_count, raw_count = snapshot.get("totalCount"), snapshot.get("rawDataRowCount")
        with error_step('FUBON-07-263-001'):
            grid_count = snapshot.get("gridCandidateCount")
        with error_step('FUBON-07-264-001'):
            if not isinstance(rows, list):
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-264-001')
        with error_step('FUBON-07-265-001'):
            if type(row_count) is not int:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-265-001')
        with error_step('FUBON-07-266-001'):
            if type(total_count) is not int:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-266-001')
        with error_step('FUBON-07-267-001'):
            if type(raw_count) is not int:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-267-001')
        with error_step('FUBON-07-268-001'):
            if type(grid_count) is not int:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-268-001')
        with error_step('FUBON-07-269-001'):
            if type(snapshot.get('hiddenGridCount')) is not int:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-269-001')
        with error_step('FUBON-07-270-001'):
            if snapshot.get('hiddenGridCount') != 0:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-270-001')
        with error_step('FUBON-07-271-001'):
            if type(snapshot.get('pagerNodeCount')) is not int:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-271-001')
        with error_step('FUBON-07-272-001'):
            if snapshot.get('pagerNodeCount') != 0:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-272-001')
        with error_step('FUBON-07-273-001'):
            if type(snapshot.get('structuralErrorCount')) is not int:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-273-001')
        with error_step('FUBON-07-274-001'):
            if snapshot.get('structuralErrorCount') != 0:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-274-001')
        with error_step('FUBON-07-275-001'):
            if type(snapshot.get('nativeTotalMarkerCount')) is not int:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-275-001')
        with error_step('FUBON-07-276-001'):
            if len(rows) != row_count:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-276-001')
        with error_step('FUBON-07-277-001'):
            if total_count != raw_count:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-277-001')
        with error_step('FUBON-07-278-001'):
            if raw_count != row_count:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-278-001')
        with error_step('FUBON-07-279-001'):
            if type(snapshot.get('malformedRowCount')) is not int:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-279-001')
        with error_step('FUBON-07-280-001'):
            if snapshot.get('malformedRowCount') != 0:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-280-001')
        with error_step('FUBON-07-281-001'):
            if type(snapshot.get('hiddenRowCount')) is not int:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-281-001')
        with error_step('FUBON-07-282-001'):
            if snapshot.get('hiddenRowCount') != 0:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-282-001')
        with error_step('FUBON-07-283-001'):
            if type(snapshot.get('hiddenCellCount')) is not int:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-283-001')
        with error_step('FUBON-07-284-001'):
            if snapshot.get('hiddenCellCount') != 0:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-284-001')
        with error_step('FUBON-07-297-001'):
            if status == "complete":
                try:
                    with error_step('FUBON-07-289-001'):
                        for cells in rows:
                            with error_step('FUBON-07-285-001'):
                                if not isinstance(cells, list):
                                    raise with_error_code(ValueError, 'FUBON-07-285-001')
                            with error_step('FUBON-07-286-001'):
                                matched = [
                                    match.group(1)
                                    for cell in cells
                                    if isinstance(cell, str)
                                    if (match := re.fullmatch(r"\*?(20\d{2}/\d{1,2}/\d{1,2})", cell))
                                ]
                            with error_step('FUBON-07-287-001'):
                                if len(matched) != 1:
                                    raise with_error_code(ValueError, 'FUBON-07-287-001')
                            with error_step('FUBON-07-288-001'):
                                date.fromisoformat(matched[0].replace("/", "-"))
                except (TypeError, ValueError) as source_error:
                    raise with_error_code(RuntimeError("fubon-twd-history-result"), exception_error_id(source_error, bank="fubon") or 'FUBON-07-290-001') from None
                with error_step('FUBON-07-291-001'):
                    if snapshot.get('hasGrid') is not True:
                        raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-291-001')
                with error_step('FUBON-07-292-001'):
                    if not ((native_total := snapshot.get('nativeTotalFound') is True) and snapshot.get('nativeTotalMarkerCount') == 1 or ((single_page := snapshot.get('singlePageProof') is True) and snapshot.get('nativeTotalMarkerCount') == 0)):
                        # Each alternative must fail; retain both causes without rereading.
                        error_id = {
                            (False, False): 'FUBON-07-292-101',
                            (True, False): 'FUBON-07-292-102',
                            (False, True): 'FUBON-07-292-103',
                            (True, True): 'FUBON-07-292-104',
                        }[native_total, single_page]
                        raise with_error_code(RuntimeError("fubon-twd-history-result"), error_id)
                with error_step('FUBON-07-293-001'):
                    if grid_count != 1:
                        raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-293-001')
                with error_step('FUBON-07-294-001'):
                    if row_count <= 0:
                        raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-294-001')
                with error_step('FUBON-07-295-001'):
                    if snapshot.get('emptyMarker') is not None:
                        raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-295-001')
            else:
                if snapshot.get("hasGrid") is not False:
                    raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-296-101')
                if snapshot.get("nativeTotalMarkerCount") != 0:
                    raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-296-102')
                if grid_count != 0:
                    raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-296-103')
                if rows != []:
                    raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-296-104')
                if row_count != 0:
                    raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-296-105')
                if snapshot.get("gridText") != "":
                    raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-296-106')
                if snapshot.get("emptyMarker") not in {"查無相關資料", "查無交易資料"}:
                    raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-07-296-107')
        return {"identity": identity, "start": start, "end": end, "status": status, "pages": 1}

    def _execute_browser_flow(
        self,
        login_url: str,
        *,
        headless: bool,
        page_action,
        fetch_kwargs: dict,
    ) -> None:
        _log("[fubon][phase] browser_start")
        with error_step('FUBON-01-234-001'):
            with StealthySession(
                headless=headless,
                user_data_dir=str(self.session_dir),
                hide_canvas=True,
                block_webrtc=True,
                dns_over_https=True,
                **fetch_kwargs,
            ) as engine:
                _log("[fubon][phase] browser_ready")
                with error_step('FUBON-01-222-001'):
                    if engine.context is None:
                        raise with_error_code(RuntimeError("富邦瀏覽器 context 未建立"), 'FUBON-01-222-001')
                with error_step('FUBON-01-223-001'):
                    page = engine.context.new_page()
                with error_step('FUBON-01-224-001'):
                    page.set_default_navigation_timeout(180000)
                with error_step('FUBON-01-225-001'):
                    page.set_default_timeout(180000)
                with error_step('FUBON-01-227-001'):
                    if headers := fetch_kwargs.get("extra_headers"):
                        with error_step('FUBON-01-226-001'):
                            page.set_extra_http_headers(headers)
                try:
                    _log("[fubon][phase] goto_start")
                    with error_step('FUBON-01-228-001'):
                        page.goto(login_url, referer="https://www.google.com/")
                    with error_step('FUBON-01-229-001'):
                        page.wait_for_load_state("load")
                    with error_step('FUBON-01-230-001'):
                        page.wait_for_load_state("domcontentloaded")
                    _log("[fubon][phase] goto_done")
                    _log("[fubon][phase] page_action_start")
                    with error_step('FUBON-01-231-001'):
                        page_action(page)
                    _log("[fubon][phase] page_action_done")
                    with error_step('FUBON-01-232-001'):
                        page.wait_for_timeout(2000)
                finally:
                    _log("[fubon][phase] browser_close")
                    with error_step('FUBON-07-330-001'):
                        page.close()

    def _host_filter(self) -> str:
        return "taipeifubon.com"

    def _is_owned_frame(self, page, frame) -> bool:
        return self._frame_origin_allowed(page, frame)

    def _find_login_frame(self, page):
        with error_step('FUBON-01-235-001'):
            matches = {
                id(frame): frame
                for frame in page.frames
                if self._is_owned_frame(page, frame)
                and (frame.name == "txnFrame" or PRE_LOGIN_HINT in (frame.url or ""))
            }
        with error_step('FUBON-01-236-001'):
            return next(iter(matches.values())) if len(matches) == 1 else None

    def _find_header_frame(self, page):
        with error_step('FUBON-01-237-001'):
            matches = {
                id(frame): frame
                for frame in page.frames
                if self._is_owned_frame(page, frame)
                and (frame.name == "frame1" or "ContextFrame" in (frame.url or ""))
            }
        with error_step('FUBON-01-238-001'):
            return next(iter(matches.values())) if len(matches) == 1 else None

    def _logged_in(self, page) -> bool:
        """Pure one-shot positive check; lifecycle owns all waiting."""
        try:
            with error_step('FUBON-03-200-001'):
                if not self._exact_https_origin_allowed(page.url, self.CREDENTIAL_HOSTS):
                    return False
            with error_step('FUBON-03-201-001'):
                if any(
                    not self._is_owned_frame(page, frame)
                    for frame in page.frames
                    if frame.name in {"txnFrame", "frame1"}
                    or PRE_LOGIN_HINT in (frame.url or "")
                    or "ContextFrame" in (frame.url or "")
                ):
                    return False
            with error_step('FUBON-03-202-001'):
                login_frames = {
                    id(frame)
                    for frame in page.frames
                    if self._is_owned_frame(page, frame)
                    and (frame.name == "txnFrame" or PRE_LOGIN_HINT in (frame.url or ""))
                }
            with error_step('FUBON-03-203-001'):
                header_frames = {
                    id(frame)
                    for frame in page.frames
                    if self._is_owned_frame(page, frame)
                    and (frame.name == "frame1" or "ContextFrame" in (frame.url or ""))
                }
            with error_step('FUBON-03-204-001'):
                if len(login_frames) > 1 or len(header_frames) > 1:
                    return False
            with error_step('FUBON-03-205-001'):
                if any(PRE_LOGIN_HINT in (frame.url or "") for frame in page.frames):
                    return False
            with error_step('FUBON-03-206-001'):
                scopes = [
                    page,
                    *(
                        frame
                        for frame in page.frames
                        if frame is not page.main_frame and self._is_owned_frame(page, frame)
                    ),
                ]
            body_parts = []
            controls_selector = (
                "input[type='password'], #m1_userCaptcha, #btnLogin2, "
                "#header_form\\:header_login"
            )
            with error_step('FUBON-03-211-001'):
                for scope in scopes:
                    with error_step('FUBON-03-207-001'):
                        controls = scope.locator(controls_selector)
                    with error_step('FUBON-03-208-001'):
                        if any(item.is_visible() for item in bounded_locator_matches(controls, error_ids=('FUBON-03-208-301', 'FUBON-03-208-302'))):
                            return False
                    with error_step('FUBON-03-209-001'):
                        bodies = scope.locator("body")
                    with error_step('FUBON-03-210-001'):
                        body_parts.extend(
                            body.inner_text(timeout=5000)
                            for body in bounded_locator_matches(bodies, error_ids=('FUBON-03-210-301', 'FUBON-03-210-302'))
                        )
            with error_step('FUBON-03-212-001'):
                body = "\n".join(body_parts)
            with error_step('FUBON-03-213-001'):
                return (
                    len(body) >= 500
                    and "登出" in body
                    and (
                        any(item in body for item in ("帳戶總覽", "我的帳戶", "資產總額"))
                        or ("帳戶" in body and "資產" in body)
                    )
                )
        except Exception:
            return False

    def login(self, page) -> bool:
        return self._shared_login(page)

    def prepare_login_page(self, page) -> None:
        _log("[fubon][phase] prepare_start")
        with bounded_login_inspection(page):
            self._prepare_login_page(page)
        _log("[fubon][phase] prepare_done")

    def _prepare_login_page(self, page) -> None:
        try:
            with error_step('FUBON-01-200-001'):
                page.wait_for_timeout(12000)
            with error_step('FUBON-01-201-001'):
                if self._logged_in(page):
                    return
            _log("[fubon][phase] prepare_auth_checked")
            with error_step('FUBON-01-202-001'):
                header_frame = self._find_header_frame(page)
            with error_step('FUBON-01-203-001'):
                if header_frame is None:
                    raise with_error_code(FubonLoginError("找不到唯一的登入頁首；未送出登入"), 'FUBON-01-203-001')
            with error_step('FUBON-01-204-001'):
                headers = tuple(bounded_locator_matches(
                    header_frame.locator("#header_form\\:header_login"),
                    first_timeout_ms=5000,
                    error_ids=('FUBON-01-204-301', 'FUBON-01-204-302'),
                ))
            with error_step('FUBON-01-205-001'):
                if len(headers) != 1:
                    raise with_error_code(FubonLoginError("找不到唯一且可操作的頁首登入按鈕；未送出登入"), 'FUBON-01-205-001')
            with error_step('FUBON-01-206-001'):
                header = headers[0]
            with error_step('FUBON-01-207-001'):
                if not header.is_visible():
                    raise with_error_code(FubonLoginError("找不到唯一且可操作的頁首登入按鈕；未送出登入"), 'FUBON-01-207-001')
            with error_step('FUBON-01-208-001'):
                if not header.is_enabled():
                    raise with_error_code(FubonLoginError("找不到唯一且可操作的頁首登入按鈕；未送出登入"), 'FUBON-01-208-001')
            with error_step('FUBON-01-209-001'):
                if ' '.join(header.inner_text().split()) != '登入':
                    raise with_error_code(FubonLoginError("找不到唯一且可操作的頁首登入按鈕；未送出登入"), 'FUBON-01-209-001')
            with error_step('FUBON-01-210-001'):
                header.click(timeout=8000)
            _log("[fubon][phase] header_clicked")
            with error_step('FUBON-01-211-001'):
                page.wait_for_timeout(5000)

            with error_step('FUBON-01-212-001'):
                login_frame = self._find_login_frame(page)
            with error_step('FUBON-01-213-001'):
                if login_frame is None:
                    raise with_error_code(FubonLoginError("找不到唯一的一般登入頁面；未送出登入"), 'FUBON-01-213-001')
            _log("[fubon][phase] login_frame_ready")
            with error_step('FUBON-01-214-001'):
                actions = error_step('FUBON-01-214-201')(lambda: error_step('FUBON-01-214-202')(lambda: login_frame.locator("a, button"))().filter(
                    has_text=re.compile(r"^\s*一般登入\s*$")
                ))()
            eligible = []
            with error_step('FUBON-01-217-001'):
                for action in bounded_locator_matches(actions, first_timeout_ms=5000, error_ids=('FUBON-01-217-301', 'FUBON-01-217-302')):
                    with error_step('FUBON-01-216-001'):
                        if (
                            error_step('FUBON-01-216-201')(lambda: action.is_visible())()
                            and error_step('FUBON-01-216-202')(lambda: action.is_enabled())()
                            and " ".join(error_step('FUBON-01-216-203')(lambda: action.inner_text())().split()) == GENERAL_LOGIN_TAB
                        ):
                            with error_step('FUBON-01-215-001'):
                                eligible.append(action)
            with error_step('FUBON-01-218-001'):
                if len(eligible) != 1:
                    raise with_error_code(FubonLoginError("找不到唯一且可操作的一般登入分頁；未送出登入"), 'FUBON-01-218-001')
            with error_step('FUBON-01-219-001'):
                eligible[0].click(timeout=8000)
            with error_step('FUBON-01-220-001'):
                page.wait_for_timeout(2000)
        except FubonLoginError:
            raise
        except Exception as error:
            raise with_error_code(FubonLoginError("登入頁面準備狀態不明；未送出登入"), exception_error_id(error, bank="fubon") or 'FUBON-01-221-001') from None

    def is_authenticated(self, page) -> bool:
        return self._logged_in(page)

    def login_checkpoint_rules(self) -> tuple[LoginCheckpointRule, ...]:
        all_phases = tuple(CheckpointPhase)
        post = (CheckpointPhase.POST_SUBMIT, CheckpointPhase.POST_SUBMIT_SETTLE)
        otp = re.compile(
            r"^[\s\S]{0,400}(?:(?<![A-Za-z])OTP(?![A-Za-z])|一次性密碼|簡訊驗證碼|裝置驗證|信任此裝置)[\s\S]{0,400}$",
            re.IGNORECASE,
        )
        password = re.compile(
            r"^[\s\S]{0,120}(?:(?:必須|強制|請立即|請先)(?:變更|修改|更新|重設)(?:您的?)?密碼|"
            r"密碼(?:已到期|已過期|必須修改|需要修改|需修改|強制變更))[\s\S]{0,120}$"
        )
        error = re.compile(
            r"^\s*(?:密碼不正確|帳號(?:因多次錯誤)?(?:已遭|已被|已)鎖定|登入失敗|驗證碼不正確|invalid credentials|account locked)[。.!！\s]*$",
            re.IGNORECASE,
        )
        rules = []
        for suffix, selector in (("modal", ".modal.show"), ("dialog", "[role='dialog']")):
            rules.append(LoginCheckpointRule(
                name=f"fubon-otp-required-{suffix}", bank="fubon", phases=all_phases,
                kind=CheckpointKind.OTP_REQUIRED, container_selector=selector,
                required_body_pattern=otp,
            ))
        for suffix, selector in (("modal", ".modal.show"), ("dialog", "[role='dialog']")):
            rules.append(LoginCheckpointRule(
                name=f"fubon-password-change-required-{suffix}", bank="fubon", phases=all_phases,
                kind=CheckpointKind.PASSWORD_CHANGE_REQUIRED, container_selector=selector,
                required_body_pattern=password,
            ))
        for suffix, selector in (("error", ".error"), ("alert", ".alert"), ("role-alert", "[role='alert']")):
            rules.append(LoginCheckpointRule(
                name=f"fubon-explicit-login-error-{suffix}", bank="fubon", phases=post,
                kind=CheckpointKind.EXPLICIT_LOGIN_ERROR, container_selector=selector,
                required_body_pattern=error,
            ))
        for suffix, selector in (("modal", ".modal.show"), ("dialog", "[role='dialog']")):
            rules.append(LoginCheckpointRule(
                name=f"fubon-unknown-{suffix}", bank="fubon", phases=all_phases,
                kind=CheckpointKind.UNKNOWN_BLOCKER, container_selector=selector,
            ))
        rules.append(LoginCheckpointRule(
            name="fubon-login-form-still-visible", bank="fubon", phases=post,
            kind=CheckpointKind.UNKNOWN_BLOCKER, container_selector="#m1_userCaptcha",
        ))
        return tuple(rules)

    def _ocr_captcha(self, frame, max_attempts=5, *, _diagnostic=None):
        # Per-invocation fixed codes only; never retain an exception or OCR text.
        if _diagnostic is not None:
            _diagnostic.clear()
        with error_step('FUBON-02-262-001'):
            attempts = min(max(max_attempts, 0), 5)
        with error_step('FUBON-02-277-001'):
            for attempt in range(attempts):
                if _diagnostic is not None:
                    _diagnostic.clear()
                try:
                    with error_step('FUBON-02-263-001'):
                        images = tuple(bounded_locator_matches(
                            frame.locator("#m1_captchaImage"), first_timeout_ms=5000,
                            error_ids=('FUBON-02-263-301', 'FUBON-02-263-302'),
                        ))
                    with error_step('FUBON-02-264-001'):
                        if len(images) != 1:
                            return None
                    with error_step('FUBON-02-265-001'):
                        image = images[0]
                    with error_step('FUBON-02-266-001'):
                        if not image.is_visible():
                            return None
                    with error_step('FUBON-02-267-001'):
                        raw = image.screenshot(timeout=5000)
                    with error_step('FUBON-02-268-001'):
                        text = ocr_bytes(
                            raw,
                            expected_len=6,
                            alnum_only=True,
                            min_confidence=0.98,
                        )
                except Exception as error:
                    if _diagnostic is not None:
                        reason = exception_error_id(error, bank="fubon")
                        _diagnostic[:] = [reason] if reason else []
                    return None
                with error_step('FUBON-02-269-001'):
                    if text and len(text) == 6 and text.isdigit():
                        if _diagnostic is not None:
                            _diagnostic.clear()
                        return text
                if attempt == attempts - 1:
                    break
                try:
                    with error_step('FUBON-02-270-001'):
                        candidates = frame.locator("a, button")
                    eligible = []
                    with error_step('FUBON-02-273-001'):
                        for action in bounded_locator_matches(candidates, first_timeout_ms=5000, error_ids=('FUBON-02-273-301', 'FUBON-02-273-302')):
                            with error_step('FUBON-02-272-001'):
                                if (
                                    error_step('FUBON-02-272-201')(lambda: action.is_visible())()
                                    and error_step('FUBON-02-272-202')(lambda: action.is_enabled())()
                                    and " ".join(error_step('FUBON-02-272-203')(lambda: action.inner_text())().split()) == "重新產生"
                                ):
                                    with error_step('FUBON-02-271-001'):
                                        eligible.append(action)
                    with error_step('FUBON-02-274-001'):
                        if len(eligible) != 1:
                            return None
                    with error_step('FUBON-02-275-001'):
                        eligible[0].click()
                    with error_step('FUBON-02-276-001'):
                        frame.wait_for_timeout(1500)
                except Exception as error:
                    if _diagnostic is not None:
                        reason = exception_error_id(error, bank="fubon")
                        _diagnostic[:] = [reason] if reason else []
                    return None
        return None

    def submit_credentials_once(self, page) -> None:
        _log("[fubon][phase] submit_start")
        with bounded_login_inspection(page):
            self._submit_credentials_once(page)
        _log("[fubon][phase] submit_done")

    def _submit_credentials_once(self, page) -> None:
        try:
            with error_step('FUBON-02-200-001'):
                frame = self._find_login_frame(page)
            with error_step('FUBON-02-201-001'):
                if frame is None:
                    raise with_error_code(FubonLoginError("找不到唯一的登入頁面；未送出登入"), 'FUBON-02-201-001')
            with error_step('FUBON-02-202-001'):
                candidates = frame.locator("input[type='password']")
            ordered = []
            with error_step('FUBON-02-216-001'):
                for field in bounded_locator_matches(candidates, first_timeout_ms=5000, error_ids=('FUBON-02-216-301', 'FUBON-02-216-302')):
                    with error_step('FUBON-02-203-001'):
                        if not field.is_visible():
                            continue
                    with error_step('FUBON-02-204-001'):
                        field_id = field.get_attribute("id") or ""
                    with error_step('FUBON-02-205-001'):
                        field_name = field.get_attribute("name") or ""
                    with error_step('FUBON-02-206-001'):
                        maxlength = field.get_attribute("maxlength") or ""
                    with error_step('FUBON-02-207-001'):
                        box = field.bounding_box()
                    with error_step('FUBON-02-208-001'):
                        if not field.is_enabled():
                            raise with_error_code(FubonLoginError("登入欄位無法安全填寫；未送出登入"), 'FUBON-02-208-001')
                    with error_step('FUBON-02-209-001'):
                        if not _DYNAMIC_LOGIN_FIELD_ID.fullmatch(field_id):
                            raise with_error_code(FubonLoginError("登入欄位無法安全填寫；未送出登入"), 'FUBON-02-209-001')
                    with error_step('FUBON-02-210-001'):
                        if field_id != f'm1_{field_name}':
                            raise with_error_code(FubonLoginError("登入欄位無法安全填寫；未送出登入"), 'FUBON-02-210-001')
                    with error_step('FUBON-02-211-001'):
                        if maxlength not in {'10', '16'}:
                            raise with_error_code(FubonLoginError("登入欄位無法安全填寫；未送出登入"), 'FUBON-02-211-001')
                    with error_step('FUBON-02-212-001'):
                        if not box:
                            raise with_error_code(FubonLoginError("登入欄位無法安全填寫；未送出登入"), 'FUBON-02-212-001')
                    with error_step('FUBON-02-213-001'):
                        if box['width'] <= 0:
                            raise with_error_code(FubonLoginError("登入欄位無法安全填寫；未送出登入"), 'FUBON-02-213-001')
                    with error_step('FUBON-02-214-001'):
                        if box['height'] <= 0:
                            raise with_error_code(FubonLoginError("登入欄位無法安全填寫；未送出登入"), 'FUBON-02-214-001')
                    with error_step('FUBON-02-215-001'):
                        ordered.append((box["y"], maxlength, field))
            with error_step('FUBON-02-217-001'):
                ordered.sort(key=lambda item: item[0])
            with error_step('FUBON-02-218-001'):
                if [item[1] for item in ordered] != ['10', '10', '16']:
                    raise with_error_code(FubonLoginError("登入欄位無法安全填寫；未送出登入"), 'FUBON-02-218-001')
            with error_step('FUBON-02-219-001'):
                if len({item[0] for item in ordered}) != 3:
                    raise with_error_code(FubonLoginError("登入欄位無法安全填寫；未送出登入"), 'FUBON-02-219-001')
            with error_step('FUBON-02-220-001'):
                fields = [item[2] for item in ordered]
            with error_step('FUBON-02-221-001'):
                values = (self.creds.national_id, self.creds.user_code, self.creds.password)
            with error_step('FUBON-02-227-001'):
                for field, value, error_action in zip(
                    fields, values, ("FUBON-02-100", "FUBON-02-110", "FUBON-02-120"), strict=True,
                ):
                    with error_step(f"{error_action}-001"):
                        field.click()
                    with error_step(f"{error_action}-002"):
                        field.click(click_count=3)
                    with error_step(f"{error_action}-003"):
                        field.press("Backspace", timeout=5000)
                    with error_step(f"{error_action}-004"):
                        field.press_sequentially(value, delay=80, timeout=5000)
                    with error_step(f"{error_action}-005"):
                        entered_length = len(field.input_value())
                    if entered_length != len(value):
                        raise with_error_code(FubonLoginError("登入欄位輸入長度不符；未送出登入"), f"{error_action}-006")

            with error_step('FUBON-02-228-001'):
                captchas = tuple(bounded_locator_matches(
                    frame.locator("#m1_userCaptcha"), first_timeout_ms=5000,
                    error_ids=('FUBON-02-228-301', 'FUBON-02-228-302'),
                ))
            with error_step('FUBON-02-229-001'):
                if len(captchas) != 1:
                    raise with_error_code(FubonLoginError("驗證碼欄位無法安全填寫；未送出登入"), 'FUBON-02-229-001')
            with error_step('FUBON-02-230-001'):
                captcha_field = captchas[0]
            with error_step('FUBON-02-231-001'):
                if not captcha_field.is_visible():
                    raise with_error_code(FubonLoginError("驗證碼欄位無法安全填寫；未送出登入"), 'FUBON-02-231-001')
            with error_step('FUBON-02-232-001'):
                if not captcha_field.is_enabled():
                    raise with_error_code(FubonLoginError("驗證碼欄位無法安全填寫；未送出登入"), 'FUBON-02-232-001')
            with error_step('FUBON-02-233-001'):
                if captcha_field.get_attribute('maxlength') != '6':
                    raise with_error_code(FubonLoginError("驗證碼欄位無法安全填寫；未送出登入"), 'FUBON-02-233-001')
            with error_step('FUBON-02-234-001'):
                ocr_reason = []
                captcha = self._ocr_captcha(frame, max_attempts=1, _diagnostic=ocr_reason)
            with error_step('FUBON-02-235-001'):
                if not captcha:
                    raise with_error_code(captcha_unreadable(FubonLoginError("圖形驗證碼 OCR 失敗；未送出登入")), ocr_reason[0] if ocr_reason else 'FUBON-02-235-001')
            with error_step('FUBON-02-236-001'):
                if len(captcha) != 6:
                    raise with_error_code(captcha_unreadable(FubonLoginError("圖形驗證碼 OCR 失敗；未送出登入")), 'FUBON-02-236-001')
            with error_step('FUBON-02-237-001'):
                if not captcha.isdigit():
                    raise with_error_code(captcha_unreadable(FubonLoginError("圖形驗證碼 OCR 失敗；未送出登入")), 'FUBON-02-237-001')
            with error_step('FUBON-02-238-001'):
                captcha_field.click()
            with error_step('FUBON-02-239-001'):
                captcha_field.click(click_count=3)
            with error_step('FUBON-02-240-001'):
                captcha_field.press("Backspace", timeout=5000)
            with error_step('FUBON-02-241-001'):
                captcha_field.press_sequentially(captcha, delay=80, timeout=5000)
            with error_step('FUBON-02-242-001'):
                if len(captcha_field.input_value()) != 6:
                    raise with_error_code(FubonLoginError("驗證碼欄位輸入長度不符；未送出登入"), 'FUBON-02-242-001')

            with error_step('FUBON-02-243-001'):
                submits = tuple(bounded_locator_matches(
                    frame.locator("#btnLogin2"), first_timeout_ms=5000,
                    error_ids=('FUBON-02-243-301', 'FUBON-02-243-302'),
                ))
            with error_step('FUBON-02-244-001'):
                if len(submits) != 1:
                    raise with_error_code(FubonLoginError("找不到唯一且可操作的登入按鈕；未送出登入"), 'FUBON-02-244-001')
            with error_step('FUBON-02-245-001'):
                submit = submits[0]
            with error_step('FUBON-02-246-001'):
                if not submit.is_visible():
                    raise with_error_code(FubonLoginError("找不到唯一且可操作的登入按鈕；未送出登入"), 'FUBON-02-246-001')
            with error_step('FUBON-02-247-001'):
                if not submit.is_enabled():
                    raise with_error_code(FubonLoginError("找不到唯一且可操作的登入按鈕；未送出登入"), 'FUBON-02-247-001')
            with error_step('FUBON-02-248-001'):
                if ' '.join(submit.inner_text().split()) != '登入':
                    raise with_error_code(FubonLoginError("找不到唯一且可操作的登入按鈕；未送出登入"), 'FUBON-02-248-001')
        except FubonLoginError:
            raise
        except Exception as error:
            raise with_error_code(FubonLoginError("登入欄位無法安全填寫；未送出登入"), exception_error_id(error, bank="fubon") or 'FUBON-02-249-001') from None

        try:
            with error_step('FUBON-02-250-001'):
                submit.click(timeout=8000)
        except Exception as error:
            raise with_error_code(FubonLoginError("登入送出狀態不明；禁止自動重試"), exception_error_id(error, bank="fubon") or 'FUBON-02-251-001') from None

        try:
            with error_step('FUBON-03-214-001'):
                page.wait_for_timeout(3000)
            with error_step('FUBON-03-215-001'):
                for _ in range(25):
                    with error_step('FUBON-03-217-001'):
                        page.wait_for_timeout(1000)
                    with error_step('FUBON-03-218-001'):
                        if self._logged_in(page):
                            return
                    with error_step('FUBON-03-219-001'):
                        scopes = [
                            page,
                            *(child for child in page.frames if child is not page.main_frame),
                        ]
                    with error_step('FUBON-03-220-001'):
                        for scope in scopes:
                            with error_step('FUBON-03-221-001'):
                                for selector in (
                                    ".modal.show", "[role='dialog']", ".error", ".alert", "[role='alert']",
                                ):
                                    with error_step('FUBON-03-222-001'):
                                        checkpoints = scope.locator(selector)
                                    with error_step('FUBON-03-223-001'):
                                        if any(
                                            item.is_visible()
                                            for item in bounded_locator_matches(checkpoints, error_ids=('FUBON-03-223-301', 'FUBON-03-223-302'))
                                        ):
                                            return
        except Exception as error:
            raise with_error_code(FubonLoginError("登入後狀態無法安全確認；禁止自動重試"), exception_error_id(error, bank="fubon") or 'FUBON-03-216-001') from None

    def _fubon_content_frame(self, page, *routes, _error_role="inventory"):
        route_ids = {
            'menu': ('FUBON-05-789-001', 'FUBON-05-789-002', 'FUBON-05-789-003', 'FUBON-05-789-004', 'FUBON-05-789-005', None),
            'inventory': ('FUBON-05-790-001', 'FUBON-05-790-002', 'FUBON-05-790-003', 'FUBON-05-790-004', None, 'FUBON-05-790-006'),
            'publication': ('FUBON-05-791-001', 'FUBON-05-791-002', 'FUBON-05-791-003', 'FUBON-05-791-004', None, 'FUBON-05-791-006'),
        }[_error_role]
        route_reason = []
        matches = {}
        with error_step('FUBON-05-203-001'):
            for candidate in page.frames:
                with error_step('FUBON-05-200-001'):
                    parsed = urlsplit(candidate.url or "")
                with error_step('FUBON-05-202-001'):
                    if (
                        self._is_owned_frame(page, candidate)
                        and any(_fubon_route_matches(parsed, route, _diagnostic=route_reason, _error_ids=route_ids) for route in routes)
                    ):
                        with error_step('FUBON-05-201-001'):
                            matches[id(candidate)] = candidate
        with error_step('FUBON-05-204-001'):
            if len(matches) != 1:
                raise with_error_code(RuntimeError("fubon-twd-history-frame"), route_reason[0] if not matches and route_reason else 'FUBON-05-204-001')
        with error_step('FUBON-05-205-001'):
            return next(iter(matches.values()))

    def _open_twd_query(self, page):
        with error_step('FUBON-05-206-001'):
            page.goto(
                "https://ebank.taipeifubon.com.tw/B2C/cgequ/cgequ001/CGEQU001_Home.faces",
                wait_until="domcontentloaded", timeout=15000,
            )
        with error_step('FUBON-05-207-001'):
            page.wait_for_timeout(5000)
        with error_step('FUBON-05-208-001'):
            frame = self._fubon_content_frame(page, "/B2C/cgequ/cgequ001/CGEQU001_Home.faces", _error_role="menu")
        with error_step('FUBON-05-209-001'):
            clicked = bounded_evaluate(frame, r"""() => {
            const links = [...document.querySelectorAll('a.task_CDSQU001.menu_CDS0401')]
                .filter((a) => (a.textContent || '').trim() === '臺外幣交易明細查詢');
            if (links.length !== 1) return {ok:false, count:links.length};
            links[0].click(); return {ok:true};
        }""", _error_id='FUBON-05-209-001')
        with error_step('FUBON-05-210-001'):
            if clicked != {'ok': True}:
                raise with_error_code(RuntimeError("fubon-twd-history-navigation"), 'FUBON-05-210-001')
        with error_step('FUBON-05-211-001'):
            page.wait_for_timeout(8000)
        with error_step('FUBON-05-212-001'):
            return self._fubon_content_frame(page, "/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces", _error_role="inventory")

    @staticmethod
    def _capture_twd_response(
        response, hits: list[dict], expected_frame, form_bound: bool,
        expected_requests: list[object], preset_shapes: list[str] | None = None,
    ) -> None:
        try:
            with error_step('FUBON-05-411-001'):
                request = response.request
            with error_step('FUBON-05-412-001'):
                expected_frame = _OriginGuardProxy._unwrap(expected_frame)
            with error_step('FUBON-05-413-001'):
                if not _fubon_twd_post_matches(response.url, request.method):
                    return
            with error_step('FUBON-05-414-001'):
                hit = {
                    "status": response.status,
                    "contentType": (response.headers.get("content-type") or "").split(";", 1)[0].lower(),
                    "frameBound": request.frame is expected_frame,
                    "requestBound": any(request is expected for expected in expected_requests),
                    "_navigation": getattr(request, "is_navigation_request", lambda: False)() is True,
                    "presetBound": False,
                    "fieldsBound": False,
                    "viewStateBound": False,
                    "actionBound": False,
                    "formBound": form_bound is True,
                }
            with error_step('FUBON-05-415-001'):
                hits.append(hit)
            with error_step('FUBON-05-416-001'):
                post_data = request.post_data or ""
            with error_step('FUBON-05-417-001'):
                if len(post_data) > 32_768:
                    return
            with error_step('FUBON-05-418-001'):
                fields = parse_qsl(post_data, keep_blank_values=True)
            names = [key for key, _value in fields]
            preset_values = [value for key, value in fields if key == "checkedConvenientPeriod"]
            with error_step('FUBON-05-424-001'):
                if preset_shapes is not None:
                    with error_step('FUBON-05-423-001'):
                        if not preset_values:
                            with error_step('FUBON-05-419-001'):
                                preset_shapes.append("missing")
                        elif len(preset_values) != 1:
                            with error_step('FUBON-05-420-001'):
                                preset_shapes.append("duplicate")
                        elif not 0 < len(preset_values[0]) <= 128:
                            with error_step('FUBON-05-421-001'):
                                preset_shapes.append("mismatch")
                        else:
                            with error_step('FUBON-05-422-001'):
                                preset_shapes.append("present")
            view_states = [value for key, value in fields if key == "javax.faces.ViewState"]
            actions = [value for key, value in fields if key == "ajaxAction"]
            with error_step('FUBON-05-425-001'):
                action_bound = len(actions) == 1 and bool(actions[0])
            with error_step('FUBON-05-426-001'):
                preset_bound = len(preset_values) == 1 and 0 < len(preset_values[0]) <= 128
            with error_step('FUBON-05-427-001'):
                hit.update({
                    "_presetValue": preset_values[0] if preset_bound else None,
                    "presetBound": preset_bound,
                    "fieldsBound": sorted(names) == ["ajaxAction", "checkedConvenientPeriod", "javax.faces.ViewState"],
                    "viewStateBound": len(view_states) == 1 and bool(view_states[0]),
                    "actionBound": action_bound,
                })
        except Exception:
            return

    def _bound_twd_result_frame(self, page, submit_frame):
        with error_step('FUBON-05-213-001'):
            result_frame = self._fubon_content_frame(
                page, "/B2C/cdsqu/cdsqu001/CDSQU001_Home.faces", _error_role="publication",
            )
        with error_step('FUBON-05-214-001'):
            if result_frame is not submit_frame:
                raise with_error_code(RuntimeError("fubon-twd-history-result-frame"), 'FUBON-05-214-001')
        return result_frame

    def _collect_twd_window(self, page, frame, option: dict, window: dict) -> tuple[dict, dict]:
        control_requests, control_pending, control_failed = [], {}, []
        with error_step('FUBON-05-215-001'):
            raw_frame = _OriginGuardProxy._unwrap(frame)

        def control_request(request):
            try:
                with error_step('FUBON-05-216-001'):
                    parsed = urlsplit(request.url or "")
                with error_step('FUBON-05-219-001'):
                    if (
                        request.method == "POST"
                        and request.frame is raw_frame
                        and parsed.scheme == "https"
                        and parsed.hostname == "ebank.taipeifubon.com.tw"
                        and parsed.port in (None, 443)
                        and parsed.path == urlsplit(TWD_HISTORY_URL).path
                        and not parsed.query
                        and not parsed.fragment
                    ):
                        with error_step('FUBON-05-217-001'):
                            control_requests.append(request)
                        with error_step('FUBON-05-218-001'):
                            control_pending[id(request)] = request
            except Exception:
                return

        def control_finished(request):
            with error_step('FUBON-05-220-001'):
                control_pending.pop(id(request), None)

        def control_failed_request(request):
            with error_step('FUBON-05-222-001'):
                if id(request) in control_pending:
                    with error_step('FUBON-05-221-001'):
                        control_failed.append(request)
            with error_step('FUBON-05-223-001'):
                control_pending.pop(id(request), None)

        control_listeners = []

        def remove_control_listeners():
            failures = []
            with error_step('FUBON-07-302-001'):
                for event, listener in reversed(control_listeners):
                    try:
                        with error_step('FUBON-07-304-001'):
                            page.remove_listener(event, listener)
                        with error_step('FUBON-07-305-001'):
                            control_listeners.remove((event, listener))
                    except Exception as exc:
                        with error_step('FUBON-07-306-001'):
                            failures.append(exc)
            with error_step('FUBON-07-303-001'):
                if failures:
                    raise with_error_code(RuntimeError("fubon-twd-history-listener-cleanup"), 'FUBON-07-303-001') from failures[0]

        def finish_control_listeners():
            with error_step('FUBON-07-307-001'):
                active_error = sys.exception()
            try:
                with error_step('FUBON-07-308-001'):
                    remove_control_listeners()
            except Exception as cleanup_error:
                if active_error is None:
                    raise
                with error_step('FUBON-07-309-001'):
                    active_error.add_note(f"listener cleanup failed: {type(cleanup_error).__name__}")

        def add_control_listeners():
            with error_step('FUBON-05-235-001'):
                for event, listener in (
                    ("request", control_request),
                    ("requestfinished", control_finished),
                    ("requestfailed", control_failed_request),
                ):
                    try:
                        with error_step('FUBON-05-232-001'):
                            page.on(event, listener)
                    except Exception:
                        with error_step('FUBON-05-233-001'):
                            finish_control_listeners()
                        raise
                    with error_step('FUBON-05-234-001'):
                        control_listeners.append((event, listener))

        def wait_control_settle():
            stable_ticks, prior_count = 0, -1
            with error_step('FUBON-05-238-001'):
                for tick in range(80):
                    with error_step('FUBON-05-236-001'):
                        page.wait_for_timeout(100)
                    with error_step('FUBON-05-237-001'):
                        current_count = len(control_requests)
                    if not control_pending and current_count == prior_count:
                        stable_ticks += 1
                    else:
                        stable_ticks = 0
                    prior_count = current_count
                    if tick >= 19 and stable_ticks >= 10:
                        break
            with error_step('FUBON-05-239-001'):
                if control_pending:
                    raise with_error_code(RuntimeError("fubon-twd-history-controls"), 'FUBON-05-239-001')
            with error_step('FUBON-05-240-001'):
                if control_failed:
                    raise with_error_code(RuntimeError("fubon-twd-history-controls"), 'FUBON-05-240-001')
            with error_step('FUBON-05-241-001'):
                if stable_ticks < 10:
                    raise with_error_code(RuntimeError("fubon-twd-history-controls"), 'FUBON-05-241-001')

        with error_step('FUBON-05-242-001'):
            add_control_listeners()
        try:
            with error_step('FUBON-05-243-001'):
                selected = bounded_evaluate(frame, r"""(args) => {
                const forms=[...document.querySelectorAll('form#form1')];
                if(forms.length!==1)return {ok:false,errorId:'FUBON-05-780-001'};
                const form=forms[0];
                const one=(s)=>{const xs=[...document.querySelectorAll(s)];return xs.length===1&&form.contains(xs[0])?xs[0]:null;};
                const account=one('#form1\\:comboAccount');
                const states=[...document.querySelectorAll('[name="javax.faces.ViewState"]')];
                const submit=one('#form1\\:doValidateAndSubmit');
                let formAction='';
                try{formAction=new URL(form.getAttribute('action')||form.action,location.href).href;}catch(_e){}
                const formBound=form.method.toUpperCase()==='POST'
                    &&states.length===1&&form.contains(states[0]);
                if(!account)return {ok:false,errorId:'FUBON-05-780-002'};
                if(!submit)return {ok:false,errorId:'FUBON-05-780-003'};
                if(!formBound)return {ok:false,errorId:'FUBON-05-780-004'};
                if(!states[0].value)return {ok:false,errorId:'FUBON-05-780-005'};
                account.value=args.value;
                for(const n of ['input','change'])account['dispatch'+'Event'](new Event(n,{bubbles:true}));
                if(typeof comboAccountChange==='function')comboAccountChange();
                if(typeof checkAccountType==='function')checkAccountType();
                account['dispatch'+'Event'](new Event('blur',{bubbles:true}));
                const chosen=account.options[account.selectedIndex];
                return {ok:true,value:chosen?.value||'',text:(chosen?.textContent||'').trim(),
                    viewState:states[0].value,formBound,formAction};
            }""", {"value": option["value"]}, _error_id='FUBON-05-243-001')
            with error_step('FUBON-05-244-001'):
                if not isinstance(selected, dict):
                    raise with_error_code(RuntimeError("fubon-twd-history-controls"), 'FUBON-05-244-001')
            with error_step('FUBON-05-245-001'):
                if selected.get('ok') is not True:
                    selected_reason = selected.get('errorId')
                    raise with_error_code(RuntimeError("fubon-twd-history-controls"), selected_reason if selected_reason in (
                        'FUBON-05-780-001', 'FUBON-05-780-002', 'FUBON-05-780-003', 'FUBON-05-780-004', 'FUBON-05-780-005',
                    ) else 'FUBON-05-245-001')
            with error_step('FUBON-05-246-001'):
                if selected.get('value') != option['value']:
                    raise with_error_code(RuntimeError("fubon-twd-history-controls"), 'FUBON-05-246-001')
            with error_step('FUBON-05-247-001'):
                if re.findall('(?<!\\d)\\d{10,16}(?!\\d)', selected.get('text') or '') != [option['identity']]:
                    raise with_error_code(RuntimeError("fubon-twd-history-controls"), 'FUBON-05-247-001')
            with error_step('FUBON-05-248-001'):
                if not isinstance(selected.get('viewState'), str):
                    raise with_error_code(RuntimeError("fubon-twd-history-controls"), 'FUBON-05-248-001')
            with error_step('FUBON-05-249-001'):
                if not selected['viewState']:
                    raise with_error_code(RuntimeError("fubon-twd-history-controls"), 'FUBON-05-249-001')
            with error_step('FUBON-05-250-001'):
                if selected.get('formBound') is not True:
                    raise with_error_code(RuntimeError("fubon-twd-history-controls"), 'FUBON-05-250-001')
            with error_step('FUBON-05-251-001'):
                form_reason = []
                if not _fubon_form_action_matches(selected.get('formAction'), _diagnostic=form_reason, _error_ids=(
                    'FUBON-05-787-001', 'FUBON-05-787-002', 'FUBON-05-787-003', 'FUBON-05-787-004',
                    'FUBON-05-787-005', 'FUBON-05-787-006', 'FUBON-05-787-007', 'FUBON-05-787-008', None, 'FUBON-05-787-009',
                )):
                    raise with_error_code(RuntimeError("fubon-twd-history-controls"), form_reason[0] if form_reason else 'FUBON-05-251-001')
            with error_step('FUBON-05-252-001'):
                wait_control_settle()
        finally:
            with error_step('FUBON-07-310-001'):
                finish_control_listeners()

        def click_control(
            selector: str, *, error_action: str, expected_preset: str | None = None,
        ) -> set[tuple[str, str]]:
            before_ids, click_ids = {
                'FUBON-05-100': (('FUBON-05-782-001', 'FUBON-05-782-002', 'FUBON-05-782-003'), ('FUBON-05-782-011', 'FUBON-05-782-012', 'FUBON-05-782-013', 'FUBON-05-782-014')),
                'FUBON-05-110': (('FUBON-05-783-001', 'FUBON-05-783-002', 'FUBON-05-783-003'), ('FUBON-05-783-011', 'FUBON-05-783-012', 'FUBON-05-783-013', 'FUBON-05-783-014')),
                'FUBON-05-120': (('FUBON-05-784-001', 'FUBON-05-784-002', 'FUBON-05-784-003'), ('FUBON-05-784-011', 'FUBON-05-784-012', 'FUBON-05-784-013', 'FUBON-05-784-014')),
            }[error_action]
            with error_step(f"{error_action}-001"):
                control_requests.clear()
            with error_step(f"{error_action}-002"):
                control_pending.clear()
            with error_step(f"{error_action}-003"):
                control_failed.clear()
            with error_step(f"{error_action}-004"):
                pairs: set[tuple[str, str]] = set()
            with error_step(f"{error_action}-005"):
                before_checked = bounded_evaluate(frame, r"""(selector) => {
                const forms=[...document.querySelectorAll('form#form1')];
                const nodes=[...document.querySelectorAll(selector)];
                const result=forms.length===1&&nodes.length===1&&forms[0].contains(nodes[0])
                    ? nodes[0].checked===true : null;
                return result===null?{errorReason:forms.length!==1?'form':nodes.length!==1?'count':'owner'}:result;
            }""", selector, _error_id=f'{error_action}-005')
            with error_step(f"{error_action}-006"):
                if type(before_checked) is not bool:
                    before_reason = {'form': before_ids[0], 'count': before_ids[1], 'owner': before_ids[2]}.get(before_checked.get('errorReason')) if isinstance(before_checked, dict) else None
                    raise with_error_code(RuntimeError("fubon-twd-history-controls"), before_reason or f"{error_action}-006")
            with error_step(f"{error_action}-008"):
                if before_checked is True and expected_preset is None:
                    with error_step(f"{error_action}-007"):
                        return set()
            with error_step(f"{error_action}-009"):
                add_control_listeners()
            try:
                with error_step(f"{error_action}-010"):
                    checked = bounded_evaluate(frame, r"""(selector) => {
                    const forms=[...document.querySelectorAll('form#form1')];
                    if(forms.length!==1)return {errorReason:'form'};
                    const nodes=[...document.querySelectorAll(selector)];
                    if(nodes.length!==1)return {errorReason:'count'};
                    if(!forms[0].contains(nodes[0]))return {errorReason:'owner'};
                    nodes[0].click();
                    const checked=nodes[0].checked===true;
                    return checked?true:{errorReason:'unchecked'};
                }""", selector, _error_id=f'{error_action}-010')
                with error_step(f"{error_action}-011"):
                    if checked is not True:
                        click_reason = {'form': click_ids[0], 'count': click_ids[1], 'owner': click_ids[2], 'unchecked': click_ids[3]}.get(checked.get('errorReason')) if isinstance(checked, dict) else None
                        raise with_error_code(RuntimeError("fubon-twd-history-controls"), click_reason or f"{error_action}-011")
                with error_step(f"{error_action}-012"):
                    wait_control_settle()
                with error_step(f"{error_action}-018"):
                    for request in control_requests:
                        with error_step(f"{error_action}-013"):
                            post_data = request.post_data or ""
                        with error_step(f"{error_action}-014"):
                            if len(post_data) > 32_768:
                                continue
                        with error_step(f"{error_action}-015"):
                            fields = parse_qsl(post_data, keep_blank_values=True)
                        names = [name for name, _value in fields]
                        actions = [value for name, value in fields if name == "ajaxAction"]
                        presets = [value for name, value in fields if name == "checkedConvenientPeriod"]
                        with error_step(f"{error_action}-017"):
                            if (
                                sorted(names) == ["ajaxAction", "checkedConvenientPeriod", "javax.faces.ViewState"]
                                and len(actions) == 1
                                and bool(actions[0])
                                and len(presets) == 1
                                and bool(presets[0])
                            ):
                                with error_step(f"{error_action}-016"):
                                    pairs.add((actions[0], presets[0]))
                with error_step(f"{error_action}-019"):
                    if len(control_requests) != 1:
                        raise with_error_code(RuntimeError("fubon-twd-history-controls"), f"{error_action}-019")
                with error_step(f"{error_action}-020"):
                    if len(pairs) != 1:
                        raise with_error_code(RuntimeError("fubon-twd-history-controls"), f"{error_action}-020")
                with error_step(f"{error_action}-021"):
                    _action, preset = next(iter(pairs))
                with error_step(f"{error_action}-022"):
                    if not preset:
                        raise with_error_code(RuntimeError("fubon-twd-history-controls"), f"{error_action}-022")
                with error_step(f"{error_action}-023"):
                    if len(preset) > 128:
                        raise with_error_code(RuntimeError("fubon-twd-history-controls"), f"{error_action}-023")
            finally:
                with error_step(f"{error_action}-024"):
                    finish_control_listeners()
            return pairs

        with error_step('FUBON-05-278-001'):
            click_control("#form1\\:rdoTxDetail", error_action="FUBON-05-100")
        with error_step('FUBON-05-279-001'):
            click_control("#form1\\:rdoFast", error_action="FUBON-05-110")
        with error_step('FUBON-05-280-001'):
            query_control_pairs = click_control(
                f"#form1\\:{window['preset']}", expected_preset=window["preset"], error_action="FUBON-05-120",
            )
        with error_step('FUBON-05-281-001'):
            settled = bounded_evaluate(frame, r"""(args) => {
            const forms=[...document.querySelectorAll('form#form1')];
            if(forms.length!==1)return {errorId:'FUBON-05-781-001'};
            const form=forms[0];
            const one=(s)=>{const xs=[...document.querySelectorAll(s)];return xs.length===1&&form.contains(xs[0])?xs[0]:null;};
            const account=one('#form1\\:comboAccount'),detail=one('#form1\\:rdoTxDetail');
            const fast=one('#form1\\:rdoFast'),preset=one(`#form1\\:${CSS.escape(args.preset)}`);
            const submit=one('#form1\\:doValidateAndSubmit');
            const states=[...document.querySelectorAll('[name="javax.faces.ViewState"]')];
            const chosen=account?.options[account.selectedIndex];
            let formAction='';
            try{formAction=new URL(form.getAttribute('action')||form.action,location.href).href;}catch(_e){}
            const formBound=form.method.toUpperCase()==='POST'&&states.length===1&&form.contains(states[0]);
            if(!account)return {errorId:'FUBON-05-781-002'};
            if(!detail)return {errorId:'FUBON-05-781-003'};
            if(!fast)return {errorId:'FUBON-05-781-004'};
            if(!preset)return {errorId:'FUBON-05-781-005'};
            if(!submit)return {errorId:'FUBON-05-781-006'};
            if(!formBound)return {errorId:'FUBON-05-781-007'};
            return {value:chosen?.value||'',text:(chosen?.textContent||'').trim(),
                detail:detail.checked,fast:fast.checked,preset:preset.checked,
                viewState:states[0].value,formBound,formAction};
        }""", {"preset": window["preset"]}, _error_id='FUBON-05-281-001')
        settled_reason = settled.get('errorId') if isinstance(settled, dict) else None
        if settled_reason in ('FUBON-05-781-001', 'FUBON-05-781-002', 'FUBON-05-781-003', 'FUBON-05-781-004', 'FUBON-05-781-005', 'FUBON-05-781-006', 'FUBON-05-781-007'):
            settled = None
        else:
            settled_reason = None
        with error_step('FUBON-05-282-001'):
            settled_guard = _fubon_settled_control_guard(settled, option, query_control_pairs, raise_on_error=True, _shape_error_id=settled_reason)
        with error_step('FUBON-05-283-001'):
            if settled_guard is not None:
                raise with_error_code(RuntimeError(settled_guard), 'FUBON-05-283-001')
        controls = settled

        with error_step('FUBON-05-284-001'):
            before_result = bounded_evaluate(frame, r"""() => {
            const forms=[...document.querySelectorAll('form#form1')], form=forms.length===1?forms[0]:null;
            const headers=['帳務日期','交易時間','摘要','支出金額','存入金額','即時餘額','附註'];
            const directRows=(table)=>[...table.querySelectorAll(':scope > tr,:scope > thead > tr,:scope > tbody > tr,:scope > tfoot > tr')];
            const directCells=(row)=>[...row.querySelectorAll(':scope > th,:scope > td')];
            const cellText=(c)=>{const k=c.cloneNode(true);k.querySelectorAll('script,style,noscript').forEach(n=>n.remove());return (k.textContent||'').trim().replaceAll('\u3000','');};
            const cellTexts=(row)=>directCells(row).map(cellText);
            const isHeader=(row)=>{const values=cellTexts(row);return values.length===headers.length&&values.every((value,index)=>value===headers[index]);};
            const tables=[...document.querySelectorAll('table')].filter(table=>form?.contains(table)&&!table.closest('.popup_com')&&directRows(table).some(isHeader));
            const labels=new Set(['查無相關資料','查無交易資料']);
            const empty=[...document.querySelectorAll('*')].filter(el=>form?.contains(el)&&labels.has((el.textContent||'').trim())&&![...el.children].some(c=>labels.has((c.textContent||'').trim())));
            const normalize=(value)=>(value||'').trim().replace(/\s+/g,' ');
            const signature=JSON.stringify({
                tables:tables.map(el=>normalize(el.innerText||el.textContent)),
                empty:empty.map(el=>normalize(el.textContent)),
            });
            const hash=(text)=>{let value=2166136261;for(let i=0;i<text.length;i++){value^=text.charCodeAt(i);value=Math.imul(value,16777619);}return(value>>>0).toString(16).padStart(8,'0');};
            const present=tables.length>0||empty.length>0;
            return {present,digest:hash(signature)};
        }""", _error_id='FUBON-05-284-001')
        with error_step('FUBON-05-285-001'):
            if not isinstance(before_result, dict):
                raise with_error_code(RuntimeError("fubon-twd-history-result-freshness"), 'FUBON-05-285-001')
        with error_step('FUBON-05-286-001'):
            if set(before_result) != {'present', 'digest'}:
                raise with_error_code(RuntimeError("fubon-twd-history-result-freshness"), 'FUBON-05-286-001')
        with error_step('FUBON-05-287-001'):
            if type(before_result.get('present')) is not bool:
                raise with_error_code(RuntimeError("fubon-twd-history-result-freshness"), 'FUBON-05-287-001')
        with error_step('FUBON-05-288-001'):
            if not isinstance(before_result.get('digest'), str):
                raise with_error_code(RuntimeError("fubon-twd-history-result-freshness"), 'FUBON-05-288-001')
        with error_step('FUBON-05-289-001'):
            if re.fullmatch('[0-9a-f]{8}', before_result['digest']) is None:
                raise with_error_code(RuntimeError("fubon-twd-history-result-freshness"), 'FUBON-05-289-001')
        hits = []
        with error_step('FUBON-05-290-001'):
            preset_shapes: list[str] = []
        with error_step('FUBON-05-291-001'):
            submit_pending: set[int] = set()
        with error_step('FUBON-05-292-001'):
            submit_requests: list[object] = []
        frame_navs = 0

        def submit_navigated(navigated_frame) -> None:
            nonlocal frame_navs
            with error_step('FUBON-05-293-001'):
                if navigated_frame is _OriginGuardProxy._unwrap(frame):
                    frame_navs += 1
        submit_request_count = 0
        submit_failed_flag = False

        def submit_request(request) -> None:
            nonlocal submit_request_count
            with error_step('FUBON-05-296-001'):
                if _fubon_twd_post_matches(request.url, request.method):
                    submit_request_count += 1
                    with error_step('FUBON-05-294-001'):
                        submit_requests.append(request)
                    with error_step('FUBON-05-295-001'):
                        submit_pending.add(id(request))

        def submit_finished(request) -> None:
            with error_step('FUBON-05-298-001'):
                if _fubon_twd_post_matches(request.url, request.method):
                    with error_step('FUBON-05-297-001'):
                        submit_pending.discard(id(request))

        def submit_failed(request) -> None:
            nonlocal submit_failed_flag
            with error_step('FUBON-05-300-001'):
                if _fubon_twd_post_matches(request.url, request.method):
                    submit_failed_flag = True
                    with error_step('FUBON-05-299-001'):
                        submit_pending.discard(id(request))

        with error_step('FUBON-05-301-001'):
            listener = lambda response: self._capture_twd_response(
                response, hits, frame, controls["formBound"], submit_requests, preset_shapes,
            )
        submit_listeners = (
            ("request", submit_request),
            ("requestfinished", submit_finished),
            ("requestfailed", submit_failed),
            ("response", listener),
            ("framenavigated", submit_navigated),
        )
        registered_submit_listeners = []

        def clear_dom_guard(scope) -> None:
            try:
                with error_step('FUBON-07-311-001'):
                    bounded_evaluate(scope, r"""() => {
                    window.__hermesFubonDomGuard?.disconnect();
                    delete window.__hermesFubonDomGuard;
                    delete window.__hermesFubonDomGuardDocument;
                    delete window.__hermesFubonDomGuardMutations;
                    delete window.__hermesFubonSubmitDocument;
                }""", _error_id='FUBON-07-311-001')
            except Exception:
                try:
                    with error_step('FUBON-07-312-001'):
                        page.close()
                except Exception:
                    pass
                raise

        try:
            with error_step('FUBON-05-306-001'):
                for event, callback in submit_listeners:
                    with error_step('FUBON-05-304-001'):
                        page.on(event, callback)
                    with error_step('FUBON-05-305-001'):
                        registered_submit_listeners.append((event, callback))
            with error_step('FUBON-05-307-001'):
                radio_submitted = bounded_evaluate(frame, r"""(requested) => {
                const forms=[...document.querySelectorAll('form#form1')];
                if(forms.length!==1)return {errorId:'FUBON-05-785-001'};
                const form=forms[0], nodes=[...document.querySelectorAll(`#form1\\:${CSS.escape(requested)}`)];
                if(nodes.length!==1)return {errorId:'FUBON-05-785-002'};
                if(!form.contains(nodes[0]))return {errorId:'FUBON-05-785-003'};
                if(!nodes[0].name)return {errorId:'FUBON-05-785-004'};
                const preset=nodes[0];
                const checked=[...form.querySelectorAll('input[type="radio"]')]
                    .filter(node=>node.name===preset.name&&node.checked);
                const submits=[...document.querySelectorAll('#form1\\:doValidateAndSubmit')];
                if(preset.checked!==true)return {errorId:'FUBON-05-785-005'};
                if(checked.length!==1)return {errorId:'FUBON-05-785-006'};
                if(checked[0]!==preset)return {errorId:'FUBON-05-785-007'};
                if(submits.length!==1)return {errorId:'FUBON-05-785-008'};
                if(!form.contains(submits[0]))return {errorId:'FUBON-05-785-009'};
                window.__hermesFubonSubmitDocument=document;
                submits[0].click();
                return true;
            }""", window["preset"], _error_id='FUBON-05-307-001')
            with error_step('FUBON-05-308-001'):
                if radio_submitted is not True:
                    submit_reason = radio_submitted.get('errorId') if isinstance(radio_submitted, dict) else None
                    raise with_error_code(RuntimeError("fubon-twd-history-controls-preset"), submit_reason if submit_reason in (
                        'FUBON-05-785-001', 'FUBON-05-785-002', 'FUBON-05-785-003', 'FUBON-05-785-004', 'FUBON-05-785-005', 'FUBON-05-785-006', 'FUBON-05-785-007', 'FUBON-05-785-008', 'FUBON-05-785-009',
                    ) else 'FUBON-05-308-001')
            with error_step('FUBON-05-309-001'):
                page.wait_for_timeout(9000)
            stable_ticks = 0
            with error_step('FUBON-05-310-001'):
                prior_count = len(hits)
            with error_step('FUBON-05-314-001'):
                for _ in range(120):
                    with error_step('FUBON-05-311-001'):
                        page.wait_for_timeout(100)
                    if not hits or submit_pending:
                        stable_ticks = 0
                        continue
                    with error_step('FUBON-05-313-001'):
                        if len(hits) == prior_count:
                            stable_ticks += 1
                        else:
                            with error_step('FUBON-05-312-001'):
                                prior_count = len(hits)
                            stable_ticks = 0
                    if stable_ticks >= 5:
                        break
            # The native submit is a full-form POST; Fubon answers it with one
            # HTML document for the same frame, then the new document's AJAX.
            with error_step('FUBON-05-315-001'):
                navigation_bound = (
                    len(hits) >= 2
                    and hits[0].get("contentType") == "text/html"
                    and sum(hit.get("contentType") == "text/html" for hit in hits) == 1
                    and hits[0].get("status") == 200
                    and hits[0].get("frameBound") is True
                    and hits[0].get("requestBound") is True
                    and hits[0].get("_navigation") is True
                    and frame_navs == 1
                )
            with error_step('FUBON-05-316-001'):
                guarded = bounded_evaluate(frame, r"""(navigated) => {
                if(!navigated&&window.__hermesFubonSubmitDocument!==document)return {errorId:'FUBON-05-786-001'};
                window.__hermesFubonSubmitDocument=document;
                window.__hermesFubonDomGuard?.disconnect();
                window.__hermesFubonDomGuardDocument=document;
                window.__hermesFubonDomGuardMutations=0;
                window.__hermesFubonDomGuard=new MutationObserver(records=>{
                    window.__hermesFubonDomGuardMutations+=records.length;
                });
                window.__hermesFubonDomGuard.observe(document.documentElement,{
                    subtree:true,childList:true,attributes:true,characterData:true,
                });
                return true;
            }""", navigation_bound, _error_id='FUBON-05-316-001')
            with error_step('FUBON-05-317-001'):
                if guarded is not True:
                    guard_reason = guarded.get('errorId') if isinstance(guarded, dict) else None
                    raise with_error_code(RuntimeError("fubon-twd-history-result-mutated"), 'FUBON-05-786-001' if guard_reason == 'FUBON-05-786-001' else 'FUBON-05-317-001')
        finally:
            with error_step('FUBON-07-313-001'):
                active_error = sys.exception()
            cleanup_errors = []
            with error_step('FUBON-07-314-001'):
                for event, callback in reversed(registered_submit_listeners):
                    try:
                        with error_step('FUBON-07-315-001'):
                            page.remove_listener(event, callback)
                    except Exception as cleanup_error:
                        with error_step('FUBON-07-316-001'):
                            cleanup_errors.append(cleanup_error)
            with error_step('FUBON-07-317-001'):
                if active_error is not None:
                    try:
                        with error_step('FUBON-07-318-001'):
                            clear_dom_guard(frame)
                    except Exception as cleanup_error:
                        with error_step('FUBON-07-319-001'):
                            cleanup_errors.append(cleanup_error)
            with error_step('FUBON-07-320-001'):
                if cleanup_errors:
                    try:
                        with error_step('FUBON-07-323-001'):
                            clear_dom_guard(frame)
                    except Exception as cleanup_error:
                        with error_step('FUBON-07-324-001'):
                            cleanup_errors.append(cleanup_error)
                    with error_step('FUBON-07-321-001'):
                        if active_error is None:
                            raise with_error_code(RuntimeError("fubon-twd-history-listener-cleanup"), 'FUBON-07-321-001') from cleanup_errors[0]
                    with error_step('FUBON-07-322-001'):
                        for cleanup_error in cleanup_errors:
                            with error_step('FUBON-07-325-001'):
                                active_error.add_note(
                                    f"listener cleanup failed: {type(cleanup_error).__name__}"
                                )
        try:
            with error_step('FUBON-05-331-001'):
                if not hits:
                    raise with_error_code(RuntimeError("fubon-twd-history-transport-no-response"), 'FUBON-05-331-001')
            with error_step('FUBON-05-332-001'):
                if len(hits) > 20:
                    raise with_error_code(RuntimeError("fubon-twd-history-transport-too-many-responses"), 'FUBON-05-332-001')
            with error_step('FUBON-05-333-001'):
                bound_hits = [
                    hit for hit in hits
                    if hit.get("status") == 200
                    and hit.get("contentType") == "text/plain"
                    and all(hit.get(key) is True for key in (
                        "frameBound", "requestBound", "presetBound", "fieldsBound", "viewStateBound",
                        "actionBound", "formBound",
                    ))
                ]
            with error_step('FUBON-05-339-001'):
                if not bound_hits:
                    with error_step('FUBON-05-334-001'):
                        preset_guard = _fubon_preset_diagnostic_guard(hits, preset_shapes)
                    with error_step('FUBON-05-335-001'):
                        if preset_guard is not None:
                            raise with_error_code(RuntimeError(preset_guard), {
                                        'fubon-twd-history-transport-no-frame-bound-response': 'FUBON-05-520-001',
                                        'fubon-twd-history-transport-no-bound-response': 'FUBON-05-521-001',
                                        'fubon-twd-history-transport-no-preset-bound-response': 'FUBON-05-522-001',
                                        'fubon-twd-history-transport-no-fields-bound-response': 'FUBON-05-523-001',
                                        'fubon-twd-history-transport-no-viewstate-bound-response': 'FUBON-05-524-001',
                                        'fubon-twd-history-transport-no-action-bound-response': 'FUBON-05-525-001',
                                        'fubon-twd-history-transport-no-form-bound-response': 'FUBON-05-526-001',
                                        'fubon-twd-history-transport-preset-missing': 'FUBON-05-527-001',
                                        'fubon-twd-history-transport-preset-duplicate': 'FUBON-05-528-001',
                                        'fubon-twd-history-transport-preset-mismatch': 'FUBON-05-529-001',
                                    }.get(preset_guard, 'FUBON-05-335-001'))
                    guards = (
                        ("frameBound", "fubon-twd-history-transport-no-frame-bound-response"),
                        ("requestBound", "fubon-twd-history-transport-no-bound-response"),
                        ("presetBound", "fubon-twd-history-transport-no-preset-bound-response"),
                        ("fieldsBound", "fubon-twd-history-transport-no-fields-bound-response"),
                        ("viewStateBound", "fubon-twd-history-transport-no-viewstate-bound-response"),
                        ("actionBound", "fubon-twd-history-transport-no-action-bound-response"),
                        ("formBound", "fubon-twd-history-transport-no-form-bound-response"),
                    )
                    with error_step('FUBON-05-337-001'):
                        for key, guard in guards:
                            with error_step('FUBON-05-336-001'):
                                if not any((hit.get(key) is True for hit in hits)):
                                    raise with_error_code(RuntimeError(guard), {
                                        'fubon-twd-history-transport-no-frame-bound-response': 'FUBON-05-520-001',
                                        'fubon-twd-history-transport-no-bound-response': 'FUBON-05-521-001',
                                        'fubon-twd-history-transport-no-preset-bound-response': 'FUBON-05-522-001',
                                        'fubon-twd-history-transport-no-fields-bound-response': 'FUBON-05-523-001',
                                        'fubon-twd-history-transport-no-viewstate-bound-response': 'FUBON-05-524-001',
                                        'fubon-twd-history-transport-no-action-bound-response': 'FUBON-05-525-001',
                                        'fubon-twd-history-transport-no-form-bound-response': 'FUBON-05-526-001',
                                        'fubon-twd-history-transport-preset-missing': 'FUBON-05-527-001',
                                        'fubon-twd-history-transport-preset-duplicate': 'FUBON-05-528-001',
                                        'fubon-twd-history-transport-preset-mismatch': 'FUBON-05-529-001',
                                    }.get(guard, 'FUBON-05-336-001'))
                    raise with_error_code(RuntimeError("fubon-twd-history-transport-no-bound-response"), 'FUBON-05-338-001')
            with error_step('FUBON-05-340-001'):
                all_responses_bound = all(
                    hit.get("status") == 200
                    and hit.get("frameBound") is True
                    and hit.get("requestBound") is True
                    and hit.get("formBound") is True
                    for hit in hits
                )
            with error_step('FUBON-05-341-001'):
                bound_hit_ids = {id(hit) for hit in bound_hits}
            with error_step('FUBON-05-342-001'):
                if not all_responses_bound:
                    raise with_error_code(RuntimeError("fubon-twd-history-transport-no-bound-response"), 'FUBON-05-342-001')
            with error_step('FUBON-05-343-001'):
                if any((hit.get('contentType') == 'text/plain' and id(hit) not in bound_hit_ids for hit in hits)):
                    raise with_error_code(RuntimeError("fubon-twd-history-transport-no-bound-response"), 'FUBON-05-343-001')
            with error_step('FUBON-05-344-001'):
                preset_values = {hit.pop("_presetValue", None) for hit in bound_hits}
            with error_step('FUBON-05-345-001'):
                if len(preset_values) != 1:
                    raise with_error_code(RuntimeError("fubon-twd-history-transport-preset-mismatch"), 'FUBON-05-345-001')
            with error_step('FUBON-05-346-001'):
                if not next(iter(preset_values)):
                    raise with_error_code(RuntimeError("fubon-twd-history-transport-preset-mismatch"), 'FUBON-05-346-001')
            with error_step('FUBON-05-347-001'):
                if submit_pending:
                    raise with_error_code(RuntimeError("fubon-twd-history-transport-pending"), 'FUBON-05-347-001')
            with error_step('FUBON-05-348-001'):
                if submit_failed_flag:
                    raise with_error_code(RuntimeError("fubon-twd-history-transport-failed"), 'FUBON-05-348-001')
            with error_step('FUBON-05-349-001'):
                if submit_request_count != len(hits):
                    raise with_error_code(RuntimeError("fubon-twd-history-transport-request-count"), 'FUBON-05-349-001')
            with error_step('FUBON-05-350-001'):
                if stable_ticks < 5:
                    raise with_error_code(RuntimeError("fubon-twd-history-transport-quiescence"), 'FUBON-05-350-001')
            with error_step('FUBON-05-351-001'):
                transport = {
                    **{key: value for key, value in bound_hits[0].items() if not key.startswith("_")},
                    "responseCount": len(hits),
                    "matchingResponseCount": len(bound_hits),
                    "allResponsesBound": True,
                }
            with error_step('FUBON-05-352-001'):
                if transport['status'] != 200:
                    raise with_error_code(RuntimeError("fubon-twd-history-transport"), 'FUBON-05-352-001')
            with error_step('FUBON-05-353-001'):
                if transport['contentType'] != 'text/plain':
                    raise with_error_code(RuntimeError("fubon-twd-history-transport"), 'FUBON-05-353-001')
            with error_step('FUBON-05-354-001'):
                if transport['frameBound'] is not True:
                    raise with_error_code(RuntimeError("fubon-twd-history-transport"), 'FUBON-05-354-001')
            with error_step('FUBON-05-355-001'):
                if transport['requestBound'] is not True:
                    raise with_error_code(RuntimeError("fubon-twd-history-transport"), 'FUBON-05-355-001')
            with error_step('FUBON-05-356-001'):
                if transport['presetBound'] is not True:
                    raise with_error_code(RuntimeError("fubon-twd-history-transport"), 'FUBON-05-356-001')
            with error_step('FUBON-05-357-001'):
                if transport['fieldsBound'] is not True:
                    raise with_error_code(RuntimeError("fubon-twd-history-transport"), 'FUBON-05-357-001')
            with error_step('FUBON-05-358-001'):
                if transport['viewStateBound'] is not True:
                    raise with_error_code(RuntimeError("fubon-twd-history-transport"), 'FUBON-05-358-001')
            with error_step('FUBON-05-359-001'):
                if transport['actionBound'] is not True:
                    raise with_error_code(RuntimeError("fubon-twd-history-transport"), 'FUBON-05-359-001')
            with error_step('FUBON-05-360-001'):
                if transport['formBound'] is not True:
                    raise with_error_code(RuntimeError("fubon-twd-history-transport"), 'FUBON-05-360-001')
            with error_step('FUBON-05-361-001'):
                if transport['allResponsesBound'] is not True:
                    raise with_error_code(RuntimeError("fubon-twd-history-transport"), 'FUBON-05-361-001')
        except Exception as active_error:
            try:
                with error_step('FUBON-05-362-001'):
                    clear_dom_guard(frame)
            except Exception as cleanup_error:
                with error_step('FUBON-05-363-001'):
                    active_error.add_note(
                        f"DOM guard cleanup failed: {type(cleanup_error).__name__}"
                    )
            raise
        try:
            with error_step('FUBON-05-364-001'):
                result_frame = self._bound_twd_result_frame(page, frame)
            with error_step('FUBON-05-365-001'):
                snapshot = bounded_evaluate(result_frame, r"""(args) => {
            const visible=(el)=>{const r=el.getBoundingClientRect();if(r.width<=0||r.height<=0)return false;
                for(let n=el;n;n=n.parentElement){const s=getComputedStyle(n);if(s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||Number(s.opacity)===0||n.hidden||(n.getAttribute('aria-hidden')||'').toLowerCase()==='true')return false;}return true;};
            const forms=[...document.querySelectorAll('form#form1')], form=forms.length===1?forms[0]:null;
            const labels=new Set(['查無相關資料','查無交易資料']);
            const empty=[...document.querySelectorAll('*')].filter(el=>form?.contains(el)&&visible(el)&&labels.has((el.textContent||'').trim())&&![...el.children].some(c=>labels.has((c.textContent||'').trim())));
            const headers=['帳務日期','交易時間','摘要','支出金額','存入金額','即時餘額','附註'];
            const directRows=(table)=>[...table.querySelectorAll(':scope > tr,:scope > thead > tr,:scope > tbody > tr,:scope > tfoot > tr')];
            const directCells=(row)=>[...row.querySelectorAll(':scope > th,:scope > td')];
            const cellText=(c)=>{const k=c.cloneNode(true);k.querySelectorAll('script,style,noscript').forEach(n=>n.remove());return (k.textContent||'').trim().replaceAll('\u3000','');};
            const cellTexts=(row)=>directCells(row).map(cellText);
            const isHeader=(row)=>{const values=cellTexts(row);return values.length===headers.length&&values.every((value,index)=>value===headers[index]);};
            const allCandidates=[...document.querySelectorAll('table')].filter(table=>form?.contains(table)&&!table.closest('.popup_com')&&directRows(table).some(isHeader));
            const candidates=allCandidates.filter(table=>{const header=directRows(table).find(isHeader);return visible(table)&&visible(header)&&directCells(header).every(visible);});
            const hiddenGridCount=allCandidates.length-candidates.length;
            const grid=candidates.length===1?candidates[0]:null, projected=[];
            let rawDataRowCount=0, malformedRowCount=0, hiddenRowCount=0, hiddenCellCount=0;
            if(grid){const rows=directRows(grid), headerAt=rows.findIndex(isHeader);
                for(const row of rows.slice(headerAt+1)){const cells=[...row.querySelectorAll(':scope > th,:scope > td')], values=cellTexts(row);
                    if(!values.some(Boolean))continue; rawDataRowCount++;
                    if(!visible(row)){hiddenRowCount++;continue;} const hidden=cells.filter(c=>!visible(c)).length; hiddenCellCount+=hidden;
                    const dates=values.filter(value=>/^\*?20\d{2}\/\d{1,2}\/\d{1,2}$/.test(value));
                    if(hidden||values.length!==7||dates.length!==1){malformedRowCount++;continue;} projected.push(values);
                }}
            const pagerControls=[...document.querySelectorAll('a,button,input:not([type="radio"]):not([type="hidden"]):not([type="checkbox"]),select,[role="button"]')].filter(el=>form?.contains(el)&&!el.closest('.ui-datepicker,[class*="calendar" i]')).filter(el=>{
                const raw=(el.textContent||el.value||'').trim();
                const pageNumber=/^\d{1,3}$/.test(raw)&&Number(raw)>1;
                const meta=[el.textContent,el.value,el.title,el.getAttribute('aria-label'),el.getAttribute('rel'),el.getAttribute('href'),el.getAttribute('onclick'),el.id,el.getAttribute('class'),el.getAttribute('data-page')].filter(Boolean).join(' ');
                return pageNumber||/(?:下一頁|下頁|next\s*page|page[-_: ]?next|pagenext|rel[=: ]?next|[?&]page=[2-9]\d*)/i.test(meta);
            });
            const pagerStructures=[...document.querySelectorAll('[class*="pagination" i],[class*="paginator" i],[id*="pagination" i],[id*="paginator" i],[aria-label*="pagination" i],[rel="next" i],[data-page]:not([data-page="1"])')].filter(el=>form?.contains(el));
            const pagerNodes=[...new Set([...pagerControls,...pagerStructures])];
            const account=[...document.querySelectorAll('select#form1\\:comboAccount')].filter(el=>form?.contains(el)), selected=account.length===1?account[0].options[account[0].selectedIndex]:null;
            const selectedIds=selected?(selected.textContent||'').match(/(?<!\d)\d{10,16}(?!\d)/g)||[]:[];
            const own=(el)=>[...el.childNodes].filter(n=>n.nodeType===Node.TEXT_NODE).map(n=>n.textContent||'').join(' ');
            const text=document.body?.innerText||'';
            const totalAdjacentToGrid=(el)=>{
                if(!grid)return false;
                let node=el;
                while(node.parentElement&&node.parentElement!==grid.parentElement)node=node.parentElement;
                if(node.parentElement!==grid.parentElement)return false;
                const siblings=[...grid.parentElement.children];
                return Math.abs(siblings.indexOf(node)-siblings.indexOf(grid))===1;
            };
            const nativeTotalMarkers=form?[form,...form.querySelectorAll('td,th,label,span,div,p')].map(el=>({el,match:visible(el)?own(el).match(/^\s*共\s*([\d,]+)\s*筆\s*$/):null})).filter(item=>item.match&&totalAdjacentToGrid(item.el)):[];
            const nativeTotals=nativeTotalMarkers.length===1?[Number(nativeTotalMarkers[0].match[1].replaceAll(',',''))].filter(Number.isSafeInteger):[];
            const pageInputs=['resultGrid:dataGridCurrentPage','resultGrid:dataGridCurrentPageSize'].map(id=>[...document.querySelectorAll('input[type="hidden"]')].filter(el=>el.id===id&&form?.contains(el)));
            const singlePageProof=!!grid&&pageInputs.every(list=>list.length===1)&&pageInputs[0][0].value==='1'&&/^[1-9]\d{0,3}$/.test(pageInputs[1][0].value)&&rawDataRowCount<Number(pageInputs[1][0].value);
            const nativeTotalFound=nativeTotals.length===1;
            const totalCount=nativeTotalFound?nativeTotals[0]:(singlePageProof?rawDataRowCount:(empty.length===1?0:-1));
            const busyText=/(?:資料(?:載入|查詢|處理)中|載入中|查詢中|處理中|請稍候|請稍待|系統忙碌|system is busy|loading|processing|querying|waiting|\bbusy\b)/i.test(text);
            const busy=busyText||[document.documentElement,...document.querySelectorAll('*')].some(el=>visible(el)&&((el.getAttribute('aria-busy')||'').toLowerCase()==='true'||(el.getAttribute('role')||'').toLowerCase()==='progressbar'||el.tagName.toLowerCase()==='progress'||/(?:loading|loader|spinner|progress|processing|querying|waiting|busy|blockui)/i.test([el.id,el.getAttribute('class')].filter(Boolean).join(' '))));
            const structuralErrors=[...document.querySelectorAll('.error,.errorMessage,.alert,.ui-message-error,.ui-messages-error,[role="alert"],dialog,[role="dialog"],[aria-invalid="true"]')].filter(visible);
            const failed=structuralErrors.length>0||/(?:錯誤|失敗|異常|逾時|失效|重新登入|請重新查詢|請稍後再試|連線中斷|連線失敗|無法處理|system error|\berror\b|timeout|expired|failed|retry|try again|disconnected)/i.test(text);
            const digestEmpty=[...document.querySelectorAll('*')].filter(el=>form?.contains(el)&&labels.has((el.textContent||'').trim())&&![...el.children].some(c=>labels.has((c.textContent||'').trim())));
            const normalize=(value)=>(value||'').trim().replace(/\s+/g,' ');
            const signature=JSON.stringify({
                tables:allCandidates.map(el=>normalize(el.innerText||el.textContent)),
                empty:digestEmpty.map(el=>normalize(el.textContent)),
            });
            const hash=(value)=>{let result=2166136261;for(let i=0;i<value.length;i++){result^=value.charCodeAt(i);result=Math.imul(result,16777619);}return(result>>>0).toString(16).padStart(8,'0');};
            const resultDigest=hash(signature);
            return {href:location.href,failed,busy,selectedValue:selected?.value||'',selectedIdentity:selectedIds.length===1?selectedIds[0]:'',resultDigest,hasGrid:!!grid,gridCandidateCount:candidates.length,hiddenGridCount,pagerNodeCount:pagerNodes.length,structuralErrorCount:structuralErrors.length,gridRows:projected,gridRowCount:projected.length,rawDataRowCount,malformedRowCount,hiddenRowCount,hiddenCellCount,totalCount,nativeTotalFound,singlePageProof,nativeTotalMarkerCount:nativeTotalMarkers.length,gridText:grid?'structured':'',emptyMarker:empty.length===1?(empty[0].textContent||'').trim():null,pager:{present:pagerNodes.length>0,actionableNext:pagerNodes.length}};
            }""", {"identity": option["identity"], "preset": window["preset"], "start": window["start"], "end": window["end"]}, _error_id='FUBON-05-365-001')
            with error_step('FUBON-05-368-001'):
                if isinstance(snapshot, dict):
                    with error_step('FUBON-05-366-001'):
                        result_digest = snapshot.pop("resultDigest", None)
                    with error_step('FUBON-05-367-001'):
                        snapshot["resultFresh"] = (
                            isinstance(result_digest, str)
                            and re.fullmatch(r"[0-9a-f]{8}", result_digest) is not None
                            and (
                                before_result["present"] is False
                                or result_digest != before_result["digest"]
                            )
                        )
            with error_step('FUBON-05-369-001'):
                page.wait_for_timeout(500)
            with error_step('FUBON-05-370-001'):
                dom_guard = bounded_evaluate(result_frame, r"""() => {
                const observer=window.__hermesFubonDomGuard;
                const records=observer?.takeRecords()||[];
                const result={
                    sameDocument:window.__hermesFubonDomGuardDocument===document,
                    submitDocument:window.__hermesFubonSubmitDocument===document,
                    mutations:(window.__hermesFubonDomGuardMutations||0)+records.length,
                };
                observer?.disconnect();
                delete window.__hermesFubonDomGuard;
                delete window.__hermesFubonDomGuardDocument;
                delete window.__hermesFubonDomGuardMutations;
                delete window.__hermesFubonSubmitDocument;
                return result;
            }""", _error_id='FUBON-05-370-001')
        except Exception:
            try:
                with error_step('FUBON-05-371-001'):
                    clear_dom_guard(frame)
            except Exception:
                pass
            raise
        with error_step('FUBON-05-372-001'):
            if not isinstance(dom_guard, dict):
                raise with_error_code(RuntimeError("fubon-twd-history-result-mutated"), 'FUBON-05-372-001')
        with error_step('FUBON-05-373-001'):
            if dom_guard.get('sameDocument') is not True:
                raise with_error_code(RuntimeError("fubon-twd-history-result-mutated"), 'FUBON-05-373-001')
        with error_step('FUBON-05-374-001'):
            if dom_guard.get('submitDocument') is not True:
                raise with_error_code(RuntimeError("fubon-twd-history-result-mutated"), 'FUBON-05-374-001')
        with error_step('FUBON-05-375-001'):
            if type(dom_guard.get('mutations')) is not int:
                raise with_error_code(RuntimeError("fubon-twd-history-result-mutated"), 'FUBON-05-375-001')
        with error_step('FUBON-05-376-001'):
            if dom_guard['mutations'] != 0:
                raise with_error_code(RuntimeError("fubon-twd-history-result-mutated"), 'FUBON-05-376-001')
        with error_step('FUBON-05-377-001'):
            if not isinstance(snapshot, dict):
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-05-377-001')
        with error_step('FUBON-05-378-001'):
            if snapshot.get('failed') is not False:
                raise with_error_code(RuntimeError("fubon-twd-history-result"), 'FUBON-05-378-001')
        with error_step('FUBON-05-379-001'):
            result = {
                "account_no": option["identity"],
                "account_value": option["value"],
                "preset": window["preset"],
                "start": window["start"],
                "end": window["end"],
                "status": "complete" if snapshot.get("hasGrid") else "explicit_empty",
                "url": snapshot.pop("href", ""),
                "transport": transport,
                "snapshot": snapshot,
            }
        with error_step('FUBON-05-380-001'):
            return result, self._validated_twd_history_result(result)

    def _collect_attested_twd_history(self, page) -> dict:
        with error_step('FUBON-05-381-001'):
            query_frame = self._open_twd_query(page)
        with error_step('FUBON-05-382-001'):
            raw_options = bounded_evaluate(query_frame, r"""() => {
            const forms=[...document.querySelectorAll('form#form1')];
            const xs=[...document.querySelectorAll('select#form1\\:comboAccount')];
            if(forms.length!==1)return {errorId:'FUBON-04-780-001'};
            if(xs.length!==1)return {errorId:'FUBON-04-780-002'};
            if(!forms[0].contains(xs[0]))return {errorId:'FUBON-04-780-003'};
            return [...xs[0].options].map((option,index)=>({index,value:option.value||'',text:(option.textContent||'').trim()}));
        }""", _error_id='FUBON-05-382-001')
        inventory_reason = raw_options.get('errorId') if isinstance(raw_options, dict) else None
        if inventory_reason in ('FUBON-04-780-001', 'FUBON-04-780-002', 'FUBON-04-780-003'):
            raw_options = None
        else:
            inventory_reason = None
        try:
            with error_step('FUBON-05-383-001'):
                options = _validated_fubon_twd_options(raw_options)
        except ValueError as source_error:
            raise with_error_code(RuntimeError("fubon-twd-history-inventory"), inventory_reason or exception_error_id(source_error, bank="fubon") or 'FUBON-05-384-001') from None
        with error_step('FUBON-05-385-001'):
            as_of = datetime.now(ZoneInfo("Asia/Taipei")).date()
        with error_step('FUBON-05-386-001'):
            mode = os.environ.get("BANK_CRAWLER_HISTORY_MODE", "full")
        expected, receipts, results = [], [], []
        with error_step('FUBON-05-391-001'):
            for option in options:
                with error_step('FUBON-05-387-001'):
                    windows = self._history_windows(option["identity"], as_of)
                with error_step('FUBON-05-388-001'):
                    expected.append({"identity": option["identity"], "start": windows[0]["start"], "end": windows[-1]["end"]})
                with error_step('FUBON-05-390-001'):
                    for window in windows:
                        with error_step('FUBON-05-389-001'):
                            result, receipt = self._collect_twd_window(page, self._open_twd_query(page), option, window)
                        results.append(result); receipts.append(receipt)
        coverage = {"version": 1, "mode": mode, "domains": [{"domain": "twd_transactions", "expected": expected, "windows": receipts}]}
        with error_step('FUBON-05-392-001'):
            validate_history_coverage(coverage, expected_mode=mode, expected_domains=self.HISTORY_COVERAGE_DOMAINS)
        with error_step('FUBON-05-393-001'):
            accounts = [
                {"account_no": option["identity"], "currency": "TWD", "type": "deposit", "name": "台幣存款"}
                for option in options
            ]
        return {
            "accounts": accounts,
            "deposit_txn_results": results,
            "history_coverage": coverage,
        }

    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        """富邦 collect：信用卡 menu 在 txnFrame (CGEQU001_Home) carousel 全渲染。

        關鍵發現 (2026-06-12):
        - top menu 在 frame1，但 mega menu 內容 carousel 全部存在於 txnFrame 內
        - DOM 全部渲染（不靠 hover），視覺上只顯示一頁，但 querySelector 都拿得到
        - 走 txnFrame 直接定位「我的信用卡」/「帳務/繳款」等子項 click 即可
        """
        _log("[fubon][phase] collect_start")
        def bounded_evaluate(scope, expression: str, arg=None, *, _error_id="FUBON-06-200-001"):
            locator_id, evaluate_id = {
                'FUBON-06-200-001': ('FUBON-06-200-101', 'FUBON-06-200-201'),
                'FUBON-06-214-001': ('FUBON-06-214-101', 'FUBON-06-214-201'),
                'FUBON-06-216-001': ('FUBON-06-216-101', 'FUBON-06-216-201'),
                'FUBON-06-238-001': ('FUBON-06-238-101', 'FUBON-06-238-201'),
                'FUBON-06-244-001': ('FUBON-06-244-101', 'FUBON-06-244-201'),
                'FUBON-06-248-001': ('FUBON-06-248-101', 'FUBON-06-248-201'),
                'FUBON-06-253-001': ('FUBON-06-253-101', 'FUBON-06-253-201'),
                'FUBON-06-256-001': ('FUBON-06-256-101', 'FUBON-06-256-201'),
                'FUBON-06-261-001': ('FUBON-06-261-101', 'FUBON-06-261-201'),
                'FUBON-06-265-001': ('FUBON-06-265-101', 'FUBON-06-265-201'),
                'FUBON-06-271-001': ('FUBON-06-271-101', 'FUBON-06-271-201'),
                'FUBON-06-276-001': ('FUBON-06-276-101', 'FUBON-06-276-201'),
                'FUBON-06-299-001': ('FUBON-06-299-101', 'FUBON-06-299-201'),
                'FUBON-06-304-001': ('FUBON-06-304-101', 'FUBON-06-304-201'),
            }[_error_id]
            with error_step(locator_id):
                root = scope.locator("html")
            with error_step(evaluate_id):
                return root.evaluate(
                    f"(root, arg) => ({expression})(arg)", arg, timeout=5000,
                )

        out: dict = {}
        def finish() -> BankCollectResult:
            with error_step('FUBON-07-326-001'):
                out["final_url"] = page.url
            with error_step('FUBON-07-327-001'):
                out["_all_endpoints"] = sorted({hit.endpoint for hit in collector.hits if hit.resp_json})
            with error_step('FUBON-07-328-001'):
                publish_card_bill_facts(out, [_fubon_card_bill_fact(out.get("amount_page_text") or "")])
            with error_step('FUBON-07-329-001'):
                return BankCollectResult(**out)

        with error_step('FUBON-06-205-001'):
            out.update(self._collect_attested_twd_history(page))
        with error_step('FUBON-06-206-001'):
            page.goto(
                "https://ebank.taipeifubon.com.tw/B2C/cgequ/cgequ001/CGEQU001_Home.faces",
                wait_until="domcontentloaded", timeout=15000,
            )
        with error_step('FUBON-06-207-001'):
            page.wait_for_timeout(5000)

        with error_step('FUBON-06-208-001'):
            out["initial_url"] = page.url

        # === Step 1: 找 txnFrame (內容區，含 carousel mega menu) ===
        content_frame = None
        with error_step('FUBON-06-211-001'):
            for f in page.frames:
                with error_step('FUBON-06-209-001'):
                    url = f.url or ""
                with error_step('FUBON-06-210-001'):
                    name = f.name or ""
                if "CGEQU001" in url or name == "txnFrame":
                    content_frame = f
                    break
        _log(f"[fubon][collect] content_frame={'OK' if content_frame else 'MISS'}")
        with error_step('FUBON-06-213-001'):
            if not content_frame:
                with error_step('FUBON-06-212-001'):
                    return finish()

        # === Step 2: 在 txnFrame 找信用卡相關子項 (carousel 全渲染，offscreen 也存在) ===
        # 優先序：直接走「我的信用卡」進信用卡頁，或「帳務/繳款」進帳單查詢
        with error_step('FUBON-06-214-001'):
            candidates = bounded_evaluate(content_frame, """() => {
            const targets = ['我的信用卡', '帳務/繳款', '消費分期', '紅利/哩程', '預借現金', '信用卡帳單', '信用卡明細', '帳單查詢'];
            const out = [];
            for (const el of document.querySelectorAll('a, button, span, div, li')) {
                const t = (el.textContent || '').trim();
                if (!targets.includes(t)) continue;
                const r = el.getBoundingClientRect();
                out.push({
                    tag: el.tagName, text: t, cls: (el.className || '').slice(0, 80),
                    href: el.href || (el.getAttribute && el.getAttribute('href')) || '',
                    onclick: (el.getAttribute && el.getAttribute('onclick')) || '',
                    x: r.x, y: r.y, w: r.width, h: r.height,
                    visible: r.width > 0 && r.height > 0 && el.offsetParent !== null
                });
            }
            return out;
        }""", _error_id='FUBON-06-214-001')
        _log(f"[fubon][collect] 找到 {len(candidates)} 個信用卡子項候選")
        with error_step('FUBON-06-215-001'):
            for c in candidates:
                _log(
                    f"  - {c['tag']} '{c['text']}' cls='{c['cls'][:30]}' "
                    f"visible={c['visible']} href='{_safe_url(c['href'])}'"
                )

        # Telemetry 2026-06-18: 同時 dump 存款/帳戶相關 menu 候選 (給 cloud 看真實有哪些字)
        # 目的: 確認富邦 menu 用「帳戶總覽」「存款查詢」「我的帳戶」哪個字眼, 才能規劃 collect path
        # 同時 dump 所有 <a> visible text 前 100 條 (上限避免 result_summary 爆)
        with error_step('FUBON-06-216-001'):
            deposit_audit = bounded_evaluate(content_frame, r"""() => {
            const KW = /(帳戶|存款|餘額|台幣|外幣|定存|數位帳戶|主帳|匯款|轉帳|資產)/;
            const out = [];
            for (const el of document.querySelectorAll('a, button, li, span, div')) {
                const t = (el.textContent || '').trim();
                if (!t || t.length > 30 || !KW.test(t)) continue;
                const r = el.getBoundingClientRect();
                out.push({
                    tag: el.tagName, text: t,
                    cls: (el.className || '').slice(0, 60),
                    href: el.href || (el.getAttribute && el.getAttribute('href')) || '',
                    visible: r.width > 0 && r.height > 0 && el.offsetParent !== null,
                });
                if (out.length >= 60) break;
            }
            // 去重 (tag+text)
            const seen = new Set();
            return out.filter(x => {
                const k = x.tag + ':' + x.text;
                if (seen.has(k)) return false;
                seen.add(k);
                return true;
            });
        }""", _error_id='FUBON-06-216-001')
        with error_step('FUBON-06-217-001'):
            out["deposit_menu_audit"] = deposit_audit
        _log(f"[fubon][collect] [TELEMETRY] 存款相關 menu 候選 {len(deposit_audit)} 條")

        with error_step('FUBON-06-219-001'):
            if not candidates:
                with error_step('FUBON-06-218-001'):
                    return finish()

        # === Step 3: 選最優先且 visible 的目標 ===
        # 富邦 menu 元素三種 tag: A (真正連結 href) / DIV (裝飾) / SPAN (icon)
        # 必須優先選 <A>，DIV/SPAN click 不 routing
        priority = ["我的信用卡", "帳務/繳款", "信用卡帳單", "信用卡明細", "帳單查詢", "消費分期", "紅利/哩程"]
        target = None
        with error_step('FUBON-06-222-001'):
            for p in priority:
                # 先找 <A> visible
                with error_step('FUBON-06-221-001'):
                    for c in candidates:
                        with error_step('FUBON-06-220-001'):
                            if c["text"] == p and c["visible"] and c["tag"] == "A":
                                target = c
                                break
                if target:
                    break
        # fallback: 任何 visible <A>
        with error_step('FUBON-06-225-001'):
            if not target:
                with error_step('FUBON-06-224-001'):
                    for c in candidates:
                        with error_step('FUBON-06-223-001'):
                            if c["visible"] and c["tag"] == "A":
                                target = c
                                break
        # fallback2: 任何 <A>（即使 offscreen）
        with error_step('FUBON-06-228-001'):
            if not target:
                with error_step('FUBON-06-227-001'):
                    for c in candidates:
                        with error_step('FUBON-06-226-001'):
                            if c["tag"] == "A":
                                target = c
                                break
        # fallback3: 真的沒 <A> 才退 DIV/SPAN
        with error_step('FUBON-06-230-001'):
            if not target and candidates:
                with error_step('FUBON-06-229-001'):
                    target = candidates[0]
        _log(
            f"[fubon][collect] 選擇 target: tag={target['tag']} "
            f"text='{target['text']}' visible={target['visible']} "
            f"href='{_safe_url(target['href'])}'"
        )

        # === Step 4: click target ===
        # 算 txnFrame offset
        offset_x = 0
        offset_y = 0
        with error_step('FUBON-06-237-001'):
            for fel in page.query_selector_all("iframe, frame"):
                try:
                    with error_step('FUBON-06-231-001'):
                        name_attr = fel.get_attribute("name") or ""
                    with error_step('FUBON-06-236-001'):
                        if name_attr == "txnFrame":
                            with error_step('FUBON-06-232-001'):
                                fbox = fel.bounding_box()
                            with error_step('FUBON-06-235-001'):
                                if fbox:
                                    with error_step('FUBON-06-233-001'):
                                        offset_x = fbox["x"]
                                    with error_step('FUBON-06-234-001'):
                                        offset_y = fbox["y"]
                                    break
                except Exception:
                    pass
        _log(f"[fubon][collect] txnFrame offset=({offset_x}, {offset_y})")

        # 走 frame.evaluate 找 A tag (相同 text 多個時優先 <A>，並用 cls 區分 menu_CCCxx)
        with error_step('FUBON-06-238-001'):
            click_result = bounded_evaluate(content_frame, """(args) => {
            const {targetText, targetCls} = args;
            // 第一順位：tag=A 且 cls 含 targetCls
            for (const el of document.querySelectorAll('a')) {
                const t = (el.textContent || '').trim();
                if (t !== targetText) continue;
                if (targetCls && !el.className.includes(targetCls.split(' ')[0])) continue;
                try {
                    el.click();
                    return {ok: true, method: 'a.click()', cls: el.className, href: el.href || ''};
                } catch (e) {
                    return {ok: false, error: String(e)};
                }
            }
            // 第二順位：任何 A 同文
            for (const el of document.querySelectorAll('a')) {
                const t = (el.textContent || '').trim();
                if (t !== targetText) continue;
                try {
                    el.click();
                    return {ok: true, method: 'a.click()-fallback', cls: el.className, href: el.href || ''};
                } catch (e) {}
            }
            return {ok: false, error: 'no_anchor_found'};
        }""", {"targetText": target["text"], "targetCls": target["cls"]}, _error_id='FUBON-06-238-001')
        _log(
            f"[fubon][collect] click result: "
            f"ok={bool(isinstance(click_result, dict) and click_result.get('ok'))}"
        )
        with error_step('FUBON-06-239-001'):
            page.wait_for_timeout(6000)

        # === Step 4.5: txnFrame 切換後重新抓 frame（URL 已換）===
        # 點完 <A> 後 txnFrame 會 navigate 到 CCCQU001_Home.faces
        with error_step('FUBON-06-240-001'):
            page.wait_for_timeout(2000)
        with error_step('FUBON-06-243-001'):
            for f in page.frames:
                with error_step('FUBON-06-241-001'):
                    url = f.url or ""
                with error_step('FUBON-06-242-001'):
                    name = f.name or ""
                if name == "txnFrame":
                    content_frame = f
                    break
        _log(f"[fubon][collect] 切換後 txnFrame url={_safe_url(content_frame.url)}")

        # 立刻抓「我的信用卡」頁卡片清單 (CCCQU001_Home)
        cards_page_text = ""
        with error_step('FUBON-06-245-001'):
            with contextlib.suppress(Exception):
                with error_step('FUBON-06-244-001'):
                    cards_page_text = bounded_evaluate(content_frame, "() => document.body.innerText.slice(0, 10000)", _error_id='FUBON-06-244-001') or ""
        with error_step('FUBON-06-246-001'):
            out["cards_page_text"] = cards_page_text
        with error_step('FUBON-06-247-001'):
            out["cards_page_url"] = content_frame.url
        _log(f"[fubon][collect] cards 頁 text_len={len(cards_page_text)}")

        # === Step 4.6: 再 click「帳務查詢」進帳單明細頁 ===
        # 富邦右上吊牌 quick links: 帳務查詢 / 網路辦卡 / 申辦進度查詢
        # 必須找 <A> tag（LI 是裝飾外殼）
        with error_step('FUBON-06-248-001'):
            bill_click = bounded_evaluate(content_frame, """() => {
            const targets = ['帳務查詢', '帳單查詢', '消費明細查詢', '消費明細', '帳單'];
            const found = [];
            for (const t of targets) {
                for (const el of document.querySelectorAll('a')) {
                    if ((el.textContent || '').trim() !== t) continue;
                    found.push({text: t, tag: el.tagName, href: el.href || '', cls: (el.className || '').slice(0, 80), visible: el.offsetParent !== null});
                }
            }
            if (found.length === 0) {
                return {ok: false, error: 'no_anchor_for_bill_tabs', found: []};
            }
            // 優先選 visible 且帶 href 的
            let chosen = found.find(f => f.visible && f.href) || found.find(f => f.visible) || found[0];
            // 真正 click 那個 A
            for (const el of document.querySelectorAll('a')) {
                if ((el.textContent || '').trim() !== chosen.text) continue;
                if (chosen.href && el.href !== chosen.href) continue;
                try {
                    el.click();
                    return {ok: true, clicked: chosen.text, tag: 'A', href: el.href || '', cls: (el.className || '').slice(0, 80), found_count: found.length};
                } catch (e) {
                    return {ok: false, error: String(e)};
                }
            }
            return {ok: false, error: 'click_failed_after_match', found: found};
        }""", _error_id='FUBON-06-248-001')
        _log(
            f"[fubon][collect] 帳務查詢 click: "
            f"ok={bool(isinstance(bill_click, dict) and bill_click.get('ok'))}"
        )
        with error_step('FUBON-06-249-001'):
            page.wait_for_timeout(6000)

        # === Step 4.7: 再切回 txnFrame 確認 URL ===
        with error_step('FUBON-06-250-001'):
            page.wait_for_timeout(2000)
        with error_step('FUBON-06-252-001'):
            for f in page.frames:
                with error_step('FUBON-06-251-001'):
                    if (f.name or "") == "txnFrame":
                        content_frame = f
                        break
        _log(f"[fubon][collect] 帳務查詢 click 後 txnFrame url={_safe_url(content_frame.url)}")

        # === Step 4.8: 抓「繳款及額度查詢」頁的 text 後，先試點「帳單明細查詢」===
        # 帳務查詢頁有 sub-tabs: 繳款及額度查詢 / 帳單明細查詢 / 未出帳單消費明細 / 消費分析
        # 先抓額度頁，再嘗試切到帳單明細
        amount_page_text = ""
        with error_step('FUBON-06-254-001'):
            with contextlib.suppress(Exception):
                with error_step('FUBON-06-253-001'):
                    amount_page_text = bounded_evaluate(content_frame, "() => document.body.innerText.slice(0, 15000)", _error_id='FUBON-06-253-001') or ""
        with error_step('FUBON-06-255-001'):
            out["amount_page_text"] = amount_page_text
        _log(f"[fubon][collect] amount 頁 text_len={len(amount_page_text)}")

        # 嘗試 click 帳單明細查詢
        with error_step('FUBON-06-256-001'):
            billed_click = bounded_evaluate(content_frame, """() => {
            const targets = ['帳單明細查詢', '帳單明細'];
            for (const t of targets) {
                for (const el of document.querySelectorAll('a')) {
                    if ((el.textContent || '').trim() !== t) continue;
                    try {
                        el.click();
                        return {ok: true, clicked: t, href: el.href || '', cls: (el.className || '').slice(0, 80)};
                    } catch (e) {}
                }
            }
            return {ok: false, error: 'no_billed_tab'};
        }""", _error_id='FUBON-06-256-001')
        _log(
            f"[fubon][collect] 帳單明細查詢 click: "
            f"ok={bool(isinstance(billed_click, dict) and billed_click.get('ok'))}"
        )
        with error_step('FUBON-06-257-001'):
            page.wait_for_timeout(6000)

        # 切回 txnFrame 抓帳單明細頁 text
        with error_step('FUBON-06-258-001'):
            page.wait_for_timeout(2000)
        with error_step('FUBON-06-260-001'):
            for f in page.frames:
                with error_step('FUBON-06-259-001'):
                    if (f.name or "") == "txnFrame":
                        content_frame = f
                        break
        billed_page_text = ""
        with error_step('FUBON-06-262-001'):
            with contextlib.suppress(Exception):
                with error_step('FUBON-06-261-001'):
                    billed_page_text = bounded_evaluate(content_frame, "() => document.body.innerText.slice(0, 20000)", _error_id='FUBON-06-261-001') or ""
        with error_step('FUBON-06-263-001'):
            out["billed_page_text"] = billed_page_text
        with error_step('FUBON-06-264-001'):
            out["billed_page_url"] = content_frame.url
        _log(f"[fubon][collect] billed 頁 url={_safe_url(content_frame.url)} text_len={len(billed_page_text)}")

        # 嘗試 click 未出帳單消費明細
        with error_step('FUBON-06-265-001'):
            pending_click = bounded_evaluate(content_frame, """() => {
            const targets = ['未出帳單消費明細', '未出帳消費明細', '未出帳明細'];
            for (const t of targets) {
                for (const el of document.querySelectorAll('a')) {
                    if ((el.textContent || '').trim() !== t) continue;
                    try {
                        el.click();
                        return {ok: true, clicked: t, href: el.href || ''};
                    } catch (e) {}
                }
            }
            return {ok: false, error: 'no_pending_tab'};
        }""", _error_id='FUBON-06-265-001')
        with error_step('FUBON-06-266-001'):
            out["pending_click_ok"] = (
                isinstance(pending_click, dict) and pending_click.get("ok") is True)
        _log(
            f"[fubon][collect] 未出帳單 click: "
            f"ok={bool(isinstance(pending_click, dict) and pending_click.get('ok'))}"
        )
        with error_step('FUBON-06-267-001'):
            page.wait_for_timeout(6000)

        # 切回 txnFrame
        with error_step('FUBON-06-268-001'):
            page.wait_for_timeout(2000)
        with error_step('FUBON-06-270-001'):
            for f in page.frames:
                with error_step('FUBON-06-269-001'):
                    if (f.name or "") == "txnFrame":
                        content_frame = f
                        break
        pending_page_text = ""
        with error_step('FUBON-06-272-001'):
            with contextlib.suppress(Exception):
                with error_step('FUBON-06-271-001'):
                    pending_page_text = bounded_evaluate(content_frame, "() => document.body.innerText.slice(0, 20000)", _error_id='FUBON-06-271-001') or ""
        with error_step('FUBON-06-273-001'):
            out["pending_page_text"] = pending_page_text
        with error_step('FUBON-06-274-001'):
            out["pending_page_url"] = content_frame.url
        _log(f"[fubon][collect] pending 頁 url={_safe_url(content_frame.url)} text_len={len(pending_page_text)}")

        # === Step 5: dump 點完後所有 frames ===
        with error_step('FUBON-06-275-001'):
            page.wait_for_timeout(2000)
        frames_data = []
        with error_step('FUBON-06-279-001'):
            for f in page.frames:
                try:
                    with error_step('FUBON-06-276-001'):
                        txt = bounded_evaluate(f, "() => document.body.innerText.slice(0, 15000)", _error_id='FUBON-06-276-001')
                    with error_step('FUBON-06-278-001'):
                        if txt and len(txt) > 50:
                            with error_step('FUBON-06-277-001'):
                                frames_data.append({
                                    "name": f.name or "",
                                    "url": (f.url or "")[:300],
                                    "text": txt,
                                })
                except Exception:
                    pass
        with error_step('FUBON-06-280-001'):
            out["frames"] = frames_data
        _log(f"[fubon][collect] 點完後 dump {len(frames_data)} frames")
        with error_step('FUBON-06-281-001'):
            for fd in frames_data:
                _log(f"  - {fd['name']} url={_safe_url(fd['url'])} text_len={len(fd['text'])}")

        # === Step 6: 找信用卡明細頁 frame ===
        card_frame_text = None
        with error_step('FUBON-06-290-001'):
            for fd in frames_data:
                with error_step('FUBON-06-282-001'):
                    t = fd["text"]
                with error_step('FUBON-06-283-001'):
                    url = fd["url"]
                # 信用卡頁特徵：URL 含 CGCRE/CGCC/CARD，或 text 含「卡號末四碼/應繳金額/結帳日」
                with error_step('FUBON-06-284-001'):
                    url_hit = any(k in url.upper() for k in ["CGCRE", "CGCC", "CARD", "CRE"])
                with error_step('FUBON-06-285-001'):
                    text_hit = any(k in t for k in ["卡號末四碼", "應繳金額", "結帳日", "繳款截止", "本期帳單"])
                with error_step('FUBON-06-289-001'):
                    if (url_hit or text_hit) and len(t) > len(card_frame_text or ""):
                        card_frame_text = t
                        with error_step('FUBON-06-286-001'):
                            out["card_frame_url"] = url
                        with error_step('FUBON-06-287-001'):
                            out["card_frame_name"] = fd["name"]
                        with error_step('FUBON-06-288-001'):
                            out["card_frame_match"] = "url" if url_hit else "text"
        with error_step('FUBON-06-292-001'):
            if card_frame_text:
                _log(f"[fubon][collect] ✓ 找到信用卡 frame: name={out.get('card_frame_name')} url={_safe_url(out.get('card_frame_url'))} text_len={len(card_frame_text)}")
                with error_step('FUBON-06-291-001'):
                    out["card_frame_text"] = card_frame_text
            else:
                _log("[fubon][collect] ⚠️ 沒命中信用卡 frame，僅 dump 待分析")

        # === Step 7 (2026-06-18): 點「我的存款」(CBO_03 / CBOQU003) dump 存款帳戶 ===
        # 富邦 home menu 有 <A class="task_CBOQU003 menu_CBO03" href="...ParamValue=...">我的存款</A>
        # 點下去 navigate 到 txnFrame 的 CBOQU003_Home.faces, body 有 tab-separated table:
        #   header: 帳號 帳戶暱稱 存款類別 分行 幣別 即時餘額 可用餘額 存單號碼 到期日 功能
        # local probe 證實 (debug_fubon_deposit.py): 使用者真有 2 個臺幣活儲帳戶 (餘額 0):
        #   00900000147012 數位活儲 營業部 / 00900000157046 活儲存款 松高分行
        # 之前 collect 完全沒走 deposit path → accounts:0 是 by-design gap 不是漏抓.
        # 修法: 重新 navigate 到 home (CGEQU001) → 點 CBOQU003 → dump deposit frame text.
        try:
            # 直接回已知 home URL；舊 collector hit 掃描結果未被使用。
            with error_step('FUBON-06-293-001'):
                home_back = page.goto(
                    "https://ebank.taipeifubon.com.tw/B2C/cgequ/cgequ001/CGEQU001_Home.faces",
                    wait_until="domcontentloaded", timeout=15000,
                )
            with error_step('FUBON-06-294-001'):
                page.wait_for_timeout(5000)
            _log(f"[fubon][collect] 回 home navigate ok={home_back is not None}")
        except Exception as e:
            _log(f"[fubon][collect] 回 home 失敗: {type(e).__name__}")

        # 重新抓 txnFrame
        deposit_frame = None
        with error_step('FUBON-06-295-001'):
            page.wait_for_timeout(2000)
        with error_step('FUBON-06-298-001'):
            for f in page.frames:
                with error_step('FUBON-06-296-001'):
                    url = f.url or ""
                with error_step('FUBON-06-297-001'):
                    name = f.name or ""
                if "CGEQU001" in url or name == "txnFrame":
                    deposit_frame = f
                    break

        with error_step('FUBON-06-310-001'):
            if deposit_frame:
                # 點 a.task_CBOQU003 (我的存款) — 不是 click text 因為 DIV/SPAN 同名沒 routing
                with error_step('FUBON-06-299-001'):
                    deposit_click = bounded_evaluate(deposit_frame, r"""() => {
                const a = document.querySelector('a.task_CBOQU003, a.menu_CBO03');
                if (!a) return {ok: false, error: 'no_deposit_anchor'};
                try {
                    a.click();
                    return {ok: true, href: a.href || '', text: (a.textContent || '').trim()};
                } catch (e) {
                    return {ok: false, error: String(e)};
                }
            }""", _error_id='FUBON-06-299-001')
                _log(
                    f"[fubon][collect] 我的存款 click: "
                    f"ok={bool(isinstance(deposit_click, dict) and deposit_click.get('ok'))}"
                )
                with error_step('FUBON-06-300-001'):
                    page.wait_for_timeout(8000)

                # 重新找 txnFrame (URL 應該換到 CBOQU003_Home.faces)
                with error_step('FUBON-06-301-001'):
                    page.wait_for_timeout(2000)
                with error_step('FUBON-06-303-001'):
                    for f in page.frames:
                        with error_step('FUBON-06-302-001'):
                            if (f.name or "") == "txnFrame":
                                deposit_frame = f
                                break

                deposit_page_text = ""
                with error_step('FUBON-06-305-001'):
                    with contextlib.suppress(Exception):
                        with error_step('FUBON-06-304-001'):
                            deposit_page_text = bounded_evaluate(deposit_frame, "() => document.body.innerText.slice(0, 20000)", _error_id='FUBON-06-304-001') or ""
                with error_step('FUBON-06-306-001'):
                    out["deposit_page_text"] = deposit_page_text
                with error_step('FUBON-06-307-001'):
                    out["deposit_page_url"] = deposit_frame.url
                _log(f"[fubon][collect] deposit 頁 url={_safe_url(deposit_frame.url)} text_len={len(deposit_page_text)}")
            else:
                _log("[fubon][collect] ⚠️ 回 home 後找不到 txnFrame, 跳過 deposit step")
                with error_step('FUBON-06-308-001'):
                    out["deposit_page_text"] = ""
                with error_step('FUBON-06-309-001'):
                    out["deposit_page_url"] = ""

        with error_step('FUBON-06-311-001'):
            return finish()


if __name__ == "__main__":
    import json
    crawler = FubonCrawler()
    try:
        result = crawler.run(login_url=BASE, headless=False)
    except FubonLoginError as e:
        result = {"error": "login_failed_stop", "detail": str(e)}

    out_file = Path(__file__).resolve().parents[1] / "data" / "fubon_collected.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"\n[done] 已存: {out_file}")
