"""HSBC 信用卡 persist 邏輯.

從 persist.py 拆分而來 (2026-06-17, W persist.py split refactor)。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from backend.core import classify
from backend.core.base import validate_history_coverage
from backend.core.persist._common import _num_to_float
from backend.core.store import BankStore


# 月份英文簡寫 → 數字
_HSBC_MONTH_MAP = {
    "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05", "Jun": "06",
    "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
}

_HSBC_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?$"
)
_HSBC_MONEY_RE = re.compile(
    r"^([+]?(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d{1,2})?) ([A-Z]{3})$"
)
_HSBC_SCALAR_RE = re.compile(
    r"^[+-]?(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d+)?$"
)
_HSBC_MAX_MONEY = Decimal("100000000")


def _hsbc_history_date(value) -> date:
    if not isinstance(value, str) or _HSBC_DATETIME_RE.fullmatch(value) is None:
        raise ValueError("invalid HSBC history transaction date")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        raise ValueError("invalid HSBC history transaction date") from None


def _hsbc_history_floor(end: date) -> date:
    try:
        return end.replace(year=end.year - 1) + timedelta(days=1)
    except ValueError:
        return end.replace(year=end.year - 1, day=28) + timedelta(days=1)


def _bounded_card_scalar(value, *, allow_negative: bool) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    if isinstance(value, bool) or _HSBC_SCALAR_RE.fullmatch(text) is None:
        return False
    try:
        amount = Decimal(text.replace(",", ""))
    except (InvalidOperation, ValueError, AttributeError):
        return False
    return (
        amount.is_finite()
        and abs(amount) <= _HSBC_MAX_MONEY
        and amount == amount.to_integral_value()
        and (allow_negative or amount >= 0)
    )


def _validate_hsbc_txn(row: object, *, end: date, start: date | None = None) -> None:
    if not isinstance(row, dict):
        raise ValueError("invalid HSBC history transaction")
    description = row.get("description")
    transaction = _hsbc_history_date(row.get("transactionDate"))
    posted_raw = row.get("postedDate")
    if start is None and posted_raw != "0002-11-30T00:00":
        raise ValueError("invalid HSBC history transaction")
    posted = None
    if posted_raw not in (None, "", "0002-11-30T00:00"):
        posted = _hsbc_history_date(posted_raw)
    amount, currency = _hsbc_amt(row.get("ntdAmount") or row.get("amount"))
    is_foreign = row.get("isForeign")
    if (
        type(amount) is not int
        or not 0 <= amount <= 100_000_000
        or currency != "TWD"
        or type(row.get("isPositive")) is not bool
        or type(is_foreign) is not bool
        or not isinstance(description, str)
        or not 0 < len(description.strip()) <= 512
        or transaction > end
        or (posted is not None and (transaction > posted or posted > end))
        or (start is not None and (posted is None or not start <= posted <= end))
        or (not is_foreign and row.get("foreignAmount") not in (None, "", "-"))
    ):
        raise ValueError("invalid HSBC history transaction")
    if is_foreign:
        foreign_amount, foreign_currency = _hsbc_amt(row.get("foreignAmount"))
        if (
            isinstance(foreign_amount, bool)
            or not isinstance(foreign_amount, (int, float))
            or foreign_currency in (None, "TWD")
        ):
            raise ValueError("invalid HSBC history transaction")


def _validate_hsbc_history(data: dict, store: BankStore) -> None:
    coverage = data.get("history_coverage")
    if coverage is None:
        raise ValueError("invalid HSBC history coverage")
    if not isinstance(coverage, dict):
        raise ValueError("invalid HSBC history coverage")
    mode = coverage.get("mode")
    if mode not in {"full", "incremental"}:
        raise ValueError("invalid HSBC history coverage")
    validate_history_coverage(
        coverage,
        expected_mode=mode,
        expected_domains=frozenset({"card_billed_transactions"}),
    )
    domains = coverage["domains"]
    if len(domains) != 1 or domains[0].get("domain") != "card_billed_transactions":
        raise ValueError("invalid HSBC history coverage")
    domain = domains[0]

    cards = data.get("cards", [])
    details = data.get("card_detail", {})
    if not isinstance(cards, list) or not isinstance(details, dict):
        raise ValueError("invalid HSBC history inventory")
    existing_cursors = store.latest_card_transaction_dates()
    identities = []
    card_ids: set[str] = set()
    inventory: dict[str, str] = {}
    for card in cards:
        identity = card.get("maskedCardNumber") if isinstance(card, dict) else None
        card_id = card.get("id") if isinstance(card, dict) else None
        if (
            not isinstance(identity, str)
            or re.fullmatch(r"[0-9]{4}-\*{4}-\*{4}-[0-9]{4}", identity) is None
            or not isinstance(card_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", card_id) is None
            or card.get("cardStatusDisplay") not in {
                "ACTIVATED", "NOT_ACTIVATED", "CLOSED",
            }
            or identity in inventory
            or card_id in card_ids
            or not _bounded_card_scalar(
                card.get("outstandingBalance"), allow_negative=True,
            )
            or not _bounded_card_scalar(
                card.get("minimumPayableAmount"), allow_negative=False,
            )
            or any(
                card.get(key) is not None and _dmy_to_iso(card.get(key)) is None
                for key in ("paymentDueDate", "statementDate")
            )
        ):
            raise ValueError("invalid HSBC history inventory")
        identities.append(identity)
        inventory[identity] = card_id
        card_ids.add(card_id)

    expected = domain.get("expected")
    windows = domain.get("windows")
    if not isinstance(expected, list) or not isinstance(windows, list):
        raise ValueError("invalid HSBC history coverage")
    if not identities:
        if details or expected or windows or "empty_window" not in domain:
            raise ValueError("invalid HSBC history inventory")
        empty = domain["empty_window"]
        try:
            empty_start = date.fromisoformat(empty["start"])
            empty_end = date.fromisoformat(empty["end"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("invalid HSBC history empty window") from None
        if (
            set(empty) != {"start", "end", "status", "pages"}
            or empty.get("status") != "explicit_empty"
            or empty.get("pages") != 1
            or empty_start > empty_end
            or empty_end > date.today()
            or any(cursor > empty_end for cursor in existing_cursors.values())
            or (mode == "full" and empty_start != _hsbc_history_floor(empty_end))
        ):
            raise ValueError("invalid HSBC history empty window")
        return

    expected_by_identity = {
        item.get("identity"): item for item in expected if isinstance(item, dict)
    }
    windows_by_identity = {
        item.get("identity"): item for item in windows if isinstance(item, dict)
    }
    identity_set = set(identities)
    if (
        len(expected_by_identity) != len(expected)
        or len(windows_by_identity) != len(windows)
        or set(details) != identity_set
        or set(expected_by_identity) != identity_set
        or set(windows_by_identity) != identity_set
    ):
        raise ValueError("invalid HSBC history identity binding")

    receipt_fields = {"identity", "start", "end", "status", "pages", "rows"}
    for identity in identities:
        entry = details[identity]
        expected_item = expected_by_identity[identity]
        window = windows_by_identity[identity]
        if (
            not isinstance(entry, dict)
            or entry.get("masked") != identity
            or entry.get("card_id") != inventory[identity]
        ):
            raise ValueError("invalid HSBC history identity binding")
        receipt = entry.get("posted_receipt")
        rows = entry.get("posted")
        if (
            not isinstance(receipt, dict)
            or set(receipt) != receipt_fields
            or receipt != window
            or receipt.get("start") != expected_item.get("start")
            or receipt.get("end") != expected_item.get("end")
            or not isinstance(rows, list)
            or type(receipt.get("rows")) is not int
            or receipt["rows"] != len(rows)
            or (receipt.get("status") == "complete") != bool(rows)
        ):
            raise ValueError("invalid HSBC history receipt")
        start = date.fromisoformat(receipt["start"])
        end = date.fromisoformat(receipt["end"])
        if (
            type(receipt.get("pages")) is not int
            or not 1 <= receipt["pages"] <= 500
            or end > date.today()
            or (mode == "full" and start != _hsbc_history_floor(end))
        ):
            raise ValueError("invalid HSBC history receipt")
        cursor = existing_cursors.get(identity)
        if isinstance(cursor, date) and cursor > end:
            raise ValueError("invalid HSBC history cursor")
        if mode == "incremental":
            expected_start = _hsbc_history_floor(end)
            if isinstance(cursor, date):
                expected_start = max(expected_start, cursor - timedelta(days=7))
            if start != expected_start:
                raise ValueError("invalid HSBC history incremental start")
        for row in rows:
            _validate_hsbc_txn(row, start=start, end=end)
        unposted = entry.get("unposted")
        if not isinstance(unposted, list) or type(entry.get("unposted_ok")) is not bool:
            raise ValueError("invalid HSBC history transaction")
        for row in unposted:
            _validate_hsbc_txn(row, end=end)
        detail = entry.get("detail")
        if detail is not None:
            detail_rows = detail.get("details") if isinstance(detail, dict) else None
            if not isinstance(detail_rows, list) or any(
                not isinstance(item, dict)
                or not isinstance(item.get("key"), str)
                or not isinstance(item.get("value"), str)
                or len(item["key"]) > 128
                or len(item["value"]) > 512
                for item in detail_rows
            ):
                raise ValueError("invalid HSBC card detail")
            keys = [item["key"].strip() for item in detail_rows]
            if len(set(keys)) != len(keys):
                raise ValueError("invalid HSBC card detail")
            for item in detail_rows:
                key = item["key"].strip()
                value = item["value"].strip()
                if key in {
                    "Credit Limit", "Last Statement Amount", "Last Payment Amount",
                }:
                    amount, currency = _hsbc_amt(value)
                    if type(amount) is not int or currency != "TWD":
                        raise ValueError("invalid HSBC card detail")
                elif key in {
                    "Last Statement Date", "Last Payment Date", "Payment Due Date",
                } and _hsbc_dmy_text_to_iso(value) is None:
                    raise ValueError("invalid HSBC card detail")


def _hsbc_amt(s):
    """Parse one bounded HSBC money scalar without binary-float validation."""
    if s is None:
        return None, None
    text = str(s).strip()
    if not text or text == "-":
        return None, None
    match = _HSBC_MONEY_RE.fullmatch(text)
    currency = match.group(2) if match else None
    try:
        amount = Decimal(match.group(1).replace(",", "")) if match else None
    except InvalidOperation:
        amount = None
    if amount is None or not amount.is_finite() or not 0 <= amount <= _HSBC_MAX_MONEY:
        return None, currency
    if amount == amount.to_integral_value():
        return int(amount), currency
    return float(amount), currency

def _hsbc_date(s):
    """HSBC 日期 '2026-05-18T00:00' → 'YYYY-MM-DD'。
    placeholder '0002-11-30...'（未入帳）→ None。"""
    if not s:
        return None
    t = str(s).strip()
    if t == "0002-11-30T00:00":
        return None  # HSBC 未入帳唯一已知 placeholder
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
    try:
        return date(int(yyyy), int(mm), int(dd)).isoformat()
    except ValueError:
        return None

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
        "post_date": _hsbc_date(t.get("postedDate")),   # 入帳日（未入帳→None）
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
        return date(y, mo, d).isoformat()
    except Exception:
        return None

def persist_hsbc(
    data: dict,
    store: BankStore,
    rules: list[dict] | None = None,
    *,
    commit: bool = True,
) -> dict:
    """HSBC collect() 結構 → store 表增量。

    映射：
      cards[]                    → cards(UPSERT) + card_summary daily_metric
      card_detail[tail].posted   → card_billed_txns(append-only，帶卡號)
      card_detail[tail].unposted → card_pending_txns(refresh 'unbilled'，帶卡號)
      card_detail[tail].detail   → daily_metric(額度/繳款/紅利)
    HSBC 是信用卡 only，無台幣存款帳戶。
    """
    _validate_hsbc_history(data, store)
    today = datetime.now().strftime("%Y-%m-%d")
    delta: dict = {}

    # --- 卡片清單（UPSERT cards）---
    # Step 2 (2026-06-14): 接 per-card outstandingBalance / paymentDueDate /
    # statementDate / cardStatusDisplay (active). HSBC 提供 per-card 完整資料.
    # cardStatusDisplay 已知值: 'ACTIVATED' = 有效; 其他 (NOT_ACTIVATED/CLOSED 等) = 失效.
    # 2026-06-14 升級：先從 card_detail[*].detail.details[] (key/value list) 抽
    # per-card creditLimit + lastStatementDate（卡片清單 endpoint 沒給）。
    details_map: dict[str, dict] = {}
    for detail_key, entry in (data.get("card_detail") or {}).items():
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
            details_map[entry.get("masked") or detail_key] = info

    cards = data.get("cards") or []
    card_rows = []
    for c in cards:
        masked = c.get("maskedCardNumber", "")
        tail = masked[-4:] if masked else ""
        status_display = (c.get("cardStatusDisplay") or "").upper()
        is_active = status_display == "ACTIVATED"
        per_card_info = details_map.get(masked) or {}
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
        store.upsert_cards(card_rows, commit=False)

    # --- 逐卡明細 ---
    billed_new = 0
    unbilled_rows_all = []
    detail_metrics = []
    card_detail_raw = data.get("card_detail")
    card_details = card_detail_raw if isinstance(card_detail_raw, dict) else {}
    for entry in card_details.values():
        masked = entry.get("masked", "")
        tail = masked[-4:]
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
            detail_metrics.append((f"card_detail_{masked[:4]}_{tail}", det))

    delta["card_billed_new"] = billed_new
    expected_identities = {
        c.get("maskedCardNumber")
        for c in cards if c.get("id") and c.get("maskedCardNumber")
    }
    fetch_ok = bool(expected_identities) and isinstance(card_detail_raw, dict) and all(
        identity in card_details and card_details[identity].get("unposted_ok") is True
        for identity in expected_identities
    )
    # 每張有 id 的卡都必須帶 collector 明示 unposted_ok=True；aggregate 非空不代表完整。
    delta["card_unbilled"] = store.refresh_card_pending(
        "unbilled", unbilled_rows_all, rules=rules, fetch_ok=fetch_ok, commit=False,
    )
    delta["card_current"] = 0

    # 同一交易內寫 metrics；由 persist_collected 在 facts/cursor 後一次 commit。
    for category, payload in detail_metrics:
        store.put_daily_metric(category, payload, today, commit=False)

    # 卡片彙總快照（應繳/額度）
    if cards:
        summary = [{
            "name": c.get("name"), "masked": c.get("maskedCardNumber"),
            "outstanding": c.get("outstandingBalance"),
            "min_payment": c.get("minimumPayableAmount"),
            "due_date": c.get("paymentDueDate"),
        } for c in cards]
        store.put_daily_metric("card_summary", summary, today, commit=False)
    store.log_sync(delta, commit=False)
    if commit:
        store.commit()
    return delta
