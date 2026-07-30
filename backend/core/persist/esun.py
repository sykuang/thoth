"""玉山銀行 (E.SUN) persist 邏輯.

從 persist.py 拆分而來 (2026-06-17, W persist.py split refactor)。
"""
from __future__ import annotations

from datetime import datetime, timedelta
import re

from backend.core import account_classify, classify
from backend.core.store import BankStore
from backend.core.persist._common import _num_real, _num_to_float, _roc_to_west


def _parse_esun_twd_txn_results(results: list[dict]) -> list[dict]:
    """玉山 FAO01002「存款交易明細查詢」結果 → twd_transactions rows.

    The real page only renders a result grid when rows exist. Empty queries still
    contain query time / hints, so parser must only parse explicit grid/table text.
    """
    out: list[dict] = []
    for result in results or []:
        if not isinstance(result, dict):
            continue
        account_no = result.get("account_no")
        if not account_no:
            selected = result.get("selected_text") or ""
            digits = re.sub(r"\D", "", selected)
            account_no = digits[:13] if len(digits) >= 13 else None
        if not account_no:
            continue
        snapshot = result.get("snapshot") or {}
        candidates: list[str] = []
        grid_text = snapshot.get("gridText") if isinstance(snapshot, dict) else None
        if grid_text:
            candidates.append(str(grid_text))
        if isinstance(snapshot, dict):
            for table in snapshot.get("tables") or []:
                if isinstance(table, dict) and table.get("text"):
                    candidates.append(str(table.get("text")))
            for q in snapshot.get("qryResult") or []:
                if isinstance(q, dict) and q.get("text"):
                    candidates.append(str(q.get("text")))
        for text in candidates:
            parsed_any = False
            # Real FAO01002 result text can render the date+time either split
            # (`2026/03/30\n06:12:14`) or compact (`2026/03/3006:12:14`).
            # Parse row blocks line-by-line first; this preserves empty 提/存 columns
            # better than one giant regex over the whole result shell.
            date_line_pat = re.compile(r"^\*?(20\d{2}/\d{1,2}/\d{1,2})\s*(\d{1,2}:\d{2}:\d{2})\s*$")
            raw_lines = text.splitlines()
            i = 0
            while i < len(raw_lines):
                line = raw_lines[i].strip()
                lm = date_line_pat.match(line)
                if not lm:
                    # alternate copied shape: date on one line, time at start of next line
                    dm = re.match(r"^\*?(20\d{2}/\d{1,2}/\d{1,2})\s*$", line)
                    if dm and i + 1 < len(raw_lines):
                        tm = re.match(r"^\s*(\d{1,2}:\d{2}:\d{2})(?:\s+(.*))?$", raw_lines[i + 1].strip())
                        if tm:
                            date_s, time_s = dm.group(1), tm.group(1)
                            first_tail = tm.group(2) or ""
                            i += 2
                        else:
                            i += 1
                            continue
                    else:
                        i += 1
                        continue
                else:
                    date_s, time_s = lm.group(1), lm.group(2)
                    first_tail = ""
                    i += 1

                block: list[str] = []
                if first_tail:
                    block.append(first_tail)
                while i < len(raw_lines):
                    nxt = raw_lines[i].strip()
                    if date_line_pat.match(nxt) or re.match(r"^\*?20\d{2}/\d{1,2}/\d{1,2}\s*$", nxt):
                        break
                    block.append(raw_lines[i])
                    i += 1

                parts = [ln.strip() for ln in block if ln.strip()]
                if not parts:
                    continue
                first_tokens = parts[0].split()
                if len(first_tokens) >= 3 and any(re.fullmatch(r"-?[\d,]+(?:\.\d+)?", tok) for tok in first_tokens[1:]):
                    # Copied/innerText shape may collapse desc + money columns into one line:
                    # `06:12:14    玉山卡款扣繳    65,714        1    測＊試`.
                    desc = first_tokens[0]
                    nums = [tok for tok in first_tokens[1:] if re.fullmatch(r"-?[\d,]+(?:\.\d+)?", tok)]
                    memos = [tok for tok in first_tokens[1:] if not re.fullmatch(r"-?[\d,]+(?:\.\d+)?", tok)]
                    memos.extend(parts[1:])
                else:
                    desc = parts[0]
                    nums = []
                    memos = []
                    for part in parts[1:]:
                        if re.fullmatch(r"-?[\d,]+(?:\.\d+)?", part):
                            nums.append(part)
                        else:
                            memos.append(part)
                if len(nums) < 2:
                    continue
                balance = _num_to_float(nums[-1])
                amount = _num_to_float(nums[-2])
                expend = income = None
                if amount is not None:
                    income_words = ("利息", "轉入", "存入", "入金", "退費", "退款", "跨行轉")
                    if any(k in desc for k in income_words) and "扣繳" not in desc:
                        income = amount
                    else:
                        expend = amount
                memo = " ".join(memos).strip() or None
                counterparty_bank = None
                counterparty_acct = None
                if memo:
                    bm = re.match(r"(.+?銀行)(.+)", memo)
                    if bm:
                        counterparty_bank = bm.group(1)
                        counterparty_acct = bm.group(2).strip() or None
                    elif "銀行" in memo:
                        counterparty_bank = memo
                    elif re.search(r"\d", memo) and "/" in memo:
                        counterparty_acct = memo
                out.append({
                    "account_no": account_no,
                    "datetime": f"{date_s.replace('/', '-')} {time_s}",
                    "account_date": date_s.replace("/", "-"),
                    "desc": desc,
                    "expend": expend,
                    "income": income,
                    "balance": balance,
                    "counterparty_bank": counterparty_bank,
                    "counterparty_acct": counterparty_acct,
                    "memo": memo,
                })
                parsed_any = True
            if parsed_any:
                continue

            if not any(k in text for k in ("交易日", "交易日期", "摘要", "餘額", "支出", "存入")):
                continue
            # Common textContent shape from table rows:
            # YYYY/MM/DD YYYY/MM/DD 摘要 支出 存入 餘額 備註
            # Empty money columns can collapse into adjacent whitespace; parse with
            # optional expend/income while keeping balance required.
            pat = re.compile(
                r"(?P<txn>20\d{2}/\d{1,2}/\d{1,2})\s+"
                r"(?P<acct>20\d{2}/\d{1,2}/\d{1,2})\s+"
                r"(?P<desc>[^\d\n][^\n]*?)\s+"
                r"(?:(?P<expend>-?[\d,]+(?:\.\d+)?)\s+)?"
                r"(?:(?P<income>-?[\d,]+(?:\.\d+)?)\s+)?"
                r"(?P<balance>-?[\d,]+(?:\.\d+)?)"
                r"(?:\s+(?P<memo>[^\n]+?))?(?=\n|20\d{2}/\d{1,2}/\d{1,2}|$)",
                re.MULTILINE,
            )
            for m in pat.finditer(text):
                desc = (m.group("desc") or "").strip()
                if not desc or any(skip in desc for skip in ("交易日", "帳務日", "查詢時間")):
                    continue
                expend = _num_to_float(m.group("expend")) if m.group("expend") else None
                income = _num_to_float(m.group("income")) if m.group("income") else None
                if income is None and expend is not None and any(k in desc for k in ("利息", "存入", "轉入", "入金", "退費", "退款")):
                    income = expend
                    expend = None
                balance = _num_to_float(m.group("balance"))
                memo = (m.group("memo") or "").strip() or None
                out.append({
                    "account_no": account_no,
                    "datetime": m.group("txn").replace("/", "-"),
                    "account_date": m.group("acct").replace("/", "-"),
                    "desc": desc,
                    "expend": expend,
                    "income": income,
                    "balance": balance,
                    "counterparty_bank": None,
                    "counterparty_acct": None,
                    "memo": memo,
                })
    return out


