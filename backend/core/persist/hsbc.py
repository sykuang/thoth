"""HSBC 信用卡 persist 邏輯.

從 persist.py 拆分而來 (2026-06-17, W persist.py split refactor)。
"""
from __future__ import annotations

from datetime import datetime

from backend.core import classify
from backend.core.persist._common import _num_to_float
from backend.core.store import BankStore


# 月份英文簡寫 → 數字
_HSBC_MONTH_MAP = {
    "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05", "Jun": "06",
    "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
}



def _hsbc_amt(s):
    """HSBC 金額字串 '21 TWD' / '2,754.53 CNY' / '12,781 TWD' → (數值, 幣別)。
    回 (float|int, currency_str)。空/異常 → (None, None)。"""
    if s is None:
        return None, None
    t = str(s).strip()
    if not t or t in ("-",):
        return None, None
    parts = t.rsplit(" ", 1)
    num_str = parts[0].replace(",", "").strip()
    cur = parts[1].strip() if len(parts) == 2 else None
    try:
        val = float(num_str)
        # 無小數則轉 int（台幣多無小數；外幣保留 float）
        if val == int(val):
            val = int(val)
        return val, cur
    except (ValueError, TypeError):
        return None, cur

def _hsbc_date(s):
    """HSBC 日期 '2026-05-18T00:00' → 'YYYY-MM-DD'。
    placeholder '0002-11-30...'（未入帳）→ None。"""
    if not s:
        return None
    t = str(s).strip()
    if t.startswith(("0002-", "0001-")):
        return None  # 未入帳 placeholder
    return t.split("T", 1)[0]

def _hsbc_dmy_text_to_iso(s: str | None) -> str | None:
    """HSBC card_detail.details[] 文字日期 '18 May 2026' → 'YYYY-MM-DD'。

    用於 'Last Statement Date' / 'Payment Due Date' 等 details key=value 的 value。
    格式: 'DD MMM YYYY' (例: '18 May 2026', '05 Jun 2026').
    """
    if not s:
        return None
    t = str(s).strip()
    parts = t.split()
    if len(parts) != 3:
        return None
    dd, mmm, yyyy = parts
    mm = _HSBC_MONTH_MAP.get(mmm)
    if not mm or not yyyy.isdigit() or not dd.isdigit():
        return None
    return f"{yyyy}-{mm}-{dd.zfill(2)}"

def _hsbc_card_txn(t: dict) -> dict:
    """HSBC 一筆交易 → store.upsert_card_billed 的欄位格式。

    HSBC 欄位：description / amount('21 TWD') / isPositive(true=消費,false=還款/退款)
      / postedDate(入帳日) / transactionDate(消費日)
      / isForeign / foreignAmount('18.98 CNY') / ntdAmount('88 TWD')
    鐵律：消費日(transactionDate) vs 入帳日(postedDate) 分存；外幣保留小數。
    """
    ntd_val, _ = _hsbc_amt(t.get("ntdAmount") or t.get("amount"))
    fx_val, fx_cur = _hsbc_amt(t.get("foreignAmount"))
    is_foreign = bool(t.get("isForeign"))
    is_positive = t.get("isPositive", True)
    desc = (t.get("description") or "").strip()
    # 還款/退款（isPositive=false）金額記為負，消費為正
    signed = ntd_val
    if ntd_val is not None and not is_positive:
        signed = -abs(ntd_val)
    return {
        "card_no": None,            # 由外層帶卡號
        "bill_date": None,          # HSBC 明細 API 未直接給帳單日（卡詳情另有）
        "currency": "TWD",          # 入帳幣別
        "date": _hsbc_date(t.get("transactionDate")),   # 消費日
        "post_date": _hsbc_date(t.get("postedDate")),   # 入帳日（未入帳→None，store fallback）
        "desc": desc,
        "amount": signed,           # 台幣入帳金額（還款為負）
        "consume_country": None,
        "consume_currency": fx_cur if is_foreign else "TWD",   # 原始消費幣別
        "consume_amount": fx_val if is_foreign else None,      # 原始外幣金額（保留小數）
        "txn_type": classify.classify_hsbc(is_positive, desc, signed),
    }

def _dmy_to_iso(s: str | None) -> str | None:
    """'05-06-2026' → '2026-06-05'. HSBC 用 (DD-MM-YYYY)."""
    if not s:
        return None
    try:
        parts = str(s).strip().split("-")
        if len(parts) != 3:
            return None
        d, mo, y = int(parts[0]), int(parts[1]), int(parts[2])
        # 防呆: y 如果只 2 位 → 拒
        if y < 1000:
            return None
        return f"{y:04d}-{mo:02d}-{d:02d}"
    except Exception:
        return None

