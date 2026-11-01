#!/usr/bin/env python3
"""樂天國際銀行個人網銀 crawler。

登入頁是 Angular SPA；帳密由銀行前端自行做 E2E 加密後送出。本 crawler 只操作
真實表單，登入後從「臺幣存款」頁的已解密 DOM 讀取帳戶、餘額與六個月交易。
"""
from __future__ import annotations

from calendar import monthrange
import contextlib
from datetime import date, datetime, timedelta
import os
import json
import re
import time
from typing import ClassVar
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from scrapling.fetchers import StealthySession

from backend.core.base import (
    captcha_unreadable,
    BankCollectResult,
    BankCrawler,
    ResponseCollector,
    _OriginGuardProxy,
    validate_history_coverage,
)

from backend.core.captcha import solve_captcha, wait_captcha_stable
from backend.core.error_codes import error_step, with_error_code, exception_error_id, format_error_id
from backend.core.creds import RakutenCreds
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointPhase,
    LoginBudget,
    LoginCheckpointBlocked,
    LoginCheckpointRule,
)

BASE = "https://www.rakuten-bank.com.tw/ebank/cgn/cgnot0001/010"
TWD_URL = "https://www.rakuten-bank.com.tw/ebank/ctw/ctwqu0001/010"
TWD_PATH_HINT = "/ctw/ctwqu0001/"
LOGIN_PATH_HINT = "/cgn/cgnot0001/010"
CAPTCHA_IMG = "captcha-image img"
LOADER_SELECTOR = "modal-loader .modal_loading"
SEMANTIC_MODAL_SELECTOR = ".modal.show:not(.modal_loading)"
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
        raise with_error_code(RuntimeError('樂天月份選單不是預期六個月份'), 'RAKUTEN-05-068-001')
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
    with error_step({"input[name='otpCode']": 'RAKUTEN-03-015-002', '.modal.show:not(.modal_loading)': 'RAKUTEN-03-015-003', '#ib_init_connect_error_popup': 'RAKUTEN-03-015-004', 'modal-loader .modal_loading': 'RAKUTEN-03-015-005'}.get(selector, 'RAKUTEN-03-015-001')):
        locators = page.locator(selector)
    with error_step('RAKUTEN-03-016-001'):
        return any(error_step({"input[name='otpCode']": 'RAKUTEN-03-700-002', '.modal.show:not(.modal_loading)': 'RAKUTEN-03-700-003', '#ib_init_connect_error_popup': 'RAKUTEN-03-700-004', 'modal-loader .modal_loading': 'RAKUTEN-03-700-005'}.get(selector, 'RAKUTEN-03-700-001'))(lambda: error_step({"input[name='otpCode']": 'RAKUTEN-03-701-002', '.modal.show:not(.modal_loading)': 'RAKUTEN-03-701-003', '#ib_init_connect_error_popup': 'RAKUTEN-03-701-004', 'modal-loader .modal_loading': 'RAKUTEN-03-701-005'}.get(selector, 'RAKUTEN-03-701-001'))(lambda: locators.nth(index))().is_visible())() for index in range(error_step({"input[name='otpCode']": 'RAKUTEN-03-702-002', '.modal.show:not(.modal_loading)': 'RAKUTEN-03-702-003', '#ib_init_connect_error_popup': 'RAKUTEN-03-702-004', 'modal-loader .modal_loading': 'RAKUTEN-03-702-005'}.get(selector, 'RAKUTEN-03-702-001'))(lambda: locators.count())()))


def _semantic_modal_visible(page) -> bool:
    with error_step('RAKUTEN-03-004-001'):
        modals = page.locator(SEMANTIC_MODAL_SELECTOR)
    with error_step('RAKUTEN-03-005-001'):
        count = modals.count()
    if count > 500:
        raise with_error_code(RuntimeError('rakuten-login-modal-budget'), 'RAKUTEN-03-006-001')
    for index in range(count):
        with error_step('RAKUTEN-03-007-001'):
            modal = modals.nth(index)
        with error_step('RAKUTEN-03-008-001'):
            if not modal.is_visible():
                continue
        with error_step('RAKUTEN-03-009-001'):
            if (modal.inner_text(timeout=5000) or "").strip():
                return True
        with error_step('RAKUTEN-03-010-001'):
            controls = modal.locator("button,a,input,form,[role=button]")
        with error_step('RAKUTEN-03-011-001'):
            control_count = controls.count()
        if control_count > 500:
            raise with_error_code(RuntimeError('rakuten-login-modal-budget'), 'RAKUTEN-03-012-001')
        with error_step('RAKUTEN-03-013-001'):
            if any(error_step('RAKUTEN-03-703-001')(lambda: error_step('RAKUTEN-03-704-001')(lambda: controls.nth(item))().is_visible())() for item in range(control_count)):
                return True
    return False


def _modal_shape(page) -> list:
    """Title and visible button labels of open modals, digits masked; never body text."""
    try:
        with error_step('RAKUTEN-03-017-001'):
            shapes = page.evaluate("""() => [...document.querySelectorAll('.modal.show:not(.modal_loading)')]
          .filter(m => m.offsetWidth || m.offsetHeight).slice(0, 5).map(m => ({
            id: (m.id || '').slice(0, 40),
            title: (m.querySelector('.modal-title,h1,h2,h3,h4,h5')?.innerText || '').trim().slice(0, 60),
            buttons: [...m.querySelectorAll('button,a,[role=button]')]
              .filter(b => b.offsetWidth || b.offsetHeight).slice(0, 8)
              .map(b => (b.innerText || b.getAttribute('aria-label') || '').trim().slice(0, 20)),
          }))""")
        return json.loads(re.sub(r"\d", "#", json.dumps(shapes, ensure_ascii=False)))
    except Exception as e:
        return [type(e).__name__]