def persist_esun(data: dict, store: BankStore, rules: list[dict] | None = None) -> dict:
    """玉山 collect → store 入庫。

    映射：
      data.accounts[]    → accounts(UPSERT) + balance_history (TWD 累加)
      data.card_frames[] → daily_metrics (debug dump)
      data._all_endpoints → daily_metrics (endpoint map)
      data.frames[]      → daily_metrics (frame text preview, debug)
    """
    today = datetime.now().strftime("%Y-%m-%d")
    delta: dict = {"bank": "esun"}
    delta["scope"] = "structured"

    # --- accounts ---
    raw_accounts = data.get("accounts") or []
    accts: list[dict] = []
    twd_total = 0.0
    for a in raw_accounts:
        acct_no = a.get("account_no")
        if not acct_no:
            continue
        category = a.get("category") or ""
        currency = a.get("currency") or "TWD"
        balance = float(a.get("balance") or 0)
        # esun classifier 對「臺幣綜存」等非標準 keyword 會 fallback unknown，
        # 這時用 currency 直接判 deposit (TWD) / fx_deposit (其他)
        pt = account_classify.classify_account("esun", {**a, "currency": currency})
        if pt == account_classify.ProductType.UNKNOWN:
            pt = (account_classify.ProductType.DEPOSIT if currency == "TWD"
                  else account_classify.ProductType.FX_DEPOSIT)
        accts.append({
            "account_no": acct_no,
            "currency": currency,
            "branch": None,
            "nickname": category or None,
            "type": category or None,
            "product_type": pt,
            "raw_balance": _num_real(a.get("balance")),
            "raw_balance_date": today,
        })
        if currency == "TWD":
            twd_total += balance
    if accts:
        store.upsert_accounts(accts)
        delta["accounts"] = len(accts)
    if accts:
        store.upsert_balance_history([{
            "snapshotDate": today,
            "twdBalance": twd_total if twd_total > 0 else None,
            "fxBalance": None,
        }])
        delta["balance_days"] = 1
        store.put_daily_metric("balance_latest", {
            "twd": twd_total,
            "n_accounts": len(accts),
            "by_currency": {a.get("currency"): a.get("balance") for a in raw_accounts},
        }, today)

    # --- TWD deposit transactions (FAO01002 存款交易明細查詢) ---
    twd_rows = _parse_esun_twd_txn_results(data.get("twd_txn_results") or [])
    if twd_rows:
        delta["twd_txn_new"] = store.upsert_twd_txns(twd_rows, rules=rules)

    # --- card_summary（信用卡額度/點數/截止日）---
    # Step 2 (2026-06-14): card_summary 是整戶層, 套到 card_transactions 出現的所有卡 (ESun 通常 1 張卡).
    # 民國年 '115/06/29' → '2026-06-29' 用 _roc_to_west.
    card_summary = data.get("card_summary") or {}
    if card_summary:
        store.put_daily_metric("esun_card_summary", card_summary, today)
        delta["card_summary"] = card_summary

    # --- card_quota（信用卡額度查詢頁，2026-06-18 新增 B 路線）---
    card_quota_raw = data.get("card_quota") or {}
    if card_quota_raw:
        store.put_daily_metric("esun_card_quota", card_quota_raw, today)
        delta["card_quota"] = {
            k: v for k, v in card_quota_raw.items()
            if k != "raw_text_sample"  # debug 樣本不入 delta log
        }

    # ESun cards UPSERT (從 card_transactions 拿卡號, 套整戶層 limit/due)
    esun_limit = _num_to_float(card_summary.get("credit_limit_twd"))
    esun_due = _roc_to_west(card_summary.get("payment_due_date_roc"))

    # 2026-06-18 升級 (B 路線)：used_credit 改為直接抓「信用卡額度查詢」頁原生欄位
    # (data.card_quota.used_credit_twd)，因為原本 sum 已入帳會少算未入帳 + 上期未繳，
    # 顯示 NT$2,085 而使用者實際 used=-807 (溢繳)。raw 欄位 vs sum 兩層 fallback:
    #   1) card_quota.used_credit_twd  ← 玉山原生顯示，最誠實 (可能 0 或負數)
    #   2) sum(card_transactions 已入帳)  ← 舊路徑，最後 fallback (顯示不足但好過 None)
    #
    # ⚠️ used_credit 可能是 0 (剛繳完) 或負數 (溢繳)，所以判 "是否抓到 quota" 用
    # `quota_used is not None` 而非 truthy check，否則 0 / -807 會誤走 fallback。
    card_quota = data.get("card_quota") or {}
    quota_used_raw = card_quota.get("used_credit_twd")
    quota_used: float | None = _num_to_float(quota_used_raw) if quota_used_raw is not None else None
    # 如果額度查詢頁有抓到「歸戶信用額度」，優先用它（比帳單頁 card_summary 新）
    quota_limit = _num_to_float(card_quota.get("credit_limit_twd"))
    if quota_limit:
        esun_limit = quota_limit

    # ⚠️ 舊邏輯保留為 fallback：used_credit 從 card_bills + card_transactions 推算
    # （quota 完全抓不到時用）。
    esun_used_total = 0.0
    for t in data.get("card_transactions") or []:
        if t.get("status") == "已入帳":
            amt = _num_to_float(t.get("billed_amount"))
            if amt:
                esun_used_total += amt
    sum_used: float | None = esun_used_total if esun_used_total > 0 else None

    # 優先順序：raw quota (含 0/負數) > sum 已入帳
    esun_used: float | None = quota_used if quota_used is not None else sum_used

    # stmt_close_date 推算: due_date - 30 天 (ESun 約莫 14-30 天繳款期間)
    esun_stmt: str | None = None
    if esun_due:
        try:
            due_dt = datetime.strptime(esun_due, "%Y-%m-%d")
            stmt_dt = due_dt - timedelta(days=30)
            esun_stmt = stmt_dt.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            esun_stmt = None

    # 2026-06-22 (audit findings): card_bills[0] (最新月帳單) 有 due_amount + paid_amount,
    # 對應 bill_due_amount / last_payment_amount. 玉山 raw 無 last_payment_date
    # (card_events 端用 deposit「信用卡款」rule txn 反推).
    esun_bill_due: float | None = None
    esun_last_pay_amt: float | None = None
    esun_last_pay_date: str | None = None
    card_bills_list = data.get("card_bills") or []
    if card_bills_list and isinstance(card_bills_list[0], dict):
        latest_bill = card_bills_list[0]
        esun_bill_due = _num_to_float(latest_bill.get("due_amount"))
        esun_last_pay_amt = _num_to_float(latest_bill.get("paid_amount"))
        # 2026-06-22 v2: 不再 sentinel paid=0 → None. 0 是合法值.
        # card_events 靠 last_payment_date is None gate 通知.

    # 2026-06-23 v3 (使用者 local crawl + parser 驗證):
    # 玉山「信用卡繳款明細查詢」(FCM01005 widget) parser 抓到的 records 是真實「上次繳款日」.
    # records 排序新→舊, records[0] = 最近一筆繳款.
    # 覆寫 card_bills[0] 的 (paid_amount, None) → 改用 (records[0].paid_amount, records[0].post_date)
    # 既然有真實日期, last_payment_date 也補上.
    pay_history = data.get("card_pay_history") or {}
    pay_records = pay_history.get("records") if isinstance(pay_history, dict) else None
    if pay_records:
        latest_pay = pay_records[0]  # 新→舊排序, [0] 是最新
        amt = _num_to_float(latest_pay.get("paid_amount"))
        date = latest_pay.get("post_date")
        if amt is not None and date:
            esun_last_pay_amt = amt
            esun_last_pay_date = date

    # 從 card_transactions 收集 unique card_no
    esun_seen_cards: dict[str, dict] = {}
    for t in data.get("card_transactions") or []:
        last4 = t.get("card_last4")
        full_masked = t.get("card_no") or ""  # '9064-XXXX-XXXX-7032'
        if not last4 or last4 in esun_seen_cards:
            continue
        esun_seen_cards[last4] = {
            "number": f"****{last4}",
            "name": f"玉山卡 ****{last4}",
            "association": None,
            "type": "credit",
            "is_cube": False,
            "card_no_full_masked": full_masked,
            "credit_limit": esun_limit,
            "used_credit": esun_used,  # 已入帳消費總和
            "statement_close_date": esun_stmt,  # due_date - 30 天推算
            "payment_due_date": esun_due,
            # 2026-06-22 (audit): bill_due + last_payment_amount 整戶層套 (玉山 raw 無 per-card)
            "bill_due_amount": esun_bill_due,
            "last_payment_amount": esun_last_pay_amt,
            # 2026-06-23 v3: last_payment_date 從 FCM01005 records[0].post_date 拿
            "last_payment_date": esun_last_pay_date,
        }
    if esun_seen_cards:
        store.upsert_cards(list(esun_seen_cards.values()))

    # --- card_bills（帳單月份列表）---
    card_bills = data.get("card_bills") or []
    if card_bills:
        store.put_daily_metric("esun_card_bills", {"bills": card_bills, "count": len(card_bills)}, today)
        delta["card_bills"] = len(card_bills)

    # --- card_transactions（消費明細）— 入正規 schema ---
    card_txns = data.get("card_transactions") or []

    # 2026-06-20 一次性 cleanup: 砍舊格式 card_no='5242-XXXX-XXXX-XXXX' row.
    # bug 期間 (2026-06-13 ~ 2026-06-20) 寫進去的 row 用 raw masked full
    # 對不上 cards.card_no='****XXXX', 砍掉讓本次 sync 寫的新 row 唯一 source of truth.
    # idempotent — 已修 fmt 的 DB 無舊格式 row, DELETE 0 row 無害.
    if card_txns:
        store.purge_legacy_masked_card_no_rows()

    pending_txns = []
    billed_txns = []
    for t in card_txns:
        # 共用：card_no（含 masked）、消費日、商店、幣別、原幣金額、入帳金額
        # 2026-06-13 修：純台幣不寫 consume_currency/consume_amount（對齊 cathay norm 規則）
        consume_cur = t.get("consume_currency") or ""
        billed_cur = t.get("billed_currency") or "TWD"
        is_foreign = consume_cur and consume_cur not in ("TWD", billed_cur)
        desc = t.get("merchant")
        amt = int(t.get("billed_amount") or 0)
        # 2026-06-20 (root cause: 帳戶 tab 玉山卡顯示「使用額度 0」):
        # cards.card_no 是 '****7032' (line 140), 但這裡若寫 raw '9064-XXXX-XXXX-7032'
        # → bill_summary SQL `WHERE card_no = ?` join 不到 → bill_due_amount=0 假象.
        # 統一寫 '****{last4}' 跟 cards 同格式 (跟 HSBC/CTBC/Taishin 同 norm 規則).
        # 沒 last4 退而求其次用 raw, 至少不會 KeyError.
        card_last4 = t.get("card_last4") or ""
        card_no_normalized = f"****{card_last4}" if card_last4 else (t.get("card_no") or "")
        row = {
            "card_no": card_no_normalized,
            "date": (t.get("consume_date") or "").replace("/", "-"),  # 2026/06/08 → 2026-06-08
            "desc": desc,
            "amount": amt,  # 入帳金額（TWD/結算幣）
            "currency": billed_cur,
            "consume_currency": consume_cur if is_foreign else None,
            "consume_amount": t.get("consume_amount") if is_foreign else None,
            "txn_type": classify.classify_by_desc_and_sign(desc or "", amt),
        }
        if t.get("status") == "未入帳":
            pending_txns.append(row)
        else:
            # 已入帳 → 帳單月份待 card_bills 對齊
            # 2026-06-20: 玉山「信用卡消費明細查詢」列表頁的物理限制 — 該頁
            # 只給「消費日期/商店/消費幣別+金額/繳款幣別+金額/卡號/狀態」共 6 欄，
            # **沒有獨立的「請款日/入帳日」欄位**（頁面註腳寫「上列為商店已請款之明細」，
            # 商店請款≈入帳即將發生，但具體入帳日要等月結帳單才有）。
            # 不再寫 post_date = consume_date (顯示誠實) — 留 NULL 讓 store 層
            # fallback (store.py:571 `post_date = t.get("post_date") or t.get("date")`)。
            # 未來想抓真實入帳日需另抓「月結帳單」(信用卡帳單 > 明細) 那邊的 posting date。
            row["bill_date"] = None
            billed_txns.append(row)
    if billed_txns:
        n = store.upsert_card_billed(billed_txns, rules=rules)
        delta["card_billed_new"] = n
    # collector 必須明示兩個期間皆成功提交且看到結果 frame；只看到 [] 不足以證明零筆。
    n = store.refresh_card_pending(
        "unbilled", pending_txns, rules=rules,
        fetch_ok=data.get("card_transactions_ok") is True)
    delta["card_unbilled"] = n
    if card_txns:
        # 同時保留原始 JSON 在 daily_metrics 做 debug
        store.put_daily_metric("esun_card_transactions",
                                {"transactions": card_txns, "count": len(card_txns)}, today)
        delta["card_transactions"] = len(card_txns)

    # cards 表 UPSERT 已在上面用 esun_seen_cards (從 card_transactions 抽 last4)
    # 處理. 舊路徑 (用全 masked card_no='9064-XXXX-XXXX-7032' 當 number) 2026-06-14 拔除,
    # 因為會跟新 path 的 number='****7032' 撞成 2 筆殘留.

    # --- card_txn_frames + card_txn_nav_probe (debug) ---
    card_txn_frames = data.get("card_txn_frames") or []
    if card_txn_frames:
        store.put_daily_metric("esun_card_txn_frames", {
            "count": len(card_txn_frames),
            "urls": [cf.get("url", "")[:200] for cf in card_txn_frames],
            "text_preview": [cf.get("text_preview", "")[:1000] for cf in card_txn_frames],
        }, today)
    card_txn_nav_probe = data.get("card_txn_nav_probe")
    if card_txn_nav_probe:
        store.put_daily_metric("esun_card_txn_nav_probe", card_txn_nav_probe, today)

    # --- card_frames (信用卡入口導航結果，debug) ---
    card_frames = data.get("card_frames") or []
    if card_frames:
        store.put_daily_metric("esun_card_frames", {
            "count": len(card_frames),
            "urls": [cf.get("url", "")[:200] for cf in card_frames],
            "text_preview": [cf.get("text_preview", "")[:500] for cf in card_frames],
        }, today)

    # --- card_nav_probe (信用卡 navigate 探勘結果，debug)---
    card_nav_probe = data.get("card_nav_probe")
    if card_nav_probe:
        store.put_daily_metric("esun_card_nav_probe", card_nav_probe, today)

    # --- card_quota_frames + card_quota_nav_probe (debug, 2026-06-18) ---
    card_quota_frames = data.get("card_quota_frames") or []
    if card_quota_frames:
        store.put_daily_metric("esun_card_quota_frames", {
            "count": len(card_quota_frames),
            "urls": [cf.get("url", "")[:200] for cf in card_quota_frames],
            "text_preview": [cf.get("text_preview", "")[:2000] for cf in card_quota_frames],
        }, today)
    card_quota_nav_probe = data.get("card_quota_nav_probe")
    if card_quota_nav_probe:
        store.put_daily_metric("esun_card_quota_nav_probe", card_quota_nav_probe, today)

    # --- card_pay_frames + card_pay_nav_probe (2026-06-22 信用卡繳款明細查詢, raw dump) ---
    # 等明早 sync 後從 PG 撈出來看真實 shape, 再決定 parser 邏輯.
    card_pay_frames = data.get("card_pay_frames") or []
    if card_pay_frames:
        store.put_daily_metric("esun_card_pay_frames", {
            "count": len(card_pay_frames),
            "urls": [cf.get("url", "")[:200] for cf in card_pay_frames],
            "text_preview": [cf.get("text_preview", "")[:2000] for cf in card_pay_frames],
        }, today)
    card_pay_nav_probe = data.get("card_pay_nav_probe")
    if card_pay_nav_probe:
        store.put_daily_metric("esun_card_pay_nav_probe", card_pay_nav_probe, today)
    card_pay_history = data.get("card_pay_history") or {}
    if card_pay_history:
        store.put_daily_metric("esun_card_pay_history", card_pay_history, today)

    # --- all_pages (debug: 是否有開新 tab) ---
    all_pages = data.get("all_pages")
    if all_pages:
        store.put_daily_metric("esun_all_pages", {"pages": all_pages}, today)

    # --- endpoint 地圖 ---
    endpoints = data.get("_all_endpoints") or []
    if endpoints:
        store.put_daily_metric("esun_endpoints", {"endpoints": endpoints}, today)

    # --- frames preview（debug，第一次入庫用，後續可移除）---
    frames = data.get("frames") or []
    if frames:
        store.put_daily_metric("esun_frames_dump", {
            "frame_urls": [f.get("url", "")[:200] for f in frames],
            "main_text_preview": (data.get("main_text") or "")[:1000],
        }, today)

    delta.setdefault("balance_days", 0)
    delta.setdefault("accounts", 0)
    delta.setdefault("twd_txn_new", 0)
    delta.setdefault("card_billed_new", 0)
    delta.setdefault("card_unbilled", 0)
    delta.setdefault("card_current", 0)

    store.log_sync(delta)
    return delta
