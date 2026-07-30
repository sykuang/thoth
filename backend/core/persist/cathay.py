"""國泰世華 (Cathay) persist 邏輯.

從 persist.py 拆分而來 (2026-06-17, W persist.py split refactor)。
"""
from __future__ import annotations

from datetime import datetime

from backend.core import account_classify, classify
from backend.core.store import BankStore
from backend.core.persist._common import _num_real, _num_to_float


def persist_cathay(data: dict, store: BankStore, rules: list[dict] | None = None) -> dict:
    """增量寫入，回傳各類變更計數。"""
    today = datetime.now().strftime("%Y-%m-%d")
    delta = {}

    # 帳戶 / 卡片當前狀態（UPSERT）
    main_accts = data.get("accounts") or []
    # 主存款帳戶：用 classifier 重判 product_type（原 collector 寫死的覆蓋掉）
    for a in main_accts:
        a["product_type"] = account_classify.classify_account("cathay", a)
    # raw_balance 來源：cathay collector 沒抓 per-account balance，但
    # balance_latest.twdBalance 是當前主帳戶餘額（cathay 目前只有 1 個主存款
    # 帳號，所以 sum == per-account；未來若 collector 升級支援多主帳戶，
    # 這條 fallback 要改成 per-account map 對應）。
    bl = data.get("balance_latest") or {}
    bl_twd = bl.get("twdBalance")
    bl_fx = bl.get("fxBalance")
    bl_snap = bl.get("snapshotDate")
    bl_date = bl_snap[:10] if isinstance(bl_snap, str) and len(bl_snap) >= 10 else today
    # 用 currency 區分：TWD 帳戶吃 twdBalance、外幣帳戶吃 fxBalance（皆 limited single-account 假設）
    tw_main_count = sum(1 for a in main_accts if (a.get("currency") or "TWD").upper() == "TWD")
    fx_main_count = sum(1 for a in main_accts if (a.get("currency") or "TWD").upper() != "TWD")
    # C-6 (2026-06-17): 若實際出現 multi-main，silent 跳過 raw_balance 太危險——加 logging
    if tw_main_count > 1 or fx_main_count > 1:
        import logging as _logging
        _log = _logging.getLogger(__name__)
        _log.warning(
            "[cathay] multi-main accounts detected (tw=%d, fx=%d) — raw_balance fallback skipped。"
            "balance_latest.twdBalance 是聚合值，無法分配到個別 main account。"
            "請升級 cathay collector 抓 per-account balance map。",
            tw_main_count, fx_main_count,
        )
    for a in main_accts:
        cur = (a.get("currency") or "TWD").upper()
        if cur == "TWD" and bl_twd is not None and tw_main_count == 1:
            a["raw_balance"] = float(bl_twd)
            a["raw_balance_date"] = bl_date
        elif cur != "TWD" and bl_fx is not None and fx_main_count == 1:
            a["raw_balance"] = float(bl_fx)
            a["raw_balance_date"] = bl_date
    # 貸款帳戶：data.loan.accounts[] 獨立路徑（使用者鐵律：所有爬蟲都要處理貸款）
    loan_block = data.get("loan") or {}
    loan_accts_raw = loan_block.get("accounts") or []
    loan_accts: list[dict] = []
    for la in loan_accts_raw:
        if not isinstance(la, dict):
            continue
        # 國泰 loan accountList 欄位多變：accountNo / loanAcct / 等
        acct_no = (la.get("accountNo") or la.get("loanAcct")
                   or la.get("accountId") or la.get("account_no"))
        if not acct_no:
            continue
        raw = {**la, "_source": "loan_accounts"}
        # loan balance defensive：試多個欄位
        loan_bal_raw = (la.get("balance") or la.get("loanBalance")
                        or la.get("outstanding") or la.get("amount"))
        loan_accts.append({
            "account_no": acct_no,
            "currency": la.get("currency") or "TWD",
            "branch": la.get("branchName") or la.get("branch"),
            "nickname": la.get("nickName") or la.get("nickname"),
            "type": (la.get("loanName") or la.get("name")
                     or la.get("product") or la.get("type") or "貸款"),
            "product_type": account_classify.classify_account("cathay", raw),
            "raw_balance": _num_real(loan_bal_raw),
            "raw_balance_date": today,
        })
    all_accts = main_accts + loan_accts
    if all_accts:
        store.upsert_accounts(all_accts)
    cc = data.get("credit_card", {})
    if cc.get("cards"):
        # Step 2 (2026-06-14): Cathay quota / bill_summary 是整戶層 (不是 per-card),
        # 套到每張卡同一組值 (limit 是「整戶可用額度」, due_date/stmt_date 跨卡共用).
        # 國泰 raw API 設計就是整戶合併出帳 (`billed_detail.TWD[].card_no=''` 證實),
        # 沒有 per-card endpoint 可打;整戶套是目前可行的最大化解析,等國泰 API 升級才能改善.
        quota = cc.get("quota") or {}
        cathay_limit = _num_to_float(quota.get("credit_limit"))
        cathay_used = _num_to_float(quota.get("current"))  # current=本期已動用
        bill_summary = cc.get("bill_summary") or {}
        # payment_deadline = '2026-05-05T00:00:00' → 切 'T' 取 YYYY-MM-DD
        deadline_raw = bill_summary.get("payment_deadline") or ""
        cathay_due = deadline_raw.split("T")[0] if "T" in deadline_raw else (deadline_raw or None)
        # billDate 同樣處理
        currencies = bill_summary.get("currencies") or []
        bill_date_raw = (currencies[0].get("billDate") if currencies else "") or ""
        cathay_stmt = bill_date_raw.split("T")[0] if "T" in bill_date_raw else (bill_date_raw or None)
        # 2026-07-02: bill_summary.currencies[0].paymentAmount 只是帳單彙總
        # 「上期已繳金額」，沒有真實繳款日；不能單獨生成 last_payment。
        # 真正最近繳款必須來自 billed_detail.TWD 的「本行自動扣繳」row。
        cathay_bill_due = None
        cathay_last_pay_amt = None
        if currencies and isinstance(currencies[0], dict):
            cur0 = currencies[0]
            cathay_bill_due = _num_to_float(cur0.get("currentPaymentAmount"))

        # 2026-06-23 v3 (使用者「你 cathay 有一頁一頁看嗎」, 逐項 dump billed_detail.TWD 發現):
        # billed_detail.TWD 內有 records 帶「本行自動扣繳」desc + post_date 真實上次繳款日!
        # 範例:
        #   {"desc": "本行自動扣繳", "amount": -2130, "post_date": "2026-04-08T..."}
        # 找最新一筆「本行自動扣繳」(或「自動扣繳」keyword) 取 post_date 寫 last_payment_date.
        # 之前 audit 漏看 billed_detail — collector OK, persist 沒讀 (跟 0.3.25 ubot 同 pattern).
        cathay_last_pay_date = None
        billed_twd = (cc.get("billed_detail") or {}).get("TWD") or []
        if isinstance(billed_twd, list):
            pay_records = [
                r for r in billed_twd
                if isinstance(r, dict) and r.get("post_date")
                and isinstance(r.get("desc"), str)
                and ("自動扣繳" in r["desc"] or "繳款" in r["desc"] or "已繳" in r["desc"])
                and isinstance(r.get("amount"), (int, float)) and r["amount"] < 0
            ]
            if pay_records:
                # 新→舊排序 (post_date 字串字典序對齊時序), 取最新一筆
                latest = max(pay_records, key=lambda r: r.get("post_date", ""))
                pd = latest.get("post_date", "")
                # ISO format 'YYYY-MM-DDTHH:MM:SS' → 'YYYY-MM-DD'
                cathay_last_pay_date = pd.split("T")[0] if "T" in pd else pd
                # 同時更新 last_payment_amount (用此筆絕對值, 比 paymentAmount 更精準)
                amt_abs = abs(latest.get("amount", 0))
                if amt_abs > 0:
                    cathay_last_pay_amt = float(amt_abs)

        # 套到每張卡 (覆蓋舊值)
        for card in cc["cards"]:
            card["credit_limit"] = cathay_limit
            card["used_credit"] = cathay_used
            card["statement_close_date"] = cathay_stmt
            card["payment_due_date"] = cathay_due
            card["bill_due_amount"] = cathay_bill_due
            card["last_payment_amount"] = cathay_last_pay_amt
            # 2026-06-23 v3: 從 billed_detail.TWD 找「本行自動扣繳」record post_date
            card["last_payment_date"] = cathay_last_pay_date
        store.upsert_cards(cc["cards"])

    # 餘額走勢（同日 UPSERT，跨日累積）
    if data.get("balance_history"):
        delta["balance_days"] = store.upsert_balance_history(data["balance_history"])

    # 台幣已過帳交易（append-only，回真正新增筆數）
    # 帳號正規化：cathay collector 在 twd_transactions[].account 加 4 個前綴 0
    # （'0000900000057055' vs accounts.account_no '900000057055'），
    # lstrip("0") 對齊讓 portfolio _bank_accounts 的 txn_balances lookup 找得到。
    twd_new = 0
    for acct in data.get("twd_transactions", []):
        acct_no_raw = acct.get("account") or ""
        # 去掉前綴 0；但若全 0 或變空字串就用原值（防呆）
        acct_no = acct_no_raw.lstrip("0") if acct_no_raw else acct_no_raw
        if not acct_no:
            acct_no = acct_no_raw
        txns = acct.get("transactions", [])
        # 外層帳號帶進每筆交易（交易本身沒有 account_no）
        for t in txns:
            t.setdefault("account_no", acct_no)
        twd_new += store.upsert_twd_txns(txns, rules=rules)
    delta["twd_txn_new"] = twd_new

    # 信用卡已出帳明細（append-only）
    # 2026-06-20 修：filter「上期帳單總額」「上期應繳金額」這類 summary header row
    # —— Cathay 帳單 API 把帳單頂端的「上期」摘要列也當交易回 (date=None, post_date=None,
    # card_no='')，這 row 不是真實交易，是月結頁面的開頭小計。
    # 判定條件：consume_date 跟 post_date 同時 NULL → 一定不是交易（真實刷卡至少有消費日）。
    billed = cc.get("billed_detail") or {}
    billed_new = 0
    billed_skipped_summary = 0
    for _cur, txns in billed.items():
        real_txns = []
        for t in txns:
            consume_date = t.get("date")
            post_date = t.get("post_date")
            if not consume_date and not post_date:
                billed_skipped_summary += 1
                continue
            desc = (t.get("desc") or "").strip()
            amt = t.get("amount")
            t["txn_type"] = classify.classify_by_desc_and_sign(desc, amt)
            real_txns.append(t)
        billed_new += store.upsert_card_billed(real_txns, rules=rules)
    delta["card_billed_new"] = billed_new
    if billed_skipped_summary:
        delta["card_billed_skipped_summary"] = billed_skipped_summary

    # 信用卡未出帳 / 即時（refresh-by-scope）
    # 2026-06-22 Bug 5: persist 層對稱 filter NULL placeholder row（amount 全空 + desc 全空）。
    # 主要 filter 在 collector `_parse_consume`（治本），這層補一道防禦 + telemetry。
    # 物理 invariant: 真實刷卡至少要有金額或描述。
    # 詳見 wiki [[card-billed-pending-cross-table-consistency-lesson]] Bug 5。
    unb_raw = cc.get("unbilled_detail")
    unb = unb_raw if isinstance(unb_raw, dict) else {}
    unb_ok = (isinstance(unb_raw, dict)
              and not any(unb_raw.get(key) for key in ("error", "Error", "errorMessage"))
              and any(
        isinstance(value, list) and (
            "consume" in str(key).lower()
            or (value and isinstance(value[0], dict)
                and any(field in value[0] for field in ("amount", "desc", "date")))
        )
        for key, value in unb_raw.items()
    ))
    unb_txns_raw = [t for lst in unb.values() if isinstance(lst, list) for t in lst]
    unb_skipped = 0
    unb_txns = []
    for t in unb_txns_raw:
        desc = (t.get("desc") or "").strip()
        amt = t.get("amount")
        if amt is None and not desc:
            unb_skipped += 1
            continue
        t["txn_type"] = classify.classify_by_desc_and_sign(desc, amt)
        unb_txns.append(t)
    # fetch_ok: collector 明確帶回 unbilled_detail dict 才算可信；空 dict 是成功零筆，
    # key 缺失／非 dict 才是抓取失敗，必須保留舊 pending。
    delta["card_unbilled"] = store.refresh_card_pending(
        "unbilled", unb_txns, rules=rules,
        fetch_ok=unb_ok,
        commit=False)
    if unb_skipped:
        delta["card_unbilled_skipped_placeholder"] = unb_skipped

    cur_raw = cc.get("current_detail")
    cur_d = cur_raw if isinstance(cur_raw, dict) else {}
    cur_ok = (isinstance(cur_raw, dict)
              and not any(cur_raw.get(key) for key in ("error", "Error", "errorMessage"))
              and any(
        isinstance(value, list) and (
            "consume" in str(key).lower()
            or (value and isinstance(value[0], dict)
                and any(field in value[0] for field in ("amount", "desc", "date")))
        )
        for key, value in cur_raw.items()
    ))
    cur_txns_raw = [t for lst in cur_d.values() if isinstance(lst, list) for t in lst]
    cur_skipped = 0
    cur_txns = []
    for t in cur_txns_raw:
        desc = (t.get("desc") or "").strip()
        amt = t.get("amount")
        if amt is None and not desc:
            cur_skipped += 1
            continue
        t["txn_type"] = classify.classify_by_desc_and_sign(desc, amt)
        cur_txns.append(t)
    # current_detail 同樣以 key 存在＋dict type 判可信，空 dict 可安全清 stale current。
    delta["card_current"] = store.refresh_card_pending(
        "current", cur_txns, rules=rules,
        fetch_ok=cur_ok)
    if cur_skipped:
        delta["card_current_skipped_placeholder"] = cur_skipped

    # 每日數值快照（同日覆蓋，跨日保留時序）
    store.put_daily_metric("net_present", data.get("net_present"), today)
    store.put_daily_metric("investment", data.get("investment"), today)
    store.put_daily_metric("insurance", data.get("insurance"), today)
    store.put_daily_metric("loan", data.get("loan"), today)
    store.put_daily_metric("balance_latest", data.get("balance_latest"), today)
    # 信用卡彙總（額度/紅利/未出帳/最近帳單）
    card_summary = {
        k: cc.get(k) for k in ["quota", "reward_points", "next_bill", "latest_bill",
                               "total_consumption", "bill_summary"] if cc.get(k) is not None
    }
    if card_summary:
        store.put_daily_metric("card_summary", card_summary, today)

    store.log_sync(delta)
    return delta
