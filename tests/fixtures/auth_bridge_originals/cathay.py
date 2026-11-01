#!/usr/bin/env python3
"""國泰世華 MyBank 全量抓取器。

繼承 BankCrawler。登入後造訪六大類功能頁，攔截式抓取所有資料 API，
parse 成結構化 dict。需互動的明細頁（台幣交易、刷卡明細）自動操作觸發。
"""
from __future__ import annotations

import copy
import os
import re
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.base import BankCollectResult, BankCrawler, ResponseCollector
from backend.core.card_bills import (
    card_bill_date,
    card_bill_money,
    make_card_bill_fact,
    publish_card_bill_facts,
)
from backend.core.card_status import cathay_bill_status
from backend.core.error_codes import error_step, with_error_code, exception_error_id
from backend.core.creds import CathayCreds
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointPhase,
    LoginCheckpointRule,
    LoginCheckpointTerminal,
)

SEL_CUSTID = "#CustID"
SEL_USERID = "#UserIdKeyin"
SEL_PWD = "#PasswordKeyin"
SEL_LOGIN_BTN = "button.js-login"

BASE = "https://www.cathaybk.com.tw"
_ACCOUNT_INVENTORY_PATH = (
    "/OnlineBankingApi/Common/Api/ClientCommon/G_CUST_Q_TransAccountList"
)
_TWD_HISTORY_PATH = (
    "/OnlineBankingApi/ClientBank/Api/ClientBank/B_ACCT_Q_TransferDetail"
)
_TWD_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    # fromisoformat normalizes overflowing offset minutes instead of rejecting them.
    r"(?:Z|[+-]\d{2}:?[0-5]\d)?$",
)
_TWD_AMOUNT_RE = re.compile(r"^[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.0+)?$")
_PG_INTEGER_MAX = 2_147_483_647
_AMOUNT_FIELD_CODES = {'expendAmt': ('CATHAY-05-800-001', 'CATHAY-05-800-002', 'CATHAY-05-800-003', 'CATHAY-05-800-004', 'CATHAY-05-800-005', 'CATHAY-05-800-006', 'CATHAY-05-800-007', 'CATHAY-05-800-008'), 'incomeAmt': ('CATHAY-05-801-001', 'CATHAY-05-801-002', 'CATHAY-05-801-003', 'CATHAY-05-801-004', 'CATHAY-05-801-005', 'CATHAY-05-801-006', 'CATHAY-05-801-007', 'CATHAY-05-801-008'), 'balance': ('CATHAY-05-802-001', 'CATHAY-05-802-002', 'CATHAY-05-802-003', 'CATHAY-05-802-004', 'CATHAY-05-802-005', 'CATHAY-05-802-006', 'CATHAY-05-802-007', 'CATHAY-05-802-008')}

# 各功能頁：造訪後等 React 打 API
FEATURE_PAGES = {
    "asset": "/OnlineBanking/Home/Asset",
    "card": "/OnlineBanking/CQuery/C0101_BillOverview",
    "invest": "/OnlineBanking/Portfolio/F0109_InvestmentOverview",
    "insurance": "/OnlineBanking/Policy/I0108_InsuranceOverview",
    "loan": "/OnlineBanking/LoanInq/L0101_LoanInq",
    "twd_txn": "/OnlineBanking/AcctInq/B0103_TxnDtlInq",
}


# W (2026-06-17): positive signal — 對齊 SCSB 鐵律, 取代「URL 含 OnlineBanking 就算
# 已登入」negative-only 訊號。失敗 fallback goto 內銀區的 URL 也會 match 但 innerText
# 是 165 字錯誤頁時，原邏輯會誤判已登入。改為 4 條件 AND：
#   1. URL 在 OnlineBanking/Asset 區
#   2. innerText >= 500 字（< 500 = loading / 空白 / 錯誤頁）
#   3. 命中 >= 2 個主菜單字樣
#   4. 登入 form 元素 #CustID 已不可見（仍可見 = 還在 login page）
# 詳見 wiki/concepts/bank-crawler-login-positive-signal-rule.md
JS_LOGGED_IN_POSITIVE = r"""
(() => {
  const url = location.href || '';
  const urlOk = /\/OnlineBanking\//.test(url) && !/\/Login/i.test(url);
  const txt = (document.body && document.body.innerText) || '';
  const lenOk = txt.length >= 500;
  const keywords = [
    '帳戶總覽', '台幣存款', '外幣存款', '信用卡', '貸款',
    '投資理財', '保險', '我的最愛', '會員專區', '登出',
    '存款明細', '繳費', '信用卡明細',
  ];
  let hit = 0;
  for (const k of keywords) {
    if (txt.indexOf(k) !== -1) { hit += 1; if (hit >= 2) break; }
  }
  const kwOk = hit >= 2;
  const custIdEl = document.querySelector('#CustID');
  const noLoginForm = !(custIdEl && custIdEl.offsetParent !== null);
  return {
    ok: urlOk && lenOk && kwOk && noLoginForm,
    urlOk, lenOk, kwOk, noLoginForm,
    url, txt_len: txt.length, hit
  };
})()
"""


_FEATURE_STEPS = {'asset': ('CATHAY-04-500-001', 'CATHAY-04-501-001'),
 'card': ('CATHAY-06-500-001', 'CATHAY-06-501-001'),
 'invest': ('CATHAY-04-510-001', 'CATHAY-04-511-001'),
 'insurance': ('CATHAY-04-520-001', 'CATHAY-04-521-001'),
 'loan': ('CATHAY-04-530-001', 'CATHAY-04-531-001'),
 'twd_txn': ('CATHAY-05-500-001', 'CATHAY-05-501-001')}

_CARD_DETAIL_STEPS = {'即時消費明細': ('CATHAY-06-600-001',
            'CATHAY-06-601-001',
            'CATHAY-06-602-001',
            'CATHAY-06-603-001',
            'CATHAY-06-604-001',
            'CATHAY-06-605-001'),
 '未出帳明細': ('CATHAY-06-610-001',
           'CATHAY-06-611-001',
           'CATHAY-06-612-001',
           'CATHAY-06-613-001',
           'CATHAY-06-614-001',
           'CATHAY-06-615-001'),
 '帳單明細': ('CATHAY-06-620-001',
          'CATHAY-06-621-001',
          'CATHAY-06-622-001',
          'CATHAY-06-623-001',
          'CATHAY-06-624-001',
          'CATHAY-06-625-001')}

_SEED_SELECT_STEPS = {'帳號': ('CATHAY-05-600-001',
        'CATHAY-05-601-001',
        'CATHAY-05-602-001',
        'CATHAY-05-603-001',
        'CATHAY-05-604-001',
        'CATHAY-05-605-001'),
 '查詢期間': ('CATHAY-05-610-001',
          'CATHAY-05-611-001',
          'CATHAY-05-612-001',
          'CATHAY-05-613-001',
          'CATHAY-05-614-001',
          'CATHAY-05-615-001')}

def _native_step(error_id, operation):
    """Keep expression short-circuiting while tagging one native operation."""
    with error_step(error_id):
        return operation()


def _log(*a):
    print(*a, file=sys.stderr)


# W (2026-06-17): cathay 統一也 raise CathayLoginError, 跟其他 11 家一致.
# session 仍存活 fallback 仍回 True (避免重打鎖卡), 只在「絕對失敗」raise.
class CathayLoginError(RuntimeError):
    """Cathay 登入失敗（絕對失敗，重打也沒用，會鎖帳號）。"""


def _click_login_once(page) -> None:
    try:
        candidates = _native_step('CATHAY-02-100-001', lambda: page.locator(SEL_LOGIN_BTN))
        if _native_step('CATHAY-02-101-001', lambda: candidates.count()) != 1:
            raise with_error_code(CathayLoginError("找不到唯一且可操作的登入按鈕；未送出登入"), "CATHAY-02-050-001")
        button = _native_step('CATHAY-02-102-001', lambda: candidates.first)
        classes = (_native_step('CATHAY-02-103-001', lambda: button.get_attribute("class")) or "").split()
        if not _native_step('CATHAY-02-104-001', lambda: button.is_visible()):
            raise with_error_code(CathayLoginError("找不到唯一且可操作的登入按鈕；未送出登入"), "CATHAY-02-050-002")
        if not _native_step('CATHAY-02-105-001', lambda: button.is_enabled()):
            raise with_error_code(CathayLoginError("找不到唯一且可操作的登入按鈕；未送出登入"), "CATHAY-02-050-003")
        if 'disabled' in classes:
            raise with_error_code(CathayLoginError("找不到唯一且可操作的登入按鈕；未送出登入"), "CATHAY-02-050-004")
    except CathayLoginError:
        raise
    except Exception:
        raise with_error_code(CathayLoginError("無法安全確認登入按鈕；未送出登入"), "CATHAY-02-050-005") from None
    try:
        _native_step('CATHAY-02-106-001', lambda: button.click(timeout=8000))
    except Exception:
        raise with_error_code(CathayLoginError("登入送出狀態不明；禁止自動重試"), "CATHAY-02-050-006") from None


