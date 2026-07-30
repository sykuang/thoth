"""上海商銀 (SCSB) persist 邏輯.

從 persist.py 拆分而來 (2026-06-17, W persist.py split refactor)。
"""
from __future__ import annotations

import re
from datetime import datetime

from backend.core import account_classify, classify
from backend.core.store import BankStore
from backend.core.persist._common import _num, _num_real


def _scsb_page_error(text: str) -> bool:
    lower = text.lower()
    return any(marker in lower for marker in (
        "系統錯誤", "系統忙碌", "請稍後再試", "連線逾時", "連線已逾時", "請重新登入", "登入失效",
        "system error", "try again later", "session expired", "login required", "timed out",
        "timeout", "log in again", "login again", "unexpected error",
    ))


def _scsb_parse_card_rows(text: str, scope: str) -> list:
    """SCSB Credit Card 表格 row 解析。

    Real-Time Transaction Records 格式：
      Transaction Date / Transaction Time / Card Type / Last 4 / Merchant Name / Amount / ...

    使用者目前無 transaction 所以僅 header 在 — 但留 parser 給未來用。
    """
    rows = []
    if not text:
        return rows
    # 找出 header 後的 data block
    if "Transaction Date" not in text:
        return rows
    after_header = text.split("Transaction Date", 1)[1]
    # 用 regex 找：日期 \d{4}/\d{2}/\d{2} ... 然後抓金額 NT$ 或純數字
    for m in re.finditer(
        r"(\d{4}/\d{2}/\d{2})\s+(\d{2}:\d{2}(?::\d{2})?)?\s*([A-Za-z\s]{0,30})?\s*(\d{4})\s+([^\t\n]{1,60})\s+(NT\$\s*[\d,]+|[\d,]+\.\d+)",
        after_header,
    ):
        desc = (m.group(5) or "").strip()
        amt = _num((m.group(6) or "").replace("NT$", "").replace(",", "").strip())
        rows.append({
            "card_no": m.group(4),  # last 4 only
            "scope": scope,
            "date": m.group(1).replace("/", "-"),
            "desc": desc,
            "amount": amt,
            "currency": "TWD",
            "txn_type": classify.classify_by_desc_and_sign(desc, amt),
        })
    return rows

