"""GET /transactions - Cross-bank transaction query (Phase 5 early implementation).

GET /transactions — 跨銀行交易查詢 (Phase 5 早期實作).

設計思路:
  每家銀行各自一顆 sqlite (backend/data/{bank}.sqlite), 由 BankStore 寫入。
  本 router 負責跨庫 UNION ALL 撈出來、依時間倒序、分頁。

Schema 對齊:
  - twd_transactions: 台幣已過帳 (account_no/txn_datetime/account_date/description/expend/income/balance/category)
  - card_billed_txns: 信用卡已出帳明細 (card_no/bill_date/consume_date/post_date/description/amount/currency/category)
  - card_pending_txns: 信用卡未出帳/即時 (scope/card_no/consume_date/description/amount/currency/category)

  正規化成 Transaction 統一 shape:
    {
      "bank": "hsbc",
      "kind": "twd" | "billed" | "pending",
      "date": "2026-06-01",
      "datetime": "2026-06-01T13:22:00" | None,
      "description": "...",
      "amount": -1234 (negative=expense, positive=income, 統一 int 整數元),
      "currency": "TWD",
      "category": "餐飲",
      "account_or_card": "****7016",   # 帳號末四/卡號末四
      "raw": {...}  # 原 row dict, 給 detail view
    }

Endpoints:
  GET  /transactions
       query:
         bank    = optional[str | comma list]  e.g. "hsbc" or "hsbc,sinopac"
         kind    = optional[twd | billed | pending | all]  default=all
         since   = optional[YYYY-MM-DD]
         until   = optional[YYYY-MM-DD]
         account_id = optional[int] (只看該 BankAccount 對應 bank)
         q       = optional[str] 描述 substring
         category = optional[str]
         limit   = int default=100 max=1000
         offset  = int default=0
       → 200 {
            total: int,
            items: [Transaction, ...],
            stats: { by_bank: {hsbc: 437, ...}, by_kind: {twd: 23, billed: 457, pending: 177} }
          }

  GET  /transactions/stats
       query: 同上但無 limit/offset
       → 200 { total, by_bank, by_kind, by_month: {"2026-06": 12, ...} }

驗證:
  每個 bank.sqlite 不一定存在 (使用者沒同步過該銀行)。
  此 router 對「DB 檔案不存在」graceful skip, 不 raise。
"""
from __future__ import annotations

import json
import logging
import re
import time
from backend.server import db
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from backend.core import bank_data
from backend.server.deps import current_user
from backend.server.dashboard_cache import (
    DEFAULT_DASHBOARD_TTL_SECONDS,
    get_or_set_dashboard_cache,
)
from backend.server.creds_store import AccountsRepo
from backend.server.db_facade import (
    BankNotAvailable,
    TxnColumnMissing,
    TxnNotFound,
    db_api,
)

router = APIRouter(prefix="/transactions", tags=["transactions"])
perf_log = logging.getLogger("backend.perf")


# 跟 SUPPORTED_BANKS 對齊, 但不 import (避免循環)
KNOWN_BANKS = bank_data.KNOWN_BANKS


class HashtagRenameIn(BaseModel):
    old_name: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=50)


class HashtagDeleteIn(BaseModel):
    name: str = Field(min_length=1, max_length=50)


class HashtagMutationOut(BaseModel):
    hashtag: str
    renamed_to: str | None = None
    transactions_updated: int


def _mask_tail(s: str | None, n: int = 4) -> str | None:
    """末 n 位顯示, 其他用 *. 例: '12345678' → '****7050'."""
    if not s:
        return s
    s = str(s)
    if len(s) <= n:
        return s
    return "*" * (len(s) - n) + s[-n:]


def _row_get(r: db.Row, key: str, default=None):
    """安全讀 db.Row 欄位; 老 schema 缺欄位時不炸 (IndexError) 回 default.

    背景: cathay.sqlite 老 schema `twd_transactions` 缺 `category` 欄, hsbc 等
    新版有。本來 `r["category"]` 在缺欄時噴 IndexError, 整個 endpoint 500。
    這個 helper 對「DB 之間 schema drift」做防呆。
    """
    try:
        return r[key]
    except (IndexError, KeyError):
        return default


def _join_display_description(
    description: str | None,
    counterparty_acct: str | None,
    memo: str | None,
) -> str | None:
    """Phase 8.4 (2026-06-15): 拼 display_description 對齊 MoneyBook.

    各銀行 raw description 常是「交易類別名」(永豐「台幣匯款」/玉山「跨行匯入」),
    真正交易對象在 counterparty_acct (永豐 DataText8/玉山對方名稱)。
    Memo 通常是 counterparty + 摘要冗餘, 故 fallback 順序:
      1. description + counterparty_acct 主 token (不同則 join '·')
      2. description 或 counterparty_acct 任一
      3. memo 第一個 token (last resort)

    Raw description 不動 (鐵則「修正≠刪除」), 給 audit/categorizer。
    """
    def _first_token(s: str | None, limit: int = 30) -> str:
        if not s:
            return ""
        clean = s.replace("\u3000", " ").strip()
        tok = clean.split()[0] if clean else ""
        return tok[:limit]

    desc = (description or "").strip()
    cp = _first_token(counterparty_acct)
    # desc 跟 counterparty 都有且不同 → join
    if desc and cp and desc != cp:
        return f"{desc} · {cp}"
    # 只有 desc → 純 desc
    if desc:
        return desc
    # desc 空 → 用 counterparty
    if cp:
        return cp
    # 兩者都空 → memo 第一個 token
    mtok = _first_token(memo)
    if mtok:
        return mtok
    return None


def _normalize_date(s: str | None) -> str | None:
    """各家銀行寫入 sqlite 的日期字串格式不統一, 一律正規化成 ISO 'YYYY-MM-DD'.

    觀察到的真實格式 (2026-06-13 11 家盤點):
      sinopac twd:    '2026/05/2101:06' (年/月/日+時分連寫無空格)
      sinopac billed: '2026/05/04'
      hsbc:           '2026-06-12' (本來就 ISO)
      cathay billed:  '2026-04-08T00:00:00' (ISO + 時分秒, 也可能 T)
      其他:           可能還有變體, 一律 strip 後取前 10 字 + 把 '/' 換 '-'.

    為什麼必須統一:
      (1) string sort: '/' (0x2F) > '-' (0x2D), 用 ISO 串排序 sinopac 會永遠排到 hsbc 之前
      (2) UI 顯示: 圖片顯示同一表格內混 '2026/05/04' 跟 '2026-06-12' 視覺不齊
      (3) frontend filter (since/until) 也是 string compare, 不正規化會找不到範圍
    """
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    # 取前 10 字 (YYYY-MM-DD 或 YYYY/MM/DD)
    head = s[:10]
    # 統一 / 為 -
    head = head.replace("/", "-")
    # 基本格式驗證: 必須是 YYYY-MM-DD
    if len(head) == 10 and head[4] == "-" and head[7] == "-":
        return head
    return head if head else None