def _cathay_card_bill_fact(out: dict):
    with error_step("CATHAY-07-030-001"):
        credit_card = out.get("credit_card") or {}
        latest = credit_card.get("latest_bill") or {}
        twd_bill = latest.get("twd") if isinstance(latest, dict) else None
        if not isinstance(twd_bill, dict):
            return None
        raw_status = twd_bill.get("payBillStatus")
        if raw_status not in {"paid", "unpaid"}:
            return None
        statement_amount = card_bill_money(twd_bill.get("billAmount"))
        if statement_amount is None:
            return None

        bill_summary = credit_card.get("bill_summary") or {}
        currencies = bill_summary.get("currencies") if isinstance(bill_summary, dict) else []
        current_currency = currencies[0] if isinstance(currencies, list) and currencies and isinstance(currencies[0], dict) else {}
        statement_date = (
            current_currency.get("billDate")
            or (credit_card.get("total_consumption") or {}).get("last_stmt_date")
        )
        due_date = latest.get("due_date") or bill_summary.get("payment_deadline")

        candidates = []
        billed = (credit_card.get("billed_detail") or {}).get("TWD") or []
        for row in billed:
            if not isinstance(row, dict) or not any(
                label in str(row.get("desc") or "") for label in ("自動扣繳", "繳款", "已繳")
            ):
                continue
            raw_amount = row.get("amount")
            if raw_amount is None:
                continue
            try:
                amount = float(raw_amount)
            except (TypeError, ValueError):
                continue
            payment_date = card_bill_date(row.get("post_date"))
            payment_amount = card_bill_money(abs(amount))
            if amount < 0 and payment_date and payment_amount is not None:
                candidates.append((payment_date, 0, payment_amount))
        for account in out.get("twd_transactions") or []:
            for txn in account.get("transactions") or []:
                if str(txn.get("desc") or "").strip() != "信用卡款":
                    continue
                payment_date = card_bill_date(txn.get("account_date") or txn.get("datetime"))
                payment_amount = card_bill_money(txn.get("expend"))
                if payment_date and payment_amount is not None and payment_amount > 0:
                    candidates.append((payment_date, 1, payment_amount))
        payment_date, _, payment_amount = max(candidates, default=(None, -1, None))

        return make_card_bill_fact(
            remaining_due=0 if raw_status == "paid" else statement_amount,
            status=raw_status,
            statement_close_date=statement_date,
            payment_due_date=due_date,
            last_payment_amount=payment_amount,
            last_payment_date=payment_date,
        )


