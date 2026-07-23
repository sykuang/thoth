"""星展台灣 (DBS) persist 邏輯.

從 persist.py 拆分而來 (2026-06-17, W persist.py split refactor)。
"""
from __future__ import annotations

from datetime import datetime

from backend.core import account_classify
from backend.core.store import BankStore
from backend.core.persist._common import _num_real, _num_to_float, _slash_date_to_iso


def _dbs_money(value) -> float | None:
    """DBS money object / scalar → float | None."""
    if isinstance(value, dict):
        for key in ("balance", "domesticCurrencyBalance", "displayBalance", "amount", "value"):
            n = _num_to_float(value.get(key))
            if n is not None:
                return n
        return None
    return _num_to_float(value)


def _dbs_date(value) -> str | None:
    """DBS date dict / ISO-ish string → YYYY-MM-DD."""
    if isinstance(value, dict):
        value = value.get("value")
    if not value:
        return None
    s = str(value).strip()
    if "T" in s:
        s = s.split("T", 1)[0]
    return _slash_date_to_iso(s)


def _parse_dbs_twd_transactions(apis: dict, account_by_global_id: dict[str, str]) -> list[dict]:
    """DBS transactions-history/inquiry responses → twd_transactions rows.

    Current使用者帳戶 raw 是空陣列；parser 先鎖 endpoint envelope + flexible field
    mapping，將來有交易時不需要再改 collector。
    """
    rows: list[dict] = []
    for hit in apis.get("inquiry") or []:
        url = hit.get("url") or ""
        if "transactions-history/inquiry" not in url:
            continue
        req = hit.get("req_body") or {}
        resp = hit.get("resp") or {}
        global_id = str(req.get("globalAccountId") or "")
        account_no = account_by_global_id.get(global_id) or global_id
        for t in resp.get("transactions") or []:
            if not isinstance(t, dict) or not account_no:
                continue
            post_date = None
            for key in ("accountingDate", "postingDate", "postDate", "valueDate", "bookingDate", "transactionDate", "txnDate", "date"):
                post_date = _dbs_date(t.get(key))
                if post_date:
                    break
            txn_date = None
            for key in ("transactionDate", "txnDate", "effectiveDate", "valueDate", "postingDate", "accountingDate", "date"):
                txn_date = _dbs_date(t.get(key))
                if txn_date:
                    break
            desc = None
            for key in ("transactionCategory", "transactionType", "txnType", "type", "description", "transactionDescription"):
                if t.get(key):
                    desc = str(t.get(key)).strip()
                    break
            memo = None
            for key in ("remarks", "remark", "memo", "narrative", "transactionRemark", "transactionDescription", "description"):
                if t.get(key):
                    memo = str(t.get(key)).strip()
                    break
            amount = None
            for key in ("transactionAmount", "amount", "txnAmount", "originalAmount"):
                amount = _dbs_money(t.get(key))
                if amount is not None:
                    break
            debit = None
            for key in ("debitAmount", "withdrawalAmount", "outAmount"):
                debit = _dbs_money(t.get(key))
                if debit is not None:
                    break
            credit = None
            for key in ("creditAmount", "depositAmount", "inAmount"):
                credit = _dbs_money(t.get(key))
                if credit is not None:
                    break
            balance = None
            for key in ("balance", "accountBalance", "runningBalance", "availableBalance", "balanceAfterTransaction"):
                balance = _dbs_money(t.get(key))
                if balance is not None:
                    break
            direction = " ".join(str(t.get(k) or "") for k in (
                "debitCreditIndicator", "creditDebitIndicator", "transactionIndicator", "txnIndicator", "direction",
            )).lower()
            expend = income = None
            if debit is not None:
                expend = abs(debit)
            if credit is not None:
                income = abs(credit)
            if amount is not None and expend is None and income is None:
                if amount < 0 or any(k in direction for k in ("debit", "dr", "withdraw", "out", "轉出", "支出")):
                    expend = abs(amount)
                elif any(k in direction for k in ("credit", "cr", "deposit", "in", "轉入", "存入")):
                    income = abs(amount)
                elif desc and any(k in desc for k in ("扣", "轉出", "提款", "付款", "支出")):
                    expend = abs(amount)
                else:
                    income = abs(amount)
            if not (post_date or txn_date) or (expend is None and income is None):
                continue
            rows.append({
                "account_no": account_no,
                "datetime": post_date or txn_date,
                "account_date": txn_date or post_date,
                "desc": desc or memo or "DBS 交易",
                "expend": expend,
                "income": income,
                "balance": balance,
                "counterparty_bank": None,
                "counterparty_acct": None,
                "memo": memo,
            })
    return rows


