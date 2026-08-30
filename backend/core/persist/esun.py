"""玉山銀行 (E.SUN) persist 邏輯.

從 persist.py 拆分而來 (2026-06-17, W persist.py split refactor)。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import re

from backend.core import account_classify, classify
from backend.core.store import BankStore
from backend.core.persist._common import _num_real, _num_to_float, _roc_to_west

_PG_INTEGER_MAX = 2_147_483_647
_PG_INTEGER_MIN = -2_147_483_648
_TWD_INTEGER_TOKEN = re.compile(r"-?(?:0|[1-9]\d*|[1-9]\d{0,2}(?:,\d{3})+)")


def _esun_twd_integer(raw, *, non_negative: bool) -> int | None:
    if raw is None:
        return None
    if not isinstance(raw, (str, int)) or isinstance(raw, bool):
        raise ValueError("invalid E.SUN TWD transaction amount")
    if isinstance(raw, str) and _TWD_INTEGER_TOKEN.fullmatch(raw) is None:
        raise ValueError("invalid E.SUN TWD transaction amount")
    try:
        value = Decimal(str(raw).replace(",", ""))
    except (InvalidOperation, ValueError):
        raise ValueError("invalid E.SUN TWD transaction amount") from None
    if (
        not value.is_finite()
        or value != value.to_integral_value()
        or not _PG_INTEGER_MIN <= value <= _PG_INTEGER_MAX
        or non_negative and value < 0
    ):
        raise ValueError("invalid E.SUN TWD transaction amount")
    return int(value)


def _validated_esun_twd_row(row: dict) -> dict:
    account_no = row.get("account_no")
    txn_datetime = row.get("datetime")
    account_date = row.get("account_date")
    if (
        not isinstance(account_no, str)
        or re.fullmatch(r"\d{13}", account_no) is None
        or not isinstance(txn_datetime, str)
        or not isinstance(account_date, str)
        or not isinstance(row.get("desc"), str)
        or not row["desc"].strip()
    ):
        raise ValueError("invalid E.SUN TWD transaction row")
    txn_format = "%Y-%m-%d %H:%M:%S" if " " in txn_datetime else "%Y-%m-%d"
    try:
        parsed_txn = datetime.strptime(txn_datetime, txn_format)
        parsed_account = datetime.strptime(account_date, "%Y-%m-%d")
    except ValueError:
        raise ValueError("invalid E.SUN TWD transaction date") from None
    if (
        parsed_txn.strftime(txn_format) != txn_datetime
        or parsed_account.strftime("%Y-%m-%d") != account_date
    ):
        raise ValueError("noncanonical E.SUN TWD transaction date")
    money = (row.get("expend"), row.get("income"))
    if sum(value is not None for value in money) != 1:
        raise ValueError("invalid E.SUN TWD transaction direction")
    for field in ("expend", "income", "balance"):
        value = row.get(field)
        if value is None and field != "balance":
            continue
        row[field] = _esun_twd_integer(value, non_negative=field != "balance")
    return row


def _parse_esun_twd_txn_results(results: list[dict]) -> list[dict]:
    """Parse FAO01002 rows only when DOM cell boundaries were preserved."""
    out: list[dict] = []
    date_token = r"\*?20\d{2}/\d{1,2}/\d{1,2}"
    time_token = r"\d{1,2}:\d{2}:\d{2}"
    for result in results or []:
        if not isinstance(result, dict):
            raise ValueError("invalid E.SUN TWD transaction result")
        account_no = result.get("account_no")
        if not isinstance(account_no, str) or re.fullmatch(r"\d{13}", account_no) is None:
            raise ValueError("invalid E.SUN TWD transaction account")
        snapshot = result.get("snapshot")
        if not isinstance(snapshot, dict):
            raise ValueError("invalid E.SUN TWD transaction snapshot")
        has_grid = snapshot.get("hasGrid")
        grid_rows = snapshot.get("gridRows")
        grid_text = snapshot.get("gridText")
        if has_grid is False:
            if grid_rows != [] or grid_text != "":
                raise ValueError("inconsistent empty E.SUN TWD structured rows")
            continue
        if has_grid is not True or not isinstance(grid_rows, list) or not grid_rows:
            raise ValueError("invalid E.SUN TWD structured rows")
        for cells in grid_rows:
            if (
                not isinstance(cells, list)
                or not all(isinstance(cell, str) and cell == cell.strip() for cell in cells)
            ):
                raise ValueError("invalid E.SUN TWD structured row")
            combined = re.fullmatch(rf"({date_token})\s+({time_token})", cells[0]) if cells else None
            if combined:
                txn_date, txn_time = combined.groups()
                account_date = txn_date
                desc_index = 1
            elif len(cells) >= 6 and re.fullmatch(date_token, cells[0]) and re.fullmatch(time_token, cells[1]):
                txn_date, txn_time = cells[0], cells[1]
                account_date = txn_date
                desc_index = 2
            elif len(cells) >= 6 and re.fullmatch(date_token, cells[0]) and re.fullmatch(date_token, cells[1]):
                txn_date, txn_time = cells[0], None
                account_date = cells[1]
                desc_index = 2
            else:
                raise ValueError("invalid E.SUN TWD structured date columns")
            if len(cells) < desc_index + 4:
                raise ValueError("invalid E.SUN TWD structured columns")
            desc = cells[desc_index]
            expend_raw = cells[desc_index + 1] or None
            income_raw = cells[desc_index + 2] or None
            balance_raw = cells[desc_index + 3]
            if not desc or (expend_raw is None) == (income_raw is None) or not balance_raw:
                raise ValueError("invalid E.SUN TWD structured money columns")
            parsed_date = datetime.strptime(txn_date.removeprefix("*"), "%Y/%m/%d")
            parsed_account_date = datetime.strptime(account_date.removeprefix("*"), "%Y/%m/%d")
            if txn_time is not None:
                parsed_time = datetime.strptime(txn_time, "%H:%M:%S")
                txn_datetime = f"{parsed_date:%Y-%m-%d} {parsed_time:%H:%M:%S}"
            else:
                txn_datetime = f"{parsed_date:%Y-%m-%d}"
            memo = " ".join(cell for cell in cells[desc_index + 4:] if cell).strip() or None
            counterparty_bank = None
            counterparty_acct = None
            if memo:
                bank_match = re.match(r"(.+?銀行)(.*)", memo)
                if bank_match:
                    counterparty_bank = bank_match.group(1)
                    counterparty_acct = bank_match.group(2).strip() or None
                elif re.search(r"\d", memo) and "/" in memo:
                    counterparty_acct = memo
            out.append(_validated_esun_twd_row({
                "account_no": account_no,
                "datetime": txn_datetime,
                "account_date": f"{parsed_account_date:%Y-%m-%d}",
                "desc": desc,
                "expend": _esun_twd_integer(expend_raw, non_negative=True),
                "income": _esun_twd_integer(income_raw, non_negative=True),
                "balance": _esun_twd_integer(balance_raw, non_negative=False),
                "counterparty_bank": counterparty_bank,
                "counterparty_acct": counterparty_acct,
                "memo": memo,
            }))
    return out


def _validated_esun_twd_results_for_coverage(data: dict) -> list[dict]:
    coverage = data.get("history_coverage")
    results = data.get("twd_txn_results")
    if not isinstance(coverage, dict) or not isinstance(results, list):
        raise ValueError("invalid E.SUN TWD history result coverage")
    domains = coverage.get("domains")
    if not isinstance(domains, list) or len(domains) != 1:
        raise ValueError("invalid E.SUN TWD history result coverage")
    domain = domains[0]
    if not isinstance(domain, dict) or domain.get("domain") != "twd_transactions":
        raise ValueError("invalid E.SUN TWD history result coverage")
    expected = domain.get("expected")
    windows = domain.get("windows")
    if not isinstance(expected, list) or not isinstance(windows, list):
        raise ValueError("invalid E.SUN TWD history result coverage")
    expected_ids = [item.get("identity") for item in expected if isinstance(item, dict)]
    window_ids = [item.get("identity") for item in windows if isinstance(item, dict)]
    result_ids = [item.get("account_no") for item in results if isinstance(item, dict)]
    if not expected_ids:
        empty_window = domain.get("empty_window")
        if (
            expected != []
            or windows != []
            or results != []
            or not isinstance(empty_window, dict)
            or empty_window.get("status") != "explicit_empty"
            or empty_window.get("pages") != 1
        ):
            raise ValueError("invalid E.SUN TWD zero-identity coverage")
        return []
    if (
        len(expected_ids) != len(expected)
        or len(set(expected_ids)) != len(expected_ids)
        or len(window_ids) != len(windows)
        or len(set(window_ids)) != len(window_ids)
        or len(result_ids) != len(results)
        or len(set(result_ids)) != len(result_ids)
        or set(expected_ids) != set(window_ids)
        or set(expected_ids) != set(result_ids)
    ):
        raise ValueError("mismatched E.SUN TWD history identities")
    expected_by_id = {item["identity"]: item for item in expected}
    result_by_id = {item["account_no"]: item for item in results}
    parsed: list[dict] = []
    for window in windows:
        identity = window["identity"]
        result = result_by_id[identity]
        snapshot = result.get("snapshot")
        expected_window = expected_by_id[identity]
        if (
            not isinstance(snapshot, dict)
            or window.get("pages") != 1
            or window.get("start") != expected_window.get("start")
            or window.get("end") != expected_window.get("end")
            or result.get("start") != window.get("start")
            or result.get("end") != window.get("end")
            or result.get("status") != window.get("status")
        ):
            raise ValueError("invalid E.SUN TWD history result coverage")
        if snapshot.get("busy") is not False or snapshot.get("evidenceFresh") is not True:
            raise ValueError("invalid E.SUN TWD result state")
        pager = snapshot.get("pager")
        if (
            not isinstance(pager, dict)
            or set(pager) != {"present", "actionableNext"}
            or pager.get("present") is not False
            or type(pager.get("actionableNext")) is not int
            or pager["actionableNext"] != 0
        ):
            raise ValueError("invalid E.SUN TWD history pagination")
        status = window.get("status")
        window_start = date.fromisoformat(window["start"])
        window_end = date.fromisoformat(window["end"])
        if status == "complete":
            rows = _parse_esun_twd_txn_results([result])
            row_count = snapshot.get("gridRowCount")
            total_count = snapshot.get("totalCount")
            grid_candidate_count = snapshot.get("gridCandidateCount")
            grid_rows = snapshot.get("gridRows")
            if (
                type(row_count) is not int
                or row_count <= 0
                or type(total_count) is not int
                or total_count != row_count
                or type(grid_candidate_count) is not int
                or grid_candidate_count != 1
                or snapshot.get("emptyMarker") is not None
                or not isinstance(grid_rows, list)
                or len(grid_rows) != row_count
                or len(rows) != row_count
                or any(
                    not (
                        window_start <= date.fromisoformat(row["datetime"][:10]) <= window_end
                        and window_start <= date.fromisoformat(row["account_date"]) <= window_end
                    )
                    for row in rows
                )
            ):
                raise ValueError("invalid E.SUN TWD complete result coverage")
            parsed.extend(rows)
        elif status == "explicit_empty":
            grid_candidate_count = snapshot.get("gridCandidateCount")
            if (
                snapshot.get("hasGrid") is not False
                or type(grid_candidate_count) is not int
                or grid_candidate_count != 0
                or snapshot.get("gridText") != ""
                or snapshot.get("gridRows") != []
                or type(snapshot.get("gridRowCount")) is not int
                or snapshot["gridRowCount"] != 0
                or type(snapshot.get("totalCount")) is not int
                or snapshot["totalCount"] != 0
                or snapshot.get("emptyMarker") not in {
                    "查無交易資料", "查無資料", "無交易明細",
                }
                or _parse_esun_twd_txn_results([result]) != []
            ):
                raise ValueError("invalid E.SUN TWD empty result coverage")
        else:
            raise ValueError("invalid E.SUN TWD history status")
    return parsed


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
    if data.get("history_coverage") is not None:
        twd_rows = _validated_esun_twd_results_for_coverage(data)
    else:
        twd_rows = _parse_esun_twd_txn_results(data.get("twd_txn_results") or [])

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
            "post_date": (t.get("post_date") or "").replace("/", "-") or None,
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
            # 消費明細查詢頁沒有獨立入帳日；月結帳單 popup 有交易日／入帳日，
            # collector 會用 statement row 取代同 identity 的 consumption row。
            # 尚未進帳單、無 statement match 的 row 必須保留 post_date=NULL。
            bill_month = t.get("bill_month")
            row["bill_date"] = (
                f"{bill_month}-01" if isinstance(bill_month, str) and len(bill_month) == 7
                else None
            )
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
