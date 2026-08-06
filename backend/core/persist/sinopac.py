"""永豐 (Sinopac MMA) persist 邏輯.

從 persist.py 拆分而來 (2026-06-17, W persist.py split refactor)。
"""
from __future__ import annotations

from datetime import datetime

from backend.core import account_classify, classify
from backend.core.store import BankStore
from backend.core.persist._common import _num, _num_real, _num_to_float


def _sinopac_date(s) -> str | None:
    """永豐日期 '20260610' / '2026/06/10' → 'YYYY-MM-DD'。"""
    if not s:
        return None
    t = str(s).strip().replace("/", "").replace("-", "")
    if len(t) == 8 and t.isdigit():
        return f"{t[:4]}-{t[4:6]}-{t[6:8]}"
    if len(t) == 6 and t.isdigit():  # 202606 → 2026-06-01
        return f"{t[:4]}-{t[4:6]}-01"
    return None

def _sinopac_strip_html(s) -> str:
    """永豐欄位含 HTML 標籤（如 '<font color="...">-2,000</font>'），全剝。"""
    if not s:
        return ""
    import re as _re
    return _re.sub(r"<[^>]*>", "", str(s)).strip()

def _sinopac_split_amount(s):
    """永豐 DataText4 = '<font>+30</font>' / '<font>-2,000</font>'
    回傳 (expend, income): 收入 → income，支出 → expend。
    """
    txt = _sinopac_strip_html(s).replace(",", "")
    if not txt:
        return None, None
    if txt.startswith("-"):
        try: return abs(int(float(txt))), None
        except (ValueError, TypeError): return None, None
    txt = txt.lstrip("+")
    try: return None, int(float(txt))
    except (ValueError, TypeError): return None, None

def _mmyy_expired(s: str | None, today_yyyy_mm: str | None = None) -> bool:
    """卡片到期日 'MMYYYY' (DBS '122026') / 'MMYY' (Sinopac '0829') → 是否過期.

    None / 解析失敗 → False (保守當未過期, 不誤殺現用卡).
    """
    if not s:
        return False
    s = str(s).strip()
    try:
        if len(s) == 6 and s.isdigit():  # MMYYYY (DBS)
            mo, y = int(s[:2]), int(s[2:])
        elif len(s) == 4 and s.isdigit():  # MMYY (Sinopac)
            mo, y = int(s[:2]), 2000 + int(s[2:])
        else:
            return False
        if not (1 <= mo <= 12):
            return False
        # 比較 (y, mo) 與今日
        if today_yyyy_mm is None:
            from datetime import datetime
            today_y, today_mo = datetime.now().year, datetime.now().month
        else:
            today_y, today_mo = int(today_yyyy_mm[:4]), int(today_yyyy_mm[5:7])
        return (y, mo) < (today_y, today_mo)
    except Exception:
        return False