def _parse_tags_overwrite(raw: Any) -> list[str]:
    """tags_overwrite 欄 (TEXT JSON array) → list[str].

    NULL / 空字串 → []. 解析失敗 → []. 過濾掉非字串 / 空字串 / 重複的 entry,
    保留原順序 (使用者編輯順序).
    """
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, str):
            continue
        t = item.strip()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _normalize_tags_input(raw: Any) -> list[str]:
    """PATCH body tags 欄輸入正規化 → 乾淨 list[str].

    接受: list[str] / None / 空 list.
    Strip / dedupe / 過濾空字串 / 限制長度 (避免攻擊面 — 標籤名不該超過 50 字).
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("tags 必須是字串陣列")
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise ValueError("tags 內每個值必須是字串")
        t = item.strip()
        if not t:
            continue
        if len(t) > 50:
            raise ValueError(f"標籤過長 (>50 字): {t[:20]}…")
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    if len(out) > 20:
        raise ValueError("單筆交易最多 20 個標籤")
    return out


# ============================================================
# Phase 10 (2026-07-29) — 分類拆帳 (splits_overwrite)
# ============================================================
# 設計: overlay column pattern, 同 description_overwrite / tags_overwrite。
#   - raw amount / category 永不變動 (使用者鐵則「修正≠刪除」)
#   - splits 子項 amount 一律「正數絕對值」, 方向沿用母筆 cashflow_direction
#     (母筆是支出 → 每個子項都是支出; 不允許一筆內同時有收入與支出子項,
#      那是兩筆不同交易, 不是拆帳)
#   - 子項和必須等於母筆 |cashflow_amount| — 分類拆帳的定義就是「不改總額,
#     只改分類歸屬」。和對不上代表使用者算錯, 必須擋下而非默默吞掉。
#   - 每個子項可獨立 auto_excluded → 該份不進收支統計桶 (皇上明確要求)
#   - NULL / [] = 未拆帳, 統計照母筆算 (完全 backward compatible)

MAX_SPLITS = 20
"""單筆最多拆幾份。跟 tags 上限同量級, 純防呆/防攻擊面, 非業務限制。"""


def _parse_splits_overwrite(raw: Any) -> list[dict[str, Any]]:
    """splits_overwrite 欄 (TEXT JSON array) → list[dict]. 壞資料一律回 []."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            amount = int(item.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        out.append({
            "amount": abs(amount),
            "category": item.get("category") or None,
            "subcategory": item.get("subcategory") or None,
            "note": item.get("note") or None,
            "auto_excluded": bool(item.get("auto_excluded")),
        })
    return out


def _split_opt_str(item: dict[str, Any], key: str, limit: int = 100) -> str | None:
    """單一 split 物件的選填字串欄位驗證。空字串 → None。"""
    v = item.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValueError(f"split {key} 必須是字串")
    v = v.strip()
    if not v:
        return None
    if len(v) > limit:
        raise ValueError(f"split {key} 不可超過 {limit} 字")
    return v


