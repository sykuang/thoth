"""中國信託 (CTBC) persist 邏輯.

從 persist.py 拆分而來 (2026-06-17, W persist.py split refactor)。
"""
from __future__ import annotations

from datetime import datetime

from backend.core import account_classify, classify
from backend.core.store import BankStore
from backend.core.persist._common import _num_real, _num_to_float, _slash_date_to_iso


def _to_num(s):
    """'7' / '12,781' / 7 → 數值；空/異常 → None。"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return s
    t = str(s).replace(",", "").strip()
    try:
        v = float(t)
        return int(v) if v == int(v) else v
    except (ValueError, TypeError):
        return None

def _bill_cycle_to_latest_stmt_date(cycle_day, today: datetime | None = None) -> str | None:
    """CTBC 'billCycle' (每月結帳日, e.g. '17') → 最近一次已結帳的 ISO 日期.

    範例: today=2026-06-13, cycle='17' → '2026-05-17' (今天 13 < 17, 還沒到本月結帳)
          today=2026-06-20, cycle='17' → '2026-06-17' (今天 20 >= 17, 已過本月結帳)
    None / 解析失敗 → None.
    """
    if cycle_day is None or cycle_day == "":
        return None
    try:
        day = int(cycle_day)
        if not 1 <= day <= 31:
            return None
    except (ValueError, TypeError):
        return None
    now = today or datetime.now()
    if now.day >= day:
        # 本月已過結帳日
        try:
            return now.replace(day=day).strftime("%Y-%m-%d")
        except ValueError:
            return None
    else:
        # 本月還沒到結帳日, 上一次結帳是上個月 day 號
        prev_month = now.month - 1 or 12
        prev_year = now.year if now.month > 1 else now.year - 1
        try:
            return datetime(prev_year, prev_month, day).strftime("%Y-%m-%d")
        except ValueError:
            return None

def _normalize_ctbc_datetime(act_dt_tm: str) -> str | None:
    """CTBC actDtTm '2026-06-02-14.53.14.296159' → '2026-06-02 14:53:14'.

    CTBC raw 用 '-' 分隔日期跟時間、'.' 分隔時分秒，後綴 microseconds 不要。
    格式異常 → 回 None.
    """
    if not act_dt_tm or not isinstance(act_dt_tm, str):
        return None
    # split first 3 '-' (year-month-day), 餘下是 'HH.MM.SS.uuuuuu'
    parts = act_dt_tm.split("-", 3)
    if len(parts) != 4:
        return None
    y, m, d, time_part = parts
    if len(y) != 4 or len(m) != 2 or len(d) != 2:
        return None
    # 抓 HH.MM.SS, 砍 .uuuuuu
    time_segs = time_part.split(".")
    if len(time_segs) < 3:
        return None
    hh, mm, ss = time_segs[0], time_segs[1], time_segs[2]
    return f"{y}-{m}-{d} {hh}:{mm}:{ss}"


def _ctbc_yyyymmdd_to_iso(s: str) -> str | None:
    """'20260602' → '2026-06-02'。"""
    if not s or not isinstance(s, str) or len(s) != 8 or not s.isdigit():
        return None
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def _parse_ctbc_twd_history(twd_history: list) -> list[dict]:
    """中信 twd_history collect 結構 → store.upsert_twd_txns 接受的 dict list.

    twd_history = [{account_no, months: {m0: [detail,...], ...}, errors}].
    每筆 detail 欄位 → store schema:
      actDtTm → datetime (normalize '2026-06-02 14:53:14')
      trnDtRaw → account_date ('2026-06-02')
      memo1 + memo2 → desc ('跨行轉 永豐銀')
      dbAmt → expend (int)
      crAmt → income (int)
      balanceAmt → balance (拔 comma → int)
      bankId → counterparty_bank
      trfAcct → counterparty_acct
      memoCode → memo

    跨 m0..m5 月可能 detailList 重複（CTBC 月窗 overlap 在月初/月底），
    交給 store.upsert_twd_txns 的 dedup_key 去重，不在這邊 dedup.
    """
    rows: list[dict] = []
    for acct in (twd_history or []):
        account_no = acct.get("account_no")
        if not account_no:
            continue
        months = acct.get("months") or {}
        for _t, detail_list in months.items():
            for d in (detail_list or []):
                if not isinstance(d, dict):
                    continue
                desc_parts = [
                    (d.get("memo1") or "").strip(),
                    (d.get("memo2") or "").strip(),
                ]
                desc = " ".join(p for p in desc_parts if p) or None
                # Invariant (collector 守門 — banks/ctbc.py:_collect_twd_deposit_history):
                # 上游 collector 已 skip 缺 actDtTm 的 raw row, 走到這裡的 detail
                # 一定有 actDtTm. _normalize_ctbc_datetime 對合法格式 return str,
                # 對極罕見的 format anomaly fall back None — 那種 case 是真 raw 病
                # (不只是缺欄), 讓 PG NOT NULL 炸出來 caller 才會被 alert 去查.
                rows.append({
                    "account_no": account_no,
                    "datetime": _normalize_ctbc_datetime(d.get("actDtTm") or ""),
                    "account_date": _ctbc_yyyymmdd_to_iso(d.get("trnDtRaw") or ""),
                    "desc": desc,
                    "expend": _to_num(d.get("dbAmt")),
                    "income": _to_num(d.get("crAmt")),
                    "balance": _to_num(d.get("balanceAmt")),
                    "counterparty_bank": (d.get("bankId") or "").strip() or None,
                    "counterparty_acct": (d.get("trfAcct") or "").strip() or None,
                    "memo": (d.get("memoCode") or "").strip() or None,
                })
    return rows


def persist_ctbc(data: dict, store: BankStore, rules: list[dict] | None = None) -> dict:
    """CTBC collect() 結構 → store。

    CTBC 抓取覆蓋層：
      summary.twdDepositSummary    → daily_metric balance_latest
      summary.creditCardSummary    → daily_metric card_limit
      twd_deposit.demDepBalSummaryResponse.infoList → accounts(UPSERT 台幣帳戶)
      twd_history (2026-06-20 補上) → twd_transactions (近 6 個月逐筆)
      card_api_dump → cards / card_billed_txns / card_pending_txns
    """
    today = datetime.now().strftime("%Y-%m-%d")
    delta: dict = {}
    summary = data.get("summary") or {}

    # 台幣存款帳戶（UPSERT accounts）
    dep = data.get("twd_deposit") or {}
    info_list = ((dep.get("demDepBalSummaryResponse") or {}).get("infoList")
                 if isinstance(dep, dict) else None) or []
    acct_rows = []
    for a in info_list:
        # 中信 raw 只有 acctType code（'00'）難 keyword，所以在 raw 餵 type=活期存款
        # 讓 classifier 命中 keyword → deposit
        raw = {**a, "type": "活期存款", "currency": "TWD"}
        acct_rows.append({
            "account_no": a.get("accountId", ""),
            "currency": "TWD",
            "type": "活期存款",
            "product_type": account_classify.classify_account("ctbc", raw),
            "nickname": a.get("accountNickName") or None,
            "raw_balance": _num_real(a.get("balance")),
            "raw_balance_date": today,
        })
    # 信貸帳戶（loanCreditSummary）：拔除 — extraAmt 是「可動用上限」(quota)
    # 不是「已動用金額」，homepage ebmwResource 無 used/drawn endpoint，
    # 真正 endpoint 在 mega-menu hover 進信貸頁但 headless 失敗（probe v1+v2 兩次
    # 都失敗）。使用者明確指示：「沒動信貸就不要顯示」（transferEnabled=False 證
    # 實未動用），不該為了「結構完整」造視覺垃圾 row 給使用者看。
    # 若使用者未來真的動用信貸 → 重新規劃 collector 進信貸頁抓 used，再回來加 row。
    if acct_rows:
        store.upsert_accounts(acct_rows)
    delta["accounts"] = len([a for a in acct_rows if a.get("account_no")])

    # 台幣已過帳交易（append-only, 回真正新增筆數）
    # 2026-06-20: 補上 known TODO. collect() 抓近 6 個月 detailList 進 twd_history.
    # _parse_ctbc_twd_history 拍平成 store schema dict, upsert_twd_txns 用 dedup_key 去重.
    twd_history_rows = _parse_ctbc_twd_history(data.get("twd_history") or [])
    twd_new = store.upsert_twd_txns(twd_history_rows, rules=rules) if twd_history_rows else 0
    delta["twd_txn_new"] = twd_new

    # 餘額快照（每日 metric）——含各帳號餘額（accounts 表不存餘額）
    twd = summary.get("twdDepositSummary") or {}
    if twd or info_list:
        store.put_daily_metric("balance_latest", {
            "twd": _to_num(twd.get("totalCurrentBal")) if twd else None,
            "accounts": [{"account_no": a.get("accountId"), "balance": _to_num(a.get("balance")),
                          "available": _to_num(a.get("availableBalance"))} for a in info_list],
        }, today)
    cc = summary.get("creditCardSummary") or {}
    if cc:
        store.put_daily_metric("card_limit", {
            "quota": _to_num(cc.get("quota")), "available": _to_num(cc.get("availBal")),
            "unpaid": _to_num(cc.get("unpaidStmt")), "due_date": cc.get("pmtExpDt"),
        }, today)
    # daily_metric loan_credit 也拔除：quota 沒實質意義（不影響淨資產也不影響支出），
    # 每天記只會把 daily_metrics 表越塞越大。若未來真要追蹤可動用額度變化再加回。
    delta["balance_days"] = 1

    # 信用卡明細 (2026-06-13 升級：分 pending/billed/cards 三路抓)
    # CTBC card_api_dump endpoint 對照：
    #   /twrbc-card/qu041/010 = 即時消費 (pending, allItems)
    #   /twrbc-card/qu002/010 = 帳單明細 (billed, billData.TWD.{月}.bills[])
    #                          + cardDataList = 4 張卡片完整資訊
    #   /twrbc-card/qu006/011 = 未出帳單 (unbilled, allItems)
    card_api = data.get("card_api_dump") or {}

    # --- 1) cards: 優先從 qu002/010 cardDataList 抓（最完整：名稱 + 正附卡 + masked full no）---
    # Step 2 (2026-06-14): summary.creditCardSummary 是整戶層 quota/已用/繳費日,
    # CTBC 沒給 per-card limit (qu002/010 cardDataList 只有名稱/正附卡標記),
    # 同 Cathay 處理: 整戶值套到每張卡 (CTBC API 沒給 per-card endpoint, 跟國泰一樣 raw API 設計限制).
    cc_summary = (data.get("summary") or {}).get("creditCardSummary") or {}
    # ⚠️ 2026-06-14 Step 3 重大修正：CTBC summary 欄位語意校正
    #
    #   舊 (錯)：ctbc_credit_limit = quota  → 抓到 25,025（其實是「本期應繳」不是 limit）
    #   證據：歷史月 currPmtAmt 多次超過 25,025（1月 84,843 / 2月 79,463），不可能 limit 才 25,025
    #
    #   官方語意 (https://www.ctbcbank.com/...Card_Notice.html)：
    #     可用餘額 (availBal) = 信用額度 - 循環信用未結清 - 已使用未入帳
    #   即時推算：信用額度 ≈ quota + availBal（quota=本期應繳已從額度扣，availBal=還可花）
    #   25,025 + 674,975 = 700,000（整 70 萬，符合 CTBC 常見額度）
    #
    #   used_credit 改用 quota（本期應繳）— 比 unpaidStmt=0 準確
    #   (unpaidStmt=0 是「循環信用未結清」即上期未繳, 0 表示有按時繳;
    #    quota=本期應繳=本期已花已出帳金額, 才是真正的 used)
    ctbc_quota = _num_to_float(cc_summary.get("quota"))  # 本期應繳
    ctbc_avail = _num_to_float(cc_summary.get("availBal"))  # 可用餘額
    ctbc_credit_limit = (
        (ctbc_quota + ctbc_avail) if (ctbc_quota is not None and ctbc_avail is not None) else None
    )
    ctbc_used = ctbc_quota  # 本期應繳 = used_credit
    ctbc_due = _slash_date_to_iso(cc_summary.get("pmtExpDt"))
    # 結帳日: billCycle='17' 是每月結帳日 day-of-month, 推算最近一次已結帳的日期
    ctbc_stmt = _bill_cycle_to_latest_stmt_date(card_api.get("/twrbc-card/qu002/010", {}).get("billCycle"))

    # 2026-07-02: CTBC 真實繳款紀錄在 mega menu「信用卡繳款記錄」
    # `/twrbc-card/qu038/011.rsData.billDataTWD[]`：payDt=繳款日、postingDt=入帳日、
    # amt=繳款金額、merchantChiName=本行扣繳。舊邏輯用 qu002 billData.summary.pmtAmt
    # + billDt 只是帳單 summary / 統計日 approximate，不可再當 last_payment source。
    ctbc_bill_due = _num_to_float(cc_summary.get("unpaidStmt"))
    ctbc_last_pay_amt = None
    ctbc_last_pay_date = None
    qu038_for_pay = card_api.get("/twrbc-card/qu038/011") or {}
    payment_rows = []
    if isinstance(qu038_for_pay, dict):
        for key, rows in qu038_for_pay.items():
            if not str(key).startswith("billData") or not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                pay_date = _slash_date_to_iso(row.get("payDt"))
                amt = _num_to_float(row.get("amt"))
                desc = str(row.get("merchantChiName") or "")
                if not pay_date or amt is None:
                    continue
                if desc and not any(k in desc for k in ("扣繳", "繳", "Payment", "payment")):
                    continue
                payment_rows.append((pay_date, abs(amt)))
    if payment_rows:
        latest_pay_date, latest_pay_amt = max(payment_rows, key=lambda x: x[0])
        ctbc_last_pay_date = latest_pay_date
        ctbc_last_pay_amt = latest_pay_amt

    seen_cards: dict[str, dict] = {}
    qu002_data = card_api.get("/twrbc-card/qu002/010") or {}
    if isinstance(qu002_data, dict):
        card_list = qu002_data.get("cardDataList") or []
        # 第一遍：統計各 last4 出現次數，決定是否需要 suffix
        from collections import Counter
        last4_counts = Counter()
        for c in card_list:
            if not isinstance(c, dict):
                continue
            suffix_raw = (c.get("cardNoSuffixFour") or "").strip()
            if not suffix_raw:
                continue
            last4_counts[suffix_raw.split("_")[0]] += 1

        # 第二遍：建 card row（同 last4 多張時加 _seq 區分）
        for c in card_list:
            if not isinstance(c, dict):
                continue
            # ⚠️ 2026-06-14 PK 衝突修：cardNoSuffixFour = "3443_1" / "3443_2"
            # 的 "_N" 是 CTBC 用來區分同 last4 多張卡（正卡 vs 附卡）的序號。
            # 之前 split("_")[0] 砍掉 suffix，導致正附卡在 cards 表互相覆蓋。
            # 修：同 last4 ≥2 張時 PK = "****7036_1" / "****7036_2"，
            # 單張時保持 "****7036"（向後相容舊資料）。
            suffix_raw = (c.get("cardNoSuffixFour") or "").strip()
            if not suffix_raw:
                continue
            parts = suffix_raw.split("_")
            last4 = parts[0]
            seq = parts[1] if len(parts) > 1 else "0"
            # 多張：加 suffix；單張：純 last4
            pk = f"****{last4}_{seq}" if last4_counts[last4] >= 2 else f"****{last4}"

            primary_flag = c.get("positiveOrAttached") or "正卡"
            card_name = c.get("cardName") or f"中信卡 {pk}"
            # 同 last4 多張時 name 加「（正卡）/（附卡）」幫忙辨識
            if last4_counts[last4] >= 2 and primary_flag and primary_flag not in card_name:
                card_name = f"{card_name}（{primary_flag}）"

            seen_cards[pk] = {
                "number": pk,
                "name": card_name,
                "association": None,
                "type": "credit",
                "currency": "TWD",
                "card_no_full_masked": c.get("cardNo", ""),  # '9000-56**-****-7036'
                "primary": primary_flag,
                # Step 2: 整戶值套到每張卡
                "credit_limit": ctbc_credit_limit,
                "used_credit": ctbc_used,
                "statement_close_date": ctbc_stmt,
                "payment_due_date": ctbc_due,
                # 2026-06-22 (audit): bill_due + last_payment_amount 整戶層套
                "bill_due_amount": ctbc_bill_due,
                "last_payment_amount": ctbc_last_pay_amt,
                # 2026-06-22 v3: last_payment_date 從 billData[最新月].summary.billDt 推
                # (帳單統計日精度, 跟 pmtAmt 同月份)
                "last_payment_date": ctbc_last_pay_date,
            }

    # --- 2) billed: 從 qu002/010 billData.TWD.{月}.bills[] 抓逐筆 + 月份 summary ---
    billed_rows = []
    months_summary: dict = {}  # {月份: summary dict} 給 daily_metric 用
    if isinstance(qu002_data, dict):
        bill_data = qu002_data.get("billData") or {}
        for currency, months_dict in bill_data.items():  # 通常只有 'TWD'
            if not isinstance(months_dict, dict):
                continue
            for month_str, month_info in months_dict.items():  # '2026/05' / '2026/04' ...
                if not isinstance(month_info, dict):
                    continue
                summary = month_info.get("summary") or {}
                if summary:
                    months_summary[f"{currency}/{month_str}"] = summary
                bills = month_info.get("bills") or []
                for t in bills:
                    if not isinstance(t, dict):
                        continue
                    last4 = (t.get("cardNo") or "").split("_")[0].strip()
                    # 日期欄位 (CTBC 格式 MMDDYY，使用者實證 050526 = 2026/05/05)
                    pdt = (t.get("purchaseDt") or "").strip()
                    if len(pdt) == 6 and pdt.isdigit():
                        mm, dd, yy = pdt[:2], pdt[2:4], pdt[4:6]
                        consume_date = f"20{yy}-{mm}-{dd}"
                    else:
                        consume_date = None
                    # 入帳日 = postingDt (CTBC raw API 有此欄位，2026-06-19 修正)
                    # 之前誤判「CTBC 無單獨入帳日」把 post_date = consume_date copy，
                    # 實證 raw API 有 postingDt (e.g. '060826' = 2026/08/06)。
                    # MMDDYY 格式 (跟 purchaseDt 一致)，'000000' 視為缺值。
                    pst = (t.get("postingDt") or "").strip()
                    if len(pst) == 6 and pst.isdigit() and pst != "000000":
                        mm, dd, yy = pst[:2], pst[2:4], pst[4:6]
                        post_date = f"20{yy}-{mm}-{dd}"
                    else:
                        # 缺值時 fallback consume_date (避免 NOT NULL 違反，
                        # 跟舊 row 顯示行為一致 — 顯示「入帳日 = 消費日」優於 null)
                        post_date = consume_date
                    # bill_date: 用月份開頭 1 日當代表（月份 '2026/05'）
                    bill_date = f"{month_str.replace('/', '-')}-01" if "/" in month_str else None
                    # 外幣處理
                    # CTBC billed 真實外幣欄位 = occCurCode（使用者實證 EUR）
                    # origCurCode 是 'I'/'N' 垃圾欄不要用（pending 用的是 origCurCode 字母才有效，billed 不同！）
                    foreign_amt_str = t.get("foreignAmt", "") or ""
                    orig_cur = (t.get("occCurCode") or "").strip()
                    fx_amount = None
                    consume_currency = None
                    if orig_cur and len(orig_cur) >= 2 and orig_cur != "TWD":
                        consume_currency = orig_cur
                        try:
                            fx_amount = float(str(foreign_amt_str).replace(",", "")) if foreign_amt_str else None
                        except (ValueError, TypeError):
                            fx_amount = None
                    desc = (t.get("merchantChiName") or "").strip()
                    amt_signed = _to_num(t.get("ntAmt"))
                    billed_rows.append({
                        "card_no": f"****{last4}" if last4 else None,
                        "bill_date": bill_date,
                        "date": consume_date,
                        "post_date": post_date,  # CTBC postingDt (raw API 有此欄位)
                        "desc": desc,
                        "amount": amt_signed,  # 台幣入帳金額（可正可負，扣繳會是負）
                        "currency": "TWD",
                        "consume_currency": consume_currency,
                        "consume_amount": fx_amount,
                        "txn_type": classify.classify_ctbc(t.get("txCode"), desc, amt_signed),
                    })

    # --- 3) pending: 抓 qu006/011 未出帳單, 但仍不抓 qu041/010 即時授權 ---
    # 2026-06-19: qu041/010 即時消費 API 給的是授權 placeholder 視角, 跟 billed
    # 永遠對不齊 (txnDate/merchName/authCode/country 都可能不同), 因此不入庫。
    # 2026-06-25: 使用者對照 MoneyBook 發現兩筆「９１ＡＰＰ＊ＩＳＰＯ＋」漏掉 —
    # root cause 是我們把真正的「未出帳單」qu006/011 allItems 也一起忽略了。
    # qu006/011 已有 purchaseDt/postingDt/description/purchaseAmt, 是可顯示的真實
    # unbilled source；只忽略 qu041 placeholder, parse qu006.
    pending_rows = []
    qu006_raw = card_api.get("/twrbc-card/qu006/011")
    qu006_detail = qu006_raw if isinstance(qu006_raw, dict) else {}
    for t in (qu006_detail.get("allItems") or []) if isinstance(qu006_detail, dict) else []:
        if not isinstance(t, dict):
            continue
        pdt = (t.get("purchaseDt") or "").strip()
        if len(pdt) == 8 and pdt.isdigit():
            consume_date = f"{pdt[:4]}-{pdt[4:6]}-{pdt[6:8]}"
        else:
            # CTBC qu006 rows without purchaseDt are structural noise; skip rather than
            # creating undated pending rows that cannot dedupe or display honestly.
            continue
        suffix_raw = (t.get("cardNoSuffixFour") or "").strip()
        last4 = suffix_raw.split("_")[0]
        desc = (t.get("description") or "").strip()
        amt = _to_num(t.get("purchaseAmt"))
        if not last4 or not desc or amt is None:
            continue
        orig_desc = (t.get("origCurDesc") or "").strip()
        orig_amt = _num_to_float(t.get("origCurAmt"))
        consume_currency = orig_desc if orig_desc and orig_desc != "TWD" else None
        consume_amount = orig_amt if consume_currency else None
        pending_rows.append({
            "card_no": f"****{last4}",
            "date": consume_date,
            "post_date": f"{(t.get('postingDt') or '').strip()[:4]}-{(t.get('postingDt') or '').strip()[4:6]}-{(t.get('postingDt') or '').strip()[6:8]}" if len((t.get("postingDt") or "").strip()) == 8 and (t.get("postingDt") or "").strip().isdigit() else consume_date,
            "desc": desc,
            "amount": amt,
            "currency": "TWD",
            "consume_country": (t.get("countryCode") or "").strip() or None,
            "consume_currency": consume_currency,
            "consume_amount": consume_amount,
            "txn_type": classify.classify_ctbc(t.get("txCode"), desc, amt),
        })

    # --- 4) 把 cards 從 qu041 也補進去（萬一 qu002 沒給但 qu041 有出現的卡）---
    # 雖然不再抓 pending txn, 但 qu041 仍可能是 cards 唯一來源 (邊緣卡未在帳單期).
    # 注意：seen_cards 的 key 是 PK（"****7036" 或 "****7036_1"），不是 last4。
    # 反查時要先把所有已存在的 last4 集合起來再比對。
    qu041_data = card_api.get("/twrbc-card/qu041/010") or {}
    existing_last4s = {pk.replace("****", "").split("_")[0] for pk in seen_cards}
    for t in (qu041_data.get("allItems") or []) if isinstance(qu041_data, dict) else []:
        last4 = (t.get("cardNoSuffixFour") or "").strip().split("_")[0]
        if last4 and last4 not in existing_last4s:
            pk_fb = f"****{last4}"
            seen_cards[pk_fb] = {
                "number": pk_fb,
                "name": f"中信卡 {pk_fb}",
                "association": None,
                "type": "credit",
                "currency": "TWD",
                # Step 2: 整戶值套到 fallback 卡 (跟主 cards 一致)
                "credit_limit": ctbc_credit_limit,
                "used_credit": ctbc_used,
                "statement_close_date": ctbc_stmt,
                "payment_due_date": ctbc_due,
            }
            existing_last4s.add(last4)

    # --- 寫入 store ---
    cards_n = 0
    if seen_cards:
        store.upsert_cards(list(seen_cards.values()))
        cards_n = len(seen_cards)

    billed_n = 0
    if billed_rows:
        billed_n = store.upsert_card_billed(billed_rows, rules=rules) or 0

    # 永遠 call refresh_card_pending 即使 pending_rows=[]:
    # 用 DELETE+INSERT semantics 來 sweep 升級前殘留的 pending row (本次 INSERT 0 筆).
    # fetch_ok: qu006/011 必須明示 allItems list；任意 error dict 不算成功，
    # pending_rows 空也不可拿來做消失比對。
    qu006_ok = (isinstance(qu006_raw, dict)
                and not any(qu006_raw.get(key) for key in ("error", "Error", "errorMessage"))
                and isinstance(qu006_raw.get("allItems"), list))
    pending_n = store.refresh_card_pending(
        "unbilled", pending_rows, rules=rules,
        fetch_ok=qu006_ok)

    if months_summary:
        store.put_daily_metric("ctbc_bill_months_summary", months_summary, today)

    # 2026-06-22 (mega menu probe raw 留底, 明早 sync 後從 PG 撈出來看 menu 全 list,
    # 找「繳款紀錄」類 menu, ship 0.3.32 hard-code 進 card_targets).
    mega_menu = data.get("card_mega_menu_dump")
    if mega_menu:
        store.put_daily_metric("ctbc_card_mega_menu_dump", mega_menu, today)

    delta["cards"] = cards_n
    delta["card_billed_new"] = billed_n
    delta["card_unbilled"] = pending_n
    delta["bill_months"] = len(months_summary)

    store.log_sync(delta)
    return delta
