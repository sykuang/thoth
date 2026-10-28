#!/usr/bin/env python3
"""DBS Taiwan digital banking crawler (internet-banking.dbs.com.tw/digitw/).

星展（台灣）internet-banking.dbs.com.tw/digitw/ 個人網銀爬蟲。

2026-06-12 初版（dry probe + vision 驗證）:

  登入頁特徵（極友善）:
    - 純 SPA（React + styled-components），無 frameset、無 iframe
    - 表單只有 2 欄:
        #username  (type=password 眼睛遮罩) - 使用者帳號
        #password  (type=password)         - 密碼
    - #loginbutton (BUTTON text='登入')
    - **無 CAPTCHA**！不用 OCR
    - 無「立即登入」開 modal 步驟，欄位 page load 就在

  登入流程:
    Step 1: page goto /digitw/ → 自動 redirect /digitw/login → 等 8s SPA hydrate
    Step 2: shared checkpoints 先分類 terminal blocker，再以 true keyboard 輸入帳密
    Step 3: 唯一、可見且 enabled 的 #loginbutton 原生 click 一次
    Step 4: 等登入身分或 scoped response，交回 shared evaluator 判定

  ⚠️ 鐵律 max_attempts=1 — 失敗 raise DbsLoginError，絕不重打（會鎖帳號）

  TODO 第二輪（尚未完成 — 待使用者配合錄 HAR）:
    - 現況: collect() 只 dump nav_items + page text + 攔到的 api_responses
            (assets/liabilities/customer-profile 已可解析但無逐筆 billed_txn / casa 交易明細)
    - 待補:
      1. 信用卡明細: 點主選單「信用卡 → 帳單明細 / 消費明細」, 攔 billed_txn endpoint
         → persist_dbs 補 billed_payload + card_pending refresh
      2. 存款交易明細: 點「帳戶 → 交易紀錄」, 攔 transactions endpoint
         → persist_dbs 補 twd_transactions upsert
      3. 處理可能的 2FA / OTP（首次登入或新裝置才會問, user_data_dir 持久化後免）
"""
from __future__ import annotations

import contextlib
import re
import sys
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.base import BankCollectResult, BankCrawler, ResponseCollector
from backend.core.card_bills import card_bill_money, make_card_bill_fact, publish_card_bill_facts
from backend.core.error_codes import error_step, with_error_code, exception_error_id, format_error_id
from backend.core.creds import DbsCreds
from backend.core.login_checkpoints import CheckpointKind, CheckpointPhase, LoginCheckpointRule

BASE = "https://internet-banking.dbs.com.tw/digitw/"
LOGIN_PATH_HINT = "/digitw/login"


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


class DbsLoginError(RuntimeError):
    """DBS login 送出後失敗——立刻中止，絕不自動重打。"""


def _dbs_card_bill_fact(out: dict):
    liabilities = (out.get("api_responses") or {}).get("liabilities") or []
    liability = liabilities[0].get("resp") if liabilities and isinstance(liabilities[0], dict) else {}
    payment = ((liability or {}).get("creditCard") or {}).get("paymentDetails") or {}
    fee = out.get("dbs_card_fee_page") or {}
    with error_step('DBS-07-001-001'):
        fee_amount = card_bill_money(fee.get("bill_due_amount"))
    with error_step('DBS-07-002-001'):
        statement_amount = card_bill_money(payment.get("amount"))
    with error_step('DBS-07-003-001'):
        already_paid = card_bill_money(payment.get("alreadyPaid"))
    fee_due = fee.get("payment_due_date")
    dashboard_due = payment.get("dueDate")

    if fee_amount is not None:
        if already_paid is None:
            with contextlib.suppress(Exception):
                _log(format_error_id('DBS-06-001-001', bank="dbs"))
            return None
        elif not fee_due:
            with contextlib.suppress(Exception):
                _log(format_error_id('DBS-06-002-001', bank="dbs"))
            return None
        elif fee_due != dashboard_due:
            with contextlib.suppress(Exception):
                _log(format_error_id('DBS-06-003-001', bank="dbs"))
            return None
        remaining = max(fee_amount - already_paid, 0)
    elif statement_amount is not None and already_paid is not None:
        remaining = max(statement_amount - already_paid, 0)
    else:
        return None
    with error_step('DBS-07-004-001'):
        return make_card_bill_fact(
            remaining_due=remaining,
            payment_due_date=fee_due or dashboard_due,
        )


