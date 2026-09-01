"""聯邦銀行 (UBOT) persist 邏輯.

從 persist.py 拆分而來 (2026-06-17, W persist.py split refactor)。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import re
from zoneinfo import ZoneInfo

from backend.banks.ubot import UbotCrawler
from backend.core import account_classify, classify
from backend.core.base import validate_history_coverage
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

def _today() -> date:
    return datetime.now(ZoneInfo("Asia/Taipei")).date()


def _iso_date(value, error: str) -> date:
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise ValueError(error)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(error) from None


def _validate_ubot_history(data: dict, store: BankStore) -> date:
    error = "invalid UBOT history coverage"
    coverage = data.get("history_coverage")
    if not isinstance(coverage, dict):
        raise ValueError(error)
    mode = coverage.get("mode")
    if mode not in {"full", "incremental"}:
        raise ValueError(error)
    as_of = _iso_date(coverage.get("as_of"), error)
    today = _today()
    if as_of > today or (today - as_of).days > 1:
        raise ValueError(error)
    validate_history_coverage(
        coverage, expected_mode=mode,
        expected_domains=frozenset({"twd_transactions"}),
    )
    try:
        encoded = json.dumps(
            {
                "history_coverage": coverage,
                "debit_accounts": data.get("debit_accounts"),
                "twd_txns": data.get("twd_txns"),
            },
            ensure_ascii=False, separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError, RecursionError):
        raise ValueError(error) from None
    if len(encoded) > 5_000_000:
        raise ValueError(error)

    domain = coverage["domains"][0]
    expected = domain["expected"]
    windows = domain["windows"]
    expected_by_identity = {item["identity"]: item for item in expected}
    inventory = data.get("debit_accounts")
    if (
        not expected or len(expected_by_identity) != len(expected)
        or not isinstance(inventory, list) or len(inventory) != len(expected)
    ):
        raise ValueError(error)
    inventory_ids = set()
    labels = set()
    account_pattern = re.compile(r"(?<!\d)(\d{3})-(\d{2})-(\d{7})(?!\d)")
    for item in inventory:
        if not isinstance(item, dict) or set(item) != {"label", "identity", "currency"}:
            raise ValueError(error)
        label = item.get("label")
        identity = item.get("identity")
        matches = account_pattern.findall(label) if isinstance(label, str) else []
        if (
            not isinstance(label, str) or not label or label != label.strip()
            or not isinstance(identity, str) or re.fullmatch(r"\d{12}", identity) is None
            or len(matches) != 1 or "".join(matches[0]) != identity
            or item.get("currency") != "TWD"
            or identity in inventory_ids or label in labels
        ):
            raise ValueError(error)
        inventory_ids.add(identity)
        labels.add(label)
    if inventory_ids != set(expected_by_identity):
        raise ValueError(error)

    results = data.get("twd_txns")
    if not isinstance(results, list) or len(results) != len(windows):
        raise ValueError(error)
    receipts = []
    total_rows = 0
    row_keys = {
        "AccountDate", "Balance", "Expenditure", "Income", "PS", "Summary",
        "TraDate", "TraSum", "TraTime",
    }
    for result in results:
        if not isinstance(result, dict) or set(result) != {
            "Account", "NTDetailList", "NTTotal", "receipt",
        }:
            raise ValueError(error)
        identity = result.get("Account")
        rows = result.get("NTDetailList")
        receipt = result.get("receipt")
        if (
            identity not in expected_by_identity or not isinstance(rows, list)
            or not isinstance(result.get("NTTotal"), dict)
            or UbotCrawler._nttotal_claims_more_pages(result["NTTotal"])
            or not isinstance(receipt, dict)
            or set(receipt) != {"identity", "start", "end", "status", "pages", "rows"}
            or receipt.get("identity") != identity
            or type(receipt.get("pages")) is not int or receipt["pages"] != 1
            or type(receipt.get("rows")) is not int or receipt["rows"] != len(rows)
            or receipt.get("status") not in {"complete", "explicit_empty"}
            or (receipt["status"] == "explicit_empty" and rows)
            or (receipt["status"] == "complete" and not rows)
        ):
            raise ValueError(error)
        start = _iso_date(receipt["start"], error)
        end = _iso_date(receipt["end"], error)
        if start > end or (start.year, start.month) != (end.year, end.month):
            raise ValueError(error)
        total_rows += len(rows)
        if total_rows > 50_000:
            raise ValueError(error)
        for row in rows:
            if not isinstance(row, dict) or set(row) != row_keys:
                raise ValueError(error)
            try:
                transacted = UbotCrawler._strict_slash_date(row["TraDate"], error)
                UbotCrawler._strict_slash_date(row["AccountDate"], error)
                if not start <= transacted <= end:
                    raise RuntimeError(error)
                if re.fullmatch(r"\d{2}:\d{2}:\d{2}", row["TraTime"]) is None:
                    raise RuntimeError(error)
                datetime.strptime(row["TraTime"], "%H:%M:%S")
                if any(not isinstance(row[key], str) or len(row[key]) > 2_000
                       for key in ("Summary", "TraSum", "PS")):
                    raise RuntimeError(error)
                for key in ("Expenditure", "Income", "Balance"):
                    UbotCrawler._strict_twd_amount(row[key], error)
                if row["Expenditure"] not in {"", "-"} and row["Income"] not in {"", "-"}:
                    raise RuntimeError(error)
            except (RuntimeError, ValueError):
                raise ValueError(error) from None

        receipts.append({key: receipt[key] for key in (
            "identity", "start", "end", "status", "pages",
        )})
    if receipts != windows:
        raise ValueError(error)

    existing = store.latest_twd_transaction_dates()
    floor = UbotCrawler._history_floor(as_of)
    for identity, item in expected_by_identity.items():
        start = _iso_date(item["start"], error)
        end = _iso_date(item["end"], error)
        cursor = existing.get(identity)
        if isinstance(cursor, date) and cursor > as_of:
            raise ValueError(error)
        expected_start = floor
        if mode == "incremental" and isinstance(cursor, date):
            expected_start = max(floor, (cursor - timedelta(days=7)).replace(day=1))
        if start != expected_start or end != as_of:
            raise ValueError(error)
        actual = [
            (_iso_date(window["start"], error), _iso_date(window["end"], error))
            for window in windows if window["identity"] == identity
        ]
        if actual != UbotCrawler._history_windows(start, end):
            raise ValueError(error)
    return as_of


def _persist_ubot(
    data: dict, store: BankStore, rules: list[dict] | None = None, *, as_of: date,
) -> dict:
    """聯邦 collect() 結構 → store 7 表增量。

    映射：
      deposit_twd.NTList   → accounts(UPSERT) + balance_history(每日快照)
      twd_txns[].NTDetailList → twd_transactions(append-only)
      card_summary/card_limit → daily_metrics
      card_billed[].CardList  → card_billed_txns(append-only)
      card_unbilled.CardList  → card_pending_txns(refresh 'unbilled')
    """
    today = as_of.isoformat()
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
        store.upsert_accounts(accts, commit=False)
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
            }], commit=False)
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
        twd_new += store.upsert_twd_txns(rows, rules=rules, commit=False)
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
                "post_date": _ubot_date(t.get("postDate")),     # 入帳日（缺值保留 NULL）
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
    unb_raw = data.get("card_unbilled")
    unb = unb_raw if isinstance(unb_raw, dict) else {}
    unb_rows = []
    if isinstance(unb, dict):
        for t in unb.get("CardList", []):
            amt = _num(t.get("txAmt"))
            desc = (t.get("txDesc") or "").strip()
            unb_rows.append({
                "card_no": t.get("cardNo"),
                "date": _ubot_date(t.get("effectiveDate")),
                "post_date": _ubot_date(t.get("postingDate") or t.get("postDate")),
                "desc": desc, "amount": amt,
                "currency": "TWD",  # amount=txAmt 是台幣估算/入帳金額
                "consume_currency": (t.get("Currency") or "").strip() or "TWD",
                "consume_amount": _num_real(t.get("oriAmt")),
                "txn_type": classify.classify_ubot(t.get("txCode"), desc, amt),
            })
    # fetch_ok: card_unbilled 要是 dict 才算真的抓到未出帳清單。非 dict (None/缺 key)
    # = API 沒回, 此時 unb_rows 空是「假消失」, 不可做消失比對。
    delta["card_unbilled"] = store.refresh_card_pending(
        "unbilled", unb_rows, rules=rules,
        fetch_ok=(isinstance(unb_raw, dict)
                  and not any(unb_raw.get(key) for key in ("error", "Error", "errorMessage"))
                  and isinstance(unb_raw.get("CardList"), list)),
        commit=False,
    )
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
    ubot_available = _num_to_float(cl_summary.get("avalCrLmt"))
    if ubot_available is None:
        ubot_available = _num_to_float(cs_summary.get("avalCrLmt"))
    # 聯邦的 unsettleAmt 是未結算交易，不是實際佔用額度；額度口徑應直接用
    # 銀行提供的「總額度 - 可用額度」，並保留溢繳造成的負數。
    ubot_used = (
        ubot_credit_limit - ubot_available
        if ubot_credit_limit is not None and ubot_available is not None
        else _num_to_float(cl_summary.get("unsettleAmt"))
    )
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

    shared_card_fields = {
        "credit_limit": ubot_credit_limit,
        "used_credit": ubot_used,
        "statement_close_date": ubot_stmt,
        "payment_due_date": ubot_due,
        "bill_due_amount": ubot_bill_due,
        "last_payment_amount": ubot_last_pay_amt,
        "last_payment_date": ubot_last_pay_date,
    }
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
                **shared_card_fields,
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
                **shared_card_fields,
            }
    # card_limit/card_summary 是整戶資料；即使某張既有卡本期沒有交易，也必須更新，
    # 否則每張卡會停在各自最後一次出現於明細時的歷史額度。
    for row in store.list_cards():
        if row["card_no"] in seen_ubot_cards:
            continue
        seen_ubot_cards[row["card_no"]] = {
            "number": row["card_no"],
            "name": row["name"],
            "association": row["association"],
            "type": row["type"],
            "is_cube": bool(row["is_cube"]),
            "active": bool(row["active"]),
            **shared_card_fields,
        }
    if seen_ubot_cards:
        store.upsert_cards(list(seen_ubot_cards.values()), commit=False)

    # --- 每日數值快照：信用卡彙總/額度、投資 ---
    cs = data.get("card_summary")
    if cs:
        store.put_daily_metric("card_summary", cs, today, commit=False)
    cl = data.get("card_limit")
    if cl:
        store.put_daily_metric("card_limit", cl, today, commit=False)
    # 2026-06-22 v3: F0801001 raw 留底, 明早 sync 後可從 daily_metrics 撈出來
    # 看真實 shape, 調 persist mapping. 即使上面 PayList parse 失敗也保得到.
    pay_hist = data.get("card_pay_history")
    if pay_hist:
        store.put_daily_metric("card_pay_history", pay_hist, today, commit=False)
    if isinstance(dt, dict):
        twd_total_metric = _num((dt.get("TotalData") or {}).get("Deposit"))
        if twd_total_metric is not None:
            store.put_daily_metric(
                "balance_latest", {"twd": twd_total_metric}, today, commit=False,
            )
    inv = data.get("investment")
    if inv:
        store.put_daily_metric("investment", inv, today, commit=False)

    store.log_sync(delta, commit=False)
    return delta


def persist_ubot(
    data: dict,
    store: BankStore,
    rules: list[dict] | None = None,
    *,
    commit: bool = True,
) -> dict:
    """Persist one validated UBOT collection atomically."""
    as_of = _validate_ubot_history(data, store)
    if not commit:
        return _persist_ubot(data, store, rules=rules, as_of=as_of)
    try:
        delta = _persist_ubot(data, store, rules=rules, as_of=as_of)
        store.commit()
        return delta
    except Exception:
        store.conn.rollback()
        raise
