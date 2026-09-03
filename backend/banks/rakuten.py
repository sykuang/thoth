#!/usr/bin/env python3
"""樂天國際銀行個人網銀 crawler。

登入頁是 Angular SPA；帳密由銀行前端自行做 E2E 加密後送出。本 crawler 只操作
真實表單，登入後從「臺幣存款」頁的已解密 DOM 讀取帳戶、餘額與六個月交易。
"""
from __future__ import annotations

import contextlib
from calendar import monthrange
from datetime import date, datetime, timedelta
import os
import re
import time
from typing import ClassVar
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from scrapling.fetchers import StealthySession

from backend.core.base import (
    BankCollectResult,
    BankCrawler,
    ResponseCollector,
    _OriginGuardProxy,
    validate_history_coverage,
)

from backend.core.captcha import solve_captcha, wait_captcha_stable
from backend.core.creds import RakutenCreds
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointPhase,
    LoginBudget,
    LoginCheckpointBlocked,
    LoginCheckpointTerminal,
    LoginCheckpointRule,
    evaluate_login_checkpoint,
    reduce_login_checkpoint,
    validate_login_checkpoint_outcome,
)

BASE = "https://www.rakuten-bank.com.tw/ebank/cgn/cgnot0001/010"
TWD_URL = "https://www.rakuten-bank.com.tw/ebank/ctw/ctwqu0001/010"
TWD_PATH_HINT = "/ctw/ctwqu0001/"
LOGIN_PATH_HINT = "/cgn/cgnot0001/010"
CAPTCHA_IMG = "captcha-image img"
LOADER_SELECTOR = "modal-loader .modal_loading"
QUERY_PATH = "/ixtein/adapters/ebank/txns/channel-ctw/CTWQU0001/011"


def _endpoint_key(url: str) -> str:
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) >= 2 and parts[-2].isupper():
        return f"{parts[-2]}_{parts[-1]}"
    return parts[-1] if parts else "unknown"


def _account_number(label: str) -> str | None:
    match = re.search(r"(?<!\d)\d(?:[ -]?\d){9,15}(?!\d)", label)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(0))
    return digits if 10 <= len(digits) <= 16 else None


def _month_labels(labels: list[str]) -> list[str]:
    out: list[str] = []
    for label in labels:
        match = re.fullmatch(r"\s*(\d{4}/(?:0[1-9]|1[0-2]))\s+活存明細\s*", label)
        if not match:
            continue
        canonical = f"{match.group(1)} 活存明細"
        if canonical not in out:
            out.append(canonical)
    return out


def _six_month_labels(labels: list[str]) -> list[str]:
    # ponytail: 樂天臺幣明細頁只提供最近 6 個月下拉，無日期區間輸入、無「更早」入口
    # （2026-07-28 real-account DOM 實證：dropdown 恰 6 個 a.dropdown-item）。
    # 這裡的 !=6 是 fail-closed 偵測 UI 改版，不是自訂上限。
    # 要抓更早期間，需另探未使用的 endpoint（CHMQU0001_010 / CCMQU0003_02x，皆未實證）。
    months = _month_labels(labels)
    if len(months) != 6:
        raise RuntimeError("樂天月份選單不是預期六個月份")
    return months


def _row_from_dom(cells: list[str]) -> dict | None:
    if (
        len(cells) != 6
        or any(not isinstance(cell, str) for cell in cells)
        or any(
            len(cell.encode("utf-8")) > 4_000
            or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", cell)
            for cell in cells
        )
    ):
        return None
    date_time = cells[0].split()
    descriptions = [line.strip() for line in cells[1].splitlines() if line.strip()]
    income = cells[2].strip()
    expend = cells[3].strip()
    if (
        len(date_time) != 2
        or not 1 <= len(descriptions) <= 2
        or bool(income) == bool(expend)
    ):
        return None
    return {
        "sysDate": date_time[0],
        "sysTime": date_time[1] if len(date_time) > 1 else "",
        "txDesc": descriptions[0],
        "nickNameOrAcct": descriptions[1] if len(descriptions) > 1 else None,
        "amt": income or expend,
        "amtSign": bool(income),
        "balance": cells[4].strip(),
        "memo": cells[5].strip(),
    }


def _selection_matches(root: str, selected: str, expected: str) -> bool:
    if root == "simple-dropdown2":
        number = _account_number(expected)
        return number is not None and _account_number(selected) == number
    return selected.strip() == expected.strip()


def _unique_option_index(labels: list[str], expected: str) -> int | None:
    matches = [
        index
        for index, label in enumerate(labels)
        if label.strip() == expected.strip()
    ]
    return matches[0] if len(matches) == 1 else None


def _any_visible(page, selector: str) -> bool:
    locators = page.locator(selector)
    return any(locators.nth(index).is_visible() for index in range(locators.count()))


def _click_visible_login(page) -> bool:
    candidates = page.locator("a.btn.btn-primary:visible").filter(
        has_text=re.compile(r"^\s*登入\s*$"),
    )
    if candidates.count() != 1:
        return False
    button = candidates.first
    classes = button.get_attribute("class") or ""
    if not button.is_visible() or not button.is_enabled() or "disabled" in classes.split():
        return False
    button.click()
    return True


def _is_twd_query_request(request) -> bool:
    parsed = urlparse(request.url)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "www.rakuten-bank.com.tw"
        and parsed.path == QUERY_PATH
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
        and request.method == "POST"
    )



def _view_ready(before_rows: str | None, state: dict) -> bool:
    rows = str(state.get("rows") or "")
    no_data = bool(state.get("noData"))
    if before_rows is not None:
        return (bool(rows) and rows != before_rows) or (not rows and no_data)
    return bool(rows) or no_data