class CathayCrawler(BankCrawler):
    USES_SHARED_LOGIN_CHECKPOINTS: ClassVar[bool] = True
    SAFE_COLLECT_GUARDS = frozenset({
        "cathay-twd-history-account-control",
        "cathay-twd-history-account-inventory",
        "cathay-twd-history-account-mismatch",
        "cathay-twd-history-count",
        "cathay-twd-history-fetch",
        "cathay-twd-history-invalid-range",
        "cathay-twd-history-missing",
        "cathay-twd-history-mode",
        "cathay-twd-history-period-control",
        "cathay-twd-history-query-control",
        "cathay-twd-history-response",
        "cathay-twd-history-seed-response",
        "cathay-twd-history-template",
        "cathay-twd-history-transaction-amount",
        "cathay-twd-history-transaction-date",
        "cathay-twd-history-transaction-range",
        "cathay-twd-history-transaction-text",
    })
    HISTORY_COVERAGE_REQUIRED: ClassVar[bool] = True
    HISTORY_COVERAGE_DOMAINS: ClassVar[frozenset[str]] = frozenset({
        "twd_transactions",
    })
    CREDENTIAL_HOSTS = frozenset({"www.cathaybk.com.tw"})

    def __init__(self):
        super().__init__(name="cathay")
        self.creds = CathayCreds.load()

    def _host_filter(self) -> str:
        return "cathaybk.com.tw"

    # ---------- positive login signal (W 2026-06-17) ----------
    def _logged_in(self, page, _failures=None) -> bool:
        """正向訊號：URL 在內銀區 + innerText >= 500 + 命中 >= 2 個菜單字 + 無 login form。

        取代原本只看 URL 含 OnlineBanking 的 negative-only 訊號，避免雲端
        fallback goto 失敗時誤判為已登入。詳見 SCSB 鐵律 wiki。
        """
        if _failures is not None:
            _failures.clear()
        try:
            current = urlparse(_native_step('CATHAY-03-010-001', lambda: page.url) or "")
            if (
                current.scheme.lower() != "https"
                or (current.hostname or "").lower() != "www.cathaybk.com.tw"
                or current.port not in (None, 443)
                or current.username is not None
                or current.password is not None
            ):
                return False
            r = _native_step('CATHAY-03-011-001', lambda: page.evaluate(JS_LOGGED_IN_POSITIVE))
        except Exception as error:
            if _failures is not None:
                _failures.append(error)
            return False
        if isinstance(r, dict):
            return bool(r.get("ok"))
        return bool(r)

    def login(self, page) -> bool:
        return self._shared_login(page)

    def _shared_login(self, page) -> bool:
        # Bounded to this invocation; never promote an observation over a known blocker.
        failures = []
        self._login_inspection_failures = failures
        try:
            return super()._shared_login(page)
        except LoginCheckpointTerminal as error:
            if failures and error.outcome.error_id in ("CATHAY-03-988-001", "CATHAY-03-988-002"):
                error.__cause__ = failures[0]
            raise
        finally:
            del self._login_inspection_failures


    def prepare_login_page(self, page) -> None:
        _native_step('CATHAY-01-010-001', lambda: page.wait_for_timeout(2500))

    def is_authenticated(self, page) -> bool:
        failures = getattr(self, "_login_inspection_failures", None)
        return self._logged_in(page) if failures is None else self._logged_in(page, failures)

    def login_checkpoint_rules(self) -> tuple[LoginCheckpointRule, ...]:
        post_login_notice = re.compile(
            r"^\s*重要通知\s+服務內容提醒\s*我知道了\s*$",
        )
        return (
            LoginCheckpointRule(
                name="cathay-login-announcement",
                bank="cathay",
                phases=(CheckpointPhase.PRE_SUBMIT,),
                kind=CheckpointKind.DISMISSIBLE_NOTICE,
                container_selector="#divSystemLoginMsgList.show",
                action_texts=("下一", "下一則", "我知道了", "關閉", "確定"),
                max_actions=12,
            ),
            LoginCheckpointRule(
                name="cathay-post-login-notice-dialog",
                bank="cathay",
                phases=(CheckpointPhase.POST_SUBMIT_SETTLE,),
                kind=CheckpointKind.DISMISSIBLE_NOTICE,
                container_selector="[role='dialog']",
                action_texts=("我知道了",),
                required_body_pattern=post_login_notice,
                max_actions=1,
                require_exclusive_action=True,
            ),
            # Live 2026-10-03: post-login CUBE announcement modal (e.g. 系統維護公告)
            # with only 不要再顯示 / 下次再提醒. Dismiss with 下次再提醒 (non-persistent).
            LoginCheckpointRule(
                name="cathay-post-login-announcement",
                bank="cathay",
                phases=(CheckpointPhase.POST_SUBMIT_SETTLE,),
                kind=CheckpointKind.DISMISSIBLE_NOTICE,
                container_selector="[role='dialog']",
                action_texts=("下次再提醒",),
                required_body_pattern=re.compile(
                    r"^\s*\S{0,12}公告\s.+\s不要再顯示\s+下次再提醒\s*$", re.S,
                ),
                max_actions=1,
            ),
            LoginCheckpointRule(
                name="cathay-unknown-modal",
                bank="cathay",
                phases=tuple(CheckpointPhase),
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector=".modal.show",
            ),
            LoginCheckpointRule(
                name="cathay-unknown-dialog",
                bank="cathay",
                phases=tuple(CheckpointPhase),
                kind=CheckpointKind.UNKNOWN_BLOCKER,
                container_selector="[role='dialog']",
            ),
        )

    def submit_credentials_once(self, page) -> None:
        try:
            with error_step('CATHAY-02-001-001'):
                page.wait_for_selector(SEL_CUSTID, state="visible", timeout=12000)
            for selector, value, codes in (
                (SEL_CUSTID, self.creds.cust_id, ('CATHAY-02-010-001', 'CATHAY-02-010-002', 'CATHAY-02-010-003', 'CATHAY-02-010-004', 'CATHAY-02-010-005', 'CATHAY-02-010-006', 'CATHAY-02-010-007', 'CATHAY-02-010-008', 'CATHAY-02-010-009', 'CATHAY-02-010-010', 'CATHAY-02-010-011', 'CATHAY-02-010-012', 'CATHAY-02-010-013', 'CATHAY-02-010-014', 'CATHAY-02-010-015')),
                (SEL_USERID, self.creds.user_id, ('CATHAY-02-020-001', 'CATHAY-02-020-002', 'CATHAY-02-020-003', 'CATHAY-02-020-004', 'CATHAY-02-020-005', 'CATHAY-02-020-006', 'CATHAY-02-020-007', 'CATHAY-02-020-008', 'CATHAY-02-020-009', 'CATHAY-02-020-010', 'CATHAY-02-020-011', 'CATHAY-02-020-012', 'CATHAY-02-020-013', 'CATHAY-02-020-014', 'CATHAY-02-020-015')),
                (SEL_PWD, self.creds.password, ('CATHAY-02-030-001', 'CATHAY-02-030-002', 'CATHAY-02-030-003', 'CATHAY-02-030-004', 'CATHAY-02-030-005', 'CATHAY-02-030-006', 'CATHAY-02-030-007', 'CATHAY-02-030-008', 'CATHAY-02-030-009', 'CATHAY-02-030-010', 'CATHAY-02-030-011', 'CATHAY-02-030-012', 'CATHAY-02-030-013', 'CATHAY-02-030-014', 'CATHAY-02-030-015')),
            ):
                with error_step(codes[0]):
                    fields = page.locator(selector)
                with error_step(codes[1]):
                    count = fields.count()
                if count != 1:
                    raise with_error_code(CathayLoginError('登入欄位無法安全填寫；未送出登入'), codes[2])
                with error_step(codes[3]):
                    field = fields.nth(0)
                with error_step(codes[4]):
                    visible = field.is_visible()
                if not visible:
                    raise with_error_code(CathayLoginError('登入欄位無法安全填寫；未送出登入'), codes[5])
                with error_step(codes[6]):
                    enabled = field.is_enabled()
                if not enabled:
                    raise with_error_code(CathayLoginError('登入欄位無法安全填寫；未送出登入'), codes[7])
                with error_step(codes[8]):
                    field.click()
                with error_step(codes[9]):
                    field.click(click_count=3)
                with error_step(codes[10]):
                    page.keyboard.press("Backspace")
                with error_step(codes[11]):
                    page.keyboard.type(value, delay=80)
                with error_step(codes[12]):
                    page.wait_for_timeout(200)
                with error_step(codes[13]):
                    actual_length = len(field.input_value())
                if actual_length != len(value):
                    raise with_error_code(CathayLoginError('登入欄位輸入長度不符；未送出登入'), codes[14])
        except Exception as error:
            raise with_error_code(
                CathayLoginError('登入欄位無法安全填寫；未送出登入'),
                exception_error_id(error, bank="cathay") or 'CATHAY-02-040-001',
            ) from None

        _click_login_once(page)
        _native_step('CATHAY-03-100-001', lambda: page.wait_for_timeout(9000))

    # ---------- 互動觸發：台幣交易明細 ----------
    def _seed_twd_query_templates(self, page, collector: ResponseCollector):
        """Use one native query per dropdown entry to obtain safe request templates."""
        _native_step('CATHAY-05-100-001', lambda: page.wait_for_timeout(3000))

        def pick(label, downs=1):
            handle = _native_step(_SEED_SELECT_STEPS[label][0], lambda: page.evaluate(
                "((label) => { const gs=[...document.querySelectorAll('.chakra-form-control,[role=group]')];"
                "const g=gs.find(x=>x.textContent.trim().startsWith(label)); if(!g) return 'no-group';"
                "const inp=g.querySelector('input'); if(!inp) return 'no-input'; inp.focus(); return 'focused'; })",
                label,
            ))
            if handle != "focused":
                return handle
            _native_step(_SEED_SELECT_STEPS[label][1], lambda: page.wait_for_timeout(400))
            for _ in range(downs):
                _native_step(_SEED_SELECT_STEPS[label][2], lambda: page.keyboard.press("ArrowDown"))
                _native_step(_SEED_SELECT_STEPS[label][3], lambda: page.wait_for_timeout(250))
            _native_step(_SEED_SELECT_STEPS[label][4], lambda: page.keyboard.press("Enter"))
            _native_step(_SEED_SELECT_STEPS[label][5], lambda: page.wait_for_timeout(800))
            return "ok"

        # 先數帳號下拉有幾個選項
        n_accts = _native_step('CATHAY-05-101-001', lambda: page.evaluate(
            "(() => { const gs=[...document.querySelectorAll('.chakra-form-control,[role=group]')];"
            "const g=gs.find(x=>x.textContent.trim().startsWith('帳號')); if(!g) return 0;"
            "const inp=g.querySelector('input'); if(inp) inp.focus(); "
            "return document.querySelectorAll('[id*=react-select][id*=option]').length || 1; })()",
        ))
        _native_step('CATHAY-05-102-001', lambda: page.keyboard.press("Escape"))
        _native_step('CATHAY-05-103-001', lambda: page.wait_for_timeout(300))
        n_accts = max(1, min(int(n_accts or 1), 10))
        _log(f"[twd_txn] 帳號選項數: {n_accts}")

        templates = []
        for idx in range(n_accts):
            picked = pick('帳號', downs=idx + 1)
            if picked != 'ok':
                raise with_error_code(RuntimeError("cathay-twd-history-account-control"), "CATHAY-05-010-005" if picked == "no-group" else "CATHAY-05-010-006" if picked == "no-input" else "CATHAY-05-010-001")
            picked = pick('查詢期間', downs=2)
            if picked != 'ok':
                raise with_error_code(RuntimeError("cathay-twd-history-period-control"), "CATHAY-05-010-007" if picked == "no-group" else "CATHAY-05-010-008" if picked == "no-input" else "CATHAY-05-010-002")
            before = len(collector.by_endpoint("B_ACCT_Q_TransferDetail"))
            clicked = _native_step('CATHAY-05-104-001', lambda: page.evaluate(
                "(() => { const b=[...document.querySelectorAll('button')].filter(x=>x.offsetParent!==null)"
                ".find(x=>x.textContent.trim()==='查詢'); if(b){b.click(); return true;} return false; })()",
            ))
            if clicked is not True:
                raise with_error_code(RuntimeError("cathay-twd-history-query-control"), "CATHAY-05-010-003")
            _log(f"[twd_txn] account_index={idx + 1}/{n_accts} seed query sent")
            _native_step('CATHAY-05-105-001', lambda: page.wait_for_timeout(6000))
            new_hits = collector.by_endpoint("B_ACCT_Q_TransferDetail")[before:]
            if len(new_hits) != 1:
                raise with_error_code(RuntimeError("cathay-twd-history-seed-response"), "CATHAY-05-010-004")
            self._twd_template_account(new_hits[0])
            templates.append(new_hits[0])
        return templates

    @staticmethod
    def _canonical_twd_identity(value, *, _codes=('CATHAY-04-020-001', 'CATHAY-04-020-002', 'CATHAY-04-020-003', 'CATHAY-04-021-001')) -> str:
        with error_step(_codes[3]):
            if not isinstance(value, str):
                raise with_error_code(RuntimeError("cathay-twd-history-account-inventory"), _codes[0])
            if not value:
                raise with_error_code(RuntimeError("cathay-twd-history-account-inventory"), _codes[1])
            if value != value.strip():
                raise with_error_code(RuntimeError("cathay-twd-history-account-inventory"), _codes[2])
            raw = value
            return raw.lstrip("0") or raw

    @classmethod
    def _twd_account_inventory(cls, collector: ResponseCollector) -> set[str]:
        with error_step("CATHAY-04-011-001"):
            hit = collector.latest("G_CUST_Q_TransAccountList")
            response = hit.resp_json if hit else None
            parsed = urlparse(hit.url) if hit else None
            request_content = (
                hit.req_body.get("content")
                if hit and isinstance(hit.req_body, dict)
                else None
            )
            if hit is None:
                raise with_error_code(RuntimeError("cathay-twd-history-account-inventory"), "CATHAY-04-010-001")
            if hit.method != 'POST':
                raise with_error_code(RuntimeError("cathay-twd-history-account-inventory"), "CATHAY-04-010-002")
            if type(hit.status) is not int:
                raise with_error_code(RuntimeError("cathay-twd-history-account-inventory"), "CATHAY-04-010-003")
            if not 200 <= hit.status < 300:
                raise with_error_code(RuntimeError("cathay-twd-history-account-inventory"), "CATHAY-04-010-004")
            if parsed is None:
                raise with_error_code(RuntimeError("cathay-twd-history-account-inventory"), "CATHAY-04-010-005")
            if parsed.scheme != 'https':
                raise with_error_code(RuntimeError("cathay-twd-history-account-inventory"), "CATHAY-04-010-006")
            if parsed.hostname != 'www.cathaybk.com.tw':
                raise with_error_code(RuntimeError("cathay-twd-history-account-inventory"), "CATHAY-04-010-007")
            if parsed.port not in (None, 443):
                raise with_error_code(RuntimeError("cathay-twd-history-account-inventory"), "CATHAY-04-010-008")
            if parsed.username is not None:
                raise with_error_code(RuntimeError("cathay-twd-history-account-inventory"), "CATHAY-04-010-009")
            if parsed.password is not None:
                raise with_error_code(RuntimeError("cathay-twd-history-account-inventory"), "CATHAY-04-010-010")
            if parsed.path != _ACCOUNT_INVENTORY_PATH:
                raise with_error_code(RuntimeError("cathay-twd-history-account-inventory"), "CATHAY-04-010-011")
            if not isinstance(request_content, dict):
                raise with_error_code(RuntimeError("cathay-twd-history-account-inventory"), "CATHAY-04-010-012")
            if request_content.get('queryType') != 'TWD':
                raise with_error_code(RuntimeError("cathay-twd-history-account-inventory"), "CATHAY-04-010-013")
            if not isinstance(response, dict):
                raise with_error_code(RuntimeError("cathay-twd-history-account-inventory"), "CATHAY-04-010-014")
            if not (response.get('success') is True or response.get('success') == 'true'):
                raise with_error_code(RuntimeError("cathay-twd-history-account-inventory"), "CATHAY-04-010-015")
            if response.get('returnCode') != '0000':
                raise with_error_code(RuntimeError("cathay-twd-history-account-inventory"), "CATHAY-04-010-016")
            content = response.get("content")
            rows = content.get("datas") if isinstance(content, dict) else None
            if not isinstance(rows, list):
                raise with_error_code(RuntimeError("cathay-twd-history-account-inventory"), "CATHAY-04-010-017")
            if any((not isinstance(row, dict) for row in rows)):
                raise with_error_code(RuntimeError("cathay-twd-history-account-inventory"), "CATHAY-04-010-018")
            currencies = [row.get("currency") for row in rows]
            for currency in currencies:
                if not isinstance(currency, str):
                    raise with_error_code(RuntimeError("cathay-twd-history-account-inventory"), "CATHAY-04-010-019")
                if not currency:
                    raise with_error_code(RuntimeError("cathay-twd-history-account-inventory"), "CATHAY-04-010-020")
                if currency != currency.strip():
                    raise with_error_code(RuntimeError("cathay-twd-history-account-inventory"), "CATHAY-04-010-021")
            twd_rows = [
                row for row, currency in zip(rows, currencies, strict=True)
                if currency.upper() == "TWD"
            ]
            identities = {
                cls._canonical_twd_identity(row.get("accountNo")) for row in twd_rows
            }
            if len(identities) != len(twd_rows):
                raise with_error_code(RuntimeError("cathay-twd-history-account-inventory"), "CATHAY-04-010-022")
            return identities

    @staticmethod
    def _history_floor(end: date) -> date:
        with error_step("CATHAY-05-051-001"):
            try:
                return end.replace(year=end.year - 1) + timedelta(days=1)
            except ValueError:  # February 29
                return end.replace(year=end.year - 1, day=28) + timedelta(days=1)

    @staticmethod
    def _history_windows(start: date, end: date) -> list[tuple[date, date]]:
        with error_step("CATHAY-05-052-001"):
            if start > end:
                raise with_error_code(RuntimeError("cathay-twd-history-invalid-range"), "CATHAY-05-050-001")
            windows = []
            cursor = start
            while cursor <= end:
                window_end = min(end, cursor + timedelta(days=29))
                windows.append((cursor, window_end))
                cursor = window_end + timedelta(days=1)
            return windows

    @classmethod
    def _twd_template_account(cls, hit) -> str:
        with error_step("CATHAY-05-021-001"):
            parsed = urlparse(hit.url)
            body = hit.req_body
            content = body.get("content") if isinstance(body, dict) else None
            filters = content.get("queryFilters") if isinstance(content, dict) else None
            if hit.method != 'POST':
                raise with_error_code(RuntimeError("cathay-twd-history-template"), "CATHAY-05-020-001")
            if type(hit.status) is not int:
                raise with_error_code(RuntimeError("cathay-twd-history-template"), "CATHAY-05-020-002")
            if not 200 <= hit.status < 300:
                raise with_error_code(RuntimeError("cathay-twd-history-template"), "CATHAY-05-020-003")
            if parsed.scheme != 'https':
                raise with_error_code(RuntimeError("cathay-twd-history-template"), "CATHAY-05-020-004")
            if parsed.hostname != 'www.cathaybk.com.tw':
                raise with_error_code(RuntimeError("cathay-twd-history-template"), "CATHAY-05-020-005")
            if parsed.port not in (None, 443):
                raise with_error_code(RuntimeError("cathay-twd-history-template"), "CATHAY-05-020-006")
            if parsed.username is not None:
                raise with_error_code(RuntimeError("cathay-twd-history-template"), "CATHAY-05-020-007")
            if parsed.password is not None:
                raise with_error_code(RuntimeError("cathay-twd-history-template"), "CATHAY-05-020-008")
            if parsed.path != _TWD_HISTORY_PATH:
                raise with_error_code(RuntimeError("cathay-twd-history-template"), "CATHAY-05-020-009")
            if not isinstance(filters, list):
                raise with_error_code(RuntimeError("cathay-twd-history-template"), "CATHAY-05-020-010")
            if len(filters) != 1:
                raise with_error_code(RuntimeError("cathay-twd-history-template"), "CATHAY-05-020-011")
            if not isinstance(filters[0], dict):
                raise with_error_code(RuntimeError("cathay-twd-history-template"), "CATHAY-05-020-012")
            if not isinstance(content, dict):
                raise with_error_code(RuntimeError("cathay-twd-history-template"), "CATHAY-05-020-013")
            if not isinstance(content.get('customerId'), str):
                raise with_error_code(RuntimeError("cathay-twd-history-template"), "CATHAY-05-020-014")
            if not content['customerId']:
                raise with_error_code(RuntimeError("cathay-twd-history-template"), "CATHAY-05-020-015")
            if content['customerId'] != content['customerId'].strip():
                raise with_error_code(RuntimeError("cathay-twd-history-template"), "CATHAY-05-020-016")
            cls._canonical_twd_identity(filters[0].get("accountNumber"), _codes=('CATHAY-05-022-001', 'CATHAY-05-022-002', 'CATHAY-05-022-003', 'CATHAY-05-022-004'))
            return str(filters[0]["accountNumber"])

    @classmethod
    def _validated_twd_account(
        cls,
        response: dict,
        raw_account: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> dict:
        with error_step("CATHAY-05-031-001"):
            if not isinstance(response, dict):
                raise with_error_code(RuntimeError("cathay-twd-history-response"), "CATHAY-05-030-001")
            if not (response.get('success') is True or response.get('success') == 'true'):
                raise with_error_code(RuntimeError("cathay-twd-history-response"), "CATHAY-05-030-002")
            if response.get('returnCode') != '0000':
                raise with_error_code(RuntimeError("cathay-twd-history-response"), "CATHAY-05-030-003")
            content = response.get("content")
            rows = content.get("datas") if isinstance(content, dict) else None
            if not isinstance(rows, list):
                raise with_error_code(RuntimeError("cathay-twd-history-response"), "CATHAY-05-030-004")
            if len(rows) != 1:
                raise with_error_code(RuntimeError("cathay-twd-history-response"), "CATHAY-05-030-005")
            if not isinstance(rows[0], dict):
                raise with_error_code(RuntimeError("cathay-twd-history-response"), "CATHAY-05-030-006")
            account = rows[0]
            query_status = account.get("queryStatus")
            if type(query_status) is not str:
                raise with_error_code(RuntimeError("cathay-twd-history-account-mismatch"), "CATHAY-05-030-007")
            if query_status not in {'SUCCESS', 'Success', 'NoData'}:
                raise with_error_code(RuntimeError("cathay-twd-history-account-mismatch"), "CATHAY-05-030-008")
            if query_status == 'NoData':
                if start is None:
                    raise with_error_code(RuntimeError("cathay-twd-history-account-mismatch"), "CATHAY-05-030-009")
                if end is None:
                    raise with_error_code(RuntimeError("cathay-twd-history-account-mismatch"), "CATHAY-05-030-010")
            if account.get('accountNumber') != raw_account:
                raise with_error_code(RuntimeError("cathay-twd-history-account-mismatch"), "CATHAY-05-030-011")
            rejection = []
            if start is not None and (not cls._twd_response_date_matches(account.get('startDate'), start, _rejection=rejection, _codes=('CATHAY-05-830-001', 'CATHAY-05-830-002', 'CATHAY-05-830-003'))):
                raise with_error_code(RuntimeError("cathay-twd-history-account-mismatch"), rejection[0] if rejection else "CATHAY-05-030-012")
            rejection = []
            if end is not None and (not cls._twd_response_date_matches(account.get('endDate'), end, _rejection=rejection, _codes=('CATHAY-05-831-001', 'CATHAY-05-831-002', 'CATHAY-05-831-003'))):
                raise with_error_code(RuntimeError("cathay-twd-history-account-mismatch"), rejection[0] if rejection else "CATHAY-05-030-013")
            details = account.get("details")
            raw_count = account.get("count")
            if type(raw_count) is not int:
                raise with_error_code(RuntimeError("cathay-twd-history-count"), "CATHAY-05-030-014")
            if raw_count < 0:
                raise with_error_code(RuntimeError("cathay-twd-history-count"), "CATHAY-05-030-015")
            count = raw_count
            if not isinstance(details, list):
                raise with_error_code(RuntimeError("cathay-twd-history-count"), "CATHAY-05-030-016")
            if any((not isinstance(row, dict) for row in details)):
                raise with_error_code(RuntimeError("cathay-twd-history-count"), "CATHAY-05-030-017")
            if count != len(details):
                raise with_error_code(RuntimeError("cathay-twd-history-count"), "CATHAY-05-030-018")
            if query_status == 'NoData' and count != 0:
                raise with_error_code(RuntimeError("cathay-twd-history-count"), "CATHAY-05-030-019")
            if start is not None and end is not None:
                for row in details:
                    try:
                        cls._normalize_twd_datetime(
                            row.get("txnDateTime"),
                        )
                    except ValueError as error:
                        raise with_error_code(
                            RuntimeError("cathay-twd-history-transaction-date"),
                            exception_error_id(error, bank="cathay") or "CATHAY-05-030-020",
                        )
                    account_date = row.get("accountDate")
                    if account_date is not None:
                        rejection = []
                        parsed_account_date = cls._twd_response_date(account_date, _rejection=rejection, _codes=('CATHAY-05-832-001', 'CATHAY-05-832-002', 'CATHAY-05-832-003'))
                        if parsed_account_date is None:
                            raise with_error_code(RuntimeError("cathay-twd-history-transaction-date"), rejection[0] if rejection else "CATHAY-05-030-021")
                    for field, code in (('description', 'CATHAY-05-810-001'), ('expendBankId', 'CATHAY-05-811-001'), ('expendAcctNo', 'CATHAY-05-812-001'), ('memo', 'CATHAY-05-813-001')):
                        value = row.get(field)
                        if value is not None and (not isinstance(value, str)):
                            raise with_error_code(RuntimeError("cathay-twd-history-transaction-text"), code)
                    if row.get('expendAmt') is None and row.get('incomeAmt') is None:
                        raise with_error_code(RuntimeError("cathay-twd-history-transaction-amount"), "CATHAY-05-030-023")
                    for field in ("expendAmt", "incomeAmt", "balance"):
                        value = row.get(field)
                        if value is None:
                            continue
                        try:
                            cls._normalize_twd_amount(
                                value, non_negative=field != "balance", _codes=_AMOUNT_FIELD_CODES[field],
                            )
                        except ValueError as error:
                            raise with_error_code(
                                RuntimeError("cathay-twd-history-transaction-amount"),
                                exception_error_id(error, bank="cathay") or "CATHAY-05-030-024",
                            )
            return account

    @staticmethod
    def _normalize_twd_amount(value, *, non_negative: bool = False, _codes=('CATHAY-07-010-001', 'CATHAY-07-010-002', 'CATHAY-07-010-003', 'CATHAY-07-010-004', 'CATHAY-07-010-005', 'CATHAY-07-010-006', 'CATHAY-07-010-007', 'CATHAY-07-011-001')):
        with error_step(_codes[7]):
            if isinstance(value, bool):
                raise with_error_code(ValueError("invalid amount"), _codes[0])
            raw = str(value).strip()
            if not _TWD_AMOUNT_RE.fullmatch(raw):
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

    @staticmethod
    def _twd_response_date(value: object, *, _rejection=None, _codes=()) -> date | None:
        with error_step("CATHAY-07-021-001"):
            if _rejection is not None:
                _rejection.clear()
            if not isinstance(value, str):
                if _rejection is not None:
                    _rejection.append(_codes[0])
                return None
            if not (
                re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
                or _TWD_DATETIME_RE.fullmatch(value)
            ):
                if _rejection is not None:
                    _rejection.append(_codes[1])
                return None
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
            except ValueError:
                if _rejection is not None:
                    _rejection.append(_codes[2])
                return None

    @classmethod
    def _twd_response_date_matches(cls, value: object, expected: date, *, _rejection=None, _codes=()) -> bool:
        with error_step("CATHAY-05-032-001"):
            return cls._twd_response_date(value, _rejection=_rejection, _codes=_codes) == expected

    @staticmethod
    def _normalize_twd_datetime(value) -> str:
        with error_step("CATHAY-07-022-001"):
            if not isinstance(value, str):
                raise with_error_code(ValueError("invalid datetime"), "CATHAY-07-020-001")
            normalized = value.strip()
            if not _TWD_DATETIME_RE.fullmatch(normalized):
                raise with_error_code(ValueError("invalid datetime"), "CATHAY-07-020-002")
            try:
                datetime.fromisoformat(normalized.replace("Z", "+00:00"))
            except ValueError:
                raise with_error_code(ValueError("invalid datetime"), "CATHAY-07-020-003") from None
            return normalized

    @staticmethod
    def _twd_fetch_script() -> str:
        return """async (payload) => {
          const headers = {'Content-Type': 'application/json'};
          if (payload.authorization) headers.Authorization = payload.authorization;
          const controller = new AbortController();
          const timeoutId = setTimeout(() => controller.abort(), 30000);
          try {
            const response = await fetch(payload.url, {
              method: 'POST', credentials: 'include', headers,
              redirect: 'error', signal: controller.signal,
              body: JSON.stringify(payload.body),
            });
            let json = null;
            let jsonParseFailed = false;
            try { json = await response.json(); } catch (_) { jsonParseFailed = true; }
            return {
              status: response.status,
              url: response.url,
              redirected: response.redirected,
              json, jsonParseFailed,
            };
          } finally {
            clearTimeout(timeoutId);
          }
        }"""

    def _collect_twd_history(
        self,
        page,
        collector: ResponseCollector,
        *,
        as_of: date | None = None,
        expected_identities: set[str] | None = None,
    ) -> dict:
        end = as_of or date.today()
        floor = self._history_floor(end)
        mode = os.environ.get("BANK_CRAWLER_HISTORY_MODE", "full")
        if mode not in {'full', 'incremental'}:
            raise with_error_code(RuntimeError("cathay-twd-history-mode"), "CATHAY-05-040-001")
        if expected_identities == set():
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

        templates = self._seed_twd_query_templates(page, collector)
        template_identities = [
            self._canonical_twd_identity(self._twd_template_account(hit))
            for hit in templates
        ]
        if len(template_identities) != len(set(template_identities)):
            raise with_error_code(RuntimeError("cathay-twd-history-account-inventory"), "CATHAY-05-040-002")
        if expected_identities is not None and set(template_identities) != expected_identities:
            raise with_error_code(RuntimeError("cathay-twd-history-account-inventory"), "CATHAY-05-040-003")

        accounts = []
        expected = []
        receipts = []
        for hit, identity in zip(templates, template_identities):
            raw_account = self._twd_template_account(hit)
            start = self.transaction_window_start(identity, floor=floor)
            transactions = []
            for window_start, window_end in self._history_windows(start, end):
                body = copy.deepcopy(hit.req_body)
                query = body["content"]["queryFilters"][0]
                query.update({
                    "accountNumber": raw_account,
                    "startDate": window_start.isoformat(),
                    "endDate": window_end.isoformat(),
                })
                fetched = _native_step('CATHAY-05-200-001', lambda: page.evaluate(self._twd_fetch_script(), {
                    "url": hit.url,
                    "authorization": collector.auth_token,
                    "body": body,
                }))
                status = fetched.get("status") if isinstance(fetched, dict) else None
                if type(status) is not int:
                    raise with_error_code(RuntimeError("cathay-twd-history-fetch"), "CATHAY-05-040-004")
                if not 200 <= status < 300:
                    raise with_error_code(RuntimeError("cathay-twd-history-fetch"), "CATHAY-05-040-005")
                if fetched.get('redirected') is not False:
                    raise with_error_code(RuntimeError("cathay-twd-history-fetch"), "CATHAY-05-040-006")
                if fetched.get('url') != hit.url:
                    raise with_error_code(RuntimeError("cathay-twd-history-fetch"), "CATHAY-05-040-007")
                response = fetched.get("json")
                if not isinstance(response, dict):
                    raise with_error_code(RuntimeError("cathay-twd-history-response"), "CATHAY-05-040-009" if fetched.get("jsonParseFailed") is True else "CATHAY-05-040-008")
                account = self._validated_twd_account(
                    response, raw_account, start=window_start, end=window_end,
                )
                details = account["details"]
                transactions.extend(self._normalize_twd_transactions(details))
                receipt = {
                    "identity": identity,
                    "start": window_start.isoformat(),
                    "end": window_end.isoformat(),
                    "status": "complete" if details else "explicit_empty",
                    "pages": 1,
                }
                receipts.append(receipt)
            accounts.append({
                "account": raw_account,
                "count": len(transactions),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "transactions": transactions,
            })
            expected.append({
                "identity": identity,
                "start": start.isoformat(),
                "end": end.isoformat(),
            })
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

    # ---------- 互動觸發：信用卡逐筆明細 ----------
    def _collect_card_details(self, page):
        """從 C0101 點三個明細連結，分別觸發：
        即時消費(C_BILL_Q_CardCurrentConsume) / 未出帳(C_BILL_Q_CardUnbilledConsume)
        / 已出帳逐筆(C_BILL_Q_RecentBillDetail)。
        """
        def click_link(text):
            return _native_step(_CARD_DETAIL_STEPS[text][5], lambda: page.evaluate(
                "((t) => { const els=[...document.querySelectorAll('a,button,[role=button]')]"
                ".filter(b=>b.offsetParent!==null);"
                "const el=els.find(b=>b.textContent.trim().startsWith(t)||b.textContent.trim().includes(t));"
                "if(el){el.scrollIntoView(); el.click(); return el.textContent.trim().slice(0,20);} return null; })", text,
            ))

        for label in ["即時消費明細", "未出帳明細", "帳單明細"]:
            try:
                _native_step(_CARD_DETAIL_STEPS[label][0], lambda: page.goto(f"{BASE}/OnlineBanking/CQuery/C0101_BillOverview",
                          wait_until="domcontentloaded", timeout=25000))
                _native_step(_CARD_DETAIL_STEPS[label][1], lambda: page.wait_for_timeout(5000))
                clicked = click_link(label)
                _log(f"[card_detail] 點 {label!r}: {clicked}")
                _native_step(_CARD_DETAIL_STEPS[label][2], lambda: page.wait_for_timeout(6500))
                # 已出帳頁可能要再點查詢
                _native_step(_CARD_DETAIL_STEPS[label][3], lambda: page.evaluate(
                    "(() => { const e=[...document.querySelectorAll('button')].filter(b=>b.offsetParent!==null)"
                    ".find(b=>/查詢|確定/.test(b.textContent)); if(e) e.click(); })()",
                ))
                _native_step(_CARD_DETAIL_STEPS[label][4], lambda: page.wait_for_timeout(3500))
            except Exception as e:
                _log(f"[card_detail] {label} 失敗: {str(e)[:80]}")

    # ---------- 主抓取 ----------
    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        # 逐頁造訪，讓 React 自動打 API
        twd_history = None
        inventory_navigation_error = None
        for key, path in FEATURE_PAGES.items():
            stage_navigation_error = None
            _log(f"\n[collect] {key}: {path}")
            try:
                _native_step(_FEATURE_STEPS[key][0], lambda: page.goto(f"{BASE}{path}", wait_until="domcontentloaded", timeout=25000))
            except Exception as e:
                stage_navigation_error = e
                if key == "asset":
                    inventory_navigation_error = e
                _log(f"  [warn] goto: {str(e)[:80]}")
            _native_step(_FEATURE_STEPS[key][1], lambda: page.wait_for_timeout(6500))
            if key == "twd_txn":
                try:
                    identities = self._twd_account_inventory(collector)
                except Exception as error:
                    if inventory_navigation_error is not None and exception_error_id(error, bank="cathay") == "CATHAY-04-010-001":
                        error.__cause__ = inventory_navigation_error
                    raise
                inventory_navigation_error = None
                try:
                    twd_history = self._collect_twd_history(
                        page,
                        collector,
                        expected_identities=identities,
                    )
                except Exception as error:
                    if stage_navigation_error is not None and exception_error_id(error, bank="cathay") in (
                        "CATHAY-05-010-001", "CATHAY-05-010-002", "CATHAY-05-010-005",
                        "CATHAY-05-010-006", "CATHAY-05-010-007", "CATHAY-05-010-008",
                    ):
                        error.__cause__ = stage_navigation_error
                    raise
        # 信用卡逐筆明細（三種）
        self._collect_card_details(page)

        out = self._parse(collector)
        if twd_history is None:
            raise with_error_code(RuntimeError("cathay-twd-history-missing"), "CATHAY-07-100-001")
        out["twd_transactions"] = twd_history["accounts"]
        out["history_coverage"] = twd_history["coverage"]
        with error_step("CATHAY-07-070-001"):
            return BankCollectResult(**out)

    # ---------- Parse ----------
    def _c(self, hit, *, _code='CATHAY-07-040-001'):
        """取 hit 的 content（成功才回）。"""
        with error_step(_code):
            if not hit or not hit.resp_json:
                return None
            j = hit.resp_json
            if isinstance(j, dict) and j.get("success"):
                return j.get("content")
            return j.get("content") if isinstance(j, dict) else None

    @staticmethod
    def _normalize_iso_date(value):
        """國泰 API 常回 '2025-07-01T00:00:00' / '2025/7/1' → 'YYYY-MM-DD'。

        `BankCollectResult` 的 balance/twd/card date field 必須是 ISO date；
        crawler 這裡先正規化好，避免整個 collect 被 contract 拒收。無法解析
        的字串保持原值，讓 contract 拋出可追蹤的錯誤。
        """
        with error_step("CATHAY-07-041-001"):
            import re as _re
            if value is None or value == "":
                return None
            text = str(value).strip()
            if not text:
                return None
            m = _re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
            if not m:
                return text
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    def _normalize_balance_row(self, row: dict) -> dict:
        """把國泰 balance snapshot row 的 snapshotDate 正規化成 YYYY-MM-DD。"""
        with error_step("CATHAY-07-042-001"):
            if not isinstance(row, dict):
                return row
            normalized = dict(row)
            for key in ("snapshotDate", "date", "snapshot_date"):
                if key in normalized:
                    normalized[key] = self._normalize_iso_date(normalized[key])
            return normalized

    def _normalize_twd_transactions(self, details: list[dict]) -> list[dict]:
        with error_step("CATHAY-07-043-001"):
            return [
                {
                    "datetime": self._normalize_twd_datetime(t.get("txnDateTime")),
                    "account_date": (
                        parsed.isoformat()
                        if (parsed := self._twd_response_date(t.get("accountDate"))) is not None
                        else t.get("accountDate")
                    ),
                    "desc": t.get("description"),
                    "expend": (
                        self._normalize_twd_amount(t["expendAmt"], non_negative=True, _codes=_AMOUNT_FIELD_CODES['expendAmt'])
                        if t.get("expendAmt") is not None else None
                    ),
                    "income": (
                        self._normalize_twd_amount(t["incomeAmt"], non_negative=True, _codes=_AMOUNT_FIELD_CODES['incomeAmt'])
                        if t.get("incomeAmt") is not None else None
                    ),
                    "balance": (
                        self._normalize_twd_amount(t["balance"], _codes=_AMOUNT_FIELD_CODES['balance'])
                        if t.get("balance") is not None else None
                    ),
                    "counterparty_bank": t.get("expendBankId"),
                    "counterparty_acct": (
                        self.mask_card(str(t.get("expendAcctNo")))
                        if t.get("expendAcctNo") else None
                    ),
                    "memo": t.get("memo"),
                }
                for t in details
                if isinstance(t, dict)
            ]

    def _parse(self, col: ResponseCollector) -> dict:
        with error_step("CATHAY-07-400-001"):
            out: dict = {}

        # 帳戶清單
        with error_step("CATHAY-07-401-001"):
            acc = self._c(col.latest("G_CUST_Q_TransAccountList"), _code="CATHAY-07-401-001")
        with error_step("CATHAY-07-402-001"):
            if acc and isinstance(acc, dict):
                out["accounts"] = [
                    {
                        "currency": d.get("currency"),
                        "account_no": d.get("accountNo"),
                        "branch": d.get("branchName"),
                        "nickname": d.get("nickName"),
                        "type": d.get("accountType"),
                        "product_type": d.get("productType"),
                    }
                    for d in acc.get("datas", [])
                ]

        # 餘額走勢（台幣/外幣）— snapshotDate 需正規化成 IsoDate YYYY-MM-DD 才通過
        # BankCollectResult contract。國泰 raw 是 '2025-07-01T00:00:00' ISO datetime，
        # 若直接放行，`base.py::_require_iso_date` 會 raise → 整個 collect fail
        # → 拖垮整包 cathay sync (2026-07-03 regression, see wiki
        # collector-vs-persist-schema-gatekeeper-layer-rule)。
        with error_step("CATHAY-07-403-001"):
            bl = self._c(col.latest("G_CUST_Q_BalanceLevel"), _code="CATHAY-07-403-001")
        with error_step("CATHAY-07-404-001"):
            if bl and isinstance(bl, dict):
                lst = bl.get("dateBalanceList", []) or []
                normalized = [self._normalize_balance_row(r) for r in lst if isinstance(r, dict)]
                if normalized:
                    out["balance_latest"] = normalized[-1]
                    out["balance_history"] = normalized

        # 各類現值
        with error_step("CATHAY-07-405-001"):
            for label, ep, keys, code in [
                ("foreign", "G_CUST_Q_ForeignNetPresentInfo", ["hasAccount", "twdBalance"], "CATHAY-07-450-001"),
                ("invest", "G_CUST_Q_InvestNetPresentInfo", ["totalRateOfReturn", "totalPresentValue"], "CATHAY-07-451-001"),
                ("insurance", "G_CUST_Q_InsNetPresentInfo", ["isRegistered", "sumAssets"], "CATHAY-07-452-001"),
                ("loan", "G_CUST_Q_LoanNetPresentInfo", ["netPresent"], "CATHAY-07-453-001"),
            ]:
                c = self._c(col.latest(ep), _code=code)
                if c and isinstance(c, dict):
                    out.setdefault("net_present", {})[label] = {k: c.get(k) for k in keys}

        # 信用卡
        with error_step("CATHAY-07-406-001"):
            card: dict = {}
        with error_step("CATHAY-07-407-001"):
            cl = self._c(col.latest("C_CardInfo_Q_MyCardList"), _code="CATHAY-07-407-001")
        with error_step("CATHAY-07-408-001"):
            if cl and isinstance(cl, dict):
                card["cards"] = [
                    {
                        "name": r.get("cardName"),
                        "number": self.mask_card(r.get("cardNumber")),
                        "association": r.get("cardAssociation"),
                        "type": r.get("cardType"),
                        "is_cube": r.get("isCubeCard"),
                    }
                    for r in cl.get("records", [])
                ]
        with error_step("CATHAY-07-409-001"):
            lb = self._c(col.latest("C_CardInfo_Q_LatestBill"), _code="CATHAY-07-409-001")
        with error_step("CATHAY-07-410-001"):
            if lb and isinstance(lb, dict):
                twd = lb.get("twdBillDetail")
                if isinstance(twd, dict) and "payBillStatus" in twd:
                    twd = dict(twd)
                    normalized_status = cathay_bill_status(
                        twd["payBillStatus"], strict=True,
                    )
                    assert normalized_status is not None
                    twd["payBillStatus"] = normalized_status.value
                card["latest_bill"] = {
                    "due_date": lb.get("dueDate"),
                    "due_days": lb.get("dueDays"),
                    "twd": twd,
                    "usd": lb.get("usdBillDetail"),
                }
        with error_step("CATHAY-07-411-001"):
            nb = self._c(col.latest("C_CardInfo_Q_NextBill"), _code="CATHAY-07-411-001")
        with error_step("CATHAY-07-412-001"):
            if nb and isinstance(nb, dict):
                card["next_bill"] = {
                    "twd": nb.get("twdNextBillInfo"),
                    "usd": nb.get("usdNextBillInfo"),
                }
        with error_step("CATHAY-07-413-001"):
            cq = self._c(col.latest("C_CardInfo_Q_CardQuota"), _code="CATHAY-07-413-001")
        with error_step("CATHAY-07-414-001"):
            if cq and isinstance(cq, dict):
                card["quota"] = {
                    "available": cq.get("realTimeAvailAmount"),
                    "credit_limit": cq.get("creditLimitAmount"),
                    "current": cq.get("currentAmount"),
                }
        with error_step("CATHAY-07-415-001"):
            cr = self._c(col.latest("C_CardInfo_Q_CardReward"), _code="CATHAY-07-415-001")
        with error_step("CATHAY-07-416-001"):
            if cr and isinstance(cr, dict):
                card["reward_points"] = cr.get("pointCurrent")
        with error_step("CATHAY-07-417-001"):
            tc = self._c(col.latest("G_CUST_Q_CardTotalConsumption"), _code="CATHAY-07-417-001")
        with error_step("CATHAY-07-418-001"):
            if tc and isinstance(tc, dict):
                card["total_consumption"] = {
                    "current_balance": tc.get("currentBalance"),
                    "unpaid": tc.get("unpaidAmount"),
                    "last_stmt_date": tc.get("lastStmtDate"),
                }
        with error_step("CATHAY-07-419-001"):
            if card:
                out["credit_card"] = card

        # 投資
        with error_step("CATHAY-07-420-001"):
            inv = self._c(col.latest("E_INFO_Q_InvestOverview"), _code="CATHAY-07-420-001")
        with error_step("CATHAY-07-421-001"):
            if inv and isinstance(inv, dict):
                ov = inv.get("investOverview", {})
                out["investment"] = {
                    "total_capital": ov.get("totalTrustCapital"),
                    "total_present_value": ov.get("totalPresentValue"),
                    "total_profit": ov.get("totalProfit"),
                    "total_roi": ov.get("totalROI"),
                    "products": [
                        {
                            "type": p.get("productType"),
                            "name": p.get("productName"),
                            "capital": p.get("trustCapital"),
                            "present_value": p.get("presentValue"),
                            "profit": p.get("profit"),
                        }
                        for p in ov.get("productRecords", [])
                    ],
                    "categories": ov.get("productCategoryRecords"),
                }
        with error_step("CATHAY-07-422-001"):
            fund = self._c(col.latest("F_SET_Q_ProductAdvanceDetail"), _code="CATHAY-07-422-001")
        with error_step("CATHAY-07-423-001"):
            if fund and isinstance(fund, dict):
                out.setdefault("investment", {})["funds"] = [
                    {
                        "fund_id": d.get("fundId"),
                        "group": d.get("groupDesc"),
                        "currency": d.get("currencyId"),
                        "networth": d.get("networth"),
                        "return_1y": d.get("fMreturnD_AR1Y"),
                    }
                    for d in fund.get("datas", [])
                ]

        # 保險
        with error_step("CATHAY-07-424-001"):
            ins: dict = {}
        with error_step("CATHAY-07-425-001"):
            ia = self._c(col.latest("I_POLICY_Q_CathayLifeAssetInfo"), _code="CATHAY-07-425-001")
        with error_step("CATHAY-07-426-001"):
            if ia and isinstance(ia, dict):
                ins["life_asset"] = {
                    "sum_asset": ia.get("sumAsset"),
                    "invest_cost": ia.get("totalInvestCost"),
                    "invest_pnl": ia.get("totalInvestPnl"),
                    "invest_roi": ia.get("totalInvestRoi"),
                }
        with error_step("CATHAY-07-427-001"):
            ip = self._c(col.latest("I_POLICY_Q_CathayInsurancePolicy"), _code="CATHAY-07-427-001")
        with error_step("CATHAY-07-428-001"):
            if ip and isinstance(ip, dict):
                ins["policies"] = [
                    {
                        "product": p.get("productName"),
                        "policy_no": p.get("policyNumber"),
                        "status": p.get("policyStatus"),
                        "start": p.get("startDate"),
                        "end": p.get("endDate"),
                        "premium": p.get("discountPremium"),
                    }
                    for p in ip.get("datas", [])
                ]
        with error_step("CATHAY-07-429-001"):
            if ins:
                out["insurance"] = ins

        # 貸款
        with error_step("CATHAY-07-430-001"):
            lo = self._c(col.latest("L_ACCT_Q_OverView"), _code="CATHAY-07-430-001")
        with error_step("CATHAY-07-431-001"):
            if lo and isinstance(lo, dict):
                out["loan"] = {"status": lo.get("status"), "accounts": lo.get("accountList")}

        # ===== 信用卡逐筆明細（三種）=====
        with error_step("CATHAY-07-432-001"):
            card_tx = out.setdefault("credit_card", {})

        # 已出帳逐筆明細
        with error_step("CATHAY-07-433-001"):
            rbd = self._c(col.latest("C_BILL_Q_RecentBillDetail"), _code="CATHAY-07-433-001")
        with error_step("CATHAY-07-434-001"):
            if rbd and isinstance(rbd, dict):
                card_tx["billed_detail"] = self._parse_bill_detail(rbd)

        # 未出帳逐筆消費
        with error_step("CATHAY-07-435-001"):
            unb = self._c(col.latest("C_BILL_Q_CardUnbilledConsume"), _code="CATHAY-07-435-001")
        with error_step("CATHAY-07-436-001"):
            if unb and isinstance(unb, dict):
                card_tx["unbilled_detail"] = self._parse_consume(unb, _codes=('CATHAY-06-350-001', 'CATHAY-06-350-002', 'CATHAY-06-350-003'))

        # 即時消費明細
        with error_step("CATHAY-07-437-001"):
            cur = self._c(col.latest("C_BILL_Q_CardCurrentConsume"), _code="CATHAY-07-437-001")
        with error_step("CATHAY-07-438-001"):
            if cur and isinstance(cur, dict):
                card_tx["current_detail"] = self._parse_consume(cur, _codes=('CATHAY-06-360-001', 'CATHAY-06-360-002', 'CATHAY-06-360-003'))

        # 帳單彙總（含繳款期限、額度循環）
        with error_step("CATHAY-07-439-001"):
            bi = self._c(col.latest("C_BILL_Q_BillInfo"), _code="CATHAY-07-439-001")
        with error_step("CATHAY-07-440-001"):
            if bi and isinstance(bi, dict):
                card_tx["bill_summary"] = {
                    "payment_deadline": bi.get("paymentDeadline"),
                    "currencies": bi.get("currencyBillInfoList"),
                }

        # ===== 台幣活存交易明細（B_ACCT_Q_TransferDetail）=====
        # 互動查詢後攔到，可能多帳號各一筆 hit，全部蒐集
        with error_step("CATHAY-07-441-001"):
            twd_txns = []
        with error_step("CATHAY-07-442-001"):
            for h in col.hits:
                if h.endpoint != "B_ACCT_Q_TransferDetail" or not h.resp_json:
                    continue
                c = h.resp_json.get("content") if isinstance(h.resp_json, dict) else None
                if not isinstance(c, dict):
                    continue
                for acct in c.get("datas", []):
                    details = acct.get("details") or []
                    twd_txns.append({
                        "account": acct.get("accountNumber"),
                        "count": acct.get("count"),
                        "start": acct.get("startDate"),
                        "end": acct.get("endDate"),
                        "transactions": self._normalize_twd_transactions(details),
                    })
        with error_step("CATHAY-07-443-001"):
            if twd_txns:
                out["twd_transactions"] = twd_txns

        with error_step("CATHAY-07-444-001"):
            publish_card_bill_facts(out, [_cathay_card_bill_fact(out)])

        # 全部攔到的 endpoint 清單（debug 用）
        with error_step("CATHAY-07-445-001"):
            out["_all_endpoints"] = sorted({h.endpoint for h in col.hits if h.resp_json})
        with error_step("CATHAY-07-446-001"):
            return out

    # ---------- 信用卡明細 parser ----------
    def _parse_bill_detail(self, content: dict) -> dict:
        """C_BILL_Q_RecentBillDetail: twd/usd/cny BillDetailInfo[].tradeData[]"""
        with error_step("CATHAY-06-300-001"):
            result = {}
            for cur_key, label in [("twdBillDetailInfo", "TWD"), ("usdBillDetailInfo", "USD"), ("cnyBillDetailInfo", "CNY")]:
                info = content.get(cur_key)
                if not isinstance(info, list):
                    continue
                txns = []
                for block in info:
                    for t in (block.get("tradeData") or []):
                        txns.append(self._norm_card_txn(t, _code='CATHAY-06-340-001'))
                if txns:
                    result[label] = txns
            return result

    def _parse_consume(self, content: dict, *, _codes=('CATHAY-06-310-001', 'CATHAY-06-320-001', 'CATHAY-06-330-001')) -> dict:
        """未出帳/即時消費：結構類似，找含 tradeData 或交易列的欄位。

        2026-06-22 Bug 5 修：filter NULL placeholder row（amount=None 且 desc 全空）。
        國泰即時消費 API（C_BILL_Q_CardCurrentConsume）會在 list 開頭塞一個空殼
        placeholder row（猜：UI 預留位 / 「目前無未出帳」狀態 marker），原本 collector
        看到 `amount` key 在第一個 dict 就把整 list 全收 → 寫進 card_pending_txns
        → 前端 amount or 0 顯示 0 元假交易。

        物理 invariant: 真實刷卡至少要有「金額」或「描述」其中之一。
        amount 全 NULL 且 desc 全空的 row 一定是 placeholder。

        詳見 wiki [[card-billed-pending-cross-table-consistency-lesson]] Bug 5。
        """
        with error_step(_codes[0]):
            result = {}
            # 常見容器名
            for k, v in content.items():
                if isinstance(v, list) and not v and "consume" in k.lower():
                    # 明確成功空清單也要保留 key，persist 才能區分零筆與 error dict。
                    result[k] = []
                    continue
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    # 若元素本身就是交易
                    if any(kk in v[0] for kk in ["transDesc", "amount", "consumeDate"]):
                        result[k] = [self._norm_card_txn(t, _code=_codes[2]) for t in v
                                     if not self._is_placeholder_consume_row(t, _code=_codes[1])]
                    # 若元素含 tradeData
                    elif "tradeData" in v[0]:
                        txns = []
                        for block in v:
                            for t in (block.get("tradeData") or []):
                                if self._is_placeholder_consume_row(t, _code=_codes[1]):
                                    continue
                                txns.append(self._norm_card_txn(t, _code=_codes[2]))
                        if txns:
                            result[k] = txns
            return result

    @staticmethod
    def _is_placeholder_consume_row(t: dict, *, _code='CATHAY-06-320-001') -> bool:
        """raw consume row 是 NULL placeholder（amount 全空 + desc 全空）→ filter。

        2026-06-22 Bug 5: cathay current/unbilled API 開頭塞空殼 row。
        判定條件：amount key 不存在或 None，且 desc/transDesc 不存在或全空白字串。

        注意 amount 用 None 判，不用 falsy（0 是合法 — refund / 紅利折抵）。
        """
        with error_step(_code):
            amt = t.get("amount") if "amount" in t else None
            if amt is None:
                amt = t.get("transAmount")
            if amt is None:
                amt = t.get("twdAmount")
            desc = t.get("desc") or t.get("transDesc") or t.get("merchantName") or t.get("description") or ""
            return amt is None and not str(desc).strip()

    def _norm_card_txn(self, t: dict, *, _code='CATHAY-06-330-001') -> dict:
        """正規化單筆信用卡 billed 交易。

        2026-06-13 修：consume_currency 空字串 → 'TWD' (避免 DB 髒資料)。
        Cathay 帳單 API 對台幣消費根本沒給 consumeCurrency 欄,
        或給空字串 ''——必須正規化才不會污染跨銀行外幣 query。

        規則:
          • consumeCurrency 非空 + consumeAmount 非零 → 視為外幣，原值保留
          • 否則 → consume_currency='TWD', consume_amount=None
          • consumeCountry='' → None
        """
        with error_step(_code):
            raw_cc = (t.get("consumeCurrency") or "").strip()
            raw_ca = t.get("consumeAmount")
            # 判斷是否真正外幣：currency 非空且不是 TWD，且金額非零
            is_foreign = bool(raw_cc) and raw_cc != "TWD" and raw_ca not in (None, 0, "0", "")
            if is_foreign:
                consume_currency = raw_cc
                consume_amount = raw_ca
            else:
                consume_currency = "TWD"
                consume_amount = None

            raw_country = t.get("consumeCountry")
            consume_country = raw_country if (raw_country and str(raw_country).strip()) else None

            posting = self._twd_response_date(t.get("postingDate"))
            post_date = posting.isoformat() if posting is not None else None

            return {
                "card_no": self.mask_card(t.get("cardNo") or t.get("mobileCardNo")),
                "date": t.get("consumeDate") or t.get("transDate"),   # 消費日
                # Conversion/value dates must not masquerade as posting dates in readers either.
                "post_date": post_date,
                "booking_status": "booked" if post_date else None,
                # Unbilled consume rows (live 2026-10-09) use merchantName/twdAmount.
                "desc": t.get("transDesc") or t.get("merchantName"),
                "amount": t.get("amount") if t.get("amount") is not None else t.get("twdAmount"),  # 台幣入帳金額
                "currency": t.get("currency"),
                "consume_country": consume_country,
                "consume_currency": consume_currency,
                "consume_amount": consume_amount,
            }


if __name__ == "__main__":
    import json
    crawler = CathayCrawler()
    result = crawler.run(login_url=f"{BASE}/mybank/", headless=False)
    out_file = Path(__file__).resolve().parents[1] / "data" / "cathay_collected.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[done] 已存: {out_file}")
    # 摘要（不印敏感數字明細，只印結構）
    data = result.get("data", {})
    print("\n===== 抓取摘要 =====")
    for k, v in data.items():
        if k.startswith("_"):
            continue
        if isinstance(v, list):
            print(f"  {k}: {len(v)} 筆")
        elif isinstance(v, dict):
            print(f"  {k}: {list(v.keys())}")
        else:
            print(f"  {k}: {type(v).__name__}")
    print(f"\n  攔到的 endpoint 數: {len(data.get('_all_endpoints', []))}")
