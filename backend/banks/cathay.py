#!/usr/bin/env python3
"""國泰世華 MyBank 全量抓取器。

繼承 BankCrawler。登入後造訪六大類功能頁，攔截式抓取所有資料 API，
parse 成結構化 dict。需互動的明細頁（台幣交易、刷卡明細）自動操作觸發。
"""
from __future__ import annotations

import sys
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
from backend.core.creds import CathayCreds
from backend.core.login_checkpoints import (
    CheckpointKind,
    CheckpointPhase,
    LoginCheckpointRule,
)

SEL_CUSTID = "#CustID"
SEL_USERID = "#UserIdKeyin"
SEL_PWD = "#PasswordKeyin"
SEL_LOGIN_BTN = "button.js-login"

BASE = "https://www.cathaybk.com.tw"

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


def _log(*a):
    print(*a, file=sys.stderr)


# W (2026-06-17): cathay 統一也 raise CathayLoginError, 跟其他 11 家一致.
# session 仍存活 fallback 仍回 True (避免重打鎖卡), 只在「絕對失敗」raise.
class CathayLoginError(RuntimeError):
    """Cathay 登入失敗（絕對失敗，重打也沒用，會鎖帳號）。"""


def _click_login_once(page) -> None:
    try:
        candidates = page.locator(SEL_LOGIN_BTN)
        if candidates.count() != 1:
            raise CathayLoginError("找不到唯一且可操作的登入按鈕；未送出登入")
        button = candidates.first
        classes = (button.get_attribute("class") or "").split()
        if not button.is_visible() or not button.is_enabled() or "disabled" in classes:
            raise CathayLoginError("找不到唯一且可操作的登入按鈕；未送出登入")
    except CathayLoginError:
        raise
    except Exception:
        raise CathayLoginError("無法安全確認登入按鈕；未送出登入") from None
    try:
        button.click(timeout=8000)
    except Exception:
        raise CathayLoginError("登入送出狀態不明；禁止自動重試") from None