class RakutenLoginError(RuntimeError):
    """樂天登入送出後未成功；不得自動重送帳密。"""


class RakutenCrawler(BankCrawler):
    USES_SHARED_LOGIN_CHECKPOINTS: ClassVar[bool] = True
    SAFE_COLLECT_GUARDS = frozenset({
        "rakuten-twd-history-cursor",
        "rakuten-twd-history-dom",
        "rakuten-twd-history-frame",
        "rakuten-twd-history-inventory",
        "rakuten-twd-history-months",
        "rakuten-twd-history-range",
        "rakuten-twd-history-response-cardinality",
        "rakuten-twd-history-result",
    })
    HISTORY_COVERAGE_REQUIRED: ClassVar[bool] = True
    HISTORY_COVERAGE_DOMAINS: ClassVar[frozenset[str]] = frozenset({"twd_transactions"})
    FETCH_TIMEZONE_ID: ClassVar[str | None] = "Asia/Taipei"
    CREDENTIAL_HOSTS = frozenset({"www.rakuten-bank.com.tw"})
    FETCH_REAL_CHROME = True  # Imperva/Incapsula 會擋 bundled Chromium。
    VISIBLE_CONFIRM_SELECTOR = (
        "modal-confirm .modal.show:visible, modal-projection .modal.show:visible"
    )
    DUP_LOGIN_BODY = (
        "帳號重複登入 您已在其他裝置登入，繼續登入將會 "
        "登出前一個裝置，是否以此裝置登入？"
    )
    REFERRAL_PROMO_PREFIX = "推薦獎金NT$500無上限+抽沖繩來回機票，新戶也享NT$300現金~"
    INSURANCE_PROMO_PREFIX = "輸入專案代碼【RICB】投保即可抽大獎"
    LOGOUT_BODY = "登出網路銀行 確認登出本系統？"

    def __init__(self) -> None:
        super().__init__(name="rakuten")
        self.creds = RakutenCreds.load()

    def _host_filter(self) -> str:
        return "rakuten-bank.com.tw"

    @staticmethod
    def _validated_account_options(
        selected_label: str,
        option_labels: list[str],
    ) -> list[tuple[str, str]]:
        error = "rakuten-twd-history-inventory"
        selected = _account_number(selected_label)
        if selected is None or not option_labels:
            raise RuntimeError(error)
        options: list[tuple[str, str]] = []
        seen: set[str] = set()
        for label in option_labels:
            identity = _account_number(label)
            if identity is None or identity in seen:
                raise RuntimeError(error)
            options.append((identity, label))
            seen.add(identity)
        if selected not in seen:
            raise RuntimeError(error)
        return options

    def _history_plan(
        self,
        identity: str,
        labels: list[str],
        as_of: date,
    ) -> list[dict]:
        if any(
            not isinstance(label, str)
            or re.fullmatch(r"\s*\d{4}/(?:0[1-9]|1[0-2])\s+活存明細\s*", label) is None
            for label in labels
        ):
            raise RuntimeError("rakuten-twd-history-months")
        try:
            months = _six_month_labels(labels)
        except RuntimeError:
            raise RuntimeError("rakuten-twd-history-months") from None
        parsed = [date(int(label[:4]), int(label[5:7]), 1) for label in months]
        expected = []
        cursor = date(as_of.year, as_of.month, 1)
        for _ in range(6):
            expected.append(cursor)
            cursor = (cursor - timedelta(days=1)).replace(day=1)
        if parsed != expected:
            raise RuntimeError("rakuten-twd-history-months")

        mode = os.environ.get("BANK_CRAWLER_HISTORY_MODE", "full")
        if mode not in {"full", "incremental"}:
            raise ValueError(f"invalid BANK_CRAWLER_HISTORY_MODE: {mode!r}")
        oldest = parsed[-1]
        start = oldest
        persisted = self.transaction_start_for(identity, domain="twd_transactions")
        if mode == "incremental" and persisted is not None and persisted > as_of:
            raise RuntimeError("rakuten-twd-history-cursor")
        if mode == "incremental" and persisted is not None:
            start = max(oldest, (persisted - timedelta(days=7)).replace(day=1))

        plan = []
        for label, month_start in reversed(list(zip(months, parsed, strict=True))):
            if month_start < start:
                continue
            month_end = date(
                month_start.year,
                month_start.month,
                monthrange(month_start.year, month_start.month)[1],
            )
            plan.append({
                "label": label,
                "start": month_start,
                "end": min(month_end, as_of),
            })
        return plan

    @staticmethod
    def _validate_history_dom(dom: dict, row_count: int) -> None:
        error = "rakuten-twd-history-dom"
        keys = {
            "table_count", "visible_tables", "headers", "raw_rows", "no_data_count",
            "invalid_cells", "pager", "busy", "dialogs", "alerts",
        }
        if (
            not isinstance(dom, dict)
            or set(dom) != keys
            or type(row_count) is not int
            or row_count < 0
            or any(
                type(dom.get(key)) is not int or dom[key] < 0
                for key in keys - {"headers"}
            )
            or not isinstance(dom.get("headers"), list)
            or any(type(header) is not str for header in dom["headers"])
            or dom["raw_rows"] != row_count
            or any(
                dom[key] != 0
                for key in ("invalid_cells", "pager", "busy", "dialogs", "alerts")
            )
        ):
            raise RuntimeError(error)
        if row_count:
            if (
                dom["table_count"] != 1
                or dom["visible_tables"] != 1
                or dom["no_data_count"] != 0
                or dom["headers"] != [
                    "交易時間", "交易說明 對方帳號或暱稱", "轉入", "轉出",
                    "帳戶餘額", "備註", "",
                ]
            ):
                raise RuntimeError(error)
        elif (
            dom["table_count"] != 0
            or dom["visible_tables"] != 0
            or dom["no_data_count"] != 1
            or dom["headers"]
        ):
            raise RuntimeError(error)

    @staticmethod
    def _validated_history_result(result: dict) -> dict:
        error = "rakuten-twd-history-result"
        if not isinstance(result, dict) or set(result) != {
            "account_no", "accounts", "txDetails", "selected_month", "dom", "receipt",
            "transport",
        }:
            raise RuntimeError(error)
        identity = result.get("account_no")
        rows = result.get("txDetails")
        accounts = result.get("accounts")
        dom = result.get("dom")
        receipt = result.get("receipt")
        transport = result.get("transport")
        if (
            not isinstance(identity, str)
            or re.fullmatch(r"\d{10,16}", identity) is None
            or not isinstance(rows, list)
            or len(rows) > 50_000
            or not isinstance(accounts, list)
            or len(accounts) != 1
            or not isinstance(accounts[0], dict)
            or set(accounts[0]) != {"acctNo", "balance"}
            or accounts[0].get("acctNo") != identity
            or not isinstance(accounts[0].get("balance"), str)
            or re.fullmatch(r"\s*(?:NT\$\s*)?-?(?:0|[1-9]\d{0,9}|[1-9]\d{0,2}(?:,\d{3}){1,3})\s*", accounts[0]["balance"]) is None
            or not isinstance(receipt, dict)
            or set(receipt) != {"identity", "start", "end", "status", "pages", "rows"}
            or receipt.get("identity") != identity
            or type(receipt.get("pages")) is not int
            or receipt["pages"] != 1
            or type(receipt.get("rows")) is not int
            or receipt["rows"] != len(rows)
            or receipt.get("status") not in {"complete", "explicit_empty"}
            or (receipt["status"] == "complete") != bool(rows)
            or not isinstance(result.get("selected_month"), str)
            or not isinstance(dom, dict)
            or not isinstance(transport, dict)
            or set(transport) != {
                "url", "method", "status", "content_type", "redirected", "main_frame",
                "request_count", "response_count",
            }
        ):
            raise RuntimeError(error)
        try:
            RakutenCrawler._validate_history_dom(dom, len(rows))
        except RuntimeError:
            raise RuntimeError(error) from None
        try:
            start = date.fromisoformat(receipt["start"])
            end = date.fromisoformat(receipt["end"])
            parsed_url = urlparse(transport["url"])
        except (TypeError, ValueError):
            raise RuntimeError(error) from None
        if (
            start > end
            or (start.year, start.month) != (end.year, end.month)
            or start.day != 1
            or result["selected_month"] != f"{start:%Y/%m} 活存明細"
            or parsed_url.scheme != "https"
            or parsed_url.netloc != "www.rakuten-bank.com.tw"
            or parsed_url.path != QUERY_PATH
            or parsed_url.params
            or parsed_url.query
            or parsed_url.fragment
            or transport.get("method") != "POST"
            or transport.get("status") != 200
            or transport.get("content_type") != "application/json"
            or transport.get("redirected") is not False
            or transport.get("main_frame") is not True
            or type(transport.get("request_count")) is not int
            or transport["request_count"] != 1
            or type(transport.get("response_count")) is not int
            or transport["response_count"] != 1
        ):
            raise RuntimeError(error)

        row_keys = {
            "sysDate", "sysTime", "txDesc", "nickNameOrAcct", "amt", "amtSign", "balance", "memo",
        }
        money = re.compile(r"(?:0|[1-9]\d{0,9}|[1-9]\d{0,2}(?:,\d{3}){1,3})")
        signed_money = re.compile(r"-?(?:0|[1-9]\d{0,9}|[1-9]\d{0,2}(?:,\d{3}){1,3})")
        for row in rows:
            if not isinstance(row, dict) or set(row) != row_keys:
                raise RuntimeError(error)
            try:
                transacted = datetime.strptime(
                    f"{row['sysDate']} {row['sysTime']}", "%Y/%m/%d %H:%M:%S",
                ).date()
            except (KeyError, TypeError, ValueError):
                raise RuntimeError(error) from None
            if (
                not start <= transacted <= end
                or not isinstance(row["txDesc"], str)
                or not row["txDesc"].strip()
                or len(row["txDesc"]) > 2_000
                or row["nickNameOrAcct"] is not None
                and (not isinstance(row["nickNameOrAcct"], str) or len(row["nickNameOrAcct"]) > 2_000)
                or not isinstance(row["amt"], str)
                or money.fullmatch(row["amt"]) is None
                or type(row["amtSign"]) is not bool
                or not isinstance(row["balance"], str)
                or signed_money.fullmatch(row["balance"]) is None
                or not isinstance(row["memo"], str)
                or len(row["memo"]) > 2_000
            ):
                raise RuntimeError(error)
        return receipt

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

        account_root = "simple-dropdown2"
        current_account = self._selected_label(page, account_root)
        accounts = self._validated_account_options(
            current_account,
            self._visible_labels(page, account_root),
        )

        expected: list[dict] = []
        windows: list[dict] = []
        results: list[dict] = []
        month_root = "simple-dropdown"
        for identity, account_label in accounts:
            if _account_number(self._selected_label(page, account_root)) != identity:
                self._select_label(page, collector, account_root, account_label)
            current_month = self._selected_label(page, month_root)
            raw_month_labels = [
                current_month,
                *self._visible_labels(page, month_root),
            ]
            plan = self._history_plan(identity, raw_month_labels, as_of)
            month_labels = _six_month_labels(raw_month_labels)
            if not plan:
                raise RuntimeError("rakuten-twd-history-range")
            expected.append({
                "identity": identity,
                "start": plan[0]["start"].isoformat(),
                "end": plan[-1]["end"].isoformat(),
            })

            if self._selected_label(page, month_root) == plan[0]["label"]:
                bootstrap = next(
                    (label for label in month_labels if label != plan[0]["label"]),
                    None,
                )
                if bootstrap is None:
                    raise RuntimeError("rakuten-twd-history-months")
                self._select_label(page, collector, month_root, bootstrap)

            for window in plan:
                transport = self._select_label(
                    page, collector, month_root, window["label"],
                )
                result = self._scrape_twd_page(page, identity)
                status = "complete" if result["txDetails"] else "explicit_empty"
                receipt = {
                    "identity": identity,
                    "start": window["start"].isoformat(),
                    "end": window["end"].isoformat(),
                    "status": status,
                    "pages": 1,
                    "rows": len(result["txDetails"]),
                }
                result.update({
                    "selected_month": window["label"],
                    "receipt": receipt,
                    "transport": transport,
                })
                self._validated_history_result(result)
                results.append(result)
                windows.append({
                    key: receipt[key]
                    for key in ("identity", "start", "end", "status", "pages")
                })

        final_accounts = self._validated_account_options(
            self._selected_label(page, account_root),
            self._visible_labels(page, account_root),
        )
        if final_accounts != accounts:
            raise RuntimeError("rakuten-twd-history-inventory")

        coverage = {
            "version": 1,
            "mode": mode,
            "as_of": as_of.isoformat(),
            "domains": [{
                "domain": "twd_transactions",
                "expected": expected,
                "windows": windows,
            }],
        }
        validate_history_coverage(
            coverage,
            expected_mode=mode,
            expected_domains=self.HISTORY_COVERAGE_DOMAINS,
        )
        return {
            "account_options": [{"identity": identity} for identity, _label in accounts],
            "twd_txn_results": results,
            "history_coverage": coverage,
        }

    def _execute_browser_flow(
        self,
        login_url: str,
        *,
        headless: bool,
        page_action,
        fetch_kwargs: dict,
    ) -> None:
        # Rakuten's Angular login transition completes only after the shared login
        # stack unwinds; keep the same owned page alive for the bounded recheck.
        with StealthySession(
            headless=headless,
            user_data_dir=str(self.session_dir),
            hide_canvas=True,
            block_webrtc=True,
            dns_over_https=True,
            **fetch_kwargs,
        ) as engine:
            if engine.context is None:
                raise RuntimeError("樂天瀏覽器 context 未建立")
            page = engine.context.new_page()
            page.set_default_navigation_timeout(180000)
            page.set_default_timeout(180000)
            if headers := fetch_kwargs.get("extra_headers"):
                page.set_extra_http_headers(headers)
            try:
                page.goto(login_url, referer="https://www.google.com/")
                page.wait_for_load_state("load")
                page.wait_for_load_state("domcontentloaded")
                page_action(page)
                page.wait_for_timeout(2000)
            finally:
                page.close()

    def _logged_in(self, page) -> bool:
        try:
            current = urlparse(page.url or "")
            path = (current.path or "").lower()
            if (
                not self._exact_https_origin_allowed(page.url, self.CREDENTIAL_HOSTS)
                or not path.startswith("/ebank/")
                or LOGIN_PATH_HINT in path
            ):
                return False
            return bool(page.evaluate("""() => {
                const visible = e => !!e && !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
                const noLogin = !visible(document.querySelector('#custNo'))
                    && !visible(document.querySelector('#userNo'))
                    && !visible(document.querySelector('#pcode'));
                const body = document.body?.innerText || '';
                const words = ['登出', '首頁', '臺幣存款', '轉帳', '貸款', '設定'];
                // 不設 body 長度門檻：樂天臺幣存款頁在該月 0 筆交易時 innerText 僅 272 字，
                // 舊的 >=300 門檻會把已登入頁誤判成未登入（2026-07-28 real-account 實證）。
                // 「無登入欄 + 命中 >=2 個登入後導覽字」已足以區分登入頁與內頁。
                return noLogin && words.filter(word => body.includes(word)).length >= 2;
            }"""))
        except Exception:
            return False

    def login(self, page) -> bool:
        return self._shared_login(page)

    def prepare_login_page(self, page) -> None:
        # browserStartup may take 3s for a token plus 15s for its handshake.
        page.wait_for_timeout(20000)

    def is_authenticated(self, page) -> bool:
        return self._logged_in(page)

    def _recover_late_authentication(self, page, error: Exception) -> bool:
        if (
            not isinstance(error, LoginCheckpointBlocked)
            or error.budget != LoginBudget(credential_submissions=1)
            or error.outcome.kind is not CheckpointKind.UNKNOWN_BLOCKER
            or error.outcome.rule_name is not None
            or getattr(self, "_shared_dialog_blocked", False)
        ):
            return False
        page.wait_for_timeout(5000)
        if (
            getattr(self, "_shared_dialog_blocked", False)
            or not self._credential_origin_allowed(page)
            or _any_visible(page, "input[name='otpCode']")
            or _any_visible(page, "#ib_init_connect_error_popup")
        ):
            return False
        if not _any_visible(page, ".modal.show"):
            return self._logged_in(page)

        rules = self.login_checkpoint_rules()
        active_rules = tuple(
            rule for rule in rules if CheckpointPhase.POST_SUBMIT_SETTLE in rule.phases
        )
        outcome = validate_login_checkpoint_outcome(
            evaluate_login_checkpoint(
                page,
                bank=self.name,
                phase=CheckpointPhase.POST_SUBMIT_SETTLE,
                rules=rules,
                is_authenticated=self.is_authenticated,
                is_scope_owned=lambda frame: self._frame_origin_allowed(page, frame),
            ),
            active_rules,
        )
        if outcome.kind not in {
            CheckpointKind.DUPLICATE_SESSION,
            CheckpointKind.DISMISSIBLE_NOTICE,
        }:
            return False
        try:
            reduce_login_checkpoint(
                CheckpointPhase.POST_SUBMIT_SETTLE,
                error.budget,
                outcome,
            )
        except LoginCheckpointTerminal:
            return False
        return (
            not getattr(self, "_shared_dialog_blocked", False)
            and self._credential_origin_allowed(page)
            and not _any_visible(page, "input[name='otpCode']")
            and not _any_visible(page, ".modal.show")
            and not _any_visible(page, "#ib_init_connect_error_popup")
            and self._logged_in(page)
        )

    def login_checkpoint_rules(self) -> tuple[LoginCheckpointRule, ...]:
        all_phases = tuple(CheckpointPhase)
        normalized_body = r"\s+".join(re.escape(part) for part in self.DUP_LOGIN_BODY.split())
        duplicate_body = re.compile(
            rf"^\s*{normalized_body}\s*(?:否，不要登入\s*)?(?:是，我要登入\s*)?$"
        )

        def prefix(text: str) -> re.Pattern[str]:
            return re.compile(r"^\s*" + r"\s+".join(re.escape(part) for part in text.split()))

        return (
            LoginCheckpointRule(
                name="rakuten-startup-connect-error",
                bank="rakuten",
                phases=(CheckpointPhase.PRE_SUBMIT,),
                kind=CheckpointKind.STARTUP_RECOVERY,
                container_selector="#ib_init_connect_error_popup",
            ),
            LoginCheckpointRule(
                name="rakuten-duplicate-session",
                bank="rakuten",
                phases=all_phases,
                kind=CheckpointKind.DUPLICATE_SESSION,
                container_selector=".modal.show",
                action_texts=("是，我要登入",),
                required_body_pattern=duplicate_body,
            ),
            LoginCheckpointRule(
                name="rakuten-otp-required",
                bank="rakuten",
                phases=(CheckpointPhase.POST_SUBMIT, CheckpointPhase.POST_SUBMIT_SETTLE),
                kind=CheckpointKind.OTP_REQUIRED,
                container_selector="input[name='otpCode']",
            ),
            LoginCheckpointRule(
                name="rakuten-referral-promo",
                bank="rakuten",
                phases=all_phases,
                kind=CheckpointKind.DISMISSIBLE_NOTICE,
                container_selector=".modal.show",
                action_texts=("稍後再看",),
                required_body_pattern=prefix(self.REFERRAL_PROMO_PREFIX),
            ),
            LoginCheckpointRule(
                name="rakuten-ricb-promo",
                bank="rakuten",
                phases=all_phases,
                kind=CheckpointKind.DISMISSIBLE_NOTICE,
                container_selector=".modal.show",
                action_texts=("略過",),
                required_body_pattern=prefix(self.INSURANCE_PROMO_PREFIX),
            ),
            LoginCheckpointRule(
                name="rakuten-unknown-modal",
                bank="rakuten",
                phases=all_phases,
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector=".modal.show",
            ),
        )

    def submit_credentials_once(self, page) -> None:

        for selector in ("#custNo", "#userNo", "#pcode"):
            page.wait_for_selector(selector, timeout=15000)

        captcha = ""
        if page.locator(CAPTCHA_IMG).is_visible():
            wait_captcha_stable(page, CAPTCHA_IMG, tmp_path=self.captcha_tmp)
            captcha = solve_captcha(
                page,
                CAPTCHA_IMG,
                expected_len=4,
                min_confidence=0.95,
                tmp_path=self.captcha_tmp,
            ) or ""
            if not captcha:
                with contextlib.suppress(Exception):
                    old_src = page.locator(CAPTCHA_IMG).get_attribute("src") or ""
                    captcha_group = page.locator("captcha-image").locator(
                        "xpath=ancestor::div[contains(@class,'form-group')][1]",
                    )
                    captcha_group.locator("a:has(.icon-restart)").click()
                    page.wait_for_function(
                        "old => (document.querySelector('captcha-image img')?.getAttribute('src') || '') !== old",
                        arg=old_src,
                        timeout=5000,
                    )
                    wait_captcha_stable(page, CAPTCHA_IMG, tmp_path=self.captcha_tmp)
                    captcha = solve_captcha(
                        page,
                        CAPTCHA_IMG,
                        expected_len=4,
                        min_confidence=0.95,
                        tmp_path=self.captcha_tmp,
                    ) or ""
            if not captcha:
                raise RakutenLoginError("圖形驗證碼 OCR 失敗；未送出登入")

        fields = (
            ("#custNo", self.creds.national_id),
            ("#userNo", self.creds.user_code),
            ("#pcode", self.creds.password),
            ("#captcha", captcha),
        )
        for selector, value in fields:
            if not value and selector == "#captcha":
                continue
            locator = page.locator(selector)
            locator.click()
            locator.press("ControlOrMeta+A")
            locator.press("Backspace")
            locator.press_sequentially(value, delay=60)

        lengths = page.evaluate("""() => Object.fromEntries(
            ['custNo', 'userNo', 'pcode', 'captcha'].map(id => [id, document.getElementById(id)?.value.length || 0])
        )""")
        expected = {
            "custNo": len(self.creds.national_id),
            "userNo": len(self.creds.user_code),
            "pcode": len(self.creds.password),
            "captcha": len(captcha),
        }
        if any(lengths.get(key) != value for key, value in expected.items()):
            raise RakutenLoginError("登入欄位輸入長度不符；未送出登入")

        if not _click_visible_login(page):
            raise RakutenLoginError("找不到唯一且可操作的登入按鈕；未送出登入")

        for _ in range(20):
            page.wait_for_timeout(1000)
            try:
                if self._logged_in(page) or any(
                    _any_visible(page, selector)
                    for selector in (
                        "input[name='otpCode']",
                        ".modal.show:not(.modal_loading)",
                        "#ib_init_connect_error_popup",
                    )
                ):
                    return
            except Exception:
                return

    def logout(self, page) -> bool:
        pattern = re.compile(r"^\s*(?:安全)?登出\s*$")
        for frame in page.frames:
            candidates = frame.locator("a, button").filter(has_text=pattern)
            for index in range(candidates.count()):
                candidate = candidates.nth(index)
                if not candidate.is_visible():
                    continue
                candidate.click()
                modal_selector = self.VISIBLE_CONFIRM_SELECTOR
                page.wait_for_selector(modal_selector, state="visible", timeout=10000)
                modals = page.locator(modal_selector)
                for modal_index in range(modals.count()):
                    modal = modals.nth(modal_index)
                    body = " ".join(modal.locator(".modal-body").inner_text().split())
                    if body != self.LOGOUT_BODY:
                        continue
                    submit = modal.locator(
                        "a:visible, button:visible, [role=button]:visible",
                    ).filter(has_text=re.compile(r"^\s*確認\s*$"))
                    if submit.count() != 1 or not submit.first.is_visible():
                        raise RakutenLoginError("樂天登出提示缺少唯一確認按鈕")
                    submit.first.click()
                    page.wait_for_selector("#custNo", state="visible", timeout=30000)
                    return True
                raise RakutenLoginError("樂天登出後未出現預期確認提示")
        return False

    @staticmethod
    def _scrape_twd_page(page, account_no: str | None = None) -> dict:
        snapshot = page.evaluate(r"""() => {
            const visible = e => {
                if (!e || !(e.offsetWidth || e.offsetHeight || e.getClientRects().length)) return false;
                for (let n = e; n; n = n.parentElement) {
                    const s = getComputedStyle(n);
                    if (n.hidden || s.display === 'none' || s.visibility === 'hidden'
                        || s.visibility === 'collapse' || Number(s.opacity) === 0
                        || (n.getAttribute('aria-hidden') || '').toLowerCase() === 'true') return false;
                }
                return true;
            };
            const text = e => (e?.innerText || '').replace(/\s+/g, ' ').trim();
            const selected = document.querySelector('simple-dropdown2 a.txt_dropdown');
            const accountLabel = selected?.innerText || document.querySelector('simple-dropdown2')?.innerText || '';
            const balance = document.querySelector('.card-title-money')?.innerText || '';
            const tables = [...document.querySelectorAll('table.tb_mul')];
            const visibleTables = tables.filter(visible);
            const table = visibleTables.length === 1 ? visibleTables[0] : null;
            const headerElements = table
                ? [...table.querySelectorAll(':scope > thead > tr > th')]
                : [];
            const headers = headerElements.every(visible) ? headerElements.map(text) : [];
            const allRowElements = table
                ? [...table.querySelectorAll(':scope > tbody > tr')]
                : [];
            const rowElements = allRowElements.filter(visible);
            const encoder = new TextEncoder();
            let rawBytes = 0;
            let invalidCells = 0;
            const rows = [];
            for (const row of rowElements) {
                const cells = [...row.querySelectorAll(':scope > td')];
                const values = cells.map(cell => cell.innerText || '');
                const sizes = values.map(value => encoder.encode(value).length);
                rawBytes += sizes.reduce((sum, size) => sum + size, 0);
                if (cells.length !== 6 || !cells.every(visible)
                    || sizes.some(size => size > 4000) || rawBytes > 5000000) {
                    invalidCells += 1;
                    continue;
                }
                rows.push(values);
            }
            const noDataNodes = [...document.querySelectorAll('.page-result.pic-card .pic-card-title')]
                .filter(e => visible(e) && text(e) === '此月份沒有任何交易明細。');
            const anchor = table || noDataNodes[0];
            let root = null;
            for (let node = anchor?.parentElement; node && node !== document.body; node = node.parentElement) {
                if (node.querySelector('simple-dropdown') && node.querySelector('simple-dropdown2')) {
                    root = node;
                    break;
                }
            }
            root ||= anchor?.closest('main') || document;
            const pager = [...root.querySelectorAll(
                '.pagination, .pager, [class*="pagination"], [class*="pager"], '
                + '[data-page], [aria-current="page"], a, button'
            )].filter(e => {
                const label = text(e);
                const target = ["href", "onclick", "rel", "title", "aria-label"]
                    .map(name => e.getAttribute(name) || "").join(" ");
                return /pagination|pager/i.test(e.className || '')
                    || /^(下一頁|上一頁|第一頁|最後一頁|首頁|末頁|next|prev|previous|first|last)$/i.test(label)
                    || /^\d{1,4}$/.test(label)
                    || /^[‹›«»←→]$/.test(label)
                    || /^(next|previous|first|last|page)/i.test(e.getAttribute('aria-label') || '')
                    || /(?:page|next|prev)/i.test(target);
            }).length;
            const busy = [...document.querySelectorAll(
                '[aria-busy="true"], [role="progressbar"], .modal_loading, .loading, .spinner'
            )].filter(visible).length;
            const dialogs = [...document.querySelectorAll(
                '.modal.show:not(.modal_loading), [role="dialog"]'
            )].filter(visible).length;
            const alerts = [...root.querySelectorAll(
                '[role="alert"], .alert, .error, .alert-danger, .alert-error, .error-message'
            )].filter(visible).length;
            return {
                accountLabel,
                balance,
                rows,
                dom: {
                    table_count: tables.length,
                    visible_tables: visibleTables.length,
                    headers,
                    raw_rows: allRowElements.length,
                    no_data_count: noDataNodes.length,
                    invalid_cells: invalidCells,
                    pager,
                    busy,
                    dialogs,
                    alerts,
                },
            };
        }""")
        account_label = str(snapshot.pop("accountLabel", "") or "")
        raw_balance = snapshot.pop("balance", "")
        raw_rows = snapshot.pop("rows", [])
        dom = snapshot.pop("dom", None)
        extracted_number = _account_number(account_label)
        if account_no is not None and extracted_number != account_no:
            raise RuntimeError("rakuten-twd-history-dom")
        number = account_no or extracted_number
        if not isinstance(raw_rows, list):
            raise RuntimeError("rakuten-twd-history-dom")
        parsed_rows = [_row_from_dom(cells) for cells in raw_rows]
        if any(parsed is None for parsed in parsed_rows) or snapshot:
            raise RuntimeError("rakuten-twd-history-dom")
        RakutenCrawler._validate_history_dom(dom, len(parsed_rows))
        snapshot["txDetails"] = [parsed for parsed in parsed_rows if parsed is not None]
        snapshot["dom"] = dom
        snapshot["account_no"] = number
        snapshot["accounts"] = [{
            "acctNo": number,
            "balance": raw_balance,
        }] if number else []
        return snapshot

    @staticmethod
    def _selected_label(page, root: str) -> str:
        if root == "simple-dropdown2":
            return page.locator(f"{root} a.txt_dropdown").inner_text().strip()
        return page.locator(f"{root} button.input-select").inner_text().strip()

    @staticmethod
    def _open_dropdown(page, root: str) -> None:
        trigger = (
            page.locator(f"{root} a.txt_dropdown")
            if root == "simple-dropdown2"
            else page.locator(f"{root} button.input-select")
        )
        trigger.click()

    @staticmethod
    def _visible_labels(page, root: str) -> list[str]:
        RakutenCrawler._open_dropdown(page, root)
        previous: list[str] | None = None
        stable_since: float | None = None
        deadline = time.monotonic() + 3.0
        try:
            options = page.locator(f"{root} .dropdown-menu a.dropdown-item")
            while time.monotonic() < deadline:
                sample = options.evaluate_all(r"""elements => elements.map(element => {
                    let visible = !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length);
                    for (let node = element; visible && node; node = node.parentElement) {
                        const style = getComputedStyle(node);
                        if (node.hidden || style.display === 'none' || style.visibility === 'hidden'
                            || style.visibility === 'collapse' || Number(style.opacity) <= 0.01) {
                            visible = false;
                        }
                    }
                    return {label: (element.innerText || '').trim(), visible};
                })""")
                labels = [item.get("label") for item in sample]
                valid = (
                    bool(labels)
                    and all(item.get("visible") is True for item in sample)
                    and all(isinstance(label, str) and label for label in labels)
                )
                now = time.monotonic()
                if valid and labels == previous and stable_since is not None:
                    if now - stable_since >= 1.0:
                        return labels
                else:
                    stable_since = now if valid else None
                previous = labels if valid else None
                remaining_ms = int((deadline - now) * 1000)
                if remaining_ms <= 0:
                    break
                page.wait_for_timeout(min(100, remaining_ms))
            raise RuntimeError("rakuten-twd-history-inventory")
        finally:
            page.keyboard.press("Escape")

    @staticmethod
    def _twd_view_state(page) -> dict:
        return page.evaluate(r"""() => {
            const visible = e => !!e && !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
            const rows = [...document.querySelectorAll('table.tb_mul tbody tr')]
                .filter(visible)
                .map(row => (row.innerText || '').trim())
                .filter(Boolean)
                .join('\n');
            const account = document.querySelector('simple-dropdown2 a.txt_dropdown')?.innerText || '';
            const month = document.querySelector('simple-dropdown button.input-select')?.innerText || '';
            const balance = document.querySelector('.card-title-money')?.innerText || '';
            const noData = [...document.querySelectorAll('.page-result.pic-card .pic-card-title')]
                .some(e => visible(e) && (e.innerText || '').trim() === '此月份沒有任何交易明細。');
            return {rows, account, month, balance, noData};
        }""")

    def _wait_for_twd_view(
        self,
        page,
        *,
        selected_root: str | None = None,
        selected_label: str | None = None,
        before_rows: str | None = None,
        timeout_seconds: int = 20,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        last_ready: tuple[str, str, str, str, str] | None = None
        stable_count = 0
        while time.monotonic() < deadline:
            if selected_root and selected_label:
                selected = self._selected_label(page, selected_root)
                if not _selection_matches(selected_root, selected, selected_label):
                    page.wait_for_timeout(250)
                    continue
            state = self._twd_view_state(page)
            signature = (
                str(state.get("rows") or ""),
                str(state.get("account") or ""),
                str(state.get("month") or ""),
                str(state.get("balance") or ""),
                str(bool(state.get("noData"))),
            )
            if _view_ready(before_rows, state):
                stable_count = stable_count + 1 if signature == last_ready else 1
                last_ready = signature
                if stable_count >= 3:
                    return
            else:
                last_ready = None
                stable_count = 0
            page.wait_for_timeout(300)
        kind = "initial" if not selected_root else (
            "account" if selected_root == "simple-dropdown2" else "month"
        )
        raise RuntimeError(f"樂天臺幣頁資料未完成（kind={kind}）")

    def _select_label(
        self,
        page,
        collector: ResponseCollector,
        root: str,
        label: str,
    ) -> dict:
        self._open_dropdown(page, root)
        page.wait_for_timeout(100)
        options = page.locator(f"{root} .dropdown-menu a.dropdown-item:visible")
        labels = [options.nth(i).inner_text().strip() for i in range(options.count())]
        target_index = _unique_option_index(labels, label)
        if target_index is None:
            kind = "account" if root == "simple-dropdown2" else "month"
            raise RuntimeError(f"找不到唯一可見的樂天下拉選項（kind={kind}）")
        target = options.nth(target_index)
        before_rows = str(self._twd_view_state(page).get("rows") or "")
        request_boundary = collector.request_sequence
        issued_before = collector.issued_count("011")
        page.wait_for_selector(LOADER_SELECTOR, state="hidden", timeout=20000)
        with page.expect_request(_is_twd_query_request, timeout=20000) as request_info:
            target.click()
            page.wait_for_selector(LOADER_SELECTOR, state="visible", timeout=3000)
        try:
            response = request_info.value.response()
            if response is None or not 200 <= response.status < 300:
                raise RuntimeError("樂天臺幣查詢回應狀態失敗")
            response.finished()
        except Exception as e:
            raise RuntimeError("樂天臺幣查詢 request 未成功") from e
        page.wait_for_selector(LOADER_SELECTOR, state="hidden", timeout=20000)
        self._wait_for_twd_view(
            page,
            selected_root=root,
            selected_label=label,
            before_rows=before_rows,
        )
        hits = []
        for _ in range(4):
            page.wait_for_timeout(500)
            hits = [
                hit for hit in collector.hits
                if hit.request_sequence > request_boundary
                and urlparse(hit.raw_url or hit.url).path == QUERY_PATH
            ]
            if (
                collector.issued_count("011") - issued_before > 1
                or len(hits) > 1
            ):
                raise RuntimeError("rakuten-twd-history-response-cardinality")
        if (
            collector.issued_count("011") - issued_before != 1
            or len(hits) != 1
        ):
            raise RuntimeError("rakuten-twd-history-response-cardinality")
        hit = hits[0]
        request = request_info.value
        request_frame = _OriginGuardProxy._unwrap(getattr(request, "frame", None))
        main_frame = _OriginGuardProxy._unwrap(getattr(page, "main_frame", None))
        if request_frame is not main_frame:
            raise RuntimeError("rakuten-twd-history-frame")
        return {
            "url": hit.raw_url or hit.url,
            "method": hit.method,
            "status": hit.status,
            "content_type": (hit.content_type or "").split(";", 1)[0].strip().lower(),
            "redirected": hit.redirected,
            "main_frame": hit.main_frame_request,
            "request_count": 1,
            "response_count": 1,
        }

    def _goto_twd(self, page) -> None:
        """走真實 UI 導覽進臺幣存款頁。

        樂天是 Angular SPA：直接 `page.goto(TWD_URL)` 會做整頁 reload，session
        不會被 SPA 還原，結果被踢回登入頁（2026-07-28 real-account probe 實證，
        final_url 落在 /cgn/cgnot0001/010）。只能點側邊導覽。
        """
        page.wait_for_selector(LOADER_SELECTOR, state="hidden", timeout=60000)
        deposit = page.get_by_role("link", name="存款", exact=True).first
        try:
            deposit.click(timeout=5000)
        except Exception:
            rules = self.login_checkpoint_rules()
            outcome = evaluate_login_checkpoint(
                page,
                bank=self.name,
                phase=CheckpointPhase.POST_SUBMIT_SETTLE,
                rules=rules,
                is_authenticated=self.is_authenticated,
            )
            active_rules = tuple(
                rule
                for rule in rules
                if CheckpointPhase.POST_SUBMIT_SETTLE in rule.phases
            )
            outcome = validate_login_checkpoint_outcome(outcome, active_rules)
            try:
                reduce_login_checkpoint(
                    CheckpointPhase.POST_SUBMIT_SETTLE,
                    LoginBudget(credential_submissions=1),
                    outcome,
                )
            except LoginCheckpointTerminal as terminal:
                raise terminal from None
            if outcome.kind not in {
                CheckpointKind.DUPLICATE_SESSION,
                CheckpointKind.DISMISSIBLE_NOTICE,
            }:
                raise
            deposit.click(timeout=5000)
        page.wait_for_selector("a.sub-nav-link:has-text('臺幣存款')", state="visible", timeout=15000)
        page.locator("a.sub-nav-link", has_text="臺幣存款").first.click()
        page.wait_for_url(lambda url: TWD_PATH_HINT in url, timeout=30000)

    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        self._goto_twd(page)
        page.wait_for_selector(
            "simple-dropdown2 a.txt_dropdown",
            state="visible",
            timeout=60000,
        )
        page.wait_for_selector(
            "simple-dropdown button.input-select",
            state="visible",
            timeout=60000,
        )
        page.wait_for_selector(LOADER_SELECTOR, state="hidden", timeout=60000)
        if not self._logged_in(page):
            raise RakutenLoginError("進入臺幣存款頁後 session 無效")
        self._wait_for_twd_view(page, timeout_seconds=30)

        history = self._collect_attested_twd_history(page, collector)

        endpoints = sorted({
            _endpoint_key(hit.url)
            for hit in collector.hits
            if "/channel-" in hit.url
        })
        return BankCollectResult(
            bank="rakuten",
            final_url=page.url,
            account_options=history["account_options"],
            twd_txn_results=history["twd_txn_results"],
            history_coverage=history["history_coverage"],
            _all_endpoints=endpoints,
            card_bill_facts_ok=False,
            card_bill_facts=[],
        )


if __name__ == "__main__":
    crawler = RakutenCrawler()
    result = crawler.run(BASE, headless=False)
    print({"error": result.get("error"), "final_url": result.get("final_url")})
