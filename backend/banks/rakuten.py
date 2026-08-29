#!/usr/bin/env python3
"""樂天國際銀行個人網銀 crawler。

登入頁是 Angular SPA；帳密由銀行前端自行做 E2E 加密後送出。本 crawler 只操作
真實表單，登入後從「臺幣存款」頁的已解密 DOM 讀取帳戶、餘額與六個月交易。
"""
from __future__ import annotations

import contextlib
import re
import time
from typing import ClassVar
from urllib.parse import urlparse

from scrapling.fetchers import StealthySession

from backend.core.base import BankCollectResult, BankCrawler, ResponseCollector

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
    if len(cells) < 6:
        return None
    date_time = cells[0].split()
    descriptions = [line.strip() for line in cells[1].splitlines() if line.strip()]
    income = cells[2].strip()
    expend = cells[3].strip()
    if not date_time or not descriptions or not (income or expend):
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
        and parsed.hostname == "www.rakuten-bank.com.tw"
        and parsed.path == QUERY_PATH
        and not parsed.params
        and not parsed.query
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
            const visible = e => !!e && !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
            const selected = document.querySelector('simple-dropdown2 a.txt_dropdown');
            const accountLabel = selected?.innerText || document.querySelector('simple-dropdown2')?.innerText || '';
            const balance = document.querySelector('.card-title-money')?.innerText || '';
            const rows = [...document.querySelectorAll('table.tb_mul tbody tr')]
                .filter(visible)
                .map(row => [...row.querySelectorAll(':scope > td')].map(cell => cell.innerText || ''));
            return {accountLabel, balance, rows};
        }""")
        account_label = str(snapshot.pop("accountLabel", "") or "")
        raw_balance = snapshot.pop("balance", "")
        number = account_no or _account_number(account_label)
        snapshot["txDetails"] = [
            parsed
            for cells in snapshot.pop("rows", [])
            if (parsed := _row_from_dom(cells)) is not None
        ]
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
        page.wait_for_timeout(200)
        options = page.locator(f"{root} .dropdown-menu a.dropdown-item:visible")
        labels = [options.nth(i).inner_text().strip() for i in range(options.count())]
        page.keyboard.press("Escape")
        return [label for label in labels if label]

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

    def _select_label(self, page, root: str, label: str) -> None:
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

        account_root = "simple-dropdown2"
        current_account = self._selected_label(page, account_root)
        account_labels = [current_account, *self._visible_labels(page, account_root)]
        accounts: list[tuple[str, str]] = []
        seen_accounts: set[str] = set()
        for label in account_labels:
            number = _account_number(label)
            if number and number not in seen_accounts:
                accounts.append((number, label))
                seen_accounts.add(number)
        if not accounts:
            raise RuntimeError("樂天帳戶選單沒有可辨識的帳號")

        results: list[dict] = []
        for account_no, account_label in accounts:
            selected_account = self._selected_label(page, account_root)
            if _account_number(selected_account) != account_no:
                self._select_label(page, account_root, account_label)

            month_root = "simple-dropdown"
            current_month = self._selected_label(page, month_root)
            month_labels = _six_month_labels([
                current_month,
                *self._visible_labels(page, month_root),
            ])
            for month_label in month_labels:
                selected_month = self._selected_label(page, month_root)
                if selected_month != month_label:
                    self._select_label(page, month_root, month_label)
                results.append(self._scrape_twd_page(page, account_no))

        endpoints = sorted({
            _endpoint_key(hit.url)
            for hit in collector.hits
            if "/channel-" in hit.url
        })
        return BankCollectResult(
            bank="rakuten",
            final_url=page.url,
            twd_txn_results=results,
            _all_endpoints=endpoints,
            card_bill_facts_ok=False,
            card_bill_facts=[],
        )


if __name__ == "__main__":
    crawler = RakutenCrawler()
    result = crawler.run(BASE, headless=False)
    print({"error": result.get("error"), "final_url": result.get("final_url")})