def _cathay_card_bill_fact(out: dict):
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
    CREDENTIAL_HOSTS = frozenset({"www.cathaybk.com.tw"})

    def __init__(self):
        super().__init__(name="cathay")
        self.creds = CathayCreds.load()

    def _host_filter(self) -> str:
        return "cathaybk.com.tw"

    # ---------- positive login signal (W 2026-06-17) ----------
    def _logged_in(self, page) -> bool:
        """正向訊號：URL 在內銀區 + innerText >= 500 + 命中 >= 2 個菜單字 + 無 login form。

        取代原本只看 URL 含 OnlineBanking 的 negative-only 訊號，避免雲端
        fallback goto 失敗時誤判為已登入。詳見 SCSB 鐵律 wiki。
        """
        try:
            current = urlparse(page.url or "")
            if (
                current.scheme.lower() != "https"
                or (current.hostname or "").lower() != "www.cathaybk.com.tw"
                or current.port not in (None, 443)
                or current.username is not None
                or current.password is not None
            ):
                return False
            r = page.evaluate(JS_LOGGED_IN_POSITIVE)
        except Exception:
            return False
        if isinstance(r, dict):
            return bool(r.get("ok"))
        return bool(r)

    def login(self, page) -> bool:
        return self._shared_login(page)

    def prepare_login_page(self, page) -> None:
        page.wait_for_timeout(2500)

    def is_authenticated(self, page) -> bool:
        return self._logged_in(page)

    def login_checkpoint_rules(self) -> tuple[LoginCheckpointRule, ...]:
        return (
            LoginCheckpointRule(
                name="cathay-login-announcement",
                bank="cathay",
                phases=(CheckpointPhase.PRE_SUBMIT,),
                kind=CheckpointKind.DISMISSIBLE_NOTICE,
                container_selector="#divSystemLoginMsgList.show",
                action_texts=("下一", "我知道了", "關閉", "確定"),
                max_actions=12,
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
            page.wait_for_selector(SEL_CUSTID, state="visible", timeout=12000)
            for selector, value in (
                (SEL_CUSTID, self.creds.cust_id),
                (SEL_USERID, self.creds.user_id),
                (SEL_PWD, self.creds.password),
            ):
                fields = page.locator(selector)
                if fields.count() != 1:
                    raise CathayLoginError("登入欄位無法安全填寫；未送出登入")
                field = fields.nth(0)
                if not field.is_visible() or not field.is_enabled():
                    raise CathayLoginError("登入欄位無法安全填寫；未送出登入")
                field.click()
                field.click(click_count=3)
                page.keyboard.press("Backspace")
                page.keyboard.type(value, delay=80)
                page.wait_for_timeout(200)
                if len(field.input_value()) != len(value):
                    raise CathayLoginError("登入欄位輸入長度不符；未送出登入")
        except Exception:
            raise CathayLoginError("登入欄位無法安全填寫；未送出登入") from None
        _click_login_once(page)
        page.wait_for_timeout(9000)

    # ---------- 互動觸發：台幣交易明細 ----------
    def _trigger_twd_txn_query(self, page):
        """B0103 react-select：選帳號(第1個) + 期間(近30天) + 按查詢，觸發 B_ACCT_Q_TransferDetail。
        對每個帳號都查一次。"""
        page.wait_for_timeout(3000)

        def pick(label, downs=1):
            handle = page.evaluate(
                "((label) => { const gs=[...document.querySelectorAll('.chakra-form-control,[role=group]')];"
                "const g=gs.find(x=>x.textContent.trim().startsWith(label)); if(!g) return 'no-group';"
                "const inp=g.querySelector('input'); if(!inp) return 'no-input'; inp.focus(); return 'focused'; })",
                label,
            )
            if handle != "focused":
                return handle
            page.wait_for_timeout(400)
            for _ in range(downs):
                page.keyboard.press("ArrowDown")
                page.wait_for_timeout(250)
            page.keyboard.press("Enter")
            page.wait_for_timeout(800)
            return "ok"

        # 先數帳號下拉有幾個選項
        n_accts = page.evaluate(
            "(() => { const gs=[...document.querySelectorAll('.chakra-form-control,[role=group]')];"
            "const g=gs.find(x=>x.textContent.trim().startsWith('帳號')); if(!g) return 0;"
            "const inp=g.querySelector('input'); if(inp) inp.focus(); "
            "return document.querySelectorAll('[id*=react-select][id*=option]').length || 1; })()",
        )
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        n_accts = max(1, min(int(n_accts or 1), 10))
        _log(f"[twd_txn] 帳號選項數: {n_accts}")

        for idx in range(n_accts):
            pick("帳號", downs=idx + 1)
            pick("查詢期間", downs=2)  # 跳過「自行輸入」，選「近30天」
            clicked = page.evaluate(
                "(() => { const b=[...document.querySelectorAll('button')].filter(x=>x.offsetParent!==null)"
                ".find(x=>x.textContent.trim()==='查詢'); if(b){b.click(); return true;} return false; })()",
            )
            _log(f"[twd_txn] 帳號#{idx+1} 查詢: {clicked}")
            page.wait_for_timeout(6000)

    # ---------- 互動觸發：信用卡逐筆明細 ----------
    def _collect_card_details(self, page):
        """從 C0101 點三個明細連結，分別觸發：
        即時消費(C_BILL_Q_CardCurrentConsume) / 未出帳(C_BILL_Q_CardUnbilledConsume)
        / 已出帳逐筆(C_BILL_Q_RecentBillDetail)。
        """
        def click_link(text):
            return page.evaluate(
                "((t) => { const els=[...document.querySelectorAll('a,button,[role=button]')]"
                ".filter(b=>b.offsetParent!==null);"
                "const el=els.find(b=>b.textContent.trim().startsWith(t)||b.textContent.trim().includes(t));"
                "if(el){el.scrollIntoView(); el.click(); return el.textContent.trim().slice(0,20);} return null; })", text,
            )

        for label in ["即時消費明細", "未出帳明細", "帳單明細"]:
            try:
                page.goto(f"{BASE}/OnlineBanking/CQuery/C0101_BillOverview",
                          wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(5000)
                clicked = click_link(label)
                _log(f"[card_detail] 點 {label!r}: {clicked}")
                page.wait_for_timeout(6500)
                # 已出帳頁可能要再點查詢
                page.evaluate(
                    "(() => { const e=[...document.querySelectorAll('button')].filter(b=>b.offsetParent!==null)"
                    ".find(b=>/查詢|確定/.test(b.textContent)); if(e) e.click(); })()",
                )
                page.wait_for_timeout(3500)
            except Exception as e:
                _log(f"[card_detail] {label} 失敗: {str(e)[:80]}")

    # ---------- 主抓取 ----------
    def collect(self, page, collector: ResponseCollector) -> BankCollectResult:
        # 逐頁造訪，讓 React 自動打 API
        for key, path in FEATURE_PAGES.items():
            _log(f"\n[collect] {key}: {path}")
            try:
                page.goto(f"{BASE}{path}", wait_until="domcontentloaded", timeout=25000)
            except Exception as e:
                _log(f"  [warn] goto: {str(e)[:80]}")
            page.wait_for_timeout(6500)
            if key == "twd_txn":
                self._trigger_twd_txn_query(page)
        # 信用卡逐筆明細（三種）
        self._collect_card_details(page)

        return BankCollectResult(**self._parse(collector))

    # ---------- Parse ----------
    def _c(self, hit):
        """取 hit 的 content（成功才回）。"""
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
        if not isinstance(row, dict):
            return row
        normalized = dict(row)
        for key in ("snapshotDate", "date", "snapshot_date"):
            if key in normalized:
                normalized[key] = self._normalize_iso_date(normalized[key])
        return normalized

    def _parse(self, col: ResponseCollector) -> dict:
        out: dict = {}

        # 帳戶清單
        acc = self._c(col.latest("G_CUST_Q_TransAccountList"))
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
        bl = self._c(col.latest("G_CUST_Q_BalanceLevel"))
        if bl and isinstance(bl, dict):
            lst = bl.get("dateBalanceList", []) or []
            normalized = [self._normalize_balance_row(r) for r in lst if isinstance(r, dict)]
            if normalized:
                out["balance_latest"] = normalized[-1]
                out["balance_history"] = normalized

        # 各類現值
        for label, ep, keys in [
            ("foreign", "G_CUST_Q_ForeignNetPresentInfo", ["hasAccount", "twdBalance"]),
            ("invest", "G_CUST_Q_InvestNetPresentInfo", ["totalRateOfReturn", "totalPresentValue"]),
            ("insurance", "G_CUST_Q_InsNetPresentInfo", ["isRegistered", "sumAssets"]),
            ("loan", "G_CUST_Q_LoanNetPresentInfo", ["netPresent"]),
        ]:
            c = self._c(col.latest(ep))
            if c and isinstance(c, dict):
                out.setdefault("net_present", {})[label] = {k: c.get(k) for k in keys}

        # 信用卡
        card: dict = {}
        cl = self._c(col.latest("C_CardInfo_Q_MyCardList"))
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
        lb = self._c(col.latest("C_CardInfo_Q_LatestBill"))
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
        nb = self._c(col.latest("C_CardInfo_Q_NextBill"))
        if nb and isinstance(nb, dict):
            card["next_bill"] = {
                "twd": nb.get("twdNextBillInfo"),
                "usd": nb.get("usdNextBillInfo"),
            }
        cq = self._c(col.latest("C_CardInfo_Q_CardQuota"))
        if cq and isinstance(cq, dict):
            card["quota"] = {
                "available": cq.get("realTimeAvailAmount"),
                "credit_limit": cq.get("creditLimitAmount"),
                "current": cq.get("currentAmount"),
            }
        cr = self._c(col.latest("C_CardInfo_Q_CardReward"))
        if cr and isinstance(cr, dict):
            card["reward_points"] = cr.get("pointCurrent")
        tc = self._c(col.latest("G_CUST_Q_CardTotalConsumption"))
        if tc and isinstance(tc, dict):
            card["total_consumption"] = {
                "current_balance": tc.get("currentBalance"),
                "unpaid": tc.get("unpaidAmount"),
                "last_stmt_date": tc.get("lastStmtDate"),
            }
        if card:
            out["credit_card"] = card

        # 投資
        inv = self._c(col.latest("E_INFO_Q_InvestOverview"))
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
        fund = self._c(col.latest("F_SET_Q_ProductAdvanceDetail"))
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
        ins: dict = {}
        ia = self._c(col.latest("I_POLICY_Q_CathayLifeAssetInfo"))
        if ia and isinstance(ia, dict):
            ins["life_asset"] = {
                "sum_asset": ia.get("sumAsset"),
                "invest_cost": ia.get("totalInvestCost"),
                "invest_pnl": ia.get("totalInvestPnl"),
                "invest_roi": ia.get("totalInvestRoi"),
            }
        ip = self._c(col.latest("I_POLICY_Q_CathayInsurancePolicy"))
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
        if ins:
            out["insurance"] = ins

        # 貸款
        lo = self._c(col.latest("L_ACCT_Q_OverView"))
        if lo and isinstance(lo, dict):
            out["loan"] = {"status": lo.get("status"), "accounts": lo.get("accountList")}

        # ===== 信用卡逐筆明細（三種）=====
        card_tx = out.setdefault("credit_card", {})

        # 已出帳逐筆明細
        rbd = self._c(col.latest("C_BILL_Q_RecentBillDetail"))
        if rbd and isinstance(rbd, dict):
            card_tx["billed_detail"] = self._parse_bill_detail(rbd)

        # 未出帳逐筆消費
        unb = self._c(col.latest("C_BILL_Q_CardUnbilledConsume"))
        if unb and isinstance(unb, dict):
            card_tx["unbilled_detail"] = self._parse_consume(unb)

        # 即時消費明細
        cur = self._c(col.latest("C_BILL_Q_CardCurrentConsume"))
        if cur and isinstance(cur, dict):
            card_tx["current_detail"] = self._parse_consume(cur)

        # 帳單彙總（含繳款期限、額度循環）
        bi = self._c(col.latest("C_BILL_Q_BillInfo"))
        if bi and isinstance(bi, dict):
            card_tx["bill_summary"] = {
                "payment_deadline": bi.get("paymentDeadline"),
                "currencies": bi.get("currencyBillInfoList"),
            }

        # ===== 台幣活存交易明細（B_ACCT_Q_TransferDetail）=====
        # 互動查詢後攔到，可能多帳號各一筆 hit，全部蒐集
        twd_txns = []
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
                    "transactions": [
                        {
                            "datetime": t.get("txnDateTime"),
                            "account_date": t.get("accountDate"),
                            "desc": t.get("description"),
                            "expend": t.get("expendAmt"),
                            "income": t.get("incomeAmt"),
                            "balance": t.get("balance"),
                            "counterparty_bank": t.get("expendBankId"),
                            "counterparty_acct": self.mask_card(t.get("expendAcctNo")) if t.get("expendAcctNo") else None,
                            "memo": t.get("memo"),
                        }
                        for t in details
                    ],
                })
        if twd_txns:
            out["twd_transactions"] = twd_txns

        publish_card_bill_facts(out, [_cathay_card_bill_fact(out)])

        # 全部攔到的 endpoint 清單（debug 用）
        out["_all_endpoints"] = sorted({h.endpoint for h in col.hits if h.resp_json})
        return out

    # ---------- 信用卡明細 parser ----------
    def _parse_bill_detail(self, content: dict) -> dict:
        """C_BILL_Q_RecentBillDetail: twd/usd/cny BillDetailInfo[].tradeData[]"""
        result = {}
        for cur_key, label in [("twdBillDetailInfo", "TWD"), ("usdBillDetailInfo", "USD"), ("cnyBillDetailInfo", "CNY")]:
            info = content.get(cur_key)
            if not isinstance(info, list):
                continue
            txns = []
            for block in info:
                for t in (block.get("tradeData") or []):
                    txns.append(self._norm_card_txn(t))
            if txns:
                result[label] = txns
        return result

    def _parse_consume(self, content: dict) -> dict:
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
                    result[k] = [self._norm_card_txn(t) for t in v
                                 if not self._is_placeholder_consume_row(t)]
                # 若元素含 tradeData
                elif "tradeData" in v[0]:
                    txns = []
                    for block in v:
                        for t in (block.get("tradeData") or []):
                            if self._is_placeholder_consume_row(t):
                                continue
                            txns.append(self._norm_card_txn(t))
                    if txns:
                        result[k] = txns
        return result

    @staticmethod
    def _is_placeholder_consume_row(t: dict) -> bool:
        """raw consume row 是 NULL placeholder（amount 全空 + desc 全空）→ filter。

        2026-06-22 Bug 5: cathay current/unbilled API 開頭塞空殼 row。
        判定條件：amount key 不存在或 None，且 desc/transDesc 不存在或全空白字串。

        注意 amount 用 None 判，不用 falsy（0 是合法 — refund / 紅利折抵）。
        """
        amt = t.get("amount") if "amount" in t else None
        if amt is None:
            amt = t.get("transAmount")
        desc = t.get("desc") or t.get("transDesc") or t.get("description") or ""
        return amt is None and not str(desc).strip()

    def _norm_card_txn(self, t: dict) -> dict:
        """正規化單筆信用卡 billed 交易。

        2026-06-13 修：consume_currency 空字串 → 'TWD' (避免 DB 髒資料)。
        Cathay 帳單 API 對台幣消費根本沒給 consumeCurrency 欄,
        或給空字串 ''——必須正規化才不會污染跨銀行外幣 query。

        規則:
          • consumeCurrency 非空 + consumeAmount 非零 → 視為外幣，原值保留
          • 否則 → consume_currency='TWD', consume_amount=None
          • consumeCountry='' → None
        """
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

        return {
            "card_no": self.mask_card(t.get("cardNo") or t.get("mobileCardNo")),
            "date": t.get("consumeDate") or t.get("transDate"),   # 消費日
            # 入帳日：國泰用 beginValueDate(折算入帳日) / convertDate(折算日)；
            # 三個獨立來源欄位都沒有時保留 None，shared store 不得偽造。
            "post_date": t.get("beginValueDate") or t.get("convertDate") or t.get("postingDate"),
            "desc": t.get("transDesc"),
            "amount": t.get("amount"),                            # 台幣入帳金額
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
