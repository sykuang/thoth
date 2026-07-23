"""LINE Bank persist 邏輯.

從 persist.py 拆分而來 (2026-06-17, W persist.py split refactor)。
"""
from __future__ import annotations

from datetime import datetime

from backend.core import account_classify
from backend.core.store import BankStore
from backend.core.persist._common import _num, _num_real


def persist_linebank(data: dict, store: BankStore, rules: list[dict] | None = None) -> dict:
    """LINE Bank → store 入庫。

    LINE Bank 是純位元銀行 (只有存款 + 簽帳金融卡, 無信用卡產品)。

    API 映射:
      api_responses['payables'][0].content.dpstAcctList[] → accounts (UPSERT) + balance_history
        - acctNbr: 帳號 (如 "900000077063")
        - acctNick: 暱稱 (如 "主帳戶")
        - acctBal: 即時餘額
        - cardNbr: 簽帳金融卡卡號

      api_responses['transactions'][N].content.txLst[] → twd_transactions (per account)
        - txDt: "20260528"  → 2026-05-28
        - txTm: "070641"     → 07:06:41
        - dpstWdrwDsCd: "1"=入帳, "2"=出帳
        - txAmt: 金額
        - afTxBal: 交易後餘額
        - bizTxFuncTpNm: 交易類型描述 ("轉帳" / "貸款還款")
        - txRmkCont: 備註 (對方資訊)

      api_responses['informations'][0].content → daily_metrics (custNm / nick, 不存敏感資料)
      api_responses['payables'][0].content     → daily_metrics (轉帳額度)

    隱私處理鐵律:
      ❌ 絕不存 natlId / brthDt / mbleTelNbr / emalAddr / 地址 / 公司 / 收入
      ✅ 只存顯示用的 nick + custNm
    """
    today = datetime.now().strftime("%Y-%m-%d")
    delta: dict = {"bank": "linebank", "scope": "structured"}

    apis = data.get("api_responses") or {}

    # ─── A. payables: 帳戶列表 + 餘額 ───
    accts: list[dict] = []
    twd_total = 0.0
    n_accounts = 0
    payables_hits = apis.get("payables") or []
    if payables_hits:
        pay_resp = payables_hits[0].get("resp") or {}
        pay_content = pay_resp.get("content") or {}
        dpst_list = pay_content.get("dpstAcctList") or []
        for a in dpst_list:
            if not isinstance(a, dict):
                continue
            acct_no = (a.get("acctNbr") or "").strip()
            if not acct_no:
                continue
            bal = _num(a.get("acctBal")) or 0
            # 餵 desc 含 pdNm + acctNick 給 keyword classifier，UNKNOWN 時 fallback deposit
            desc_hint = " ".join(filter(None, [a.get("acctNick"), a.get("pdNm"), "存款"]))
            raw = {**a, "desc": desc_hint, "currency": "TWD"}
            pt = account_classify.classify_account("linebank", raw)
            if pt == account_classify.ProductType.UNKNOWN:
                pt = account_classify.ProductType.DEPOSIT  # dpstAcctList = 存款帳戶清單
            accts.append({
                "account_no": acct_no,
                "currency": "TWD",
                "branch": None,
                "nickname": a.get("acctNick") or a.get("pdNm") or None,
                "type": a.get("pdNm") or None,        # 如 "主帳戶"
                "product_type": pt,
                "raw_balance": _num_real(a.get("acctBal")),
                "raw_balance_date": today,
            })
            twd_total += bal
            n_accounts += 1
        if accts:
            store.upsert_accounts(accts)

        # 轉帳額度 / 全戶餘額摘要 → daily_metrics
        store.put_daily_metric("linebank_payables_summary", {
            "total_balance": twd_total,
            "n_accounts": n_accounts,
            "daily_txfr_remaining": pay_content.get("custDylyTxfrRmngLmtAmt"),
            "monthly_txfr_remaining": pay_content.get("custMnlyTxfrRmngLmtAmt"),
        }, today)

    if accts:
        store.upsert_balance_history([{
            "snapshotDate": today,
            "twdBalance": int(twd_total) if twd_total else None,
            "fxBalance": None,
        }])
        delta["balance_days"] = 1

    # ─── B. transactions: 每帳戶的交易明細 ───
    twd_new = 0
    txn_hits = apis.get("transactions") or []
    for h in txn_hits:
        resp = h.get("resp") or {}
        content = resp.get("content") or {}
        acct_nbr = (content.get("acctNbr") or "").strip()
        if not acct_nbr:
            continue
        tx_lst = content.get("txLst") or []
        txns: list[dict] = []
        for t in tx_lst:
            if not isinstance(t, dict):
                continue
            tx_dt = t.get("txDt") or ""        # "20260528"
            tx_tm = t.get("txTm") or "000000"  # "070641"
            if len(tx_dt) != 8:
                continue
            iso_date = f"{tx_dt[:4]}-{tx_dt[4:6]}-{tx_dt[6:8]}"
            tx_tm_padded = str(tx_tm).zfill(6)
            iso_dt = f"{iso_date}T{tx_tm_padded[:2]}:{tx_tm_padded[2:4]}:{tx_tm_padded[4:6]}"

            dpst_wdrw = (t.get("dpstWdrwDsCd") or "").strip()
            amt = _num(t.get("txAmt")) or 0
            # "1" = 入帳 (income), "2" = 出帳 (expend)
            expend = amt if dpst_wdrw == "2" else None
            income = amt if dpst_wdrw == "1" else None
            bal_after = _num(t.get("afTxBal"))

            biz_nm = t.get("bizTxFuncTpNm") or ""
            rmk = t.get("txRmkCont") or ""
            desc = f"{biz_nm}: {rmk}".strip().strip(":").strip() if rmk else biz_nm

            txns.append({
                "account_no": acct_nbr,
                "datetime": iso_dt,
                "account_date": iso_date,
                "desc": desc or None,
                "expend": expend,
                "income": income,
                "balance": bal_after,
                "counterparty_bank": None,
                "counterparty_acct": None,
                "memo": t.get("txMemoVal") or None,
            })
        if txns:
            twd_new += store.upsert_twd_txns(txns, rules=rules)
    delta["twd_txn_new"] = twd_new

    # 信貸帳戶（loan_inferred）：拔除 — LINE Bank raw `api_responses` 沒有任何
    # loan endpoint，只有 payables/transactions/informations。看到 transactions
    # 內單筆「分期信貸」rmk 字串就合成假帳戶是錯的：(1) 那只是歷史一筆「貸款還款」
    # 交易紀錄不代表此戶有未結清信貸；(2) 連 raw_balance 都沒（不知剩餘本金多少）
    # → frontend 顯示「—」造視覺垃圾；(3) 違反「raw API 給不到實值就不入合成 row」
    # 鐵則（同 CTBC ctbc_loan_summary、Cathay per-card placeholder 同 class）。
    # 若使用者未來真有 LINE Bank 分期信貸而 LINE 開放 loan endpoint API，再回來
    # 寫真實 collector 抓 outstanding balance + 加正常 row。
    # （classify_linebank 的 _source=loan_inferred short-circuit 也同步拔除）

    # ─── C. 客戶資訊 (不存敏感欄位, 只留 暱稱/姓名 給 UI) ───
    info_hits = apis.get("informations") or []
    if info_hits:
        ic = (info_hits[0].get("resp") or {}).get("content") or {}
        store.put_daily_metric("linebank_profile", {
            "nick": ic.get("nick"),
            "cust_nm": ic.get("custNm"),
        }, today)

    # ─── D. 信用卡: LINE Bank 無信用卡產品, 跳過 ───
    delta["card_billed_new"] = 0
    delta["card_unbilled"] = 0
    delta["card_current"] = 0

    # ─── E. endpoint 地圖 (debug) ───
    endpoints = data.get("_all_endpoints") or sorted(apis.keys())
    if endpoints:
        store.put_daily_metric("linebank_endpoints", {"endpoints": endpoints}, today)

    delta.setdefault("balance_days", 0)
    store.log_sync(delta)
    return delta
