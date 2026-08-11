"""Portfolio summary router (Phase 6 / Plan A — MoneyBook 總覽).

聚合各銀行 sqlite 的「資產 / 負債」數字, 算出「淨資產」給 dashboard 顯示。

設計策略:
  - 跨 11 家銀行格式差異大, **保守提取** — 看得懂才算, 否則 N/A
  - 一律台幣 (外幣資產不換算, 避免推算違反禁推算紅線)
  - 資料來源全 freshness window: 預設只看 90 天內的 snapshot, 太舊資料視為 stale

資產 (assets) 來源:
  1. balance_history.twd_balance — 銀行真實寫的台幣存款餘額 (最新 snapshot)
  2. 來源優先序: balance_history 最新 row > daily_metric balance_latest fallback

負債 (liabilities) 來源:
  1. 信用卡未繳金額 — daily_metric.card_summary (各家 schema 不同, per-bank parser)
  2. 信用卡 pending unbilled — card_pending_txns 表 sum (本期已刷未出帳, 也算負債)
  3. (未來) 房貸 / 個信 — daily_metric.loan / loan_credit, 目前不算 (parse 太雜)

每家銀行的最新 snapshot 是 max(snapshot_date), stale = 超過 90 天的不算。

Endpoint:
  GET /portfolio/summary →
    {
      "total_assets": 43_220_322,        # TWD, sum 所有銀行
      "total_liabilities": 285_063,      # TWD, sum 信用卡未繳 + pending
      "net_worth": 42_935_259,
      "as_of": "2026-06-14",
      "by_bank": [
        {"bank": "cathay", "assets": 888987, "liabilities": 0,
         "card_unbilled": 0, "stale": false, "as_of": "2026-06-12"},
        ...
      ],
      "skipped": ["fubon", "scb"]  # 無資料或解不出
    }
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, UTC
from math import isfinite
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.core import account_classify, bank_data
from backend.core.card_status import CathayBillStatus, cathay_bill_status
from backend.server.deps import current_user
from backend.server import db, fx_service
from backend.server.bank_account_projection import bank_accounts as _project_bank_accounts
from backend.server.dashboard_cache import (
    DEFAULT_DASHBOARD_TTL_SECONDS,
    clear_dashboard_cache,  # noqa: F401 — intentional router-level cache control API
    get_or_set_dashboard_cache,
)
from backend.server.db_facade import (
    AccountNotFound,
    BankNotAvailable,
    db_api,
)

router = APIRouter(prefix="/portfolio", tags=["portfolio"])
perf_log = logging.getLogger("backend.perf")

# stale = snapshot 超過 N 天就標 stale (但仍算進總額, 只是 UI 上掛 ⚠ icon)
STALE_DAYS = 90
# /portfolio/accounts 用更嚴格的 stale window — 帳戶餘額過 7 天沒更新就 stale
ACCOUNT_STALE_DAYS = 7

KNOWN_BANKS = bank_data.KNOWN_BANKS


def _to_int(val: Any) -> int | None:
    """安全把 raw value 轉 int. 支援 '1,234' / '1234.0' / int / None."""
    if val is None or val == "" or isinstance(val, bool):
        return None
    try:
        s = str(val).replace(",", "").strip()
        if not s or s == "-":
            return None
        number = float(s)
        return int(number) if isfinite(number) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _liability_to_twd(balance: int | float | None, currency: str | None) -> int | None:
    """Canonical liability magnitude converted to TWD; unavailable FX stays unknown."""
    magnitude = account_classify.normalize_liability_magnitude(balance)
    if magnitude is None:
        return None
    normalized_currency = (currency or "TWD").upper()
    if normalized_currency == "TWD":
        return round(magnitude)
    try:
        return fx_service.convert_to_twd(magnitude, normalized_currency)
    except Exception:
        return None


def _latest_payload(bank: str, category: str, user_id: int) -> tuple[str, dict] | None:
    """撈某 bank 某 category 最新 snapshot 的 payload_json (parse 完). None = 沒資料.

    Plan B B4: 完全走 db_facade. Caller 不再傳 con.
    """
    m = db_api.get_latest_metric(bank=bank, category=category, user_id=user_id)
    if m is None:
        return None
    return (m.snapshot_date, m.payload)


def _latest_balance(bank: str, user_id: int) -> tuple[str, int | None] | None:
    """銀行 balance_history 最新一筆 (snapshot_date, twd_balance)."""
    b = db_api.get_latest_twd_balance(bank=bank, user_id=user_id)
    if b is None:
        return None
    return (b.snapshot_date, b.twd_balance)


def _latest_loan_balance(bank: str, user_id: int) -> tuple[str, int | None] | None:
    """銀行貸款餘額：優先讀 balance_history.loan_balance（爬蟲層 sum 過的總額），
    若該欄位為 NULL 則 fallback sum accounts WHERE product_type IN (loan/mortgage)。

    這條鏈解使用者「所有爬蟲都應該處理好貸款」的鐵律——讓 portfolio 層
    無論銀行用 balance_history 或 accounts 表存貸款餘額都能讀到。

    Plan B B4: 全 SQL 走 db_facade. Caller 不再傳 con.
    """
    # 1. 優先 balance_history.loan_balance
    lb = db_api.get_latest_loan_balance(bank=bank, user_id=user_id)
    if lb is not None:
        magnitude = account_classify.normalize_liability_magnitude(lb.loan_balance)
        return (lb.snapshot_date, int(magnitude) if magnitude is not None else None)
    # 2. fallback：accounts 有 product_type 為 loan/mortgage 才進 daily_metrics 撈
    loan_accts = db_api.list_loan_accounts(bank=bank, user_id=user_id)
    if not loan_accts:
        return None
    account_total = 0
    account_dates: list[str | None] = []
    account_balances_complete = True
    for account in loan_accts:
        twd_magnitude = _liability_to_twd(account.raw_balance, account.currency)
        if twd_magnitude is None:
            account_balances_complete = False
            break
        account_total += twd_magnitude
        account_dates.append(account.raw_balance_date)
    if account_balances_complete:
        dated = [date for date in account_dates if date]
        aggregate_date = min(dated) if len(dated) == len(account_dates) else ""
        return (aggregate_date, account_total)
    latest = _latest_payload(bank, "balance_latest", user_id)
    if not latest:
        return None
    snapshot_date, payload = latest
    loan = account_classify.normalize_liability_magnitude(
        _to_int(payload.get("loan")),
    )
    if loan is None or loan == 0:
        return None
    return (snapshot_date, int(loan))


def _is_stale(snapshot_iso: str | None) -> bool:
    """snapshot 超過 STALE_DAYS 天 → True (前端會掛 ⚠️ icon)."""
    return _is_stale_days(snapshot_iso, STALE_DAYS)


def _is_stale_days(snapshot_iso: str | None, days: int) -> bool:
    """snapshot 超過 days 天 → True. 用於不同 endpoint 不同 freshness window."""
    if not snapshot_iso:
        return True
    # snapshot_date 形如 '2026-06-12T00:00:00' 或 '2026-06-12'
    try:
        head = snapshot_iso[:10]
        dt = datetime.strptime(head, "%Y-%m-%d").replace(tzinfo=UTC)
        return (datetime.now(UTC) - dt) > timedelta(days=days)
    except (ValueError, TypeError):
        return True


def _normalize_iso_date(raw: str | None) -> str | None:
    """把各種 txn_datetime / updated_at 格式正規化成 'YYYY-MM-DD' (or None).

    真實爬蟲層 txn_datetime 至少 3 種格式:
      - '2026-06-03T15:54:53'      (cathay/linebank — ISO 標準)
      - '2026-05-16 13:02:02'      (ubot — ISO+空白)
      - '2026/05/2101:06'          (sinopac — 斜線, 日期+時間黏在一起無 separator)
      - '2026/05/25'               (scsb — 只有日期, 斜線)

    snapshot_date 一律輸出 ISO 'YYYY-MM-DD', None 表示無法解析或為空.
    """
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # 取前 10 char 試試 ISO 'YYYY-MM-DD'
    iso_head = s[:10]
    try:
        datetime.strptime(iso_head, "%Y-%m-%d")
        return iso_head
    except ValueError:
        pass
    # 試試斜線 'YYYY/MM/DD'
    try:
        datetime.strptime(iso_head, "%Y/%m/%d")
        return iso_head.replace("/", "-")
    except ValueError:
        pass
    return None


# ============================================================
# Per-bank liability parsers — 各家信用卡 card_summary 格式不同
# ============================================================
#
# **負債 = 上期帳單未繳餘額**（使用者規則 2026-06-14）
#   - 上期帳單已出, 還沒繳的錢才算負債
#   - 本月已刷未出帳的不算負債（那是「本月消費」, 另算）
#   - 分期未來幾期不算負債（要等出帳變上期帳單才算）
#
# 每個 parser 拿 daily_metric payload (parsed dict), 回傳「上期帳單未繳 TWD」.
# None = 解不出 (UI 顯示 N/A); 0 = 真實 0 元 (上期已繳清).

def _liab_cathay(payload: dict) -> int | None:
    """Cathay 規則: latest_bill.twd.billAmount > 0 且 payBillStatus='UnPaid' → 未繳金額.

    觀察真實 payload (2026-06-13 使用者 cathay 已繳清):
      latest_bill.twd: {statementDate:'202604', billAmount:0, payBillStatus:'UnPaid'}
      total_consumption: {unpaid: 0, current_balance: 0}

    語意對照:
      - billAmount=上期帳單應繳金額 (出帳當下)
      - payBillStatus='Paid'/'Payed' → 0 (已繳); 'UnPaid' → billAmount (未繳)
      - total_consumption.unpaid 其實在 cathay schema 是「累計未繳」
        但目前 cathay user 永遠是 0, 跟 latest_bill 對齊, 用任一個都行

    保守取 latest_bill.twd.billAmount when payBillStatus='UnPaid'.
    """
    lb = (payload.get("latest_bill") or {}).get("twd") or {}
    if not lb:
        return None
    status = cathay_bill_status(lb.get("payBillStatus"))
    amount = _to_int(lb.get("billAmount"))
    if amount is None or status is None:
        return None
    if status is CathayBillStatus.PAID:
        return 0
    # Canonical unpaid → return the bill amount.
    return amount


def _liab_ubot(payload: dict) -> int | None:
    """Ubot 規則: TotalData.Card = 上期帳單應繳 (真正負債).

    重要 schema 陷阱 (使用者 2026-06-14 指正):
      - TotalData.Unpaid (字面是「未繳」) **其實是本月已刷未出帳 sum**, 不是負債
      - TotalData.Card = 上期帳單應繳金額 (本期需繳)
      - CardList[].payAmt = TotalData.Card (per-card 拆分)

    真實案例 (2026-06-13):
      上期 billed sum = 38647 (2026-06-03 出帳)
      TotalData.Card = 38647 ✅ 對齊
      TotalData.Unpaid = 41065 = pending sum (本月 11 個別+分期 12/12)

    所以負債取 TotalData.Card 不是 Unpaid.
    """
    td = payload.get("TotalData") or {}
    return _to_int(td.get("Card"))


def _liab_hsbc(payload: list | dict) -> int | None:
    """HSBC 規則: card[].outstanding = 該卡歷史累計未繳 (真實負債).

    驗證 (2026-06-13):
      billed 一期 sum = 95881
      outstanding sum = 130393 (差 34512 = 上上期未繳累積)
      → outstanding 是「目前帳上欠多少」, 真實負債定義對齊 ✅
    """
    if not isinstance(payload, list):
        return None
    total = 0
    saw_any = False
    for card in payload:
        if not isinstance(card, dict):
            continue
        v = _to_int(card.get("outstanding"))
        if v is not None:
            total += v
            saw_any = True
    return total if saw_any else None


def _liab_sinopac(payload: list | dict) -> int | None:
    """Sinopac 規則: SubInfo 找「本期應繳」(=上期帳單出帳本期該繳的)."""
    if not isinstance(payload, list) or not payload:
        return None
    sub_info = (payload[0].get("SubInfo") if isinstance(payload[0], dict) else None) or []
    if not sub_info or not isinstance(sub_info[0], list):
        return None
    for entry in sub_info[0]:
        if isinstance(entry, dict) and entry.get("DataText") == "本期應繳":
            return _to_int(entry.get("DataValue"))
    return None


def _liab_ctbc(payload: dict) -> int | None:
    """CTBC: schema 還沒摸清楚, 暫回 None (UI 顯示 N/A)."""
    return None


# bank → category key + parser
LIABILITY_PARSERS: dict[str, tuple[str, Any]] = {
    "cathay": ("card_summary", _liab_cathay),
    "ubot": ("card_summary", _liab_ubot),
    "hsbc": ("card_summary", _liab_hsbc),
    "sinopac": ("card_summary", _liab_sinopac),
    "ctbc": ("card_summary", _liab_ctbc),  # placeholder
}


def _current_month_iso() -> str:
    """Return current month as 'YYYY-MM' (UTC). 用 UTC 跟 backend 其他地方一致."""
    return datetime.now(UTC).strftime("%Y-%m")


def _included_card_spending_amount(row: Any) -> int:
    """Return the card amount remaining after per-split exclusions.

    Parent ``auto_excluded`` rows are removed by the facade query. Invalid
    legacy JSON or splits that do not reconcile to the parent conservatively
    fall back to the parent amount instead of silently under-counting.
    """
    parent_amount = abs(_to_int(getattr(row, "amount", None)) or 0)
    raw = getattr(row, "splits_overwrite", None)
    if not raw:
        return parent_amount
    try:
        splits = json.loads(raw)
    except (TypeError, ValueError):
        return parent_amount
    if not isinstance(splits, list) or not splits or len(splits) > 20:
        return parent_amount

    total = 0
    included = 0
    for split in splits:
        if not isinstance(split, dict):
            return parent_amount
        value = split.get("amount")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return parent_amount
        excluded = split.get("auto_excluded", False)
        if not isinstance(excluded, bool):
            return parent_amount
        amount = value
        total += amount
        if not excluded:
            included += amount
    return included if total == parent_amount else parent_amount


def _is_card_expense(row: Any) -> bool:
    txn_type = (getattr(row, "txn_type", None) or "").lower()
    if txn_type in {"cashback", "refund", "fee_waiver", "payment"}:
        return False
    flow_type = (getattr(row, "flow_type", None) or "").lower()
    if flow_type:
        return flow_type == "expense"
    return bool(_to_int(getattr(row, "amount", None)) or 0)


def _pending_belongs_to_month(row: Any, month: str) -> bool:
    value = str(getattr(row, "consume_date", None) or "").strip()
    return not value or value[:7].replace("/", "-") == month


def _bank_current_month_spending(bank: str, user_id: int) -> int:
    """本月消費 (TWD only) = 本月卡片支出，排除非支出與舊月 pending。

    使用者規則:
      - pending 有 consume_date 時只算本月；缺日期才視為當下未出帳並納入
      - billed 以 consume_date 過濾本月
      - cashback/refund/fee_waiver/payment 與 non-expense flow 不算消費
      - card、parent 或 split exclusion 皆需排除

    Plan B B4: 全 SQL 走 db_facade. Caller 不再傳 con.
    """
    month = _current_month_iso()
    pattern = f"{month}-%"
    # 撈 excluded card_no set (該 bank only)
    excluded_map = db_api.list_excluded_card_nos_all_banks(
        user_id=user_id, banks=[bank],
    )
    excluded_cards: set[str] = excluded_map.get(bank, set())

    total = 0
    # pending 有可用消費日時只算本月；無日期保留未出帳 fallback。
    for r in db_api.list_card_pending_amounts_for_user(bank=bank, user_id=user_id):
        currency = (r.currency or "TWD").upper()
        if currency != "TWD":
            continue
        if r.card_no in excluded_cards:
            continue
        if not _pending_belongs_to_month(r, month) or not _is_card_expense(r):
            continue
        total += _included_card_spending_amount(r)
    # billed 表 — 看 consume_date 過濾本月
    for r in db_api.list_card_billed_amounts_for_month(
        bank=bank, user_id=user_id, month_pattern=pattern,
    ):
        currency = (r.currency or "TWD").upper()
        if currency != "TWD":
            continue
        if r.card_no in excluded_cards:
            continue
        if not _is_card_expense(r):
            continue
        total += _included_card_spending_amount(r)
    return total


# ============================================================
# Endpoint
# ============================================================

@router.get("/summary")
def portfolio_summary(user: dict = Depends(current_user)) -> dict[str, Any]:
    return get_or_set_dashboard_cache(
        "portfolio.summary",
        user_id=user["id"],
        ttl_seconds=DEFAULT_DASHBOARD_TTL_SECONDS,
        compute=lambda: _compute_portfolio_summary(user["id"]),
    )


def _compute_portfolio_summary(user_id: int) -> dict[str, Any]:
    """掃所有 sqlite 算總資產 / 總負債 / 本月消費 / 淨資產.

    **語意定義** (使用者規則 2026-06-14):
      total_assets       = 銀行台幣存款 balance_history.twd_balance sum (TWD only, 真實值)
      fx_assets_twd      = 所有銀行外幣帳戶用 fx_service 換 TWD 的估值 sum
      brokerage_assets_twd = SnapTrade 每個券商帳戶 balance_total 換 TWD 的估值 sum
      manual_assets_twd = 手動存款與投資帳戶換 TWD 的估值 sum
      total_assets_with_fx = total_assets + fx_assets_twd + brokerage_assets_twd + manual_assets_twd
      total_liabilities  = **上期帳單未繳** sum (只算已出帳還沒繳的)
      current_month_spending = 本月 consume_date 在 pending + billed 表 sum
      net_worth          = total_assets - total_liabilities (TWD only, 保守)
      net_worth_with_fx  = total_assets_with_fx - total_liabilities (含外幣估值)

    為什麼 net_worth 不扣 current_month_spending:
      本月消費的錢還在「資產」存款裡 (還沒從 balance_history.twd_balance 扣除),
      要等下個月帳單出, 自動扣款後 balance 才會下降。所以 current_month_spending
      只是一個資訊性數字, 給使用者知道「本月我大概要繳多少」.

    fx_assets_twd 設計鐵則 (使用者 2026-06-14):
      - 只算 accounts 表 currency != 'TWD' 且 twd_transactions 撈得到 balance 的帳戶
      - 用 fx_service.convert_to_twd (台銀即期買賣中間價, 6h cache)
      - fx_service 抓不到該幣別 → 該帳戶不算進 fx_assets_twd (保守略過)
      - 不從 balance_history.fx_balance 撈 (爬蟲對 fx 折算口徑不統一, 不可信)

    Return shape:
        {
            "total_assets": int,              # TWD only (真實)
            "fx_assets_twd": int,             # 外幣帳戶 TWD 估值 sum
            "brokerage_assets_twd": int,     # 券商帳戶總值 TWD 估值
            "total_assets_with_fx": int,      # = total_assets + fx + brokerage
            "total_liabilities": int,         # 上期帳單未繳 sum
            "current_month_spending": int,
            "net_worth": int,                 # = total_assets - total_liabilities (TWD only)
            "net_worth_with_fx": int,         # = total_assets_with_fx - total_liabilities
            "as_of": "YYYY-MM-DD",
            "by_bank": [{...}, ...],
            "skipped": [...]
        }
    """
    from backend.server.fx_service import convert_to_twd

    by_bank: list[dict[str, Any]] = []
    skipped: list[str] = []
    total_assets = 0
    fx_assets_twd = 0           # 外幣帳戶 TWD 估值 sum (給 frontend 大字用)
    brokerage_assets_twd = 0    # SnapTrade account total；不重加 cash / positions
    manual_assets_twd = 0       # 手動存款 + 投資 current valuation（breakdown only）
    manual_liabilities_twd = 0  # 手動貸款 current valuation（breakdown only）
    total_liabilities = 0       # 信用卡未繳 + 貸款餘額
    total_card_unpaid = 0       # 純信用卡未繳（給 frontend 拆分用）
    total_loan = 0              # 純貸款餘額
    total_current_month = 0
    overall_latest: str | None = None
    total_started = time.perf_counter()

    for bank in KNOWN_BANKS:
        bank_started = time.perf_counter()
        balance_ms = liab_ms = loan_ms = spending_ms = accounts_ms = 0.0
        section_started = time.perf_counter()
        balance_info = _latest_balance(bank, user_id)
        balance_ms = (time.perf_counter() - section_started) * 1000
        assets: int | None = None
        assets_date: str | None = None
        if balance_info is not None:
            assets_date, assets_val = balance_info
            assets = assets_val if assets_val is not None else None

        # 信用卡未繳 = 真實負債（per-bank parser）
        card_unpaid: int | None = None
        parser_info = LIABILITY_PARSERS.get(bank)
        liab_snapshot_date: str | None = None
        if parser_info:
            category, parser_fn = parser_info
            section_started = time.perf_counter()
            latest = _latest_payload(bank, category, user_id)
            liab_ms = (time.perf_counter() - section_started) * 1000
            if latest:
                liab_snapshot_date, payload = latest
                card_unpaid = parser_fn(payload)

        # 貸款餘額（信貸/房貸）— 使用者鐵律：所有爬蟲都要處理
        loan_balance: int | None = None
        loan_snapshot_date: str | None = None
        section_started = time.perf_counter()
        loan_info = _latest_loan_balance(bank, user_id)
        loan_ms = (time.perf_counter() - section_started) * 1000
        if loan_info is not None:
            loan_snapshot_date, loan_val = loan_info
            loan_balance = loan_val if loan_val is not None else None

        # 本月消費 (資訊性) = pending + billed 本月 consume_date sum
        section_started = time.perf_counter()
        month_spending = _bank_current_month_spending(bank, user_id)
        spending_ms = (time.perf_counter() - section_started) * 1000

        # 外幣帳戶 TWD 估值 sum (per-bank, 給總資產 with_fx 用)
        # 鐵則: 用 _bank_accounts() 同邏輯抓 per-account balance, 過濾 currency!=TWD,
        # 用 fx_service.convert_to_twd 換 TWD; 抓不到 rate 該帳戶 skip
        # Phase 6 (excluded): 跳過使用者手動標「不納入淨資產統計」的帳戶
        bank_fx_twd = 0
        twd_excluded_deduct = 0    # 台幣存款 excluded → 從 assets 扣
        loan_excluded_deduct = 0   # 貸款 excluded → 從 loan_balance 扣
        try:
            section_started = time.perf_counter()
            bank_accounts = _bank_accounts(bank, user_id)
            accounts_ms = (time.perf_counter() - section_started) * 1000
            for acc in bank_accounts:
                cur = (acc.currency or "TWD").upper()
                ptype = (acc.product_type or "").lower()
                # 負債帳戶永遠不進 FX 資產；excluded 才從負債 aggregate 扣除。
                if account_classify.is_liability_type(ptype):
                    if acc.excluded and acc.balance is not None:
                        excluded_twd = _liability_to_twd(acc.balance, acc.currency)
                        if excluded_twd is not None:
                            loan_excluded_deduct += excluded_twd
                    continue
                if cur == "TWD":
                    # 台幣存款 excluded (非貸款) → 從 assets 扣
                    if acc.excluded and acc.balance is not None:
                        twd_excluded_deduct += round(acc.balance)
                    continue
                if acc.excluded:
                    continue
                if acc.balance is None:
                    continue
                est = convert_to_twd(acc.balance, acc.currency)
                if est is not None:
                    bank_fx_twd += est
        except Exception:
            # fx_service / sqlite 錯不要 break 整個 summary
            pass
        bank_ms = (time.perf_counter() - bank_started) * 1000
        perf_log.info(
            "event=portfolio.summary section=bank user_id=%s bank=%s duration_ms=%.1f balance_ms=%.1f liab_ms=%.1f loan_ms=%.1f spending_ms=%.1f accounts_ms=%.1f",
            user_id, bank, bank_ms, balance_ms, liab_ms, loan_ms, spending_ms, accounts_ms,
        )

        # 若銀行什麼資料都沒, skip
        if (assets is None and card_unpaid is None
                and loan_balance is None and month_spending == 0
                and bank_fx_twd == 0):
            skipped.append(bank)
            continue

        bank_assets = (assets or 0) - twd_excluded_deduct
        bank_assets = max(bank_assets, 0)     # 防 deduct 超過 (理論上不會, 保險夾)
        bank_card_unpaid = card_unpaid or 0
        # Phase 6 (excluded): 貸款 excluded → 從 loan_balance 扣
        # 同 assets 邏輯, deduct 後夾在 0 之上防超量
        bank_loan = (loan_balance or 0) - loan_excluded_deduct
        bank_loan = max(bank_loan, 0)
        bank_liab = bank_card_unpaid + bank_loan
        total_assets += bank_assets
        fx_assets_twd += bank_fx_twd
        total_card_unpaid += bank_card_unpaid
        total_loan += bank_loan
        total_liabilities += bank_liab
        total_current_month += month_spending

        # 取較新的 snapshot date 當該 bank 的 as_of
        bank_dates = [d for d in (assets_date, liab_snapshot_date,
                                  loan_snapshot_date) if d]
        bank_as_of = max(bank_dates)[:10] if bank_dates else None
        if bank_as_of and (overall_latest is None or bank_as_of > overall_latest):
            overall_latest = bank_as_of

        by_bank.append({
            "bank": bank,
            "assets": assets,
            "fx_assets_twd": bank_fx_twd or None,
            "liabilities": bank_liab if (card_unpaid is not None or
                                         loan_balance is not None) else None,
            "card_unpaid": card_unpaid,
            "loan_balance": loan_balance,
            "current_month_spending": month_spending,
            "stale": _is_stale(bank_as_of),
            "as_of": bank_as_of,
        })

    try:
        brokerage_snapshot = db.snaptrade_snapshot(user_id)
        for account in brokerage_snapshot["accounts"]:
            amount = account.get("balance_total")
            currency = account.get("balance_currency")
            if amount is None or not currency:
                continue
            try:
                estimate = convert_to_twd(amount, currency)
            except Exception:
                continue
            if estimate is not None:
                brokerage_assets_twd += estimate
        brokerage_as_of = _normalize_iso_date(brokerage_snapshot.get("last_synced_at"))
        if brokerage_as_of and (overall_latest is None or brokerage_as_of > overall_latest):
            overall_latest = brokerage_as_of
    except Exception:
        # 券商快照 / FX 失敗不應遮蔽既有銀行 summary。
        pass

    # 手動帳戶共用既有 product taxonomy 與 summary buckets。交易明細只作
    # journal；不以歷史成交價冒充 current valuation。First-party manual store
    # 若整體讀取失敗必須 fail closed，不能把已知資產偽裝成 0。
    from backend.server.financial_accounts import list_manual_accounts

    for account in list_manual_accounts(user_id):
        if not account.included_in_net_worth or account.balance is None:
            continue
        try:
            estimate = fx_service.convert_to_twd(account.balance, account.currency)
        except Exception:
            skipped.append(account.id)
            continue
        if estimate is None:
            skipped.append(account.id)
            continue
        product_type = account.product_type
        if account_classify.is_liability_type(product_type):
            magnitude = abs(estimate)
            total_liabilities += magnitude
            total_loan += magnitude
            manual_liabilities_twd += magnitude
        elif (
            product_type == account_classify.ProductType.INVESTMENT
            or account_classify.is_asset_type(product_type)
        ):
            value = max(estimate, 0)
            manual_assets_twd += value
        manual_as_of = _normalize_iso_date(account.as_of)
        if manual_as_of and (overall_latest is None or manual_as_of > overall_latest):
            overall_latest = manual_as_of

    total_assets_with_fx = (
        total_assets + fx_assets_twd + brokerage_assets_twd + manual_assets_twd
    )
    total_ms = (time.perf_counter() - total_started) * 1000
    perf_log.info(
        "event=portfolio.summary section=total user_id=%s duration_ms=%.1f banks=%s skipped=%s",
        user_id, total_ms, len(by_bank), len(skipped),
    )
    return {
        "total_assets": total_assets,                       # TWD only (真實)
        "fx_assets_twd": fx_assets_twd,                     # 銀行外幣帳戶 TWD 估值
        "brokerage_assets_twd": brokerage_assets_twd,       # 券商帳戶 TWD 估值
        "manual_assets_twd": manual_assets_twd,             # 手動資產獨立 bucket
        "manual_liabilities_twd": manual_liabilities_twd,   # 手動負債 breakdown（已含 total_liabilities）
        "total_assets_with_fx": total_assets_with_fx,       # 含銀行外幣、券商與手動資產估值
        "total_liabilities": total_liabilities,
        "total_card_unpaid": total_card_unpaid,             # frontend 拆分用
        "total_loan": total_loan,                           # frontend 拆分用
        "current_month_spending": total_current_month,
        "net_worth": total_assets - total_liabilities,                   # TWD only
        "net_worth_with_fx": total_assets_with_fx - total_liabilities,   # 含外幣與券商估值
        "as_of": overall_latest,
        "by_bank": sorted(by_bank, key=lambda b: -((b["assets"] or 0) + (b.get("fx_assets_twd") or 0))),
        "skipped": skipped,
    }


# ============================================================
# /portfolio/accounts — per-account latest balance list
# ============================================================
#
# 給 frontend「帳戶」tab 用。回每帳戶最新餘額（原幣 or TWD, 看 currency 欄）。
#
# 設計鐵則:
#   - 餘額來源優先 twd_transactions 該 account_no 最新 txn_datetime 的 balance
#     (sinopac JPY 帳戶 balance 直接以 JPY 存進去, 不分 currency)
#   - 若該 account_no 沒對應 txn (e.g. cathay 數位帳戶 account_no 不對齊),
#     balance=None, snapshot_date=accounts.updated_at[:10]
#   - 沒 accounts 表 / 沒 rows 該銀行 skip (不 raise)
#   - is_stale 用 ACCOUNT_STALE_DAYS=7 (短一點, 帳戶餘額過 7 天沒更新就 stale)


class BankAccountBalance(BaseModel):
    bank: str                   # 'sinopac', 'cathay', ...
    account_no: str             # raw account number from accounts table
    currency: str               # 'TWD' / 'JPY' / 'USD' / ...
    nickname: str | None        # accounts.nickname (可能空字串或 None)
    # Phase 8.2 C (2026-06-14): user 覆寫帳戶暱稱 (raw 不動)
    # UI fallback: nickname_overwrite || nickname || account_no
    nickname_overwrite: str | None = None
    product_type: str | None    # accounts.product_type ('deposit', 'loan', ...)
    type: str | None            # accounts.type (中文 nickname 補充)
    balance: float | None        # 該帳戶最新餘額 (currency 對應原幣或 TWD)
                                 # float 因外幣可能 USD 1.55；台幣帳戶仍是整數但 JSON 序列化都能吃
    snapshot_date: str | None   # ISO date YYYY-MM-DD
    is_stale: bool              # > ACCOUNT_STALE_DAYS 天沒更新就 True
    # Phase 6 新增: 外幣帳戶 → TWD 估值 (給 frontend 顯示「JPY 1,201,387 ≈ NT$ 240,277」)
    # 鐵則:
    #   - currency='TWD' → twd_estimate=balance (不換算, 1:1)
    #   - currency!='TWD' 且 balance 有值 → 用 fx_service.convert_to_twd
    #   - fx_service 抓不到該幣別 → 兩個欄位都 None
    #   - balance=None → 兩個欄位都 None
    twd_estimate: int | None = None       # TWD 估值 (rounded int)
    fx_rate_used: float | None = None     # 用了哪個匯率 (debug + 透明度)
    # Phase 6 (excluded) 新增: 使用者手動標「不納入淨資產統計」
    # → portfolio summary 跳過 (total_assets / fx_assets_twd 都不算)
    # → transactions stats 跳過該帳戶 txn (amount_by_month / amount_by_category 不算)
    # → frontend cards / transactions UI 反灰顯示
    excluded: bool = False


def _bank_accounts(bank: str, user_id: int) -> list[BankAccountBalance]:
    """掃單一銀行 sqlite, 回傳該銀行所有帳戶 + 最新餘額.

    餘額 lookup 優先順序（使用者鐵律：所有爬蟲都該抓帳號級餘額，先信爬蟲直給的）：
      1. accounts.raw_balance — 爬蟲層直接抓的帳號級餘額快照（最可信）
      2. twd_transactions 最新一筆 balance — 舊邏輯 fallback
      3. balance_history.loan_balance — 貸款帳戶 fallback
      4. None — fall through
      最後統一符號：loan/mortgage/credit_line balance 一律為負。

    Plan B B4: 全 SQL 走 db_facade. Caller 不再傳 con.
    """
    return [
        BankAccountBalance(**row.model_dump())
        for row in _project_bank_accounts(bank, user_id)
    ]


@router.get("/accounts", response_model=list[BankAccountBalance])
def portfolio_accounts(user: dict = Depends(current_user)) -> list[BankAccountBalance]:
    """回所有銀行所有帳戶最新餘額清單.

    每帳戶一 row, currency 是該帳戶原幣 (TWD/JPY/USD/...), balance 也是原幣金額.
    沒 accounts 表 / 沒 rows 的銀行直接 skip, 不 raise.
    """
    result: list[BankAccountBalance] = []
    for bank in KNOWN_BANKS:
        result.extend(_bank_accounts(bank, user["id"]))
    return result


# ============================================================
# Phase 6 (2026-06-14): excluded flag mutate endpoint
# ============================================================

class AccountExcludedPayload(BaseModel):
    excluded: bool


# Phase 8.2 C (2026-06-14): user 覆寫帳戶暱稱
class AccountNicknamePayload(BaseModel):
    nickname_overwrite: str | None  # None / "" → 清空 (恢復顯示 bank API nickname)


@router.patch("/accounts/{bank}/{account_no}/nickname")
def patch_account_nickname(
    bank: str,
    account_no: str,
    payload: AccountNicknamePayload,
    user: dict = Depends(current_user),
) -> dict[str, Any]:
    """設定/清空 user 對單一帳戶的暱稱覆寫.

    鐵則 (對齊 cards.nickname_overwrite / description_overwrite):
      - accounts.nickname 是銀行 API 原文 (e.g. 「主存錢筒」), 重 sync 蓋, 永遠不動.
      - nickname_overwrite 是 user 在 thoth UI 取的名字, 重 sync 不動.
      - UI fallback: nickname_overwrite || nickname || account_no.
      - payload.nickname_overwrite == None / "" → SQL 寫 NULL.

    老 db 沒 nickname_overwrite 欄 → db_facade 自動 ALTER 兜底 (idempotent).

    Return: {bank, account_no, nickname_overwrite, updated_at}
    """
    if bank not in KNOWN_BANKS:
        raise HTTPException(status_code=404, detail=f"unknown bank: {bank}")
    try:
        with db_api.transaction(bank=bank) as tx:
            result = tx.set_account_nickname(
                user_id=user["id"],
                account_no=account_no,
                nickname_overwrite=payload.nickname_overwrite,
            )
    except BankNotAvailable as e:
        raise HTTPException(
            status_code=404, detail=f"bank db not found: {e.bank}",
        ) from e
    except AccountNotFound as e:
        raise HTTPException(
            status_code=404,
            detail=f"account not found: {e.bank}/{e.account_no}",
        ) from e
    return result.model_dump()


@router.patch("/accounts/{bank}/{account_no}/excluded")
def patch_account_excluded(
    bank: str,
    account_no: str,
    payload: AccountExcludedPayload,
    user: dict = Depends(current_user),
) -> dict[str, Any]:
    """切換單一帳戶的「納入淨資產統計」flag.

    使用者手動標的單一 flag, 同時影響:
      - portfolio summary (total_assets / fx_assets_twd 不算 excluded 帳戶)
      - transactions stats (amount_by_month / amount_by_category 跳過 excluded 帳戶 txn)
      - frontend cards / transactions UI 反灰

    Return: {"bank", "account_no", "excluded", "updated_at"}
    """
    if bank not in KNOWN_BANKS:
        raise HTTPException(status_code=404, detail=f"unknown bank: {bank}")
    try:
        with db_api.transaction(bank=bank) as tx:
            result = tx.set_account_excluded(
                user_id=user["id"],
                account_no=account_no,
                excluded=payload.excluded,
            )
    except BankNotAvailable as e:
        raise HTTPException(
            status_code=404, detail=f"bank db not found: {e.bank}",
        ) from e
    except AccountNotFound as e:
        raise HTTPException(
            status_code=404,
            detail=f"account not found: {e.bank}/{e.account_no}",
        ) from e
    return result.model_dump()


def get_excluded_account_nos(user_id: int) -> dict[str, set[str]]:
    """掃所有銀行 db, 回 {bank: set(excluded account_no)} — limit 本 user.

    給 transactions stats 用 (跳過 excluded 帳戶的 txn).
    沒 accounts 表 / 沒 excluded 欄 / 全空 都安全 fallback 空 dict.
    """
    return db_api.list_excluded_account_nos_all_banks(
        user_id=user_id, banks=list(KNOWN_BANKS),
    )
