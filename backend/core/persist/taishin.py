"""台新銀行 (Taishin) persist 邏輯.

從 persist.py 拆分而來 (2026-06-17, W persist.py split refactor)。
"""
from __future__ import annotations

from datetime import datetime
import re

from backend.core import account_classify, classify
from backend.core.store import BankStore
from backend.core.persist._common import _num, _num_real, _num_to_float, _slash_date_to_iso


def _parse_taishin_twd_txn_results(results: list[dict]) -> list[dict]:
    """台新 RB0102/0100「查詢交易明細」頁 text → twd_transactions rows."""
    rows: list[dict] = []
    for result in results or []:
        if not isinstance(result, dict):
            continue
        text = result.get("text") or ""
        if not text or "交易明細" not in text:
            continue
        selected = result.get("selected_text") or (result.get("query_result") or {}).get("accountText") or ""
        acct_digits = re.sub(r"\D", "", selected)
        account_no = acct_digits if len(acct_digits) >= 10 else None
        if not account_no:
            continue

        # Real shape is multi-line 6-column blocks after header:
        # 交易日 / 帳務日 / 摘要 / 金額 / 餘額 / 備註
        pat = re.compile(
            r"(\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}:\d{2})\s*\n\s*\t\s*\n\s*"
            r"(\d{4}/\d{1,2}/\d{1,2})\s*\n\s*\t\s*\n\s*"
            r"([^\n]+?)\s*\n\s*\t\s*\n\s*"
            r"(-?[\d,]+(?:\.\d+)?)\s*\n\s*\t\s*\n\s*"
            r"(-?[\d,]+(?:\.\d+)?)\s*\n\s*\t\s*\n\s*"
            r"([^\n\t]*)(?:\s*\n\s*\t\s*\n\s*消費屬性設定)?",
            re.MULTILINE,
        )
        for m in pat.finditer(text):
            txn_dt = m.group(1).replace("/", "-")
            account_date = _slash_date_to_iso(m.group(2))
            desc = m.group(3).strip()
            amount = _num_to_float(m.group(4))
            balance = _num_to_float(m.group(5))
            memo = (m.group(6) or "").strip() or None
            expend = income = None
            if amount is not None:
                if amount < 0:
                    expend = abs(amount)
                else:
                    income = amount
            rows.append({
                "account_no": account_no,
                "datetime": txn_dt,
                "account_date": account_date,
                "desc": desc,
                "expend": expend,
                "income": income,
                "balance": balance,
                "counterparty_bank": None,
                "counterparty_acct": memo[:30] if memo else None,
                "memo": memo,
            })
    return rows