def persist_dbs(data: dict, store: BankStore, rules: list[dict] | None = None) -> dict:
    """星展 DBS digibank → store 入庫。

    映射：
      api_responses['liabilities'].creditCard.cards[] → cards (UPSERT)
      api_responses['liabilities'].creditCard.paymentDetails → daily_metrics (帳單摘要)
      api_responses['assets'].casa.accounts[] → accounts (UPSERT) + balance_history (TWD 累加)
      api_responses['customer-profile'] → daily_metrics (顧客資料 keys)
      api_responses 全保留 → daily_metrics (endpoint 地圖)

    DBS 卡號特徵：'************7002' (12 個 * + 末四碼)
    DBS 持有兩種 'isDisplayImg' 狀態的卡 — 過期卡(false) + 有效卡(true)，全部入庫但 metric 標註。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    delta: dict = {"bank": "dbs", "scope": "structured"}

    apis = data.get("api_responses") or {}

    # === A. 信用卡 (liabilities) ===
    # Step 2 (2026-06-14): paymentDetails 是整戶層 (DBS 多卡共用 billing cycle),
    # 套到每張卡的 payment_due_date. limit/used 無 API 提供 (留 None).
    cc_new = 0
    lib_hits = apis.get("liabilities") or []
    if lib_hits:
        lib_resp = lib_hits[0].get("resp") or {}
        cc = (lib_resp.get("creditCard") or {})
        pay = cc.get("paymentDetails") or {}
        dbs_due = pay.get("dueDate")  # dashboard fallback, ISO 格式
        dbs_used = _num_to_float(pay.get("amount"))  # paymentDetails.amount 是整戶應繳
        # 2026-07-02: 使用者指出 DBS 要登入後點「繳卡費」才看得到最近一期帳單金額。
        # paymentDetails.alreadyPaid 只是 dashboard summary，沒有繳款日，不能生成
        # last_payment_*；card fee page 也只是當期應繳/截止日，不是歷史繳款紀錄。
        card_fee = data.get("dbs_card_fee_page") or {}
        dbs_bill_due = _num_to_float(card_fee.get("bill_due_amount"))
        if dbs_bill_due is None:
            dbs_bill_due = _num_to_float(pay.get("amount"))
        dbs_due = card_fee.get("payment_due_date") or dbs_due
        dbs_last_pay_amt = None

        cards_raw = cc.get("cards") or []
        cards = []
        for c in cards_raw:
            if not isinstance(c, dict):
                continue
            cn = (c.get("cardNumber") or "")
            last4 = cn[-4:] if cn else ""
            cards.append({
                "number": f"****{last4}" if last4 else cn,
                "card_no_full_masked": cn,
                "name": c.get("cardDescription"),
                "brand": None,
                "primary": "正卡" if c.get("isPrimaryCard") else "附卡",
                "status": "有效" if c.get("isDisplayImg") else "已失效",
                "card_id": c.get("cardId"),
                "expiry": c.get("cardExpiryDate"),
                # 2026-06-14 使用者指示: 過期卡 (isDisplayImg=False) UI 不顯示
                # 但 txn 紀錄保留 (stats/月消費仍計算). cards.active 控制 UI 可見性.
                "active": bool(c.get("isDisplayImg")),
                # Step 2: paymentDetails 整戶值套到每張卡 (DBS 共用 billing)
                "credit_limit": None,  # DBS API 無 creditLimit (需 live probe)
                "used_credit": dbs_used,  # 整戶應繳當已動用
                "statement_close_date": None,  # DBS API 無 statementDate
                "payment_due_date": dbs_due,
                "bill_due_amount": dbs_bill_due,
                # 2026-07-02: DBS has no payment-history date in current scrape.
                "last_payment_amount": dbs_last_pay_amt,
                # last_payment_date 不寫 — DBS API 無此欄
            })
        if cards:
            store.upsert_cards(cards)

        if pay:
            billing_metric = {
                "amount_due": dbs_bill_due,
                "due_date": dbs_due,
                "min_due": pay.get("minimumAmount"),
                "already_paid": pay.get("alreadyPaid"),
                "currency": card_fee.get("currency") or pay.get("currency"),
                "source": "card_fee_page" if card_fee else "liabilities.paymentDetails",
            }
            store.put_daily_metric("dbs_card_billing_summary", billing_metric, today)

    # === B. 帳戶 (assets.casa.accounts) ===
    ast_hits = apis.get("assets") or []
    accts = []
    account_by_global_id: dict[str, str] = {}
    twd_total = 0.0
    if ast_hits:
        ast_resp = ast_hits[0].get("resp") or {}
        casa = ast_resp.get("casa") or {}
        for a in (casa.get("accounts") or []):
            if not isinstance(a, dict):
                continue
            acct_no = a.get("displayAccountNumber") or a.get("accountId") or ""
            if not acct_no:
                continue
            global_id = a.get("globalAccountId")
            if global_id:
                account_by_global_id[str(global_id)] = acct_no
            bal_obj = a.get("availableBalance") or {}
            currency = bal_obj.get("currency") or "TWD"
            try:
                domestic_bal = float(bal_obj.get("domesticCurrencyBalance") or 0)
            except (TypeError, ValueError):
                domestic_bal = 0.0
            # raw_balance 存「原幣 balance」（不是 domesticCurrencyBalance）
            # 配合 portfolio router 走 fx_service 換 TWD，避免雙重計算
            raw = {**a, "currency": currency}
            accts.append({
                "account_no": acct_no,
                "currency": currency,
                "branch": None,
                "nickname": a.get("schemeName"),
                "type": a.get("schemeType"),
                "product_type": account_classify.classify_account("dbs", raw),
                "raw_balance": _num_real(bal_obj.get("balance")),
                "raw_balance_date": today,
            })
            twd_total += domestic_bal
    # 貸款帳戶：liabilities[0].resp.loan — 使用者鐵律：所有爬蟲都該處理貸款
    liab_hits = apis.get("liabilities") or []
    if liab_hits:
        liab_resp = liab_hits[0].get("resp") or {}
        loan_block = liab_resp.get("loan")
        if isinstance(loan_block, dict):
            # loan 結構：可能是 single dict 或含 accounts[] 列表（使用者實測 None）
            loan_items = (loan_block.get("accounts")
                          or loan_block.get("loans") or [loan_block])
            for ln in loan_items:
                if not isinstance(ln, dict):
                    continue
                acct_no = (ln.get("displayAccountNumber") or ln.get("accountId")
                           or ln.get("loanAccountNumber"))
                if not acct_no:
                    continue
                raw = {**ln, "_source": "loan"}
                # loan balance defensive：試 outstanding 或 balance 結構
                bal_raw = (ln.get("outstandingBalance") or ln.get("balance")
                           or (ln.get("availableBalance", {}) or {}).get("balance"))
                accts.append({
                    "account_no": acct_no,
                    "currency": ln.get("currency") or "TWD",
                    "branch": None,
                    "nickname": ln.get("schemeName") or "DBS 貸款",
                    "type": ln.get("schemeType") or "loan",
                    "product_type": account_classify.classify_account("dbs", raw),
                    "raw_balance": _num_real(bal_raw),
                    "raw_balance_date": today,
                })
    if accts:
        store.upsert_accounts(accts)
        store.upsert_balance_history([{
            "snapshotDate": today,
            "twdBalance": twd_total if twd_total else None,
            "fxBalance": None,
        }])
        delta["balance_days"] = 1
        store.put_daily_metric("dbs_balance_latest",
                                {"twd": twd_total, "n_accounts": len(accts)}, today)

    # === C. 存款交易明細 (overview 帳戶 row drilldown) ===
    twd_rows = _parse_dbs_twd_transactions(apis, account_by_global_id)
    if twd_rows:
        delta["twd_txn_new"] = store.upsert_twd_txns(twd_rows, rules=rules)
    for hit in apis.get("inquiry") or []:
        url = hit.get("url") or ""
        resp = hit.get("resp") or {}
        if "historical-summary/inquiry" in url:
            store.put_daily_metric("dbs_twd_historical_summary", {
                "request": hit.get("req_body"),
                "historicalSummaries": resp.get("historicalSummaries") or [],
            }, today)
        elif "transactions-history/inquiry" in url:
            store.put_daily_metric("dbs_twd_transactions_history", {
                "request": hit.get("req_body"),
                "pageInfo": resp.get("pageInfo") or {},
                "count": len(resp.get("transactions") or []),
            }, today)

    # === D. customer-profile → daily_metrics ===
    cp_hits = apis.get("customer-profile") or []
    if cp_hits:
        cp_resp = cp_hits[0].get("resp") or {}
        if isinstance(cp_resp, dict):
            store.put_daily_metric("dbs_customer_profile_keys", {
                "keys": sorted(cp_resp.keys()),
            }, today)

    # === D. endpoint 地圖 dump ===
    eps = data.get("_all_endpoints") or []
    if eps:
        store.put_daily_metric("dbs_endpoints", {"endpoints": eps}, today)

    delta.setdefault("balance_days", 0)
    delta.setdefault("twd_txn_new", 0)
    delta["card_billed_new"] = cc_new
    delta["card_unbilled"] = 0
    delta["card_current"] = 0  # 第一輪 dashboard 無逐筆明細，需點信用卡 menu
    store.log_sync(delta)
    return delta