def persist_sinopac(data: dict, store: BankStore, rules: list[dict] | None = None) -> dict:
    """永豐 collect() 結構 → store 各業務表增量。

    映射：
      bank_balance[].SubInfo → accounts(UPSERT) + balance_history(每日快照)
      loan.details[].records → accounts + balance_history.loan_balance + daily_metrics
      all_cards.Result.Items → cards(UPSERT)
      card_summary / asset_chart / card_billing / debit_accounts → daily_metrics
      twd_transactions / card_statements / card_unbilled → 交易明細表
    """
    today = datetime.now().strftime("%Y-%m-%d")
    delta: dict = {}

    # --- 銀行帳戶（含 TWD/USD/JPY 各幣別行）---
    bb = data.get("bank_balance") or []
    accts = []
    twd_total = 0
    fx_total = 0  # 外幣原始金額累加（未經 FX 換算）
    for grp in bb:
        if not isinstance(grp, dict):
            continue
        for a in grp.get("SubInfo", []) or []:
            acct_no = a.get("AcctValue") or a.get("AcctValueFormat")
            cur = a.get("Curr", "")
            bal = _num(a.get("AvailBalance"))
            if not acct_no:
                continue
            raw = {**a, "currency": cur}
            accts.append({
                "account_no": acct_no, "currency": cur,
                "branch": None, "nickname": a.get("AcctText"),
                "type": a.get("AcctText"),
                "product_type": account_classify.classify_account("sinopac", raw),
                "raw_balance": _num_real(a.get("AvailBalance")),
                "raw_balance_date": today,
            })
            if cur == "TWD" and bal is not None:
                twd_total += bal
            elif bal is not None:
                fx_total += bal
    # 貸款帳戶：ws_loanaccount + 每帳號 ws_loaninfo 真實明細。
    loan = data.get("loan") or {}
    loan_total = None
    loan_metric_records = []
    if isinstance(loan, dict) and loan.get("fetch_ok") is True:
        loan_balances = []
        for detail in loan.get("details") or []:
            if not isinstance(detail, dict) or not detail.get("account"):
                continue
            records = [r for r in detail.get("records") or [] if isinstance(r, dict)]
            loan_metric_records.extend({
                "loan_kind": record.get("LoanKind"),
                "repayment_method": record.get("PayName"),
                "sub_account": record.get("Sub1_Sub2"),
                "currency": record.get("Currency"),
                "begin_loan_date": record.get("BeginLoanDate"),
                "loan_date": record.get("LoanDate"),
                "maturity_date": record.get("MatureDate"),
                "original_principal": _num_real(record.get("LoanAmt")),
                "principal_balance": _num_real(record.get("LoanBalance")),
                "interest_rate": record.get("LoanRate"),
            } for record in records)
            balances = [
                value for value in (_num_real(r.get("LoanBalance")) for r in records)
                if value is not None
            ]
            raw_balance = sum(balances) if balances else None
            loan_balances.extend(balances)
            first = records[0] if records else {}
            cur = first.get("Currency") or "TWD"
            loan_type = first.get("LoanKind") or "貸款"
            raw = {"AcctText": loan_type, "currency": cur}
            accts.append({
                "account_no": detail["account"],
                "currency": cur,
                "branch": None,
                "nickname": first.get("LoanAcctCName") or loan_type,
                "type": loan_type,
                "product_type": account_classify.classify_account("sinopac", raw),
                "raw_balance": raw_balance,
                "raw_balance_date": today,
            })
        if loan_balances:
            loan_total = round(sum(loan_balances))
    if accts:
        store.upsert_accounts(accts)
    if twd_total or fx_total or loan_total is not None:
        store.upsert_balance_history([{
            "snapshotDate": today,
            "twdBalance": twd_total if twd_total else None,
            "fxBalance": fx_total if fx_total else None,
            "loanBalance": loan_total,
        }])
        delta["balance_days"] = 1
        store.put_daily_metric(
            "balance_latest", {"twd": twd_total, "fx_raw": fx_total, "loan": loan_total}, today,
        )

    # --- 信用卡清單（UPSERT）---
    # Step 2 (2026-06-14): all_cards.Result.Items 沒給 limit/已用/帳單日,
    # 只有 ExpDate (MMYY 格式 e.g. '0829' = 2029/08). 推 active:
    #   ExpDate <  今月 → 過期, active=0
    #   ExpDate >= 今月 → 有效, active=1
    # 2026-06-14 Step 2 升級：從 card_statements[最新月] 套整戶 due/stmt 到每張卡。
    # 2026-06-14 Step 3 升級：card_summary[].SubInfo[][] 是 [{DataText, DataValue}] 結構,
    #   藏「信用額度(臺幣)」/「以刷卡未請款金額」等欄, 解析後套到 cards.
    # 2026-06-22 Step 4 (audit findings): 補 bill_due_amount / last_payment_amount /
    #   last_payment_date 三欄. 永豐自扣 records 「永豐自扣已入帳，謝謝！」 desc + amount<0
    #   是 last_payment 真實 source, statement.summary.paid 是已繳金額.
    #   整戶層 by-design: sinopac 多卡共用同一帳單統合, 跟 ubot 同 pattern.
    stmt_list = data.get("card_statements") or []
    sinopac_due = None
    sinopac_stmt = None
    sinopac_bill_due = None
    sinopac_last_pay_amt = None
    sinopac_last_pay_date = None
    if stmt_list and isinstance(stmt_list[0], dict):
        # card_statements 已按月份新→舊排序，[0] 是最新月
        latest = stmt_list[0]
        sinopac_due = latest.get("payment_due_date")  # '2026/06/01'
        sinopac_stmt = latest.get("billing_cycle_date")  # '2026/05/17'
        # 轉 ISO YYYY-MM-DD
        if sinopac_due and "/" in sinopac_due:
            sinopac_due = sinopac_due.replace("/", "-")
        if sinopac_stmt and "/" in sinopac_stmt:
            sinopac_stmt = sinopac_stmt.replace("/", "-")
        # 2026-07-02: card_statements.summary.paid 只是帳單彙總金額，不能單獨
        # 生成 last_payment；必須看到 records 內真實「自扣已入帳」row 才寫日期/金額。
        # 另外不能只看最新月份：最新 statement 常尚未出現自扣 row，要跨已抓月份找最新 payment row。
        summary = latest.get("summary") or {}
        sinopac_bill_due = _num_to_float(summary.get("current_due"))
        payment_rows = []
        for st in stmt_list:
            if not isinstance(st, dict):
                continue
            for r in (st.get("records") or []):
                if not isinstance(r, dict):
                    continue
                desc = r.get("description") or ""
                amt = _num_to_float(r.get("amount"))
                if amt is None or amt >= 0:
                    continue
                if "自扣" in desc and "入帳" in desc:
                    td = _sinopac_date(r.get("trans_date") or r.get("post_date"))
                    if td:
                        payment_rows.append((td, abs(amt)))
        if payment_rows:
            sinopac_last_pay_date, sinopac_last_pay_amt = max(payment_rows, key=lambda x: x[0])

    # ⚠️ 2026-06-14 Step 3: 從 card_summary 解析 limit / used / stmt / due
    # 結構: card_summary = [{"TitleInfo": "", "SubInfo": [[{"DataText", "DataValue"}, ...]]}]
    # 兩層 list — 外層每張卡一筆 / 內層 SubInfo 是 group of rows
    sinopac_limit = None
    sinopac_used = None
    cs_list = data.get("card_summary") or []
    if cs_list and isinstance(cs_list[0], dict):
        # 把第一張卡的 SubInfo 攤平成 dict
        kv_map: dict[str, str] = {}
        for group in cs_list[0].get("SubInfo") or []:
            if not isinstance(group, list):
                continue
            for row in group:
                if isinstance(row, dict) and "DataText" in row and "DataValue" in row:
                    kv_map[row["DataText"]] = row["DataValue"]
        # "信用額度(臺幣)" / "信用額度" 都試
        for k in ("信用額度(臺幣)", "信用額度"):
            v = kv_map.get(k)
            if v:
                sinopac_limit = _num_to_float(v)
                if sinopac_limit:
                    break
        # used: "以刷卡未請款金額" (即時) + "本期應繳" fallback
        used_unbilled = _num_to_float(kv_map.get("以刷卡未請款金額"))
        used_due = _num_to_float(kv_map.get("本期應繳"))
        # 優先抓未請款(即時), 沒有就用本期應繳
        sinopac_used = used_unbilled if used_unbilled is not None else used_due
        # 2026-06-23: card_summary raw 明確有本期應繳 / 最近繳款金額 / 最近繳款日期.
        # 若 card_statements 沒帶 summary/records (或 card_no mapping 對不上), 仍要從
        # card_summary 寫入 cards native 欄，讓 UI 最近繳款 + payments fallback 能顯示.
        if sinopac_bill_due is None:
            sinopac_bill_due = _num_to_float(kv_map.get("本期應繳"))
        if sinopac_last_pay_amt is None:
            sinopac_last_pay_amt = _num_to_float(kv_map.get("最近繳款金額"))
        if not sinopac_last_pay_date:
            v = kv_map.get("最近繳款日期")
            if v and "/" in v:
                sinopac_last_pay_date = v.replace("/", "-")
        # card_summary 也有自己的 stmt/due, 用作 fallback (若 card_statements 沒抽到)
        if not sinopac_stmt:
            v = kv_map.get("結帳日")
            if v and "/" in v:
                sinopac_stmt = v.replace("/", "-")
        if not sinopac_due:
            v = kv_map.get("繳款截止日")
            if v and "/" in v:
                sinopac_due = v.replace("/", "-")

    ac = data.get("all_cards") or {}
    if isinstance(ac, dict):
        items = (ac.get("Result") or {}).get("Items", []) or []
        cards = [
            {"number": it.get("CardNo"), "name": it.get("Name"),
             "type": it.get("CardTypeDesc"), "association": it.get("CardBrand"),
             "is_cube": False,
             # active: ExpDate 'MMYY' < 今月 → 過期
             "active": not _mmyy_expired(it.get("ExpDate")),
             # Step 2/3: 套整戶 limit/used/due/stmt
             "credit_limit": sinopac_limit,
             "used_credit": sinopac_used,
             "payment_due_date": sinopac_due,
             "statement_close_date": sinopac_stmt,
             # 2026-06-22 Step 4: 整戶層 bill_due + last_payment 套每張卡
             "bill_due_amount": sinopac_bill_due,
             "last_payment_amount": sinopac_last_pay_amt,
             "last_payment_date": sinopac_last_pay_date,
             }
            for it in items if it.get("CardNo")
        ]
        if cards:
            store.upsert_cards(cards)

    # --- 每日快照：信用卡彙總 / 帳單 / 資產分析 / 扣款帳戶 / 貸款明細 ---
    for key, mtag in [("card_summary", "card_summary"), ("card_billing", "card_billing"),
                       ("asset_chart", "asset_chart"), ("debit_accounts", "debit_accounts")]:
        v = data.get(key)
        if v:
            store.put_daily_metric(mtag, v, today)
    if loan_metric_records:
        store.put_daily_metric("loan", {"records": loan_metric_records}, today)

    # --- 台幣交易明細（永豐 ws_transdetailMerge.ashx，欄位 DataText1~11）---
    # DataText 對應：
    #   DataText1 = 交易日 + 時間 (HTML: 'YYYY/MM/DD<br />HH:MM')
    #   DataText2 = 入帳日 (YYYY/MM/DD)
    #   DataText3 = 交易類別 (官方分類名: 「台幣匯款」「手機轉帳」「利息存入」「ATM」...)
    #   DataText4 = 金額 (HTML: '<font color="...">±NNN</font>'; +/- 區分收支)
    #   DataText5 = 餘額
    #   DataText8 = 對方資訊 (含對方帳號 + 對方名稱 + 摘要; 是真正「跟誰交易」資訊)
    #
    # 鐵則: raw description 永遠 = 銀行官方原文 (DataText3 = 交易類別), 不蓋。
    # counterparty_acct + memo 額外存對方資訊。Display join 在 backend transform
    # `_twd_to_transaction` 統一處理 (`description · counterparty_acct` 對齊 MoneyBook),
    # 所有銀行通用 — 不在 persist 層做。
    twd_new = 0
    for body in data.get("twd_transactions") or []:
        if not isinstance(body, dict):
            continue
        acct_no = body.get("account")
        rows = []
        for t in body.get("records", []) or []:
            # DataText1 = '2026/06/09<br />19:13' → 交易日期 + 時間
            dt_raw = _sinopac_strip_html(t.get("DataText1", "")).replace("\n", " ")
            # 截 'YYYY/MM/DD' 和後面的 'HH:MM' 合一行
            expend, income = _sinopac_split_amount(t.get("DataText4"))
            rows.append({
                "account_no": acct_no,
                "datetime": dt_raw,  # 原 '2026/06/09 19:13' 留字串
                "account_date": _sinopac_date(_sinopac_strip_html(t.get("DataText2"))),
                "desc": _sinopac_strip_html(t.get("DataText3")),
                "expend": expend,
                "income": income,
                "balance": _num(_sinopac_strip_html(t.get("DataText5"))),
                "counterparty_bank": None,
                "counterparty_acct": _sinopac_strip_html(t.get("DataText8"))[:30] or None,
                "memo": _sinopac_strip_html(t.get("DataText8")) or None,
            })
        twd_new += store.upsert_twd_txns(rows, rules=rules)
    delta["twd_txn_new"] = twd_new

    # 預留位（dropdown 破完再補）
    delta.setdefault("balance_days", 0)

    # --- 信用卡帳單已請款明細（StatementInquiry HTML 解析）---
    billed_new = 0
    # card_statements: list[{month, billing_cycle_date, payment_due_date, summary, records[]}]
    # 永豐 StatementInquiry 頁有「主卡」卡號顯示但 records 只給 last4，用 all_cards 補：
    cards_by_last4: dict[str, str] = {}
    ac_items = ((data.get("all_cards") or {}).get("Result") or {}).get("Items") or []
    for it in ac_items:
        cn = (it.get("CardNo") or "").strip()
        if len(cn) >= 4:
            cards_by_last4[cn[-4:]] = cn
    for m in data.get("card_statements") or []:
        if not isinstance(m, dict):
            continue
        bill_date = m.get("billing_cycle_date")
        rows = []
        for r in m.get("records") or []:
            last4 = r.get("card_last4")
            card_no = cards_by_last4.get(last4 or "", last4)
            desc = r.get("description")
            amt = _num(r.get("amount"))
            rows.append({
                "card_no": card_no,
                "bill_date": bill_date,
                "currency": "TWD",
                "date": r.get("trans_date"),
                "post_date": r.get("post_date"),
                "desc": desc,
                "amount": amt,
                "consume_country": None,
                "consume_currency": None,
                "consume_amount": None,
                "txn_type": classify.classify_by_desc_and_sign(desc, amt),
            })
        if rows:
            billed_new += store.upsert_card_billed(rows, rules=rules)
    delta["card_billed_new"] = billed_new

    # --- 信用卡未請款（LatestTx + OutstandingDetail API）---
    unb = data.get("card_unbilled") or {}
    unb_rows = []
    if isinstance(unb, dict):
        latest = unb.get("latest_tx") or {}
        if isinstance(latest, dict):
            items = ((latest.get("Result") or {}).get("Items") or [])
            for t in items:
                if not isinstance(t, dict):
                    continue
                # 欄位名稱未明（API 回空）暫用通用映射，實測有資料時再調
                card_no = t.get("CardNo") or t.get("CardNumber")
                desc = t.get("MerchantName") or t.get("Description") or t.get("Memo")
                amt = _num(t.get("AuthAmount") or t.get("Amount"))
                unb_rows.append({
                    "card_no": card_no,
                    "date": t.get("AuthDate") or t.get("ConsumeDate") or t.get("TransDate"),
                    "desc": desc,
                    "amount": amt,
                    "currency": t.get("Currency") or "TWD",
                    "txn_type": classify.classify_by_desc_and_sign(desc, amt),
                })
    latest_raw = unb.get("latest_tx") if isinstance(unb, dict) else None
    latest_result = latest_raw.get("Result") if isinstance(latest_raw, dict) else None
    latest_ok = (isinstance(latest_raw, dict)
                 and str(latest_raw.get("ResultCode")) == "00"
                 and latest_raw.get("Error") in (None, "")
                 and isinstance(latest_result, dict)
                 and isinstance(latest_result.get("Items"), list))
    delta["card_unbilled"] = store.refresh_card_pending(
        "unbilled", unb_rows, rules=rules, fetch_ok=latest_ok)
    delta["card_current"] = 0

    store.log_sync(delta)
    return delta