def _normalize_splits_input(raw: Any, parent_amount: int) -> list[dict[str, Any]]:
    """PATCH body splits 欄輸入正規化 + 驗證。

    `parent_amount` 是母筆 |cashflow_amount| (絕對值整數)。
    回 [] 代表取消拆帳。任何不合法輸入 raise ValueError (router 轉 400)。
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("splits 必須是陣列")
    if not raw:
        return []
    if len(raw) > MAX_SPLITS:
        raise ValueError(f"單筆交易最多拆成 {MAX_SPLITS} 份")
    if len(raw) < 2:
        raise ValueError("拆帳至少要兩份 (只有一份請直接改該筆分類)")

    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("splits 內每個值必須是物件")
        unknown = set(item.keys()) - {
            "amount", "category", "subcategory", "note", "auto_excluded",
        }
        if unknown:
            raise ValueError(f"split 不支援的欄位: {sorted(unknown)}")
        raw_amount = item.get("amount")
        if raw_amount is None or isinstance(raw_amount, bool):
            raise ValueError("split amount 必須是整數")
        try:
            amount = int(raw_amount)
        except (TypeError, ValueError):
            raise ValueError("split amount 必須是整數") from None
        if amount <= 0:
            raise ValueError("split amount 必須大於 0 (方向沿用母筆, 不用負號)")

        out.append({
            "amount": amount,
            "category": _split_opt_str(item, "category"),
            "subcategory": _split_opt_str(item, "subcategory"),
            "note": _split_opt_str(item, "note", 200),
            "auto_excluded": bool(item.get("auto_excluded")),
        })

    total = sum(s["amount"] for s in out)
    if total != parent_amount:
        raise ValueError(
            f"拆帳金額總和 {total} 與原交易金額 {parent_amount} 不符 "
            f"(差 {total - parent_amount})",
        )
    return out


def _expand_splits(t: dict[str, Any]) -> list[dict[str, Any]]:
    """把一筆已拆帳的 transaction 展開成 N 筆子項; 未拆帳原樣回 [t].

    子項繼承母筆全部欄位, 只覆寫:
      id           → "{母id}#{序號}" (字串, 供 frontend key; 子項不可再 PATCH)
      amount / cashflow_amount / display_amount → 該份金額 (方向沿用母筆)
      category / subcategory / auto_excluded    → 該份自己的
      split_of     → 母筆 id (frontend 可回溯/摺疊顯示)
      split_index  → 第幾份 (0-based)
      splits       → [] (子項不再帶 splits, 避免遞迴展開)
    """
    splits = t.get("splits") or []
    if not splits:
        return [t]
    parent_id = t.get("id")
    direction = t.get("cashflow_direction")
    out: list[dict[str, Any]] = []
    for i, s in enumerate(splits):
        child = dict(t)
        amount = int(s["amount"])
        child["id"] = f"{parent_id}#{i}"
        # amount 沿用母筆符號: 支出為負, 收入為正 (跟母筆 transform 的慣例一致)
        child["amount"] = -amount if (t.get("amount") or 0) < 0 else amount
        child["cashflow_amount"] = amount
        child["display_amount"] = amount
        child["cashflow_direction"] = direction
        child["category"] = s.get("category")
        child["subcategory"] = s.get("subcategory")
        # 子項的 auto_excluded 是「該份是否納入統計」— 皇上要求可分別設定。
        # 母筆若整筆已 auto_excluded, 子項一律跟著排除 (母筆優先, OR 邏輯)。
        child["auto_excluded"] = bool(t.get("auto_excluded")) or bool(s.get("auto_excluded"))
        child["split_of"] = parent_id
        child["split_index"] = i
        child["split_note"] = s.get("note")
        child["splits"] = []
        out.append(child)
    return out


def _transaction_cashflow(
    amount: int | float,
    txn_type: str | None,
) -> tuple[str, int]:
    """Return (cashflow_direction, cashflow_amount) in the user's perspective.

    `amount` is kept as bank/card-statement perspective for audit/backward compat.
    These derived fields are the normalized API contract frontend should use for
    filtering, stats, and display direction.
    """
    amt = int(amount or 0)
    if txn_type in ("cashback", "refund", "fee_waiver"):
        # fee_waiver (年費減免/手續費減免/利息減免): 銀行減免費用, 對 user 是正向現金流 (income),
        # 即使 amount<0 (從帳單視角是「貸記」負值) 也算 income; 跟 refund/cashback 同一 branch.
        # 語意獨立於 refund (refund=商家退款, fee_waiver=銀行減免費) 但 cashflow 方向一致.
        return "income", abs(amt)
    if txn_type == "payment" or amt == 0:
        return "neutral", 0
    if amt > 0:
        return "income", amt
    return "expense", -abs(amt)


def _cashflow_fields(amount: int | float, txn_type: str | None) -> dict[str, Any]:
    direction, value = _transaction_cashflow(amount, txn_type)
    return {
        "cashflow_direction": direction,
        "cashflow_amount": abs(value),
        "display_amount": abs(value),
    }


def _apply_card_date_basis(t: dict[str, Any], card_date_basis: str) -> dict[str, Any]:
    """Apply user-selected card transaction recognition date.

    `date` is the recognition date used for filtering, sorting, and stats.
    For card rows we also expose both source dates so UI/detail can disclose them.
    """
    if t.get("kind") not in {"billed", "pending"}:
        return t
    consume_date = _normalize_date(t.get("consume_date") or t.get("date"))
    post_date = _normalize_date(t.get("post_date"))
    t["consume_date"] = consume_date
    t["post_date"] = post_date
    t["date"] = (post_date or consume_date) if card_date_basis == "post" else consume_date
    return t


def _twd_to_transaction(
    bank: str, r: db.Row, excluded_accounts: set[str] | None = None,
) -> dict[str, Any]:
    """twd_transactions row → 統一 Transaction shape. 支出負值, 收入正值."""
    expend = _row_get(r, "expend") or 0
    income = _row_get(r, "income") or 0
    amount = income - expend  # net 一律 income - expense
    date = _normalize_date(_row_get(r, "txn_datetime")) or _normalize_date(_row_get(r, "account_date"))
    account_no = _row_get(r, "account_no")
    # Phase 6 (excluded): 該帳戶被使用者標「不納入淨資產統計」→ 反灰 + 不算 stats
    is_excluded = bool(excluded_accounts and account_no in excluded_accounts)
    txn_type = None
    return {
        "id": _row_get(r, "id"),  # L8.5 — frontend detail/PATCH 用
        "bank": bank,
        "kind": "twd",
        "date": date,
        "datetime": _row_get(r, "txn_datetime"),
        "description": _row_get(r, "description"),
        "description_overwrite": _row_get(r, "description_overwrite"),  # Phase 8.2: 使用者覆寫
        "amount": amount,
        **_cashflow_fields(amount, txn_type),
        "currency": "TWD",
        "category": _row_get(r, "category"),
        "txn_type": txn_type,  # Phase 6 (B-full): twd_transactions 不分類 (expend/income 已分開)
        "flow_type": _row_get(r, "flow_type"),  # Phase 6 (taxonomy): 收支統計閘門
        "is_subscription": bool(_row_get(r, "is_subscription") or 0),
        "income_category": _row_get(r, "income_category"),  # Phase 7: 5 enum or None
        "subcategory": _row_get(r, "subcategory"),
        "legacy_category": _row_get(r, "legacy_category"),
        "account_no": account_no,
        "account_or_card": _mask_tail(account_no),
        "balance": _row_get(r, "balance"),
        # Phase 8.4 (2026-06-15): 暴露 counterparty_acct / memo — desc 是「交易類別名」
        # (台幣匯款 / 手機轉帳 / 利息存入...), 真正交易對象在 counterparty_acct;
        # memo 通常含完整對方資訊 + 摘要 (給 detail modal 看)。
        "counterparty_bank": _row_get(r, "counterparty_bank"),
        "counterparty_acct": _row_get(r, "counterparty_acct"),
        "memo": _row_get(r, "memo"),
        # display_description: backend 統一 join — raw description 不動,
        # frontend 直接拿這欄顯示對齊 MoneyBook。所有銀行通用。
        "display_description": _join_display_description(
            _row_get(r, "description"),
            _row_get(r, "counterparty_acct"),
            _row_get(r, "memo"),
        ),
        "excluded": is_excluded,
        # Phase 8.3 (2026-06-15): rule auto_excluded 命中 → stats skip 收支桶
        "auto_excluded": bool(_row_get(r, "auto_excluded") or 0),
        # Phase 9 (2026-06-16): user 自定義 tags (overlay column pattern, raw 不動)
        "tags": _parse_tags_overwrite(_row_get(r, "tags_overwrite")),
        # Phase 10 (2026-07-29): 分類拆帳子項 ([] = 未拆帳)
        "splits": _parse_splits_overwrite(_row_get(r, "splits_overwrite")),
        "raw": dict(r),
    }


def _billed_to_transaction(
    bank: str, r: db.Row, excluded_cards: set[str] | None = None,
) -> dict[str, Any]:
    """card_billed_txns row → 統一 shape. amount 正值 = 消費 (信用卡視角), 但顯示要 negative
    所以這裡反號: 信用卡消費對使用者就是支出."""
    amt = _row_get(r, "amount") or 0
    # 信用卡的 amount 從銀行端通常都是正值 (消費金額), 對使用者財務角度是支出 → -amount
    # 但 退款/扣繳沖正會是 negative, 保留原值
    if amt > 0:
        amt = -amt

    # Phase 6: 真實匯率 (來自信用卡帳單) — 禁止推算 / 估算 spot rate
    # 算法: |TWD 入帳金額| / |原幣消費金額|
    # → 這是銀行實際入帳匯率 (含海外刷卡手續費 ~1.5%), 才是 user 真實成本
    # null 條件 (任一成立都不算):
    #   - consume_currency 缺 / 等於 TWD (純台幣消費)
    #   - consume_amount 缺 / 為 0
    #   - amount 為 0
    consume_ccy = _row_get(r, "consume_currency")
    consume_amt_raw = _row_get(r, "consume_amount")
    fx_rate: float | None = None
    fx_rate_source: str | None = None
    if (
        consume_ccy
        and consume_ccy != "TWD"
        and consume_amt_raw is not None
        and consume_amt_raw != 0
        and amt != 0
    ):
        try:
            fx_rate = abs(int(amt)) / abs(float(consume_amt_raw))
            fx_rate_source = "bank_billed"
        except (TypeError, ValueError, ZeroDivisionError):
            fx_rate = None
            fx_rate_source = None

    txn_type = _row_get(r, "txn_type")
    return {
        "id": _row_get(r, "id"),  # L8.5
        "bank": bank,
        "kind": "billed",
        "date": _normalize_date(_row_get(r, "consume_date")),
        "consume_date": _normalize_date(_row_get(r, "consume_date")),
        "datetime": None,
        "description": _row_get(r, "description"),
        "description_overwrite": _row_get(r, "description_overwrite"),  # Phase 8.2
        "amount": amt,
        **_cashflow_fields(amt, txn_type),
        "currency": _row_get(r, "currency") or "TWD",
        "category": _row_get(r, "category"),
        "txn_type": txn_type,  # Phase 6 (B-full): spending/cashback/refund/...
        "flow_type": _row_get(r, "flow_type"),  # Phase 6 (taxonomy): 收支統計閘門
        "is_subscription": bool(_row_get(r, "is_subscription") or 0),
        "income_category": _row_get(r, "income_category"),  # Phase 7: 5 enum or None
        "subcategory": _row_get(r, "subcategory"),
        "legacy_category": _row_get(r, "legacy_category"),
        "card_no": _row_get(r, "card_no"),
        "account_or_card": _mask_tail(_row_get(r, "card_no")),
        "post_date": _row_get(r, "post_date"),
        "consume_currency": consume_ccy,
        "consume_amount": consume_amt_raw,
        "fx_rate": fx_rate,          # Phase 6: 真實匯率 (來自帳單), null = 純台幣或無資料
        "fx_rate_source": fx_rate_source,  # 'bank_billed' | None
        # Phase 6 (excluded): 該卡被使用者標「不納入淨資產統計」→ 反灰 + 不算 stats
        "excluded": bool(excluded_cards and _row_get(r, "card_no") in excluded_cards),
        # Phase 8.3 (2026-06-15): rule auto_excluded 命中 → stats skip 收支桶
        "auto_excluded": bool(_row_get(r, "auto_excluded") or 0),
        # Phase 9 (2026-06-16): user 自定義 tags
        "tags": _parse_tags_overwrite(_row_get(r, "tags_overwrite")),
        # Phase 10 (2026-07-29): 分類拆帳子項 ([] = 未拆帳)
        "splits": _parse_splits_overwrite(_row_get(r, "splits_overwrite")),
        "raw": dict(r),
    }


_PENDING_FX_BRACKET_RE = re.compile(
    r"\[(?P<ccy>[A-Z]{3})\s+(?P<amt>\d+(?:\.\d+)?)\]",
)
"""Pending desc 內嵌原幣 pattern, 例: '暫無資訊 [SGD 100.2]' → ccy='SGD', amt='100.2'.

