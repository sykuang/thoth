#!/usr/bin/env python3
"""HSBC Taiwan credit-card crawler with a two-stage SPA login."""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.base import BankCollectResult, BankCrawler, ResponseCollector
from backend.core.card_bills import (
    card_bill_date, card_bill_money, make_card_bill_fact, publish_card_bill_facts,
)
from backend.core.creds import HsbcCreds
from backend.core.captcha import ocr_bytes
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointPhase,
    LoginCheckpointRule,
)

BASE = "https://card.hsbc.com.tw/#/login"

SEL_USERID = "#userId"
SEL_PWD = "#password"
SEL_CAPTCHA = "#captchaInput"
# HSBC 是 5 碼英數 CAPTCHA。2026-07-05 Azure log 證實只檢查
# expected_len/alnum_only 會放行形式合法但內容錯的 OCR false positive，銀行回
#「驗證碼錯誤，請重新輸入。」；送出後不可自動重試，故送出前必須加信心門檻。
HSBC_CAPTCHA_MIN_CONFIDENCE = 0.85

def _log(*a):
    print(*a, file=sys.stderr)


class HsbcLoginError(RuntimeError):
    """HSBC login 送出後失敗——立刻中止，絕不自動重打（防鎖帳號）。"""


def _hsbc_card_bill_facts(out: dict):
    facts = []
    details_by_tail = out.get("card_detail") or {}
    expected_cards = [
        card for card in out.get("cards") or []
        if isinstance(card, dict) and card.get("maskedCardNumber")
    ]
    for card in expected_cards:
        tail = str(card["maskedCardNumber"])[-4:]
        entry = details_by_tail.get(tail) if isinstance(details_by_tail, dict) else None
        if not isinstance(entry, dict):
            facts.append(None)
            continue
        details = ((entry.get("detail") or {}).get("details") or [])
        kv = {
            str(row.get("key") or "").strip(): str(row.get("value") or "").strip()
            for row in details if isinstance(row, dict) and row.get("key")
        }
        statement_amount = card_bill_money((kv.get("Last Statement Amount") or "").split(" ")[0])
        payment_amount = card_bill_money((kv.get("Last Payment Amount") or "").split(" ")[0])
        statement_date = card_bill_date(kv.get("Last Statement Date"))
        payment_date = card_bill_date(kv.get("Last Payment Date"))
        remaining = statement_amount
        if (remaining is not None and payment_amount is not None and statement_date
                and payment_date and payment_date >= statement_date):
            remaining = max(remaining - payment_amount, 0)
        facts.append(make_card_bill_fact(
            scope="card",
            card_no=entry.get("masked") or card.get("maskedCardNumber"),
            remaining_due=remaining,
            statement_close_date=statement_date or card.get("statementDate"),
            payment_due_date=card.get("paymentDueDate"),
            last_payment_amount=payment_amount,
            last_payment_date=payment_date,
        ))
    return facts


