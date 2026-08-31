#!/usr/bin/env python3
"""Abstract base class for bank crawlers.

銀行爬蟲抽象基類。

每家銀行繼承 BankCrawler，實作 login() 與 collect()。
統一用 Scrapling StealthyFetcher (headful) + user_data_dir session 持久化。
攔截式抓取：讓銀行自己的前端打 API，攔 response 拿 JSON，不逆向加密。
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, fields as dataclass_fields, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, NotRequired, Required, TypedDict
from urllib.parse import urlparse

from scrapling.fetchers import StealthyFetcher

from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointOutcome,
    CheckpointPhase,
    DEFAULT_ACTION_SELECTOR,
    LoginBudget,
    LoginCheckpointBlocked,
    LoginCheckpointRule,
    LoginInteractionRequired,
    evaluate_login_checkpoint,
    reduce_login_checkpoint,
    validate_login_checkpoint_outcome,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PROJECT_ROOT / "data"

MACOS_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
MACOS_SPOOF_JS = r"""
(() => {
    try {
        Object.defineProperty(navigator, 'platform', {
            get: () => 'MacIntel',
            configurable: true,
        });
    } catch (e) {}
    try {
        if (navigator.userAgentData) {
            const orig = navigator.userAgentData;
            const fake = {
                brands: orig.brands,
                mobile: orig.mobile,
                platform: 'macOS',
                getHighEntropyValues: orig.getHighEntropyValues
                    ? (hints) => orig.getHighEntropyValues.call(orig, hints).then(v => ({
                        ...v, platform: 'macOS', platformVersion: '15.0.0',
                        architecture: 'x86', bitness: '64', model: '',
                    }))
                    : undefined,
                toJSON: () => ({ brands: orig.brands, mobile: orig.mobile, platform: 'macOS' }),
            };
            Object.defineProperty(navigator, 'userAgentData', {
                get: () => fake,
                configurable: true,
            });
        }
    } catch (e) {}
    try {
        Object.defineProperty(navigator, 'appVersion', {
            get: () => '5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            configurable: true,
        });
    } catch (e) {}
})();
"""


@dataclass
class ApiHit:
    """一次攔截到的 API 呼叫（request + response 配對）。"""
    url: str
    method: str
    status: int
    req_body: Any = None
    resp_json: Any = None
    content_type: str = ""
    raw_url: str = ""
    redirected: bool = False
    body_size: int | None = None

    @property
    def endpoint(self) -> str:
        return self.url.split("?")[0].rsplit("/", 1)[-1]


class ResponseCollector:
    """掛在 Playwright page 上，攔截所有 XHR/fetch 的 request+response。"""

    SKIP_RE = re.compile(
        r"(\.js|\.css|\.ico|\.png|\.jpg|\.svg|\.woff2?|\.gif)(\?|$)"
        r"|/locales/|google|gtm|omtrdc|doubleclick|analytics|datalayer|celebrus|faro|/assets/",
        re.I,
    )

    def __init__(self, host_filter: str = ""):
        self.hits: list[ApiHit] = []
        self.host_filter = host_filter
        self.auth_token: str = ""  # 攔到的 Authorization 標頭（如 'Bearer eyJ...'），給直接 fetch 用
        self.auth_token_url: str = ""
        self.auth_token_events: list[dict[str, Any]] = []
        self.hsbc_inventory_bytes = 0
        self._auth_event_sequence = 0
        self._auth_requests: dict[int, dict[str, Any]] = {}
        self._latest_auth_request_sequence = 0
        self._response_handler = self._on_response
        self._request_handler = self._on_request

    def attach(self, page):
        page.on("request", self._request_handler)
        page.on("response", self._response_handler)

    def detach(self, page) -> None:
        remove = getattr(page, "remove_listener", None)
        if callable(remove):
            remove("request", self._request_handler)
            remove("response", self._response_handler)

    def _on_request(self, req) -> None:
        try:
            auth = req.headers.get("authorization", "")
            if re.fullmatch(r"Bearer [^\s\r\n]+", auth) is None:
                return
            parsed = urlparse(req.url)
            expected = self.host_filter.lower().strip(".")
            hostname = (parsed.hostname or "").lower()
            if self.host_filter and (
                parsed.scheme.lower() != "https"
                or (hostname != expected and not hostname.endswith("." + expected))
            ):
                return
            self._auth_event_sequence += 1
            self._auth_requests[id(req)] = {
                "token": auth,
                "url": req.url,
                "redirected": getattr(req, "redirected_from", None) is not None,
                "sequence": self._auth_event_sequence,
            }
        except Exception:
            return

    def _on_response(self, resp):
        try:
            url = resp.url
            if self.SKIP_RE.search(url):
                return
            parsed = urlparse(url)
            if self.host_filter:
                expected = self.host_filter.lower().strip(".")
                hostname = (parsed.hostname or "").lower()
                if (
                    parsed.scheme.lower() != "https"
                    or (hostname != expected and not hostname.endswith("." + expected))
                ):
                    return
            req = resp.request
            ct = resp.headers.get("content-type", "")
            content_length = resp.headers.get("content-length", "")
            content_encoding = resp.headers.get("content-encoding", "")
            is_bounded_hsbc_inventory = (
                self.host_filter == "card.hsbc.com.tw"
                and parsed.path in {"/ibk-bff/api/v1/cards", "/ibk-bff/api/v1/cards/suspend"}
            )
            body_size = int(content_length) if content_length.isdigit() else None
            auth = req.headers.get("authorization", "")
            auth_event = self._auth_requests.pop(id(req), None)
            # 只按 request 發出順序更新 token；response 亂序不得降回舊 token。
            if (
                auth_event is not None
                and auth == auth_event["token"]
                and 200 <= resp.status < 300
                and re.fullmatch(r"Bearer [^\s\r\n]+", auth)
            ):
                event = {
                    "token": auth_event["token"],
                    "url": url,
                    "redirected": auth_event["redirected"],
                    "sequence": auth_event["sequence"],
                }
                self.auth_token_events.append(event)
                if event["sequence"] > self._latest_auth_request_sequence:
                    self._latest_auth_request_sequence = event["sequence"]
                    self.auth_token = event["token"]
                    self.auth_token_url = url
            is_data = ("json" in ct) or (req.method == "POST") or bool(auth)
            if not is_data:
                return
            req_body = None
            try:
                pd = req.post_data
                if pd:
                    try:
                        req_body = json.loads(pd)
                    except Exception:
                        req_body = pd[:500]
            except Exception:
                pass
            resp_json = None
            if "json" in ct and (
                not is_bounded_hsbc_inventory
                or (
                    body_size is not None
                    and body_size <= 5_000_000
                    and content_encoding.lower() in {"", "identity"}
                )
            ):
                with contextlib.suppress(Exception):
                    resp_json = resp.json()
            self.hits.append(ApiHit(
                url=url.split("?")[0], method=req.method, status=resp.status,
                req_body=req_body, resp_json=resp_json, content_type=ct,
                raw_url=url,
                redirected=getattr(req, "redirected_from", None) is not None,
                body_size=body_size,
            ))
        except Exception:
            pass

    def by_endpoint(self, name: str) -> list[ApiHit]:
        return [h for h in self.hits if h.endpoint == name and h.resp_json is not None]

    def latest(self, name: str) -> ApiHit | None:
        hits = self.by_endpoint(name)
        return hits[-1] if hits else None


IsoDate = str  # Contract: normalized calendar date text, exactly YYYY-MM-DD.
Money = int | float


# Normalized payload aliases. They intentionally remain dict-compatible while
# `BankCollectResult.__post_init__` enforces the cross-bank date invariants.
NormalizedAccount = dict[str, Any]
NormalizedCard = dict[str, Any]
NormalizedTwdTxn = dict[str, Any]
NormalizedCardBilledTxn = dict[str, Any]
NormalizedCardPendingTxn = dict[str, Any]
class NormalizedCardBillFact(TypedDict, total=False):
    scope: Required[str]
    status: Required[str]
    remaining_due: Required[float]
    card_no: NotRequired[str]
    statement_close_date: NotRequired[str]
    payment_due_date: NotRequired[str]
    last_payment_amount: NotRequired[float]
    last_payment_date: NotRequired[str]
NormalizedBalanceHistory = dict[str, Any]
DailyMetric = dict[str, Any]


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?$")


def _require_iso_date(value: Any, *, path: str) -> None:
    if value in (None, ""):
        return
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        raise ValueError(f"{path} must be IsoDate YYYY-MM-DD, got {value!r}")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{path} must be IsoDate YYYY-MM-DD, got {value!r}") from exc


def _require_iso_date_or_datetime(value: Any, *, path: str) -> None:
    if value in (None, ""):
        return
    if not isinstance(value, str) or not (_ISO_DATE_RE.match(value) or _ISO_DATETIME_RE.match(value)):
        raise ValueError(f"{path} must be IsoDate/ISO datetime, got {value!r}")
    try:
        offset = re.search(r"[+-](\d{2}):?(\d{2})$", value)
        if offset and (int(offset.group(1)) > 23 or int(offset.group(2)) > 59):
            raise ValueError
        (datetime.fromisoformat if "T" in value or " " in value else date.fromisoformat)(value)
    except ValueError as exc:
        raise ValueError(f"{path} must be IsoDate/ISO datetime, got {value!r}") from exc


_CARD_BILL_FACT_FIELDS = frozenset({
    "scope", "status", "card_no", "remaining_due", "statement_close_date",
    "payment_due_date", "last_payment_amount", "last_payment_date",
})


def validate_card_bill_facts(facts: list[NormalizedCardBillFact], *, facts_ok: bool | None) -> None:
    """Validate canonical remaining-due facts at the collector seam."""
    if facts_ok is True and not facts:
        raise ValueError("card_bill_facts_ok=True requires at least one fact")
    if facts and facts_ok is not True:
        raise ValueError("card_bill_facts require card_bill_facts_ok=True")
    scopes: set[str] = set()
    card_nos: set[str] = set()
    for i, fact in enumerate(facts):
        if not isinstance(fact, dict):
            raise ValueError(f"card_bill_facts[{i}] must be a dict")
        unknown = set(fact) - _CARD_BILL_FACT_FIELDS
        if unknown:
            raise ValueError(f"card_bill_facts[{i}] has unknown fields: {sorted(unknown)}")
        scope = fact.get("scope")
        if scope not in {"bank", "card"}:
            raise ValueError(f"card_bill_facts[{i}].scope must be 'bank' or 'card'")
        scopes.add(scope)
        card_no = fact.get("card_no")
        if scope == "card":
            if not isinstance(card_no, str) or not card_no.strip():
                raise ValueError(f"card_bill_facts[{i}].card_no is required for card scope")
            if card_no in card_nos:
                raise ValueError(f"card_bill_facts[{i}].card_no is duplicated")
            card_nos.add(card_no)
        elif card_no not in (None, ""):
            raise ValueError(f"card_bill_facts[{i}].card_no is forbidden for bank scope")
        status = fact.get("status")
        if status not in {"paid", "unpaid", "no_payment_required"}:
            raise ValueError(f"card_bill_facts[{i}].status is not canonical")
        remaining = fact.get("remaining_due")
        if (isinstance(remaining, bool) or not isinstance(remaining, (int, float))
                or not math.isfinite(float(remaining)) or remaining < 0
                or remaining > 100_000_000):
            raise ValueError(f"card_bill_facts[{i}].remaining_due must be finite and non-negative")
        if (status == "unpaid") != (remaining > 0):
            raise ValueError(f"card_bill_facts[{i}] status conflicts with remaining_due")
        payment_amount = fact.get("last_payment_amount")
        payment_date = fact.get("last_payment_date")
        if (payment_amount is None) != (payment_date is None):
            raise ValueError(f"card_bill_facts[{i}] last payment must be an atomic pair")
        if payment_amount is not None and (
            isinstance(payment_amount, bool)
            or not isinstance(payment_amount, (int, float))
            or not math.isfinite(float(payment_amount))
            or payment_amount < 0
            or payment_amount > 100_000_000
        ):
            raise ValueError(
                f"card_bill_facts[{i}].last_payment_amount must be finite and non-negative"
            )
        for key in ("statement_close_date", "payment_due_date", "last_payment_date"):
            _require_iso_date(fact.get(key), path=f"card_bill_facts[{i}].{key}")
    if len(scopes) > 1 or ("bank" in scopes and len(facts) > 1):
        raise ValueError("card_bill_facts must be one bank fact or card-scoped facts")


HISTORY_DOMAINS = frozenset({"twd_transactions", "card_billed_transactions"})


def _history_date(value: Any, error: str) -> date:
    if not isinstance(value, str) or not _ISO_DATE_RE.fullmatch(value):
        raise ValueError(error)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(error) from None


def validate_history_coverage(
    coverage: Any,
    *,
    expected_mode: str,
    expected_domains: frozenset[str],
) -> dict[str, Any]:
    """Validate complete, contiguous, non-sensitive history evidence."""
    error = "invalid history coverage"
    if (
        expected_mode not in {"full", "incremental"}
        or not isinstance(expected_domains, frozenset)
        or not expected_domains
        or not expected_domains <= HISTORY_DOMAINS
    ):
        raise ValueError(error)
    if not isinstance(coverage, dict) or coverage.get("mode") != expected_mode:
        raise ValueError(error)
    domains = coverage.get("domains")
    if not isinstance(domains, list) or not domains:
        raise ValueError(error)

    domain_names: list[str] = []
    identity_count = 0
    window_count = 0
    starts: list[date] = []
    ends: list[date] = []
    for domain in domains:
        if not isinstance(domain, dict):
            raise ValueError(error)
        name = domain.get("domain")
        expected = domain.get("expected")
        windows = domain.get("windows")
        if (
            name not in HISTORY_DOMAINS
            or not isinstance(expected, list)
            or not isinstance(windows, list)
        ):
            raise ValueError(error)

        if not expected:
            empty_window = domain.get("empty_window")
            if not isinstance(empty_window, dict) or windows:
                raise ValueError(error)
            try:
                empty_start = _history_date(empty_window["start"], error)
                empty_end = _history_date(empty_window["end"], error)
            except (KeyError, TypeError, ValueError):
                raise ValueError(error) from None
            if (
                empty_start > empty_end
                or empty_window.get("status") != "explicit_empty"
                or type(empty_window.get("pages")) is not int
                or empty_window["pages"] < 1
            ):
                raise ValueError(error)
            domain_names.append(name)
            window_count += 1
            starts.append(empty_start)
            ends.append(empty_end)
            continue

        expected_ranges: dict[str, tuple[date, date]] = {}
        for item in expected:
            if not isinstance(item, dict):
                raise ValueError(error)
            identity = item.get("identity")
            try:
                expected_start = _history_date(item["start"], error)
                expected_end = _history_date(item["end"], error)
            except (KeyError, TypeError, ValueError):
                raise ValueError(error) from None
            if (
                not isinstance(identity, str) or not identity.strip()
                or identity in expected_ranges
                or expected_start > expected_end
            ):
                raise ValueError(error)
            expected_ranges[identity] = (expected_start, expected_end)

        by_identity: dict[str, list[tuple[date, date]]] = {
            identity: [] for identity in expected_ranges
        }
        for window in windows:
            if not isinstance(window, dict):
                raise ValueError(error)
            identity = window.get("identity")
            if identity not in by_identity:
                raise ValueError(error)
            try:
                start = _history_date(window["start"], error)
                end = _history_date(window["end"], error)
            except (KeyError, TypeError, ValueError):
                raise ValueError(error) from None
            if (
                start > end
                or window.get("status") not in {"complete", "explicit_empty"}
                or type(window.get("pages")) is not int
                or window["pages"] < 1
            ):
                raise ValueError(error)
            by_identity[identity].append((start, end))

        for identity, (expected_start, expected_end) in expected_ranges.items():
            identity_windows = sorted(by_identity[identity])
            if not identity_windows or identity_windows[0][0] != expected_start:
                raise ValueError(error)
            covered_end = identity_windows[0][1]
            for start, end in identity_windows[1:]:
                if start != covered_end + timedelta(days=1):
                    raise ValueError(error)
                covered_end = end
            if covered_end != expected_end:
                raise ValueError(error)
            starts.append(expected_start)
            ends.append(expected_end)

        domain_names.append(name)
        identity_count += len(expected_ranges)
        window_count += len(windows)

    if len(domain_names) != len(set(domain_names)) or set(domain_names) != expected_domains:
        raise ValueError(error)

    return {
        "ok": True,
        "mode": expected_mode,
        "domains": domain_names,
        "identities": identity_count,
        "windows": window_count,
        "start": min(starts).isoformat(),
        "end": max(ends).isoformat(),
    }


@dataclass(kw_only=True)
class BankCollectResult:
    """Shared return contract for every `BankCrawler.collect()`.

    The contract is explicit: collectors may only return fields declared on this
    dataclass. There is intentionally no opaque `raw` dict escape hatch. Current
    bank-specific parser payloads are represented as named transitional fields
    until each domain is migrated into the normalized lists above.
    """
    # Normalized cross-bank fields.
    bank: str | None = None
    accounts: list[NormalizedAccount] = field(default_factory=list)
    cards: list[NormalizedCard] = field(default_factory=list)
    twd_txns: list[NormalizedTwdTxn] = field(default_factory=list)
    card_billed_txns: list[NormalizedCardBilledTxn] = field(default_factory=list)
    card_pending_txns: list[NormalizedCardPendingTxn] = field(default_factory=list)
    card_bill_facts: list[NormalizedCardBillFact] = field(default_factory=list)
    card_bill_facts_ok: bool | None = None
    balance_history: list[NormalizedBalanceHistory] = field(default_factory=list)
    daily_metrics: list[DailyMetric] = field(default_factory=list)
    telemetry: dict[str, Any] = field(default_factory=dict)
    history_coverage: dict[str, Any] | None = None

    # Explicit transitional collect fields consumed by existing persist_<bank>()
    # adapters. These replace the previous opaque `raw` escape hatch: every key
    # still allowed through the collect contract must be declared here by name.
    _all_endpoints: Any = None
    _all_resources: Any = None
    _endpoint_count: Any = None
    _final_url: Any = None
    account_options: Any = None
    accounts_queried: Any = None
    after_card_click_url: Any = None
    alert_info: Any = None
    all_cards: Any = None
    all_pages: Any = None
    amount_page_text: Any = None
    api_responses: Any = None
    asset_chart: Any = None
    available_credit_twd: Any = None
    balance_latest: Any = None
    bank_balance: Any = None
    bill_due_amount: Any = None
    bill_text: Any = None
    bill_url: Any = None
    billed_page_text: Any = None
    billed_page_url: Any = None
    billed_txns: Any = None
    billing_period: Any = None
    billing_summary: Any = None
    card_all_frames_meta: Any = None
    card_api_dump: Any = None
    card_billed: Any = None
    card_billing: Any = None
    card_bills: Any = None
    card_bill_details: Any = None
    card_detail: Any = None
    card_final_url: Any = None
    card_frame_match: Any = None
    card_frame_name: Any = None
    card_frame_text: Any = None
    card_frame_url: Any = None
    card_frames: Any = None
    card_inquiry: Any = None
    card_limit: Any = None
    card_mega_menu_dump: Any = None
    card_nav_probe: Any = None
    card_nav_probe_2: Any = None
    card_pay_frames: Any = None
    card_pay_history: Any = None
    card_pay_nav_probe: Any = None
    card_quota: Any = None
    card_quota_frames: Any = None
    card_quota_nav_probe: Any = None
    card_resources: Any = None
    card_statement_transactions: Any = None
    card_statements: Any = None
    card_submenu: Any = None
    card_summary: Any = None
    card_text: Any = None
    card_transactions: Any = None
    card_transactions_ok: bool | None = None
    card_txn_form_submitted: Any = None
    card_txn_frames: Any = None
    card_txn_nav_probe: Any = None
    card_unbilled: Any = None
    card_url: Any = None
    cards_detail: Any = None
    cards_page_text: Any = None
    cards_page_url: Any = None
    clicked_credit_card: Any = None
    credit_card: Any = None
    credit_card_frame_url: Any = None
    credit_card_month_options: Any = None
    credit_card_page_text: Any = None
    credit_card_parsed: Any = None
    credit_limit_twd: Any = None
    currency: Any = None
    dbs_card_fee_click: Any = None
    dbs_card_fee_endpoints: Any = None
    dbs_card_fee_error: Any = None
    dbs_card_fee_page: Any = None
    dbs_card_fee_page_text: Any = None
    debit_accounts: Any = None
    deposit_foreign: Any = None
    deposit_menu_audit: Any = None
    deposit_page_text: Any = None
    deposit_page_url: Any = None
    deposit_twd: Any = None
    deposit_txn_click: Any = None
    deposit_txn_page_text: Any = None
    deposit_txn_page_url: Any = None
    deposit_txn_results: Any = None
    error: Any = None
    final_url: Any = None
    frames: Any = None
    home_text: Any = None
    initial_url: Any = None
    insurance: Any = None
    investment: Any = None
    limits: Any = None
    loan: Any = None
    main_text: Any = None
    menu_dom_audit: Any = None
    nav_items: Any = None
    net_present: Any = None
    overview_text: Any = None
    overview_url: Any = None
    payment_due_date: Any = None
    pending_click_ok: bool | None = None
    pending_page_text: Any = None
    pending_page_url: Any = None
    pending_txns: Any = None
    points: Any = None
    raw_text_sample: Any = None
    summary: Any = None
    title: Any = None
    top_summary: Any = None
    totals: Any = None
    transaction_text: Any = None
    transaction_url: Any = None
    twd_account_detail_api_endpoints: Any = None
    twd_account_detail_controls: Any = None
    twd_account_detail_text: Any = None
    twd_account_detail_url: Any = None
    twd_account_drilldown_click: Any = None
    twd_account_drilldown_error: Any = None
    twd_account_drilldown_target: Any = None
    twd_deposit: Any = None
    twd_history: Any = None
    twd_inquiry: Any = None
    twd_text: Any = None
    twd_transactions: Any = None
    twd_txn_error: Any = None
    twd_txn_form_controls: Any = None
    twd_txn_frame_url: Any = None
    twd_txn_frames: Any = None
    twd_txn_month_click_endpoints: Any = None
    twd_txn_month_clicks: Any = None
    twd_txn_nav_probe: Any = None
    twd_txn_other_month_clicks: Any = None
    twd_txn_other_months_probe: Any = None
    twd_txn_page_text: Any = None
    twd_txn_results: Any = None
    twd_url: Any = None
    used_credit_twd: Any = None

    def __post_init__(self) -> None:
        self._validate_normalized_dates()
        validate_card_bill_facts(self.card_bill_facts, facts_ok=self.card_bill_facts_ok)

    def _validate_normalized_dates(self) -> None:
        for i, a in enumerate(self.accounts):
            _require_iso_date(a.get("raw_balance_date"), path=f"accounts[{i}].raw_balance_date")
        for i, c in enumerate(self.cards):
            for key in ("statement_close_date", "payment_due_date", "last_payment_date"):
                _require_iso_date(c.get(key), path=f"cards[{i}].{key}")
        for i, t in enumerate(self.twd_txns):
            _require_iso_date_or_datetime(t.get("datetime"), path=f"twd_txns[{i}].datetime")
            _require_iso_date(t.get("account_date"), path=f"twd_txns[{i}].account_date")
        for i, t in enumerate(self.card_billed_txns):
            for key in ("bill_date", "date", "post_date"):
                _require_iso_date(t.get(key), path=f"card_billed_txns[{i}].{key}")
        for i, t in enumerate(self.card_pending_txns):
            for key in ("date", "post_date"):
                _require_iso_date(t.get(key), path=f"card_pending_txns[{i}].{key}")
        for i, r in enumerate(self.balance_history):
            _require_iso_date(r.get("snapshotDate"), path=f"balance_history[{i}].snapshotDate")

    def to_dict(self) -> dict[str, Any]:
        """Serialize non-empty contract fields for existing persist adapters."""
        out: dict[str, Any] = {}
        for f in dataclass_fields(self):
            name = f.name
            if name in {"bank", "telemetry"}:
                continue
            value = getattr(self, name)
            if value is None:
                continue
            if value == [] or value == {}:
                continue
            if isinstance(value, list):
                out[name] = [dict(x) if isinstance(x, dict) else x for x in value]
            else:
                out[name] = value
        if self.telemetry:
            out["_collect_telemetry"] = self.telemetry
        return out


class _OriginGuardProxy:
    """Fail closed around every browser object operation during collection."""

    __slots__ = ("_target", "_guard", "_cache")

    def __init__(self, target, guard, cache=None):
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_guard", guard)
        object.__setattr__(self, "_cache", cache if cache is not None else {})
        self._cache[id(target)] = self

    def _wrap(self, value):
        if value is None or isinstance(value, (str, bytes, int, float, bool)):
            return value
        if isinstance(value, list):
            return [self._wrap(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._wrap(item) for item in value)
        if isinstance(value, dict):
            return {key: self._wrap(item) for key, item in value.items()}
        cached = self._cache.get(id(value))
        return cached if cached is not None else type(self)(value, self._guard, self._cache)

    @staticmethod
    def _unwrap(value):
        return value._target if isinstance(value, _OriginGuardProxy) else value

    def __getattr__(self, name):
        self._guard()
        value = getattr(self._target, name)
        self._guard()
        if not callable(value):
            return self._wrap(value)

        def guarded(*args, **kwargs):
            self._guard()
            result = value(
                *(self._unwrap(arg) for arg in args),
                **{key: self._unwrap(item) for key, item in kwargs.items()},
            )
            self._guard()
            return self._wrap(result)

        return guarded

    def __setattr__(self, name, value):
        if name in self.__slots__:
            object.__setattr__(self, name, value)
            return
        self._guard()
        setattr(self._target, name, self._unwrap(value))
        self._guard()

    def __enter__(self):
        self._guard()
        result = self._target.__enter__()
        self._guard()
        return self._wrap(result)

    def __exit__(self, *args):
        self._guard()
        result = self._target.__exit__(*args)
        self._guard()
        return result

    def __bool__(self):
        self._guard()
        return bool(self._target)

    def __repr__(self):
        return "<origin-guarded browser object>"


@dataclass
class BankCrawler(ABC):
    """銀行爬蟲基類。"""
    name: str
    session_dir: Path = field(init=False)
    collector: ResponseCollector | None = field(init=False, default=None)
    transaction_cursors: dict[str, dict[str, date]] = field(
        init=False, default_factory=dict,
    )

    HISTORY_COVERAGE_REQUIRED: ClassVar[bool] = False
    HISTORY_COVERAGE_DOMAINS: ClassVar[frozenset[str]] = frozenset()

    def configure_transaction_cursor(
        self,
        domain: str,
        cursor: dict[str, date],
    ) -> None:
        if (
            domain not in HISTORY_DOMAINS
            or any(
                not isinstance(identity, str) or not identity.strip() or type(value) is not date
                for identity, value in cursor.items()
            )
        ):
            raise ValueError("transaction cursor must map non-empty identities to dates")
        self.transaction_cursors[domain] = dict(cursor)

    def transaction_start_for(
        self,
        identity: str,
        *,
        domain: str = "twd_transactions",
    ) -> date | None:
        return getattr(self, "transaction_cursors", {}).get(domain, {}).get(identity)

    def transaction_window_start(
        self,
        identity: str,
        *,
        floor: date,
        overlap_days: int = 7,
        domain: str = "twd_transactions",
    ) -> date:
        if type(overlap_days) is not int or not 0 <= overlap_days <= 31:
            raise ValueError("overlap_days must be an integer from 0 through 31")
        mode = os.environ.get("BANK_CRAWLER_HISTORY_MODE", "full")
        if mode not in {"full", "incremental"}:
            raise ValueError(f"invalid BANK_CRAWLER_HISTORY_MODE: {mode!r}")
        if mode == "full":
            return floor
        cursor = self.transaction_start_for(identity, domain=domain)
        if cursor is None:
            return floor
        return max(floor, cursor - timedelta(days=overlap_days))

    # 一個 user_data_dir 持久化 session 可信的最長秒數。
    # 預設 3 分鐘（使用者指示 2026-06-17）— 為什麼這麼短：
    #   1. 銀行 server-side session 通常 5-15 分鐘逾時，client cookie 仍在但已
    #      被踢；下次再用會出現「pseudo logged-in 但頁面是 stub（text len<200）」
    #      灰色狀態，crawler 看 cookie 存在以為登入成功，所有 navigation 卻全
    #      fail，但 result 卻被誤判 status=done。詳見 SCSB job 43 案例
    #      (2026-06-16 17:02-17:03)。
    #   2. 我們的 sync 通常 1 分鐘內結束，3 分鐘 buffer 涵蓋連續手測的場景，
    #      但避開所有「跨 sync 殘留」風險。
    #   3. 重 login 成本不算貴（每家 10-30s），比 stale session 抓 0 筆值得。
    # 子類可 override：例 HSBC 首次登入有裝置綁定 OTP，可拉到 600 秒減少 OTP 觸發。
    SESSION_MAX_AGE_SECONDS: int = 180
    USES_SHARED_LOGIN_CHECKPOINTS: ClassVar[bool] = False
    CREDENTIAL_HOSTS: ClassVar[frozenset[str]] = frozenset()
    _shared_dialog_blocked: bool = False

    def __post_init__(self):
        self.session_dir = DATA_ROOT / f"{self.name}_session"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        # C-3 修法 (2026-06-17): per-bank captcha 暫存檔路徑 (放 session_dir 內)。
        # 12 家共用 /tmp/captcha_tmp.png 已踩過 race condition,
        # 改放 session_dir/captcha.png 確保每家獨立, sync 並行不互踩.
        # solve_captcha / wait_captcha_stable 都需 caller 傳 tmp_path=self.captcha_tmp.
        self.captcha_tmp: Path = self.session_dir / "captcha.png"

    def _session_age_seconds(self) -> float | None:
        """回傳 session_dir 內任一 cookie/state 檔的最新 mtime 距現在秒數。

        無檔（首次 / 已被清）回 None；用於 _enforce_session_freshness。
        看的檔：Chromium 持久 profile 內常見的 Cookies / Default/Cookies /
        Local State，挑最新的 mtime。
        """
        candidates: list[float] = []
        for sub in ("Cookies", "Default/Cookies", "Local State",
                    "Default/Local Storage", "Default/Session Storage"):
            p = self.session_dir / sub
            try:
                if p.exists():
                    candidates.append(p.stat().st_mtime)
            except OSError:
                pass
        if not candidates:
            return None
        import time as _t
        return _t.time() - max(candidates)

    def _enforce_session_freshness(self) -> None:
        """若 session_dir 上次活動超過 SESSION_MAX_AGE_SECONDS，整個 dir 砍掉重建。

        為什麼整個砍而不只刪 cookies：Chromium user_data_dir 內 Cookies、
        Session Storage、Local State、IndexedDB 互相依賴；只刪 Cookies 容易
        造成「半 stale」反而更難 debug。砍掉重建 = 強制走完整 login，path
        well-tested。

        什麼時機觸發：BankCrawler.run() 開瀏覽器前。
        """
        import shutil
        import sys as _sys
        age = self._session_age_seconds()
        if age is None:
            return  # 首次 / 已空，沒事
        if age <= self.SESSION_MAX_AGE_SECONDS:
            return  # 還新鮮，沿用
        # 過期了
        print(
            f"[{self.name}][session] age={age:.0f}s > max={self.SESSION_MAX_AGE_SECONDS}s "
            f"→ 砍 {self.session_dir} 強制重 login",
            file=_sys.stderr,
        )
        try:
            shutil.rmtree(self.session_dir)
        except OSError as e:
            print(f"[{self.name}][session] rmtree 失敗（best-effort）: {e}",
                  file=_sys.stderr)
        self.session_dir.mkdir(parents=True, exist_ok=True)

    @abstractmethod
    def login(self, page) -> bool:
        """填表登入，回傳是否成功。"""

    def prepare_login_page(self, page) -> None:
        raise NotImplementedError

    def is_authenticated(self, page) -> bool:
        raise NotImplementedError

    def submit_credentials_once(self, page) -> None:
        raise NotImplementedError

    def prepare_captcha_resubmit(self, page) -> None:
        """Prepare one reducer-authorized CAPTCHA resubmission, if needed."""

    def login_checkpoint_rules(self) -> tuple[LoginCheckpointRule, ...]:
        return ()

    def _recover_late_authentication(self, page, error: Exception) -> bool:
        """Allow a bank-specific, non-resubmitting auth recheck after a terminal."""
        return False

    def _shared_login(self, page) -> bool:
        if not self._credential_origin_allowed(page):
            reduce_login_checkpoint(
                CheckpointPhase.PRE_SUBMIT,
                LoginBudget(),
                CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER),
            )
        self.prepare_login_page(page)
        if not self._credential_origin_allowed(page):
            reduce_login_checkpoint(
                CheckpointPhase.PRE_SUBMIT,
                LoginBudget(),
                CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER),
            )
        rules = self.login_checkpoint_rules()
        if len({rule.name for rule in rules}) != len(rules):
            reduce_login_checkpoint(
                CheckpointPhase.PRE_SUBMIT,
                LoginBudget(),
                CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER),
            )
        if any(rule.bank != self.name for rule in rules):
            reduce_login_checkpoint(
                CheckpointPhase.PRE_SUBMIT,
                LoginBudget(),
                CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER),
            )

        action_counts = {rule.name: 0 for rule in rules}
        phase = CheckpointPhase.PRE_SUBMIT
        budget = LoginBudget()
        max_steps = sum(
            rule.max_actions for rule in rules if rule.is_clickable
        ) + 8

        for _ in range(max_steps):
            if getattr(self, "_shared_dialog_blocked", False):
                reduce_login_checkpoint(
                    phase,
                    budget,
                    CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER),
                )
            if not self._credential_origin_allowed(page):
                reduce_login_checkpoint(
                    phase,
                    budget,
                    CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER),
                )
            active_rules = tuple(
                rule
                if (
                    (not rule.is_clickable or action_counts[rule.name] < rule.max_actions)
                    and (
                        rule.kind is not CheckpointKind.PROTOCOL_RESUBMIT
                        or (
                            phase is CheckpointPhase.POST_SUBMIT
                            and budget.credential_submissions == 1
                            and budget.protocol_resubmits == 0
                        )
                    )
                    and (
                        rule.kind is not CheckpointKind.CAPTCHA_RETRY
                        or (
                            phase is CheckpointPhase.POST_SUBMIT
                            and budget.credential_submissions == 1
                            and budget.captcha_resubmits == 0
                        )
                    )
                    and (
                        rule.kind is not CheckpointKind.STARTUP_RECOVERY
                        or budget.reloads == 0
                    )
                )
                else replace(
                    rule,
                    kind=CheckpointKind.UNKNOWN_BLOCKER,
                    action_selector=DEFAULT_ACTION_SELECTOR,
                    action_texts=(),
                    max_actions=1,
                )
                for rule in rules
                if phase in rule.phases
            )
            outcome = evaluate_login_checkpoint(
                page,
                bank=self.name,
                phase=phase,
                rules=active_rules,
                is_authenticated=self.is_authenticated,
                is_scope_owned=lambda frame: self._frame_origin_allowed(page, frame),
            )
            if not self._credential_origin_allowed(page):
                reduce_login_checkpoint(
                    phase,
                    budget,
                    CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER),
                )
            active_rules_by_name = {rule.name: rule for rule in active_rules}
            outcome = validate_login_checkpoint_outcome(outcome, active_rules)
            if getattr(self, "_shared_dialog_blocked", False):
                reduce_login_checkpoint(
                    phase,
                    budget,
                    CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER),
                )
            next_phase, next_budget = reduce_login_checkpoint(phase, budget, outcome)

            if (
                outcome.rule_name in active_rules_by_name
                and active_rules_by_name[outcome.rule_name].is_clickable
            ):
                action_counts[outcome.rule_name] += 1
            if next_budget.credential_submissions == budget.credential_submissions + 1:
                if next_budget.captcha_resubmits == budget.captcha_resubmits + 1:
                    self.prepare_captcha_resubmit(page)
                if not self._credential_origin_allowed(page):
                    reduce_login_checkpoint(
                        phase,
                        budget,
                        CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER),
                    )
                self.submit_credentials_once(page)
            if next_budget.reloads == budget.reloads + 1:
                page.reload()
                self.prepare_login_page(page)
            if (
                phase is CheckpointPhase.POST_SUBMIT_SETTLE
                and outcome.kind is CheckpointKind.AUTHENTICATED
            ):
                return True
            phase, budget = next_phase, next_budget

        reduce_login_checkpoint(
            phase,
            budget,
            CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER),
        )
        return False  # pragma: no cover - reducer always raises

    def _credential_origin_allowed(self, page) -> bool:
        try:
            return self._exact_https_origin_allowed(page.url, self.CREDENTIAL_HOSTS)
        except Exception:
            return False

    @staticmethod
    def _exact_https_origin_allowed(url: str, hosts: frozenset[str]) -> bool:
        try:
            current = urlparse(url or "")
            return (
                current.scheme.lower() == "https"
                and (current.hostname or "").lower() in hosts
                and current.port in (None, 443)
                and current.username is None
                and current.password is None
            )
        except Exception:
            return False

    def _frame_origin_allowed(self, page, frame) -> bool:
        current = frame
        main_frame = getattr(page, "main_frame", None)
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if main_frame is not None and current is main_frame:
                return self._credential_origin_allowed(page)
            try:
                parsed = urlparse(current.url or "")
            except Exception:
                return False
            if parsed.scheme == "about" and parsed.path in {"blank", "srcdoc"}:
                current = getattr(current, "parent_frame", None)
                continue
            return self._exact_https_origin_allowed(
                current.url, self.CREDENTIAL_HOSTS
            )
        return False

    @abstractmethod
    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        """造訪各功能頁、觸發查詢，回傳共同 BankCollectResult contract。"""

    # ─────────────────────────────────────────────────────────
    # Shared macOS browser fingerprint（所有銀行預設繼承）
    # ─────────────────────────────────────────────────────────
    # UA / Client Hints / navigator properties 必須一起 macOS 化，避免互相矛盾。
    # 子類仍可覆寫 FETCH_*，但 production banks 預設共用這套設定。
    # 詳見 wiki/concepts/bank-crawler-platform-spoof-rule.md。
    FETCH_USERAGENT: ClassVar[str] = MACOS_UA
    FETCH_EXTRA_HEADERS: ClassVar[dict[str, str]] = {
        "sec-ch-ua-platform": '"macOS"',
        "sec-ch-ua-platform-version": '"15.0.0"',
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    }
    FETCH_LOCALE: ClassVar[str] = "zh-TW"
    FETCH_INIT_SCRIPT: ClassVar[str] = MACOS_SPOOF_JS
    FETCH_REAL_CHROME: ClassVar[bool] = False

    def _build_fetch_kwargs(self) -> dict:
        """組裝 StealthyFetcher.fetch 額外參數，把 init script 寫成臨時檔。

        子類覆寫上面 FETCH_* class var 即可生效。
        回傳 dict 內含 __cleanups__ key (list of callable)，呼叫者跑完要逐一呼叫。
        """
        import tempfile
        kw: dict = {}
        cleanups: list = []
        if self.FETCH_USERAGENT:
            kw["useragent"] = self.FETCH_USERAGENT
        if self.FETCH_EXTRA_HEADERS:
            kw["extra_headers"] = dict(self.FETCH_EXTRA_HEADERS)
        if self.FETCH_LOCALE:
            kw["locale"] = self.FETCH_LOCALE
        if self.FETCH_INIT_SCRIPT:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".js", delete=False, encoding="utf-8",
            ) as f:
                f.write(self.FETCH_INIT_SCRIPT)
            kw["init_script"] = f.name
            def _cleanup(path=f.name):
                with contextlib.suppress(OSError): Path(path).unlink()
            cleanups.append(_cleanup)
        if self.FETCH_REAL_CHROME:
            kw["real_chrome"] = True
        kw["__cleanups__"] = cleanups
        return kw

    def _execute_browser_flow(
        self,
        login_url: str,
        *,
        headless: bool,
        page_action,
        fetch_kwargs: dict,
    ) -> None:
        StealthyFetcher.fetch(
            login_url,
            headless=headless,
            network_idle=False,
            load_dom=True,
            wait=2000,
            timeout=180000,
            user_data_dir=str(self.session_dir),
            page_action=page_action,
            google_search=True,
            # 2026-06-18 evidence: these three protections pass CTBC's
            # PerimeterX/HUMAN BotManager; solve_cloudflare breaks this SPA path.
            hide_canvas=True,
            block_webrtc=True,
            dns_over_https=True,
            **fetch_kwargs,
        )

    def run(self, login_url: str, headless: bool = False) -> dict:
        """完整流程：開瀏覽器 → 登入 → 抓取 → **登出** → 回傳資料。

        logout 走 finally 保證執行（包含 collect raise 的 case），best-effort
        不影響 result。為什麼：crawler 不正常登出會讓銀行 server-side session
        殘留，下次登入撞「重複登入」/「上次未正常登出」彈窗（CTBC, 台新 …）。
        """
        # StealthyFetcher is imported at module scope so tests can monkeypatch
        # backend.core.base.StealthyFetcher.fetch and exercise run() without a browser.

        # 開瀏覽器前先檢查 session 是否過期；過期就砍掉強制重 login。
        # 詳見 SESSION_MAX_AGE_SECONDS docstring（SCSB 2026-06-16 案例）。
        self._enforce_session_freshness()

        collector = ResponseCollector(host_filter=self._host_filter())
        result: dict = {}

        def page_action(page):
            collector.attach(page)
            self.collector = collector  # 讓 login() 能用攔截到的 API（如 captcha base64）
            self._shared_dialog_blocked = False
            self.attach_shared_dialog_handler(page)
            logged_in = False
            try:
                try:
                    ok = self._shared_login(page)
                except Exception as e:
                    try:
                        recovered = self._recover_late_authentication(page, e)
                    except Exception:
                        recovered = False
                    if recovered:
                        ok = True
                    else:
                        # login 子類可能 raise（例：ScsbLoginError、TaishinLoginError）
                        # — 不讓 StealthyFetcher 內部 swallow 變成 silent done。
                        # 把 exception 訊息寫進 result，由 sync_runner 轉成 status=error。
                        import sys as _sys
                        safe_code = getattr(e, "safe_code", None)
                        if isinstance(e, (LoginCheckpointBlocked, LoginInteractionRequired)):
                            msg = f"{type(e).__name__}: {e}"
                        elif isinstance(safe_code, str) and safe_code in {"captcha_ocr_failed"}:
                            msg = f"{type(e).__name__}: code={safe_code}"
                        else:
                            msg = f"{type(e).__name__}: login failed"
                        print(
                            f"[{self.name}][login] raise → {type(e).__name__}: details withheld",
                            file=_sys.stderr,
                        )
                        result["error"] = msg
                        return page

                if not ok:
                    result["error"] = "login_failed"
                    return page
                logged_in = True
                try:
                    def ensure_collect_origin() -> None:
                        if not self._credential_origin_allowed(page):
                            reduce_login_checkpoint(
                                CheckpointPhase.POST_SUBMIT_SETTLE,
                                LoginBudget(credential_submissions=1),
                                CheckpointOutcome(CheckpointKind.UNKNOWN_BLOCKER),
                            )

                    ensure_collect_origin()
                    collect_result = self.collect(
                        _OriginGuardProxy(page, ensure_collect_origin), collector
                    )
                    ensure_collect_origin()
                    if not isinstance(collect_result, BankCollectResult):
                        raise TypeError(
                            f"{self.__class__.__name__}.collect() must return "
                            f"BankCollectResult, got {type(collect_result).__name__}"
                        )
                    if collect_result.error is None and collect_result.card_bill_facts_ok is None:
                        raise ValueError(
                            f"{self.__class__.__name__}.collect() must publish "
                            "card_bill_facts_ok at the crawler boundary"
                        )
                    result["data"] = collect_result.to_dict()
                except Exception as e:
                    # collect 階段 raise（包含 SCSB/Taishin 等明細查詢 raise）
                    # 一樣寫進 error，但 logged_in 仍 True → finally 會跑 logout。
                    import sys as _sys
                    if isinstance(e, (LoginCheckpointBlocked, LoginInteractionRequired)):
                        msg = f"collect_failed: {type(e).__name__}: {e}"
                        print(f"[{self.name}][collect] raise → {msg}", file=_sys.stderr)
                    else:
                        msg = f"collect_failed: {type(e).__name__}"
                        print(
                            f"[{self.name}][collect] raise → {msg}: details withheld",
                            file=_sys.stderr,
                        )
                    result["error"] = msg
            finally:
                # 已登入且仍在owned origin才best-effort logout；foreign origin零互動。
                if logged_in and self._credential_origin_allowed(page):
                    try:
                        self.logout(page)
                    except Exception as e:
                        import sys as _sys
                        print(
                            f"[{self.name}][logout] exception {type(e).__name__} "
                            "(details withheld; best-effort, swallow)",
                            file=_sys.stderr,
                        )
            return page

        # 所有 crawler 從 base 繼承同一套 macOS fingerprint spoof。
        # 詳見 wiki/concepts/bank-crawler-platform-spoof-rule.md
        fetch_kwargs = self._build_fetch_kwargs()
        cleanups = fetch_kwargs.pop("__cleanups__", [])

        try:
            # Stealth defaults shared by all banks: no solve_cloudflare hook; keep the
            # proven canvas/WebRTC/DNS protections and each adapter's fetch kwargs.
            self._execute_browser_flow(
                login_url,
                headless=headless,
                page_action=page_action,
                fetch_kwargs=fetch_kwargs,
            )
        finally:
            for c in cleanups:
                with contextlib.suppress(Exception): c()
        return result


    def attach_shared_dialog_handler(self, page) -> None:
        """Dismiss opaque JS dialogs and let the typed lifecycle fail closed."""
        def _on_dialog(dialog):
            self._shared_dialog_blocked = True
            with contextlib.suppress(Exception):
                dialog.dismiss()

        page.on("dialog", _on_dialog)

    def _host_filter(self) -> str:
        return ""

    # ─────────────────────────────────────────────────────────
    # 通用「登出」處理（所有銀行共用）
    # ─────────────────────────────────────────────────────────
    # 預設登出按鈕文字優先順序（找第一個可見且符合的元素點下）。
    # 為什麼必須做：crawler 不主動登出 → server-side session 殘留 →
    # 下次登入會被銀行視為「重複登入」/「上次未正常登出」，
    # 不少銀行（CTBC, 台新…）會跳「確認登入」彈窗甚至直接擋登入。
    #
    # ⚠️ W (2026-06-17): 移除 "結束" — 太籠統會誤點到 "結束查詢"、"結束會員"、
    # "結束作業" 之類非登出按鈕，反而讓 session 沒真正結束 → 下次撞 ghost。
    # 若某家銀行真的只有 "結束" 字樣，請在該 crawler subclass override LOGOUT_BTN_TEXTS。
    LOGOUT_BTN_TEXTS = (
        "登出", "安全登出", "Sign Out", "Sign out", "Logout", "Log Out", "Log out",
        "登出系統", "離開系統",
    )
    LOGOUT_AVOID_BTN_TEXTS = ("取消", "Cancel", "No", "否", "返回",)

    def logout(self, page) -> bool:
        """嘗試讓 user 從銀行 server 正常登出。

        預設實作：掃所有 frame 找含 LOGOUT_BTN_TEXTS 的可見按鈕/連結，點第一個。
        子類可 override：例如 SPA 銀行 (CTBC, HSBC) 走 frontend route，
        或某些銀行登出按鈕藏在 user menu 裡需先 hover 父選單。

        回傳：True=成功點到登出按鈕；False=找不到（已 best-effort 不必 raise）。
        """
        import sys as _sys
        kick_json = json.dumps(list(self.LOGOUT_BTN_TEXTS))
        avoid_json = json.dumps(list(self.LOGOUT_AVOID_BTN_TEXTS))
        for f in page.frames:
            try:
                clicked = f.evaluate(f"""
                    () => {{
                      const wants = {kick_json};
                      const avoidSet = new Set({avoid_json});
                      const cand = document.querySelectorAll(
                        'button, a, input[type=button], input[type=submit], [role=button]'
                      );
                      const visible = [];
                      for (const b of cand) {{
                        const t = (b.textContent || b.value || '').trim();
                        if (!t || t.length > 30) continue;
                        if (avoidSet.has(t)) continue;
                        const rect = b.getBoundingClientRect();
                        if (rect.width <= 0 || rect.height <= 0) continue;
                        const cs = window.getComputedStyle(b);
                        if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                        visible.push({{ el: b, text: t, tag: b.tagName }});
                      }}
                      // 依優先順序找匹配（exact match 優先，含 partial 次之）
                      for (const want of wants) {{
                        for (const v of visible) {{
                          if (v.text === want) {{
                            v.el.click();
                            return v.text + ' (' + v.tag + ', exact)';
                          }}
                        }}
                      }}
                      for (const want of wants) {{
                        for (const v of visible) {{
                          if (v.text.includes(want)) {{
                            v.el.click();
                            return v.text + ' (' + v.tag + ', partial)';
                          }}
                        }}
                      }}
                      return null;
                    }}
                """)
                if clicked:
                    print(f"[{self.name}][logout] ✓ 已點登出控制", file=_sys.stderr)
                    try:
                        page.wait_for_timeout(3000)  # 等 server 處理 logout
                    except Exception:
                        pass
                    return True
            except Exception as e:
                print(
                    f"[{self.name}][logout] frame 掃描失敗: {type(e).__name__}",
                    file=_sys.stderr,
                )
                continue
        print(f"[{self.name}][logout] ⚠️ 沒找到登出按鈕（best-effort，繼續）", file=_sys.stderr)
        return False

    @staticmethod
    def mask_card(num: str) -> str:
        s = re.sub(r"\D", "", str(num or ""))
        if len(s) >= 8:
            return f"{s[:4]}****{s[-4:]}"
        return num or ""