背景: CTBC 未出帳對外幣消費的處理是把 TWD 估算放 amount, 原幣藏在 description
括號內。Parse 出來可以給 frontend 顯示「真實原幣金額 + 銀行 pending 估算匯率」。
HSBC 等其他銀行 pending 直接存原幣 amount, 不會走這條路徑。
"""


def _pending_to_transaction(
    bank: str, r: db.Row, excluded_cards: set[str] | None = None,
) -> dict[str, Any]:
    amt = _row_get(r, "amount") or 0
    if amt > 0:
        amt = -amt
    currency = _row_get(r, "currency") or "TWD"
    desc = _row_get(r, "description") or ""

    # Phase 6: pending fx_rate — 對應「不同銀行不同存法」現況, 禁推算
    # Case A (HSBC-style): currency=外幣 + amount=原幣值
    #   → 沒 TWD 入帳金額, fx_rate=None
    #   → consume_currency=currency, consume_amount=abs(amt)
    # Case B (CTBC-style): currency=TWD + amount=TWD 估算 + desc 含 [SGD 100.2]
    #   → fx_rate = TWD / 原幣, source='bank_pending_estimate'
    #   → consume_currency 從 desc parse, consume_amount 從 desc parse
    # Case C: currency=TWD + amount=TWD + 無外幣 bracket
    #   → fx_rate=None (純台幣)
    fx_rate: float | None = None
    fx_rate_source: str | None = None
    consume_currency: str | None = None
    consume_amount: float | None = None

    if currency != "TWD":
        # Case A: 原幣消費, amount 就是原幣值
        consume_currency = currency
        try:
            consume_amount = abs(float(amt))
        except (TypeError, ValueError):
            consume_amount = None
        # fx_rate 留 None (要等出帳)
    else:
        # Case B/C: currency=TWD, 看 desc 有沒有 [CCY N] bracket
        m = _PENDING_FX_BRACKET_RE.search(desc)
        if m and amt != 0:
            try:
                consume_currency = m.group("ccy")
                consume_amount = float(m.group("amt"))
                if consume_amount > 0:
                    fx_rate = abs(int(amt)) / consume_amount
                    fx_rate_source = "bank_pending_estimate"
            except (TypeError, ValueError, ZeroDivisionError):
                pass

    txn_type = _row_get(r, "txn_type")
    return {
        "id": _row_get(r, "id"),  # L8.5
        "bank": bank,
        "kind": "pending",
        "date": _normalize_date(_row_get(r, "consume_date")),
        "consume_date": _normalize_date(_row_get(r, "consume_date")),
        "post_date": _normalize_date(_row_get(r, "post_date")),
        "datetime": None,
        "description": desc,
        "description_overwrite": _row_get(r, "description_overwrite"),  # Phase 8.2
        "amount": amt,
        **_cashflow_fields(amt, txn_type),
        "currency": currency,
        "category": _row_get(r, "category"),
        "txn_type": txn_type,  # Phase 6 (B-full)
        "flow_type": _row_get(r, "flow_type"),  # Phase 6 (taxonomy): 收支統計閘門
        "is_subscription": bool(_row_get(r, "is_subscription") or 0),
        "income_category": _row_get(r, "income_category"),  # Phase 7: 5 enum or None
        "subcategory": _row_get(r, "subcategory"),
        "legacy_category": _row_get(r, "legacy_category"),
        "card_no": _row_get(r, "card_no"),
        "account_or_card": _mask_tail(_row_get(r, "card_no")),
        "scope": _row_get(r, "scope"),
        "consume_currency": consume_currency,  # Phase 6
        "consume_amount": consume_amount,        # Phase 6
        "fx_rate": fx_rate,                       # Phase 6: null for HSBC pending (要等出帳)
        "fx_rate_source": fx_rate_source,         # 'bank_pending_estimate' | None
        # Phase 6 (excluded): 該卡被使用者標「不納入淨資產統計」→ 反灰 + 不算 stats
        "excluded": bool(excluded_cards and _row_get(r, "card_no") in excluded_cards),
        # Phase 8.3 (2026-06-15): rule auto_excluded 命中 → stats skip 收支桶
        "auto_excluded": bool(_row_get(r, "auto_excluded") or 0),
        # Phase 9 (2026-06-16): user 自定義 tags
        "tags": _parse_tags_overwrite(_row_get(r, "tags_overwrite")),
        # Phase 10 (2026-07-29): 分類拆帳子項 ([] = 未拆帳)
        "splits": _parse_splits_overwrite(_row_get(r, "splits_overwrite")),
        "raw": dict(r),
    }


def _resolve_banks(
    bank: str | None,
    account_id: int | None,
    user_id: int,
) -> list[str]:
    """解出要查的銀行清單.

    優先順序:
      1. account_id 給了 → 只查該 account 對應的 bank
      2. bank 給了 → 用該 bank (支援 comma list)
      3. 都沒 → 用該 user 所有 bank_accounts 對應的 bank set
      4. fallback (legacy) → 所有 KNOWN_BANKS 中 DB 存在的
    """
    if account_id is not None:
        repo = AccountsRepo()
        acct = repo.get(account_id)
        if acct is None or acct.user_id != user_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "找不到此帳號")
        return [acct.bank]

    if bank:
        return [b.strip() for b in bank.split(",") if b.strip()]

    # 用 user 的 bank_accounts
    repo = AccountsRepo()
    accts = repo.list_for_user(user_id)
    if accts:
        return sorted({a.bank for a in accts})

    # Legacy fallback: follow the whole-data-layer DB_BACKEND choice.
    return bank_data.fallback_banks_with_data()


def _item_get(t: Any, key: str, default: Any = None) -> Any:
    if isinstance(t, dict):
        return t.get(key, default)
    return getattr(t, key, default)


def _apply_stat_filters(
    rows: list[Any],
    *,
    since: str | None,
    until: str | None,
    q: str | None,
    card_date_basis: Literal["consume", "post"] = "consume",
) -> list[Any]:
    out = rows
    if card_date_basis == "post":
        for t in out:
            if _item_get(t, "kind") in ("billed", "pending"):
                post_date = _normalize_date(_item_get(t, "post_date"))
                consume_date = _normalize_date(_item_get(t, "consume_date") or _item_get(t, "date"))
                if hasattr(t, "date"):
                    t.date = post_date or consume_date
    if since:
        out = [t for t in out if (_normalize_date(_item_get(t, "date")) or "") >= since]
    if until:
        out = [t for t in out if (_normalize_date(_item_get(t, "date")) or "") <= until]
    if q:
        # Lightweight rows intentionally do not carry description. If query is
        # present, caller must use the full transform path.
        return []
    return out


def _stat_cashflow_direction(row: Any) -> str:
    txn_type = getattr(row, "txn_type", None)
    if txn_type in ("cashback", "refund"):
        return "income"
    if txn_type == "payment":
        return "neutral"
    amount = getattr(row, "amount", None) or 0
    if amount > 0:
        return "income"
    if amount < 0:
        return "expense"
    return "neutral"


def _stat_cashflow_amount(row: Any) -> int:
    amount = int(getattr(row, "amount", None) or 0)
    direction = _stat_cashflow_direction(row)
    if direction == "income":
        return abs(amount)
    if direction == "expense":
        return -abs(amount)
    return 0


def _expand_stat_split_rows(rows: list[Any]) -> list[Any]:
    """Stats fast path 版的 _expand_splits — 對 TxnStatRow 展開分類拆帳。

    列表路徑走 dict (_expand_splits), stats fast path 走 pydantic TxnStatRow,
    兩條路都必須展開否則 dashboard 與交易列表口徑不一致 (已拆帳的交易在
    dashboard 仍照母筆的單一分類計)。

    stat row 的 `amount` 已是使用者視角 (支出負值), 所以子項沿用母筆符號。
    """
    out: list[Any] = []
    for r in rows:
        splits = _parse_splits_overwrite(_item_get(r, "splits_overwrite"))
        if not splits:
            out.append(r)
            continue
        parent_amount = _item_get(r, "amount") or 0
        sign = -1 if parent_amount < 0 else 1
        parent_excluded = bool(_item_get(r, "auto_excluded"))
        for s in splits:
            out.append(r.model_copy(update={
                "amount": sign * int(s["amount"]),
                "category": s.get("category"),
                "subcategory": s.get("subcategory"),
                # 母筆整筆排除 → 子項一律排除 (OR, 母筆優先); 同 _expand_splits
                "auto_excluded": parent_excluded or bool(s.get("auto_excluded")),
                "splits_overwrite": None,
            }))
    return out


def _collect_transaction_stat_rows(
    banks: list[str],
    kinds: list[str],
    since: str | None,
    until: str | None,
    q: str | None,
    user_id: int,
    card_date_basis: Literal["consume", "post"] = "consume",
) -> list[Any]:
    """Stats fast path: lightweight rows only, no SELECT * / raw transform."""
    if q:
        # q searches description/counterparty/memo; lightweight rows do not carry
        # those fields. Preserve behavior by falling back to full rows.
        return []
    rows: list[Any] = []
    excluded_prefetch_started = time.perf_counter()
    excluded_accounts_by_bank = db_api.list_excluded_account_nos_all_banks(
        user_id=user_id, banks=banks,
    )
    excluded_cards_by_bank = db_api.list_excluded_card_nos_all_banks(
        user_id=user_id, banks=banks,
    )
    excluded_prefetch_ms = (time.perf_counter() - excluded_prefetch_started) * 1000
    perf_log.info(
        "event=transactions.stats section=excluded_prefetch user_id=%s duration_ms=%.1f banks=%s account_banks=%s card_banks=%s",
        user_id, excluded_prefetch_ms, len(banks), len(excluded_accounts_by_bank), len(excluded_cards_by_bank),
    )
    for bank in banks:
        for row in db_api.list_txn_stat_rows_for_bank(
            bank=bank,
            user_id=user_id,
            kinds=kinds,
            excluded_accounts_by_bank=excluded_accounts_by_bank,
            excluded_cards_by_bank=excluded_cards_by_bank,
        ):
            rows.append(row)
    return _apply_stat_filters(
        _expand_stat_split_rows(rows),
        since=since, until=until, q=q, card_date_basis=card_date_basis,
    )


def _collect_transactions(
    banks: list[str],
    kinds: list[str],
    since: str | None,
    until: str | None,
    q: str | None,
    category: str | None,
    user_id: int,
    subcategory: str | None = None,
    account_no: str | None = None,
    card_no: str | None = None,
    card_date_basis: Literal["consume", "post"] = "consume",
) -> list[dict[str, Any]]:
    """跨銀行收集 transactions, in-memory 過濾, 後續排序+分頁.

    Phase C (2026-06-17): user_id 必填——SQL `WHERE user_id = ?` 強制只回本 user 的 row,
    擋掉 cross-tenant read。
    """
    # Phase 6 (excluded): 一次撈 excluded account_no / card_no map, 給 transform 標旗
    from backend.server.routers.portfolio import get_excluded_account_nos
    from backend.server.routers.cards import get_excluded_card_nos
    excluded_accounts_map = get_excluded_account_nos(user_id)
    excluded_cards_map = get_excluded_card_nos(user_id)

    items: list[dict[str, Any]] = []
    for bank in banks:
        bank_excluded_accounts = excluded_accounts_map.get(bank, set())
        bank_excluded_cards = excluded_cards_map.get(bank, set())
        for txn_row in db_api.list_txns_for_bank(
            bank=bank, user_id=user_id, kinds=kinds,
        ):
            if txn_row.kind == "twd":
                items.append(_twd_to_transaction(bank, txn_row, bank_excluded_accounts))
            elif txn_row.kind == "billed":
                items.append(_billed_to_transaction(bank, txn_row, bank_excluded_cards))
            elif txn_row.kind == "pending":
                items.append(_pending_to_transaction(bank, txn_row, bank_excluded_cards))

    items = [_apply_card_date_basis(t, card_date_basis) for t in items]

    # Phase 10 (2026-07-29) 分類拆帳: 已拆帳的母筆展開成 N 筆子項取代之。
    # 放在 filter 之前 — 子項各有自己的 category, 必須讓 category filter 看得到;
    # 若先 filter 再展開, 「篩餐飲」會漏掉母筆分類為日用品但有餐飲子項的交易。
    # 未拆帳 (splits=[]) 的 row 原樣通過, 完全 backward compatible。
    expanded: list[dict[str, Any]] = []
    for t in items:
        expanded.extend(_expand_splits(t))
    items = expanded

    # In-memory filter (txn 量級在 backend tests 上千都 OK; 真正大才 push to SQL)
    if since:
        items = [t for t in items if (t["date"] or "") >= since]
    if until:
        items = [t for t in items if (t["date"] or "") <= until]
    if account_no:
        items = [t for t in items if t.get("account_no") == account_no]
    if card_no:
        items = [t for t in items if t.get("card_no") == card_no]
    if q:
        ql = q.lower()
        items = [t for t in items if t["description"] and ql in t["description"].lower()]
    if category:
        # Phase 8.2 B: __null__ sentinel → 篩出未分類 (category IS NULL)
        if category == "__null__":
            items = [t for t in items if t["category"] is None or t["category"] == ""]
        else:
            items = [t for t in items if t["category"] == category]
    if subcategory:
        items = [t for t in items if t.get("subcategory") == subcategory]

    # Time desc, NULL date 沉底
    items.sort(key=lambda t: (t["date"] or "0000-00-00", t["datetime"] or ""), reverse=True)
    return items


@router.get("")
def list_transactions(
    bank: str | None = Query(None, description="銀行 code 或 comma 分隔列表 (e.g. 'hsbc,sinopac')"),
    kind: Literal["twd", "billed", "pending", "all"] = Query("all"),
    since: str | None = Query(None, description="起始日 YYYY-MM-DD (含)"),
    until: str | None = Query(None, description="結束日 YYYY-MM-DD (含)"),
    account_id: int | None = Query(None, description="指定 BankAccount id, 蓋過 bank"),
    account_no: str | None = Query(None, description="精準篩選台幣帳戶交易 (canonical account number)"),
    card_no: str | None = Query(None, description="精準篩選信用卡交易 (canonical/raw card number)"),
    q: str | None = Query(None, description="描述子字串 (case-insensitive)"),
    category: str | None = Query(None, description="分類字串"),
    subcategory: str | None = Query(None, description="子分類字串 (drill-down)"),
    direction: Literal["income", "expense", "all"] = Query(
        "all", description="只看收入 / 只看支出 / 全部 (依 amount 正負判斷)"),
    card_date_basis: Literal["consume", "post"] = Query(
        "consume", description="信用卡日期認列方式: consume=消費日, post=入帳日"),
    # Phase 9 C-2 (2026-06-19): limit 上限 1000 → 5000 配合 frontend client-side
    # filter pivot. 一個 user 一個 period 50-200 筆, 5000 cover 極端 + future-proof.
    # 超 5000 frontend 顯示「資料超量」warning 提示縮短期間.
    limit: int = Query(100, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    user: dict = Depends(current_user),
) -> dict[str, Any]:
    banks = _resolve_banks(bank, account_id, user["id"])
    kinds = ["twd", "billed", "pending"] if kind == "all" else [kind]
    items = _collect_transactions(
        banks, kinds, since, until, q, category, user_id=user["id"],
        subcategory=subcategory, account_no=account_no, card_no=card_no,
        card_date_basis=card_date_basis,
    )

    # Phase 6 (2026-06-14 PM): direction filter — 收入 / 支出 / 全部
    # 依 normalized cashflow_direction 判斷，不看 raw amount 符號。
    if direction == "income":
        items = [t for t in items if t.get("cashflow_direction") == "income"]
    elif direction == "expense":
        items = [t for t in items if t.get("cashflow_direction") == "expense"]

    # 統計 (full set, not paginated)
    by_bank: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for t in items:
        by_bank[t["bank"]] = by_bank.get(t["bank"], 0) + 1
        by_kind[t["kind"]] = by_kind.get(t["kind"], 0) + 1

    total = len(items)
    page = items[offset: offset + limit]
    return {
        "total": total,
        "items": page,
        "offset": offset,
        "limit": limit,
        "stats": {"by_bank": by_bank, "by_kind": by_kind, "banks_queried": banks},
    }


@router.get("/stats")
def transactions_stats(
    bank: str | None = Query(None),
    account_id: int | None = Query(None),
    # Phase 8.2 A 路線 (2026-06-14): chip 來源跟隨當前 filter 範圍
    kind: Literal["twd", "billed", "pending", "all"] = Query("all"),
    since: str | None = Query(None, description="起始日 YYYY-MM-DD (含)"),
    until: str | None = Query(None, description="結束日 YYYY-MM-DD (含)"),
    q: str | None = Query(None, description="描述子字串 (case-insensitive)"),
    category: str | None = Query(None, description="主類 (給 by_subcategory 限縮用)"),
    card_date_basis: Literal["consume", "post"] = Query(
        "consume", description="信用卡日期認列方式: consume=消費日, post=入帳日"),
    user: dict = Depends(current_user),
) -> dict[str, Any]:
    banks = _resolve_banks(bank, account_id, user["id"])
    kinds = ["twd", "billed", "pending"] if kind == "all" else [kind]
    params = (tuple(banks), tuple(kinds), since, until, q, category, card_date_basis)
    return get_or_set_dashboard_cache(
        "transactions.stats",
        user_id=user["id"],
        params=params,
        ttl_seconds=DEFAULT_DASHBOARD_TTL_SECONDS,
        compute=lambda: _compute_transactions_stats(
            banks=banks,
            kinds=kinds,
            since=since,
            until=until,
            q=q,
            category=category,
            card_date_basis=card_date_basis,
            user_id=user["id"],
        ),
    )


def _compute_transactions_stats(
    *,
    banks: list[str],
    kinds: list[str],
    since: str | None,
    until: str | None,
    q: str | None,
    category: str | None,
    card_date_basis: Literal["consume", "post"],
    user_id: int,
) -> dict[str, Any]:
    # Phase 8.2 鐵則: 主聚合不帶 category 才有「全部主類 chip」可選；
    # category 只用在 by_subcategory 限縮 (見下方 if t["category"] == category 判斷)
    collect_started = time.perf_counter()
    items = _collect_transaction_stat_rows(
        banks, kinds, since, until, q, user_id=user_id, card_date_basis=card_date_basis,
    )
    collect_ms = (time.perf_counter() - collect_started) * 1000
    fallback_used = False
    if not items:
        fallback_used = True
        fallback_started = time.perf_counter()
        items = _collect_transactions(
            banks, kinds, since, until, q, None, user_id=user_id, subcategory=None,
            card_date_basis=card_date_basis,
        )
        collect_ms += (time.perf_counter() - fallback_started) * 1000
    perf_log.info(
        "event=transactions.stats section=collect user_id=%s duration_ms=%.1f banks=%s kinds=%s rows=%s fallback=%s",
        user_id, collect_ms, len(banks), ",".join(kinds), len(items), fallback_used,
    )
    aggregate_started = time.perf_counter()

    by_bank: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    by_month: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_subcategory: dict[str, int] = {}  # Phase 8.2: 子分類 chip 來源 (含當前 filter)

    # L8.5 — amount sum buckets (income = positive, expense = negative)
    amount_by_month: dict[str, dict[str, int]] = {}  # {"2026-06": {income, expense, net, count}}
    amount_by_category: dict[str, int] = {}  # 純支出金額 (取絕對值)
    total_income = 0
    total_expense = 0  # 累計支出 (絕對值，正數)

    # Phase 6 (category taxonomy 2026-06-15) — 新統計:
    #   amount_by_flow_type: 看 flow_type 4 桶分佈 (expense/income/transfer/investment)
    #                        驗證 transfer/investment 是否乾淨脫離 expense KPI
    #   subscription_total : 本月訂閱合計 (Netflix/Spotify/iCloud/...)
    #   subscription_by_month: 各月訂閱金額 (用於趨勢)
    # 詳見 wiki [[personal-finance-transaction-category-taxonomy]]
    amount_by_flow_type: dict[str, int] = {
        "expense": 0, "income": 0, "transfer": 0, "investment": 0,
    }
    subscription_total = 0  # 全期訂閱金額 (絕對值)
    subscription_by_month: dict[str, int] = {}
    # Phase 7 (Income 5 類 2026-06-15) — FIRE 被動收入指標
    #   amount_by_income_category: 收入 5 enum 分桶 (salary/bonus/interest_dividend/investment_gain/other)
    #   passive_income_total:      被動收入合計 (interest_dividend + investment_gain)
    #                              FIRE 公式分子
    #   passive_income_by_month:   各月被動收入 (用於趨勢圖)
    # 詳見 wiki [[income-classifier-and-fire-passive-income-spec]]
    amount_by_income_category: dict[str, int] = {
        "salary": 0, "bonus": 0, "interest_dividend": 0,
        "investment_gain": 0, "other": 0,
    }
    income_unclassified_count = 0  # 未分類 income row 數 (UI 提示用)
    passive_income_total = 0
    passive_income_by_month: dict[str, int] = {}

    for t in items:
        # Phase 6 (excluded): 該 txn 的帳戶被標「不納入淨資產統計」→ 不算 stats
        # by_bank / by_kind / by_month 等 raw count 仍算 (frontend 可看到), 但
        # 金額類 bucket (amount_by_month / amount_by_category / total_*) 全跳過
        #
        # Phase 8.3 (2026-06-15): 加 auto_excluded — categorizer 命中標
        # auto_excluded=1 的 rule (信用卡還款/轉帳/退款/回饋) 的 row 也納入 skip。
        # 跟既有 per-account/per-card excluded 等效併用 (OR 邏輯)。
        # 注意: by_category / by_subcategory raw count 也跟著 skip (auto_excluded row
        # 不該污染 chip 列), 跟既有 excluded 不同 —— 後者只 skip 金額不 skip count,
        # 因為 per-account excluded 是「整個帳戶不看」, 而 auto_excluded 是「這筆 by
        # definition 不算收支」, 但這筆 row 還是要在 list 看得到 (反灰), 統計類
        # bucket 全 skip。
        is_excluded = bool(_item_get(t, "excluded")) or bool(_item_get(t, "auto_excluded"))
        bank_key = _item_get(t, "bank")
        kind_key = _item_get(t, "kind")
        by_bank[bank_key] = by_bank.get(bank_key, 0) + 1
        by_kind[kind_key] = by_kind.get(kind_key, 0) + 1
        m = (_normalize_date(_item_get(t, "date")) or "")[:7]
        if m:
            by_month[m] = by_month.get(m, 0) + 1
        # by_category / by_subcategory chip count: auto_excluded row 不計入
        # (避免「還款 9 筆」chip 仍出現)
        if not is_excluded:
            category_value = _item_get(t, "category")
            if category_value:
                by_category[category_value] = by_category.get(category_value, 0) + 1
            else:
                # Phase 8.2 B: 未分類 (NULL/"") 用 __null__ sentinel key 暴露給 frontend chip
                by_category["__null__"] = by_category.get("__null__", 0) + 1
            # Phase 8.2: subcategory 統計 (chip 來源用)
            # 鐵則: 只 aggregate 屬於當前 category filter 的 row, 否則子類 chip 會跨主類混雜
            sub = _item_get(t, "subcategory")
            if sub and (category is None or category_value == category):
                by_subcategory[sub] = by_subcategory.get(sub, 0) + 1

        if is_excluded:
            continue

        cashflow_direction = _item_get(t, "cashflow_direction") or _stat_cashflow_direction(t)
        cashflow_amount = _item_get(t, "cashflow_amount")
        if not isinstance(cashflow_amount, (int, float)):
            cashflow_amount = _stat_cashflow_amount(t)
        cashflow_amount = int(cashflow_amount)
        abs_cashflow = abs(cashflow_amount)
        flow_type = _item_get(t, "flow_type")
        if flow_type in amount_by_flow_type:
            amount_by_flow_type[flow_type] += abs_cashflow
        # Phase 6 (taxonomy) subscription aggregate
        if _item_get(t, "is_subscription") and cashflow_direction == "expense":
            v = abs_cashflow
            subscription_total += v
            if m:
                subscription_by_month[m] = subscription_by_month.get(m, 0) + v
        # Phase 7 (Income 5 類) income_category aggregate (FIRE 指標基礎)
        # 鐵則: persisted taxonomy 與使用者視角方向都必須是 income。任一 writer
        # 誤標（例如「放款利息」支出）都 fail closed，不得進收入或 FIRE 分子。
        if flow_type == "income" and cashflow_direction == "income":
            ic = _item_get(t, "income_category")
            if ic in amount_by_income_category:
                v = abs_cashflow
                amount_by_income_category[ic] += v
                # 被動收入兩類 (interest_dividend + investment_gain)
                if ic in ("interest_dividend", "investment_gain"):
                    passive_income_total += v
                    if m:
                        passive_income_by_month[m] = (
                            passive_income_by_month.get(m, 0) + v
                        )
            else:
                # ic is None / 未分類 / 信用卡 refund row → 不算進 5 類但記 unclassified count
                income_unclassified_count += 1
        if m:
            bucket = amount_by_month.setdefault(
                m, {"income": 0, "expense": 0, "net": 0, "count": 0},
            )
            bucket["count"] += 1
            if cashflow_direction == "income":
                v = abs_cashflow
                bucket["income"] += v
                bucket["net"] += v
                total_income += v
            elif cashflow_direction == "expense":
                v = abs_cashflow
                bucket["expense"] += v
                bucket["net"] -= v
                total_expense += v
        # 分類統計只看「真正的支出」(expense 性質), 排掉 income/payment
        category_value = _item_get(t, "category")
        if category_value and cashflow_direction == "expense":
            amount_by_category[category_value] = (
                amount_by_category.get(category_value, 0) + abs_cashflow
            )

    aggregate_ms = (time.perf_counter() - aggregate_started) * 1000
    perf_log.info(
        "event=transactions.stats section=aggregate user_id=%s duration_ms=%.1f rows=%s months=%s categories=%s income=%s expense=%s",
        user_id, aggregate_ms, len(items), len(amount_by_month), len(amount_by_category), total_income, total_expense,
    )
    return {
        "total": len(items),
        "by_bank": by_bank,
        "by_kind": by_kind,
        "by_month": dict(sorted(by_month.items(), reverse=True)),
        "by_category": dict(sorted(by_category.items(), key=lambda kv: -kv[1])),
        # Phase 8.2 A: 子分類 chip 來源 — 按筆數降冪
        "by_subcategory": dict(sorted(by_subcategory.items(), key=lambda kv: -kv[1])),
        "banks_queried": banks,
        # L8.5 新增 — 金額統計
        "amount_by_month": dict(sorted(amount_by_month.items(), reverse=True)),
        "amount_by_category": dict(sorted(amount_by_category.items(), key=lambda kv: -kv[1])),
        "total_income": total_income,
        "total_expense": total_expense,
        "total_net": total_income - total_expense,
        # Phase 6 (category taxonomy) 新增 — flow_type 分桶 + 訂閱統計
        "amount_by_flow_type": amount_by_flow_type,
        "subscription_total": subscription_total,
        "subscription_by_month": dict(sorted(subscription_by_month.items(), reverse=True)),
        # Phase 7 (Income 5 類) 新增 — FIRE 被動收入指標
        "amount_by_income_category": amount_by_income_category,
        "passive_income_total": passive_income_total,
        "passive_income_by_month": dict(sorted(passive_income_by_month.items(), reverse=True)),
        "passive_income_pct": (
            round(passive_income_total / total_income * 100, 1)
            if total_income > 0 else 0.0
        ),
        "income_unclassified_count": income_unclassified_count,
    }


# ============================================================
# 單筆 detail + category 編輯 (L8.5)
# ============================================================

# 三種 table 對應的 sql table 名跟 transform fn
_KIND_TO_TABLE = {
    "twd": ("twd_transactions", _twd_to_transaction),
    "billed": ("card_billed_txns", _billed_to_transaction),
    "pending": ("card_pending_txns", _pending_to_transaction),
}


def _assert_bank_ownership(user_id: int, bank: str) -> None:
    """確認該 user 確實擁有該 bank 的 account, 否則 403.

    避免 user A 透過 GET /transactions/hsbc/billed/123 偷看 user B 同銀行的 row。
    (對 multi-user 部署是必要; single-user 也保留以求 defense-in-depth)
    """
    repo = AccountsRepo()
    accts = repo.list_for_user(user_id)
    if not any(a.bank == bank for a in accts):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"您沒有此銀行 ({bank}) 的帳號",
        )


@router.get("/{bank}/{kind}/{txn_id}")
def get_transaction_detail(
    bank: str,
    kind: Literal["twd", "billed", "pending"],
    txn_id: int,
    user: dict = Depends(current_user),
) -> dict[str, Any]:
    """單筆交易完整 detail (含 raw row, 給 frontend modal 編輯用)."""
    if bank not in KNOWN_BANKS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"不支援的銀行: {bank}")
    if kind not in _KIND_TO_TABLE:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"不支援的 kind: {kind}")

    _assert_bank_ownership(user["id"], bank)

    _table, transform = _KIND_TO_TABLE[kind]
    row = db_api.get_txn(bank=bank, kind=kind, txn_id=txn_id, user_id=user["id"])
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "找不到此筆交易")
    return transform(bank, row)


def _mutate_hashtag(
    user_id: int,
    banks: set[str],
    old_name: str,
    new_name: str | None,
) -> int:
    changed = 0
    committed: list[tuple[str, list[dict[str, Any]]]] = []
    try:
        for bank in banks:
            if bank not in KNOWN_BANKS:
                continue
            try:
                with db_api.transaction(bank=bank) as tx:
                    tx_any: Any = tx
                    snapshot = tx_any.tag_snapshot(user_id=user_id, name=old_name)
                    bank_changed = tx_any.replace_tag(
                        user_id=user_id, old_name=old_name, new_name=new_name,
                    )
                if snapshot:
                    committed.append((bank, snapshot))
                changed += bank_changed
            except BankNotAvailable:
                continue
        return changed
    except Exception as mutation_error:
        rollback_errors: list[str] = []
        for bank, snapshot in reversed(committed):
            try:
                with db_api.transaction(bank=bank) as tx:
                    tx_any: Any = tx
                    tx_any.restore_tag_snapshot(user_id=user_id, snapshots=snapshot)
            except Exception as rollback_error:  # pragma: no cover
                rollback_errors.append(f"{bank}: {rollback_error}")
        if rollback_errors:
            raise RuntimeError(
                "hashtag mutation failed and rollback was incomplete: "
                + "; ".join(rollback_errors),
            ) from mutation_error
        raise


@router.get("/tags/popular")
def list_popular_tags(
    sort: str = Query("count", description="排序: count (預設, 用次數降冪) 或 recent (最近一次掛上的日期降冪)"),
    user: dict = Depends(current_user),
) -> dict[str, list[dict[str, Any]]]:
    """跨所有 user 擁有的 bank × 3 table aggregate tags_overwrite, 給 frontend tag picker.

    回傳: {"tags": [{"name": str, "count": int, "last_used": str | None}, ...]}
      - name: tag 文字
      - count: 跨所有 row 出現次數 (同 row 內重複算一次, 因為 _normalize_tags_input 已 dedup)
      - last_used: 最近一次掛此 tag 的 row 的 date (ISO YYYY-MM-DD), 沒日期就 None
      - sort=count: 預設, 由 count desc → last_used desc → name asc
      - sort=recent: 由 last_used desc (None 排最後) → count desc → name asc

    使用情境: 編輯交易時 frontend 開 tag picker, 列使用者過去用過的 tag 讓人選 + 搜尋,
    避免每次重打 / 同義字打不齊 (「日本旅遊」vs「日本旅行」)。
    """
    if sort not in ("count", "recent"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "sort 只支援 count / recent")

    # 該 user 擁有的 bank list
    repo = AccountsRepo()
    user_banks = {a.bank for a in repo.list_for_user(user["id"])}
    if not user_banks:
        return {"tags": []}

    # tag → (count, last_used_iso_or_none)
    agg: dict[str, dict[str, Any]] = {}

    for bank in user_banks:
        if bank not in KNOWN_BANKS:
            continue
        for tag_row in db_api.list_tag_aggregates_for_bank(
            bank=bank, user_id=user["id"],
        ):
            tags = _parse_tags_overwrite(tag_row.tags_overwrite)
            if not tags:
                continue
            date_iso = _normalize_date(tag_row.date)
            for tag in tags:
                entry = agg.setdefault(tag, {"count": 0, "last_used": None})
                entry["count"] += 1
                if date_iso and (entry["last_used"] is None or date_iso > entry["last_used"]):
                    entry["last_used"] = date_iso

    # 組 list + sort
    items = [
        {"name": name, "count": v["count"], "last_used": v["last_used"]}
        for name, v in agg.items()
    ]
    if sort == "recent":
        items.sort(key=lambda x: (
            x["last_used"] is None,           # None 排最後 (False<True)
            # last_used desc — 用反向 string
            "" if x["last_used"] is None else "".join(chr(255 - ord(c)) for c in x["last_used"]),
            -x["count"],
            x["name"],
        ))
    else:  # count
        items.sort(key=lambda x: (
            -x["count"],
            x["last_used"] is None,
            "" if x["last_used"] is None else "".join(chr(255 - ord(c)) for c in x["last_used"]),
            x["name"],
        ))
    return {"tags": items}


@router.put("/tags", response_model=HashtagMutationOut)
def rename_hashtag(
    body: HashtagRenameIn,
    user: dict = Depends(current_user),
) -> dict:
    new_name = body.name.strip()
    if not new_name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "hashtag name cannot be blank")
    if body.old_name == new_name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "hashtag name is unchanged")
    banks = {account.bank for account in AccountsRepo().list_for_user(user["id"])}
    changed = _mutate_hashtag(user["id"], banks, body.old_name, new_name)
    if changed == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"hashtag {body.old_name!r} not found")
    return {
        "hashtag": body.old_name,
        "renamed_to": new_name,
        "transactions_updated": changed,
    }


@router.delete("/tags", response_model=HashtagMutationOut)
def delete_hashtag(
    body: HashtagDeleteIn,
    user: dict = Depends(current_user),
) -> dict:
    banks = {account.bank for account in AccountsRepo().list_for_user(user["id"])}
    changed = _mutate_hashtag(user["id"], banks, body.name, None)
    if changed == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"hashtag {body.name!r} not found")
    return {
        "hashtag": body.name,
        "renamed_to": None,
        "transactions_updated": changed,
    }


@router.patch("/{bank}/{kind}/{txn_id}")
def update_transaction(
    bank: str,
    kind: Literal["twd", "billed", "pending"],
    txn_id: int,
    body: dict[str, Any],
    user: dict = Depends(current_user),
) -> dict[str, Any]:
    """改單筆交易的 category / subcategory (其他欄位禁改保護 raw data)."""
    if bank not in KNOWN_BANKS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"不支援的銀行: {bank}")
    if kind not in _KIND_TO_TABLE:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"不支援的 kind: {kind}")

    # 白名單欄位 (Phase 8.2: 開放 category + subcategory + Phase 9: tags +
    # Phase 9.3 2026-06-17: auto_excluded — 使用者可手動勾「忽略這筆 / 不納入收支統計」.
    # 共用 rule auto_excluded 同欄, 不額外設 manual_excluded — 統計層只看一個 flag.)
    ALLOWED = {"category", "subcategory", "description_overwrite", "tags", "tags_mode", "auto_excluded", "splits"}
    unknown = set(body.keys()) - ALLOWED
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"不支援編輯的欄位: {sorted(unknown)} (只允許 {sorted(ALLOWED)})",
        )
    if not body:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "請至少提供一個欄位")

    _assert_bank_ownership(user["id"], bank)

    _table, transform = _KIND_TO_TABLE[kind]

    # Validate tags_mode separately (it must accompany tags, not stand alone)
    if "tags_mode" in body and "tags" not in body:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "tags_mode 只在送 tags 時有效",
        )
    if "tags_mode" in body and body["tags_mode"] not in ("replace", "add"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "tags_mode 只支援 replace / add",
        )

    # Normalize/validate fields before delegating to db_facade
    update_kwargs: dict[str, Any] = {}
    if "category" in body:
        update_kwargs["category"] = body.get("category")
    if "subcategory" in body:
        update_kwargs["subcategory"] = body.get("subcategory")
    if "description_overwrite" in body:
        update_kwargs["description_overwrite"] = body.get("description_overwrite")
    if "tags" in body:
        try:
            update_kwargs["tags"] = _normalize_tags_input(body.get("tags"))
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None
        update_kwargs["tags_mode"] = body.get("tags_mode", "replace")
    if "auto_excluded" in body:
        raw = body.get("auto_excluded")
        if isinstance(raw, bool):
            update_kwargs["auto_excluded"] = raw
        elif raw in (0, 1):
            update_kwargs["auto_excluded"] = bool(raw)
        else:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "auto_excluded 只接受 true/false",
            )

    if "splits" in body:
        # Phase 10 (2026-07-29) 分類拆帳: 子項總和必須等於母筆金額, 所以要先讀母筆。
        # 用 transform 後的 cashflow_amount 而非 raw amount — 信用卡 raw 是帳單視角
        # (消費為正), transform 後才是使用者視角的絕對值金額, 跟 UI 顯示的數字一致,
        # 使用者在畫面上看到 1200 就該填 1200。
        parent_row = db_api.get_txn(
            bank=bank, kind=kind, txn_id=txn_id, user_id=user["id"],
        )
        if parent_row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "找不到此筆交易")
        parent = transform(bank, parent_row)
        parent_amount = abs(int(parent.get("cashflow_amount") or 0))
        if parent_amount <= 0:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "此筆交易金額為 0 或無法認列現金流, 無法拆帳",
            )
        try:
            update_kwargs["splits"] = _normalize_splits_input(
                body.get("splits"), parent_amount,
            )
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None

    try:
        with db_api.transaction(bank=bank) as tx:
            result = tx.update_txn(
                kind=kind,
                txn_id=txn_id,
                user_id=user["id"],
                **update_kwargs,
            )
    except BankNotAvailable as e:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"找不到資料 (bank={e.bank})",
        ) from e
    except TxnNotFound as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "找不到此筆交易") from e
    except TxnColumnMissing as e:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"此銀行 ({e.bank}) 的 {e.table} 缺 {e.column} 欄, 請先升級",
        ) from e

    return transform(bank, result)