def persist_hsbc(data: dict, store: BankStore, rules: list[dict] | None = None) -> dict:
    """HSBC collect() 結構 → store 表增量。

    映射：
      cards[]                    → cards(UPSERT) + card_summary daily_metric
      card_detail[tail].posted   → card_billed_txns(append-only，帶卡號)
      card_detail[tail].unposted → card_pending_txns(refresh 'unbilled'，帶卡號)
      card_detail[tail].detail   → daily_metric(額度/繳款/紅利)
    HSBC 是信用卡 only，無台幣存款帳戶。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    delta: dict = {}

    # --- 卡片清單（UPSERT cards）---
    # Step 2 (2026-06-14): 接 per-card outstandingBalance / paymentDueDate /
    # statementDate / cardStatusDisplay (active). HSBC 提供 per-card 完整資料.
    # cardStatusDisplay 已知值: 'ACTIVATED' = 有效; 其他 (NOT_ACTIVATED/CLOSED 等) = 失效.
    # 2026-06-14 升級：先從 card_detail[*].detail.details[] (key/value list) 抽
    # per-card creditLimit + lastStatementDate（卡片清單 endpoint 沒給）。
    details_map: dict[str, dict] = {}  # {tail: {credit_limit, last_stmt_date}}
    for tail, entry in (data.get("card_detail") or {}).items():
        det = entry.get("detail") or {}
        details_list = det.get("details") or []
        info: dict = {}
        for kv in details_list:
            if not isinstance(kv, dict):
                continue
            k = (kv.get("key") or "").strip()
            v = (kv.get("value") or "").strip()
            if not k or not v:
                continue
            if k == "Credit Limit":
                # "1,500,000 TWD" → 1500000.0
                info["credit_limit"] = _num_to_float(v.split()[0] if v else "")
            elif k == "Last Statement Date":
                # "18 May 2026" → "2026-05-18"
                info["last_stmt_date"] = _hsbc_dmy_text_to_iso(v)
            elif k == "Last Statement Amount":
                # "71,032 TWD" → 71032.0
                # HSBC 官方「本期應繳」直給 (不必靠 card_billed_txns latest bill_date sum;
                # HSBC 明細表 bill_date 永遠 NULL 那條 derive 路徑會把整 12 月歷史消費當本期帳單)
                info["bill_due_amount"] = _num_to_float(v.split()[0] if v else "")
            elif k == "Last Payment Amount":
                # "622 TWD" → 622.0 (HSBC 官方「最近繳款金額」)
                info["last_payment_amount"] = _num_to_float(v.split()[0] if v else "")
            elif k == "Last Payment Date":
                # "11 Jun 2026" → "2026-06-11" (HSBC 官方「最近繳款日」)
                info["last_payment_date"] = _hsbc_dmy_text_to_iso(v)
        if info:
            details_map[tail] = info

    cards = data.get("cards") or []
    card_rows = []
    for c in cards:
        masked = c.get("maskedCardNumber", "")
        tail = masked[-4:] if masked else ""
        status_display = (c.get("cardStatusDisplay") or "").upper()
        is_active = status_display == "ACTIVATED"
        per_card_info = details_map.get(tail) or {}
        card_rows.append({
            "number": masked,                       # 用 masked 當卡號（HSBC 不給明碼）
            "name": c.get("name"),
            "association": None,
            "type": c.get("cardType"),
            "is_cube": False,
            # Step 2: per-card 信用卡明細 (HSBC API 不直接給 limit; outstandingBalance=已欠)
            # 2026-06-14：credit_limit / statement_close_date 從 card_detail.details[] 補
            # 2026-06-20：bill_due_amount / last_payment_amount / last_payment_date 也從
            #   card_detail.details[] 補 (HSBC 官方直給, 跳過 db_facade SQL derive 路徑.
            #   原 derive 在 HSBC 表 bill_date 全 NULL 時, 把整 12 月歷史消費 SUM 成「本期應繳」)
            "credit_limit": per_card_info.get("credit_limit"),
            "used_credit": _num_to_float(c.get("outstandingBalance")),
            "statement_close_date": (per_card_info.get("last_stmt_date")
                                      or _dmy_to_iso(c.get("statementDate"))),
            "payment_due_date": _dmy_to_iso(c.get("paymentDueDate")),
            "bill_due_amount": per_card_info.get("bill_due_amount"),
            "last_payment_amount": per_card_info.get("last_payment_amount"),
            "last_payment_date": per_card_info.get("last_payment_date"),
            "active": is_active,
        })
    if card_rows:
        store.upsert_cards(card_rows)

    # --- 逐卡明細 ---
    billed_new = 0
    unbilled_rows_all = []
    for tail, entry in (data.get("card_detail") or {}).items():
        masked = entry.get("masked", "")
        # 已出帳（append-only）
        posted = entry.get("posted") or []
        rows = []
        for t in posted:
            r = _hsbc_card_txn(t)
            r["card_no"] = masked
            rows.append(r)
        billed_new += store.upsert_card_billed(rows, rules=rules)
        # 未出帳（refresh-by-scope，彙整所有卡一起 refresh）
        for t in (entry.get("unposted") or []):
            r = _hsbc_card_txn(t)
            # ⚠️ 鐵律 (2026-06-14)：amount/currency = 入帳金額+TWD，外幣資訊另存 consume_*
            # 不要把 r["consume_currency"] (EUR) 塞進 currency 欄，會害 UI 以為「沒台幣金額」
            unbilled_rows_all.append({
                "card_no": masked, "date": r["date"], "post_date": r.get("post_date"),
                "desc": r["desc"],
                "amount": r["amount"], "currency": r["currency"],     # 主金額 = TWD
                "consume_country": r.get("consume_country"),
                "consume_currency": r["consume_currency"],            # 原始外幣別
                "consume_amount": r["consume_amount"],                # 原始外幣金額
                "txn_type": r.get("txn_type"),
            })
        # 卡片詳情（額度/繳款/紅利）存每日快照
        det = entry.get("detail")
        if isinstance(det, dict):
            store.put_daily_metric(f"card_detail_{tail}", det, today)

    delta["card_billed_new"] = billed_new
    delta["card_unbilled"] = store.refresh_card_pending("unbilled", unbilled_rows_all, rules=rules)
    delta["card_current"] = 0

    # 卡片彙總快照（應繳/額度）
    if cards:
        summary = [{
            "name": c.get("name"), "masked": c.get("maskedCardNumber"),
            "outstanding": c.get("outstandingBalance"),
            "min_payment": c.get("minimumPayableAmount"),
            "due_date": c.get("paymentDueDate"),
        } for c in cards]
        store.put_daily_metric("card_summary", summary, today)
    store.log_sync(delta)
    return delta