def persist_scsb(data: dict, store: BankStore, rules: list[dict] | None = None) -> dict:
    """SCSB collect() 結構 → store 表增量。

    第一版只入庫帳號與餘額（DOM regex 抽出來的），明細待 API 解密路線完成。
      accounts[] (從 overview_text regex) → accounts(UPSERT) + balance_history
      totals.twd_total                    → daily_metrics
    """
    today = datetime.now().strftime("%Y-%m-%d")
    delta: dict = {}

    # Telemetry 2026-06-18: 把 collector 抓到 / 沒抓到 的關鍵中介資料寫進
    # delta 讓 sync_jobs.result_summary 直接看 root cause, 不用 grep cloud log.
    # SCSB done=0 帳號真兇可能: (a) overview text 抓不到, (b) regex 沒命中, (c) login 假成功
    delta["telemetry"] = {
        "overview_text_len": len(data.get("overview_text") or ""),
        "twd_text_len": len(data.get("twd_text") or ""),
        "card_text_len": len(data.get("card_text") or ""),
        "accounts_extracted": len(data.get("accounts") or []),
        "totals": data.get("totals") or {},
        "final_url": data.get("_final_url"),
        # 2026-06-18: menu DOM inventory — 給「menu button 找不到」silent fail 看真結構
        "menu_dom_audit": (data.get("menu_dom_audit") or [])[:20],
    }

    # 帳戶 UPSERT + 餘額快照
    # raw_balance 直接帶進去 — scsb _extract_accounts 已抓 NT$13,065 / NT$0 / USD1.55
    # frontend 才能顯示真實 0 餘額（$0）跟貸款餘額，而非 fallback「—」
    # 用 _num_real 保留外幣小數（USD 1.55 不能截成 1）
    accts = data.get("accounts") or []
    if accts:
        store.upsert_accounts([
            {"account_no": a["account_no"], "currency": a.get("currency"),
             "branch": None, "nickname": None, "type": a.get("type_header"),
             "product_type": account_classify.classify_account("scsb", a),
             "raw_balance": _num_real(a.get("balance")),
             "raw_balance_date": today}
            for a in accts if a.get("account_no")
        ])
        # 餘額合計：排除貸款（loan/mortgage 不算 asset）
        def _is_asset_account(a: dict) -> bool:
            pt = account_classify.classify_account("scsb", a)
            return account_classify.is_asset_type(pt)
        twd_total = sum(_num(a["balance"]) or 0 for a in accts
                        if a.get("currency") in ("TWD", "新台幣")
                        and _is_asset_account(a))
        fx_total = sum(_num(a["balance"]) or 0 for a in accts
                       if a.get("currency") not in ("TWD", "新台幣")
                       and _is_asset_account(a))
        loan_total = sum(_num(a["balance"]) or 0 for a in accts
                         if a.get("currency") in ("TWD", "新台幣")
                         and account_classify.is_liability_type(
                             account_classify.classify_account("scsb", a)))
        store.upsert_balance_history([{
            "snapshotDate": today,
            "twdBalance": twd_total if twd_total else None,
            "fxBalance": fx_total if fx_total else None,
            "loanBalance": loan_total if loan_total else None,
        }])
        delta["balance_days"] = 1
        store.put_daily_metric("balance_latest",
                                {"twd": twd_total, "fx_raw": fx_total,
                                 "loan": loan_total, "n_accounts": len(accts)},
                                today)

    # totals from regex
    totals = data.get("totals") or {}
    if totals:
        store.put_daily_metric("scsb_totals", totals, today)

    # 全文 DOM dump 留快照（debug 用，看到底抓對沒抓對）
    overview = data.get("overview_text", "")
    if overview:
        # 只存前 2000 字摘要避免 DB 爆
        store.put_daily_metric("overview_text_preview", {"text": overview[:2000]}, today)

    # 預留位
    delta.setdefault("balance_days", 0)

    # --- 台幣交易明細（從 _collect_twd_inquiry 抓的 tab-separated 表格）---
    twd_inq = data.get("twd_inquiry") or {}
    twd_new = 0
    if isinstance(twd_inq, dict):
        # SCSB account number 沒 explicit 給，從 url/queries 推（或同 collect step 抓 dropdown 第一個）
        # 暫時 hard-code 用 inquiry data 內隱含的 account（_collect_twd_inquiry 沒回傳 acct_no）
        # 改進路：collect 時把 acct_no 一起回傳
        # 暫從 records 第一筆 remarks 或 result url 反推；本版用 'unknown' 並記到 raw
        rows = []
        for r in twd_inq.get("records") or []:
            if not isinstance(r, dict):
                continue
            amt_expense = _num(r.get("expense") or 0)
            amt_deposit = _num(r.get("deposit") or 0)
            rows.append({
                "account_no": twd_inq.get("account_no") or "unknown",
                "datetime": r.get("date"),
                "account_date": r.get("date"),
                "desc": r.get("summary"),
                "expend": amt_expense,
                "income": amt_deposit,
                "balance": _num(r.get("balance")),
                "counterparty_bank": None,
                "counterparty_acct": (r.get("remarks") or "")[:30] or None,
                "memo": r.get("remarks") or None,
            })
        if rows:
            twd_new = store.upsert_twd_txns(rows, rules=rules)
    delta["twd_txn_new"] = twd_new

    # 把 twd_inquiry 帳戶總計也存 daily_metrics（看歷史 expense/deposit total）
    if isinstance(twd_inq, dict) and (twd_inq.get("account_balance") or twd_inq.get("total_expenditure")):
        store.put_daily_metric("twd_inquiry_summary", {
            "account_no": twd_inq.get("account_no"),
            "account_balance": twd_inq.get("account_balance"),
            "available_balance": twd_inq.get("available_balance"),
            "total_expenditure": twd_inq.get("total_expenditure"),
            "total_deposit": twd_inq.get("total_deposit"),
            "record_count": len(twd_inq.get("records") or []),
        }, today)

    # --- 信用卡明細（設計規範：每家都要抓信用卡明細）---
    # SCSB collect 抓 3 leaves: unbilled / current / statement，皆為純 text 頁面
    card_inq = data.get("card_inquiry") or {}
    leaves = card_inq.get("leaves") if isinstance(card_inq, dict) else {}
    card_billed_new = card_unbilled = card_current = 0
    seen_card_nos: set[str] = set()

    if isinstance(leaves, dict):
        # 1) unbilled：「You currently have no new transactions」=  確認使用者目前無未入帳
        unb = leaves.get("unbilled", {})
        unb_text = (unb.get("text_final") or unb.get("text") or "")
        unb_nav_ok = isinstance(unb.get("nav"), dict) and unb["nav"].get("ok") is True
        unb_refreshed = False
        if unb_text and unb_nav_ok and not _scsb_page_error(unb_text):
            no_txn = "no new transactions" in unb_text.lower() or "have not yet been recorded" in unb_text.lower()
            store.put_daily_metric("scsb_card_unbilled", {
                "url": unb.get("url"),
                "empty": no_txn,
                "snippet": unb_text[unb_text.find("Unbilled Transaction Details"):][:600] if "Unbilled" in unb_text else unb_text[:600],
            }, today, commit=False)
            # 無未入帳 → refresh empty list 清掉舊 pending
            if no_txn:
                card_unbilled = store.refresh_card_pending(
                    "unbilled", [], rules=rules, fetch_ok=True, commit=False)
                unb_refreshed = True
            else:
                # 嘗試解析（未來若使用者有刷卡才會走到這）
                unb_rows = _scsb_parse_card_rows(unb_text, scope="unbilled")
                if unb_rows:
                    card_unbilled = store.refresh_card_pending(
                        "unbilled", unb_rows, rules=rules, fetch_ok=True,
                        commit=False)
                    unb_refreshed = True
                    for r in unb_rows:
                        if r.get("card_no"):
                            seen_card_nos.add(r["card_no"])

        if not unb_refreshed:
            card_unbilled = store.refresh_card_pending(
                "unbilled", [], rules=rules, fetch_ok=False, commit=False)

        # 2) current (即時 7 天)：表格 header 在 + 解析 data rows
        cur = leaves.get("current", {})
        cur_text = (cur.get("text_final") or cur.get("text") or "")
        cur_nav_ok = isinstance(cur.get("nav"), dict) and cur["nav"].get("ok") is True
        cur_refreshed = False
        if cur_text and cur_nav_ok and not _scsb_page_error(cur_text):
            lower_cur = cur_text.lower()
            explicit_empty = ("no real-time transaction" in lower_cur
                              or "no transaction records" in lower_cur)
            store.put_daily_metric("scsb_card_current", {
                "url": cur.get("url"),
                "snippet": cur_text[cur_text.find("Real-Time Transaction Records"):][:600] if "Real-Time" in cur_text else cur_text[:600],
            }, today, commit=False)
            cur_rows = _scsb_parse_card_rows(cur_text, scope="current")
            if cur_rows:
                card_current = store.refresh_card_pending(
                    "current", cur_rows, rules=rules, fetch_ok=True)
                cur_refreshed = True
                for r in cur_rows:
                    if r.get("card_no"):
                        seen_card_nos.add(r["card_no"])
            elif explicit_empty:
                card_current = store.refresh_card_pending(
                    "current", [], rules=rules, fetch_ok=True)
                cur_refreshed = True
        if not cur_refreshed:
            card_current = store.refresh_card_pending(
                "current", [], rules=rules, fetch_ok=False)

        # 3) statement：抽 account_no (A99999****) + 帳單金額 + 月份迭代
        stmt = leaves.get("statement", {})
        stmt_text = (stmt.get("text_final") or stmt.get("text") or "")
        if stmt_text:
            store.put_daily_metric("scsb_card_statement", {
                "url": stmt.get("url"),
                "snippet": stmt_text[stmt_text.find("Statement Inquiry"):][:1200] if "Statement Inquiry" in stmt_text else stmt_text[:1200],
            }, today, commit=False)
            # ⚠️ 2026-06-14 移除「從 statement 抽 masked account 當卡號」的邏輯：
            # 原 regex `[A-Z]\d{4,8}\*+` 會匹到身分證 masked (例 "A12651****"
            # = 身分證 A12651* + 4 顆星)，導致使用者 SCSB 沒辦任何信用卡，cards 表
            # 卻有一張幽靈卡。statement 頁面的 "Your account number" 永遠是
            # 身分證，從來都不是信用卡卡號。
            # 真實信用卡 masked 應該從 unbilled.text / current.text 的交易表格
            # 抽 (見上方兩個分支)，這裡完全不抽。

        # 2026-06-13 升級：statement 月份迭代資料
        # 把每月帳單摘要寫進 daily_metric（即使 due/paid 全 --- 也記錄為 0）
        months = stmt.get("months") if isinstance(stmt, dict) else []
        if months:
            months_summary = {}
            for m in months:
                mo = m.get("month")
                mo_text = m.get("text", "")
                if not mo or not mo_text:
                    continue
                # 抽 Current Period Total Amount Due / Bill Settlement Date
                # SCSB 帳單摘要全空 = '---'，有金額才能 parse
                def _grab_num(label, _text=mo_text):
                    """從 mo_text 抽指定 label 後第一個數字 (TWD)。"""
                    idx = _text.find(label)
                    if idx < 0:
                        return None
                    tail = _text[idx + len(label):idx + len(label) + 100]
                    nm = re.search(r"([0-9,]+(?:\.\d+)?)", tail)
                    if nm and nm.group(1) != "---":
                        try:
                            return int(float(nm.group(1).replace(",", "")))
                        except (ValueError, TypeError):
                            return None
                    return None

                due = _grab_num("Current Period Total Amount Due")
                min_pmt = _grab_num("Current Period Total Minimum Amount Due")
                months_summary[mo] = {
                    "due_amount": due,
                    "min_payment": min_pmt,
                    "has_data": due is not None,
                }
            if months_summary:
                store.put_daily_metric("scsb_card_statement_months", months_summary, today)

    # cards 表 UPSERT
    if seen_card_nos:
        rows = []
        for card_no in seen_card_nos:
            # masked like A99999**** 拿不到 last4，用整段 masked 當顯示
            display_no = card_no
            rows.append({
                "number": display_no,
                "name": f"SCSB 卡 {display_no}",
                "association": None,
                "type": "credit",
                "currency": "TWD",
            })
        store.upsert_cards(rows)
        delta["cards"] = len(seen_card_nos)

    delta["card_billed_new"] = card_billed_new
    delta["card_unbilled"] = card_unbilled
    delta["card_current"] = card_current

    store.log_sync(delta)
    return delta