def persist_taishin(data: dict, store: BankStore, rules: list[dict] | None = None) -> dict:
    """台新 collect → store 入庫。

    映射：
      api_responses.query.OUTPUTDATA.SavingAccount[] → accounts(UPSERT) + balance_history
      api_responses.qryTaishinPoint.value.balance     → daily_metrics
      api_responses.login.CUSTNO                       → daily_metrics
      api_responses 全保留                              → daily_metrics (dump)
    """
    today = datetime.now().strftime("%Y-%m-%d")
    delta: dict = {"bank": "taishin"}

    apis = data.get("api_responses") or {}
    delta["scope"] = "structured"

    # --- query: SavingAccount ---
    query_resp = apis.get("query") or {}
    output = query_resp.get("OUTPUTDATA") or {}
    savings = output.get("SavingAccount") or []
    accts = []
    twd_total = 0.0
    for s in savings:
        if not isinstance(s, dict):
            continue
        acct_no = s.get("accountNo") or ""
        if not acct_no:
            continue
        bal = _num(s.get("balance")) or 0.0
        raw = {**s, "type": s.get("accountTypeName") or "活期存款", "currency": "TWD"}
        accts.append({
            "account_no": acct_no,
            "currency": "TWD",
            "branch": None,
            "nickname": s.get("userdefineName") or None,
            "type": s.get("accountTypeName") or None,
            "product_type": account_classify.classify_account("taishin", raw),
            "raw_balance": _num_real(s.get("balance")),
            "raw_balance_date": today,
        })
        twd_total += bal
    if accts:
        store.upsert_accounts(accts)
    if accts and twd_total >= 0:
        store.upsert_balance_history([{
            "snapshotDate": today,
            "twdBalance": twd_total if twd_total else None,
            "fxBalance": None,
        }])
        delta["balance_days"] = 1
        store.put_daily_metric("balance_latest",
                                {"twd": twd_total, "n_accounts": len(accts)}, today)

    # --- qryTaishinPoint ---
    pts = apis.get("qryTaishinPoint") or {}
    val = pts.get("value") or {}
    if isinstance(val, dict) and val.get("balance") is not None:
        store.put_daily_metric("taishin_points",
                                {"balance": val.get("balance"),
                                 "TSPOINT_balance": val.get("TSPOINT_balance")}, today)

    # --- login meta ---
    login_resp = apis.get("login") or {}
    if isinstance(login_resp, dict):
        store.put_daily_metric("taishin_login_meta", {
            "custno": login_resp.get("CUSTNO"),
            "cust_type": login_resp.get("custTypeCode"),
            "pwd_expired": login_resp.get("PWDEXPIRED"),
            "card_member": login_resp.get("CARDMBR"),
        }, today)

    # --- 全 endpoint dump（debug） ---
    if apis:
        store.put_daily_metric("taishin_endpoints",
                                {"endpoints": sorted(apis.keys())}, today)

    # --- 信用卡（從 frame text parser 抽出）---
    # 2026-06-11 端到端：mouse.move hover mega menu → click「查詢信用卡明細」
    # → frame URL = `RB0708/0100` → frame.body.innerText → _parse_credit_card_page
    cc_new = 0
    cc_pending_n = 0

    def _norm_date(s: str | None) -> str | None:
        """台新 frame text 日期可能是 '2026/5/12' (無零) → 規範化成 '2026-05-12'。"""
        if not s:
            return None
        try:
            y, mo, d = s.replace("-", "/").split("/")
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        except Exception:
            return s.replace("/", "-")

    parsed_raw = data.get("credit_card_parsed")
    parsed = parsed_raw if isinstance(parsed_raw, dict) else {}
    if isinstance(parsed, dict) and not parsed.get("error"):
        # Step 2 (2026-06-14): Taishin per-card 沒 limit, 整戶 qryRealTime.crlimit
        # + billing_period.{pay_due_date, statement_date} 套到每張卡 (通常 1 張).
        period = parsed.get("billing_period") or {}
        taishin_stmt = _slash_date_to_iso(period.get("statement_date"))  # '2026/5/12'
        taishin_due = _slash_date_to_iso(period.get("pay_due_date"))      # '2026/05/27'
        # qryRealTime.value.crlimit = '400,000' (整戶層)
        api_resps = data.get("api_responses") or {}
        qry_real = api_resps.get("qryRealTime")
        qry_val = (qry_real[0] if isinstance(qry_real, list) else qry_real or {}).get("value") or {}
        taishin_limit = _num_to_float(qry_val.get("crlimit"))

        # 2026-07-02: 台新真正繳款紀錄藏在 RB0708 其他月份的「上期實繳金額明細」；
        # 不是「已繳款明細」（該段實測查無資料），也不是存款端卡費 fallback。
        payment_rows = []
        for row in parsed.get("billed_txns") or []:
            desc = str(row.get("desc") or "")
            amt = _num_to_float(row.get("amount"))
            if amt is None or amt >= 0:
                continue
            if "信用卡" in desc and ("扣繳" in desc or "自動轉帳" in desc or "繳" in desc):
                payment_rows.append(row)
        latest_payment = None
        if payment_rows:
            latest_payment = max(payment_rows, key=lambda r: str(r.get("post_date") or r.get("txn_date") or ""))

        # 2026-06-14 Step 2 升級：used_credit 用 doXTPA.value.001 (即時可用額度 +
        # 永久額度) 算出來，比 top_summary.unpaid (未繳已出帳) 更準確 — 它含
        # 已動用但尚未出帳的即時消費.
        # used = CRLIMIT-PERM - AVAIL-CREDIT  e.g. 400000 - 399920 = 80
        taishin_used = None
        xtpa = api_resps.get("doXTPA")
        xtpa_val = (xtpa[0] if isinstance(xtpa, list) else xtpa or {}).get("value") or {}
        # doXTPA.value.001 (card index 001) 為主卡
        card_001 = xtpa_val.get("001") if isinstance(xtpa_val, dict) else None
        if isinstance(card_001, dict):
            crlimit_perm = _num_to_float(card_001.get("OUT-CRLIMIT-PERM"))
            avail = _num_to_float(card_001.get("OUT-AVAIL-CREDIT"))
            if crlimit_perm is not None and avail is not None:
                taishin_used = crlimit_perm - avail
            # 若 doXTPA 也給 CRLIMIT, 信用度比 qryRealTime 更新
            if taishin_limit is None:
                taishin_limit = crlimit_perm

        # cards 入庫（store 需要 number 鍵 → card_no SQL 欄）
        cards = parsed.get("cards") or []
        # 2026-07-03 修正: bill_due_amount 應用 summary.remaining (本期剩餘應繳)
        # 而非 bill_amount (本期帳單總額)。1409 卡自動扣繳 case:
        #   bill_amount=80, paid=80, remaining=0
        # 舊碼寫 bill_due_amount=80 → frontend bill_status 誤判「未繳」。
        # remaining 是 UI「還要繳多少」的正確口徑; fallback 用 bill_amount - paid
        # 保護 raw 沒吐 remaining 的舊 shape。詳見 wiki
        # [[card-payment-history-native-fallback-pattern]] Taishin section.
        # last_payment_* 仍走真實 billed_txns payment row (使用者「不假造」鐵則),
        # 整戶層套到每張卡 (台新通常 1 張)。
        summary = parsed.get("summary") or {}
        taishin_bill_due = _num_to_float(summary.get("remaining"))
        if taishin_bill_due is None:
            _bill = _num_to_float(summary.get("bill_amount"))
            _paid = _num_to_float(summary.get("paid")) or 0
            taishin_bill_due = (_bill - _paid) if _bill is not None else None
        taishin_last_pay_amt = None
        taishin_last_pay_date = None
        if latest_payment:
            pay_amt = _num_to_float(latest_payment.get("amount"))
            if pay_amt is not None:
                taishin_last_pay_amt = abs(pay_amt)
            taishin_last_pay_date = _slash_date_to_iso(
                latest_payment.get("post_date") or latest_payment.get("txn_date"),
            )
        # 2026-06-22 v2: 不再 sentinel paid=0 → None. 0 是合法值.
        # 若有「上期實繳金額明細」payment row，該 row 的入帳日/金額才是繳款紀錄 source.

        for card in cards:
            card["credit_limit"] = taishin_limit
            card["statement_close_date"] = taishin_stmt
            card["payment_due_date"] = taishin_due
            # used_credit: 優先 doXTPA 即時算 (taishin_used)，
            # fallback top_summary.unpaid (未繳已出帳) 或 period.bill_amount
            top = parsed.get("top_summary") or {}
            card["used_credit"] = (taishin_used
                                   if taishin_used is not None
                                   else _num_to_float(top.get("unpaid") or period.get("bill_amount")))
            # 2026-06-22 (audit): bill_due + last_payment_amount 整戶層套
            card["bill_due_amount"] = taishin_bill_due
            card["last_payment_amount"] = taishin_last_pay_amt
            card["last_payment_date"] = taishin_last_pay_date
        if cards:
            store.upsert_cards(cards)

        # 帳單期間（給 bill_date 用）
        bill_date = _norm_date(period.get("statement_date")) or today

        # billed_txns 入庫（上期實繳明細）
        bills = parsed.get("billed_txns") or []
        billed_payload = []
        for b in bills:
            try:
                card_no_suffix = b.get("card_no_suffix") or ""
                card_no = f"****{card_no_suffix}" if card_no_suffix else None
                consume_date = _norm_date(b.get("txn_date"))
                post_date = _norm_date(b.get("post_date"))
                desc = b.get("desc")
                amt = b.get("amount")
                billed_payload.append({
                    "card_no": card_no,
                    "bill_date": bill_date,
                    "date": consume_date,
                    "post_date": post_date,
                    "desc": desc,
                    "amount": amt,
                    "currency": "TWD" if b.get("currency") == "新臺幣" else b.get("currency"),
                    "txn_type": classify.classify_by_desc_and_sign(desc, amt),
                })
            except Exception:
                continue
        if billed_payload:
            cc_new = store.upsert_card_billed(billed_payload, rules=rules) or 0

        # pending_txns 入庫（即時消費紀錄, scope='realtime'）
        pendings = parsed.get("pending_txns") or []
        pending_payload = []
        for p in pendings:
            try:
                card_no_suffix = p.get("card_no_suffix") or ""
                card_no = f"****{card_no_suffix}" if card_no_suffix else None
                desc = p.get("desc")
                amt = p.get("amount")
                pending_payload.append({
                    "card_no": card_no,
                    "date": _norm_date(p.get("txn_date")),
                    "desc": desc,
                    "amount": amt,
                    "currency": "TWD",
                    "txn_type": classify.classify_by_desc_and_sign(desc, amt),
                })
            except Exception:
                continue
        fetch_ok = (isinstance(parsed_raw, dict)
                    and parsed_raw.get("fetch_ok") is True
                    and not parsed_raw.get("error"))
        cc_pending_n = store.refresh_card_pending(
            "realtime", pending_payload, rules=rules, fetch_ok=fetch_ok)

        # 各類 summary → daily_metrics（SCSB 模式）
        top = parsed.get("top_summary") or {}
        if top:
            store.put_daily_metric("taishin_card_top_summary", top, today)
        summary = parsed.get("summary") or {}
        if summary:
            store.put_daily_metric("taishin_card_current_period", summary, today)
        if period:
            store.put_daily_metric("taishin_card_billing_period", period, today)

    delta.setdefault("balance_days", 0)
    twd_rows = _parse_taishin_twd_txn_results(data.get("twd_txn_results") or [])
    twd_new = store.upsert_twd_txns(twd_rows, rules=rules) if twd_rows else 0
    delta["twd_txn_new"] = twd_new
    delta["card_billed_new"] = cc_new
    delta["card_unbilled"] = 0  # 台新「未出帳款」表頁無逐筆明細，僅頂部 TWD 80 摘要
    delta["card_current"] = cc_pending_n

    store.log_sync(delta)
    return delta