class DbsCrawler(BankCrawler):
    USES_SHARED_LOGIN_CHECKPOINTS: ClassVar[bool] = True
    CREDENTIAL_HOSTS = frozenset({"internet-banking.dbs.com.tw"})

    def __init__(self):
        super().__init__(name="dbs")
        self.creds = DbsCreds.load()

    def _host_filter(self) -> str:
        return "dbs.com.tw"

    def _logged_in(self, page, *, _auth_probe_ids=None) -> bool:
        if _auth_probe_ids is not None:
            _auth_probe_ids.clear()
        try:
            parsed = urlsplit(page.url or "")
            if (
                parsed.scheme.lower() != "https"
                or parsed.hostname != "internet-banking.dbs.com.tw"
                or parsed.port not in (None, 443)
                or parsed.username is not None
                or parsed.password is not None
                or LOGIN_PATH_HINT in parsed.path.lower()
            ):
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
            body_parts = []
            for scope in scopes:
                for selector in ("#username", "#password", "#loginbutton"):
                    with error_step({'#username': 'DBS-03-014-002', '#password': 'DBS-03-014-003', '#loginbutton': 'DBS-03-014-004'}.get(selector, 'DBS-03-014-001')):
                        candidates = scope.locator(selector)
                    with error_step('DBS-03-015-001'):
                        if any(
                            error_step({'#username': 'DBS-03-700-002', '#password': 'DBS-03-700-003', '#loginbutton': 'DBS-03-700-004'}.get(selector, 'DBS-03-700-001'))(lambda: error_step({'#username': 'DBS-03-701-002', '#password': 'DBS-03-701-003', '#loginbutton': 'DBS-03-701-004'}.get(selector, 'DBS-03-701-001'))(lambda: candidates.nth(index))().is_visible())()
                            for index in range(error_step({'#username': 'DBS-03-702-002', '#password': 'DBS-03-702-003', '#loginbutton': 'DBS-03-702-004'}.get(selector, 'DBS-03-702-001'))(lambda: candidates.count())())
                        ):
                            return False
                with error_step('DBS-03-016-001'):
                    bodies = scope.locator("body")
                with error_step('DBS-03-017-001'):
                    body_parts.extend(
                        error_step('DBS-03-703-001')(lambda: error_step('DBS-03-704-001')(lambda: bodies.nth(index))().inner_text())()
                        for index in range(error_step('DBS-03-705-001')(lambda: bodies.count())())
                    )
            body = "\n".join(body_parts)
            known_overview = parsed.path.rstrip("/").lower() == "/digitw/overview"
            identity = (
                "帳戶總覽" in body
                or "資產總覽" in body
                or (
                    known_overview
                    and "總覽" in body
                    and ("帳戶" in body or "資產" in body)
                )
            )
            logout = "登出" in body or re.search(
                r"(?<![A-Za-z])logout(?![A-Za-z])", body, re.IGNORECASE
            ) is not None
            return len(body) >= 300 and logout and identity
        except Exception as error:
            if _auth_probe_ids is not None:
                error_id = exception_error_id(error, bank='dbs')
                if error_id in ('DBS-03-014-002', 'DBS-03-014-003', 'DBS-03-014-004', 'DBS-03-700-002',
                 'DBS-03-700-003', 'DBS-03-700-004', 'DBS-03-701-002', 'DBS-03-701-003',
                 'DBS-03-701-004', 'DBS-03-702-002', 'DBS-03-702-003', 'DBS-03-702-004',
                 'DBS-03-016-001', 'DBS-03-703-001', 'DBS-03-704-001', 'DBS-03-705-001'):
                    _auth_probe_ids[:] = [error_id]
            return False

    def login(self, page) -> bool:
        return self._shared_login(page)

    def prepare_login_page(self, page) -> None:
        with error_step('DBS-01-001-001'):
            page.wait_for_timeout(8000)

    def is_authenticated(self, page) -> bool:
        auth_probe_ids = getattr(self, "_login_auth_probe_ids", None)
        return (
            self._logged_in(page) if auth_probe_ids is None
            else self._logged_in(page, _auth_probe_ids=auth_probe_ids)
        )

    def login_checkpoint_rules(self) -> tuple[LoginCheckpointRule, ...]:
        all_phases = tuple(CheckpointPhase)
        post = (CheckpointPhase.POST_SUBMIT, CheckpointPhase.POST_SUBMIT_SETTLE)
        otp = re.compile(
            r"^[\s\S]{0,400}(?:(?<![A-Za-z])OTP(?![A-Za-z])|一次性密碼|簡訊驗證碼|裝置驗證|信任此裝置|新裝置登入)[\s\S]{0,400}$",
            re.IGNORECASE,
        )
        password = re.compile(
            r"^[\s\S]{0,200}(?:(?:必須|強制|請立即)(?:變更|修改|更新)(?:您的)?密碼|密碼已到期)[\s\S]{0,200}$"
        )
        error = re.compile(
            r"^\s*(?:帳號不存在|密碼不正確|帳號.{0,20}(?:已)?(?:被)?鎖定|登入失敗|invalid credentials|account locked)[。.!！\s]*$",
            re.IGNORECASE,
        )
        rules = []
        for suffix, selector in (("modal", ".modal.show"), ("dialog", "[role='dialog']")):
            rules.append(LoginCheckpointRule(
                name=f"dbs-otp-required-{suffix}", bank="dbs", phases=all_phases,
                error_id={'modal': 'DBS-03-050-001', 'dialog': 'DBS-03-055-001'}[suffix],
                kind=CheckpointKind.OTP_REQUIRED, container_selector=selector,
                required_body_pattern=otp,
            ))
        for suffix, selector in (("modal", ".modal.show"), ("dialog", "[role='dialog']")):
            rules.append(LoginCheckpointRule(
                name=f"dbs-password-change-required-{suffix}", bank="dbs", phases=all_phases,
                error_id={'modal': 'DBS-03-060-001', 'dialog': 'DBS-03-065-001'}[suffix],
                kind=CheckpointKind.PASSWORD_CHANGE_REQUIRED, container_selector=selector,
                required_body_pattern=password,
            ))
        for suffix, selector in (("error", ".error"), ("alert", ".alert"), ("role-alert", "[role='alert']")):
            rules.append(LoginCheckpointRule(
                name=f"dbs-explicit-login-error-{suffix}", bank="dbs", phases=post,
                error_id={'error': 'DBS-03-070-001', 'alert': 'DBS-03-075-001', 'role-alert': 'DBS-03-080-001'}[suffix],
                kind=CheckpointKind.EXPLICIT_LOGIN_ERROR, container_selector=selector,
                required_body_pattern=error,
            ))
        for suffix, selector in (("modal", ".modal.show"), ("dialog", "[role='dialog']")):
            rules.append(LoginCheckpointRule(
                name=f"dbs-unknown-{suffix}", bank="dbs", phases=all_phases,
                error_id={'modal': 'DBS-03-085-001', 'dialog': 'DBS-03-090-001'}[suffix],
                kind=CheckpointKind.UNKNOWN_BLOCKER, container_selector=selector,
            ))
        rules.append(LoginCheckpointRule(
            name="dbs-login-form-still-visible", bank="dbs", phases=post,
            error_id='DBS-03-095-001',
            kind=CheckpointKind.UNKNOWN_BLOCKER, container_selector="#username",
        ))
        return tuple(rules)

    def submit_credentials_once(self, page) -> None:
        fields = (
            ("#username", self.creds.username, 300),
            ("#password", self.creds.password, 500),
        )
        try:
            with error_step('DBS-02-001-001'):
                page.wait_for_selector("#username", state="visible", timeout=15000)
            with error_step('DBS-02-002-001'):
                page.wait_for_selector("#password", state="visible", timeout=5000)
            with error_step('DBS-02-003-001'):
                page.wait_for_selector("#loginbutton", state="visible", timeout=5000)
            for selector, value, final_wait in fields:
                with error_step({'#username': 'DBS-02-017-001', '#password': 'DBS-02-035-001'}[selector]):
                    candidates = page.locator(selector)
                with error_step({'#username': 'DBS-02-018-001', '#password': 'DBS-02-036-001'}[selector]):
                    if candidates.count() != 1:
                        raise with_error_code(DbsLoginError('登入欄位無法安全填寫；未送出'), {'#username': 'DBS-02-019-001', '#password': 'DBS-02-037-001'}[selector])
                with error_step({'#username': 'DBS-02-020-001', '#password': 'DBS-02-038-001'}[selector]):
                    field = candidates.nth(0)
                with error_step({'#username': 'DBS-02-021-001', '#password': 'DBS-02-039-001'}[selector]):
                    with error_step({'#username': 'DBS-02-022-001', '#password': 'DBS-02-040-001'}[selector]):
                        _error_condition = not field.is_visible()
                    if _error_condition:
                        raise with_error_code(DbsLoginError('登入欄位無法安全填寫；未送出'), {'#username': 'DBS-02-023-001', '#password': 'DBS-02-041-001'}[selector])
                    else:
                        with error_step({'#username': 'DBS-02-024-001', '#password': 'DBS-02-042-001'}[selector]):
                            _error_condition = not field.is_enabled()
                        if _error_condition:
                            raise with_error_code(DbsLoginError('登入欄位無法安全填寫；未送出'), {'#username': 'DBS-02-023-002', '#password': 'DBS-02-041-002'}[selector])
                with error_step({'#username': 'DBS-02-025-001', '#password': 'DBS-02-043-001'}[selector]):
                    field.click()
                with error_step({'#username': 'DBS-02-026-001', '#password': 'DBS-02-044-001'}[selector]):
                    page.wait_for_timeout(200)
                with error_step({'#username': 'DBS-02-027-001', '#password': 'DBS-02-045-001'}[selector]):
                    field.click(click_count=3)
                with error_step({'#username': 'DBS-02-028-001', '#password': 'DBS-02-046-001'}[selector]):
                    page.wait_for_timeout(100)
                with error_step({'#username': 'DBS-02-029-001', '#password': 'DBS-02-047-001'}[selector]):
                    page.keyboard.press("Backspace")
                with error_step({'#username': 'DBS-02-030-001', '#password': 'DBS-02-048-001'}[selector]):
                    page.wait_for_timeout(100)
                with error_step({'#username': 'DBS-02-031-001', '#password': 'DBS-02-049-001'}[selector]):
                    page.keyboard.type(value, delay=80)
                with error_step({'#username': 'DBS-02-032-001', '#password': 'DBS-02-050-001'}[selector]):
                    page.wait_for_timeout(final_wait)
                with error_step({'#username': 'DBS-02-033-001', '#password': 'DBS-02-051-001'}[selector]):
                    if len(field.input_value()) != len(value):
                        raise with_error_code(DbsLoginError('登入欄位輸入長度不符；未送出'), {'#username': 'DBS-02-034-001', '#password': 'DBS-02-052-001'}[selector])
        except DbsLoginError:
            raise
        except Exception as error:
            raise with_error_code(DbsLoginError('登入欄位無法安全填寫；未送出'), exception_error_id(error, bank="dbs") or 'DBS-02-004-001') from None

        try:
            with error_step('DBS-02-005-001'):
                candidates = page.locator("#loginbutton")
            with error_step('DBS-02-006-001'):
                if candidates.count() != 1:
                    raise with_error_code(DbsLoginError('找不到唯一且可操作的登入按鈕；未送出'), 'DBS-02-007-001')
            with error_step('DBS-02-008-001'):
                submit = candidates.nth(0)
            with error_step('DBS-02-009-001'):
                with error_step('DBS-02-010-001'):
                    _error_condition = not submit.is_visible()
                if _error_condition:
                    raise with_error_code(DbsLoginError('找不到唯一且可操作的登入按鈕；未送出'), 'DBS-02-011-001')
                else:
                    with error_step('DBS-02-012-001'):
                        _error_condition = not submit.is_enabled()
                    if _error_condition:
                        raise with_error_code(DbsLoginError('找不到唯一且可操作的登入按鈕；未送出'), 'DBS-02-011-002')
                    else:
                        with error_step('DBS-02-013-001'):
                            _error_condition = ' '.join(submit.inner_text().split()) != '登入'
                        if _error_condition:
                            raise with_error_code(DbsLoginError('找不到唯一且可操作的登入按鈕；未送出'), 'DBS-02-011-003')
        except DbsLoginError:
            raise
        except Exception as error:
            raise with_error_code(DbsLoginError('無法安全確認登入按鈕；未送出'), exception_error_id(error, bank="dbs") or 'DBS-02-014-001') from None

        try:
            with error_step('DBS-02-015-001'):
                submit.click(timeout=8000)
        except Exception as error:
            raise with_error_code(DbsLoginError('送出狀態不明；禁止自動重試'), exception_error_id(error, bank="dbs") or 'DBS-02-016-001') from None

        try:
            with error_step('DBS-03-001-001'):
                page.wait_for_timeout(3000)
            for _ in range(20):
                with error_step('DBS-03-002-001'):
                    page.wait_for_timeout(1000)
                if self._logged_in(page):
                    return
                scopes = [
                    page,
                    *(frame for frame in page.frames if frame is not page.main_frame),
                ]
                for scope in scopes:
                    for selector in (
                        ".modal.show", "[role='dialog']", ".error", ".alert", "[role='alert']",
                    ):
                        with error_step({'.modal.show': 'DBS-03-003-001', "[role='dialog']": 'DBS-03-004-001', '.error': 'DBS-03-005-001', '.alert': 'DBS-03-006-001', "[role='alert']": 'DBS-03-007-001'}[selector]):
                            candidates = scope.locator(selector)
                        with error_step({'.modal.show': 'DBS-03-008-001', "[role='dialog']": 'DBS-03-009-001', '.error': 'DBS-03-010-001', '.alert': 'DBS-03-011-001', "[role='alert']": 'DBS-03-012-001'}[selector]):
                            if any(
                                error_step({'.modal.show': 'DBS-03-706-002', "[role='dialog']": 'DBS-03-706-003', '.error': 'DBS-03-706-004', '.alert': 'DBS-03-706-005', "[role='alert']": 'DBS-03-706-006'}[selector])(lambda: error_step({'.modal.show': 'DBS-03-707-002', "[role='dialog']": 'DBS-03-707-003', '.error': 'DBS-03-707-004', '.alert': 'DBS-03-707-005', "[role='alert']": 'DBS-03-707-006'}[selector])(lambda: candidates.nth(index))().is_visible())()
                                for index in range(error_step({'.modal.show': 'DBS-03-708-002', "[role='dialog']": 'DBS-03-708-003', '.error': 'DBS-03-708-004', '.alert': 'DBS-03-708-005', "[role='alert']": 'DBS-03-708-006'}[selector])(lambda: candidates.count())())
                            ):
                                return
        except Exception as error:
            raise with_error_code(DbsLoginError('狀態無法安全確認；禁止自動重試'), exception_error_id(error, bank="dbs") or 'DBS-03-013-001') from None

    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        """DBS collect 第一輪：dump 登入後 URL + page text + endpoint 地圖。

        TODO 第二輪（見檔案頂端 docstring）: 點信用卡 menu / 帳戶 → 抓 billed_txn / casa 交易明細
        """
        out: dict = {}
        with error_step('DBS-06-004-001'):
            page.wait_for_timeout(8000)

        debug_dir = _debug_dir()
        with contextlib.suppress(Exception):
            page.screenshot(path=str(debug_dir / "00_home.png"), full_page=True)

        out["initial_url"] = page.url
        try:
            with error_step('DBS-04-001-001'):
                out["title"] = page.title()
        except Exception as _action_error:
            with contextlib.suppress(Exception):
                _log(format_error_id(exception_error_id(_action_error, bank="dbs") or 'DBS-07-005-001', bank="dbs"))
            out["title"] = ""

        # dump 主 page text（DBS 是純 SPA，無 frameset）
        try:
            with error_step('DBS-04-002-001'):
                txt = page.evaluate("() => (document.body.innerText || '').slice(0, 15000)") or ""
        except Exception as _action_error:
            with contextlib.suppress(Exception):
                _log(format_error_id(exception_error_id(_action_error, bank="dbs") or 'DBS-07-006-001', bank="dbs"))
            txt = ""
        out["home_text"] = txt
        _log(f"[dbs][collect] home text_len={len(txt)}")

        # dump 主 page 內所有 link/button text（找 menu）
        try:
            with error_step('DBS-04-003-001'):
                nav_items = page.evaluate("""() => {
                const out = [];
                for (const el of document.querySelectorAll('a, button, [role=button], [role=link]')) {
                    if (el.offsetParent === null) continue;
                    const t = (el.textContent || '').trim();
                    if (!t || t.length > 30) continue;
                    const r = el.getBoundingClientRect();
                    out.push({
                        tag: el.tagName,
                        text: t,
                        href: el.href || '',
                        x: r.x, y: r.y, w: r.width, h: r.height,
                    });
                }
                return out;
            }""")
        except Exception as _action_error:
            with contextlib.suppress(Exception):
                _log(format_error_id(exception_error_id(_action_error, bank="dbs") or 'DBS-07-007-001', bank="dbs"))
            nav_items = []
        out["nav_items"] = nav_items[:80]
        _log(f"[dbs][collect] nav items: {len(nav_items)}（含 menu/button）")

        # 第二輪 probe：照使用者截圖，從 overview 的「活期存款」帳戶名稱 / row 點進帳戶明細頁。
        # DBS dashboard 沒有顯式「交易紀錄」menu；交易明細藏在帳戶 row drilldown。
        before_detail_hit_count = len(collector.hits)
        try:
            assets_hit = collector.latest("assets")
            twd_account = None
            if assets_hit and isinstance(assets_hit.resp_json, dict):
                casa = assets_hit.resp_json.get("casa") or {}
                for acct in casa.get("accounts") or []:
                    if not isinstance(acct, dict):
                        continue
                    bal = acct.get("availableBalance") or {}
                    if bal.get("currency") == "TWD" or acct.get("schemeName") == "臺幣數位存款":
                        twd_account = acct
                        break
            acct_name = (twd_account or {}).get("schemeName") or "臺幣數位存款"
            acct_masked = (twd_account or {}).get("accountId") or ""
            acct_display = (twd_account or {}).get("displayAccountNumber") or ""
            acct_tail = (acct_display or acct_masked)[-5:] if (acct_display or acct_masked) else ""
            out["twd_account_drilldown_target"] = {
                "schemeName": acct_name,
                "accountId": acct_masked,
                "displayAccountNumber": acct_display,
                "tail": acct_tail,
            }
            with error_step('DBS-05-001-001'):
                click_result = page.evaluate(r"""({acctName, acctTail}) => {
                const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
                };
                const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
                const candidates = [];
                for (const el of document.querySelectorAll('a,button,[role="button"],[role="link"],li,div,span')) {
                    if (!visible(el)) continue;
                    const text = norm(el.textContent);
                    if (!text || text.length > 220) continue;
                    const hasName = acctName && text.includes(acctName);
                    const hasTail = acctTail && text.includes(acctTail);
                    if (!hasName && !hasTail) continue;
                    const r = el.getBoundingClientRect();
                    candidates.push({
                        el, text, tag: el.tagName, role: el.getAttribute('role') || '',
                        href: el.getAttribute('href') || '', cls: (el.className || '').toString().slice(0, 80),
                        x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
                        area: r.width * r.height,
                        score: (hasName ? 10 : 0) + (hasTail ? 20 : 0)
                            + (/TWD|帳戶餘額|活期存款|外幣/.test(text) ? 5 : 0)
                            + ((el.tagName === 'A' || el.tagName === 'BUTTON' || el.getAttribute('role')) ? 8 : 0),
                    });
                }
                candidates.sort((a, b) => (b.score - a.score) || (a.area - b.area));
                const dump = candidates.slice(0, 12).map(({el, ...rest}) => rest);
                if (!candidates.length) return {
                    clicked: false, reason: 'no_candidate',
                    candidate_count: candidates.length, dump
                };
                const c = candidates[0];
                try { c.el.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
                c.el.click();
                return {
                    clicked: true, target: dump[0],
                    candidate_count: candidates.length, dump
                };
            }""", {"acctName": acct_name, "acctTail": acct_tail})
            out["twd_account_drilldown_click"] = click_result
            clicked = isinstance(click_result, dict) and click_result.get("clicked") is True
            candidate_count = (
                click_result.get("candidate_count", 0)
                if isinstance(click_result, dict)
                and type(click_result.get("candidate_count", 0)) is int
                else 0
            )
            _log(
                f"[dbs][twd] drilldown click={clicked} "
                f"candidate_count={candidate_count}"
            )
            with error_step('DBS-05-002-001'):
                page.wait_for_timeout(9000)
            with contextlib.suppress(Exception):
                page.screenshot(path=str(debug_dir / "01_twd_account_detail.png"), full_page=True)
            with error_step('DBS-05-003-001'):
                detail_text = page.evaluate("() => (document.body.innerText || '').slice(0, 40000)") or ""
            out["twd_account_detail_url"] = page.url
            out["twd_account_detail_text"] = detail_text
            out["twd_account_detail_api_endpoints"] = sorted({
                h.endpoint for h in collector.hits[before_detail_hit_count:] if h.resp_json is not None
            })
            with error_step('DBS-05-004-001'):
                out["twd_account_detail_controls"] = page.evaluate(r"""() => {
                const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
                };
                const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
                return [...document.querySelectorAll('a,button,[role="button"],[role="link"],select,input')]
                    .filter(visible)
                    .map((el, idx) => ({
                        idx, tag: el.tagName, role: el.getAttribute('role') || '', type: el.getAttribute('type') || '',
                        text: norm(el.textContent || el.value || el.getAttribute('aria-label') || '').slice(0, 120),
                        id: el.id || '', name: el.getAttribute('name') || '', href: el.getAttribute('href') || '',
                    }))
                    .filter(x => x.text || x.id || x.name)
                    .slice(0, 120);
            }""")

            # DBS API calls are made through frontend interceptors; raw fetch misses required
            # request decoration and returns 401. So collect via real UI clicks instead.
            before_month_click_hits = len(collector.hits)
            month_clicks = []
            for label in ("七月", "六月", "五月"):
                try:
                    with error_step('DBS-05-005-001'):
                        clicked = page.evaluate(r"""(wanted) => {
                        const visible = (el) => {
                            const r = el.getBoundingClientRect();
                            const st = getComputedStyle(el);
                            return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
                        };
                        for (const el of document.querySelectorAll('button,a,[role="button"]')) {
                            const t = (el.textContent || '').replace(/\s+/g, '').trim();
                            if (t === wanted && visible(el)) {
                                el.click();
                                return {clicked: true, tag: el.tagName, text: t};
                            }
                        }
                        return {clicked: false, text: wanted};
                    }""", label)
                    with error_step('DBS-05-006-001'):
                        page.wait_for_timeout(2500)
                    month_clicks.append({"label": label, **(clicked or {})})
                except Exception as _action_error:
                    with contextlib.suppress(Exception):
                        _log(format_error_id(exception_error_id(_action_error, bank="dbs") or 'DBS-07-008-001', bank="dbs"))
                    month_clicks.append({"label": label, "clicked": False, "error": "probe_failed"})
            def _click_other_months_and_probe() -> dict:
                try:
                    with error_step('DBS-05-014-001'):
                        probe = page.evaluate(r"""() => {
                        const visible = (el) => {
                            const r = el.getBoundingClientRect();
                            const st = getComputedStyle(el);
                            return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
                        };
                        for (const el of document.querySelectorAll('button,a,[role="button"]')) {
                            const t = (el.textContent || '').replace(/\s+/g, '').trim();
                            if (t === '其他月份' && visible(el)) {
                                el.click();
                                return {clicked: true, tag: el.tagName, text: t};
                            }
                        }
                        return {clicked: false, reason: 'not_found'};
                    }""") or {"clicked": False}
                    with error_step('DBS-05-015-001'):
                        page.wait_for_timeout(1000)
                    return probe
                except Exception as _action_error:
                    with contextlib.suppress(Exception):
                        _log(format_error_id(exception_error_id(_action_error, bank="dbs") or 'DBS-05-016-001', bank="dbs"))
                    return {"clicked": False, "error": "probe_failed"}

            other_probe = _click_other_months_and_probe()
            with error_step('DBS-05-007-001'):
                page.wait_for_timeout(1500)
            with contextlib.suppress(Exception):
                page.screenshot(path=str(debug_dir / "02_other_months.png"), full_page=True)
            with error_step('DBS-05-008-001'):
                other_probe["controls_after_click"] = page.evaluate(r"""() => {
                const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
                };
                const wanted = new Set(['2025','2026','一月','二月','三月','四月','五月','六月','七月','八月','九月','十月','十一月','十二月','其他月份']);
                return [...document.querySelectorAll('button,a,[role="button"],select,input,div,span,p')]
                    .filter(visible)
                    .map((el, idx) => {
                        const r = el.getBoundingClientRect();
                        return {
                            idx, tag: el.tagName, role: el.getAttribute('role') || '', type: el.getAttribute('type') || '',
                            text: (el.textContent || el.value || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim().slice(0, 160),
                            id: el.id || '', name: el.getAttribute('name') || '', cls: (el.className || '').toString().slice(0, 80),
                            x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
                        };
                    })
                    .filter(x => wanted.has(x.text))
                    .slice(0, 300);
            }""")

            # Click every selectable month in the last 12 months. The popover has duplicate
            # Chinese month labels under 2025/2026, so choose by nearest year-column x.
            # Important: DOM el.click() can report success without triggering DBS React handlers;
            # use real mouse coordinates and verify a new transactions-history API hit appears.
            other_month_clicks = []
            for year, month_label in (
                ("2026", "四月"), ("2026", "三月"), ("2026", "二月"), ("2026", "一月"),
                ("2025", "十二月"), ("2025", "十一月"), ("2025", "十月"), ("2025", "九月"), ("2025", "八月"),
            ):
                probe = _click_other_months_and_probe()
                if not probe.get("clicked"):
                    other_month_clicks.append({"year": year, "month": month_label, "clicked": False, "error": probe})
                    continue
                try:
                    with error_step('DBS-05-009-001'):
                        target = page.evaluate(r"""({year, monthLabel}) => {
                        const visible = (el) => {
                            const r = el.getBoundingClientRect();
                            const st = getComputedStyle(el);
                            return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
                        };
                        const norm = (s) => (s || '').replace(/\s+/g, '').trim();
                        const nodes = [...document.querySelectorAll('button,a,[role="button"],div,span,p')]
                            .filter(visible)
                            .map(el => {
                                const r = el.getBoundingClientRect();
                                return {text: norm(el.textContent), tag: el.tagName, cls: (el.className || '').toString(), x: r.x, y: r.y, w: r.width, h: r.height};
                            });
                        // Prefer the narrower year label, not the wide popover column container.
                        const years = nodes.filter(n => n.text === year && n.w < 100).sort((a,b) => a.y-b.y || a.x-b.x);
                        if (!years.length) return {found: false, reason: 'year_not_found'};
                        const yr = years[0];
                        // Prefer the outer 53x32 selectable month cell, not the inner text span.
                        const monthCandidates = nodes
                            .filter(n => n.text === monthLabel && n.w >= 40 && n.h >= 24)
                            .map(n => ({...n, dist: Math.abs((n.x + n.w/2) - (yr.x + yr.w/2))}))
                            .sort((a,b) => a.dist-b.dist || Math.abs(a.w-53)-Math.abs(b.w-53) || a.y-b.y);
                        if (!monthCandidates.length) return {found: false, reason: 'month_not_found'};
                        const t = monthCandidates[0];
                        return {found: true, year, month: monthLabel, x: Math.round(t.x + t.w/2), y: Math.round(t.y + t.h/2), rawX: Math.round(t.x), rawY: Math.round(t.y), w: Math.round(t.w), h: Math.round(t.h), dist: Math.round(t.dist), tag: t.tag, cls: t.cls.slice(0, 80)};
                    }""", {"year": year, "monthLabel": month_label}) or {"found": False}
                    before_hits = len([h for h in collector.hits if h.endpoint == "inquiry" and h.resp_json is not None])
                    if target.get("found"):
                        with error_step('DBS-05-010-001'):
                            page.mouse.click(float(target["x"]), float(target["y"]))
                    with error_step('DBS-05-011-001'):
                        page.wait_for_timeout(3000)
                    after_hits = len([h for h in collector.hits if h.endpoint == "inquiry" and h.resp_json is not None])
                    other_month_clicks.append({
                        **target,
                        "clicked": bool(target.get("found")),
                        "api_triggered": after_hits > before_hits,
                        "api_hits_before": before_hits,
                        "api_hits_after": after_hits,
                    })
                except Exception as _action_error:
                    with contextlib.suppress(Exception):
                        _log(format_error_id(exception_error_id(_action_error, bank="dbs") or 'DBS-07-009-001', bank="dbs"))
                    other_month_clicks.append({
                        "year": year, "month": month_label,
                        "clicked": False, "error": "probe_failed",
                    })
            out["twd_txn_month_clicks"] = month_clicks
            out["twd_txn_other_months_probe"] = other_probe
            out["twd_txn_other_month_clicks"] = other_month_clicks
            out["twd_txn_month_click_endpoints"] = sorted({
                h.endpoint for h in collector.hits[before_month_click_hits:] if h.resp_json is not None
            })
            _log(
                f"[dbs][twd] month_click_count={len(month_clicks)} "
                f"other_month_click_count={len(other_month_clicks)} "
                f"endpoint_count={len(out['twd_txn_month_click_endpoints'])}"
            )
            _log(
                f"[dbs][twd] detail text_len={len(detail_text)} "
                f"endpoint_count={len(out['twd_account_detail_api_endpoints'])}"
            )

            # Return to overview before probing top-nav card-fee shortcut.
            with contextlib.suppress(Exception):
                with error_step('DBS-05-012-001'):
                    page.goto("https://internet-banking.dbs.com.tw/digitw/overview", wait_until="domcontentloaded", timeout=15000)
                with error_step('DBS-05-013-001'):
                    page.wait_for_timeout(5000)
        except Exception as _action_error:
            with contextlib.suppress(Exception):
                _log(format_error_id(exception_error_id(_action_error, bank="dbs") or 'DBS-07-010-001', bank="dbs"))
            out["twd_account_drilldown_error"] = "probe_failed"
            _log("[dbs][twd] drilldown probe failed; details withheld")

        # 2026-08-15 實頁驗證：overview 卡片 tile 不是 drilldown；點 CardWrapper 不改 URL、
        # 不發任何 API，頂部「繳卡費」也只有帳單摘要。DBS Internet Banking 未提供可證明
        # 的逐筆信用卡來源，故此處不得合成明細或日期。

        # 信用卡卡費：使用者實測指出登入後點頂部「繳卡費」才看得到最近一期帳單金額。
        # 這頁是發起繳卡費流程，不是歷史繳款紀錄；只用來補 bill_due/payment_due。
        try:
            before_card_fee_hits = len(collector.hits)
            with error_step('DBS-06-005-001'):
                card_fee_click = page.evaluate(r"""() => {
                const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
                };
                const norm = (s) => (s || '').replace(/\s+/g, '').trim();
                const candidates = [...document.querySelectorAll('a,button,[role="button"],[role="link"],div,span')]
                    .filter(visible)
                    .map(el => {
                        const r = el.getBoundingClientRect();
                        return {el, text: norm(el.textContent), tag: el.tagName, role: el.getAttribute('role') || '', href: el.getAttribute('href') || '', x: r.x, y: r.y, w: r.width, h: r.height};
                    })
                    .filter(x => x.text === '繳卡費')
                    .sort((a, b) => {
                        const rank = (x) => (x.tag === 'A' || x.tag === 'BUTTON' || x.role ? 0 : 1);
                        return rank(a) - rank(b) || (a.w * a.h) - (b.w * b.h);
                    });
                const dump = candidates.slice(0, 8).map(({el, ...rest}) => rest);
                if (!candidates.length) return {clicked: false, reason: 'not_found', dump};
                const c = candidates[0];
                try { c.el.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
                c.el.click();
                return {clicked: true, target: dump[0], dump};
            }""") or {"clicked": False}
            out["dbs_card_fee_click"] = card_fee_click
            with error_step('DBS-06-006-001'):
                page.wait_for_timeout(8000)
            with error_step('DBS-06-007-001'):
                card_fee_text = page.evaluate("() => (document.body.innerText || '').slice(0, 20000)") or ""
            out["dbs_card_fee_page_text"] = card_fee_text
            out["dbs_card_fee_page"] = self._parse_card_fee_page(card_fee_text)
            out["dbs_card_fee_endpoints"] = sorted({
                h.endpoint for h in collector.hits[before_card_fee_hits:] if h.resp_json is not None
            })
            card_fee_clicked = (
                isinstance(card_fee_click, dict)
                and card_fee_click.get("clicked") is True
            )
            _log(
                f"[dbs][card_fee] click={card_fee_clicked} "
                f"parsed={bool(out['dbs_card_fee_page'])} "
                f"endpoint_count={len(out['dbs_card_fee_endpoints'])}"
            )
        except Exception as _action_error:
            with contextlib.suppress(Exception):
                _log(format_error_id(exception_error_id(_action_error, bank="dbs") or 'DBS-07-011-001', bank="dbs"))
            out["dbs_card_fee_error"] = "probe_failed"
            _log("[dbs][card_fee] probe failed; details withheld")

        out["final_url"] = page.url
        out["_all_endpoints"] = sorted({h.endpoint for h in collector.hits if h.resp_json})

        # 完整 dump 所有攔到的 API JSON（給後續 parser 用）
        # 鍵：endpoint name；值：list of resp_json（同 endpoint 可能多次呼叫）
        api_responses: dict = {}
        for h in collector.hits:
            if h.resp_json is None:
                continue
            ep = h.endpoint
            api_responses.setdefault(ep, []).append({
                "url": h.url,
                "method": h.method,
                "status": h.status,
                "resp": h.resp_json,
                "req_body": h.req_body,
            })
        out["api_responses"] = api_responses
        _log(f"[dbs][collect] dump {len(api_responses)} 個 endpoint 的 resp_json")

        with error_step('DBS-07-012-001'):
            publish_card_bill_facts(out, [_dbs_card_bill_fact(out)])

        _log(f"[dbs][collect] 攔到 {len(out['_all_endpoints'])} 個 endpoint")
        return BankCollectResult(**out)


    @staticmethod
    def _parse_card_fee_page(text: str) -> dict:
        """Parse DBS top-nav「繳卡費」page text.

        The page exposes only the current bill amount and due date. It is not a
        payment-history page, so no last_payment_* should be derived from it.
        """
        import re
        out: dict = {}
        compact = re.sub(r"\s+", "", text or "")
        amount = None
        m = re.search(r"信用卡.*?最近一期帳單金額.*?TWD\s*([\d,]+(?:\.\d+)?)", compact)
        if not m:
            m = re.search(r"最近一期帳單金額.*?TWD\s*([\d,]+(?:\.\d+)?)", compact)
        if m:
            try:
                amount = float(m.group(1).replace(",", ""))
            except ValueError:
                amount = None
        if amount is not None:
            out["bill_due_amount"] = amount
            out["currency"] = "TWD"
        due = None
        m = re.search(r"繳款截止日\s*(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text or "")
        if m:
            due = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        else:
            m = re.search(r"繳款截止日\s*(\d{1,2})月(\d{1,2})日", text or "")
            if m:
                from datetime import datetime
                due = f"{datetime.now().year:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
        if due:
            out["payment_due_date"] = due
        return out


def _debug_dir() -> Path:
    from backend.core.store import _data_root
    d = _data_root() / "dbs_collect"
    d.mkdir(parents=True, exist_ok=True)
    return d


if __name__ == "__main__":
    import json
    crawler = DbsCrawler()
    try:
        result = crawler.run(login_url=BASE, headless=False)
    except DbsLoginError:
        result = {"error": "login_failed_stop"}

    out_file = Path(__file__).resolve().parents[1] / "data" / "dbs_collected.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"\n[done] 已存: {out_file}")
