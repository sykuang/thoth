"""聯邦銀行 (UBOT) persist 邏輯.

從 persist.py 拆分而來 (2026-06-17, W persist.py split refactor)。
"""
from __future__ import annotations

from datetime import datetime

from backend.core import account_classify, classify
from backend.core.store import BankStore
from backend.core.persist._common import _num, _num_real, _num_to_float, _ubot_date


def _yyyymmdd_to_iso(s: str | None) -> str | None:
    """'20260618' → '2026-06-18'；空字串 / '00000000' / 非預期格式 → None.

    用於 UBOT / Taishin 等回 YYYYMMDD 純數字字串的銀行.
    """
    if not s:
        return None
    s = str(s).strip()
    if len(s) != 8 or not s.isdigit() or s == "00000000":
        return None
    try:
        y, mo, d = int(s[:4]), int(s[4:6]), int(s[6:])
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            return None
        return f"{y:04d}-{mo:02d}-{d:02d}"
    except Exception:
        return None

def persist_ubot(data: dict, store: BankStore, rules: list[dict] | None = None) -> dict:
    """聯邦 collect() 結構 → store 7 表增量。

    映射：
      deposit_twd.NTList   → accounts(UPSERT) + balance_history(每日快照)
      twd_txns[].NTDetailList → twd_transactions(append-only)
      card_summary/card_limit → daily_metrics
      card_billed[].CardList  → card_billed_txns(append-only)
      card_unbilled.CardList  → card_pending_txns(refresh 'unbilled')
    """
    today = datetime.now().strftime("%Y-%m-%d")
    delta: dict = {}

    # --- 台幣存款帳戶（UPSERT）+ 餘額快照 ---
    dt = data.get("deposit_twd") or {}
    nt_list = dt.get("NTList", []) if isinstance(dt, dict) else []
    ft_list = dt.get("FTList", []) if isinstance(dt, dict) else []
    loan_list = dt.get("LoanList", []) if isinstance(dt, dict) else []

    # 存款帳戶（TWD + FX）
    accts: list[dict] = []
    for a in nt_list:
        if not a.get("Account"):
            continue
        raw = {**a, "_list_origin": "NTList", "currency": "TWD"}
        accts.append({
            "account_no": a.get("Account"), "currency": "TWD", "branch": a.get("Branch"),
            "nickname": None, "type": a.get("AccountType"),
            "product_type": account_classify.classify_account("ubot", raw),
            "raw_balance": _num_real(a.get("AccountBal")),
            "raw_balance_date": today,
        })
    for a in ft_list:
        if not a.get("Account"):
            continue
        cur = a.get("Currency") or "USD"
        raw = {**a, "_list_origin": "FTList", "currency": cur}
        accts.append({
            "account_no": a.get("Account"), "currency": cur, "branch": a.get("Branch"),
            "nickname": None, "type": a.get("AccountType"),
            "product_type": account_classify.classify_account("ubot", raw),
            "raw_balance": _num_real(a.get("AccountBal")),
            "raw_balance_date": today,
        })
    # 貸款帳戶 (LoanList): 解使用者「所有爬蟲都應該處理好貸款」的鐵律
    for a in loan_list:
        if not a.get("Account"):
            continue
        raw = {**a, "_list_origin": "LoanList", "currency": "TWD"}
        accts.append({
            "account_no": a.get("Account"), "currency": "TWD", "branch": a.get("Branch"),
            "nickname": None, "type": a.get("AccountType"),
            "product_type": account_classify.classify_account("ubot", raw),
            "raw_balance": _num_real(a.get("AccountBal")),
            "raw_balance_date": today,
        })

    if accts:
        store.upsert_accounts(accts)
    # 台幣總餘額 + 貸款餘額快照（同日覆蓋）
    if isinstance(dt, dict):
        td = dt.get("TotalData") or {}
        twd_total = _num(td.get("Deposit"))
        loan_total = _num(td.get("Loan"))
        if twd_total is not None or loan_total is not None:
            store.upsert_balance_history([{
                "snapshotDate": today,
                "twdBalance": twd_total,
                "fxBalance": None,
                "loanBalance": loan_total if loan_total else None,
            }])
            delta["balance_days"] = 1

    # --- 台幣交易明細（append-only）---
    twd_new = 0
    for body in data.get("twd_txns") or []:
        if not isinstance(body, dict):
            continue
        acct_no = body.get("Account")
        rows = []
        for t in body.get("NTDetailList", []):
            rows.append({
                "account_no": acct_no,
                "datetime": f"{_ubot_date(t.get('TraDate'))} {t.get('TraTime', '')}".strip(),
                "account_date": _ubot_date(t.get("AccountDate")),
                "desc": (t.get("Summary") or "").strip(),
                "expend": _num(t.get("Expenditure")),
                "income": _num(t.get("Income")),
                "balance": _num(t.get("Balance")),
                "counterparty_bank": None,
                "counterparty_acct": None,
                "memo": (t.get("TraSum") or "").strip() or (t.get("PS") or "").strip() or None,
            })
        twd_new += store.upsert_twd_txns(rows, rules=rules)
    delta["twd_txn_new"] = twd_new

    # --- 信用卡已出帳明細（append-only）---
    billed_new = 0
    for body in data.get("card_billed") or []:
        if not isinstance(body, dict):
            continue
        bill_date = _ubot_date((body.get("CardHeader") or {}).get("stmtDate"))
        rows = []
        for t in body.get("CardList", []):
            amt = _num(t.get("txAmt"))                 # 台幣入帳金額（整數，signed）
            desc = (t.get("txDesc") or "").strip()
            rows.append({
                "card_no": t.get("cardNo"),
                "bill_date": bill_date,
                "currency": "TWD",                              # 入帳幣別（台幣）
                "date": _ubot_date(t.get("effectDate")),       # 消費日
                "post_date": _ubot_date(t.get("postDate")),     # 入帳日（爬不到 store 層 fallback=消費日）
                "desc": desc,
                "amount": amt,
                "consume_country": None,
                "consume_currency": (t.get("Currency") or "").strip() or "TWD",  # 原始消費幣別
                "consume_amount": _num_real(t.get("oriAmt")),   # 原始外幣金額（保留小數）
                "txn_type": classify.classify_ubot(t.get("txCode"), desc, amt),
            })
        billed_new += store.upsert_card_billed(rows, rules=rules)
    delta["card_billed_new"] = billed_new

    # --- 信用卡未出帳（refresh-by-scope）---
    unb = data.get("card_unbilled") or {}
    unb_rows = []
    if isinstance(unb, dict):
        for t in unb.get("CardList", []):
            amt = _num(t.get("txAmt"))
            desc = (t.get("txDesc") or "").strip()
            unb_rows.append({
                "card_no": t.get("cardNo"), "date": _ubot_date(t.get("effectiveDate")),
                "desc": desc, "amount": amt,
                "currency": (t.get("Currency") or "").strip() or "TWD",
                "txn_type": classify.classify_ubot(t.get("txCode"), desc, amt),
            })
    delta["card_unbilled"] = store.refresh_card_pending("unbilled", unb_rows, rules=rules)
    delta["card_current"] = 0

    # --- cards 表 UPSERT（從 billed/unbilled 卡號推斷，設計規範）---
    # Step 2 (2026-06-14): per-card 接信用額度 / 帳單日 / 繳費日.
    # UBOT card_limit/card_summary CardList 只 1 筆 = 整戶層 (多卡 aggregate),
    # 所以同 user 下每張卡套同一組 limit/dueDate/stmtDate.
    # billed[0].CardHeader.stmtDate 是該帳單結帳日 (跨卡共用).
    cl_summary = ((data.get("card_limit") or {}).get("CardList") or [{}])[0]
    cs_summary = ((data.get("card_summary") or {}).get("CardList") or [{}])[0]
    # 整戶字段 fallback: card_limit 優先 (含 crLmt 總額度), card_summary 次之
    ubot_credit_limit = _num_to_float(cl_summary.get("crLmt"))
    ubot_used = _num_to_float(cl_summary.get("unsettleAmt"))
    ubot_due = _yyyymmdd_to_iso(cl_summary.get("dueDate") or cs_summary.get("dueDate"))
    # 結帳日從 billed[0].CardHeader.stmtDate 拿 (最新一期)
    ubot_stmt = None
    billed_list = data.get("card_billed") or []
    if billed_list:
        hdr = (billed_list[0].get("CardHeader") or {})
        ubot_stmt = _yyyymmdd_to_iso(hdr.get("stmtDate"))

    # 2026-06-22 升級 (multi-account + m1~m5): IBKF010001 card_limit raw 本來就有
    # lastPayAmt / lastPayDate / payAmt (本期應繳),
    # 之前 persist 只用 crLmt/unsettleAmt/dueDate 三欄漏抓. 這三欄是 card_events
    # `detect_card_events()` 判定「new_payment」通知的依據之一, 漏抓 → 聯邦永遠不會
    # 推「new_payment」通知 + UI bill_due_amount 永遠 NULL.
    # 整戶層 aggregate 套到每張卡 (UBOT 多卡共用唯一 dueDate / 唯一 lastPay 歷史 by-design).
    ubot_bill_due = _num_to_float(cl_summary.get("payAmt") or cs_summary.get("payAmt"))
    ubot_last_pay_amt = _num_to_float(cl_summary.get("lastPayAmt"))
    ubot_last_pay_date = _yyyymmdd_to_iso(cl_summary.get("lastPayDate"))
    # 2026-06-22 v2: 不再 sentinel amount=0 → None — 0 是合法值
    # (聯邦 lastPayAmt='0' + lastPayDate='00000000' 代表「自動扣繳尚未到期」).
    # card_events 仍靠 last_payment_date is None 來 gate 通知, 不會誤推.
    # 但 lastPayDate=00000000 確實是 sentinel (date "從未繳款"), _yyyymmdd_to_iso
    # 已處理成 None.

    # 2026-06-22 v3 (使用者指示「ubot 有近期繳款紀錄查詢呀」F0801001):
    # F0801001 → IBKF080001 是真實「近期繳款紀錄」, 有日期 + 金額 + 卡號. 補在
    # card_limit 的 lastPayDate='00000000' 拿不到 date 的場景.
    # 邏輯: 找 list 內最新一筆 (按 postDate 排序最大), 覆寫 amount + date.
    #
    # 2026-06-22 v4 (local ubot crawl 後確認 real shape):
    #   {"DateList": [{"postDate": "2026/06/22", "effectDate": "2026/06/22",
    #                  "payAmt": "38,647", "txDesc": "自動轉帳－聯邦銀行", "seqNo": "00001"}, ...]}
    #   日期格式 'YYYY/MM/DD' 不是 'YYYYMMDD' (要走 slash 轉 ISO).
    #   金額帶逗號 '38,647' (要走 _num_to_float, 內建 strip comma).
    pay_history = data.get("card_pay_history") or {}
    if isinstance(pay_history, dict):
        # 試 DateList (實測命中) + 其他常見 key fallback
        records = None
        for key in ("DateList", "PayList", "payList", "dataList", "list", "records", "DataList"):
            v = pay_history.get(key)
            if isinstance(v, list) and v:
                records = v
                break
        if records:
            # 找最新一筆 — 按可能的 date key (postDate / effectDate / payDate / date)
            def _record_date(r: dict) -> str:
                for dk in ("postDate", "PostDate", "effectDate", "EffectDate",
                           "payDate", "PayDate", "date", "Date", "txnDate"):
                    v = r.get(dk)
                    if v and isinstance(v, str):
                        return v  # 字典序 (YYYY/MM/DD 或 YYYYMMDD 都對齊時序)
                return ""

            latest_pay = max(records, key=_record_date)
            # date: 試多個 key, 同時支援 YYYY/MM/DD slash 跟 YYYYMMDD 兩種格式
            for dk in ("postDate", "PostDate", "effectDate", "EffectDate",
                       "payDate", "PayDate", "date", "Date", "txnDate"):
                dv = latest_pay.get(dk)
                if not dv or not isinstance(dv, str):
                    continue
                dv_clean = dv.strip()
                iso = None
                if "/" in dv_clean:
                    # '2026/06/22' → '2026-06-22'
                    parts = dv_clean.split("/")
                    if len(parts) == 3 and all(p.isdigit() for p in parts):
                        try:
                            y, mo, d = int(parts[0]), int(parts[1]), int(parts[2])
                            if 1 <= mo <= 12 and 1 <= d <= 31 and y >= 2000:
                                iso = f"{y:04d}-{mo:02d}-{d:02d}"
                        except ValueError:
                            pass
                else:
                    iso = _yyyymmdd_to_iso(dv_clean)
                if iso:
                    ubot_last_pay_date = iso
                    break
            # amount: 試多個 key — _num_to_float 已處理逗號 '38,647'
            for ak in ("payAmt", "PayAmt", "amt", "Amt", "amount", "Amount", "txnAmt"):
                av = latest_pay.get(ak)
                if av is not None:
                    parsed = _num_to_float(av)
                    if parsed is not None:
                        ubot_last_pay_amt = parsed
                        break

    seen_ubot_cards: dict[str, dict] = {}
    for body in data.get("card_billed") or []:
        for t in (body.get("CardList") or []):
            cn = t.get("cardNo")
            if not cn or cn in seen_ubot_cards:
                continue
            seen_ubot_cards[cn] = {
                "number": cn,
                "name": (t.get("typeName") or "").strip() or "聯邦卡",
                "association": None,
                "type": "credit",
                "is_cube": False,
                # Step 2: 整戶層 aggregate 套到每張 (UBOT 唯一 user 假設, 多卡都共用)
                "credit_limit": ubot_credit_limit,
                "used_credit": ubot_used,
                "statement_close_date": ubot_stmt,
                "payment_due_date": ubot_due,
                "bill_due_amount": ubot_bill_due,
                "last_payment_amount": ubot_last_pay_amt,
                "last_payment_date": ubot_last_pay_date,
            }
    for t in (unb.get("CardList") or []):
        cn = t.get("cardNo")
        if cn and cn not in seen_ubot_cards:
            seen_ubot_cards[cn] = {
                "number": cn,
                "name": (t.get("typeName") or "").strip() or "聯邦卡",
                "association": None,
                "type": "credit",
                "is_cube": False,
                "credit_limit": ubot_credit_limit,
                "used_credit": ubot_used,
                "statement_close_date": ubot_stmt,
                "payment_due_date": ubot_due,
                "bill_due_amount": ubot_bill_due,
                "last_payment_amount": ubot_last_pay_amt,
                "last_payment_date": ubot_last_pay_date,
            }
    if seen_ubot_cards:
        store.upsert_cards(list(seen_ubot_cards.values()))

    # --- 每日數值快照：信用卡彙總/額度、投資 ---
    cs = data.get("card_summary")
    if cs:
        store.put_daily_metric("card_summary", cs, today)
    cl = data.get("card_limit")
    if cl:
        store.put_daily_metric("card_limit", cl, today)
    # 2026-06-22 v3: F0801001 raw 留底, 明早 sync 後可從 daily_metrics 撈出來
    # 看真實 shape, 調 persist mapping. 即使上面 PayList parse 失敗也保得到.
    pay_hist = data.get("card_pay_history")
    if pay_hist:
        store.put_daily_metric("card_pay_history", pay_hist, today)
    if isinstance(dt, dict):
        twd_total_metric = _num((dt.get("TotalData") or {}).get("Deposit"))
        if twd_total_metric is not None:
            store.put_daily_metric("balance_latest", {"twd": twd_total_metric}, today)
    inv = data.get("investment")
    if inv:
        store.put_daily_metric("investment", inv, today)

    store.log_sync(delta)
    return delta