class HsbcCrawler(BankCrawler):
    USES_SHARED_LOGIN_CHECKPOINTS: ClassVar[bool] = True
    CREDENTIAL_HOSTS = frozenset({"card.hsbc.com.tw"})

    def __init__(self):
        super().__init__(name="hsbc")
        self.creds = HsbcCreds.load()

    def _host_filter(self) -> str:
        return "hsbc.com.tw"


    # ---------- 登入 ----------
    def login(self, page) -> bool:
        return self._shared_login(page)

    def prepare_login_page(self, page) -> None:
        page.wait_for_timeout(7000)
        self._logged_in(page)

    def is_authenticated(self, page) -> bool:
        return self._logged_in(page)

    def _logged_in(self, page) -> bool:
        try:
            current = urlparse(page.url or "")
            if (
                current.scheme.lower() != "https"
                or (current.hostname or "").lower() != "card.hsbc.com.tw"
                or current.port not in (None, 443)
                or current.username is not None
                or current.password is not None
                or (current.fragment or "").lower().startswith("/login")
            ):
                return False
            for selector in (SEL_USERID, SEL_PWD, SEL_CAPTCHA):
                controls = page.locator(selector)
                if any(
                    controls.nth(index).is_visible()
                    for index in range(controls.count())
                ):
                    return False
            body = page.locator("body").inner_text()
        except Exception:
            return False
        return (
            len(body) >= 300
            and (
                "登出" in body
                or re.search(r"(?<![A-Za-z])Logout(?![A-Za-z])", body, re.IGNORECASE)
                is not None
            )
            and (
                "我的卡片" in body
                or "卡片清單" in body
                or "信用卡" in body
                or re.search(r"(?<![A-Za-z])My Cards(?![A-Za-z])", body, re.IGNORECASE)
                is not None
            )
        )

    def login_checkpoint_rules(self) -> tuple[LoginCheckpointRule, ...]:
        all_phases = tuple(CheckpointPhase)
        post_settle = (
            CheckpointPhase.POST_SUBMIT,
            CheckpointPhase.POST_SUBMIT_SETTLE,
        )
        otp = re.compile(
            r"^[\s\S]{0,400}(?:OTP|一次性(?:密碼|驗證碼)|簡訊驗證碼|動態驗證碼|"
            r"裝置驗證|新裝置登入|信任此裝置)[\s\S]{0,400}$",
            re.IGNORECASE,
        )
        password = re.compile(
            r"^[\s\S]{0,200}(?:密碼(?:已)?(?:到期|過期)|"
            r"(?:立即|必須|請先|需要|需)\s*(?:修改|變更|重設)\s*(?:您的?)?\s*密碼|"
            r"強制\s*(?:修改|變更|重設)\s*(?:您的?)?\s*密碼)[\s\S]{0,200}$"
        )
        error = re.compile(
            r"^\s*(?:密碼不正確|帳號(?:已遭|已被|已)鎖定|登入失敗|"
            r"驗證碼(?:錯誤|不正確)，?請重新輸入|Invalid credentials|Account locked)"
            r"[\s。.!！?？:：,，]*$",
            re.IGNORECASE,
        )
        security_notice = re.compile(
            r"^(?![\s\S]*(?:異常登入|是否本人|裝置驗證|新裝置|OTP|驗證碼|條款|授權|確認交易))"
            r"(?=[\s\S]*資訊安全)(?=[\s\S]*密碼)[\s\S]{1,120}$"
        )
        modal_scopes = (("modal", ".modal.show"), ("dialog", "[role='dialog']"))
        alert_scopes = (
            ("error", ".error"),
            ("alert", ".alert"),
            ("role-alert", "[role='alert']"),
        )
        return (
            *(
                LoginCheckpointRule(
                    name=f"hsbc-otp-required-{suffix}", bank="hsbc",
                    phases=all_phases, kind=CheckpointKind.OTP_REQUIRED,
                    container_selector=selector, required_body_pattern=otp,
                )
                for suffix, selector in modal_scopes
            ),
            *(
                LoginCheckpointRule(
                    name=f"hsbc-password-change-required-{suffix}", bank="hsbc",
                    phases=all_phases, kind=CheckpointKind.PASSWORD_CHANGE_REQUIRED,
                    container_selector=selector, required_body_pattern=password,
                )
                for suffix, selector in modal_scopes
            ),
            *(
                LoginCheckpointRule(
                    name=f"hsbc-explicit-login-error-{suffix}", bank="hsbc",
                    phases=post_settle, kind=CheckpointKind.EXPLICIT_LOGIN_ERROR,
                    container_selector=selector, required_body_pattern=error,
                )
                for suffix, selector in alert_scopes
            ),
            LoginCheckpointRule(
                name="hsbc-security-notice",
                bank="hsbc",
                phases=post_settle,
                kind=CheckpointKind.DISMISSIBLE_NOTICE,
                container_selector="[role='dialog']",
                action_texts=("繼續",),
                required_body_pattern=security_notice,
            ),
            *(
                LoginCheckpointRule(
                    name=f"hsbc-unknown-{suffix}", bank="hsbc", phases=all_phases,
                    kind=CheckpointKind.UNKNOWN_BLOCKER, container_selector=selector,
                )
                for suffix, selector in modal_scopes
            ),
            *(
                LoginCheckpointRule(
                    name=f"hsbc-login-form-still-visible-{selector[1:]}", bank="hsbc",
                    phases=post_settle, kind=CheckpointKind.UNKNOWN_BLOCKER,
                    container_selector=selector,
                )
                for selector in (SEL_USERID, SEL_PWD, SEL_CAPTCHA)
            ),
        )

    @staticmethod
    def _visible_enabled(page, selector: str, *, optional: bool = False):
        matches = page.locator(selector)
        visible = [
            matches.nth(index)
            for index in range(matches.count())
            if matches.nth(index).is_visible()
        ]
        if not visible and optional:
            return None
        if len(visible) != 1 or not visible[0].is_enabled():
            raise HsbcLoginError("登入欄位無法安全確認；未送出登入")
        return visible[0]

    @staticmethod
    def _keyboard_fill(page, field, value: str) -> None:
        field.click()
        field.click(click_count=3)
        page.keyboard.press("Backspace")
        page.keyboard.type(value, delay=80)
        if len(field.input_value()) != len(value):
            raise HsbcLoginError("登入欄位輸入長度不符；未送出登入")

    @staticmethod
    def _exact_button(page, selector: str, text: str, *, candidate_only: bool = False):
        matches = page.locator(selector)
        eligible = []
        for index in range(matches.count()):
            button = matches.nth(index)
            if not button.is_visible() or not button.is_enabled():
                continue
            if candidate_only and " ".join(button.inner_text().split()) != text:
                continue
            eligible.append(button)
        if len(eligible) != 1 or " ".join(eligible[0].inner_text().split()) != text:
            raise HsbcLoginError("找不到唯一且可操作的登入按鈕；未送出登入")
        return eligible[0]

    @staticmethod
    def _captcha_image(page):
        images = page.locator("img")
        eligible = []
        for index in range(images.count()):
            image = images.nth(index)
            if not image.is_visible():
                continue
            src = image.get_attribute("src") or ""
            box = image.bounding_box()
            if (
                src.startswith("data:image/jpeg;base64,")
                and box is not None
                and 80 <= box["width"] <= 200
                and 25 <= box["height"] <= 60
            ):
                eligible.append(image)
        return eligible[0] if len(eligible) == 1 else None

    @classmethod
    def _stable_captcha(cls, page, previous_digest: bytes | None):
        last_digest = None
        for _ in range(12):
            image = cls._captcha_image(page)
            if image is None:
                return None
            raw = image.screenshot()
            digest = hashlib.sha256(raw).digest()
            if digest != previous_digest and digest == last_digest:
                return raw, digest
            last_digest = digest if digest != previous_digest else None
            page.wait_for_timeout(300)
        return None

    @staticmethod
    def _refresh_captcha(page) -> bool:
        buttons = page.locator("button[aria-label='Refresh Captcha']")
        eligible = [
            buttons.nth(index)
            for index in range(buttons.count())
            if buttons.nth(index).is_visible() and buttons.nth(index).is_enabled()
        ]
        if len(eligible) != 1:
            return False
        eligible[0].click()
        return True

    def _solve_captcha(self, page) -> str | None:
        previous_digest = None
        for attempt in range(8):
            try:
                stable = self._stable_captcha(page, previous_digest)
                if stable is None:
                    return None
                raw, previous_digest = stable
            except Exception:
                return None
            try:
                result = ocr_bytes(
                    raw, expected_len=5, alnum_only=True,
                    min_confidence=HSBC_CAPTCHA_MIN_CONFIDENCE,
                )
            except Exception:
                result = None
            if isinstance(result, str) and re.fullmatch(r"[A-Za-z0-9]{5}", result):
                return result
            try:
                if attempt == 7 or not self._refresh_captcha(page):
                    return None
            except Exception:
                return None
        return None

    @staticmethod
    def _response_visible(page) -> bool:
        for selector in (
            ".modal.show", "[role='dialog']", ".error", ".alert", "[role='alert']",
        ):
            matches = page.locator(selector)
            if any(matches.nth(index).is_visible() for index in range(matches.count())):
                return True
        return False

    def submit_credentials_once(self, page) -> None:
        try:
            user_id = self._visible_enabled(page, SEL_USERID, optional=True)
            if user_id is not None:
                self._keyboard_fill(page, user_id, self.creds.user_id)
                first = self._exact_button(
                    page, "button[data-testid='continueButton']", "繼續"
                )
                try:
                    first.click(timeout=8000)
                    page.wait_for_timeout(6000)
                except Exception:
                    raise HsbcLoginError("帳號階段狀態不明；未送出登入") from None
                if getattr(self, "_shared_dialog_blocked", False):
                    raise HsbcLoginError("帳號階段出現未分類提示；未送出登入")
                if self._response_visible(page):
                    raise HsbcLoginError("帳號階段出現未分類提示；未送出登入")

            password = self._visible_enabled(page, SEL_PWD)
            self._keyboard_fill(page, password, self.creds.password)
            captcha_text = self._solve_captcha(page)
            if captcha_text is None:
                raise HsbcLoginError("無法安全辨識驗證碼；未送出登入")
            captcha = self._visible_enabled(page, SEL_CAPTCHA)
            self._keyboard_fill(page, captcha, captcha_text)
            final = self._exact_button(
                page, "button[type='submit']", "繼續", candidate_only=True
            )
            if getattr(self, "_shared_dialog_blocked", False):
                raise HsbcLoginError("登入前出現未分類提示；未送出登入")
            if self._response_visible(page):
                raise HsbcLoginError("登入前出現未分類提示；未送出登入")
        except HsbcLoginError:
            raise
        except Exception:
            raise HsbcLoginError("登入欄位無法安全填寫；未送出登入") from None

        try:
            final.click(timeout=8000)
        except Exception:
            raise HsbcLoginError("登入送出狀態不明；禁止自動重試") from None

        try:
            for _ in range(22):
                page.wait_for_timeout(1000)
                if self._logged_in(page) or self._response_visible(page):
                    return
        except Exception:
            raise HsbcLoginError("登入送出後狀態無法安全確認；禁止自動重試") from None

    # ---------- 抓取 ----------
    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        """登入後抓信用卡：卡片清單(cards 或 legacy cards/suspend) + 逐卡明細。

        注意：HSBC 2026-07-09 實測已把卡片清單 endpoint 從 `cards/suspend`
        改成 `cards`。兩者 payload[] 都是各卡狀態；crawler 必須先認新端點再
        fallback 舊端點，否則會登入成功但 cards=[]，逐卡 posted/unposted 全不跑。
        明細用 cards/{id}/transactions/{posted,unposted} 直接 fetch。
        """
        out: dict = {}
        page.wait_for_timeout(7000)

        # 1) 卡片清單（dashboard 自動載入 cards；舊版曾用 cards/suspend）
        cards = self._card_list_payload(collector)
        out["cards"] = cards if isinstance(cards, list) else []
        _log(f"[collect] 卡片清單: {len(out['cards'])} 張")

        # 2) 逐卡點進去攔明細 API
        out["card_detail"] = self._collect_card_details(page, collector, out["cards"])

        out["_final_url"] = page.url
        out["_all_endpoints"] = sorted({
            h.url.split("?")[0].split("card.hsbc.com.tw/", 1)[-1]
            for h in collector.hits if h.resp_json and "card.hsbc.com.tw" in h.url
            and "/v3.1/" not in h.url
        })
        publish_card_bill_facts(out, _hsbc_card_bill_facts(out))
        return BankCollectResult(**out)

    @staticmethod
    def _card_list_payload(collector: ResponseCollector):
        """取 HSBC 卡片清單 payload；先認 2026 新 endpoint `cards`，再 fallback 舊 `suspend`。

        HSBC 2026-07-09 browser evidence：dashboard 已改打
        `ibk-bff/api/v1/cards`。舊 crawler 只讀 `suspend` 會 silent cards=[]，
        導致 posted/unposted 完全不抓。
        """
        cards = HsbcCrawler._latest_payload(collector, "cards")
        if isinstance(cards, list):
            return cards
        return HsbcCrawler._latest_payload(collector, "suspend")

    @staticmethod
    def _latest_payload(collector: ResponseCollector, endpoint: str):
        """取某 endpoint 最新成功回應的 payload（HSBC 格式 {success, payload, error}）。"""
        hits = [h for h in collector.hits
                if h.endpoint == endpoint and isinstance(h.resp_json, dict)
                and h.resp_json.get("success")]
        if hits:
            return hits[-1].resp_json.get("payload")
        return None

    def _collect_card_details(self, page, collector, cards: list) -> dict:
        """直接用 page fetch 打明細 API（帶 Bearer token，繞過點卡片導航）。

        已知 endpoint（實機攔到）：
          GET cards/{id}                              單卡詳情
          GET cards/{id}/transactions/posted?pageSize=10   已出帳（分頁）
          GET cards/{id}/transactions/unposted?pageSize=   未出帳（直接陣列）
        ⚠️ 裸 fetch 回 HTTP 500——必須帶前端的 `Authorization: Bearer <JWT>`（從 collector 攔到）。
        卡 id 從卡片清單 payload[].id 取（新 endpoint `cards`；legacy `cards/suspend`）。
        """
        token = getattr(collector, "auth_token", "") or ""
        _log(f"[collect] auth token: {'有' if token else '無'}（{token[:20]}…）")
        details: dict = {}
        for c in cards:
            cid = c.get("id")
            masked = c.get("maskedCardNumber", "")
            tail = masked.replace("-", "")[-4:] if masked else cid
            if not cid:
                continue
            entry: dict = {"card_id": cid, "masked": masked}
            # 單卡詳情
            entry["detail"] = self._fetch_json(page, f"cards/{cid}", token)
            # 已出帳（翻頁抓全）
            entry["posted"] = self._fetch_paged(page, cid, "posted", token)
            # 未出帳（陣列）
            unp = self._fetch_json(page, f"cards/{cid}/transactions/unposted?pageSize=200", token)
            unposted_rows = unp if isinstance(unp, list) else []
            entry["unposted_ok"] = isinstance(unp, list)
            entry["unposted"] = unposted_rows
            details[tail] = entry
            n_posted = len(entry["posted"])
            n_unp = len(entry["unposted"])
            _log(f"[collect] 卡 {tail}({cid}): 已出帳 {n_posted} 筆, 未出帳 {n_unp} 筆")
        return details

    @staticmethod
    def _fetch_json(page, path: str, token: str = ""):
        """在瀏覽器內 fetch HSBC API，帶 Authorization Bearer token（裸 fetch 會 500）。回 payload。"""
        try:
            return page.evaluate(
                "async ({p, tok}) => { try {"
                " const h={'Accept':'application/json'};"
                " if(tok) h['Authorization']=tok;"
                " const r=await fetch('https://card.hsbc.com.tw/ibk-bff/api/v1/'+p,"
                "   {credentials:'include', headers:h});"
                " const j=await r.json(); return j && j.success ? j.payload : null;"
                " } catch(e){ return null; } }",
                {"p": path, "tok": token},
            )
        except Exception as e:
            _log(f"[fetch] {path} 失敗: {e}")
            return None

    def _fetch_paged(self, page, cid: str, kind: str, token: str = "") -> list:
        """已出帳明細翻頁抓全。posted 回 {pageInfo:{totalPages,currentPageIndex}, content:[]}。

        ⚠️ 分頁參數實測（2026-06-10）：HSBC 用 **`pageNumber`**（0-based），不是 `page`！
        `page`/`pageIndex`/`offset`/`size`/大 `pageSize` **全部無效**（HSBC 忽略，每次回同一批
        前 10 筆）→ 若用錯參數逐頁抓，會把同 10 筆抓 N 次（曾誤抓出 410 筆=41 頁×重複 10 筆）。
        正解：`?pageSize=10&pageNumber=M`，並**逐頁比對防重**（某頁內容與上頁全同就停，雙保險）。
        """
        page_size = 10  # HSBC 每頁固定 10 筆
        all_rows: list = []
        prev_sig = None
        # 先抓第 0 頁拿 totalPages
        first = self._fetch_json(
            page, f"cards/{cid}/transactions/{kind}?pageSize={page_size}&pageNumber=0", token)
        if not isinstance(first, dict):
            return first if isinstance(first, list) else []
        rows0 = first.get("content", []) or []
        all_rows.extend(rows0)
        prev_sig = self._page_sig(rows0)
        pi = first.get("pageInfo") or {}
        total_pages = pi.get("totalPages", 1) or 1
        # 安全上限：避免 totalPages 異常導致無限抓（每頁 10 筆，500 頁=5000 筆足夠）
        total_pages = min(total_pages, 500)
        for pn in range(1, total_pages):
            nxt = self._fetch_json(
                page, f"cards/{cid}/transactions/{kind}?pageSize={page_size}&pageNumber={pn}", token)
            rows = nxt.get("content", []) if isinstance(nxt, dict) else []
            if not rows:
                break  # 空頁 = 抓完
            sig = self._page_sig(rows)
            if sig == prev_sig:
                _log(f"[fetch] {kind} pageNumber={pn} 與上頁相同，停止翻頁（防重複抓）")
                break
            all_rows.extend(rows)
            prev_sig = sig
            page.wait_for_timeout(400)
        return all_rows

    @staticmethod
    def _page_sig(rows: list) -> str:
        """一頁內容的簽章（用於偵測「翻頁回到同一批」的假象）。"""
        return "|".join(
            f"{r.get('description','')}_{r.get('transactionDate','')}_{r.get('ntdAmount','')}"
            for r in rows
        )


if __name__ == "__main__":
    import json
    crawler = HsbcCrawler()
    result = crawler.run(login_url=BASE, headless=False)
    out_file = Path(__file__).resolve().parents[1] / "data" / "hsbc_collected.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"\n[done] 已存: {out_file}")
    data = result.get("data", {})
    _log("\n===== 抓取摘要 =====")
    if result.get("error"):
        _log(f"  error: {result['error']} url={result.get('final_url')}")
    _log(f"  卡片: {len(data.get('cards', []))} 張")
    for tail, apis in (data.get("card_detail") or {}).items():
        _log(f"  卡 {tail}: {list(apis.keys())}")
    _log(f"  攔到的 endpoint: {data.get('_all_endpoints', [])}")