def _click_visible_login(page, *, _failure=None) -> bool:
    with error_step('RAKUTEN-02-036-001'):
        candidates = error_step('RAKUTEN-02-706-001')(lambda: error_step('RAKUTEN-02-705-001')(lambda: page.locator("a.btn.btn-primary:visible"))().filter(
            has_text=re.compile(r"^\s*登入\s*$"),
        ))()
    with error_step('RAKUTEN-02-037-001'):
        candidate_count = candidates.count()
    if candidate_count == 0:
        with_error_code(_failure, 'RAKUTEN-02-041-001')
        return False
    if candidate_count != 1:
        with_error_code(_failure, 'RAKUTEN-02-042-001')
        return False
    button = candidates.first
    with error_step('RAKUTEN-02-038-001'):
        classes = button.get_attribute("class") or ""
    with error_step('RAKUTEN-02-039-001'):
        if not button.is_visible():
            with_error_code(_failure, 'RAKUTEN-02-043-001')
            return False
    with error_step('RAKUTEN-02-046-001'):
        if not button.is_enabled():
            with_error_code(_failure, 'RAKUTEN-02-044-001')
            return False
    if "disabled" in classes.split():
        with_error_code(_failure, 'RAKUTEN-02-045-001')
        return False
    with error_step('RAKUTEN-02-040-001'):
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
        if selected is None:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-04-001-001')
        elif not option_labels:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-04-001-002')
        options: list[tuple[str, str]] = []
        seen: set[str] = set()
        for label in option_labels:
            identity = _account_number(label)
            if identity is None:
                raise with_error_code(RuntimeError(error), 'RAKUTEN-04-002-001')
            elif identity in seen:
                raise with_error_code(RuntimeError(error), 'RAKUTEN-04-002-002')
            options.append((identity, label))
            seen.add(identity)
        if selected not in seen:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-04-003-001')
        return options

    def _history_plan(
        self,
        identity: str,
        labels: list[str],
        as_of: date,
    ) -> list[dict]:
        for label in labels:
            if not isinstance(label, str):
                raise with_error_code(RuntimeError('rakuten-twd-history-months'), 'RAKUTEN-05-011-002')
            if re.fullmatch('\\s*\\d{4}/(?:0[1-9]|1[0-2])\\s+活存明細\\s*', label) is None:
                raise with_error_code(RuntimeError('rakuten-twd-history-months'), 'RAKUTEN-05-011-003')
        try:
            months = _six_month_labels(labels)
        except RuntimeError as source_error:
            raise with_error_code(RuntimeError('rakuten-twd-history-months'), exception_error_id(source_error, bank="rakuten") or 'RAKUTEN-05-012-001') from None
        parsed = [date(int(label[:4]), int(label[5:7]), 1) for label in months]
        expected = []
        cursor = date(as_of.year, as_of.month, 1)
        for _ in range(6):
            expected.append(cursor)
            cursor = (cursor - timedelta(days=1)).replace(day=1)
        if parsed != expected:
            raise with_error_code(RuntimeError('rakuten-twd-history-months'), 'RAKUTEN-05-013-001')

        mode = os.environ.get("BANK_CRAWLER_HISTORY_MODE", "full")
        if mode not in {'full', 'incremental'}:
            raise with_error_code(ValueError(f'invalid BANK_CRAWLER_HISTORY_MODE: {mode!r}'), 'RAKUTEN-05-014-001')
        oldest = parsed[-1]
        start = oldest
        persisted = self.transaction_start_for(identity, domain="twd_transactions")
        if mode == 'incremental' and persisted is not None and (persisted > as_of):
            raise with_error_code(RuntimeError('rakuten-twd-history-cursor'), 'RAKUTEN-05-015-001')
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
        if not isinstance(dom, dict):
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-053-001')
        if set(dom) != keys:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-053-002')
        if type(row_count) is not int:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-053-003')
        if row_count < 0:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-053-004')
        for key in keys - {'headers'}:
            if type(dom.get(key)) is not int:
                raise with_error_code(RuntimeError(error), {'alerts': 'RAKUTEN-05-053-010', 'busy': 'RAKUTEN-05-053-011', 'dialogs': 'RAKUTEN-05-053-012', 'invalid_cells': 'RAKUTEN-05-053-013', 'no_data_count': 'RAKUTEN-05-053-014', 'pager': 'RAKUTEN-05-053-015', 'raw_rows': 'RAKUTEN-05-053-016', 'table_count': 'RAKUTEN-05-053-017', 'visible_tables': 'RAKUTEN-05-053-018'}[key])
            if dom[key] < 0:
                raise with_error_code(RuntimeError(error), {'alerts': 'RAKUTEN-05-053-019', 'busy': 'RAKUTEN-05-053-020', 'dialogs': 'RAKUTEN-05-053-021', 'invalid_cells': 'RAKUTEN-05-053-022', 'no_data_count': 'RAKUTEN-05-053-023', 'pager': 'RAKUTEN-05-053-024', 'raw_rows': 'RAKUTEN-05-053-025', 'table_count': 'RAKUTEN-05-053-026', 'visible_tables': 'RAKUTEN-05-053-027'}[key])
        if not isinstance(dom.get('headers'), list):
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-053-006')
        for header in dom['headers']:
            if type(header) is not str:
                raise with_error_code(RuntimeError(error), 'RAKUTEN-05-053-007')
        if dom['raw_rows'] != row_count:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-053-008')
        for key in ('invalid_cells', 'pager', 'busy', 'dialogs', 'alerts'):
            if dom[key] != 0:
                raise with_error_code(RuntimeError(error), {'invalid_cells': 'RAKUTEN-05-053-028', 'pager': 'RAKUTEN-05-053-029', 'busy': 'RAKUTEN-05-053-030', 'dialogs': 'RAKUTEN-05-053-031', 'alerts': 'RAKUTEN-05-053-032'}[key])
        expected_headers = [
            "交易時間", "交易說明 對方帳號或暱稱", "轉入", "轉出",
            "帳戶餘額", "備註", "",
        ]
        if row_count:
            if dom['table_count'] != 1:
                raise with_error_code(RuntimeError(error), 'RAKUTEN-05-054-001')
            elif dom['visible_tables'] != 1:
                raise with_error_code(RuntimeError(error), 'RAKUTEN-05-054-002')
            elif dom['no_data_count'] != 0:
                raise with_error_code(RuntimeError(error), 'RAKUTEN-05-054-003')
            elif dom['headers'] != expected_headers:
                raise with_error_code(RuntimeError(error), 'RAKUTEN-05-054-004')
        elif dom['no_data_count'] != 1:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-055-001')
        elif (dom['table_count'], dom['visible_tables'], dom['headers']) not in ((0, 0, []), (3, 1, expected_headers)):
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-055-002')

    @staticmethod
    def _validated_history_result(result: dict) -> dict:
        error = "rakuten-twd-history-result"
        if not isinstance(result, dict):
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-056-001')
        elif set(result) != {'account_no', 'accounts', 'txDetails', 'selected_month', 'dom', 'receipt', 'transport'}:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-056-002')
        identity = result.get("account_no")
        rows = result.get("txDetails")
        accounts = result.get("accounts")
        dom = result.get("dom")
        receipt = result.get("receipt")
        transport = result.get("transport")
        if not isinstance(identity, str):
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-057-001')
        elif re.fullmatch('\\d{10,16}', identity) is None:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-057-002')
        elif not isinstance(rows, list):
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-057-003')
        elif len(rows) > 50000:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-057-004')
        elif not isinstance(accounts, list):
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-057-005')
        elif len(accounts) != 1:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-057-006')
        elif not isinstance(accounts[0], dict):
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-057-007')
        elif set(accounts[0]) != {'acctNo', 'balance'}:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-057-008')
        elif accounts[0].get('acctNo') != identity:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-057-009')
        elif not isinstance(accounts[0].get('balance'), str):
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-057-010')
        elif re.fullmatch('\\s*(?:(?:NT)?\\$\\s*)?-?(?:0|[1-9]\\d{0,9}|[1-9]\\d{0,2}(?:,\\d{3}){1,3})\\s*', accounts[0]['balance']) is None:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-057-011')
        elif not isinstance(receipt, dict):
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-057-012')
        elif set(receipt) != {'identity', 'start', 'end', 'status', 'pages', 'rows'}:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-057-013')
        elif receipt.get('identity') != identity:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-057-014')
        elif type(receipt.get('pages')) is not int:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-057-015')
        elif receipt['pages'] != 1:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-057-016')
        elif type(receipt.get('rows')) is not int:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-057-017')
        elif receipt['rows'] != len(rows):
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-057-018')
        elif receipt.get('status') not in {'complete', 'explicit_empty'}:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-057-019')
        elif (receipt['status'] == 'complete') != bool(rows):
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-057-020')
        elif not isinstance(result.get('selected_month'), str):
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-057-021')
        elif not isinstance(dom, dict):
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-057-022')
        elif not isinstance(transport, dict):
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-057-023')
        elif set(transport) != {'url', 'method', 'status', 'content_type', 'redirected', 'main_frame', 'request_count', 'response_count'}:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-057-024')
        try:
            RakutenCrawler._validate_history_dom(dom, len(rows))
        except RuntimeError as source_error:
            raise with_error_code(RuntimeError(error), exception_error_id(source_error, bank="rakuten") or 'RAKUTEN-05-058-001') from None
        try:
            start = date.fromisoformat(receipt["start"])
            end = date.fromisoformat(receipt["end"])
            parsed_url = urlparse(transport["url"])
        except (TypeError, ValueError):
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-059-001') from None
        if start > end:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-060-001')
        elif (start.year, start.month) != (end.year, end.month):
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-060-002')
        elif start.day != 1:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-060-003')
        elif result['selected_month'] != f'{start:%Y/%m} 活存明細':
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-060-004')
        elif parsed_url.scheme != 'https':
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-060-005')
        elif parsed_url.netloc != 'www.rakuten-bank.com.tw':
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-060-006')
        elif parsed_url.path != QUERY_PATH:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-060-007')
        elif parsed_url.params:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-060-008')
        elif parsed_url.query:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-060-009')
        elif parsed_url.fragment:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-060-010')
        elif transport.get('method') != 'POST':
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-060-011')
        elif transport.get('status') != 200:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-060-012')
        elif transport.get('content_type') not in {'application/json', 'text/json'}:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-060-013')
        elif transport.get('redirected') is not False:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-060-014')
        elif transport.get('main_frame') is not True:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-060-015')
        elif type(transport.get('request_count')) is not int:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-060-016')
        elif transport['request_count'] != 1:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-060-017')
        elif type(transport.get('response_count')) is not int:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-060-018')
        elif transport['response_count'] != 1:
            raise with_error_code(RuntimeError(error), 'RAKUTEN-05-060-019')

        row_keys = {
            "sysDate", "sysTime", "txDesc", "nickNameOrAcct", "amt", "amtSign", "balance", "memo",
        }
        money = re.compile(r"(?:0|[1-9]\d{0,9}|[1-9]\d{0,2}(?:,\d{3}){1,3})")
        signed_money = re.compile(r"-?(?:0|[1-9]\d{0,9}|[1-9]\d{0,2}(?:,\d{3}){1,3})")
        for row in rows:
            if not isinstance(row, dict):
                raise with_error_code(RuntimeError(error), 'RAKUTEN-05-061-001')
            elif set(row) != row_keys:
                raise with_error_code(RuntimeError(error), 'RAKUTEN-05-061-002')
            try:
                datetime.strptime(
                    f"{row['sysDate']} {row['sysTime']}", "%Y/%m/%d %H:%M:%S",
                )
            except (KeyError, TypeError, ValueError):
                raise with_error_code(RuntimeError(error), 'RAKUTEN-05-062-001') from None
            if not isinstance(row['txDesc'], str):
                raise with_error_code(RuntimeError(error), 'RAKUTEN-05-063-001')
            elif not row['txDesc'].strip():
                raise with_error_code(RuntimeError(error), 'RAKUTEN-05-063-002')
            elif len(row['txDesc']) > 2000:
                raise with_error_code(RuntimeError(error), 'RAKUTEN-05-063-003')
            elif row['nickNameOrAcct'] is not None and (not isinstance(row['nickNameOrAcct'], str) or len(row['nickNameOrAcct']) > 2000):
                raise with_error_code(RuntimeError(error), 'RAKUTEN-05-063-004')
            elif not isinstance(row['amt'], str):
                raise with_error_code(RuntimeError(error), 'RAKUTEN-05-063-005')
            elif money.fullmatch(row['amt']) is None:
                raise with_error_code(RuntimeError(error), 'RAKUTEN-05-063-006')
            elif type(row['amtSign']) is not bool:
                raise with_error_code(RuntimeError(error), 'RAKUTEN-05-063-007')
            elif not isinstance(row['balance'], str):
                raise with_error_code(RuntimeError(error), 'RAKUTEN-05-063-008')
            elif signed_money.fullmatch(row['balance']) is None:
                raise with_error_code(RuntimeError(error), 'RAKUTEN-05-063-009')
            elif not isinstance(row['memo'], str):
                raise with_error_code(RuntimeError(error), 'RAKUTEN-05-063-010')
            elif len(row['memo']) > 2000:
                raise with_error_code(RuntimeError(error), 'RAKUTEN-05-063-011')
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
        if mode not in {'full', 'incremental'}:
            raise with_error_code(ValueError(f'invalid BANK_CRAWLER_HISTORY_MODE: {mode!r}'), 'RAKUTEN-05-007-001')

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
                raise with_error_code(RuntimeError('rakuten-twd-history-range'), 'RAKUTEN-05-008-001')
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
                    raise with_error_code(RuntimeError('rakuten-twd-history-months'), 'RAKUTEN-05-009-001')
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
            raise with_error_code(RuntimeError('rakuten-twd-history-inventory'), 'RAKUTEN-05-010-001')

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
        with error_step('RAKUTEN-07-001-001'):
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
                raise with_error_code(RuntimeError('樂天瀏覽器 context 未建立'), 'RAKUTEN-01-001-001')
            with error_step('RAKUTEN-01-002-001'):
                page = engine.context.new_page()
            with error_step('RAKUTEN-01-003-001'):
                page.set_default_navigation_timeout(180000)
            with error_step('RAKUTEN-01-004-001'):
                page.set_default_timeout(180000)
            if headers := fetch_kwargs.get("extra_headers"):
                with error_step('RAKUTEN-01-005-001'):
                    page.set_extra_http_headers(headers)
            try:
                with error_step('RAKUTEN-01-006-001'):
                    page.goto(login_url, referer="https://www.google.com/")
                with error_step('RAKUTEN-01-007-001'):
                    page.wait_for_load_state("load")
                with error_step('RAKUTEN-01-008-001'):
                    page.wait_for_load_state("domcontentloaded")
                page_action(page)
                with error_step('RAKUTEN-01-009-001'):
                    page.wait_for_timeout(2000)
            finally:
                with error_step('RAKUTEN-01-010-001'):
                    page.close()

    def _logged_in(self, page, *, _auth_probe_ids=None) -> bool:
        if _auth_probe_ids is not None:
            _auth_probe_ids.clear()
        try:
            current = urlparse(page.url or "")
            path = (current.path or "").lower()
            if (
                not self._exact_https_origin_allowed(page.url, self.CREDENTIAL_HOSTS)
                or not path.startswith("/ebank/")
                or LOGIN_PATH_HINT in path
            ):
                return False
            with error_step('RAKUTEN-03-003-001'):
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
        except Exception as error:
            if _auth_probe_ids is not None:
                error_id = exception_error_id(error, bank='rakuten')
                if error_id in ('RAKUTEN-03-003-001',):
                    _auth_probe_ids[:] = [error_id]
            return False

    def login(self, page) -> bool:
        return self._shared_login(page)

    def prepare_login_page(self, page) -> None:
        # browserStartup may take 3s for a token plus 15s for its handshake.
        with error_step('RAKUTEN-01-011-001'):
            page.wait_for_timeout(20000)

    def is_authenticated(self, page) -> bool:
        auth_probe_ids = getattr(self, "_login_auth_probe_ids", None)
        return not self._semantic_modal_visible_in_owned_scope(page) and (
            self._logged_in(page) if auth_probe_ids is None
            else self._logged_in(page, _auth_probe_ids=auth_probe_ids)
        )

    def _semantic_modal_visible_in_owned_scope(self, page) -> bool:
        self._modal_failure_id = None
        try:
            if _semantic_modal_visible(page):
                return True
            main_frame = getattr(page, "main_frame", None)
            frames = getattr(page, "frames", ())
            if not isinstance(frames, (list, tuple)):
                frames = ()
            return any(
                frame is not main_frame
                and self._frame_origin_allowed(page, frame)
                and _semantic_modal_visible(frame)
                for frame in frames
            )
        except Exception as error:
            self._modal_failure_id = exception_error_id(error, bank="rakuten")
            return True

    def _recover_late_authentication(self, page, error: Exception) -> bool:
        if (
            not isinstance(error, LoginCheckpointBlocked)
            or error.budget != LoginBudget(credential_submissions=1)
            or error.outcome.kind is not CheckpointKind.UNKNOWN_BLOCKER
            or error.outcome.rule_name not in {None, "rakuten-promo", "rakuten-unknown-modal"}
            or getattr(self, "_shared_dialog_blocked", False)
        ):
            return False
        # Unknown post-submit modals fail closed; record their shape so the rule can be added.
        print(f"[rakuten][login] unknown modal shape={_modal_shape(page)}", flush=True)
        authenticated_quiet_polls = 0
        deadline = time.monotonic() + 60.0  # matches the 60s loader wait in collect()

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            with error_step('RAKUTEN-03-014-001'):
                page.wait_for_timeout(min(1000, max(1, int(remaining * 1000))))
            if (
                time.monotonic() >= deadline
                or getattr(self, "_shared_dialog_blocked", False)
                or not self._credential_origin_allowed(page)
                or _any_visible(page, "input[name='otpCode']")
                or _any_visible(page, "#ib_init_connect_error_popup")
            ):
                return False
            if _any_visible(page, LOADER_SELECTOR):
                authenticated_quiet_polls = 0
                continue
            if self._semantic_modal_visible_in_owned_scope(page):
                return False
            if not self._logged_in(page):
                authenticated_quiet_polls = 0
                continue
            authenticated_quiet_polls += 1
            if authenticated_quiet_polls < 3:
                continue
            return not (
                time.monotonic() >= deadline
                or getattr(self, "_shared_dialog_blocked", False)
                or not self._credential_origin_allowed(page)
                or _any_visible(page, "input[name='otpCode']")
                or _any_visible(page, "#ib_init_connect_error_popup")
                or _any_visible(page, LOADER_SELECTOR)
                or self._semantic_modal_visible_in_owned_scope(page)
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
                error_id='RAKUTEN-03-800-001',
                bank="rakuten",
                phases=(CheckpointPhase.PRE_SUBMIT,),
                kind=CheckpointKind.STARTUP_RECOVERY,
                container_selector="#ib_init_connect_error_popup",
            ),
            LoginCheckpointRule(
                name="rakuten-duplicate-session",
                error_id='RAKUTEN-03-805-001',
                bank="rakuten",
                phases=all_phases,
                kind=CheckpointKind.DUPLICATE_SESSION,
                container_selector=".modal.show",
                action_texts=("是，我要登入",),
                required_body_pattern=duplicate_body,
            ),
            LoginCheckpointRule(
                name="rakuten-otp-required",
                error_id='RAKUTEN-03-050-001',
                bank="rakuten",
                phases=(CheckpointPhase.POST_SUBMIT, CheckpointPhase.POST_SUBMIT_SETTLE),
                kind=CheckpointKind.OTP_REQUIRED,
                container_selector="input[name='otpCode']",
            ),
            # Promo copy changes every campaign; match the dismiss button, never the body.
            LoginCheckpointRule(
                name="rakuten-promo",
                error_id='RAKUTEN-03-810-001',
                bank="rakuten",
                phases=all_phases,
                kind=CheckpointKind.DISMISSIBLE_NOTICE,
                container_selector=".modal.show",
                action_texts=("稍後再說", "稍後再看", "稍後", "略過"),
            ),
            LoginCheckpointRule(
                name="rakuten-unknown-modal",
                error_id='RAKUTEN-03-055-001',
                bank="rakuten",
                phases=all_phases,
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector=".modal.show",
                required_body_pattern=re.compile(r"\S"),
            ),
        )

    def submit_credentials_once(self, page) -> None:

        self._modal_failure_id = None
        try:
            modal_visible = self._semantic_modal_visible_in_owned_scope(page)
        finally:
            modal_failure_id = self._modal_failure_id
            self._modal_failure_id = None
        if modal_visible:
            raise with_error_code(RakutenLoginError('登入前仍有未處理互動視窗；未送出登入'), modal_failure_id or 'RAKUTEN-02-001-001')

        for selector in ("#custNo", "#userNo", "#pcode"):
            with error_step({'#custNo': 'RAKUTEN-02-011-001', '#userNo': 'RAKUTEN-02-017-001', '#pcode': 'RAKUTEN-02-023-001', '#captcha': 'RAKUTEN-02-029-001'}[selector]):
                page.wait_for_selector(selector, timeout=15000)

        captcha = ""
        with error_step('RAKUTEN-02-002-001'):
            if error_step('RAKUTEN-02-707-001')(lambda: error_step('RAKUTEN-02-708-001')(lambda: page.locator(CAPTCHA_IMG))().is_visible())():
                with error_step('RAKUTEN-02-003-001'):
                    wait_captcha_stable(page, CAPTCHA_IMG, tmp_path=self.captcha_tmp)
                with error_step('RAKUTEN-02-004-001'):
                    captcha = solve_captcha(
                        page,
                        CAPTCHA_IMG,
                        expected_len=4,
                        min_confidence=0.95,
                        tmp_path=self.captcha_tmp,
                    ) or ""
                if not captcha:
                    raise with_error_code(captcha_unreadable(RakutenLoginError('圖形驗證碼 OCR 失敗；未送出登入')), 'RAKUTEN-02-005-001')

        fields = (
            ("#custNo", self.creds.national_id),
            ("#userNo", self.creds.user_code),
            ("#pcode", self.creds.password),
            ("#captcha", captcha),
        )
        for selector, value in fields:
            if not value and selector == "#captcha":
                continue
            with error_step({'#custNo': 'RAKUTEN-02-012-001', '#userNo': 'RAKUTEN-02-018-001', '#pcode': 'RAKUTEN-02-024-001', '#captcha': 'RAKUTEN-02-030-001'}[selector]):
                locator = page.locator(selector)
            with error_step({'#custNo': 'RAKUTEN-02-013-001', '#userNo': 'RAKUTEN-02-019-001', '#pcode': 'RAKUTEN-02-025-001', '#captcha': 'RAKUTEN-02-031-001'}[selector]):
                locator.click()
            with error_step({'#custNo': 'RAKUTEN-02-014-001', '#userNo': 'RAKUTEN-02-020-001', '#pcode': 'RAKUTEN-02-026-001', '#captcha': 'RAKUTEN-02-032-001'}[selector]):
                locator.press("ControlOrMeta+A")
            with error_step({'#custNo': 'RAKUTEN-02-015-001', '#userNo': 'RAKUTEN-02-021-001', '#pcode': 'RAKUTEN-02-027-001', '#captcha': 'RAKUTEN-02-033-001'}[selector]):
                locator.press("Backspace")
            with error_step({'#custNo': 'RAKUTEN-02-016-001', '#userNo': 'RAKUTEN-02-022-001', '#pcode': 'RAKUTEN-02-028-001', '#captcha': 'RAKUTEN-02-034-001'}[selector]):
                locator.press_sequentially(value, delay=60)

        with error_step('RAKUTEN-02-006-001'):
            lengths = page.evaluate("""() => Object.fromEntries(
            ['custNo', 'userNo', 'pcode', 'captcha'].map(id => [id, document.getElementById(id)?.value.length || 0])
        )""")
        expected = {
            "custNo": len(self.creds.national_id),
            "userNo": len(self.creds.user_code),
            "pcode": len(self.creds.password),
            "captcha": len(captcha),
        }
        for key, value in expected.items():
            if lengths.get(key) != value:
                raise with_error_code(RakutenLoginError('登入欄位輸入長度不符；未送出登入'), {'custNo': 'RAKUTEN-02-007-001', 'userNo': 'RAKUTEN-02-008-001', 'pcode': 'RAKUTEN-02-009-001', 'captcha': 'RAKUTEN-02-035-001'}[key])

        login_action_error = RakutenLoginError('找不到唯一且可操作的登入按鈕；未送出登入')
        if not _click_visible_login(page, _failure=login_action_error):
            raise with_error_code(login_action_error, 'RAKUTEN-02-010-001')

        for _ in range(20):
            with error_step('RAKUTEN-03-001-001'):
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
            except Exception as _action_error:
                with contextlib.suppress(Exception):
                    print(format_error_id(exception_error_id(_action_error, bank="rakuten") or 'RAKUTEN-03-002-001', bank="rakuten"))
                return

    def logout(self, page) -> bool:
        pattern = re.compile(r"^\s*(?:安全)?登出\s*$")
        for frame in page.frames:
            with error_step('RAKUTEN-07-006-001'):
                candidates = error_step('RAKUTEN-07-709-001')(lambda: error_step('RAKUTEN-07-710-001')(lambda: frame.locator("a, button"))().filter(has_text=pattern))()
            with error_step('RAKUTEN-07-007-001'):
                for index in range(candidates.count()):
                    with error_step('RAKUTEN-07-008-001'):
                        candidate = candidates.nth(index)
                    with error_step('RAKUTEN-07-009-001'):
                        if not candidate.is_visible():
                            continue
                    with error_step('RAKUTEN-07-010-001'):
                        candidate.click()
                    modal_selector = self.VISIBLE_CONFIRM_SELECTOR
                    with error_step('RAKUTEN-07-011-001'):
                        page.wait_for_selector(modal_selector, state="visible", timeout=10000)
                    with error_step('RAKUTEN-07-012-001'):
                        modals = page.locator(modal_selector)
                    with error_step('RAKUTEN-07-013-001'):
                        for modal_index in range(modals.count()):
                            with error_step('RAKUTEN-07-014-001'):
                                modal = modals.nth(modal_index)
                            with error_step('RAKUTEN-07-015-001'):
                                body = " ".join(error_step('RAKUTEN-07-711-001')(lambda: error_step('RAKUTEN-07-712-001')(lambda: modal.locator(".modal-body"))().inner_text())().split())
                            if body != self.LOGOUT_BODY:
                                continue
                            with error_step('RAKUTEN-07-016-001'):
                                submit = error_step('RAKUTEN-07-713-001')(lambda: error_step('RAKUTEN-07-714-001')(lambda: modal.locator(
                                    "a:visible, button:visible, [role=button]:visible",
                                ))().filter(has_text=re.compile(r"^\s*確認\s*$")))()
                            with error_step('RAKUTEN-07-017-001'):
                                if error_step('RAKUTEN-07-715-001')(lambda: submit.count())() != 1:
                                    raise with_error_code(RakutenLoginError('樂天登出提示缺少唯一確認按鈕'), 'RAKUTEN-07-018-001')
                                elif not error_step('RAKUTEN-07-716-001')(lambda: submit.first.is_visible())():
                                    raise with_error_code(RakutenLoginError('樂天登出提示缺少唯一確認按鈕'), 'RAKUTEN-07-018-002')
                            with error_step('RAKUTEN-07-019-001'):
                                submit.first.click()
                            with error_step('RAKUTEN-07-020-001'):
                                page.wait_for_selector("#custNo", state="visible", timeout=30000)
                            return True
                    raise with_error_code(RakutenLoginError('樂天登出後未出現預期確認提示'), 'RAKUTEN-07-021-001')
        return False

    @staticmethod
    def _scrape_twd_page(page, account_no: str | None = None) -> dict:
        with error_step('RAKUTEN-05-064-001'):
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
            raise with_error_code(RuntimeError('rakuten-twd-history-dom'), 'RAKUTEN-05-065-001')
        number = account_no or extracted_number
        if not isinstance(raw_rows, list):
            raise with_error_code(RuntimeError('rakuten-twd-history-dom'), 'RAKUTEN-05-066-001')
        parsed_rows = [_row_from_dom(cells) for cells in raw_rows]
        for parsed in parsed_rows:
            if parsed is None:
                raise with_error_code(RuntimeError('rakuten-twd-history-dom'), 'RAKUTEN-05-067-001')
        if snapshot:
            raise with_error_code(RuntimeError('rakuten-twd-history-dom'), 'RAKUTEN-05-067-002')
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
            with error_step('RAKUTEN-05-037-001'):
                return error_step('RAKUTEN-05-717-001')(lambda: error_step('RAKUTEN-05-718-001')(lambda: page.locator(f"{root} a.txt_dropdown"))().inner_text())().strip()
        with error_step('RAKUTEN-05-038-001'):
            return error_step('RAKUTEN-05-719-001')(lambda: error_step('RAKUTEN-05-720-001')(lambda: page.locator(f"{root} button.input-select"))().inner_text())().strip()

    @staticmethod
    def _open_dropdown(page, root: str) -> None:
        with error_step('RAKUTEN-05-039-001'):
            trigger = (
                error_step('RAKUTEN-05-721-001')(lambda: page.locator(f"{root} a.txt_dropdown"))()
                if root == "simple-dropdown2"
                else error_step('RAKUTEN-05-722-001')(lambda: page.locator(f"{root} button.input-select"))()
            )
        with error_step(('RAKUTEN-05-040-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-040-021')):
            trigger.click()

    @staticmethod
    def _selection_options(page, root: str):
        with error_step(('RAKUTEN-05-041-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-041-021')):
            all_options = page.locator(f"{root} .dropdown-menu a.dropdown-item")
        if root == "simple-dropdown2":
            with error_step('RAKUTEN-05-042-001'):
                trigger = page.locator(f"{root} > a.txt_dropdown[aria-expanded='true']")
            with error_step('RAKUTEN-05-043-001'):
                menu = trigger.locator(":scope > .dropdown-menu.show[hidden]")
            with error_step('RAKUTEN-05-044-001'):
                if (
                    error_step('RAKUTEN-05-723-001')(lambda: trigger.count())() == 1
                    and error_step('RAKUTEN-05-724-001')(lambda: trigger.is_visible())()
                    and error_step('RAKUTEN-05-725-001')(lambda: menu.count())() == 1
                    and error_step('RAKUTEN-05-726-001')(lambda: error_step('RAKUTEN-05-727-001')(lambda: menu.locator("a.dropdown-item"))().count())() == error_step('RAKUTEN-05-728-001')(lambda: all_options.count())()
                ):
                    return all_options, True
        return all_options, False

    @staticmethod
    def _visible_labels(page, root: str) -> list[str]:
        RakutenCrawler._open_dropdown(page, root)
        previous: list[str] | None = None
        stable_since: float | None = None
        deadline = time.monotonic() + 3.0
        try:
            options, native_hidden_inventory = RakutenCrawler._selection_options(page, root)
            while time.monotonic() < deadline:
                with error_step(('RAKUTEN-05-045-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-045-021')):
                    sample = options.evaluate_all(r"""(elements, nativeHidden) => elements.map(element => {
                    let visible = !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length);
                    for (let node = element; visible && node; node = node.parentElement) {
                        const style = getComputedStyle(node);
                        if (node.hidden || style.display === 'none' || style.visibility === 'hidden'
                            || style.visibility === 'collapse' || Number(style.opacity) <= 0.01) {
                            visible = false;
                        }
                    }
                    const text = nativeHidden ? element.textContent : (element.innerText || element.textContent);
                    return {label: (text || '').trim(), visible};
                })""", native_hidden_inventory)
                labels = [item.get("label") for item in sample]
                valid = (
                    bool(labels)
                    and (
                        native_hidden_inventory
                        or all(item.get("visible") is True for item in sample)
                    )
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
                with error_step(('RAKUTEN-05-046-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-046-021')):
                    page.wait_for_timeout(min(100, remaining_ms))
            raise with_error_code(RuntimeError('rakuten-twd-history-inventory'), ('RAKUTEN-05-047-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-047-021'))
        finally:
            with error_step(('RAKUTEN-05-048-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-048-021')):
                page.keyboard.press("Escape")

    @staticmethod
    def _twd_view_state(page, _error_role="view") -> dict:
        with error_step({'before_account': 'RAKUTEN-05-851-001', 'before_month': 'RAKUTEN-05-852-001', 'initial': 'RAKUTEN-05-853-001', 'account': 'RAKUTEN-05-854-001', 'month': 'RAKUTEN-05-855-001'}.get(_error_role, 'RAKUTEN-05-052-001')):
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
        _error_role = "initial" if not selected_root else ("account" if selected_root == "simple-dropdown2" else "month")
        deadline = time.monotonic() + timeout_seconds
        last_ready: tuple[str, str, str, str, str] | None = None
        stable_count = 0
        while time.monotonic() < deadline:
            if selected_root and selected_label:
                selected = self._selected_label(page, selected_root)
                if not _selection_matches(selected_root, selected, selected_label):
                    with error_step({'initial': 'RAKUTEN-05-049-040', 'account': 'RAKUTEN-05-049-041', 'month': 'RAKUTEN-05-049-042'}[_error_role]):
                        page.wait_for_timeout(250)
                    continue
            state = self._twd_view_state(page, _error_role)
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
            with error_step({'initial': 'RAKUTEN-05-050-040', 'account': 'RAKUTEN-05-050-041', 'month': 'RAKUTEN-05-050-042'}[_error_role]):
                page.wait_for_timeout(300)
        kind = "initial" if not selected_root else (
            "account" if selected_root == "simple-dropdown2" else "month"
        )
        raise with_error_code(RuntimeError(f'樂天臺幣頁資料未完成（kind={kind}）'), {'initial': 'RAKUTEN-05-051-040', 'account': 'RAKUTEN-05-051-041', 'month': 'RAKUTEN-05-051-042'}[_error_role])

    def _select_label(
        self,
        page,
        collector: ResponseCollector,
        root: str,
        label: str,
    ) -> dict:
        self._open_dropdown(page, root)
        with error_step(('RAKUTEN-05-016-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-016-021')):
            page.wait_for_timeout(100)
        options, native_hidden_inventory = self._selection_options(page, root)
        if not native_hidden_inventory:
            with error_step(('RAKUTEN-05-017-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-017-021')):
                options = page.locator(f"{root} .dropdown-menu a.dropdown-item:visible")
        with error_step(('RAKUTEN-05-018-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-018-021')):
            labels = [
                (error_step(('RAKUTEN-05-729-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-729-021'))(lambda: error_step(('RAKUTEN-05-730-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-730-021'))(lambda: options.nth(i))().text_content())() if native_hidden_inventory else error_step(('RAKUTEN-05-731-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-731-021'))(lambda: error_step(('RAKUTEN-05-732-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-732-021'))(lambda: options.nth(i))().inner_text())()).strip()
                for i in range(error_step(('RAKUTEN-05-733-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-733-021'))(lambda: options.count())())
            ]
        target_index = _unique_option_index(labels, label)
        if target_index is None:
            kind = "account" if root == "simple-dropdown2" else "month"
            raise with_error_code(RuntimeError(f'找不到唯一可見的樂天下拉選項（kind={kind}）'), ('RAKUTEN-05-019-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-019-021'))
        with error_step(('RAKUTEN-05-020-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-020-021')):
            target = options.nth(target_index)
        before_rows = str(self._twd_view_state(page, "before_account" if root == "simple-dropdown2" else "before_month").get("rows") or "")
        request_boundary = collector.request_sequence
        issued_before = collector.issued_count("011")
        with error_step(('RAKUTEN-05-021-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-021-021')):
            page.wait_for_selector(LOADER_SELECTOR, state="hidden", timeout=20000)
        with error_step(('RAKUTEN-05-022-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-022-021')), page.expect_request(_is_twd_query_request, timeout=20000) as request_info:
            if native_hidden_inventory:
                with error_step(('RAKUTEN-05-023-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-023-021')):
                    trigger = page.locator(f"{root} > a.txt_dropdown[aria-expanded='true']")
                for _ in range(target_index + 1):
                    with error_step(('RAKUTEN-05-024-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-024-021')):
                        trigger.press("ArrowDown")
                with error_step(('RAKUTEN-05-025-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-025-021')):
                    trigger.press("Enter")
            else:
                with error_step(('RAKUTEN-05-026-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-026-021')):
                    target.click()
            with error_step(('RAKUTEN-05-027-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-027-021')):
                page.wait_for_selector(LOADER_SELECTOR, state="visible", timeout=3000)
        try:
            with error_step(('RAKUTEN-05-028-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-028-021')):
                response = request_info.value.response()
            if response is None:
                raise with_error_code(RuntimeError('樂天臺幣查詢回應狀態失敗'), ('RAKUTEN-05-029-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-029-021'))
            elif not 200 <= response.status < 300:
                raise with_error_code(RuntimeError('樂天臺幣查詢回應狀態失敗'), ('RAKUTEN-05-029-022' if root == 'simple-dropdown2' else 'RAKUTEN-05-029-023'))
            with error_step(('RAKUTEN-05-030-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-030-021')):
                response.finished()
        except Exception as e:
            raise with_error_code(RuntimeError('樂天臺幣查詢 request 未成功'), ('RAKUTEN-05-031-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-031-021')) from e
        with error_step(('RAKUTEN-05-032-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-032-021')):
            page.wait_for_selector(LOADER_SELECTOR, state="hidden", timeout=20000)
        self._wait_for_twd_view(
            page,
            selected_root=root,
            selected_label=label,
            before_rows=before_rows,
        )
        hits = []
        for _ in range(4):
            with error_step(('RAKUTEN-05-033-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-033-021')):
                page.wait_for_timeout(500)
            hits = [
                hit for hit in collector.hits
                if hit.request_sequence > request_boundary
                and urlparse(hit.raw_url or hit.url).path == QUERY_PATH
            ]
            if collector.issued_count('011') - issued_before > 1:
                raise with_error_code(RuntimeError('rakuten-twd-history-response-cardinality'), ('RAKUTEN-05-034-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-034-021'))
            elif len(hits) > 1:
                raise with_error_code(RuntimeError('rakuten-twd-history-response-cardinality'), ('RAKUTEN-05-034-022' if root == 'simple-dropdown2' else 'RAKUTEN-05-034-023'))
        if collector.issued_count('011') - issued_before != 1:
            raise with_error_code(RuntimeError('rakuten-twd-history-response-cardinality'), ('RAKUTEN-05-035-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-035-021'))
        elif len(hits) != 1:
            raise with_error_code(RuntimeError('rakuten-twd-history-response-cardinality'), ('RAKUTEN-05-035-022' if root == 'simple-dropdown2' else 'RAKUTEN-05-035-023'))
        hit = hits[0]
        request = request_info.value
        request_frame = _OriginGuardProxy._unwrap(getattr(request, "frame", None))
        main_frame = _OriginGuardProxy._unwrap(getattr(page, "main_frame", None))
        if request_frame is not main_frame:
            raise with_error_code(RuntimeError('rakuten-twd-history-frame'), ('RAKUTEN-05-036-020' if root == 'simple-dropdown2' else 'RAKUTEN-05-036-021'))
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
        with error_step('RAKUTEN-05-001-001'):
            page.wait_for_selector(LOADER_SELECTOR, state="hidden", timeout=60000)
        with error_step('RAKUTEN-05-850-001'):
            deposit = page.get_by_role("link", name="存款", exact=True).first
        try:
            with error_step('RAKUTEN-05-002-001'):
                deposit.click(timeout=5000)
        except Exception:
            if self._semantic_modal_visible_in_owned_scope(page):
                raise with_error_code(RakutenLoginError('臺幣存款導覽遭互動視窗阻擋；未執行視窗操作'), 'RAKUTEN-05-003-001') from None
            raise
        with error_step('RAKUTEN-05-004-001'):
            page.wait_for_selector("a.sub-nav-link:has-text('臺幣存款')", state="visible", timeout=15000)
        with error_step('RAKUTEN-05-005-001'):
            error_step('RAKUTEN-05-734-001')(lambda: error_step('RAKUTEN-05-735-001')(lambda: page.locator("a.sub-nav-link", has_text="臺幣存款"))().first.click())()
        with error_step('RAKUTEN-05-006-001'):
            page.wait_for_url(lambda url: TWD_PATH_HINT in url, timeout=30000)

    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        self._goto_twd(page)
        with error_step('RAKUTEN-07-002-001'):
            page.wait_for_selector(
                "simple-dropdown2 a.txt_dropdown",
                state="visible",
                timeout=60000,
            )
        with error_step('RAKUTEN-07-003-001'):
            page.wait_for_selector(
                "simple-dropdown button.input-select",
                state="visible",
                timeout=60000,
            )
        with error_step('RAKUTEN-07-004-001'):
            page.wait_for_selector(LOADER_SELECTOR, state="hidden", timeout=60000)
        if not self._logged_in(page):
            raise with_error_code(RakutenLoginError('進入臺幣存款頁後 session 無效'), 'RAKUTEN-07-005-001')
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
